# Restarts

Part of [What the tests check](README.md).

The [`restart`](../../framework/suites/restart.py) suite takes servers away
mid-run and checks what survives and what the rest of the federation does
meanwhile. It puts each server back, even after a failure, and runs after
every other suite but [`keys`](keys.md). It needs a
[control helper](../glossary.md#service-control-helper)
([service control](../../README.md#service-control)), and skips under
[`-p topo-tiny`](../../framework/presets/topo-tiny.sh). A
[case](../glossary.md#case)'s row is named `<scenario> <case>`, or
`<scenario> <service>`. `state-survives`, `ad-expiry`, `xrootd-wedged`, and
`peer-director` run in the [WLCG pass](../glossary.md#pass) only
([token profiles](README.md#token-profiles)).

- `cache-restart`: Each cache, [site-local](site-local-cache.md) included,
  restarted, must serve the objects it held from its store, intact: the
  origins' containers send under an object's worth of bytes meanwhile
  ([origins' bytes](../glossary.md#origins-bytes)), and a V2 cache sends
  `Age`.
  Every director must name a federated cache again within 150 seconds. A row
  per cache.

- `state-survives`: Origin-0, director-0, and the registry restart; after
  each but the registry, every director must name origin-0 within 150
  seconds (`restarts`). Then each server's ID and name (`ids`), and the
  registry's servers, none twice (`registrations`), must be as before. So
  must, at origin-0, a user's and group's IDs (`accounts`), an API token
  that reads `/metrics` (`api-token`), a downtime (`downtime`), and a
  refresh token of the namespace's issuer (`issuer`). Without the origins'
  own issuer, `issuer` skips, and the three before it are left out.

- `ad-expiry`: The first federated cache, and with several origins the last
  origin, is killed (SIGKILL). No director may name it after
  `Server.AdLifetime` (90 seconds here) and 30 more, even under
  [`-p topo-multi-director`](../../framework/presets/topo-multi-director.sh),
  whose peers forward ads. Restarted, it must be named again within 150
  seconds. A row per server.

- `backend-outage`: The origins' storage service (`s3`, `webdav`, or
  `lab-server`, by the [`-p origin-*`](../../README.md#presets) preset), a
  [helper service](../glossary.md#helper-service), is stopped. Meanwhile, a
  GET, HEAD, or PUT at origin-0 (`direct`, where it takes direct clients),
  and a cache miss through the first federated cache (`cache`), must get a
  server error, never 404, 405, or the object. Both Pelican clients, asking
  the director for an object that no cache holds, must fail, leaving no
  file, but not as not found (error 5011; `clients`). An `ssh` origin's
  health must report `ssh-backend` not ok within 60 seconds (`health`). With
  the service
  back, the origin must serve again within 120 seconds, without a restart,
  and its health say ok (`recovered`); if the origin exited, the suite
  restarts it, and `recovered` fails. Skips where the origin stores on its
  own disk.

- `xrootd-wedged`: A server whose XRootD answers no liveness probe for
  `Xrootd.LivenessMaxUnresponsiveTime` must shut XRootD down and exit.
  Origin-0, if XRootD serves it, and the first federated XRootD cache
  restart with that set to 20 seconds, then their XRootD is stopped
  (SIGSTOP). Health must turn `warning` or `critical`, and the container
  exit, within 90 seconds. Restarted, the server must be named by every
  director within 150 seconds. A row per server.

- `cache-failover`: With several federated caches, the first is paused, then
  killed. Each time, the [Pelican client](../glossary.md#pelican-client)'s
  `pelican object get` and [`stash_plugin`](../glossary.md#stash_plugin)
  must get an object through another cache, while the directors still name
  the first (`early`), and once they have dropped it, within
  `Server.AdLifetime` and 30 more (`late`). Restored, it must be named again
  within 150 seconds (`back`). Cases are named like `paused early`.

- `origin-failover`: The same with the last of several origins
  ([`-p topo-multi-origin`](../../framework/presets/topo-multi-origin.sh)),
  by two [routes](../glossary.md#route): direct (`direct`; not under
  [`-p origin-no-direct`](../../framework/presets/origin-no-direct.sh)), and
  through a cache (`cache`). A director with the namespace's token must stat
  origins before redirecting (`Director.CheckOriginPresence`). Cases are
  named like `killed late direct`, besides `paused back` and `killed back`.

- `peer-director`: With several directors, the second is killed. The first
  must list only itself at `/api/v1.0/director/directors` within
  `Server.AdLifetime` and 30 more, and both within 150 seconds of the
  second's restart.

- `director-failover`: Under
  [`-p director-ha`](../../framework/presets/director-ha.sh), each director
  in turn is killed, so its port refuses connections, which the load
  balancer then sends to the other. Meanwhile (`<director> down`), the
  Pelican client, `stash_plugin`, and a plain HTTP client, which asks the
  load balancer for the bare path and follows the redirect with its token,
  must each get an object through the load balancer, and, where
  `/protected-a` takes writes, put one, which some origin must then hold.
  Started again, the director must name origin-0 and the first federated
  cache within 150 seconds, and the clients must do the same again
  (`<director> back`). No director is paused: its port would still take
  connections, which a balancer that passes TLS through cannot tell from a
  slow director's.
