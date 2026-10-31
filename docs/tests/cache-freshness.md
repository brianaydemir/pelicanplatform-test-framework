# Cache freshness

Part of [What the tests check](README.md).

The `freshness` suite checks that each federated cache keeps a copy as long
as the origins' `Cache-Control` allows (RFC 9111; `Origin.CacheControl`),
and tells clients, by `Cache-Control` and `Age`, how fresh the copy is.
Where the origins send nothing, a V2 cache keeps a copy a day
(`LocalCache.DefaultMaxAge`), less up to 10%
(`LocalCache.RevalidationJitter`). Only `no-negative-cache` asks XRootD
caches; none asks a site-local cache
([what is not tested](what-is-not-tested.md)).

The suite runs in phases, one per `Cache-Control` value. The first takes
what the origins send as the shape is: nothing, or `max-age=30` under
`-p origin-max-age`. For each other value, the suite restarts every origin
with `Origin.CacheControl` set to it
([service control](../../README.md#service-control)), runs the cases that
need it, then restarts them without it. After each restart, directors and
caches must reach the origins within 150 seconds. The restarts need native
origins, a control helper, no `ORIGIN_CACHE_CONTROL`, and a topology other
than `tiny`; elsewhere, the cases that need them skip. A scenario with
several cases has a row per case, as in `cc-immediate max-age=0`.

- `only-if-cached`: Asked by `Cache-Control: only-if-cached` (RFC 9111,
  section 5.2.1.7) or `X-Pelican-NoDownload: true`, a V2 cache must serve
  what it holds, and answer a GET or HEAD of what it lacks with 504,
  fetching nothing (`pelican_http_bytes_total`). A plain HEAD must fetch
  nothing either.

- `no-negative-cache`: An object no origin holds must get 404 at each cache
  (a V2 cache's JSON `error` naming `not_found`) and director. Once every
  origin holds it, each must serve it at once, or, with several origins,
  within 75 seconds (`Director.CachePresenceTTL`). Directors are not asked
  under `-p director-default-origin -p origin-no-direct`.

- `cc-downstream`: Every answer of a V2 cache, to a fill, two hits 3 seconds
  apart, and a conditional GET (304), must carry `Cache-Control`:

  - `none` (origins send nothing): `max-age` of the cache's remaining
    freshness, at most a day, and at least 21.6 hours less the time since
    the fill;
  - `max-age` (origins send `max-age=30`): `s-maxage=30, max-age=30`.

  Each `Age` must be at most the time since the fill, and the two hits' must
  differ by the time between them, within 2 seconds.

- `propfind-freshness`: A V2 cache's Depth 0 PROPFIND of its copy:

  - `empty`: of an empty object, a `getcontentlength` of 0;
  - `overwrite` (`max-age=30`): once its copy of an object the origins
    lengthened is stale, the new size and another `ETag`, within 15 seconds,
    with no GET between.

- `stale-if-error` (`max-age=30`): With every origin stopped, each V2 cache
  must serve a stale copy it holds (RFC 9111, section 4.2.4), and fail a
  read of one it lacks, never with other bytes. Once the origins start
  again, the federation must serve as before. Skips without a control
  helper, and under `tiny`.

- `must-revalidate` (`max-age=30, must-revalidate`): The same, but the cache
  must answer 504 instead of the stale copy (RFC 9111, section 5.2.2.2).

- `cc-immediate`, cases `max-age=0` and `no-cache`: `blocks`'s
  `overwrite-cached` and `overwrite-range-first`
  ([block boundaries](block-boundaries.md)). Each V2 cache must serve an
  overwrite at the first whole read; under `no-cache`, within its 5-second
  grace between revalidations, and 3 seconds more. Then every read must be
  the new version alone.

- `cc-not-stored`, cases `no-store` and `private`: A shared cache must not
  store the object (RFC 9111, sections 5.2.2.5 and 5.2.2.7). Each V2 cache
  must serve it and its ranges (or the whole, with `Accept-Ranges: none`),
  with the directive and no `Age`. Then `only-if-cached` must get 504, and
  neither the cache's introspection nor its store may show the object.

- `s-maxage` (`max-age=120, s-maxage=20`): Each V2 cache, being shared, must
  take `s-maxage` over `max-age` (RFC 9111, section 5.2.2.10), and serve an
  overwrite within 35 seconds of reading the old version.
