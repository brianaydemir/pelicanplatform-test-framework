# OIDC login

Part of [What the tests check](README.md).

Under `-p auth-oidc-local`, every server lets users log in through an OpenID
Connect provider: the framework's `idp` service (`https://idp:9007`,
`OIDC.Issuer`), standing in for CILogon. Its users are those of
`framework/etc/idp-users.json`, which `local/etc/idp-users.json` may
override.

The `oidc` suite skips without the provider, and runs in the WLCG pass only
([token profiles](README.md#token-profiles)). A case's row is named
`<scenario> <case>`; `enabled` and `login` have a row per server.

- `enabled`: Each server's `/api/v1.0/auth/oauth` lists its modules. Its
  login route (`/api/v1.0/auth/oauth/login`) answers 307 to the provider's
  authorization endpoint, asking for a code for the servers' client, with a
  state, and a callback on `OIDC.ClientRedirectHostname`
  (`https://localhost:<port>/api/v1.0/auth/oauth/callback`); it sets a
  session cookie.
- `login`: `idp-member` logs in at each server, and is then the user of
  their `preferred_username`, with the role `user`. A second login finds the
  same user, whose record links to the provider's subject and issuer. The
  provider's log (`framework/var/data/idp/requests.log`) shows the server
  redeeming the code as the servers' client and asking userinfo.
- `callback`: At the issuer origin, a callback is refused (400) without its
  session cookie (`no-session`) or with a forged state (`forged-state`).
  With the cookie it logs the user in; replayed, it fails and logs no one in
  (`used-code`).
- `collision`: `idp-collider`, whose `preferred_username` is `admin`, logs
  in at the issuer origin as the user of the next claim of
  `Server.AutoEnrollUsernameClaims`, the email, and gets neither the admin
  role nor `GET /api/v1.0/users` (401 or 403).
- `registry-identity`: `pelican namespace register --with-identity`,
  approved at the provider as `idp-member`, must record the client's key,
  `idp-member`'s subject and `preferred_username`, and, as owner, the
  registry user that `login` made. `pelican namespace delete` must remove
  it. Skips for a standalone origin.

The last three scenarios restart origin-0, in the tests' configuration layer
([service control](../../README.md#service-control)), with
`Server.UIAdminUsers` naming `idp-owner` and `Issuer.GroupSource: oidc`, and
restore it afterward. They need a control helper, and skip under
`-p topo-tiny`. `groups` and `client` also need the embedded issuer and a
protected namespace that takes direct writes; their objects go in the
groups' [issuer](issuer.md) project prefixes at every origin.

- `admin`: `idp-owner` is origin-0's admin by `whoami`, and may list its
  users; so is `idp-admin`, by its group `test-server-admins`
  (`Server.AdminGroups`); `idp-member` is neither. Once restored,
  `idp-owner` is no admin. Rows are `restarted <user>` and
  `restored <user>`.
- `groups`: Through the device flow, the issuer grants `idp-member` exactly
  what the templates give `test-project-a`, with the group in `wlcg.groups`;
  `idp-admin` likewise for `test-project-b`; and `idp-outsider`, in no
  group, no data scope. A row per user. In the `data` row, `idp-member`
  reads `test-project-a`'s object at origin-0 and through the federation,
  and is refused `test-project-b`'s.
- `client`: `pelican object get` of `test-project-a`'s object with no token,
  approved as `idp-member` at the provider, gets the object.
