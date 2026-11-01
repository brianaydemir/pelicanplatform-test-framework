# Director APIs

Part of [What the tests check](README.md).

The [`director-api`](../../framework/suites/director_api.py) suite checks
that what each director publishes through its read-only APIs is true of the
[shape](../glossary.md#shape). Each row covers every director. The suite
skips a [standalone origin](../../framework/presets/topo-standalone.sh).

- `servers`: The director's list of servers
  (`/api/v1.0/director_ui/servers`, unless noted).

  - `listing`: Every origin, [another owner](another-owner.md)'s included,
    and federated cache, once each, and nothing else, with its URLs,
    registry prefix, storage (`posix` for a cache), and version; not
    filtered; and its director test disabled only where its storage can't
    take one. An origin's `FallBackRead` must match its exports' direct
    reads, where they agree.
  - `by-type`: `?server_type=origin` and `cache` must list that type alone;
    another type must get 400.
  - `by-type-case`: So must `Origin` and `Cache`.
  - `one`: `/servers/<name>` and `/servers/<name>/namespaces` must describe
    each server, and an origin's exports; an unknown name must get 404.
  - `ui-namespaces`: `/api/v1.0/director_ui/namespaces` must list each
    export, with its origins.
  - `contact`: `/api/v1.0/director_ui/contact` must give the configured
    `Director.SupportContactEmail` and `SupportContactUrl`.
  - `directors`: `/api/v1.0/director/directors` must list every director.

- `namespaces`:

  - `v2`: `/api/v2.0/director/listNamespaces` must list each export, with
    its capabilities and issuer, and `/pelican/monitoring`, and nothing
    else.
  - `v1`: The deprecated `/api/v1.0/director/listNamespaces` must answer
    299, with a `Warning` and a JSON object.
  - `prefix`: `/api/v1.0/director/namespaces/prefix/<path>` must give the
    namespace a path falls in, by whole segments, or 404. Under
    [`-p topo-multi-owner`](../../framework/presets/topo-multi-owner.sh),
    `/public/other/x`'s is `/public/other`, and `/public/others-x`'s is
    `/public`.

- `health`: A director tests each cache, and each origin whose storage can
  take a test file (`posix`, `posixv2`, `pstore`), every 15 seconds
  (`Director.OriginCacheHealthTestInterval`).

  - `status`: Within 150 seconds, each server it tests must be listed with
    `healthStatus` `OK`, and each other with `Health Test Disabled`.
  - `test-runs`: Over 35 seconds or more,
    `pelican_director_total_ftx_test_runs` must rise by a passed, reported
    run for each server tested, and not for the others.
  - `component`: Each origin and federated cache must report
    `pelican_component_health_status` `ok` for component `director`.
  - `cache-file`: A director test file read through each federated cache
    must have the director's test body.

- `health-test-file`: The test file a director serves the caches
  (`/api/v1.0/director/healthTest/pelican/monitoring/directorTest/director-test-<RFC 3339 time>.txt`).
  A GET must get the test body (`get`); a HEAD, its length (`head`); a
  PROPFIND, 207 and a multistatus naming the file and its length
  (`propfind`); a name with no valid time, 400 (`bad-timestamp`); and a
  PROPFIND in a directory named `a&b<`, the name escaped (`escaped`).

- `origin-stat`: Which origins hold an object in `/public`
  (`/api/v1.0/director_ui/servers/origins/stat/<path>`).

  - `no-auth`: Without credentials, it must answer 401.
  - `admin`: For the director's admin, 200, naming the one origin that holds
    it. Skips without `/public`.
  - `bad-bounds`: With `min_responses` above `max_responses`, 400.
  - `unknown-namespace`: For a namespace no origin exports, 404, or 429 in
    the director's first 5 minutes, as one JSON document.

- `metrics`:

  - `redirects`: 5 GETs of `/api/v1.0/director/object/` by a client of a
    made-up version, `pelican-client/999.99.<n>`, must raise by 5 both
    `pelican_director_redirects_total` for that version and status 307, and
    `pelican_director_client_requests_total` for version `999.99`.
  - `ads`: `pelican_director_advertisements_received_total` (status 200)
    must have risen for every origin and federated cache over 40 seconds or
    more. Forwarded ads don't count.
  - `server-count`: `pelican_director_server_count` must count each origin
    and federated cache once, by type, and nothing from the
    [OSDF](https://osg-htc.org/services/osdf)'s topology.

- `metadata-hosting`: Only the
  [discovery host](../glossary.md#discovery-host) may serve the federation's
  metadata (`Director.EnableFederationMetadataHosting`).

  - `discovery`: `/.well-known/pelican-configuration` must get 410 at each
    director, but 200 at the discovery host, and at every director under
    [`-p director-discovery`](../../framework/presets/director-discovery.sh)
    with [`-p director-ha`](../../framework/presets/director-ha.sh).
  - `no-auth`: `/api/v1.0/director_ui/federation/discrepancy`, which
    compares the director's metadata with the discovery host's, must refuse
    all but its admin (401).
  - `discrepancy`: It must report agreement, with keys in common, or at the
    discovery host, no comparison.

  [Federation](federation.md)'s `discovery` checks the document itself.

- `forwarding`: Under
  [`-p topo-multi-director`](../../framework/presets/topo-multi-director.sh),
  a director must forward each ad to every director it knows that has not
  seen it. The suite posts the first federated cache's ad, signed as the
  [`advertise`](advertisements.md) suite signs it, to one director. Rows are
  [`INCONCLUSIVE`](../glossary.md#case) if the cache's own next ad could
  explain them.

  - `forwarded`: An ad with another status (`warning` or `degraded`), posted
    to the first director, must reach the second within 10 seconds.
  - `shutdown-lifted`: An ad of shutdown, posted to the second, must make it
    filter the cache ("Disabled due to the server shutdown in progress");
    the cache's own status, posted to the first, must lift that at the
    second within 10 seconds.
  - `restored`: Within 150 seconds, the cache's own ads must restore it at
    both.
