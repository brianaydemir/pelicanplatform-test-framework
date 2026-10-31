# Another owner

Part of [What the tests check](README.md).

Under `-p topo-multi-owner`, origin-2 has a key and an issuer of its
own. The `owners` suite tests its exports `/other`, like `/protected-a`,
and `/public/other`, like `/public` and nested in it. Its
`/public/secret` is for the `auth` suite's `nested-protected` check
([authorization](authorization.md)). `ready`, `keys`, and `routing` run
in the WLCG pass alone ([token profiles](README.md#token-profiles)).

- `ready`: origin-2's objects must come back through the federation and
  from each cache within 20 tries, 15 seconds apart, or the suite stops.
- `keys`: origin-2's JWKS, server-wide and of each export's issuer, must
  hold its keys, and none of the origins' or the test keys; the origins'
  must hold none of its.
- `routing`: Each director must send a direct read of origin-2's
  prefixes to it alone, and of the origins' to them alone; for an export
  that takes no direct clients, it must find no origin (405) and name
  none. Through each cache, `/public/other/...` must be origin-2's
  object, not origin-0's decoy, and `/public/others-<run>/...` must be
  origin-0's.
- One scenario per credential (`framework/testlib/owners.py`): GET,
  HEAD, PUT, and DELETE straight to the origins, and GET and HEAD to
  each cache, on both sides of each pair (`/other` and `/protected-a`;
  `/public/other` and `/public`). The credentials are `owner`
  (origin-2's key and issuer); `origins` (the origins'); `owner-key` and
  `origins-key` (one's key, the other's issuer); `jwks` and `ns-jwks`
  (`test-keys/jwks.pem` and `ns-protected-a.pem`, naming origin-2's
  issuer); and `none`. Where a token decides, only `owner` on origin-2's
  side and `origins` on the origins' should be allowed. The `/public`
  pair gets only `owner`, `origins`, and `none`; under
  `-p origin-public-writes`, all but `ns-jwks`.
- `get`, `direct-get`, and `put`: The client must get origin-2's objects
  from a cache and from origin-2, and put with its token to each of its
  exports that takes writes.
- `cross-get` and `cross-put`: Each owner's token must be refused in the
  other's namespace, storing nothing.
