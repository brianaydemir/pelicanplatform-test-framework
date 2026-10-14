# Redirects

Part of [What the tests check](README.md).

A plain HTTP client, such as curl, asks a director for an object and
follows the redirect (307) it gets, presenting its token again where it
lands; the Pelican client asks for a cache or an origin by name
(`/api/v1.0/director/object/` or `/origin/`), but curl and browsers ask
for the bare path, which the director sends to a cache, or under
`-p director-default-origin` (`Director.DefaultResponse`) to an origin.
The `federation` suite's `redirects` check follows each director's
redirects (and, under `-p director-ha`, the load balancer's, which the
federation names as its director, and so which curl is given) for each
exported namespace, with the namespace's `server` token (none for a
read of `/public`):

- A GET and a HEAD of the object's bare path must land at a cache, or
  under `-p director-default-origin` at an origin; one with
  `?directread` at an origin, where the namespace takes direct clients,
  and otherwise find none (405), as the Pelican client's `--direct`
  asks; and the object and origin routes at a cache and an origin.
  Where the federation has no cache (`-p topo-no-cache`), whatever is
  meant for a cache must land at an origin instead, where the namespace
  takes direct clients (the director falls back to an origin with
  `DirectReads`; `redirectToCache` in `director/director.go`), and
  otherwise find none (404); `directors` checks the same of what the
  object route names.
- What lands at a cache must be served: the object's bytes, or for a
  HEAD its `Content-Length`. What lands at an origin must be served
  only where the namespace takes direct clients, and otherwise refused
  (401 or 403), as the origin refuses every direct client there.
- A bare-path PUT of a new object, with the namespace's token, must
  land at an origin, where the namespace takes writes, which must then
  store it; where it takes none, the director must find no origin, or
  the one it names refuse, and no origin may hold the object.

`hints` checks what the same redirects tell the client about the
namespace ([authorization](authorization.md)).
