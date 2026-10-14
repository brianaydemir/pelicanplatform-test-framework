# Routing

Part of [What the tests check](README.md).

A director decides which servers a client is sent to, in what order,
and what the redirect carries (`director/director.go`, `sort.go`, and
`stat.go` in Pelican). `federation` checks whom each director names;
the `routing` suite checks the rest: the order, the redirect itself, and
what a director refuses. It skips a standalone origin, which has no
director. It asks about objects it puts at every origin, in the first
protected namespace, under `data/routing/<run>/`, and removes them
afterward: `probe`; `warm`, which only `random-and-adaptive` reads; and
`missing`, which it never puts. Each case is a row, named `<scenario>
<case>`, and covers every director.

Each server's own layer (`framework/config.d/instance/`) declares where
it is (`GeoLocation`): cache-0 and origin-0 in Madison, and cache-1 and
origin-1 in Lisbon. The framework has no GeoIP database, so a client
that says nothing of where it is is placed at random in the contiguous
US, and sent to the Madison servers first, as their names would order
them anyway.

- `distance`: the default sort, by distance from the client
  (`Director.CacheSortMethod` `distance`). A client may say where it is
  (`X-Pelican-Coordinate: lat=..., long=...`), which wins over GeoIP.
  - `caches`: with several caches, a GET of the object route for a
    client near Madrid must send it to cache-1, and its `Link` list
    cache-1 first; for one near Madison, cache-0.
  - `caches-debug`: with `X-Pelican-Debug: true`, the director must
    answer with the same redirect and, as its body, the decision as
    JSON: `directorSortMethod` `distance`; the client's coordinate as it
    declared it, with `Source` `declared`; and for each cache, its
    declared coordinate, with `Source` `declared`, and its
    `distanceWeight`, which must be what the director's formula gives:
    1 at the client, halving every 200 miles of great-circle distance.
  - `origins` and `origins-debug`: the same for the origin route, with
    several origins, between origin-0 and origin-1.
  - `malformed`: an `X-Pelican-Coordinate` whose latitude is no number,
    or out of range, must be ignored: the client is still sent to a
    cache, and the debug body's client coordinate is not `declared`.
- `stat`: an object that no origin holds, asked for of the origin
  route, by GET and HEAD, in each exported namespace. The director asks
  the origins first (`shouldStatOrigins` in `director/stat.go`), when
  it can and should.
  - `several`: with several origins, and the namespace's token, it asks
    them, and must answer 404, with a body saying that no source
    reported possession of the object. Where the namespace takes no
    direct clients, the origins refuse its question, which carries no
    federation token, and it must take a refusal for presence, since it
    cannot tell: there a redirect to an origin passes too.
  - `skipstat`: with `?skipstat`, it must not ask, and must redirect
    (307) to an origin.
  - `no-token`: in a protected namespace, with no token, it can't ask,
    and must redirect.
  - `single`: with one origin, it assumes the object is there
    (`Director.AssumePresenceAtSingleOrigin`, on by default), and must
    redirect.
  - `single-checked`: with one origin and that setting off (a restart;
    below), it must ask, and answer 404.
- `redirect`: how a director builds a redirect of `probe`, at its
  object route and at the bare path (`generateRedirectResponse`).
  - `header-token`: the token in `Authorization` must reach `Location`
    as `authz`, without `Bearer `. Following `Location` alone, with no
    `Authorization`, as curl does across hosts, must serve the object
    where the namespace admits a client at the server it lands at, and
    otherwise be refused. No URL in `Link` may carry the token, which
    would make the header long; its `pri` must run 1 to N, in order,
    and its `depth` be the number of path segments below the namespace
    (4).
  - `query-token` and `query-bearer`: the same, with the token in
    `?authz=`, and with it as `?authz=Bearer <token>`, as XRootD sends
    it, from which the director must strip `Bearer `.
  - `timeout`: `X-Pelican-Timeout: 5s` must reach `Location` as
    `pelican.timeout=5s`.
  - `params`: `?skipstat`, and a parameter the director doesn't know,
    whose value a URL must escape (`a b&c=d/é?#%`), must reach
    `Location`, escaped; and `?directread`, where the namespace takes
    direct clients.
  - `job-id`: an `X-Pelican-JobId` that is a UUID must come back in the
    answer's; for one that isn't, the director must send a UUID of its
    own.
  - `no-redirect`: `?redirect=false` must get 200, with no `Location`,
    but the same `X-Pelican-Namespace`, and a `Link` of the same servers.
- `expired-401`: a director refuses a token that will expire before the
  client could follow the redirect, with 401 and `WWW-Authenticate:
  Bearer error="invalid_token", error_description="token has expired"`,
  so that a client such as rclone fetches a new one
  (`validateClientToken`). It reads the token's times without checking
  its signature, and allows 10 seconds of grace, or half the token's
  lifetime if less. At the object route, the origin route, and the bare
  path, these must get 401: a token that expired five minutes ago
  (`expired`), and the same in `?authz=` (`in-query`); one issued an
  hour ago that expires in 5 seconds (`expiring`); a 10-second one
  issued 6 seconds ago (`half-life`); and one whose `exp` is its `iat`
  (`no-lifetime`). These must be redirected: a 10-second one just
  issued (`short-fresh`), whose grace is 5 seconds; an opaque token
  (`opaque`); and a JWT with no `exp` (`no-exp`). The suite signs each
  with the origins' key (`jwt.encode()`).
- `status-codes`: what a director refuses, and with what.
  - `unknown-namespace`: an object in a namespace that no origin
    exports must get 404, but within 5 minutes of the director's start,
    429, since it may not yet have heard from every origin
    (`inStartupSequence`). The suite reads when it started from
    `process_start_time_seconds` on its `/metrics`, and within 10
    seconds of the boundary takes either.
  - `writes`: a bare-path PUT and DELETE in `/public`, which no origin
    takes writes in, must get 405, since the director finds the
    namespace's origins, and none that supports the method
    (`noOriginsForReqErr`); `federation`'s `redirects` takes any refusal.
  - `bad-requests`: `?directread&prefercached`, which conflict, must get
    400, and so must a client older than 7.0.0 (`User-Agent:
    pelican-client/6.9.0`), while a 7.0.0 client is redirected.
  - `options`: a bare OPTIONS must get 200, with `Allow` and CORS
    headers.
  - `server` and `server-once`: every answer of the scenario must carry
    `Server: pelican/<version>`, and in one field, since `Server` is no
    list (RFC 9110, section 5.3). The director's API routes add it twice,
    once in each of the two router groups they are in
    (`launchers/director_serve.go` and `RegisterDirectorAPI`), so
    `server-once` fails there.

The other scenarios need a director configured otherwise. Under
`-p topo-multi-cache`, with a control helper, the suite restarts every
director four times, as the last of its configuration layers
([service control](../../README.md#service-control)), and after each
waits up to 150 seconds until every director names every server again;
afterward, it restores them, and waits again. Elsewhere these rows
skip, and say why, so that other shapes are not slowed.

- `random-and-adaptive`:
  - `random`: with `Director.CacheSortMethod: random`, each director,
    asked 20 times, must put each cache first at least once.
  - `adaptive-presence`: with `adaptive`, after `warm` is read through
    cache-1 alone, and cache-1 gives it an `Age` of a second or more,
    each director, asked with the token, with which it asks the caches
    whether they hold it, must explain (`X-Pelican-Debug`) an
    `availabilityWeight` of 2 for cache-1 and 0.5 for cache-0
    (`availabilityWeightFn`). A director takes a cache's copy by its
    `Age`, which only a V2 cache is known to send, so the row skips XRootD
    caches. The order is drawn at random by the weights, so it is not
    checked.
  - `adaptive-no-token`: asked with no token, with which it can't ask the
    caches of a protected namespace, it must weigh them alike.
  - `adaptive-unchecked`: with `Director.CheckCachePresence: false`, the
    same, though asked with the token.
- The last restart sets `Director.FilteredServers` to cache-1's name,
  `Director.AssumePresenceAtSingleOrigin: false` (for `stat`'s
  `single-checked`), and response hostnames that no DNS knows, which
  the suite sends only in headers:
  - `filtered`: `routing`: no director may name cache-1, at the object
    route or the bare path, asked five times each, and each must still
    name the other caches. `listing`: each director's
    `/api/v1.0/director_ui/servers` must list cache-1 with `filtered`
    true and `filteredType` `permFiltered`.
  - `response-hostnames`: with `Director.OriginResponseHostnames` and
    `CacheResponseHostnames` each naming a host, a bare-path GET asked
    of the first must go to an origin, and of the second to a cache,
    whether the name is in `Host` (`host-origin` and `host-cache`) or in
    `X-Forwarded-Host` (`forwarded-origin` and `forwarded-cache`). The
    director looks for `Host` among the request's headers
    (`ShortcutMiddleware`), but Go's server moves it out of them, into
    the request's `Host`, so it never finds it: the `host-` row for the
    kind of server that the director sends a bare path to by default
    passes only for that reason, and the other fails, on v26.0.0-rc.3
    and main alike.
- `permitted-namespaces`: a cache may be restricted to namespaces
  (`Cache.PermittedNamespaces`), which it then alone advertises to the
  directors (`filterAdsBasedOnNamespace` in `cache/advertise.go`), so
  that they send it clients for those alone. The suite restarts the
  last cache with the setting naming `/public` (or, where `/public` is
  not exported, another protected namespace), as the last of its
  configuration layers, and waits up to 150 seconds until the directors
  route as it says; afterward it restores the cache, and waits again.
  `routing`: each director, asked five times at the object route, must
  name the cache for an object in the permitted namespace, and not for
  the probe in the suite's own, for which it must still name every
  other cache. `listing`: each director's
  `/api/v1.0/director_ui/servers` must list the cache with
  `namespacePrefixes` of the permitted namespace, and any under it, and
  no other. `direct`: asked straight, the cache must serve the object
  in the permitted namespace; what it does with the probe in the
  suite's own is not settled, since the setting narrows what the cache
  advertises, so the row's note says, but it must never serve other
  bytes. It needs several caches and a control helper, as the other
  restarts do, and a shape that exports two namespaces.
