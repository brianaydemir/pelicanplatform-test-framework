# Throttling

Part of [What the tests check](README.md).

No server is slow as a shape is, so the `throttling` suite makes them
so for as long as it runs. It restarts every origin with
`Origin.TransferRateLimit: 256KB/s` (a KB is 1,024 bytes), and every
federated V2 cache with a small fair scheduler: `Cache.WorkerCount: 2`,
`Cache.Throttle.PerOriginStarvingPercent` and `PerOriginActivePercent`
25, `PerOriginPendingSize` 1, and `RetryAfter` 2s. A cache then fetches
from one origin one object at a time, queues one more, and sheds the
rest. Both go in the last of the servers' configuration layers
([service control](../../README.md#service-control)); afterward the suite
restores them, and waits, before and after, until every director names
every origin and every cache answers, as `freshness` does. The rate limit
slows only a `posixv2` origin (`origin_serve/rate_limited_fs.go` in
Pelican), so the suite needs `posixv2` origins, a federated V2 cache,
and a control helper, and skips the `tiny` topology, whose servers are
one process. Otherwise it skips, and says why.

The limit is one bucket per export, which every read and write of it
shares, and which lets a second's worth through at once. So the cases
run one at a time, and the suite writes its objects straight into the
origin's store on disk, under `/protected-a/data/throttling/<run>/`, a
set per cache, so that each is cold where it is first read. Only the
first origin holds them, so that a director with several sends every
fetch there, and a cache's fetches share one scheduler queue, which is
per origin host. A scenario with several cases records a row per case,
named `<scenario> <case>`.

- `shed-429`: at each V2 cache, 30 reads at once of cold 1 MiB objects,
  each of which takes the cache seconds to fetch, and half a second in,
  a read of an object that no origin holds. The cache must shed some:
  429, with `Retry-After: 2`, and a JSON body whose `error` is
  `origin_unresponsive`, `origin_slow`, `cache_overloaded`, or
  `too_many_requests` (`handleError` in
  `local_cache/persistent_cache_api.go`). Every other read must come
  back whole. The missing object must be 404: a 429 would have a client
  come back for ever for what does not exist. Where there is one origin,
  the director sends the cache to it without asking whether it holds
  the object (`Director.AssumePresenceAtSingleOrigin`), and the cache
  then asks its scheduler for a fetch, which it may shed before the
  origin says 404, so the row may fail there: on v26.0.0-rc.3, in the
  default shape, it does (`origin_slow`).
- `client-throttled`: at each V2 cache, a cold read of a 4 MiB object
  holds the origin's one slot, and another its one place in the queue.
  Meanwhile, `pelican object get --cache` and `stash_plugin` (with
  `Client.PreferredCaches`) each download a cold object through that
  cache alone. Each must fail as shed: error 6008, 6009, or 6010
  (`Transfer.OriginUnresponsive`, `OriginSlow`, or `CacheOverloaded`,
  `docs/error_codes.yaml`), exit 11, which says the failure may be
  retried (`cmd/object_get.go`, `cmd/plugin.go`), and nothing left in
  its directory. The plugin's ad must say `TransferRetryable = true`,
  and its error's `DeveloperData` must carry `RetryAfterSeconds = 2` and
  `ThrottleReason`, the 429's reason. The client does not wait on
  `Retry-After`, and has no other cache to try, so the test, as a
  retrier outside it would, tries it again once the reads are done, 2
  seconds apart, up to five times, and it must then get the object. A
  client that was never shed leaves its row inconclusive. `pelican
  object get` first asks for one byte (`Range: bytes=0-0`), which a V2
  cache serves by fetching only the blocks it reads, and those after it
  has answered; so whether it sheds that request with a 429 is less
  plain from the code than for the plugin's whole read: on
  v26.0.0-rc.3, in the default shape, the `pelican` row fails, the
  download failing by something other than a shed.
- `timeout-header`: a client says with `X-Pelican-Timeout` how long it
  will wait, and a V2 cache waits for a cold object's headers that long
  less a second, or half of it if under a second, and then answers 504,
  while the fetch goes on (`serveObject` in
  `local_cache/persistent_cache_api.go`). At each V2 cache: `short`, a
  cold read of a 2 MiB object with `20ms` must get 504 within 3 seconds;
  `queued`, 2 seconds later, while that fetch holds the origin's slot,
  a cold read of another with `2s`, which must wait behind it, must get
  504 within 3 seconds (if the cache sheds it instead, or serves it at
  once, the row is inconclusive); and then the cache must come to hold
  each object, as `only-if-cached` shows, and serve it. `held`: an
  object the cache holds, with `5s`, must come back. `malformed`: a cold
  read with `soon`, no duration, must come back, the value ignored.
- `streaming`: at each V2 cache, a cold read of a 2 MiB object, which
  takes about 7 seconds to come: its first 4 KiB must come within 5
  seconds, and in less than half the time of the whole, which must be
  the object. A cache that sent nothing until it held the whole object
  would send its first bytes with its last. A whole read in under about
  3.5 seconds shows nothing, since the origins were not slow, and
  leaves the row inconclusive.
- `slow-transfer`: the clients give up on a transfer that stays slower
  than `Client.MinimumDownloadSpeed` for `Client.SlowTransferWindow`,
  once `Client.SlowTransferRampupTime` has passed, with error 6002
  (`Transfer.SlowTransfer`; `downloadHTTP` in `client/handle_http.go`).
  With `PELICAN_CLIENT_MINIMUMDOWNLOADSPEED=1048576`,
  `PELICAN_CLIENT_SLOWTRANSFERRAMPUPTIME=2s`, and
  `PELICAN_CLIENT_SLOWTRANSFERWINDOW=5s`, four times the origins' rate,
  a 4 MiB object, which would take about 15 seconds: `direct`, `pelican
  object get --direct` must fail with 6002, exit 11, and leave nothing;
  `plugin-direct`, `stash_plugin` with `?directread` likewise, its ad
  saying `TransferSuccess = false`, `TransferRetryable = true`, and an
  error of `ErrorType = "Transfer"` whose `PelicanErrorCode` is 6002.
  Both skip where the origins take no direct clients. `default`: with
  the default thresholds (100 KiB a second, after 100 seconds, for 30),
  each client must get another such object, direct, or where the
  origins take no direct clients, through the first V2 cache. `cache`:
  `pelican object get --cache` through each V2 cache, cold, with the
  thresholds, must fail as `direct` does.
