"""How the directors choose where to send a client, and what they answer.

See docs/tests/routing.md.

Each service's own layer (framework/config.d/instance/) gives cache-0
and origin-0 a GeoLocation in Madison, and cache-1 and origin-1 one in
Lisbon (GEO). The framework has no GeoIP database, so a director places
a client that sends no coordinate at random in the contiguous US, and
sorts the Madison servers first; `distance` sends its own.

The other director settings need a restart. Under `-p topo-multi-cache`,
with a control helper, the suite restarts every director four times,
each time with a configuration layer of its own added last
(control.restart_all_with()): CacheSortMethod random; adaptive; adaptive
with CheckCachePresence off; and FilteredServers naming the last cache,
with AssumePresenceAtSingleOrigin off and response hostnames that no DNS
knows, sent in headers. After each restart, and once the directors are
restored, it waits until every director names every server again.
Elsewhere, what needs a restart is skipped. `permitted-namespaces`
restarts the last cache instead, restricted to /public or another
protected namespace, and waits likewise.

The suite's objects are in the probe namespace
(credentials.probe_namespace()) at every origin. It puts them in place,
and fills the last cache for `adaptive`, with WLCG tokens; every request
it sends the directors carries a token of the session's profile.
"""

import functools
import json
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from testlib import common, control, credentials, director, jwt, metrics, stores, suite, web
from testlib.common import first, outcome, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session
from testlib.suite import CaseRow

SCENARIOS = {
    "distance": (
        "a client's X-Pelican-Coordinate and the servers' declared locations decide the order;"
        " X-Pelican-Debug says why"
    ),
    "stat": "an object no origin holds: 404 where the director asks the origins, else 307",
    "redirect": "Location carries the token and parameters, and serves alone; Link carries no token",
    "expired-401": "a token about to expire gets 401 and WWW-Authenticate; an unreadable one 307",
    "status-codes": (
        "unknown namespaces, unwritable writes, a redirected DELETE, bad parameters, old"
        " clients, OPTIONS"
    ),
    "random-and-adaptive": (
        "random orders the caches both ways; adaptive weighs a cache's copy, when it can ask"
    ),
    "filtered": "a cache in Director.FilteredServers is named by no director",
    "response-hostnames": "a bare path asked of a response hostname goes to an origin or a cache",
    "permitted-namespaces": (
        "a cache restricted to a namespace (Cache.PermittedNamespaces) is named for and serves it alone"
    ),
}

# The routes a client asks a director by, for a cache and for an origin.
OBJECT = "/api/v1.0/director/object"
ORIGIN = "/api/v1.0/director/origin"

# The locations that the servers declare (GeoLocation in
# framework/config.d/instance/<service>/), and those the clients claim.
MADISON = (43.07, -89.38)
LISBON = (38.72, -9.14)
GEO = {"cache-0": MADISON, "cache-1": LISBON, "origin-0": MADISON, "origin-1": LISBON}
NEAR_MADRID = (40.4, -3.7)
NEAR_MADISON = (43.1, -89.4)
# X-Pelican-Coordinate values that a director must not take: a latitude
# that is no number, and one out of range.
MALFORMED = ("lat=north, long=-3.7", "lat=95, long=-3.7")
# How a director weighs distance (distanceWeight in Pelican's
# director/sort_algorithms.go): 1 at the client, halving every 200 miles
# of great-circle distance, on an Earth of radius 3960 miles.
EARTH_MILES = 3960
HALVING_MILES = 200

# How an adaptive sort weighs a cache that holds the object, and one that
# doesn't (objAvailabilityFactor).
HOLDS, LACKS = 2.0, 0.5

# What a director answers a token that has expired, or soon will.
EXPIRED_CHALLENGE = 'Bearer error="invalid_token", error_description="token has expired"'

# A client older than any a director serves (minClientVersion).
ANCIENT_CLIENT = "pelican-client/0.0.1"

# A query parameter a director knows nothing of, with a value that a URL
# must escape.
EXTRA_PARAM = "routing-test"
EXTRA_VALUE = "a b&c=d/é?#%"

# The response hostnames of the combined restart: names no DNS knows,
# sent only in headers.
ORIGIN_HOSTNAME = "origins.routing.invalid"
CACHE_HOSTNAME = "caches.routing.invalid"

# How many times `random` asks each director, `filtered` each route, and
# `permitted-namespaces` each director.
RANDOM_ASKS = 20
FILTERED_ASKS = 5
PERMITTED_ASKS = 5
# How long a V2 cache may take, after it has fetched an object, to give
# it an Age of a second or more, which a director takes as its copy.
AGE_WAIT = 10

CONFIG_FILE = "90-routing.yaml"


@dataclass(frozen=True)
class Server:
    """An origin or a cache: its service, its data URL, and its kind.

    Attributes:
        kind: "origin", or a cache's kind: "v2" or "xrootd".
    """

    svc: str
    url: str
    kind: str

    @property
    def host(self) -> str:
        """Its host, as a director's URLs name it."""
        return director.host(self.url)


class Test(suite.Test):
    """The suite's objects in the probe namespace, and the servers.

    The namespace is the first protected one, or /public where the
    origins export only that (`-p origin-public`), whose token every
    server ignores.

    Attributes:
        data: The contents of the objects that the suite puts, by name:
            the probe, which every origin holds; `warm`, which every
            origin holds, and only `adaptive` reads, through the last
            cache; and `permitted`, which only `permitted-namespaces`
            puts, in another namespace. No origin holds `missing`.
        caches: The federated caches.
        origins: The origins.
    """

    def __init__(self, session: Session):
        super().__init__(session, "routing", credentials.probe_namespace(session.fed))
        self.data = {
            "probe": os.urandom(4096),
            "warm": os.urandom(8192),
            "permitted": os.urandom(4096),
        }
        self.caches = [Server(c.svc, c.url, c.kind) for c in self.fed.federated_caches]
        self.origins = [Server(o.svc, o.url, "origin") for o in self.fed.origins]

    def path_of(self, name: str, namespace: str | None = None) -> str:
        """Return the federation path of object name.

        Args:
            name: `probe`, `warm`, `missing`, or `permitted`.
            namespace: Its namespace, if not the suite's own.
        """
        return f"/{namespace or self.namespace}/{self.rel(name)}"

    def token_for(self, namespace: str) -> str | None:
        """Return namespace's `server` token, or None for /public."""
        return None if namespace == "public" else self.session.token(namespace)


# --------------------------------------------------------------------------
# Reading a director's answers.


def link_entries(value: str) -> list[tuple[str, dict[str, str]]]:
    """Return a Link header's rel="duplicate" entries, in the order sent.

    Returns:
        Each entry's URL and its parameters (e.g. pri and depth),
        unquoted.
    """
    found: list[tuple[str, dict[str, str]]] = []
    for part in value.split(", <"):
        url, sep, rest = part.lstrip("<").partition(">")
        if not sep:
            continue
        params: dict[str, str] = {}
        for item in rest.split(";"):
            key, eq, val = item.strip().partition("=")
            if eq:
                params[key.strip()] = val.strip().strip('"')
        if params.get("rel") == "duplicate":
            found.append((url, params))
    return found


def linked(answer: web.Response) -> list[str]:
    """Return the hosts in answer's Link, in the order sent."""
    return [director.host(u) for u, _ in link_entries(answer.header("Link"))]


def listed_servers(url: str) -> tuple[web.Response, list[dict[str, Any]]]:
    """Return the director at url's list of servers, and its answer."""
    answer = web.request("GET", f"{url}{director.SERVERS}")
    return answer, common.objects(answer.json())


def cache_row(rows: Sequence[Mapping[str, Any]], cache: Server) -> Mapping[str, Any] | None:
    """Return the row of a list of servers that is cache's, or None."""
    return next(
        (
            r
            for r in rows
            if r.get("type") == "Cache" and director.host(str(r.get("webUrl", ""))) == cache.host
        ),
        None,
    )


def query_of(location: str) -> dict[str, list[str]]:
    """Return the query parameters of URL location, blank values kept."""
    return parse_qs(urlsplit(location).query, keep_blank_values=True)


def where(location: str, servers: Sequence[Server]) -> Server | None:
    """Return which of servers URL location is at, or None."""
    return next((s for s in servers if location.startswith(s.url + "/")), None)


def servers_info(info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return what a debug answer says of each server it sorted, by host.

    Args:
        info: The body of a director's answer to X-Pelican-Debug
            (server_structs.RedirectInfo).
    """
    found = common.as_object(info.get("serversInfo")) or {}
    return {director.host(url): common.as_object(v) or {} for url, v in found.items()}


def weight(server: Mapping[str, Any], name: str) -> float:
    """Return a server's weight in a debug answer; NaN if it has none.

    Args:
        server: What the answer says of the server.
        name: The weight's name, e.g. distanceWeight.
    """
    weights = common.as_object(server.get("RedirectWeights")) or {}
    value = weights.get(name)
    return float(value) if isinstance(value, (int, float)) else math.nan


def coordinate(found: Mapping[str, Any]) -> dict[str, Any]:
    """Return a server's Coordinate, or clientInfo's, in a debug answer."""
    return common.as_object(found.get("Coordinate")) or {}


def at(found: Mapping[str, Any], place: tuple[float, float]) -> bool:
    """Return whether a debug answer's Coordinate is place."""
    return (found.get("Lat"), found.get("Long")) == place


def distance_weight(client: tuple[float, float], server: tuple[float, float]) -> float:
    """Return the distance weight a director gives server for client.

    That is, by distance alone (distanceWeight and angularDistanceOnSphere
    in Pelican).
    """
    if client == server:
        return 1.0
    phi1, phi2 = math.radians(90 - client[0]), math.radians(90 - server[0])
    theta = math.radians(client[1]) - math.radians(server[1])
    cos = math.sin(phi1) * math.sin(phi2) * math.cos(theta) + math.cos(phi1) * math.cos(phi2)
    miles = math.acos(max(-1.0, min(1.0, cos))) * EARTH_MILES
    return 1.0 if miles <= 0 else 2.0 ** (-miles / HALVING_MILES)


def coordinate_header(place: tuple[float, float]) -> dict[str, str]:
    """Return an X-Pelican-Coordinate header claiming place."""
    return {"X-Pelican-Coordinate": f"lat={place[0]}, long={place[1]}"}


def ask(
    url: str,
    token: str | None = None,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
) -> web.Response:
    """Send method to url, presenting token, with headers."""
    return web.request(method, url, token=token, headers=headers)


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test: no director."""
    return "a standalone origin has no director" if fed.standalone else None


# --------------------------------------------------------------------------
# distance


def own_version(url: str, version: str | None) -> str:
    """Return the version of the director at url.

    It is the shape's version, if known, or else what the director's
    /api/v1.0/version says; "" if neither says.
    """
    if version:
        return version
    found = web.request("GET", f"{url}/api/v1.0/version").object().get("version")
    return found if isinstance(found, str) else ""


def expected_order(servers: Sequence[Server], client: tuple[float, float]) -> list[Server]:
    """Return servers, nearest client first, by their declared locations."""
    return sorted(servers, key=lambda s: -distance_weight(client, GEO[s.svc]))


def order_problems(
    test: Test, route: str, servers: Sequence[Server], client: tuple[float, float]
) -> list[str]:
    """Return what is wrong with where each director sends a client.

    Location must be the nearest of servers, and Link list them nearest
    first.

    Args:
        test: The Test.
        route: The route by which the client asks for the probe.
        servers: The servers it may be sent to.
        client: Where the client says it is.
    """
    want = [s.host for s in expected_order(servers, client)]
    hosts = {s.host for s in servers}
    problems: list[str] = []
    for url in test.fed.directors:
        answer = ask(f"{url}{route}{test.path_of('probe')}", test.token, coordinate_header(client))
        what = f"{director.host(url)} ({route.rsplit('/', 1)[-1]}, near {client})"
        listed = [h for h in linked(answer) if h in hosts]
        if answer.status != 307:
            problems.append(f"{what}: {answer.describe()}, not 307")
        elif director.host(answer.header("Location")) != want[0]:
            problems.append(f"{what}: Location is at {answer.header('Location')}, not {want[0]}")
        elif listed != want:
            problems.append(f"{what}: Link lists {listed}, not {want}")
    return problems


def debug_problems(
    test: Test, route: str, servers: Sequence[Server], client: tuple[float, float]
) -> list[str]:
    """Return what is wrong with how each director explains a distance sort.

    With X-Pelican-Debug, each must answer a 307 to the nearest of
    servers, whose body names the sort, the client's coordinate as it
    declared it, and each server's declared coordinate and its weight.

    Args:
        test: The Test.
        route: The route by which the client asks for the probe.
        servers: The servers it may be sent to.
        client: Where the client says it is.
    """
    nearest = expected_order(servers, client)[0]
    problems: list[str] = []
    for url in test.fed.directors:
        headers = {**coordinate_header(client), "X-Pelican-Debug": "true"}
        answer = ask(f"{url}{route}{test.path_of('probe')}", test.token, headers)
        what = f"{director.host(url)} ({route.rsplit('/', 1)[-1]}, debug)"
        info = answer.object()
        if answer.status != 307 or not info:
            problems.append(f"{what}: {answer.describe()}, not a 307 with a JSON body")
            continue
        if director.host(answer.header("Location")) != nearest.host:
            problems.append(f"{what}: Location is at {answer.header('Location')}")
        if info.get("directorSortMethod") != "distance":
            problems.append(f"{what}: directorSortMethod is {info.get('directorSortMethod')!r}")
        client_at = coordinate(common.as_object(info.get("clientInfo")) or {})
        if not at(client_at, client) or client_at.get("Source") != "declared":
            problems.append(f"{what}: clientInfo's Coordinate is {client_at}")
        found = servers_info(info)
        for server in servers:
            entry = found.get(server.host)
            if entry is None:
                problems.append(f"{what}: serversInfo lacks {server.svc}: {sorted(found)}")
                continue
            place = coordinate(entry)
            if not at(place, GEO[server.svc]) or place.get("Source") != "declared":
                problems.append(f"{what}: {server.svc}'s Coordinate is {place}")
            got = weight(entry, "distanceWeight")
            want = distance_weight(client, GEO[server.svc])
            if not math.isclose(got, want, rel_tol=1e-6, abs_tol=1e-12):
                problems.append(f"{what}: {server.svc}'s distanceWeight is {got}, not {want:.6g}")
    return problems


def malformed_problems(test: Test) -> list[str]:
    """Return what is wrong with each director's answer to a bad coordinate.

    Given a malformed X-Pelican-Coordinate, it must still send the client
    to a cache, and must not take the header as the client's coordinate.
    """
    problems: list[str] = []
    for url in test.fed.directors:
        for value in MALFORMED:
            headers = {"X-Pelican-Coordinate": value, "X-Pelican-Debug": "true"}
            answer = ask(f"{url}{OBJECT}{test.path_of('probe')}", test.token, headers)
            what = f"{director.host(url)} ({value!r})"
            client_at = coordinate(common.as_object(answer.object().get("clientInfo")) or {})
            if answer.status != 307:
                problems.append(f"{what}: {answer.describe()}, not 307")
            elif where(answer.header("Location"), test.caches) is None:
                problems.append(f"{what}: Location is at {answer.header('Location')}, no cache")
            elif client_at.get("Source") == "declared":
                problems.append(f"{what}: took the header as the client's coordinate")
    return problems


def distance(test: Test) -> list[CaseRow]:
    """Check that the directors sort the caches and origins by distance.

    The sort is the default. Each director must order them as a client
    near Madrid, and one near Madison, says it is (see order_problems()),
    and explain why with X-Pelican-Debug (see debug_problems()); and must
    route a client whose coordinate it can't read (see
    malformed_problems()).
    """
    rows: list[CaseRow] = []
    for kind, route, servers in (
        ("caches", OBJECT, test.caches),
        ("origins", ORIGIN, test.origins),
    ):
        if len(servers) < 2:
            why = f"one {kind[:-1]}: nothing to order"
            rows += [(kind, SKIP, why), (f"{kind}-debug", SKIP, why)]
            continue
        unplaced = [s.svc for s in servers if s.svc not in GEO]
        if unplaced:
            why = f"no location is declared for {', '.join(unplaced)}"
            rows += [(kind, FAIL, why), (f"{kind}-debug", FAIL, why)]
            continue
        problems = [p for c in (NEAR_MADRID, NEAR_MADISON) for p in order_problems(test, route, servers, c)]
        near = ", ".join(f"{c}: {expected_order(servers, c)[0].svc}" for c in (NEAR_MADRID, NEAR_MADISON))
        rows.append((kind, *outcome(problems, near)))
        problems = debug_problems(test, route, servers, NEAR_MADRID)
        rows.append((f"{kind}-debug", *outcome(problems, f"{len(servers)} weighed")))
    if not test.caches:
        rows.append(("malformed", SKIP, "no federated cache"))
    else:
        problems = malformed_problems(test)
        rows.append(("malformed", *outcome(problems, f"{len(MALFORMED)} values: to a cache")))
    return rows


# --------------------------------------------------------------------------
# stat


def unasked(test: Test, namespace: str) -> bool:
    """Return whether a director can't ask the origins about namespace.

    Where the namespace takes no direct clients, the origins refuse the
    director's stat of an object in it, which carries no federation
    token. The director must then take a refusal for presence, since it
    cannot tell (generateAvailabilityMaps).
    """
    return "DirectReads" not in test.fed.exports[namespace]


def stat_problems(
    test: Test, namespaces: Sequence[str], query: str, with_token: bool, found: bool
) -> list[str]:
    """Return what is wrong with each director's answers for `missing`.

    Each director is sent a GET and a HEAD of it by its origin route. If
    found, each answer must be a 307 to an origin; otherwise 404, and a
    GET's body saying no origin had it, but where the director cannot ask
    (unasked()), a 307 too.

    Args:
        test: The Test.
        namespaces: The namespaces to ask in.
        query: The query to send, if any.
        with_token: Whether to present the namespace's token.
        found: Whether the director must take the object as present.
    """
    problems: list[str] = []
    for url in test.fed.directors:
        for ns in namespaces:
            token = test.token_for(ns) if with_token else None
            target = f"{url}{ORIGIN}{test.path_of('missing', ns)}" + (f"?{query}" if query else "")
            for method in ("GET", "HEAD"):
                answer = ask(target, token, method=method)
                what = f"{director.host(url)}: {method} /{ns}{'?' + query if query else ''}"
                redirected = (
                    answer.status == 307 and where(answer.header("Location"), test.origins) is not None
                )
                if found or (answer.status == 307 and unasked(test, ns)):
                    if not redirected:
                        problems.append(f"{what}: {answer.describe()}, not 307 to an origin")
                elif answer.status != 404:
                    problems.append(f"{what}: {answer.describe()}, not 404")
                elif method == "GET" and b"possession of the object" not in answer.body:
                    problems.append(f"{what}: 404, but its body is {answer.body[:200]!r}")
    return problems


def stat(test: Test) -> list[CaseRow]:
    """Check each director's answer for an object no origin holds.

    The object is asked for of each director's origin route: with
    several origins, the director asks them (shouldStatOrigins in
    Pelican's director/stat.go), and must answer 404; with ?skipstat, or
    with no token in a protected namespace, it can't or won't ask, and
    must redirect; with one origin, it assumes the object is there
    (Director.AssumePresenceAtSingleOrigin), and must redirect. The
    restart's `single-checked` row checks one origin with that off.
    """
    namespaces = test.session.namespaces
    protected = [ns for ns in namespaces if ns in credentials.PROTECTED]
    several = len(test.origins) > 1
    rows: list[CaseRow] = []
    if several:
        problems = stat_problems(test, namespaces, "", True, False)
        rows.append(("several", *outcome(problems, f"{len(test.origins)} origins, 404")))
    else:
        rows.append(("several", SKIP, "one origin, which the director need not ask"))
    problems = stat_problems(test, namespaces, "skipstat", True, True)
    rows.append(("skipstat", *outcome(problems, "307")))
    problems = stat_problems(test, protected, "", False, True)
    rows.append(("no-token", *outcome(problems, f"{', '.join(protected)}: 307")))
    if several:
        rows.append(("single", SKIP, f"{len(test.origins)} origins"))
    else:
        problems = stat_problems(test, namespaces, "", True, True)
        rows.append(("single", *outcome(problems, "assumed present: 307")))
    return rows


def single_checked(test: Test) -> tuple[str, str]:
    """Check that a director asks a single origin about the missing object.

    With Director.AssumePresenceAtSingleOrigin off, the one origin is
    asked, and the answer must be 404 (see stat_problems()).
    """
    problems = stat_problems(test, test.session.namespaces, "", True, False)
    return outcome(problems, "404")


# --------------------------------------------------------------------------
# redirect


@dataclass
class Redirects:
    """The checks of `redirect` at one director and route.

    Attributes:
        url: The director's URL.
        route: "object", or "bare".
        problems: What the checks found: each case's problems, by case.
    """

    test: Test
    url: str
    route: str
    problems: dict[str, list[str]]

    @property
    def target(self) -> str:
        """The URL asked for the probe."""
        prefix = OBJECT if self.route == "object" else ""
        return f"{self.url}{prefix}{self.test.path_of('probe')}"

    def note(self, case: str, problem: str) -> None:
        """Record a problem of case."""
        asked = f"{director.host(self.url)} ({self.route})"
        self.problems.setdefault(case, []).append(f"{asked}: {problem}")

    def redirect(self, case: str, answer: web.Response) -> dict[str, list[str]] | None:
        """Return the query of answer's Location, if it is a 307.

        Otherwise, note a problem of case, and return None.
        """
        if answer.status != 307 or not answer.header("Location"):
            self.note(case, f"{answer.describe()}, not 307")
            return None
        return query_of(answer.header("Location"))

    def follow(self, case: str, answer: web.Response) -> None:
        """Follow answer's Location, presenting nothing.

        curl, too, presents nothing across hosts. What it lands at must
        serve the probe, where the namespace lets it, and otherwise
        refuse.
        """
        test = self.test
        location = answer.header("Location")
        at_origin = where(location, test.origins) is not None
        allowed = (
            credentials.decide(credentials.ALLOW, test.caps, "get", direct=at_origin) == credentials.ALLOW
        )
        served = web.request("GET", location)
        if not allowed:
            if served.status not in (401, 403):
                self.note(case, f"followed: {served.describe()}, not a refusal")
        elif served.status != 200:
            self.note(case, f"followed to {location.split('?')[0]}: {served.describe()}")
        elif served.body != test.data["probe"]:
            self.note(case, "followed: the body is not the probe")

    def link(self, case: str, answer: web.Response) -> list[str]:
        """Check answer's Link, and return its URLs.

        None of its URLs may carry the token; pri must run 1..N in order;
        and depth must be the probe's below the namespace.
        """
        token = self.test.token
        entries = link_entries(answer.header("Link"))
        depth = str(len(self.test.rel("probe").split("/")))
        if not entries:
            self.note(case, "no Link")
        if token in answer.header("Link") or "authz=" in answer.header("Link"):
            self.note(case, "Link carries the token")
        pris = [params.get("pri") for _, params in entries]
        if pris != [str(n) for n in range(1, len(entries) + 1)]:
            self.note(case, f"Link's pri values are {pris}")
        depths = {params.get("depth") for _, params in entries}
        if entries and depths != {depth}:
            self.note(case, f"Link's depths are {sorted(map(str, depths))}, not {depth}")
        return [u for u, _ in entries]

    def check(self) -> None:
        """Check every case, at this director and route."""
        test = self.test
        token = test.token

        # The token from the header, or from ?authz (with "Bearer " or
        # without), goes in Location, and Location alone serves.
        answer = ask(self.target, token)
        found = self.redirect("header-token", answer)
        namespace = answer.header("X-Pelican-Namespace")
        listed: list[str] = []
        if found is not None:
            if found.get("authz") != [token]:
                self.note("header-token", f"Location's authz is {found.get('authz')}")
            listed = self.link("header-token", answer)
            self.follow("header-token", answer)
        for case, value in (("query-token", token), ("query-bearer", f"Bearer {token}")):
            answer = ask(f"{self.target}?authz={quote(value, safe='')}")
            found = self.redirect(case, answer)
            if found is not None:
                if found.get("authz") != [token]:
                    self.note(case, f"Location's authz is {found.get('authz')}")
                self.link(case, answer)
                self.follow(case, answer)

        # X-Pelican-Timeout, and the query parameters, pass through.
        found = self.redirect("timeout", ask(self.target, token, {"X-Pelican-Timeout": "5s"}))
        if found is not None and found.get("pelican.timeout") != ["5s"]:
            self.note("timeout", f"Location's pelican.timeout is {found.get('pelican.timeout')}")
        query = f"skipstat&{EXTRA_PARAM}={quote(EXTRA_VALUE, safe='')}"
        found = self.redirect("params", ask(f"{self.target}?{query}", token))
        if found is not None:
            if found.get("skipstat") != [""]:
                self.note("params", f"Location's skipstat is {found.get('skipstat')}")
            if found.get(EXTRA_PARAM) != [EXTRA_VALUE]:
                self.note("params", f"Location's {EXTRA_PARAM} is {found.get(EXTRA_PARAM)}")
        if test.direct:
            found = self.redirect("params", ask(f"{self.target}?directread", token))
            if found is not None and found.get("directread") != [""]:
                self.note("params", f"Location's directread is {found.get('directread')}")

        # X-Pelican-JobId comes back, if it is a UUID; otherwise one of
        # the director's.
        sent = str(uuid.uuid4())
        answer = ask(self.target, token, {"X-Pelican-JobId": sent})
        if answer.header("X-Pelican-JobId") != sent:
            self.note("job-id", f"X-Pelican-JobId {answer.header('X-Pelican-JobId')!r}, not {sent}")
        answer = ask(self.target, token, {"X-Pelican-JobId": "not-a-uuid"})
        got = answer.header("X-Pelican-JobId")
        try:
            uuid.UUID(got)
        except ValueError:
            self.note("job-id", f"for a JobId no UUID, X-Pelican-JobId {got!r}, no UUID")

        # ?redirect=false: the same answer, but 200, and no Location.
        answer = ask(f"{self.target}?redirect=false", token)
        if answer.status != 200 or answer.header("Location"):
            location = answer.header("Location")
            self.note("no-redirect", f"{answer.describe()}, Location {location!r}: not 200")
        elif not answer.header("X-Pelican-Namespace"):
            self.note("no-redirect", "no X-Pelican-Namespace")
        elif answer.header("X-Pelican-Namespace") != namespace:
            self.note(
                "no-redirect",
                f"X-Pelican-Namespace {answer.header('X-Pelican-Namespace')!r},"
                + f" not the redirect's {namespace!r}",
            )
        elif set(self.link("no-redirect", answer)) != set(listed):
            self.note("no-redirect", "Link lists other servers than the redirect's")


REDIRECT_CASES = (
    "header-token",
    "query-token",
    "query-bearer",
    "timeout",
    "params",
    "job-id",
    "no-redirect",
)


def redirect(test: Test) -> list[CaseRow]:
    """Check how each director builds a redirect of the probe.

    That is for its object route and its bare path
    (generateRedirectResponse in Pelican's director/director.go); see
    Redirects.
    """
    problems: dict[str, list[str]] = {}
    for url in test.fed.directors:
        for route in ("object", "bare"):
            Redirects(test, url, route, problems).check()
    note = f"{len(test.fed.directors)} director(s), object route and bare path"
    return [(case, *outcome(problems.get(case, []), note)) for case in REDIRECT_CASES]


# --------------------------------------------------------------------------
# expired-401


def signed(test: Test, issued: int, expires: int | None) -> str:
    """Return a token like the namespace's `server` token.

    It is signed with the origins' key. The director reads it without
    checking its signature.

    Args:
        test: The Test.
        issued: When it is issued (iat and nbf).
        expires: When it expires (exp); None for no exp claim.
    """
    claims = credentials.server_claims(test.fed, test.namespace, profile=test.session.profile)
    claims["iat"] = claims["nbf"] = issued
    if expires is None:
        del claims["exp"]
    else:
        claims["exp"] = expires
    return jwt.sign(claims, test.fed.origin_key)


# Each case: its name, the token to present (made from now), whether in
# ?authz, and whether the director must refuse it.
EXPIRY_CASES: tuple[tuple[str, Callable[[Test, int], str], bool, bool], ...] = (
    ("expired", lambda t, now: signed(t, now - 900, now - 300), False, True),
    ("expiring", lambda t, now: signed(t, now - 3600, now + 5), False, True),
    ("half-life", lambda t, now: signed(t, now - 6, now + 4), False, True),
    ("in-query", lambda t, now: signed(t, now - 900, now - 300), True, True),
    ("no-lifetime", lambda t, now: signed(t, now + 600, now + 600), False, True),
    ("short-fresh", lambda t, now: signed(t, now, now + 10), False, False),
    ("opaque", lambda t, now: f"opaque-{uuid.uuid4().hex}", False, False),
    ("no-exp", lambda t, now: signed(t, now, None), False, False),
)


def expired_401(test: Test) -> list[CaseRow]:
    """Check that a token about to expire gets 401 and WWW-Authenticate.

    Each director, on its object and origin routes and the bare path of
    the probe, must refuse a token that expires within its grace (10
    seconds, or half the token's lifetime if less), or whose lifetime is
    none, with 401 and a WWW-Authenticate challenge, so that a client
    gets a new one (validateClientToken in Pelican's
    director/director.go): one long expired, in the header or ?authz;
    one that expires in 5 seconds; a 10-second one issued 6 seconds ago;
    and one that expires as it is issued. A 10-second one just issued,
    and those whose expiry it can't read, an opaque token and a JWT with
    no exp, must be redirected (307).
    """
    rows: list[CaseRow] = []
    targets = [("object", OBJECT), ("origin", ORIGIN), ("bare", "")]
    for case, make, in_query, refused in EXPIRY_CASES:
        problems: list[str] = []
        for url in test.fed.directors:
            for route, prefix in targets:
                token = make(test, int(time.time()))
                target = f"{url}{prefix}{test.path_of('probe')}"
                if in_query:
                    answer = ask(f"{target}?authz={quote(token, safe='')}")
                else:
                    answer = ask(target, token)
                what = f"{director.host(url)} ({route})"
                challenge = answer.header("WWW-Authenticate")
                if not refused and answer.status != 307:
                    problems.append(f"{what}: {answer.describe()}, not 307")
                elif refused and answer.status != 401:
                    problems.append(f"{what}: {answer.describe()}, not 401")
                elif refused and challenge != EXPIRED_CHALLENGE:
                    problems.append(f"{what}: WWW-Authenticate is {challenge!r}")
        rows.append((case, *outcome(problems, "401" if refused else "307")))
    return rows


# --------------------------------------------------------------------------
# status-codes


def unknown_namespace(test: Test) -> tuple[str, str]:
    """Check each director's answer for a namespace no origin exports.

    Each must answer 404, but within director.STARTUP_WINDOW seconds of
    starting, 429 (director.unknown_statuses()).
    """
    path = f"/routing-nowhere-{test.run}/probe"
    problems: list[str] = []
    unsure: list[str] = []
    for url in test.fed.directors:
        before = metrics.started_ago(url)
        answer = ask(f"{url}{OBJECT}{path}", test.token)
        after = metrics.started_ago(url)
        what = director.host(url)
        if before is None or after is None:
            unsure.append(f"{what}: its /metrics has no process_start_time_seconds")
            continue
        want = director.unknown_statuses(before, after)
        if answer.status not in want:
            problems.append(f"{what}, started {after:.0f}s ago: {answer.describe()}, not {want}")
    if problems:
        return FAIL, first(problems)
    if unsure:
        return INCONCLUSIVE, first(unsure)
    return PASS, f"{len(test.fed.directors)} director(s)"


@dataclass
class Statuses:
    """`status-codes`'s cases, and every answer they got.

    Attributes:
        answers: Every answer that the cases got, with what it was to;
            the last cases check their Server headers.
    """

    test: Test
    answers: list[tuple[str, web.Response]]

    def ask(
        self,
        what: str,
        url: str,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> web.Response:
        """Return ask()'s answer, and keep it, labeled what."""
        answer = ask(url, token, headers, method)
        self.answers.append((what, answer))
        return answer

    def expect(self, what: str, want: int, url: str, **kwargs: Any) -> list[str]:
        """Return a problem, if the answer at url is not want."""
        answer = self.ask(what, url, **kwargs)
        return [] if answer.status == want else [f"{what}: {answer.describe()}, not {want}"]

    def writes(self) -> tuple[str, str]:
        """Check that a PUT or DELETE of a bare path in /public gets 405.

        No origin takes writes in /public, so every origin of the
        namespace is found, and none supports the verb
        (noOriginsForReqErr).
        """
        if "public" not in self.test.fed.exports:
            return SKIP, "the shape exports no /public"
        if "Writes" in self.test.fed.exports["public"]:
            return SKIP, "/public takes writes"
        path = f"/public/{self.test.base}/nothing"
        problems: list[str] = []
        for url in self.test.fed.directors:
            for method in ("PUT", "DELETE"):
                what = f"{director.host(url)}: {method} {path}"
                problems += self.expect(what, 405, f"{url}{path}", method=method)
        return outcome(problems, "405")

    def delete(self) -> tuple[str, str]:
        """Check that a bare-path DELETE, followed as curl follows it, removes the object.

        Each director must redirect a DELETE of an object in the
        namespace to an origin (307), carrying the token in Location's
        authz, since a client such as curl drops its Authorization header
        when a redirect leads to another host. Followed so, the DELETE
        must succeed, and leave that origin's store without the object;
        another origin's replica is not the DELETE's to remove.
        """
        test = self.test
        if not test.writes:
            return SKIP, f"/{test.namespace} takes no writes"
        cannot = stores.cannot(test.fed, "delete")
        if cannot:
            return SKIP, cannot
        problems: list[str] = []
        for url in test.fed.directors:
            host = director.host(url)
            rel = test.rel(f"delete-{host}")
            errors = stores.put_everywhere(
                stores.unique(test.fed.origins), test.namespace, rel, os.urandom(4096), test.setup
            )
            if errors:
                problems.append(f"{host}: could not put the object in place: {first(errors)}")
                continue
            path = f"/{test.namespace}/{rel}"
            answer = self.ask(f"{host}: DELETE {path}", f"{url}{path}", test.token, method="DELETE")
            location = answer.header("Location")
            if answer.status != 307 or not location:
                problems.append(f"{host}: DELETE got {answer.describe()}, not 307")
                continue
            if "authz=" not in urlsplit(location).query:
                problems.append(f"{host}: the redirect's Location carries no authz")
                continue
            target = urljoin(url, location)
            origin = next(
                (o for o in test.fed.origins if director.host(o.url) == director.host(target)), None
            )
            if origin is None:
                problems.append(f"{host}: the DELETE was redirected to {director.host(target)}, no origin")
                continue
            followed = web.request("DELETE", target)
            if followed.status not in (200, 204):
                problems.append(f"{host}: the DELETE, redirected, got {followed.describe()}")
            elif stores.held(origin, test.namespace, rel, test.setup) is not None:
                problems.append(f"{host}: after the DELETE, {origin.svc} still holds {path}")
        return outcome(problems, f"{len(test.fed.directors)} director(s): redirected, and removed")

    def bad_requests(self) -> tuple[str, str]:
        """Check that conflicting parameters and too old a client get 400.

        ?directread with ?prefercached conflict, and ANCIENT_CLIENT is too
        old. A client of the director's own version gets 307.
        """
        problems: list[str] = []
        token = self.test.token
        for url in self.test.fed.directors:
            target = f"{url}{OBJECT}{self.test.path_of('probe')}"
            name = director.host(url)
            problems += self.expect(
                f"{name}: ?directread&prefercached",
                400,
                f"{target}?directread&prefercached",
                token=token,
            )
            version = own_version(url, self.test.fed.pelican_version)
            agents = [(ANCIENT_CLIENT, 400)]
            if version:
                agents.append((f"pelican-client/{version}", 307))
            else:
                problems.append(f"{name}: its version is unknown")
            for agent, want in agents:
                problems += self.expect(
                    f"{name}: {agent}", want, target, token=token, headers={"User-Agent": agent}
                )
        return outcome(problems, "400, and a client of the director's version 307")

    def options(self) -> tuple[str, str]:
        """Check that a bare OPTIONS gets 200, with Allow and CORS headers.

        ShortcutMiddleware answers it.
        """
        problems: list[str] = []
        for url in self.test.fed.directors:
            what = f"{director.host(url)}: OPTIONS"
            answer = self.ask(what, f"{url}{self.test.path_of('probe')}", method="OPTIONS")
            allow = {m.strip() for m in answer.header("Allow").split(",")}
            if answer.status != 200:
                problems.append(f"{what}: {answer.describe()}, not 200")
            elif not {"GET", "HEAD", "OPTIONS"} <= allow:
                problems.append(f"{what}: Allow is {answer.header('Allow')!r}")
            elif answer.header("Access-Control-Allow-Origin") != "*":
                origin = answer.header("Access-Control-Allow-Origin")
                problems.append(f"{what}: Access-Control-Allow-Origin is {origin!r}")
            elif not answer.header("Access-Control-Allow-Methods"):
                problems.append(f"{what}: no Access-Control-Allow-Methods")
        return outcome(problems, "200, with Allow and CORS")

    def servers(self) -> list[CaseRow]:
        """Check that each answer names the server as Pelican, in one field.

        Each answer that came must name it as Pelican, of the shape's
        version, `Server: pelican/<version>`; and in one field, since
        Server is no list (RFC 9110, sections 5.3 and 10.2.4).
        """
        version = self.test.fed.pelican_version
        wrong: list[str] = []
        repeated: list[str] = []
        for what, answer in self.answers:
            if answer.status is None:
                continue
            values = [v for k, v in answer.headers if k.lower() == "server"]
            if not values or any(not v.startswith("pelican/") for v in values):
                wrong.append(f"{what}: Server {values}")
            elif version and any(v != f"pelican/{version}" for v in values):
                wrong.append(f"{what}: Server {values}, not pelican/{version}")
            if len(values) > 1:
                repeated.append(f"{what}: {len(values)} Server fields")
        want = f"pelican/{version}" if version else "pelican/<custom>"
        return [
            ("server", *outcome(wrong, f"{len(self.answers)} answers: {want}")),
            ("server-once", *outcome(repeated, f"{len(self.answers)} answers")),
        ]


def status_codes(test: Test) -> list[CaseRow]:
    """Check what each director refuses, and how; and its Server headers.

    See Statuses and unknown_namespace().
    """
    checks = Statuses(test, [])
    rows: list[CaseRow] = [("unknown-namespace", *unknown_namespace(test))]
    rows.append(("writes", *checks.writes()))
    rows.append(("delete", *checks.delete()))
    rows.append(("bad-requests", *checks.bad_requests()))
    rows.append(("options", *checks.options()))
    for url in test.fed.directors:
        name = director.host(url)
        checks.ask(f"{name}: object route", f"{url}{OBJECT}{test.path_of('probe')}", test.token)
        checks.ask(f"{name}: bare path", f"{url}{test.path_of('probe')}", test.token)
    return rows + checks.servers()


# --------------------------------------------------------------------------
# The restarts: random-and-adaptive, filtered, response-hostnames, and
# stat's single-checked.


def unnamed(test: Test, filtered: frozenset[str] = frozenset()) -> str | None:
    """Return why a director does not name every server for the probe.

    Each director must name every federated cache, but those filtered
    (hosts), and every origin. It is asked with setup: this checks the
    shape, not the pass's tokens.

    Returns:
        Why, or None if each does.
    """
    path = test.path_of("probe")
    for url in test.fed.directors:
        name = director.host(url)
        found = director.ask(url, "object", path, token=test.setup)
        for cache in test.caches:
            if cache.host not in filtered and not found.names(cache.host):
                return f"{name} does not name {cache.svc}: {found.describe()}"
        found = director.ask(url, "origin", path, token=test.setup)
        for origin in test.origins:
            if not found.names(origin.host):
                return f"{name} does not name {origin.svc}: {found.describe()}"
    return None


def wait_named(test: Test, filtered: frozenset[str] = frozenset()) -> str | None:
    """Wait until every director names every server (see unnamed()).

    A server that exits on its own ends the wait (control.settle()).

    Returns:
        Why they don't within suite.SETTLE_WAIT seconds, or None.
    """
    return control.settle(lambda: unnamed(test, filtered), suite.SETTLE_WAIT)


def random_order(test: Test) -> tuple[str, str]:
    """Check that each director, sorting at random, puts each cache first.

    With CacheSortMethod random, each director, asked RANDOM_ASKS times
    for the probe, must put each cache first at least once.
    """
    hosts = {c.host for c in test.caches}
    problems: list[str] = []
    for url in test.fed.directors:
        name = director.host(url)
        firsts: dict[str, int] = {}
        for _ in range(RANDOM_ASKS):
            answer = ask(f"{url}{OBJECT}{test.path_of('probe')}", test.token)
            listed = [h for h in linked(answer) if h in hosts]
            if answer.status != 307 or set(listed) != hosts:
                problems.append(f"{name}: {answer.describe()}, listing {listed}")
                break
            firsts[listed[0]] = firsts.get(listed[0], 0) + 1
        else:
            if set(firsts) != hosts:
                problems.append(f"{name}: in {RANDOM_ASKS} asks, first only {firsts}")
    return outcome(problems, f"each cache first, in {RANDOM_ASKS} asks per director")


def availability(test: Test, token: str | None) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Read each director's adaptive sort of the caches for `warm`.

    Each is asked presenting token, and explains its sort with
    X-Pelican-Debug.

    Returns:
        What is wrong; and each director's availabilityWeight of each
        cache, by the director's host and the cache's.
    """
    problems: list[str] = []
    found: dict[str, dict[str, float]] = {}
    headers = {**coordinate_header(NEAR_MADISON), "X-Pelican-Debug": "true"}
    for url in test.fed.directors:
        name = director.host(url)
        answer = ask(f"{url}{OBJECT}{test.path_of('warm')}", token, headers)
        info = answer.object()
        if answer.status != 307 or not info:
            problems.append(f"{name}: {answer.describe()}, not a 307 with a JSON body")
            continue
        if info.get("directorSortMethod") != "adaptive":
            problems.append(f"{name}: directorSortMethod is {info.get('directorSortMethod')!r}")
            continue
        servers = servers_info(info)
        weights = {c.host: weight(servers.get(c.host, {}), "availabilityWeight") for c in test.caches}
        missing = [h for h, w in weights.items() if math.isnan(w)]
        if missing:
            problems.append(f"{name}: no availabilityWeight for {missing}: {sorted(servers)}")
        found[name] = weights
    return problems, found


def equal_weights(test: Test, token: str | None) -> tuple[str, str]:
    """Check that adaptive sorts weigh the caches' availability alike.

    Each director's adaptive sort, which can't tell which cache holds
    `warm`, asked presenting token, must weigh their availability alike.
    """
    problems, found = availability(test, token)
    for name, weights in found.items():
        if len({round(w, 9) for w in weights.values()}) > 1:
            problems.append(f"{name}: availabilityWeight {weights}, not equal")
    seen = sorted({w for weights in found.values() for w in weights.values()})
    return outcome(problems, f"equal: {seen}")


def tokenless_weights(test: Test) -> tuple[str, str]:
    """Check equal_weights() with no token.

    With none, a director can't ask the caches of a protected namespace
    (shouldStatCaches).
    """
    return equal_weights(test, None)


def unchecked_weights(test: Test) -> tuple[str, str]:
    """Check equal_weights() with the token, but CheckCachePresence off."""
    return equal_weights(test, test.token)


def warm_last_cache(test: Test) -> str | None:
    """Read `warm` through the last cache alone, and wait for its Age.

    Wait until the cache gives the copy an Age of a second or more, by
    which a director takes it as held there (generateAvailabilityMaps).

    Returns:
        Why that failed, or None.
    """
    cache = test.caches[-1]
    url = f"{cache.url}{test.path_of('warm')}"
    why = web.served(web.request("GET", url, token=test.setup), test.data["warm"])
    if why:
        return f"reading `warm` through {cache.svc}: {why}"

    def aged() -> bool:
        age = web.age(web.request("HEAD", url, token=test.setup))
        return age is not None and age >= 1

    if not common.wait_for(aged, AGE_WAIT, interval=0.5):
        return f"{cache.svc} gave `warm` no Age of a second or more within {AGE_WAIT}s"
    return None


def adaptive_presence(test: Test) -> tuple[str, str]:
    """Check that adaptive sorts weigh a cache that holds `warm` higher.

    With CacheSortMethod adaptive, `warm` read through the last cache
    alone: each director, asked with the token, which it asks the caches
    with, must weigh that cache's availability HOLDS, and every other
    cache's LACKS (availabilityWeightFn in Pelican's
    director/sort_algorithms.go).
    """
    if any(c.kind != "v2" for c in test.caches):
        return SKIP, "a director takes a cache's copy by its Age, which V2 caches send"
    why = warm_last_cache(test)
    if why:
        return FAIL, why
    holder = test.caches[-1].host
    problems, found = availability(test, test.token)
    for name, weights in found.items():
        for host, got in weights.items():
            want = HOLDS if host == holder else LACKS
            if not math.isclose(got, want, rel_tol=1e-9):
                problems.append(f"{name}: {host}'s availabilityWeight is {got}, not {want}")
    return outcome(problems, f"{test.caches[-1].svc} {HOLDS}, the rest {LACKS}")


def cache_name(test: Test, cache: Server) -> str | None:
    """Return the name that the first director knows cache by.

    Returns:
        The name, from its list of servers; None if it doesn't list it.
    """
    row = cache_row(listed_servers(test.fed.directors[0])[1], cache)
    return None if row is None else str(row.get("name") or "") or None


def unlisted(test: Test, name: str | None) -> str | None:
    """Return why `filtered` can't check the last cache, or None.

    It can't if the first director did not list it, so that the suite
    knows no name to filter it by.
    """
    if name is not None:
        return None
    return f"{director.host(test.fed.directors[0])} does not list {test.caches[-1].svc}"


def filtered_routing(test: Test, name: str | None) -> tuple[str, str]:
    """Check that no director names a cache in FilteredServers.

    With FilteredServers naming the last cache (name), no director may
    name it, by the object route or the bare path, asked FILTERED_ASKS
    times each; and, by the object route, each must still name the
    other caches.
    """
    why = unlisted(test, name)
    if why:
        return FAIL, why
    banned = test.caches[-1]
    others = [c for c in test.caches if c.host != banned.host]
    problems: list[str] = []
    for url in test.fed.directors:
        what = director.host(url)
        for route, prefix in (("object", OBJECT), ("bare", "")):
            for _ in range(FILTERED_ASKS):
                answer = ask(f"{url}{prefix}{test.path_of('probe')}", test.token)
                listed = linked(answer)
                location = director.host(answer.header("Location"))
                if answer.status != 307:
                    problems.append(f"{what} ({route}): {answer.describe()}, not 307")
                    break
                if banned.host in listed or banned.host == location:
                    problems.append(f"{what} ({route}) named {banned.svc}: {listed}")
                    break
                if route == "object" and not all(c.host in listed for c in others):
                    problems.append(f"{what} ({route}) listed only {listed}")
                    break
    return outcome(problems, f"{banned.svc} unnamed, in {FILTERED_ASKS} asks per route")


def filtered_listing(test: Test, name: str | None) -> tuple[str, str]:
    """Check that each director lists the last cache as filtered.

    Each director's list of servers must show the last cache, by name,
    as filtered by its configuration: `filtered` true, `filteredType`
    permFiltered's text (director.PERM_FILTERED).
    """
    why = unlisted(test, name)
    if why:
        return FAIL, why
    problems: list[str] = []
    for url in test.fed.directors:
        answer, rows = listed_servers(url)
        mine = [r for r in rows if r.get("name") == name]
        what = director.host(url)
        if not mine:
            problems.append(f"{what}: {director.SERVERS} ({answer.describe()}) does not list {name!r}")
        elif mine[0].get("filtered") is not True or mine[0].get("filteredType") != director.PERM_FILTERED:
            shown = {k: mine[0].get(k) for k in ("filtered", "filteredType")}
            problems.append(f"{what}: lists {name!r} with {shown}")
    return outcome(problems, f"{name!r}: permFiltered")


def response_hostname(test: Test, header: str, hostname: str, kind: str) -> tuple[str, str]:
    """Check that a bare path asked of hostname goes to kind.

    Each director must send a bare-path GET of the probe, sent with
    header naming hostname, to kind (`origin` or `cache`)
    (checkHostnameRedirects in Pelican's director/director.go).
    """
    servers = test.origins if kind == "origin" else test.caches
    problems: list[str] = []
    for url in test.fed.directors:
        answer = ask(f"{url}{test.path_of('probe')}", test.token, {header: hostname})
        what = f"{director.host(url)} ({header}: {hostname})"
        if answer.status != 307:
            problems.append(f"{what}: {answer.describe()}, not 307")
        elif where(answer.header("Location"), servers) is None:
            problems.append(f"{what}: Location is at {answer.header('Location')}, no {kind}")
    note = "to an origin" if kind == "origin" else "to a cache"
    if kind == test.fed.director_default_response:
        note += ", as without the setting"
    return outcome(problems, note)


def combined_text(name: str | None) -> str:
    """Return the combined restart's configuration.

    It has FilteredServers naming name (none if None),
    AssumePresenceAtSingleOrigin off, and the response hostnames.
    """
    lines = ["Director:"]
    if name:
        lines += ["  FilteredServers:", f"    - {json.dumps(name)}"]
    lines += [
        "  AssumePresenceAtSingleOrigin: false",
        "  OriginResponseHostnames:",
        f"    - {ORIGIN_HOSTNAME}",
        "  CacheResponseHostnames:",
        f"    - {CACHE_HOSTNAME}",
    ]
    return "\n".join(lines) + "\n"


# A check of a restart: its scenario, its case, and what it calls, with
# the Test, for a row's status and note.
Check = tuple[str, str, Callable[[Test], tuple[str, str]]]


@dataclass(frozen=True)
class Phase:
    """A restart of the directors.

    Attributes:
        text: The directors' configuration for it.
        checks: Its checks.
    """

    name: str
    text: str
    checks: tuple[Check, ...]


def phases(selected: Sequence[str], name: str | None) -> list[Phase]:
    """Return the restarts that the selected scenarios need, in order.

    Args:
        selected: The selected scenarios.
        name: The last cache's name, for `filtered`, if known.
    """
    found: list[Phase] = []
    scenario = "random-and-adaptive"
    if scenario in selected:
        found.append(
            Phase(
                "random",
                "Director:\n  CacheSortMethod: random\n",
                ((scenario, "random", random_order),),
            )
        )
        found.append(
            Phase(
                "adaptive",
                "Director:\n  CacheSortMethod: adaptive\n",
                (
                    (scenario, "adaptive-presence", adaptive_presence),
                    (scenario, "adaptive-no-token", tokenless_weights),
                ),
            )
        )
        found.append(
            Phase(
                "adaptive-unchecked",
                "Director:\n  CacheSortMethod: adaptive\n  CheckCachePresence: false\n",
                ((scenario, "adaptive-unchecked", unchecked_weights),),
            )
        )
    checks: list[Check] = []
    if "filtered" in selected:
        checks.append(("filtered", "routing", functools.partial(filtered_routing, name=name)))
        checks.append(("filtered", "listing", functools.partial(filtered_listing, name=name)))
    if "stat" in selected:
        checks.append(("stat", "single-checked", single_checked))
    if "response-hostnames" in selected:
        for header, case in (("Host", "host"), ("X-Forwarded-Host", "forwarded")):
            for hostname, kind in ((ORIGIN_HOSTNAME, "origin"), (CACHE_HOSTNAME, "cache")):
                check = functools.partial(response_hostname, header=header, hostname=hostname, kind=kind)
                checks.append(("response-hostnames", f"{case}-{kind}", check))
    if checks:
        found.append(Phase("combined", combined_text(name), tuple(checks)))
    return found


def directors_unrestartable(test: Test) -> str | None:
    """Return why the suite won't restart the directors here, or None."""
    if len(test.caches) < 2:
        return "the suite restarts the directors only under -p topo-multi-cache"
    return control.unavailable()


def run_restarted(test: Test, selected: Sequence[str], results: common.Results) -> None:
    """Restart the directors for each phase the selected scenarios need.

    For each, run its checks, and restore the directors; then wait until
    they route as before.

    Args:
        test: The Test.
        selected: The selected scenarios.
        results: The suite's results.
    """
    why = directors_unrestartable(test)
    name: str | None = None
    if not why and "filtered" in selected:
        name = cache_name(test, test.caches[-1])
    wanted = phases(selected, name)
    if why:
        for phase in wanted:
            for scenario, case, _ in phase.checks:
                suite.record(results, scenario, {case: (SKIP, why)})
        return
    banned = frozenset([test.caches[-1].host]) if name else frozenset[str]()
    for phase in wanted:
        print(f"\nRestarting the directors with:\n{phase.text}")
        suite.check_restarted(
            {director.host(u): {CONFIG_FILE: phase.text} for u in test.fed.directors},
            "the directors",
            {
                f"{scenario} {case}": functools.partial(check, test)
                for scenario, case, check in phase.checks
            },
            functools.partial(suite.record, results),
            ready=functools.partial(wait_named, test, banned if phase.name == "combined" else frozenset()),
            restored=None,
        )
    if wanted:
        why = wait_named(test)
        if why:
            warn(f"with the directors restored, {why}")


# --------------------------------------------------------------------------
# permitted-namespaces: a cache restricted to a namespace, which it then
# alone advertises (Cache.PermittedNamespaces; filterAdsBasedOnNamespace
# in Pelican's cache/advertise.go).


def permitted_namespace(test: Test) -> str | None:
    """Return the namespace `permitted-namespaces` leaves the last cache.

    It is /public, if exported, else another protected namespace; None if
    the shape exports only the suite's own (`-p origin-public` too, whose
    /public is the suite's own).
    """
    others = [
        ns for ns in ("public", *credentials.PROTECTED) if ns in test.fed.exports and ns != test.namespace
    ]
    return others[0] if others else None


def permitted_routing(test: Test, permitted: str, restricted: Server) -> str | None:
    """Return why the directors don't heed the restriction, or None.

    Each must name the restricted cache for the object in the permitted
    namespace, and not for the probe in the suite's own, for which it
    must still name every other cache.
    """
    others = [c for c in test.caches if c.host != restricted.host]
    for url in test.fed.directors:
        name = director.host(url)
        path = test.path_of("permitted", permitted)
        found = director.ask(url, "object", path, token=test.token_for(permitted))
        if found.status != 307:
            return f"{name}: {path}: {found.describe()}, not 307"
        if not found.names(restricted.host):
            return f"{name} does not name {restricted.svc} for /{permitted}"
        path = test.path_of("probe")
        found = director.ask(url, "object", path, token=test.token)
        if found.status != 307:
            return f"{name}: {path}: {found.describe()}, not 307"
        if found.names(restricted.host):
            return f"{name} names {restricted.svc} for /{test.namespace}"
        for other in others:
            if not found.names(other.host):
                return f"{name} does not name {other.svc} for /{test.namespace}"
    return None


def permitted_listing(test: Test, permitted: str, restricted: Server) -> tuple[str, str]:
    """Check how each director lists the restricted cache.

    Each director's list of servers must show the restricted cache with
    namespacePrefixes of the permitted namespace, and any under it, and
    no other.
    """
    problems: list[str] = []
    for url in test.fed.directors:
        what = director.host(url)
        answer, rows = listed_servers(url)
        mine = cache_row(rows, restricted)
        if mine is None:
            problems.append(
                f"{what}: {director.SERVERS} ({answer.describe()}) does not list {restricted.svc}"
            )
            continue
        prefixes = [str(p) for p in common.as_array(mine.get("namespacePrefixes")) or []]
        wrong = [
            p for p in prefixes if p.rstrip("/") != f"/{permitted}" and not p.startswith(f"/{permitted}/")
        ]
        if wrong or not prefixes:
            problems.append(f"{what} lists {restricted.svc} with namespacePrefixes {prefixes}")
    return outcome(problems, f"/{permitted} alone")


def permitted_direct(test: Test, permitted: str, restricted: Server) -> tuple[str, str]:
    """Check what the restricted cache serves, asked straight.

    It must serve the object in the permitted namespace, and refuse the
    probe in the suite's own: it may access only the permitted ones
    (Cache.PermittedNamespaces in Pelican's docs/parameters.yaml).
    """
    answer = web.request(
        "GET",
        f"{restricted.url}{test.path_of('permitted', permitted)}",
        token=test.token_for(permitted),
    )
    why = web.served(answer, test.data["permitted"])
    if why:
        return FAIL, f"/{permitted}: {why}"
    answer = web.request("GET", f"{restricted.url}{test.path_of('probe')}", token=test.token)
    if answer.status == 200:
        return FAIL, f"/{test.namespace}: served, though not permitted"
    return PASS, f"/{permitted} served; /{test.namespace} refused ({answer.describe()})"


def permitted_namespaces(test: Test, results: common.Results) -> None:
    """Run `permitted-namespaces`: restrict the last cache to one namespace.

    Restart it so restricted, check how the directors route and list it,
    and what it serves, and restore it; then wait until the directors
    route as before.

    Args:
        test: The Test.
        results: The suite's results.
    """
    scenario = "permitted-namespaces"
    why = directors_unrestartable(test)
    permitted = permitted_namespace(test)
    if not why and permitted is None:
        why = f"the shape exports no namespace beside /{test.namespace}"
    if why or permitted is None:
        for case in ("routing", "listing", "direct"):
            suite.record(results, scenario, {case: (SKIP, why or "")})
        return
    # On a pstore origin, /public's storage is /protected-a's.
    errors = stores.put_in_place(
        test.session,
        stores.unique(test.fed.origins),
        permitted,
        test.rel("permitted"),
        test.data["permitted"],
    )
    if errors:
        suite.record(results, scenario, (FAIL, f"putting the object in /{permitted}: {errors[0]}"))
        return
    restricted = test.caches[-1]
    text = f"Cache:\n  PermittedNamespaces:\n    - /{permitted}\n"
    print(f"\nRestarting {restricted.svc} with:\n{text}")
    found: list[str] = []

    def routed() -> bool:
        problem = permitted_routing(test, permitted, restricted)
        found[:] = [problem] if problem else []
        return problem is None

    try:
        with control.restart_with(restricted.svc, {CONFIG_FILE: text}):
            if not common.wait_for(routed, suite.SETTLE_WAIT, interval=5):
                routing = (FAIL, f"{suite.SETTLE_WAIT}s after the restart, {first(found)}")
            else:
                problems: list[str] = []
                for _ in range(PERMITTED_ASKS):
                    problem = permitted_routing(test, permitted, restricted)
                    if problem:
                        problems.append(problem)
                        break
                note = (
                    f"{restricted.svc} named for /{permitted} alone, in {PERMITTED_ASKS} asks per director"
                )
                routing = outcome(problems, note)
            suite.record(results, scenario, {"routing": routing})
            checks = {
                "listing": functools.partial(
                    permitted_listing, test, permitted=permitted, restricted=restricted
                ),
                "direct": functools.partial(
                    permitted_direct, test, permitted=permitted, restricted=restricted
                ),
            }
            suite.record(results, scenario, {case: suite.attempt(check) for case, check in checks.items()})
    except control.ControlError as e:
        suite.record(results, scenario, (FAIL, f"restarting {restricted.svc}, or restoring it: {e}"))
        warn(f"restarting {restricted.svc}, or restoring it: {e}")
    why = wait_named(test)
    if why:
        warn(f"with {restricted.svc} restored, {why}")


# --------------------------------------------------------------------------


# The scenarios that run without a restart, by name: each takes the Test,
# and returns its rows.
RUN: dict[str, Callable[[Test], list[CaseRow]]] = {
    "distance": distance,
    "stat": stat,
    "redirect": redirect,
    "expired-401": expired_401,
    "status-codes": status_codes,
}

# The scenarios whose every row needs a restart.
RESTARTED = ("random-and-adaptive", "filtered", "response-hostnames")


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, the restarts' last.

    The objects are put in place first, and removed afterward. `stat`'s
    `single-checked` row needs a restart only with one origin.
    `permitted-namespaces` restarts a cache, not the directors, and comes
    last.
    """
    test = Test(session)

    why = test.put({test.rel(name): test.data[name] for name in ("probe", "warm")})
    if why:
        for scenario in selected:
            suite.record(results, scenario, (FAIL, f"putting the suite's objects at every origin: {why}"))
        return
    print(f"Asking the directors about /{test.namespace}/{test.base}/\n")
    try:
        for name in selected:
            if name in RUN:
                suite.record(results, name, suite.attempt_cases(functools.partial(RUN[name], test)))
        later = [s for s in selected if s in RESTARTED]
        if "stat" in selected and len(test.origins) > 1:
            suite.record(results, "stat", {"single-checked": (SKIP, f"{len(test.origins)} origins")})
        elif "stat" in selected:
            later.append("stat")
        run_restarted(test, later, results)
        if "permitted-namespaces" in selected:
            permitted_namespaces(test, results)
    finally:
        test.clean("after the run")
