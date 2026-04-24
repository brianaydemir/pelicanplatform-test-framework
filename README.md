# Pelican Test Framework

Stand up a local Pelican federation in Docker, fill it with known
objects, and run client transfers against it. It supports published
releases as well as your own builds.

The default federation has one director, one registry, one origin, one
cache, and an nginx container serving the federation metadata. It uses
no XRootD and no external identity provider. Presets add everything
else.


## Requirements

- Docker Engine 25+ and Docker Compose v2.24+
- `bash`, `curl`, `openssl`, `ssh-keygen`, `tar`
- Memory: about 2.5 GB of container limits by default (3.7 GB with
  `-p ha -p multi`)
- Optional: a clone of [Pelican](https://github.com/PelicanPlatform/pelican)
  to mount in the dev container


## Quickstart

```sh
./fed.sh init          # certificates, binaries, issuer keys, test data
./fed.sh up            # start the federation
./fed.sh dev           # open a shell in the dev container, then:
cd /scratch && ./run.sh
```

`run.sh` ends with a pass/fail summary and a count of which servers
handled the transfers. It exits non-zero on unexpected results. Stop
with `./fed.sh down`. Start over with `./reset.sh init`.


## Workflow

1. **Choose a shape.** Presets are passed with `-p` and remembered in
   `generated/presets`, so later commands inherit them. `-p default`
   clears them.

       ./fed.sh -p xrootd -p multi init

2. **Prepare.** `./fed.sh init` needs no Docker. Each step is skipped if
   already done; `--force` redoes the certificates, binaries, and test
   data. Rerun `init` after changing presets, because the test data
   depends on the shape (`up` warns if it doesn't match).

3. **Start.** `./fed.sh up` checks for certificates, keys, and matching
   test data, then runs `docker compose up`. It also removes containers
   the current shape doesn't use.

4. **Test.** Inside `./fed.sh dev`, the checkout is at `/scratch` and
   your Pelican clone is at `/app`. Run `./run.sh` for the full load, or
   use the client directly:

       /scratch/bin/linux/pelican object get \
           pelican://discovery:8444/public/data/0.0 /tmp/0.0

5. **Inspect.** Use `./fed.sh logs -f origin-0`, `./fed.sh status`, the
   [web UIs](#ports), and the service state under `state/` and `data/`.

6. **Iterate.** After changing configuration, run `./fed.sh restart`
   (everything) or `./fed.sh up --force-recreate origin-0` (one service).
   Pelican reads its configuration only at startup.

7. **Reset.** `./reset.sh` stops everything and deletes all generated
   state, including the dev container's volumes (its home directory and
   build caches). `./reset.sh init` then re-prepares with the same
   presets.


## Presets

Presets can be combined, e.g. `./fed.sh -p pstore -p ha up`.

| Preset       | Effect                                                   |
| ------------ | -------------------------------------------------------- |
| *(default)*  | `posixv2` origin, V2 cache, embedded issuer              |
| `xrootd`     | XRootD origin and cache; CILogon login on the origin     |
| `pstore`     | Origin backed by the encrypted Pelican store             |
| `ssh`        | Origin backed by the `lab-server` container over SSH     |
| `ha`         | Adds a second director                                   |
| `multi`      | Adds a second origin (same prefixes) and a second cache  |
| `monitoring` | Adds Grafana                                             |
| `lab`        | Adds `lab-server` without making it the origin's storage |
| `tiny`       | Runs the whole federation in one container (smoke test)  |

Each preset is a short shell file in `presets/`, so read it for the
details. To add your own, write `presets/<name>.sh`: set knobs, or call
`fed_enable_profile` to start optional services.


## Testing unreleased code

### Server images

Every service runs a published image at `PELICAN_TAG`. To test your own
build, build the images from your Pelican checkout:

```sh
cd ~/pelican
for t in origin cache director registry; do
  docker build -f images/Dockerfile --target $t -t pelican-$t:local .
done
```

Then name them in `environment.local.cfg` (untracked):

```sh
IMAGE_ORIGIN=pelican-origin:local
IMAGE_CACHE=pelican-cache:local
IMAGE_DIRECTOR=pelican-director:local
IMAGE_REGISTRY=pelican-registry:local
```

`./fed.sh status` shows which images are in use, and `./fed.sh up`
recreates containers whose image changed. The `tiny` topology runs
everything from `IMAGE_ORIGIN`.

### Client binary

`run.sh` uses `bin/linux/pelican` (and `stash_plugin`, which is a
symlink to it). To test your own client, build over it from inside the
dev container:

```sh
cd /app && go build -tags forceposix,client -o /scratch/bin/linux/pelican ./cmd
```

`init` keeps this binary. It is replaced only when `PELICAN_TAG`
changes, by `init --force`, or by `reset.sh`.

### A different release

Set `PELICAN_TAG` in `environment.local.cfg`, then run `./fed.sh init`
to download the matching binaries.


## Configuration

### Pelican configuration

Every service reads the same layered configuration. Later layers win,
and files within a layer merge in lexicographic order.

| Layer (in the container) | Source in this repo                        |
| ------------------------ | ------------------------------------------ |
| `/fed/conf.base`         | `config.d/base/` (all services)            |
| `/fed/conf.role`         | `config.d/<origin\|cache\|...>/<variant>/` |
| `/fed/generated/conf`    | `generated/conf/`, written by `fed.sh`     |
| `/fed/conf.instance`     | `config.d/instance/<service>/`             |

**Local overrides.** Add an untracked file to the layer you want, for
example:

```sh
cat >config.d/instance/director-0/90-local.yaml <<'EOF'
Director:
  CacheSortMethod: random
EOF
./fed.sh up --force-recreate director-0
```

Use `config.d/instance/<service>/` for one service or `config.d/base/`
for all of them. These files are not ignored by Git on purpose, so
`git status` shows what you've customized. Delete them when you're done.

Keep in mind:

- A later layer *replaces* a list rather than merging into it. Setting
  `Origin.Exports` means restating the whole list.
- `Origin.EnableIssuer` and `Origin.EnableOIDC` come from environment
  variables, which beat files. Set them with the knobs below instead.
- Under `-p multi`, an origin override usually belongs in both
  `origin-0/` and `origin-1/`.

### Knobs

Knobs are shell variables. From lowest to highest precedence, they come
from `environment.cfg`, `environment.local.cfg` (untracked, for your
machine), `presets/default.sh`, the `-p` presets in order, and finally
your shell environment:

```sh
ORIGIN_VARIANT=pstore ./fed.sh up
```

| Knob                   | Default                            | Sets                                   |
| ---------------------- | ---------------------------------- | -------------------------------------- |
| `PELICAN_TAG`          | see `environment.cfg`              | release for images and `bin/`          |
| `IMAGE_<SERVICE>`      | `${IMAGE_HUB}/<service>:<tag>`     | one service's image                    |
| `IMAGE_HUB`            | `hub.osg-htc.org/pelican_platform` | image registry                         |
| `PELICAN_DEV_TAG`      | `latest-itb`                       | dev container tag                      |
| `PELICAN_SRC_DIR`      | `$HOME/pelican`                    | mounted at `/app` in the dev container |
| `TOPOLOGY`             | `full`                             | `full` or `tiny`                       |
| `ORIGIN_VARIANT`       | `posixv2`                          | a directory under `config.d/origin/`   |
| `CACHE_VARIANT`        | `v2`                               | a directory under `config.d/cache/`    |
| `ORIGIN_ENABLE_ISSUER` | `true`                             | `Origin.EnableIssuer`                  |
| `ORIGIN_ENABLE_OIDC`   | `false`                            | `Origin.EnableOIDC`                    |
| `PELICAN_LOG_LEVEL`    | `info`                             | `Logging.Level`                        |
| `TZ_SERVICES`          | `UTC`                              | central services' timezone             |
| `TZ_STORAGE`           | `America/Chicago`                  | origins' and caches' timezone          |
| `<SERVICE>_MEM`        | see `presets/default.sh`           | memory limits                          |

The test data knobs are [below](#test-data). `MAX_CONCURRENT_ADS` (default
32) is set where `run.sh` runs, inside the dev container.


## Commands

| Command                        | Does                                               |
| ------------------------------ | -------------------------------------------------- |
| `./fed.sh [-p PRESET]... init` | prepare certificates, binaries, keys, test data    |
| `./fed.sh up` / `down`         | start / stop (`down` stops everything)             |
| `./fed.sh restart`             | `down`, then `up`                                  |
| `./fed.sh dev`                 | shell in the dev container (starts it if needed)   |
| `./fed.sh status`              | containers, presets, profiles, and images          |
| `./fed.sh keys [...]`          | manage issuer keys (see below)                     |
| `./fed.sh <anything else>`     | passed to `docker compose`, e.g. `logs -f cache-0` |
| `./reset.sh [init]`            | delete everything generated (then re-`init`)       |
| `./run.sh`                     | the test load (inside the dev container)           |


## Where things live

| Path                 | Contents                                                    |
| -------------------- | ----------------------------------------------------------- |
| `state/<service>/`   | SQLite database and backups (`/var/lib/pelican`)            |
| `data/origin/<N>/`   | origin-N's storage (`/data`)                                |
| `data/cache/<N>/`    | cache-N's storage (`/data`)                                 |
| `data/lab-server/`   | the lab server's shared directory                           |
| `data/ads/`          | transfer ads (`input/`), results (`output/`), logs (`log/`) |
| `issuer-keys/<svc>/` | each service's issuer keys (`/fed/issuer-keys`)             |
| `generated/`         | files derived from the shape by `fed.sh`                    |
| `bin/`               | downloaded binaries (`linux/` is used in the dev container) |
| `certs/`             | the framework CA and server certificate                     |
| `grafana/`           | Grafana's database (`reset.sh` keeps it)                    |

All of these are plain directories, so you can read or edit them with
ordinary tools. Stop the federation before editing anything.


## Test data

`init` writes objects named `<origin>.<n>` into the origins' storage,
and writes transfer ads into `data/ads/input/`, in three scenarios:

| Scenario | Objects                                      | Expected |
| -------- | -------------------------------------------- | -------- |
| `exist`  | `/public/data/0.<n>`                         | success  |
| `token`  | `/private/data/0.<n>` (run.sh mints a token) | success  |
| `dne`    | `/public/data/9.<n>` (never created)         | failure  |

| Knob                 | Default | Sets                      |
| -------------------- | ------- | ------------------------- |
| `OBJECTS_PER_ORIGIN` | 512     | objects per primed origin |
| `ADS_PER_SCENARIO`   | 2048    | transfers per scenario    |
| `URLS_PER_EXIST_AD`  | 4       | objects per `exist` ad    |
| `URLS_PER_TOKEN_AD`  | 2       | objects per `token` ad    |
| `URLS_PER_DNE_AD`    | 1       | objects per `dne` ad      |

Ads pick objects uniformly; change `pick_object` in `init-data.sh` to
skew that.

A `pstore` origin can't be primed from the host. Start it empty and
upload with `pelican object put`. Uploaded objects survive `init`, even
with `--force`, but not `reset.sh`.


## Issuer keys

Each service reads a directory of keys. Pelican signs with the `.pem`
that sorts first and publishes the rest, so the `NN-` prefix decides
which key is active.

```sh
./fed.sh keys                 # list keys; the first per service is active
./fed.sh keys init            # create a key wherever one is missing
./fed.sh keys add TARGET      # add a verify-only key
./fed.sh keys rotate TARGET   # add a key and make it active
```

`TARGET` is a service (`director-0`, `origin-1`, `fed`, ...), `directors`,
`origins`, or `all`. The directors share a key, and so do the origins;
naming just one member is a way to break that on purpose.

- Services pick up a new key within a minute. Other services may keep
  using a cached copy of the old keyset for up to 15 minutes;
  `./fed.sh restart` skips the wait.
- Each service's database backups, and a V2 cache's store, are
  encrypted with that service's keys. Starting a cache without the keys
  its store was sealed to destroys the store. `./reset.sh` discards keys
  and data together.
- An empty key directory doesn't simulate a missing key: Pelican
  generates its own key there.


## Ports

Web UIs log in as `admin` / `asdf`. Certificates are signed by
`certs/ca.crt`.

| URL                      | Service                                                  |
| ------------------------ | -------------------------------------------------------- |
| <https://localhost:8444> | origin-0 (origin-1: 8446)                                |
| <https://localhost:8445> | cache-0 (cache-1: 8447)                                  |
| <https://localhost:9000> | director-0 (director-1: 9004; `tiny`: all)               |
| <https://localhost:9001> | registry                                                 |
| <https://localhost:9005> | discovery (`/.well-known/pelican-configuration`)         |
| <http://localhost:9002>  | dev container; serve on `0.0.0.0:8444` inside            |
| <http://localhost:9003>  | Grafana (`admin` / `admin`; see `presets/monitoring.sh`) |


## Sharp edges

- **Switching variants.** Changing `ORIGIN_VARIANT` or `CACHE_VARIANT`
  reuses the same `data/` directories with a different layout. Run
  `./reset.sh` between them.
- **Changing shape under a running federation.** `up` recreates only
  containers whose definition changed. If `generated/conf/` changed
  (e.g. after dropping `-p ha`), `up` names the stale services; run
  `./fed.sh restart`.
- **Linux hosts.** Containers run as root, so files they write under
  `state/` and `data/` are owned by root. `./reset.sh` removes them
  anyway.
- **Docker Desktop.** Use VirtioFS file sharing. gRPC-FUSE can break
  the V2 cache's database.
- **`~/.gitconfig`.** The dev container mounts it, so `fed.sh` creates
  an empty one if it's missing.
- **`-p xrootd`'s CILogon button** needs a registered client in
  `etc/oidc-client-id` and `etc/oidc-client-secret`. Nothing else
  depends on it.
- **`-p tiny`** ignores `ha`, `multi`, `monitoring`, and `lab`, and
  refuses `ssh`.
