# Pelican Test Framework

A white-box test framework for [Pelican](https://pelicanplatform.org/)
data federations. It runs a federation in Docker, fills it with known
objects, and checks that Pelican does what it should.

Use it to:

- **Run the smoke tests.** `smoke.sh` runs every suite over a matrix of
  federation shapes, each from a clean start, and writes results for
  people and CI ([CI and results](docs/ci.md)).
- **Test your changes.** Point it at your own images, client, or
  configuration ([Testing your changes](#testing-your-changes)).

By default, the federation has one director, one registry, one origin,
and one cache. An nginx host serves the federation's metadata, as the
OSDF does. [Presets](#presets) change this shape.

## Requirements

- Docker Engine 25+ and Docker Compose v2.24+. On Docker Desktop, use
  VirtioFS file sharing.
- `sh`, `curl`, `openssl`, `ssh-keygen`, and `tar`.
- [uv](https://docs.astral.sh/uv/), which provides the scripts' Python.
- About 2.5 GB of memory for the containers, or 3.5 GB with
  `-p topo-multi-origin -p topo-multi-cache`.
- Optional: a Pelican checkout, mounted in the dev container at `/app`.

## Quickstart

```sh
./fed.sh init       # create certificates, binaries, keys, and test data
./fed.sh up         # start the federation
./fed.sh test       # run the tests
./fed.sh down       # stop everything

./smoke.sh          # or: run the tests in every shape (destructive)
```

## Running the tests

`./fed.sh test` runs `test.py` in the
[dev container](docs/glossary.md#dev-container). It runs each suite in
`framework/suites/` that applies to the shape, and skips the rest,
saying why. Each suite runs twice: first presenting WLCG tokens, then
SciTokens ([token profiles](docs/tests/README.md#token-profiles)).
[What the tests check](docs/tests/README.md) describes each suite.

```sh
./fed.sh test -l                        # list the suites and their scenarios
./fed.sh test auth commands             # only these suites
./fed.sh test 'transfers/plugin-*'      # only a suite's scenarios that match
./fed.sh test commands/ls-public        # just one
./fed.sh test --profile wlcg auth       # only its WLCG pass (or scitokens)
```

The run prints each suite's results as it goes, then a summary of the
failures, and exits non-zero if any case failed. Each suite writes a row
per case to `framework/var/results/` ([Results](docs/ci.md#results)). A
watchdog abandons a pass that prints nothing for 20 minutes, or runs for
90, and records it as `aborted`.

### Logs

After every run, pass or fail, `./fed.sh test` saves these in
`framework/var/logs/<time>/`:

| File                                     | Contents                                                                    |
| ---------------------------------------- | --------------------------------------------------------------------------- |
| `<service>.log`                          | the service's log since its container was created                           |
| `shape.txt`                              | presets, topology, profiles, tag, and images                                |
| `ps.txt`, `containers.txt`               | each container's state and exit code, and whether it was killed for memory  |
| `stats.txt`                              | each container's memory use against its limit                               |
| `config/`                                | the configuration that `fed.sh` generated, without secrets                  |
| `results/`                               | the run's [results](docs/ci.md#results)                                     |
| `commands.log`, `commands-scitokens.log` | each pass's client commands, with exit status, output, and the plugin's ads |
| `test.log`                               | everything the tests printed                                                |
| `control.log`                            | what the [control helper](#service-control) did                             |

Tokens in these files are kept without their signatures. The tests
restart services but never recreate them, so each log covers the whole
run; `./fed.sh up` and `restart` start a recreated container's log
afresh.

## Workflow

1. **Pick a shape** with [presets](#presets). `fed.sh` remembers them;
   `-p default` clears them.

   ```
   ./fed.sh -p origin-xrootd -p topo-multi-origin init
   ```

1. **Prepare** with `./fed.sh init`. It creates only what is missing,
   without Docker. Rerun it after changing presets, since the test data
   depends on the shape.

1. **Start** with `./fed.sh up`. It also removes containers that the
   shape doesn't use.

1. **Test** with `./fed.sh test`. Or open a shell in the dev container
   with `./fed.sh dev`, where this checkout is `/scratch` and your
   Pelican checkout is `/app`, and run `python3.13 /scratch/test.py` or
   the client:

   ```
   /scratch/framework/var/bin/linux/pelican object get \
       pelican://discovery:8444/public/data/0.0 /tmp/0.0
   ```

1. **Inspect** with `./fed.sh logs -f origin-0`, `./fed.sh status`, the
   [web UIs](#ports), and [`framework/var/`](#where-things-live).

1. **Change something** ([Testing your changes](#testing-your-changes)),
   then apply it.

1. **Reset** with `./reset.sh`. It stops everything, and discards
   `framework/var/` (but Grafana's database) and the dev container's
   volumes. It leaves `local/` alone.

## Presets

Presets combine, e.g. `./fed.sh -p origin-pstore -p topo-multi-cache up`.
A preset's name begins with what it changes: the topology (`topo-`),
origins (`origin-`), caches (`cache-`), authorization (`auth-`),
directors (`director-`), registry (`registry-`), every server
(`server-`), or a helper service (`with-`). Presets that choose among
alternatives set the same knob, and `fed.sh` refuses two of them
together.

With no presets, the federation has one director, one registry, one
`posixv2` origin, and one V2 cache, and the origins run Pelican's
embedded issuer. The origins export three namespaces, which hold the
same objects:

- `/public`: anyone may read and list it; no one may write it, so it has
  no issuer (but see `origin-public-writes`).
- `/protected-a` and `/protected-b`: reading, listing, and writing each
  take a token. Each has its own issuer and test key.

Each preset is a short file in `framework/presets/`; read it for the
details, including what it refuses. To write your own, see
[Configuration](docs/configuration.md#presets).

**Topology** (`topo-basic`, `topo-tiny`, or `topo-standalone`; the
others change `topo-basic`)

| Preset                  | Effect                                                                                                                                                                                 |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `topo-basic`            | the default: each server in its own container                                                                                                                                          |
| `topo-tiny`             | the whole federation in one process: director, registry, origin, and V2 cache; `posixv2`, `xrootd`, or `pstore` origins only; scenarios that restart one server skip                   |
| `topo-multi-origin`     | adds origin-1, a replica of origin-0: the same prefixes, key, and issuer, over its own store (under `origin-ssh`, the same one)                                                        |
| `topo-multi-cache`      | adds cache-1                                                                                                                                                                           |
| `topo-multi-owner`      | adds origin-2, another owner with its own key and issuer, exporting `/other`, and `/public/other` and `/public/secret` inside `/public` ([another owner](docs/tests/another-owner.md)) |
| `topo-multi-director`   | adds director-1, which shares director-0's key; servers advertise to both, but clients ask director-0 (unless `director-ha`)                                                           |
| `topo-site-local-cache` | adds cache-2, a site-local cache that no director knows of, which reads through a federated cache ([site-local cache](docs/tests/site-local-cache.md))                                 |
| `topo-transfer-server`  | adds `transfer`, a transfer server of its own, tested in place of origin-0's transfer API ([transfer API](docs/tests/transfer-api.md))                                                 |
| `topo-no-cache`         | removes cache-0, so the directors send every read to an origin                                                                                                                         |
| `topo-standalone`       | origin-0 alone, as a standalone origin and its own discovery host; native origins only                                                                                                 |

**Origin storage** (one of)

| Preset           | Effect                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `origin-posixv2` | the default: native `posixv2`                                                                                                              |
| `origin-xrootd`  | XRootD `posix`                                                                                                                             |
| `origin-pstore`  | native `pstore`, the encrypted Pelican store, filled by uploads; `/public` reads `/protected-a`'s objects ([pstore](docs/tests/pstore.md)) |
| `origin-ssh`     | native `ssh`, to the `lab-server` service, logging in with a key; every origin shares its store; no checksums                              |
| `origin-httpsv2` | native `httpsv2`, in front of the `webdav` service (rclone), which takes no credentials; `/protected-a` only; no checksums                 |
| `origin-s3v2`    | native `s3v2`, in front of the `s3` service (rclone), a bucket per origin; MD5 only                                                        |
| `origin-https`   | XRootD `https`, in front of the `webdav` service; `/protected-a` only; no checksums                                                        |
| `origin-s3`      | XRootD `s3`, in front of the `s3` service, a bucket per export; no deletes or checksums                                                    |

**Caches** (one of)

| Preset         | Effect                                                                  |
| -------------- | ----------------------------------------------------------------------- |
| `cache-v2`     | the default: V2 caches                                                  |
| `cache-xrootd` | XRootD caches                                                           |
| `cache-mixed`  | cache-1 is an XRootD cache beside the V2 ones; needs `topo-multi-cache` |

**Cache features**

| Preset         | Effect                                                                                                                                             |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cache-tiered` | V2 caches move objects of 16 MiB or more to the `s3` service, and redirect clients there (cache-1 proxies them) ([tiering](docs/tests/tiering.md)) |
| `cache-memory` | V2 caches keep up to 64 MB of decrypted blocks in memory (`Cache.MemoryCacheSize`)                                                                 |
| `cache-small`  | caches evict at 128 MiB, down to 96 MiB (V2: `Cache.HighWaterMark`; XRootD: `Cache.FilesMaxSize`)                                                  |

**Origin features**

| Preset                    | Effect                                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `origin-posc`             | stages uploads until they complete (POSC); `posixv2` only ([POSC and metadata](docs/tests/posc-and-metadata.md))                                                                                 |
| `origin-atomic-uploads`   | XRootD's POSC (`Origin.EnableAtomicUploads`); `origin-xrootd` only                                                                                                                               |
| `origin-metadata`         | publishes an event, with the uploader's metadata, for each object committed, overwritten, or deleted, to a recorder (eventual mode); `posixv2` only                                              |
| `origin-metadata-tx`      | `origin-metadata` in transactional mode: an upload whose event is not delivered fails, and is rolled back                                                                                        |
| `origin-max-age`          | origins send `max-age=30` (`Origin.CacheControl`); native origins only ([cache freshness](docs/tests/cache-freshness.md))                                                                        |
| `origin-no-direct`        | origins serve only caches (`Origin.DisableDirectClients`); one read-only export: `/protected-a`, or `/public` with `origin-public`; suites that write skip                                       |
| `origin-multiuser`        | origins act as each token's Unix user (`Origin.Multiuser`); `posixv2` and `origin-xrootd` only ([Unix users](docs/tests/unix-users.md))                                                          |
| `origin-broker`           | origin-0 uses director-0 as its connection broker; XRootD origins only; one export: `/protected-a`, or `/public` with `origin-public`                                                            |
| `origin-firewalled`       | `origin-broker`, with origin-0 on a network of its own, which the caches reach only through the broker; XRootD caches only; needs `auth-external-issuer` or `origin-public`; not `topo-no-cache` |
| `origin-ssh-tunnel`       | the origin reaches its helper on the lab server through the SSH connection (`Origin.SSH.TunnelCallback`); `origin-ssh` only                                                                      |
| `origin-transfer-api`     | origins run the transfer API (`Origin.EnableTransferAPI`) ([transfer API](docs/tests/transfer-api.md))                                                                                           |
| `origin-short-tokens`     | the issuer's access tokens and codes last a minute, and refresh tokens four (`Issuer.*Lifetime`) ([issuer](docs/tests/issuer.md))                                                                |
| `origin-export-templates` | `/protected-b` has authorization templates of its own, in place of the global ones                                                                                                               |
| `origin-public`           | origins export only `/public`: no issuer, and no writes; most suites of the data path skip                                                                                                       |
| `origin-public-writes`    | `/public` takes writes with a token (`PublicReads` and `Writes`), so it has an issuer                                                                                                            |

**Authorization**

| Preset                 | Effect                                                                                                                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auth-oidc-local`      | web UI login, and the registry's `--with-identity`, through the framework's identity provider (`idp`), whose users are in `framework/etc/idp-users.json` ([OIDC login](docs/tests/oidc-login.md)) |
| `auth-oidc-cilogon`    | the same through CILogon, with your own client in `local/etc/oidc-client-{id,secret}`, which must accept each server's redirect URI ([Ports](#ports)); `oidc` skips                               |
| `auth-external-issuer` | the exports trust `https://discovery:8444/issuer`, which signs the tests' tokens, in place of the origins' issuers, which are off                                                                 |
| `auth-no-issuer`       | origins run no issuer (`Origin.EnableIssuer`); each namespace's issuer is the origin, whose key signs tokens out of band, as `pelican token create` does                                          |

**Director and registry**

| Preset                    | Effect                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `director-default-origin` | directors send a GET of a bare path to an origin, not a cache (`Director.DefaultResponse`); this affects curl and browsers, not the Pelican client                             |
| `director-discovery`      | director-0 serves the federation's metadata (`Director.EnableFederationMetadataHosting`) in place of the `discovery` service, so the federation is `pelican://director-0:8444` |
| `director-ha`             | adds `director`, a load balancer that passes TLS through to director-0 and director-1, and that the federation names as its director; needs `topo-multi-director`              |
| `registry-approval`       | nothing serves until an administrator approves its registration (`Registry.Require{Origin,Cache}Approval`), which `federation`'s `ready` does                                  |

Under `director-ha`, every director serves the metadata of
`director-discovery`, and the federation is `pelican://director:8444`.

**Every server**

| Preset                | Effect                                                                                |
| --------------------- | ------------------------------------------------------------------------------------- |
| `server-unprivileged` | each server drops root for the `pelican` user (`Server.DropPrivileges`); `keys` skips |

**Helpers**

| Preset         | Effect                                                               |
| -------------- | -------------------------------------------------------------------- |
| `with-grafana` | adds Grafana, with no data source (see the preset); no suite uses it |

`smoke.sh`'s `pelican-defaults` shape runs Pelican's own defaults in
place of the framework's: `origin-xrootd`, `cache-xrootd`,
`auth-no-issuer`, and `director-discovery`.

## Testing your changes

Nothing needs to be released first. Your changes go in `local/`, or, for
the client, in `framework/var/bin/`:

| To test                                              | Put it in                                  | Then                            |
| ---------------------------------------------------- | ------------------------------------------ | ------------------------------- |
| server images                                        | `IMAGE_*` in `local/environment.cfg`       | `./fed.sh up`                   |
| a client binary                                      | `framework/var/bin/linux/pelican`          | run it                          |
| another release                                      | `PELICAN_TAG` in `local/environment.cfg`   | `./fed.sh init`, `up`           |
| config for every service                             | `local/config.d/base/*.yaml`               | `./fed.sh restart`              |
| config for one service                               | `local/config.d/instance/<service>/*.yaml` | `./fed.sh restart <service>`    |
| a [knob](docs/configuration.md#knobs)                | your environment, or a local preset        | `./fed.sh init`, `up`           |
| a [shape of your own](docs/configuration.md#presets) | `local/presets/<name>.sh`                  | `./fed.sh -p <name> init`, `up` |
| CILogon's or another identity provider's client      | `local/etc/oidc-client-{id,secret}`        | `./fed.sh restart`              |
| S3 credentials (`-p origin-s3v2`)                    | `local/etc/s3-{access,secret}-key`         | `./fed.sh restart`              |
| identity provider users (`-p auth-oidc-local`)       | `local/etc/idp-users.json`                 | any `./fed.sh` command          |

Pelican reads its configuration only at startup. `restart` recreates the
containers, so they read it again.

### Server images

Build images from your Pelican checkout. The build compiles the server
and its web UI inside Docker, so the first build is slow.

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

`./fed.sh status` shows the images in use. `up` recreates the containers
whose image changed. `topo-tiny` runs everything from `IMAGE_ORIGIN`.

### Client binary

The tests run `framework/var/bin/linux/pelican`, and `stash_plugin`, a
symlink to it. To test your own client, build it over that file from
the dev container:

```sh
cd /app && CGO_ENABLED=0 go build -tags client -o /scratch/framework/var/bin/linux/pelican ./cmd
```

`init` keeps your binary; only a change of `PELICAN_TAG`, or
`reset.sh`, replaces it.

For server-only commands, such as `origin collection`, the tests run
the dev container's `pelican-server`, if it has one, or else
`framework/var/bin/linux/pelican-server`, which `init` fetches if it is
missing. To test your own, put it there. On a Linux host, that binary
is also the host's, so `init` keeps it only if it reports
`PELICAN_TAG`'s version; otherwise it replaces it and the client.

### Pelican configuration

Each service merges layers of configuration files; later ones win. The
framework's own (`framework/config.d/`) and those that `fed.sh`
generates come first, then yours: `local/config.d/base/` for every
service, then `local/config.d/instance/<service>/`. Within a directory,
files merge in lexicographic order. For example:

```sh
mkdir -p local/config.d/instance/director-0
cat >local/config.d/instance/director-0/90-local.yaml <<'EOF'
Director:
  CacheSortMethod: random
EOF
./fed.sh restart director-0
```

A later layer replaces a list instead of merging it, and a few settings
come from environment variables, which files can't override.
[Configuration](docs/configuration.md) has the details, and the
[knobs](docs/configuration.md#knobs) that `fed.sh` reads.

## Commands

| Command                           | Does                                                            |
| --------------------------------- | --------------------------------------------------------------- |
| `./fed.sh [-p PRESET]... init`    | create what's missing: certs, binaries, keys, data              |
| `./fed.sh up` / `down`            | start / stop everything                                         |
| `./fed.sh restart [SERVICE]...`   | recreate containers so they reread configuration                |
| `./fed.sh test [ARG]...`          | the tests: `test.py` in the dev container                       |
| `./fed.sh control`                | [service control](#service-control) for a `test.py` run by hand |
| `./fed.sh dev [CMD]...`           | a shell (or `CMD`) in the dev container                         |
| `./fed.sh status`                 | containers, presets, and images                                 |
| `./fed.sh [-p PRESET]... check`   | exit 0 if it accepts the presets                                |
| `./fed.sh keys [...]`             | [issuer keys](docs/configuration.md#issuer-keys)                |
| `./fed.sh <other>`                | passed to `docker compose`, e.g. `logs -f cache-0`              |
| `./test.py [SUITE[/SCENARIO]]...` | the tests (in the dev container); `-l` lists them               |
| `./reset.sh`                      | discard everything generated                                    |
| `./smoke.sh [-o DIR] [SHAPE]...`  | the smoke tests, over every shape; destructive                  |
| `framework/report.py`             | merge [results](docs/ci.md#results); JUnit XML; Markdown        |

## Service control

Some tests restart, stop, start, pause, or kill a service mid-run,
restart one with configuration of their own, or freeze the XRootD
daemons in one. The dev container can't control Docker, so
`./fed.sh test` runs a helper on the host (`framework/control.py`) that
does this for them. The tests ask it through files in
`framework/var/control/`. It acts only on the shape's services, never
`dev`, and only with `docker compose`. `smoke.sh` runs the tests through
`./fed.sh test`, so it has the helper too.

To run `test.py` by hand in the dev container, run `./fed.sh control`
on the host beside it, and stop it with Ctrl-C when you're done. Without
a helper, the scenarios that need one skip, and say why.

A test's configuration of a service goes in
`framework/var/control/config/<service>/`, the last
[configuration layer](docs/configuration.md#pelican-configuration). When
the helper stops, it puts back what the tests left: it removes their
configuration, restarting the services that had it, and starts,
unpauses, or thaws what they left stopped, killed, paused, or frozen. A
helper that starts removes any configuration that an earlier one left.

## Where things live

```
fed.sh  reset.sh  smoke.sh  test.py
local/        your customizations (untracked; `git status` shows them)
framework/    the framework itself; don't edit it for local changes
  suites/     the test suites that test.py runs
  testlib/    the suites' shared code, credentials, and scenarios
  report.py   merges results and converts them to JUnit XML and Markdown
  smoke-shapes.tsv
              the shapes that smoke.sh runs (framework/matrix.py writes it)
  var/        everything generated (ignored by Git, emptied by reset.sh)
smoke-logs/   smoke.sh's logs and results (ignored by Git)
```

Everything the framework creates is in plain directories under
`framework/var/`. Stop the federation before editing anything there.

| Path                 | Contents                                                                                                            |
| -------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `state/<service>/`   | database and backups (`/var/lib/pelican`)                                                                           |
| `data/origin/<N>/`   | origin-N's store (`/data`; `/srv/origin-<N>` on the `webdav` and `s3` backends), a directory per namespace          |
| `data/cache/<N>/`    | cache-N's storage (`/data`)                                                                                         |
| `data/lab-server/`   | the lab server's shared directory                                                                                   |
| `data/transfers/`    | the `transfers` suite's batches (`input/`) and results; `transfers-scitokens/` for its SciTokens pass               |
| `data/auth-test/`    | the responses the `auth` suite got; `auth-test-scitokens/` for its SciTokens pass                                   |
| `data/owners-test/`  | the responses the `owners` suite got; `owners-test-scitokens/` for its SciTokens pass                               |
| `data/metadata/`     | every request the metadata recorder received                                                                        |
| `data/webdav-gate/`  | the `Authorization` of each request that an `https` or `httpsv2` origin sent its backend through the gate           |
| `data/idp/`          | every request the identity provider served (`-p auth-oidc-local`), one JSON object per line, without secrets        |
| `data/commands.log`  | the client commands the tests ran, with exit status and output; `commands-scitokens.log` for the SciTokens pass     |
| `results/`           | a row per test case, by suite ([Results](docs/ci.md#results))                                                       |
| `logs/<time>/`       | each `./fed.sh test` run's [logs](#logs)                                                                            |
| `control/`           | the tests' requests to [control services](#service-control), and their configuration of a service (`config/<svc>/`) |
| `issuer-keys/<svc>/` | each service's issuer keys (`/fed/issuer-keys`), and `issuer/`, the external issuer's                               |
| `test-keys/`         | private keys for the [authorization tests](docs/tests/authorization.md)                                             |
| `issuer-jwks/`       | the public keys that the origins add with `Server.IssuerJwks` and each export's `IssuerJwks`                        |
| `generated/`         | files that `fed.sh` derives from the shape and `local/`, e.g. each service's own layer in `instance/<service>/`     |
| `bin/`               | binaries; the dev container uses `linux/`                                                                           |
| `certs/`             | the framework's CA and server certificate                                                                           |
| `grafana/`           | Grafana's database (`reset.sh` keeps it)                                                                            |

## Ports

The web UIs log in as `admin` / `asdf`. `framework/var/certs/ca.crt`
signs the certificates.

| URL                      | Service                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------- |
| <https://localhost:8444> | origin-0 (origin-1: 8446; origin-2: 8448)                                             |
| <https://localhost:8445> | cache-0 (cache-1: 8447; cache-2: 8449)                                                |
| <https://localhost:9000> | director-0 (`topo-tiny`: everything; director-1: 9004)                                |
| <https://localhost:9001> | registry                                                                              |
| <https://localhost:9005> | discovery (`/.well-known/pelican-configuration`), and the external issuer (`/issuer`) |
| <https://localhost:9006> | transfer (`topo-transfer-server`)                                                     |
| <https://localhost:9007> | idp, the identity provider (`auth-oidc-local`); `https://idp:9007` to the servers     |
| <https://localhost:9008> | director, the directors' load balancer (`director-ha`)                                |
| <http://localhost:9002>  | dev container (serve on `0.0.0.0:8444` inside)                                        |
| <http://localhost:9003>  | Grafana (`admin` / `admin`; see `framework/presets/with-grafana.sh`)                  |

Under `-p auth-oidc-local` or `-p auth-oidc-cilogon`, each server's
OIDC redirect URI is
`https://localhost:<port>/api/v1.0/auth/oauth/callback`. To log in
through `idp` from a browser, map `idp` to `127.0.0.1` in your hosts
file, and log in as a user in `framework/etc/idp-users.json` or
`local/etc/idp-users.json`.

## Sharp edges

- **Switching storage.** Another `origin-*` or `cache-*` preset reuses
  `framework/var/data/` with a different layout. Run `./reset.sh`
  between them.
- **Changing shape while running.** `up` recreates only the containers
  whose definition changed. If that leaves a service on its old
  configuration, `up` names it, and the `restart` that fixes it.
- **Linux hosts.** Containers run as root, so the files they write under
  `framework/var/` are root's. `./reset.sh` removes them anyway. Under
  `-p server-unprivileged`, each server's keys and database belong to
  uid 10941, so `./fed.sh keys add` and `rotate` refuse until
  `./reset.sh`.
- **New hosts.** When `framework/etc/tls.req` names a host that the
  server certificate lacks, `./fed.sh init` issues a new certificate
  from the same CA, which running services still trust.
- **`~/.gitconfig`.** The dev container mounts it, so `fed.sh` creates
  an empty one if it's missing.
- **Your Pelican checkout.** The dev image's entrypoint runs
  `pre-commit install` in `/app`, which installs Pelican's Git hook in
  the checkout that `PELICAN_SRC_DIR` names.
- **`-p topo-tiny`** refuses `cache-xrootd`, `auth-external-issuer`,
  `director-discovery`, `origin-broker`, `origin-firewalled`,
  `topo-no-cache`, and every preset that adds a service but
  `origin-metadata`, `origin-metadata-tx`, and `auth-oidc-local`.
- **`-p origin-metadata`** builds the verifier's image from GitHub on
  the first `up`, which needs network access and takes a few minutes.

## More documentation

- [What the tests check](docs/tests/README.md): each suite, the test
  data, and the credentials.
- [Configuration](docs/configuration.md): configuration layers, knobs,
  writing presets, and issuer keys.
- [CI and results](docs/ci.md): running the smoke tests in CI, and the
  results format.
- [Developing the framework](docs/development.md): its layout, what a
  suite is made of, and the checks that keep it consistent.
- [Glossary](docs/glossary.md): the terms these docs use.
