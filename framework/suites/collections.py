"""Collections: prefixes of an exported namespace with access control of
their own, kept in an origin's database and granted through its embedded
OAuth2 issuer (Pelican's docs/collections-design.md). The suite needs
the embedded issuer (ORIGIN_ENABLE_ISSUER, on by default) and a
protected namespace, and skips with the external issuer.

Its users are records of the issuer origin (origin-0, or `fed`), which
its admin creates once and later runs reuse: test-curator, a collection
administrator (Server.CollectionAdminUsers in
framework/config.d/base/40-origin.yaml) but no server administrator;
and test-owner, test-writer, test-reader, test-outsider, test-sharer,
test-guest, and test-heir, ordinary users. Each acts through a token
that the origin's web API takes as theirs (credentials.mint_user()),
and gets tokens for data from the namespace's issuer through its device
flow, which the tests approve as the user (testlib/issuer.py). Each run
makes groups of its own, owned by test-owner (test-readers-<run>,
test-writers-<run>, and test-admins-<run>, for test-heir), and
collections under /<namespace>/data/collections/<run>/:

  alpha  private; test-owner's, with the admins group as its admin
         group; the readers may read and the writers write; sharing on
  beta   public; test-owner's; every authenticated user may read

with an object in each (and alpha/shared/obj, for a share), and one
beside them, decoy/obj, in no collection. The scenarios make and remove
collections of their own where they change things:

  users       the admin creates users, and nobody else; a user makes
              groups of their own, and nobody joins one uninvited
  create      a collection administrator creates a collection, an
              ordinary user may not, and a namespace outside every
              export, climbing out of one, or malformed is refused
  visibility  a private collection is invisible to a user with no
              access, a public one visible to every user, and
              canEdit tells each what they may change
  acl         who may grant access, to what roles, and what the ACL
              lists; the admin group revokes
  tokens      what the issuer grants each user: the storage scopes of
              their collections alone, and collection.create only to
              a collection administrator
  data        what those tokens allow, straight at every origin and
              through the federation with `pelican object`: reads and
              writes within a collection, and nothing beside it
  client      `pelican object get` with no token acquires one from the
              issuer itself, which the tests approve: a reader's works,
              and an outsider's gets nothing
  shares      a reader shares part of a collection with a guest, whose
              access is the lesser of the share's and the sharer's,
              and goes with the sharer's; what may not be shared; and
              a multiuser origin refuses shares
  expiry      a grant with an expiry grants nothing after it
  revoke      a revoked grant is gone from the next token; the old
              token keeps it until it expires, as designed
  manage      who may rename and describe a collection, set its
              metadata, and delete it
  ownership   only the owner hands a collection on, by setting its
              owner or by an invite that one user redeems once
  cli         `pelican origin collection` manages collections through
              the issuer's device flow, approved as test-curator, and
              fails for an ordinary user

The run's collections, groups, and objects are removed after the suite;
the users stay.
"""

import json
import os
import subprocess  # nosec B404
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from testlib import accounts, common, credentials, issuer, stores, web
from testlib.accounts import Answer, Api, ApiError, must
from testlib.common import die, warn
from testlib.credentials import PROTECTED
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "users": "the admin creates users, nobody else does, and a user makes groups of their own",
    "create": "a collection administrator creates a collection; bad namespaces are refused",
    "visibility": "a private collection is invisible without access; a public one is not",
    "acl": "who may grant which roles, and what the ACL lists",
    "tokens": "the issuer grants each user the storage scopes of their collections alone",
    "data": "what those tokens allow, at each origin and through the federation",
    "client": "`pelican object get` acquires a token from the issuer, approved as the user",
    "shares": "a reader's share grants a guest the lesser of its access and the reader's",
    "expiry": "a grant with an expiry grants nothing after it",
    "revoke": "a revoked grant is gone from the next token, not the old one",
    "manage": "who may change a collection, its metadata, and delete it",
    "ownership": "only the owner hands a collection on, by setting its owner or by an invite",
    "cli": "`pelican origin collection` manages collections through the device flow",
}

COLLECTIONS = "/api/v1.0/origin_ui/collections"
REDEEM = "/api/v1.0/invites/redeem/collection-ownership"
MY_GROUPS = "/api/v1.0/me/groups"
# The director's list of origins, from which the CLI takes the first.
ORIGINS = "/api/v1.0/director_ui/servers?server_type=origin"

ROLES = ("curator", "owner", "writer", "reader", "outsider", "sharer", "guest", "heir")
# The run's groups, and who test-owner puts in each.
GROUPS = {"readers": ("reader", "sharer"), "writers": ("writer",), "admins": ("heir",)}
# The ACL target that means every authenticated user.
EVERYONE = "@authenticated"

# What a user asks the namespace's issuer for: it grants what the user's
# collections allow, no more.
DATA_SCOPES = ("storage.read:/", "storage.modify:/", "storage.create:/", "share.access:/")
# What the CLI asks the origin's local issuer for.
CONTROL_SCOPES = ("collection.read:/", "collection.create:/")

# How a server may refuse: a collection one may not see is "not found".
REFUSED = (401, 403, 404)

OBJECTS = ("alpha/obj", "alpha/shared/obj", "beta/obj", "decoy/obj")
OBJECT_SIZE = 4096

# How long a grant lasts in `expiry`, in seconds.
EXPIRY = 20
# How long a client may take to acquire its token and finish.
CLIENT_TIMEOUT = 180.0


def username(role: str) -> str:
    """The user who plays role."""
    return "admin" if role == "admin" else f"test-{role}"


def personal(role: str) -> str:
    """The ACL target that grants to role's user alone."""
    return f"user-{username(role)}"


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no embedded issuer, or no protected
    namespace."""
    if fed.external_issuer:
        return "the exports trust the external issuer ('-p auth-external-issuer')"
    if not fed.embedded_issuer:
        return "the origins' embedded issuer is off (ORIGIN_ENABLE_ISSUER=false)"
    if not any(ns in fed.exports for ns in PROTECTED):
        return "no protected namespace is exported"
    return None


class Test:
    """The issuer origin and its namespace, the run's users, groups,
    collections, and objects, and tokens as each user."""

    def __init__(self, session: Session):
        fed = session.fed
        self.session = session
        self.fed = fed
        self.run = session.run
        self.origin = fed.origins[0]
        self.web = fed.origin_web_url
        self.namespace = next(ns for ns in PROTECTED if ns in fed.exports)
        self.caps = fed.exports[self.namespace]
        self.issuer_url = fed.issuer_of(self.namespace)
        self.base = f"data/collections/{self.run}"
        self.prefix = f"/{self.namespace}/{self.base}"
        self.server_token = session.token(self.namespace)
        self.ids: dict[str, str] = {}  # the users' IDs, by role
        self.groups: dict[str, str] = {}  # the groups' IDs, by name
        self.coll: dict[str, str] = {}  # alpha's and beta's IDs
        self.data: dict[str, bytes] = {}  # the objects' bytes, by name
        self._web_tokens: dict[tuple[str, str], str] = {}
        self._clients: dict[str, issuer.Client] = {}
        self._minted = 0

    def web_token(self, role: str, web_url: Optional[str] = None) -> str:
        """A token that the web API of the origin at web_url (the issuer
        origin's by default) takes as role's user."""
        web_url = web_url or self.web
        key = (web_url, role)
        if key not in self._web_tokens:
            svc = urlsplit(web_url).hostname or ""
            self._web_tokens[key] = credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/web.{svc}.{role}"),
                credentials.local_issuer(self.fed, web_url),
                common.signing_key(f"issuer-keys/{svc}"),
                username(role),
                lifetime=3600,
            )
        return self._web_tokens[key]

    def api(self, role: str) -> Api:
        """The issuer origin's web API, as role's user."""
        return Api(self.web, self.web_token(role))

    def client(self, namespace: str) -> issuer.Client:
        """The tests' client of the issuer origin's issuer of namespace."""
        if namespace not in self._clients:
            self._clients[namespace] = issuer.create_client(
                self.web, namespace, self.web_token("admin")
            )
        return self._clients[namespace]

    def data_token(self, role: str) -> str:
        """A token for the namespace from its issuer, as role's user: what
        the user's collections allow right now."""
        return issuer.token(
            self.client(f"/{self.namespace}"), self.web_token(role), DATA_SCOPES
        )

    def control_token(self, role: str) -> str:
        """A token from the origin's local issuer, as role's user, for
        managing collections."""
        return issuer.token(
            self.client(issuer.LOCAL_NAMESPACE), self.web_token(role), CONTROL_SCOPES
        )

    def token_file(self, role: str) -> str:
        """A fresh data token for role's user, in a file for `--token`."""
        self._minted += 1
        path = self.session.path(f"tokens/collections.{role}.{self._minted}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.data_token(role))
        return path

    def group_name(self, name: str) -> str:
        """The run's group name (the ACL target), e.g. test-readers-<run>."""
        return f"test-{name}-{self.run}"

    def path(self, name: str) -> str:
        """The namespace of the run's collection name: its federation path."""
        return f"{self.prefix}/{name}"

    def rel(self, name: str) -> str:
        """Object or collection name's path within the namespace."""
        return f"{self.base}/{name}"

    def scope(self, kind: str, name: str) -> str:
        """The storage scope for collection name that the namespace's
        issuer mints: `storage.<kind>` of its path within the namespace."""
        return f"storage.{kind}:/{self.base}/{name}"

    def object_url(self, origin: common.Origin, name: str) -> str:
        """Where origin serves object name."""
        return stores.url(origin, self.namespace, self.rel(name))

    def federation_url(self, name: str) -> str:
        """Object name's URL through the federation."""
        return f"{self.fed.url}/{self.namespace}/{self.rel(name)}"


# --------------------------------------------------------------------------
# The collections API (origin/collections.go in Pelican).


def create(
    api: Api, name: str, namespace: str, visibility: str = "private", sharing: bool = False
) -> Answer:
    """Create a collection named name at namespace, owned by the caller."""
    body: dict[str, Any] = {"name": name, "namespace": namespace, "visibility": visibility}
    body["enableSharing"] = sharing
    return api.call("POST", COLLECTIONS, body)


def get(api: Api, cid: str) -> Answer:
    """One collection, if the caller may see it."""
    return api.call("GET", f"{COLLECTIONS}/{cid}")


def listed(api: Api) -> set[str]:
    """The IDs of the collections the caller may see."""
    return {str(row.get("id")) for row in objects(api.call("GET", COLLECTIONS).array)}


def patch(api: Api, cid: str, **fields: Any) -> Answer:
    """Change a collection's fields, e.g. description or ownerId."""
    return api.call("PATCH", f"{COLLECTIONS}/{cid}", fields)


def delete(api: Api, cid: str) -> Answer:
    """Delete a collection."""
    return api.call("DELETE", f"{COLLECTIONS}/{cid}")


def grant(
    api: Api, cid: str, target: str, role: str, expires_at: Optional[str] = None
) -> Answer:
    """Grant role (read or write) on a collection to target: a group
    name, a user (personal()), or EVERYONE; until expires_at (RFC 3339)
    if given."""
    body: dict[str, Any] = {"groupId": target, "role": role}
    if expires_at:
        body["expiresAt"] = expires_at
    return api.call("POST", f"{COLLECTIONS}/{cid}/acl", body)


def revoke(api: Api, cid: str, target: str, role: str) -> Answer:
    """Revoke a grant."""
    return api.call("DELETE", f"{COLLECTIONS}/{cid}/acl", {"groupId": target, "role": role})


def grants(api: Api, cid: str) -> set[tuple[str, str]]:
    """A collection's ACL, as (target, role) pairs: a group name, a
    username, or EVERYONE, and read or write."""
    rows = objects(must(api.call("GET", f"{COLLECTIONS}/{cid}/acl"), "listing the ACL").array)
    return {
        (str(row.get("subjectName", "")).removeprefix("user-"), str(row.get("role", "")))
        for row in rows
    }


def set_metadata(api: Api, cid: str, key: str, value: str) -> Answer:
    """Set one key of a collection's metadata."""
    return api.call("PUT", f"{COLLECTIONS}/{cid}/metadata/{key}", {"value": value})


def metadata(api: Api, cid: str) -> Answer:
    """A collection's metadata: rows of key and value."""
    return api.call("GET", f"{COLLECTIONS}/{cid}/metadata")


def share(api: Api, cid: str, name: str, namespace: str) -> Answer:
    """Share part of a collection (namespace, within its own) as a
    collection of the caller's."""
    return api.call(
        "POST", f"{COLLECTIONS}/{cid}/shares", {"name": name, "namespace": namespace}
    )


def objects(items: Iterable[Any]) -> list[dict[str, Any]]:
    """The JSON objects among items."""
    return [found for found in (common.as_object(item) for item in items) if found is not None]


def storage_scopes(token: str) -> frozenset[str]:
    """A token's scopes for data: storage.* and share.access."""
    return frozenset(s for s in issuer.scopes(token) if s.startswith(("storage.", "share.")))


def rfc3339(when: float) -> str:
    """when (seconds since the epoch), as a server reads it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def outcome(problems: Sequence[str], note: str) -> tuple[str, str]:
    """A row: the first problem, or note if there were none."""
    return (FAIL, common.first(problems)) if problems else (PASS, note)


# --------------------------------------------------------------------------
# The fixture.


def make_collection(
    test: Test,
    name: str,
    rules: Sequence[tuple[str, str]] = (),
    admins: bool = False,
    sharing: bool = False,
    visibility: str = "private",
) -> str:
    """Have test-curator create the run's collection name, hand it to
    test-owner (with the admins group as its admin group, if admins),
    and have test-owner grant rules ((target, role) pairs): its ID."""
    curator = test.api("curator")
    made = must(create(curator, name, test.path(name), visibility, sharing), f"creating {name}")
    cid = str(made.object.get("id", ""))
    fields: dict[str, Any] = {"ownerId": test.ids["owner"]}
    if admins:
        fields["adminId"] = test.groups["admins"]
    must(patch(curator, cid, **fields), f"handing {name} to test-owner")
    owner = test.api("owner")
    for target, role in rules:
        must(grant(owner, cid, target, role), f"granting {role} on {name} to {target}")
    return cid


def sweep(test: Test, this_run: bool) -> None:
    """Remove the collections under the suite's directory: the run's, or
    earlier runs' leftovers."""
    admin = test.api("admin")
    directory = f"/{test.namespace}/data/collections/"
    for row in objects(admin.call("GET", COLLECTIONS).array):
        namespace = str(row.get("namespace", ""))
        if namespace.startswith(directory) and namespace.startswith(test.prefix) == this_run:
            gone = delete(admin, str(row.get("id")))
            if not gone.ok:
                warn(f"could not remove collection {row.get('name')}: {gone.describe()}")


def prepare(test: Test) -> None:
    """Make the run's users (once), groups, collections, and objects."""
    sweep(test, this_run=False)
    admin = test.api("admin")
    for role in ROLES:
        test.ids[role] = accounts.ensure_user(admin, username(role))
    owner = test.api("owner")
    for name, members in GROUPS.items():
        test.groups[name] = accounts.create_group(owner, test.group_name(name))
        for member in members:
            accounts.add_member(owner, test.groups[name], test.ids[member])
    readers, writers = test.group_name("readers"), test.group_name("writers")
    test.coll["alpha"] = make_collection(
        test, "alpha", ((readers, "read"), (writers, "write")), admins=True, sharing=True
    )
    test.coll["beta"] = make_collection(
        test, "beta", ((EVERYONE, "read"),), visibility="public"
    )
    for name in OBJECTS:
        test.data[name] = os.urandom(OBJECT_SIZE)
        errors = stores.seed_everywhere(
            test.fed.origins,
            test.namespace,
            test.rel(name),
            test.data[name],
            test.server_token,
            test.caps,
        )
        if errors:
            die(f"could not put {name} in place: {errors[0]}")


def finish(test: Test) -> None:
    """Remove the run's collections, groups, and objects."""
    sweep(test, this_run=True)
    admin = test.api("admin")
    for name, gid in test.groups.items():
        failed = accounts.delete_group(admin, gid)
        if failed:
            warn(f"could not remove the {name} group: {failed}")
    for origin in stores.unique(test.fed.origins):
        for error in stores.empty_tree(
            origin, test.namespace, "data/collections", test.server_token
        ):
            warn(error)


# --------------------------------------------------------------------------
# The clients, acquiring tokens from the issuer themselves.


@dataclass
class ClientRun:
    """How a client that acquires its own tokens ran."""

    status: int
    stdout: str
    stderr: str
    approvals: int  # how many device flows it started, which the tests approved
    problems: list[str]  # what went wrong approving


def client_env(test: Test, role: str) -> dict[str, str]:
    """The environment for a client acting as role's user: it may acquire
    a token with no terminal, and keeps its credentials apart from other
    users', since the issuer binds a client that registers itself to the
    first user who approves a flow for it."""
    env = dict(test.session.env)
    env["PELICAN_SKIP_TERMINAL_CHECK"] = "1"
    store = test.session.path(f"credentials/{role}")
    os.makedirs(store, exist_ok=True)
    env["PELICAN_CLIENT_CREDENTIALFILE"] = os.path.join(store, "client-credentials.pem")
    return env


def run_approving(test: Test, args: Sequence[str], role: str) -> ClientRun:
    """Run `pelican <args>` as role's user, approving each device flow it
    announces on its stderr as that user."""
    with subprocess.Popen(  # nosec B603
        [test.session.pelican, *args],
        env=client_env(test, role),
        cwd=test.session.tmp,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("the client's output is not piped")
        stdout = proc.stdout
        out: list[str] = []
        collector = threading.Thread(target=lambda: out.append(stdout.read()))
        collector.start()
        killer = threading.Timer(CLIENT_TIMEOUT, proc.kill)
        killer.start()
        done = ClientRun(0, "", "", 0, [])
        err: list[str] = []
        for line in proc.stderr:
            err.append(line)
            flow = issuer.approval_url(line)
            if flow:
                done.approvals += 1
                try:
                    web_url = flow[0].split(issuer.ROUTE, 1)[0]
                    issuer.approve(flow[0], flow[1], test.web_token(role, web_url))
                except issuer.IssuerError as e:
                    done.problems.append(str(e))
        done.status = proc.wait()
        killer.cancel()
        collector.join()
        done.stdout, done.stderr = "".join(out), "".join(err)
        if done.status < 0:
            done.problems.append(f"the client was killed after {CLIENT_TIMEOUT:.0f}s")
        return done


def cli_json(out: str) -> Any:
    """The JSON that `pelican --json` printed, after any preamble."""
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The scenarios.


def users(test: Test) -> tuple[str, str]:
    """The admin creates users, and nobody else; a user makes groups of
    their own, and nobody joins one uninvited."""
    problems: list[str] = []
    found = {
        row.get("username")
        for row in objects(test.api("admin").call("GET", accounts.USERS).array)
    }
    missing = [username(role) for role in ROLES if username(role) not in found]
    if missing:
        problems.append(f"the admin's list of users lacks {', '.join(missing)}")
    refused = test.api("outsider").call(
        "POST", accounts.USERS, {"username": f"test-nobody-{test.run}"}
    )
    if refused.status != 403:
        problems.append(
            f"an ordinary user's POST {accounts.USERS}: {refused.describe()}, not 403"
        )
    mine = {row.get("id") for row in objects(test.api("reader").call("GET", MY_GROUPS).array)}
    if test.groups["readers"] not in mine:
        problems.append(f"test-reader's groups ({MY_GROUPS}) lack the readers group")
    joined = test.api("reader").call(
        "POST",
        f"{accounts.GROUPS}/{test.groups['writers']}/members",
        {"userId": test.ids["reader"]},
    )
    if joined.ok:
        problems.append("test-reader could join the writers group, which test-owner owns")
    return outcome(problems, "the admin's users; test-owner's groups")


def create_scenario(test: Test) -> tuple[str, str]:
    """A collection administrator creates a collection, an ordinary user
    may not, and a bad namespace is refused."""
    problems: list[str] = []
    curator = test.api("curator")
    made = create(curator, "gamma", test.path("gamma"))
    if made.status != 201:
        problems.append(f"test-curator's create: {made.describe()}, not 201")
    else:
        row = made.object
        wanted = (
            ("namespace", test.path("gamma")),
            ("ownerId", test.ids["curator"]),
            ("visibility", "private"),
        )
        for name, want in wanted:
            if row.get(name) != want:
                problems.append(
                    f"the new collection's {name} is {row.get(name)!r}, not {want!r}"
                )
        gone = delete(curator, str(row.get("id")))
        if not gone.ok:
            problems.append(f"test-curator's delete of it: {gone.describe()}")
        elif get(curator, str(row.get("id"))).status != 404:
            problems.append("the deleted collection is still there")
    plain = create(test.api("owner"), "gamma", test.path("gamma"))
    if plain.status != 403:
        problems.append(
            f"test-owner's create, who administers no collections: {plain.describe()}, not 403"
        )
    bad = (
        (f"/elsewhere/{test.run}", "outside every export"),
        (f"/{test.namespace}/../../etc", "climbing out of its export"),
        (f"{test.path('bad')} x", "with a space"),
    )
    for namespace, why in bad:
        refused = create(curator, "bad", namespace)
        if refused.status != 400:
            problems.append(f"a create {why} ({namespace!r}): {refused.describe()}, not 400")
        if refused.ok:
            delete(curator, str(refused.object.get("id")))
    odd = create(curator, "bad", test.path("bad"), visibility="secret")
    if odd.status != 400:
        problems.append(f"a create with visibility `secret`: {odd.describe()}, not 400")
    if odd.ok:
        delete(curator, str(odd.object.get("id")))
    return outcome(problems, "test-curator may, test-owner may not, bad namespaces refused")


def visibility_scenario(test: Test) -> tuple[str, str]:
    """A private collection is invisible to a user with no access, a
    public one visible to every user, and canEdit says who may change
    each."""
    problems: list[str] = []
    alpha, beta = test.coll["alpha"], test.coll["beta"]
    outsider = test.api("outsider")
    hidden = get(outsider, alpha)
    if hidden.status != 404:
        problems.append(f"test-outsider's GET of alpha, private: {hidden.describe()}, not 404")
    seen = listed(outsider)
    if alpha in seen:
        problems.append("test-outsider's list of collections includes alpha, private")
    if beta not in seen:
        problems.append("test-outsider's list of collections lacks beta, public")
    shown = get(outsider, beta)
    if shown.status != 200:
        problems.append(f"test-outsider's GET of beta, public: {shown.describe()}")
    elif shown.object.get("canEdit") is not False:
        problems.append("test-outsider may edit beta, says canEdit")
    for role, can_edit in (
        ("reader", False),
        ("writer", False),
        ("heir", True),
        ("owner", True),
    ):
        answer = get(test.api(role), alpha)
        if answer.status != 200:
            problems.append(f"{username(role)}'s GET of alpha: {answer.describe()}")
        elif answer.object.get("canEdit") is not can_edit:
            problems.append(
                f"canEdit of alpha is {answer.object.get('canEdit')!r} for {username(role)}"
            )
    if alpha not in listed(test.api("curator")):
        problems.append(
            "test-curator's list of collections, as their administrator, lacks alpha"
        )
    changed = patch(outsider, beta, description="changed by test-outsider")
    if changed.status != 404:
        problems.append(f"test-outsider's PATCH of beta: {changed.describe()}, not 404")
    return outcome(problems, "alpha hidden from test-outsider; beta shown, not editable")


def acl_scenario(test: Test) -> tuple[str, str]:
    """Who may grant access, to what roles, and what the ACL lists."""
    problems: list[str] = []
    readers, writers = test.group_name("readers"), test.group_name("writers")
    delta = make_collection(test, "delta", ((writers, "write"),))
    owner = test.api("owner")
    answer = grant(test.api("writer"), delta, readers, "read")
    if answer.status != 404:
        problems.append(
            f"test-writer's grant, who may only write: {answer.describe()}, not 404"
        )
    answer = grant(owner, delta, readers, "owner")
    if answer.status != 400:
        problems.append(f"a grant of role `owner`: {answer.describe()}, not 400")
    answer = grant(owner, delta, f"test-nobody-{test.run}", "read")
    if answer.status != 400:
        problems.append(f"a grant to a group that does not exist: {answer.describe()}, not 400")
    later = rfc3339(time.time() + 3600)
    for target, role, until in ((readers, "read", None), (personal("guest"), "read", later)):
        answer = grant(owner, delta, target, role, until)
        if not answer.ok:
            problems.append(f"test-owner's grant of {role} to {target}: {answer.describe()}")
    found = grants(owner, delta)
    wanted = {(writers, "write"), (readers, "read"), (username("guest"), "read")}
    if found != wanted:
        problems.append(f"the ACL lists {sorted(found)}, not {sorted(wanted)}")
    answer = revoke(test.api("heir"), delta, personal("guest"), "read")
    if not answer.ok:
        problems.append(f"test-heir's revoke, as the admin group: {answer.describe()}")
    elif (username("guest"), "read") in grants(owner, delta):
        problems.append("the revoked grant is still listed")
    must(delete(owner, delta), "deleting delta")
    return outcome(problems, "owner and admin group grant and revoke; writers may not")


def tokens(test: Test) -> tuple[str, str]:
    """What the issuer grants each user: the storage scopes of their
    collections alone, and collection.create only to a collection
    administrator."""
    problems: list[str] = []
    read_alpha, read_beta = test.scope("read", "alpha"), test.scope("read", "beta")
    wanted = {
        "writer": {
            read_alpha,
            test.scope("modify", "alpha"),
            test.scope("create", "alpha"),
            read_beta,
        },
        "reader": {read_alpha, read_beta},
        "outsider": {read_beta},
    }
    for role, want in wanted.items():
        token = test.data_token(role)
        found = storage_scopes(token)
        if set(found) != want:
            problems.append(f"{username(role)}'s token has {sorted(found)}, not {sorted(want)}")
        claims = issuer.claims(token)
        if claims.get("iss") != test.issuer_url:
            problems.append(f"{username(role)}'s token's issuer is {claims.get('iss')!r}")
        if claims.get("sub") != username(role):
            problems.append(f"{username(role)}'s token's subject is {claims.get('sub')!r}")
    local = credentials.local_issuer(test.fed, test.web)
    for role, may_create in (("curator", True), ("outsider", False)):
        token = test.control_token(role)
        found = issuer.scopes(token)
        if "collection.read:/" not in found:
            problems.append(f"{username(role)}'s local token lacks collection.read:/")
        if ("collection.create:/" in found) != may_create:
            problems.append(f"{username(role)}'s local token has {sorted(found)}")
        if issuer.claims(token).get("iss") != local:
            problems.append(f"{username(role)}'s local token's issuer is not {local}")
        made = create(Api(test.web, token), "eta", test.path("eta"))
        if may_create and made.status != 201:
            problems.append(
                f"a create with test-curator's local token: {made.describe()}, not 201"
            )
        if not may_create and made.status != 403:
            problems.append(
                f"a create with test-outsider's local token: {made.describe()}, not 403"
            )
        if made.ok:
            must(delete(test.api("curator"), str(made.object.get("id"))), "deleting eta")
    return outcome(
        problems, "each token has its collections' scopes; collection.create test-curator's"
    )


def data(test: Test) -> tuple[str, str]:
    """What the tokens allow, straight at every origin and through the
    federation."""
    problems: list[str] = []
    token = {role: test.data_token(role) for role in ("writer", "reader", "outsider")}
    writes = "Writes" in test.caps
    reads = (
        ("reader", "alpha/obj", True),
        ("reader", "beta/obj", True),
        ("reader", "decoy/obj", False),
        ("outsider", "alpha/obj", False),
        ("outsider", "beta/obj", True),
        ("writer", "decoy/obj", False),
    )
    notes: list[str] = []
    if "DirectReads" in test.caps:
        for origin in test.fed.origins:
            for role, name, allowed in reads:
                url = test.object_url(origin, name)
                answer = web.request("GET", url, token=token[role])
                if allowed and (answer.status != 200 or answer.body != test.data[name]):
                    problems.append(
                        f"GET {url} as {username(role)}: {answer.describe()}, or wrong bytes"
                    )
                if not allowed and answer.status not in REFUSED:
                    problems.append(
                        f"GET {url} as {username(role)}: {answer.describe()}, not refused"
                    )
            if writes:
                problems += write_problems(test, origin, token)
        notes.append(f"at {len(test.fed.origins)} origin(s)")
    else:
        notes.append("no direct reads")
    if not test.fed.standalone:
        problems += federation_problems(test, writes)
        notes.append("through the federation")
    return outcome(problems, ", ".join(notes))


def write_problems(test: Test, origin: common.Origin, token: dict[str, str]) -> list[str]:
    """What is wrong with PUT and DELETE straight to origin with the
    tokens: only test-writer may, and only within alpha."""
    problems: list[str] = []
    payload = os.urandom(OBJECT_SIZE)
    for role, name in (
        ("reader", f"alpha/put-reader-{origin.svc}"),
        ("writer", "decoy/put-writer"),
    ):
        url = test.object_url(origin, name)
        answer = web.request("PUT", url, token=token[role], upload=payload)
        if answer.status not in REFUSED:
            problems.append(f"PUT {url} as {username(role)}: {answer.describe()}, not refused")
    name = f"alpha/put-writer-{origin.svc}"
    url = test.object_url(origin, name)
    answer = web.request("PUT", url, token=token["writer"], upload=payload)
    if not answer.ok:
        return problems + [f"PUT {url} as test-writer: {answer.describe()}"]
    back = web.request("GET", url, token=test.server_token)
    if back.status != 200 or back.body != payload:
        problems.append(f"GET {url} after test-writer's PUT: {back.describe()}, or wrong bytes")
    if not stores.cannot(test.fed, "delete"):
        answer = web.request("DELETE", url, token=token["writer"])
        if not answer.ok:
            problems.append(f"DELETE {url} as test-writer: {answer.describe()}")
    return problems


def federation_problems(test: Test, writes: bool) -> list[str]:
    """What is wrong with `pelican object` through the federation with
    the tokens: test-reader gets alpha's object and test-outsider does
    not; test-writer puts one and test-reader does not."""
    problems: list[str] = []
    session = test.session
    for role, allowed in (("reader", True), ("outsider", False)):
        dest = session.path(f"get-{role}")
        status, _, err = session.pelican_cmd(
            "object",
            "get",
            test.federation_url("alpha/obj"),
            dest,
            "--token",
            test.token_file(role),
        )
        if allowed and (
            status != 0 or common.sha256(dest) != common.digest(test.data["alpha/obj"])
        ):
            problems.append(f"`object get` as test-reader: exit {status}: {last_line(err)}")
        if not allowed and status == 0:
            problems.append("`object get` as test-outsider succeeded")
    if not writes:
        return problems
    source = session.path("put-source")
    with open(source, "wb") as f:
        f.write(os.urandom(OBJECT_SIZE))
    for role, allowed in (("writer", True), ("reader", False)):
        status, _, err = session.pelican_cmd(
            "object",
            "put",
            source,
            test.federation_url(f"alpha/put-{role}"),
            "--token",
            test.token_file(role),
        )
        if allowed and status != 0:
            problems.append(f"`object put` as test-writer: exit {status}: {last_line(err)}")
        if not allowed and status == 0:
            problems.append("`object put` as test-reader succeeded")
    return problems


def client(test: Test) -> tuple[str, str]:
    """`pelican object get` with no token acquires one from the issuer,
    which the tests approve as the user: test-reader's works, and
    test-outsider's gets nothing."""
    if test.fed.standalone:
        return SKIP, "a standalone origin has no director to find the issuer through"
    problems: list[str] = []
    for role, allowed in (("reader", True), ("outsider", False)):
        dest = test.session.path(f"client-get-{role}")
        done = run_approving(
            test, ["object", "get", test.federation_url("alpha/obj"), dest], role
        )
        problems += done.problems
        if done.approvals == 0:
            problems.append(
                f"the client as {username(role)} announced no device flow to approve"
            )
        if allowed and (
            done.status != 0 or common.sha256(dest) != common.digest(test.data["alpha/obj"])
        ):
            problems.append(
                f"`object get` as test-reader: exit {done.status}: {last_line(done.stderr)}"
            )
        if not allowed and done.status == 0:
            problems.append("`object get` as test-outsider succeeded")
    return outcome(problems, "test-reader's get approved and done; test-outsider's refused")


def shares(test: Test) -> tuple[str, str]:
    """test-sharer, who may read alpha, shares alpha/shared with
    test-guest, whose access is the lesser of the share's and
    test-sharer's, and goes with test-sharer's."""
    problems: list[str] = []
    alpha = test.coll["alpha"]
    owner, sharer = test.api("owner"), test.api("sharer")
    made = share(sharer, alpha, "shared", test.path("alpha/shared"))
    if test.fed.multiuser:
        if made.status != 409:
            return FAIL, f"a share at a multiuser origin: {made.describe()}, not 409"
        return PASS, "refused (409): a multiuser origin cannot impersonate a share's owner"
    if made.status != 201:
        return FAIL, f"test-sharer's share of alpha/shared: {made.describe()}, not 201"
    sid = str(made.object.get("id"))
    if made.object.get("parentCollectionId") != alpha:
        problems.append("the share's parentCollectionId is not alpha's ID")
    answer = share(test.api("outsider"), alpha, "shared", test.path("alpha/shared"))
    if answer.status != 404:
        problems.append(
            f"test-outsider's share of alpha, unreadable to them: {answer.describe()}, not 404"
        )
    answer = share(sharer, test.coll["beta"], "shared", test.path("beta"))
    if answer.status != 409:
        problems.append(f"a share of beta, with sharing off: {answer.describe()}, not 409")
    answer = share(sharer, sid, "nested", test.path("alpha/shared/nested"))
    if answer.status not in (400, 409):
        problems.append(f"a share of the share: {answer.describe()}, not refused")
    must(grant(sharer, sid, personal("guest"), "read"), "granting read on the share")
    access, shared = f"share.access:/{sid}", test.scope("read", "alpha/shared")
    found = storage_scopes(test.data_token("guest"))
    if not {access, shared} <= found or test.scope("read", "alpha") in found:
        problems.append(
            f"test-guest's token has {sorted(found)}, not {access} and {shared} alone"
        )
    if "DirectReads" in test.caps:
        for origin in test.fed.origins:
            token = test.data_token("guest")
            url = test.object_url(origin, "alpha/shared/obj")
            reply = web.request("GET", url, token=token)
            if reply.status != 200 or reply.body != test.data["alpha/shared/obj"]:
                problems.append(f"GET {url} as test-guest: {reply.describe()}, or wrong bytes")
            url = test.object_url(origin, "alpha/obj")
            reply = web.request("GET", url, token=token)
            if reply.status not in REFUSED:
                problems.append(
                    f"GET {url} as test-guest, outside the share: {reply.describe()}"
                )
    must(grant(sharer, sid, personal("guest"), "write"), "granting write on the share")
    found = storage_scopes(test.data_token("guest"))
    if test.scope("modify", "alpha/shared") in found:
        problems.append("test-guest may write the share, which test-sharer may only read")
    must(
        grant(owner, alpha, personal("sharer"), "write"), "granting test-sharer write on alpha"
    )
    found = storage_scopes(test.data_token("guest"))
    if test.scope("modify", "alpha/shared") not in found:
        problems.append("test-guest may not write the share once test-sharer may write alpha")
    must(revoke(owner, alpha, personal("sharer"), "write"), "revoking test-sharer's write")
    accounts.remove_member(owner, test.groups["readers"], test.ids["sharer"])
    found = storage_scopes(test.data_token("guest"))
    if found & {access, shared}:
        problems.append(
            f"test-guest's token has {sorted(found)} once test-sharer may not read alpha"
        )
    must(delete(sharer, sid), "deleting the share")
    return outcome(problems, "test-guest's access through the share follows test-sharer's")


def expiry(test: Test) -> tuple[str, str]:
    """A grant with an expiry grants nothing after it."""
    alpha, owner = test.coll["alpha"], test.api("owner")
    deadline = time.time() + EXPIRY
    must(
        grant(owner, alpha, personal("guest"), "read", rfc3339(deadline)),
        "granting with an expiry",
    )
    try:
        read = test.scope("read", "alpha")
        if read not in storage_scopes(test.data_token("guest")):
            return FAIL, f"test-guest's token lacks {read} before the grant expires"
        time.sleep(max(0.0, deadline + 2 - time.time()))
        if read in storage_scopes(test.data_token("guest")):
            return FAIL, f"test-guest's token has {read} after the grant expired"
    finally:
        revoke(owner, alpha, personal("guest"), "read")
    return PASS, f"granted for {EXPIRY}s, then gone"


def revoke_scenario(test: Test) -> tuple[str, str]:
    """A revoked grant is gone from the next token; the old one keeps it
    until it expires."""
    problems: list[str] = []
    alpha, owner = test.coll["alpha"], test.api("owner")
    read = test.scope("read", "alpha")
    old = test.data_token("reader")
    if read not in storage_scopes(old):
        return FAIL, f"test-reader's token lacks {read} before the revoke"
    must(revoke(owner, alpha, test.group_name("readers"), "read"), "revoking the readers' read")
    if (test.group_name("readers"), "read") in grants(owner, alpha):
        problems.append("the revoked grant is still listed")
    new = test.data_token("reader")
    if read in storage_scopes(new):
        problems.append(f"test-reader's next token still has {read}")
    if test.scope("read", "beta") not in storage_scopes(new):
        problems.append("test-reader's next token lost beta's read")
    if "DirectReads" in test.caps:
        url = test.object_url(test.origin, "alpha/obj")
        answer = web.request("GET", url, token=old)
        if answer.status != 200:
            problems.append(f"GET {url} with the old token: {answer.describe()}, not 200")
        answer = web.request("GET", url, token=new)
        if answer.status not in REFUSED:
            problems.append(f"GET {url} with the new token: {answer.describe()}, not refused")
    return outcome(problems, "gone from the next token; the old one still reads")


def manage(test: Test) -> tuple[str, str]:
    """Who may rename and describe a collection, set its metadata, and
    delete it."""
    problems: list[str] = []
    readers, writers = test.group_name("readers"), test.group_name("writers")
    eps = make_collection(test, "epsilon", ((readers, "read"), (writers, "write")), admins=True)
    owner, heir, writer, reader = (test.api(r) for r in ("owner", "heir", "writer", "reader"))
    allowed = (
        ("test-owner", patch(owner, eps, description="described by test-owner")),
        ("test-heir, as the admin group", patch(heir, eps, name="renamed by test-heir")),
        ("test-writer, who may write", patch(writer, eps, description="by test-writer")),
    )
    for who, answer in allowed:
        if not answer.ok:
            problems.append(f"a PATCH by {who}: {answer.describe()}")
    row = get(owner, eps).object
    if row.get("name") != "renamed by test-heir" or row.get("description") != "by test-writer":
        found = f"{row.get('name')!r} and {row.get('description')!r}"
        problems.append(f"after the PATCHes, the name and description are {found}")
    refused = (
        ("test-heir's transfer", patch(heir, eps, ownerId=test.ids["heir"])),
        ("test-heir's delete", delete(heir, eps)),
        ("test-writer's clearing of the admin group", patch(writer, eps, adminId="")),
        ("test-writer's grant", grant(writer, eps, personal("guest"), "read")),
        ("test-reader's PATCH", patch(reader, eps, description="by test-reader")),
        ("test-reader's metadata", set_metadata(reader, eps, "by", "test-reader")),
        ("test-writer's delete", delete(writer, eps)),
    )
    for what, answer in refused:
        if answer.status != 404:
            problems.append(f"{what}: {answer.describe()}, not 404")
    answer = set_metadata(owner, eps, "key", "value")
    if not answer.ok:
        problems.append(f"test-owner's metadata: {answer.describe()}")
    rows = {(r.get("key"), r.get("value")) for r in objects(metadata(reader, eps).array)}
    if rows != {("key", "value")}:
        problems.append(f"the metadata test-reader lists is {sorted(rows)}, not key=value")
    if get(owner, eps).object.get("metadata") != {"key": "value"}:
        problems.append("the collection's metadata field lacks key=value")
    answer = owner.call("DELETE", f"{COLLECTIONS}/{eps}/metadata/key")
    if not answer.ok:
        problems.append(f"test-owner's delete of the metadata: {answer.describe()}")
    elif objects(metadata(owner, eps).array):
        problems.append("the deleted metadata is still listed")
    answer = delete(owner, eps)
    if not answer.ok:
        problems.append(f"test-owner's delete: {answer.describe()}")
    elif get(owner, eps).status != 404:
        problems.append("the deleted collection is still there")
    return outcome(problems, "owner, admin group, and writers change; only the owner deletes")


def ownership(test: Test) -> tuple[str, str]:
    """Only the owner hands a collection on: by setting its owner, or by
    an invite that one user redeems once."""
    problems: list[str] = []
    zeta = make_collection(test, "zeta", ((test.group_name("writers"), "write"),))
    owner, heir, guest = test.api("owner"), test.api("heir"), test.api("guest")
    answer = patch(test.api("writer"), zeta, ownerId=test.ids["writer"])
    if answer.status != 404:
        problems.append(f"test-writer's taking of zeta: {answer.describe()}, not 404")
    answer = patch(owner, zeta, ownerId=test.ids["heir"])
    if not answer.ok:
        return FAIL, f"test-owner's transfer of zeta to test-heir: {answer.describe()}"
    if get(heir, zeta).object.get("ownerId") != test.ids["heir"]:
        problems.append("after the transfer, zeta's ownerId is not test-heir's")
    answer = delete(owner, zeta)
    if answer.status != 404:
        problems.append(f"the former owner's delete: {answer.describe()}, not 404")
    invite = heir.call("POST", f"{COLLECTIONS}/{zeta}/ownership-invites", {})
    token = str(invite.object.get("inviteToken", ""))
    if invite.status != 201 or not token:
        problems.append(f"test-heir's ownership invite: {invite.describe()}")
    else:
        answer = guest.call("POST", REDEEM, {"token": token})
        if not answer.ok:
            problems.append(f"test-guest's redeeming of the invite: {answer.describe()}")
        elif get(guest, zeta).object.get("ownerId") != test.ids["guest"]:
            problems.append("after the invite, zeta's ownerId is not test-guest's")
        again = test.api("outsider").call("POST", REDEEM, {"token": token})
        if again.ok:
            problems.append("test-outsider redeemed the invite too")
    answer = delete(guest, zeta)
    if not answer.ok:
        must(delete(test.api("curator"), zeta), "deleting zeta")
        problems.append(f"test-guest's delete, as the owner at last: {answer.describe()}")
    return outcome(problems, "test-owner to test-heir by PATCH, to test-guest by invite")


def first_origin(test: Test) -> Optional[str]:
    """The web URL of the origin the CLI will manage: the first the
    director lists."""
    answer = Api(test.fed.directors[0], None).call("GET", ORIGINS)
    for row in objects(answer.array):
        if row.get("webUrl"):
            return str(row["webUrl"])
    return None


def cli(test: Test) -> tuple[str, str]:
    """`pelican origin collection` manages collections through the
    issuer's device flow, approved as test-curator, and fails for an
    ordinary user."""
    if test.fed.standalone:
        return SKIP, "a standalone origin has no director for the CLI to find it through"
    first = first_origin(test)
    if first is None:
        return FAIL, f"{test.fed.directors[0]} lists no origin for the CLI to manage"
    if first != test.web:
        return SKIP, f"the CLI manages only the first origin the director lists, {first}"
    problems: list[str] = []
    curator = test.api("curator")

    def manage_as(role: str, *args: str) -> ClientRun:
        done = run_approving(
            test, ["--json", "-f", test.fed.url, "origin", "collection", *args], role
        )
        problems.extend(done.problems)
        return done

    made = manage_as(
        "curator", "create", "--name", f"cli-{test.run}", "--namespace", test.path("cli")
    )
    row = common.as_object(cli_json(made.stdout)) or {}
    cid = str(row.get("id", ""))
    if made.status != 0 or not cid:
        why = f"exit {made.status}: {last_line(made.stderr)}"
        return FAIL, f"`collection create` as test-curator: {why}"
    if row.get("namespace") != test.path("cli"):
        problems.append(f"the created collection's namespace is {row.get('namespace')!r}")
    done = manage_as(
        "outsider", "create", "--name", "cli-outsider", "--namespace", test.path("cli-outsider")
    )
    if done.status == 0:
        problems.append("`collection create` as test-outsider succeeded")
        delete(curator, str((common.as_object(cli_json(done.stdout)) or {}).get("id")))
    done = manage_as("curator", "list")
    if cid not in {
        str(r.get("id")) for r in objects(common.as_array(cli_json(done.stdout)) or [])
    }:
        problems.append(f"`collection list`: exit {done.status}, without the new collection")
    done = manage_as("curator", "get", cid)
    if (common.as_object(cli_json(done.stdout)) or {}).get("namespace") != test.path("cli"):
        problems.append(f"`collection get`: exit {done.status}: {last_line(done.stderr)}")
    done = manage_as("curator", "update", cid, "--description", "managed by the CLI")
    if done.status != 0 or get(curator, cid).object.get("description") != "managed by the CLI":
        problems.append(f"`collection update`: exit {done.status}: {last_line(done.stderr)}")
    readers = test.group_name("readers")
    done = manage_as("curator", "acl", "grant", cid, "--group-id", readers, "--role", "read")
    if done.status != 0 or (readers, "read") not in grants(curator, cid):
        problems.append(f"`collection acl grant`: exit {done.status}: {last_line(done.stderr)}")
    done = manage_as("curator", "acl", "list", cid)
    if readers not in done.stdout:
        problems.append(
            f"`collection acl list`: exit {done.status}, without the readers' grant"
        )
    done = manage_as("curator", "metadata", "set", cid, "key", "value")
    if done.status != 0:
        problems.append(
            f"`collection metadata set`: exit {done.status}: {last_line(done.stderr)}"
        )
    done = manage_as("curator", "metadata", "get", cid)
    if "value" not in done.stdout:
        problems.append(f"`collection metadata get`: exit {done.status}, without the value")
    done = manage_as("curator", "delete", cid)
    if done.status != 0 or get(curator, cid).status != 404:
        problems.append(f"`collection delete`: exit {done.status}: {last_line(done.stderr)}")
    return outcome(problems, "created, listed, changed, and deleted as test-curator")


RUN = {
    "users": users,
    "create": create_scenario,
    "visibility": visibility_scenario,
    "acl": acl_scenario,
    "tokens": tokens,
    "data": data,
    "client": client,
    "shares": shares,
    "expiry": expiry,
    "revoke": revoke_scenario,
    "manage": manage,
    "ownership": ownership,
    "cli": cli,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Make the run's users, groups, collections, and objects; run the
    selected scenarios; and remove what the run made."""
    test = Test(session)
    try:
        prepare(test)
    except (ApiError, issuer.IssuerError) as e:
        die(f"could not set up the suite's users and collections: {e}")
    try:
        for name in selected:
            try:
                status, note = RUN[name](test)
            except (ApiError, issuer.IssuerError) as e:
                status, note = FAIL, str(e)
            results.add(name, status, note)
    finally:
        finish(test)
