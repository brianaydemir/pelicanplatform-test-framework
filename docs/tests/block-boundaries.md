# Block boundaries

Part of [What the tests check](README.md).

The `blocks` suite writes objects of sizes on either side of the block
boundaries of pstore (an origin's encrypted store), the V2 cache, and
the XRootD cache, from 0 bytes to just over 8 MiB, then reads them back
from each origin, through each cache, and through both clients, whole
and in byte ranges that straddle the boundaries.
`framework/testlib/blocks.py` lists the sizes and ranges, and the facts
about Pelican's stores behind them: 4,080-byte blocks, pstore's inline,
buffered, and streamed tiers, and the XRootD cache's 128 KiB blocks.

- Each object goes straight to every origin, with a `Content-Length` or
  in chunks with none, which is how the client uploads; `pelican object
  put` and `stash_plugin` upload every size too. On a `pstore` origin,
  `/metrics` must show each upload in the tier its size predicts. Where
  `/protected-a` takes no writes, the objects are put straight in the
  stores instead, and the upload scenarios skip; where it takes no
  direct clients, the origins are read only through the caches, and
  the clients' direct reads must be refused.
- `put-checksum` runs `pelican object put --require-checksum` of every
  size, naming the first algorithm the origins' storage reports
  (`--checksum-algorithm`; the client asks for `crc32c` otherwise). The
  client verifies each upload against a digest the origin reports, so
  the command must succeed, and leave the objects intact, for the
  objects whose digests the storage reports, and fail for the rest,
  rather than call an unverified upload a success: every object where
  the storage reports none (below), and on an `s3v2` origin those over
  5 MiB, which the client, declaring no length, sends in parts, of
  which the S3 service keeps no MD5 (`framework/testlib/stores.py`).
- How a V2 cache first sees an object decides how it fills (one stream,
  or block by block), so each cache reads one set of objects whole first,
  another in ranges first, and one each through `pelican object get`
  and `stash_plugin` first. Each client reads its set the three ways a
  client reaches an object, as the `transfers` suite does: through the
  cache the director picks; direct from an origin (`--direct`, or
  `?directread`), which must be refused where `/protected-a` takes no
  direct clients; and from each federated cache in turn, named
  (`--cache`, or `Client.PreferredCaches`), which alone may serve it, as
  the client's transfer statistics and the plugin's result ads must say,
  so that each kind of cache the shape has (`-p cache-mixed`) serves a
  client that names it.
- `director-read`: a plain HTTP client that asks a director for a cache
  (`/api/v1.0/director/object/`) and sends the same request, token and
  `Range` and all, to wherever the redirect (307) leads, as `curl -L`
  does. Through each director, every object of the set read whole
  first, whole and in every range, must come back as the object's
  bytes, as from the cache itself; the redirect must lead to a
  federated cache, or, with no cache (`-p topo-no-cache`), to an
  origin. It skips on a standalone origin, which has no director.
- `cache-abandoned` starts a cold read of an object through each cache,
  and gives up after 50,000 bytes; those bytes, and the object's last
  block and the whole object afterward, must be the object's. A V2
  cache may go on fetching the object, or not; the row's note says how
  much of it the cache's introspection then said it held.
- `cache-corrupted` has each V2 cache spoil its own copy of an object it
  holds whole, through its chaos API (`Cache.EnableChaosAPI`, which the
  framework turns on; Pelican v7.27.0 and v26.0.0-rc.0 and later, so the
  scenario skips on an older cache): a block corrupted in its store, and
  bytes cut from the end of another object's chunk file. A read of the
  spoiled part, and of the whole object, must then come back as the
  object's bytes or fail, never as other bytes, and within 30 seconds
  the whole object must come back intact, fetched afresh. An XRootD
  cache has no such API, and is passed over.
- `transfer-status`: a client that asks for a transfer's status
  (`X-Transfer-Status: true` and `TE: trailers`) learns of a failure
  that comes after the headers from a trailer
  ([self-tests](self-tests.md)). Each federated cache, V2 or XRootD (whose
  HTTP server sends it from XRootD 5.6 on), so asked, must announce
  `Trailer: X-Transfer-Status` and end with that trailer, `200: OK`,
  for a whole object, the range `bytes=4080-12239`, the last 1,000
  bytes, and a 512-byte object, which a V2 cache keeps in its database:
  each of an object of its own, read cold and then warm. A GET that
  does not ask must get no trailer; a HEAD that asks has no body, and
  so no trailer, but must get the object's length.
- `transfer-status-failure`: each federated V2 cache holds an object
  whole; its chaos API then corrupts the object's fourth block, past
  what the cache reads before it sends the headers, and the object is
  removed from every origin's store, so that the cache cannot repair
  the block, as it otherwise would (`cache-corrupted`). A GET that asks
  for the status must then get 200 and a trailer that reports a
  failure (Pelican's is `500: ...`), or fail before the body, as where
  the cache's copy has gone stale and no origin has one. Through that
  cache alone, `pelican object get --cache` must then fail, by
  something other than a refusal, and leave nothing; and so must
  stash_plugin, preferring that cache alone
  (`Client.PreferredCaches`), whose ad must say `TransferRetryable =
  true`, since the transfer failed after it began, unless the director
  found no origin holding the object. The row's note says how each
  failed. A cache that serves the object intact never read the
  corrupted block, as where it holds it in memory (`-p cache-memory`),
  and leaves the row inconclusive.
- `origin-transfer-status`: Pelican's `docs/pelican-http-headers.md`
  says that origins, like caches, send the trailer when asked. A GET of
  an object straight to each origin, so asked, must get it, `200: OK`.
  An XRootD origin sends it; nothing in Pelican's native origin
  (`origin_serve/`) does, so the row fails there, on main and
  v26.0.0-rc.3 alike. It skips where the origins take no direct
  clients.
- `digests` asks every server for each object's digest by each
  algorithm in turn (`Want-Digest`: `crc32c`, `crc32`, `adler32`, `md5`,
  `sha`, `sha-256`), by HEAD, and by GET for `crc32c`. Every digest a
  server reports must be the object's, read as the client reads it
  (which knows `crc32c`, `crc32`, `md5`, and `sha`), since the client
  fails a download whose bytes don't match. Each origin must
  report `crc32c` and `md5`, but for what its storage cannot: XRootD's
  S3 and HTTPS plugins and the `httpsv2` and `ssh` backends compute none,
  an XRootD origin under `-p origin-multiuser` no `crc32c`,
  and `s3v2` reports only the MD5 that the S3 service holds, which it
  does not for an object over 5 MiB uploaded with no `Content-Length`,
  in parts (`framework/testlib/stores.py`). A federated V2 cache relays
  the origins' digests (`digest-relay`), so it must report the same of
  the objects that `cache-whole-first` reads whole first; of those that
  `cache-range-first` reads through a range first it need report none,
  since it records none of them (`digest-range-first`), and neither
  need an XRootD or site-local cache. Over XRootD origins, a V2 cache
  knows only their `md5` of an object it has not read (`digest-relay`),
  so `digests` run without `cache-whole-first` fails there. The row's
  note names the servers that report none. `digest-overwrite` then
  overwrites an object at each origin, by a PUT at once and by a write in
  the store a second later: a native origin keeps digests by the file's
  modification time,
  to the second, and must not report a stale one; and its `ETag`, by
  which a cache tells that an object has changed, must change too.
- `digest-defaults` asks each origin for the digests of an object of
  its own in the other ways a client may. A HEAD with no `Want-Digest`
  must get `crc32c` from a native origin, the first of
  `Origin.DefaultChecksumTypes`, which the framework leaves at its
  default (`handleHeadWithChecksum` in `origin_serve/handlers.go`); an
  XRootD origin need send none. A HEAD with `Want-Digest: crc32c, md5`
  must get both from a native origin, in one `Digest` field, since the
  client reads one; an XRootD origin answers with the one it prefers,
  and must send at least one. A HEAD with `Want-Digest: md5;q=0.5,
  crc32c;q=1`, as RFC 3230 allows, must get at least one from every
  origin. Each is asked only of what the origins' storage reports (see
  `digests`), and every digest must be the object's. The row's note
  says what each origin sent. Pelican's native origins take each
  algorithm's name with its `;q=` and match none
  (`xattrChecksumAdapter.GetDigests` in `origin_serve/backend.go`, and
  likewise for `pstore` and `s3v2`), so they report nothing, and the
  row fails there, on main and v26.0.0-rc.3 alike. On a `posixv2`
  origin, the digests must then be in the file's `user.XrdCks.crc32c`
  and `user.XrdCks.md5` extended attributes, each the object's; a PUT
  of another version must remove them or replace them with its own,
  and the origin then report that version's. Last, the test writes
  bytes of the same length over the file in the store, and gives it
  back its modification time. The origin keeps digests by that time,
  to the second, as XRootD does, so it may report the old version's
  digests, or the new bytes' if it noticed, but no others; the note
  says which. A store rewritten behind the origin's back, its time
  kept, is beyond what the origin can see, so the old digests are
  accepted (`stale-origin-digest` relies on them). Where the store's
  extended attributes cannot be read, the row is inconclusive. It skips
  where the origins take no direct clients, or their storage reports no
  digest.
- `digest-relay`: a V2 cache relays the origins' digests, as an RFC
  3230 `Digest`, so that a client can check what it downloads through
  it (`local_cache/persistent_cache_api.go`). Of an object of its own
  at each federated V2 cache: a cold HEAD, which the cache answers from
  an origin's answer, and once it has read the object whole, a HEAD and
  a GET, from what it recorded, must each report the digests that the
  origins' storage holds (`crc32c` and `md5`, but for those it cannot;
  see `digests`), each the object's, whether `Want-Digest` asks for
  `crc32c` or `md5`. A cold GET may come before the cache knows them,
  and the row's note says whether it carried any. Then `pelican object
  get --cache` must bring the object, and its transfer statistics
  (`--transfer-stats`: the `serverChecksums` that it asked the cache
  for once the body came) must hold the same digests, each the
  object's; the client has no `--require-checksum` for a download. A V2
  cache asks an origin for every digest in one `Want-Digest`, which an
  XRootD origin answers with one, `md5`, so over XRootD origins
  (`-p origin-xrootd`) the cold HEAD lacks `crc32c`, and the row fails
  there, on main and v26.0.0-rc.3 alike. It skips where the origins'
  storage reports no digest.
- `digest-range-first`: the same, of an object that each federated V2
  cache first reads through a range, of its second block: a HEAD must
  report the origins' digests once it has, and again once it has read
  the object whole (it sends `Age`). A V2 cache fills a range of an
  object it lacks block by block, after a stat of the origin that asks
  for no digest (`initObjectFromStat` in
  `local_cache/persistent_cache.go`), and records none, so it relays
  none of that object, and the row fails, on main and v26.0.0-rc.3
  alike.
- `stale-origin-digest`: a V2 cache checks what it fetches against the
  digests that an origin reports, and must neither pass on as good, nor
  keep, an object whose bytes fail them. A `posixv2` origin keeps an
  object's digests in its file's extended attributes (`user.XrdCks.*`,
  as XRootD does), with the file's modification time, to the second,
  and reports them while that time is unchanged
  (`origin_serve/checksum.go`). So, for each federated V2 cache, every
  origin first reports every digest of an object of its own (a HEAD
  with `Want-Digest: md5,crc32c,crc32,sha`, as a V2 cache asks); the
  test then writes another version of the same size over it in each
  store, and gives the file back its modification time, after which
  the origins serve the new bytes with the old digests. A cold GET
  through the cache, asking for the status, must then end with a
  trailer that reports a failure, or fail; within 10 seconds a GET with
  `only-if-cached` must get 504, since the cache kept nothing; and
  `pelican object get --cache` must fail, by something other than a
  refusal, and leave nothing. Where a store's files show the tests no
  such attributes (a filesystem without them, or a host that does not
  pass them through), or an origin notices the overwrite, the row is
  inconclusive. It runs only over `posixv2` origins that take direct
  clients, which it asks first.
- Ranges past an object's end: a suffix longer than the object is all
  of it, and a range that runs past the end stops at its last byte; one
  that starts at or past the end (`past-end`) must get 416, or, from a
  server that ignores `Range`, the whole object. In a set of ranges, one
  past the end is passed over, as RFC 9110 says.
- `huge-ranges`: an object past 4 GiB, which no suite could move whole:
  4 GiB and 128 KiB, rounded up to a block boundary, and a byte more
  (4,295,101,681 bytes), so that 2^32 falls inside a block (no power of
  two is a multiple of 4,080) and the last block is one byte, and a size
  or an offset that a server keeps in 32 bits, signed or not, goes wrong
  somewhere in it. The suite writes it as a sparse file in each origin's
  store on disk: zeros, but for 64 KiB markers of bytes of their own at
  its start, around 2 GiB and 4 GiB, and at its end, with holes between,
  which cost nothing to keep, and which a cache fetches no more of than
  a range reads. At each origin
  that takes direct clients, through each director's object route,
  which leads to a cache, and at each cache, a HEAD must give its
  length, and nine ranges, each within a marker, must come back as 206,
  with a `Content-Range` of the bytes asked for out of its size, and
  exactly those bytes: 200 bytes across 2 GiB, and across 4 GiB; the
  byte before each, and the byte at each; the last byte; the last 1,000
  bytes, as a suffix; and from 1,000 bytes before the end on. A server
  that ignored `Range` would send the whole object, so the suite reads
  64 KiB of an answer and hangs up. A PROPFIND of Depth 0 at each origin
  and cache must say its size, as must `pelican object stat --json`,
  and `pelican object ls -l --json` of its collection, where
  `/protected-a` has `Listings`. Afterward, it is evicted from each V2
  cache, whose accounting may have charged its store the whole object.
  It skips on a pstore origin, whose store is encrypted, so that the
  object would have to go over HTTPS whole; and where a store keeps no
  sparse files, so that the object would take 4 GiB on disk.
- `conditional` sends each server conditional GETs of two objects (RFC
  9110): with `If-None-Match` naming the `ETag` the server sent, among
  others, weakened (`W/`), or as `*`, and with `If-Modified-Since` at
  the `Last-Modified` it sent or later, the server may answer 304, or,
  ignoring the condition, 200 with the whole object; with
  `If-None-Match` naming another ETag, or `If-Modified-Since` long ago,
  it must answer 200 with the whole object. A 304 to a condition that
  doesn't hold would leave a client with stale bytes. The row's note
  says what each server honored, and whether it sent no `ETag` or
  `Last-Modified`, whose conditions are then left out.
- `conditional-put` sends each origin that takes writes from direct
  clients conditional PUTs: with `If-None-Match: *`, a new object is
  created, and a second PUT refused (412), changing nothing; with
  `If-Match` naming the object's `ETag`, it is replaced, and with the
  ETag it replaced, or another, the PUT is refused, changing nothing;
  and with `If-Match: *`, a PUT of an object that doesn't exist is
  refused, storing nothing. A server that ignores a condition on a PUT
  would let two writers overwrite each other unawares; Pelican's native
  origins check it, and XRootD's HTTP server does not, so the row fails
  on an XRootD origin.
- `etag-agreement`: an object's `ETag` names its version, whichever
  method reports it (RFC 9110, section 8.8.3); a cache revalidates by
  it, and a writer compares and swaps by it. Of an object of its own,
  the answer to each origin's PUT that wrote it, a GET, a HEAD, and
  PROPFIND's `getetag` must all report one ETag. Then a GET with
  `If-Match` naming the GET's ETag must get the object; a GET of a
  range with `If-Range` naming it, 206 and the range; and a PUT of
  another version with `If-Match` naming it must replace the object.
  The same again with the ETag in that PUT's answer. A weak ETag, which
  neither condition can match, is not tried. A `posixv2` origin makes
  the ETag of a GET and of a PUT's answer itself, from the file's
  device, inode, size, and modification time (`computeETag` in
  `origin_serve/handlers.go`), while its HEAD and PROPFIND, and its
  checks of `If-Match` and `If-Range`, use the backend's, from the
  modification time and size (`etagFileInfo` in
  `origin_serve/backend_etag.go`). So its GET's ETag is not its HEAD's,
  and the conditions that name it fail: `If-Match` gets 412, and
  `If-Range` the whole object. The row fails there, on main and
  v26.0.0-rc.3 alike. Where `/protected-a` takes no writes, the object
  is put in each store, and only GETs are sent. It skips where the
  origins take no direct clients.
- `conditional-edges` sends each origin conditions at their edges, of an
  object of its own (RFC 9110, section 13). A GET of an object that does
  not exist, with `If-None-Match` naming an ETag or `*`, must get 404,
  not 304. A GET with an invalid `If-Modified-Since` must get the
  object, since a server ignores an invalid date; and one with
  `If-None-Match` that does not hold, beside an `If-Modified-Since` that
  would, must get it too, since `If-None-Match` decides. A GET with
  `If-Unmodified-Since` after the object's `Last-Modified` must get the
  object, and one with a time before it, 412. Where `/protected-a` takes
  writes, a PUT with `If-None-Match: *, "junk"` over the object must be
  refused (412), changing nothing, since a `*` in the list still names
  any object. Then 8 PUTs at once of a new object, each with
  `If-None-Match: *` and bytes of its own: exactly one may succeed, the
  rest must get 412, and the store must then hold the one's bytes. A
  native origin checks a PUT's condition before it writes
  (`checkPutPreconditions` in `origin_serve/preconditions.go`), and only
  a `pstore` origin checks it again as it commits the object, so
  elsewhere two racing PUTs may both find no object, and both succeed;
  whether the row fails there depends on timing. XRootD's HTTP server
  ignores conditions on a PUT (see `conditional-put`), so the PUT rows
  fail on an XRootD origin. It skips where the origins take no direct
  clients.
- `mixed-version` overwrites an object of three XRootD blocks at the
  origins after a cache has cached its first 4,080-byte block; each
  later response, to a range in the third XRootD block (which neither
  kind of cache fetched for the first read) and to the whole object,
  must then succeed, and be entirely one version, the same for the
  range as for the whole object.
- `cache-overlap` reads ranges from objects of each cache's own, each
  plan in turn, so that every read after the first covers both blocks
  the cache fetched for an earlier one and blocks it must fetch now, at
  both block sizes; then it reads one more object in overlapping ranges
  all at once. The whole object must come back intact after each.
- `coalescing`: a cache is there to spare the origins. Each federated
  cache, read cold and whole by 16 clients at once, of a 4 MiB object of
  its own, must fetch it from the origins about once: the native
  origins' counters of GETs (`pelican_http_bytes_total` and
  `pelican_http_requests_total`, `metrics/http_metrics.go`) must show
  less than two objects' worth of bytes sent, and, for a V2 cache,
  which fetches an object whole in one GET, no more than two GETs under
  the username of the tests' token. An XRootD cache fetches an object a
  block at a time, so only its bytes are judged. Then 16 warm reads
  must have the origins serve no GET under that username, and send no
  more than the directors' health tests do (64 KiB). Other traffic
  moves the counters too, so no count is exact; where the origins
  counted fewer bytes than the object, the row is inconclusive. It
  skips where XRootD serves the origins, which keep no such counters. A
  site-local cache reads through another cache, so this and each
  scenario below that names federated caches leave it to `sitelocal`.
- `cache-assemble` reads unaligned, overlapping ranges that together
  cover an object, out of order. The cache must hold part of it after
  the first and all of it after the last. A V2 cache's introspection
  (`/api/v1.0/cache/introspect/metadata`, as its admin) summarizes which
  blocks it holds: after the first range, which starts in the object's
  middle, it must hold that range's blocks, and not block 0, since it
  fetches a range's blocks rather than fill the object from its start
  (`initObjectFromStat` in `local_cache/persistent_cache.go`); within 10
  seconds of the last, it must hold every block. A V2 cache whose
  introspection gives no summary is judged by `Age`, which it sends only
  once it holds every block. An XRootD cache's `.cinfo` (read from
  `data/cache/<N>/`) records which blocks it holds. When a cache cannot
  show the partial state (it sends `Age` and gives no summary, has no
  readable `.cinfo`, or its `.cinfo` shows none or all of the blocks
  after the first range), the row is inconclusive; but an XRootD cache
  whose `.cinfo` could be read after the first range fails if, after the
  last, it has none, or one that doesn't show every block.
- `overwrite-cached` and `overwrite-range-first` overwrite objects at the
  origins once each cache holds them (read whole, or a range first): one
  keeping its size, one growing, and one shrinking. Every response, whole
  or ranged, must then be entirely one version, with that version's
  size, and a whole response's digest (`Want-Digest`), if one comes, that
  version's: a stale one would fail the client's check of bytes that are
  right. Without
  `Cache-Control`, a cache decides for itself how long its copy stays
  fresh. Under `-p origin-max-age`, every native origin must send
  `Cache-Control: max-age=30`, and every cache must then serve the new
  version, and only it, once its copy is stale. The scenarios wait up to
  about 75 seconds for it. An XRootD origin sends none
  (`Origin.CacheControl` is the native origin's), so there the caches
  decide for themselves, as without.
- `write-through`: a V2 cache relays a PUT or DELETE to an origin through
  the director, and drops its own copy. Of an object each holds, a PUT
  with a read-only token must be refused, with nothing changed; one with
  the `server` token must reach an origin, and once every origin holds
  what it wrote, the cache must serve that at once. Likewise a DELETE,
  after which the cache must answer 404. Where `/protected-a` takes no
  writes, the PUT must be refused. An XRootD cache takes no writes, so
  its PUT must be refused, and change no origin.
- `cache-evicted`, under `-p cache-small`, which bounds each V2 cache's
  store (`Cache.HighWaterMark` 128 MiB, `Cache.LowWaterMark` 96 MiB;
  Pelican's defaults are a share of the whole disk, which no suite could
  fill): each cache reads, cold and whole, enough 8 MiB objects to exceed
  its high water mark by half, and reads the first again once it has read
  as many as fit under the mark, less one. Within 60 seconds its store
  (`data/cache/<N>/persistent-cache/objects/`) must have come back under
  the mark, and not fallen below the low one by more than an object.
  Then, asked with `only-if-cached`, it must still hold the three
  objects read last, and none of at least two of the oldest. The first
  object, read again, may be held or gone: a V2 cache records an access
  for its LRU order at most once in ten minutes (`UpdateLRU` in
  `local_cache/database.go`), so reading it again may move it nowhere;
  the row's note says which. Last, every object must still come back
  whole and in a range, as must the first by `pelican object get
  --cache`. An XRootD cache measures its filesystem, which a bind mount
  cannot bound, so `fed.sh` refuses the preset with `cache-xrootd`.
