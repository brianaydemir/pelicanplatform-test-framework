"""The tests' own helpers, against the running federation: that each
does what the suites that use it rely on.

  trailers  testlib.web keeps a chunked body's trailers: a V2 cache's
            X-Transfer-Status, which it sends when asked
            (`X-Transfer-Status: true` and `TE: trailers`), and only then
  jwt       testlib.jwt signs tokens that the origins take: one with
            credentials.server_claims(), as `pelican token create` mints
            them, is taken by every origin, and refused once expired
  metrics   testlib.metrics reads /metrics: a GET straight to a native
            origin raises its pelican_http_requests_total

Each reads an object of its own under /protected-a/data/selftest/<run>/,
which the suite puts in place and removes.
"""

import os
import time
import traceback
from dataclasses import dataclass
from typing import Optional

from testlib import common, credentials, jwt, metrics, stores, web
from testlib.common import die, first, warn
from testlib.credentials import R
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "trailers": "a V2 cache's X-Transfer-Status trailer comes back when asked for, and only then",
    "jwt": "a token jwt.encode() signs with the origins' key is taken, and refused once expired",
    "metrics": "a GET straight to a native origin raises its pelican_http_requests_total",
}

NAMESPACE = "protected-a"

# The origins that XRootD serves (ORIGIN_VARIANT); the rest are Pelican's
# native origins (origin_serve/ in Pelican).
XROOTD_ORIGINS = ("posix", "https", "s3")

# What a V2 cache sends in the trailer of a transfer that went well
# (local_cache/persistent_cache_api.go in Pelican).
TRANSFER_OK = "200: OK"

REQUESTS = "pelican_http_requests_total"


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a to read from."""
    if NAMESPACE not in fed.exports:
        return f"this shape does not export /{NAMESPACE}"
    return None


@dataclass(frozen=True)
class Test:
    """What every scenario needs: the federation, the object, and a read
    token for it."""

    fed: common.Federation
    rel: str  # the object, in /protected-a
    data: bytes
    token: str

    @property
    def direct(self) -> bool:
        """Whether the origins take direct clients."""
        return "DirectReads" in self.fed.exports[NAMESPACE]

    def url(self, server_url: str) -> str:
        """The object's URL at the server whose data URL is server_url."""
        return f"{server_url}/{NAMESPACE}/{self.rel}"

    def served(self, answer: web.Response) -> Optional[str]:
        """Why answer is not the object, or None."""
        if not answer.ok:
            return answer.describe()
        if answer.body != self.data:
            return f"{len(answer.body)} bytes that are not the object's"
        return None


def trailers(test: Test) -> tuple[str, str]:
    """A GET of the object at each V2 cache, with `X-Transfer-Status:
    true` and `TE: trailers`, must announce the trailer (`Trailer:
    X-Transfer-Status`) and end with it, saying 200: OK; without them, no
    trailer may come."""
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


def claims_problems(ours: dict[str, object], theirs: dict[str, object]) -> list[str]:
    """How claims ours, from credentials.server_claims(), differ from
    theirs, a token that `pelican token create` minted with the same
    arguments, but for the claims that differ from token to token."""
    problems: list[str] = []
    if set(ours) != set(theirs):
        problems.append(f"claims {sorted(ours)}, not token create's {sorted(theirs)}")
    for name in ("aud", "iss", "sub", "wlcg.ver"):
        if ours.get(name) != theirs.get(name):
            problems.append(f"{name} {ours.get(name)!r}, not {theirs.get(name)!r}")
    if set(str(ours.get("scope")).split()) != set(str(theirs.get("scope")).split()):
        problems.append(f"scope {ours.get('scope')!r}, not {theirs.get('scope')!r}")
    return problems


def jwt_tokens(test: Test) -> tuple[str, str]:
    """A token that jwt.encode() signs with the origins' key, with the
    claims and header of the `server` token that `pelican token create`
    mints (credentials.server_claims(), jwt.header(), jwt.key_id()),
    must read the object at every origin; the same token, expired ten
    minutes ago (well past native servers' 60 seconds of clock skew),
    must be refused."""
    if not test.direct:
        return SKIP, f"/{NAMESPACE} takes no direct clients"
    fed = test.fed
    key = fed.origin_key
    head = jwt.header(jwt.algorithm(key), jwt.key_id(key))
    claims = credentials.server_claims(fed, NAMESPACE, R)
    minted_head, minted = jwt.decode(test.token)
    problems = claims_problems(claims, minted)
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
    return PASS, f"{len(stores.unique(fed.origins))} origin(s); kid {head['kid']}"


def http_requests(test: Test) -> tuple[str, str]:
    """A GET of the object straight to each native origin must raise its
    pelican_http_requests_total{server_type="origin",method="GET",
    code="200"} by at least 1 (metrics/http_metrics.go in Pelican);
    other requests may raise it too."""
    fed = test.fed
    if fed.origin_variant in XROOTD_ORIGINS:
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


RUN = {"trailers": trailers, "jwt": jwt_tokens, "metrics": http_requests}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the object in place at every origin, run the selected
    scenarios, and remove it."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of(NAMESPACE)}/data/selftest"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/selftest", tokens)
        if errors:
            warn(f"emptying data/selftest {when}: {first(errors)}")

    clean("before the run")
    base = f"data/selftest/{session.run}"
    test = Test(fed, f"{base}/object", os.urandom(200_000), session.token(NAMESPACE, R))
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
