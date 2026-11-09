"""Objects and byte ranges at the block boundaries of pstore and caches.

See docs/tests/block-boundaries.md. framework/testlib/blocks.py lists
the sizes and ranges, and the layout facts behind them.

Objects go under /protected-a/data/blocks/<run>/ in four sets, each
first read a different way, since that decides how a V2 cache fills:
`whole` (first read whole through each cache), `ranged` (first read in
ranges), `client` (first read by `pelican object get`), and `plugin`
(first read by stash_plugin). `ranged` is uploaded in chunks with no
length, as the clients upload; the rest, with a Content-Length. The
other scenarios put objects of their own, one per cache or origin, so
that each is cold where it is read. Where /protected-a takes no writes,
the objects are put straight in each store (stores.seed()), and the
upload scenarios skip. Where it takes no direct clients, the origins are
read only through the caches, and a direct read must be refused.

Requests, client runs, and the uploads of `put-declared` and
`put-chunked` present tokens of the session's profile. The scenarios'
own objects are put in place, and the stores and caches checked and
cleaned, with WLCG tokens.
"""

import base64
import binascii
import functools
import hashlib
import os
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from email.utils import formatdate

from testlib import (
    blocks,
    cacheapi,
    common,
    control,
    credentials,
    digests,
    director,
    metrics,
    stores,
    suite,
    transfers,
    web,
    webdav,
)
from testlib.common import first, outcome, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, json_object, json_objects, last_line
from testlib.suite import Row

SCENARIOS = {
    "put-declared": "upload every size with a Content-Length to every origin",
    "put-chunked": "upload every size in chunks, with no length, to every origin",
    "put-client": "`pelican object put` of every size",
    "put-plugin": "stash_plugin's upload of every size",
    "put-checksum": "`pelican object put --require-checksum` succeeds only where a digest is reported",
    "put-checksum-config": "the same, with Client.DefaultChecksumAlgo configured in place of the flags",
    "origin-read": "each origin returns every object whole and every range",
    "client-get": "`pelican object get` of every size: through a cache, direct, and from each cache",
    "plugin-get": "stash_plugin's download of every size: through a cache, direct, and from each cache",
    "cache-whole-first": (
        "each cache: a cold whole read, a warm one that fetches nothing, then every range"
    ),
    "director-read": "each director's redirect, followed by a plain HTTP client: every object and range",
    "cache-range-first": "each cache: a cold range first, then every range, then the whole",
    "cache-stored": "each XRootD cache's store on disk holds every object's bytes, block by block",
    "cache-overlap": "each cache: ranges that overlap what it holds, in turn and at once",
    "coalescing": "each federated cache: concurrent cold reads of an object fetch it from an origin once",
    "cache-assemble": "each cache: ranges that add up to an object leave it holding all",
    "cache-abandoned": "each cache: a cold read that its client gives up on spoils nothing",
    "cache-corrupted": "each V2 cache: a block corrupted or cut from its store is never served",
    "transfer-status": "each federated cache, asked, ends a GET with an X-Transfer-Status trailer, 200: OK",
    "transfer-status-failure": "each federated V2 cache reports a block it cannot repair; the clients fail",
    "cache-memory": (
        "each federated V2 cache with a memory cache serves an object from memory once its block"
        " on disk is corrupted"
    ),
    "origin-transfer-status": "each origin, asked, ends a GET with an X-Transfer-Status trailer, 200: OK",
    "digests": "every digest that a server reports (Want-Digest) is the object's",
    "digest-overwrite": "each origin's digests and ETag of an object follow its overwrites",
    "digest-defaults": "each origin's digests asked for by default, by two algorithms, and by q-values",
    "digest-relay": "each federated V2 cache relays the origins' digests to HEAD, GET, and the client",
    "digest-range-first": "each federated V2 cache relays digests of an object it first read in a range",
    "stale-origin-digest": "each federated V2 cache keeps and passes on nothing that fails its digests",
    "past-end": "a range that starts at or past an object's end: 416, not bytes",
    "empty": "a 0-byte object: 200 with no body; a range of it gets no bytes",
    "huge-ranges": "an object past 4 GiB: its length, and ranges across 2 and 4 GiB, everywhere",
    "conditional": "a GET with If-None-Match or If-Modified-Since: 304 only if the condition holds",
    "conditional-put": "a PUT with If-Match or If-None-Match: refused (412) unless its condition holds",
    "etag-agreement": "each origin gives a version one ETag, by every method, and honors it in conditions",
    "conditional-edges": "each origin: conditions on a missing object, invalid, or combined; racing PUTs",
    "mixed-version": "each cache: an object overwritten mid-way comes back as one version",
    "overwrite-cached": "each cache: an object it holds, overwritten, comes back as one version",
    "overwrite-range-first": "the same, for an object it first read through a range",
    "write-through": "each V2 cache relays a PUT and a DELETE; an XRootD cache refuses them",
    "cache-evicted": "each bounded cache evicts its oldest objects, and every one still comes back",
}

# The scenarios that read through each cache, and so have nothing to test
# without one.
CACHE_SCENARIOS = (
    "cache-whole-first",
    "cache-range-first",
    "cache-stored",
    "cache-overlap",
    "coalescing",
    "cache-assemble",
    "cache-abandoned",
    "cache-corrupted",
    "transfer-status",
    "transfer-status-failure",
    "cache-memory",
    "digest-relay",
    "digest-range-first",
    "stale-origin-digest",
    "mixed-version",
    "overwrite-cached",
    "overwrite-range-first",
    "write-through",
    "cache-evicted",
)

# How each set is uploaded: with a declared length, or in chunks.
SETS = {"whole": True, "ranged": False, "client": True, "plugin": True}

# pstore's write tiers (see blocks.tier), and the metric that counts the
# writes in each.
TIERS = ("inline", "buffered", "streamed")
PSTORE_WRITES = "pelican_pstore_writes_total"


def tier_counts(declared: bool, sets: int = 1) -> dict[str, int]:
    """Return how many objects pstore should write in each of TIERS.

    The objects are sets of one of each of blocks.SIZES, uploaded with a
    declared length or not (see blocks.tier()).
    """
    found = {t: 0 for t in TIERS}
    for size in blocks.SIZES:
        found[blocks.tier(size, declared)] += sets
    return found


# Why a scenario skips where /protected-a takes no writes, or no direct
# clients.
NO_WRITES = "/protected-a takes no writes"
NO_DIRECT = "/protected-a takes no direct clients"


class Test(suite.Test):
    """The objects of every set, and how the uploads went.

    Attributes:
        origins: The origins, each store once.
        caches: Every cache.
        federated_v2: The federated V2 caches.
        data: Each set's object of each size, by set and size.
        uploads: How each kind of upload went, by "declared" or "chunked".
        digests: The digests the origins' storage reports.
        no_digests: Why the storage does not report the rest, or None.
    """

    def __init__(self, session: Session):
        super().__init__(session, "blocks")
        self.origins = stores.unique(self.fed.origins)
        self.caches = self.fed.caches
        self.federated_v2 = [c for c in self.fed.federated_caches if c.kind == "v2"]
        self.data = {(s, size): os.urandom(size) for s in SETS for size in blocks.SIZES}
        self.uploads: dict[str, Row] = {}
        self.digests, self.no_digests = stores.digests(self.fed)
        # Minted now, since the reads that first present it may be
        # concurrent.
        self.session.token_file(self.namespace)

    def set_rel(self, set_name: str, size: int) -> str:
        """Return the rel of a set's object of size."""
        return self.rel(set_name, str(size))

    def admin_token(self, cache: common.Cache) -> str:
        """Return a V2 cache's admin token (cacheapi.admin_token())."""
        return cacheapi.admin_token(self.session, cache)

    def digestless(self) -> str | None:
        """Return why the origins report no digest, or None if they do."""
        return None if self.digests else self.no_digests or "the origins report no digest"

    def object_route(self, director_url: str, rel: str) -> str:
        """Return the URL of rel at the director's object route."""
        return f"{director_url}/api/v1.0/director/object{self.path(rel)}"

    def tiers(self, origin: common.Origin) -> dict[str, float] | None:
        """Return pstore's writes at origin, by tier.

        Returns:
            The writes, or None if /metrics has none, as before an
            origin's first write.
        """
        found = metrics.fetch(origin.web_url) or metrics.EMPTY
        if not found.has(PSTORE_WRITES):
            return None
        return {t: found.value(PSTORE_WRITES, tier=t) for t in TIERS}

    def upload(self, declared: bool) -> Row:
        """Upload each set uploaded this way to every origin, and check it.

        Upload the objects one at a time, and check that each store holds
        them; on a pstore origin, check the tiers they landed in, and the
        block files that each made on disk (blocks.pstore_files()). Where
        /protected-a takes no writes, put them straight in each store
        instead.

        Returns:
            The result and a note.
        """
        names = [s for s, d in SETS.items() if d == declared]
        # Each object: its set and size, as a message names it; its rel;
        # and its bytes.
        objects = [
            (f"{set_name}/{size}", self.set_rel(set_name, size), self.data[(set_name, size)])
            for set_name in names
            for size in blocks.SIZES
        ]
        if not self.writes:
            for origin in self.origins:
                for _, rel, data in objects:
                    error = stores.write_file(origin.store_of(self.namespace), rel, data)
                    if error:
                        return FAIL, error
            return PASS, f"put in place in {len(self.origins)} store(s)"
        notes: list[str] = []
        for origin in self.origins:
            for set_name in names:
                errors = stores.make_dirs([origin], self.namespace, self.rel(set_name), self.setup)
                if errors:
                    return FAIL, "; ".join(errors)
            before = (self.tiers(origin) or dict.fromkeys(TIERS, 0.0)) if origin.pstore else None
            files = blocks.pstore_block_files(origin) if origin.pstore else {}
            for what, rel, data in objects:
                error = stores.put(origin, self.namespace, rel, data, self.token, chunked=not declared)
                if error:
                    return FAIL, error
                if origin.pstore:
                    known, files = files, blocks.pstore_block_files(origin)
                    made = sorted(n for p, n in files.items() if p not in known)
                    want = sorted(blocks.pstore_files(len(data), declared))
                    if made != want:
                        return FAIL, f"{origin.svc}: {what} made block files of {made} bytes, not {want}"
            for what, rel, data in objects:
                try:
                    got = stores.held(origin, self.namespace, rel, self.setup)
                except stores.Unreadable as e:
                    return FAIL, str(e)
                if got != common.digest(data):
                    return FAIL, (
                        f"{origin.svc} does not hold {what} intact" if got else f"{origin.svc} lacks {what}"
                    )
            if origin.pstore:
                after = self.tiers(origin)
                if before is None or after is None:
                    return FAIL, f"{origin.svc}: no {PSTORE_WRITES} after its writes"
                want_tiers = tier_counts(declared, len(names))
                got_tiers = {t: int(after[t] - before[t]) for t in before}
                if got_tiers != want_tiers:
                    return FAIL, f"{origin.svc}: tiers {got_tiers}, not {want_tiers}"
                notes.append(f"{origin.svc}: tiers {got_tiers}")
        return PASS, "; ".join([f"{len(objects)} objects at {len(self.origins)} origin(s)"] + notes)

    def ensure(self, declared: bool) -> Row:
        """Upload the sets uploaded this way, once, and say how it went."""
        key = "declared" if declared else "chunked"
        if key not in self.uploads:
            self.uploads[key] = self.upload(declared)
        return self.uploads[key]

    def not_uploaded(self, *kinds: bool) -> str | None:
        """Return why an upload that a scenario needs failed, or None.

        Args:
            *kinds: The uploads that the scenario needs, each by whether it
                declares a length (see ensure()).
        """
        for declared in kinds:
            status, note = self.ensure(declared)
            if status != PASS:
                return f"the upload failed: {note}"
        return None

    @staticmethod
    def judge(answer: web.Response, data: bytes, request: blocks.Request | None) -> str | None:
        """Return what is wrong with answer to a GET of data, or None.

        Args:
            answer: The answer to a GET of an object that should hold
                data, or of a range of it.
            data: The object's bytes.
            request: The ranges asked for, or None for the whole object.
        """
        return web.served(answer, data) if request is None else blocks.check(data, request, answer)

    def get(self, server_url: str, rel: str, data: bytes, request: blocks.Request | None) -> str | None:
        """Read rel, or a range of it, from the server at server_url.

        Args:
            server_url: The server's URL.
            rel: The object, which should hold data.
            data: Its bytes.
            request: The ranges to ask for, or None for the whole object.

        Returns:
            What is wrong with the answer, or None.
        """
        headers = {"Range": blocks.range_header(request)} if request else {}
        return self.judge(self.ask("GET", server_url, rel, headers), data, request)

    def get_through(
        self, director_url: str, rel: str, data: bytes, request: blocks.Request | None
    ) -> str | None:
        """Read as get() does, but through the director at director_url.

        Read through its object route, as a plain HTTP client reads: the
        director's redirect (307) must lead to a federated cache, or, with
        no cache, to an origin, and the same request, token and Range and
        all, is then sent there, as curl -L sends it.
        """
        headers = {"Range": blocks.range_header(request)} if request else {}
        answer = web.request("GET", self.object_route(director_url, rel), token=self.token, headers=headers)
        why = director.misdirected(self.fed, answer)
        if why:
            return f"the director: {why}"
        where = answer.header("Location")
        answer = web.request("GET", where, token=self.token, headers=headers, timeout=120)
        why = self.judge(answer, data, request)
        return f"at {director.host(where)}: {why}" if why else None

    def read(
        self,
        server_url: str,
        set_name: str,
        size: int,
        request: blocks.Request | None,
        through: bool = False,
    ) -> str | None:
        """Read a set's object of size, or a range of it.

        Args:
            server_url: The server to read it from.
            set_name: The set.
            size: The object's size.
            request: The ranges to ask for, or None for the whole object.
            through: Whether the server is a director, to read through
                (get_through()).

        Returns:
            What went wrong, or None.
        """
        reader = self.get_through if through else self.get
        why = reader(server_url, self.set_rel(set_name, size), self.data[(set_name, size)], request)
        if not why:
            return None
        return f"{set_name}/{size}{' ' + blocks.range_header(request) if request else ''}: {why}"

    def read_whole(self, server_url: str, set_name: str, through: bool = False) -> list[str]:
        """Read every size in a set whole, one at a time.

        Returns:
            What went wrong with each read (see read()) that failed.
        """
        found = (self.read(server_url, set_name, size, None, through) for size in blocks.SIZES)
        return [p for p in found if p]

    def read_all(self, server_url: str, set_name: str, through: bool = False) -> list[str]:
        """Read every range of every size in a set, 8 at a time.

        Returns:
            What went wrong with each read (see read()) that failed.
        """
        jobs = [(size, r) for size in blocks.SIZES for r in blocks.requests(size)]

        def one(job: tuple[int, blocks.Request]) -> str | None:
            return self.read(server_url, set_name, *job, through=through)

        with common.pool(8) as pool:
            found = pool.map(one, jobs)
        return [p for p in found if p]


def put_under(test: Test, base: str, objects: Mapping[str, bytes]) -> str | None:
    """Put objects, by name, at <base>/<name> at every origin (put())."""
    return test.put({f"{base}/{name}": data for name, data in objects.items()})


def servers(test: Test) -> list[tuple[str, str]]:
    """Return each server to ask straight, by service and URL.

    They are the origins, if /protected-a takes direct clients, then
    every cache.
    """
    found = [(o.svc, o.url) for o in test.origins] if test.direct else []
    return found + [(c.svc, c.url) for c in test.caches]


def each_origin(test: Test, name: str, check: Callable[[common.Origin, str], tuple[list[str], str]]) -> Row:
    """Run check at each origin, of an object of its own.

    Args:
        test: The test.
        name: The objects' collection, in the run's.
        check: Returns what is wrong at an origin, and a note, given the
            origin and its object's rel.

    Returns:
        FAIL with the first problem, or PASS with each origin's note.
    """
    base = test.rel(name)
    if why := test.make(base):
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        wrong, note = check(origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc}: {note}")
    return outcome(problems, "; ".join(notes))


def at_once(jobs: list[Callable[[], str | None]]) -> list[str | None]:
    """Run jobs together, each started once all are ready.

    Returns:
        Each job's problem, or None, in the order of jobs.
    """
    barrier = threading.Barrier(len(jobs))

    def start(job: Callable[[], str | None]) -> str | None:
        try:
            barrier.wait(timeout=60)
        except threading.BrokenBarrierError:
            return "could not start with the others"
        return job()

    with common.pool(len(jobs)) as pool:
        return list(pool.map(start, jobs))


# --------------------------------------------------------------------------
# The scenarios.


def put_declared(test: Test) -> Row:
    """Upload every size with a Content-Length to every origin."""
    result = test.ensure(True)
    return result if test.writes else (SKIP, NO_WRITES)


def put_chunked(test: Test) -> Row:
    """Upload every size in chunks, with no length, to every origin."""
    result = test.ensure(False)
    return result if test.writes else (SKIP, NO_WRITES)


def local_sizes(test: Test, name: str) -> tuple[str, dict[int, bytes]]:
    """Make a local directory holding a new file of each size.

    Each file is named for its size.

    Returns:
        The directory's path, and each file's bytes, by size.
    """
    directory = test.directory(name)
    files = {size: os.urandom(size) for size in blocks.SIZES}
    for size, data in files.items():
        with open(os.path.join(directory, str(size)), "wb") as f:
            f.write(data)
    return directory, files


def pstore_tiers(test: Test) -> dict[str, dict[str, float]]:
    """Return each pstore origin's writes by tier, so far."""
    return {o.svc: test.tiers(o) or dict.fromkeys(TIERS, 0.0) for o in test.origins if o.pstore}


def holdings(test: Test, rel: str) -> list[str | None]:
    """Return the digest of what each store holds at rel (stores.held())."""
    return stores.held_anywhere(test.origins, test.namespace, rel, {test.namespace: test.setup})


def uploaded(test: Test, files: dict[int, bytes], collection: str) -> str | None:
    """Return why no origin holds one of files intact, or None.

    Args:
        test: The test.
        files: Each file's bytes, by size.
        collection: Where they should be, each at <collection>/<size>.
    """
    for size, data in files.items():
        if common.digest(data) not in holdings(test, f"{collection}/{size}"):
            return f"no origin holds {size} intact"
    return None


def landed(
    test: Test, files: dict[int, bytes], collection: str, before: dict[str, dict[str, float]]
) -> Row:
    """Check a client's upload of one file of each size.

    An origin must hold each intact (uploaded()); and the pstore origins'
    writes since before must be one of each size, in the tier its size
    predicts for an upload of no declared length, as the clients upload.

    Args:
        test: The test.
        files: Each file's bytes, by size.
        collection: Where they should be, each at <collection>/<size>.
        before: pstore_tiers() before the upload.

    Returns:
        The result and a note.
    """
    why = uploaded(test, files, collection)
    if why:
        return FAIL, why
    notes = [f"{len(files)} objects"]
    if before:
        got = {t: 0 for t in TIERS}
        for origin in test.origins:
            if not origin.pstore:
                continue
            was, now = before[origin.svc], test.tiers(origin)
            if now is None:
                return FAIL, f"{origin.svc}: no {PSTORE_WRITES} after its writes"
            for t in got:
                got[t] += int(now[t] - was[t])
        want = tier_counts(False)
        if got != want:
            return FAIL, f"tiers {got}, not {want} (the clients declare no length)"
        notes.append(f"tiers {got}")
    return PASS, "; ".join(notes)


def put_client(test: Test) -> Row:
    """Upload every size, into a collection, by `pelican object put`."""
    if not test.writes:
        return SKIP, NO_WRITES
    collection = test.rel("put-client")
    if why := test.make(collection):
        return FAIL, why
    directory, files = local_sizes(test, "put-client")
    sources = [os.path.join(directory, str(size)) for size in files]
    before = pstore_tiers(test)
    code, out, err = test.session.pelican_cmd(
        "object",
        "put",
        "--token",
        test.token_file,
        *sources,
        test.pelican_url(collection) + "/",
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    return landed(test, files, collection, before)


def plugin(
    test: Test,
    name: str,
    urls: list[str],
    directory: str,
    upload: bool,
    cache: str | None = None,
) -> tuple[bool, str]:
    """Run stash_plugin on urls, with the token's file.

    Args:
        test: The test.
        name: A name for the plugin's files (Session.plugin_run()).
        urls: The objects' URLs.
        directory: Where each object's local file is, named as its URL
            ends.
        upload: Whether to upload, rather than download.
        cache: The cache to use alone (Client.PreferredCaches), if any.

    Returns:
        Whether it says every transfer succeeded, and its result ads (or,
        where it wrote none, its output).
    """
    _, out, err, ads = test.session.plugin_run(
        f"{test.area}/{name}",
        [(url, transfers.local_name(url)) for url in urls],
        {"server.use": test.token_file},
        upload,
        cache,
        cwd=directory,
    )
    return transfers.plugin_passed(ads), ads or err or out


def put_plugin(test: Test) -> Row:
    """Upload every size by stash_plugin."""
    if not test.writes:
        return SKIP, NO_WRITES
    collection = test.rel("put-plugin")
    if why := test.make(collection):
        return FAIL, why
    directory, files = local_sizes(test, "put-plugin")
    urls = [test.pelican_url(f"{collection}/{size}") for size in files]
    before = pstore_tiers(test)
    passed, said = plugin(test, "put-plugin", urls, directory, upload=True)
    if not passed:
        return FAIL, f"failed: {last_line(said)}"
    return landed(test, files, collection, before)


def put_checksum(test: Test) -> Row:
    """Check `pelican object put --require-checksum` of every size.

    The client checks each upload against a digest that the origin
    reports after the PUT, by the first algorithm the origins' storage
    reports (--checksum-algorithm; crc32c otherwise). The command must
    succeed, leaving the objects intact, where the storage reports the
    object's digest (stores.has_digests(); the client declares no
    length). Elsewhere it must fail, rather than report an unverified
    upload as a success.
    """
    return checked_put(test, "put-checksum", configured=False)


def put_checksum_config(test: Test) -> Row:
    """Check `pelican object put` of every size, with its algorithm configured.

    Client.DefaultChecksumAlgo, given in the environment, names the
    algorithm that put_checksum() names with --checksum-algorithm (crc32c
    where the storage reports none), and, with neither flag given, must
    require a checksum as --require-checksum does.
    """
    return checked_put(test, "put-checksum-config", configured=True)


def checked_put(test: Test, name: str, configured: bool) -> Row:
    """Upload every size into collection name, requiring a checksum.

    With configured, the algorithm comes from Client.DefaultChecksumAlgo,
    and no flag asks for a checksum; otherwise --require-checksum does,
    and --checksum-algorithm names the algorithm, if the storage reports
    any.
    """
    if not test.writes:
        return SKIP, NO_WRITES
    collection = test.rel(name)
    if why := test.make(collection):
        return FAIL, why
    directory, files = local_sizes(test, name)
    verifiable = {
        size: data for size, data in files.items() if stores.has_digests(test.fed, size, declared=False)
    }
    algorithm = test.digests[0] if test.digests else "crc32c"
    if configured:
        flags: list[str] = []
        env = dict(test.session.env, PELICAN_CLIENT_DEFAULTCHECKSUMALGO=algorithm)
    else:
        flags = ["--require-checksum", *(["--checksum-algorithm", algorithm] if test.digests else [])]
        env = None
    notes: list[str] = []
    for sizes, verified in ((verifiable, True), (set(files) - set(verifiable), False)):
        if not sizes:
            continue
        code, out, err = test.session.pelican_cmd(
            "object",
            "put",
            *flags,
            "--token",
            test.token_file,
            *(os.path.join(directory, str(size)) for size in sorted(sizes)),
            test.pelican_url(collection) + "/",
            env=env,
        )
        if verified:
            if code != 0:
                return FAIL, f"exit {code}: {last_line(err or out)}"
            why = uploaded(test, verifiable, collection)
            if why:
                return FAIL, why
            notes.append(f"{len(sizes)} objects verified by {algorithm}")
            continue
        why = test.no_digests or f"the storage holds no digest of an object over {stores.S3_PART} bytes"
        if code == 0:
            return FAIL, f"succeeded, though {why}, so nothing was verified"
        if not re.search(r"checksum", out + err, re.I):
            return FAIL, f"failed, but not for want of a checksum: {last_line(err or out)}"
        notes.append(f"{len(sizes)} refused, since {why}")
    return PASS, "; ".join(notes)


def origin_read(test: Test) -> Row:
    """Check that each origin returns each object whole, and every range."""
    if not test.direct:
        return SKIP, NO_DIRECT
    if why := test.not_uploaded(True, False):
        return FAIL, why
    problems: list[str] = []
    for origin in test.origins:
        for set_name in ("whole", "ranged"):
            problems += test.read_whole(origin.url, set_name)
            problems += test.read_all(origin.url, set_name)
    return outcome(problems, f"{len(test.origins)} origin(s)")


def downloaded(test: Test, set_name: str, directory: str) -> str | None:
    """Return why directory does not hold just the set's objects, or None.

    It should hold the set's object of each size, named for its size, and
    nothing else.
    """
    names = {str(size) for size in blocks.SIZES}
    extra = sorted(set(os.listdir(directory)) - names)
    if extra:
        return f"left {', '.join(extra)}"
    for size in blocks.SIZES:
        got = common.sha256(os.path.join(directory, str(size)))
        if got != common.digest(test.data[(set_name, size)]):
            return f"{size} is {'missing' if got is None else 'different'}"
    return None


def named_caches(test: Test) -> list[common.Cache]:
    """Return the caches that a client's named reads name, each in turn.

    That is every federated one, so that each kind of cache the shape has
    (`-p cache-mixed`) serves a client that names it.
    """
    return list(test.fed.federated_caches)


def client_get(test: Test) -> Row:
    """Check `pelican object get` of every size, three ways.

    The ways are through the cache the director picks; with --direct,
    which must be refused where /protected-a takes no direct clients; and
    from each federated cache in turn, named (--cache), which alone may
    serve it.
    """
    if why := test.not_uploaded(True):
        return FAIL, why
    urls = [test.pelican_url(test.set_rel("client", size)) for size in blocks.SIZES]
    notes: list[str] = []
    routed = "through a cache" if test.caches else "without --direct"
    # Each way: its key, its label, the client's flags, and the cache it
    # names, if any.
    ways: list[tuple[str, str, list[str], common.Cache | None]] = [
        ("cache", routed, [], None),
        ("direct", "direct", ["--direct"], None),
    ]
    for named in named_caches(test):
        ways.append((f"named-{named.svc}", f"named ({named.svc})", ["--cache", named.url], named))
    for key, label, flags, cache in ways:
        target = test.directory(f"client-get-{key}")
        stats = os.path.join(test.tmp, f"client-get-{key}.stats.json")
        code, out, err = test.session.pelican_cmd(
            "object",
            "get",
            "--token",
            test.token_file,
            "--transfer-stats",
            stats,
            *flags,
            *urls,
            target,
        )
        if "--direct" in flags and not test.direct:
            if code == 0:
                return FAIL, f"{label}: succeeded, but {NO_DIRECT}"
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
        if cache is not None:
            why = director.served_elsewhere(transfers.stats_endpoints(transfers.read_stats(stats)), cache)
            if why:
                return FAIL, f"{label}: {why}"
        notes.append(label)
    return PASS, ", ".join(notes)


def plugin_get(test: Test) -> Row:
    """Check stash_plugin's download of every size, three ways.

    The ways are through the cache the director picks; direct
    (?directread, the plugin's only way to ask for it), which must be
    refused where /protected-a takes no direct clients; and from each
    federated cache in turn, preferred (Client.PreferredCaches), which
    alone may serve it.
    """
    if why := test.not_uploaded(True):
        return FAIL, why
    notes: list[str] = []
    routed = "through a cache" if test.caches else "without ?directread"
    # Each way: its key, its label, the query of each URL, and the cache
    # it prefers, if any.
    ways: list[tuple[str, str, str, common.Cache | None]] = [
        ("cache", routed, "", None),
        ("direct", "direct", transfers.DIRECT_QUERY, None),
    ]
    for named in named_caches(test):
        ways.append((f"named-{named.svc}", f"preferred ({named.svc})", "", named))
    for key, label, query, cache in ways:
        name = f"plugin-get-{key}"
        directory = test.directory(name)
        urls = [test.pelican_url(test.set_rel("plugin", size)) + query for size in blocks.SIZES]
        passed, said = plugin(test, name, urls, directory, upload=False, cache=cache.url if cache else None)
        if query and not test.direct:
            if passed:
                return FAIL, f"{label}: succeeded, but {NO_DIRECT}"
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
        if cache is not None:
            why = director.served_elsewhere(transfers.plugin_endpoints(said), cache)
            if why:
                return FAIL, f"{label}: {why}"
        notes.append(label)
    return PASS, ", ".join(notes)


def cache_whole_first(test: Test) -> Row:
    """Check each cache: a cold whole read, a warm one, then every range.

    Each cache reads every object of its set whole, cold, and then again,
    warm. The warm reads must come from what the cache stored, not the
    origins (warm_fetches()), as they would not if the cache could not
    read back a block it wrote, and fetched it again. A V2 cache must
    then keep each object as its size says (stored_files()).
    """
    if why := test.not_uploaded(True):
        return FAIL, why
    found = Findings()
    for cache in test.caches:
        for size in blocks.SIZES:
            why = test.read(cache.url, "whole", size, None)
            if why:
                found.fail(f"{cache.svc} (cold): {why}")
        before = settled_metrics(test) if test.fed.native_origins else None
        sent = control.origins_sent(test.fed)
        for size in blocks.SIZES:
            why = test.read(cache.url, "whole", size, None)
            if why:
                found.fail(f"{cache.svc} (warm): {why}")
        why = warm_fetches(test, before, sent_since(test, sent))
        if why:
            found.fail(f"{cache.svc}: for its warm reads, {why}")
        elif before is not None or sent is not None:
            found.note(f"{cache.svc}'s warm reads fetched nothing")
        if cache.kind == "v2":
            wrong = stored_files(test, cache, "whole")
            found.problems += [f"{cache.svc}: {p}" for p in wrong]
            if not wrong:
                found.note(f"{cache.svc} stored {len(blocks.SIZES)} objects as their sizes say")
        found.problems += [f"{cache.svc}: {p}" for p in test.read_all(cache.url, "whole")]
    if not test.fed.native_origins:
        found.note("XRootD serves the origins, which count no GETs")
    return found.row(f"{len(test.caches)} cache(s)")


def stored_files(test: Test, cache: common.Cache, set_name: str) -> list[str]:
    """Return what is wrong with how a V2 cache keeps a set it read whole.

    Each object under blocks.INLINE bytes must be inline in its database;
    each other, in a file of its own in its store on disk
    (cacheapi.object_file()), whose size, found on disk, must be its
    blocks and a tag for each (blocks.stored()).
    """
    token = test.admin_token(cache)
    problems: list[str] = []
    for size in blocks.SIZES:
        url = cacheapi.object_url(test.fed, test.path(test.set_rel(set_name, size)))
        path, inline, why = cacheapi.object_file(cache, token, url)
        if inline is None:
            problems.append(f"{size}: {why}")
        elif inline != (size < blocks.INLINE):
            problems.append(f"{size}: held {'inline' if inline else 'in a file'}, not as its size says")
        elif path is not None:
            try:
                on_disk = os.stat(path).st_size
            except OSError as e:
                problems.append(f"{size}: its file, framework/var/{path}: {e.strerror}")
                continue
            if on_disk != blocks.stored(size):
                problems.append(f"{size}: its file is {on_disk} bytes, not {blocks.stored(size)}")
    return problems


def settled_metrics(test: Test) -> list[metrics.Snapshot] | None:
    """Return the origins' metrics once their count of the tests' GETs holds still.

    An origin counts a request once its handler returns, which may be
    after the cache has handed its client the last byte. So they are
    fetched every second until the count is the same twice, for up to
    SETTLE_METRICS seconds; None if an origin's /metrics did not answer.
    """
    last = origin_metrics(test)
    deadline = time.monotonic() + SETTLE_METRICS
    while last is not None and time.monotonic() < deadline:
        time.sleep(1)
        now = origin_metrics(test)
        if now is None:
            return None
        if risen(last, now, GETS, OUR_GETS) == 0:
            return now
        last = now
    return last


def warm_fetches(test: Test, before: list[metrics.Snapshot] | None, sent: int | None) -> str | None:
    """Return what the origins served for reads that should need nothing.

    The origins' containers may send no more than WARM_SLACK bytes, by
    the kernel's count (sent; see control.origins_sent()), and native
    origins, by their metrics since before, may serve no GET of the
    tests' own (OUR_GETS). Each check is made where its count is known.

    Args:
        test: The test.
        before: The native origins' metrics before the reads, or None.
        sent: What their containers sent during the reads, or None.
    """
    problems: list[str] = []
    if sent is not None and sent > WARM_SLACK:
        problems.append(f"the origins' containers sent {sent} bytes")
    if before is not None:
        time.sleep(1)
        now = origin_metrics(test)
        if now is None:
            return "an origin's /metrics did not answer"
        served = sum(risen(before, now, GETS, {**OUR_GETS, "code": c}) for c in ("200", "206"))
        if served:
            problems.append(f"the origins served {served:g} GETs of the tests'")
    return "; ".join(problems) or None


def director_read(test: Test) -> Row:
    """Check that each director's object route returns every object.

    The route, followed as a plain HTTP client follows it (see
    Test.get_through()), must return every object of the set read whole
    first, whole and in every range, from the federated cache the redirect
    leads to, or, with no cache, the origin.
    """
    if not test.fed.directors:
        return SKIP, "a standalone origin has no director"
    if why := test.not_uploaded(True):
        return FAIL, why
    problems: list[str] = []
    for url in test.fed.directors:
        name = director.host(url)
        problems += [f"{name}: {p}" for p in test.read_whole(url, "whole", through=True)]
        problems += [f"{name}: {p}" for p in test.read_all(url, "whole", through=True)]
    where = "a cache" if test.fed.federated_caches else "an origin, there being no cache"
    return outcome(problems, f"{len(test.fed.directors)} director(s), each leading to {where}")


def cache_range_first(test: Test) -> Row:
    """Check each cache: a cold range, then every range, then the whole."""
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
        problems += [
            f"{cache.svc} (whole, after ranges): {p}" for p in test.read_whole(cache.url, "ranged")
        ]
    return outcome(problems, f"{len(test.caches)} cache(s)")


def cache_stored(test: Test) -> Row:
    """Check the bytes that each XRootD cache wrote to its store on disk.

    By now each cache has read every object of the `whole` and `ranged`
    sets whole (cache-whole-first, cache-range-first). An XRootD cache
    keeps an object in a plain file, so each block that it records as
    held must be the object's bytes there (blocks.xrootd_stored()), read
    from its store on disk rather than through the cache. A V2 cache
    encrypts its store with keys of its own, and is passed over. An
    object whose record is missing, as one the cache has evicted, is not
    compared; a cache of which none could be is inconclusive.
    """
    if why := test.not_uploaded(True, False):
        return FAIL, why
    xrootd = [c for c in test.caches if c.kind == "xrootd"]
    if not xrootd:
        return SKIP, "no XRootD cache here, and a V2 cache's store is encrypted"
    problems: list[str] = []
    doubts: list[str] = []
    notes: list[str] = []
    for cache in xrootd:
        compared = objects = 0
        missing: list[str] = []
        for set_name in ("whole", "ranged"):
            for size in blocks.SIZES:
                rel = test.set_rel(set_name, size)
                count, wrong, why = blocks.xrootd_stored(cache, rel, test.data[(set_name, size)])
                problems += [f"{cache.svc}: {set_name}/{size}: {w}" for w in wrong]
                if why:
                    missing.append(why)
                    continue
                compared += count
                objects += 1
        if not objects:
            doubts.append(f"{cache.svc}: no object's blocks could be read: {first(missing)}")
            continue
        notes.append(f"{cache.svc}: {compared} blocks of {objects} objects")
        if missing:
            notes.append(f"{cache.svc}: {len(missing)} not compared: {first(missing)}")
    if problems:
        return FAIL, first(problems)
    if doubts:
        return INCONCLUSIVE, "; ".join(doubts)
    passed_over = [c.svc for c in test.caches if c.kind != "xrootd"]
    if passed_over:
        notes.append(f"passed over V2 {', '.join(passed_over)}")
    return PASS, "; ".join(notes)


def cache_overlap(test: Test) -> Row:
    """Check ranges that overlap what each cache holds, in turn and at once.

    Read each plan of blocks.overlap_plans in turn, from an object of its
    own: every read after the first covers blocks that the cache fetched
    for an earlier one and blocks that it must fetch now. Then read
    blocks.concurrent_reads, all at once, of one more cold object. After
    each, the whole object must come back intact.
    """
    plans = blocks.overlap_plans()
    base = test.rel("overlap")
    objects = {
        f"{c.svc}-{plan}": os.urandom(blocks.OVERLAP_SIZE)
        for c in test.caches
        for plan in [*plans, "concurrent"]
    }
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    reads = blocks.concurrent_reads()
    for cache in test.caches:
        for plan, requests in plans.items():
            name = f"{cache.svc}-{plan}"
            for n, request in enumerate(requests, 1):
                why = test.get(cache.url, f"{base}/{name}", objects[name], request)
                if why:
                    problems.append(
                        f"{cache.svc}: {plan}, read {n} ({blocks.range_header(request)}): {why}"
                    )
            why = test.get(cache.url, f"{base}/{name}", objects[name], None)
            if why:
                problems.append(f"{cache.svc}: {plan}, the whole object afterward: {why}")
        name = f"{cache.svc}-concurrent"
        found = at_once(
            [functools.partial(test.get, cache.url, f"{base}/{name}", objects[name], r) for r in reads]
        )
        for read, why in zip(reads, found):
            if why:
                what = blocks.range_header(read) if read else "the whole object"
                problems.append(f"{cache.svc}: at once, {what}: {why}")
        why = test.get(cache.url, f"{base}/{name}", objects[name], None)
        if why:
            problems.append(f"{cache.svc}: the whole object after the reads at once: {why}")
    return outcome(
        problems, f"{len(test.caches)} cache(s): {len(plans)} plans, then {len(reads)} reads at once"
    )


def has_age(test: Test, cache: common.Cache, rel: str) -> bool | None:
    """Return whether a V2 cache says, with Age, that it holds all of rel.

    Unlike Test.held(), this tells a failed HEAD apart.

    Returns:
        Whether it does, or None if a HEAD of rel fails.
    """
    answer = web.request("HEAD", test.url(cache.url, rel), token=test.setup)
    return bool(answer.header("Age")) if answer.status == 200 else None


def wait_cinfo(
    cache: common.Cache, rel: str, want: Callable[[blocks.Cinfo], bool], seconds: float
) -> tuple[blocks.Cinfo | None, str]:
    """Run blocks.read_cinfo() until what it finds is wanted.

    The cache writes its .cinfo some time after the blocks; give up once
    seconds pass.

    Returns:
        What blocks.read_cinfo() returned last.
    """
    return common.wait_last(
        lambda: blocks.read_cinfo(cache, rel),
        lambda found: found[0] is not None and want(found[0]),
        seconds,
    )


class Findings(common.Findings):
    """What a scenario found, recorded as it goes."""

    def fail(self, why: str) -> None:
        """Record a problem."""
        self.problems.append(why)

    def doubt(self, why: str) -> None:
        """Record a doubt."""
        self.doubts.append(why)

    def note(self, what: str) -> None:
        """Record what went right."""
        self.notes.append(what)

    def verdict(self) -> Row:
        """Return FAIL, INCONCLUSIVE, or PASS, and a note.

        Unlike row(), the note names what went right, if inconclusive too.
        """
        if self.problems:
            return FAIL, first(self.problems)
        if self.doubts:
            return INCONCLUSIVE, "; ".join(self.doubts + self.notes)
        return PASS, "; ".join(self.notes)


def each_cache(
    test: Test,
    name: str,
    size: int,
    caches: list[common.Cache],
    check: Callable[[common.Cache, str, bytes, Findings], None],
) -> Row:
    """Run check at each of caches, of a new object of its own.

    Args:
        test: The test.
        name: The objects' collection, in the run's.
        size: Each object's size.
        caches: The caches.
        check: Records what it finds at a cache, given the cache, its
            object's rel, and the object's bytes.

    Returns:
        The findings' verdict, or FAIL if the objects could not be put.
    """
    base = test.rel(name)
    objects = {c.svc: os.urandom(size) for c in caches}
    if why := put_under(test, base, objects):
        return FAIL, why
    found = Findings()
    for cache in caches:
        check(cache, f"{base}/{cache.svc}", objects[cache.svc], found)
    return found.verdict()


def summary(test: Test, cache: common.Cache, rel: str) -> tuple[cacheapi.Blocks | None, str]:
    """Return which blocks of rel a V2 cache holds.

    Its admin asks its introspection (cacheapi.blocks_held()).

    Returns:
        The blocks it holds, or None; and why it did not say.
    """
    url = cacheapi.object_url(test.fed, test.path(rel))
    return cacheapi.blocks_held(cache, test.admin_token(cache), url)


def assembled_part(test: Test, cache: common.Cache, rel: str, found: Findings) -> bool:
    """Check a V2 cache's blocks after cache_assemble's first range.

    A V2 cache fetches a range's blocks rather than fill the object from
    its start (initObjectFromStat in local_cache/persistent_cache.go). Its
    introspection must show the range's blocks held, and none before them
    but block 0.

    Returns:
        Whether its introspection said: if not, the caller falls back to
        Age.
    """
    held, why = summary(test, cache, rel)
    if held is None:
        found.note(f"{cache.svc}: no block summary ({why}), so Age")
        return False
    start, end = blocks.ASSEMBLE[0]
    wanted = range(start // blocks.BLOCK, end // blocks.BLOCK + 1)
    lacking = [n for n in wanted if n in held.missing]
    # Block 0 it may hold: its stat of the origin reads the first byte
    # (Range: bytes=0-0), and it may store the block that holds it.
    before = [n for n in range(1, wanted[0]) if n not in held.missing]
    if lacking:
        found.fail(f"{cache.svc}: after the first range, it lacks its block {lacking[0]}")
    elif held.complete or before:
        found.fail(
            f"{cache.svc}: after a range from block {wanted[0]}, it holds block"
            + f" {before[0] if before else 'every one'} ({held.describe()}), as if it"
            + " filled the object from the start"
        )
    elif 0 not in held.missing:
        found.note(f"{cache.svc} holds block 0 too, after the first range")
    return True


def assemble(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """Run cache_assemble at one cache, of rel, which holds data."""
    ranges = [(r,) for r in blocks.ASSEMBLE]
    why = test.get(cache.url, rel, data, ranges[0])
    if why:
        found.fail(f"{cache.svc}: the first range: {why}")
        return
    # Whether an XRootD cache's .cinfo could be read after the first range,
    # and whether a V2 cache's introspection said which blocks it held.
    seen = introspected = False
    if cache.kind == "v2":
        introspected = assembled_part(test, cache, rel, found)
        partial = has_age(test, cache, rel)
        if partial is None:
            found.fail(f"{cache.svc}: a HEAD after the first range failed")
            return
        if partial and not introspected:
            found.doubt(f"{cache.svc}: Age after the first range, so it cannot show completion")
    else:
        cinfo, why = wait_cinfo(cache, rel, lambda c: 0 < c.count < c.blocks, 30)
        seen = cinfo is not None
        if cinfo is None:
            found.doubt(f"{cache.svc}: after the first range, {why}")
        elif not 0 < cinfo.count < cinfo.blocks:
            found.doubt(
                f"{cache.svc}: {cinfo.count} of {cinfo.blocks} blocks after the first range, not some"
            )
    for request in ranges[1:]:
        why = test.get(cache.url, rel, data, request)
        if why:
            found.fail(f"{cache.svc}: {blocks.range_header(request)}: {why}")
    if introspected:
        held = common.wait_last(
            lambda: summary(test, cache, rel)[0], lambda b: b is not None and b.complete, 10, 1
        )
        if held is not None and held.complete:
            found.note(f"{cache.svc}: {held.describe()}")
        else:
            said = held.describe() if held else "no block summary"
            found.fail(f"{cache.svc}: {said} after every range, not all")
    elif cache.kind == "v2":
        if test.wait_held(cache, rel, 10):
            found.note(f"{cache.svc}: Age")
        else:
            found.fail(f"{cache.svc}: no Age after every range, so it does not hold it all")
    else:
        cinfo, why = wait_cinfo(cache, rel, lambda c: c.complete, 60)
        if cinfo is None and seen:
            found.fail(f"{cache.svc}: after every range, {why}, though it was readable after the first")
        elif cinfo is None:
            found.doubt(f"{cache.svc}: after every range, {why}")
        elif not cinfo.complete:
            found.fail(f"{cache.svc}: {cinfo.count} of {cinfo.blocks} blocks after every range")
        else:
            found.note(f"{cache.svc}: {cinfo.blocks} of {cinfo.blocks} blocks")
    why = test.get(cache.url, rel, data, None)
    if why:
        found.fail(f"{cache.svc}: the whole object afterward: {why}")


def cache_assemble(test: Test) -> Row:
    """Check that ranges adding up to an object leave a cache holding all.

    Read blocks.ASSEMBLE in its order from a cold object: ranges that
    overlap, and add up to the object. The cache must hold part of it
    after the first, and all of it after the last: a V2 cache's
    introspection says which blocks it holds (assembled_part()), and an
    XRootD cache's .cinfo. Then the whole object must come back intact. A
    V2 cache whose introspection says nothing is judged by Age, which it
    sends only once it holds every block. Only a cache that cannot show
    the partial state after the first (with Age and no block summary,
    with no .cinfo, or with a .cinfo showing none or all of the blocks)
    leaves the result unsure; an XRootD cache whose .cinfo could be read
    then must still have one, showing every block, after the last.
    """
    return each_cache(
        test, "assemble", blocks.ASSEMBLE_SIZE, test.caches, functools.partial(assemble, test)
    )


# coalescing's object, and how many clients read it at once.
COALESCE_SIZE = 4 << 20
COALESCE_READERS = 16
# What a native origin counts of the GETs it serves
# (metrics/http_metrics.go): the bytes it sent, and the requests, by the
# username of the token each presented (the tests', for a cache's fetch on
# a client's behalf).
SENT_BYTES = "pelican_http_bytes_total"
SENT = {"server_type": "origin", "direction": "out", "method": "GET"}
GETS = "pelican_http_requests_total"
OUR_GETS = {"server_type": "origin", "method": "GET", "username": credentials.SUBJECT}
# How many GETs a V2 cache may make of the origins for the cold reads. It
# fetches a whole object in one, and may need a second. Besides these, the
# client it fetches through ranks the origins by a one-byte GET at each
# (sortAttempts in client/handle_http.go).
COALESCE_GETS = 2
# How many bytes the origins' containers may send while the reads are
# warm: the directors' health tests fetch a small object from each origin
# every 15 seconds (Director.OriginCacheHealthTestInterval), and the
# origins advertise, each over TLS.
WARM_SLACK = 128 << 10
# How long the origins' count of the tests' GETs may take to hold still
# after the reads that made them (settled_metrics()).
SETTLE_METRICS = 15


def origin_metrics(test: Test) -> list[metrics.Snapshot] | None:
    """Return every origin's metrics, or None if one did not answer."""
    found: list[metrics.Snapshot] = []
    for origin in test.fed.origins:
        snapshot = metrics.fetch(origin.web_url)
        if snapshot is None:
            return None
        found.append(snapshot)
    return found


def sent_since(test: Test, before: int | None) -> int | None:
    """Return how much more the origins' containers have sent than before.

    None if before is None (control.origins_sent()).
    """
    now = None if before is None else control.origins_sent(test.fed)
    return None if now is None or before is None else now - before


def risen(
    before: list[metrics.Snapshot],
    after: list[metrics.Snapshot],
    name: str,
    labels: dict[str, str],
) -> float:
    """Return how much metric name's series with labels rose at the origins.

    That is in all, from before to after.
    """
    return sum(metrics.delta(b, a, name, **labels) for b, a in zip(before, after))


def read_at_once(test: Test, cache: common.Cache, rel: str, data: bytes) -> str | None:
    """Read rel, holding data, whole through cache, COALESCE_READERS at once.

    Returns:
        The first thing wrong, or None.
    """
    found = at_once(
        [functools.partial(test.get, cache.url, rel, data, None) for _ in range(COALESCE_READERS)]
    )
    wrong = [why for why in found if why]
    return first(wrong) if wrong else None


def coalesced(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """Run coalescing at one cache, of rel, which holds data."""
    cold = origin_metrics(test) if test.fed.native_origins else None
    if test.fed.native_origins and cold is None:
        found.fail("an origin's /metrics did not answer")
        return
    sent = control.origins_sent(test.fed)
    why = read_at_once(test, cache, rel, data)
    if why:
        found.fail(f"{cache.svc}: a cold read: {why}")
        return
    sent = sent_since(test, sent)
    reads = f"for {COALESCE_READERS} cold reads at once"
    notes: list[str] = []
    if sent is not None:
        copies = sent / len(data)
        if sent < len(data):
            found.fail(
                f"{cache.svc}: {reads}, the origins' containers sent {sent} bytes, less than the object"
            )
        elif copies >= 2:
            found.fail(
                f"{cache.svc}: {reads}, the origins' containers sent {copies:.1f} objects' worth, not one"
            )
        notes.append(f"the origins' containers sent {copies:.2f} objects")
    warm = cold
    if cold is not None:
        warm = counted_gets(test, cache, cold, len(data), reads, found, notes)
    found.note(f"{cache.svc}: cold, {', '.join(notes)}")
    sent = control.origins_sent(test.fed)
    why = read_at_once(test, cache, rel, data)
    if why:
        found.fail(f"{cache.svc}: a warm read: {why}")
        return
    why = warm_fetches(test, warm, sent_since(test, sent))
    if why:
        found.fail(
            f"{cache.svc}: for {COALESCE_READERS} warm reads at once, though it held the object, {why}"
        )


def counted_gets(
    test: Test,
    cache: common.Cache,
    cold: list[metrics.Snapshot],
    size: int,
    reads: str,
    found: Findings,
    notes: list[str],
) -> list[metrics.Snapshot] | None:
    """Judge the cold reads by the native origins' own counts.

    They must have sent about one object (SENT_BYTES) for GETs, and a V2
    cache made no more than COALESCE_GETS of the tests' username (GETS),
    besides one at each origin.

    Args:
        test: The test.
        cache: The cache read through.
        cold: The origins' metrics before the reads.
        size: The object's size.
        reads: What the reads were, for a problem.
        found: Where the findings go.
        notes: Where the counts go.

    Returns:
        The origins' metrics once they counted the reads; None if they
        did not answer.
    """
    # An origin counts a request once its handler returns, which may be
    # just after the cache has handed the last reader its bytes.
    after = [cold]

    def counted() -> bool:
        now = origin_metrics(test)
        if now is not None:
            after[0] = now
        return risen(cold, after[0], SENT_BYTES, SENT) >= size

    common.wait_for(counted, 10, interval=1)
    sent = risen(cold, after[0], SENT_BYTES, SENT)
    gets = risen(cold, after[0], GETS, OUR_GETS)
    if sent < size:
        found.doubt(
            f"{cache.svc}: the origins counted {sent:.0f} bytes sent for GETs, less than the"
            + " object, so they did not count the cache's fetch"
        )
    elif sent / size >= 2:
        found.fail(
            f"{cache.svc}: {reads}, the origins counted {sent / size:.1f} objects' worth sent, not one"
        )
    elif cache.kind == "v2" and gets > COALESCE_GETS + len(test.fed.origins):
        found.fail(
            f"{cache.svc}: {reads}, the origins served {gets:g} GETs, more than"
            + f" {COALESCE_GETS} and one at each origin"
        )
    unnamed = " (none under the tests' username)" if not gets else ""
    notes.append(f"the origins counted {sent / size:.2f} objects in {gets:g} GETs{unnamed}")
    return after[0]


def coalescing(test: Test) -> Row:
    """Check that concurrent cold reads of an object fetch it only once.

    Each federated cache gets COALESCE_READERS cold whole reads of an
    object of its own, all at once. The origins' containers must send
    about one object, not one per reader, by the kernel's count
    (control.origins_sent()): a cache is there to spare the origins. Native
    origins must count as much (SENT_BYTES), and a V2 cache may make no
    more than COALESCE_GETS under the tests' username (GETS), besides one
    at each origin; an XRootD cache fetches block by block, so only its
    bytes are judged. Then as many warm reads must fetch nothing
    (warm_fetches()). A site-local cache reads through another cache, and
    is passed over.
    """
    caches = test.fed.federated_caches
    if not caches:
        return SKIP, "this shape has no federated cache"
    if not test.fed.native_origins and (test.fed.tiny or control.unavailable()):
        return SKIP, (
            f"XRootD serves the origins ({test.fed.origin_variant}), which count nothing,"
            + " and without a control helper, nor does the kernel"
        )
    return each_cache(test, "coalescing", COALESCE_SIZE, caches, functools.partial(coalesced, test))


# How much of an object cache-abandoned reads before it gives up: part of
# the first XRootD block, and more than a read batch of V2 blocks.
ABANDON_AFTER = 50000


def cache_abandoned(test: Test) -> Row:
    """Check that a cold read that its client gives up on spoils nothing.

    Each cache gets a cold whole read of an object of its own, which its
    client gives up on after ABANDON_AFTER bytes. Those bytes must be the
    object's; and afterward, its last block, then the whole object, must
    come back intact, however far the cache had fetched it. A V2 cache's
    introspection says how far that was (abandoned_blocks()).
    """
    base = test.rel("abandoned")
    objects = {c.svc: os.urandom(blocks.OVERLAP_SIZE) for c in test.caches}
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.caches:
        rel, data = f"{base}/{cache.svc}", objects[cache.svc]
        answer = web.read_part(test.url(cache.url, rel), test.token, ABANDON_AFTER)
        if answer.status != 200:
            problems.append(f"{cache.svc}: the read given up on: {answer.describe()}, not 200")
            continue
        if answer.body != data[:ABANDON_AFTER]:
            problems.append(
                f"{cache.svc}: the read given up on got {len(answer.body)} bytes,"
                + f" not the object's first {ABANDON_AFTER}"
            )
        if cache.kind == "v2":
            why = abandoned_blocks(test, cache, rel, notes)
            if why:
                problems.append(f"{cache.svc}: {why}")
        last = ((len(data) - blocks.BLOCK, len(data) - 1),)
        for request in (last, None):
            why = test.get(cache.url, rel, data, request)
            if why:
                what = blocks.range_header(request) if request else "the whole object"
                problems.append(f"{cache.svc}: {what} afterward: {why}")
    return outcome(problems, "; ".join([f"{len(test.caches)} cache(s)"] + notes))


def abandoned_blocks(test: Test, cache: common.Cache, rel: str, notes: list[str]) -> str | None:
    """Check what a V2 cache holds of rel once its client has given up.

    The cache may go on fetching the object, or not, so either is
    accepted, but a summary that contradicts itself is not.

    Args:
        test: The test.
        cache: The cache.
        rel: The object.
        notes: Gets what its introspection says it holds.

    Returns:
        What is wrong with the summary, or None.
    """
    held, why = summary(test, cache, rel)
    if held is None:
        notes.append(f"{cache.svc}: no block summary ({why})")
        return None
    notes.append(f"{cache.svc} then held {held.describe()}")
    if held.complete and (held.held < held.total or held.missing):
        return f"its block summary says it is complete, but holds {held.held} of {held.total}"
    if not held.complete and not held.missing:
        return f"its block summary says it is partial ({held.describe()}), but lacks no block"
    return None


# How long a cache may take to serve a spoiled object again, in seconds.
REPAIR_WAIT = 30


class NoChaos(Exception):
    """The cache has no chaos API."""


def spoil(test: Test, cache: common.Cache, rel: str, op: str, **params: str) -> None:
    """Have cache spoil its copy of rel, through its chaos API.

    Args:
        test: The test.
        cache: The V2 cache.
        rel: The object.
        op: As for cacheapi.chaos().
        **params: As for cacheapi.chaos().

    Raises:
        NoChaos: The cache has no chaos API.
        ValueError: The cache refused.
    """
    url = cacheapi.object_url(test.fed, test.path(rel))
    answer = cacheapi.chaos(cache, test.admin_token(cache), url, op, **params)
    if answer.status == 404:
        raise NoChaos(f"{cache.svc} has no chaos API ({answer.describe()})")
    if answer.status in (401, 403):
        raise ValueError(f"{cache.svc} refused the admin token: {answer.describe()}")
    if answer.status != 200:
        said = answer.body[:200].decode(errors="replace")
        raise ValueError(f"{cache.svc} did not {op}: {answer.describe()}: {said}")


def read_held(test: Test, cache: common.Cache, rel: str, data: bytes) -> str | None:
    """Read rel, which holds data, whole through a V2 cache, to hold it.

    The cache must then say that it holds it all within 10 seconds
    (Test.wait_held()).

    Returns:
        What went wrong, or None.
    """
    why = test.get(cache.url, rel, data, None)
    if why:
        return f"a whole read: {why}"
    if not test.wait_held(cache, rel, 10):
        return "no Age after a whole read, so it does not hold it all"
    return None


def never_spoiled(
    test: Test, cache: common.Cache, rel: str, data: bytes, request: blocks.Request
) -> str | None:
    """Read request of rel, which holds data, through cache, then the whole.

    Each read must be the object's bytes or fail, never other bytes.
    Within REPAIR_WAIT seconds the whole object must come back intact: a
    V2 cache fetches again a block that fails its authentication tag.

    Returns:
        What went wrong, or None.
    """
    first_byte, last_byte = blocks.resolve(len(data), request[0])
    for what, headers, want in (
        (
            blocks.range_header(request),
            {"Range": blocks.range_header(request)},
            data[first_byte : last_byte + 1],
        ),
        ("the whole object", {}, data),
    ):
        answer = test.ask("GET", cache.url, rel, headers)
        if answer.status in (200, 206) and answer.body != want and answer.body != data:
            return (
                f"{what}, once spoiled: {answer.describe()} with {len(answer.body)} bytes"
                + " that are not the object's"
            )

    def intact() -> bool:
        return web.served(test.ask("GET", cache.url, rel), data) is None

    if not common.wait_for(intact, REPAIR_WAIT, interval=2):
        return f"the whole object is not served intact within {REPAIR_WAIT}s of being spoiled"
    return None


def cache_corrupted(test: Test) -> Row:
    """Check that a block spoiled in a V2 cache's store is never served.

    Each V2 cache: once it holds an object whole, a block of it is
    corrupted in the cache's store, and once it holds another, bytes are
    cut from the end of its chunk file (see spoil()). A read of the
    spoiled part, and of the whole object, must then never be other bytes
    (see never_spoiled()). An XRootD cache has no such API, and is passed
    over.
    """
    caches = [c for c in test.caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no V2 cache, whose chaos API alone can spoil a block"
    base = test.rel("corrupted")
    size = blocks.OVERLAP_SIZE
    # Each spoiling: its op and parameters (see spoil()), and the range it
    # spoils.
    trials: list[tuple[str, dict[str, str], blocks.Request]] = [
        ("corrupt", {"block": "1", "bytes": str(blocks.TAG)}, ((blocks.BLOCK, blocks.BLOCK + 99),)),
        ("truncate", {"chunk": "-1", "drop-bytes": str(blocks.BLOCK)}, ((size - 100, size - 1),)),
    ]
    objects = {f"{c.svc}-{op}": os.urandom(size) for c in caches for op, _, _ in trials}
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    for cache in caches:
        for op, params, request in trials:
            rel, data = f"{base}/{cache.svc}-{op}", objects[f"{cache.svc}-{op}"]
            why = read_held(test, cache, rel, data)
            if why:
                problems.append(f"{cache.svc}: {op}, before spoiling: {why}")
                continue
            try:
                spoil(test, cache, rel, op, **params)
            except NoChaos as e:
                return SKIP, f"{e}; it needs Cache.EnableChaosAPI"
            except ValueError as e:
                problems.append(str(e))
                continue
            why = never_spoiled(test, cache, rel, data, request)
            if why:
                problems.append(f"{cache.svc}: {op}: {why}")
    return outcome(problems, f"{len(caches)} V2 cache(s): {', '.join(op for op, _, _ in trials)}")


# What a client sends to learn how a transfer ended once its headers have
# gone out (docs/pelican-http-headers.md in Pelican's repository). The
# server then announces `Trailer: X-Transfer-Status`, and ends the body
# with that trailer, `<status>: <text>`.
ASK_STATUS = {"X-Transfer-Status": "true", "TE": "trailers"}
STATUS_OK = "200: OK"
# transfer-status's objects: one of several V2 blocks, one under 4,096
# bytes, which a V2 cache keeps inline in its database, and one of 0 bytes.
# Each read is of an object of its own, so that it is cold the first time:
# the whole object; a range across the second and third blocks; the last
# 1,000 bytes; two ranges, in the first and third blocks, as
# multipart/byteranges; the small object; and the empty one.
STATUS_SIZE = 16384
STATUS_READS: dict[str, tuple[int, blocks.Request | None]] = {
    "whole": (STATUS_SIZE, None),
    "range": (STATUS_SIZE, ((blocks.BLOCK, 3 * blocks.BLOCK - 1),)),
    "suffix": (STATUS_SIZE, ((None, 1000),)),
    "multi": (STATUS_SIZE, ((0, 99), (2 * blocks.BLOCK, 2 * blocks.BLOCK + 99))),
    "small": (512, None),
    "empty": (0, None),
}
# The block that transfer-status-failure corrupts: past the bytes that
# http.ServeContent reads to sniff a type before it sends the headers, so
# that the failure comes after them.
FAILED_BLOCK = 3


def status_failed(said: str) -> bool:
    """Return whether X-Transfer-Status trailer said reports a failure.

    A failure is a status other than 2xx.
    """
    found = re.match(r"\s*(\d{3})\b", said)
    return found is not None and not found.group(1).startswith("2")


def read_status(
    test: Test,
    server_url: str,
    rel: str,
    data: bytes,
    request: blocks.Request | None,
    ask: bool = True,
) -> str | None:
    """GET rel, or a range of it, at the server at server_url.

    So asked (ASK_STATUS), the answer must announce the trailer and end
    with it, STATUS_OK; unasked, no trailer may come.

    Args:
        test: The test.
        server_url: The server's URL.
        rel: The object.
        data: Its bytes.
        request: The ranges to ask for, or None for the whole object.
        ask: Whether to ask for the transfer's status.

    Returns:
        What is wrong, or None.
    """
    headers = dict(ASK_STATUS) if ask else {}
    if request:
        headers["Range"] = blocks.range_header(request)
    answer = test.ask("GET", server_url, rel, headers)
    why = test.judge(answer, data, request)
    if why:
        return why
    if not ask:
        if answer.trailers or answer.header("Trailer"):
            return f"unasked, it sent trailers {list(answer.trailers)}"
        return None
    announced = [n.strip().lower() for n in answer.header("Trailer").split(",")]
    if "x-transfer-status" not in announced:
        return f"Trailer is '{answer.header('Trailer')}', not X-Transfer-Status"
    said = answer.trailer("X-Transfer-Status")
    if said != STATUS_OK:
        return f"X-Transfer-Status trailer '{said or 'none'}', not '{STATUS_OK}'"
    return None


def transfer_status(test: Test) -> Row:
    """Check that each federated cache, asked, ends a GET with its status.

    Each federated cache, V2 or XRootD, is asked for each of
    STATUS_READS, cold and then warm, with the transfer's status: each
    must announce the trailer and end with it, STATUS_OK (read_status()).
    Unasked, a GET of the whole object must come with no trailer. A HEAD
    so asked has no body, and so no trailer, but must answer 200, with
    the object's length if it gives one. A site-local cache reads
    through another cache, and is passed over. Each V2 cache's
    introspection must then say that it holds the small object inline,
    the path that read is for; if not, the row is inconclusive.
    """
    caches = test.fed.federated_caches
    if not caches:
        return SKIP, "this shape has no federated cache"
    base = test.rel("transfer-status")
    objects = {
        f"{c.svc}-{name}": os.urandom(size) for c in caches for name, (size, _) in STATUS_READS.items()
    }
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    for cache in caches:
        for when in ("cold", "warm"):
            for name, (_, request) in STATUS_READS.items():
                key = f"{cache.svc}-{name}"
                why = read_status(test, cache.url, f"{base}/{key}", objects[key], request)
                if why:
                    problems.append(f"{cache.svc}: {name} ({when}): {why}")
        key = f"{cache.svc}-whole"
        why = read_status(test, cache.url, f"{base}/{key}", objects[key], None, ask=False)
        if why:
            problems.append(f"{cache.svc}: a GET that did not ask: {why}")
        answer = web.request(
            "HEAD", test.url(cache.url, f"{base}/{key}"), token=test.token, headers=ASK_STATUS
        )
        length = answer.header("Content-Length")
        if answer.status != 200:
            problems.append(f"{cache.svc}: a HEAD that asked: {answer.describe()}, not 200")
        elif length and length != str(STATUS_SIZE):
            problems.append(f"{cache.svc}: a HEAD that asked: Content-Length {length}")
    if problems:
        return FAIL, first(problems)
    note = f"{len(caches)} cache(s): '{STATUS_OK}' for {len(STATUS_READS)} reads, cold and warm"
    doubts: list[str] = []
    inline_at: list[str] = []
    for cache in (c for c in caches if c.kind == "v2"):
        url = cacheapi.object_url(test.fed, test.path(f"{base}/{cache.svc}-small"))
        inline, why = cacheapi.held_inline(cache, test.admin_token(cache), url)
        if inline is None:
            note += f"; {cache.svc} did not say how it holds the small object ({why})"
        elif not inline:
            doubts.append(f"{cache.svc} holds the small object in blocks, not inline")
        else:
            inline_at.append(cache.svc)
    if inline_at:
        note += f"; the small object inline at {', '.join(inline_at)}"
    if doubts:
        return INCONCLUSIVE, f"{first(doubts)}, so its inline path went untested; {note}"
    return PASS, note


def origin_transfer_status(test: Test) -> Row:
    """Check that each origin, asked, ends a GET with its status.

    Pelican's docs/pelican-http-headers.md has origins send the trailer,
    as caches do. Each origin gets a GET of a whole object, asking for
    the transfer's status (read_status()). The origins are asked only if
    they take direct clients.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    rel = test.rel("origin-transfer-status", "object")
    data = os.urandom(STATUS_SIZE)
    if why := test.put({rel: data}):
        return FAIL, why
    problems: list[str] = []
    for origin in test.origins:
        why = read_status(test, origin.url, rel, data, None)
        if why:
            problems.append(f"{origin.svc}: {why}")
    return outcome(problems, f"{len(test.origins)} origin(s): '{STATUS_OK}'")


def get_from(test: Test, cache: common.Cache, rel: str, target: str, *flags: str) -> tuple[int, str, str]:
    """Run `pelican object get` of rel through cache alone (--cache).

    Args:
        test: The test.
        cache: The cache.
        rel: The object.
        target: The file to write.
        *flags: More of the client's flags.

    Returns:
        The client's exit status, and what it wrote to stdout and stderr.
    """
    return test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        cache.url,
        "--token",
        test.token_file,
        *flags,
        test.pelican_url(rel),
        target,
    )


def client_failed(test: Test, cache: common.Cache, rel: str, name: str) -> tuple[str | None, str]:
    """Check that `pelican object get` of rel through cache alone fails.

    It reads into a directory of its own, name, and must fail, by
    something other than a refusal, and leave nothing.

    Returns:
        What is wrong, or None; and how it failed.
    """
    directory = test.directory(name)
    code, out, err = get_from(test, cache, rel, os.path.join(directory, "object"))
    said = out + err
    if code == 0:
        return "`pelican object get` succeeded", ""
    if os.listdir(directory):
        return f"`pelican object get` failed, but left {sorted(os.listdir(directory))}", ""
    if transfers.refused(said):
        return f"`pelican object get` was refused: {last_line(said)}", ""
    found = re.search(r"Error code (\d{4})", said)
    return None, f"Error code {found.group(1)}" if found else f"exit {code}"


def plugin_failed(test: Test, cache: common.Cache, rel: str, name: str) -> tuple[str | None, str]:
    """Check that stash_plugin's download of rel through cache alone fails.

    It reads into a directory of its own, name, and must say that the
    transfer failed, and leave nothing. Its ad must say that the failure
    may be retried, since it came after the transfer began, unless the
    plugin found no such object (as where the director, asking every
    origin, found none).

    Returns:
        What is wrong, or None; and how it failed.
    """
    directory = test.directory(name)
    url = test.pelican_url(rel)
    passed, said = plugin(test, name, [url], directory, upload=False, cache=cache.url)
    if passed:
        return "stash_plugin succeeded", ""
    if "TransferSuccess = false" not in said:
        return f"stash_plugin wrote no ad of the failure: {last_line(said)}", ""
    if os.listdir(directory):
        return f"stash_plugin failed, but left {sorted(os.listdir(directory))}", ""
    kind = transfers.combine(transfers.classify(line) for line in transfers.failure_lines("plugin", said))
    if kind == "not found":
        return None, "not found"
    if not re.search(r"TransferRetryable\s*=\s*true", said):
        return "stash_plugin's ad does not say TransferRetryable = true", ""
    return None, "retryable"


def unrepairable(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """Run transfer_status_failure at one V2 cache, of rel, holding data.

    Raises:
        NoChaos: The cache has no chaos API.
    """
    why = read_held(test, cache, rel, data)
    if why:
        found.fail(f"{cache.svc}: before spoiling: {why}")
        return
    try:
        spoil(test, cache, rel, "corrupt", block=str(FAILED_BLOCK), bytes=str(blocks.TAG))
    except ValueError as e:
        found.fail(str(e))
        return
    for origin in test.origins:
        error = stores.delete(origin, test.namespace, rel, test.setup)
        if error:
            found.fail(error)
            return
    answer = test.ask("GET", cache.url, rel, ASK_STATUS)
    said = answer.trailer("X-Transfer-Status")
    intact = answer.body == data
    if answer.status == 200 and status_failed(said):
        how = f"200, then '{said[:60]}'"
    elif answer.status is not None and answer.status >= 400:
        # As where its copy has gone stale, and no origin has one now.
        how = f"{answer.describe()} before the body"
    elif answer.ok and intact and said == STATUS_OK:
        found.doubt(
            f"{cache.svc} served the object intact, '{said}', so it never read the corrupted"
            + " block (as from memory, under -p cache-memory)"
        )
        return
    else:
        body = "the object" if intact else f"{len(answer.body)} bytes that are not the object"
        found.fail(
            f"{cache.svc}: once a block could not be repaired, {answer.describe()} with {body},"
            + f" and trailer '{said or 'none'}', not a failure"
        )
        return
    why, client = client_failed(test, cache, rel, f"failure-get-{cache.svc}")
    if why:
        found.fail(f"{cache.svc}: {why}")
        return
    why, plugin_kind = plugin_failed(test, cache, rel, f"failure-plugin-{cache.svc}")
    if why:
        found.fail(f"{cache.svc}: {why}")
        return
    found.note(f"{cache.svc}: {how}; the client failed ({client}), the plugin too ({plugin_kind})")


def transfer_status_failure(test: Test) -> Row:
    """Check that a block a V2 cache cannot repair fails the transfer.

    Each federated V2 cache holds an object of its own whole. Its chaos
    API corrupts block FAILED_BLOCK in its store (see spoil()), and every
    origin's copy is removed, so that the cache cannot repair the block.
    A GET asking for the transfer's status must then end with a trailer
    that reports a failure, since the headers (200) went out before the
    cache read the block; or, as where the cache revalidates first, fail
    before the body. Through that cache alone, `pelican object get` must
    then fail, by something other than a refusal, and leave nothing; so
    must stash_plugin (plugin_failed()). A cache that serves the object
    intact never read the block, as where it holds it in memory, and
    leaves the row inconclusive.
    """
    if not test.federated_v2:
        return SKIP, "no federated V2 cache, whose chaos API alone can spoil a block"
    check = functools.partial(unrepairable, test)
    try:
        return each_cache(test, "transfer-failure", STATUS_SIZE, test.federated_v2, check)
    except NoChaos as e:
        return SKIP, f"{e}; it needs Cache.EnableChaosAPI"


# How many times cache-memory reads an object whole from a cache's store,
# so that its blocks, read that often, win a place in the memory cache
# over blocks read less (ristretto's TinyLFU admission); and how long it
# then waits for the cache's buffered writes to its memory cache.
MEMORY_READS = 4
MEMORY_SETTLE = 2


def cache_memory(test: Test) -> Row:
    """Check that a V2 cache with a memory cache serves blocks from it.

    Under `-p cache-memory` (Cache.MemoryCacheSize), each federated V2
    cache holds an object of its own whole, and reads it whole from its
    store MEMORY_READS times more, each read leaving the blocks it
    decrypts in memory (decryptBlocksFromFile in Pelican's
    local_cache/storage.go). Then, as in transfer_status_failure(), its
    chaos API corrupts block FAILED_BLOCK on disk, and every origin's
    copy is removed. A GET must then get the object intact, with the
    trailer STATUS_OK: the block came from memory. A failure to read the
    block shows only that the memory cache did not keep it, and leaves
    the row inconclusive; bytes other than the object's fail it.
    """
    if not test.fed.cache_memory:
        return SKIP, "the caches keep no blocks in memory ('-p cache-memory')"
    if not test.federated_v2:
        return SKIP, "no federated V2 cache, whose chaos API alone can spoil a block"
    check = functools.partial(from_memory, test)
    try:
        return each_cache(test, "memory", STATUS_SIZE, test.federated_v2, check)
    except NoChaos as e:
        return SKIP, f"{e}; it needs Cache.EnableChaosAPI"


def from_memory(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """Run cache_memory at one V2 cache, of rel, holding data.

    Raises:
        NoChaos: The cache has no chaos API.
    """
    why = read_held(test, cache, rel, data)
    for _ in range(MEMORY_READS):
        why = why or test.get(cache.url, rel, data, None)
    if why:
        found.fail(f"{cache.svc}: before spoiling: {why}")
        return
    time.sleep(MEMORY_SETTLE)
    try:
        spoil(test, cache, rel, "corrupt", block=str(FAILED_BLOCK), bytes=str(blocks.TAG))
    except ValueError as e:
        found.fail(str(e))
        return
    for origin in test.origins:
        error = stores.delete(origin, test.namespace, rel, test.setup)
        if error:
            found.fail(error)
            return
    answer = test.ask("GET", cache.url, rel, ASK_STATUS)
    said = answer.trailer("X-Transfer-Status")
    if answer.status == 200 and answer.body == data and said == STATUS_OK:
        return
    if answer.status == 200 and answer.body != data and not status_failed(said):
        found.fail(
            f"{cache.svc}: once a block on disk was corrupted, 200 with {len(answer.body)} bytes"
            + f" that are not the object, and trailer '{said or 'none'}'"
        )
        return
    found.doubt(
        f"{cache.svc} did not serve the object from memory ({answer.describe()}, trailer"
        + f" '{said or 'none'}'), so its memory cache did not keep the block"
    )


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


def send_head(test: Test, server_url: str, rel: str, want: str | None = None) -> web.Response:
    """Send a HEAD of rel to the server at server_url, with the token.

    Args:
        test: The test.
        server_url: The server's URL.
        rel: The object.
        want: The Want-Digest to send, if any.
    """
    headers = {"Want-Digest": want} if want else None
    return web.request("HEAD", test.url(server_url, rel), token=test.token, headers=headers)


def reported(
    test: Test, server_url: str, rel: str, data: bytes, algorithms: tuple[str, ...]
) -> tuple[list[str], set[str]]:
    """Ask a server for rel's digest by each of algorithms in turn.

    Each ask is a HEAD with Want-Digest.

    Args:
        test: The test.
        server_url: The server's URL.
        rel: The object.
        data: Its bytes.
        algorithms: The algorithms.

    Returns:
        What is wrong with what it reports, and the algorithms it reports
        rightly.
    """
    problems: list[str] = []
    right: set[str] = set()
    for algorithm in algorithms:
        answer = send_head(test, server_url, rel, algorithm)
        if answer.status != 200:
            problems.append(f"HEAD, for {algorithm}: {answer.describe()}")
            continue
        wrong, found = digests.check(answer.header("Digest"), data)
        problems += wrong
        right.update(found)
    return problems, right


def server_digests(
    test: Test, server_url: str, origin: bool, relays: bool = False
) -> tuple[list[str], set[str]]:
    """Check the digests that a server reports of the sets' objects.

    The objects are those of DIGEST_SIZES in the `whole` and `ranged`
    sets. The server is asked by HEAD for each of digests.ALGORITHMS in
    turn, and by GET for crc32c, the client's default. An origin must
    report each digest its storage holds of the object (test.digests,
    where stores.has_digests() says so for its size and upload). So must
    a server that relays the origins' (a V2 cache), of the `whole` set.

    Args:
        test: The test.
        server_url: The server's URL.
        origin: Whether the server is an origin.
        relays: Whether it relays the origins' digests.

    Returns:
        What is wrong, and the algorithms it reports rightly.
    """
    problems: list[str] = []
    given: set[str] = set()
    for set_name in ("whole", "ranged"):
        for size in DIGEST_SIZES:
            rel, data = test.set_rel(set_name, size), test.data[(set_name, size)]
            wrong, right = reported(test, server_url, rel, data, digests.ALGORITHMS)
            problems += [f"{set_name}/{size}: {w}" for w in wrong]
            given |= right
            lacking = sorted(set(test.digests) - right)
            must = (origin or (relays and set_name == "whole")) and stores.has_digests(
                test.fed, size, SETS[set_name]
            )
            if must and lacking:
                problems.append(f"{set_name}/{size}: reports no {', '.join(lacking)}")
            answer = test.ask("GET", server_url, rel, {"Want-Digest": "crc32c"})
            why = web.served(answer, data)
            if why:
                problems.append(f"{set_name}/{size}: GET: {why}")
                continue
            wrong, found = digests.check(answer.header("Digest"), data)
            problems += [f"{set_name}/{size}: GET: {w}" for w in wrong]
            given.update(found)
    return problems, given


def digests_scenario(test: Test) -> Row:
    """Check that every digest that a server reports is the object's.

    Every digest a server reports must be the object's, as the client
    checks it (server_digests()). Each origin must report
    stores.ORIGIN_DIGESTS of each object, but for those its storage
    cannot (stores.digests()). So must a federated V2 cache, of the
    `whole` set, since it relays the origins' (digest_relay()). The
    `ranged` set, which a V2 cache first read through a range, is
    digest_range_first's to judge; of it, as of any object at an XRootD
    or site-local cache, a cache need report none. The note names the
    servers that report none. The origins are asked only if they take
    direct clients.
    """
    if why := test.not_uploaded(True, False):
        return FAIL, why
    asked = [(o.svc, o.url, True, False) for o in test.origins] if test.direct else []
    asked += [(c.svc, c.url, False, c.kind == "v2" and not c.site_local) for c in test.caches]
    problems: list[str] = []
    silent: list[str] = []
    for svc, url, origin, relays in asked:
        wrong, given = server_digests(test, url, origin, relays)
        problems += [f"{svc}: {w}" for w in wrong]
        if not given:
            silent.append(svc)
    notes = [f"{len(asked)} server(s)"]
    if silent:
        notes.append(f"no digest from {', '.join(silent)}")
    if test.no_digests:
        notes.append(test.no_digests)
    return outcome(problems, "; ".join(notes))


def write_version(test: Test, origin: common.Origin, rel: str, how: str, data: bytes) -> str | None:
    """Put data at rel at origin, as how says.

    Args:
        test: The test.
        origin: The origin.
        rel: The object.
        how: Whether to `seed` it (see stores.seed()), `put` it, or write
            it in the store's `file` a second after the last write.
        data: The version to put.

    Returns:
        Why that failed, or None.
    """
    if how == "file":
        time.sleep(1.1)
        return stores.write_file(origin.store_of(test.namespace), rel, data)
    if how == "put":
        return stores.put(origin, test.namespace, rel, data, test.setup)
    return stores.seed(origin, test.namespace, rel, data, test.setup, test.caps)


def digests_after_writes(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], str]:
    """Write versions of rel at origin, checking each.

    Write them as digest_overwrite says, and after each, check that origin
    serves it, and reports each digest its storage can (test.digests) of
    it.

    Returns:
        What was wrong, and a note of how many versions there were.
    """
    steps = [("the first version", "seed")]
    if test.writes:
        steps.append(("a PUT at once", "put"))
    if not origin.pstore:
        steps.append(("a write in the store a second later", "file"))
    problems: list[str] = []
    etags: list[str] = []
    note = f"{len(steps)} versions"
    for what, how in steps:
        data = os.urandom(3 * blocks.BLOCK + 5)
        error = write_version(test, origin, rel, how, data)
        if error:
            return problems + [f"{what}: {error}"], note
        served = web.request("GET", test.url(origin.url, rel), token=test.token)
        if served.status != 200 or served.body != data:
            return problems + [f"after {what}, it serves another version"], note
        etag = served.header("ETag")
        if etag and etag in etags:
            problems.append(f"after {what}, its ETag is still an earlier version's, {etag}")
        etags.append(etag)
        wrong, right = reported(test, origin.url, rel, data, test.digests)
        problems += [f"after {what}: {w}" for w in wrong]
        if right != set(test.digests):
            problems.append(f"after {what}, it reports only {sorted(right)}")
    return problems, note


def digest_overwrite(test: Test) -> Row:
    """Check that each origin's digests and ETags follow overwrites.

    Each origin reports the digests of an object of its own. Another
    version is then PUT at once, and, in a store on disk, written there a
    second later, which the origin must notice: it may keep digests by
    the file's modification time, to the second. Each time, the digests
    it reports must be those of the version it serves. Its ETag, if it
    sends one, must differ from every earlier version's, since by it a
    cache tells that an object has changed.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    if why := test.digestless():
        return SKIP, why
    return each_origin(test, "digest-overwrite", functools.partial(digests_after_writes, test))


# The algorithms the client knows (ChecksumType in client/handle_http.go).
# Each is told by its digest, not by the number the client's stats give it.
CLIENT_ALGORITHMS = ("md5", "crc32c", "crc32", "sha")
# How long a V2 cache may take to record the origins' digests of an object
# it has read whole: it asks an origin for them once the body has come.
DIGEST_WAIT = 10


def relayed(answer: web.Response, data: bytes, want: tuple[str, ...]) -> str | None:
    """Return what is wrong with the digests in answer, or None.

    It must succeed; every digest it reports must be the object's; and it
    must report each of want.

    Args:
        answer: The answer to a HEAD or GET of an object.
        data: The object's bytes.
        want: The algorithms it must report.
    """
    if answer.status != 200:
        return f"{answer.describe()}, not 200"
    header = answer.header("Digest")
    wrong, right = digests.check(header, data)
    if wrong:
        return wrong[0]
    lacking = [a for a in want if a not in right]
    if lacking:
        return f"Digest '{header}' has no {', '.join(lacking)}"
    return None


def relay_problem(test: Test, server_url: str, rel: str, data: bytes, method: str) -> str | None:
    """Return what is wrong with the digests a server reports, or None.

    It is asked by Want-Digest for each of stores.ORIGIN_DIGESTS in turn:
    it must report those the origins' storage does (test.digests; see
    relayed()), and a GET must bring the object.

    Args:
        test: The test.
        server_url: The server's URL.
        rel: The object.
        data: Its bytes.
        method: HEAD or GET.
    """
    for algorithm in stores.ORIGIN_DIGESTS:
        answer = test.ask(method, server_url, rel, {"Want-Digest": algorithm})
        why = relayed(answer, data, test.digests)
        if not why and method == "GET" and answer.body != data:
            why = f"{len(answer.body)} bytes, not the object"
        if why:
            return f"{method}, for {algorithm}: {why}"
    return None


def relayed_soon(test: Test, server_url: str, rel: str, data: bytes) -> str | None:
    """Return relay_problem() for HEAD, asked again while it finds one.

    Give up after DIGEST_WAIT seconds.
    """
    return common.wait_last(
        lambda: relay_problem(test, server_url, rel, data, "HEAD"), lambda why: why is None, DIGEST_WAIT, 1
    )


def stats_checksums(path: str) -> tuple[list[bytes], str]:
    """Return the checksums a server gave, by the client's stats at path.

    Returns:
        The values that the client's --transfer-stats file says a server
        gave it (ChecksumInfo.Value), each told by its digest alone (see
        CLIENT_ALGORITHMS), and ""; or nothing, and why.
    """
    stats = transfers.read_stats(path)
    if stats is None:
        return [], "its --transfer-stats file is unreadable, or not JSON"
    values: list[bytes] = []
    for result in transfers.stats_results(stats):
        for info in common.objects(result.get("serverChecksums")):
            try:
                values.append(base64.b64decode(str(info.get("Value") or ""), validate=True))
            except binascii.Error:
                continue
    return values, ""


def client_digests(test: Test, cache: common.Cache, rel: str, data: bytes) -> str | None:
    """Check the digests that `pelican object get` gets from cache alone.

    The client reads rel, which holds data: it must bring the object, and
    its transfer statistics show the digests that it then asked the cache
    for, which must be the object's, and include each of test.digests.

    Returns:
        What is wrong, or None.
    """
    name = f"relay-get-{cache.svc}"
    target = os.path.join(test.directory(name), "object")
    stats = os.path.join(test.tmp, f"{name}.json")
    code, out, err = get_from(test, cache, rel, target, "--transfer-stats", stats)
    if code != 0:
        return f"`pelican object get`: exit {code}: {last_line(err or out)}"
    if not common.file_holds(target, data):
        return "`pelican object get` did not bring the object"
    got, why = stats_checksums(stats)
    if why:
        return why
    found: set[str] = set()
    for value in got:
        names = [a for a in CLIENT_ALGORITHMS if digests.compute(a, data) == value]
        if not names:
            return f"the client got a digest from it that is not the object's ({value.hex()})"
        found.update(names)
    lacking = [a for a in test.digests if a not in found]
    if lacking:
        return (
            f"the client got no {', '.join(lacking)} from it"
            + f" (its --transfer-stats lists {len(got)} digest(s))"
        )
    return None


def no_relay(test: Test) -> str | None:
    """Return why no cache here need relay the origins' digests, or None."""
    if not test.federated_v2:
        return "no federated V2 cache; an XRootD cache need relay no digest"
    return test.digestless()


def relayed_at(test: Test, cache: common.Cache, rel: str, data: bytes) -> tuple[str | None, str]:
    """Run digest_relay at one V2 cache, of rel, which holds data.

    Returns:
        What is wrong, or None; and a note.
    """
    why = relay_problem(test, cache.url, rel, data, "HEAD")
    if why:
        return f"cold, {why}", ""
    answer = test.ask("GET", cache.url, rel, {"Want-Digest": test.digests[0]})
    why = web.served(answer, data)
    if why:
        return f"a cold GET: {why}", ""
    wrong, _ = digests.check(answer.header("Digest"), data)
    if wrong:
        return f"a cold GET: {wrong[0]}", ""
    note = "a cold GET with a Digest" if answer.header("Digest") else "a cold GET without one"
    why = relayed_soon(test, cache.url, rel, data)
    if why:
        return f"warm, {why}", note
    why = relay_problem(test, cache.url, rel, data, "GET")
    if why:
        return f"warm, {why}", note
    return client_digests(test, cache, rel, data), note


def digest_relay(test: Test) -> Row:
    """Check that each federated V2 cache relays the origins' digests.

    A V2 cache relays the origins' digests as an RFC 3230 Digest, so that
    a client can check what it downloads through the cache. Of an object
    of each federated V2 cache's own, a cold HEAD, and once the cache has
    read the object whole, a HEAD and a GET, must each report the digests
    that the origins' storage holds (test.digests), each the object's,
    whatever Want-Digest asks for (relay_problem()). A cold GET may come
    before the cache knows them; the note says whether it carried them.
    Then `pelican object get --cache` must get them from the cache
    (client_digests()). A site-local cache reads through another cache,
    and is passed over.
    """
    if why := no_relay(test):
        return SKIP, why
    caches = test.federated_v2
    base = test.rel("digest-relay")
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for cache in caches:
        why, note = relayed_at(test, cache, f"{base}/{cache.svc}", objects[cache.svc])
        if why:
            problems.append(f"{cache.svc}: {why}")
        if note:
            notes.append(f"{cache.svc}: {note}")
    return outcome(problems, "; ".join([f"{', '.join(test.digests)} relayed"] + notes))


def digest_range_first(test: Test) -> Row:
    """Check a V2 cache's digests of an object it first read in a range.

    Each federated V2 cache first reads an object of its own through a
    range, of its second block. A HEAD must then report the origins'
    digests (relay_problem()), and again once the cache has read the
    object whole and holds it all (it sends Age).
    """
    if why := no_relay(test):
        return SKIP, why
    caches = test.federated_v2
    base = test.rel("digest-range-first")
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    if why := put_under(test, base, objects):
        return FAIL, why
    problems: list[str] = []
    for cache in caches:
        rel, data = f"{base}/{cache.svc}", objects[cache.svc]
        request = ((blocks.BLOCK, 2 * blocks.BLOCK - 1),)
        why = test.get(cache.url, rel, data, request)
        if why:
            problems.append(f"{cache.svc}: the range: {why}")
            continue
        why = relay_problem(test, cache.url, rel, data, "HEAD")
        if why:
            problems.append(f"{cache.svc}: after the range, {why}")
        why = read_held(test, cache, rel, data)
        if why:
            problems.append(f"{cache.svc}: {why}")
            continue
        why = relayed_soon(test, cache.url, rel, data)
        if why:
            problems.append(f"{cache.svc}: once it held it all, {why}")
    return outcome(problems, f"{len(caches)} V2 cache(s): {', '.join(test.digests)} relayed")


# The extended attributes in which a posixv2 origin keeps an object's
# digests, each with the file's modification time, to the second, in
# XRootD's format (origin_serve/checksum.go).
CHECKSUM_XATTRS = (
    "user.XrdCks.md5",
    "user.XrdCks.crc32c",
    "user.XrdCks.crc32",
    "user.XrdCks.sha1",
)
# What a V2 cache asks an origin for (client.KnownChecksumTypes), so that
# every digest the origin then reports is stale.
EVERY_DIGEST = "md5,crc32c,crc32,sha"


def store_path(test: Test, origin: common.Origin, rel: str) -> str:
    """Return the path of rel's file in origin's store on disk."""
    return f"{origin.store_of(test.namespace)}/{rel}"


def xattrs(path: str, names: Iterable[str]) -> dict[str, bytes | None] | None:
    """Return the extended attributes names of the file at path.

    Returns:
        Each one's value, by name, or None for one it lacks; or None if
        nothing here can read extended attributes (os.getxattr is
        Linux's alone).
    """
    getxattr = vars(os).get("getxattr")
    if getxattr is None:
        return None
    found: dict[str, bytes | None] = {}
    for name in names:
        try:
            found[name] = getxattr(path, name)
        except OSError:
            found[name] = None
    return found


def rewrite_keeping_time(test: Test, origin: common.Origin, rel: str, data: bytes) -> str | None:
    """Overwrite rel in origin's store with data, keeping its times.

    The file gets back its access and modification times, so that an
    origin that keeps digests by the latter cannot tell.

    Returns:
        Why that failed, or None.
    """
    store = origin.store_of(test.namespace)
    path = store_path(test, origin, rel)
    was = os.stat(path)
    error = stores.write_file(store, rel, data)
    if error:
        return error
    try:
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    except OSError as e:
        return f"restoring framework/var/{path}'s modification time: {e.strerror}"
    return None


def stale_trap(
    test: Test, origin: common.Origin, rel: str, a: bytes, b: bytes
) -> tuple[str | None, str | None]:
    """Make origin serve b at rel but report a's digests.

    origin holds a at rel. Have it record a's digests (a HEAD with
    Want-Digest of EVERY_DIGEST), then write b over a in its store, and
    give the file back its modification time.

    Returns:
        What went wrong, and why the trap could not be set (where the
        origin keeps no digest in the store, or noticed), each or None.
    """
    answer = send_head(test, origin.url, rel, EVERY_DIGEST)
    if answer.status != 200:
        return f"{origin.svc}: a HEAD with Want-Digest: {answer.describe()}", None
    path = store_path(test, origin, rel)
    kept = xattrs(path, CHECKSUM_XATTRS)
    if kept is None:
        return None, "nothing here can read extended attributes"
    lacking = [name for name, value in kept.items() if value is None]
    if lacking:
        return None, (
            f"{origin.svc} kept no {lacking[0]} on framework/var/{path}, as where its"
            + " filesystem has no extended attributes, so it computes digests afresh"
        )
    error = rewrite_keeping_time(test, origin, rel, b)
    if error:
        return error, None
    answer = send_head(test, origin.url, rel, EVERY_DIGEST)
    _, stale = digests.check(answer.header("Digest"), a)
    if not stale:
        return None, (
            f"{origin.svc} reports no digest of the overwritten object"
            + f" (Digest '{answer.header('Digest')}'), so none is stale"
        )
    return None, None


def stale_digest(test: Test, cache: common.Cache, rel: str, a: bytes, found: Findings) -> None:
    """Run stale_origin_digest at one V2 cache, of rel, which holds a."""
    b = bytes(x ^ 0xFF for x in a)
    for origin in test.origins:
        why, doubt = stale_trap(test, origin, rel, a, b)
        if why:
            found.fail(f"{cache.svc}: {why}")
            return
        if doubt:
            found.doubt(doubt)
            return
    answer = test.ask("GET", cache.url, rel, ASK_STATUS)
    said = answer.trailer("X-Transfer-Status")
    if not answer.ok:
        how = f"{answer.describe()} before the body"
    elif status_failed(said):
        how = f"200, then '{said[:60]}'"
    else:
        body = "the bytes the origins hold" if answer.body == b else f"{len(answer.body)} bytes"
        found.fail(
            f"{cache.svc}: {answer.describe()} with {body} and trailer '{said or 'none'}',"
            + " though they fail the digests the origins report"
        )
        return
    url = test.url(cache.url, rel)
    status = common.wait_last(
        lambda: web.request("GET", url, token=test.token, headers=web.ONLY_IF_CACHED).status,
        lambda got: got == 504,
        10,
        1,
    )
    if status != 504:
        got = "no response" if status is None else f"HTTP {status}"
        found.fail(f"{cache.svc}: then, only-if-cached got {got}, not 504: it kept the object")
        return
    why, client = client_failed(test, cache, rel, f"stale-get-{cache.svc}")
    if why:
        found.fail(f"{cache.svc}: {why}")
        return
    found.note(f"{cache.svc}: {how}; kept nothing; the client failed ({client})")


def stale_origin_digest(test: Test) -> Row:
    """Check that a V2 cache keeps and serves nothing that fails its digest.

    Each federated V2 cache reads an object of its own that the origins
    serve with digests of another version. It must neither pass the
    object off as good nor keep it. A posixv2 origin keeps an object's
    digests in its file's extended attributes, and reports them while
    the file's modification time, to the second, is unchanged. So each
    origin records the digests of one version; the test then writes
    another of the same size over it in the store, and restores its time
    (stale_trap()). A GET through the cache, asking for the transfer's
    status, must then report a failure in its trailer, or fail; within
    10 seconds, a GET with only-if-cached must get 504; and
    `pelican object get --cache` must fail, by something other than a
    refusal, and leave nothing. Where the store keeps no extended
    attributes, or the origin notices the overwrite, the row is
    inconclusive. The origins are asked first, directly, so it skips
    where they take no direct clients.
    """
    if test.fed.origin_variant != "posixv2":
        return SKIP, (
            "only a posixv2 origin keeps digests by modification time in its store,"
            + f" not {test.fed.origin_variant}"
        )
    if not test.federated_v2:
        return SKIP, "no federated V2 cache, which alone checks the origins' digests"
    if not test.direct:
        return SKIP, f"{NO_DIRECT}, so no origin can be asked first"
    check = functools.partial(stale_digest, test)
    return each_cache(test, "stale-digest", STATUS_SIZE, test.federated_v2, check)


# What digest-defaults asks for in one Want-Digest: two algorithms, and the
# same weighed by q-values (RFC 3230, section 4.3.1), preferring crc32c.
WANT_BOTH = "crc32c, md5"
WANT_WEIGHTED = "md5;q=0.5, crc32c;q=1"
# Where a posixv2 origin keeps each digest of stores.ORIGIN_DIGESTS (see
# CHECKSUM_XATTRS), as XRootD's XrdCksData lays it out: a 16-byte name,
# the file's modification time (8 bytes), a time (4), reserved (3), the
# value's length (1, at XRDCKS_LENGTH), and the value.
DIGEST_XATTRS = {"crc32c": "user.XrdCks.crc32c", "md5": "user.XrdCks.md5"}
XRDCKS_LENGTH = 31


def xattr_digests(path: str) -> dict[str, bytes | None] | None:
    """Return the digests that the file at path keeps in DIGEST_XATTRS.

    Returns:
        Each digest, by algorithm, or None for one it lacks; or None if
        nothing here can read extended attributes (see xattrs()).
    """
    kept = xattrs(path, DIGEST_XATTRS.values())
    if kept is None:
        return None
    found: dict[str, bytes | None] = {}
    for algorithm, name in DIGEST_XATTRS.items():
        raw = kept[name]
        if raw is None:
            found[algorithm] = None
            continue
        length = raw[XRDCKS_LENGTH] if len(raw) > XRDCKS_LENGTH else 0
        found[algorithm] = raw[XRDCKS_LENGTH + 1 : XRDCKS_LENGTH + 1 + length]
    return found


def digest_fields(answer: web.Response) -> list[str]:
    """Return the values of each Digest field of answer."""
    return [v for k, v in answer.headers if k.lower() == "digest"]


def defaults_at(test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings) -> None:
    """Send digest_defaults's HEADs of rel, which holds data, at origin.

    Each HEAD asks for digests in its own way: with no Want-Digest, with
    WANT_BOTH, and with WANT_WEIGHTED. Every digest it
    reports must be the object's, in one Digest field, which is all the
    Pelican client reads. Unasked, a native origin must report crc32c, the
    first of Origin.DefaultChecksumTypes, which the framework leaves at
    its default. Asked for both, a native origin must report each, and an
    XRootD origin, which reports the one it prefers, at least one. Asked
    with q-values, every origin must report at least one. Each only of
    what its storage reports (test.digests).
    """
    native = test.fed.native_origins
    asks = (
        ("unasked", None, ("crc32c",) if native else (), False),
        ("asked for both", WANT_BOTH, test.digests if native else (), not native),
        ("weighed", WANT_WEIGHTED, (), True),
    )
    said: list[str] = []
    for label, want, must, some in asks:
        answer = send_head(test, origin.url, rel, want)
        what = f"{origin.svc}: a HEAD with {'Want-Digest: ' + want if want else 'none asked'}"
        if answer.status != 200:
            found.fail(f"{what}: {answer.describe()}")
            continue
        fields = digest_fields(answer)
        if len(fields) > 1:
            found.fail(f"{what}: {len(fields)} Digest fields, of which the client reads one")
        wrong, right = digests.check(",".join(fields), data)
        for why in wrong:
            found.fail(f"{what}: {why}")
        lacking = [a for a in must if a in test.digests and a not in right]
        if lacking:
            found.fail(f"{what}: reports no {', '.join(lacking)}")
        elif some and not right:
            found.fail(f"{what}: reports no digest")
        said.append(f"{label} {'+'.join(right) or 'none'}")
    found.note(f"{origin.svc}: {', '.join(said)}")


def kept_digests(test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings) -> bool:
    """Check that a posixv2 origin keeps rel's digests in its xattrs.

    rel holds data. Once the origin has reported its digests, its file's
    extended attributes must hold each, the object's. Where they cannot
    be read, record a doubt; where one is wrong or missing, a problem.

    Returns:
        Whether it found any there to check.
    """
    path = store_path(test, origin, rel)
    kept = xattr_digests(path)
    if kept is None:
        found.doubt("nothing here can read extended attributes")
        return False
    if not any(kept.values()):
        found.doubt(
            f"{origin.svc} kept no digest on framework/var/{path}, as where its"
            + " filesystem has no extended attributes"
        )
        return False
    for algorithm in test.digests:
        value = kept.get(algorithm)
        if value is None:
            found.fail(f"{origin.svc}: framework/var/{path} has no {DIGEST_XATTRS[algorithm]}")
        elif value != digests.compute(algorithm, data):
            found.fail(
                f"{origin.svc}: framework/var/{path}'s {DIGEST_XATTRS[algorithm]}"
                + " is not the object's digest"
            )
    return True


def digests_rewritten(test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings) -> None:
    """Overwrite rel in a posixv2 origin's store, and check its digests.

    rel holds data, and the origin has recorded its digests. Write bytes
    of the same length over it in the store, and give
    the file back its modification time. An origin that keeps digests by
    that time, to the second, cannot see such a rewrite, so it may report
    the old version's digests, or the new bytes' if it noticed. Any other
    digest, or none, is a problem. The note says which.
    """
    rewrite = os.urandom(len(data))
    error = rewrite_keeping_time(test, origin, rel, rewrite)
    if error:
        found.fail(f"{origin.svc}: {error}")
        return
    answer = send_head(test, origin.url, rel, WANT_BOTH)
    what = f"{origin.svc}: after a rewrite in the store with its modification time kept"
    if answer.status != 200:
        found.fail(f"{what}, a HEAD: {answer.describe()}")
        return
    named = {a for a in digests.parse(answer.header("Digest")) if a in digests.ALGORITHMS}
    _, old = digests.check(answer.header("Digest"), data)
    _, new = digests.check(answer.header("Digest"), rewrite)
    if not named:
        found.fail(f"{what}, it reports no digest")
    elif set(old) == named:
        found.note(f"{origin.svc} kept the old digests after the rewrite")
    elif set(new) == named:
        found.note(f"{origin.svc} noticed the rewrite")
    else:
        found.fail(f"{what}, it reports digests of neither version ({answer.header('Digest')})")


def xattrs_at(test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings) -> None:
    """Check the extended attributes of rel at a posixv2 origin.

    For digest_defaults, rel holds data, and the origin has just reported
    its digests: its file's extended attributes
    must hold them (kept_digests()). A PUT of another version must clear
    them, or replace them with its digests, which the origin must then
    report. Last, digests_rewritten().
    """
    if not kept_digests(test, origin, rel, data, found):
        return
    if test.writes:
        new = os.urandom(len(data) + 7)
        error = stores.put(origin, test.namespace, rel, new, test.setup)
        if error:
            found.fail(f"{origin.svc}: {error}")
            return
        path = store_path(test, origin, rel)
        for algorithm, value in (xattr_digests(path) or {}).items():
            if value is not None and value != digests.compute(algorithm, new):
                found.fail(
                    f"{origin.svc}: after a PUT, framework/var/{path}'s"
                    + f" {DIGEST_XATTRS[algorithm]} is still the old version's"
                )
        data = new
        answer = send_head(test, origin.url, rel, WANT_BOTH)
        wrong, right = digests.check(answer.header("Digest"), data)
        if answer.status != 200 or wrong or set(right) != set(test.digests):
            found.fail(
                f"{origin.svc}: after a PUT, a HEAD: {answer.describe()},"
                + f" Digest '{answer.header('Digest')}', not the new version's"
            )
            return
    digests_rewritten(test, origin, rel, data, found)


def digest_defaults(test: Test) -> Row:
    """Check the digests each origin reports unasked, and asked in two ways.

    Each origin, of an object of its own: the digests it reports unasked,
    asked for two, and asked with q-values (defaults_at()). On a posixv2
    origin, it must then keep them in its file's extended attributes, and
    follow a PUT; and after a rewrite in the store that keeps the file's
    modification time, it may report the old digests, but no others
    (xattrs_at()). Where the store's extended attributes cannot be read,
    the row is inconclusive. The origins are asked only if they take
    direct clients.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    if why := test.digestless():
        return SKIP, why
    base = test.rel("digest-defaults")
    if why := test.make(base):
        return FAIL, why
    found = Findings()
    for origin in test.origins:
        rel, data = f"{base}/{origin.svc}", os.urandom(3 * blocks.BLOCK + 5)
        error = stores.seed(origin, test.namespace, rel, data, test.setup, test.caps)
        if error:
            found.fail(f"{origin.svc}: {error}")
            continue
        defaults_at(test, origin, rel, data, found)
        if test.fed.origin_variant == "posixv2":
            xattrs_at(test, origin, rel, data, found)
    if test.no_digests:
        found.note(test.no_digests)
    return found.verdict()


# The sizes that past-end asks about.
PAST_END_SIZES = (1, blocks.BLOCK, blocks.XROOTD_BLOCK + 1, blocks.SPILL + 1)


def past_end(test: Test) -> Row:
    """Check each server's answer to a range at or past an object's end.

    Each server, asked for ranges that start at or past the end of objects
    of PAST_END_SIZES in the `whole` and `ranged` sets
    (blocks.past_end()): 416, with Content-Range `bytes */<size>` if it
    says; or, if it ignores Range, as RFC 9110 lets it, 200 with the
    whole object. The origins are asked only if they take direct
    clients.
    """
    if why := test.not_uploaded(True, False):
        return FAIL, why
    asked = servers(test)
    problems: list[str] = []
    for svc, url in asked:
        for set_name in ("whole", "ranged"):
            for size in PAST_END_SIZES:
                data = test.data[(set_name, size)]
                target = test.url(url, test.set_rel(set_name, size))
                for request in blocks.past_end(size):
                    range_header = blocks.range_header(request)
                    answer = web.request("GET", target, token=test.token, headers={"Range": range_header})
                    what = f"{svc}: {set_name}/{size} {range_header}"
                    said = answer.header("Content-Range")
                    if answer.status == 200 and answer.body == data:
                        continue
                    if answer.status != 416:
                        problems.append(f"{what}: {answer.describe()}, not 416")
                    elif said and said.replace(" ", "") != f"bytes*/{size}":
                        problems.append(f"{what}: Content-Range '{said}', not 'bytes */{size}'")
    return outcome(problems, f"{len(asked)} server(s)")


def empty(test: Test) -> Row:
    """Check that every server answers an empty object's GET with no body.

    It must answer with 200, and a range of it with 200 and no body
    (RFC 9110 lets it ignore Range) or with 416, since no range of it is
    satisfiable. The origins are asked only if they take direct clients.
    """
    if why := test.not_uploaded(True, False):
        return FAIL, why
    notes: list[str] = []
    for svc, url in servers(test):
        for set_name in ("whole", "ranged"):
            target = test.url(url, test.set_rel(set_name, 0))
            answer = web.request("GET", target, token=test.token)
            if answer.status != 200 or answer.body:
                return FAIL, f"{svc}: {set_name}/0: {answer.describe()}, {len(answer.body)} bytes"
            answer = web.request("GET", target, token=test.token, headers={"Range": "bytes=0-0"})
            if answer.status not in (200, 416):
                return FAIL, f"{svc}: a range of {set_name}/0 got {answer.describe()}, not 200 or 416"
            # A 416's body explains it; a success's must be empty.
            if answer.status == 200 and answer.body:
                return FAIL, (
                    f"{svc}: a range of {set_name}/0 got {answer.describe()}"
                    f" with {len(answer.body)} bytes"
                )
        notes.append(f"{svc} ok")
    return PASS, ", ".join(notes)


# --------------------------------------------------------------------------
# huge-ranges: an object past 4 GiB.

# The object is zeros, but for MARKER bytes of a pattern of their own at
# its start, around 2 GiB and 4 GiB, and at its end (huge_pattern()); the
# rest is holes in a sparse file, which costs little to keep, and which
# a cache fetches no more of than a range reads.
MARKER = 1 << 16
# Its size: two markers past 4 GiB, rounded up to a block boundary, and a
# byte more, so that 2^32 falls inside a block (no power of two is a
# multiple of BLOCK), the last block is one byte, and the end marker
# clears the 4 GiB one: a size or an offset kept in 32 bits, signed or
# not, goes wrong somewhere in it.
HUGE_SIZE = -(-((1 << 32) + 2 * MARKER) // blocks.BLOCK) * blocks.BLOCK + 1
HUGE_MARKERS = (0, (1 << 31) - MARKER // 2, (1 << 32) - MARKER // 2, HUGE_SIZE - MARKER)
# The ranges asked for, each within a marker: across, just before, and
# at each power of two; the last byte; a suffix; and an open-ended tail.
HUGE_RANGES: tuple[blocks.Range, ...] = (
    ((1 << 31) - 100, (1 << 31) + 99),
    ((1 << 31) - 1, 1 << 31),
    (1 << 31, 1 << 31),
    ((1 << 32) - 100, (1 << 32) + 99),
    ((1 << 32) - 1, (1 << 32) - 1),
    (1 << 32, 1 << 32),
    (HUGE_SIZE - 1, HUGE_SIZE - 1),
    (None, 1000),
    (HUGE_SIZE - 1000, None),
)
# How much of the body of an answer to one of them is read before hanging
# up: more than any range, so that a server that ignores Range and sends
# the whole object is caught without the whole object being read.
HUGE_READ = 1 << 16
# How much of the object its file may take on disk and still be sparse.
SPARSE_LIMIT = 16 << 20
# How long a server may take to answer about it: a backend may hash the
# whole object for an ETag before it answers a HEAD.
HUGE_TIMEOUT = 300


def huge_pattern(run_id: str, offset: int) -> bytes:
    """Return the marker at offset.

    It is MARKER bytes from a digest of the run and the offset, so that no
    two markers, and no two runs', are alike.
    """
    seed = hashlib.sha256(f"huge:{run_id}:{offset}".encode()).digest()
    return (seed * (MARKER // len(seed) + 1))[:MARKER]


def huge_expected(run_id: str, low: int, high: int) -> bytes:
    """Return bytes low through high of the object."""
    found = bytearray(high - low + 1)
    for offset in HUGE_MARKERS:
        start, end = max(low, offset), min(high, offset + MARKER - 1)
        if start <= end:
            marker = huge_pattern(run_id, offset)
            found[start - low : end - low + 1] = marker[start - offset : end - offset + 1]
    return bytes(found)


def huge_file(run_id: str, path: str) -> tuple[str | None, bool]:
    """Write the object to path (under framework/var) as a sparse file.

    The markers are written, and the rest left as holes.

    Returns:
        Why that failed, or None if it did not; and whether the file is
        sparse there, which a store on a filesystem without holes cannot
        keep it.
    """
    try:
        with open(path, "wb") as f:
            for offset in HUGE_MARKERS:
                f.seek(offset)
                f.write(huge_pattern(run_id, offset))
            f.truncate(HUGE_SIZE)
        os.chmod(path, 0o644)
        found = os.stat(path)
    except OSError as e:
        return f"writing framework/var/{path}: {e.strerror}", False
    if found.st_size != HUGE_SIZE:
        return f"framework/var/{path} is {found.st_size} bytes, not {HUGE_SIZE}", False
    return None, found.st_blocks * 512 <= SPARSE_LIMIT


def huge_range_problem(run_id: str, answer: web.Response, r: blocks.Range) -> str | None:
    """Return what is wrong with answer to a request for range r, or None.

    It must be 206, with a Content-Range of the bytes asked for out of the
    object's size, and exactly those bytes.
    """
    low, high = blocks.resolve(HUGE_SIZE, r)
    if answer.status != 206:
        return f"{answer.describe()}, not 206"
    said = answer.header("Content-Range")
    if blocks.content_range(said) != (low, high, HUGE_SIZE):
        return f"Content-Range '{said}', not 'bytes {low}-{high}/{HUGE_SIZE}'"
    if answer.body != huge_expected(run_id, low, high):
        return f"{len(answer.body)} bytes came, not bytes {low}-{high}"
    return None


def huge_at(
    test: Test,
    what: str,
    head: Callable[[], web.Response],
    get: Callable[[str], web.Response],
    size_of: Callable[[], tuple[int | None, str]] | None = None,
    get_whole: Callable[[], web.Response] | None = None,
) -> list[str]:
    """Return what is wrong with how a server serves the object.

    The HEAD must give the object's length, each range must come back
    right, and the size must be the object's. A GET of the whole object,
    if sent, must give its length too, and begin with its first bytes.

    Args:
        test: The test.
        what: The server, as the problems name it.
        head: Sends a HEAD.
        get: Sends a GET of the range in a Range header, read only in
            part.
        size_of: Says what the server says the object's size is, and why
            not, if it says none.
        get_whole: Sends a GET with no Range, read only in part.
    """
    problems: list[str] = []
    answer = head()
    if answer.status != 200:
        problems.append(f"{what}: HEAD got {answer.describe()}")
    elif answer.header("Content-Length").strip() != str(HUGE_SIZE):
        length = answer.header("Content-Length")
        problems.append(f"{what}: HEAD gave Content-Length '{length}', not {HUGE_SIZE}")
    if get_whole is not None:
        answer = get_whole()
        if answer.status != 200:
            problems.append(f"{what}: GET got {answer.describe()}")
        elif answer.header("Content-Length").strip() != str(HUGE_SIZE):
            length = answer.header("Content-Length")
            problems.append(f"{what}: GET gave Content-Length '{length}', not {HUGE_SIZE}")
        elif answer.body != huge_expected(test.run, 0, len(answer.body) - 1) or not answer.body:
            problems.append(f"{what}: GET's body does not begin with the object's first bytes")
    for r in HUGE_RANGES:
        header = blocks.range_header((r,))
        why = huge_range_problem(test.run, get(header), r)
        if why:
            problems.append(f"{what}: {header}: {why}")
    if size_of is not None:
        size, why = size_of()
        if size != HUGE_SIZE:
            problems.append(f"{what}: {why or f'size {size}, not {HUGE_SIZE}'}")
    return problems


def huge_propfind(target: str, token: str) -> tuple[int | None, str]:
    """Return the object's size, as a PROPFIND of Depth 0 of target says.

    Returns:
        The size, or None; and what the PROPFIND said, or why it said no
        size.
    """
    answer = webdav.propfind(target, "0", token)
    if answer.status != 207:
        return None, f"PROPFIND got {answer.describe()}"
    try:
        entries = webdav.parse(answer.body)
    except ValueError as e:
        return None, f"PROPFIND: {e}"
    sizes = [e.size for e in entries if not e.collection]
    if len(sizes) != 1:
        return None, f"PROPFIND listed {len(entries)} entries"
    return sizes[0], f"PROPFIND said getcontentlength {sizes[0]}"


def huge_server(test: Test, svc: str, server_url: str, rel: str, whole: bool = False) -> list[str]:
    """Return huge_at() for a server, asked straight.

    With whole, it is also sent a GET of the whole object: only an origin,
    which reads its sparse file, since a cache would set about storing
    the object whole.
    """
    target = test.url(server_url, rel)
    return huge_at(
        test,
        svc,
        lambda: web.request("HEAD", target, token=test.token, timeout=HUGE_TIMEOUT),
        lambda header: web.read_part(
            target, test.token, HUGE_READ, timeout=HUGE_TIMEOUT, headers={"Range": header}
        ),
        lambda: huge_propfind(target, test.token),
        (lambda: web.read_part(target, test.token, HUGE_READ, timeout=HUGE_TIMEOUT)) if whole else None,
    )


def huge_through_director(test: Test, director_url: str, rel: str) -> list[str]:
    """Return huge_at() through the director's object route.

    The route sends a client to a cache: each request is sent there, and
    then again to wherever the redirect leads, with the token, as a client
    does.
    """
    route = test.object_route(director_url, rel)
    name = f"{director.host(director_url)} (object route)"

    def follow(method: str, headers: dict[str, str]) -> web.Response:
        answer = web.request(method, route, token=test.token, headers=headers)
        if answer.status != 307 or not answer.header("Location"):
            return answer
        where = answer.header("Location")
        if method == "HEAD":
            return web.request("HEAD", where, token=test.token, timeout=HUGE_TIMEOUT)
        return web.read_part(where, test.token, HUGE_READ, timeout=HUGE_TIMEOUT, headers=headers)

    return huge_at(
        test,
        name,
        lambda: follow("HEAD", {}),
        lambda header: follow("GET", {"Range": header}),
    )


def huge_client_sizes(test: Test, rel: str) -> list[str]:
    """Return what is wrong with the object's size as the client says it.

    The client says it by `pelican object stat --json`, and, where
    /protected-a has Listings, `pelican object ls -l --json` of its
    collection.
    """
    problems: list[str] = []
    url = test.pelican_url(rel)
    code, out, err = test.session.pelican_cmd("object", "stat", "--json", "--token", test.token_file, url)
    if code != 0:
        problems.append(f"`object stat`: exit {code}: {last_line(err or out)}")
    else:
        try:
            info = json_object(out)
        except ValueError as e:
            problems.append(f"`object stat`: unreadable output ({e}): {last_line(out)}")
        else:
            if info.get("Size") != HUGE_SIZE:
                problems.append(f"`object stat` said Size {info.get('Size')}, not {HUGE_SIZE}")
    if "Listings" not in test.caps:
        return problems
    code, out, err = test.session.pelican_cmd(
        "object", "ls", "-l", "--json", "--token", test.token_file, url.rsplit("/", 1)[0]
    )
    if code != 0:
        problems.append(f"`object ls`: exit {code}: {last_line(err or out)}")
        return problems
    try:
        sizes = [e.get("Size") for e in json_objects(out) if not e.get("IsCollection")]
    except ValueError as e:
        problems.append(f"`object ls`: unreadable output ({e}): {last_line(out)}")
        return problems
    if sizes != [HUGE_SIZE]:
        problems.append(f"`object ls` listed sizes {sizes}, not [{HUGE_SIZE}]")
    return problems


def huge_evict(test: Test, rel: str) -> list[str]:
    """Evict the object from each V2 cache, and return what went wrong.

    A V2 cache may have charged its store the whole of it.
    """
    problems: list[str] = []
    for cache in test.caches:
        if cache.kind != "v2":
            continue
        answer = cacheapi.evict(cache, test.path(rel), test.setup, method="DELETE")
        if answer.status != 200:
            problems.append(f"evicting it from {cache.svc}: {answer.describe()}")
    return problems


def huge_ranges(test: Test) -> Row:
    """Check the length and ranges of an object past 4 GiB at every server.

    The object is a sparse file in each origin's store on disk: at each
    origin that takes direct clients, through each director's object route
    (a cache), and at each cache, a HEAD must give its length, and each of
    HUGE_RANGES come back right, with its Content-Range; a PROPFIND at
    each origin and cache must say its size, as must `pelican object stat`
    and `ls`. A pstore origin's store is encrypted, so the object would
    have to go over HTTPS, whole: the scenario skips there. Afterward, the
    object is evicted from each V2 cache.
    """
    if any(o.pstore for o in test.origins):
        return (
            SKIP,
            "a pstore origin's store is encrypted: the object would go over HTTPS whole",
        )
    collection = test.rel("huge")
    rel = f"{collection}/{HUGE_SIZE}"
    if why := test.make(collection):
        return FAIL, why
    for origin in test.origins:
        path = store_path(test, origin, rel)
        why, sparse = huge_file(test.run, path)
        if why:
            return FAIL, why
        if not sparse:
            try:
                os.remove(path)
            except OSError:
                pass
            return SKIP, (
                f"{origin.svc}'s store keeps no sparse files, so the object would take"
                f" {HUGE_SIZE >> 30} GiB on disk"
            )
    problems: list[str] = []
    asked: list[str] = []
    try:
        if test.direct:
            for origin in test.origins:
                problems += huge_server(test, origin.svc, origin.url, rel, whole=True)
                asked.append(origin.svc)
        if test.fed.federated_caches:
            for url in test.fed.directors:
                problems += huge_through_director(test, url, rel)
                asked.append(f"{director.host(url)} (object route)")
        for cache in test.caches:
            problems += huge_server(test, cache.svc, cache.url, rel)
            asked.append(cache.svc)
        problems += huge_client_sizes(test, rel)
    finally:
        for why in huge_evict(test, rel):
            warn(why)
    return outcome(
        problems, f"{HUGE_SIZE} bytes, {len(HUGE_RANGES)} ranges, at {', '.join(asked)}, and the client"
    )


# The sizes whose conditional requests `conditional` sends: within a block,
# and past an XRootD block.
CONDITIONAL_SIZES = (blocks.BLOCK + 1, blocks.XROOTD_BLOCK + 1)
# A time before any object here was written.
LONG_AGO = "Thu, 01 Jan 2015 00:00:00 GMT"


def conditions(etag: str, modified: str) -> list[tuple[str, str, bool]]:
    """Return the conditions to send for an object with etag and modified.

    etag and modified are its ETag and Last-Modified (RFC 9110). Without
    an ETag or a Last-Modified, the conditions on it are left out.

    Returns:
        Each condition's header, its value, and whether it holds for the
        object as it is, in which case a server may answer 304.
    """
    found: list[tuple[str, str, bool]] = []
    if etag:
        found += [
            ("If-None-Match", etag, True),
            ("If-None-Match", f'"nope", {etag}', True),
            ("If-None-Match", f"W/{unweakened(etag)}", True),
            ("If-None-Match", "*", True),
            ("If-None-Match", '"nope"', False),
        ]
    if modified:
        tomorrow = formatdate(time.time() + 86400, usegmt=True)
        found += [
            ("If-Modified-Since", modified, True),
            ("If-Modified-Since", tomorrow, True),
            ("If-Modified-Since", LONG_AGO, False),
        ]
    return found


def conditional_problems(
    test: Test, svc: str, server_url: str, rel: str, data: bytes
) -> tuple[list[str], set[str], set[str]]:
    """Send each of conditions() for rel, which holds data, to a server.

    Returns:
        What is wrong, the headers it honored (304), and those of ETag and
        Last-Modified it never sent.
    """
    target = test.url(server_url, rel)
    answer = web.request("GET", target, token=test.token)
    why = web.served(answer, data)
    if why:
        return [f"{svc}: {rel}: {why}"], set(), set()
    etag, modified = answer.header("ETag"), answer.header("Last-Modified")
    unsent = {name for name, value in (("ETag", etag), ("Last-Modified", modified)) if not value}
    problems: list[str] = []
    honored: set[str] = set()
    for header, value, holds in conditions(etag, modified):
        reply = web.request("GET", target, token=test.token, headers={header: value})
        what = f"{svc}: {rel} with {header}: {value}"
        if reply.status == 304:
            if holds:
                honored.add(header)
            else:
                problems.append(f"{what}: 304, though the condition does not hold")
        elif reply.status != 200:
            problems.append(f"{what}: {reply.describe()}, not 200 or 304")
        elif reply.body != data:
            problems.append(f"{what}: the body is not the object")
    return problems, honored, unsent


def conditional(test: Test) -> Row:
    """Check that a server answers 304 only when a GET's condition holds.

    Each server, asked for objects of the `whole` set with a condition
    (RFC 9110, which lets a server ignore a condition on a GET, but never
    answer 304 to one that does not hold): with If-None-Match naming the
    ETag it sent, among others, weakened (W/), or as *, and with
    If-Modified-Since at the Last-Modified it sent or later, it may answer
    304, or 200 with the whole object; with If-None-Match naming another
    ETag, or If-Modified-Since long ago, it must answer 200 with the whole
    object. The origins are asked only if they take direct clients. The
    note says what each server honored, and what it never sent.
    """
    if why := test.not_uploaded(True):
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for svc, url in servers(test):
        honored: set[str] = set()
        unsent: set[str] = set()
        for size in CONDITIONAL_SIZES:
            wrong, right, missing = conditional_problems(
                test, svc, url, test.set_rel("whole", size), test.data[("whole", size)]
            )
            problems += wrong
            honored |= right
            unsent |= missing
        note = f"{svc} honors {', '.join(sorted(honored)) or 'none'}"
        if unsent:
            note += f", sends no {', '.join(sorted(unsent))}"
        notes.append(note)
    return outcome(problems, "; ".join(notes))


def cas_put(
    test: Test, origin: common.Origin, rel: str, data: bytes, header: str, value: str
) -> web.Response:
    """PUT data to rel at origin, on condition header: value."""
    return web.request(
        "PUT", test.url(origin.url, rel), token=test.token, upload=data, headers={header: value}
    )


def cas_problems(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], str]:
    """Run conditional_put at one origin, of rel.

    Returns:
        What is wrong, and a note.
    """
    v1, v2, v3 = (os.urandom(4096) for _ in range(3))
    problems: list[str] = []

    def check(what: str, answer: web.Response, allowed: bool, where: str, want: bytes | None) -> None:
        """Check that the write what succeeded if allowed, or else got 412.

        Either way the store must then hold want at where (None: nothing).
        """
        if allowed and not answer.ok:
            problems.append(f"{what}: {answer.describe()}")
        elif not allowed and answer.status != 412:
            problems.append(f"{what}: {answer.describe()}, not 412")
        have = stores.held(origin, test.namespace, where, test.setup)
        if want is None and have is not None:
            problems.append(f"after {what}, the store holds an object")
        elif want is not None and have != common.digest(want):
            problems.append(f"after {what}, the store does not hold what it should")

    check(
        "a PUT with If-None-Match: * of a new object",
        cas_put(test, origin, rel, v1, "If-None-Match", "*"),
        True,
        rel,
        v1,
    )
    if problems:
        return problems, ""
    check(
        "a PUT with If-None-Match: * over it",
        cas_put(test, origin, rel, v2, "If-None-Match", "*"),
        False,
        rel,
        v1,
    )
    current = v1
    etag = send_head(test, origin.url, rel).header("ETag")
    if etag:
        replaced = cas_put(test, origin, rel, v2, "If-Match", etag)
        check("a PUT with If-Match naming its ETag", replaced, True, rel, v2)
        if replaced.ok:
            current = v2
            check(
                "a PUT with If-Match naming the ETag it replaced",
                cas_put(test, origin, rel, v3, "If-Match", etag),
                False,
                rel,
                v2,
            )
    check(
        "a PUT with If-Match naming another ETag",
        cas_put(test, origin, rel, v3, "If-Match", '"nope"'),
        False,
        rel,
        current,
    )
    missing = f"{rel}-missing"
    check(
        "a PUT with If-Match: * of an object that does not exist",
        cas_put(test, origin, missing, v3, "If-Match", "*"),
        False,
        missing,
        None,
    )
    return problems, "with its ETag" if etag else "without an ETag, which it does not send"


def conditional_put(test: Test) -> Row:
    """Check that a PUT is refused (412) unless its condition holds.

    Each origin that takes writes from direct clients: a PUT with
    If-None-Match: * creates an object, and a second one is refused (412)
    and changes nothing; one with If-Match naming the object's ETag
    replaces it, and one naming the ETag it replaced, or another, is
    refused and changes nothing; and one with If-Match: * of an object
    that does not exist is refused, and stores nothing (RFC 9110). A
    server that ignores a condition on a PUT would let two writers
    overwrite each other unawares.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    if not test.writes:
        return SKIP, NO_WRITES
    return each_origin(test, "conditional-put", functools.partial(cas_problems, test))


# etag-agreement's object, and the range it asks for with If-Range.
ETAG_SIZE = 3 * blocks.BLOCK + 5
IF_RANGE = (100, blocks.BLOCK + 99)


def unweakened(etag: str) -> str:
    """Return etag without a weak prefix (W/).

    That is how a weak comparison sees it.
    """
    return etag[2:] if etag.startswith("W/") else etag


def method_etags(test: Test, target: str, data: bytes, problems: list[str]) -> dict[str, str]:
    """Return the ETag that each method reports of target, by method.

    The methods are GET, HEAD, and PROPFIND (Depth 0, its getetag).

    Args:
        test: The test.
        target: The object's URL.
        data: Its bytes.
        problems: Gets what goes wrong.

    Returns:
        Each method's ETag, or "" for one that reports none.
    """
    found: dict[str, str] = {}
    answer = web.request("GET", target, token=test.token)
    why = web.served(answer, data)
    if why:
        problems.append(f"a GET: {why}")
    found["GET"] = answer.header("ETag")
    answer = web.request("HEAD", target, token=test.token)
    if answer.status != 200:
        problems.append(f"a HEAD: {answer.describe()}")
    found["HEAD"] = answer.header("ETag")
    _, entry, why = webdav.stat(target, test.token)
    found["PROPFIND"] = ""
    if entry is None:
        problems.append(f"a PROPFIND: {why}")
    else:
        found["PROPFIND"] = entry.etag or ""
    return found


def etag_conditions(
    test: Test,
    origin: common.Origin,
    rel: str,
    data: bytes,
    etag: str,
    source: str,
    problems: list[str],
) -> tuple[bytes, str | None]:
    """Check conditions that name etag on rel at origin.

    Send a GET with If-Match, which must serve the object; a GET of
    IF_RANGE with If-Range, which must serve the range (206); and, where
    /protected-a takes writes, a PUT of another version with If-Match,
    which must replace it.

    Args:
        test: The test.
        origin: The origin.
        rel: The object.
        data: Its bytes.
        etag: Its ETag, a strong one.
        source: The method that reported etag.
        problems: Gets what goes wrong.

    Returns:
        What the object holds afterward, and the ETag of the PUT's answer,
        if it replaced it, or None.
    """
    target = test.url(origin.url, rel)
    what = f"with If-Match: {etag} ({source}'s)"
    answer = web.request("GET", target, token=test.token, headers={"If-Match": etag})
    if answer.status != 200 or answer.body != data:
        problems.append(f"a GET {what}: {answer.describe()}, not 200 with the object")
    start, end = IF_RANGE
    answer = web.request(
        "GET",
        target,
        token=test.token,
        headers={"Range": f"bytes={start}-{end}", "If-Range": etag},
    )
    if answer.status != 206 or answer.body != data[start : end + 1]:
        whole = ", the whole object" if answer.body == data else ""
        problems.append(
            f"a GET of bytes={start}-{end} with If-Range: {etag} ({source}'s):"
            + f" {answer.describe()}{whole}, not 206 with the range"
        )
    if not test.writes:
        return data, None
    new = os.urandom(ETAG_SIZE)
    answer = cas_put(test, origin, rel, new, "If-Match", etag)
    if not answer.ok:
        problems.append(f"a PUT {what}: {answer.describe()}")
    held = stores.held(origin, test.namespace, rel, test.setup)
    now = new if answer.ok else data
    if held != common.digest(now):
        problems.append(f"after a PUT {what}, the store does not hold what it should")
    return now, (answer.header("ETag") if answer.ok else None)


def etag_problems(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], str]:
    """Run etag_agreement at one origin, of rel.

    Returns:
        What is wrong, and a note.
    """
    target = test.url(origin.url, rel)
    data = os.urandom(ETAG_SIZE)
    problems: list[str] = []
    tags: dict[str, str] = {}
    if test.writes:
        answer = web.request("PUT", target, token=test.token, upload=data)
        if not answer.ok:
            return [f"a PUT: {answer.describe()}"], ""
        tags["PUT"] = answer.header("ETag")
    elif error := stores.seed(origin, test.namespace, rel, data, test.setup, test.caps):
        return [error], ""
    tags.update(method_etags(test, target, data, problems))
    sent = {method: tag for method, tag in tags.items() if tag}
    if len({unweakened(tag) for tag in sent.values()}) > 1:
        problems.append("one version, several ETags: " + ", ".join(f"{m} {t}" for m, t in sent.items()))
    # Each source's ETag of the version there when it is tried: GET's of
    # the object as written, then the latest PUT's. If-Match and If-Range
    # compare ETags strongly, so a weak one never holds, and is not tried.
    put_tag = tags.get("PUT", "")
    for source in ("GET", "PUT"):
        tag = tags["GET"] if source == "GET" else put_tag
        if tag and not tag.startswith("W/"):
            data, replaced = etag_conditions(test, origin, rel, data, tag, source, problems)
            put_tag = put_tag if replaced is None else replaced
    unsent = [method for method, tag in tags.items() if not tag]
    note = f"an ETag from {', '.join(sent) or 'none'}"
    if unsent:
        note += f", none from {', '.join(unsent)}"
    return problems, note


def etag_agreement(test: Test) -> Row:
    """Check that each origin gives one version one ETag, by every method.

    Each origin, of an object of its own, written by a PUT (or, where
    /protected-a takes no writes, put in its store): the ETags that the
    PUT's answer, a GET, a HEAD, and PROPFIND's getetag report must be
    one, since they name one version (RFC 9110, section 8.8.3), by which a
    cache revalidates and a writer compares and swaps. Then the ETags of
    GET and of PUT must each hold as conditions (etag_conditions()). The
    origins are asked only if they take direct clients. The note says
    which methods reported none.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    return each_origin(test, "etag-agreement", functools.partial(etag_problems, test))


# How many PUTs conditional-edges races to create one object.
RACERS = 8
# How a racing PUT that lost may be refused: 412 if its condition was
# evaluated once the winner's object existed; otherwise, since its
# condition held, as conflicting with the write in progress (409, or 423
# from a native origin's WebDAV locks, which serialize same-path PUTs).
RACE_REFUSALS = (409, 412, 423)


def race(test: Test, origin: common.Origin, rel: str) -> list[str]:
    """Race RACERS PUTs that create rel at origin.

    Each has If-None-Match: * and bytes of its own, and rel does not
    exist: exactly one must succeed, and the rest be refused
    (RACE_REFUSALS); and the store must then hold the winner's bytes.
    RFC 9110, section 13.1.2, requires 412 only of a condition that is
    false when evaluated, which it is not for a PUT that arrives while the
    winner's is still in progress.

    Returns:
        What is wrong.
    """
    bodies = [os.urandom(1 << 16) for _ in range(RACERS)]
    answers = [web.Response(None) for _ in range(RACERS)]

    def racer(n: int) -> Callable[[], str | None]:
        def put() -> str | None:
            answers[n] = cas_put(test, origin, rel, bodies[n], "If-None-Match", "*")
            return None if answers[n].status else "no response"

        return put

    lost = [p for p in at_once([racer(n) for n in range(RACERS)]) if p]
    if lost:
        return [f"a racing PUT: {lost[0]}"]
    problems: list[str] = []
    won = [n for n, answer in enumerate(answers) if answer.ok]
    if len(won) != 1:
        problems.append(
            f"{len(won)} of {RACERS} PUTs at once with If-None-Match: * of a new object"
            + " succeeded, not 1"
        )
    other = [a for a in answers if not a.ok and a.status not in RACE_REFUSALS]
    if other:
        problems.append(f"a racing PUT that lost: {other[0].describe()}, not 409, 412, or 423")
    held = stores.held(origin, test.namespace, rel, test.setup)
    if not any(held == common.digest(bodies[n]) for n in won):
        problems.append("after the racing PUTs, the store holds no winner's bytes")
    return problems


def edge_problems(test: Test, origin: common.Origin, base: str) -> tuple[list[str], str]:
    """Run conditional_edges at one origin, of objects under base.

    Returns:
        What is wrong, and a note.
    """
    rel = f"{base}/object"
    data = os.urandom(blocks.BLOCK + 1)
    error = stores.seed(origin, test.namespace, rel, data, test.setup, test.caps)
    if error:
        return [error], ""
    target = test.url(origin.url, rel)
    problems: list[str] = []
    for value in ('"nope"', "*"):
        missing = test.url(origin.url, f"{base}/missing")
        answer = web.request("GET", missing, token=test.token, headers={"If-None-Match": value})
        if answer.status != 404:
            problems.append(
                f"a GET of an object that does not exist, with If-None-Match: {value}:"
                + f" {answer.describe()}, not 404"
            )
    tomorrow = formatdate(time.time() + 86400, usegmt=True)
    for what, headers, status in (
        ("an invalid If-Modified-Since", {"If-Modified-Since": "not a date"}, 200),
        (
            "If-None-Match that does not hold, beside If-Modified-Since that does",
            {"If-None-Match": '"nope"', "If-Modified-Since": tomorrow},
            200,
        ),
        ("If-Unmodified-Since after Last-Modified", {"If-Unmodified-Since": tomorrow}, 200),
        ("If-Unmodified-Since before Last-Modified", {"If-Unmodified-Since": LONG_AGO}, 412),
    ):
        answer = web.request("GET", target, token=test.token, headers=headers)
        if status == 412 and answer.status != 412:
            problems.append(f"a GET with {what}: {answer.describe()}, not 412")
        elif status == 200 and (answer.status != 200 or answer.body != data):
            problems.append(f"a GET with {what}: {answer.describe()}, not 200 with the object")
    if not test.writes:
        return problems, "GETs only, since /protected-a takes no writes"
    junk = '*, "junk"'
    answer = cas_put(test, origin, rel, os.urandom(4096), "If-None-Match", junk)
    if answer.status != 412:
        problems.append(f"a PUT with If-None-Match: {junk} over it: {answer.describe()}, not 412")
    if stores.held(origin, test.namespace, rel, test.setup) != common.digest(data):
        problems.append(f"after a PUT with If-None-Match: {junk}, the store holds another object")
    problems += race(test, origin, f"{base}/raced")
    return problems, "GETs and PUTs"


def conditional_edges(test: Test) -> Row:
    """Check each origin's answers to edge cases of conditions.

    Each origin, of objects of its own (RFC 9110, section 13): a GET of an
    object that does not exist, with If-None-Match naming an ETag or *,
    must get 404, not 304; one with an invalid If-Modified-Since, which is
    ignored, the object; one with If-None-Match that does not hold, beside
    If-Modified-Since that would, the object, since If-None-Match decides;
    and one with If-Unmodified-Since, the object if it is after the
    object's Last-Modified, and otherwise 412. Where /protected-a takes
    writes, a PUT with If-None-Match: *, "junk" over the object must be
    refused (412), changing nothing, since * in a list still means any
    object; and of RACERS PUTs at once of a new object, each with
    If-None-Match: *, exactly one may succeed (race()). The origins are
    asked only if they take direct clients.
    """
    if not test.direct:
        return SKIP, NO_DIRECT
    base = test.rel("conditional-edges")
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        errors = stores.make_dirs([origin], test.namespace, f"{base}/{origin.svc}", test.setup)
        if errors:
            return FAIL, errors[0]
        wrong, note = edge_problems(test, origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc}: {note}")
    return outcome(problems, "; ".join(notes))


# mixed-version's object: three XRootD blocks, so that each kind of cache
# fetches only part of it for the first read. The later range is one
# BLOCK in the third XRootD block, and on a BLOCK edge, which neither
# kind fetched for the first.
MIXED_SIZE = 3 * blocks.XROOTD_BLOCK
MIXED_LATER = -(-2 * blocks.XROOTD_BLOCK // blocks.BLOCK) * blocks.BLOCK


def mixed_version(test: Test) -> Row:
    """Check that an object overwritten mid-way comes back as one version.

    Each cache reads the first block of an object, and the origins then
    take another version of it. Every later response, to a range the cache
    has yet to fetch and to the whole object, must be entirely one
    version, and the same one.
    """
    size = MIXED_SIZE
    rel = test.rel("mixed")
    v1, v2 = os.urandom(size), os.urandom(size)
    if why := test.put({rel: v1}):
        return FAIL, why

    def get(cache: common.Cache, request: str | None) -> web.Response:
        return test.ask("GET", cache.url, rel, {"Range": request} if request else {})

    block = blocks.BLOCK
    for cache in test.caches:
        answer = get(cache, f"bytes=0-{block - 1}")
        if answer.status != 206 or answer.body != v1[:block]:
            return FAIL, f"{cache.svc}: the first block of v1 came back wrong ({answer.describe()})"
    if why := test.put({rel: v2}):
        return FAIL, why
    start, end = MIXED_LATER, MIXED_LATER + block
    later = f"bytes {start}-{end - 1}"
    versions = {"v1": v1, "v2": v2}
    notes: list[str] = []
    for cache in test.caches:
        answer = get(cache, f"bytes={start}-{end - 1}")
        range_version, why = blocks.which_version(versions, ((start, end - 1),), answer)
        if range_version is None:
            return FAIL, f"{cache.svc}: {later}: {why}"
        answer = get(cache, None)
        version, why = blocks.which_version(versions, None, answer)
        if version is None:
            if answer.status == 200 and len(answer.body) == size:
                why = "it mixes versions, block by block: " + "".join(
                    (
                        "1"
                        if answer.body[i : i + block] == v1[i : i + block]
                        else "2" if answer.body[i : i + block] == v2[i : i + block] else "?"
                    )
                    for i in range(0, size, block)
                )
            return FAIL, f"{cache.svc}: the whole object: {why}"
        if range_version != version:
            return FAIL, f"{cache.svc}: {later} came back as {range_version}, the whole object as {version}"
        notes.append(f"{cache.svc} served {version}")
    return PASS, ", ".join(notes)


class Versions:
    """Two versions of each cache's objects for the overwrite scenarios.

    One object keeps its size, one grows, and one shrinks, by
    blocks.OVERWRITE_CHANGE.

    Attributes:
        CHANGES: How much longer v2 is than v1, by kind of change.
        NAMES: The kinds of change.
        base: The objects' collection.
        v1: Each object's first version, by name (<cache>-<kind>).
        v2: Each object's second version, by name: as long as v1, longer,
            or shorter.
    """

    CHANGES = {"same": 0, "grown": blocks.OVERWRITE_CHANGE, "shrunk": -blocks.OVERWRITE_CHANGE}
    NAMES = tuple(CHANGES)

    def __init__(self, test: Test, base: str):
        self.test = test
        self.base = base
        self.v1: dict[str, bytes] = {}
        self.v2: dict[str, bytes] = {}
        for cache in test.caches:
            for name, change in self.CHANGES.items():
                key = f"{cache.svc}-{name}"
                self.v1[key] = os.urandom(blocks.OVERWRITE_SIZE)
                self.v2[key] = os.urandom(blocks.OVERWRITE_SIZE + change)

    def which(
        self, cache: common.Cache, key: str, request: blocks.Request | None
    ) -> tuple[str | None, str]:
        """Return which version a read of key through cache came back as.

        The read is of object key (see Test.which_version()).
        """
        versions = {"v1": self.v1[key], "v2": self.v2[key]}
        return self.test.which_version(cache, f"{self.base}/{key}", versions, request)

    def now(self, cache: common.Cache) -> list[str | None]:
        """Return the version of a whole read of each of cache's objects."""
        return [self.which(cache, f"{cache.svc}-{n}", None)[0] for n in self.NAMES]

    def battery(self, cache: common.Cache) -> tuple[Counter[str], list[str]]:
        """Read cache's objects whole, and in every range of either version.

        Returns:
            How many reads came back as each version, and what was wrong.
        """
        jobs: list[tuple[str, blocks.Request | None]] = []
        for n in self.NAMES:
            key = f"{cache.svc}-{n}"
            ranges = blocks.requests(len(self.v1[key])) + blocks.requests(len(self.v2[key]))
            jobs += [(key, r) for r in [None, *dict.fromkeys(ranges)]]

        def read(job: tuple[str, blocks.Request | None]) -> tuple[str | None, str]:
            return self.which(cache, *job)

        with common.pool(8) as pool:
            found = list(pool.map(read, jobs))
        counts: Counter[str] = Counter()
        wrong: list[str] = []
        for (key, request), (version, why) in zip(jobs, found):
            if version is None:
                what = blocks.range_header(request) if request else "whole"
                wrong.append(f"{key} {what}: {why}")
            else:
                counts[version] += 1
        return counts, wrong


def cache_control_problem(test: Test, rel: str, wanted: str) -> str | None:
    """Return why an origin does not send Cache-Control wanted for rel.

    Returns:
        Why, or None if every origin sends it.
    """
    for origin in test.origins:
        answer = web.request(
            "GET", test.url(origin.url, rel), token=test.token, headers={"Range": "bytes=0-0"}
        )
        sent = answer.header("Cache-Control")
        if sent != wanted:
            return f"{origin.svc} sends Cache-Control '{sent}', not ORIGIN_CACHE_CONTROL's '{wanted}'"
    return None


def overwrite(test: Test, range_first: bool) -> Row:
    """Check that each cache serves an overwritten object as one version.

    Each cache reads v1 of three objects of its own, whole twice or a
    range then whole, and the origins then take v2: of the same size,
    larger, or smaller. Every response must then be all v1 or all v2, with
    that version's size. With ORIGIN_CACHE_CONTROL (`-p origin-max-age`)
    at native origins, every origin must send it, and each cache, once its
    copy is stale, must serve v2, and only v2. Without it, and at XRootD
    origins, which send none, a cache decides for itself how long its copy
    stays fresh. The origins are asked only if they take direct clients.

    Args:
        test: The test.
        range_first: Whether each cache first reads a range, then the
            whole, rather than the whole twice.
    """
    kind = "range-first" if range_first else "cached"
    versions = Versions(test, test.rel(f"overwrite-{kind}"))
    if why := put_under(test, versions.base, versions.v1):
        return FAIL, why
    sent_control = test.fed.sent_cache_control
    if sent_control and test.direct:
        why = cache_control_problem(test, f"{versions.base}/{next(iter(versions.v1))}", sent_control)
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
    if why := put_under(test, versions.base, versions.v2):
        return FAIL, why

    fresh = blocks.freshness(sent_control) if sent_control else None
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.caches:
        counts, wrong = versions.battery(cache)
        problems += wrong
        served = ", ".join(f"{v} {counts[v]}" for v in sorted(counts)) or "nothing"
        if fresh is None:
            notes.append(f"{cache.svc} served {served}")
            continue
        deadline = cached_at[cache.svc] + fresh + blocks.REVALIDATION_MARGIN
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
    return outcome(problems, "; ".join(notes))


def overwrite_cached(test: Test) -> Row:
    """Check overwrite(), of objects first read whole."""
    return overwrite(test, False)


def overwrite_range_first(test: Test) -> Row:
    """Check overwrite(), of objects first read through a range."""
    return overwrite(test, True)


def held_everywhere(test: Test, rel: str, data: bytes) -> bool:
    """Return whether every origin holds data at rel."""
    return all(h == common.digest(data) for h in holdings(test, rel))


def put_through(test: Test, cache: common.Cache, rel: str, v1: bytes) -> str | None:
    """Send write_through()'s PUTs of rel, which every origin holds as v1.

    Returns:
        What went wrong, or None.
    """
    url = test.url(cache.url, rel)
    v2 = os.urandom(len(v1) - blocks.OVERWRITE_CHANGE)
    why = test.get(cache.url, rel, v1, None)
    if why:
        return f"before the writes: {why}"
    read_only = test.session.token(test.namespace, credentials.R)
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
    if common.digest(v2) not in holdings(test, rel):
        return "a PUT succeeded, but no origin holds what it wrote"
    # Then only the cache's own copy could still be v1.
    why = test.put({rel: v2})
    if why:
        return why
    why = test.get(cache.url, rel, v2, None)
    if why:
        return f"after the PUT: {why}"
    return None


def new_through(test: Test, cache: common.Cache, rel: str, data: bytes, chunked: bool) -> str | None:
    """Send write_through()'s PUT of a new object, rel, holding data.

    Some origin must then hold data at rel, and the cache serve it.

    Args:
        test: The test.
        cache: The cache to send it through.
        rel: The object, which no origin holds.
        data: Its bytes.
        chunked: Whether to send them in chunks, with no Content-Length.

    Returns:
        What went wrong, or None.
    """
    how = f"a {'chunked ' if chunked else ''}PUT of {len(data)} bytes"
    answer = web.request("PUT", test.url(cache.url, rel), token=test.token, upload=data, chunked=chunked)
    if not answer.ok:
        return f"{how}: {answer.describe()}"
    if common.digest(data) not in holdings(test, rel):
        return f"{how} succeeded, but no origin holds what it wrote"
    why = test.get(cache.url, rel, data, None)
    if why:
        return f"after {how}: {why}"
    return None


def delete_through(test: Test, cache: common.Cache, rel: str) -> str | None:
    """Send write_through()'s DELETE of rel, which every origin holds.

    Returns:
        What went wrong, or None.
    """
    url = test.url(cache.url, rel)
    answer = web.request("DELETE", url, token=test.token)
    if not answer.ok:
        return f"a DELETE: {answer.describe()}"
    if None not in holdings(test, rel):
        return "a DELETE succeeded, but every origin holds the object"
    # Then only the cache's own copy could still be served.
    for origin in test.origins:
        error = stores.delete(origin, test.namespace, rel, test.setup)
        if error:
            return error
    answer = web.request("GET", url, token=test.token)
    if answer.status != 404:
        return f"after the DELETE: {answer.describe()}, not 404"
    return None


def refused_through(test: Test, cache: common.Cache, rel: str, v1: bytes) -> str | None:
    """Check that an XRootD cache refuses write_through()'s PUT of rel.

    Every origin holds rel as v1. The PUT must be refused, and change no
    origin.

    Returns:
        What went wrong, or None.
    """
    answer = web.request("PUT", test.url(cache.url, rel), token=test.token, upload=os.urandom(len(v1)))
    if answer.status is None or not 400 <= answer.status < 500:
        return f"a PUT: {answer.describe()}, not a refusal, though an XRootD cache takes no writes"
    if not held_everywhere(test, rel, v1):
        return "a PUT, refused, changed an origin"
    return None


def write_through(test: Test) -> Row:
    """Check that V2 caches relay writes, and XRootD caches refuse them.

    A V2 cache relays a write of /protected-a to an origin through the
    director, and drops its own copy. Of an object each holds, a PUT
    with a read-only token must be refused, and change nothing; one with
    the server's token must reach an origin; and once every origin holds
    what was written, the cache must serve it at once. Then likewise a
    DELETE, after which the cache must serve nothing, where the origins'
    storage can delete. Then a PUT of a new object of 0 bytes, and one
    sent in chunks with no Content-Length, as the Pelican client sends
    it, must each reach an origin, and be served by the cache. Where
    /protected-a takes no writes, the PUT must be refused. An XRootD
    cache takes no writes, so its PUT must be refused, and change no
    origin.
    """
    base = test.rel("write-through")
    v1 = {c.svc: os.urandom(blocks.OVERWRITE_SIZE) for c in test.caches}
    if why := put_under(test, base, v1):
        return FAIL, why
    cannot_delete = stores.cannot(test.fed, "delete")
    deletes = test.writes and not cannot_delete
    problems: list[str] = []
    for cache in test.caches:
        rel = f"{base}/{cache.svc}"
        if cache.kind != "v2":
            why = refused_through(test, cache, rel, v1[cache.svc])
        else:
            why = put_through(test, cache, rel, v1[cache.svc])
            if not why and deletes:
                why = delete_through(test, cache, rel)
            if not why and test.writes:
                why = new_through(test, cache, f"{rel}-empty", b"", False) or new_through(
                    test, cache, f"{rel}-chunked", os.urandom(blocks.OVERWRITE_SIZE), True
                )
        if why:
            problems.append(f"{cache.svc}: {why}")
    kinds = Counter(c.kind for c in test.caches)
    notes: list[str] = []
    if kinds["xrootd"]:
        notes.append(f"{kinds['xrootd']} XRootD cache(s) refused a PUT")
    if kinds["v2"] and not test.writes:
        notes.append(f"{kinds['v2']} V2 cache(s) refused a PUT")
    elif kinds["v2"] and not deletes:
        notes.append(f"{kinds['v2']} V2 cache(s) relayed PUTs, empty and chunked too; {cannot_delete}")
    elif kinds["v2"]:
        notes.append(f"{kinds['v2']} V2 cache(s) relayed PUTs, empty and chunked too, and a DELETE")
    return outcome(problems, "; ".join(notes))


# cache-evicted's objects: each this big, and enough of them to exceed a
# cache's high water mark by half.
EVICT_OBJECT = 8 << 20
# How long a cache may take to evict down to its high water mark, in
# seconds, by kind. A V2 cache's eviction runs every 10 seconds. An
# XRootD cache checks every 60 (heart_beat in XrdPfc's ResourceMonitor),
# and its purge spares whatever was read since the last one, so it may
# take two.
EVICT_WAIT = {"v2": 60, "xrootd": 180}
# How far above the mark its object files may add up to: the cache charges
# itself each object's file as it makes it, and counts in the database
# rather than on disk.
EVICT_SLACK = 1 << 20
# How many of the objects read last must survive the evictions.
LRU_NEWEST = 3


def evict_object(run_id: str, n: int) -> bytes:
    """Return the bytes of cache-evicted's object n.

    They are made anew from the run and n each time, since holding every
    object would take as much memory as the caches' bound. Test data, so
    not a cryptographic generator.
    """
    return random.Random(f"{run_id}/{n}").randbytes(EVICT_OBJECT)


def cached(test: Test, cache: common.Cache, rel: str) -> bool | None:
    """Return whether a cache holds all of rel.

    A V2 cache says, by only-if-cached (Test.cached()); an XRootD cache's
    .cinfo says (blocks.xrootd_holds()).

    Returns:
        Whether it does, or None if neither says.
    """
    if cache.kind == "xrootd":
        return blocks.xrootd_holds(cache, rel)
    return test.cached(cache, rel)


def reread_at(cache: common.Cache, high: int, count: int) -> int:
    """Return after how many objects cache_evicted reads object 0 again.

    That is as many as fit under cache's high water mark high, less one,
    so that the cache holds object 0 still, and evicts nothing yet. A V2
    cache stores an object with its blocks' tags; an XRootD cache, as is.
    """
    size = EVICT_OBJECT if cache.kind == "xrootd" else blocks.stored(EVICT_OBJECT)
    fit = high // size - 1
    return max(1, min(count - LRU_NEWEST - 1, fit))


def lru_order(
    test: Test, cache: common.Cache, base: str, count: int, again: int, was_held: bool | None
) -> tuple[str | None, str]:
    """Check which objects a cache holds after cache_evicted's reads.

    The LRU_NEWEST objects read last must still be held, and at least two
    of the oldest (1 to again - 1) gone. Object 0, read again after again
    objects while held (was_held), may be held or gone: a V2 cache
    records an access for its LRU order at most once in ten minutes
    (UpdateLRU in local_cache/database.go).

    An XRootD cache need only have evicted at least two. It purges the
    oldest by its .cinfo's mtime, in whole seconds (FPurgeState in
    XrdPfc), which it writes some seconds after a read, and not after
    every one, so its order does not follow reads a second apart.

    Returns:
        What is wrong, or None; and a note of what the cache held.
    """
    found = {n: cached(test, cache, f"{base}/{n}") for n in range(count)}
    unsure = [n for n, held in found.items() if held is None]
    if unsure:
        how = "only-if-cached got neither 200 nor 504"
        if cache.kind == "xrootd":
            how = "its .cinfo is unreadable"
        return f"object {unsure[0]}: {how}", ""
    if cache.kind == "xrootd":
        gone = [n for n in range(count) if not found[n]]
        if len(gone) < 2:
            return f"{len(gone)} of the {count} objects are gone, not at least two", ""
        return None, f"{len(gone)} of {count} gone, in an order not checked (see lru_order())"
    lost = [n for n in range(count - LRU_NEWEST, count) if not found[n]]
    if lost:
        return f"object {lost[0]}, among the {LRU_NEWEST} read last, is gone", ""
    gone = [n for n in range(1, again) if not found[n]]
    if len(gone) < 2:
        return f"{len(gone)} of objects 1 to {again - 1}, the oldest, are gone, not at least two", ""
    then = {True: "held", False: "gone", None: "unknown"}[was_held]
    now = "held" if found[0] else "gone"
    return None, (
        f"{len(gone)} of objects 1 to {again - 1} gone; object 0, read again after"
        + f" {again} ({then} then), {now}"
    )


def evicted(test: Test, cache: common.Cache, base: str, count: int) -> tuple[str | None, str]:
    """Run cache_evicted at one cache, whose store is bounded.

    Its bounds are in test.fed.eviction.

    Returns:
        What went wrong, or None; and a note of what it held (see
        lru_order()).
    """
    high, low = test.fed.eviction[cache.svc]
    again = reread_at(cache, high, count)
    wait = EVICT_WAIT[cache.kind]
    was_held: bool | None = None
    for n in range(count):
        if n == again:
            was_held = cached(test, cache, f"{base}/0")
            why = test.get(cache.url, f"{base}/0", evict_object(test.run, 0), None)
            if why:
                return f"object 0, read again: {why}", ""
        why = test.get(cache.url, f"{base}/{n}", evict_object(test.run, n), None)
        if why:
            return f"object {n}, cold: {why}", ""
    if blocks.store_bytes(cache) is None:
        return f"framework/var/{blocks.cache_dir(cache)} has no store to measure", ""

    def under() -> bool:
        return (blocks.store_bytes(cache) or 0) <= high + EVICT_SLACK

    if not common.wait_for(under, wait, interval=2):
        return (
            f"holds {blocks.store_bytes(cache)} bytes {wait}s after reading {count * EVICT_OBJECT},"
            f" above its high water mark of {high}"
        ), ""
    held = blocks.store_bytes(cache) or 0
    if held < low - EVICT_OBJECT:
        return f"holds {held} bytes, below its low water mark of {low}, so it evicted too much", ""
    why, note = lru_order(test, cache, base, count, again, was_held)
    if why:
        return why, note
    for n in range(count):
        data = evict_object(test.run, n)
        for request in (None, ((blocks.XROOTD_BLOCK, blocks.XROOTD_BLOCK + blocks.BLOCK - 1),)):
            why = test.get(cache.url, f"{base}/{n}", data, request)
            if why:
                what = blocks.range_header(request) if request else "whole"
                return f"object {n}, {what}, after the evictions: {why}", note
    target = os.path.join(test.tmp, f"evicted-{cache.svc}")
    code, out, err = get_from(test, cache, f"{base}/0", target)
    if code != 0:
        return f"`pelican object get` of object 0 after the evictions: {last_line(err or out)}", note
    if not common.file_holds(target, evict_object(test.run, 0)):
        return "`pelican object get` of object 0 after the evictions: not the object", note
    return None, note


def cache_evicted(test: Test) -> Row:
    """Check that a bounded cache evicts the oldest objects first.

    Each cache whose store is bounded (`-p cache-small`), of either kind,
    reads cold and whole enough objects of EVICT_OBJECT bytes to exceed
    its high water mark by half. It reads the first again once as many
    as fit under the mark have been read (reread_at()). Within
    EVICT_WAIT seconds for its kind, its store must have come back under
    the mark, and not below its low one by more than an object. It must
    have evicted the oldest objects and kept the newest, or, an XRootD
    cache, at least two objects (lru_order()). Every object must still
    come back whole and in a range, and the first by
    `pelican object get --cache`. The objects are one set, read cold by
    each cache. An XRootD cache's marks are the bounds on its files
    (Cache.FilesMaxSize and Cache.FilesBaseSize; see fed.sh).
    """
    caches = [c for c in test.caches if c.svc in test.fed.eviction]
    if not caches:
        return SKIP, "no cache has a bounded store ('-p cache-small')"
    high = max(test.fed.eviction[c.svc][0] for c in caches)
    count = -(-3 * high // (2 * EVICT_OBJECT))
    base = test.rel("evicted")
    # One at a time, so as not to hold them all (see evict_object()).
    for n in range(count):
        if why := test.put({f"{base}/{n}": evict_object(test.run, n)}):
            return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for cache in caches:
        why, note = evicted(test, cache, base, count)
        if why:
            problems.append(f"{cache.svc}: {why}")
        if note:
            notes.append(f"{cache.svc}: {note}")
    return outcome(
        problems,
        "; ".join([f"{len(caches)} cache(s): {count} objects of {EVICT_OBJECT} bytes, read twice"] + notes),
    )


# Each scenario, by name: it takes the Test, and returns its result and a
# note.
RUN = {
    "put-declared": put_declared,
    "put-chunked": put_chunked,
    "put-client": put_client,
    "put-plugin": put_plugin,
    "put-checksum": put_checksum,
    "put-checksum-config": put_checksum_config,
    "origin-read": origin_read,
    "client-get": client_get,
    "plugin-get": plugin_get,
    "cache-whole-first": cache_whole_first,
    "director-read": director_read,
    "cache-range-first": cache_range_first,
    "cache-stored": cache_stored,
    "cache-overlap": cache_overlap,
    "coalescing": coalescing,
    "cache-assemble": cache_assemble,
    "cache-abandoned": cache_abandoned,
    "cache-corrupted": cache_corrupted,
    "transfer-status": transfer_status,
    "transfer-status-failure": transfer_status_failure,
    "cache-memory": cache_memory,
    "origin-transfer-status": origin_transfer_status,
    "digests": digests_scenario,
    "digest-overwrite": digest_overwrite,
    "digest-defaults": digest_defaults,
    "digest-relay": digest_relay,
    "digest-range-first": digest_range_first,
    "stale-origin-digest": stale_origin_digest,
    "past-end": past_end,
    "empty": empty,
    "huge-ranges": huge_ranges,
    "conditional": conditional,
    "conditional-put": conditional_put,
    "etag-agreement": etag_agreement,
    "conditional-edges": conditional_edges,
    "mixed-version": mixed_version,
    "overwrite-cached": overwrite_cached,
    "overwrite-range-first": overwrite_range_first,
    "write-through": write_through,
    "cache-evicted": cache_evicted,
}


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test (no /protected-a), or None."""
    return suite.unexported(fed)


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    stores.require_dirs(fed.origins, ["protected-a"], "data/blocks")
    test = Test(session)
    # Earlier runs' objects come out first.
    test.clean("before the run")
    print(f"Block test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        for name in selected:
            if name in CACHE_SCENARIOS and not test.caches:
                results.add(name, SKIP, "this shape has no cache")
                continue
            results.add(name, *suite.attempt(functools.partial(RUN[name], test), (stores.Unreadable,)))
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        test.clean("after the run")
