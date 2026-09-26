# Pelican Test Framework

This framework runs a [Pelican](https://pelicanplatform.org/) federation
in Docker on your machine, fills it with known objects, and tests it. It
has two uses:

- **Smoke tests.** `smoke.sh` runs the tests over a matrix of federation
  shapes, from a clean start each time, and writes results that people
  and CI can both read ([Running in CI](#running-in-ci)).
- **Testing your changes.** Point it at your own images, client, or
  configuration, and run the tests, or anything else, against them
  ([Testing your changes](#testing-your-changes)).

By default, the federation has one director, one registry, one origin,
and one cache. An nginx host serves the federation metadata, as the OSDF
does. Presets change the shape.


## Requirements

- Docker Engine 25+ and Docker Compose v2.24+. On Docker Desktop, use
  VirtioFS file sharing.
- `bash`, `curl`, `openssl`, `python3` (3.9 or later), `ssh-keygen`, and
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

`./fed.sh test` runs `test.py` in the dev container. It runs every
basic functionality check that applies to the federation's shape, over
every combination of client, route, namespace, credential, and server
that the shape has. The checks are grouped in suites
(`framework/suites/`), which run in this order:

| Suite        | Tests                                                                 |
| ------------ | --------------------------------------------------------------------- |
| `federation` | that the federation serves at all, and every director names every origin and cache |
| `commands`   | `ls`, `stat`, `delete`, `copy`, `sync`, and `du` ([client commands](#client-commands)) |
| `listings`   | PROPFIND at each origin, director, and cache ([listings](#listings))  |
| `blocks`     | objects and ranges at pstore's and the caches' [block boundaries](#block-boundaries), and overwritten objects |
| `posc`       | interrupted uploads, under `-p origin-posc` or `-p origin-pstore` ([POSC and metadata](#posc-and-metadata)) |
| `metadata`   | metadata events, under `-p origin-metadata` ([POSC and metadata](#posc-and-metadata)) |
| `auth`       | each [credential](#authorization) at each server, in each namespace, and each issuer's keys |
| `transfers`  | gets and puts through `pelican object` and `stash_plugin`, through a cache and direct from an origin, byte-checked ([test data](#test-data)) |

A suite that doesn't apply to the shape is skipped, and says why.
`federation`'s `ready` and `caches` checks run first, whatever else is
asked for; if the federation never serves, nothing else runs. The
`expired` credential must be 70 seconds past its expiry before it is
presented, so `auth` and `transfers` run last, by which time it usually
is.

```sh
./fed.sh test -l                        # list the suites and their scenarios
./fed.sh test auth commands             # only these suites
./fed.sh test 'transfers/plugin-*'      # only a suite's scenarios that match
./fed.sh test commands/ls-public        # just one
```

The run prints each suite's results as it goes, then a summary of every
suite and every case that failed. Each suite writes a row per test case
to `framework/var/results/` ([Results](#results)), and `test.py` exits
non-zero if any case failed. These are basic checks, not stress tests:
each moves only a few small objects.


## Layout

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
| a [knob](#knobs)                  | your environment, or a local preset        | `./fed.sh init`, `up`           |
| a shape of your own               | `local/presets/<name>.sh`                  | `./fed.sh -p <name> init`, `up` |
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

### Knobs

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

### Presets

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

| Preset              | Effect                                                                                                                       | Was              |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `topo-basic`        | the default: each server in a container of its own                                                                           | new              |
| `topo-tiny`         | the whole federation in one container; refuses the presets that add a service, `cache-xrootd`, and `auth-external-issuer`    | `tiny`           |
| `topo-multi-origin` | adds origin-1, a replica: origin-0's prefixes and key                                                                        | `multi`, in part |
| `topo-multi-cache`  | adds cache-1                                                                                                                 | `multi`, in part |
| `topo-multi-owner`  | adds origin-2, another owner: `/other`, and `/public/other` inside origin-0's `/public`, with a key and an issuer of its own | new              |

**Origin storage** (one of)

| Preset           | Effect                                                                           | Was               |
| ---------------- | -------------------------------------------------------------------------------- | ----------------- |
| `origin-posixv2` | the default: native `posixv2`                                                    | new               |
| `origin-xrootd`  | XRootD in front of the origins (`posix`)                                         | `xrootd`, in part |
| `origin-pstore`  | the encrypted Pelican store                                                      | `pstore`          |
| `origin-ssh`     | the `lab-server` container, over SSH                                             | `ssh`             |
| `origin-httpsv2` | native `httpsv2`, in front of the `webdav` service (rclone); `/protected-a` only | new               |
| `origin-s3v2`    | native `s3v2`, in front of the `s3` service (rclone)                             | new               |

**Caches** (one of)

| Preset         | Effect                 | Was               |
| -------------- | ---------------------- | ----------------- |
| `cache-v2`     | the default: V2 caches | new               |
| `cache-xrootd` | XRootD caches          | `xrootd`, in part |

**Origin features**

| Preset               | Effect                                                                                         | Was           |
| -------------------- | ---------------------------------------------------------------------------------------------- | ------------- |
| `origin-posc`        | stages uploads until they complete (POSC); `posixv2` only                                      | `posc`        |
| `origin-metadata`    | publishes object events to a recorder; `posixv2` only                                          | `metadata`    |
| `origin-metadata-tx` | `origin-metadata`, in transactional mode                                                       | `metadata-tx` |
| `origin-max-age`     | origins send `max-age=30`, so V2 caches revalidate                                             | `revalidate`  |
| `origin-no-direct`   | origins serve only caches (`Origin.DisableDirectClients`); `/protected-a` only, and only reads | new           |
| `origin-multiuser`   | the origin reads and writes as each token's user (`Origin.Multiuser`); `posixv2` only          | new           |

**Authorization**

| Preset                 | Effect                                                                            | Was    |
| ---------------------- | --------------------------------------------------------------------------------- | ------ |
| `auth-oidc`            | web UI login through an external identity provider                                | `oidc` |
| `auth-external-issuer` | the exports trust `https://discovery:8444/issuer` instead of the origins' issuers | new    |

**Every server**

| Preset                | Effect                                                                  | Was |
| --------------------- | ----------------------------------------------------------------------- | --- |
| `server-unprivileged` | each server drops root for the `pelican` user (`Server.DropPrivileges`) | new |

**Helpers**

| Preset         | Effect                                                   | Was          |
| -------------- | -------------------------------------------------------- | ------------ |
| `with-grafana` | adds Grafana                                             | `monitoring` |
| `with-lab`     | adds `lab-server` without making it the origin's storage | `lab`        |

Each preset is a short file in `framework/presets/`, so read it for the
details, including what it refuses. To add your own, write
`local/presets/<name>.sh` with a new name. It can set any knob in
`framework/presets/default.sh`, or call `fed_enable_profile` to start
optional services.

- **Old names.** Presets that `fed.sh` remembers under an old name are
  renamed, with a warning. A remembered `ha` or `public-ro`, whose
  presets are gone, is dropped: `public-ro`'s read-only `/public` is now
  the default. An old name given with `-p` is refused, with its new
  names.
- **New presets.** Apart from the defaults, the tests don't know about
  the new presets yet, so they may fail or skip under them: they test
  only the origins in `framework/var/generated/origins` (not origin-2).
  `smoke.sh` runs none of them.
- **The connection broker** has no preset. At v26.0.0-rc.0 its data
  path isn't wired: a native origin forwards brokered data requests to
  the XRootD port, where nothing listens; neither kind of cache asks for
  brokered connections; brokering from the director to an origin in
  another container looks likely to fail, since the director signs the
  reverse token with its own key and the broker checks the origin's; and
  the origin must have exactly one export.


## Running in CI

Pelican's own CI can run these tests; this repository ships no workflow
of its own. A job needs a Linux host that meets the
[requirements](#requirements), and network access to GitHub and the
image registries.

- **What to test** is given the usual way: `PELICAN_TAG` names the
  release whose binaries, metadata verifier, and default images are
  used; `IMAGE_ORIGIN`, `IMAGE_CACHE`, `IMAGE_DIRECTOR`, and
  `IMAGE_REGISTRY` name images the job built and loaded into the local
  daemon. To test a client the job built, run `./fed.sh init` once, then
  copy the client over `framework/var/bin/linux/pelican`; `smoke.sh`
  keeps `framework/var/bin/` across its resets.
- **What to run:** `./smoke.sh -o DIR SHAPE...`. `./smoke.sh --names`
  lists the shapes one per line, for a matrix of one shape per job.
- **What comes back:** the exit status (non-zero if any shape failed),
  and, in `DIR`, each shape's log, its results in `results/<shape>/`,
  and, merged over the shapes, `results.tsv`, `junit.xml` (for a test
  reporter), and `summary.md` (for `$GITHUB_STEP_SUMMARY`). See
  [Results](#results).

For example, as steps of a job in a matrix over the shapes:

```yaml
- run: ./fed.sh init
- run: ./smoke.sh -o smoke-results ${{ matrix.shape }}
  env:
    IMAGE_ORIGIN: pelican-origin:ci
    IMAGE_CACHE: pelican-cache:ci
    IMAGE_DIRECTOR: pelican-director:ci
    IMAGE_REGISTRY: pelican-registry:ci
- if: always()
  run: cat smoke-results/summary.md >>"$GITHUB_STEP_SUMMARY"
- if: always()
  uses: actions/upload-artifact@v4
  with:
    name: smoke-${{ matrix.shape }}
    path: smoke-results/
```


## Reference

### Commands

| Command                             | Does                                               |
| ----------------------------------- | -------------------------------------------------- |
| `./fed.sh [-p PRESET]... init`      | create what's missing: certs, binaries, keys, data |
| `./fed.sh up` / `down`              | start / stop everything                            |
| `./fed.sh restart [SERVICE]...`     | recreate containers so they reread configuration   |
| `./fed.sh test [ARG]...`            | the tests: `test.py` in the dev container          |
| `./fed.sh dev [CMD]...`             | a shell (or `CMD`) in the dev container            |
| `./fed.sh status`                   | containers, presets, and images                    |
| `./fed.sh keys [...]`               | [issuer keys](#issuer-keys)                        |
| `./fed.sh <other>`                  | passed to `docker compose`, e.g. `logs -f cache-0` |
| `./test.py [SUITE[/SCENARIO]]...`   | the tests (in the dev container); `-l` lists them  |
| `./reset.sh`                        | discard everything generated                       |
| `./smoke.sh [-o DIR] [SHAPE]...`    | the smoke tests, over every shape; destructive     |
| `framework/report.py`               | merge [results](#results); JUnit XML; Markdown     |

### Where things live

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
| `data/metadata/`     | every request the metadata recorder received                |
| `results/`           | a row per test case, by suite ([Results](#results))         |
| `issuer-keys/<svc>/` | each service's issuer keys (`/fed/issuer-keys`), and `issuer/`, the external issuer's |
| `test-keys/`         | private keys for the [authorization tests](#authorization)  |
| `issuer-jwks/`       | the public keys the origins add with `Server.IssuerJwks` and each export's `IssuerJwks` |
| `generated/`         | files that `fed.sh` derives from the shape and `local/`, e.g. each service's own layer in `instance/<service>/` |
| `bin/`               | binaries; the dev container uses `linux/`                   |
| `certs/`             | the framework's CA and server certificate                   |
| `grafana/`           | Grafana's database (`reset.sh` keeps it)                    |

### Results

Each suite writes `framework/var/results/<suite>.tsv`, one row per test
case, with a header row:

```
suite   case   status   seconds   note
```

`status` is `PASS`, `FAIL`, `SKIP`, or `INCONCLUSIVE`. A note's tabs and
newlines are written as `\t` and `\n`, so each row is one line. The
suite is the suite's name, with `-<tag>` after it if `RESULTS_TAG` names
a tag, as `smoke.sh` does to tell apart two runs of one suite. A suite
that stops early records why, as a `FAIL` row named `setup` or
`aborted`. A run of everything first removes earlier runs' results.
`transfers` has a row per scenario, and `auth` one per request and per
document its `keys` check fetches. To read one:

```sh
column -t -s "$(printf '\t')" framework/var/results/transfers.tsv
```

`smoke.sh` records its own steps as the suite `smoke`, keeps each
shape's results in `DIR/results/<shape>/`, and merges them with
`framework/report.py`:

```sh
framework/report.py merge DIR/results/*/ >results.tsv   # adds a shape column
framework/report.py junit results.tsv >junit.xml
framework/report.py markdown results.tsv >summary.md
```

### Test data

`init` writes 16 objects named `<origin>.<n>` to the origins' storage.
The `transfers` suite makes its batches each time it runs, in
`framework/var/data/transfers/input/<scenario>/<index>`. A batch is one
client invocation. `<ns>` is each namespace, and `<cred>` each of the
eleven [credentials](#authorization) that applies to it:

| Scenario          | Transfers                                                 |
| ----------------- | --------------------------------------------------------- |
| `exist`           | get `/public/data/0.<n>`, with no token                   |
| `dne`             | get `/public/data/9.<n>` (never created): not found       |
| `get-<ns>-<cred>` | get `/<ns>/data/0.<n>`, presenting `<cred>` (`get-public-none` is `exist`) |
| `put-<ns>-<cred>` | put `/<ns>/data/put/put-<ns>-<cred>/<x>.<z>`, presenting `<cred>` |

These run through `pelican object get` and `put`. Each get also has a
direct twin, `direct-<scenario>`, which asks the director for an origin
instead of a cache (`pelican object get --direct`). Every scenario also
has a plugin twin, `plugin-<scenario>` (e.g. `plugin-direct-exist`),
which runs the same batches through `stash_plugin`. The plugin has no
flag for a direct read, so its direct batches add `?directread` to each
URL. Only the plugin's uploads go elsewhere, to
`put/plugin-put-<ns>-<cred>/`.

What each scenario should do follows the
[authorization](#authorization) rules: every get of `/public` should
pass, and every put to it should be refused. A shape that exports one
namespace (`origin-httpsv2`, `origin-no-direct`) has only that
namespace's scenarios. `./fed.sh test -l transfers` lists the
scenarios, and the summary after a run shows what each was expected to
do.

`exist`, `dne`, and each scenario that a token should let through run
two batches, of one object and then two (a put of two goes to a
collection rather than to an object's URL); the rest run one, of one
object. In the default shape that is 178 scenarios and 222 client
invocations, 16 at a time. Each batch picks distinct objects, uniformly
at random. `framework/testlib/transfers.py` sets these numbers.

The suite mints the tokens and writes new random bytes for each upload.
Afterward, it judges each batch by the rules in
`framework/testlib/transfers.py`. Every file downloaded must match the
object in the origin's store, and every upload what the store received.
Every batch that should be refused must fail by being refused, with
nothing moved: no object's bytes in a file it downloaded, and no upload
in any store. Every `dne` batch must fail with not found (error 5011).
The summary counts each scenario's batches that passed, failed, and
went unexpectedly, says why the failed ones failed, and names the batch
sizes that had unexpected results. `verify/problems` lists each
unexpected result.

Last, it shows which servers served the successful downloads, by client
and route. Outside `topo-tiny`, some cache must have served the downloads
meant for one, since a director that routed around its caches would
otherwise pass every scenario; and only an origin may serve a direct
one.

The scenario and credential tables, the rules that judge each batch,
and the suites' tables have unit tests that need no federation:

```sh
python3 -B -m unittest discover -s framework -t framework
```

A `pstore` origin's store is encrypted, and only the running origin can
write it. So `init` writes a plain copy of the objects beside it, in
`data/origin/<N>/data/`, and the `transfers` suite uploads whatever the
store lacks, to `/protected-a` and `/protected-b`, before its gets. Each
has storage of its own in the store, and `/public`, which takes no
writes, reads `/protected-a`'s. Uploads to it are read back from the
origin instead of from disk.

### Authorization

The `transfers` and `auth` suites present the same credentials, defined
in one table in `framework/testlib/credentials.py`. Each is for
one namespace. `/public` has no issuer, and so no key of its own, so it
gets only the credentials that need neither its own key or issuer nor
another namespace's. The "other namespace" of `/protected-a` is
`/protected-b`, and the reverse:

| Credential   | Token                                                                  |
| ------------ | ---------------------------------------------------------------------- |
| `server`     | signed by the origins' issuer key                                      |
| `jwks`       | signed by `test-keys/jwks.pem`, whose public key the origins list only in `Server.IssuerJwks` |
| `ns-jwks`    | signed by `test-keys/ns-<ns>.pem`, whose public key the origins list only in the `IssuerJwks` of the `/<ns>` export |
| `none`       | none                                                                   |
| `unknown`    | signed by `test-keys/unknown.pem`, which no server knows               |
| `ns-other`   | signed by the other namespace's `ns-<ns>.pem`                          |
| `wrong-op`   | the origins' key, but read-only for a put, or create and modify for a get |
| `wrong-path` | the origins' key, scoped to `/elsewhere/`                              |
| `wrong-iss`  | the origins' key, but the other namespace's issuer                     |
| `ns-cross`   | the other namespace's `ns-<ns>.pem` and issuer: a token only it accepts |
| `expired`    | the origins' key, expired                                              |

Otherwise each token has read, create, and modify on the whole namespace,
is issued by that namespace's issuer, and lasts four hours, so that each
tests one thing (`ns-cross` is `wrong-iss` signed with the other
namespace's key). With `ORIGIN_ENABLE_ISSUER=false`, every namespace has
one issuer and no keys of its own, so the tests skip `wrong-iss`,
`ns-jwks`, `ns-other`, and `ns-cross`, as they do with
`auth-external-issuer`, whose key then signs `server`, `wrong-op`,
`wrong-path`, and `expired`. A shape that exports only `/protected-a`
(`origin-httpsv2`, `origin-no-direct`) lists `/protected-b`'s key
nowhere, so the tests skip `ns-other` and `ns-cross`, which would only
repeat `unknown`. Where a token decides, only `server`, `jwks`, and
`ns-jwks` should be allowed. A token doesn't always decide:

- **Reads of `/public`** should be allowed whatever the token. The
  servers ignore it, and the clients don't even send one.
- **Writes to a namespace without `Writes`** should be refused whatever
  the token. That is `/public`, and every namespace under
  `origin-no-direct`. `/public` has no issuer at all, so a token claiming
  its issuer URL is trusted by nothing.

The director rejects any expired token it is shown, whatever the
namespace.

- **Through the clients**, the `transfers` suite runs the `get-` and
  `put-` scenarios with `pelican object`, and their `plugin-` twins with
  `stash_plugin`. Its `failures` column says who refused: `client` (it
  had no token to send), `director` (it rejects expired tokens),
  `unsupported` (the director found no origin that allows the operation
  in the namespace, e.g. a put to `/public`), or
  `server` (an origin or cache answered 401 or 403). Any other failure
  is unexpected, since it doesn't show a refusal. A `pelican object get`
  whose token is refused is `server`, even when the token has expired:
  the client stats each object first, and asks the director without the
  token. With no token, `pelican object` tries to acquire one, which
  should fail at once with error 4010, since the test gives it no
  terminal and an empty credential store.
- **At each server**, the `auth` suite sends GET, HEAD, PUT, and DELETE
  to every origin, and GET and HEAD to every cache, over HTTPS, in every
  namespace. An allowed request must return or store the right bytes,
  or remove the object, which the test uploads just before each DELETE.
  A refused one must get 401 or 403 and change nothing. A native origin
  answers 401, and a native cache 403. The caches are tried only after
  `server` has stored the test object in them.
- **At the issuer**, the `auth` suite's `keys` check comes first. For
  `/protected-a` and `/protected-b`, the discovery document's `jwks_uri`
  must be the namespace's own JWKS. That JWKS must hold every key in the
  origin's server-wide JWKS (`/.well-known/issuer.jwks`), plus the
  namespace's own `ns-<ns>.pem`, and nothing else. The server-wide JWKS
  must hold `jwks.pem`'s key, and neither namespace's. `./fed.sh test
  auth/keys` runs only this check.

- **After a server has seen a token**, the `auth` suite's `narrow` check
  presents one like `server`'s but scoped to the test object alone, at
  every origin and cache in `/protected-a`. A GET of the object must
  pass, and then one of its sibling, whose name begins with the object's,
  must be refused.

Both suites wait until the `expired` token is 70 seconds past its expiry
before presenting it, since native servers allow 60 seconds of clock
skew. `test.py` mints it as it starts, and runs these suites last, so
the wait has usually passed by then.

`init` creates the four test keys in `framework/var/test-keys/`, and
the public keys of `jwks.pem`, `ns-protected-a.pem`, and
`ns-protected-b.pem` in `framework/var/issuer-jwks/`, which the origins
mount. They name
`jwks.pem`'s in `Server.IssuerJwks`
(`framework/config.d/instance/origin-*/`), and each `ns-<ns>.pem`'s in
the `IssuerJwks` of the `/<ns>` export, which `fed.sh` generates. No
server sees any of the private keys. Under `-p topo-multi-origin`,
origin-1 warns that its keys authorize nothing, because both origins
advertise origin-0's issuer.

### Client commands

The `commands` suite tests the client's commands other than `get` and
`put`: `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in `/public`
and `/protected-a` where they apply. Writes to `/public` must be
refused. `./fed.sh test -l commands` lists the scenarios.

- The read-only commands work on a small tree that the test uploads
  straight to every origin, under `/<ns>/data/cmd/<run>/tree/`, so that
  they agree whichever origin the director picks. Listings, sizes,
  checksums (`crc32c` and `md5`; an `ssh` origin sends none), and `du`'s
  totals must match it.
- `delete`, `copy`, and `sync` go through the director like any client.
  Under `topo-multi-origin` their effects land on one origin, so a success
  must show in some store, and a refusal in none. A copy between two
  federation URLs is a third-party copy, which the destination origin pulls.
  Every service here has a private address, which origins refuse to pull
  from by default, so `framework/config.d/base/40-origin.yaml` allows it.
- `sync` compares sizes only and never deletes; the scenarios check what
  it uploads by its `--dry-run` output.
- These commands exit 0, 1, or 11 whatever went wrong, so failures are
  judged by what they print, as the `transfers` suite judges them.

### Block boundaries

The `blocks` suite writes objects of sizes on either side of the block
boundaries of pstore (an origin's encrypted store), the V2 cache, and
the XRootD cache, then reads them back from each origin, through each
cache, and through the client, whole and in byte ranges that straddle
the boundaries. `framework/testlib/blocks.py` lists the sizes and ranges,
and the facts about Pelican's stores behind them: 4,080-byte blocks,
pstore's inline, buffered, and streamed tiers, and the XRootD cache's
128 KiB blocks.

- Each object goes straight to every origin, with a `Content-Length` or
  in chunks with none, which is how the client uploads. On a `pstore`
  origin, `/metrics` must show each upload in the tier its size predicts.
- How a V2 cache first sees an object decides how it fills (one stream,
  or block by block), so each cache reads one set of objects whole first
  and another in ranges first.
- `mixed-version` overwrites an object at the origins after a cache has
  cached part of it; each response must then be entirely one version.
  The V2 cache fills a range miss without checking the version, so this
  may fail. A cache that answers with an error instead is inconclusive.
- `cache-overlap` reads ranges from objects of each cache's own, each
  plan in turn, so that every read after the first covers both blocks
  the cache fetched for an earlier one and blocks it must fetch now, at
  both block sizes; then it reads one more object in overlapping ranges
  all at once. The whole object must come back intact after each.
- `cache-assemble` reads unaligned, overlapping ranges that together
  cover an object, out of order. The cache must hold part of it after
  the first and all of it after the last: a V2 cache sends `Age` only
  once it holds every block, and an XRootD cache's `.cinfo` (read from
  `data/cache/<N>/`) records which blocks it holds. When a cache cannot
  show the partial state, the row is inconclusive.
- `overwrite-cached` and `overwrite-range-first` overwrite objects at the
  origins once each cache holds them (read whole, or a range first), one
  keeping its size and one growing. Every response, whole or ranged, must
  then be entirely one version, with that version's size. Pelican treats
  objects as immutable: an XRootD cache never revalidates, and a V2 cache
  does only once an object is stale, which by default takes about a day.
  Under `-p origin-max-age` (in `smoke.sh`'s `extras` shape), the origins
  send `Cache-Control: max-age=30`, and each V2 cache must then serve the
  new version, and only it. Only a `posixv2` origin sends it, and the
  scenarios fail on an origin that doesn't. They wait up to about 75
  seconds for it.

### Listings

The `listings` suite sends PROPFIND straight to every origin, director,
and cache, at depths 0, 1, and infinity, in every namespace, over a
small tree that it uploads under `/<ns>/data/listings/<run>/`.
`./fed.sh test -l listings` lists the scenarios.

- An origin must list the tree exactly. It may refuse Depth infinity
  with 403, as RFC 4918 allows, but must not answer it as Depth 1.
- A director must redirect a PROPFIND to an origin, whose listing must
  then be right.
- A V2 cache relays Depth 1 through the director to an origin, and must
  list what the origin does. It answers Depth 0 of an object it holds
  from its own records. `cache-body` sends the propfind body that
  `pelican object ls` sends, since the cache may then hand back the
  director's redirect instead of following it.
- An XRootD cache answers 409 to a PROPFIND of a collection, which the
  client knows (`client/handle_http.go`): those rows skip.
- Listing `/protected-a` with no token, or one scoped elsewhere, must be
  refused everywhere; listing `/public` with none must work.
- `new-object` checks that an object shows up at once, by PROPFIND and
  with `pelican object ls`, and is gone once removed.

### POSC and metadata

Both are features of the `posixv2` origin, which is the default. `fed.sh`
refuses them with any other origin. The `posc` and `metadata` suites
run only where they apply, and skip otherwise; `./fed.sh test -l posc
metadata` lists their scenarios.

- **POSC** (`-p origin-posc`): the origin stages each upload in
  `<store>/.pelican-posc/` and renames it into place once it completes.
  The `posc` suite interrupts uploads in several ways and checks what the
  origin answers and what the store shows.
- **pstore** (`-p origin-pstore`) commits a new version of an object only
  when its upload completes, so the `posc` suite runs there too: the same
  interruptions, judged by what the origins serve. `stalled` and
  `hidden`, which are about POSC's staging, skip.
- **Metadata** (`-p origin-metadata`): the origins send an event for every
  object committed, overwritten, or deleted to the `metadata` recorder. It
  saves each request in `framework/var/data/metadata/` (listed in
  `index.tsv`) and passes it to `metadata-verifier`: Pelican's
  `sample_metadata_server`, built from `PELICAN_TAG`'s source. The recorder
  answers 503 once for objects named `fail-once-*`, and 422 for `reject-*`.
  The `metadata` suite uploads with `--metadata-file` and
  `--metadata-body`, and checks the events.
- **Transactional mode** (`-p origin-metadata-tx`): an upload should fail
  with a 5xx if its event can't be delivered, and its object should be
  removed, as Pelican's `docs/metadata-publish-design.md` says.
  The `metadata` suite's `retry` and `reject` check both.

### Issuer keys

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
owner, has a key of its own.

- Services pick up a new key within a minute, but origins and caches may
  keep a keyset they fetched, even their own, for up to 15 minutes.
  `./fed.sh restart` skips the wait.
- Database backups and a V2 cache's store are encrypted with the
  service's keys. A cache started without the keys that sealed its store
  replaces the store's master key, which leaves the store unreadable for
  good, and then refuses to start until the store is removed.
- An empty key directory doesn't simulate a missing key, because Pelican
  generates its own.
- `keys` also lists the four [test keys](#authorization), which `keys
  init` creates. They are never rotated.

### Ports

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

### Sharp edges

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
- **`-p topo-tiny`** refuses every preset that adds a service but the
  metadata receiver, and `cache-xrootd` and `auth-external-issuer`.
- **`-p origin-metadata`** builds the verifier's image from GitHub on the
  first `up`, which needs network access and takes a few minutes.
