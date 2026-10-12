"""The registry, the federation's root of trust: it records which key
owns which namespace, and serves those keys to every director and cache
(registry/ in Pelican).

  register      registering prefixes, as `pelican namespace register`
                does: the prefixes it refuses, key chaining, a prefix
                with a LIKE wildcard in it, a prefix registered again,
                deleting a registration, approval, and the CLI itself
  jwks          the keys it serves for each export and server, each
                prefix's openid-configuration, an unknown prefix, and no
                private key material in any JWKS the federation serves
  registry-ui   its web API: what anyone, a user, and its admin may
                list and see, approving, denying, and its servers
  completeness  a server's registration, which starts Incomplete, and
                its completion with a token of the server's key
  ownership     who owns a registration: its key's holder claims it,
                its owner edits it once approved, or resubmits it once
                denied, and hands it on with an invite
  identity      each server's ID, as it, the registry, and the registry's
                list of servers know it
  downtime      downtimes recorded at the registry: whose they are, who
                may change them, and what the directors do with them

Every registration the suite makes has a prefix with `registry-test-` in
it (MARKER), its site name too, and every downtime it declares that in
its description. Before the scenarios and after them, it removes every
such registration, server, and downtime, a failed run's included, as
the registry's admin, or for a server's downtime, as that server.
"""

import functools
import json
import os
import shutil
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from testlib import accounts, common, credentials, director, registry, web
from testlib.common import first, warn
from testlib.registry import REGISTRATIONS, REGISTRY, SERVERS
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "register": "refused prefixes, key chaining, LIKE wildcards, again, deletion, the CLI",
    "jwks": "each prefix's keys and openid-configuration; no private key in any JWKS",
    "registry-ui": "listing, approving, and denying registrations; the list of servers",
    "completeness": "a server's registration starts Incomplete; completing it with its key",
    "ownership": "claiming a registration; an owner's edits; a denied one resubmitted; invites",
    "identity": "each server's ID agrees at the server, the registry, and its list",
    "downtime": "downtimes at the registry: whose they are, who may change them, routing",
}

# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

# What every prefix, site name, and downtime description of the suite's
# own has in it, by which it finds them to remove.
MARKER = "registry-test-"

# The ordinary user, whom the registry's admin creates once, and later
# runs reuse; and another, to whom `ownership` hands a registration.
USER = "test-registry"
HEIR = "test-registry-heir"

# How long the suite's web tokens last, in seconds.
TOKEN_LIFETIME = 3600

# A server's web API routes (web_ui/ui.go): its record of itself, and its
# downtimes; the registry serves the latter too, for every server.
LOCAL_METADATA = "/api/v1.0/server/localMetadata"
DOWNTIME = "/api/v1.0/downtime"
# The directors' views of the servers and their downtimes
# (director/director_ui.go).
DIRECTOR_SERVERS = "/api/v1.0/director_ui/servers"
DIRECTOR_DOWNTIMES = "/api/v1.0/director_ui/downtimes"

# How long a downtime takes to reach the directors, in seconds: they ask
# the registry every minute (Director.RegistryQueryInterval).
DOWNTIME_WAIT = 150
# How long a server's downtime takes to reach the registry: it tells the
# registry at once (mirrorDowntimeToRegistry), and otherwise with its
# next advertisement.
MIRROR_WAIT = 60
DAY_MS = 86_400_000
SEVERITY = "Outage (completely inaccessible)"

# What the registry must refuse to register (validatePrefix in
# registry/registry_validation.go, and its TestValidatePrefix), each
# with the run's tag in it, but `/`.
INVALID = ("/api/{t}", "/view/{t}", "/pelican/{t}", "/{t}/./b", "/{t}/../b", "/.{t}", "/")


@dataclass(frozen=True)
class Server:
    """An origin or a federated cache, which registers itself."""

    svc: str  # the compose service
    kind: str  # "origin" or "cache"
    web_url: str  # where its web API is
    key_dir: str  # the directory (under framework/var) of its keys

    @property
    def key(self) -> str:
        """Its active signing key."""
        return common.signing_key(self.key_dir)

    @property
    def host(self) -> str:
        """Its host, by which the directors name it."""
        return director.host(self.web_url)


def servers(fed: common.Federation) -> list[Server]:
    """Every origin, another owner's included, and every federated cache.
    A site-local cache registers nothing."""
    found = [Server(o.svc, "origin", o.web_url, f"issuer-keys/{o.svc}") for o in fed.origins]
    found += [Server(o.origin.svc, "origin", o.web_url, o.key_dir) for o in fed.owners]
    found += [
        Server(c.svc, "cache", c.web_url, f"issuer-keys/{c.svc}") for c in fed.federated_caches
    ]
    return found


def row(case: str, problems: Sequence[str], note: str) -> Row:
    """A row that passes with note, or fails with the first of problems."""
    return (case, FAIL, first(problems)) if problems else (case, PASS, note)


def guarded(check: Callable[[], list[Row]]) -> list[Row]:
    """check()'s rows; one failing row if it raised."""
    try:
        return check()
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]


def path_of(prefix: str) -> str:
    """prefix, as a URL's path quotes it."""
    return quote(prefix, safe="/:")


def millis() -> int:
    """Now, in milliseconds since the epoch, as a downtime's times are."""
    return int(time.time() * 1000)


class Test:
    """The registry, the shape's servers, and the keys, tokens, and users
    the scenarios share."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.registry = self.fed.registry_url
        self.registry_host = director.host(self.registry)
        self.tag = f"{MARKER}{session.run}"
        # fed.sh turns Registry.RequireKeyChaining off under
        # `topo-multi-owner`, whose origin-2 registers prefixes inside
        # origin-0's with a key of its own.
        self.chaining = not self.fed.owners
        self.servers = servers(self.fed)
        self.protected = next(
            (ns for ns in credentials.PROTECTED if ns in self.fed.exports), None
        )
        self._admin: Optional[accounts.Api] = None
        self._csrf: dict[str, str] = {}
        self._user: Optional[tuple[accounts.Api, str]] = None
        self._heir: Optional[tuple[accounts.Api, str]] = None
        self._server_admins: dict[str, accounts.Api] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._published: dict[str, Optional[frozenset[str]]] = {}

    # Keys and prefixes of the run's own.

    def key(self, name: str) -> str:
        """The run's key named name, made the first time."""
        return registry.new_key(self.session.path(f"registry-{name}.pem"))

    def prefix(self, name: str) -> str:
        """The run's prefix named name: /registry-test-<run>-<name>."""
        return f"/{self.tag}-{name}"

    def register(self, prefix: str, *keys: str, server_id: str = "") -> web.Response:
        """Register prefix with keys, the first signing (see
        registry.register()), under the run's site name."""
        return registry.register(self.registry, prefix, keys, self.tag, server_id)

    # The registry's users.

    def admin(self) -> accounts.Api:
        """The registry's web API, as its admin (credentials.mint_admin())."""
        if self._admin is None:
            self._admin = accounts.Api(
                self.registry,
                credentials.mint_user(
                    self.fed,
                    self.session.path(f"tokens/registry.admin.{self.registry_host}"),
                    credentials.local_issuer(self.fed, self.registry),
                    common.signing_key(f"issuer-keys/{self.registry_host}"),
                    "admin",
                    TOKEN_LIFETIME,
                ),
            )
        return self._admin

    def csrf(self) -> dict[str, str]:
        """The headers with which the admin may change things through the
        registry's web API, which guards against cross-site requests."""
        if not self._csrf:
            self._csrf = accounts.csrf(self.admin())
        return self._csrf

    def user(self) -> tuple[accounts.Api, str]:
        """The registry's web API as the ordinary user, whom its admin
        creates first, if need be, and the user's ID. The admin, and then
        the user, agree to the registry's AUP first, as webapi does."""
        if self._user is None:
            self._user = self._as_user(USER)
        return self._user

    def heir(self) -> tuple[accounts.Api, str]:
        """The registry's web API as the other ordinary user, HEIR, made
        as user() makes USER, and the user's ID."""
        if self._heir is None:
            self._heir = self._as_user(HEIR)
        return self._heir

    def _as_user(self, name: str) -> tuple[accounts.Api, str]:
        admin = self.admin()
        accounts.agree_to_aup(admin)
        user_id = accounts.ensure_user(admin, name)
        api = accounts.Api(
            self.registry,
            credentials.mint_user(
                self.fed,
                self.session.path(f"tokens/registry.{name}"),
                credentials.local_issuer(self.fed, self.registry),
                common.signing_key(f"issuer-keys/{self.registry_host}"),
                name,
                TOKEN_LIFETIME,
            ),
        )
        accounts.agree_to_aup(api)
        return api, user_id

    # What the registry holds.

    def get(self, path: str) -> web.Response:
        """A GET of path at the registry, with no credentials."""
        return web.request("GET", self.registry + path)

    def stored(self, prefix: str) -> Optional[dict[str, Any]]:
        """The registration of prefix, as anyone may read it
        (/api/v1.0/registry/<prefix>, getNamespaceHandler), pubkey and
        all; None if there is none."""
        answer = self.get(REGISTRY + path_of(prefix))
        if answer.status != 200:
            return None
        return common.as_object(registry.parse(answer.body))

    def stored_keys(self, prefix: str) -> Optional[frozenset[str]]:
        """The thumbprints of the keys registered for prefix; None if
        there is no registration, or its pubkey is no JWKS."""
        found = self.stored(prefix)
        return None if found is None else registry.thumbprints(found.get("pubkey"))

    def jwks(self, prefix: str) -> web.Response:
        """A GET of the JWKS the registry serves for prefix."""
        return self.get(f"{REGISTRY}{path_of(prefix)}/.well-known/issuer.jwks")

    def registration(self, reg_id: int) -> dict[str, Any]:
        """Registration reg_id, as the admin reads it."""
        return accounts.must(
            self.admin().call("GET", f"{REGISTRATIONS}/{reg_id}"),
            f"reading registration {reg_id}",
        ).object

    def status_of(self, prefix: str) -> str:
        """The status of prefix's registration, or "" if there is none."""
        found = self.stored(prefix)
        return "" if found is None else str(registry.admin_metadata(found).get("status", ""))

    def set_status(self, prefix: str, action: str) -> Optional[str]:
        """approve or deny prefix's registration, as the admin; what went
        wrong, or None."""
        found = self.stored(prefix)
        if found is None:
            return f"{prefix} is not registered"
        answer = self.admin().call(
            "PATCH", f"{REGISTRATIONS}/{found.get('id')}/{action}", headers=self.csrf()
        )
        return None if answer.ok else f"{action} {prefix}: {answer.describe()}"

    # The shape's servers.

    def server_admin(self, server: Server) -> accounts.Api:
        """server's web API, as its admin."""
        if server.web_url not in self._server_admins:
            self._server_admins[server.web_url] = accounts.Api(
                server.web_url,
                credentials.mint_user(
                    self.fed,
                    self.session.path(f"tokens/registry.admin.{server.host}"),
                    credentials.local_issuer(self.fed, server.web_url),
                    server.key,
                    "admin",
                    TOKEN_LIFETIME,
                ),
            )
        return self._server_admins[server.web_url]

    def metadata(self, server: Server) -> dict[str, Any]:
        """What server records of itself, as the registry told it: its ID,
        name, and type (HandleGetServerLocalMetadata), as its admin reads
        it. Raises accounts.ApiError if it won't say."""
        if server.web_url not in self._metadata:
            answer = accounts.must(
                self.server_admin(server).call("GET", LOCAL_METADATA),
                f"{server.svc}'s {LOCAL_METADATA}",
            )
            self._metadata[server.web_url] = answer.object
        return self._metadata[server.web_url]

    def server_prefix(self, server: Server) -> str:
        """server's registry prefix: an origin's is
        /origins/<host>:<port> of its web URL (GetOriginNs), and a cache's
        /caches/<name> (GetCacheNs)."""
        if server.kind == "origin":
            return f"/origins/{urlsplit(server.web_url).netloc}"
        return f"/caches/{self.metadata(server).get('name', '')}"

    def export_servers(self) -> dict[str, Server]:
        """Each export's prefix, and the origin that registered it: the
        origins' exports the first origin, whose keys the others share;
        another owner's, its own origin."""
        found = {f"/{ns}": self.servers[0] for ns in self.fed.exports}
        for owner in self.fed.owners:
            server = next(s for s in self.servers if s.svc == owner.origin.svc)
            for export in owner.exports:
                found[export.prefix] = server
        return found

    def published(self, server: Server) -> Optional[frozenset[str]]:
        """The thumbprints of the keys server publishes itself
        (/.well-known/issuer.jwks), which it registers
        (GetIssuerPublicJWKS): its key directory's, and those of
        Server.IssuerJwks, which the origins set; None if it won't say."""
        if server.web_url not in self._published:
            answer = web.request("GET", f"{server.web_url}/.well-known/issuer.jwks")
            found = registry.thumbprints(registry.parse(answer.body))
            self._published[server.web_url] = found if answer.status == 200 else None
        return self._published[server.web_url]

    def server_token(self, server: Server, scope: str, server_id: str) -> str:
        """A token the registry takes from a registered server
        (checkRegisteredServer in token/token_verify.go): subject
        server_id, for the registry's host and port, of scope, signed with
        server's own key."""
        return registry.token(
            server.key,
            scope,
            server_id,
            server.web_url,
            audience=urlsplit(self.registry).netloc,
        )


# --------------------------------------------------------------------------
# register


def register(test: Test) -> list[Row]:
    """The registration of prefixes of the run's own, as a server or
    `pelican namespace register` does, through the protocol itself, so
    that each status the registry answers is seen."""
    k1, k2 = test.key("k1"), test.key("k2")
    rows = [invalid(test, k1)]
    rows += chaining(test, k1)
    rows += like_decoys(test, k1, k2)
    rows.append(again(test, k2))
    rows += deletion(test, k1, k2)
    rows.append(approval(test, k1))
    rows += cli(test, k1, k2)
    return rows


def invalid(test: Test, key: str) -> Row:
    """Each prefix that validatePrefix refuses gets 400."""
    problems: list[str] = []
    for pattern in INVALID:
        prefix = pattern.format(t=test.tag)
        answer = test.register(prefix, key)
        if answer.status != 400:
            problems.append(f"{prefix}: {answer.describe()}, not 400")
    return row("invalid", problems, f"{len(INVALID)} prefixes refused (400)")


def chaining(test: Test, k1: str) -> list[Row]:
    """With key chaining on, a prefix inside a protected export's may be
    registered only with a key of the export's (validateKeyChaining):
    with a key no one has registered, 403; with the origins' own, 201.
    One that only begins with the export's name is no child of it, and
    takes any key. With chaining off (`topo-multi-owner`), the first is
    registered too."""
    if test.protected is None:
        return [("chaining", SKIP, "no protected namespace is exported")]
    export = f"/{test.protected}"
    owner_key = test.export_servers()[export].key
    child, sibling = f"{export}/{test.tag}", f"{export}-{test.tag}"
    rows: list[Row] = []
    foreign = test.register(child, k1)
    if test.chaining:
        rows.append(
            row(
                "child-foreign",
                [] if foreign.status == 403 else [f"{child}: {foreign.describe()}, not 403"],
                f"{child}, with a key no one registered: 403",
            )
        )
        owned = test.register(child, owner_key)
        rows.append(
            row(
                "child-owner",
                [] if owned.status == 201 else [f"{child}: {owned.describe()}, not 201"],
                f"{child}, with {export}'s key: 201",
            )
        )
    else:
        rows.append(
            row(
                "child-foreign",
                [] if foreign.status == 201 else [f"{child}: {foreign.describe()}, not 201"],
                f"{child}, with any key, since key chaining is off: 201",
            )
        )
        rows.append(("child-owner", SKIP, "key chaining is off"))
    answer = test.register(sibling, k1)
    rows.append(
        row(
            "sibling",
            [] if answer.status == 201 else [f"{sibling}: {answer.describe()}, not 201"],
            f"{sibling}, with any key: 201",
        )
    )
    return rows


def like_decoys(test: Test, k1: str, k2: str) -> list[Row]:
    """A registered prefix with a LIKE wildcard in it (`_` or `%`) must not
    enclose one it only matches as a pattern: the registry finds a
    prefix's superspaces with `? LIKE (prefix || '/%')`
    (namespaceSupSubChecks in registry/registry_db.go), which takes a
    registered /x_b for the parent of /xYb/c, and so refuses the latter
    another key."""
    if not test.chaining:
        why = "key chaining is off, so no superspace is consulted"
        return [("like-underscore", SKIP, why), ("like-percent", SKIP, why)]
    rows: list[Row] = []
    for case, decoy, victim in (
        ("like-underscore", test.prefix("a_b"), test.prefix("aXb/c")),
        ("like-percent", test.prefix("p%q"), test.prefix("pXYZq/c")),
    ):
        first_answer = test.register(decoy, k1)
        if first_answer.status != 201:
            rows.append(
                (case, FAIL, f"registering {decoy}: {first_answer.describe()}, not 201")
            )
            continue
        answer = test.register(victim, k2)
        rows.append(
            row(
                case,
                (
                    []
                    if answer.status == 201
                    else [
                        f"{victim}, with another key: {answer.describe()}, not 201;"
                        + f" {decoy} does not enclose it"
                    ]
                ),
                f"{victim} beside {decoy}, with another key: 201",
            )
        )
    return rows


def again(test: Test, k2: str) -> Row:
    """A protected export's prefix, registered again with another key:
    200, already registered, with its keys as they were."""
    if test.protected is None:
        return ("again", SKIP, "no protected namespace is exported")
    export = f"/{test.protected}"
    before = test.stored(export)
    if before is None:
        return ("again", FAIL, f"{export} is not registered")
    jwks_before = test.jwks(export)
    answer = test.register(export, k2)
    problems: list[str] = []
    message = str((common.as_object(registry.parse(answer.body)) or {}).get("message", ""))
    if answer.status != 200 or "already registered" not in message:
        problems.append(
            f"{answer.describe()} ({message or 'no message'}), not 200, already registered"
        )
    after = test.stored(export)
    if after is None or after.get("pubkey") != before.get("pubkey"):
        problems.append(f"{export}'s registered keys changed")
    jwks_after = test.jwks(export)
    if (jwks_after.status, jwks_after.body) != (jwks_before.status, jwks_before.body):
        problems.append(f"{export}'s JWKS changed")
    return row("again", problems, f"{export}, with another key: 200, keys unchanged")


def deletion(test: Test, k1: str, k2: str) -> list[Row]:
    """DELETE /api/v1.0/registry/<prefix> takes a token of
    pelican.namespace_delete signed with a key registered for the prefix
    (deleteNamespaceHandler), as `pelican namespace delete` mints one:
    signed with another key, 403; with the registered one, 200, after
    which the prefix has no JWKS (404). A prefix that was never
    registered gets 400, as one JSON document; Pelican writes that and
    goes on, and writes another."""
    prefix, never = test.prefix("del"), test.prefix("never")
    rows: list[Row] = []
    made = test.register(prefix, k1)
    if made.status != 201:
        return [("delete", FAIL, f"registering {prefix}: {made.describe()}, not 201")]
    url = test.registry + REGISTRY + path_of(prefix)

    def delete(key: str, target: str = url) -> web.Response:
        tok = registry.token(key, registry.NAMESPACE_DELETE, "origin", url, audience="registry")
        return web.request("DELETE", target, token=tok)

    foreign = delete(k2)
    rows.append(
        row(
            "delete-foreign",
            [] if foreign.status == 403 else [f"{foreign.describe()}, not 403"],
            f"{prefix}, with another key's token: 403",
        )
    )
    owned = delete(k1)
    problems = [] if owned.status == 200 else [f"{owned.describe()}, not 200"]
    gone = test.jwks(prefix)
    if gone.status != 404:
        problems.append(f"its JWKS afterward: {gone.describe()}, not 404")
    rows.append(row("delete-owner", problems, f"{prefix}, with its key's token: 200, then 404"))
    unknown = delete(k1, test.registry + REGISTRY + path_of(never))
    problems = [] if unknown.status == 400 else [f"{unknown.describe()}, not 400"]
    count = registry.documents(unknown.body)
    if count != 1:
        problems.append(f"{count} JSON documents in the body, not 1")
    rows.append(row("delete-unknown", problems, f"{never}, never registered: 400, one body"))
    return rows


def approval(test: Test, key: str) -> Row:
    """A new registration, which starts Incomplete (keySignChallengeCommit),
    serves its keys at once; but under `-p registry-approval`
    (Registry.RequireOriginApproval), not until the admin approves it:
    403 before, and then its keys."""
    prefix = test.prefix("approval")
    made = test.register(prefix, key)
    if made.status != 201:
        return ("approval", FAIL, f"registering {prefix}: {made.describe()}, not 201")
    want = registry.key_thumbprints([key])
    problems: list[str] = []
    status = test.status_of(prefix)
    if status != "Incomplete":
        problems.append(f"its status is {status or 'missing'}, not Incomplete")
    if test.fed.registry_approval:
        early = test.jwks(prefix)
        if early.status != 403:
            problems.append(f"its JWKS before approval: {early.describe()}, not 403")
        error = test.set_status(prefix, "approve")
        if error:
            return ("approval", FAIL, error)
    served = test.jwks(prefix)
    if served.status != 200 or registry.thumbprints(registry.parse(served.body)) != want:
        problems.append(f"its JWKS: {served.describe()}, not 200 with its key")
    if test.fed.registry_approval:
        return row(
            "approval", problems, f"{prefix}: Incomplete, 403 until approved, then its key"
        )
    return row("approval", problems, f"{prefix}: Incomplete, and its key served at once")


def cli_env(test: Test, key_dir: str) -> dict[str, str]:
    """The environment for `pelican namespace` to register under the run's
    site name with the keys in key_dir alone: IssuerKeysDirectory, and no
    legacy IssuerKey file (config.loadPEMFiles)."""
    env = dict(test.session.env)
    env["PELICAN_ISSUERKEYSDIRECTORY"] = os.path.abspath(key_dir)
    env["PELICAN_ISSUERKEY"] = os.path.abspath(os.path.join(key_dir, "absent.jwk"))
    env["PELICAN_XROOTD_SITENAME"] = test.tag
    env["PELICAN_FEDERATION_REGISTRYURL"] = test.registry
    return env


def key_dir_of(test: Test, name: str, keys: Sequence[str]) -> str:
    """A directory of the session's, named name, holding keys alone."""
    path = test.session.path(f"registry-keys-{name}")
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, mode=0o700)
    for key in keys:
        shutil.copy(key, os.path.join(path, os.path.basename(key)))
    return path


def cli(test: Test, k1: str, k2: str) -> list[Row]:
    """`pelican namespace register` and `delete` (cmd/registry_client.go),
    with the key the client is configured with: it must register the
    prefix with that key, and delete it. Its --privkey flag names the key
    to sign with, and must be honored; Pelican binds it to IssuerKey only
    if it has a value when the command is defined, which it never does,
    so the client signs with a key of its configuration, or one it makes,
    and the `cli-flags` row fails."""
    rows: list[Row] = []
    prefix = test.prefix("cli")
    env = cli_env(test, key_dir_of(test, "cli", [k1]))
    code, out, err = test.session.pelican_cmd(
        "namespace", "register", "--prefix", prefix, env=env
    )
    problems = [] if code == 0 else [f"register: exit {code}: {last_line(err or out)}"]
    if not problems and test.stored_keys(prefix) != registry.key_thumbprints([k1]):
        problems.append(f"{prefix} is not registered with the configured key alone")
    if not problems:
        code, out, err = test.session.pelican_cmd(
            "namespace", "delete", "--prefix", prefix, env=env
        )
        if code != 0:
            problems.append(f"delete: exit {code}: {last_line(err or out)}")
        elif test.stored(prefix) is not None:
            problems.append(f"{prefix} is still registered after delete")
    rows.append(row("cli", problems, f"{prefix}: registered with the configured key, deleted"))
    prefix = test.prefix("cli-flags")
    public = registry.write_public_key(k2, test.session.path("registry-k2.pub"))
    env = cli_env(test, key_dir_of(test, "cli-flags", []))
    code, out, err = test.session.pelican_cmd(
        "namespace",
        "register",
        "--prefix",
        prefix,
        "--privkey",
        os.path.abspath(k2),
        "--pubkey",
        os.path.abspath(public),
        "--namespace-url",
        test.registry,
        env=env,
    )
    problems = [] if code == 0 else [f"register: exit {code}: {last_line(err or out)}"]
    if not problems:
        found = test.stored_keys(prefix)
        if found is None:
            problems.append(f"{prefix} is not registered")
        elif found != registry.key_thumbprints([k2]):
            problems.append(f"{prefix} is registered with a key other than --privkey's")
    rows.append(row("cli-flags", problems, f"{prefix}: registered with --privkey's key"))
    return rows


# --------------------------------------------------------------------------
# jwks


def jwks(test: Test) -> list[Row]:
    """The keys the registry serves for each prefix
    (/api/v1.0/registry/<prefix>/.well-known/issuer.jwks, the wildcard
    handler's issuer.jwks branch), and the openid-configuration that
    names them; and private key material in any JWKS the federation
    serves."""
    exports = test.export_servers()
    rows: list[Row] = []
    problems: list[str] = []
    for prefix, server in exports.items():
        problems += served_problems(test, prefix, server, exact=True)
    rows.append(
        row("exports", problems, f"{len(exports)} export(s): their origin's own keys, exactly")
    )
    problems = []
    for server in test.servers:
        # A cache never tells the registry of a key it adds since it
        # registered, which this does not check: of a cache's, only that
        # every key served is its own.
        prefix = test.server_prefix(server)
        problems += served_problems(test, prefix, server, exact=server.kind == "origin")
    rows.append(
        row(
            "servers",
            problems,
            f"{len(test.servers)} server(s): each origin's own keys, and only each cache's",
        )
    )
    problems = []
    prefixes = list(exports) + [test.server_prefix(s) for s in test.servers]
    for prefix in prefixes:
        problems += openid_problems(test, prefix)
    rows.append(row("openid", problems, f"{len(prefixes)} prefix(es) name their own JWKS"))
    unknown = test.prefix("unknown")
    problems = []
    for what, answer in (
        ("JWKS", test.jwks(unknown)),
        (
            "openid-configuration",
            test.get(f"{REGISTRY}{unknown}/.well-known/openid-configuration"),
        ),
    ):
        if answer.status != 404:
            problems.append(f"its {what}: {answer.describe()}, not 404")
    rows.append(row("unknown", problems, f"{unknown}: 404 for both"))
    rows.append(private_keys(test, prefixes))
    return rows


def served_problems(test: Test, prefix: str, server: Server, exact: bool) -> list[str]:
    """What is wrong with the JWKS the registry serves for prefix, which
    server registered: with exact, it must hold the keys server publishes
    itself (see Test.published()), and nothing else; without, only some
    of them. Those must include every key in server's key directory."""
    own = test.published(server)
    if own is None:
        return [f"{server.svc}'s own JWKS did not answer"]
    problems: list[str] = []
    missing = registry.key_thumbprints(registry.dir_keys(server.key_dir)) - own
    if missing:
        problems.append(f"{server.svc}'s own JWKS lacks {len(missing)} of its keys")
    answer = test.jwks(prefix)
    have = registry.thumbprints(registry.parse(answer.body))
    if answer.status != 200 or have is None:
        return problems + [f"{prefix}'s JWKS: {answer.describe()}"]
    if exact and have != own:
        problems.append(
            f"{prefix}'s JWKS holds {len(have & own)} of {server.svc}'s {len(own)} key(s),"
            + f" and {len(have - own)} other(s)"
        )
    if not exact and (not have or not have <= own):
        problems.append(
            f"{prefix}'s JWKS holds {len(have - own)} key(s) not {server.svc}'s, of {len(have)}"
        )
    return problems


def openid_problems(test: Test, prefix: str) -> list[str]:
    """What is wrong with prefix's openid-configuration at the registry:
    its jwks_uri must be the registry's JWKS of prefix."""
    answer = test.get(f"{REGISTRY}{path_of(prefix)}/.well-known/openid-configuration")
    if answer.status != 200:
        return [f"{prefix}'s openid-configuration: {answer.describe()}"]
    found = str((common.as_object(registry.parse(answer.body)) or {}).get("jwks_uri", ""))
    parts = urlsplit(found)
    want = urlsplit(f"{test.registry}{REGISTRY}{prefix}/.well-known/issuer.jwks")
    if (parts.scheme, parts.netloc) != (want.scheme, want.netloc) or (
        parts.path.replace("%3A", ":") != want.path
    ):
        return [f"{prefix}'s jwks_uri is {found!r}"]
    return []


def private_keys(test: Test, prefixes: Sequence[str]) -> Row:
    """No JWKS the federation serves may hold private key material (any of
    registry.PRIVATE_MEMBERS): each server's own; each namespace issuer's,
    by its openid-configuration; the registry's for each prefix; the
    registrations' keys, as it lists them all (/api/v1.0/registry), as
    it serves each one (/api/v1.0/registry/<prefix>), as its admin
    downloads each (registry_ui's /namespaces/:id/pubkey), and as its
    list of servers shows them."""
    documents: dict[str, Any] = {}
    problems: list[str] = []

    def fetch(name: str, url: str) -> Any:
        answer = web.request("GET", url)
        if answer.status != 200:
            problems.append(f"{name}: {answer.describe()}")
            return None
        found = registry.parse(answer.body)
        if found is None:
            problems.append(f"{name}: not JSON")
        return found

    urls = {s.web_url for s in test.servers} | {c.web_url for c in test.fed.caches}
    urls |= set(test.fed.directors) | {test.registry}
    for url in sorted(urls):
        documents[f"{director.host(url)}'s JWKS"] = fetch(
            f"{director.host(url)}'s JWKS", f"{url}/.well-known/issuer.jwks"
        )
    for issuer in namespace_issuers(test.fed):
        config = common.as_object(fetch(issuer, f"{issuer}/.well-known/openid-configuration"))
        uri = str((config or {}).get("jwks_uri", ""))
        if config is not None and not uri:
            problems.append(f"{issuer}'s openid-configuration names no jwks_uri")
        elif uri:
            documents[f"{issuer}'s jwks_uri"] = fetch(uri, uri)
    for prefix in prefixes:
        documents[f"the registry's JWKS of {prefix}"] = fetch(
            f"the registry's JWKS of {prefix}",
            f"{test.registry}{REGISTRY}{path_of(prefix)}/.well-known/issuer.jwks",
        )
    listed = common.as_array(fetch("/api/v1.0/registry", test.registry + REGISTRY)) or []
    for entry in (common.as_object(e) or {} for e in listed):
        prefix, reg_id = str(entry.get("prefix", "")), entry.get("id")
        documents[f"{prefix}, as listed"] = entry.get("pubkey")
        documents[f"{prefix}, as served"] = (
            common.as_object(
                fetch(f"{prefix}'s registration", test.registry + REGISTRY + path_of(prefix))
            )
            or {}
        ).get("pubkey")
        answer = test.admin().call("GET", f"{REGISTRATIONS}/{reg_id}/pubkey")
        if answer.status != 200:
            problems.append(f"{prefix}'s pubkey download: {answer.describe()}")
        documents[f"{prefix}'s pubkey download"] = answer.data
    for server in common.as_array(fetch(SERVERS, test.registry + SERVERS)) or []:
        found = common.as_object(server) or {}
        for reg in common.as_array(found.get("registration")) or []:
            entry = common.as_object(reg) or {}
            documents[f"server {found.get('name')}'s {entry.get('prefix')}"] = entry.get(
                "pubkey"
            )
    for name, document in documents.items():
        if document is None:
            continue
        if registry.as_jwks(document) is None:
            problems.append(f"{name} is no JWKS")
        problems += [
            f"{name} holds private key material: {p}"
            for p in registry.private_members(document)
        ]
    read = sum(1 for d in documents.values() if d is not None)
    return row("private", problems, f"{read} JWKS documents, none with private key material")


def namespace_issuers(fed: common.Federation) -> list[str]:
    """The issuer URL of each exported namespace that has one: the
    protected ones, and another owner's exports with issuers."""
    found = {
        fed.issuer_of(ns)
        for ns in fed.exports
        if credentials.has_issuer(fed.exports[ns]) and ns in fed.issuers
    }
    for owner in fed.owners:
        found |= {e.issuer for e in owner.exports if credentials.has_issuer(e.caps)}
    return sorted(found)


# --------------------------------------------------------------------------
# registry-ui


def registry_ui(test: Test) -> list[Row]:
    """The registry's web API (/api/v1.0/registry_ui; registry_ui.go):
    listing registrations (listNamespaces), seeing one (getNamespace),
    approving and denying them (updateNamespaceStatus), and its servers
    (listServersHandler, getServerHandler)."""
    key = test.key("k1")
    listed, unlisted = test.prefix("listed"), test.prefix("unlisted")
    rows: list[Row] = []
    for prefix in (listed, unlisted):
        made = test.register(prefix, key)
        if made.status != 201:
            return [("", FAIL, f"registering {prefix}: {made.describe()}, not 201")]
    error = test.set_status(listed, "approve")
    if error:
        return [("", FAIL, error)]
    rows.append(public_listing(test, listed, unlisted))
    answer = test.get(f"{REGISTRATIONS}?status=Pending")
    rows.append(
        row(
            "list-pending",
            [] if answer.status == 403 else [f"{answer.describe()}, not 403"],
            "?status=Pending with no credentials: 403",
        )
    )
    answer = test.get(f"{REGISTRATIONS}?prefixType=bogus")
    rows.append(
        row(
            "prefix-type",
            [] if answer.status == 400 else [f"{answer.describe()}, not 400"],
            "?prefixType=bogus: 400",
        )
    )
    rows.append(bad_status(test))
    rows.append(non_owner(test))
    rows.append(unknown_id(test))
    rows.append(denied(test, key))
    rows += server_list(test)
    return rows


def public_listing(test: Test, listed: str, unlisted: str) -> Row:
    """With no credentials, the registrations are Approved ones alone,
    without their keys: the run's approved one, but not its incomplete
    one."""
    answer = test.get(REGISTRATIONS)
    if answer.status != 200:
        return ("list-public", FAIL, f"{answer.describe()}, not 200")
    entries = [
        common.as_object(e) or {} for e in common.as_array(registry.parse(answer.body)) or []
    ]
    problems: list[str] = []
    for entry in entries:
        prefix, status = entry.get("prefix"), registry.admin_metadata(entry).get("status")
        if status != "Approved":
            problems.append(f"{prefix} is listed, but {status}")
        if "pubkey" in entry:
            problems.append(f"{prefix} is listed with its pubkey")
    prefixes = {e.get("prefix") for e in entries}
    if listed not in prefixes:
        problems.append(f"{listed}, approved, is not listed")
    if unlisted in prefixes:
        problems.append(f"{unlisted}, incomplete, is listed")
    return row("list-public", problems, f"{len(entries)} listed, all Approved, without keys")


def bad_status(test: Test) -> Row:
    """As the admin, ?status=Bogus gets 400, as one JSON document. Pelican
    writes that and goes on, and writes the list too (R2)."""
    answer = web.request(
        "GET", f"{test.registry}{REGISTRATIONS}?status=Bogus", token=test.admin().token
    )
    problems = [] if answer.status == 400 else [f"{answer.describe()}, not 400"]
    count = registry.documents(answer.body)
    if count != 1:
        problems.append(f"{count} JSON documents in the body, not 1")
    return row("bad-status", problems, "?status=Bogus as the admin: 400, one body")


def non_owner(test: Test) -> Row:
    """An ordinary user may not see a registration that isn't theirs: an
    export's, which no user owns (403)."""
    if test.protected is None:
        return ("non-owner", SKIP, "no protected namespace is exported")
    found = test.stored(f"/{test.protected}")
    if found is None:
        return ("non-owner", FAIL, f"/{test.protected} is not registered")
    api, _ = test.user()
    answer = api.call("GET", f"{REGISTRATIONS}/{found.get('id')}")
    return row(
        "non-owner",
        [] if answer.status == 403 else [f"{answer.describe()}, not 403"],
        f"/{test.protected}'s registration, as {USER}: 403",
    )


def unknown_id(test: Test) -> Row:
    """Approving or denying a registration that doesn't exist gets 404."""
    ids = [
        int(e.get("id", 0))
        for e in (
            common.as_object(x) or {}
            for x in common.as_array(registry.parse(test.get(REGISTRY).body)) or []
        )
    ]
    unknown = max(ids, default=0) + 100000
    problems: list[str] = []
    for action in ("approve", "deny"):
        answer = test.admin().call(
            "PATCH", f"{REGISTRATIONS}/{unknown}/{action}", headers=test.csrf()
        )
        if answer.status != 404:
            problems.append(f"{action}: {answer.describe()}, not 404")
    return row("unknown-id", problems, f"approve and deny of registration {unknown}: 404")


def denied(test: Test, key: str) -> Row:
    """Under `-p registry-approval`, a denied registration's JWKS gets
    403."""
    if not test.fed.registry_approval:
        return ("denied", SKIP, "the registry serves every registration's keys (no approval)")
    prefix = test.prefix("denied")
    made = test.register(prefix, key)
    if made.status != 201:
        return ("denied", FAIL, f"registering {prefix}: {made.describe()}, not 201")
    error = test.set_status(prefix, "deny")
    if error:
        return ("denied", FAIL, error)
    answer = test.jwks(prefix)
    return row(
        "denied",
        [] if answer.status == 403 else [f"its JWKS: {answer.describe()}, not 403"],
        f"{prefix}, denied: its JWKS 403",
    )


def server_list(test: Test) -> list[Row]:
    """/servers lists each of the shape's servers once, with its
    registry prefix among its registrations; /servers/<id> is each; an ID
    no server has gets 404."""
    answer = test.get(SERVERS)
    if answer.status != 200:
        return [("servers", FAIL, f"{SERVERS}: {answer.describe()}")]
    listed = [
        common.as_object(s) or {} for s in common.as_array(registry.parse(answer.body)) or []
    ]
    problems: list[str] = []
    prefixes: dict[str, list[str]] = {}
    for entry in listed:
        for reg in common.as_array(entry.get("registration")) or []:
            prefix = str((common.as_object(reg) or {}).get("prefix", ""))
            prefixes.setdefault(prefix, []).append(str(entry.get("id", "")))
    wanted = sorted({test.server_prefix(s) for s in test.servers})
    for prefix in wanted:
        holders = prefixes.get(prefix, [])
        if len(holders) != 1:
            problems.append(
                f"{prefix} is a registration of {len(holders)} listed servers, not 1"
            )
    names = [str(e.get("name", "")) for e in listed]
    problems += [
        f"{n} is listed {names.count(n)} times"
        for n in sorted(set(names))
        if names.count(n) > 1
    ]
    rows = [
        row("servers", problems, f"{len(wanted)} server prefix(es), each of one server, once")
    ]
    problems = []
    for prefix in wanted:
        for server_id in prefixes.get(prefix, [])[:1]:
            one = test.get(f"{SERVERS}/{quote(server_id)}")
            found = common.as_object(registry.parse(one.body)) or {}
            if one.status != 200 or found.get("id") != server_id:
                problems.append(f"{SERVERS}/{server_id}: {one.describe()}")
    unknown = test.get(f"{SERVERS}/{MARKER}none")
    if unknown.status != 404:
        problems.append(f"{SERVERS}/{MARKER}none: {unknown.describe()}, not 404")
    rows.append(row("server-one", problems, "each server by its ID; an unknown ID 404"))
    return rows


# --------------------------------------------------------------------------
# completeness


def completeness(test: Test) -> list[Row]:
    """A server's registration starts Incomplete (keySignChallengeCommit),
    and the completeness check (checkStatusHandler) says so, with the
    URL of the page to complete it on. Its holder completes it through
    the web API (PUT /namespaces/:id, createUpdateNamespace) with an
    access_token of registry.edit_registration signed by the
    registration's key, by which a user who doesn't own it claims it:
    with any other key, 403; with its own, it becomes the user's, not the
    owner the body names, and Pending. Claiming is for good, so the suite
    completes a server registration of the run's own,
    /origins/registry-test-<run>.invalid, not one of the shape's."""
    rows = [shape_completeness(test)]
    key = test.key("server")
    prefix = f"/origins/{test.tag}.invalid"
    made = test.register(prefix, key, server_id=uuid.uuid4().hex[:7])
    if made.status != 201:
        return rows + [
            ("incomplete", FAIL, f"registering {prefix}: {made.describe()}, not 201")
        ]
    stored = test.stored(prefix) or {}
    reg_id = int(stored.get("id", 0))
    meta = registry.admin_metadata(stored)
    problems: list[str] = []
    if meta.get("status") != "Incomplete":
        problems.append(f"its status is {meta.get('status')}, not Incomplete")
    if meta.get("user_id"):
        problems.append(f"it is owned, by {meta.get('user_id')}")
    problems += check_problems(test, {prefix: reg_id}, complete=False, page="claim")
    rows.append(row("incomplete", problems, f"{prefix}: Incomplete, unowned, the claim page"))
    api, user_id = test.user()
    decoy = f"{MARKER}not-the-owner"
    body = {
        "prefix": prefix,
        "pubkey": stored.get("pubkey", ""),
        "admin_metadata": {
            "description": f"pelican-test-framework {test.tag}",
            "institution": "pelican-test-framework",
            "site_name": test.tag,
            "user_id": decoy,
        },
    }
    headers = accounts.csrf(api)

    def put(signer: str) -> accounts.Answer:
        tok = registry.token(
            signer, registry.EDIT_REGISTRATION, "admin", f"https://{test.tag}.invalid:8444"
        )
        return api.call("PUT", f"{REGISTRATIONS}/{reg_id}?access_token={tok}", body, headers)

    answer = put(test.key("k2"))
    problems = [] if answer.status == 403 else [f"{answer.describe()}, not 403"]
    after = registry.admin_metadata(test.registration(reg_id))
    if after.get("status") != "Incomplete" or after.get("user_id"):
        problems.append(
            f"it is now {after.get('status')}, owned by {after.get('user_id') or 'no one'}"
        )
    rows.append(row("foreign-token", problems, f"as {USER}, with another key's token: 403"))
    answer = put(key)
    problems = [] if answer.ok else [f"{answer.describe()}, not 200"]
    after = registry.admin_metadata(test.registration(reg_id))
    if after.get("status") != "Pending":
        problems.append(f"its status is {after.get('status')}, not Pending")
    if after.get("user_id") != user_id:
        problems.append(
            f"it is owned by {after.get('user_id') or 'no one'}, not {USER}"
            + (" (the body's owner)" if after.get("user_id") == decoy else "")
        )
    problems += check_problems(test, {prefix: reg_id}, complete=True, page="origin/edit")
    rows.append(
        row(
            "own-token",
            problems,
            f"as {USER}, with its key's token: {USER}'s, Pending, complete",
        )
    )
    return rows


def shape_completeness(test: Test) -> Row:
    """The completeness check of each of the shape's server registrations:
    incomplete, since no one has filled in its form, with a page to do
    so; and where nothing needs approval, Incomplete. (Under
    `-p registry-approval`, the federation suite has approved them.)"""
    prefixes: dict[str, int] = {}
    problems: list[str] = []
    for server in test.servers:
        prefix = test.server_prefix(server)
        stored = test.stored(prefix)
        if stored is None:
            problems.append(f"{prefix} is not registered")
            continue
        prefixes[prefix] = int(stored.get("id", 0))
        status = registry.admin_metadata(stored).get("status")
        if not test.fed.registry_approval and status != "Incomplete":
            problems.append(f"{prefix} is {status}, not Incomplete")
    problems += check_problems(test, prefixes, complete=False, page="")
    return row("check-status", problems, f"{len(prefixes)} server registration(s): incomplete")


def check_problems(
    test: Test, prefixes: Mapping[str, int], complete: bool, page: str
) -> list[str]:
    """What is wrong with the completeness check of prefixes (by their
    registrations' IDs): each must be complete or not, as complete says,
    with the URL of a page of the registry's for its registration, and
    if page names one, that one (claim/, or origin/edit/)."""
    answer = web.request(
        "POST",
        test.registry + registry.CHECK_STATUS,
        upload=json.dumps({"prefixes": list(prefixes)}).encode(),
        headers=registry.JSON,
    )
    if answer.status != 200:
        return [f"the completeness check: {answer.describe()}"]
    results = (
        common.as_object((common.as_object(registry.parse(answer.body)) or {}).get("results"))
        or {}
    )
    problems: list[str] = []
    for prefix, reg_id in prefixes.items():
        found = common.as_object(results.get(prefix)) or {}
        if found.get("complete") is not complete:
            problems.append(f"{prefix}: complete is {found.get('complete')}, not {complete}")
        url = str(found.get("edit_url", ""))
        base = f"{test.registry}/view/registry/{page}"
        if not url.startswith(base) or not url.endswith(f"/?id={reg_id}"):
            problems.append(f"{prefix}'s edit_url is {url!r}, not {base}.../?id={reg_id}")
    return problems


# --------------------------------------------------------------------------
# ownership

# An owner's invite to take a registration over (registry_ui), and where
# the invited user redeems it (web_ui/groups.go).
OWNERSHIP_INVITES = "ownership-invites"
REDEEM_OWNERSHIP = "/api/v1.0/invites/redeem/registration-ownership"
# Longer than an invite may last (maxOwnershipInviteExpiry, 30 days).
INVITE_TOO_LONG = "800h"


def ownership(test: Test) -> list[Row]:
    """Who owns a registration, and what that lets them do
    (registry/registry_ui.go). A registration starts unowned; the
    holder of its key claims it (POST /namespaces/:id/claim,
    claimNamespace), with a token of registry.edit_registration signed
    with the key, for good: a claim without a token is 400, with another
    key's 403, the owner's again 200, and another user's 409. Once the
    admin approves it, its owner may change its description and its
    institution (applyOwnerEdits), and nothing else: the registry keeps
    its site name, prefix, and key as they were, whatever the owner
    sends, and refuses anyone else (403). A registration the admin
    denies, its owner may edit, which resubmits it as Pending. And its
    owner, or the admin, may hand it on with a single-use invite (POST
    /namespaces/:id/ownership-invites; a non-owner gets 403, and an
    expiry of none, of 0, or beyond 30 days 400), which the user who
    redeems it (POST /api/v1.0/invites/redeem/registration-ownership)
    then owns, and no one else may redeem: the former owner may change
    nothing afterward. The registration is one of the run's own,
    /registry-test-<run>-own, which `test-registry` claims and hands on
    to `test-registry-heir`; a user keeps the registrations they own, so
    the suite's sweep removes it."""
    key = test.key("own")
    prefix = test.prefix("own")
    made = test.register(prefix, key)
    if made.status != 201:
        return [("claim", FAIL, f"registering {prefix}: {made.describe()}, not 201")]
    reg_id = int((test.stored(prefix) or {}).get("id", 0))
    owner, owner_id = test.user()
    heir, heir_id = test.heir()
    headers = {USER: accounts.csrf(owner), HEIR: accounts.csrf(heir)}
    apis = {USER: owner, HEIR: heir}
    issuer = f"https://{test.tag}.invalid:8444"
    rows: list[Row] = []

    def owned_by() -> str:
        return str(registry.admin_metadata(test.registration(reg_id)).get("user_id") or "")

    def status() -> str:
        return str(registry.admin_metadata(test.registration(reg_id)).get("status") or "")

    def description() -> str:
        return str(registry.admin_metadata(test.registration(reg_id)).get("description") or "")

    def claim(who: str, signer: Optional[str]) -> accounts.Answer:
        query = ""
        if signer:
            tok = registry.token(signer, registry.EDIT_REGISTRATION, "admin", issuer)
            query = f"?access_token={tok}"
        return apis[who].call(
            "POST", f"{REGISTRATIONS}/{reg_id}/claim{query}", None, headers[who]
        )

    def edit(
        who: str, text: str, site_name: Optional[str] = None, pubkey: Optional[str] = None
    ) -> accounts.Answer:
        """A PUT of the registration as who, with text as its description,
        and otherwise as it is, but for a site name or a key given."""
        stored = test.registration(reg_id)
        meta = registry.admin_metadata(stored)
        body = {
            "prefix": prefix,
            "pubkey": pubkey or stored.get("pubkey", ""),
            "admin_metadata": {
                "description": text,
                "institution": meta.get("institution") or "pelican-test-framework",
                "site_name": site_name or meta.get("site_name") or test.tag,
                "user_id": meta.get("user_id") or "",
            },
        }
        return apis[who].call("PUT", f"{REGISTRATIONS}/{reg_id}", body, headers[who])

    def invite(who: str, body: Optional[dict[str, Any]]) -> accounts.Answer:
        return apis[who].call(
            "POST", f"{REGISTRATIONS}/{reg_id}/{OWNERSHIP_INVITES}", body, headers[who]
        )

    # claim
    problems: list[str] = []
    if owned_by():
        problems.append(f"it is owned by {owned_by()} before any claim")
    answer = claim(USER, None)
    if answer.status != 400:
        problems.append(f"with no token: {answer.describe()}, not 400")
    answer = claim(USER, test.key("own-foreign"))
    if answer.status != 403:
        problems.append(f"with another key's token: {answer.describe()}, not 403")
    if owned_by():
        problems.append(f"a refused claim left it owned by {owned_by()}")
    answer = claim(USER, key)
    if not answer.ok:
        problems.append(f"{USER}'s claim: {answer.describe()}, not 200")
    if owned_by() != owner_id:
        problems.append(f"after {USER}'s claim it is {owned_by() or 'no one'}'s, not {USER}'s")
    answer = claim(USER, key)
    if not answer.ok:
        problems.append(f"{USER}'s claim again: {answer.describe()}, not 200")
    answer = claim(HEIR, key)
    if answer.status != 409:
        problems.append(f"{HEIR}'s claim: {answer.describe()}, not 409")
    if owned_by() != owner_id:
        problems.append(f"after {HEIR}'s claim it is {owned_by() or 'no one'}'s, not {USER}'s")
    rows.append(
        row(
            "claim",
            problems,
            f"no token 400; another key 403; {USER}'s key: {USER}'s, again 200; {HEIR} 409",
        )
    )

    # owner-edit
    error = test.set_status(prefix, "approve")
    problems = [error] if error else []
    if status() != "Approved":
        problems.append(f"approved, its status is {status()}, not Approved")
    before = test.registration(reg_id)
    site_name = registry.admin_metadata(before).get("site_name")
    other_key = json.dumps(registry.jwks([test.key("own-foreign")]))
    answer = edit(USER, f"edited by {USER}", f"{test.tag}-renamed", other_key)
    if not answer.ok:
        problems.append(f"the owner's edit: {answer.describe()}, not 200")
    after = test.registration(reg_id)
    meta = registry.admin_metadata(after)
    if meta.get("description") != f"edited by {USER}":
        problems.append(f"its description is {meta.get('description')!r}, not the owner's")
    if meta.get("site_name") != site_name:
        problems.append(f"its site name is {meta.get('site_name')!r}: the owner changed it")
    if after.get("prefix") != prefix:
        problems.append(f"its prefix is {after.get('prefix')!r}: the owner changed it")
    if after.get("pubkey") != before.get("pubkey"):
        problems.append("its key changed: the owner's edit replaced it")
    if test.stored_keys(prefix) != registry.key_thumbprints([key]):
        problems.append("the registry no longer serves its key alone")
    if meta.get("status") != "Approved":
        problems.append(f"its status is {meta.get('status')}, not Approved")
    answer = edit(HEIR, f"edited by {HEIR}")
    if answer.status != 403:
        problems.append(f"{HEIR}'s edit: {answer.describe()}, not 403")
    if description() != f"edited by {USER}":
        problems.append(f"after {HEIR}'s edit its description is {description()!r}")
    rows.append(
        row(
            "owner-edit",
            problems,
            "approved: the owner's description; its site name, prefix, and key kept; "
            + f"{HEIR} 403",
        )
    )

    # deny-resubmit
    error = test.set_status(prefix, "deny")
    problems = [error] if error else []
    if status() != "Denied":
        problems.append(f"denied, its status is {status()}, not Denied")
    answer = edit(USER, f"resubmitted by {USER}")
    if not answer.ok:
        problems.append(f"the owner's edit of the denied registration: {answer.describe()}")
    if status() != "Pending":
        problems.append(f"after the owner's edit its status is {status()}, not Pending")
    if owned_by() != owner_id:
        problems.append(f"after the owner's edit it is {owned_by() or 'no one'}'s")
    rows.append(
        row("deny-resubmit", problems, "denied; the owner's edit resubmits it: Pending")
    )

    # invite-refusals
    problems = []
    answer = invite(HEIR, None)
    if answer.status != 403:
        problems.append(f"{HEIR}'s invite: {answer.describe()}, not 403")
    for expiry in ("0s", INVITE_TOO_LONG, "soon"):
        answer = invite(USER, {"expiresIn": expiry})
        if answer.status != 400:
            problems.append(f"an invite expiring in {expiry!r}: {answer.describe()}, not 400")
    answer = test.admin().call(
        "POST",
        f"{REGISTRATIONS}/{reg_id}/{OWNERSHIP_INVITES}",
        {"expiresIn": "1h"},
        test.csrf(),
    )
    if answer.status != 201 or not answer.object.get("inviteToken"):
        problems.append(f"the admin's invite: {answer.describe()}, not 201 with a token")
    rows.append(
        row(
            "invite-refusals",
            problems,
            f"{HEIR} 403; expiring in 0s, {INVITE_TOO_LONG}, or 'soon' 400; the admin's 201",
        )
    )

    # invite-transfer
    answer = invite(USER, {"expiresIn": "1h"})
    token = str(answer.object.get("inviteToken") or "")
    if answer.status != 201 or not token:
        rows.append(
            ("invite-transfer", FAIL, f"the owner's invite: {answer.describe()}, with no token")
        )
        return rows
    problems = []
    if answer.object.get("isSingleUse") is not True:
        problems.append("the invite does not say that it is single-use")
    redeemed = heir.call("POST", REDEEM_OWNERSHIP, {"token": token}, headers[HEIR])
    if not redeemed.ok:
        problems.append(f"{HEIR}'s redemption: {redeemed.describe()}, not 200")
    elif (
        redeemed.object.get("registrationId") != reg_id
        or redeemed.object.get("registrationPrefix") != prefix
    ):
        problems.append(
            f"the redemption names registration {redeemed.object.get('registrationId')}"
            + f" ({redeemed.object.get('registrationPrefix')}), not {reg_id} ({prefix})"
        )
    if owned_by() != heir_id:
        problems.append(f"redeemed, it is {owned_by() or 'no one'}'s, not {HEIR}'s")
    answer = edit(USER, f"edited by {USER} after the transfer")
    if answer.status != 403:
        problems.append(f"the former owner's edit: {answer.describe()}, not 403")
    answer = edit(HEIR, f"edited by {HEIR}")
    if not answer.ok:
        problems.append(f"the new owner's edit: {answer.describe()}, not 200")
    if description() != f"edited by {HEIR}":
        problems.append(f"after both edits its description is {description()!r}")
    rows.append(
        row(
            "invite-transfer",
            problems,
            f"{HEIR} redeems the owner's invite: {HEIR}'s; {USER} 403; {HEIR}'s edit 200",
        )
    )

    # invite-single-use
    answer = owner.call("POST", REDEEM_OWNERSHIP, {"token": token}, headers[USER])
    problems = [] if answer.status in (400, 404) else [f"{answer.describe()}, not 400 or 404"]
    if owned_by() != heir_id:
        problems.append(f"redeemed again, it is {owned_by() or 'no one'}'s, not {HEIR}'s")
    rows.append(row("invite-single-use", problems, f"redeemed again, by {USER}: refused"))
    return rows


# --------------------------------------------------------------------------
# identity


def identity(test: Test) -> list[Row]:
    """Each server's ID: as it records it (/api/v1.0/server/localMetadata,
    which it learns from the registry), as the registry looks it up by its
    prefix (/api/v1.0/registry/server/<prefix>, getServerByPrefixHandler),
    and as the registry lists it (registry_ui's /servers), once."""
    if test.fed.tiny:
        return [("", SKIP, "the tiny topology's origin and cache are one process, one record")]
    problems: list[str] = []
    ids: dict[str, Server] = {}
    for server in test.servers:
        meta = test.metadata(server)
        server_id, name = str(meta.get("id", "")), str(meta.get("name", ""))
        if not server_id:
            problems.append(f"{server.svc} records no ID")
            continue
        ids[server_id] = server
        prefix = test.server_prefix(server)
        answer = test.get(f"{REGISTRY}/server{path_of(prefix)}")
        found = common.as_object(registry.parse(answer.body)) or {}
        if answer.status != 200:
            problems.append(f"{prefix}'s server: {answer.describe()}")
        elif (found.get("id"), found.get("name")) != (server_id, name):
            problems.append(
                f"{prefix}'s server is {found.get('id')} ({found.get('name')}),"
                + f" but {server.svc} says {server_id} ({name})"
            )
    rows = [row("lookup", problems, f"{len(test.servers)} server(s): the same ID at both")]
    answer = test.get(SERVERS)
    listed = [
        common.as_object(s) or {} for s in common.as_array(registry.parse(answer.body)) or []
    ]
    problems = [] if answer.status == 200 else [f"{SERVERS}: {answer.describe()}"]
    for server_id, server in ids.items():
        count = sum(1 for e in listed if e.get("id") == server_id)
        if count != 1:
            problems.append(f"{server.svc}'s ID {server_id} is listed {count} times")
        names = [e for e in listed if e.get("name") == test.metadata(server).get("name")]
        if len(names) > 1:
            problems.append(f"{server.svc}'s name is listed {len(names)} times")
    rows.append(row("listed", problems, f"{len(ids)} server(s), each listed once, by its ID"))
    return rows


# --------------------------------------------------------------------------
# downtime


def downtime_body(test: Test, what: str, start: int, end: int) -> dict[str, Any]:
    """A downtime of the run's, from start to end (milliseconds), its
    description saying what it is for."""
    return {
        "class": "SCHEDULED",
        "severity": SEVERITY,
        "description": f"pelican-test-framework {test.tag} {what}",
        "startTime": start,
        "endTime": end,
    }


def registry_downtime(test: Test, downtime_id: str) -> web.Response:
    """A GET of downtime downtime_id at the registry."""
    return test.get(f"{DOWNTIME}/{downtime_id}")


def downtime(test: Test) -> list[Row]:
    """Downtimes recorded at the registry (web_ui/server_api.go): a cache's
    own, which it tells the registry of; a registered server's token,
    which may create or change only that server's own
    (DowntimeAuthHandler, and downtimeMutationAuthorized); and the
    admin's, which the directors apply to routing, at once if current,
    and only list if not yet begun (director/cache_ads.go)."""
    fed = test.fed
    if fed.tiny:
        return [("", SKIP, "the tiny topology's servers and director are one process")]
    caches = [s for s in test.servers if s.kind == "cache"]
    if not caches:
        return [("", SKIP, "no federated cache")]
    cache, origin = caches[0], test.servers[0]
    cache_meta, origin_meta = test.metadata(cache), test.metadata(origin)
    cache_id, cache_name = str(cache_meta.get("id", "")), str(cache_meta.get("name", ""))
    origin_id, origin_name = str(origin_meta.get("id", "")), str(origin_meta.get("name", ""))
    rows: list[Row] = []
    now = millis()
    later = (now + DAY_MS, now + 2 * DAY_MS)

    # A cache's own downtime, declared at the cache.
    mirrored = str(uuid.uuid4())
    api = test.server_admin(cache)
    answer = api.call(
        "POST",
        f"{DOWNTIME}/{mirrored}",
        downtime_body(test, "mirrored", *later),
        accounts.csrf(api),
    )
    if not answer.ok:
        return [("mirrored", FAIL, f"declaring it at {cache.svc}: {answer.describe()}")]
    record: dict[str, Any] = {}

    def recorded() -> bool:
        found = registry_downtime(test, mirrored)
        record.clear()
        if found.status == 200:
            record.update(common.as_object(registry.parse(found.body)) or {})
        return bool(record)

    problems: list[str] = []
    if not common.wait_for(recorded, MIRROR_WAIT, interval=5):
        problems.append(f"the registry has not had it for {MIRROR_WAIT}s")
    elif (record.get("serverId"), str(record.get("source", "")).lower()) != (cache_id, "cache"):
        problems.append(
            f"the registry has it as {record.get('source')}'s, of {record.get('serverId')},"
            + f" not the cache's, of {cache_id}"
        )
    rows.append(row("mirrored", problems, f"{cache.svc}'s, at the registry, of {cache_id}"))

    # Another server's token may not change it.
    problems = []
    for method, scope, body in (
        (
            "PUT",
            registry.DOWNTIME_MODIFY,
            {"description": f"pelican-test-framework {test.tag} x"},
        ),
        ("DELETE", registry.DOWNTIME_DELETE, None),
    ):
        tok = test.server_token(origin, scope, origin_id)
        found = web.request(
            method,
            f"{test.registry}{DOWNTIME}/{mirrored}",
            token=tok,
            upload=None if body is None else json.dumps(body).encode(),
            headers=registry.JSON if body is not None else None,
        )
        if found.status != 403:
            problems.append(f"{method}: {found.describe()}, not 403")
    rows.append(row("foreign-modify", problems, f"{origin.svc}'s token, PUT and DELETE: 403"))

    answer = api.call("DELETE", f"{DOWNTIME}/{mirrored}", headers=accounts.csrf(api))
    problems = [] if answer.ok else [f"deleting it at {cache.svc}: {answer.describe()}"]
    if not problems and not common.wait_for(
        lambda: registry_downtime(test, mirrored).status == 404, MIRROR_WAIT, interval=5
    ):
        problems.append(f"the registry still has it {MIRROR_WAIT}s after")
    rows.append(row("mirror-deleted", problems, f"deleted at {cache.svc}, and at the registry"))

    rows += bound(test, origin, origin_id, origin_name, cache_id, cache_name, later)
    rows.append(admin_current(test, cache, cache_id, cache_name))
    rows.append(future(test, cache, cache_id, cache_name, later))
    return rows


def bound(
    test: Test,
    origin: Server,
    origin_id: str,
    origin_name: str,
    cache_id: str,
    cache_name: str,
    later: tuple[int, int],
) -> list[Row]:
    """A registered server's token creates a downtime only of its own:
    the registry records the token's subject and that server's name,
    whatever server the body names; and none whose source is the
    registry (403)."""
    rows: list[Row] = []
    downtime_id = str(uuid.uuid4())
    body = {
        **downtime_body(test, "bound", *later),
        "serverId": cache_id,
        "serverName": cache_name,
    }
    answer = web.request(
        "POST",
        f"{test.registry}{DOWNTIME}/{downtime_id}",
        token=test.server_token(origin, registry.DOWNTIME_CREATE, origin_id),
        upload=json.dumps(body).encode(),
        headers=registry.JSON,
    )
    found = common.as_object(registry.parse(answer.body)) or {}
    problems = [] if answer.ok else [f"{answer.describe()}, not 200"]
    if answer.ok and (found.get("serverId"), found.get("serverName")) != (
        origin_id,
        origin_name,
    ):
        problems.append(
            f"recorded as {found.get('serverId')} ({found.get('serverName')}),"
            + f" not {origin_id} ({origin_name})"
        )
    rows.append(row("bound", problems, f"as {origin.svc}, naming the cache: {origin.svc}'s"))
    if answer.ok:
        remove_downtime(test, downtime_id, found)
    downtime_id = str(uuid.uuid4())
    body = {**downtime_body(test, "source", *later), "source": "registry"}
    answer = web.request(
        "POST",
        f"{test.registry}{DOWNTIME}/{downtime_id}",
        token=test.server_token(origin, registry.DOWNTIME_CREATE, origin_id),
        upload=json.dumps(body).encode(),
        headers=registry.JSON,
    )
    if answer.ok:
        remove_downtime(test, downtime_id, common.as_object(registry.parse(answer.body)) or {})
    rows.append(
        row(
            "source-registry",
            [] if answer.status == 403 else [f"{answer.describe()}, not 403"],
            f"as {origin.svc}, with source registry: 403",
        )
    )
    return rows


def director_names(test: Test, cache: Server) -> dict[str, str]:
    """What each director (by URL) calls cache in its list of servers, by
    its host; "" where it lists no such server."""
    found: dict[str, str] = {}
    for url in test.fed.directors:
        answer = web.request("GET", url + DIRECTOR_SERVERS)
        entries = [
            common.as_object(e) or {}
            for e in common.as_array(registry.parse(answer.body)) or []
        ]
        found[url] = next(
            (
                str(e.get("name", ""))
                for e in entries
                if director.host(str(e.get("url", ""))) == cache.host
            ),
            "",
        )
    return found


def filtered_at(test: Test, cache: Server) -> list[str]:
    """The directors that list cache as filtered."""
    found: list[str] = []
    for url in test.fed.directors:
        answer = web.request("GET", url + DIRECTOR_SERVERS)
        for entry in (
            common.as_object(e) or {}
            for e in common.as_array(registry.parse(answer.body)) or []
        ):
            if (
                director.host(str(entry.get("url", ""))) == cache.host
                and entry.get("filtered") is True
            ):
                found.append(director.host(url))
    return found


def routing_path(test: Test) -> str:
    """An object that every director would send to a cache: the first
    exported namespace's first object (init-data.py)."""
    return f"/{next(iter(test.fed.exports))}/data/0.0"


def admin_current(test: Test, cache: Server, cache_id: str, cache_name: str) -> Row:
    """The registry's admin declares the cache down now: within
    DOWNTIME_WAIT seconds, no director names it, and each lists it as
    filtered; once the admin ends it, every director names it again, as
    soon."""
    fed, path = test.fed, routing_path(test)

    def named() -> list[str]:
        return director.naming(fed.directors, "object", path, cache.host)

    if len(named()) != len(fed.directors):
        return ("admin-current", FAIL, f"before the downtime, only {named()} named {cache.svc}")
    downtime_id = str(uuid.uuid4())
    now = millis()
    body = {
        **downtime_body(test, "current", now - 60_000, now + 3_600_000),
        "serverId": cache_id,
        "serverName": cache_name,
    }
    answer = test.admin().call("POST", f"{DOWNTIME}/{downtime_id}", body)
    if not answer.ok:
        return ("admin-current", FAIL, f"declaring it as the admin: {answer.describe()}")
    problems: list[str] = []
    try:
        if not common.wait_for(
            lambda: not named() and len(filtered_at(test, cache)) == len(fed.directors),
            DOWNTIME_WAIT,
            interval=5,
        ):
            problems.append(
                f"{DOWNTIME_WAIT}s in, {named()} still named {cache.svc}, and only"
                + f" {filtered_at(test, cache)} listed it filtered"
            )
    finally:
        ended = test.admin().call("DELETE", f"{DOWNTIME}/{downtime_id}")
    if not ended.ok:
        problems.append(f"ending it as the admin: {ended.describe()}")
    elif not common.wait_for(
        lambda: len(named()) == len(fed.directors), DOWNTIME_WAIT, interval=5
    ):
        problems.append(f"{DOWNTIME_WAIT}s after it ended, only {named()} named {cache.svc}")
    return row(
        "admin-current",
        problems,
        f"{cache.svc}: dropped and filtered by {len(fed.directors)} director(s), then back",
    )


def future(
    test: Test, cache: Server, cache_id: str, cache_name: str, later: tuple[int, int]
) -> Row:
    """A downtime of the cache's that begins tomorrow, as the registry's
    admin declares it: within DOWNTIME_WAIT seconds, every director lists
    it among its downtimes, and among the cache's, with its severity and
    who created it; and every director still names the cache."""
    fed, path = test.fed, routing_path(test)
    downtime_id = str(uuid.uuid4())
    body = {
        **downtime_body(test, "future", *later),
        "serverId": cache_id,
        "serverName": cache_name,
    }
    answer = test.admin().call("POST", f"{DOWNTIME}/{downtime_id}", body)
    if not answer.ok:
        return ("future", FAIL, f"declaring it as the admin: {answer.describe()}")
    names = director_names(test, cache)
    missing: list[str] = []

    def listed() -> bool:
        missing.clear()
        for url in fed.directors:
            if not names[url]:
                missing.append(f"{director.host(url)} does not list {cache.svc}")
                continue
            for route in (
                DIRECTOR_DOWNTIMES,
                f"{DIRECTOR_SERVERS}/{quote(names[url])}/downtimes",
            ):
                answer = web.request("GET", url + route)
                entries = [
                    common.as_object(e) or {}
                    for e in common.as_array(registry.parse(answer.body)) or []
                ]
                match = [e for e in entries if e.get("id") == downtime_id]
                if not match:
                    missing.append(f"{director.host(url)}'s {route} lacks it")
                elif (match[0].get("severity"), match[0].get("createdBy")) != (
                    SEVERITY,
                    "admin",
                ):
                    missing.append(
                        f"{director.host(url)}'s {route} has it of"
                        + f" {match[0].get('severity')!r}, by {match[0].get('createdBy')!r}"
                    )
        return not missing

    problems: list[str] = []
    try:
        if not common.wait_for(listed, DOWNTIME_WAIT, interval=5):
            problems.append(f"after {DOWNTIME_WAIT}s, {first(missing)}")
        routed = director.naming(fed.directors, "object", path, cache.host)
        if len(routed) != len(fed.directors):
            problems.append(f"only {routed} still named {cache.svc}")
    finally:
        ended = test.admin().call("DELETE", f"{DOWNTIME}/{downtime_id}")
    if not ended.ok:
        problems.append(f"deleting it as the admin: {ended.describe()}")
    return row(
        "future",
        problems,
        f"{cache.svc}'s, tomorrow: listed by {len(fed.directors)} director(s), still routed",
    )


def remove_downtime(test: Test, downtime_id: str, record: Mapping[str, Any]) -> Optional[str]:
    """Remove downtime downtime_id (record, as the registry has it) from
    the registry: the registry's own, as its admin; a server's, as that
    server, by its token. What went wrong, or None."""
    if str(record.get("source", "")).lower() == "registry":
        answer = test.admin().call("DELETE", f"{DOWNTIME}/{downtime_id}")
        return None if answer.ok else f"as the admin: {answer.describe()}"
    owner_id = str(record.get("serverId", ""))
    for server in test.servers:
        try:
            if str(test.metadata(server).get("id", "")) != owner_id:
                continue
        except accounts.ApiError:
            continue
        found = web.request(
            "DELETE",
            f"{test.registry}{DOWNTIME}/{downtime_id}",
            token=test.server_token(server, registry.DOWNTIME_DELETE, owner_id),
        )
        return None if found.ok else f"as {server.svc}: {found.describe()}"
    return f"no server of the shape's has ID {owner_id}"


# --------------------------------------------------------------------------
# Removing what the suite registered and declared.


def sweep(test: Test) -> list[str]:
    """Remove every registration, server, and downtime of the suite's
    (MARKER), of any run: downtimes at each server, which would otherwise
    tell the registry of them again, and at the registry; then
    registrations and servers, as the registry's admin. What went
    wrong."""
    problems: list[str] = []
    for server in {s.web_url: s for s in test.servers}.values():
        try:
            api = test.server_admin(server)
            answer = api.call("GET", f"{DOWNTIME}?status=all")
            for entry in (common.as_object(e) or {} for e in answer.array):
                if MARKER in str(entry.get("description", "")):
                    gone = api.call(
                        "DELETE", f"{DOWNTIME}/{entry.get('id')}", headers=accounts.csrf(api)
                    )
                    if not gone.ok and gone.status != 404:
                        problems.append(
                            f"downtime {entry.get('id')} at {server.svc}: {gone.describe()}"
                        )
        except accounts.ApiError as e:
            problems.append(f"{server.svc}'s downtimes: {e}")
    listed = accounts.Api(test.registry, None).call("GET", f"{DOWNTIME}?status=all")
    for entry in (common.as_object(e) or {} for e in listed.array):
        if MARKER in str(entry.get("description", "")):
            if registry_downtime(test, str(entry.get("id"))).status == 404:
                continue
            error = remove_downtime(test, str(entry.get("id")), entry)
            if error:
                problems.append(f"downtime {entry.get('id')} at the registry: {error}")
    # A server's removal takes its registrations with it
    # (deleteServerByID); a registration's, its server's, once it has no
    # other (deleteRegistrationByID).
    try:
        admin, headers = test.admin(), test.csrf()
        listed = accounts.must(admin.call("GET", SERVERS), "listing servers")
        for entry in (common.as_object(e) or {} for e in listed.array):
            if MARKER in str(entry.get("name", "")):
                target = f"{SERVERS}/{quote(str(entry.get('id')))}"
                gone = admin.call("DELETE", target, headers=headers)
                if not gone.ok:
                    problems.append(f"server {entry.get('name')}: {gone.describe()}")
        listed = accounts.must(admin.call("GET", REGISTRATIONS), "listing registrations")
        for entry in (common.as_object(e) or {} for e in listed.array):
            if MARKER in str(entry.get("prefix", "")):
                target = f"{REGISTRATIONS}/{entry.get('id')}"
                gone = admin.call("DELETE", target, headers=headers)
                if not gone.ok:
                    problems.append(f"registration {entry.get('prefix')}: {gone.describe()}")
    except accounts.ApiError as e:
        problems.append(str(e))
    return problems


RUN: dict[str, Callable[[Test], list[Row]]] = {
    "register": register,
    "jwks": jwks,
    "registry-ui": registry_ui,
    "completeness": completeness,
    "ownership": ownership,
    "identity": identity,
    "downtime": downtime,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: a standalone origin has no
    registry."""
    return "a standalone origin has no registry" if not fed.registry_url else None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Remove what earlier runs left, run the selected scenarios, and
    remove what they registered and declared."""
    test = Test(session)
    errors = sweep(test)
    if errors:
        warn(f"could not remove what earlier runs registered: {first(errors)}")
    print(f"Testing the registry at {test.registry}, with prefixes /{test.tag}-*\n")
    try:
        for name in selected:
            for case, status, note in guarded(functools.partial(RUN[name], test)):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        errors = sweep(test)
        if errors:
            warn(f"could not remove what the suite registered: {first(errors)}")
