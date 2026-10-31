# Client contracts

Part of [What the tests check](README.md).

The `client` suite checks contracts that show only through the
[clients](../glossary.md#client): `stash_plugin`'s result ads, failover
between caches, which token the client presents, how
`pelican token create` finds an issuer, and the URL
`pelican object share` hands out. It needs `/protected-a`. Its
[credentials](authorization.md) are those of the `transfers` and `auth`
suites. `stash_plugin stage` is not tested.

- `plugin-ads`: `stash_plugin`'s result ads, one per transfer. Skips
  without a federated cache.

  - `cold`, `warm`: Of a read through a cache, each ad must report the
    transfer's success, size, name, URL, type, and times, and
    `DeveloperData` its attempts. At a V2 cache, `CacheHit` must be
    false when cold, and true when warm, with a `DataAge` above 0, and
    `ServerChecksums` must hold the object's `crc32c`. Every digest
    reported must be the object's.
  - `dne`, `refused`: A failed download must report `TransferSuccess`
    and `TransferRetryable` false, and leave no file. Its error must be
    of type `Specification` (`PelicanErrorCode` 5011) for a missing
    object, and `Authorization` for a token of an unknown key.
  - `mixed`: Of a batch of an object that exists and one that does not,
    the last ad must be the missing object's failure, and any other the
    existing object's success.
  - `upload`, `upload-refused`: Where `/protected-a` takes writes, an
    upload's ads must be like a download's, and an origin must hold
    each file. With an unknown key's token, the error must be of type
    `Authorization`, and no origin may hold the file.

  The exit status must be 0 if every transfer succeeded, else 11 if any
  failure may be retried, else 1.

- `failover`: A federated V2 cache and a second cache
  (`-p topo-multi-cache`; an XRootD one under `-p cache-mixed`) hold an
  object that is then removed from the origins. The first cache's chaos
  API corrupts a block in the middle of its copy.

  - `get`: `pelican object get --cache <first>,<second>` must bring the
    object, its stats showing a failed attempt at the first cache, then
    a success at the second. The client resumes by `Range` only if the
    `ETag` is unchanged, so the caches' ETags must agree.
  - `plugin`: `stash_plugin`, with the same `Client.PreferredCaches`,
    must bring the object, recording a failed `Endpoint0` at the first.

  Skips without `Cache.EnableChaosAPI`, under `-p origin-max-age`, and
  under `-p topo-multi-origin`.

- `etag-agreement`: Each origin that takes direct clients, and each
  federated cache, must report one `ETag` for an object by HEAD and by
  GET, or none by either (RFC 9110, section 8.8.3). A cache's must be
  one of the origins', and origins that share a store must agree.

- `token-discovery`: Which token `pelican object get` presents, of those
  its environment offers:

  - `bearer-token`, `bearer-token-file`, `condor-creds`: the `server`
    token alone in `BEARER_TOKEN`, `BEARER_TOKEN_FILE`, or
    `_CONDOR_CREDS/scitokens.use` must work;
  - `skips-unacceptable`: the `wrong-path` token in `BEARER_TOKEN`, with
    `server` in `_CONDOR_CREDS`, must work, since the director's hints
    rule out the first;
  - `acceptable-wins`: the `unknown` token in `BEARER_TOKEN`, with
    `server`, must be refused, since it looks acceptable;
  - `prefix`: a `protected-a+pelican://` URL must select
    `_CONDOR_CREDS/protected-a.use` over `scitokens.use`;
    `prefix-underscore`: `protected_a+pelican://` must select
    `protected.a.use`.

  Each case runs again through `stash_plugin`, as `plugin-<case>`, which
  must get the object where the client does, and otherwise report an
  `Authorization` error.

- `url-forms`: `pelican object get` must get an object by a bare path,
  with the federation configured (`configured`; skips at a standalone
  origin), given by `-f` (`flag`), or in
  `PELICAN_FEDERATION_DISCOVERYURL` (`env`); by `osdf:///` and
  `stash:///` URLs with `-f` (`osdf`, `stash`); and by `osdf://` with
  two slashes, read as three (`osdf-two-slashes`). `pelican object put`
  to an `osdf:///` URL must land (`put-osdf`; needs writes).
  `stash_plugin` must get an `osdf:///` URL (`plugin-osdf`).

- `token-create-discovery`: `pelican token create` without `--issuer` or
  `--scope-path`, in the pass's profile:

  - `origin-key`: with the origins' key, the issuer must be the first
    the director names whose JWKS holds the key, and the scope the path;
    `reads`: the token must read there at every server;
  - `namespace-key`: with `ns-protected-a.pem`, the issuer must be found
    through the export's `IssuerJwks`; skips where no namespace has keys
    of its own;
  - `unknown-key`: with `unknown.pem`, it must fail, saying that no
    issuer matches;
  - `public`: for `/public`, it must fail, asking whether a token is
    needed; under `-p origin-public-writes`, the issuer must be
    `/public`'s;
  - `other-owner`: under `-p topo-multi-owner`, with origin-2's key for
    `/other`, the issuer must be origin-2's.

- `share-url`: `pelican object share` of a collection must print its URL
  with `?authz=<token>`, scoped to its `storage.read` alone (`url`), and
  with `--write`, its `storage.create` (`url-write`). With the `url`
  token in the query, each director's object route must redirect with
  `authz` in `Location` and no token in `Link`, and lead to the object
  (`curl`). `pelican object get` of the URL must get it (`get`). Skips
  without the origins' embedded issuer, and at a standalone origin.
  Runs in the WLCG pass only ([token profiles](README.md#token-profiles)).

- `director-decision`: With `Plugin.DirectorDecisionPercentage` at 100,
  `stash_plugin` must record the director's decision in
  `DeveloperData.DirectorDecision`: the sort method, the client's
  coordinate, and each server's weights. Skips at a standalone origin,
  and without a federated cache.

- `skipstat-failover`: Under `-p topo-multi-origin`, an object in one
  origin's store alone, read by `pelican object get --direct` with
  `?skipstat`, must come back, since the client tries each origin the
  director lists. Skips where the origins share a store, and where
  `/protected-a` takes no direct clients.

- `multi-source`: `get` and `put` with several sources (G8–G10, P8–P10
  of Pelican's `docs/object-transfer-semantics.md`), and `?recursive` in
  place of `-r`:

  - `get-directory`: `get` into a directory must land each source by its
    name (G8); `get-missing-dir`, `get-file`: into a missing path, or
    onto a file, it must fail (G9, G10);
  - `put-collection`, `put-new`: `put` to a collection, or to a missing
    path, must land each source under it by its name (P8, P9);
    `put-object`: to an object, with overwrites off, it must be refused,
    leaving the object as it was (P10);
  - `put-recursive-query`, `get-recursive-query`: `put` of a directory
    with `?recursive` must lay the tree out flat (P6), and `get` with
    `?recursive` bring it back;
  - `plugin-put-recursive-query`, `plugin-get-recursive-query`: the same
    through `stash_plugin`.

  Rows from `put-collection` on need writes; without them, one `put`
  row skips. The `?recursive` gets need `Listings`.

- `preferred-cache-down`:
  `pelican object get --cache https://<cache>:1,+` names a dead cache,
  then the director's, which must serve (`plus`). Without `+`, the get
  must fail, but not by refusal (`alone`). Skips without a federated
  cache.

- `async-job`: `pelican object get --async --wait`, run by the client
  agent, must bring the object (`get`). `pelican job status --json` must
  report it `completed`, with the object's bytes (`status`), and
  `pelican job list --json` must list it (`list`). Where `/protected-a`
  takes writes, `pelican object copy --async --wait` must land a local
  file (`copy`). `pelican job cancel` must leave the finished job
  `completed` (`cancel-finished`). `pelican client-agent stop` must stop
  the agent within 15 seconds (`stop`).
