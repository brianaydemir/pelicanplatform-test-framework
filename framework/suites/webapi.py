"""Who may use each server's web API (/api/v1.0/).

See docs/tests/web-api.md.

The suite acts at each server as its admin, and as an ordinary user
(testlib.webapi.Access). As the admin, it sends no route that would
change anything, but those that make the user and API tokens, and the
registry's for a registration and a server that do not exist. It sends
the routes that restart a server and change its configuration and AUP
only with no credentials or the user's, which they must refuse.
"""

import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from functools import partial

from testlib import accounts, common, credentials, director, issuer, jwt, web
from testlib.common import outcome
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
    create_token,
    delete_token,
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

# Every scenario: each presents only tokens of a server's web API, which
# Pelican mints in the WLCG profile alone (see suites/__init__.py).
ONE_PROFILE = tuple(SCENARIOS)

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

# How long the login cookie lasts, in seconds: 16 hours.
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
PROMQL = f"{API}/prometheus/query"


class Missing(Exception):
    """Something the scenario needs is not there."""


def each_server(test: Access, rows_of: Callable[[Server], list[Row]], case: str = "") -> list[Row]:
    """Return rows_of(server)'s rows for every server.

    A server where it raises Missing or accounts.ApiError gets one failing
    row instead, named for the server and case.
    """
    rows: list[Row] = []
    for server in test.servers:
        try:
            rows += rows_of(server)
        except (Missing, accounts.ApiError) as e:
            rows.append((f"{server.name} {case}".strip(), FAIL, str(e)))
    return rows


def unknown_ids(test: Access, server: Server) -> tuple[str, str]:
    """Return IDs of a registration and a server that the registry lacks.

    The registry is server's, and its admin finds that it holds neither.

    Raises:
        Missing: Its admin cannot show that.
    """
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
# UserAdminAuthHandler, and LogReadAuthHandler (Pelican's web_ui/), and
# the registry's.


def namespace_issuer(test: Access, server: Server) -> str:
    """Return an issuer URL of a namespace's, not server's own local issuer.

    That is a protected namespace's, or, where that is the server's (with
    the origins' issuer off), the URL an embedded issuer would give
    /protected-a there.
    """
    local = credentials.local_issuer(test.fed, server.web_url)
    for ns in credentials.PROTECTED:
        if test.fed.issuer_of(ns) != local:
            return test.fed.issuer_of(ns)
    return issuer.url(server.web_url, "/protected-a")


def strange_tokens(test: Access, server: Server) -> list[tuple[str, str, str, tuple[int, ...]]]:
    """Return bearer tokens that server's web API must take for no one.

    Each is like the admin's, but signed by another key, from a namespace's
    issuer, or expired; and one is for a subject of whom the server has no
    record.

    Returns:
        For each token, its row's case, what it is, the token, and the
        statuses it may get at an admin route: 401, or for the subject
        without a record, 401 or 403, either of which grants nothing.
    """
    head, claims = jwt.decode(test.admin(server))
    now = int(time.time())
    key, whose = other_key(test, server)
    expired = {**claims, "iat": now - 600, "nbf": now - 600, "exp": now - 300}
    nobody = {**claims, "sub": f"webapi-nobody-{test.run}"}
    other_issuer = namespace_issuer(test, server)
    return [
        (
            "other-key",
            f"signed by {whose}",
            jwt.sign(claims, key),
            (401,),
        ),
        (
            "namespace-issuer",
            f"from {other_issuer}",
            jwt.encode(head, {**claims, "iss": other_issuer}, key=server.key),
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
    """Return the rows of the registry's admin routes at server.

    Each is sent for a registration and a server that the registry does not
    hold, with a CSRF token that it takes (see accounts.csrf()), so that
    only the credentials decide: none must get 401, the user 403, and the
    admin 404.
    """
    admin, user = test.admin(server), test.user(server)
    guard = accounts.csrf(accounts.Api(server.web_url, admin))
    ns_id, server_id = unknown_ids(test, server)
    rows: list[Row] = []
    for case, method, path in REGISTRY_ROUTES:
        url = server.web_url + path.format(ns=ns_id, server=server_id)
        expects: list[Expect] = [
            ("none", web.request(method, url, headers=guard), (401,)),
            ("user", web.request(method, url, token=user, headers=guard), (403,)),
            ("admin", web.request(method, url, token=admin, headers=guard), (404,)),
        ]
        rows.append(judge(f"{server.name} {case}", expects))
    return rows


def matrix_rows(test: Access, server: Server) -> list[Row]:
    """Return admin_matrix()'s rows for server."""
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
            # API tokens are managed only through a login.
            as_admin = web.request("GET", url + path, headers=cookie_header(login=test.cookie(server)))
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
    """Check each admin route at every server.

    Each is tried with no credentials, the user's bearer token, and the
    admin's; and with bearer tokens that must count as no credentials.
    """
    return each_server(test, partial(matrix_rows, test))


# --------------------------------------------------------------------------
# api-tokens: a director's API tokens (Pelican's web_ui/ui.go and
# api_token/), which its service discovery takes.


def kept(answer: web.Response, made: list[str]) -> tuple[str, str, str] | None:
    """Return the API token that answer hands out (made_token()), if any.

    Its ID goes in made.
    """
    token = made_token(answer)
    if token:
        made.append(token[1])
    return token


def discover(server: Server, token: str) -> web.Response:
    """GET the director's service discovery, with token."""
    return web.request("GET", server.web_url + DISCOVER, token=token)


def query_up(server: Server, token: str) -> web.Response:
    """Ask server's Prometheus for `up`, an instant query, with token."""
    return web.request("GET", f"{server.web_url}{PROMQL}?query=up", token=token)


def query_problems(answer: web.Response) -> list[str]:
    """Return what is wrong with answer, a Prometheus query's, if it is 200.

    A 200 must carry a successful query (Prometheus's own envelope).
    """
    if answer.status != 200:
        return []
    return [] if answer.object().get("status") == "success" else ["the 200 is no successful query"]


def listed_problems(answer: web.Response, token_id: str, name: str, secret: str) -> list[str]:
    """Return what is wrong with answer, the listing of API tokens.

    It must list the token token_id, named name, and show no secret: not
    secret, and no field of a hash.
    """
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


def expiring_rows(server: Server, cookie: str, name: str, made: list[str]) -> list[Row]:
    """Check an API token that expires in API_TOKEN_EXPIRY seconds.

    It must work at once, and be refused (403) two seconds after it expires.
    Its ID goes in made.
    """
    expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=API_TOKEN_EXPIRY)
    answer = create_token(server.web_url, name, [DISCOVERY_SCOPE], expires, cookie)
    token = kept(answer, made)
    if token is None:
        return [("expired", FAIL, f"making it: {answer.describe()}, with no API token")]
    before = discover(server, token[0])
    wait = expires.timestamp() + 2 - time.time()
    if wait > 0:
        time.sleep(wait)
    after = discover(server, token[0])
    return [judge("expired", [("before", before, (200,)), ("after", after, (403,))])]


def api_tokens(test: Access) -> list[Row]:
    """Check the first director's API tokens.

    At the first director, as its admin, logged in: make an API token
    of the service discovery's scope, use it, list it, present it with a
    wrong secret, and a bogus one; delete it, and use it again; make one
    that expires in API_TOKEN_EXPIRY seconds, and use it after; ask for
    one with the admin's bearer token instead of the cookie; and make one
    of a user-grantable scope that no role derives, which must be minted
    and then work. Every token made is deleted afterward.
    """
    if not test.fed.directors:
        return [("", SKIP, "a standalone origin has no director")]
    server = test.server_at(test.fed.directors[0])
    cookie = test.cookie(server)
    name = f"webapi-{test.run}"
    made: list[str] = []
    rows: list[Row] = []
    try:
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        answer = create_token(server.web_url, name, [DISCOVERY_SCOPE], expires, cookie)
        token = kept(answer, made)
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
        listed = web.request("GET", server.web_url + TOKENS, headers=cookie_header(login=cookie))
        problems = listed_problems(listed, token_id, name, secret)
        rows.append(("list", *outcome(problems, token_id)))
        wrong = f"{token_id}.{os.urandom(32).hex()}"
        bogus = f"{os.urandom(3).hex()[:5]}.{os.urandom(32).hex()}"
        rows.append(judge("wrong-secret", [("discoverServers", discover(server, wrong), (403,))]))
        rows.append(judge("bogus", [("discoverServers", discover(server, bogus), (403,))]))
        deleted = delete_token(server.web_url, token_id, cookie)
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
            server.web_url, f"{name}-bearer", [DISCOVERY_SCOPE], expires, token=test.admin(server)
        )
        kept(answer, made)
        rows.append(judge("bearer-create", [("admin's bearer token", answer, (403,))]))
        # A token keeps a user-grantable scope only while its creator
        # holds it, and a creator who holds server.admin holds them all
        # (Pelican's docs/user-group-design.md). So the admin must be able
        # to mint a token of monitoring.query, which no role derives (an
        # outside Grafana queries a director's Prometheus with one), and
        # it must query the director's Prometheus; a refusal must not be
        # answered 200.
        answer = create_token(server.web_url, f"{name}-query", [credentials.QUERY_SCOPE], expires, cookie)
        token = kept(answer, made)
        if token is None:
            rows.append(("admin-grantable", FAIL, f"{answer.describe()}, with no API token"))
        else:
            queried = query_up(server, token[0])
            rows.append(
                judge(
                    "admin-grantable",
                    [(credentials.QUERY_SCOPE, answer, (200, 201)), ("query", queried, (200,))],
                    query_problems(queried),
                )
            )
    finally:
        for token_id in made:
            gone = delete_token(server.web_url, token_id, cookie)
            if not gone.ok:
                common.warn(f"could not delete API token {token_id}: {gone.describe()}")
    return rows


# --------------------------------------------------------------------------
# login: logging in and out, /whoami, and the login rate limit (Pelican's
# web_ui/authentication.go and web_ui/middleware.go).


def set_cookie(answer: web.Response, name: str) -> tuple[str, dict[str, str]] | None:
    """Return the cookie name that answer sets.

    Returns:
        Its value, and its attributes by lower-case name (a flag's value is
        ""); None if answer sets none.
    """
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
    """Return what is wrong with the login cookie that answer sets.

    It must be HttpOnly, Secure, SameSite=Lax, for Path=/, and last 16
    hours.
    """
    found = set_cookie(answer, "login")
    if found is None or not found[0]:
        return ["it set no login cookie"]
    attributes = found[1]
    problems = [
        f"the cookie is not {flag}" for flag in ("HttpOnly", "Secure") if flag.lower() not in attributes
    ]
    wanted = {"samesite": "lax", "path": "/", "max-age": str(COOKIE_MAX_AGE)}
    for attr, value in wanted.items():
        if attributes.get(attr, "").lower() != value:
            problems.append(f"the cookie's {attr} is {attributes.get(attr)!r}, not {value!r}")
    return problems


def cleared(answer: web.Response) -> list[str]:
    """Return what is wrong with how answer clears the login cookie.

    The answer, a logout's, must set the cookie empty, and already expired.
    """
    found = set_cookie(answer, "login")
    if found is None:
        return ["the logout set no login cookie"]
    value, attributes = found
    age = attributes.get("max-age", "")
    if value or not (re.fullmatch(r"-?\d+", age) and int(age) <= 0):
        return [f"the logout's cookie is not empty and expired (Max-Age {age!r})"]
    return []


def whoami(server: Server, token: str | None = None, cookie: str = "") -> web.Response:
    """GET server's /whoami, with token or cookie."""
    headers = cookie_header(login=cookie) if cookie else {}
    return web.request("GET", server.web_url + accounts.WHOAMI, token=token, headers=headers)


def whoami_row(case: str, answer: web.Response, role: str | None, user: str = "") -> Row:
    """Return case's row: answer, a /whoami's, must say who is logged in.

    Args:
        case: The row's case.
        answer: The answer.
        role: The role it must name; None if no one is logged in, when
            it must carry no CSRF token either.
        user: The user it must name, if any.
    """
    found = answer.object()
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
    """Return login()'s rows for server."""
    name = server.name
    admin = {"user": "admin", "password": ADMIN_PASSWORD}
    rows: list[Row] = []
    good = test.post_login(server, admin)
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
        replay = web.request("GET", server.web_url + CONFIG, headers=cookie_header(login=cookie))
        rows.append(judge(f"{name} logout-replay", [("the cookie after logout", replay, (401,))]))
    user = whoami(server, token=test.user(server))
    rows.append(whoami_row(f"{name} whoami-user", user, "user", USER))
    # Two logins at once, the second claiming another address, which a
    # server that trusts no proxy (Server.TrustedProxies) must not believe.
    one = test.post_login(server, admin, early=True)
    two = test.post_login(server, admin, {"X-Forwarded-For": "203.0.113.7"}, spaced=False)
    rows.append(judge(f"{name} rate-limit", [("first", one, (200,)), ("second, at once", two, (429,))]))
    return rows


def login(test: Access) -> list[Row]:
    """Check logging in and out, and who is logged in, at every server.

    That is, logging in as the admin, with a wrong password, and with a
    field missing, and the cookie; logging out, and the cookie after; who is
    logged in, with no credentials, the admin's cookie, and the user's
    token; and two logins at once.
    """
    return each_server(test, partial(login_rows, test))


# --------------------------------------------------------------------------
# csrf: the registry's CSRF tokens (gorilla/csrf, before every route of
# its web UI's API), and the same-origin check before every AuthHandler
# route.


def csrf_refused(answer: web.Response) -> list[str]:
    """Return what is wrong with answer, which must be a CSRF refusal.

    The registry's CSRF guard must have refused it: 403, with its message.
    """
    if answer.status == 403 and b"CSRF token invalid" in answer.body:
        return []
    return [f"{answer.describe()}, not 403 'CSRF token invalid'"]


def registry_csrf(test: Access) -> list[Row]:
    """Check the registry's CSRF tokens.

    At the registry, as its admin, by cookie and by bearer token,
    approve a registration that does not exist: with no CSRF token, and
    with one that does not match its cookie, the registry must refuse
    (403 'CSRF token invalid'); with a matching one, it must get as far
    as finding none (404). gorilla/csrf exempts no bearer token.
    """
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
                rows.append((f"registry-{how}-{case}", *outcome(problems, "HTTP 403 (CSRF token invalid)")))
    return rows


def cross_origin(test: Access, server: Server) -> Row:
    """Check that a logout at server refuses a cookie from another site.

    AuthHandler guards the logout: with the admin's cookie, from another
    site's page (Origin), it must be refused (403); with the admin's bearer
    token from there, and with the cookie and no Origin, it must succeed
    (200), judged on the credentials alone. The cookie is a fresh one, since
    the logout may end it.
    """
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
    """Run registry_csrf(), and cross_origin() at every server."""
    rows = registry_csrf(test)
    return rows + each_server(test, lambda server: [cross_origin(test, server)], "cross-origin")


# --------------------------------------------------------------------------
# malformed-path: paths that are not UTF-8 (Pelican's web_ui/middleware.go).


def malformed_path(test: Access) -> list[Row]:
    """Check that every server refuses a path that is not UTF-8 (400).

    At every server, GET of a path whose percent-encoding is not UTF-8
    (an overlong encoding of /), at the root and under the API, must get
    400; then the server must still answer.
    """
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
# version: /api/v1.0/version, the Server header, and the directors' list
# of servers.


def reported_version(server: Server) -> tuple[str | None, str]:
    """Return the version that server reports (GET /api/v1.0/version).

    Returns:
        The version and "", or None and why it reports none.
    """
    answer = web.request("GET", server.web_url + VERSION)
    found = answer.object().get("version")
    if answer.status != 200 or not isinstance(found, str) or not found:
        return None, f"{server.name}: {answer.describe()}, with no version"
    return found, ""


def header_row(case: str, answer: web.Response, reported: str) -> Row:
    """Return case's row: answer must carry `Server: pelican/<reported>`."""
    want = f"pelican/{reported}"
    got = answer.header("Server")
    if answer.status is None:
        return case, FAIL, "no response"
    return (case, PASS, got) if got == want else (case, FAIL, f"Server is {got!r}, not {want!r}")


def listing_row(
    server: Server,
    listing: web.Response,
    by_host: Mapping[str, str],
    versions: Mapping[str, str],
) -> Row:
    """Return a row for the director server's list of servers.

    Each server of the shape in it must have the version that server
    reports.

    Args:
        server: A director.
        listing: Its answer to a GET of its list of servers.
        by_host: The name of each of the shape's servers, by host.
        versions: The version that each server reports, by name.
    """
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
                f"it lists {entry.get('name')} at {entry.get('version')!r}, not {versions[name]!r}"
            )
    if count == 0:
        problems.append("it lists none of the shape's servers")
    return (case, *outcome(problems, f"{count} server(s)"))


def version(test: Access) -> list[Row]:
    """Check every server's version.

    Each must report one, the same, and PELICAN_TAG's, unless the shape runs
    images of the user's own, when the row only reports them. A director's
    responses, and the registry's, must name it in their Server header; and
    each director must list each server it knows with the version it
    reports.
    """
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
        problems += [f"{n} reports {v}, not {expected}" for n, v in versions.items() if v != expected]
    note = f"images of your own: {seen}" if expected is None else f"{len(versions)} server(s), {expected}"
    rows: list[Row] = [("servers", *outcome(problems, note))]
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


RUN: dict[str, Callable[[Access], list[Row]]] = {
    "admin-matrix": admin_matrix,
    "api-tokens": api_tokens,
    "login": login,
    "csrf": csrf,
    "malformed-path": malformed_path,
    "version": version,
}


def skip(_fed: common.Federation) -> str | None:
    """Return None: every server has a web API."""
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios.

    Afterward, leave the next suite free to log in at once.
    """
    test = Access(session)
    try:
        for name in selected:
            rows = common.guarded(
                partial(RUN[name], test), lambda note: [("", FAIL, note)], (Missing, accounts.ApiError)
            )
            for case, status, note in rows:
                results.add(f"{name} {case}" if case else name, status, note)
    finally:
        test.wait_for_logins()
