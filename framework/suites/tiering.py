"""Cache tiering (`-p cache-tiered`): each V2 cache that holds all of an
object of at least its threshold (16 MiB) uploads it to its prefix of
the `s3` service's bucket `tier`, framework/var/data/tier/<cache>/, and
from then on serves it from there: by redirecting the client to a
pre-signed URL, or, for a cache that proxies (cache-1), itself.

Objects go under /protected-a/data/tiering/<run>/, each cache's its
own, so that each is cold where it is read. A tiered object is found in
the bucket by its size and SHA-256, since the cache names it by a hash.
Only `tampered` touches the bucket, as corruption or a substitution
there would; nothing touches the caches' stores. Pelican's main has
tiering, but no release yet; a cache that neither reports tiering
metrics nor has claimed its prefix in the bucket is taken not to tier,
and its scenarios skip.

  tiered       a cold read of an object over the threshold: it lands in
               the bucket, and the cache counts a successful upload
  threshold    an object of exactly the threshold is tiered; one a byte
               smaller is not
  redirect     a read of a tiered object: a redirect (307) to a
               pre-signed URL in the bucket, without the token, which
               serves the object, and with the object's Digest, by which
               a client checks what the bucket sends; or, where the
               cache proxies, the object
  ranges       ranges of a tiered object, through the redirect or the
               proxy
  client-get   `pelican object get --cache` of a tiered object
  tampered     a tiered object whose copy in the bucket is replaced by
               other bytes of its size: a proxying cache must not serve
               them, and fetch the object afresh; through a redirecting
               one, `pelican object get` must fail rather than keep them
  overwritten  a tiered object overwritten at the origins: every read is
               all one version, and under ORIGIN_CACHE_CONTROL, the new
               one once the old is stale
"""

import hashlib
import os
import time
import traceback
from collections.abc import Callable
from typing import Optional
from urllib.parse import urlsplit

from testlib import blocks, common, digests, stores, web
from testlib.common import die, first
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "tiered": "an object over the threshold, read whole, lands in the bucket",
    "threshold": "an object of exactly the threshold is tiered; one a byte smaller is not",
    "redirect": "a tiered object: a redirect to a pre-signed URL, or the object, if proxied",
    "ranges": "ranges of a tiered object, through the redirect or the proxy",
    "client-get": "`pelican object get --cache` of a tiered object",
    "tampered": "a tiered object replaced in the bucket: never served, nor kept by the client",
    "overwritten": "a tiered object overwritten at the origins: each read all one version",
}

# How long past an object's freshness a cache may take to serve the new
# version (see the blocks suite's REVALIDATION_MARGIN).
REVALIDATION_MARGIN = 45

# How long a cache may take to tier an object it holds, in seconds.
TIER_WAIT = 60
# How much an object over the threshold is over it.
OVER = 4321
# The ranges that `ranges` asks for, within an object of the threshold
# plus OVER (see blocks.Request).
RANGES: tuple[blocks.Request, ...] = (
    ((0, 0),),
    ((5242879, 5242881),),
    ((None, 10),),
    ((16777215, 16777216),),
    ((16777216, None),),
)


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no cache tiers."""
    if not fed.tiering:
        return "no cache tiers ('-p cache-tiered')"
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def metrics(cache: common.Cache) -> str:
    """The cache's /metrics."""
    root = urlsplit(cache.url)
    return web.request("GET", f"{root.scheme}://{root.netloc}/metrics").body.decode(
        errors="replace"
    )


def counted(cache: common.Cache, name: str, label: str) -> float:
    """The sum of the cache's series of metric name with label, e.g.
    `result="success"`."""
    found = common.metric_series(metrics(cache), name)
    return sum(value for labels, value in found.items() if label in labels)


def tiers(cache: common.Cache, target: common.TierTarget) -> bool:
    """Whether the cache tiers at all: it reports tiering metrics, or has
    claimed its prefix in the bucket."""
    reported = "pelican_cache_tiering_" in metrics(cache)
    return reported or os.path.exists(f"{target.directory}/.pelican-cache-id")


class NotTiered(Exception):
    """A cache did not tier an object that a scenario needs tiered."""


def bucket_file(target: common.TierTarget, data: bytes) -> Optional[str]:
    """The file of the cache's prefix of the bucket that holds an object
    of data, if one does."""
    want = hashlib.sha256(data).hexdigest()
    for root, _, files in os.walk(target.directory):
        for name in files:
            path = os.path.join(root, name)
            if os.path.getsize(path) == len(data) and common.sha256(path) == want:
                return path
    return None


def in_bucket(target: common.TierTarget, data: bytes) -> bool:
    """Whether the cache's prefix of the bucket holds an object of data."""
    return bucket_file(target, data) is not None


class Test:
    """The tiering caches, the run's objects, and the token."""

    def __init__(self, session: Session, caches: list[common.Cache]):
        self.session = session
        self.fed = session.fed
        self.caches = caches
        self.base = f"data/tiering/{session.run}"
        self.token = session.token("protected-a")
        self.tmp = session.path("tiering")
        os.makedirs(self.tmp)
        # Objects that each cache has tiered, by cache, then name.
        self.tiered: dict[str, dict[str, bytes]] = {c.svc: {} for c in caches}

    def target(self, cache: common.Cache) -> common.TierTarget:
        """Where cache tiers objects."""
        return self.fed.tiering[cache.svc]

    def rel(self, cache: common.Cache, name: str) -> str:
        """Where object name of cache is in /protected-a."""
        return f"{self.base}/{cache.svc}-{name}"

    def put(self, cache: common.Cache, name: str, size: int) -> bytes:
        """Put a new object of size, cache's object name, at every origin:
        its bytes."""
        data = os.urandom(size)
        caps = self.fed.exports["protected-a"]
        rel = self.rel(cache, name)
        errors = stores.seed_everywhere(
            self.fed.origins, "protected-a", rel, data, self.token, caps
        )
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")
        return data

    def fetch(self, cache: common.Cache, name: str) -> web.Response:
        """GET cache's object name whole through cache, following a
        redirect without the token."""
        answer = web.request(
            "GET", f"{cache.url}/protected-a/{self.rel(cache, name)}", token=self.token
        )
        if answer.status == 307:
            answer = web.request("GET", answer.header("Location"))
        return answer

    def read(self, cache: common.Cache, name: str, data: bytes) -> Optional[str]:
        """Read cache's object name, holding data, whole through cache,
        following a redirect without the token; what went wrong, or
        None."""
        answer = self.fetch(cache, name)
        if answer.status != 200:
            return answer.describe()
        if answer.body != data:
            return f"{len(answer.body)} bytes, not the object"
        return None

    def tier(self, cache: common.Cache, name: str, size: int) -> Optional[str]:
        """Read a new object of size through cache, cold, and wait for it
        to land in the bucket; what went wrong, or None."""
        data = self.put(cache, name, size)
        why = self.read(cache, name, data)
        if why:
            return f"the cold read: {why}"
        if not common.wait_for(lambda: in_bucket(self.target(cache), data), TIER_WAIT, 2):
            return f"not in framework/var/{self.target(cache).directory} after {TIER_WAIT}s"
        self.tiered[cache.svc][name] = data
        return None

    def tiered_object(self, cache: common.Cache, name: str = "big") -> tuple[str, bytes]:
        """An object name that cache has tiered, of the threshold plus
        OVER, made once: its name and bytes. Raises NotTiered if the cache
        could not tier it."""
        if name not in self.tiered[cache.svc]:
            why = self.tier(cache, name, self.target(cache).threshold + OVER)
            if why:
                raise NotTiered(f"it did not tier an object: {why}")
        return name, self.tiered[cache.svc][name]

    def get_tiered(
        self, cache: common.Cache, name: str, headers: dict[str, str]
    ) -> web.Response:
        """GET cache's tiered object name through cache. A cache serves a
        stale object itself while it revalidates it, which, under an
        origin's Cache-Control (`-p origin-max-age`), it may be: then a
        200 is asked again, once."""
        url = f"{cache.url}/protected-a/{self.rel(cache, name)}"
        answer = web.request("GET", url, token=self.token, headers=headers)
        if answer.status == 200 and self.fed.origin_cache_control:
            answer = web.request("GET", url, token=self.token, headers=headers)
        return answer


def each_cache(test: Test, check: Callable[[common.Cache], Optional[str]]) -> tuple[str, str]:
    """check() at each tiering cache: FAIL with the first problem."""
    problems: list[str] = []
    for cache in test.caches:
        try:
            why = check(cache)
        except NotTiered as e:
            why = str(e)
        if why:
            problems.append(f"{cache.svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, ", ".join(f"{c.svc} ({test.target(c).mode})" for c in test.caches)


def tiered(test: Test) -> tuple[str, str]:
    """A cold read of an object over the threshold lands in the bucket,
    and the cache counts a successful upload."""

    def check(cache: common.Cache) -> Optional[str]:
        name = "uploads_total"
        before = counted(cache, f"pelican_cache_tiering_{name}", 'result="success"')
        why = test.tier(cache, "tiered", test.target(cache).threshold + OVER)
        if why:
            return why
        after = counted(cache, f"pelican_cache_tiering_{name}", 'result="success"')
        if after <= before:
            return f"in the bucket, but pelican_cache_tiering_{name} stayed at {before:g}"
        return None

    return each_cache(test, check)


def threshold(test: Test) -> tuple[str, str]:
    """An object of exactly the threshold is tiered. One a byte smaller,
    read first, is not, by the time the other has been."""

    def check(cache: common.Cache) -> Optional[str]:
        size = test.target(cache).threshold
        under = test.put(cache, "under", size - 1)
        why = test.read(cache, "under", under)
        if why:
            return f"a byte under the threshold, the cold read: {why}"
        why = test.tier(cache, "exact", size)
        if why:
            return f"at the threshold, {why}"
        if in_bucket(test.target(cache), under):
            return "an object a byte under the threshold is in the bucket"
        return None

    return each_cache(test, check)


def redirect(test: Test) -> tuple[str, str]:
    """A read of a tiered object: from a redirecting cache, a 307 to a
    pre-signed URL in the bucket, without the token, which serves the
    object to anyone holding it; from a proxying cache, the object."""

    def check(cache: common.Cache) -> Optional[str]:
        name, data = test.tiered_object(cache)
        answer = test.get_tiered(cache, name, {})
        if test.target(cache).mode == "proxy":
            if answer.status != 200 or answer.body != data:
                return f"proxied: {answer.describe()}, {len(answer.body)} bytes"
            return None
        if answer.status != 307:
            return f"{answer.describe()}, not a redirect (307)"
        location = answer.header("Location")
        parts = urlsplit(location)
        if parts.netloc != "s3:8444" or not parts.path.startswith("/tier/"):
            return f"redirected to {parts.netloc}{parts.path}, not bucket tier at s3:8444"
        if "X-Amz-Signature=" not in parts.query:
            return "redirected to a URL that is not pre-signed"
        if test.token in location:
            return "redirected to a URL that holds the token"
        wrong, right = digests.check(answer.header("Digest"), data)
        if wrong:
            return f"the redirect's Digest: {first(wrong)}"
        if not right:
            return "the redirect has no Digest by which a client could check the bucket's bytes"
        fetched = web.request("GET", location)
        if fetched.status != 200 or fetched.body != data:
            return f"the pre-signed URL: {fetched.describe()}, {len(fetched.body)} bytes"
        return None

    return each_cache(test, check)


def ranges(test: Test) -> tuple[str, str]:
    """Ranges of a tiered object (RANGES), each checked by blocks.check():
    from a proxying cache itself; from a redirecting one, a 307 for each,
    followed with the range but no token. rclone answers a range at the
    bucket with 200 and the range's bytes, which counts, but leaves the
    result inconclusive."""
    deviations: list[str] = []

    def check(cache: common.Cache) -> Optional[str]:
        name, data = test.tiered_object(cache)
        for request in RANGES:
            headers = {"Range": blocks.header(request)}
            answer = test.get_tiered(cache, name, headers)
            if test.target(cache).mode == "redirect":
                if answer.status != 307:
                    return f"{headers['Range']}: {answer.describe()}, not a redirect (307)"
                answer = web.request("GET", answer.header("Location"), headers=headers)
                first_byte, last_byte = blocks.resolve(len(data), request[0])
                if answer.status == 200 and answer.body == data[first_byte : last_byte + 1]:
                    deviations.append(f"{cache.svc} {headers['Range']}")
                    continue
            why = blocks.check(data, request, answer.status, answer.headers, answer.body)
            if why:
                return f"{headers['Range']}: {why}"
        return None

    status, note = each_cache(test, check)
    if status == PASS and deviations:
        return INCONCLUSIVE, f"the bucket answered ranges with 200: {first(deviations)}"
    return status, note


def client_get(test: Test) -> tuple[str, str]:
    """`pelican object get --cache` of a tiered object, which the client
    follows wherever the cache sends it."""

    def check(cache: common.Cache) -> Optional[str]:
        name, data = test.tiered_object(cache)
        target = os.path.join(test.tmp, f"{cache.svc}-{name}")
        code, out, err = test.session.pelican_cmd(
            "object",
            "get",
            "--cache",
            cache.url,
            "--token",
            test.session.token_file("protected-a"),
            f"{test.fed.url}/protected-a/{test.rel(cache, name)}",
            target,
        )
        if code != 0:
            return f"exit {code}: {last_line(err or out)}"
        if common.sha256(target) != common.digest(data):
            return "the file is not the object"
        return None

    return each_cache(test, check)


def client_get_file(test: Test, cache: common.Cache, name: str) -> tuple[int, str, str]:
    """`pelican object get --cache` of cache's object name into a file of
    its own: the client's exit status, the file, and what it said."""
    target = os.path.join(test.tmp, f"{cache.svc}-{name}")
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        cache.url,
        "--token",
        test.session.token_file("protected-a"),
        f"{test.fed.url}/protected-a/{test.rel(cache, name)}",
        target,
    )
    return code, target, last_line(err or out)


def proxied_after_tampering(
    test: Test, cache: common.Cache, name: str, data: bytes, other: bytes
) -> Optional[str]:
    """Why a proxying cache served the bucket's substitute (other) for its
    object name (data), or did not serve the object again within
    TIER_WAIT seconds; None if neither."""
    answer = test.fetch(cache, name)
    if answer.body == other:
        return "served the bucket's substitute"
    if answer.status == 200 and answer.body != data:
        return f"served {len(answer.body)} bytes that are not the object"

    def fetched_again() -> bool:
        again = test.fetch(cache, name)
        return again.status == 200 and again.body == data

    if not common.wait_for(fetched_again, TIER_WAIT, 2):
        return f"did not serve the object again within {TIER_WAIT}s"
    return None


def redirected_after_tampering(
    test: Test, cache: common.Cache, name: str, data: bytes, other: bytes
) -> Optional[str]:
    """Why `pelican object get` through a redirecting cache kept the
    bucket's substitute (other) for its object name (data), or kept
    anything but the object; None if it did neither."""
    code, target, said = client_get_file(test, cache, name)
    got = common.sha256(target)
    if got == common.digest(other):
        return "`pelican object get` kept the bucket's substitute"
    if code == 0 and got != common.digest(data):
        return "`pelican object get` succeeded, but the file is not the object"
    if code != 0 and got is not None:
        return f"`pelican object get` failed ({said}), but left a file"
    return None


def tampered(test: Test) -> tuple[str, str]:
    """A tiered object whose copy in the bucket is then replaced by other
    bytes of its size. A proxying cache must not serve those bytes: the
    read may fail, but within TIER_WAIT seconds the cache must serve the
    object again, fetched afresh (openTierStream in Pelican's
    local_cache/tier_serve.go). Through a redirecting cache, `pelican
    object get` must not keep the bytes, which the redirect's Digest
    belies: it must fail, or get the object itself."""

    def check(cache: common.Cache) -> Optional[str]:
        name, data = test.tiered_object(cache, "tampered")
        path = bucket_file(test.target(cache), data)
        if path is None:
            return "the tiered object is no longer in the bucket"
        other = os.urandom(len(data))
        try:
            with open(path, "r+b") as f:
                f.write(other)
        except OSError as e:
            return f"could not replace framework/var/{path}: {e.strerror}"
        if test.target(cache).mode == "proxy":
            return proxied_after_tampering(test, cache, name, data, other)
        return redirected_after_tampering(test, cache, name, data, other)

    return each_cache(test, check)


def overwritten(test: Test) -> tuple[str, str]:
    """A tiered object, overwritten at the origins with another of its
    size: each whole read through the cache must be all the old version
    or all the new. With ORIGIN_CACHE_CONTROL (`-p origin-max-age`), the
    cache must serve the new version once the old is stale, within
    REVALIDATION_MARGIN seconds; without it, the cache decides for itself
    how long its copy stays fresh."""
    fresh = blocks.freshness(test.fed.origin_cache_control)
    notes: list[str] = []

    def version(cache: common.Cache, name: str, versions: dict[str, bytes]) -> str:
        answer = test.fetch(cache, name)
        if answer.status != 200:
            return answer.describe()
        return next((v for v, data in versions.items() if answer.body == data), "neither")

    def check(cache: common.Cache) -> Optional[str]:
        name, v1 = test.tiered_object(cache, "overwritten")
        cached_at = time.time()
        v2 = os.urandom(len(v1))
        errors = stores.seed_everywhere(
            test.fed.origins,
            "protected-a",
            test.rel(cache, name),
            v2,
            test.token,
            test.fed.exports["protected-a"],
        )
        if errors:
            return first(errors)
        versions = {"v1": v1, "v2": v2}
        seen = version(cache, name, versions)
        if seen not in versions:
            return f"after the overwrite: {seen}"
        if fresh is None:
            notes.append(f"{cache.svc} served {seen}")
            return None
        deadline = cached_at + fresh + REVALIDATION_MARGIN
        while seen == "v1" and time.time() <= deadline:
            time.sleep(3)
            seen = version(cache, name, versions)
        if seen != "v2":
            return f"served {seen} {time.time() - cached_at:.0f}s after tiering v1"
        notes.append(f"{cache.svc} served v2 by {time.time() - cached_at:.0f}s")
        return None

    status, note = each_cache(test, check)
    if status == PASS and notes:
        note = "; ".join(notes)
    return status, note


RUN = {
    "tiered": tiered,
    "threshold": threshold,
    "redirect": redirect,
    "ranges": ranges,
    "client-get": client_get,
    "tampered": tampered,
    "overwritten": overwritten,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios at each cache that tiers, and remove the
    run's objects from the origins."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/tiering"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    targets = [(c, fed.tiering[c.svc]) for c in fed.caches if c.svc in fed.tiering]
    caches = [cache for cache, target in targets if tiers(cache, target)]
    if not caches:
        why = "no cache's image tiers (Pelican's main only, not a release yet)"
        for name in selected:
            results.add(name, SKIP, why)
        return
    tokens = session.tokens()
    test = Test(session, caches)
    print(
        f"Tiering through {', '.join(c.svc for c in caches)},"
        + f" objects under /protected-a/{test.base}/\n"
    )
    try:
        for name in selected:
            try:
                status, note = RUN[name](test)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # Only the origins' copies: the caches own the bucket and their
        # stores.
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/tiering", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
