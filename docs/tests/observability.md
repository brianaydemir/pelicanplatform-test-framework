# Observability

Part of [What the tests check](README.md).

The `observability` suite checks each server's health report, metrics,
log level, and log tail, by its web URL. It acts as each server's admin
and as `test-webapi`, an ordinary user, as the [web API](web-api.md)
suite does. Rows are named `<scenario> <server> <case>`, but
`health-metrics`'s are `<scenario> <server>`, and `http-metrics`'s
`<scenario> <case>`. Only `http-metrics` and `log-redaction` run in both
passes ([token profiles](README.md#token-profiles)).

The framework leaves `Monitoring.MetricAuthorization` and
`Monitoring.PromQLAuthorization` on. The suite raises each server's log
level for at most a minute. `log-level-xrootd` restarts XRootD twice at
each server it checks.

- `health-api`: Each server's health report
  (`/api/v1.0/metrics/health`).

  - `components`: As the admin reads it, the report must say `ok`
    overall and of each component. It must list `web-ui`; at an origin,
    and at a cache the directors know of, `federation`, `director`, and
    `registry`, none of which a standalone origin may list; and where
    XRootD serves, `xrootd` and `config-updates`. A report that fails by
    `storage` alone, at 80% full disks
    (`Monitoring.StorageWarningThreshold`), is inconclusive.
  - `access`: The report must get 401 with no credentials, 403 as
    `test-webapi`, and 200 as the admin, or 200 for all where
    `Server.HealthMonitoringPublic` is on. `/api/v1.0/health` must
    answer anyone with 200.

- `health-metrics`: Each server's `pelican_component_health_status` must
  agree with its health report, read before and after (`ok` is 5,
  `warning` 4, and `critical` 2), and name no other component.

- `metric-authorization`: At each server:

  - `metrics`: `/metrics` must get 401 or 403 with no token, or with one
    signed by another key; 200 and the exposition with a token of the
    server's local issuer and scope `monitoring.scrape`, or with the
    admin's login cookie; and 403 with the admin's bearer token
    (`web_ui.access` alone).
  - `promql`: A Prometheus query (`/api/v1.0/prometheus/query`) must get
    401 or 403 with no token; 200 and a result with a local-issuer token
    of scope `monitoring.query`, or with the admin's login cookie; and
    403 with `monitoring.scrape` alone.
  - `scrapes`: Within 60 seconds, each server's Prometheus must find its
    scrape of itself up (`up{job="prometheus"}`), and each director's,
    its scrape of every origin and federated cache
    (`up{job="origin_cache_servers"}`).

- `http-metrics`: A native origin's counters (`server_type="origin"`),
  at the first origin, in the first protected namespace that takes
  direct reads and writes, or else `/public`. Requests but `refused`'s
  carry a subject of the run's own, and must raise the counters with its
  `username` by exactly theirs (under `-p origin-multiuser`, at least).
  Skips where XRootD serves the origins.

  - `put`: A PUT of 64 KiB must get 201, and raise
    `pelican_http_requests_total` with `method="PUT"` and `code="201"`
    by one, and `pelican_http_bytes_total` with `direction="in"` by at
    least 64 KiB.
  - `get`: A GET of it must get the bytes, and raise the requests
    counter with `method="GET"` and `code="200"` by one, and the bytes
    counter with `direction="out"` by at least 64 KiB.
  - `missing`: A GET of a missing object must get 404, and raise the
    requests counter with `code="404"` by one.
  - `refused`: A GET with a token signed by `unknown.pem` must get 401
    or 403, and raise the requests counter with that code and no
    `username`. Skips in `/public`.

  With `posixv2` or `pstore` storage, `put` must also raise
  `pelican_storage_writes_total`, and `get`
  `pelican_storage_reads_total`.

- `log-level`: Each server's log level (`/api/v1.0/logging/level`).

  - `change`: A POST of a more verbose level for 20 seconds must get 200
    and a `changeId`, and set the level, with the change listed.
  - `expiry`: Within 40 seconds of its deadline, each change must be
    gone, and the level restored.
  - `delete`: A change deleted by its ID must get 200, and deleted
    again, 404; the level must be restored.
  - `refusals`: As the admin, 400 for no level; a duration over 24
    hours, or none; a parameter that is no setting
    (`Logging.NoSuchLevel`); one that is no level
    (`Logging.Buffer.MaxSize`); and one outside `Logging`. A good change
    must get 403 from `test-webapi`, and 401 from no one.
  - `cli`, at the first server:
    `pelican-server server set-logging-level <level> 20s --server <web URL> --token <file>`,
    with the admin's token, must print a change's ID, which the API then
    lists. Skips without `pelican-server`
    ([client binary](../../README.md#client-binary)).

- `log-redaction`: At every origin and cache, the newest lines of its
  log (`/api/v1.0/logs/tail`).

  - `tail-access`: 401 with no credentials, 403 as `test-webapi`, and
    200 as the admin.
  - `redaction`: With the log level at `debug`, a fresh token presented
    in the `Authorization` header and in `?authz=Bearer%20<token>` at
    each data URL, and as a `login` cookie at the web API, must not
    appear in the tail, nor its signature, which a server replaces with
    `REDACTED`.

- `log-level-xrootd`: At the first origin, where XRootD serves the
  origins, and at the first federated XRootD cache; skips where there is
  neither. A change of `Logging.Origin.Xrootd` or `Logging.Cache.Xrootd`
  restarts XRootD after draining for `Xrootd.ShutdownTimeout` (2 seconds
  here).

  - `short`: A change for no longer than the drain must get 400.
  - `restart`: A change for 5 minutes past the drain must get 200, with
    `requiresRestart` and `effectiveAt`. XRootD must then restart (a
    probe goes unanswered, or `xrootd` is not `ok`), and within 120
    seconds serve and be `ok` again.
  - `named`: Within 90 seconds, every director must name the server for
    the probe again.
  - `restored`: Deleting the change must get 200, and restart XRootD
    again, with the same results.
