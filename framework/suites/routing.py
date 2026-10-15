"""How the directors choose where to send a client, and what they answer.

The suite covers in what order they send it, and what they put in the
redirect: sorting by declared location, at random, by adaptive weights,
and with servers filtered out; whether they ask the origins for an
object first; what Location and Link carry; and what they refuse, and
with what status.

  distance            the default sort: a client's X-Pelican-Coordinate,
                      against the servers' declared GeoLocation, decides
                      the order of the caches and of the origins; with
                      X-Pelican-Debug, the director says why
  stat                an object no origin holds: 404 where the director
                      asks the origins (several, or one with
                      Director.AssumePresenceAtSingleOrigin off), and
                      otherwise a redirect: with ?skipstat, without the
                      token it would ask with, and at one origin
  redirect            Location carries the token, from the header or
                      ?authz, X-Pelican-Timeout, and the other query
                      parameters, escaped, and serves alone; Link
                      carries no token, and its pri and depth;
                      X-Pelican-JobId; ?redirect=false
  expired-401         a token that expires before a client could follow
                      the redirect gets 401, with WWW-Authenticate; one
                      the director cannot read the expiry of does not
  status-codes        an unknown namespace, writes where no origin takes
                      them, conflicting parameters, a client too old,
                      OPTIONS, and the Server header
  random-and-adaptive Director.CacheSortMethod random orders the caches
                      both ways; adaptive weighs a cache that holds the
                      object above one that doesn't, unless it can't ask
                      (no token, or Director.CheckCachePresence off)
  filtered            a cache in Director.FilteredServers is named by no
                      director, which lists it as filtered
  response-hostnames  Director.OriginResponseHostnames and
                      CacheResponseHostnames: a bare path asked of such
                      a name, by Host or X-Forwarded-Host, goes to an
                      origin or a cache
  permitted-namespaces
                      a cache restricted to a namespace
                      (Cache.PermittedNamespaces) is named for that
                      namespace alone, and listed with it alone

Each service's own layer (framework/config.d/instance/) gives cache-0
and origin-0 a GeoLocation in Madison, and cache-1 and origin-1 one in
Lisbon, which the suite knows (GEO). The framework has no MaxMind
database, so a client that sends no coordinate is placed at random in
the contiguous US, and sorted toward the Madison servers, as their names
would sort them anyway; `distance` sends its own.

The other director settings need a restart: under `-p topo-multi-cache`,
and with a control helper, the suite restarts every director, as the
last of its configuration layers (control.restart_with()), four times:
with CacheSortMethod random; adaptive; adaptive with CheckCachePresence
off; and FilteredServers naming the last cache, with
AssumePresenceAtSingleOrigin off and response hostnames that no DNS
knows, sent as headers. Each time, and once restored, it waits until
every director names every server again. Elsewhere, what needs a restart
is skipped. `permitted-namespaces` restarts the last cache instead,
restricted to /public, or to another protected namespace, and waits
likewise.

The suite puts its objects at every origin, in the first protected
namespace, under data/routing/<run>/, and removes them afterward. It
puts them in place, and fills the last cache for `adaptive`, with WLCG
tokens; every request it asks the directors, which they pass on, carries
a token of the session's profile.
"""

import contextlib
import functools
import json
import math
import os
import time
import traceback
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

from testlib import common, control, credentials, director, jwt, metrics, stores, web
from testlib.common import first, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "distance": (
        "a client's X-Pelican-Coordinate and the servers' declared locations decide the order;"
        " X-Pelican-Debug says why"
    ),
    "stat": "an object no origin holds: 404 where the director asks the origins, else 307",
    "redirect": (
        "Location carries the token, timeout, and parameters, and serves alone; Link carries"
        " no token"
    ),
    "expired-401": "a token about to expire gets 401 and WWW-Authenticate; an unreadable one 307",
    "status-codes": "unknown namespaces, unwritable writes, bad parameters, old clients, OPTIONS",
    "random-and-adaptive": (
        "random orders the caches both ways; adaptive weighs a cache's copy, when it can ask"
    ),
    "filtered": "a cache in Director.FilteredServers is named by no director",
    "response-hostnames": "a bare path asked of a response hostname goes to an origin or a cache",
    "permitted-namespaces": (
        "a cache restricted to a namespace (Cache.PermittedNamespaces) is named for it alone"
    ),
}

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# The routes a client asks a director by, for a cache and for an origin.
OBJECT = "/api/v1.0/director/object"
ORIGIN = "/api/v1.0/director/origin"
# The directors' list of servers.
SERVERS = "/api/v1.0/director_ui/servers"

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
# How long after starting a director answers 429, not 404, for a
# namespace no origin exports (inStartupSequence), in seconds, and how
# near that a row takes either.
STARTUP_WINDOW = 300
STARTUP_SLACK = 10

# The oldest client a director serves (minClientVersion).
OLD_CLIENT = "pelican-client/6.9.0"
NEW_ENOUGH_CLIENT = "pelican-client/7.0.0"

# A query parameter a director knows nothing of, with a value that a URL
# must escape.
EXTRA_PARAM = "routing-test"
EXTRA_VALUE = "a b&c=d/é?#%"

# The response hostnames of the combined restart: names no DNS knows,
# sent only in headers.
ORIGIN_HOSTNAME = "origins.routing.invalid"
CACHE_HOSTNAME = "caches.routing.invalid"

# How many times `random` asks each director, and `filtered` each route.
RANDOM_ASKS = 20
FILTERED_ASKS = 5
# How long after a restart every director may take to name every server
# again: each learns of a server from its next ad, within
# Server.AdvertisementInterval (30 seconds here).
SETTLE_WAIT = 150
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


class Test:
    """The federation, the suite's objects, and what the scenarios share.

    Attributes:
        namespace: The first protected namespace, or /public where the
            origins export only that (`-p origin-public`), whose token
            every server ignores.
        caps: The namespace's capabilities.
        token: The namespace's `server` token, of the session's profile.
        setup: The namespace's `server` token, of the WLCG profile, for
            putting objects in place, and filling a cache.
        base: The suite's collection, below the namespace.
        data: The contents of the objects that the suite puts, by name.
        objects: Each object's path below the namespace, by name: the
            probe, which every origin holds; `warm`, which every origin
            holds, and only `adaptive` reads, through the last cache;
            `missing`, which none does; and `permitted`, which only
            `permitted-namespaces` puts, in another namespace.
        caches: The federated caches.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.namespace = credentials.probe_namespace(self.fed)
        self.caps = self.fed.exports[self.namespace]
        self.token = session.token(self.namespace)
        self.setup = session.setup_token(self.namespace)
        self.base = f"data/routing/{self.run}"
        self.data = {
            "probe": os.urandom(4096),
            "warm": os.urandom(8192),
            "permitted": os.urandom(4096),
        }
        self.objects = {
            name: f"{self.base}/{name}" for name in ("probe", "warm", "missing", "permitted")
        }
        self.caches = [Server(c.svc, c.url, c.kind) for c in self.fed.federated_caches]
        self.origins = [Server(o.svc, o.url, "origin") for o in self.fed.origins]

    def path(self, name: str, namespace: Optional[str] = None) -> str:
        """Return the federation path of object name.

        Args:
            name: `probe`, `warm`, `missing`, or `permitted`.
            namespace: Its namespace, if not the suite's own.
        """
        return f"/{namespace or self.namespace}/{self.objects[name]}"

    def token_for(self, namespace: str) -> Optional[str]:
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


def query_of(location: str) -> dict[str, list[str]]:
    """Return the query parameters of URL location, blank values kept."""
    return parse_qs(urlsplit(location).query, keep_blank_values=True)


def where(location: str, servers: Sequence[Server]) -> Optional[Server]:
    """Return which of servers URL location is at, or None."""
    return next((s for s in servers if location.startswith(s.url + "/")), None)


def debug_info(answer: web.Response) -> dict[str, Any]:
    """Return the decision a director explains in answer to X-Pelican-Debug.

    It is answer's JSON body (server_structs.RedirectInfo), or {}.
    """
    try:
        return common.as_object(json.loads(answer.body)) or {}
    except ValueError:
        return {}


def servers_info(info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return what a debug answer says of each server it sorted, by host."""
    found = common.as_object(info.get("serversInfo")) or {}
    return {director.host(url): common.as_object(v) or {} for url, v in found.items()}


def weight(server: Mapping[str, Any], name: str) -> float:
    """Return a server's weight in a debug answer; NaN if it has none.

    name names the weight, e.g. distanceWeight.
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
    token: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    method: str = "GET",
) -> web.Response:
    """Send method to url, presenting token, with headers."""
    return web.request(method, url, token=token, headers=headers)


def outcome(problems: Sequence[str], note: str) -> tuple[str, str]:
    """Return FAIL with the first of problems, or PASS with note."""
    return (FAIL, first(problems)) if problems else (PASS, note)


def guarded(check: Callable[[], list[Row]]) -> list[Row]:
    """Return check()'s rows; one failing row if it raised."""
    try:
        return check()
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]


def no_director(fed: common.Federation) -> Optional[str]:
    """Return why the shape has no director to ask, or None."""
    return "a standalone origin has no director" if fed.standalone else None


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test: no director."""
    return no_director(fed)


# --------------------------------------------------------------------------
# distance


def expected_order(servers: Sequence[Server], client: tuple[float, float]) -> list[Server]:
    """Return servers, nearest client first, by their declared locations."""
    return sorted(servers, key=lambda s: -distance_weight(client, GEO[s.svc]))


def order_problems(
    test: Test, route: str, servers: Sequence[Server], client: tuple[float, float]
) -> list[str]:
    """Return what is wrong with where each director sends a client.

    The client is at client, and asks for the probe by route: Location
    must be the nearest of servers, and Link list them nearest first.
    """
    want = [s.host for s in expected_order(servers, client)]
    hosts = {s.host for s in servers}
    problems: list[str] = []
    for url in test.fed.directors:
        answer = ask(f"{url}{route}{test.path('probe')}", test.token, coordinate_header(client))
        what = f"{director.host(url)} ({route.rsplit('/', 1)[-1]}, near {client})"
        listed = [director.host(u) for u, _ in link_entries(answer.header("Link"))]
        listed = [h for h in listed if h in hosts]
        if answer.status != 307:
            problems.append(f"{what}: {answer.describe()}, not 307")
        elif director.host(answer.header("Location")) != want[0]:
            problems.append(
                f"{what}: Location is at {answer.header('Location')}, not {want[0]}"
            )
        elif listed != want:
            problems.append(f"{what}: Link lists {listed}, not {want}")
    return problems


def debug_problems(
    test: Test, route: str, servers: Sequence[Server], client: tuple[float, float]
) -> list[str]:
    """Return what is wrong with how each director explains a distance sort.

    The sort is for a client at client, by route. With X-Pelican-Debug,
    each must answer a 307 to the nearest of servers, whose body names the
    sort, the client's coordinate as it declared it, and each server's
    declared coordinate and its weight.
    """
    nearest = expected_order(servers, client)[0]
    problems: list[str] = []
    for url in test.fed.directors:
        headers = {**coordinate_header(client), "X-Pelican-Debug": "true"}
        answer = ask(f"{url}{route}{test.path('probe')}", test.token, headers)
        what = f"{director.host(url)} ({route.rsplit('/', 1)[-1]}, debug)"
        info = debug_info(answer)
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
                problems.append(
                    f"{what}: {server.svc}'s distanceWeight is {got}, not {want:.6g}"
                )
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
            answer = ask(f"{url}{OBJECT}{test.path('probe')}", test.token, headers)
            what = f"{director.host(url)} ({value!r})"
            client_at = coordinate(common.as_object(debug_info(answer).get("clientInfo")) or {})
            if answer.status != 307:
                problems.append(f"{what}: {answer.describe()}, not 307")
            elif where(answer.header("Location"), test.caches) is None:
                problems.append(f"{what}: Location is at {answer.header('Location')}, no cache")
            elif client_at.get("Source") == "declared":
                problems.append(f"{what}: took the header as the client's coordinate")
    return problems


def distance(test: Test) -> list[Row]:
    """Check that the directors sort the caches and origins by distance.

    The sort is the default. Each director must order them as a client
    near Madrid, and one near Madison, says it is (see order_problems()),
    and explain why with X-Pelican-Debug (see debug_problems()); and must
    route a client whose coordinate it can't read (see
    malformed_problems()).
    """
    rows: list[Row] = []
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
        problems = [
            p
            for c in (NEAR_MADRID, NEAR_MADISON)
            for p in order_problems(test, route, servers, c)
        ]
        near = ", ".join(
            f"{c}: {expected_order(servers, c)[0].svc}" for c in (NEAR_MADRID, NEAR_MADISON)
        )
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
    token (a native origin answers 401), and it takes a refusal for
    presence, as it must when it cannot tell (generateAvailabilityMaps).
    """
    return "DirectReads" not in test.fed.exports[namespace]


def stat_problems(
    test: Test, namespaces: Sequence[str], query: str, with_token: bool, found: bool
) -> list[str]:
    """Return what is wrong with each director's answers for `missing`.

    The answers, for an origin, are to a GET and a HEAD of the missing
    object, in each of namespaces, with query, presenting the
    namespace's token if with_token. If found, each must be a 307 to an
    origin; otherwise 404, and a GET's body saying no origin had it, but
    where the director cannot ask (unasked()), a 307 too.
    """
    problems: list[str] = []
    for url in test.fed.directors:
        for ns in namespaces:
            token = test.token_for(ns) if with_token else None
            target = f"{url}{ORIGIN}{test.path('missing', ns)}" + (f"?{query}" if query else "")
            for method in ("GET", "HEAD"):
                answer = ask(target, token, method=method)
                what = f"{director.host(url)}: {method} /{ns}{'?' + query if query else ''}"
                redirected = (
                    answer.status == 307
                    and where(answer.header("Location"), test.origins) is not None
                )
                if found or (answer.status == 307 and unasked(test, ns)):
                    if not redirected:
                        problems.append(f"{what}: {answer.describe()}, not 307 to an origin")
                elif answer.status != 404:
                    problems.append(f"{what}: {answer.describe()}, not 404")
                elif method == "GET" and b"possession of the object" not in answer.body:
                    problems.append(f"{what}: 404, but its body is {answer.body[:200]!r}")
    return problems


def stat(test: Test) -> list[Row]:
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
    rows: list[Row] = []
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
        return f"{self.url}{prefix}{self.test.path('probe')}"

    def note(self, case: str, problem: str) -> None:
        """Record a problem of case."""
        asked = f"{director.host(self.url)} ({self.route})"
        self.problems.setdefault(case, []).append(f"{asked}: {problem}")

    def redirect(self, case: str, answer: web.Response) -> Optional[dict[str, list[str]]]:
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
            credentials.decide(credentials.ALLOW, test.caps, "get", direct=at_origin)
            == credentials.ALLOW
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
        depth = str(len(self.test.objects["probe"].split("/")))
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
            self.note(
                "timeout", f"Location's pelican.timeout is {found.get('pelican.timeout')}"
            )
        query = f"skipstat&{EXTRA_PARAM}={quote(EXTRA_VALUE, safe='')}"
        found = self.redirect("params", ask(f"{self.target}?{query}", token))
        if found is not None:
            if found.get("skipstat") != [""]:
                self.note("params", f"Location's skipstat is {found.get('skipstat')}")
            if found.get(EXTRA_PARAM) != [EXTRA_VALUE]:
                self.note("params", f"Location's {EXTRA_PARAM} is {found.get(EXTRA_PARAM)}")
        if "DirectReads" in test.caps:
            found = self.redirect("params", ask(f"{self.target}?directread", token))
            if found is not None and found.get("directread") != [""]:
                self.note("params", f"Location's directread is {found.get('directread')}")

        # X-Pelican-JobId comes back, if it is a UUID; otherwise one of
        # the director's.
        sent = str(uuid.uuid4())
        answer = ask(self.target, token, {"X-Pelican-JobId": sent})
        if answer.header("X-Pelican-JobId") != sent:
            self.note(
                "job-id", f"X-Pelican-JobId {answer.header('X-Pelican-JobId')!r}, not {sent}"
            )
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


def redirect(test: Test) -> list[Row]:
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


def signed(test: Test, issued: int, expires: Optional[int]) -> str:
    """Return a token like the namespace's `server` token.

    It is signed with the origins' key, issued at issued and expiring at
    expires (None: no exp claim). The director reads it without checking
    its signature.
    """
    claims = credentials.server_claims(test.fed, test.namespace, profile=test.session.profile)
    claims["iat"] = claims["nbf"] = issued
    if expires is None:
        del claims["exp"]
    else:
        claims["exp"] = expires
    key = test.fed.origin_key
    return jwt.encode(jwt.header(jwt.algorithm(key), jwt.key_id(key)), claims, key=key)


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


def expired_401(test: Test) -> list[Row]:
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
    rows: list[Row] = []
    targets = [("object", OBJECT), ("origin", ORIGIN), ("bare", "")]
    for case, make, in_query, refused in EXPIRY_CASES:
        problems: list[str] = []
        for url in test.fed.directors:
            for route, prefix in targets:
                token = make(test, int(time.time()))
                target = f"{url}{prefix}{test.path('probe')}"
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


def started_ago(url: str) -> Optional[float]:
    """Return how long ago the director at url started, in seconds.

    That is by its process_start_time_seconds; None if it won't say.
    """
    found = metrics.fetch(url)
    if found is None or not found.has("process_start_time_seconds"):
        return None
    return time.time() - found.value("process_start_time_seconds")


def unknown_namespace(test: Test) -> tuple[str, str]:
    """Check each director's answer for a namespace no origin exports.

    Each must answer 404, but within 5 minutes of starting, 429, since it
    may not have heard from every origin yet (inStartupSequence).
    """
    path = f"/routing-nowhere-{test.run}/probe"
    problems: list[str] = []
    unsure: list[str] = []
    for url in test.fed.directors:
        before = started_ago(url)
        answer = ask(f"{url}{OBJECT}{path}", test.token)
        after = started_ago(url)
        what = director.host(url)
        if before is None or after is None:
            unsure.append(f"{what}: its /metrics has no process_start_time_seconds")
            continue
        if after < STARTUP_WINDOW - STARTUP_SLACK:
            want: tuple[int, ...] = (429,)
        elif before > STARTUP_WINDOW + STARTUP_SLACK:
            want = (404,)
        else:
            want = (404, 429)
        if answer.status not in want:
            problems.append(
                f"{what}, started {after:.0f}s ago: {answer.describe()}, not {want}"
            )
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
        token: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
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

    def bad_requests(self) -> tuple[str, str]:
        """Check that conflicting parameters and too old a client get 400.

        ?directread with ?prefercached conflict, and a client older than
        7.0.0 is too old (minClientVersion). A 7.0.0 client gets 307.
        """
        problems: list[str] = []
        token = self.test.token
        for url in self.test.fed.directors:
            target = f"{url}{OBJECT}{self.test.path('probe')}"
            name = director.host(url)
            problems += self.expect(
                f"{name}: ?directread&prefercached",
                400,
                f"{target}?directread&prefercached",
                token=token,
            )
            for agent, want in ((OLD_CLIENT, 400), (NEW_ENOUGH_CLIENT, 307)):
                problems += self.expect(
                    f"{name}: {agent}", want, target, token=token, headers={"User-Agent": agent}
                )
        return outcome(problems, f"400, and {NEW_ENOUGH_CLIENT} 307")

    def options(self) -> tuple[str, str]:
        """Check that a bare OPTIONS gets 200, with Allow and CORS headers.

        ShortcutMiddleware answers it.
        """
        problems: list[str] = []
        for url in self.test.fed.directors:
            what = f"{director.host(url)}: OPTIONS"
            answer = self.ask(what, f"{url}{self.test.path('probe')}", method="OPTIONS")
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

    def servers(self) -> list[Row]:
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


def status_codes(test: Test) -> list[Row]:
    """Check what each director refuses, and how; and its Server headers.

    See Statuses and unknown_namespace().
    """
    checks = Statuses(test, [])
    rows: list[Row] = [("unknown-namespace", *unknown_namespace(test))]
    rows.append(("writes", *checks.writes()))
    rows.append(("bad-requests", *checks.bad_requests()))
    rows.append(("options", *checks.options()))
    for url in test.fed.directors:
        name = director.host(url)
        checks.ask(f"{name}: object route", f"{url}{OBJECT}{test.path('probe')}", test.token)
        checks.ask(f"{name}: bare path", f"{url}{test.path('probe')}", test.token)
    return rows + checks.servers()


# --------------------------------------------------------------------------
# The restarts: random-and-adaptive, filtered, response-hostnames, and
# stat's single-checked.


def serving(test: Test, filtered: frozenset[str] = frozenset()) -> Optional[str]:
    """Return why the directors don't route the probe as before, or None.

    Each must name every federated cache, but those filtered (hosts), and
    every origin.
    """
    path = test.path("probe")
    for url in test.fed.directors:
        name = director.host(url)
        found = director.ask(url, "object", path, token=test.token)
        for cache in test.caches:
            if cache.host not in filtered and not found.names(cache.host):
                return f"{name} does not name {cache.svc}: {found.describe()}"
        found = director.ask(url, "origin", path, token=test.token)
        for origin in test.origins:
            if not found.names(origin.host):
                return f"{name} does not name {origin.svc}: {found.describe()}"
    return None


def settle(test: Test, filtered: frozenset[str] = frozenset()) -> Optional[str]:
    """Wait until the directors route the probe as before.

    Returns:
        Why they don't within SETTLE_WAIT seconds (see serving()), or
        None.
    """
    found: list[str] = []

    def served() -> bool:
        why = serving(test, filtered)
        found[:] = [why] if why else []
        return why is None

    if common.wait_for(served, SETTLE_WAIT, interval=5):
        return None
    return f"after {SETTLE_WAIT}s, {first(found)}"


@contextlib.contextmanager
def directors_with(test: Test, text: str) -> Generator[None, None, None]:
    """Restart every director with text as its last configuration layer.

    Afterward, even on failure, restore and restart it.
    """
    with contextlib.ExitStack() as stack:
        for url in test.fed.directors:
            svc = director.host(url)
            stack.enter_context(control.restart_with(svc, {CONFIG_FILE: text}))
        yield


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
            answer = ask(f"{url}{OBJECT}{test.path('probe')}", test.token)
            listed = [director.host(u) for u, _ in link_entries(answer.header("Link"))]
            listed = [h for h in listed if h in hosts]
            if answer.status != 307 or set(listed) != hosts:
                problems.append(f"{name}: {answer.describe()}, listing {listed}")
                break
            firsts[listed[0]] = firsts.get(listed[0], 0) + 1
        else:
            if set(firsts) != hosts:
                problems.append(f"{name}: in {RANDOM_ASKS} asks, first only {firsts}")
    return outcome(problems, f"each cache first, in {RANDOM_ASKS} asks per director")


def availability(
    test: Test, token: Optional[str]
) -> tuple[list[str], dict[str, dict[str, float]]]:
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
        answer = ask(f"{url}{OBJECT}{test.path('warm')}", token, headers)
        info = debug_info(answer)
        if answer.status != 307 or not info:
            problems.append(f"{name}: {answer.describe()}, not a 307 with a JSON body")
            continue
        if info.get("directorSortMethod") != "adaptive":
            problems.append(f"{name}: directorSortMethod is {info.get('directorSortMethod')!r}")
            continue
        servers = servers_info(info)
        weights = {
            c.host: weight(servers.get(c.host, {}), "availabilityWeight") for c in test.caches
        }
        missing = [h for h, w in weights.items() if math.isnan(w)]
        if missing:
            problems.append(f"{name}: no availabilityWeight for {missing}: {sorted(servers)}")
        found[name] = weights
    return problems, found


def equal_weights(test: Test, token: Optional[str]) -> tuple[str, str]:
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


def warm_last_cache(test: Test) -> Optional[str]:
    """Read `warm` through the last cache alone, and wait for its Age.

    Wait until the cache gives the copy an Age of a second or more, by
    which a director takes it as held there (generateAvailabilityMaps).

    Returns:
        Why that failed, or None.
    """
    cache = test.caches[-1]
    url = f"{cache.url}{test.path('warm')}"
    answer = web.request("GET", url, token=test.setup)
    if answer.status != 200 or answer.body != test.data["warm"]:
        return f"reading `warm` through {cache.svc}: {answer.describe()}"

    def aged() -> bool:
        age = web.request("HEAD", url, token=test.setup).header("Age")
        return age.isdigit() and int(age) >= 1

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


def cache_name(test: Test, cache: Server) -> Optional[str]:
    """Return the name that the first director knows cache by.

    Returns:
        The name, from its list of servers; None if it doesn't list it.
    """
    answer = web.request("GET", f"{test.fed.directors[0]}{SERVERS}")
    try:
        rows = common.as_array(json.loads(answer.body)) or []
    except ValueError:
        return None
    for row in (common.as_object(r) or {} for r in rows):
        if (
            row.get("type") == "Cache"
            and director.host(str(row.get("webUrl", ""))) == cache.host
        ):
            return str(row.get("name") or "") or None
    return None


def unlisted(test: Test, name: Optional[str]) -> Optional[str]:
    """Return why `filtered` can't check the last cache, or None.

    It can't if the first director did not list it, so that the suite
    knows no name to filter it by.
    """
    if name is not None:
        return None
    return f"{director.host(test.fed.directors[0])} does not list {test.caches[-1].svc}"


def filtered_routing(test: Test, name: Optional[str]) -> tuple[str, str]:
    """Check that no director names a cache in FilteredServers.

    With FilteredServers naming the last cache (name), no director may
    name it, by the object route or the bare path, asked FILTERED_ASKS
    times each; and each must still name the other caches.
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
                answer = ask(f"{url}{prefix}{test.path('probe')}", test.token)
                listed = [director.host(u) for u, _ in link_entries(answer.header("Link"))]
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


def filtered_listing(test: Test, name: Optional[str]) -> tuple[str, str]:
    """Check that each director lists the last cache as filtered.

    Each director's list of servers must show the last cache, by name,
    as filtered by its configuration: `filtered` true, `filteredType`
    `permFiltered`.
    """
    why = unlisted(test, name)
    if why:
        return FAIL, why
    problems: list[str] = []
    for url in test.fed.directors:
        answer = web.request("GET", f"{url}{SERVERS}")
        try:
            rows = common.as_array(json.loads(answer.body)) or []
        except ValueError:
            rows = []
        mine = [r for r in (common.as_object(r) or {} for r in rows) if r.get("name") == name]
        what = director.host(url)
        if not mine:
            problems.append(f"{what}: {SERVERS} ({answer.describe()}) does not list {name!r}")
        elif (
            mine[0].get("filtered") is not True or mine[0].get("filteredType") != "permFiltered"
        ):
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
        answer = ask(f"{url}{test.path('probe')}", test.token, {header: hostname})
        what = f"{director.host(url)} ({header}: {hostname})"
        if answer.status != 307:
            problems.append(f"{what}: {answer.describe()}, not 307")
        elif where(answer.header("Location"), servers) is None:
            problems.append(f"{what}: Location is at {answer.header('Location')}, no {kind}")
    note = "to an origin" if kind == "origin" else "to a cache"
    if kind == test.fed.director_default_response:
        note += ", as without the setting"
    return outcome(problems, note)


def combined_text(name: Optional[str]) -> str:
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


def phases(selected: Sequence[str], name: Optional[str]) -> list[Phase]:
    """Return the restarts that the selected scenarios need, in order.

    name is the last cache's, for `filtered`, if known.
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
                check = functools.partial(
                    response_hostname, header=header, hostname=hostname, kind=kind
                )
                checks.append(("response-hostnames", f"{case}-{kind}", check))
    if checks:
        found.append(Phase("combined", combined_text(name), tuple(checks)))
    return found


def unrestartable(test: Test) -> Optional[str]:
    """Return why the suite won't restart the directors here, or None."""
    if len(test.caches) < 2:
        return "the suite restarts the directors only under -p topo-multi-cache"
    return control.unavailable()


def checked(check: Callable[[Test], tuple[str, str]], test: Test) -> tuple[str, str]:
    """Return check(test); FAIL if it raised (see guarded())."""
    rows = guarded(lambda: [("", *check(test))])
    return rows[0][1], rows[0][2]


def restarted(
    test: Test, selected: Sequence[str], add: Callable[[str, str, str, str], None]
) -> None:
    """Restart the directors for each phase the selected scenarios need.

    For each, run its checks, and restore the directors; then wait until
    they route as before. add() records a row: its scenario, case,
    status, and note.
    """
    why = unrestartable(test)
    name: Optional[str] = None
    if not why and "filtered" in selected:
        name = cache_name(test, test.caches[-1])
    wanted = phases(selected, name)
    if why:
        for phase in wanted:
            for scenario, case, _ in phase.checks:
                add(scenario, case, SKIP, why)
        return
    banned = frozenset([test.caches[-1].host]) if name else frozenset[str]()
    for phase in wanted:
        print(f"\nRestarting the directors with:\n{phase.text}")
        done: set[tuple[str, str]] = set()
        try:
            with directors_with(test, phase.text):
                why = settle(test, banned if phase.name == "combined" else frozenset())
                for scenario, case, check in phase.checks:
                    if why:
                        add(scenario, case, FAIL, f"after the restart, {why}")
                    else:
                        add(scenario, case, *checked(check, test))
                    done.add((scenario, case))
        except control.ControlError as e:
            # Restarting, or restoring, failed: what is left unrun fails.
            for scenario, case, _ in phase.checks:
                if (scenario, case) not in done:
                    add(scenario, case, FAIL, f"restarting the directors: {e}")
            warn(f"restarting the directors, or restoring them: {e}")
    if wanted:
        why = settle(test)
        if why:
            warn(f"with the directors restored, {why}")


# --------------------------------------------------------------------------
# permitted-namespaces: a cache restricted to a namespace, which it then
# alone advertises (Cache.PermittedNamespaces; filterAdsBasedOnNamespace
# in Pelican's cache/advertise.go).

# How many times `permitted-namespaces` asks each director.
PERMITTED_ASKS = FILTERED_ASKS


def permitted_namespace(test: Test) -> Optional[str]:
    """Return the namespace `permitted-namespaces` leaves the last cache.

    It is /public, if exported, else another protected namespace; None if
    the shape exports only the suite's own (`-p origin-public` too, whose
    /public is the suite's own).
    """
    others = [
        ns
        for ns in ("public", *credentials.PROTECTED)
        if ns in test.fed.exports and ns != test.namespace
    ]
    return others[0] if others else None


def names_cache(answer: web.Response, cache: Server) -> bool:
    """Return whether a director's redirect names cache.

    It does if it sends the client to cache, or lists it in Link.
    """
    listed = [director.host(u) for u, _ in link_entries(answer.header("Link"))]
    return cache.host in listed or director.host(answer.header("Location")) == cache.host


def permitted_routing(test: Test, permitted: str, restricted: Server) -> Optional[str]:
    """Return why the directors don't heed the restriction, or None.

    Each must name the restricted cache for the object in the permitted
    namespace, and not for the probe in the suite's own, for which it
    must still name every other cache.
    """
    others = [c for c in test.caches if c.host != restricted.host]
    for url in test.fed.directors:
        name = director.host(url)
        path = test.path("permitted", permitted)
        answer = ask(f"{url}{OBJECT}{path}", test.token_for(permitted))
        if answer.status != 307:
            return f"{name}: {path}: {answer.describe()}, not 307"
        if not names_cache(answer, restricted):
            return f"{name} does not name {restricted.svc} for /{permitted}"
        path = test.path("probe")
        answer = ask(f"{url}{OBJECT}{path}", test.token)
        if answer.status != 307:
            return f"{name}: {path}: {answer.describe()}, not 307"
        if names_cache(answer, restricted):
            return f"{name} names {restricted.svc} for /{test.namespace}"
        for other in others:
            if not names_cache(answer, other):
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
        answer = web.request("GET", f"{url}{SERVERS}")
        try:
            rows = common.as_array(json.loads(answer.body)) or []
        except ValueError:
            rows = []
        mine = [
            r
            for r in (common.as_object(r) or {} for r in rows)
            if r.get("type") == "Cache"
            and director.host(str(r.get("webUrl", ""))) == restricted.host
        ]
        if not mine:
            problems.append(
                f"{what}: {SERVERS} ({answer.describe()}) does not list {restricted.svc}"
            )
            continue
        prefixes = [str(p) for p in common.as_array(mine[0].get("namespacePrefixes")) or []]
        wrong = [
            p
            for p in prefixes
            if p.rstrip("/") != f"/{permitted}" and not p.startswith(f"/{permitted}/")
        ]
        if wrong or not prefixes:
            problems.append(f"{what} lists {restricted.svc} with namespacePrefixes {prefixes}")
    return outcome(problems, f"/{permitted} alone")


def permitted_direct(test: Test, permitted: str, restricted: Server) -> tuple[str, str]:
    """Check what the restricted cache serves, asked straight.

    It must serve the object in the permitted namespace; of the probe in
    the suite's own, it may serve it or refuse, but never other bytes.
    """
    data = test.data["permitted"]
    answer = web.request(
        "GET",
        f"{restricted.url}{test.path('permitted', permitted)}",
        token=test.token_for(permitted),
    )
    if answer.status != 200 or answer.body != data:
        return FAIL, f"/{permitted}: {answer.describe()}" + (
            ", but not the object" if answer.status == 200 else ""
        )
    answer = web.request("GET", f"{restricted.url}{test.path('probe')}", token=test.token)
    if answer.status == 200 and answer.body != test.data["probe"]:
        return FAIL, f"/{test.namespace}: HTTP 200, but not the object"
    said = "served" if answer.status == 200 else f"refused ({answer.describe()})"
    return PASS, f"/{permitted} served; /{test.namespace} {said}"


def put_permitted(test: Test, permitted: str) -> Optional[str]:
    """Put `permitted` in the permitted namespace's storage at every origin.

    On a pstore origin, /public's storage is /protected-a's.

    Returns:
        Why that failed, or None.
    """
    target = test.fed.storage_namespace(permitted)
    token = test.session.setup_token(target)
    caps = test.fed.exports[target]
    rel = test.objects["permitted"]
    for origin in stores.unique(test.fed.origins):
        errors = stores.make_dirs([origin], target, test.base, token)
        if errors:
            return errors[0]
        why = stores.seed(origin, target, rel, test.data["permitted"], token, caps)
        if why:
            return why
    return None


def permitted_namespaces(test: Test, add: Callable[[str, str, str, str], None]) -> None:
    """Run `permitted-namespaces`: restrict the last cache to one namespace.

    Restart it so restricted, check how the directors route and list it,
    and what it serves, and restore it; then wait until the directors
    route as before. add() records a row: its scenario, case, status, and
    note.
    """
    scenario = "permitted-namespaces"
    why = unrestartable(test)
    permitted = permitted_namespace(test)
    if not why and permitted is None:
        why = f"the shape exports no namespace beside /{test.namespace}"
    if why or permitted is None:
        for case in ("routing", "listing", "direct"):
            add(scenario, case, SKIP, why or "")
        return
    why = put_permitted(test, permitted)
    if why:
        add(scenario, "", FAIL, f"putting the object in /{permitted}: {why}")
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
            if not common.wait_for(routed, SETTLE_WAIT, interval=5):
                add(
                    scenario,
                    "routing",
                    FAIL,
                    f"{SETTLE_WAIT}s after the restart, {first(found)}",
                )
            else:
                problems: list[str] = []
                for _ in range(PERMITTED_ASKS):
                    problem = permitted_routing(test, permitted, restricted)
                    if problem:
                        problems.append(problem)
                        break
                note = f"{restricted.svc} named for /{permitted} alone, in {PERMITTED_ASKS} asks per director"
                add(scenario, "routing", *outcome(problems, note))
            add(
                scenario,
                "listing",
                *checked(
                    functools.partial(
                        permitted_listing, permitted=permitted, restricted=restricted
                    ),
                    test,
                ),
            )
            add(
                scenario,
                "direct",
                *checked(
                    functools.partial(
                        permitted_direct, permitted=permitted, restricted=restricted
                    ),
                    test,
                ),
            )
    except control.ControlError as e:
        add(scenario, "", FAIL, f"restarting {restricted.svc}, or restoring it: {e}")
        warn(f"restarting {restricted.svc}, or restoring it: {e}")
    why = settle(test)
    if why:
        warn(f"with {restricted.svc} restored, {why}")


# --------------------------------------------------------------------------


# The scenarios that run without a restart, by name: each takes the Test,
# and returns its rows.
RUN: dict[str, Callable[[Test], list[Row]]] = {
    "distance": distance,
    "stat": stat,
    "redirect": redirect,
    "expired-401": expired_401,
    "status-codes": status_codes,
}

# The scenarios whose every row needs a restart.
RESTARTED = ("random-and-adaptive", "filtered", "response-hostnames")


def put_objects(test: Test) -> Optional[str]:
    """Put the probe and `warm` at every origin.

    Returns:
        Why that failed, or None.
    """
    errors = stores.make_dirs(test.fed.origins, test.namespace, test.base, test.setup)
    for name in ("probe", "warm"):
        for origin in stores.unique(test.fed.origins):
            if errors:
                return errors[0]
            error = stores.seed(
                origin,
                test.namespace,
                test.objects[name],
                test.data[name],
                test.setup,
                test.caps,
            )
            if error:
                errors.append(error)
    return errors[0] if errors else None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, the restarts' last.

    The objects are put in place first, and removed afterward. `stat`'s
    `single-checked` row needs a restart only with one origin.
    `permitted-namespaces` restarts a cache, not the directors, and comes
    last.
    """
    test = Test(session)

    def add(scenario: str, case: str, status: str, note: str) -> None:
        results.add(f"{scenario} {case}".strip(), status, note)

    why = put_objects(test)
    if why:
        for scenario in selected:
            add(scenario, "", FAIL, f"putting the suite's objects at every origin: {why}")
        return
    print(f"Asking the directors about /{test.namespace}/{test.base}/\n")
    try:
        for name in selected:
            if name in RUN:
                for case, status, note in guarded(functools.partial(RUN[name], test)):
                    add(name, case, status, note)
        later = [s for s in selected if s in RESTARTED]
        if "stat" in selected and len(test.origins) > 1:
            add("stat", "single-checked", SKIP, f"{len(test.origins)} origins")
        elif "stat" in selected:
            later.append("stat")
        restarted(test, later, add)
        if "permitted-namespaces" in selected:
            permitted_namespaces(test, add)
    finally:
        errors = stores.empty_tree_everywhere(
            session.fed.origins, session.fed.exports, "data/routing", session.setup_tokens()
        )
        if errors:
            warn(f"could not remove the suite's objects: {first(errors)}")
