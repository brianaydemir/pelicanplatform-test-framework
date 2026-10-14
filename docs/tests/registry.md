# Registry

Part of [What the tests check](README.md).

The registry records which key owns which namespace, and serves those
keys to every director and cache: a flaw there lets one party claim
another's namespace. The `registry` suite tests it directly
(`registry/` in Pelican). It skips a standalone origin, which has no
registry. Each case is a row, named `<scenario> <case>`.

Every registration the suite makes has `registry-test-<run>` in its
prefix (e.g. `/registry-test-<run>-del`), and in its site name, and
every downtime it declares has it in its description. Before its
scenarios, and after them, the suite removes every registration,
server, and downtime with `registry-test-` in it, of any run, so that
a failed run leaves nothing for a later one, or a director, to trip
over: downtimes at each server, as its admin, and at the registry, as
its admin, or for a server's, as that server; then servers and
registrations, as the registry's admin. It never touches the shape's
own, but for `register again`, which must leave it as it was. It makes
keys of its own (EC P-256, with `openssl`), and signs its tokens for
the registry with them, or with a server's own key, through
`framework/testlib/jwt.py`.

- `register`: registering prefixes. `pelican namespace register`
  reports only an exit status, so most rows speak the registry's
  protocol themselves, as the client does
  (`framework/testlib/registry.py`; `keySignChallenge` in
  `registry/registry.go`): a nonce and the client's JWKS, then both
  nonces signed with the JWKS's first key. The registry must answer
  201 for a prefix it registers, and 200 for one already registered.
  Below, `<t>` is `registry-test-<run>`.
  - `invalid`: `/api/<t>`, `/view/<t>`, `/pelican/<t>`, `/<t>/./b`,
    `/<t>/../b`, `/.<t>`, and `/` must get 400 (`validatePrefix`, and
    Pelican's `TestValidatePrefix`).
  - `child-foreign`, `child-owner`, `sibling`: with key chaining on
    (`Registry.RequireKeyChaining`, by default), a prefix inside a
    registered one may be registered only with a key of the outer one
    (`validateKeyChaining`). `/protected-a/<t>` with a key no one has
    registered must get 403, and with the origins' own key, 201;
    `/protected-a-<t>` is no child of `/protected-a`, and must take
    any key. Under `-p topo-multi-owner`, chaining is off (`fed.sh`),
    so `/protected-a/<t>` must take any key, and `child-owner` skips.
  - `like-underscore`, `like-percent`: the registry finds a prefix's
    superspaces with SQL `LIKE` (`namespaceSupSubChecks` in
    `registry/registry_db.go`), in which `_` and `%` are wildcards. So
    with `/<t>-a_b` registered with one key, `/<t>-aXb/c` with another
    must still get 201, since `/<t>-a_b` does not enclose it; likewise
    `/<t>-pXYZq/c` beside `/<t>-p%q`. Pelican takes the decoy for a
    parent, and refuses them (403), so both rows fail, on v26.0.0-rc.3
    and main alike. They skip where chaining is off.
  - `again`: `/protected-a`, registered again with another key, must
    get 200, "already registered", and its registered keys and JWKS
    must be as they were.
  - `delete-foreign`, `delete-owner`, `delete-unknown`: a DELETE of
    `/api/v1.0/registry/<prefix>` takes a token of
    `pelican.namespace_delete` signed with a key registered for the
    prefix, as `pelican namespace delete` mints one
    (`deleteNamespaceHandler`). Signed with another key, it must get
    403; with the prefix's own, 200, after which the prefix's JWKS
    must get 404. A prefix never registered must get 400, as one JSON
    document. Pelican writes that answer and goes on, and writes
    another, so the body is two, and `delete-unknown` fails, on
    v26.0.0-rc.3 and main alike.
  - `approval`: a new registration starts Incomplete
    (`keySignChallengeCommit`), and its keys are served at once; but
    under `-p registry-approval`, its JWKS must get 403 until the
    admin approves it, and then its keys.
  - `cli`: `pelican namespace register` and `delete`, configured with
    a key of the run's (`IssuerKeysDirectory`) and a site name
    (`Xrootd.Sitename`), must register the prefix with that key alone,
    and delete it.
  - `cli-flags`: `pelican namespace register --privkey <key> --pubkey
    <key> --namespace-url <registry>` must register the prefix with
    `--privkey`'s key. Pelican binds `--privkey` to `IssuerKey`, and
    `--namespace-url` to `Federation.RegistryUrl`, only if each has a
    value when the command is defined, which it never does
    (`cmd/registry_client.go`), and `--pubkey` is read by nothing. So
    the client signs with a key it makes in its empty key directory,
    and the row fails, on v26.0.0-rc.3 and main alike. The dev
    container's configuration names the registry anyway, so no row
    shows `--namespace-url` ignored.
- `jwks`: the keys the registry serves for each prefix
  (`/api/v1.0/registry/<prefix>/.well-known/issuer.jwks`; the wildcard
  handler in `registry/registry.go`). A server registers the keys it
  publishes itself (`GetIssuerPublicJWKS`): those of its key directory
  (`framework/var/issuer-keys/<service>/`), and where its configuration
  sets `Server.IssuerJwks` (origin-0, origin-1, and `tiny`'s server),
  that file's. Keys are compared by their
  thumbprints, computed from each key's members, not taken from its
  `kid`.
  - `exports`: each export's JWKS must hold exactly the keys its origin
    publishes at its own `/.well-known/issuer.jwks`, which must include
    every key in its key directory: origin-0's, or another owner's own.
  - `servers`: so must each origin's server prefix's
    (`/origins/<host>:<port>`). A cache's (`/caches/<name>`) must hold
    only keys the cache publishes, and at least one: a cache never
    tells the registry of a key it adds since it registered, unlike an
    origin (`updateNamespacesPubKey`), which the suite does not check.
  - `openid`: each prefix's
    `/.well-known/openid-configuration` must name that JWKS as its
    `jwks_uri`.
  - `unknown`: a prefix never registered must get 404 for both.
  - `private`: no JWKS the federation serves may hold private key
    material (`d`, `p`, `q`, `dp`, `dq`, or `qi`): each server's own,
    each director's, and the registry's; each namespace issuer's, by
    its openid-configuration; the registry's for each prefix; and the
    registrations' keys, as the registry lists them all
    (`/api/v1.0/registry`), serves each
    (`/api/v1.0/registry/<prefix>`), hands its admin each
    (`/api/v1.0/registry_ui/namespaces/<id>/pubkey`), and lists them
    with its servers (`/api/v1.0/registry_ui/servers`). Each must be a
    JWKS.
- `registry-ui`: the registry's web API (`/api/v1.0/registry_ui`;
  `registry/registry_ui.go`). It registers two prefixes of the run's,
  and the admin approves one.
  - `list-public`: `/namespaces` with no credentials must list only
    Approved registrations, without their `pubkey`: the approved one,
    and not the other, which is Incomplete.
  - `list-pending`: `?status=Pending` with no credentials must get 403.
  - `prefix-type`: `?prefixType=bogus` must get 400.
  - `bad-status`: as the admin, `?status=Bogus` must get 400, as one
    JSON document. Pelican writes that answer and goes on, and writes
    the list too (`listNamespaces`), so the body is two, and the row
    fails, on v26.0.0-rc.3 and main alike.
  - `non-owner`: an ordinary user, `test-registry`, whom the admin
    creates once, and later runs reuse, must get 403 for
    `/namespaces/<id>` of `/protected-a`'s registration, which no user
    owns.
  - `unknown-id`: as the admin, approving or denying a registration
    that doesn't exist must get 404.
  - `denied`: under `-p registry-approval`, a registration the admin
    has denied must have its JWKS refused (403). Elsewhere the
    registry serves every registration's keys, and the row skips.
  - `servers`: `/servers` must list each of the shape's server
    prefixes as a registration of one server, once, and no name twice.
  - `server-one`: `/servers/<id>` must give each of those servers, and
    an ID no server has must get 404.
- `completeness`: a server's registration starts Incomplete
  (`keySignChallengeCommit`), and the completeness check
  (`POST /api/v1.0/registry/namespaces/check/status`;
  `checkStatusHandler`) must say it is not `complete`, with an
  `edit_url` of a page of the registry's for it. Its holder completes
  it through the web API (`PUT /api/v1.0/registry_ui/namespaces/<id>`;
  `createUpdateNamespace`), with an `access_token` of
  `registry.edit_registration` signed by the registration's key, by
  which a logged-in user who doesn't own it claims it. A claim is for
  good, so the suite completes a server registration of the run's own,
  `/origins/registry-test-<run>.invalid`, which it registers with a
  key of its own, as a server does; it would not undo a claim of one of
  the shape's. With neither `Registry.Institutions` nor
  `Registry.InstitutionsUrl` set, the registry takes any institution
  (`validateInstitution`).
  - `check-status`: each of the shape's server registrations must be
    incomplete, with an `edit_url`, and where nothing needs approval,
    Incomplete. (Under `-p registry-approval`, the `federation` suite
    has approved them.)
  - `incomplete`: the run's server registration must be Incomplete,
    unowned, and incomplete, with the claim page as its `edit_url`.
  - `foreign-token`: its completion by `test-registry`, with a token
    signed by another key, must get 403, and leave it as it was.
  - `own-token`: with a token signed by its own key, and a body that
    names another owner, it must succeed; the registration must then
    be `test-registry`'s, not the body's owner's, Pending, and
    complete, with the origin's edit page as its `edit_url`.
- `ownership`: who owns a registration, and what that lets them do
  (`registry/registry_ui.go`), on a registration of the run's own,
  `/registry-test-<run>-own`, which `test-registry` claims and hands
  on to `test-registry-heir`, another ordinary user, whom the admin
  creates once and later runs reuse. Where an owner edits, it is
  through `PUT /namespaces/<id>`, with the registry's CSRF token.
  - `claim`: a registration starts unowned, and the holder of its key
    claims it (`POST /namespaces/<id>/claim?access_token=...`,
    `claimNamespace`), with a token of `registry.edit_registration`
    signed with the key: with no token, 400; with another key's, 403,
    leaving it unowned; with its own, as `test-registry`, 200, after
    which it is `test-registry`'s; again, 200; and then as
    `test-registry-heir`, 409, leaving it `test-registry`'s.
  - `owner-edit`: once the admin approves it, its owner may change its
    description and its institution, and nothing else
    (`applyOwnerEdits`): an edit that also sends another site name,
    another key, and the same prefix must succeed, change the
    description, and leave the site name, prefix, and key as they
    were, the registry still serving the key alone, and the
    registration Approved; `test-registry-heir`'s edit must get 403,
    and change nothing.
  - `deny-resubmit`: once the admin denies it, its owner's edit must
    succeed, and resubmit it as Pending, still the owner's
    (`createUpdateNamespace`).
  - `invite-refusals`: an invite to take it over (`POST
    /namespaces/<id>/ownership-invites`,
    `createRegistrationOwnershipInvite`) is the owner's or the admin's
    to mint: `test-registry-heir`'s must get 403; the owner's with an
    `expiresIn` of `0s`, longer than 30 days
    (`maxOwnershipInviteExpiry`), or no duration, 400; and the admin's,
    201, with a token.
  - `invite-transfer`: the owner's invite, 201, must say that it is
    single-use; `test-registry-heir` redeems it (`POST
    /api/v1.0/invites/redeem/registration-ownership`, with the token;
    `handleRedeemRegistrationOwnershipInvite`), 200, naming the
    registration's ID and prefix, after which the registration is
    theirs; the former owner's edit must then get 403, and the new
    owner's succeed.
  - `invite-single-use`: the same token, redeemed again by
    `test-registry`, must be refused (400 or 404), and the registration
    stay `test-registry-heir`'s.
- `identity`: each server's ID, as it records it
  (`/api/v1.0/server/localMetadata`, as its admin), must be the one
  the registry gives for its server prefix
  (`/api/v1.0/registry/server/<prefix>`; `lookup`), and the registry's
  list of servers must list it once, and its name once (`listed`). It
  skips in the tiny topology, whose origin and cache are one process,
  with one record of itself.
- `downtime`: downtimes recorded at the registry
  (`web_ui/server_api.go`). A server's admin declares one at the
  server, which tells the registry; a registered server may declare
  one at the registry with a token of its own (subject its server ID,
  audience the registry's host and port, and
  `pelican.downtime_create`, `_modify`, or `_delete`;
  `checkRegisteredServer` in `token/token_verify.go`), but only its
  own; and the registry's admin may declare any, which the directors
  apply to routing (`director/cache_ads.go`). It uses the first
  federated cache and the first origin, and skips in the tiny topology,
  as `federation`'s `downtime` does. A downtime's times are in
  milliseconds. What it declares at the cache, and as the origin,
  begins tomorrow, so that it changes no routing.
  - `mirrored`: one the cache's admin declares must reach the
    registry within 60 seconds, as the cache's, with the cache's ID.
  - `foreign-modify`: the origin's token must not change it, by PUT
    or DELETE (403).
  - `mirror-deleted`: once the cache's admin deletes it, the registry
    must not have it within 60 seconds.
  - `bound`: one the origin declares at the registry, naming the
    cache's ID and name in its body, must be recorded as the origin's,
    with its ID and name.
  - `source-registry`: one the origin declares with `"source":
    "registry"` must get 403.
  - `admin-current`: one the registry's admin declares for the cache,
    now: within 150 seconds, no director may name the cache, and each
    must list it in `/api/v1.0/director_ui/servers` as `filtered`; once
    the admin deletes it, within 150 seconds every director must name
    the cache again.
  - `future`: one the admin declares for the cache, beginning
    tomorrow: within 150 seconds, each director must list it in
    `/api/v1.0/director_ui/downtimes`, and in
    `/api/v1.0/director_ui/servers/<name>/downtimes`, with its
    severity, and `admin` as who created it, and still name the cache.
