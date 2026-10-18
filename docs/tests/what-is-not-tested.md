# What is not tested

Part of [What the tests check](README.md).

Two kinds of gap: features of Pelican that no preset offers, and what a
preset implies about the handling of data that the tests don't
exercise. What a preset itself limits, such as the namespaces it
exports or the suites that skip under it, is in its row of the
[README's preset tables](../../README.md#presets).


## Not offered as presets

Features that would seem natural presets, and why none is offered:

- **Globus origins** (`globus` and `globusv2`), and **`s3v2` in front
  of GCS or Azure** (`Origin.ObjectProviderURL`): each needs the
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
- **A server behind a reverse proxy** (`Server.TrustedProxies`), **or
  on port 443**, which Pelican leaves out of the URLs it derives: every
  server here listens on 8444, and `director-ha`'s load balancer passes
  TLS through.


## Not exercised

What a preset implies about the handling of data that the tests don't
exercise, or not in a way that would catch a fault:

- `topo-tiny`: nothing confirms that a client's download came through
  the cache, since the `transfers` suite's `served-by` checks skip.
- `topo-multi-origin`: no read with one replica down; `restart` fails
  over only between caches.
- `topo-site-local-cache`: of cache-2, only its reads, a refusal, a
  listing, and what it leaves in a federated cache (`sitelocal`); the
  suites that check a cache's own APIs, the freshness of its copies, or
  its block boundaries ask only the federated caches.
- `origin-pstore`: of `pelican-server origin pstore`, which runs with
  the origin stopped, `fsck --repair`, and a restore opened with an
  escrowed key (`metadata-backup-key`) rather than the origin's issuer
  keys ([pstore](pstore.md)).
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
  brokered: Pelican's brokered path to an unreachable origin does not
  work end to end.
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
