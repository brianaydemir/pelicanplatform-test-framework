# What is not tested

Part of [What the tests check](README.md).

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
  namespaces' ([authorization](authorization.md)).
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
  ([Testing your changes](../../README.md#testing-your-changes)).
- The XRootD monitoring shoveler (`Shoveler.Enable`), which forwards
  the XRootD servers' UDP monitoring stream to a message bus, or to
  collectors of its own (`Shoveler.OutputDestinations`); the
  `observability` suite reads each server's metrics instead
  ([observability](observability.md)).
- Sorting by GeoIP, which needs a MaxMind database, and the adaptive
  sort's status and I/O load weights, which change too slowly to
  observe; the `routing` suite sorts by declared locations
  ([routing](routing.md)).
- Whether a cache keeps only the latest of the directors' test files,
  which a director asks it to evict: a V2 cache's store is not laid out
  by path, and an XRootD cache's eviction only marks a file for its
  next purge (`HandleDirectorEvictRequest` in `cache/cache_api.go`);
  the `director-api` suite reads the test file through each cache
  ([director APIs](director-apis.md)).
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
  exports from it ([pstore](pstore.md)).
- `pelican cache evict`, `purge`, `introspect`, and `chaos`, which read
  the cache's own configuration and address file, and so run only
  beside it; `cache-api` calls the APIs they call
  ([cache APIs](cache-apis.md)).
- Of the embedded issuer: groups from a file (`Issuer.GroupSource`
  `file`), which the suite's users get from the server's own database,
  and the `oidc` suite's from the identity provider, for a while. Also
  `pelican.transfer` granted
  through a scope grant (`Transfer.EnabledGroups` the `transfer-api`
  suite checks); and the limit on registrations, five from an address,
  then one a minute.
- Of the registry: the
  OSDF topology's namespaces, `Registry.InstitutionsUrl`, and the
  cleanup of inactive registrations ([registry](registry.md)); the `keys`
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
  as it starts ([observability](observability.md)).
- Of accounts: `Server.UserAdminGroups`; scopes granted to a group;
  onboarding invites; linked identities; and clearing a user's password
  or their agreement to the AUP ([accounts](accounts.md)).
- Of the transfer API: a finished job that the manager drops from
  memory on its own, five minutes after it ends (the `restart` case of
  the `transfer-api` suite drops it with a restart instead); the
  authorization-code bootstrap of a credential, whose callback needs a
  browser; and a job interrupted while its source is another server's
  ([transfer API](transfer-api.md)).
- Of the clients: the order of the client's tries among servers it
  probes, which it ranks by their answers; `osdf://` and `stash://`
  URLs, whose discovery is hard-coded to osg-htc.org
  ([client contracts](client-contracts.md)); `?pack`, by which `pelican
  object get` and `put` move a collection as one archive, which the
  native origin does not serve, and which no suite asks an XRootD
  origin for; and of the client agent, a job cancelled while it runs,
  and the agent's wallet, which `pelican client-agent warm` fills with
  tokens acquired interactively ([client contracts](client-contracts.md)).
- A site's HTTP proxy, which the client sends its transfers through
  when `http_proxy` or its kin is set (`Client.DisableHttpProxy` turns
  that off), trying without it when it fails, unless
  `Client.DisableProxyFallback` says not to; nothing here sets one.
- Of a transfer server (`-p topo-transfer-server`): its web API, health,
  metrics, restarts, and keys, which the `webapi`, `observability`,
  `restart`, and `keys` suites check only of the federation's origins,
  caches, directors, and registry; the `transfer-api` suite checks its
  API, its local issuer's grants, and its restart with a job under way
  ([transfer API](transfer-api.md)).
- Reloading a server's certificate without a restart: the servers mount
  the certificate files as Docker secrets, which a file written anew on
  the host does not replace, so a reissued certificate needs a restart
  here ([keys](keys.md)).
- Of logging in through an identity provider (`-p auth-oidc`): GitHub
  as the provider (`Issuer.GroupSource` `github`, whose organizations
  are the groups), CILogon's own claims, and any provider but the
  framework's own; `Issuer.OIDCAuthenticationRequirements`, which only
  the retired OA4MP issuer read; `Issuer.UserStripDomain`; a login at
  a transfer server (`-p topo-transfer-server`); and the browser's own
  part, which the tests play ([OIDC login](oidc-login.md)).
- `with-grafana`, which needs a data source the framework lacks
  ([Configuration](../configuration.md#presets)).
