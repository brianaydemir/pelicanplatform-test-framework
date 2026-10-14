# Cache APIs

Part of [What the tests check](README.md).

Besides the objects it serves, a V2 cache serves APIs of its own on its
web port (`local_cache/persistent_cache_api.go` and
`prestage_evict_api.go` in Pelican): prestaging and evicting objects,
for whoever may read or modify them, which the client's `pelican object
prestage` and `pelican object evict` call; its usage
(`/api/v1.0/cache/stats`), for anyone; and purges and introspection, for
its admin alone, among them a chaos API that corrupts what it holds
(`Cache.EnableChaosAPI`, which the framework turns on). The `cache-api`
suite checks them at each federated V2 cache, with objects of its own
under `/protected-a/data/cache-api/<run>/`, put at every origin, a set
per cache, so that each is cold where it is first read. Whether a cache
holds an object it asks with a HEAD with `Cache-Control:
only-if-cached`, which gets 200 if it holds all of it, and 504 if not
([cache freshness](cache-freshness.md)). A site-local cache serves the
same APIs, and is left to `sitelocal`. XRootD caches serve none of them
but prestage, through the xrdhttp-pelican plugin, so only `prestage
client` asks them. A scenario records a row per case, named `<scenario>
<case>`.

- `prestage`: `GET /pelican/api/v1.0/prestage?path=<path>` fetches an
  object whole, and streams its progress in chunked `text/plain` lines,
  `status: queued` and `status: active,offset=<bytes>`, until a last
  line of `success: ok`, or of `failure: <code>(<text>): <message>`.
  `probe`: with no `path`, which is how the client asks whether a cache
  has the API, and with a relative one, it must get 400. `refused`: with
  no token, or one scoped elsewhere, 403, with the object left
  uncached. `object`: of an object the cache lacks, with a read-only
  token, the progress, ending well, after which the cache must hold the
  object, and serve it; and a second prestage of it must end well too.
  `missing`: of an object no origin holds, 200, since the cache has
  answered before it knows, and a last line of `failure: 404(Not
  Found): ...`. Pelican answers `failure: 500(Internal Server Error)`,
  since nothing makes the status that the handler would report for a
  missing object (`statusCodeErr` in `prestage_evict_api.go`), so the
  row fails, on main and v26.0.0-rc.3 alike. `encoded-name`: of an
  object named `plus+<cache>`, its path encoded once in the query, as
  the client sends it, beside a decoy named `plus <cache>`: the cache
  must hold the first afterward, and not the decoy. Pelican decodes
  `path` twice, once as a query and again with `url.QueryUnescape`, so
  it takes the `+` for a space and prestages the decoy, and the row
  fails, on main and v26.0.0-rc.3 alike. `client`: at every cache, V2 or
  XRootD, `pelican object prestage --cache` of an object must exit 0 and
  leave the cache holding it (an XRootD cache's `.cinfo` showing every
  block); of a collection, where `/protected-a` takes listings and
  direct clients, so that the client can list it, every object in it;
  and of a missing object it must exit non-zero.
- `evict`: `GET`, `POST`, or `DELETE /pelican/api/v1.0/evict?path=<path>`
  evicts every object the cache holds at that path or under it, at once
  with `immediate=true`, and otherwise marks them to go first when it
  next evicts; it takes a token with `storage.modify` for the path.
  `refused`: an immediate eviction of an object the cache holds, with no
  token, a read-only one, or one scoped elsewhere, must get 403, and
  leave it held; as must one of an object in `/public`, with no token
  and with `/protected-a`'s, which is for another namespace, whether or
  not `/public` takes writes (`-p origin-public-writes`).
  `immediate`: by each method, with the `server` token, 200 and
  `Evicted 1 objects under <path>`, after which the cache must not hold
  the object, and a GET must fetch it again. `marked`: without
  `immediate`, 200 and `Marked 1 objects for priority eviction under
  <path>`, with the object still held. `prefix`: of `foo`, while the
  cache holds `foo/a`, `foo/b`, and `foobar`, `Evicted 2 objects`, and
  `foobar` still held, since a prefix matches whole path segments.
  `encoded-name`: of `plus+<cache>`, beside a decoy `plus <cache>`, both
  held, the first gone, and the decoy held. Pelican decodes `path` twice
  here too, and evicts the decoy, so the row fails, on main and
  v26.0.0-rc.3 alike. `client`: `pelican object evict --immediate` of
  an object every V2 cache holds, which asks the first cache the
  director names, must print `Evicted 1 objects under <path>`, and leave
  one cache without it; without `--immediate`, `Marked 1 objects ...`,
  and every cache holding it. Each object marked is then evicted, which
  drops its mark too, so that `purge` finds only its own. `pelican cache
  evict` reads the cache's own configuration and address file, so it
  runs only beside the cache, and is not tested.
- `admin-authz`: a row per cache and admin route: `POST` to
  `/api/v1.0/cache/purge`, `purge_first`, and `purge_to_target`; `GET`
  of `/api/v1.0/cache/introspect/list`, `etags`, `metadata`, and
  `stats`; and `POST` to `introspect/verify`, `disk-usage`,
  `consistency`, and `chaos`. Each request would harm nothing were it
  let through: `chaos` and the routes that take an object name one no
  cache holds, `consistency` has both its scans off, `purge_to_target`
  has a target of 0, and `purge_first` no body. With no token, each
  must get 401; with `/protected-a`'s `server` token, and with another
  server's admin token (the first origin's, signed with its own key;
  in the `tiny` topology, whose servers share one, one naming the
  cache's issuer but signed by `unknown.pem`), 401 or 403. With the
  cache's own admin token, as `pelican cache introspect` mints it, each
  introspection route must give its own answer to the request: 200 for
  `list`, `etags`, `stats`, and `disk-usage`, 404 for `metadata` and
  `verify`, and 400 for `consistency` and `chaos`. A purge route also
  wants `localcache.purge` (`token.Verify` in `purgeCmd` and its
  siblings), as `pelican cache purge` mints it: with the admin token
  alone, it must get 403, and with the scope too, 400 for both requests.
  `purge` itself is sent with that scope only by the `purge` scenario.
  The `chaos` row skips where the admin finds no such route.
- `purge`, with the admin token that has `localcache.purge`.
  `to-target`: of a cache holding objects of the run's,
  `purge_to_target` with `target_bytes` 0 must get 400; with a byte
  below what the cache holds, 200, with `freed_bytes` above 0, after
  which it must hold no more than that. What a cache holds, here, is
  what its stats' `NamespaceUsage` charges the storage that `DirStats`
  names, the counters in its database by which it purges. `first`: of
  three objects it holds, read in turn, `purge_first` of the last read
  must get 200, and then a purge to a byte below what it holds must
  evict that one, and leave the other two held, since a purge takes the
  objects marked to go first before any other (`EvictByLRU` in
  `local_cache/database.go`). An object an earlier run marked and left
  may go first instead, so the purge is repeated, up to three times,
  until the marked object goes; the row's note says if it took more than
  one. `all`: `purge` must get 200, and under `-p cache-small` leave the
  cache holding no more than its low water mark. A cache already
  evicting answers a purge 500, so a purge that gets one is sent again,
  up to three times, five seconds apart.
- `allowed-federations`: a V2 cache serves its data, besides at bare
  paths, under `/api/v1.0/cache/data/<discovery host>/<path>`, only for
  the federations in `Cache.AllowedFederations`, which is empty by
  default and so allows only its own: otherwise anyone could use it to
  fetch from anywhere. `foreign`: an object asked for under
  `evil.invalid%3A443` must get 403, with JSON whose `error` is
  `federation_not_allowed`, and none of the object's bytes, and the
  cache must not hold it afterward. `own`: under the federation's own
  discovery host, as is, with its colon escaped, and in upper case,
  which the cache compares without regard to case, the object's bytes.
- `usage`: `accounting`: of three objects of 100,000 bytes, each read
  cold and whole, the cache stores each in a file of its blocks and a
  16-byte tag for each, 100,400 bytes. What it charges `/protected-a`
  (`NamespaceUsage`, by the namespace IDs that
  `introspect/stats`'s `namespace_names` names) must grow by exactly
  three times that, and what it holds in all (`TotalUsage`) by at
  least that; its object files on disk by that, give or take 64 KiB;
  and what its introspection measures of them (`introspect/disk-usage`)
  must agree with the files, as closely. Objects under 4,096 bytes
  would be stored in its database, so these are larger. Under
  `-p cache-small`, the cache is first purged to its low water mark,
  so that the reads evict nothing. `water-marks`: under
  `-p cache-small`, its stats' `DirStats` must give its high and low
  water marks, 128 and 96 MiB; elsewhere, the row skips.
- `consistency`: of an object the cache holds whole, its chaos API
  corrupts block 1 in its store, and three rows judge what follows.
  `verify`: its `introspect/verify` must say `valid: false`.
  `scan-counts`: its data scan (`introspect/consistency` with
  `metadata=false`, a stream of server-sent events ending in `done`)
  must count a checksum mismatch more than before it, and
  `pelican_cache_data_scan_inconsistent_objects_total` must rise.
  `scan-drops`: the scan must drop the object, which the cache must no
  longer hold, and a GET must then fetch it intact. A scan must catch
  what a reader would. Pelican's reads the object through its
  decryption, whose check of each block's tag fails first, and takes
  that for an error reading the object, which it logs, counting no
  mismatch and keeping the object (`verifyObjectChecksum` in
  `local_cache/consistency.go`), so the `scan-counts` and `scan-drops`
  rows fail, on main and v26.0.0-rc.3 alike. A cache whose verify finds
  the object valid has not seen the corruption, as where it holds the
  block in memory (`-p cache-memory`), and leaves all three rows
  inconclusive.
  `orphan`: a stray file, named as an object's would be and made an
  hour old, since a metadata scan passes over younger files, planted in
  the cache's store; its metadata scan (`data=false`) must remove it,
  and count an orphaned file more than before. Where the suite cannot
  write the store, the row skips.
