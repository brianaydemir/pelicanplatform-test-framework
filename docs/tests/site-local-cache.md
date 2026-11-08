# Site-local cache

Part of [What the tests check](README.md).

Under
[`-p topo-site-local-cache`](../../framework/presets/topo-site-local-cache.sh),
cache-2 is a site-local cache (`Cache.EnableSiteLocalMode`), unknown to the
directors. It serves only clients that name it (`--cache`, or
`Client.PreferredCaches`), fetching through a federated cache with the
client's token.

The [`sitelocal`](../../framework/suites/sitelocal.py) suite
[skips](../glossary.md#skip) without a site-local cache, or where
`/protected-a` is not exported; a scenario in `/public` skips where
`/public` is not. [Clients](../glossary.md#client) present tokens of the
pass's profile ([token profiles](README.md#token-profiles)); `get-public`
and `plugin-get-public` present none, and run in the WLCG
[pass](../glossary.md#pass) only. Each [scenario](../glossary.md#scenario)
records one [row](../glossary.md#case), named for it.

- `get-public`, `get-protected-a`:
  [`pelican object get --cache <cache-2>`](../glossary.md#pelican-client) of
  a cold object must exit 0 and write it. Its `--transfer-stats` must name
  cache-2 alone.
- `plugin-get-public`, `plugin-get-protected-a`:
  [`stash_plugin`](../glossary.md#stash_plugin), preferring cache-2, must
  report `TransferSuccess = true` and write the object. Its ad's last
  `Endpoint<N>` must name cache-2.
- `get-plus`, `plugin-get-plus`: As `get-protected-a` and
  `plugin-get-protected-a`, preferring `<cache-2>,+`: cache-2 must still
  serve the object alone.
- `refused`, `plugin-refused`: As `get-protected-a` and
  `plugin-get-protected-a`, with a token signed by a key no server knows: a
  refusal, and nothing written.
- `http-refused`: A GET of cache-2 with that token must get 401 or 403, and
  none of the object's bytes.
- `upstream`: A GET through cache-2 of a cold object in `/protected-a` must
  get it, and leave a federated [V2 cache](../glossary.md#native-and-xrootd)
  holding it, where none did before ([cache freshness](cache-freshness.md)).
  [Inconclusive](../ci.md#results) under
  [`-p cache-xrootd`](../../framework/presets/cache-xrootd.sh).
- `list`: A Depth 1 PROPFIND through cache-2 of a collection holding `a`
  (1,000 bytes) and `b` (2,000) must get 207, listing just those at their
  sizes, where `/protected-a` has `Listings` and `DirectReads`; otherwise,
  as under
  [`-p origin-no-direct`](../../framework/presets/origin-no-direct.sh), 401,
  403, or 405.

The other cache suites ask only federated caches
([what is not tested](what-is-not-tested.md)).
