# Cache freshness

Part of [What the tests check](README.md).

An origin tells a cache how long its copy of an object stays fresh with
`Cache-Control` (Pelican's `Origin.CacheControl`, which only a native
origin sends; RFC 9111). A V2 cache keeps what the origin says, and
decides for itself where it says nothing (`local_cache/cache_control.go`
in Pelican): a day, less up to 10% (`LocalCache.DefaultMaxAge` and
`LocalCache.RevalidationJitter`). It tells its own clients how fresh its
copy is, with `Cache-Control` and `Age`. The `freshness` suite checks
both, at each federated cache, with objects of its own under
`/protected-a/data/freshness/<run>/`, put at every origin. A site-local
cache's upstream is another cache, whose `Cache-Control` is its own, so
the suite leaves it to `sitelocal`. XRootD caches do not read
`Cache-Control` as a V2 cache does, so only `no-negative-cache` asks
them.

Each case needs the origins to send a `Cache-Control` of its own, or
any. The suite runs the cases in phases, one per value: first those that
take what the origins send as the shape is, which is nothing, or
`max-age=30` under `-p origin-max-age`; then, for each other value, it
restarts every origin with `Origin.CacheControl` set to it, as the last
of its configuration layers ([service control](../../README.md#service-control)),
runs the cases that need it, and restarts the origins again without it.
Before a phase's cases, and after it, every director must name every
origin, and every cache answer a HEAD of a probe object, within 150
seconds; and where the origins take direct clients, each must send the
value. `ORIGIN_CACHE_CONTROL` reaches Pelican by environment, which no
file overrides, so the restarts need a shape without it, of native
origins, with a control helper, and not the `tiny` topology, whose
origin is the whole federation's process. Elsewhere, the cases that
need a restart skip, and say why. A case's row is named `<scenario>
<case>`, e.g. `cc-immediate max-age=0`.

- `only-if-cached`: a client may ask a cache for only what it holds
  (`Cache-Control: only-if-cached`, RFC 9111 section 5.2.1.7, or the
  older `X-Pelican-NoDownload: true`), and a V2 cache must answer 504 if
  it holds none. At each V2 cache, a GET and a HEAD of a 1 MiB object it
  lacks, asked each way, must get 504, as must a GET 2 seconds later;
  meanwhile, the native origins must send fewer bytes for GETs than one
  such object (`pelican_http_bytes_total`, which other traffic moves
  too, but not by as much). A plain HEAD of another such object must
  get its `Content-Length`, and fetch nothing: `only-if-cached` must
  then get 504. An object the cache holds whole (it sends `Age`) must
  come back to a GET asked either way, and to a HEAD with
  `only-if-cached`.
- `no-negative-cache`: a GET of an object that no origin holds yet,
  at each cache, must get 404, a V2 cache's a JSON body naming
  `not_found` (or `Not Found`, as v26.0.0-rc.3 spells its `error`); as
  must one at each director's bare path, followed to
  wherever it leads. Once every origin holds the object, each must
  serve it at the first try. Where there are several origins, a
  director asks them which holds an object, and keeps an answer that
  none does for a minute (`Director.CachePresenceTTL`), so there each
  may take 75 seconds; the row's note says how long they took.
- `cc-downstream`: at each V2 cache, a cold GET of an object, which
  fills the cache; once it holds it, two GETs 3 seconds apart; and a
  GET with `If-None-Match` naming its `ETag`, which must get 304. Each
  answer must carry `Cache-Control`, the 304 too. With the origins
  sending none (`cc-downstream none`), it must be `max-age` of what
  remains of the cache's own freshness: at most a day, and at least
  21.6 hours less the time since the fill. With the origins sending
  `max-age=30` (`cc-downstream max-age`), it must be `s-maxage=30,
  max-age=30`, so that a cache downstream, which is shared, keeps it as
  long (`ResponseCacheControl` in `local_cache/schema.go`). The fill may
  carry no `Age`, or one of at most 2; each later `Age` must be no more
  than the time since the fill, and the second must exceed the first by
  the time between them, within 2 seconds.
- `propfind-freshness`: a V2 cache answers a Depth 0 PROPFIND from its
  copy, if it holds one. `propfind-freshness empty`: of an empty object
  it holds, it must say that it is 0 bytes long, since every object has
  a `getcontentlength`, 0 included; if the cache never says, with `Age`,
  that it holds the object, a pass is inconclusive, since an origin may
  have answered. Pelican writes `getcontentlength` only for an object
  longer than 0 bytes (`buildPropfindMultistatus` in
  `local_cache/persistent_cache_api.go`), so the row fails, on main and
  v26.0.0-rc.3 alike. `propfind-freshness overwrite`, under
  `max-age=30`: an object the cache holds is replaced at the origins by
  one 1,000 bytes longer; once its copy is stale, Depth 0, with no GET
  in between, must say the new size, and an `ETag` other than the old
  one (where the origins take direct clients, one of theirs), within 15
  seconds. Pelican answers Depth 0 from whatever copy it holds, stale
  or not (`servePropfindFromCache` in
  `local_cache/persistent_cache_api.go`), so the row fails, on main and
  v26.0.0-rc.3 alike.
- `stale-if-error`, under `max-age=30`: at each V2 cache, an object it
  holds, which is then stale, and another it never read. With every
  origin stopped, the first must still come back, its bytes, as a V2
  cache serves its copy when it cannot revalidate it
  (`revalidateObject` in `local_cache/persistent_cache.go`); the other
  must fail, and never come back with other bytes. Then the origins
  start again, and the federation must serve as before. It skips
  without a control helper, and in the `tiny` topology.
- `cc-immediate`: under `max-age=0`, `no-cache`, and
  `must-revalidate, max-age=0`, `blocks`'s `overwrite-cached` and
  `overwrite-range-first` ([block boundaries](block-boundaries.md)): of
  three objects read whole and three read through a range first, each
  replaced at the origins by one as long, longer, or shorter. A V2
  cache must serve each new version, whole, at the first read after:
  for `no-cache`, within the 5 seconds' grace it allows between
  revalidations (`NoCacheGracePeriod`), and 3 more, of when it last
  read the old one. Then every read, whole and in every range, must be
  the new version alone, with that version's digest, if one comes.
  Pelican keeps `max-age` only if it is above 0, and so takes
  `max-age=0` for no `max-age` at all, and keeps the object a day
  (`GetCacheDirectives` in `local_cache/schema.go`); and it never reads
  `must-revalidate`. So the `max-age=0` and `must-revalidate` rows
  fail, on main and v26.0.0-rc.3 alike.
- `cc-not-stored`: under `no-store`, and again `private`, which a shared
  cache must not store (RFC 9111, sections 5.2.2.5 and 5.2.2.7), a V2
  cache passes an object through. At each, two GETs of an object must
  each get it, with the directive in `Cache-Control`, and no `Age`; and
  every range of `blocks`'s, the range, or the whole object with
  `Accept-Ranges: none`, as RFC 9110 lets a server ignore `Range`, and
  as Pelican's pass-through does (the row's note counts them). Then a
  GET and a HEAD with `only-if-cached` must get 504; the cache's
  introspection (`/api/v1.0/cache/introspect/list`, as its admin) must
  list none of the object; and its store's object files
  (`data/cache/<N>/persistent-cache/objects/`) must not have grown by
  as much as the object. A V2 cache passes a whole read of such an
  object through, as it should, but a ranged read fills it block by
  block as any object, with no regard to the directive (the range comes
  back with the cache's own `max-age`), and the cache then holds and
  serves it whole, so both rows fail, on v26.0.0-rc.3 (C26; main not
  run).
- `s-maxage`: under `max-age=120, s-maxage=20`, an object each V2
  cache has read is replaced at the origins by a longer one. RFC 9111
  (section 5.2.2.10) says that a shared cache, as every Pelican cache
  is, must take `s-maxage` over `max-age`, so each must serve the new
  version within 35 seconds of reading the old one. Pelican takes the
  larger of the two, on purpose (`ParseCacheControl` in
  `local_cache/cache_control.go`), and so keeps the old one for 120
  seconds, and the row fails, on main and v26.0.0-rc.3 alike.

Each new version is written at least 1.1 seconds after the old one,
since a native origin's `ETag` follows a file's modification time, to
the second.
