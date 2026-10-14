# Restarts

Part of [What the tests check](README.md).

Production servers restart and fail all the time. The `restart` suite
takes servers away in the middle of a run, through the control helper
([service control](../../README.md#service-control)), and checks what
survives and what the rest of the federation does meanwhile; it puts
each back, even after a failure, and comes after every other suite but
`keys`. It needs a control helper, and skips the tiny topology, whose
servers are one process. Its objects go under
`/protected-a/data/restart/<run>/`, put at every origin, and are
removed afterward. A scenario with several cases records a row per
case, named `<scenario> <case>`, or `<scenario> <service> <case>`. The
`transfer-api` suite's `restart` covers a transfer job interrupted by a
restart, and `freshness`'s `stale-if-error` a stale copy served while
every origin is stopped.

- `cache-restart`: each cache, the site-local one included, holds three
  1 MiB objects read whole (a V2 cache says so with `Age`). Restarted,
  and once every director names it again (within 150 seconds, timed in
  the note), it must serve each from its store, intact, with `Age` for
  a V2 cache, and the native origins must have sent less than an
  object's worth of bytes for GETs meanwhile (`pelican_http_bytes_total`;
  `selftest`'s `restart-cache` does the same for cache-0 and one
  object). An XRootD cache sends no `Age`, so there the origins'
  counters alone tell; over XRootD origins, which keep none, the bytes
  alone must be right.
- `state-survives`: before origin-0, director-0, and the registry
  restart, the suite records each origin's and cache's ID and name, as
  it records them itself (`/api/v1.0/server/localMetadata`), and the
  servers the registry lists; and at the issuer origin, with the
  origins' own issuer (the `issuer` row and the rest below skip with
  the external one), it makes a user, a group, an API token of the
  `monitoring.scrape` scope, a downtime declared for tomorrow, a client
  of the namespace issuer's, and a refresh token through it. Each
  restart must end with every director naming origin-0, or for
  director-0 every server, again within 150 seconds (`restarts`).
  Afterward each server's ID and name must be as
  recorded (`ids`); the registry must list the same servers, none twice
  (`registrations`); the user and the group must be there, with their
  IDs (`accounts`); the API token must still read `/metrics` and be
  listed (`api-token`); the downtime must still be declared
  (`downtime`); and the refresh token must refresh, with the client's
  secret (`issuer`): the issuer signs its tokens with a key it derives
  from a master key in its database (`oauth2/issuer/provider.go`).
  What the suite made is removed afterward.
- `ad-expiry`: the first federated cache, and with several origins the
  last origin, killed (SIGKILL, so it says nothing to the directors):
  within `Server.AdLifetime` (90 seconds here) and 30 more, no director
  may name it, since its ad has expired (the directors keep ads in a
  cache with that TTL, `director/cache_ads.go`; under
  `-p topo-multi-director`, a peer's forwarded ad must not keep it
  alive); started again, every director must name it within 150
  seconds. The note times both.
- `backend-outage`, where the origins' storage is a service of the
  framework's (`s3` under `-p origin-s3v2`, `webdav` under
  `-p origin-httpsv2`, `lab-server` under `-p origin-ssh`): with it
  stopped, at origin-0, where it takes direct clients, a GET and a HEAD
  of an object, and a PUT of a new one, must each get a server error
  (5xx), never 404 or 405, nor the object's bytes (`direct`); through
  the first federated cache, a GET of an object it does not hold must
  get a server error too, not 404 (`cache`); and an `ssh` origin's
  health must say its `ssh-backend` is not ok within 60 seconds
  (`health`; the other backends have no component). Once the service
  is back, the origin must serve the object again within 120 seconds,
  without a restart, and its HEAD, a PUT, and the cache's read must
  work (`recovered`), and the health component say ok. An `httpsv2`
  origin whose `OPTIONS` probe of its backend fails takes it for a
  plain HTTP server, and so answers a missing backend as missing
  objects (O12; `probeBackendMode` in `origin_serve/backend_https.go`),
  so its `direct` and `cache` rows are expected to fail; this comes
  from reading the code, not from a run. It skips where the storage is
  the origin's own, or XRootD's.
- `cache-failover`, with several federated caches: the first is paused
  (its ports take connections that nothing answers), and then killed
  (they refuse). Each time, `pelican object get` and stash_plugin of
  an object of their own through the federation must succeed, served
  by another cache, while the directors still name the first (`early`;
  inconclusive if they had already dropped it), and once they have
  dropped it, within `Server.AdLifetime` and 30 more (`late`). The note
  says which caches each client tried: the client probes each
  endpoint for a second before it downloads, so a cache that answers
  nothing may be tried last rather than fail. Afterward the cache is
  unpaused, or started, and every director must name it again within
  150 seconds (`back`).
- `peer-director`, with several directors: the second killed, the first
  must list only itself at `/api/v1.0/director/directors` within
  `Server.AdLifetime` and 30 more, since its peer's ad has expired
  (`director/director_advertise.go`); once the second starts again,
  the first must list both within 150 seconds. The note times both.
