# Keys

Part of [What the tests check](README.md).

Each server signs with the first of the keys in its directory, and
publishes them all ([issuer keys](../configuration.md#issuer-keys)).
`smoke.sh` rotates every key in some shapes, but restarts everything
before it tests again. The `keys` suite changes keys while the servers
run, as `./fed.sh keys add` and `keys rotate` do, writing into
`framework/var/issuer-keys/` from the dev container, and puts them back:
a key it added is removed, and one it made the signer is renamed to
sort last, so it stays as a verify-only key, since a V2 cache must keep
the keys that sealed its store. It comes last of all: a rotation takes a
server out of the directors' routing until the keys are back, and the
servers keep what they cached of a namespace's keys for up to 15
minutes. It skips under `-p server-unprivileged`, whose servers' keys
the host may not write, and the tiny topology, whose one process holds
every key. Its probe object goes under `/protected-a/data/keys/<run>/`,
and is removed afterward. A scenario with several cases records a row
per case, named `<scenario> <case>`. The suite runs in the WLCG pass
alone ([token profiles](README.md#token-profiles)), since a second pass
would change the keys again while the servers still hold what the first
left; `hot-rotate` presents tokens of both profiles in its one rotation
instead.

- `hot-add`: a verify-only key, written into the origins' key
  directories (origin-0's and origin-1's, which share a key) so that it
  sorts last: within 120 seconds, origin-0's own JWKS must list it
  (`origin-jwks`), and so must the registry's for `/protected-a`
  (`registry-namespace`) and for origin-0's own prefix
  (`registry-server`), since an origin rereads its key directory every
  minute and pushes its keys to the registry when they change, signing
  the update with a key the registry already holds
  (`LaunchIssuerKeysDirRefresh` in
  `launcher_utils/key_refresh_and_update_namespace_pubkey.go`,
  `updateNamespacesPubKey` in `registry/registry_pubkey_update.go`); the
  note times each. Then the key is removed, and each must be rid of it
  within 120 seconds (`removed`). No director may drop origin-0
  meanwhile (`routing`). It skips for a standalone origin, which
  registers nothing.
- `hot-rotate`: a new signing key on the origins, and another on the
  first federated V2 cache, each written to sort first, and watched for
  3 minutes, every 15 seconds. Every director must go on naming
  origin-0 and the cache (`directors`): a director checks an ad's
  token against the keys it fetched from the registry for the server's
  prefix up to `Director.AdvertisementTTL` (15 minutes) before, and
  never refetches for a kid it lacks (`verifyAdvertiseToken` in
  `director/origin_api.go`), so once a server's ads carry its new kid
  the directors refuse them, its last accepted ad expires, and it drops
  out of routing (D5), and the rows fail, on v26.0.0-rc.3 and main
  alike. Each origin that takes direct clients, and each federated
  cache, must come to take tokens signed with the origins' new key, and
  the note says how soon (`new-key`); they keep a namespace's keys for
  up to 15 minutes (`origin_serve/authz.go`,
  `local_cache/cache_authz.go`), which the window may not outlast, and
  the row is then inconclusive; each must take the old key's tokens
  throughout (`old-key`). Each token is presented in both profiles, and
  the SciTokens ones have rows of their own, named for the server and
  `scitokens` (e.g. `new-key origin-0 scitokens`), which fail at a
  native server (T6). Under `-p auth-external-issuer`, the exports
  trust none of the origins' keys, so the token rows skip. The
  issuer's tokens must come to carry the new kid (`issuer-kid`).
  The registry must come to list the origins'
  new key (`registry-origins`), and the cache's (`registry-cache`): a
  cache reconciles its keys with the registry only as it starts
  (`RegisterNamespaceWithRetry` in `launcher_utils/register_namespace.go`),
  never when they change (R4), so that row fails, on v26.0.0-rc.3 and
  main alike. Afterward both new keys are demoted, and every director
  must name origin-0 again.
- `master-key`: a V2 cache seals its master key, with which its store
  is encrypted, with each of its keys, in `masterkey.json` under its
  store (`local_cache/encryption.go`). At the first federated V2 cache,
  given a verify-only key (one the suite adds, if `hot-rotate` left
  none), the file must be sealed with every key before a restart
  (`sealed`) and after one (`sealed-after-restart`): Pelican re-seals
  it only through a key-change callback that the launcher registers for
  its local cache module, never for the cache server
  (`launchers/launcher.go`, `launchers/cache_serve.go`), and loading the
  file never re-saves it, so both rows fail, on v26.0.0-rc.3 and main
  alike (C4). Then, destructively, with every key but the newest
  removed from the cache's key directory, the cache restarted must
  still open its store, and serve the probe it held, from its store,
  with `Age`, the origins sending nothing (`retired`): a master key it
  cannot unseal is replaced with a new one, with which its store cannot
  be read, so the cache fails to start, and the row fails. Afterward the
  keys and the master key file are put back, and the cache restarted
  again; should that not bring it back, the note says to run
  `./reset.sh`. It needs a control helper.
- `backups`: each server (every origin, cache, and director, and the
  registry) keeps encrypted backups of its database under
  `framework/var/state/<service>/backups/` (`Server.DatabaseBackup`;
  `config.d/base/20-server.yaml`), one from startup on, since a server
  with none makes one as it starts (`LaunchPeriodicBackup` in
  `database/backup.go`). There must be at least one; each must be PEM,
  never a plain SQLite file; and `pelican-server server database backup
  verify` must open each, given a copy of the server's keys
  (`PELICAN_ISSUERKEYSDIRECTORY`), after the suite's key changes too.
  Without a `pelican-server` ([client binary](../../README.md#client-binary)),
  the backups are checked but not opened.
