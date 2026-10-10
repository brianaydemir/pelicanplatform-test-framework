"""A Pelican server's web API, as one of its users: JSON requests with
the user's bearer token (credentials.mint_user()); and its users and
groups.

Users and groups are records in each server's database (Pelican's
docs/user-group-design.md). A user administrator creates users (the
built-in `admin` is one), and any user creates groups and adds members
to their own. A bearer token's subject is resolved to the user record
of that name, so a user must exist before a token means anything.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

from . import common, web

USERS = "/api/v1.0/users"
GROUPS = "/api/v1.0/groups"

JSON = {"Content-Type": "application/json"}


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

    def call(self, method: str, path: str, body: Any = None) -> Answer:
        """Send method to path, with body as JSON if given."""
        upload = None if body is None else json.dumps(body).encode()
        answer = web.request(
            method,
            self.url + path,
            token=self.token,
            upload=upload,
            headers=JSON if body is not None else None,
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


def create_group(api: Api, name: str) -> str:
    """Create the group named name, as its owner: its ID."""
    made = must(api.call("POST", GROUPS, {"name": name}), f"creating group {name}")
    return str(made.object.get("id", ""))


def add_member(api: Api, group_id: str, user_id: str) -> None:
    """Add the user to the group, as its owner."""
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
