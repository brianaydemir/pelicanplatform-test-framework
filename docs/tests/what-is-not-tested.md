# What Is Not Tested

Part of [What the tests check](README.md).

Two kinds of gap: features of [Pelican](https://pelicanplatform.org/) that
[no preset offers](#not-offered-as-presets), and what a preset implies that
[the tests don't exercise](#not-exercised). What a preset itself limits,
such as the namespaces it exports, is in the
[README's preset tables](../../README.md#presets).

## Not offered as presets

- **[Globus](https://www.globus.org/) origins** (`globus` and `globusv2`),
  **`s3v2` in front of GCS or Azure** (`Origin.ObjectProviderURL`), and
  **tiering to either** (`Cache.TieringTargets`): each needs the provider's
  own service and credentials.
- **The `xroot` origin**, which relays to another
  [XRootD](https://xrootd.org/) server through XRootD's proxy: Pelican's
  part is the configuration it writes; the data path is XRootD's.
- **The [OSDF](https://osg-htc.org/services/osdf)'s servers**
  (`osdf-server`): they differ from Pelican's by their defaults
  (`osdf_default` in Pelican's
  [`docs/parameters.yaml`](https://github.com/PelicanPlatform/pelican/blob/main/docs/parameters.yaml)),
  which point at services that only the OSDF runs.
- **A local cache on the client's host**
  (`pelican-server serve --module localcache`, `LocalCache.*`), which
  listens on a Unix socket that a client names (`--cache unix:///...`). It
  would need a service that shares the socket with
  [`dev`](../glossary.md#dev-container), and a suite of its own. The V2
  cache's code runs here as a federated and a
  [site-local cache](site-local-cache.md) instead.
- **A cache that sends a client to its storage on a shared filesystem**
  (`Client.FileRedirectRoots`): a client so configured accepts a `file://`
  redirect, but no Pelican server sends one yet.
- **A cache serving a second federation** (`Cache.AllowedFederations`),
  which would need a second director, registry, and discovery host. The
  [`cache-api`](cache-apis.md) suite checks that a cache refuses a
  federation not its own.
- **Modules sharing a process**, other than every module at once
  ([`topo-tiny`](../../framework/presets/topo-tiny.sh)).
- **A broker in a server of its own** (`--module broker`): the directors run
  one (`Director.EnableBroker`), which
  [`origin-broker`](../../framework/presets/origin-broker.sh) uses; one
  apart would change only the host that the federation's metadata names.
- **Mixed releases.** Every shape runs one release (`PELICAN_TAG`;
  [knobs](../configuration.md#knobs)) from scratch, so none is mid-upgrade,
  and no release reads what another wrote. `IMAGE_*` can mix releases by
  hand ([Testing your changes](../../README.md#testing-your-changes)).
- **Origins of mixed kinds behind one namespace**: beside
  [`topo-multi-origin`](../../framework/presets/topo-multi-origin.sh), it
  would add only replicas whose ETags disagree, and every kind of origin
  meets every kind of cache in some shape.
- **A cache over several storage directories** (a V2 cache's
  `LocalCache.StorageDirs`, an XRootD cache's `Cache.DataLocations`): every
  cache here stores under `/data` alone.
- **[Lotman](https://github.com/PelicanPlatform/lotman)**
  (`Cache.EnableLotman`, `Lotman.EnableAPI`): the XRootD cache's allocations
  of its space by namespace. It would need a suite of its own.
- **The XRootD monitoring shoveler** (`Shoveler.Enable`): the stream it
  forwards is XRootD's. The [`observability`](observability.md) suite reads
  each server's own metrics instead.
- **X.509 and VOMS at XRootD servers** (`Origin.EnableTLSClientAuth`,
  `Origin.EnableVoms`, and the caches' own), **and macaroons**
  (`Origin.EnableMacaroons`): no Pelican client presents either.
- **Sorting by GeoIP** (`Director.GeoIPLocation`), which needs
  [MaxMind](https://www.maxmind.com/)'s database and clients at public
  addresses. The servers declare their locations (`GeoLocation`) instead
  ([routing](routing.md)). Its overrides (`GeoIPOverrides`), which map
  addresses to coordinates without the database, change one director's
  settings, not the shape; no scenario sets them.
- **Caches that fetch from caches** (`Director.CachesPullFromCaches`): the
  director adds caches to its list of origins only for a GET with
  `?prefercached`, and only while it knows fewer origins than it would
  name. A cache reading through another is the
  [site-local cache](site-local-cache.md)'s case.
- **XRootD's throttle** (`Origin.Concurrency`, `Cache.Concurrency`): a limit
  on an XRootD server's I/O operations at once, which changes when requests
  are served, not what. The [`throttling`](throttling.md) suite checks the
  V2 cache's own limits.
- **Settings of one server's administration**, each for a scenario that
  restarts the server with it rather than for a shape, and none set by any:
  a read-only web UI (`Server.WebReadOnly`), the issuer's groups from a file
  (`Issuer.GroupSource` `file`; [`oidc`](oidc-login.md)'s `groups` checks
  `oidc`), and the registry's list of institutions and fields of its own
  (`Registry.Institutions`, `Registry.CustomRegistrationFields`).
- **Background checks**: a V2 cache's data scan of each object once
  (`Cache.DataScanMode` `once`), rather than on every pass, and an origin's
  count of its exports' usage (`Origin.EnableDiskUsageCalculation`), which
  adds metrics alone. Neither changes what a server serves.
- **A server behind a reverse proxy** (`Server.TrustedProxies`), **or on
  port 443**: every server here listens on 8444, and
  [`director-ha`](../../framework/presets/director-ha.sh)'s load balancer
  passes TLS through.
- **A cache behind a firewall** (`Cache.EnableBroker`): it would serve only
  its site's own clients, which nothing here stands in for.
  [`origin-firewalled`](../../framework/presets/origin-firewalled.sh) puts
  an origin there.
- **XRootD clustering** (`Origin.EnableCmsd`, `Xrootd.ManagerHost`): the
  manager is an XRootD redirector that no Pelican server runs.
- **A certificate chain** (`Server.TLSCertificateChain` with an intermediate
  CA): the framework's CA signs each certificate directly.
- **Directors that route to a cache in an error state**
  (`Director.FilterCachesInErrorState` off): it would hide what
  [`advertise`](advertisements.md)'s `spoofed-status` checks.
- **An XRootD cache's read-ahead** (`Cache.BlocksToPrefetch`): it changes
  when a cache fetches blocks, not what it serves.
- **A server without its web UI** (`Server.EnableUI` off): only the pages
  go; the [`webapi`](web-api.md) suite checks the API, login, and issuer.
- **Virtual-host-style S3 URLs** (`Origin.S3UrlStyle` `virtual`): the `s3`
  [service](../glossary.md#helper-service) serves path style.

## Not exercised

What a preset implies that the tests don't exercise, or not in a way that
would catch a fault:

- [`cache-v2`](../../framework/presets/cache-v2.sh) and
  [`origin-pstore`](../../framework/presets/origin-pstore.sh): the bytes in
  the store on disk, which only Pelican can decrypt. The tests compare what
  such a server serves with what they wrote, and check the sizes of the
  files in its store ([block boundaries](block-boundaries.md)).
- [`topo-tiny`](../../framework/presets/topo-tiny.sh): that a download came
  through the cache; the [`transfers`](test-data.md) suite's `served-by`
  checks skip.
- [`topo-site-local-cache`](../../framework/presets/topo-site-local-cache.sh):
  of cache-2, anything beyond its reads, a refusal, a listing, and what it
  leaves in a federated cache. The suites of a cache's
  [APIs](cache-apis.md), [freshness](cache-freshness.md), and block
  boundaries ask only the federated caches.
- [`origin-pstore`](../../framework/presets/origin-pstore.sh):
  `pelican-server origin pstore fsck --repair`, and a restore opened with an
  escrowed key (`metadata-backup-key`) ([pstore](pstore.md)).
- [`origin-https`](../../framework/presets/origin-https.sh) and
  [`origin-httpsv2`](../../framework/presets/origin-httpsv2.sh): a backend
  token from an OAuth2 issuer (`Origin.HttpAuthOAuth2Issuer`, `ClientID`,
  and `ClientSecretFile`) ([exports and copies](exports-and-copies.md)).
- [`cache-mixed`](../../framework/presets/cache-mixed.sh): the `transfers`
  suite's named-cache scenarios name cache-0, a V2 cache;
  [`client`](client-contracts.md)'s `failover` crosses from V2 to XRootD
  alone.
- [`cache-tiered`](../../framework/presets/cache-tiered.sh): that a cache
  drops its copy of a tiered object, or serves one after a restart or an
  eviction ([tiering](tiering.md)).
- [`origin-metadata`](../../framework/presets/origin-metadata.sh): that an
  upload cut short, or refused, publishes no event
  ([POSC and metadata](posc-and-metadata.md)).
- [`origin-metadata-tx`](../../framework/presets/origin-metadata-tx.sh): the
  rollback of an overwrite whose event fails; only a new object's is
  checked.
- [`origin-multiuser`](../../framework/presets/origin-multiuser.sh): a read
  through a cache, as the token's user ([Unix users](unix-users.md)).
- [`origin-broker`](../../framework/presets/origin-broker.sh): data through
  the broker. The origin stays reachable, so only the director's own
  requests are brokered. Under
  [`origin-firewalled`](../../framework/presets/origin-firewalled.sh), the
  caches' fetches must go through the broker, but a client's don't, since
  the dev container is on the origin's network.
- [`origin-ssh-tunnel`](../../framework/presets/origin-ssh-tunnel.sh): that
  the tunnel is used; the lab server can reach the origin without it.
- [`origin-short-tokens`](../../framework/presets/origin-short-tokens.sh): a
  transfer that outlives its token, or a cache shown an expired one.
- [`origin-export-templates`](../../framework/presets/origin-export-templates.sh):
  a read with such a token; only the scopes the issuer grants are checked
  ([issuer](issuer.md)).
- [`auth-external-issuer`](../../framework/presets/auth-external-issuer.sh):
  a token of the origins' own keys or issuers, and a token of the trusted
  key that names another issuer.
- [`auth-no-issuer`](../../framework/presets/auth-no-issuer.sh): a token of
  the origin's key that names another issuer; [`auth`](authorization.md)'s
  `wrong-iss` skips.
- [`director-ha`](../../framework/presets/director-ha.sh): a read or write
  through the load balancer with a director down.
- [`registry-approval`](../../framework/presets/registry-approval.sh): a
  request for a pending server's data; the directors' routing is checked
  instead ([federation](federation.md)).
