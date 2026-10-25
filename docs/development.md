# Developing the Framework

For changing the framework itself: where its parts are, what a suite is
made of, and the checks that keep it consistent. The rest of the
documentation is for running it ([README](../README.md)). Each module's
docstring is its reference; this page says where to look.

The Python targets 3.9 and its standard library alone, which is what
the dev container (AlmaLinux 9) and macOS ship.

## Layout

| Path                                                                    | Holds                                                                                                                            |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `fed.sh`                                                                | the shape: knobs, presets, generated configuration, and Docker Compose                                                           |
| `test.py`                                                               | the runner: selects suites and scenarios, runs them under a watchdog, and sums up                                                |
| `framework/suites/`                                                     | the suites; `__init__.py` gives their contract, and the order they run in                                                        |
| `framework/testlib/`                                                    | what the suites share; `__init__.py` indexes its modules                                                                         |
| `framework/control.py`                                                  | the helper on the host that restarts, stops, and pauses services for the tests ([service control](../README.md#service-control)) |
| `framework/report.py`                                                   | merges results and converts them to JUnit XML and Markdown ([Results](ci.md#results))                                            |
| `framework/matrix.py`                                                   | the choices the presets make, and the shapes that `smoke.sh` runs: it writes them and checks them ([below](#the-smoke-matrix))   |
| `framework/smoke-shapes.tsv`                                            | the shapes that `smoke.sh` runs, which `framework/matrix.py` writes                                                              |
| `framework/init-data.py`, `framework/init-certs.sh`                     | the test objects and the certificates, which `./fed.sh init` makes                                                               |
| `framework/presets/`                                                    | one file per preset; `default.sh` declares every knob                                                                            |
| `framework/config.d/`                                                   | Pelican configuration, by layer ([Configuration](configuration.md#pelican-configuration))                                        |
| `framework/etc/`                                                        | static files the services mount, among them `pelican.yaml`, which names the layers                                               |
| `framework/idp/`, `framework/metadata-server/`, `framework/lab-server/` | the helper services beside the federation: the identity provider, the metadata recorder, and the SSH host                        |
| `framework/docker-compose.yaml`, `framework/docker-compose.tiny.yaml`   | the services, by topology                                                                                                        |

## Suites

A suite is a module in `framework/suites/` with `SCENARIOS`, its
scenarios' names and descriptions in the order they run; `skip(fed)`,
why it can test nothing in a shape, or `None`; and `run(session, selected, results)`. It may name `REQUIRED` scenarios, which run
whatever `test.py` is asked for. A suite imports only `testlib`, never
another suite: what two suites share lives there. The docstring of
`framework/suites/__init__.py` gives the contract in full, and why the
suites run in the order they do; a new suite goes in its `SUITES`, in
that order.

`test.py` runs each suite once for each
[token profile](tests/README.md#token-profiles), each time with a
session of that profile, so a suite's tokens come from its session:
`session.token()` and the credential table's are the session's profile,
for what a scenario presents, and `session.setup_token()` is WLCG's in
either pass, for putting objects in place and reading the stores. A
token a suite mints itself takes the session's profile too
(`credentials.mint()` and its kin require one). A suite names its
`ONE_PROFILE` scenarios, which present no storage token of the tests'
own minting, or make a check that no other profile can, and so run in
the WLCG pass alone. A suite that keeps files under
`framework/var/data/` for people to read keeps each pass's apart, with
`session.var_dir()`.

`test.py` abandons a pass that stalls, by raising an exception in the
suite wherever it is. So a suite gives every client command and request
a timeout, runs tasks at once on `testlib.common.pool()`, which drops
what it had queued when its block is left so, rather than on a
`ThreadPoolExecutor` of its own, which would run all of it, and puts
back what it changed in `finally` blocks. It waits for the federation to
serve again after a restart with `testlib.control.settle()`, which, like
`control.wait_serving()`, stops at once if a Pelican server has exited
on its own, as one that panics as it starts does, rather than wait out
its deadline. It runs the clients with `session.pelican_cmd()` and
`session.plugin_cmd()`, which kill a run that outlasts its timeout and
log every run, with all it printed, in the pass's `commands.log`.

A suite records a row per case ([Results](ci.md#results)), named
`<scenario>` or `<scenario> <case>`, through `testlib.report`. A row
that found several problems names the first in its note with
`testlib.common.first()`, which prints them all, for the log. A row the
suite could not judge is `INCONCLUSIVE`, with a note saying why, rather
than a pass. Each suite's page under
[What the tests check](tests/README.md) describes every scenario, and
which rows are expected to fail on Pelican as it is; a change to a suite
is a change to its page.

## Service control from a test

`framework/testlib/control.py` has a function for each action the
helper takes (`restart`, `stop`, `start`, `pause`, `unpause`, `kill`,
and `freeze_xrootd` and `thaw_xrootd`, which stop and resume the XRootD
daemons in a service's container), each of which returns once the
action is done, and for a restart, a start, or an unpause, once the
server answers again;
`state()`, which asks what state a service's container is in, for a
test to notice a server that exited on its own, and start it again;
`restart_with()`, which configures a service for the length of a
`with` block; and `unavailable()`, which says why the tests can't
control services here, if they can't. Its docstring describes the files
under `framework/var/control/` by which the tests and the helper talk.
A scenario should put back whatever it stops, pauses, kills, freezes,
or configures, even if it fails; the helper puts back what is left only
when it stops serving, and the next one to start only the configuration.

## The smoke matrix

Named no shapes, `smoke.sh` runs those in `framework/smoke-shapes.tsv`:
as many as are useful, each as simple as it can be. A shape makes a
choice for each of its presets, and another if it rotates every key;
what it tests is measured by the pairs of choices it makes. So the
shapes:

- make, together, every pair of choices that a shape could make, but
  those that would test nothing
  ([below](#refusals-skips-and-smoke-limits));
- each make some pair that no other shape makes, since a shape that
  makes none adds nothing to a run;
- each make at most `MAX_CHOICES` choices (8), none of which could go
  back to its default without leaving some pair uncovered, since a
  simpler shape points at fewer presets when it fails;
- and include two baselines, whatever else they cover: `default`, the
  framework's own choices, what `./fed.sh up` runs, and
  `pelican-defaults`, those that Pelican makes when told nothing.

`framework/matrix.py` holds the rules, in its tables: `PRESETS`, which
maps each preset to the choice it makes; `REFUSED`, which mirrors the
combinations that `fed.sh` refuses, and must agree with its checks;
`SMOKE_LIMITS`, the shapes that `smoke.sh` does not run; `BASELINES`;
and `MAX_CHOICES`. `framework/matrix.py generate` writes
`framework/smoke-shapes.tsv` from them, in a few minutes: it keeps the
shapes there that still pass, adds shapes greedily until every pair is
covered, then makes each as simple as it can be, and drops any that
adds nothing. With `--fresh`, it starts from none, which may find fewer
shapes, but changes more of them. A shape's name spells out its
choices, e.g. `xrootd-v2-multi-cache-no-direct`. `framework/matrix.py check` reads the shapes from `smoke.sh --table`, and reports any shape
that `fed.sh` would refuse, that breaks a rule above, or that is
misnamed; any preset no shape runs; and any pair no shape covers.

So a new preset goes in `framework/presets/`, the tables in
`framework/matrix.py`, and the preset tables in the
[README](../README.md#presets); then run `framework/matrix.py generate`, and commit the shapes it writes. `./fed.sh -p ... check`
asks `fed.sh` whether it accepts a set of presets, without starting
anything.

## Refusals, skips, and smoke limits

A set of presets can be turned away at three points, each for a reason
of its own:

- **`fed.sh` refuses it**, as `REFUSED` mirrors, where a preset could
  not do what it says in that shape: Pelican will not run it (a
  standalone origin over XRootD), the setting would have no effect
  (`cache-memory` at an XRootD cache, `origin-max-age` at an XRootD
  origin), or the framework cannot build it (another container under
  `topo-tiny`, or a `pstore` origin, which only its writes can fill,
  under `origin-no-direct`). So a federation that `fed.sh` starts is
  what its presets say.
- **A suite skips**, and says why, where the federation is what its
  presets say, but the suite has nothing to check there: the
  `transfer-api` suite, where `/protected-a` takes no writes. That is
  no reason for `fed.sh` to refuse the shape, which someone may want
  for something else.
- **No smoke shape makes it**, as `SMOKE_LIMITS` says, where it would
  test nothing: nothing at all, as with a transfer API where
  `/protected-a` takes no writes, or nothing that other shapes don't
  test already, as with the features that run over `posixv2` and
  `xrootd` origins alone, since what they test is not the origins'
  storage. `framework/matrix.py check` rejects such a shape in
  `framework/smoke-shapes.tsv`, and asks no shape to make a pair that
  only such shapes make.

The exceptions:

- `topo-transfer-server` and `origin-transfer-api`, which Pelican could
  run together, are refused together, since the `transfer-api` suite
  tests one transfer API at a time; `framework/matrix.py` takes them
  for two levels of one choice (`transfer_api`).
- `origin-firewalled` includes `origin-broker`, so `fed.sh` takes the
  two together, while `framework/matrix.py` takes them for two levels
  of one choice (`broker`), of which a smoke shape makes one.
- `cache-tiered` has no effect with a cache image older than Pelican's
  main, but `fed.sh` only warns, and only of the released image, which
  it knows by name, since it cannot tell what an image holds; the
  `tiering` suite then skips.
- Rotated keys with `server-unprivileged` cannot be had, since the host
  may lose the right to write the servers' keys, but they are a smoke
  limit, not a refusal: rotating keys is a step of `smoke.sh`'s, not a
  preset, so `fed.sh` never sees it. `./fed.sh keys add` and `rotate`
  refuse instead, once the keys are no longer the host's to write.

## Linters

`pyproject.toml` configures black, isort, bandit, mypy, pylint, and
pyright, and its header lists the commands, each run with `uv run`,
which installs them.
