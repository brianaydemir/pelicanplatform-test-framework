# Glossary

The terms these docs use: what the framework tests, the parts of the
framework, and its approach to testing Pelican.

## Scope

The framework tests [Pelican](https://pelicanplatform.org/)'s servers in
full, and its clients as they deal with a federation.

### Pelican server

An origin, cache, director, registry, or transfer server, run from Pelican's
images (`IMAGE_*`; [knobs](configuration.md#knobs)). Pelican's documentation
also calls these services; here, a [service](#service) is a container.
Everything about a server is in scope: its data path, APIs and web UI,
issuer, keys and database, what it reports of itself, what survives its
restart, and the server-only commands of `pelican-server`, such as
`origin collection`.

### Client

What a federation serves. The tests act as each of three kinds: the
[Pelican client](#pelican-client), [`stash_plugin`](#stash_plugin), and a
[plain HTTP client](#plain-http-client). In scope are a client's dealings
with a federation: the requests it makes, what it does with the answers,
which token it presents, and how it fails over. "Both clients" means the
Pelican client and `stash_plugin`.

### Pelican client

`pelican`, the client binary (`framework/var/bin/linux/pelican`):
`pelican object get`, `put`, `ls`, and the rest, `pelican token create`, and
the client agent (`--async`). It lacks the server-only commands.

### stash_plugin

[HTCondor](https://htcondor.org/)'s file transfer plugin for Pelican, which
reports each transfer in a result ad. Here it is a symlink to the Pelican
client, which acts as the plugin when run by that name.

### Plain HTTP client

An HTTP client that knows nothing of Pelican, such as
[curl](https://curl.se/): it asks a director for an object's bare path, and
follows the redirect as `curl -L` does. The tests play one themselves
([`framework/testlib/web.py`](../framework/testlib/web.py)).

### Out of scope and not tested

Out of scope: a client beyond its dealings with a federation, the
[helper services](#helper-service), and Docker. Not tested: what is in scope
but has no test, or none that would catch a fault
([What is not tested](tests/what-is-not-tested.md)).

## The federation

### Federation

The Pelican servers that [`fed.sh`](../fed.sh) runs in Docker, with the
[discovery host](#discovery-host), which together serve the
[namespaces](#namespace). By default it has one director, one registry, one
origin, and one cache; its [shape](#shape) decides the rest.

### Service

A [Docker Compose](https://docs.docker.com/compose/) service: one container,
by the name that `./fed.sh restart`, `./fed.sh logs`, and each run's logs
give it, e.g. `origin-0`, `cache-1`, `director-0`, or `registry`. A service
holds a Pelican server or a [helper service](#helper-service), or is the
[dev container](#dev-container). The tests restart services but never
recreate them.

### Helper service

A service beside the federation that stands in for what a site would
provide, and is not under test: the storage backends (`s3` and `webdav`,
both [rclone](https://rclone.org/), and the
[lab server](../framework/lab-server/), over SSH), the identity provider
([`idp`](../framework/idp/)), the
[metadata recorder](../framework/metadata-server/), `webdav-gate`, and
[Grafana](https://grafana.com/). Not to be confused with the
[service-control helper](#service-control-helper).

### Dev container

The service in which the tests run (`./fed.sh test`, `./fed.sh dev`). This
checkout is `/scratch` there, and your Pelican checkout is `/app`. It is no
part of the federation.

### Discovery host

The host that serves the federation's metadata
(`/.well-known/pelican-configuration`), which a client looks up from a
`pelican://` URL. By default, it is an [nginx](https://nginx.org/) service,
as the [OSDF](https://osg-htc.org/services/osdf)'s is. Under
[`-p director-discovery`](../framework/presets/director-discovery.sh), it is
director-0 (or, with
[`-p director-ha`](../framework/presets/director-ha.sh), the directors' load
balancer); under [`-p topo-tiny`](../framework/presets/topo-tiny.sh), `fed`;
under [`-p topo-standalone`](../framework/presets/topo-standalone.sh), the
origin itself.

### Namespace

A prefix that the origins export: `/public`, which anyone may read, and
`/protected-a` and `/protected-b`, which take a token, and each have their
own issuer and [test key](tests/authorization.md); and, under
[`-p topo-multi-owner`](../framework/presets/topo-multi-owner.sh),
origin-2's ([another owner](tests/another-owner.md)). Every namespace holds
the same [test data](#test-data).

### Native and XRootD

The two ways Pelican serves an origin or a cache. A native origin
(`posixv2`, `httpsv2`, `s3v2`, `pstore`, or `ssh`) or a V2 cache is
Pelican's own code; an XRootD origin (`posix`, `https`, or `s3`) or cache
puts [XRootD](https://xrootd.org/) in front.

### Topology

Which servers run, and in how many containers: what the `topo-`
[presets](../README.md#presets) choose. Part of the [shape](#shape).

## Configuring the federation

### Knob

A shell variable that `fed.sh` reads, from
[`framework/environment.cfg`](../framework/environment.cfg),
`local/environment.cfg`, the presets, or your environment. Only presets may
set the knobs that shape the federation; you set the others, such as
`PELICAN_TAG` and `IMAGE_*` ([Configuration](configuration.md#knobs)).

### Preset

A named change to the shape, given with `-p`: a short file in
[`framework/presets/`](../framework/presets/), or `local/presets/`, that
sets [knobs](#knob). Its name's prefix says what it changes (`topo-`,
`origin-`, `cache-`, `auth-`, `director-`, `registry-`, `server-`, or
`with-`) ([README](../README.md#presets)).

### Shape

The federation as its presets configure it: its topology, the kind of each
origin and cache, the features each has on, and how it authorizes. `fed.sh`
remembers the shape between commands, and each run's
[logs](../README.md#logs) record it in `shape.txt`.
[`smoke.sh`](../smoke.sh) names the shapes it runs, such as `default` and
`pelican-defaults`.

### Configuration layer

One of the directories of Pelican configuration files that each service
merges, later ones winning: the framework's own, those that `fed.sh`
generates from the shape, yours in `local/config.d/`, and last, the tests'
own ([Configuration](configuration.md#pelican-configuration)).

## Running the tests

### Suite

A module in [`framework/suites/`](../framework/suites/) that checks one area
of Pelican, such as [`transfers`](tests/test-data.md) or
[`registry`](tests/registry.md), and has a page under
[What the tests check](tests/README.md). [`test.py`](../test.py) runs the
suites in a fixed order
([`framework/suites/__init__.py`](../framework/suites/__init__.py)).

### Scenario

A named part of a suite, which `test.py` can run alone, e.g.
`./fed.sh test commands/ls-public`. A scenario records one or more
[cases](#case).

### Case

One judgment, recorded as a row of results, named `<scenario>` or
`<scenario> <case>`, with a status of `PASS`, `FAIL`, `SKIP`, or
`INCONCLUSIVE` ([Results](ci.md#results)). A case that the suite could not
judge is `INCONCLUSIVE`, with a note saying why.

### Token profile

One of the two forms of token that Pelican understands, whose scopes differ:
the
[WLCG common JWT profile](https://github.com/WLCG-AuthZ-WG/common-jwt-profile/blob/master/profile.md)'s
and [SciTokens 2.0](https://scitokens.org/technical_docs/Claims)'s
([token profiles](tests/README.md#token-profiles)).

### Pass

One run of a suite with one [token profile](#token-profile): the WLCG pass,
reported as `<suite>`, then the SciTokens pass, as `<suite>-scitokens`. A
scenario marked `WLCG only` runs in the first alone. Not to be confused with
a case's `PASS`.

### Setup token

The WLCG token that a scenario's setup, and its checks of what the stores
hold, use in either [pass](#pass), so that the SciTokens pass tests only
what the scenario itself presents.

### Credential

One of the named tokens that the `transfers` and `auth` suites present in
each namespace, such as `server`, `expired`, and `wrong-path`, each right or
wrong in one way, or `none`, which presents no token
([Authorization](tests/authorization.md)).

### Route

How a client reaches an object: through the cache a director picks, direct
from an origin (`--direct`, or `?directread`), or from a cache it names
(`--cache`).

### Test data

The objects that `./fed.sh init` puts in each namespace, and those the
suites write. An object's bytes are its own path in the federation, so no
two are alike, and a server that answers from the wrong storage is caught.
Each suite removes what it wrote ([test data](tests/test-data.md)).

### Twin

A scenario or row that repeats another with one thing changed: a `transfers`
scenario's direct, named, plugin, and HTTP twins
([test data](tests/test-data.md)), and each row of the SciTokens pass, the
twin of its row in the WLCG pass.

### Service-control helper

[`framework/control.py`](../framework/control.py), which runs on the host
beside the tests, and restarts, stops, starts, pauses, and kills services
for them, since the dev container can't control Docker. Without it, the
scenarios that need it skip ([README](../README.md#service-control)).

### Watchdog

`test.py`'s guard against a stalled run: it abandons a pass that prints
nothing for 20 minutes, or runs for 90, records it as `aborted` (or `setup`,
if it recorded no case), and goes on to the next.

### Smoke tests

What `smoke.sh` runs: every suite, in each of its shapes, each from a clean
start. It resets the federation before each shape, so it is destructive
([CI and results](ci.md)).

### Smoke matrix

The shapes that `smoke.sh` runs when named none, listed in
[`framework/smoke-shapes.tsv`](../framework/smoke-shapes.tsv). Together they
make every pair of choices that the presets offer, but those that would test
nothing ([the smoke matrix](development.md#the-smoke-matrix)).

## The approach

### Functional check

What every test is: whether Pelican does what it should, moving a few
objects, the largest about 16 MiB. The tests are no stress test or
benchmark.

### Every combination

The checks run over every combination of client, route, namespace,
credential, server, and token profile that the shape has, so that a fault in
one of them alone shows.

### Skip

What a suite or scenario does in a shape that lacks what it tests, or
without what it needs, such as the service-control helper: it runs nothing,
and says why.

### Failure

A row whose check Pelican did not meet. The tests define what Pelican should
do, and the framework works around no fault of Pelican's.
