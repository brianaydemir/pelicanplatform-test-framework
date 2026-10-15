# Client contracts

Part of [What the tests check](README.md).

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
  line of its outfile, in the order the transfers finished, so each is
  known by its `TransferUrl`; of a batch of two objects through a cache, cold
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
  one that exists and one that does not (`mixed`): a failed download
  ends a batch, so the plugin writes the ads of what finished before it,
  then the failure's, and no more; so one ad or two, the last the
  missing object's failure, and any other the existing object's
  success. The exit
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
  object, must report one `ETag` by HEAD and by GET, or none by either,
  as XRootD's HTTP server sends none (so a client resuming there cannot
  tell that the object changed, which the note says), and a cache's must
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
  (`other-owner`). Each runs with the pass's `--profile`
  ([token profiles](README.md#token-profiles)), so that in the
  SciTokens pass the scope is `read:` and the path.
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
  embedded issuer, and for a standalone origin. Its tokens are the
  issuer's, of the WLCG profile, so it runs in the WLCG pass alone.
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
