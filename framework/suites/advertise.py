"""What the directors take from servers, and from anyone posing as one:
the advertisements by which each origin and cache tells them what it
serves; the federation tokens they mint for a server that proves it is
one; their service discovery; and the connection broker. Each must turn
away a caller who lies, and serve the real thing.

  forged-ads           an ad is taken only with its server's own key,
                       the advertise scope, a server's registry prefix of
                       its kind, and, for an origin, its namespaces' keys
  bad-broker-url       an origin's ad whose broker URL does not parse gets
                       400, and the directors serve on
  spoofed-status       a cache's own key may take it out of routing, by
                       its status; another cache's key may not, by its
                       name or by its URL
  fed-token            a director mints a federation token only for a
                       server's own advertise token
  fed-token-at-origin  under `-p origin-no-direct`, an origin serves a
                       user only beside a federation token
  discover-servers     the directors' service discovery: refused without
                       a token of its scope, and otherwise every server,
                       once
  broker               under `-p origin-broker`, the broker gives a caller
                       only its own origin's requests

A server signs its advertise token with its own key, which the registry
holds for its prefix (/origins/<host> or /caches/<host>), and the
director checks it against the registry's copy (verifyAdvertiseToken in
Pelican's director/origin_api.go). The suite signs as each server with
the key in framework/var/issuer-keys/<host>/ that the registry holds.

The suite posts ads that the directors take, as a server's own, for a
server that is running, which its next ad, within
Server.AdvertisementInterval (30 seconds here), replaces: each is built
from what the director says of the server (/api/v1.0/director_ui/
servers/<name>), and lasts 90 seconds. After each scenario that does,
every director must name each server it touched again, within 150
seconds, or the scenario's `restored` row fails.

It puts an object of its own at every origin, in the first protected
namespace, or in /public where the origins export only that
(`-p origin-public`), at data/advertise/<run>-probe, about which it asks
the directors, and removes it afterward.
"""

import glob
import json
import os
import subprocess  # nosec B404
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

from testlib import accounts, common, credentials, director, jwt, metrics, stores, web
from testlib.ads import (
    ADVERTISE,
    SERVERS,
    Missing,
    Server,
    Servers,
    ad,
    namespace_ad,
    post_ad,
    sign,
    unknown_token,
)
from testlib.common import die, first
from testlib.director import expected_targets, listing_problems
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "forged-ads": (
        "an ad is taken only with its server's own key and scope, and a prefix of its kind"
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

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# Scopes, as a token carries them, beside the advertise scope
# (ads.ADVERTISE).
NOT_ADVERTISE = "storage.read:/"
DISCOVERY = "pelican.director_service_discovery"
RETRIEVE, REVERSE = "broker.retrieve", "broker.reverse"

# An issuer that no server trusts.
ELSEWHERE = "https://elsewhere.invalid"

# The registry's registrations.
REGISTRATIONS = "/api/v1.0/registry_ui/namespaces"
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


class Test(Servers):
    """The servers, keys, and tokens (ads.Servers), and the probe."""

    def __init__(self, session: Session):
        super().__init__(session.fed)
        self.session = session
        self.run = session.run
        # The first protected namespace, or /public where the origins
        # export only that (`-p origin-public`).
        self.namespace = credentials.probe_namespace(self.fed)
        self.rel = f"data/advertise/{self.run}-probe"
        self.path = f"/{self.namespace}/{self.rel}"
        self.data = os.urandom(4096)

    def caches(self) -> list[common.Cache]:
        """The caches that the directors know of."""
        return self.fed.federated_caches


def rejected(url: str) -> Optional[float]:
    """How many ads the director at url has refused (REJECTED, every
    series), or None if its /metrics did not answer."""
    snapshot = metrics.fetch(url)
    return None if snapshot is None else snapshot.value(REJECTED)


def route(server: Server) -> str:
    """The director route that names server: `origin` or `object` (a
    cache)."""
    return "origin" if server.kind == "Origin" else "object"


def naming(test: Test, server: Server) -> list[str]:
    """The directors that name server for the probe."""
    return director.naming(test.fed.directors, route(server), test.path, server.host)


def restored(test: Test, *servers: Server) -> list[str]:
    """Wait, up to RESTORE_WAIT seconds, until every director names each
    of servers for the probe; what is still wrong after that."""
    problems: list[str] = []
    wanted = len(test.fed.directors)
    for server in servers:

        def named(s: Server = server) -> bool:
            return len(naming(test, s)) == wanted

        if not common.wait_for(named, RESTORE_WAIT, 5):
            problems.append(
                f"{RESTORE_WAIT}s on, only {naming(test, server)} named {server.host} again"
            )
    return problems


def restored_row(test: Test, *servers: Server) -> Row:
    """restored()'s row."""
    problems = restored(test, *servers)
    names = ", ".join(s.host for s in servers)
    return ("restored", FAIL, first(problems)) if problems else ("restored", PASS, names)


# --------------------------------------------------------------------------
# forged-ads


def forged(
    test: Test, kind: str, body: Mapping[str, Any], token: Optional[str]
) -> tuple[list[str], list[str]]:
    """POST ad body to every director, as a server of kind, with token;
    each must refuse it (403) and count it among the ads it refused
    (REJECTED). What is wrong, and what each answered."""
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
Variant = tuple[str, str, dict[str, Any], Optional[str], Optional[tuple[str, Server]]]


def forged_ads(test: Test) -> list[Row]:
    """Post origin-0's and the first cache's own ads, as each, to every
    director, which must take them (200); then variants of each, each
    changed in one way, which every director must refuse (403)."""
    fed = test.fed
    origin = test.server("Origin", director.host(fed.origins[0].url))
    servers = [origin]
    if test.caches():
        servers.append(test.server("Cache", director.host(test.caches()[0].url)))
    rows: list[Row] = []
    variants: list[Variant] = []
    for server in servers:
        kind = server.kind.lower()
        key, kid = test.key(server)
        genuine = test.advertise_token(server)
        problems: list[str] = []
        for url in fed.directors:
            answer = post_ad(url, server.kind, ad(server), genuine)
            if answer.status != 200:
                problems.append(f"{director.host(url)}: {answer.describe()}, not 200")
        if not problems and len(naming(test, server)) != len(fed.directors):
            problems.append(f"only {naming(test, server)} name it afterward")
        rows.append(
            (
                f"{kind}-genuine",
                FAIL if problems else PASS,
                first(problems) if problems else server.prefix,
            )
        )
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
            path = test.path if prefix == f"/{test.namespace}" else f"{prefix}/probe"
            for url in fed.directors:
                if director.ask(url, route(server), path).names(server.host):
                    problems.append(f"{director.host(url)} now names {server.host} for {path}")
        rows.append(
            (name, FAIL if problems else PASS, first(problems) if problems else ", ".join(seen))
        )
    rows.append(restored_row(test, *servers))
    return rows


# --------------------------------------------------------------------------
# bad-broker-url


def bad_broker_url(test: Test) -> list[Row]:
    """Post origin-0's own ad, as it, but with a broker URL that Go's
    url.Parse refuses, to every director: each must answer 400, with one
    JSON body, and then still list its servers, and name origin-0. Pelican
    writes the 400 and goes on to read the URL it could not parse
    (finishRegisterServeAd in director/director.go), which panics, and gin
    recovers."""
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
        listed = web.request("GET", f"{url}{SERVERS}")
        if listed.status != 200:
            problems.append(
                f"{director.host(url)} then answered {SERVERS} with {listed.describe()}"
            )
    problems += restored(test, origin)
    note = f"{len(test.fed.directors)} director(s)"
    return [("", FAIL if problems else PASS, first(problems) if problems else note)]


# --------------------------------------------------------------------------
# spoofed-status


def active_downtime(source: str) -> dict[str, Any]:
    """A downtime under way, as a server's ad carries it (times in
    milliseconds)."""
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


def post_everywhere(
    test: Test, kind: str, body: Mapping[str, Any], token: str, refusable: bool = False
) -> list[str]:
    """POST ad body to every director, which must take it (200), or if
    refusable, may refuse it (403) too; what is wrong."""
    problems: list[str] = []
    for url in test.fed.directors:
        answer = post_ad(url, kind, body, token)
        if answer.status != 200 and not (refusable and answer.status == 403):
            problems.append(f"{director.host(url)} answered {answer.describe()}")
    return problems


def spoofed_status(test: Test) -> list[Row]:
    """With the first cache's own key, its ad saying it is shutting down,
    then that it is critical: within DROP_WAIT seconds, no director may
    name it, until its next ad (director.go: registerServerAd; sort.go:
    cacheNotInErrorState). Then, with a second cache's key and prefix, ads
    that name the first: by its name alone (the second's URL), saying each
    status, and with a downtime under way; and by its URL too, saying it
    is critical. The first must stay named: an advertise token proves a
    server's prefix, not its name or URL, and the director keeps the ad
    of a URL, whoever sent it (recordAd in director/cache_ads.go)."""
    caches = test.caches()
    if not caches:
        return [("", SKIP, "the directors know of no cache")]
    first_cache = test.server("Cache", director.host(caches[0].url))
    wanted = len(test.fed.directors)
    rows: list[Row] = []
    before = naming(test, first_cache)
    if len(before) != wanted:
        return [
            ("", FAIL, f"before anything was posted, only {before} named {first_cache.host}")
        ]
    for status in ("shutting down", "critical"):
        name = "own-" + status.replace(" ", "-")
        own = test.advertise_token(first_cache)
        problems = post_everywhere(test, "Cache", ad(first_cache, status=status), own)
        if not problems and not common.wait_for(
            lambda: not naming(test, first_cache), DROP_WAIT, 1
        ):
            problems.append(f"{DROP_WAIT}s on, {naming(test, first_cache)} still named it")
        problems += restored(test, first_cache)
        rows.append(
            (
                name,
                FAIL if problems else PASS,
                first(problems) if problems else "dropped, then named again",
            )
        )
    if len(caches) < 2:
        why = "needs a second cache (-p topo-multi-cache)"
        return rows + [
            (n, SKIP, why)
            for n in ("name-shutting-down", "name-critical", "name-downtime", "url-hijack")
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
        listed = web.request(
            "GET", f"{test.director}{SERVERS}/{quote(first_cache.name, safe='')}"
        )
        found = common.as_object(listed.json()) or {}
        if found.get("filtered"):
            problems.append(
                f"{director.host(test.director)} lists it filtered ({found.get('filteredType')})"
            )
        rows.append(
            (
                name,
                FAIL if problems else PASS,
                first(problems) if problems else f"{first_cache.host} still named",
            )
        )
        problems = restored(test, first_cache, second)
        if problems:
            rows.append((f"{name}-restored", FAIL, first(problems)))
    return rows


# --------------------------------------------------------------------------
# fed-token


def get_fed_token(test: Test, query: Mapping[str, str], token: Optional[str]) -> web.Response:
    """Ask the first director for a federation token, with query and
    token (none if None)."""
    url = f"{test.director}/api/v1.0/director/getFedToken"
    if query:
        url += f"?{urlencode(query)}"
    return web.request("GET", url, token=token)


def fed_token_problems(test: Test, answer: web.Response, host: str) -> list[str]:
    """What is wrong with a federation token the director minted for host,
    in answer: it must come from the federation (its discovery URL), for
    host, for any audience, to read everything, for FED_TOKEN_LIFETIME
    seconds, signed by a key of the federation's JWKS."""
    found = common.as_object(answer.json()) or {}
    token = str(found.get("access_token") or "")
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


def pending_registration(test: Test) -> Row:
    """Under `-p registry-approval`: register a cache's prefix of the run's,
    /caches/advertise-<run>, as the registry's admin, with a key made for
    it, which leaves it pending; ask for a federation token for it, with a
    token signed by that key, which must be refused (403); then remove the
    registration."""
    fed = test.fed
    host = f"advertise-{test.run}"
    prefix = f"/caches/{host}"
    key = test.session.path("advertise-pending.pem")
    done = subprocess.run(  # nosec B603 B607
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        return (
            "pending",
            FAIL,
            f"openssl could not make a key: {done.stderr.decode(errors='replace').strip()}",
        )
    jwk = jwt.public_jwk(key)
    kid = jwt.thumbprint(jwk)
    pubkey = json.dumps({"keys": [{**jwk, "kid": kid, "alg": jwt.ES256, "use": "sig"}]})
    registry = director.host(fed.registry_url)
    admin = accounts.Api(
        fed.registry_url,
        credentials.mint_admin(
            fed,
            test.session.path(f"tokens/admin.{registry}"),
            credentials.local_issuer(fed, fed.registry_url),
            common.signing_key(f"issuer-keys/{registry}"),
        ),
    )
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
    created = admin.call("POST", REGISTRATIONS, registration, headers)
    if not created.ok:
        return (
            "pending",
            FAIL,
            f"registering {prefix} as the registry's admin: {created.describe()}",
        )
    listed = admin.call("GET", f"{REGISTRATIONS}?status=Pending")
    ids = [
        str(entry.get("id", ""))
        for entry in (common.as_object(r) or {} for r in listed.array)
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
            admin.call("DELETE", f"{REGISTRATIONS}/{reg_id}", headers=headers)


def fed_token(test: Test) -> list[Row]:
    """Ask the first director for a federation token as the first cache,
    with its own advertise token: it must mint one (fed_token_problems());
    then without the host, the server type, or a token; for a director;
    with the cache's key but not the advertise scope; with the cache's
    token for another server; and with a token signed by a key no server
    knows: each must be refused (403). Pelican answers 500 to a token it
    cannot verify (getFedToken in director/fed_token.go)."""
    caches = test.caches()
    if not caches:
        return [("", SKIP, "the directors know of no cache, which asks for federation tokens")]
    cache = test.server("Cache", director.host(caches[0].url))
    host = cache.registered_host
    token = test.advertise_token(cache)
    key, kid = test.key(cache)
    web_url = str(cache.detail.get("webUrl", ""))
    rows: list[Row] = []
    answer = get_fed_token(test, {"host": host, "sType": "Cache"}, token)
    problems = fed_token_problems(test, answer, host)
    rows.append(
        ("valid", FAIL if problems else PASS, first(problems) if problems else f"for {host}")
    )
    if len(caches) > 1:
        other = test.server("Cache", director.host(caches[1].url))
        other_query = {"host": other.registered_host, "sType": "Cache"}
    else:
        other = test.server("Origin", director.host(test.fed.origins[0].url))
        other_query = {"host": other.registered_host, "sType": "Origin"}
    bad: list[tuple[str, dict[str, str], Optional[str]]] = [
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


def fed_token_at_origin(test: Test) -> list[Row]:
    """Where the origins take no direct clients (`-p origin-no-direct`,
    Origin.DisableDirectClients), a native origin serves a request only
    with a federation token beside the user's (authMiddleware in Pelican's
    origin_serve/handlers.go): a GET of the probe straight to each, with
    the `server` token and the first cache's federation token in one
    header, must get it; with the `server` token alone, 401; and with the
    federation token alone, which grants nothing in a protected
    namespace, a refusal (401 or 403), but in /public (`-p origin-public`),
    which anyone may read, the probe."""
    fed = test.fed
    if "DirectReads" in fed.exports[test.namespace]:
        return [("", SKIP, "the origins take direct clients ('-p origin-no-direct')")]
    if not fed.native_origins:
        return [("", SKIP, f"XRootD serves the origins ({fed.origin_variant}), not Pelican")]
    if not test.caches():
        return [("", SKIP, "the directors know of no cache, which asks for federation tokens")]
    cache = test.server("Cache", director.host(test.caches()[0].url))
    answer = get_fed_token(
        test, {"host": cache.registered_host, "sType": "Cache"}, test.advertise_token(cache)
    )
    federation = str((common.as_object(answer.json()) or {}).get("access_token") or "")
    if not federation:
        return [("", FAIL, f"no federation token for {cache.host}: {answer.describe()}")]
    user = test.session.token(test.namespace)
    fed_only = (200,) if test.namespace == "public" else (401, 403)
    rows: list[Row] = []
    for origin in stores.unique(fed.origins):
        url = stores.url(origin, test.namespace, test.rel)
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
    """The key that signs as the federation: of the keys in
    framework/var/issuer-keys/, the first whose public key the federation's
    JWKS (its discovery document's jwks_uri) lists, and its kid there; the
    directors' (or under `-p auth-external-issuer`, the external issuer's).
    Raises Missing if none is listed."""
    jwks_uri = str(fed.discovery.get("jwks_uri", ""))
    answer = web.request("GET", jwks_uri)
    listed = common.as_array((common.as_object(answer.json()) or {}).get("keys")) or []
    for key in sorted(glob.glob("issuer-keys/*/*.pem")):
        jwk = jwt.public_jwk(key)
        for entry in (common.as_object(e) or {} for e in listed):
            if all(entry.get(k) == v for k, v in jwk.items()):
                return key, str(entry.get("kid") or "")
    raise Missing(
        f"{jwks_uri} ({answer.describe()}) lists none of framework/var/issuer-keys/'s keys"
    )


def discover_servers(test: Test) -> list[Row]:
    """Ask each director for its servers, as Prometheus's service discovery
    does (/api/v1.0/director/discoverServers): with no token, and with one
    of the scope from an issuer it does not trust, it must refuse (403);
    with one of the scope from the federation (the discovery URL), signed
    with the directors' key, it must list every server (listing_problems())."""
    fed = test.fed
    key, kid = federation_key(fed)
    issuer = str(fed.discovery.get("discovery_endpoint", ""))
    tokens: tuple[tuple[str, Optional[str]], ...] = (
        ("no-token", None),
        ("unknown-issuer", unknown_token(DISCOVERY, ELSEWHERE, jwt.ANY_AUDIENCE)),
        ("federation", sign(key, kid, DISCOVERY, issuer, jwt.ANY_AUDIENCE)),
    )
    rows: list[Row] = []
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
            note = why or (
                f"{len(expected_targets(fed))} servers" if case == "federation" else "HTTP 403"
            )
            rows.append((f"{name} {case}", FAIL if why else PASS, note))
    return rows


# --------------------------------------------------------------------------
# broker


# A request of the broker: its row's name, its path, its token, its body,
# its timeout (X-Pelican-Timeout), and the statuses it may get.
BrokerRequest = tuple[str, str, Optional[str], dict[str, str], str, tuple[int, ...]]


def broker(test: Test) -> list[Row]:
    """Under `-p origin-broker`, ask the director's broker for requests
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
    is ever taken from origin-0's queue."""
    fed = test.fed
    if not fed.broker:
        return [("", SKIP, "the origins use no connection broker ('-p origin-broker')")]
    if not test.caches():
        return [("", SKIP, "the directors know of no cache")]
    endpoint = urlsplit(str(fed.discovery.get("broker_endpoint", "")))
    base = f"{endpoint.scheme}://{endpoint.netloc}"
    origin = test.server("Origin", director.host(fed.origins[0].url))
    cache = test.server("Cache", director.host(test.caches()[0].url))
    made_up = f"advertise-{test.run}"

    def registry_token(server: Server, scope: str) -> str:
        key, kid = test.key(server)
        issuer = f"{fed.registry_url}/api/v1.0/registry{server.prefix}"
        return sign(key, kid, scope, issuer, base, server.registered_host)

    as_origin = {"origin": made_up, "prefix": origin.prefix}
    unknown = unknown_token(
        RETRIEVE, f"{fed.registry_url}/api/v1.0/registry{origin.prefix}", base
    )
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
    rows: list[Row] = []
    for name, path, token, body, timeout, statuses in requests:
        answer = web.request(
            "POST",
            f"{base}{path}",
            token=token,
            upload=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Pelican-Timeout": timeout},
        )
        why = ""
        if answer.status not in statuses:
            why = f"{answer.describe()}, not {' or '.join(map(str, statuses))}"
        elif (
            name == "valid"
            and (common.as_object(answer.json()) or {}).get("status") != "timeout"
        ):
            why = f"its status is not 'timeout': {answer.body[:200]!r}"
        rows.append((name, FAIL if why else PASS, why or answer.describe()))
    return rows


RUN: dict[str, Callable[[Test], list[Row]]] = {
    "forged-ads": forged_ads,
    "bad-broker-url": bad_broker_url,
    "spoofed-status": spoofed_status,
    "fed-token": fed_token,
    "fed-token-at-origin": fed_token_at_origin,
    "discover-servers": discover_servers,
    "broker": broker,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: a standalone origin has no director."""
    if not fed.directors:
        return "a standalone origin has no director"
    return None


def put_probe(test: Test) -> None:
    """Put the probe at every origin, in the suite's namespace."""
    fed = test.fed
    token = test.session.token(test.namespace)
    errors = stores.make_dirs(fed.origins, test.namespace, "data/advertise", token)
    for origin in stores.unique(fed.origins):
        if errors:
            break
        error = stores.seed(
            origin, test.namespace, test.rel, test.data, token, fed.exports[test.namespace]
        )
        if error:
            errors.append(error)
    if errors:
        die(f"could not put the probe at every origin: {first(errors)}")


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the probe in place, run the selected scenarios, and remove it."""
    test = Test(session)
    put_probe(test)
    print(f"Asking the directors about {test.path}\n")
    try:
        for name in selected:
            try:
                rows = RUN[name](test)
            except (Missing, accounts.ApiError) as e:
                rows = [("", FAIL, str(e))]
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                rows = [("", FAIL, f"{type(e).__name__}: {e}")]
            for case, status, note in rows:
                results.add(f"{name} {case}" if case else name, status, note)
    finally:
        fed = session.fed
        tokens = {test.namespace: session.token(test.namespace)}
        errors = stores.empty_tree_everywhere(
            fed.origins, {test.namespace: fed.exports[test.namespace]}, "data/advertise", tokens
        )
        if errors:
            common.warn(f"could not remove the probe: {first(errors)}")
