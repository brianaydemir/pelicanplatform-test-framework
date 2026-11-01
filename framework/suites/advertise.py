"""What the directors take from servers, and from anyone posing as one.

These are the advertisements by which each origin and cache tells them
what it serves; the federation tokens they mint for a server that proves
it is one; their service discovery; and the connection broker. Each must
turn away a caller who lies, and serve the real thing.

See docs/tests/advertisements.md.

A server signs its advertise token with its own key, which the registry
holds for its prefix (/origins/<host> or /caches/<host>), and the
director checks it against the registry's copy (verifyAdvertiseToken in
Pelican's director/origin_api.go). The suite signs as each server with
the key in framework/var/issuer-keys/<host>/ that the registry holds.

An ad the suite posts that a director takes stands as the server's
own: built from what the director says of the server
(/api/v1.0/director_ui/servers/<name>), it lasts 90 seconds, and the
server's next ad replaces it within Server.AdvertisementInterval (30
seconds here). After a scenario that posts such ads, every director
must name each server touched again within RESTORE_WAIT seconds, or a
row of the scenario fails.

The suite puts a probe at every origin, in the probe namespace
(credentials.probe_namespace()), and asks the directors about it.
"""

import glob
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from testlib import accounts, ads, common, credentials, director, jwt, metrics, registry, stores, suite, web
from testlib.ads import (
    ADVERTISE,
    Missing,
    Server,
    Servers,
    ad,
    namespace_ad,
    post_ad,
    sign,
    unknown_token,
)
from testlib.common import die, first, outcome
from testlib.director import expected_targets, listing_problems
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session
from testlib.suite import CaseRow

SCENARIOS = {
    "forged-ads": (
        "an ad is taken only with its server's own key and scope, a prefix of its kind, and a"
        " token for this director, from the server, that expires"
    ),
    "bad-broker-url": "an ad whose broker URL does not parse gets 400; the directors serve on",
    "spoofed-status": (
        "a cache's own key may take it out of routing; another cache's may not, by name or URL"
    ),
    "fed-token": "a federation token, only for a server's own advertise token",
    "fed-token-at-origin": "an origin without direct clients serves a user beside a fed token",
    "discover-servers": "service discovery: refused without its scope, else every server, once",
    "broker": "the connection broker gives a caller only its own origin's requests",
}

# The scenarios that present no storage token but their setup's (see
# suites/__init__.py): only `fed-token-at-origin` presents a user's, of
# the session's profile, beside a federation token.
ONE_PROFILE = tuple(s for s in SCENARIOS if s != "fed-token-at-origin")

# Scopes, as a token carries them, beside the advertise scope
# (ads.ADVERTISE).
NOT_ADVERTISE = "storage.read:/"
DISCOVERY = "pelican.director_service_discovery"
RETRIEVE, REVERSE = "broker.retrieve", "broker.reverse"

# An issuer that no server trusts.
ELSEWHERE = "https://elsewhere.invalid"

# The counter of the ads a director refuses (metrics/director.go).
REJECTED = "pelican_director_rejected_advertisements"

# How long each director has to name a server again after a scenario
# changed where it sends clients (it learns of an ad at once, and of a
# server's next ad within Server.AdvertisementInterval); and how long one
# has to stop naming a server once an ad says it may not be used.
RESTORE_WAIT = 150
DROP_WAIT = 10
# How long a federation token lasts (Director.FedTokenLifetime's default).
FED_TOKEN_LIFETIME = 900


class Test(suite.Test, Servers):
    """The probe, and the servers, keys, and tokens (ads.Servers).

    The probe's namespace is the first protected one, or /public where
    the origins export only that (`-p origin-public`).

    Attributes:
        probe: The probe's path in the namespace.
        data: The probe's content.
    """

    def __init__(self, session: Session):
        suite.Test.__init__(self, session, "advertise", credentials.probe_namespace(session.fed))
        Servers.__init__(self, session.fed)
        self.probe = self.rel("probe")
        self.data = os.urandom(4096)


def rejected(url: str) -> float | None:
    """Return how many ads the director at url has refused.

    Returns:
        The count (REJECTED, every series), or None if the director's
        /metrics did not answer.
    """
    snapshot = metrics.fetch(url)
    return None if snapshot is None else snapshot.value(REJECTED)


def route(server: Server) -> str:
    """Return the director route that names server.

    It is `origin` for an origin, and `object` for a cache.
    """
    return "origin" if server.kind == "Origin" else "object"


def naming(test: Test, server: Server) -> list[str]:
    """Return the directors that name server for the probe."""
    return director.naming(test.fed.directors, route(server), test.path(test.probe), server.host)


def restored(test: Test, *servers: Server) -> list[str]:
    """Wait until every director names each of servers for the probe.

    Each server gets up to RESTORE_WAIT seconds.

    Returns:
        What is still wrong after that.
    """
    problems: list[str] = []
    wanted = len(test.fed.directors)
    for server in servers:

        def named(s: Server = server) -> bool:
            return len(naming(test, s)) == wanted

        if not common.wait_for(named, RESTORE_WAIT, 5):
            problems.append(f"{RESTORE_WAIT}s on, only {naming(test, server)} named {server.host} again")
    return problems


def restored_row(test: Test, *servers: Server) -> CaseRow:
    """Return restored()'s row."""
    problems = restored(test, *servers)
    names = ", ".join(s.host for s in servers)
    return ("restored", *outcome(problems, names))


def post_everywhere(
    test: Test, kind: str, body: Mapping[str, Any], token: str, refusable: bool = False
) -> list[str]:
    """POST ad body to every director, which must take it (200).

    If refusable, a director may refuse it (403) too.

    Returns:
        What is wrong.
    """
    problems: list[str] = []
    for url in test.fed.directors:
        answer = post_ad(url, kind, body, token)
        if answer.status != 200 and not (refusable and answer.status == 403):
            problems.append(f"{director.host(url)} answered {answer.describe()}")
    return problems


# --------------------------------------------------------------------------
# forged-ads


def forged(
    test: Test, kind: str, body: Mapping[str, Any], token: str | None
) -> tuple[list[str], list[str]]:
    """POST ad body to every director, as a server of kind, with token.

    Each must refuse it (403) and count it among the ads it refused
    (REJECTED).

    Returns:
        What is wrong, and what each answered.
    """
    problems: list[str] = []
    seen: list[str] = []
    for url in test.fed.directors:
        name = director.host(url)
        before = rejected(url)
        answer = post_ad(url, kind, body, token)
        after = rejected(url)
        seen.append(f"{name} {answer.describe()}")
        if answer.status != 403:
            problems.append(f"{name}: {answer.describe()}, not 403")
        if before is None or after is None:
            problems.append(f"{name}: its /metrics did not answer")
        elif after - before < 1:
            problems.append(f"{name}: {REJECTED} rose by {after - before:g}, not 1 or more")
    return problems, seen


# A forged ad: its row's name, the kind of server it posts as, the ad, the
# token, and, if it claims a prefix, the prefix and the server that no
# director may then name for it.
Variant = tuple[str, str, dict[str, Any], str | None, tuple[str, Server] | None]


def everlasting(key: str, kid: str, issuer: str, audience: str) -> str:
    """Return an advertise token like sign()'s, but with no `exp`.

    The WLCG token profile requires `exp`; a token without one would be
    good for as long as its key is.
    """
    body = ads.claims(ADVERTISE, issuer, audience, issuer)
    del body["exp"]
    return jwt.sign(body, key, kid)


def forged_ads(test: Test) -> list[CaseRow]:
    """Check that the directors take servers' own ads, and no forgeries.

    Post origin-0's and the first cache's own ads, as each, to every
    director, which must take them (200); then variants of each, each
    changed in one way, which every director must refuse (403): among
    them the server's own ad with a token of its key and scope, but for
    another audience, from another issuer, or with no `exp`, which RFC
    7519 (sections 4.1.3 and 4.1.4) and the WLCG token profile say a
    verifier must refuse.
    """
    fed = test.fed
    origin = test.server("Origin", director.host(fed.origins[0].url))
    servers = [origin]
    if test.fed.federated_caches:
        servers.append(test.server("Cache", director.host(test.fed.federated_caches[0].url)))
    rows: list[CaseRow] = []
    variants: list[Variant] = []
    for server in servers:
        kind = server.kind.lower()
        key, kid = test.key(server)
        genuine = test.advertise_token(server)
        problems = post_everywhere(test, server.kind, ad(server), genuine)
        if not problems and len(naming(test, server)) != len(fed.directors):
            problems.append(f"only {naming(test, server)} name it afterward")
        rows.append((f"{kind}-genuine", *outcome(problems, server.prefix)))
        web_url = str(server.detail.get("webUrl", ""))
        variants += [
            (f"{kind}-no-token", server.kind, ad(server), None, None),
            (
                f"{kind}-unknown-key",
                server.kind,
                ad(server),
                unknown_token(ADVERTISE, web_url, test.director),
                None,
            ),
            (
                f"{kind}-no-scope",
                server.kind,
                ad(server),
                sign(key, kid, NOT_ADVERTISE, web_url, test.director, web_url),
                None,
            ),
            (
                f"{kind}-ns-prefix",
                server.kind,
                ad(server, **{"registry-prefix": f"/{test.namespace}"}),
                genuine,
                None,
            ),
            (
                f"{kind}-other-audience",
                server.kind,
                ad(server),
                sign(key, kid, ADVERTISE, web_url, ELSEWHERE, web_url),
                None,
            ),
            (
                f"{kind}-other-issuer",
                server.kind,
                ad(server),
                sign(key, kid, ADVERTISE, ELSEWHERE, test.director, web_url),
                None,
            ),
            (
                f"{kind}-no-exp",
                server.kind,
                ad(server),
                everlasting(key, kid, web_url, test.director),
                None,
            ),
        ]
    if len(servers) > 1:
        cache = servers[1]
        variants.append(("cross-type", "Origin", ad(cache), test.advertise_token(cache), None))
    unregistered = f"/advertise-{test.run}"
    extra = ad(origin)
    extra["namespaces"] = extra["namespaces"] + [
        {**namespace_ad({"path": unregistered}), "Caps": {"PublicRead": True, "Read": True}}
    ]
    variants.append(
        (
            "unregistered-ns",
            "Origin",
            extra,
            test.advertise_token(origin),
            (unregistered, origin),
        )
    )
    owner = next(iter(fed.owners), None)
    if owner is not None:
        other = test.server("Origin", director.host(owner.origin.url))
        theirs = [n for n in ad(origin)["namespaces"] if n.get("path") == f"/{test.namespace}"]
        claim = ad(other)
        claim["namespaces"] = claim["namespaces"] + theirs
        variants.append(
            (
                "owner-claims",
                "Origin",
                claim,
                test.advertise_token(other),
                (f"/{test.namespace}", other),
            )
        )
    for name, kind, body, token, nowhere in variants:
        problems, seen = forged(test, kind, body, token)
        if nowhere is not None:
            prefix, server = nowhere
            path = test.path(test.probe) if prefix == f"/{test.namespace}" else f"{prefix}/probe"
            for url in fed.directors:
                if director.ask(url, route(server), path).names(server.host):
                    problems.append(f"{director.host(url)} now names {server.host} for {path}")
        rows.append((name, *outcome(problems, ", ".join(seen))))
    rows.append(restored_row(test, *servers))
    return rows


# --------------------------------------------------------------------------
# bad-broker-url


def bad_broker_url(test: Test) -> list[CaseRow]:
    """Check that an ad whose broker URL does not parse gets 400.

    Post origin-0's own ad, as it, but with a broker URL that Go's
    url.Parse refuses, to every director: each must answer 400, with one
    JSON body, and then still list its servers, and name origin-0.
    """
    origin = test.server("Origin", director.host(test.fed.origins[0].url))
    body = ad(origin, **{"broker-url": "http://[::1"})
    token = test.advertise_token(origin)
    problems: list[str] = []
    for url in test.fed.directors:
        name = director.host(url)
        answer = post_ad(url, "Origin", body, token)
        if answer.status != 400:
            problems.append(f"{name}: {answer.describe()}, not 400")
        elif not isinstance(answer.json(), dict):
            problems.append(f"{name}: its 400's body is not one JSON object")
    for url in test.fed.directors:
        listed = web.request("GET", f"{url}{director.SERVERS}")
        if listed.status != 200:
            problems.append(
                f"{director.host(url)} then answered {director.SERVERS} with {listed.describe()}"
            )
    problems += restored(test, origin)
    return [("", *outcome(problems, f"{len(test.fed.directors)} director(s)"))]


# --------------------------------------------------------------------------
# spoofed-status


def active_downtime(source: str) -> dict[str, Any]:
    """Return a downtime under way, as a server's ad carries it.

    Its times are in milliseconds.
    """
    now = int(time.time() * 1000)
    return {
        "id": str(uuid.uuid4()),
        "serverName": "",
        "serverId": "",
        "createdBy": "pelican-test-framework",
        "updatedBy": "pelican-test-framework",
        "source": source,
        "class": "SCHEDULED",
        "description": "pelican-test-framework",
        "severity": "Outage (completely inaccessible)",
        "startTime": now - 60000,
        "endTime": now + 3600000,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
    }


def spoofed_status(test: Test) -> list[CaseRow]:
    """Check that only a cache's own key may take it out of routing.

    With the first cache's own key, its ad saying it is shutting down,
    then that it is critical: within DROP_WAIT seconds, no director may
    name it, until its next ad (director.go: registerServerAd; sort.go:
    cacheNotInErrorState). Then, with a second cache's key and prefix, ads
    that name the first: by its name alone (the second's URL), saying each
    status, and with a downtime under way; and by its URL too, saying it
    is critical. The first must stay named, since an advertise token
    proves a server's prefix, not its name or URL.
    """
    caches = test.fed.federated_caches
    if not caches:
        return [("", SKIP, "the directors know of no cache")]
    first_cache = test.server("Cache", director.host(caches[0].url))
    wanted = len(test.fed.directors)
    rows: list[CaseRow] = []
    before = naming(test, first_cache)
    if len(before) != wanted:
        return [("", FAIL, f"before anything was posted, only {before} named {first_cache.host}")]
    for status in ("shutting down", "critical"):
        name = "own-" + status.replace(" ", "-")
        own = test.advertise_token(first_cache)
        problems = post_everywhere(test, "Cache", ad(first_cache, status=status), own)
        if not problems and not common.wait_for(lambda: not naming(test, first_cache), DROP_WAIT, 1):
            problems.append(f"{DROP_WAIT}s on, {naming(test, first_cache)} still named it")
        problems += restored(test, first_cache)
        rows.append((name, *outcome(problems, "dropped, then named again")))
    if len(caches) < 2:
        why = "needs a second cache (-p topo-multi-cache)"
        return rows + [
            (n, SKIP, why) for n in ("name-shutting-down", "name-critical", "name-downtime", "url-hijack")
        ]
    second = test.server("Cache", director.host(caches[1].url))
    spoofs = [
        ("name-shutting-down", ad(second, name=first_cache.name, status="shutting down")),
        ("name-critical", ad(second, name=first_cache.name, status="critical")),
        (
            "name-downtime",
            ad(second, name=first_cache.name, downtimes=[active_downtime("cache")]),
        ),
        (
            "url-hijack",
            ad(first_cache, **{"registry-prefix": second.prefix, "status": "critical"}),
        ),
    ]
    for name, body in spoofs:
        theirs = test.advertise_token(second)
        problems = post_everywhere(test, "Cache", body, theirs, refusable=True)
        named = naming(test, first_cache)
        if len(named) != wanted:
            problems.append(f"afterward, only {named} named {first_cache.host}")
        listed = web.request("GET", f"{test.director}{director.SERVERS}/{quote(first_cache.name, safe='')}")
        found = listed.object()
        if found.get("filtered"):
            problems.append(
                f"{director.host(test.director)} lists it filtered ({found.get('filteredType')})"
            )
        rows.append((name, *outcome(problems, f"{first_cache.host} still named")))
        problems = restored(test, first_cache, second)
        if problems:
            rows.append((f"{name}-restored", FAIL, first(problems)))
    return rows


# --------------------------------------------------------------------------
# fed-token


def get_fed_token(test: Test, query: Mapping[str, str], token: str | None) -> web.Response:
    """Ask the first director for a federation token, with query and token.

    If token is None, present none.
    """
    url = f"{test.director}/api/v1.0/director/getFedToken"
    if query:
        url += f"?{urlencode(query)}"
    return web.request("GET", url, token=token)


def fed_token_problems(test: Test, answer: web.Response, host: str) -> list[str]:
    """Return what is wrong with answer's federation token for host.

    The token must come from the federation (its discovery URL), for host,
    for any audience, to read everything, for FED_TOKEN_LIFETIME seconds,
    signed by a key of the federation's JWKS.
    """
    token = str(answer.object().get("access_token") or "")
    if answer.status != 200 or not token:
        return [f"{answer.describe()}, with no access_token"]
    try:
        _, claims = jwt.decode(token)
    except ValueError:
        return ["its access_token is not a JWT"]
    fed = test.fed
    problems: list[str] = []
    issuer = str(fed.discovery.get("discovery_endpoint", ""))
    if claims.get("iss") != issuer:
        problems.append(f"iss {claims.get('iss')!r}, not {issuer!r}")
    if claims.get("sub") != host:
        problems.append(f"sub {claims.get('sub')!r}, not {host!r}")
    audience = claims.get("aud")
    audiences = common.as_array(audience) or [audience]
    if jwt.ANY_AUDIENCE not in audiences:
        problems.append(f"aud {audience!r} lacks {jwt.ANY_AUDIENCE}")
    if claims.get("scope") != "storage.read:/":
        problems.append(f"scope {claims.get('scope')!r}, not 'storage.read:/'")
    lifetime = int(claims.get("exp") or 0) - int(claims.get("iat") or 0)
    if lifetime != FED_TOKEN_LIFETIME:
        problems.append(f"it lasts {lifetime}s, not {FED_TOKEN_LIFETIME}s")
    # Under `-p auth-external-issuer`, fed.sh's discovery document names
    # the external issuer's JWKS, which lacks the directors' keys.
    if fed.external_issuer:
        return problems
    jwks_uri = str(fed.discovery.get("jwks_uri", ""))
    jwks = web.request("GET", jwks_uri)
    if jwks.status != 200:
        problems.append(f"{jwks_uri}: {jwks.describe()}")
    elif jwt.verify(token, jwks.body) is None:
        problems.append(f"no key of {jwks_uri} signed it")
    return problems


def pending_registration(test: Test) -> CaseRow:
    """Check that a director mints no federation token for a pending prefix.

    Under `-p registry-approval`: register a cache's prefix of the run's,
    /caches/advertise-<run>, as the registry's admin, with a key made for
    it, which leaves it pending; ask for a federation token for it, with a
    token signed by that key, which must be refused (403); then remove the
    registration.
    """
    fed = test.fed
    host = f"advertise-{test.run}"
    prefix = f"/caches/{host}"
    key = registry.new_key(test.session.path("advertise-pending.pem"))
    kid = jwt.key_id(key)
    pubkey = json.dumps(registry.jwks([key]))
    admin = accounts.Api(fed.registry_url, test.session.web_token(fed.registry_url))
    try:
        headers = accounts.csrf(admin)
    except accounts.ApiError as e:
        return ("pending", FAIL, str(e))
    registration = {
        "prefix": prefix,
        "pubkey": pubkey,
        "admin_metadata": {
            "description": "pelican-test-framework: the advertise suite's pending cache",
            "institution": "pelican-test-framework",
        },
    }
    created = admin.call("POST", registry.REGISTRATIONS, registration, headers)
    if not created.ok:
        return (
            "pending",
            FAIL,
            f"registering {prefix} as the registry's admin: {created.describe()}",
        )
    listed = admin.call("GET", f"{registry.REGISTRATIONS}?status=Pending")
    ids = [
        str(entry.get("id", ""))
        for entry in common.objects(listed.array())
        if entry.get("prefix") == prefix
    ]
    try:
        token = sign(key, kid, ADVERTISE, f"https://{host}:8444", test.director)
        answer = get_fed_token(test, {"host": host, "sType": "Cache"}, token)
        if answer.status != 403:
            return ("pending", FAIL, f"{answer.describe()}, not 403: {prefix} is pending")
        return ("pending", PASS, f"HTTP 403 for {prefix}")
    finally:
        for reg_id in ids:
            admin.call("DELETE", f"{registry.REGISTRATIONS}/{reg_id}", headers=headers)


def fed_token(test: Test) -> list[CaseRow]:
    """Check that a federation token needs a server's own advertise token.

    Ask the first director for a federation token as the first cache,
    with its own advertise token: it must mint one (fed_token_problems());
    then without the host, the server type, or a token; for a director;
    with the cache's key but not the advertise scope; with the cache's
    token for another server; and with a token signed by a key no server
    knows: each must be refused (403).
    """
    caches = test.fed.federated_caches
    if not caches:
        return [("", SKIP, "the directors know of no cache, which asks for federation tokens")]
    cache = test.server("Cache", director.host(caches[0].url))
    host = cache.registered_host
    token = test.advertise_token(cache)
    key, kid = test.key(cache)
    web_url = str(cache.detail.get("webUrl", ""))
    rows: list[CaseRow] = []
    answer = get_fed_token(test, {"host": host, "sType": "Cache"}, token)
    problems = fed_token_problems(test, answer, host)
    rows.append(("valid", *outcome(problems, f"for {host}")))
    if len(caches) > 1:
        other = test.server("Cache", director.host(caches[1].url))
        other_query = {"host": other.registered_host, "sType": "Cache"}
    else:
        other = test.server("Origin", director.host(test.fed.origins[0].url))
        other_query = {"host": other.registered_host, "sType": "Origin"}
    bad: list[tuple[str, dict[str, str], str | None]] = [
        ("no-host", {"sType": "Cache"}, token),
        ("no-stype", {"host": host}, token),
        ("no-token", {"host": host, "sType": "Cache"}, None),
        ("stype-director", {"host": host, "sType": "Director"}, token),
        (
            "no-scope",
            {"host": host, "sType": "Cache"},
            sign(key, kid, NOT_ADVERTISE, web_url, test.director, web_url),
        ),
        ("other-host", other_query, token),
        (
            "unknown-key",
            {"host": host, "sType": "Cache"},
            unknown_token(ADVERTISE, web_url, test.director),
        ),
    ]
    for name, query, presented in bad:
        answer = get_fed_token(test, query, presented)
        if answer.status == 403:
            rows.append((name, PASS, "HTTP 403"))
        else:
            rows.append((name, FAIL, f"{answer.describe()}, not 403"))
    if test.fed.registry_approval:
        rows.append(pending_registration(test))
    else:
        rows.append(
            (
                "pending",
                SKIP,
                "the registry approves every registration ('-p registry-approval')",
            )
        )
    return rows


# --------------------------------------------------------------------------
# fed-token-at-origin


def fed_token_at_origin(test: Test) -> list[CaseRow]:
    """Check that an origin without direct clients needs a federation token.

    Where the origins take no direct clients (`-p origin-no-direct`,
    Origin.DisableDirectClients), a native origin serves a request only
    with a federation token beside the user's (authMiddleware in Pelican's
    origin_serve/handlers.go): a GET of the probe straight to each, with
    the `server` token and the first cache's federation token in one
    header, must get it; with the `server` token alone, 401; and with the
    federation token alone, which grants nothing in a protected
    namespace, a refusal (401 or 403), but in /public (`-p origin-public`),
    which anyone may read, the probe.
    """
    fed = test.fed
    if test.direct:
        return [("", SKIP, "the origins take direct clients ('-p origin-no-direct')")]
    if not fed.native_origins:
        return [("", SKIP, f"XRootD serves the origins ({fed.origin_variant}), not Pelican")]
    if not test.fed.federated_caches:
        return [("", SKIP, "the directors know of no cache, which asks for federation tokens")]
    cache = test.server("Cache", director.host(test.fed.federated_caches[0].url))
    answer = get_fed_token(
        test, {"host": cache.registered_host, "sType": "Cache"}, test.advertise_token(cache)
    )
    federation = str(answer.object().get("access_token") or "")
    if not federation:
        return [("", FAIL, f"no federation token for {cache.host}: {answer.describe()}")]
    user = test.token
    fed_only = (200,) if test.namespace == "public" else (401, 403)
    rows: list[CaseRow] = []
    for origin in stores.unique(fed.origins):
        url = stores.url(origin, test.namespace, test.probe)
        cases = (
            ("both", {"Authorization": f"Bearer {user}, Bearer {federation}"}, (200,)),
            ("user-only", {"Authorization": f"Bearer {user}"}, (401,)),
            ("fed-only", {"Authorization": f"Bearer {federation}"}, fed_only),
        )
        for name, headers, statuses in cases:
            got = web.request("GET", url, headers=headers)
            why = ""
            if got.status not in statuses:
                why = f"{got.describe()}, not {' or '.join(map(str, statuses))}"
            elif got.status == 200 and got.body != test.data:
                why = "the body is not the probe"
            elif got.status != 200 and got.body == test.data:
                why = "refused, but the body is the probe"
            rows.append((f"{origin.svc} {name}", FAIL if why else PASS, why or got.describe()))
    return rows


# --------------------------------------------------------------------------
# discover-servers


def federation_key(fed: common.Federation) -> tuple[str, str]:
    """Return the key that signs as the federation, and its kid.

    It is the first of the keys in framework/var/issuer-keys/ whose public
    key the federation's JWKS (its discovery document's jwks_uri) lists,
    with its kid there: the directors' (or under
    `-p auth-external-issuer`, the external issuer's).

    Raises:
        Missing: The federation's JWKS lists none of those keys.
    """
    jwks_uri = str(fed.discovery.get("jwks_uri", ""))
    answer = web.request("GET", jwks_uri)
    listed = jwt.jwks_keys(answer.body) or []
    for key in sorted(glob.glob("issuer-keys/*/*.pem")):
        kid = jwt.listed_kid(key, listed)
        if kid is not None:
            return key, kid
    raise Missing(f"{jwks_uri} ({answer.describe()}) lists none of framework/var/issuer-keys/'s keys")


def discover_servers(test: Test) -> list[CaseRow]:
    """Check that only the federation's token gets the directors' servers.

    Ask each director for its servers, as Prometheus's service discovery
    does (/api/v1.0/director/discoverServers): with no token, and with one
    of the scope from an issuer it does not trust, it must refuse (403);
    with one of the scope from the federation (the discovery URL), signed
    with the directors' key, it must list every server
    (listing_problems()).
    """
    fed = test.fed
    key, kid = federation_key(fed)
    issuer = str(fed.discovery.get("discovery_endpoint", ""))
    tokens: tuple[tuple[str, str | None], ...] = (
        ("no-token", None),
        ("unknown-issuer", unknown_token(DISCOVERY, ELSEWHERE, jwt.ANY_AUDIENCE)),
        ("federation", sign(key, kid, DISCOVERY, issuer, jwt.ANY_AUDIENCE)),
    )
    rows: list[CaseRow] = []
    for url in fed.directors:
        name = director.host(url)
        for case, token in tokens:
            answer = web.request("GET", f"{url}/api/v1.0/director/discoverServers", token=token)
            if token is None or case == "unknown-issuer":
                why = "" if answer.status == 403 else f"{answer.describe()}, not 403"
            elif answer.status != 200:
                why = f"{answer.describe()}, not 200"
            else:
                why = first(listing_problems(fed, answer.json()) or [""])
            note = why or (f"{len(expected_targets(fed))} servers" if case == "federation" else "HTTP 403")
            rows.append((f"{name} {case}", FAIL if why else PASS, note))
    return rows


# --------------------------------------------------------------------------
# broker


# A request of the broker: its row's name, its path, its token, its body,
# its timeout (X-Pelican-Timeout), and the statuses it may get.
BrokerRequest = tuple[str, str, str | None, dict[str, str], str, tuple[int, ...]]


def broker(test: Test) -> list[CaseRow]:
    """Check that the broker gives a caller only its own origin's requests.

    Under `-p origin-broker`, ask the director's broker for requests
    (/api/v1.0/broker/retrieve), as the broker client does
    (LaunchRequestMonitor in Pelican's broker/client.go), with a token
    from the registry's issuer for a server's prefix, signed with its key:
    as the first cache, for an origin of the run's own (a caller may
    retrieve only its own origin's requests: 401 or 403); with no token
    (401); with a token signed by a key no server knows (refused: 400, 401,
    or 403); with a timeout that is not a duration (400); and as origin-0,
    for 1s, which must time out (`timeout`). Then ask it to reverse a
    connection to origin-0's prefix with the cache's token, which must be
    refused (400, 401, or 403). The origin is made up, so that no request
    is ever taken from origin-0's queue.
    """
    fed = test.fed
    if not fed.broker:
        return [("", SKIP, "the origins use no connection broker ('-p origin-broker')")]
    if not test.fed.federated_caches:
        return [("", SKIP, "the directors know of no cache")]
    endpoint = urlsplit(str(fed.discovery.get("broker_endpoint", "")))
    base = f"{endpoint.scheme}://{endpoint.netloc}"
    origin = test.server("Origin", director.host(fed.origins[0].url))
    cache = test.server("Cache", director.host(test.fed.federated_caches[0].url))
    made_up = f"advertise-{test.run}"

    def registry_token(server: Server, scope: str) -> str:
        key, kid = test.key(server)
        issuer = f"{fed.registry_url}{registry.prefix_route(server.prefix)}"
        return sign(key, kid, scope, issuer, base, server.registered_host)

    as_origin = {"origin": made_up, "prefix": origin.prefix}
    unknown = unknown_token(RETRIEVE, f"{fed.registry_url}{registry.prefix_route(origin.prefix)}", base)
    retrieve, reverse = "/api/v1.0/broker/retrieve", "/api/v1.0/broker/reverse"
    requests: list[BrokerRequest] = [
        (
            "cross-origin",
            retrieve,
            registry_token(cache, RETRIEVE),
            {"origin": made_up, "prefix": cache.prefix},
            "2s",
            (401, 403),
        ),
        ("no-token", retrieve, None, as_origin, "2s", (401,)),
        ("unknown-key", retrieve, unknown, as_origin, "2s", (400, 401, 403)),
        ("bad-timeout", retrieve, registry_token(origin, RETRIEVE), as_origin, "soon", (400,)),
        ("valid", retrieve, registry_token(origin, RETRIEVE), as_origin, "1s", (200,)),
        (
            "reverse",
            reverse,
            registry_token(cache, REVERSE),
            {
                "origin": made_up,
                "prefix": origin.prefix,
                "request_id": str(uuid.uuid4()),
                "callback_url": f"{cache.detail.get('webUrl', '')}/api/v1.0/broker/callback",
            },
            "2s",
            (400, 401, 403),
        ),
    ]
    rows: list[CaseRow] = []
    for name, path, token, body, timeout, statuses in requests:
        answer = web.request(
            "POST",
            f"{base}{path}",
            token=token,
            headers={"X-Pelican-Timeout": timeout},
            json_body=body,
        )
        why = ""
        if answer.status not in statuses:
            why = f"{answer.describe()}, not {' or '.join(map(str, statuses))}"
        elif name == "valid" and answer.object().get("status") != "timeout":
            why = f"its status is not 'timeout': {answer.body[:200]!r}"
        rows.append((name, FAIL if why else PASS, why or answer.describe()))
    return rows


RUN: dict[str, Callable[[Test], list[CaseRow]]] = {
    "forged-ads": forged_ads,
    "bad-broker-url": bad_broker_url,
    "spoofed-status": spoofed_status,
    "fed-token": fed_token,
    "fed-token-at-origin": fed_token_at_origin,
    "discover-servers": discover_servers,
    "broker": broker,
}


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None if there are directors.

    A standalone origin has no director.
    """
    return "a standalone origin has no director" if not fed.directors else None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the probe in place, run the selected scenarios, and remove it."""
    test = Test(session)
    why = test.put({test.probe: test.data})
    if why:
        die(f"could not put the probe at every origin: {why}")
    print(f"Asking the directors about {test.path(test.probe)}\n")
    try:
        for name in selected:
            suite.record(
                results, name, suite.attempt_cases(partial(RUN[name], test), (Missing, accounts.ApiError))
            )
    finally:
        test.clean("after the run")
