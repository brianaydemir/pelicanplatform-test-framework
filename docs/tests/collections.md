# Collections

Part of [What the tests check](README.md).

A collection is a prefix of a [namespace](../glossary.md#namespace) with
access control of its own ([Pelican](https://pelicanplatform.org/)'s
[`docs/collections-design.md`](https://github.com/PelicanPlatform/pelican/blob/main/docs/collections-design.md)).
The [`collections`](../../framework/suites/collections.py) suite checks the
origin's collection API, and the storage scopes its issuer grants for
collections. It skips under
[`-p auth-no-issuer`](../../framework/presets/auth-no-issuer.sh) and
[`-p auth-external-issuer`](../../framework/presets/auth-external-issuer.sh),
and runs in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)). A row per
[scenario](../glossary.md#scenario).

Its users, `test-<role>`, get only what collections grant; `test-curator` is
a collection administrator (`Server.CollectionAdminUsers`, in
[`framework/config.d/base/40-origin.yaml`](../../framework/config.d/base/40-origin.yaml)).
Each run makes `alpha`, private, with sharing on, readers (`test-reader`,
`test-sharer`), writers (`test-writer`), and an admin group; `beta`, public;
both `test-owner`'s; and `decoy/obj` beside them.

- `users`: The admin lists the suite's users; an ordinary user may not
  create one (403), nor join a group they do not own.
- `create`: A collection administrator creates (201) and deletes a
  collection, private and their own; an ordinary user may not (403). A
  namespace outside every export, climbing out of one, or with a space, and
  an unknown visibility, are 400.
- `visibility`: A private collection is hidden (404, unlisted) from users
  without access, and visible to its readers, writers, admin group, owner,
  and collection administrators; `canEdit` is true for the admin group and
  owner alone. A public one is visible to all, but unchangeable without
  access (404).
- `acl`: A writer may not grant (404); a grant of role `owner`, or to a
  missing group, is 400. The ACL lists exactly the owner's grants, one with
  an expiry. The admin group may revoke.
- `tokens`: The namespace's issuer grants each user their collections'
  storage scopes alone. The local issuer grants `collection.read:/` to all,
  and `collection.create:/` only to a collection administrator, whose token
  creates a collection (201); another user's is 403.
- `data`: At every origin taking direct reads, and through the federation,
  tokens read and write within their collections and nothing beside:
  `test-reader` reads `alpha` and `beta` but may not write; `test-outsider`
  reads `beta` alone; `test-writer` writes in `alpha`; none reaches `decoy`.
- `client`: `pelican object get` of `alpha/obj` with no token acquires one,
  approved as the user: `test-reader`'s succeeds, `test-outsider`'s fails.
  Skips for a standalone origin.
- `share-confinement`: A share must lie within its collection. One that
  climbs out (`<alpha>/../decoy`), lies beside it (`decoy`), begins like it
  (`alphax`), or lies outside every export is 400, and grants nothing. Skips
  under
  [`-p origin-multiuser`](../../framework/presets/origin-multiuser.sh).
- `shares`: A share of `alpha/shared` gives `test-guest`
  `share.access:/<share>` and its `storage.read`, and the lesser of the
  share's access and `test-sharer`'s: no write until `test-sharer` may
  write, and nothing once they may not read. Sharing by a non-reader (404),
  of a share, or with sharing off (409) is refused, as is every share under
  `-p origin-multiuser` (409).
- `expiry`: A grant grants nothing after its expiry.
- `revoke`: A revoked grant leaves the ACL and the next token. The old token
  keeps its access until it expires, by design.
- `manage`: The owner, admin group, and writers may rename and describe a
  collection. Otherwise (404 each): the admin group may neither hand it on
  nor delete it; a writer may neither clear its admin group, grant, nor
  delete; a reader may change neither it nor its metadata. The owner sets
  metadata, which a reader lists, deletes it, then deletes the collection.
- `ownership`: A writer may not take a collection (404). The owner hands it
  on, after which they may not delete it (404). The new owner hands it on by
  an invite, which one user redeems, and no other.
- `cli`: `pelican-server origin collection`, approved as `test-curator`,
  creates, lists, shows, changes, grants on, sets and gets metadata of, and
  deletes a collection; as `test-outsider`, it may not create one. Needs
  `pelican-server` ([client binary](../../README.md#client-binary)). It runs
  as on the issuer origin's host, with `Server.ExternalWebUrl` that origin's
  web URL. Skips unless the director lists that origin first, since a
  release that asks the director manages that one.
