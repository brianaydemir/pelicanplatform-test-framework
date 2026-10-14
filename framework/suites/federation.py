"""The federation itself: it serves, and its servers know of each other.

test.py runs `ready` and `caches` before any other suite, whatever it is
asked for, since nothing else can pass without them; and it stops if
`ready` can't pass.

The suite uploads an object straight to every origin, under
/<namespace>/data/federation/<run>/, and reads it back:

  ready      through the federation (the director and a cache), with
             `pelican object get`: 20 tries, 15 seconds apart, since
             servers just started take a while to find each other
  caches     straight from each cache, with the server's token: 20
             tries, 15 seconds apart, while it fails or does not answer,
             since a new cache refuses a namespace's tokens (403) until
             it has learned the namespace's issuers from the director
  approval   under `-p registry-approval`, where nothing serves until
             an administrator approves its registration, `ready`
             approves every pending one through the registry's admin
             API before it waits, and first asks the directors where
             they would send the object: none may name a server whose
             registration was still pending
  directors  each director must name every federated cache for it, and
             no site-local one, and every origin; and, for a client's
             direct read (?directread), every origin, or where the
             namespace takes no direct clients, no origin, answering 405;
             within 60 seconds (not in the tiny topology)
  discovery  where a Pelican server serves the federation's discovery
             document (the tiny topology's director, or a standalone
             origin), it names the federation's servers as fed.sh does,
             and its jwks_uri the server's own keys
  broker     under `-p origin-broker`, each director names origin-0's
             connection broker, for an origin, and so does its list of
             servers, which names none for a cache; within 60 seconds
  hints      each director's redirect, for a cache and for an origin, in
             each namespace, tells the client the namespace, whether it
             takes a token, and its issuer (the X-Pelican-Namespace,
             X-Pelican-Authorization, and X-Pelican-Token-Generation
             headers), by which the client picks a token
  redirects  a plain HTTP client, which follows a director's redirect
             and presents its token again where it lands, as curl does:
             in each namespace, at each director (and at the load
             balancer, under `-p director-ha`), a GET and a HEAD of
             the object's bare path must land at a cache, or at an
             origin under `-p director-default-origin`
             (Director.DefaultResponse); one with ?directread at an
             origin, where the namespace takes direct clients, and
             otherwise find none; the object and origin routes at a
             cache and an origin; and a bare-path PUT, with the
             namespace's token, at an origin, where the namespace
             takes writes, and otherwise nowhere that takes it. What
             lands at a cache must be served, and what lands at an
             origin only where the namespace takes direct clients
  presence   with several origins, an object that one of them alone
             holds: each director must send an origin's read of it,
             plain and for a client's direct read, to that origin alone,
             by asking the origins which has it
             (Director.CheckOriginPresence, on by default); and it must
             come back through each cache, and by `pelican object get`,
             through a cache and direct
  downtime   a server's admin declares it down through its web API, as
             before maintenance: within 150 seconds no director may
             name it, and once the downtime is ended, every director
             must again, as soon; the last federated cache, and with
             several origins, the last origin

Its data, under each origin's data/federation/, on a pstore origin too,
is removed after the suite.
"""

import json
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from testlib import accounts, common, credentials, director, owners, stores, transfers, web
from testlib.common import die
from testlib.credentials import ALLOW
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ready": "an object uploaded to every origin comes back through the federation",
    "caches": "each cache serves it with the server's token (a new one refuses at first)",
    "approval": (
        "under `-p registry-approval`, no director named a server before its registration"
        " was approved, which `ready` does"
    ),
    "directors": (
        "each director names every federated cache and origin, and for a direct read,"
        " every origin if the namespace takes direct clients, and otherwise none; with no"
        " cache, an origin in a cache's place"
    ),
    "discovery": "a Pelican server's discovery document names the federation's servers",
    "broker": "the directors name origin-0's connection broker, and no cache's",
    "hints": "each director's redirect names the namespace, whether it takes a token, and its issuer",
    "redirects": (
        "a plain HTTP client follows each director's redirects, of bare paths too, to a"
        " cache or an origin, and gets the object"
    ),
    "presence": "an object that one origin alone holds: the directors send reads of it there",
    "downtime": "a server its admin declares down is named by no director, until it is up again",
}

# The scenarios that run whatever test.py is asked for.
REQUIRED = ("ready", "caches")

# How long `ready` and `caches` wait: tries, and seconds between them.
TRIES, INTERVAL = 20, 15
# How long `directors` and `broker` wait, in seconds.
DIRECTORS_WAIT = 60
# How long `downtime` waits for the directors, in seconds: they learn of a
# downtime, and of its end, from the registry, which they ask every minute
# (Director.RegistryQueryInterval), and which the server tells at once.
DOWNTIME_WAIT = 150

# The registry's registrations (`registry-approval`), each a prefix with a
# status; a server's is /caches/<host> or /origins/<host>.
REGISTRATIONS = "/api/v1.0/registry_ui/namespaces"
SERVER_PREFIXES = ("/caches/", "/origins/")
# The statuses of a registration awaiting approval.
PENDING_STATUSES = ("Incomplete", "Pending")
# A server's downtimes, through its web API.
DOWNTIME = "/api/v1.0/downtime"

# The connection broker's path.
BROKER_PATH = "/api/v1.0/broker/reverse"

# What a discovery document must agree on with fed.sh's.
DISCOVERY_FIELDS = (
    "discovery_endpoint",
    "director_endpoint",
    "namespace_registration_endpoint",
    "jwks_uri",
)


def skip(_fed: common.Federation) -> Optional[str]:
    """Return None: every shape has a federation to test."""
    return None


@dataclass
class Approval:
    """What approving the registrations found (see approve_pending()).

    Attributes:
        approved: The prefixes approved.
        named_early: The servers a director named while their registration
            was pending.
        problems: What went wrong.
    """

    approved: list[str] = field(default_factory=list[str])
    named_early: list[str] = field(default_factory=list[str])
    problems: list[str] = field(default_factory=list[str])


class Probe:
    """An object at every origin, in each namespace.

    It also holds what every scenario needs to read the object.

    Attributes:
        base: The collection of the run's objects, in each namespace.
        rel: The object's path, in each namespace.
        data: Each namespace's probe, by namespace.
        namespace: The namespace that caches and directors are asked
            about: the first protected one, or /public where the origins
            export only that (`-p origin-public`).
        read_ns: The namespace that `ready` reads: /public, with no token,
            where there is one.
        uploaded: Whether upload() has put the probes in place.
        approval: What `ready` found as it approved the registrations.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.base = f"data/federation/{session.run}"
        self.rel = f"{self.base}/probe"
        # On a pstore origin, /public's is /protected-a's, whose storage
        # it reads.
        own = {ns: os.urandom(4096) for ns in session.namespaces}
        self.data = {ns: own[self.fed.storage_namespace(ns)] for ns in own}
        self.namespace = credentials.probe_namespace(self.fed)
        self.read_ns = session.namespaces[0]
        self.uploaded = False
        self.approval = Approval()

    def admin(self, web_url: str, key_dir: str) -> accounts.Api:
        """Return the web API of the server at web_url, as its admin.

        The admin's token (see credentials.mint_admin()) is signed with the
        server's key in key_dir.
        """
        host = director.host(web_url)
        token = credentials.mint_admin(
            self.fed,
            self.session.path(f"tokens/admin.{host}"),
            credentials.local_issuer(self.fed, web_url),
            common.signing_key(key_dir),
        )
        return accounts.Api(web_url, token)

    def upload(self) -> Optional[str]:
        """Put each namespace's probe at every origin, once.

        See stores.seed() for how.

        Returns:
            Why that failed, or None if nothing did.
        """
        if self.uploaded:
            return None
        for origin in stores.unique(self.fed.origins):
            for ns in stores.storage_namespaces(origin, self.fed.exports):
                token = self.session.token(ns)
                errors = stores.make_dirs([origin], ns, self.base, token)
                if errors:
                    return errors[0]
                error = stores.seed(
                    origin, ns, self.rel, self.data[ns], token, self.fed.exports[ns]
                )
                if error:
                    return error
        self.uploaded = True
        return None

    def get(self) -> Optional[str]:
        """Get the object through the federation with `pelican object get`.

        Returns:
            What went wrong, or None if nothing did.
        """
        target = self.session.path("federation-probe")
        if os.path.exists(target):
            os.remove(target)
        token = (
            []
            if self.read_ns == "public"
            else ["--token", self.session.token_file(self.read_ns)]
        )
        code, out, err = self.session.pelican_cmd(
            "object", "get", *token, f"{self.fed.url}/{self.read_ns}/{self.rel}", target
        )
        if code != 0:
            return f"`pelican object get`: exit {code}: {last_line(err or out)}"
        if common.sha256(target) != common.digest(self.data[self.read_ns]):
            return "`pelican object get` succeeded, but the file is not the object"
        return None


def pending_registrations(api: accounts.Api) -> list[tuple[str, str]]:
    """Return each pending registration's ID and prefix.

    Pending is each awaiting approval, whether Incomplete, as a server's
    own registration starts (Pelican's registry/registry.go, since
    v26.0.0-rc.1), or Pending, as one whose form has been filled in does.

    Raises:
        accounts.ApiError: The registry did not list them.
    """
    found: list[tuple[str, str]] = []
    for status in PENDING_STATUSES:
        listed = accounts.must(
            api.call("GET", f"{REGISTRATIONS}?status={status}"),
            f"listing {status} registrations",
        )
        for row in listed.array:
            entry = common.as_object(row) or {}
            found.append((str(entry.get("id", "")), str(entry.get("prefix", ""))))
    return found


def approve_pending(probe: Probe) -> None:
    """Approve every pending registration, as the registry's admin.

    Record in probe.approval what was approved, what went wrong, and,
    from before approving, which of the pending servers a director already
    names for the object, which none should, since the directors refuse
    the advertisements of a server whose registration is pending.
    """
    fed, found = probe.fed, probe.approval
    try:
        host = director.host(fed.registry_url)
        api = probe.admin(fed.registry_url, f"issuer-keys/{host}")
        pending = pending_registrations(api)
        if not pending:
            return
        servers = sorted(
            prefix[len(start) :]
            for _, prefix in pending
            for start in SERVER_PREFIXES
            if prefix.startswith(start)
        )
        path = f"/{probe.namespace}/{probe.rel}"
        for url in fed.directors:
            for route in ("object", "origin"):
                answer = director.ask(url, route, path)
                found.named_early += [
                    f"{director.host(url)} named {name}, whose registration was pending"
                    for name in servers
                    if answer.names(name)
                ]
        headers = accounts.csrf(api)
        for reg_id, prefix in pending:
            accounts.must(
                api.call("PATCH", f"{REGISTRATIONS}/{reg_id}/approve", headers=headers),
                f"approving {prefix}",
            )
            found.approved.append(prefix)
    except accounts.ApiError as e:
        found.problems.append(str(e))


def ready(probe: Probe) -> tuple[str, str]:
    """Check that the object comes back through the federation.

    Where the registry requires approval, each try first approves what is
    pending, since a server registers once it is up.

    Raises:
        Died: The federation never serves: nothing after it could pass.
    """
    why: Optional[str] = None
    for n in range(1, TRIES + 1):
        if probe.fed.registry_approval:
            approve_pending(probe)
        why = probe.upload() or probe.get()
        if why is None:
            return PASS, f"{probe.read_ns}, after {n} tries" if n > 1 else probe.read_ns
        if n == 1:
            print(
                f"Waiting for the federation to serve {probe.fed.url}/{probe.read_ns}/"
                + f"{probe.rel} ..."
            )
        if n < TRIES:
            time.sleep(INTERVAL)
    die(
        f"gave up waiting for the federation after {TRIES} tries, {INTERVAL}s apart ({why})."
        + " Is it up (./fed.sh status)? Was it initialized (./fed.sh init) for this shape?"
    )


def caches(probe: Probe) -> tuple[str, str]:
    """Check that each cache serves the object with the server's token."""
    if not probe.fed.caches:
        return SKIP, "this shape has no cache"
    token = probe.session.token(probe.namespace)
    problems: list[str] = []
    for cache in probe.fed.caches:
        url = f"{cache.url}/{probe.namespace}/{probe.rel}"
        answer = web.request("GET", url, token=token)
        # A new cache refuses the token (403) until it has learned the
        # namespace's issuers, and may fail otherwise, or not answer,
        # while it finds the origins.
        for n in range(1, TRIES):
            if answer.ok:
                break
            if n == 1:
                print(f"Waiting for {cache.svc} to serve the object ({answer.describe()}) ...")
            time.sleep(INTERVAL)
            answer = web.request("GET", url, token=token)
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()}")
        elif answer.body != probe.data[probe.namespace]:
            problems.append(f"{cache.svc}: the body is not the object")
    if problems:
        return FAIL, common.first(problems)
    return PASS, f"{len(probe.fed.caches)} cache(s)"


def approval(probe: Probe) -> tuple[str, str]:
    """Check that no director named a server whose registration was pending.

    `ready` found which did as it approved the registrations (see
    approve_pending()).
    """
    found = probe.approval
    if not probe.fed.registry_approval:
        return SKIP, "the registry requires no approval ('-p registry-approval')"
    if found.problems:
        return FAIL, common.first(found.problems)
    if found.named_early:
        return FAIL, common.first(found.named_early)
    if not found.approved:
        return PASS, "nothing was pending: an earlier run approved every registration"
    return PASS, f"approved {len(found.approved)}: {', '.join(found.approved)}"


def directors(probe: Probe) -> tuple[str, str]:
    """Check where each director would send a GET of the object.

    Its lists for a cache (`object`) and for an origin must name every one.
    The `origin` route is also how caches reach origins, so without
    ?directread it names origins whether or not they take direct clients.
    Only with ?directread, a client's direct read, does the director check
    DirectReads (originSupportsQuery() in Pelican's director/sort.go):
    then it must name every origin where the namespace takes direct
    clients, and elsewhere find none (405), and name none. Where the
    federation has no cache (`-p topo-no-cache`), the `object` route must
    fall back to an origin, where the namespace takes direct clients
    (redirectToCache in director/director.go), and name only origins; and
    elsewhere find none (404).
    """
    fed = probe.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no director"
    if fed.tiny:
        return SKIP, "the tiny topology's director, cache, and origin are one server"
    path = f"/{probe.namespace}/{probe.rel}"
    origin_hosts = [director.host(o.url) for o in fed.origins]
    site_local = [director.host(c.url) for c in fed.caches if c.site_local]
    direct = "DirectReads" in fed.exports[probe.namespace]
    cacheless = not fed.federated_caches
    # Each ask: its route, its query, and whom it must name.
    asks = [
        ("object", "", [director.host(c.url) for c in fed.federated_caches]),
        ("origin", "", origin_hosts),
        ("origin", "directread", origin_hosts if direct else []),
    ]

    def unnamed() -> list[str]:
        found: list[str] = []
        for url in fed.directors:
            for route, query, hosts in asks:
                answer = director.ask(url, route, path, query=query)
                what = f"{director.host(url)} ({route}{'?' + query if query else ''})"
                found += [
                    f"{what} did not name {host}: {answer.describe()}"
                    for host in hosts
                    if not answer.names(host)
                ]
                found += [
                    f"{what} named site-local {host}: {answer.describe()}"
                    for host in site_local
                    if answer.names(host)
                ]
                if query and not direct:
                    named = [h for h in origin_hosts if answer.names(h)]
                    if answer.status != 405 or named:
                        found.append(f"{what} should find no origin (405): {answer.describe()}")
                if route == "object" and cacheless:
                    named = [h for h in origin_hosts if answer.names(h)]
                    if direct and (answer.status != 307 or not named):
                        found.append(
                            f"{what} should fall back to an origin (307): {answer.describe()}"
                        )
                    elif not direct and (answer.status != 404 or named):
                        found.append(f"{what} should find no cache (404): {answer.describe()}")
        return found

    missing: list[str] = []

    def all_named() -> bool:
        missing[:] = unnamed()
        return not missing

    if not common.wait_for(all_named, DIRECTORS_WAIT, interval=5):
        return FAIL, f"after {DIRECTORS_WAIT}s, {common.first(missing)}"
    direct_reads = (
        f"{len(fed.origins)} for direct reads"
        if direct
        else "none for direct reads (no direct clients)"
    )
    caches_named = f"{len(fed.federated_caches)} cache(s)"
    if cacheless:
        caches_named = "no cache: an origin for a cache" if direct else "no cache (404)"
    return PASS, (
        f"{len(fed.directors)} director(s): {caches_named},"
        f" {len(fed.origins)} origin(s), {direct_reads}"
    )


def discovery(probe: Probe) -> tuple[str, str]:
    """Check the discovery document, where a Pelican server serves it.

    One does where it is the discovery host: director-0 under
    `-p director-discovery` (Director.EnableFederationMetadataHosting), or
    the directors' load balancer under `-p director-ha` too, the tiny
    topology's one server, or a standalone origin. Each of
    DISCOVERY_FIELDS must be as fed.sh has it (an empty one may be
    missing), and the jwks_uri serve the server's own keys, by which the
    federation's tokens are checked. Elsewhere, the discovery host serves
    fed.sh's own document.
    """
    fed = probe.fed
    host = director.host(str(fed.discovery["discovery_endpoint"]))
    if host == "discovery":
        return SKIP, "the discovery host serves fed.sh's own document"
    # The load balancer's keys (`-p director-ha`) are director-0's.
    key_dir = credentials.key_dir_of(fed, str(fed.discovery["discovery_endpoint"]))
    url = f"{fed.discovery['discovery_endpoint']}/.well-known/pelican-configuration"
    answer = web.request("GET", url)
    if answer.status != 200:
        return FAIL, f"{url}: {answer.describe()}"
    try:
        served = common.as_object(json.loads(answer.body)) or {}
    except ValueError:
        return FAIL, f"{url}: not JSON"
    wrong = [
        f"{name} is '{served.get(name) or ''}', not '{fed.discovery[name]}'"
        for name in DISCOVERY_FIELDS
        if (served.get(name) or "") != fed.discovery[name]
    ]
    if wrong:
        return FAIL, f"{url}: {common.first(wrong)}"
    jwks_uri = str(served["jwks_uri"])
    answer = web.request("GET", jwks_uri)
    try:
        kids = credentials.key_ids(answer.body) if answer.status == 200 else None
    except ValueError:
        kids = None
    if kids is None:
        return FAIL, f"{jwks_uri}: {answer.describe()}, not a JWKS"
    own = set(owners.key_ids(key_dir))
    if not own & kids:
        return FAIL, f"{jwks_uri} holds none of framework/var/{key_dir}'s keys"
    return PASS, url


def broker_problems(fed: common.Federation, path: str, namespace: str) -> list[str]:
    """Return what is wrong with what each director says of the broker.

    That is the connection broker that it names for an origin's read of
    path, in namespace, the one the brokered origin exports, in the
    X-Pelican-Broker header; and in its list of servers, each one's broker
    URL.
    """
    want = urlsplit(str(fed.discovery["broker_endpoint"]))
    origin = director.host(fed.origins[0].url)
    problems: list[str] = []

    def wrong(advertised: str) -> Optional[str]:
        got = urlsplit(advertised)
        query = parse_qs(got.query)
        if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, BROKER_PATH):
            return f"'{advertised}' is not the broker at {want.netloc}{BROKER_PATH}"
        if query.get("origin") != [origin] or query.get("prefix") != [f"/{namespace}"]:
            return f"'{advertised}' is not for {origin}'s /{namespace}"
        return None

    for url in fed.directors:
        name = director.host(url)
        answer = director.ask(url, "origin", path)
        why = wrong(answer.broker)
        if why:
            problems.append(f"{name}'s X-Pelican-Broker: {why}")
        listed = web.request("GET", f"{url}/api/v1.0/director_ui/servers")
        try:
            servers = common.as_array(json.loads(listed.body)) if listed.status == 200 else None
        except ValueError:
            servers = None
        if servers is None:
            problems.append(f"{name}'s list of servers: {listed.describe()}, not JSON")
            continue
        for server in (common.as_object(s) or {} for s in servers):
            advertised = str(server.get("brokerUrl") or "")
            where = f"{name} lists {server.get('type')} {server.get('name')} with broker"
            if server.get("type") == "Origin":
                why = wrong(advertised)
                if why:
                    problems.append(f"{where} {why}")
            elif advertised:
                problems.append(f"{where} '{advertised}'")
    return problems


def broker(probe: Probe) -> tuple[str, str]:
    """Check that each director names origin-0's connection broker.

    This holds under `-p origin-broker`, within DIRECTORS_WAIT seconds (see
    broker_problems()).
    """
    fed = probe.fed
    if not fed.broker:
        return SKIP, "the origins use no connection broker ('-p origin-broker')"
    path = f"/{probe.namespace}/{probe.rel}"
    found: list[str] = []

    def named() -> bool:
        found[:] = broker_problems(fed, path, probe.namespace)
        return not found

    if not common.wait_for(named, DIRECTORS_WAIT, interval=5):
        return FAIL, f"after {DIRECTORS_WAIT}s, {common.first(found)}"
    return PASS, f"{len(fed.directors)} director(s)"


def hints(probe: Probe) -> tuple[str, str]:
    """Check the token hints in each director's redirect of the object.

    Each director's redirect of a GET of the object, for a cache
    (`object`) and for an origin, in each exported namespace, must say
    which namespace it is and whether it takes a token
    (X-Pelican-Namespace), and name its issuer, where it has one, and no
    other (X-Pelican-Authorization; X-Pelican-Token-Generation, if sent,
    names it too): the client chooses its token by these (see
    director.hints()).
    """
    fed = probe.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no director"
    problems: list[str] = []
    for url in fed.directors:
        for ns in probe.session.namespaces:
            caps = fed.exports[ns]
            issuer = fed.issuer_of(ns) if credentials.has_issuer(caps) else ""
            for route in ("object", "origin"):
                answer = web.request("GET", f"{url}/api/v1.0/director/{route}/{ns}/{probe.rel}")
                what = f"{director.host(url)} ({route}, /{ns})"
                if answer.status != 307:
                    problems.append(f"{what}: {answer.describe()}, not 307")
                    continue
                found = director.hints(answer)
                if found.namespace != f"/{ns}" or found.require_token is None:
                    sent = answer.header("X-Pelican-Namespace")
                    problems.append(f"{what}: X-Pelican-Namespace '{sent}' does not name /{ns}")
                elif found.require_token != ("PublicReads" not in caps):
                    problems.append(
                        f"{what}: X-Pelican-Namespace says require-token={found.require_token}"
                    )
                if issuer and found.issuers != (issuer,):
                    problems.append(
                        f"{what}: X-Pelican-Authorization names {list(found.issuers)}, not {issuer}"
                    )
                elif not issuer and found.issuers:
                    problems.append(
                        f"{what}: X-Pelican-Authorization names {list(found.issuers)},"
                        + f" though /{ns} has no issuer"
                    )
                if found.generation_issuer and found.generation_issuer != issuer:
                    problems.append(
                        f"{what}: X-Pelican-Token-Generation names {found.generation_issuer}"
                    )
    if problems:
        return FAIL, common.first(problems)
    return (
        PASS,
        f"{len(fed.directors)} director(s), {len(probe.session.namespaces)} namespace(s)",
    )


def follow(
    answer: web.Response, method: str, token: Optional[str], upload: Optional[bytes] = None
) -> web.Response:
    """Return the answer to method at answer's Location.

    Present token again, as a client that trusts the redirect does.
    """
    return web.request(method, answer.header("Location"), token=token, upload=upload)


def landed(answer: web.Response, servers: Sequence[tuple[str, str]]) -> Optional[str]:
    """Return which of servers a redirect sends the client to.

    Args:
        answer: The redirect, which says where by its Location.
        servers: Each server's name and data URL.

    Returns:
        The server's name, or None if it is none of them.
    """
    location = answer.header("Location")
    return next((name for name, url in servers if location.startswith(url + "/")), None)


def redirect_problems(probe: Probe, director_url: str, namespace: str) -> list[str]:
    """Return what is wrong with where a director sends a plain HTTP client.

    The director is at director_url, and the client's requests are for the
    object in namespace; what the client gets there is checked too (see
    redirects()).
    """
    fed = probe.fed
    caps = fed.exports[namespace]
    token = None if namespace == "public" else probe.session.token(namespace)
    path = f"/{namespace}/{probe.rel}"
    data = probe.data[namespace]
    name = director.host(director_url)
    at_caches = [(c.svc, c.url) for c in fed.federated_caches]
    at_origins = [(o.svc, o.url) for o in fed.origins]
    # With no cache, a read meant for one falls back to an origin, where
    # the namespace takes direct clients, and otherwise finds none (404).
    cacheless = not at_caches
    if cacheless:
        at_caches = at_origins
    for_cache = fed.director_default_response == "cache"
    bare = at_caches if for_cache else at_origins
    # Each read: its method and path, the servers it must land at, whether
    # it asks for a direct read, which the director refuses (405) where
    # the namespace takes no direct clients, and whether it is meant for
    # a cache.
    reads = [
        ("GET", path, bare, False, for_cache),
        ("HEAD", path, bare, False, for_cache),
        ("GET", f"{path}{transfers.DIRECT_QUERY}", at_origins, True, False),
        ("GET", f"/api/v1.0/director/object{path}", at_caches, False, True),
        ("GET", f"/api/v1.0/director/origin{path}", at_origins, False, False),
    ]
    problems: list[str] = []
    for method, target, where, asks_direct, meant_for_cache in reads:
        answer = web.request(method, f"{director_url}{target}", token=token)
        what = f"{name}: {method} {target}"
        # What lands at an origin is served only to direct clients.
        allowed = credentials.decide(ALLOW, caps, "get", direct=where is at_origins) == ALLOW
        if asks_direct and not allowed:
            if answer.status == 307:
                problems.append(
                    f"{what}: redirected, though /{namespace} takes no direct clients"
                )
            continue
        if cacheless and meant_for_cache and not allowed:
            if answer.status != 404:
                problems.append(
                    f"{what}: {answer.describe()}, not 404: no cache, and /{namespace}"
                    + " takes no direct clients"
                )
            continue
        if answer.status != 307:
            problems.append(f"{what}: {answer.describe()}, not 307")
            continue
        at = landed(answer, where)
        if at is None:
            kind = "an origin" if where is at_origins else "a cache"
            problems.append(f"{what}: redirected to {answer.header('Location')}, not {kind}")
            continue
        served = follow(answer, method, token)
        length = served.header("Content-Length")
        if not allowed:
            if served.status not in (401, 403):
                problems.append(f"{what} -> {at}: {served.describe()}, not a refusal")
            elif served.body == data:
                problems.append(f"{what} -> {at}: refused, but the body is the object")
        elif served.status != 200:
            problems.append(f"{what} -> {at}: {served.describe()}, not 200")
        elif method == "GET" and served.body != data:
            problems.append(f"{what} -> {at}: the body is not the object")
        elif method == "HEAD" and length != str(len(data)):
            problems.append(f"{what} -> {at}: Content-Length '{length}', not {len(data)}")

    # A bare-path PUT goes to an origin, where the namespace takes writes,
    # with the namespace's token: a write takes one even where reads do
    # not (`-p origin-public-writes`).
    rel = f"{probe.rel}-put-{name}"
    payload = os.urandom(4096)
    writes = "Writes" in caps
    put_token = probe.session.token(namespace) if writes else token
    answer = web.request(
        "PUT", f"{director_url}/{namespace}/{rel}", token=put_token, upload=payload
    )
    what = f"{name}: PUT /{namespace}/{rel}"
    if writes and answer.status != 307:
        problems.append(f"{what}: {answer.describe()}, not 307")
    elif writes and landed(answer, at_origins) is None:
        problems.append(f"{what}: redirected to {answer.header('Location')}, not an origin")
    elif answer.status == 307:
        served = follow(answer, "PUT", put_token, payload)
        if writes and not served.ok:
            problems.append(f"{what} -> {landed(answer, at_origins)}: {served.describe()}")
        elif not writes and served.ok:
            problems.append(f"{what}: succeeded, though /{namespace} takes no writes")
    elif answer.status is None or not 400 <= answer.status < 500:
        problems.append(f"{what}: {answer.describe()}, not a refusal")
    held = stores.held_anywhere(fed.origins, namespace, rel, probe.session.tokens())
    if writes and common.digest(payload) not in held:
        problems.append(f"{what}: no origin holds what was put")
    elif not writes and any(held):
        problems.append(
            f"{what}: an origin holds what was put, though /{namespace} takes no writes"
        )
    return problems


def client_directors(fed: common.Federation) -> list[str]:
    """Return the directors a plain HTTP client may ask.

    That is each by its own name, and, under `-p director-ha`, the load
    balancer's, which the federation names as its director, and so which
    curl is given.
    """
    found = list(fed.directors)
    if fed.director_ha and fed.director_url not in found:
        found.append(fed.director_url)
    return found


def redirects(probe: Probe) -> tuple[str, str]:
    """Check each director's redirects, as a plain HTTP client follows them.

    It checks them in each exported namespace (see redirect_problems());
    under `-p director-ha`, the load balancer's too.
    """
    fed = probe.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no director"
    problems: list[str] = []
    asked = client_directors(fed)
    for url in asked:
        for namespace in probe.session.namespaces:
            problems += redirect_problems(probe, url, namespace)
    if problems:
        return FAIL, common.first(problems)
    where = "a cache"
    if fed.director_default_response == "origin":
        where = "an origin"
    elif not fed.federated_caches:
        where = "an origin, there being no cache"
    balanced = " (the load balancer among them)" if len(asked) > len(fed.directors) else ""
    return PASS, (
        f"{len(asked)} director(s){balanced}, {len(probe.session.namespaces)}"
        f" namespace(s): bare paths to {where}"
    )


def presence_problems(
    probe: Probe, holder: common.Origin, namespace: str, rel: str, data: bytes
) -> list[str]:
    """Return what is wrong with how the federation serves rel in namespace.

    rel holds data, and holder alone holds it. Each director's answer for
    an origin's read, plain and for a client's direct read (?directread),
    must name holder and no other origin, or, where the namespace takes no
    direct clients, find none (405) for the direct read; and each cache
    must serve the object. The director asks the origins which has it only
    when it has the namespace's token, so each ask presents it.
    """
    fed = probe.fed
    token = None if namespace == "public" else probe.session.token(namespace)
    direct = "DirectReads" in fed.exports[namespace]
    others = [director.host(o.url) for o in fed.origins if o.svc != holder.svc]
    path = f"/{namespace}/{rel}"
    problems: list[str] = []
    for url in fed.directors:
        for query in ("", "directread"):
            answer = director.ask(url, "origin", path, token=token, query=query)
            what = f"{director.host(url)} (origin{'?' + query if query else ''}, /{namespace})"
            named = [h for h in others if answer.names(h)]
            if query and not direct:
                if answer.status != 405 or named or answer.names(director.host(holder.url)):
                    problems.append(f"{what} should find no origin (405): {answer.describe()}")
            elif not answer.names(director.host(holder.url)):
                problems.append(f"{what} did not name {holder.svc}: {answer.describe()}")
            elif named:
                problems.append(f"{what} named {named}, which lack the object")
    for cache in fed.caches:
        reply = web.request("GET", f"{cache.url}{path}", token=token)
        if reply.status != 200:
            problems.append(f"{cache.svc}: GET {path}: {reply.describe()}")
        elif reply.body != data:
            problems.append(f"{cache.svc}: GET {path}: the body is not the object")
    return problems


def presence(probe: Probe) -> tuple[str, str]:
    """Check that reads of an object that one origin alone holds go there.

    The object is put in one origin's store alone, in each exported
    namespace: the directors must route it there (see
    presence_problems()), within DIRECTORS_WAIT seconds, and `pelican
    object get` fetch it through a cache, and direct where the namespace
    allows it.
    """
    fed = probe.fed
    origins = stores.unique(fed.origins)
    if len(origins) < 2:
        return SKIP, "one origin, or origins sharing a store: no object is one origin's alone"
    holder = origins[-1]
    namespaces = [ns for ns in probe.session.namespaces if holder.storage_namespace(ns) == ns]
    objects = {ns: os.urandom(8192) for ns in namespaces}
    for ns, data in objects.items():
        rel = f"{probe.base}/only-{holder.svc}"
        token, caps = probe.session.token(ns), fed.exports[ns]
        errors = stores.make_dirs([holder], ns, probe.base, token)
        error = errors[0] if errors else stores.seed(holder, ns, rel, data, token, caps)
        if error:
            return FAIL, f"could not put the object at {holder.svc} alone: {error}"
    rel = f"{probe.base}/only-{holder.svc}"
    found: list[str] = []

    def routed() -> bool:
        found[:] = [
            p
            for ns, data in objects.items()
            for p in presence_problems(probe, holder, ns, rel, data)
        ]
        return not found

    if not common.wait_for(routed, DIRECTORS_WAIT, interval=5):
        return FAIL, f"after {DIRECTORS_WAIT}s, {common.first(found)}"
    for ns, data in objects.items():
        token_args = [] if ns == "public" else ["--token", probe.session.token_file(ns)]
        routes: list[tuple[str, list[str]]] = [("through a cache", [])]
        if "DirectReads" in fed.exports[ns]:
            routes.append(("direct", ["--direct"]))
        for how, flags in routes:
            target = probe.session.path(f"federation-presence-{ns}")
            if os.path.exists(target):
                os.remove(target)
            code, out, err = probe.session.pelican_cmd(
                "object", "get", *token_args, *flags, f"{fed.url}/{ns}/{rel}", target
            )
            if code != 0:
                return (
                    FAIL,
                    f"`pelican object get` {how} of /{ns}/{rel}: {last_line(err or out)}",
                )
            if common.sha256(target) != common.digest(data):
                return FAIL, f"`pelican object get` {how} of /{ns}/{rel}: not the object"
    return PASS, (
        f"{holder.svc}'s alone, in {', '.join('/' + ns for ns in namespaces)}:"
        f" {len(fed.directors)} director(s), {len(fed.caches)} cache(s), both clients' routes"
    )


def routed_to(fed: common.Federation, route: str, path: str, host: str) -> list[str]:
    """Return the directors that send a GET of path, for route, to host."""
    return director.naming(fed.directors, route, path, host)


def down(probe: Probe, svc: str, web_url: str, route: str) -> Optional[str]:
    """Declare server svc down, then up, and check what the directors name.

    As its admin, through its web API at web_url, declare it down for the
    next hour: within DOWNTIME_WAIT seconds, no director may name it for
    route. Then end the downtime, after which, as soon, every director
    must name it again.

    Returns:
        What went wrong, or None if nothing did.
    """
    fed = probe.fed
    path = f"/{probe.namespace}/{probe.rel}"
    host = director.host(web_url)
    before = routed_to(fed, route, path, host)
    if len(before) != len(fed.directors):
        return f"before its downtime, only {before} named it"
    api = probe.admin(web_url, f"issuer-keys/{svc}")
    headers = accounts.csrf(api)
    # A downtime's times are in milliseconds since the epoch: a server
    # refuses seconds (isValidTimeRange in web_ui/server_api.go).
    now = int(time.time() * 1000)
    declared = {
        "class": "SCHEDULED",
        "severity": "Outage (completely inaccessible)",
        "description": "pelican-test-framework",
        "startTime": now - 60_000,
        "endTime": now + 3_600_000,
    }
    downtime_id = str(uuid.uuid4())
    answer = api.call("POST", f"{DOWNTIME}/{downtime_id}", declared, headers)
    if not answer.ok:
        return f"declaring the downtime: {answer.describe()}"
    why = None
    if not common.wait_for(lambda: not routed_to(fed, route, path, host), DOWNTIME_WAIT, 5):
        why = f"{routed_to(fed, route, path, host)} still named it {DOWNTIME_WAIT}s into its downtime"
    ended = api.call("DELETE", f"{DOWNTIME}/{downtime_id}", headers=headers)
    if why:
        return why
    if not ended.ok:
        return f"ending the downtime: {ended.describe()}"
    if not common.wait_for(
        lambda: len(routed_to(fed, route, path, host)) == len(fed.directors), DOWNTIME_WAIT, 5
    ):
        return (
            f"{DOWNTIME_WAIT}s after its downtime ended, only {routed_to(fed, route, path, host)}"
            " named it"
        )
    return None


def downtime(probe: Probe) -> tuple[str, str]:
    """Check that no director names a server declared down, until it is up.

    The servers are the last federated cache, and with several origins,
    the last origin (see down()).
    """
    fed = probe.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no director"
    if fed.tiny:
        return SKIP, "the tiny topology's servers and director are one process"
    targets: list[tuple[str, str, str]] = []
    if fed.federated_caches:
        cache = fed.federated_caches[-1]
        targets.append((cache.svc, cache.web_url, "object"))
    if len(fed.origins) > 1:
        targets.append((fed.origins[-1].svc, fed.origins[-1].web_url, "origin"))
    if not targets:
        return SKIP, "no federated cache, and one origin, which nothing else could serve for"
    for svc, web_url, route in targets:
        why = down(probe, svc, web_url, route)
        if why:
            return FAIL, f"{svc}: {why}"
    return PASS, (
        f"{', '.join(svc for svc, _, _ in targets)}: dropped by {len(fed.directors)}"
        " director(s), then named again"
    )


# Each scenario, by name: it takes the Probe, and returns its result and
# a note.
RUN = {
    "ready": ready,
    "caches": caches,
    "approval": approval,
    "directors": directors,
    "discovery": discovery,
    "broker": broker,
    "hints": hints,
    "redirects": redirects,
    "presence": presence,
    "downtime": downtime,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the object in place, run the scenarios, and remove it."""
    fed = session.fed
    disk = [o for o in stores.unique(fed.origins) if not o.pstore]
    for origin in disk:
        for ns in fed.exports:
            directory = f"{origin.store_of(ns)}/data/federation"
            if not os.path.isdir(directory):
                die(f"framework/var/{directory} is missing; run ./fed.sh init")
    # Earlier runs' objects come out first: the disk stores' only, since a
    # pstore origin may not serve yet (that is what `ready` waits for).
    errors = stores.empty_tree_everywhere(disk, fed.exports, "data/federation", {})
    if errors:
        die(f"could not remove earlier runs' objects: {common.first(errors)}")
    probe = Probe(session)
    print(f"Probing {fed.url} with /<namespace>/{probe.rel}\n")
    try:
        for name in selected:
            results.add(name, *RUN[name](probe))
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach; a
        # pstore origin's only it can remove.
        tokens = session.tokens() if any(o.pstore for o in fed.origins) else {}
        errors = stores.empty_tree_everywhere(
            fed.origins, fed.exports, "data/federation", tokens
        )
        if errors:
            common.warn(f"could not remove the run's objects: {common.first(errors)}")
