# Issuer

Part of [What the tests check](README.md).

The [`issuer`](../../framework/suites/issuer.py) suite checks the origins'
embedded [OAuth 2.0](https://www.rfc-editor.org/rfc/rfc6749) issuer: what it
grants through each flow, its refresh tokens, and the boundaries between
users, clients, and namespaces. A user gets what the
`Issuer.AuthorizationTemplates`, their [collections](collections.md), and
shares allow, narrowed to what the client asks for and may carry. The suite
skips under [`-p auth-no-issuer`](../../framework/presets/auth-no-issuer.sh)
and
[`-p auth-external-issuer`](../../framework/presets/auth-external-issuer.sh),
and runs in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)).

The templates
([`framework/config.d/base/40-origin.yaml`](../../framework/config.d/base/40-origin.yaml)),
the run's collection `gamma`, and `test-stranger`'s share of `gamma/shared`
(not under
[`-p origin-multiuser`](../../framework/presets/origin-multiuser.sh)) allow,
under `/data/issuer/`:

| User            | May                                                                                                   |
| --------------- | ----------------------------------------------------------------------------------------------------- |
| `test-analyst`  | read, create, and modify `home/test-analyst`; read the share                                          |
| `test-member`   | read, create, and modify `home/test-member` and `gamma`; read and create the projects; read `archive` |
| `test-stranger` | read `gamma`; and `archive`, once the rogue group may match                                           |

The projects are `projects/<group>` for `test-member`'s groups,
`test-project-a` and `test-project-b`. `archive` is for any `test-project-*`
group; `test-stranger`'s rogue group, `test-project-rogue-<run>`, matches
only while the admin has made it eligible.

The admin creates a [device flow](https://www.rfc-editor.org/rfc/rfc8628)
client and a code flow client (refresh grant, every scope but
`pelican.transfer`), a read-only client, and an exchange client. The public
client (`Issuer.PublicClientID`) has no secret and uses
[PKCE](https://www.rfc-editor.org/rfc/rfc7636). A row per
[scenario](../glossary.md#scenario):

- `discovery`: Each namespace issuer's discovery document names itself, its
  endpoints, and the authorization code, refresh, device code, and token
  exchange grants; its JWKS holds its signing key. The local issuer's names
  the local issuer URL.

- `templates`: Each user's token of every storage and share scope holds
  exactly what the table allows (with `share.access:/<share>` for
  `test-analyst`), and the right issuer, subject, `any` audience, and
  `wlcg.groups` (the groups that earned something). The rogue group grants
  `archive` only while eligible. Under
  [`-p origin-export-templates`](../../framework/presets/origin-export-templates.sh),
  `/protected-b`'s own templates grant `test-stranger` read on `export`
  alone, and `test-member` nothing.

- `requests`: What the device flow grants `test-member` of:

  - an allowed or narrower scope (`archive/sub`): as asked;
  - a broader one (`storage.read:/data/issuer`): the allowed ones under it;
  - a scope that climbs with `..`: the resolved scope if allowed;
  - an action, path, or case not allowed, a partial path component
    (`archivex`), `storage.delete`, `pelican.transfer`, or
    `collection.create`: nothing;
  - `collection.read:/` and the standard scopes: always;
  - `test-stranger`'s `storage.read:/` and `storage.modify:/`: `gamma`'s
    read alone;
  - `test-analyst`'s share, by `share.access:/` or its ID: the share; by
    another ID, nothing.

  A refresh token comes only with `offline_access`.

- `code`: The same through the authorization code flow with PKCE. A scope
  the client may not carry is `invalid_scope`, not dropped. A wrong verifier
  and a replayed code are `invalid_grant`; an unregistered redirect URI is
  400, with no redirect. The public client's flow works, without a refresh
  token; the issuer may refuse it `offline_access` (RFC 6749).

- `clients`: The read-only client gets `openid` and reads alone, and no
  refresh token. The code flow client may not start a device flow
  (`unauthorized_client`). A wrong secret or unknown client is
  `invalid_client`. `client_credentials` is `unauthorized_client` from a
  known client. The public client's device flow gets no refresh token.

- `device`: The verification page shows the scopes and client. Polling is
  `authorization_pending` before approval, and `slow_down` again within five
  seconds. A code redeemed by another client or twice is `invalid_grant`;
  approving it again is 400. A denied flow polls `access_denied`. The page
  needs a login, and refuses an unknown code.

- `data`: Each user reads what the table allows at every origin, and nothing
  else. Where the namespace takes writes, a PUT works in a home and a
  project, and fails in `archive`, the share, and `gamma` for
  `test-stranger`. Through the federation and at each federated cache,
  `test-member` reads `archive/obj` and `test-stranger` does not. Under
  `-p origin-export-templates`, `test-stranger`'s token of `/protected-b`'s
  issuer reads `export/obj` there, and not `archive/obj`, and
  `test-member`'s reads neither: at every origin, at each federated cache,
  and by `pelican object get` through the federation.

- `refresh`: A refresh gives an active access token with the same scopes,
  subject, issuer, and groups and the configured `expires_in`, and a new,
  active refresh token. The used one works within
  `Issuer.RefreshTokenGracePeriod`. A refresh is `invalid_grant` from
  another client or for an unknown token, and `unauthorized_client` from a
  client without the grant.

- `revoked`: Once `test-curator` revokes `test-stranger`'s read on `gamma`,
  a refresh must not restore it ([Pelican](https://pelicanplatform.org/)'s
  [`docs/collections-design.md`](https://github.com/PelicanPlatform/pelican/blob/main/docs/collections-design.md)).
  A revoked refresh token
  ([RFC 7009](https://www.rfc-editor.org/rfc/rfc7009)) is `invalid_grant`,
  and its access token introspects as inactive and is refused by `userinfo`.
  Under
  [`-p origin-short-tokens`](../../framework/presets/origin-short-tokens.sh),
  a refresh token reused past its grace period is `invalid_grant`, and so is
  its successor.

- `exchange` ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)):
  Exchanging a token of `test-member`'s, the exchange client gets
  `archive`'s read alone if it asks for that; no scope for `storage.modify`;
  the token's reads, `offline_access`, and a refresh token for no scope in
  particular; and `invalid_target` for another audience. The device client
  is `unauthorized_client`, and an unknown subject token `invalid_grant`.

- `expiry`: Needs `-p origin-short-tokens` (access tokens last a minute,
  refresh tokens four). An expired token introspects as inactive, `userinfo`
  refuses it, and the origin must refuse it within 90 seconds (timed in the
  note). A refresh then renews it. An expired refresh token is
  `invalid_grant`.

- `client`: `pelican object get` with no token gets one through the device
  flow, approved once. A second get, and one after the token expires
  (`-p origin-short-tokens`), need no approval. Skips for a standalone
  origin.

- `isolation`: Each namespace's issuer is its own trust domain. At the other
  protected namespace's issuer, this one's client is `invalid_client` (401),
  its refresh token `invalid_grant`, and its access token inactive, refused
  by `userinfo` (401), and not exchangeable. Revoking them there leaves them
  active here, and that issuer grants nothing of `gamma`'s path. A namespace
  that merely begins like the suite's (`/protected-ax`) has no discovery
  document (404); without a second protected namespace, only that is
  checked.

- `device-csrf`: The verification page takes a decision only with its CSRF
  token in both cookie and body. Otherwise it is 403
  (`CSRF validation failed`), the flow still polls `authorization_pending`,
  and a proper approval works.

- `admin-clients`: The issuer's client admin API
  (`/api/v1.0/issuer/admin/ns/<namespace>/clients`) refuses no token (401)
  and `test-member` (403). For the admin, it shows no secret, refuses
  `client_credentials` (400), and is 404 for an unknown client. Changes
  apply at once: without the refresh grant, a refresh is
  `unauthorized_client`; narrowed scopes narrow its tokens; once deleted
  (204), a refresh is `invalid_client`.

- `cors-and-pkce`: Another site may read the discovery document, but not the
  token or device authorization endpoints, nor preflight the token endpoint,
  since no `Issuer.RedirectUris` is set. The public client's code flow
  without PKCE is `invalid_request`.

- `code-expiry`: Needs `-p origin-short-tokens`
  (`Issuer.AuthorizationCodeLifetime` of a minute). Redeemed after expiry,
  an approved device code is `expired_token`
  ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628#section-3.5)), and an
  authorization code `invalid_grant`. An expired device code may not be
  approved (400).

- `registration`: A client registering itself (`<issuer>/oidc-cm`,
  [RFC 7591](https://www.rfc-editor.org/rfc/rfc7591)) and asking for
  `authorization_code` gets the device code and refresh grants alone (201).
  A redirect URI off this host is 403 (`invalid_redirect_uri`), and a name
  over 128 bytes 400 (`invalid_client_metadata`). The client is bound to its
  first approving user; another's approval is 403. Its configuration
  (`<issuer>/oidc-cm/<id>`,
  [RFC 7592](https://www.rfc-editor.org/rfc/rfc7592)) needs its registration
  access token (401); with it, a GET shows no secret, a PUT keeps its
  grants, and a DELETE removes it (204). Runs last, since the issuer allows
  an address five registrations, then one a minute; waits once if refused
  (429).
