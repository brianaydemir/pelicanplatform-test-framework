# Block boundaries

Part of [What the tests check](README.md).

The `blocks` suite writes objects of sizes on either side of the block
boundaries of [pstore](pstore.md), the V2 cache, and the XRootD cache
([native and XRootD](../glossary.md#native-and-xrootd)), from 0 bytes to
just over 8 MiB. It reads them back from each origin, through each cache and
director, and with both clients, whole and in byte ranges that straddle
those boundaries and the edges of XRootD's 8 MiB reads.
`framework/testlib/blocks.py` lists the sizes and ranges. A suffix longer
than an object must bring all of it, and a range past the end must stop at
its last byte; in a set of ranges, one past the end is passed over
(RFC 9110).

How a V2 cache first sees an object decides how it fills, so the objects
come in four sets, each first read a different way: whole through each
cache, in ranges through each cache, by `pelican object get`, and by
stash_plugin. Other scenarios use objects of their own, cold where they are
read. Where `/protected-a` takes no writes, the objects are put straight in
the stores, and the upload scenarios skip. Where it takes no direct clients,
direct reads must be refused, and the scenarios that ask origins directly
skip.

The suite skips where the shape does not export `/protected-a`, and its
cache scenarios where the shape has no cache. Scenarios that name federated
caches leave out a [site-local cache](site-local-cache.md). The origins must
report `crc32c` and `md5` (`Want-Digest`) where their storage can
(`framework/testlib/stores.py` says which); the digest scenarios skip where
it reports none.

- `put-declared`, `put-chunked`: Every size must upload straight to every
  origin, with a `Content-Length`, or in chunks with none. A pstore origin's
  `/metrics` must count each upload in the tier its size predicts.
- `put-client`, `put-plugin`: Every size, uploaded by `pelican object put`
  or stash_plugin, must reach an origin intact, and, at a pstore origin,
  land in its tier.
- `put-checksum`: `pelican object put --require-checksum` of every size must
  succeed, with the objects intact, where the storage reports a digest, and
  fail elsewhere.
- `origin-read`: Each origin must return every object whole and in every
  range.
- `client-get`, `plugin-get`: Each client reads its set by every
  [route](../glossary.md#route): through the director's choice of cache,
  direct from an origin, and from each federated cache, named (`--cache`, or
  `Client.PreferredCaches`). Only the named cache may serve, as the client's
  transfer statistics and the plugin's result ads must say.
- `cache-whole-first`: Each cache reads its set cold and whole, then warm,
  then in every range.
- `director-read`: A [plain HTTP client](../glossary.md#plain-http-client)
  asks each director for every object of the whole-first set, whole and in
  every range, and follows the 307 to a federated cache (an origin under
  `-p topo-no-cache`). Each must come back. Skips for a standalone origin.
- `cache-range-first`: Each cache reads its set cold in a range, then in
  every range, then whole.
- `cache-stored`: Each XRootD cache's store on disk must hold every block
  that an object's `.cinfo` records, at its place in the file. Inconclusive
  where no `.cinfo` remains. Skips without an XRootD cache.
- `cache-overlap`: Each cache reads ranges that each cover blocks already
  fetched and blocks not yet fetched, at both block sizes, then overlapping
  ranges at once. The whole object must come back intact after each.
- `coalescing`: 16 clients at once read a 4 MiB object cold through each
  federated cache, which must fetch it from the origins about once: less
  than two objects' worth of GET bytes at the native origins
  (`pelican_http_bytes_total`), and, for a V2 cache, at most two GETs under
  the tests' username, besides a one-byte GET per origin. Then 16 warm reads
  must cause no such GET, and the origins send at most 64 KiB. Skips where
  XRootD serves the origins.
- `cache-assemble`: Each cache reads unaligned, overlapping ranges that
  cover an object, out of order. After the first range, it must hold its
  blocks and none before them but block 0; within 10 seconds of the last,
  every block. A V2 cache is judged by its introspection
  (`/api/v1.0/cache/introspect/metadata`), or else by `Age`; an XRootD
  cache, by its `.cinfo`. Inconclusive where a cache cannot show the partial
  state.
- `cache-abandoned`: Each cache starts a cold read whose client gives up
  after 50,000 bytes. Those bytes, the object's last block, and the whole
  object must then be the object's.
- `cache-corrupted`: Each V2 cache's chaos API (`Cache.EnableChaosAPI`)
  corrupts a block of an object it holds, and truncates another's chunk
  file. A read must bring the object's bytes or fail, never other bytes, and
  within 30 seconds bring the whole object intact. Skips without a V2 cache
  and its chaos API.
- `transfer-status`: Each federated cache, asked for the transfer's status
  (`X-Transfer-Status: true`, `TE: trailers`; `docs/pelican-http-headers.md`
  in Pelican's repository), must announce `Trailer: X-Transfer-Status` and
  end with the trailer `200: OK`, cold and warm, of a whole object, a range,
  a suffix, and a 512-byte object, which a V2 cache should hold inline
  (`is_inline`; inconclusive otherwise). A GET that does not ask must get no
  trailer; a HEAD that asks, 200.
- `transfer-status-failure`: Each federated V2 cache's chaos API corrupts
  the fourth block of an object it holds, which is then removed from every
  origin. A GET that asks for the status must get a trailer reporting a
  failure, or fail before the body. Through that cache alone,
  `pelican object get --cache` and stash_plugin must fail, but not by a
  refusal, and leave nothing. The plugin's ad must say
  `TransferRetryable = true`, unless the director found no origin holding
  the object. Inconclusive where the cache serves the object intact (as
  under `-p cache-memory`). Skips without a federated V2 cache and its chaos
  API.
- `origin-transfer-status`: Each origin, so asked, must send the trailer
  too, `200: OK`.
- `digests`: Every server is asked each object's digest, by each of
  `crc32c`, `crc32`, `adler32`, `md5`, `sha`, and `sha-256`. Every digest
  reported must be the object's; a server that cannot give one must answer
  without it. Each origin must report what its storage holds, as must a
  federated V2 cache of the whole-first set; XRootD and site-local caches
  need not.
- `digest-overwrite`: Each origin's digests and `ETag` must follow
  overwrites, by PUT at once and in the store a second later (not at
  pstore), even where it keeps digests by mtime, to the second.
- `digest-defaults`: Each origin's digests, asked the other ways a client
  may, must be the object's:
  - with no `Want-Digest`, a native origin must send `crc32c` (the first of
    `Origin.DefaultChecksumTypes`);
  - with `Want-Digest: crc32c, md5`, a native origin must send both, in one
    `Digest`; an XRootD origin, at least one;
  - with `Want-Digest: md5;q=0.5, crc32c;q=1` (RFC 3230), every origin must
    send at least one;
  - at a `posixv2` origin, the file's `user.XrdCks.crc32c` and
    `user.XrdCks.md5` must hold the digests, and a PUT must replace or
    remove them; once the file is overwritten with its mtime restored, the
    origin may report the old digests or the new, no others.
- `digest-relay`: Each federated V2 cache must relay the origins' digests
  (RFC 3230 `Digest`) to a cold HEAD, to a HEAD and a GET once it holds the
  object, and to `pelican object get --transfer-stats`. Skips without a
  federated V2 cache.
- `digest-range-first`: The same, of an object each such cache first reads
  in a range, by HEAD, before and after it holds it whole.
- `stale-origin-digest`: A V2 cache must neither pass on as good, nor keep,
  an object that fails the origin's digests. The test overwrites an object
  in each `posixv2` store, restoring its mtime, so that the origins send the
  old digests. Through each federated V2 cache, a cold GET must fail or end
  with a failure trailer; within 10 seconds, a GET with `only-if-cached`
  must get 504; and `pelican object get --cache` must fail, but not by a
  refusal, and leave nothing. Inconclusive where the origins notice. Needs a
  federated V2 cache, and `posixv2` origins that take direct clients.
- `past-end`: A range at or past an object's end must get 416, or the whole
  object from a server that ignores `Range`.
- `empty`: An empty object must come back as 200 with no body; a range of it
  as that or 416.
- `huge-ranges`: An object of 4,295,101,681 bytes, past the reach of any
  32-bit size or offset, written as a sparse file in each origin's store. At
  each origin, director, and cache, a HEAD must give its length; nine ranges
  across 2 GiB, 4 GiB, and the end must get 206, the right `Content-Range`,
  and exactly their bytes; and Depth 0 PROPFIND, `pelican object stat`, and
  `pelican object ls -l` (with `Listings`) must give its size. It is evicted
  from each V2 cache afterward. Skips at pstore origins, and where a store
  keeps no sparse files.
- `conditional`: Each server may answer a conditional GET (RFC 9110) 304
  only where the condition holds (`If-None-Match` naming its `ETag`,
  weakened, or `*`; `If-Modified-Since` at or after `Last-Modified`).
  Otherwise, it must send the whole object.
- `conditional-put`: Each origin that takes direct writes must refuse a PUT
  whose condition fails with 412, changing nothing: a second
  `If-None-Match: *`; `If-Match` naming a stale or other `ETag`; and
  `If-Match: *` of a missing object. One whose condition holds must succeed.
- `etag-agreement`: Each origin must report one `ETag` for a version
  (RFC 9110, section 8.8.3), to the PUT that wrote it, GET, HEAD, and
  PROPFIND (`getetag`). That ETag must satisfy `If-Match` on a GET and a
  PUT, and `If-Range` on a ranged GET (206). Without writes, only GETs are
  sent.
- `conditional-edges`: Each origin, at the edges of RFC 9110, section 13:
  - `If-None-Match` of a missing object must get 404, not 304;
  - an invalid `If-Modified-Since` is ignored;
  - a failing `If-None-Match` beats a holding `If-Modified-Since`;
  - `If-Unmodified-Since` before `Last-Modified` must get 412, and after it
    the object;
  - a PUT with `If-None-Match: *, "junk"` over an object must get 412;
  - of 8 PUTs at once of a new object with `If-None-Match: *`, exactly one
    may succeed, and the store hold its bytes; the rest must get 412 or 423
    (RFC 4918).
- `mixed-version`: An object is overwritten at the origins once each cache
  holds its first block. Later reads, of a range in its third XRootD block
  and of the whole, must succeed, and be entirely one version, the same for
  both.
- `overwrite-cached`, `overwrite-range-first`: Once each cache holds objects
  (read whole, or a range first), the origins overwrite them, keeping,
  growing, and shrinking their sizes. Every response must be entirely one
  version, with that version's size and digest, if any. Under
  `-p origin-max-age`, native origins must send `Cache-Control: max-age=30`,
  and every cache serve the new version within about 75 seconds.
- `write-through`: A V2 cache must relay a PUT or DELETE to the origins and
  drop its copy. A PUT with a read-only token must be refused; once one with
  the `server` token reaches the origins, the cache must serve the new bytes
  at once. After a DELETE, where the storage can delete, it must answer 404.
  Where `/protected-a` takes no writes, and at an XRootD cache, a PUT must
  be refused, changing nothing.
- `cache-evicted`: Needs `-p cache-small`
  ([presets](../../README.md#presets)). Each cache reads 8 MiB objects to
  half again its high mark. Within 60 seconds (V2) or 180 (XRootD), its
  store must fall under the high mark, and not below the low one by more
  than an object. A V2 cache must still hold the three objects read last,
  and have dropped at least two of the oldest; an XRootD cache need only
  have dropped two. Every object must still come back.
