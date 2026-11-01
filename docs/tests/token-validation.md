# Token validation

Part of [What the tests check](README.md).

These [`auth`](../../framework/suites/auth.py) suite checks present tokens
that [`pelican token create`](../glossary.md#pelican-client) cannot make,
each like the `server` [credential](authorization.md)'s but for one thing.
Each asks for `/protected-a`'s test object at every origin and cache;
[XRootD](../glossary.md#native-and-xrootd) servers are controls. An origin
that takes no direct clients must refuse each such request.

- `time-claims`: A server should allow 60 seconds of clock skew, as the
  [WLCG token profile](https://github.com/WLCG-AuthZ-WG/common-jwt-profile/blob/master/profile.md)
  recommends, and no more. A token whose `nbf` or `iat` is 300 seconds ahead
  (`nbf+300`, `iat+300`), or that expired 120 seconds ago (`exp-120`), must
  be refused; one whose `nbf` is 20 seconds ahead, taken (`nbf+20`).
- `expired-in-use`: A 30-second token, presented at once and every 30
  seconds for three minutes, must be taken until it expires, and refused
  from 70 seconds past on. The note lists each server's answers. Its rows
  come last. Skips an origin that takes no direct clients.
- `malformed-claims`: A token must be refused that lacks `exp` (`no-exp`),
  `kid` (`no-kid`), or `aud` (`no-aud`); that names an unknown `kid`
  (`unknown-kid`); that has `alg` `none` (`alg-none`); or that is signed by
  HS256 with the origins' public key (`hs256`). One with an undefined
  version claim, `wlcg.ver` `2.0` (`wlcg.ver-2`) or `ver` `scitokens:3.0`
  (`ver-3`), may be taken or refused.
- `web-token-strict`: A server's web API should check its own tokens with no
  clock skew. Given an admin token, each server's `/api/v1.0/auth/whoami`
  must name the admin (`web-fresh`); given one not valid for another 20
  seconds (`web-nbf+20`), or expired 5 seconds ago (`web-exp-5`), it must
  refuse it (401) or name no one. [WLCG only](README.md#token-profiles).
