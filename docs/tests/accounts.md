# Accounts

Part of [What the tests check](README.md).

The [`accounts`](../../framework/suites/accounts.py) suite checks how the
issuer origin (origin-0, or `fed`) treats an account through its life
([Pelican](https://pelicanplatform.org/)'s
[`docs/user-group-design.md`](https://github.com/PelicanPlatform/pelican/blob/main/docs/user-group-design.md)):
deactivated, deleted, granted scopes and stripped of them, added to
authority groups and removed, renamed, and recreated under a freed name. It
skips under [`-p auth-no-issuer`](../../framework/presets/auth-no-issuer.sh)
and
[`-p auth-external-issuer`](../../framework/presets/auth-external-issuer.sh),
and runs in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)). Each
[case](../glossary.md#case)'s row is named `<scenario> <case>`.

Its users and groups are the run's own (`acct-<role>-<run>`).
[`framework/config.d/base/40-origin.yaml`](../../framework/config.d/base/40-origin.yaml)
names its authorities: the groups `test-server-admins`
(`Server.AdminGroups`) and `test-collection-admins`
(`Server.CollectionAdminGroups`), whose names the server reserves as it
starts, so a federation started before they were configured needs a restart;
and the user `test-user-admin` (`Server.UserAdminUsers`). A group confers
authority from the user's next login (`Issuer.GroupSource: internal`).

- `user-status`: A user is made inactive (`PUT /api/v1.0/users/:id/status`),
  active again, then deleted. While inactive or deleted:

  - their cookie is refused at `/api/v1.0/me` (401) and cleared, and a new
    login and their bearer token are refused (401; `inactive-web`,
    `deleted-web`);
  - the issuer approves no device flow, sends no authorization code, and
    refuses their refresh token (`invalid_grant`; `inactive-issuer`,
    `deleted-issuer`).

  While active, before and after, each works (`active`, `reactivated`).

- `scope-grants`: The admin grants users scopes
  (`POST /api/v1.0/users/:id/scopes`):

  - `user-admin`: `server.user_admin` lets a user create users, but not act
    on the admin, nor grant any scope (403).
  - `log-read`: `pelican.log_read` lets a user read `/api/v1.0/logs/tail`,
    but not `/api/v1.0/config` (403).
  - `collection-admin`: `server.collection_admin` lets a user create a
    [collection](collections.md).
  - `collection-admin-token`: Only the admin may make an API token
    (`POST /api/v1.0/tokens`; 403 for a collection administrator). The
    admin's token creates a collection, and is refused once deleted.
  - `ungrantable`: A data scope (`storage.read:/`) is 400.

  Before each grant, and once `pelican.log_read` or
  `server.collection_admin` is revoked (`DELETE .../scopes/:scope`), the
  user is refused (403).

- `authority-groups`:

  - `reserved`: Neither group may be created by a user or deleted by the
    admin (409).
  - `collection-admins`: A member may create a collection, and may not once
    removed.
  - `server-admins`: A member is `admin` in `/whoami`, and `user` before and
    once removed.
  - `stale-cookie`: A cookie from while the user was a member must say
    `user` once they are removed, as a deactivation takes effect at once.
  - `user-admin`: `test-user-admin` may create users, may not grant itself
    `server.admin` (403), and has `server.user_admin` and the role `user` in
    `/whoami`.

- `id-keyed-grants`: Grants are kept by ID, not name
  (`docs/user-group-design.md`).

  - `group-deleted`: A deleted group's read and admin role on a collection
    go with it, from the ACL and a former member's next token. A new group
    of its name gets none of them.
  - `user-deleted`: A new user of a deleted owner's name may not list, see,
    delete (404), or read the collection, and the ACL drops the name.
  - `renamed`: A renamed group and user (`PATCH /api/v1.0/groups/:id`,
    `PATCH /api/v1.0/users/:id`) keep their reads, listed under the new
    names. New ones of the old names get nothing.

- `group-invites`: A group's owner makes invite links
  (`POST /api/v1.0/groups/:id/invites`).

  - `single-use`: A link joins the first user who redeems it, and no other.
    A member who is not the owner may make none (403).
  - `multi-use`: A link joins each user who redeems it.

- `aup`: The admin puts up a new acceptable use policy
  (`PUT /api/v1.0/aup`). Skips under `Server.AUPFile: none`.

  - `gates`: Until the admin agrees to the new version, the server refuses
    them its user routes (403, `requires_aup` and the version), agreeing to
    the old one does not count, and `/whoami` says so. The policy is public
    (`GET /api/v1.0/aup`).
  - `again`: Another new policy gates the admin and other users again.
  - `restored`: The original policy, put back, needs no new agreement, since
    its version is the hash of its text (`Server.AUPFile` in Pelican's
    [`docs/parameters.yaml`](https://github.com/PelicanPlatform/pelican/blob/main/docs/parameters.yaml)).

  Runs last, since it briefly gates every user of the origin.
