# What the Tests Check

`./fed.sh test` runs [`test.py`](../../test.py) in the
[dev container](../glossary.md#dev-container)
([Running the tests](../../README.md#running-the-tests)). It runs every
[suite](../glossary.md#suite) that applies to the federation's
[shape](../glossary.md#shape), over
[every combination](../glossary.md#every-combination) of
[client](../glossary.md#client), [route](../glossary.md#route),
[namespace](../glossary.md#namespace),
[credential](../glossary.md#credential), server, and
[token profile](#token-profiles) that the shape has.

These are [functional checks](../glossary.md#functional-check), not stress
tests: each moves a few objects, the largest about 16 MiB. The exception is
[`blocks`](block-boundaries.md)'s `cache-evicted`, which under
[`-p cache-small`](../../framework/presets/cache-small.sh) moves about 200
MiB through each cache.

## Suites

The suites run in this order:

| Suite                                                      | Checks                                                                                                                                                                                             |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`federation`](../../framework/suites/federation.py)       | that the federation serves; what each director names and redirects to; discovery; the broker; approval; downtime ([federation](federation.md), [redirects](redirects.md), [downtime](downtime.md)) |
| [`selftest`](../../framework/suites/selftest.py)           | the tests' own helpers ([self-tests](self-tests.md))                                                                                                                                               |
| [`commands`](../../framework/suites/commands.py)           | `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, and what `get` and `put` do with what already exists ([client commands](client-commands.md))                                                     |
| [`client`](../../framework/suites/client.py)               | contracts that show only through the clients: result ads, failover, ETags, tokens, URL forms, and the client agent ([client contracts](client-contracts.md))                                       |
| [`transfer-api`](../../framework/suites/transfer_api.py)   | the transfer API of origin-0 or a transfer server: authentication, credentials, copies, jobs, and confinement ([transfer API](transfer-api.md))                                                    |
| [`listings`](../../framework/suites/listings.py)           | PROPFIND at each server, a GET of a collection, and an origin's HTML listing ([listings](listings.md))                                                                                             |
| [`names`](../../framework/suites/names.py)                 | objects whose names a URL or a listing must encode ([names](names.md))                                                                                                                             |
| [`blocks`](../../framework/suites/blocks.py)               | block boundaries and objects past 4 GiB; digests; conditional requests; overwrites, corruption, and eviction ([block boundaries](block-boundaries.md))                                             |
| [`freshness`](../../framework/suites/freshness.py)         | how long the caches keep their copies under each `Cache-Control` ([cache freshness](cache-freshness.md))                                                                                           |
| [`cache-api`](../../framework/suites/cache_api.py)         | a V2 cache's own APIs: prestage, evict, purge, introspection, usage, and consistency ([cache APIs](cache-apis.md))                                                                                 |
| [`throttling`](../../framework/suites/throttling.py)       | the servers and clients with the origins slowed ([throttling](throttling.md))                                                                                                                      |
| [`exports`](../../framework/suites/exports.py)             | a native origin's export capabilities, nested exports, third-party COPY, and backend tokens ([exports and copies](exports-and-copies.md))                                                          |
| [`pstore`](../../framework/suites/pstore.py)               | a pstore origin's storage API, scans, reclamation, capacity, and catalog backups ([pstore](pstore.md))                                                                                             |
| [`routing`](../../framework/suites/routing.py)             | how the directors order and filter servers, and what they tell a client ([routing](routing.md))                                                                                                    |
| [`director-api`](../../framework/suites/director_api.py)   | what the directors publish: servers, namespaces, health tests, and metrics ([director APIs](director-apis.md))                                                                                     |
| [`registry`](../../framework/suites/registry.py)           | prefixes, keys, the registry's web API, ownership, and downtimes ([registry](registry.md))                                                                                                         |
| [`tiering`](../../framework/suites/tiering.py)             | V2 caches' tiering of large objects to object storage ([tiering](tiering.md))                                                                                                                      |
| [`posc`](../../framework/suites/posc.py)                   | interrupted and racing uploads at every origin ([POSC and metadata](posc-and-metadata.md))                                                                                                         |
| [`metadata`](../../framework/suites/metadata.py)           | metadata events, the catalog, and the publish queue ([POSC and metadata](posc-and-metadata.md))                                                                                                    |
| [`users`](../../framework/suites/users.py)                 | which Unix user the origins act as ([Unix users](unix-users.md))                                                                                                                                   |
| [`owners`](../../framework/suites/owners.py)               | origin-2, another owner ([another owner](another-owner.md))                                                                                                                                        |
| [`sitelocal`](../../framework/suites/sitelocal.py)         | cache-2, a site-local cache ([site-local cache](site-local-cache.md))                                                                                                                              |
| [`collections`](../../framework/suites/collections.py)     | prefixes with access control of their own, granted through the embedded issuer ([collections](collections.md))                                                                                     |
| [`issuer`](../../framework/suites/issuer.py)               | the origins' embedded issuer: grants, flows, refresh tokens, and boundaries ([issuer](issuer.md))                                                                                                  |
| [`accounts`](../../framework/suites/accounts.py)           | the life cycle of a server's users and groups, and the authority it carries ([accounts](accounts.md))                                                                                              |
| [`oidc`](../../framework/suites/oidc.py)                   | logging in through an identity provider ([OIDC login](oidc-login.md))                                                                                                                              |
| [`advertise`](../../framework/suites/advertise.py)         | what the directors take from servers, and from impostors ([advertisements](advertisements.md))                                                                                                     |
| [`webapi`](../../framework/suites/webapi.py)               | who may use each server's web API; login, CSRF, and cross-origin requests ([web API](web-api.md))                                                                                                  |
| [`observability`](../../framework/suites/observability.py) | each server's health, metrics, and logs, and who may see them ([observability](observability.md))                                                                                                  |
| [`webdav`](../../framework/suites/webdav.py)               | the origins' WebDAV methods beyond the basics, and how each is authorized ([WebDAV methods](webdav-methods.md))                                                                                    |
| [`auth`](../../framework/suites/auth.py)                   | each credential at each server in each namespace, and token validation ([authorization](authorization.md), [token validation](token-validation.md))                                                |
| [`transfers`](../../framework/suites/transfers.py)         | gets and puts by every client, route, namespace, and credential, byte-checked ([test data](test-data.md))                                                                                          |
| [`restart`](../../framework/suites/restart.py)             | what survives a server's restart, and what the others do meanwhile ([restarts](restarts.md))                                                                                                       |
| [`keys`](../../framework/suites/keys.py)                   | keys added and rotated while the servers run ([keys](keys.md))                                                                                                                                     |

[`framework/suites/__init__.py`](../../framework/suites/__init__.py)
explains the order. In short:

- `federation`'s `ready` and `caches` run first, whatever is asked for. If
  the federation never serves, or no cache does, nothing else runs.
- `auth` and `transfers` run late, since the `expired` credential must be 70
  seconds past its expiry.
- `restart` and `keys` run last, since each takes servers, or their keys,
  away for a while.

A suite that doesn't apply to the shape [skips](../glossary.md#skip), and
says why. A [scenario](../glossary.md#scenario) that raises an unexpected
error fails, with the error as its note. A client command still running
after 600 seconds is killed, and fails. The
[watchdog](../glossary.md#watchdog) abandons a stalled pass. Each suite
removes what it wrote from every origin's store afterward.

[What is not tested](what-is-not-tested.md) lists the gaps.

## Token profiles

[Pelican](https://pelicanplatform.org/) understands tokens of two
[profiles](../glossary.md#token-profile), whose scopes differ: the
[WLCG common JWT profile](https://github.com/WLCG-AuthZ-WG/common-jwt-profile/blob/master/profile.md)'s
`storage.read`, `storage.create`, and `storage.modify`, and
[SciTokens 2.0](https://scitokens.org/technical_docs/Claims)'s `read` and
`write`. So each suite runs in two [passes](../glossary.md#pass):

1. The WLCG pass, reported as `<suite>`.
2. The SciTokens pass, reported as `<suite>-scitokens`. Its tokens are what
   `pelican token create --profile scitokens2` mints, with the profile's
   version claim (`ver`) and the `ANY` audience. `--read` grants `read:`,
   and `--write` and `--modify` both grant `write:`, since a SciTokens
   `write` may create, replace, and remove an object.

Each row of the SciTokens pass must fare as its [twin](../glossary.md#twin)
in the WLCG pass does.

A scenario presents tokens of its pass's profile. Its setup, and its checks
of what the stores hold, use WLCG [setup tokens](../glossary.md#setup-token)
in either pass, so that the SciTokens pass tests only what the scenario
presents.

Some scenarios run in the WLCG pass alone, marked `WLCG only` by
`./fed.sh test -l`:

- those that present no storage token of the tests' own minting, such as
  every scenario of `registry`, `collections`, `issuer`, `accounts`, `oidc`,
  and `webapi`;
- [`auth`](authorization.md)'s `create-only`, since SciTokens has no scope
  of create alone;
- every scenario of `keys`, since a second pass would change the keys again
  ([keys](keys.md)).

`./fed.sh test --profile wlcg` (or `scitokens`) runs one pass alone.
`federation`'s `ready` and `caches` try once in the second pass. In a
SciTokens pass alone, a federation that refuses SciTokens but serves a WLCG
token fails them without stopping the run.
