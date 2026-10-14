# Observability

Part of [What the tests check](README.md).

Each server reports on itself: its health, by component; its metrics,
at `/metrics`, which its own Prometheus scrapes, as a director scrapes
every origin and cache; its log level, which its admin may raise for a
while; and the newest lines of its log. Operators and the federation's
own monitoring rely on these. The `observability` suite checks them at
every server of the shape, each once by its web URL, as the `webapi`
suite does, and acts as each server's admin and as `test-webapi`, as
that suite does ([web API](web-api.md)). Each case is a row, named
`<scenario> <server> <case>`.

Pelican guards each server's `/metrics` (`Monitoring.MetricAuthorization`)
and its Prometheus query API (`Monitoring.PromQLAuthorization`) by
default, and so does the framework. Every suite that reads `/metrics`
presents a token of the server's own making, from its local issuer,
signed with its own key, with scope `monitoring.scrape`, as the server
scrapes itself (`framework/testlib/metrics.py`).

- `health-api`: each server's health report
  (`/api/v1.0/metrics/health`; `GetHealthStatus` in
  `metrics/health.go`).
  - `components`: as its admin reads it, the report must say `ok`
    overall, and of each component. It must list `web-ui`; at an
    origin, and a cache that the directors know of, `federation`,
    `director`, and `registry`; and where XRootD serves an origin or a
    cache, `xrootd` and `config-updates`. A standalone origin must list
    none of `federation`, `director`, or `registry`, since it talks to
    no director or registry (`standalone_origin_cli_e2e_test.go`). A
    server says `warning` of its advertisements and of the directors'
    tests until the first of each, so the suite asks every 5 seconds,
    for up to 150, until every report is all `ok`, or none has changed
    for 35 seconds and none says a status that may yet change by
    itself. The `storage` component warns once the host's disks are
    80% full (`Monitoring.StorageWarningThreshold`), so a report that
    fails by it alone is inconclusive. A native origin serves no
    `/pelican/monitoring`, so the directors' tests of a `posixv2` or
    `pstore` origin fail, and it says `director` is critical, and its
    row fails, on v26.0.0-rc.3 and main alike
    ([director APIs](director-apis.md)).
  - `access`: with no credentials, the report must get 401; as
    `test-webapi`, 403; and as the admin, 200; but where
    `Server.HealthMonitoringPublic` is on, which the suite reads from
    the server's configuration, 200 for all. `/api/v1.0/health` must
    answer anyone with 200.
- `health-metrics`: each server's `pelican_component_health_status`
  must agree with its health report. The suite reads the report, then
  `/metrics`, then the report again; each component whose status both
  reports give must have the gauge of that status's value
  (`HealthStatusEnum`: `ok` is 5, `warning` 4, `critical` 2). And no
  gauge may be of a component that neither report lists, since an
  operator's alerts read the gauges. Pelican drops a component from the
  report but leaves its gauge: an XRootD origin says `cmsd` is
  `warning` as it starts, then, with no `Xrootd.ManagerHost`, drops it
  (`launcher_utils/xrootd_servers.go`), so the gauge stays at 4, and
  the origin's row fails under `-p origin-xrootd`, on v26.0.0-rc.3 and
  main alike. This comes from reading the code, not from a run.
- `metric-authorization`: `promMetricAuthHandler` and
  `promQueryEngineAuthHandler` (`web_ui/authorization.go`).
  - `metrics`: at each server, `/metrics` with no token must get 401
    or 403 (Pelican answers 403); with a token of the server's own
    local issuer with scope `monitoring.scrape`, 200, and the
    exposition; with the same token signed by another key (another
    server's, or `unknown.pem` where every server shares one), 401 or
    403; with the admin's login cookie, which carries the monitoring
    scopes (`setLoginCookie`), 200; and with the admin's bearer token,
    which carries only `web_ui.access`, 403.
  - `promql`: at each server, a query of its Prometheus
    (`/api/v1.0/prometheus/query`) with no token must get 401 or 403; with a
    token of its local issuer with scope `monitoring.query`, 200, and a
    successful query; with the admin's login cookie, 200; and with a
    token of scope `monitoring.scrape` alone, 403.
  - `scrapes`: within 60 seconds, each server's Prometheus must find
    its scrape of itself up (`up{job="prometheus"}` is 1), which needs
    the token the server mints for it (`createPromMetricToken`) to pass
    its own guard; and each director's, its scrape of every origin and
    federated cache up (`up{job="origin_cache_servers"}`, by each
    server's type and web host), which needs the token it mints from
    the federation's issuer (`configDirectorPromScraper`) to pass
    theirs.
- `http-metrics`, at the first origin, where Pelican serves it (not
  XRootD), in the first protected namespace that takes direct writes
  and reads, or, where only `/public` does (`-p origin-public` with
  `-p origin-public-writes`), in `/public`, where `refused` then skips,
  since its reads take no token to refuse
  (`httpMetricsMiddleware` in `origin_serve/handlers.go`). Straight to
  the origin, it sends a PUT of 64 KiB, a GET of it, a GET of an object
  that does not exist, and a GET with a token signed by `unknown.pem`,
  the first three with a token for a subject of the run's own
  (`observability-<run>`), which no other request has, so that the
  counters of that `username` must rise by exactly these. Under
  `-p origin-multiuser`, whose mapfile knows only the tests' usual
  subject, it uses that one, and the counters must rise by at least
  these. It writes under `data/observability/<run>/`, and removes it
  afterward.
  - `put`: the PUT must get 201, and raise
    `pelican_http_requests_total` with `server_type="origin"`,
    `method="PUT"`, and `code="201"` by one, and
    `pelican_http_bytes_total` with `direction="in"` by at least 64
    KiB.
  - `get`: the GET must get the bytes, and raise the requests counter
    with `method="GET"` and `code="200"` by one, and the bytes counter
    with `direction="out"` by at least 64 KiB.
  - `missing`: the GET of nothing must get 404, and raise the requests
    counter with `code="404"` by one.
  - `refused`: the unknown key's GET must get 401 or 403, and raise the
    requests counter with that code. The middleware counts a request
    before the origin's authorization, so a refusal counts too, with no
    `username`.

  Where the origin's storage goes through Pelican's afero adapter
  (`posixv2` and `pstore`; `origin_serve/filesystem.go`), `put` must
  also raise `pelican_storage_writes_total`, and `get`
  `pelican_storage_reads_total`, for that `username`.
- `log-level`: each server's log level, which its admin may change for
  a while (`/api/v1.0/logging/level`; `web_ui/logging_api.go` and
  `logging/level_manager.go`).
  - `change`: at every server at once, a POST of a more verbose level
    than the server's (`debug`, or `trace` from `debug`) for 20 seconds
    must get 200 and a `changeId`; the level must then be the one asked
    for, with the change listed.
  - `expiry`: within 40 seconds of its deadline, each change must be
    gone, and the level back to what it was. The server drops a change
    at its deadline.
  - `delete`: a change for 10 minutes, deleted by its ID, must get 200,
    and deleted again, 404; and the level must be back.
  - `refusals`: as the admin, a level that is none, a duration over 24
    hours or of none, a parameter that is no setting
    (`Logging.NoSuchLevel`), one that is no level
    (`Logging.Buffer.MaxSize`, asked for a second, which the server
    reads only as it starts), and one outside `Logging`, must each get
    400; a good change from
    `test-webapi`, 403; and from no one, 401. Pelican takes any setting
    under `Logging` (`isValidLoggingParameter`), so the `not a level`
    case gets 200, and every server's `refusals` row fails, on
    v26.0.0-rc.3 and main alike. This comes from reading the code, not
    from a run.
  - `cli`, at the first server: `pelican-server server
    set-logging-level <level> 20s --server <web URL> --token <file>`,
    with the admin's token, must print a change's ID, which the API
    must then list. It skips without a `pelican-server`.
- `log-redaction`: at every origin and cache. A server keeps its newest
  log lines in memory, and serves them to its admin, and to a user
  granted `pelican.log_read` (`/api/v1.0/logs/tail`;
  `web_ui/log_buffer_api.go`).
  - `tail-access`: the tail with no credentials must get 401; as
    `test-webapi`, 403; and as the admin, 200.
  - `redaction`: with the server's log level raised to `debug`, the
    suite presents a fresh token to it, as a client might: in the
    `Authorization` header and in the query (`?authz=Bearer%20<token>`),
    at each of its data URLs, for `/<ns>/data/0.0` of the first
    namespace that takes tokens; and as a `login` cookie, at its web
    API. Pelican replaces a token's signature with `REDACTED` in a
    line's message and `url` field before the line reaches the tail
    (`redactEntryCopy` in `config/logging.go`). The tail since the
    suite began must hold neither the token nor its signature. The
    note says how often the token appeared, redacted. If a request went
    unanswered, or the tail dropped lines the suite had not read, the
    row is inconclusive.
- `log-level-xrootd`, at the first origin, where XRootD serves the
  origins, and at the first federated XRootD cache: XRootD's own log
  levels (`Logging.Origin.Xrootd` and `Logging.Cache.Xrootd`) take a
  restart of XRootD, which Pelican makes when one changes
  (`handleXrootdLoggingChange` in `xrootd/logging_callback.go`), after
  draining for `Xrootd.ShutdownTimeout`, which the framework sets to 2
  seconds.
  - `short`: a change for no longer than the drain must get 400, since
    it would revert before XRootD restarted to take it.
  - `restart`: a change for 5 minutes past the drain must get 200, with
    `requiresRestart` and `effectiveAt`. XRootD must then restart:
    the object must go unanswered, or the health report say `xrootd` is
    not `ok`; and then, within 120 seconds, the object must be answered
    as before, and `xrootd` be `ok`. The note says how long the object
    went unanswered. It is the one way the tests can restart XRootD
    without restarting its server.
  - `named`: within 90 seconds, every director must name the server for
    the object again. A server tells the directors it is shutting down
    as XRootD restarts, and advertises again once it serves.
  - `restored`: deleting the change must get 200, and restart XRootD
    again, after which the same must hold.

The suite raises each server's log level for a minute at most, and
deletes every change it made when it ends.
