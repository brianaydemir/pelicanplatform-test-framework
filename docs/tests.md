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
| `federation` | that the federation serves at all, and every director names every federated cache and every origin, and for a client's direct read (`?directread`) only origins that take direct clients, and with no cache (`-p topo-no-cache`) an origin in a cache's place; what its redirects tell a client of a namespace, and where they lead a plain HTTP client, bare paths included ([redirects](#redirects)); a Pelican server's discovery document; under `-p origin-broker`, the connection broker; with several origins, that an object one of them alone holds is routed there, and comes back; under `-p registry-approval`, that nothing is routed to before its registration is approved; and that a server its admin declares down is routed to by no director until the downtime ends ([downtime](#downtime)) |
| `selftest`   | the tests' own helpers ([self-tests](#self-tests)): reading a V2 cache's `X-Transfer-Status` trailer, signing tokens of any claims and checking signatures, reading `/metrics`, and restarting a service mid-run |
| `commands`   | `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, and what `get` and `put` do with what already exists ([client commands](#client-commands)) |
| `client`     | the contracts that only show through the clients: stash_plugin's result ads, failing over between caches partway through a download, the ETags the servers agree on, which token the client presents, how `pelican token create` finds an issuer, share URLs, the director's recorded decision, direct reads with `?skipstat`, several sources at once, a preferred cache that is down, and transfers handed to the client agent (`--async`) ([client contracts](#client-contracts)) |
| `transfer-api` | the transfer API, origin-0's under `-p origin-transfer-api`, or a transfer server's own under `-p topo-transfer-server`: authentication, credentials, server-run copies, jobs, and confinement to the exports, which only origin-0's has; and its edges: subjects that are no usernames, a finished job cancelled, the event stream, sources on its own network, `pelican transfer credential`, `Transfer.EnabledGroups`, OAuth2 clients and a bootstrapped credential, and a restart with a job under way ([transfer API](#transfer-api)) |
| `listings`   | PROPFIND at each origin, director, and cache, of collections and of a plain object; a GET of a collection; and the links of an origin's HTML listing ([listings](#listings)) |
| `names`      | objects whose [names](#names) a URL or a listing must encode, beside decoys |
| `blocks`     | objects and ranges at pstore's and the caches' [block boundaries](#block-boundaries), through each server, each director's redirect, and both clients; an object past 4 GiB, whose length and ranges across 2 GiB and 4 GiB every server must get right; digests, asked for one at a time, by default, several at once, or by q-values, and an upload the client must verify by one; conditional reads and writes, at their edges, and the `ETag` that each method must report alike; objects overwritten, given up on, corrupted in a cache's store, or written through a cache; the `X-Transfer-Status` trailer, by which a server reports a failure after its headers; the origins' digests that a V2 cache relays, and an object that fails them; that a cache fetches an object read by many clients at once from the origins once; and, under `-p cache-small`, objects evicted from a cache |
| `freshness` | how long the caches keep their copies, by the `Cache-Control` the origins send, which it restarts them to vary: `only-if-cached`, 404s, what a V2 cache tells its clients, `no-store` and `private`, `max-age=0`, `no-cache`, `must-revalidate`, and `s-maxage`, and stale copies while the origins are down ([cache freshness](#cache-freshness)) |
| `cache-api` | a V2 cache's own APIs: prestaging and evicting objects, by whoever may read or modify them, and through the client; purges and introspection, which only its admin may use; that it serves no other federation; its usage accounting; and its consistency checker ([cache APIs](#cache-apis)) |
| `throttling` | how the servers and clients behave with the origins slowed, which it restarts them to be: a V2 cache's 429s when it sheds a burst, `X-Pelican-Timeout`, and streaming a cold object; the clients' errors for a shed attempt and for a transfer too slow ([throttling](#throttling)) |
| `exports`    | what a native origin does with each export's capabilities, which it restarts the origins to vary: `Listings`, `DirectReads`, and copies (`Origin.DisableCopies`); a third-party COPY's progress, its failures, and what a failed one leaves; a multipart PUT with metadata off; and, at an `https` or `httpsv2` origin, what it sends a backend that takes a token, which it restarts the origins to see: a static token on every request, or the client's own ([exports and copies](#exports-and-copies)) |
| `pstore`     | a pstore origin's own features, under `-p origin-pstore`: its storage API and `pelican-server origin introspect`, which only its admin may use; its scheduled index checks and data scans, and the reclamation of what is replaced; refusing writes once full, and backing up its catalog, which it restarts the first origin to check; and a backup restored into its store offline, with the origin stopped, which must give back every object it held, byte for byte ([pstore](#pstore)); and elsewhere, that a native origin has no storage API |
| `routing`    | how the directors choose where to send a client, and what they tell it: the order of the caches and of the origins by declared location, at random, by adaptive weights, and with a server filtered out; an object no origin holds; what `Location` and `Link` carry; tokens about to expire; their refusals and their status codes; and response hostnames; under `-p topo-multi-cache`, it restarts the directors to vary how they sort, and a cache restricted to a namespace (`Cache.PermittedNamespaces`) ([routing](#routing)) |
| `director-api` | what the directors publish of the federation, against the shape: their lists of servers and of namespaces, and the namespace a path falls in; their health tests of each origin and cache, as they and the servers report them, and the test file they serve the caches; which origins hold an object, for their admin alone; their metrics; their comparison of the federation's metadata; and under `-p topo-multi-director`, the ads one forwards to another ([director APIs](#director-apis)) |
| `registry`   | the registry, the federation's root of trust: registering and deleting prefixes, the prefixes it refuses, key chaining, a prefix with a LIKE wildcard, and `pelican namespace`; the keys it serves for each prefix, and no private key in any JWKS the federation serves; its web API for listing, approving, and denying registrations, and its servers; completing a server's registration with the server's key; who owns a registration: claiming one, an owner's edits of an approved one, a denied one resubmitted, and handing one on with an invite; each server's ID; and downtimes recorded at the registry, whose they are, and what the directors do with them ([registry](#registry)) |
| `tiering`    | V2 caches' tiering of large objects to object storage, under `-p cache-tiered`: what a plain HTTP client gets from the cache, and through a director, and what both clients get, from the cache named and through the director; including a copy replaced in the bucket |
| `posc`       | interrupted and racing uploads, under `-p origin-posc`, `-p origin-atomic-uploads`, or `-p origin-pstore`; and the native POSC's reaping of abandoned staging files, which it restarts the first origin to hasten ([POSC and metadata](#posc-and-metadata)) |
| `metadata`   | metadata events, under `-p origin-metadata`: the URLs by which an uploader follows or cancels its event; the events that a MOVE, a COPY, and a collection's DELETE send, and those the reconcile sweep must not; the first origin's admin APIs over its record of objects and its publish queue, and the CLI over the record; and refusing uploads while a namespace's queue is full; which it restarts the first origin to check; with publishing off, that the queue's admin API says so ([POSC and metadata](#posc-and-metadata)) |
| `users`      | which Unix user the origins act as, under `-p origin-multiuser` or `-p server-unprivileged`: who owns what they write, and in which group; under multiuser, reads with no token, what a user learns of another's files, POSC's staging, users below `Origin.MultiuserMinID`, and the mapfile's rules, a username claim, and a mapfile edited while the origins run ([Unix users](#unix-users)) |
| `owners`     | origin-2, another owner, under `-p topo-multi-owner` ([another owner](#another-owner)) |
| `sitelocal`  | cache-2, a site-local cache, under `-p topo-site-local-cache` |
| `collections` | collections: prefixes with access control of their own, granted through the origins' embedded issuer ([collections](#collections)) |
| `issuer`     | the origins' embedded issuer: what it grants of what a client asks for, through each flow, and its refresh tokens; and its boundaries: each namespace's issuer apart from another's, clients that register themselves, its verification page's CSRF tokens, its admin API for clients, cross-origin requests, PKCE, and codes' expiry ([issuer](#issuer)) |
| `accounts`   | the life cycle of a server's users and groups, and who may act as whom because of it: deactivated and deleted users, at the web API and the embedded issuer; scopes granted to a user, and authority from configured groups; grants that go with a deleted user or group, and stay with a renamed one; group invites; and a new acceptable use policy ([accounts](#accounts)) |
| `oidc`       | logging in through an identity provider, under `-p auth-oidc`: what each server does with a user the provider vouches for, by the code flow a browser follows; the username and groups it takes from the provider's claims, and the admin and the issuer's grants they earn; the registry's proof of a registrant by the provider's device flow; and the Pelican client acquiring a token approved by such a user ([OIDC login](#oidc-login)) |
| `advertise`  | what the directors take from servers, and from anyone posing as one: forged and spoofed advertisements, federation tokens, service discovery, and the connection broker ([advertisements](#advertisements)) |
| `webapi`     | who may use each server's web API: its admin routes, with no credentials, as an ordinary user, as the admin, and with tokens it must take for no one; a director's API tokens; logging in and out, and the login cookie; CSRF tokens and cross-origin requests; malformed paths; and the version each server reports ([web API](#web-api)) |
| `observability` | what each server reports about itself, and who may see it: its health report, and the gauges that must agree with it; who may read its metrics and query its Prometheus, and its scrapes and the directors'; a native origin's request, byte, and storage counters; its log level, raised for a while through its admin API; tokens kept out of its log; and under XRootD, an XRootD log level, which restarts XRootD ([observability](#observability)) |
| `webdav`     | the origins' WebDAV methods beyond GET, HEAD, PUT, DELETE, and PROPFIND, and how each is authorized: POST, COPY, MOVE, MKCOL, LOCK, PROPPATCH, and OPTIONS; writes through symbolic links that leave a store; how an origin serves stored HTML; and that a cache serves those methods for no one, and changes no origin ([WebDAV methods](#webdav-methods)) |
| `auth`       | each [credential](#authorization) at each server, in each namespace; each issuer's keys; tokens of one storage scope, for another audience or one origin's, in the query string, and several at once; tokens whose times, claims, or signatures are wrong, one that expires while in use, SciTokens scopes, and a server's own web tokens ([token validation](#token-validation)); paths and symbolic links that climb out of a namespace or a token's scope; a protected prefix inside a public one; and what a refusal tells a client |
| `transfers`  | gets and puts through `pelican object` and `stash_plugin`: gets through the cache the director picks, direct from an origin, and from a cache the client names, byte-checked ([test data](#test-data)) |
| `restart`    | what survives a server's restart, and what a server does while another is gone: a cache's store, every server's identity and database, the directors' routing once a server is killed and once it is back, an origin whose storage service is stopped, the clients' failover from a cache that is paused or killed, and a director's view of its peer ([restarts](#restarts)) |
| `keys`       | keys added and rotated while the servers run: the registry's and the origins' JWKS, the directors' routing, tokens of either key at every server, the issuer's kid, a V2 cache's master key, and every server's database backups ([keys](#keys)) |

A suite that doesn't apply to the shape is skipped, and says why.
`federation`'s `ready` and `caches` checks run first, whatever else is
asked for; `caches` tries each cache 20 times, 15 seconds apart, while
it fails or doesn't answer. If the federation never serves, nothing
else runs. The
`expired` credential must be 70 seconds past its expiry before it is
presented, so `auth` and `transfers` run after every suite but two, by
which time it usually is. `restart` and `keys` come last of all, since
each takes servers, or their keys, away for a while, and puts them
back.

Under `-p registry-approval`, nothing serves until an administrator
approves its registration, so before each of its tries `ready` approves
every pending registration through the registry's admin API: each
Incomplete one, as a server's own registration starts (since
v26.0.0-rc.1), and each Pending one, whose form someone has filled in.
It approves them as the registry's built-in `admin`, with a token like the one `pelican registry` mints
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
redirects (and, under `-p director-ha`, the load balancer's, which the
federation names as its director, and so which curl is given) for each
exported namespace, with the namespace's `server` token (none for a
read of `/public`):

- A GET and a HEAD of the object's bare path must land at a cache, or
  under `-p director-default-origin` at an origin; one with
  `?directread` at an origin, where the namespace takes direct clients,
  and otherwise find none (405), as the Pelican client's `--direct`
  asks; and the object and origin routes at a cache and an origin.
  Where the federation has no cache (`-p topo-no-cache`), whatever is
  meant for a cache must land at an origin instead, where the namespace
  takes direct clients (the director falls back to an origin with
  `DirectReads`; `redirectToCache` in `director/director.go`), and
  otherwise find none (404); `directors` checks the same of what the
  object route names.
- What lands at a cache must be served: the object's bytes, or for a
  HEAD its `Content-Length`. What lands at an origin must be served
  only where the namespace takes direct clients, and otherwise refused
  (401 or 403), as the origin refuses every direct client there.
- A bare-path PUT of a new object, with the namespace's token, must
  land at an origin, where the namespace takes writes, which must then
  store it; where it takes none, the director must find no origin, or
  the one it names refuse, and no origin may hold the object.

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
the `tiny` topology, whose servers and director are one process, and
with no cache and one origin (`-p topo-no-cache`), when nothing could
be named instead.

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
  minted. `jwt.verify()`, which checks an ES256 signature against a
  JWKS, must then find the token's key, by its `kid`, in the first
  origin's published JWKS, and none in a JWKS of the `unknown` test key
  alone. It skips where the origins take no direct clients.
- `metrics`: `framework/testlib/metrics.py` reads a server's
  `/metrics` with a token of the server's own making, with scope
  `monitoring.scrape`, since Pelican guards it
  ([observability](#observability)). A GET of the object straight to
  each native origin must raise its `pelican_http_requests_total` with
  `server_type="origin"`, `method="GET"`, and `code="200"` by at least
  one (`metrics/http_metrics.go`). Other requests may raise it too, so no
  check of a counter here is exact. It skips for an XRootD origin, which
  keeps no such counter, and where the origins take no direct clients.
- `restart-cache`: `framework/testlib/control.py` restarts a service in
  the middle of a run ([service control](../README.md#service-control)).
  It reads the object through cache-0 until a V2 cache says, with `Age`,
  that it holds it whole (`local_cache/persistent_cache_api.go`), and
  every director names cache-0 for it; then restarts cache-0. A cache
  that stops tells the directors it is shutting down, and they send no
  client to it until it advertises again, which it does as it starts and
  then every minute (`launchers/launcher.go`, `director/director.go`).
  So within 150 seconds every director must name cache-0 again, and
  within 300 it must serve the object, the same bytes, since a cache
  just started refuses a namespace's tokens until it has learned its
  issuers. A V2 cache keeps its store across a restart, and must serve
  the object from it, with an `Age` of at least the seconds since it
  first said it held it all, which only the record it kept can give
  (`Age` counts from when the object was completed, and a cache that
  fetched it again would send none). An XRootD cache need only serve
  the bytes. It skips where no helper serves the run, in the tiny
  topology, whose cache is the whole federation's process, and where
  there is no cache-0.


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
instead of a cache (`pelican object get --direct`), and a named twin,
`cache-<scenario>`, which names the first federated cache (`pelican
object get --cache`) and takes the objects from it, asking the director
only what the namespace wants: the three ways a client reaches an
object. Every scenario also has a plugin twin, `plugin-<scenario>`
(e.g. `plugin-direct-exist`), which runs the same batches through
`stash_plugin`. The plugin has no flag for a direct read, so its direct
batches add `?directread` to each URL, and takes the named cache from
`Client.PreferredCaches`. Only the plugin's uploads go elsewhere, to
`put/plugin-put-<ns>-<cred>/`. A shape without a federated cache
(`topo-standalone`, `topo-no-cache`) has no named scenarios, and its
other gets, asking for a cache, must be sent to an origin.

What each scenario should do follows the
[authorization](#authorization) rules: every get of `/public` should
pass, and every put to it should be refused, unless
`-p origin-public-writes` gives it writes, when the puts with the
`server` and `jwks` tokens should pass, as in the protected namespaces,
and the rest be refused. A shape that exports one
namespace (`origin-httpsv2`, `origin-no-direct`, `origin-public`) has
only that namespace's scenarios, and `dne`, which gets from that
namespace instead. Without `DirectReads` (`origin-no-direct`),
`direct-dne` must be refused. `./fed.sh test -l transfers` lists the
scenarios, and the summary after a run shows what each was expected to
do.

`exist`, `dne`, and each scenario that a token should let through run
two batches, of one object and then two (a put of two goes to a
collection rather than to an object's URL); the rest run one, of one
object. In the default shape that is 238 scenarios and 298 client
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
pass every scenario; only an origin may serve a direct one; and only the
named cache a named one, since a client that fell back to the director's
caches would otherwise pass. Under `topo-tiny`, whose cache and origin are
one host, these checks are skipped. Where a namespace takes no direct
clients (`origin-no-direct`), every direct read must be refused.

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
one namespace. `/public` has no key of its own, and no issuer unless it
takes writes (`-p origin-public-writes`), so it gets only the
credentials that need neither its own key or issuer nor another
namespace's. The "other namespace" of `/protected-a` is `/protected-b`,
and the reverse:

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
With the origins' issuer off (`-p auth-no-issuer`), every namespace has
one issuer, the origin itself, and no keys of its own, so the tests skip
`wrong-iss`, `ns-jwks`, `ns-other`, and `ns-cross`, as they do with
`auth-external-issuer`, whose key then signs `server`, `wrong-op`,
`wrong-path`, and `expired`. Its JWKS holds only its own keys, so they
skip `jwks` there too: a token that names it, signed with a key listed
only in `Server.IssuerJwks`, would only repeat `unknown`. A
shape that exports only `/protected-a` (`origin-httpsv2`,
`origin-no-direct`) lists `/protected-b`'s key nowhere, so the tests skip
`ns-other` and `ns-cross`, which would only repeat `unknown`. A shape
that exports only `/public` (`origin-public`) presents each credential
there alone, where no token decides, and skips the checks that write
beside `/protected-a`'s test object. Where a token decides, only
`server`, `jwks`, and `ns-jwks` should be allowed. A token doesn't always
decide:

- **Reads of `/public`** should be allowed whatever the token. The
  servers ignore it, and the clients don't even send one.
- **Writes to a namespace without `Writes`** should be refused whatever
  the token. That is `/public`, and every namespace under
  `origin-no-direct`. `/public` has no issuer at all, so a token claiming
  its issuer URL is trusted by nothing. Under `-p origin-public-writes`,
  `/public` takes writes, and so has an issuer, which a write there must
  name, as in the protected namespaces, while its reads still take no
  token: the write path of an export that anyone reads, which Pelican's
  own documentation of exports gives as its example.
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
  must hold `jwks.pem`'s key, and neither namespace's. Where `/public`
  has an issuer (`-p origin-public-writes`), its JWKS must hold the
  server-wide keys, and neither protected namespace's own, with which a
  token for that namespace alone could otherwise write `/public`.
  `./fed.sh test auth/keys` runs only this check.
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
  WLCG token profile says. Every credential names the profile's "any"
  audience; this token, like `server`'s but for
  `https://elsewhere.invalid`, must be refused by every origin, for a
  GET of the test object and a PUT of a new one, which must store
  nothing. Only the origins are asked (Pelican's `origin_serve/authz.go`,
  and the audience in an XRootD origin's scitokens configuration): a
  cache serves tokens minted for any origin, so what it should make of
  an audience is not settled. `audience-specific` presents a token for
  the first origin's own audience alone (`Origin.TokenAudience`, which
  is `Origin.Url` by default: the scheme, host, and port of its data
  URL, XRootD's for an XRootD origin), which only that origin may take,
  and one whose `aud` lists the "any" audience and another, which every
  origin must take, since a token is for each audience it lists. Under
  `-p topo-multi-origin`, the second origin must refuse the first. Both
  checks skip where the origins take no direct clients.
- **Out of a token's scope**, the `auth` suite's `narrow-climb` check
  presents a token for one collection in `/protected-a`,
  `data/auth/<run>-narrow`, and climbs out of it to a sibling with `..`,
  in each spelling of `traversal` but `%252e%252e` (which a server
  decodes once to a name inside the collection): a GET of the test
  object at every server, and at every origin that takes writes, a PUT
  of a new object and a DELETE of one put there first. Each must be
  refused (4xx), without the object, and change nothing. Each cache must
  first serve the object with the `server` token, so that it refuses an
  object it holds. `traversal` climbs only with tokens for a whole
  namespace, and `narrow` asks only for a sibling by name.
- **Inside a public namespace**, the `auth` suite's `nested-protected`
  check, under `-p topo-multi-owner`: origin-2 exports `/public/secret`,
  which takes a token, inside the origins' `/public`, which anyone may
  read ([another owner](#another-owner)). It puts an object there, which
  the owner then reads through each cache, so that each holds it. A GET
  of it with no token, and with one naming the prefix's issuer but
  signed by `unknown.pem`, must then be refused (401 or 403) at each
  cache, at origin-2, and at each director's bare path, followed to
  wherever it leads. A V2 cache grants the read of every public
  namespace on its whole path, and authorizes a path by any grant that
  encloses it (`local_cache/cache_authz.go`), so it serves the object to
  anyone, and the rows fail there, on main and v26.0.0-rc.3 alike. An
  XRootD cache's authfile denies the inner prefix outright
  (`xrootd/authorization.go`), and is the control. The prefix is not
  inside `/public/other`, origin-2's own public export, since one native
  origin cannot serve one export inside another: their routes conflict,
  and its router (gin) panics as it registers them.
- **Several tokens at once**, the `auth` suite's `token-lists` check, at
  each native origin: it takes tokens from its `Authorization` header,
  several separated by commas, after a bearer scheme in any case (RFC
  9110, section 11.1), and from the `access_token` (RFC 6750) and `authz`
  query parameters, each of which may repeat, and tries each in turn
  (`extractTokens` in `origin_serve/handlers.go`). A GET of the test
  object in `/protected-a` must pass with the `server` token in
  `access_token`; after `unknown`'s in one header, separated by a
  comma; after `unknown`'s in a second `authz`; and after `bearer` in
  lower case. With the token under `Basic`, it must be refused. With
  eleven tokens, the last the `server` token, it must get 401, since an
  origin treats more than ten as none; Pelican's main does, but
  v26.0.0-rc.3 tries them all, so that row fails there. A V2 cache and
  XRootD take one token, from the header or `authz`, and are not asked.
- **How a native origin refuses**, the `auth` suite's
  `standalone-refusals` check: a GET of the test object in
  `/protected-a` at each native origin with no token must get 401; with
  `unknown`'s, 403 from a standalone origin (`-p topo-standalone`) and
  401 from a federated one; with `server`'s, the object; and of a
  missing object, 404. A standalone origin has no director to tell a
  client what a namespace wants, so every answer of its must carry the
  headers a director's redirect does (`X-Pelican-Namespace`,
  `X-Pelican-Authorization`, and `X-Pelican-Token-Generation`), naming
  `/protected-a`, that it takes a token, and its issuer; its 403 sends
  the Pelican client to get the token they name. A federated origin
  sends none of them, and answers 401 either way, since a 401 tells
  rclone to run its token command again (`authMiddleware` in
  `origin_serve/handlers.go`). It skips where the origins take no direct
  clients.

Both suites wait until the `expired` token is 70 seconds past its expiry
before presenting it, since native servers allow 60 seconds of clock
skew. `test.py` mints it as it starts, and runs these suites after
every other but `restart` and `keys`, so the wait has usually passed by
then.

`init` creates the four test keys in `framework/var/test-keys/`, and
the public keys of `jwks.pem`, `ns-protected-a.pem`, and
`ns-protected-b.pem` in `framework/var/issuer-jwks/`, which the origins
mount. They name `jwks.pem`'s in `Server.IssuerJwks`
(`framework/config.d/instance/origin-*/`), and each `ns-<ns>.pem`'s in
the `IssuerJwks` of the `/<ns>` export, which `fed.sh` generates. No
server sees any of the private keys. Under `-p topo-multi-origin`,
origin-1 warns that its keys authorize nothing, because both origins
advertise origin-0's issuer.


## Token validation

The credentials are each wrong in one way that `pelican token create`
can make. The `auth` suite also presents tokens that it cannot,
which `framework/testlib/jwt.py` signs: each like `server`'s, with the
same header and the origins' key, but for one thing. Each check asks
for the test object in `/protected-a` at every origin and cache, where
the origins' XRootD servers (`-p origin-xrootd`) and XRootD caches
(`-p cache-xrootd`) are controls; where the origins take no direct
clients, every request straight to one must be refused anyway.

- `time-claims`: native servers allow 60 seconds of clock skew on each
  of a token's times, as the WLCG token profile recommends, and no more
  (`ClockSkewLeeway` in `token/token_verify.go`). A token whose `nbf` or
  `iat` is 300 seconds ahead must be refused; one whose `nbf` is 20
  seconds ahead, taken; and one that expired 120 seconds ago, refused.
  Each request has a token of its own, since a V2 cache remembers a
  token it refused for 30 seconds.
- `expired-in-use`: a token that lasts 30 seconds, presented at once and
  then every 30 seconds for three minutes, must be taken before it
  expires, and refused from 70 seconds past its expiry on; in between,
  either. The row's note lists what each server answered, and when.
  Pelican's native origin and V2 cache keep what a token authorizes for
  5 minutes from its last use, and check its expiry only when they first
  see it (`origin_serve/authz.go` and `local_cache/cache_authz.go`), so a
  token in use never expires there, and the rows fail, on main and
  v26.0.0-rc.3 alike. The check runs beside the suite's others, in a
  thread of its own, and its rows come last. Unlike the `expired`
  credential, whose wait sidesteps that memory, it tests it.
- `malformed-claims`: a token without `exp`; without `kid`; with a `kid`
  that no key has; with `alg` `none` and no signature; signed by HS256,
  keyed with the origins' public key as a JWK, as in the classic
  key-confusion attack; or without `aud`, must be refused. The WLCG
  token profile requires `exp` and `aud`. Pelican's native servers check
  a token with jwx, which checks `exp` only if a token has one; a native
  origin checks `aud` only if a token has one (`origin_serve/authz.go`),
  and a V2 cache never. So the `no-exp` rows fail at native origins and
  V2 caches, and the `no-aud` rows too, on main and v26.0.0-rc.3 alike.
  A token whose `wlcg.ver` is `2.0`, which the profile does not define,
  may be taken or refused, and the row's note says which.
- `scitokens-profile`: a token of the SciTokens 2.0 profile (`pelican
  token create --profile scitokens2`), whose scopes are `read:/` and
  `write:/` and audience `ANY`, for a GET at every server and a PUT of a
  new object at every origin. XRootD takes such scopes; Pelican's native
  servers compare a token's scopes with the WLCG profile's `storage.*`
  ones (`token_scopes/token_scope_utils.go`), and so grant it nothing.
  Whether they should is not settled, so a server may take the token or
  refuse it, and the row's note says which; but wholly, serving the
  object or none of it, and storing the PUT's bytes or nothing.
- `web-token-strict`: a server checks the tokens of its own web API with
  no clock skew, since it alone mints and checks them
  (`VerifyWithKeysetStrict`, from `extractUserFromBearerToken` in
  `web_ui/authentication.go`). At each origin's and cache's web API,
  `/api/v1.0/auth/whoami` with a token that it takes as its admin's
  (`credentials.mint_admin()`) must say who that is; with the same token
  not valid for another 20 seconds, or expired 5 seconds ago, it must
  refuse it (401) or say that no one is logged in.


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


## Transfer API

Under `-p origin-transfer-api`, origin-0 runs the transfer API
(`Origin.EnableTransferAPI`; `transfer/` in Pelican), a service that
runs transfer jobs on the server, such as third-party copies, with
storage credentials it keeps for each user, and only where a job's
source or destination is one of the origin's exports. Under
`-p topo-transfer-server`, a transfer server of its own, `transfer`,
runs it instead (`pelican-server serve --module transfer`), which
confines a job to no export, and authenticates its users through a
local issuer of its own (`RegisterTransferAPI` and `RegisterLocalIssuer`
in `transfer/transfer.go`). The API takes tokens with the
`pelican.transfer` scope from its host's issuer, which the
`transfer-api` suite mints with that host's key, origin-0's or the
transfer server's, for a subject of the tests' own; its storage
credentials are the `server` tokens for `/protected-a`. The suite's
objects go under `/protected-a/data/xfer/<run>/`, which it removes
afterward, with the credentials it added. A scenario with several
cases records a row per case, named `<scenario> <case>`.

- `ping`, `auth-none`, `auth-storage-token`, and `auth-unknown-key`:
  the public ping says that the service is the transfer service, and a
  request with no token, with a storage token (which lacks
  `pelican.transfer`), or with a `pelican.transfer` token signed by a
  key no server knows, is refused (401 or 403, `UNAUTHORIZED`).
- `credentials`: a credential is listed by its ID, no answer shows its
  token, and a second one of the same name is a conflict (409).
- `copy`, `job-status`, `bad-dest-credential`, `unknown-credential`,
  `other-user`, `path-not-allowed`, `cancel`, `local-get`, and
  `local-put`: `pelican object copy --transfer-server ... --wait` copies
  an object byte for byte (`copy server`), and with `--dest-origin`
  naming origin-0 instead, the client asks the origin whether it runs
  the transfer API (`pingTransferService` in `cmd/object_copy.go`), and
  copies through it where it does, or must fail, saying that the
  service is not available there, and land nothing, where a transfer
  server's is the API (`copy dest-origin`); a finished job reports `completed`, and is
  listed; a copy whose destination credential may only read fails, and
  writes nothing; a job naming a credential that does not exist is
  refused (400); another user can neither see a job nor use a
  credential; a job outside the exports is refused (403,
  `PATH_NOT_ALLOWED`); cancelling an unknown job is not found, and a
  finished one must stay as it finished; and a job may neither download
  to the origin's own filesystem outside an export nor upload from it,
  which Pelican's main does not confine, so those two fail there. A
  transfer server confines a job to nothing, by design: there, a job
  outside the exports must be taken (201), and then fail, since nothing
  serves what it names (`path-not-allowed`); and a job must download to
  the server's own filesystem (`/var/lib/pelican`, its state, where the
  suite then finds the source object, byte for byte; `local-get`) and
  upload from it (`/etc/hostname`, which must land as the file,
  `transfer` and a newline; `local-put`), so that what a user of such a
  server may reach is on record. For a job that finished and is still
  in the manager's memory, Pelican's `CancelJob` answers "job already
  completed", which the handler turns into 500
  (`client_agent/transfer_manager.go`, `transfer/handlers.go`), so
  `cancel` fails, on v26.0.0-rc.3 and main alike.
- `subjects`: `pelican.transfer` tokens whose subjects are a CILogon
  URL, one character, 65 characters, or `admin`, the built-in user's
  name: each must list its jobs (200), run a copy of its own, and see
  no other subject's job. The API resolves each token's issuer and
  subject to a user record of its own, creating one with the subject as
  its username (`GetOrCreateUser`, from `TransferAuthMiddleware` in
  `transfer/auth.go`), and a username must be 2 to 64 characters of a
  narrow set (`ValidateIdentifier` in `database/identifiers.go`), so
  the first three fail with 500, "Failed to resolve user identity"
  (I4). The `admin` case shows whether a token for an existing
  username, from the local issuer, is the existing user's or a
  collision.
- `cancel-finished`: a copy whose destination credential may only
  read, which fails, cancelled: the cancel is taken (2xx) or refused as
  too late (409), and the job must stay `failed`. Pelican counts every
  transfer of a failed job as cancelled, and records the job cancelled
  (I5), so the row fails, on v26.0.0-rc.3 and main alike.
- `events`: a job's event stream (`GET /jobs/<id>/events` with `Accept:
  text/event-stream`) must carry `status` events whose data names the
  job, the last of them its terminal status, `completed`, after which
  the stream ends (`StreamJobEvents` in `client_agent/job_events.go`),
  and the job's status must agree; another user's stream of the job is
  not found (404). `pelican object copy --wait` falls back to polling
  when a stream fails, so `copy` would not notice a broken one.
- `ssrf-and-prefixes`: a job whose source is on link-local
  (`169.254.169.254`) or loopback, which the allowed ranges of
  `Server.SSRFProtection.AllowedCIDRs` do not cover, must be refused
  (403, `ENDPOINT_NOT_ALLOWED`); one whose source and destination are
  under `/protected-ab`, which merely begins like an export, 403
  `PATH_NOT_ALLOWED`, since a prefix matches whole path segments (at a
  transfer server, taken and then failed, as `path-not-allowed`); and
  no credential's token, nor the client secret that `oauth-clients`
  registers, may lie in the API's host's database
  (`framework/var/state/`) in plain, which keeps each encrypted
  (`transfer/encryption.go`).
- `credential-cli`: `pelican transfer credential add`, `list`, and
  `delete`, with the user's token in a file, against the API: each
  must agree with it, and neither `add` nor `list` may print the
  token.
- `enabled-groups` and `oauth-clients` need configuration that no shape
  has: `Transfer.EnabledGroups`, naming a group of the run's own
  (`xfer-<run>`), and `Transfer.EnableOAuth2Clients`. The suite restarts
  the API's host once with both, as the last of its configuration
  layers ([service control](../README.md#service-control)), runs them,
  and restarts it without, waiting each time until the API answers and
  every director names origin-0 again. The restart needs a control
  helper and a topology other than `tiny`, whose origin is the whole
  federation's process; elsewhere both skip, and say why.
  - `enabled-groups`: with the group named, the host's local issuer
    grants `pelican.transfer` to a member who asks for it through the
    device flow, approved with their login cookie, which carries their
    groups, and not to another user (`transferAccessAllowed` in
    `oauth2/issuer/handlers.go`; `issuer`). The member's token is taken
    at the API, and the other's refused (`token`). The member's login
    cookie must be taken too, since the API takes a cookie whose groups
    match (`TransferAuthMiddleware`), and the other's refused (403;
    `cookie`): the cookie carries no `pelican.transfer`, which the
    middleware asks of every token, so the member's is refused too
    (I6), and the row fails, on v26.0.0-rc.3 and main alike.
  - `oauth-clients`: with the clients off, as by default, the routes
    refuse everyone (403, `DISABLED`; `disabled`). With them on, a
    client of the namespace issuer's, which origin-0's admin creates
    for the token exchange and the refresh grant (made-up credentials,
    with the issuer off or external, since the origin can make no
    client of it), is
    registered with the API (`POST /oauth-clients`, 201), listed
    without its secret, refused a second time (409), and deleted
    afterward (`crud`); a token the
    admin got from the issuer is exchanged, through it, for a
    credential (`POST /credentials/bootstrap/token-exchange`, 201;
    `handleTokenExchangeBootstrap` in `transfer/bootstrap.go`), which a
    copy then uses (`bootstrap`); and under `-p origin-short-tokens`,
    once the credential's access token has expired, a copy must still
    complete, since the server refreshes the token first
    (`getDecryptedAccessToken` in `transfer/credentials.go`), and the
    credential's `token_expiry` must have moved on (`refresh`;
    elsewhere it skips, since the tokens last an hour).
- `restart`: the API's host restarted while a copy runs: the job must
  then be `failed`, saying it was interrupted by a restart
  (`reconcileInterruptedJobs` in `transfer/recovery.go`), or, if the
  copy finished first, the row is inconclusive (`interrupted`). A
  `posixv2` origin-0 is first restarted slowed
  (`Origin.TransferRateLimit` 256 KiB a second, which it alone honors),
  so that the copy of the 8 MiB source takes about a minute; afterward
  it is restored. A
  credential stored before the restart must still work (`credential`).
  And a job that finished before the restart, which the manager no
  longer holds, cancelled, must keep its status (`forgotten`): Pelican
  records it cancelled instead (I5), so the row fails, on v26.0.0-rc.3
  and main alike. It needs a control helper and a topology other than
  `tiny`, and skips otherwise.


## Client contracts

Some of the servers' contracts show only through the clients: what
stash_plugin reports to HTCondor, which decides whether a job retries;
failing over from one cache to another partway through a download,
which needs the servers' ETags to agree; which token the client
presents, of those its environment offers; how `pelican token create`
finds an issuer; and the URL that `pelican object share` hands out. The
`transfers` suite judges each batch by what moved; the `client` suite
reads what the clients report. Its objects go under
`/<namespace>/data/client/<run>/`, put at every origin, and are removed
afterward. It needs `/protected-a`, as `commands` does. A scenario with
several cases records a row per case, named `<scenario> <case>`.

- `plugin-ads`: stash_plugin's result ads (`cmd/plugin.go`), one per
  line of its outfile, of a batch of two objects through a cache, cold
  and then warm (`cold`, `warm`): each must say `TransferSuccess`,
  `TransferFileBytes` and `TransferTotalBytes` of the object's size,
  `TransferUrl` as given, `TransferType` `download`, `TransferFileName`
  the object's name, and a `TransferStartTime` no later than its
  `TransferEndTime`; its `DeveloperData` must count the attempts and
  name `Endpoint0`; on a warm read through a V2 cache, which sends
  `Age`, the last attempt's `CacheHit` must be true, with a `DataAge`
  above 0, and on a cold one not; and `ServerChecksums` must hold the
  object's `crc32c` after a whole read through a V2 cache, which relays
  the origin's digests or computes its own, and whatever digests any
  ad reports must be the object's. Of an object that does not exist
  (`dne`): `TransferSuccess` false, `TransferRetryable` false, an error
  of type `Specification` with `PelicanErrorCode` 5011, and nothing
  left. Of a download refused, with a token signed by a key no server
  knows (`refused`): an error of type `Authorization`. Of a batch of
  one that exists and one that does not (`mixed`): two ads. The exit
  status must be 0 if every transfer succeeded, else 11 if any failure
  may be retried, else 1 (`writeOutfile`). It skips where there is no
  cache.
- `failover`, with a federated V2 cache beside another cache, of
  either kind (`-p cache-mixed` makes cache-1 an XRootD cache, so that
  the failover crosses kinds): both hold a 1 MiB object whole, and the
  first's, a V2 cache's, chaos API corrupts a block in the
  middle of its copy, which it cannot repair once the object is removed
  from every origin's store (as `blocks`'s `transfer-status-failure`
  does). `pelican object get --cache <first>,<second>` must then
  succeed, with the object's bytes, its stats showing a failed attempt
  at the first cache that brought some bytes, then a success at the
  second, the attempts' bytes adding up to the object: the client
  resumes with a `Range` from where it stopped, and refuses to if the
  `ETag` changed between attempts (`downloadObject` in
  `client/handle_http.go`); so the two caches' ETags must agree
  (`get`). stash_plugin, with the same preferred caches
  (`Client.PreferredCaches`), must record a failed `Endpoint0` at the
  first cache, with `TransferError0`, and succeed at the second
  (`plugin`). It skips where the origins send a `Cache-Control`
  (`-p origin-max-age`), under which the second cache would revalidate
  its copy against origins that no longer hold the object.
- `etag-agreement`: an object put at every origin: each origin that
  takes direct clients, and each federated cache once it holds the
  object, must report one `ETag` by HEAD and by GET, and a cache's must
  be one of the origins', since a V2 cache relays the origin's
  (`local_cache/persistent_cache_api.go`); origins that share a store
  must agree. Origins with stores of their own hold copies of their
  own, whose ETags may differ, and the note says so: a client that
  resumes at a cache filled from another origin then aborts. A
  `posixv2` origin's GET reports an ETag of its own making, and its
  HEAD the backend's (O2; `blocks`'s `etag-agreement`), so the row
  fails there, on v26.0.0-rc.3 and main alike.
- `token-discovery`: which token `pelican object get` presents, of
  those its environment offers (`tokenContentIterator` in
  `client/acquire_token.go`), for an object in `/protected-a`, each row
  in an environment of its own: `BEARER_TOKEN`, `BEARER_TOKEN_FILE`,
  and `_CONDOR_CREDS/scitokens.use`, each alone with the `server`
  token, must each succeed (`bearer-token`, `bearer-token-file`,
  `condor-creds`); the `wrong-path` token in `BEARER_TOKEN` beside the
  `server` token in `_CONDOR_CREDS` must succeed, since the client
  passes over a token that the director's hints say would not do
  (`skips-unacceptable`); the `unknown` token in `BEARER_TOKEN` beside
  the `server` token must be refused, since it looks acceptable and so
  is presented (`acceptable-wins`); and a URL whose scheme names a
  token (`protected-a+pelican://...`) must select
  `_CONDOR_CREDS/protected-a.use`, the `server` token, before
  `scitokens.use`, the `unknown` token (`prefix`), as must
  `protected_a+pelican://...`, whose `_` the client reads as `.`
  (`protected.a.use`; `GetTokenName` in `pelican_url/parse.go`;
  `prefix-underscore`). Pelican's main has a V2 cache hint at the
  namespace's issuer when it refuses a token, which the client may act
  on by retrying with another token it holds; whether that lets
  `acceptable-wins` through is for a run on main to show.
- `token-create-discovery`: `pelican token create` without `--issuer`
  or `--scope-path` (`cmd/token.go`), for a collection in
  `/protected-a`: with the origins' key, the token's issuer must be the
  namespace's, the first of those the director names whose JWKS holds
  the signing key's public key, and its scope the path within the
  namespace (`origin-key`), and the token must read an object there at
  every origin that takes direct clients and every federated cache
  (`reads`); with `ns-protected-a.pem`, listed only in the export's own
  `IssuerJwks`, the issuer must be found through that JWKS
  (`namespace-key`); with `unknown.pem`, it must fail, saying that no
  issuer matches the key (`unknown-key`); for `/public`, which has no
  issuer, it must fail, asking whether a token is needed at all, but
  under `-p origin-public-writes`, where it has one, the token's issuer
  must be `/public`'s (`public`); and under `-p topo-multi-owner`, with
  origin-2's key for `/other`, the issuer must be origin-2's
  (`other-owner`).
- `share-url`: `pelican object share` of a collection in
  `/protected-a`, its device flow approved as the admin, whom the issuer
  grants the namespace (`client/sharing_url.go`, `cmd/object_share.go`):
  the URL printed must be the collection's with `?authz=<token>`, whose
  scope is the collection's `storage.read` alone (`url`), and with
  `--write` its `storage.create` (`url-write`). A GET of the object in
  it at each director's object route, with the token in the query as
  curl sends it, must redirect with `authz` in `Location` and no token
  in `Link`, and following the redirect must serve the object (`curl`).
  And `pelican object get` of the URL, with no other token, must serve
  it too (`get`): nothing in Pelican's client reads `authz` back from a
  URL, so it finds no token (error 4010), and the row fails, on
  v26.0.0-rc.3 and main alike (L1). It skips without the origins'
  embedded issuer, and for a standalone origin.
- `director-decision`: with `Plugin.DirectorDecisionPercentage` at 100,
  stash_plugin asks the director to explain its decision
  (`X-Pelican-Debug`), and must record it in the ad's
  `DeveloperData.DirectorDecision`: the sort method, the client's
  coordinate, and each server's weights (`redirectInfoToClassAd` in
  `cmd/plugin.go`; the `routing` suite checks the director's answer
  itself).
- `skipstat-failover`, with several origins: an object put in the last
  origin's store alone, read by `pelican object get --direct` with
  `?skipstat`, which keeps the director from asking the origins which
  holds it (`routing`'s `stat skipstat`), must still come back, since
  the client tries the origins the director lists until one serves
  (`getObjectServersToTry` in `client/handle_http.go`). The note says
  whether the client's stats show a failed try at an origin that lacks
  it, or the client's probes of the endpoints put the holder first.
- `multi-source`: `get` with several sources into an existing
  directory lands each by its name (G8 of Pelican's
  `docs/object-transfer-semantics.md`; `get-directory`); into a path
  that does not exist, or onto a regular file, it must fail, saying
  "Destination does not exist" or "Destination is not a directory"
  (G9, G10; `get-missing-dir`, `get-file`). Where `/protected-a` takes
  writes, `put` with several sources to an existing collection, and to
  a path that does not exist, lands each under it by its name (P8, P9;
  `put-collection`, `put-new`); to an existing object, each source is
  put as given, so the first is refused as existing, and the object is
  left as it was (P10, with overwrites off; `put-object`). `put` of a
  directory to a URL with `?recursive` lays the tree out flat, as `-r`
  would (P6; `put-recursive-query`), and `get` of a collection's URL
  with `?recursive` brings it back, where the namespace has `Listings`
  (`get-recursive-query`).
- `preferred-cache-down`: `pelican object get --cache
  https://<cache>:1,+`, a preferred cache that answers nothing (no
  server listens on port 1) and then, with `+`, the director's caches:
  one of them must serve, as the stats show (`plus`). Without `+`, the
  dead cache is the client's only one, and the get must fail to reach
  it, by something other than a refusal (`alone`).
- `async-job`: `pelican object get --async --wait` hands a download to
  the client agent (`pelican client-agent`, a daemon of the client's
  own, which the command starts if none runs; `cmd/object_get.go` and
  `client_agent/` in Pelican), and waits for it: the file must be the
  object (`get`). `pelican job status --json` must then report the job
  `completed`, with one transfer and the object's bytes (`status`), and
  `pelican job list --json` list it (`list`). `pelican object copy
  --async --wait` of a local file to `/protected-a`, where it takes
  writes, must land it at an origin (`copy`). `pelican job cancel` of
  the finished job must leave it `completed`, whether it refuses or
  says that it cancelled it (`cancel-finished`). Last, whatever else
  happened, `pelican client-agent stop` must stop the agent, so that
  `client-agent status` says within 15 seconds that none runs (`stop`).
  The agent's socket, PID file, and database are the run's own
  (`ClientAgent.Socket`, `PidFile`, and `DbLocation`, by environment),
  so that no agent of the user's, or of an earlier run's, is spoken to.


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
  and `stash_plugin` first. Each client reads its set the three ways a
  client reaches an object, as the `transfers` suite does: through the
  cache the director picks; direct from an origin (`--direct`, or
  `?directread`), which must be refused where `/protected-a` takes no
  direct clients; and from each federated cache in turn, named
  (`--cache`, or `Client.PreferredCaches`), which alone may serve it, as
  the client's transfer statistics and the plugin's result ads must say,
  so that each kind of cache the shape has (`-p cache-mixed`) serves a
  client that names it.
- `director-read`: a plain HTTP client that asks a director for a cache
  (`/api/v1.0/director/object/`) and sends the same request, token and
  `Range` and all, to wherever the redirect (307) leads, as `curl -L`
  does. Through each director, every object of the set read whole
  first, whole and in every range, must come back as the object's
  bytes, as from the cache itself; the redirect must lead to a
  federated cache, or, with no cache (`-p topo-no-cache`), to an
  origin. It skips on a standalone origin, which has no director.
- `cache-abandoned` starts a cold read of an object through each cache,
  and gives up after 50,000 bytes; those bytes, and the object's last
  block and the whole object afterward, must be the object's. A V2
  cache may go on fetching the object, or not; the row's note says how
  much of it the cache's introspection then said it held.
- `cache-corrupted` has each V2 cache spoil its own copy of an object it
  holds whole, through its chaos API (`Cache.EnableChaosAPI`, which the
  framework turns on; Pelican v7.27.0 and v26.0.0-rc.0 and later, so the
  scenario skips on an older cache): a block corrupted in its store, and
  bytes cut from the end of another object's chunk file. A read of the
  spoiled part, and of the whole object, must then come back as the
  object's bytes or fail, never as other bytes, and within 30 seconds
  the whole object must come back intact, fetched afresh. An XRootD
  cache has no such API, and is passed over.
- `transfer-status`: a client that asks for a transfer's status
  (`X-Transfer-Status: true` and `TE: trailers`) learns of a failure
  that comes after the headers from a trailer
  ([self-tests](#self-tests)). Each federated cache, V2 or XRootD (whose
  HTTP server sends it from XRootD 5.6 on), so asked, must announce
  `Trailer: X-Transfer-Status` and end with that trailer, `200: OK`,
  for a whole object, the range `bytes=4080-12239`, the last 1,000
  bytes, and a 512-byte object, which a V2 cache keeps in its database:
  each of an object of its own, read cold and then warm. A GET that
  does not ask must get no trailer; a HEAD that asks has no body, and
  so no trailer, but must get the object's length.
- `transfer-status-failure`: each federated V2 cache holds an object
  whole; its chaos API then corrupts the object's fourth block, past
  what the cache reads before it sends the headers, and the object is
  removed from every origin's store, so that the cache cannot repair
  the block, as it otherwise would (`cache-corrupted`). A GET that asks
  for the status must then get 200 and a trailer that reports a
  failure (Pelican's is `500: ...`), or fail before the body, as where
  the cache's copy has gone stale and no origin has one. Through that
  cache alone, `pelican object get --cache` must then fail, by
  something other than a refusal, and leave nothing; and so must
  stash_plugin, preferring that cache alone
  (`Client.PreferredCaches`), whose ad must say `TransferRetryable =
  true`, since the transfer failed after it began, unless the director
  found no origin holding the object. The row's note says how each
  failed. A cache that serves the object intact never read the
  corrupted block, as where it holds it in memory (`-p cache-memory`),
  and leaves the row inconclusive.
- `origin-transfer-status`: Pelican's `docs/pelican-http-headers.md`
  says that origins, like caches, send the trailer when asked. A GET of
  an object straight to each origin, so asked, must get it, `200: OK`.
  An XRootD origin sends it; nothing in Pelican's native origin
  (`origin_serve/`) does, so the row fails there, on main and
  v26.0.0-rc.3 alike. It skips where the origins take no direct
  clients.
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
  in parts (`framework/testlib/stores.py`). A federated V2 cache relays
  the origins' digests (`digest-relay`), so it must report the same of
  the objects that `cache-whole-first` reads whole first; of those that
  `cache-range-first` reads through a range first it need report none,
  since it records none of them (`digest-range-first`), and neither
  need an XRootD or site-local cache. Over XRootD origins, a V2 cache
  knows only their `md5` of an object it has not read (`digest-relay`),
  so `digests` run without `cache-whole-first` fails there. The row's
  note names the servers that report none. `digest-overwrite` then
  overwrites an object at each origin, by a PUT at once and by a write in
  the store a second later: a native origin keeps digests by the file's
  modification time,
  to the second, and must not report a stale one; and its `ETag`, by
  which a cache tells that an object has changed, must change too.
- `digest-defaults` asks each origin for the digests of an object of
  its own in the other ways a client may. A HEAD with no `Want-Digest`
  must get `crc32c` from a native origin, the first of
  `Origin.DefaultChecksumTypes`, which the framework leaves at its
  default (`handleHeadWithChecksum` in `origin_serve/handlers.go`); an
  XRootD origin need send none. A HEAD with `Want-Digest: crc32c, md5`
  must get both from a native origin, in one `Digest` field, since the
  client reads one; an XRootD origin answers with the one it prefers,
  and must send at least one. A HEAD with `Want-Digest: md5;q=0.5,
  crc32c;q=1`, as RFC 3230 allows, must get at least one from every
  origin. Each is asked only of what the origins' storage reports (see
  `digests`), and every digest must be the object's. The row's note
  says what each origin sent. Pelican's native origins take each
  algorithm's name with its `;q=` and match none
  (`xattrChecksumAdapter.GetDigests` in `origin_serve/backend.go`, and
  likewise for `pstore` and `s3v2`), so they report nothing, and the
  row fails there, on main and v26.0.0-rc.3 alike. On a `posixv2`
  origin, the digests must then be in the file's `user.XrdCks.crc32c`
  and `user.XrdCks.md5` extended attributes, each the object's; a PUT
  of another version must remove them or replace them with its own,
  and the origin then report that version's. Last, the test writes
  bytes of the same length over the file in the store, and gives it
  back its modification time. The origin keeps digests by that time,
  to the second, as XRootD does, so it may report the old version's
  digests, or the new bytes' if it noticed, but no others; the note
  says which. A store rewritten behind the origin's back, its time
  kept, is beyond what the origin can see, so the old digests are
  accepted (`stale-origin-digest` relies on them). Where the store's
  extended attributes cannot be read, the row is inconclusive. It skips
  where the origins take no direct clients, or their storage reports no
  digest.
- `digest-relay`: a V2 cache relays the origins' digests, as an RFC
  3230 `Digest`, so that a client can check what it downloads through
  it (`local_cache/persistent_cache_api.go`). Of an object of its own
  at each federated V2 cache: a cold HEAD, which the cache answers from
  an origin's answer, and once it has read the object whole, a HEAD and
  a GET, from what it recorded, must each report the digests that the
  origins' storage holds (`crc32c` and `md5`, but for those it cannot;
  see `digests`), each the object's, whether `Want-Digest` asks for
  `crc32c` or `md5`. A cold GET may come before the cache knows them,
  and the row's note says whether it carried any. Then `pelican object
  get --cache` must bring the object, and its transfer statistics
  (`--transfer-stats`: the `serverChecksums` that it asked the cache
  for once the body came) must hold the same digests, each the
  object's; the client has no `--require-checksum` for a download. A V2
  cache asks an origin for every digest in one `Want-Digest`, which an
  XRootD origin answers with one, `md5`, so over XRootD origins
  (`-p origin-xrootd`) the cold HEAD lacks `crc32c`, and the row fails
  there, on main and v26.0.0-rc.3 alike. It skips where the origins'
  storage reports no digest.
- `digest-range-first`: the same, of an object that each federated V2
  cache first reads through a range, of its second block: a HEAD must
  report the origins' digests once it has, and again once it has read
  the object whole (it sends `Age`). A V2 cache fills a range of an
  object it lacks block by block, after a stat of the origin that asks
  for no digest (`initObjectFromStat` in
  `local_cache/persistent_cache.go`), and records none, so it relays
  none of that object, and the row fails, on main and v26.0.0-rc.3
  alike.
- `stale-origin-digest`: a V2 cache checks what it fetches against the
  digests that an origin reports, and must neither pass on as good, nor
  keep, an object whose bytes fail them. A `posixv2` origin keeps an
  object's digests in its file's extended attributes (`user.XrdCks.*`,
  as XRootD does), with the file's modification time, to the second,
  and reports them while that time is unchanged
  (`origin_serve/checksum.go`). So, for each federated V2 cache, every
  origin first reports every digest of an object of its own (a HEAD
  with `Want-Digest: md5,crc32c,crc32,sha`, as a V2 cache asks); the
  test then writes another version of the same size over it in each
  store, and gives the file back its modification time, after which
  the origins serve the new bytes with the old digests. A cold GET
  through the cache, asking for the status, must then end with a
  trailer that reports a failure, or fail; within 10 seconds a GET with
  `only-if-cached` must get 504, since the cache kept nothing; and
  `pelican object get --cache` must fail, by something other than a
  refusal, and leave nothing. Where a store's files show the tests no
  such attributes (a filesystem without them, or a host that does not
  pass them through), or an origin notices the overwrite, the row is
  inconclusive. It runs only over `posixv2` origins that take direct
  clients, which it asks first.
- Ranges past an object's end: a suffix longer than the object is all
  of it, and a range that runs past the end stops at its last byte; one
  that starts at or past the end (`past-end`) must get 416, or, from a
  server that ignores `Range`, the whole object. In a set of ranges, one
  past the end is passed over, as RFC 9110 says.
- `huge-ranges`: an object past 4 GiB, which no suite could move whole:
  4 GiB and 128 KiB, rounded up to a block boundary, and a byte more
  (4,295,101,681 bytes), so that 2^32 falls inside a block (no power of
  two is a multiple of 4,080) and the last block is one byte, and a size
  or an offset that a server keeps in 32 bits, signed or not, goes wrong
  somewhere in it. The suite writes it as a sparse file in each origin's
  store on disk: zeros, but for 64 KiB markers of bytes of their own at
  its start, around 2 GiB and 4 GiB, and at its end, with holes between,
  which cost nothing to keep, and which a cache fetches no more of than
  a range reads. At each origin
  that takes direct clients, through each director's object route,
  which leads to a cache, and at each cache, a HEAD must give its
  length, and nine ranges, each within a marker, must come back as 206,
  with a `Content-Range` of the bytes asked for out of its size, and
  exactly those bytes: 200 bytes across 2 GiB, and across 4 GiB; the
  byte before each, and the byte at each; the last byte; the last 1,000
  bytes, as a suffix; and from 1,000 bytes before the end on. A server
  that ignored `Range` would send the whole object, so the suite reads
  64 KiB of an answer and hangs up. A PROPFIND of Depth 0 at each origin
  and cache must say its size, as must `pelican object stat --json`,
  and `pelican object ls -l --json` of its collection, where
  `/protected-a` has `Listings`. Afterward, it is evicted from each V2
  cache, whose accounting may have charged its store the whole object.
  It skips on a pstore origin, whose store is encrypted, so that the
  object would have to go over HTTPS whole; and where a store keeps no
  sparse files, so that the object would take 4 GiB on disk.
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
- `etag-agreement`: an object's `ETag` names its version, whichever
  method reports it (RFC 9110, section 8.8.3); a cache revalidates by
  it, and a writer compares and swaps by it. Of an object of its own,
  the answer to each origin's PUT that wrote it, a GET, a HEAD, and
  PROPFIND's `getetag` must all report one ETag. Then a GET with
  `If-Match` naming the GET's ETag must get the object; a GET of a
  range with `If-Range` naming it, 206 and the range; and a PUT of
  another version with `If-Match` naming it must replace the object.
  The same again with the ETag in that PUT's answer. A weak ETag, which
  neither condition can match, is not tried. A `posixv2` origin makes
  the ETag of a GET and of a PUT's answer itself, from the file's
  device, inode, size, and modification time (`computeETag` in
  `origin_serve/handlers.go`), while its HEAD and PROPFIND, and its
  checks of `If-Match` and `If-Range`, use the backend's, from the
  modification time and size (`etagFileInfo` in
  `origin_serve/backend_etag.go`). So its GET's ETag is not its HEAD's,
  and the conditions that name it fail: `If-Match` gets 412, and
  `If-Range` the whole object. The row fails there, on main and
  v26.0.0-rc.3 alike. Where `/protected-a` takes no writes, the object
  is put in each store, and only GETs are sent. It skips where the
  origins take no direct clients.
- `conditional-edges` sends each origin conditions at their edges, of an
  object of its own (RFC 9110, section 13). A GET of an object that does
  not exist, with `If-None-Match` naming an ETag or `*`, must get 404,
  not 304. A GET with an invalid `If-Modified-Since` must get the
  object, since a server ignores an invalid date; and one with
  `If-None-Match` that does not hold, beside an `If-Modified-Since` that
  would, must get it too, since `If-None-Match` decides. A GET with
  `If-Unmodified-Since` after the object's `Last-Modified` must get the
  object, and one with a time before it, 412. Where `/protected-a` takes
  writes, a PUT with `If-None-Match: *, "junk"` over the object must be
  refused (412), changing nothing, since a `*` in the list still names
  any object. Then 8 PUTs at once of a new object, each with
  `If-None-Match: *` and bytes of its own: exactly one may succeed, the
  rest must get 412, and the store must then hold the one's bytes. A
  native origin checks a PUT's condition before it writes
  (`checkPutPreconditions` in `origin_serve/preconditions.go`), and only
  a `pstore` origin checks it again as it commits the object, so
  elsewhere two racing PUTs may both find no object, and both succeed;
  whether the row fails there depends on timing. XRootD's HTTP server
  ignores conditions on a PUT (see `conditional-put`), so the PUT rows
  fail on an XRootD origin. It skips where the origins take no direct
  clients.
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
- `coalescing`: a cache is there to spare the origins. Each federated
  cache, read cold and whole by 16 clients at once, of a 4 MiB object of
  its own, must fetch it from the origins about once: the native
  origins' counters of GETs (`pelican_http_bytes_total` and
  `pelican_http_requests_total`, `metrics/http_metrics.go`) must show
  less than two objects' worth of bytes sent, and, for a V2 cache,
  which fetches an object whole in one GET, no more than two GETs under
  the username of the tests' token. An XRootD cache fetches an object a
  block at a time, so only its bytes are judged. Then 16 warm reads
  must have the origins serve no GET under that username, and send no
  more than the directors' health tests do (64 KiB). Other traffic
  moves the counters too, so no count is exact; where the origins
  counted fewer bytes than the object, the row is inconclusive. It
  skips where XRootD serves the origins, which keep no such counters. A
  site-local cache reads through another cache, so this and each
  scenario below that names federated caches leave it to `sitelocal`.
- `cache-assemble` reads unaligned, overlapping ranges that together
  cover an object, out of order. The cache must hold part of it after
  the first and all of it after the last. A V2 cache's introspection
  (`/api/v1.0/cache/introspect/metadata`, as its admin) summarizes which
  blocks it holds: after the first range, which starts in the object's
  middle, it must hold that range's blocks, and not block 0, since it
  fetches a range's blocks rather than fill the object from its start
  (`initObjectFromStat` in `local_cache/persistent_cache.go`); within 10
  seconds of the last, it must hold every block. A V2 cache whose
  introspection gives no summary is judged by `Age`, which it sends only
  once it holds every block. An XRootD cache's `.cinfo` (read from
  `data/cache/<N>/`) records which blocks it holds. When a cache cannot
  show the partial state (it sends `Age` and gives no summary, has no
  readable `.cinfo`, or its `.cinfo` shows none or all of the blocks
  after the first range), the row is inconclusive; but an XRootD cache
  whose `.cinfo` could be read after the first range fails if, after the
  last, it has none, or one that doesn't show every block.
- `overwrite-cached` and `overwrite-range-first` overwrite objects at the
  origins once each cache holds them (read whole, or a range first): one
  keeping its size, one growing, and one shrinking. Every response, whole
  or ranged, must then be entirely one version, with that version's
  size, and a whole response's digest (`Want-Digest`), if one comes, that
  version's: a stale one would fail the client's check of bytes that are
  right. Without
  `Cache-Control`, a cache decides for itself how long its copy stays
  fresh. Under `-p origin-max-age`, every native origin must send
  `Cache-Control: max-age=30`, and every cache must then serve the new
  version, and only it, once its copy is stale. The scenarios wait up to
  about 75 seconds for it. An XRootD origin sends none
  (`Origin.CacheControl` is the native origin's), so there the caches
  decide for themselves, as without.
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
  its high water mark by half, and reads the first again once it has read
  as many as fit under the mark, less one. Within 60 seconds its store
  (`data/cache/<N>/persistent-cache/objects/`) must have come back under
  the mark, and not fallen below the low one by more than an object.
  Then, asked with `only-if-cached`, it must still hold the three
  objects read last, and none of at least two of the oldest. The first
  object, read again, may be held or gone: a V2 cache records an access
  for its LRU order at most once in ten minutes (`UpdateLRU` in
  `local_cache/database.go`), so reading it again may move it nowhere;
  the row's note says which. Last, every object must still come back
  whole and in a range, as must the first by `pelican object get
  --cache`. An XRootD cache measures its filesystem, which a bind mount
  cannot bound, so `fed.sh` refuses the preset with `cache-xrootd`.


## Cache freshness

An origin tells a cache how long its copy of an object stays fresh with
`Cache-Control` (Pelican's `Origin.CacheControl`, which only a native
origin sends; RFC 9111). A V2 cache keeps what the origin says, and
decides for itself where it says nothing (`local_cache/cache_control.go`
in Pelican): a day, less up to 10% (`LocalCache.DefaultMaxAge` and
`LocalCache.RevalidationJitter`). It tells its own clients how fresh its
copy is, with `Cache-Control` and `Age`. The `freshness` suite checks
both, at each federated cache, with objects of its own under
`/protected-a/data/freshness/<run>/`, put at every origin. A site-local
cache's upstream is another cache, whose `Cache-Control` is its own, so
the suite leaves it to `sitelocal`. XRootD caches do not read
`Cache-Control` as a V2 cache does, so only `no-negative-cache` asks
them.

Each case needs the origins to send a `Cache-Control` of its own, or
any. The suite runs the cases in phases, one per value: first those that
take what the origins send as the shape is, which is nothing, or
`max-age=30` under `-p origin-max-age`; then, for each other value, it
restarts every origin with `Origin.CacheControl` set to it, as the last
of its configuration layers ([service control](../README.md#service-control)),
runs the cases that need it, and restarts the origins again without it.
Before a phase's cases, and after it, every director must name every
origin, and every cache answer a HEAD of a probe object, within 150
seconds; and where the origins take direct clients, each must send the
value. `ORIGIN_CACHE_CONTROL` reaches Pelican by environment, which no
file overrides, so the restarts need a shape without it, of native
origins, with a control helper, and not the `tiny` topology, whose
origin is the whole federation's process. Elsewhere, the cases that
need a restart skip, and say why. A case's row is named `<scenario>
<case>`, e.g. `cc-immediate max-age=0`.

- `only-if-cached`: a client may ask a cache for only what it holds
  (`Cache-Control: only-if-cached`, RFC 9111 section 5.2.1.7, or the
  older `X-Pelican-NoDownload: true`), and a V2 cache must answer 504 if
  it holds none. At each V2 cache, a GET and a HEAD of a 1 MiB object it
  lacks, asked each way, must get 504, as must a GET 2 seconds later;
  meanwhile, the native origins must send fewer bytes for GETs than one
  such object (`pelican_http_bytes_total`, which other traffic moves
  too, but not by as much). A plain HEAD of another such object must
  get its `Content-Length`, and fetch nothing: `only-if-cached` must
  then get 504. An object the cache holds whole (it sends `Age`) must
  come back to a GET asked either way, and to a HEAD with
  `only-if-cached`.
- `no-negative-cache`: a GET of an object that no origin holds yet,
  at each cache, must get 404, a V2 cache's a JSON body naming
  `not_found`; as must one at each director's bare path, followed to
  wherever it leads. Once every origin holds the object, each must
  serve it at the first try. Where there are several origins, a
  director asks them which holds an object, and keeps an answer that
  none does for a minute (`Director.CachePresenceTTL`), so there each
  may take 75 seconds; the row's note says how long they took.
- `cc-downstream`: at each V2 cache, a cold GET of an object, which
  fills the cache; once it holds it, two GETs 3 seconds apart; and a
  GET with `If-None-Match` naming its `ETag`, which must get 304. Each
  answer must carry `Cache-Control`, the 304 too. With the origins
  sending none (`cc-downstream none`), it must be `max-age` of what
  remains of the cache's own freshness: at most a day, and at least
  21.6 hours less the time since the fill. With the origins sending
  `max-age=30` (`cc-downstream max-age`), it must be `s-maxage=30,
  max-age=30`, so that a cache downstream, which is shared, keeps it as
  long (`ResponseCacheControl` in `local_cache/schema.go`). The fill may
  carry no `Age`, or one of at most 2; each later `Age` must be no more
  than the time since the fill, and the second must exceed the first by
  the time between them, within 2 seconds.
- `propfind-freshness`: a V2 cache answers a Depth 0 PROPFIND from its
  copy, if it holds one. `propfind-freshness empty`: of an empty object
  it holds, it must say that it is 0 bytes long, since every object has
  a `getcontentlength`, 0 included; if the cache never says, with `Age`,
  that it holds the object, a pass is inconclusive, since an origin may
  have answered. Pelican writes `getcontentlength` only for an object
  longer than 0 bytes (`buildPropfindMultistatus` in
  `local_cache/persistent_cache_api.go`), so the row fails, on main and
  v26.0.0-rc.3 alike. `propfind-freshness overwrite`, under
  `max-age=30`: an object the cache holds is replaced at the origins by
  one 1,000 bytes longer; once its copy is stale, Depth 0, with no GET
  in between, must say the new size, and an `ETag` other than the old
  one (where the origins take direct clients, one of theirs), within 15
  seconds. Pelican answers Depth 0 from whatever copy it holds, stale
  or not (`servePropfindFromCache` in
  `local_cache/persistent_cache_api.go`), so the row fails, on main and
  v26.0.0-rc.3 alike.
- `stale-if-error`, under `max-age=30`: at each V2 cache, an object it
  holds, which is then stale, and another it never read. With every
  origin stopped, the first must still come back, its bytes, as a V2
  cache serves its copy when it cannot revalidate it
  (`revalidateObject` in `local_cache/persistent_cache.go`); the other
  must fail, and never come back with other bytes. Then the origins
  start again, and the federation must serve as before. It skips
  without a control helper, and in the `tiny` topology.
- `cc-immediate`: under `max-age=0`, `no-cache`, and
  `must-revalidate, max-age=0`, `blocks`'s `overwrite-cached` and
  `overwrite-range-first` ([block boundaries](#block-boundaries)): of
  three objects read whole and three read through a range first, each
  replaced at the origins by one as long, longer, or shorter. A V2
  cache must serve each new version, whole, at the first read after:
  for `no-cache`, within the 5 seconds' grace it allows between
  revalidations (`NoCacheGracePeriod`), and 3 more, of when it last
  read the old one. Then every read, whole and in every range, must be
  the new version alone, with that version's digest, if one comes.
  Pelican keeps `max-age` only if it is above 0, and so takes
  `max-age=0` for no `max-age` at all, and keeps the object a day
  (`GetCacheDirectives` in `local_cache/schema.go`); and it never reads
  `must-revalidate`. So the `max-age=0` and `must-revalidate` rows
  fail, on main and v26.0.0-rc.3 alike.
- `cc-not-stored`: under `no-store`, and again `private`, which a shared
  cache must not store (RFC 9111, sections 5.2.2.5 and 5.2.2.7), a V2
  cache passes an object through. At each, two GETs of an object must
  each get it, with the directive in `Cache-Control`, and no `Age`; and
  every range of `blocks`'s, the range, or the whole object with
  `Accept-Ranges: none`, as RFC 9110 lets a server ignore `Range`, and
  as Pelican's pass-through does (the row's note counts them). Then a
  GET and a HEAD with `only-if-cached` must get 504; the cache's
  introspection (`/api/v1.0/cache/introspect/list`, as its admin) must
  list none of the object; and its store's object files
  (`data/cache/<N>/persistent-cache/objects/`) must not have grown by
  as much as the object.
- `s-maxage`: under `max-age=120, s-maxage=20`, an object each V2
  cache has read is replaced at the origins by a longer one. RFC 9111
  (section 5.2.2.10) says that a shared cache, as every Pelican cache
  is, must take `s-maxage` over `max-age`, so each must serve the new
  version within 35 seconds of reading the old one. Pelican takes the
  larger of the two, on purpose (`ParseCacheControl` in
  `local_cache/cache_control.go`), and so keeps the old one for 120
  seconds, and the row fails, on main and v26.0.0-rc.3 alike.

Each new version is written at least 1.1 seconds after the old one,
since a native origin's `ETag` follows a file's modification time, to
the second.


## Cache APIs

Besides the objects it serves, a V2 cache serves APIs of its own on its
web port (`local_cache/persistent_cache_api.go` and
`prestage_evict_api.go` in Pelican): prestaging and evicting objects,
for whoever may read or modify them, which the client's `pelican object
prestage` and `pelican object evict` call; its usage
(`/api/v1.0/cache/stats`), for anyone; and purges and introspection, for
its admin alone, among them a chaos API that corrupts what it holds
(`Cache.EnableChaosAPI`, which the framework turns on). The `cache-api`
suite checks them at each federated V2 cache, with objects of its own
under `/protected-a/data/cache-api/<run>/`, put at every origin, a set
per cache, so that each is cold where it is first read. Whether a cache
holds an object it asks with a HEAD with `Cache-Control:
only-if-cached`, which gets 200 if it holds all of it, and 504 if not
([cache freshness](#cache-freshness)). A site-local cache serves the
same APIs, and is left to `sitelocal`. XRootD caches serve none of them
but prestage, through the xrdhttp-pelican plugin, so only `prestage
client` asks them. A scenario records a row per case, named `<scenario>
<case>`.

- `prestage`: `GET /pelican/api/v1.0/prestage?path=<path>` fetches an
  object whole, and streams its progress in chunked `text/plain` lines,
  `status: queued` and `status: active,offset=<bytes>`, until a last
  line of `success: ok`, or of `failure: <code>(<text>): <message>`.
  `probe`: with no `path`, which is how the client asks whether a cache
  has the API, and with a relative one, it must get 400. `refused`: with
  no token, or one scoped elsewhere, 403, with the object left
  uncached. `object`: of an object the cache lacks, with a read-only
  token, the progress, ending well, after which the cache must hold the
  object, and serve it; and a second prestage of it must end well too.
  `missing`: of an object no origin holds, 200, since the cache has
  answered before it knows, and a last line of `failure: 404(Not
  Found): ...`. Pelican answers `failure: 500(Internal Server Error)`,
  since nothing makes the status that the handler would report for a
  missing object (`statusCodeErr` in `prestage_evict_api.go`), so the
  row fails, on main and v26.0.0-rc.3 alike. `encoded-name`: of an
  object named `plus+<cache>`, its path encoded once in the query, as
  the client sends it, beside a decoy named `plus <cache>`: the cache
  must hold the first afterward, and not the decoy. Pelican decodes
  `path` twice, once as a query and again with `url.QueryUnescape`, so
  it takes the `+` for a space and prestages the decoy, and the row
  fails, on main and v26.0.0-rc.3 alike. `client`: at every cache, V2 or
  XRootD, `pelican object prestage --cache` of an object must exit 0 and
  leave the cache holding it (an XRootD cache's `.cinfo` showing every
  block); of a collection, where `/protected-a` takes listings and
  direct clients, so that the client can list it, every object in it;
  and of a missing object it must exit non-zero.
- `evict`: `GET`, `POST`, or `DELETE /pelican/api/v1.0/evict?path=<path>`
  evicts every object the cache holds at that path or under it, at once
  with `immediate=true`, and otherwise marks them to go first when it
  next evicts; it takes a token with `storage.modify` for the path.
  `refused`: an immediate eviction of an object the cache holds, with no
  token, a read-only one, or one scoped elsewhere, must get 403, and
  leave it held; as must one of an object in `/public`, with no token
  and with `/protected-a`'s, which is for another namespace, whether or
  not `/public` takes writes (`-p origin-public-writes`).
  `immediate`: by each method, with the `server` token, 200 and
  `Evicted 1 objects under <path>`, after which the cache must not hold
  the object, and a GET must fetch it again. `marked`: without
  `immediate`, 200 and `Marked 1 objects for priority eviction under
  <path>`, with the object still held. `prefix`: of `foo`, while the
  cache holds `foo/a`, `foo/b`, and `foobar`, `Evicted 2 objects`, and
  `foobar` still held, since a prefix matches whole path segments.
  `encoded-name`: of `plus+<cache>`, beside a decoy `plus <cache>`, both
  held, the first gone, and the decoy held. Pelican decodes `path` twice
  here too, and evicts the decoy, so the row fails, on main and
  v26.0.0-rc.3 alike. `client`: `pelican object evict --immediate` of
  an object every V2 cache holds, which asks the first cache the
  director names, must print `Evicted 1 objects under <path>`, and leave
  one cache without it; without `--immediate`, `Marked 1 objects ...`,
  and every cache holding it. Each object marked is then evicted, which
  drops its mark too, so that `purge` finds only its own. `pelican cache
  evict` reads the cache's own configuration and address file, so it
  runs only beside the cache, and is not tested.
- `admin-authz`: a row per cache and admin route: `POST` to
  `/api/v1.0/cache/purge`, `purge_first`, and `purge_to_target`; `GET`
  of `/api/v1.0/cache/introspect/list`, `etags`, `metadata`, and
  `stats`; and `POST` to `introspect/verify`, `disk-usage`,
  `consistency`, and `chaos`. Each request would harm nothing were it
  let through: `chaos` and the routes that take an object name one no
  cache holds, `consistency` has both its scans off, `purge_to_target`
  has a target of 0, and `purge_first` no body. With no token, each
  must get 401; with `/protected-a`'s `server` token, and with another
  server's admin token (the first origin's, signed with its own key;
  in the `tiny` topology, whose servers share one, one naming the
  cache's issuer but signed by `unknown.pem`), 401 or 403. With the
  cache's own admin token, as `pelican cache introspect` mints it, each
  introspection route must give its own answer to the request: 200 for
  `list`, `etags`, `stats`, and `disk-usage`, 404 for `metadata` and
  `verify`, and 400 for `consistency` and `chaos`. A purge route also
  wants `localcache.purge` (`token.Verify` in `purgeCmd` and its
  siblings), as `pelican cache purge` mints it: with the admin token
  alone, it must get 403, and with the scope too, 400 for both requests.
  `purge` itself is sent with that scope only by the `purge` scenario.
  The `chaos` row skips where the admin finds no such route.
- `purge`, with the admin token that has `localcache.purge`.
  `to-target`: of a cache holding objects of the run's,
  `purge_to_target` with `target_bytes` 0 must get 400; with a byte
  below what the cache holds, 200, with `freed_bytes` above 0, after
  which it must hold no more than that. What a cache holds, here, is
  what its stats' `NamespaceUsage` charges the storage that `DirStats`
  names, the counters in its database by which it purges. `first`: of
  three objects it holds, read in turn, `purge_first` of the last read
  must get 200, and then a purge to a byte below what it holds must
  evict that one, and leave the other two held, since a purge takes the
  objects marked to go first before any other (`EvictByLRU` in
  `local_cache/database.go`). An object an earlier run marked and left
  may go first instead, so the purge is repeated, up to three times,
  until the marked object goes; the row's note says if it took more than
  one. `all`: `purge` must get 200, and under `-p cache-small` leave the
  cache holding no more than its low water mark. A cache already
  evicting answers a purge 500, so a purge that gets one is sent again,
  up to three times, five seconds apart.
- `allowed-federations`: a V2 cache serves its data, besides at bare
  paths, under `/api/v1.0/cache/data/<discovery host>/<path>`, only for
  the federations in `Cache.AllowedFederations`, which is empty by
  default and so allows only its own: otherwise anyone could use it to
  fetch from anywhere. `foreign`: an object asked for under
  `evil.invalid%3A443` must get 403, with JSON whose `error` is
  `federation_not_allowed`, and none of the object's bytes, and the
  cache must not hold it afterward. `own`: under the federation's own
  discovery host, as is, with its colon escaped, and in upper case,
  which the cache compares without regard to case, the object's bytes.
- `usage`: `accounting`: of three objects of 100,000 bytes, each read
  cold and whole, the cache stores each in a file of its blocks and a
  16-byte tag for each, 100,400 bytes. What it charges `/protected-a`
  (`NamespaceUsage`, by the namespace IDs that
  `introspect/stats`'s `namespace_names` names) must grow by exactly
  three times that, and what it holds in all (`TotalUsage`) by at
  least that; its object files on disk by that, give or take 64 KiB;
  and what its introspection measures of them (`introspect/disk-usage`)
  must agree with the files, as closely. Objects under 4,096 bytes
  would be stored in its database, so these are larger. Under
  `-p cache-small`, the cache is first purged to its low water mark,
  so that the reads evict nothing. `water-marks`: under
  `-p cache-small`, its stats' `DirStats` must give its high and low
  water marks, 128 and 96 MiB; elsewhere, the row skips.
- `consistency`: of an object the cache holds whole, its chaos API
  corrupts block 1 in its store, and three rows judge what follows.
  `verify`: its `introspect/verify` must say `valid: false`.
  `scan-counts`: its data scan (`introspect/consistency` with
  `metadata=false`, a stream of server-sent events ending in `done`)
  must count a checksum mismatch more than before it, and
  `pelican_cache_data_scan_inconsistent_objects_total` must rise.
  `scan-drops`: the scan must drop the object, which the cache must no
  longer hold, and a GET must then fetch it intact. A scan must catch
  what a reader would. Pelican's reads the object through its
  decryption, whose check of each block's tag fails first, and takes
  that for an error reading the object, which it logs, counting no
  mismatch and keeping the object (`verifyObjectChecksum` in
  `local_cache/consistency.go`), so the `scan-counts` and `scan-drops`
  rows fail, on main and v26.0.0-rc.3 alike. A cache whose verify finds
  the object valid has not seen the corruption, as where it holds the
  block in memory (`-p cache-memory`), and leaves all three rows
  inconclusive.
  `orphan`: a stray file, named as an object's would be and made an
  hour old, since a metadata scan passes over younger files, planted in
  the cache's store; its metadata scan (`data=false`) must remove it,
  and count an orphaned file more than before. Where the suite cannot
  write the store, the row skips.


## Throttling

No server is slow as a shape is, so the `throttling` suite makes them
so for as long as it runs. It restarts every origin with
`Origin.TransferRateLimit: 256KB/s` (a KB is 1,024 bytes), and every
federated V2 cache with a small fair scheduler: `Cache.WorkerCount: 2`,
`Cache.Throttle.PerOriginStarvingPercent` and `PerOriginActivePercent`
25, `PerOriginPendingSize` 1, and `RetryAfter` 2s. A cache then fetches
from one origin one object at a time, queues one more, and sheds the
rest. Both go in the last of the servers' configuration layers
([service control](../README.md#service-control)); afterward the suite
restores them, and waits, before and after, until every director names
every origin and every cache answers, as `freshness` does. The rate limit
slows only a `posixv2` origin (`origin_serve/rate_limited_fs.go` in
Pelican), so the suite needs `posixv2` origins, a federated V2 cache,
and a control helper, and skips the `tiny` topology, whose servers are
one process. Otherwise it skips, and says why.

The limit is one bucket per export, which every read and write of it
shares, and which lets a second's worth through at once. So the cases
run one at a time, and the suite writes its objects straight into the
origin's store on disk, under `/protected-a/data/throttling/<run>/`, a
set per cache, so that each is cold where it is first read. Only the
first origin holds them, so that a director with several sends every
fetch there, and a cache's fetches share one scheduler queue, which is
per origin host. A scenario with several cases records a row per case,
named `<scenario> <case>`.

- `shed-429`: at each V2 cache, 30 reads at once of cold 1 MiB objects,
  each of which takes the cache seconds to fetch, and half a second in,
  a read of an object that no origin holds. The cache must shed some:
  429, with `Retry-After: 2`, and a JSON body whose `error` is
  `origin_unresponsive`, `origin_slow`, `cache_overloaded`, or
  `too_many_requests` (`handleError` in
  `local_cache/persistent_cache_api.go`). Every other read must come
  back whole. The missing object must be 404: a 429 would have a client
  come back for ever for what does not exist. Where there is one origin,
  the director sends the cache to it without asking whether it holds
  the object (`Director.AssumePresenceAtSingleOrigin`), and the cache
  then asks its scheduler for a fetch, which it may shed before the
  origin says 404, so the row may fail there.
- `client-throttled`: at each V2 cache, a cold read of a 4 MiB object
  holds the origin's one slot, and another its one place in the queue.
  Meanwhile, `pelican object get --cache` and `stash_plugin` (with
  `Client.PreferredCaches`) each download a cold object through that
  cache alone. Each must fail as shed: error 6008, 6009, or 6010
  (`Transfer.OriginUnresponsive`, `OriginSlow`, or `CacheOverloaded`,
  `docs/error_codes.yaml`), exit 11, which says the failure may be
  retried (`cmd/object_get.go`, `cmd/plugin.go`), and nothing left in
  its directory. The plugin's ad must say `TransferRetryable = true`,
  and its error's `DeveloperData` must carry `RetryAfterSeconds = 2` and
  `ThrottleReason`, the 429's reason. The client does not wait on
  `Retry-After`, and has no other cache to try, so the test, as a
  retrier outside it would, tries it again once the reads are done, 2
  seconds apart, up to five times, and it must then get the object. A
  client that was never shed leaves its row inconclusive. `pelican
  object get` first asks for one byte (`Range: bytes=0-0`), which a V2
  cache serves by fetching only the blocks it reads, and those after it
  has answered; so whether it sheds that request with a 429 is less
  plain from the code than for the plugin's whole read.
- `timeout-header`: a client says with `X-Pelican-Timeout` how long it
  will wait, and a V2 cache waits for a cold object's headers that long
  less a second, or half of it if under a second, and then answers 504,
  while the fetch goes on (`serveObject` in
  `local_cache/persistent_cache_api.go`). At each V2 cache: `short`, a
  cold read of a 2 MiB object with `20ms` must get 504 within 3 seconds;
  `queued`, 2 seconds later, while that fetch holds the origin's slot,
  a cold read of another with `2s`, which must wait behind it, must get
  504 within 3 seconds (if the cache sheds it instead, or serves it at
  once, the row is inconclusive); and then the cache must come to hold
  each object, as `only-if-cached` shows, and serve it. `held`: an
  object the cache holds, with `5s`, must come back. `malformed`: a cold
  read with `soon`, no duration, must come back, the value ignored.
- `streaming`: at each V2 cache, a cold read of a 2 MiB object, which
  takes about 7 seconds to come: its first 4 KiB must come within 5
  seconds, and in less than half the time of the whole, which must be
  the object. A cache that sent nothing until it held the whole object
  would send its first bytes with its last. A whole read in under about
  3.5 seconds shows nothing, since the origins were not slow, and
  leaves the row inconclusive.
- `slow-transfer`: the clients give up on a transfer that stays slower
  than `Client.MinimumDownloadSpeed` for `Client.SlowTransferWindow`,
  once `Client.SlowTransferRampupTime` has passed, with error 6002
  (`Transfer.SlowTransfer`; `downloadHTTP` in `client/handle_http.go`).
  With `PELICAN_CLIENT_MINIMUMDOWNLOADSPEED=1048576`,
  `PELICAN_CLIENT_SLOWTRANSFERRAMPUPTIME=2s`, and
  `PELICAN_CLIENT_SLOWTRANSFERWINDOW=5s`, four times the origins' rate,
  a 4 MiB object, which would take about 15 seconds: `direct`, `pelican
  object get --direct` must fail with 6002, exit 11, and leave nothing;
  `plugin-direct`, `stash_plugin` with `?directread` likewise, its ad
  saying `TransferSuccess = false`, `TransferRetryable = true`, and an
  error of `ErrorType = "Transfer"` whose `PelicanErrorCode` is 6002.
  Both skip where the origins take no direct clients. `default`: with
  the default thresholds (100 KiB a second, after 100 seconds, for 30),
  each client must get another such object, direct, or where the
  origins take no direct clients, through the first V2 cache. `cache`:
  `pelican object get --cache` through each V2 cache, cold, with the
  thresholds, must fail as `direct` does.


## Exports and copies

Each export of an origin has capabilities of its own (`Origin.Exports`):
`Listings`, `DirectReads`, and `Copies`, which follows `Writes` unless
`Origin.DisableCopies` turns it off (`server_utils/origin.go` in
Pelican). An origin must enforce each export's own, and not only the
switches that apply to every export. The `exports` suite checks this at
each native origin, along with a third-party COPY, which a native origin
serves itself (`origin_serve/tpc_handler.go`), and a PUT of a multipart
body. Its objects go under `/<namespace>/data/exports/<run>/`, which it
removes afterward. A COPY's failing sources are a stub HTTP server in
the dev container (`framework/testlib/stub.py`), which the origins reach
at `http://dev:<port>/`. A scenario with several cases records a row per
case, named `<scenario> <case>`.

No shape gives an export without `Listings`, or one without
`DirectReads` beside others with them, or turns copies off. So, for
`listings-capability`, `direct-reads-capability`, and `disable-copies
disabled`, the suite restarts every origin with `fed.sh`'s
`Origin.Exports` restated, as the last of its configuration layers
([service control](../README.md#service-control)): `/protected-b` (or,
where it is not exported, `/protected-a`) without `Listings`; `/public`,
if it is exported, without `DirectReads`; and `Origin.DisableCopies` on.
Afterward it restores them. Before those rows, and after, every origin
must serve a probe object, every director name every origin for it,
and every federated cache serve it, within 150 seconds. The restart
needs native origins that take direct clients, since Pelican refuses
`DirectReads` and `Listings` with `Origin.DisableDirectClients`
(`-p origin-no-direct`); a control helper; and a topology other than
`tiny`, whose origin is the whole federation's process. Elsewhere, those
rows skip, and say why.

An `https` or `httpsv2` origin may have to present a token to its
backend (`Origin.HttpAuthTokenFile`, a static one on every request;
or, for `httpsv2`, `Origin.HttpAuthTokenPassthrough`, the client's own),
and the `webdav` service asks for none, so nothing else would show
what an origin sends. The `webdav-gate` service, nginx in front of it,
relays every request and logs the `Authorization` it came with, in
`framework/var/data/webdav-gate/access.log`, and checks nothing: the
log is the evidence. `backend-token` and `backend-passthrough` restart
every origin to reach its backend through the gate
(`Origin.HttpServiceUrl`), so configured, in the tests' own layer, and
restore them afterward; the static token is one `fed.sh init` made, in
`framework/var/generated/http-auth-token`. They need `/protected-a` to
take writes and direct clients, and a control helper; elsewhere they
skip.

- `multipart-put`: with metadata off, a PUT typed `multipart/form-data;
  boundary=...` must be stored as sent, as a PUT stores its body (RFC
  9110, section 9.3.4). At each origin that takes direct writes, a PUT
  of a well-formed multipart document, then a GET of it, must get it
  byte for byte, as must the store: `pelican-shaped`, a `metadata` part
  and then an `object` part, as Pelican's metadata uploads are; and
  `form-shaped`, one `file` part, as an HTML form sends. A native
  origin takes every such PUT apart (`rewriteMultipartPUT` in
  `origin_serve/metadata_multipart.go`), gated by
  `Origin.Metadata.AllowMultipart`, which is on by default, and not by
  `Origin.Metadata.Enabled`: it stores only the first's `object` part,
  and refuses the second (400), so both rows fail there, on main and
  v26.0.0-rc.3 alike. An XRootD origin stores both as sent, and is the
  control. Under `-p origin-metadata`, the parts are taken apart by
  design, and the rows skip.
- `tpc-body`: at each native origin, a third-party COPY (WLCG HTTP-TPC,
  pull mode) of an 8 MiB object in `/protected-a`, whose `Source` is
  the first federated cache (or, without one, another origin, or the
  origin itself), with the `server` token for the destination, and for
  the source in `TransferHeaderAuthorization`, which the origin sends
  the source as `Authorization`. It must get 201, and an answer of
  Perf Markers, each ending `End`, whose `Stripe Bytes Transferred`
  never falls nor exceeds the object, the last of them the object's
  size, then a last line of `success: Created` (Pelican's
  `TestMonitorTPC`). The destination must then hold the object. The
  row's note counts the markers.
- `tpc-failures`, at each native origin, a COPY to a path of its own:
  `bad-source`, with a `Source` that is no http or https URL (`ftp:`,
  and `file:///etc/passwd`), must get 400; `source-error`, from the stub
  answering 404, and 500, must get 502; and `missing-source`, from the
  cache (as `tpc-body`) of an object no origin holds, must fail, by an
  error status or a last line of `failure: ...`. None may write the
  destination; and `pelican object copy` of a missing object must exit
  non-zero, and leave no origin holding its destination. Where an
  origin never asked the stub, a 502 shows nothing, and the row is
  inconclusive. `mid-stream`: over an object X, a COPY from the stub
  claiming a `Content-Length` of 4 MiB and sending 1 MiB must fail, and
  then, where uploads are staged (POSC, `-p origin-posc`, or a `pstore`
  origin), leave X as it was. Elsewhere, the origin opens its
  destination truncated, and so has lost X before it learns that the
  copy fails; X intact or gone is accepted there, never other bytes,
  and the row's note says which. Pelican aborts a staged upload, which
  keeps X, but then removes the destination anyway (`RemoveAll` in
  `handleCopyTPC`), so the row fails under `-p origin-posc` and
  `-p origin-pstore`, on main and v26.0.0-rc.3 alike. Under
  `-p origin-metadata`, an origin that kept X must publish no
  `object.deleted` for it within 15 seconds.
- `disable-copies`: `public`, in the shape as it is, a COPY to
  `/public`, which takes no writes, and so has no `Copies`, from the
  probe at the origin itself, must be refused (404 or 405, as a native
  origin routes no COPY there, or 401 or 403), and write nothing. Every
  origin is asked, an XRootD one too; where `/public` takes writes
  (`-p origin-public-writes`), and so copies, the row skips. `disabled`,
  under
  `Origin.DisableCopies`: at each native origin, a COPY to
  `/protected-a` must get 404 or 405, since no export then has
  `Copies`, and a native origin routes COPY only for an export with it
  (`RegisterHandlers` in `origin_serve/handlers.go`), and write nothing;
  and OPTIONS of the server's root, with no token, must answer 200,
  with an `Allow` that names no COPY (`registerRootOptions`; Pelican's
  `TestRootOptionsAdvertisesCopyOnlyWhenEnabled`).
- `listings-capability`: of the export without `Listings`, with its
  `server` token, at each native origin, a PROPFIND of Depth 1 of a
  collection, and a GET of it with `Accept: text/html`, must be refused
  (401, 403, or 405); a PROPFIND of Depth 0 of the object in it, a
  stat, must answer 207, with its size. Where that export is not
  `/protected-a`, a PROPFIND of Depth 1 in `/protected-a`, which keeps
  `Listings`, must answer 207, so that the refusal is the export's
  alone. The directors already send no deeper
  PROPFIND to an export without `Listings` (`director/sort.go`), which
  the `listings` suite checks; this is the origin's own check. Nothing
  in a native origin reads `Listings` (its ACLs come from
  `PublicReads`, `Reads`, and `Writes` alone, in
  `origin_serve/authz.go`), so the row fails there, on main and
  v26.0.0-rc.3 alike.
- `direct-reads-capability`: with `/public` without `DirectReads`, while
  `/protected-a` keeps them, a GET of an object in `/public` straight to
  each native origin must be refused (401 or 403), with none of its
  bytes, while one of the probe in `/protected-a` is served; and a GET
  of it through each federated cache must be served. A native origin
  checks `DirectReads` only through `Origin.DisableDirectClients`, for
  every export at once (`origin_serve/handlers.go`), so the row fails
  there, on main and v26.0.0-rc.3 alike. It skips where `/public` is not
  exported.
- `backend-token`: with the origins reaching their backend through the
  gate, with the static token, everything must still work, straight at
  each origin with the `server` token: a PUT of an object with a
  `Content-Length` and one in chunks, which the store on disk, which
  the backend serves, must then hold; a GET of the first whole, of a
  range of it, and a HEAD; a PROPFIND of Depth 1 of the run's
  collection, which must list both; and at a native origin, an MKCOL, a
  MOVE of the second object, which the store must show, and a DELETE of
  what it moved. Then, through the federation, `pelican object get` of
  the first object, which a cache, or without one an origin, fetches
  from an origin, and so through the gate; and `pelican object put` of
  a new object. Every request the gate logged meanwhile, the origins'
  own probes of the backend included, must have carried the static
  token, and none a client's, which a backend has no business seeing;
  and the gate must have seen a GET, a PUT, and a DELETE. It runs at
  `https` and `httpsv2` origins.
- `backend-passthrough`: the same with the client's token forwarded
  (`Origin.HttpAuthTokenPassthrough`), at `httpsv2` origins alone, since
  XRootD's HTTPS plugin forwards none. Each request the gate logged for
  a path the test asked an origin for straight must have carried the
  client's token, and none the static one; the rest, the origins' own
  probes, which carry no client's token, and what they fetched for a
  cache, are counted in the row's note.


## pstore

A pstore origin (`-p origin-pstore`) keeps its objects encrypted, in
block files and in a catalog that only the running origin can read
(`pstore/` and `origin_serve/backend_pstore.go` in Pelican). Besides
serving objects, which `blocks` and `posc` check, it has an
administrative storage API and CLI, verification passes that run on a
schedule, a size bound past which it refuses writes, and backups of its
catalog. The `pstore` suite checks them. Its objects go under
`/<namespace>/data/pstore/<run>/`, in the first of `/protected-a` and
`/protected-b` that takes writes, at every pstore origin, and it
removes them afterward. At another native origin, only `storage-api`'s
`absent` row runs, and the suite skips XRootD origins. A scenario with
several cases records a row per case, named `<scenario> <case>`, or
`<scenario> <origin> <case>` where it asks each origin.

- `storage-api`: each pstore origin serves, on its web port, `GET
  /api/v1.0/origin/storage/pstore/ls/<path>`, `stat/<path>`,
  `digest/<path>`, and `usage`, for its admin alone
  (`origin_serve/storage_api.go`). A path is in the store's catalog,
  under the export's `StoragePrefix` (`/protected-a/...`). At each
  pstore origin, the suite puts a collection of seven objects, of 100 to
  18,100 bytes, some held in the catalog and some in block files, and a
  collection `sub`, which holds one.
  - `authz`: each route, with no token, must get 401; with the
    namespace's `server` token, which is not of the origin's making for
    its web API, 401; with the web token of an ordinary user,
    `test-pstore` (`credentials.mint_user()`), 403; and with the
    admin's, 200. The routes take two handlers, one to say who the
    caller is, and one whether they are the admin; registered alone,
    the second would refuse everyone with 401.
  - `ls`: of the collection, whole, its eight entries in name order,
    each with its path, size, and kind, and `more` false. Three at a
    time (`limit=3`), each page continued with `after` set to the last
    name before it: pages of three, three, and two entries, `more` on
    all but the last, together the whole listing, with none twice. `ls`
    of an object must get 400, and of nothing, 404.
  - `stat`: of an object in a block file, and of one in the catalog,
    its name, path, size, and ETag, each as a HEAD at the data port
    says them; of `sub`, that it is a collection, with no ETag; and of
    nothing, 404.
  - `digest`: of an object, the RFC 3230 checksums that the store
    recorded as it wrote it, md5, sha, and crc32c
    (`pstore/checksum.go`), each the object's, and each what a HEAD
    with `Want-Digest` of it alone gets at the data port; of a
    collection, 400.
  - `usage`: `backend` `pstore`, at least the collection's bytes used,
    and a capacity of 0, since the shape bounds no store. Within 45
    seconds, `pelican_pstore_used_bytes` and
    `pelican_pstore_limit_bytes` must say the same: the janitor
    republishes them every 30 seconds.
  - `absent`: at a native origin whose store is not a pstore, every
    route, with no token or with the admin's, must be 404, since the
    origin registers none.
- `introspect-cli`: `pelican-server origin introspect` (the client
  lacks it; see [client binary](../README.md#client-binary)) asks the
  first pstore origin's storage API, with `--server` and `--json`.
  `token`: with the admin's token in a file (`--token`), `usage`, and
  `ls`, `stat`, and `digest` of `storage-api`'s collection, must print
  what the API answers. `missing`: with a token file that does not
  exist, it must fail, and say that the file was not found. `minted`:
  with no file, it mints an admin token with its host's issuer key, for
  the issuer that the origin's web API takes. Given a copy of the
  origin's key (`PELICAN_ISSUERKEYSDIRECTORY`; a copy, since Pelican
  makes a key in a key directory that has none) and the origin's web
  URL (`PELICAN_SERVER_EXTERNALWEBURL`), `usage` must print what the
  API answers. The scenario skips without a `pelican-server`.
- `scheduled-passes`: a pstore origin checks its catalog
  (`Origin.PStoreIndexCheckInterval`), and reads every object back
  against its checksums (`Origin.PStoreDataScanInterval`), on a
  schedule: here every minute, the first a minute after it starts
  (`config.d/origin/pstore/50-origin.yaml`). Its janitor frees, every
  30 seconds, the versions that an overwrite or a delete replaced. At
  each pstore origin, once each pass has succeeded, within 150 seconds,
  the suite puts two objects of 100,000 bytes, then overwrites one and
  deletes the other. Within 150 seconds,
  `pelican_pstore_reclaimed_objects_total` must rise by 2, and
  `pelican_pstore_reclaimed_bytes_total` by their sizes; and each pass
  must begin again, and succeed. No pass may have failed
  (`pelican_pstore_scheduled_pass_failures_total`); the index check
  must find nothing (`pelican_pstore_fsck_findings`); and the data scan
  must have verified objects, and found none changed
  (`pelican_pstore_data_scan_mismatches_total`). The index check's
  `pending_instances`, writes under way or abandoned less than 10
  minutes ago, which only the janitor reclaims, are reported but not
  failed (`pstore/fsck.go`): a copy that failed in the `exports` suite
  may leave some.

No shape bounds a store, or backs one up. So for `capacity`, `backups`,
and `restore`, the suite restarts the first origin, once, as the last of
its configuration layers ([service control](../README.md#service-control)).
Its one storage directory, `/data/pstore`, is bounded at what the
storage API says it holds and 8 MiB more (`Origin.PStoreStorageDirs`,
with a `MaxSize`). It backs up its catalog every 20 seconds, keeping 3
(`Origin.PStoreMetadataBackupLocation`, `PStoreMetadataBackupInterval`,
and `PStoreMetadataBackupsToKeep`), to a directory of the run's own in
its state, `framework/var/state/<service>/pstore-backups/<run>/`, which
the suite removes afterward. The suite then restores the origin, and
waits, as `freshness` does, until every director names every origin and
every cache answers. The restart needs a control helper, and skips the
`tiny` topology, whose origin is the whole federation's process;
otherwise the three scenarios skip, and say why. `scheduled-passes` runs
before it, since a restart starts the passes over.

- `capacity`: straight at the origin, in order. A pstore never evicts:
  past its bound, a write gets 507 until something is deleted
  (`pstore/capacity.go`). Each refusal must count once in
  `pelican_pstore_write_failures_total{reason="no_space"}`.
  - `fill`: objects of up to 960 KiB, each written whole, with its
    length, below pstore's 1 MiB spill threshold, so that each takes
    its bytes and a tag per block, until 512 KiB are left. The store's
    usage must have grown by what they take, less what the janitor
    freed meanwhile.
  - `sized`: a PUT's headers, with a `Content-Length` 256 KiB more than
    is left, and no body: the origin must answer 507, and nothing else,
    before any of the body comes. It checks a declared length against
    the store before it reads (`checkBackendCapacity` in
    `origin_serve/handlers.go`).
  - `expect`: the same, with `Expect: 100-continue`: 507, with no `100
    Continue` first. Python's `http.client` passes over a `100
    Continue`, and sends a body whole, so these two send a request of
    their own (`web.statuses_before_body()`).
  - `chunked`: a PUT in chunks, with no length, of 128 KiB more than is
    left, which the origin can refuse only once it has read what is
    left, must get 507, not the WebDAV handler's 405 nor a 500
    (`statusForBackendError` in `origin_serve/error_handling.go`). It
    must leave nothing behind: a HEAD of the object and its `stat` must
    get 404, and the store's usage must not have grown. What the origin
    leaves unread is less than the 256 KiB that Go's server reads and
    discards before it answers, so that it need not hang up on a
    client still sending.
  - `freed`: after a DELETE of a filler, the store must have room for
    `sized`'s object within 90 seconds, once the janitor frees the
    filler; `sized`'s PUT, with its body, must then succeed, read back
    whole, and count no refusal.
  - `gauges`: `pelican_pstore_limit_bytes` must be the bound, and with
    `pelican_pstore_used_bytes`, within 45 seconds, say what the storage
    API's `usage` says.
- `backups`: the origin backs up its catalog at startup, then an
  interval after each backup, to `pstore-metadata-<UTC>.pmb`, through a
  `.partial` file that it renames, and prunes all but the newest it
  keeps (`snapshotOnce` in `pstore/backup.go`).
  - `startup`: within 60 seconds of the restart, a backup must appear,
    written within an interval of when the origin started
    (`process_start_time_seconds`), with its pass counted
    (`pelican_pstore_scheduled_passes_total{pass="metadata_backup"}`).
  - `schedule`: once four backup passes have begun, the backups must be
    20 seconds apart, give or take 10 (the interval runs from the end of
    one to the start of the next), and none of the passes may have
    failed.
  - `files`: the directory must then hold three backups, the startup
    one pruned, each named for when it was written and beginning with
    the format's magic and version, `PELICAN-PSTORE-BACKUP\0\1`
    (`pstore/backup_crypto.go`), and no `.partial` file, nor anything
    else; and `pelican_pstore_metadata_backup_last_size_bytes` must be
    the newest one's size.
- `restore`, with the origin still backing up its catalog so: a backup
  is only as good as what comes back out of it, and nothing but the
  origin can read the store, so the scenario checks a restore through
  the offline CLI, which reads the store on its own
  (`pelican-server origin pstore`; `pstore/backup.go` and `recover.go`).
  - `objects`: five objects of 100 bytes to 1 MiB and 5, held in the
    catalog and in block files, written straight to the origin; the
    first backup taken after them, copied out of its directory; and
    then one more object, written after that backup, which the origin
    must serve.
  - `cli`: with the origin stopped, the store's catalog (`db/`) is set
    aside, leaving its block files (`objects/`) and master key
    (`masterkey.json`), as a recovery is laid out, and
    `metadata-restore` must restore the copied backup into it, opened
    with a copy of the origin's issuer keys, to which the catalog's
    master key and every backup are sealed.
  - `export`: `export --verify` of the scenario's collection, from the
    restored store, must write out every object the backup held, byte
    for byte, each verified against the checksums the catalog records,
    and not the object written after the backup.
  - `served`: the origin, started again on the restored catalog, given
    back to whoever owns the master key, must serve the five objects,
    byte for byte, and answer 404 for the one written after the backup.
    If it does not, the catalog set aside is put back, so that the rest
    of the run finds the store as it was.

  The scenario needs the `pelican-server` that has the CLI, and skips
  without it; the block files that the late object left behind are the
  janitor's to reclaim. It runs between `backups`'s `startup` and its
  later cases, which wait for the schedule to run on past the stop and
  the start, and before `capacity`, which would leave the store too
  full for its objects.


## Routing

A director decides which servers a client is sent to, in what order,
and what the redirect carries (`director/director.go`, `sort.go`, and
`stat.go` in Pelican). `federation` checks whom each director names;
the `routing` suite checks the rest: the order, the redirect itself, and
what a director refuses. It skips a standalone origin, which has no
director. It asks about objects it puts at every origin, in the first
protected namespace, under `data/routing/<run>/`, and removes them
afterward: `probe`; `warm`, which only `random-and-adaptive` reads; and
`missing`, which it never puts. Each case is a row, named `<scenario>
<case>`, and covers every director.

Each server's own layer (`framework/config.d/instance/`) declares where
it is (`GeoLocation`): cache-0 and origin-0 in Madison, and cache-1 and
origin-1 in Lisbon. The framework has no GeoIP database, so a client
that says nothing of where it is is placed at random in the contiguous
US, and sent to the Madison servers first, as their names would order
them anyway.

- `distance`: the default sort, by distance from the client
  (`Director.CacheSortMethod` `distance`). A client may say where it is
  (`X-Pelican-Coordinate: lat=..., long=...`), which wins over GeoIP.
  - `caches`: with several caches, a GET of the object route for a
    client near Madrid must send it to cache-1, and its `Link` list
    cache-1 first; for one near Madison, cache-0.
  - `caches-debug`: with `X-Pelican-Debug: true`, the director must
    answer with the same redirect and, as its body, the decision as
    JSON: `directorSortMethod` `distance`; the client's coordinate as it
    declared it, with `Source` `declared`; and for each cache, its
    declared coordinate, with `Source` `declared`, and its
    `distanceWeight`, which must be what the director's formula gives:
    1 at the client, halving every 200 miles of great-circle distance.
  - `origins` and `origins-debug`: the same for the origin route, with
    several origins, between origin-0 and origin-1.
  - `malformed`: an `X-Pelican-Coordinate` whose latitude is no number,
    or out of range, must be ignored: the client is still sent to a
    cache, and the debug body's client coordinate is not `declared`.
- `stat`: an object that no origin holds, asked for of the origin
  route, by GET and HEAD, in each exported namespace. The director asks
  the origins first (`shouldStatOrigins` in `director/stat.go`), when
  it can and should.
  - `several`: with several origins, and the namespace's token, it asks
    them, and must answer 404, with a body saying that no source
    reported possession of the object. Where the namespace takes no
    direct clients, the origins refuse its question, which carries no
    federation token, and it must take a refusal for presence, since it
    cannot tell: there a redirect to an origin passes too.
  - `skipstat`: with `?skipstat`, it must not ask, and must redirect
    (307) to an origin.
  - `no-token`: in a protected namespace, with no token, it can't ask,
    and must redirect.
  - `single`: with one origin, it assumes the object is there
    (`Director.AssumePresenceAtSingleOrigin`, on by default), and must
    redirect.
  - `single-checked`: with one origin and that setting off (a restart;
    below), it must ask, and answer 404.
- `redirect`: how a director builds a redirect of `probe`, at its
  object route and at the bare path (`generateRedirectResponse`).
  - `header-token`: the token in `Authorization` must reach `Location`
    as `authz`, without `Bearer `. Following `Location` alone, with no
    `Authorization`, as curl does across hosts, must serve the object
    where the namespace admits a client at the server it lands at, and
    otherwise be refused. No URL in `Link` may carry the token, which
    would make the header long; its `pri` must run 1 to N, in order,
    and its `depth` be the number of path segments below the namespace
    (4).
  - `query-token` and `query-bearer`: the same, with the token in
    `?authz=`, and with it as `?authz=Bearer <token>`, as XRootD sends
    it, from which the director must strip `Bearer `.
  - `timeout`: `X-Pelican-Timeout: 5s` must reach `Location` as
    `pelican.timeout=5s`.
  - `params`: `?skipstat`, and a parameter the director doesn't know,
    whose value a URL must escape (`a b&c=d/é?#%`), must reach
    `Location`, escaped; and `?directread`, where the namespace takes
    direct clients.
  - `job-id`: an `X-Pelican-JobId` that is a UUID must come back in the
    answer's; for one that isn't, the director must send a UUID of its
    own.
  - `no-redirect`: `?redirect=false` must get 200, with no `Location`,
    but the same `X-Pelican-Namespace`, and a `Link` of the same servers.
- `expired-401`: a director refuses a token that will expire before the
  client could follow the redirect, with 401 and `WWW-Authenticate:
  Bearer error="invalid_token", error_description="token has expired"`,
  so that a client such as rclone fetches a new one
  (`validateClientToken`). It reads the token's times without checking
  its signature, and allows 10 seconds of grace, or half the token's
  lifetime if less. At the object route, the origin route, and the bare
  path, these must get 401: a token that expired five minutes ago
  (`expired`), and the same in `?authz=` (`in-query`); one issued an
  hour ago that expires in 5 seconds (`expiring`); a 10-second one
  issued 6 seconds ago (`half-life`); and one whose `exp` is its `iat`
  (`no-lifetime`). These must be redirected: a 10-second one just
  issued (`short-fresh`), whose grace is 5 seconds; an opaque token
  (`opaque`); and a JWT with no `exp` (`no-exp`). The suite signs each
  with the origins' key (`jwt.encode()`).
- `status-codes`: what a director refuses, and with what.
  - `unknown-namespace`: an object in a namespace that no origin
    exports must get 404, but within 5 minutes of the director's start,
    429, since it may not yet have heard from every origin
    (`inStartupSequence`). The suite reads when it started from
    `process_start_time_seconds` on its `/metrics`, and within 10
    seconds of the boundary takes either.
  - `writes`: a bare-path PUT and DELETE in `/public`, which no origin
    takes writes in, must get 405, since the director finds the
    namespace's origins, and none that supports the method
    (`noOriginsForReqErr`); `federation`'s `redirects` takes any refusal.
  - `bad-requests`: `?directread&prefercached`, which conflict, must get
    400, and so must a client older than 7.0.0 (`User-Agent:
    pelican-client/6.9.0`), while a 7.0.0 client is redirected.
  - `options`: a bare OPTIONS must get 200, with `Allow` and CORS
    headers.
  - `server` and `server-once`: every answer of the scenario must carry
    `Server: pelican/<version>`, and in one field, since `Server` is no
    list (RFC 9110, section 5.3). The director's API routes add it twice,
    once in each of the two router groups they are in
    (`launchers/director_serve.go` and `RegisterDirectorAPI`), so
    `server-once` fails there.

The other scenarios need a director configured otherwise. Under
`-p topo-multi-cache`, with a control helper, the suite restarts every
director four times, as the last of its configuration layers
([service control](../README.md#service-control)), and after each
waits up to 150 seconds until every director names every server again;
afterward, it restores them, and waits again. Elsewhere these rows
skip, and say why, so that other shapes are not slowed.

- `random-and-adaptive`:
  - `random`: with `Director.CacheSortMethod: random`, each director,
    asked 20 times, must put each cache first at least once.
  - `adaptive-presence`: with `adaptive`, after `warm` is read through
    cache-1 alone, and cache-1 gives it an `Age` of a second or more,
    each director, asked with the token, with which it asks the caches
    whether they hold it, must explain (`X-Pelican-Debug`) an
    `availabilityWeight` of 2 for cache-1 and 0.5 for cache-0
    (`availabilityWeightFn`). A director takes a cache's copy by its
    `Age`, which only a V2 cache is known to send, so the row skips XRootD
    caches. The order is drawn at random by the weights, so it is not
    checked.
  - `adaptive-no-token`: asked with no token, with which it can't ask the
    caches of a protected namespace, it must weigh them alike.
  - `adaptive-unchecked`: with `Director.CheckCachePresence: false`, the
    same, though asked with the token.
- The last restart sets `Director.FilteredServers` to cache-1's name,
  `Director.AssumePresenceAtSingleOrigin: false` (for `stat`'s
  `single-checked`), and response hostnames that no DNS knows, which
  the suite sends only in headers:
  - `filtered`: `routing`: no director may name cache-1, at the object
    route or the bare path, asked five times each, and each must still
    name the other caches. `listing`: each director's
    `/api/v1.0/director_ui/servers` must list cache-1 with `filtered`
    true and `filteredType` `permFiltered`.
  - `response-hostnames`: with `Director.OriginResponseHostnames` and
    `CacheResponseHostnames` each naming a host, a bare-path GET asked
    of the first must go to an origin, and of the second to a cache,
    whether the name is in `Host` (`host-origin` and `host-cache`) or in
    `X-Forwarded-Host` (`forwarded-origin` and `forwarded-cache`). The
    director looks for `Host` among the request's headers
    (`ShortcutMiddleware`), but Go's server moves it out of them, into
    the request's `Host`, so it never finds it: the `host-` row for the
    kind of server that the director sends a bare path to by default
    passes only for that reason, and the other fails, on v26.0.0-rc.3
    and main alike.
- `permitted-namespaces`: a cache may be restricted to namespaces
  (`Cache.PermittedNamespaces`), which it then alone advertises to the
  directors (`filterAdsBasedOnNamespace` in `cache/advertise.go`), so
  that they send it clients for those alone. The suite restarts the
  last cache with the setting naming `/public` (or, where `/public` is
  not exported, another protected namespace), as the last of its
  configuration layers, and waits up to 150 seconds until the directors
  route as it says; afterward it restores the cache, and waits again.
  `routing`: each director, asked five times at the object route, must
  name the cache for an object in the permitted namespace, and not for
  the probe in the suite's own, for which it must still name every
  other cache. `listing`: each director's
  `/api/v1.0/director_ui/servers` must list the cache with
  `namespacePrefixes` of the permitted namespace, and any under it, and
  no other. `direct`: asked straight, the cache must serve the object
  in the permitted namespace; what it does with the probe in the
  suite's own is not settled, since the setting narrows what the cache
  advertises, so the row's note says, but it must never serve other
  bytes. It needs several caches and a control helper, as the other
  restarts do, and a shape that exports two namespaces.


## Director APIs

A director publishes what it knows of the federation through read-only
APIs: the lists of servers and namespaces that its web UI and the
caches read, its health tests of each server, its metrics, and its
comparison of the federation's metadata. The `director-api` suite
checks that each director's are true of the shape. It skips a
standalone origin, which has no director. Each case is a row, named
`<scenario> <case>`, and covers every director.

- `servers`: `/api/v1.0/director_ui/servers` (`listServers` in
  `director/director_ui.go`).
  - `listing`: every origin, another owner's included, and every
    federated cache, once, and nothing else; a site-local cache is not
    listed. Each with its web URL; its data URL on its host; its
    registry prefix, `/origins/<host>:<port>` of its web URL for an
    origin (`GetOriginNs`) and `/caches/<name>` for a cache; its
    storage, the origins' variant, and `posix` for a cache, whose ad
    names none; the shape's version; unfiltered; and its director test
    disabled only at an origin whose storage can't take it. An origin
    with one export takes that export's capabilities as its own, and
    with several, direct reads, by default: so under
    `-p origin-no-direct`, it must not offer direct reads.
  - `by-type`: with `?server_type=origin`, the origins alone; with
    `cache`, the caches alone; with another type, 400.
  - `by-type-case`: with `?server_type=Origin` and `Cache`, as Pelican
    spells the types, the same lists, since `listServers`
    (`director/director_ui.go`) takes the type without regard to case.
    It then compares it in lower case exactly, so `Origin` and `Cache`
    list nothing, and the row fails, on v26.0.0-rc.3 and main alike.
  - `one`: `/servers/<name>` of each server, by its name, and
    `/servers/<name>/namespaces`: for an origin, its exports; for a
    name no server has, 404.
  - `ui-namespaces`: `/api/v1.0/director_ui/namespaces` must list
    each export, with the names of the origins that export it.
  - `contact`: `/api/v1.0/director_ui/contact` must give the support
    contact that `framework/config.d/director/default/` configures
    (`Director.SupportContactEmail` and `SupportContactUrl`).
  - `directors`: `/api/v1.0/director/directors` must list every
    director, by the URL it takes ads at.
- `namespaces`:
  - `v2`: `/api/v2.0/director/listNamespaces` must list each export,
    once per origin that exports it, with its capabilities and its
    issuer, none for one without (`has_issuer`), and
    `/pelican/monitoring`, which anyone may read, and nothing else. A
    cache learns a namespace's issuers here, so a wrong one breaks
    every protected read through it.
  - `v1`: `/api/v1.0/director/listNamespaces`, which serves no data
    since the first version of a namespace's ad went away, must answer
    299, with a `Warning`, and a JSON object, not a list.
  - `prefix`: `/api/v1.0/director/namespaces/prefix/<path>` must give
    the namespace a path falls in: `/<ns>/data/x`'s is `/<ns>`; a path
    in no namespace gets 404; and under `-p topo-multi-owner`,
    `/public/other/x`'s is `/public/other`, but `/public/others-x`'s is
    `/public`, since a prefix matches whole segments of a path.
- `health`: a director tests each origin and cache it hears from every
  15 seconds (`Director.OriginCacheHealthTestInterval`): at an origin,
  it puts a file under `/pelican/monitoring/directorTest/`, reads it,
  and removes it; at a cache, it reads one, which the cache gets from
  the director itself. It reports each result to the server
  (`director/monitor.go` and `cache_monitor.go`). It does not test an
  origin whose storage can't take the file: only `posix`, `posixv2`,
  and `pstore` can (`SupportsSelfTest`), so not `s3v2`, `httpsv2`,
  `ssh`, `https`, or `s3`.
  - `status`: within 150 seconds, each director must list each server
    it tests with `healthStatus` `OK`, and each it doesn't with
    `Health Test Disabled`. The wait ends sooner once no test is still
    to run, and a failed one has had 35 seconds to pass.
  - `test-runs`: in 35 seconds or more, each director's
    `pelican_director_total_ftx_test_runs` for each server it tests
    must rise by a run that passed and was reported, and for each it
    doesn't, by none.
  - `component`: each origin and federated cache must say, by its own
    `pelican_component_health_status`, component `director`, that the
    tests pass (ok); one that the directors don't test says ok too.
  - `cache-file`: a GET of a test file of the suite's own, by the name
    a director would give it, through each federated cache must get
    the director's test body.

  A native origin serves only its exports' prefixes
  (`RegisterHandlers` in `origin_serve/handlers.go`), and nothing in
  `origin_serve/` serves `/pelican/monitoring`, which an XRootD origin
  exports for these tests. So a director's tests of a `posixv2` or
  `pstore` origin should fail, and the `status`, `test-runs`, and
  `component` rows with them, on v26.0.0-rc.3 and main alike. This
  comes from reading the code, not from a run.
- `health-test-file`: the test file a director serves the caches
  (`/api/v1.0/director/healthTest/pelican/monitoring/directorTest/director-test-<RFC 3339 time>.txt`;
  `getHealthTestFile` in `director/director.go`). A GET must get the
  test body (`get`); a HEAD, its length (`head`); and a PROPFIND, 207,
  and a multistatus naming the file and its length (`propfind`). A
  name whose time is none must get 400 (`bad-timestamp`). In a
  directory named `a&b<`, a PROPFIND must name it, escaped
  (`escaped`). Pelican escapes the path, but then writes it as it was,
  so its XML is malformed, and the `escaped` row fails, on
  v26.0.0-rc.3 and main alike.
- `origin-stat`: which origins hold an object, as a director asks
  them (`/api/v1.0/director_ui/servers/origins/stat/<path>`;
  `queryOrigins` in `director/director_ui.go`). It takes the token it
  is shown to the origins, and a director's admin token is no storage
  token, so the suite asks of an object in `/public`, which it puts at
  one origin alone (on a `pstore` origin, in `/protected-a`'s storage,
  which `/public` reads), at `data/director-api/<run>/held`, and
  removes afterward. With no credentials, it must get 401
  (`no-auth`); as the director's admin, 200, naming that origin alone
  (`admin`; it skips where the shape exports no `/public`); with
  `min_responses` above `max_responses`, 400 (`bad-bounds`); and for a
  namespace no origin exports, 404, or 429 within 5 minutes of the
  director's start, as one JSON document (`unknown-namespace`).
  Pelican writes that answer and goes on, asks no origin, and writes
  another, so the body is two JSON documents, and the
  `unknown-namespace` row fails, on v26.0.0-rc.3 and main alike.
- `metrics`: what each director counts (`metrics/director.go`). Other
  traffic moves these counters too, so each row counts only what is the
  run's own, or what only rises.
  - `redirects`: 5 GETs of the object route, each from a client whose
    `User-Agent` names a version of the run's own
    (`pelican-client/7.99.<n>`), must raise
    `pelican_director_redirects_total` of that version, with status
    307, by 5, and `pelican_director_client_requests_total` of version
    `7.99` by 5.
  - `ads`: since the suite began, at least 40 seconds before,
    `pelican_director_advertisements_received_total` must have risen,
    with status 200, for every origin and federated cache. A director
    counts the ads a server sends it, but not those another director
    forwards (`serverAdMetricMiddleware`).
  - `server-count`: `pelican_director_server_count` must count each
    origin and federated cache once, of its type, and none from the
    OSDF topology, and as many of each type as the shape has.
- `metadata-hosting`: `framework/config.d/director/default/` turns
  `Director.EnableFederationMetadataHosting` off, since the discovery
  service serves the federation's metadata. So each director's
  `/.well-known/pelican-configuration` must get 410, but at the
  discovery host, 200 (`discovery`): in `tiny`, whose director it is,
  and under `-p director-discovery`, where `fed.sh` turns the setting
  on for director-0 alone, so that another director (`-p
  topo-multi-director`) still answers 410, unless the directors share
  one name (`-p director-ha`), at which any may answer, so every
  director then serves it. Its comparison of the
  metadata it was given with the discovery host's
  (`/api/v1.0/director_ui/federation/discrepancy`;
  `director/metadata_comparison.go`) must refuse anyone but its admin
  (401; `no-auth`), and tell the admin that it compared them, and
  found them to agree, with keys in common, or, where it is the
  discovery host, that it did not compare them (`discrepancy`). The
  `federation` suite's `discovery` scenario checks what the discovery
  host serves where it is a Pelican server: each field of the document
  as `fed.sh` has it, and a `jwks_uri` that serves the server's own
  keys.
- `forwarding`, under `-p topo-multi-director`: a director forwards
  each ad a server posts to it to every director it knows that has not
  yet seen it (`forwardServiceAd` in `director/director_advertise.go`).
  No configuration can keep a server's ads from any director, since a
  server advertises to every director it hears of: from its
  configuration, the discovery document, and each director's list of
  directors (`doDiscovery` in `server_utils/director_discovery.go`).
  So the suite posts the first federated cache's own ad, signed as the
  `advertise` suite signs it, to one director alone, just after the
  cache's own ad has reached the other, so that the cache's next is
  about 30 seconds away. If it comes sooner, and could explain what the
  other director shows, the row is inconclusive.
  - `forwarded`: the ad, posted to the first director, saying a status
    the cache doesn't (`warning`, or `degraded`), must reach the
    second within 10 seconds.
  - `shutdown-lifted`: the ad, posted to the second director, saying
    the cache is shutting down, after which it must filter the cache
    (`shutdownFiltered`); then its ad saying its own status, posted to
    the first, must lift that at the second within 10 seconds, as an
    ad posted to the second would. Only an ad posted to a director
    lifts it (`registerServerAd` in `director/director.go`), and not
    one forwarded (`finishRegisterServeAd`), so the row fails, on
    v26.0.0-rc.3 and main alike: a director that hears a server only
    by another's forwarding keeps it out of routing from its restart
    on.
  - `restored`: within 150 seconds, the cache's own ads must have
    restored it at both directors.

  A director that forwarded an ad back to the one it came from would
  forward it forever, but two directors can't be made to, since each
  skips the directors an ad has already seen, and its own. Nothing the
  suite can read shows a forwarded ad, so it can't see such a loop.


## Registry

The registry records which key owns which namespace, and serves those
keys to every director and cache: a flaw there lets one party claim
another's namespace. The `registry` suite tests it directly
(`registry/` in Pelican). It skips a standalone origin, which has no
registry. Each case is a row, named `<scenario> <case>`.

Every registration the suite makes has `registry-test-<run>` in its
prefix (e.g. `/registry-test-<run>-del`), and in its site name, and
every downtime it declares has it in its description. Before its
scenarios, and after them, the suite removes every registration,
server, and downtime with `registry-test-` in it, of any run, so that
a failed run leaves nothing for a later one, or a director, to trip
over: downtimes at each server, as its admin, and at the registry, as
its admin, or for a server's, as that server; then servers and
registrations, as the registry's admin. It never touches the shape's
own, but for `register again`, which must leave it as it was. It makes
keys of its own (EC P-256, with `openssl`), and signs its tokens for
the registry with them, or with a server's own key, through
`framework/testlib/jwt.py`.

- `register`: registering prefixes. `pelican namespace register`
  reports only an exit status, so most rows speak the registry's
  protocol themselves, as the client does
  (`framework/testlib/registry.py`; `keySignChallenge` in
  `registry/registry.go`): a nonce and the client's JWKS, then both
  nonces signed with the JWKS's first key. The registry must answer
  201 for a prefix it registers, and 200 for one already registered.
  Below, `<t>` is `registry-test-<run>`.
  - `invalid`: `/api/<t>`, `/view/<t>`, `/pelican/<t>`, `/<t>/./b`,
    `/<t>/../b`, `/.<t>`, and `/` must get 400 (`validatePrefix`, and
    Pelican's `TestValidatePrefix`).
  - `child-foreign`, `child-owner`, `sibling`: with key chaining on
    (`Registry.RequireKeyChaining`, by default), a prefix inside a
    registered one may be registered only with a key of the outer one
    (`validateKeyChaining`). `/protected-a/<t>` with a key no one has
    registered must get 403, and with the origins' own key, 201;
    `/protected-a-<t>` is no child of `/protected-a`, and must take
    any key. Under `-p topo-multi-owner`, chaining is off (`fed.sh`),
    so `/protected-a/<t>` must take any key, and `child-owner` skips.
  - `like-underscore`, `like-percent`: the registry finds a prefix's
    superspaces with SQL `LIKE` (`namespaceSupSubChecks` in
    `registry/registry_db.go`), in which `_` and `%` are wildcards. So
    with `/<t>-a_b` registered with one key, `/<t>-aXb/c` with another
    must still get 201, since `/<t>-a_b` does not enclose it; likewise
    `/<t>-pXYZq/c` beside `/<t>-p%q`. Pelican takes the decoy for a
    parent, and refuses them (403), so both rows fail, on v26.0.0-rc.3
    and main alike. They skip where chaining is off.
  - `again`: `/protected-a`, registered again with another key, must
    get 200, "already registered", and its registered keys and JWKS
    must be as they were.
  - `delete-foreign`, `delete-owner`, `delete-unknown`: a DELETE of
    `/api/v1.0/registry/<prefix>` takes a token of
    `pelican.namespace_delete` signed with a key registered for the
    prefix, as `pelican namespace delete` mints one
    (`deleteNamespaceHandler`). Signed with another key, it must get
    403; with the prefix's own, 200, after which the prefix's JWKS
    must get 404. A prefix never registered must get 400, as one JSON
    document. Pelican writes that answer and goes on, and writes
    another, so the body is two, and `delete-unknown` fails, on
    v26.0.0-rc.3 and main alike.
  - `approval`: a new registration starts Incomplete
    (`keySignChallengeCommit`), and its keys are served at once; but
    under `-p registry-approval`, its JWKS must get 403 until the
    admin approves it, and then its keys.
  - `cli`: `pelican namespace register` and `delete`, configured with
    a key of the run's (`IssuerKeysDirectory`) and a site name
    (`Xrootd.Sitename`), must register the prefix with that key alone,
    and delete it.
  - `cli-flags`: `pelican namespace register --privkey <key> --pubkey
    <key> --namespace-url <registry>` must register the prefix with
    `--privkey`'s key. Pelican binds `--privkey` to `IssuerKey`, and
    `--namespace-url` to `Federation.RegistryUrl`, only if each has a
    value when the command is defined, which it never does
    (`cmd/registry_client.go`), and `--pubkey` is read by nothing. So
    the client signs with a key it makes in its empty key directory,
    and the row fails, on v26.0.0-rc.3 and main alike. The dev
    container's configuration names the registry anyway, so no row
    shows `--namespace-url` ignored.
- `jwks`: the keys the registry serves for each prefix
  (`/api/v1.0/registry/<prefix>/.well-known/issuer.jwks`; the wildcard
  handler in `registry/registry.go`). A server registers the keys it
  publishes itself (`GetIssuerPublicJWKS`): those of its key directory
  (`framework/var/issuer-keys/<service>/`), and where its configuration
  sets `Server.IssuerJwks` (origin-0, origin-1, and `tiny`'s server),
  that file's. Keys are compared by their
  thumbprints, computed from each key's members, not taken from its
  `kid`.
  - `exports`: each export's JWKS must hold exactly the keys its origin
    publishes at its own `/.well-known/issuer.jwks`, which must include
    every key in its key directory: origin-0's, or another owner's own.
  - `servers`: so must each origin's server prefix's
    (`/origins/<host>:<port>`). A cache's (`/caches/<name>`) must hold
    only keys the cache publishes, and at least one: a cache never
    tells the registry of a key it adds since it registered, unlike an
    origin (`updateNamespacesPubKey`), which the suite does not check.
  - `openid`: each prefix's
    `/.well-known/openid-configuration` must name that JWKS as its
    `jwks_uri`.
  - `unknown`: a prefix never registered must get 404 for both.
  - `private`: no JWKS the federation serves may hold private key
    material (`d`, `p`, `q`, `dp`, `dq`, or `qi`): each server's own,
    each director's, and the registry's; each namespace issuer's, by
    its openid-configuration; the registry's for each prefix; and the
    registrations' keys, as the registry lists them all
    (`/api/v1.0/registry`), serves each
    (`/api/v1.0/registry/<prefix>`), hands its admin each
    (`/api/v1.0/registry_ui/namespaces/<id>/pubkey`), and lists them
    with its servers (`/api/v1.0/registry_ui/servers`). Each must be a
    JWKS.
- `registry-ui`: the registry's web API (`/api/v1.0/registry_ui`;
  `registry/registry_ui.go`). It registers two prefixes of the run's,
  and the admin approves one.
  - `list-public`: `/namespaces` with no credentials must list only
    Approved registrations, without their `pubkey`: the approved one,
    and not the other, which is Incomplete.
  - `list-pending`: `?status=Pending` with no credentials must get 403.
  - `prefix-type`: `?prefixType=bogus` must get 400.
  - `bad-status`: as the admin, `?status=Bogus` must get 400, as one
    JSON document. Pelican writes that answer and goes on, and writes
    the list too (`listNamespaces`), so the body is two, and the row
    fails, on v26.0.0-rc.3 and main alike.
  - `non-owner`: an ordinary user, `test-registry`, whom the admin
    creates once, and later runs reuse, must get 403 for
    `/namespaces/<id>` of `/protected-a`'s registration, which no user
    owns.
  - `unknown-id`: as the admin, approving or denying a registration
    that doesn't exist must get 404.
  - `denied`: under `-p registry-approval`, a registration the admin
    has denied must have its JWKS refused (403). Elsewhere the
    registry serves every registration's keys, and the row skips.
  - `servers`: `/servers` must list each of the shape's server
    prefixes as a registration of one server, once, and no name twice.
  - `server-one`: `/servers/<id>` must give each of those servers, and
    an ID no server has must get 404.
- `completeness`: a server's registration starts Incomplete
  (`keySignChallengeCommit`), and the completeness check
  (`POST /api/v1.0/registry/namespaces/check/status`;
  `checkStatusHandler`) must say it is not `complete`, with an
  `edit_url` of a page of the registry's for it. Its holder completes
  it through the web API (`PUT /api/v1.0/registry_ui/namespaces/<id>`;
  `createUpdateNamespace`), with an `access_token` of
  `registry.edit_registration` signed by the registration's key, by
  which a logged-in user who doesn't own it claims it. A claim is for
  good, so the suite completes a server registration of the run's own,
  `/origins/registry-test-<run>.invalid`, which it registers with a
  key of its own, as a server does; it would not undo a claim of one of
  the shape's. With neither `Registry.Institutions` nor
  `Registry.InstitutionsUrl` set, the registry takes any institution
  (`validateInstitution`).
  - `check-status`: each of the shape's server registrations must be
    incomplete, with an `edit_url`, and where nothing needs approval,
    Incomplete. (Under `-p registry-approval`, the `federation` suite
    has approved them.)
  - `incomplete`: the run's server registration must be Incomplete,
    unowned, and incomplete, with the claim page as its `edit_url`.
  - `foreign-token`: its completion by `test-registry`, with a token
    signed by another key, must get 403, and leave it as it was.
  - `own-token`: with a token signed by its own key, and a body that
    names another owner, it must succeed; the registration must then
    be `test-registry`'s, not the body's owner's, Pending, and
    complete, with the origin's edit page as its `edit_url`.
- `ownership`: who owns a registration, and what that lets them do
  (`registry/registry_ui.go`), on a registration of the run's own,
  `/registry-test-<run>-own`, which `test-registry` claims and hands
  on to `test-registry-heir`, another ordinary user, whom the admin
  creates once and later runs reuse. Where an owner edits, it is
  through `PUT /namespaces/<id>`, with the registry's CSRF token.
  - `claim`: a registration starts unowned, and the holder of its key
    claims it (`POST /namespaces/<id>/claim?access_token=...`,
    `claimNamespace`), with a token of `registry.edit_registration`
    signed with the key: with no token, 400; with another key's, 403,
    leaving it unowned; with its own, as `test-registry`, 200, after
    which it is `test-registry`'s; again, 200; and then as
    `test-registry-heir`, 409, leaving it `test-registry`'s.
  - `owner-edit`: once the admin approves it, its owner may change its
    description and its institution, and nothing else
    (`applyOwnerEdits`): an edit that also sends another site name,
    another key, and the same prefix must succeed, change the
    description, and leave the site name, prefix, and key as they
    were, the registry still serving the key alone, and the
    registration Approved; `test-registry-heir`'s edit must get 403,
    and change nothing.
  - `deny-resubmit`: once the admin denies it, its owner's edit must
    succeed, and resubmit it as Pending, still the owner's
    (`createUpdateNamespace`).
  - `invite-refusals`: an invite to take it over (`POST
    /namespaces/<id>/ownership-invites`,
    `createRegistrationOwnershipInvite`) is the owner's or the admin's
    to mint: `test-registry-heir`'s must get 403; the owner's with an
    `expiresIn` of `0s`, longer than 30 days
    (`maxOwnershipInviteExpiry`), or no duration, 400; and the admin's,
    201, with a token.
  - `invite-transfer`: the owner's invite, 201, must say that it is
    single-use; `test-registry-heir` redeems it (`POST
    /api/v1.0/invites/redeem/registration-ownership`, with the token;
    `handleRedeemRegistrationOwnershipInvite`), 200, naming the
    registration's ID and prefix, after which the registration is
    theirs; the former owner's edit must then get 403, and the new
    owner's succeed.
  - `invite-single-use`: the same token, redeemed again by
    `test-registry`, must be refused (400 or 404), and the registration
    stay `test-registry-heir`'s.
- `identity`: each server's ID, as it records it
  (`/api/v1.0/server/localMetadata`, as its admin), must be the one
  the registry gives for its server prefix
  (`/api/v1.0/registry/server/<prefix>`; `lookup`), and the registry's
  list of servers must list it once, and its name once (`listed`). It
  skips in the tiny topology, whose origin and cache are one process,
  with one record of itself.
- `downtime`: downtimes recorded at the registry
  (`web_ui/server_api.go`). A server's admin declares one at the
  server, which tells the registry; a registered server may declare
  one at the registry with a token of its own (subject its server ID,
  audience the registry's host and port, and
  `pelican.downtime_create`, `_modify`, or `_delete`;
  `checkRegisteredServer` in `token/token_verify.go`), but only its
  own; and the registry's admin may declare any, which the directors
  apply to routing (`director/cache_ads.go`). It uses the first
  federated cache and the first origin, and skips in the tiny topology,
  as `federation`'s `downtime` does. A downtime's times are in
  milliseconds. What it declares at the cache, and as the origin,
  begins tomorrow, so that it changes no routing.
  - `mirrored`: one the cache's admin declares must reach the
    registry within 60 seconds, as the cache's, with the cache's ID.
  - `foreign-modify`: the origin's token must not change it, by PUT
    or DELETE (403).
  - `mirror-deleted`: once the cache's admin deletes it, the registry
    must not have it within 60 seconds.
  - `bound`: one the origin declares at the registry, naming the
    cache's ID and name in its body, must be recorded as the origin's,
    with its ID and name.
  - `source-registry`: one the origin declares with `"source":
    "registry"` must get 403.
  - `admin-current`: one the registry's admin declares for the cache,
    now: within 150 seconds, no director may name the cache, and each
    must list it in `/api/v1.0/director_ui/servers` as `filtered`; once
    the admin deletes it, within 150 seconds every director must name
    the cache again.
  - `future`: one the admin declares for the cache, beginning
    tomorrow: within 150 seconds, each director must list it in
    `/api/v1.0/director_ui/downtimes`, and in
    `/api/v1.0/director_ui/servers/<name>/downtimes`, with its
    severity, and `admin` as who created it, and still name the cache.


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
- `html-links`: a browser user follows the links of an origin's HTML
  listing, so each must lead to its entry. Under
  `/protected-a/data/listings/<run>/html/` are the `names` suite's
  objects, beside their decoys ([names](#names)), and a collection
  holding one object. A GET of that collection with `Accept: text/html`
  goes to each origin, and each link in its answer is followed as a
  browser follows it: resolved against the listing's URL, without its
  fragment, and with what a URL may not hold as it is percent-encoded,
  but a `%` left as it is. A link is known by its text, the entry's
  name. One to an object must bring its bytes, never a decoy's or a
  404; one to the collection, or to the parent (`..`), if there is one,
  an HTML listing that links to what it holds. Every entry must have a
  link. A native origin's listing must also carry `Cache-Control:
  no-store`, so that no cache keeps it, as Pelican sends it. Pelican's
  native origin writes each link as the entry's path in the export,
  without the namespace, HTML-escaped but not percent-encoded
  (`streamDirectoryListing` in `origin_serve/handlers.go`), so no link
  leads to its entry, and the row fails there, on main and v26.0.0-rc.3
  alike. An XRootD origin writes the whole path, but also not
  percent-encoded, so the links to `a%20b`, `x%41y`, `hash#tag`, and
  `what?q=1` bring their neighbors' bytes, and the row fails there too.
  Of the native origins, only `posixv2`, whose storage is on its own
  host, answers an HTML listing at all. An origin that answers none is
  passed over, and named in the note, since `collection-get` fails it;
  where none answers one, the row skips, as it does where the origins
  take no direct clients.
- `plain-object-listing`: a PROPFIND of Depth 1 of an object, which is
  no collection, must answer as Depth 0 would (RFC 4918, section 9.1):
  207, listing the object alone, with its size. Every origin and
  federated cache is asked so of `tree/b` in `/protected-a`, and must
  answer so, or refuse where it may not list. Then `pelican object ls -l
  --json` of it must list the object alone, where `/protected-a` has
  `Listings` (Pelican's `TestListPlainObjectPosixV2`).


## WebDAV methods

Pelican's native origin serves every WebDAV method through
`golang.org/x/net/webdav`, behind its own checks: of the request's
token, for the action it maps the method to (`getActionFromMethod` in
`origin_serve/handlers.go`: GET, HEAD, OPTIONS, and PROPFIND read; PUT,
POST, MKCOL, LOCK, UNLOCK, PROPPATCH, and COPY create; DELETE and MOVE
modify), and of a COPY's or MOVE's `Destination`
(`checkCopyMoveDestination`). The `webdav` suite sends the methods that
no other suite does, straight to each origin in `/protected-a`, under
`/protected-a/data/webdav/<run>/<origin>/`, and to each cache, under
`.../caches/`, and removes what it wrote
afterward. It needs `/protected-a` to take writes and direct clients.
The scenarios that send WebDAV's other methods ask only native origins,
and skip an XRootD origin; `symlink-writes` and `stored-html` ask every
origin, so that an XRootD origin is a control. A scenario's row fails
with the first thing it found wrong; each thing is printed as it is
found. Its tokens are the origins' own, of the scopes each names.

- `post-create-only`: x/net serves a POST as a GET. A POST of an object
  with a token of `storage.create` alone, and with one of
  `storage.read` alone, must each be refused (401 or 403), with none of
  the object's bytes: the first may not read, and a POST is not a write
  anyone asked for. Pelican authorizes a POST as a create, so the first
  gets the object, and the row fails there.
- `copy-move-destination`: COPY and MOVE, with the `server` token. Within
  `/protected-a`, each must succeed (201 or 204), leaving the bytes at
  the destination, and for MOVE, none at the source. To a `Destination`
  in `/protected-b` or `/public`, directly or by climbing out of
  `/protected-a` with `..`, `%2e%2e`, `%2E%2E`, or `.%2e`; or, with a
  token scoped to the run's `a/`, from `a/` to `b/`, each must be
  refused (403), and change nothing at the source or the destination,
  in any store. With no `Destination`, it must get 400, and to another
  server (`https://elsewhere.invalid/...`), 502 (RFC 4918, sections 9.8
  and 9.9), which x/net answers, changing nothing.
- `copy-move-scopes`: with a `storage.create` token, a MOVE must be
  refused (401 or 403), since it removes its source, and change
  nothing; a COPY to a new path must succeed (201); and a COPY over an
  object must be refused, and leave it as it was, since replacing an
  object takes `storage.modify`, as the WLCG token profile says. x/net
  replaces an object that a COPY lands on unless asked not to, while
  Pelican authorizes every COPY as a create (`getActionFromMethod`
  says COPY is purely additive), so that row fails there. Then, with
  the `server` token, a COPY and a MOVE onto an object with
  `Overwrite: F` must each get 412, and change nothing (RFC 4918,
  section 10.6).
- `mkcol`: MKCOL with a `storage.read` token must be refused, and make
  nothing; with the `server` token, it must make a new collection
  (201), which a PROPFIND then shows; where a collection already is, it
  must get 405; and with a body, 415, making nothing (RFC 4918, section
  9.3.1). The row's note says what each origin answered where a
  collection was. Pelican's `posixv2` and `pstore` make a collection
  with `MkdirAll`, and `s3v2` writes a marker, so each answers 201
  there, and the row fails.
- `lock`: Pelican uses no locks, but x/net keeps them, in memory
  (`webdav.NewMemLS()`), forever when a LOCK names no `Timeout`, and
  makes an empty file for a LOCK of a new path, beside POSC and the
  metadata events. With a `storage.create` token and no `Timeout`, a
  LOCK of a new path and one of an object must each be refused, or
  grant a lock with a timeout (`Second-<n>`, with `n` above 0; x/net
  writes a lock without one as `Second-0`); and the new path must hold
  nothing. What locks should do here is not settled, so either is
  accepted. Then, with the `server` token and no lock token, a PUT of
  the new path and a DELETE of the object; the row's note says what
  each got (423, if the lock holds). Each lock granted is then
  released with UNLOCK, which must succeed: otherwise its path stays
  locked until the origin restarts. A native origin grants each lock
  forever, and makes the file, so the row fails there.
- `symlink-writes`: as the `auth` suite's `symlink` rows do for reads,
  it makes symbolic links in `/protected-a`'s storage on disk, to a
  file and a directory beside the namespaces' storage in each store,
  which any uid may write, so that only the server can stop a write
  that follows a link. A HEAD of the linked file with
  `Want-Digest: crc32c` must report no digest of it. A PUT and an MKCOL
  under the linked directory, and a DELETE of the file in it, must be
  refused (4xx). A PUT and a DELETE of the linked file itself may be
  refused, or replace or remove the link alone: a POSC upload renames
  its staged file over the link, and an XRootD origin unlinks it. Each
  must leave the file and the directory as they were, and nothing new
  beside them. Pelican's `posixv2` refuses a link that leaves its
  export (`os.Root`); an XRootD `posix` origin follows it, so the row
  fails there. A `pstore` origin's store is encrypted, and holds no
  links, so it is passed over.
- `stored-html`: an origin serves its data on the host and port of its
  web UI and its login cookie, so an object that a browser renders as a
  page could run script there, against its admin. An HTML page and an
  SVG image, each with a script, put at each origin, must come back
  with `X-Content-Type-Options: nosniff`, and either typed so that a
  browser will not render them (`application/octet-stream`, or
  `text/plain`) or with `Content-Disposition: attachment`. The row's
  note says how each origin typed them. A native origin types an object
  by its name's extension (`http.ServeContent`), and sends no
  `nosniff` (only the V2 cache does), so the row fails there.
- `webdav-semantics`: OPTIONS of each native origin's root, with no
  token, must answer 200, with a `DAV` header and an `Allow` naming
  OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL, COPY, and MOVE
  (`registerRootOptions`). A PUT onto a collection must get 405 or 409,
  leaving what it holds. A PROPPATCH of an object, setting two dead
  properties that no server here keeps, must get 207, with a failing
  status for each, or be refused (403, 405, or 501), leaving the
  object as it was. Last, after everything else at every origin, a
  DELETE of `/protected-a`'s root, with the `server` token, must get
  403 or 405, and remove nothing. If an origin honored it, it would
  take `/protected-a`'s storage with it, and `init`'s objects, so
  `webdav` runs after every other suite but `auth`, `transfers`,
  `restart`, and `keys`.
- `cache-methods`: a cache serves objects and listings (GET, HEAD, and
  PROPFIND), and relays a PUT or a DELETE to an origin (`blocks`'s
  `write-through`), and that is all a V2 cache routes
  (`local_cache/persistent_cache_api.go`), so Gin answers every other
  method 405 (`HandleMethodNotAllowed` in `web_ui/ui.go`). Each cache
  is sent POST, COPY, MOVE, PROPPATCH, and LOCK of an object of the
  run's own, which every origin holds, and MKCOL of a new path beside
  it, with the `server` token, which would let an origin honor each: so
  a cache that relayed one would show. A V2 cache must answer 405; an
  XRootD cache must refuse (4xx or 5xx), as its HTTP server sees fit,
  and the row's note says how. Whatever the answer, no origin's store
  may change: the object must keep its bytes, and nothing may appear at
  the new path or at a COPY's or MOVE's `Destination`.


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
- Every director, asked for each object by its name through its object
  route (`/api/v1.0/director/object/`), must send a plain HTTP client to
  a federated cache, or, with no cache, to an origin, by a redirect
  whose `Location` names the object again. The director takes the name
  decoded and encodes it anew (`getRedirectURL` in Pelican's
  `director/director.go`), so a name it mangled on the way would fetch
  a decoy, or nothing. The client follows the `Location` as it is, as
  `curl -L` does, and must get the object's bytes.
- Every origin and federated cache must list each name once, decoded,
  with its size, as must `pelican object ls`.
- `pelican object get` of every object must write a file of each one's
  name, with its bytes, and nothing else: through the cache the director
  picks, direct from an origin (`--direct`) where `/protected-a` takes
  direct clients, and from each federated cache in turn, named
  (`--cache`), which alone may serve it, since the client builds that
  cache's URL of each name itself; `pelican object put` of a file of
  each name must land as the object of that name.


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

  `posc-expiry` checks, at the first origin, what POSC does with
  abandoned uploads (`expireFiles` and `touchOpenFiles` in
  `origin_serve/posc.go`), and how it guards its staging directory:

  - `mode`: a complete upload's object must have mode 0644, as without
    POSC: the 0666 with which the WebDAV handler opens a PUT's file,
    less the origin's umask, 022. POSC opens its staging file with the
    mode asked for. This upload also makes the staging directory, if
    there is none yet; the suite never makes it.
  - `stale`: a staging file that the suite plants in a staging
    directory of its own, `.pelican-posc/expiry-<run>/in_progress.*`,
    last modified two hours ago, past the default
    `Origin.Posc.FileTimeout` of an hour, must be removed within 15
    seconds, since the reaper sweeps every 5, and counted in
    `pelican_origin_posc_expired_total`.
  - `staging-put` and `staging-mkcol`: a PUT and a MKCOL straight into
    `.pelican-posc/` must be refused, the PUT with 409, as for a path
    whose parent is missing, since POSC hides the directory, and the
    MKCOL with 405, since POSC refuses to make anything there; and
    neither may leave anything there.
  - `idle`: the suite restarts the first origin with
    `Origin.Posc.FileTimeout` at 20 seconds and `KeepaliveInterval` at
    5, as the last of its configuration layers
    ([service control](../README.md#service-control)), and restores it
    afterward, after which the federation must serve again within 150
    seconds. A staging file planted 40 seconds old must then be reaped,
    which shows the short timeout in force. An upload that sends half
    of its 2 MiB, then idles 35 seconds, past the timeout, must keep
    its staging file, whose mtime POSC's keepalive refreshes, and
    complete, whole, once it sends the rest. The other scenarios run
    before the restart, under the default timeout. The row needs a
    control helper and a topology other than `tiny`; elsewhere, it
    skips, and says why.
- **XRootD's POSC** (`-p origin-atomic-uploads`): `Origin.EnableAtomicUploads`
  stages each upload in `Origin.UploadTempLocation`, which Pelican
  requires on the exports' filesystem but in none of them, so `fed.sh`
  puts it beside their storage in each origin's store,
  `framework/var/data/origin/<N>/.in-progress/`. The `posc` suite runs
  the same interruptions, judged by the staging files there and by the
  store. `hidden` skips, since nothing is staged inside an export, as
  does `posc-expiry`, since XRootD reaps its own staging; and `stalled`
  checks no metric, which only the native POSC has.
- **pstore** (`-p origin-pstore`) commits a new version of an object only
  when its upload completes, so the `posc` suite runs there too: the same
  interruptions, judged by what the origins serve, once
  `pstore/objects` has grown to show the upload under way (`chunked`
  sends just over pstore's 1 MiB spill threshold for this). `stalled`,
  `hidden`, and `posc-expiry`, which are about POSC's staging, skip.
- **Metadata** (`-p origin-metadata`): the origins send an event for every
  object committed, overwritten, or deleted to the `metadata` recorder. It
  saves each request in `framework/var/data/metadata/` (listed in
  `index.tsv`) and passes it to `metadata-verifier`: Pelican's
  `sample_metadata_server`, built from `PELICAN_TAG`'s source. The recorder
  answers 503 once for objects named `fail-once-*`, 503 always for
  `fail-always-*`, which holds an event queued, and 422 for `reject-*`.
  It reads `recorder.py` when it starts, so after the framework changes
  it, run `./fed.sh restart metadata`.
  The `metadata` suite uploads with `--metadata-file` and
  `--metadata-body`, and checks the events, including that
  `object.updated` carries the new size and custom fields, compared
  type for type. Beyond the events of uploads, it checks, at the first
  origin:

  - `status-urls`: an eventual-mode upload whose event outlives it
    hands the uploader two URLs, `X-Pelican-Metadata-Query-Url` and
    `X-Pelican-Metadata-Manage-Url`, on the origin's web API, which
    take no token: the random token in each is the capability
    (`origin_serve/metadata_status_api.go`). `queued`: an upload of a
    `fail-once-*` object must answer `queued`, with both URLs, each of
    which a GET must answer with the event, `pending`; once the event
    is delivered, which deletes it, both must answer 404. `rejected`:
    an upload of a `reject-*` object must answer `rejected`, with both
    URLs, which must read it `rejected`, as it stays; a DELETE through
    the manage URL must remove it (200), after which both answer 404.
    `cancel`: for an upload of a `fail-always-*` object, a DELETE
    through the query URL, which may only read, must get 403, and leave
    the event; one through the manage URL must cancel it (200); then
    both URLs must answer 404, and for 20 seconds, though a retry was
    due within 10, the recorder must get no delivery of it.
  - `catalog`: the events of changes to the store other than an upload,
    each awaited for 30 seconds. `move`: a MOVE of an object must send
    `object.committed` (or `object.updated`) for its new path, and
    `object.deleted` for its old one. `copy`: a COPY of an object
    uploaded with custom fields must send `object.committed` for the
    copy, with the source's fields. `collection-delete`: a DELETE of a
    collection that holds two objects must send `object.deleted` for
    each. Pelican records a MOVE in its record of objects, but
    publishes nothing for it (`aferoFileSystem.Rename` in
    `origin_serve/filesystem.go`); removes a collection with one
    `RemoveAll`, for which it publishes `object.deleted` for the
    collection's own path, and none for its objects; and publishes the
    copy as an upload of it with no custom fields, since a COPY carries
    none. So the three rows fail, on main and v26.0.0-rc.3 alike.
    Whether a copy's event should carry its source's fields is not
    settled; the suite holds that the catalog should describe the copy
    as it does the source.

    The reconcile sweep (`Origin.Metadata.ReconcileEnabled`, on by
    default) re-enqueues an object whose publish was lost, as to a
    crash between the commit and the queue's write, and, its
    documentation says, never touches an object the origin did not
    write. The suite makes three objects that it must leave alone:
    `reconcile-cancelled`, an upload of a `fail-always-*` object whose
    event is cancelled through its manage URL; `reconcile-rejected`, an
    upload of a `reject-*` object; and `reconcile-observed`, an object
    written straight to the store, which the origin then serves, and so
    records as one it did not write. Each must be in the origin's
    record of objects, or its row is inconclusive. Then the suite
    restarts the first origin with a sweep every 15 seconds, of objects
    at rest for 5 (`ReconcileInterval` and `ReconcileSettleWindow`, 2h
    and 6h by default), and for 45 seconds the recorder must get no
    delivery of any of the three. Pelican's sweep takes every live
    record without a publish of its ETag and with no pending queue row
    (`ReconcileCandidates` in `origin_serve/object_metadata.go`), so all
    three rows are expected to fail: the rejected event is republished
    at every sweep, and every object that the origin has served but not
    written is published, as if it had. The rows run in eventual mode
    only.
  - `object-metadata-cli`: the first origin's record of objects
    (`Origin.Metadata.TrackAccess`, which the metadata presets turn on).
    `api`: after an upload, each of its admin routes,
    `/api/v1.0/origin_ui/object_metadata`, `/lookup`, and `/history`,
    must refuse no credentials (401) and an ordinary user (403); the
    admin's lookup must have the object, of its size, and its history a
    `commit`. The ordinary user, `test-metadata`, is made as the
    `webapi` suite's is ([web API](#web-api)). `cli`:
    `pelican-server origin object-metadata list`, `get`, and `history`,
    with the admin's token file, must each print, as JSON, what the
    route it asks answers the admin, but for access times. It needs a
    `pelican-server`, as `collections`'s `cli` does
    ([client binary](../README.md#client-binary)), and skips without
    one.
  - `queue-admin`: the publish queue's admin routes,
    `/api/v1.0/origin_ui/metadata_queue` (its list and `/_health`, and an
    event's GET, DELETE, and `/retry`), with an event held queued: the
    suite writes a `fail-always-*` object straight to the store, and
    deletes it through the origin, which then publishes
    `object.deleted`, in either mode. `auth`: each route must refuse no
    credentials (401) and an ordinary user (403). `admin`: the admin's
    list must have the event, `/_health` count it, and a GET show it; a
    GET of an event that isn't there must get 404. `retry`: with the
    event's next delivery minutes away, a retry must answer 200, and the
    recorder get the event again within 30 seconds. Pelican's handler
    moves the event's next attempt to now, but wakes no worker
    (`retryQueueRowHandler` in `origin_serve/metadata_admin.go`), and
    the workers sleep until the attempt they last saw due, so the row
    is expected to fail. `delete`: the admin's DELETE must remove the
    event (204). With publishing off, `queue-admin` runs alone, as one
    row, `off`: the list must refuse no credentials (401), and answer
    the admin, as `/_health` must, 503.
  - `backpressure` (eventual mode): the namespace's queue capped, at two
    rows more than it has pending when the suite asks
    (`Origin.Metadata.MaxQueuedPerNamespace`), and at 4096 bytes of
    blobs (`MaxQueuedBytesPerNamespace`); both are off by default.
    `rows`: uploads of `fail-always-*` objects, each of which must be
    taken, until the namespace has as many rows pending as the cap; then
    an ordinary upload must be refused with a 5xx, which tells a client
    to try again later, leave nothing, and be counted in
    `pelican_origin_metadata_backpressure_refusals_total{limit="rows"}`.
    `bytes`: with those rows deleted through the admin API, an upload
    of a `fail-always-*` object with a 4096-byte blob (a multipart PUT,
    as in [exports and copies](#exports-and-copies)) must be taken; then
    an ordinary upload must be refused, as before, and counted for
    `bytes`. `drained`: with the held rows deleted, an ordinary upload
    must be taken, and stored. Pelican refuses an upload by failing its
    close (`ErrMetadataBacklogFull`), which x/net's WebDAV handler
    answers with 405, so `rows` and `bytes` fail, on main and
    v26.0.0-rc.3 alike; and without POSC (`-p origin-posc`), nothing
    removes the refused object, so they fail for that too.

  `queue-admin` and `backpressure` restart the first origin together,
  with every failed delivery put off for ten minutes
  (`Origin.Metadata.MinBackoff` and `MaxBackoff`), so that a held event
  stays put, and with the caps; `catalog`'s reconcile rows restart it
  again. Each restart's configuration is the last of the origin's
  layers, and is removed afterward, after which the federation must
  serve again within 150 seconds
  ([service control](../README.md#service-control)). Without a control
  helper, or in the `tiny` topology, the restarts' rows skip, but for
  `queue-admin`'s, which run at the origin as it is, but for `retry`.
  Afterward, the suite deletes through the origin the objects whose
  events the recorder always fails, so that the origin records them
  gone, and then removes the run's rows from the queue.
- **Transactional mode** (`-p origin-metadata-tx`): an upload should fail
  with a 5xx if its event can't be delivered, and its object should be
  removed, as Pelican's `docs/metadata-publish-design.md` says.
  The `metadata` suite's `retry` and `reject` check both, and that the
  recorder answered a delivery for the object with 503 or 422.
  `status-urls`, `catalog`'s reconcile rows, and `backpressure` skip,
  since a transactional event never outlives its upload; the rest run.


## Unix users

Under `-p origin-multiuser`, a POSIX origin (`posixv2`, or XRootD's
`posix` through its multiuser plugin) does every filesystem operation
as the Unix user that the request's token maps to; under
`-p server-unprivileged`, every server drops to the `pelican` user. The
`users` suite sends requests straight to each origin, and checks who
owns the files they leave, and what a user can learn of another's. Its
objects go under `/protected-a/data/users/<run>/`, which it removes
afterward. A scenario with several cases records a row per case, named
`<scenario> <case>`.

Under multiuser, `fed.sh` maps tokens to users with a mapfile
(`framework/var/generated/multiuser-mapfile.json`): the tests' subject
to `pelican` (uid 10941), and any other to `xrootd` (uid 10940),
`Origin.ScitokensDefaultUser`. Its other rules are the suite's, each
for a subject or a group that only the suite's tokens carry: two map to
`root` and `daemon`, and the rest to `pelican`, so that an upload's
owner shows that the rule matched, and not the default. A native origin
is the one tested; an XRootD origin is the control.

Whether a bind mount shows a file's owner depends on the host, so the
suite first gives a file of its own to uid 10941 and gid 10941; where
the store doesn't show that, the rows that depend on owners are
inconclusive.

- `owner`: an upload with the tests' token is the `pelican` user's,
  which `fed.sh` maps the tests' subject to under multiuser.
- `default-user`: under multiuser, an upload with a token for another
  subject is the `xrootd` user's. Under POSC, one of these two users'
  uploads fails, as `posc-multiuser` says.
- `primary-group`: an upload with the tests' token, which names no
  group, must be in the `pelican` user's own group (gid 10941). A
  native multiuser origin acts as the token's first group
  (`wlcg.groups`), and without one, as gid 0, root's
  (`resolveIdentity` in `origin_serve/multiuser_fs.go`), so the row
  fails there, on main and v26.0.0-rc.3 alike.
- `public-tokenless`: under multiuser, with no token, at each origin, a
  GET of an object in `/public` must serve its bytes (`get`), and a
  PROPFIND of Depth 1 of its collection must answer 207 and name it
  (`propfind`). Pelican's documentation says such requests run as
  `nobody` (`Origin.ScitokensUnauthenticatedUser`), but a native origin
  attaches a user only from a token (`authorizeWithContext` in
  `origin_serve/authz.go`), and its filesystem refuses a request with
  none (`resolveIdentity`). So both rows fail there, with 404 and 405,
  on main and v26.0.0-rc.3 alike. They skip where `/public` isn't
  exported.
- `bypass`: under multiuser, at each origin, the suite makes a file of
  the `pelican` user's, mode 0600, in a directory of its own, mode 0700,
  straight in the store. The `pelican` user's own GET of it must serve
  it, or the rows are inconclusive. Then the `xrootd` user (a token for
  another subject) must be refused each of these (403, or 404 from a
  permission error), and get no `ETag`, `Last-Modified`, or `Digest`,
  nor the file's bytes:
  - `get`: a plain GET of the file;
  - `html-listing`: a GET of the directory with `Accept: text/html`,
    which also must not name the file;
  - `if-none-match`: a GET of the file with `If-None-Match: *`, which
    must not get 304;
  - `digest`: a HEAD of the file with `Want-Digest: crc32c`;
  - `staging`: under POSC, the same three, of POSC's staging directory
    for the `pelican` user (`<storage>/.pelican-posc/pelican/`, mode
    0700), and of a file in it, mode 0600, that the suite makes there.

  A native origin answers these paths from the store itself, as its own
  process, before it asks the filesystem that acts as the user
  (`handleGetWithETag`, `streamDirectoryListing`, and
  `handleHeadWithChecksum` in `origin_serve/handlers.go`): a plain GET
  gets 404 with the file's `ETag` and `Last-Modified`; the listing is
  served; `If-None-Match: *` gets 304; and the HEAD gets 404 with a
  `Digest`. So every row fails there, on main and v26.0.0-rc.3 alike.
- `posc-multiuser`: under POSC and multiuser, at each origin:
  - `first`: an upload with the tests' token must succeed, and be the
    `pelican` user's, as must its staging directory,
    `<storage>/.pelican-posc/pelican/`, mode 0700.
  - `second`: then an upload with another subject's token must succeed,
    and be the `xrootd` user's, as must `.pelican-posc/xrootd/`, mode
    0700.
  - `read-only-dir`: an upload with the tests' token into a directory of
    the `pelican` user's, mode 0500, must be refused (403), and land
    nothing.

  POSC sits beneath the multiuser filesystem, so the first upload into
  a store, whoever's, makes the staging directory, and `.pelican-posc`
  above it, as its own user, both mode 0700 (`ensureUserDir` in
  `origin_serve/posc.go`), and no other user can make its own there. In
  a full run, the `posc` suite's uploads, with the tests' token, come
  first, so `first` passes, and `second` fails, as does `default-user`.
  Run alone, into a store with no `.pelican-posc` yet, `default-user`
  comes first, and `first` fails instead. A failing row's note says who
  owns `.pelican-posc`. The rename into place runs as the origin's own
  process (`poscFile.Close`), so `read-only-dir` fails too. It uploads
  as the `pelican` user, who can stage, since an upload of the `xrootd`
  user's would be refused for the wrong reason. Both fail on main and
  v26.0.0-rc.3 alike.
- `min-id`: under multiuser, at each origin, an upload with a token
  that the mapfile maps to `root` (`root`), and one to `daemon`
  (`daemon`), whose uids are below `Origin.MultiuserMinID` (1000 by
  default), must be refused (403), and leave no file, nor, under POSC,
  a staging directory for the user. A native origin refuses the user
  (`identity.WithMinID`), but with 404, since the error isn't a
  permission error, so the rows fail there on their status alone, on
  main and v26.0.0-rc.3 alike.
- `mapfile`: under multiuser, at each origin, uploads whose owners show
  the mapfile's rules at work:
  - `path`: with a token for a subject that the mapfile maps to
    `pelican` under `/data/users/mapped` alone, an upload under
    `/protected-a/data/users/mapped/` must be the `pelican` user's, and
    one elsewhere must not. In XrdSciTokens' mapfile, which
    Pelican's documentation cites, a `path` is relative to the
    namespace; the mapfile also has the rule with the federation's
    path, `/protected-a/data/users/mapped`, so that either reading
    passes. A native origin maps a token to a user with no path at all
    (`UserMapper.MapTokenToUser` in `origin_serve/user_mapfile.go`), so
    no `path` rule matches, and the row fails there, on main and
    v26.0.0-rc.3 alike.
  - `group`: an upload with a token whose `wlcg.groups` is `[xrootd]`,
    which the mapfile maps to `pelican`, must be the `pelican` user's.
    The group is a Unix group's name, since a native origin acts as the
    token's first group, and refuses a request whose group it can't
    find.
  - `edited`: an upload with a token for a subject of the run's must
    not be the `pelican` user's. Then the suite adds a rule to the
    mapfile, in place, mapping the subject to `pelican`, and an upload
    with a new token for it must be the `pelican` user's within 150
    seconds. Pelican rereads the mapfile every
    `Origin.UserMapfileRefreshInterval` (1m by default), and XRootD its
    configuration every 60 seconds. Both map a token to a user once,
    and keep it, so each try has a new token. The suite restores the
    mapfile afterward. This first upload, and `path`'s elsewhere, which
    no rule maps, may be the `xrootd` user's or be refused, since under
    POSC the `xrootd` user may be unable to stage (`posc-multiuser`).
  - `username-claim` and `unmatched` need configuration that no shape
    has, so the suite restarts every origin with
    `Origin.ScitokensUsernameClaim` set and
    `Origin.ScitokensDefaultUser` unset, as the last of its
    configuration layers
    ([service control](../README.md#service-control)), and restores
    them afterward. Then every origin must serve a probe object, every
    director name every origin for it, and every federated cache serve
    it, within 150 seconds. The restart needs a control helper and a
    topology other than `tiny`; elsewhere, the rows skip, and say why.
    `username-claim`: an upload with a token whose username claim names
    `pelican` must be the `pelican` user's, since Pelican's
    documentation says the claim overrides
    `Origin.ScitokensDefaultUser`. A native origin with a mapfile takes
    the claim only where a rule maps it, and otherwise the default
    user, here none, so it drops the token, and the row fails there, on
    main and v26.0.0-rc.3 alike. `unmatched`: an upload with a token
    whose subject matches no rule, and which has no username claim,
    must be refused (401 or 403), and write nothing. A native origin
    drops the token (`authConfig.loader` in `origin_serve/authz.go`);
    XRootD refuses it for lacking the claim.

Under `-p server-unprivileged`, only `owner` and `primary-group` apply;
the rest skip.


## Another owner

Under `-p topo-multi-owner`, origin-2 has a key and an issuer of its
own, and exports `/other`, like `/protected-a`, and `/public/other`,
like `/public` and nested in it (as far as `ORIGIN_NAMESPACES` exports
those). Where `/public` is exported, origin-2 also exports
`/public/secret`, nested in it too, which takes a token like
`/protected-a` but mirrors no namespace: the `auth` suite's
`nested-protected` check asks for it ([authorization](#authorization)),
and the `owners` suite pairs it with nothing. `fed.sh` describes origin-2
in `framework/var/generated/owners` and `owner-exports`; the other
suites test only the origins' namespaces. The `owners` suite checks:

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
  `origins`, and `none`, unless `/public` takes writes
  (`-p origin-public-writes`), when each side has an issuer that decides
  a write there, and it gets every credential but `ns-jwks`, since no
  public namespace has keys of its own.
- `get`, `direct-get`, `put`, `cross-get`, and `cross-put`: the client
  moves origin-2's objects through a cache (served by one) and from
  origin-2 (served by it), uploads with origin-2's token to each of its
  exports that takes writes (`/other`, and `/public/other` under
  `-p origin-public-writes`), and presents each owner's token in the
  other's namespace, which must be refused with nothing stored.


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
skips with the issuer off (`-p auth-no-issuer`) and with the external
issuer (`-p auth-external-issuer`).

The suite's users are records of the issuer origin (origin-0, or `fed`),
which its admin creates once and later runs reuse: `test-curator`, whom
`Server.CollectionAdminUsers` in `framework/config.d/base/40-origin.yaml`
makes a collection administrator, and seven ordinary users, `test-owner`,
`test-writer`, `test-reader`, `test-outsider`, `test-sharer`, `test-guest`,
and `test-heir`. The suite acts as each through a bearer token that the
origin's web API takes as theirs: signed with the origin's own key and
naming its local issuer, as `pelican cache introspect` mints the admin's
(`credentials.mint_user()`), so no login or identity provider is needed.
The admin, and then each user, first agree to the origin's acceptable
use policy, without which it refuses them its routes for users and
groups ([web API](#web-api)).
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
- `share-confinement`: `test-sharer` may share only within `alpha`: a
  share whose namespace climbs out of it (`<alpha>/../decoy`), lies
  beside it (`decoy`), begins like it without lying in it (`alphax`), or
  lies outside every export is refused (400), and `test-guest`'s token
  gets nothing of `decoy`. Pelican cleans the namespace before it
  compares it with the collection's (`CreateShare` in
  `database/collection.go`), so this guards against a regression. It
  runs before `shares`, which leaves `test-sharer` unable to read
  `alpha`, and skips for a multiuser origin, which refuses every share.
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
each of its flows, and how its refresh tokens behave; and the
boundaries that keep one user, client, or namespace from acting as
another. It needs the embedded issuer and a protected namespace, as
`collections` does, and skips with the issuer off or the external one.

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
`test-curator`, a collection administrator. The admin, and then each
user, first agree to the origin's acceptable use policy, as
`collections`'s do. `test-member` is in both
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
secret and proves itself with PKCE. Only `registration` registers a
client itself (RFC 7591). The scenarios:

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
- `isolation`: each namespace's issuer is a trust domain of its own
  (`oauth2/issuer/registry.go`, and each issuer's storage, which is
  keyed by namespace). With `test-member`'s tokens and the suite's
  device client from the suite's namespace's issuer, A, and the other
  protected namespace's, B: A's client is unknown at B's device and
  token endpoints (401 `invalid_client`); A's refresh token, presented
  by B's client, is `invalid_grant`; A's access token introspects as
  inactive at B, `userinfo` at B refuses it (401), and B's exchange
  client may not exchange it (`invalid_grant`); and A's access and
  refresh tokens, revoked at B by B's client, still introspect as
  active at A. A collection belongs to its namespace's
  issuer alone: B grants `test-member` nothing of `gamma`'s path,
  which A grants, though B holds an object there, which B's token may
  not read. The issuer of a namespace that merely begins like the
  suite's (`/protected-ax`) has no discovery document (404), not A's.
  Without a second protected namespace, only the last is checked.
- `device-csrf`: the verification page takes a decision only with its
  CSRF token in both its cookie and the body
  (`handleDeviceVerifySubmit`). As `test-member`, an approval with the
  token in the body alone, with a cookie of another value, or in
  neither, and a denial with it in the body alone, are each refused
  (403, `CSRF validation failed`). The flow then still polls
  `authorization_pending`, and an approval with the token in both
  still works.
- `admin-clients`: the issuer's admin API for its clients
  (`/api/v1.0/issuer/admin/ns/<namespace>/clients`) refuses no token
  (401) and `test-member`'s (403), to list them or to delete one. As
  the admin, the list holds a client the scenario creates, and neither
  the list nor a GET of it shows a secret; a client asking for
  `client_credentials`, which the API does not know, is refused (400).
  Once the admin drops the refresh grant from the scenario's client,
  its refresh token is refused (`unauthorized_client`); once it
  narrows its scopes to `openid` and `archive`'s read, a token through
  it carries those alone, however much is asked. Changing a client
  that never was is 404. Once the admin deletes the client (204), its
  refresh is `invalid_client`, and a second delete is 404. The
  scenario changes only the client it creates.
- `cors-and-pkce`: from another site (`Origin: https://evil.invalid`),
  the discovery document may be read by anyone
  (`Access-Control-Allow-Origin: *`, as Pelican sends for public
  metadata, or none), but the token and device authorization
  endpoints, and a preflight of the token endpoint, allow no origin
  that is not among `Issuer.RedirectUris`, which the framework sets
  none of. The public client's authorization code flow without a PKCE
  challenge is refused (`invalid_request`), with no code.
- `code-expiry`, under `-p origin-short-tokens` (codes last a minute,
  `Issuer.AuthorizationCodeLifetime`; it skips otherwise): a device
  flow says `expires_in` 60. A device code approved at once, and an
  authorization code, each redeemed 3 seconds after the minute is
  over, are refused (`expired_token` or `invalid_grant` for the device
  code; `invalid_grant` for the authorization code); and a device code
  never approved may not then be (400). Pelican deletes expired device
  codes every ten minutes, and redeems a deleted one as 404
  `not_found`, so the scenario redeems soon after the expiry, when
  that is unlikely.
- `registration`: a client may register itself at the issuer
  (`<issuer>/oidc-cm`, RFC 7591). One that asks for the
  `authorization_code` grant is registered (201) with the device code
  and refresh grants alone, and a registration access token; one that
  names a redirect URI off this host is refused (403,
  `invalid_redirect_uri`), and one whose name is longer than 128 bytes
  (400, `invalid_client_metadata`). The client is bound to the first
  user who approves a flow for it: `test-member` gets a token through
  it, and then `test-analyst`'s approval is refused (403, `This client
  is registered to a different user`), and the flow still polls
  `authorization_pending`. Its configuration (`<issuer>/oidc-cm/<id>`,
  RFC 7592) refuses a GET, PUT, or DELETE with no registration access
  token or a wrong one (401). With the right one, a GET shows the
  client without its secret; a PUT renames it but leaves its grants as
  they were, though it asks for the code grant; a DELETE removes it
  (204), after which a GET with that token is 401. The issuer allows
  an address five registrations, then one a minute, across all its
  namespaces, and the `client` scenario and the `collections` suite
  register clients too, so `registration` comes last, sends three
  registrations, and if refused (429), waits a minute and tries once
  more. It does not test the limit itself.

The run's collection, share, rogue group, clients, and objects are
removed after the suite, and the registered client, if the scenario
could not delete it; the users and their groups stay.


## Accounts

A server keeps its users and groups in its database (Pelican's
`docs/user-group-design.md`), and decides from them who may act as
whom: whether an account is active, which scopes it holds, directly or
through a group that the configuration names, and which collections
are granted to its ID or its groups' IDs. The `accounts` suite checks
how those decisions follow an account through its life: deactivated,
deleted, granted scopes and stripped of them, added to authority groups
and removed, renamed, and recreated under a freed name. A mistake in
any of them is a privilege escalation, or a grant that outlives its
owner. The suite acts at the issuer origin (origin-0, or `fed`), needs
the embedded issuer and a protected namespace, as `collections` does,
and skips with the issuer off or the external one.

Every user and group the suite makes is the run's own, named
`acct-<role>-<run>`, so that deleting and recreating them touches no
other suite's. Each user agrees to the origin's acceptable use policy
as the admin creates them, and acts through a bearer token that the
origin's web API takes as theirs (`credentials.mint_user()`), or, where
a check is of a login, through the cookie of one, with a password of
the run's set through an invite, as the `issuer` suite's users do. Such
a token names its user, not their ID, so it stands for whoever bears
that name when it is presented. Tokens for data come from the
namespace's issuer through its device flow, approved as the user, with
a client the admin creates: the suite asks for every data scope, and
looks for a collection's `storage.read`. Collections are the admin's,
under `/<namespace>/data/accounts/<run>/`.

`framework/config.d/base/40-origin.yaml` configures the suite's
authorities, in every shape: `Server.AdminGroups` names
`test-server-admins`, `Server.CollectionAdminGroups` names
`test-collection-admins`, and `Server.UserAdminUsers` names
`test-user-admin`, whom the admin creates once and later runs reuse.
Each server reserves the two groups' names as it starts
(`EnsureConfiguredAuthorityGroups` in `database/authority_groups.go`),
so a federation started before they were configured must be restarted.
The configured groups match only the groups that a login's cookie
carries, those the user was in as they logged in
(`Issuer.GroupSource: internal`), so a check of a member's authority
logs them in again after each change of membership. The scenarios:

- `user-status`: a user logs in, and holds a refresh token from the
  device flow, approved with their cookie. The admin makes them
  inactive (`PUT /api/v1.0/users/:id/status`), then active again, then
  deletes them. While inactive, and once deleted, `GET /api/v1.0/me`
  with their cookie must be refused (401) and clear the cookie, and
  logging in again and their bearer token at `/me` must be refused
  (401) (`inactive-web` and `deleted-web`); and the issuer must approve
  no device flow with their cookie, send no code from its authorization
  page, and refuse their refresh token with `invalid_grant`
  (`inactive-issuer` and `deleted-issuer`). While active, before and
  after (`active` and `reactivated`), each works. The issuer's rows
  fail on v26.0.0-rc.3 and on Pelican's main: the issuer's middleware
  (in `launchers/origin_serve.go`) takes the cookie's identity without
  the check of the account that `AuthHandler` makes
  (`userRecordIsActive` in `web_ui/authentication.go`), and a refresh
  grants again without looking the user up (`handleToken` in
  `oauth2/issuer/handlers.go`). `deleted-web` fails there too: a bearer
  token whose subject names no live user passes `AuthHandler` with no
  user ID, and `/me` answers 500 (`callerID` in `web_ui/me.go`).
- `scope-grants`: the admin grants run users scopes
  (`POST /api/v1.0/users/:id/scopes`), which the server looks up on
  each request. With `server.user_admin`, a user may create users, but
  may not make the admin active or relabel them (403: a user
  administrator may not act on a server administrator), nor grant a
  scope to itself or the admin (403: granting is the admin's alone);
  each of these would change nothing if it got through. With
  `pelican.log_read`, a user may read the log tail
  (`/api/v1.0/logs/tail`), but not the configuration
  (`/api/v1.0/config`, 403). With `server.collection_admin`, a user may
  create a collection, and may make an API token of that scope
  (`POST /api/v1.0/tokens`, from their login cookie) with which to
  create one (`collection-admin-token`). Before each grant, and on the
  first request once `pelican.log_read` or `server.collection_admin` is
  revoked (`DELETE /api/v1.0/users/:id/scopes/:scope`), the user is
  refused (403), and so is the API token, since a token keeps a
  user-grantable scope only while its creator holds it
  (`api_token/api_token.go`). A data scope, `storage.read:/`, may not be
  granted (400).
- `authority-groups`: an ordinary user may not create a group of
  either configured name, nor may the admin delete one (409). A user
  may not create a collection until the admin adds them to
  `test-collection-admins` and they log in again, and may not once
  removed and logged in again. A user is `admin` in `/whoami` once in
  `test-server-admins` and logged in again, and `user` before and once
  removed. A login cookie from while the user was in
  `test-server-admins` must say `user` once they are removed
  (`stale-cookie`): a cookie keeps the groups it was minted with for
  its 16 hours, and `AuthHandler` asks the database about the user's
  status on each request, but not their groups, so a removed
  administrator keeps the role until their cookie expires, and the row
  fails, on v26.0.0-rc.3 and main alike. Whether a session should lose
  a group before it expires is not settled; the suite holds that an
  authority a group confers should go with the membership, as a
  deactivation takes effect at once. `test-user-admin` may create users
  and may not grant itself `server.admin` (403); `/whoami` gives them
  `server.user_admin` and the role `user`.
- `id-keyed-grants`: grants are kept by the IDs of users and groups,
  not their names (Pelican's issues #3752 and #3753). Once a group that
  may read a collection, and is its admin group, is deleted, the ACL
  lists nothing, the collection has no admin group, and a former
  member's next token lacks the read; a new group of the freed name,
  with the former member in it, gets none of it. Once a user who owns a
  collection, and may read it, is deleted, and a new user takes the
  name, the new user does not list the collection, may neither see it
  (404) nor delete it (404), and gets no read of it, and the ACL no
  longer lists the name. A renamed group (`PATCH /api/v1.0/groups/:id`,
  the admin's alone) and a renamed user (`PATCH /api/v1.0/users/:id`)
  keep their reads, which the ACL lists by their new names, and a new
  group and a new user of the names they left get nothing.
- `group-invites`: a group's owner makes invite links
  (`POST /api/v1.0/groups/:id/invites`). A single-use link joins the
  first user who redeems it (`POST /api/v1.0/invites/redeem`), and no
  other; a multi-use one joins each who does. A member who does not own
  the group may make none (403).
- `aup`: the admin puts up a new acceptable use policy
  (`PUT /api/v1.0/aup`). Until the admin agrees to it, the server
  refuses them its user routes (403, with `requires_aup` and the new
  version), agreeing to the old version does not do, and `/whoami`
  says `requires_aup` and the version; the policy is public
  (`GET /api/v1.0/aup` with no credentials). Once they agree, the user
  routes serve them. Another new policy gates them again, as it does a
  user who had agreed to the old one. The suite then puts back the
  policy it found. A policy's version is the hash of its text
  (`database/aup.go`), so that restores the version every user had
  agreed to, and the user's routes serve that user again without their
  agreeing anew (`restored`).

`aup` comes last, since for a moment it gates every user of the origin,
the admin included, who agrees again afterward. Should the policy not
be restored, every suite that uses the routes it gates agrees to the
AUP again as it starts.

The run's collections, groups, users, and clients are removed after
the suite, and those that earlier runs left. The server only marks a
deleted user or group deleted, and frees its name. `test-user-admin`
stays.


## OIDC login

Under `-p auth-oidc`, every server lets its users log in through an
identity provider (`Origin.EnableOIDC` and the other modules' flags),
and the registry has one prove who registers a prefix from the command
line. The provider is the `idp` service, an OpenID Connect provider of
the framework's own (`framework/idp/idp.py`), at `https://idp:9007`
(`OIDC.Issuer`), standing in for CILogon: its users, their passwords,
and their groups are `framework/etc/idp-users.json`, and its one client
is the ID and secret that `fed.sh` makes at random for the servers
(`framework/var/generated/oidc-client-{id,secret}`). It serves
discovery, a JWKS, the authorization code flow, the device flow, and
userinfo, signs its ID tokens itself (RS256, with a key it makes as it
starts), and logs every request in `framework/var/data/idp/requests.log`,
one JSON object per line, never a secret. `framework/testlib/oidc.py`
plays the browser.

A server begins a login by sending the user to the provider's
authorization endpoint (`/api/v1.0/auth/oauth/login`; `handleOAuthLogin`
in `web_ui/oauth2_client.go`), naming its client, its callback, and a
state, which it keeps in a session cookie too. The user logs in at the
provider, which sends them back to the callback with a code; the server
checks the state against its cookie, trades the code for tokens at the
provider, asks its userinfo endpoint who the user is, finds or makes its
record of them, and sets its login cookie (`handleOAuthCallback`). The
callback is on `OIDC.ClientRedirectHostname`, `localhost` and the
server's published port, for a browser on the host; the suite follows it
to the server's own host instead. The suite skips without the provider.
A scenario with several cases records a row per case, named `<scenario>
<case>`; the first four ask every server of the shape, by web URL.

- `enabled`: each server's `/api/v1.0/auth/oauth` must list each module
  it runs (`listOIDCEnabledServersHandler`), and its login route answer
  307 to the provider's authorization endpoint, asking for a code
  (`response_type`) for the servers' client, to be sent back to
  `https://localhost:<port>/api/v1.0/auth/oauth/callback`, with a state,
  and set the session cookie.
- `login`: `idp-member` logs in at each server. `whoami` must then name
  them by their `preferred_username`, as no admin (`role` `user`); a
  second login must find the same user; the server's admin must list one
  user of that name, whose record links to the provider's subject and
  issuer; and the provider's log must show the server trading each
  login's code at the token endpoint, as the servers' client, and
  asking userinfo who the user is.
- `callback`: at the issuer origin, a login's callback sent without its
  session cookie must be refused (400), as must one whose state is not
  the cookie's; with the cookie it must log the user in; and sent again,
  its code used, it must fail (the provider refuses the code), and log
  no one in.
- `collision`: `idp-collider`, whose `preferred_username` is `admin`,
  the built-in administrator's, logs in at the issuer origin. The server
  must walk to the next claim of `Server.AutoEnrollUsernameClaims`, the
  email, for the username (`LookupOrBootstrapUser` in
  `database/collection.go`), and give them no admin role, nor the
  admin's routes (403 for `GET /api/v1.0/users`).
- `registry-identity`: `pelican namespace register --with-identity` of
  a prefix of the run's. The registry starts the provider's device flow
  (`cliRegisterNamespace` in `registry/registry.go`) and hands the
  client the verification URL, which the suite approves at the
  provider as `idp-member`; the client polls the registry, which polls
  the provider's token endpoint, and once it has an access token asks
  userinfo who the user is, and registers the prefix with that identity.
  The registration must hold the client's key and `idp-member`'s
  identity (the provider's userinfo: their subject and
  `preferred_username`), and name the registry's user of that identity
  as its owner, whom `login` made. Then `pelican namespace delete` must
  remove it. It skips for a standalone origin, which has no registry.
- The last three restart origin-0 with `Server.UIAdminUsers` naming
  `idp-owner`, and `Issuer.GroupSource` `oidc`, in the tests' own layer
  ([service control](../README.md#service-control)), and restore it
  afterward. The shape itself keeps `internal`, since under `oidc` a
  password login carries no groups (`GroupsForLogin`), which the
  `issuer`, `collections`, and `accounts` suites need. They need a
  control helper, and skip in the `tiny` topology, whose origin is the
  whole federation's process; `groups` and `client` also need the
  embedded issuer and a protected namespace that takes direct writes,
  which hold their objects, in each group's project prefix
  (`/data/issuer/projects/<group>/`, where the templates grant the
  group), at every origin, removed afterward.
  - `admin`: with origin-0 restarted, `idp-owner` must be its admin by
    `whoami`, and may list its users; so must `idp-admin`, whose claimed
    group `test-server-admins` is in `Server.AdminGroups`
    (`RecordAssertedGroups`); `idp-member` must be no admin, and refused
    the users. Once restored, `idp-owner` must be no admin.
  - `groups`: through a client of origin-0's issuer that the admin
    creates, `idp-member`, logged in through the provider, must be
    granted by the device flow exactly what the templates give
    `test-project-a` (`framework/config.d/base/40-origin.yaml`): read and
    create in its project, and read in the archive; and the token must
    name the group (`wlcg.groups`). `idp-admin`, in `test-project-b`,
    likewise for that group alone; `idp-outsider`, in no group, must be
    granted no data scope, or refused. Then `idp-member`'s token must
    read `test-project-a`'s object straight at origin-0 and by `pelican
    object get` through the federation, and be refused
    `test-project-b`'s.
  - `client`: `pelican object get` of `test-project-a`'s object with no
    token: the client registers itself with the issuer and starts a
    device flow, which the suite approves as `idp-member`, logged in
    through the provider (as the `issuer` suite's `client` does with a
    password login); the client must then get the object.


## Advertisements

Every origin and cache tells the directors what it serves by posting an
advertisement, signed with its own key, which the registry holds for
its prefix (`/origins/<host>` or `/caches/<host>`); a director checks the
token against the registry's copy, and that it carries
`pelican.advertise` (`registerServerAd` in `director/director.go`,
`verifyAdvertiseToken` in `director/origin_api.go`). A cache trades the
same token for a federation token, which an origin that takes no direct
clients requires beside a user's; and an origin behind a firewall takes
its connections through the director's broker. The `advertise` suite
plays a caller who lies at each, and checks that each turns it away,
and serves the real thing. It skips for a standalone origin, which has
no director.

The suite signs as each server with the key of
`framework/var/issuer-keys/<host>/` that the registry holds for its
prefix. Some ads it posts are taken, as the server's own: each is built
from what a director says of the server
(`/api/v1.0/director_ui/servers/<name>`), lasts 90 seconds, and is
replaced by the server's next ad, within `Server.AdvertisementInterval`
(30 seconds here). After each scenario that changes where the directors
send clients, every director must name each server it touched again
within 150 seconds. The directors are asked about an object that the
suite puts at every origin, in the first protected namespace, at
`data/advertise/<run>-probe`, and removes afterward.

- `forged-ads`: origin-0's and the first cache's own ads, posted as
  each to every director, must be taken (200). Then variants, each
  changed in one way, must each be refused (403) by every director, and
  counted among the ads it refused
  (`pelican_director_rejected_advertisements`): with no token; signed
  by `unknown.pem`; with the server's key but not `pelican.advertise`;
  with `/protected-a` as its registry prefix; a cache's ad posted as an
  origin's (`registerOrigin`), with its own key; origin-0's ad with a
  namespace that no one registered, after which no director may name
  origin-0 for it; and under `-p topo-multi-owner`, origin-2's ad
  claiming `/protected-a`, after which no director may name origin-2
  for it. A director counts no ad that comes without a token
  (`registerServerAd` returns first), so the `no-token` rows fail.
- `bad-broker-url`: origin-0's own ad, but with a broker URL that Go's
  `url.Parse` refuses (`http://[::1`), must get 400, with one JSON body,
  and every director must then still list its servers and name origin-0.
  Pelican writes the 400 and goes on to read the URL it could not parse
  (`finishRegisterServeAd`), which panics; gin recovers, and the client
  sees only the 400.
- `spoofed-status`: with the first cache's own key, its ad saying that
  it is `shutting down`, and then `critical`, must take it out of every
  director's routing within 10 seconds, until its next ad. Under
  `-p topo-multi-cache`, the second cache's key and prefix may not do
  that to the first: an ad naming the first, at the second's URL,
  saying each status, or with a downtime under way, may be taken or
  refused, but must leave the first named, and unfiltered in the
  director's list of servers (`downtimeNameAuthorized`, v26.0.0-rc.0 and
  later). Nor may an ad at the first's URL, with its name, saying it is
  `critical`: a director keeps an ad by its URL, whoever signed it
  (`recordAd` in `director/cache_ads.go`), and keeps a critical cache
  out of routing (`cacheNotInErrorState` in `director/sort.go`), so the
  `url-hijack` row fails, on main and v26.0.0-rc.3 alike.
- `fed-token`: the first director must give the first cache a
  federation token for its own advertise token
  (`/api/v1.0/director/getFedToken?host=<host>&sType=Cache`): from the
  federation (`iss` its discovery URL), for the cache (`sub` its host),
  for any audience, with `storage.read:/`, lasting 900 seconds, and
  signed by a key of the federation's JWKS (but under
  `-p auth-external-issuer`, whose JWKS lacks the directors' keys, the
  signature is not checked). It must refuse (403) a request without the
  host, without the server type, or without a token; for a director;
  with the cache's key but not `pelican.advertise`; with the cache's
  token for another server (the second cache, or origin-0); and with a
  token signed by `unknown.pem`. Pelican answers a token it cannot
  verify with 500 (`getFedToken` in `director/fed_token.go`), so the
  last two rows fail. Under `-p registry-approval`, the registry's
  admin registers a cache's prefix of the run's, which stays pending,
  with a key made for it; a token signed with that key must be refused
  (403); then the registration is removed.
- `fed-token-at-origin`, under `-p origin-no-direct`: a native origin
  serves a request only with a federation token beside the user's
  (`authMiddleware` in `origin_serve/handlers.go`). A GET of the probe
  straight to each origin, with the `server` token and the first
  cache's federation token in one header, must get it; with the
  `server` token alone, 401; and with the federation token alone, which
  grants nothing in a protected namespace, a refusal (401 or 403).
- `discover-servers`: each director's list of servers for Prometheus
  (`/api/v1.0/director/discoverServers`) must be refused (403) with no
  token, and with one of its scope (`pelican.director_service_discovery`)
  from an issuer it does not trust; with one from the federation (its
  discovery URL), signed with a key of the federation's JWKS, it must
  list every origin and federated cache of the shape once, by its web
  URL's host and port, with its type, its name, its web URL, and a data
  URL on the same host, and nothing else.
- `broker`, under `-p origin-broker`: the director's broker hands an
  origin the requests to connect to it (`/api/v1.0/broker/retrieve`),
  with a token from the registry's issuer for the caller's prefix,
  signed with its key. As the first cache, a request for an origin that
  is not its own must be refused (401 or 403); Pelican checks the token
  against the prefix the caller names, but hands it the requests of
  whatever origin it names (`retrieveRequest` in
  `broker/server_apis.go`), so the row fails there. With no token, it
  must get 401; with a token signed by `unknown.pem`, a refusal (400,
  401, or 403); with a timeout that is not a duration, 400; and as
  origin-0, for a second, `{"status":"timeout"}`. A request to reverse a
  connection to origin-0's prefix (`/api/v1.0/broker/reverse`), with the
  cache's token, must be refused (400, 401, or 403). The origin these
  requests name is made up, so that none takes a request from
  origin-0's queue.


## Web API

Every Pelican server serves a web API under `/api/v1.0/` on its web
port, behind middleware of `web_ui/authentication.go`: `AuthHandler`
finds who is asking, from a bearer token of the server's own making or
a login's cookie, and `AdminAuthHandler`, `UserAdminAuthHandler`, and
`LogReadAuthHandler` decide whether they may. The `webapi` suite checks
those decisions at every server of the shape: each origin (origin-2
under `-p topo-multi-owner` too), cache (site-local ones too),
director, and the registry, each once by its web URL, so in the `tiny`
topology just `fed`.

It acts at each server as its admin, with a bearer token signed with
the server's own key, naming its local issuer, as `pelican cache
introspect` mints the admin's (`credentials.mint_user()`), or with the
cookie of a login as `admin`; and as `test-webapi`, an ordinary user,
whom the admin creates at each server once, and later runs reuse. Every
server enforces a built-in acceptable use policy unless
`Server.AUPFile` is `none`, and until a user agrees to it, refuses them
its routes for users and groups (`RequireAUPCompliance`), its admin
included; so the admin, and then `test-webapi`, first agree to the
server's current one (`POST /api/v1.0/me/aup`), as each would on first
logging in. A server allows an address one login a second
(`Server.UILoginRateLimit`), counted by whole seconds of the clock, so
the suite sends its logins to a server more than a second apart, but
where it means to exceed that, and leaves a second before the next
suite.

- `admin-matrix`: at every server, a GET of each admin route
  (`/config`, `/tokens`, `/logging/level`, `/logs/tail`,
  `/metrics/health`, `/debug/pprof/`, which `Server.EnablePprof` turns
  on here, and `/users`) with no credentials must get 401; with
  `test-webapi`'s token, 403; and as the admin, 200. `/metrics/health`
  is open to all where `Server.HealthMonitoringPublic` is set, which the
  suite reads from the server's configuration, as its admin. A server
  manages API tokens only through a login (`createApiToken` and
  `listApiTokens` read only the cookie), so `/tokens` is asked as the
  admin with their cookie, and must refuse their bearer token (403).
  `POST /restart`, `PATCH /config`, and `PUT /aup`, each with a body
  that would change nothing, are sent with no credentials and as the
  user only, which must get 401 and 403. Tokens that the server must
  take for no one, at `/config`: like the admin's, but signed by another
  server's key (one that differs from the server's own; `unknown.pem`
  where every server shares it), naming a namespace's issuer rather
  than the server's local one, or expired, must get 401. One for a
  subject of whom the server has no record may get 401 or 403: Pelican
  takes it for a user of no standing (`AuthHandler`), and refuses it
  with 403, and at `/users` with 401, since `RequireAUPCompliance` finds
  no user to ask about; either grants nothing. At the registry, its
  admin routes (`PATCH .../namespaces/<id>/approve` and `/deny`,
  `DELETE .../namespaces/<id>`, and `DELETE .../servers/<id>`), for a
  registration and a server that its admin first finds it does not hold
  (404), each with a CSRF token that it takes, so that only the
  credentials decide: no credentials must get 401, the user 403, and the
  admin 404; but the registry deletes a server without asking whether it
  exists (`deleteServerHandler`), so there 200 is accepted too. These
  are the only admin routes the suite sends as the admin that would
  change anything, were the IDs real.
- `api-tokens`, at the first director, logged in as its admin: a POST
  to `/api/v1.0/tokens` for a token of
  `pelican.director_service_discovery` must get 200 or 201 and the
  token, `<ID>.<secret>`; with it, the director's service discovery
  (`/api/v1.0/director/discoverServers`) must list every origin and
  federated cache, as `advertise`'s `discover-servers` checks it. The
  list of tokens must show it, by ID and name, and neither its secret
  nor any hash of it. The token with another secret, a well-formed
  token that never was, the token once deleted (which must get 200 or
  204), and a token made to expire in 10 seconds, two seconds after it
  does, must each be refused with 403, not a server error; the last
  must work before it expires. A POST for a token with the admin's
  bearer token instead of the cookie must get 403. A token may carry a
  scope that a user may be granted only if its creator holds it
  (`validateScopesForCreator`), and the admin holds no
  `pelican.transfer` (`server.admin` implies only `server.user_admin`
  and `server.collection_admin`), so a POST for a token of it must be
  refused (400 or 403), with no token. Pelican refuses it, but answers
  200, the status of its check of the cookie (`createApiToken` in
  `web_ui/ui.go`), so the `unheld-scope` row fails, on main and
  v26.0.0-rc.3 alike. Every token made is deleted afterward. It skips
  for a standalone origin.
- `login`, at every server: `POST /api/v1.0/auth/login` (form fields
  `user` and `password`) as `admin`/`asdf` must get 200, and a `login`
  cookie that is `HttpOnly`, `Secure`, `SameSite=Lax`, for `Path=/`,
  with `Max-Age=57600` (16 hours); with a wrong password, 401; and
  without the password, 400. `/whoami` with no credentials must say no
  one is logged in (`authenticated: false`), and hand out no
  `X-CSRF-Token`; with the admin's cookie, the role `admin`; and with
  `test-webapi`'s token, the role `user`. A logout with the cookie must
  get 200, and clear it (empty, and already expired); and the cookie,
  presented again, must then count as none (401 at `/config`), as a
  logout must end the session it logs out of (OWASP ASVS 3.3.1).
  Pelican's login cookie is a token that the server checks by its
  signature and expiry alone, and its logout only asks the browser to
  drop it, so the `logout-replay` rows fail, on main and v26.0.0-rc.3
  alike. Last, two logins at once, the second with `X-Forwarded-For`
  naming another address, which a server that trusts no proxy
  (`Server.TrustedProxies`) must not believe: the first must get 200,
  and the second 429.
- `csrf`: the registry guards each route of its web UI's API with
  gorilla/csrf, which refuses a request that changes anything unless its
  `X-CSRF-Token` matches its `_gorilla_csrf` cookie (both from
  `/whoami`). As the registry's admin, by cookie and by bearer token, a
  PATCH approving a registration that does not exist must get 403
  (`CSRF token invalid`) with no token, and with one from another
  `/whoami` that does not match the cookie; gorilla/csrf exempts no
  bearer token. With a matching one, it must get 404. At every server, a
  logout (an `AuthHandler` route) with the admin's cookie and
  `Origin: https://evil.invalid` must get 403 (`isSameOriginRequest`);
  with their bearer token from there, and with the cookie and no
  `Origin`, 200, since a browser sends no bearer token of its own
  accord, nor a cross-site request without an `Origin`.
- `malformed-path`: at every server, a GET of `/%C0%AF` and of
  `/api/v1.0/%C0%AF`, an overlong encoding of `/` that is not UTF-8,
  must get 400 (`rejectInvalidPathMiddleware`), not a server error; then
  `/api/v1.0/version` must still get 200.
- `version`: every server's `/api/v1.0/version` must report the same
  `version`, `PELICAN_TAG`'s without the `v`, where the shape runs its
  images (`fed.sh` writes it in `framework/var/generated/pelican-version`,
  or `custom` where any `IMAGE_*` the shape uses is the user's own,
  when the row only reports what each server says). Each director's
  responses (`director_ui/servers`), and the registry's
  (`registry_ui/namespaces`), must carry `Server: pelican/<version>`;
  and each director must list each server of the shape it knows with
  the version that server reports.


## Observability

Each server reports on itself: its health, by component; its metrics,
at `/metrics`, which its own Prometheus scrapes, as a director scrapes
every origin and cache; its log level, which its admin may raise for a
while; and the newest lines of its log. Operators and the federation's
own monitoring rely on these. The `observability` suite checks them at
every server of the shape, each once by its web URL, as the `webapi`
suite does, and acts as each server's admin and as `test-webapi`, as
that suite does ([web API](#web-api)). Each case is a row, named
`<scenario> <server> <case>`.

Pelican guards each server's `/metrics` (`Monitoring.MetricAuthorization`)
and its Prometheus query API (`Monitoring.PromQLAuthorization`) by
default, and so does the framework. Every suite that reads `/metrics`
presents a token of the server's own making, from its local issuer,
signed with its own key, with scope `monitoring.scrape`, as the server
scrapes itself (`framework/testlib/metrics.py`).

- `health-api`: each server's health report
  (`/api/v1.0/metrics/health`; `GetHealthStatus` in
  `metrics/health.go`).
  - `components`: as its admin reads it, the report must say `ok`
    overall, and of each component. It must list `web-ui`; at an
    origin, and a cache that the directors know of, `federation`,
    `director`, and `registry`; and where XRootD serves an origin or a
    cache, `xrootd` and `config-updates`. A standalone origin must list
    none of `federation`, `director`, or `registry`, since it talks to
    no director or registry (`standalone_origin_cli_e2e_test.go`). A
    server says `warning` of its advertisements and of the directors'
    tests until the first of each, so the suite asks every 5 seconds,
    for up to 150, until every report is all `ok`, or none has changed
    for 35 seconds and none says a status that may yet change by
    itself. The `storage` component warns once the host's disks are
    80% full (`Monitoring.StorageWarningThreshold`), so a report that
    fails by it alone is inconclusive. A native origin serves no
    `/pelican/monitoring`, so the directors' tests of a `posixv2` or
    `pstore` origin fail, and it says `director` is critical, and its
    row fails, on v26.0.0-rc.3 and main alike
    ([director APIs](#director-apis)).
  - `access`: with no credentials, the report must get 401; as
    `test-webapi`, 403; and as the admin, 200; but where
    `Server.HealthMonitoringPublic` is on, which the suite reads from
    the server's configuration, 200 for all. `/api/v1.0/health` must
    answer anyone with 200.
- `health-metrics`: each server's `pelican_component_health_status`
  must agree with its health report. The suite reads the report, then
  `/metrics`, then the report again; each component whose status both
  reports give must have the gauge of that status's value
  (`HealthStatusEnum`: `ok` is 5, `warning` 4, `critical` 2). And no
  gauge may be of a component that neither report lists, since an
  operator's alerts read the gauges. Pelican drops a component from the
  report but leaves its gauge: an XRootD origin says `cmsd` is
  `warning` as it starts, then, with no `Xrootd.ManagerHost`, drops it
  (`launcher_utils/xrootd_servers.go`), so the gauge stays at 4, and
  the origin's row fails under `-p origin-xrootd`, on v26.0.0-rc.3 and
  main alike. This comes from reading the code, not from a run.
- `metric-authorization`: `promMetricAuthHandler` and
  `promQueryEngineAuthHandler` (`web_ui/authorization.go`).
  - `metrics`: at each server, `/metrics` with no token must get 401
    or 403 (Pelican answers 403); with a token of the server's own
    local issuer with scope `monitoring.scrape`, 200, and the
    exposition; with the same token signed by another key (another
    server's, or `unknown.pem` where every server shares one), 401 or
    403; with the admin's login cookie, which carries the monitoring
    scopes (`setLoginCookie`), 200; and with the admin's bearer token,
    which carries only `web_ui.access`, 403.
  - `promql`: at each server, a query of its Prometheus
    (`/api/v1.0/prometheus/query`) with no token must get 401 or 403; with a
    token of its local issuer with scope `monitoring.query`, 200, and a
    successful query; with the admin's login cookie, 200; and with a
    token of scope `monitoring.scrape` alone, 403.
  - `scrapes`: within 60 seconds, each server's Prometheus must find
    its scrape of itself up (`up{job="prometheus"}` is 1), which needs
    the token the server mints for it (`createPromMetricToken`) to pass
    its own guard; and each director's, its scrape of every origin and
    federated cache up (`up{job="origin_cache_servers"}`, by each
    server's type and web host), which needs the token it mints from
    the federation's issuer (`configDirectorPromScraper`) to pass
    theirs.
- `http-metrics`, at the first origin, where Pelican serves it (not
  XRootD), in the first protected namespace that takes direct writes
  and reads, or, where only `/public` does (`-p origin-public` with
  `-p origin-public-writes`), in `/public`, where `refused` then skips,
  since its reads take no token to refuse
  (`httpMetricsMiddleware` in `origin_serve/handlers.go`). Straight to
  the origin, it sends a PUT of 64 KiB, a GET of it, a GET of an object
  that does not exist, and a GET with a token signed by `unknown.pem`,
  the first three with a token for a subject of the run's own
  (`observability-<run>`), which no other request has, so that the
  counters of that `username` must rise by exactly these. Under
  `-p origin-multiuser`, whose mapfile knows only the tests' usual
  subject, it uses that one, and the counters must rise by at least
  these. It writes under `data/observability/<run>/`, and removes it
  afterward.
  - `put`: the PUT must get 201, and raise
    `pelican_http_requests_total` with `server_type="origin"`,
    `method="PUT"`, and `code="201"` by one, and
    `pelican_http_bytes_total` with `direction="in"` by at least 64
    KiB.
  - `get`: the GET must get the bytes, and raise the requests counter
    with `method="GET"` and `code="200"` by one, and the bytes counter
    with `direction="out"` by at least 64 KiB.
  - `missing`: the GET of nothing must get 404, and raise the requests
    counter with `code="404"` by one.
  - `refused`: the unknown key's GET must get 401 or 403, and raise the
    requests counter with that code. The middleware counts a request
    before the origin's authorization, so a refusal counts too, with no
    `username`.

  Where the origin's storage goes through Pelican's afero adapter
  (`posixv2` and `pstore`; `origin_serve/filesystem.go`), `put` must
  also raise `pelican_storage_writes_total`, and `get`
  `pelican_storage_reads_total`, for that `username`.
- `log-level`: each server's log level, which its admin may change for
  a while (`/api/v1.0/logging/level`; `web_ui/logging_api.go` and
  `logging/level_manager.go`).
  - `change`: at every server at once, a POST of a more verbose level
    than the server's (`debug`, or `trace` from `debug`) for 20 seconds
    must get 200 and a `changeId`; the level must then be the one asked
    for, with the change listed.
  - `expiry`: within 40 seconds of its deadline, each change must be
    gone, and the level back to what it was. The server drops a change
    at its deadline.
  - `delete`: a change for 10 minutes, deleted by its ID, must get 200,
    and deleted again, 404; and the level must be back.
  - `refusals`: as the admin, a level that is none, a duration over 24
    hours or of none, a parameter that is no setting
    (`Logging.NoSuchLevel`), one that is no level
    (`Logging.Buffer.MaxSize`, asked for a second, which the server
    reads only as it starts), and one outside `Logging`, must each get
    400; a good change from
    `test-webapi`, 403; and from no one, 401. Pelican takes any setting
    under `Logging` (`isValidLoggingParameter`), so the `not a level`
    case gets 200, and every server's `refusals` row fails, on
    v26.0.0-rc.3 and main alike. This comes from reading the code, not
    from a run.
  - `cli`, at the first server: `pelican-server server
    set-logging-level <level> 20s --server <web URL> --token <file>`,
    with the admin's token, must print a change's ID, which the API
    must then list. It skips without a `pelican-server`.
- `log-redaction`: at every origin and cache. A server keeps its newest
  log lines in memory, and serves them to its admin, and to a user
  granted `pelican.log_read` (`/api/v1.0/logs/tail`;
  `web_ui/log_buffer_api.go`).
  - `tail-access`: the tail with no credentials must get 401; as
    `test-webapi`, 403; and as the admin, 200.
  - `redaction`: with the server's log level raised to `debug`, the
    suite presents a fresh token to it, as a client might: in the
    `Authorization` header and in the query (`?authz=Bearer%20<token>`),
    at each of its data URLs, for `/<ns>/data/0.0` of the first
    namespace that takes tokens; and as a `login` cookie, at its web
    API. Pelican replaces a token's signature with `REDACTED` in a
    line's message and `url` field before the line reaches the tail
    (`redactEntryCopy` in `config/logging.go`). The tail since the
    suite began must hold neither the token nor its signature. The
    note says how often the token appeared, redacted. If a request went
    unanswered, or the tail dropped lines the suite had not read, the
    row is inconclusive.
- `log-level-xrootd`, at the first origin, where XRootD serves the
  origins, and at the first federated XRootD cache: XRootD's own log
  levels (`Logging.Origin.Xrootd` and `Logging.Cache.Xrootd`) take a
  restart of XRootD, which Pelican makes when one changes
  (`handleXrootdLoggingChange` in `xrootd/logging_callback.go`), after
  draining for `Xrootd.ShutdownTimeout`, which the framework sets to 2
  seconds.
  - `short`: a change for no longer than the drain must get 400, since
    it would revert before XRootD restarted to take it.
  - `restart`: a change for 5 minutes past the drain must get 200, with
    `requiresRestart` and `effectiveAt`. XRootD must then restart:
    the object must go unanswered, or the health report say `xrootd` is
    not `ok`; and then, within 120 seconds, the object must be answered
    as before, and `xrootd` be `ok`. The note says how long the object
    went unanswered. It is the one way the tests can restart XRootD
    without restarting its server.
  - `named`: within 90 seconds, every director must name the server for
    the object again. A server tells the directors it is shutting down
    as XRootD restarts, and advertises again once it serves.
  - `restored`: deleting the change must get 200, and restart XRootD
    again, after which the same must hold.

The suite raises each server's log level for a minute at most, and
deletes every change it made when it ends.


## Restarts

Production servers restart and fail all the time. The `restart` suite
takes servers away in the middle of a run, through the control helper
([service control](../README.md#service-control)), and checks what
survives and what the rest of the federation does meanwhile; it puts
each back, even after a failure, and comes after every other suite but
`keys`. It needs a control helper, and skips the tiny topology, whose
servers are one process. Its objects go under
`/protected-a/data/restart/<run>/`, put at every origin, and are
removed afterward. A scenario with several cases records a row per
case, named `<scenario> <case>`, or `<scenario> <service> <case>`. The
`transfer-api` suite's `restart` covers a transfer job interrupted by a
restart, and `freshness`'s `stale-if-error` a stale copy served while
every origin is stopped.

- `cache-restart`: each cache, the site-local one included, holds three
  1 MiB objects read whole (a V2 cache says so with `Age`). Restarted,
  and once every director names it again (within 150 seconds, timed in
  the note), it must serve each from its store, intact, with `Age` for
  a V2 cache, and the native origins must have sent less than an
  object's worth of bytes for GETs meanwhile (`pelican_http_bytes_total`;
  `selftest`'s `restart-cache` does the same for cache-0 and one
  object). An XRootD cache sends no `Age`, so there the origins'
  counters alone tell; over XRootD origins, which keep none, the bytes
  alone must be right.
- `state-survives`: before origin-0, director-0, and the registry
  restart, the suite records each origin's and cache's ID and name, as
  it records them itself (`/api/v1.0/server/localMetadata`), and the
  servers the registry lists; and at the issuer origin, with the
  origins' own issuer (the `issuer` row and the rest below skip with
  the external one), it makes a user, a group, an API token of the
  `monitoring.scrape` scope, a downtime declared for tomorrow, a client
  of the namespace issuer's, and a refresh token through it. Each
  restart must end with every director naming origin-0, or for
  director-0 every server, again within 150 seconds (`restarts`).
  Afterward each server's ID and name must be as
  recorded (`ids`); the registry must list the same servers, none twice
  (`registrations`); the user and the group must be there, with their
  IDs (`accounts`); the API token must still read `/metrics` and be
  listed (`api-token`); the downtime must still be declared
  (`downtime`); and the refresh token must refresh, with the client's
  secret (`issuer`): the issuer signs its tokens with a key it derives
  from a master key in its database (`oauth2/issuer/provider.go`).
  What the suite made is removed afterward.
- `ad-expiry`: the first federated cache, and with several origins the
  last origin, killed (SIGKILL, so it says nothing to the directors):
  within `Server.AdLifetime` (90 seconds here) and 30 more, no director
  may name it, since its ad has expired (the directors keep ads in a
  cache with that TTL, `director/cache_ads.go`; under
  `-p topo-multi-director`, a peer's forwarded ad must not keep it
  alive); started again, every director must name it within 150
  seconds. The note times both.
- `backend-outage`, where the origins' storage is a service of the
  framework's (`s3` under `-p origin-s3v2`, `webdav` under
  `-p origin-httpsv2`, `lab-server` under `-p origin-ssh`): with it
  stopped, at origin-0, where it takes direct clients, a GET and a HEAD
  of an object, and a PUT of a new one, must each get a server error
  (5xx), never 404 or 405, nor the object's bytes (`direct`); through
  the first federated cache, a GET of an object it does not hold must
  get a server error too, not 404 (`cache`); and an `ssh` origin's
  health must say its `ssh-backend` is not ok within 60 seconds
  (`health`; the other backends have no component). Once the service
  is back, the origin must serve the object again within 120 seconds,
  without a restart, and its HEAD, a PUT, and the cache's read must
  work (`recovered`), and the health component say ok. An `httpsv2`
  origin whose `OPTIONS` probe of its backend fails takes it for a
  plain HTTP server, and so answers a missing backend as missing
  objects (O12; `probeBackendMode` in `origin_serve/backend_https.go`),
  so its `direct` and `cache` rows are expected to fail; this comes
  from reading the code, not from a run. It skips where the storage is
  the origin's own, or XRootD's.
- `cache-failover`, with several federated caches: the first is paused
  (its ports take connections that nothing answers), and then killed
  (they refuse). Each time, `pelican object get` and stash_plugin of
  an object of their own through the federation must succeed, served
  by another cache, while the directors still name the first (`early`;
  inconclusive if they had already dropped it), and once they have
  dropped it, within `Server.AdLifetime` and 30 more (`late`). The note
  says which caches each client tried: the client probes each
  endpoint for a second before it downloads, so a cache that answers
  nothing may be tried last rather than fail. Afterward the cache is
  unpaused, or started, and every director must name it again within
  150 seconds (`back`).
- `peer-director`, with several directors: the second killed, the first
  must list only itself at `/api/v1.0/director/directors` within
  `Server.AdLifetime` and 30 more, since its peer's ad has expired
  (`director/director_advertise.go`); once the second starts again,
  the first must list both within 150 seconds. The note times both.


## Keys

Each server signs with the first of the keys in its directory, and
publishes them all ([issuer keys](configuration.md#issuer-keys)).
`smoke.sh` rotates every key in some shapes, but restarts everything
before it tests again. The `keys` suite changes keys while the servers
run, as `./fed.sh keys add` and `keys rotate` do, writing into
`framework/var/issuer-keys/` from the dev container, and puts them back:
a key it added is removed, and one it made the signer is renamed to
sort last, so it stays as a verify-only key, since a V2 cache must keep
the keys that sealed its store. It comes last of all: a rotation takes a
server out of the directors' routing until the keys are back, and the
servers keep what they cached of a namespace's keys for up to 15
minutes. It skips under `-p server-unprivileged`, whose servers' keys
the host may not write, and the tiny topology, whose one process holds
every key. Its probe object goes under `/protected-a/data/keys/<run>/`,
and is removed afterward. A scenario with several cases records a row
per case, named `<scenario> <case>`.

- `hot-add`: a verify-only key, written into the origins' key
  directories (origin-0's and origin-1's, which share a key) so that it
  sorts last: within 120 seconds, origin-0's own JWKS must list it
  (`origin-jwks`), and so must the registry's for `/protected-a`
  (`registry-namespace`) and for origin-0's own prefix
  (`registry-server`), since an origin rereads its key directory every
  minute and pushes its keys to the registry when they change, signing
  the update with a key the registry already holds
  (`LaunchIssuerKeysDirRefresh` in
  `launcher_utils/key_refresh_and_update_namespace_pubkey.go`,
  `updateNamespacesPubKey` in `registry/registry_pubkey_update.go`); the
  note times each. Then the key is removed, and each must be rid of it
  within 120 seconds (`removed`). No director may drop origin-0
  meanwhile (`routing`). It skips for a standalone origin, which
  registers nothing.
- `hot-rotate`: a new signing key on the origins, and another on the
  first federated V2 cache, each written to sort first, and watched for
  3 minutes, every 15 seconds. Every director must go on naming
  origin-0 and the cache (`directors`): a director checks an ad's
  token against the keys it fetched from the registry for the server's
  prefix up to `Director.AdvertisementTTL` (15 minutes) before, and
  never refetches for a kid it lacks (`verifyAdvertiseToken` in
  `director/origin_api.go`), so once a server's ads carry its new kid
  the directors refuse them, its last accepted ad expires, and it drops
  out of routing (D5), and the rows fail, on v26.0.0-rc.3 and main
  alike. Each origin that takes direct clients, and each federated
  cache, must come to take tokens signed with the origins' new key, and
  the note says how soon (`new-key`); they keep a namespace's keys for
  up to 15 minutes (`origin_serve/authz.go`,
  `local_cache/cache_authz.go`), which the window may not outlast, and
  the row is then inconclusive; each must take the old key's tokens
  throughout (`old-key`). Under `-p auth-external-issuer`, the exports
  trust none of the origins' keys, so the token rows skip. The
  issuer's tokens must come to carry the new kid (`issuer-kid`).
  The registry must come to list the origins'
  new key (`registry-origins`), and the cache's (`registry-cache`): a
  cache reconciles its keys with the registry only as it starts
  (`RegisterNamespaceWithRetry` in `launcher_utils/register_namespace.go`),
  never when they change (R4), so that row fails, on v26.0.0-rc.3 and
  main alike. Afterward both new keys are demoted, and every director
  must name origin-0 again.
- `master-key`: a V2 cache seals its master key, with which its store
  is encrypted, with each of its keys, in `masterkey.json` under its
  store (`local_cache/encryption.go`). At the first federated V2 cache,
  given a verify-only key (one the suite adds, if `hot-rotate` left
  none), the file must be sealed with every key before a restart
  (`sealed`) and after one (`sealed-after-restart`): Pelican re-seals
  it only through a key-change callback that the launcher registers for
  its local cache module, never for the cache server
  (`launchers/launcher.go`, `launchers/cache_serve.go`), and loading the
  file never re-saves it, so both rows fail, on v26.0.0-rc.3 and main
  alike (C4). Then, destructively, with every key but the newest
  removed from the cache's key directory, the cache restarted must
  still open its store, and serve the probe it held, from its store,
  with `Age`, the origins sending nothing (`retired`): a master key it
  cannot unseal is replaced with a new one, with which its store cannot
  be read, so the cache fails to start, and the row fails. Afterward the
  keys and the master key file are put back, and the cache restarted
  again; should that not bring it back, the note says to run
  `./reset.sh`. It needs a control helper.
- `backups`: each server (every origin, cache, and director, and the
  registry) keeps encrypted backups of its database under
  `framework/var/state/<service>/backups/` (`Server.DatabaseBackup`;
  `config.d/base/20-server.yaml`), one from startup on, since a server
  with none makes one as it starts (`LaunchPeriodicBackup` in
  `database/backup.go`). There must be at least one; each must be PEM,
  never a plain SQLite file; and `pelican-server server database backup
  verify` must open each, given a copy of the server's keys
  (`PELICAN_ISSUERKEYSDIRECTORY`), after the suite's key changes too.
  Without a `pelican-server` ([client binary](../README.md#client-binary)),
  the backups are checked but not opened.


## What is not tested

Shapes that Pelican offers and this framework does not, and why:

- `globus` and `globusv2` origins, which need Globus credentials;
  `s3v2` in front of GCS or Azure (`Origin.ObjectProviderURL`), which
  need theirs; and the `xroot` origin, which fronts another XRootD
  server.
- Of an `ssh` origin: logging in to the storage host with a password,
  a keyboard-interactive prompt, or an agent (`Origin.SSH.AuthMethods`),
  or through a jump host (`Origin.SSH.ProxyJump`); the lab server takes
  a key alone. Both of its connection modes run: the helper connecting
  back to the origin by default, and the origin dialing the helper
  through the SSH connection under `-p origin-ssh-tunnel`.
- Of an export that anyone may read and a token may write
  (`-p origin-public-writes`), a key of its own beside the origins'
  (`IssuerJwks`): `/public` trusts the origins' keys alone, so the
  `ns-jwks`, `ns-other`, and `ns-cross` credentials stay the protected
  namespaces' ([authorization](#authorization)).
- Of an origin of public data alone (`-p origin-public`), the data
  path beyond the clients' reads and every credential's: the suites
  that write, or need `/protected-a`, skip there, and say so.
- A server behind a reverse proxy (`Server.TrustedProxies`), and a
  server on port 443, which Pelican leaves out of the URLs it derives
  (`Server.ExternalWebUrl`): every server here listens on 8444. The
  directors' load balancer (`-p director-ha`) passes TLS through, so
  the directors see its address as every client's.
- X.509 client certificates at XRootD servers
  (`Origin.EnableTLSClientAuth` and `Origin.EnableVoms`, and the
  caches' own), whose authorization Pelican leaves to the
  administrator.
- A cache serving a second federation (`Cache.AllowedFederations`),
  which would need one; the `cache-api` suite checks only that a cache
  refuses a federation not its own.
- Of `pelican-server serve --module`, a director and registry sharing
  a process, or an origin and a cache, as a site might run them:
  `topo-tiny` runs every module in one process, and the other shapes
  each in a process of its own.
- A federation of mixed versions, as one mid-upgrade is: every shape
  runs one release's images (`PELICAN_TAG`), so the directors never
  meet a cache too old for a feature an origin requires
  (`requiredFeatures` in its ad, which `Origin.DisableDirectClients`
  fills with `CacheAuthz`, from v7.16; `features/` in Pelican), which
  they would route around. `IMAGE_*` can mix releases by hand
  ([Testing your changes](../README.md#testing-your-changes)).
- The XRootD monitoring shoveler (`Shoveler.Enable`), which forwards
  the XRootD servers' UDP monitoring stream to a message bus, or to
  collectors of its own (`Shoveler.OutputDestinations`); the
  `observability` suite reads each server's metrics instead
  ([observability](#observability)).
- Sorting by GeoIP, which needs a MaxMind database, and the adaptive
  sort's status and I/O load weights, which change too slowly to
  observe; the `routing` suite sorts by declared locations
  ([routing](#routing)).
- Whether a cache keeps only the latest of the directors' test files,
  which a director asks it to evict: a V2 cache's store is not laid out
  by path, and an XRootD cache's eviction only marks a file for its
  next purge (`HandleDirectorEvictRequest` in `cache/cache_api.go`);
  the `director-api` suite reads the test file through each cache
  ([director APIs](#director-apis)).
- `Director.CachesPullFromCaches`, which applies only to a request
  carrying `?prefercached`, which no Pelican cache or client sends.
- Lotman (`Cache.EnableLotman`), the XRootD cache's quotas; the
  `localcache` module, a client-side cache over a Unix socket; and
  `Client.FileRedirectRoots`, by which a cache sends a client that
  shares its filesystem to a file instead of bytes.
- A cache over several storage directories, as one with several disks
  is: a V2 cache's `LocalCache.StorageDirs`, across which it stripes
  objects larger than `LocalCache.ChunkSize` and evicts by each
  directory's own bound, and an XRootD cache's `Cache.DataLocations`
  and `Cache.MetaLocations`; every cache here stores under `/data`
  alone, and the `blocks` and `cache-api` suites measure that store.
- Of `pelican-server origin pstore`, which needs the origin stopped:
  `fsck --repair`, a `metadata-backup` taken by hand, and a restore
  opened with an escrowed backup key (`metadata-backup-key`,
  `--key-file`, and `--file-key`) rather than the origin's issuer keys;
  the `pstore` suite restores a scheduled backup with those keys, and
  exports from it ([pstore](#pstore)).
- `pelican cache evict`, `purge`, `introspect`, and `chaos`, which read
  the cache's own configuration and address file, and so run only
  beside it; `cache-api` calls the APIs they call
  ([cache APIs](#cache-apis)).
- Of the embedded issuer: groups from a file (`Issuer.GroupSource`
  `file`), which the suite's users get from the server's own database,
  and the `oidc` suite's from the identity provider, for a while. Also
  `pelican.transfer` granted
  through a scope grant (`Transfer.EnabledGroups` the `transfer-api`
  suite checks); and the limit on registrations, five from an address,
  then one a minute.
- Of the registry: the
  OSDF topology's namespaces, `Registry.InstitutionsUrl`, and the
  cleanup of inactive registrations ([registry](#registry)); the `keys`
  suite covers the keys a server adds or rotates while it runs, and
  `restart` server IDs across a restart. Also its refusal of an edit
  whose registration changed while the edit was being processed
  (409, `errRegistrationModified`), which only a race with another
  write brings about.
- An XRootD server that stops answering connections, which Pelican's
  main probes for (`Xrootd.LivenessCheck*`), reports as the
  `xrootd-liveness` health component, and shuts down: the framework can
  only pause or kill a whole container, not wedge the XRootD inside
  one; the `observability` suite reads the component with the rest.
- Of what a server reports on itself: a level asked of a setting under
  `Logging` that is no string, which, by Pelican's code,
  the log level API takes, and which would leave every later change of
  the server's configuration failing until it restarts
  (`param.MultiSet`); the
  `observability` suite asks it of a string that the server reads only
  as it starts ([observability](#observability)).
- Of accounts: `Server.UserAdminGroups`; scopes granted to a group;
  onboarding invites; linked identities; and clearing a user's password
  or their agreement to the AUP ([accounts](#accounts)).
- Of the transfer API: a finished job that the manager drops from
  memory on its own, five minutes after it ends (the `restart` case of
  the `transfer-api` suite drops it with a restart instead); the
  authorization-code bootstrap of a credential, whose callback needs a
  browser; and a job interrupted while its source is another server's
  ([transfer API](#transfer-api)).
- Of the clients: the order of the client's tries among servers it
  probes, which it ranks by their answers; `osdf://` and `stash://`
  URLs, whose discovery is hard-coded to osg-htc.org
  ([client contracts](#client-contracts)); `?pack`, by which `pelican
  object get` and `put` move a collection as one archive, which the
  native origin does not serve, and which no suite asks an XRootD
  origin for; and of the client agent, a job cancelled while it runs,
  and the agent's wallet, which `pelican client-agent warm` fills with
  tokens acquired interactively ([client contracts](#client-contracts)).
- A site's HTTP proxy, which the client sends its transfers through
  when `http_proxy` or its kin is set (`Client.DisableHttpProxy` turns
  that off), trying without it when it fails, unless
  `Client.DisableProxyFallback` says not to; nothing here sets one.
- Of a transfer server (`-p topo-transfer-server`): its web API, health,
  metrics, restarts, and keys, which the `webapi`, `observability`,
  `restart`, and `keys` suites check only of the federation's origins,
  caches, directors, and registry; the `transfer-api` suite checks its
  API, its local issuer's grants, and its restart with a job under way
  ([transfer API](#transfer-api)).
- Reloading a server's certificate without a restart: the servers mount
  the certificate files as Docker secrets, which a file written anew on
  the host does not replace, so a reissued certificate needs a restart
  here ([keys](#keys)).
- Of logging in through an identity provider (`-p auth-oidc`): GitHub
  as the provider (`Issuer.GroupSource` `github`, whose organizations
  are the groups), CILogon's own claims, and any provider but the
  framework's own; `Issuer.OIDCAuthenticationRequirements`, which only
  the retired OA4MP issuer read; `Issuer.UserStripDomain`; a login at
  a transfer server (`-p topo-transfer-server`); and the browser's own
  part, which the tests play ([OIDC login](#oidc-login)).
- `with-grafana`, which needs a data source the framework lacks
  ([Configuration](configuration.md#presets)).
