"""A Pelican server's web API, as one of its users.

That is JSON requests with the user's bearer token
(credentials.mint_web_user()) or login cookie; the server's users and
groups; and logging a user in.

Users and groups are records in each server's database (Pelican's
docs/user-group-design.md). A user administrator creates users (the
built-in `admin` is one), and any user creates groups and adds members
to their own. A bearer token's subject is resolved to the user record
of that name, so a user must exist before a token means anything. A
group may match the embedded issuer's Issuer.AuthorizationTemplates
only if it is eligible to, which only an administrator may make it.
"""

import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import common, web

USERS = "/api/v1.0/users"
GROUPS = "/api/v1.0/groups"
WHOAMI = "/api/v1.0/auth/whoami"
LOGIN = "/api/v1.0/auth/login"
REDEEM_PASSWORD = "/api/v1.0/invites/redeem/password"
REDEEM_GROUP = "/api/v1.0/invites/redeem"
AUP = "/api/v1.0/aup"
ME = "/api/v1.0/me"
MY_AUP = "/api/v1.0/me/aup"
MY_GROUPS = "/api/v1.0/me/groups"

# The server allows an address one login a second
# (Server.UILoginRateLimit): how far apart to send logins, in seconds,
# and how many times to try one that it refuses as too soon (429).
LOGIN_SPACING = 1.1
LOGIN_TRIES = 5


class ApiError(Exception):
    """A failure of the server to do as asked."""


@dataclass(frozen=True)
class Answer:
    """A server's answer to a JSON request.

    Attributes:
        status: The HTTP status, or None if no response came.
        data: The decoded JSON body, or None.
    """

    status: int | None
    data: Any

    @property
    def ok(self) -> bool:
        """Whether the status is a success (2xx)."""
        return self.status is not None and 200 <= self.status < 300

    def object(self) -> dict[str, Any]:
        """Return the body, if it is a JSON object; {} otherwise."""
        return common.as_object(self.data) or {}

    def array(self) -> list[Any]:
        """Return the body, if it is a JSON array; [] otherwise."""
        return common.as_array(self.data) or []

    def id(self) -> str:
        """Return the body's `id`, e.g. a created object's; "" if none."""
        return str(self.object().get("id", ""))

    def describe(self) -> str:
        """Return the status, and the server's message if it sent one."""
        if self.status is None:
            return "no response"
        message = self.object().get("msg") or self.object().get("error")
        return f"HTTP {self.status}" + (f" ({message})" if message else "")


@dataclass(frozen=True)
class Api:
    """The web API of a server, as one of its users.

    Attributes:
        url: The server's web URL.
        token: The user's bearer token, if any.
        cookie: The user's login cookie, if any.
    """

    url: str
    token: str | None
    cookie: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Answer:
        """Send method to path, with body as JSON if given, and headers."""
        sent = dict(headers or {})
        if self.cookie is not None:
            sent["Cookie"] = f"login={self.cookie}"
        answer = web.request(method, self.url + path, token=self.token, headers=sent, json_body=body)
        return Answer(answer.status, answer.json())


def must(answer: Answer, what: str) -> Answer:
    """Return answer if it succeeded.

    Args:
        answer: The server's answer.
        what: What was asked, to begin the error's message.

    Raises:
        ApiError: The answer is not a success.
    """
    if not answer.ok:
        raise ApiError(f"{what}: {answer.describe()}")
    return answer


def csrf(api: Api) -> dict[str, str]:
    """Return headers that let api's user change things past a CSRF guard.

    Some routes guard against cross-site requests, as the registry's do
    (gorilla/csrf). The headers are the token that /whoami answers with,
    and the cookie it sets, which the server checks against each other;
    and an Origin that is the server's own, which it checks too.

    Raises:
        ApiError: The server's /whoami hands out no token.
    """
    answer = web.request("GET", api.url + WHOAMI, token=api.token)
    token, cookie = answer.header("X-CSRF-Token"), answer.cookie("_gorilla_csrf")
    if answer.status != 200 or not token or not cookie:
        raise ApiError(f"GET {WHOAMI}: {answer.describe()}, with no CSRF token")
    return {"X-CSRF-Token": token, "Cookie": f"_gorilla_csrf={cookie}", "Origin": api.url}


def agree_to_aup(api: Api) -> None:
    """Have api's user agree to the server's acceptable use policy, if any.

    A user would agree on first logging in. Every server serves a
    built-in policy unless Server.AUPFile is `none` (web_ui/aup.go in
    Pelican). Until a user agrees, it refuses them its routes for users,
    groups, and much of /me (RequireAUPCompliance), its admin included.

    Raises:
        ApiError: Reading or agreeing to the policy fails.
    """
    found = api.call("GET", AUP)
    if found.status == 404:
        return
    version = must(found, f"GET {AUP}").object().get("version")
    if not version:
        raise ApiError(f"GET {AUP}: {found.describe()}, with no version")
    must(api.call("POST", MY_AUP, {"version": version}), f"agreeing to AUP {version}")


def _find_id(api: Api, route: str, kind: str, field: str, value: str) -> str | None:
    """Return the ID of the first of kind at route whose field is value.

    Raises:
        ApiError: Listing them fails.
    """
    for found in common.objects(must(api.call("GET", route), f"listing {kind}").array()):
        if found.get(field) == value:
            return str(found.get("id", ""))
    return None


def find_user(api: Api, username: str) -> str | None:
    """Return the ID of the user named username, if there is one.

    Raises:
        ApiError: Listing the users fails.
    """
    return _find_id(api, USERS, "users", "username", username)


def ensure_user(api: Api, username: str) -> str:
    """Return the ID of the user named username, created if need be.

    It is created as a user administrator.

    Raises:
        ApiError: Listing the users, or creating the user, fails.
    """
    found = find_user(api, username)
    if found:
        return found
    return must(api.call("POST", USERS, {"username": username}), f"creating user {username}").id()


def set_status(api: Api, user_id: str, status: str) -> Answer:
    """Make the user active or inactive (status) as a user administrator."""
    return api.call("PUT", f"{USERS}/{user_id}/status", {"status": status})


def delete_user(api: Api, user_id: str) -> str | None:
    """Delete the user, as a user administrator.

    The server keeps the record, marked deleted, and frees its name.

    Returns:
        Why that failed, or None if it did not.
    """
    answer = api.call("DELETE", f"{USERS}/{user_id}")
    return None if answer.ok else f"deleting user {user_id}: {answer.describe()}"


def find_group(api: Api, name: str) -> str | None:
    """Return the ID of the group named name, if api's user may see one.

    Raises:
        ApiError: Listing the groups fails.
    """
    return _find_id(api, GROUPS, "groups", "name", name)


def create_group(api: Api, name: str, eligible: bool = False) -> str:
    """Create the group named name, as its owner, and return its ID.

    Args:
        api: The web API, as the group's owner.
        name: The group's name.
        eligible: Whether to ask that it may match the issuer's
            authorization templates, which only an administrator may.

    Raises:
        ApiError: Creating the group fails.
    """
    body: dict[str, Any] = {"name": name}
    if eligible:
        body["authTemplateEligible"] = True
    return must(api.call("POST", GROUPS, body), f"creating group {name}").id()


def ensure_group(api: Api, name: str, eligible: bool = False) -> str:
    """Return the ID of the group named name, created if need be.

    See create_group().

    Raises:
        ApiError: Listing the groups, or creating the group, fails.
    """
    return find_group(api, name) or create_group(api, name, eligible)


def set_eligible(api: Api, group_id: str, eligible: bool) -> Answer:
    """Set whether the group may match the issuer's authorization templates.

    Only an administrator may.
    """
    return api.call("PATCH", f"{GROUPS}/{group_id}", {"authTemplateEligible": eligible})


def add_member(api: Api, group_id: str, user_id: str) -> None:
    """Add the user to the group, as its owner; a member already is one.

    Raises:
        ApiError: Adding the user fails.
    """
    must(
        api.call("POST", f"{GROUPS}/{group_id}/members", {"userId": user_id}),
        f"adding {user_id} to group {group_id}",
    )


def remove_member(api: Api, group_id: str, user_id: str) -> None:
    """Remove the user from the group, as its owner.

    Raises:
        ApiError: Removing the user fails.
    """
    must(
        api.call("DELETE", f"{GROUPS}/{group_id}/members/{user_id}"),
        f"removing {user_id} from group {group_id}",
    )


def delete_group(api: Api, group_id: str) -> str | None:
    """Delete the group, as its owner or an administrator.

    Returns:
        Why that failed, or None if it did not.
    """
    answer = api.call("DELETE", f"{GROUPS}/{group_id}")
    return None if answer.ok else f"deleting group {group_id}: {answer.describe()}"


# --------------------------------------------------------------------------
# Logging in. An administrator never sets a user's password: they mint
# an invite, with which the user sets their own. The login's cookie then
# stands for the user, and carries the groups they are in
# (Issuer.GroupSource: internal in framework/config.d/base/40-origin.yaml).


def password_invite(api: Api, user_id: str) -> str:
    """Return an invite with which the user may set their password.

    It is minted as a user administrator.

    Raises:
        ApiError: Minting the invite fails.
    """
    made = must(
        api.call("POST", f"{USERS}/{user_id}/password-invite", {}),
        f"minting a password invite for {user_id}",
    )
    return str(made.object().get("inviteToken", ""))


def set_password(web_url: str, invite: str, password: str) -> None:
    """Set the password of the user the invite was minted for.

    Raises:
        ApiError: The server does not set it.
    """
    answer = web.request(
        "POST", web_url + REDEEM_PASSWORD, json_body={"token": invite, "password": password}
    )
    if answer.status != 200:
        raise ApiError(f"POST {REDEEM_PASSWORD}: {answer.describe()}")


def new_password(admin: Api, user_id: str) -> str:
    """Give the user a new password of the run's, and return it.

    The admin mints an invite (password_invite()), with which the user
    sets it.

    Raises:
        ApiError: Minting the invite, or setting the password, fails.
    """
    password = secrets.token_urlsafe(12)
    set_password(admin.url, password_invite(admin, user_id), password)
    return password


def try_login(web_url: str, username: str, password: str) -> web.Response:
    """Try to log in to the server at web_url as username.

    It waits out the server's limit of a login a second.

    Returns:
        The server's answer, which sets the login cookie if it succeeded.
    """
    answer = web.Response(None)
    for _ in range(LOGIN_TRIES):
        answer = web.request("POST", web_url + LOGIN, form={"user": username, "password": password})
        if answer.status != 429:
            break
        time.sleep(LOGIN_SPACING)
    return answer


def login(web_url: str, username: str, password: str) -> str:
    """Log in to the server at web_url as username, and return the cookie.

    It waits out the server's limit of a login a second.

    Raises:
        ApiError: The server sets no login cookie.
    """
    answer = try_login(web_url, username, password)
    cookie = answer.cookie("login")
    if answer.status != 200 or not cookie:
        raise ApiError(f"POST {LOGIN} as {username}: {answer.describe()}, with no login cookie")
    return cookie


def whoami(web_url: str, cookie: str) -> dict[str, Any]:
    """Return what the server at web_url says of cookie's user."""
    return Api(web_url, None, cookie).call("GET", WHOAMI).object()
