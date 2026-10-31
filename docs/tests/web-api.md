# Web API

Part of [What the tests check](README.md).

Every Pelican server serves a web API under `/api/v1.0/` on its web port.
The `webapi` suite checks who may use it, at every server of the shape, once
per web URL (so just `fed` in the tiny topology). It runs in the WLCG pass
only ([token profiles](README.md#token-profiles)). A case's row is named
`<scenario> <case>`.

The suite acts as each server's admin, by a bearer token of the server's own
making or a login's cookie, and as `test-webapi`, an ordinary user.

- `admin-matrix`: At every server, in rows named `<server> <case>`:

  - each admin GET (`config`, `tokens`, `log-level`, `logs`, `health`,
    `pprof`, and `users`) is 401 with no credentials, 403 for `test-webapi`,
    and 200 for the admin. `health` is open where
    `Server.HealthMonitoringPublic` is set; `pprof` skips without
    `Server.EnablePprof`. `tokens` takes only a login's cookie: the admin's
    bearer token is 403 (`tokens-bearer`);
  - `POST /restart` (`restart`), `PATCH /config` (`config-patch`), and
    `PUT /aup` (`aup-put`), with harmless bodies, are 401 with no
    credentials and 403 for the user (never sent as the admin);
  - at `/config`, a token like the admin's but signed by another key
    (`bearer-other-key`), from a namespace's issuer
    (`bearer-namespace-issuer`), or expired (`bearer-expired`) is 401; for
    an unknown subject (`bearer-no-record`), 401 or 403;
  - the registry's admin routes (`approve`, `deny`, `delete-namespace`,
    `delete-server`), for a registration and server that do not exist, with
    a valid CSRF token, are 401 with no credentials, 403 for the user, and
    404 for the admin.

- `api-tokens`: At the first director, logged in as its admin. Skips for a
  standalone origin.

  - `create`: A token of `pelican.director_service_discovery` is made (200
    or 201), as `<ID>.<secret>`.
  - `use`: With it, `/api/v1.0/director/discoverServers` lists every origin
    and federated cache ([advertisements](advertisements.md)).
  - `list`: The token list shows its ID and name, but no secret or hash.
  - `wrong-secret`, `bogus`, `delete`, and `expired`: The token with another
    secret, a token that never was, the token once deleted, and a 10-second
    token once expired (it works before) are each 403, not a server error.
  - `bearer-create`: Making a token with the admin's bearer token instead of
    the cookie is 403.
  - `admin-grantable`: The admin may make a token of `monitoring.query`,
    which no role derives (Pelican's `docs/user-group-design.md`), and query
    the director's Prometheus with it. A refused creation must not be
    answered 200.

- `login`: At every server:

  - `admin`: `POST /api/v1.0/auth/login` as `admin`/`asdf` is 200, with a
    `login` cookie that is `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`,
    and `Max-Age=57600` (16 hours).
  - `wrong-password` and `missing-field`: 401 and 400.
  - `whoami-none`, `whoami-admin`, and `whoami-user`: `/whoami` says no one
    is logged in, with no `X-CSRF-Token`; the role `admin` with the admin's
    cookie; and `user` with `test-webapi`'s token.
  - `logout`: 200, and the cookie cleared.
  - `logout-replay`: The cookie, presented again, counts as none (401)
    (OWASP ASVS 3.3.1).
  - `rate-limit`: Of two logins at once, the second, with an
    `X-Forwarded-For` the server must not trust (`Server.TrustedProxies`),
    is 429 (`Server.UILoginRateLimit`).

- `csrf`:

  - `registry-<how>-<case>`: As the registry's admin, by cookie and by
    bearer token, a PATCH approving a missing registration is 403
    (`CSRF token invalid`) with no `X-CSRF-Token` (`no-token`) or one not
    matching its `_gorilla_csrf` cookie (`mismatch`), and 404 with a
    matching one (`valid`).
  - `<server> cross-origin`: A logout with the admin's cookie and
    `Origin: https://evil.invalid` is 403. With their bearer token from
    there, or the cookie and no `Origin`, it is 200.

- `malformed-path`: `/%C0%AF` and `/api/v1.0/%C0%AF` (not UTF-8) are 400,
  not a server error, and `/api/v1.0/version` then still answers. A row per
  server.

- `version`: Every server's `/api/v1.0/version` reports `PELICAN_TAG`'s
  version, without the `v` ([knobs](../configuration.md#knobs)) (`servers`);
  with images of the user's own (`IMAGE_*`), the row only reports them.
  Directors' and the registry's responses carry `Server: pelican/<version>`
  (`header <server> <role>`). Each director lists each server with the
  version it reports (`listed <server>`).
