"""The origins' embedded OAuth2 issuer: what it grants of what a client
asks for, through each of its flows, and how its refresh tokens behave.
The suite needs the embedded issuer (ORIGIN_ENABLE_ISSUER, on by
default) and a protected namespace, and skips with the issuer off or the
external one.

What a user may be granted comes from three places, which the suite
sets up so that each user gets something different: the issuer's
authorization templates (Issuer.AuthorizationTemplates in
framework/config.d/base/40-origin.yaml, whose prefixes are within each
issuer's namespace, as its tokens' scopes are), the user's collections
and shares (see the collections suite), and under
`-p origin-export-templates`, the /protected-b export's templates of
its own, which replace the global ones there. The templates grant:

  /data/issuer/home/$USER         read, create, and modify, to
                                  test-analyst and test-member
  /data/issuer/projects/$GROUP    read and create, to the groups
                                  test-project-a and test-project-b
  /data/issuer/archive            read, to any group named test-project-*
  /data/issuer/export             read, to test-stranger; /protected-b
                                  only, under `-p origin-export-templates`

The users are records of the issuer origin (origin-0, or `fed`), which
its admin creates once and later runs reuse: test-analyst, test-member,
and test-stranger, and the collections suite's test-curator, a
collection administrator. test-member is in both groups, which the
admin creates once, eligible to match templates. Each run, test-stranger
makes a group of their own named like the template's,
test-project-rogue-<run>, which may not match a template until the
admin makes it eligible. Each user logs in: the admin mints them a
password invite, they set a password with it, and their login's cookie
stands for them, carrying the groups they are in (Issuer.GroupSource),
as a browser's would. Each run also makes a collection, gamma, under
/<namespace>/data/issuer/collections/<run>/, which test-stranger may
read and test-member write, and which test-stranger shares part of
(gamma/shared) with test-analyst; and an object in each prefix above,
with decoy/obj in none. So, within the namespace:

  test-analyst   home/test-analyst; the share, read-only
  test-member    home/test-member; the projects, read and create;
                 archive, read; gamma
  test-stranger  gamma, read-only; archive once the rogue group may match

The admin creates the suite's clients through the issuer's admin API
(testlib/issuer.py): one for the device flow, one for the code flow,
one allowed only reads and no refresh token, and one for token
exchange; and the suite also uses the client every issuer seeds for
browsers, which has no secret. The scenarios:

  discovery   each issuer's discovery document names itself and its
              endpoints, and its JWKS holds the key that signs its
              tokens
  templates   what each user is granted of everything: the templates'
              scopes, their collections', and their share's, no more;
              the groups that earned them; and that a group may match a
              template only once the admin makes it eligible
  requests    what the issuer grants of each request, through the device
              flow: a scope it allows, as asked; a narrower one, as
              asked; a broader one, as the allowed scopes under it; an
              action, a path, or a case it does not allow, nothing; one
              that climbs with `..`, as it resolves; the standard
              scopes, always; and a refresh token only with offline_access
  code        the same through the authorization code flow, with PKCE,
              which refuses a scope the client may not carry instead of
              dropping it; a wrong verifier, a replayed code, and a
              wrong redirect URI; and the browsers' public client
  clients     a client gets no more than it may carry, and no refresh
              token without the grant; a client without the device
              grant, a wrong secret, an unknown client
  device      the device flow's own rules: the consent page, polling
              before approval and too fast, a code redeemed by another
              client, twice, or approved twice, a denied flow, an
              approval by nobody, an unknown code
  data        what the tokens allow: reads and writes straight at every
              origin, and reads through the federation and at each cache
  refresh     a refresh token works, once, and the tokens it gives work
  revoked     what a refresh token gives once a grant is revoked; a
              revoked token; a refresh token reused past its grace period
  exchange    token exchange narrows a token to what is asked and the
              client may carry, and refuses another audience
  expiry      an expired access token is refused everywhere, and a
              refresh token renews it until it expires itself; needs
              `-p origin-short-tokens`
  client      `pelican object get` acquires a token from the issuer,
              approved as the user, and reuses it; under
              `-p origin-short-tokens`, refreshes it once it expires
  isolation   each namespace's issuer is a trust domain of its own: its
              clients, tokens, revocations, and collections are nothing
              to another's, nor is a namespace that begins like it
  device-csrf the verification page refuses a decision whose CSRF token
              is not in both its cookie and the body
  admin-clients
              only the admin may use the admin API for clients, which
              shows no secret, refuses a grant it does not know, and
              narrows or deletes a client at once
  cors-and-pkce
              another site may read no endpoint that takes credentials,
              and the public client may not run the code flow without
              PKCE
  code-expiry device and authorization codes expire unredeemed; needs
              `-p origin-short-tokens`
  registration
              a client that registers itself (RFC 7591) gets only the
              device and refresh grants, a redirect URI on this host and
              a short name, and its first user; and manages itself
              (RFC 7592) only with its registration access token

`registration` registers a client, as the Pelican client does in the
`client` scenario and the collections suite; the issuer allows an
address five registrations, then one a minute, so it comes last, and
waits once for the limit if it must.

The run's collection, group, clients, and objects are removed after the
suite; the users and their groups stay.
"""

import dataclasses
import json
import os
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from suites import collections
from testlib import accounts, common, credentials, issuer, stores, web
from testlib.accounts import Api, ApiError, must
from testlib.common import die, warn
from testlib.credentials import PROTECTED
from testlib.issuer import IssuerError, Tokens, User
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "discovery": "each issuer's discovery document names itself, and its JWKS its signing key",
    "templates": "each user is granted the templates', collections', and share's scopes",
    "requests": "what the device flow grants of a request: allowed, narrower, broader, or not",
    "code": "the same through the code flow, which refuses what a client may not ask for",
    "clients": "a client gets no more than it may carry; the wrong client or secret, nothing",
    "device": "the device flow: consent, polling, denial, and a code redeemed wrongly",
    "data": "what the tokens allow at every origin, through the federation, and at each cache",
    "refresh": "a refresh token works, once, and the tokens it gives work",
    "revoked": "a refresh after a revoked grant; a revoked token; a refresh token reused late",
    "exchange": "token exchange narrows a token to what is asked and the client may carry",
    "expiry": "an expired access token is refused; a refresh token renews it until it expires",
    "client": "`pelican object get` acquires a token, reuses it, and refreshes it once expired",
    "isolation": "each namespace's issuer: its clients, tokens, and collections are its own",
    "device-csrf": "the verification page refuses a decision with a forged CSRF token",
    "admin-clients": "the admin API for clients: the admin's alone, no secrets, changes obeyed",
    "cors-and-pkce": "no credentialed endpoint for another site; the public client needs PKCE",
    "code-expiry": "device and authorization codes expire unredeemed",
    "registration": "a client that registers itself: its grants, URIs, name, user, and config",
}

ROLES = ("analyst", "member", "stranger")
USERS = {"analyst": "test-analyst", "member": "test-member", "stranger": "test-stranger"}
CURATOR = "test-curator"
# The groups a template names, which the admin creates, eligible to match.
GROUPS = ("test-project-a", "test-project-b")
# How test-stranger names the group of their own each run.
ROGUE = "test-project-rogue"

# Where the suite's objects and collections live, within the namespace.
BASE = "data/issuer"
ARCHIVE = f"{BASE}/archive"
EXPORT = f"{BASE}/export"
DECOY = f"{BASE}/decoy"

RWC = ("read", "modify", "create")
R, M, C = "storage.read", "storage.modify", "storage.create"
# What a user asks for who wants everything they may have.
DATA_SCOPES = ("storage.read:/", "storage.modify:/", "storage.create:/", "share.access:/")

# How a server may refuse.
REFUSED = (401, 403, 404)
OBJECT_SIZE = 4096

# `expiry` waits for the access token to expire, so it runs only where
# that is soon; and how long past its expiry the origins may still
# accept it, allowing for clock skew between containers.
SHORT = 120
SKEW = 90
# Longer than this, the refresh token's grace period is not waited out,
# and a refresh token's expiry, which prepare() leaves tokens for, not
# waited for.
SHORT_GRACE = 60
DOOMED_WAIT = 600
# The device flow's least polling interval (RFC 8628), which the issuer
# enforces.
POLL_INTERVAL = 5.2
# How long a client may take to acquire its token and finish.
CLIENT_TIMEOUT = 180.0
# How long past a code's expiry `code-expiry` redeems it, allowing for
# clock skew between containers.
CODE_MARGIN = 3
# What the verification page says to a user who approves a flow for a
# client that registered itself and is bound to another user.
BOUND_ELSEWHERE = "This client is registered to a different user"
# How long the issuer takes to allow another registration from an
# address that has used up its five (one a minute).
REGISTRATION_WAIT = 61


def username(role: str) -> str:
    """The user who plays role."""
    return USERS.get(role, f"test-{role}")


def personal(role: str) -> str:
    """The ACL target that grants to role's user alone."""
    return f"user-{username(role)}"


def home(role: str) -> str:
    """role's user's home prefix, within the namespace."""
    return f"{BASE}/home/{username(role)}"


def project(group: str) -> str:
    """A group's project prefix, within the namespace."""
    return f"{BASE}/projects/{group}"


def scope(kind: str, rel: str) -> str:
    """The storage scope for kind (read, modify, or create) of rel, a
    prefix within the namespace."""
    return f"storage.{kind}:/{rel}"


def data_scopes(tok: str) -> frozenset[str]:
    """A token's scopes for data: storage.* and share.access."""
    return frozenset(s for s in issuer.scopes(tok) if s.startswith(("storage.", "share.")))


def outcome(problems: Sequence[str], note: str) -> tuple[str, str]:
    """A row: the first problem, or note if there were none."""
    return (FAIL, common.first(problems)) if problems else (PASS, note)


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no embedded issuer, or no protected
    namespace."""
    return collections.skip(fed)


class Test:
    """The issuer origin and its namespace, the run's users, logins,
    groups, collection, share, clients, and objects."""

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
        self.lifetimes = fed.issuer_lifetimes
        self.server_token = session.token(self.namespace)
        self.gamma_rel = f"{BASE}/collections/{self.run}/gamma"
        self.shared_rel = f"{self.gamma_rel}/shared"
        self.ids: dict[str, str] = {}  # the users' IDs, by role
        self.groups: dict[str, str] = {}  # the groups' IDs, by name
        self.logins: dict[str, User] = {}  # each user's login, by role
        self.gamma = ""  # the collection's ID
        self.share = ""  # the share's ID, or "" if the origin refuses shares
        self.data: dict[str, bytes] = {}  # the objects' bytes, by path within the namespace
        self.seeded: list[str] = []  # the namespaces the objects went in
        self.doomed: Optional[tuple[Tokens, float]] = None  # tokens left to expire, and when
        self._web_tokens: dict[str, str] = {}
        self.clients: dict[tuple[str, str], issuer.Client] = {}  # by kind and namespace
        self._minted = 0

    def web_token(self, role: str) -> str:
        """A token that the issuer origin's web API takes as role's user."""
        if role not in self._web_tokens:
            self._web_tokens[role] = credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/issuer-web.{role}"),
                credentials.local_issuer(self.fed, self.web),
                self.fed.origin_key,
                username(role),
                lifetime=3600,
            )
        return self._web_tokens[role]

    def api(self, role: str) -> Api:
        """The issuer origin's web API, as role's user, by bearer token."""
        return Api(self.web, self.web_token(role))

    def user(self, role: str) -> User:
        """role's user, as the issuer knows them: by their login, if they
        have one, else by a bearer token."""
        return self.logins.get(role) or User(token=self.web_token(role))

    def client(self, kind: str, namespace: Optional[str] = None) -> issuer.Client:
        """The suite's client of kind (see CLIENTS) of the issuer origin's
        issuer of namespace (the suite's by default)."""
        namespace = namespace or self.namespace
        key = (kind, namespace)
        if key not in self.clients:
            if kind == "public":
                self.clients[key] = issuer.client_of(
                    self.web,
                    f"/{namespace}",
                    issuer.PUBLIC_CLIENT,
                    "",
                    f"{self.web}/view/origin/client/",
                )
            else:
                grants, scopes, redirects = CLIENTS[kind]
                self.clients[key] = issuer.create_client(
                    self.web,
                    f"/{namespace}",
                    self.web_token("admin"),
                    grants,
                    scopes,
                    redirects,
                    name=f"pelican-test-framework {kind} {self.run}",
                )
        return self.clients[key]

    def tokens(
        self,
        role: str,
        wanted: Sequence[str],
        kind: str = "device",
        namespace: Optional[str] = None,
    ) -> Tokens:
        """Tokens for namespace (the suite's by default) from its issuer
        through the device flow, as role's user, asking for wanted."""
        return issuer.device_tokens(self.client(kind, namespace), self.user(role), wanted)

    def token_file(self, role: str) -> str:
        """A fresh token for role's user, of everything they may have, in
        a file for `--token`."""
        self._minted += 1
        path = self.session.path(f"tokens/issuer.{role}.{self._minted}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.tokens(role, DATA_SCOPES).access)
        return path

    def allowed(self, role: str) -> frozenset[str]:
        """The data scopes role's user may be granted in the namespace:
        the templates', their collection's, and their share's."""
        found: set[str] = set()
        if role in ("analyst", "member"):
            found |= {scope(k, home(role)) for k in RWC}
        if role == "member":
            for group in GROUPS:
                found |= {scope("read", project(group)), scope("create", project(group))}
            found.add(scope("read", ARCHIVE))
            found |= {scope(k, self.gamma_rel) for k in RWC}
        if role == "stranger":
            found.add(scope("read", self.gamma_rel))
        if role == "analyst" and self.share:
            found |= {scope("read", self.shared_rel), f"share.access:/{self.share}"}
        return frozenset(found)

    def object_url(self, origin: common.Origin, rel: str) -> str:
        """Where origin serves the object at rel within the namespace."""
        return stores.url(origin, self.namespace, rel)

    def cache_url(self, cache: common.Cache, rel: str) -> str:
        """Where cache serves the object at rel within the namespace."""
        return f"{cache.url}/{self.namespace}/{quote(rel)}"

    def federation_url(self, rel: str) -> str:
        """The object at rel's URL through the federation."""
        return f"{self.fed.url}/{self.namespace}/{rel}"


# The clients the admin creates: each kind's grants, scopes, and
# redirect URIs.
CLIENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "device": ((issuer.DEVICE_GRANT, issuer.REFRESH_GRANT), issuer.CLIENT_SCOPES, ()),
    "code": (
        (issuer.CODE_GRANT, issuer.REFRESH_GRANT),
        issuer.CLIENT_SCOPES,
        (issuer.REDIRECT_URI,),
    ),
    "narrow": ((issuer.DEVICE_GRANT,), ("openid", "storage.read:/"), ()),
    "exchange": (
        (issuer.EXCHANGE_GRANT, issuer.REFRESH_GRANT),
        ("storage.read:/", "offline_access"),
        (),
    ),
}


# --------------------------------------------------------------------------
# The fixture.


def sweep(test: Test) -> None:
    """Remove what earlier runs left: their collections under the suite's
    directory, and their rogue groups."""
    admin = test.api("admin")
    directory = f"/{test.namespace}/{BASE}/collections/"
    for row in collections.objects(admin.call("GET", collections.COLLECTIONS).array):
        namespace = str(row.get("namespace", ""))
        if namespace.startswith(directory) and not namespace.startswith(
            f"/{test.namespace}/{test.gamma_rel}"
        ):
            gone = collections.delete(admin, str(row.get("id")))
            if not gone.ok:
                warn(f"could not remove collection {row.get('name')}: {gone.describe()}")
    for row in collections.objects(admin.call("GET", accounts.GROUPS).array):
        name = str(row.get("name", ""))
        if name.startswith(f"{ROGUE}-") and name != f"{ROGUE}-{test.run}":
            failed = accounts.delete_group(admin, str(row.get("id")))
            if failed:
                warn(f"could not remove group {name}: {failed}")


def log_in(test: Test, role: str) -> User:
    """Log role's user in, with a password of this run's that they set
    through an invite the admin mints: their login."""
    invite = accounts.password_invite(test.api("admin"), test.ids[role])
    password = secrets.token_urlsafe(12)
    accounts.set_password(test.web, invite, password)
    return User(cookie=accounts.login(test.web, username(role), password))


def prepare(test: Test) -> None:
    """Make the run's users and groups (once), logins, collection, share,
    objects, and the tokens that `expiry` leaves to expire. The admin,
    and each user, agree to the origin's AUP first, without which it
    refuses them its user and group routes."""
    admin = test.api("admin")
    accounts.agree_to_aup(admin)
    sweep(test)
    for role in (*ROLES, "curator"):
        test.ids[role] = accounts.ensure_user(admin, username(role))
        accounts.agree_to_aup(test.api(role))
    for name in GROUPS:
        test.groups[name] = accounts.ensure_group(admin, name, eligible=True)
        must(accounts.set_eligible(admin, test.groups[name], True), f"making {name} eligible")
        accounts.add_member(admin, test.groups[name], test.ids["member"])
    stranger = test.api("stranger")
    rogue = f"{ROGUE}-{test.run}"
    test.groups[rogue] = accounts.create_group(stranger, rogue)
    accounts.add_member(stranger, test.groups[rogue], test.ids["stranger"])
    # A login's cookie carries the groups the user is in as they log in.
    for role in ROLES:
        test.logins[role] = log_in(test, role)
    curator = test.api("curator")
    made = must(
        collections.create(
            curator, "gamma", f"/{test.namespace}/{test.gamma_rel}", sharing=True
        ),
        "creating gamma",
    )
    test.gamma = str(made.object.get("id", ""))
    for role, right in (("stranger", "read"), ("member", "write")):
        must(
            collections.grant(curator, test.gamma, personal(role), right),
            f"granting {right} on gamma to {username(role)}",
        )
    if not test.fed.multiuser:
        shared = must(
            collections.share(
                stranger, test.gamma, "shared", f"/{test.namespace}/{test.shared_rel}"
            ),
            "sharing gamma/shared",
        )
        test.share = str(shared.object.get("id", ""))
        must(
            collections.grant(stranger, test.share, personal("analyst"), "read"),
            "granting read on the share to test-analyst",
        )
    prefixes = [home("analyst"), home("member"), *(project(g) for g in GROUPS)]
    prefixes += [ARCHIVE, test.gamma_rel, test.shared_rel, DECOY]
    seed(test, test.namespace, prefixes)
    if test.fed.export_templates and "protected-b" in test.fed.exports:
        seed(test, "protected-b", [EXPORT, ARCHIVE])
    if test.lifetimes.refresh <= DOOMED_WAIT:
        test.doomed = (
            test.tokens("member", ("offline_access", scope("read", ARCHIVE))),
            time.time(),
        )


def seed(test: Test, namespace: str, prefixes: Sequence[str]) -> None:
    """Put an object of its own bytes, obj, in each prefix within
    namespace, at every origin."""
    caps = test.fed.exports[namespace]
    token = test.session.token(namespace)
    for prefix in prefixes:
        rel = f"{prefix}/obj"
        test.data[rel] = os.urandom(OBJECT_SIZE)
        errors = stores.seed_everywhere(
            test.fed.origins, namespace, rel, test.data[rel], token, caps
        )
        if errors:
            die(f"could not put /{namespace}/{rel} in place: {errors[0]}")
    test.seeded.append(namespace)


def finish(test: Test) -> None:
    """Remove the run's share, collection, rogue group, clients, and
    objects."""
    admin = test.api("admin")
    if test.share:
        gone = collections.delete(admin, test.share)
        if not gone.ok:
            warn(f"could not remove the share: {gone.describe()}")
    if test.gamma:
        gone = collections.delete(admin, test.gamma)
        if not gone.ok:
            warn(f"could not remove gamma: {gone.describe()}")
    rogue = f"{ROGUE}-{test.run}"
    if rogue in test.groups:
        failed = accounts.delete_group(admin, test.groups[rogue])
        if failed:
            warn(f"could not remove the rogue group: {failed}")
    for (kind, namespace), made in test.clients.items():
        if kind != "public":
            failed = issuer.delete_client(
                test.web, f"/{namespace}", test.web_token("admin"), made.client_id
            )
            if failed:
                warn(f"could not remove the {kind} client: {failed}")
    for namespace in test.seeded:
        for origin in stores.unique(test.fed.origins):
            for error in stores.empty_tree(
                origin, namespace, BASE, test.session.token(namespace)
            ):
                warn(error)


# --------------------------------------------------------------------------
# What a token should say.


def claim_problems(tok: str, role: str, issuer_url: str, groups: frozenset[str]) -> list[str]:
    """What is wrong with the token's claims: it should name issuer_url,
    role's user, the WLCG "any" audience, and the groups that earned it
    something."""
    problems: list[str] = []
    claims = issuer.claims(tok)
    who = username(role)
    if claims.get("iss") != issuer_url:
        problems.append(f"{who}'s token's issuer is {claims.get('iss')!r}, not {issuer_url}")
    if claims.get("sub") != who:
        problems.append(f"{who}'s token's subject is {claims.get('sub')!r}")
    audience = claims.get("aud")
    if audience not in (issuer.ANY_AUDIENCE, [issuer.ANY_AUDIENCE]):
        problems.append(f"{who}'s token's audience is {audience!r}, not the WLCG any")
    if claims.get("wlcg.ver") != "1.0":
        problems.append(f"{who}'s token's wlcg.ver is {claims.get('wlcg.ver')!r}")
    found = issuer.groups(tok)
    if found != groups:
        problems.append(f"{who}'s token's groups are {sorted(found)}, not {sorted(groups)}")
    return problems


# --------------------------------------------------------------------------
# The scenarios.


def discovery(test: Test) -> tuple[str, str]:
    """Each issuer's discovery document names itself and its endpoints,
    and its JWKS holds the key that signs its tokens."""
    problems: list[str] = []
    endpoints = (
        "token_endpoint",
        "authorization_endpoint",
        "device_authorization_endpoint",
        "userinfo_endpoint",
        "revocation_endpoint",
        "introspection_endpoint",
        "jwks_uri",
    )
    grants = {
        issuer.CODE_GRANT,
        issuer.REFRESH_GRANT,
        issuer.DEVICE_GRANT,
        issuer.EXCHANGE_GRANT,
    }
    checked = 0
    for namespace in PROTECTED:
        if namespace not in test.fed.exports:
            continue
        checked += 1
        issuer_url = test.fed.issuer_of(namespace)
        try:
            document = issuer.discovery(issuer_url)
        except IssuerError as e:
            problems.append(str(e))
            continue
        if document.get("issuer") != issuer_url:
            problems.append(f"/{namespace}'s discovery names {document.get('issuer')!r}")
        for key in endpoints:
            if not str(document.get(key, "")).startswith(issuer_url + "/"):
                problems.append(f"/{namespace}'s {key} is {document.get(key)!r}")
        supported = set(common.as_array(document.get("grant_types_supported")) or [])
        if not grants <= supported:
            problems.append(f"/{namespace}'s grant_types_supported are {sorted(supported)}")
        jwks = web.request("GET", str(document.get("jwks_uri", "")))
        try:
            kids = credentials.key_ids(jwks.body)
        except ValueError:
            problems.append(f"/{namespace}'s jwks_uri serves no JWKS ({jwks.describe()})")
            continue
        tok = test.tokens("member", (scope("read", ARCHIVE),), namespace=namespace).access
        kid = str(issuer.header(tok).get("kid", ""))
        if not kid or kid not in kids:
            problems.append(f"/{namespace}'s JWKS lacks kid {kid!r}, which signed a token")
        if issuer.claims(tok).get("iss") != issuer_url:
            problems.append(f"/{namespace}'s token names {issuer.claims(tok).get('iss')!r}")
    local = credentials.local_issuer(test.fed, test.web)
    try:
        document = issuer.discovery(issuer.url(test.web, issuer.LOCAL_NAMESPACE))
        if document.get("issuer") != local:
            problems.append(f"the local issuer's discovery names {document.get('issuer')!r}")
    except IssuerError as e:
        problems.append(str(e))
    return outcome(problems, f"{checked} namespace issuer(s) and the local issuer")


def templates(test: Test) -> tuple[str, str]:
    """What each user is granted of everything: the templates' scopes,
    their collection's, and their share's, no more; the groups that
    earned them; a group that may match a template only once the admin
    makes it eligible; and under `-p origin-export-templates`,
    /protected-b's own templates alone."""
    problems: list[str] = []
    earned = {"member": frozenset(GROUPS)}
    for role in ROLES:
        tok = test.tokens(role, DATA_SCOPES).access
        found, want = data_scopes(tok), test.allowed(role)
        if found != want:
            problems.append(f"{username(role)}'s token has {sorted(found)}, not {sorted(want)}")
        problems += claim_problems(tok, role, test.issuer_url, earned.get(role, frozenset()))
    rogue = test.groups[f"{ROGUE}-{test.run}"]
    archive = scope("read", ARCHIVE)
    must(
        accounts.set_eligible(test.api("admin"), rogue, True), "making the rogue group eligible"
    )
    try:
        tok = test.tokens("stranger", DATA_SCOPES).access
        if archive not in data_scopes(tok):
            problems.append(
                "test-stranger's token lacks the archive once their group may match"
            )
        if issuer.groups(tok) != frozenset({f"{ROGUE}-{test.run}"}):
            problems.append(f"test-stranger's token's groups are {sorted(issuer.groups(tok))}")
    finally:
        must(accounts.set_eligible(test.api("admin"), rogue, False), "revoking the eligibility")
    if archive in data_scopes(test.tokens("stranger", DATA_SCOPES).access):
        problems.append("test-stranger's token has the archive again once their group may not")
    notes = [f"{len(ROLES)} users"]
    if test.fed.export_templates and "protected-b" in test.fed.exports:
        wanted: dict[str, frozenset[str]] = {
            "stranger": frozenset({scope("read", EXPORT)}),
            "member": frozenset(),
        }
        for role, want in wanted.items():
            tok = test.tokens(role, DATA_SCOPES, namespace="protected-b").access
            found = data_scopes(tok)
            if found != want:
                problems.append(
                    f"{username(role)}'s /protected-b token has {sorted(found)},"
                    + f" not {sorted(want)}"
                )
            if issuer.claims(tok).get("iss") != test.fed.issuer_of("protected-b"):
                problems.append(f"{username(role)}'s /protected-b token names another issuer")
        notes.append("/protected-b's own templates")
    return outcome(problems, "; ".join(notes))


@dataclass(frozen=True)
class Request:
    """A request for scopes, and what it should get."""

    role: str
    asks: tuple[str, ...]
    gets: frozenset[str]
    why: str

    @property
    def carried(self) -> bool:
        """Whether the suite's clients may carry everything it asks."""
        return all(carried(s) for s in self.asks)


def carried(wanted: str) -> bool:
    """Whether a client allowed issuer.CLIENT_SCOPES may carry wanted."""
    if wanted in issuer.STANDARD_SCOPES or wanted in issuer.CLIENT_SCOPES:
        return True
    action = wanted.split(":", 1)[0]
    return f"{action}:/" in issuer.CLIENT_SCOPES


def requests_table(test: Test) -> list[Request]:
    """What the issuer should grant of each request."""
    reads = {s for s in test.allowed("member") if s.startswith(R)}
    writes = {s for s in test.allowed("member") if s.startswith(M)}
    rows = [
        Request(
            "member", (f"{R}:/{ARCHIVE}",), frozenset({f"{R}:/{ARCHIVE}"}), "allowed, as asked"
        ),
        Request(
            "member", (f"{R}:/{ARCHIVE}/sub",), frozenset({f"{R}:/{ARCHIVE}/sub"}), "narrower"
        ),
        Request("member", (f"{R}:/{BASE}",), frozenset(reads), "broader: the reads under it"),
        Request("member", (f"{M}:/{BASE}",), frozenset(writes), "broader: the writes under it"),
        Request("member", (f"{M}:/{ARCHIVE}",), frozenset(), "an action not allowed"),
        Request("member", (f"{R}:/elsewhere",), frozenset(), "a path not allowed"),
        Request(
            "member",
            (f"{R}:/{ARCHIVE}/../home/{username('stranger')}",),
            frozenset(),
            "climbing to a path not allowed",
        ),
        Request(
            "member",
            (f"{R}:/{home('member')}/../../archive",),
            frozenset({f"{R}:/{ARCHIVE}"}),
            "climbing to an allowed path, granted as it resolves",
        ),
        Request("member", (f"{R}:/{ARCHIVE}x",), frozenset(), "a prefix, not a path component"),
        Request("member", (f"{R}:/{BASE}/Archive",), frozenset(), "another case"),
        Request("member", ("storage.delete:/",), frozenset(), "an action no template knows"),
        Request("member", ("pelican.transfer",), frozenset(), "the transfer API's, ungranted"),
        Request("member", ("collection.read:/",), frozenset({"collection.read:/"}), "anyone's"),
        Request(
            "member", ("collection.create:/",), frozenset(), "a collection administrator's"
        ),
        Request(
            "member",
            ("openid", "wlcg", "profile", "email"),
            frozenset({"openid", "wlcg", "profile", "email"}),
            "the standard scopes",
        ),
        Request("stranger", (f"{R}:/",), frozenset({scope("read", test.gamma_rel)}), "gamma"),
        Request("stranger", (f"{M}:/",), frozenset(), "gamma is read-only to them"),
        Request(
            "analyst",
            (f"{R}:/",),
            frozenset(s for s in test.allowed("analyst") if s.startswith(R)),
            "home, and the share",
        ),
        Request(
            "analyst",
            (f"{M}:/",),
            frozenset({scope("modify", home("analyst"))}),
            "home alone: the share is read-only",
        ),
    ]
    if test.share:
        access = f"share.access:/{test.share}"
        rows += [
            Request("analyst", ("share.access:/",), frozenset({access}), "the share's access"),
            Request("analyst", (access,), frozenset({access}), "the share's access, as asked"),
            Request("analyst", ("share.access:/nonesuch",), frozenset(), "no such share"),
        ]
    return rows


def requests(test: Test) -> tuple[str, str]:
    """What the issuer grants of each request, through the device flow;
    and a refresh token only with offline_access."""
    problems: list[str] = []
    rows = requests_table(test)
    for row in rows:
        toks = test.tokens(row.role, row.asks)
        found = issuer.scopes(toks.access)
        if found != row.gets:
            problems.append(
                f"{username(row.role)} asking {' '.join(row.asks)} ({row.why}) got"
                + f" {sorted(found)}, not {sorted(row.gets)}"
            )
        if any(".." in s for s in found):
            problems.append(f"a granted scope climbs: {sorted(found)}")
        if toks.refresh is not None:
            problems.append(f"a refresh token came without offline_access ({row.why})")
    toks = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"))
    if issuer.scopes(toks.access) != frozenset({"offline_access", f"{R}:/{ARCHIVE}"}):
        problems.append(
            f"with offline_access, the token has {sorted(issuer.scopes(toks.access))}"
        )
    if toks.refresh is None:
        problems.append("no refresh token came with offline_access")
    return outcome(problems, f"{len(rows) + 1} requests")


def code(test: Test) -> tuple[str, str]:
    """The same through the authorization code flow, with PKCE, which
    refuses a scope the client may not carry; a wrong verifier, a
    replayed code, and a wrong redirect URI; and the browsers' public
    client, which gets no refresh token."""
    problems: list[str] = []
    client = test.client("code")
    rows = requests_table(test)
    for row in rows:
        user = test.user(row.role)
        if row.carried:
            try:
                found = issuer.scopes(issuer.code_flow(client, user, row.asks).access)
            except IssuerError as e:
                problems.append(f"{row.why}: {e}")
                continue
            if found != row.gets:
                problems.append(
                    f"{username(row.role)} asking {' '.join(row.asks)} ({row.why}) got"
                    + f" {sorted(found)}, not {sorted(row.gets)}"
                )
            continue
        state = secrets.token_urlsafe(12)
        sent = issuer.authorize(client, user, row.asks, issuer.pkce()[1], state)
        back = issuer.redirected(sent)
        if back.get("error") != "invalid_scope" or back.get("state") != state:
            problems.append(
                f"asking {' '.join(row.asks)}, which the client may not: {sent.describe()},"
                + f" sent back with {back or 'nothing'}, not invalid_scope"
            )
    verifier, challenge = issuer.pkce()
    sent = issuer.authorize(
        client, test.user("member"), (f"{R}:/{ARCHIVE}",), challenge, "x" * 16
    )
    back = issuer.redirected(sent)
    if not back.get("code"):
        problems.append(f"the authorization for a code: {sent.describe()}, with {back}")
    else:
        wrong = issuer.code_tokens(client, back["code"], issuer.pkce()[0])
        if wrong.status != 400 or issuer.error_of(wrong) != "invalid_grant":
            problems.append(
                f"the code with a wrong verifier: {wrong.describe()}, not invalid_grant"
            )
        right = issuer.code_tokens(client, back["code"], verifier)
        if right.status == 200:
            again = issuer.code_tokens(client, back["code"], verifier)
            if again.status != 400 or issuer.error_of(again) != "invalid_grant":
                problems.append(f"the code replayed: {again.describe()}, not invalid_grant")
        else:
            problems.append(f"the code with the right verifier: {right.describe()}")
    sent = issuer.authorize(
        client,
        test.user("member"),
        (f"{R}:/{ARCHIVE}",),
        challenge,
        "x" * 16,
        "https://localhost/elsewhere",
    )
    if sent.status != 400 or issuer.redirected(sent):
        problems.append(f"a redirect URI the client did not register: {sent.describe()}")
    public = test.client("public")
    try:
        toks = issuer.code_flow(
            public, test.user("member"), ("offline_access", f"{R}:/{ARCHIVE}")
        )
        if issuer.scopes(toks.access) != frozenset({f"{R}:/{ARCHIVE}"}):
            problems.append(
                f"the public client's token has {sorted(issuer.scopes(toks.access))}"
            )
        if toks.refresh is not None:
            problems.append("the public client got a refresh token")
    except IssuerError as e:
        problems.append(f"the public client's code flow: {e}")
    return outcome(problems, f"{len(rows)} requests; wrong verifier, replay, and URI refused")


def clients(test: Test) -> tuple[str, str]:
    """A client gets no more than it may carry, and no refresh token
    without the grant; a client without the device grant, a wrong
    secret, and an unknown client get nothing."""
    problems: list[str] = []
    narrow = test.client("narrow")
    toks = test.tokens("member", ("openid", "offline_access", *DATA_SCOPES), kind="narrow")
    want = frozenset({"openid"} | {s for s in test.allowed("member") if s.startswith(R)})
    if issuer.scopes(toks.access) != want:
        problems.append(
            f"the read-only client's token has {sorted(issuer.scopes(toks.access))}"
        )
    if toks.refresh is not None:
        problems.append("the read-only client, which may not refresh, got a refresh token")
    refused = (
        ("a client without the device grant", test.client("code"), 400, "unauthorized_client"),
        (
            "a wrong secret",
            dataclasses.replace(narrow, client_secret="wrong"),  # nosec B106
            401,
            "invalid_client",
        ),
        (
            "an unknown client",
            dataclasses.replace(narrow, client_id="nonesuch"),
            401,
            "invalid_client",
        ),
    )
    for what, client, status, error in refused:
        answer = issuer.start_device(client, (f"{R}:/{ARCHIVE}",))
        if answer.status != status or issuer.error_of(answer) != error:
            problems.append(f"{what} starting a device flow: {answer.describe()}, not {error}")
    for what, client, status, error in (
        ("the client", narrow, 400, "unauthorized_client"),
        (
            "an unknown client",
            dataclasses.replace(narrow, client_id="nonesuch"),
            401,
            "invalid_client",
        ),
    ):
        answer = issuer.post(client, narrow.token_url, {"grant_type": "client_credentials"})
        if answer.status != status or issuer.error_of(answer) != error:
            problems.append(
                f"{what}'s client_credentials ping: {answer.describe()}, not {error}"
            )
    toks = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"), kind="public")
    if issuer.scopes(toks.access) != frozenset({f"{R}:/{ARCHIVE}"}) or toks.refresh is not None:
        problems.append(
            f"the public client's device flow gave {sorted(issuer.scopes(toks.access))}"
            + f" and {'a' if toks.refresh else 'no'} refresh token"
        )
    return outcome(problems, "read-only, public, and wrong clients get what they may")


def device(test: Test) -> tuple[str, str]:
    """The device flow's own rules."""
    problems: list[str] = []
    client, member = test.client("device"), test.user("member")
    flow = issuer.started(issuer.start_device(client, (f"{R}:/{ARCHIVE}",)), client)
    device_code, user_code = str(flow["device_code"]), str(flow["user_code"])
    complete = str(flow.get("verification_uri_complete", ""))
    if "/view/issuer/device" not in complete or user_code not in complete:
        problems.append(f"verification_uri_complete is {complete!r}")
    page = issuer.consent_page(client.url, user_code, member)
    shown = issuer.decoded(page)
    if page.status != 200 or shown.get("scopes") != [f"{R}:/{ARCHIVE}"]:
        problems.append(f"the consent page: {page.describe()}, showing {shown.get('scopes')!r}")
    if shown.get("client_id") != client.client_id:
        problems.append(f"the consent page names client {shown.get('client_id')!r}")
    polled = issuer.poll(client, device_code)
    if polled.status != 400 or issuer.error_of(polled) != "authorization_pending":
        problems.append(
            f"polling before approval: {polled.describe()}, not authorization_pending"
        )
    polled = issuer.poll(client, device_code)
    if polled.status != 400 or issuer.error_of(polled) != "slow_down":
        problems.append(f"polling again at once: {polled.describe()}, not slow_down")
    issuer.approve(client.url, user_code, member)
    time.sleep(POLL_INTERVAL)
    polled = issuer.poll(test.client("narrow"), device_code)
    if polled.status != 400 or issuer.error_of(polled) != "invalid_grant":
        problems.append(
            f"another client redeeming the code: {polled.describe()}, not invalid_grant"
        )
    time.sleep(POLL_INTERVAL)
    polled = issuer.poll(client, device_code)
    if polled.status != 200:
        problems.append(f"redeeming the approved code: {polled.describe()}")
    time.sleep(POLL_INTERVAL)
    polled = issuer.poll(client, device_code)
    if polled.status != 400 or issuer.error_of(polled) != "invalid_grant":
        problems.append(f"redeeming the code again: {polled.describe()}, not invalid_grant")
    again = issuer.decide(client.url, user_code, member, "approve")
    if again.status != 400:
        problems.append(f"approving the used code again: {again.describe()}, not 400")
    flow = issuer.started(issuer.start_device(client, (f"{R}:/{ARCHIVE}",)), client)
    denied = issuer.decide(client.url, str(flow["user_code"]), member, "deny")
    if denied.status != 200 or issuer.decoded(denied).get("status") != "denied":
        problems.append(f"denying a flow: {denied.describe()}")
    polled = issuer.poll(client, str(flow["device_code"]))
    if polled.status != 403 or issuer.error_of(polled) != "access_denied":
        problems.append(f"polling a denied flow: {polled.describe()}, not access_denied")
    flow = issuer.started(issuer.start_device(client, (f"{R}:/{ARCHIVE}",)), client)
    nobody = issuer.consent_page(client.url, str(flow["user_code"]), User())
    if nobody.status == 200:
        problems.append("the consent page shows a flow to nobody")
    unknown = issuer.decide(client.url, "XXXX-XXXX", member, "approve")
    if unknown.status != 400:
        problems.append(f"approving an unknown code: {unknown.describe()}, not 400")
    return outcome(problems, "consent, pending, slow_down, wrong client, replay, denial")


def data(test: Test) -> tuple[str, str]:
    """What the tokens allow: reads and writes straight at every origin,
    and reads through the federation and at each cache."""
    problems: list[str] = []
    tokens = {role: test.tokens(role, DATA_SCOPES).access for role in ROLES}
    reads: list[tuple[str, str, bool]] = [
        ("member", f"{ARCHIVE}/obj", True),
        ("member", f"{project(GROUPS[0])}/obj", True),
        ("member", f"{project(GROUPS[1])}/obj", True),
        ("member", f"{home('member')}/obj", True),
        ("member", f"{test.gamma_rel}/obj", True),
        ("member", f"{DECOY}/obj", False),
        ("member", f"{home('analyst')}/obj", False),
        ("analyst", f"{home('analyst')}/obj", True),
        ("analyst", f"{test.gamma_rel}/obj", False),
        ("analyst", f"{ARCHIVE}/obj", False),
        ("stranger", f"{test.gamma_rel}/obj", True),
        ("stranger", f"{ARCHIVE}/obj", False),
        ("stranger", f"{home('member')}/obj", False),
    ]
    if test.share:
        reads += [("analyst", f"{test.shared_rel}/obj", True)]
    notes: list[str] = []
    if "DirectReads" in test.caps:
        for origin in test.fed.origins:
            for role, rel, allowed in reads:
                target = test.object_url(origin, rel)
                answer = web.request("GET", target, token=tokens[role])
                if allowed and (answer.status != 200 or answer.body != test.data[rel]):
                    problems.append(
                        f"GET {target} as {username(role)}: {answer.describe()}, or wrong bytes"
                    )
                if not allowed and answer.status not in REFUSED:
                    problems.append(f"GET {target} as {username(role)}: {answer.describe()}")
            if "Writes" in test.caps:
                problems += write_problems(test, origin, tokens)
        notes.append(f"at {len(test.fed.origins)} origin(s)")
    else:
        notes.append("no direct reads")
    if not test.fed.standalone:
        problems += federation_problems(test, tokens)
        notes.append(f"through the federation and {len(test.fed.federated_caches)} cache(s)")
    return outcome(problems, ", ".join(notes))


def write_problems(test: Test, origin: common.Origin, tokens: dict[str, str]) -> list[str]:
    """What is wrong with PUT straight to origin with the tokens: within
    a prefix with create or modify, and nowhere else."""
    problems: list[str] = []
    payload = os.urandom(OBJECT_SIZE)
    refused = [
        ("member", f"{ARCHIVE}/put-{origin.svc}"),
        ("stranger", f"{test.gamma_rel}/put-{origin.svc}"),
    ]
    if test.share:
        refused.append(("analyst", f"{test.shared_rel}/put-{origin.svc}"))
    for role, rel in refused:
        target = test.object_url(origin, rel)
        answer = web.request("PUT", target, token=tokens[role], upload=payload)
        if answer.status not in REFUSED:
            problems.append(
                f"PUT {target} as {username(role)}: {answer.describe()}, not refused"
            )
    for role, rel in (
        ("member", f"{home('member')}/put-{origin.svc}"),
        ("member", f"{project(GROUPS[0])}/put-{origin.svc}"),
        ("analyst", f"{home('analyst')}/put-{origin.svc}"),
    ):
        target = test.object_url(origin, rel)
        answer = web.request("PUT", target, token=tokens[role], upload=payload)
        if not answer.ok:
            problems.append(f"PUT {target} as {username(role)}: {answer.describe()}")
            continue
        back = web.request("GET", target, token=test.server_token)
        if back.status != 200 or back.body != payload:
            problems.append(f"GET {target} after the PUT: {back.describe()}, or wrong bytes")
    return problems


def federation_problems(test: Test, tokens: dict[str, str]) -> list[str]:
    """What is wrong with the tokens through the federation: a `pelican
    object get` through a cache, and a GET at each cache, as test-member
    and as test-stranger."""
    problems: list[str] = []
    rel = f"{ARCHIVE}/obj"
    for role, allowed in (("member", True), ("stranger", False)):
        dest = test.session.path(f"issuer-get-{role}")
        status, _, err = test.session.pelican_cmd(
            "object", "get", test.federation_url(rel), dest, "--token", test.token_file(role)
        )
        if allowed and (status != 0 or common.sha256(dest) != common.digest(test.data[rel])):
            problems.append(f"`object get` as test-member: exit {status}: {last_line(err)}")
        if not allowed and status == 0:
            problems.append("`object get` as test-stranger succeeded")
    for cache in test.fed.federated_caches:
        target = test.cache_url(cache, rel)
        answer = web.request("GET", target, token=tokens["member"])
        if answer.status != 200 or answer.body != test.data[rel]:
            problems.append(f"GET {target} as test-member: {answer.describe()}, or wrong bytes")
        answer = web.request("GET", target, token=tokens["stranger"])
        if answer.status not in REFUSED:
            problems.append(f"GET {target} as test-stranger: {answer.describe()}, not refused")
    return problems


def reads_archive(test: Test, access: str) -> Optional[str]:
    """Why the access token does not read archive/obj at the issuer
    origin, or None if it does; None too where the origin takes no
    direct clients."""
    if "DirectReads" not in test.caps:
        return None
    target = test.object_url(test.origin, f"{ARCHIVE}/obj")
    answer = web.request("GET", target, token=access)
    if answer.status != 200 or answer.body != test.data[f"{ARCHIVE}/obj"]:
        return f"GET {target}: {answer.describe()}, or wrong bytes"
    return None


def refresh(test: Test) -> tuple[str, str]:
    """A refresh token works, once (but again within its grace period),
    for the client it was issued to, and the tokens it gives work."""
    problems: list[str] = []
    client = test.client("device")
    wanted = ("offline_access", f"{R}:/{ARCHIVE}")
    first = test.tokens("member", wanted)
    if first.refresh is None:
        return FAIL, "no refresh token came with offline_access"
    renewed = issuer.refresh(client, first.refresh)
    try:
        second = issuer.tokens_of(renewed, "the refresh")
    except IssuerError as e:
        return FAIL, str(e)
    if second.access == first.access:
        problems.append("the refresh gave the same access token back")
    if second.refresh is None or second.refresh == first.refresh:
        problems.append("the refresh gave no new refresh token")
    if issuer.scopes(second.access) != issuer.scopes(first.access):
        problems.append(
            f"the refreshed token's scopes are {sorted(issuer.scopes(second.access))}"
        )
    problems += claim_problems(second.access, "member", test.issuer_url, frozenset(GROUPS))
    if abs(second.expires_in - test.lifetimes.access) > 1:
        problems.append(f"the refreshed token's expires_in is {second.expires_in}")
    failed = reads_archive(test, second.access)
    if failed:
        problems.append(f"with the refreshed token, {failed}")
    said = issuer.introspect(client, second.access)
    if not said.get("active") or said.get("sub") != username("member"):
        problems.append(f"the refreshed token introspects as {said}")
    said = issuer.introspect(client, second.refresh or "", "refresh_token")
    if not said.get("active"):
        problems.append(f"the new refresh token introspects as {said}")
    reused = issuer.refresh(client, first.refresh)
    if reused.status != 200:
        problems.append(f"the used refresh token, within its grace period: {reused.describe()}")
    for what, other, status, error in (
        ("another client", test.client("code"), 400, "invalid_grant"),
        (
            "a client without the refresh grant",
            test.client("narrow"),
            400,
            "unauthorized_client",
        ),
    ):
        answer = issuer.refresh(other, second.refresh or "")
        if answer.status != status or issuer.error_of(answer) != error:
            problems.append(
                f"the refresh token used by {what}: {answer.describe()}, not {error}"
            )
    answer = issuer.refresh(client, "nonesuch")
    if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
        problems.append(
            f"a refresh token that never was: {answer.describe()}, not invalid_grant"
        )
    return outcome(problems, "new tokens that work; the old one works within its grace period")


def revoked(test: Test) -> tuple[str, str]:
    """What a refresh gives once a grant is revoked: nothing of it; a
    revoked refresh token, and the access token issued with it; and a
    refresh token reused past its grace period, which ends its chain."""
    problems: list[str] = []
    client, curator = test.client("device"), test.api("curator")
    gamma = scope("read", test.gamma_rel)
    before = test.tokens("stranger", ("offline_access", f"{R}:/"))
    if gamma not in issuer.scopes(before.access) or before.refresh is None:
        return FAIL, "test-stranger's tokens lack gamma's read, or a refresh token"
    must(
        collections.revoke(curator, test.gamma, personal("stranger"), "read"),
        "revoking test-stranger's read",
    )
    try:
        after = issuer.refresh(client, before.refresh)
        got = issuer.tokens_of(after, "the refresh after the revoke")
        if gamma in issuer.scopes(got.access):
            problems.append(
                "a refresh after the grant was revoked still gives gamma's read (Pelican's"
                + " main re-grants a refresh token's scopes without asking the collections)"
            )
    except IssuerError as e:
        problems.append(str(e))
    finally:
        must(
            collections.grant(curator, test.gamma, personal("stranger"), "read"),
            "granting test-stranger's read again",
        )
    chain = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"))
    answer = issuer.revoke(client, chain.refresh or "", "refresh_token")
    if answer.status != 200:
        problems.append(f"revoking a refresh token: {answer.describe()}")
    answer = issuer.refresh(client, chain.refresh or "")
    if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
        problems.append(f"the revoked refresh token: {answer.describe()}, not invalid_grant")
    said = issuer.introspect(client, chain.access)
    if said.get("active") is not False:
        problems.append(f"the access token issued with it introspects as {said}")
    answer = issuer.userinfo(client.url, chain.access)
    if answer.status != 401:
        problems.append(f"userinfo with it: {answer.describe()}, not 401")
    notes = ["a revoked grant", "a revoked token"]
    if reads_archive(test, chain.access) is None and "DirectReads" in test.caps:
        notes.append("which the origin, which checks its signature alone, still honors")
    if test.lifetimes.grace <= SHORT_GRACE:
        chain = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"))
        newer = issuer.tokens_of(issuer.refresh(client, chain.refresh or ""), "the refresh")
        time.sleep(test.lifetimes.grace + 3)
        answer = issuer.refresh(client, chain.refresh or "")
        if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
            problems.append(
                f"a refresh token reused past its grace period: {answer.describe()}"
            )
        answer = issuer.refresh(client, newer.refresh or "")
        if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
            problems.append(f"the chain of a refresh token reused late: {answer.describe()}")
        notes.append(f"a reuse {test.lifetimes.grace}s late ends the chain")
    else:
        notes.append(f"the grace period is {test.lifetimes.grace}s, too long to wait out")
    return outcome(problems, "; ".join(notes))


def exchange(test: Test) -> tuple[str, str]:
    """Token exchange narrows a token to what is asked and the client may
    carry, inherits the token's scopes if nothing is asked, and refuses
    another audience, another client, and a token that never was."""
    problems: list[str] = []
    client = test.client("exchange")
    subject = test.tokens("member", ("offline_access", f"{R}:/", f"{M}:/")).access
    reads = {s for s in issuer.scopes(subject) if s.startswith(R)}
    answer = issuer.exchange(client, subject, (f"{R}:/{ARCHIVE}",))
    try:
        got = issuer.tokens_of(answer, "the exchange")
        if issuer.scopes(got.access) != frozenset({f"{R}:/{ARCHIVE}"}):
            problems.append(f"the exchanged token has {sorted(issuer.scopes(got.access))}")
        if got.raw.get("issued_token_type") != issuer.ACCESS_TOKEN_TYPE:
            problems.append(f"issued_token_type is {got.raw.get('issued_token_type')!r}")
        problems += claim_problems(got.access, "member", test.issuer_url, frozenset(GROUPS))
        failed = reads_archive(test, got.access)
        if failed:
            problems.append(f"with the exchanged token, {failed}")
    except IssuerError as e:
        problems.append(str(e))
    answer = issuer.exchange(client, subject, (f"{M}:/{home('member')}",))
    try:
        got = issuer.tokens_of(answer, "the exchange for a scope the client may not carry")
        if issuer.scopes(got.access):
            problems.append(
                "an exchange for a scope the client may not carry gave"
                + f" {sorted(issuer.scopes(got.access))}"
            )
    except IssuerError as e:
        problems.append(str(e))
    answer = issuer.exchange(client, subject)
    try:
        got = issuer.tokens_of(answer, "the exchange for nothing in particular")
        if issuer.scopes(got.access) != frozenset(reads | {"offline_access"}):
            problems.append(f"the inherited scopes are {sorted(issuer.scopes(got.access))}")
        if got.refresh is None:
            problems.append("no refresh token came with the inherited offline_access")
    except IssuerError as e:
        problems.append(str(e))
    answer = issuer.exchange(client, subject, (f"{R}:/{ARCHIVE}",), "https://elsewhere.invalid")
    if answer.status != 400 or issuer.error_of(answer) != "invalid_target":
        problems.append(
            f"an exchange for another audience: {answer.describe()}, not invalid_target"
        )
    answer = issuer.exchange(test.client("device"), subject, (f"{R}:/{ARCHIVE}",))
    if answer.status != 400 or issuer.error_of(answer) != "unauthorized_client":
        problems.append(
            f"an exchange by the device client: {answer.describe()}, not unauthorized_client"
        )
    answer = issuer.exchange(client, "nonesuch", (f"{R}:/{ARCHIVE}",))
    if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
        problems.append(
            f"an exchange of a token that never was: {answer.describe()}, not invalid_grant"
        )
    return outcome(problems, "narrowed, inherited, and refused as it should be")


def expiry(test: Test) -> tuple[str, str]:
    """An expired access token is refused by the issuer and the origin,
    a refresh token renews it, and the refresh token expires itself."""
    lifetimes = test.lifetimes
    if lifetimes.access > SHORT:
        return (
            SKIP,
            f"access tokens last {lifetimes.access}s ('-p origin-short-tokens' shortens them)",
        )
    problems: list[str] = []
    client = test.client("device")
    toks = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"))
    claims = issuer.claims(toks.access)
    # The token's iat is when the user approved the flow, so the lifetime
    # is from now, not from iat.
    exp = float(claims.get("exp", 0))
    left = exp - time.time()
    if (
        toks.expires_in != lifetimes.access
        or not lifetimes.access - 3 <= left <= lifetimes.access
    ):
        problems.append(
            f"the token says expires_in {toks.expires_in}, and expires in {left:.0f}s"
        )
    time.sleep(max(0.0, exp + 1 - time.time()))
    if issuer.introspect(client, toks.access).get("active") is not False:
        problems.append("the expired token introspects as active")
    if issuer.userinfo(client.url, toks.access).status != 401:
        problems.append("userinfo accepts the expired token")
    notes = [f"expired after {lifetimes.access}s"]
    if "DirectReads" in test.caps:
        target = test.object_url(test.origin, f"{ARCHIVE}/obj")
        refused_at: list[float] = []

        def origin_refuses() -> bool:
            if web.request("GET", target, token=toks.access).status in REFUSED:
                refused_at.append(time.time())
                return True
            return False

        if common.wait_for(origin_refuses, SKEW, 5):
            notes.append(f"refused by the origin {refused_at[0] - exp:.0f}s past its expiry")
        else:
            problems.append(f"the origin still serves with the token {SKEW}s past its expiry")
    renewed = issuer.refresh(client, toks.refresh or "")
    try:
        fresh = issuer.tokens_of(renewed, "the refresh of an expired access token")
        failed = reads_archive(test, fresh.access)
        if failed:
            problems.append(f"with the renewed token, {failed}")
    except IssuerError as e:
        problems.append(str(e))
    if test.doomed is not None:
        doomed, minted = test.doomed
        time.sleep(max(0.0, minted + lifetimes.refresh + 2 - time.time()))
        answer = issuer.refresh(client, doomed.refresh or "")
        if answer.status != 400 or issuer.error_of(answer) != "invalid_grant":
            problems.append(
                f"the expired refresh token: {answer.describe()}, not invalid_grant"
            )
        notes.append(f"the refresh token expired after {lifetimes.refresh}s")
    return outcome(problems, "; ".join(notes))


def client_scenario(test: Test) -> tuple[str, str]:
    """`pelican object get` acquires a token from the issuer, approved as
    test-member, reuses it for a second get, and, once it has expired,
    refreshes it, approved by nobody."""
    if test.fed.standalone:
        return SKIP, "a standalone origin has no director to find the issuer through"
    problems: list[str] = []
    session, rel = test.session, f"{ARCHIVE}/obj"
    env = issuer.client_env(session.env, session.path("credentials/member"))

    def approve_as_member(issuer_url: str, user_code: str) -> None:
        issuer.approve(issuer_url, user_code, test.user("member"))

    def get(name: str) -> tuple[issuer.ClientRun, bool]:
        dest = session.path(f"issuer-client-{name}")
        args = ["object", "get", test.federation_url(rel), dest]
        done = issuer.run_approving(
            session.pelican, args, env, session.tmp, approve_as_member, CLIENT_TIMEOUT
        )
        problems.extend(done.problems)
        right = done.status == 0 and common.sha256(dest) == common.digest(test.data[rel])
        if not right:
            problems.append(
                f"`object get` ({name}): exit {done.status}: {last_line(done.stderr)}"
            )
        return done, right

    done, _ = get("first")
    # The token was minted before the get finished, so it expires within
    # its lifetime of now.
    acquired = time.time()
    if done.approvals != 1:
        problems.append(f"the first get asked for {done.approvals} approvals, not 1")
    done, _ = get("second")
    if done.approvals != 0:
        problems.append(f"the second get asked for {done.approvals} approvals, not 0")
    notes = ["acquired once, reused"]
    if test.lifetimes.access <= SHORT:
        time.sleep(max(0.0, acquired + test.lifetimes.access + 2 - time.time()))
        done, right = get("expired")
        if done.approvals != 0:
            problems.append(f"the get after expiry asked for {done.approvals} approvals, not 0")
        if right and done.approvals == 0:
            notes.append("refreshed once expired")
    else:
        notes.append(f"tokens last {test.lifetimes.access}s, too long to see one refreshed")
    return outcome(problems, ", ".join(notes))


# --------------------------------------------------------------------------
# The issuer's boundaries.


def refused_with(answer: web.Response, status: int, error: str) -> bool:
    """Whether answer is an OAuth2 error of status and error."""
    return answer.status == status and issuer.error_of(answer) == error


def isolation(test: Test) -> tuple[str, str]:
    """Each namespace's issuer is a trust domain of its own: a client, a
    refresh token, or an access token of one is nothing to another, a
    revocation at one is nothing to the other, a namespace that merely
    begins like another has no issuer, and a collection in one is not
    granted by the other."""
    problems: list[str] = []
    stray = f"{test.web}{issuer.ROUTE}/{test.namespace}x/.well-known/openid-configuration"
    found = web.request("GET", stray)
    if found.status != 404 or "token_endpoint" in issuer.decoded(found):
        problems.append(f"GET {stray}: {found.describe()}, not 404")
    other = credentials.OTHER[test.namespace]
    if other not in test.fed.exports:
        return outcome(problems, f"/{test.namespace}x has no issuer; no /{other} to cross")
    mine, theirs = test.client("device"), test.client("device", other)
    toks = test.tokens("member", ("offline_access", f"{R}:/{ARCHIVE}"))
    if toks.refresh is None:
        return FAIL, "no refresh token came with offline_access"
    crossed = dataclasses.replace(
        mine,
        url=theirs.url,
        device_url=theirs.device_url,
        token_url=theirs.token_url,
        authorize_url=theirs.authorize_url,
    )
    answer = issuer.start_device(crossed, (f"{R}:/{ARCHIVE}",))
    if not refused_with(answer, 401, "invalid_client"):
        problems.append(f"the client at /{other}'s device endpoint: {answer.describe()}")
    answer = issuer.refresh(crossed, toks.refresh)
    if not refused_with(answer, 401, "invalid_client"):
        problems.append(f"the client at /{other}'s token endpoint: {answer.describe()}")
    answer = issuer.refresh(theirs, toks.refresh)
    if not refused_with(answer, 400, "invalid_grant"):
        problems.append(
            f"the refresh token at /{other}: {answer.describe()}, not invalid_grant"
        )
    said = issuer.introspect(theirs, toks.access)
    if said.get("active") is not False:
        problems.append(f"the access token introspects at /{other} as {said}")
    answer = issuer.userinfo(theirs.url, toks.access)
    if answer.status != 401:
        problems.append(f"/{other}'s userinfo with the access token: {answer.describe()}")
    answer = issuer.exchange(test.client("exchange", other), toks.access, (f"{R}:/{ARCHIVE}",))
    if not refused_with(answer, 400, "invalid_grant"):
        problems.append(f"an exchange of the access token at /{other}: {answer.describe()}")
    for tok, hint in ((toks.access, "access_token"), (toks.refresh, "refresh_token")):
        issuer.revoke(theirs, tok, hint)
        said = issuer.introspect(mine, tok, hint)
        if not said.get("active"):
            problems.append(f"the {hint}, revoked at /{other}, introspects as {said}")
    problems += collection_isolation(test, other)
    return outcome(problems, f"/{test.namespace}'s clients and tokens are nothing to /{other}")


def collection_isolation(test: Test, other: str) -> list[str]:
    """What is wrong with what other's issuer grants of gamma, which is a
    collection of the suite's namespace alone, though other holds an
    object at the same path: it should grant nothing of it."""
    problems: list[str] = []
    rel = f"{test.gamma_rel}/obj"
    errors = stores.seed_everywhere(
        test.fed.origins,
        other,
        rel,
        os.urandom(OBJECT_SIZE),
        test.session.token(other),
        test.fed.exports[other],
    )
    if other not in test.seeded:
        test.seeded.append(other)
    if errors:
        return [f"could not put /{other}/{rel} in place: {errors[0]}"]
    wanted = (f"{R}:/", f"{M}:/", scope("read", test.gamma_rel))
    granted = data_scopes(test.tokens("member", wanted).access)
    if scope("read", test.gamma_rel) not in granted:
        problems.append(f"/{test.namespace}'s issuer does not grant gamma to test-member")
    tok = test.tokens("member", wanted, namespace=other).access
    gamma = sorted(s for s in data_scopes(tok) if f":/{test.gamma_rel}" in s)
    if gamma:
        problems.append(f"/{other}'s issuer grants /{test.namespace}'s collection: {gamma}")
    if "DirectReads" in test.fed.exports[other]:
        target = stores.url(test.origin, other, rel)
        answer = web.request("GET", target, token=tok)
        if answer.status not in REFUSED:
            problems.append(f"GET {target} with /{other}'s token: {answer.describe()}")
    return problems


def device_csrf(test: Test) -> tuple[str, str]:
    """The verification page refuses an approval, and a denial, whose CSRF
    token is not both in its cookie and in the body, alike; the flow is
    still pending after them, and an approval with it still works."""
    problems: list[str] = []
    client, member = test.client("device"), test.user("member")
    flow = issuer.started(issuer.start_device(client, (f"{R}:/{ARCHIVE}",)), client)
    device_code, user_code = str(flow["device_code"]), str(flow["user_code"])
    forged = (
        ("approve", issuer.CSRF_NO_COOKIE, "without the cookie"),
        ("approve", issuer.CSRF_WRONG_COOKIE, "with a cookie of another value"),
        ("approve", issuer.CSRF_NONE, "with neither cookie nor token"),
        ("deny", issuer.CSRF_NO_COOKIE, "denied without the cookie"),
    )
    for action, csrf, what in forged:
        answer = issuer.decide(client.url, user_code, member, action, csrf)
        said = issuer.decoded(answer)
        if answer.status != 403 or said.get("error") != "CSRF validation failed":
            problems.append(f"{what}: {answer.describe()}, {said.get('error')!r}")
    polled = issuer.poll(client, device_code)
    if not refused_with(polled, 400, "authorization_pending"):
        problems.append(f"polling after them: {polled.describe()}, not authorization_pending")
    try:
        issuer.approve(client.url, user_code, member)
        time.sleep(POLL_INTERVAL)
        issuer.tokens_of(issuer.poll(client, device_code), "redeeming the approved code")
    except IssuerError as e:
        problems.append(f"approving with the CSRF token after them: {e}")
    return outcome(problems, f"{len(forged)} forged decisions refused; the flow still works")


def admin_clients(test: Test) -> tuple[str, str]:
    """The admin API for the issuer's clients: only the admin may use it;
    it never shows a secret; it creates clients only of the grants it
    knows; and a client it narrows, or deletes, can do only as much as it
    now says."""
    problems: list[str] = []
    namespace, admin = f"/{test.namespace}", test.web_token("admin")
    for who, tok, status in (
        ("nobody", None, 401),
        ("test-member", test.web_token("member"), 403),
    ):
        answer = issuer.admin_call(test.web, namespace, tok)
        if answer.status != status:
            problems.append(f"listing the clients as {who}: {answer.describe()}, not {status}")
    made = issuer.create_client(
        test.web, namespace, admin, name=f"pelican-test-framework admin-clients {test.run}"
    )
    gone = False
    try:
        answer = issuer.admin_call(
            test.web, namespace, test.web_token("member"), "DELETE", made.client_id
        )
        if answer.status != 403:
            problems.append(f"deleting a client as test-member: {answer.describe()}, not 403")
        listed = issuer.admin_call(test.web, namespace, admin)
        rows = collections.objects(common.as_array(json_of(listed)) or [])
        if listed.status != 200 or made.client_id not in {r.get("client_id") for r in rows}:
            problems.append(f"the admin's list: {listed.describe()}, without the new client")
        shown = issuer.admin_call(test.web, namespace, admin, "GET", made.client_id)
        detail = issuer.decoded(shown)
        grants = [issuer.DEVICE_GRANT, issuer.REFRESH_GRANT]
        if shown.status != 200 or detail.get("grant_types") != grants:
            problems.append(f"the admin's GET of the client: {shown.describe()}, {detail}")
        if any("secret" in key for row in [*rows, detail] for key in row):
            problems.append("the admin's list or GET shows a client's secret")
        body = {"client_name": f"pelican-test-framework {test.run}"}
        answer = issuer.admin_call(
            test.web,
            namespace,
            admin,
            "POST",
            body={**body, "grant_types": ["client_credentials"]},
        )
        if answer.status != 400:
            problems.append(
                f"creating a client_credentials client: {answer.describe()}, not 400"
            )
            stray = str(issuer.decoded(answer).get("client_id") or "")
            if stray:
                issuer.delete_client(test.web, namespace, admin, stray)
        toks = issuer.device_tokens(
            made, test.user("member"), ("offline_access", f"{R}:/{ARCHIVE}")
        )
        problems += narrowed_client_problems(test, made, toks.refresh or "")
        answer = issuer.admin_call(
            test.web, namespace, admin, "PUT", f"nonesuch-{test.run}", {"scopes": ["openid"]}
        )
        if answer.status != 404:
            problems.append(f"changing a client that never was: {answer.describe()}, not 404")
        answer = issuer.admin_call(test.web, namespace, admin, "DELETE", made.client_id)
        gone = answer.status == 204
        if not gone:
            problems.append(f"deleting the client: {answer.describe()}, not 204")
        answer = issuer.refresh(made, toks.refresh or "")
        if not refused_with(answer, 401, "invalid_client"):
            problems.append(
                f"a refresh by the deleted client: {answer.describe()}, not invalid_client"
            )
        answer = issuer.admin_call(test.web, namespace, admin, "DELETE", made.client_id)
        if answer.status != 404:
            problems.append(f"deleting the client again: {answer.describe()}, not 404")
    finally:
        if not gone:
            failed = issuer.delete_client(test.web, namespace, admin, made.client_id)
            if failed:
                warn(f"could not remove the admin-clients client: {failed}")
    return outcome(problems, "admin only; no secrets; narrowed and deleted clients obey")


def json_of(answer: web.Response) -> object:
    """The JSON value in answer's body, or None."""
    try:
        return json.loads(answer.body)
    except ValueError:
        return None


def narrowed_client_problems(test: Test, made: issuer.Client, refresh_token: str) -> list[str]:
    """What is wrong with a client the admin narrows: once it may no
    longer refresh, refresh_token, which it was issued, is refused; once
    it may carry only archive's read, its tokens carry no more."""
    problems: list[str] = []
    namespace, admin = f"/{test.namespace}", test.web_token("admin")
    grants = [issuer.DEVICE_GRANT]
    answer = issuer.admin_call(
        test.web, namespace, admin, "PUT", made.client_id, {"grant_types": grants}
    )
    if answer.status != 200 or issuer.decoded(answer).get("grant_types") != grants:
        problems.append(f"dropping the refresh grant: {answer.describe()}")
    answer = issuer.refresh(made, refresh_token)
    if not refused_with(answer, 400, "unauthorized_client"):
        problems.append(
            f"a refresh once the grant is dropped: {answer.describe()}, not unauthorized_client"
        )
    narrow = ["openid", f"{R}:/{ARCHIVE}"]
    answer = issuer.admin_call(
        test.web, namespace, admin, "PUT", made.client_id, {"scopes": narrow}
    )
    if answer.status != 200 or issuer.decoded(answer).get("scopes") != narrow:
        problems.append(f"narrowing the scopes: {answer.describe()}")
    wanted = ("openid", "offline_access", f"{R}:/", f"{M}:/")
    toks = issuer.device_tokens(made, test.user("member"), wanted)
    if issuer.scopes(toks.access) != frozenset(narrow) or toks.refresh is not None:
        problems.append(
            f"the narrowed client's token has {sorted(issuer.scopes(toks.access))}"
            + f" and {'a' if toks.refresh else 'no'} refresh token"
        )
    return problems


def cors_and_pkce(test: Test) -> tuple[str, str]:
    """A site the issuer does not know may read its discovery document,
    which is public, but its endpoints that take credentials do not let
    it read their answers; and the public client may not run the code
    flow without PKCE."""
    problems: list[str] = []
    evil = {"Origin": "https://evil.invalid"}
    target = f"{test.issuer_url}/.well-known/openid-configuration"
    found = web.request("GET", target, headers=evil)
    allowed = found.header("Access-Control-Allow-Origin")
    if found.status != 200 or allowed not in ("", "*"):
        problems.append(
            f"GET {target} from another site: {found.describe()}, allowing {allowed!r}"
        )
    client = test.client("device")
    for method, url in (
        ("POST", client.token_url),
        ("POST", client.device_url),
        ("OPTIONS", client.token_url),
    ):
        headers = {**evil, **issuer.FORM, "Access-Control-Request-Method": "POST"}
        answer = web.request(method, url, upload=b"", headers=headers)
        allowed = answer.header("Access-Control-Allow-Origin")
        if answer.status is None or allowed:
            problems.append(
                f"{method} {url} from another site: {answer.describe()}, {allowed!r}"
            )
    state = secrets.token_urlsafe(12)
    sent = issuer.authorize(
        test.client("public"), test.user("member"), (f"{R}:/{ARCHIVE}",), "", state
    )
    back = issuer.redirected(sent)
    if back.get("code") or (back.get("error") != "invalid_request" and sent.status != 400):
        problems.append(
            f"the public client's code flow without PKCE: {sent.describe()},"
            + f" sent back with {back or 'nothing'}"
        )
    return outcome(problems, "no credentialed endpoint for another site; PKCE required")


def code_expiry(test: Test) -> tuple[str, str]:
    """A device code and an authorization code expire unredeemed once
    their lifetime is over, and a device code may then not be approved."""
    lifetime = test.lifetimes.code
    if lifetime > SHORT:
        return SKIP, f"codes last {lifetime}s ('-p origin-short-tokens' shortens them)"
    problems: list[str] = []
    client, member, wanted = test.client("device"), test.user("member"), (f"{R}:/{ARCHIVE}",)
    approved = issuer.started(issuer.start_device(client, wanted), client)
    unapproved = issuer.started(issuer.start_device(client, wanted), client)
    for flow in (approved, unapproved):
        if flow.get("expires_in") != lifetime:
            problems.append(
                f"a device flow expires_in {flow.get('expires_in')!r}, not {lifetime}"
            )
    issuer.approve(client.url, str(approved["user_code"]), member)
    code_client = test.client("code")
    verifier, challenge = issuer.pkce()
    sent = issuer.authorize(code_client, member, wanted, challenge, secrets.token_urlsafe(12))
    auth_code = issuer.redirected(sent).get("code", "")
    issued = time.time()
    if not auth_code:
        problems.append(f"the authorization for a code: {sent.describe()}")
    # The codes were issued before `issued`, the device codes first.
    time.sleep(max(0.0, issued + lifetime + CODE_MARGIN - time.time()))
    polled = issuer.poll(client, str(approved["device_code"]))
    expired = ("expired_token", "invalid_grant")
    if polled.status != 400 or issuer.error_of(polled) not in expired:
        problems.append(f"redeeming an expired device code: {polled.describe()}")
    answer = issuer.decide(client.url, str(unapproved["user_code"]), member, "approve")
    if answer.status != 400:
        problems.append(f"approving an expired device code: {answer.describe()}, not 400")
    if auth_code:
        answer = issuer.code_tokens(code_client, auth_code, verifier)
        if not refused_with(answer, 400, "invalid_grant"):
            problems.append(
                f"redeeming an expired authorization code: {answer.describe()},"
                + " not invalid_grant"
            )
    return outcome(problems, f"device and authorization codes expired after {lifetime}s")


def registered(test: Test, metadata: dict[str, Any]) -> web.Response:
    """Register a client at the suite's issuer with metadata, waiting
    out the rate limit once if the issuer's other registrations have used
    it up."""
    answer = issuer.register(test.issuer_url, metadata)
    if answer.status == 429:
        time.sleep(REGISTRATION_WAIT)
        answer = issuer.register(test.issuer_url, metadata)
    return answer


def registration(test: Test) -> tuple[str, str]:
    """A client that registers itself (RFC 7591) gets the device and
    refresh grants alone, whatever it asks; may not name a redirect URI
    off this host, or a long name; is bound to the first user who
    approves a flow for it; and manages its registration (RFC 7592) only
    with its registration access token, which cannot widen its grants."""
    problems: list[str] = []
    name = f"pelican-test-framework registration {test.run}"
    grants = {issuer.DEVICE_GRANT, issuer.REFRESH_GRANT}
    made = registered(test, {"client_name": name, "grant_types": [issuer.CODE_GRANT]})
    record = issuer.decoded(made)
    client_id = str(record.get("client_id") or "")
    access = str(record.get("registration_access_token") or "")
    if made.status != 201 or not client_id or not access:
        problems.append(f"registering a client: {made.describe()}, with {sorted(record)}")
    elif set(common.as_array(record.get("grant_types")) or []) != grants:
        problems.append(f"asking for the code grant, it was given {record.get('grant_types')}")
    try:
        for metadata, status, error, what in (
            (
                {"client_name": name, "redirect_uris": ["https://evil.invalid/callback"]},
                403,
                "invalid_redirect_uri",
                "a redirect URI off this host",
            ),
            ({"client_name": "x" * 129}, 400, "invalid_client_metadata", "a 129-byte name"),
        ):
            answer = registered(test, metadata)
            if not refused_with(answer, status, error):
                problems.append(f"registering with {what}: {answer.describe()}, not {error}")
                stray = str(issuer.decoded(answer).get("client_id") or "")
                if stray:
                    issuer.delete_client(
                        test.web, f"/{test.namespace}", test.web_token("admin"), stray
                    )
        if client_id and access:
            client = issuer.client_of(
                test.web, f"/{test.namespace}", client_id, str(record.get("client_secret", ""))
            )
            problems += binding_problems(test, client)
            problems += configuration_problems(test, client_id, access, grants)
    finally:
        if client_id:
            gone = issuer.admin_call(
                test.web, f"/{test.namespace}", test.web_token("admin"), "DELETE", client_id
            )
            if gone.status not in (204, 404):
                warn(f"could not remove the registered client: {gone.describe()}")
    return outcome(problems, "grants restricted, URIs and names checked, bound, managed")


def binding_problems(test: Test, client: issuer.Client) -> list[str]:
    """What is wrong with a client that registered itself: test-member,
    the first to approve a flow for it, should get tokens through it, and
    test-analyst should then be refused it."""
    problems: list[str] = []
    try:
        toks = issuer.device_tokens(client, test.user("member"), (f"{R}:/{ARCHIVE}",))
        if issuer.scopes(toks.access) != frozenset({f"{R}:/{ARCHIVE}"}):
            problems.append(f"its token has {sorted(issuer.scopes(toks.access))}")
    except IssuerError as e:
        problems.append(f"test-member's flow with the registered client: {e}")
    flow = issuer.started(issuer.start_device(client, (f"{R}:/{ARCHIVE}",)), client)
    answer = issuer.decide(client.url, str(flow["user_code"]), test.user("analyst"), "approve")
    said = issuer.decoded(answer)
    if answer.status != 403 or said.get("error") != BOUND_ELSEWHERE:
        problems.append(
            f"test-analyst approving after test-member: {answer.describe()}, {said}"
        )
    polled = issuer.poll(client, str(flow["device_code"]))
    if not refused_with(polled, 400, "authorization_pending"):
        problems.append(f"polling after test-analyst's approval: {polled.describe()}")
    return problems


def configuration_problems(
    test: Test, client_id: str, access: str, grants: set[str]
) -> list[str]:
    """What is wrong with a registered client's configuration endpoint
    (RFC 7592): it should refuse no registration access token and a wrong
    one; with the right one (access), read the client, rename it but not
    widen its grants, and delete it, after which the token is nothing."""
    problems: list[str] = []
    url = test.issuer_url
    for method in ("GET", "PUT", "DELETE"):
        for tok, what in ((None, "no token"), ("wrong", "a wrong token")):
            body = {"client_name": "renamed"} if method == "PUT" else None
            answer = issuer.configuration(url, client_id, method, tok, body)
            if answer.status != 401:
                problems.append(f"{method} its configuration with {what}: {answer.describe()}")
    shown = issuer.configuration(url, client_id, "GET", access)
    said = issuer.decoded(shown)
    if shown.status != 200 or said.get("client_id") != client_id or "client_secret" in said:
        problems.append(f"GET its configuration: {shown.describe()}, with {sorted(said)}")
    wider = {"client_name": "renamed", "grant_types": [issuer.CODE_GRANT]}
    changed = issuer.configuration(url, client_id, "PUT", access, wider)
    said = issuer.decoded(changed)
    if changed.status != 200 or said.get("client_name") != "renamed":
        problems.append(
            f"PUT its configuration: {changed.describe()}, named {said.get('client_name')!r}"
        )
    if set(common.as_array(said.get("grant_types")) or []) != grants:
        problems.append(f"PUT asking for the code grant gave it {said.get('grant_types')}")
    gone = issuer.configuration(url, client_id, "DELETE", access)
    if gone.status != 204:
        problems.append(f"DELETE its configuration: {gone.describe()}, not 204")
    shown = issuer.configuration(url, client_id, "GET", access)
    if shown.status != 401:
        problems.append(f"GET its configuration once deleted: {shown.describe()}, not 401")
    return problems


RUN = {
    "discovery": discovery,
    "templates": templates,
    "requests": requests,
    "code": code,
    "clients": clients,
    "device": device,
    "data": data,
    "refresh": refresh,
    "revoked": revoked,
    "exchange": exchange,
    "expiry": expiry,
    "client": client_scenario,
    "isolation": isolation,
    "device-csrf": device_csrf,
    "admin-clients": admin_clients,
    "cors-and-pkce": cors_and_pkce,
    "code-expiry": code_expiry,
    "registration": registration,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Make the run's users, logins, groups, collection, and objects; run
    the selected scenarios; and remove what the run made."""
    test = Test(session)
    try:
        prepare(test)
    except (ApiError, IssuerError) as e:
        die(f"could not set up the suite's users, logins, and collection: {e}")
    try:
        for name in selected:
            try:
                status, note = RUN[name](test)
            except (ApiError, IssuerError) as e:
                status, note = FAIL, str(e)
            results.add(name, status, note)
    finally:
        finish(test)
