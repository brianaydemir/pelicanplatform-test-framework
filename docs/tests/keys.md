# Keys

Part of [What the tests check](README.md).

The [`keys`](../../framework/suites/keys.py) suite changes
[issuer keys](../configuration.md#issuer-keys) while the servers run, as
`./fed.sh keys add` and `keys rotate` do, then restores them. A key it made
the signer stays as a verify-only key, since V2 caches and
[pstore](pstore.md) origins need the keys that sealed their stores. The next
run removes it first, if each store is sealed with a key that stays too, so
that such keys do not pile up.

The suite runs last, since servers cache keys for up to 15 minutes, so its
changes outlast it. It runs in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)), since a second pass would
change the keys again. It skips under
[`-p server-unprivileged`](../../framework/presets/server-unprivileged.sh),
whose keys the host may not write, and under
[`-p topo-tiny`](../../framework/presets/topo-tiny.sh), whose one process
holds every key. Its probe object goes under
`/protected-a/data/keys/<run>/`. A [case](../glossary.md#case)'s row is
named `<scenario> <case>`.

- `hot-add`: A verify-only key added to the origins must appear within 120
  seconds in origin-0's JWKS (`origin-jwks`), and in the registry's for
  `/protected-a` (`registry-namespace`) and for origin-0's server prefix
  (`registry-server`). Once removed, it must disappear from each within 120
  seconds (`removed`). No director may drop origin-0 meanwhile (`routing`).
  Skips for a
  [standalone origin](../../framework/presets/topo-standalone.sh).

- `hot-rotate`: New signing keys on the origins and on the first federated
  V2 cache, watched for 3 minutes:

  - every director keeps naming origin-0 and the cache (`directors`);
  - each origin that takes direct clients, and each federated cache, comes
    to accept tokens signed with the origins' new key (`new-key`), and
    accepts the old key's throughout (`old-key`);
  - the issuer's tokens come to carry the new `kid` (`issuer-kid`);
  - the registry comes to list the origins' new key (`registry-origins`) and
    the cache's (`registry-cache`).

  Rows of `directors`, `new-key`, and `old-key` name the server, as in
  `new-key origin-0`. Tokens are presented in both
  [token profiles](../glossary.md#token-profile); SciTokens rows end in
  `scitokens`. `new-key` is `INCONCLUSIVE` if the window ends first, since
  servers cache keys. Token rows skip under
  [`-p auth-external-issuer`](../../framework/presets/auth-external-issuer.sh),
  and `issuer-kid` where the origins run no issuer. Skips for a standalone
  origin.

- `master-key`: The first federated V2 cache's `masterkey.json` must be
  sealed with each of its keys, before a restart (`sealed`) and after one
  (`sealed-after-restart`). The suite adds a verify-only key first if the
  cache has only one. Then, destructively, with every key but the newest
  removed, the restarted cache must still open its store and serve the probe
  from it, with `Age`, fetching nothing (`retired`). That row is
  `INCONCLUSIVE` if the cache will not serve the probe beforehand. Afterward
  the keys and the master key file are put back; if the cache does not
  recover, the note says to run [`./reset.sh`](../../reset.sh). Needs a
  [control helper](../glossary.md#service-control-helper).

- `pstore-master-key`: The same for the first pstore origin
  ([`-p origin-pstore`](../../framework/presets/origin-pstore.sh)), whose
  store holds the only copy of its objects. Keys are removed from that
  origin's key directory alone. `retired` checks the probe through the
  storage API, and fails if the origin replaced its master key file. Needs a
  control helper.

- `backups`: Each server's database backups
  (`framework/var/state/<service>/backups/`; `Server.DatabaseBackup`) must
  exist, be PEM, and open with
  `pelican-server server database backup verify`, given the server's keys.
  Without `pelican-server` ([client binary](../../README.md#client-binary)),
  they are checked but not opened. A row per server.
