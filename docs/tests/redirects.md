# Redirects

Part of [What the tests check](README.md).

The [`federation`](federation.md) suite's `redirects` scenario follows each
director's redirects for the probe, as a
[plain HTTP client](../glossary.md#plain-http-client), in each exported
[namespace](../glossary.md#namespace), with its `server`
[credential](../glossary.md#credential) (none to read `/public`). Under
[`-p director-ha`](../../framework/presets/director-ha.sh), it follows the
load balancer's too.

- A GET and a HEAD of the bare path must land at a cache, or under
  [`-p director-default-origin`](../../framework/presets/director-default-origin.sh)
  (`Director.DefaultResponse`), an origin.
- `/api/v1.0/director/object/` must land at a cache, and `/origin/` at an
  origin.
- `?directread` must land at an origin where the namespace takes direct
  clients, and otherwise get 405.
- Under [`-p topo-no-cache`](../../framework/presets/topo-no-cache.sh), a
  read meant for a cache must land at an origin where the namespace takes
  direct clients, or else get 404.
- A cache must serve what lands there; an origin, where the namespace takes
  direct clients, and otherwise refuse it (401 or 403).
- A bare-path PUT must be stored at an origin where the namespace takes
  writes, and otherwise refused, and stored nowhere.
