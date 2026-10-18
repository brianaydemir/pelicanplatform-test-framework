# Tiering

Part of [What the tests check](README.md).

Under `-p cache-tiered` ([presets](../../README.md#presets)), once a V2
cache holds all of an object of at least 16 MiB
(`Cache.TieringThreshold`; `eligible` in `local_cache/tier_uploader.go`
in Pelican), it uploads it to a prefix of its own in the `s3` service's
bucket `tier`, `framework/var/data/tier/<cache>/`, and from then on
serves it from there: by redirecting the client to a pre-signed URL in
the bucket (`tryTierRedirect` in `local_cache/tier_serve.go`), or, for
cache-1 (`-p topo-multi-cache`), which has
`Cache.TieringDisableRedirect`, by proxying it itself. The `tiering`
suite checks every cache that tiers, the site-local one included
(`-p topo-site-local-cache`), with objects of its own under
`/protected-a/data/tiering/<run>/`, put at every origin, each named for
the cache that reads it, so that each is cold there. A cache names a
tiered object in the bucket by a hash, so the suite finds it there by
its size and SHA-256. Only `tampered` touches the bucket, as corruption
or a substitution there would; nothing touches the caches' stores, and
only the origins' copies are removed afterward. The suite skips without
`-p cache-tiered`, or where the shape does not export `/protected-a`.
Pelican's main has tiering, but no release yet, and an older cache
ignores the setting ([server images](../../README.md#server-images)): a
cache that neither reports tiering metrics (`pelican_cache_tiering_*`
in its `/metrics`) nor has claimed its prefix in the bucket
(`.pelican-cache-id`) is taken not to tier, and where none tiers, every
scenario skips. Each scenario records one row, named for it, which
judges every cache that tiers, and fails with the first problem it
finds. The reads present the `server` token for `/protected-a`, of the
session's profile, and follow a redirect without it; the objects are
put at the origins with WLCG tokens either way. Every scenario presents
a token, so each runs in both passes.

- `tiered`: at each cache, a new object of the threshold and 4,321
  bytes more, read whole and cold through the cache, must come back,
  and then be in the cache's prefix of the bucket within 60 seconds;
  and the cache's `pelican_cache_tiering_uploads_total` with
  `result="success"` must have risen meanwhile.
- `threshold`: an object a byte under the threshold (16,777,215 bytes),
  read whole and cold, must come back; then one of exactly the
  threshold (16,777,216 bytes), read the same way, must be in the
  bucket within 60 seconds, and by then the first must not be.
- `redirect`: a GET, at the cache, of another object tiered as in
  `tiered`, which `ranges` and `client-get` read too. A redirecting
  cache must answer 307, to a pre-signed URL (`X-Amz-Signature`) under
  `/tier/` at `s3:8444`, which must not hold the token, with a
  `Digest`, by which a client checks what the bucket sends: each
  algorithm in it that the tests know must be the object's, and there
  must be one. A GET of that URL, with no token, must get the object. A
  proxying cache must answer 200 with the object. Where the cache is
  the only one the directors know of, each director's object route
  (`/api/v1.0/director/object/`), asked with no token, must redirect
  (307) to the cache, which must then answer as before; elsewhere, the
  note says the directors were not asked. Under an origin's
  `Cache-Control` (`-p origin-max-age`), a cache serves a stale tiered
  object itself while it revalidates it, rather than redirect, so a 200
  there is asked again, once, here and in `ranges`.
- `ranges`: of the same object, through the cache, with the token,
  `bytes=0-0`, `5242879-5242881`, `-10`, `16777215-16777216`, and
  `16777216-`, one at a time. A proxying cache must answer each as at
  [block boundaries](block-boundaries.md): 206, with exactly the
  range's bytes, and a `Content-Range` of that range and the object's
  size. A redirecting cache must answer each 307, and the bucket, asked
  for the range with no token, must answer likewise. rclone, which
  serves the bucket, answers a range with 200 and the range's bytes
  alone, which counts, but leaves the row inconclusive.
- `client-get`: of the same object, `pelican object get --cache
  <cache>` with the token (`--token`), which follows wherever the cache
  sends it, must exit 0 and write the object; and, where the cache is
  the only one the directors know of, so must the same without
  `--cache`, through the director; elsewhere, the note says it was not
  run. Then stash_plugin, with the cache its preferred cache
  (`Client.PreferredCaches`) and the token as `server.use` in its
  credentials directory, must report success, and write the object.
  Nothing checks which server served either client.
- `tampered`: a tiered object of its own, whose file in the bucket is
  then overwritten with random bytes of its size. Through a proxying
  cache, the first whole read after may fail, but must get neither
  those bytes nor any others but the object's; and within 60 seconds,
  read every 2, the cache must serve the object again, fetched afresh,
  and never those bytes meanwhile, as it drops a tiered object that
  changed in the bucket
  (`openTierStream` in `local_cache/tier_serve.go`). Through a
  redirecting cache, the bytes reach the client from the bucket, and
  the redirect's `Digest` belies them: `pelican object get --cache
  <cache>` must not keep them, and must either write the object, or
  fail and leave no file.
- `overwritten`: a tiered object of its own, overwritten at every
  origin with another of its size. A whole read through the cache must
  then be all the old version or all the new. With the origins sending
  `Cache-Control` (`-p origin-max-age`, `max-age=30`, which only native
  origins send), reads 3 seconds apart, each all one version, must
  reach the new one within the `max-age`, and 45 seconds more, of the
  old one's being found in the bucket. Without it, the cache decides
  for itself how long its copy stays fresh, so one read is made, and
  the note says which version it got.

Nothing checks that a cache drops its copy of a tiered object, or
serves one after a restart or an eviction
([what is not tested](what-is-not-tested.md)).
