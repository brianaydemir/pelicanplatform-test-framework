# Site-local cache

Part of [What the tests check](README.md).

Under `-p topo-site-local-cache` ([presets](../../README.md#presets)),
cache-2 is a site-local cache (`Cache.EnableSiteLocalMode`), unknown to the
directors. It serves only clients that name it (`--cache`, or
`Client.PreferredCaches`), fetching through a federated cache with the
client's token.

The `sitelocal` suite skips without a site-local cache, or where
`/protected-a` is not exported; a scenario in `/public` skips where
`/public` is not. Clients present tokens of the pass's profile
([token profiles](README.md#token-profiles)); `get-public` and
`plugin-get-public` present none, and run in the WLCG pass only. Each
scenario records one row, named for it.

- `get-public`, `get-protected-a`: `pelican object get --cache <cache-2>` of
  a cold object must exit 0 and write it. Its `--transfer-stats` must name
  cache-2 alone.
- `plugin-get-public`, `plugin-get-protected-a`: stash_plugin, preferring
  cache-2, must report `TransferSuccess = true` and write the object. Its
  ad's last `Endpoint<N>` must name cache-2.
- `get-plus`: As `get-protected-a`, with `--cache <cache-2>,+`: cache-2 must
  still serve the object alone.
- `refused`: As `get-protected-a`, with a token signed by a key no server
  knows: a refusal, and nothing written.
- `upstream`: A GET through cache-2 of a cold object in `/protected-a` must
  get it, and leave a federated V2 cache holding it, where none did before
  ([cache freshness](cache-freshness.md)). Inconclusive under
  `-p cache-xrootd`.
- `list`: A Depth 1 PROPFIND through cache-2 of a collection holding `a`
  (1,000 bytes) and `b` (2,000) must get 207, listing just those at their
  sizes, where `/protected-a` has `Listings` and `DirectReads`; otherwise,
  as under `-p origin-no-direct`, 401, 403, or 405.

The other cache suites ask only federated caches
([what is not tested](what-is-not-tested.md)).
