"""The tests' own helpers, against the running federation.

The suite checks that each does what the suites that use it rely on.

  trailers  testlib.web keeps a chunked body's trailers: a V2 cache's
            X-Transfer-Status, which it sends when asked
            (`X-Transfer-Status: true` and `TE: trailers`), and only then
  jwt       testlib.jwt signs tokens that the origins take: one with
            credentials.server_claims(), as `pelican token create` mints
            them in the session's profile, is taken by every origin, and
            refused once expired
  metrics   testlib.metrics reads /metrics: a GET straight to a native
            origin raises its pelican_http_requests_total
  restart-cache
            testlib.control restarts a service mid-run: cache-0, once it
            holds the object whole, comes back, every director names it
            again, and it serves the object, a V2 cache from its store

Each reads an object of its own under /protected-a/data/selftest/<run>/,
which the suite puts in place and removes, with a read token of the
session's profile.
"""

import json
import os
import time
import traceback
from dataclasses import dataclass
from typing import Optional

from testlib import common, control, credentials, director, jwt, metrics, stores, web
from testlib.common import die, first, warn
from testlib.credentials import R
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session

# `restart-cache` presents the setup token alone, since its reads are
# how it fills the cache and sees it serve again, not what it checks; so
# it runs in the WLCG pass alone (see suites/__init__.py), and cache-0
# is restarted once.
ONE_PROFILE = ("restart-cache",)

SCENARIOS = {
    "trailers": "a V2 cache's X-Transfer-Status trailer comes back when asked for, and only then",
    "jwt": (
        "a token jwt.encode() signs with the origins' key is taken, and refused once expired;"
        " jwt.verify() finds its key in the origins' JWKS, and in no other"
    ),
    "metrics": "a GET straight to a native origin raises its pelican_http_requests_total",
    "restart-cache": (
        "cache-0, restarted mid-run, is named by every director again, and serves what"
        " it held from its store"
    ),
}

NAMESPACE = "protected-a"

# What a V2 cache sends in the trailer of a transfer that went well
# (local_cache/persistent_cache_api.go in Pelican).
TRANSFER_OK = "200: OK"

REQUESTS = "pelican_http_requests_total"

# The cache that `restart-cache` restarts.
RESTARTED = "cache-0"
# How long it waits for a V2 cache to say, with Age, that it holds the
# object whole, and for every director to name the cache, in seconds.
# A server advertises as it starts, then every minute
# (Server.AdvertisementInterval); the directors drop a cache that says,
# as it stops, that it is shutting down, until it advertises again
# (director/director.go in Pelican).
HELD_WAIT, NAMED_WAIT = 30, 150
# How long it waits for the cache to serve the object again: a cache
# just started refuses a namespace's tokens until it has learned the
# namespace's issuers.
SERVED_WAIT = 300


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test: no /protected-a to read from."""
    if NAMESPACE not in fed.exports:
        return f"this shape does not export /{NAMESPACE}"
    return None


@dataclass(frozen=True)
class Test:
    """What every scenario needs: the federation, the object, and a token.

    Attributes:
        rel: The object's path, in /protected-a.
        data: The object's contents.
        token: A read token for the object, of the pass's profile.
        setup: A read token for the object, of the WLCG profile, for
            what is setup rather than the check (`restart-cache`).
        profile: token's profile.
    """

    fed: common.Federation
    rel: str
    data: bytes
    token: str
    setup: str
    profile: str

    @property
    def direct(self) -> bool:
        """Whether the origins take direct clients."""
        return "DirectReads" in self.fed.exports[NAMESPACE]

    def url(self, server_url: str) -> str:
        """Return the object's URL at server_url, a server's data URL."""
        return f"{server_url}/{NAMESPACE}/{self.rel}"

    def served(self, answer: web.Response) -> Optional[str]:
        """Return why answer is not the object, or None if it is."""
        if not answer.ok:
            return answer.describe()
        if answer.body != self.data:
            return f"{len(answer.body)} bytes that are not the object's"
        return None


def trailers(test: Test) -> tuple[str, str]:
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
        url = test.url(cache.url)
        answer = web.request("GET", url, token=test.token, headers=asked)
        why = test.served(answer)
        announced = [n.strip().lower() for n in answer.header("Trailer").split(",")]
        if why:
            problems.append(f"{cache.svc}: asked for the trailer, GET got {why}")
        elif "x-transfer-status" not in announced:
            problems.append(f"{cache.svc}: Trailer is '{answer.header('Trailer')}'")
        elif answer.trailer("X-Transfer-Status") != TRANSFER_OK:
            got = answer.trailer("X-Transfer-Status") or "none"
            problems.append(f"{cache.svc}: X-Transfer-Status trailer is '{got}'")
        answer = web.request("GET", url, token=test.token)
        why = test.served(answer)
        if why:
            problems.append(f"{cache.svc}: unasked, GET got {why}")
        elif answer.trailers or answer.header("Trailer"):
            problems.append(f"{cache.svc}: unasked, it sent trailers {answer.trailers}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(caches)} cache(s): '{TRANSFER_OK}' when asked, nothing otherwise"


def claims_problems(
    ours: dict[str, object], theirs: dict[str, object], profile: str
) -> list[str]:
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


def jwt_tokens(test: Test) -> tuple[str, str]:
    """Check that testlib.jwt signs tokens that the origins take.

    A token that jwt.encode() signs with the origins' key, with the
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
    claims = credentials.server_claims(fed, NAMESPACE, R, profile=test.profile)
    minted_head, minted = jwt.decode(test.token)
    problems = claims_problems(claims, minted, test.profile)
    if head != minted_head:
        problems.append(f"header {head}, not token create's {minted_head}")
    if problems:
        return FAIL, "unlike token create's: " + first(problems)
    token = jwt.encode(head, claims, key=key)
    now = int(time.time())
    stale = {**claims, "iat": now - 1200, "nbf": now - 1200, "exp": now - 600}
    expired = jwt.encode(head, stale, key=key)
    for origin in stores.unique(fed.origins):
        url = test.url(origin.url)
        why = test.served(web.request("GET", url, token=token))
        if why:
            return FAIL, f"{origin.svc}: GET with the token got {why}"
        answer = web.request("GET", url, token=expired)
        if answer.status not in (401, 403):
            return FAIL, f"{origin.svc}: GET with the expired token got {answer.describe()}"
    why = verify_problem(fed, token, str(head["kid"]))
    if why:
        return FAIL, why
    return PASS, f"{len(stores.unique(fed.origins))} origin(s); kid {head['kid']}"


def verify_problem(fed: common.Federation, token: str, kid: str) -> Optional[str]:
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
    other = json.dumps({"keys": [jwt.public_jwk(credentials.keys(fed)["unknown"])]})
    found = jwt.verify(token, other.encode())
    if found is not None:
        return f"jwt.verify() found {found!r} in a JWKS of the unknown key alone"
    return None


def http_requests(test: Test) -> tuple[str, str]:
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
        why = test.served(web.request("GET", test.url(origin.url), token=test.token))
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


def age(answer: web.Response) -> Optional[int]:
    """Return answer's Age, in seconds, or None if it has none."""
    value = answer.header("Age")
    return int(value) if value.isdigit() else None


def restart_cache(test: Test) -> tuple[str, str]:
    """Check that testlib.control restarts a service mid-run.

    Read the object through cache-0 until it holds it whole (a V2 cache
    sends Age then, and only then), and see every director name it;
    restart it (testlib.control). Within NAMED_WAIT seconds every
    director must name it again, and within SERVED_WAIT it must serve
    the object; a V2 cache from its store, with an Age at least the
    seconds since it first said it held it all, which only the record
    it kept across the restart can give.
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
    url = test.url(cache.url)
    path = f"/{NAMESPACE}/{test.rel}"
    host = director.host(cache.url)
    v2 = cache.kind == "v2"

    why = test.served(web.request("GET", url, token=test.setup))
    if why:
        return FAIL, f"before the restart, GET got {why}"
    held = 0.0
    if v2:

        def says_held() -> bool:
            return age(web.request("HEAD", url, token=test.setup)) is not None

        if not common.wait_for(says_held, HELD_WAIT, interval=1):
            return FAIL, f"before the restart, no Age within {HELD_WAIT}s of a whole read"
        held = time.time()
    named = director.naming(fed.directors, "object", path, host)
    if len(named) != len(fed.directors):
        return FAIL, f"before the restart, only {named} named {host}"

    control.restart(cache.svc)
    restarted = time.time()

    def all_named() -> bool:
        return len(director.naming(fed.directors, "object", path, host)) == len(fed.directors)

    if not common.wait_for(all_named, NAMED_WAIT, interval=5):
        named = director.naming(fed.directors, "object", path, host)
        return FAIL, f"{NAMED_WAIT}s after the restart, only {named} named {host}"
    took = time.time() - restarted

    answer = web.Response(None)

    def serves() -> bool:
        nonlocal answer
        answer = web.request("GET", url, token=test.setup)
        return answer.ok

    common.wait_for(serves, SERVED_WAIT, interval=5)
    why = test.served(answer)
    if why:
        return FAIL, f"after the restart, GET got {why} (waited up to {SERVED_WAIT}s)"
    note = f"{cache.svc}: every director named it again {took:.0f}s after its restart"
    if not v2:
        return PASS, f"{note}, and it serves the object"
    since = int(time.time() - held)
    found = age(answer)
    if found is None:
        return FAIL, "after the restart, it served the object with no Age, as if it held none"
    # Age is whole seconds, rounded down, and the clocks are one host's.
    if found < since - 1:
        return FAIL, (
            f"after the restart, Age {found}, but it said it held the object {since}s"
            " before, so it fetched it again"
        )
    return PASS, f"{note}, and serves the object from its store (Age {found})"


# The scenarios, by name: each takes the Test, and returns its result and
# a note.
RUN = {
    "trailers": trailers,
    "jwt": jwt_tokens,
    "metrics": http_requests,
    "restart-cache": restart_cache,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the object in place, run the selected scenarios, and remove it.

    The object is put in place at every origin.
    """
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of(NAMESPACE)}/data/selftest"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.setup_tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/selftest", tokens)
        if errors:
            warn(f"emptying data/selftest {when}: {first(errors)}")

    clean("before the run")
    base = f"data/selftest/{session.run}"
    read = session.token(NAMESPACE, R)
    setup = session.setup_token(NAMESPACE, R)
    test = Test(fed, f"{base}/object", os.urandom(200_000), read, setup, session.profile)
    try:
        errors = stores.make_dirs(fed.origins, NAMESPACE, base, tokens[NAMESPACE])
        errors += stores.seed_everywhere(
            fed.origins,
            NAMESPACE,
            test.rel,
            test.data,
            tokens[NAMESPACE],
            fed.exports[NAMESPACE],
        )
        if errors:
            die(f"could not put the object in place: {errors[0]}")
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
        clean("after the run")
