# What is not tested

Part of [What the tests check](README.md).

Two kinds of gap: features of Pelican that no preset offers, and what a
preset implies about the handling of data that the tests don't
exercise. What a preset itself limits, such as the namespaces it
exports or the suites that skip under it, is in its row of the
[README's preset tables](../../README.md#presets).

## Not offered as presets

Features that would seem natural presets, and why none is offered:

- **Globus origins** (`globus` and `globusv2`), **`s3v2` in front of
  GCS or Azure** (`Origin.ObjectProviderURL`), and **the caches'
  tiering to either** (`Cache.TieringTargets`): each needs the
  provider's own service and credentials, which nothing in Docker
  stands in for.
- **The `xroot` origin**, which relays to another XRootD server through
  XRootD's own proxy, as a bridge from the OSDF's legacy origins:
  Pelican's part is the configuration it writes, and the data path is
  XRootD's.
- **The OSDF's servers** (`osdf-server`, in Pelican's `osdf-*` images),
  which differ from Pelican's by their defaults (`osdf_default` in
  `docs/parameters.yaml`): those point at the OSDF's discovery host,
  legacy services, and OSG Topology, which only the OSDF serves.
- **A local cache on the clients' host** (`pelican-server serve --module localcache`): the V2 cache's own code in its client-side
  mode (`LocalCache.*`), which joins no federation and listens on a
  Unix socket, which a client names (`--cache unix:///...`). It would
  need a service beside `dev` that shares the socket, and a suite of
  its own, as cache-2 has (`topo-site-local-cache`); the shapes run
  the same code as a federated or a site-local cache, over HTTPS.
- **A cache serving a second federation** (`Cache.AllowedFederations`),
  which would need one, with a director, registry, and discovery host
  of its own; the `cache-api` suite checks that a cache refuses a
  federation not its own.
- **Modules sharing a process**, as a site might run a director with
  its registry, or an origin with a cache (`pelican-server serve --module ...`): `topo-tiny` runs every module in one process, and
  the other shapes each in a process of its own.
- **A broker in a server of its own** (`--module broker`): the
  directors here run one, as Pelican's do by default
  (`Director.EnableBroker`), and `origin-broker` uses director-0's; one
  apart from the directors would change only the host that the
  federation's metadata names.
- **Mixed releases.** Every shape runs one release (`PELICAN_TAG`) from
  scratch, so none is a federation mid-upgrade, and no release reads
  the databases or stores that an older one wrote. `IMAGE_*` can mix
  releases by hand
  ([Testing your changes](../../README.md#testing-your-changes)).
- **Origins of mixed kinds behind one namespace**, the origins'
  counterpart of `cache-mixed`: beside `topo-multi-origin`, it would
  add only replicas whose ETags disagree, as XRootD replicas' already
  do, and every kind of origin meets every kind of cache in some shape.
- **A cache over several storage directories** (a V2 cache's
  `LocalCache.StorageDirs`, an XRootD cache's `Cache.DataLocations`),
  across which it stripes objects and evicts by each directory's bound:
  every cache here stores under `/data` alone.
- **Lotman** (`Cache.EnableLotman`): the XRootD cache's lots,
  allocations of its space by namespace, each with an expiry and a
  place in the purge order, and an API of their own
  (`Lotman.EnableAPI`). It would need a suite of its own; the V2 cache
  has no lots, and the caches here are bounded as a whole
  (`cache-small`), and evict as Pelican does by default.
- **The XRootD monitoring shoveler** (`Shoveler.Enable`), which forwards
  the XRootD servers' UDP monitoring stream to a message bus that a
  federation's operators run: the stream is XRootD's, and the
  `observability` suite reads each server's own metrics instead.
- **X.509 and VOMS at XRootD servers** (`Origin.EnableTLSClientAuth`,
  `Origin.EnableVoms`, and the caches' own), **and macaroons**
  (`Origin.EnableMacaroons`): no Pelican client presents either, and
  what a certificate may do is XRootD's authorization, which Pelican
  leaves to the administrator.
- **Sorting by GeoIP** (`Director.GeoIPLocation`), which needs MaxMind's
  database, and clients at public addresses, which Docker's are not:
  the servers declare their locations (`GeoLocation`), and the
  `routing` suite sorts by those.
- **A server behind a reverse proxy** (`Server.TrustedProxies`), **or
  on port 443**, which Pelican leaves out of the URLs it derives: every
  server here listens on 8444, and `director-ha`'s load balancer passes
  TLS through.
- **A cache behind a firewall** (`Cache.EnableBroker`, which
  `origin-broker` turns off at the caches): the director would reach it
  through its broker, for its health tests, but no client outside the
  site could, so it would serve only the site's own clients, which
  nothing here stands in for; `origin-firewalled` puts an origin there.
- **XRootD clustering** (`Origin.EnableCmsd` and `Xrootd.ManagerHost`,
  by which an XRootD origin joins a redirector, as the OSDF's origins
  join its legacy ones): the manager is an XRootD redirector that no
  Pelican server runs, and the directors send clients to an origin by
  its own URL, so the path through a redirector is XRootD's.
- **A certificate chain** (`Server.TLSCertificateChain` holding an
  intermediate CA's certificate, as a public CA issues): Go's and
  XRootD's TLS both read the chain from the file, and Pelican's part is
  naming it, which every server does alike; the framework's CA signs
  each certificate directly.
- **Directors that route to a cache in an error state**
  (`Director.FilterCachesInErrorState` off): it would hide what a
  server's `critical` health status does to its routing, which
  `advertise`'s `spoofed-status` checks.
- **An XRootD cache's read-ahead** (`Cache.BlocksToPrefetch`, off by
  default): it changes when a cache fetches blocks, not what it serves,
  which the `blocks` suite reads at the block boundaries either way.
- **A server without its web UI** (`Server.EnableUI` off): only the
  pages go; its API, login, and issuer stay, which the `webapi` suite
  checks.
- **Virtual-host-style S3 URLs** (`Origin.S3UrlStyle` `virtual`), by
  which an origin asks for `<bucket>.<service>/<object>`: the form is
  the AWS SDK's to build, and Pelican passes the style through; the
  `s3` service serves path style, at `<service>/<bucket>/<object>`.

## Not exercised

What a preset implies about the handling of data that the tests don't
exercise, or not in a way that would catch a fault:

- `cache-v2` and `origin-pstore`: the bytes in the store on disk. Both
  encrypt each object's blocks with a key of the object's own, which
  they keep, sealed by the server's master key, in a BadgerDB catalog,
  so only Pelican can read them back. The tests compare what such a
  server serves with the bytes they wrote, read a pstore origin's
  uploads back through it, and check the sizes of a V2 cache's files on
  disk; the bytes that an XRootD cache, or any other origin, keeps on
  disk they read there ([block boundaries](block-boundaries.md)).
- `topo-tiny`: nothing confirms that a client's download came through
  the cache, since the `transfers` suite's `served-by` checks skip.
- `topo-site-local-cache`: of cache-2, only its reads, a refusal, a
  listing, and what it leaves in a federated cache (`sitelocal`); the
  suites that check a cache's own APIs, the freshness of its copies, or
  its block boundaries ask only the federated caches.
- `origin-pstore`: of `pelican-server origin pstore`, which runs with
  the origin stopped, `fsck --repair`, and a restore opened with an
  escrowed key (`metadata-backup-key`) rather than the origin's issuer
  keys ([pstore](pstore.md)).
- `origin-https` and `origin-httpsv2`: a token for the backend from an
  OAuth2 issuer by client credentials (`Origin.HttpAuthOAuth2Issuer`,
  `ClientID`, and `ClientSecretFile`); the `exports` suite has the
  origins present a static token, and the client's own
  ([exports and copies](exports-and-copies.md)).
- `cache-mixed`: the `transfers` suite's named-cache scenarios name
  cache-0, a V2 cache, so the XRootD cache-1 serves them only when a
  director picks it; and `client`'s `failover` crosses from V2 to
  XRootD alone.
- `cache-tiered`: nothing checks that a cache drops its copy of a
  tiered object, or serves one after a restart or an eviction.
- `cache-memory`: nothing confirms that a read came from memory, or
  that the setting took effect.
- `origin-metadata`: nothing checks that an upload cut short, or
  refused, publishes no event; the `posc` and `metadata` suites never
  cross.
- `origin-metadata-tx`: the rollback of an upload whose event fails is
  checked only for a new object, not for an overwrite, which, by
  Pelican's design, may leave no version at all.
- `origin-multiuser`: a read through a cache, as the token's user: the
  `users` suite asks the origins directly, and the other suites'
  objects are readable by every user.
- `origin-broker`: no data goes through the broker. The origin stays
  reachable, so the caches and the clients connect to it directly, and
  only the director's own requests, chiefly its health tests, are
  brokered. `origin-firewalled` cuts the origin off from the caches,
  whose fetches must then go through it, which in v26.0.0-rc.3 they
  never do, since the XRootD cache never asks it; a client's never go
  through it either, since the dev container is on the origin's
  network, as a site's own hosts are.
- `origin-ssh-tunnel`: nothing tells the tunnel from the default mode;
  the lab server can still reach the origin, so an origin that ignored
  the setting would pass every check.
- `origin-short-tokens`: no transfer outlives its token, and no cache is
  shown an expired one.
- `origin-export-templates`: only the scopes that the issuer grants; no
  read presents such a token.
- `auth-external-issuer`: a token of the origins' own keys or issuers,
  which the exports no longer trust; and a token of the trusted key
  that names another issuer.
- `auth-no-issuer`: a token of the origin's key that names another
  issuer; every namespace has the one issuer, so `wrong-iss` skips.
- `director-ha`: no read or write through the load balancer with a
  director down; `restart`'s `peer-director` kills one, and checks only
  the other's list of directors.
- `registry-approval`: no request for a pending server's data; whether
  one serves is judged by what the directors name.
