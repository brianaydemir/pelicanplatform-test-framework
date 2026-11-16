"""The tests' own helpers, against the running federation.

Each scenario checks that a helper does what the suites rely on, or, in
`confined`, the dev container's mounts. Each but `pool`, `confined`,
and `token-leaks` reads one object, which the suite puts under
/protected-a/data/selftest/<run>/ and removes. See
docs/tests/self-tests.md.
"""

import errno
import functools
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from testlib import common, control, credentials, director, jwt, leaks, metrics, stores, suite, web
from testlib.common import die, first, outcome
from testlib.credentials import R
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session
from testlib.suite import NAMESPACE

# `restart-cache` presents the setup token alone, since its reads are
# how it fills the cache and sees it serve again, not what it checks; so
# it runs in the WLCG pass alone (see suites/__init__.py), and cache-0
# is restarted once. `pool` and `confined` present no token, and
# `token-leaks` presents none that it checks.
ONE_PROFILE = ("restart-cache", "pool", "confined", "token-leaks")

SCENARIOS = {
    "trailers": "a V2 cache's X-Transfer-Status trailer comes back when asked for, and only then",
    "jwt": (
        "a token jwt.sign() signs with the origins' key is taken, and refused once expired;"
        " jwt.verify() finds its key in the origins' JWKS, and in no other"
    ),
    "metrics": "a GET straight to a native origin raises its pelican_http_requests_total",
    "net-bytes": "testlib.control.sent_bytes() counts what an origin sends for a GET, and little more",
    "restart-cache": (
        "cache-0, restarted mid-run, is named by every director again, and serves what"
        " it held from its store"
    ),
    "pool": (
        "testlib.common.pool() drops the tasks it has not started when an abandoned pass"
        " leaves it, or interrupts its wait for them, and waits for every one otherwise"
    ),
    "confined": (
        "the dev container cannot write the checkout, nor what of framework/var the host"
        " runs or reads as code, nor the directories that hold the services' bind sources"
    ),
    "token-leaks": (
        "testlib.leaks finds a token whose signature is whole, where it counts, and no other;"
        " testlib.web reports to it, and the control helper reads the servers' logs"
    ),
}


# What a V2 cache sends in the trailer of a transfer that went well
# (local_cache/persistent_cache_api.go in Pelican).
TRANSFER_OK = "200: OK"

REQUESTS = "pelican_http_requests_total"

# The cache that `restart-cache` restarts.
RESTARTED = "cache-0"
# How long it waits for the cache to serve the object again, in seconds:
# a cache just started refuses a namespace's tokens until it has learned
# the namespace's issuers.
SERVED_WAIT = 300

# The issuer of `token-leaks`' tokens, which is no one's; and how far back
# it reads the servers' logs, in seconds.
LEAK_ISSUER = "https://selftest-leaks.invalid"
LEAK_LOG_WINDOW = 300


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None.

    There is nothing without /protected-a.
    """
    return suite.unexported(fed)


class Test(suite.Test):
    """The object that the scenarios read, and a read token for it.

    Attributes:
        object: The object's rel.
        data: The object's bytes.
        read_token: A read token for /protected-a, of the session's
            profile.
        read_setup: A read token for /protected-a, of the WLCG profile,
            for what is setup rather than the check (`restart-cache`).
    """

    def __init__(self, session: Session):
        super().__init__(session, "selftest")
        self.object = self.rel("object")
        self.data = os.urandom(200_000)
        self.read_token = session.token(NAMESPACE, R)
        self.read_setup = session.setup_token(NAMESPACE, R)


def trailers(test: Test) -> suite.Row:
    """Check that testlib.web keeps a chunked body's trailers.

    A GET of the object at each V2 cache, with `X-Transfer-Status: true`
    and `TE: trailers`, must announce the trailer
    (`Trailer: X-Transfer-Status`) and end with it, saying 200: OK;
    without them, no trailer may come.
    """
    caches = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not caches:
        return SKIP, "this shape has no federated V2 cache"
    asked = {"X-Transfer-Status": "true", "TE": "trailers"}
    problems: list[str] = []
    for cache in caches:
        url = test.url(cache.url, test.object)
        answer = web.request("GET", url, token=test.read_token, headers=asked)
        why = web.served(answer, test.data)
        announced = [n.strip().lower() for n in answer.header("Trailer").split(",")]
        if why:
            problems.append(f"{cache.svc}: asked for the trailer, GET got {why}")
        elif "x-transfer-status" not in announced:
            problems.append(f"{cache.svc}: Trailer is '{answer.header('Trailer')}'")
        elif answer.trailer("X-Transfer-Status") != TRANSFER_OK:
            got = answer.trailer("X-Transfer-Status") or "none"
            problems.append(f"{cache.svc}: X-Transfer-Status trailer is '{got}'")
        answer = web.request("GET", url, token=test.read_token)
        why = web.served(answer, test.data)
        if why:
            problems.append(f"{cache.svc}: unasked, GET got {why}")
        elif answer.trailers or answer.header("Trailer"):
            problems.append(f"{cache.svc}: unasked, it sent trailers {answer.trailers}")
    return outcome(problems, f"{len(caches)} cache(s): '{TRANSFER_OK}' when asked, nothing otherwise")


def claims_problems(ours: dict[str, object], theirs: dict[str, object], profile: str) -> list[str]:
    """Return how claims ours differ from theirs.

    The claims that differ from token to token are left out.

    Args:
        ours: Claims from credentials.server_claims().
        theirs: The claims of a token that `pelican token create` minted
            with the same arguments.
        profile: The profile of both.
    """
    problems: list[str] = []
    if set(ours) != set(theirs):
        problems.append(f"claims {sorted(ours)}, not token create's {sorted(theirs)}")
    for name in ("aud", "iss", "sub", credentials.VERSIONS[profile][0]):
        if ours.get(name) != theirs.get(name):
            problems.append(f"{name} {ours.get(name)!r}, not {theirs.get(name)!r}")
    if set(str(ours.get("scope")).split()) != set(str(theirs.get("scope")).split()):
        problems.append(f"scope {ours.get('scope')!r}, not {theirs.get('scope')!r}")
    return problems


def jwt_tokens(test: Test) -> suite.Row:
    """Check that testlib.jwt signs tokens that the origins take.

    A token that jwt.sign() signs with the origins' key, with the
    claims and header of the `server` token that `pelican token create`
    mints (credentials.server_claims(), jwt.header(), jwt.key_id()),
    must read the object at every origin; the same token, expired ten
    minutes ago (well past native servers' 60 seconds of clock skew),
    must be refused. jwt.verify() must find the token's key, by its kid,
    in the first origin's published JWKS (/.well-known/issuer.jwks), and
    none in a JWKS of the `unknown` test key alone.
    """
    if not test.direct:
        return SKIP, f"/{NAMESPACE} takes no direct clients"
    fed = test.fed
    key = fed.origin_key
    head = jwt.header(jwt.algorithm(key), jwt.key_id(key))
    profile = test.session.profile
    claims = credentials.server_claims(fed, NAMESPACE, R, profile=profile)
    minted_head, minted = jwt.decode(test.read_token)
    problems = claims_problems(claims, minted, profile)
    if head != minted_head:
        problems.append(f"header {head}, not token create's {minted_head}")
    if problems:
        return FAIL, "unlike token create's: " + first(problems)
    token = jwt.sign(claims, key)
    now = int(time.time())
    stale = {**claims, "iat": now - 1200, "nbf": now - 1200, "exp": now - 600}
    expired = jwt.sign(stale, key)
    for origin in stores.unique(fed.origins):
        url = test.url(origin.url, test.object)
        why = web.served(web.request("GET", url, token=token), test.data)
        if why:
            return FAIL, f"{origin.svc}: GET with the token got {why}"
        answer = web.request("GET", url, token=expired)
        if answer.status not in (401, 403):
            return FAIL, f"{origin.svc}: GET with the expired token got {answer.describe()}"
    why = verify_problem(fed, token, str(head["kid"]))
    if why:
        return FAIL, why
    return PASS, f"{len(stores.unique(fed.origins))} origin(s); kid {head['kid']}"


def verify_problem(fed: common.Federation, token: str, kid: str) -> str | None:
    """Return why jwt.verify() is wrong about token, or None if it is not.

    It is wrong if it does not find token's key, kid, in the first
    origin's published JWKS, or finds one in a JWKS of the `unknown` test
    key alone.
    """
    origin = fed.origins[0]
    jwks = web.request("GET", f"{origin.web_url}/.well-known/issuer.jwks")
    if not jwks.ok:
        return f"{origin.svc}'s JWKS: {jwks.describe()}"
    found = jwt.verify(token, jwks.body)
    if found != kid:
        return f"jwt.verify() found {found!r} in {origin.svc}'s JWKS, not {kid!r}"
    other = json.dumps({"keys": [jwt.public_jwk(credentials.UNKNOWN_KEY)]})
    found = jwt.verify(token, other.encode())
    if found is not None:
        return f"jwt.verify() found {found!r} in a JWKS of the unknown key alone"
    return None


def http_requests(test: Test) -> suite.Row:
    """Check that testlib.metrics reads /metrics.

    A GET of the object straight to each native origin must raise its
    pelican_http_requests_total{server_type="origin",method="GET",
    code="200"} by at least 1 (metrics/http_metrics.go in Pelican);
    other requests may raise it too.
    """
    fed = test.fed
    if not fed.native_origins:
        return SKIP, f"XRootD serves the origins ({fed.origin_variant}), not Pelican"
    if not test.direct:
        return SKIP, f"/{NAMESPACE} takes no direct clients"
    labels = {"server_type": "origin", "method": "GET", "code": "200"}
    notes: list[str] = []
    for origin in stores.unique(fed.origins):
        before = metrics.fetch(origin.web_url)
        if before is None:
            return FAIL, f"{origin.svc}: GET {origin.web_url}/metrics failed"
        answer = web.request("GET", test.url(origin.url, test.object), token=test.read_token)
        why = web.served(answer, test.data)
        if why:
            return FAIL, f"{origin.svc}: GET of the object got {why}"
        after = metrics.fetch(origin.web_url)
        if after is None:
            return FAIL, f"{origin.svc}: GET {origin.web_url}/metrics failed afterward"
        rise = metrics.delta(before, after, REQUESTS, **labels)
        if rise < 1:
            return FAIL, f"{origin.svc}: {REQUESTS} for a GET rose by {rise:g}, not 1 or more"
        notes.append(f"{origin.svc} +{rise:g}")
    return PASS, ", ".join(notes)


# How much more than an object an origin's container may send for a GET of
# it (`net-bytes`): headers, TLS, and whatever else it sends meanwhile.
NET_SLACK = 64 << 10


def net_bytes(test: Test) -> suite.Row:
    """Check that testlib.control counts the bytes a container sends.

    A GET of the object straight to each origin must raise the kernel's
    count of what its container sent (control.sent_bytes()) by at least
    the object's size, and by no more than NET_SLACK beyond it.
    """
    fed = test.fed
    if fed.tiny:
        return SKIP, "the tiny topology's origin shares its container with every other server"
    if not test.direct:
        return SKIP, f"/{NAMESPACE} takes no direct clients"
    why = control.unavailable()
    if why:
        return SKIP, why
    notes: list[str] = []
    for origin in fed.origins:
        before = control.sent_bytes([origin.svc])
        answer = web.request("GET", test.url(origin.url, test.object), token=test.read_token)
        why = web.served(answer, test.data)
        if why:
            return FAIL, f"{origin.svc}: GET of the object got {why}"
        rise = control.sent_bytes([origin.svc]) - before
        if not len(test.data) <= rise <= len(test.data) + NET_SLACK:
            return FAIL, f"{origin.svc} sent {rise} bytes for a GET of {len(test.data)}"
        notes.append(f"{origin.svc} +{rise}")
    return PASS, ", ".join(notes)


def restart_cache(test: Test) -> suite.Row:
    """Check that testlib.control restarts a service mid-run.

    Read the object through cache-0 until it holds it whole (a V2 cache
    sends Age then, and only then), and see every director name it;
    restart it (testlib.control). Within suite.SETTLE_WAIT seconds every
    director must name it again: the directors drop a cache that says,
    as it stops, that it is shutting down, until it advertises again.
    Within SERVED_WAIT it must serve the object; a V2 cache from its
    store, with an Age at least the seconds since it first said it held
    it all, which only the record it kept across the restart can give.
    """
    fed = test.fed
    if fed.standalone:
        return SKIP, "a standalone origin has no cache"
    if fed.tiny:
        return SKIP, "the tiny topology's cache is the whole federation's process"
    cache = next((c for c in fed.federated_caches if c.svc == RESTARTED), None)
    if cache is None:
        return SKIP, f"this shape has no federated {RESTARTED}"
    why = control.unavailable()
    if why:
        return SKIP, why
    url = test.url(cache.url, test.object)
    path = test.path(test.object)
    host = director.host(cache.url)
    v2 = cache.kind == "v2"
    why = web.served(web.request("GET", url, token=test.read_setup), test.data)
    if why:
        return FAIL, f"before the restart, GET got {why}"
    held = 0.0
    if v2:
        if not test.wait_held(cache, test.object):
            return FAIL, f"before the restart, no Age within {suite.HELD_WAIT}s of a whole read"
        held = time.time()
    why = director.unnamed(fed.directors, "object", path, host)
    if why:
        return FAIL, f"before the restart, {why}"

    control.restart(cache.svc)
    restarted = time.time()
    why = director.wait_named(fed.directors, "object", path, host, suite.SETTLE_WAIT)
    if why:
        return FAIL, f"after the restart, {why}"
    took = time.time() - restarted

    answer = common.wait_last(
        lambda: web.request("GET", url, token=test.read_setup), lambda got: got.ok, SERVED_WAIT, 5
    )
    why = web.served(answer, test.data)
    if why:
        return FAIL, f"after the restart, GET got {why} (waited up to {SERVED_WAIT}s)"
    note = f"{cache.svc}: every director named it again {took:.0f}s after its restart"
    if not v2:
        return PASS, f"{note}, and it serves the object"
    since = int(time.time() - held)
    found = web.age(answer)
    if found is None:
        return FAIL, "after the restart, it served the object with no Age, as if it held none"
    # Age is whole seconds, rounded down, and the clocks are one host's.
    if found < since - 1:
        return FAIL, (
            f"after the restart, Age {found}, but it said it held the object {since}s"
            " before, so it fetched it again"
        )
    return PASS, f"{note}, and serves the object from its store (Age {found})"


class Abandoned(BaseException):
    """What `pool` abandons a pool's block with, as test.py's Stalled does."""


def pool(_test: Test) -> suite.Row:
    """Check what testlib.common.pool() does as its block ends.

    Ten tasks are queued on a pool of two workers, each of which holds
    its worker until it is let go. Abandoned with two running, by an
    exception that no suite catches, which a signal raises as test.py's
    watchdog does, the block must cancel the eight not started, and
    return at once; the two then end on their own. It is abandoned once
    within the block, and once as the block, ended as usual, waits for
    its tasks. Left as usual, it must wait for every task.
    """
    problems: list[str] = []
    took: list[float] = []
    for where in ("in its block", "as it waited"):
        found, seconds = abandon(in_wait=where == "as it waited")
        problems += [f"abandoned {where}, {p}" for p in found]
        took.append(seconds)
    done: list[int] = []
    with common.pool(2) as workers:
        for n in range(10):
            workers.submit(done.append, n)
    if sorted(done) != list(range(10)):
        problems.append(f"left as usual, it had run only tasks {sorted(done)} of 10 as it ended")
    return outcome(
        problems,
        "abandoned in its block, and as it waited, it cancelled 8 queued tasks"
        f" each time, and ended in {took[0]:.1f}s and {took[1]:.1f}s",
    )


def abandon(in_wait: bool) -> tuple[list[str], float]:
    """Abandon a pool's block with 2 of its 10 tasks running (see pool()).

    Args:
        in_wait: Whether the block ends as usual, to be abandoned as it
            waits for its tasks; otherwise, it is abandoned within.

    Returns:
        What went wrong, and how long the block took to end once
        abandoned.
    """
    lock = threading.Lock()
    started: list[int] = []
    release = threading.Event()
    sent: list[float] = []
    main = threading.get_ident()

    def task(n: int) -> int:
        with lock:
            started.append(n)
        release.wait(30)
        return n

    def interrupt() -> None:
        common.wait_for(lambda: len(started) == 2, 10, interval=0.05)
        # For the block to queue every task and, if it is to, end.
        time.sleep(0.5)
        sent.append(time.monotonic())
        signal.pthread_kill(main, signal.SIGUSR1)

    def abandoned(_signum: int, _frame: Any) -> None:
        raise Abandoned()

    futures: list[Future[int]] = []
    took = float("inf")
    previous = signal.signal(signal.SIGUSR1, abandoned)
    interrupter = threading.Thread(target=interrupt)
    interrupter.start()
    try:
        with common.pool(2) as workers:
            futures = [workers.submit(task, n) for n in range(10)]
            if not in_wait:
                time.sleep(30)
    except Abandoned:
        took = time.monotonic() - sent[0]
    finally:
        # Ignored first, so that a signal sent late does no harm.
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        interrupter.join()
        signal.signal(signal.SIGUSR1, previous)
    release.set()
    problems: list[str] = []
    cancelled = sum(f.cancelled() for f in futures)
    if cancelled != 8:
        problems.append(f"with 2 tasks running, it cancelled {cancelled} of 8 queued")
    if took > 5:
        problems.append(f"its block took {took:.1f}s to end, waiting for its tasks")
    ran = [f.result(30) for f in futures if not f.cancelled()]
    if sorted(started) != sorted(ran) or len(started) != 2:
        problems.append(f"it started tasks {sorted(started)}, and ran {sorted(ran)}")
    return problems, took


# What `confined` finds read-only, under the top of the checkout: the
# checkout itself, so that nothing in the dev container can change what
# the host runs or the framework's code (see the dev service in
# docker-compose.yaml); the host's own binaries, and what fed.sh generates
# and reads; and the directories that hold the services' bind sources,
# which a symlink must not replace.
CONFINED_FILES = ("fed.sh", "framework/testlib/common.py")
CONFINED_DIRS = (
    ".",
    "framework/testlib",
    "framework/var",
    "framework/var/bin",
    "framework/var/bin/host",
    "framework/var/generated",
    "framework/var/control",
    "framework/var/data",
    "framework/var/data/origin",
    "framework/var/issuer-keys",
)


def refused_write(shown: str, opener: Callable[[], int], made: str | None = None) -> str | None:
    """Return why opening something to write was not refused as read-only.

    Args:
        shown: What it is, for the message.
        opener: Opens it, and returns the file descriptor.
        made: The file that opener creates, if it does, to remove.

    Returns:
        None if it was refused with EROFS; otherwise the problem.
    """
    try:
        fd = opener()
    except OSError as e:
        return None if e.errno == errno.EROFS else f"{shown}: {e.strerror}, not a read-only file system"
    os.close(fd)
    if made is not None:
        os.unlink(made)
    return f"{shown} is writable from the dev container"


def confined(_test: Test) -> suite.Row:
    """Check that the dev container cannot write what it must not.

    Each file of CONFINED_FILES is opened to append to, and a new file is
    made in each directory of CONFINED_DIRS: each must fail with EROFS.
    Nothing is written; a file made is removed.
    """
    top = os.path.dirname(common.FRAMEWORK)
    problems: list[str] = []
    for rel in CONFINED_FILES:
        path = os.path.join(top, rel)
        problem = refused_write(rel, functools.partial(os.open, path, os.O_WRONLY | os.O_APPEND))
        if problem:
            problems.append(problem)
    for rel in CONFINED_DIRS:
        path = os.path.join(top, rel, f".confined-{os.getpid()}")
        shown = "the checkout" if rel == "." else f"{rel}/"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        problem = refused_write(shown, functools.partial(os.open, path, flags, 0o600), path)
        if problem:
            problems.append(problem)
    checked = len(CONFINED_FILES) + len(CONFINED_DIRS)
    return outcome(problems, f"all {checked} files and directories are read-only")


def leak_tokens(test: Test) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return a token whole, the others that must be found, and those not.

    Each is of claims that grant nothing anywhere: a scope of a path no
    namespace has, from an issuer that is no one's.

    Returns:
        The token, signed with the origins' key; the tokens whose
        signatures are whole, by what they are; and those whose
        signatures are not, likewise.
    """
    claims = jwt.wlcg_claims("storage.read:/selftest-leaks", LEAK_ISSUER, jwt.ANY_AUDIENCE, "selftest", 600)
    whole = jwt.sign(claims, test.fed.origin_key)
    found = {
        "signed with the origins' key": whole,
        "signed by HS256": jwt.encode(jwt.header(jwt.HS256), claims, secret=os.urandom(32)),
    }
    head = jwt.header(jwt.ES256)
    not_found = {
        "cut out, as Pelican's logs cut it": f"{leaks.unsigned(whole)}.REDACTED",
        "cut short": whole[:-8],
        "of alg none": jwt.encode(jwt.header(jwt.NONE), claims),
        "HS256's under an ES256 header": jwt.encode(head, claims, secret=os.urandom(32), alg=jwt.HS256),
    }
    return whole, found, not_found


def leak_answers(whole: str) -> tuple[dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]]:
    """Return answers in which leaks.Watch must find whole, and those not.

    Each is Watch.answer()'s arguments, by what it is.
    """
    cache, origin = "https://cache-0:8443", "https://origin-0:8443"
    detail = f'{{"detail":"Get \\"{origin}/x?access_token={whole}\\": no such host"}}'.encode()
    status = (("X-Transfer-Status", f"500: {detail.decode()}"),)
    found: dict[str, tuple[Any, ...]] = {
        "a 500's body": ("GET", f"{cache}/x", {}, 500, (), detail, ()),
        "a 403's header": ("GET", f"{origin}/x", {}, 403, (("WWW-Authenticate", whole),), b"", ()),
        "a 206's trailer": ("GET", f"{cache}/x", {}, 206, (), b"", status),
        "a 200's X-Transfer-Status header": ("GET", f"{cache}/x", {}, 200, status, b"", ()),
        "a third-party COPY's 201": ("COPY", f"{origin}/x", {}, 201, (), b"failure: " + detail, ()),
        "a prestage's 200": ("GET", f"{cache}/pelican/api/v1.0/prestage", {}, 200, (), detail, ()),
        "a health 200": ("GET", f"{origin}/api/v1.0/metrics/health", {}, 200, (), detail, ()),
    }
    passed_over: dict[str, tuple[Any, ...]] = {
        "a GET's 200": ("GET", f"{cache}/x", {}, 200, (), detail, ()),
        "a 307's Location": ("GET", "https://director-0:8443/x", {}, 307, (("Location", whole),), b"", ()),
        "the identity provider's 500": ("GET", "https://idp:8443/token", {}, 500, (), detail, ()),
    }
    return found, passed_over


def leak_commands(test: Test, whole: str) -> tuple[dict[str, tuple[Any, ...]], dict[str, tuple[Any, ...]]]:
    """Return commands in whose output leaks.Watch must find whole, and not.

    Each is Watch.command()'s arguments, by what it is. The files that
    they name are made in the scenario's directory.
    """
    directory = test.directory("token-leaks")
    for name in ("stats.json", "result.ad"):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(f'TransferError = "failed: {whole}"\n')
    get = ["pelican", "object", "get"]
    found: dict[str, tuple[Any, ...]] = {
        "a get's stderr": ([*get, "x", "."], directory, "", whole),
        "`job status`'s stdout": (["pelican", "job", "status", "x"], directory, whole, ""),
        "`origin introspect`'s stdout": (["pelican-server", "origin", "introspect"], directory, whole, ""),
        "a get's --transfer-stats": ([*get, "--transfer-stats", "stats.json", "x", "."], directory, "", ""),
        "the plugin's ads": (["stash_plugin", "-infile", "in", "-outfile", "result.ad"], directory, "", ""),
    }
    passed_over: dict[str, tuple[Any, ...]] = {
        "`token create`'s stdout": (["pelican", "token", "create", "x"], directory, whole, ""),
        "a get's stdout": ([*get, "x", "."], directory, whole, ""),
    }
    return found, passed_over


def token_leaks(test: Test) -> suite.Row:
    """Check that testlib.leaks finds tokens whole where they count.

    find() must find a token jwt.sign() signs with the origins' key, and
    one signed by HS256, and none whose signature is cut out, as
    Pelican's logs cut it, cut short, of `alg` none, or of another
    algorithm's length than its header names. A Watch of the scenario's
    own must find the first in each part of an answer, and of a client
    command's output, that counts (leak_answers(), leak_commands()), and
    in no other; and say of one that the request presented that it was
    the request's own. A GET of a missing object straight to origin-0
    must be reported to leaks.WATCH, as testlib.web reports every
    answer; and with a control helper, control.token_leaks() must read
    the last few minutes of every Pelican server's log, origin-0's among
    them, and find some lines there.
    """
    problems: list[str] = []
    whole, found, not_found = leak_tokens(test)
    for what, token in found.items():
        if leaks.find(f"x {token} y") != [token]:
            problems.append(f"find() missed a token {what}")
    for what, token in not_found.items():
        if leaks.find(f"x {token} y"):
            problems.append(f"find() took a token {what} for one whole")
    counted, passed_over = leak_answers(whole)
    ran, unrun = leak_commands(test, whole)
    for what, args, scanned, wanted in (
        *((w, a, leaks.Watch.answer, True) for w, a in counted.items()),
        *((w, a, leaks.Watch.answer, False) for w, a in passed_over.items()),
        *((w, a, leaks.Watch.command, True) for w, a in ran.items()),
        *((w, a, leaks.Watch.command, False) for w, a in unrun.items()),
    ):
        watch = leaks.Watch()
        scanned(watch, *args)
        tokens = [f.token for f in watch.take()[0]]
        if wanted and tokens != [leaks.unsigned(whole)]:
            problems.append(f"a Watch found {len(tokens)} token(s) in {what}, not the one")
        elif not wanted and tokens:
            problems.append(f"a Watch found a token in {what}, which does not count")
    watch = leaks.Watch()
    method, url, _, *rest = counted["a 500's body"]
    watch.answer(method, url, {"Authorization": f"Bearer {whole}"}, *rest)
    own = [f.note for f in watch.take()[0]]
    if own != ["the request's own"]:
        problems.append(f"a Watch said {own} of a token the request presented")
    origin = test.fed.origins[0]
    before = leaks.WATCH.answers
    answer = web.request("GET", test.url(origin.url, test.rel("missing")), token=test.read_token)
    if answer.status is None or answer.status < 400:
        problems.append(f"{origin.svc}: a GET of a missing object got {answer.describe()}, not 4xx")
    elif leaks.WATCH.answers <= before:
        problems.append(f"testlib.web did not report {origin.svc}'s HTTP {answer.status} to leaks.WATCH")
    checked = f"{len(found) + len(not_found)} tokens, {len(counted) + len(passed_over)} answers,"
    checked += f" and {len(ran) + len(unrun)} commands"
    why = control.unavailable()
    if why:
        return outcome(problems, f"{checked}; not the logs: {why}")
    try:
        counts, _ = control.token_leaks(time.time() - LEAK_LOG_WINDOW, time.time())
    except control.ControlError as e:
        return outcome([*problems, str(e)], "")
    if origin.svc not in counts:
        problems.append(f"control.token_leaks() read no log of {origin.svc}, but of {sorted(counts)}")
    elif not sum(counts.values()):
        problems.append(f"control.token_leaks() found no line in the last {LEAK_LOG_WINDOW}s of any log")
    lines = sum(counts.values())
    return outcome(problems, f"{checked}; {lines} line(s) of {len(counts)} server(s)' logs")


# The scenarios, by name: each takes the Test, and returns its result and
# a note.
RUN = {
    "trailers": trailers,
    "jwt": jwt_tokens,
    "metrics": http_requests,
    "net-bytes": net_bytes,
    "restart-cache": restart_cache,
    "pool": pool,
    "confined": confined,
    "token-leaks": token_leaks,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the object in place, run the selected scenarios, and clean up."""
    stores.require_dirs(session.fed.origins, [NAMESPACE], "data/selftest")
    test = Test(session)
    test.clean("before the run")
    try:
        why = test.put({test.object: test.data})
        if why:
            die(f"could not put the object in place: {why}")
        for name in selected:
            results.add(name, *suite.attempt(functools.partial(RUN[name], test)))
    finally:
        test.clean("after the run")
