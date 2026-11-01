# Advertisements

Part of [What the tests check](README.md).

The [`advertise`](../../framework/suites/advertise.py) suite poses as the
servers, with their own keys, and checks that the directors and the broker
turn away each lie in an advertisement (ad) or token, and still serve the
real servers. A forged ad that a director takes stands until the server's
next, within 30 seconds (`Server.AdvertisementInterval`). The probe is an
object in the first protected [namespace](../glossary.md#namespace), or else
`/public`. The suite skips for a
[standalone origin](../../framework/presets/topo-standalone.sh). Every
[scenario](../glossary.md#scenario) but `fed-token-at-origin` runs in the
[WLCG pass](../glossary.md#pass) alone
([token profiles](README.md#token-profiles)).

- `forged-ads`: origin-0's and the first cache's own ads must be taken by
  every director (`origin-genuine`, `cache-genuine`). Each of these variants
  must be refused (403), and counted in
  `pelican_director_rejected_advertisements`. The first seven are tried as
  each server, as in `origin-no-token` and `cache-no-token`:

  - `no-token`: no token;
  - `unknown-key`: a token signed by [`unknown.pem`](authorization.md), a
    test key no server knows;
  - `no-scope`: a token without `pelican.advertise`;
  - `ns-prefix`: the probe's namespace as the registry prefix;
  - `other-audience`, `other-issuer`, `no-exp`: a token for another
    audience, from another issuer, or with no `exp`
    ([RFC 7519, sections 4.1.3](https://www.rfc-editor.org/rfc/rfc7519#section-4.1.3)
    and [4.1.4](https://www.rfc-editor.org/rfc/rfc7519#section-4.1.4); the
    [WLCG token profile](https://github.com/WLCG-AuthZ-WG/common-jwt-profile/blob/master/profile.md));
  - `cross-type`: a cache's ad, posted as an origin's;
  - `unregistered-ns`, `owner-claims`: origin-0's ad with an unregistered
    namespace, and under
    [`-p topo-multi-owner`](../../framework/presets/topo-multi-owner.sh),
    origin-2's claiming the probe's; no director may then name either server
    for it.

  Then every director must name both servers within 150 seconds
  (`restored`).

- `bad-broker-url`: origin-0's ad with a broker URL that Go's
  [`url.Parse`](https://pkg.go.dev/net/url#Parse) refuses (`http://[::1`)
  must get 400 from every director, with a JSON object as its body. Every
  director must still list its servers, and name origin-0 within 150
  seconds.

- `spoofed-status`: The first cache's own ad saying `shutting down`
  (`own-shutting-down`), then `critical` (`own-critical`), must take it out
  of every director's routing within 10 seconds. Under
  [`-p topo-multi-cache`](../../framework/presets/topo-multi-cache.sh), the
  second cache's key may not do that to the first; these ads may be taken or
  refused, but must leave the first cache named and unfiltered:

  - `name-shutting-down`, `name-critical`: its name, at the second's URL;
  - `name-downtime`: the same, during a downtime;
  - `url-hijack`: its name and URL, saying `critical`.

  After each, every director must name both caches within 150 seconds
  (`<case>-restored`).

- `fed-token`: The first director must give the first cache, for its
  advertise token, a federation token (`/api/v1.0/director/getFedToken`;
  `valid`) from the federation's issuer, for the cache's host, with any
  audience and `storage.read:/`, lasting 900 seconds
  (`Director.FedTokenLifetime`), and signed by a key of the federation's
  JWKS (unchecked under
  [`-p auth-external-issuer`](../../framework/presets/auth-external-issuer.sh)).
  It must refuse (403):

  - `no-host`, `no-stype`, `no-token`: a request without the host, the
    server type, or a token;
  - `stype-director`: a request for a director;
  - `no-scope`: a token without `pelican.advertise`;
  - `other-host`: the cache's token, for another server;
  - `unknown-key`: a token signed by `unknown.pem`;
  - `pending`: under
    [`-p registry-approval`](../../framework/presets/registry-approval.sh),
    a token of a cache prefix whose registration is pending.

- `fed-token-at-origin`: Under
  [`-p origin-no-direct`](../../framework/presets/origin-no-direct.sh), a
  [native](../glossary.md#native-and-xrootd) origin must require a
  federation token beside the user's. A GET of the probe must succeed with
  the `server` [credential](../glossary.md#credential)'s token and the
  cache's federation token in one header (`both`); get 401 with `server`'s
  alone (`user-only`); and with the federation token alone (`fed-only`), be
  refused (401 or 403), but in `/public`, succeed. Rows are named
  `<origin> <case>`. Skips where XRootD serves the origins.

- `discover-servers`: Each director's list of servers for
  [Prometheus](https://prometheus.io/)
  (`/api/v1.0/director/discoverServers`) must be refused (403) with no token
  (`no-token`), and with an untrusted issuer's (`unknown-issuer`). With the
  federation's (`federation`), it must list every origin and federated cache
  once, by web host and port, with its type, name, web URL, and a data URL
  on that host, and nothing else. Rows are named `<director> <case>`.

- `broker`: Under
  [`-p origin-broker`](../../framework/presets/origin-broker.sh), the broker
  (`/api/v1.0/broker/retrieve`) must refuse:

  - `cross-origin`: the first cache's token, asking for an origin's requests
    (401 or 403);
  - `no-token`: no token (401);
  - `unknown-key`: a token signed by `unknown.pem` (400, 401, or 403);
  - `bad-timeout`: a timeout that is not a duration (400);
  - `reverse`: the cache's token, reversing a connection to origin-0's
    prefix (`/api/v1.0/broker/reverse`; 400, 401, or 403).

  As origin-0, a request for a second must get `{"status":"timeout"}`
  (`valid`). Skips without a federated cache.
