"""A V2 cache's own HTTP APIs, beside the objects it serves
(local_cache/persistent_cache_api.go and prestage_evict_api.go in
Pelican): who may call each, and what each does.

  prestage             each cache fetches an object whole for whoever may
                       read it, and for no one else; `pelican object
                       prestage` of an object, a collection, and a
                       missing object
  evict                each V2 cache evicts, or marks for eviction, what a
                       storage.modify token names, by whole path
                       segments, and nothing for anyone else; `pelican
                       object evict`
  admin-authz          each V2 cache's purge and introspection routes
                       answer its admin alone, and purging takes
                       localcache.purge too
  purge                each V2 cache purges to a target, and an object
                       marked purge-first goes first
  allowed-federations  each V2 cache serves its data routes for its own
                       federation alone, however spelled
  usage                each V2 cache's usage grows by what it stores, as
                       its store on disk and its introspection agree
  consistency          each V2 cache's verify finds an object corrupted
                       in its store, its scan drops it, and a stray file
                       in its store goes

The caches asked are the federated ones: a site-local cache serves the
same APIs, and the sitelocal suite asks it for objects. XRootD caches
serve none of these routes but prestage, through the xrdhttp-pelican
plugin, so only `prestage client` asks them, and judges them by their
.cinfo files. Objects go under /protected-a/data/cache-api/<run>/, put at
every origin (stores.seed_everywhere()), one set per cache, so that each
is cold where it is first read.

The routes that would destroy data are sent, in `admin-authz`, only
requests that would harm nothing were they let through: chaos aimed at
an object no cache holds, a consistency check with both its scans off,
and purges to a target of 0 or of no object. A refused one must get 401
or 403, and the admin's the route's own answer to such a request. Real
purges, chaos, and scans are in `purge` and `consistency`.
"""

import json
import os
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

from testlib import blocks, cacheapi, common, credentials, jwt, metrics, stores, web
from testlib.blocks import cache_dir, read_cinfo, store_bytes
from testlib.common import Findings, die, first, warn
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "prestage": "each cache prestages an object for a read token, and only then; `pelican object prestage`",
    "evict": "each V2 cache evicts, or marks, what a storage.modify token names, and nothing else",
    "admin-authz": "each V2 cache's purge and introspection routes answer its admin alone",
    "purge": "each V2 cache purges to a target, and an object marked purge-first goes first",
    "allowed-federations": "each V2 cache serves its data routes for its own federation alone",
    "usage": "each V2 cache's usage grows by what it stores, as its store and introspection say",
    "consistency": "each V2 cache finds an object corrupted in its store, drops it, and removes strays",
}

NAMESPACE = "protected-a"

# A small object, which a V2 cache stores in a file of its own, not
# inline in its database (InlineThreshold, 4096 bytes), and which spans
# several of its blocks and of an XRootD cache's.
OBJECT_SIZE = 3 * blocks.XROOTD_BLOCK + 7
# How long a cache may take to say that it holds an object it has read or
# prestaged whole.
HELD_WAIT = 30
# usage's objects: K of S bytes each, stored in S plus a tag per block.
USAGE_COUNT = 3
USAGE_SIZE = 100000
# How far two measures of a store's files may differ, taken a moment
# apart: the cache's own self-test objects come and go.
USAGE_SLACK = 64 << 10

ONLY_IF_CACHED = {"Cache-Control": "only-if-cached"}

# The answers that refuse a request for want of credentials.
REFUSED = (401, 403)

# A row: its status, and its note.
Row = tuple[str, str]


class Test:
    """The run's objects in /protected-a, the tokens, and the shape."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.base = f"data/cache-api/{session.run}"
        self.token = session.token(NAMESPACE)
        self.token_file = session.token_file(NAMESPACE)
        self.read_token = session.token(NAMESPACE, credentials.R)
        self.caps = self.fed.exports[NAMESPACE]
        self.origins = self.fed.origins
        self.caches = self.fed.federated_caches
        self.v2 = [c for c in self.caches if c.kind == "v2"]
        self.made: set[str] = set()
        self._admin: dict[tuple[str, tuple[str, ...]], str] = {}
        self._elsewhere: Optional[str] = None
        # The rows of `consistency` that corrupted() decides, once it has.
        self.corrupted: Optional[dict[str, Row]] = None

    def rel(self, *parts: str) -> str:
        """Where parts are, under the run's collection."""
        return "/".join([self.base, *parts])

    def path(self, rel: str) -> str:
        """rel's path in the federation."""
        return f"/{NAMESPACE}/{rel}"

    def url(self, server_url: str, rel: str) -> str:
        """rel in /protected-a at the server at server_url."""
        return f"{server_url}/{NAMESPACE}/{quote(rel)}"

    def put(self, objects: Mapping[str, bytes]) -> Optional[str]:
        """Put objects, by rel, at every origin, making their collections
        first; what went wrong, or None."""
        for rel, data in objects.items():
            parent = rel.rsplit("/", 1)[0]
            if parent not in self.made:
                errors = stores.make_dirs(self.origins, NAMESPACE, parent, self.token)
                if errors:
                    return errors[0]
                self.made.add(parent)
            errors = stores.seed_everywhere(
                self.origins, NAMESPACE, rel, data, self.token, self.caps
            )
            if errors:
                return errors[0]
        return None

    def get(self, cache: common.Cache, rel: str) -> web.Response:
        """A GET of rel through cache, with the token."""
        return web.request("GET", self.url(cache.url, rel), token=self.token, timeout=120)

    def held(self, cache: common.Cache, rel: str) -> Optional[bool]:
        """Whether a V2 cache holds all of rel, as a HEAD with
        only-if-cached says (504 if not); None if it says neither."""
        answer = web.request(
            "HEAD", self.url(cache.url, rel), token=self.token, headers=ONLY_IF_CACHED
        )
        if answer.status == 200:
            return True
        if answer.status == 504:
            return False
        return None

    def fill(self, cache: common.Cache, rel: str, data: bytes) -> Optional[str]:
        """Read rel whole through a V2 cache, which must serve data, until
        it says it holds it all; what went wrong, or None."""
        answer = self.get(cache, rel)
        if answer.status != 200 or answer.body != data:
            return f"{cache.svc}: a GET of {leaf(rel)} got {served(answer, data)}"
        if not common.wait_for(partial(holds, self, cache, rel), HELD_WAIT, interval=1):
            return f"{cache.svc} does not hold {leaf(rel)} {HELD_WAIT}s after reading it whole"
        return None

    def admin(self, cache: common.Cache, extra_scopes: tuple[str, ...] = ()) -> str:
        """The admin's token for cache (see cacheapi.admin_token())."""
        key = (cache.svc, extra_scopes)
        if key not in self._admin:
            self._admin[key] = cacheapi.admin_token(self.session, cache, extra_scopes)
        return self._admin[key]

    def purger(self, cache: common.Cache) -> str:
        """The admin's token for cache, with localcache.purge."""
        return self.admin(cache, (cacheapi.PURGE_SCOPE,))

    def elsewhere(self) -> str:
        """A token like the `server` token, but scoped to /elsewhere/."""
        if self._elsewhere is None:
            self._elsewhere = credentials.mint_server(
                self.fed,
                self.session.path("tokens/cache-api.elsewhere"),
                NAMESPACE,
                scope_path="/elsewhere/",
            )
        return self._elsewhere

    def object_url(self, rel: str) -> str:
        """The URL by which a V2 cache keys rel (see cacheapi.object_url())."""
        return cacheapi.object_url(self.fed, self.path(rel))


def holds(test: Test, cache: common.Cache, rel: str) -> bool:
    """Whether a V2 cache holds all of rel (see Test.held())."""
    return test.held(cache, rel) is True


def leaf(rel: str) -> str:
    """rel's last segment, for a message."""
    return rel.rsplit("/", 1)[-1]


def served(answer: web.Response, data: bytes) -> str:
    """What answer was, briefly, given that it should have held data."""
    if answer.status != 200:
        return answer.describe()
    return "the object" if answer.body == data else f"{len(answer.body)} bytes, not the object"


def text(answer: web.Response) -> str:
    """answer's body, decoded, briefly."""
    return answer.body[:300].decode(errors="replace").strip()


def held_word(found: Optional[bool]) -> str:
    """What Test.held() found, for a message."""
    if found is None:
        return "neither held nor not (only-if-cached got no 200 or 504)"
    return "held" if found else "not held"


# --------------------------------------------------------------------------
# prestage


def prestage(cache: common.Cache, path: Optional[str], token: Optional[str]) -> web.Response:
    """GET the prestage API of cache for path (none: no `path` at all),
    with token, which streams its progress until the object is whole."""
    query = "" if path is None else "?" + urlencode({"path": path})
    return web.request(
        "GET", f"{cache.web_url}{cacheapi.PRESTAGE}{query}", token=token, timeout=120
    )


def lines(answer: web.Response) -> list[str]:
    """The lines of answer's body that aren't blank."""
    return [
        line.strip()
        for line in answer.body.decode(errors="replace").splitlines()
        if line.strip()
    ]


def progress_problem(answer: web.Response) -> Optional[str]:
    """Why answer is not a prestage that went well: 200, chunked
    text/plain, `status: queued` and `status: active,offset=N` lines,
    and last `success: ok` (prestageHandler); or None."""
    if answer.status != 200:
        return f"{answer.describe()}: {text(answer)}"
    if not answer.header("Content-Type").startswith("text/plain"):
        return f"Content-Type '{answer.header('Content-Type')}', not text/plain"
    if "chunked" not in answer.header("Transfer-Encoding").lower():
        return "not chunked"
    found = lines(answer)
    if not found or found[-1] != "success: ok":
        return f"ending '{found[-1] if found else '(nothing)'}', not 'success: ok'"
    for line in found[:-1]:
        if line != "status: queued" and not (
            line.startswith("status: active,offset=")
            and line[len("status: active,offset=") :].isdigit()
        ):
            return f"a line '{line}', which is no status"
    return None


def prestage_probe(test: Test) -> Row:
    """At each V2 cache: no `path` (how the client asks whether a cache
    has the API) and a relative path must each get 400."""
    problems: list[str] = []
    for cache in test.v2:
        for what, path in (("no path", None), ("a relative path", test.rel("probe"))):
            answer = prestage(cache, path, test.read_token)
            if answer.status != 400:
                problems.append(f"{cache.svc}, {what}: {answer.describe()}, not 400")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def prestage_refused(test: Test) -> Row:
    """At each V2 cache, an object it lacks: a prestage with no token, and
    with one scoped elsewhere, must get 403, and leave it uncached."""
    rels = {c.svc: test.rel("prestage", f"refused-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        for what, token in (("no token", None), ("a token scoped elsewhere", test.elsewhere())):
            answer = prestage(cache, test.path(rel), token)
            if answer.status != 403:
                problems.append(f"{cache.svc}, {what}: {answer.describe()}, not 403")
        time.sleep(2)
        found = test.held(cache, rel)
        if found is not False:
            problems.append(f"{cache.svc}: the object is {held_word(found)} afterward")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def prestage_object(test: Test) -> Row:
    """At each V2 cache, an object it lacks (only-if-cached: 504): a
    prestage with a read token must stream its progress and succeed
    (progress_problem()), after which the cache must hold the object,
    and serve it; and a prestage of it again must succeed too."""
    rels = {c.svc: test.rel("prestage", f"object-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.v2:
        rel, data = rels[cache.svc], objects[rels[cache.svc]]
        found = test.held(cache, rel)
        if found is not False:
            problems.append(
                f"{cache.svc}: the object is {held_word(found)} before its prestage"
            )
            continue
        why = progress_problem(prestage(cache, test.path(rel), test.read_token))
        if why:
            problems.append(f"{cache.svc}: the prestage: {why}")
            continue
        if not common.wait_for(partial(holds, test, cache, rel), HELD_WAIT, interval=1):
            problems.append(f"{cache.svc} does not hold the object after its prestage")
        answer = test.get(cache, rel)
        if answer.status != 200 or answer.body != data:
            problems.append(f"{cache.svc}: a GET afterward got {served(answer, data)}")
        why = progress_problem(prestage(cache, test.path(rel), test.read_token))
        if why:
            problems.append(f"{cache.svc}: a second prestage: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def prestage_missing(test: Test) -> Row:
    """At each V2 cache, a prestage of an object that no origin holds: it
    has already said 200 by the time it knows, so its last line must say
    the fetch failed, as the origin did: `failure: 404(Not Found): ...`
    (prestageHandler, and the xrdhttp-pelican plugin's protocol)."""
    problems: list[str] = []
    for cache in test.v2:
        answer = prestage(
            cache, test.path(test.rel("prestage", f"missing-{cache.svc}")), test.read_token
        )
        found = lines(answer)
        last = found[-1] if found else "(nothing)"
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()}, not 200: {text(answer)}")
        elif not last.startswith("failure: 404"):
            problems.append(
                f"{cache.svc}: ending '{last[:200]}', not 'failure: 404(Not Found): ...'"
            )
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def prestage_name(test: Test) -> Row:
    """At each V2 cache, an object named with a `+` beside a decoy named
    with a space there, both uncached: a prestage of the first, its path
    encoded once in the query, as the client sends it, must leave the
    cache holding it, and not the decoy. A server that decoded the path
    twice would take the `+` for a space."""
    names = {c.svc: (f"plus+{c.svc}", f"plus {c.svc}") for c in test.v2}
    objects = {
        test.rel("prestage", name): os.urandom(OBJECT_SIZE)
        for pair in names.values()
        for name in pair
    }
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.v2:
        wanted, decoy = (test.rel("prestage", name) for name in names[cache.svc])
        answer = prestage(cache, test.path(wanted), test.read_token)
        if answer.status != 200:
            problems.append(
                f"{cache.svc}: the prestage got {answer.describe()}: {text(answer)}"
            )
            continue
        common.wait_for(partial(holds, test, cache, wanted), 10, interval=1)
        for rel, want in ((wanted, True), (decoy, False)):
            found = test.held(cache, rel)
            if found is not want:
                problems.append(f"{cache.svc}: '{leaf(rel)}' is {held_word(found)} afterward")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def whole_at(test: Test, cache: common.Cache, rel: str) -> bool:
    """Whether cache holds all of rel: a V2 cache by only-if-cached, an
    XRootD cache by its .cinfo (blocks.read_cinfo())."""
    if cache.kind == "v2":
        return test.held(cache, rel) is True
    cinfo, _ = read_cinfo(cache, rel)
    return cinfo is not None and cinfo.complete


def client_prestage(test: Test, cache: common.Cache, rel: str) -> tuple[int, str]:
    """`pelican object prestage` of rel through cache: its exit status,
    and the last line it printed."""
    code, out, err = test.session.pelican_cmd(
        "object",
        "prestage",
        "--cache",
        cache.url,
        "--token",
        test.token_file,
        f"{test.fed.url}/{NAMESPACE}/{rel}",
    )
    return code, last_line(err or out)


def prestage_client(test: Test) -> Row:
    """At each cache, V2 or XRootD: `pelican object prestage` of an object
    must exit 0, and leave the cache holding it (an XRootD cache's .cinfo
    showing every block); of a collection, where /protected-a lists and
    takes direct clients, so that the client can list it, every object in
    it; and of a missing object it must exit non-zero."""
    listable = {"Listings", "DirectReads"} <= test.caps
    rels: dict[str, list[str]] = {}
    for cache in test.caches:
        found = [test.rel("prestage", f"client-{cache.svc}")]
        if listable:
            found += [test.rel("prestage", f"tree-{cache.svc}", n) for n in ("a", "b")]
        rels[cache.svc] = found
    objects = {rel: os.urandom(OBJECT_SIZE) for found in rels.values() for rel in found}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.caches:
        single, *tree = rels[cache.svc]
        jobs = [(single, [single])]
        if tree:
            jobs.append((test.rel("prestage", f"tree-{cache.svc}") + "/", tree))
        for target, wanted in jobs:
            code, said = client_prestage(test, cache, target)
            if code != 0:
                problems.append(
                    f"{cache.svc}: prestage of {leaf(target.rstrip('/'))} exited {code}: {said}"
                )
                continue
            for rel in wanted:
                if not common.wait_for(partial(whole_at, test, cache, rel), HELD_WAIT):
                    problems.append(f"{cache.svc} does not hold {leaf(rel)} after the prestage")
        code, said = client_prestage(test, cache, test.rel("prestage", f"absent-{cache.svc}"))
        if code == 0:
            problems.append(f"{cache.svc}: prestage of a missing object exited 0: {said}")
    note = f"{len(test.caches)} cache(s)" + (
        "" if listable else "; no collection, since /protected-a cannot be listed directly"
    )
    if problems:
        return FAIL, first(problems)
    return PASS, note


# --------------------------------------------------------------------------
# evict


def evict(
    cache: common.Cache,
    path: str,
    token: Optional[str],
    immediate: bool = True,
    method: str = "POST",
) -> web.Response:
    """method of the eviction API of cache for path, with token."""
    query = {"path": path, **({"immediate": "true"} if immediate else {})}
    return web.request(
        method, f"{cache.web_url}{cacheapi.EVICT}?{urlencode(query)}", token=token
    )


def evicted_message(path: str, count: int, immediate: bool) -> str:
    """What the eviction API says it did (evictHandler)."""
    if immediate:
        return f"Evicted {count} objects under {path}"
    return f"Marked {count} objects for priority eviction under {path}"


def fill_all(
    test: Test, objects: Mapping[str, bytes], caches: Sequence[common.Cache]
) -> Optional[str]:
    """Put objects at every origin, and have each of caches hold every one;
    what went wrong, or None."""
    why = test.put(objects)
    if why:
        return why
    for cache in caches:
        for rel, data in objects.items():
            why = test.fill(cache, rel, data)
            if why:
                return why
    return None


def evict_refused(test: Test) -> Row:
    """At each V2 cache, an object it holds: an immediate eviction with no
    token, a read-only token, or one scoped elsewhere must get 403, and
    leave it held; as must one of an object in /public, with no token,
    and with /protected-a's, which is for another namespace, whether or
    not /public takes writes (`-p origin-public-writes`)."""
    problems: list[str] = []
    rels = {c.svc: test.rel("evict", f"refused-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    for cache in test.v2:
        why = fill_all(test, {rels[cache.svc]: objects[rels[cache.svc]]}, [cache])
        if why:
            return FAIL, why
    public, no_public = public_object(test)
    for cache in test.v2:
        rel = rels[cache.svc]
        for what, token in (
            ("no token", None),
            ("a read-only token", test.read_token),
            ("a token scoped elsewhere", test.elsewhere()),
        ):
            answer = evict(cache, test.path(rel), token)
            if answer.status != 403:
                problems.append(f"{cache.svc}, {what}: {answer.describe()}, not 403")
        found = test.held(cache, rel)
        if found is not True:
            problems.append(f"{cache.svc}: the object is {held_word(found)} afterward")
        if public:
            path, data = public
            answer = web.request("GET", f"{cache.url}{quote(path)}", timeout=120)
            if answer.status != 200 or answer.body != data:
                problems.append(f"{cache.svc}: a GET of {path} got {served(answer, data)}")
                continue
            for what, token in (("no token", None), ("/protected-a's token", test.token)):
                answer = evict(cache, path, token)
                if answer.status != 403:
                    problems.append(
                        f"{cache.svc}, {path} with {what}: {answer.describe()}, not 403"
                    )
            answer = web.request("HEAD", f"{cache.url}{quote(path)}", headers=ONLY_IF_CACHED)
            if answer.status != 200:
                problems.append(
                    f"{cache.svc}: only-if-cached of {path} got {answer.describe()}"
                )
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)" + (
        f"; no /public object: {no_public}" if no_public else ""
    )


def public_object(test: Test) -> tuple[Optional[tuple[str, bytes]], str]:
    """An object of the run's own in /public, put at every origin (with
    /public's own token, where it takes writes): its path in the
    federation, and its bytes; or None, and why not."""
    caps = test.fed.exports.get("public")
    if caps is None:
        return None, "/public is not exported"
    if any(o.pstore for o in test.origins):
        return None, "/public takes no writes, and a pstore origin's store is encrypted"
    rel = test.rel("public-object")
    data = os.urandom(OBJECT_SIZE)
    token = test.session.token("public")
    errors = stores.make_dirs(test.origins, "public", rel.rsplit("/", 1)[0], token)
    errors += stores.seed_everywhere(test.origins, "public", rel, data, token, caps)
    if errors:
        return None, f"could not put an object in /public: {errors[0]}"
    return (f"/public/{rel}", data), ""


def evict_immediate(test: Test) -> Row:
    """At each V2 cache, three objects it holds: an immediate eviction of
    each, by GET, POST, and DELETE, with the `server` token (which has
    storage.modify) must get 200, `Evicted 1 objects under <path>`, after
    which the cache must not hold it, and a GET must fetch it again."""
    methods = ("GET", "POST", "DELETE")
    rels = {
        (c.svc, m): test.rel("evict", f"now-{m.lower()}-{c.svc}")
        for c in test.v2
        for m in methods
    }
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    problems: list[str] = []
    for cache in test.v2:
        mine = {rels[(cache.svc, m)]: objects[rels[(cache.svc, m)]] for m in methods}
        why = fill_all(test, mine, [cache])
        if why:
            return FAIL, why
        for method in methods:
            rel = rels[(cache.svc, method)]
            answer = evict(cache, test.path(rel), test.token, method=method)
            want = evicted_message(test.path(rel), 1, True)
            if answer.status != 200 or text(answer) != want:
                problems.append(
                    f"{cache.svc}, {method}: {answer.describe()} '{text(answer)}', not 200 '{want}'"
                )
            found = test.held(cache, rel)
            if found is not False:
                problems.append(
                    f"{cache.svc}, {method}: the object is {held_word(found)} afterward"
                )
            answer = test.get(cache, rel)
            if answer.status != 200 or answer.body != objects[rel]:
                problems.append(
                    f"{cache.svc}, {method}: a GET afterward got {served(answer, objects[rel])}"
                )
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s), by {', '.join(methods)}"


def evict_marked(test: Test) -> Row:
    """At each V2 cache, an object it holds: an eviction without
    `immediate` must get 200, `Marked 1 objects for priority eviction
    under <path>`, and leave it held until the cache next evicts."""
    rels = {c.svc: test.rel("evict", f"marked-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    problems: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        why = fill_all(test, {rel: objects[rel]}, [cache])
        if why:
            return FAIL, why
        answer = evict(cache, test.path(rel), test.token, immediate=False)
        want = evicted_message(test.path(rel), 1, False)
        if answer.status != 200 or text(answer) != want:
            problems.append(
                f"{cache.svc}: {answer.describe()} '{text(answer)}', not 200 '{want}'"
            )
        found = test.held(cache, rel)
        if found is not True:
            problems.append(f"{cache.svc}: the object is {held_word(found)} afterward")
        unmark(test, rel)
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def unmark(test: Test, rel: str) -> None:
    """Evict rel at once from every V2 cache, which drops its mark for
    priority eviction too, so that `purge first` finds only its own."""
    for cache in test.v2:
        evict(cache, test.path(rel), test.token)


def evict_prefix(test: Test) -> Row:
    """At each V2 cache, objects foo/a, foo/b, and foobar that it holds: an
    immediate eviction of foo must get `Evicted 2 objects`, and leave the
    cache holding foobar alone, since a prefix matches whole path segments
    (EvictPrefix)."""
    problems: list[str] = []
    for cache in test.v2:
        base = test.rel("evict", f"prefix-{cache.svc}")
        objects = {f"{base}/{n}": os.urandom(OBJECT_SIZE) for n in ("foo/a", "foo/b", "foobar")}
        why = fill_all(test, objects, [cache])
        if why:
            return FAIL, why
        answer = evict(cache, test.path(f"{base}/foo"), test.token)
        want = evicted_message(test.path(f"{base}/foo"), 2, True)
        if answer.status != 200 or text(answer) != want:
            problems.append(
                f"{cache.svc}: {answer.describe()} '{text(answer)}', not 200 '{want}'"
            )
        for name, want_held in (("foo/a", False), ("foo/b", False), ("foobar", True)):
            found = test.held(cache, f"{base}/{name}")
            if found is not want_held:
                problems.append(f"{cache.svc}: {name} is {held_word(found)} afterward")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def evict_name(test: Test) -> Row:
    """At each V2 cache, an object named with a `+` and a decoy named with
    a space there, both held: an immediate eviction of the first, its path
    encoded once, must evict it, and not the decoy (see prestage_name())."""
    problems: list[str] = []
    for cache in test.v2:
        wanted, decoy = (
            test.rel("evict", n) for n in (f"plus+{cache.svc}", f"plus {cache.svc}")
        )
        objects = {wanted: os.urandom(OBJECT_SIZE), decoy: os.urandom(OBJECT_SIZE)}
        why = fill_all(test, objects, [cache])
        if why:
            return FAIL, why
        answer = evict(cache, test.path(wanted), test.token)
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()} '{text(answer)}', not 200")
        for rel, want_held in ((wanted, False), (decoy, True)):
            found = test.held(cache, rel)
            if found is not want_held:
                problems.append(
                    f"{cache.svc}: '{leaf(rel)}' is {held_word(found)} afterward ('{text(answer)}')"
                )
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def client_evict(test: Test, rel: str, immediate: bool) -> tuple[int, str]:
    """`pelican object evict` of rel, which asks the first cache the
    director names: its exit status, and its output's last line."""
    args = ["object", "evict", "--token", test.token_file]
    if immediate:
        args.append("--immediate")
    code, out, err = test.session.pelican_cmd(*args, f"{test.fed.url}/{NAMESPACE}/{rel}")
    return code, last_line(out if code == 0 else err or out)


def evict_client(test: Test) -> Row:
    """`pelican object evict --immediate` of an object that every V2 cache
    holds must exit 0, print `Evicted 1 objects under <path>`, and leave
    one cache without it (the client asks the first that the director
    names), and the others holding it; without --immediate, of another,
    it must print `Marked 1 objects ...`, and leave every cache holding
    it. Each marked object is then evicted (see unmark())."""
    rels = {flag: test.rel("evict", f"client-{flag}") for flag in ("now", "marked")}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = fill_all(test, objects, test.v2)
    if why:
        return FAIL, why
    problems: list[str] = []
    for flag, rel in rels.items():
        immediate = flag == "now"
        code, said = client_evict(test, rel, immediate)
        want = evicted_message(test.path(rel), 1, immediate)
        if code != 0 or said != want:
            problems.append(f"{flag}: exited {code}, saying '{said}', not '{want}'")
            continue
        gone = [c.svc for c in test.v2 if test.held(c, rel) is False]
        if immediate and len(gone) != 1:
            problems.append(
                f"{flag}: {len(gone)} cache(s) lack it afterward ({', '.join(gone)}), not 1"
            )
        elif not immediate and gone:
            problems.append(f"{flag}: {', '.join(gone)} lack(s) it afterward")
        if not immediate:
            unmark(test, rel)
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


# --------------------------------------------------------------------------
# admin-authz


def other_admin(test: Test, cache: common.Cache) -> tuple[str, str]:
    """Another server's admin token, which cache must take for no one, and
    whose it is: the first origin's, whose local issuer and key are its
    own; or, where every server shares one process or key (`tiny`), one
    naming cache's local issuer but signed by test-keys/unknown.pem."""
    own = jwt.public_jwk(common.signing_key(f"issuer-keys/{cache.svc}"))
    for origin in test.origins:
        key = common.signing_key(f"issuer-keys/{origin.svc}")
        if origin.web_url != cache.web_url and jwt.public_jwk(key) != own:
            issuer = credentials.local_issuer(test.fed, origin.web_url)
            out = test.session.path(f"tokens/cache-api.other.{cache.svc}")
            return credentials.mint_user(test.fed, out, issuer, key, "admin"), f"{origin.svc}'s"
    issuer = credentials.local_issuer(test.fed, cache.web_url)
    out = test.session.path(f"tokens/cache-api.unknown.{cache.svc}")
    key = credentials.keys(test.fed)["unknown"]
    return credentials.mint_user(test.fed, out, issuer, key, "admin"), "unknown.pem's"


# Each admin route, by row: its method, its path and query (given the URL
# of an object that no cache holds, {url}), its body, whether it purges,
# and what its admin must get for that request; None for no request as
# the admin, for /purge, which `purge` sends.
AdminRoute = tuple[str, str, str, Optional[bytes], bool, Optional[tuple[int, ...]]]
ADMIN_ROUTES: tuple[AdminRoute, ...] = (
    ("purge", "POST", cacheapi.PURGE, None, True, None),
    ("purge-first", "POST", cacheapi.PURGE_FIRST, b"", True, (400,)),
    ("purge-to-target", "POST", cacheapi.PURGE_TO_TARGET, b'{"target_bytes": 0}', True, (400,)),
    ("list", "GET", cacheapi.INTROSPECT + "/list?limit=1&pattern={url}", None, False, (200,)),
    ("etags", "GET", cacheapi.INTROSPECT + "/etags?url={url}", None, False, (200,)),
    ("metadata", "GET", cacheapi.INTROSPECT + "/metadata?url={url}", None, False, (404,)),
    ("stats", "GET", cacheapi.INTROSPECT + "/stats", None, False, (200,)),
    ("verify", "POST", cacheapi.INTROSPECT + "/verify?url={url}", None, False, (404,)),
    ("disk-usage", "POST", cacheapi.INTROSPECT + "/disk-usage", None, False, (200,)),
    (
        "consistency",
        "POST",
        cacheapi.INTROSPECT + "/consistency?metadata=false&data=false",
        None,
        False,
        (400,),
    ),
    (
        "chaos",
        "POST",
        cacheapi.INTROSPECT + "/chaos?op=corrupt&block=0&bytes=16&url={url}",
        None,
        False,
        (400,),
    ),
)


def send(route: AdminRoute, url: str, token: Optional[str]) -> web.Response:
    """route's request, to url, with token."""
    _, method, _, body, _, _ = route
    return web.request(
        method, url, token=token, upload=body, headers=cacheapi.JSON if body else None
    )


def admin_authz(test: Test, results: common.Results) -> None:
    """At each V2 cache, each admin route (ADMIN_ROUTES), with a request
    that would harm nothing were it let through: with no token, it must
    get 401; with the namespace's `server` token, and another server's
    admin token, 401 or 403; with the cache's admin token, the route's own
    answer to the request, but at a purge route, 403, since purging also
    takes localcache.purge; and with that scope too, the route's own
    answer. A row per cache and route; `chaos` skips at a cache whose
    admin finds no such route, as where Cache.EnableChaosAPI is off."""
    nowhere = quote(test.object_url(test.rel("admin-authz", "nowhere")), safe="")
    for cache in test.v2:
        other, whose = other_admin(test, cache)
        for route in ADMIN_ROUTES:
            name, _, path, _, purges, admin_gets = route
            row = f"admin-authz {cache.svc} {name}"
            url = cache.web_url + path.format(url=nowhere)

            if name == "chaos" and send(route, url, test.admin(cache)).status == 404:
                results.add(
                    row, SKIP, "no chaos API (Cache.EnableChaosAPI; Pelican v7.27.0 on)"
                )
                continue
            tries: list[tuple[str, Optional[str], tuple[int, ...]]] = [
                ("no token", None, (401,)),
                ("the server token", test.token, REFUSED),
                (f"{whose} admin token", other, REFUSED),
            ]
            if purges:
                tries.append(("the admin token", test.admin(cache), (403,)))
                if admin_gets:
                    tries.append(
                        (
                            "the admin token with localcache.purge",
                            test.purger(cache),
                            admin_gets,
                        )
                    )
            elif admin_gets:
                tries.append(("the admin token", test.admin(cache), admin_gets))
            problems: list[str] = []
            for what, token, wanted in tries:
                answer = send(route, url, token)
                if answer.status not in wanted:
                    shown = " or ".join(str(n) for n in wanted)
                    problems.append(f"{what}: {answer.describe()}, not {shown}")
            if problems:
                results.add(row, FAIL, first(problems))
            else:
                results.add(row, PASS, f"{len(tries)} credentials")


# --------------------------------------------------------------------------
# purge


# How many times, and how far apart, a purge is asked while a cache is
# already evicting.
PURGE_TRIES, PURGE_INTERVAL = 3, 5
# How many purges `purge first` makes until its marked object goes.
MARKED_PURGES = 3


def total_usage(cache: common.Cache) -> Optional[int]:
    """What cache says it holds, in bytes (TotalUsage, from its counters in
    memory), or None."""
    found = cacheapi.stats(cache)
    if found is None or not isinstance(found.get("TotalUsage"), int):
        return None
    return int(found["TotalUsage"])


def charged(cache: common.Cache) -> Optional[int]:
    """What cache charges its storage directories, in bytes, by the usage
    counters in its database (NamespaceUsage, keyed `s<storage>:ns<ID>`,
    of the storage that DirStats names, not inline storage, s0), which
    its purges go by (getDirUsage in local_cache/eviction.go); or None."""
    found = cacheapi.stats(cache) or {}
    dirs = common.as_object(found.get("DirStats"))
    usage = common.as_object(found.get("NamespaceUsage"))
    if not dirs or usage is None:
        return None
    return sum(
        value
        for key, value in usage.items()
        if isinstance(value, int) and key.split(":", 1)[0][1:] in dirs
    )


def purge_request(test: Test, cache: common.Cache, path: str, body: Any) -> web.Response:
    """A POST of body, as JSON (none for None), to purge route path at
    cache, with its admin's token with localcache.purge. A cache already
    evicting answers a purge 500 (forcePurgeToTargets), so one that does
    is asked again, up to PURGE_TRIES times."""
    data = None if body is None else json.dumps(body).encode()
    answer = web.Response(None)
    for n in range(PURGE_TRIES):
        if n:
            time.sleep(PURGE_INTERVAL)
        answer = web.request(
            "POST",
            cache.web_url + path,
            token=test.purger(cache),
            upload=data,
            headers=cacheapi.JSON if data is not None else None,
            timeout=300,
        )
        if answer.status != 500:
            break
    return answer


def purge_to_target(test: Test) -> Row:
    """At each V2 cache, holding objects of the run's: purge_to_target with
    a target of 0 must get 400; with a target a byte below what it holds
    (charged()), 200 with `freed_bytes` above 0, after which it must hold
    no more than the target."""
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        objects = {
            test.rel("purge", f"target-{cache.svc}", n): os.urandom(OBJECT_SIZE) for n in "abc"
        }
        why = fill_all(test, objects, [cache])
        if why:
            return FAIL, why
        answer = purge_request(test, cache, cacheapi.PURGE_TO_TARGET, {"target_bytes": 0})
        if answer.status != 400:
            problems.append(f"{cache.svc}, a target of 0: {answer.describe()}, not 400")
        usage = charged(cache)
        if not usage:
            problems.append(f"{cache.svc}'s stats give no usage of its storage")
            continue
        target = usage - 1
        answer = purge_request(test, cache, cacheapi.PURGE_TO_TARGET, {"target_bytes": target})
        said = common.as_object(cacheapi.decoded(answer)) or {}
        freed = said.get("freed_bytes")
        if answer.status != 200 or not isinstance(freed, int) or freed <= 0:
            problems.append(
                f"{cache.svc}, a target of {target}: {answer.describe()} {text(answer)}"
            )
            continue
        after = charged(cache)
        if after is None or after > target:
            problems.append(f"{cache.svc} holds {after} bytes after a purge to {target}")
        notes.append(f"{cache.svc} freed {freed} of {usage} bytes")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


def purge_first(test: Test) -> Row:
    """At each V2 cache, holding three objects of the run's, read in turn:
    purge_first of the last read must get 200; then a purge to a byte
    below what the cache holds must evict that object first, and leave the
    other two held (EvictByLRU in local_cache/database.go drains objects
    marked purge-first before its LRU walk)."""
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        rels = [test.rel("purge", f"first-{cache.svc}", n) for n in "abc"]
        objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels}
        why = fill_all(test, objects, [cache])
        if why:
            return FAIL, why
        marked = rels[-1]
        answer = purge_request(test, cache, cacheapi.PURGE_FIRST, {"path": test.path(marked)})
        if answer.status != 200:
            problems.append(f"{cache.svc}: purge_first got {answer.describe()} {text(answer)}")
            continue
        why, purges = purge_marked(test, cache, marked)
        if why:
            problems.append(f"{cache.svc}: {why}")
            continue
        if purges > 1:
            notes.append(f"{cache.svc} took {purges} purges, another object being marked too")
        for rel in rels:
            found = test.held(cache, rel)
            if found is not (rel != marked):
                problems.append(f"{cache.svc}: {leaf(rel)} is {held_word(found)} afterward")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{len(test.v2)} V2 cache(s)"] + notes)


def purge_marked(test: Test, cache: common.Cache, marked: str) -> tuple[Optional[str], int]:
    """Purge cache to a byte below what it holds, which evicts an object
    marked purge-first, until it no longer holds marked, up to
    MARKED_PURGES times, since an object that an earlier run marked, and
    left, may go first: why that failed, if it did, and how many purges
    it took."""
    for n in range(1, MARKED_PURGES + 1):
        usage = charged(cache)
        if not usage:
            return "its stats give no usage of its storage", n
        answer = purge_request(
            test, cache, cacheapi.PURGE_TO_TARGET, {"target_bytes": usage - 1}
        )
        if answer.status != 200:
            return f"the purge got {answer.describe()} {text(answer)}", n
        if test.held(cache, marked) is not True:
            return None, n
    return None, MARKED_PURGES


def purge_all(test: Test) -> Row:
    """At each V2 cache, a purge (POST /api/v1.0/cache/purge) must get
    200; where its store is bounded (`-p cache-small`), it must then hold
    no more than its low water mark, to which a purge evicts."""
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        answer = purge_request(test, cache, cacheapi.PURGE, None)
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()} {text(answer)}")
            continue
        usage = charged(cache)
        if cache.svc in test.fed.eviction:
            low = test.fed.eviction[cache.svc][1]
            if usage is None or usage > low:
                problems.append(
                    f"{cache.svc} holds {usage} bytes, above its low water mark of {low}"
                )
                continue
        notes.append(f"{cache.svc} holds {usage} bytes")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


# --------------------------------------------------------------------------
# allowed-federations


def data_route(cache: common.Cache, discovery: str, path: str) -> str:
    """The URL of path at cache's data route for federation discovery
    (/api/v1.0/cache/data/<discovery>/<path>), discovery as given."""
    return f"{cache.web_url}/api/v1.0/cache/data/{discovery}{quote(path)}"


def foreign_federation(test: Test) -> Row:
    """At each V2 cache, an object it lacks, asked for under a federation
    that is not its own (evil.invalid:443, its colon escaped): 403, with
    JSON whose `error` is `federation_not_allowed`, and none of the
    object, which the cache must not then hold (Cache.AllowedFederations,
    empty by default, allows only the cache's own federation)."""
    rels = {c.svc: test.rel("federations", f"foreign-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        answer = web.request(
            "GET", data_route(cache, "evil.invalid%3A443", test.path(rel)), token=test.token
        )
        said = common.as_object(cacheapi.decoded(answer)) or {}
        if answer.status != 403 or said.get("error") != "federation_not_allowed":
            problems.append(f"{cache.svc}: {answer.describe()} '{text(answer)[:120]}'")
        if objects[rel] in answer.body:
            problems.append(f"{cache.svc} served the object for another federation")
        time.sleep(1)
        found = test.held(cache, rel)
        if found is not False:
            problems.append(f"{cache.svc}: the object is {held_word(found)} afterward")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s)"


def own_federation(test: Test) -> Row:
    """At each V2 cache, an object asked for under its own federation's
    discovery host, as is, with its colon escaped, and in upper case:
    each must get the object's bytes (isFederationAllowed compares hosts
    without regard to case)."""
    discovery = urlsplit(str(test.fed.discovery["discovery_endpoint"])).netloc
    spellings = (discovery, quote(discovery, safe=""), discovery.upper())
    rels = {c.svc: test.rel("federations", f"own-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        for spelling in spellings:
            answer = web.request(
                "GET",
                data_route(cache, spelling, test.path(rel)),
                token=test.token,
                timeout=120,
            )
            if answer.status != 200 or answer.body != objects[rel]:
                problems.append(f"{cache.svc}, as {spelling}: {served(answer, objects[rel])}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.v2)} V2 cache(s): {', '.join(spellings)}"


# --------------------------------------------------------------------------
# usage


def namespace_usage(test: Test, cache: common.Cache) -> tuple[Optional[int], str]:
    """How many bytes cache charges /protected-a, from its stats'
    NamespaceUsage (keyed `s<storage>:ns<namespace ID>`), summed over its
    storage, and the IDs' names from introspect/stats; or None, and why
    it could not say."""
    found = cacheapi.stats(cache)
    if found is None:
        return None, "its stats answered no JSON object"
    answer = cacheapi.introspect(cache, test.admin(cache), "GET", "stats")
    names = common.as_object(
        (common.as_object(cacheapi.decoded(answer)) or {}).get("namespace_names")
    )
    if answer.status != 200 or names is None:
        return None, f"introspect/stats answered {answer.describe()}, with no namespace_names"
    ids = {str(n) for n, prefix in names.items() if prefix == f"/{NAMESPACE}"}
    usage = common.as_object(found.get("NamespaceUsage")) or {}
    total = 0
    for key, value in usage.items():
        if key.split(":ns", 1)[-1] in ids and isinstance(value, int):
            total += value
    return total, ""


def disk_usage(test: Test, cache: common.Cache) -> Optional[int]:
    """How many bytes cache's store's files take, as its introspection
    says (introspect/disk-usage), or None."""
    answer = cacheapi.introspect(cache, test.admin(cache), "POST", "disk-usage")
    found = common.as_object(cacheapi.decoded(answer)) or {}
    value = found.get("total_bytes_on_disk")
    return value if answer.status == 200 and isinstance(value, int) else None


def usage_accounting(test: Test) -> Row:
    """At each V2 cache, USAGE_COUNT cold whole reads of objects of
    USAGE_SIZE bytes. What the cache charges /protected-a
    (NamespaceUsage) must grow by exactly their stored size (blocks.stored()),
    and what it holds in all (TotalUsage) by at least that; its store's
    object files on disk (data/cache/<N>/persistent-cache/objects/) by
    that, give or take USAGE_SLACK; and its introspection's measure of
    those files (introspect/disk-usage) must agree with the files'. A
    bounded cache (`-p cache-small`) is purged first, to its low water
    mark, so that the reads make it evict nothing."""
    want = USAGE_COUNT * blocks.stored(USAGE_SIZE)
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        objects = {
            test.rel("usage", cache.svc, str(n)): os.urandom(USAGE_SIZE)
            for n in range(USAGE_COUNT)
        }
        why = test.put(objects)
        if why:
            return FAIL, why
        if cache.svc in test.fed.eviction:
            # Down to the low water mark, so that the reads evict nothing.
            answer = purge_request(test, cache, cacheapi.PURGE, None)
            if answer.status != 200:
                problems.append(f"{cache.svc}: a purge first got {answer.describe()}")
                continue
        ns_before, why = namespace_usage(test, cache)
        total_before, disk_before = total_usage(cache), store_bytes(cache)
        if ns_before is None or total_before is None:
            problems.append(f"{cache.svc}: {why or 'its stats give no TotalUsage'}")
            continue
        for rel, data in objects.items():
            why = test.fill(cache, rel, data)
            if why:
                return FAIL, why
        ns_after, why = namespace_usage(test, cache)
        total_after, disk_after = total_usage(cache), store_bytes(cache)
        if ns_after is None or total_after is None:
            problems.append(f"{cache.svc}: {why or 'its stats give no TotalUsage'}")
            continue
        if ns_after - ns_before != want:
            problems.append(
                f"{cache.svc}: /{NAMESPACE}'s usage grew by {ns_after - ns_before}, not {want}"
            )
        if total_after - total_before < want:
            problems.append(
                f"{cache.svc}: TotalUsage grew by {total_after - total_before}, less than {want}"
            )
        if disk_before is None or disk_after is None:
            notes.append(f"{cache.svc}: no store of object files to measure")
            continue
        if not want <= disk_after - disk_before <= want + USAGE_SLACK:
            problems.append(
                f"{cache.svc}'s object files grew by {disk_after - disk_before}, not {want}"
            )
        measured = disk_usage(test, cache)
        if measured is None:
            problems.append(f"{cache.svc}: introspect/disk-usage gave no total_bytes_on_disk")
        elif abs(measured - disk_after) > USAGE_SLACK:
            problems.append(
                f"{cache.svc}: introspect/disk-usage says {measured} bytes, its files {disk_after}"
            )
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{len(test.v2)} V2 cache(s), {want} bytes each"] + notes)


def water_marks(test: Test) -> Row:
    """At each V2 cache whose store is bounded (`-p cache-small`), its
    stats' DirStats must give its high and low water marks."""
    bounded = [c for c in test.v2 if c.svc in test.fed.eviction]
    if not bounded:
        return SKIP, "no V2 cache has a bounded store ('-p cache-small')"
    problems: list[str] = []
    for cache in bounded:
        high, low = test.fed.eviction[cache.svc]
        found = common.as_object((cacheapi.stats(cache) or {}).get("DirStats")) or {}
        marks = [
            (d.get("HighWater"), d.get("LowWater"))
            for d in (common.as_object(v) for v in found.values())
            if d is not None
        ]
        if (high, low) not in marks:
            problems.append(f"{cache.svc}: DirStats give {marks}, not {(high, low)}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(bounded)} V2 cache(s)"


# --------------------------------------------------------------------------
# consistency


def consistency_check(
    test: Test, cache: common.Cache, **query: str
) -> tuple[Optional[dict[str, Any]], str]:
    """Run cache's consistency check (introspect/consistency, an event
    stream) with query, and return what its last event, `done`, says
    (ConsistencyCheckResult; its counts are the cache's since it
    started), or None and why."""
    answer = cacheapi.introspect(cache, test.admin(cache), "POST", "consistency", **query)
    if answer.status != 200:
        return None, f"{answer.describe()} {text(answer)}"
    for event in answer.body.decode(errors="replace").split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in event.strip().splitlines() if ": " in line
        )
        if fields.get("event") == "done":
            try:
                found = common.as_object(json.loads(fields.get("data", "")))
            except ValueError:
                found = None
            return (found, "") if found is not None else (None, "its `done` event held no JSON")
    return None, "it sent no `done` event"


def consistency_stats(cache: common.Cache) -> dict[str, Any]:
    """The cache's ConsistencyStats, from its stats; {} if none."""
    return common.as_object((cacheapi.stats(cache) or {}).get("ConsistencyStats")) or {}


INCONSISTENT = "pelican_cache_data_scan_inconsistent_objects_total"


def scan_metric(cache: common.Cache) -> Optional[float]:
    """The cache's count of objects its data scans found inconsistent."""
    found = metrics.fetch(cache.web_url)
    return None if found is None else found.value(INCONSISTENT)


# The rows of `consistency` that corrupted() decides, in order.
CORRUPTED_CASES = ("verify", "scan-counts", "scan-drops")


def corrupted(test: Test) -> dict[str, Row]:
    """At each V2 cache, an object it holds whole, whose block 1 its chaos
    API then corrupts in its store; a row for each of CORRUPTED_CASES:

      verify       its verify (introspect/verify) must say `valid: false`
      scan-counts  its data scan (introspect/consistency, metadata=false)
                   must count a checksum mismatch more than before it,
                   and INCONSISTENT must rise
      scan-drops   the scan must drop the object, which the cache must no
                   longer hold; and a GET must then fetch it intact

    A cache whose verify finds the object valid has not seen the
    corruption (it may hold the block in memory, `-p cache-memory`), and
    leaves every row inconclusive. Run once, by the first of the rows
    (see Test.corrupted)."""
    found = {case: Findings() for case in CORRUPTED_CASES}
    verify, counts, drops = (found[case] for case in CORRUPTED_CASES)

    def everywhere(why: str) -> None:
        for each in found.values():
            each.problems.append(why)

    rels = {c.svc: test.rel("consistency", f"corrupted-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    for cache in test.v2:
        rel, data = rels[cache.svc], objects[rels[cache.svc]]
        why = fill_all(test, {rel: data}, [cache])
        if why:
            everywhere(why)
            continue
        url = test.object_url(rel)
        admin = test.admin(cache)
        answer = cacheapi.introspect(
            cache,
            admin,
            "POST",
            "chaos",
            op="corrupt",
            url=url,
            block="1",
            bytes=str(blocks.TAG),
        )
        if answer.status == 404:
            why = f"{cache.svc} has no chaos API (Cache.EnableChaosAPI; Pelican v7.27.0 on)"
            return {case: (SKIP, why) for case in CORRUPTED_CASES}
        if answer.status != 200:
            everywhere(f"{cache.svc}: the chaos API got {answer.describe()} {text(answer)}")
            continue
        answer = cacheapi.introspect(cache, admin, "POST", "verify", url=url)
        said = common.as_object(cacheapi.decoded(answer)) or {}
        if answer.status != 200 or not isinstance(said.get("valid"), bool):
            verify.problems.append(f"{cache.svc}: {answer.describe()} {text(answer)}")
        elif said["valid"]:
            for each in found.values():
                each.doubts.append(f"{cache.svc}: verify finds the corrupted object valid")
            continue
        mismatches = consistency_stats(cache).get("ChecksumMismatches")
        metric = scan_metric(cache)
        done, why = consistency_check(test, cache, metadata="false")
        if done is None or not done.get("data_scan_ran"):
            for each in (counts, drops):
                each.problems.append(f"{cache.svc}: the data scan: {why or 'it did not run'}")
            continue
        counted = done.get("checksum_mismatches")
        if not isinstance(mismatches, int) or not isinstance(counted, int):
            counts.problems.append(f"{cache.svc}: no count of mismatches before and after")
        elif counted <= mismatches:
            counts.problems.append(
                f"{cache.svc}: the data scan counts {counted} mismatches, as before it"
            )
        after = scan_metric(cache)
        if metric is None or after is None:
            counts.notes.append(f"{cache.svc}: no {INCONSISTENT} to read")
        elif after <= metric:
            counts.problems.append(f"{cache.svc}: {INCONSISTENT} stayed at {after}")
        held = test.held(cache, rel)
        if held is not False:
            drops.problems.append(
                f"{cache.svc}: the object is {held_word(held)} after the scan"
            )
        answer = test.get(cache, rel)
        if answer.status != 200 or answer.body != data:
            drops.problems.append(
                f"{cache.svc}: a GET after the scan got {served(answer, data)}"
            )
    return {case: found[case].row(f"{len(test.v2)} V2 cache(s)") for case in CORRUPTED_CASES}


def corrupted_case(case: str) -> Callable[[Test], Row]:
    """The case of `consistency` named case, among CORRUPTED_CASES."""

    def run_it(test: Test) -> Row:
        if test.corrupted is None:
            test.corrupted = corrupted(test)
        return test.corrupted[case]

    return run_it


# How long ago a stray file's modification time is set: a metadata scan
# passes over files younger than 5 minutes (MinAgeForCleanup).
STRAY_AGE = 3600


def plant_stray(cache: common.Cache) -> tuple[Optional[str], str]:
    """Write a stray file in cache's store of object files, named as an
    object's would be (64 hex digits, split as objects/<2>/<2>/<60>; see
    GetInstanceStoragePath), in a directory the cache made if there is
    one, so that it may remove it, and STRAY_AGE seconds old: its path,
    under framework/var, or None and why it could not."""
    root = f"{cache_dir(cache)}/persistent-cache/objects"
    if not os.path.isdir(root):
        return None, f"framework/var/{root} is missing"
    parents = [
        os.path.join(root, a, b)
        for a in sorted(os.listdir(root))
        if len(a) == 2 and os.path.isdir(os.path.join(root, a))
        for b in sorted(os.listdir(os.path.join(root, a)))
        if len(b) == 2 and os.path.isdir(os.path.join(root, a, b))
    ]
    name = os.urandom(32).hex()
    try:
        if parents:
            parent = parents[0]
        else:
            parent = root
            for part in (name[:2], name[2:4]):
                parent = os.path.join(parent, part)
                os.mkdir(parent)
                os.chmod(parent, 0o777)  # nosec B103
        path = os.path.join(parent, name[4:])
        with open(path, "wb") as f:
            f.write(os.urandom(blocks.BLOCK + blocks.TAG))
        moment = time.time() - STRAY_AGE
        os.utime(path, (moment, moment))
    except OSError as e:
        return None, f"writing a stray file in framework/var/{root}: {e.strerror}"
    return path, ""


def orphan(test: Test) -> Row:
    """At each V2 cache, a stray file planted in its store of object files
    (plant_stray()), which no object of its claims: its metadata scan
    (introspect/consistency, data=false) must remove it, and count an
    orphaned file more than before (`orphaned_files`)."""
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        before = consistency_stats(cache).get("OrphanedFiles")
        path, why = plant_stray(cache)
        if path is None:
            notes.append(f"{cache.svc}: {why}")
            continue
        try:
            done, why = consistency_check(test, cache, data="false")
            if done is None:
                problems.append(f"{cache.svc}: the metadata scan: {why}")
                continue
            counted = done.get("orphaned_files")
            if os.path.exists(path):
                problems.append(
                    f"{cache.svc}: framework/var/{path} is still there after the scan"
                )
            if not isinstance(before, int) or not isinstance(counted, int) or counted <= before:
                problems.append(
                    f"{cache.svc}: the scan counts {counted} orphaned files, as before ({before})"
                )
        finally:
            if os.path.exists(path):
                os.remove(path)
    if problems:
        return FAIL, first(problems)
    if len(notes) == len(test.v2):
        return SKIP, first(notes)
    return PASS, "; ".join([f"{len(test.v2) - len(notes)} V2 cache(s)"] + notes)


# --------------------------------------------------------------------------
# Running the scenarios.

# Each scenario's cases: its row's name after the scenario's, and what
# runs it. admin-authz records its own rows.
CASES: dict[str, tuple[tuple[str, Callable[[Test], Row]], ...]] = {
    "prestage": (
        ("probe", prestage_probe),
        ("refused", prestage_refused),
        ("object", prestage_object),
        ("missing", prestage_missing),
        ("encoded-name", prestage_name),
        ("client", prestage_client),
    ),
    "evict": (
        ("refused", evict_refused),
        ("immediate", evict_immediate),
        ("marked", evict_marked),
        ("prefix", evict_prefix),
        ("encoded-name", evict_name),
        ("client", evict_client),
    ),
    "purge": (
        ("to-target", purge_to_target),
        ("first", purge_first),
        ("all", purge_all),
    ),
    "allowed-federations": (("foreign", foreign_federation), ("own", own_federation)),
    "usage": (("accounting", usage_accounting), ("water-marks", water_marks)),
    "consistency": (
        *((case, corrupted_case(case)) for case in CORRUPTED_CASES),
        ("orphan", orphan),
    ),
}

# The cases that ask every cache, XRootD ones too; the rest ask V2 caches.
ANY_CACHE = {("prestage", "client")}


def run_case(
    test: Test, scenario: str, name: str, case: Callable[[Test], Row], results: common.Results
) -> None:
    """Run a case, and record its row."""
    row = f"{scenario} {name}"
    if (scenario, name) not in ANY_CACHE and not test.v2:
        results.add(row, SKIP, "this shape has no federated V2 cache")
        return
    try:
        status, note = case(test)
    except stores.Unreadable as e:
        status, note = FAIL, f"could not read a store: {e}"
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # case fails, and the rest still run.
        traceback.print_exc()
        status, note = FAIL, f"{type(e).__name__}: {e}"
    results.add(row, status, note)


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a, or no cache."""
    if NAMESPACE not in fed.exports:
        return "this shape does not export /protected-a"
    if not fed.federated_caches:
        return "this shape has no federated cache"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        for namespace in stores.storage_namespaces(origin, fed.exports):
            directory = f"{origin.store_of(namespace)}/data/cache-api"
            if not origin.pstore and not os.path.isdir(directory):
                die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(
            fed.origins, fed.exports, "data/cache-api", tokens
        )
        if errors:
            warn(f"emptying data/cache-api {when}: {first(errors)}")

    clean("before the run")
    test = Test(session)
    print(f"Cache API test against {fed.url}, objects under /{NAMESPACE}/{test.base}/")
    try:
        for scenario in selected:
            if scenario == "admin-authz":
                if not test.v2:
                    results.add(scenario, SKIP, "this shape has no federated V2 cache")
                    continue
                try:
                    admin_authz(test, results)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    traceback.print_exc()
                    results.add(scenario, FAIL, f"{type(e).__name__}: {e}")
                continue
            for name, case in CASES[scenario]:
                run_case(test, scenario, name, case, results)
    finally:
        clean("after the run")
