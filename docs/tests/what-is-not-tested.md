# What Is Not Tested

Part of [What the tests check](README.md).

Two kinds of gap: features of Pelican that no preset offers, and what a
preset implies that the tests don't exercise. What a preset itself
limits, such as the namespaces it exports, is in the
[README's preset tables](../../README.md#presets).

## Not offered as presets

- **Globus origins** (`globus` and `globusv2`), **`s3v2` in front of
  GCS or Azure** (`Origin.ObjectProviderURL`), and **tiering to either**
  (`Cache.TieringTargets`): each needs the provider's own service and
  credentials.
- **The `xroot` origin**, which relays to another XRootD server through
  XRootD's proxy: Pelican's part is the configuration it writes; the
  data path is XRootD's.
- **The OSDF's servers** (`osdf-server`): they differ from Pelican's by
  their defaults (`osdf_default` in Pelican's `docs/parameters.yaml`),
  which point at services that only the OSDF runs.
- **A local cache on the client's host**
  (`pelican-server serve --module localcache`, `LocalCache.*`), which
  listens on a Unix socket that a client names
  (`--cache unix:///...`). It would need a service that shares the
  socket with `dev`, and a suite of its own. The V2 cache's code runs
  here as a federated and a site-local cache instead.
- **A cache serving a second federation**
  (`Cache.AllowedFederations`), which would need a second director,
  registry, and discovery host. The `cache-api` suite checks that a
  cache refuses a federation not its own.
- **Modules sharing a process**, other than every module at once
  (`topo-tiny`).
- **A broker in a server of its own** (`--module broker`): the
  directors run one (`Director.EnableBroker`), which `origin-broker`
  uses; one apart would change only the host that the federation's
  metadata names.
- **Mixed releases.** Every shape runs one release (`PELICAN_TAG`) from
  scratch, so none is mid-upgrade, and no release reads what another
  wrote. `IMAGE_*` can mix releases by hand
  ([Testing your changes](../../README.md#testing-your-changes)).
- **Origins of mixed kinds behind one namespace**: beside
  `topo-multi-origin`, it would add only replicas whose ETags disagree,
  and every kind of origin meets every kind of cache in some shape.
- **A cache over several storage directories** (a V2 cache's
  `LocalCache.StorageDirs`, an XRootD cache's `Cache.DataLocations`):
  every cache here stores under `/data` alone.
- **Lotman** (`Cache.EnableLotman`, `Lotman.EnableAPI`): the XRootD
  cache's allocations of its space by namespace. It would need a suite
  of its own.
- **The XRootD monitoring shoveler** (`Shoveler.Enable`): the stream it
  forwards is XRootD's. The `observability` suite reads each server's
  own metrics instead.
- **X.509 and VOMS at XRootD servers** (`Origin.EnableTLSClientAuth`,
  `Origin.EnableVoms`, and the caches' own), **and macaroons**
  (`Origin.EnableMacaroons`): no Pelican client presents either.
- **Sorting by GeoIP** (`Director.GeoIPLocation`), which needs MaxMind's
  database and clients at public addresses. The servers declare their
  locations (`GeoLocation`) instead.
- **A server behind a reverse proxy** (`Server.TrustedProxies`), **or
  on port 443**: every server here listens on 8444, and `director-ha`'s
  load balancer passes TLS through.
- **A cache behind a firewall** (`Cache.EnableBroker`): it would serve
  only its site's own clients, which nothing here stands in for.
  `origin-firewalled` puts an origin there.
- **XRootD clustering** (`Origin.EnableCmsd`, `Xrootd.ManagerHost`): the
  manager is an XRootD redirector that no Pelican server runs.
- **A certificate chain** (`Server.TLSCertificateChain` with an
  intermediate CA): the framework's CA signs each certificate directly.
- **Directors that route to a cache in an error state**
  (`Director.FilterCachesInErrorState` off): it would hide what
  `advertise`'s `spoofed-status` checks.
- **An XRootD cache's read-ahead** (`Cache.BlocksToPrefetch`): it
  changes when a cache fetches blocks, not what it serves.
- **A server without its web UI** (`Server.EnableUI` off): only the
  pages go; the `webapi` suite checks the API, login, and issuer.
- **Virtual-host-style S3 URLs** (`Origin.S3UrlStyle` `virtual`): the
  `s3` service serves path style.

## Not exercised

What a preset implies that the tests don't exercise, or not in a way
that would catch a fault:

- `cache-v2` and `origin-pstore`: the bytes in the store on disk, which
  only Pelican can decrypt. The tests compare what such a server serves
  with what they wrote, and check the sizes of a V2 cache's files
  ([block boundaries](block-boundaries.md)).
- `topo-tiny`: that a download came through the cache; the `transfers`
  suite's `served-by` checks skip.
- `topo-site-local-cache`: of cache-2, anything beyond its reads, a
  refusal, a listing, and what it leaves in a federated cache. The
  suites of a cache's APIs, freshness, and block boundaries ask only the
  federated caches.
- `origin-pstore`: `pelican-server origin pstore fsck --repair`, and a
  restore opened with an escrowed key (`metadata-backup-key`)
  ([pstore](pstore.md)).
- `origin-https` and `origin-httpsv2`: a backend token from an OAuth2
  issuer (`Origin.HttpAuthOAuth2Issuer`, `ClientID`, and
  `ClientSecretFile`) ([exports and copies](exports-and-copies.md)).
- `cache-mixed`: the `transfers` suite's named-cache scenarios name
  cache-0, a V2 cache; `client`'s `failover` crosses from V2 to XRootD
  alone.
- `cache-tiered`: that a cache drops its copy of a tiered object, or
  serves one after a restart or an eviction.
- `cache-memory`: that a read came from memory.
- `origin-metadata`: that an upload cut short, or refused, publishes no
  event.
- `origin-metadata-tx`: the rollback of an overwrite whose event fails;
  only a new object's is checked.
- `origin-multiuser`: a read through a cache, as the token's user.
- `origin-broker`: data through the broker. The origin stays reachable,
  so only the director's own requests are brokered. Under
  `origin-firewalled`, the caches' fetches must go through the broker,
  but a client's don't, since the dev container is on the origin's
  network.
- `origin-ssh-tunnel`: that the tunnel is used; the lab server can
  still reach the origin.
- `origin-short-tokens`: a transfer that outlives its token, or a cache
  shown an expired one.
- `origin-export-templates`: a read with such a token; only the scopes
  the issuer grants are checked.
- `auth-external-issuer`: a token of the origins' own keys or issuers,
  and a token of the trusted key that names another issuer.
- `auth-no-issuer`: a token of the origin's key that names another
  issuer; `wrong-iss` skips.
- `director-ha`: a read or write through the load balancer with a
  director down.
- `registry-approval`: a request for a pending server's data; the
  directors' routing is checked instead.
