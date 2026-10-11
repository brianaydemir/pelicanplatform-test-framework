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
| `federation` | that the federation serves at all, and every director names every federated cache and every origin, and for a client's direct read (`?directread`) only origins that take direct clients; what its redirects tell a client of a namespace, and where they lead a plain HTTP client, bare paths included ([redirects](#redirects)); a Pelican server's discovery document; under `-p origin-broker`, the connection broker; with several origins, that an object one of them alone holds is routed there, and comes back; under `-p registry-approval`, that nothing is routed to before its registration is approved; and that a server its admin declares down is routed to by no director until the downtime ends ([downtime](#downtime)) |
| `selftest`   | the tests' own helpers ([self-tests](#self-tests)): reading a V2 cache's `X-Transfer-Status` trailer, signing tokens of any claims, and reading `/metrics` |
| `commands`   | `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, and what `get` and `put` do with what already exists ([client commands](#client-commands)) |
| `transfer-api` | origin-0's transfer API, under `-p origin-transfer-api`: authentication, credentials, server-run copies, jobs, and confinement to the exports (`local-get` and `local-put`, which Pelican's main does not confine, so they fail there) |
| `listings`   | PROPFIND at each origin, director, and cache, and a GET of a collection ([listings](#listings)) |
| `names`      | objects whose [names](#names) a URL or a listing must encode, beside decoys |
| `blocks`     | objects and ranges at pstore's and the caches' [block boundaries](#block-boundaries), through each server and both clients; digests, and an upload the client must verify by one; conditional reads and writes; objects overwritten, given up on, corrupted in a cache's store, or written through a cache; and, under `-p cache-small`, objects evicted from a cache |
| `tiering`    | V2 caches' tiering of large objects to object storage, under `-p cache-tiered`, including a copy replaced in the bucket |
| `posc`       | interrupted and racing uploads, under `-p origin-posc`, `-p origin-atomic-uploads`, or `-p origin-pstore` ([POSC and metadata](#posc-and-metadata)) |
| `metadata`   | metadata events, under `-p origin-metadata` ([POSC and metadata](#posc-and-metadata)) |
| `users`      | who owns what the origins write, under `-p origin-multiuser` or `-p server-unprivileged` ([Unix users](#unix-users)) |
| `owners`     | origin-2, another owner, under `-p topo-multi-owner` ([another owner](#another-owner)) |
| `sitelocal`  | cache-2, a site-local cache, under `-p topo-site-local-cache` |
| `collections` | collections: prefixes with access control of their own, granted through the origins' embedded issuer ([collections](#collections)) |
| `issuer`     | the origins' embedded issuer: what it grants of what a client asks for, through each flow, and its refresh tokens ([issuer](#issuer)) |
| `auth`       | each [credential](#authorization) at each server, in each namespace; each issuer's keys; tokens of one storage scope, for another audience, and in the query string; paths and symbolic links that climb out of a namespace; and what a cache's refusal tells a client |
| `transfers`  | gets and puts through `pelican object` and `stash_plugin`, through a cache and direct from an origin, byte-checked ([test data](#test-data)) |

A suite that doesn't apply to the shape is skipped, and says why.
`federation`'s `ready` and `caches` checks run first, whatever else is
asked for; `caches` tries each cache 20 times, 15 seconds apart, while
it fails or doesn't answer. If the federation never serves, nothing
else runs. The
`expired` credential must be 70 seconds past its expiry before it is
presented, so `auth` and `transfers` run last, by which time it usually
is.

Under `-p registry-approval`, nothing serves until an administrator
approves its registration, so before each of its tries `ready` approves
every pending registration through the registry's admin API, as its
built-in `admin`, with a token like the one `pelican registry` mints
(`credentials.mint_user()`). Before approving, it asks each director
where it would send the object: `approval` fails if any named a server
whose registration was still pending, since the directors refuse the
advertisements of such a server. On a rerun, nothing is pending, and
`approval` says so.


## Redirects

A plain HTTP client, such as curl, asks a director for an object and
follows the redirect (307) it gets, presenting its token again where it
lands; the Pelican client asks for a cache or an origin by name
(`/api/v1.0/director/object/` or `/origin/`), but curl and browsers ask
for the bare path, which the director sends to a cache, or under
`-p director-default-origin` (`Director.DefaultResponse`) to an origin.
The `federation` suite's `redirects` check follows each director's
redirects for each exported namespace, with the namespace's `server`
token (none for `/public`):

- A GET and a HEAD of the object's bare path must land at a cache, or
  under `-p director-default-origin` at an origin; one with
  `?directread` at an origin, where the namespace takes direct clients,
  and otherwise find none (405), as the Pelican client's `--direct`
  asks; and the object and origin routes at a cache and an origin.
- What lands at a cache must be served: the object's bytes, or for a
  HEAD its `Content-Length`. What lands at an origin must be served
  only where the namespace takes direct clients, and otherwise refused
  (401 or 403), as the origin refuses every direct client there.
- A bare-path PUT of a new object must land at an origin, where the
  namespace takes writes, which must then store it; where it takes
  none, the director must find no origin, or the one it names refuse,
  and no origin may hold the object.

`hints` checks what the same redirects tell the client about the
namespace ([authorization](#authorization)).


## Downtime

A server's admin declares it down through its web API
(`/api/v1.0/downtime`), as before maintenance; the server tells the
registry, from which the directors learn of it, and stop sending
clients there. The `federation` suite's `downtime` check declares the
last federated cache down for an hour, as its admin, with a token like
`pelican cache introspect`'s; within 150 seconds no director may name
it for the test object (the directors ask the registry every minute,
`Director.RegistryQueryInterval`); then it ends the downtime, after
which, as soon, every director must name it again. With several
origins, it does the same for the last origin. The check is skipped in
the `tiny` topology, whose servers and director are one process.

These are functional checks, not stress tests: each moves a few
objects, the largest about 16 MiB, but for `blocks`'s `cache-evicted`,
which under `-p cache-small` moves about 200 MiB through each cache, to
make it evict. Each suite writes a row per test case
to
`framework/var/results/` ([Results](ci.md#results)). A scenario that
raises an unexpected error fails, with the error as its note, and the
suite goes on to the next. A client command still running after 600
seconds is killed, and fails. Each suite removes what it wrote from
every origin's store afterward, a `pstore` origin's included.


## Self-tests

The `selftest` suite checks the helpers in `framework/testlib/` that
other suites build checks on, against the running federation, so that a
failure there shows as the helper's, not as the checks'. It puts an
object of its own in `/protected-a/data/selftest/<run>/` at every
origin, reads it, and removes it.

- `trailers`: a V2 cache that a client asks for the transfer's status
  (`X-Transfer-Status: true` and `TE: trailers`) sends its body in
  chunks, announces `Trailer: X-Transfer-Status`, and ends with that
  trailer, `200: OK` for a transfer that went well
  (`local_cache/persistent_cache_api.go`); it is how a cache reports a
  failure once it has sent its headers. `framework/testlib/web.py`
  keeps the trailers that Python's `http.client` throws away. A GET of
  the object at each federated V2 cache, so asked, must get them; one
  not so asked must get none. XRootD caches are passed over.
- `jwt`: `framework/testlib/jwt.py` makes tokens that `pelican token
  create` cannot: any claim present or absent and of any JSON type, any
  `kid` or none, and signatures by ES256 or RS256 (with the `openssl`
  command), HS256 (with any bytes as the secret), or `none`. Its token
  with the claims and header of the `server` token, signed with the
  origins' key, must read the object at every origin, and the same
  token, expired ten minutes before, be refused. The claims' names, and
  the values that are the same from token to token, and the header,
  must first match the `server` token that `pelican token create`
  minted. It skips where the origins take no direct clients.
- `metrics`: a GET of the object straight to each native origin must
  raise its `pelican_http_requests_total` with `server_type="origin"`,
  `method="GET"`, and `code="200"` by at least one
  (`metrics/http_metrics.go`). Other requests may raise it too, so no
  check of a counter here is exact. It skips for an XRootD origin, which
  keeps no such counter, and where the origins take no direct clients.


## Test data

Each namespace has storage of its own: a directory of each origin's
store, `framework/var/data/origin/<N>/<namespace>/`, which its export's
`StoragePrefix` leads to (`/data/<namespace>` in the origin, or a path
or a bucket of the `webdav` or `s3` backend). `init` writes 16 objects
named `<origin>.<n>` to each namespace's `data/`. An object's bytes are
its path in the federation, e.g. `/protected-a/data/0.3`, so no two are
alike, and a server that answers from another namespace's storage is
caught. The suites likewise give each namespace's objects bytes of their
own.

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
in any store. The client downloads into a temporary file beside the
object's, and renames it only once the object is complete, so no batch
may leave anything else in its directory, and a get that fails must
leave no file of the object's name. A failure that also shows a server
error (HTTP 5xx), a
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
`data/origin/<N>/<namespace>/data/`, and the `transfers` suite uploads
whatever the store lacks, to `/protected-a` and `/protected-b`, before
its gets. Each has storage of its own in the store, and `/public`, which
takes no writes, reads `/protected-a`'s: the one place two namespaces
share storage. Uploads to it are read back from the origin instead of
from disk.


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
  namespace. An allowed request must return or store the right bytes (a
  HEAD, with the object's `Content-Length`), or remove the object, which
  the test puts in place just before each DELETE (straight in the store,
  where the namespace takes no writes). A refused one must get 401 or
  403, from any kind of server, and change nothing. Each namespace has
  a test object of its own bytes, so a response with another
  namespace's fails, allowed or refused. The caches are tried only after
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
- **One storage scope at a time**, the `auth` suite's `create-only` and
  `modify-only` checks present tokens with only `storage.create` or only
  `storage.modify` in `/protected-a`, straight to every origin. As the
  WLCG token profile says, `storage.create` may create an object, but
  neither replace nor delete one; `storage.modify` may do all three. Each
  write is of an object of its own, put in place first where it must
  exist. A refused write must leave the object as it was. Pelican's
  native origin authorizes every PUT as a create, without asking whether
  the object exists (`getActionFromMethod` in
  `origin_serve/handlers.go`), so a `storage.create` token replaces an
  object, and `create-only`'s `put-over` row fails there, on main and
  v26.0.0-rc.3 alike.
- **Out of a namespace**, the `auth` suite's `traversal` check sends
  every origin and cache paths that begin in one exported namespace and
  climb out of it with `..`, spelled as is, as `%2e%2e`, `%2E%2E`,
  `.%2e`, and `%252e%252e`, and with an encoded slash (`..%2f`), each
  with the start's `server` token (none for `/public`). No answer may
  hold what it climbs toward: another protected namespace's test object;
  a file in each origin's store beside the namespaces' storage; or the
  origin's signing key, in `/fed/issuer-keys`, beside its store. The
  last two stand for what a server would expose if it let a path out of
  an export's storage. Its `symlink` rows ask the same of symbolic
  links, which the check makes in each store on disk, in the first
  protected namespace's storage: one to the file beside the namespaces'
  storage, by a relative path, and one to each origin's signing key, by
  the absolute path its container sees. Pelican's `posixv2` origin
  refuses a link that leaves its export (`os.Root`); an XRootD `posix`
  origin follows it, so the row fails there; the rclone backends and
  the lab server skip links.
- **What a refusal tells a client**, the `auth` suite's `hints` check:
  a GET of the test object at each V2 cache with no token must be refused
  with the headers a director's redirect carries, `X-Pelican-Namespace`,
  naming the namespace and that it takes a token, and
  `X-Pelican-Authorization`, naming its issuer, since a client that named
  the cache itself (`Client.PreferredCaches`) heard them from no director.
  Pelican's main sends them; its releases through v26.0.0-rc.3 do not, so
  the rows fail there. An XRootD cache sends none, and is passed over. The
  `federation` suite's `hints` check asks the same of each director's
  redirects, for a cache and for an origin, in every namespace: the
  issuer must be the namespace's, and `/public`, which has none, must
  name none.
- **A token in the query string**, the `auth` suite's `query-token`
  check: an XRootD cache forwards a client's token to an origin as
  `?authz=Bearer%20<token>` (older ones bare), and a director's redirect
  carries it the same way, so every origin and cache must take a token
  in the `authz` query parameter, with or without the `Bearer ` prefix,
  as it takes one in the header: a GET of the test object in
  `/protected-a` with the `server` token so must pass, and one with the
  `unknown` token be refused. An origin that takes no direct clients
  refuses both.
- **A token for another audience**, the `auth` suite's `audience` check:
  a resource server must check a token's `aud` against its own, as the
  WLCG token profile says. Every other token here names the profile's
  "any" audience; this one, like `server`'s but for
  `https://elsewhere.invalid`, must be refused by every origin, for a
  GET of the test object and a PUT of a new one, which must store
  nothing. Only the origins are asked (Pelican's `origin_serve/authz.go`,
  and the audience in an XRootD origin's scitokens configuration): a
  cache serves tokens minted for any origin, so what it should make of
  an audience is not settled.

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

The `commands` suite tests the client's commands beyond the `transfers`
suite's: `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in `/public`
and `/protected-a` where they apply; and `get` and `put` where Pelican's
`docs/object-transfer-semantics.md` says what each must do with what
already exists. Writes to `/public` must be refused, and so must any
command that needs a capability its namespace lacks (`Listings` for
`ls`, `du`, `sync`, and recursive `get`; `Writes` for `delete`, `put`,
`copy` to the federation, and `sync` to it; `DirectReads` for `copy
--direct`), with nothing changed. `./fed.sh test -l commands` lists the
scenarios.

- The read-only commands work on a small tree that the test puts
  straight in every origin's store, under `/<ns>/data/cmd/<run>/tree/`,
  so that they agree whichever origin the director picks: the same paths
  and sizes in each namespace, but bytes of its own. Listings, sizes,
  checksums (`crc32c` and `md5`, in hex, as `pelican object stat` prints
  them, those the origins' storage reports; see
  [block boundaries](#block-boundaries)), and `du`'s totals must match
  it, `stat`'s modification time must be the file's, within a minute,
  and a copy from `/public` must hold `/public`'s bytes.
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
- `get` onto a local file larger than the object must leave the object
  alone in it (row G1 of that document), and a failed `get` onto it must
  leave it as it was. A `get` of a collection without `-r` must be
  refused, rather than write the listing into a file (G4); `get -r` lays
  the collection out flat, in an existing directory or a new one (G5,
  G6).
- With overwrites off, as by default, `put` to an existing object must be
  refused, and change nothing (P2); with them on, a smaller file put
  over an object must replace it whole. `put` of a file to a collection
  lands in it by the file's name (P3); of a directory without `-r`, it
  must be refused, and upload nothing (P4); and `put -r` of a tree, with
  an empty file in it, lays it out flat, under a new collection or an
  existing one (P5, P6).


## Block boundaries

The `blocks` suite writes objects of sizes on either side of the block
boundaries of pstore (an origin's encrypted store), the V2 cache, and
the XRootD cache, from 0 bytes to just over 8 MiB, then reads them back
from each origin, through each cache, and through both clients, whole
and in byte ranges that straddle the boundaries.
`framework/testlib/blocks.py` lists the sizes and ranges, and the facts
about Pelican's stores behind them: 4,080-byte blocks, pstore's inline,
buffered, and streamed tiers, and the XRootD cache's 128 KiB blocks.

- Each object goes straight to every origin, with a `Content-Length` or
  in chunks with none, which is how the client uploads; `pelican object
  put` and `stash_plugin` upload every size too. On a `pstore` origin,
  `/metrics` must show each upload in the tier its size predicts. Where
  `/protected-a` takes no writes, the objects are put straight in the
  stores instead, and the upload scenarios skip; where it takes no
  direct clients, the origins are read only through the caches, and
  the clients' direct reads must be refused.
- `put-checksum` runs `pelican object put --require-checksum` of every
  size, naming the first algorithm the origins' storage reports
  (`--checksum-algorithm`; the client asks for `crc32c` otherwise). The
  client verifies each upload against a digest the origin reports, so
  the command must succeed, and leave the objects intact, for the
  objects whose digests the storage reports, and fail for the rest,
  rather than call an unverified upload a success: every object where
  the storage reports none (below), and on an `s3v2` origin those over
  5 MiB, which the client, declaring no length, sends in parts, of
  which the S3 service keeps no MD5 (`framework/testlib/stores.py`).
- How a V2 cache first sees an object decides how it fills (one stream,
  or block by block), so each cache reads one set of objects whole first,
  another in ranges first, and one each through `pelican object get`
  and `stash_plugin` first.
- `cache-abandoned` starts a cold read of an object through each cache,
  and gives up after 50,000 bytes; those bytes, and the object's last
  block and the whole object afterward, must be the object's.
- `cache-corrupted` has each V2 cache spoil its own copy of an object it
  holds whole, through its chaos API (`Cache.EnableChaosAPI`, which the
  framework turns on; Pelican v7.27.0 and v26.0.0-rc.0 and later, so the
  scenario skips on an older cache): a block corrupted in its store, and
  bytes cut from the end of another object's chunk file. A read of the
  spoiled part, and of the whole object, must then come back as the
  object's bytes or fail, never as other bytes, and within 30 seconds
  the whole object must come back intact, fetched afresh. An XRootD
  cache has no such API, and is passed over.
- `digests` asks every server for each object's digest by each
  algorithm in turn (`Want-Digest`: `crc32c`, `crc32`, `adler32`, `md5`,
  `sha`, `sha-256`), by HEAD, and by GET for `crc32c`. Every digest a
  server reports must be the object's, read as the client reads it
  (which knows `crc32c`, `crc32`, `md5`, and `sha`), since the client
  fails a download whose bytes don't match. Each origin must
  report `crc32c` and `md5`, but for what its storage cannot: XRootD's
  S3 and HTTPS plugins and the `httpsv2` and `ssh` backends compute none,
  an XRootD origin under `-p origin-multiuser` no `crc32c`,
  and `s3v2` reports only the MD5 that the S3 service holds, which it
  does not for an object over 5 MiB uploaded with no `Content-Length`,
  in parts (`framework/testlib/stores.py`); a cache need report none,
  and the row's note names those that report none. `digest-overwrite` then
  overwrites an object at each origin, by a PUT at once and by a write in
  the store a second later: a native origin keeps digests by the file's
  modification time,
  to the second, and must not report a stale one; and its `ETag`, by
  which a cache tells that an object has changed, must change too.
- Ranges past an object's end: a suffix longer than the object is all
  of it, and a range that runs past the end stops at its last byte; one
  that starts at or past the end (`past-end`) must get 416, or, from a
  server that ignores `Range`, the whole object. In a set of ranges, one
  past the end is passed over, as RFC 9110 says.
- `conditional` sends each server conditional GETs of two objects (RFC
  9110): with `If-None-Match` naming the `ETag` the server sent, among
  others, weakened (`W/`), or as `*`, and with `If-Modified-Since` at
  the `Last-Modified` it sent or later, the server may answer 304, or,
  ignoring the condition, 200 with the whole object; with
  `If-None-Match` naming another ETag, or `If-Modified-Since` long ago,
  it must answer 200 with the whole object. A 304 to a condition that
  doesn't hold would leave a client with stale bytes. The row's note
  says what each server honored, and whether it sent no `ETag` or
  `Last-Modified`, whose conditions are then left out.
- `conditional-put` sends each origin that takes writes from direct
  clients conditional PUTs: with `If-None-Match: *`, a new object is
  created, and a second PUT refused (412), changing nothing; with
  `If-Match` naming the object's `ETag`, it is replaced, and with the
  ETag it replaced, or another, the PUT is refused, changing nothing;
  and with `If-Match: *`, a PUT of an object that doesn't exist is
  refused, storing nothing. A server that ignores a condition on a PUT
  would let two writers overwrite each other unawares; Pelican's native
  origins check it, and XRootD's HTTP server does not, so the row fails
  on an XRootD origin.
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
  origins once each cache holds them (read whole, or a range first): one
  keeping its size, one growing, and one shrinking. Every response, whole
  or ranged, must then be entirely one version, with that version's
  size, and a whole response's digest (`Want-Digest`), if one comes, that
  version's: a stale one would fail the client's check of bytes that are
  right. Without
  `Cache-Control`, a cache decides for itself how long its copy stays
  fresh. Under `-p origin-max-age`, every origin must send
  `Cache-Control: max-age=30`, and every cache must then serve the new
  version, and only it, once its copy is stale. The scenarios wait up to
  about 75 seconds for it.
- `write-through`: a V2 cache relays a PUT or DELETE to an origin through
  the director, and drops its own copy. Of an object each holds, a PUT
  with a read-only token must be refused, with nothing changed; one with
  the `server` token must reach an origin, and once every origin holds
  what it wrote, the cache must serve that at once. Likewise a DELETE,
  after which the cache must answer 404. Where `/protected-a` takes no
  writes, the PUT must be refused. An XRootD cache takes no writes, so
  its PUT must be refused, and change no origin.
- `cache-evicted`, under `-p cache-small`, which bounds each V2 cache's
  store (`Cache.HighWaterMark` 128 MiB, `Cache.LowWaterMark` 96 MiB;
  Pelican's defaults are a share of the whole disk, which no suite could
  fill): each cache reads, cold and whole, enough 8 MiB objects to exceed
  its high water mark by half. Within 60 seconds its store
  (`data/cache/<N>/persistent-cache/objects/`) must have come back under
  the mark, and not fallen below the low one by more than an object; then
  every object must still come back whole and in a range, as must the
  first by `pelican object get --cache`. An XRootD cache measures its
  filesystem, which a bind mount cannot bound, so `fed.sh` refuses the
  preset with `cache-xrootd`.


## Listings

The `listings` suite sends PROPFIND straight to every origin, director,
and cache, at depths 0, 1, and infinity, in every namespace, over a
small tree that it uploads under `/<ns>/data/listings/<run>/`. Each
namespace's tree also holds an object only it holds, `only-<ns>`, so
that a server that lists another namespace's lists the wrong tree.
`./fed.sh test -l listings` lists the scenarios.

- An origin must list the tree exactly. It may refuse Depth infinity
  with 403, as RFC 4918 allows, but must not answer it as Depth 1. An
  object's `getlastmodified` must be when its file was written, within a
  minute, where the store is on disk: the origins and the tests keep
  different time zones (`TZ_STORAGE`), so a time rendered in the wrong
  one is hours off.
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
- `collection-get` asks every server for a collection by GET, as a naive
  client would, which is no object (Pelican issue #1706): an origin,
  asked without `Accept: text/html`, refuses (a native origin, 405) or
  answers a listing (an XRootD origin, HTML), never an object's bytes,
  and asked with it answers an HTML listing. A federated cache must not
  keep what an origin answers as the collection's object, which every
  later request for that path would then get: its answer must be no
  object's bytes; a V2 cache's 200 must be typed
  `application/octet-stream`, with `X-Content-Type-Options: nosniff`,
  so that a browser never runs a listing as a page on the cache's own
  web origin, and carry no `Age`, on a second GET either, since a V2
  cache sends `Age` only for an object it holds whole; and the object
  in the collection must come back intact afterward.


## Names

The `names` suite puts objects whose names a URL or a listing must
encode under `/protected-a/data/names/<run>/`: a space, `%`, `+`, `#`,
`?`, `&`, `;`, quotes, angle brackets, brackets, `*`, and letters beyond
ASCII; and two that need no encoding but are easily mishandled, a name
that begins with a dot and one of 200 characters. Each sits beside
decoys, whose names a server or client that
mishandles the encoding would take for its own: `x%41y` beside `xAy`,
`a+b` and `a%20b` beside `a b`, `hash#tag` beside `hash`, and
`what?q=1` beside `what`. Every object is a different size. A URL
percent-encodes every character of a name but letters, digits, and
`-._~`, and so does what the client is given.

- A PUT of each name must land in each store on disk as a file of exactly
  that name.
- Every origin and cache must serve each object by its name: any bytes
  but its own fail, and a decoy's are named.
- Every origin and federated cache must list each name once, decoded,
  with its size, as must `pelican object ls`.
- `pelican object get` of every object must write a file of each one's
  name, with its bytes, and nothing else; `pelican object put` of a file
  of each name must land as the object of that name.


## POSC and metadata

POSC and metadata are features of the `posixv2` origin, which is the
default, and XRootD's POSC of its `posix` origin; `fed.sh` refuses each
with any other. The `posc` and `metadata` suites run only where they
apply, and skip otherwise; `./fed.sh test -l posc metadata` lists their
scenarios.

- **POSC** (`-p origin-posc`): the origin stages each upload in
  `<store>/.pelican-posc/` and renames it into place once it completes.
  The `posc` suite interrupts uploads in several ways and checks what the
  origin answers and what the store shows. It judges an interruption
  only once it has seen the upload under way, from a new staging file;
  an upload refused, or unanswered, before then fails. While an
  overwrite is under way, the origin must still serve the old object;
  and of two whole uploads of one object at once (`concurrent`), the
  object must end up all one of them. `hidden` lists the export while an
  upload is staged, so that `.pelican-posc` is there to hide: by
  PROPFIND, as `pelican object ls` does, and as a browser does, by a
  GET for HTML, whose links it reads. Both must list `data/`, and
  neither may show `.pelican-posc`. Pelican hides the staging directory
  from PROPFIND, since POSC's filesystem says it isn't there, but its
  HTML listing reads the export's directory itself
  (`streamDirectoryListing` in `origin_serve/handlers.go`) and shows
  it, so the row fails there, on main and v26.0.0-rc.3 alike.
- **XRootD's POSC** (`-p origin-atomic-uploads`): `Origin.EnableAtomicUploads`
  stages each upload in `Origin.UploadTempLocation`, which Pelican
  requires on the exports' filesystem but in none of them, so `fed.sh`
  puts it beside their storage in each origin's store,
  `framework/var/data/origin/<N>/.in-progress/`. The `posc` suite runs
  the same interruptions, judged by the staging files there and by the
  store. `hidden` skips, since nothing is staged inside an export, and
  `stalled` checks no metric, which only the native POSC has.
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

Under `-p origin-multiuser`, a POSIX origin (`posixv2`, or XRootD's
`posix` through its multiuser plugin) reads and writes each object as the
Unix user that the request's token maps to; under
`-p server-unprivileged`, every server drops to the `pelican` user. The
`users` suite puts an object straight to each origin, and checks who
owns the file it leaves:

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


## Collections

A collection is a prefix of an exported namespace with access control
of its own (Pelican's `docs/collections-design.md`): an owner, an admin
group, groups and users that may read or write, and a visibility.
Collections live in an origin's database, and the origin's embedded
OAuth2 issuer turns them into storage scopes: a token it mints for a
user carries `storage.read` (and, for a writer, `storage.modify` and
`storage.create`) of each collection the user may use, and nothing for
the rest of the namespace. The `collections` suite needs that issuer
(`ORIGIN_ENABLE_ISSUER`, on by default) and a protected namespace, and
skips with the external issuer (`-p auth-external-issuer`).

The suite's users are records of the issuer origin (origin-0, or `fed`),
which its admin creates once and later runs reuse: `test-curator`, whom
`Server.CollectionAdminUsers` in `framework/config.d/base/40-origin.yaml`
makes a collection administrator, and seven ordinary users, `test-owner`,
`test-writer`, `test-reader`, `test-outsider`, `test-sharer`, `test-guest`,
and `test-heir`. The suite acts as each through a bearer token that the
origin's web API takes as theirs: signed with the origin's own key and
naming its local issuer, as `pelican cache introspect` mints the admin's
(`credentials.mint_user()`), so no login or identity provider is needed.
For data, each user gets tokens from the namespace's issuer through its
device flow (RFC 8628), which the suite approves at the issuer's
verification page as the user, as Pelican's own tests do
(`framework/testlib/issuer.py`). Since the issuer binds a client that
registers itself to the first user who approves a flow for it, and
allows an address five registrations, then one a minute, the suite
registers none: the admin creates one client of each issuer through the
issuer's admin API. The `client` and `cli` scenarios register two each,
as the Pelican client and CLI do that themselves.

The embedded issuer's `Issuer.AuthorizationTemplates`, which grant a
user beyond their collections, grant only the built-in `admin` and the
`issuer` suite's users and groups (`40-origin.yaml`), so that this
suite's users get only what collections grant them.

Each run makes groups of its own, owned by `test-owner`
(`test-readers-<run>`, `test-writers-<run>`, and `test-admins-<run>`, for
`test-heir`), and collections under `/<namespace>/data/collections/<run>/`:
`alpha`,
private, `test-owner`'s, with the admins group as its admin group, the
readers allowed to read and the writers to write, and sharing on; and
`beta`, public, `test-owner`'s, which every authenticated user may read.
Each gets an object (`alpha/shared/obj` too, for a share), and `decoy/obj`
sits beside them in no collection. `./fed.sh test -l collections` lists
the scenarios. In short:

- `users`, `create`, `visibility`, `acl`, `manage`, and `ownership`
  check the collections API, through the origin's web API: who may
  create a collection (a collection administrator, with a namespace
  within an export), see it (anyone, if public; otherwise its owner,
  admin group, and grantees), change, rename, and describe it (owner,
  admin group, and writers), grant and revoke access (owner and admin
  group), delete it (the owner alone), and hand it on (the owner alone,
  by setting its owner, or by an invite that one user redeems once).
  A collection one may not see is "not found" (404), so its existence
  is hidden. Where a scenario changes things, it makes a collection of
  its own.
- `tokens` checks what the issuer mints: the storage scopes of each
  user's collections alone, within the namespace, and from the origin's
  local issuer `collection.create` only for a collection administrator,
  which the collections API honors.
- `data` presents those tokens, straight to every origin (where the
  namespace takes direct clients) and through the federation with
  `pelican object`: reads and writes within a collection, and nothing
  beside it. `client` has `pelican object get` acquire its own token
  from the issuer, approved as the user; `test-outsider`'s gets nothing.
- `shares`: `test-sharer`, who may read `alpha`, shares `alpha/shared`
  with `test-guest`, whose token then carries `share.access:/<share>` and
  the share's `storage.read`, and whose access is the lesser of the
  share's and `test-sharer`'s own: write on the share grants nothing
  until `test-sharer` may write `alpha`, and nothing at all once
  `test-sharer` may not read it. A share of a share, and a share of a
  collection with sharing off, are refused; a multiuser origin
  (`-p origin-multiuser`) refuses every share (409), since its data path
  cannot act as the share's owner.
- `expiry` and `revoke`: a grant with an expiry grants nothing after it,
  and a revoked grant is gone from the next token. The old token keeps
  its access until it expires, by design: a token is not revoked with
  the grant it came from.
- `cli`: `pelican-server origin collection` creates, lists, changes,
  grants on, and deletes a collection through the device flow, approved
  as `test-curator`, and fails for `test-outsider`. The command is
  server-only: Pelican's client lacks it. The scenario runs the dev
  container's own `pelican-server`, if it has one, and otherwise
  `framework/var/bin/linux/pelican-server`, which `./fed.sh init`
  fetches, and skips if there is neither. The CLI manages the first
  origin the director lists, so the scenario skips where that is not
  the issuer origin; it and `client` skip for a standalone origin,
  which has no director.

The run's collections, groups, and objects are removed after the suite;
the users stay.


## Issuer

The origins' embedded OAuth2 issuer (`oauth2/issuer/` in Pelican) mints
a user's tokens for a namespace: what it grants is what the user is
allowed, narrowed to what the client asked for and may carry. A user
is allowed what `Issuer.AuthorizationTemplates` say, by name and by
group, and what their collections and shares grant
([collections](#collections)). A template's prefix is within the
namespace of each issuer that applies it, as its tokens' scopes are,
and an export may have templates of its own, which replace the global
ones there. The
`issuer` suite checks what the issuer grants of each request, through
each of its flows, and how its refresh tokens behave. It needs the
embedded issuer and a protected namespace, as `collections` does, and
skips with the external issuer.

`framework/config.d/base/40-origin.yaml` grants, within each namespace
that has an issuer:

| Prefix                       | Actions               | To                                               |
| ---------------------------- | --------------------- | ------------------------------------------------ |
| `/`                          | read, create, modify  | the built-in `admin`                             |
| `/data/issuer/home/$USER`    | read, create, modify  | `test-analyst` and `test-member`                 |
| `/data/issuer/projects/$GROUP` | read, create        | the groups `test-project-a` and `test-project-b` |
| `/data/issuer/archive`       | read                  | any group named `test-project-*`                 |

Under `-p origin-export-templates`, `fed.sh` gives the `/protected-b`
export templates of its own, read on `/data/issuer/export` for
`test-stranger`, so that its issuer grants that and nothing else.

The suite's users are records of the issuer origin (origin-0, or
`fed`), which its admin creates once and later runs reuse:
`test-analyst`, `test-member`, and `test-stranger`, and `collections`'s
`test-curator`, a collection administrator. `test-member` is in both
groups, which the admin creates once, eligible to match templates. A
group an ordinary user creates may not match a template until an
administrator makes it eligible, so each run `test-stranger` makes one
named like the template's, `test-project-rogue-<run>`, and the suite
checks that it grants nothing until the admin flips it. The issuer
learns a user's groups from how they logged in, so the suite logs each
user in: the admin mints them a password invite, they set a password of
the run's with it, and their login's cookie stands for them at the
issuer's verification and authorization pages, carrying the groups
they were in when they logged in (`Issuer.GroupSource: internal`), as a
browser's would. Each run also makes a collection, `gamma`, under
`/<namespace>/data/issuer/collections/<run>/`, which `test-stranger`
may read and `test-member` write, and which `test-stranger` shares
part of (`gamma/shared`) with `test-analyst` (not under
`-p origin-multiuser`, which refuses shares), and an object in each
prefix above, with `decoy/obj` in none. So, within the namespace:

| User            | May                                                                       |
| --------------- | ------------------------------------------------------------------------- |
| `test-analyst`  | read, create, and modify `home/test-analyst`; read the share              |
| `test-member`   | read, create, and modify `home/test-member` and `gamma`; read and create the projects; read `archive` |
| `test-stranger` | read `gamma`; and `archive`, once the rogue group may match               |

The admin creates the suite's clients through the issuer's admin API:
one for the device flow and one for the authorization code flow, each
allowed every scope a self-registered client gets and the refresh
grant; one allowed only `openid` and `storage.read:/` and no refresh
grant; and one for token exchange. The suite also uses the client every
issuer seeds for browsers (`Issuer.PublicClientID`), which has no
secret and proves itself with PKCE. The scenarios:

- `discovery`: each namespace issuer's discovery document names itself
  and endpoints under it, offers the four grants, and its JWKS holds
  the key that signed a token it minted; the local issuer's names the
  server's local issuer URL.
- `templates`: what each user is granted of everything
  (`storage.read:/`, `storage.modify:/`, `storage.create:/`, and
  `share.access:/`): exactly the table above, plus the share's
  `storage.read` and `share.access:/<share>` for `test-analyst`, with
  the issuer, subject, WLCG `any` audience, and `wlcg.groups` (the
  groups that earned something: both for `test-member`, none for the
  others) right; the rogue group grants `archive` once eligible, and
  not once ineligible again; and under `-p origin-export-templates`,
  `/protected-b`'s issuer grants `test-stranger` `export` alone and
  `test-member` nothing.
- `requests`: what the device flow grants of each request, for
  `test-member` unless said otherwise: an allowed scope, as asked; a
  narrower one (`archive/sub`), as asked; a broader one
  (`storage.read:/data/issuer`), as every allowed scope under it,
  collections included; an action, a path, a case, or a prefix that is
  not a path component (`archivex`), nothing; a scope that climbs with
  `..` into a path not allowed, nothing, and into one allowed, the
  scope as it resolves, never with `..`; `storage.delete`,
  `pelican.transfer` (granted only to the transfer API's users), and
  `collection.create` (a collection administrator's), nothing;
  `collection.read:/` and the standard scopes (`openid`, `wlcg`,
  `profile`, `email`), always; `test-stranger`'s `storage.read:/` as
  `gamma`'s read and `storage.modify:/` as nothing; `test-analyst`'s
  share by `share.access:/` or by its ID, and not by another's; and a
  refresh token only with `offline_access`.
- `code`: the same requests through the authorization code flow, with
  PKCE: the user is sent back (303) with a code and the state, which
  the client trades for tokens. A request for a scope the client may
  not carry (`storage.delete:/`, `pelican.transfer`) is sent back with
  `invalid_scope` instead of granted less; a wrong verifier and a
  replayed code are refused (`invalid_grant`); a redirect URI the
  client did not register gets no redirect (400); and the public
  client's flow works, with no refresh token even for
  `offline_access`.
- `clients`: the read-only client's token has only `openid` and the
  user's reads, and no refresh token; the code flow's client may not
  start a device flow (`unauthorized_client`); a wrong secret and an
  unknown client are refused (`invalid_client`), as a
  `client_credentials` ping tells them apart; and the public client's
  device flow gets no refresh token.
- `device`: the verification page shows the scopes asked and the
  client; polling before approval says `authorization_pending`, and
  polling again within five seconds `slow_down`; an approved code
  redeemed by another client, or redeemed twice, is `invalid_grant`,
  and approved twice, refused; a denied flow polls `access_denied`;
  nobody may see a flow's page; an unknown code is refused.
- `data`: with tokens of everything they may have, each user reads
  what the table allows straight at every origin, and is refused the
  rest (`decoy`, another's home, `archive` for the others); where the
  namespace takes writes, a PUT lands in a home and a project and is
  refused in `archive`, `gamma` for `test-stranger`, and the share.
  Through the federation, `pelican object get` fetches `archive/obj`
  as `test-member` and fails as `test-stranger`; each federated cache
  serves it to `test-member` and refuses `test-stranger`.
- `refresh`: with `offline_access`, the device flow issues a refresh
  token; a refresh gives a new access token, with the same scopes,
  subject, issuer, and groups and the configured `expires_in`, that
  reads `archive/obj` at the origin and introspects as active, and a
  new refresh token, which introspects as active too. The used refresh
  token still works within its grace period
  (`Issuer.RefreshTokenGracePeriod`), so a client can retry a lost
  response. Another client's refresh is `invalid_grant`, a client
  without the refresh grant's `unauthorized_client`, and a token that
  never was `invalid_grant`.
- `revoked`: once `test-curator` revokes `test-stranger`'s read on
  `gamma`, a refresh must not give `gamma`'s read back. Pelican's main
  re-grants a refresh token's scopes as they were, without asking the
  collections again, although `docs/collections-design.md` says a
  refresh should, so this row fails there. A refresh token revoked
  (RFC 7009) is `invalid_grant`, and the access token issued with it
  introspects as inactive and is refused by `userinfo`; the origin,
  which checks a token's signature and expiry alone, still honors it,
  by design. Under `-p origin-short-tokens`, a refresh token reused
  past its grace period is `invalid_grant`, and so is the newer one it
  had been traded for: the reuse ends the chain.
- `exchange` (RFC 8693): the exchange client, allowed `storage.read:/`
  and `offline_access`, trades `test-member`'s token of everything for
  one of `archive`'s read alone, which reads `archive/obj`; for a
  scope it may not carry (`storage.modify`), it gets a token with
  none; for no scope in particular, it gets the token's reads and
  `offline_access`, and a refresh token; and it is refused another
  audience (`invalid_target`), as the device client is refused the
  grant (`unauthorized_client`) and a subject token that never was
  (`invalid_grant`).
- `expiry`, under `-p origin-short-tokens` (access tokens last a
  minute, refresh tokens four, and a used refresh token twenty seconds
  more; it skips otherwise): a token's `expires_in` and `exp - iat`
  say the lifetime; once it has expired, it introspects as inactive,
  `userinfo` refuses it, and the origin refuses it within 90 seconds,
  which the row's note times. The origin is asked every 5 seconds from
  a second past the token's expiry. Pelican's native origin keeps what
  a token authorizes for 5 minutes from its last use
  (`origin_serve/authz.go`), and checks its expiry only when it first
  sees it, allowing 60 seconds of clock skew, so it takes the token at
  the first try, and each try keeps it; it never refuses it, and the
  row fails there, on main and v26.0.0-rc.3 alike. Its refresh token
  then renews it, and the new one reads; and tokens left since the
  suite began can no longer be refreshed once their refresh token has
  expired (`invalid_grant`).
- `client`: `pelican object get` of `archive/obj` as `test-member`,
  with no token, acquires one from the issuer through the device flow,
  which the suite approves once; a second get reuses it, approved by
  nobody; and under `-p origin-short-tokens`, a get after the token
  has expired succeeds, approved by nobody, since the client refreshed
  it. It skips for a standalone origin, which has no director to find
  the issuer through.

The run's collection, share, rogue group, clients, and objects are
removed after the suite; the users and their groups stay.


## What is not tested

Shapes that Pelican offers and this framework does not, and why:

- `globus` and `globusv2` origins, which need Globus credentials, and
  the `xroot` origin, which fronts another XRootD server.
- `Cache.PermittedNamespaces`, which only narrows what a cache
  advertises to the director; the V2 cache serves any namespace a
  client asks it for.
- `Director.CachesPullFromCaches`, which applies only to a request
  carrying `?prefercached`, which no Pelican cache or client sends.
- Lotman (`Cache.EnableLotman`), the XRootD cache's quotas; the
  `localcache` module, a client-side cache over a Unix socket; and
  `Client.FileRedirectRoots`, by which a cache sends a client that
  shares its filesystem to a file instead of bytes.
- `Origin.DisableCopies`, which turns third-party copies off.
- Of the embedded issuer: groups from a file or an identity provider
  (`Issuer.GroupSource`), which the suite's users get from the server's
  own database; `pelican.transfer` granted through
  `Transfer.EnabledGroups` or a scope grant; a client's own
  registration (RFC 7591) and configuration (RFC 7592), beyond what
  the Pelican client does for itself; and a device code's expiry, ten
  minutes after it is issued.
- `auth-oidc` and `with-grafana`, which need an identity provider and a
  data source the framework lacks
  ([Configuration](configuration.md#presets)).
