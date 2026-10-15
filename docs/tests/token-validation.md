# Token validation

Part of [What the tests check](README.md).

The credentials are each wrong in one way that `pelican token create`
can make. The `auth` suite also presents tokens that it cannot,
which `framework/testlib/jwt.py` signs: each like `server`'s, of the
pass's [token profile](README.md#token-profiles), with the same header
and the origins' key, but for one thing. Each check asks
for the test object in `/protected-a` at every origin and cache, where
the origins' XRootD servers (`-p origin-xrootd`) and XRootD caches
(`-p cache-xrootd`) are controls; where the origins take no direct
clients, every request straight to one must be refused anyway.

- `time-claims`: native servers allow 60 seconds of clock skew on each
  of a token's times, as the WLCG token profile recommends, and no more
  (`ClockSkewLeeway` in `token/token_verify.go`). A token whose `nbf` or
  `iat` is 300 seconds ahead must be refused; one whose `nbf` is 20
  seconds ahead, taken; and one that expired 120 seconds ago, refused.
  Each request has a token of its own, since a V2 cache remembers a
  token it refused for 30 seconds.
- `expired-in-use`: a token that lasts 30 seconds, presented at once and
  then every 30 seconds for three minutes, must be taken before it
  expires, and refused from 70 seconds past its expiry on; in between,
  either. The row's note lists what each server answered, and when.
  Pelican's native origin and V2 cache keep what a token authorizes for
  5 minutes from its last use, and check its expiry only when they first
  see it (`origin_serve/authz.go` and `local_cache/cache_authz.go`), so a
  token in use never expires there, and the rows fail, on main and
  v26.0.0-rc.3 alike. The check runs beside the suite's others, in a
  thread of its own, and its rows come last. Unlike the `expired`
  credential, whose wait sidesteps that memory, it tests it.
- `malformed-claims`: a token without `exp`; without `kid`; with a `kid`
  that no key has; with `alg` `none` and no signature; signed by HS256,
  keyed with the origins' public key as a JWK, as in the classic
  key-confusion attack; or without `aud`, must be refused. The WLCG
  token profile requires `exp` and `aud`, as SciTokens 2.0 does.
  Pelican's native servers check a token with jwx, which checks `exp`
  only if a token has one; a native origin checks `aud` only if a token
  has one (`origin_serve/authz.go`), and a V2 cache never. So the
  `no-exp` rows fail at native origins and V2 caches, and the `no-aud`
  rows too, on main and v26.0.0-rc.3 alike. A token whose version claim
  names a version that its profile does not define, `wlcg.ver` `2.0`
  (row `wlcg.ver-2`), or in the SciTokens pass `ver` `scitokens:3.0`
  (row `ver-3`), may be taken or refused, and the row's note says which.
- `web-token-strict`: a server checks the tokens of its own web API with
  no clock skew, since it alone mints and checks them
  (`VerifyWithKeysetStrict`, from `extractUserFromBearerToken` in
  `web_ui/authentication.go`). At each origin's and cache's web API,
  `/api/v1.0/auth/whoami` with a token that it takes as its admin's
  (`credentials.mint_admin()`) must say who that is; with the same token
  not valid for another 20 seconds, or expired 5 seconds ago, it must
  refuse it (401) or say that no one is logged in. A server mints these
  in the WLCG profile alone, so the check runs in the WLCG pass alone.
