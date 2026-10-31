# Authorization

Part of [What the tests check](README.md).

The `transfers` and `auth` suites present the same credentials
(`framework/testlib/credentials.py`), each in one namespace. The "other
namespace" of `/protected-a` is `/protected-b`, and the reverse.
`/public` gets only those that need neither a key of its own nor another
namespace.

| Credential   | Token                                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------- |
| `server`     | signed by the origins' issuer key                                                                                   |
| `jwks`       | signed by `test-keys/jwks.pem`, whose public key the origins list only in `Server.IssuerJwks`                       |
| `ns-jwks`    | signed by `test-keys/ns-<ns>.pem`, whose public key the origins list only in the `IssuerJwks` of the `/<ns>` export |
| `none`       | none                                                                                                                |
| `unknown`    | signed by `test-keys/unknown.pem`, which no server knows                                                            |
| `ns-other`   | signed by the other namespace's `ns-<ns>.pem`                                                                       |
| `wrong-op`   | the origins' key, but read-only for a put, or create and modify for a get                                           |
| `wrong-path` | the origins' key, scoped to `/elsewhere/`                                                                           |
| `wrong-iss`  | the origins' key, but the other namespace's issuer                                                                  |
| `ns-cross`   | the other namespace's `ns-<ns>.pem` and issuer: a token only it accepts                                             |
| `expired`    | the origins' key, expired                                                                                           |

Otherwise, each token has read, create, and modify on the whole
namespace, names the namespace's issuer, and lasts four hours. `init`
creates the four test keys in `framework/var/test-keys/`. Each
credential is presented in both
[token profiles](README.md#token-profiles), but `none` in the WLCG pass
alone.

Where a credential would test nothing new, it skips:

- `wrong-iss`, `ns-jwks`, `ns-other`, and `ns-cross`, under
  `-p auth-no-issuer`, and under `-p auth-external-issuer`, whose key
  stands in for the origins';
- `jwks`, under `auth-external-issuer`;
- `ns-other` and `ns-cross`, where only `/protected-a` is exported.

Checks that need `/protected-a` skip under `-p origin-public`.

## What should be allowed

Where a token decides, only `server`, `jwks`, and `ns-jwks` should be
allowed. A token does not decide these:

- reads of `/public`: allowed;
- writes to a namespace without `Writes` (`/public`, unless
  `-p origin-public-writes`, and every namespace under
  `-p origin-no-direct`): refused;
- requests straight to an origin, and direct reads, in a namespace
  without `DirectReads` (every namespace under `origin-no-direct`):
  refused.

The director rejects any expired token.

## Through the clients

The `transfers` suite presents each credential through each client
([test data](test-data.md)). Its `failures` column says who refused: the
client, with no token to send (`client`); the director, of an expired
token (`director`), or finding no origin that allows the operation, e.g.
a put to `/public` (`unsupported`); or an origin or cache, with 401 or
403 and nothing else (`server`). Any other failure is unexpected. With
no terminal and an empty credential store, `pelican object` should fail
at once with error 4010.

## At each server

The `auth` suite sends GET, HEAD, PUT, and DELETE to every origin, and
GET and HEAD to every cache, in every namespace, with each credential.
An allowed request must return (for HEAD, `Content-Length`), store, or
remove the object; a refused one must get 401 or 403, and change
nothing. An answer with another namespace's test object fails. Most rows
are named `<server> <namespace> <method> <case>`, e.g.
`origin-0 protected-a get server`.

Both suites present `expired` once it is 70 seconds past its expiry,
since a server may allow 60 seconds of clock skew.

## Other checks

The `auth` suite's other checks follow; those of
[token validation](token-validation.md) have a page of their own. Each
works in `/protected-a` unless it says otherwise. Where the origins take
no direct clients, every request straight to one must be refused.

- `keys`: Each protected namespace's discovery document must name its
  own JWKS, holding the server-wide keys and its `ns-<ns>.pem` alone.
  The server-wide JWKS must hold `jwks.pem`'s key, and neither
  namespace's; `/public`'s (`-p origin-public-writes`), the server-wide
  keys, and neither namespace's. Rows are named
  `keys <issuer> <document>`. WLCG only.
- `narrow`: A token for the test object alone, at every server, must get
  it (`narrow-in`), but not a sibling whose name begins with the
  object's (`narrow-out`). Each cache first serves the sibling with
  `server` (`narrow-sibling`).
- `create-only` and `modify-only`: At every origin, `storage.create`
  alone may create an object (`put-new`), but neither replace
  (`put-over`) nor delete one (`delete`); `storage.modify` may do all
  three (WLCG token profile). In the SciTokens pass, `modify-only`
  presents `write`; `create-only` does not run.
- `traversal`: Paths that climb out of a namespace with `..`, `%2e%2e`,
  `%2E%2E`, `.%2e`, `%252e%252e`, or `..%2f`, at every server, must not
  reach another namespace's test object (`namespace`), a file beside the
  namespaces' storage (`store`), or the origin's signing key
  (`filesystem`); nor may symbolic links in the storage (`symlink`).
  Rows are named `traversal <server> <kind>`.
- `hints`: A V2 cache's refusal of a GET with no token must carry a
  director's hint headers, naming the namespace and its issuer
  (`docs/pelican-http-headers.md` in Pelican's repository). WLCG only.
  The [federation](federation.md) suite's `hints` checks the directors'.
- `query-token`: `server`'s and `unknown`'s tokens in the `authz` query
  parameter, bare (`authz-bare-*`) or after `Bearer `
  (`authz-bearer-*`), must fare as in the header, at every server.
- `audience`: Every origin must refuse a token for another audience
  (`wrong-aud`), for a GET and a PUT, as the WLCG token profile and
  SciTokens 2.0 require.
- `audience-specific`: A token for the first origin's
  `Origin.TokenAudience` alone must be taken there alone
  (`aud-<origin>`); one that also lists the profile's "any" audience,
  everywhere (`aud-list`). Both audience checks skip where the origins
  take no direct clients.
- `narrow-climb`: A token for one collection must reach nothing beside
  it by `traversal`'s spellings but `%252e%252e`: a GET at every server,
  and a PUT and a DELETE at each origin that takes writes, must be
  refused (4xx), changing nothing.
- `nested-protected`: Under `-p topo-multi-owner`, origin-2's
  `/public/secret` takes a token ([another owner](another-owner.md)). An
  object there must be refused (401 or 403) with no token (`none`), and
  with `unknown.pem`'s naming the prefix's issuer (`unknown`), at each
  cache holding it, at origin-2, and through each director.
- `token-lists`: A native origin should try each token, up to ten, in
  its `Authorization` header (comma-separated, after a bearer scheme in
  any case; RFC 9110, section 11.1), and in repeated `access_token`
  (RFC 6750) and `authz` parameters. A GET must pass with `server`'s
  token in `access_token` (`access-token`), after `unknown`'s in one
  header (`comma`) or a second `authz` (`authz-twice`), and after
  `bearer` (`lowercase`). It must fail under `Basic` (`basic`), and get
  401 with eleven tokens, the last `server`'s (`eleven`).
- `standalone-refusals`: At each native origin, a GET must get 401 with
  no token (`hdr-none`); with `unknown`'s, 403 standalone
  (`-p topo-standalone`) or 401 federated (`hdr-unknown`); with
  `server`'s, the object (`hdr-server`); and of a missing object, 404
  (`hdr-missing`). A standalone origin's answers must carry a director's
  hint headers for `/protected-a`, and `X-Pelican-Token-Generation`; a
  federated origin's, none.
