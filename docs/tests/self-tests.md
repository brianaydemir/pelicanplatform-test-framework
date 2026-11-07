# Self-tests

Part of [What the tests check](README.md).

The [`selftest`](../../framework/suites/selftest.py) suite checks the
helpers in [`framework/testlib/`](../../framework/testlib/) that other
suites build on, so that a fault in a helper shows as the helper's. It skips
a [shape](../glossary.md#shape) that does not export `/protected-a`.

- `trailers`: [`framework/testlib/web.py`](../../framework/testlib/web.py)
  must keep a chunked body's trailers. Each federated V2 cache, asked with
  `X-Transfer-Status: true` and `TE: trailers`, must send
  `Trailer: X-Transfer-Status` and the trailer `200: OK`, and none when not
  asked. Skips without a federated V2 cache.
- `jwt`: [`framework/testlib/jwt.py`](../../framework/testlib/jwt.py) must
  make tokens the origins take. Its version of the `server`
  [credential](../glossary.md#credential) must match the one
  `pelican token create` mints in the pass's
  [token profile](README.md#token-profiles), in header, claim names, and
  fixed values. Signed with the origins' key, it must read the object at
  every origin, and be refused once expired. `jwt.verify()` must find its
  key in the first origin's JWKS, and not in one of the `unknown`
  [test key](authorization.md) alone. Skips where the origins take no direct
  clients.
- `metrics`:
  [`framework/testlib/metrics.py`](../../framework/testlib/metrics.py) must
  read a server's `/metrics` ([observability](observability.md)). A GET of
  the object straight to each [native](../glossary.md#native-and-xrootd)
  origin must raise its `pelican_http_requests_total`
  (`server_type="origin"`, `method="GET"`, `code="200"`). Skips an XRootD
  origin, and where the origins take no direct clients.
- `restart-cache`:
  [`framework/testlib/control.py`](../../framework/testlib/control.py) must
  restart a service mid-run
  ([service control](../../README.md#service-control)). Once cache-0 holds
  the object whole, it is restarted. Within 150 seconds, every director must
  name it again, and within 300, it must serve the object. A V2 cache must
  serve it from the store it kept, with an `Age` of at least the seconds
  since it first held it. [WLCG pass](../glossary.md#pass) only. Skips
  without a [control helper](../glossary.md#service-control-helper), under
  [`-p topo-tiny`](../../framework/presets/topo-tiny.sh), and without
  cache-0.
- `pool`: [`testlib.common.pool()`](../../framework/testlib/common.py) must
  drop the tasks it has not started when an exception that no suite catches,
  such as the [watchdog](../glossary.md#watchdog)'s, leaves its block. With
  ten tasks queued on two workers, such an exception must cancel the eight
  waiting at once, and let the two running end; a normal exit must wait for
  all ten. WLCG pass only.
- `confined`: the [dev container](../glossary.md#dev-container) must not
  write what the host runs or the framework's own code
  ([Isolation](../development.md#isolation)). Opening `fed.sh` or
  `framework/testlib/common.py` to append, and making a file in the
  checkout, `framework/testlib/`, `framework/var/`, `framework/var/bin/`,
  `framework/var/bin/host/`, `framework/var/generated/`,
  `framework/var/control/`, `framework/var/data/`,
  `framework/var/data/origin/`, or `framework/var/issuer-keys/`, must each
  fail as on a read-only file system. Nothing is written. WLCG pass only.
