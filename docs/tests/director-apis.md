# Director APIs

Part of [What the tests check](README.md).

A director publishes what it knows of the federation through read-only
APIs: the lists of servers and namespaces that its web UI and the
caches read, its health tests of each server, its metrics, and its
comparison of the federation's metadata. The `director-api` suite
checks that each director's are true of the shape. It skips a
standalone origin, which has no director. Each case is a row, named
`<scenario> <case>`, and covers every director.

- `servers`: `/api/v1.0/director_ui/servers` (`listServers` in
  `director/director_ui.go`).
  - `listing`: every origin, another owner's included, and every
    federated cache, once, and nothing else; a site-local cache is not
    listed. Each with its web URL; its data URL on its host; its
    registry prefix, `/origins/<host>:<port>` of its web URL for an
    origin (`GetOriginNs`) and `/caches/<name>` for a cache; its
    storage, the origins' variant, and `posix` for a cache, whose ad
    names none; the shape's version; unfiltered; and its director test
    disabled only at an origin whose storage can't take it. An origin
    with one export takes that export's capabilities as its own, and
    with several, direct reads, by default: so under
    `-p origin-no-direct`, it must not offer direct reads.
  - `by-type`: with `?server_type=origin`, the origins alone; with
    `cache`, the caches alone; with another type, 400.
  - `by-type-case`: with `?server_type=Origin` and `Cache`, as Pelican
    spells the types, the same lists, since `listServers`
    (`director/director_ui.go`) takes the type without regard to case.
    It then compares it in lower case exactly, so `Origin` and `Cache`
    list nothing, and the row fails, on v26.0.0-rc.3 and main alike.
  - `one`: `/servers/<name>` of each server, by its name, and
    `/servers/<name>/namespaces`: for an origin, its exports; for a
    name no server has, 404.
  - `ui-namespaces`: `/api/v1.0/director_ui/namespaces` must list
    each export, with the names of the origins that export it.
  - `contact`: `/api/v1.0/director_ui/contact` must give the support
    contact that `framework/config.d/director/default/` configures
    (`Director.SupportContactEmail` and `SupportContactUrl`).
  - `directors`: `/api/v1.0/director/directors` must list every
    director, by the URL it takes ads at.
- `namespaces`:
  - `v2`: `/api/v2.0/director/listNamespaces` must list each export,
    once per origin that exports it, with its capabilities and its
    issuer, none for one without (`has_issuer`), and
    `/pelican/monitoring`, which anyone may read, and nothing else. A
    cache learns a namespace's issuers here, so a wrong one breaks
    every protected read through it.
  - `v1`: `/api/v1.0/director/listNamespaces`, which serves no data
    since the first version of a namespace's ad went away, must answer
    299, with a `Warning`, and a JSON object, not a list.
  - `prefix`: `/api/v1.0/director/namespaces/prefix/<path>` must give
    the namespace a path falls in: `/<ns>/data/x`'s is `/<ns>`; a path
    in no namespace gets 404; and under `-p topo-multi-owner`,
    `/public/other/x`'s is `/public/other`, but `/public/others-x`'s is
    `/public`, since a prefix matches whole segments of a path.
- `health`: a director tests each origin and cache it hears from every
  15 seconds (`Director.OriginCacheHealthTestInterval`): at an origin,
  it puts a file under `/pelican/monitoring/directorTest/`, reads it,
  and removes it; at a cache, it reads one, which the cache gets from
  the director itself. It reports each result to the server
  (`director/monitor.go` and `cache_monitor.go`). It does not test an
  origin whose storage can't take the file: only `posix`, `posixv2`,
  and `pstore` can (`SupportsSelfTest`), so not `s3v2`, `httpsv2`,
  `ssh`, `https`, or `s3`.
  - `status`: within 150 seconds, each director must list each server
    it tests with `healthStatus` `OK`, and each it doesn't with
    `Health Test Disabled`. The wait ends sooner once no test is still
    to run, and a failed one has had 35 seconds to pass.
  - `test-runs`: in 35 seconds or more, each director's
    `pelican_director_total_ftx_test_runs` for each server it tests
    must rise by a run that passed and was reported, and for each it
    doesn't, by none.
  - `component`: each origin and federated cache must say, by its own
    `pelican_component_health_status`, component `director`, that the
    tests pass (ok); one that the directors don't test says ok too.
  - `cache-file`: a GET of a test file of the suite's own, by the name
    a director would give it, through each federated cache must get
    the director's test body.

  A native origin serves only its exports' prefixes
  (`RegisterHandlers` in `origin_serve/handlers.go`), and nothing in
  `origin_serve/` serves `/pelican/monitoring`, which an XRootD origin
  exports for these tests. So a director's tests of a `posixv2` or
  `pstore` origin should fail, and the `status`, `test-runs`, and
  `component` rows with them, on v26.0.0-rc.3 and main alike. This
  comes from reading the code, not from a run.
- `health-test-file`: the test file a director serves the caches
  (`/api/v1.0/director/healthTest/pelican/monitoring/directorTest/director-test-<RFC 3339 time>.txt`;
  `getHealthTestFile` in `director/director.go`). A GET must get the
  test body (`get`); a HEAD, its length (`head`); and a PROPFIND, 207,
  and a multistatus naming the file and its length (`propfind`). A
  name whose time is none must get 400 (`bad-timestamp`). In a
  directory named `a&b<`, a PROPFIND must name it, escaped
  (`escaped`). Pelican escapes the path, but then writes it as it was,
  so its XML is malformed, and the `escaped` row fails, on
  v26.0.0-rc.3 and main alike.
- `origin-stat`: which origins hold an object, as a director asks
  them (`/api/v1.0/director_ui/servers/origins/stat/<path>`;
  `queryOrigins` in `director/director_ui.go`). It takes the token it
  is shown to the origins, and a director's admin token is no storage
  token, so the suite asks of an object in `/public`, which it puts at
  one origin alone (on a `pstore` origin, in `/protected-a`'s storage,
  which `/public` reads), at `data/director-api/<run>/held`, and
  removes afterward. With no credentials, it must get 401
  (`no-auth`); as the director's admin, 200, naming that origin alone
  (`admin`; it skips where the shape exports no `/public`); with
  `min_responses` above `max_responses`, 400 (`bad-bounds`); and for a
  namespace no origin exports, 404, or 429 within 5 minutes of the
  director's start, as one JSON document (`unknown-namespace`).
  Pelican writes that answer and goes on, asks no origin, and writes
  another, so the body is two JSON documents, and the
  `unknown-namespace` row fails, on v26.0.0-rc.3 and main alike.
- `metrics`: what each director counts (`metrics/director.go`). Other
  traffic moves these counters too, so each row counts only what is the
  run's own, or what only rises.
  - `redirects`: 5 GETs of the object route, each from a client whose
    `User-Agent` names a version of the run's own
    (`pelican-client/7.99.<n>`), must raise
    `pelican_director_redirects_total` of that version, with status
    307, by 5, and `pelican_director_client_requests_total` of version
    `7.99` by 5.
  - `ads`: since the suite began, at least 40 seconds before,
    `pelican_director_advertisements_received_total` must have risen,
    with status 200, for every origin and federated cache. A director
    counts the ads a server sends it, but not those another director
    forwards (`serverAdMetricMiddleware`).
  - `server-count`: `pelican_director_server_count` must count each
    origin and federated cache once, of its type, and none from the
    OSDF topology, and as many of each type as the shape has.
- `metadata-hosting`: `framework/config.d/director/default/` turns
  `Director.EnableFederationMetadataHosting` off, since the discovery
  service serves the federation's metadata. So each director's
  `/.well-known/pelican-configuration` must get 410, but at the
  discovery host, 200 (`discovery`): in `tiny`, whose director it is,
  and under `-p director-discovery`, where `fed.sh` turns the setting
  on for director-0 alone, so that another director (`-p
  topo-multi-director`) still answers 410, unless the directors share
  one name (`-p director-ha`), at which any may answer, so every
  director then serves it. Its comparison of the
  metadata it was given with the discovery host's
  (`/api/v1.0/director_ui/federation/discrepancy`;
  `director/metadata_comparison.go`) must refuse anyone but its admin
  (401; `no-auth`), and tell the admin that it compared them, and
  found them to agree, with keys in common, or, where it is the
  discovery host, that it did not compare them (`discrepancy`). The
  `federation` suite's `discovery` scenario checks what the discovery
  host serves where it is a Pelican server: each field of the document
  as `fed.sh` has it, and a `jwks_uri` that serves the server's own
  keys.
- `forwarding`, under `-p topo-multi-director`: a director forwards
  each ad a server posts to it to every director it knows that has not
  yet seen it (`forwardServiceAd` in `director/director_advertise.go`).
  No configuration can keep a server's ads from any director, since a
  server advertises to every director it hears of: from its
  configuration, the discovery document, and each director's list of
  directors (`doDiscovery` in `server_utils/director_discovery.go`).
  So the suite posts the first federated cache's own ad, signed as the
  `advertise` suite signs it, to one director alone, just after the
  cache's own ad has reached the other, so that the cache's next is
  about 30 seconds away. If it comes sooner, and could explain what the
  other director shows, the row is inconclusive.
  - `forwarded`: the ad, posted to the first director, saying a status
    the cache doesn't (`warning`, or `degraded`), must reach the
    second within 10 seconds.
  - `shutdown-lifted`: the ad, posted to the second director, saying
    the cache is shutting down, after which it must filter the cache
    (`shutdownFiltered`); then its ad saying its own status, posted to
    the first, must lift that at the second within 10 seconds, as an
    ad posted to the second would. Only an ad posted to a director
    lifts it (`registerServerAd` in `director/director.go`), and not
    one forwarded (`finishRegisterServeAd`), so the row fails, on
    v26.0.0-rc.3 and main alike: a director that hears a server only
    by another's forwarding keeps it out of routing from its restart
    on.
  - `restored`: within 150 seconds, the cache's own ads must have
    restored it at both directors.

  A director that forwarded an ad back to the one it came from would
  forward it forever, but two directors can't be made to, since each
  skips the directors an ad has already seen, and its own. Nothing the
  suite can read shows a forwarded ad, so it can't see such a loop.
