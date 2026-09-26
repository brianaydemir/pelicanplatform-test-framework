"""Listings: PROPFIND at depths 0, 1, and infinity at each origin, each
director, and each cache, in every namespace; who may list /protected-a;
and an object that comes and goes.

Objects go under /protected-a/data/listings/<run>/, put straight in
every origin's store (see stores.seed(); and in /protected-b on a pstore
origin, which gives it storage of its own; /public reads /protected-a's).
The director redirects a PROPFIND to an origin. A cache answers Depth 0
from what it holds, or asks an origin, and relays deeper listings to an
origin.

Who may list depends on the namespace's capabilities (see may_list()):
an origin serves only direct clients, which a namespace without
DirectReads has none of (`-p origin-no-direct`); a cache answers Depth 0
of anything it may read; and anything deeper, at any server, needs
Listings. A PROPFIND that may not be answered must be refused, with 401,
403, or 405.
"""

import json
import os
from typing import AbstractSet, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from testlib import common, credentials, stores, web, webdav
from testlib.common import die, first
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
    "cache-body":           "each cache: Depth 1 with a propfind body, as the client sends",
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
# How a server refuses a PROPFIND.
REFUSALS = (401, 403, 405)


def may_list(caps: AbstractSet[str], depth: str, role: str) -> bool:
    """Whether a server in role (`origin`, `cache`, or `director`) should
    answer a PROPFIND at depth in a namespace with caps: an origin only
    direct clients, and only Depth 0 without Listings; a cache Depth 0 of
    anything it may read; and a director, by redirecting, only with
    Listings."""
    if role == "origin" and "DirectReads" not in caps:
        return False
    if depth == "0" and role != "director":
        return True
    return "Listings" in caps


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

    def caps(self, namespace: str) -> AbstractSet[str]:
        return self.fed.exports[namespace]

    def refusal(self, url: str, depth: str, token: Optional[str]) -> Optional[str]:
        """What is wrong with how url answers a PROPFIND at depth that it
        should refuse, or None."""
        answer = webdav.propfind(url, depth, token)
        if answer.status == 207:
            return f"Depth {depth}: listed it, but should have refused"
        if answer.status not in REFUSALS:
            return f"Depth {depth}: {answer.describe()}, not a refusal (401, 403, or 405)"
        return None

    def seed(self) -> List[str]:
        """Put the tree, and a probe object for each cache, in place; what
        went wrong."""
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
                    error = stores.seed(origin, ns, f"{self.base}/{rel}", os.urandom(size),
                                        self.tokens[ns], self.caps(ns))
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
                if not may_list(test.caps(ns), "0", "origin"):
                    why = test.refusal(origin_url(origin, target), "0", test.tokens[ns])
                    if why:
                        problems.append(f"{origin.svc} {target}: {why}")
                    continue
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
                elif want is not None and (entry.collection or entry.size != want):
                    problems.append(f"{origin.svc} {target}: {entry.size} bytes, not {want}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s)"


def origin_depth_1(test: Test) -> Tuple[str, str]:
    problems = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path(ns)
            url = origin_url(origin, f"{path}/")
            if may_list(test.caps(ns), "1", "origin"):
                why = test.compare(url, "1", path, CHILDREN, test.tokens[ns])
            else:
                why = test.refusal(url, "1", test.tokens[ns])
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
            url = origin_url(origin, f"{path}/")
            if may_list(test.caps(ns), "infinity", "origin"):
                why, note = infinity(test, url, path, test.tokens[ns])
            else:
                why, note = test.refusal(url, "infinity", test.tokens[ns]), "refused"
            if why:
                problems.append(f"{origin.svc} {path}/: {why}")
            elif note not in notes:
                notes.append(note)
    if problems:
        return FAIL, first(problems)
    return PASS, ", ".join(notes)


def director(test: Test) -> Tuple[str, str]:
    """A PROPFIND to a director comes back as a redirect to an origin,
    whose listing must then be right; or, where the namespace has no
    Listings, as a refusal."""
    origins = {urlsplit(o.url).netloc for o in test.fed.origins}
    problems = []
    for director_url in test.fed.directors:
        for ns in test.namespaces:
            path = test.path(ns)
            for target, depth, base, want in ((f"{path}/b", "0", f"{path}/b", None),
                                              (f"{path}/", "1", path, CHILDREN)):
                where = f"{urlsplit(director_url).netloc} {target}"
                if not may_list(test.caps(ns), depth, "director"):
                    answer = webdav.propfind(f"{director_url}{target}", depth, test.tokens[ns])
                    if answer.status not in REFUSALS:
                        problems.append(f"{where}: {answer.describe()}, not a refusal"
                                        " (401, 403, or 405)")
                    continue
                answer = webdav.propfind(f"{director_url}{target}", depth, test.tokens[ns])
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
                    elif set(got) != {""} or got[""].size != 5000:
                        problems.append(f"{where} -> {parts.netloc}: not the object")
                elif (why := test.compare(location, depth, base, want, test.tokens[ns])):
                    problems.append(f"{where} -> {parts.netloc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.fed.directors)} director(s)"


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
            elif set(got) != {""} or got[""].collection or got[""].size != PROBE:
                problems.append(f"{cache.svc} ({when}): not the {PROBE}-byte object")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def cache_depth_1(test: Test) -> Tuple[str, str]:
    problems: List[str] = []
    caps = test.caps("protected-a")
    for cache in test.caches:
        path = test.path("protected-a")
        url = f"{cache.url}{path}/"
        _, got, why = test.listing(url, "0", path, test.tokens["protected-a"])
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        if set(got) != {""} or not got[""].collection:
            problems.append(f"{cache.svc}: Depth 0 of {path}/ is not one collection")
            continue
        if not may_list(caps, "1", "cache"):
            if (why := test.refusal(url, "1", test.tokens["protected-a"])):
                problems.append(f"{cache.svc}: {why}")
            continue
        _, got, why = test.listing(url, "1", path, test.tokens["protected-a"])
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        wrong = webdav.problems(CHILDREN, got)
        if wrong:
            problems.append(f"{cache.svc}: Depth 1: {'; '.join(wrong)}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def cache_infinity(test: Test) -> Tuple[str, str]:
    problems, notes = [], []
    caps = test.caps("protected-a")
    for cache in test.caches:
        path = test.path("protected-a")
        url = f"{cache.url}{path}/"
        if may_list(caps, "infinity", "cache"):
            why, note = infinity(test, url, path, test.tokens["protected-a"])
        else:
            why, note = test.refusal(url, "infinity", test.tokens["protected-a"]), "refused"
        if why:
            problems.append(f"{cache.svc}: {why}")
        else:
            notes.append(f"{cache.svc}: {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, ", ".join(notes)


def cache_body(test: Test) -> Tuple[str, str]:
    """A cache relays a PROPFIND through the director, and must follow the
    director's redirect, with the request's body, to answer it."""
    problems = []
    caps = test.caps("protected-a")
    for cache in test.caches:
        path = test.path("protected-a")
        url = f"{cache.url}{path}/"
        if not may_list(caps, "1", "cache"):
            if (why := test.refusal(url, "1", test.tokens["protected-a"])):
                problems.append(f"{cache.svc}: {why}")
            continue
        answer, got, why = test.listing(url, "1", path, test.tokens["protected-a"],
                                        webdav.PROPFIND_BODY)
        if answer.status is not None and 300 <= answer.status < 400:
            problems.append(f"{cache.svc}: answered with the director's {answer.status}, to"
                            f" {urlsplit(answer.header('Location')).netloc}")
        elif got is None:
            problems.append(f"{cache.svc}: {why}")
        elif (wrong := webdav.problems(CHILDREN, got)):
            problems.append(f"{cache.svc}: {'; '.join(wrong)}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def servers(test: Test) -> List[Tuple[str, str, str]]:
    """Every origin and cache: its name, data URL, and role."""
    return ([(o.svc, o.url, "origin") for o in test.origins]
            + [(c.svc, c.url, "cache") for c in test.caches])


def refused_everywhere(test: Test, token: Optional[str]) -> Tuple[str, str]:
    """Listing /protected-a with token (none, or one for elsewhere) must be
    refused, of an object and of a collection, at every server: with 401
    or 403, or with 405 where the server would not list it anyway."""
    problems = []
    caps = test.caps("protected-a")
    for svc, url, role in servers(test):
        path = test.path("protected-a")
        for target, depth in ((f"{path}/b", "0"), (f"{path}/", "1")):
            answer = webdav.propfind(f"{url}{target}", depth, token)
            allowed = (401, 403) if may_list(caps, depth, role) else REFUSALS
            if answer.status == 207:
                problems.append(f"{svc} Depth {depth} {target}: listed it")
            elif answer.status not in allowed:
                problems.append(f"{svc} Depth {depth} {target}: {answer.describe()},"
                                f" not {' or '.join(map(str, allowed))}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers(test))} server(s)"


def protected_a_no_token(test: Test) -> Tuple[str, str]:
    return refused_everywhere(test, None)


def protected_a_scope(test: Test) -> Tuple[str, str]:
    return refused_everywhere(test, test.elsewhere)


def public_no_token(test: Test) -> Tuple[str, str]:
    """/public has PublicReads: anyone may list it, as far as its
    capabilities allow (see may_list())."""
    problems = []
    path = test.path("public")
    for svc, url, role in servers(test):
        if may_list(test.caps("public"), "1", role):
            why = test.compare(f"{url}{path}/", "1", path, CHILDREN, None)
        else:
            why = test.refusal(f"{url}{path}/", "1", None)
        if why:
            problems.append(f"{svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers(test))} server(s)"


def new_object(test: Test) -> Tuple[str, str]:
    """An object put straight in every origin's store is listed at once by
    each origin and cache (which relays Depth 1), and by the client; once
    removed from every origin, by none."""
    caps = test.caps("protected-a")
    if "Listings" not in caps:
        return SKIP, "/protected-a has no Listings"
    rel = f"{test.base}/tree/new"
    errors = stores.seed_everywhere(test.origins, "protected-a", rel, os.urandom(1234),
                                    test.tokens["protected-a"], caps)
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
        for svc, url, role in servers(test):
            if not may_list(caps, "1", role):
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
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    disk_stores = [o.store for o in stores.unique(fed.origins) if not o.pstore]
    for store in disk_stores:
        if not os.path.isdir(f"{store}/data/listings"):
            die(f"framework/var/{store}/data/listings is missing; run ./fed.sh init")
        # Earlier runs' objects come out first.
        common.empty_dir(f"{store}/data/listings")

    test = Test(session)
    print(f"Listing test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        errors = test.seed()
        if errors:
            die(f"could not put the tree in place: {errors[0]}")
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
            common.empty_dir(f"{store}/data/listings")
