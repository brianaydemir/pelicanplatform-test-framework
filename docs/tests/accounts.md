# Accounts

Part of [What the tests check](README.md).

A server keeps its users and groups in its database (Pelican's
`docs/user-group-design.md`), and decides from them who may act as
whom: whether an account is active, which scopes it holds, directly or
through a group that the configuration names, and which collections
are granted to its ID or its groups' IDs. The `accounts` suite checks
how those decisions follow an account through its life: deactivated,
deleted, granted scopes and stripped of them, added to authority groups
and removed, renamed, and recreated under a freed name. A mistake in
any of them is a privilege escalation, or a grant that outlives its
owner. The suite acts at the issuer origin (origin-0, or `fed`), needs
the embedded issuer and a protected namespace, as `collections` does,
and skips with the issuer off or the external one.

Every user and group the suite makes is the run's own, named
`acct-<role>-<run>`, so that deleting and recreating them touches no
other suite's. Each user agrees to the origin's acceptable use policy
as the admin creates them, and acts through a bearer token that the
origin's web API takes as theirs (`credentials.mint_user()`), or, where
a check is of a login, through the cookie of one, with a password of
the run's set through an invite, as the `issuer` suite's users do. Such
a token names its user, not their ID, so it stands for whoever bears
that name when it is presented. Tokens for data come from the
namespace's issuer through its device flow, approved as the user, with
a client the admin creates: the suite asks for every data scope, and
looks for a collection's `storage.read`. Collections are the admin's,
under `/<namespace>/data/accounts/<run>/`.

`framework/config.d/base/40-origin.yaml` configures the suite's
authorities, in every shape: `Server.AdminGroups` names
`test-server-admins`, `Server.CollectionAdminGroups` names
`test-collection-admins`, and `Server.UserAdminUsers` names
`test-user-admin`, whom the admin creates once and later runs reuse.
Each server reserves the two groups' names as it starts
(`EnsureConfiguredAuthorityGroups` in `database/authority_groups.go`),
so a federation started before they were configured must be restarted.
The configured groups match only the groups that a login's cookie
carries, those the user was in as they logged in
(`Issuer.GroupSource: internal`), so a check of a member's authority
logs them in again after each change of membership. The scenarios:

- `user-status`: a user logs in, and holds a refresh token from the
  device flow, approved with their cookie. The admin makes them
  inactive (`PUT /api/v1.0/users/:id/status`), then active again, then
  deletes them. While inactive, and once deleted, `GET /api/v1.0/me`
  with their cookie must be refused (401) and clear the cookie, and
  logging in again and their bearer token at `/me` must be refused
  (401) (`inactive-web` and `deleted-web`); and the issuer must approve
  no device flow with their cookie, send no code from its authorization
  page, and refuse their refresh token with `invalid_grant`
  (`inactive-issuer` and `deleted-issuer`). While active, before and
  after (`active` and `reactivated`), each works. The issuer's rows
  fail on v26.0.0-rc.3 and on Pelican's main: the issuer's middleware
  (in `launchers/origin_serve.go`) takes the cookie's identity without
  the check of the account that `AuthHandler` makes
  (`userRecordIsActive` in `web_ui/authentication.go`), and a refresh
  grants again without looking the user up (`handleToken` in
  `oauth2/issuer/handlers.go`). `deleted-web` fails there too: a bearer
  token whose subject names no live user passes `AuthHandler` with no
  user ID, and `/me` answers 500 (`callerID` in `web_ui/me.go`).
- `scope-grants`: the admin grants run users scopes
  (`POST /api/v1.0/users/:id/scopes`), which the server looks up on
  each request. With `server.user_admin`, a user may create users, but
  may not make the admin active or relabel them (403: a user
  administrator may not act on a server administrator), nor grant a
  scope to itself or the admin (403: granting is the admin's alone);
  each of these would change nothing if it got through. With
  `pelican.log_read`, a user may read the log tail
  (`/api/v1.0/logs/tail`), but not the configuration
  (`/api/v1.0/config`, 403). With `server.collection_admin`, a user may
  create a collection, but may not make an API token of that scope from
  their login cookie: `POST /api/v1.0/tokens` is the admin's alone
  (`AdminAuthHandler` in `web_ui/ui.go`, on v26.0.0-rc.3 and main
  alike, though `api_token/api_token.go` would limit any creator's
  token to the scopes the creator holds), so the user is refused (403),
  and the admin, logged in, makes one instead, creates a collection with
  it, and is refused once it is deleted (`collection-admin-token`).
  Before each grant, and on the first request once `pelican.log_read`
  or `server.collection_admin` is revoked
  (`DELETE /api/v1.0/users/:id/scopes/:scope`), the user is refused
  (403). A data scope, `storage.read:/`, may not be granted (400).
- `authority-groups`: an ordinary user may not create a group of
  either configured name, nor may the admin delete one (409). A user
  may not create a collection until the admin adds them to
  `test-collection-admins` and they log in again, and may not once
  removed and logged in again. A user is `admin` in `/whoami` once in
  `test-server-admins` and logged in again, and `user` before and once
  removed. A login cookie from while the user was in
  `test-server-admins` must say `user` once they are removed
  (`stale-cookie`): a cookie keeps the groups it was minted with for
  its 16 hours, and `AuthHandler` asks the database about the user's
  status on each request, but not their groups, so a removed
  administrator keeps the role until their cookie expires, and the row
  fails, on v26.0.0-rc.3 and main alike. Whether a session should lose
  a group before it expires is not settled; the suite holds that an
  authority a group confers should go with the membership, as a
  deactivation takes effect at once. `test-user-admin` may create users
  and may not grant itself `server.admin` (403); `/whoami` gives them
  `server.user_admin` and the role `user`.
- `id-keyed-grants`: grants are kept by the IDs of users and groups,
  not their names (Pelican's issues #3752 and #3753). Once a group that
  may read a collection, and is its admin group, is deleted, the ACL
  lists nothing, the collection has no admin group, and a former
  member's next token lacks the read; a new group of the freed name,
  with the former member in it, gets none of it. Once a user who owns a
  collection, and may read it, is deleted, and a new user takes the
  name, the new user does not list the collection, may neither see it
  (404) nor delete it (404), and gets no read of it, and the ACL no
  longer lists the name. A renamed group (`PATCH /api/v1.0/groups/:id`,
  the admin's alone) and a renamed user (`PATCH /api/v1.0/users/:id`)
  keep their reads, which the ACL lists by their new names, and a new
  group and a new user of the names they left get nothing.
- `group-invites`: a group's owner makes invite links
  (`POST /api/v1.0/groups/:id/invites`). A single-use link joins the
  first user who redeems it (`POST /api/v1.0/invites/redeem`), and no
  other; a multi-use one joins each who does. A member who does not own
  the group may make none (403).
- `aup`: the admin puts up a new acceptable use policy
  (`PUT /api/v1.0/aup`). Until the admin agrees to it, the server
  refuses them its user routes (403, with `requires_aup` and the new
  version), agreeing to the old version does not do, and `/whoami`
  says `requires_aup` and the version; the policy is public
  (`GET /api/v1.0/aup` with no credentials). Once they agree, the user
  routes serve them. Another new policy gates them again, as it does a
  user who had agreed to the old one. The suite then puts back the
  policy it found. A policy's version is the hash of its text
  (`database/aup.go`), so that restores the version every user had
  agreed to, and the user's routes serve that user again without their
  agreeing anew (`restored`).

`aup` comes last, since for a moment it gates every user of the origin,
the admin included, who agrees again afterward. Should the policy not
be restored, every suite that uses the routes it gates agrees to the
AUP again as it starts.

The run's collections, groups, users, and clients are removed after
the suite, and those that earlier runs left. The server only marks a
deleted user or group deleted, and frees its name. `test-user-admin`
stays.
