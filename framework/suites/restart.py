"""What survives when a server restarts, stops, or loses its backend.

That is a cache's store, every server's identity and database, the
issuer's refresh tokens and clients, the directors' view of a server
that has gone, and an origin's answers while its storage is
unreachable. The suite controls the services through the control helper
(testlib/control.py), so it needs one, and skips the tiny topology,
whose servers are one process. It comes after every other suite but
`keys`, since it takes servers away for a while. Its objects go under
/protected-a/data/restart/<run>/, put at every origin, and are removed
afterward. A scenario with several cases records a row per case, named
`<scenario> <case>`, or `<scenario> <service> <case>`. The scenarios:

  cache-restart    each cache, restarted, serves the objects it held
                   from its store, with Age for a V2 cache, and the
                   origins send nothing for them
  state-survives   each server's ID and registration, and at the issuer
                   origin a user, a group, an API token, a downtime, a
                   refresh token, and a client's secret, are as they
                   were once origin-0, director-0, and the registry have
                   restarted
  ad-expiry        a cache, and with several origins an origin, killed
                   without a word: every director drops it once its ad
                   expires (Server.AdLifetime), and names it again once
                   it advertises again
  backend-outage   an origin whose storage service is stopped (`s3`,
                   `webdav`, or `lab-server`) refuses with a server error,
                   never a missing object, and serves again once it is
                   back; its health says so meanwhile
  cache-failover   with several caches, the clients get an object while
                   the first cache is paused, and killed, before the
                   directors drop it and after
  peer-director    with several directors, a director killed drops from
                   its peer's list of directors once its ad expires, and
                   returns
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from testlib import (
    accounts,
    common,
    control,
    credentials,
    director,
    issuer,
    metrics,
    stores,
    transfers,
    web,
)
from testlib.accounts import Api
from testlib.common import die, first, warn
from testlib.issuer import User
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

SCENARIOS = {
    "cache-restart": "each cache, restarted, serves what it held from its store",
    "state-survives": (
        "IDs, registrations, users, groups, an API token, a downtime, a refresh token, and"
        " a client's secret outlive restarts of origin-0, director-0, and the registry"
    ),
    "ad-expiry": "a server killed drops from every director once its ad expires, and returns",
    "backend-outage": (
        "an origin whose storage service is stopped answers a server error, never a missing"
        " object, and serves again once it is back"
    ),
    "cache-failover": (
        "the clients get an object while the first cache is paused, or killed, before the"
        " directors drop it and after (several caches)"
    ),
    "peer-director": "a director killed drops from its peer's list, and returns (several directors)",
}

NAMESPACE = "protected-a"
# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]
Rows = Union[tuple[str, str], list[Row]]

OBJECT_SIZE = 1 << 20
# How many objects each cache holds across its restart.
HELD_OBJECTS = 3
# How long a V2 cache may take to say, with Age, that it holds an object
# read whole; how long the directors may take to name a server again
# once it serves, or to drop one declared down; and how long a server's
# ad lives (Server.AdLifetime in config.d/base/20-server.yaml), past
# which every director must have dropped a server that said nothing.
HELD_WAIT = 30
NAMED_WAIT = 150
AD_LIFETIME = 90
EXPIRY_SLACK = 30
# How long an origin may take to serve again once its backend is back.
BACKEND_WAIT = 120
# How long an origin's health may take to notice its backend gone.
HEALTH_WAIT = 60
# The storage services behind the native backends, by ORIGIN_VARIANT.
BACKENDS = {"s3v2": "s3", "httpsv2": "webdav", "ssh": "lab-server"}
# The health component of each, where there is one (metrics/health.go).
BACKEND_COMPONENTS = {"ssh": "ssh-backend"}
# A downtime's severity, as the web UI offers it.
SEVERITY = "Outage (completely inaccessible)"
# The admin's password (framework/etc/server-web-passwd).
ADMIN_PASSWORD = "asdf"  # nosec B105
# The scope of an API token that reads /metrics (promMetricAuthHandler in
# web_ui/authorization.go takes API tokens).
SCRAPE_SCOPE = credentials.SCRAPE_SCOPE

LOCAL_METADATA = "/api/v1.0/server/localMetadata"
REGISTERED_SERVERS = "/api/v1.0/registry_ui/servers"
TOKENS = "/api/v1.0/tokens"
DOWNTIME = "/api/v1.0/downtime"
DIRECTORS = "/api/v1.0/director/directors"
HEALTH = "/api/v1.0/metrics/health"


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None.

    There is nothing in the tiny topology, whose servers are one
    process, or without /protected-a. Whether a control helper serves
    the run is asked as the suite runs, since asking waits.
    """
    if fed.tiny:
        return "the tiny topology's servers are one process"
    if NAMESPACE not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def outcome(problems: Sequence[str], note: str) -> Row:
    """Return a row with no case.

    It is FAIL with the first of problems, or PASS with note.
    """
    return ("", FAIL, first(problems)) if problems else ("", PASS, note)


class Test:
    """The federation, the run's objects, and the tokens scenarios share.

    Attributes:
        base: The run's collection, within /protected-a.
        tmp: A directory of the run's own, for the clients' files.
        caps: /protected-a's capabilities.
        objects: The run's objects' bytes, by rel within the namespace.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.base = f"data/restart/{self.run}"
        self.tmp = session.path("restart")
        os.makedirs(self.tmp)
        self.token = session.token(NAMESPACE)
        self.token_file = session.token_file(NAMESPACE)
        self.caps = self.fed.exports[NAMESPACE]
        self.objects: dict[str, bytes] = {}
        self._admins: dict[str, str] = {}
        self._runs = 0

    def rel(self, name: str) -> str:
        """Return where object name of the run is in the namespace."""
        return f"{self.base}/{name}"

    def path(self, name: str) -> str:
        """Return object name's path in the federation."""
        return f"/{NAMESPACE}/{self.rel(name)}"

    def url(self, name: str) -> str:
        """Return object name's federation URL."""
        return f"{self.fed.url}{self.path(name)}"

    def put(self, name: str, data: bytes) -> None:
        """Put object name, holding data, at every origin."""
        rel = self.rel(name)
        errors = stores.make_dirs(
            self.fed.origins, NAMESPACE, rel.rsplit("/", 1)[0], self.token
        )
        errors += stores.seed_everywhere(
            self.fed.origins, NAMESPACE, rel, data, self.token, self.caps
        )
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")
        self.objects[rel] = data

    def admin_token(self, web_url: str, key_dir: str) -> str:
        """Return a token that the server at web_url takes as its admin's.

        It is signed with the server's key in key_dir (see
        credentials.mint_admin()).
        """
        if web_url not in self._admins:
            self._admins[web_url] = credentials.mint_admin(
                self.fed,
                self.session.path(f"tokens/restart.admin.{director.host(web_url)}"),
                credentials.local_issuer(self.fed, web_url),
                common.signing_key(key_dir),
            )
        return self._admins[web_url]

    def admin(self, web_url: str, key_dir: str) -> Api:
        """Return the web API of the server at web_url, as its admin."""
        return Api(web_url, self.admin_token(web_url, key_dir))

    def held(self, cache: common.Cache, name: str) -> bool:
        """Return whether a V2 cache holds all of object name.

        The cache says so with Age.
        """
        answer = web.request("HEAD", f"{cache.url}{self.path(name)}", token=self.token)
        return answer.status == 200 and answer.header("Age").strip().isdigit()

    def fill(self, cache: common.Cache, name: str) -> Optional[str]:
        """Read object name whole through cache, which must serve it.

        For a V2 cache, wait until it says it holds it all.

        Returns:
            What went wrong, or None.
        """
        answer = web.request(
            "GET", f"{cache.url}{self.path(name)}", token=self.token, timeout=120
        )
        if answer.status != 200 or answer.body != self.objects[self.rel(name)]:
            return f"{cache.svc}: a GET of {name} got {answer.describe()}"
        if cache.kind == "v2" and not common.wait_for(
            lambda: self.held(cache, name), HELD_WAIT, interval=1
        ):
            return f"{cache.svc} does not hold {name} {HELD_WAIT}s after reading it whole"
        return None

    def directory(self, name: str) -> str:
        """Return a new directory of the run's for a client run."""
        self._runs += 1
        path = os.path.join(self.tmp, f"{self._runs:03d}-{name}")
        os.makedirs(path)
        return path

    def named_by(self, route: str, path: str, host: str) -> list[str]:
        """Return the directors that would send a GET of path to host.

        Each is asked by route, as for director.naming().
        """
        return director.naming(self.fed.directors, route, path, host)

    def wait_named(self, route: str, path: str, host: str, seconds: float) -> Optional[float]:
        """Wait up to seconds until every director names host for path.

        Returns:
            How long that took; None if not within seconds.
        """
        started = time.monotonic()
        if common.wait_for(
            lambda: len(self.named_by(route, path, host)) == len(self.fed.directors),
            seconds,
            interval=5,
        ):
            return time.monotonic() - started
        return None

    def wait_unnamed(self, route: str, path: str, host: str, seconds: float) -> Optional[float]:
        """Wait up to seconds until no director names host for path.

        Returns:
            How long that took; None if not within seconds.
        """
        started = time.monotonic()
        if common.wait_for(lambda: not self.named_by(route, path, host), seconds, interval=5):
            return time.monotonic() - started
        return None


def sent_bytes(fed: common.Federation) -> Optional[float]:
    """Return how many bytes the native origins have sent for GETs, in all.

    Returns:
        The sum of their pelican_http_bytes_total; None where they keep
        no such counter, or one did not answer.
    """
    if not fed.native_origins:
        return None
    total = 0.0
    for origin in fed.origins:
        found = metrics.fetch(origin.web_url)
        if found is None:
            return None
        total += found.value(
            "pelican_http_bytes_total", server_type="origin", direction="out", method="GET"
        )
    return total


# --------------------------------------------------------------------------
# cache-restart


def cache_restart(test: Test) -> list[Row]:
    """Check that each cache, restarted, serves what it held from its store.

    Each cache holds HELD_OBJECTS objects read whole (a V2 cache says so
    with Age). Restarted, and once every director names it again, it
    must serve each from its store, intact, with Age for a V2 cache, and
    the native origins must have sent less than an object for GETs
    meanwhile. An XRootD cache sends no Age, so there only the origins'
    counters tell; over XRootD origins, which keep none, the bytes alone
    must be right.
    """
    rows: list[Row] = []
    for cache in test.fed.caches:
        names = [f"held-{cache.svc}-{n}" for n in range(HELD_OBJECTS)]
        for name in names:
            test.put(name, os.urandom(OBJECT_SIZE))
        problems: list[str] = []
        for name in names:
            why = test.fill(cache, name)
            if why:
                problems.append(why)
        if problems:
            rows.append((cache.svc, FAIL, f"before the restart, {first(problems)}"))
            continue
        host = director.host(cache.url)
        federated = not cache.site_local
        path = test.path(names[0])
        if federated and len(test.named_by("object", path, host)) != len(test.fed.directors):
            rows.append((cache.svc, FAIL, "before the restart, not every director named it"))
            continue
        before = sent_bytes(test.fed)
        try:
            control.restart(cache.svc)
        except control.ControlError as e:
            rows.append((cache.svc, FAIL, str(e)))
            continue
        took = test.wait_named("object", path, host, NAMED_WAIT) if federated else 0.0
        if took is None:
            rows.append(
                (
                    cache.svc,
                    FAIL,
                    f"{NAMED_WAIT}s after the restart, not every director named it",
                )
            )
            continue
        ages: list[int] = []
        for name in names:
            answer = web.request(
                "GET", f"{cache.url}{test.path(name)}", token=test.token, timeout=120
            )
            if answer.status != 200 or answer.body != test.objects[test.rel(name)]:
                problems.append(f"after the restart, a GET of {name} got {answer.describe()}")
            elif cache.kind == "v2":
                age = answer.header("Age").strip()
                if not age.isdigit():
                    problems.append(
                        f"after the restart, {name} came with no Age, so not from its store"
                    )
                else:
                    ages.append(int(age))
        after = sent_bytes(test.fed)
        if before is not None and after is not None and after - before >= OBJECT_SIZE:
            problems.append(
                f"the origins sent {after - before:.0f} bytes for GETs across the restart,"
                + " an object's worth or more"
            )
        if problems:
            rows.append((cache.svc, FAIL, first(problems)))
            continue
        note = f"named again {took:.0f}s after its restart; {len(names)} objects from its store"
        if ages:
            note += f" (Age {min(ages)}..{max(ages)})"
        if before is None:
            note += "; the origins keep no byte counters"
        rows.append((cache.svc, PASS, note))
    if not rows:
        return [("", SKIP, "this shape has no cache")]
    return rows


# --------------------------------------------------------------------------
# state-survives


@dataclass(frozen=True)
class Server:
    """An origin or a cache, whose record of itself the suite reads.

    Attributes:
        key_dir: The directory of its signing key, under framework/var.
    """

    svc: str
    web_url: str
    key_dir: str


def servers_of(fed: common.Federation) -> list[Server]:
    """Return the origins (another owner's too) and the caches."""
    found = [Server(o.svc, o.web_url, f"issuer-keys/{o.svc}") for o in fed.origins]
    found += [Server(o.origin.svc, o.web_url, o.key_dir) for o in fed.owners]
    found += [Server(c.svc, c.web_url, f"issuer-keys/{c.svc}") for c in fed.caches]
    return found


@dataclass
class Recorded:
    """What state-survives recorded before the restarts.

    Attributes:
        metadata: What each server records of itself, by service.
        registered: Each server the registry lists: its ID and name.
    """

    metadata: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    registered: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    user_id: str = ""
    group_id: str = ""
    api_token: str = ""
    api_token_id: str = ""
    downtime_id: str = ""
    refresh: str = ""
    client: Optional[issuer.Client] = None


def local_metadata(test: Test, server: Server) -> dict[str, Any]:
    """Return what server records of itself, as its admin reads it.

    Returns:
        Its record (its ID, name, and type); {} if it will not say.
    """
    answer = test.admin(server.web_url, server.key_dir).call("GET", LOCAL_METADATA)
    return answer.object if answer.status == 200 else {}


def registered_servers(test: Test) -> Optional[list[tuple[str, str]]]:
    """Return each server the registry lists, as its admin reads them.

    Returns:
        Each one's ID and name; None if there is no registry, or it will
        not say.
    """
    fed = test.fed
    if not fed.registry_url:
        return None
    host = director.host(fed.registry_url)
    answer = test.admin(fed.registry_url, f"issuer-keys/{host}").call("GET", REGISTERED_SERVERS)
    if answer.status != 200:
        return None
    return [
        (str(entry.get("id", "")), str(entry.get("name", "")))
        for entry in (common.as_object(s) or {} for s in answer.array)
    ]


def issuer_origin(test: Test) -> tuple[Optional[common.Origin], str]:
    """Return the issuer origin (origin-0), if its issuer can be used.

    Returns:
        The origin, and ""; or None, and why its issuer cannot be used.
    """
    fed = test.fed
    if not fed.embedded_issuer or fed.external_issuer:
        return None, "the origins run no issuer of their own"
    return fed.origins[0], ""


def login_cookie(web_url: str) -> str:
    """Return a login cookie of the admin's at web_url."""
    return accounts.login(web_url, "admin", ADMIN_PASSWORD)


def record_state(test: Test, found: Recorded) -> list[str]:
    """Record into found the state that state-survives checks.

    Record every server's metadata and the registry's servers, and at
    the issuer origin make a user, a group, an API token, a downtime, a
    client, and a refresh token.

    Returns:
        What went wrong.
    """
    problems: list[str] = []
    for server in servers_of(test.fed):
        found.metadata[server.svc] = local_metadata(test, server)
        if not found.metadata[server.svc].get("id"):
            problems.append(f"{server.svc} records no ID of itself ({LOCAL_METADATA})")
    listed = registered_servers(test)
    if listed is None and test.fed.registry_url:
        problems.append(f"the registry's {REGISTERED_SERVERS} did not answer the admin")
    found.registered = listed or []
    origin, _ = issuer_origin(test)
    if origin is None:
        return problems
    web_url = origin.web_url
    admin = test.admin(web_url, f"issuer-keys/{origin.svc}")
    try:
        accounts.agree_to_aup(admin)
        found.user_id = accounts.ensure_user(admin, f"restart-user-{test.run}")
        found.group_id = accounts.create_group(admin, f"restart-group-{test.run}")
        cookie = login_cookie(web_url)
        made = web.request(
            "POST",
            web_url + TOKENS,
            upload=json.dumps(
                {
                    "name": f"restart-{test.run}",
                    "scopes": [SCRAPE_SCOPE],
                    "expiration": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
                    ),
                }
            ).encode(),
            headers={**accounts.JSON, "Cookie": f"login={cookie}"},
        )
        token = str((common.as_object(json.loads(made.body or b"{}")) or {}).get("token", ""))
        if made.status not in (200, 201) or "." not in token:
            problems.append(f"making an API token: {made.describe()}")
        else:
            found.api_token, found.api_token_id = token, token.split(".", 1)[0]
        now = int(time.time() * 1000)
        found.downtime_id = str(uuid.uuid4())
        declared = {
            "class": "SCHEDULED",
            "severity": SEVERITY,
            "description": f"pelican-test-framework restart {test.run}",
            "startTime": now + 86_400_000,
            "endTime": now + 90_000_000,
        }
        answer = admin.call(
            "POST", f"{DOWNTIME}/{found.downtime_id}", declared, accounts.csrf(admin)
        )
        if not answer.ok:
            problems.append(f"declaring a downtime for tomorrow: {answer.describe()}")
            found.downtime_id = ""
        found.client = issuer.create_client(
            web_url,
            f"/{NAMESPACE}",
            test.admin_token(web_url, f"issuer-keys/{origin.svc}"),
            name=f"pelican-test-framework restart {test.run}",
        )
        tokens = issuer.device_tokens(
            found.client,
            User(token=test.admin_token(web_url, f"issuer-keys/{origin.svc}")),
            ("openid", "offline_access", "storage.read:/"),
        )
        if not tokens.refresh:
            problems.append("the device flow issued no refresh token")
        found.refresh = tokens.refresh or ""
    except (accounts.ApiError, issuer.IssuerError) as e:
        problems.append(str(e))
    return problems


def restart_and_settle(test: Test, svc: str, route: str, host: str) -> Optional[str]:
    """Restart svc, and wait until every director names host for the probe.

    The directors are asked by route; with none, nothing is waited for.

    Returns:
        Why that failed, or None.
    """
    try:
        control.restart(svc)
    except control.ControlError as e:
        return str(e)
    if route and test.wait_named(route, test.path("probe"), host, NAMED_WAIT) is None:
        return f"{NAMED_WAIT}s after {svc} restarted, not every director named {host}"
    return None


def state_survives(test: Test) -> list[Row]:
    """Check that what record_state() records outlives restarts.

    Once origin-0, director-0, and the registry have restarted, each
    server's ID and name must be as recorded (`ids`), the registry must
    list the same servers, none twice (`registrations`), the user and
    group must be there (`accounts`), the API token must still read
    /metrics and be listed (`api-token`), the downtime must still be
    declared (`downtime`), and the refresh token must refresh, with the
    client's secret (`issuer`): the issuer's HMAC key is derived from a
    master key kept in its database (oauth2/issuer/provider.go).
    """
    fed = test.fed
    test.put("probe", os.urandom(4096))
    found = Recorded()
    problems = record_state(test, found)
    rows: list[Row] = []
    if problems:
        rows.append(("setup", FAIL, first(problems)))
    origin = fed.origins[0]
    origin_host = director.host(origin.url)
    # Each service restarted, and whether the directors must then name
    # origin-0 again (after the registry's restart, nothing changes).
    restarted = [(origin.svc, "origin")]
    if fed.directors:
        restarted.append((director.host(fed.directors[0]), "origin"))
    if fed.registry_url:
        restarted.append((director.host(fed.registry_url), ""))
    for svc, route in restarted:
        why = restart_and_settle(test, svc, route, origin_host)
        if why:
            return rows + [("restarts", FAIL, why)]
    rows.append(("restarts", PASS, ", ".join(svc for svc, _ in restarted)))
    problems = list[str]()
    for server in servers_of(fed):
        before, after = found.metadata.get(server.svc, {}), local_metadata(test, server)
        for key in ("id", "name"):
            if before.get(key) and after.get(key) != before.get(key):
                problems.append(
                    f"{server.svc}'s {key} is {after.get(key)!r}, was {before.get(key)!r}"
                )
    rows.append(("ids", *outcome(problems, f"{len(found.metadata)} server(s) unchanged")[1:]))
    if fed.registry_url:
        listed = registered_servers(test)
        problems = list[str]()
        if listed is None:
            problems.append("the registry would not list its servers")
        else:
            if sorted(listed) != sorted(found.registered):
                problems.append(
                    f"the registry lists {sorted(listed)}, listed {sorted(found.registered)}"
                )
            names = [name for _, name in listed]
            if len(set(names)) != len(names):
                problems.append(f"a server is registered twice: {names}")
        rows.append(
            (
                "registrations",
                *outcome(problems, f"{len(listed or [])} server(s), none twice")[1:],
            )
        )
    issuing, why = issuer_origin(test)
    if issuing is None:
        rows.append(("issuer", SKIP, why))
        return rows
    web_url = issuing.web_url
    admin = test.admin(web_url, f"issuer-keys/{issuing.svc}")
    try:
        problems = list[str]()
        if (
            found.user_id
            and accounts.find_user(admin, f"restart-user-{test.run}") != found.user_id
        ):
            problems.append("the user is gone, or has another ID")
        if (
            found.group_id
            and accounts.find_group(admin, f"restart-group-{test.run}") != found.group_id
        ):
            problems.append("the group is gone, or has another ID")
        rows.append(("accounts", *outcome(problems, "the user and the group are there")[1:]))
        if found.api_token:
            problems = list[str]()
            scraped = web.request("GET", f"{web_url}/metrics", token=found.api_token)
            if scraped.status != 200:
                problems.append(f"/metrics with the API token: {scraped.describe()}")
            listed_tokens = web.request(
                "GET", web_url + TOKENS, headers={"Cookie": f"login={login_cookie(web_url)}"}
            )
            ids = [
                str((common.as_object(e) or {}).get("id"))
                for e in common.as_array(json.loads(listed_tokens.body or b"[]")) or []
            ]
            if found.api_token_id not in ids:
                problems.append(f"the token {found.api_token_id} is not listed")
            rows.append(("api-token", *outcome(problems, "reads /metrics, and is listed")[1:]))
        if found.downtime_id:
            answer = admin.call("GET", DOWNTIME)
            ids = [str((common.as_object(d) or {}).get("id")) for d in answer.array]
            if found.downtime_id not in ids:
                rows.append(
                    ("downtime", FAIL, f"the downtime is not listed: {answer.describe()}")
                )
            else:
                rows.append(("downtime", PASS, "still declared"))
        if found.client and found.refresh:
            refreshed = issuer.refresh(found.client, found.refresh)
            if refreshed.status != 200:
                rows.append(
                    (
                        "issuer",
                        FAIL,
                        f"refreshing: {refreshed.describe()} ({issuer.error_of(refreshed)})",
                    )
                )
            else:
                rows.append(("issuer", PASS, "the refresh token and the client's secret work"))
    except accounts.ApiError as e:
        rows.append(("accounts", FAIL, str(e)))
    finally:
        cleanup_state(test, found)
    return rows


def cleanup_state(test: Test, found: Recorded) -> None:
    """Remove what state-survives made."""
    origin, _ = issuer_origin(test)
    if origin is None:
        return
    web_url = origin.web_url
    admin = test.admin(web_url, f"issuer-keys/{origin.svc}")
    try:
        if found.downtime_id:
            admin.call(
                "DELETE", f"{DOWNTIME}/{found.downtime_id}", headers=accounts.csrf(admin)
            )
        if found.api_token_id:
            web.request(
                "DELETE",
                f"{web_url}{TOKENS}/{found.api_token_id}",
                headers={"Cookie": f"login={login_cookie(web_url)}"},
            )
        if found.client:
            issuer.delete_client(
                web_url,
                f"/{NAMESPACE}",
                test.admin_token(web_url, f"issuer-keys/{origin.svc}"),
                found.client.client_id,
            )
        if found.group_id:
            accounts.delete_group(admin, found.group_id)
        if found.user_id:
            accounts.delete_user(admin, found.user_id)
    except accounts.ApiError as e:
        warn(f"could not remove what state-survives made: {e}")


# --------------------------------------------------------------------------
# ad-expiry


def ad_expiry(test: Test) -> list[Row]:
    """Check that a server killed drops from every director, and returns.

    The first federated cache, and with several origins the last origin,
    is killed (SIGKILL, so it says nothing): within Server.AdLifetime
    and some slack, no director may name it, since its ad has expired
    (the directors keep ads in a cache with that TTL,
    director/cache_ads.go); started again, every director must name it
    within NAMED_WAIT seconds. The note times both.
    """
    fed = test.fed
    if not fed.directors:
        return [("", SKIP, "a standalone origin has no director")]
    test.put("probe", os.urandom(4096))
    path = test.path("probe")
    targets: list[tuple[str, str, str]] = []
    if fed.federated_caches:
        cache = fed.federated_caches[0]
        targets.append((cache.svc, "object", director.host(cache.url)))
    if len(fed.origins) > 1:
        origin = fed.origins[-1]
        targets.append((origin.svc, "origin", director.host(origin.url)))
    if not targets:
        return [("", SKIP, "no cache, and one origin")]
    rows: list[Row] = []
    for svc, route, host in targets:
        if len(test.named_by(route, path, host)) != len(fed.directors):
            rows.append((svc, FAIL, "before it was killed, not every director named it"))
            continue
        try:
            control.kill(svc)
        except control.ControlError as e:
            rows.append((svc, FAIL, str(e)))
            continue
        dropped = test.wait_unnamed(route, path, host, AD_LIFETIME + EXPIRY_SLACK)
        try:
            control.start(svc)
        except control.ControlError as e:
            rows.append((svc, FAIL, str(e)))
            continue
        back = test.wait_named(route, path, host, NAMED_WAIT)
        if dropped is None:
            rows.append(
                (
                    svc,
                    FAIL,
                    f"{AD_LIFETIME + EXPIRY_SLACK}s after it was killed, a director still named it",
                )
            )
        elif back is None:
            rows.append(
                (
                    svc,
                    FAIL,
                    f"{NAMED_WAIT}s after it started again, not every director named it",
                )
            )
        else:
            rows.append(
                (
                    svc,
                    PASS,
                    f"dropped {dropped:.0f}s after it was killed, named again {back:.0f}s after it started",
                )
            )
    return rows


# --------------------------------------------------------------------------
# backend-outage


def health_component(test: Test, origin: common.Origin, component: str) -> str:
    """Return what origin's health report says of component.

    Returns:
        Its status, as the admin reads it; "" if nothing.
    """
    answer = test.admin(origin.web_url, f"issuer-keys/{origin.svc}").call("GET", HEALTH)
    components = common.as_object(answer.object.get("components")) or {}
    found = common.as_object(components.get(component)) or {}
    return str(found.get("status", ""))


def restart_backend(backend: str) -> Optional[str]:
    """Start backend again, and return why that failed, or None."""
    try:
        control.start(backend)
    except control.ControlError as e:
        return str(e)
    return None


def backend_outage(test: Test) -> list[Row]:
    """Check that an origin answers 5xx while its backend is stopped.

    With the origins' storage service stopped: at origin-0, where it
    takes direct clients, a GET and a HEAD of an object, and a PUT of a
    new one, must each get a server error (5xx), never 404 or 405, nor
    the object's bytes (`direct`); through the first federated cache, a
    GET of an object it does not hold must get no 404 either (`cache`);
    and for an `ssh` origin, its health must say its backend is not ok
    within HEALTH_WAIT seconds (`health`). Once the service is back, the
    origin must serve the object again within BACKEND_WAIT seconds,
    without a restart, as must the cache (`recovered`).
    """
    fed = test.fed
    backend = BACKENDS.get(fed.origin_variant)
    if backend is None:
        return [("", SKIP, f"a {fed.origin_variant} origin has no storage service to stop")]
    origin = fed.origins[0]
    direct = "DirectReads" in test.caps
    test.put("outage", os.urandom(OBJECT_SIZE))
    test.put("outage-cold", os.urandom(OBJECT_SIZE))
    data = test.objects[test.rel("outage")]
    cache = fed.federated_caches[0] if fed.federated_caches else None
    component = BACKEND_COMPONENTS.get(fed.origin_variant)
    rows: list[Row] = []
    try:
        control.stop(backend)
    except control.ControlError as e:
        return [("", FAIL, str(e))]
    try:
        problems: list[str] = []
        if direct:
            for method, name, upload in (
                ("GET", "outage", None),
                ("HEAD", "outage", None),
                ("PUT", "outage-put", os.urandom(4096)),
            ):
                answer = web.request(
                    method,
                    f"{origin.url}{test.path(name)}",
                    token=test.token,
                    upload=upload,
                    timeout=120,
                )
                if answer.status is None:
                    problems.append(f"{method} with {backend} stopped: no answer")
                elif not 500 <= answer.status < 600:
                    problems.append(
                        f"{method} with {backend} stopped: {answer.describe()}, not 5xx"
                    )
                elif method == "GET" and answer.body == data:
                    problems.append(
                        f"GET with {backend} stopped: 5xx, but with the object's bytes"
                    )
            rows.append(
                (
                    "direct",
                    *outcome(problems, f"GET, HEAD, and PUT: 5xx with {backend} stopped")[1:],
                )
            )
        else:
            rows.append(("direct", SKIP, "/protected-a takes no direct clients"))
        if cache is not None:
            answer = web.request(
                "GET", f"{cache.url}{test.path('outage-cold')}", token=test.token, timeout=120
            )
            if answer.status is None:
                rows.append(("cache", FAIL, f"through {cache.svc}: no answer"))
            elif answer.status in (404, 405) or answer.ok:
                rows.append(
                    (
                        "cache",
                        FAIL,
                        f"through {cache.svc}: {answer.describe()}, not a server error",
                    )
                )
            else:
                rows.append(("cache", PASS, f"through {cache.svc}: {answer.describe()}"))
        if component:
            seen = ""

            def unwell() -> bool:
                nonlocal seen
                seen = health_component(test, origin, component)
                return seen not in ("", "ok")

            if common.wait_for(unwell, HEALTH_WAIT, interval=5):
                rows.append(("health", PASS, f"{component}: {seen}"))
            else:
                rows.append(("health", FAIL, f"{component} stayed {seen!r} for {HEALTH_WAIT}s"))
    finally:
        started_again = restart_backend(backend)
    if started_again:
        rows.append(("recovered", FAIL, started_again))
        return rows

    def serves() -> bool:
        if direct:
            answer = web.request(
                "GET", f"{origin.url}{test.path('outage')}", token=test.token, timeout=60
            )
            return answer.status == 200 and answer.body == data
        if cache is not None:
            answer = web.request(
                "GET", f"{cache.url}{test.path('outage')}", token=test.token, timeout=60
            )
            return answer.status == 200 and answer.body == data
        return True

    started = time.monotonic()
    if not common.wait_for(serves, BACKEND_WAIT, interval=5):
        rows.append(
            (
                "recovered",
                FAIL,
                f"{BACKEND_WAIT}s after {backend} started, the object does not come back",
            )
        )
        return rows
    took = time.monotonic() - started
    problems = []
    if direct:
        answer = web.request("HEAD", f"{origin.url}{test.path('outage')}", token=test.token)
        if answer.status != 200:
            problems.append(f"HEAD once {backend} was back: {answer.describe()}")
        if "Writes" in test.caps:
            answer = web.request(
                "PUT",
                f"{origin.url}{test.path('outage-after')}",
                token=test.token,
                upload=os.urandom(4096),
            )
            if not answer.ok:
                problems.append(f"PUT once {backend} was back: {answer.describe()}")
    if cache is not None:
        answer = web.request(
            "GET", f"{cache.url}{test.path('outage-cold')}", token=test.token, timeout=120
        )
        if answer.status != 200 or answer.body != test.objects[test.rel("outage-cold")]:
            problems.append(f"through {cache.svc} once {backend} was back: {answer.describe()}")
    if component:
        seen = health_component(test, origin, component)
        if not common.wait_for(
            lambda: health_component(test, origin, component) == "ok", HEALTH_WAIT, 5
        ):
            problems.append(f"{component} is {seen!r} {HEALTH_WAIT}s after {backend} came back")
    rows.append(
        (
            "recovered",
            *outcome(problems, f"serving again {took:.0f}s after {backend} started")[1:],
        )
    )
    return rows


# --------------------------------------------------------------------------
# cache-failover


@dataclass
class Fetched:
    """How a client run went.

    Attributes:
        hosts: The host of each attempt, the last the one that served.
    """

    status: int
    said: str
    directory: str
    hosts: list[str]


def get_pelican(test: Test, name: str, url: str) -> Fetched:
    """Run `pelican object get` of url into a directory of its own.

    It writes transfer stats, which give the host of each attempt.
    """
    directory = test.directory(name)
    stats = os.path.join(directory, "stats.json")
    status, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--token",
        test.token_file,
        "--transfer-stats",
        stats,
        url,
        "object",
        cwd=directory,
    )
    hosts: list[str] = []
    try:
        with open(stats, encoding="utf-8") as f:
            results = json.load(f)
        pending: list[Any] = [results]
        while pending:
            item = pending.pop()
            nested = common.as_array(item)
            if nested is not None:
                pending.extend(nested)
                continue
            result = common.as_object(item) or {}
            attempts = [
                common.as_object(a) or {} for a in common.as_array(result.get("attempts")) or []
            ]
            for attempt in sorted(attempts, key=lambda a: int(a.get("attemptNumber") or 0)):
                hosts.append(str(attempt.get("endpoint") or "").partition(":")[0])
    except (FileNotFoundError, ValueError):
        pass
    return Fetched(status, out + err, directory, hosts)


def get_plugin(test: Test, name: str, url: str) -> Fetched:
    """Run stash_plugin's download of url into a directory of its own.

    The server's token is in its credentials directory.
    """
    directory = test.directory(name)
    creds = os.path.join(test.tmp, f"{name}.creds")
    os.makedirs(creds, exist_ok=True)
    shutil.copyfile(test.token_file, os.path.join(creds, "scitokens.use"))
    infile, outfile = (os.path.join(test.tmp, f"{name}.{ext}") for ext in ("in", "ad"))
    with open(infile, "w", encoding="utf-8") as f:
        f.write(f'[ Url="{url}"; LocalFileName="object" ]\n')
    env = dict(test.session.env)
    env["_CONDOR_CREDS"] = creds
    try:
        done = subprocess.run(  # nosec B603
            [test.session.plugin, "-infile", infile, "-outfile", outfile],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLIENT_TIMEOUT,
            check=False,
        )
        status, output = done.returncode, done.stderr or done.stdout
    except subprocess.TimeoutExpired:
        status, output = -1, f"timed out after {CLIENT_TIMEOUT:.0f}s"
    try:
        with open(outfile, encoding="utf-8") as f:
            ads = f.read()
    except FileNotFoundError:
        ads = ""
    hosts: list[str] = []
    for ad in ads.splitlines():
        if "TransferSuccess = true" in ad:
            endpoints = sorted(
                (int(n), host) for n, host in re.findall(r'Endpoint(\d+) = "([^":]*)', ad)
            )
            hosts = [host for _, host in endpoints]
    return Fetched(status, ads or output, directory, hosts)


def failover_problems(fetch: Fetched, data: bytes, dead: str, client: str) -> list[str]:
    """Return what is wrong with a download while cache dead is away.

    client's download must succeed, with the object, served by another
    cache.
    """
    if client == "pelican" and fetch.status != 0:
        return [f"exit {fetch.status}: {last_line(fetch.said)}"]
    if client == "plugin" and not transfers.plugin_passed(fetch.said):
        return [f"exit {fetch.status}: {last_line(fetch.said)}"]
    problems: list[str] = []
    if common.sha256(os.path.join(fetch.directory, "object")) != common.digest(data):
        problems.append("the file is not the object")
    if not fetch.hosts:
        problems.append("no host recorded")
    elif fetch.hosts[-1] == dead:
        problems.append(f"served by {dead}, which is away")
    return problems


def cache_failover(test: Test) -> list[Row]:
    """Check that the clients get an object while the first cache is away.

    With the first federated cache paused (its ports take connections
    that nothing answers), and then killed (they refuse), `pelican
    object get` and stash_plugin of an object through the federation
    must each succeed, served by another cache, while the directors
    still name the first (before its ad expires), and once they have
    dropped it. The note says which caches each client tried. Afterward
    the cache is unpaused, or started, and every director must name it
    again.
    """
    fed = test.fed
    caches = fed.federated_caches
    if len(caches) < 2:
        return [("", SKIP, "fewer than two federated caches ('-p topo-multi-cache')")]
    dead = caches[0]
    host = director.host(dead.url)
    test.put("probe", os.urandom(4096))
    path = test.path("probe")
    rows: list[Row] = []
    for mode, away, back in (
        ("paused", control.pause, control.unpause),
        ("killed", control.kill, control.start),
    ):
        objects = {
            f"{mode}-{when}-{client}": os.urandom(OBJECT_SIZE)
            for when in ("early", "late")
            for client in ("pelican", "plugin")
        }
        for name, data in objects.items():
            test.put(name, data)
        if len(test.named_by("object", path, host)) != len(fed.directors):
            rows.append(
                (mode, FAIL, f"before {dead.svc} was {mode}, not every director named it")
            )
            continue
        try:
            away(dead.svc)
        except control.ControlError as e:
            rows.append((mode, FAIL, str(e)))
            continue
        try:
            for when in ("early", "late"):
                if when == "late":
                    dropped = test.wait_unnamed(
                        "object", path, host, AD_LIFETIME + EXPIRY_SLACK
                    )
                    if dropped is None:
                        rows.append(
                            (
                                f"{mode} late",
                                FAIL,
                                f"{AD_LIFETIME + EXPIRY_SLACK}s after {dead.svc} was {mode}, a director still named it",
                            )
                        )
                        continue
                elif not test.named_by("object", path, host):
                    rows.append(
                        (
                            f"{mode} early",
                            INCONCLUSIVE,
                            f"the directors had dropped {dead.svc} before the clients ran",
                        )
                    )
                    continue
                problems: list[str] = []
                tried: list[str] = []
                for client, runner in (("pelican", get_pelican), ("plugin", get_plugin)):
                    name = f"{mode}-{when}-{client}"
                    fetch = runner(test, name, test.url(name))
                    problems += [
                        f"{client}: {p}"
                        for p in failover_problems(fetch, objects[name], dead.svc, client)
                    ]
                    tried.append(f"{client} tried {fetch.hosts or 'nothing recorded'}")
                rows.append((f"{mode} {when}", *outcome(problems, "; ".join(tried))[1:]))
        finally:
            try:
                back(dead.svc)
                took = test.wait_named("object", path, host, NAMED_WAIT)
                if took is None:
                    rows.append(
                        (
                            f"{mode} back",
                            FAIL,
                            f"{NAMED_WAIT}s after {dead.svc} came back, not every director named it",
                        )
                    )
                else:
                    rows.append(
                        (f"{mode} back", PASS, f"named again {took:.0f}s after it came back")
                    )
            except control.ControlError as e:
                rows.append((f"{mode} back", FAIL, str(e)))
    return rows


# --------------------------------------------------------------------------
# peer-director


def listed_directors(url: str) -> Optional[set[str]]:
    """Return the hosts in the director at url's list of directors.

    Returns:
        The hosts; None if it gave no list.
    """
    answer = web.request("GET", f"{url}{DIRECTORS}")
    if answer.status != 200:
        return None
    try:
        found = common.as_array(json.loads(answer.body)) or []
    except ValueError:
        return None
    return {
        director.host(str((common.as_object(d) or {}).get("advertise_url", ""))) for d in found
    }


def peer_director(test: Test) -> list[Row]:
    """Check that a director killed drops from its peer's list, and returns.

    With several directors, the second killed: within Server.AdLifetime
    and some slack, the first must list only itself at
    /api/v1.0/director/directors, since its peer's ad has expired
    (director/director_advertise.go); once the second starts again, the
    first must list both within NAMED_WAIT seconds.
    """
    fed = test.fed
    if len(fed.directors) < 2:
        return [("", SKIP, "fewer than two directors ('-p topo-multi-director')")]
    first_url, peer_url = fed.directors[0], fed.directors[1]
    me, peer = director.host(first_url), director.host(peer_url)
    if listed_directors(first_url) != {me, peer}:
        return [
            ("", FAIL, f"before the peer was killed, {me} lists {listed_directors(first_url)}")
        ]
    try:
        control.kill(peer)
    except control.ControlError as e:
        return [("", FAIL, str(e))]
    started = time.monotonic()
    dropped = common.wait_for(
        lambda: listed_directors(first_url) == {me}, AD_LIFETIME + EXPIRY_SLACK, 5
    )
    took_drop = time.monotonic() - started
    try:
        control.start(peer)
    except control.ControlError as e:
        return [("", FAIL, str(e))]
    started = time.monotonic()
    back = common.wait_for(lambda: listed_directors(first_url) == {me, peer}, NAMED_WAIT, 5)
    took_back = time.monotonic() - started
    if not dropped:
        return [
            (
                "",
                FAIL,
                f"{AD_LIFETIME + EXPIRY_SLACK}s after {peer} was killed, {me} lists {listed_directors(first_url)}",
            )
        ]
    if not back:
        return [
            (
                "",
                FAIL,
                f"{NAMED_WAIT}s after {peer} started again, {me} lists {listed_directors(first_url)}",
            )
        ]
    return [
        (
            "",
            PASS,
            f"{peer} dropped {took_drop:.0f}s after it was killed, listed again {took_back:.0f}s after it started",
        )
    ]


# --------------------------------------------------------------------------

RUN: dict[str, Callable[[Test], Rows]] = {
    "cache-restart": cache_restart,
    "state-survives": state_survives,
    "ad-expiry": ad_expiry,
    "backend-outage": backend_outage,
    "cache-failover": cache_failover,
    "peer-director": peer_director,
}


def guarded(name: str, test: Test) -> list[Row]:
    """Return scenario name's rows; one failing row if it raised."""
    try:
        found = RUN[name](test)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]
    if isinstance(found, list):
        return found
    return [("", found[0], found[1])]


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    why = control.unavailable()
    if why:
        for name in selected:
            results.add(name, SKIP, why)
        return
    tokens = session.tokens()
    errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/restart", tokens)
    if errors:
        die(f"could not remove earlier runs' objects: {first(errors)}")
    test = Test(session)
    print(f"Restarts against {fed.url}, objects under /{NAMESPACE}/{test.base}/\n")
    try:
        for name in selected:
            for case, status, note in guarded(name, test):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/restart", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
        shutil.rmtree(test.tmp, ignore_errors=True)
