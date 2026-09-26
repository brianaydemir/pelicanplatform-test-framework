"""Listings: PROPFIND at depths 0, 1, and infinity at each origin, each
director, and each cache, in every namespace; who may list /protected-a;
and an object that comes and goes.

Objects go under /protected-a/data/listings/<run>/, uploaded straight
to every origin (and to /protected-b on a pstore origin, which gives it
storage of its own; /public reads /protected-a's). The director
redirects every PROPFIND to an origin. A V2 cache relays Depth 1 to an
origin, and answers Depth 0 from what it holds; an XRootD cache refuses
to list a collection (409).
"""

import json
import os
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from testlib import common, credentials, stores, web, webdav
from testlib.common import first
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "origin-depth-0":       "each origin: Depth 0 of an object, and of a collection",
    "origin-depth-1":       "each origin: Depth 1 lists a collection's members",
    "origin-infinity":      "each origin: Depth infinity lists the whole tree, or is refused",
    "director":             "each director redirects a PROPFIND to an origin, which lists",
    "cache-depth-0":        "each cache: Depth 0 of an object it lacks, then of one it holds",
    "cache-depth-1":        "each cache: Depth 0 and 1 of a collection, as the origins list it",
    "cache-infinity":       "each cache: Depth infinity, as at the origins",
    "cache-body":           "each V2 cache: Depth 1 with a propfind body, as the client sends",
    "protected-a-no-token": "listing /protected-a with no token is refused everywhere",
    "protected-a-scope":    "listing /protected-a with a token for elsewhere is refused everywhere",
    "public-no-token":      "listing /public with no token is allowed everywhere",
    "new-object":           "an object is listed as soon as it is uploaded, and not once removed",
}

# The tree under <base>/tree: each object's size (paths below it).
TREE = {"a": 1000, "b": 5000, "empty": 0, "sub/c": 300, "sub/deeper/d": 7000}
# Depth 1 of tree/: sizes, and None for a collection.
CHILDREN = {"a": 1000, "b": 5000, "empty": 0, "sub": None}
# Depth infinity of tree/.
EVERYTHING = {**TREE, "sub": None, "sub/deeper": None}
# The size of the object each cache first sees through a PROPFIND.
PROBE = 2345


class Test:
    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.origins = stores.unique(session.fed.origins)
        self.caches = session.fed.caches
        self.run = session.run
        self.base = f"data/listings/{self.run}"
        self.namespaces = session.namespaces
        self.token_files = {ns: session.token_file(ns) for ns in self.namespaces}
        self.tokens = {ns: session.token(ns) for ns in self.namespaces}
        path = session.path("listings-elsewhere.token")
        credentials.mint(self.fed, path, "/protected-a/", self.fed.origin_key,
                         self.fed.issuer_of("protected-a"), f"/{self.base}/elsewhere",
                         credentials.LONG, credentials.R)
        with open(path) as f:
            self.elsewhere = f.read().strip()

    def path(self, namespace: str, rel: str = "tree") -> str:
        return f"/{namespace}/{self.base}/{rel}"

    def seed(self) -> List[str]:
        """Upload the tree, and a probe object for each cache; what went
        wrong."""
        errors: List[str] = []
        for origin in self.origins:
            for ns in stores.upload_namespaces(origin, self.fed.exports):
                for rel in ("tree/sub/deeper", "probe"):
                    errors += stores.make_dirs([origin], ns, f"{self.base}/{rel}", self.tokens[ns])
                if errors:
                    return errors
                objects = {f"tree/{p}": size for p, size in TREE.items()}
                objects.update({f"probe/{c.svc}": PROBE for c in self.caches})
                for rel, size in objects.items():
                    error = stores.put(origin, ns, f"{self.base}/{rel}", os.urandom(size),
                                       self.tokens[ns])
                    if error:
                        return [error]
        return errors

    def listing(self, url: str, depth: str, base: str, token: Optional[str],
                body: Optional[bytes] = None) -> Tuple[web.Response, Optional[Dict], str]:
        """PROPFIND url: the answer, and if it is a listing, its entries
        below base; else why not."""
        answer = webdav.propfind(url, depth, token, body)
        if answer.status != 207:
            return answer, None, f"Depth {depth}: {answer.describe()}, not 207"
        try:
            return answer, webdav.relative(webdav.parse(answer.body), base), ""
        except ValueError as e:
            return answer, None, f"Depth {depth}: {e}"

    def compare(self, url: str, depth: str, base: str, want: Mapping[str, Optional[int]],
                token: Optional[str], body: Optional[bytes] = None) -> Optional[str]:
        """What is wrong with url's listing at depth, against want."""
        _, got, why = self.listing(url, depth, base, token, body)
        if got is None:
            return why
        wrong = webdav.problems(want, got)
        return f"Depth {depth}: {'; '.join(wrong)}" if wrong else None

    def ls(self, rel: str) -> Tuple[Optional[Dict[str, Tuple[bool, int]]], str]:
        """`pelican object ls -l --json` of rel in /protected-a: each name's
        collection flag and size, or why it failed."""
        code, out, err = self.session.pelican_cmd(
            "object", "ls", "-l", "--json", "-t", self.token_files["protected-a"],
            f"{self.fed.url}/protected-a/{self.base}/{rel}")
        if code != 0:
            lines = err.strip().splitlines()
            return None, f"exit {code}: {lines[-1] if lines else ''}"
        for line in reversed(out.strip().splitlines()):
            try:
                entries = json.loads(line)
                return {e["Name"].rstrip("/").rsplit("/", 1)[-1]:
                        (bool(e.get("IsCollection")), e.get("Size") or 0) for e in entries}, ""
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return None, "no JSON in the output"


def origin_url(origin: common.Origin, path: str) -> str:
    return f"{origin.url}{path}"


#---------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.

def origin_depth_0(test: Test) -> Tuple[str, str]:
    problems = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path(ns)
            for rel, want in (("b", 5000), ("", None)):
                target = f"{path}/{rel}" if rel else f"{path}/"
                _, got, why = test.listing(origin_url(origin, target), "0",
                                           f"{path}/{rel}" if rel else path, test.tokens[ns])
                if got is None:
                    problems.append(f"{origin.svc} {target}: {why}")
                    continue
                entry = got.get("")
                if set(got) != {""} or entry is None:
                    problems.append(f"{origin.svc} {target}: Depth 0 listed {sorted(got)}")
                elif want is None and not entry.collection:
                    problems.append(f"{origin.svc} {target}: not a collection")
                elif want is not None and (entry.collection or (entry.size or 0) != want):
                    problems.append(f"{origin.svc} {target}: {entry.size} bytes, not {want}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s)"


def origin_depth_1(test: Test) -> Tuple[str, str]:
    problems = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path(ns)
            why = test.compare(origin_url(origin, f"{path}/"), "1", path, CHILDREN,
                               test.tokens[ns])
            if why:
                problems.append(f"{origin.svc} {path}/: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s)"


def infinity(test: Test, url: str, path: str, token: Optional[str]) -> Tuple[Optional[str], str]:
    """Depth infinity of path's tree at url: what is wrong, or None, and
    a note. RFC 4918 lets a server refuse it with 403."""
    answer, got, why = test.listing(url, "infinity", path, token)
    if answer.status == 403:
        return None, "refused (403)"
    if got is None:
        return why, ""
    if set(got) - {""} == set(CHILDREN):
        return "Depth infinity listed only the first level, as Depth 1 would", ""
    wrong = webdav.problems(EVERYTHING, got)
    return (f"Depth infinity: {'; '.join(wrong)}" if wrong else None), "the whole tree"


def origin_infinity(test: Test) -> Tuple[str, str]:
    problems, notes = [], []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path(ns)
            why, note = infinity(test, origin_url(origin, f"{path}/"), path, test.tokens[ns])
            if why:
                problems.append(f"{origin.svc} {path}/: {why}")
            elif note not in notes:
                notes.append(note)
    if problems:
        return FAIL, first(problems)
    return PASS, ", ".join(notes)


def director(test: Test) -> Tuple[str, str]:
    """A PROPFIND to a director comes back as a redirect to an origin,
    whose listing must then be right. An older director that answers
    with the listing itself passes, noted."""
    origins = {urlsplit(o.url).netloc for o in test.fed.origins}
    problems, notes = [], []
    for director_url in test.fed.directors:
        for ns in test.namespaces:
            path = test.path(ns)
            for target, depth, base, want in ((f"{path}/b", "0", f"{path}/b", None),
                                              (f"{path}/", "1", path, CHILDREN)):
                answer = webdav.propfind(f"{director_url}{target}", depth, test.tokens[ns])
                where = f"{urlsplit(director_url).netloc} {target}"
                if answer.status == 207:
                    notes.append(f"{where}: answered itself")
                    continue
                location = answer.header("Location")
                if answer.status not in (301, 302, 303, 307, 308) or not location:
                    problems.append(f"{where}: {answer.describe()}, not a redirect")
                    continue
                parts = urlsplit(location)
                if parts.netloc not in origins:
                    problems.append(f"{where}: redirected to {parts.netloc}, not an origin")
                    continue
                if not parts.path.rstrip("/").endswith(target.rstrip("/")):
                    problems.append(f"{where}: redirected to {parts.path}")
                    continue
                if want is None:
                    _, got, why = test.listing(location, depth, base, test.tokens[ns])
                    if got is None:
                        problems.append(f"{where} -> {parts.netloc}: {why}")
                    elif set(got) != {""} or (got[""].size or 0) != 5000:
                        problems.append(f"{where} -> {parts.netloc}: not the object")
                elif (why := test.compare(location, depth, base, want, test.tokens[ns])):
                    problems.append(f"{where} -> {parts.netloc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{len(test.fed.directors)} director(s)"] + notes)


def cache_depth_0(test: Test) -> Tuple[str, str]:
    """Each cache's own probe object: Depth 0 before the cache holds it,
    then after a GET."""
    problems = []
    for cache in test.caches:
        path = test.path("protected-a", f"probe/{cache.svc}")
        url = f"{cache.url}{path}"
        for when in ("cold", "cached"):
            if when == "cached":
                answer = web.request("GET", url, token=test.tokens["protected-a"])
                if answer.status != 200 or len(answer.body) != PROBE:
                    problems.append(f"{cache.svc}: GET {path}: {answer.describe()}")
                    break
            _, got, why = test.listing(url, "0", path, test.tokens["protected-a"])
            if got is None:
                problems.append(f"{cache.svc} ({when}): {why}")
            elif set(got) != {""} or got[""].collection or (got[""].size or 0) != PROBE:
                problems.append(f"{cache.svc} ({when}): not the {PROBE}-byte object")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def xrootd_409(cache: common.Cache, answer: web.Response) -> bool:
    """Whether this is an XRootD cache's known refusal to list a
    collection (client/handle_http.go in Pelican)."""
    return cache.kind == "xrootd" and answer.status == 409


def by_cache(problems: List[str], known: List[str], passed: List[str]) -> Tuple[str, str]:
    """The result of a scenario over the caches: a failure; else a skip
    if every cache was an XRootD one refusing, as it is known to; else a
    pass."""
    if problems:
        return FAIL, first(problems)
    if known and not passed:
        return SKIP, (f"known: XRootD caches answer 409 to a PROPFIND of a collection"
                      f" ({', '.join(known)})")
    return PASS, ", ".join(passed + [f"{svc}: 409 (known)" for svc in known])


def cache_depth_1(test: Test) -> Tuple[str, str]:
    problems: List[str] = []
    known: List[str] = []
    passed: List[str] = []
    for cache in test.caches:
        path = test.path("protected-a")
        url = f"{cache.url}{path}/"
        answer, got, why = test.listing(url, "0", path, test.tokens["protected-a"])
        if xrootd_409(cache, answer):
            known.append(cache.svc)
            continue
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        if set(got) != {""} or not got[""].collection:
            problems.append(f"{cache.svc}: Depth 0 of {path}/ is not one collection")
            continue
        answer, got, why = test.listing(url, "1", path, test.tokens["protected-a"])
        if xrootd_409(cache, answer):
            known.append(cache.svc)
            continue
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        wrong = webdav.problems(CHILDREN, got)
        if wrong:
            problems.append(f"{cache.svc}: Depth 1: {'; '.join(wrong)}")
            continue
        passed.append(cache.svc)
    return by_cache(problems, known, passed)


def cache_infinity(test: Test) -> Tuple[str, str]:
    problems: List[str] = []
    known: List[str] = []
    passed: List[str] = []
    for cache in test.caches:
        path = test.path("protected-a")
        answer = webdav.propfind(f"{cache.url}{path}/", "infinity", test.tokens["protected-a"])
        if xrootd_409(cache, answer):
            known.append(cache.svc)
            continue
        why, note = infinity(test, f"{cache.url}{path}/", path, test.tokens["protected-a"])
        if why:
            problems.append(f"{cache.svc}: {why}")
        else:
            passed.append(f"{cache.svc}: {note}")
    return by_cache(problems, known, passed)


def cache_body(test: Test) -> Tuple[str, str]:
    """A V2 cache relays a PROPFIND through the director, whose redirect
    it must follow even when the request has a body: without GetBody, Go
    hands the redirect back instead (local_cache/persistent_cache_api.go
    proxyPropfind in Pelican)."""
    v2 = [c for c in test.caches if c.kind == "v2"]
    if not v2:
        return SKIP, "no V2 cache"
    problems = []
    for cache in v2:
        path = test.path("protected-a")
        answer, got, why = test.listing(f"{cache.url}{path}/", "1", path, test.tokens["protected-a"],
                                        webdav.PROPFIND_BODY)
        if answer.status is not None and 300 <= answer.status < 400:
            problems.append(f"{cache.svc}: handed back the director's {answer.status}, to"
                            f" {urlsplit(answer.header('Location')).netloc}")
        elif got is None:
            problems.append(f"{cache.svc}: {why}")
        elif (wrong := webdav.problems(CHILDREN, got)):
            problems.append(f"{cache.svc}: {'; '.join(wrong)}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(v2)} V2 cache(s)"


def servers(test: Test) -> List[Tuple[str, str, Optional[common.Cache]]]:
    """Every origin and cache: its name, data URL, and if a cache, it."""
    return ([(o.svc, o.url, None) for o in test.origins]
            + [(c.svc, c.url, c) for c in test.caches])


def refused_everywhere(test: Test, token: Optional[str]) -> Tuple[str, str]:
    """Listing /protected-a with token (none, or one for elsewhere) must be
    refused, of an object and of a collection, at every server."""
    problems, notes = [], []
    for svc, url, cache in servers(test):
        path = test.path("protected-a")
        for target, depth in ((f"{path}/b", "0"), (f"{path}/", "1")):
            answer = webdav.propfind(f"{url}{target}", depth, token)
            if cache and xrootd_409(cache, answer) and depth == "1":
                notes.append(f"{svc}: 409 for the collection")
            elif answer.status == 207:
                problems.append(f"{svc} Depth {depth} {target}: listed it")
            elif answer.status not in (401, 403):
                problems.append(f"{svc} Depth {depth} {target}: {answer.describe()},"
                                " not 401 or 403")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{len(servers(test))} server(s)"] + notes)


def protected_a_no_token(test: Test) -> Tuple[str, str]:
    return refused_everywhere(test, None)


def protected_a_scope(test: Test) -> Tuple[str, str]:
    return refused_everywhere(test, test.elsewhere)


def public_no_token(test: Test) -> Tuple[str, str]:
    """/public has Listings and PublicReads: anyone may list it. An
    XRootD cache lists no collection, so it is asked for an object."""
    problems = []
    path = test.path("public")
    for svc, url, cache in servers(test):
        if cache and cache.kind == "xrootd":
            _, got, why = test.listing(f"{url}{path}/b", "0", f"{path}/b", None)
            if got is None:
                problems.append(f"{svc}: {why}")
            elif (got.get("") is None) or (got[""].size or 0) != 5000:
                problems.append(f"{svc}: Depth 0 of b is not the object")
        elif (why := test.compare(f"{url}{path}/", "1", path, CHILDREN, None)):
            problems.append(f"{svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers(test))} server(s)"


def new_object(test: Test) -> Tuple[str, str]:
    """An object uploaded straight to every origin is listed at once by
    each origin and V2 cache (which relays Depth 1), and by the client;
    once removed from every origin, by none."""
    rel = f"{test.base}/tree/new"
    errors = stores.put_everywhere(test.origins, "protected-a", rel, os.urandom(1234),
                                   test.tokens["protected-a"])
    if errors:
        return FAIL, errors[0]
    path = test.path("protected-a")
    problems = []
    for when, want in (("after the upload", {**CHILDREN, "new": 1234}),
                       ("after the removal", CHILDREN)):
        if when == "after the removal":
            for origin in test.origins:
                if (why := stores.delete(origin, "protected-a", rel, test.tokens["protected-a"])):
                    return FAIL, why
        for svc, url, cache in servers(test):
            if cache and cache.kind == "xrootd":
                continue
            if (why := test.compare(f"{url}{path}/", "1", path, want, test.tokens["protected-a"])):
                problems.append(f"{svc} {when}: {why}")
        listed, why = test.ls("tree")
        if listed is None:
            problems.append(f"`pelican object ls` {when}: {why}")
        elif ("new" in listed) != ("new" in want):
            problems.append(f"`pelican object ls` {when}: {'' if 'new' in listed else 'not '}"
                            "listed")
    if problems:
        return FAIL, first(problems)
    return PASS, "listed at once, and not once removed"


RUN = {
    "origin-depth-0": origin_depth_0, "origin-depth-1": origin_depth_1,
    "origin-infinity": origin_infinity, "director": director,
    "cache-depth-0": cache_depth_0, "cache-depth-1": cache_depth_1,
    "cache-infinity": cache_infinity, "cache-body": cache_body,
    "protected-a-no-token": protected_a_no_token, "protected-a-scope": protected_a_scope,
    "public-no-token": public_no_token, "new-object": new_object,
}


def skip(fed: common.Federation) -> Optional[str]:
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    disk_stores = [o.store for o in stores.unique(fed.origins) if not o.pstore]
    test = Test(session)
    print(f"Listing test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        for store in disk_stores:
            # Earlier runs' objects come out first.
            if os.path.isdir(f"{store}/data/listings"):
                common.empty_dir(f"{store}/data/listings")
        errors = test.seed()
        if errors:
            common.die(f"could not upload the tree: {errors[0]}")
        for name in selected:
            if name == "public-no-token" and "public" not in fed.exports:
                results.add(name, SKIP, "this shape does not export /public")
                continue
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            results.add(name, status, note)
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        for store in disk_stores:
            if os.path.isdir(f"{store}/data/listings"):
                common.empty_dir(f"{store}/data/listings")
