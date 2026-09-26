# Pelican Test Framework

This framework runs a [Pelican](https://pelicanplatform.org/) federation
in Docker on your machine, fills it with known objects, and tests it. It
has two uses:

- **Smoke tests.** `smoke.sh` runs the tests over a matrix of federation
  shapes, which together make every pair of choices that the presets
  offer, from a clean start each time, and writes results that people
  and CI can both read ([CI and results](docs/ci.md)).
- **Testing your changes.** Point it at your own images, client, or
  configuration, and run the tests, or anything else, against them
  ([Testing your changes](#testing-your-changes)).

By default, the federation has one director, one registry, one origin,
and one cache. An nginx host serves the federation metadata, as the OSDF
does. Presets change the shape.


## Requirements

- Docker Engine 25+ and Docker Compose v2.24+. On Docker Desktop, use
  VirtioFS file sharing.
- `sh`, `curl`, `openssl`, `python3` (3.9 or later), `ssh-keygen`, and
  `tar`.
- About 2.5 GB of memory for the containers, or 3.5 GB with
  `-p topo-multi-origin -p topo-multi-cache`.
- Optional: a Pelican checkout, mounted in the dev container at `/app`.


## Quickstart

```sh
./fed.sh init       # create certificates, binaries, keys, and test data
./fed.sh up         # start the federation
./fed.sh test       # run the tests
./fed.sh down       # stop everything

./smoke.sh          # or: the tests in every shape (destructive)
```

`./fed.sh test` runs `test.py` in the dev container, which runs every
check that applies to the federation's shape. The checks are grouped in
suites (`framework/suites/`): `federation`, `commands`, `listings`,
`blocks`, `posc`, `metadata`, `users`, `owners`, `auth`, and
`transfers`. A suite that doesn't apply to the shape is skipped, and
says why.
[What the tests check](docs/tests.md) describes each one.

```sh
./fed.sh test -l                        # list the suites and their scenarios
./fed.sh test auth commands             # only these suites
./fed.sh test 'transfers/plugin-*'      # only a suite's scenarios that match
./fed.sh test commands/ls-public        # just one
```

The run prints each suite's results as it goes, then a summary of every
suite and every case that failed. Each suite writes a row per test case
to `framework/var/results/` ([Results](docs/ci.md#results)), and
`test.py` exits non-zero if any case failed.


## Workflow

1. **Pick a shape** with [presets](#presets). `fed.sh` remembers them for
   later commands, and `-p default` clears them.

       ./fed.sh -p origin-xrootd -p topo-multi-origin init

2. **Prepare** with `./fed.sh init`. It creates only what is missing and
   needs no Docker. Rerun it after changing presets, because the test data
   depends on the shape.
3. **Start** with `./fed.sh up`. It also removes containers that the shape
   doesn't use.
4. **Test.** `./fed.sh test` runs the tests. `./fed.sh dev` opens a shell
   in the dev container, where this checkout is `/scratch` and your
   Pelican checkout is `/app`. Run `/scratch/test.py` there, or use the
   client directly:

       /scratch/framework/var/bin/linux/pelican object get \
           pelican://discovery:8444/public/data/0.0 /tmp/0.0

5. **Inspect** with `./fed.sh logs -f origin-0`, `./fed.sh status`, the
   [web UIs](#ports), and [`framework/var/`](#where-things-live).
6. **Change something** ([below](#testing-your-changes)), then apply it.
7. **Reset** with `./reset.sh`. It stops everything, then discards
   `framework/var/` (except Grafana's database) and the dev container's
   volumes. It doesn't touch `local/`.


## Testing your changes

Nothing needs to be released first. Your changes go in `local/`, or, for
the client, in `framework/var/bin/`:

| To test                           | Put it in                                  | Then                            |
| --------------------------------- | ------------------------------------------ | ------------------------------- |
| server images                     | `IMAGE_*` in `local/environment.cfg`       | `./fed.sh up`                   |
| a client binary                   | `framework/var/bin/linux/pelican`          | run it                          |
| another release                   | `PELICAN_TAG` in `local/environment.cfg`   | `./fed.sh init`, `up`           |
| config for every service          | `local/config.d/base/*.yaml`               | `./fed.sh restart`              |
| config for one service            | `local/config.d/instance/<service>/*.yaml` | `./fed.sh restart <service>`    |
| a [knob](docs/configuration.md#knobs) | your environment, or a local preset        | `./fed.sh init`, `up`           |
| a [shape of your own](docs/configuration.md#presets) | `local/presets/<name>.sh` | `./fed.sh -p <name> init`, `up` |
| an OIDC client (`-p auth-oidc`)   | `local/etc/oidc-client-{id,secret}`        | `./fed.sh restart`              |
| S3 credentials (`-p origin-s3v2`) | `local/etc/s3-{access,secret}-key`         | `./fed.sh restart`              |

Pelican reads its configuration only at startup. `restart` recreates the
containers, so they read it again.

### Server images

To test server code, build images from your Pelican checkout. The build
compiles the server and its web UI inside Docker, so the first build is
slow.

```sh
cd ~/pelican
for t in origin cache director registry; do
  docker build -f images/Dockerfile --target "$t" -t "pelican-$t:local" .
done
```

Then name the images in `local/environment.cfg`:

```sh
IMAGE_ORIGIN=pelican-origin:local
IMAGE_CACHE=pelican-cache:local
IMAGE_DIRECTOR=pelican-director:local
IMAGE_REGISTRY=pelican-registry:local
```

`./fed.sh status` shows which images are in use. `up` recreates the
containers whose image changed. `topo-tiny` runs everything from
`IMAGE_ORIGIN`.

### Client binary

The tests use `framework/var/bin/linux/pelican`. `stash_plugin` is a
symlink to it, which the `transfers` suite's `plugin-*` scenarios run.
To test your own client, build it over that file from the dev
container:

```sh
cd /app && CGO_ENABLED=0 go build -tags forceposix,client -o /scratch/framework/var/bin/linux/pelican ./cmd
```

`init` keeps your binary. Only a change to `PELICAN_TAG`, or `reset.sh`,
replaces it.

### Pelican configuration

Each service merges layers of configuration files, and later ones win.
The framework's own (`framework/config.d/`) and those that `fed.sh`
generates from the shape come first, then yours:
`local/config.d/base/` for every service, then
`local/config.d/instance/<service>/`. Within a directory, files merge in
lexicographic order. For example:

```sh
mkdir -p local/config.d/instance/director-0
cat >local/config.d/instance/director-0/90-local.yaml <<'EOF'
Director:
  CacheSortMethod: random
EOF
./fed.sh restart director-0
```

A later layer replaces a list instead of merging it, and a few settings
come from environment variables that files can't override.
[Configuration](docs/configuration.md) has the full layering, those
rules, and the [knobs](docs/configuration.md#knobs) that `fed.sh` reads.


## Presets

Presets combine, e.g.
`./fed.sh -p origin-pstore -p topo-multi-cache up`. Each name begins
with what it changes: the topology (`topo-`), the origins (`origin-`),
the caches (`cache-`), authorization (`auth-`), every server
(`server-`), or a helper beside the federation (`with-`). Presets that
choose among alternatives, such as an origin's storage, set the same
knob, and `fed.sh` refuses two of them together, even when one of them
names the default.

With none, the federation has one director, registry, `posixv2` origin,
and V2 cache. The origins export three namespaces, which hold the same
objects:

- `/public`: anyone may read and list it, and no one may write it, so it
  has no issuer.
- `/protected-a` and `/protected-b`: reading, listing, and writing each
  take a token. Each has an issuer and a test key of its own.

**Topology** (`topo-basic` or `topo-tiny`; the `topo-multi-*` presets add
to `topo-basic`)

| Preset              | Effect                                                                                                                                            |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `topo-basic`        | the default: each server in a container of its own                                                                                                |
| `topo-tiny`         | the whole federation in one container; refuses `cache-xrootd`, `auth-external-issuer`, and every preset that adds a service but the metadata ones |
| `topo-multi-origin` | adds origin-1, a replica: origin-0's prefixes and key                                                                                             |
| `topo-multi-cache`  | adds cache-1                                                                                                                                      |
| `topo-multi-owner`  | adds origin-2, another owner: `/other`, and `/public/other` inside origin-0's `/public`, with a key and an issuer of its own; see `owners`       |

**Origin storage** (one of)

| Preset           | Effect                                                                           |
| ---------------- | -------------------------------------------------------------------------------- |
| `origin-posixv2` | the default: native `posixv2`                                                    |
| `origin-xrootd`  | XRootD in front of the origins (`posix`)                                         |
| `origin-pstore`  | the encrypted Pelican store                                                      |
| `origin-ssh`     | the `lab-server` container, over SSH                                             |
| `origin-httpsv2` | native `httpsv2`, in front of the `webdav` service (rclone); `/protected-a` only |
| `origin-s3v2`    | native `s3v2`, in front of the `s3` service (rclone)                             |

**Caches** (one of)

| Preset         | Effect                 |
| -------------- | ---------------------- |
| `cache-v2`     | the default: V2 caches |
| `cache-xrootd` | XRootD caches          |

**Origin features**

| Preset               | Effect                                                                                         |
| -------------------- | ---------------------------------------------------------------------------------------------- |
| `origin-posc`        | stages uploads until they complete (POSC); `posixv2` only                                      |
| `origin-metadata`    | publishes object events to a recorder; `posixv2` only                                          |
| `origin-metadata-tx` | `origin-metadata`, in transactional mode                                                       |
| `origin-max-age`     | origins send `max-age=30`, so V2 caches revalidate                                             |
| `origin-no-direct`   | origins serve only caches (`Origin.DisableDirectClients`); `/protected-a` only, and only reads |
| `origin-multiuser`   | the origin reads and writes as each token's user (`Origin.Multiuser`); `posixv2` only          |

**Authorization**

| Preset                 | Effect                                                                            |
| ---------------------- | --------------------------------------------------------------------------------- |
| `auth-oidc`            | web UI login through an external identity provider                                |
| `auth-external-issuer` | the exports trust `https://discovery:8444/issuer` instead of the origins' issuers |

**Every server**

| Preset                | Effect                                                                  |
| --------------------- | ----------------------------------------------------------------------- |
| `server-unprivileged` | each server drops root for the `pelican` user (`Server.DropPrivileges`) |

**Helpers**

| Preset         | Effect                                                   |
| -------------- | -------------------------------------------------------- |
| `with-grafana` | adds Grafana                                             |
| `with-lab`     | adds `lab-server` without making it the origin's storage |

Each preset is a short file in `framework/presets/`, so read it for the
details, including what it refuses. To write your own, see
[Configuration](docs/configuration.md#presets).


## Commands

| Command                             | Does                                               |
| ----------------------------------- | -------------------------------------------------- |
| `./fed.sh [-p PRESET]... init`      | create what's missing: certs, binaries, keys, data |
| `./fed.sh up` / `down`              | start / stop everything                            |
| `./fed.sh restart [SERVICE]...`     | recreate containers so they reread configuration   |
| `./fed.sh test [ARG]...`            | the tests: `test.py` in the dev container          |
| `./fed.sh dev [CMD]...`             | a shell (or `CMD`) in the dev container            |
| `./fed.sh status`                   | containers, presets, and images                    |
| `./fed.sh keys [...]`               | [issuer keys](docs/configuration.md#issuer-keys)   |
| `./fed.sh <other>`                  | passed to `docker compose`, e.g. `logs -f cache-0` |
| `./test.py [SUITE[/SCENARIO]]...`   | the tests (in the dev container); `-l` lists them  |
| `./reset.sh`                        | discard everything generated                       |
| `./smoke.sh [-o DIR] [SHAPE]...`    | the smoke tests, over every shape; destructive     |
| `framework/report.py`               | merge [results](docs/ci.md#results); JUnit XML; Markdown |


## Where things live

```
fed.sh  reset.sh  smoke.sh  test.py
local/        your customizations (untracked; `git status` shows them)
framework/    the framework itself; don't edit it for local changes
  suites/     the test suites that test.py runs
  testlib/    the tests' shared code, and their credentials and scenarios
  report.py   merges results and converts them to JUnit XML and Markdown
  var/        everything generated (ignored by Git, emptied by reset.sh)
smoke-logs/   smoke.sh's logs and results (ignored by Git)
```

Everything that the framework creates is under `framework/var/`. These
are plain directories. Stop the federation before editing anything in
them.

| Path                 | Contents                                                    |
| -------------------- | ----------------------------------------------------------- |
| `state/<service>/`   | database and backups (`/var/lib/pelican`)                   |
| `data/origin/<N>/`   | origin-N's storage (`/data`; `/srv/origin-<N>` on the `webdav` and `s3` backends) |
| `data/cache/<N>/`    | cache-N's storage (`/data`)                                 |
| `data/lab-server/`   | the lab server's shared directory                           |
| `data/transfers/`    | the `transfers` suite's batches (`input/`) and results      |
| `data/auth-test/`    | the responses the `auth` suite got                          |
| `data/owners-test/`  | the responses the `owners` suite got                        |
| `data/metadata/`     | every request the metadata recorder received                |
| `results/`           | a row per test case, by suite ([Results](docs/ci.md#results)) |
| `issuer-keys/<svc>/` | each service's issuer keys (`/fed/issuer-keys`), and `issuer/`, the external issuer's |
| `test-keys/`         | private keys for the [authorization tests](docs/tests.md#authorization) |
| `issuer-jwks/`       | the public keys the origins add with `Server.IssuerJwks` and each export's `IssuerJwks` |
| `generated/`         | files that `fed.sh` derives from the shape and `local/`, e.g. each service's own layer in `instance/<service>/` |
| `bin/`               | binaries; the dev container uses `linux/`                   |
| `certs/`             | the framework's CA and server certificate                   |
| `grafana/`           | Grafana's database (`reset.sh` keeps it)                    |


## Ports

The web UIs log in as `admin` / `asdf`. `framework/var/certs/ca.crt` signs
the certificates. Under `-p auth-oidc`, each server's OIDC redirect URI is
`https://localhost:<port>/api/v1.0/auth/oauth/callback`.

| URL                      | Service                                                      |
| ------------------------ | ------------------------------------------------------------ |
| <https://localhost:8444> | origin-0 (origin-1: 8446; origin-2: 8448)                    |
| <https://localhost:8445> | cache-0 (cache-1: 8447)                                      |
| <https://localhost:9000> | director-0 (`topo-tiny`: everything)                         |
| <https://localhost:9001> | registry                                                     |
| <https://localhost:9005> | discovery (`/.well-known/pelican-configuration`), and the external issuer (`/issuer`) |
| <http://localhost:9002>  | dev container (serve on `0.0.0.0:8444` inside)               |
| <http://localhost:9003>  | Grafana (`admin` / `admin`; see `framework/presets/with-grafana.sh`) |


## Sharp edges

- **Switching storage.** Another `origin-*` or `cache-*` preset reuses
  `framework/var/data/` with a different layout. Run `./reset.sh`
  between them.
- **Changing shape while running.** `up` recreates only the containers
  whose definition changed. If that leaves any services on the old
  configuration, it names them and the `restart` that fixes them.
- **Linux hosts.** Containers run as root, so files they write under
  `framework/var/` are owned by root. `./reset.sh` removes them anyway.
  Under `-p server-unprivileged`, Pelican gives each server's keys and
  database to uid 10941, which can shut you out of them: `./fed.sh keys
  add` and `rotate` then refuse until `./reset.sh`.
- **New hosts.** When `framework/etc/tls.req` names a host that the
  server certificate lacks, `./fed.sh init` issues a new one from the
  same CA, so that running services go on trusting it.
- **`~/.gitconfig`.** The dev container mounts it, so `fed.sh` creates an
  empty one if it's missing.
- **Your Pelican checkout.** The dev image's entrypoint runs
  `pre-commit install` in `/app`, which installs Pelican's Git hook in
  the checkout that `PELICAN_SRC_DIR` names.
- **`-p topo-tiny`** refuses `cache-xrootd`, `auth-external-issuer`, and
  every preset that adds a service but `origin-metadata` and
  `origin-metadata-tx`.
- **`-p origin-metadata`** builds the verifier's image from GitHub on the
  first `up`, which needs network access and takes a few minutes.


## More documentation

- [What the tests check](docs/tests.md): each suite, the test data, the
  credentials, and the unit tests.
- [Configuration](docs/configuration.md): configuration layering, knobs,
  writing presets, and issuer keys.
- [CI and results](docs/ci.md): running the smoke tests in CI, and the
  results format.
