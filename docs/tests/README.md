# What the Tests Check

`./fed.sh test` runs `test.py` in the dev container. It runs every
basic functionality check that applies to the federation's shape, over
every combination of client, route, namespace, credential, server, and
[token profile](#token-profiles) that the shape has. The checks are
grouped in suites (`framework/suites/`). The table below says what each
one checks, and links to a page on each; see the
[README](../../README.md#quickstart) for how to run them.


## Suites

The suites run in this order:

| Suite        | Tests                                                                 |
| ------------ | --------------------------------------------------------------------- |
| `federation` | that the federation serves at all, and every director names every federated cache and every origin, and for a client's direct read (`?directread`) only origins that take direct clients, and with no cache (`-p topo-no-cache`) an origin in a cache's place; what its redirects tell a client of a namespace, and where they lead a plain HTTP client, bare paths included ([redirects](redirects.md)); a Pelican server's discovery document; under `-p origin-broker`, the connection broker; with several origins, that an object one of them alone holds is routed there, and comes back; under `-p registry-approval`, that nothing is routed to before its registration is approved; and that a server its admin declares down is routed to by no director until the downtime ends ([downtime](downtime.md)) |
| `selftest`   | the tests' own helpers ([self-tests](self-tests.md)): reading a V2 cache's `X-Transfer-Status` trailer, signing tokens of any claims and checking signatures, reading `/metrics`, and restarting a service mid-run |
| `commands`   | `ls`, `stat`, `delete`, `copy`, `sync`, and `du`, and what `get` and `put` do with what already exists ([client commands](client-commands.md)) |
| `client`     | the contracts that only show through the clients: stash_plugin's result ads, failing over between caches partway through a download, the ETags the servers agree on, which token the client presents, how `pelican token create` finds an issuer, share URLs, the director's recorded decision, direct reads with `?skipstat`, several sources at once, a preferred cache that is down, and transfers handed to the client agent (`--async`) ([client contracts](client-contracts.md)) |
| `transfer-api` | the transfer API, origin-0's under `-p origin-transfer-api`, or a transfer server's own under `-p topo-transfer-server`: authentication, credentials, server-run copies, jobs, and confinement to the exports, which only origin-0's has; and its edges: subjects that are no usernames, a finished job cancelled, the event stream, sources on its own network, `pelican transfer credential`, `Transfer.EnabledGroups`, OAuth2 clients and a bootstrapped credential, and a restart with a job under way ([transfer API](transfer-api.md)) |
| `listings`   | PROPFIND at each origin, director, and cache, of collections and of a plain object; a GET of a collection; and the links of an origin's HTML listing ([listings](listings.md)) |
| `names`      | objects whose [names](names.md) a URL or a listing must encode, beside decoys |
| `blocks`     | objects and ranges at pstore's and the caches' [block boundaries](block-boundaries.md), through each server, each director's redirect, and both clients; an object past 4 GiB, whose length and ranges across 2 GiB and 4 GiB every server must get right; digests, asked for one at a time, by default, several at once, or by q-values, and an upload the client must verify by one; conditional reads and writes, at their edges, and the `ETag` that each method must report alike; objects overwritten, given up on, corrupted in a cache's store, or written through a cache; the `X-Transfer-Status` trailer, by which a server reports a failure after its headers; the origins' digests that a V2 cache relays, and an object that fails them; that a cache fetches an object read by many clients at once from the origins once; and, under `-p cache-small`, objects evicted from a cache |
| `freshness` | how long the caches keep their copies, by the `Cache-Control` the origins send, which it restarts them to vary: `only-if-cached`, 404s, what a V2 cache tells its clients, `no-store` and `private`, `max-age=0`, `no-cache`, `must-revalidate`, and `s-maxage`, and stale copies while the origins are down ([cache freshness](cache-freshness.md)) |
| `cache-api` | a V2 cache's own APIs: prestaging and evicting objects, by whoever may read or modify them, and through the client; purges and introspection, which only its admin may use; that it serves no other federation; its usage accounting; and its consistency checker ([cache APIs](cache-apis.md)) |
| `throttling` | how the servers and clients behave with the origins slowed, which it restarts them to be: a V2 cache's 429s when it sheds a burst, `X-Pelican-Timeout`, and streaming a cold object; the clients' errors for a shed attempt and for a transfer too slow ([throttling](throttling.md)) |
| `exports`    | what a native origin does with each export's capabilities, which it restarts the origins to vary: `Listings`, `DirectReads`, and copies (`Origin.DisableCopies`); a third-party COPY's progress, its failures, and what a failed one leaves; a multipart PUT with metadata off; and, at an `https` or `httpsv2` origin, what it sends a backend that takes a token, which it restarts the origins to see: a static token on every request, or the client's own ([exports and copies](exports-and-copies.md)) |
| `pstore`     | a pstore origin's own features, under `-p origin-pstore`: its storage API and `pelican-server origin introspect`, which only its admin may use; its scheduled index checks and data scans, and the reclamation of what is replaced; refusing writes once full, and backing up its catalog, which it restarts the first origin to check; and a backup restored into its store offline, with the origin stopped, which must give back every object it held, byte for byte ([pstore](pstore.md)); and elsewhere, that a native origin has no storage API |
| `routing`    | how the directors choose where to send a client, and what they tell it: the order of the caches and of the origins by declared location, at random, by adaptive weights, and with a server filtered out; an object no origin holds; what `Location` and `Link` carry; tokens about to expire; their refusals and their status codes; and response hostnames; under `-p topo-multi-cache`, it restarts the directors to vary how they sort, and a cache restricted to a namespace (`Cache.PermittedNamespaces`) ([routing](routing.md)) |
| `director-api` | what the directors publish of the federation, against the shape: their lists of servers and of namespaces, and the namespace a path falls in; their health tests of each origin and cache, as they and the servers report them, and the test file they serve the caches; which origins hold an object, for their admin alone; their metrics; their comparison of the federation's metadata; and under `-p topo-multi-director`, the ads one forwards to another ([director APIs](director-apis.md)) |
| `registry`   | the registry, the federation's root of trust: registering and deleting prefixes, the prefixes it refuses, key chaining, a prefix with a LIKE wildcard, and `pelican namespace`; the keys it serves for each prefix, and no private key in any JWKS the federation serves; its web API for listing, approving, and denying registrations, and its servers; completing a server's registration with the server's key; who owns a registration: claiming one, an owner's edits of an approved one, a denied one resubmitted, and handing one on with an invite; each server's ID; and downtimes recorded at the registry, whose they are, and what the directors do with them ([registry](registry.md)) |
| `tiering`    | V2 caches' tiering of large objects to object storage, under `-p cache-tiered`: what a plain HTTP client gets from the cache, and through a director, and what both clients get, from the cache named and through the director; including a copy replaced in the bucket |
| `posc`       | interrupted and racing uploads, under `-p origin-posc`, `-p origin-atomic-uploads`, or `-p origin-pstore`; and the native POSC's reaping of abandoned staging files, which it restarts the first origin to hasten ([POSC and metadata](posc-and-metadata.md)) |
| `metadata`   | metadata events, under `-p origin-metadata`: the URLs by which an uploader follows or cancels its event; the events that a MOVE, a COPY, and a collection's DELETE send, and those the reconcile sweep must not; the first origin's admin APIs over its record of objects and its publish queue, and the CLI over the record; and refusing uploads while a namespace's queue is full; which it restarts the first origin to check; with publishing off, that the queue's admin API says so ([POSC and metadata](posc-and-metadata.md)) |
| `users`      | which Unix user the origins act as, under `-p origin-multiuser` or `-p server-unprivileged`: who owns what they write, and in which group; under multiuser, reads with no token, what a user learns of another's files, POSC's staging, users below `Origin.MultiuserMinID`, and the mapfile's rules, a username claim, and a mapfile edited while the origins run ([Unix users](unix-users.md)) |
| `owners`     | origin-2, another owner, under `-p topo-multi-owner` ([another owner](another-owner.md)) |
| `sitelocal`  | cache-2, a site-local cache, under `-p topo-site-local-cache` |
| `collections` | collections: prefixes with access control of their own, granted through the origins' embedded issuer ([collections](collections.md)) |
| `issuer`     | the origins' embedded issuer: what it grants of what a client asks for, through each flow, and its refresh tokens; and its boundaries: each namespace's issuer apart from another's, clients that register themselves, its verification page's CSRF tokens, its admin API for clients, cross-origin requests, PKCE, and codes' expiry ([issuer](issuer.md)) |
| `accounts`   | the life cycle of a server's users and groups, and who may act as whom because of it: deactivated and deleted users, at the web API and the embedded issuer; scopes granted to a user, and authority from configured groups; grants that go with a deleted user or group, and stay with a renamed one; group invites; and a new acceptable use policy ([accounts](accounts.md)) |
| `oidc`       | logging in through an identity provider, under `-p auth-oidc`: what each server does with a user the provider vouches for, by the code flow a browser follows; the username and groups it takes from the provider's claims, and the admin and the issuer's grants they earn; the registry's proof of a registrant by the provider's device flow; and the Pelican client acquiring a token approved by such a user ([OIDC login](oidc-login.md)) |
| `advertise`  | what the directors take from servers, and from anyone posing as one: forged and spoofed advertisements, federation tokens, service discovery, and the connection broker ([advertisements](advertisements.md)) |
| `webapi`     | who may use each server's web API: its admin routes, with no credentials, as an ordinary user, as the admin, and with tokens it must take for no one; a director's API tokens; logging in and out, and the login cookie; CSRF tokens and cross-origin requests; malformed paths; and the version each server reports ([web API](web-api.md)) |
| `observability` | what each server reports about itself, and who may see it: its health report, and the gauges that must agree with it; who may read its metrics and query its Prometheus, and its scrapes and the directors'; a native origin's request, byte, and storage counters; its log level, raised for a while through its admin API; tokens kept out of its log; and under XRootD, an XRootD log level, which restarts XRootD ([observability](observability.md)) |
| `webdav`     | the origins' WebDAV methods beyond GET, HEAD, PUT, DELETE, and PROPFIND, and how each is authorized: POST, COPY, MOVE, MKCOL, LOCK, PROPPATCH, and OPTIONS; writes through symbolic links that leave a store; how an origin serves stored HTML; and that a cache serves those methods for no one, and changes no origin ([WebDAV methods](webdav-methods.md)) |
| `auth`       | each [credential](authorization.md) at each server, in each namespace; each issuer's keys; tokens of one storage scope, for another audience or one origin's, in the query string, and several at once; tokens whose times, claims, or signatures are wrong, one that expires while in use, and a server's own web tokens ([token validation](token-validation.md)); paths and symbolic links that climb out of a namespace or a token's scope; a protected prefix inside a public one; and what a refusal tells a client |
| `transfers`  | gets and puts through `pelican object` and `stash_plugin`: gets through the cache the director picks, direct from an origin, and from a cache the client names, byte-checked ([test data](test-data.md)) |
| `restart`    | what survives a server's restart, and what a server does while another is gone: a cache's store, every server's identity and database, the directors' routing once a server is killed and once it is back, an origin whose storage service is stopped, the clients' failover from a cache that is paused or killed, and a director's view of its peer ([restarts](restarts.md)) |
| `keys`       | keys added and rotated while the servers run: the registry's and the origins' JWKS, the directors' routing, tokens of either key at every server, the issuer's kid, a V2 cache's master key, and every server's database backups ([keys](keys.md)) |

A suite that doesn't apply to the shape is skipped, and says why.
`federation`'s `ready` and `caches` checks run first, whatever else is
asked for; `caches` tries each cache 20 times, 15 seconds apart, while
it fails or doesn't answer. If the federation never serves, or no cache
does, nothing else runs. A V2 cache of v26.0.0-rc.3 or main fetches
each object through a `pelican://` URL of its director's host, and
looks for the federation's metadata there, which a director serves
only as the discovery host (`Director.EnableFederationMetadataHosting`,
which `framework/config.d/director/default/` turns off, as the OSDF's
directors need not serve it): so in the default shape every V2 cache
answers 500 ("Federation metadata discovery failed with HTTP status
410"), `caches` fails, and the run stops there (C16). Where the
director is the discovery host (`-p director-discovery`, `topo-tiny`),
a v26.0.0-rc.3 V2 cache serves, but advertises itself `critical`, since
`pelican cache serve` marks its `xrootd` component critical as it
starts and the V2 cache never clears it, so no director names it, and
`federation`'s `directors`, `redirects`, and `downtime` fail, as does
every row that reads through a cache's director route (C17; main's V2
cache is clean). The framework works around neither. The
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

These are functional checks, not stress tests: each moves a few
objects, the largest about 16 MiB, but for `blocks`'s `cache-evicted`,
which under `-p cache-small` moves about 200 MiB through each cache, to
make it evict. Each suite writes a row per test case to
`framework/var/results/` ([Results](../ci.md#results)). A scenario that
raises an unexpected error fails, with the error as its note, and the
suite goes on to the next. A client command still running after 600
seconds is killed, and fails. Each suite removes what it wrote from
every origin's store afterward, a `pstore` origin's included.

[What is not tested](what-is-not-tested.md) lists the shapes and features
that Pelican offers and this framework does not, and why.


## Token profiles

Pelican understands tokens of two profiles, whose scopes differ (its
docs/app/advanced-concepts/auth/): the WLCG common JWT profile's
`storage.read`, `storage.create`, and `storage.modify`, which it
prefers, and SciTokens 2.0's `read` and `write`. So each suite runs
twice. Its first pass presents WLCG tokens; its second, reported as
`<suite>-scitokens`, presents SciTokens in their place, each as
`pelican token create --profile scitokens2` mints it, with the
profile's version claim (`ver`) and "any" audience (`ANY`). A token's
scopes in the second pass are what the same `token create` flags grant:
`read:` for `--read`, and `write:` for `--write` and `--modify` alike,
since SciTokens' `write` may create, replace, and remove an object. Each
row of the SciTokens pass must fare as its twin in the WLCG pass does.

What a scenario presents is of its pass's profile. Its setup, such as
putting objects in place or filling a cache, and its checks of what the
stores hold, use WLCG tokens in either pass, so that the SciTokens pass
tests only what the scenario presents. A scenario that presents no
storage token of the tests' own minting runs in the WLCG pass alone:
one that presents none, or only tokens that Pelican mints itself, in
the WLCG profile alone (its web API's, its issuer's, or a server's own),
as every scenario of `registry`, `collections`, `issuer`, `accounts`,
`oidc`, and `webapi` does. So does `auth`'s `create-only`, since
SciTokens has no scope of create alone; and so does every scenario of
`keys`, since a second pass would change the keys again while the
servers still hold what the first left: its `hot-rotate` presents both
profiles' tokens in its one rotation instead, in rows whose names end
in `scitokens`. `./fed.sh test -l` marks each such scenario `WLCG only`,
and `./fed.sh test --profile wlcg` (or `scitokens`) runs one pass alone.
`federation`'s `ready` and `caches`, once they have passed, try once in
the second pass, since the federation is no longer starting; the second
pass repeats every other suite's restarts.

Pelican's native origins and V2 caches compare a token's scopes with
the WLCG profile's alone (`ParseResourceScopeString` in
`token_scopes/token_scope_utils.go`, and `authorize` in
`origin_serve/authz.go` and `local_cache/cache_authz.go`), so they grant
a SciToken nothing, though the native origin takes the SciTokens `ANY`
audience as its own; XRootD maps the scopes of both
(`XrdSciTokensAccess.cc`). So, on main and v26.0.0-rc.3 alike, the
SciTokens pass fails wherever a native origin or V2 cache must take a
token, which in a shape of native servers is nearly every row (T6);
where XRootD serves the origins and caches (`-p origin-xrootd
-p cache-xrootd`), the passes should fare alike.
