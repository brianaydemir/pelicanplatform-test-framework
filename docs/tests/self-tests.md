# Self-tests

Part of [What the tests check](README.md).

The `selftest` suite checks the helpers in `framework/testlib/` that
other suites build checks on, against the running federation, so that a
failure there shows as the helper's, not as the checks'. It puts an
object of its own in `/protected-a/data/selftest/<run>/` at every
origin, reads it, and removes it.

- `trailers`: a V2 cache that a client asks for the transfer's status
  (`X-Transfer-Status: true` and `TE: trailers`) sends its body in
  chunks, announces `Trailer: X-Transfer-Status`, and ends with that
  trailer, `200: OK` for a transfer that went well
  (`local_cache/persistent_cache_api.go`); it is how a cache reports a
  failure once it has sent its headers. `framework/testlib/web.py`
  keeps the trailers that Python's `http.client` throws away. A GET of
  the object at each federated V2 cache, so asked, must get them; one
  not so asked must get none. XRootD caches are passed over.
- `jwt`: `framework/testlib/jwt.py` makes tokens that `pelican token
  create` cannot: any claim present or absent and of any JSON type, any
  `kid` or none, and signatures by ES256 or RS256 (with the `openssl`
  command), HS256 (with any bytes as the secret), or `none`. Its token
  with the claims and header of the `server` token, signed with the
  origins' key, must read the object at every origin, and the same
  token, expired ten minutes before, be refused. The claims' names, and
  the values that are the same from token to token, and the header,
  must first match the `server` token that `pelican token create`
  minted. `jwt.verify()`, which checks an ES256 signature against a
  JWKS, must then find the token's key, by its `kid`, in the first
  origin's published JWKS, and none in a JWKS of the `unknown` test key
  alone. It skips where the origins take no direct clients.
- `metrics`: `framework/testlib/metrics.py` reads a server's
  `/metrics` with a token of the server's own making, with scope
  `monitoring.scrape`, since Pelican guards it
  ([observability](observability.md)). A GET of the object straight to
  each native origin must raise its `pelican_http_requests_total` with
  `server_type="origin"`, `method="GET"`, and `code="200"` by at least
  one (`metrics/http_metrics.go`). Other requests may raise it too, so no
  check of a counter here is exact. It skips for an XRootD origin, which
  keeps no such counter, and where the origins take no direct clients.
- `restart-cache`: `framework/testlib/control.py` restarts a service in
  the middle of a run ([service control](../../README.md#service-control)).
  It reads the object through cache-0 until a V2 cache says, with `Age`,
  that it holds it whole (`local_cache/persistent_cache_api.go`), and
  every director names cache-0 for it; then restarts cache-0. A cache
  that stops tells the directors it is shutting down, and they send no
  client to it until it advertises again, which it does as it starts and
  then every minute (`launchers/launcher.go`, `director/director.go`).
  So within 150 seconds every director must name cache-0 again, and
  within 300 it must serve the object, the same bytes, since a cache
  just started refuses a namespace's tokens until it has learned its
  issuers. A V2 cache keeps its store across a restart, and must serve
  the object from it, with an `Age` of at least the seconds since it
  first said it held it all, which only the record it kept can give
  (`Age` counts from when the object was completed, and a cache that
  fetched it again would send none). An XRootD cache need only serve
  the bytes. It skips where no helper serves the run, in the tiny
  topology, whose cache is the whole federation's process, and where
  there is no cache-0.
