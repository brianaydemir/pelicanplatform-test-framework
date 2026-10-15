# Issuer

Part of [What the tests check](README.md).

The origins' embedded OAuth2 issuer (`oauth2/issuer/` in Pelican) mints
a user's tokens for a namespace: what it grants is what the user is
allowed, narrowed to what the client asked for and may carry. A user
is allowed what `Issuer.AuthorizationTemplates` say, by name and by
group, and what their collections and shares grant
([collections](collections.md)). A template's prefix is within the
namespace of each issuer that applies it, as its tokens' scopes are,
and an export may have templates of its own, which replace the global
ones there. The
`issuer` suite checks what the issuer grants of each request, through
each of its flows, and how its refresh tokens behave; and the
boundaries that keep one user, client, or namespace from acting as
another. It needs the embedded issuer and a protected namespace, as
`collections` does, and skips with the issuer off or the external one.

`framework/config.d/base/40-origin.yaml` grants, within each namespace
that has an issuer:

| Prefix                       | Actions               | To                                               |
| ---------------------------- | --------------------- | ------------------------------------------------ |
| `/`                          | read, create, modify  | the built-in `admin`                             |
| `/data/issuer/home/$USER`    | read, create, modify  | `test-analyst` and `test-member`                 |
| `/data/issuer/projects/$GROUP` | read, create        | the groups `test-project-a` and `test-project-b` |
| `/data/issuer/archive`       | read                  | any group named `test-project-*`                 |

Under `-p origin-export-templates`, `fed.sh` gives the `/protected-b`
export templates of its own, read on `/data/issuer/export` for
`test-stranger`, so that its issuer grants that and nothing else.

The suite's users are records of the issuer origin (origin-0, or
`fed`), which its admin creates once and later runs reuse:
`test-analyst`, `test-member`, and `test-stranger`, and `collections`'s
`test-curator`, a collection administrator. The admin, and then each
user, first agree to the origin's acceptable use policy, as
`collections`'s do. `test-member` is in both
groups, which the admin creates once, eligible to match templates. A
group an ordinary user creates may not match a template until an
administrator makes it eligible, so each run `test-stranger` makes one
named like the template's, `test-project-rogue-<run>`, and the suite
checks that it grants nothing until the admin flips it. The issuer
learns a user's groups from how they logged in, so the suite logs each
user in: the admin mints them a password invite, they set a password of
the run's with it, and their login's cookie stands for them at the
issuer's verification and authorization pages, carrying the groups
they were in when they logged in (`Issuer.GroupSource: internal`), as a
browser's would. Each run also makes a collection, `gamma`, under
`/<namespace>/data/issuer/collections/<run>/`, which `test-stranger`
may read and `test-member` write, and which `test-stranger` shares
part of (`gamma/shared`) with `test-analyst` (not under
`-p origin-multiuser`, which refuses shares), and an object in each
prefix above, with `decoy/obj` in none. So, within the namespace:

| User            | May                                                                       |
| --------------- | ------------------------------------------------------------------------- |
| `test-analyst`  | read, create, and modify `home/test-analyst`; read the share              |
| `test-member`   | read, create, and modify `home/test-member` and `gamma`; read and create the projects; read `archive` |
| `test-stranger` | read `gamma`; and `archive`, once the rogue group may match               |

The admin creates the suite's clients through the issuer's admin API:
one for the device flow and one for the authorization code flow, each
allowed every scope a self-registered client gets and the refresh
grant; one allowed only `openid` and `storage.read:/` and no refresh
grant; and one for token exchange. The suite also uses the client every
issuer seeds for browsers (`Issuer.PublicClientID`), which has no
secret and proves itself with PKCE. Only `registration` registers a
client itself (RFC 7591). The scenarios:

- `discovery`: each namespace issuer's discovery document names itself
  and endpoints under it, offers the four grants, and its JWKS holds
  the key that signed a token it minted; the local issuer's names the
  server's local issuer URL.
- `templates`: what each user is granted of everything
  (`storage.read:/`, `storage.modify:/`, `storage.create:/`, and
  `share.access:/`): exactly the table above, plus the share's
  `storage.read` and `share.access:/<share>` for `test-analyst`, with
  the issuer, subject, WLCG `any` audience, and `wlcg.groups` (the
  groups that earned something: both for `test-member`, none for the
  others) right; the rogue group grants `archive` once eligible, and
  not once ineligible again; and under `-p origin-export-templates`,
  `/protected-b`'s issuer grants `test-stranger` `export` alone and
  `test-member` nothing.
- `requests`: what the device flow grants of each request, for
  `test-member` unless said otherwise: an allowed scope, as asked; a
  narrower one (`archive/sub`), as asked; a broader one
  (`storage.read:/data/issuer`), as every allowed scope under it,
  collections included; an action, a path, a case, or a prefix that is
  not a path component (`archivex`), nothing; a scope that climbs with
  `..` into a path not allowed, nothing, and into one allowed, the
  scope as it resolves, never with `..`; `storage.delete`,
  `pelican.transfer` (granted only to the transfer API's users), and
  `collection.create` (a collection administrator's), nothing (the
  namespace's issuer grants `collection.create:/` to any user, as the
  local issuer does, so `requests` and `code` fail, on v26.0.0-rc.3 and
  main alike; the collections API then refuses the namespace issuer's
  token, and checks the user's standing itself, so nothing is gained
  by it: I13);
  `collection.read:/` and the standard scopes (`openid`, `wlcg`,
  `profile`, `email`), always; `test-stranger`'s `storage.read:/` as
  `gamma`'s read and `storage.modify:/` as nothing; `test-analyst`'s
  share by `share.access:/` or by its ID, and not by another's; and a
  refresh token only with `offline_access`.
- `code`: the same requests through the authorization code flow, with
  PKCE: the user is sent back (303) with a code and the state, which
  the client trades for tokens. A request for a scope the client may
  not carry (`storage.delete:/`, `pelican.transfer`) is sent back with
  `invalid_scope` instead of granted less; a wrong verifier and a
  replayed code are refused (`invalid_grant`); a redirect URI the
  client did not register gets no redirect (400); and the public
  client's flow works, with no refresh token even for
  `offline_access`.
- `clients`: the read-only client's token has only `openid` and the
  user's reads, and no refresh token; the code flow's client may not
  start a device flow (`unauthorized_client`); a wrong secret and an
  unknown client are refused (`invalid_client`), as a
  `client_credentials` ping tells them apart; and the public client's
  device flow gets no refresh token.
- `device`: the verification page shows the scopes asked and the
  client; polling before approval says `authorization_pending`, and
  polling again within five seconds `slow_down`; an approved code
  redeemed by another client, or redeemed twice, is `invalid_grant`,
  and approved twice, refused; a denied flow polls `access_denied`;
  nobody may see a flow's page; an unknown code is refused.
- `data`: with tokens of everything they may have, each user reads
  what the table allows straight at every origin, and is refused the
  rest (`decoy`, another's home, `archive` for the others); where the
  namespace takes writes, a PUT lands in a home and a project and is
  refused in `archive`, `gamma` for `test-stranger`, and the share.
  Through the federation, `pelican object get` fetches `archive/obj`
  as `test-member` and fails as `test-stranger`; each federated cache
  serves it to `test-member` and refuses `test-stranger`.
- `refresh`: with `offline_access`, the device flow issues a refresh
  token; a refresh gives a new access token, with the same scopes,
  subject, issuer, and groups and the configured `expires_in`, that
  reads `archive/obj` at the origin and introspects as active, and a
  new refresh token, which introspects as active too. The used refresh
  token still works within its grace period
  (`Issuer.RefreshTokenGracePeriod`), so a client can retry a lost
  response. Another client's refresh is `invalid_grant`, a client
  without the refresh grant's `unauthorized_client`, and a token that
  never was `invalid_grant`.
- `revoked`: once `test-curator` revokes `test-stranger`'s read on
  `gamma`, a refresh must not give `gamma`'s read back. Pelican's main
  re-grants a refresh token's scopes as they were, without asking the
  collections again, although `docs/collections-design.md` says a
  refresh should, so this row fails there. A refresh token revoked
  (RFC 7009) is `invalid_grant`, and the access token issued with it
  introspects as inactive and is refused by `userinfo`; the origin,
  which checks a token's signature and expiry alone, still honors it,
  by design. Under `-p origin-short-tokens`, a refresh token reused
  past its grace period is `invalid_grant`, and so is the newer one it
  had been traded for: the reuse ends the chain.
- `exchange` (RFC 8693): the exchange client, allowed `storage.read:/`
  and `offline_access`, trades `test-member`'s token of everything for
  one of `archive`'s read alone, which reads `archive/obj`; for a
  scope it may not carry (`storage.modify`), it gets a token with
  none; for no scope in particular, it gets the token's reads and
  `offline_access`, and a refresh token; and it is refused another
  audience (`invalid_target`), as the device client is refused the
  grant (`unauthorized_client`) and a subject token that never was
  (`invalid_grant`).
- `expiry`, under `-p origin-short-tokens` (access tokens last a
  minute, refresh tokens four, and a used refresh token twenty seconds
  more; it skips otherwise): a token's `expires_in` and `exp - iat`
  say the lifetime; once it has expired, it introspects as inactive,
  `userinfo` refuses it, and the origin refuses it within 90 seconds,
  which the row's note times. The origin is asked every 5 seconds from
  a second past the token's expiry. Pelican's native origin keeps what
  a token authorizes for 5 minutes from its last use
  (`origin_serve/authz.go`), and checks its expiry only when it first
  sees it, allowing 60 seconds of clock skew, so it takes the token at
  the first try, and each try keeps it; it never refuses it, and the
  row fails there, on main and v26.0.0-rc.3 alike. Its refresh token
  then renews it, and the new one reads; and tokens left since the
  suite began can no longer be refreshed once their refresh token has
  expired (`invalid_grant`).
- `client`: `pelican object get` of `archive/obj` as `test-member`,
  with no token, acquires one from the issuer through the device flow,
  which the suite approves once; a second get reuses it, approved by
  nobody; and under `-p origin-short-tokens`, a get after the token
  has expired succeeds, approved by nobody, since the client refreshed
  it. It skips for a standalone origin, which has no director to find
  the issuer through.
- `isolation`: each namespace's issuer is a trust domain of its own
  (`oauth2/issuer/registry.go`, and each issuer's storage, which is
  keyed by namespace). With `test-member`'s tokens and the suite's
  device client from the suite's namespace's issuer, A, and the other
  protected namespace's, B: A's client is unknown at B's device and
  token endpoints (401 `invalid_client`); A's refresh token, presented
  by B's client, is `invalid_grant`; A's access token introspects as
  inactive at B, `userinfo` at B refuses it (401), and B's exchange
  client may not exchange it (`invalid_grant`); and A's access and
  refresh tokens, revoked at B by B's client, still introspect as
  active at A. A collection belongs to its namespace's
  issuer alone: B grants `test-member` nothing of `gamma`'s path,
  which A grants, though B holds an object there, which B's token may
  not read. The issuer of a namespace that merely begins like the
  suite's (`/protected-ax`) has no discovery document (404), not A's.
  Without a second protected namespace, only the last is checked.
- `device-csrf`: the verification page takes a decision only with its
  CSRF token in both its cookie and the body
  (`handleDeviceVerifySubmit`). As `test-member`, an approval with the
  token in the body alone, with a cookie of another value, or in
  neither, and a denial with it in the body alone, are each refused
  (403, `CSRF validation failed`). The flow then still polls
  `authorization_pending`, and an approval with the token in both
  still works.
- `admin-clients`: the issuer's admin API for its clients
  (`/api/v1.0/issuer/admin/ns/<namespace>/clients`) refuses no token
  (401) and `test-member`'s (403), to list them or to delete one. As
  the admin, the list holds a client the scenario creates, and neither
  the list nor a GET of it shows a secret; a client asking for
  `client_credentials`, which the API does not know, is refused (400).
  Once the admin drops the refresh grant from the scenario's client,
  its refresh token is refused (`unauthorized_client`); once it
  narrows its scopes to `openid` and `archive`'s read, a token through
  it carries those alone, however much is asked. Changing a client
  that never was is 404. Once the admin deletes the client (204), its
  refresh is `invalid_client`, and a second delete is 404. The
  scenario changes only the client it creates.
- `cors-and-pkce`: from another site (`Origin: https://evil.invalid`),
  the discovery document may be read by anyone
  (`Access-Control-Allow-Origin: *`, as Pelican sends for public
  metadata, or none), but the token and device authorization
  endpoints, and a preflight of the token endpoint, allow no origin
  that is not among `Issuer.RedirectUris`, which the framework sets
  none of. The public client's authorization code flow without a PKCE
  challenge is refused (`invalid_request`), with no code.
- `code-expiry`, under `-p origin-short-tokens` (codes last a minute,
  `Issuer.AuthorizationCodeLifetime`; it skips otherwise): a device
  flow says `expires_in` 60. A device code approved at once, and an
  authorization code, each redeemed 3 seconds after the minute is
  over, are refused (`expired_token` or `invalid_grant` for the device
  code; `invalid_grant` for the authorization code); and a device code
  never approved may not then be (400). Pelican deletes expired device
  codes every ten minutes, and redeems a deleted one as 404
  `not_found`, so the scenario redeems soon after the expiry, when
  that is unlikely.
- `registration`: a client may register itself at the issuer
  (`<issuer>/oidc-cm`, RFC 7591). One that asks for the
  `authorization_code` grant is registered (201) with the device code
  and refresh grants alone, and a registration access token; one that
  names a redirect URI off this host is refused (403,
  `invalid_redirect_uri`), and one whose name is longer than 128 bytes
  (400, `invalid_client_metadata`). The client is bound to the first
  user who approves a flow for it: `test-member` gets a token through
  it, and then `test-analyst`'s approval is refused (403, `This client
  is registered to a different user`), and the flow still polls
  `authorization_pending`. Its configuration (`<issuer>/oidc-cm/<id>`,
  RFC 7592) refuses a GET, PUT, or DELETE with no registration access
  token or a wrong one (401). With the right one, a GET shows the
  client without its secret; a PUT renames it but leaves its grants as
  they were, though it asks for the code grant; a DELETE removes it
  (204), after which a GET with that token is 401. The issuer allows
  an address five registrations, then one a minute, across all its
  namespaces, and the `client` scenario and the `collections` suite
  register clients too, so `registration` comes last, sends three
  registrations, and if refused (429), waits a minute and tries once
  more. It does not test the limit itself.

The run's collection, share, rogue group, clients, and objects are
removed after the suite, and the registered client, if the scenario
could not delete it; the users and their groups stay.
