"""How the servers, and the clients, behave when an origin is slow.

See docs/tests/throttling.md.

Nothing is slow as the shape is, so the suite makes it so while it runs:
it restarts every origin with Origin.TransferRateLimit, as the last of
their configuration layers (control.restart_all_with()), runs range-ahead,
which needs the caches' scheduler as it is, then restarts every
federated V2 cache with a small fair scheduler (Cache.WorkerCount,
Cache.Throttle.*) for the rest, and afterward restores them all. The
rate limit slows only a posixv2 origin (origin_serve/rate_limited_fs.go
in Pelican), so the suite needs posixv2 origins, a federated V2 cache, a
control helper, and a topology other than `tiny`, whose servers are one
process.

The limit is one bucket per export, which every read and write of it
shares, so the cases run one at a time, and each waits for what it set
going to finish. The suite writes its objects straight into the first
origin's store on disk, which the limit does not slow, under
/protected-a/data/throttling/<run>/, a set per cache, so that each is
cold where it is first read, with a token of the session's profile.
Only the first origin holds them, so that a director with several sends
every fetch there, and a cache's fetches share one scheduler queue (a
tag per origin host; deriveSchedulerTag in client/handle_http.go).
"""

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from testlib import cacheapi, common, control, stores, suite, transfers, web
from testlib.common import Findings, die, outcome, warn
from testlib.report import FAIL, SKIP
from testlib.session import Session, last_line
from testlib.suite import NAMESPACE

SCENARIOS = {
    "range-ahead": (
        "each V2 cache serves a range far ahead of a cold read under way at once, and both"
        " come back intact"
    ),
    "shed-429": "each V2 cache sheds a burst of cold reads with 429 and Retry-After, not a 404",
    "client-throttled": "the clients, shed by a V2 cache, report a retryable error and retry",
    "timeout-header": "each V2 cache answers 504 by X-Pelican-Timeout, and goes on filling",
    "streaming": "each V2 cache sends a cold object's first bytes long before its last",
    "slow-miss": (
        "each V2 cache keeps a cold object whose fill outlasts its idle timeout, and what it filled"
        " of one given up on"
    ),
    "slow-transfer": "the clients give up on a transfer too slow, with error 6002",
}

# The scenarios that run with the origins slowed, and the caches'
# scheduler as the shape has it: the small one lets a cache fetch from
# an origin one at a time, which would queue range-ahead's fill behind
# the download it is ahead of.
SCHEDULER_AS_IS = ("range-ahead", "slow-miss")


# The origins' rate limit (Origin.TransferRateLimit, in which a KB is
# 1024 bytes), and its bytes per second. The limiter lets a second's
# worth through at once (golang.org/x/time/rate's burst), and then that
# many a second.
RATE_LIMIT = "256KB/s"
RATE = 256 << 10
# The caches' fair scheduler: two workers, of which one origin may hold
# one (25%, rounded up), with one more fetch queued for it; a 429 tells
# a client to come back in 2 seconds.
RETRY_AFTER = 2
CACHE_CONFIG = (
    "Cache:\n"
    + "  WorkerCount: 2\n"
    + "  Throttle:\n"
    + "    PerOriginStarvingPercent: 25\n"
    + "    PerOriginActivePercent: 25\n"
    + "    PerOriginPendingSize: 1\n"
    + f"    RetryAfter: {RETRY_AFTER}s\n"
)
ORIGIN_CONFIG = f"Origin:\n  TransferRateLimit: {RATE_LIMIT}\n"
CONFIG_FILE = "90-throttling.yaml"

# How long a fetch from a slow origin may take to finish: several times
# what the largest object takes at the rate.
FILL_WAIT = 120

# A small object that shows the federation serves /protected-a again.
PROBE = "probe"
PROBE_SIZE = 5000

# shed-429: a burst of cold reads, each of an object the cache takes
# seconds to fetch, so that the first still holds the origin's one slot,
# and the next its one place in the queue, while the rest come.
BURST = 30
BURST_SIZE = 1 << 20
# How long after the burst begins the read of a missing object is sent,
# by when the cache is shedding.
MISSING_DELAY = 0.5
SHED_REASONS = ("origin_unresponsive", "origin_slow", "cache_overloaded", "too_many_requests")

# timeout-header: an object whose fetch holds the origin's slot for
# seconds, and small ones. A V2 cache waits for a cold object's headers
# for the header's time less a second, or half of it if under a second
# (serveObject in local_cache/persistent_cache_api.go), so it should
# answer within these of the request.
SLOW_SIZE = 2 << 20
SMALL_SIZE = 64 << 10
SHORT_TIMEOUT = "20ms"
QUEUED_TIMEOUT = "2s"
HELD_TIMEOUT = "5s"
MALFORMED_TIMEOUT = "soon"
SHORT_ANSWER = 3.0
QUEUED_ANSWER = 3.0
# How long after the 20ms read the next is sent, by when the first's
# fetch holds the origin's slot.
OCCUPY_DELAY = 2.0

# streaming: an object that takes seconds to come; its first bytes must
# come within STREAM_FIRST, and in less than half the time of the whole.
STREAM_SIZE = 2 << 20
STREAM_HEAD = 4096
STREAM_FIRST = 5.0
# Less than this for the whole read, and the origins were not slow.
STREAM_SLOWEST = 0.5 * (STREAM_SIZE - RATE) / RATE

# range-ahead: an object whose cold read takes seconds; once that read
# is under way, a range that begins a kilobyte before its last 128 KiB
# chunk (the granularity at which a V2 cache's download tells readers
# of its progress; local_cache/block_fetcher.go), and runs to its end,
# crossing that chunk's edge and blocks' edges. A V2 cache waits for a
# download that is expected to write a block within 5 seconds
# (ETAStaleThreshold), and otherwise fills the range itself, so the
# range must come within AHEAD_ANSWER, many seconds before the read
# reaches it.
AHEAD_SIZE = 4 << 20
AHEAD_START = AHEAD_SIZE - (128 << 10) - 1000
AHEAD_DELAY = 2.0
AHEAD_ANSWER = 5.0
AHEAD_SLOWEST = 0.5 * (AHEAD_SIZE - RATE) / RATE

# slow-miss: a V2 cache goes on filling a miss while any client reads it,
# and LocalCache.PrefetchTimeout (10 seconds by default) once none does,
# keeping the whole blocks it wrote (local_cache/block_fetcher.go). Its
# objects: one whose cold read, at the rate limit, takes about 16
# seconds (one faster than MISS_SLOWEST shows nothing, since the origins
# were not slow); and one read for MISS_ABANDON_AFTER bytes, then given
# up on, after which the cache's fill goes on for FILL_IDLE seconds at
# most.
MISS_SIZE = 4 << 20
MISS_SLOWEST = 12.0
MISS_ABANDONED_SIZE = 8 << 20
MISS_ABANDON_AFTER = 64 << 10
FILL_IDLE = 10 + 5
ASK_STATUS = {"X-Transfer-Status": "true", "TE": "trailers"}
STATUS_OK = "200: OK"


class Test(suite.Test):
    """The run's objects in /protected-a, and the shape.

    Attributes:
        origins: The origins.
        holder: The origin that holds the run's objects.
        v2: The federated V2 caches.
    """

    def __init__(self, session: Session):
        super().__init__(session, "throttling")
        self.origins = self.fed.origins
        self.holder = self.origins[0]
        self.v2 = [c for c in self.fed.federated_caches if c.kind == "v2"]

    def write(self, objects: Mapping[str, bytes], everywhere: bool = False) -> str | None:
        """Write objects, by rel, straight into origins' stores on disk.

        They go into the holder's store, or with everywhere into every
        origin's.

        Returns:
            What went wrong, or None.
        """
        origins = self.origins if everywhere else [self.holder]
        for rel, data in objects.items():
            errors = stores.write_file_everywhere(origins, self.namespace, rel, data)
            if errors:
                return errors[0]
        return None


def shed_reason(answer: web.Response) -> str | None:
    """Return the `error` of a 429's JSON body, or None if it has none."""
    reason = answer.object().get("error")
    return reason if isinstance(reason, str) else None


def shed_problem(answer: web.Response) -> str | None:
    """Return what is wrong with a 429 from a V2 cache, or None.

    It must say to come back in RETRY_AFTER seconds, and why, as one of
    SHED_REASONS.
    """
    retry = answer.header("Retry-After")
    if retry != str(RETRY_AFTER):
        return f"a 429 with Retry-After '{retry}', not '{RETRY_AFTER}'"
    reason = shed_reason(answer)
    if reason not in SHED_REASONS:
        return f"a 429 whose body's error is {reason!r}, not one of {', '.join(SHED_REASONS)}"
    return None


# --------------------------------------------------------------------------
# The federation, slowed.


# --------------------------------------------------------------------------
# The cases. Each returns its row, or its rows by name (see CASES).


def burst(test: Test, cache: common.Cache, found: Findings) -> None:
    """Run shed-429 at one V2 cache (see shed_429())."""
    rels = [test.rel("shed-429", cache.svc, f"burst-{n}") for n in range(BURST)]
    missing = test.rel("shed-429", cache.svc, "missing")
    objects = {rel: os.urandom(BURST_SIZE) for rel in rels}
    why = test.write(objects)
    if why:
        found.problems.append(why)
        return

    def get_missing() -> web.Response:
        time.sleep(MISSING_DELAY)
        return test.ask("GET", cache.url, missing)

    with common.pool(BURST + 1) as pool:
        reads = [pool.submit(test.ask, "GET", cache.url, rel) for rel in rels]
        lost = pool.submit(get_missing)
        answers = [r.result() for r in reads]
        absent = lost.result()

    statuses: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for rel, answer in zip(rels, answers):
        statuses[answer.describe()] = statuses.get(answer.describe(), 0) + 1
        if answer.status == 429:
            problem = shed_problem(answer)
            if problem:
                found.problems.append(f"{cache.svc}: {suite.leaf(rel)}: {problem}")
            reason = shed_reason(answer) or "none"
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        why = web.served(answer, objects[rel])
        if why:
            found.problems.append(f"{cache.svc}: {suite.leaf(rel)}: {why}, neither the object nor 429")
    counts = ", ".join(f"{n} {s}" for s, n in sorted(statuses.items()))
    if absent.status != 404:
        what = f"429 ({shed_reason(absent)})" if absent.status == 429 else absent.describe()
        found.problems.append(f"{cache.svc}: a missing object, read during the burst, got {what}, not 404")
    if not reasons:
        found.problems.append(f"{cache.svc}: of {BURST} cold reads at once, none shed: {counts}")
        return
    said = ", ".join(f"{n} {r}" for r, n in sorted(reasons.items()))
    found.notes.append(f"{cache.svc}: {counts} ({said})")


def shed_429(test: Test) -> suite.Row:
    """Check that each V2 cache sheds some of a burst of cold reads.

    Each gets BURST reads at once of cold objects that take seconds each
    to fetch, and, MISSING_DELAY seconds in, one of an object that no
    origin holds. The cache fetches one at a time from the origin, and
    queues one more, so it must shed some: 429, with Retry-After of
    RETRY_AFTER seconds, and a JSON body whose error says why
    (handleError in local_cache/persistent_cache_api.go). The rest must
    come back whole. The missing object must be 404: telling a client to
    come back later for what does not exist would have it come back for
    ever.
    """
    found = Findings()
    for cache in test.v2:
        burst(test, cache, found)
    return found.row(f"{len(test.v2)} cache(s)")


def answered_in(answer: web.Response, took: float, status: int, within: float) -> str | None:
    """Return why answer is not status within `within` seconds, or None.

    Args:
        answer: The answer.
        took: How many seconds it took.
        status: The status it must be.
        within: How many seconds it may take.
    """
    if answer.status != status:
        return f"{answer.describe()}, not {status}"
    if took > within:
        return f"{status}, but after {took:.1f}s, past {within:.0f}s"
    return None


def timed(test: Test, cache: common.Cache, rel: str, timeout: str) -> tuple[web.Response, float]:
    """GET rel at cache with X-Pelican-Timeout timeout, and time it.

    Returns:
        The answer, and how many seconds it took.
    """
    started = time.monotonic()
    answer = test.ask("GET", cache.url, rel, {"X-Pelican-Timeout": timeout})
    return answer, time.monotonic() - started


def timeouts(test: Test, cache: common.Cache, found: dict[str, Findings]) -> None:
    """Run timeout-header at one V2 cache (see TIMEOUT_CASES)."""
    slow, queued, held, odd = (
        test.rel("timeout-header", cache.svc, name) for name in ("slow", "queued", "held", "malformed")
    )
    objects = {slow: os.urandom(SLOW_SIZE)}
    objects.update({rel: os.urandom(SMALL_SIZE) for rel in (queued, held, odd)})
    why = test.write(objects)
    if why:
        for each in found.values():
            each.problems.append(why)
        return

    why = web.served(test.ask("GET", cache.url, held), objects[held])
    if why:
        found["held"].problems.append(f"{cache.svc}: a plain GET, to fill it, got {why}")
    elif not test.wait_cached(cache, held, FILL_WAIT):
        found["held"].problems.append(
            f"{cache.svc}: did not hold an object within {FILL_WAIT}s of a whole read"
        )
    else:
        answer, took = timed(test, cache, held, HELD_TIMEOUT)
        why = web.served(answer, objects[held])
        if why:
            found["held"].problems.append(
                f"{cache.svc}: with X-Pelican-Timeout: {HELD_TIMEOUT}, what it holds got {why}"
            )
        else:
            found["held"].notes.append(f"{cache.svc}: in {took:.1f}s")

    # Which cold reads were answered 504, behind which a fetch goes on.
    timed_out: set[str] = set()
    answer, took = timed(test, cache, slow, SHORT_TIMEOUT)
    if answer.status == 504:
        timed_out.add("short")
    why = answered_in(answer, took, 504, SHORT_ANSWER)
    if why:
        found["short"].problems.append(
            f"{cache.svc}: a cold read with X-Pelican-Timeout: {SHORT_TIMEOUT} got {why}"
        )
    time.sleep(OCCUPY_DELAY)
    answer, took = timed(test, cache, queued, QUEUED_TIMEOUT)
    if answer.status == 504:
        timed_out.add("queued")
    if answer.status == 429:
        found["queued"].doubts.append(
            f"{cache.svc}: shed (429): another fetch had the origin's place in the queue"
        )
    elif answer.status == 200 and took <= QUEUED_ANSWER:
        found["queued"].doubts.append(
            f"{cache.svc}: served in {took:.1f}s: its fetch began at once, not behind another"
        )
    else:
        why = answered_in(answer, took, 504, QUEUED_ANSWER)
        if why:
            found["queued"].problems.append(
                f"{cache.svc}: a cold read queued behind another, with X-Pelican-Timeout:"
                + f" {QUEUED_TIMEOUT}, got {why}"
            )

    # The fetches go on, and finish, behind the 504s. A read refused, or
    # shed, began none, so there is none to wait for.
    for case, rel in (("short", slow), ("queued", queued)):
        if case not in timed_out:
            continue
        if not test.wait_cached(cache, rel, FILL_WAIT):
            found[case].problems.append(
                f"{cache.svc}: after the 504, the cache did not hold the object within"
                + f" {FILL_WAIT}s: it gave up the fetch"
            )
        else:
            why = web.served(test.ask("GET", cache.url, rel), objects[rel])
            if why:
                found[case].problems.append(f"{cache.svc}: once held, a GET got {why}")

    answer, _ = timed(test, cache, odd, MALFORMED_TIMEOUT)
    why = web.served(answer, objects[odd])
    if why:
        found["malformed"].problems.append(
            f"{cache.svc}: with X-Pelican-Timeout: {MALFORMED_TIMEOUT}, a cold read got {why}"
        )


# timeout-header's cases, in the order of their rows.
TIMEOUT_CASES = ("short", "queued", "held", "malformed")


def timeout_header(test: Test) -> dict[str, suite.Row]:
    """Check that each V2 cache heeds X-Pelican-Timeout.

    The header says how long a client will wait (serveObject in
    local_cache/persistent_cache_api.go):

      short      a cold read with 20ms must get 504 within SHORT_ANSWER
                 seconds
      queued     a cold read with 2s, sent while another's fetch holds
                 the origin's one slot, so that its own waits, must get
                 504 within QUEUED_ANSWER seconds
      held       what the cache holds, with 5s, must come back
      malformed  a cold read with a value that is no duration must come
                 back, the value ignored

    For `short` and `queued`, where the read got its 504, the fetch must
    go on behind it: the cache must then hold the object, and serve it.
    """
    found = {case: Findings() for case in TIMEOUT_CASES}
    for cache in test.v2:
        timeouts(test, cache, found)
    return {case: found[case].row(f"{len(test.v2)} cache(s)") for case in TIMEOUT_CASES}


def streaming(test: Test) -> suite.Row:
    """Check that each V2 cache streams a cold object as it fetches it.

    At each, a cold read of an object that takes seconds to fetch: its
    first STREAM_HEAD bytes must come within STREAM_FIRST seconds, and
    in less than half the time of the whole, which must come back
    intact. A cache that sent nothing until it held the whole object
    would send its first bytes with its last. A whole read faster than
    STREAM_SLOWEST shows nothing, since the origins were not slow.
    """
    found = Findings()
    for cache in test.v2:
        rel = test.rel("streaming", cache.svc)
        data = os.urandom(STREAM_SIZE)
        why = test.write({rel: data})
        if why:
            found.problems.append(why)
            continue
        read = web.timed_get(test.url(cache.url, rel), test.token, STREAM_HEAD, timeout=120)
        why = web.served(read.response, data)
        if why or read.first is None or read.whole is None:
            found.problems.append(f"{cache.svc}: a cold read got {why or 'no body'}")
            continue
        times = f"the first {STREAM_HEAD} bytes in {read.first:.1f}s, all in {read.whole:.1f}s"
        if read.whole < STREAM_SLOWEST:
            found.doubts.append(f"{cache.svc}: {times}: the origins were not slow")
        elif read.first > STREAM_FIRST or read.first > read.whole / 2:
            found.problems.append(f"{cache.svc}: {times}: it did not stream")
        else:
            found.notes.append(f"{cache.svc}: {times}")
    return found.row(f"{len(test.v2)} cache(s)")


def range_ahead(test: Test) -> suite.Row:
    """Check that each V2 cache serves a range ahead of a read under way.

    At each, a cold whole read of an object that takes many seconds to
    fetch; AHEAD_DELAY seconds in, a range from AHEAD_START to the end,
    which the read will not reach for many seconds more. The range must
    come back within AHEAD_ANSWER seconds, as 206 with exactly its bytes,
    rather than wait for the read; the read must come back intact, though
    its download found blocks that the range's fill had written already;
    and so must a warm whole read afterward. A whole read faster than
    AHEAD_SLOWEST shows nothing, since the origins were not slow.
    """
    found = Findings()
    for cache in test.v2:
        rel = test.rel("range-ahead", cache.svc)
        data = os.urandom(AHEAD_SIZE)
        why = test.write({rel: data})
        if why:
            found.problems.append(why)
            continue
        with common.pool(1) as pool:
            whole = pool.submit(web.timed_get, test.url(cache.url, rel), test.token, 64 << 10, timeout=120)
            time.sleep(AHEAD_DELAY)
            asked = time.monotonic()
            ranged = test.ask("GET", cache.url, rel, headers={"Range": f"bytes={AHEAD_START}-"})
            took = time.monotonic() - asked
            read = whole.result()
        want = f"bytes {AHEAD_START}-{AHEAD_SIZE - 1}/{AHEAD_SIZE}"
        if ranged.status != 206 or ranged.header("Content-Range") != want:
            found.problems.append(
                f"{cache.svc}: the range got {ranged.describe()}, Content-Range"
                + f" '{ranged.header('Content-Range')}', not 206 and '{want}'"
            )
        elif ranged.body != data[AHEAD_START:]:
            found.problems.append(f"{cache.svc}: the range's bytes are not the object's")
        why = web.served(read.response, data)
        if why or read.whole is None:
            found.problems.append(f"{cache.svc}: the read under way got {why or 'no body'}")
            continue
        why = web.served(test.ask("GET", cache.url, rel), data)
        if why:
            found.problems.append(f"{cache.svc}: a warm read after both got {why}")
        times = f"the range in {took:.1f}s, the whole read in {read.whole:.1f}s"
        if read.whole < AHEAD_SLOWEST:
            found.doubts.append(f"{cache.svc}: {times}: the origins were not slow")
        elif took > AHEAD_ANSWER:
            found.problems.append(f"{cache.svc}: {times}: the range waited for the read")
        else:
            found.notes.append(f"{cache.svc}: {times}")
    return found.row(f"{len(test.v2)} cache(s)")


def slow_miss(test: Test) -> dict[str, suite.Row]:
    """Check that a V2 cache keeps what it fills of a slow miss.

    kept       a cold whole read of MISS_SIZE through each V2 cache,
               asking for the transfer's status, which the rate limit
               draws out past LocalCache.PrefetchTimeout: it must get
               the object, and end with the trailer STATUS_OK, and the
               cache then hold the object whole (only-if-cached)
    abandoned  a cold read of MISS_ABANDONED_SIZE given up on after
               MISS_ABANDON_AFTER bytes: FILL_IDLE seconds later, the
               cache's introspection must say it holds some of the
               object's blocks, which it keeps; a whole read must then
               get the object, and leave the cache holding it whole
    """
    kept, abandoned = Findings(), Findings()
    admin = {c.svc: cacheapi.admin_token(test.session, c) for c in test.v2}
    for cache in test.v2:
        rel = test.rel("slow-miss", cache.svc, "kept")
        data = os.urandom(MISS_SIZE)
        why = test.write({rel: data})
        if why:
            kept.problems.append(why)
            continue
        started = time.monotonic()
        answer = test.ask("GET", cache.url, rel, ASK_STATUS)
        took = time.monotonic() - started
        said = answer.trailer("X-Transfer-Status")
        why = web.served(answer, data)
        if why:
            kept.problems.append(f"{cache.svc}: the cold read got {why}")
        elif said != STATUS_OK:
            kept.problems.append(
                f"{cache.svc}: the cold read ended with '{said or 'no trailer'}', not '{STATUS_OK}'"
            )
        elif not test.wait_cached(cache, rel):
            kept.problems.append(
                f"{cache.svc}: after the cold read of {took:.0f}s, it does not hold the object"
            )
        elif took < MISS_SLOWEST:
            kept.doubts.append(f"{cache.svc}: the cold read took {took:.1f}s: the origins were not slow")
        else:
            kept.notes.append(f"{cache.svc}: '{said}' after {took:.0f}s, and held")
    for cache in test.v2:
        rel = test.rel("slow-miss", cache.svc, "abandoned")
        data = os.urandom(MISS_ABANDONED_SIZE)
        why = test.write({rel: data})
        if why:
            abandoned.problems.append(why)
            continue
        answer = web.read_part(test.url(cache.url, rel), test.token, MISS_ABANDON_AFTER)
        if answer.status != 200 or answer.body != data[:MISS_ABANDON_AFTER]:
            abandoned.problems.append(f"{cache.svc}: the read given up on got {answer.describe()}")
            continue
        time.sleep(FILL_IDLE)
        url = cacheapi.object_url(test.fed, test.path(rel))
        held, why = cacheapi.blocks_held(cache, admin[cache.svc], url)
        if held is None:
            abandoned.doubts.append(f"{cache.svc}: no block summary ({why})")
        elif not held.held:
            abandoned.problems.append(
                f"{cache.svc}: {FILL_IDLE}s after its client gave up, it holds no block"
            )
        why = web.served(test.ask("GET", cache.url, rel), data)
        if why:
            abandoned.problems.append(f"{cache.svc}: a whole read afterward got {why}")
        elif not test.wait_cached(cache, rel):
            abandoned.problems.append(f"{cache.svc}: after a whole read, it does not hold the object")
        elif held is not None:
            abandoned.notes.append(f"{cache.svc} kept {held.describe()}")
    return {
        "kept": kept.row(f"{len(test.v2)} cache(s)"),
        "abandoned": abandoned.row(f"{len(test.v2)} cache(s)"),
    }


# --------------------------------------------------------------------------
# The clients.


@dataclass(frozen=True)
class Fetched:
    """What a client's download of one object did.

    The object is named `object`, in a directory of its own.

    Attributes:
        status: Its exit status.
        said: Its output; the plugin's, its result ads if it wrote any.
        left: What its directory holds afterward.
        data: The object's bytes, if it left them.
    """

    status: int
    said: str
    left: tuple[str, ...]
    data: bytes | None

    def codes(self) -> set[int]:
        """Return the Pelican error codes it reported."""
        found = re.findall(r"Error code (\d{4})|PelicanErrorCode = (\d+)", self.said)
        return {int(a or b) for a, b in found}

    def leftovers(self) -> str | None:
        """Return why a failed download left something behind, or None.

        The clients write a temporary file beside the object's, and
        rename it once it is complete.
        """
        return f"it left {list(self.left)}" if self.left else None

    def got(self, data: bytes) -> str | None:
        """Return why it did not download data, or None."""
        if self.status != 0:
            return f"exit {self.status}: {last_line(self.said)}"
        if self.data != data:
            what = "no file" if self.data is None else f"{len(self.data)} bytes, not the object"
            return f"exit 0, but {what}"
        if self.left != ("object",):
            return f"exit 0, but it left {list(self.left)}"
        return None


# The client's thresholds for a slow transfer (Client.MinimumDownloadSpeed,
# in bytes a second, Client.SlowTransferRampupTime, and
# Client.SlowTransferWindow): after 2 seconds, it gives up on a transfer
# that stays under 1 MiB a second for 5. The origins' rate is a quarter
# of that. The defaults (100 KiB a second, after 100 seconds, for 30) let
# it be.
SLOW_ENV = {
    "PELICAN_CLIENT_MINIMUMDOWNLOADSPEED": str(1 << 20),
    "PELICAN_CLIENT_SLOWTRANSFERRAMPUPTIME": "2s",
    "PELICAN_CLIENT_SLOWTRANSFERWINDOW": "5s",
}
# An object that would take the clients about 15 seconds at the origins'
# rate, and so is cut off as slow after about 7.
SLOW_TRANSFER_SIZE = 4 << 20
SLOW_TRANSFER = 6002
# The error codes of an attempt that a cache shed (docs/error_codes.yaml):
# Transfer.OriginUnresponsive, OriginSlow, and CacheOverloaded.
THROTTLED = {6008, 6009, 6010}
# stash_plugin's exit status for a failure it may retry (`Retryable` in
# cmd/plugin.go).
RETRYABLE_EXIT = 11
# `pelican object get`'s exit status for 6002 and 6008-6010: their
# clientExitCode in docs/error_codes.yaml.
TRANSFER_EXIT = 9


def client_env(test: Test, slow: bool) -> dict[str, str]:
    """Return the clients' environment, with SLOW_ENV, or without it."""
    env = dict(test.session.env)
    for name in SLOW_ENV:
        env.pop(name, None)
    if slow:
        env.update(SLOW_ENV)
    return env


def fetched(directory: str, status: int, said: str) -> Fetched:
    """Return what a client that downloaded into directory did."""
    left = tuple(sorted(os.listdir(directory)))
    data: bytes | None = None
    path = os.path.join(directory, "object")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            data = f.read()
    return Fetched(status, said, left, data)


def get_pelican(
    test: Test,
    name: str,
    rel: str,
    slow: bool,
    direct: bool = False,
    cache: common.Cache | None = None,
) -> Fetched:
    """Run `pelican object get` of rel into a directory of its own.

    Args:
        test: The run.
        name: The directory's name.
        rel: The object.
        slow: Whether to run with SLOW_ENV.
        direct: Whether to read from an origin.
        cache: The cache to read through alone (--cache), if any.
    """
    directory = test.directory(name)
    args = ["object", "get", "--token", test.token_file]
    if direct:
        args.append("--direct")
    if cache:
        args += ["--cache", cache.url]
    args += [test.pelican_url(rel), os.path.join(directory, "object")]
    status, out, err = test.session.pelican_cmd(*args, env=client_env(test, slow), cwd=directory)
    return fetched(directory, status, out + err)


def get_plugin(
    test: Test,
    name: str,
    rel: str,
    slow: bool,
    direct: bool = False,
    cache: common.Cache | None = None,
) -> Fetched:
    """Run stash_plugin on rel, into a directory of its own.

    It takes what get_pelican() takes; a direct read adds ?directread to
    the URL, and a cache is named by Client.PreferredCaches.
    """
    directory = test.directory(name)
    url = test.pelican_url(rel) + (transfers.DIRECT_QUERY if direct else "")
    status, out, err, ads = test.session.plugin_run(
        f"{test.area}/{name}",
        [(url, "object")],
        creds={"server.use": test.token_file},
        cache=cache.url if cache else None,
        env=client_env(test, slow),
        cwd=directory,
    )
    return fetched(directory, status, ads or err or out)


def plugin_problems(fetch: Fetched, codes: set[int]) -> list[str]:
    """Return what is wrong with the plugin's failed download.

    It must have failed with one of codes, in a way it may retry: exit
    RETRYABLE_EXIT, TransferSuccess = false and TransferRetryable =
    true, and an error of type Transfer whose DeveloperData names the
    code (createTransferError in cmd/plugin.go).
    """
    problems: list[str] = []
    if "TransferSuccess = false" not in fetch.said:
        return [f"it wrote no ad of a failure: {last_line(fetch.said)}"]
    if fetch.status != RETRYABLE_EXIT:
        problems.append(f"exit {fetch.status}, not {RETRYABLE_EXIT} (retryable)")
    if not re.search(r"TransferRetryable = true\b", fetch.said):
        problems.append("its ad does not say TransferRetryable = true")
    if not re.search(r'\bErrorType = "Transfer"', fetch.said):
        problems.append('its TransferErrorData has no ErrorType = "Transfer"')
    if not fetch.codes() & codes:
        found = sorted(fetch.codes()) or "none"
        problems.append(f"PelicanErrorCode {found}, not {' or '.join(map(str, sorted(codes)))}")
    why = fetch.leftovers()
    if why:
        problems.append(why)
    return problems


def pelican_problems(fetch: Fetched, codes: set[int]) -> list[str]:
    """Return what is wrong with `pelican object get`'s failed download.

    It must have failed with one of codes, and exited with their
    clientExitCode, TRANSFER_EXIT.
    """
    problems: list[str] = []
    if fetch.status == 0:
        return ["it succeeded"]
    if not fetch.codes() & codes:
        problems.append(f"it failed, but not with {' or '.join(map(str, sorted(codes)))}:")
        problems[-1] += f" {last_line(fetch.said)}"
    if fetch.status != TRANSFER_EXIT:
        problems.append(f"exit {fetch.status}, not {TRANSFER_EXIT}")
    why = fetch.leftovers()
    if why:
        problems.append(why)
    return problems


def throttle_hints(fetch: Fetched) -> list[str]:
    """Return what is wrong with the throttle hints in the plugin's ad.

    The ad is of a shed attempt. Its DeveloperData must carry
    RetryAfterSeconds, the cache's Retry-After, and ThrottleReason, the
    429's reason, which it leaves out only where there is none
    (cmd/plugin.go).
    """
    problems: list[str] = []
    retry = re.search(r"RetryAfterSeconds = (\d+)", fetch.said)
    if not retry or retry.group(1) != str(RETRY_AFTER):
        given = retry.group(1) if retry else "none"
        problems.append(f"RetryAfterSeconds {given}, not {RETRY_AFTER}")
    reason = re.search(r'ThrottleReason = "([^"]*)"', fetch.said)
    if reason and reason.group(1) not in SHED_REASONS:
        problems.append(f"ThrottleReason {reason.group(1)!r}")
    elif not reason and 6010 not in fetch.codes():
        problems.append("no ThrottleReason, though the error names a reason")
    return problems


# The clients, by the name of their row.
CLIENTS: dict[str, Callable[..., Fetched]] = {"pelican": get_pelican, "plugin": get_plugin}
# How many times client-throttled tries a client again, RETRY_AFTER
# seconds apart, once the cache has room.
RETRIES = 5
# client-throttled's objects: one whose fetch holds the origin's one slot
# for about 15 seconds, then one that holds its place in the queue.
OCCUPY_SIZE = 4 << 20


def shed_client(test: Test, cache: common.Cache, found: dict[str, Findings]) -> None:
    """Run client-throttled at one V2 cache (see client_throttled())."""
    slot, queue = (test.rel("client-throttled", cache.svc, n) for n in ("slot", "queue"))
    rels = {c: test.rel("client-throttled", cache.svc, c) for c in CLIENTS}
    objects = {slot: os.urandom(OCCUPY_SIZE), queue: os.urandom(SLOW_SIZE)}
    objects.update({rel: os.urandom(SMALL_SIZE) for rel in rels.values()})
    why = test.write(objects)
    if why:
        for each in found.values():
            each.problems.append(why)
        return

    shed: dict[str, Fetched] = {}
    with common.pool(2) as pool:
        first_read = pool.submit(test.ask, "GET", cache.url, slot)
        time.sleep(OCCUPY_DELAY)
        second_read = pool.submit(test.ask, "GET", cache.url, queue)
        time.sleep(OCCUPY_DELAY)
        for client, get in CLIENTS.items():
            shed[client] = get(test, f"shed-{client}-{cache.svc}", rels[client], False, cache=cache)
        occupied = [(slot, first_read.result()), (queue, second_read.result())]
    for rel, answer in occupied:
        why = web.served(answer, objects[rel])
        if why:
            for each in found.values():
                each.doubts.append(f"{cache.svc}: the read to fill its queue, {suite.leaf(rel)}, got {why}")

    for client, fetch in shed.items():
        if fetch.status == 0:
            found[client].doubts.append(f"{cache.svc}: it was never shed")
            continue
        if client == "pelican":
            problems = pelican_problems(fetch, THROTTLED)
        else:
            problems = plugin_problems(fetch, THROTTLED) + throttle_hints(fetch)
        found[client].problems += [f"{cache.svc}: shed: {p}" for p in problems]
        tries = 0
        again = fetch
        while tries < RETRIES:
            tries += 1
            time.sleep(RETRY_AFTER)
            name = f"retry-{client}-{cache.svc}-{tries}"
            again = CLIENTS[client](test, name, rels[client], False, cache=cache)
            if again.status == 0:
                break
        why = again.got(objects[rels[client]])
        if why:
            found[client].problems.append(
                f"{cache.svc}: tried again {tries} time(s), {RETRY_AFTER}s apart: {why}"
            )
        else:
            found[client].notes.append(
                f"{cache.svc}: shed with {sorted(fetch.codes() & THROTTLED)}, then served"
                + (f" at try {tries}" if tries > 1 else "")
            )


def client_throttled(test: Test) -> dict[str, suite.Row]:
    """Check that the clients, shed by a V2 cache, report a retryable error.

    At each V2 cache, a cold read holds the origin's one slot, and
    another its one place in the queue, so that the cache sheds the
    next. Meanwhile, `pelican object get` and stash_plugin each download
    a cold object through that cache alone. Each must fail as the cache
    shed it, with Transfer.OriginUnresponsive, OriginSlow, or
    CacheOverloaded (6008-6010), and exit with its documented code, and
    leave nothing; the plugin's ad must also say TransferRetryable =
    true, and carry the cache's Retry-After and reason. The client does
    not wait on Retry-After, and has no other cache to try; so the test,
    as a retrier outside it would, tries it again once the reads are
    done, RETRY_AFTER seconds apart, and it must then get the object.
    """
    found = {client: Findings() for client in CLIENTS}
    for cache in test.v2:
        shed_client(test, cache, found)
    return {client: found[client].row(f"{len(test.v2)} cache(s)") for client in CLIENTS}


def slow_transfer(test: Test) -> dict[str, suite.Row]:
    """Check that the clients give up on transfers too slow, and only those.

    The transfers are of objects of SLOW_TRANSFER_SIZE:

    direct         `pelican object get --direct`, with SLOW_ENV, must
                   fail with Transfer.SlowTransfer (6002), exit 9, and
                   leave nothing (handle_http.go's downloadHTTP)
    plugin-direct  stash_plugin, with ?directread, likewise, its ad
                   saying TransferRetryable = true
    default        each client, with the default thresholds, must get
                   the object: direct, or where the origins take no
                   direct clients, through the first V2 cache
    cache          `pelican object get` through each V2 cache, cold,
                   with SLOW_ENV, must fail as `direct` does
    plugin-cache   stash_plugin, preferring each V2 cache, likewise, as
                   `plugin-direct` does
    """
    rows: dict[str, suite.Row] = {}
    slow = test.rel("slow-transfer", "direct")
    data = os.urandom(SLOW_TRANSFER_SIZE)
    via = {
        (client, c.svc): test.rel("slow-transfer", f"{client}-{c.svc}")
        for client in CLIENTS
        for c in test.v2
    }
    defaults = {c: test.rel("slow-transfer", f"default-{c}") for c in CLIENTS}
    objects = {slow: data}
    objects.update({rel: os.urandom(SLOW_TRANSFER_SIZE) for rel in [*via.values(), *defaults.values()]})
    why = test.write(objects)
    if why:
        return {"": (FAIL, why)}

    no_direct = "/protected-a takes no direct clients"
    for name, client in (("direct", "pelican"), ("plugin-direct", "plugin")):
        if not test.direct:
            rows[name] = (SKIP, no_direct)
            continue
        fetch = CLIENTS[client](test, f"slow-{name}", slow, True, direct=True)
        if fetch.status == 0:
            problems = ["it got the object, at the origins' rate, under the minimum speed"]
        elif client == "pelican":
            problems = pelican_problems(fetch, {SLOW_TRANSFER})
        else:
            problems = plugin_problems(fetch, {SLOW_TRANSFER})
        rows[name] = outcome(problems, "error 6002")

    found = Findings()
    route = "direct" if test.direct else f"through {test.v2[0].svc}"
    for client, rel in defaults.items():
        if test.direct:
            fetch = CLIENTS[client](test, f"default-{client}", rel, False, direct=True)
        else:
            fetch = CLIENTS[client](test, f"default-{client}", rel, False, cache=test.v2[0])
        why = fetch.got(objects[rel])
        if why:
            found.problems.append(f"{client}, {route}: {why}")
    rows["default"] = found.row(f"both clients, {route}")

    for name, client in (("cache", "pelican"), ("plugin-cache", "plugin")):
        found = Findings()
        for cache in test.v2:
            rel = via[(client, cache.svc)]
            fetch = CLIENTS[client](test, f"slow-{name}-{cache.svc}", rel, True, cache=cache)
            if fetch.status == 0:
                found.problems.append(f"{cache.svc}: it got the object, under the minimum speed")
                continue
            judge = pelican_problems if client == "pelican" else plugin_problems
            found.problems += [f"{cache.svc}: {p}" for p in judge(fetch, {SLOW_TRANSFER})]
        rows[name] = found.row(f"{len(test.v2)} cache(s), error 6002")
    return rows


# --------------------------------------------------------------------------
# Running the cases.


def single(case: Callable[[Test], suite.Row]) -> Callable[[Test], dict[str, suite.Row]]:
    """Return case, of a scenario with one row, as CASES takes it."""
    return lambda test: {"": case(test)}


# What runs each scenario: its rows, each by its name after the
# scenario's ("" for the scenario's only row).
CASES: dict[str, Callable[[Test], dict[str, suite.Row]]] = {
    "shed-429": single(shed_429),
    "client-throttled": client_throttled,
    "timeout-header": timeout_header,
    "streaming": single(streaming),
    "range-ahead": single(range_ahead),
    "slow-miss": slow_miss,
    "slow-transfer": slow_transfer,
}


def row_name(scenario: str, name: str) -> str:
    """Return the row name of scenario's case name."""
    return f"{scenario} {name}" if name else scenario


def run_scenario(test: Test, scenario: str, results: common.Results) -> None:
    """Run scenario, and record its rows."""
    rows = suite.attempt_all([""], partial(CASES[scenario], test))
    for name, (status, note) in rows.items():
        results.add(row_name(scenario, name), status, note)


def run_phase(test: Test, scenarios: list[str], results: common.Results, done: set[str]) -> None:
    """Run scenarios, once the federation serves again, and note each done.

    Each fails where it does not serve within suite.SETTLE_WAIT seconds.
    """
    if not scenarios:
        return
    why = test.settle(test.rel(PROBE), PROBE_SIZE)
    for scenario in scenarios:
        if why:
            results.add(scenario, FAIL, f"with the servers restarted slowed, {why}")
        else:
            run_scenario(test, scenario, results)
        done.add(scenario)


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None.

    There is nothing with origins that the rate limit does not slow, the
    tiny topology, no /protected-a, or no federated V2 cache.
    """
    if fed.origin_variant != "posixv2":
        return "Origin.TransferRateLimit slows only a posixv2 origin"
    if fed.tiny:
        return "the tiny topology's servers are one process, which slowing the origin would slow"
    why = suite.unexported(fed)
    if why:
        return why
    if not any(c.kind == "v2" for c in fed.federated_caches):
        return "this shape has no federated V2 cache"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios with the origins and the caches slowed.

    Restore them afterward; then remove the run's objects.
    """
    fed = session.fed
    why = control.unavailable()
    if why:
        for scenario in selected:
            results.add(scenario, SKIP, why)
        return
    stores.require_dirs(fed.origins, [NAMESPACE], "data/throttling")
    test = Test(session)
    test.clean("before the run")
    print(f"Throttling test against {fed.url}, objects under /{NAMESPACE}/{test.base}/")
    done: set[str] = set()
    try:
        why = test.write({test.rel(PROBE): os.urandom(PROBE_SIZE)}, everywhere=True)
        if why:
            die(f"could not put the probe in place: {why}")
        first_phase = [s for s in selected if s in SCHEDULER_AS_IS]
        rest = [s for s in selected if s not in SCHEDULER_AS_IS]
        print(f"\nRestarting the origins limited to {RATE_LIMIT}")
        try:
            with control.restart_all_with({o.svc: {CONFIG_FILE: ORIGIN_CONFIG} for o in test.origins}):
                run_phase(test, first_phase, results, done)
                if rest:
                    print("\nRestarting the V2 caches throttled")
                    with control.restart_all_with({c.svc: {CONFIG_FILE: CACHE_CONFIG} for c in test.v2}):
                        run_phase(test, rest, results, done)
        except control.ControlError as e:
            # Restarting, or restoring, failed: what is left unrun fails.
            for scenario in selected:
                if scenario not in done:
                    results.add(scenario, FAIL, f"restarting the servers: {e}")
            warn(f"restarting the servers slowed, or restoring them: {e}")
        why = test.settle(test.rel(PROBE), PROBE_SIZE)
        if why:
            warn(f"with the servers restored, {why}")
    finally:
        test.clean("after the run")
