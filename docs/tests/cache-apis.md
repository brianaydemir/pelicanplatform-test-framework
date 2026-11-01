# Cache APIs

Part of [What the tests check](README.md).

The [`cache-api`](../../framework/suites/cache_api.py) suite checks each
federated [V2 cache](../glossary.md#native-and-xrootd)'s own APIs: prestage
and evict, which [`pelican object prestage`](../glossary.md#pelican-client)
and `evict` call; usage (`/api/v1.0/cache/stats`); and the admin's purge and
introspection routes, among them a chaos API (`Cache.EnableChaosAPI`).
Whether a cache holds an object is asked with
`Cache-Control: only-if-cached` ([cache freshness](cache-freshness.md)).
XRootD caches serve only prestage, which `prestage client` alone asks of
them. `purge` and `usage` run in the WLCG [pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)). A
[case](../glossary.md#case)'s row is named `<scenario> <case>`.

- `prestage`: `GET /pelican/api/v1.0/prestage?path=<path>` fetches an object
  whole, streaming its progress, and ends with `success: ok` or
  `failure: <code>(<text>): <message>`.

  - `probe`: No `path`, or a relative one: 400.
  - `refused`: No token, or one scoped elsewhere: 403, nothing cached.
  - `object`: A cold object, with a read token: success, then the object
    held, and a second prestage succeeding.
  - `missing`: An object no origin holds: 200, then
    `failure: 404(Not Found): ...`.
  - `encoded-name`: `plus+<cache>`, encoded once: the cache must hold it,
    and not a decoy `plus <cache>`.
  - `client`: At every cache, V2 or XRootD,
    `pelican object prestage --cache` of an object, or of a collection where
    `/protected-a` takes listings and direct clients, must exit 0 and leave
    each object held; of a missing object, non-zero.

- `evict`: `GET`, `POST`, or `DELETE /pelican/api/v1.0/evict?path=<path>`
  with `storage.modify` evicts what the cache holds at or under the path: at
  once with `immediate=true`, else by marking it to go first.

  - `refused`: With no token, a read-only one, or one scoped elsewhere: 403,
    the object kept. Likewise in `/public`, with no token or
    `/protected-a`'s.
  - `immediate`: By each method: 200 and `Evicted 1 objects under <path>`,
    the object gone.
  - `marked`: Without `immediate`: 200 and
    `Marked 1 objects for priority eviction under <path>`, the object kept.
  - `prefix`: Of `foo`, with `foo/a`, `foo/b`, and `foobar` held:
    `Evicted 2 objects`, `foobar` kept.
  - `encoded-name`: Of `plus+<cache>`: it goes, and the decoy stays.
  - `client`: `pelican object evict --immediate` of an object every V2 cache
    holds must print `Evicted 1 objects under <path>`, and leave one cache
    without it; without `--immediate`, `Marked 1 objects ...`, every cache
    keeping it.

- `admin-authz`: A row per cache and route, as
  `admin-authz cache-0 purge-first`. The routes are `purge`, `purge-first`,
  and `purge-to-target`, and the introspection routes `list`, `etags`,
  `metadata`, `stats`, `verify`, `disk-usage`, `consistency`, and `chaos`.
  Each request would harm nothing if let through. Each route must answer:

  - with no token, 401;
  - with the `server` token, or another server's admin token, 401 or 403;
  - with the cache's admin token, as `pelican cache introspect` mints it,
    the introspection route's own answer: 200 for `list`, `etags`, `stats`,
    and `disk-usage`; 404 for `metadata` and `verify`; 400 for `consistency`
    and `chaos`;
  - at a purge route, 403 without `localcache.purge`, and 400 with it (not
    asked of `purge`).

  `chaos` [skips](../glossary.md#skip) where the cache has no such route.

- `purge`, with `localcache.purge`, after reading three objects:

  - `to-target`: `purge_to_target` with `target_bytes` 0: 400. With a target
    a byte below what the cache holds: 200, `freed_bytes` above 0. The cache
    must then hold no more than the target, and keep the object read last
    (least recently used go first). With one storage directory,
    `evicted_objects` must be 1.
  - `first`: `purge_first` of the object read last: 200. A purge to a byte
    below what the cache holds must then evict that object alone.
  - `all`: `purge`: 200. Under
    [`-p cache-small`](../../framework/presets/cache-small.sh), the cache
    must then hold no more than its low water mark.

- `allowed-federations`: A V2 cache must serve
  `/api/v1.0/cache/data/<discovery host>/<path>` only for the federations in
  `Cache.AllowedFederations` (by default, its own).

  - `foreign`: Under `evil.invalid%3A443`: 403, a JSON `error` of
    `federation_not_allowed`, and nothing served or cached.
  - `own`: Under its own [discovery host](../glossary.md#discovery-host), as
    is, with its colon escaped, and in upper case: the object.

- `usage`:

  - `accounting`: Three objects of 100,000 bytes, read cold, are stored as
    100,400 bytes each (a 16-byte tag per block). `NamespaceUsage` for
    `/protected-a` must grow by exactly three times that; `TotalUsage` by at
    least that; and the files on disk by that, give or take 64 KiB, with
    `introspect/disk-usage` agreeing as closely.
  - `water-marks`: Under `-p cache-small`, `DirStats` must give water marks
    of 128 and 96 MiB. Skips elsewhere.

- `consistency`:

  - `verify`, `scan-counts`, and `scan-drops`: With block 1 of a held object
    corrupted by the chaos API, `introspect/verify` must say `valid: false`
    (`verify`). The data scan (`introspect/consistency`) must count more
    checksum mismatches and raise
    `pelican_cache_data_scan_inconsistent_objects_total` (`scan-counts`),
    and drop the object, so a GET fetches it intact (`scan-drops`).
    [Inconclusive](../ci.md#results) where verify finds the object valid, as
    under [`-p cache-memory`](../../framework/presets/cache-memory.sh);
    skipped without a chaos API.
  - `orphan`: The metadata scan must remove, and count, an hour-old stray
    file in the store. Skips where the suite cannot write it.
