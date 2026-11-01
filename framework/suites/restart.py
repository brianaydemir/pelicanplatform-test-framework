"""What survives when a server restarts, stops, or loses its backend.

That is a cache's store; every server's identity and database; the
issuer's refresh tokens and clients; the directors' view of a server
that has gone; and an origin's answers while its storage is
unreachable. See docs/tests/restarts.md.

The suite controls the services through the control helper
(testlib/control.py), so it needs one, and skips the tiny topology,
whose servers are one process. It comes after every other suite but
`keys`, since it takes servers away for a while. Its objects go under
/protected-a/data/restart/<run>/, at every origin, and are removed
afterward. A case's row is named `<scenario> <case>`, or
`<scenario> <service>`.

The reads that the scenarios check present tokens of the session's
profile; the objects are put in place, and the caches filled, with WLCG
tokens either way. `state-survives`, `ad-expiry`, `xrootd-wedged`, and
`peer-director` present no storage token, and run in the WLCG pass
alone.
"""

import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from testlib import (
    accounts,
    common,
    control,
    credentials,
    director,
    issuer,
    metrics,
    suite,
    transfers,
    web,
    webapi,
)
from testlib.accounts import Api
from testlib.common import die, first, outcome, warn
from testlib.issuer import User
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

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
    "xrootd-wedged": (
        "an XRootD origin and cache whose XRootD stops answering say so, and shut down; started"
        " again, they serve"
    ),
    "cache-failover": (
        "the clients get an object while the first cache is paused, or killed, before the"
        " directors drop it and after (several caches)"
    ),
    "origin-failover": (
        "the clients get an object, direct and through a cache, while the last origin is"
        " paused, or killed, before the directors drop it and after (several origins)"
    ),
    "peer-director": "a director killed drops from its peer's list, and returns (several directors)",
}

NAMESPACE = "protected-a"
# The scenarios that present no storage token (see suites/__init__.py).
ONE_PROFILE = ("state-survives", "ad-expiry", "xrootd-wedged", "peer-director")
# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

OBJECT_SIZE = 1 << 20
# How many objects each cache holds across its restart.
HELD_OBJECTS = 3
# How long the directors may take to name a server again once it serves;
# and how long a server's ad lives (Server.AdLifetime in
# config.d/base/20-server.yaml), and a margin, past which every director
# must have dropped a server that said nothing.
NAMED_WAIT = 150
AD_LIFETIME = 90
EXPIRY_SLACK = 30
# How long an origin may take to serve again once its backend is back.
BACKEND_WAIT = 120
# How long an origin's health may take to notice its backend gone.
HEALTH_WAIT = 60
# The storage services behind the origins, by ORIGIN_VARIANT: the native
# backends', and those of XRootD's S3 and HTTPS plugins. The rest store
# on the origin's own disk.
BACKENDS = {
    "s3v2": "s3",
    "httpsv2": "webdav",
    "ssh": "lab-server",
    "s3": "s3",
    "https": "webdav",
}
# The health component of each, where there is one (metrics/health.go).
BACKEND_COMPONENTS = {"ssh": "ssh-backend"}
# xrootd-wedged: the probe of a server's own XRootD, shortened from its
# defaults (a minute between probes, three minutes for one, and ten
# unanswered before XRootD is shut down); its health component; and how
# long the server may take to exit once XRootD stops: the window, a
# probe, and Xrootd.ShutdownTimeout (config.d/base/60-xrootd.yaml), with
# room.
LIVENESS_FILE = "90-liveness.yaml"
LIVENESS_CONFIG = (
    "Xrootd:\n"
    + "  LivenessCheckInterval: 2s\n"
    + "  LivenessCheckTimeout: 3s\n"
    + "  LivenessMaxUnresponsiveTime: 20s\n"
)
LIVENESS = "xrootd-liveness"
WEDGED_WAIT = 90
# A downtime's severity, as the web UI offers it.
SEVERITY = "Outage (completely inaccessible)"
# The scope of an API token that reads /metrics (promMetricAuthHandler in
# web_ui/authorization.go takes API tokens).
SCRAPE_SCOPE = credentials.SCRAPE_SCOPE

LOCAL_METADATA = "/api/v1.0/server/localMetadata"
REGISTERED_SERVERS = "/api/v1.0/registry_ui/servers"
DOWNTIME = "/api/v1.0/downtime"
DIRECTORS = "/api/v1.0/director/directors"
HEALTH = "/api/v1.0/metrics/health"


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None.

    There is nothing in the tiny topology, whose servers are one
    process, or without /protected-a. Whether a control helper serves
    the run is asked as the suite runs, since asking waits.
    """
    if fed.tiny:
        return "the tiny topology's servers are one process"
    return suite.unexported(fed)


class Test(suite.Test):
    """The run's objects in /protected-a, and the servers' admin tokens."""

    def __init__(self, session: Session):
        super().__init__(session, "restart", NAMESPACE)
        self._admins: dict[str, str] = {}

    def place(self, name: str, data: bytes) -> None:
        """Put object name, holding data, at every origin (put())."""
        why = self.put({self.rel(name): data})
        if why:
            die(f"could not put {self.rel(name)} in place: {why}")

    def admin_token(self, web_url: str, key_dir: str) -> str:
        """Return a token that the server at web_url takes as its admin's.

        It is signed with the server's key in key_dir (see
        credentials.mint_web_user()).
        """
        if web_url not in self._admins:
            self._admins[web_url] = credentials.mint_web_user(
                self.fed,
                self.session.path(f"tokens/restart.admin.{director.host(web_url)}"),
                web_url,
                "admin",
                key=common.signing_key(key_dir),
            )
        return self._admins[web_url]

    def admin(self, web_url: str, key_dir: str) -> Api:
        """Return the web API of the server at web_url, as its admin."""
        return Api(web_url, self.admin_token(web_url, key_dir))

    def named_by(self, route: str, path: str, host: str) -> list[str]:
        """Return the directors that would send a GET of path to host.

        Each is asked by route, as for director.naming().
        """
        return director.naming(self.fed.directors, route, path, host)

    def wait_named(self, route: str, path: str, host: str, seconds: float) -> float | None:
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

    def wait_unnamed(self, route: str, path: str, host: str, seconds: float) -> float | None:
        """Wait up to seconds until no director names host for path.

        Returns:
            How long that took; None if not within seconds.
        """
        started = time.monotonic()
        if common.wait_for(lambda: not self.named_by(route, path, host), seconds, interval=5):
            return time.monotonic() - started
        return None


# --------------------------------------------------------------------------
# cache-restart


def cache_restart(test: Test) -> list[Row]:
    """Check that each cache, restarted, serves what it held from its store.

    Each cache holds HELD_OBJECTS objects read whole (a V2 cache says so
    with Age). Restarted, and once every director names it again, it
    must serve each from its store, intact, with Age for a V2 cache, and
    the native origins must have sent less than an object for GETs
    meanwhile. Age is checked at a V2 cache alone; at an XRootD cache,
    the origins' counters alone tell, and over XRootD origins, which
    keep none, only the bytes are checked.
    """
    rows: list[Row] = []
    for cache in test.fed.caches:
        names = [f"held-{cache.svc}-{n}" for n in range(HELD_OBJECTS)]
        objects = {name: os.urandom(OBJECT_SIZE) for name in names}
        for name, data in objects.items():
            test.place(name, data)
        problems: list[str] = []
        for name, data in objects.items():
            why = test.fill(cache, test.rel(name), data)
            if why:
                problems.append(why)
        if problems:
            rows.append((cache.svc, FAIL, f"before the restart, {first(problems)}"))
            continue
        host = director.host(cache.url)
        federated = not cache.site_local
        path = test.path(test.rel(names[0]))
        # A director restarted just before (by state_survives(), in the
        # pass before) learns of the cache again only from its next ad.
        if federated and test.wait_named("object", path, host, NAMED_WAIT) is None:
            rows.append(
                (
                    cache.svc,
                    FAIL,
                    f"before the restart, not every director named it within {NAMED_WAIT}s",
                )
            )
            continue
        before = metrics.origins_sent_bytes(test.fed)
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
        for name, data in objects.items():
            answer = test.ask("GET", cache.url, test.rel(name))
            why = web.served(answer, data)
            if why:
                problems.append(f"after the restart, a GET of {name} got {why}")
            elif cache.kind == "v2":
                age = web.age(answer)
                if age is None:
                    problems.append(f"after the restart, {name} came with no Age, so not from its store")
                else:
                    ages.append(age)
        after = metrics.origins_sent_bytes(test.fed)
        if before is not None and after is not None and after - before >= OBJECT_SIZE:
            problems.append(
                f"the origins sent {after - before:.0f} bytes for GETs across the restart,"
                + " an object's worth or more"
            )
        note = f"named again {took:.0f}s after its restart; {len(names)} objects from its store"
        if ages:
            note += f" (Age {min(ages)}..{max(ages)})"
        if before is None:
            note += "; the origins keep no byte counters"
        rows.append((cache.svc, *outcome(problems, note)))
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
    client: issuer.Client | None = None


def local_metadata(test: Test, server: Server) -> dict[str, Any]:
    """Return what server records of itself, as its admin reads it.

    Returns:
        Its record (its ID, name, and type); {} if it will not say.
    """
    answer = test.admin(server.web_url, server.key_dir).call("GET", LOCAL_METADATA)
    return answer.object if answer.status == 200 else {}


def registered_servers(test: Test) -> list[tuple[str, str]] | None:
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


def issuer_origin(test: Test) -> tuple[common.Origin | None, str]:
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
    return accounts.login(web_url, "admin", webapi.ADMIN_PASSWORD)


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
        # A registered server has an ID; a standalone origin, which
        # registers nothing, has none to keep.
        if test.fed.registry_url and not found.metadata[server.svc].get("id"):
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
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        answer = webapi.create_token(
            web_url, f"restart-{test.run}", [SCRAPE_SCOPE], expires, login_cookie(web_url)
        )
        made = webapi.made_token(answer)
        if answer.status not in (200, 201) or made is None:
            problems.append(f"making an API token: {answer.describe()}")
        else:
            found.api_token, found.api_token_id, _ = made
        now = int(time.time() * 1000)
        found.downtime_id = str(uuid.uuid4())
        declared = {
            "class": "SCHEDULED",
            "severity": SEVERITY,
            "description": f"pelican-test-framework restart {test.run}",
            "startTime": now + 86_400_000,
            "endTime": now + 90_000_000,
        }
        answer = admin.call("POST", f"{DOWNTIME}/{found.downtime_id}", declared, accounts.csrf(admin))
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


def restart_and_settle(test: Test, svc: str, route: str, host: str) -> str | None:
    """Restart svc, and wait until every director names host for the probe.

    The directors are asked by route; with none, nothing is waited for.

    Returns:
        Why that failed, or None.
    """
    try:
        control.restart(svc)
    except control.ControlError as e:
        return str(e)
    if route and test.wait_named(route, test.path(test.rel("probe")), host, NAMED_WAIT) is None:
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
    client's secret (`issuer`), whose HMAC key the issuer derives from a
    master key in its database (oauth2/issuer/provider.go).
    """
    fed = test.fed
    test.place("probe", os.urandom(4096))
    found = Recorded()
    problems = record_state(test, found)
    rows: list[Row] = []
    if problems:
        rows.append(("setup", FAIL, first(problems)))
    origin = fed.origins[0]
    origin_host = director.host(origin.url)
    # Each service restarted, and the route by which every director must
    # then name origin-0 again: none after the registry's, which takes
    # no server out of routing.
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
                problems.append(f"{server.svc}'s {key} is {after.get(key)!r}, was {before.get(key)!r}")
    rows.append(("ids", *outcome(problems, f"{len(found.metadata)} server(s) unchanged")))
    if fed.registry_url:
        listed = registered_servers(test)
        problems = list[str]()
        if listed is None:
            problems.append("the registry would not list its servers")
        else:
            if sorted(listed) != sorted(found.registered):
                problems.append(f"the registry lists {sorted(listed)}, listed {sorted(found.registered)}")
            names = [name for _, name in listed]
            if len(set(names)) != len(names):
                problems.append(f"a server is registered twice: {names}")
        rows.append(("registrations", *outcome(problems, f"{len(listed or [])} server(s), none twice")))
    issuing, why = issuer_origin(test)
    if issuing is None:
        rows.append(("issuer", SKIP, why))
        return rows
    web_url = issuing.web_url
    admin = test.admin(web_url, f"issuer-keys/{issuing.svc}")
    try:
        problems = list[str]()
        if found.user_id and accounts.find_user(admin, f"restart-user-{test.run}") != found.user_id:
            problems.append("the user is gone, or has another ID")
        if found.group_id and accounts.find_group(admin, f"restart-group-{test.run}") != found.group_id:
            problems.append("the group is gone, or has another ID")
        rows.append(("accounts", *outcome(problems, "the user and the group are there")))
        if found.api_token:
            problems = list[str]()
            scraped = web.request("GET", f"{web_url}/metrics", token=found.api_token)
            if scraped.status != 200:
                problems.append(f"/metrics with the API token: {scraped.describe()}")
            listed_tokens = web.request(
                "GET", web_url + webapi.TOKENS, headers=webapi.cookie_header(login=login_cookie(web_url))
            )
            listed = common.as_array(listed_tokens.json())
            ids = [str((common.as_object(e) or {}).get("id")) for e in listed or []]
            if listed is None:
                problems.append(f"listing the API tokens: {listed_tokens.describe()}, not a JSON array")
            elif found.api_token_id not in ids:
                problems.append(f"the token {found.api_token_id} is not listed")
            rows.append(("api-token", *outcome(problems, "reads /metrics, and is listed")))
        if found.downtime_id:
            answer = admin.call("GET", DOWNTIME)
            ids = [str((common.as_object(d) or {}).get("id")) for d in answer.array]
            if found.downtime_id not in ids:
                rows.append(("downtime", FAIL, f"the downtime is not listed: {answer.describe()}"))
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
            admin.call("DELETE", f"{DOWNTIME}/{found.downtime_id}", headers=accounts.csrf(admin))
        if found.api_token_id:
            webapi.delete_token(web_url, found.api_token_id, login_cookie(web_url))
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
    and EXPIRY_SLACK, no director may name it, since its ad has expired;
    started again, every director must name it within NAMED_WAIT
    seconds. The note times both.
    """
    fed = test.fed
    if not fed.directors:
        return [("", SKIP, "a standalone origin has no director")]
    test.place("probe", os.urandom(4096))
    path = test.path(test.rel("probe"))
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
        # state_survives() restarts director-0 just before, which then
        # learns of each server again only from its next ad.
        if test.wait_named(route, path, host, NAMED_WAIT) is None:
            rows.append(
                (
                    svc,
                    FAIL,
                    f"before it was killed, not every director named it within {NAMED_WAIT}s",
                )
            )
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


def health_of(test: Test, svc: str, web_url: str, component: str) -> str:
    """Return what the health report of svc, at web_url, says of component.

    Returns:
        Its status, as the admin reads it; "" if nothing, as where svc does
        not answer.
    """
    answer = test.admin(web_url, f"issuer-keys/{svc}").call("GET", HEALTH)
    components = common.as_object(answer.object.get("components")) or {}
    found = common.as_object(components.get(component)) or {}
    return str(found.get("status", ""))


def health_component(test: Test, origin: common.Origin, component: str) -> str:
    """Return what origin's health report says of component (health_of())."""
    return health_of(test, origin.svc, origin.web_url, component)


def restart_backend(backend: str) -> str | None:
    """Start backend again, and return why that failed, or None."""
    try:
        control.start(backend)
    except control.ControlError as e:
        return str(e)
    return None


def revived(svc: str) -> str | None:
    """Start svc again if its container has exited, and say so.

    An origin that gives up on its storage and exits would stay down for
    the rest of the run.

    Returns:
        What happened to svc, if its container was not running, or it
        did not answer; None if it answers as before.
    """
    if control.answers(svc):
        return None
    try:
        found = control.state(svc)
        if found == "running":
            return f"{svc} is running but does not answer on its web port"
        control.start(svc)
    except control.ControlError as e:
        return f"{svc}'s container was not running: {e}"
    return f"{svc}'s container had {found or 'gone'}, so it was started again"


def backend_outage(test: Test) -> list[Row]:
    """Check that an origin answers 5xx while its backend is stopped.

    With the origins' storage service stopped: at origin-0, where it
    takes direct clients, a GET and a HEAD of an object, and a PUT of a
    new one, must each get a server error (5xx), never 404 or 405, nor
    the object's bytes (`direct`); through the first federated cache, a
    GET of an object it does not hold must get a server error too
    (`cache`); and for an `ssh` origin, its health must say its backend
    is not ok within HEALTH_WAIT seconds (`health`). Once the service is
    back, the origin must serve the object again within BACKEND_WAIT
    seconds, without a restart, as must the cache, and the health
    component must say ok again (`recovered`).
    """
    fed = test.fed
    backend = BACKENDS.get(fed.origin_variant)
    if backend is None:
        return [("", SKIP, f"a {fed.origin_variant} origin stores on its own disk")]
    origin = fed.origins[0]
    data, cold_data = os.urandom(OBJECT_SIZE), os.urandom(OBJECT_SIZE)
    test.place("outage", data)
    test.place("outage-cold", cold_data)
    rel, cold = test.rel("outage"), test.rel("outage-cold")
    cache = fed.federated_caches[0] if fed.federated_caches else None
    component = BACKEND_COMPONENTS.get(fed.origin_variant)
    rows: list[Row] = []
    try:
        control.stop(backend)
    except control.ControlError as e:
        return [("", FAIL, str(e))]
    try:
        problems: list[str] = []
        if test.direct:
            for method, target, upload in (
                ("GET", rel, None),
                ("HEAD", rel, None),
                ("PUT", test.rel("outage-put"), os.urandom(4096)),
            ):
                answer = web.request(
                    method,
                    test.url(origin.url, target),
                    token=test.token,
                    upload=upload,
                    timeout=120,
                )
                if answer.status is None:
                    problems.append(f"{method} with {backend} stopped: no answer")
                elif not 500 <= answer.status < 600:
                    problems.append(f"{method} with {backend} stopped: {answer.describe()}, not 5xx")
                elif method == "GET" and answer.body == data:
                    problems.append(f"GET with {backend} stopped: 5xx, but with the object's bytes")
            rows.append(("direct", *outcome(problems, f"GET, HEAD, and PUT: 5xx with {backend} stopped")))
        else:
            rows.append(("direct", SKIP, "/protected-a takes no direct clients"))
        if cache is not None:
            answer = test.ask("GET", cache.url, cold)
            if answer.status is None:
                rows.append(("cache", FAIL, f"through {cache.svc}: no answer"))
            elif not 500 <= answer.status < 600:
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
            gone = False

            def unwell() -> bool:
                nonlocal seen, gone
                seen = health_component(test, origin, component)
                # An origin that exits answers nothing: no use waiting.
                gone = seen == "" and not control.answers(origin.svc)
                return gone or seen not in ("", "ok")

            if not common.wait_for(unwell, HEALTH_WAIT, interval=5):
                rows.append(("health", FAIL, f"{component} stayed {seen!r} for {HEALTH_WAIT}s"))
            elif gone:
                rows.append(("health", FAIL, f"{origin.svc} stopped answering with {backend} stopped"))
            else:
                rows.append(("health", PASS, f"{component}: {seen}"))
    finally:
        started_again = restart_backend(backend)
    if started_again:
        rows.append(("recovered", FAIL, started_again))
        return rows
    # An origin that exited would stay down for the rest of the run: start
    # it again, and fail the row, which asks for a recovery without a
    # restart.
    exited = revived(origin.svc)
    if exited:
        rows.append(("recovered", FAIL, f"with {backend} stopped, {exited}"))
        return rows

    reader = origin.url if test.direct else (cache.url if cache is not None else None)

    def serves() -> bool:
        if reader is None:
            return True
        return web.served(test.ask("GET", reader, rel, timeout=60), data) is None

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
    if test.direct:
        answer = test.ask("HEAD", origin.url, rel, timeout=60)
        if answer.status != 200:
            problems.append(f"HEAD once {backend} was back: {answer.describe()}")
        if test.writes:
            answer = web.request(
                "PUT",
                test.url(origin.url, test.rel("outage-after")),
                token=test.token,
                upload=os.urandom(4096),
            )
            if not answer.ok:
                problems.append(f"PUT once {backend} was back: {answer.describe()}")
    if cache is not None:
        why = web.served(test.ask("GET", cache.url, cold), cold_data)
        if why:
            problems.append(f"through {cache.svc} once {backend} was back: {why}")
    if component and not common.wait_for(
        lambda: health_component(test, origin, component) == "ok", HEALTH_WAIT, 5
    ):
        seen = health_component(test, origin, component)
        problems.append(f"{component} is {seen!r} {HEALTH_WAIT}s after {backend} came back")
    rows.append(("recovered", *outcome(problems, f"serving again {took:.0f}s after {backend} started")))
    return rows


# --------------------------------------------------------------------------
# xrootd-wedged


def xrootd_targets(fed: common.Federation) -> list[tuple[str, str, str]]:
    """Return the servers whose XRootD xrootd-wedged stops.

    They are the first origin, where XRootD serves the origins, and the
    first federated cache that XRootD serves.

    Returns:
        Each one's service, web URL, and director route: `origin` or
        `object`, by which every director must name it again afterward.
    """
    found: list[tuple[str, str, str]] = []
    if not fed.native_origins:
        found.append((fed.origins[0].svc, fed.origins[0].web_url, "origin"))
    cache = next((c for c in fed.federated_caches if c.kind == "xrootd"), None)
    if cache is not None:
        found.append((cache.svc, cache.web_url, "object"))
    return found


def wedge(test: Test, svc: str, web_url: str, route: str) -> tuple[str, str]:
    """Run xrootd-wedged at one server; return its row's status and note."""
    problems: list[str] = []
    notes: list[str] = []
    try:
        with control.restart_with(svc, {LIVENESS_FILE: LIVENESS_CONFIG}):
            if not common.wait_for(
                lambda: health_of(test, svc, web_url, LIVENESS) == "ok", HEALTH_WAIT, interval=2
            ):
                seen = health_of(test, svc, web_url, LIVENESS)
                problems.append(f"before its XRootD stopped, {LIVENESS} was {seen!r}, not 'ok'")
            control.freeze_xrootd(svc)
            frozen = time.monotonic()
            unwell: list[str] = []

            def ended() -> bool:
                if not unwell:
                    seen = health_of(test, svc, web_url, LIVENESS)
                    if seen in ("warning", "critical"):
                        unwell.append(f"{seen} {time.monotonic() - frozen:.0f}s in")
                return control.state(svc) != "running"

            if common.wait_for(ended, WEDGED_WAIT, interval=2):
                notes.append(f"it exited {time.monotonic() - frozen:.0f}s after XRootD stopped")
                if unwell:
                    notes.append(f"{LIVENESS} was {unwell[0]}")
                else:
                    problems.append(f"its health never said {LIVENESS} was unwell")
            else:
                problems.append(f"{WEDGED_WAIT}s after its XRootD stopped, it still runs")
                control.thaw_xrootd(svc)
    except control.ControlError as e:
        problems.append(str(e))
    # restart_with() has started it again as it was, and waited until it
    # answers; the directors must route to it again, as before.
    if test.wait_named(route, test.path(test.rel("wedged")), svc, NAMED_WAIT) is None:
        problems.append(f"{NAMED_WAIT}s after it started again, not every director named it")
    return outcome(problems, "; ".join(notes))


def xrootd_wedged(test: Test) -> list[Row]:
    """Check that a server whose XRootD stops answering shuts itself down.

    Once its own XRootD has answered no liveness probe for
    Xrootd.LivenessMaxUnresponsiveTime, a server must shut XRootD down
    and end with it, so that what runs the server can start it afresh.
    At each of xrootd_targets(), restarted with the probe's timings
    shortened (LIVENESS_CONFIG), once its health says `xrootd-liveness`
    is `ok`, its XRootD daemons are stopped (SIGSTOP), which leaves
    their port taking connections, but answering nothing. Its health
    must then say `warning` or `critical`, and within WEDGED_WAIT
    seconds its container must have exited. It is then started again,
    as it was, and every director must name it again.
    """
    targets = xrootd_targets(test.fed)
    if not targets:
        return [("", SKIP, "XRootD serves neither the origins nor a federated cache")]
    test.place("wedged", os.urandom(OBJECT_SIZE))
    return [(svc, *wedge(test, svc, web_url, route)) for svc, web_url, route in targets]


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


def get_pelican(test: Test, name: str, url: str, direct: bool = False) -> Fetched:
    """Run `pelican object get` of url into a directory of its own.

    It writes transfer stats, which give the host of each attempt. With
    direct, it asks for an origin (--direct).
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
        *(["--direct"] if direct else []),
        url,
        "object",
        cwd=directory,
    )
    hosts = [
        transfers.endpoint_host(attempt)
        for result in transfers.stats_results(transfers.read_stats(stats))
        for attempt in transfers.attempts_of(result)
    ]
    return Fetched(status, out + err, directory, hosts)


def get_plugin(test: Test, name: str, url: str, direct: bool = False) -> Fetched:
    """Run stash_plugin's download of url into a directory of its own.

    The server's token is in its credentials directory. With direct, it
    asks for an origin, by the URL's query, since the plugin has no flag
    for it (transfers.DIRECT_QUERY).
    """
    directory = test.directory(name)
    if direct:
        url += transfers.DIRECT_QUERY
    status, out, err, ads = test.session.plugin_run(
        f"{test.area}/{name}", [(url, "object")], creds={"scitokens.use": test.token_file}, cwd=directory
    )
    output = err or out
    hosts: list[str] = []
    for ad in ads.splitlines():
        if "TransferSuccess = true" in ad:
            endpoints = sorted((int(n), host) for n, host in re.findall(r'Endpoint(\d+) = "([^":]*)', ad))
            hosts = [host for _, host in endpoints]
    return Fetched(status, ads or output, directory, hosts)


def failover_problems(fetch: Fetched, data: bytes, dead: str, client: str) -> list[str]:
    """Return what is wrong with a download while server dead is away.

    client's download must succeed, with the object, served by another
    server.
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


def unfetched(test: Test, name: str, data: bytes, routes: Sequence[str] = ("",)) -> str | None:
    """Return why a client can't get object name with every server up.

    None if both can. `pelican object get` and stash_plugin each get it
    by each of routes: `direct` from an origin, `cache` through a cache,
    or "", through the federation, as the failover scenarios get their
    objects. A client that can't can't with a server away either, so
    none of their rows could pass.
    """
    for route in routes:
        for client, runner in (("pelican", get_pelican), ("plugin", get_plugin)):
            label = f"{name}-{client}-{route}" if route else f"{name}-{client}"
            fetch = runner(test, label, test.pelican_url(test.rel(name)), route == "direct")
            problems = failover_problems(fetch, data, "", client)
            if problems:
                how = f"{client} ({route})" if route else client
                return f"with every server up, {how} could not get {name}: {first(problems)}"
    return None


def failover_rows(
    test: Test,
    dead: common.Cache | common.Origin,
    naming: str,
    probe: str,
    prefix: str = "",
    routes: Sequence[str] = ("",),
) -> list[Row]:
    """Return a failover scenario's rows, with server dead away.

    dead is paused, and then killed. Each time, while the directors still
    name it (`early`), and once they have dropped it (`late`), `pelican
    object get` and stash_plugin each get an object of their own by each
    of routes (as unfetched() takes them), named
    <prefix>-<mode>-<when>-<client>-<route>, less any part that is empty.
    Afterward dead is unpaused, or started (`back`), and every director
    must name it again.

    Args:
        naming: The route by which the directors name dead for probe,
            an object of the run's: `object` or `origin`.
    """
    host = director.host(dead.url)
    path = test.path(test.rel(probe))
    rows: list[Row] = []
    for mode, away, back in (
        ("paused", control.pause, control.unpause),
        ("killed", control.kill, control.start),
    ):
        names = {
            (when, client, route): "-".join(p for p in (prefix, mode, when, client, route) if p)
            for when in ("early", "late")
            for client in ("pelican", "plugin")
            for route in routes
        }
        objects = {name: os.urandom(OBJECT_SIZE) for name in names.values()}
        for name, data in objects.items():
            test.place(name, data)
        if len(test.named_by(naming, path, host)) != len(test.fed.directors):
            rows.append((mode, FAIL, f"before {dead.svc} was {mode}, not every director named it"))
            continue
        try:
            away(dead.svc)
        except control.ControlError as e:
            rows.append((mode, FAIL, str(e)))
            continue
        try:
            for when in ("early", "late"):
                if when == "late":
                    dropped = test.wait_unnamed(naming, path, host, AD_LIFETIME + EXPIRY_SLACK)
                    if dropped is None:
                        rows.append(
                            (
                                f"{mode} late",
                                FAIL,
                                f"{AD_LIFETIME + EXPIRY_SLACK}s after {dead.svc} was {mode}, a director still named it",
                            )
                        )
                        continue
                elif not test.named_by(naming, path, host):
                    rows.append(
                        (
                            f"{mode} early",
                            INCONCLUSIVE,
                            f"the directors had dropped {dead.svc} before the clients ran",
                        )
                    )
                    continue
                for route in routes:
                    problems: list[str] = []
                    tried: list[str] = []
                    for client, runner in (("pelican", get_pelican), ("plugin", get_plugin)):
                        name = names[when, client, route]
                        fetch = runner(test, name, test.pelican_url(test.rel(name)), route == "direct")
                        problems += [
                            f"{client}: {p}"
                            for p in failover_problems(fetch, objects[name], dead.svc, client)
                        ]
                        tried.append(f"{client} tried {fetch.hosts or 'nothing recorded'}")
                    case = f"{mode} {when} {route}" if route else f"{mode} {when}"
                    rows.append((case, *outcome(problems, "; ".join(tried))))
        finally:
            try:
                back(dead.svc)
                took = test.wait_named(naming, path, host, NAMED_WAIT)
                if took is None:
                    rows.append(
                        (
                            f"{mode} back",
                            FAIL,
                            f"{NAMED_WAIT}s after {dead.svc} came back, not every director named it",
                        )
                    )
                else:
                    rows.append((f"{mode} back", PASS, f"named again {took:.0f}s after it came back"))
            except control.ControlError as e:
                rows.append((f"{mode} back", FAIL, str(e)))
    return rows


def cache_failover(test: Test) -> list[Row]:
    """Check that the clients get an object while the first cache is away.

    With the first federated cache paused (its ports take connections
    that nothing answers), and then killed (they refuse), `pelican
    object get` and stash_plugin of an object through the federation
    must each succeed, served by another cache, while the directors
    still name the first (before its ad expires), and once they have
    dropped it. The note says which caches each client tried. Afterward
    the cache is unpaused, or started, and every director must name it
    again. First, with every cache up, both clients must get the probe
    (unfetched()), or the scenario fails at once.
    """
    caches = test.fed.federated_caches
    if len(caches) < 2:
        return [("", SKIP, "fewer than two federated caches ('-p topo-multi-cache')")]
    probe = os.urandom(4096)
    test.place("probe", probe)
    why = unfetched(test, "probe", probe)
    if why:
        return [("", FAIL, why)]
    return failover_rows(test, caches[0], "object", "probe")


# --------------------------------------------------------------------------
# origin-failover


def origin_failover(test: Test) -> list[Row]:
    """Check that the clients get an object while the last origin is away.

    With several origins, each holding the object: the last is paused
    (its ports take connections that nothing answers), and then killed
    (they refuse). Each time, `pelican object get` and stash_plugin must
    each get an object of their own, direct from an origin
    (`--direct`, or `?directread`), where the namespace takes direct
    clients, and through a cache (or, with none, from whichever origin
    the directors send them to), served by a server other than the one
    away. That holds while the directors still name it for an origin's
    read, as they do until its ad expires, and once they have dropped
    it. Meanwhile a director that has the namespace's token stats the
    origins before it redirects (Director.CheckOriginPresence), and a
    cache fetches with the client's token, so each must reach an origin
    that answers. The note says which servers each client tried.
    Afterward the origin is unpaused, or started, and every director
    must name it again. First, with every origin up, both clients must
    get the probe by each route (unfetched()), or the scenario fails at
    once.
    """
    fed = test.fed
    if len(fed.origins) < 2:
        return [("", SKIP, "fewer than two origins ('-p topo-multi-origin')")]
    routes = ["cache"]
    if test.direct:
        routes.insert(0, "direct")
    probe = os.urandom(4096)
    test.place("origin-probe", probe)
    why = unfetched(test, "origin-probe", probe, routes)
    if why:
        return [("", FAIL, why)]
    return failover_rows(test, fed.origins[-1], "origin", "origin-probe", "origin", routes)


# --------------------------------------------------------------------------
# peer-director


def listed_directors(url: str) -> set[str] | None:
    """Return the hosts in the director at url's list of directors.

    Returns:
        The hosts; None if it gave no list.
    """
    answer = web.request("GET", f"{url}{DIRECTORS}")
    found = common.as_array(answer.json())
    if answer.status != 200 or found is None:
        return None
    return {director.host(str((common.as_object(d) or {}).get("advertise_url", ""))) for d in found}


def peer_director(test: Test) -> list[Row]:
    """Check that a director killed drops from its peer's list, and returns.

    With several directors, the second killed: within Server.AdLifetime
    and EXPIRY_SLACK, the first must list only itself at
    /api/v1.0/director/directors, since its peer's ad has expired; once
    the second starts again, the first must list both within NAMED_WAIT
    seconds.
    """
    fed = test.fed
    if len(fed.directors) < 2:
        return [("", SKIP, "fewer than two directors ('-p topo-multi-director')")]
    first_url, peer_url = fed.directors[0], fed.directors[1]
    me, peer = director.host(first_url), director.host(peer_url)
    if listed_directors(first_url) != {me, peer}:
        return [("", FAIL, f"before the peer was killed, {me} lists {listed_directors(first_url)}")]
    try:
        control.kill(peer)
    except control.ControlError as e:
        return [("", FAIL, str(e))]
    started = time.monotonic()
    dropped = common.wait_for(lambda: listed_directors(first_url) == {me}, AD_LIFETIME + EXPIRY_SLACK, 5)
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

RUN: dict[str, Callable[[Test], list[Row]]] = {
    "cache-restart": cache_restart,
    "state-survives": state_survives,
    "ad-expiry": ad_expiry,
    "backend-outage": backend_outage,
    "xrootd-wedged": xrootd_wedged,
    "cache-failover": cache_failover,
    "origin-failover": origin_failover,
    "peer-director": peer_director,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    why = control.unavailable()
    if why:
        for name in selected:
            results.add(name, SKIP, why)
        return
    test = Test(session)
    test.clean("before the run", fatal=True)
    print(f"Restarts against {fed.url}, objects under /{NAMESPACE}/{test.base}/\n")
    try:
        for name in selected:
            rows = common.guarded(partial(RUN[name], test), lambda note: [("", FAIL, note)])
            for case, status, note in rows:
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        test.clean("after the run")
        shutil.rmtree(test.tmp, ignore_errors=True)
