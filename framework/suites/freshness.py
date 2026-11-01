"""How long a cache keeps its copy of an object, and what it tells clients.

See docs/tests/cache-freshness.md.

Each case needs the origins to send a Cache-Control of its own, or any.
The suite runs the cases in phases, one per Cache-Control: first those
that take what the origins send as the shape is (none, or
ORIGIN_CACHE_CONTROL's); then, for each other value, it restarts every
origin with Origin.CacheControl set to it (control.restart_all_with()),
runs the cases that need it, and restores the origins. Only native
origins send Origin.CacheControl, and ORIGIN_CACHE_CONTROL beats a
configuration file, so the restarts need native origins, no
ORIGIN_CACHE_CONTROL, and origins that can restart alone
(control.unrestartable()); otherwise, their cases skip.

The caches asked are the federated ones (a site-local cache's upstream
is another cache, which sends Cache-Control of its own). XRootD caches
do not parse Cache-Control as a V2 cache does, so only
`no-negative-cache` asks them. Objects go under
/protected-a/data/freshness/<run>/, a collection per case, put at every
origin with a WLCG token (suite.Test.put()); every request for them
presents a token of the session's profile.
"""

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

from testlib import (
    blocks,
    cacheapi,
    common,
    control,
    digests,
    director,
    metrics,
    stores,
    suite,
    web,
    webdav,
)
from testlib.blocks import OVERWRITE_CHANGE, OVERWRITE_SIZE, store_bytes
from testlib.common import die, first, outcome, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "only-if-cached": "each V2 cache: only-if-cached gets 504 for what it lacks, and fetches nothing",
    "no-negative-cache": "each cache and director: an object that was not found comes back once it is",
    "cc-downstream": "each V2 cache's Cache-Control and Age on a fill, a hit, and a 304",
    "propfind-freshness": "each V2 cache's Depth 0: 0 bytes for an empty object, the new size once stale",
    "stale-if-error": "each V2 cache serves its stale copy while the origins are down",
    "must-revalidate": "max-age=30, must-revalidate: each V2 cache serves no stale copy then",
    "cc-immediate": "max-age=0, no-cache: each V2 cache serves an overwrite at once",
    "cc-not-stored": "no-store, private: each V2 cache passes the object through, and keeps none",
    "s-maxage": "max-age=120, s-maxage=20: each V2 cache serves an overwrite after about 20s",
}

NAMESPACE = "protected-a"

# The Cache-Control of -p origin-max-age, under which, or with the
# origins restarted to send it, the cases that need a copy to go stale
# soon run.
MAX_AGE = "max-age=30"
# The configuration file with which the origins restart to send another.
CONFIG_FILE = "90-freshness.yaml"

# A V2 cache's freshness for an object whose origin sends no
# Cache-Control (LocalCache.DefaultMaxAge, 24 hours), less up to its
# jitter (LocalCache.RevalidationJitter, 10%), per object.
DEFAULT_MAX_AGE = 86400
DEFAULT_MIN_AGE = DEFAULT_MAX_AGE * 9 // 10
# How far an Age may stray from the time that has passed: it is whole
# seconds, rounded down, and requests take time.
AGE_SLACK = 2
# How long past an object's freshness a cache may take to serve a new
# version, which it fetches on a request: no-cache's grace, and
# max-age=0's none, are counted from when it last read the old one.
IMMEDIATE_SLACK = 3
STALE_SLACK = 15
# How long a director keeps an origin's answer that it lacks an object
# (Director.CachePresenceTTL, a minute), which it asks only where there
# are several origins (Director.AssumePresenceAtSingleOrigin), and a
# margin.
PRESENCE_WAIT = 75

# A small object that every phase reads to see that the federation
# serves /protected-a again.
PROBE = "probe"
PROBE_SIZE = 5000
# only-if-cached's objects: those it must not fetch are large enough
# that no other traffic moves as many of an origin's bytes in the time.
UNFETCHED_SIZE = 1 << 20
HELD_SIZE = 10000
# An object of a size that a V2 cache stores in files, not inline in its
# database (InlineThreshold, 4096 bytes).
OBJECT_SIZE = 3 * blocks.XROOTD_BLOCK + 7

# Pelican's own header asking a cache to answer only from what it holds
# (web.ONLY_IF_CACHED is the standard one).
NO_DOWNLOAD = {"X-Pelican-NoDownload": "true"}


class Test(suite.Test):
    """The run's objects in /protected-a, and the shape.

    Attributes:
        origins: The origins.
        caches: The federated caches.
        v2: The federated V2 caches.
        sent: What the origins send as the shape is: only native ones send
            Origin.CacheControl.
    """

    def __init__(self, session: Session):
        super().__init__(session, "freshness", NAMESPACE)
        self.origins = self.fed.origins
        self.caches = self.fed.federated_caches
        self.v2 = [c for c in self.caches if c.kind == "v2"]
        self.sent = self.fed.sent_cache_control


# --------------------------------------------------------------------------
# What the caches answer.


def directives(value: str) -> dict[str, str]:
    """Return the directives of Cache-Control value.

    Returns:
        Each directive's value, unquoted, or "", by its name in lower
        case.
    """
    found: dict[str, str] = {}
    for part in value.split(","):
        name, _, given = part.strip().partition("=")
        if name.strip():
            found[name.strip().lower()] = given.strip().strip('"')
    return found


def seconds_of(value: str, name: str) -> int:
    """Return the seconds of directive name in Cache-Control value.

    Raises:
        KeyError: value lacks it.
    """
    return int(directives(value)[name])


def sleep_until(moment: float) -> None:
    """Sleep until time moment, if it is to come."""
    time.sleep(max(0.0, moment - time.time()))


def poll(problem: Callable[[], str | None], seconds: float, interval: float) -> str | None:
    """Ask problem() what is wrong until it finds nothing, or seconds pass.

    It is asked at once, and then every interval seconds.

    Returns:
        What it last found, or None.
    """
    deadline = time.time() + seconds
    found = problem()
    while found and time.time() < deadline:
        time.sleep(interval)
        found = problem()
    return found


def hold(test: Test, cache: common.Cache, rel: str, data: bytes) -> str | None:
    """Read rel whole through a V2 cache, which must then say it holds it.

    Unlike suite.Test.fill(), the read presents the pass's token.

    Returns:
        What went wrong, or None.
    """
    why = web.served(test.ask("GET", cache.url, rel), data)
    if why:
        return f"{cache.svc}: GET of the object to hold got {why}"
    if not test.wait_held(cache, rel):
        return f"{cache.svc}: no Age within {suite.HELD_WAIT}s of a whole read"
    return None


# An origin writes an object's ETag from its file's modification time, to
# the second, so a second version written within the same second as the
# first may have its ETag, and a cache would take it for the first.
ETAG_RESOLUTION = 1.1


class Versions:
    """Two versions of objects, by rel.

    v2 differs from v1 in its bytes, and perhaps in its size.

    Attributes:
        v1: The first version of each object, by rel.
        v2: The second version of each object, by rel.
        written: When v1 was put.
    """

    def __init__(self, sizes: Mapping[str, tuple[int, int]]):
        self.v1 = {rel: os.urandom(a) for rel, (a, _) in sizes.items()}
        self.v2 = {rel: os.urandom(b) for rel, (_, b) in sizes.items()}
        self.written = 0.0

    def put_v1(self, test: Test) -> str | None:
        """Put v1 of every object at every origin.

        Returns:
            What went wrong, or None.
        """
        why = test.put(self.v1)
        self.written = time.time()
        return why

    def put_v2(self, test: Test) -> str | None:
        """Put v2 of every object at every origin.

        They are put once they will have another ETag.

        Returns:
            What went wrong, or None.
        """
        sleep_until(self.written + ETAG_RESOLUTION)
        return test.put(self.v2)

    def which(
        self, test: Test, cache: common.Cache, rel: str, request: blocks.Request | None
    ) -> tuple[str | None, str]:
        """Return which version a read of rel through cache came back as.

        A whole read asks for a digest too, which, if one comes, must be
        that version's.

        Args:
            test: The test.
            cache: The cache to read through.
            rel: The object.
            request: The range to read, or None for the whole object.

        Returns:
            The version, `v1` or `v2`, and ""; or None, and why
            (blocks.which_version()).
        """
        headers = {"Range": blocks.header(request)} if request else {"Want-Digest": "crc32c"}
        answer = test.ask("GET", cache.url, rel, headers)
        versions = {"v1": self.v1[rel], "v2": self.v2[rel]}
        version, why = blocks.which_version(versions, request, answer)
        if version and request is None:
            wrong, _ = digests.check(answer.header("Digest"), versions[version])
            if wrong:
                return None, f"{version}, but with a Digest that is not its ({first(wrong)})"
        return version, why

    def all_v2(self, test: Test, cache: common.Cache, rels: Sequence[str]) -> list[str]:
        """Return what whole reads of rels through cache got, but v2."""
        found: list[str] = []
        for rel in rels:
            version, why = self.which(test, cache, rel, None)
            if version != "v2":
                found.append(f"{rel.rsplit('/', 1)[-1]}: {version or why}")
        return found

    def battery(self, test: Test, cache: common.Cache, rels: Sequence[str]) -> list[str]:
        """Return the reads of rels through cache that did not get v2.

        The reads are whole, and in every range of v2.
        """
        found: list[str] = []
        for rel in rels:
            for request in [None, *blocks.requests(len(self.v2[rel]))]:
                version, why = self.which(test, cache, rel, request)
                if version != "v2":
                    what = blocks.header(request) if request else "whole"
                    found.append(f"{rel.rsplit('/', 1)[-1]} {what}: {version or why}")
        return found


# --------------------------------------------------------------------------
# Phases: the origins sending one Cache-Control.


def settle(test: Test) -> str | None:
    """Wait until the federation serves the probe (suite.Test.settle())."""
    return test.settle(test.rel(PROBE), PROBE_SIZE)


def not_sent(test: Test, value: str) -> str | None:
    """Return why the origins do not send Cache-Control value, or None.

    They are asked directly, where they take direct clients, with the
    setup token, since this is a check of the shape.
    """
    if not test.direct:
        return None
    for origin in test.origins:
        answer = web.request(
            "GET",
            test.url(origin.url, test.rel(PROBE)),
            token=test.setup,
            headers={"Range": "bytes=0-0"},
        )
        if not answer.ok:
            return f"{origin.svc}: GET of the probe got {answer.describe()}"
        sent = answer.header("Cache-Control")
        if sent != value:
            return f"{origin.svc} sends Cache-Control '{sent}', not '{value}'"
    return None


def unreachable(test: Test, value: str) -> str | None:
    """Return why the origins can't be made to send Cache-Control value.

    Returns:
        Why, or None if they can be.
    """
    if value == test.sent:
        return None
    if not test.fed.native_origins:
        return "an XRootD origin sends no Cache-Control (Origin.CacheControl is the native origin's)"
    if test.fed.origin_cache_control:
        return (
            f"ORIGIN_CACHE_CONTROL ('{test.fed.origin_cache_control}') sets"
            " Origin.CacheControl by environment, which no configuration file overrides"
        )
    return control.unrestartable(test.fed)


# --------------------------------------------------------------------------
# The cases. Each takes the Test and the Cache-Control that the origins
# send, and returns its result and a note.


def only_if_cached(test: Test, _value: str) -> suite.Row:
    """Check that each V2 cache answers only-if-cached from what it holds.

    At each V2 cache, an object it lacks: a GET and a HEAD with
    only-if-cached, and with X-Pelican-NoDownload, get 504, and again 2
    seconds later; and a native origin sends fewer bytes for GETs
    meanwhile than one such object (pelican_http_bytes_total). A plain
    HEAD of another such object gets its Content-Length, and leaves the
    cache lacking it. An object the cache holds comes back with either.
    """
    cold = {c.svc: test.rel("only-if-cached", f"cold-{c.svc}") for c in test.v2}
    head = {c.svc: test.rel("only-if-cached", f"head-{c.svc}") for c in test.v2}
    held = {c.svc: test.rel("only-if-cached", f"held-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(UNFETCHED_SIZE) for rel in [*cold.values(), *head.values()]}
    objects.update({rel: os.urandom(HELD_SIZE) for rel in held.values()})
    why = test.put(objects)
    if why:
        return FAIL, why

    problems: list[str] = []
    asks = [("GET", web.ONLY_IF_CACHED), ("HEAD", web.ONLY_IF_CACHED), ("GET", NO_DOWNLOAD)]
    asks += [("HEAD", NO_DOWNLOAD), ("GET", web.ONLY_IF_CACHED)]
    before = metrics.origins_sent_bytes(test.fed)
    for wait, plan in ((0, asks), (2, asks[:1])):
        time.sleep(wait)
        for cache in test.v2:
            for method, headers in plan:
                answer = test.ask(method, cache.url, cold[cache.svc], headers)
                if answer.status != 504:
                    asked = ", ".join(f"{k}: {v}" for k, v in headers.items())
                    later = f", {wait}s later" if wait else ""
                    problems.append(
                        f"{cache.svc}: {method} with {asked} of an object it lacks got"
                        + f" {answer.describe()}, not 504{later}"
                    )
    after = metrics.origins_sent_bytes(test.fed)
    notes: list[str] = []
    if before is None or after is None:
        notes.append("no origin's byte counter to read")
    elif after - before >= UNFETCHED_SIZE:
        problems.append(
            f"the origins sent {after - before:.0f} bytes for GETs meanwhile, as much as an"
            + f" object of {UNFETCHED_SIZE} that the caches were asked only for if they held it"
        )
    else:
        notes.append(f"the origins sent {after - before:.0f} bytes meanwhile")

    for cache in test.v2:
        rel = head[cache.svc]
        answer = test.ask("HEAD", cache.url, rel)
        if answer.status != 200 or answer.header("Content-Length") != str(UNFETCHED_SIZE):
            problems.append(
                f"{cache.svc}: a plain HEAD of an object it lacks got {answer.describe()},"
                + f" Content-Length '{answer.header('Content-Length')}'"
            )
        for method in ("HEAD", "GET"):
            answer = test.ask(method, cache.url, rel, web.ONLY_IF_CACHED)
            if answer.status != 504:
                problems.append(
                    f"{cache.svc}: after a plain HEAD, {method} with only-if-cached got"
                    + f" {answer.describe()}, not 504: the HEAD fetched the object"
                )

    for cache in test.v2:
        rel, data = held[cache.svc], objects[held[cache.svc]]
        why = hold(test, cache, rel, data)
        if why:
            problems.append(why)
            continue
        for method, headers in (("GET", web.ONLY_IF_CACHED), ("GET", NO_DOWNLOAD)):
            why = web.served(test.ask(method, cache.url, rel, headers), data)
            if why:
                asked = ", ".join(f"{k}: {v}" for k, v in headers.items())
                problems.append(f"{cache.svc}: {method} with {asked} of what it holds got {why}")
        answer = test.ask("HEAD", cache.url, rel, web.ONLY_IF_CACHED)
        if answer.status != 200 or answer.header("Content-Length") != str(HELD_SIZE):
            problems.append(
                f"{cache.svc}: HEAD with only-if-cached of what it holds got"
                + f" {answer.describe()}, Content-Length '{answer.header('Content-Length')}'"
            )
    return outcome(problems, "; ".join([f"{len(test.v2)} cache(s)"] + notes))


def bare_get(test: Test, url: str, rel: str) -> tuple[web.Response, str]:
    """GET rel's bare path at the director at url, as curl -L would.

    The answer is followed once to where it leads.

    Returns:
        The answer, and the host that gave it.
    """
    answer = web.request("GET", f"{url}{test.path(quote(rel))}", token=test.token, timeout=120)
    if answer.status in (301, 302, 303, 307, 308) and answer.header("Location"):
        target = answer.header("Location")
        return web.request("GET", target, token=test.token, timeout=120), director.host(target)
    return answer, director.host(url)


def not_back(test: Test, target: common.Cache | str, rel: str, data: bytes) -> str | None:
    """Return why a GET of rel at target does not get data, or None.

    Args:
        test: The test.
        target: A cache, or the URL of a director, whose bare path is
            asked and followed (bare_get()).
        rel: The object.
        data: Its bytes.
    """
    if isinstance(target, common.Cache):
        answer, name, where = test.ask("GET", target.url, rel), target.svc, target.svc
    else:
        answer, where = bare_get(test, target, rel)
        name = f"{director.host(target)} (bare path)"
    why = web.served(answer, data)
    return f"{name} got {why}" + (f", at {where}" if where != name else "") if why else None


def no_negative_cache(test: Test, _value: str) -> suite.Row:
    """Check that each cache and director serves an object once it exists.

    An object that no origin holds yet: each cache, and each director's
    bare path, followed, must answer 404 (a V2 cache's with a JSON body
    whose `error` is `not_found`). Once every origin holds it, each must
    serve it at once; or, where there are several origins, within
    PRESENCE_WAIT seconds, since a director keeps an origin's 404 for a
    minute.
    """
    rel = test.rel("no-negative-cache", "object")
    data = os.urandom(HELD_SIZE)
    errors = stores.make_dirs(test.origins, NAMESPACE, rel.rsplit("/", 1)[0], test.setup)
    if errors:
        return FAIL, errors[0]
    # A director that sends a bare path to an origin, which takes no
    # direct clients, would only be refused.
    directors = test.fed.directors
    if test.fed.director_default_response == "origin" and not test.direct:
        directors = []
    problems: list[str] = []
    for cache in test.caches:
        answer = test.ask("GET", cache.url, rel)
        if answer.status != 404:
            problems.append(f"{cache.svc}: before the object was, {answer.describe()}, not 404")
        elif cache.kind == "v2":
            if answer.object().get("error") != "not_found":
                problems.append(f"{cache.svc}: a 404 whose body is not JSON with not_found")
    for url in directors:
        answer, where = bare_get(test, url, rel)
        if answer.status != 404:
            problems.append(
                f"{director.host(url)} (bare path, at {where}): before the object was,"
                + f" {answer.describe()}, not 404"
            )
    if problems:
        return FAIL, first(problems)

    error = test.put({rel: data})
    if error:
        return FAIL, error
    created = time.time()
    wait = PRESENCE_WAIT if len(test.origins) > 1 else 0
    took: list[float] = []
    targets: list[common.Cache | str] = [*test.caches, *directors]
    for target in targets:
        why = poll(partial(not_back, test, target, rel, data), wait, 3)
        if why:
            when = f"{wait}s after" if wait else "once"
            problems.append(f"{when} every origin held the object, {why}")
        else:
            took.append(time.time() - created)
    if problems:
        return FAIL, first(problems)
    note = f"{len(test.caches)} cache(s), {len(directors)} director(s)"
    if wait:
        note += f", by {max(took):.0f}s"
    return PASS, note


def downstream_problems(answer: web.Response, value: str, filled: float) -> list[str]:
    """Return what is wrong with the Cache-Control of a V2 cache's answer.

    Under max-age=N, the cache must send s-maxage=N and max-age=N
    (ResponseCacheControl in local_cache/schema.go); under none, max-age
    of what remains of its own freshness.

    Args:
        answer: The cache's answer.
        value: The Cache-Control that the origins send.
        filled: When the cache first fetched the object (time.time()).
    """
    sent = answer.header("Cache-Control")
    found = directives(sent)
    if value:
        n = str(seconds_of(value, "max-age"))
        if found != {"s-maxage": n, "max-age": n}:
            return [f"Cache-Control '{sent}', not 's-maxage={n}, max-age={n}'"]
        return []
    if set(found) != {"max-age"} or not found["max-age"].isdigit():
        return [f"Cache-Control '{sent}', not max-age of what remains of its freshness"]
    remaining = int(found["max-age"])
    least = DEFAULT_MIN_AGE - (time.time() - filled) - AGE_SLACK
    if not least <= remaining <= DEFAULT_MAX_AGE:
        return [f"Cache-Control '{sent}', not max-age between {least:.0f} and {DEFAULT_MAX_AGE}"]
    return []


def downstream(test: Test, value: str) -> suite.Row:
    """Check each V2 cache's Cache-Control and Age on fills, hits, and 304s.

    At each V2 cache, an object of its own: a cold GET, which fills it;
    once the cache says it holds it, two GETs 3 seconds apart; and a GET
    with If-None-Match naming its ETag. Each must carry Cache-Control
    (see downstream_problems()), the 304 too. The fill may carry no Age,
    or one of at most AGE_SLACK; each hit's Age must be no more than the
    time since the fill, and the second's must exceed the first's by the
    time between them, within AGE_SLACK.
    """
    slug = "max-age" if value else "none"
    rels = {c.svc: test.rel(f"cc-downstream-{slug}", c.svc) for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        rel, data = rels[cache.svc], objects[rels[cache.svc]]
        filled = time.time()
        fill = test.ask("GET", cache.url, rel)
        why = web.served(fill, data)
        if why:
            problems.append(f"{cache.svc}: the cold GET got {why}")
            continue
        problems += [f"{cache.svc} (fill): {p}" for p in downstream_problems(fill, value, filled)]
        found = web.age(fill)
        if found is not None and found > AGE_SLACK:
            problems.append(f"{cache.svc} (fill): Age {found}, for what it just fetched")
        if not test.wait_held(cache, rel):
            problems.append(f"{cache.svc}: no Age within {suite.HELD_WAIT}s of a whole read")
            continue
        hits: list[tuple[float, web.Response]] = []
        for pause in (0, 3):
            time.sleep(pause)
            hits.append((time.time(), test.ask("GET", cache.url, rel)))
        ages: list[int] = []
        for n, (when, hit) in enumerate(hits, 1):
            why = web.served(hit, data)
            found = web.age(hit)
            if why or found is None:
                problems.append(f"{cache.svc} (hit {n}): {why or 'no Age'}")
                continue
            ages.append(found)
            problems += [f"{cache.svc} (hit {n}): {p}" for p in downstream_problems(hit, value, filled)]
            if found > when - filled + AGE_SLACK:
                problems.append(
                    f"{cache.svc} (hit {n}): Age {found}, but it fetched the object"
                    + f" {when - filled:.0f}s before"
                )
        if len(ages) == 2:
            between = hits[1][0] - hits[0][0]
            if abs((ages[1] - ages[0]) - between) > AGE_SLACK:
                problems.append(f"{cache.svc}: Age went from {ages[0]} to {ages[1]} in {between:.0f}s")
        etag = hits[-1][1].header("ETag")
        if not etag:
            notes.append(f"{cache.svc} sent no ETag, so no 304 was asked for")
            continue
        answer = test.ask("GET", cache.url, rel, {"If-None-Match": etag})
        if answer.status != 304:
            problems.append(f"{cache.svc}: If-None-Match its ETag got {answer.describe()}, not 304")
        else:
            problems += [f"{cache.svc} (304): {p}" for p in downstream_problems(answer, value, filled)]
    return outcome(problems, "; ".join([f"{len(test.v2)} cache(s)"] + notes))


def depth_0(test: Test, cache: common.Cache, rel: str) -> tuple[webdav.Entry | None, str]:
    """Return the entry of a cache's Depth 0 PROPFIND of rel.

    Returns:
        The entry, and ""; or None, and why there is none.
    """
    answer = webdav.propfind(test.url(cache.url, rel), "0", test.token)
    if answer.status != 207:
        return None, f"Depth 0 got {answer.describe()}, not 207"
    try:
        entries = webdav.relative(webdav.parse(answer.body), test.path(rel))
    except ValueError as e:
        return None, f"Depth 0: {e}"
    if set(entries) != {""}:
        return None, f"Depth 0 lists {sorted(entries)}, not the object alone"
    entry = entries[""]
    if entry.collection:
        return None, "Depth 0 calls the object a collection"
    return entry, ""


def propfind_empty(test: Test, _value: str) -> suite.Row:
    """Check that each V2 cache's Depth 0 says an empty object is 0 bytes.

    At each V2 cache, an empty object: once read whole, a Depth 0
    PROPFIND must say it is 0 bytes long, since every object has a
    getcontentlength, 0 included. A cache answers Depth 0 from its copy
    if it holds one, and otherwise asks an origin; if it never says,
    with Age, that it holds the object, a pass is inconclusive.
    """
    rels = {c.svc: test.rel("propfind-empty", c.svc) for c in test.v2}
    why = test.put({rel: b"" for rel in rels.values()})
    if why:
        return FAIL, why
    problems: list[str] = []
    unheld: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        why = web.served(test.ask("GET", cache.url, rel), b"")
        if why:
            problems.append(f"{cache.svc}: GET got {why}")
            continue
        if not test.wait_held(cache, rel, 10):
            unheld.append(cache.svc)
        entry, why = depth_0(test, cache, rel)
        if entry is None:
            problems.append(f"{cache.svc}: {why}")
        elif entry.size is None:
            problems.append(f"{cache.svc}: Depth 0 of the empty object has no getcontentlength")
        elif entry.size != 0:
            problems.append(f"{cache.svc}: Depth 0 says the empty object is {entry.size} bytes")
    if problems:
        return FAIL, first(problems)
    if unheld:
        return INCONCLUSIVE, (
            f"{', '.join(unheld)} never said it held the object (no Age), so an origin may" " have answered"
        )
    return PASS, f"{len(test.v2)} cache(s)"


def origin_etags(test: Test, rel: str) -> set[str]:
    """Return the ETags that the origins send for rel.

    There are none where the origins take no direct clients.
    """
    if not test.direct:
        return set()
    found = {test.ask("HEAD", o.url, rel).header("ETag") for o in test.origins}
    return {e for e in found if e}


def not_current(
    test: Test, cache: common.Cache, rel: str, size: int, old: str | None, etags: set[str]
) -> str | None:
    """Return why a cache's Depth 0 of rel is not current, or None.

    It must say the object is size bytes long, with an ETag other than
    old (one of etags, if any).
    """
    entry, why = depth_0(test, cache, rel)
    if entry is None:
        return why
    if entry.size != size or (old and entry.etag == old) or (etags and entry.etag not in etags):
        want = f"one of the origins' ETags {sorted(etags)}" if etags else f"an ETag not {old}"
        return f"Depth 0 says {entry.size} bytes, ETag {entry.etag}, not {size} and {want}"
    return None


def propfind_overwrite(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache's Depth 0 says the new size once stale.

    At each V2 cache, an object read whole, which it holds, and whose
    Depth 0 says its size; the origins then take one 1,000 bytes longer.
    Once the cache's copy is stale (max-age), Depth 0, with no GET in
    between, must say the new size, and an ETag other than the old one
    (an origin's, where they can be asked), within STALE_SLACK seconds.
    """
    fresh = seconds_of(value, "max-age")
    rels = {c.svc: test.rel("propfind-overwrite", c.svc) for c in test.v2}
    versions = Versions({rel: (OBJECT_SIZE, OBJECT_SIZE + 1000) for rel in rels.values()})
    why = versions.put_v1(test)
    if why:
        return FAIL, why
    problems: list[str] = []
    filled: dict[str, float] = {}
    old: dict[str, str | None] = {}
    for cache in test.v2:
        rel = rels[cache.svc]
        filled[cache.svc] = time.time()
        why = hold(test, cache, rel, versions.v1[rel])
        if why:
            problems.append(why)
            continue
        entry, why = depth_0(test, cache, rel)
        if entry is None or entry.size != len(versions.v1[rel]):
            problems.append(f"{cache.svc}: before the overwrite, {why or entry}")
            continue
        old[cache.svc] = entry.etag
    if problems:
        return FAIL, first(problems)
    why = versions.put_v2(test)
    if why:
        return FAIL, why
    notes: list[str] = []
    for cache in test.v2:
        rel = rels[cache.svc]
        etags = origin_etags(test, rel)
        sleep_until(filled[cache.svc] + fresh + 1)
        check = partial(not_current, test, cache, rel, len(versions.v2[rel]), old[cache.svc], etags)
        why = poll(check, STALE_SLACK, 3)
        waited = time.time() - filled[cache.svc]
        if why:
            problems.append(
                f"{cache.svc}: {waited:.0f}s after it cached the object, past its freshness"
                + f" of {fresh}s, {why}"
            )
        else:
            notes.append(f"{cache.svc} by {waited:.0f}s")
    return outcome(problems, ", ".join(notes))


def stale_if_error(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache serves a stale copy with every origin down.

    That is origins_down(), whose stale copy must come back.
    """
    return origins_down(test, value, "stale-if-error", True)


def must_revalidate(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache serves no stale copy that must revalidate.

    That is origins_down(), under must-revalidate, whose stale copy must
    not come back: a cache that cannot reach the origin to revalidate it
    must answer 504 instead (RFC 9111, section 5.2.2.2), or at least not
    with the copy.
    """
    return origins_down(test, value, "must-revalidate", False)


def origins_down(test: Test, value: str, scenario: str, stale_ok: bool) -> suite.Row:
    """Check what each V2 cache serves of a stale copy, every origin down.

    At each V2 cache, an object read whole, which it holds, and another
    it never read. Once the first is stale (max-age), every origin is
    stopped. The first must still come back, as the cache holds it, if
    stale_ok; else it must not. The other must fail, and never come
    back with bytes that are not its own. Then the origins start again,
    and the federation must serve as before.

    Args:
        test: The test.
        value: The Cache-Control that the origins send.
        scenario: The scenario, whose collection holds the objects.
        stale_ok: Whether a cache may serve its stale copy.
    """
    why = control.unrestartable(test.fed)
    if why:
        return SKIP, why
    fresh = seconds_of(value, "max-age")
    held = {c.svc: test.rel(scenario, f"held-{c.svc}") for c in test.v2}
    cold = {c.svc: test.rel(scenario, f"cold-{c.svc}") for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in [*held.values(), *cold.values()]}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    notes = [f"{len(test.v2)} cache(s), {len(test.origins)} origin(s) stopped"]
    stale_at = 0.0
    for cache in test.v2:
        rel = held[cache.svc]
        stale_at = time.time() + fresh + 2
        why = hold(test, cache, rel, objects[rel])
        if why:
            return FAIL, why
    sleep_until(stale_at)
    stopped: list[str] = []
    try:
        for origin in test.origins:
            control.stop(origin.svc)
            stopped.append(origin.svc)
        for cache in test.v2:
            rel = held[cache.svc]
            answer = test.ask("GET", cache.url, rel, timeout=180)
            why = web.served(answer, objects[rel])
            if why and stale_ok:
                problems.append(f"{cache.svc}: its stale copy, with every origin down, got {why}")
            elif answer.ok and not stale_ok:
                problems.append(
                    f"{cache.svc}: its stale copy, with every origin down, came back"
                    + f" ({answer.describe()}), not 504"
                )
            elif not stale_ok:
                notes.append(f"{cache.svc}: {answer.describe()}")
            rel = cold[cache.svc]
            answer = test.ask("GET", cache.url, rel, timeout=180)
            if answer.ok:
                what = "the object" if answer.body == objects[rel] else "bytes not the object's"
                problems.append(
                    f"{cache.svc}: an object it never read, with every origin down, came back"
                    + f" ({answer.describe()}, {what})"
                )
    finally:
        for svc in stopped:
            try:
                control.start(svc)
            except control.ControlError as e:
                problems.append(str(e))
    why = settle(test)
    if why:
        problems.append(f"once the origins started again, {why}")
    return outcome(problems, "; ".join(notes))


def immediate(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache serves an overwrite at once.

    Under Cache-Control value, a copy is fresh for blocks.freshness()
    seconds: 0, or no-cache's grace of 5. Of three objects read whole
    twice, and three read through a range and then whole, the origins
    then take another version: as long, longer, or shorter. Each cache
    must serve the new version of each, whole, at the first read after
    (where it is fresh for 0), or within its grace and IMMEDIATE_SLACK of
    when it last read the old one; then every read, whole and in every
    range, must be the new version alone.
    """
    fresh = blocks.freshness(value) or 0.0
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    changes = {"same": 0, "grown": OVERWRITE_CHANGE, "shrunk": -OVERWRITE_CHANGE}
    # Each object's first read, by its rel: whole, or a range.
    reads: dict[str, blocks.Request | None] = {}
    rels: dict[str, list[str]] = {}
    sizes: dict[str, tuple[int, int]] = {}
    for cache in test.v2:
        rels[cache.svc] = []
        for how, read in (("whole", None), ("range", ((0, 2 * blocks.BLOCK + 99),))):
            for name, change in changes.items():
                rel = test.rel(f"cc-immediate-{slug}", f"{cache.svc}-{how}-{name}")
                rels[cache.svc].append(rel)
                reads[rel] = read
                sizes[rel] = (OVERWRITE_SIZE, OVERWRITE_SIZE + change)
    versions = Versions(sizes)
    why = versions.put_v1(test)
    if why:
        return FAIL, why
    last_read: dict[str, float] = {}
    for cache in test.v2:
        for rel in rels[cache.svc]:
            for request in (reads[rel], None):
                version, why = versions.which(test, cache, rel, request)
                if version != "v1":
                    return FAIL, f"{cache.svc}: {rel} before the overwrite: {version or why}"
        last_read[cache.svc] = time.time()
    why = versions.put_v2(test)
    if why:
        return FAIL, why
    overwritten = time.time()
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        if not fresh:
            stale = versions.all_v2(test, cache, rels[cache.svc])
            if stale:
                problems.append(
                    f"{cache.svc} served, at the first read after the overwrite, {first(stale)}"
                )
                continue
        else:
            deadline = max(last_read[cache.svc] + fresh, overwritten) + IMMEDIATE_SLACK
            stale = versions.all_v2(test, cache, rels[cache.svc])
            while stale and time.time() < deadline:
                time.sleep(1)
                stale = versions.all_v2(test, cache, rels[cache.svc])
            if stale:
                waited = time.time() - last_read[cache.svc]
                problems.append(
                    f"{cache.svc} still served, {waited:.0f}s after it last read the old"
                    + f" version, {first(stale)}"
                )
                continue
        wrong = versions.battery(test, cache, rels[cache.svc])
        if wrong:
            problems.append(f"{cache.svc}, after serving the new version: {first(wrong)}")
            continue
        notes.append(f"{cache.svc} by {time.time() - overwritten:.0f}s")
    return outcome(problems, "new version " + ", ".join(notes))


def ranged(data: bytes, request: blocks.Request, answer: web.Response) -> tuple[str | None, bool]:
    """Return what is wrong with answer to request, and if it was whole.

    Returns:
        What is wrong with answer for an object holding data
        (blocks.check()), or None; and whether the server answered it
        whole, with `Accept-Ranges: none`, which RFC 9110 allows.
    """
    why = blocks.check(data, request, answer)
    if (
        why
        and answer.status == 200
        and answer.body == data
        and answer.header("Accept-Ranges").strip().lower() == "none"
    ):
        return None, True
    return why, False


def introspected(test: Test, cache: common.Cache, rel: str) -> tuple[int | None, str]:
    """Return how many instances of rel a V2 cache's introspection lists.

    It is asked as the cache's admin (introspect/list, whose pattern
    without a wildcard matches a substring of an object's URL).

    Returns:
        The count, and ""; or None, and why it said nothing.
    """
    token = cacheapi.admin_token(test.session, cache)
    answer = cacheapi.introspect(cache, token, "GET", "list", limit="1000", pattern=test.path(rel))
    if answer.status != 200:
        return None, f"introspection answered {answer.describe()}"
    found = common.as_array(answer.json())
    if found is None:
        return None, "introspection answered no JSON array"
    return len(found), ""


def not_stored(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache passes the object through, and keeps none.

    At each V2 cache, an object the origins mark value (no-store or
    private): two GETs must each pass it through, whole, with value in
    its Cache-Control and no Age; every range of it must come back as
    blocks.check() says, or whole, with `Accept-Ranges: none`. Then a
    GET and a HEAD with only-if-cached must get 504, the cache's
    introspection must list none of it, and its store's object files
    must not have grown by as much as the object.
    """
    rels = {c.svc: test.rel(f"cc-not-stored-{value}", c.svc) for c in test.v2}
    objects = {rel: os.urandom(OBJECT_SIZE) for rel in rels.values()}
    why = test.put(objects)
    if why:
        return FAIL, why
    problems: list[str] = []
    notes: list[str] = []
    for cache in test.v2:
        rel, data = rels[cache.svc], objects[rels[cache.svc]]
        before = store_bytes(cache)
        for n in (1, 2):
            answer = test.ask("GET", cache.url, rel)
            why = web.served(answer, data)
            sent = answer.header("Cache-Control")
            if why:
                problems.append(f"{cache.svc} (GET {n}): {why}")
            elif value not in directives(sent):
                problems.append(f"{cache.svc} (GET {n}): Cache-Control '{sent}', without {value}")
            elif web.age(answer) is not None:
                problems.append(f"{cache.svc} (GET {n}): Age {web.age(answer)}, as if it held it")
        requests = blocks.requests(len(data))
        whole = 0
        for request in requests:
            why, was_whole = ranged(
                data,
                request,
                test.ask("GET", cache.url, rel, {"Range": blocks.header(request)}),
            )
            whole += was_whole
            if why:
                problems.append(f"{cache.svc} {blocks.header(request)}: {why}")
        if whole:
            notes.append(
                f"{cache.svc} answered {whole} of {len(requests)} ranges whole,"
                + " with Accept-Ranges: none"
            )
        for method in ("GET", "HEAD"):
            answer = test.ask(method, cache.url, rel, web.ONLY_IF_CACHED)
            if answer.status != 504:
                problems.append(
                    f"{cache.svc}: {method} with only-if-cached got {answer.describe()}, not 504"
                )
        count, why = introspected(test, cache, rel)
        if count:
            problems.append(f"{cache.svc}'s introspection lists {count} instance(s) of it")
        elif why:
            notes.append(f"{cache.svc}: {why}")
        after = store_bytes(cache)
        if before is None or after is None:
            notes.append(f"{cache.svc}: no store of object files to measure")
        elif after - before >= len(data):
            problems.append(
                f"{cache.svc}'s object files grew by {after - before} bytes, for an object of"
                + f" {len(data)} it may not store"
            )
    return outcome(problems, "; ".join([f"{len(test.v2)} cache(s)"] + notes))


def s_maxage(test: Test, value: str) -> suite.Row:
    """Check that each V2 cache takes s-maxage over max-age.

    At each V2 cache, an object read whole, which the origins then
    replace with a longer one. RFC 9111 (section 5.2.2.10) says a shared
    cache must take s-maxage over max-age, so each cache must serve the
    new version within STALE_SLACK seconds of s-maxage's freshness.
    """
    shared, private = seconds_of(value, "s-maxage"), seconds_of(value, "max-age")
    rels = {c.svc: test.rel("s-maxage", c.svc) for c in test.v2}
    versions = Versions({rel: (OVERWRITE_SIZE, OVERWRITE_SIZE + OVERWRITE_CHANGE) for rel in rels.values()})
    why = versions.put_v1(test)
    if why:
        return FAIL, why
    filled: dict[str, float] = {}
    for cache in test.v2:
        filled[cache.svc] = time.time()
        version, why = versions.which(test, cache, rels[cache.svc], None)
        if version != "v1":
            return FAIL, f"{cache.svc}: before the overwrite: {version or why}"
    why = versions.put_v2(test)
    if why:
        return FAIL, why
    pending = {c.svc: c for c in test.v2}
    took: dict[str, float] = {}
    seen: dict[str, str] = {}
    deadline = max(filled.values()) + shared + STALE_SLACK
    while pending and time.time() < deadline:
        for svc, cache in list(pending.items()):
            version, why = versions.which(test, cache, rels[svc], None)
            seen[svc] = version or why
            if version == "v2":
                took[svc] = time.time() - filled[svc]
                del pending[svc]
        time.sleep(2)
    if pending:
        svc = next(iter(pending))
        return FAIL, (
            f"{svc} still served {seen.get(svc)} {time.time() - filled[svc]:.0f}s after it"
            + f" cached the object, past s-maxage={shared}: a shared cache takes s-maxage over"
            + f" max-age={private}"
        )
    return PASS, ", ".join(f"{svc}: the new version by {t:.0f}s" for svc, t in took.items())


@dataclass(frozen=True)
class Case:
    """A case of a scenario.

    Attributes:
        scenario: Its scenario.
        name: Its row's name after the scenario's.
        control: The Cache-Control the origins must send for it (None:
            whatever they send).
        run: What runs it, with what the origins send.
        v2: Whether it needs a V2 cache.
    """

    scenario: str
    name: str
    control: str | None
    run: Callable[[Test, str], suite.Row]
    v2: bool = True

    @property
    def row(self) -> str:
        """Its row's name."""
        return f"{self.scenario} {self.name}" if self.name else self.scenario


# In the order they run, but grouped by what the origins send.
CASES = (
    Case("only-if-cached", "", None, only_if_cached),
    Case("no-negative-cache", "", None, no_negative_cache, v2=False),
    Case("cc-downstream", "none", "", downstream),
    Case("cc-downstream", "max-age", MAX_AGE, downstream),
    Case("propfind-freshness", "empty", None, propfind_empty),
    Case("propfind-freshness", "overwrite", MAX_AGE, propfind_overwrite),
    Case("stale-if-error", "", MAX_AGE, stale_if_error),
    Case("must-revalidate", "", f"{MAX_AGE}, must-revalidate", must_revalidate),
    Case("cc-immediate", "max-age=0", "max-age=0", immediate),
    Case("cc-immediate", "no-cache", "no-cache", immediate),
    Case("cc-not-stored", "no-store", "no-store", not_stored),
    Case("cc-not-stored", "private", "private", not_stored),
    Case("s-maxage", "", "max-age=120, s-maxage=20", s_maxage),
)


def run_case(test: Test, case: Case, value: str, results: common.Results) -> None:
    """Run case, with the origins sending value, and record its row."""
    results.add(case.row, *suite.attempt(partial(case.run, test, value), (stores.Unreadable,)))


def run_phase(test: Test, value: str, cases: Sequence[Case], results: common.Results) -> None:
    """Run cases with the origins sending Cache-Control value.

    The origins are as they are, if they send it, or restarted to send
    it, and afterward restored.
    """
    ready: list[Case] = []
    for case in cases:
        if case.v2 and not test.v2:
            results.add(case.row, SKIP, "this shape has no federated V2 cache")
        else:
            ready.append(case)
    if not ready:
        return
    why = unreachable(test, value)
    if why:
        for case in ready:
            results.add(case.row, SKIP, f"needs origins sending '{value}', but {why}")
        return
    if value == test.sent:
        print(f"\nThe origins send {repr(value) if value else 'no Cache-Control'}, as the shape is")
        for case in ready:
            run_case(test, case, value, results)
        return
    print(f"\nRestarting the origins to send Cache-Control: {value}")
    text = f"Origin:\n  CacheControl: {json.dumps(value)}\n"
    done: set[str] = set()
    try:
        with control.restart_all_with({o.svc: {CONFIG_FILE: text} for o in test.origins}):
            why = settle(test) or not_sent(test, value)
            for case in ready:
                if why:
                    results.add(case.row, FAIL, f"with the origins restarted to send it, {why}")
                else:
                    run_case(test, case, value, results)
                done.add(case.row)
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for case in ready:
            if case.row not in done:
                results.add(case.row, FAIL, f"restarting the origins: {e}")
        warn(f"restarting the origins for Cache-Control '{value}': {e}")
    why = settle(test)
    if why:
        warn(f"with the origins restored from Cache-Control '{value}', {why}")


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test: no /protected-a, or no cache."""
    why = suite.unexported(fed)
    if why:
        return why
    if not fed.federated_caches:
        return "this shape has no federated cache"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios' cases phase by phase.

    The probe is put in place first, and the run's objects are removed
    afterward.
    """
    test = Test(session)
    stores.require_dirs(test.fed.origins, [NAMESPACE], f"data/{test.area}")
    test.clean("before the run")
    print(f"Freshness test against {test.fed.url}, objects under {test.path(test.base)}/")
    try:
        why = test.put({test.rel(PROBE): os.urandom(PROBE_SIZE)})
        if why:
            die(f"could not put the probe in place: {why}")
        cases = [c for c in CASES if c.scenario in selected]
        values = [test.sent] + [c.control for c in cases if c.control is not None]
        for value in dict.fromkeys(values):
            here = [c for c in cases if c.control == value or (c.control is None and value == test.sent)]
            if here:
                run_phase(test, value, here, results)
    finally:
        test.clean("after the run")
