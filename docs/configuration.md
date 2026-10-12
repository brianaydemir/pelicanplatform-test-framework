# Configuration

How `fed.sh` configures the federation, in more depth than the
[README](../README.md#testing-your-changes): how Pelican's configuration
files layer, the knobs that `fed.sh` reads, writing presets of your own,
and managing issuer keys.


## Pelican configuration

Each service merges these directories, and later ones win. Within a
directory, files merge in lexicographic order.

| In the container          | From                                                  |
| ------------------------- | ----------------------------------------------------- |
| `/fed/conf.base`          | `framework/config.d/base/`                            |
| `/fed/conf.role`          | `framework/config.d/<origin\|cache\|...>/<variant>/`  |
| `/fed/generated/conf`     | `framework/var/generated/conf/` (from the shape)      |
| `/fed/generated.instance` | `framework/var/generated/instance/<service>/` (ditto) |
| `/fed/conf.instance`      | `framework/config.d/instance/<service>/`              |
| `/fed/local.base`         | `local/config.d/base/`                                |
| `/fed/local.instance`     | `local/config.d/instance/<service>/`                  |
| `/fed/control.instance`   | `framework/var/control/config/<service>/` (tests)     |

For example:

```sh
mkdir -p local/config.d/instance/director-0
cat >local/config.d/instance/director-0/90-local.yaml <<'EOF'
Director:
  CacheSortMethod: random
EOF
./fed.sh restart director-0
```

- `/fed/control.instance` is the tests' own, and empty but while a
  test restarts a service with configuration of its own
  ([service control](../README.md#service-control)), so it wins over
  yours then.
- A later layer replaces a list instead of merging it. `fed.sh`
  generates `Origin.Exports` from the knobs; to change it, restate the
  whole list. In `local/config.d/base/`, that replaces origin-2's too
  (`-p topo-multi-owner`).
- `Origin.EnableIssuer`, `Origin.Posc.Enabled`,
  `Origin.EnableAtomicUploads`, `Origin.Metadata.{Enabled,TrackAccess,Mode}`,
  `Origin.CacheControl`, `Origin.DisableDirectClients`, `Origin.Multiuser`,
  `Origin.EnableStandaloneMode`, `Origin.EnableTransferAPI`,
  `Server.DropPrivileges`, and cache-2's `Cache.EnableSiteLocalMode` are
  set by environment variables, which beat files, because `fed.sh` and
  the tests depend on them. To change one,
  write a [preset](#presets) of your own. Pelican ignores an empty
  variable, though, so without `-p origin-max-age`, which leaves
  `Origin.CacheControl` empty, a file of yours can still set it, and
  the tests won't know: the `blocks` suite checks what the origins send
  only when `ORIGIN_CACHE_CONTROL` is set. The `freshness` suite sets it
  this way itself, for a while, in the tests' own layer
  ([cache freshness](tests.md#cache-freshness)).
- No preset slows a server, since every suite would crawl. The
  `throttling` suite slows the origins (`Origin.TransferRateLimit`) and
  shrinks the V2 caches' scheduler (`Cache.WorkerCount`,
  `Cache.Throttle.*`) itself, for as long as it runs, in the tests' own
  layer ([throttling](tests.md#throttling)).
- No preset gives an export without `Listings`, or one without
  `DirectReads` beside others with them, or turns copies off
  (`Origin.DisableCopies`). The `exports` suite restarts the origins
  so for a while, in the tests' own layer, with `fed.sh`'s
  `Origin.Exports` restated, which then also replaces any of yours
  ([exports and copies](tests.md#exports-and-copies)). Nor does one
  have an `https` or `httpsv2` origin present a token to its backend
  (`Origin.HttpAuthTokenFile`, `Origin.HttpAuthTokenPassthrough`),
  which asks for none: the same suite restarts the origins so for a
  while, reaching the backend through the `webdav-gate` service, which
  records what they send (`Origin.HttpServiceUrl`).
- No preset bounds a pstore origin's store (`Origin.PStoreStorageDirs`),
  which would bound every suite's writes, or backs up its catalog
  (`Origin.PStoreMetadataBackup*`). The `pstore` suite restarts the
  first origin so for a while, in the tests' own layer, and stops it
  meanwhile to restore one of the backups into its store
  ([pstore](tests.md#pstore)).
- `framework/config.d/instance/` declares where cache-0, cache-1,
  origin-0, and origin-1 are (`GeoLocation`), which the directors sort
  by, and the `routing` suite checks. No preset changes how the
  directors sort or filter the servers (`Director.CacheSortMethod`,
  `CheckCachePresence`, `FilteredServers`,
  `AssumePresenceAtSingleOrigin`, and the response hostnames), or
  restricts a cache to namespaces (`Cache.PermittedNamespaces`). Under
  `-p topo-multi-cache`, the `routing` suite restarts the directors so
  for a while, in the tests' own layer, and then the last cache so
  restricted ([routing](tests.md#routing)).
- `framework/config.d/director/default/` (and `instance/fed/`, in
  `tiny`) names a support contact (`Director.SupportContactEmail` and
  `SupportContactUrl`), which the `director-api` suite checks
  ([director APIs](tests.md#director-apis)).
- Under `-p origin-multiuser`, `fed.sh` generates the mapfile,
  `framework/var/generated/multiuser-mapfile.json`, and sets
  `Origin.ScitokensNameMapFile` and `Origin.ScitokensDefaultUser` in
  `/fed/generated/conf`. The `users` suite adds a rule to the mapfile
  for a while, in place, and restarts the origins with
  `Origin.ScitokensUsernameClaim` set and no default user, in the
  tests' own layer ([Unix users](tests.md#unix-users)).
- `fed.sh` generates `Logging.Level` (from `PELICAN_LOG_LEVEL`), every
  server's `EnableOIDC` and, with it, `OIDC.Issuer`, the identity
  provider's URL (from `-p auth-oidc`; `Issuer.GroupSource` stays
  `internal`, and the `oidc` suite restarts origin-0 with `oidc`, and
  with `Server.UIAdminUsers`, for a while), the caches'
  `Cache.MemoryCacheSize` and water marks (from `-p cache-memory` and
  `-p cache-small`), `Director.DefaultResponse` (from
  `-p director-default-origin`), the registry's
  `RequireOriginApproval`, `RequireCacheApproval` (from
  `-p registry-approval`), and `RequireKeyChaining` (off under
  `-p topo-multi-owner`), and the embedded issuer's token and code
  lifetimes (from `-p origin-short-tokens`) in `/fed/generated/conf`, and
  director-0's `Director.EnableFederationMetadataHosting` (from
  `-p director-discovery`) in its `/fed/generated.instance`, so a file
  of yours can override them, e.g. for one service; but the tests
  expect what the presets say. Under `-p origin-export-templates`, the
  generated `Origin.Exports` gives `/protected-b` authorization
  templates of its own.
- No preset names a group in `Transfer.EnabledGroups`, or turns on the
  transfer API's OAuth2 clients (`Transfer.EnableOAuth2Clients`). Under
  `-p origin-transfer-api` or `-p topo-transfer-server`, the
  `transfer-api` suite restarts the API's host so for a while, with a
  group of the run's own, in the tests' own layer, and restarts it
  again with a job under way, origin-0 slowed meanwhile
  (`Origin.TransferRateLimit`) ([transfer API](tests.md#transfer-api)).
- Under `-p topo-multi-origin`, an origin override usually belongs in
  both `origin-0/` and `origin-1/`.
- `framework/config.d/base/40-origin.yaml` sets the embedded issuer's
  `Issuer.AuthorizationTemplates`, which grant the built-in `admin` and
  the `issuer` suite's users and groups ([issuer](tests.md#issuer)),
  takes a web login's groups from the server's own database
  (`Issuer.GroupSource`), and names `test-curator` in
  `Server.CollectionAdminUsers`; the `collections` and `issuer` suites
  depend on these ([collections](tests.md#collections)). It also names
  the `accounts` suite's authorities: the groups `test-server-admins`
  in `Server.AdminGroups` and `test-collection-admins` in
  `Server.CollectionAdminGroups`, whose names each server reserves as
  it starts, and `test-user-admin` in `Server.UserAdminUsers`
  ([accounts](tests.md#accounts)).


## Knobs

Knobs are shell variables. Later sources win:

1. `framework/environment.cfg`
2. `local/environment.cfg`
3. `framework/presets/default.sh`
4. the `-p` presets, in order
5. your environment, e.g. `PELICAN_LOG_LEVEL=debug ./fed.sh up`

The table lists the knobs you set. The rest shape the federation, so
only presets set them, and `fed.sh` ignores them in your environment.
`framework/presets/default.sh` lists them. For a shape that the presets
don't offer, write a [preset](#presets) of your own; to change any other
Pelican setting, use a [configuration file](#pelican-configuration).

`default.sh` sets every knob that it lists, so `local/environment.cfg`
can only set the others: `PELICAN_TAG`, `IMAGE_*`, `PELICAN_DEV_TAG`,
and `PELICAN_SRC_DIR`. Set `PELICAN_LOG_LEVEL`, the timezones, and the
memory limits in your environment, or in a preset of your own.

| Knob                | Default                            | Sets                                                                                    |
| ------------------- | ---------------------------------- | --------------------------------------------------------------------------------------- |
| `PELICAN_TAG`       | see `framework/environment.cfg`    | release for images and binaries                                                         |
| `IMAGE_<SERVICE>`   | `${IMAGE_HUB}/<service>:<tag>`     | one service's image (`ORIGIN`, ..., `DEV`); `DEV`'s is `pelican-dev:${PELICAN_DEV_TAG}` |
| `IMAGE_HUB`         | `hub.osg-htc.org/pelican_platform` | image registry                                                                          |
| `PELICAN_DEV_TAG`   | `latest-itb`                       | dev container tag                                                                       |
| `PELICAN_SRC_DIR`   | `$HOME/pelican`                    | mounted at `/app` in the dev container                                                  |
| `PELICAN_LOG_LEVEL` | `info`                             | `Logging.Level`                                                                         |
| `TZ_SERVICES`       | `UTC`                              | central services' timezone                                                              |
| `TZ_STORAGE`        | `America/Chicago`                  | origins' and caches' timezone                                                           |
| `<SERVICE>_MEM`     | see `framework/presets/default.sh` | memory limits                                                                           |


## Presets

The [README](../README.md#presets) lists the presets. Each is a short
file in `framework/presets/`, so read it for the details, including what
it refuses. To add your own, write `local/presets/<name>.sh` with a new
name. It can set any knob in `framework/presets/default.sh`, or call
`fed_enable_profile` to start optional services.

- **Coverage.** The shapes that `smoke.sh` runs (`./smoke.sh -l`) make,
  together, every pair of choices that the presets offer, and rotating every
  key, which some shapes do before running every suite again. The
  exceptions: `with-grafana`, which needs a data source that the
  framework lacks; rotated keys with `server-unprivileged`, whose
  servers' keys the host may not write; `cache-mixed`, `topo-no-cache`,
  and `auth-oidc` with origins other than `posixv2` and `xrootd`, since
  what the first tests is between the caches, the second how the
  directors and the clients do without one, and the third the servers'
  web login, which their storage does not change; and
  `topo-transfer-server`, whose transfer module is on Pelican's main
  only, which no released `pelican-server` has, so it runs only from
  images built from main.
  Under
  `origin-no-direct`, `/protected-a` takes no writes, so the `posc`,
  `metadata`, and `users` suites skip; its pairs with `origin-posc`,
  `origin-atomic-uploads`, `origin-metadata`, `origin-metadata-tx`, and
  `origin-multiuser` test reads with those features on.
  `framework/matrix.py` lists the choices, and which combinations
  `fed.sh` refuses, which must agree with `fed.sh`'s checks;
  `framework/matrix.py check` checks that the shapes cover every pair. A
  preset of your own is in no shape.
- **The connection broker** (`origin-broker`) is on, but the origin stays
  reachable, so only the director always goes through it: Pelican's
  brokered path to an unreachable origin does not work end to end.


## Issuer keys

Each service reads a directory of keys. Pelican signs with the `.pem`
that sorts first and publishes them all, so the `NN-` prefix decides
which key is active.

```sh
./fed.sh keys                 # list keys; the first per service is active
./fed.sh keys init            # create a key wherever one is missing
./fed.sh keys add TARGET      # add a verify-only key
./fed.sh keys rotate TARGET   # add a key and make it active
```

`TARGET` is a service (e.g. `director-0` or `fed`), `issuer` (the
external issuer's), `origins`, or `all`. origin-0 and origin-1 share a
key; naming just one of them breaks that on purpose. origin-2, another
owner, has a key of its own, which the `owners` suite signs its tokens
with.

- Services pick up a new key within a minute, but origins and caches may
  keep a keyset they fetched, even their own, for up to 15 minutes.
  `./fed.sh restart` skips the wait.
- Database backups and a V2 cache's store are encrypted with the
  service's keys, so a cache must start with the keys that sealed its
  store. `fed.sh` never replaces a key.
- An empty key directory doesn't simulate a missing key, because Pelican
  generates its own.
- `keys` also lists the four [test keys](tests.md#authorization), which
  `keys init` creates. They are never rotated.
- The `keys` suite adds and rotates keys itself, as `keys add` and
  `keys rotate` do, while the federation runs, and puts them back: a key
  it added is removed, and one it made the signer is renamed to sort
  last, so it stays as a verify-only key, since a V2 cache must keep the
  keys that sealed its store ([keys](tests.md#keys)). Its `master-key`
  scenario also retires a cache's keys for a moment, and restores them
  with the cache's master key file.
