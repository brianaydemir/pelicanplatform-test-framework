# Federation

Part of [What the tests check](README.md).

The `federation` suite checks that the federation serves at all, and
that its servers know of each other: whom each director names, and
what its redirects tell a client and where they lead. It probes with
an object of 4 KiB of random bytes, which it puts at every origin, in
each exported namespace, at `/<ns>/data/federation/<run>/probe`:
through the origin where the namespace takes writes, and otherwise
straight into its store on disk (on a pstore origin, `/public`'s is
`/protected-a`'s, whose storage it reads). Where a scenario asks of one
namespace, it is the probe's: the first protected namespace exported,
or `/public` where the origins export only that (`-p origin-public`).
The suite removes earlier runs' objects from the stores on disk first,
and the run's from every origin's store afterward, a pstore origin's
included. Its `ready` and `caches` scenarios are `REQUIRED`: `test.py`
runs them first, before any other suite, whatever it is asked for, and
stops the run if the federation never serves, or no cache does, or
either raises an unexpected error, since nothing else could pass.
Another scenario that raises an unexpected error fails, with the error
as its note, and the rest still run. Each scenario is a row.
`./fed.sh test -l federation` lists the scenarios.

- `ready`: the object must come back through the federation, through a
  director and a cache, by `pelican object get` of the federation's
  URL, byte for byte: in `/public` with no token, where it is exported,
  and otherwise in the first protected namespace with its `server`
  token. Servers just started take a while to find each other, so it
  tries 20 times, 15 seconds apart; each try first puts the probes in
  place, until that has succeeded, and under `-p registry-approval`
  first approves what is pending (`approval`). If no try passes, the
  run stops, and asks whether the federation is up and initialized for
  the shape.
- `caches`: each cache, a site-local one included, must serve the
  object in the probe's namespace, asked straight with the namespace's
  `server` token: 200, and the object's bytes. A new cache refuses a
  namespace's tokens (403) until it has learned the namespace's issuers
  from the director, and may fail otherwise, or not answer, while it
  finds the origins, so the suite tries each cache 20 times, 15 seconds
  apart, while it fails or doesn't answer. If no cache serves, the run
  stops; if some do, the row fails, and the run goes on. It skips a
  shape with no cache.
- `approval`: under `-p registry-approval`, nothing serves until an
  administrator approves its registration, so before each of its tries
  `ready` approves every pending registration (Incomplete or Pending)
  through the registry's admin API, as its built-in `admin`
  ([What the tests check](README.md#suites)). Before approving, it asks
  each director where it would send a GET of the object, for a cache
  and for an origin: `approval` fails if any named a server whose
  registration was still pending, since the directors refuse such a
  server's advertisements, or if listing or approving the registrations
  failed. Where nothing was pending, as on a rerun after an earlier run
  approved everything, it has nothing to judge, and is `INCONCLUSIVE`.
  It skips elsewhere.
- `directors`: each director is asked where it would send a GET of the
  object, with no token (`/api/v1.0/director/object/<path>` or
  `/origin/<path>`), and must name each server by its redirect or in
  its `Link` header. For a cache (`object`), it must name every
  federated cache; for an origin (`origin`), every origin, whether or
  not the namespace takes direct clients, since the caches reach the
  origins by that route too; and for a client's direct read (`origin`
  with `?directread`), the one ask for which it checks `DirectReads`
  (`originSupportsQuery()` in `director/sort.go`), every origin where
  the namespace takes direct clients, and elsewhere it must find none
  (405), and name none. No answer may name a site-local cache
  (`-p topo-site-local-cache`), which the directors do not know of.
  Where the federation has no cache (`-p topo-no-cache`), the `object`
  route must fall back to an origin (307) where the namespace takes
  direct clients, and otherwise find none (404), as for a plain HTTP
  client ([redirects](redirects.md)). All of it must hold at once
  within 60 seconds, asked every 5. It skips a standalone origin, which
  has no director, and the `tiny` topology, whose director, cache, and
  origin are one server.
- `discovery`: where a Pelican server is the federation's discovery
  host, its `/.well-known/pelican-configuration` must answer 200 with
  JSON whose `discovery_endpoint`, `director_endpoint`,
  `namespace_registration_endpoint`, and `jwks_uri` are as `fed.sh` has
  them (`framework/var/generated/discovery.json`; one that is empty
  there may be missing), and its `jwks_uri` must serve a JWKS holding
  one of the server's own keys, by which the federation's tokens are
  checked. The discovery host is director-0 under
  `-p director-discovery` (`Director.EnableFederationMetadataHosting`;
  `federationDiscoveryHandler` in `director/discovery.go`), or the
  directors' load balancer under `-p director-ha` too, whose keys are
  director-0's; the `tiny` topology's one server; or a standalone
  origin (`RegisterStandaloneFederationMetadata` in
  `origin/standalone.go`). It skips elsewhere, where the `discovery`
  service serves `fed.sh`'s own document. The `director-api` suite's
  `metadata-hosting` checks which directors serve one
  ([director APIs](director-apis.md)).
- `broker`: under `-p origin-broker`, origin-0 advertises director-0's
  connection broker (`Origin.EnableBroker`), and `fed.sh` keeps the
  caches from advertising one (`Cache.EnableBroker`). Each director's
  answer for an origin's read of the object, with no token, must name
  it in its `X-Pelican-Broker` header (`generateXBrokerHeader` in
  `director/director.go`): the discovery document's `broker_endpoint`
  (its scheme, host, and port), at `/api/v1.0/broker/reverse`, with
  `origin` origin-0's host and `prefix` the probe's namespace, as the
  origin builds it (`SetBrokerURL` in `server_utils/server_utils.go`).
  Each director's list of servers (`/api/v1.0/director_ui/servers`;
  `listServers` in `director/director_ui.go`) must give each origin
  the same `brokerUrl`, and a cache none. All of it must hold within
  60 seconds, asked every 5. Behind a firewall (`-p origin-firewalled`),
  where origin-0 is on a network the caches are not on, every fetch of
  theirs comes through the broker: `caches` has just had each cache
  fetch the probe, so origin-0's `pelican_broker_connections_total`
  (`server_type="origin"`) and `pelican_broker_object_requests_total`
  (`metrics/server.go`, counted in `origin/broker_client.go`) must each
  be at least 1, and the note says what it counted. In v26.0.0-rc.3
  (xrdcl-pelican 1.8.2), the XRootD cache is told of the broker, but
  reports its refused connection to the origin and never asks the
  broker for one, so `ready` fails there and the run stops; the check
  is for a release that brokers a cache's fetch. It skips elsewhere.
- `hints`: each director's redirect (307) of a GET of the object, with
  no token, for a cache and for an origin, in each exported namespace,
  must tell the client the namespace and whether it takes a token
  (`X-Pelican-Namespace`), and name its issuer alone, where it has one,
  and otherwise none (`X-Pelican-Authorization`;
  `X-Pelican-Token-Generation`, if it names one, names the same), by
  which the client picks a token. It skips a standalone origin;
  [authorization](authorization.md) has the `auth` suite's check of the
  same headers at a cache.
- `redirects`: a plain HTTP client, such as curl, follows each
  director's redirects (and under `-p director-ha`, the load
  balancer's) in each exported namespace: a GET and a HEAD of the bare
  path must land at a cache, or under `-p director-default-origin` at
  an origin; one with `?directread` at an origin, or where the
  namespace takes no direct clients find none (405); and the object
  and origin routes at a cache and an origin. What lands at a cache
  must be served, and at an origin only where the namespace takes
  direct clients; a bare-path PUT must be stored at an origin where the
  namespace takes writes, and nowhere where it takes none. It skips a
  standalone origin; [redirects](redirects.md) has the rest, a shape
  with no cache among it.
- `presence`: with two or more origins of stores of their own (those
  that share one, as the lab server is shared under `-p origin-ssh`,
  count once), the suite puts an object of 8 KiB of random bytes at the
  last of them alone, at `data/federation/<run>/only-<origin>`, in each
  exported namespace whose storage there is its own (not `/public` on a
  pstore origin); if it can't, the row fails. A director asks the
  origins which holds an object before it sends a read to one
  (`Director.CheckOriginPresence`, on by default), so within 60
  seconds, asked every 5, each director's answer for an origin's read
  of it, plain, as a cache asks, and for a client's direct read
  (`?directread`), must name that origin and no other; where the
  namespace takes no direct clients, the direct read must find none
  (405), and name none. A director asks the origins only with the
  namespace's token, where its reads take one (`shouldStatOrigins` in
  `director/stat.go`), so each ask presents its `server` token (none in
  `/public`), and with it each cache must serve the object. Then
  `pelican object get` must fetch it, byte for byte, by both of the
  client's routes: through a cache, and with `--direct` where the
  namespace takes direct clients. It skips a shape of one origin, or of
  origins that share a store. A director asks the origins before a GET
  or a HEAD, but not before a PROPFIND, which it sends to any origin
  (`shouldStatOrigins` in `director/stat.go`), and an XRootD cache stats
  an object by PROPFIND before it opens it, so an XRootD cache answers
  404 (`-p cache-xrootd`, or cache-1 under `-p cache-mixed`), and the row
  fails there, on main.
- `downtime`: a server's admin declares it down through its web API,
  as before maintenance: every director must name it before, none
  within 150 seconds, and every director again as soon once the
  downtime is ended; the last federated cache, and with several
  origins, the last origin. It skips a standalone origin, the `tiny`
  topology, and a shape with no federated cache and one origin; see
  [downtime](downtime.md).

`approval`, `directors`, `discovery`, `broker`, `hints`, and `downtime`
present no storage token of the tests' own minting, and run in the WLCG
pass alone ([token profiles](README.md#token-profiles)). `ready`,
`caches`, `redirects`, and `presence` run in both, presenting the
pass's tokens, though their objects are put in place with WLCG tokens
either way. Once `ready` or `caches` has passed, it tries once in a
later pass, since the federation is no longer starting, and fails
rather than stop the run. In a pass of SciTokens alone
(`--profile scitokens`), each judges whether the federation serves at
all with a WLCG token as well: a federation that serves it but refuses
the pass's tokens, as Pelican's native servers do, is up, so the row
fails, and the run goes on.

[What the tests check](README.md#suites) says where Pelican as it is
fails `caches`, which stops the run, and `directors`, `redirects`, and
`downtime`.
