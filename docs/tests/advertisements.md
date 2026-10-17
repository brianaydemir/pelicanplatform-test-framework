# Advertisements

Part of [What the tests check](README.md).

Every origin and cache tells the directors what it serves by posting an
advertisement, signed with its own key, which the registry holds for
its prefix (`/origins/<host>` or `/caches/<host>`); a director checks the
token against the registry's copy, and that it carries
`pelican.advertise` (`registerServerAd` in `director/director.go`,
`verifyAdvertiseToken` in `director/origin_api.go`). A cache trades the
same token for a federation token, which an origin that takes no direct
clients requires beside a user's; and an origin behind a firewall takes
its connections through the director's broker. The `advertise` suite
plays a caller who lies at each, and checks that each turns it away,
and serves the real thing. It skips for a standalone origin, which has
no director.

The suite signs as each server with the key of
`framework/var/issuer-keys/<host>/` that the registry holds for its
prefix. Some ads it posts are taken, as the server's own: each is built
from what a director says of the server
(`/api/v1.0/director_ui/servers/<name>`), lasts 90 seconds, and is
replaced by the server's next ad, within `Server.AdvertisementInterval`
(30 seconds here). After each scenario that changes where the directors
send clients, every director must name each server it touched again
within 150 seconds. The directors are asked about an object that the
suite puts at every origin, in the first protected namespace, at
`data/advertise/<run>-probe`, and removes afterward.

- `forged-ads`: origin-0's and the first cache's own ads, posted as
  each to every director, must be taken (200). Then variants, each
  changed in one way, must each be refused (403) by every director, and
  counted among the ads it refused
  (`pelican_director_rejected_advertisements`): with no token; signed
  by `unknown.pem`; with the server's key but not `pelican.advertise`;
  with the server's key and scope, but for another audience
  (`https://elsewhere.invalid`), from another issuer (the same), or
  with no `exp`, each of which RFC 7519 (sections 4.1.3 and 4.1.4) and
  the WLCG token profile have a verifier refuse (`other-audience`,
  `other-issuer`, `no-exp`); with `/protected-a` as its registry
  prefix; a cache's ad posted as an origin's (`registerOrigin`), with
  its own key; origin-0's ad with a namespace that no one registered,
  after which no director may name origin-0 for it; and under
  `-p topo-multi-owner`, origin-2's ad claiming `/protected-a`, after
  which no director may name origin-2 for it. A director counts no ad
  that comes without a token (`registerServerAd` returns first), so the
  `no-token` rows fail. It checks an advertise token's signature,
  against the registry's keys for the server's prefix, and its scope,
  and nothing else of it (`verifyAdvertiseToken` in
  `director/origin_api.go`), and jwx validates `exp` only if a token has
  one, so a server's advertise token is good at any director, from any
  issuer, and without `exp` for as long as its key is: the
  `other-audience`, `other-issuer`, and `no-exp` rows fail, on
  v26.0.0-rc.3 and main alike.
- `bad-broker-url`: origin-0's own ad, but with a broker URL that Go's
  `url.Parse` refuses (`http://[::1`), must get 400, with one JSON body,
  and every director must then still list its servers and name origin-0.
  Pelican writes the 400 and goes on to read the URL it could not parse
  (`finishRegisterServeAd`), which panics; gin recovers, and the client
  sees only the 400.
- `spoofed-status`: with the first cache's own key, its ad saying that
  it is `shutting down`, and then `critical`, must take it out of every
  director's routing within 10 seconds, until its next ad. Under
  `-p topo-multi-cache`, the second cache's key and prefix may not do
  that to the first: an ad naming the first, at the second's URL,
  saying each status, or with a downtime under way, may be taken or
  refused, but must leave the first named, and unfiltered in the
  director's list of servers (`downtimeNameAuthorized`, v26.0.0-rc.0 and
  later). Nor may an ad at the first's URL, with its name, saying it is
  `critical`: a director keeps an ad by its URL, whoever signed it
  (`recordAd` in `director/cache_ads.go`), and keeps a critical cache
  out of routing (`cacheNotInErrorState` in `director/sort.go`), so the
  `url-hijack` row fails, on main and v26.0.0-rc.3 alike.
- `fed-token`: the first director must give the first cache a
  federation token for its own advertise token
  (`/api/v1.0/director/getFedToken?host=<host>&sType=Cache`): from the
  federation (`iss` its discovery URL), for the cache (`sub` its host),
  for any audience, with `storage.read:/`, lasting 900 seconds, and
  signed by a key of the federation's JWKS (but under
  `-p auth-external-issuer`, whose JWKS lacks the directors' keys, the
  signature is not checked). It must refuse (403) a request without the
  host, without the server type, or without a token; for a director;
  with the cache's key but not `pelican.advertise`; with the cache's
  token for another server (the second cache, or origin-0); and with a
  token signed by `unknown.pem`. Pelican answers a token it cannot
  verify with 500 (`getFedToken` in `director/fed_token.go`), so the
  last two rows fail. Under `-p registry-approval`, the registry's
  admin registers a cache's prefix of the run's, which stays pending,
  with a key made for it; a token signed with that key must be refused
  (403); then the registration is removed.
- `fed-token-at-origin`, under `-p origin-no-direct`: a native origin
  serves a request only with a federation token beside the user's
  (`authMiddleware` in `origin_serve/handlers.go`). A GET of the probe
  straight to each origin, with the `server` token and the first
  cache's federation token in one header, must get it; with the
  `server` token alone, 401; and with the federation token alone, which
  grants nothing in a protected namespace, a refusal (401 or 403).
- `discover-servers`: each director's list of servers for Prometheus
  (`/api/v1.0/director/discoverServers`) must be refused (403) with no
  token, and with one of its scope (`pelican.director_service_discovery`)
  from an issuer it does not trust; with one from the federation (its
  discovery URL), signed with a key of the federation's JWKS, it must
  list every origin and federated cache of the shape once, by its web
  URL's host and port, with its type, its name, its web URL, and a data
  URL on the same host, and nothing else.
- `broker`, under `-p origin-broker`: the director's broker hands an
  origin the requests to connect to it (`/api/v1.0/broker/retrieve`),
  with a token from the registry's issuer for the caller's prefix,
  signed with its key. As the first cache, a request for an origin that
  is not its own must be refused (401 or 403); Pelican checks the token
  against the prefix the caller names, but hands it the requests of
  whatever origin it names (`retrieveRequest` in
  `broker/server_apis.go`), so the row fails there. With no token, it
  must get 401; with a token signed by `unknown.pem`, a refusal (400,
  401, or 403); with a timeout that is not a duration, 400; and as
  origin-0, for a second, `{"status":"timeout"}`. A request to reverse a
  connection to origin-0's prefix (`/api/v1.0/broker/reverse`), with the
  cache's token, must be refused (400, 401, or 403). The origin these
  requests name is made up, so that none takes a request from
  origin-0's queue.
