# Glossary

The terms these docs use, and what they mean here: first what the
framework tests and what it doesn't, then the parts of the framework,
then its approach to testing Pelican.


## Scope

The framework tests Pelican: its servers in full, and its clients only
as they deal with a federation.

### Pelican server

An origin, cache, director, registry, or transfer server, run from
Pelican's images (`IMAGE_*`). Pelican's own documentation also calls
these services; here, a [service](#service) is a container, which may
hold a server or something else. Everything about a server is in scope:
its data path, its APIs and web UI, its issuer, its keys and database,
what it reports of itself, what survives its restart, and the
server-only commands of `pelican-server`, such as `origin collection`.

### Client

What a federation serves. There are three kinds, and the tests act as
each: the [Pelican client](#pelican-client),
[`stash_plugin`](#stash_plugin), and a
[plain HTTP client](#plain-http-client). A client's dealings with a
federation are in scope: the requests it makes, what it does with the
answers, which token it presents, and how it fails over. Anything else
about it is out of scope: the tests don't check that curl is correct,
only what a federation tells a client that behaves as curl does. "Both
clients" means the Pelican client and `stash_plugin`.

### Pelican client

`pelican`, the client binary (`framework/var/bin/linux/pelican`):
`pelican object get`, `put`, `ls`, and the rest, `pelican token create`,
and the client agent (`--async`). It lacks the server-only commands.

### stash_plugin

HTCondor's file transfer plugin for Pelican, which reports each
transfer in a result ad. Here it is a symlink to the Pelican client,
which acts as the plugin when run by that name.

### Plain HTTP client

An HTTP client that knows nothing of Pelican, such as curl: it asks a
director for an object's bare path, and follows the redirect as
`curl -L` does. The tests play one themselves
(`framework/testlib/web.py`), in the HTTP [twins](#twin).

### Out of scope and not tested

Out of scope is what the framework has no business testing: a client
beyond its dealings with a federation, the
[helper services](#helper-service), and Docker. Not tested is what is
in scope but has no test, or none that would catch a fault;
[What is not tested](tests/what-is-not-tested.md) lists what matters
most.


## The federation

### Federation

The Pelican servers that `fed.sh` runs in Docker, with the
[discovery host](#discovery-host), which together serve the
[namespaces](#namespace). By default it has one director, one registry,
one origin, and one cache; its [shape](#shape) decides the rest.

### Service

A Docker Compose service: one container, by the name that
`./fed.sh restart`, `./fed.sh logs`, and each run's logs give it, e.g.
`origin-0`, `cache-1`, `director-0`, or `registry`. A service holds a
Pelican server or a [helper service](#helper-service), or is the
[dev container](#dev-container). The tests restart services but never
recreate them.

### Helper service

A service beside the federation that stands in for what a site would
provide, and is not under test: the storage backends (`s3`, `webdav`,
and the lab server, over SSH), the identity provider (`idp`), the
metadata recorder, `webdav-gate`, and Grafana. A `with-` preset adds
one. Not to be confused with the
[service-control helper](#service-control-helper).

### Dev container

The service in which the tests run (`./fed.sh test`, `./fed.sh dev`).
This checkout is `/scratch` there, and your Pelican checkout `/app`. It
is no part of the federation, and the service-control helper never
touches it.

### Discovery host

The host that serves the federation's metadata
(`/.well-known/pelican-configuration`), which a client looks up from a
`pelican://` URL: an nginx service, as the OSDF does, or, under
`-p director-discovery`, director-0.

### Namespace

A prefix that the origins export: `/public`, which anyone may read, and
`/protected-a` and `/protected-b`, which take a token, and each have an
issuer and a test key of their own; and, under `-p topo-multi-owner`,
origin-2's. Every namespace holds the same [test data](#test-data).

### Native and XRootD

The two ways Pelican serves an origin or a cache. A native origin
(`posixv2`, `httpsv2`, `s3v2`, `pstore`, or `ssh`) or a V2 cache is
Pelican's own code; an XRootD origin (`posix`, `https`, or `s3`) or
cache puts XRootD in front. Many results differ between them, so a
suite's page often says which it means.

### Topology

Which servers run, and in how many containers: what the `topo-` presets
choose. Part of the shape.


## Configuring the federation

### Shape

The federation as configured: its topology, the kind of each origin and
cache, the features each has on, and how it authorizes; everything that
the presets decide. `fed.sh` remembers the shape between commands, and
each run's logs record it in `shape.txt`. `smoke.sh` names the shapes it
runs, each a set of presets, such as `default` and `pelican-defaults`.

### Preset

A named change to the shape, given with `-p`: a short file in
`framework/presets/`, or `local/presets/`, that sets
[knobs](#knob). Its name's prefix says what it changes (`topo-`,
`origin-`, `cache-`, `auth-`, `server-`, or `with-`). Presets combine,
but those that choose among alternatives set the same knob, and
`fed.sh` refuses two of them together
([README](../README.md#presets)).

### Knob

A shell variable that `fed.sh` reads, from `framework/environment.cfg`,
`local/environment.cfg`, the presets, or your environment. Only presets
may set the knobs that shape the federation; you set the others, such
as `PELICAN_TAG` and `IMAGE_*` ([Configuration](configuration.md#knobs)).

### Configuration layer

One of the directories of Pelican configuration files that each service
merges, later ones winning: the framework's own, those that `fed.sh`
generates from the shape, yours in `local/config.d/`, and last, the
tests' own ([Configuration](configuration.md#pelican-configuration)).


## Running the tests

### Suite

A module in `framework/suites/` that checks one area of Pelican, such
as `transfers` or `registry`, and has a page under
[What the tests check](tests/README.md). `test.py` runs the suites in a
fixed order.

### Scenario

A named part of a suite, which `test.py` can run alone, e.g.
`./fed.sh test commands/ls-public`. A scenario records one or more
[cases](#case).

### Case

One judgment, recorded as a row of results, named `<scenario>` or
`<scenario> <case>`, with a status of `PASS`, `FAIL`, `SKIP`, or
`INCONCLUSIVE` ([Results](ci.md#results)). A case that the suite could
not judge is `INCONCLUSIVE`, with a note saying why, rather than a
pass.

### Pass

One run of a suite with one [token profile](#token-profile): the WLCG
pass, reported as `<suite>`, then the SciTokens pass, as
`<suite>-scitokens`. A scenario marked `WLCG only` runs in the first
alone. Not to be confused with a case's `PASS`.

### Token profile

One of the two forms of token that Pelican understands, whose scopes
differ: the WLCG common JWT profile's and SciTokens 2.0's. Each suite
runs once with each, and each row of the second pass must fare as its
twin in the first does
([Token profiles](tests/README.md#token-profiles)).

### Credential

One of the named tokens that the `transfers` and `auth` suites present
in each namespace, such as `server`, `expired`, and `wrong-path`, each
right or wrong in one way, or `none`, which presents no token
([Authorization](tests/authorization.md)).

### Route

How a client reaches an object: through the cache a director picks,
direct from an origin (`--direct`, or `?directread`), or from a cache it
names (`--cache`).

### Twin

A scenario or row that repeats another with one thing changed: a
`transfers` scenario's direct, named, plugin, and HTTP twins
([Test data](tests/test-data.md)), and each row of the SciTokens pass,
the twin of its row in the WLCG pass.

### Test data

The objects that `./fed.sh init` puts in each namespace, and those the
suites write. An object's bytes are its own path in the federation, so
no two are alike, and a server that answers from the wrong storage is
caught. Each suite removes what it wrote.

### Service-control helper

`framework/control.py`, which runs on the host beside the tests, and
restarts, stops, starts, pauses, and kills services for them, since the
dev container can't control Docker. Without it, the scenarios that need
it are skipped ([README](../README.md#service-control)).

### Watchdog

`test.py`'s guard against a stalled run: it abandons a pass that prints
nothing for 20 minutes, or runs for 90, records it as `aborted`, and
goes on to the next.

### Smoke tests

What `smoke.sh` runs: every suite, in each of its shapes, each from a
clean start. It resets the federation before each shape, so it is
destructive ([CI and results](ci.md)).

### Smoke matrix

The shapes that `smoke.sh` runs when named none, listed in
`framework/smoke-shapes.tsv`: as many as are useful, each as simple as
it can be. Together they make every pair of choices that the presets
offer, but those that would test nothing; each makes some pair that no
other does, and at most eight choices. `framework/matrix.py` writes
them, and checks them
([Developing the framework](development.md#the-smoke-matrix)). Two
shapes are baselines, whose failures are none of the presets' doing:
`default`, the framework's own choices, and `pelican-defaults`,
Pelican's.


## The approach

### Functional check

What every test is: whether Pelican does what it should, moving a few
objects, the largest about 16 MiB. The tests are no stress test or
benchmark.

### Every combination

The checks run over every combination of client, route, namespace,
credential, server, and token profile that the shape has, so that a
fault in one of them alone shows.

### Skip

What a suite or scenario does in a shape that lacks what it tests, or
without what it needs, such as the service-control helper: it runs
nothing, and says why.

### Expected failure

A row that fails on Pelican as it is. The tests check what Pelican
should do, and the framework works around no fault of Pelican's, even a
known one; each suite's page says which of its rows are expected to
fail.

### Setup token

The WLCG token that a scenario's setup, and its checks of what the
stores hold, use in either pass, so that the SciTokens pass tests only
what the scenario itself presents.
