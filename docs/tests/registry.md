# Registry

Part of [What the tests check](README.md).

The `registry` suite tests the registry directly. It runs in the WLCG
pass only ([token profiles](README.md#token-profiles)), and skips a
standalone origin.

What the suite registers or declares has `registry-test-<run>` in its
name (`<t>` below). Before and after its scenarios, it removes all such,
of any run. It acts as the registry's admin, and as the users
`test-registry` and `test-registry-heir`.

- `register`: A new prefix must get 201; one registered already, 200.
  - `invalid`: `/api/<t>`, `/view/<t>`, `/pelican/<t>`, `/<t>/./b`,
    `/<t>/../b`, `/.<t>`, and `/` must get 400.
  - `child-foreign`, `child-owner`, `sibling`: With key chaining
    (`Registry.RequireKeyChaining`, on by default), `/protected-a/<t>`
    must get 403 with an unregistered key, and 201 with the origins'
    key; `/protected-a-<t>` must take any key. Under
    `-p topo-multi-owner`, chaining is off: `child-foreign` must get
    201, and `child-owner` skips.
  - `like-underscore`, `like-percent`: `_` and `%` are literal. Beside
    `/<t>-a_b`, `/<t>-aXb/c` must take another key; likewise
    `/<t>-pXYZq/c` beside `/<t>-p%q`. Skip without chaining.
  - `again`: `/protected-a`, registered again with another key, must get
    200, "already registered", and keep its keys.
  - `delete-foreign`, `delete-owner`, `delete-unknown`: A DELETE of
    `/api/v1.0/registry/<prefix>` must get 403 with another key's
    `pelican.namespace_delete` token; 200 with the prefix's, its JWKS
    then 404; and 400, as one JSON document, for an unknown prefix.
  - `approval`: A new registration's keys must be served at once, or
    under `-p registry-approval`, refused (403) until approved.
  - `cli`: `pelican namespace register` and `delete`, with a key of the
    run's (`IssuerKeysDirectory`), must register the prefix with that
    key alone, and delete it.
  - `cli-flags`: `pelican namespace register --privkey <key> --pubkey <key> --namespace-url <registry>`,
    with no keys in `IssuerKeysDirectory`, must register the prefix with
    `--privkey`'s key. The row can't show whether `--namespace-url` is
    honored.
- `jwks`: The registry's JWKS for each prefix
  (`/api/v1.0/registry/<prefix>/.well-known/issuer.jwks`), compared by
  thumbprint.
  - `exports`: Each export's must hold exactly the keys its origin
    publishes, which must include its key directory's.
  - `servers`: So must each server prefix's (`/origins/<host>:<port>` or
    `/caches/<name>`). [Keys](keys.md) checks key changes.
  - `openid`: Each prefix's `/.well-known/openid-configuration` must
    name that JWKS as `jwks_uri`.
  - `unknown`: An unregistered prefix must get 404 for both.
  - `private`: Every JWKS the federation serves, and every
    registration's keys in `/api/v1.0/registry` and
    `/api/v1.0/registry_ui`, must be a JWKS free of private key material
    (`d`, `p`, `q`, `dp`, `dq`, or `qi`).
- `registry-ui`: The web API (`/api/v1.0/registry_ui`), with two
  registrations of the run's, one approved.
  - `list-public`: `/namespaces`, with no credentials, must list
    Approved registrations alone, without `pubkey`.
  - `list-pending`: With no credentials, `?status=Pending` must get 403.
  - `prefix-type`: `?prefixType=bogus` must get 400.
  - `bad-status`: `?status=Bogus`, as the admin, must get 400, as one
    JSON document.
  - `non-owner`: `/namespaces/<id>` of `/protected-a`, as
    `test-registry`, must get 403.
  - `unknown-id`: Approving or denying an unknown registration must
    get 404.
  - `denied`: Under `-p registry-approval`, a denied registration's
    JWKS must get 403. Skips elsewhere.
  - `servers`: `/servers` must list each server prefix of the shape
    once, as one server, and no name twice.
  - `server-one`: `/servers/<id>` must give each, and an unknown ID
    must get 404.
- `completeness`: A server's registration starts Incomplete. Its key's
  holder completes it with a `registry.edit_registration` token, and
  becomes its owner for good, so the suite completes
  `/origins/<t>.invalid`, not the shape's.
  - `check-status`: The completeness check
    (`POST /api/v1.0/registry/namespaces/check/status`) must call each of
    the shape's server registrations not `complete`, with an `edit_url`.
    Each must be Incomplete, or Approved under `-p registry-approval`.
  - `incomplete`: `/origins/<t>.invalid` must be Incomplete, unowned,
    and not `complete`, with the claim page as `edit_url`.
  - `foreign-token`: Completing it as `test-registry` with another key's
    token must get 403, and change nothing.
  - `own-token`: Completing it with its own key's token, and a body
    naming another owner, must leave it `test-registry`'s, Pending, and
    `complete`, with the origin edit page as `edit_url`.
- `ownership`: `/<t>-own`, through the web API: `test-registry` claims
  it and hands it on to `test-registry-heir`.
  - `claim`: A claim (`POST /namespaces/<id>/claim`) must get 400 with
    no token; 403 with another key's, leaving it unowned; 200 with its
    own key's, as `test-registry`, making it theirs; 200 again; and 409
    as `test-registry-heir`.
  - `owner-edit`: Once approved, its owner may change its description
    and institution alone: an edit that also changes its site name and
    key must change only the description. `test-registry-heir`'s edit
    must get 403.
  - `deny-resubmit`: Once denied, its owner's edit must resubmit it as
    Pending, still the owner's.
  - `invite-refusals`: An ownership invite
    (`POST /namespaces/<id>/ownership-invites`) must get 403 from
    `test-registry-heir`; 400 from the owner, with an `expiresIn` of
    `0s`, over 30 days, or `soon`; and 201 from the admin.
  - `invite-transfer`: The owner's single-use invite, redeemed by
    `test-registry-heir`
    (`POST /api/v1.0/invites/redeem/registration-ownership`), must make
    them the owner. The former owner's edit must then get 403, and the
    new owner's succeed.
  - `invite-single-use`: Redeemed again, by `test-registry`, it must be
    refused (400 or 404), and change nothing.
- `identity`: Each server's ID (`/api/v1.0/server/localMetadata`) must
  match the registry's for its prefix
  (`/api/v1.0/registry/server/<prefix>`; `lookup`). The registry must
  list each ID, and each name, once (`listed`). Skips under
  `-p topo-tiny`.
- `downtime`: Downtimes at the registry, for the first federated cache.
  Each but `admin-current`'s begins tomorrow. Skips without a federated
  cache, and under `-p topo-tiny`.
  - `mirrored`: One the cache's admin declares must reach the registry
    within 60 seconds, as the cache's.
  - `foreign-modify`: A token of the first origin's key must not change
    it, by PUT or DELETE (403).
  - `mirror-deleted`: Once the cache's admin deletes it, the registry
    must drop it within 60 seconds.
  - `bound`: One the origin declares at the registry, naming the cache,
    must be recorded as the origin's.
  - `source-registry`: One the origin declares with
    `"source": "registry"` must get 403.
  - `admin-current`: One the registry's admin declares for the cache,
    starting now: within 150 seconds, no director may name the cache,
    and each must list it as `filtered`; once it is deleted, every
    director must name the cache again within 150 seconds.
  - `future`: One the admin declares for the cache: within 150 seconds,
    each director must list it in `/api/v1.0/director_ui/downtimes` and
    `/api/v1.0/director_ui/servers/<name>/downtimes`, with its severity
    and `admin` as creator, and still name the cache.
