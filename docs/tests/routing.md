# Routing

Part of [What the tests check](README.md).

The [`routing`](../../framework/suites/routing.py) suite checks how each
director orders servers, what its redirects carry, and what it refuses;
[`federation`](federation.md) checks whom it names. Each row covers every
director. The suite skips a
[standalone origin](../../framework/presets/topo-standalone.sh).

The object route is `/api/v1.0/director/object/`; the origin route,
`/origin/`. The suite reads `probe`, at every origin, and `missing`, at
none, in the first protected [namespace](../glossary.md#namespace) (or
`/public` under
[`-p origin-public`](../../framework/presets/origin-public.sh)). Cache-0 and
origin-0 are in Madison; cache-1 and origin-1, in Lisbon
([`GeoLocation`](../../framework/config.d/instance/)).

- `distance`: The default sort (`Director.CacheSortMethod: distance`), by
  the client's `X-Pelican-Coordinate`. Cache rows skip with one cache;
  origin rows, with one origin.

  - `caches`: A client near Madrid must be sent to cache-1, first in `Link`;
    one near Madison, to cache-0.
  - `caches-debug`: `X-Pelican-Debug: true` must add a JSON body:
    `directorSortMethod` `distance`, the client's and each cache's
    coordinate (`Source` `declared`), and each cache's `distanceWeight`, 1
    at the client and halving every 200 miles.
  - `origins` and `origins-debug`: The same at the origin route.
  - `malformed`: A latitude that is no number, or out of range, must be
    ignored.

- `stat`: The origin route for `missing`, by GET and HEAD.

  - `several`: With several origins and the namespace's token, the director
    must ask them, and answer 404; where the namespace takes no direct
    clients, it may redirect.
  - `skipstat`: With `?skipstat`, it must redirect without asking.
  - `no-token`: With no token in a protected namespace, it must redirect.
  - `single`: With one origin, it must redirect
    (`Director.AssumePresenceAtSingleOrigin`, on by default).
  - `single-checked`: With one origin and that setting off, it must
    answer 404. Needs the directors restarted, as for `filtered`.

  `several` skips with one origin; `single` and `single-checked`, with
  several.

- `redirect`: The redirect of `probe`, at the object route and the bare
  path.

  - `header-token`: The `Authorization` token must reach `Location` as
    `authz`, without `Bearer `, so that `Location` alone is served where the
    namespace admits the client. `Link` must not carry the token; its `pri`
    must run 1 to N, and its `depth` be the path's segments under the
    namespace (4).
  - `query-token` and `query-bearer`: The same, from `?authz=<token>` and
    `?authz=Bearer <token>`.
  - `timeout`: `X-Pelican-Timeout: 5s` must reach `Location` as
    `pelican.timeout=5s`.
  - `params`: `?skipstat`, an unknown parameter, and `?directread` where the
    namespace takes direct clients must reach `Location`, escaped.
  - `job-id`: An `X-Pelican-JobId` UUID must come back; for anything else, a
    UUID of the director's.
  - `no-redirect`: `?redirect=false` must get 200, with no `Location`, but
    the same `X-Pelican-Namespace` and `Link`.

- `expired-401`: A token that expires before the client could follow the
  redirect (with 10 seconds' grace, or half its lifetime if less) must get
  401 and
  `WWW-Authenticate: Bearer error="invalid_token", error_description="token has expired"`,
  at either route and the bare path.

  - 401: `expired` (five minutes ago), `in-query` (the same, in `?authz=`),
    `expiring` (in 5 seconds), `half-life` (a 10-second lifetime, 6 seconds
    old), and `no-lifetime` (`exp` equal to `iat`).
  - Redirect: `short-fresh` (a 10-second lifetime, new), `opaque`, and
    `no-exp`.

- `status-codes`:

  - `unknown-namespace`: A namespace no origin exports must get 404, or 429
    in the director's first 5 minutes.
  - `writes`: A bare-path PUT and DELETE in `/public` must get 405. Skips
    where `/public` is not exported, or takes writes.
  - `delete`: A bare-path DELETE of an object in the namespace, with the
    pass's token, must be redirected to an origin (307), with the token in
    `Location`'s `authz`; followed so, with no `Authorization` header, as
    curl follows a redirect to another host, it must succeed, and leave that
    origin's store without the object. Skips where the namespace takes no
    writes, or its storage no deletes.
  - `bad-requests`: `?directread&prefercached` must get 400, and so must a
    client older than any the director serves
    (`User-Agent: pelican-client/0.0.1`); a client of the director's own
    version must be redirected.
  - `options`: A bare OPTIONS must get 200, with `Allow` and CORS headers.
  - `server` and `server-once`: Every answer must carry
    `Server: pelican/<version>`, in one field
    ([RFC 9110, section 5.3](https://www.rfc-editor.org/rfc/rfc9110#section-5.3)).

`random-and-adaptive`, `filtered`, `response-hostnames`, and
`permitted-namespaces` need several caches
([`-p topo-multi-cache`](../../framework/presets/topo-multi-cache.sh)) and a
[control helper](../glossary.md#service-control-helper), and skip otherwise.
For the first three, the suite restarts every director with each setting,
then restores them, waiting up to 150 seconds each time for every director
to name every server.

- `random-and-adaptive`:
  - `random`: With `Director.CacheSortMethod: random`, each director, asked
    20 times, must put each cache first at least once.
  - `adaptive-presence`: With `adaptive`, once cache-1 alone holds an
    object, each director, asked with the token, must give cache-1 an
    `availabilityWeight` of 2, and cache-0 0.5 (`X-Pelican-Debug`). Skips
    with an [XRootD](../glossary.md#native-and-xrootd) cache.
  - `adaptive-no-token`: Asked with no token, each director must weigh the
    caches alike.
  - `adaptive-unchecked`: So must each director with
    `Director.CheckCachePresence: false`, though asked with the token.
- `filtered`: With `Director.FilteredServers` naming cache-1, no director
  may name it, at the object route or the bare path, but each must name the
  other caches (`routing`). Each must list it in
  `/api/v1.0/director_ui/servers` with `filtered` true and `filteredType`
  "Permanently Disabled via the director configuration" (`listing`).
- `response-hostnames`: A bare-path GET asked of the host in
  `Director.OriginResponseHostnames` must go to an origin, and of the one in
  `CacheResponseHostnames` to a cache, by `Host` (`host-origin`,
  `host-cache`) or `X-Forwarded-Host` (`forwarded-origin`,
  `forwarded-cache`).
- `permitted-namespaces`: The last cache, restarted with
  `Cache.PermittedNamespaces` naming a namespace other than the suite's,
  then restored. Skips where the [shape](../glossary.md#shape) exports no
  other.
  - `routing`: Each director must name the cache for the permitted namespace
    alone, and every other cache for `probe`.
  - `listing`: Each director's `/api/v1.0/director_ui/servers` must list the
    cache with that namespace (and any under it) alone.
  - `direct`: The cache must serve the permitted namespace, and refuse
    `probe`.
