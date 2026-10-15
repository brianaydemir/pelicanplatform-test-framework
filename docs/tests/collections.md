# Collections

Part of [What the tests check](README.md).

A collection is a prefix of an exported namespace with access control
of its own (Pelican's `docs/collections-design.md`): an owner, an admin
group, groups and users that may read or write, and a visibility.
Collections live in an origin's database, and the origin's embedded
OAuth2 issuer turns them into storage scopes: a token it mints for a
user carries `storage.read` (and, for a writer, `storage.modify` and
`storage.create`) of each collection the user may use, and nothing for
the rest of the namespace. The `collections` suite needs that issuer
(`ORIGIN_ENABLE_ISSUER`, on by default) and a protected namespace, and
skips with the issuer off (`-p auth-no-issuer`) and with the external
issuer (`-p auth-external-issuer`).

The suite's users are records of the issuer origin (origin-0, or `fed`),
which its admin creates once and later runs reuse: `test-curator`, whom
`Server.CollectionAdminUsers` in `framework/config.d/base/40-origin.yaml`
makes a collection administrator, and seven ordinary users, `test-owner`,
`test-writer`, `test-reader`, `test-outsider`, `test-sharer`, `test-guest`,
and `test-heir`. The suite acts as each through a bearer token that the
origin's web API takes as theirs: signed with the origin's own key and
naming its local issuer, as `pelican cache introspect` mints the admin's
(`credentials.mint_user()`), so no login or identity provider is needed.
The admin, and then each user, first agree to the origin's acceptable
use policy, without which it refuses them its routes for users and
groups ([web API](web-api.md)).
For data, each user gets tokens from the namespace's issuer through its
device flow (RFC 8628), which the suite approves at the issuer's
verification page as the user, as Pelican's own tests do
(`framework/testlib/issuer.py`). Since the issuer binds a client that
registers itself to the first user who approves a flow for it, and
allows an address five registrations, then one a minute, the suite
registers none: the admin creates one client of each issuer through the
issuer's admin API. The `client` and `cli` scenarios register two each,
as the Pelican client and CLI do that themselves.

The embedded issuer's `Issuer.AuthorizationTemplates`, which grant a
user beyond their collections, grant only the built-in `admin` and the
`issuer` suite's users and groups (`40-origin.yaml`), so that this
suite's users get only what collections grant them.

Each run makes groups of its own, owned by `test-owner`
(`test-readers-<run>`, `test-writers-<run>`, and `test-admins-<run>`, for
`test-heir`), and collections under `/<namespace>/data/collections/<run>/`:
`alpha`,
private, `test-owner`'s, with the admins group as its admin group, the
readers allowed to read and the writers to write, and sharing on; and
`beta`, public, `test-owner`'s, which every authenticated user may read.
Each gets an object (`alpha/shared/obj` too, for a share), and `decoy/obj`
sits beside them in no collection. `./fed.sh test -l collections` lists
the scenarios. In short:

- `users`, `create`, `visibility`, `acl`, `manage`, and `ownership`
  check the collections API, through the origin's web API: who may
  create a collection (a collection administrator, with a namespace
  within an export), see it (anyone, if public; otherwise its owner,
  admin group, and grantees), change, rename, and describe it (owner,
  admin group, and writers), grant and revoke access (owner and admin
  group), delete it (the owner alone), and hand it on (the owner alone,
  by setting its owner, or by an invite that one user redeems once).
  A collection one may not see is "not found" (404), so its existence
  is hidden. Where a scenario changes things, it makes a collection of
  its own.
- `tokens` checks what the issuer mints: the storage scopes of each
  user's collections alone, within the namespace, and from the origin's
  local issuer `collection.create` only for a collection administrator,
  which the collections API honors. The local issuer grants
  `collection.create:/` to any user (`test-outsider`'s token carries
  it), and the API refuses the token anyway, since it checks the user's
  standing itself (403, "you must hold server.collection_admin"), so
  the row fails, on v26.0.0-rc.3 and main alike (I13).
- `data` presents those tokens, straight to every origin (where the
  namespace takes direct clients) and through the federation with
  `pelican object`: reads and writes within a collection, and nothing
  beside it. `client` has `pelican object get` acquire its own token
  from the issuer, approved as the user; `test-outsider`'s gets nothing.
- `share-confinement`: `test-sharer` may share only within `alpha`: a
  share whose namespace climbs out of it (`<alpha>/../decoy`), lies
  beside it (`decoy`), begins like it without lying in it (`alphax`), or
  lies outside every export is refused (400), and `test-guest`'s token
  gets nothing of `decoy`. Pelican cleans the namespace before it
  compares it with the collection's (`CreateShare` in
  `database/collection.go`), so this guards against a regression. It
  runs before `shares`, which leaves `test-sharer` unable to read
  `alpha`, and skips for a multiuser origin, which refuses every share.
- `shares`: `test-sharer`, who may read `alpha`, shares `alpha/shared`
  with `test-guest`, whose token then carries `share.access:/<share>` and
  the share's `storage.read`, and whose access is the lesser of the
  share's and `test-sharer`'s own: write on the share grants nothing
  until `test-sharer` may write `alpha`, and nothing at all once
  `test-sharer` may not read it. A share of a share, and a share of a
  collection with sharing off, are refused; a multiuser origin
  (`-p origin-multiuser`) refuses every share (409), since its data path
  cannot act as the share's owner.
- `expiry` and `revoke`: a grant with an expiry grants nothing after it,
  and a revoked grant is gone from the next token. The old token keeps
  its access until it expires, by design: a token is not revoked with
  the grant it came from.
- `cli`: `pelican-server origin collection` creates, lists, changes,
  grants on, and deletes a collection through the device flow, approved
  as `test-curator`, and fails for `test-outsider`. The command is
  server-only: Pelican's client lacks it. The scenario runs the dev
  container's own `pelican-server`, if it has one, and otherwise
  `framework/var/bin/linux/pelican-server`, which `./fed.sh init`
  fetches, and skips if there is neither. The CLI manages the first
  origin the director lists, so the scenario skips where that is not
  the issuer origin; it and `client` skip for a standalone origin,
  which has no director. The CLI asks the origin's local issuer for
  `collection.read:/` or `collection.create:/`, the path form, and the
  v26.0.0-rc.3 origin's web API wants the scope bare (`collection.read`),
  so it refuses the token ("Token does not contain any of the scopes:
  [web_ui.access collection.read]"), and every command fails there
  (I14); main's CLI says the path form is the one its server takes.

The run's collections, groups, and objects are removed after the suite;
the users stay.
