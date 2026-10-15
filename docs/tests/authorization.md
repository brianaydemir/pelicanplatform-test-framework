# Authorization

Part of [What the tests check](README.md).

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
Each suite presents each credential in both
[token profiles](README.md#token-profiles), as `pelican token create`
makes them: of the WLCG profile, then of SciTokens 2.0, in its
`-scitokens` pass, where read is `read:` and create and modify are both
`write:`, so that `wrong-op`'s token is `write:` alone for a get and
`read:` alone for a put. Each must fare alike in both. `none` presents
no token, and so runs in the WLCG pass alone.
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
  `./fed.sh test auth/keys` runs only this check, which presents no
  token, and so runs in the WLCG pass alone.
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
  neither replace nor delete one; `storage.modify` may do all three. In
  the SciTokens pass, `modify-only` presents a token of `write` alone,
  which may do all three too, and `create-only` does not run, since
  SciTokens has no scope of create alone. Each
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
  the rows fail there. An XRootD cache sends none, and is passed over.
  Presenting no token, the check runs in the WLCG pass alone. The
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
  WLCG token profile says, and SciTokens 2.0 too. Every credential names
  its profile's "any" audience (`https://wlcg.cern.ch/jwt/v1/any`, or
  SciTokens' `ANY`); this token, like `server`'s but for
  `https://elsewhere.invalid`, must be refused by every origin, for a
  GET of the test object and a PUT of a new one, which must store
  nothing. Only the origins are asked (Pelican's `origin_serve/authz.go`,
  and the audience in an XRootD origin's scitokens configuration): a
  cache serves tokens minted for any origin, so what it should make of
  an audience is not settled. `audience-specific` presents a token for
  the first origin's own audience alone (`Origin.TokenAudience`, which
  is `Origin.Url` by default: the scheme, host, and port of its data
  URL, XRootD's for an XRootD origin), which only that origin may take,
  and one whose `aud` lists its profile's "any" audience and another,
  which every origin must take, since a token is for each audience it
  lists. Under `-p topo-multi-origin`, the second origin must refuse the
  first. Both checks skip where the origins take no direct clients.
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
  read ([another owner](another-owner.md)). It puts an object there, which
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
