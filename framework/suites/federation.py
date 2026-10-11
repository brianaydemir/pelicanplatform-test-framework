"""The federation itself: that it serves at all, and that its servers
know of each other. test.py runs `ready` and `caches` before any other
suite, whatever it is asked for, since nothing else can pass without
them; and it stops if `ready` can't pass.

The suite uploads an object straight to every origin, under
/<namespace>/data/federation/<run>/, and reads it back:

  ready      through the federation (the director and a cache), with
             `pelican object get`: 20 tries, 15 seconds apart, since
             servers just started take a while to find each other
  caches     straight from each cache, with the server's token: 20
             tries, 15 seconds apart, while it fails or does not answer,
             since a new cache refuses a namespace's tokens (403) until
             it has learned the namespace's issuers from the director
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
  presence   with several origins, an object that one of them alone
             holds: each director must send an origin's read of it,
             plain and for a client's direct read, to that origin alone,
             by asking the origins which has it
             (Director.CheckOriginPresence, on by default); and it must
             come back through each cache, and by `pelican object get`,
             through a cache and direct

Its data, under each origin's data/federation/, on a pstore origin too,
is removed after the suite.
"""

import json
import os
import time
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from testlib import common, credentials, director, owners, stores, web
from testlib.common import die
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ready": "an object uploaded to every origin comes back through the federation",
    "caches": "each cache serves it with the server's token (a new one refuses at first)",
    "directors": (
        "each director names every federated cache and origin, and for a direct read,"
        " every origin if the namespace takes direct clients, and otherwise none"
    ),
    "discovery": "a Pelican server's discovery document names the federation's servers",
    "broker": "the directors name origin-0's connection broker, and no cache's",
    "hints": "each director's redirect names the namespace, whether it takes a token, and its issuer",
    "presence": "an object that one origin alone holds: the directors send reads of it there",
}

# The scenarios that run whatever test.py is asked for.
REQUIRED = ("ready", "caches")

# How long `ready` and `caches` wait: tries, and seconds between them.
TRIES, INTERVAL = 20, 15
# How long `directors` and `broker` wait, in seconds.
DIRECTORS_WAIT = 60

# The connection broker's path, and the one namespace a brokered origin
# exports (see presets/origin-broker.sh).
BROKER_PATH = "/api/v1.0/broker/reverse"
PROTECTED_A = "protected-a"

# What a discovery document must agree on with fed.sh's.
DISCOVERY_FIELDS = (
    "discovery_endpoint",
    "director_endpoint",
    "namespace_registration_endpoint",
    "jwks_uri",
)


def skip(_fed: common.Federation) -> Optional[str]:
    """Never: every shape has a federation to test."""
    return None


class Probe:
    """An object at every origin, in each namespace, and what every
    scenario needs to read it."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.base = f"data/federation/{session.run}"
        self.rel = f"{self.base}/probe"
        # Each namespace's probe, by namespace: on a pstore origin,
        # /public's is /protected-a's, whose storage it reads.
        own = {ns: os.urandom(4096) for ns in session.namespaces}
        self.data = {ns: own[self.fed.storage_namespace(ns)] for ns in own}
        # The protected namespace that caches and directors are asked about,
        # and the namespace `ready` reads: /public, with no token, where
        # there is one.
        self.protected = next(ns for ns in credentials.PROTECTED if ns in self.fed.exports)
        self.read_ns = session.namespaces[0]
        self.uploaded = False

    def upload(self) -> Optional[str]:
        """Put each namespace's probe at every origin, once (see
        stores.seed()); why that failed, or None."""
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
        """`pelican object get` of the object through the federation; what
        went wrong, or None."""
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


def ready(probe: Probe) -> tuple[str, str]:
    """Dies if the federation never serves: nothing after it could pass."""
    why: Optional[str] = None
    for n in range(1, TRIES + 1):
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
    """Each cache serves the object with the server's token."""
    if not probe.fed.caches:
        return SKIP, "this shape has no cache"
    token = probe.session.token(probe.protected)
    problems: list[str] = []
    for cache in probe.fed.caches:
        url = f"{cache.url}/{probe.protected}/{probe.rel}"
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
        elif answer.body != probe.data[probe.protected]:
            problems.append(f"{cache.svc}: the body is not the object")
    if problems:
        return FAIL, common.first(problems)
    return PASS, f"{len(probe.fed.caches)} cache(s)"


def directors(probe: Probe) -> tuple[str, str]:
    """Where each director would send a GET of the object, for a cache
    (`object`) and for an origin: both lists must name every one. The
    `origin` route is also how caches reach origins, so without
    ?directread it names origins whether or not they take direct
    clients. Only with ?directread, a client's direct read, does the
    director check DirectReads (originSupportsQuery() in Pelican's
    director/sort.go): then it must name every origin where the
    namespace takes direct clients, and elsewhere find none (405), and
    name none."""
    fed = probe.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no director"
    if fed.tiny:
        return SKIP, "the tiny topology's director, cache, and origin are one server"
    path = f"/{probe.protected}/{probe.rel}"
    origin_hosts = [director.host(o.url) for o in fed.origins]
    site_local = [director.host(c.url) for c in fed.caches if c.site_local]
    direct = "DirectReads" in fed.exports[probe.protected]
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
    return PASS, (
        f"{len(fed.directors)} director(s): {len(fed.federated_caches)} cache(s),"
        f" {len(fed.origins)} origin(s), {direct_reads}"
    )


def discovery(probe: Probe) -> tuple[str, str]:
    """The discovery document that a Pelican server serves, in the tiny
    topology (its director) or for a standalone origin: each of
    DISCOVERY_FIELDS as fed.sh has it (an empty one may be missing), and
    a jwks_uri that serves the server's own keys. Elsewhere, the
    discovery host serves fed.sh's own document."""
    fed = probe.fed
    if fed.topology == "tiny":
        key_dir = "issuer-keys/fed"
    elif fed.standalone:
        key_dir = "issuer-keys/origin-0"
    else:
        return SKIP, "the discovery host serves fed.sh's own document"
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


def broker_problems(fed: common.Federation, path: str) -> list[str]:
    """What is wrong with what each director says of the connection
    broker: for an origin's read of path, the X-Pelican-Broker header;
    and in its list of servers, each one's broker URL."""
    want = urlsplit(str(fed.discovery["broker_endpoint"]))
    origin = director.host(fed.origins[0].url)
    problems: list[str] = []

    def wrong(advertised: str) -> Optional[str]:
        got = urlsplit(advertised)
        query = parse_qs(got.query)
        if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, BROKER_PATH):
            return f"'{advertised}' is not the broker at {want.netloc}{BROKER_PATH}"
        if query.get("origin") != [origin] or query.get("prefix") != [f"/{PROTECTED_A}"]:
            return f"'{advertised}' is not for {origin}'s /{PROTECTED_A}"
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
    """Under `-p origin-broker`, each director names origin-0's broker (see
    broker_problems()), within DIRECTORS_WAIT seconds."""
    fed = probe.fed
    if not fed.broker:
        return SKIP, "the origins use no connection broker ('-p origin-broker')"
    path = f"/{probe.protected}/{probe.rel}"
    found: list[str] = []

    def named() -> bool:
        found[:] = broker_problems(fed, path)
        return not found

    if not common.wait_for(named, DIRECTORS_WAIT, interval=5):
        return FAIL, f"after {DIRECTORS_WAIT}s, {common.first(found)}"
    return PASS, f"{len(fed.directors)} director(s)"


def hints(probe: Probe) -> tuple[str, str]:
    """Each director's redirect of a GET of the object, for a cache
    (`object`) and for an origin, in each exported namespace, must say
    which namespace it is and whether it takes a token (X-Pelican-Namespace),
    and name its issuer, where it has one, and no other
    (X-Pelican-Authorization; X-Pelican-Token-Generation, if sent, names it
    too): the client chooses its token by these (see director.hints())."""
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


def presence_problems(
    probe: Probe, holder: common.Origin, namespace: str, rel: str, data: bytes
) -> list[str]:
    """What is wrong with how the federation serves rel in namespace,
    which holder alone holds: each director's answer for an origin's
    read, plain and for a client's direct read (?directread), must name
    holder and no other origin, or, where the namespace takes no direct
    clients, find none (405) for the direct read; and each cache must
    serve the object. The director asks the origins which has it only
    when it has the namespace's token, so each ask presents it."""
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
    """An object put in one origin's store alone, in each exported
    namespace: the directors route it there (see presence_problems()),
    within DIRECTORS_WAIT seconds, and `pelican object get` fetches it
    through a cache, and direct where the namespace allows it."""
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


RUN = {
    "ready": ready,
    "caches": caches,
    "directors": directors,
    "discovery": discovery,
    "broker": broker,
    "hints": hints,
    "presence": presence,
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
