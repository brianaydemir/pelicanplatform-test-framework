# Another owner

Part of [What the tests check](README.md).

Under [`-p topo-multi-owner`](../../framework/presets/topo-multi-owner.sh),
origin-2 has a key and an issuer of its own. The
[`owners`](../../framework/suites/owners.py) suite tests its exports
`/other`, like `/protected-a`, and `/public/other`, like `/public` and
nested in it. Its `/public/secret` is for the [`auth`](authorization.md)
suite's `nested-protected` check. `ready`, `keys`, and `routing` run in the
[WLCG pass](../glossary.md#pass) alone
([token profiles](README.md#token-profiles)).

- `ready`: origin-2's objects must come back through the federation and from
  each cache within 20 tries, 15 seconds apart, or the suite stops.
- `keys`: origin-2's JWKS, server-wide and of each export's issuer, must
  hold its keys, and none of the origins' or the test keys; the origins'
  must hold none of its.
- `routing`: Each director must send a direct read of origin-2's prefixes to
  it alone, and of the origins' to them alone; for an export that takes no
  direct clients, it must find no origin (405) and name none. Through each
  cache, `/public/other/...` must be origin-2's object, not origin-0's
  decoy, and `/public/others-<run>/...` must be origin-0's.
- One scenario per [credential](../glossary.md#credential)
  ([`framework/testlib/owners.py`](../../framework/testlib/owners.py)): GET,
  HEAD, PUT, and DELETE straight to the origins, and GET and HEAD to each
  cache, on both sides of each pair (`/other` and `/protected-a`;
  `/public/other` and `/public`). The credentials are `owner` (origin-2's
  key and issuer); `origins` (the origins'); `owner-key` and `origins-key`
  (one's key, the other's issuer); `jwks` and `ns-jwks`
  (`test-keys/jwks.pem` and `ns-protected-a.pem`, naming origin-2's issuer);
  and `none`. Where a token decides, only `owner` on origin-2's side and
  `origins` on the origins' should be allowed. The `/public` pair gets only
  `owner`, `origins`, and `none`; under
  [`-p origin-public-writes`](../../framework/presets/origin-public-writes.sh),
  all but `ns-jwks`.
- `get`, `direct-get`, `cache-get`, and `put`: The
  [Pelican client](../glossary.md#pelican-client) must get origin-2's
  objects through the director, from origin-2, and from each federated
  cache it names, and put with its token to each of its exports that takes
  writes.
- `plugin-get`: [`stash_plugin`](../glossary.md#stash_plugin) must get an
  object on each side of each pair in one run, with both owners' tokens in
  its credentials directory and no `scitokens.use`, so that it picks, for
  each object, the token whose issuer the director names. It runs twice,
  with the tokens' files in either order.
- `cross-get` and `cross-put`: Each owner's token must be refused in the
  other's namespace, storing nothing.
