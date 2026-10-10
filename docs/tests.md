# What the Tests Check

`./fed.sh test` runs `test.py` in the dev container. It runs every
basic functionality check that applies to the federation's shape, over
every combination of client, route, namespace, credential, and server
that the shape has. The checks are grouped in suites
(`framework/suites/`). This page describes what each one checks; see the
[README](../README.md#quickstart) for how to run them.


## Suites

The suites run in this order:

| Suite        | Tests                                                                 |
| ------------ | --------------------------------------------------------------------- |
| `federation` | that the federation serves at all, and every director names every cache and every origin, and for a client's direct read (`?directread`) only origins that take direct clients |
| `commands`   | `ls`, `stat`, `delete`, `copy`, `sync`, and `du` ([client commands](#client-commands)) |
| `listings`   | PROPFIND at each origin, director, and cache ([listings](#listings))  |
| `blocks`     | objects and ranges at pstore's and the caches' [block boundaries](#block-boundaries), and overwritten objects |
| `posc`       | interrupted uploads, under `-p origin-posc` or `-p origin-pstore` ([POSC and metadata](#posc-and-metadata)) |
| `metadata`   | metadata events, under `-p origin-metadata` ([POSC and metadata](#posc-and-metadata)) |
| `users`      | who owns what the origins write, under `-p origin-multiuser` or `-p server-unprivileged` ([Unix users](#unix-users)) |
| `owners`     | origin-2, another owner, under `-p topo-multi-owner` ([another owner](#another-owner)) |
| `auth`       | each [credential](#authorization) at each server, in each namespace, and each issuer's keys |
| `transfers`  | gets and puts through `pelican object` and `stash_plugin`, through a cache and direct from an origin, byte-checked ([test data](#test-data)) |

A suite that doesn't apply to the shape is skipped, and says why.
`federation`'s `ready` and `caches` checks run first, whatever else is
asked for; `caches` tries each cache 20 times, 15 seconds apart, while
it fails or doesn't answer. If the federation never serves, nothing
else runs. The
`expired` credential must be 70 seconds past its expiry before it is
presented, so `auth` and `transfers` run last, by which time it usually
is.

These are basic checks, not stress tests: each moves only a few small
objects. Each suite writes a row per test case to
`framework/var/results/` ([Results](ci.md#results)). A scenario that
raises an unexpected error fails, with the error as its note, and the
suite goes on to the next. A client command still running after 600
seconds is killed, and fails. Each suite removes what it wrote from
every origin's store afterward, a `pstore` origin's included.


## Test data

`init` writes 16 objects named `<origin>.<n>` to the origins' storage.
The `transfers` suite makes its batches each time it runs, in
`framework/var/data/transfers/input/<scenario>/<index>`. A batch is one
client invocation. `<ns>` is each namespace, and `<cred>` each of the
eleven [credentials](#authorization) that applies to it:

| Scenario          | Transfers                                                 |
| ----------------- | --------------------------------------------------------- |
| `exist`           | get `/public/data/0.<n>`, with no token                   |
| `dne`             | get `/public/data/9.<n>` (never created): not found; where `/public` isn't exported, from the first exported protected namespace instead, with its `server` token |
| `get-<ns>-<cred>` | get `/<ns>/data/0.<n>`, presenting `<cred>` (`get-public-none` is `exist`); `get-public-<cred>` checks that a configured, possibly bad, token doesn't break a public read, since the client sends none |
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
namespace's scenarios, and `dne`, which gets from that namespace
instead. Without `DirectReads` (`origin-no-direct`), `direct-dne` must
be refused. `./fed.sh test -l transfers` lists the
scenarios, and the summary after a run shows what each was expected to
do.

`exist`, `dne`, and each scenario that a token should let through run
two batches, of one object and then two (a put of two goes to a
collection rather than to an object's URL); the rest run one, of one
object. In the default shape that is 178 scenarios and 222 client
invocations, 16 at a time. Each batch picks distinct objects, uniformly
at random. `framework/testlib/transfers.py` sets these numbers.

The suite mints the tokens and writes new random bytes for each upload.
Before the run, it empties every put collection in every store, a `pstore`
origin's included, and stops if it can't. Afterward, it judges each batch by
the rules in `framework/testlib/transfers.py`. Every file downloaded must
match the object in the origin's store, and every upload what the store
received. Every batch that should be refused must fail by being refused,
with nothing moved: no object's bytes in a file it downloaded, and no upload
in any store. A failure that also shows a server error (HTTP 5xx), a
timeout, a refused connection, not found (error 5011), or a transfer error
(6xxx) is not a refusal, even beside a 401 or 403; nor is a local
`permission denied`. A batch killed after 600 seconds fails as `other`.
Every `dne` batch must fail with not found (error 5011). The summary counts
each scenario's batches that passed, failed, and went unexpectedly, says why
the failed ones failed, and names the batch sizes that had unexpected
results. `verify/problems` lists each unexpected result.

Last, it shows which servers served the successful downloads, by client and
route. In each namespace with downloads meant for a cache, a cache must have
served some, since a director that routed around its caches would otherwise
pass every scenario; and only an origin may serve a direct one. Under
`topo-tiny`, whose cache and origin are one host, both checks are skipped.
Where a namespace takes no direct clients (`origin-no-direct`), every direct
read must be refused.

A `pstore` origin's store is encrypted, and only the running origin can
write it. So `init` writes a plain copy of the objects beside it, in
`data/origin/<N>/data/`, and the `transfers` suite uploads whatever the
store lacks, to `/protected-a` and `/protected-b`, before its gets. Each
has storage of its own in the store, and `/public`, which takes no
writes, reads `/protected-a`'s. Uploads to it are read back from the
origin instead of from disk.


## Authorization

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

Otherwise each token has read, create, and modify on the whole namespace, is
issued by that namespace's issuer, and lasts four hours, so that each tests
one thing (`ns-cross` is `wrong-iss` signed with the other namespace's key).
With `ORIGIN_ENABLE_ISSUER=false`, every namespace has one issuer and no
keys of its own, so the tests skip `wrong-iss`, `ns-jwks`, `ns-other`, and
`ns-cross`, as they do with `auth-external-issuer`, whose key then signs
`server`, `wrong-op`, `wrong-path`, and `expired`. Its JWKS holds only its
own keys, so they skip `jwks` there too: a token that names it, signed with
a key listed only in `Server.IssuerJwks`, would only repeat `unknown`. A
shape that exports only `/protected-a` (`origin-httpsv2`,
`origin-no-direct`) lists `/protected-b`'s key nowhere, so the tests skip
`ns-other` and `ns-cross`, which would only repeat `unknown`. Where a token
decides, only `server`, `jwks`, and `ns-jwks` should be allowed. A token
doesn't always decide:

- **Reads of `/public`** should be allowed whatever the token. The
  servers ignore it, and the clients don't even send one.
- **Writes to a namespace without `Writes`** should be refused whatever
  the token. That is `/public`, and every namespace under
  `origin-no-direct`. `/public` has no issuer at all, so a token claiming
  its issuer URL is trusted by nothing.
- **Requests straight to an origin**, and direct reads through the
  clients, should be refused whatever the token in a namespace without
  `DirectReads`: every namespace under `origin-no-direct`, whose origins
  serve only caches.

The director rejects any expired token it is shown, whatever the
namespace.

- **Through the clients**, the `transfers` suite runs the `get-` and
  `put-` scenarios with `pelican object`, and their `plugin-` twins with
  `stash_plugin`. Its `failures` column says who refused: `client` (it
  had no token to send), `director` (it rejects expired tokens),
  `unsupported` (the director found no origin that allows the operation
  in the namespace, e.g. a put to `/public`), or
  `server` (an origin or cache answered 401 or 403, with no server
  error, timeout, or not found beside it). Any other failure
  is unexpected, since it doesn't show a refusal. A `pelican object get`
  into a directory, as the suite's batches are, stats each object
  first, asking the director without the token, so one whose token is
  refused is `server`, even when the token has expired. With no token,
  `pelican object` tries to acquire one, which
  should fail at once with error 4010, since the test gives it no
  terminal and an empty credential store.
- **At each server**, the `auth` suite sends GET, HEAD, PUT, and DELETE
  to every origin, and GET and HEAD to every cache, over HTTPS, in every
  namespace. An allowed request must return or store the right bytes,
  or remove the object, which the test puts in place just before each
  DELETE (straight in the store, where the namespace takes no writes).
  A refused one must get 401 or 403, from any kind of server, and change
  nothing. The caches are tried only after
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
  pass (but at an origin that takes no direct clients, be refused), and
  then one of its sibling, whose name begins with the object's, must be
  refused. Each cache must first serve the sibling with the `server`
  token, so that the refusal is of an object it holds.

Both suites wait until the `expired` token is 70 seconds past its expiry
before presenting it, since native servers allow 60 seconds of clock
skew. `test.py` mints it as it starts, and runs these suites last, so
the wait has usually passed by then.

`init` creates the four test keys in `framework/var/test-keys/`, and
the public keys of `jwks.pem`, `ns-protected-a.pem`, and
`ns-protected-b.pem` in `framework/var/issuer-jwks/`, which the origins
mount. They name `jwks.pem`'s in `Server.IssuerJwks`
(`framework/config.d/instance/origin-*/`), and each `ns-<ns>.pem`'s in
the `IssuerJwks` of the `/<ns>` export, which `fed.sh` generates. No
server sees any of the private keys. Under `-p topo-multi-origin`,
origin-1 warns that its keys authorize nothing, because both origins
advertise origin-0's issuer.


## Client commands

The `commands` suite tests the client's commands other than `get` and
`put`: `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in `/public`
and `/protected-a` where they apply. Writes to `/public` must be
refused, and so must any command that needs a capability its namespace
lacks (`Listings` for `ls`, `du`, and `sync`; `Writes` for `delete`,
`copy` to the federation, and `sync` to it; `DirectReads` for `copy
--direct`), with nothing changed. `./fed.sh test -l commands` lists the
scenarios.

- The read-only commands work on a small tree that the test puts
  straight in every origin's store, under `/<ns>/data/cmd/<run>/tree/`,
  so that they agree whichever origin the director picks. Listings,
  sizes, checksums (`crc32c` and `md5`, in hex, as `pelican object stat`
  prints them), and `du`'s totals must match it.
- `delete`, `copy`, and `sync` go through the director like any client.
  Under `topo-multi-origin` their effects land on one origin, so a success
  must show in some store, and a refusal in none. A copy between two
  federation URLs is a third-party copy, which the destination origin pulls.
  Every service here has a private address, which origins refuse to pull
  from by default, so `framework/config.d/base/40-origin.yaml` allows it.
- `sync` compares sizes only and never deletes; the scenarios check what
  it uploads by its `--dry-run` output.
- These commands' exit status says little about what went wrong (mostly
  1 or 11), so the tests require only a non-zero one, and judge
  failures by what the commands print, as the `transfers` suite judges
  them.
- `ls -r` must list every object by its path in the tree, once.
- `du --json` must give each collection's bytes, objects, and
  collections, and `du --count`, whose counts only its text output
  shows, must print the same.
- `ls`, `delete`, and `du` of `/protected-a` with no token must be
  refused by the client itself, for want of a token (error 4010),
  before it asks any server; the `auth` and `listings` suites check the
  servers.


## Block boundaries

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
  Where `/protected-a` takes no writes, the objects are put straight in
  the stores instead, and the upload scenarios skip; where it takes no
  direct clients, the origins are read only through the caches, and
  `client-get`'s direct read must be refused.
- How a V2 cache first sees an object decides how it fills (one stream,
  or block by block), so each cache reads one set of objects whole first
  and another in ranges first.
- `mixed-version` overwrites an object of three XRootD blocks at the
  origins after a cache has cached its first 4,080-byte block; each
  later response, to a range in the third XRootD block (which neither
  kind of cache fetched for the first read) and to the whole object,
  must then succeed, and be entirely one version, the same for the
  range as for the whole object.
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
  show the partial state (it sends `Age`, has no readable `.cinfo`, or
  its `.cinfo` shows none or all of the blocks after the first range),
  the row is inconclusive; but an XRootD cache whose `.cinfo` could be
  read after the first range fails if, after the last, it has none, or
  one that doesn't show every block.
- `overwrite-cached` and `overwrite-range-first` overwrite objects at the
  origins once each cache holds them (read whole, or a range first), one
  keeping its size and one growing. Every response, whole or ranged, must
  then be entirely one version, with that version's size. Without
  `Cache-Control`, a cache decides for itself how long its copy stays
  fresh. Under `-p origin-max-age`, every origin must send
  `Cache-Control: max-age=30`, and every cache must then serve the new
  version, and only it, once its copy is stale. The scenarios wait up to
  about 75 seconds for it.


## Listings

The `listings` suite sends PROPFIND straight to every origin, director,
and cache, at depths 0, 1, and infinity, in every namespace, over a
small tree that it uploads under `/<ns>/data/listings/<run>/`.
`./fed.sh test -l listings` lists the scenarios.

- An origin must list the tree exactly. It may refuse Depth infinity
  with 403, as RFC 4918 allows, but must not answer it as Depth 1.
- A director must redirect a PROPFIND to an origin: Depth 0 (a stat)
  for any namespace that can be read, and deeper only with `Listings`
  (Pelican's `director/sort.go`). The origin's listing must then be
  right; where the namespace takes no direct clients, the origin must
  refuse it instead.
- A cache must answer Depth 0 of anything it may read, and relay deeper
  listings through the director to an origin, listing what the origin
  does. `cache-body` sends the propfind body that `pelican object ls`
  sends, which the cache must pass on as it follows the director's
  redirect. A cache may refuse Depth infinity with 403 only if an origin
  does; where no origin may be asked directly, a 403 is accepted, and
  the row's note says so.
- Deeper than Depth 0, any server, directors included, lists only a
  namespace with `Listings`, and an origin answers only direct clients,
  which a namespace without `DirectReads` has none of. A PROPFIND that may
  not be answered must be refused with 401, 403, or 405.
- Every object in a listing must have a `getcontentlength`, 0 included.
- Listing `/protected-a` with no token, or one scoped elsewhere, must be
  refused everywhere; listing `/public` with none must work.
- `new-object` checks that an object shows up at once, by PROPFIND and
  with `pelican object ls`, and is gone once removed.


## POSC and metadata

Both are features of the `posixv2` origin, which is the default. `fed.sh`
refuses them with any other origin. The `posc` and `metadata` suites
run only where they apply, and skip otherwise; `./fed.sh test -l posc
metadata` lists their scenarios.

- **POSC** (`-p origin-posc`): the origin stages each upload in
  `<store>/.pelican-posc/` and renames it into place once it completes.
  The `posc` suite interrupts uploads in several ways and checks what the
  origin answers and what the store shows. It judges an interruption
  only once it has seen the upload under way, from a new staging file;
  an upload refused, or unanswered, before then fails. `hidden` lists
  the export while an upload is staged, so that `.pelican-posc` is there
  to hide.
- **pstore** (`-p origin-pstore`) commits a new version of an object only
  when its upload completes, so the `posc` suite runs there too: the same
  interruptions, judged by what the origins serve, once
  `pstore/objects` has grown to show the upload under way (`chunked`
  sends just over pstore's 1 MiB spill threshold for this). `stalled` and
  `hidden`, which are about POSC's staging, skip.
- **Metadata** (`-p origin-metadata`): the origins send an event for every
  object committed, overwritten, or deleted to the `metadata` recorder. It
  saves each request in `framework/var/data/metadata/` (listed in
  `index.tsv`) and passes it to `metadata-verifier`: Pelican's
  `sample_metadata_server`, built from `PELICAN_TAG`'s source. The recorder
  answers 503 once for objects named `fail-once-*`, and 422 for `reject-*`.
  The `metadata` suite uploads with `--metadata-file` and
  `--metadata-body`, and checks the events, including that
  `object.updated` carries the new size and custom fields, compared
  type for type.
- **Transactional mode** (`-p origin-metadata-tx`): an upload should fail
  with a 5xx if its event can't be delivered, and its object should be
  removed, as Pelican's `docs/metadata-publish-design.md` says.
  The `metadata` suite's `retry` and `reject` check both, and that the
  recorder answered a delivery for the object with 503 or 422.


## Unix users

Under `-p origin-multiuser`, a `posixv2` origin reads and writes each
object as the Unix user that the request's token maps to; under
`-p server-unprivileged`, every server drops to the `pelican` user. The
`users` suite puts an object straight to each `posixv2` origin, and
checks who owns the file it leaves:

- `owner`: with the tests' token, the `pelican` user (uid 10941), which
  `fed.sh` maps the tests' subject to under multiuser.
- `default-user`: under multiuser, with a token for another subject,
  the `xrootd` user (uid 10940), `Origin.ScitokensDefaultUser`.

Whether a bind mount shows a file's owner depends on the host, so the
suite first gives a file of its own to uid 10941; where the store
doesn't show that, its rows are inconclusive.


## Another owner

Under `-p topo-multi-owner`, origin-2 has a key and an issuer of its
own, and exports `/other`, like `/protected-a`, and `/public/other`,
like `/public` and nested in it (as far as `ORIGIN_NAMESPACES` exports
those). `fed.sh` describes it in `framework/var/generated/owners` and
`owner-exports`; the other suites test only the origins' namespaces. The
`owners` suite checks:

- `ready`: origin-2's objects come back through the federation and from
  each cache, with 20 tries, 15 seconds apart. It runs first, whatever
  else is asked for; if origin-2 never serves, the suite stops.
- `keys`: origin-2's server-wide JWKS, and its issuer's for `/other`,
  hold its keys, and none of the origins' or the test keys; the origins'
  hold none of its.
- `routing`: each director sends a client's direct read (`?directread`)
  of origin-2's prefixes to it alone, and of the origins' to them alone;
  where an export takes no direct clients, the director must find no
  origin (405) and name none, not even the origins', whose `/public`
  encloses `/public/other`. Through each cache,
  `/public/other/...` must be origin-2's object, never a decoy that
  origin-0's `/public` holds at `other/...`, and
  `/public/others-<run>/...` origin-0's, since a prefix matches whole
  path segments.
- One scenario per credential of `framework/testlib/owners.py`, on both
  sides of each pair (`/other` and `/protected-a`; `/public/other` and
  `/public`), with GET, HEAD, PUT, and DELETE straight to the side's
  origins, and GET and HEAD to each cache:

  | Credential    | Token                                                          |
  | ------------- | -------------------------------------------------------------- |
  | `owner`       | origin-2's key and issuer                                      |
  | `origins`     | the origins' key and issuer                                    |
  | `owner-key`   | origin-2's key, naming the origins' issuer                     |
  | `origins-key` | the origins' key, naming origin-2's issuer                     |
  | `jwks`        | `test-keys/jwks.pem`, naming origin-2's issuer                 |
  | `ns-jwks`     | `test-keys/ns-protected-a.pem`, naming origin-2's issuer       |
  | `none`        | none                                                           |

  Where a token decides, only `owner` on origin-2's side and `origins`
  on the origins' should be allowed; the capabilities then apply as in
  [authorization](#authorization). The `/public` pair gets only `owner`,
  `origins`, and `none`.
- `get`, `direct-get`, `put`, `cross-get`, and `cross-put`: the client
  moves origin-2's objects through a cache (served by one) and from
  origin-2 (served by it), uploads with origin-2's token, and presents
  each owner's token in the other's namespace, which must be refused
  with nothing stored.


## Unit tests

The framework's own unit tests cover only the bugs in it that would let
a broken Pelican pass unnoticed. Most bugs in the framework would make a
working Pelican fail instead, and running the tests finds those. So the
unit tests check:

- That the checks that judge an answer aren't too lenient, for example
  by taking a timeout for a refusal, or a mix of two versions of an
  object for one of them.
- That nothing is quietly left untested: no suite or credential is
  skipped in a shape where it applies, every scenario moves something,
  and the smoke shapes cover every pair of choices.
- That a suite that stops early, or a failed case, still counts as a
  failure.

They need no federation:

```sh
python3 -B -m unittest discover -s framework -t framework
```
