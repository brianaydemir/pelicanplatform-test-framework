"""Objects and byte ranges at the block boundaries of pstore (an origin's
encrypted store), the V2 cache, and the XRootD cache: write objects of
sizes on either side of each boundary, then read them back, whole and in
ranges, from each origin, through each cache, and through both clients;
the digests that each server reports of them; and objects that caches
read in overlapping pieces or give up on, that are overwritten once
cached, or that are written through a cache.

The sizes, ranges, and the layout facts behind them are in
framework/testlib/blocks.py. Objects go under
/protected-a/data/blocks/<run>/, uploaded straight to every origin, in
four sets, each first read a different way, since how a V2 cache first
sees an object decides how it fills: `whole` (uploaded with a
Content-Length; first read whole through each cache), `ranged` (uploaded
in chunks with no length, as the client uploads; first read in ranges),
`client` (uploaded with a length; first read by `pelican object get`),
and `plugin` (likewise, first read by stash_plugin). Both clients also
upload every size. On a pstore origin, /metrics must show each upload in
the tier its size predicts. The overlap, assemble, abandon, overwrite,
and write-through scenarios upload objects of their own, one per cache,
so that each is cold where it is read. Where /protected-a takes no
writes, every object is put straight in each origin's store instead
(see stores.seed()), and the uploads themselves are skipped; where it
takes no direct clients, the origins are read only through the caches,
and a direct read must be refused.
"""

import functools
import os
import re
import shutil
import subprocess  # nosec B404
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from testlib import blocks, common, credentials, digests, stores, transfers, web
from testlib.common import die, first, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

SCENARIOS = {
    "put-declared": "upload every size with a Content-Length to every origin",
    "put-chunked": "upload every size in chunks, with no length, to every origin",
    "put-client": "`pelican object put` of every size",
    "put-plugin": "stash_plugin's upload of every size",
    "origin-read": "each origin returns every object whole and every range",
    "client-get": "`pelican object get` of every size, through a cache and with --direct",
    "plugin-get": "stash_plugin's download of every size, through a cache and direct",
    "cache-whole-first": "each cache: a cold whole read, a warm one, then every range",
    "cache-range-first": "each cache: a cold range first, then every range, then the whole",
    "cache-overlap": "each cache: ranges that overlap what it holds, in turn and at once",
    "cache-assemble": "each cache: ranges that add up to an object leave it holding all",
    "cache-abandoned": "each cache: a cold read that its client gives up on spoils nothing",
    "digests": "every digest that a server reports (Want-Digest) is the object's",
    "digest-overwrite": "each origin's digests and ETag of an object follow its overwrites",
    "past-end": "a range that starts at or past an object's end: 416, not bytes",
    "empty": "a 0-byte object: 200 with no body; a range of it gets no bytes",
    "mixed-version": "each cache: an object overwritten mid-way comes back as one version",
    "overwrite-cached": "each cache: an object it holds, overwritten, comes back as one version",
    "overwrite-range-first": "the same, for an object it first read through a range",
    "write-through": "each V2 cache relays a PUT and a DELETE, and serves the result at once",
}

# The scenarios that read through each cache, and so have nothing to test
# without one.
CACHE_SCENARIOS = (
    "cache-whole-first",
    "cache-range-first",
    "cache-overlap",
    "cache-assemble",
    "cache-abandoned",
    "mixed-version",
    "overwrite-cached",
    "overwrite-range-first",
    "write-through",
)

# How each set is uploaded: with a declared length, or in chunks.
SETS = {"whole": True, "ranged": False, "client": True, "plugin": True}

# pstore's write tiers (see blocks.tier).
TIERS = ("inline", "buffered", "streamed")


def origin_root(url: str) -> str:
    """The scheme and host of url, without its path."""
    return re.sub(r"^(https://[^/]*).*", r"\1", url)


class Test:
    """The objects of every set, the token, and how the uploads went."""

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
        self.uploads: dict[str, tuple[str, str]] = {}

    def rel(self, set_name: str, size: int) -> str:
        """Where a set's object of size is in /protected-a."""
        return f"{self.base}/{set_name}/{size}"

    def metrics(self, origin: common.Origin) -> str:
        """origin's /metrics."""
        return web.request("GET", f"{origin_root(origin.url)}/metrics").body.decode(
            errors="replace"
        )

    def tiers(self, origin: common.Origin) -> Optional[dict[str, float]]:
        """pstore's writes by tier, or None if /metrics has none, as before
        an origin's first write."""
        found = common.metric_series(self.metrics(origin), "pelican_pstore_writes_total")
        if not found:
            return None
        return {t: found.get(f'tier="{t}"', 0.0) for t in TIERS}

    def upload(self, declared: bool) -> tuple[str, str]:
        """Upload every set uploaded this way to every origin, one object at
        a time, and check each store holds them; on a pstore origin, check
        the tiers they landed in. Where /protected-a takes no writes, put
        them straight in each store instead."""
        names = [s for s, d in SETS.items() if d == declared]
        if not self.writes:
            for origin in self.origins:
                for set_name in names:
                    for size in blocks.SIZES:
                        error = stores.write_file(
                            origin.store_of("protected-a"),
                            self.rel(set_name, size),
                            self.data[(set_name, size)],
                        )
                        if error:
                            return FAIL, error
            return PASS, f"put in place in {len(self.origins)} store(s)"
        notes: list[str] = []
        for origin in self.origins:
            for set_name in names:
                errors = stores.make_dirs(
                    [origin], "protected-a", f"{self.base}/{set_name}", self.token
                )
                if errors:
                    return FAIL, "; ".join(errors)
            before = (
                (self.tiers(origin) or dict.fromkeys(TIERS, 0.0)) if origin.pstore else None
            )
            for set_name in names:
                for size in blocks.SIZES:
                    error = stores.put(
                        origin,
                        "protected-a",
                        self.rel(set_name, size),
                        self.data[(set_name, size)],
                        self.token,
                        chunked=not declared,
                    )
                    if error:
                        return FAIL, error
            for set_name in names:
                for size in blocks.SIZES:
                    want = common.digest(self.data[(set_name, size)])
                    try:
                        got = stores.held(
                            origin, "protected-a", self.rel(set_name, size), self.token
                        )
                    except stores.Unreadable as e:
                        return FAIL, str(e)
                    if got != want:
                        return FAIL, (
                            f"{origin.svc} does not hold {set_name}/{size} intact"
                            if got
                            else f"{origin.svc} lacks {set_name}/{size}"
                        )
            if origin.pstore:
                after = self.tiers(origin)
                if before is None or after is None:
                    return (
                        FAIL,
                        f"{origin.svc}: no pelican_pstore_writes_total after its writes",
                    )
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

    def ensure(self, declared: bool) -> tuple[str, str]:
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

    def get(
        self, base_url: str, rel: str, data: bytes, request: Optional[blocks.Request]
    ) -> Optional[str]:
        """Read rel in /protected-a (or a range of it), which should hold
        data; what went wrong, or None."""
        headers = {"Range": blocks.header(request)} if request else {}
        answer = web.request(
            "GET",
            f"{base_url}/protected-a/{rel}",
            token=self.token,
            headers=headers,
            timeout=120,
        )
        if request is None:
            if answer.status != 200:
                return f"{answer.describe()}, not 200"
            if answer.body != data:
                return f"{len(answer.body)} bytes, not the object"
            return None
        return blocks.check(data, request, answer.status, answer.headers, answer.body)

    def read(
        self, base_url: str, set_name: str, size: int, request: Optional[blocks.Request]
    ) -> Optional[str]:
        """Read an object of a set (or a range of it); what went wrong, or
        None."""
        why = self.get(base_url, self.rel(set_name, size), self.data[(set_name, size)], request)
        if not why:
            return None
        return f"{set_name}/{size}{' ' + blocks.header(request) if request else ''}: {why}"

    def read_all(self, base_url: str, set_name: str) -> list[str]:
        """Every range of every size in a set, 8 at a time."""
        jobs = [(size, r) for size in blocks.SIZES for r in blocks.requests(size)]

        def one(job: tuple[int, blocks.Request]) -> Optional[str]:
            return self.read(base_url, set_name, *job)

        with ThreadPoolExecutor(8) as pool:
            found = pool.map(one, jobs)
        return [p for p in found if p]


def cache_dir(cache: common.Cache) -> str:
    """Where cache keeps its store, which it mounts at /data."""
    name = cache.svc[len("cache-") :] if cache.svc.startswith("cache-") else cache.svc
    return f"data/cache/{name}"


def upload_each(test: Test, base: str, objects: dict[str, bytes]) -> list[str]:
    """Put objects, by name, at <base>/<name> at every origin (see
    stores.seed_everywhere()); what went wrong."""
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    for name, data in objects.items():
        if errors:
            break
        errors += stores.seed_everywhere(
            test.origins, "protected-a", f"{base}/{name}", data, test.token, test.caps
        )
    return errors


def at_once(jobs: list[Callable[[], Optional[str]]]) -> list[Optional[str]]:
    """Run jobs together, each started once all are ready; each one's
    problem, or None."""
    barrier = threading.Barrier(len(jobs))

    def start(job: Callable[[], Optional[str]]) -> Optional[str]:
        try:
            barrier.wait(timeout=60)
        except threading.BrokenBarrierError:
            return "could not start with the others"
        return job()

    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(start, jobs))


# --------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.

NO_WRITES = "/protected-a takes no writes"


def put_declared(test: Test) -> tuple[str, str]:
    """Upload every size with a Content-Length to every origin."""
    result = test.ensure(True)
    return result if test.writes else (SKIP, NO_WRITES)


def put_chunked(test: Test) -> tuple[str, str]:
    """Upload every size in chunks, with no length, to every origin."""
    result = test.ensure(False)
    return result if test.writes else (SKIP, NO_WRITES)


def local_sizes(test: Test, name: str) -> tuple[str, dict[int, bytes]]:
    """A local directory, name, holding a new file of each size, named
    for its size: its path, and each file's bytes, by size."""
    directory = os.path.join(test.tmp, name)
    os.makedirs(directory)
    files = {size: os.urandom(size) for size in blocks.SIZES}
    for size, data in files.items():
        with open(os.path.join(directory, str(size)), "wb") as f:
            f.write(data)
    return directory, files


def pstore_tiers(test: Test) -> dict[str, dict[str, float]]:
    """Each pstore origin's writes by tier, so far."""
    return {o.svc: test.tiers(o) or dict.fromkeys(TIERS, 0.0) for o in test.origins if o.pstore}


def uploaded(test: Test, files: dict[int, bytes], collection: str) -> Optional[str]:
    """Why some origin does not hold each of files (bytes by size) intact,
    as collection/<size> in /protected-a, if one does not."""
    for size, data in files.items():
        held = stores.held_anywhere(
            test.origins,
            "protected-a",
            f"{collection}/{size}",
            {"protected-a": test.token},
        )
        if common.digest(data) not in held:
            return f"no origin holds {size} intact"
    return None


def tiers_landed(test: Test, before: dict[str, dict[str, float]]) -> tuple[Optional[str], str]:
    """On pstore origins, why the writes since before are not one of every
    size in the tier its size predicts for an upload of no declared
    length, as the clients upload, if they are not; and a note."""
    if not before:
        return None, ""
    got = {t: 0 for t in TIERS}
    for origin in test.origins:
        if not origin.pstore:
            continue
        was, now = before[origin.svc], test.tiers(origin)
        if now is None:
            return f"{origin.svc}: no pelican_pstore_writes_total after its writes", ""
        for t in got:
            got[t] += int(now[t] - was[t])
    want = {t: 0 for t in got}
    for size in blocks.SIZES:
        want[blocks.tier(size, False)] += 1
    if got != want:
        return f"tiers {got}, not {want} (the clients declare no length)", ""
    return None, f"tiers {got}"


def put_client(test: Test) -> tuple[str, str]:
    """`pelican object put` of every size, into a collection."""
    if not test.writes:
        return SKIP, NO_WRITES
    collection = f"{test.base}/put-client"
    errors = stores.make_dirs(test.origins, "protected-a", collection, test.token)
    if errors:
        return FAIL, "; ".join(errors)
    directory, files = local_sizes(test, "put-client")
    sources = [os.path.join(directory, str(size)) for size in files]
    before = pstore_tiers(test)
    code, out, err = test.session.pelican_cmd(
        "object",
        "put",
        "--token",
        test.token_file,
        *sources,
        f"{test.fed.url}/protected-a/{collection}/",
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    why = uploaded(test, files, collection)
    if why:
        return FAIL, why
    why, note = tiers_landed(test, before)
    if why:
        return FAIL, why
    return PASS, "; ".join(n for n in (f"{len(sources)} objects", note) if n)


def plugin(
    test: Test, name: str, urls: list[str], directory: str, upload: bool
) -> tuple[bool, str]:
    """Run stash_plugin on urls, each object's local file named as its
    URL ends, in directory, with the server's token for /protected-a:
    whether it says every transfer succeeded, and its result ads (or,
    where it wrote none, its output)."""
    creds = os.path.join(test.tmp, f"{name}.creds")
    os.makedirs(creds)
    shutil.copyfile(test.token_file, os.path.join(creds, "server.use"))
    infile, outfile = (os.path.join(test.tmp, f"{name}.{ext}") for ext in ("in", "ad"))
    with open(infile, "w", encoding="utf-8") as f:
        for url in urls:
            f.write(f'[ Url="{url}"; LocalFileName="{transfers.local_name(url)}" ]\n')
    env = dict(test.session.env)
    env["_CONDOR_CREDS"] = creds
    args = [test.session.plugin, *(["-upload"] if upload else [])]
    try:
        done = subprocess.run(  # nosec B603
            [*args, "-infile", infile, "-outfile", outfile],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLIENT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CLIENT_TIMEOUT:.0f}s"
    try:
        with open(outfile, encoding="utf-8") as f:
            ads = f.read()
    except FileNotFoundError:
        ads = ""
    return transfers.plugin_passed(ads), ads or done.stderr or done.stdout


def put_plugin(test: Test) -> tuple[str, str]:
    """stash_plugin's upload of every size."""
    if not test.writes:
        return SKIP, NO_WRITES
    collection = f"{test.base}/put-plugin"
    errors = stores.make_dirs(test.origins, "protected-a", collection, test.token)
    if errors:
        return FAIL, "; ".join(errors)
    directory, files = local_sizes(test, "put-plugin")
    urls = [f"{test.fed.url}/protected-a/{collection}/{size}" for size in files]
    before = pstore_tiers(test)
    passed, said = plugin(test, "put-plugin", urls, directory, upload=True)
    if not passed:
        return FAIL, f"failed: {last_line(said)}"
    why = uploaded(test, files, collection)
    if why:
        return FAIL, why
    why, note = tiers_landed(test, before)
    if why:
        return FAIL, why
    return PASS, "; ".join(n for n in (f"{len(urls)} objects", note) if n)


def origin_read(test: Test) -> tuple[str, str]:
    """Each origin returns every object whole, and every range."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    if why := test.not_uploaded(True, False):
        return FAIL, why
    problems: list[str] = []
    for origin in test.origins:
        for set_name in ("whole", "ranged"):
            problems += [
                p
                for p in (test.read(origin.url, set_name, size, None) for size in blocks.SIZES)
                if p
            ]
            problems += test.read_all(origin.url, set_name)
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s)"


def downloaded(test: Test, set_name: str, directory: str) -> Optional[str]:
    """Why directory does not hold the set's object of each size, named
    for its size, and nothing else, if it does not."""
    names = {str(size) for size in blocks.SIZES}
    extra = sorted(set(os.listdir(directory)) - names)
    if extra:
        return f"left {', '.join(extra)}"
    for size in blocks.SIZES:
        got = common.sha256(os.path.join(directory, str(size)))
        if got != common.digest(test.data[(set_name, size)]):
            return f"{size} is {'missing' if got is None else 'different'}"
    return None


def client_get(test: Test) -> tuple[str, str]:
    """`pelican object get` of every size, through a cache and with
    --direct."""
    if why := test.not_uploaded(True):
        return FAIL, why
    urls = [f"{test.fed.url}/protected-a/{test.rel('client', size)}" for size in blocks.SIZES]
    notes: list[str] = []
    routed = "through a cache" if test.caches else "without --direct"
    for label, flags in ((routed, []), ("direct", ["--direct"])):
        target = os.path.join(test.tmp, f"client-get-{'direct' if flags else 'cache'}")
        os.makedirs(target)
        code, out, err = test.session.pelican_cmd(
            "object", "get", "--token", test.token_file, *flags, *urls, target
        )
        if flags and not test.direct:
            if code == 0:
                return FAIL, f"{label}: succeeded, but /protected-a takes no direct clients"
            why = transfers.refused(out + err)
            if why is None:
                return FAIL, f"{label}: failed, but not by refusal: {last_line(err or out)}"
            if os.listdir(target):
                return FAIL, f"{label}: refused, but wrote {sorted(os.listdir(target))}"
            notes.append(f"{label}: refused ({why})")
            continue
        if code != 0:
            return FAIL, f"{label}: exit {code}: {last_line(err or out)}"
        why = downloaded(test, "client", target)
        if why:
            return FAIL, f"{label}: {why}"
        notes.append(label)
    return PASS, ", ".join(notes)


def plugin_get(test: Test) -> tuple[str, str]:
    """stash_plugin's download of every size, through a cache, then
    direct (?directread, the plugin's only way to ask for it); where
    /protected-a takes no direct clients, that must be refused."""
    if why := test.not_uploaded(True):
        return FAIL, why
    notes: list[str] = []
    routed = "through a cache" if test.caches else "without ?directread"
    for label, query in ((routed, ""), ("direct", transfers.DIRECT_QUERY)):
        name = f"plugin-get-{'direct' if query else 'cache'}"
        directory = os.path.join(test.tmp, name)
        os.makedirs(directory)
        urls = [
            f"{test.fed.url}/protected-a/{test.rel('plugin', size)}{query}"
            for size in blocks.SIZES
        ]
        passed, said = plugin(test, name, urls, directory, upload=False)
        if query and not test.direct:
            if passed:
                return FAIL, f"{label}: succeeded, but /protected-a takes no direct clients"
            lines = transfers.failure_lines("plugin", said)
            why = transfers.combine(transfers.classify(line) for line in lines)
            if why not in transfers.REFUSALS | {"mixed"}:
                return FAIL, f"{label}: failed ({why}), but not by refusal: {last_line(said)}"
            if os.listdir(directory):
                return FAIL, f"{label}: refused, but wrote {sorted(os.listdir(directory))}"
            notes.append(f"{label}: refused ({why})")
            continue
        if not passed:
            return FAIL, f"{label}: failed: {last_line(said)}"
        why = downloaded(test, "plugin", directory)
        if why:
            return FAIL, f"{label}: {why}"
        notes.append(label)
    return PASS, ", ".join(notes)


def cache_whole_first(test: Test) -> tuple[str, str]:
    """Each cache: a cold whole read, a warm one, then every range."""
    if why := test.not_uploaded(True):
        return FAIL, why
    problems: list[str] = []
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


def cache_range_first(test: Test) -> tuple[str, str]:
    """Each cache: a cold range first, then every range, then the whole."""
    if why := test.not_uploaded(False):
        return FAIL, why
    problems: list[str] = []
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


def cache_overlap(test: Test) -> tuple[str, str]:
    """Each plan of blocks.overlap_plans, read in turn from an object of
    its own: every read after the first covers blocks that the cache
    fetched for an earlier one and blocks that it must fetch now. Then
    blocks.concurrent_reads, all at once, of one more cold object. After
    each, the whole object must come back intact."""
    plans = blocks.overlap_plans()
    base = f"{test.base}/overlap"
    objects = {
        f"{c.svc}-{plan}": os.urandom(blocks.OVERLAP_SIZE)
        for c in test.caches
        for plan in [*plans, "concurrent"]
    }
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    reads = blocks.concurrent_reads()
    for cache in test.caches:
        for plan, requests in plans.items():
            name = f"{cache.svc}-{plan}"
            for n, request in enumerate(requests, 1):
                why = test.get(cache.url, f"{base}/{name}", objects[name], request)
                if why:
                    problems.append(
                        f"{cache.svc}: {plan}, read {n} ({blocks.header(request)}): {why}"
                    )
            why = test.get(cache.url, f"{base}/{name}", objects[name], None)
            if why:
                problems.append(f"{cache.svc}: {plan}, the whole object afterward: {why}")
        name = f"{cache.svc}-concurrent"
        found = at_once(
            [
                functools.partial(test.get, cache.url, f"{base}/{name}", objects[name], r)
                for r in reads
            ]
        )
        for read, why in zip(reads, found):
            if why:
                what = blocks.header(read) if read else "the whole object"
                problems.append(f"{cache.svc}: at once, {what}: {why}")
        why = test.get(cache.url, f"{base}/{name}", objects[name], None)
        if why:
            problems.append(f"{cache.svc}: the whole object after the reads at once: {why}")
    if problems:
        return FAIL, first(problems)
    return (
        PASS,
        f"{len(test.caches)} cache(s): {len(plans)} plans, then {len(reads)} reads at once",
    )


def has_age(test: Test, cache: common.Cache, rel: str) -> Optional[bool]:
    """Whether a V2 cache says, with Age, that it holds all of rel; None
    if a HEAD of it fails."""
    answer = web.request("HEAD", f"{cache.url}/protected-a/{rel}", token=test.token)
    return bool(answer.header("Age")) if answer.status == 200 else None


def read_cinfo(cache: common.Cache, rel: str) -> tuple[Optional[blocks.Cinfo], str]:
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


def wait_cinfo(
    cache: common.Cache, rel: str, want: Callable[[blocks.Cinfo], bool], seconds: float
) -> tuple[Optional[blocks.Cinfo], str]:
    """read_cinfo, until what it finds is wanted or seconds pass: the
    cache writes its .cinfo some time after the blocks."""
    last: list[tuple[Optional[blocks.Cinfo], str]] = []

    def wanted() -> bool:
        last[:] = [read_cinfo(cache, rel)]
        found = last[0][0]
        return found is not None and want(found)

    common.wait_for(wanted, seconds)
    return last[0]


class Findings:
    """What a scenario found: problems, which fail it; doubts, which leave
    it inconclusive; and notes on what went right."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.doubts: list[str] = []
        self.notes: list[str] = []

    def fail(self, why: str) -> None:
        """Record a problem."""
        self.problems.append(why)

    def doubt(self, why: str) -> None:
        """Record a doubt."""
        self.doubts.append(why)

    def note(self, what: str) -> None:
        """Record what went right."""
        self.notes.append(what)

    def verdict(self) -> tuple[str, str]:
        """FAIL, INCONCLUSIVE, or PASS, and a note."""
        if self.problems:
            return FAIL, first(self.problems)
        if self.doubts:
            return INCONCLUSIVE, "; ".join(self.doubts + self.notes)
        return PASS, "; ".join(self.notes)


def assemble(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """cache_assemble at one cache, of rel, which holds data."""
    ranges = [(r,) for r in blocks.ASSEMBLE]
    why = test.get(cache.url, rel, data, ranges[0])
    if why:
        found.fail(f"{cache.svc}: the first range: {why}")
        return
    # Whether an XRootD cache's .cinfo could be read after the first range.
    seen = False
    if cache.kind == "v2":
        partial = has_age(test, cache, rel)
        if partial is None:
            found.fail(f"{cache.svc}: a HEAD after the first range failed")
            return
        if partial:
            found.doubt(f"{cache.svc}: Age after the first range, so it cannot show completion")
    else:
        cinfo, why = wait_cinfo(cache, rel, lambda c: 0 < c.count < c.blocks, 30)
        seen = cinfo is not None
        if cinfo is None:
            found.doubt(f"{cache.svc}: after the first range, {why}")
        elif not 0 < cinfo.count < cinfo.blocks:
            found.doubt(
                f"{cache.svc}: {cinfo.count} of {cinfo.blocks} blocks after the first range,"
                + " not some"
            )
    for request in ranges[1:]:
        why = test.get(cache.url, rel, data, request)
        if why:
            found.fail(f"{cache.svc}: {blocks.header(request)}: {why}")
    if cache.kind == "v2":
        if common.wait_for(lambda: bool(has_age(test, cache, rel)), 10, interval=1):
            found.note(f"{cache.svc}: Age")
        else:
            found.fail(f"{cache.svc}: no Age after every range, so it does not hold it all")
    else:
        cinfo, why = wait_cinfo(cache, rel, lambda c: c.complete, 60)
        if cinfo is None and seen:
            found.fail(
                f"{cache.svc}: after every range, {why}, though it was readable after the first"
            )
        elif cinfo is None:
            found.doubt(f"{cache.svc}: after every range, {why}")
        elif not cinfo.complete:
            found.fail(f"{cache.svc}: {cinfo.count} of {cinfo.blocks} blocks after every range")
        else:
            found.note(f"{cache.svc}: {cinfo.blocks} of {cinfo.blocks} blocks")
    why = test.get(cache.url, rel, data, None)
    if why:
        found.fail(f"{cache.svc}: the whole object afterward: {why}")


def cache_assemble(test: Test) -> tuple[str, str]:
    """blocks.ASSEMBLE, read in its order from a cold object: ranges that
    overlap, and add up to the object. The cache must hold part of it
    after the first, and all of it after the last: a V2 cache sends Age
    only then, and an XRootD cache's .cinfo shows which blocks it holds.
    Then the whole object must come back intact. Only a cache that cannot
    show the partial state after the first (with Age, with no .cinfo, or
    with a .cinfo showing none or all of the blocks) leaves the result
    unsure; an XRootD cache whose .cinfo could be read then must still
    have one, showing every block, after the last."""
    base = f"{test.base}/assemble"
    objects = {c.svc: os.urandom(blocks.ASSEMBLE_SIZE) for c in test.caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for cache in test.caches:
        assemble(test, cache, f"{base}/{cache.svc}", objects[cache.svc], found)
    return found.verdict()


# How much of an object cache-abandoned reads before it gives up: part of
# the first XRootD block, and more than a read batch of V2 blocks.
ABANDON_AFTER = 50000


def cache_abandoned(test: Test) -> tuple[str, str]:
    """Each cache: a cold whole read of an object of its own, which its
    client gives up on after ABANDON_AFTER bytes. Those bytes must be the
    object's; and afterward, its last block, then the whole object, must
    come back intact, however far the cache had fetched it."""
    base = f"{test.base}/abandoned"
    objects = {c.svc: os.urandom(blocks.OVERLAP_SIZE) for c in test.caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    for cache in test.caches:
        rel, data = f"{base}/{cache.svc}", objects[cache.svc]
        answer = web.read_part(f"{cache.url}/protected-a/{rel}", test.token, ABANDON_AFTER)
        if answer.status != 200:
            problems.append(f"{cache.svc}: the read given up on: {answer.describe()}, not 200")
            continue
        if answer.body != data[:ABANDON_AFTER]:
            problems.append(
                f"{cache.svc}: the read given up on got {len(answer.body)} bytes,"
                + f" not the object's first {ABANDON_AFTER}"
            )
        last = ((len(data) - blocks.BLOCK, len(data) - 1),)
        for request in (last, None):
            why = test.get(cache.url, rel, data, request)
            if why:
                what = blocks.header(request) if request else "the whole object"
                problems.append(f"{cache.svc}: {what} afterward: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.caches)} cache(s)"


# The sizes whose digests `digests` asks each server for: empty, within a
# block, past each kind of block, past pstore's spill, and past a chunk.
DIGEST_SIZES = (
    0,
    1,
    blocks.BLOCK + 1,
    blocks.XROOTD_BLOCK + 1,
    blocks.SPILL + 1,
    blocks.CHUNK_UNDECLARED + 1,
)
# What an origin must report, as `pelican object stat --checksums` asks.
ORIGIN_DIGESTS: tuple[str, ...] = ("crc32c", "md5")


def reported(
    test: Test, url: str, rel: str, data: bytes, algorithms: tuple[str, ...]
) -> tuple[list[str], set[str]]:
    """Ask url's server for each of algorithms in turn of rel in
    /protected-a, which holds data, with a HEAD and Want-Digest: what is
    wrong with what it reports, and the algorithms it reports rightly."""
    problems: list[str] = []
    right: set[str] = set()
    for algorithm in algorithms:
        answer = web.request(
            "HEAD",
            f"{url}/protected-a/{rel}",
            token=test.token,
            headers={"Want-Digest": algorithm},
        )
        if answer.status != 200:
            problems.append(f"HEAD, for {algorithm}: {answer.describe()}")
            continue
        wrong, found = digests.check(answer.header("Digest"), data)
        problems += wrong
        right.update(found)
    return problems, right


def server_digests(test: Test, url: str, must: bool) -> tuple[list[str], set[str]]:
    """What is wrong with the digests that url's server reports of the
    objects of DIGEST_SIZES in the `whole` and `ranged` sets: asked by
    HEAD for each of digests.ALGORITHMS in turn, and by GET for crc32c,
    the client's default. If must, it must report ORIGIN_DIGESTS of each.
    Also the algorithms it reports rightly."""
    problems: list[str] = []
    given: set[str] = set()
    for set_name in ("whole", "ranged"):
        for size in DIGEST_SIZES:
            rel, data = test.rel(set_name, size), test.data[(set_name, size)]
            wrong, right = reported(test, url, rel, data, digests.ALGORITHMS)
            problems += [f"{set_name}/{size}: {w}" for w in wrong]
            given |= right
            lacking = sorted(set(ORIGIN_DIGESTS) - right)
            if must and lacking:
                problems.append(f"{set_name}/{size}: reports no {', '.join(lacking)}")
            answer = web.request(
                "GET",
                f"{url}/protected-a/{rel}",
                token=test.token,
                headers={"Want-Digest": "crc32c"},
                timeout=120,
            )
            if answer.status != 200 or answer.body != data:
                problems.append(
                    f"{set_name}/{size}: GET: {answer.describe()}, or not the object"
                )
                continue
            wrong, found = digests.check(answer.header("Digest"), data)
            problems += [f"{set_name}/{size}: GET: {w}" for w in wrong]
            given.update(found)
    return problems, given


def digests_scenario(test: Test) -> tuple[str, str]:
    """Each server's digests (see server_digests()): every one it reports
    must be the object's, as the client checks it. Each origin must report
    ORIGIN_DIGESTS of each object, unless its storage cannot; a cache need
    report none, and the note names those that report none. The origins
    are asked only if they take direct clients."""
    if why := test.not_uploaded(True, False):
        return FAIL, why
    cannot = stores.cannot(test.fed, "checksum")
    servers = [(o.svc, o.url, not cannot) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url, False) for c in test.caches]
    problems: list[str] = []
    silent: list[str] = []
    for svc, url, must in servers:
        wrong, given = server_digests(test, url, must)
        problems += [f"{svc}: {w}" for w in wrong]
        if not given:
            silent.append(svc)
    if problems:
        return FAIL, first(problems)
    notes = [f"{len(servers)} server(s)"]
    if silent:
        notes.append(f"no digest from {', '.join(silent)}")
    if cannot:
        notes.append(cannot)
    return PASS, "; ".join(notes)


def write_version(
    test: Test, origin: common.Origin, rel: str, how: str, data: bytes
) -> Optional[str]:
    """Put data at rel in /protected-a at origin: `seed` it (see
    stores.seed()), `put` it, or write it in the store's `file` a second
    after the last write; why that failed, or None."""
    if how == "file":
        time.sleep(1.1)
        return stores.write_file(origin.store_of("protected-a"), rel, data)
    if how == "put":
        return stores.put(origin, "protected-a", rel, data, test.token)
    return stores.seed(origin, "protected-a", rel, data, test.token, test.caps)


def digests_after_writes(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], int]:
    """Write versions of rel in /protected-a at origin, as digest_overwrite
    says, and after each, check that origin serves it, and reports
    ORIGIN_DIGESTS of it: what was wrong, and how many versions there
    were."""
    steps = [("the first version", "seed")]
    if test.writes:
        steps.append(("a PUT at once", "put"))
    if not origin.pstore:
        steps.append(("a write in the store a second later", "file"))
    problems: list[str] = []
    etags: list[str] = []
    for what, how in steps:
        data = os.urandom(3 * blocks.BLOCK + 5)
        error = write_version(test, origin, rel, how, data)
        if error:
            return problems + [f"{what}: {error}"], len(steps)
        served = web.request("GET", stores.url(origin, "protected-a", rel), token=test.token)
        if served.status != 200 or served.body != data:
            return problems + [f"after {what}, it serves another version"], len(steps)
        etag = served.header("ETag")
        if etag and etag in etags:
            problems.append(f"after {what}, its ETag is still an earlier version's, {etag}")
        etags.append(etag)
        wrong, right = reported(test, origin.url, rel, data, ORIGIN_DIGESTS)
        problems += [f"after {what}: {w}" for w in wrong]
        if right != set(ORIGIN_DIGESTS):
            problems.append(f"after {what}, it reports only {sorted(right)}")
    return problems, len(steps)


def digest_overwrite(test: Test) -> tuple[str, str]:
    """Each origin, once it has reported ORIGIN_DIGESTS of an object of
    its own: after a PUT of another version, at once, and in a store on
    disk, after a write there a second later, which it must notice (a
    native origin keeps digests by the file's modification time, to the
    second; origin_serve/checksum.go). Each time, the digests it reports
    must be those of the version it serves, and its ETag, if it sends
    one, must differ from every earlier version's, since by it a cache
    tells that an object has changed."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    cannot = stores.cannot(test.fed, "checksum")
    if cannot:
        return SKIP, cannot
    base = f"{test.base}/digest-overwrite"
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        wrong, versions = digests_after_writes(test, origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc}: {versions} versions")
    if problems:
        return FAIL, first(problems)
    return PASS, ", ".join(notes)


# The sizes that past-end asks about.
PAST_END_SIZES = (1, blocks.BLOCK, blocks.XROOTD_BLOCK + 1, blocks.SPILL + 1)


def past_end(test: Test) -> tuple[str, str]:
    """Each server, asked for ranges that start at or past the end of
    objects of PAST_END_SIZES in the `whole` and `ranged` sets
    (blocks.past_end()): 416, with Content-Range `bytes */<size>` if it
    says, and none of the object's bytes; or, if it ignores Range, as RFC
    9110 lets it, 200 with the whole object. The origins are asked only
    if they take direct clients."""
    if why := test.not_uploaded(True, False):
        return FAIL, why
    servers = [(o.svc, o.url) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url) for c in test.caches]
    problems: list[str] = []
    for svc, url in servers:
        for set_name in ("whole", "ranged"):
            for size in PAST_END_SIZES:
                data = test.data[(set_name, size)]
                for request in blocks.past_end(size):
                    range_header = blocks.header(request)
                    answer = web.request(
                        "GET",
                        f"{url}/protected-a/{test.rel(set_name, size)}",
                        token=test.token,
                        headers={"Range": range_header},
                    )
                    what = f"{svc}: {set_name}/{size} {range_header}"
                    said = answer.header("Content-Range")
                    if answer.status == 200 and answer.body == data:
                        continue
                    if answer.status != 416:
                        problems.append(f"{what}: {answer.describe()}, not 416")
                    elif said and said.replace(" ", "") != f"bytes*/{size}":
                        problems.append(f"{what}: Content-Range '{said}', not 'bytes */{size}'")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers)} server(s)"


def empty(test: Test) -> tuple[str, str]:
    """Every server answers a GET of an empty object with 200 and no body,
    and a range of it with 200 and no body (RFC 7233 lets it ignore Range)
    or with 416, since no range of it is satisfiable. The origins are
    asked only if they take direct clients."""
    if why := test.not_uploaded(True, False):
        return FAIL, why
    servers = [(o.svc, o.url) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url) for c in test.caches]
    notes: list[str] = []
    for svc, url in servers:
        for set_name in ("whole", "ranged"):
            target = f"{url}/protected-a/{test.rel(set_name, 0)}"
            answer = web.request("GET", target, token=test.token)
            if answer.status != 200 or answer.body:
                return (
                    FAIL,
                    f"{svc}: {set_name}/0: {answer.describe()}, {len(answer.body)} bytes",
                )
            answer = web.request(
                "GET", target, token=test.token, headers={"Range": "bytes=0-0"}
            )
            if answer.status not in (200, 416):
                return FAIL, (
                    f"{svc}: a range of {set_name}/0 got {answer.describe()}, not 200 or 416"
                )
            # A 416's body explains it; a success's must be empty.
            if answer.status == 200 and answer.body:
                return FAIL, (
                    f"{svc}: a range of {set_name}/0 got {answer.describe()}"
                    f" with {len(answer.body)} bytes"
                )
        notes.append(f"{svc} ok")
    return PASS, ", ".join(notes)


# mixed-version's object: three XRootD blocks, so that each kind of cache
# fetches only part of it for the first read. The later range is one
# BLOCK in the third XRootD block, and on a BLOCK edge, which neither
# kind fetched for the first.
MIXED_SIZE = 3 * blocks.XROOTD_BLOCK
MIXED_LATER = -(-2 * blocks.XROOTD_BLOCK // blocks.BLOCK) * blocks.BLOCK


def mixed_version(test: Test) -> tuple[str, str]:
    """Each cache reads the first block of an object, and the origins then
    take another version of it. Every later response, to a range the
    cache has yet to fetch and to the whole object, must be entirely one
    version, and the same one."""
    size = MIXED_SIZE
    rel = f"{test.base}/mixed"
    v1, v2 = os.urandom(size), os.urandom(size)
    errors = stores.make_dirs(test.origins, "protected-a", test.base, test.token)
    errors += stores.seed_everywhere(
        test.origins, "protected-a", rel, v1, test.token, test.caps
    )
    if errors:
        return FAIL, "; ".join(errors)

    def get(cache: common.Cache, request: Optional[str]) -> web.Response:
        headers = {"Range": request} if request else {}
        return web.request(
            "GET",
            f"{cache.url}/protected-a/{rel}",
            token=test.token,
            headers=headers,
            timeout=120,
        )

    block = blocks.BLOCK
    for cache in test.caches:
        answer = get(cache, f"bytes=0-{block - 1}")
        if answer.status != 206 or answer.body != v1[:block]:
            return (
                FAIL,
                f"{cache.svc}: the first block of v1 came back wrong ({answer.describe()})",
            )
    errors = stores.seed_everywhere(test.origins, "protected-a", rel, v2, test.token, test.caps)
    if errors:
        return FAIL, "; ".join(errors)
    start, end = MIXED_LATER, MIXED_LATER + block
    later = f"bytes {start}-{end - 1}"
    notes: list[str] = []
    for cache in test.caches:
        answer = get(cache, f"bytes={start}-{end - 1}")
        part = answer.body
        if answer.status != 206:
            return FAIL, f"{cache.svc}: {later} got {answer.describe()}, not 206"
        if part not in (v1[start:end], v2[start:end]):
            return FAIL, f"{cache.svc}: {later} are neither version"
        answer = get(cache, None)
        if answer.status != 200:
            return FAIL, f"{cache.svc}: the whole object got {answer.describe()}, not 200"
        if answer.body == v1:
            version = "v1"
        elif answer.body == v2:
            version = "v2"
        else:
            which = "".join(
                (
                    "1"
                    if answer.body[i : i + block] == v1[i : i + block]
                    else "2" if answer.body[i : i + block] == v2[i : i + block] else "?"
                )
                for i in range(0, size, block)
            )
            return (
                FAIL,
                f"{cache.svc}: the whole object mixes versions, block by block: {which}",
            )
        range_version = "v1" if part == v1[start:end] else "v2"
        if range_version != version:
            return FAIL, (
                f"{cache.svc}: {later} came back as {range_version},"
                f" the whole object as {version}"
            )
        notes.append(f"{cache.svc} served {version}")
    return PASS, ", ".join(notes)


# The overwrite scenarios' objects: one keeps its size, one grows, and one
# shrinks, by OVERWRITE_CHANGE.
OVERWRITE_SIZE = 3 * blocks.XROOTD_BLOCK + 7
OVERWRITE_CHANGE = 3 * blocks.BLOCK + 11
# How long past an object's freshness a cache may take to serve the new
# version: requests are 3 seconds apart, and it revalidates by fetching
# the object again.
REVALIDATION_MARGIN = 45


class Versions:
    """Two versions of each cache's objects for the overwrite scenarios,
    under base, by name: v2 is as long as v1, longer, or shorter."""

    CHANGES = {"same": 0, "grown": OVERWRITE_CHANGE, "shrunk": -OVERWRITE_CHANGE}
    NAMES = tuple(CHANGES)

    def __init__(self, test: Test, base: str):
        self.test = test
        self.base = base
        self.v1: dict[str, bytes] = {}
        self.v2: dict[str, bytes] = {}
        for cache in test.caches:
            for name, change in self.CHANGES.items():
                key = f"{cache.svc}-{name}"
                self.v1[key] = os.urandom(OVERWRITE_SIZE)
                self.v2[key] = os.urandom(OVERWRITE_SIZE + change)

    def which(
        self, cache: common.Cache, key: str, request: Optional[blocks.Request]
    ) -> tuple[Optional[str], str]:
        """Which version a read of object key (or a range of it) through
        cache came back as (see blocks.which_version())."""
        headers = {"Range": blocks.header(request)} if request else {}
        url = f"{cache.url}/protected-a/{self.base}/{key}"
        answer = web.request("GET", url, token=self.test.token, headers=headers, timeout=120)
        return blocks.which_version(
            {"v1": self.v1[key], "v2": self.v2[key]},
            request,
            answer.status,
            answer.headers,
            answer.body,
        )

    def now(self, cache: common.Cache) -> list[Optional[str]]:
        """Which version a whole read of each of cache's objects came back
        as."""
        return [self.which(cache, f"{cache.svc}-{n}", None)[0] for n in self.NAMES]

    def battery(self, cache: common.Cache) -> tuple[Counter[str], list[str]]:
        """Every read of cache's objects, whole and in every range of
        either version: how many came back as each version, and what was
        wrong."""
        jobs: list[tuple[str, Optional[blocks.Request]]] = []
        for n in self.NAMES:
            key = f"{cache.svc}-{n}"
            ranges = blocks.requests(len(self.v1[key])) + blocks.requests(len(self.v2[key]))
            jobs += [(key, r) for r in [None, *dict.fromkeys(ranges)]]

        def read(job: tuple[str, Optional[blocks.Request]]) -> tuple[Optional[str], str]:
            return self.which(cache, *job)

        with ThreadPoolExecutor(8) as pool:
            found = list(pool.map(read, jobs))
        counts: Counter[str] = Counter()
        wrong: list[str] = []
        for (key, request), (version, why) in zip(jobs, found):
            if version is None:
                what = blocks.header(request) if request else "whole"
                wrong.append(f"{key} {what}: {why}")
            else:
                counts[version] += 1
        return counts, wrong


def cache_control_problem(test: Test, rel: str, control: str) -> Optional[str]:
    """Why an origin does not send Cache-Control control for rel in
    /protected-a, if one does not."""
    for origin in test.origins:
        answer = web.request(
            "GET",
            f"{origin.url}/protected-a/{rel}",
            token=test.token,
            headers={"Range": "bytes=0-0"},
        )
        sent = answer.header("Cache-Control")
        if sent != control:
            return f"{origin.svc} sends Cache-Control '{sent}', not ORIGIN_CACHE_CONTROL's '{control}'"
    return None


def overwrite(test: Test, range_first: bool) -> tuple[str, str]:
    """Each cache reads v1 of three objects of its own, whole twice or a
    range then whole, and the origins then take v2: of the same size,
    larger, or smaller. Every response must then be all v1 or all v2,
    with that version's size. With ORIGIN_CACHE_CONTROL
    (`-p origin-max-age`), every origin must send it, and each cache, once
    its copy is stale, must serve v2, and only v2. Without it, a cache
    decides for itself how long its copy stays fresh. The origins are
    asked only if they take direct clients."""
    kind = "range-first" if range_first else "cached"
    versions = Versions(test, f"{test.base}/overwrite-{kind}")
    errors = upload_each(test, versions.base, versions.v1)
    if errors:
        return FAIL, errors[0]
    control = test.fed.origin_cache_control
    if control and test.direct:
        why = cache_control_problem(test, f"{versions.base}/{next(iter(versions.v1))}", control)
        if why:
            return FAIL, why
    cached_at: dict[str, float] = {}
    reads = [((0, 2 * blocks.BLOCK + 99),), None] if range_first else [None, None]
    for cache in test.caches:
        cached_at[cache.svc] = time.time()
        for n in Versions.NAMES:
            key = f"{cache.svc}-{n}"
            for request in reads:
                why = test.get(cache.url, f"{versions.base}/{key}", versions.v1[key], request)
                if why:
                    return FAIL, f"{cache.svc}: {n} before the overwrite: {why}"
    for key, data in versions.v2.items():
        rel = f"{versions.base}/{key}"
        errors = stores.seed_everywhere(
            test.origins, "protected-a", rel, data, test.token, test.caps
        )
        if errors:
            return FAIL, errors[0]

    fresh = blocks.freshness(control) if control else None
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.caches:
        counts, wrong = versions.battery(cache)
        problems += wrong
        served = ", ".join(f"{v} {counts[v]}" for v in sorted(counts)) or "nothing"
        if fresh is None:
            notes.append(f"{cache.svc} served {served}")
            continue
        deadline = cached_at[cache.svc] + fresh + REVALIDATION_MARGIN
        now = versions.now(cache)
        while not all(v == "v2" for v in now) and time.time() <= deadline:
            time.sleep(3)
            now = versions.now(cache)
        waited = time.time() - cached_at[cache.svc]
        if not all(v == "v2" for v in now):
            seen = " (it first saw each through a range)" if range_first else ""
            problems.append(
                f"{cache.svc} still served {', '.join(map(str, now))} {waited:.0f}s"
                + f" after caching v1, past its freshness of {fresh:g}s{seen}"
            )
            continue
        counts, wrong = versions.battery(cache)
        problems += wrong
        if counts["v1"]:
            problems.append(f"{cache.svc} served v1 {counts['v1']} times after serving v2")
        notes.append(f"{cache.svc} served {served}, then v2 by {waited:.0f}s")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


def overwrite_cached(test: Test) -> tuple[str, str]:
    """overwrite(), of objects first read whole."""
    return overwrite(test, False)


def overwrite_range_first(test: Test) -> tuple[str, str]:
    """overwrite(), of objects first read through a range."""
    return overwrite(test, True)


def held_everywhere(test: Test, rel: str, data: bytes) -> bool:
    """Whether every origin holds data at rel in /protected-a."""
    held = stores.held_anywhere(test.origins, "protected-a", rel, {"protected-a": test.token})
    return all(h == common.digest(data) for h in held)


def put_through(test: Test, cache: common.Cache, rel: str, v1: bytes) -> Optional[str]:
    """write_through()'s PUTs through cache of rel in /protected-a, which
    every origin holds as v1; what went wrong, or None."""
    url = f"{cache.url}/protected-a/{rel}"
    v2 = os.urandom(len(v1) - OVERWRITE_CHANGE)
    why = test.get(cache.url, rel, v1, None)
    if why:
        return f"before the writes: {why}"
    read_only = test.session.token("protected-a", credentials.R)
    answer = web.request("PUT", url, token=read_only, upload=v2)
    if answer.status not in (401, 403):
        return f"a PUT with a read-only token: {answer.describe()}, not 401 or 403"
    if not held_everywhere(test, rel, v1):
        return "a PUT with a read-only token changed an origin"
    answer = web.request("PUT", url, token=test.token, upload=v2)
    if not test.writes:
        if answer.status not in (401, 403, 405):
            return f"a PUT, with no Writes: {answer.describe()}, not a refusal"
        if not held_everywhere(test, rel, v1):
            return "a PUT, refused, changed an origin"
        return None
    if not answer.ok:
        return f"a PUT: {answer.describe()}"
    held = stores.held_anywhere(test.origins, "protected-a", rel, {"protected-a": test.token})
    if common.digest(v2) not in held:
        return "a PUT succeeded, but no origin holds what it wrote"
    # Then only the cache's own copy could still be v1.
    errors = stores.seed_everywhere(test.origins, "protected-a", rel, v2, test.token, test.caps)
    if errors:
        return errors[0]
    why = test.get(cache.url, rel, v2, None)
    if why:
        return f"after the PUT: {why}"
    return None


def delete_through(test: Test, cache: common.Cache, rel: str) -> Optional[str]:
    """write_through()'s DELETE through cache of rel in /protected-a,
    which every origin holds; what went wrong, or None."""
    url = f"{cache.url}/protected-a/{rel}"
    answer = web.request("DELETE", url, token=test.token)
    if not answer.ok:
        return f"a DELETE: {answer.describe()}"
    held = stores.held_anywhere(test.origins, "protected-a", rel, {"protected-a": test.token})
    if None not in held:
        return "a DELETE succeeded, but every origin holds the object"
    # Then only the cache's own copy could still be served.
    for origin in test.origins:
        error = stores.delete(origin, "protected-a", rel, test.token)
        if error:
            return error
    answer = web.request("GET", url, token=test.token)
    if answer.status != 404:
        return f"after the DELETE: {answer.describe()}, not 404"
    return None


def write_through(test: Test) -> tuple[str, str]:
    """Each V2 cache takes writes of /protected-a, which it relays to an
    origin through the director, and then drops its copy (proxyWrite in
    Pelican's local_cache/persistent_cache_api.go). Of an object it holds:
    a PUT with a read-only token must be refused, and change nothing; one
    with the server's token must reach an origin; and once every origin
    holds what was written, the cache must serve it at once. Then
    likewise a DELETE, after which the cache must serve nothing, unless
    the origins' storage cannot delete. Where /protected-a takes no
    writes, the PUT must be refused. An XRootD cache takes no writes,
    and is passed over."""
    caches = [c for c in test.caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no V2 cache, which alone takes writes"
    base = f"{test.base}/write-through"
    v1 = {c.svc: os.urandom(OVERWRITE_SIZE) for c in caches}
    errors = upload_each(test, base, v1)
    if errors:
        return FAIL, errors[0]
    cannot_delete = stores.cannot(test.fed, "delete")
    deletes = test.writes and not cannot_delete
    problems: list[str] = []
    for cache in caches:
        rel = f"{base}/{cache.svc}"
        why = put_through(test, cache, rel, v1[cache.svc])
        if not why and deletes:
            why = delete_through(test, cache, rel)
        if why:
            problems.append(f"{cache.svc}: {why}")
    if problems:
        return FAIL, first(problems)
    if not test.writes:
        return PASS, f"{len(caches)} V2 cache(s) refused a PUT"
    if not deletes:
        return PASS, f"{len(caches)} V2 cache(s) relayed a PUT; {cannot_delete}"
    return PASS, f"{len(caches)} V2 cache(s) relayed a PUT and a DELETE"


RUN = {
    "put-declared": put_declared,
    "put-chunked": put_chunked,
    "put-client": put_client,
    "put-plugin": put_plugin,
    "origin-read": origin_read,
    "client-get": client_get,
    "plugin-get": plugin_get,
    "cache-whole-first": cache_whole_first,
    "cache-range-first": cache_range_first,
    "cache-overlap": cache_overlap,
    "cache-assemble": cache_assemble,
    "cache-abandoned": cache_abandoned,
    "digests": digests_scenario,
    "digest-overwrite": digest_overwrite,
    "past-end": past_end,
    "empty": empty,
    "mixed-version": mixed_version,
    "overwrite-cached": overwrite_cached,
    "overwrite-range-first": overwrite_range_first,
    "write-through": write_through,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a."""
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/blocks"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/blocks", tokens)
        if errors:
            warn(f"emptying data/blocks {when}: {first(errors)}")

    # Earlier runs' objects come out first.
    clean("before the run")
    test = Test(session)
    print(f"Block test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        for name in selected:
            if name in CACHE_SCENARIOS and not test.caches:
                results.add(name, SKIP, "this shape has no cache")
                continue
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        clean("after the run")
