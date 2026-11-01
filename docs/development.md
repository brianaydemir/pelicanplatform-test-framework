# Developing the Framework

For changing the framework itself: where its parts are, what a
[suite](glossary.md#suite) is made of, and the checks that keep it
consistent. Each module's docstring is its reference; this page says where
to look.

The Python uses the standard library alone, and runs under the version that
[`pyproject.toml`](../pyproject.toml) requires, which the checks assume. The
scripts on the host ([`control.py`](../framework/control.py),
[`report.py`](../framework/report.py),
[`matrix.py`](../framework/matrix.py), and
[`init-data.py`](../framework/init-data.py)) get it from
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
| `framework/matrix.py`                                                                                                                                     | the choices the presets make, and the shapes that `smoke.sh` runs ([the smoke matrix](#the-smoke-matrix))                        |
| [`framework/smoke-shapes.tsv`](../framework/smoke-shapes.tsv)                                                                                             | the shapes that `smoke.sh` runs, which `framework/matrix.py` writes                                                              |
| `framework/init-data.py`, [`framework/init-certs.sh`](../framework/init-certs.sh)                                                                         | the test objects and the certificates, which `./fed.sh init` makes                                                               |
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
could not judge is `INCONCLUSIVE`, with a note saying why, never a pass. A
scenario that raises fails, by `testlib.common.guarded()`, and the rest
still run.

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

[`smoke.sh`](../smoke.sh) runs the shapes in `framework/smoke-shapes.tsv`:
as many as are useful, each as simple as it can be. A shape makes a choice
for each of its presets, and another if it rotates every key. What it tests
is measured by the pairs of choices it makes. So the shapes:

- together make every pair of choices that a shape could make, but those
  that would test nothing
  ([smoke limits](#refusals-skips-and-smoke-limits));
- each make some pair that no other shape makes;
- each make at most `MAX_CHOICES` choices (8), none of which could go back
  to its default without leaving some pair uncovered, since a simpler shape
  points at fewer presets when it fails;
- include two baselines: `default`, the framework's own choices, and
  `pelican-defaults`, Pelican's.

`framework/matrix.py` holds the rules in its tables:

- `PRESETS`: the choice each preset makes;
- `REFUSED`: the combinations that `fed.sh` refuses, which must agree with
  its checks;
- `SMOKE_LIMITS`: the shapes that `smoke.sh` does not run;
- `BASELINES` and `MAX_CHOICES`.

`framework/matrix.py generate` writes `framework/smoke-shapes.tsv` from
them, in a few minutes. It keeps the shapes there that still pass, adds
shapes greedily until every pair is covered, simplifies each, and drops any
that adds nothing. With `--fresh`, it starts from none, which may find fewer
shapes, but changes more. A shape's name spells out its choices, e.g.
`xrootd-v2-multi-cache-no-direct`.

`framework/matrix.py check` reads the shapes from `smoke.sh --table`, and
reports any shape that `fed.sh` would refuse, that breaks one of these
rules, or that is misnamed; any preset no shape runs; and any pair no shape
covers.

So a new preset goes in `framework/presets/`, the tables in
`framework/matrix.py`, and the
[README's preset tables](../README.md#presets); then run
`framework/matrix.py generate`, and commit the shapes it writes.
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
