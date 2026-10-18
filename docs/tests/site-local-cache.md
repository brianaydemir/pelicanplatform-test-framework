# Site-local cache

Part of [What the tests check](README.md).

Under `-p topo-site-local-cache` ([presets](../../README.md#presets)),
cache-2 is a site-local cache (`Cache.EnableSiteLocalMode`): it neither
registers nor advertises, so no director knows of it, and it serves
only clients that name it (`--cache`, or `Client.PreferredCaches`). It
fetches objects through a federated cache, as a client would,
presenting the client's token, and asks an origin for listings (in a V2
cache, `useEmbeddedCacheMode` in `local_cache/persistent_cache.go`, and
`proxyPropfind` in `local_cache/persistent_cache_api.go`, in Pelican).
The `sitelocal` suite checks it with objects of its own under
`/<namespace>/data/sitelocal/<run>/`, put in every origin's store, in
the storage of the namespace they are read in (on a `pstore` origin,
`/public` reads `/protected-a`'s), each read once, so that each is cold
wherever it is read; each is 10,000 bytes, but the two that `list`
lists. The suite skips without a site-local cache, or where the shape
does not export `/protected-a`; a scenario in `/public` skips where the
shape does not export it. Each scenario records one row, named for it;
`get-<ns>` and `plugin-get-<ns>` read in `/public` and `/protected-a`.
The clients and requests present tokens of the session's profile; the
objects are put in place, and the federated caches asked what they
hold, with WLCG tokens either way. `get-public` and `plugin-get-public`
present no token, and so run in the WLCG pass alone.

- `get-public` and `get-protected-a`: `pelican object get --cache
  <cache-2>` of a new object, to a file, with the `server` token
  (`--token`) in `/protected-a`, must exit 0 and write the object; and
  its transfer statistics (`--transfer-stats`) must name cache-2, and
  no other server, as the one that served it.
- `plugin-get-public` and `plugin-get-protected-a`: stash_plugin's
  download of a new object, with cache-2 its preferred cache
  (`Client.PreferredCaches`) and, in `/protected-a`, the `server` token
  as `server.use` in its credentials directory (`_CONDOR_CREDS`), must
  report success (`TransferSuccess = true`) and write the object; and
  its result ad's last `Endpoint<N>` must name cache-2.
- `get-plus`: `get-protected-a`, but with `--cache <cache-2>,+`, by
  which the client may fall back to the director's caches, though it
  need not: cache-2 must still serve the object, alone.
- `refused`: `pelican object get --cache <cache-2>` of an object in
  `/protected-a`, with a token to read `/protected-a` that names its
  issuer but is signed by `test-keys/unknown.pem`, which no server
  knows. Cache-2 must pass the refusal on: the client must fail, by a
  refusal and nothing else, and write no file.
- `upstream`: a GET, with the `server` token, of a new object in
  `/protected-a` through cache-2, which must get 200 and the object;
  before it and after it, a HEAD with `Cache-Control: only-if-cached`
  at each federated V2 cache ([cache freshness](cache-freshness.md)).
  Before, every one must answer 504, holding none of the object; after,
  one at least must answer 200, holding it, which the note names. An
  XRootD cache cannot be asked what it holds without fetching it, so
  where no federated cache is V2 (`-p cache-xrootd`), the row is
  inconclusive.
- `list`: a Depth 1 PROPFIND, with the `server` token, through
  cache-2, of a collection in `/protected-a` that holds `a` (1,000
  bytes) and `b` (2,000). Where `/protected-a` may be listed straight
  from an origin (`Listings` and `DirectReads`), it must get 207,
  listing `a` and `b` alone, at their sizes; otherwise, as under
  `-p origin-no-direct`, a refusal (401, 403, or 405).

The suites that check a cache's own APIs, the freshness of its copies,
or its block boundaries ask only the federated caches
([what is not tested](what-is-not-tested.md)).
