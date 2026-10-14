"""A V2 cache's own HTTP APIs, beside the objects it serves.

They are on its web port (local_cache/persistent_cache_api.go in
Pelican), open to:

  /pelican/api/v1.0/prestage, /evict   anyone whose token may read (or,
                                       to evict, modify) the path
  /api/v1.0/cache/stats                anyone
  /api/v1.0/cache/purge*               its admin, with localcache.purge
  /api/v1.0/cache/introspect/...       its admin

A V2 cache keys each object by its URL at the director, e.g.
pelican://director-0:8444/protected-a/data/0.1 (normalizePath in
local_cache/persistent_cache.go), which the introspection routes take
as `url`.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

from . import common, credentials, web
from .session import Session

PRESTAGE = "/pelican/api/v1.0/prestage"
EVICT = "/pelican/api/v1.0/evict"
STATS = "/api/v1.0/cache/stats"
PURGE = "/api/v1.0/cache/purge"
PURGE_FIRST = "/api/v1.0/cache/purge_first"
PURGE_TO_TARGET = "/api/v1.0/cache/purge_to_target"
INTROSPECT = "/api/v1.0/cache/introspect"

# The scope that the purge routes require of a token, on top of their
# admin's (token.Verify in purgeCmd and its siblings), as `pelican cache
# purge` mints it (cmd/cache_evict.go).
PURGE_SCOPE = "localcache.purge"

JSON = {"Content-Type": "application/json"}

# How long the admin's tokens last, in seconds: a suite keeps one per
# cache for as long as it runs.
TOKEN_LIFETIME = 3600


def admin_token(
    session: Session, cache: common.Cache, extra_scopes: tuple[str, ...] = ()
) -> str:
    """Return a token that cache's web API takes as its admin's.

    It is minted as credentials.mint_user() mints one, signed with the
    cache's own key, with extra_scopes too (e.g. PURGE_SCOPE), lasting
    TOKEN_LIFETIME.
    """
    issuer = credentials.local_issuer(session.fed, cache.web_url)
    key = common.signing_key(f"issuer-keys/{cache.svc}")
    name = "-".join(("admin", *extra_scopes))
    out = session.path(f"tokens/cacheapi.{cache.svc}.{name}")
    return credentials.mint_user(
        session.fed, out, issuer, key, "admin", TOKEN_LIFETIME, extra_scopes
    )


def object_url(fed: common.Federation, path: str) -> str:
    """Return the URL by which a V2 cache keys the object at path in fed.

    path is e.g. /protected-a/data/0.1; the URL is at the director the
    cache knows of (Federation.DirectorEndpoint).
    """
    director = urlsplit(str(fed.discovery["director_endpoint"])).netloc
    return f"pelican://{director}{quote(path)}"


def decoded(answer: web.Response) -> Any:
    """Return answer's body as JSON, or None if it is not JSON."""
    try:
        return json.loads(answer.body)
    except ValueError:
        return None


def stats(cache: common.Cache) -> Optional[dict[str, Any]]:
    """Return what cache's /api/v1.0/cache/stats says.

    Returns:
        Its PersistentCacheStats: TotalUsage, DirStats, NamespaceUsage,
        and ConsistencyStats; or None if it does not answer 200 with a
        JSON object.
    """
    answer = web.request("GET", cache.web_url + STATS)
    if answer.status != 200:
        return None
    return common.as_object(decoded(answer))


def introspect(
    cache: common.Cache, token: str, method: str, route: str, **query: str
) -> web.Response:
    """Call cache's introspection route with method, as the holder of token.

    route is e.g. `metadata`, and query the parameters of the URL's
    query.
    """
    url = f"{cache.web_url}{INTROSPECT}/{route}"
    if query:
        url += "?" + urlencode(query)
    return web.request(method, url, token=token, timeout=300)


@dataclass(frozen=True)
class Blocks:
    """Which of an object's blocks a V2 cache holds.

    This is how its introspection summarizes them (BlockSummary in
    local_cache/introspect.go).

    Attributes:
        total: How many blocks the object has.
        held: How many of them the cache holds.
        missing: The first 100 it lacks, in order.
        complete: Whether it holds them all.
    """

    total: int
    held: int
    missing: tuple[int, ...]
    complete: bool

    def describe(self) -> str:
        """Return the summary, for a note."""
        if self.complete:
            return f"all {self.total} blocks"
        return f"{self.held} of {self.total} blocks"


def blocks_held(cache: common.Cache, token: str, url: str) -> tuple[Optional[Blocks], str]:
    """Return which blocks of the object that cache keys by url it holds.

    url is as object_url() makes it. The cache's admin, the holder of
    token, asks introspect/metadata. An object stored inline in the
    database has no block summary.

    Returns:
        The blocks, and ""; or None, and why the cache did not say.
    """
    answer = introspect(cache, token, "GET", "metadata", url=url)
    if answer.status != 200:
        return None, f"introspect/metadata answered {answer.describe()}"
    found = common.as_object(decoded(answer)) or {}
    summary = common.as_object(found.get("block_summary"))
    if summary is None:
        return None, "introspect/metadata gave no block summary"
    missing = common.as_array(summary.get("missing_blocks")) or []
    return (
        Blocks(
            int(summary.get("total_blocks") or 0),
            int(summary.get("downloaded_blocks") or 0),
            tuple(int(n) for n in missing),
            bool(summary.get("is_complete")),
        ),
        "",
    )
