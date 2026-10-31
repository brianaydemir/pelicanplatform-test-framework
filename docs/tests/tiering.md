# Tiering

Part of [What the tests check](README.md).

Under `-p cache-tiered` ([presets](../../README.md#presets)), a V2 cache
that holds all of an object of at least 16 MiB (`Cache.TieringThreshold`)
uploads it to its prefix of the `s3` service's bucket `tier`
(`framework/var/data/tier/<cache>/`). It then serves the object from there,
by redirecting the client to a pre-signed URL, or, with
`Cache.TieringDisableRedirect` (cache-1 under `-p topo-multi-cache`), by
proxying it.

The `tiering` suite checks every cache set to tier, the site-local one
included. Each scenario records one row, which judges every such cache. Only
`tampered` touches the bucket. The suite skips without `-p cache-tiered`, or
where the shape does not export `/protected-a`.

- `tiered`: An object over the threshold, read cold and whole, must be in
  the cache's prefix within 60 seconds, and
  `pelican_cache_tiering_uploads_total` (`result="success"`) must have
  risen.

- `threshold`: An object of exactly the threshold, read cold, must be tiered
  within 60 seconds; one a byte smaller must not be.

- `redirect`: A GET of a tiered object at the cache:

  - a redirecting cache must answer 307 to a pre-signed URL
    (`X-Amz-Signature`) in the bucket, without the token, and with a
    `Digest` of the object; the URL must serve the object with no token;
  - a proxying cache must answer 200 with the object.

  Where the cache is the only one the directors know of, each director's
  object route, with no token, must redirect to the cache. Under
  `-p origin-max-age`, a redirecting cache may answer 200 while it
  revalidates; it is asked once more, here and in `ranges`.

- `ranges`: `bytes=0-0`, `5242879-5242881`, `-10`, `16777215-16777216`, and
  `16777216-` of the same object. A proxying cache must answer each as at
  [block boundaries](block-boundaries.md). A redirecting cache must answer
  307, and the bucket the range, with no token; a 200 with the range's bytes
  alone (rclone) is inconclusive.

- `client-get`: `pelican object get --cache <cache>` of the same object must
  write it, as must the same through the director, where the cache is the
  only one it knows of. So must stash_plugin, preferring the cache
  (`Client.PreferredCaches`).

- `tampered`: A tiered object's copy in the bucket is overwritten with
  random bytes of its size.

  - A proxying cache must never serve them. The first read may fail, but
    within 60 seconds the cache must serve the object, fetched afresh.
  - Through a redirecting cache, whose `Digest` belies the bytes,
    `pelican object get --cache <cache>` must write the object, or fail and
    leave no file.

- `overwritten`: A tiered object is overwritten at every origin. Every whole
  read must be all one version. Under `-p origin-max-age`, the reads must
  reach the new version within 75 seconds (`max-age=30`, and 45 more) of the
  old one's tiering; otherwise, the note says which version one read got.

Nothing checks that a cache drops a tiered object, or serves one after a
restart or an eviction ([what is not tested](what-is-not-tested.md)).
