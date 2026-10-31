# Federation

Part of [What the tests check](README.md).

The `federation` suite checks that the federation serves, and that its
servers know of each other. Each scenario is a row. The suite puts a
probe object at every origin in each exported namespace. The probe
namespace is the first protected namespace exported, or `/public` under
`-p origin-public`.

`test.py` runs `ready` and `caches` before any other suite. If the
federation never serves, or no cache does, the run stops; in a later
pass, each tries once and fails instead. `ready`, `caches`, `redirects`,
and `presence` run in both [passes](README.md#token-profiles); the rest
in the WLCG pass alone.

- `ready`: `pelican object get` must fetch the probe through the
  federation, byte for byte: from `/public` with no token where it is
  exported, and otherwise from the probe namespace with its `server`
  token. Tries 20 times, 15 seconds apart.

- `caches`: Each cache, a site-local one included, asked straight for
  the probe with the `server` token, must serve it. Tries each cache 20
  times, 15 seconds apart. Fails if only some serve. Skips a shape with
  no cache.

- `approval`: Under `-p registry-approval`, no director may name a
  server whose registration is pending. `ready` approves every pending
  registration before each try; the row fails if that fails. Skips
  elsewhere.

- `directors`: Each director, asked with no token where it would send a
  GET of the probe, must name, by its redirect or `Link`, within 60
  seconds:

  - for a cache (`/api/v1.0/director/object/`), every federated cache;
  - for an origin (`/origin/`), every origin;
  - for a direct read (`/origin/` with `?directread`), every origin
    where the namespace takes direct clients, and otherwise none (405).

  No answer may name a site-local cache. Under `-p topo-no-cache`, a
  read meant for a cache must go to an origin (307) where the namespace
  takes direct clients, and otherwise get 404. Skips a standalone origin
  and `-p topo-tiny`.

- `discovery`: A Pelican server that is the
  [discovery host](../glossary.md#discovery-host) must serve
  `/.well-known/pelican-configuration` with the endpoints in
  `framework/var/generated/discovery.json`, and a `jwks_uri` that holds
  one of its own keys. Skips where the `discovery` service is the host.
  [Director APIs](director-apis.md)' `metadata-hosting` checks which
  directors serve the document.

- `broker`: Under `-p origin-broker`, each director's answer for an
  origin's read of the probe must name origin-0's connection broker in
  `X-Pelican-Broker`: the discovery document's `broker_endpoint`, at
  `/api/v1.0/broker/reverse`, for origin-0's host and the probe
  namespace. Each director's `/api/v1.0/director_ui/servers` must give
  each origin that `brokerUrl`, and a cache none. Within 60 seconds.
  Under `-p origin-firewalled`, origin-0's
  `pelican_broker_connections_total` and
  `pelican_broker_object_requests_total` must each be at least 1. Skips
  elsewhere.

- `hints`: Each director's redirect of the probe, with no token, for a
  cache and for an origin, in each exported namespace, must carry
  `X-Pelican-Namespace`, naming the namespace and whether it requires a
  token, and `X-Pelican-Authorization`, naming the namespace's issuer
  alone, or none. `X-Pelican-Token-Generation`, if sent, must name the
  same. Skips a standalone origin. [Authorization](authorization.md)
  checks the same headers at a cache.

- `redirects`: A [plain HTTP client](../glossary.md#plain-http-client)
  that follows each director's redirects must land where the namespace
  allows, and be served there. Skips a standalone origin. See
  [redirects](redirects.md).

- `presence`: An object put at the last origin alone, in each exported
  namespace with its own storage there. Within 60 seconds, each
  director's answer for an origin's read of it, plain and with
  `?directread`, must name that origin alone
  (`Director.CheckOriginPresence`), or, for a direct read where the
  namespace takes no direct clients, get 405. Each cache must serve the
  object, and `pelican object get` must fetch it through a cache and,
  where the namespace takes direct clients, with `--direct`. Skips a
  shape of one origin, or of origins sharing a store (`-p origin-ssh`).

- `downtime`: A server its admin declares down must be named by no
  director, and by every one again once the downtime ends. See
  [downtime](downtime.md).
