"""What the directors publish about the federation, and whether it is
true of the shape: their lists of servers and of namespaces, the
namespace a path falls in, their health tests of each server and the
file they serve the caches for them, their stat of an object at the
origins, their metrics, their comparison of the federation's metadata,
and the ads they forward to one another.

  servers           director_ui's list of servers, whole, by type, and
                    one at a time, with each one's namespaces; its list
                    of namespaces; the support contact; and the list of
                    directors
  namespaces        listNamespaces, and its deprecated first version;
                    the namespace a path falls in
  health            each director's health test of each origin and
                    cache: the status it lists, its count of test runs,
                    and each server's own view of it; and a test file
                    read through each cache
  health-test-file  the test file a director serves the caches, by GET,
                    HEAD, and PROPFIND; with a timestamp that is none,
                    and in a directory whose name XML must escape
  origin-stat       which origins hold an object, as a director's admin
                    asks: refused without credentials, with bounds that
                    conflict, and for a namespace no origin exports
  metrics           the redirects and client requests a director counts,
                    the ads it receives, and its count of servers
  metadata-hosting  the discovery document a director does not serve,
                    and its comparison of the federation's metadata
  forwarding        under -p topo-multi-director, an ad posted to one
                    director reaches the other, and so must one that
                    lifts a cache's shutdown there

A server advertises to every director it hears of, from its
configuration, the discovery document, and each director's list of
directors (doDiscovery in Pelican's server_utils/director_discovery.go),
so no configuration keeps a server's ads from any director. `forwarding`
posts the first cache's own ad instead, signed as the `advertise` suite
signs it, to one director alone, and looks for it at the other. It
waits until the cache's own ads have restored both.

`origin-stat` puts an object in /public at one origin, under
data/director-api/<run>/, and the suite removes it afterward.
"""

import functools
import json
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from suites import advertise
from testlib import common, credentials, director, metrics, stores, web, webdav
from testlib.common import first, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "servers": "the lists of servers and namespaces, one server, the contact, and the directors",
    "namespaces": "listNamespaces, v2 and the deprecated v1, and the namespace of a path",
    "health": "each server's health test: its status, test runs, the server's view, a cache read",
    "health-test-file": "the director's test file by GET, HEAD, and PROPFIND, and bad names",
    "origin-stat": "which origins hold an object, for the admin alone; bad bounds, no namespace",
    "metrics": "the redirects, client requests, ads, and servers that a director counts",
    "metadata-hosting": "no discovery document at a director; its comparison of the metadata",
    "forwarding": "an ad posted to one director reaches another, and lifts a shutdown there",
}

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# The directors' routes (director/director.go and director_ui.go in
# Pelican).
API = "/api/v1.0/director"
UI = "/api/v1.0/director_ui"
SERVERS = f"{UI}/servers"
UI_NAMESPACES = f"{UI}/namespaces"
CONTACT = f"{UI}/contact"
STAT = f"{SERVERS}/origins/stat"
DISCREPANCY = f"{UI}/federation/discrepancy"
DIRECTORS = f"{API}/directors"
LIST_NAMESPACES = "/api/v2.0/director/listNamespaces"
LIST_NAMESPACES_V1 = f"{API}/listNamespaces"
PREFIX = f"{API}/namespaces/prefix"
HEALTH_TEST = f"{API}/healthTest"
OBJECT = f"{API}/object"
DISCOVERY = "/.well-known/pelican-configuration"

# Director.SupportContactEmail and SupportContactUrl, as
# framework/config.d/director/default/ and config.d/instance/fed/ set
# them.
CONTACT_EMAIL = "support@pelican-testing.invalid"
CONTACT_URL = "https://pelican-testing.invalid/support"

# Where the directors' health tests put their files
# (server_utils/test_file_transfer.go), and what each holds, as the
# director serves it.
MONITORING = "/pelican/monitoring"
TEST_DIR = f"{MONITORING}/directorTest"
TEST_BODY = b"This object was created by the Pelican director-test functionality\n"

# What a director lists of a server's health test (HealthTestStatus):
# passed, not run, and not yet run.
HEALTH_OK = "OK"
HEALTH_DISABLED = "Health Test Disabled"
HEALTH_PENDING = ("Initializing", "Unknown")
# The origins' storage that can take the test's probe
# (OriginStorageType.SupportsSelfTest); the director tests no other.
SELF_TESTABLE = ("posix", "posixv2", "pstore")
# What a director lists as a cache's storage: a cache's ad names none,
# and the director takes that for posix (finishRegisterServeAd).
CACHE_STORAGE = "posix"

# The metrics (metrics/director.go and metrics/health.go).
TEST_RUNS = "pelican_director_total_ftx_test_runs"
COMPONENT = "pelican_component_health_status"
REDIRECTS = "pelican_director_redirects_total"
CLIENT_REQUESTS = "pelican_director_client_requests_total"
ADS_RECEIVED = "pelican_director_advertisements_received_total"
SERVER_COUNT = "pelican_director_server_count"
# pelican_component_health_status's values (HealthStatusEnum) for `ok`,
# and for `warning`, which a server says until its first test.
COMPONENT_OK = 5.0
COMPONENT_PENDING = 4.0

# A namespace's capabilities, as the generated exports name them, and as
# a director's JSON does (server_structs.Capabilities). PublicReads
# implies reads (NamespaceAdsFromExports).
CAPS = {
    "PublicReads": "PublicRead",
    "Reads": "Read",
    "Writes": "Write",
    "Listings": "Listing",
    "DirectReads": "FallBackRead",
}

# How long the health tests may take to pass at every director, a director
# testing each server every 15 seconds (Director.OriginCacheHealthTestInterval)
# and a server calling a test missed after 30; and the least time between
# two counts of test runs.
HEALTH_WAIT = 150
RUN_SAMPLE = 35
# The least time between two counts of the ads a director received, each
# server advertising every 30 seconds (Server.AdvertisementInterval).
AD_SAMPLE = 40
# How many requests `metrics` sends each director, and how long a count
# may take to show the last of them.
REDIRECT_ASKS = 5
COUNT_WAIT = 5
# How long a director has after starting before it answers 404, not 429,
# for a namespace it knows of no origin for (inStartupSequence), and how
# near that a row takes either, in seconds.
STARTUP_WINDOW = 300
STARTUP_SLACK = 10
# `forwarding`: how long to wait for a cache's own ad to reach a director;
# how long one director has to show what another forwarded; and how long
# the cache's own ads have to restore both.
FRESH_WAIT = 40
FORWARD_WAIT = 10
RESTORE_WAIT = 150
# How long the director's admin tokens last.
TOKEN_LIFETIME = 3600


@dataclass(frozen=True)
class Server:
    """An origin or a federated cache of the shape."""

    kind: str  # "Origin" or "Cache", as a director names its type
    svc: str
    url: str  # its data URL
    storage: str  # an origin's ORIGIN_VARIANT; a cache's is CACHE_STORAGE
    exports: tuple[common.Export, ...]  # an origin's; none for a cache

    @property
    def host(self) -> str:
        """Its host, by which a director's URLs name it."""
        return director.host(self.url)

    @property
    def web_url(self) -> str:
        """Its web URL."""
        return common.web_url_of(self.url)

    @property
    def key(self) -> tuple[str, str]:
        """How a director's list of servers is matched to it: its type,
        and its web URL's host (one host is everything in `tiny`)."""
        return self.kind, self.host

    @property
    def tested(self) -> bool:
        """Whether the directors test it: every cache, and an origin whose
        storage can take the test (SupportsSelfTest)."""
        return self.kind == "Cache" or self.storage in SELF_TESTABLE

    @property
    def prefixes(self) -> set[str]:
        """Its exports' federation prefixes."""
        return {e.prefix for e in self.exports}


def shape_servers(fed: common.Federation) -> list[Server]:
    """Every origin (another owner's included) and federated cache of the
    shape, which every director should list."""
    exports = tuple(
        common.Export(f"/{ns}", ns, fed.exports[ns], fed.issuer_of(ns))
        for ns in credentials.exported(fed)
    )
    found = [Server("Origin", o.svc, o.url, fed.origin_variant, exports) for o in fed.origins]
    found += [
        Server("Origin", w.origin.svc, w.origin.url, fed.origin_variant, w.exports)
        for w in fed.owners
    ]
    found += [Server("Cache", c.svc, c.url, CACHE_STORAGE, ()) for c in fed.federated_caches]
    return found


class Test:
    """The federation, its servers, and what the scenarios share."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.base = f"data/director-api/{self.run}"
        # The first exported namespace, which the scenarios ask about.
        self.namespace = credentials.exported(self.fed)[0]
        self.servers = shape_servers(self.fed)
        self._admin: dict[str, str] = {}
        self._names: dict[str, dict[tuple[str, str], str]] = {}
        # Each director's ads received so far, which `metrics` compares
        # with later.
        self.started = time.monotonic()
        self.ads_before = {url: metrics.fetch(url) for url in self.fed.directors}

    @property
    def exports(self) -> list[common.Export]:
        """Every export of every origin of the shape, each prefix once."""
        found: dict[str, common.Export] = {}
        for server in self.servers:
            for export in server.exports:
                found.setdefault(export.prefix, export)
        return list(found.values())

    def admin(self, url: str) -> str:
        """A token that the director at url takes as its admin's
        (credentials.mint_user())."""
        if url not in self._admin:
            host = director.host(url)
            self._admin[url] = credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/director-api.admin.{host}"),
                credentials.local_issuer(self.fed, url),
                common.signing_key(f"issuer-keys/{host}"),
                "admin",
                TOKEN_LIFETIME,
            )
        return self._admin[url]

    def names(self, url: str) -> dict[tuple[str, str], str]:
        """The name by which the director at url lists each server, by its
        key (Server.key), as its list of servers says; asked once."""
        if url not in self._names:
            rows = listed_servers(web.request("GET", f"{url}{SERVERS}")) or []
            self._names[url] = {
                row_key(row): str(row.get("name", "")) for row in rows if row.get("name")
            }
        return self._names[url]


# --------------------------------------------------------------------------
# Reading a director's answers.


def parse(answer: web.Response) -> Any:
    """answer's body, as JSON, or None if it is not. A body of two JSON
    documents, one after the other, is none."""
    try:
        return json.loads(answer.body)
    except ValueError:
        return None


def objects(value: Any) -> list[dict[str, Any]]:
    """The objects in value, a JSON array; none if it is none."""
    return [common.as_object(v) or {} for v in common.as_array(value) or []]


def listed_servers(answer: web.Response) -> Optional[list[dict[str, Any]]]:
    """The servers in a director's list of servers, or None if answer is
    not one."""
    found = common.as_array(parse(answer)) if answer.status == 200 else None
    return None if found is None else objects(found)


def row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """How a server that a director lists is matched to the shape's (see
    Server.key)."""
    return str(row.get("type", "")), director.host(str(row.get("webUrl", "")))


def url_text(value: Any) -> str:
    """A URL as a director's JSON holds it: a string, or, where Go
    encodes a url.URL as it is, an object of its parts."""
    found = common.as_object(value)
    if found is None:
        return str(value or "")
    text = f"{found.get('Scheme', '')}://{found.get('Host', '')}{found.get('Path', '')}"
    return text + (f"?{found['RawQuery']}" if found.get("RawQuery") else "")


def caps_json(caps: frozenset[str]) -> dict[str, bool]:
    """A namespace's capabilities, as a director's JSON names them."""
    found = {name: theirs in caps for theirs, name in CAPS.items()}
    found["Read"] = found["Read"] or found["PublicRead"]
    return found


def issuer_of(export: common.Export) -> Optional[str]:
    """The issuer a director should name for export, or None if it has
    none."""
    return export.issuer if credentials.has_issuer(export.caps) else None


def outcome(problems: Sequence[str], note: str) -> tuple[str, str]:
    """FAIL with the first of problems, or PASS with note."""
    return (FAIL, first(problems)) if problems else (PASS, note)


def guarded(check: Callable[[], list[Row]]) -> list[Row]:
    """check()'s rows; one failing row if it raised."""
    try:
        return check()
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]


def utc_stamp() -> str:
    """Now, as a director's test files name it (RFC 3339, in UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def started_ago(url: str) -> Optional[float]:
    """How long ago the director at url started
    (process_start_time_seconds), in seconds; None if it won't say."""
    found = metrics.fetch(url)
    if found is None or not found.has("process_start_time_seconds"):
        return None
    return time.time() - found.value("process_start_time_seconds")


def unknown_statuses(url: str) -> tuple[int, ...]:
    """What the director at url may answer for a namespace no origin
    exports: 404, but 429 while it may not have heard from every origin
    (inStartupSequence), and either near the boundary or if it won't say
    when it started."""
    ago = started_ago(url)
    if ago is None or abs(ago - STARTUP_WINDOW) <= STARTUP_SLACK:
        return (404, 429)
    return (429,) if ago < STARTUP_WINDOW else (404,)


# --------------------------------------------------------------------------
# servers


def field_problems(test: Test, server: Server, row: Mapping[str, Any]) -> list[str]:
    """What is wrong with what a director lists of server (row), whose
    fields are listServerResponse's (director/director_ui.go)."""
    name = str(row.get("name", ""))
    if server.kind == "Origin":
        # GetOriginNs of its web URL's host and port (origin/advertise.go).
        prefix = f"/origins/{urlsplit(server.web_url).netloc}"
    else:
        # GetCacheNs of its Xrootd.Sitename, which is also its name.
        prefix = f"/caches/{name}"
    want: dict[str, Any] = {
        "webUrl": server.web_url,
        "registryPrefix": prefix,
        "storageType": server.storage,
        "disableDirectorTest": not server.tested,
        "filtered": False,
    }
    if test.fed.pelican_version:
        want["version"] = test.fed.pelican_version
    problems = [
        f"{server.svc}'s {key} is {row.get(key)!r}, not {value!r}"
        for key, value in want.items()
        if (str(row.get(key, "")).rstrip("/") if key == "webUrl" else row.get(key)) != value
    ]
    if director.host(str(row.get("url", ""))) != server.host:
        problems.append(f"{server.svc}'s url is {row.get('url')!r}, not at {server.host}")
    if server.kind == "Origin":
        # An origin of one export takes that export's capabilities as its
        # own (mapSingleExportIfNeeded); of several, Origin.EnableDirectReads,
        # true by default.
        direct = {"DirectReads" in e.caps for e in server.exports}
        caps = common.as_object(row.get("capabilities")) or {}
        if len(direct) == 1 and caps.get("FallBackRead") != direct.pop():
            problems.append(f"{server.svc}'s capabilities are {caps}")
    return problems


def listing_rows(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's list of servers: each
    origin and federated cache of the shape, once, with the fields the
    shape gives it (see field_problems()), and nothing else, a site-local
    cache included."""
    answer = web.request("GET", f"{url}{SERVERS}")
    rows = listed_servers(answer)
    name = director.host(url)
    if rows is None:
        return [f"{name}: {answer.describe()}, not a list of servers"]
    problems: list[str] = []
    wanted = {s.key: s for s in test.servers}
    for key in {row_key(r) for r in rows} - set(wanted):
        problems.append(f"{name} lists {key[0]} {key[1]}, which the shape lacks")
    for key, server in wanted.items():
        mine = [r for r in rows if row_key(r) == key]
        if len(mine) != 1:
            problems.append(f"{name} lists {server.svc} {len(mine)} times")
            continue
        problems += [f"{name}: {p}" for p in field_problems(test, server, mine[0])]
    return problems


def by_type_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's list of servers of one
    type: `origin` must list the origins alone, and `cache` the caches; a
    type that is neither must get 400."""
    name = director.host(url)
    problems: list[str] = []
    for kind in ("Origin", "Cache"):
        answer = web.request("GET", f"{url}{SERVERS}?server_type={kind.lower()}")
        rows = listed_servers(answer)
        if rows is None:
            problems.append(f"{name} ({kind.lower()}): {answer.describe()}, not a list")
            continue
        got = sorted(f"{k[0]} {k[1]}" for k in {row_key(r) for r in rows})
        want = sorted(f"{s.kind} {s.host}" for s in test.servers if s.kind == kind)
        if got != want:
            problems.append(f"{name} ({kind.lower()}) lists {got}, not {want}")
    answer = web.request("GET", f"{url}{SERVERS}?server_type=director")
    if answer.status != 400:
        problems.append(f"{name} (director): {answer.describe()}, not 400")
    return problems


def by_type_case_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's list of servers of a type
    spelled as Pelican does, `Origin` or `Cache`: it takes the type
    without regard to case (listServers in director/director_ui.go), so
    each must list what its lower-case spelling does."""
    name = director.host(url)
    problems: list[str] = []
    for kind in ("Origin", "Cache"):
        answer = web.request("GET", f"{url}{SERVERS}?server_type={kind}")
        rows = listed_servers(answer)
        if rows is None:
            problems.append(f"{name} ({kind}): {answer.describe()}, not a list")
            continue
        got = sorted(f"{k[0]} {k[1]}" for k in {row_key(r) for r in rows})
        want = sorted(f"{s.kind} {s.host}" for s in test.servers if s.kind == kind)
        if got != want:
            problems.append(f"{name} ({kind}) lists {got}, not {want}")
    return problems


def namespace_paths(value: Any) -> Optional[set[str]]:
    """The paths of a list of namespaces, or None if value is no list (a
    director answers null for none)."""
    if value is None:
        return set()
    found = common.as_array(value)
    return None if found is None else {str(n.get("path", "")) for n in objects(found)}


def one_problems(test: Test, url: str) -> list[str]:
    """What is wrong with what the director at url says of each server by
    its name, /servers/<name>, and of its namespaces,
    /servers/<name>/namespaces: its name and type, and for an origin, its
    exports; and of a name it does not know: 404. In `tiny`, the origin
    and the cache are one host, and share a name, of which the director
    describes whichever it finds first (getServer), so there each must
    only be one of them."""
    name = director.host(url)
    names = test.names(url)
    kinds: dict[str, set[str]] = {}
    for (kind, _), listed_as in names.items():
        kinds.setdefault(listed_as, set()).add(kind)
    problems: list[str] = []
    for server in test.servers:
        known = names.get(server.key)
        if known is None:
            problems.append(f"{name} does not list {server.svc}")
            continue
        target = f"{url}{SERVERS}/{quote(known, safe='')}"
        answer = web.request("GET", target)
        found = common.as_object(parse(answer)) if answer.status == 200 else None
        if found is None:
            problems.append(f"{name}: {SERVERS}/{known}: {answer.describe()}, not a server")
            continue
        if found.get("name") != known or found.get("type") not in kinds[known]:
            problems.append(
                f"{name}: {SERVERS}/{known} is {found.get('type')} {found.get('name')!r}"
            )
        listed = web.request("GET", f"{target}/namespaces")
        paths = namespace_paths(parse(listed)) if listed.status == 200 else None
        if paths is None:
            problems.append(f"{name}: {SERVERS}/{known}/namespaces: {listed.describe()}")
        elif server.kind == "Origin" and found.get("type") == "Origin":
            for what, got in (
                ("/servers/<name>", namespace_paths(found.get("namespaces"))),
                ("/servers/<name>/namespaces", paths),
            ):
                if got != server.prefixes:
                    problems.append(
                        f"{name}: {what} for {server.svc} has {sorted(got or ())},"
                        + f" not {sorted(server.prefixes)}"
                    )
    nobody = f"director-api-nobody-{test.run}"
    answer = web.request("GET", f"{url}{SERVERS}/{nobody}")
    if answer.status != 404:
        problems.append(f"{name}: {SERVERS}/{nobody}: {answer.describe()}, not 404")
    return problems


def ui_namespaces_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's list of namespaces
    (/director_ui/namespaces): each export, named with every origin that
    exports it, and no other."""
    name = director.host(url)
    answer = web.request("GET", f"{url}{UI_NAMESPACES}")
    found = common.as_array(parse(answer)) if answer.status == 200 else None
    if found is None:
        return [f"{name}: {answer.describe()}, not a list of namespaces"]
    by_path = {str(n.get("path", "")): n for n in objects(found)}
    names = test.names(url)
    problems: list[str] = []
    for export in test.exports:
        entry = by_path.get(export.prefix)
        if entry is None:
            problems.append(f"{name} does not list {export.prefix}")
            continue
        want = sorted(
            names.get(s.key, s.svc)
            for s in test.servers
            if s.kind == "Origin" and export.prefix in s.prefixes
        )
        got = sorted(str(o) for o in common.as_array(entry.get("origins")) or [])
        if got != want:
            problems.append(f"{name}: {export.prefix}'s origins are {got}, not {want}")
    return problems


def contact_problems(url: str) -> list[str]:
    """What is wrong with the director at url's support contact: the
    email and URL its configuration gives."""
    answer = web.request("GET", f"{url}{CONTACT}")
    found = common.as_object(parse(answer)) or {}
    want = {"email": CONTACT_EMAIL, "url": CONTACT_URL}
    got = {k: found.get(k) for k in want}
    if answer.status != 200 or got != want:
        return [f"{director.host(url)}: {answer.describe()}, {got}, not {want}"]
    return []


def directors_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's list of directors: every
    director of the shape, by the URL it takes ads at, and no other."""
    answer = web.request("GET", f"{url}{DIRECTORS}")
    found = common.as_array(parse(answer)) if answer.status == 200 else None
    if found is None:
        return [f"{director.host(url)}: {answer.describe()}, not a list of directors"]
    got = sorted({director.host(str(d.get("advertise_url", ""))) for d in objects(found)})
    want = sorted(director.host(u) for u in test.fed.directors)
    return [] if got == want else [f"{director.host(url)} lists {got}, not {want}"]


def servers(test: Test) -> list[Row]:
    """What each director says of the federation's servers (see each
    *_problems() function)."""
    count = f"{len(test.servers)} server(s), {len(test.fed.directors)} director(s)"
    checks: list[tuple[str, Callable[[str], list[str]], str]] = [
        ("listing", lambda u: listing_rows(test, u), count),
        ("by-type", lambda u: by_type_problems(test, u), "origin, cache, and 400"),
        ("by-type-case", lambda u: by_type_case_problems(test, u), "Origin and Cache"),
        ("one", lambda u: one_problems(test, u), f"{count}, and 404"),
        ("ui-namespaces", lambda u: ui_namespaces_problems(test, u), "every export"),
        ("contact", contact_problems, CONTACT_EMAIL),
        ("directors", lambda u: directors_problems(test, u), "every director"),
    ]
    rows: list[Row] = []
    for case, check, note in checks:
        problems = [p for url in test.fed.directors for p in check(url)]
        rows.append((case, *outcome(problems, note)))
    return rows


# --------------------------------------------------------------------------
# namespaces


def v2_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's listNamespaces: every
    export, once per origin that exports it, with its capabilities and its
    issuer (none for one without), and /pelican/monitoring, which anyone
    may read, and nothing else. A cache learns a namespace's issuer here,
    so a wrong one breaks every protected read through it."""
    name = director.host(url)
    answer = web.request("GET", f"{url}{LIST_NAMESPACES}")
    found = common.as_array(parse(answer)) if answer.status == 200 else None
    if found is None:
        return [f"{name}: {answer.describe()}, not a list of namespaces"]
    entries = objects(found)
    exports = {e.prefix: e for e in test.exports}
    problems: list[str] = []
    paths = {str(e.get("path", "")) for e in entries}
    extra = sorted(paths - set(exports) - {MONITORING})
    missing = sorted((set(exports) | {MONITORING}) - paths)
    if extra:
        problems.append(f"{name} also lists {extra}")
    if missing:
        problems.append(f"{name} does not list {missing}")
    for entry in entries:
        path = str(entry.get("path", ""))
        caps = common.as_object(entry.get("Caps")) or {}
        issuers = [url_text(i.get("issuer")) for i in objects(entry.get("token-issuer"))]
        if path == MONITORING:
            if caps.get("PublicRead") is not True or caps.get("Read") is not True:
                problems.append(f"{name}: {MONITORING}'s capabilities are {caps}")
            continue
        export = exports.get(path)
        if export is None:
            continue
        want = caps_json(export.caps)
        got = {k: caps.get(k) for k in want}
        if got != want:
            problems.append(f"{name}: {path}'s capabilities are {got}, not {want}")
        issuer = issuer_of(export)
        if issuers != ([issuer] if issuer else []):
            problems.append(f"{name}: {path}'s issuers are {issuers}, not {issuer or 'none'}")
    return problems


def v1_problems(url: str) -> list[str]:
    """What is wrong with the director at url's answer to the first
    version of listNamespaces, which it serves no longer: status 299, a
    Warning, and no namespaces (listNamespacesV1Deprecated)."""
    name = director.host(url)
    answer = web.request("GET", f"{url}{LIST_NAMESPACES_V1}")
    warning = answer.header("Warning")
    if answer.status != 299:
        return [f"{name}: {answer.describe()}, not 299"]
    if not warning.startswith("299 "):
        return [f"{name}: Warning is {warning!r}"]
    if common.as_object(parse(answer)) is None:
        return [f"{name}: its body is {answer.body[:200]!r}, not a JSON object"]
    return []


def prefix_cases(test: Test) -> list[tuple[str, Optional[str]]]:
    """The paths whose namespace the suite asks for, and the namespace
    each must get, or None for 404. A namespace's prefix matches whole
    segments of a path."""
    ns = test.namespace
    cases: list[tuple[str, Optional[str]]] = [
        (f"/{ns}/data/x", f"/{ns}"),
        (f"/director-api-nowhere-{test.run}/x", None),
    ]
    prefixes = {e.prefix for e in test.exports}
    if {"/public", "/public/other"} <= prefixes:
        cases += [("/public/other/x", "/public/other"), ("/public/others-x", "/public")]
    return cases


def prefix_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the namespace the director at url gives each
    of prefix_cases()."""
    name = director.host(url)
    problems: list[str] = []
    for path, want in prefix_cases(test):
        answer = web.request("GET", f"{url}{PREFIX}{path}")
        got = (common.as_object(parse(answer)) or {}).get("prefix")
        if want is None and answer.status != 404:
            problems.append(f"{name}: {path}: {answer.describe()}, not 404")
        elif want is not None and (answer.status != 200 or got != want):
            problems.append(f"{name}: {path}: {answer.describe()}, {got!r}, not {want}")
    return problems


def namespaces(test: Test) -> list[Row]:
    """What each director says of the namespaces (see v2_problems(),
    v1_problems(), and prefix_problems())."""
    directors = test.fed.directors
    cases = prefix_cases(test)
    rows: list[Row] = []
    problems = [p for url in directors for p in v2_problems(test, url)]
    rows.append(("v2", *outcome(problems, f"{len(test.exports)} export(s), and {MONITORING}")))
    problems = [p for url in directors for p in v1_problems(url)]
    rows.append(("v1", *outcome(problems, "299, with Warning")))
    problems = [p for url in directors for p in prefix_problems(test, url)]
    rows.append(("prefix", *outcome(problems, ", ".join(f"{p}: {w or 404}" for p, w in cases))))
    return rows


# --------------------------------------------------------------------------
# health


def status_problems(test: Test) -> tuple[list[str], bool]:
    """What is wrong with the health each director lists of each server:
    OK, for a server it tests; and for an origin whose storage can't take
    the test, disabled. And whether any may yet change: one not listed,
    or whose test has not yet run."""
    problems: list[str] = []
    pending = False
    for url in test.fed.directors:
        rows = listed_servers(web.request("GET", f"{url}{SERVERS}")) or []
        by_key = {row_key(r): r for r in rows}
        for server in test.servers:
            row = by_key.get(server.key)
            what = f"{director.host(url)}: {server.svc}"
            want = HEALTH_OK if server.tested else HEALTH_DISABLED
            if row is None:
                problems.append(f"{what} is not listed")
                pending = True
            elif row.get("healthStatus") != want:
                problems.append(f"{what}'s healthStatus is {row.get('healthStatus')!r}")
                pending = pending or row.get("healthStatus") in HEALTH_PENDING
    return problems, pending


def component_problems(test: Test) -> tuple[list[str], bool]:
    """What is wrong with each origin's and federated cache's own view of
    the directors' tests (pelican_component_health_status, component
    `director`): ok. A server the directors don't test says so, ok too.
    And whether any may yet change: one that says nothing, or that is
    still waiting for its first test."""
    problems: list[str] = []
    pending = False
    seen: set[str] = set()
    for server in test.servers:
        if server.web_url in seen:
            continue
        seen.add(server.web_url)
        found = metrics.fetch(server.web_url)
        series = None if found is None else found.series(COMPONENT, component="director")
        if not series:
            problems.append(f"{server.svc}: no {COMPONENT} for `director`")
            pending = True
        elif set(series.values()) != {COMPONENT_OK}:
            problems.append(f"{server.svc}: {COMPONENT} for `director` is {series}")
            pending = pending or COMPONENT_PENDING in series.values()
    return problems, pending


def run_problems(
    test: Test,
    before: Mapping[str, Optional[metrics.Snapshot]],
    after: Mapping[str, Optional[metrics.Snapshot]],
) -> list[str]:
    """What is wrong with how each director's test runs rose between two
    counts: by at least one run that passed and was reported, for each
    server it tests; by none at all, for each it doesn't."""
    problems: list[str] = []
    for url in test.fed.directors:
        then, now = before.get(url), after.get(url)
        name = director.host(url)
        if then is None or now is None:
            problems.append(f"{name}: its /metrics did not answer")
            continue
        names = test.names(url)
        for server in test.servers:
            labels = {
                "server_name": names.get(server.key, server.svc),
                "server_type": server.kind,
            }
            if server.tested:
                rose = metrics.delta(
                    then,
                    now,
                    TEST_RUNS,
                    status="Succeeded",
                    report_status="Succeeded",
                    **labels,
                )
                if rose < 1:
                    problems.append(f"{name}: {server.svc}'s passed runs rose by {rose:g}")
            elif metrics.delta(then, now, TEST_RUNS, **labels) != 0:
                problems.append(f"{name}: tested {server.svc}, whose storage can't take it")
    return problems


def cache_file_problems(test: Test) -> list[str]:
    """What is wrong with a test file read through each federated cache,
    as a director tests the cache: the cache asks a director for an
    origin of /pelican/monitoring, which sends it to the director's own
    test file, whose body it must serve (runCacheTest in
    director/cache_monitor.go). The file is the suite's own, by its name,
    beside the directors' own."""
    stamp = utc_stamp()
    path = f"{TEST_DIR}/director-api-{test.run}/{stamp[:10]}/director-test-{stamp}.txt"
    problems: list[str] = []
    for cache in test.fed.federated_caches:
        answer = web.request("GET", f"{cache.url}{path}")
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()}")
        elif answer.body != TEST_BODY:
            problems.append(f"{cache.svc}: the body is {answer.body[:200]!r}")
    return problems


def health(test: Test) -> list[Row]:
    """The directors' health tests: within HEALTH_WAIT seconds, each
    director must list each server's as passing (see status_problems()),
    and each server must say so too (see component_problems()); each
    director must have run them, at least RUN_SAMPLE seconds apart (see
    run_problems()); and a test file must come back through each cache
    (see cache_file_problems()). The wait ends early once nothing is
    pending, and a failed test has had RUN_SAMPLE seconds to pass."""
    before = {url: metrics.fetch(url) for url in test.fed.directors}
    started = time.monotonic()
    found: dict[str, list[str]] = {}

    def settled() -> bool:
        found["status"], waiting = status_problems(test)
        found["component"], unsure = component_problems(test)
        if not found["status"] and not found["component"]:
            return True
        return not waiting and not unsure and time.monotonic() - started >= RUN_SAMPLE

    common.wait_for(settled, HEALTH_WAIT, interval=5)
    left = RUN_SAMPLE - (time.monotonic() - started)
    if left > 0:
        time.sleep(left)
    after = {url: metrics.fetch(url) for url in test.fed.directors}
    tested = sum(1 for s in test.servers if s.tested)
    disabled = len(test.servers) - tested
    note = f"{tested} tested, {disabled} disabled"
    return [
        ("status", *outcome(found["status"], note)),
        ("test-runs", *outcome(run_problems(test, before, after), note)),
        ("component", *outcome(found["component"], "ok")),
        ("cache-file", *outcome(cache_file_problems(test), "the test body")),
    ]


# --------------------------------------------------------------------------
# health-test-file


def health_test_file(test: Test) -> list[Row]:
    """The test file each director serves for its tests of caches
    (getHealthTestFile in director/director.go), named by a timestamp:
    a GET gets the body; a HEAD its length; and a PROPFIND a multistatus
    naming the file and its length. A name whose timestamp is none gets
    400. A directory whose name holds `&` and `<` must come back escaped
    in the PROPFIND's XML: Pelican escapes the path, but writes it as it
    was, so the `escaped` row fails there."""
    stamp = utc_stamp()
    name = f"director-test-{stamp}.txt"
    good = f"{TEST_DIR}/{name}"
    escaped = f"{TEST_DIR}/a&b</{name}"
    cases: dict[str, list[str]] = {
        c: [] for c in ("get", "head", "propfind", "bad-timestamp", "escaped")
    }
    for url in test.fed.directors:
        what = director.host(url)
        base = f"{url}{HEALTH_TEST}"
        answer = web.request("GET", f"{base}{good}")
        if answer.status != 200 or answer.body != TEST_BODY:
            cases["get"].append(f"{what}: {answer.describe()}, {answer.body[:100]!r}")
        answer = web.request("HEAD", f"{base}{good}")
        length = answer.header("Content-Length")
        if answer.status != 200 or length != str(len(TEST_BODY)):
            cases["head"].append(f"{what}: {answer.describe()}, Content-Length {length!r}")
        for case, path, sent in (
            ("propfind", good, f"{base}{good}"),
            ("escaped", escaped, f"{base}{TEST_DIR}/a%26b%3C/{name}"),
        ):
            answer = webdav.propfind(sent, "0")
            if answer.status != 207:
                cases[case].append(f"{what}: {answer.describe()}, not 207")
                continue
            try:
                entries = webdav.parse(answer.body)
            except ValueError as e:
                cases[case].append(f"{what}: {e}")
                continue
            if [(e.path, e.size) for e in entries] != [(path, len(TEST_BODY))]:
                found = [(e.path, e.size) for e in entries]
                cases[case].append(f"{what}: it lists {found}, not {path}")
        answer = web.request("GET", f"{base}{TEST_DIR}/director-test-yesterday.txt")
        if answer.status != 400:
            cases["bad-timestamp"].append(f"{what}: {answer.describe()}, not 400")
    notes = {
        "get": "the test body",
        "head": f"Content-Length {len(TEST_BODY)}",
        "propfind": "207, the file",
        "bad-timestamp": "400",
        "escaped": "207, the file, escaped",
    }
    return [(case, *outcome(problems, notes[case])) for case, problems in cases.items()]


# --------------------------------------------------------------------------
# origin-stat


@dataclass(frozen=True)
class Held:
    """The object that `origin-stat` puts at one origin alone."""

    origin: common.Origin
    rel: str  # its path below /public


def put_held(test: Test) -> tuple[Optional[Held], str]:
    """Put an object in /public at the last origin (of those with stores
    of their own), and no other: what was put, or None and why not. Where
    /public reads another namespace's storage (pstore), it goes there."""
    if "public" not in test.fed.exports:
        return None, "the shape exports no /public, whose objects the admin may stat"
    holder = stores.unique(test.fed.origins)[-1]
    store_ns = holder.storage_namespace("public")
    token = test.session.token(store_ns)
    rel = f"{test.base}/held"
    errors = stores.make_dirs([holder], store_ns, test.base, token)
    error = (
        errors[0]
        if errors
        else stores.seed(
            holder, store_ns, rel, b"director-api\n", token, test.fed.exports[store_ns]
        )
    )
    if error:
        return None, f"putting the object at {holder.svc}: {error}"
    return Held(holder, rel), ""


def holders(test: Test, held: Held) -> set[str]:
    """The hosts of the origins that hold held: its origin, and any that
    shares its store (the lab server's, under `-p origin-ssh`)."""
    mine = stores.unique([held.origin])[0]
    return {
        director.host(o.url)
        for o in test.fed.origins
        if (o.svc if o.pstore else o.store) == (mine.svc if mine.pstore else mine.store)
    }


def stat_admin_problems(test: Test, url: str, held: Held) -> list[str]:
    """What is wrong with what the director at url's admin learns of
    held: 200, naming origins that hold it, and no other. It names one,
    by default (Director.MaxStatResponse)."""
    answer = web.request("GET", f"{url}{STAT}/public/{held.rel}", token=test.admin(url))
    found = common.as_object(parse(answer)) or {}
    hosts = {director.host(str(o.get("url", ""))) for o in objects(found.get("objects"))}
    want = holders(test, held)
    if answer.status != 200 or not hosts or not hosts <= want:
        named = sorted(hosts)
        return [
            f"{director.host(url)}: {answer.describe()}, naming {named}, not {sorted(want)}"
        ]
    return []


def origin_stat(test: Test) -> list[Row]:
    """Which origins hold an object, as each director asks them
    (queryOrigins in director/director_ui.go): without credentials, 401;
    as its admin, of an object one origin alone holds, that origin; with
    min_responses above max_responses, 400; and for a namespace no origin
    exports, 404, as one JSON document. Pelican writes the 404 and goes
    on to ask no origin, which it answers with another, so the
    `unknown-namespace` row fails there."""
    held, why = put_held(test)
    cases: dict[str, list[str]] = {
        c: [] for c in ("no-auth", "admin", "bad-bounds", "unknown-namespace")
    }
    # An object in an exported namespace. With ?skipstat, the director
    # finds its origins without asking them, so that only the bounds
    # decide.
    target = f"{STAT}/public/{held.rel}" if held else f"{STAT}/{test.namespace}/{test.base}/x"
    for url in test.fed.directors:
        what = director.host(url)
        token = test.admin(url)
        answer = web.request("GET", f"{url}{target}")
        if answer.status != 401:
            cases["no-auth"].append(f"{what}: {answer.describe()}, not 401")
        if held is not None:
            cases["admin"] += stat_admin_problems(test, url, held)
        query = "skipstat&min_responses=2&max_responses=1"
        answer = web.request("GET", f"{url}{target}?{query}", token)
        if answer.status != 400:
            cases["bad-bounds"].append(f"{what}: {answer.describe()}, not 400")
        want = unknown_statuses(url)
        path = f"/director-api-nowhere-{test.run}/x"
        answer = web.request("GET", f"{url}{STAT}{path}", token=token)
        if answer.status not in want:
            cases["unknown-namespace"].append(f"{what}: {answer.describe()}, not {want}")
        elif common.as_object(parse(answer)) is None:
            body = answer.body[:300]
            cases["unknown-namespace"].append(f"{what}: its body is no JSON object: {body!r}")
    rows: list[Row] = [("no-auth", *outcome(cases["no-auth"], "401"))]
    if held is None:
        rows.append(("admin", SKIP, why))
    else:
        named = ", ".join(sorted(holders(test, held)))
        rows.append(("admin", *outcome(cases["admin"], f"only {named}")))
    rows.append(("bad-bounds", *outcome(cases["bad-bounds"], "400")))
    rows.append(("unknown-namespace", *outcome(cases["unknown-namespace"], "404, one body")))
    return rows


# --------------------------------------------------------------------------
# metrics


def client_agent(test: Test) -> tuple[str, str, str]:
    """A Pelican client's User-Agent of a version of the run's own, which
    no other client sends: it, its version, as a director's redirects
    count it, and its major and minor version, as its client requests
    do (collectClientVersionMetric)."""
    patch = int(test.run.replace("-", "")[-6:])
    version = f"7.99.{patch}"
    return f"pelican-client/{version}", version, "7.99"


def redirect_problems(test: Test, url: str) -> list[str]:
    """What is wrong with how the director at url counts REDIRECT_ASKS
    redirects of the run's client (see client_agent()): each must raise
    its redirects of that version with status 307, and its client
    requests of that version, by one."""
    agent, version, short = client_agent(test)
    ns = test.namespace
    token = None if ns == "public" else test.session.token(ns)
    target = f"{url}{OBJECT}/{ns}/{test.base}/metrics-probe?skipstat"
    name = director.host(url)
    before = metrics.fetch(url)
    if before is None:
        return [f"{name}: its /metrics did not answer"]
    for _ in range(REDIRECT_ASKS):
        answer = web.request("GET", target, token=token, headers={"User-Agent": agent})
        if answer.status != 307:
            return [f"{name}: {answer.describe()}, not 307"]
    counted: dict[str, float] = {}

    def done() -> bool:
        after = metrics.fetch(url) or metrics.EMPTY
        counted["redirects"] = metrics.delta(
            before, after, REDIRECTS, version=version, status_code="307"
        )
        counted["requests"] = metrics.delta(
            before, after, CLIENT_REQUESTS, version=short, service="client"
        )
        return set(counted.values()) == {float(REDIRECT_ASKS)}

    if common.wait_for(done, COUNT_WAIT, interval=1):
        return []
    return [f"{name}: for {REDIRECT_ASKS} redirects, counted {counted}"]


def ads_problems(test: Test, url: str) -> list[str]:
    """What is wrong with how the director at url counts the ads it
    receives: since the suite began, at least AD_SAMPLE seconds ago, each
    origin's and federated cache's, taken (200). A director counts the
    ads that a server sends it, and not those that another director
    forwards (serverAdMetricMiddleware)."""
    name = director.host(url)
    before, after = test.ads_before.get(url), metrics.fetch(url)
    if before is None or after is None:
        return [f"{name}: its /metrics did not answer"]
    names = test.names(url)
    problems: list[str] = []
    for server in test.servers:
        labels = {"server_name": names.get(server.key, server.svc), "server_type": server.kind}
        if metrics.delta(before, after, ADS_RECEIVED, status_code="200", **labels) < 1:
            problems.append(f"{name}: counted no ad of {server.svc}")
    return problems


def count_problems(test: Test, url: str) -> list[str]:
    """What is wrong with the director at url's count of servers
    (pelican_director_server_count): one of each origin and federated
    cache, of its type, none from the topology, and as many of each type
    as the shape has."""
    name = director.host(url)
    found = metrics.fetch(url)
    if found is None:
        return [f"{name}: its /metrics did not answer"]
    names = test.names(url)
    problems: list[str] = []
    for server in test.servers:
        got = found.value(
            SERVER_COUNT,
            server_name=names.get(server.key, server.svc),
            server_type=server.kind,
            from_topology="false",
        )
        if got != 1:
            problems.append(f"{name} counts {server.svc} {got:g} times")
    for kind in ("Origin", "Cache"):
        got = found.value(SERVER_COUNT, server_type=kind)
        want = sum(1 for s in test.servers if s.kind == kind)
        if got != want:
            problems.append(f"{name} counts {got:g} {kind.lower()}(s), not {want}")
    return problems


def metrics_scenario(test: Test) -> list[Row]:
    """What each director counts (metrics/director.go): the redirects and
    client requests of a client of the run's own (see
    redirect_problems()), the ads it receives (see ads_problems()), and
    the servers it knows (see count_problems())."""
    left = AD_SAMPLE - (time.monotonic() - test.started)
    if left > 0:
        time.sleep(left)
    directors = test.fed.directors
    agent = client_agent(test)[0]
    return [
        (
            "redirects",
            *outcome(
                [p for u in directors for p in redirect_problems(test, u)],
                f"{REDIRECT_ASKS} each, as {agent}",
            ),
        ),
        (
            "ads",
            *outcome([p for u in directors for p in ads_problems(test, u)], "every server's"),
        ),
        (
            "server-count",
            *outcome(
                [p for u in directors for p in count_problems(test, u)],
                f"{len(test.servers)} server(s)",
            ),
        ),
    ]


# --------------------------------------------------------------------------
# metadata-hosting


def hosts_metadata(test: Test, url: str) -> bool:
    """Whether the director at url is the federation's discovery host:
    `tiny`'s is, director-0 under `-p director-discovery`, and every
    director where the discovery host is the directors' load balancer
    (`-p director-ha` too); elsewhere the discovery service is."""
    discovery_host = director.host(str(test.fed.discovery["discovery_endpoint"]))
    if test.fed.director_ha and discovery_host == director.host(test.fed.director_url):
        return True
    return director.host(url) == discovery_host


def metadata_hosting(test: Test) -> list[Row]:
    """Each director's discovery document, which it serves only where it
    is the discovery host, and otherwise refuses with 410
    (Director.EnableFederationMetadataHosting, off in
    framework/config.d/director/default/); and its comparison of the
    metadata it was given with the discovery host's
    (director/metadata_comparison.go): its admin's alone (401 for no
    one), and in agreement, but where it is the discovery host itself,
    off."""
    cases: dict[str, list[str]] = {c: [] for c in ("discovery", "no-auth", "discrepancy")}
    for url in test.fed.directors:
        what = director.host(url)
        own = hosts_metadata(test, url)
        answer = web.request("GET", f"{url}{DISCOVERY}")
        want = 200 if own else 410
        if answer.status != want:
            cases["discovery"].append(f"{what}: {answer.describe()}, not {want}")
        answer = web.request("GET", f"{url}{DISCREPANCY}")
        if answer.status != 401:
            cases["no-auth"].append(f"{what}: {answer.describe()}, not 401")
        answer = web.request("GET", f"{url}{DISCREPANCY}", token=test.admin(url))
        found = common.as_object(parse(answer)) or {}
        expected: dict[str, Any] = {"enabled": not own, "hasDiscrepancy": False}
        if not own:
            expected["jwksHasOverlap"] = True
        got = {k: found.get(k) for k in expected}
        if answer.status != 200 or got != expected:
            cases["discrepancy"].append(f"{what}: {answer.describe()}, {got}, not {expected}")
    return [
        ("discovery", *outcome(cases["discovery"], "410, or 200 at the discovery host")),
        ("no-auth", *outcome(cases["no-auth"], "401")),
        ("discrepancy", *outcome(cases["discrepancy"], "none")),
    ]


# --------------------------------------------------------------------------
# forwarding


@dataclass
class Forwarding:
    """`forwarding`'s cache, its own key, and the two directors."""

    ads: advertise.Test
    cache: advertise.Server
    token: str
    first: str  # the director the suite posts the cache's ads to, by URL
    second: str  # the director that must learn of them from the first
    status: str  # the cache's status, as the second listed it at first
    # The statuses the suite posted, but the cache's own.
    posted: set[str] = field(default_factory=set[str])

    def listed(self, url: str) -> dict[str, Any]:
        """What the director at url says of the cache."""
        answer = web.request("GET", f"{url}{SERVERS}/{quote(self.cache.name, safe='')}")
        return common.as_object(parse(answer)) or {}

    def ads_seen(self, url: str) -> Optional[float]:
        """How many of the cache's ads the director at url has taken
        (each of its namespaces' series counts every ad); None if it
        won't say."""
        found = metrics.fetch(url)
        if found is None:
            return None
        labels = {"server_name": self.cache.name, "server_type": "Cache", "status_code": "200"}
        return max(found.series(ADS_RECEIVED, **labels).values(), default=0.0)

    def fresh(self, url: str) -> bool:
        """Wait, up to FRESH_WAIT seconds, until the cache's own ad has
        just reached the director at url, so that its next is about
        Server.AdvertisementInterval away; whether it did."""
        start = self.ads_seen(url)
        if start is None:
            return False
        return common.wait_for(lambda: (self.ads_seen(url) or 0) > start, FRESH_WAIT, 1)

    def post(self, url: str, status: str) -> web.Response:
        """Post the cache's own ad, saying status, to the director at url
        alone."""
        if status != self.status:
            self.posted.add(status)
        body = advertise.ad(self.cache, status=status)
        return advertise.post_ad(url, "Cache", body, self.token)


def forwarded(f: Forwarding) -> Row:
    """The cache's own ad, saying a status it doesn't, posted to the
    first director alone, must reach the second within FORWARD_WAIT
    seconds, by the first's forwarding (forwardServiceAd in
    director/director_advertise.go). If the cache's own ad reached the
    second meanwhile, and the second shows the cache's status, the row
    can't tell."""
    mark = "degraded" if f.status == "warning" else "warning"
    if not f.fresh(f.second):
        return "", INCONCLUSIVE, f"no ad of the cache's reached the second in {FRESH_WAIT}s"
    before = f.ads_seen(f.second)
    answer = f.post(f.first, mark)
    if answer.status != 200:
        return "", FAIL, f"posting to {director.host(f.first)}: {answer.describe()}, not 200"
    seen = common.wait_for(
        lambda: f.listed(f.second).get("serverStatus") == mark, FORWARD_WAIT, 0.5
    )
    second = director.host(f.second)
    if seen:
        return "", PASS, f"{second} lists it `{mark}`"
    if f.ads_seen(f.second) != before:
        return "", INCONCLUSIVE, f"the cache's own ad reached {second} meanwhile"
    got = f.listed(f.second).get("serverStatus")
    return "", FAIL, f"{FORWARD_WAIT}s on, {second} lists it {got!r}, not {mark!r}"


def shutdown_lifted(f: Forwarding) -> Row:
    """The cache's own ad saying it is shutting down, posted to the
    second director, which must then filter it (shutdownFiltered); then
    its ad saying its status, posted to the first alone, which forwards
    it, must lift that within FORWARD_WAIT seconds, as it would have, had
    the cache posted it to the second. Only an ad posted to a director
    lifts it (registerServerAd), not a forwarded one
    (finishRegisterServeAd), so the row fails there. If the cache's own
    ad reached the second meanwhile, which would lift it, the row can't
    tell."""
    second = director.host(f.second)
    if not f.fresh(f.second):
        return "", INCONCLUSIVE, f"no ad of the cache's reached {second} in {FRESH_WAIT}s"
    before = f.ads_seen(f.second)
    answer = f.post(f.second, "shutting down")
    if answer.status != 200:
        return "", FAIL, f"posting to {second}: {answer.describe()}, not 200"
    if not common.wait_for(
        lambda: f.listed(f.second).get("filteredType") == "shutdownFiltered", FORWARD_WAIT, 0.5
    ):
        got = f.listed(f.second).get("filteredType")
        return "", INCONCLUSIVE, f"{second} did not filter it as shutting down: {got!r}"
    answer = f.post(f.first, f.status)
    if answer.status != 200:
        return "", FAIL, f"posting to {director.host(f.first)}: {answer.describe()}, not 200"
    lifted = common.wait_for(lambda: not f.listed(f.second).get("filtered"), FORWARD_WAIT, 0.5)
    after = f.ads_seen(f.second)
    if before is None or after is None or after - before != 1:
        return "", INCONCLUSIVE, f"the cache's own ad reached {second} meanwhile"
    if lifted:
        return "", PASS, f"{second} unfiltered it"
    got = f.listed(f.second).get("filteredType")
    return "", FAIL, f"{FORWARD_WAIT}s on, {second} still filters it ({got})"


def restored(f: Forwarding) -> list[str]:
    """Wait, up to RESTORE_WAIT seconds, until the cache's own ads have
    restored it at both directors: unfiltered, and saying none of the
    statuses the suite posted; what is still wrong after that."""
    problems: list[str] = []
    for url in (f.first, f.second):

        def back(u: str = url) -> bool:
            found = f.listed(u)
            return not found.get("filtered") and found.get("serverStatus") not in f.posted

        if not common.wait_for(back, RESTORE_WAIT, 5):
            found = f.listed(url)
            shown = {k: found.get(k) for k in ("serverStatus", "filtered", "filteredType")}
            problems.append(f"{RESTORE_WAIT}s on, {director.host(url)} lists it {shown}")
    return problems


def as_rows(check: Callable[[Forwarding], Row], f: Forwarding) -> list[Row]:
    """check(f)'s row, as a list (see guarded())."""
    return [check(f)]


def forwarding(test: Test) -> list[Row]:
    """Under -p topo-multi-director, the first federated cache's own ads,
    each posted to one director alone (see forwarded() and
    shutdown_lifted()); then the cache's own ads must restore it at both
    (`restored`)."""
    directors = test.fed.directors
    cases = ("forwarded", "shutdown-lifted", "restored")
    if len(directors) < 2:
        return [(c, SKIP, "one director (-p topo-multi-director)") for c in cases]
    if not test.fed.federated_caches:
        return [(c, SKIP, "the directors know of no cache") for c in cases]
    ads = advertise.Test(test.session)
    try:
        cache = ads.server("Cache", director.host(test.fed.federated_caches[0].url))
        token = ads.advertise_token(cache)
    except advertise.Missing as e:
        return [(c, FAIL, str(e)) for c in cases]
    f = Forwarding(ads, cache, token, directors[0], directors[1], "")
    f.status = str(f.listed(f.second).get("serverStatus") or "ok")
    if f.status == "shutting down":
        return [(c, INCONCLUSIVE, "the cache says it is shutting down") for c in cases]
    rows: list[Row] = []
    try:
        for case, check in (("forwarded", forwarded), ("shutdown-lifted", shutdown_lifted)):
            _, status, note = guarded(functools.partial(as_rows, check, f))[0]
            rows.append((case, status, note))
    finally:
        problems = restored(f)
        rows.append(("restored", *outcome(problems, f"{cache.name}, at both")))
    return rows


# --------------------------------------------------------------------------


RUN: dict[str, Callable[[Test], list[Row]]] = {
    "servers": servers,
    "namespaces": namespaces,
    "health": health,
    "health-test-file": health_test_file,
    "origin-stat": origin_stat,
    "metrics": metrics_scenario,
    "metadata-hosting": metadata_hosting,
    "forwarding": forwarding,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: a standalone origin has no
    director."""
    return "a standalone origin has no director" if not fed.directors else None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove what `origin-stat` put."""
    test = Test(session)
    try:
        for name in selected:
            for case, status, note in guarded(functools.partial(RUN[name], test)):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        fed = session.fed
        exports = {ns: fed.exports[ns] for ns in credentials.exported(fed)}
        errors = stores.empty_tree_everywhere(
            fed.origins, exports, "data/director-api", session.tokens()
        )
        if errors:
            warn(f"could not remove the suite's objects: {first(errors)}")
