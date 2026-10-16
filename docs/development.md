# Developing the Framework

For changing the framework itself: where its parts are, what a suite is
made of, and the checks that keep it consistent. The rest of the
documentation is for running it ([README](../README.md)). Each module's
docstring is its reference; this page says where to look.

The Python targets 3.9 and its standard library alone, which is what
the dev container (AlmaLinux 9) and macOS ship.


## Layout

| Path                     | Holds                                                                       |
| ------------------------ | --------------------------------------------------------------------------- |
| `fed.sh`                 | the shape: knobs, presets, generated configuration, and Docker Compose      |
| `test.py`                | the runner: selects suites and scenarios, runs them under a watchdog, and sums up |
| `framework/suites/`      | the suites; `__init__.py` gives their contract, and the order they run in   |
| `framework/testlib/`     | what the suites share; `__init__.py` indexes its modules                    |
| `framework/control.py`   | the helper on the host that restarts, stops, and pauses services for the tests ([service control](../README.md#service-control)) |
| `framework/report.py`    | merges results and converts them to JUnit XML and Markdown ([Results](ci.md#results)) |
| `framework/matrix.py`    | the choices the presets make, and whether `smoke.sh`'s shapes cover every pair ([below](#the-smoke-matrix)) |
| `framework/init-data.py`, `framework/init-certs.sh` | the test objects and the certificates, which `./fed.sh init` makes |
| `framework/presets/`     | one file per preset; `default.sh` declares every knob                       |
| `framework/config.d/`    | Pelican configuration, by layer ([Configuration](configuration.md#pelican-configuration)) |
| `framework/etc/`         | static files the services mount, among them `pelican.yaml`, which names the layers |
| `framework/idp/`, `framework/metadata-server/`, `framework/lab-server/` | the helper services beside the federation: the identity provider, the metadata recorder, and the SSH host |
| `framework/docker-compose.yaml`, `framework/docker-compose.tiny.yaml` | the services, by topology |


## Suites

A suite is a module in `framework/suites/` with `SCENARIOS`, its
scenarios' names and descriptions in the order they run; `skip(fed)`,
why it can test nothing in a shape, or `None`; and `run(session,
selected, results)`. It may name `REQUIRED` scenarios, which run
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
own minting, and so run in the WLCG pass alone. A suite that keeps
files under `framework/var/data/` for people to read keeps each pass's
apart, with `session.var_dir()`.

A suite records a row per case ([Results](ci.md#results)), named
`<scenario>` or `<scenario> <case>`, through `testlib.report`. A row the
suite could not judge is `INCONCLUSIVE`, with a note saying why, rather
than a pass. Each suite's page under
[What the tests check](tests/README.md) describes every scenario, and
which rows are expected to fail on Pelican as it is; a change to a suite
is a change to its page.


## Service control from a test

`framework/testlib/control.py` has a function for each action the
helper takes (`restart`, `stop`, `start`, `pause`, `unpause`, and
`kill`), each of which returns once the action is done, and for a
restart, once the server answers again; `state()`, which asks what
state a service's container is in, for a test to notice a server that
exited on its own, and start it again; `restart_with()`, which
configures a service for the length of a `with` block; and
`unavailable()`, which says why the tests can't control services here,
if they can't. Its docstring describes the files under
`framework/var/control/` by which the tests and the helper talk. A
scenario should put back whatever it stops, pauses, kills, or
configures, even if it fails; the helper puts back what is left only
when it stops serving.


## The smoke matrix

`smoke.sh`'s shapes together make every pair of choices that the
presets offer, but for the exceptions that
[Configuration](configuration.md#presets) lists. `framework/matrix.py`
keeps that true, with three tables: `PRESETS`, which maps each preset to
the choice it makes; `REFUSED`, which mirrors the combinations that
`fed.sh` refuses, and must agree with its checks; and `SMOKE_LIMITS`,
the pairs left out on purpose. `framework/matrix.py check` reads the
shapes from `smoke.sh --table` and reports any shape that `fed.sh` would
refuse, any preset no shape runs, and any pair no shape covers;
`suggest` proposes shapes to add.

So a new preset goes in four places: `framework/presets/`, those tables,
a shape in `smoke.sh`, and the preset tables in the
[README](../README.md#presets). `./fed.sh -p ... check` asks `fed.sh`
whether it accepts a set of presets, without starting anything.


## Linters

`pyproject.toml` configures black, isort, bandit, mypy, pylint, and
pyright, and its header lists the commands, each run with `uv run`,
which installs them.
