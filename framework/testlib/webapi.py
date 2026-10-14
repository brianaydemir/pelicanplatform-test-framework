"""Every server's web API (/api/v1.0/), on its web port, and who may use it.

They are its admin, by a bearer token of the server's own making
(credentials.mint_user(), as `pelican cache introspect` mints the
admin's) or by the cookie of a login as `admin`; and an ordinary user,
`test-webapi`, whom the admin creates at each server once, and later
runs reuse. Access holds those credentials for every server of a shape.
The admin, and then the user, agree to each server's AUP first
(accounts.agree_to_aup()), without which it refuses them its user
routes. Access spaces its logins at each server a second apart, since a
server allows an address a login a second (Server.UILoginRateLimit),
counted by whole seconds of the clock.
"""

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from . import accounts, common, credentials, director, jwt, web
from .common import first
from .report import FAIL, PASS
from .session import Session

API = "/api/v1.0"
CONFIG = f"{API}/config"
TOKENS = f"{API}/tokens"

# The built-in admin's password (README.md), and the ordinary user.
ADMIN_PASSWORD = "asdf"  # nosec B105
USER = "test-webapi"

# How long the bearer tokens last, in seconds: a whole suite, logins
# spaced out included.
TOKEN_LIFETIME = 3600
# How far apart logins to one server are sent, in seconds: a server
# allows an address a login a second (Server.UILoginRateLimit).
LOGIN_SPACING = 1.1
# How far into a second of the clock the first of two logins that must
# fall in one second may be sent, in seconds.
EARLY = 0.3

# What an API token looks like (ApiTokenRegex in Pelican's
# api_token/api_token.go): an ID, and the secret, in hex.
API_TOKEN = re.compile(r"^([a-zA-Z0-9]{5})\.([a-zA-Z0-9]{64})$")


@dataclass(frozen=True)
class Server:
    """A server's web API: one per web URL, so one for everything in `tiny`.

    Attributes:
        name: Its service, as rows name it, e.g. origin-0 (`fed` in
            `tiny`).
        key_dir: The directory (under framework/var) of its keys.
        roles: Which of origin, cache, director, and registry it is.
    """

    name: str
    web_url: str
    key_dir: str
    roles: frozenset[str]

    @property
    def key(self) -> str:
        """Its active signing key."""
        return common.signing_key(self.key_dir)


def servers(fed: common.Federation) -> list[Server]:
    """Return every server of the shape, each once, by web URL.

    They are the origins (another owner's included), the caches
    (site-local ones included), the directors, and the registry.
    """
    found: dict[str, tuple[str, str, set[str]]] = {}

    def add(name: str, web_url: str, key_dir: str, role: str) -> None:
        found.setdefault(web_url, (name, key_dir, set()))[2].add(role)

    for origin in fed.origins:
        add(origin.svc, origin.web_url, f"issuer-keys/{origin.svc}", "origin")
    for owner in fed.owners:
        add(owner.origin.svc, owner.web_url, owner.key_dir, "origin")
    for cache in fed.caches:
        add(cache.svc, cache.web_url, f"issuer-keys/{cache.svc}", "cache")
    for url in fed.directors:
        add(director.host(url), url, f"issuer-keys/{director.host(url)}", "director")
    if fed.registry_url:
        host = director.host(fed.registry_url)
        add(host, fed.registry_url, f"issuer-keys/{host}", "registry")
    return [
        Server(name, url, key_dir, frozenset(roles))
        for url, (name, key_dir, roles) in found.items()
    ]


class Access:
    """The admin's and user's tokens, cookies, and logins at each server.

    Attributes:
        servers: The federation's servers.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.servers = servers(self.fed)
        self._tokens: dict[tuple[str, str], str] = {}
        self._cookies: dict[str, str] = {}
        self._last_login: dict[str, float] = {}

    def server_at(self, web_url: str) -> Server:
        """Return the server whose web URL is web_url."""
        return next(s for s in self.servers if s.web_url == web_url)

    def token(self, server: Server, username: str) -> str:
        """Return a bearer token that server's web API takes as username's.

        It lasts TOKEN_LIFETIME seconds (see credentials.mint_user()).
        """
        if (server.name, username) not in self._tokens:
            self._tokens[(server.name, username)] = credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/webapi.{server.name}.{username}"),
                credentials.local_issuer(self.fed, server.web_url),
                server.key,
                username,
                TOKEN_LIFETIME,
            )
        return self._tokens[(server.name, username)]

    def admin(self, server: Server) -> str:
        """Return the admin's bearer token for server."""
        return self.token(server, "admin")

    def user(self, server: Server) -> str:
        """Return the ordinary user's bearer token for server.

        Its admin creates the user first, if need be. The admin, and then
        the user, agree to the server's AUP, without which it refuses them
        its user routes, whatever else they may do there.

        Raises:
            accounts.ApiError: Any of that fails.
        """
        if (server.name, USER) not in self._tokens:
            admin = accounts.Api(server.web_url, self.admin(server))
            accounts.agree_to_aup(admin)
            accounts.ensure_user(admin, USER)
            accounts.agree_to_aup(accounts.Api(server.web_url, self.token(server, USER)))
        return self.token(server, USER)

    def post_login(
        self,
        server: Server,
        fields: Mapping[str, str],
        headers: Optional[Mapping[str, str]] = None,
        spaced: bool = True,
        early: bool = False,
    ) -> web.Response:
        """POST fields to server's login route, as a login form does.

        Args:
            server: Where to log in.
            fields: The form's fields.
            headers: Headers to send besides the form's content type.
            spaced: Whether to send it no sooner than LOGIN_SPACING
                seconds after the last login there.
            early: Whether to send it in the first EARLY of a second of
                the clock, which the server and the tests share, since the
                server counts logins by the whole second (gin-rate-limit's
                InMemoryStore).
        """
        wait = self._last_login.get(server.web_url, 0) + LOGIN_SPACING - time.time()
        if spaced and wait > 0:
            time.sleep(wait)
        if early and time.time() % 1 > EARLY:
            time.sleep(1.01 - time.time() % 1)
        answer = web.request(
            "POST",
            server.web_url + accounts.LOGIN,
            upload=urlencode(fields).encode(),
            headers={**accounts.FORM, **(headers or {})},
        )
        self._last_login[server.web_url] = time.time()
        return answer

    def login(self, server: Server) -> str:
        """Return a fresh login cookie of the admin's at server.

        Raises:
            accounts.ApiError: The login fails.
        """
        answer = web.Response(None)
        for _ in range(accounts.LOGIN_TRIES):
            answer = self.post_login(server, {"user": "admin", "password": ADMIN_PASSWORD})
            if answer.status != 429:
                break
        cookie = answer.cookie("login")
        if answer.status != 200 or not cookie:
            raise accounts.ApiError(
                f"logging in to {server.name} as admin: {answer.describe()}, with no cookie"
            )
        return cookie

    def cookie(self, server: Server) -> str:
        """Return the admin's login cookie at server, made once.

        It is for routes that take only a cookie. A cookie that a check may
        log out is a fresh one of login()'s.
        """
        if server.name not in self._cookies:
            self._cookies[server.name] = self.login(server)
        return self._cookies[server.name]

    def wait_for_logins(self) -> None:
        """Wait until the next suite may log in at once anywhere."""
        last = max(self._last_login.values(), default=0)
        wait = last + LOGIN_SPACING - time.time()
        if wait > 0:
            time.sleep(wait)


def cookie_header(**cookies: str) -> dict[str, str]:
    """Return a Cookie header of cookies."""
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


# An expected answer: a label for the note, the server's answer, and the
# statuses it may have.
Expect = tuple[str, web.Response, tuple[int, ...]]


def judge(
    case: str, expects: Sequence[Expect], extra: Sequence[str] = ()
) -> tuple[str, str, str]:
    """Return a row for case: whether each answer has a status it may.

    Args:
        case: The row's case.
        expects: The answers, each with the statuses it may have.
        extra: Other problems, any of which fails the row.

    Returns:
        The row: case, PASS or FAIL, and a note that says what each
        answer got, or else the first problem.
    """
    problems = [
        f"{label}: {answer.describe()}, not {' or '.join(map(str, allowed))}"
        for label, answer, allowed in expects
        if answer.status not in allowed
    ]
    problems += list(extra)
    seen = ", ".join(f"{label} {answer.status}" for label, answer, _ in expects)
    return (case, FAIL, first(problems)) if problems else (case, PASS, seen)


def server_settings(access: Access, server: Server) -> dict[str, Any]:
    """Return server's Server.* settings, as its admin reads them.

    Returns:
        The settings, from its configuration (GET /api/v1.0/config); {}
        if that fails.
    """
    answer = web.request("GET", server.web_url + CONFIG, token=access.admin(server))
    found = common.as_object(answer.json()) or {}
    return common.as_object(found.get("Server")) or {}


def other_key(access: Access, server: Server) -> tuple[str, str]:
    """Return a key that is not server's, and what it is.

    Returns:
        The key: another server's, or where every server shares it
        (`tiny`, or a standalone origin), test-keys/unknown.pem, which no
        server knows; and what it is.
    """
    own = jwt.public_jwk(server.key)
    for other in access.servers:
        if jwt.public_jwk(other.key) != own:
            return other.key, f"{other.name}'s key"
    return credentials.keys(access.fed)["unknown"], "unknown.pem"


def made_token(answer: web.Response) -> Optional[tuple[str, str, str]]:
    """Return the API token that answer hands out, if it does.

    Args:
        answer: The answer to a POST of /api/v1.0/tokens.

    Returns:
        The token, its ID, and its secret; None if there is none.
    """
    found = API_TOKEN.match(str((common.as_object(answer.json()) or {}).get("token", "")))
    return (found.group(0), found.group(1), found.group(2)) if found else None
