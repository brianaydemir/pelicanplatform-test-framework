"""Collections, through an origin's web API.

Collections (origin/collections.go in Pelican) are prefixes of an
exported namespace with access control of their own, an owner, an admin
group, groups and users that may read or write, and a visibility
(Pelican's docs/collections-design.md), which the origin's embedded
OAuth2 issuer turns into storage scopes. Each call is by an accounts.Api
of the origin, as one of its users.
"""

from typing import Any, Optional

from . import common
from .accounts import Answer, Api, must
from .credentials import PROTECTED

COLLECTIONS = "/api/v1.0/origin_ui/collections"


def missing(fed: common.Federation) -> Optional[str]:
    """Return why there are no collections to test, or None if there are.

    There are none with no embedded issuer, or no protected namespace.
    """
    if fed.external_issuer:
        return "the exports trust the external issuer ('-p auth-external-issuer')"
    if not fed.embedded_issuer:
        return "the origins' embedded issuer is off (ORIGIN_ENABLE_ISSUER=false)"
    if not any(ns in fed.exports for ns in PROTECTED):
        return "no protected namespace is exported"
    return None


def create(
    api: Api, name: str, namespace: str, visibility: str = "private", sharing: bool = False
) -> Answer:
    """Create a collection named name at namespace, owned by the caller.

    Args:
        api: The caller.
        name: The collection's name.
        namespace: The prefix it covers.
        visibility: `private` or `public`.
        sharing: Whether parts of it may be shared (see share()).
    """
    body: dict[str, Any] = {"name": name, "namespace": namespace, "visibility": visibility}
    body["enableSharing"] = sharing
    return api.call("POST", COLLECTIONS, body)


def get(api: Api, cid: str) -> Answer:
    """Return one collection, if the caller may see it."""
    return api.call("GET", f"{COLLECTIONS}/{cid}")


def listed(api: Api) -> set[str]:
    """Return the IDs of the collections the caller may see."""
    return {str(row.get("id")) for row in common.objects(api.call("GET", COLLECTIONS).array)}


def patch(api: Api, cid: str, **fields: Any) -> Answer:
    """Change a collection's fields, e.g. description or ownerId."""
    return api.call("PATCH", f"{COLLECTIONS}/{cid}", fields)


def delete(api: Api, cid: str) -> Answer:
    """Delete a collection."""
    return api.call("DELETE", f"{COLLECTIONS}/{cid}")


def grant(
    api: Api, cid: str, target: str, role: str, expires_at: Optional[str] = None
) -> Answer:
    """Grant role on a collection to target, until expires_at if given.

    Args:
        api: The caller.
        cid: The collection's ID.
        target: A group name, a user (`user-<username>`), or
            `@authenticated`, every authenticated user.
        role: `read` or `write`.
        expires_at: When the grant ends, in RFC 3339.
    """
    body: dict[str, Any] = {"groupId": target, "role": role}
    if expires_at:
        body["expiresAt"] = expires_at
    return api.call("POST", f"{COLLECTIONS}/{cid}/acl", body)


def revoke(api: Api, cid: str, target: str, role: str) -> Answer:
    """Revoke a grant."""
    return api.call("DELETE", f"{COLLECTIONS}/{cid}/acl", {"groupId": target, "role": role})


def grants(api: Api, cid: str) -> set[tuple[str, str]]:
    """Return a collection's ACL, as (target, role) pairs.

    Returns:
        Each grant's target (a group name, a username, or
        `@authenticated`), and its role (`read` or `write`).

    Raises:
        ApiError: The ACL could not be listed.
    """
    rows = common.objects(
        must(api.call("GET", f"{COLLECTIONS}/{cid}/acl"), "listing the ACL").array
    )
    return {
        (str(row.get("subjectName", "")).removeprefix("user-"), str(row.get("role", "")))
        for row in rows
    }


def set_metadata(api: Api, cid: str, key: str, value: str) -> Answer:
    """Set one key of a collection's metadata."""
    return api.call("PUT", f"{COLLECTIONS}/{cid}/metadata/{key}", {"value": value})


def metadata(api: Api, cid: str) -> Answer:
    """Return a collection's metadata: rows of key and value."""
    return api.call("GET", f"{COLLECTIONS}/{cid}/metadata")


def share(api: Api, cid: str, name: str, namespace: str) -> Answer:
    """Share part of a collection as a collection of the caller's.

    Args:
        api: The caller.
        cid: The collection's ID.
        name: The new collection's name.
        namespace: The part to share, within the collection's own.
    """
    return api.call(
        "POST", f"{COLLECTIONS}/{cid}/shares", {"name": name, "namespace": namespace}
    )
