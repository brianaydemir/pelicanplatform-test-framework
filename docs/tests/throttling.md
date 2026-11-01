# Throttling

Part of [What the tests check](README.md).

The [`throttling`](../../framework/suites/throttling.py) suite makes the
servers slow while it runs. For `range-ahead`, it restarts every origin with
`Origin.TransferRateLimit: 256KB/s`. For the rest, it restarts every
federated [V2 cache](../glossary.md#native-and-xrootd) with a small fair
scheduler (`Cache.WorkerCount: 2`; `Cache.Throttle.PerOriginStarvingPercent`
and `PerOriginActivePercent` 25, `PerOriginPendingSize` 1, `RetryAfter` 2s),
which fetches one object at a time from an origin, queues one more, and
sheds the rest. It restores the servers afterward
([service control](../../README.md#service-control)).

It needs `posixv2` origins, which alone the rate limit slows, a federated V2
cache, and a [control helper](../glossary.md#service-control-helper). It
[skips](../glossary.md#skip) without them, and under
[`-p topo-tiny`](../../framework/presets/topo-tiny.sh). A
[case](../glossary.md#case)'s row is named `<scenario> <case>`.

- `range-ahead`: At each V2 cache, a cold read of a 4 MiB object (about 15
  seconds), and 2 seconds in, a read of its last 129 KiB. The range must
  come within 5 seconds, by a fetch of the cache's own, as 206 with exactly
  its bytes. The whole read, and a warm read, must return the object.

- `shed-429`: At each V2 cache, 30 cold reads of 1 MiB objects at once, then
  a read of a missing object. The cache must shed some with 429,
  `Retry-After: 2`, and a JSON `error` of `origin_unresponsive`,
  `origin_slow`, `cache_overloaded`, or `too_many_requests`, and serve every
  other read whole. The missing object must get 404.

- `client-throttled`: At each V2 cache, with the origin's slot and queue
  place taken, [`pelican object get --cache`](../glossary.md#pelican-client)
  (`pelican`) and [`stash_plugin`](../glossary.md#stash_plugin) with
  `Client.PreferredCaches` (`plugin`) each download a cold object. Each must
  fail as shed, leaving nothing:

  - with error 6008, 6009, or 6010
    ([Pelican](https://pelicanplatform.org/)'s
    [`docs/error_codes.yaml`](https://github.com/PelicanPlatform/pelican/blob/main/docs/error_codes.yaml));
  - with its `clientExitCode`: 9 for `pelican`, 11 (retryable) for the
    plugin;
  - for the plugin, with an ad saying `TransferRetryable = true`, whose
    error's `DeveloperData` has `RetryAfterSeconds = 2` and the 429's reason
    as `ThrottleReason`.

  Once the cache is free, a retry must get the object.

- `timeout-header`: Given `X-Pelican-Timeout`, a V2 cache must wait for a
  cold object's headers that long less a second (or half, if under a
  second), then answer 504 and go on fetching. At each V2 cache:

  - `short`: a cold read with `20ms`: 504 within 3 seconds;
  - `queued`: with that fetch holding the origin's slot, a cold read with
    `2s`: 504 within 3 seconds;
  - `held`: a read with `5s` of a held object: served;
  - `malformed`: a cold read with `soon`: served.

  The cache must then come to hold `short`'s and `queued`'s objects.

- `streaming`: At each V2 cache, a cold read of a 2 MiB object must bring
  its first 4 KiB within 5 seconds, and in under half the time of the whole.

- `slow-transfer`: The [clients](../glossary.md#client) must give up with
  error 6002 on a transfer slower than `Client.MinimumDownloadSpeed` for
  `Client.SlowTransferWindow`, after `Client.SlowTransferRampupTime`. With
  these at 1 MiB/s, 5s, and 2s, a 4 MiB read:

  - `direct`: `pelican object get --direct` must fail with 6002, exit 9, and
    leave nothing;
  - `plugin-direct`: `stash_plugin` with `?directread` likewise, but exit
    11, its ad saying `TransferSuccess = false` and
    `TransferRetryable = true`, with a `PelicanErrorCode` of 6002;
  - `cache`: through each V2 cache, cold, as `direct`;
  - `default`: with the default thresholds, each client must get such an
    object, direct where origins take direct clients.

  `direct` and `plugin-direct` skip where origins take no direct clients.
