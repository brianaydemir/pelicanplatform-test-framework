"""Logging in through an identity provider (`-p auth-oidc`).

What every server does with a user that the provider vouches for,
through the OAuth2 authorization code flow that a browser follows
(testlib/oidc.py); what the servers make of the provider's claims, a
username and groups; and the registry's use of the provider's device
flow to learn who registers a prefix
(`pelican namespace register --with-identity`). The provider is the
`idp` service (framework/idp/idp.py), whose users are
framework/etc/idp-users.json.

  enabled            each server lists the modules it logs in through
                     the provider, and begins a login: a redirect to
                     the provider naming the server's client, its
                     callback on OIDC.ClientRedirectHostname, and a
                     state, kept in a session cookie too
  login              at each server, the provider's user logs in: a
                     user of the claims' username, no admin, found again
                     at the next login, whom the server's record links
                     to the provider's subject; the server traded the
                     code and asked the provider who the user is
  callback           a callback without the login's session, with a
                     forged state, or with a code already used is
                     refused, and logs no one in
  collision          a user whose preferred_username is `admin` becomes
                     a user named for their email, and no admin
  registry-identity  `pelican namespace register --with-identity`: the
                     registry has the provider's device flow prove who
                     registers, and records their identity
  admin              with origin-0 restarted to name a user in
                     Server.UIAdminUsers and take groups from the
                     provider: that user is its admin, as is one whose
                     claimed group is in Server.AdminGroups, and no
                     other; and once restored, the named user is not
  groups             with origin-0 so restarted: its issuer grants a
                     user what the templates give the groups the
                     provider claims, and the data they allow
  client             with origin-0 so restarted: `pelican object get`
                     acquires a token through the issuer, approved by
                     the provider's user, and reads what their group
                     allows

The restart gives origin-0 Issuer.GroupSource `oidc`, under which a
password login carries no groups, so the issuer, collections, and
accounts suites have the shape keep `internal`; it is restored
afterward. The objects go under /<namespace>/data/issuer/projects/<group>/,
where the templates grant the groups, and are removed afterward, as
are the run's prefix at the registry and its client of the issuer.
"""

import contextlib
import json
import os
import secrets
import subprocess  # nosec B404
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from testlib import (
    accounts,
    common,
    control,
    credentials,
    director,
    issuer,
    oidc,
    registry,
    stores,
    web,
)
from testlib.accounts import Api, ApiError
from testlib.common import first, warn
from testlib.credentials import PROTECTED
from testlib.issuer import ARCHIVE, DATA_SCOPES, IssuerError, User, data_scopes, project, scope
from testlib.oidc import IdpUser, LoginError
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line
from testlib.webapi import Server, servers

SCENARIOS = {
    "enabled": (
        "each server lists its OIDC-enabled modules, and begins a login: a redirect to the"
        " provider with the client, callback, and state"
    ),
    "login": (
        "at each server, the provider's user logs in: a user of the claims' username, no"
        " admin, the same again, linked to the provider's subject"
    ),
    "callback": (
        "a callback without its session, with a forged state, or with a used code is"
        " refused, and logs no one in"
    ),
    "collision": "a user whose preferred_username is `admin` becomes another user, and no admin",
    "registry-identity": (
        "`pelican namespace register --with-identity`: the registry proves the registrant by"
        " the provider's device flow, and records their identity"
    ),
    "admin": (
        "origin-0 restarted: Server.UIAdminUsers and a claimed group in Server.AdminGroups"
        " make the provider's users its admins; once restored, not"
    ),
    "groups": (
        "origin-0 restarted with Issuer.GroupSource oidc: the issuer grants the templates'"
        " scopes for the groups the provider claims, and the data they allow"
    ),
    "client": (
        "`pelican object get` acquires a token through the issuer, approved by the"
        " provider's user, and reads what their group allows"
    ),
}
# Every scenario: each presents only Pelican's tokens, its issuer's and
# its web API's, beside its setup's (see suites/__init__.py).
ONE_PROFILE = tuple(SCENARIOS)

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# The provider's users (framework/etc/idp-users.json), by what each is
# for.
MEMBER = "idp-member"  # in test-project-a
ADMIN = "idp-admin"  # in test-server-admins (Server.AdminGroups) and test-project-b
OWNER = "idp-owner"  # named in Server.UIAdminUsers by the restart
OUTSIDER = "idp-outsider"  # in no group
COLLIDER = "idp-collider"  # whose preferred_username is admin
PROJECT_A, PROJECT_B = "test-project-a", "test-project-b"
# The callback that a server names, on OIDC.ClientRedirectHostname
# (framework/config.d/instance/*/50-instance.yaml): localhost and its
# published port.
CALLBACK_HOST = "localhost"
# The restart's configuration layer.
RESTART_FILE = "90-oidc.yaml"
RESTART_CONFIG = f"Server:\n  UIAdminUsers:\n    - {OWNER}\nIssuer:\n  GroupSource: oidc\n"
# How long the federation may take to serve origin-0 again after a
# restart, and how long `pelican namespace register` may take.
SETTLE_WAIT = 150
REGISTER_TIMEOUT = 120.0
OBJECT_SIZE = 4096
TOKEN_LIFETIME = 3600


def row(case: str, problems: Sequence[str], note: str) -> Row:
    """Return case's row: FAIL with the first problem, or PASS with note."""
    return (case, FAIL, first(problems)) if problems else (case, PASS, note)


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test (no provider), or None."""
    if fed.oidc_issuer is None:
        return "the servers log in through no identity provider ('-p auth-oidc')"
    return None


@dataclass
class Test:
    """The provider, its users, the servers, and what the scenarios share.

    Attributes:
        issuer: The provider's issuer URL.
        users: The provider's users, by name.
        client_id: The servers' client at the provider.
        namespace: The first protected namespace exported, if any.
        registry_ids: The provider's users' IDs at the registry, by name.
    """

    session: Session
    issuer: str
    users: dict[str, IdpUser]
    servers: list[Server]
    client_id: str
    namespace: Optional[str]
    registry_ids: dict[str, str] = field(default_factory=dict[str, str])
    _admins: dict[str, Api] = field(default_factory=dict[str, Api])

    @property
    def fed(self) -> common.Federation:
        """The federation."""
        return self.session.fed

    @property
    def run(self) -> str:
        """The run's ID."""
        return self.session.run

    @property
    def origin(self) -> common.Origin:
        """The issuer origin, which the restart scenarios restart."""
        return self.fed.origins[0]

    @property
    def web(self) -> str:
        """The issuer origin's web URL."""
        return self.fed.origin_web_url

    def user(self, name: str) -> IdpUser:
        """Return the provider's user named name."""
        return self.users[name]

    def admin(self, server: Server) -> Api:
        """Return server's web API as its admin.

        The admin has agreed to its AUP.
        """
        if server.web_url not in self._admins:
            token = credentials.mint_admin(
                self.fed,
                self.session.path(f"tokens/oidc.admin.{server.name}"),
                credentials.local_issuer(self.fed, server.web_url),
                server.key,
            )
            found = Api(server.web_url, token)
            accounts.agree_to_aup(found)
            self._admins[server.web_url] = found
        return self._admins[server.web_url]

    def server_at(self, web_url: str) -> Server:
        """Return the server whose web URL is web_url."""
        return next(s for s in self.servers if s.web_url == web_url)

    def login(self, web_url: str, name: str) -> str:
        """Return a login cookie of the provider's user name at web_url."""
        return oidc.login(web_url, self.user(name))

    def record_of(self, server: Server, username: str) -> list[dict[str, Any]]:
        """Return server's records of the users named username.

        Its admin lists them.

        Raises:
            ApiError: The admin could not list them.
        """
        listed = accounts.must(
            self.admin(server).call("GET", accounts.USERS), f"listing {server.name}'s users"
        ).array
        found = (common.as_object(x) or {} for x in listed)
        return [u for u in found if u.get("username") == username]


# --------------------------------------------------------------------------
# enabled


def enabled(test: Test) -> list[Row]:
    """Check that each server lists its OIDC modules, and begins a login.

    At each server, /api/v1.0/auth/oauth must list each module it runs
    (listOIDCEnabledServersHandler in web_ui/authentication.go); and a
    GET of /api/v1.0/auth/oauth/login must answer 307 to the provider's
    authorization endpoint, asking for a code for the servers' client,
    to be sent back to the server's callback on
    OIDC.ClientRedirectHostname, with a state, which a session cookie
    keeps too (handleOAuthLogin).
    """
    rows: list[Row] = []
    for server in test.servers:
        problems: list[str] = []
        answer = web.request("GET", server.web_url + oidc.ENABLED)
        found = common.as_object(parsed(answer)) or {}
        listed = [str(x) for x in common.as_array(found.get("oidc_enabled_servers")) or []]
        if answer.status != 200:
            problems.append(f"GET {oidc.ENABLED}: {answer.describe()}")
        for role in sorted(server.roles):
            if role not in listed:
                problems.append(f"GET {oidc.ENABLED} lists {listed}, without {role}")
        try:
            started = oidc.begin(server.web_url)
        except LoginError as e:
            problems.append(str(e))
            rows.append(row(server.name, problems, ""))
            continue
        parts = urlsplit(started.authorize_url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        sent_to = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if sent_to != f"{test.issuer}/authorize":
            problems.append(f"the login sends the user to {sent_to}, not the provider")
        if query.get("response_type") != "code":
            problems.append(f"the login asks for response_type {query.get('response_type')!r}")
        if query.get("client_id") != test.client_id:
            problems.append("the login names another client than the servers'")
        callback_url = urlsplit(query.get("redirect_uri", ""))
        if callback_url.hostname != CALLBACK_HOST or callback_url.path != oidc.CALLBACK:
            problems.append(
                f"the login's callback is {query.get('redirect_uri')!r}, not"
                + f" https://{CALLBACK_HOST}:<port>{oidc.CALLBACK}"
            )
        if not query.get("state"):
            problems.append("the login carries no state")
        rows.append(
            row(server.name, problems, f"{', '.join(sorted(server.roles))}: to the provider")
        )
    return rows


def parsed(answer: web.Response) -> Any:
    """Return answer's body as JSON, or None if it is none."""
    try:
        return json.loads(answer.body) if answer.body else None
    except ValueError:
        return None


# --------------------------------------------------------------------------
# login


def login(test: Test) -> list[Row]:
    """Check that MEMBER logs in at each server through the provider.

    whoami must then name them by their preferred_username, as no admin;
    a second login must find the same user; the server's admin must list
    one such user, linked to the provider's subject and issuer; and the
    provider must have been asked for the code's tokens, as the servers'
    client, and for who the user is (its log).
    """
    rows: list[Row] = []
    member = test.user(MEMBER)
    for server in test.servers:
        problems: list[str] = []
        before = len(oidc.log_entries())
        try:
            cookie = test.login(server.web_url, MEMBER)
            again = test.login(server.web_url, MEMBER)
        except LoginError as e:
            rows.append(row(server.name, [str(e)], ""))
            continue
        me = oidc.whoami(server.web_url, cookie)
        if me.get("authenticated") is not True or me.get("user") != member.preferred_username:
            problems.append(f"whoami after logging in: {me}")
        elif me.get("role") != "user":
            problems.append(f"whoami says the role is {me.get('role')!r}, not user")
        later = oidc.whoami(server.web_url, again)
        if later.get("user") != me.get("user") or later.get("user_id") != me.get("user_id"):
            problems.append(f"a second login is another user: {later}")
        try:
            records = test.record_of(server, member.preferred_username)
        except ApiError as e:
            problems.append(str(e))
            records = []
        if len(records) != 1:
            problems.append(
                f"{server.name} has {len(records)} users named {member.preferred_username}"
            )
        elif records[0].get("sub") != member.sub or records[0].get("issuer") != test.issuer:
            problems.append(
                f"the user's record links to {records[0].get('sub')!r} at"
                + f" {records[0].get('issuer')!r}, not the provider's subject"
            )
        elif "registry" in server.roles:
            test.registry_ids[MEMBER] = str(records[0].get("id", ""))
        entries = oidc.log_entries()[before:]
        exchanges = oidc.count(
            entries,
            path="/token",
            grant_type="authorization_code",
            client_id=test.client_id,
            user=MEMBER,
        )
        asked = oidc.count(entries, path="/userinfo", user=MEMBER)
        if exchanges < 2 or asked < 2:
            problems.append(
                f"the provider logged {exchanges} code exchanges and {asked} userinfo requests"
                + " for two logins"
            )
        rows.append(row(server.name, problems, f"{member.preferred_username}, user"))
    return rows


# --------------------------------------------------------------------------
# callback


def callback(test: Test) -> list[Row]:
    """Check how the issuer origin takes a login's callback.

    A callback sent without its session cookie must be refused (400), as
    must one whose state is not the cookie's (handleOAuthCallback); with
    the cookie, it must log the user in; sent again, with the code used,
    it must fail, and log no one in.
    """
    web_url = test.web
    try:
        started = oidc.begin(web_url)
        sent = oidc.authorize(started.authorize_url, test.user(MEMBER))
    except LoginError as e:
        return [("", FAIL, str(e))]
    location = sent.header("Location")
    if sent.status != 302 or not location:
        return [("", FAIL, f"logging in at the provider: {sent.describe()}")]
    target = oidc.callback_at(web_url, location)
    rows: list[Row] = []

    answer = web.request("GET", target)
    problems: list[str] = []
    if answer.status != 400:
        problems.append(f"without the session cookie: {answer.describe()}, not 400")
    if answer.cookie("login"):
        problems.append("without the session cookie, a login cookie was set")
    rows.append(row("no-session", problems, "400"))

    parts = urlsplit(target)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    query["state"] = secrets.token_urlsafe(16)
    forged = f"{web_url}{parts.path}?" + "&".join(f"{k}={v}" for k, v in query.items())
    answer = oidc.finish(started.session, forged)
    problems = []
    if answer.status != 400:
        problems.append(f"with a forged state: {answer.describe()}, not 400")
    if answer.cookie("login"):
        problems.append("with a forged state, a login cookie was set")
    rows.append(row("forged-state", problems, "400"))

    answer = oidc.finish(started.session, target)
    problems = []
    if answer.status != 307 or not answer.cookie("login"):
        problems.append(f"with the session cookie: {answer.describe()}, with no login")
    again = oidc.finish(started.session, target)
    if again.status is None or again.status < 400:
        problems.append(f"the callback again, its code used: {again.describe()}, not an error")
    if again.cookie("login"):
        problems.append("the callback again, its code used, set a login cookie")
    rows.append(row("used-code", problems, f"logged in once; then {again.describe()}"))
    return rows


# --------------------------------------------------------------------------
# collision


def collision(test: Test) -> list[Row]:
    """Check that a user whose preferred_username is `admin` gets no admin.

    COLLIDER, whose preferred_username is `admin`, the built-in
    administrator's, logs in at the issuer origin: the server must walk
    to the next claim (Server.AutoEnrollUsernameClaims: their email) for
    the username (LookupOrBootstrapUser in database/collection.go), and
    give them no admin role, nor the admin's routes.
    """
    user = test.user(COLLIDER)
    try:
        cookie = test.login(test.web, COLLIDER)
    except LoginError as e:
        return [("", FAIL, str(e))]
    me = oidc.whoami(test.web, cookie)
    problems: list[str] = []
    if me.get("user") == "admin":
        problems.append("the provider's user became `admin`")
    elif me.get("user") != user.email:
        problems.append(f"whoami names the user {me.get('user')!r}, not their email")
    if me.get("role") != "user":
        problems.append(f"whoami says the role is {me.get('role')!r}, not user")
    api = oidc.api(test.web, cookie)
    try:
        accounts.agree_to_aup(api)
    except ApiError as e:
        problems.append(str(e))
    answer = api.call("GET", accounts.USERS)
    if answer.status not in (401, 403):
        problems.append(f"GET {accounts.USERS} as the user: {answer.describe()}, not refused")
    return [row("", problems, f"became {me.get('user')}, user")]


# --------------------------------------------------------------------------
# registry-identity


def registry_env(test: Test, key_dir: str) -> dict[str, str]:
    """Return an environment for `pelican namespace` to register with.

    It registers at the shape's registry, with the keys in key_dir
    alone, under a site name of the run's.
    """
    env = dict(test.session.env)
    env["PELICAN_ISSUERKEYSDIRECTORY"] = os.path.abspath(key_dir)
    env["PELICAN_ISSUERKEY"] = os.path.abspath(os.path.join(key_dir, "absent.jwk"))
    env["PELICAN_XROOTD_SITENAME"] = f"oidc-test-{test.run}"
    env["PELICAN_FEDERATION_REGISTRYURL"] = test.fed.registry_url
    return env


def register_with_identity(
    test: Test, env: dict[str, str], prefix: str, approver: IdpUser
) -> tuple[int, str, int, list[str]]:
    """Run `pelican namespace register --with-identity` for prefix.

    Approve the provider's device flow that it announces, by its
    verification URL, as approver.

    Returns:
        Its exit status, its output, how many flows were approved, and
        what went wrong approving.
    """
    problems: list[str] = []
    approvals = [0]
    with subprocess.Popen(  # nosec B603
        [
            test.session.pelican,
            "namespace",
            "register",
            "--prefix",
            prefix,
            "--with-identity",
        ],
        env=env,
        cwd=test.session.tmp,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        if proc.stdout is None:
            raise RuntimeError("the client's output is not piped")
        killer = threading.Timer(REGISTER_TIMEOUT, proc.kill)
        killer.start()
        lines: list[str] = []
        seen: set[str] = set()
        for line in proc.stdout:
            lines.append(line)
            code = oidc.user_code_in(line)
            if code and code not in seen:
                seen.add(code)
                answer = oidc.approve_device(test.issuer, code, approver)
                approvals[0] += 1
                if answer.status != 200:
                    problems.append(f"approving {code} at the provider: {answer.describe()}")
        status = proc.wait()
        killer.cancel()
    if status < 0:
        problems.append(f"the client was killed after {REGISTER_TIMEOUT:.0f}s")
    return status, "".join(lines), approvals[0], problems


def registry_identity(test: Test) -> list[Row]:
    """Check `pelican namespace register --with-identity` of a run's prefix.

    The registry starts the provider's device flow and hands the client
    the verification URL (cliRegisterNamespace in registry/registry.go),
    which the test approves as MEMBER; the client then registers, and
    the registration must record MEMBER's identity, the provider's
    userinfo (sub, preferred_username), and name the registry's user of
    that identity as its owner, where `login` found one. Then
    `pelican namespace delete` removes it.
    """
    fed = test.fed
    if not fed.registry_url:
        return [("", SKIP, "a standalone origin has no registry")]
    key_dir = test.session.path("oidc-registry-keys")
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    key = registry.new_key(os.path.join(key_dir, "key.pem"))
    env = registry_env(test, key_dir)
    prefix = f"/oidc-test-{test.run}"
    member = test.user(MEMBER)
    status, output, approvals, problems = register_with_identity(test, env, prefix, member)
    if status != 0:
        problems.append(f"register: exit {status}: {last_line(output)}")
    if approvals != 1:
        problems.append(f"the client announced {approvals} verification URLs, not one")
    stored = web.request("GET", f"{fed.registry_url}{registry.REGISTRY}{prefix}")
    found = common.as_object(registry.parse(stored.body)) or {}
    if stored.status != 200 or not found:
        problems.append(f"GET {registry.REGISTRY}{prefix}: {stored.describe()}, not registered")
    else:
        if registry.thumbprints(found.get("pubkey")) != registry.key_thumbprints([key]):
            problems.append(f"{prefix} is not registered with the client's key")
        identity = (
            common.as_object(registry.parse(str(found.get("identity", "")).encode())) or {}
        )
        if identity.get("sub") != member.sub:
            problems.append(
                f"the registration's identity is {found.get('identity')!r}, not {MEMBER}'s"
            )
        elif identity.get("preferred_username") != member.preferred_username:
            problems.append(f"the registration's identity names no {member.preferred_username}")
        owner = str(registry.admin_metadata(found).get("user_id") or "")
        known = test.registry_ids.get(MEMBER)
        if known and owner != known:
            problems.append(
                f"the registration's owner is {owner!r}, not the registry's {MEMBER}"
            )
    code, out, err = test.session.pelican_cmd(
        "namespace", "delete", "--prefix", prefix, env=env
    )
    if code != 0:
        problems.append(f"delete: exit {code}: {last_line(err or out)}")
    elif web.request("GET", f"{fed.registry_url}{registry.REGISTRY}{prefix}").status == 200:
        problems.append(f"{prefix} is still registered after delete")
    note = f"{prefix}: registered as {MEMBER}" + (
        ", owned by the registry's user of that identity"
        if test.registry_ids.get(MEMBER)
        else ""
    )
    return [row("", problems, note)]


# --------------------------------------------------------------------------
# The restart: admin, groups, and client.


def unrestartable(test: Test) -> Optional[str]:
    """Return why origin-0 cannot be restarted here, or None if it can."""
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


def no_issuer(test: Test) -> Optional[str]:
    """Return why the issuer origin's embedded issuer cannot grant anything.

    Return None if it can.
    """
    fed = test.fed
    if fed.external_issuer:
        return "the exports trust the external issuer ('-p auth-external-issuer')"
    if not fed.embedded_issuer:
        return "the origins' embedded issuer is off (ORIGIN_ENABLE_ISSUER=false)"
    if test.namespace is None:
        return "no protected namespace is exported"
    if (
        "Writes" not in fed.exports[test.namespace]
        or "DirectReads" not in fed.exports[test.namespace]
    ):
        return f"/{test.namespace} takes no writes from direct clients"
    return None


def settle(test: Test, rel: str) -> Optional[str]:
    """Wait until the federation serves the issuer origin again.

    Wait up to SETTLE_WAIT seconds, until it answers a HEAD of rel in
    the namespace, every director names it for the path, and every
    federated cache answers a HEAD of it.

    Returns:
        Why it does not, or None if it does.
    """
    namespace = test.namespace or ""
    token = test.session.setup_token(namespace)
    path = f"/{namespace}/{rel}"

    def unserved() -> Optional[str]:
        answer = web.request("HEAD", stores.url(test.origin, namespace, rel), token=token)
        if answer.status != 200:
            return f"{test.origin.svc}: HEAD of the object got {answer.describe()}"
        for url in test.fed.directors:
            where = director.ask(url, "origin", path, token=token)
            if not where.names(director.host(test.origin.url)):
                return (
                    f"{director.host(url)} does not name {test.origin.svc}: {where.describe()}"
                )
        for cache in test.fed.federated_caches:
            answer = web.request("HEAD", f"{cache.url}{path}", token=token)
            if answer.status != 200:
                return f"{cache.svc}: HEAD of the object got {answer.describe()}"
        return None

    deadline = time.time() + SETTLE_WAIT
    why = unserved()
    while why and time.time() < deadline:
        time.sleep(5)
        why = unserved()
    return f"after {SETTLE_WAIT}s, {why}" if why else None


@dataclass
class Restarted:
    """What the restart scenarios share.

    Attributes:
        objects: The objects in the groups' project prefixes, by group:
            each one's path in the namespace, and its bytes.
        client: The issuer's client, once `groups` makes it.
    """

    objects: dict[str, tuple[str, bytes]]
    client: Optional[issuer.Client] = None


def seed(test: Test) -> Restarted:
    """Put an object in each group's project prefix at every origin.

    A token for the group may then read it (testlib.issuer.project()).

    Returns:
        What was put, by group.

    Raises:
        ApiError: An object could not be put in place.
    """
    namespace = test.namespace or ""
    token = test.session.setup_token(namespace)
    caps = test.fed.exports[namespace]
    found: dict[str, tuple[str, bytes]] = {}
    for group in (PROJECT_A, PROJECT_B):
        rel = f"{project(group)}/oidc-{test.run}"
        data = os.urandom(OBJECT_SIZE)
        errors = stores.make_dirs(test.fed.origins, namespace, project(group), token)
        errors += stores.seed_everywhere(test.fed.origins, namespace, rel, data, token, caps)
        if errors:
            raise ApiError(f"could not put {rel} in place: {first(errors)}")
        found[group] = (rel, data)
    return Restarted(found)


def unseed(test: Test, made: Restarted) -> None:
    """Remove what seed() put in place."""
    namespace = test.namespace or ""
    token = test.session.setup_token(namespace)
    for rel, _ in made.objects.values():
        for origin in stores.unique(test.fed.origins):
            error = stores.delete(origin, namespace, rel, token)
            if error:
                warn(error)


def admin_rows(test: Test, when: str) -> list[Row]:
    """Check who is origin-0's admin, when `restarted` or `restored`.

    With origin-0 restarted: OWNER, named in Server.UIAdminUsers, must
    be its admin by whoami, and may list its users; ADMIN, whose claimed
    group test-server-admins is in Server.AdminGroups, likewise
    (RecordAssertedGroups in web_ui/oauth2_client.go); MEMBER must be no
    admin, and refused the users. Once restored, OWNER must be no admin.
    """
    rows: list[Row] = []
    if when == "restored":
        cases = [(OWNER, False)]
    else:
        cases = [(OWNER, True), (ADMIN, True), (MEMBER, False)]
    for name, expect_admin in cases:
        problems: list[str] = []
        try:
            cookie = test.login(test.web, name)
        except LoginError as e:
            rows.append(row(f"{when} {name}", [str(e)], ""))
            continue
        me = oidc.whoami(test.web, cookie)
        role = me.get("role")
        if (role == "admin") != expect_admin:
            problems.append(f"whoami says the role is {role!r}")
        api = oidc.api(test.web, cookie)
        with contextlib.suppress(ApiError):
            accounts.agree_to_aup(api)
        answer = api.call("GET", accounts.USERS)
        if expect_admin and answer.status != 200:
            problems.append(f"GET {accounts.USERS}: {answer.describe()}, not 200")
        if not expect_admin and answer.status not in (401, 403):
            problems.append(f"GET {accounts.USERS}: {answer.describe()}, not refused")
        rows.append(row(f"{when} {name}", problems, "admin" if expect_admin else "user"))
    return rows


def expected_scopes(group: str) -> frozenset[str]:
    """Return the data scopes the templates give a member of group alone.

    The templates (framework/config.d/base/40-origin.yaml) give read and
    create in its project, and read in the archive.
    """
    return frozenset(
        {scope("read", project(group)), scope("create", project(group)), scope("read", ARCHIVE)}
    )


def group_rows(test: Test, made: Restarted) -> list[Row]:
    """Check what the issuer grants for the groups that the provider claims.

    With origin-0 taking groups from the provider: MEMBER's device flow
    must be granted exactly the templates' scopes for test-project-a,
    and its token name the group; ADMIN's those for test-project-b;
    OUTSIDER's none for data; and MEMBER's token must read the object in
    test-project-a's project, at the origin and through the federation,
    and be refused test-project-b's. The flows use made.client, which
    this makes.
    """
    rows: list[Row] = []
    namespace = test.namespace or ""
    admin_token = test.admin(test.server_at(test.web)).token or ""
    try:
        made.client = issuer.create_client(
            test.web,
            f"/{namespace}",
            admin_token,
            name=f"pelican-test-framework oidc {test.run}",
        )
    except IssuerError as e:
        return [("", FAIL, f"creating a client of the issuer: {e}")]
    tokens: dict[str, str] = {}
    for name, group in ((MEMBER, PROJECT_A), (ADMIN, PROJECT_B)):
        problems: list[str] = []
        try:
            cookie = test.login(test.web, name)
            granted = issuer.device_tokens(made.client, User(cookie=cookie), DATA_SCOPES)
        except (LoginError, IssuerError) as e:
            rows.append(row(name, [str(e)], ""))
            continue
        tokens[name] = granted.access
        got = data_scopes(granted.access)
        want = expected_scopes(group)
        if got != want:
            problems.append(f"granted {sorted(got)}, not {sorted(want)}")
        if group not in issuer.groups(granted.access):
            problems.append(
                f"the token's wlcg.groups {sorted(issuer.groups(granted.access))} lack {group}"
            )
        rows.append(row(name, problems, f"{group}: {len(want)} scopes"))
    problems = []
    try:
        cookie = test.login(test.web, OUTSIDER)
        granted = issuer.device_tokens(made.client, User(cookie=cookie), DATA_SCOPES)
    except LoginError as e:
        problems.append(str(e))
        note = ""
    except IssuerError as e:
        note = f"refused: {e}"
    else:
        got = data_scopes(granted.access)
        if got:
            problems.append(f"granted {sorted(got)} to a user in no group")
        note = "a token of no data scope"
    rows.append(row(OUTSIDER, problems, note))

    problems = []
    tok = tokens.get(MEMBER)
    if tok is None:
        return rows + [("data", FAIL, f"{MEMBER} got no token")]
    rel_a, data_a = made.objects[PROJECT_A]
    rel_b, _ = made.objects[PROJECT_B]
    answer = web.request("GET", stores.url(test.origin, namespace, rel_a), token=tok)
    if answer.status != 200 or answer.body != data_a:
        problems.append(
            f"GET of {PROJECT_A}'s object at {test.origin.svc}: {answer.describe()}"
        )
    answer = web.request("GET", stores.url(test.origin, namespace, rel_b), token=tok)
    if answer.status not in (401, 403):
        problems.append(
            f"GET of {PROJECT_B}'s object at {test.origin.svc}: {answer.describe()}, not refused"
        )
    token_file = test.session.path(f"tokens/oidc.{MEMBER}")
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(tok)
    target = test.session.path(f"oidc-{MEMBER}-get")
    code, out, err = test.session.pelican_cmd(
        "object", "get", "--token", token_file, f"{test.fed.url}/{namespace}/{rel_a}", target
    )
    if code != 0:
        problems.append(
            f"`pelican object get` with the token: exit {code}: {last_line(err or out)}"
        )
    elif common.sha256(target) != common.digest(data_a):
        problems.append("`pelican object get` with the token: the file is not the object")
    rows.append(row("data", problems, f"{PROJECT_A}'s object read, {PROJECT_B}'s refused"))
    return rows


def client_rows(test: Test, made: Restarted) -> list[Row]:
    """Check `pelican object get` of test-project-a's object, with no token.

    The client registers itself with the issuer and starts a device
    flow, which the test approves as MEMBER, logged in through the
    provider; the client must then get the object, and the file be the
    object.
    """
    namespace = test.namespace or ""
    rel, data = made.objects[PROJECT_A]
    try:
        cookie = test.login(test.web, MEMBER)
    except LoginError as e:
        return [("", FAIL, str(e))]
    user = User(cookie=cookie)
    store = test.session.path("oidc-client-credentials")
    env = issuer.client_env(test.session.env, store)
    target = test.session.path("oidc-client-get")
    with contextlib.suppress(FileNotFoundError):
        os.remove(target)
    done = issuer.run_approving(
        test.session.pelican,
        ["object", "get", f"{test.fed.url}/{namespace}/{rel}", target],
        env,
        test.session.tmp,
        lambda issuer_url, code: issuer.approve(issuer_url, code, user),
    )
    problems = list(done.problems)
    if done.status != 0:
        problems.append(f"exit {done.status}: {last_line(done.stderr or done.stdout)}")
    if done.approvals < 1:
        problems.append("the client started no device flow")
    if common.sha256(target) != common.digest(data):
        problems.append("the file is not the object")
    return [row("", problems, f"{done.approvals} flow(s) approved as {MEMBER}")]


def run_restarted(
    test: Test, selected: Sequence[str], record: Callable[[str, Row], None]
) -> None:
    """Run the selected restart scenarios, with origin-0 restarted.

    origin-0 is restarted with RESTART_CONFIG, and afterward restored.
    Each row goes to record, with its scenario's name.
    """
    wanted = [s for s in ("admin", "groups", "client") if s in selected]
    if not wanted:
        return
    why = unrestartable(test)
    if why:
        for scenario in wanted:
            record(scenario, ("", SKIP, f"needs origin-0 restarted, but {why}"))
        return
    lacking = no_issuer(test)
    for scenario in ("groups", "client"):
        if scenario in wanted and lacking:
            record(scenario, ("", SKIP, lacking))
    wanted = [s for s in wanted if s == "admin" or not lacking]
    if not wanted:
        return
    made: Optional[Restarted] = None
    try:
        if not lacking:
            made = seed(test)
    except (ApiError, stores.Unreadable) as e:
        for scenario in wanted:
            record(scenario, ("", FAIL, str(e)))
        return
    print(f"\nRestarting {test.origin.svc} with:\n{RESTART_CONFIG}")
    done: set[str] = set()
    try:
        with control.restart_with(test.origin.svc, {RESTART_FILE: RESTART_CONFIG}):
            why = settle(test, made.objects[PROJECT_A][0]) if made else None
            for scenario in wanted:
                if why:
                    record(scenario, ("", FAIL, f"with {test.origin.svc} restarted, {why}"))
                    done.add(scenario)
                    continue
                try:
                    if scenario == "admin":
                        rows = admin_rows(test, "restarted")
                    elif scenario == "groups" and made:
                        rows = group_rows(test, made)
                    elif made:
                        rows = client_rows(test, made)
                    else:
                        rows = []
                except Exception as e:  # pylint: disable=broad-exception-caught
                    traceback.print_exc()
                    rows = [("", FAIL, f"{type(e).__name__}: {e}")]
                for found in rows:
                    record(scenario, found)
                done.add(scenario)
    except control.ControlError as e:
        for scenario in wanted:
            if scenario not in done:
                record(scenario, ("", FAIL, f"restarting {test.origin.svc}: {e}"))
        warn(f"restarting {test.origin.svc}: {e}")
    if made:
        why = settle(test, made.objects[PROJECT_A][0])
        if why:
            warn(f"with {test.origin.svc} restored, {why}")
        if made.client is not None:
            admin_token = test.admin(test.server_at(test.web)).token or ""
            error = issuer.delete_client(
                test.web, f"/{test.namespace}", admin_token, made.client.client_id
            )
            if error:
                warn(error)
        unseed(test, made)
    if "admin" in wanted and "admin" in done:
        for found in admin_rows(test, "restored"):
            record("admin", found)


# --------------------------------------------------------------------------


# The scenarios that need no restart: each takes the Test, and returns
# its rows.
RUN: dict[str, Callable[[Test], list[Row]]] = {
    "enabled": enabled,
    "login": login,
    "callback": callback,
    "collision": collision,
    "registry-identity": registry_identity,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, those that restart origin-0 last."""
    fed = session.fed
    issuer_url = fed.oidc_issuer or ""
    test = Test(
        session,
        issuer_url,
        oidc.users(issuer_url),
        servers(fed),
        oidc.client_id(),
        next((ns for ns in PROTECTED if ns in fed.exports), None),
    )

    def record(scenario: str, found: Row) -> None:
        case, status, note = found
        results.add(f"{scenario} {case}".strip(), status, note)

    print(f"Logging in through {issuer_url} at {len(test.servers)} server(s)\n")
    for name in selected:
        if name in RUN:
            print(f"  {name} ...")
            try:
                rows = RUN[name](test)
            except (ApiError, stores.Unreadable, IssuerError) as e:
                rows = [("", FAIL, str(e))]
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                rows = [("", FAIL, f"{type(e).__name__}: {e}")]
            for found in rows:
                record(name, found)
    run_restarted(test, selected, record)
