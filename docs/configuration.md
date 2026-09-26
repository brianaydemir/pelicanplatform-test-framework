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

For example:

```sh
mkdir -p local/config.d/instance/director-0
cat >local/config.d/instance/director-0/90-local.yaml <<'EOF'
Director:
  CacheSortMethod: random
EOF
./fed.sh restart director-0
```

- A later layer replaces a list instead of merging it. `fed.sh`
  generates `Origin.Exports` from the knobs; to change it, restate the
  whole list. In `local/config.d/base/`, that replaces origin-2's too
  (`-p topo-multi-owner`).
- `Origin.EnableIssuer`, `Origin.Posc.Enabled`,
  `Origin.Metadata.{Enabled,TrackAccess,Mode}`, `Origin.CacheControl`,
  `Origin.DisableDirectClients`, `Origin.Multiuser`, and
  `Server.DropPrivileges` are set by environment variables, which beat
  files, because `fed.sh` and the tests depend on them. To change one,
  write a [preset](#presets) of your own.
- `fed.sh` generates `Logging.Level` (from `PELICAN_LOG_LEVEL`) and every
  server's `EnableOIDC` (from `-p auth-oidc`) in `/fed/generated/conf`,
  so a file of yours can override them, e.g. for one service.
- Under `-p topo-multi-origin`, an origin override usually belongs in
  both `origin-0/` and `origin-1/`.


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
  together, every pair of choices that the presets offer, except
  `auth-oidc` and `with-grafana`, which need an identity provider and a
  data source that the framework lacks. `framework/matrix.py` lists the
  choices, and which combinations `fed.sh` refuses; its unit tests check
  that it agrees with `fed.sh`, and that the shapes cover every pair. A
  preset of your own is in no shape.
- **The connection broker** has no preset.


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
