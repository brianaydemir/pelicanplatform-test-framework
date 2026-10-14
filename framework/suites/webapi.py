"""Who may use each server's web API (/api/v1.0/), which every Pelican
server serves on its web port: its admin routes; a director's API
tokens; logging in, and the login cookie; its guards against cross-site
requests; malformed paths; and the version each server reports.

  admin-matrix    at every server, each admin route refuses no
                  credentials (401) and an ordinary user (403), and
                  serves the admin; a bearer token not of the server's
                  own making counts as no credentials
  api-tokens      a director's API tokens: made only through a login,
                  shown once, and good for their scope until deleted or
                  expired
  login           logging in and out at every server, the login cookie,
                  whoami, and the limit of a login a second
  csrf            the registry's CSRF tokens, and every server's refusal
                  of a cookie's cross-origin POST
  malformed-path  a path that is not UTF-8 gets 400, and the server
                  serves on
  version         every server reports one version, as do its Server
                  header and the directors

The suite acts at each server as its admin, with a bearer token that the
server takes as theirs (credentials.mint_user(), as `pelican cache
introspect` mints the admin's), or with the cookie of a login as `admin`;
and as an ordinary user, `test-webapi`, whom the admin creates at each
server once, and later runs reuse. Each agrees to the server's AUP first
(accounts.agree_to_aup()), which every server has by default, and
without which it refuses them its user routes, as an admin's first login
would. As the admin, it sends no admin route
that would change anything, but the registry's on a registration and a
server that do not exist; and, with no credentials or the user's, the
routes that restart a server and change its configuration and AUP, which
must refuse them. It spaces its logins at each server a second apart,
since a server allows an address a login a second
(Server.UILoginRateLimit), but where it means to exceed that.
"""

import json
import os
import re
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Optional

from testlib import accounts, common, credentials, director, jwt, web
from testlib.common import first
from testlib.director import expected_targets, listing_problems
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session
from testlib.webapi import (
    ADMIN_PASSWORD,
    API,
    CONFIG,
    TOKENS,
    USER,
    Access,
    Expect,
    Server,
    cookie_header,
    judge,
    made_token,
    other_key,
    server_settings,
)

SCENARIOS = {
    "admin-matrix": "each admin route: 401 for no one, 403 for a user, 200 for the admin",
    "api-tokens": "API tokens: made by a login, shown once, good until deleted or expired",
    "login": "login, its cookie, logout, whoami, and the limit of a login a second",
    "csrf": "the registry's CSRF tokens; no server takes a cookie's cross-origin POST",
    "malformed-path": "a path that is not UTF-8 gets 400, and the server serves on",
    "version": "every server reports one version, as its Server header and the directors do",
}

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

LOGOUT = f"{API}/auth/logout"
VERSION = f"{API}/version"
DISCOVER = f"{API}/director/discoverServers"
REGISTRATIONS = f"{API}/registry_ui/namespaces"
REGISTERED_SERVERS = f"{API}/registry_ui/servers"
DIRECTOR_SERVERS = f"{API}/director_ui/servers"

# An origin that is none of the federation's, as a cross-site page has.
EVIL = "https://evil.invalid"

# How long the login cookie lasts, in seconds (setLoginCookie in
# Pelican's web_ui/authentication.go).
COOKIE_MAX_AGE = 57600
# How long the expiring API token lasts, in seconds.
API_TOKEN_EXPIRY = 10

# The admin's routes that only read, by row: each must refuse no
# credentials (401) and an ordinary user (403), and serve the admin (200).
ADMIN_GETS = (
    ("config", CONFIG),
    ("tokens", TOKENS),
    ("log-level", f"{API}/logging/level"),
    ("logs", f"{API}/logs/tail?limit=10"),
    ("health", f"{API}/metrics/health"),
    ("pprof", f"{API}/debug/pprof/"),
    ("users", accounts.USERS),
)
# The admin's routes that change things, by row: its method, path, and
# body. Each is sent with no credentials and as the user only, which it
# must refuse; each body is one that would change nothing, if it got
# through.
ADMIN_CHANGES = (
    ("restart", "POST", f"{API}/restart", None),
    ("config-patch", "PATCH", CONFIG, b"{}"),
    ("aup-put", "PUT", f"{API}/aup", b"{}"),
)
# The registry's admin routes, by row: their method and path, given a
# registration's ID ({ns}) and a server's ({server}). The suite sends
# them for a registration and a server that do not exist.
REGISTRY_ROUTES = (
    ("approve", "PATCH", REGISTRATIONS + "/{ns}/approve"),
    ("deny", "PATCH", REGISTRATIONS + "/{ns}/deny"),
    ("delete-namespace", "DELETE", REGISTRATIONS + "/{ns}"),
    ("delete-server", "DELETE", REGISTERED_SERVERS + "/{server}"),
)

DISCOVERY_SCOPE = "pelican.director_service_discovery"
# A scope that a user may be granted, but the admin is not by default:
# server.admin implies only server.user_admin and server.collection_admin
# (EffectiveScopesForIdentity in web_ui/scopes.go).
UNHELD_SCOPE = "pelican.transfer"


class Missing(Exception):
    """Something the scenario needs is not there."""


def unknown_ids(test: Access, server: Server) -> tuple[str, str]:
    """The ID of a registration and of a server that the registry at
    server does not hold, as its admin finds; raises Missing if it
    cannot show that."""
    ns_id = str(1_000_000_000 + int.from_bytes(os.urandom(3), "big"))
    server_id = f"webapi-{test.run}"
    admin = test.admin(server)
    for path in (f"{REGISTRATIONS}/{ns_id}", f"{REGISTERED_SERVERS}/{server_id}"):
        answer = web.request("GET", server.web_url + path, token=admin)
        if answer.status != 404:
            raise Missing(
                f"GET {path} as the admin: {answer.describe()}, not 404, so the"
                + " suite will not send the registry's admin routes for it"
            )
    return ns_id, server_id


# --------------------------------------------------------------------------
# admin-matrix: the admin routes of AuthHandler, AdminAuthHandler,
# UserAdminAuthHandler, and LogReadAuthHandler (web_ui/ in Pelican), and
# the registry's.


def namespace_issuer(test: Access, server: Server) -> str:
    """An issuer URL of a namespace's that is not server's own local
    issuer: a protected namespace's, or, where that is the server's
    (with the origins' issuer off), the URL an embedded issuer would give
    /protected-a there."""
    local = credentials.local_issuer(test.fed, server.web_url)
    for ns in credentials.PROTECTED:
        if test.fed.issuer_of(ns) != local:
            return test.fed.issuer_of(ns)
    return f"{server.web_url}/api/v1.0/issuer/ns/protected-a"


def strange_tokens(test: Access, server: Server) -> list[tuple[str, str, str, tuple[int, ...]]]:
    """Bearer tokens that server's web API must take for no one: each
    like the admin's, but signed by another key, from a namespace's
    issuer, or expired; and one for a subject of whom the server has no
    record. Each with its row's case, what it is, and the statuses it may
    get at an admin route: 401, or for the subject without a record, 401
    or 403, since the server takes such a subject for a user of no
    standing (AuthHandler)."""
    head, claims = jwt.decode(test.admin(server))
    now = int(time.time())
    key, whose = other_key(test, server)
    expired = {**claims, "iat": now - 600, "nbf": now - 600, "exp": now - 300}
    nobody = {**claims, "sub": f"webapi-nobody-{test.run}"}
    issuer = namespace_issuer(test, server)
    return [
        (
            "other-key",
            f"signed by {whose}",
            jwt.encode(jwt.header(jwt.algorithm(key), jwt.key_id(key)), claims, key=key),
            (401,),
        ),
        (
            "namespace-issuer",
            f"from {issuer}",
            jwt.encode(head, {**claims, "iss": issuer}, key=server.key),
            (401,),
        ),
        ("expired", "expired 300s ago", jwt.encode(head, expired, key=server.key), (401,)),
        (
            "no-record",
            "for a subject with no record",
            jwt.encode(head, nobody, key=server.key),
            (401, 403),
        ),
    ]


def registry_rows(test: Access, server: Server) -> list[Row]:
    """The registry's admin routes, for a registration and a server that
    it does not hold, each with a CSRF token that the registry takes
    (see accounts.csrf()), so that only the credentials decide: none must
    get 401, the user 403, and the admin 404, or for the server, which
    the registry deletes without asking whether it exists
    (deleteServerHandler), 404 or 200."""
    admin, user = test.admin(server), test.user(server)
    guard = accounts.csrf(accounts.Api(server.web_url, admin))
    ns_id, server_id = unknown_ids(test, server)
    rows: list[Row] = []
    for case, method, path in REGISTRY_ROUTES:
        url = server.web_url + path.format(ns=ns_id, server=server_id)
        found = (404, 200, 204) if case == "delete-server" else (404,)
        expects: list[Expect] = [
            ("none", web.request(method, url, headers=guard), (401,)),
            ("user", web.request(method, url, token=user, headers=guard), (403,)),
            ("admin", web.request(method, url, token=admin, headers=guard), found),
        ]
        rows.append(judge(f"{server.name} {case}", expects))
    return rows


def matrix_rows(test: Access, server: Server) -> list[Row]:
    """admin_matrix() at server."""
    admin, user = test.admin(server), test.user(server)
    settings = server_settings(test, server)
    url = server.web_url
    rows: list[Row] = []
    for case, path in ADMIN_GETS:
        if case == "pprof" and settings.get("EnablePprof") is False:
            rows.append((f"{server.name} {case}", SKIP, "Server.EnablePprof is off"))
            continue
        public = case == "health" and settings.get("HealthMonitoringPublic") is True
        if case == "tokens":
            # API tokens are managed only through a login (token.Cookie in
            # createApiToken and listApiTokens).
            as_admin = web.request(
                "GET", url + path, headers=cookie_header(login=test.cookie(server))
            )
        else:
            as_admin = web.request("GET", url + path, token=admin)
        expects: list[Expect] = [
            ("none", web.request("GET", url + path), (200,) if public else (401,)),
            ("user", web.request("GET", url + path, token=user), (200,) if public else (403,)),
            ("admin", as_admin, (200,)),
        ]
        case_name, status, note = judge(f"{server.name} {case}", expects)
        if public and status == PASS:
            note += " (Server.HealthMonitoringPublic)"
        rows.append((case_name, status, note))
    rows.append(
        judge(
            f"{server.name} tokens-bearer",
            [("admin's bearer token", web.request("GET", url + TOKENS, token=admin), (403,))],
        )
    )
    for case, method, path, body in ADMIN_CHANGES:
        sent = accounts.JSON if body is not None else {}
        expects = [
            ("none", web.request(method, url + path, upload=body, headers=sent), (401,)),
            (
                "user",
                web.request(method, url + path, token=user, upload=body, headers=sent),
                (403,),
            ),
        ]
        rows.append(judge(f"{server.name} {case}", expects))
    for case, what, token, allowed in strange_tokens(test, server):
        answer = web.request("GET", url + CONFIG, token=token)
        rows.append(judge(f"{server.name} bearer-{case}", [(what, answer, allowed)]))
    if "registry" in server.roles:
        rows += registry_rows(test, server)
    return rows


def admin_matrix(test: Access) -> list[Row]:
    """At every server, each admin route with no credentials, the user's
    bearer token, and the admin's; and bearer tokens that must count as
    no credentials."""
    rows: list[Row] = []
    for server in test.servers:
        try:
            rows += matrix_rows(test, server)
        except (Missing, accounts.ApiError) as e:
            rows.append((server.name, FAIL, str(e)))
    return rows


# --------------------------------------------------------------------------
# api-tokens: a director's API tokens (createApiToken, listApiTokens,
# and deleteApiToken in web_ui/ui.go, and api_token/api_token.go), which
# its service discovery takes (discoverOriginCache in director/director.go).


def create_token(
    server: Server,
    name: str,
    expires: datetime,
    cookie: str = "",
    token: Optional[str] = None,
    scope: str = DISCOVERY_SCOPE,
) -> web.Response:
    """POST a request for an API token of scope, named name and expiring
    at expires, to server, with the login cookie or the bearer token."""
    body = {"name": name, "scopes": [scope], "expiration": common.timestamp(expires)}
    headers = dict(accounts.JSON)
    if cookie:
        headers.update(cookie_header(login=cookie))
    return web.request(
        "POST",
        server.web_url + TOKENS,
        token=token,
        upload=json.dumps(body).encode(),
        headers=headers,
    )


def discover(server: Server, token: str) -> web.Response:
    """The director's service discovery, with token."""
    return web.request("GET", server.web_url + DISCOVER, token=token)


def listed_problems(answer: web.Response, token_id: str, name: str, secret: str) -> list[str]:
    """What is wrong with answer, the listing of API tokens: it must list
    the token token_id, named name, and show no secret: not secret, and no
    field of a hash."""
    if answer.status != 200:
        return [f"listing: {answer.describe()}, not 200"]
    entries = [common.as_object(e) or {} for e in common.as_array(answer.json()) or []]
    problems: list[str] = []
    if not any(e.get("id") == token_id and e.get("name") == name for e in entries):
        problems.append(f"the listing lacks {token_id}, named {name}")
    if secret.encode() in answer.body:
        problems.append("the listing shows the token's secret")
    hashed = sorted({k for e in entries for k in e if "hash" in k.lower() or k == "token"})
    if hashed:
        problems.append(f"the listing has fields {', '.join(hashed)}")
    return problems


def api_tokens(test: Access) -> list[Row]:
    """At the first director, as its admin, logged in: make an API token
    of the service discovery's scope, use it, list it, present it with a
    wrong secret, and a bogus one; delete it, and use it again; make one
    that expires in API_TOKEN_EXPIRY seconds, and use it after; ask for
    one with the admin's bearer token instead of the cookie; and ask for
    one of a scope that the admin does not hold. Every token made is
    deleted afterward."""
    if not test.fed.directors:
        return [("", SKIP, "a standalone origin has no director")]
    server = test.server_at(test.fed.directors[0])
    cookie = test.cookie(server)
    name = f"webapi-{test.run}"
    made: list[str] = []
    rows: list[Row] = []
    try:
        answer = create_token(
            server, name, datetime.now(timezone.utc) + timedelta(hours=1), cookie
        )
        token = made_token(answer)
        if token:
            made.append(token[1])
        if answer.status not in (200, 201) or token is None:
            return [("create", FAIL, f"{answer.describe()}, with no API token")]
        full, token_id, secret = token
        rows.append(("create", PASS, f"{answer.describe()}, {token_id}"))
        used = discover(server, full)
        unlisted = listing_problems(test.fed, used.json()) if used.status == 200 else []
        case, status, note = judge("use", [("discoverServers", used, (200,))], unlisted)
        if status == PASS:
            note = f"{len(expected_targets(test.fed))} servers"
        rows.append((case, status, note))
        listed = web.request(
            "GET", server.web_url + TOKENS, headers=cookie_header(login=cookie)
        )
        problems = listed_problems(listed, token_id, name, secret)
        rows.append(("list", FAIL, first(problems)) if problems else ("list", PASS, token_id))
        wrong = f"{token_id}.{os.urandom(32).hex()}"
        bogus = f"{os.urandom(3).hex()[:5]}.{os.urandom(32).hex()}"
        rows.append(
            judge("wrong-secret", [("discoverServers", discover(server, wrong), (403,))])
        )
        rows.append(judge("bogus", [("discoverServers", discover(server, bogus), (403,))]))
        deleted = web.request(
            "DELETE",
            f"{server.web_url}{TOKENS}/{token_id}",
            headers=cookie_header(login=cookie),
        )
        if deleted.ok:
            made.remove(token_id)
        rows.append(
            judge(
                "delete",
                [("delete", deleted, (200, 204)), ("then", discover(server, full), (403,))],
            )
        )
        rows += expiring_rows(server, cookie, f"{name}-expiring", made)
        answer = create_token(
            server,
            f"{name}-bearer",
            datetime.now(timezone.utc) + timedelta(hours=1),
            token=test.admin(server),
        )
        token = made_token(answer)
        if token:
            made.append(token[1])
        rows.append(judge("bearer-create", [("admin's bearer token", answer, (403,))]))
        # A token may carry a user-grantable scope only if its creator
        # holds it (validateScopesForCreator in api_token/api_token.go).
        answer = create_token(
            server,
            f"{name}-unheld",
            datetime.now(timezone.utc) + timedelta(hours=1),
            cookie,
            scope=UNHELD_SCOPE,
        )
        token = made_token(answer)
        if token:
            made.append(token[1])
        rows.append(
            judge(
                "unheld-scope",
                [(UNHELD_SCOPE, answer, (400, 403))],
                ["it handed out a token"] if token else [],
            )
        )
    finally:
        for token_id in made:
            gone = web.request(
                "DELETE",
                f"{server.web_url}{TOKENS}/{token_id}",
                headers=cookie_header(login=cookie),
            )
            if not gone.ok:
                common.warn(f"could not delete API token {token_id}: {gone.describe()}")
    return rows


def expiring_rows(server: Server, cookie: str, name: str, made: list[str]) -> list[Row]:
    """Make an API token that expires in API_TOKEN_EXPIRY seconds, which
    must work at once, and be refused (403) two seconds after it expires.
    Its ID goes in made."""
    expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=API_TOKEN_EXPIRY
    )
    answer = create_token(server, name, expires, cookie)
    token = made_token(answer)
    if token is None:
        return [("expired", FAIL, f"making it: {answer.describe()}, with no API token")]
    made.append(token[1])
    before = discover(server, token[0])
    wait = expires.timestamp() + 2 - time.time()
    if wait > 0:
        time.sleep(wait)
    after = discover(server, token[0])
    return [judge("expired", [("before", before, (200,)), ("after", after, (403,))])]


# --------------------------------------------------------------------------
# login: loginHandler, logoutHandler, and whoamiHandler in
# web_ui/authentication.go, and loginRateLimitMiddleware in
# web_ui/middleware.go.


def set_cookie(answer: web.Response, name: str) -> Optional[tuple[str, dict[str, str]]]:
    """The value of the cookie name that answer sets, and its attributes,
    by lower-case name (a flag's value is ""); None if it sets none."""
    for key, value in answer.headers:
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            pair, *attributes = [p.strip() for p in value.split(";")]
            found: dict[str, str] = {}
            for attribute in attributes:
                attr, _, attr_value = attribute.partition("=")
                found[attr.strip().lower()] = attr_value.strip()
            return pair[len(name) + 1 :], found
    return None


def cookie_problems(answer: web.Response) -> list[str]:
    """What is wrong with the login cookie that answer sets: it must be
    HttpOnly, Secure, SameSite=Lax, for Path=/, and last 16 hours."""
    found = set_cookie(answer, "login")
    if found is None or not found[0]:
        return ["it set no login cookie"]
    attributes = found[1]
    problems = [
        f"the cookie is not {flag}"
        for flag in ("HttpOnly", "Secure")
        if flag.lower() not in attributes
    ]
    wanted = {"samesite": "lax", "path": "/", "max-age": str(COOKIE_MAX_AGE)}
    for attr, value in wanted.items():
        if attributes.get(attr, "").lower() != value:
            problems.append(f"the cookie's {attr} is {attributes.get(attr)!r}, not {value!r}")
    return problems


def cleared(answer: web.Response) -> list[str]:
    """What is wrong with how answer, a logout's, clears the login cookie:
    it must set it empty, and already expired."""
    found = set_cookie(answer, "login")
    if found is None:
        return ["the logout set no login cookie"]
    value, attributes = found
    age = attributes.get("max-age", "")
    if value or not (re.fullmatch(r"-?\d+", age) and int(age) <= 0):
        return [f"the logout's cookie is not empty and expired (Max-Age {age!r})"]
    return []


def whoami(server: Server, token: Optional[str] = None, cookie: str = "") -> web.Response:
    """server's /whoami, with token or cookie."""
    headers = cookie_header(login=cookie) if cookie else {}
    return web.request("GET", server.web_url + accounts.WHOAMI, token=token, headers=headers)


def whoami_row(case: str, answer: web.Response, role: Optional[str], user: str = "") -> Row:
    """A row for case: answer, a /whoami's, must say who is logged in,
    with role, or, for role None, that no one is, and then carry no CSRF
    token."""
    found = common.as_object(answer.json()) or {}
    problems: list[str] = []
    if role is None:
        if found.get("authenticated") is not False:
            problems.append(f"authenticated is {found.get('authenticated')!r}, not false")
        if answer.header("X-CSRF-Token"):
            problems.append("it hands out a CSRF token")
    else:
        if found.get("authenticated") is not True:
            problems.append(f"authenticated is {found.get('authenticated')!r}, not true")
        if found.get("role") != role:
            problems.append(f"role is {found.get('role')!r}, not {role!r}")
        if user and found.get("user") != user:
            problems.append(f"user is {found.get('user')!r}, not {user!r}")
    return judge(case, [("whoami", answer, (200,))], problems)


def login_rows(test: Access, server: Server) -> list[Row]:
    """login() at server."""
    name = server.name
    rows: list[Row] = []
    good = test.post_login(server, {"user": "admin", "password": ADMIN_PASSWORD})
    rows.append(judge(f"{name} admin", [("admin/asdf", good, (200,))], cookie_problems(good)))
    wrong = test.post_login(server, {"user": "admin", "password": f"wrong-{test.run}"})
    rows.append(judge(f"{name} wrong-password", [("wrong password", wrong, (401,))]))
    missing = test.post_login(server, {"user": "admin"})
    rows.append(judge(f"{name} missing-field", [("no password", missing, (400,))]))
    rows.append(whoami_row(f"{name} whoami-none", whoami(server), None))
    found = set_cookie(good, "login")
    cookie = found[0] if found else ""
    if not cookie:
        rows.append((f"{name} whoami-admin", SKIP, "the login set no cookie"))
        rows.append((f"{name} logout", SKIP, "the login set no cookie"))
    else:
        rows.append(whoami_row(f"{name} whoami-admin", whoami(server, cookie=cookie), "admin"))
        out = web.request("POST", server.web_url + LOGOUT, headers=cookie_header(login=cookie))
        rows.append(judge(f"{name} logout", [("logout", out, (200,))], cleared(out)))
        # The cookie a logout cleared must count as none, as if the
        # browser had kept it.
        replay = web.request(
            "GET", server.web_url + CONFIG, headers=cookie_header(login=cookie)
        )
        rows.append(
            judge(f"{name} logout-replay", [("the cookie after logout", replay, (401,))])
        )
    user = whoami(server, token=test.user(server))
    rows.append(whoami_row(f"{name} whoami-user", user, "user", USER))
    # Two logins at once, the second claiming another address, which a
    # server that trusts no proxy (Server.TrustedProxies) must not believe.
    admin = {"user": "admin", "password": ADMIN_PASSWORD}
    one = test.post_login(server, admin, early=True)
    two = test.post_login(server, admin, {"X-Forwarded-For": "203.0.113.7"}, spaced=False)
    rows.append(
        judge(f"{name} rate-limit", [("first", one, (200,)), ("second, at once", two, (429,))])
    )
    return rows


def login(test: Access) -> list[Row]:
    """At every server: logging in as the admin, with a wrong password,
    and with a field missing, and the cookie; logging out, and the cookie
    after; who is logged in, with no credentials, the admin's cookie, and
    the user's token; and two logins at once."""
    rows: list[Row] = []
    for server in test.servers:
        try:
            rows += login_rows(test, server)
        except (Missing, accounts.ApiError) as e:
            rows.append((server.name, FAIL, str(e)))
    return rows


# --------------------------------------------------------------------------
# csrf: the registry's CSRF tokens (gorilla/csrf, from config/csrf.go,
# before every route of registry/registry_ui.go's), and isSameOriginRequest
# in web_ui/authentication.go, before every AuthHandler route.


def csrf_refused(answer: web.Response) -> list[str]:
    """What is wrong with answer, which the registry's CSRF guard must
    have refused: 403, with its message."""
    if answer.status == 403 and b"CSRF token invalid" in answer.body:
        return []
    return [f"{answer.describe()}, not 403 'CSRF token invalid'"]


def registry_csrf(test: Access) -> list[Row]:
    """At the registry, as its admin, by cookie and by bearer token,
    approve a registration that does not exist: with no CSRF token, and
    with one that does not match its cookie, the registry must refuse
    (403 'CSRF token invalid'); with a matching one, it must get as far
    as finding none (404). gorilla/csrf exempts no bearer token."""
    if not test.fed.registry_url:
        return [("registry", SKIP, "a standalone origin has no registry")]
    server = test.server_at(test.fed.registry_url)
    admin = test.admin(server)
    api = accounts.Api(server.web_url, admin)
    first_guard, second_guard = accounts.csrf(api), accounts.csrf(api)
    csrf_cookie = first_guard["Cookie"].split("=", 1)[1]
    ns_id, _ = unknown_ids(test, server)
    url = f"{server.web_url}{REGISTRATIONS}/{ns_id}/approve"
    login_cookie = test.cookie(server)
    by = {
        "cookie": (None, {"_gorilla_csrf": csrf_cookie, "login": login_cookie}),
        "bearer": (admin, {"_gorilla_csrf": csrf_cookie}),
    }
    rows: list[Row] = []
    for how, (token, cookies) in by.items():
        base = {**cookie_header(**cookies), "Origin": server.web_url}
        cases = (
            ("no-token", base, csrf_refused),
            ("mismatch", {**base, "X-CSRF-Token": second_guard["X-CSRF-Token"]}, csrf_refused),
            ("valid", {**base, "X-CSRF-Token": first_guard["X-CSRF-Token"]}, None),
        )
        for case, headers, check in cases:
            answer = web.request("PATCH", url, token=token, headers=headers)
            if check is None:
                rows.append(judge(f"registry-{how}-{case}", [("approve", answer, (404,))]))
            else:
                problems = check(answer)
                note = first(problems) if problems else "HTTP 403 (CSRF token invalid)"
                rows.append((f"registry-{how}-{case}", FAIL if problems else PASS, note))
    return rows


def cross_origin(test: Access, server: Server) -> Row:
    """A logout at server, which AuthHandler guards: with the admin's
    cookie, from another site's page (Origin), it must be refused (403);
    with the admin's bearer token from there, and with the cookie and no
    Origin, it must succeed (200), judged on the credentials alone. The
    cookie is a fresh one, since the logout may end it."""
    cookie = test.login(server)
    url = server.web_url + LOGOUT
    expects: list[Expect] = [
        (
            "cookie from elsewhere",
            web.request("POST", url, headers={**cookie_header(login=cookie), "Origin": EVIL}),
            (403,),
        ),
        (
            "bearer from elsewhere",
            web.request("POST", url, token=test.admin(server), headers={"Origin": EVIL}),
            (200,),
        ),
        (
            "cookie, no Origin",
            web.request("POST", url, headers=cookie_header(login=cookie)),
            (200,),
        ),
    ]
    return judge(f"{server.name} cross-origin", expects)


def csrf(test: Access) -> list[Row]:
    """registry_csrf(), and cross_origin() at every server."""
    rows = registry_csrf(test)
    for server in test.servers:
        try:
            rows.append(cross_origin(test, server))
        except accounts.ApiError as e:
            rows.append((f"{server.name} cross-origin", FAIL, str(e)))
    return rows


# --------------------------------------------------------------------------
# malformed-path: rejectInvalidPathMiddleware in web_ui/middleware.go.


def malformed_path(test: Access) -> list[Row]:
    """At every server, GET of a path whose percent-encoding is not UTF-8
    (an overlong encoding of /), at the root and under the API, must get
    400; then the server must still answer."""
    rows: list[Row] = []
    for server in test.servers:
        expects: list[Expect] = [
            ("/%C0%AF", web.request("GET", server.web_url + "/%C0%AF"), (400,)),
            (f"{API}/%C0%AF", web.request("GET", f"{server.web_url}{API}/%C0%AF"), (400,)),
            ("then /version", web.request("GET", server.web_url + VERSION), (200,)),
        ]
        rows.append(judge(server.name, expects))
    return rows


# --------------------------------------------------------------------------
# version: getVersionHandler in web_ui/ui.go, ServerHeaderMiddleware in
# web_ui/middleware.go, and the directors' list of servers.


def reported_version(server: Server) -> tuple[Optional[str], str]:
    """The version that server reports (GET /api/v1.0/version), or None
    and why not."""
    answer = web.request("GET", server.web_url + VERSION)
    found = (common.as_object(answer.json()) or {}).get("version")
    if answer.status != 200 or not isinstance(found, str) or not found:
        return None, f"{server.name}: {answer.describe()}, with no version"
    return found, ""


def header_row(case: str, answer: web.Response, reported: str) -> Row:
    """A row for case: answer must carry `Server: pelican/<reported>`."""
    want = f"pelican/{reported}"
    got = answer.header("Server")
    if answer.status is None:
        return case, FAIL, "no response"
    return (
        (case, PASS, got) if got == want else (case, FAIL, f"Server is {got!r}, not {want!r}")
    )


def version(test: Access) -> list[Row]:
    """Every server's version: each must report one, the same, and
    PELICAN_TAG's, unless the shape runs images of the user's own, when
    the row only reports them. A director's responses, and the
    registry's, must name it in their Server header; and each director
    must list each server it knows with the version it reports."""
    versions: dict[str, str] = {}
    problems: list[str] = []
    for server in test.servers:
        found, why = reported_version(server)
        if found is None:
            problems.append(why)
        else:
            versions[server.name] = found
    expected = test.fed.pelican_version
    seen = ", ".join(f"{name} {v}" for name, v in versions.items())
    if expected is not None:
        problems += [
            f"{n} reports {v}, not {expected}" for n, v in versions.items() if v != expected
        ]
    rows: list[Row] = []
    if problems:
        rows.append(("servers", FAIL, first(problems)))
    elif expected is None:
        rows.append(("servers", PASS, f"images of your own: {seen}"))
    else:
        rows.append(("servers", PASS, f"{len(versions)} server(s), {expected}"))
    by_host = {director.host(s.web_url): s.name for s in test.servers}
    for server in test.servers:
        mine = versions.get(server.name)
        if mine is None:
            continue
        if "director" in server.roles:
            listing = web.request("GET", server.web_url + DIRECTOR_SERVERS)
            rows.append(header_row(f"header {server.name} director", listing, mine))
            rows.append(listing_row(server, listing, by_host, versions))
        if "registry" in server.roles:
            answer = web.request("GET", server.web_url + REGISTRATIONS)
            rows.append(header_row(f"header {server.name} registry", answer, mine))
    return rows


def listing_row(
    server: Server,
    listing: web.Response,
    by_host: Mapping[str, str],
    versions: Mapping[str, str],
) -> Row:
    """A row for the director server's list of servers, listing: each
    server of the shape in it must have the version that server
    reports."""
    case = f"listed {server.name}"
    if listing.status != 200:
        return case, FAIL, f"{DIRECTOR_SERVERS}: {listing.describe()}"
    problems: list[str] = []
    count = 0
    for entry in (common.as_object(e) or {} for e in common.as_array(listing.json()) or []):
        name = by_host.get(director.host(str(entry.get("webUrl", ""))))
        if name is None or name not in versions:
            continue
        count += 1
        if entry.get("version") != versions[name]:
            problems.append(
                f"it lists {entry.get('name')} at {entry.get('version')!r},"
                + f" not {versions[name]!r}"
            )
    if count == 0:
        problems.append("it lists none of the shape's servers")
    return (case, FAIL, first(problems)) if problems else (case, PASS, f"{count} server(s)")


RUN: dict[str, Callable[[Access], list[Row]]] = {
    "admin-matrix": admin_matrix,
    "api-tokens": api_tokens,
    "login": login,
    "csrf": csrf,
    "malformed-path": malformed_path,
    "version": version,
}


def skip(_fed: common.Federation) -> Optional[str]:
    """Never: every server has a web API."""
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and leave the next suite free to log
    in at once."""
    test = Access(session)
    try:
        for name in selected:
            try:
                rows = RUN[name](test)
            except (Missing, accounts.ApiError) as e:
                rows = [("", FAIL, str(e))]
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                rows = [("", FAIL, f"{type(e).__name__}: {e}")]
            for case, status, note in rows:
                results.add(f"{name} {case}" if case else name, status, note)
    finally:
        test.wait_for_logins()
