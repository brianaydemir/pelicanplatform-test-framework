# Developing the Framework

For changing the framework itself: where its parts are, what a
[suite](glossary.md#suite) is made of, and the checks that keep it
consistent. Each module's docstring is its reference; this page says where
to look.

The Python uses the standard library alone, and runs under the version that
[`pyproject.toml`](../pyproject.toml) requires, which the checks assume. The
scripts on the host ([`control.py`](../framework/control.py),
[`report.py`](../framework/report.py),
[`matrix.py`](../framework/matrix.py),
[`init-data.py`](../framework/init-data.py),
[`check-mounts.py`](../framework/check-mounts.py), and
[`put-file.py`](../framework/put-file.py)) get it from
[uv](https://docs.astral.sh/uv/), by their `#!` lines and
[`fed.sh`](../fed.sh). The tests get it from the
[dev container](glossary.md#dev-container), which has no uv, so
[`test.py`](../test.py)'s `#!` line names it.

## Layout

| Path                                                                                                                                                      | Holds                                                                                                                            |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `fed.sh`                                                                                                                                                  | the [shape](glossary.md#shape): knobs, presets, generated configuration, and Docker Compose                                      |
| `test.py`                                                                                                                                                 | the runner: selects suites and scenarios, runs them under a watchdog, and sums up                                                |
| [`framework/suites/`](../framework/suites/)                                                                                                               | the suites; `__init__.py` gives their contract, and the order they run in                                                        |
| [`framework/testlib/`](../framework/testlib/)                                                                                                             | what the suites share; `__init__.py` indexes its modules                                                                         |
| `framework/control.py`                                                                                                                                    | the helper on the host that restarts, stops, and pauses services for the tests ([service control](../README.md#service-control)) |
| `framework/report.py`                                                                                                                                     | merges results and converts them to JUnit XML and Markdown ([Results](ci.md#results))                                            |
| `framework/matrix.py`                                                                                                                                     | the choices the presets make, and the pairs of them that the smoke shapes don't make ([the smoke matrix](#the-smoke-matrix))     |
| [`framework/smoke-shapes.tsv`](../framework/smoke-shapes.tsv)                                                                                             | the shapes that `smoke.sh` runs, picked by hand                                                                                  |
| `framework/init-data.py`, [`framework/init-certs.sh`](../framework/init-certs.sh)                                                                         | the test objects and the certificates, which `./fed.sh init` makes                                                               |
| `framework/check-mounts.py`, `framework/put-file.py`                                                                                                      | what keeps the containers from changing what the host runs ([Isolation](#isolation))                                             |
| [`framework/presets/`](../framework/presets/)                                                                                                             | one file per [preset](glossary.md#preset); `default.sh` declares every [knob](glossary.md#knob)                                  |
| [`framework/config.d/`](../framework/config.d/)                                                                                                           | Pelican configuration, by layer ([Configuration](configuration.md#pelican-configuration))                                        |
| [`framework/etc/`](../framework/etc/)                                                                                                                     | static files the services mount, among them `pelican.yaml`, which names the layers                                               |
| [`framework/idp/`](../framework/idp/), [`framework/metadata-server/`](../framework/metadata-server/), [`framework/lab-server/`](../framework/lab-server/) | the [helper services](glossary.md#helper-service): the identity provider, the metadata recorder, and the SSH host                |
| [`framework/docker-compose.yaml`](../framework/docker-compose.yaml), [`framework/docker-compose.tiny.yaml`](../framework/docker-compose.tiny.yaml)        | the services, by [topology](glossary.md#topology)                                                                                |

## Suites

A suite is a module in `framework/suites/` with:

- `SCENARIOS`: its [scenarios](glossary.md#scenario)' names and
  descriptions, in the order they run;
- `skip(fed)`: why it can test nothing in a shape, or `None`;
- `run(session, selected, results)`;
- optionally, `REQUIRED` scenarios, which run whatever `test.py` is asked
  for, and `ONE_PROFILE` scenarios, which run in the WLCG pass alone.

A suite imports only `testlib`, never another suite. The docstring of
[`framework/suites/__init__.py`](../framework/suites/__init__.py) gives the
contract in full, and the order in which the suites run; a new suite goes in
its `SUITES`.

A suite's `Test` class subclasses
[`testlib.suite.Test`](../framework/testlib/suite.py), which gives it its
collection, `data/<area>/<run>`, its tokens, and helpers to put, read, and
settle. The area is the suite's name in `SUITES`; `./fed.sh init` makes it
in each namespace (`SUITE_DIRS` in `init-data.py`).

### Token profiles

`test.py` runs each suite once per
[token profile](tests/README.md#token-profiles), each time with a session of
that profile:

- `session.token()`, the [credential](glossary.md#credential) table, and
  `credentials.mint()` give tokens of the session's profile, for what a
  scenario presents.
- `session.setup_token()` gives a WLCG
  [setup token](glossary.md#setup-token) in either [pass](glossary.md#pass),
  for putting objects in place and reading the stores.
- A scenario that presents no storage token of the tests' own minting, or
  makes a check no other profile can, is `ONE_PROFILE`.
- A suite that keeps files under `framework/var/data/` keeps each pass's
  apart, with `session.var_dir()`.

### Stalls and cleanup

The [watchdog](glossary.md#watchdog) abandons a pass that stalls by raising
an exception in the suite, wherever it is. So a suite:

- gives every client command and request a timeout;
- runs tasks at once on
  [`testlib.common.pool()`](../framework/testlib/common.py), which drops its
  queue when its block is left, not on a `ThreadPoolExecutor` of its own;
- puts back what it changed in `finally` blocks;
- waits for the federation after a restart with `Test.settle()` or
  `testlib.control.settle()`, which, like `control.wait_serving()`, stops at
  once if a Pelican server has exited on its own;
- runs the clients with `session.pelican_cmd()` and `session.plugin_cmd()`
  or `plugin_run()`, which kill a run that outlasts its timeout, and log
  every run in the pass's `commands.log`.

### Results

A suite records a row per [case](glossary.md#case)
([Results](ci.md#results)), named `<scenario>` or `<scenario> <case>`,
through [`testlib.report`](../framework/testlib/report.py).
`testlib.common.outcome()` gives a row's verdict: a FAIL naming the first
problem, with `first()`, which prints them all; or a PASS. A row the suite
could not judge is `INCONCLUSIVE`, with a note saying why, never a pass.
[`testlib.suite`](../framework/testlib/suite.py)'s `attempt()`,
`attempt_all()`, and `attempt_cases()` run a scenario so that one that
raises fails, and the rest still run; one that cannot set itself up raises
`suite.SetupError`, whose message is the row's note. `suite.record()`
records a scenario's rows.

Each suite's page under [What the tests check](tests/README.md) describes
every scenario, and the suite's docstring points to it. A change to a suite
is a change to its page.

## Service control from a test

[`framework/testlib/control.py`](../framework/testlib/control.py) has a
function for each action the [helper](glossary.md#service-control-helper)
takes: `restart`, `stop`, `start`, `pause`, `unpause`, `kill`, and
`freeze_xrootd` and `thaw_xrootd`, which stop and resume the
[XRootD](https://xrootd.org/) daemons in a container. Each returns once the
action is done, and, for a restart, start, or unpause, once the server
answers again. It also has:

- `state()`: the state of a service's container, for noticing a server that
  exited on its own;
- `sent_bytes()` and `origins_sent()`: what containers have sent on their
  networks, by the kernel's count
  ([origins' bytes](glossary.md#origins-bytes)), for checking that a cache
  fetched nothing, independently of the servers' own counters;
- `restart_with()`: configuration for a service for the length of a `with`
  block;
- `unavailable()`: why the tests can't control services here, if they can't;
- `unrestartable()`: the same, or, under
  [`topo-tiny`](../framework/presets/topo-tiny.sh), that an origin can't
  restart alone.

Its docstring describes the files under `framework/var/control/` by which
the tests and the helper talk. A scenario puts back whatever it stops,
pauses, kills, freezes, or configures, even if it fails.

## The smoke matrix

[`smoke.sh`](../smoke.sh) runs the shapes in `framework/smoke-shapes.tsv`,
which are picked by hand. A shape makes a choice for each of its presets,
and another if it rotates every key. What the shapes test together is
measured by the pairs of choices they make. The suites aim to be
exhaustive; the shapes need not make every pair.

`framework/matrix.py` holds the rules in its tables:

- `PRESETS`: the choice each preset makes;
- `REFUSED`: the combinations that `fed.sh` refuses, which must agree with
  its checks, each rule refusing some shape that no other does;
- `SMOKE_LIMITS`: the shapes that `smoke.sh` does not run, since they would
  test nothing, or nothing that other shapes don't
  ([smoke limits](#refusals-skips-and-smoke-limits)).

A pair of choices is possible if some shape that `fed.sh` accepts, and that
no smoke limit rules out, makes it with at most `SEARCH_DEPTH` (3) other
choices away from their defaults. `framework/matrix.py pairs` lists them.

`framework/matrix.py check` reads the shapes from `smoke.sh --table`, and
lists each preset that no shape runs and each possible pair that no shape
makes. It takes about half a minute. It exits 1, saying why, if a shape
names an unknown preset, or is one that `fed.sh` would refuse or a smoke
limit rules out; if a preset is missing from the tables; or if `REFUSED`
and `fed.sh` disagree: `./fed.sh check` must refuse, for each rule, the
simplest shape that the rule alone refuses, and accept each shape that
`smoke.sh` is to run.

So a new preset goes in `framework/presets/`, the tables in
`framework/matrix.py`, and the
[README's preset tables](../README.md#presets); then run
`framework/matrix.py check` to see the pairs that the shapes don't make.
`./fed.sh -p ... check` asks `fed.sh` whether it accepts a set of presets,
without starting anything.

## Refusals, skips, and smoke limits

A set of presets can be turned away at three points:

- **`fed.sh` refuses it** (as `REFUSED` mirrors) where a preset could not do
  what it says: Pelican will not run it (a standalone origin over XRootD),
  the setting would have no effect
  ([`cache-memory`](../framework/presets/cache-memory.sh) at an XRootD
  cache), or the framework cannot build it (another container under
  `topo-tiny`). So a federation that `fed.sh` starts is what its presets
  say.
- **A suite [skips](glossary.md#skip)**, saying why, where it has nothing to
  check in a shape, e.g. [`transfer-api`](tests/transfer-api.md) where
  `/protected-a` takes no writes. That is no reason for `fed.sh` to refuse
  the shape.
- **No smoke shape makes it** (as `SMOKE_LIMITS` says) where it would test
  nothing, or nothing that other shapes don't test already.
  `framework/matrix.py check` rejects such a shape.

The exceptions:

- [`topo-transfer-server`](../framework/presets/topo-transfer-server.sh) and
  [`origin-transfer-api`](../framework/presets/origin-transfer-api.sh) are
  refused together, since the `transfer-api` suite tests one transfer API at
  a time; `framework/matrix.py` takes them for two levels of one choice
  (`transfer_api`).
- [`origin-firewalled`](../framework/presets/origin-firewalled.sh) includes
  [`origin-broker`](../framework/presets/origin-broker.sh), so `fed.sh`
  takes the two together, while `framework/matrix.py` takes them for two
  levels of one choice (`broker`).
- Rotated keys with
  [`server-unprivileged`](../framework/presets/server-unprivileged.sh) are a
  smoke limit, not a refusal: rotating keys is a step of `smoke.sh`'s, not a
  preset, so `fed.sh` never sees it. `./fed.sh keys add` and `rotate` refuse
  instead, once the keys are not the host's to write.

## Isolation

Nothing running in a container may change what the host runs, or the
framework's own code. Whatever could would run code on the host as the user
who runs `fed.sh`, who has Docker access. The Pelican code under test, and
the images, are trusted; the dev image, and what a build in the
[dev container](glossary.md#dev-container) fetches, less so.

- The dev container mounts this checkout read-only at `/scratch`. Under
  it, the dev container writes only what
  [`docker-compose.yaml`](../framework/docker-compose.yaml) mounts there
  read-write, each on its own: `framework/var/bin/linux/`, `results/`, and
  `dev/`, which hold what it keeps for itself; `control/requests/`; and
  the directories of the services that the tests write. A test that writes
  anywhere else fails with "Read-only file system". Put what a suite keeps
  for itself in `framework/var/dev/`.
- The host runs only its own binaries, in `framework/var/bin/host/`.
- A container can replace anything in a directory it writes with a symlink,
  and Docker follows a symlink in a bind source when it starts the
  container that mounts it. So no bind source is below a directory that a
  container writes: the dev container mounts each of the services'
  directories that it writes, rather than their parent, and the `s3`
  service serves each export's bucket through an rclone `combine` remote,
  not a bind mount of its own.
- The host's code that writes where a container does follows no symlink:
  [`control.py`](../framework/control.py),
  [`init-data.py`](../framework/init-data.py), and `fed.sh`, through
  [`put-file.py`](../framework/put-file.py), use the helpers in
  [`testlib/common.py`](../framework/testlib/common.py).
- Neither does the host's code that keeps what a container writes:
  `fed.sh` and `smoke.sh` copy the tests' results and logs with
  `put-file.py`, so that a symlink there cannot put a host file, such as a
  credential, in the logs or a CI artifact.
- [`check-mounts.py`](../framework/check-mounts.py), which `fed.sh` runs
  before it starts a container, refuses to while the Compose files break
  these rules; its docstring lists them. A new writable mount goes below
  one of its `WRITABLE` directories, and, if the dev container writes it,
  is mounted there on its own.
- The [`selftest`](tests/self-tests.md) suite's `confined` scenario checks
  the dev container's mounts from inside.

The dev container may write the Pelican checkout that `PELICAN_SRC_DIR`
names, which must not be, or hold, this one.

## Checks

[`.pre-commit-config.yaml`](../.pre-commit-config.yaml) runs the checks
([pre-commit](https://pre-commit.com/)):
[ShellCheck](https://www.shellcheck.net/);
[isort](https://pycqa.github.io/isort/),
[Black](https://black.readthedocs.io/),
[Bandit](https://bandit.readthedocs.io/),
[Pylint](https://pylint.readthedocs.io/), and
[Pyright](https://microsoft.github.io/pyright/) (configured in
`pyproject.toml`, and run with `uv run`);
[pytest](https://docs.pytest.org/);
[mdformat](https://mdformat.readthedocs.io/); and
[typos](https://github.com/crate-ci/typos). Run them all with
`pre-commit run --all-files`.
