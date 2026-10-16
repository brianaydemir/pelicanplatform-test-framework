"""How the servers, and the clients, behave when an origin is slow.

A V2 cache sheds what it has no room for, with 429 and Retry-After;
answers by the time a client's X-Pelican-Timeout says; and streams the
first bytes of a cold object before it has all of it. The clients
classify a throttled attempt, and a transfer too slow to finish, by
error code.

  shed-429         each V2 cache, sent a burst of cold reads, sheds some
                   with 429, Retry-After, and a reason; a missing object
                   is still 404
  client-throttled `pelican object get` and stash_plugin through a V2
                   cache that sheds them: a retryable error, then the
                   object
  timeout-header   each V2 cache answers 504 by the X-Pelican-Timeout of
                   a cold read, and goes on filling; it serves what it
                   holds, and ignores a malformed value
  streaming        each V2 cache sends the first bytes of a cold object
                   long before the last
  slow-transfer    the clients give up on a transfer slower than
                   Client.MinimumDownloadSpeed, with error 6002, direct
                   and through a cache

Nothing is slow as the shape is, so the suite makes it so for as long as
it runs: it restarts every origin with Origin.TransferRateLimit, and
every federated V2 cache with a small fair scheduler (Cache.WorkerCount,
Cache.Throttle.*), as the last of their configuration layers
(control.restart_with()), and afterward restores them. The rate limit
slows only a posixv2 origin (origin_serve/rate_limited_fs.go in
Pelican), so the suite needs posixv2 origins, a federated V2 cache, a
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

import contextlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
import traceback
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from testlib import common, control, director, stores, transfers, web
from testlib.common import Findings, die, first, warn
from testlib.report import FAIL, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

SCENARIOS = {
    "shed-429": "each V2 cache sheds a burst of cold reads with 429 and Retry-After, not a 404",
    "client-throttled": "the clients, shed by a V2 cache, report a retryable error and retry",
    "timeout-header": "each V2 cache answers 504 by X-Pelican-Timeout, and goes on filling",
    "streaming": "each V2 cache sends a cold object's first bytes long before its last",
    "slow-transfer": "the clients give up on a transfer too slow, with error 6002",
}

NAMESPACE = "protected-a"

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

# How long, after the restarts, every director may take to name every
# origin, and every cache to reach them.
SETTLE_WAIT = 150
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

ONLY_IF_CACHED = {"Cache-Control": "only-if-cached"}

# A row: its status, and its note.
Row = tuple[str, str]


class Test:
    """The run's objects in /protected-a, the token, and the shape.

    Attributes:
        base: The run's collection, within /protected-a.
        caps: /protected-a's capabilities.
        direct: Whether /protected-a takes direct clients.
        holder: The origin that holds the run's objects.
        v2: The federated V2 caches.
        tmp: A directory of the run's own, for the clients' files.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.base = f"data/throttling/{session.run}"
        self.token = session.token(NAMESPACE)
        self.token_file = session.token_file(NAMESPACE)
        self.setup = session.setup_token(NAMESPACE)
        self.caps = self.fed.exports[NAMESPACE]
        self.direct = "DirectReads" in self.caps
        self.origins = self.fed.origins
        self.holder = self.origins[0]
        self.v2 = [c for c in self.fed.federated_caches if c.kind == "v2"]
        self.tmp = session.path("throttling")
        os.makedirs(self.tmp, exist_ok=True)

    def rel(self, *parts: str) -> str:
        """Return where parts are, under the run's collection."""
        return "/".join([self.base, *parts])

    def url(self, server_url: str, rel: str) -> str:
        """Return rel's URL in /protected-a at the server at server_url."""
        return f"{server_url}/{NAMESPACE}/{quote(rel)}"

    def path(self, rel: str) -> str:
        """Return rel's path in the federation."""
        return f"/{NAMESPACE}/{rel}"

    def object_url(self, rel: str) -> str:
        """Return rel's pelican:// URL."""
        return f"{self.fed.url}{self.path(rel)}"

    def put(self, objects: Mapping[str, bytes], everywhere: bool = False) -> Optional[str]:
        """Write objects, by rel, straight into origins' stores on disk.

        They go into the first origin's store, or with everywhere into
        every origin's.

        Returns:
            What went wrong, or None.
        """
        targets = stores.unique(self.origins) if everywhere else [self.holder]
        for origin in targets:
            for rel, data in objects.items():
                why = stores.write_file(origin.store_of(NAMESPACE), rel, data)
                if why:
                    return why
        return None

    def ask(
        self,
        method: str,
        server_url: str,
        rel: str,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 120,
    ) -> web.Response:
        """Send method to rel at server_url's server, with the token."""
        return web.request(
            method,
            self.url(server_url, rel),
            token=self.token,
            headers=headers,
            timeout=timeout,
        )

    def holds(self, cache: common.Cache, rel: str) -> bool:
        """Return whether cache holds all of rel.

        It does if a HEAD with only-if-cached, which fetches nothing,
        gets 200. It is asked with the setup token: this is a check of
        the cache's state, not of the pass's tokens.
        """
        answer = web.request(
            "HEAD", self.url(cache.url, rel), token=self.setup, headers=ONLY_IF_CACHED
        )
        return answer.status == 200

    def wait_held(self, cache: common.Cache, rels: Sequence[str], seconds: float) -> list[str]:
        """Wait up to seconds for cache to hold each of rels.

        Returns:
            Those of rels that cache does not hold within seconds.
        """
        deadline = time.time() + seconds
        pending = list(rels)
        while pending:
            pending = [rel for rel in pending if not self.holds(cache, rel)]
            if not pending or time.time() >= deadline:
                break
            time.sleep(1)
        return pending


def leaf(rel: str) -> str:
    """Return rel's last segment, for a message."""
    return rel.rsplit("/", 1)[-1]


def served(answer: web.Response, data: bytes) -> Optional[str]:
    """Return why answer is not a whole object holding data, or None."""
    if answer.status != 200:
        return answer.describe()
    if answer.body != data:
        return f"{len(answer.body)} bytes, not the object"
    return None


def shed_reason(answer: web.Response) -> Optional[str]:
    """Return the `error` of a 429's JSON body, or None if it has none."""
    try:
        found = common.as_object(json.loads(answer.body)) or {}
    except ValueError:
        return None
    reason = found.get("error")
    return reason if isinstance(reason, str) else None


def shed_problem(answer: web.Response) -> Optional[str]:
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


def serving(test: Test) -> Optional[str]:
    """Return why /protected-a is not served as before, or None.

    Every director must name every origin for the probe, and every cache
    answer a HEAD of it, which fetches nothing. Each is asked with the
    setup token, since this is a check of the shape, not of the pass's
    tokens.
    """
    path = test.path(test.rel(PROBE))
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.setup)
        for origin in test.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request("HEAD", test.url(cache.url, test.rel(PROBE)), token=test.setup)
        if answer.status != 200 or answer.header("Content-Length") != str(PROBE_SIZE):
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def settle(test: Test) -> Optional[str]:
    """Wait until the federation serves /protected-a as before.

    Returns:
        Why it doesn't within SETTLE_WAIT seconds (see serving()), or
        None.
    """
    deadline = time.time() + SETTLE_WAIT
    why = serving(test)
    while why and time.time() < deadline:
        time.sleep(5)
        why = serving(test)
    return f"after {SETTLE_WAIT}s, {why}" if why else None


@contextlib.contextmanager
def slowed(test: Test) -> Generator[None, None, None]:
    """Restart the origins and the V2 caches slowed, for the with block.

    Every origin gets the rate limit, and every federated V2 cache the
    small scheduler; and afterward, each is restarted as it was.

    Raises:
        control.ControlError: Restarting, or restoring, failed.
    """
    with contextlib.ExitStack() as stack:
        for origin in test.origins:
            stack.enter_context(control.restart_with(origin.svc, {CONFIG_FILE: ORIGIN_CONFIG}))
        for cache in test.v2:
            stack.enter_context(control.restart_with(cache.svc, {CONFIG_FILE: CACHE_CONFIG}))
        yield


# --------------------------------------------------------------------------
# The cases. Each returns its row, or its rows by name (see CASES).


def burst(test: Test, cache: common.Cache, found: Findings) -> None:
    """Run shed-429 at one V2 cache (see shed_429())."""
    rels = [test.rel("shed-429", cache.svc, f"burst-{n}") for n in range(BURST)]
    missing = test.rel("shed-429", cache.svc, "missing")
    objects = {rel: os.urandom(BURST_SIZE) for rel in rels}
    why = test.put(objects)
    if why:
        found.problems.append(why)
        return

    def get_missing() -> web.Response:
        time.sleep(MISSING_DELAY)
        return test.ask("GET", cache.url, missing)

    with ThreadPoolExecutor(BURST + 1) as pool:
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
                found.problems.append(f"{cache.svc}: {leaf(rel)}: {problem}")
            reason = shed_reason(answer) or "none"
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        why = served(answer, objects[rel])
        if why:
            found.problems.append(
                f"{cache.svc}: {leaf(rel)}: {why}, neither the object nor 429"
            )
    counts = ", ".join(f"{n} {s}" for s, n in sorted(statuses.items()))
    if absent.status != 404:
        what = f"429 ({shed_reason(absent)})" if absent.status == 429 else absent.describe()
        found.problems.append(
            f"{cache.svc}: a missing object, read during the burst, got {what}, not 404"
        )
    if not reasons:
        found.problems.append(
            f"{cache.svc}: of {BURST} cold reads at once, none shed: {counts}"
        )
        return
    said = ", ".join(f"{n} {r}" for r, n in sorted(reasons.items()))
    found.notes.append(f"{cache.svc}: {counts} ({said})")


def shed_429(test: Test) -> Row:
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


def answered_in(answer: web.Response, took: float, status: int, within: float) -> Optional[str]:
    """Return why answer is not status within within seconds, or None.

    took is how many seconds answer took.
    """
    if answer.status != status:
        return f"{answer.describe()}, not {status}"
    if took > within:
        return f"{status}, but after {took:.1f}s, past {within:.0f}s"
    return None


def timed(
    test: Test, cache: common.Cache, rel: str, timeout: str
) -> tuple[web.Response, float]:
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
        test.rel("timeout-header", cache.svc, name)
        for name in ("slow", "queued", "held", "malformed")
    )
    objects = {slow: os.urandom(SLOW_SIZE)}
    objects.update({rel: os.urandom(SMALL_SIZE) for rel in (queued, held, odd)})
    why = test.put(objects)
    if why:
        for each in found.values():
            each.problems.append(why)
        return

    why = served(test.ask("GET", cache.url, held), objects[held])
    if why:
        found["held"].problems.append(f"{cache.svc}: a plain GET, to fill it, got {why}")
    elif test.wait_held(cache, [held], FILL_WAIT):
        found["held"].problems.append(
            f"{cache.svc}: did not hold an object within {FILL_WAIT}s of a whole read"
        )
    else:
        answer, took = timed(test, cache, held, HELD_TIMEOUT)
        why = served(answer, objects[held])
        if why:
            found["held"].problems.append(
                f"{cache.svc}: with X-Pelican-Timeout: {HELD_TIMEOUT}, what it holds got {why}"
            )
        else:
            found["held"].notes.append(f"{cache.svc}: in {took:.1f}s")

    answer, took = timed(test, cache, slow, SHORT_TIMEOUT)
    why = answered_in(answer, took, 504, SHORT_ANSWER)
    if why:
        found["short"].problems.append(
            f"{cache.svc}: a cold read with X-Pelican-Timeout: {SHORT_TIMEOUT} got {why}"
        )
    time.sleep(OCCUPY_DELAY)
    answer, took = timed(test, cache, queued, QUEUED_TIMEOUT)
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

    # The fetches go on, and finish, behind the 504s.
    for case, rel in (("short", slow), ("queued", queued)):
        if test.wait_held(cache, [rel], FILL_WAIT):
            found[case].problems.append(
                f"{cache.svc}: after the 504, the cache did not hold the object within"
                + f" {FILL_WAIT}s: it gave up the fetch"
            )
        else:
            why = served(test.ask("GET", cache.url, rel), objects[rel])
            if why:
                found[case].problems.append(f"{cache.svc}: once held, a GET got {why}")

    answer, _ = timed(test, cache, odd, MALFORMED_TIMEOUT)
    why = served(answer, objects[odd])
    if why:
        found["malformed"].problems.append(
            f"{cache.svc}: with X-Pelican-Timeout: {MALFORMED_TIMEOUT}, a cold read got {why}"
        )


# timeout-header's cases, in the order of their rows.
TIMEOUT_CASES = ("short", "queued", "held", "malformed")


def timeout_header(test: Test) -> dict[str, Row]:
    """Check that each V2 cache heeds X-Pelican-Timeout.

    The header says how long a client will wait (serveObject in
    local_cache/persistent_cache_api.go):

      short      a cold read with 20ms must get 504 at once
      queued     a cold read with 2s, sent while another's fetch holds
                 the origin's one slot, so that its own waits, must get
                 504 within about a second
      held       what the cache holds, with 5s, must come back
      malformed  a cold read with a value that is no duration must come
                 back, the value ignored

    For `short` and `queued`, the fetch must go on behind the 504: the
    cache must then hold the object, and serve it.
    """
    found = {case: Findings() for case in TIMEOUT_CASES}
    for cache in test.v2:
        timeouts(test, cache, found)
    return {case: found[case].row(f"{len(test.v2)} cache(s)") for case in TIMEOUT_CASES}


def streaming(test: Test) -> Row:
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
        why = test.put({rel: data})
        if why:
            found.problems.append(why)
            continue
        read = web.timed_get(test.url(cache.url, rel), test.token, STREAM_HEAD, timeout=120)
        why = served(read.response, data)
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
    data: Optional[bytes]

    def codes(self) -> set[int]:
        """Return the Pelican error codes it reported."""
        found = re.findall(r"Error code (\d{4})|PelicanErrorCode = (\d+)", self.said)
        return {int(a or b) for a, b in found}

    def leftovers(self) -> Optional[str]:
        """Return why a failed download left something behind, or None.

        The clients write a temporary file beside the object's, and
        rename it once it is complete.
        """
        return f"it left {list(self.left)}" if self.left else None

    def got(self, data: bytes) -> Optional[str]:
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
# The error codes of an attempt that a cache shed: Transfer.OriginUnresponsive,
# Transfer.OriginSlow, and Transfer.CacheOverloaded (docs/error_codes.yaml).
THROTTLED = {6008, 6009, 6010}
# The exit status of a client that failed in a way it may retry: the
# plugin's `Retryable` (cmd/plugin.go), which `pelican object get` shares.
RETRYABLE_EXIT = 11


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
    data: Optional[bytes] = None
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
    cache: Optional[common.Cache] = None,
) -> Fetched:
    """Run `pelican object get` of rel into a directory of its own, name.

    It runs with SLOW_ENV if slow; and reads from an origin if direct,
    or through cache alone (--cache), if one is named.
    """
    directory = os.path.join(test.tmp, name)
    os.makedirs(directory)
    args = ["object", "get", "--token", test.token_file]
    if direct:
        args.append("--direct")
    if cache:
        args += ["--cache", cache.url]
    args += [test.object_url(rel), os.path.join(directory, "object")]
    status, out, err = test.session.pelican_cmd(
        *args, env=client_env(test, slow), cwd=directory
    )
    return fetched(directory, status, out + err)


def get_plugin(
    test: Test,
    name: str,
    rel: str,
    slow: bool,
    direct: bool = False,
    cache: Optional[common.Cache] = None,
) -> Fetched:
    """Run stash_plugin on rel, into a directory of its own, name.

    It takes what get_pelican() takes; a direct read adds ?directread to
    the URL, and a cache is named by Client.PreferredCaches.
    """
    directory = os.path.join(test.tmp, name)
    creds = os.path.join(test.tmp, f"{name}.creds")
    os.makedirs(directory)
    os.makedirs(creds)
    shutil.copyfile(test.token_file, os.path.join(creds, "server.use"))
    infile, outfile = (os.path.join(test.tmp, f"{name}.{ext}") for ext in ("in", "ad"))
    url = test.object_url(rel) + (transfers.DIRECT_QUERY if direct else "")
    with open(infile, "w", encoding="utf-8") as f:
        f.write(f'[ Url="{url}"; LocalFileName="object" ]\n')
    env = client_env(test, slow)
    env["_CONDOR_CREDS"] = creds
    if cache:
        env["PELICAN_CLIENT_PREFERREDCACHES"] = cache.url
    try:
        done = subprocess.run(  # nosec B603
            [test.session.plugin, "-infile", infile, "-outfile", outfile],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLIENT_TIMEOUT,
            check=False,
        )
        status, output = done.returncode, done.stderr or done.stdout
    except subprocess.TimeoutExpired:
        status, output = -1, f"timed out after {CLIENT_TIMEOUT:.0f}s"
    try:
        with open(outfile, encoding="utf-8") as f:
            ads = f.read()
    except FileNotFoundError:
        ads = ""
    return fetched(directory, status, ads or output)


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

    It must have failed with one of codes, in a way it may retry: it
    says so, and exits RETRYABLE_EXIT (cmd/object_get.go).
    """
    problems: list[str] = []
    if fetch.status == 0:
        return ["it succeeded"]
    if not fetch.codes() & codes:
        problems.append(f"it failed, but not with {' or '.join(map(str, sorted(codes)))}:")
        problems[-1] += f" {last_line(fetch.said)}"
    if fetch.status != RETRYABLE_EXIT:
        problems.append(f"exit {fetch.status}, not {RETRYABLE_EXIT} (retryable)")
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
    why = test.put(objects)
    if why:
        for each in found.values():
            each.problems.append(why)
        return

    shed: dict[str, Fetched] = {}
    with ThreadPoolExecutor(2) as pool:
        first_read = pool.submit(test.ask, "GET", cache.url, slot)
        time.sleep(OCCUPY_DELAY)
        second_read = pool.submit(test.ask, "GET", cache.url, queue)
        time.sleep(OCCUPY_DELAY)
        for client, get in CLIENTS.items():
            shed[client] = get(
                test, f"shed-{client}-{cache.svc}", rels[client], False, cache=cache
            )
        occupied = [(slot, first_read.result()), (queue, second_read.result())]
    for rel, answer in occupied:
        why = served(answer, objects[rel])
        if why:
            for each in found.values():
                each.doubts.append(
                    f"{cache.svc}: the read to fill its queue, {leaf(rel)}, got {why}"
                )

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


def client_throttled(test: Test) -> dict[str, Row]:
    """Check that the clients, shed by a V2 cache, report a retryable error.

    At each V2 cache, a cold read holds the origin's one slot, and
    another its one place in the queue, so that the cache sheds the
    next. Meanwhile, `pelican object get` and stash_plugin each download
    a cold object through that cache alone. Each must fail as the cache
    shed it, with Transfer.OriginUnresponsive, OriginSlow, or
    CacheOverloaded (6008-6010), in a way it may retry (exit 11), and
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


def slow_transfer(test: Test) -> dict[str, Row]:
    """Check that the clients give up on transfers too slow, and only those.

    The transfers are of objects of SLOW_TRANSFER_SIZE:

    direct         `pelican object get --direct`, with SLOW_ENV, must
                   fail with Transfer.SlowTransfer (6002), and leave
                   nothing (handle_http.go's downloadHTTP)
    plugin-direct  stash_plugin, with ?directread, likewise, and exit
                   11, its ad saying TransferRetryable = true
    default        each client, with the default thresholds, must get
                   the object: direct, or where the origins take no
                   direct clients, through the first V2 cache
    cache          `pelican object get` through each V2 cache, cold,
                   with SLOW_ENV, must fail as `direct` does
    """
    rows: dict[str, Row] = {}
    slow = test.rel("slow-transfer", "direct")
    data = os.urandom(SLOW_TRANSFER_SIZE)
    via = {c.svc: test.rel("slow-transfer", c.svc) for c in test.v2}
    defaults = {c: test.rel("slow-transfer", f"default-{c}") for c in CLIENTS}
    objects = {slow: data}
    objects.update(
        {rel: os.urandom(SLOW_TRANSFER_SIZE) for rel in [*via.values(), *defaults.values()]}
    )
    why = test.put(objects)
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
        rows[name] = (FAIL, first(problems)) if problems else (PASS, "error 6002")

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

    found = Findings()
    for cache in test.v2:
        fetch = get_pelican(test, f"slow-cache-{cache.svc}", via[cache.svc], True, cache=cache)
        if fetch.status == 0:
            found.problems.append(f"{cache.svc}: it got the object, under the minimum speed")
            continue
        found.problems += [
            f"{cache.svc}: {p}" for p in pelican_problems(fetch, {SLOW_TRANSFER})
        ]
    rows["cache"] = found.row(f"{len(test.v2)} cache(s), error 6002")
    return rows


# --------------------------------------------------------------------------
# Running the cases.


def single(case: Callable[[Test], Row]) -> Callable[[Test], dict[str, Row]]:
    """Return case, of a scenario with one row, as CASES takes it."""
    return lambda test: {"": case(test)}


# What runs each scenario: its rows, each by its name after the
# scenario's ("" for the scenario's only row).
CASES: dict[str, Callable[[Test], dict[str, Row]]] = {
    "shed-429": single(shed_429),
    "client-throttled": client_throttled,
    "timeout-header": timeout_header,
    "streaming": single(streaming),
    "slow-transfer": slow_transfer,
}


def row_name(scenario: str, name: str) -> str:
    """Return the row name of scenario's case name."""
    return f"{scenario} {name}" if name else scenario


def run_scenario(test: Test, scenario: str, results: common.Results) -> None:
    """Run scenario, and record its rows."""
    try:
        rows = CASES[scenario](test)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        rows = {"": (FAIL, f"{type(e).__name__}: {e}")}
    for name, (status, note) in rows.items():
        results.add(row_name(scenario, name), status, note)


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None.

    There is nothing with origins that the rate limit does not slow, the
    tiny topology, no /protected-a, or no federated V2 cache.
    """
    if fed.origin_variant != "posixv2":
        return "Origin.TransferRateLimit slows only a posixv2 origin"
    if fed.tiny:
        return (
            "the tiny topology's servers are one process, which slowing the origin would slow"
        )
    if NAMESPACE not in fed.exports:
        return "this shape does not export /protected-a"
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
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of(NAMESPACE)}/data/throttling"
        if not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.setup_tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(
            fed.origins, fed.exports, "data/throttling", tokens
        )
        if errors:
            warn(f"emptying data/throttling {when}: {first(errors)}")

    clean("before the run")
    test = Test(session)
    print(f"Throttling test against {fed.url}, objects under /{NAMESPACE}/{test.base}/")
    done: set[str] = set()
    try:
        why = test.put({test.rel(PROBE): os.urandom(PROBE_SIZE)}, everywhere=True)
        if why:
            die(f"could not put the probe in place: {why}")
        print(f"\nRestarting the origins limited to {RATE_LIMIT}, and the V2 caches throttled")
        try:
            with slowed(test):
                why = settle(test)
                for scenario in selected:
                    if why:
                        results.add(scenario, FAIL, f"with the servers restarted slowed, {why}")
                    else:
                        run_scenario(test, scenario, results)
                    done.add(scenario)
        except control.ControlError as e:
            # Restarting, or restoring, failed: what is left unrun fails.
            for scenario in selected:
                if scenario not in done:
                    results.add(scenario, FAIL, f"restarting the servers: {e}")
            warn(f"restarting the servers slowed, or restoring them: {e}")
        why = settle(test)
        if why:
            warn(f"with the servers restored, {why}")
    finally:
        clean("after the run")
