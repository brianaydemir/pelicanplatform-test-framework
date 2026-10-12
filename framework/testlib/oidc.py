"""Logging in to a Pelican server through the identity provider
(`-p auth-oidc`; framework/idp/idp.py), as a browser would; and what
else the provider offers the tests: its users, and approving a device
flow there.

A server sends a user to the provider from its login route
(/api/v1.0/auth/oauth/login; handleOAuthLogin in Pelican's
web_ui/oauth2_client.go): a redirect to the provider's authorization
endpoint, naming the server's client, its callback, and a state, which
the server keeps in a session cookie too. The user logs in at the
provider, which sends them back to the callback with a code; the server
checks the state against its cookie, trades the code for tokens at the
provider's token endpoint, asks its userinfo endpoint who the user is,
finds or makes its record of them, and sets its login cookie
(handleOAuthCallback). The callback is at OIDC.ClientRedirectHostname,
localhost and the server's published port, for a browser on the host;
the tests follow it to the server's own host instead.
"""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from . import accounts, common, web

# The provider's users, as fed.sh gives the `idp` service them.
USERS_FILE = os.path.join(common.FRAMEWORK, "etc", "idp-users.json")
# What the provider logs, under framework/var: a JSON object per request.
LOG = "data/idp/requests.log"
# The servers' client, under framework/var, which the provider takes.
CLIENT_ID_FILE = "generated/oidc-client-id"

ENABLED = "/api/v1.0/auth/oauth"
LOGIN = "/api/v1.0/auth/oauth/login"
CALLBACK = "/api/v1.0/auth/oauth/callback"
# The cookie in which a server keeps the state of a login it began.
SESSION_COOKIE = "pelican-session"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class LoginError(Exception):
    """A login did not go as it should."""


@dataclass(frozen=True)
class IdpUser:
    """A user of the provider, and what it says of them."""

    username: str
    password: str
    name: str
    email: str
    groups: tuple[str, ...]
    preferred_username: str  # the claim a server takes the username from
    sub: str  # the subject the provider names them by


def users(issuer: str) -> dict[str, IdpUser]:
    """The provider's users, by username, given its issuer URL, as it
    fills in what USERS_FILE leaves out (idp.py)."""
    with open(USERS_FILE, encoding="utf-8") as f:
        parsed = common.as_object(json.load(f)) or {}
    found: dict[str, IdpUser] = {}
    for name, listed in parsed.items():
        user = common.as_object(listed) or {}
        found[name] = IdpUser(
            name,
            str(user.get("password", "")),
            str(user.get("name") or name),
            str(user.get("email") or f"{name}@idp.test"),
            tuple(str(g) for g in common.as_array(user.get("groups")) or []),
            str(user.get("preferred_username") or name),
            str(user.get("sub") or f"{issuer}/users/{name}"),
        )
    return found


def client_id() -> str:
    """The client ID that the servers present to the provider."""
    with open(CLIENT_ID_FILE, encoding="utf-8") as f:
        return f.read().strip()


@dataclass(frozen=True)
class Started:
    """A login begun at a server: the session cookie it set, and the
    authorization URL it sent the user to."""

    session: str
    authorize_url: str


def begin(web_url: str, next_url: str = "/") -> Started:
    """Begin a login at the server at web_url, to return to next_url.
    Raises LoginError if the server sends the user nowhere, or keeps no
    session."""
    answer = web.request("GET", f"{web_url}{LOGIN}?{urlencode({'nextUrl': next_url})}")
    session = answer.cookie(SESSION_COOKIE)
    location = answer.header("Location")
    if answer.status != 307 or not location:
        raise LoginError(f"GET {web_url}{LOGIN}: {answer.describe()}, not a redirect")
    if not session:
        raise LoginError(f"GET {web_url}{LOGIN}: no {SESSION_COOKIE} cookie")
    return Started(session, location)


def authorize(authorize_url: str, user: IdpUser) -> web.Response:
    """Log user in at the provider's authorization endpoint, as its login
    form does, with the request the server put in authorize_url: the
    provider's answer, a redirect (302) to the callback with the code."""
    parts = urlsplit(authorize_url)
    form = {k: v[0] for k, v in parse_qs(parts.query).items()}
    form.update({"username": user.username, "password": user.password})
    return web.request(
        "POST",
        urlunsplit(parts._replace(query="")),
        upload=urlencode(form).encode(),
        headers=FORM,
    )


def callback_at(web_url: str, location: str) -> str:
    """The callback that the provider sends the user to (location), at the
    server's own host (web_url) rather than OIDC.ClientRedirectHostname."""
    parts = urlsplit(location)
    return f"{web_url}{parts.path}?{parts.query}"


def finish(session: str, callback: str) -> web.Response:
    """GET callback at the server, with the session cookie of the login
    it belongs to: the server's answer, a redirect that sets the login
    cookie if all went well."""
    return web.request("GET", callback, headers={"Cookie": f"{SESSION_COOKIE}={session}"})


def login(web_url: str, user: IdpUser) -> str:
    """Log user in at the server at web_url through the provider: the
    login cookie. Raises LoginError if any step fails."""
    started = begin(web_url)
    sent = authorize(started.authorize_url, user)
    location = sent.header("Location")
    if sent.status != 302 or not location:
        raise LoginError(f"logging {user.username} in at the provider: {sent.describe()}")
    done = finish(started.session, callback_at(web_url, location))
    cookie = done.cookie("login")
    if done.status != 307 or not cookie:
        raise LoginError(
            f"GET {web_url}{CALLBACK} as {user.username}: {done.describe()}"
            + (", with no login cookie" if done.status == 307 else f": {done.body[:200]!r}")
        )
    return cookie


def api(web_url: str, cookie: str) -> accounts.Api:
    """The server's web API, as the user whose login cookie this is."""
    return accounts.Api(web_url, None, cookie)


def whoami(web_url: str, cookie: str) -> dict[str, Any]:
    """What the server says of the user whose login cookie this is."""
    return api(web_url, cookie).call("GET", accounts.WHOAMI).object


def approve_device(issuer: str, user_code: str, user: IdpUser) -> web.Response:
    """Approve the device flow with user_code at the provider, as user
    logging in at its verification page: the provider's answer."""
    form = {"user_code": user_code, "username": user.username, "password": user.password}
    return web.request(
        "POST", f"{issuer}/device", upload=urlencode(form).encode(), headers=FORM
    )


def user_code_in(line: str) -> Optional[str]:
    """The user code of the provider's verification URL
    (<issuer>/device?user_code=...) in a line of a client's output; None
    if the line names none."""
    for word in line.split():
        parts = urlsplit(word)
        if parts.path.endswith("/device"):
            found = parse_qs(parts.query).get("user_code")
            if found:
                return found[0]
    return None


def log_entries() -> list[dict[str, Any]]:
    """What the provider has logged, in order: a JSON object per
    request; none if there is no log yet."""
    try:
        with open(LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    found: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = common.as_object(json.loads(line))
        except ValueError:
            entry = None
        if entry is not None:
            found.append(entry)
    return found


def count(entries: Iterable[dict[str, Any]], **fields: str) -> int:
    """How many of entries have each of fields."""
    return sum(1 for e in entries if all(e.get(k) == v for k, v in fields.items()))
