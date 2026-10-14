# Web API

Part of [What the tests check](README.md).

Every Pelican server serves a web API under `/api/v1.0/` on its web
port, behind middleware of `web_ui/authentication.go`: `AuthHandler`
finds who is asking, from a bearer token of the server's own making or
a login's cookie, and `AdminAuthHandler`, `UserAdminAuthHandler`, and
`LogReadAuthHandler` decide whether they may. The `webapi` suite checks
those decisions at every server of the shape: each origin (origin-2
under `-p topo-multi-owner` too), cache (site-local ones too),
director, and the registry, each once by its web URL, so in the `tiny`
topology just `fed`.

It acts at each server as its admin, with a bearer token signed with
the server's own key, naming its local issuer, as `pelican cache
introspect` mints the admin's (`credentials.mint_user()`), or with the
cookie of a login as `admin`; and as `test-webapi`, an ordinary user,
whom the admin creates at each server once, and later runs reuse. Every
server enforces a built-in acceptable use policy unless
`Server.AUPFile` is `none`, and until a user agrees to it, refuses them
its routes for users and groups (`RequireAUPCompliance`), its admin
included; so the admin, and then `test-webapi`, first agree to the
server's current one (`POST /api/v1.0/me/aup`), as each would on first
logging in. A server allows an address one login a second
(`Server.UILoginRateLimit`), counted by whole seconds of the clock, so
the suite sends its logins to a server more than a second apart, but
where it means to exceed that, and leaves a second before the next
suite.

- `admin-matrix`: at every server, a GET of each admin route
  (`/config`, `/tokens`, `/logging/level`, `/logs/tail`,
  `/metrics/health`, `/debug/pprof/`, which `Server.EnablePprof` turns
  on here, and `/users`) with no credentials must get 401; with
  `test-webapi`'s token, 403; and as the admin, 200. `/metrics/health`
  is open to all where `Server.HealthMonitoringPublic` is set, which the
  suite reads from the server's configuration, as its admin. A server
  manages API tokens only through a login (`createApiToken` and
  `listApiTokens` read only the cookie), so `/tokens` is asked as the
  admin with their cookie, and must refuse their bearer token (403).
  `POST /restart`, `PATCH /config`, and `PUT /aup`, each with a body
  that would change nothing, are sent with no credentials and as the
  user only, which must get 401 and 403. Tokens that the server must
  take for no one, at `/config`: like the admin's, but signed by another
  server's key (one that differs from the server's own; `unknown.pem`
  where every server shares it), naming a namespace's issuer rather
  than the server's local one, or expired, must get 401. One for a
  subject of whom the server has no record may get 401 or 403: Pelican
  takes it for a user of no standing (`AuthHandler`), and refuses it
  with 403, and at `/users` with 401, since `RequireAUPCompliance` finds
  no user to ask about; either grants nothing. At the registry, its
  admin routes (`PATCH .../namespaces/<id>/approve` and `/deny`,
  `DELETE .../namespaces/<id>`, and `DELETE .../servers/<id>`), for a
  registration and a server that its admin first finds it does not hold
  (404), each with a CSRF token that it takes, so that only the
  credentials decide: no credentials must get 401, the user 403, and the
  admin 404; but the registry deletes a server without asking whether it
  exists (`deleteServerHandler`), so there 200 is accepted too. These
  are the only admin routes the suite sends as the admin that would
  change anything, were the IDs real.
- `api-tokens`, at the first director, logged in as its admin: a POST
  to `/api/v1.0/tokens` for a token of
  `pelican.director_service_discovery` must get 200 or 201 and the
  token, `<ID>.<secret>`; with it, the director's service discovery
  (`/api/v1.0/director/discoverServers`) must list every origin and
  federated cache, as `advertise`'s `discover-servers` checks it. The
  list of tokens must show it, by ID and name, and neither its secret
  nor any hash of it. The token with another secret, a well-formed
  token that never was, the token once deleted (which must get 200 or
  204), and a token made to expire in 10 seconds, two seconds after it
  does, must each be refused with 403, not a server error; the last
  must work before it expires. A POST for a token with the admin's
  bearer token instead of the cookie must get 403. A token may carry a
  scope that a user may be granted only if its creator holds it
  (`validateScopesForCreator`), and the admin holds no
  `pelican.transfer` (`server.admin` implies only `server.user_admin`
  and `server.collection_admin`), so a POST for a token of it must be
  refused (400 or 403), with no token. Pelican refuses it, but answers
  200, the status of its check of the cookie (`createApiToken` in
  `web_ui/ui.go`), so the `unheld-scope` row fails, on main and
  v26.0.0-rc.3 alike. Every token made is deleted afterward. It skips
  for a standalone origin.
- `login`, at every server: `POST /api/v1.0/auth/login` (form fields
  `user` and `password`) as `admin`/`asdf` must get 200, and a `login`
  cookie that is `HttpOnly`, `Secure`, `SameSite=Lax`, for `Path=/`,
  with `Max-Age=57600` (16 hours); with a wrong password, 401; and
  without the password, 400. `/whoami` with no credentials must say no
  one is logged in (`authenticated: false`), and hand out no
  `X-CSRF-Token`; with the admin's cookie, the role `admin`; and with
  `test-webapi`'s token, the role `user`. A logout with the cookie must
  get 200, and clear it (empty, and already expired); and the cookie,
  presented again, must then count as none (401 at `/config`), as a
  logout must end the session it logs out of (OWASP ASVS 3.3.1).
  Pelican's login cookie is a token that the server checks by its
  signature and expiry alone, and its logout only asks the browser to
  drop it, so the `logout-replay` rows fail, on main and v26.0.0-rc.3
  alike. Last, two logins at once, the second with `X-Forwarded-For`
  naming another address, which a server that trusts no proxy
  (`Server.TrustedProxies`) must not believe: the first must get 200,
  and the second 429.
- `csrf`: the registry guards each route of its web UI's API with
  gorilla/csrf, which refuses a request that changes anything unless its
  `X-CSRF-Token` matches its `_gorilla_csrf` cookie (both from
  `/whoami`). As the registry's admin, by cookie and by bearer token, a
  PATCH approving a registration that does not exist must get 403
  (`CSRF token invalid`) with no token, and with one from another
  `/whoami` that does not match the cookie; gorilla/csrf exempts no
  bearer token. With a matching one, it must get 404. At every server, a
  logout (an `AuthHandler` route) with the admin's cookie and
  `Origin: https://evil.invalid` must get 403 (`isSameOriginRequest`);
  with their bearer token from there, and with the cookie and no
  `Origin`, 200, since a browser sends no bearer token of its own
  accord, nor a cross-site request without an `Origin`.
- `malformed-path`: at every server, a GET of `/%C0%AF` and of
  `/api/v1.0/%C0%AF`, an overlong encoding of `/` that is not UTF-8,
  must get 400 (`rejectInvalidPathMiddleware`), not a server error; then
  `/api/v1.0/version` must still get 200.
- `version`: every server's `/api/v1.0/version` must report the same
  `version`, `PELICAN_TAG`'s without the `v`, where the shape runs its
  images (`fed.sh` writes it in `framework/var/generated/pelican-version`,
  or `custom` where any `IMAGE_*` the shape uses is the user's own,
  when the row only reports what each server says). Each director's
  responses (`director_ui/servers`), and the registry's
  (`registry_ui/namespaces`), must carry `Server: pelican/<version>`;
  and each director must list each server of the shape it knows with
  the version that server reports.
