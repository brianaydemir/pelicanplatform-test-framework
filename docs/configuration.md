# Configuration

How `fed.sh` configures the federation, in more depth than the
[README](../README.md#testing-your-changes): Pelican's configuration
layers, the knobs that `fed.sh` reads, presets of your own, and issuer
keys.

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

- `/fed/control.instance` is the tests' own. It is empty except while a
  test restarts a service with configuration of its own
  ([service control](../README.md#service-control)), and then it wins
  over yours.
- A later layer replaces a list instead of merging it. `fed.sh`
  generates `Origin.Exports`; to change it, restate the whole list. In
  `local/config.d/base/`, that replaces origin-2's too
  (`-p topo-multi-owner`).
- Under `-p topo-multi-origin`, an origin's override usually belongs in
  both `origin-0/` and `origin-1/`.

### Settings from the environment

`fed.sh` and the tests depend on these, so `fed.sh` sets them by
environment variables, which beat files. To change one, write a
[preset](#presets) of your own.

- `Origin.EnableIssuer`, `Origin.Posc.Enabled`,
  `Origin.EnableAtomicUploads`,
  `Origin.Metadata.{Enabled,TrackAccess,Mode}`, `Origin.CacheControl`,
  `Origin.DisableDirectClients`, `Origin.Multiuser`,
  `Origin.EnableStandaloneMode`, and `Origin.EnableTransferAPI`
- `Server.DropPrivileges`
- cache-2's `Cache.EnableSiteLocalMode`

Pelican ignores an empty variable. So, without `-p origin-max-age`, a
file of yours can set `Origin.CacheControl`, but the tests won't know:
the `blocks` suite checks what the origins send only when
`ORIGIN_CACHE_CONTROL` is set.

### Generated settings

`fed.sh` generates these from the presets, in `/fed/generated/conf`, or
per service in `/fed/generated.instance`. A file of yours can override
them, e.g. for one service, but the tests expect what the presets say.

| Setting                                                                                                                             | From                                           |
| ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `Logging.Level`                                                                                                                     | `PELICAN_LOG_LEVEL`                            |
| `Origin.Exports`                                                                                                                    | the namespaces and their capabilities          |
| every server's `EnableOIDC` and `OIDC.Issuer`                                                                                       | `-p auth-oidc-local` or `-p auth-oidc-cilogon` |
| the caches' `Cache.MemoryCacheSize`                                                                                                 | `-p cache-memory`                              |
| each cache's bound on its store (V2: water marks; XRootD: `Cache.Files*Size`)                                                       | `-p cache-small`                               |
| `Director.DefaultResponse`                                                                                                          | `-p director-default-origin`                   |
| `Director.EnableFederationMetadataHosting`                                                                                          | `-p director-discovery`                        |
| each director's `Server.ExternalWebUrl`, `Director.AdvertiseUrl`, and `Xrootd.Sitename`                                             | `-p director-ha`                               |
| the registry's `RequireOriginApproval` and `RequireCacheApproval`                                                                   | `-p registry-approval`                         |
| the registry's `RequireKeyChaining` (off)                                                                                           | `-p topo-multi-owner`                          |
| the embedded issuer's token and code lifetimes                                                                                      | `-p origin-short-tokens`                       |
| an `ssh` origin's `Origin.SSH.TunnelCallback`                                                                                       | `-p origin-ssh-tunnel`                         |
| `Origin.ScitokensNameMapFile` and `Origin.ScitokensDefaultUser`, and the mapfile (`framework/var/generated/multiuser-mapfile.json`) | `-p origin-multiuser`                          |

### Fixed settings

The framework's own layers set these, and the suites depend on them:

- `framework/config.d/base/40-origin.yaml`: the embedded issuer's
  `Issuer.AuthorizationTemplates`, which grant the built-in `admin` and
  the `issuer` suite's users and groups; `Issuer.GroupSource`
  (`internal`); `test-curator` in `Server.CollectionAdminUsers`
  ([collections](tests/collections.md)); and the
  [accounts](tests/accounts.md) suite's authorities:
  `test-server-admins` in `Server.AdminGroups`,
  `test-collection-admins` in `Server.CollectionAdminGroups`, and
  `test-user-admin` in `Server.UserAdminUsers`.
- `framework/config.d/instance/`: where cache-0, cache-1, origin-0, and
  origin-1 are (`GeoLocation`), by which the directors sort
  ([routing](tests/routing.md)).
- `framework/config.d/director/default/` (and `instance/fed/`, in
  `topo-tiny`): a support contact (`Director.SupportContactEmail` and
  `SupportContactUrl`) ([director APIs](tests/director-apis.md)).

### Settings the suites vary

No preset sets these, since they would slow or limit every suite. The
suites that test them restart services with them for a while, in the
tests' own layer, and restore them afterward.

| Suite                                    | Settings                                                                                                                                                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`freshness`](tests/cache-freshness.md)  | `Origin.CacheControl`                                                                                                                                                                                   |
| [`throttling`](tests/throttling.md)      | `Origin.TransferRateLimit`; the V2 caches' `Cache.WorkerCount` and `Cache.Throttle.*`                                                                                                                   |
| [`exports`](tests/exports-and-copies.md) | `Origin.Exports` (restated, replacing yours): no `Listings` or `DirectReads`, and a nested export; `Origin.DisableCopies`; `Origin.HttpAuthTokenFile`, `HttpAuthTokenPassthrough`, and `HttpServiceUrl` |
| [`pstore`](tests/pstore.md)              | `Origin.PStoreStorageDirs`, `Origin.PStoreMetadataBackup*`                                                                                                                                              |
| [`routing`](tests/routing.md)            | `Director.CacheSortMethod`, `CheckCachePresence`, `FilteredServers`, `AssumePresenceAtSingleOrigin`, and response hostnames; `Cache.PermittedNamespaces`                                                |
| [`users`](tests/unix-users.md)           | a mapfile rule, in place; `Origin.ScitokensUsernameClaim`, with no default user                                                                                                                         |
| [`oidc`](tests/oidc-login.md)            | origin-0's `Issuer.GroupSource` (`oidc`) and `Server.UIAdminUsers`                                                                                                                                      |
| [`transfer-api`](tests/transfer-api.md)  | `Transfer.EnabledGroups`, `Transfer.EnableOAuth2Clients`, and origin-0's `Origin.TransferRateLimit`                                                                                                     |

## Knobs

Knobs are shell variables. Later sources win:

1. `framework/environment.cfg`
2. `local/environment.cfg`
3. `framework/presets/default.sh`
4. the `-p` presets, in order
5. your environment, e.g. `PELICAN_LOG_LEVEL=debug ./fed.sh up`

`framework/presets/default.sh` declares every knob that shapes the
federation. Only presets may set those; `fed.sh` ignores them in your
environment. For a shape the presets don't offer, write a
[preset](#presets) of your own; for any other Pelican setting, use a
[configuration file](#pelican-configuration).

You set the knobs below. `local/environment.cfg` can set only those that
`default.sh` doesn't: `PELICAN_TAG`, `IMAGE_*`, `PELICAN_DEV_TAG`, and
`PELICAN_SRC_DIR`. Set the rest in your environment, or in a preset.

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

The [README](../README.md#presets) lists the presets. To add your own,
write `local/presets/<name>.sh` with a new name. It can set any knob in
`framework/presets/default.sh`, or call `fed_enable_profile` to start
optional services. No smoke shape runs a preset of your own.

### Coverage

Together, the shapes that `smoke.sh` runs (`./smoke.sh -l`) make every
pair of choices that the presets offer, including rotating every key
([the smoke matrix](development.md#the-smoke-matrix)). The exceptions:

- `with-grafana`, which needs a data source that the framework lacks.
- `auth-oidc-cilogon`, which needs a CILogon client of your own.
- Rotated keys with `server-unprivileged`, whose servers' keys the host
  may not write.
- `cache-mixed`, `topo-no-cache`, `auth-oidc-local`,
  `topo-transfer-server`, `origin-public`, `origin-public-writes`, and
  `director-ha` with origins other than `posixv2` and `xrootd`, since
  what they test doesn't depend on the origins' storage.
- A transfer API (`origin-transfer-api` or `topo-transfer-server`) where
  `/protected-a` takes no writes (`origin-no-direct` or
  `origin-public`), since the `transfer-api` suite then skips.

Two shapes are baselines, whose failures are none of the presets'
doing: `default`, the framework's own choices, and `pelican-defaults`,
Pelican's.

[What is not tested](tests/what-is-not-tested.md) lists notable choices
that no preset makes; [above](#settings-the-suites-vary) are those that
a suite makes for a while.

## Issuer keys

Each service reads a directory of keys. Pelican signs with the `.pem`
that sorts first, and publishes them all, so the `NN-` prefix decides
which key is active.

```sh
./fed.sh keys                 # list keys; the first per service is active
./fed.sh keys init            # create a key wherever one is missing
./fed.sh keys add TARGET      # add a verify-only key
./fed.sh keys rotate TARGET   # add a key and make it active
```

`TARGET` is a service (e.g. `director-0` or `fed`), `issuer` (the
external issuer's), `origins`, `directors`, or `all`. origin-0 and
origin-1 share a key, as do director-0 and director-1; naming just one
of them breaks that on purpose. origin-2, another owner, has its own.

- Servers pick up a new key within a minute, but origins and caches may
  keep a keyset they fetched, even their own, for up to 15 minutes.
  `./fed.sh restart` skips the wait.
- Database backups and a V2 cache's store are encrypted with the
  service's keys, so a cache must start with the keys that sealed its
  store. `fed.sh` never replaces a key.
- An empty key directory doesn't simulate a missing key, since Pelican
  generates its own.
- `keys` also lists the four [test keys](tests/authorization.md), which
  `keys init` creates. They are never rotated.
- The [keys](tests/keys.md) suite adds and rotates keys while the
  federation runs, and restores them afterward.
