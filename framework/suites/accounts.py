"""Accounts: the life cycle of the identities that the issuer origin keeps.

The issuer origin (origin-0, or `fed`) keeps them in its database
(Pelican's docs/user-group-design.md). The suite checks that life cycle,
and who may act as whom because of it: deactivating and deleting users;
granting roles by scope and by authority group; what becomes of
collection grants when users and groups are deleted, renamed, or
recreated; group invites; and the acceptable use policy (AUP). A
mistake in any of these is a privilege escalation, or a grant that
outlives its owner. The suite needs the embedded issuer and a protected
namespace, and skips with the issuer off or the external one.

Every user and group the suite makes is the run's own, named
acct-<role>-<run>, and acts through a bearer token that the origin's web
API takes as theirs (credentials.mint_user()), or through the cookie of
a login, with a password set through an invite. Tokens for data come
from the namespace's issuer through its device flow, approved as the
user, with a client that the admin creates. Collections are the admin's,
under /<namespace>/data/accounts/<run>/.
framework/config.d/base/40-origin.yaml names the suite's authorities:
the groups test-server-admins (Server.AdminGroups) and
test-collection-admins (Server.CollectionAdminGroups), which the origin
reserves as it starts, and the user test-user-admin
(Server.UserAdminUsers), whom the admin creates once and later runs
reuse. The scenarios:

  user-status       a user made inactive, then active, then deleted:
                    their login cookie, logging in, their web token,
                    and the issuer's device flow, authorization page,
                    and refresh tokens, for each
  scope-grants      the scopes the admin grants a user: server.user_admin
                    (no power over the admin, nor to grant), and
                    pelican.log_read and server.collection_admin, each
                    obeyed at once, and revoked at once, an API token of
                    the scope with it; a data scope is refused
  authority-groups  the configured groups' names are reserved; their
                    members are collection administrators and server
                    administrators, from their next login; the
                    configured user administrator manages users and
                    grants nothing
  id-keyed-grants   a deleted group's grants go with it, and a deleted
                    user's collection stays out of reach of a new user
                    of the same name; a renamed group and user keep
                    their grants, and the names they leave grant
                    nothing
  group-invites     a group's invite links: one redeemed once, if
                    single-use, and by many, if not; only the owner
                    makes them
  aup               a new AUP gates the admin until they agree to it,
                    and the policy is public; it is restored afterward,
                    which restores every earlier agreement

`aup` comes last: for a moment, it gates every user of the origin,
the admin included. It puts back the policy it found, whose version
is the hash of its text, so every user who had agreed to it has again,
and the admin agrees again. Should that fail, every suite that uses
the routes it gates agrees to the AUP again as it starts.

The run's collections, groups, and users are removed afterward, and
those that earlier runs left. The server only marks a deleted user or
group deleted, and frees its name.
"""

import functools
import json
import secrets
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from testlib import accounts, collections, common, credentials, issuer, web, webapi
from testlib.accounts import Api, ApiError, must
from testlib.collections import create, delete, get, grant, grants, listed, patch
from testlib.common import die, first, objects, warn
from testlib.credentials import PROTECTED
from testlib.issuer import IssuerError, Tokens, User
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session

SCENARIOS = {
    "user-status": "an inactive or deleted user's cookie, login, token, and issuer flows",
    "scope-grants": "scopes the admin grants a user work at once, revoke at once, and no more",
    "authority-groups": "the configured groups' names are reserved; their members' authority",
    "id-keyed-grants": "grants go with a deleted group or user, and stay with a renamed one",
    "group-invites": "a group's invite links: single-use, multi-use, and the owner's alone",
    "aup": "a new AUP gates the admin until they agree; the policy is public",
}
# Every scenario: each presents only Pelican's tokens, its web API's and
# its issuer's (see suites/__init__.py).
ONE_PROFILE = tuple(SCENARIOS)

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# How the run's users and groups are named: acct-<role>-<run>.
PREFIX = "acct-"
# The authorities that framework/config.d/base/40-origin.yaml configures.
SERVER_ADMINS = "test-server-admins"
COLLECTION_ADMINS = "test-collection-admins"
USER_ADMIN = "test-user-admin"

LOG_TAIL = "/api/v1.0/logs/tail?limit=1"
CONFIG = "/api/v1.0/config"

# What a user asks the device flow for in `user-status`: what any user
# is granted, and a refresh token.
REFRESHABLE = ("openid", "offline_access")


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None if there is something.

    That is so with no embedded issuer, or no protected namespace.
    """
    return collections.missing(fed)


def outcome(problems: Sequence[str], note: str) -> Row:
    """Return a row with no case.

    Returns:
        FAIL with the first of problems, or PASS with note.
    """
    return ("", FAIL, first(problems)) if problems else ("", PASS, note)


class Test:
    """The issuer origin and its namespace, and the run's users.

    It keeps tokens and clients for them.

    Attributes:
        web: The issuer origin's web URL.
        namespace: Its namespace: the first protected one exported.
        base: The run's path in the namespace: data/accounts/<run>.
        prefix: The run's path in the federation: /<namespace>/<base>.
        passwords: The passwords set, by username.
        clients: The admin's clients (see client()), by kind.
    """

    def __init__(self, session: Session):
        fed = session.fed
        self.session = session
        self.fed = fed
        self.run = session.run
        self.web = fed.origin_web_url
        self.namespace = next(ns for ns in PROTECTED if ns in fed.exports)
        self.base = f"data/accounts/{self.run}"
        self.prefix = f"/{self.namespace}/{self.base}"
        self.passwords: dict[str, str] = {}
        self.clients: dict[str, issuer.Client] = {}
        self._web_tokens: dict[str, str] = {}

    def name(self, role: str) -> str:
        """Return the run's name for role: acct-<role>-<run>."""
        return f"{PREFIX}{role}-{self.run}"

    def web_token(self, username: str) -> str:
        """Return a token that the origin's web API takes as username's.

        That is whoever is named so when it is presented.
        """
        if username not in self._web_tokens:
            self._web_tokens[username] = credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/accounts-web.{username}"),
                credentials.local_issuer(self.fed, self.web),
                self.fed.origin_key,
                username,
                lifetime=3600,
            )
        return self._web_tokens[username]

    def api(self, username: str) -> Api:
        """Return the origin's web API, as username, by bearer token."""
        return Api(self.web, self.web_token(username))

    def client(self, kind: str) -> issuer.Client:
        """Return the admin's client of the namespace's issuer.

        Args:
            kind: `device`, for the device flow, or `code`, for the
                authorization code flow; each with refresh tokens.
        """
        if kind not in self.clients:
            code = kind == "code"
            self.clients[kind] = issuer.create_client(
                self.web,
                f"/{self.namespace}",
                self.web_token("admin"),
                (issuer.CODE_GRANT if code else issuer.DEVICE_GRANT, issuer.REFRESH_GRANT),
                redirect_uris=(issuer.REDIRECT_URI,) if code else (),
                name=f"pelican-test-framework accounts {kind} {self.run}",
            )
        return self.clients[kind]

    def data_scopes(self, username: str) -> frozenset[str]:
        """Return the data scopes the namespace's issuer grants username.

        It asks now, for everything they might have.
        """
        tok = issuer.token(
            self.client("device"), User(token=self.web_token(username)), issuer.DATA_SCOPES
        )
        return issuer.data_scopes(tok)

    def path(self, name: str) -> str:
        """Return the namespace of the run's collection name.

        It is the collection's federation path.
        """
        return f"{self.prefix}/{name}"

    def read_scope(self, name: str) -> str:
        """Return the scope that reads the run's collection name.

        It is as the namespace's issuer mints it.
        """
        return f"storage.read:/{self.base}/{name}"


# --------------------------------------------------------------------------
# Users, logins, and collections.


def make_user(test: Test, username: str) -> str:
    """Have the admin create the user username, who agrees to the AUP.

    Returns:
        Their ID.
    """
    made = must(
        test.api("admin").call("POST", accounts.USERS, {"username": username}),
        f"creating user {username}",
    )
    accounts.agree_to_aup(test.api(username))
    return str(made.object.get("id", ""))


def log_in(test: Test, username: str, user_id: str) -> str:
    """Log username in.

    The first time, the admin mints them a password invite, and they set
    a password of the run's with it.

    Returns:
        The login cookie, which carries the groups they are in now.
    """
    if username not in test.passwords:
        invite = accounts.password_invite(test.api("admin"), user_id)
        password = secrets.token_urlsafe(12)
        accounts.set_password(test.web, invite, password)
        test.passwords[username] = password
    return accounts.login(test.web, username, test.passwords[username])


def make_collection(test: Test, name: str) -> str:
    """Have the admin create the run's collection name.

    Returns:
        Its ID.
    """
    made = must(create(test.api("admin"), name, test.path(name)), f"creating collection {name}")
    return str(made.object.get("id", ""))


def scope_route(user_id: str, scope: str = "") -> str:
    """Return the route of a user's scope grants, or of one of them."""
    return f"{accounts.USERS}/{user_id}/scopes" + (f"/{scope}" if scope else "")


def whoami(test: Test, cookie: str) -> dict[str, Any]:
    """Return what /whoami says of the user whose login cookie this is."""
    return Api(test.web, None, cookie).call("GET", accounts.WHOAMI).object


def sweep(test: Test, this_run: bool) -> None:
    """Remove the suite's collections, groups, and users.

    Args:
        test: The suite's run.
        this_run: Whether to remove the run's, rather than earlier runs'
            leftovers.
    """
    admin = test.api("admin")
    directory = f"/{test.namespace}/data/accounts/"
    for row in objects(admin.call("GET", collections.COLLECTIONS).array):
        namespace = str(row.get("namespace", ""))
        if (
            namespace.startswith(directory)
            and namespace.startswith(test.prefix + "/") == this_run
        ):
            gone = delete(admin, str(row.get("id")))
            if not gone.ok:
                warn(f"could not remove collection {row.get('name')}: {gone.describe()}")

    def ours(row: dict[str, Any], key: str) -> bool:
        name = str(row.get(key, ""))
        return name.startswith(PREFIX) and name.endswith(f"-{test.run}") == this_run

    for row in objects(admin.call("GET", accounts.GROUPS).array):
        if ours(row, "name"):
            failed = accounts.delete_group(admin, str(row.get("id")))
            if failed:
                warn(f"could not remove group {row.get('name')}: {failed}")
    for row in objects(admin.call("GET", accounts.USERS).array):
        if ours(row, "username"):
            failed = accounts.delete_user(admin, str(row.get("id")))
            if failed:
                warn(f"could not remove user {row.get('username')}: {failed}")


def prepare(test: Test) -> None:
    """Have the admin agree to the AUP; remove what earlier runs left."""
    accounts.agree_to_aup(test.api("admin"))
    sweep(test, this_run=False)


def finish(test: Test) -> None:
    """Remove the run's collections, groups, users, and clients.

    The admin agrees to the AUP first, in case `aup` left one they have
    not.
    """
    try:
        accounts.agree_to_aup(test.api("admin"))
        sweep(test, this_run=True)
    except ApiError as e:
        warn(f"could not remove the run's users and groups: {e}")
    for kind, made in test.clients.items():
        failed = issuer.delete_client(
            test.web, f"/{test.namespace}", test.web_token("admin"), made.client_id
        )
        if failed:
            warn(f"could not remove the {kind} client: {failed}")


def checked(rows: list[Row], case: str, check: Callable[[], Row]) -> None:
    """Append check()'s row to rows, as case.

    It is a failing one if check() raised ApiError or IssuerError.
    """
    try:
        _, status, note = check()
    except (ApiError, IssuerError) as e:
        status, note = FAIL, str(e)
    rows.append((case, status, note))


# --------------------------------------------------------------------------
# user-status


def cleared(answer: web.Response) -> bool:
    """Return whether answer clears the login cookie: sets it empty."""
    return any(
        key.lower() == "set-cookie" and value.split(";", 1)[0].strip() == "login="
        for key, value in answer.headers
    )


def web_problems(test: Test, username: str, cookie: str, allowed: bool) -> list[str]:
    """Return what is wrong with the user's login cookie, login, and token.

    Each of the cookie at /me, logging in again, and their web token at
    /me should work if allowed, and otherwise be refused (401), with the
    cookie cleared.
    """
    problems: list[str] = []
    want = "200" if allowed else "401"
    answer = web.request("GET", test.web + accounts.ME, headers={"Cookie": f"login={cookie}"})
    if answer.status != (200 if allowed else 401):
        problems.append(
            f"GET {accounts.ME} with the login cookie: {answer.describe()}, not {want}"
        )
    elif not allowed and not cleared(answer):
        problems.append(
            f"GET {accounts.ME} with the login cookie: 401, but the cookie not cleared"
        )
    answer = accounts.try_login(test.web, username, test.passwords[username])
    if answer.status != (200 if allowed else 401):
        problems.append(f"logging in again: {answer.describe()}, not {want}")
    found = test.api(username).call("GET", accounts.ME)
    if found.status != (200 if allowed else 401):
        problems.append(f"GET {accounts.ME} with a web token: {found.describe()}, not {want}")
    return problems


def approves(client: issuer.Client, user: User) -> tuple[bool, str]:
    """Return whether the issuer approves a device flow for client as user.

    Returns:
        Whether it does, and what it answered.
    """
    flow = issuer.started(issuer.start_device(client, REFRESHABLE), client)
    try:
        done = issuer.decide(client.url, str(flow["user_code"]), user, "approve")
    except IssuerError as e:
        return False, str(e)
    return (
        done.status == 200 and issuer.decoded(done).get("status") == "approved",
        done.describe(),
    )


def authorizes(client: issuer.Client, user: User) -> tuple[bool, str]:
    """Return whether the issuer's authorization page sends client a code.

    The page is shown user.

    Returns:
        Whether it sends client back a code, and what it answered.
    """
    _, challenge = issuer.pkce()
    sent = issuer.authorize(client, user, REFRESHABLE, challenge, secrets.token_urlsafe(12))
    return bool(issuer.redirected(sent).get("code")), sent.describe()


def issuer_refusals(test: Test, user: User, refresh: str) -> list[str]:
    """Return what the issuer does for a user it should refuse.

    It may approve a device flow, send a code from its authorization page,
    or honor the user's refresh token (it should say invalid_grant).

    Args:
        test: The suite's run.
        user: How the issuer knows the user.
        refresh: The user's unused refresh token, or "" if they hold none.
    """
    problems: list[str] = []
    if approves(test.client("device"), user)[0]:
        problems.append("the device flow was approved with the login cookie")
    if authorizes(test.client("code"), user)[0]:
        problems.append("the authorization page sent back a code for the login cookie")
    if not refresh:
        return problems
    answer = issuer.refresh(test.client("device"), refresh)
    if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
        error = issuer.error_of(answer) or "no error"
        problems.append(f"the refresh token: {answer.describe()} ({error}), not invalid_grant")
    return problems


def user_status(test: Test) -> list[Row]:
    """Check that a user is refused while inactive or deleted.

    A user, logged in and holding a refresh token, is made inactive,
    active again, and then deleted: their login cookie, logging in, and
    their web token must be refused (401) while inactive or deleted, and
    the issuer must approve no device flow, send no code, and honor no
    refresh token for them.
    """
    username = test.name("status")
    admin = test.api("admin")
    user_id = make_user(test, username)
    cookie = log_in(test, username, user_id)
    user = User(cookie=cookie)
    held = issuer.device_tokens(test.client("device"), user, REFRESHABLE)
    if not held.refresh:
        return [
            ("", FAIL, "the device flow, asked for offline_access, issued no refresh token")
        ]
    refresh = held.refresh
    rows: list[Row] = []

    def active() -> Row:
        problems = web_problems(test, username, cookie, allowed=True)
        if not authorizes(test.client("code"), user)[0]:
            problems.append("the authorization page sent back no code for the login cookie")
        return outcome(problems, "the cookie, a login, the token, and the issuer all work")

    def inactive_web() -> Row:
        must(accounts.set_status(admin, user_id, "inactive"), "making the user inactive")
        problems = web_problems(test, username, cookie, allowed=False)
        return outcome(problems, "the cookie cleared, a login, and the token: each 401")

    def refused_issuer() -> Row:
        nonlocal refresh
        problems = issuer_refusals(test, user, refresh)
        # Honored or not, it may be spent.
        refresh = ""
        return outcome(problems, "no device flow, no code, and invalid_grant")

    def reactivated() -> Row:
        nonlocal refresh
        must(accounts.set_status(admin, user_id, "active"), "making the user active again")
        problems = web_problems(test, username, cookie, allowed=True)
        renewed: Optional[Tokens] = None
        try:
            renewed = issuer.device_tokens(test.client("device"), user, REFRESHABLE)
        except IssuerError as e:
            problems.append(f"the device flow with the login cookie: {e}")
        if not authorizes(test.client("code"), user)[0]:
            problems.append("the authorization page sent back no code for the login cookie")
        if renewed and renewed.refresh:
            answer = issuer.refresh(test.client("device"), renewed.refresh)
            if answer.status == 200:
                refresh = issuer.tokens_of(answer, "refreshing").refresh or ""
            else:
                problems.append(f"refreshing a new refresh token: {answer.describe()}")
        if not refresh:
            problems.append("no unused refresh token to try once the user is deleted")
        return outcome(problems, "all work again")

    def deleted_web() -> Row:
        failed = accounts.delete_user(admin, user_id)
        if failed:
            raise ApiError(failed)
        problems = web_problems(test, username, cookie, allowed=False)
        return outcome(problems, "the cookie cleared, a login, and the token: each 401")

    checked(rows, "active", active)
    checked(rows, "inactive-web", inactive_web)
    checked(rows, "inactive-issuer", refused_issuer)
    checked(rows, "reactivated", reactivated)
    checked(rows, "deleted-web", deleted_web)
    checked(rows, "deleted-issuer", refused_issuer)
    return rows


# --------------------------------------------------------------------------
# scope-grants


def scope_grants(test: Test) -> list[Row]:
    """Check that scopes the admin grants a user work at once, and no more.

    The admin grants a user scopes (POST /users/:id/scopes), which the
    server consults on each request: server.user_admin, pelican.log_read,
    and server.collection_admin. An API token of server.collection_admin,
    which the user makes while granted it, goes with the grant. A data
    scope is refused (400).
    """
    admin = test.api("admin")
    rows: list[Row] = []

    def granted(user_id: str, scope: str) -> None:
        answer = admin.call("POST", scope_route(user_id), {"scope": scope})
        if answer.status != 204:
            raise ApiError(f"granting {scope}: {answer.describe()}, not 204")

    def revoked(user_id: str, scope: str) -> None:
        answer = admin.call("DELETE", scope_route(user_id, scope))
        if answer.status != 204:
            raise ApiError(f"revoking {scope}: {answer.describe()}, not 204")

    def user_admin() -> Row:
        username = test.name("useradmin")
        user_id = make_user(test, username)
        api = test.api(username)
        made = {"username": test.name("made")}
        problems: list[str] = []
        answer = api.call("POST", accounts.USERS, made)
        if answer.status != 403:
            problems.append(f"creating a user before the grant: {answer.describe()}, not 403")
        granted(user_id, "server.user_admin")
        answer = api.call("POST", accounts.USERS, made)
        if answer.status != 201:
            problems.append(f"creating a user: {answer.describe()}, not 201")
        admin_id = accounts.find_user(admin, "admin") or ""
        label = must(admin.call("GET", f"{accounts.USERS}/{admin_id}"), "reading the admin")
        # Each would change nothing, if it got through.
        refused = (
            ("making the admin active", accounts.set_status(api, admin_id, "active")),
            (
                "relabeling the admin",
                api.call(
                    "PATCH",
                    f"{accounts.USERS}/{admin_id}",
                    {"displayName": str(label.object.get("displayName", ""))},
                ),
            ),
            (
                "granting itself server.admin",
                api.call("POST", scope_route(user_id), {"scope": "server.admin"}),
            ),
            (
                "granting the admin pelican.log_read",
                api.call("POST", scope_route(admin_id), {"scope": "pelican.log_read"}),
            ),
        )
        for what, answer in refused:
            if answer.status != 403:
                problems.append(f"{what}: {answer.describe()}, not 403")
        return outcome(problems, "creates users; may not touch the admin, nor grant")

    def log_read() -> Row:
        username = test.name("logreader")
        user_id = make_user(test, username)
        api = test.api(username)
        problems: list[str] = []
        answer = api.call("GET", LOG_TAIL)
        if answer.status != 403:
            problems.append(f"GET {LOG_TAIL} before the grant: {answer.describe()}, not 403")
        granted(user_id, "pelican.log_read")
        answer = api.call("GET", LOG_TAIL)
        if answer.status != 200:
            problems.append(f"GET {LOG_TAIL}: {answer.describe()}, not 200")
        answer = api.call("GET", CONFIG)
        if answer.status != 403:
            problems.append(f"GET {CONFIG}: {answer.describe()}, not 403")
        revoked(user_id, "pelican.log_read")
        answer = api.call("GET", LOG_TAIL)
        if answer.status != 403:
            problems.append(f"GET {LOG_TAIL} once revoked: {answer.describe()}, not 403")
        return outcome(problems, "the log tail, not the configuration; refused once revoked")

    def collection_admin() -> Row:
        username = test.name("curator")
        user_id = make_user(test, username)
        api = test.api(username)
        problems: list[str] = []
        answer = create(api, "by-scope", test.path("by-scope"))
        if answer.status != 403:
            problems.append(f"creating a collection before the grant: {answer.describe()}")
        granted(user_id, "server.collection_admin")
        answer = create(api, "by-scope", test.path("by-scope"))
        if answer.status != 201:
            problems.append(f"creating a collection: {answer.describe()}, not 201")
        else:
            must(delete(admin, str(answer.object.get("id"))), "deleting the collection")
        revoked(user_id, "server.collection_admin")
        answer = create(api, "by-scope", test.path("by-scope"))
        if answer.status != 403:
            problems.append(f"creating a collection once revoked: {answer.describe()}, not 403")
        return outcome(problems, "creates a collection; refused once revoked")

    def collection_admin_token() -> Row:
        """Check who may make an API token of server.collection_admin.

        A user granted server.collection_admin asks for an API token of
        that scope from their login cookie (POST /tokens), and must be
        refused (403): the route is the admin's alone (AdminAuthHandler
        in web_ui/ui.go), on v26.0.0-rc.3 and main alike, though
        api_token/api_token.go would limit any creator's token to the
        scopes the creator holds (intersectWithUserScopes). The admin,
        logged in, makes one, which creates a collection, and which is
        refused once deleted. The admin holds the scope by implication
        (server.admin implies server.collection_admin), so no revoked
        grant can be tried.
        """
        username = test.name("tokencurator")
        user_id = make_user(test, username)
        cookie = log_in(test, username, user_id)
        granted(user_id, "server.collection_admin")
        body = {
            "name": test.name("token"),
            "scopes": ["server.collection_admin"],
            "expiration": common.timestamp(datetime.now(timezone.utc) + timedelta(hours=1)),
        }

        def mint(login: str) -> web.Response:
            return web.request(
                "POST",
                test.web + webapi.TOKENS,
                upload=json.dumps(body).encode(),
                headers={**accounts.JSON, **webapi.cookie_header(login=login)},
            )

        def drop(login: str, token_id: str) -> None:
            dropped = web.request(
                "DELETE",
                f"{test.web}{webapi.TOKENS}/{token_id}",
                headers=webapi.cookie_header(login=login),
            )
            if not dropped.ok:
                warn(f"deleting API token {token_id}: {dropped.describe()}")

        problems: list[str] = []
        minted = mint(cookie)
        revoked(user_id, "server.collection_admin")
        if minted.status != 403:
            problems.append(f"a user's request for an API token: {minted.describe()}, not 403")
            if (made := webapi.made_token(minted)) is not None:
                drop(cookie, made[1])
        admin_cookie = accounts.login(test.web, "admin", webapi.ADMIN_PASSWORD)
        minted = mint(admin_cookie)
        made = webapi.made_token(minted)
        if minted.status not in (200, 201) or made is None:
            problems.append(f"the admin's request for one: {minted.describe()}, with no token")
            return outcome(problems, "")
        token, token_id, _ = made
        api = Api(test.web, token)
        try:
            answer = create(api, "by-token", test.path("by-token"))
            if answer.status != 201:
                problems.append(
                    f"creating a collection with the admin's token: {answer.describe()}"
                )
            else:
                must(delete(admin, str(answer.object.get("id"))), "deleting the collection")
        finally:
            drop(admin_cookie, token_id)
        answer = create(api, "by-token", test.path("by-token"))
        if answer.status not in (401, 403):
            problems.append(
                f"creating a collection with the token once deleted: {answer.describe()}"
            )
            if answer.status == 201:
                must(delete(admin, str(answer.object.get("id"))), "deleting the collection")
        return outcome(
            problems,
            "a user is refused one; the admin's creates a collection, and is refused once deleted",
        )

    def ungrantable() -> Row:
        user_id = make_user(test, test.name("ungranted"))
        answer = admin.call("POST", scope_route(user_id), {"scope": "storage.read:/"})
        if answer.status != 400:
            return ("", FAIL, f"granting storage.read:/: {answer.describe()}, not 400")
        held = {
            str(row.get("scope"))
            for row in objects(must(admin.call("GET", scope_route(user_id)), "listing").array)
        }
        if "storage.read:/" in held:
            return ("", FAIL, "storage.read:/ is listed among the user's scopes")
        return ("", PASS, "storage.read:/ refused (400)")

    checked(rows, "user-admin", user_admin)
    checked(rows, "log-read", log_read)
    checked(rows, "collection-admin", collection_admin)
    checked(rows, "collection-admin-token", collection_admin_token)
    checked(rows, "ungrantable", ungrantable)
    return rows


# --------------------------------------------------------------------------
# authority-groups


def authority_groups(test: Test) -> list[Row]:
    """Check that the configured authority groups and user admin work.

    The groups are Server.AdminGroups and Server.CollectionAdminGroups,
    which the origin reserved as it started: nobody may create or delete
    one (409); a member is a collection administrator, or a server
    administrator, once they log in again, and not once removed and
    logged in again; nor once removed, with the login cookie from before
    (a cookie keeps the groups it was minted with, so this fails). The
    configured user administrator (Server.UserAdminUsers) manages users,
    and grants nothing.
    """
    admin = test.api("admin")
    reserved: dict[str, str] = {}
    for name in (SERVER_ADMINS, COLLECTION_ADMINS):
        found = accounts.find_group(admin, name)
        if not found:
            why = (
                f"there is no group {name}, which the origin reserves as it starts, from "
                "framework/config.d/base/40-origin.yaml: restart it"
            )
            return [("", FAIL, why)]
        reserved[name] = found
    rows: list[Row] = []

    def names() -> Row:
        username = test.name("squatter")
        make_user(test, username)
        problems: list[str] = []
        for name, gid in reserved.items():
            answer = test.api(username).call("POST", accounts.GROUPS, {"name": name})
            if answer.status != 409:
                problems.append(f"a user's creating {name}: {answer.describe()}, not 409")
            if answer.ok:
                accounts.delete_group(admin, str(answer.object.get("id")))
            answer = admin.call("DELETE", f"{accounts.GROUPS}/{gid}")
            if answer.status != 409:
                problems.append(f"the admin's deleting {name}: {answer.describe()}, not 409")
        return outcome(problems, "creating and deleting each: 409")

    def collection_admins() -> Row:
        username = test.name("member-curator")
        user_id = make_user(test, username)
        gid = reserved[COLLECTION_ADMINS]
        problems: list[str] = []

        def creates(when: str, status: int) -> None:
            cookie = log_in(test, username, user_id)
            answer = create(Api(test.web, None, cookie), "by-group", test.path("by-group"))
            if answer.status != status:
                problems.append(
                    f"creating a collection {when}: {answer.describe()}, not {status}"
                )
            if answer.ok:
                must(delete(admin, str(answer.object.get("id"))), "deleting the collection")

        creates("before joining", 403)
        accounts.add_member(admin, gid, user_id)
        try:
            creates(f"in {COLLECTION_ADMINS}, logged in again", 201)
        finally:
            accounts.remove_member(admin, gid, user_id)
        creates("once removed, logged in again", 403)
        return outcome(problems, "a member creates a collection; refused once removed")

    def server_admins() -> Row:
        username = test.name("member-admin")
        user_id = make_user(test, username)
        gid = reserved[SERVER_ADMINS]
        problems: list[str] = []

        def role(when: str, want: str) -> None:
            found = whoami(test, log_in(test, username, user_id)).get("role")
            if found != want:
                problems.append(f"/whoami's role {when}: {found!r}, not {want!r}")

        role("before joining", "user")
        accounts.add_member(admin, gid, user_id)
        try:
            role(f"in {SERVER_ADMINS}, logged in again", "admin")
        finally:
            accounts.remove_member(admin, gid, user_id)
        role("once removed, logged in again", "user")
        return outcome(problems, "a member is admin; not once removed")

    def stale_cookie() -> Row:
        username = test.name("member-stale")
        user_id = make_user(test, username)
        gid = reserved[SERVER_ADMINS]
        accounts.add_member(admin, gid, user_id)
        try:
            cookie = log_in(test, username, user_id)
            found = whoami(test, cookie).get("role")
            if found != "admin":
                return ("", FAIL, f"/whoami's role in {SERVER_ADMINS}: {found!r}, not 'admin'")
        finally:
            accounts.remove_member(admin, gid, user_id)
        found = whoami(test, cookie).get("role")
        if found != "user":
            return ("", FAIL, f"once removed, the old login cookie's role is still {found!r}")
        return ("", PASS, "once removed, the old login cookie is a user's")

    def user_admin_user() -> Row:
        user_id = accounts.ensure_user(admin, USER_ADMIN)
        api = test.api(USER_ADMIN)
        accounts.agree_to_aup(api)
        problems: list[str] = []
        answer = api.call("POST", accounts.USERS, {"username": test.name("by-config")})
        if answer.status != 201:
            problems.append(f"{USER_ADMIN}'s creating a user: {answer.describe()}, not 201")
        answer = api.call("POST", scope_route(user_id), {"scope": "server.admin"})
        if answer.status != 403:
            problems.append(f"{USER_ADMIN}'s granting itself server.admin: {answer.describe()}")
        said = api.call("GET", accounts.WHOAMI).object
        if said.get("role") != "user":
            problems.append(f"{USER_ADMIN}'s /whoami role: {said.get('role')!r}, not 'user'")
        if "server.user_admin" not in (common.as_array(said.get("scopes")) or []):
            problems.append(f"{USER_ADMIN}'s /whoami scopes lack server.user_admin")
        return outcome(problems, f"{USER_ADMIN} creates users, and may not grant")

    checked(rows, "reserved", names)
    checked(rows, "collection-admins", collection_admins)
    checked(rows, "server-admins", server_admins)
    checked(rows, "stale-cookie", stale_cookie)
    checked(rows, "user-admin", user_admin_user)
    return rows


# --------------------------------------------------------------------------
# id-keyed-grants


def id_keyed_grants(test: Test) -> list[Row]:
    """Check that grants are kept by the IDs of users and groups, not names.

    See Pelican issues #3752 and #3753. A deleted group's go with it, and
    a new group of its name gets none; a new user of a deleted user's name
    gets none of theirs; and a renamed group and user keep theirs, while
    the names they leave grant nothing.
    """
    admin = test.api("admin")
    rows: list[Row] = []

    def group_deleted() -> Row:
        member = test.name("member")
        member_id = make_user(test, member)
        name = test.name("doomed")
        gid = accounts.create_group(admin, name)
        accounts.add_member(admin, gid, member_id)
        cid = make_collection(test, "x")
        read = test.read_scope("x")
        must(grant(admin, cid, name, "read"), "granting the group read")
        must(patch(admin, cid, adminId=gid), "making the group the collection's admin group")
        if read not in test.data_scopes(member):
            return ("", FAIL, f"the member's token lacks {read} before the group is deleted")
        failed = accounts.delete_group(admin, gid)
        if failed:
            raise ApiError(failed)
        problems: list[str] = []
        if grants(admin, cid):
            problems.append(f"the deleted group's grant is still listed: {grants(admin, cid)}")
        if get(admin, cid).object.get("adminId"):
            problems.append("the collection still names the deleted group its admin group")
        if read in test.data_scopes(member):
            problems.append(f"the member's next token still has {read}")
        new_gid = accounts.create_group(admin, name)
        accounts.add_member(admin, new_gid, member_id)
        if grants(admin, cid):
            problems.append(f"with a new group of the name, the ACL lists {grants(admin, cid)}")
        if get(admin, cid).object.get("adminId") == new_gid:
            problems.append("the new group of the name is the collection's admin group")
        if read in test.data_scopes(member):
            problems.append(f"a member of the new group of the name gets {read}")
        return outcome(
            problems, "its grant and admin role gone; a new group of its name, nothing"
        )

    def user_deleted() -> Row:
        username = test.name("former")
        old_id = make_user(test, username)
        cid = make_collection(test, "y")
        read = test.read_scope("y")
        must(patch(admin, cid, ownerId=old_id), "handing the collection to the user")
        must(grant(admin, cid, f"user-{username}", "read"), "granting the user read")
        api = test.api(username)
        if cid not in listed(api) or read not in test.data_scopes(username):
            return ("", FAIL, f"the owner does not see the collection, or lacks {read}")
        failed = accounts.delete_user(admin, old_id)
        if failed:
            raise ApiError(failed)
        new_id = make_user(test, username)
        problems: list[str] = []
        if new_id == old_id:
            problems.append("the new user of the name has the deleted user's ID")
        if (username, "read") in grants(admin, cid):
            problems.append("the deleted user's grant is still listed")
        if cid in listed(api):
            problems.append("the new user of the name lists the deleted user's collection")
        answer = get(api, cid)
        if answer.status != 404:
            problems.append(f"the new user's GET of it: {answer.describe()}, not 404")
        answer = delete(api, cid)
        if answer.status != 404:
            problems.append(f"the new user's DELETE of it: {answer.describe()}, not 404")
        elif get(admin, cid).status != 200:
            problems.append("the collection is gone after the new user's refused DELETE")
        if read in test.data_scopes(username):
            problems.append(f"the new user of the name gets {read}")
        return outcome(problems, "a new user of the name may not see, delete, or read it")

    def renamed() -> Row:
        holder, newcomer = test.name("holder"), test.name("newcomer")
        holder_id = make_user(test, holder)
        newcomer_id = make_user(test, newcomer)
        old_user, new_user = test.name("renamee"), test.name("renamed")
        user_id = make_user(test, old_user)
        old_group, new_group = test.name("held"), test.name("held2")
        gid = accounts.create_group(admin, old_group)
        accounts.add_member(admin, gid, holder_id)
        cid = make_collection(test, "z")
        read = test.read_scope("z")
        must(grant(admin, cid, old_group, "read"), "granting the group read")
        must(grant(admin, cid, f"user-{old_user}", "read"), "granting the user read")
        if read not in test.data_scopes(holder) or read not in test.data_scopes(old_user):
            return ("", FAIL, f"the group's member or the user lacks {read} before the renames")
        must(
            admin.call("PATCH", f"{accounts.GROUPS}/{gid}", {"name": new_group}),
            "renaming the group",
        )
        must(
            admin.call("PATCH", f"{accounts.USERS}/{user_id}", {"username": new_user}),
            "renaming the user",
        )
        problems: list[str] = []
        wanted = {(new_group, "read"), (new_user, "read")}
        if grants(admin, cid) != wanted:
            problems.append(f"after the renames, the ACL lists {sorted(grants(admin, cid))}")
        if read not in test.data_scopes(holder):
            problems.append(f"the renamed group's member lost {read}")
        if read not in test.data_scopes(new_user):
            problems.append(f"the renamed user lost {read}")
        squatter_gid = accounts.create_group(admin, old_group)
        accounts.add_member(admin, squatter_gid, newcomer_id)
        make_user(test, old_user)
        if read in test.data_scopes(newcomer):
            problems.append(f"a member of a new group of the group's old name gets {read}")
        if read in test.data_scopes(old_user):
            problems.append(f"a new user of the user's old name gets {read}")
        if grants(admin, cid) != wanted:
            problems.append(
                f"with the old names taken, the ACL lists {sorted(grants(admin, cid))}"
            )
        return outcome(problems, "the renamed keep their grant; their old names, nothing")

    checked(rows, "group-deleted", group_deleted)
    checked(rows, "user-deleted", user_deleted)
    checked(rows, "renamed", renamed)
    return rows


# --------------------------------------------------------------------------
# group-invites


def group_invites(test: Test) -> list[Row]:
    """Check that a group's invite links work, and are the owner's alone.

    A group's owner makes invite links: a single-use one joins the first
    user who redeems it, and no other; a multi-use one joins each. A
    member who does not own the group may make none.
    """
    owner = test.name("inviter")
    make_user(test, owner)
    joiners = {role: test.name(f"joiner-{role}") for role in ("a", "b", "c")}
    ids = {role: make_user(test, name) for role, name in joiners.items()}
    gid = accounts.create_group(test.api(owner), test.name("invited"))
    rows: list[Row] = []

    def invite(single: bool) -> str:
        made = must(
            test.api(owner).call(
                "POST", f"{accounts.GROUPS}/{gid}/invites", {"isSingleUse": single}
            ),
            "making an invite link",
        )
        token = str(made.object.get("inviteToken", ""))
        if made.status != 201 or not token:
            raise ApiError(f"making an invite link: {made.describe()}, with no token")
        return token

    def redeem(role: str, token: str) -> accounts.Answer:
        return test.api(joiners[role]).call("POST", accounts.REDEEM_GROUP, {"token": token})

    def member(role: str) -> bool:
        mine = test.api(joiners[role]).call("GET", accounts.MY_GROUPS).array
        return gid in {str(row.get("id")) for row in objects(mine)}

    def single_use() -> Row:
        token = invite(single=True)
        problems: list[str] = []
        answer = redeem("a", token)
        if not answer.ok or not member("a"):
            problems.append(f"the first redemption: {answer.describe()}, or not a member")
        answer = redeem("b", token)
        if answer.ok or member("b"):
            problems.append(f"the second redemption: {answer.describe()}, not refused")
        answer = test.api(joiners["a"]).call(
            "POST", f"{accounts.GROUPS}/{gid}/invites", {"isSingleUse": False}
        )
        if answer.status != 403:
            problems.append(f"a member's making an invite link: {answer.describe()}, not 403")
        return outcome(problems, "the first joins; the second refused; only the owner invites")

    def multi_use() -> Row:
        token = invite(single=False)
        problems: list[str] = []
        for role in ("b", "c"):
            answer = redeem(role, token)
            if not answer.ok or not member(role):
                problems.append(f"{joiners[role]}'s redemption: {answer.describe()}")
        return outcome(problems, f"{len(ids) - 1} joined by one link")

    checked(rows, "single-use", single_use)
    checked(rows, "multi-use", multi_use)
    return rows


# --------------------------------------------------------------------------
# aup


def aup(test: Test) -> list[Row]:
    """Check that a new AUP gates the admin until they agree to it.

    The admin puts up a new AUP (PUT /aup): until they agree to it, the
    server refuses them its user routes (403, with requires_aup and the
    new version), and /whoami says so; agreeing to an older version does
    not do; the policy is public. Another new one gates them again. The
    policy found is put back afterward, and with it the version every
    user had agreed to.
    """
    admin = test.api("admin")
    found = admin.call("GET", accounts.AUP)
    if found.status == 404:
        return [("", SKIP, "the origin has no AUP (Server.AUPFile is none)")]
    original = must(found, f"GET {accounts.AUP}").object
    content, version = str(original.get("content", "")), str(original.get("version", ""))
    if not content or not version:
        return [("", FAIL, f"GET {accounts.AUP}: no content, or no version")]
    signer = test.name("signer")
    make_user(test, signer)
    rows: list[Row] = []

    def put_up(n: int) -> str:
        text = f"The Pelican test framework's acceptable use policy {n}, run {test.run}.\n"
        made = must(admin.call("PUT", accounts.AUP, {"content": text}), "putting up a new AUP")
        new = str(made.object.get("version", ""))
        if not new or new == version:
            raise ApiError(f"PUT {accounts.AUP}: version {new!r}, not a new one")
        return new

    def gated(api: Api, route: str, new: str) -> Optional[str]:
        """Return why api's user is not refused route, or None if they are.

        It is a user route, and the refusal must be for want of agreeing
        to new.
        """
        answer = api.call("GET", route)
        said = answer.object
        if answer.status != 403:
            return f"GET {route}: {answer.describe()}, not 403"
        if said.get("requires_aup") is not True or said.get("aup_version") != new:
            found = f"requires_aup {said.get('requires_aup')!r}, {said.get('aup_version')!r}"
            return f"GET {route}'s 403 says {found}, not the new version"
        return None

    def gates() -> Row:
        new = put_up(1)
        problems: list[str] = []
        why = gated(admin, accounts.USERS, new)
        if why:
            problems.append(f"as the admin, {why}")
        public = Api(test.web, None).call("GET", accounts.AUP)
        if public.status != 200 or public.object.get("version") != new:
            problems.append(
                f"GET {accounts.AUP} with no token: {public.describe()}, not the new"
            )
        said = admin.call("GET", accounts.WHOAMI).object
        if said.get("requires_aup") is not True or said.get("aup_version") != new:
            problems.append(f"/whoami says requires_aup {said.get('requires_aup')!r}")
        admin.call("POST", accounts.MY_AUP, {"version": version})
        why = gated(admin, accounts.USERS, new)
        if why:
            problems.append(f"once the admin agreed to the old version, {why}")
        must(admin.call("POST", accounts.MY_AUP, {"version": new}), "agreeing to the new AUP")
        answer = admin.call("GET", accounts.USERS)
        if answer.status != 200:
            problems.append(f"GET {accounts.USERS} once agreed: {answer.describe()}, not 200")
        return outcome(problems, "refused until agreed, not by an old version; policy public")

    def again() -> Row:
        new = put_up(2)
        why = gated(admin, accounts.USERS, new) or gated(
            test.api(signer), accounts.MY_GROUPS, new
        )
        return ("", FAIL, why) if why else ("", PASS, "the admin and a user refused again")

    def restored() -> Row:
        back = must(admin.call("PUT", accounts.AUP, {"content": content}), "restoring the AUP")
        found = str(back.object.get("version", ""))
        if found != version:
            return ("", FAIL, f"the restored AUP's version is {found!r}, not {version!r}")
        answer = test.api(signer).call("GET", accounts.MY_GROUPS)
        if answer.status != 200:
            why = f"GET {accounts.MY_GROUPS}: {answer.describe()}"
            return ("", FAIL, f"as a user who had agreed to it, {why}")
        return ("", PASS, "its version back, and the agreements to it good again")

    try:
        checked(rows, "gates", gates)
        checked(rows, "again", again)
    finally:
        checked(rows, "restored", restored)
        try:
            accounts.agree_to_aup(admin)
        except ApiError as e:
            warn(f"the admin could not agree to the restored AUP: {e}")
    return rows


# --------------------------------------------------------------------------


# Each scenario, by name: it returns its rows, one per case.
RUN: dict[str, Callable[[Test], list[Row]]] = {
    "user-status": user_status,
    "scope-grants": scope_grants,
    "authority-groups": authority_groups,
    "id-keyed-grants": id_keyed_grants,
    "group-invites": group_invites,
    "aup": aup,
}


def guarded(check: Callable[[], list[Row]]) -> list[Row]:
    """Return check()'s rows, or one failing row if it raised."""
    try:
        return check()
    except (ApiError, IssuerError) as e:
        return [("", FAIL, str(e))]
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove what this run made.

    It first removes what earlier runs left.
    """
    test = Test(session)
    try:
        prepare(test)
    except ApiError as e:
        die(f"could not set up the suite: {e}")
    try:
        for name in selected:
            for case, status, note in guarded(functools.partial(RUN[name], test)):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        finish(test)
