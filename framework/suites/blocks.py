"""Objects and byte ranges at the block boundaries of pstore (an origin's
encrypted store), the V2 cache, and the XRootD cache: write objects of
sizes on either side of each boundary, then read them back, whole and in
ranges, from each origin, through each cache, and through both clients;
the digests that each server reports of them, asked for by default or
several at once, and an upload the client must verify by one;
conditional reads and writes, and the ETags they name, which every
method must report alike; and objects that caches read in overlapping
pieces or give up on, that are overwritten once cached, that are
corrupted in a cache's store, or that are written through a cache. Also the signals by which a client trusts what a cache
gives it: the X-Transfer-Status trailer, which reports a failure after
the headers; the digests a V2 cache relays from the origins, and its
refusal to keep an object whose bytes fail them; and that a cache
fetches an object that many clients read at once from the origins once.

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

import base64
import binascii
import functools
import json
import os
import random
import re
import shutil
import subprocess  # nosec B404
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from typing import Optional
from urllib.parse import urlencode, urlsplit

from testlib import (
    blocks,
    cacheapi,
    common,
    credentials,
    digests,
    metrics,
    stores,
    transfers,
    web,
    webdav,
)
from testlib.common import die, first, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

SCENARIOS = {
    "put-declared": "upload every size with a Content-Length to every origin",
    "put-chunked": "upload every size in chunks, with no length, to every origin",
    "put-client": "`pelican object put` of every size",
    "put-plugin": "stash_plugin's upload of every size",
    "put-checksum": "`pelican object put --require-checksum` succeeds only where the origin reports a digest",
    "origin-read": "each origin returns every object whole and every range",
    "client-get": "`pelican object get` of every size, through a cache and with --direct",
    "plugin-get": "stash_plugin's download of every size, through a cache and direct",
    "cache-whole-first": "each cache: a cold whole read, a warm one, then every range",
    "cache-range-first": "each cache: a cold range first, then every range, then the whole",
    "cache-overlap": "each cache: ranges that overlap what it holds, in turn and at once",
    "coalescing": "each federated cache: concurrent cold reads of an object fetch it from the origins once",
    "cache-assemble": "each cache: ranges that add up to an object leave it holding all",
    "cache-abandoned": "each cache: a cold read that its client gives up on spoils nothing",
    "cache-corrupted": "each V2 cache: a block corrupted or cut from its store is never served",
    "transfer-status": "each federated cache, asked, ends a GET with an X-Transfer-Status trailer of 200: OK",
    "transfer-status-failure": "each federated V2 cache reports a block it cannot repair in the trailer, and the clients fail",
    "origin-transfer-status": "each origin, asked, ends a GET with an X-Transfer-Status trailer of 200: OK",
    "digests": "every digest that a server reports (Want-Digest) is the object's",
    "digest-overwrite": "each origin's digests and ETag of an object follow its overwrites",
    "digest-defaults": "each origin's digests asked for by default, by two algorithms, and by q-values",
    "digest-relay": "each federated V2 cache relays the origins' digests, cold and warm, to HEAD, GET, and the client",
    "digest-range-first": "each federated V2 cache relays the origins' digests of an object it first read through a range",
    "stale-origin-digest": "each federated V2 cache keeps and passes on nothing whose bytes fail the origins' digest",
    "past-end": "a range that starts at or past an object's end: 416, not bytes",
    "empty": "a 0-byte object: 200 with no body; a range of it gets no bytes",
    "conditional": "a GET with If-None-Match or If-Modified-Since: 304 only when the condition holds",
    "conditional-put": "a PUT with If-Match or If-None-Match: refused (412) unless the condition holds",
    "etag-agreement": "each origin gives one version one ETag, by every method, and honors it in conditions",
    "conditional-edges": "each origin: conditions on a missing object, invalid or combined, and racing PUTs",
    "mixed-version": "each cache: an object overwritten mid-way comes back as one version",
    "overwrite-cached": "each cache: an object it holds, overwritten, comes back as one version",
    "overwrite-range-first": "the same, for an object it first read through a range",
    "write-through": "each V2 cache relays a PUT and a DELETE, and serves the result at once; an XRootD cache refuses",
    "cache-evicted": "each bounded V2 cache: objects past its bound evict, oldest first, and every one still comes back",
}

# The scenarios that read through each cache, and so have nothing to test
# without one.
CACHE_SCENARIOS = (
    "cache-whole-first",
    "cache-range-first",
    "cache-overlap",
    "coalescing",
    "cache-assemble",
    "cache-abandoned",
    "cache-corrupted",
    "transfer-status",
    "transfer-status-failure",
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
        # The digests the origins' storage reports, and why not the rest.
        self.digests, self.no_digests = stores.digests(session.fed)
        # Each V2 cache's admin token, by service (see summary()).
        self.admins: dict[str, str] = {}

    def rel(self, set_name: str, size: int) -> str:
        """Where a set's object of size is in /protected-a."""
        return f"{self.base}/{set_name}/{size}"

    def tiers(self, origin: common.Origin) -> Optional[dict[str, float]]:
        """pstore's writes by tier, or None if /metrics has none, as before
        an origin's first write."""
        found = metrics.fetch(origin_root(origin.url)) or metrics.EMPTY
        if not found.has(PSTORE_WRITES):
            return None
        return {t: found.value(PSTORE_WRITES, tier=t) for t in TIERS}

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
    test: Test,
    name: str,
    urls: list[str],
    directory: str,
    upload: bool,
    cache: Optional[str] = None,
) -> tuple[bool, str]:
    """Run stash_plugin on urls, each object's local file named as its
    URL ends, in directory, with the server's token for /protected-a, and
    through cache alone (Client.PreferredCaches), if one is named:
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
    if cache:
        env["PELICAN_CLIENT_PREFERREDCACHES"] = cache
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


def put_checksum(test: Test) -> tuple[str, str]:
    """`pelican object put --require-checksum` of every size: the client
    verifies each upload against a digest the origin reports (a HEAD with
    Want-Digest after the PUT; client/handle_http.go in Pelican), asking
    for the first algorithm the origins' storage reports (it asks for
    crc32c otherwise), so it must succeed, and leave the objects intact,
    for the objects whose digests the storage reports
    (stores.has_digests(); the client declares no length), and fail for
    the rest, rather than report an unverified upload as a success."""
    if not test.writes:
        return SKIP, NO_WRITES
    collection = f"{test.base}/put-checksum"
    errors = stores.make_dirs(test.origins, "protected-a", collection, test.token)
    if errors:
        return FAIL, "; ".join(errors)
    directory, files = local_sizes(test, "put-checksum")
    verifiable = {
        size: data
        for size, data in files.items()
        if stores.has_digests(test.fed, size, declared=False)
    }
    notes: list[str] = []
    for sizes, verified in ((verifiable, True), (set(files) - set(verifiable), False)):
        if not sizes:
            continue
        algorithm = ["--checksum-algorithm", test.digests[0]] if test.digests else []
        code, out, err = test.session.pelican_cmd(
            "object",
            "put",
            "--require-checksum",
            *algorithm,
            "--token",
            test.token_file,
            *(os.path.join(directory, str(size)) for size in sorted(sizes)),
            f"{test.fed.url}/protected-a/{collection}/",
        )
        if verified:
            if code != 0:
                return FAIL, f"exit {code}: {last_line(err or out)}"
            why = uploaded(test, verifiable, collection)
            if why:
                return FAIL, why
            notes.append(f"{len(sizes)} objects verified by {test.digests[0]}")
            continue
        why = (
            test.no_digests
            or f"the storage holds no digest of an object over {stores.S3_PART} bytes"
        )
        if code == 0:
            return FAIL, f"succeeded, though {why}, so nothing was verified"
        if not re.search(r"checksum", out + err, re.I):
            return FAIL, f"failed, but not for want of a checksum: {last_line(err or out)}"
        notes.append(f"{len(sizes)} refused, since {why}")
    return PASS, "; ".join(notes)


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


def summary(test: Test, cache: common.Cache, rel: str) -> tuple[Optional[cacheapi.Blocks], str]:
    """Which blocks of rel in /protected-a a V2 cache holds, as its admin
    asks its introspection (cacheapi.blocks_held()); or None, and why it
    did not say."""
    if cache.svc not in test.admins:
        test.admins[cache.svc] = cacheapi.admin_token(test.session, cache)
    url = cacheapi.object_url(test.fed, f"/protected-a/{rel}")
    return cacheapi.blocks_held(cache, test.admins[cache.svc], url)


def assembled_part(test: Test, cache: common.Cache, rel: str, found: Findings) -> bool:
    """After cache_assemble's first range, which a V2 cache fetches block by
    block (initObjectFromStat in local_cache/persistent_cache.go): its
    introspection must show the range's blocks held, and not block 0,
    since it fills no object from the start for a range in its middle.
    Whether its introspection said: if not, the caller falls back to Age."""
    held, why = summary(test, cache, rel)
    if held is None:
        found.note(f"{cache.svc}: no block summary ({why}), so Age")
        return False
    start, end = blocks.ASSEMBLE[0]
    wanted = range(start // blocks.BLOCK, end // blocks.BLOCK + 1)
    lacking = [n for n in wanted if n in held.missing]
    if lacking:
        found.fail(f"{cache.svc}: after the first range, it lacks its block {lacking[0]}")
    elif held.complete or 0 not in held.missing:
        found.fail(
            f"{cache.svc}: after a range from block {wanted[0]}, it holds block 0"
            + f" ({held.describe()}), as if it filled the object from the start"
        )
    return True


def assemble(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """cache_assemble at one cache, of rel, which holds data."""
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
                f"{cache.svc}: {cinfo.count} of {cinfo.blocks} blocks after the first range,"
                + " not some"
            )
    for request in ranges[1:]:
        why = test.get(cache.url, rel, data, request)
        if why:
            found.fail(f"{cache.svc}: {blocks.header(request)}: {why}")
    if introspected:
        last: list[Optional[cacheapi.Blocks]] = [None]

        def complete() -> bool:
            last[0] = summary(test, cache, rel)[0]
            return last[0] is not None and last[0].complete

        if common.wait_for(complete, 10, interval=1):
            found.note(f"{cache.svc}: {last[0].describe() if last[0] else ''}")
        else:
            held = last[0].describe() if last[0] else "no block summary"
            found.fail(f"{cache.svc}: {held} after every range, not all")
    elif cache.kind == "v2":
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
    after the first, and all of it after the last: a V2 cache's
    introspection says which blocks it holds (assembled_part()), and an
    XRootD cache's .cinfo. Then the whole object must come back intact. A
    V2 cache whose introspection says nothing is judged by Age, which it
    sends only once it holds every block. Only a cache that cannot show
    the partial state after the first (with Age and no block summary,
    with no .cinfo, or with a .cinfo showing none or all of the blocks)
    leaves the result unsure; an XRootD cache whose .cinfo could be read
    then must still have one, showing every block, after the last."""
    base = f"{test.base}/assemble"
    objects = {c.svc: os.urandom(blocks.ASSEMBLE_SIZE) for c in test.caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for cache in test.caches:
        assemble(test, cache, f"{base}/{cache.svc}", objects[cache.svc], found)
    return found.verdict()


# coalescing's object, and how many clients read it at once.
COALESCE_SIZE = 4 << 20
COALESCE_READERS = 16
# What a native origin counts of the GETs it serves (metrics/http_metrics.go
# in Pelican): the bytes it sent, and the requests, by the username of the
# token that each presented (the tests', for a cache's fetch on a client's
# behalf).
SENT_BYTES = "pelican_http_bytes_total"
SENT = {"server_type": "origin", "direction": "out", "method": "GET"}
GETS = "pelican_http_requests_total"
OUR_GETS = {"server_type": "origin", "method": "GET", "username": credentials.SUBJECT}
# How many GETs a V2 cache may make of the origins for the cold reads: it
# fetches a whole object in one (downloadObject in Pelican's
# local_cache/persistent_cache.go), and may need a second.
COALESCE_GETS = 2
# How many bytes the origins may send while the reads are warm: the
# directors' health tests fetch a small object from each origin every 15
# seconds (Director.OriginCacheHealthTestInterval).
WARM_SLACK = 64 << 10


def origin_metrics(test: Test) -> Optional[list[metrics.Snapshot]]:
    """Every origin's metrics, or None if one did not answer."""
    found: list[metrics.Snapshot] = []
    for origin in test.fed.origins:
        snapshot = metrics.fetch(origin.web_url)
        if snapshot is None:
            return None
        found.append(snapshot)
    return found


def risen(
    before: list[metrics.Snapshot],
    after: list[metrics.Snapshot],
    name: str,
    labels: dict[str, str],
) -> float:
    """How much metric name's series with labels rose at the origins, in
    all, from before to after."""
    return sum(metrics.delta(b, a, name, **labels) for b, a in zip(before, after))


def read_at_once(test: Test, cache: common.Cache, rel: str, data: bytes) -> Optional[str]:
    """COALESCE_READERS whole reads of rel in /protected-a, which holds
    data, through cache, all at once: the first thing wrong, or None."""
    found = at_once(
        [
            functools.partial(test.get, cache.url, rel, data, None)
            for _ in range(COALESCE_READERS)
        ]
    )
    wrong = [why for why in found if why]
    return first(wrong) if wrong else None


def coalesced(test: Test, cache: common.Cache, rel: str, data: bytes, found: Findings) -> None:
    """coalescing at one cache, of rel, which holds data."""
    cold = origin_metrics(test)
    if cold is None:
        found.fail("an origin's /metrics did not answer")
        return
    why = read_at_once(test, cache, rel, data)
    if why:
        found.fail(f"{cache.svc}: a cold read: {why}")
        return
    # An origin counts a request once its handler returns, which may be
    # just after the cache has handed the last reader its bytes.
    start = cold
    after = [start]

    def counted() -> bool:
        now = origin_metrics(test)
        if now is not None:
            after[0] = now
        return risen(start, after[0], SENT_BYTES, SENT) >= len(data)

    common.wait_for(counted, 10, interval=1)
    sent = risen(start, after[0], SENT_BYTES, SENT)
    gets = risen(start, after[0], GETS, OUR_GETS)
    copies = sent / len(data)
    if sent < len(data):
        found.doubt(
            f"{cache.svc}: the origins counted {sent:.0f} bytes sent for GETs, less than the"
            + " object, so they did not count the cache's fetch"
        )
        return
    if copies >= 2:
        found.fail(
            f"{cache.svc}: for {COALESCE_READERS} cold reads at once, the origins sent"
            + f" {copies:.1f} objects' worth, not one"
        )
    elif cache.kind == "v2" and gets > COALESCE_GETS:
        found.fail(
            f"{cache.svc}: for {COALESCE_READERS} cold reads at once, the origins served"
            + f" {gets:g} GETs, not one or {COALESCE_GETS}"
        )
    unnamed = " (none under the tests' username)" if not gets else ""
    note = f"{cache.svc}: cold, {copies:.2f} objects in {gets:g} GETs{unnamed}"
    warm = after[0]
    why = read_at_once(test, cache, rel, data)
    if why:
        found.fail(f"{cache.svc}: a warm read: {why}")
        return
    time.sleep(1)
    now = origin_metrics(test)
    if now is None:
        found.fail("an origin's /metrics did not answer")
        return
    sent = risen(warm, now, SENT_BYTES, SENT)
    served = sum(risen(warm, now, GETS, {**OUR_GETS, "code": c}) for c in ("200", "206"))
    if served or sent > WARM_SLACK:
        found.fail(
            f"{cache.svc}: for {COALESCE_READERS} warm reads at once, the origins served"
            + f" {served:g} GETs and sent {sent:.0f} bytes, though it held the object"
        )
    found.note(f"{note}; warm, {sent:.0f} bytes")


def coalescing(test: Test) -> tuple[str, str]:
    """Each federated cache: COALESCE_READERS cold whole reads of an
    object of its own, all at once, must have the origins send it about
    once, as their counters show (SENT_BYTES): not once per reader, which
    a cache is there to spare them. A V2 cache fetches an object whole in
    one GET, and may make no more than COALESCE_GETS for it under the
    tests' username (GETS); an XRootD cache fetches it block by block,
    so only the bytes are judged. Then as many warm reads must have the
    origins serve no GET of the tests', and send no more than the
    directors' health tests do (WARM_SLACK). Only a native origin keeps
    the counters. A site-local cache reads through another cache, and is
    left to the sitelocal suite."""
    if not test.fed.native_origins:
        return SKIP, f"XRootD serves the origins ({test.fed.origin_variant}), not Pelican"
    caches = test.fed.federated_caches
    if not caches:
        return SKIP, "this shape has no federated cache"
    base = f"{test.base}/coalescing"
    objects = {c.svc: os.urandom(COALESCE_SIZE) for c in caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for cache in caches:
        coalesced(test, cache, f"{base}/{cache.svc}", objects[cache.svc], found)
    return found.verdict()


# How much of an object cache-abandoned reads before it gives up: part of
# the first XRootD block, and more than a read batch of V2 blocks.
ABANDON_AFTER = 50000


def cache_abandoned(test: Test) -> tuple[str, str]:
    """Each cache: a cold whole read of an object of its own, which its
    client gives up on after ABANDON_AFTER bytes. Those bytes must be the
    object's; and afterward, its last block, then the whole object, must
    come back intact, however far the cache had fetched it. A V2 cache's
    introspection says how far that was (abandoned_blocks())."""
    base = f"{test.base}/abandoned"
    objects = {c.svc: os.urandom(blocks.OVERLAP_SIZE) for c in test.caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
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
        if cache.kind == "v2":
            why = abandoned_blocks(test, cache, rel, notes)
            if why:
                problems.append(f"{cache.svc}: {why}")
        last = ((len(data) - blocks.BLOCK, len(data) - 1),)
        for request in (last, None):
            why = test.get(cache.url, rel, data, request)
            if why:
                what = blocks.header(request) if request else "the whole object"
                problems.append(f"{cache.svc}: {what} afterward: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{len(test.caches)} cache(s)"] + notes)


def abandoned_blocks(
    test: Test, cache: common.Cache, rel: str, notes: list[str]
) -> Optional[str]:
    """What a V2 cache's introspection says it holds of rel once its
    client has given up, added to notes: the cache may go on fetching the
    object, or not, so either is accepted, but a summary that contradicts
    itself is not. What is wrong with it, or None."""
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


# A V2 cache's chaos API (Cache.EnableChaosAPI, which config.d/cache/v2
# turns on): an admin may corrupt a block of an object in its store, or
# cut bytes from the end of one of its chunk files
# (local_cache/persistent_cache_api.go in Pelican).
CHAOS_PATH = "/api/v1.0/cache/introspect/chaos"
# How long a cache may take to serve a spoiled object again, in seconds.
REPAIR_WAIT = 30


class NoChaos(Exception):
    """The cache has no chaos API."""


def admin_token(test: Test, cache: common.Cache) -> str:
    """A token that cache's web API takes as its admin's (see
    credentials.mint_admin()), signed with the cache's own key."""
    web_url = origin_root(cache.url)
    issuer = credentials.local_issuer(test.fed, web_url)
    key = common.signing_key(f"issuer-keys/{cache.svc}")
    return credentials.mint_admin(
        test.fed, test.session.path(f"tokens/admin.{cache.svc}"), issuer, key
    )


def spoil(
    test: Test, cache: common.Cache, token: str, rel: str, op: str, **params: str
) -> None:
    """Have cache spoil its copy of rel in /protected-a: op `corrupt`
    (a block, with `block` and `bytes`) or `truncate` (a chunk file, with
    `chunk` and `drop-bytes`). The cache keys an object by its URL at the
    director (normalizePath in local_cache/persistent_cache.go). Raises
    NoChaos if the cache has no such API, and ValueError if it refused."""
    director_host = urlsplit(str(test.fed.discovery["director_endpoint"])).netloc
    query = {"op": op, "url": f"pelican://{director_host}/protected-a/{rel}", **params}
    answer = web.request(
        "POST", f"{origin_root(cache.url)}{CHAOS_PATH}?{urlencode(query)}", token=token
    )
    if answer.status == 404:
        raise NoChaos(f"{cache.svc} has no chaos API ({answer.describe()})")
    if answer.status in (401, 403):
        raise ValueError(f"{cache.svc} refused the admin token: {answer.describe()}")
    if answer.status != 200:
        said = answer.body[:200].decode(errors="replace")
        raise ValueError(f"{cache.svc} did not {op}: {answer.describe()}: {said}")


def holds_all(test: Test, cache: common.Cache, rel: str) -> bool:
    """Whether a V2 cache says, with Age, that it holds all of rel."""
    return bool(has_age(test, cache, rel))


def never_spoiled(
    test: Test, cache: common.Cache, rel: str, data: bytes, request: blocks.Request
) -> Optional[str]:
    """Read request of rel in /protected-a through cache, then the whole
    object: each must be the object's bytes, or fail, but never be other
    bytes; and within REPAIR_WAIT seconds the whole object must come back
    intact, the cache having fetched the spoiled blocks again (a V2 cache
    repairs a block that fails its authentication tag;
    local_cache/range_reader.go). What went wrong, or None."""
    url = f"{cache.url}/protected-a/{rel}"
    first_byte, last_byte = blocks.resolve(len(data), request[0])
    for what, headers, want in (
        (
            blocks.header(request),
            {"Range": blocks.header(request)},
            data[first_byte : last_byte + 1],
        ),
        ("the whole object", {}, data),
    ):
        answer = web.request("GET", url, token=test.token, headers=headers, timeout=120)
        if answer.status in (200, 206) and answer.body != want and answer.body != data:
            return (
                f"{what}, once spoiled: {answer.describe()} with {len(answer.body)} bytes"
                + " that are not the object's"
            )

    def intact() -> bool:
        answer = web.request("GET", url, token=test.token, timeout=120)
        return answer.status == 200 and answer.body == data

    if not common.wait_for(intact, REPAIR_WAIT, interval=2):
        return f"the whole object is not served intact within {REPAIR_WAIT}s of being spoiled"
    return None


def cache_corrupted(test: Test) -> tuple[str, str]:
    """Each V2 cache: once it holds an object whole, a block of it is
    corrupted in the cache's store, and once it holds another, bytes are
    cut from the end of its chunk file (see spoil()). A read of the
    spoiled part, and of the whole object, must then never be other bytes
    (see never_spoiled()). An XRootD cache has no such API, and is passed
    over."""
    caches = [c for c in test.caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no V2 cache, whose chaos API alone can spoil a block"
    base = f"{test.base}/corrupted"
    # Each spoiling: its name, op and parameters, and the range it spoils.
    size = blocks.OVERLAP_SIZE
    trials: list[tuple[str, str, dict[str, str], blocks.Request]] = [
        (
            "corrupt",
            "corrupt",
            {"block": "1", "bytes": str(blocks.TAG)},
            ((blocks.BLOCK, blocks.BLOCK + 99),),
        ),
        (
            "truncate",
            "truncate",
            {"chunk": "-1", "drop-bytes": str(blocks.BLOCK)},
            ((size - 100, size - 1),),
        ),
    ]
    objects = {f"{c.svc}-{name}": os.urandom(size) for c in caches for name, _, _, _ in trials}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    for cache in caches:
        token = admin_token(test, cache)
        for name, op, params, request in trials:
            rel, data = f"{base}/{cache.svc}-{name}", objects[f"{cache.svc}-{name}"]
            why = test.get(cache.url, rel, data, None)
            if why:
                problems.append(f"{cache.svc}: {name}, before spoiling: {why}")
                continue
            if not common.wait_for(
                functools.partial(holds_all, test, cache, rel), 10, interval=1
            ):
                problems.append(
                    f"{cache.svc}: {name}: no Age after a whole read, so it does not hold it all"
                )
                continue
            try:
                spoil(test, cache, token, rel, op, **params)
            except NoChaos as e:
                return (
                    SKIP,
                    f"{e}; Cache.EnableChaosAPI is in Pelican v7.27.0 and v26.0.0-rc.0 on",
                )
            except ValueError as e:
                problems.append(str(e))
                continue
            why = never_spoiled(test, cache, rel, data, request)
            if why:
                problems.append(f"{cache.svc}: {name}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(caches)} V2 cache(s): {', '.join(name for name, _, _, _ in trials)}"


# What a client sends to learn how a transfer ended once its headers have
# gone out (docs/pelican-http-headers.md in Pelican): the server then sends
# the body in chunks, announces `Trailer: X-Transfer-Status`, and ends with
# that trailer, `<status>: <text>`.
ASK_STATUS = {"X-Transfer-Status": "true", "TE": "trailers"}
STATUS_OK = "200: OK"
# transfer-status's objects: one of several V2 blocks, and one under 4,096
# bytes, which a V2 cache keeps inline in its database. Each read is of an
# object of its own, so that it is cold the first time: the whole object;
# a range across the second and third blocks, and the last 1,000 bytes, as
# Pelican's own tests ask (e2e_fed_tests/transfer_status_test.go); and the
# small object.
STATUS_SIZE = 16384
STATUS_READS: dict[str, tuple[int, Optional[blocks.Request]]] = {
    "whole": (STATUS_SIZE, None),
    "range": (STATUS_SIZE, ((blocks.BLOCK, 3 * blocks.BLOCK - 1),)),
    "suffix": (STATUS_SIZE, ((None, 1000),)),
    "small": (512, None),
}
# The block that transfer-status-failure corrupts: past the bytes that
# http.ServeContent reads to sniff a type before it sends the headers, so
# that the failure comes after them.
FAILED_BLOCK = 3


def status_failed(said: str) -> bool:
    """Whether an X-Transfer-Status trailer, as said, reports a failure: a
    status other than 2xx."""
    found = re.match(r"\s*(\d{3})\b", said)
    return found is not None and not found.group(1).startswith("2")


def read_status(
    test: Test,
    url: str,
    rel: str,
    data: bytes,
    request: Optional[blocks.Request],
    ask: bool = True,
) -> Optional[str]:
    """GET rel in /protected-a (or a range of it), which holds data, at
    url's server, asking for the transfer's status if ask (ASK_STATUS):
    what is wrong, or None. So asked, the answer must announce the
    trailer and end with it, STATUS_OK; unasked, no trailer may come."""
    headers = dict(ASK_STATUS) if ask else {}
    if request:
        headers["Range"] = blocks.header(request)
    answer = web.request(
        "GET", f"{url}/protected-a/{rel}", token=test.token, headers=headers, timeout=120
    )
    if request:
        why = blocks.check(data, request, answer.status, answer.headers, answer.body)
    elif answer.status != 200:
        why = f"{answer.describe()}, not 200"
    else:
        why = None if answer.body == data else f"{len(answer.body)} bytes, not the object"
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


def transfer_status(test: Test) -> tuple[str, str]:
    """Each federated cache, V2 or XRootD, asked for a transfer's status
    (local_cache/persistent_cache_api.go in Pelican; XrdHttpReq.cc in
    XRootD, 5.6 on): each of STATUS_READS, cold and then warm, must
    announce the trailer and end with it, STATUS_OK (read_status()).
    Unasked, a GET of the whole object must come with no trailer. A HEAD
    so asked has no body, and so no trailer, but must be answered as any
    HEAD, with the object's length. A site-local cache reads through
    another cache, and is left to the sitelocal suite."""
    caches = test.fed.federated_caches
    if not caches:
        return SKIP, "this shape has no federated cache"
    base = f"{test.base}/transfer-status"
    objects = {
        f"{c.svc}-{name}": os.urandom(size)
        for c in caches
        for name, (size, _) in STATUS_READS.items()
    }
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
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
            "HEAD",
            f"{cache.url}/protected-a/{base}/{key}",
            token=test.token,
            headers=ASK_STATUS,
        )
        length = answer.header("Content-Length")
        if answer.status != 200:
            problems.append(f"{cache.svc}: a HEAD that asked: {answer.describe()}, not 200")
        elif length and length != str(STATUS_SIZE):
            problems.append(f"{cache.svc}: a HEAD that asked: Content-Length {length}")
    if problems:
        return FAIL, first(problems)
    return (
        PASS,
        f"{len(caches)} cache(s): '{STATUS_OK}' for {len(STATUS_READS)} reads, cold and warm",
    )


def origin_transfer_status(test: Test) -> tuple[str, str]:
    """Each origin, asked for a transfer's status, by a GET of a whole
    object of the run's (read_status()): Pelican's
    docs/pelican-http-headers.md says that caches and origins alike send
    the trailer. XRootD's origins do (XrdHttpReq.cc); nothing in
    Pelican's origin_serve/ does, so the row fails at a native origin.
    The origins are asked only if they take direct clients."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    base = f"{test.base}/origin-transfer-status"
    data = os.urandom(STATUS_SIZE)
    errors = upload_each(test, base, {"object": data})
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    for origin in test.origins:
        why = read_status(test, origin.url, f"{base}/object", data, None)
        if why:
            problems.append(f"{origin.svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.origins)} origin(s): '{STATUS_OK}'"


def client_failed(
    test: Test, cache: common.Cache, rel: str, name: str
) -> tuple[Optional[str], str]:
    """`pelican object get --cache` of rel in /protected-a through cache
    alone, into a directory of its own, name: it must fail, by something
    other than a refusal, and leave nothing. What is wrong, or None, and
    how it failed."""
    directory = os.path.join(test.tmp, name)
    os.makedirs(directory)
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        cache.url,
        "--token",
        test.token_file,
        f"{test.fed.url}/protected-a/{rel}",
        os.path.join(directory, "object"),
    )
    said = out + err
    if code == 0:
        return "`pelican object get` succeeded", ""
    if os.listdir(directory):
        return f"`pelican object get` failed, but left {sorted(os.listdir(directory))}", ""
    if transfers.refused(said):
        return f"`pelican object get` was refused: {last_line(said)}", ""
    found = re.search(r"Error code (\d{4})", said)
    return None, f"Error code {found.group(1)}" if found else f"exit {code}"


def plugin_failed(
    test: Test, cache: common.Cache, rel: str, name: str
) -> tuple[Optional[str], str]:
    """stash_plugin's download of rel in /protected-a through cache alone,
    into a directory of its own, name: it must say that the transfer
    failed, and leave nothing; and, unless it found no such object (as
    where the director, asking every origin, found none), that the
    failure may be retried, since it came after the transfer began. What
    is wrong, or None, and how it failed."""
    directory = os.path.join(test.tmp, name)
    os.makedirs(directory)
    url = f"{test.fed.url}/protected-a/{rel}"
    passed, said = plugin(test, name, [url], directory, upload=False, cache=cache.url)
    if passed:
        return "stash_plugin succeeded", ""
    if "TransferSuccess = false" not in said:
        return f"stash_plugin wrote no ad of the failure: {last_line(said)}", ""
    if os.listdir(directory):
        return f"stash_plugin failed, but left {sorted(os.listdir(directory))}", ""
    kind = transfers.combine(
        transfers.classify(line) for line in transfers.failure_lines("plugin", said)
    )
    if kind == "not found":
        return None, "not found"
    if not re.search(r"TransferRetryable\s*=\s*true", said):
        return "stash_plugin's ad does not say TransferRetryable = true", ""
    return None, "retryable"


def unrepairable(
    test: Test, cache: common.Cache, rel: str, data: bytes, token: str, found: Findings
) -> None:
    """transfer_status_failure at one V2 cache, of rel, which holds data:
    token is the cache's admin's. Raises NoChaos if it has no chaos API."""
    why = test.get(cache.url, rel, data, None)
    if why:
        found.fail(f"{cache.svc}: before spoiling: {why}")
        return
    if not common.wait_for(functools.partial(holds_all, test, cache, rel), 10, interval=1):
        found.fail(f"{cache.svc}: no Age after a whole read, so it does not hold it all")
        return
    try:
        spoil(
            test, cache, token, rel, "corrupt", block=str(FAILED_BLOCK), bytes=str(blocks.TAG)
        )
    except ValueError as e:
        found.fail(str(e))
        return
    for origin in test.origins:
        error = stores.delete(origin, "protected-a", rel, test.token)
        if error:
            found.fail(error)
            return
    answer = web.request(
        "GET",
        f"{cache.url}/protected-a/{rel}",
        token=test.token,
        headers=ASK_STATUS,
        timeout=120,
    )
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
    found.note(
        f"{cache.svc}: {how}; the client failed ({client}), the plugin too ({plugin_kind})"
    )


def transfer_status_failure(test: Test) -> tuple[str, str]:
    """Each federated V2 cache: once it holds an object of its own whole,
    its chaos API corrupts block FAILED_BLOCK in its store (see spoil()),
    and every origin's copy is removed, so that it cannot repair the
    block, which it otherwise would. A GET asked for the transfer's
    status must then end with a trailer that reports a failure (Pelican
    sends `500: ...`), since the headers, 200, went out before the cache
    read the block; or, as where the cache revalidates first, fail before
    the body. Through that cache alone, `pelican object get` must then
    fail, by something other than a refusal, and leave nothing; and so
    must stash_plugin, whose ad must say the failure may be retried,
    unless it found no such object (plugin_failed()). A cache that serves
    the object intact never read the block, as where it holds it in
    memory, and leaves the row inconclusive."""
    caches = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no federated V2 cache, whose chaos API alone can spoil a block"
    base = f"{test.base}/transfer-failure"
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for cache in caches:
        try:
            unrepairable(
                test,
                cache,
                f"{base}/{cache.svc}",
                objects[cache.svc],
                admin_token(test, cache),
                found,
            )
        except NoChaos as e:
            return SKIP, f"{e}; Cache.EnableChaosAPI is in Pelican v7.27.0 and v26.0.0-rc.0 on"
    return found.verdict()


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


def server_digests(
    test: Test, url: str, origin: bool, relays: bool = False
) -> tuple[list[str], set[str]]:
    """What is wrong with the digests that url's server reports of the
    objects of DIGEST_SIZES in the `whole` and `ranged` sets: asked by
    HEAD for each of digests.ALGORITHMS in turn, and by GET for crc32c,
    the client's default. An origin must report each of those its
    storage holds for the object (test.digests, where
    stores.has_digests() says so for its size and how it was uploaded);
    so must a server that relays the origins' (a V2 cache), of the
    `whole` set, which it holds, if at all, as it read it whole. Also
    the algorithms it reports rightly."""
    problems: list[str] = []
    given: set[str] = set()
    for set_name in ("whole", "ranged"):
        for size in DIGEST_SIZES:
            rel, data = test.rel(set_name, size), test.data[(set_name, size)]
            wrong, right = reported(test, url, rel, data, digests.ALGORITHMS)
            problems += [f"{set_name}/{size}: {w}" for w in wrong]
            given |= right
            lacking = sorted(set(test.digests) - right)
            must = (origin or (relays and set_name == "whole")) and stores.has_digests(
                test.fed, size, SETS[set_name]
            )
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
    stores.ORIGIN_DIGESTS of each object, but for those its storage cannot
    (stores.digests()); so must a federated V2 cache of the `whole` set,
    since it relays the origins' (digest_relay()). An object of the
    `ranged` set that a V2 cache first read through a range comes with
    none (digest_range_first()), so of it, as of any object at an XRootD
    or site-local cache, a cache need report none. The note names the
    servers that report none. The origins are asked only if they take
    direct clients."""
    if why := test.not_uploaded(True, False):
        return FAIL, why
    servers = [(o.svc, o.url, True, False) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url, False, c.kind == "v2" and not c.site_local) for c in test.caches]
    problems: list[str] = []
    silent: list[str] = []
    for svc, url, origin, relays in servers:
        wrong, given = server_digests(test, url, origin, relays)
        problems += [f"{svc}: {w}" for w in wrong]
        if not given:
            silent.append(svc)
    if problems:
        return FAIL, first(problems)
    notes = [f"{len(servers)} server(s)"]
    if silent:
        notes.append(f"no digest from {', '.join(silent)}")
    if test.no_digests:
        notes.append(test.no_digests)
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
    says, and after each, check that origin serves it, and reports each
    digest its storage can (test.digests) of it: what was wrong, and how
    many versions there were."""
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
        wrong, right = reported(test, origin.url, rel, data, test.digests)
        problems += [f"after {what}: {w}" for w in wrong]
        if right != set(test.digests):
            problems.append(f"after {what}, it reports only {sorted(right)}")
    return problems, len(steps)


def digest_overwrite(test: Test) -> tuple[str, str]:
    """Each origin, once it has reported the digests of an object of its
    own: after a PUT of another version, at once, and in a store on disk,
    after a write there a second later, which it must notice (a native
    origin keeps digests by the file's modification time, to the second;
    origin_serve/checksum.go). Each time, the digests it reports must be
    those of the version it serves, and its ETag, if it sends one, must
    differ from every earlier version's, since by it a cache tells that an
    object has changed."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    if not test.digests:
        return SKIP, test.no_digests or "the origins report no digest"
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


# How the client's transfer statistics (--transfer-stats) name each
# checksum algorithm (ChecksumType in Pelican's client/handle_http.go).
CLIENT_ALGORITHMS = {0: "md5", 1: "crc32c", 2: "crc32", 3: "sha"}
# How long a V2 cache may take to record the origins' digests of an object
# it has read whole: it asks an origin for them once the body has come.
DIGEST_WAIT = 10


def relayed(answer: web.Response, data: bytes, want: tuple[str, ...]) -> Optional[str]:
    """What is wrong with the digests in answer, to a HEAD or GET of an
    object holding data: it must succeed; every digest it reports must be
    the object's; and it must report each of want. None if nothing."""
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


def relay_problem(test: Test, url: str, data: bytes, method: str) -> Optional[str]:
    """What is wrong with what url's server reports of the digests of the
    object there, which holds data, to method (HEAD or GET), asked by
    Want-Digest for each of stores.ORIGIN_DIGESTS in turn: it must report
    those the origins' storage does (test.digests; see relayed()), and a
    GET must bring the object. None if nothing."""
    for algorithm in stores.ORIGIN_DIGESTS:
        answer = web.request(
            method, url, token=test.token, headers={"Want-Digest": algorithm}, timeout=120
        )
        why = relayed(answer, data, test.digests)
        if not why and method == "GET" and answer.body != data:
            why = f"{len(answer.body)} bytes, not the object"
        if why:
            return f"{method}, for {algorithm}: {why}"
    return None


def relayed_soon(test: Test, url: str, data: bytes) -> Optional[str]:
    """relay_problem() for HEAD, asked again for DIGEST_WAIT seconds while
    it finds one."""
    last: list[Optional[str]] = [None]

    def right() -> bool:
        last[0] = relay_problem(test, url, data, "HEAD")
        return last[0] is None

    common.wait_for(right, DIGEST_WAIT, interval=1)
    return last[0]


def stats_checksums(path: str) -> tuple[dict[str, bytes], str]:
    """The checksums that the client's --transfer-stats file says a server
    gave it, by algorithm (digests.ALGORITHMS' names); or nothing, and
    why."""
    try:
        with open(path, encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"its --transfer-stats file could not be read: {e}"
    checksums: dict[str, bytes] = {}
    for results in common.as_array(found) or []:
        for result in common.as_array(results) or []:
            entry = common.as_object(result) or {}
            for item in common.as_array(entry.get("serverChecksums")) or []:
                info = common.as_object(item) or {}
                algorithm = info.get("Algorithm")
                name = CLIENT_ALGORITHMS.get(algorithm) if isinstance(algorithm, int) else None
                try:
                    value = base64.b64decode(str(info.get("Value") or ""), validate=True)
                except binascii.Error:
                    continue
                if name:
                    checksums[name] = value
    return checksums, ""


def client_digests(test: Test, cache: common.Cache, rel: str, data: bytes) -> Optional[str]:
    """`pelican object get --cache` of rel in /protected-a, which holds
    data, through cache alone: it must bring the object, and its transfer
    statistics show the digests that it then asked the cache for, which
    must be the object's, and include each of test.digests. What is
    wrong, or None."""
    name = f"relay-get-{cache.svc}"
    directory = os.path.join(test.tmp, name)
    os.makedirs(directory)
    target, stats = os.path.join(directory, "object"), os.path.join(test.tmp, f"{name}.json")
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        cache.url,
        "--token",
        test.token_file,
        "--transfer-stats",
        stats,
        f"{test.fed.url}/protected-a/{rel}",
        target,
    )
    if code != 0:
        return f"`pelican object get`: exit {code}: {last_line(err or out)}"
    if common.sha256(target) != common.digest(data):
        return "`pelican object get` did not bring the object"
    got, why = stats_checksums(stats)
    if why:
        return why
    for algorithm, value in got.items():
        if value != digests.compute(algorithm, data):
            return f"the client got a {algorithm} from it that is not the object's"
    lacking = [a for a in test.digests if a not in got]
    if lacking:
        return (
            f"the client got no {', '.join(lacking)} from it"
            + f" (its --transfer-stats lists {', '.join(sorted(got)) or 'none'})"
        )
    return None


def relayed_at(
    test: Test, cache: common.Cache, rel: str, data: bytes
) -> tuple[Optional[str], str]:
    """digest_relay at one V2 cache, of rel, which holds data: what is
    wrong, or None, and a note."""
    url = f"{cache.url}/protected-a/{rel}"
    why = relay_problem(test, url, data, "HEAD")
    if why:
        return f"cold, {why}", ""
    answer = web.request(
        "GET", url, token=test.token, headers={"Want-Digest": test.digests[0]}, timeout=120
    )
    if answer.status != 200 or answer.body != data:
        return f"a cold GET: {answer.describe()}, or not the object", ""
    wrong, _ = digests.check(answer.header("Digest"), data)
    if wrong:
        return f"a cold GET: {wrong[0]}", ""
    note = "a cold GET with a Digest" if answer.header("Digest") else "a cold GET without one"
    why = relayed_soon(test, url, data)
    if why:
        return f"warm, {why}", note
    why = relay_problem(test, url, data, "GET")
    if why:
        return f"warm, {why}", note
    return client_digests(test, cache, rel, data), note


def digest_relay(test: Test) -> tuple[str, str]:
    """Each federated V2 cache relays the origins' digests of an object of
    its own, as an RFC 3230 Digest (local_cache/persistent_cache_api.go
    in Pelican), so that a client can check what it downloads through a
    cache: a cold HEAD, from the origin's answer, and once the cache has
    read the object whole, a HEAD and a GET, from what it recorded, must
    each report the digests that the origins' storage holds
    (test.digests), each the object's, whatever Want-Digest asks for
    (relay_problem()). A cold GET may come before the cache knows them,
    and the note says whether it carried them. Then `pelican object get
    --cache` must get them from the cache (client_digests()). Over XRootD
    origins, which answer a Want-Digest of several algorithms with one,
    the cache, which asks for all at once, relays only that one to a
    cold HEAD, and the row fails there. A site-local cache reads through
    another cache, and is left to the sitelocal suite."""
    caches = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no federated V2 cache; an XRootD cache need relay no digest"
    if not test.digests:
        return SKIP, test.no_digests or "the origins report no digest"
    base = f"{test.base}/digest-relay"
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
    for cache in caches:
        why, note = relayed_at(test, cache, f"{base}/{cache.svc}", objects[cache.svc])
        if why:
            problems.append(f"{cache.svc}: {why}")
        if note:
            notes.append(f"{cache.svc}: {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join([f"{', '.join(test.digests)} relayed"] + notes)


def digest_range_first(test: Test) -> tuple[str, str]:
    """Each federated V2 cache relays the origins' digests (relay_problem())
    of an object of its own that it first read through a range, of its
    second block: by HEAD once it has, and again once it has read the
    object whole, and holds it all (it sends Age). A V2 cache fills a
    range of an object it lacks block by block, and records only what
    the origin's stat says of it (initObjectFromStat in
    local_cache/persistent_cache.go), which asks for no digest; so it
    relays none of that object, and the row fails, on main and
    v26.0.0-rc.3 alike."""
    caches = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no federated V2 cache; an XRootD cache need relay no digest"
    if not test.digests:
        return SKIP, test.no_digests or "the origins report no digest"
    base = f"{test.base}/digest-range-first"
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    for cache in caches:
        rel, data = f"{base}/{cache.svc}", objects[cache.svc]
        url = f"{cache.url}/protected-a/{rel}"
        request = ((blocks.BLOCK, 2 * blocks.BLOCK - 1),)
        why = test.get(cache.url, rel, data, request)
        if why:
            problems.append(f"{cache.svc}: the range: {why}")
            continue
        why = relay_problem(test, url, data, "HEAD")
        if why:
            problems.append(f"{cache.svc}: after the range, {why}")
        why = test.get(cache.url, rel, data, None)
        if why:
            problems.append(f"{cache.svc}: the whole object: {why}")
            continue
        if not common.wait_for(functools.partial(holds_all, test, cache, rel), 10, interval=1):
            problems.append(
                f"{cache.svc}: no Age after a whole read, so it does not hold it all"
            )
            continue
        why = relayed_soon(test, url, data)
        if why:
            problems.append(f"{cache.svc}: once it held it all, {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(caches)} V2 cache(s): {', '.join(test.digests)} relayed"


# The extended attributes in which a posixv2 origin keeps an object's
# digests, each with the file's modification time, to the second, in
# XRootD's format (origin_serve/checksum.go in Pelican).
CHECKSUM_XATTRS = (
    "user.XrdCks.md5",
    "user.XrdCks.crc32c",
    "user.XrdCks.crc32",
    "user.XrdCks.sha1",
)
# What a V2 cache asks an origin for (client.KnownChecksumTypes in
# Pelican), so that every digest that the origin then reports is stale.
EVERY_DIGEST = "md5,crc32c,crc32,sha"


def lacks_xattrs(path: str) -> Optional[list[str]]:
    """Which of CHECKSUM_XATTRS the file at path lacks; None if nothing
    here can read extended attributes (os.getxattr is Linux's alone)."""
    getxattr = vars(os).get("getxattr")
    if getxattr is None:
        return None
    lacking: list[str] = []
    for name in CHECKSUM_XATTRS:
        try:
            getxattr(path, name)
        except OSError:
            lacking.append(name)
    return lacking


def stale_trap(
    test: Test, origin: common.Origin, rel: str, a: bytes, b: bytes
) -> tuple[Optional[str], Optional[str]]:
    """At origin, which holds a at rel in /protected-a: have it record a's
    digests (a HEAD with Want-Digest of EVERY_DIGEST), then write b over
    a in its store, and give the file back its modification time, so
    that the origin serves b and reports a's digests. What went wrong,
    and why the trap could not be set (where the origin keeps no digest
    in the store, or noticed), each or None."""
    target = stores.url(origin, "protected-a", rel)
    answer = web.request(
        "HEAD", target, token=test.token, headers={"Want-Digest": EVERY_DIGEST}
    )
    if answer.status != 200:
        return f"{origin.svc}: a HEAD with Want-Digest: {answer.describe()}", None
    path = f"{origin.store_of('protected-a')}/{rel}"
    lacking = lacks_xattrs(path)
    if lacking is None:
        return None, "nothing here can read extended attributes"
    if lacking:
        return None, (
            f"{origin.svc} kept no {lacking[0]} on framework/var/{path}, as where its"
            + " filesystem has no extended attributes, so it computes digests afresh"
        )
    was = os.stat(path)
    error = stores.write_file(origin.store_of("protected-a"), rel, b)
    if error:
        return error, None
    try:
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    except OSError as e:
        return f"restoring framework/var/{path}'s modification time: {e.strerror}", None
    answer = web.request(
        "HEAD", target, token=test.token, headers={"Want-Digest": EVERY_DIGEST}
    )
    _, stale = digests.check(answer.header("Digest"), a)
    if not stale:
        return None, (
            f"{origin.svc} reports no digest of the overwritten object"
            + f" (Digest '{answer.header('Digest')}'), so none is stale"
        )
    return None, None


def stale_digest(test: Test, cache: common.Cache, rel: str, a: bytes, found: Findings) -> None:
    """stale_origin_digest at one V2 cache, of rel, which every origin
    holds as a."""
    b = bytes(x ^ 0xFF for x in a)
    for origin in test.origins:
        why, doubt = stale_trap(test, origin, rel, a, b)
        if why:
            found.fail(f"{cache.svc}: {why}")
            return
        if doubt:
            found.doubt(doubt)
            return
    url = f"{cache.url}/protected-a/{rel}"
    answer = web.request("GET", url, token=test.token, headers=ASK_STATUS, timeout=120)
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
    last: list[Optional[int]] = [None]

    def dropped() -> bool:
        probe = web.request(
            "GET", url, token=test.token, headers={"Cache-Control": "only-if-cached"}
        )
        last[0] = probe.status
        return probe.status == 504

    if not common.wait_for(dropped, 10, interval=1):
        got = "no response" if last[0] is None else f"HTTP {last[0]}"
        found.fail(f"{cache.svc}: then, only-if-cached got {got}, not 504: it kept the object")
        return
    why, client = client_failed(test, cache, rel, f"stale-get-{cache.svc}")
    if why:
        found.fail(f"{cache.svc}: {why}")
        return
    found.note(f"{cache.svc}: {how}; kept nothing; the client failed ({client})")


def stale_origin_digest(test: Test) -> tuple[str, str]:
    """Each federated V2 cache, reading an object of its own that the
    origins serve with digests that are not its own, must neither pass
    it off as good nor keep it (Pelican's
    TestTransferStatus_Failure_OriginChecksumMismatch). A posixv2 origin
    keeps an object's digests in its file's extended attributes, with
    the file's modification time, to the second, and reports them while
    that is unchanged; so each origin records the digests of one
    version, and the test then writes another of the same size over it
    in the store, and restores its time (stale_trap()). A GET through
    the cache, asked for the transfer's status, must then report a
    failure in its trailer, or fail; a GET with only-if-cached must get
    504, within 10 seconds; and `pelican object get --cache` must fail,
    by something other than a refusal, and leave nothing. Where the
    store keeps no extended attributes, or the origin notices the
    overwrite, the row is inconclusive. The origins are first asked
    directly, so it skips where they take no direct clients."""
    if test.fed.origin_variant != "posixv2":
        return SKIP, (
            "only a posixv2 origin keeps digests by modification time in its store,"
            + f" not {test.fed.origin_variant}"
        )
    caches = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not caches:
        return SKIP, "no federated V2 cache, which alone checks the origins' digests"
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients, so no origin can be asked first"
    base = f"{test.base}/stale-digest"
    objects = {c.svc: os.urandom(STATUS_SIZE) for c in caches}
    errors = upload_each(test, base, objects)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for cache in caches:
        stale_digest(test, cache, f"{base}/{cache.svc}", objects[cache.svc], found)
    return found.verdict()


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


def xattr_digests(path: str) -> Optional[dict[str, Optional[bytes]]]:
    """The digest that the file at path keeps in each of DIGEST_XATTRS, by
    algorithm, or None for one it lacks; None if nothing here can read
    extended attributes (os.getxattr is Linux's alone)."""
    getxattr = vars(os).get("getxattr")
    if getxattr is None:
        return None
    found: dict[str, Optional[bytes]] = {}
    for algorithm, name in DIGEST_XATTRS.items():
        try:
            raw: bytes = getxattr(path, name)
        except OSError:
            found[algorithm] = None
            continue
        length = raw[XRDCKS_LENGTH] if len(raw) > XRDCKS_LENGTH else 0
        found[algorithm] = raw[XRDCKS_LENGTH + 1 : XRDCKS_LENGTH + 1 + length]
    return found


def digest_fields(answer: web.Response) -> list[str]:
    """The values of each Digest field of answer."""
    return [v for k, v in answer.headers if k.lower() == "digest"]


def defaults_at(
    test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings
) -> None:
    """digest_defaults's HEADs of rel in /protected-a at origin, which
    holds data. Each asks for digests in its own way: with no Want-Digest,
    with WANT_BOTH, and with WANT_WEIGHTED. Every digest it reports must be
    the object's, in one Digest field, which is all the Pelican client
    reads. Unasked, a native origin must report crc32c, the first of
    Origin.DefaultChecksumTypes, which the framework leaves at its
    default. Asked for both, a native origin must report each, and an
    XRootD origin, which reports the one it prefers, at least one. Asked
    with q-values, every origin must report at least one. Each only of
    what its storage reports (test.digests)."""
    native = test.fed.native_origins
    asks = (
        ("unasked", None, ("crc32c",) if native else (), False),
        ("asked for both", WANT_BOTH, test.digests if native else (), not native),
        ("weighed", WANT_WEIGHTED, (), True),
    )
    target = stores.url(origin, "protected-a", rel)
    said: list[str] = []
    for label, want, must, some in asks:
        headers = {"Want-Digest": want} if want else None
        answer = web.request("HEAD", target, token=test.token, headers=headers)
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


def kept_digests(
    test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings
) -> bool:
    """Whether a posixv2 origin keeps the digests of rel in /protected-a,
    which holds data, in its file's extended attributes, each the
    object's, once it has reported them. Where they cannot be read, a
    doubt; where one is wrong or missing, a problem."""
    path = f"{origin.store_of('protected-a')}/{rel}"
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


def digests_rewritten(
    test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings
) -> None:
    """At a posixv2 origin, of rel in /protected-a, which holds data, and
    whose digests it has recorded: write bytes of the same length over it
    in the store, and give the file back its modification time. The origin
    keeps digests by that time, to the second (origin_serve/checksum.go),
    so it may report the old version's, which is accepted, as XRootD does
    the same; or the new bytes', if it noticed. Any other digest, or none,
    is a problem. The note says which."""
    target = stores.url(origin, "protected-a", rel)
    path = f"{origin.store_of('protected-a')}/{rel}"
    rewrite = os.urandom(len(data))
    was = os.stat(path)
    error = stores.write_file(origin.store_of("protected-a"), rel, rewrite)
    if error:
        found.fail(f"{origin.svc}: {error}")
        return
    try:
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    except OSError as e:
        found.fail(f"restoring framework/var/{path}'s modification time: {e.strerror}")
        return
    answer = web.request("HEAD", target, token=test.token, headers={"Want-Digest": WANT_BOTH})
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


def xattrs_at(
    test: Test, origin: common.Origin, rel: str, data: bytes, found: Findings
) -> None:
    """digest_defaults at a posixv2 origin, of rel in /protected-a, which
    holds data, and whose digests it has just reported: its file's
    extended attributes must hold them (kept_digests()). A PUT of another
    version must clear them, or replace them with its digests, which the
    origin must then report. Last, digests_rewritten()."""
    if not kept_digests(test, origin, rel, data, found):
        return
    if test.writes:
        new = os.urandom(len(data) + 7)
        error = stores.put(origin, "protected-a", rel, new, test.token)
        if error:
            found.fail(f"{origin.svc}: {error}")
            return
        path = f"{origin.store_of('protected-a')}/{rel}"
        for algorithm, value in (xattr_digests(path) or {}).items():
            if value is not None and value != digests.compute(algorithm, new):
                found.fail(
                    f"{origin.svc}: after a PUT, framework/var/{path}'s"
                    + f" {DIGEST_XATTRS[algorithm]} is still the old version's"
                )
        data = new
        answer = web.request(
            "HEAD",
            stores.url(origin, "protected-a", rel),
            token=test.token,
            headers={"Want-Digest": WANT_BOTH},
        )
        wrong, right = digests.check(answer.header("Digest"), data)
        if answer.status != 200 or wrong or set(right) != set(test.digests):
            found.fail(
                f"{origin.svc}: after a PUT, a HEAD: {answer.describe()},"
                + f" Digest '{answer.header('Digest')}', not the new version's"
            )
            return
    digests_rewritten(test, origin, rel, data, found)


def digest_defaults(test: Test) -> tuple[str, str]:
    """Each origin, of an object of its own: the digests it reports
    unasked, asked for two, and asked with q-values (defaults_at()). On a
    posixv2 origin, it must then keep them in its file's extended
    attributes, and follow a PUT; and after a rewrite in the store that
    keeps the file's modification time, it may report the old digests,
    but no others (xattrs_at()). Where the store's extended attributes
    cannot be read, the row is inconclusive. The origins are asked only
    if they take direct clients."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    if not test.digests:
        return SKIP, test.no_digests or "the origins report no digest"
    base = f"{test.base}/digest-defaults"
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    if errors:
        return FAIL, errors[0]
    found = Findings()
    for origin in test.origins:
        rel, data = f"{base}/{origin.svc}", os.urandom(3 * blocks.BLOCK + 5)
        error = stores.seed(origin, "protected-a", rel, data, test.token, test.caps)
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


# The sizes whose conditional requests `conditional` sends: within a block,
# and past an XRootD block.
CONDITIONAL_SIZES = (blocks.BLOCK + 1, blocks.XROOTD_BLOCK + 1)
# A time before any object here was written.
LONG_AGO = "Thu, 01 Jan 2015 00:00:00 GMT"


def conditions(etag: str, modified: str) -> list[tuple[str, str, bool]]:
    """The conditions to send for an object whose ETag and Last-Modified
    these are (RFC 9110): each header, its value, and whether it holds for
    the object as it is, in which case a server may answer 304. Without
    an ETag or a Last-Modified, the conditions on it are left out."""
    found: list[tuple[str, str, bool]] = []
    if etag:
        strong = etag[2:] if etag.startswith("W/") else etag
        found += [
            ("If-None-Match", etag, True),
            ("If-None-Match", f'"nope", {etag}', True),
            ("If-None-Match", f"W/{strong}", True),
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
    test: Test, svc: str, url: str, rel: str, data: bytes
) -> tuple[list[str], set[str], set[str]]:
    """Send each of conditions() for rel in /protected-a, which holds data,
    to url's server: what is wrong, the headers it honored (304), and
    those of ETag and Last-Modified it never sent."""
    target = f"{url}/protected-a/{rel}"
    answer = web.request("GET", target, token=test.token)
    if answer.status != 200 or answer.body != data:
        return [f"{svc}: {rel}: {answer.describe()}, or not the object"], set(), set()
    etag, modified = answer.header("ETag"), answer.header("Last-Modified")
    unsent = {
        name for name, value in (("ETag", etag), ("Last-Modified", modified)) if not value
    }
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


def conditional(test: Test) -> tuple[str, str]:
    """Each server, asked for objects of the `whole` set with a condition
    (RFC 9110, which lets a server ignore a condition on a GET, but never
    answer 304 to one that does not hold): with If-None-Match naming the
    ETag it sent, among others, weakened (W/), or as *, and with
    If-Modified-Since at the Last-Modified it sent or later, it may answer
    304, or 200 with the whole object; with If-None-Match naming another
    ETag, or If-Modified-Since long ago, it must answer 200 with the whole
    object. The origins are asked only if they take direct clients. The
    note says what each server honored, and what it never sent."""
    if why := test.not_uploaded(True):
        return FAIL, why
    servers = [(o.svc, o.url) for o in test.origins] if test.direct else []
    servers += [(c.svc, c.url) for c in test.caches]
    problems: list[str] = []
    notes: list[str] = []
    for svc, url in servers:
        honored: set[str] = set()
        unsent: set[str] = set()
        for size in CONDITIONAL_SIZES:
            wrong, right, missing = conditional_problems(
                test, svc, url, test.rel("whole", size), test.data[("whole", size)]
            )
            problems += wrong
            honored |= right
            unsent |= missing
        note = f"{svc} honors {', '.join(sorted(honored)) or 'none'}"
        if unsent:
            note += f", sends no {', '.join(sorted(unsent))}"
        notes.append(note)
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


def cas_put(
    test: Test, origin: common.Origin, rel: str, data: bytes, header: str, value: str
) -> web.Response:
    """PUT data to rel in /protected-a at origin, on the condition header:
    value."""
    return web.request(
        "PUT",
        stores.url(origin, "protected-a", rel),
        token=test.token,
        upload=data,
        headers={header: value},
    )


def cas_problems(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], str]:
    """conditional_put at one origin, of rel: what is wrong, and a note."""
    v1, v2, v3 = (os.urandom(4096) for _ in range(3))
    problems: list[str] = []

    def check(
        what: str, answer: web.Response, allowed: bool, where: str, want: Optional[bytes]
    ) -> None:
        """The write what must have succeeded if allowed, and otherwise
        been refused (412); either way the store must then hold want at
        where (None: nothing)."""
        if allowed and not answer.ok:
            problems.append(f"{what}: {answer.describe()}")
        elif not allowed and answer.status != 412:
            problems.append(f"{what}: {answer.describe()}, not 412")
        have = stores.held(origin, "protected-a", where, test.token)
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
    etag = web.request("HEAD", stores.url(origin, "protected-a", rel), token=test.token).header(
        "ETag"
    )
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


def conditional_put(test: Test) -> tuple[str, str]:
    """Each origin that takes writes from direct clients: a PUT with
    If-None-Match: * creates an object, and a second one is refused (412)
    and changes nothing; one with If-Match naming the object's ETag
    replaces it, and one naming the ETag it replaced, or another, is
    refused and changes nothing; and one with If-Match: * of an object
    that does not exist is refused, and stores nothing (RFC 9110). A
    server that ignores a condition on a PUT would let two writers
    overwrite each other unawares."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    if not test.writes:
        return SKIP, NO_WRITES
    base = f"{test.base}/conditional-put"
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        wrong, note = cas_problems(test, origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc} {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


# etag-agreement's object, and the range it asks for with If-Range.
ETAG_SIZE = 3 * blocks.BLOCK + 5
IF_RANGE = (100, blocks.BLOCK + 99)


def unweakened(etag: str) -> str:
    """etag without a weak prefix (W/), as a weak comparison sees it."""
    return etag[2:] if etag.startswith("W/") else etag


def method_etags(test: Test, target: str, data: bytes, problems: list[str]) -> dict[str, str]:
    """The ETag that each of GET, HEAD, and PROPFIND (Depth 0, its
    getetag) reports of target, which holds data, by method; "" for one
    that reports none. What goes wrong goes in problems."""
    found: dict[str, str] = {}
    answer = web.request("GET", target, token=test.token)
    if answer.status != 200 or answer.body != data:
        problems.append(f"a GET: {answer.describe()}, or not the object")
    found["GET"] = answer.header("ETag")
    answer = web.request("HEAD", target, token=test.token)
    if answer.status != 200:
        problems.append(f"a HEAD: {answer.describe()}")
    found["HEAD"] = answer.header("ETag")
    answer = webdav.propfind(target, "0", test.token)
    found["PROPFIND"] = ""
    try:
        if answer.status != 207:
            raise ValueError(f"{answer.describe()}, not 207")
        entries = webdav.parse(answer.body)
        if len(entries) != 1:
            raise ValueError(f"{len(entries)} entries, not 1")
        found["PROPFIND"] = entries[0].etag or ""
    except ValueError as e:
        problems.append(f"a PROPFIND: {e}")
    return found


def etag_conditions(
    test: Test,
    origin: common.Origin,
    rel: str,
    data: bytes,
    etag: str,
    source: str,
    problems: list[str],
) -> tuple[bytes, Optional[str]]:
    """Conditions on rel in /protected-a at origin, which holds data and
    has etag, a strong one, which source reported: a GET with If-Match,
    which must serve the object; a GET of IF_RANGE with If-Range, which
    must serve the range (206); and, where /protected-a takes writes, a
    PUT of another version with If-Match, which must replace it. What goes
    wrong goes in problems. Returns what the object holds afterward, and
    the ETag of the PUT's answer, if it replaced it."""
    target = stores.url(origin, "protected-a", rel)
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
    held = stores.held(origin, "protected-a", rel, test.token)
    now = new if answer.ok else data
    if held != common.digest(now):
        problems.append(f"after a PUT {what}, the store does not hold what it should")
    return now, (answer.header("ETag") if answer.ok else None)


def etag_problems(test: Test, origin: common.Origin, rel: str) -> tuple[list[str], str]:
    """etag_agreement at one origin, of rel: what is wrong, and a note."""
    target = stores.url(origin, "protected-a", rel)
    data = os.urandom(ETAG_SIZE)
    problems: list[str] = []
    tags: dict[str, str] = {}
    if test.writes:
        answer = web.request("PUT", target, token=test.token, upload=data)
        if not answer.ok:
            return [f"a PUT: {answer.describe()}"], ""
        tags["PUT"] = answer.header("ETag")
    elif error := stores.seed(origin, "protected-a", rel, data, test.token, test.caps):
        return [error], ""
    tags.update(method_etags(test, target, data, problems))
    sent = {method: tag for method, tag in tags.items() if tag}
    if len({unweakened(tag) for tag in sent.values()}) > 1:
        problems.append(
            "one version, several ETags: " + ", ".join(f"{m} {t}" for m, t in sent.items())
        )
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


def etag_agreement(test: Test) -> tuple[str, str]:
    """Each origin, of an object of its own, written by a PUT (or, where
    /protected-a takes no writes, put in its store): the ETags that the
    PUT's answer, a GET, a HEAD, and PROPFIND's getetag report must be
    one, since they name one version (RFC 9110, section 8.8.3), by which a
    cache revalidates and a writer compares and swaps. Then the ETags of
    GET and of PUT must each hold as conditions (etag_conditions()). The
    origins are asked only if they take direct clients. The note says
    which methods reported none."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    base = f"{test.base}/etag-agreement"
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        wrong, note = etag_problems(test, origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc}: {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


# How many PUTs conditional-edges races to create one object.
RACERS = 8


def race(test: Test, origin: common.Origin, rel: str) -> list[str]:
    """RACERS PUTs at once, each with If-None-Match: * and bytes of its
    own, of rel in /protected-a at origin, which does not exist: exactly
    one must succeed, and the rest be refused (412), and the store must
    then hold the winner's bytes. What is wrong."""
    bodies = [os.urandom(1 << 16) for _ in range(RACERS)]
    answers = [web.Response(None) for _ in range(RACERS)]

    def racer(n: int) -> Callable[[], Optional[str]]:
        def put() -> Optional[str]:
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
    other = [a for a in answers if not a.ok and a.status != 412]
    if other:
        problems.append(f"a racing PUT that lost: {other[0].describe()}, not 412")
    held = stores.held(origin, "protected-a", rel, test.token)
    if not any(held == common.digest(bodies[n]) for n in won):
        problems.append("after the racing PUTs, the store holds no winner's bytes")
    return problems


def edge_problems(test: Test, origin: common.Origin, base: str) -> tuple[list[str], str]:
    """conditional_edges at one origin, of objects under base: what is
    wrong, and a note."""
    rel = f"{base}/object"
    data = os.urandom(blocks.BLOCK + 1)
    error = stores.seed(origin, "protected-a", rel, data, test.token, test.caps)
    if error:
        return [error], ""
    target = stores.url(origin, "protected-a", rel)
    problems: list[str] = []
    for value in ('"nope"', "*"):
        answer = web.request(
            "GET",
            stores.url(origin, "protected-a", f"{base}/missing"),
            token=test.token,
            headers={"If-None-Match": value},
        )
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
        problems.append(
            f"a PUT with If-None-Match: {junk} over it: {answer.describe()}, not 412"
        )
    if stores.held(origin, "protected-a", rel, test.token) != common.digest(data):
        problems.append(
            f"after a PUT with If-None-Match: {junk}, the store holds another object"
        )
    problems += race(test, origin, f"{base}/raced")
    return problems, "GETs and PUTs"


def conditional_edges(test: Test) -> tuple[str, str]:
    """Each origin, of objects of its own (RFC 9110, section 13): a GET of
    an object that does not exist, with If-None-Match naming an ETag or *,
    must get 404, not 304; one with an invalid If-Modified-Since, which is
    ignored, the object; one with If-None-Match that does not hold, beside
    If-Modified-Since that would, the object, since If-None-Match decides;
    and one with If-Unmodified-Since, the object if it is after the
    object's Last-Modified, and otherwise 412. Where /protected-a takes
    writes, a PUT with If-None-Match: *, "junk" over the object must be
    refused (412), changing nothing, since * in a list still means any
    object; and of RACERS PUTs at once of a new object, each with
    If-None-Match: *, exactly one may succeed (race()). The origins are
    asked only if they take direct clients."""
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    base = f"{test.base}/conditional-edges"
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        errors = stores.make_dirs([origin], "protected-a", f"{base}/{origin.svc}", test.token)
        if errors:
            return FAIL, errors[0]
        wrong, note = edge_problems(test, origin, f"{base}/{origin.svc}")
        problems += [f"{origin.svc}: {w}" for w in wrong]
        notes.append(f"{origin.svc}: {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


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
        cache came back as (see blocks.which_version()). A whole read asks
        for a digest too (Want-Digest), which, if one comes, must be that
        version's: a stale one would fail the client's check of bytes that
        are right."""
        headers = {"Range": blocks.header(request)} if request else {"Want-Digest": "crc32c"}
        url = f"{cache.url}/protected-a/{self.base}/{key}"
        answer = web.request("GET", url, token=self.test.token, headers=headers, timeout=120)
        versions = {"v1": self.v1[key], "v2": self.v2[key]}
        version, why = blocks.which_version(
            versions, request, answer.status, answer.headers, answer.body
        )
        if version and request is None:
            wrong, _ = digests.check(answer.header("Digest"), versions[version])
            if wrong:
                return (
                    None,
                    f"served {version}, but with a Digest that is not its ({first(wrong)})",
                )
        return version, why

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
    (`-p origin-max-age`) at native origins, every origin must send it,
    and each cache, once its copy is stale, must serve v2, and only v2.
    Without it, and at XRootD origins, which send none, a cache decides
    for itself how long its copy stays fresh. The origins are asked only
    if they take direct clients."""
    kind = "range-first" if range_first else "cached"
    versions = Versions(test, f"{test.base}/overwrite-{kind}")
    errors = upload_each(test, versions.base, versions.v1)
    if errors:
        return FAIL, errors[0]
    control = test.fed.sent_cache_control
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
    if test.fed.origin_cache_control and not control:
        notes.append("XRootD origins send no Cache-Control")
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


def refused_through(test: Test, cache: common.Cache, rel: str, v1: bytes) -> Optional[str]:
    """write_through()'s PUT through an XRootD cache of rel in
    /protected-a, which every origin holds as v1: it must be refused, and
    change no origin; what went wrong, or None."""
    answer = web.request(
        "PUT", f"{cache.url}/protected-a/{rel}", token=test.token, upload=os.urandom(len(v1))
    )
    if answer.status is None or not 400 <= answer.status < 500:
        return (
            f"a PUT: {answer.describe()}, not a refusal, though an XRootD cache takes no writes"
        )
    if not held_everywhere(test, rel, v1):
        return "a PUT, refused, changed an origin"
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
    writes, the PUT must be refused. An XRootD cache takes no writes, so
    its PUT must be refused, and change no origin."""
    base = f"{test.base}/write-through"
    v1 = {c.svc: os.urandom(OVERWRITE_SIZE) for c in test.caches}
    errors = upload_each(test, base, v1)
    if errors:
        return FAIL, errors[0]
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
        if why:
            problems.append(f"{cache.svc}: {why}")
    if problems:
        return FAIL, first(problems)
    kinds = Counter(c.kind for c in test.caches)
    notes: list[str] = []
    if kinds["xrootd"]:
        notes.append(f"{kinds['xrootd']} XRootD cache(s) refused a PUT")
    if kinds["v2"] and not test.writes:
        notes.append(f"{kinds['v2']} V2 cache(s) refused a PUT")
    elif kinds["v2"] and not deletes:
        notes.append(f"{kinds['v2']} V2 cache(s) relayed a PUT; {cannot_delete}")
    elif kinds["v2"]:
        notes.append(f"{kinds['v2']} V2 cache(s) relayed a PUT and a DELETE")
    return PASS, "; ".join(notes)


# cache-evicted's objects: each this big, and enough of them to exceed a
# cache's high water mark by half.
EVICT_OBJECT = 8 << 20
# How long a cache may take to evict down to its high water mark, in
# seconds: its eviction runs every 10 seconds.
EVICT_WAIT = 60
# How far above the mark its object files may add up to: the cache charges
# itself each object's file as it makes it, and counts in the database
# rather than on disk.
EVICT_SLACK = 1 << 20
# How many of the objects read last must survive the evictions.
LRU_NEWEST = 3


def evict_object(run_id: str, n: int) -> bytes:
    """The bytes of cache-evicted's object n: made anew from the run and n
    each time, since holding every object would take as much memory as
    the caches' bound. Test data, so not a cryptographic generator."""
    return random.Random(f"{run_id}/{n}").randbytes(EVICT_OBJECT)  # nosec B311


def store_bytes(cache: common.Cache) -> Optional[int]:
    """How many bytes a V2 cache's object files take, in the store it
    mounts at /data (local_cache/storage.go in Pelican); None if it has
    no such store."""
    root = f"{cache_dir(cache)}/persistent-cache/objects"
    if not os.path.isdir(root):
        return None
    return sum(
        os.path.getsize(os.path.join(directory, name))
        for directory, _, files in os.walk(root)
        for name in files
    )


def cached(test: Test, cache: common.Cache, rel: str) -> Optional[bool]:
    """Whether a V2 cache holds all of rel, as a HEAD with only-if-cached
    says (504 if not); None if it says neither."""
    answer = web.request(
        "HEAD",
        f"{cache.url}/protected-a/{rel}",
        token=test.token,
        headers={"Cache-Control": "only-if-cached"},
    )
    return {200: True, 504: False}.get(answer.status or 0)


def reread_at(high: int, count: int) -> int:
    """After how many of cache_evicted's objects it reads object 0 again:
    as many as fit under the high water mark high, less one, so that the
    cache holds object 0 still, and evicts nothing yet."""
    fit = high // blocks.stored(EVICT_OBJECT) - 1
    return max(1, min(count - LRU_NEWEST - 1, fit))


def lru_order(
    test: Test, cache: common.Cache, base: str, count: int, again: int, was_held: Optional[bool]
) -> tuple[Optional[str], str]:
    """After cache_evicted's reads: the LRU_NEWEST objects read last must
    still be held, and at least two of the oldest (1 to again - 1) gone.
    Object 0, read again after again objects, while held (was_held), may
    be held or gone: the cache records an access for its LRU at most once
    in ten minutes (UpdateLRU's debounce in local_cache/database.go), so
    the read again may move it nowhere. What is wrong, or None, and a
    note of what the cache held."""
    found = {n: cached(test, cache, f"{base}/{n}") for n in range(count)}
    unsure = [n for n, held in found.items() if held is None]
    if unsure:
        return f"object {unsure[0]}: only-if-cached got neither 200 nor 504", ""
    lost = [n for n in range(count - LRU_NEWEST, count) if not found[n]]
    if lost:
        return f"object {lost[0]}, among the {LRU_NEWEST} read last, is gone", ""
    gone = [n for n in range(1, again) if not found[n]]
    if len(gone) < 2:
        return f"{len(gone)} of objects 1 to {again - 1}, the oldest, are gone, not several", ""
    then = {True: "held", False: "gone", None: "unknown"}[was_held]
    now = "held" if found[0] else "gone"
    return None, (
        f"{len(gone)} of objects 1 to {again - 1} gone; object 0, read again after"
        + f" {again} ({then} then), {now}"
    )


def evicted(
    test: Test, cache: common.Cache, base: str, count: int
) -> tuple[Optional[str], str]:
    """cache_evicted at one cache, whose store is bounded (test.fed.eviction):
    what went wrong, or None, and a note of what it held (see lru_order())."""
    high, low = test.fed.eviction[cache.svc]
    again = reread_at(high, count)
    was_held: Optional[bool] = None
    for n in range(count):
        if n == again:
            was_held = cached(test, cache, f"{base}/0")
            why = test.get(cache.url, f"{base}/0", evict_object(test.run, 0), None)
            if why:
                return f"object 0, read again: {why}", ""
        why = test.get(cache.url, f"{base}/{n}", evict_object(test.run, n), None)
        if why:
            return f"object {n}, cold: {why}", ""
    if store_bytes(cache) is None:
        return (
            f"framework/var/{cache_dir(cache)} has no persistent-cache/objects to measure",
            "",
        )

    def under() -> bool:
        return (store_bytes(cache) or 0) <= high + EVICT_SLACK

    if not common.wait_for(under, EVICT_WAIT, interval=2):
        return (
            f"holds {store_bytes(cache)} bytes {EVICT_WAIT}s after reading {count * EVICT_OBJECT},"
            f" above its high water mark of {high}"
        ), ""
    held = store_bytes(cache) or 0
    if held < low - EVICT_OBJECT:
        return (
            f"holds {held} bytes, below its low water mark of {low}, so it evicted too much",
            "",
        )
    why, note = lru_order(test, cache, base, count, again, was_held)
    if why:
        return why, note
    for n in range(count):
        data = evict_object(test.run, n)
        for request in (None, ((blocks.XROOTD_BLOCK, blocks.XROOTD_BLOCK + blocks.BLOCK - 1),)):
            why = test.get(cache.url, f"{base}/{n}", data, request)
            if why:
                what = blocks.header(request) if request else "whole"
                return f"object {n}, {what}, after the evictions: {why}", note
    target = os.path.join(test.tmp, f"evicted-{cache.svc}")
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        cache.url,
        "--token",
        test.token_file,
        f"{test.fed.url}/protected-a/{base}/0",
        target,
    )
    if code != 0:
        return (
            f"`pelican object get` of object 0 after the evictions: {last_line(err or out)}",
            note,
        )
    if common.sha256(target) != common.digest(evict_object(test.run, 0)):
        return "`pelican object get` of object 0 after the evictions: not the object", note
    return None, note


def cache_evicted(test: Test) -> tuple[str, str]:
    """Each V2 cache whose store is bounded (`-p cache-small`): a cold whole
    read of each of enough objects of EVICT_OBJECT bytes to exceed its
    high water mark by half, reading the first again once as many as fit
    under the mark have been read (reread_at()); then, within EVICT_WAIT
    seconds, its store must have come back under the mark, and not below
    its low one by more than an object; it must have evicted the oldest
    objects and kept the newest (lru_order()); and every object must
    still come back whole and in a range, as must the first by `pelican
    object get --cache`. The objects are one set, read cold by each
    cache. An XRootD cache's usage is its filesystem's, which the
    framework cannot bound, and is passed over."""
    caches = [c for c in test.caches if c.kind == "v2" and c.svc in test.fed.eviction]
    if not caches:
        return SKIP, "no V2 cache has a bounded store ('-p cache-small')"
    high = max(test.fed.eviction[c.svc][0] for c in caches)
    count = -(-3 * high // (2 * EVICT_OBJECT))
    base = f"{test.base}/evicted"
    errors = stores.make_dirs(test.origins, "protected-a", base, test.token)
    for n in range(count):
        if errors:
            break
        errors += stores.seed_everywhere(
            test.origins,
            "protected-a",
            f"{base}/{n}",
            evict_object(test.run, n),
            test.token,
            test.caps,
        )
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    notes: list[str] = []
    for cache in caches:
        why, note = evicted(test, cache, base, count)
        if why:
            problems.append(f"{cache.svc}: {why}")
        if note:
            notes.append(f"{cache.svc}: {note}")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(
        [f"{len(caches)} cache(s): {count} objects of {EVICT_OBJECT} bytes, read twice"] + notes
    )


RUN = {
    "put-declared": put_declared,
    "put-chunked": put_chunked,
    "put-client": put_client,
    "put-plugin": put_plugin,
    "put-checksum": put_checksum,
    "origin-read": origin_read,
    "client-get": client_get,
    "plugin-get": plugin_get,
    "cache-whole-first": cache_whole_first,
    "cache-range-first": cache_range_first,
    "cache-overlap": cache_overlap,
    "coalescing": coalescing,
    "cache-assemble": cache_assemble,
    "cache-abandoned": cache_abandoned,
    "cache-corrupted": cache_corrupted,
    "transfer-status": transfer_status,
    "transfer-status-failure": transfer_status_failure,
    "origin-transfer-status": origin_transfer_status,
    "digests": digests_scenario,
    "digest-overwrite": digest_overwrite,
    "digest-defaults": digest_defaults,
    "digest-relay": digest_relay,
    "digest-range-first": digest_range_first,
    "stale-origin-digest": stale_origin_digest,
    "past-end": past_end,
    "empty": empty,
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
