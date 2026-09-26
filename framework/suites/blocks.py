"""Objects and byte ranges at the block boundaries of pstore (an origin's
encrypted store), the V2 cache, and the XRootD cache: write objects of
sizes on either side of each boundary, then read them back, whole and in
ranges, from each origin, through each cache, and through the client;
and objects that caches read in overlapping pieces, or that are
overwritten once cached.

The sizes, ranges, and the layout facts behind them are in
framework/testlib/blocks.py. Objects go under
/protected-a/data/blocks/<run>/, uploaded straight to every origin, in
three sets, each first read a different way, since how a V2 cache first
sees an object decides how it fills: `whole` (uploaded with a
Content-Length; first read whole through each cache), `ranged` (uploaded
in chunks with no length, as the client uploads; first read in ranges),
and `client` (uploaded with a length; first read by `pelican object
get`). On a pstore origin, /metrics must show each upload in the tier
its size predicts. The overlap, assemble, and overwrite scenarios upload
objects of their own, one per cache, so that each is cold where it is
read. Where /protected-a takes no writes, every object is put straight
in each origin's store instead (see stores.seed()), and the uploads
themselves are skipped; where it takes no direct clients, the origins
are read only through the caches, and a direct read must be refused.
"""

import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from testlib import blocks, common, stores, transfers, web
from testlib.common import die, first
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "put-declared":          "upload every size with a Content-Length to every origin",
    "put-chunked":           "upload every size in chunks, with no length, to every origin",
    "put-client":            "`pelican object put` of the sizes around pstore's chunk edges",
    "origin-read":           "each origin returns every object whole and every range",
    "client-get":            "`pelican object get`, through a cache and with --direct",
    "cache-whole-first":     "each cache: a cold whole read, a warm one, then every range",
    "cache-range-first":     "each cache: a cold range first, then every range, then the whole",
    "cache-overlap":         "each cache: ranges that overlap what it holds, in turn and at once",
    "cache-assemble":        "each cache: ranges that add up to an object leave it holding all",
    "empty":                 "a 0-byte object: 200 with no body; a range of it gets no bytes",
    "mixed-version":         "each cache: an object overwritten mid-way comes back as one version",
    "overwrite-cached":      "each cache: an object it holds, overwritten, comes back as one version",
    "overwrite-range-first": "the same, for an object it first read through a range",
}

# How each set is uploaded: with a declared length, or in chunks.
SETS = {"whole": True, "ranged": False, "client": True}

# The client uploads these, where pstore's chunking changes.
CLIENT_SIZES = (blocks.SPILL - 1, blocks.SPILL, blocks.SPILL + 1,
                blocks.CHUNK_UNDECLARED, blocks.CHUNK_UNDECLARED + 1)

# pstore's write tiers (see blocks.tier).
TIERS = ("inline", "buffered", "streamed")


def series(text: str, name: str) -> Dict[str, float]:
    """Every series of name in text, by its labels."""
    return {m.group(1): float(m.group(2))
            for m in re.finditer(rf"^{re.escape(name)}\{{([^}}]*)\}} (\S+)$", text, re.M)}


def origin_root(url: str) -> str:
    return re.sub(r"^(https://[^/]*).*", r"\1", url)


class Test:
    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.origins = stores.unique(session.fed.origins)
        self.caches = session.fed.caches
        self.tmp = session.path("blocks")
        os.makedirs(self.tmp)
        self.run = session.run
        self.base = f"data/blocks/{self.run}"
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        self.caps = session.fed.exports["protected-a"]
        self.writes = "Writes" in self.caps
        self.direct = "DirectReads" in self.caps
        self.data = {(s, size): os.urandom(size) for s in SETS for size in blocks.SIZES}
        # How each kind of upload went, by "declared" or "chunked".
        self.uploads: Dict[str, Tuple[str, str]] = {}

    def rel(self, set_name: str, size: int) -> str:
        return f"{self.base}/{set_name}/{size}"

    def metrics(self, origin: common.Origin) -> str:
        return web.request("GET", f"{origin_root(origin.url)}/metrics").body.decode(
            errors="replace")

    def tiers(self, origin: common.Origin) -> Optional[Dict[str, float]]:
        """pstore's writes by tier, or None if /metrics has none, as before
        an origin's first write."""
        found = series(self.metrics(origin), "pelican_pstore_writes_total")
        if not found:
            return None
        return {t: found.get(f'tier="{t}"', 0.0) for t in TIERS}

    def upload(self, declared: bool) -> Tuple[str, str]:
        """Upload every set uploaded this way to every origin, one object at
        a time, and check each store holds them; on a pstore origin, check
        the tiers they landed in. Where /protected-a takes no writes, put
        them straight in each store instead."""
        names = [s for s, d in SETS.items() if d == declared]
        if not self.writes:
            for origin in self.origins:
                for set_name in names:
                    for size in blocks.SIZES:
                        error = stores.write_file(origin.store, self.rel(set_name, size),
                                                  self.data[(set_name, size)])
                        if error:
                            return FAIL, error
            return PASS, f"put in place in {len(self.origins)} store(s)"
        notes = []
        for origin in self.origins:
            for set_name in names:
                errors = stores.make_dirs([origin], "protected-a", f"{self.base}/{set_name}",
                                          self.token)
                if errors:
                    return FAIL, "; ".join(errors)
            before = (self.tiers(origin) or dict.fromkeys(TIERS, 0.0)) if origin.pstore else None
            for set_name in names:
                for size in blocks.SIZES:
                    error = stores.put(origin, "protected-a", self.rel(set_name, size),
                                       self.data[(set_name, size)], self.token,
                                       chunked=not declared)
                    if error:
                        return FAIL, error
            for set_name in names:
                for size in blocks.SIZES:
                    want = common.digest(self.data[(set_name, size)])
                    try:
                        got = stores.held(origin, "protected-a", self.rel(set_name, size), self.token)
                    except stores.Unreadable as e:
                        return FAIL, str(e)
                    if got != want:
                        return FAIL, (f"{origin.svc} does not hold {set_name}/{size} intact"
                                      if got else f"{origin.svc} lacks {set_name}/{size}")
            if origin.pstore:
                after = self.tiers(origin)
                if before is None or after is None:
                    return FAIL, f"{origin.svc}: no pelican_pstore_writes_total after its writes"
                want_tiers = {t: 0 for t in before}
                for _ in names:
                    for size in blocks.SIZES:
                        want_tiers[blocks.tier(size, declared)] += 1
                got_tiers = {t: int(after[t] - before[t]) for t in before}
                if got_tiers != want_tiers:
                    return FAIL, f"{origin.svc}: tiers {got_tiers}, not {want_tiers}"
                notes.append(f"{origin.svc}: tiers {got_tiers}")
        count = len(names) * len(blocks.SIZES)
        return PASS, "; ".join([f"{count} objects at {len(self.origins)} origin(s)"] + notes)

    def ensure(self, declared: bool) -> Tuple[str, str]:
        """Upload the sets uploaded this way, once, and say how it went."""
        key = "declared" if declared else "chunked"
        if key not in self.uploads:
            self.uploads[key] = self.upload(declared)
        return self.uploads[key]

    def not_uploaded(self, *kinds: bool) -> Optional[str]:
        """Why an upload that a scenario needs failed, if one did."""
        for declared in kinds:
            status, note = self.ensure(declared)
            if status != PASS:
                return f"the upload failed: {note}"
        return None

    def get(self, base_url: str, rel: str, data: bytes,
            request: Optional[blocks.Request]) -> Optional[str]:
        """Read rel in /protected-a (or a range of it), which should hold
        data; what went wrong, or None."""
        headers = {"Range": blocks.header(request)} if request else {}
        answer = web.request("GET", f"{base_url}/protected-a/{rel}", token=self.token,
                             headers=headers, timeout=120)
        if request is None:
            if answer.status != 200:
                return f"{answer.describe()}, not 200"
            if answer.body != data:
                return f"{len(answer.body)} bytes, not the object"
            return None
        return blocks.check(data, request, answer.status, answer.headers, answer.body)

    def read(self, base_url: str, set_name: str, size: int,
             request: Optional[blocks.Request]) -> Optional[str]:
        """Read an object of a set (or a range of it); what went wrong, or
        None."""
        why = self.get(base_url, self.rel(set_name, size), self.data[(set_name, size)], request)
        if not why:
            return None
        return f"{set_name}/{size}{' ' + blocks.header(request) if request else ''}: {why}"

    def read_all(self, base_url: str, set_name: str, sizes=blocks.SIZES) -> List[str]:
        """Every range of every size in a set, 8 at a time."""
        jobs = [(size, r) for size in sizes for r in blocks.requests(size)]
        with ThreadPoolExecutor(8) as pool:
            found = pool.map(lambda job: self.read(base_url, set_name, *job), jobs)
        return [p for p in found if p]


def cache_dir(cache: common.Cache) -> str:
    """Where cache keeps its store, which it mounts at /data."""
    name = cache.svc[len("cache-"):] if cache.svc.startswith("cache-") else cache.svc
    return f"data/cache/{name}"


def upload_each(test: Test, base: str, objects: Dict[str, bytes]) -> List[str]:
    """Put objects, by name, at <base>/<name> at every origin (see
    stores.seed_everywhere()); what went wrong."""
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    for name, data in objects.items():
        if errors:
            break
        errors += stores.seed_everywhere(test.origins, "protected-a", f"{base}/{name}", data,
                                         test.token, test.caps)
    return errors


def at_once(jobs: List[Callable[[], Optional[str]]]) -> List[Optional[str]]:
    """Run jobs together, each started once all are ready; each one's
    problem, or None."""
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], Optional[str]]) -> Optional[str]:
        try:
            barrier.wait(timeout=60)
        except threading.BrokenBarrierError:
            return "could not start with the others"
        return job()

    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(run, jobs))


#---------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.

NO_WRITES = "/protected-a takes no writes"


def put_declared(test: Test) -> Tuple[str, str]:
    result = test.ensure(True)
    return result if test.writes else (SKIP, NO_WRITES)


def put_chunked(test: Test) -> Tuple[str, str]:
    result = test.ensure(False)
    return result if test.writes else (SKIP, NO_WRITES)


def put_client(test: Test) -> Tuple[str, str]:
    if not test.writes:
        return SKIP, NO_WRITES
    errors = stores.make_dirs(test.origins, "protected-a", f"{test.base}/put-client", test.token)
    if errors:
        return FAIL, "; ".join(errors)
    sources = []
    for size in CLIENT_SIZES:
        path = os.path.join(test.tmp, "put-client", str(size))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        sources.append(path)
    before = {o.svc: test.tiers(o) or dict.fromkeys(TIERS, 0.0)
              for o in test.origins if o.pstore}
    code, out, err = test.session.pelican_cmd(
        "object", "put", "--token", test.token_file, *sources,
        f"{test.fed.url}/protected-a/{test.base}/put-client/")
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    for size, path in zip(CLIENT_SIZES, sources):
        digest = common.sha256(path)
        held = stores.held_anywhere(test.origins, "protected-a", f"{test.base}/put-client/{size}",
                                    {"protected-a": test.token})
        if digest not in held:
            return FAIL, f"no origin holds {size} intact"
    notes = [f"{len(CLIENT_SIZES)} objects"]
    if before:
        got = {t: 0 for t in TIERS}
        for origin in test.origins:
            if not origin.pstore:
                continue
            was, now = before[origin.svc], test.tiers(origin)
            if now is None:
                return FAIL, f"{origin.svc}: no pelican_pstore_writes_total after its writes"
            for t in got:
                got[t] += int(now[t] - was[t])
        want = {t: 0 for t in got}
        for size in CLIENT_SIZES:
            want[blocks.tier(size, False)] += 1
        if got != want:
            return FAIL, f"tiers {got}, not {want} (the client declares no length)"
        notes.append(f"tiers {got}")
    return PASS, "; ".join(notes)


def origin_read(test: Test) -> Tuple[str, str]:
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    if (why := test.not_uploaded(True, False)):
        return FAIL, why
    problems = []
    for origin in test.origins:
        for set_name in ("whole", "ranged"):
            problems += [p for p in (test.read(origin.url, set_name, size, None)
                                     for size in blocks.SIZES) if p]
            problems += test.read_all(origin.url, set_name)
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s)"


def client_get(test: Test) -> Tuple[str, str]:
    if (why := test.not_uploaded(True)):
        return FAIL, why
    urls = [f"{test.fed.url}/protected-a/{test.rel('client', size)}" for size in blocks.SIZES]
    notes = []
    for label, flags in (("through a cache", []), ("direct", ["--direct"])):
        target = os.path.join(test.tmp, f"client-get-{'direct' if flags else 'cache'}")
        os.makedirs(target)
        code, out, err = test.session.pelican_cmd("object", "get", "--token", test.token_file,
                                                  *flags, *urls, target)
        if flags and not test.direct:
            if code == 0:
                return FAIL, f"{label}: succeeded, but /protected-a takes no direct clients"
            why = transfers.refused(out + err)
            if why is None:
                return FAIL, f"{label}: failed, but not by refusal: {last_line(err or out)}"
            notes.append(f"{label}: refused ({why})")
            continue
        if code != 0:
            return FAIL, f"{label}: exit {code}: {last_line(err or out)}"
        for size in blocks.SIZES:
            got = common.sha256(os.path.join(target, str(size)))
            if got != common.digest(test.data[("client", size)]):
                return FAIL, f"{label}: {size} is {'missing' if got is None else 'different'}"
        notes.append(label)
    return PASS, ", ".join(notes)


def cache_whole_first(test: Test) -> Tuple[str, str]:
    if (why := test.not_uploaded(True)):
        return FAIL, why
    problems = []
    for cache in test.caches:
        for size in blocks.SIZES:
            for when in ("cold", "warm"):
                why = test.read(cache.url, "whole", size, None)
                if why:
                    problems.append(f"{cache.svc} ({when}): {why}")
        problems += [f"{cache.svc}: {p}" for p in test.read_all(cache.url, "whole")]
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def cache_range_first(test: Test) -> Tuple[str, str]:
    if (why := test.not_uploaded(False)):
        return FAIL, why
    problems = []
    for cache in test.caches:
        for size in (s for s in blocks.SIZES if s > 0):
            middle = size // 2
            request = ((middle, min(size - 1, middle + blocks.BLOCK)),)
            why = test.read(cache.url, "ranged", size, request)
            if why:
                problems.append(f"{cache.svc} (cold): {why}")
        problems += [f"{cache.svc}: {p}" for p in test.read_all(cache.url, "ranged")]
        for size in blocks.SIZES:
            why = test.read(cache.url, "ranged", size, None)
            if why:
                problems.append(f"{cache.svc} (whole, after ranges): {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


def cache_overlap(test: Test) -> Tuple[str, str]:
    """Each plan of blocks.overlap_plans, read in turn from an object of
    its own: every read after the first covers blocks that the cache
    fetched for an earlier one and blocks that it must fetch now. Then
    blocks.concurrent_reads, all at once, of one more cold object. After
    each, the whole object must come back intact."""
    plans = blocks.overlap_plans()
    base = f"{test.base}/overlap"
    objects = {f"{c.svc}-{plan}": os.urandom(blocks.OVERLAP_SIZE)
               for c in test.caches for plan in [*plans, "concurrent"]}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems = []
    reads = blocks.concurrent_reads()
    for cache in test.caches:
        for plan, requests in plans.items():
            name = f"{cache.svc}-{plan}"
            for n, request in enumerate(requests, 1):
                why = test.get(cache.url, f"{base}/{name}", objects[name], request)
                if why:
                    problems.append(f"{cache.svc}: {plan}, read {n} ({blocks.header(request)}): {why}")
            why = test.get(cache.url, f"{base}/{name}", objects[name], None)
            if why:
                problems.append(f"{cache.svc}: {plan}, the whole object afterward: {why}")
        name = f"{cache.svc}-concurrent"
        found = at_once([lambda r=r: test.get(cache.url, f"{base}/{name}", objects[name], r)
                         for r in reads])
        for request, why in zip(reads, found):
            if why:
                what = blocks.header(request) if request else "the whole object"
                problems.append(f"{cache.svc}: at once, {what}: {why}")
        why = test.get(cache.url, f"{base}/{name}", objects[name], None)
        if why:
            problems.append(f"{cache.svc}: the whole object after the reads at once: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s): {len(plans)} plans, then {len(reads)} reads at once"


def has_age(test: Test, cache: common.Cache, rel: str) -> Optional[bool]:
    """Whether a V2 cache says, with Age, that it holds all of rel; None
    if a HEAD of it fails."""
    answer = web.request("HEAD", f"{cache.url}/protected-a/{rel}", token=test.token)
    return bool(answer.header("Age")) if answer.status == 200 else None


def read_cinfo(cache: common.Cache, rel: str) -> Tuple[Optional[blocks.Cinfo], str]:
    """What an XRootD cache's .cinfo of rel in /protected-a records, or why
    it could not be read. The cache links it from namespace/ to where it
    keeps it, by a path in its own mount."""
    root = cache_dir(cache)
    link = f"{root}/namespace/protected-a/{rel}.cinfo"
    try:
        target = os.readlink(link)
    except FileNotFoundError:
        return None, f"framework/var/{link} is missing"
    except OSError:
        target = link
    if target.startswith("/data/"):
        target = f"{root}/{target[len('/data/'):]}"
    try:
        with open(target, "rb") as f:
            return blocks.parse_cinfo(f.read()), ""
    except FileNotFoundError:
        return None, f"framework/var/{target} is missing"
    except ValueError as e:
        return None, f"framework/var/{target}: {e}"


def wait_cinfo(cache: common.Cache, rel: str, want: Callable[[blocks.Cinfo], bool],
               seconds: float) -> Tuple[Optional[blocks.Cinfo], str]:
    """read_cinfo, until what it finds is wanted or seconds pass: the
    cache writes its .cinfo some time after the blocks."""
    last: List[Tuple[Optional[blocks.Cinfo], str]] = []

    def wanted() -> bool:
        last[:] = [read_cinfo(cache, rel)]
        found = last[0][0]
        return found is not None and want(found)

    common.wait_for(wanted, seconds)
    return last[0]


def cache_assemble(test: Test) -> Tuple[str, str]:
    """blocks.ASSEMBLE, read in its order from a cold object: ranges that
    overlap, and add up to the object. The cache must hold part of it
    after the first, and all of it after the last: a V2 cache sends Age
    only then, and an XRootD cache's .cinfo shows which blocks it holds.
    Then the whole object must come back intact."""
    base = f"{test.base}/assemble"
    objects = {c.svc: os.urandom(blocks.ASSEMBLE_SIZE) for c in test.caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: List[str] = []
    unsure: List[str] = []
    notes = []
    for cache in test.caches:
        rel, data = f"{base}/{cache.svc}", objects[cache.svc]
        ranges = [(r,) for r in blocks.ASSEMBLE]
        why = test.get(cache.url, rel, data, ranges[0])
        if why:
            problems.append(f"{cache.svc}: the first range: {why}")
            continue
        if cache.kind == "v2":
            partial = has_age(test, cache, rel)
            if partial is None:
                problems.append(f"{cache.svc}: a HEAD after the first range failed")
                continue
            if partial:
                unsure.append(f"{cache.svc}: Age after the first range, so it cannot show completion")
        else:
            found, why = wait_cinfo(cache, rel, lambda c: 0 < c.count < c.blocks, 30)
            if not found:
                unsure.append(f"{cache.svc}: {why}")
            elif not 0 < found.count < found.blocks:
                unsure.append(f"{cache.svc}: {found.count} of {found.blocks} blocks after the"
                              " first range, not some")
        for request in ranges[1:]:
            why = test.get(cache.url, rel, data, request)
            if why:
                problems.append(f"{cache.svc}: {blocks.header(request)}: {why}")
        if cache.kind == "v2":
            if not common.wait_for(lambda: bool(has_age(test, cache, rel)), 10, interval=1):
                problems.append(f"{cache.svc}: no Age after every range, so it does not hold it all")
            else:
                notes.append(f"{cache.svc}: Age")
        else:
            found, why = wait_cinfo(cache, rel, lambda c: c.complete, 60)
            if not found:
                unsure.append(f"{cache.svc}: {why}")
            elif not found.complete:
                problems.append(f"{cache.svc}: {found.count} of {found.blocks} blocks after every"
                                " range")
            else:
                notes.append(f"{cache.svc}: {found.blocks} of {found.blocks} blocks")
        why = test.get(cache.url, rel, data, None)
        if why:
            problems.append(f"{cache.svc}: the whole object afterward: {why}")
    if problems:
        return FAIL, first(problems)
    if unsure:
        return INCONCLUSIVE, "; ".join(unsure + notes)
    return PASS, "; ".join(notes)


def empty(test: Test) -> Tuple[str, str]:
    """Every server answers a GET of an empty object with 200 and no body,
    and a range of it with 200 and no body (RFC 7233 lets it ignore Range)
    or with 416, since no range of it is satisfiable. The origins are
    asked only if they take direct clients."""
    if (why := test.not_uploaded(True, False)):
        return FAIL, why
    servers = [(o.svc, o.url) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url) for c in test.caches]
    notes = []
    for svc, url in servers:
        for set_name in ("whole", "ranged"):
            target = f"{url}/protected-a/{test.rel(set_name, 0)}"
            answer = web.request("GET", target, token=test.token)
            if answer.status != 200 or answer.body:
                return FAIL, f"{svc}: {set_name}/0: {answer.describe()}, {len(answer.body)} bytes"
            answer = web.request("GET", target, token=test.token, headers={"Range": "bytes=0-0"})
            if answer.status not in (200, 416):
                return FAIL, (f"{svc}: a range of {set_name}/0 got {answer.describe()},"
                              " not 200 or 416")
            # A 416's body explains it; a success's must be empty.
            if answer.status == 200 and answer.body:
                return FAIL, (f"{svc}: a range of {set_name}/0 got {answer.describe()}"
                              f" with {len(answer.body)} bytes")
        notes.append(f"{svc} ok")
    return PASS, ", ".join(notes)


def mixed_version(test: Test) -> Tuple[str, str]:
    """Each cache reads the first block of an object, and the origins then
    take another version of it. Every later response, to a range the
    cache has yet to fetch and to the whole object, must be entirely one
    version, and the same one."""
    size = 20 * blocks.BLOCK
    rel = f"{test.base}/mixed"
    v1, v2 = os.urandom(size), os.urandom(size)
    errors = stores.make_dirs(test.origins, "protected-a", test.base, test.token)
    errors += stores.seed_everywhere(test.origins, "protected-a", rel, v1, test.token, test.caps)
    if errors:
        return FAIL, "; ".join(errors)

    def get(cache: common.Cache, request: Optional[str]) -> web.Response:
        headers = {"Range": request} if request else {}
        return web.request("GET", f"{cache.url}/protected-a/{rel}", token=test.token,
                           headers=headers, timeout=120)

    block = blocks.BLOCK
    for cache in test.caches:
        answer = get(cache, f"bytes=0-{block - 1}")
        if answer.status != 206 or answer.body != v1[:block]:
            return FAIL, f"{cache.svc}: the first block of v1 came back wrong ({answer.describe()})"
    errors = stores.seed_everywhere(test.origins, "protected-a", rel, v2, test.token, test.caps)
    if errors:
        return FAIL, "; ".join(errors)
    notes = []
    for cache in test.caches:
        start = 10 * block
        answer = get(cache, f"bytes={start}-{start + block - 1}")
        part = answer.body
        if answer.status != 206:
            return FAIL, f"{cache.svc}: block 10 got {answer.describe()}, not 206"
        if part not in (v1[start:start + block], v2[start:start + block]):
            return FAIL, f"{cache.svc}: block 10 is neither version"
        answer = get(cache, None)
        if answer.status != 200:
            return FAIL, f"{cache.svc}: the whole object got {answer.describe()}, not 200"
        if answer.body == v1:
            version = "v1"
        elif answer.body == v2:
            version = "v2"
        else:
            which = "".join("1" if answer.body[i:i + block] == v1[i:i + block]
                            else "2" if answer.body[i:i + block] == v2[i:i + block] else "?"
                            for i in range(0, size, block))
            return FAIL, f"{cache.svc}: the whole object mixes versions, block by block: {which}"
        block_version = "v1" if part == v1[start:start + block] else "v2"
        if block_version != version:
            return FAIL, (f"{cache.svc}: block 10 came back as {block_version},"
                          f" the whole object as {version}")
        notes.append(f"{cache.svc} served {version}")
    return PASS, ", ".join(notes)


# The overwrite scenarios' objects: one keeps its size, one grows.
OVERWRITE_SIZE = 3 * blocks.XROOTD_BLOCK + 7
OVERWRITE_GROWTH = 3 * blocks.BLOCK + 11
# How long past an object's freshness a cache may take to serve the new
# version: requests are 3 seconds apart, and it revalidates by fetching
# the object again.
REVALIDATION_MARGIN = 45


def overwrite(test: Test, range_first: bool) -> Tuple[str, str]:
    """Each cache reads v1 of two objects of its own, whole twice or a
    range then whole, and the origins then take v2: one of the same
    size, one larger. Every response must then be all v1 or all v2, with
    that version's size. With ORIGIN_CACHE_CONTROL (`-p origin-max-age`),
    every origin must send it, and each cache, once its copy is stale,
    must serve v2, and only v2. Without it, a cache decides for itself
    how long its copy stays fresh. The origins are asked only if they
    take direct clients."""
    kind = "range-first" if range_first else "cached"
    base = f"{test.base}/overwrite-{kind}"
    names = ("same", "resized")
    v1 = {f"{c.svc}-{n}": os.urandom(OVERWRITE_SIZE) for c in test.caches for n in names}
    v2 = {key: os.urandom(len(data) + (OVERWRITE_GROWTH if key.endswith("-resized") else 0))
          for key, data in v1.items()}
    errors = upload_each(test, base, v1)
    if errors:
        return FAIL, errors[0]
    control = test.fed.origin_cache_control
    fresh = blocks.freshness(control) if control else None
    if control and test.direct:
        for origin in test.origins:
            answer = web.request("GET", f"{origin.url}/protected-a/{base}/{next(iter(v1))}",
                                 token=test.token, headers={"Range": "bytes=0-0"})
            if answer.header("Cache-Control") != control:
                return FAIL, (f"{origin.svc} sends Cache-Control '{answer.header('Cache-Control')}',"
                              f" not ORIGIN_CACHE_CONTROL's '{control}'")
    cached_at = {}
    for cache in test.caches:
        cached_at[cache.svc] = time.time()
        for n in names:
            key = f"{cache.svc}-{n}"
            reads = [((0, 2 * blocks.BLOCK + 99),), None] if range_first else [None, None]
            for request in reads:
                why = test.get(cache.url, f"{base}/{key}", v1[key], request)
                if why:
                    return FAIL, f"{cache.svc}: {n} before the overwrite: {why}"
    for key, data in v2.items():
        errors = stores.seed_everywhere(test.origins, "protected-a", f"{base}/{key}", data,
                                        test.token, test.caps)
        if errors:
            return FAIL, errors[0]

    def battery(cache: common.Cache) -> Tuple[Counter, List[str]]:
        """Every read of both objects: how many came back as each version,
        and what was wrong."""
        jobs = []
        for n in names:
            key = f"{cache.svc}-{n}"
            asks = [None, *dict.fromkeys(blocks.requests(len(v1[key]))
                                         + blocks.requests(len(v2[key])))]
            jobs += [(n, key, r) for r in asks]

        def one(job: Tuple[str, str, Optional[blocks.Request]]) -> Tuple[Optional[str], str]:
            n, key, request = job
            headers = {"Range": blocks.header(request)} if request else {}
            answer = web.request("GET", f"{cache.url}/protected-a/{base}/{key}", token=test.token,
                                 headers=headers, timeout=120)
            return blocks.which_version({"v1": v1[key], "v2": v2[key]}, request, answer.status,
                                        answer.headers, answer.body)

        with ThreadPoolExecutor(8) as pool:
            found = list(pool.map(one, jobs))
        counts: Counter = Counter()
        wrong = []
        for (n, _, request), (version, why) in zip(jobs, found):
            what = f"{cache.svc}: {n} {blocks.header(request) if request else 'whole'}"
            if version is None:
                wrong.append(f"{what}: {why}")
            else:
                counts[version] += 1
        return counts, wrong

    problems: List[str] = []
    notes = []
    for cache in test.caches:
        counts, wrong = battery(cache)
        problems += wrong
        served = ", ".join(f"{v} {counts[v]}" for v in sorted(counts)) or "nothing"
        if fresh is None:
            notes.append(f"{cache.svc} served {served}")
            continue
        def versions_now() -> List[Optional[str]]:
            found = []
            for key in (f"{cache.svc}-{n}" for n in names):
                a = web.request("GET", f"{cache.url}/protected-a/{base}/{key}", token=test.token,
                                timeout=120)
                found.append(blocks.which_version({"v1": v1[key], "v2": v2[key]}, None,
                                                  a.status, a.headers, a.body)[0])
            return found

        deadline = cached_at[cache.svc] + fresh + REVALIDATION_MARGIN
        while True:
            now = versions_now()
            if all(v == "v2" for v in now) or time.time() > deadline:
                break
            time.sleep(3)
        waited = time.time() - cached_at[cache.svc]
        if not all(v == "v2" for v in now):
            seen = " (it first saw each through a range)" if range_first else ""
            problems.append(f"{cache.svc} still served {', '.join(map(str, now))} {waited:.0f}s"
                            f" after caching v1, past its freshness of {fresh:g}s{seen}")
            continue
        counts, wrong = battery(cache)
        problems += wrong
        if counts["v1"]:
            problems.append(f"{cache.svc} served v1 {counts['v1']} times after serving v2")
        notes.append(f"{cache.svc} served {served}, then v2 by {waited:.0f}s")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


def overwrite_cached(test: Test) -> Tuple[str, str]:
    return overwrite(test, False)


def overwrite_range_first(test: Test) -> Tuple[str, str]:
    return overwrite(test, True)


RUN = {
    "put-declared": put_declared, "put-chunked": put_chunked, "put-client": put_client,
    "origin-read": origin_read, "client-get": client_get,
    "cache-whole-first": cache_whole_first, "cache-range-first": cache_range_first,
    "cache-overlap": cache_overlap, "cache-assemble": cache_assemble,
    "empty": empty, "mixed-version": mixed_version,
    "overwrite-cached": overwrite_cached, "overwrite-range-first": overwrite_range_first,
}


def skip(fed: common.Federation) -> Optional[str]:
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    disk_stores = [o.store for o in stores.unique(fed.origins) if not o.pstore]
    for store in disk_stores:
        if not os.path.isdir(f"{store}/data/blocks"):
            die(f"framework/var/{store}/data/blocks is missing; run ./fed.sh init")
        # Earlier runs' objects come out first.
        common.empty_dir(f"{store}/data/blocks")

    test = Test(session)
    print(f"Block test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        for name in selected:
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            results.add(name, status, note)
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        for store in disk_stores:
            common.empty_dir(f"{store}/data/blocks")
