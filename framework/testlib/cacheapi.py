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

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from . import blocks, common, web
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


def admin_token(session: Session, cache: common.Cache, extra_scopes: tuple[str, ...] = ()) -> str:
    """Return a token that cache's web API takes as its admin's.

    It is Session.web_token()'s, with extra_scopes too (e.g. PURGE_SCOPE).
    """
    return session.web_token(cache.web_url, "admin", extra_scopes)


def object_url(fed: common.Federation, path: str) -> str:
    """Return the URL by which a V2 cache keys the object at path in fed.

    The URL is at the director the cache knows of
    (Federation.DirectorEndpoint).

    Args:
        fed: The federation.
        path: The object's path, e.g. /protected-a/data/0.1.
    """
    director = urlsplit(str(fed.discovery["director_endpoint"])).netloc
    return f"pelican://{director}{quote(path)}"


def prestage(cache: common.Cache, path: str | None, token: str | None) -> web.Response:
    """GET the prestage API of cache for path, with token.

    The API streams its progress until the object is whole.

    Args:
        cache: The cache to ask.
        path: The object's path in the federation; None for no `path`.
        token: The token to present, if any.
    """
    query = "" if path is None else "?" + urlencode({"path": path})
    return web.request("GET", f"{cache.web_url}{PRESTAGE}{query}", token=token, timeout=120)


def evict(
    cache: common.Cache,
    path: str,
    token: str | None,
    immediate: bool = True,
    method: str = "POST",
) -> web.Response:
    """Send method to the eviction API of cache for path, with token."""
    query = {"path": path, **({"immediate": "true"} if immediate else {})}
    return web.request(method, f"{cache.web_url}{EVICT}?{urlencode(query)}", token=token)


def stats(cache: common.Cache) -> dict[str, Any] | None:
    """Return what cache's /api/v1.0/cache/stats says.

    Returns:
        Its PersistentCacheStats: TotalUsage, DirStats, NamespaceUsage,
        and ConsistencyStats; or None if it does not answer 200 with a
        JSON object.
    """
    answer = web.request("GET", cache.web_url + STATS)
    if answer.status != 200:
        return None
    return common.as_object(answer.json())


def introspect(cache: common.Cache, token: str, method: str, route: str, **query: str) -> web.Response:
    """Call cache's introspection route with method, as the holder of token.

    Args:
        cache: The cache.
        token: The token to present, e.g. admin_token()'s.
        method: The HTTP method.
        route: The route, e.g. `metadata`.
        **query: The parameters of the URL's query.
    """
    url = f"{cache.web_url}{INTROSPECT}/{route}"
    if query:
        url += "?" + urlencode(query)
    return web.request(method, url, token=token, timeout=300)


def chaos(cache: common.Cache, token: str, url: str, op: str, **params: str) -> web.Response:
    """Have cache spoil its copy of the object it keys by url.

    Its chaos API (Cache.EnableChaosAPI, which config.d/cache/v2 turns
    on), one of its introspection routes, does; a cache without one
    answers 404.

    Args:
        cache: The V2 cache.
        token: A token of its admin (admin_token()).
        url: The object's key (object_url()).
        op: Either `corrupt` (a block, with `block` and `bytes`) or
            `truncate` (a chunk file, with `chunk` and `drop-bytes`).
        **params: The parameters of op.
    """
    return introspect(cache, token, "POST", "chaos", op=op, url=url, **params)


def corrupt(cache: common.Cache, token: str, url: str, block: int) -> web.Response:
    """Have cache corrupt block of its copy of the object (see chaos()).

    The cache flips the first blocks.TAG bytes of the block (zero-based)
    on disk, so that its authentication tag fails (CorruptBlock in
    local_cache/chaos.go).
    """
    return chaos(cache, token, url, "corrupt", block=str(block), bytes=str(blocks.TAG))


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


def _metadata(cache: common.Cache, token: str, url: str) -> tuple[dict[str, Any] | None, str]:
    """Return what introspect/metadata says of the object cache keys by url.

    Returns:
        Its answer, and ""; or None, and why it did not answer 200.
    """
    answer = introspect(cache, token, "GET", "metadata", url=url)
    if answer.status != 200:
        return None, f"introspect/metadata answered {answer.describe()}"
    return answer.object(), ""


def blocks_held(cache: common.Cache, token: str, url: str) -> tuple[Blocks | None, str]:
    """Return which blocks of the object that cache keys by url it holds.

    It asks introspect/metadata. An object stored inline in the database
    has no block summary.

    Args:
        cache: The cache.
        token: A token of the cache's admin (admin_token()).
        url: The object's key (object_url()).

    Returns:
        The blocks, and ""; or None, and why the cache did not say.
    """
    found, why = _metadata(cache, token, url)
    if found is None:
        return None, why
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


def held_inline(cache: common.Cache, token: str, url: str) -> tuple[bool | None, str]:
    """Return whether cache holds the object it keys by url inline.

    That is in its database, rather than in a file of blocks: a V2 cache
    keeps an object under 4,096 bytes so (InlineMaxBytes, in
    local_cache/persistent_cache.go). introspect/metadata's is_inline
    says.

    Args:
        cache: The cache.
        token: A token of the cache's admin (admin_token()).
        url: The object's key (object_url()).

    Returns:
        Whether it does, and ""; or None, and why the cache did not say.
    """
    found, why = _metadata(cache, token, url)
    if found is None:
        return None, why
    inline = found.get("is_inline")
    if not isinstance(inline, bool):
        return None, "introspect/metadata did not say whether it is inline"
    return inline, ""
