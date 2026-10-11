"""A Pelican server's web API, as one of its users: JSON requests with
the user's bearer token (credentials.mint_user()); its users and
groups; and logging a user in (login()), for the cookie that stands for
them elsewhere.

Users and groups are records in each server's database (Pelican's
docs/user-group-design.md). A user administrator creates users (the
built-in `admin` is one), and any user creates groups and adds members
to their own. A bearer token's subject is resolved to the user record
of that name, so a user must exist before a token means anything. A
group may match the embedded issuer's Issuer.AuthorizationTemplates
only if it is eligible to, which only an administrator may make it.
"""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from . import common, web

USERS = "/api/v1.0/users"
GROUPS = "/api/v1.0/groups"
WHOAMI = "/api/v1.0/auth/whoami"
LOGIN = "/api/v1.0/auth/login"
REDEEM_PASSWORD = "/api/v1.0/invites/redeem/password"  # nosec B105

JSON = {"Content-Type": "application/json"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# The server allows an address one login a second (Server.UILoginRateLimit).
LOGIN_TRIES = 5


class ApiError(Exception):
    """The server did not do as asked."""


@dataclass(frozen=True)
class Answer:
    """A server's answer to a JSON request."""

    status: Optional[int]  # None if no response came
    data: Any  # the decoded JSON body, or None

    @property
    def ok(self) -> bool:
        """Whether the status is a success (2xx)."""
        return self.status is not None and 200 <= self.status < 300

    @property
    def object(self) -> dict[str, Any]:
        """The body, if it is a JSON object; {} otherwise."""
        return common.as_object(self.data) or {}

    @property
    def array(self) -> list[Any]:
        """The body, if it is a JSON array; [] otherwise."""
        return common.as_array(self.data) or []

    def describe(self) -> str:
        """The status, and the server's message if it sent one."""
        if self.status is None:
            return "no response"
        message = self.object.get("msg") or self.object.get("error")
        return f"HTTP {self.status}" + (f" ({message})" if message else "")


@dataclass(frozen=True)
class Api:
    """The web API of the server at url, as the user whose token this is."""

    url: str
    token: Optional[str]

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Answer:
        """Send method to path, with body as JSON if given, and headers."""
        upload = None if body is None else json.dumps(body).encode()
        sent = dict(headers or {})
        if body is not None:
            sent.update(JSON)
        answer = web.request(
            method, self.url + path, token=self.token, upload=upload, headers=sent
        )
        data: Any = None
        if answer.body:
            try:
                data = json.loads(answer.body)
            except ValueError:
                data = None
        return Answer(answer.status, data)


def must(answer: Answer, what: str) -> Answer:
    """answer, if it succeeded; raises ApiError otherwise."""
    if not answer.ok:
        raise ApiError(f"{what}: {answer.describe()}")
    return answer


def csrf(api: Api) -> dict[str, str]:
    """The headers that let api's user change things through a route that
    guards against cross-site requests, as the registry's do
    (gorilla/csrf): the token that /whoami answers with, and the cookie it
    sets, which the server checks against each other; and an Origin that
    is the server's own, which it checks too. Raises ApiError if /whoami
    hands out no token."""
    answer = web.request("GET", api.url + WHOAMI, token=api.token)
    token, cookie = answer.header("X-CSRF-Token"), answer.cookie("_gorilla_csrf")
    if answer.status != 200 or not token or not cookie:
        raise ApiError(f"GET {WHOAMI}: {answer.describe()}, with no CSRF token")
    return {"X-CSRF-Token": token, "Cookie": f"_gorilla_csrf={cookie}", "Origin": api.url}


def find_user(api: Api, username: str) -> Optional[str]:
    """The ID of the user named username, if there is one."""
    for user in must(api.call("GET", USERS), "listing users").array:
        found = common.as_object(user) or {}
        if found.get("username") == username:
            return str(found.get("id", ""))
    return None


def ensure_user(api: Api, username: str) -> str:
    """The ID of the user named username, created if need be, as a user
    administrator."""
    found = find_user(api, username)
    if found:
        return found
    made = must(api.call("POST", USERS, {"username": username}), f"creating user {username}")
    return str(made.object.get("id", ""))


def find_group(api: Api, name: str) -> Optional[str]:
    """The ID of the group named name, if there is one the caller may
    see."""
    for group in must(api.call("GET", GROUPS), "listing groups").array:
        found = common.as_object(group) or {}
        if found.get("name") == name:
            return str(found.get("id", ""))
    return None


def create_group(api: Api, name: str, eligible: bool = False) -> str:
    """Create the group named name, as its owner: its ID. With eligible,
    ask that it may match the issuer's authorization templates, which
    the server grants only an administrator."""
    body: dict[str, Any] = {"name": name}
    if eligible:
        body["authTemplateEligible"] = True
    made = must(api.call("POST", GROUPS, body), f"creating group {name}")
    return str(made.object.get("id", ""))


def ensure_group(api: Api, name: str, eligible: bool = False) -> str:
    """The ID of the group named name, created if need be (see
    create_group())."""
    return find_group(api, name) or create_group(api, name, eligible)


def set_eligible(api: Api, group_id: str, eligible: bool) -> Answer:
    """Set whether the group may match the issuer's authorization
    templates, which only an administrator may."""
    return api.call("PATCH", f"{GROUPS}/{group_id}", {"authTemplateEligible": eligible})


def add_member(api: Api, group_id: str, user_id: str) -> None:
    """Add the user to the group, as its owner; a member already is
    one."""
    must(
        api.call("POST", f"{GROUPS}/{group_id}/members", {"userId": user_id}),
        f"adding {user_id} to group {group_id}",
    )


def remove_member(api: Api, group_id: str, user_id: str) -> None:
    """Remove the user from the group, as its owner."""
    must(
        api.call("DELETE", f"{GROUPS}/{group_id}/members/{user_id}"),
        f"removing {user_id} from group {group_id}",
    )


def delete_group(api: Api, group_id: str) -> Optional[str]:
    """Delete the group, as its owner or an administrator; why that
    failed, or None."""
    answer = api.call("DELETE", f"{GROUPS}/{group_id}")
    return None if answer.ok else f"deleting group {group_id}: {answer.describe()}"


# --------------------------------------------------------------------------
# Logging in. An administrator never sets a user's password: they mint
# an invite, with which the user sets their own. The login's cookie then
# stands for the user, and carries the groups they are in
# (Issuer.GroupSource: internal in framework/config.d/base/40-origin.yaml).


def password_invite(api: Api, user_id: str) -> str:
    """An invite with which the user may set their password, minted as a
    user administrator."""
    made = must(
        api.call("POST", f"{USERS}/{user_id}/password-invite", {}),
        f"minting a password invite for {user_id}",
    )
    return str(made.object.get("inviteToken", ""))


def set_password(web_url: str, invite: str, password: str) -> None:
    """Set the password of the user the invite was minted for."""
    answer = web.request(
        "POST",
        web_url + REDEEM_PASSWORD,
        upload=json.dumps({"token": invite, "password": password}).encode(),
        headers=JSON,
    )
    if answer.status != 200:
        raise ApiError(f"POST {REDEEM_PASSWORD}: {answer.describe()}")


def login(web_url: str, username: str, password: str) -> str:
    """Log in to the server at web_url as username: the login cookie.
    Waits out the server's limit of a login a second."""
    form = urlencode({"user": username, "password": password}).encode()
    answer = web.Response(None)
    for _ in range(LOGIN_TRIES):
        answer = web.request("POST", web_url + LOGIN, upload=form, headers=FORM)
        if answer.status != 429:
            break
        time.sleep(1.1)
    cookie = answer.cookie("login")
    if answer.status != 200 or not cookie:
        raise ApiError(f"POST {LOGIN} as {username}: {answer.describe()}, with no login cookie")
    return cookie
