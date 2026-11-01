"""Authorization at each server directly, in every namespace.

Each credential of framework/testlib/credentials.py is presented at
every server, and credentials.expected() says what it should get. Other
checks present tokens that `pelican token create` cannot make
(testlib/jwt.py), each wrong in one way. Each namespace has a test
object of its own bytes (but on a pstore origin, /public reads
/protected-a's), so a server that answers from another namespace's
storage is caught. Most checks beyond the credentials work in
/protected-a, and skip where it isn't exported (PROTECTED_A_CHECKS).
The run's objects lie flat in data/auth/, as data/auth/<run>-<name>.

`expired-in-use` takes three minutes, so it runs in a thread of its own
beside the rest, and records its rows last. Responses go to
framework/var/data/auth-test/ (in the SciTokens pass,
data/auth-test-scitokens/), and a row per request to the results (per
server and kind, for `expired-in-use`, `traversal`, and `narrow-climb`).

Every token presented is of the session's profile, and must fare as its
twin of the other profile does: SciTokens' read: and write: stand for
storage.read and storage.modify. The test objects are put in place, and
the stores read, with WLCG tokens either way. See
docs/tests/authorization.md and docs/tests/token-validation.md.
"""

import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from functools import cached_property, partial
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from testlib import accounts, common, credentials, director, jwt, stores, suite, web
from testlib.common import die, warn
from testlib.credentials import ALLOW, CREDENTIALS, DENY, PROTECTED, RWM, Credential, R
from testlib.report import FAIL, PASS, SKIP, Report, label, record, record_skip
from testlib.session import Session
from testlib.web import save

OUT = "data/auth-test"

# The checks that are not credentials.
KEYS = "keys"
NARROW = "narrow"
EXPIRED_IN_USE = "expired-in-use"

SCENARIOS = {
    KEYS: "each issuer's discovery document and JWKS, and the server-wide JWKS",
    EXPIRED_IN_USE: "a token in use as it expires: taken before, refused from 70s past its expiry on",
}
SCENARIOS.update(
    {
        c.name: (
            f"a token: {c.description} ({c.verdict} where a token decides)"
            if c.key
            else f"{c.description} ({c.verdict} where a token decides)"
        )
        for c in CREDENTIALS
    }
)
SCENARIOS[NARROW] = "a token for the test object alone: it may GET the object, not its sibling"
SCENARIOS.update(
    {
        "create-only": "storage.create alone: it may create an object, not replace or delete one",
        "modify-only": "storage.modify alone (or SciTokens' write): it may create, replace, and delete an object",
    }
)

# The checks of one storage scope each: the `pelican token create` flag
# that grants it, and whether it may replace or delete an object (the WLCG
# token profile; storage.modify implies storage.create). In SciTokens,
# the flag grants write, which may do all three.
SCOPES = {"create-only": ("write", False), "modify-only": ("modify", True)}

TRAVERSAL = "traversal"
SCENARIOS[TRAVERSAL] = (
    "`..` in a path, however spelled, and a symbolic link in a store, reach no other"
    " namespace, nor beyond an export's storage"
)

# Spellings of a step up a path. Each server must keep a request within
# the namespace that its path begins with, however it is spelled.
STEPS = ("..", "%2e%2e", "%2E%2E", ".%2e", "%252e%252e")

HINTS = "hints"
SCENARIOS[HINTS] = "a V2 cache's refusal of a request with no token names the namespace and its issuer"

AUDIENCE = "audience"
SCENARIOS[AUDIENCE] = "a token for another audience: every origin refuses it, and stores nothing"
# An audience that no server here answers to.
OTHER_AUDIENCE = "https://elsewhere.invalid"

QUERY_TOKEN = "query-token"
SCENARIOS[QUERY_TOKEN] = (
    "a token in the authz query parameter, bare or after `Bearer `, counts as the header's"
)

TOKEN_LISTS = "token-lists"
SCENARIOS[TOKEN_LISTS] = "a native origin tries each token of a request, in its header and query, up to 10"
AUDIENCE_SPECIFIC = "audience-specific"
SCENARIOS[AUDIENCE_SPECIFIC] = (
    "a token for the first origin's audience is taken there alone; one that lists `any` too, everywhere"
)
TIME_CLAIMS = "time-claims"
SCENARIOS[TIME_CLAIMS] = (
    "nbf or iat ahead by more than 60s of clock skew is refused, by less is not; exp past is refused"
)
MALFORMED = "malformed-claims"
SCENARIOS[MALFORMED] = "no exp, kid, or aud, an unknown kid, or a signature by none or HS256: refused"
STANDALONE = "standalone-refusals"
SCENARIOS[STANDALONE] = (
    "a native origin refuses with 401 or 403 and token hints standalone, with 401 and none federated"
)
WEB_TOKEN_STRICT = "web-token-strict"
SCENARIOS[WEB_TOKEN_STRICT] = (
    "a server's web API refuses its own token 20s before its nbf or 5s past its exp"
)
NARROW_CLIMB = "narrow-climb"
SCENARIOS[NARROW_CLIMB] = "a token for one collection reaches nothing beside it with `..`, however spelled"
NESTED = "nested-protected"
SCENARIOS[NESTED] = (
    "a protected prefix inside a public one (/public/secret) is served to no one without a token"
)

# The checks that present no storage token, or none of a profile but
# WLCG's: SciTokens has no scope of create alone (see suites/__init__.py).
ONE_PROFILE = (KEYS, "none", HINTS, WEB_TOKEN_STRICT, "create-only")

# The checks, beyond the credentials, that ask for /protected-a's test
# object or write beside it, and so skip where it isn't exported.
PROTECTED_A_CHECKS = (
    EXPIRED_IN_USE,
    NARROW,
    *SCOPES,
    AUDIENCE,
    QUERY_TOKEN,
    TOKEN_LISTS,
    AUDIENCE_SPECIFIC,
    TIME_CLAIMS,
    MALFORMED,
    STANDALONE,
    NARROW_CLIMB,
)


class Test(suite.Test):
    """The test objects, the setup tokens, and the rows recorded.

    Its namespace is /protected-a, where most checks work.

    Attributes:
        report: Where the rows go.
        out: Where the responses go, under framework/var.
        setup_tokens: A setup token for each exported namespace, by
            namespace (Session.setup_tokens()).
        object: The test object's rel in each namespace.
        object_bytes: The test object's bytes in each namespace.
        sibling: For `narrow`: a rel that the object's is a prefix of.
        sibling_bytes: The sibling's bytes.
    """

    def __init__(self, session: Session, report: Report, out: str):
        super().__init__(session, "auth", "protected-a")
        self.report = report
        self.out = out
        self.setup_tokens = session.setup_tokens()
        self.object = self.flat("object")
        # On a pstore origin, /public's bytes are /protected-a's, whose
        # storage it reads.
        own = {ns: os.urandom(65536) for ns in session.namespaces}
        self.object_bytes = {ns: own[session.fed.storage_namespace(ns)] for ns in own}
        self.sibling = f"{self.object}-sibling"
        self.sibling_bytes = os.urandom(65536)

    def flat(self, name: str) -> str:
        """Return the rel of name, one of the run's objects.

        It is data/auth/<run>-<name>.
        """
        return f"data/{self.area}/{self.run}-{name}"

    def credential_token(self, cred: Credential, namespace: str, op: str) -> str | None:
        """Return cred's token for op (`get` or `put`) in namespace.

        The token is of the session's profile.

        Returns:
            The token, or None for `none`, which has no token.
        """
        return self.session.credential_token(cred, namespace, op)

    @property
    def profile(self) -> str:
        """The profile of the tokens that the checks present."""
        return self.session.profile

    @cached_property
    def header(self) -> dict[str, Any]:
        """The header of the `server` token for /protected-a.

        It is as `pelican token create` wrote it: its alg, and the kid of
        the origins' key in their issuer's JWKS. raw_token() signs under
        it.
        """
        return jwt.decode(self.token)[0]

    def whose(self, body: bytes) -> str | None:
        """Return the namespace whose test object body is, if any."""
        return next((ns for ns, data in self.object_bytes.items() if data == body), None)

    def stored(self, origin: common.Origin, namespace: str, rel: str) -> str | None:
        """Return what origin holds at rel in namespace (see stores.held()).

        Raises:
            stores.Unreadable: The origin could not say what it holds.
        """
        return stores.held(origin, namespace, rel, self.setup_tokens[namespace])

    @staticmethod
    def labels(target: str, namespace: str, op: str, name: str) -> tuple[str, str]:
        """Return a row's heading, as printed, and its case, as recorded."""
        return f"{target:<10} {namespace:<11} {op:<8} {name:<11}", f"{target} {namespace} {op} {name}"

    def row(
        self,
        target: str,
        namespace: str,
        op: str,
        name: str,
        answer: web.Response,
        why: str,
        note: str = "",
    ) -> None:
        """Print and record the row of one request."""
        record(self.report, *self.labels(target, namespace, op, name), answer, why, note)

    def skip_row(self, target: str, namespace: str, op: str, name: str, why: str) -> None:
        """Print and record the row of a request skipped because of why."""
        record_skip(self.report, *self.labels(target, namespace, op, name), why)

    def outcome_row(
        self, target: str, namespace: str, op: str, name: str, problems: Sequence[str], note: str
    ) -> None:
        """Print and record a row of several requests.

        It fails with the first of problems, or passes with note.
        """
        heading, case = self.labels(target, namespace, op, name)
        print(f"{heading}       {FAIL if problems else PASS}  {problems[0] if problems else note}")
        self.report.add(case, *common.outcome(problems, note))


def write(
    test: Test,
    origin: common.Origin,
    namespace: str,
    rel: str,
    token: str | None,
    allowed: bool,
    old: bytes | None,
    new: bytes | None,
) -> tuple[web.Response, str]:
    """Write rel in namespace straight to origin, with token.

    old, if given, is put in place first, in the namespace's storage at
    origin. The write is a PUT of new, or a DELETE if new is None. It
    must succeed if allowed, and leave new in the store; otherwise it
    must be refused, and leave old (None: nothing).

    Returns:
        The answer, and what is wrong with it.
    """
    if old is not None:
        storage = origin.storage_namespace(namespace)
        caps = test.fed.exports[storage]
        error = stores.seed(origin, storage, rel, old, test.setup_tokens[storage], caps)
        if error:
            return web.Response(None), f"could not put the object in place ({error})"
    method = "DELETE" if new is None else "PUT"
    answer = web.request(method, stores.url(origin, namespace, rel), token=token, upload=new)
    want = new if allowed else old
    problems: list[str] = []
    if allowed and not answer.ok:
        problems.append("expected 2xx")
    elif not allowed and answer.status not in (401, 403):
        problems.append("expected 401 or 403")
    try:
        have = test.stored(origin, namespace, rel)
    except stores.Unreadable:
        problems.append("could not check the store")
    else:
        if want is None and have is not None:
            problems.append("the store holds the object")
        elif want is not None and have is None:
            problems.append("the store has no such object")
        elif want is not None and have != common.digest(want):
            problems.append("the store holds other bytes")
    return answer, "; ".join(problems)


# --------------------------------------------------------------------------
# The credentials, straight to each server.


def check_credential(test: Test, cred: Credential) -> None:
    """Present cred in every namespace it applies to, at every server."""
    if cred.expired:
        test.session.wait_until_stale()
    for namespace in test.session.namespaces:
        if not credentials.applies(cred, namespace):
            continue
        for origin in test.fed.origins:
            target = label(origin.svc, "origin")
            check_read(test, cred, namespace, target, origin.url, "get", direct=True)
            check_read(test, cred, namespace, target, origin.url, "head", direct=True)
            check_put(test, cred, namespace, target, origin)
            check_delete(test, cred, namespace, target, origin)
        for cache in test.fed.caches:
            target = label(cache.svc, "cache")
            check_read(test, cred, namespace, target, cache.url, "get", direct=False)
            check_read(test, cred, namespace, target, cache.url, "head", direct=False)


def check_read(
    test: Test, cred: Credential, namespace: str, target: str, url: str, op: str, direct: bool
) -> None:
    """GET or HEAD (op) the test object at the server at url.

    url is an origin's if direct.
    """
    token = test.credential_token(cred, namespace, "get")
    answer = web.request(op.upper(), f"{url}/{namespace}/{test.object}", token=token)
    save(f"{test.out}/responses/{target}-{namespace}-{op}-{cred.name}", answer)
    data = test.object_bytes[namespace]
    whose = test.whose(answer.body) if op == "get" else None
    length = answer.header("Content-Length")
    why = ""
    if credentials.expected(cred, test.fed.exports[namespace], op, direct) == ALLOW:
        if answer.status != 200:
            why = "expected 200"
        elif op == "get" and answer.body != data:
            why = f"the body is /{whose}'s object" if whose else "the body is not the object"
        elif op == "head" and length != str(len(data)):
            why = f"Content-Length '{length}', not the object's {len(data)}"
    elif answer.status not in (401, 403):
        why = "expected 401 or 403"
    elif whose:
        why = f"refused, but the body is /{whose}'s object"
    test.row(target, namespace, op, cred.name, answer, why)


def check_put(test: Test, cred: Credential, namespace: str, target: str, origin: common.Origin) -> None:
    """PUT new bytes to origin."""
    rel = test.flat(f"{target}-{namespace}-{cred.name}")
    data = os.urandom(4096 + len(rel))
    with open(f"{test.out}/sources/{target}-{namespace}-{cred.name}", "wb") as f:
        f.write(data)
    allowed = credentials.expected(cred, test.fed.exports[namespace], "put", direct=True) == ALLOW
    token = test.credential_token(cred, namespace, "put")
    answer, why = write(test, origin, namespace, rel, token, allowed, None, data)
    save(f"{test.out}/responses/{target}-{namespace}-put-{cred.name}", answer)
    test.row(target, namespace, "put", cred.name, answer, why)


def check_delete(test: Test, cred: Credential, namespace: str, target: str, origin: common.Origin) -> None:
    """DELETE an object put at origin for the purpose.

    Skip the row if the DELETE should succeed but the origin's storage
    cannot delete.
    """
    allowed = credentials.expected(cred, test.fed.exports[namespace], "delete", direct=True) == ALLOW
    cannot_delete = stores.cannot(test.fed, "delete")
    if cannot_delete and allowed:
        test.skip_row(target, namespace, "delete", cred.name, cannot_delete)
        return
    rel = test.flat(f"{target}-{namespace}-{cred.name}-delete")
    token = test.credential_token(cred, namespace, "put")
    answer, why = write(test, origin, namespace, rel, token, allowed, os.urandom(4096 + len(rel)), None)
    save(f"{test.out}/responses/{target}-{namespace}-delete-{cred.name}", answer)
    test.row(target, namespace, "delete", cred.name, answer, why)


# --------------------------------------------------------------------------
# The keys check: what each JWKS should hold is in credentials.py
# (server_jwks_problems, namespace_jwks_problems, and
# public_jwks_problems).


def file_key_ids(path: str) -> frozenset[str]:
    """Return the kids in the JWKS file at path."""
    try:
        with open(path, "rb") as f:
            return credentials.key_ids(f.read())
    except FileNotFoundError:
        die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    except ValueError:
        die(f"framework/var/{path} is not a JWKS; remove it and run ./fed.sh keys init")


def get_jwks(url: str, out: str, name: str) -> tuple[web.Response, frozenset[str] | None, list[str]]:
    """GET the JWKS at url, keeping the response as name in out.

    Returns:
        The answer, its kids (None if it is not a JWKS), and what is wrong
        with it if it is not.
    """
    answer = web.request("GET", url)
    save(f"{out}/responses/{name}", answer)
    if answer.status != 200:
        return answer, None, ["expected 200"]
    try:
        return answer, credentials.key_ids(answer.body), []
    except ValueError:
        return answer, None, ["the body is not a JWKS"]


def check_keys(fed: common.Federation, report: Report, out: str) -> None:
    """Check the keys that the origins publish, keeping responses in out.

    First the server-wide JWKS, then each exported protected namespace's
    discovery document and JWKS. Where /public has an issuer
    (`-p origin-public-writes`), its JWKS must hold the server-wide keys,
    and neither protected namespace's own, whose tokens could otherwise
    write it.
    """
    if not credentials.namespace_issuers(fed):
        why = (
            "every namespace trusts the external issuer"
            if fed.external_issuer
            else "the origins' issuer is off"
        ) + ", so no namespace has keys of its own"
        warn(f"skipping {KEYS}: {why}")
        report.add(KEYS, SKIP, why, seconds=None)
        return
    jwks = file_key_ids(credentials.SERVER_JWKS)
    own = {ns: file_key_ids(credentials.namespace_jwks(ns)) for ns in PROTECTED}

    print(f"Checking the keys that {fed.origin_web_url} publishes ...")
    print(f"\n{'issuer':<11} {'document':<10} {'code':>4}  result")
    answer, server, problems = get_jwks(
        f"{fed.origin_web_url}/.well-known/issuer.jwks", out, "keys-server-jwks"
    )
    if server is not None:
        problems = credentials.server_jwks_problems(server, jwks, own)
    record(
        report,
        f"{'server':<11} {'jwks':<10}",
        f"{KEYS} server jwks",
        answer,
        "; ".join(problems),
    )

    for ns in PROTECTED:
        if ns not in fed.exports:
            continue
        issuer = fed.issuer_of(ns)
        jwks_uri = f"{issuer}/.well-known/issuer.jwks"

        answer = web.request("GET", f"{issuer}/.well-known/openid-configuration")
        save(f"{out}/responses/keys-{ns}-discovery", answer)
        document = common.as_object(answer.json())
        why = ""
        if answer.status != 200:
            why = "expected 200"
        elif document is None:
            why = "the body is not a discovery document"
        elif document.get("jwks_uri") != jwks_uri:
            why = f"jwks_uri is {document.get('jwks_uri')}, not {jwks_uri}"
        record(report, f"{ns:<11} {'discovery':<10}", f"{KEYS} {ns} discovery", answer, why)

        answer, kids, problems = get_jwks(jwks_uri, out, f"keys-{ns}-jwks")
        if kids is not None:
            problems = credentials.namespace_jwks_problems(ns, kids, server, own)
        record(report, f"{ns:<11} {'jwks':<10}", f"{KEYS} {ns} jwks", answer, "; ".join(problems))

    if "public" in fed.exports and credentials.has_issuer(fed.exports["public"]):
        jwks_uri = f"{fed.issuer_of('public')}/.well-known/issuer.jwks"
        answer, kids, problems = get_jwks(jwks_uri, out, "keys-public-jwks")
        if kids is not None:
            problems = credentials.public_jwks_problems(kids, server, own)
        record(
            report,
            f"{'public':<11} {'jwks':<10}",
            f"{KEYS} public jwks",
            answer,
            "; ".join(problems),
        )


# --------------------------------------------------------------------------
# The narrow check: a token like `server`'s, but for one object, in
# /protected-a (where the token decides) at every server.


def targets(fed: common.Federation) -> list[tuple[str, str, bool]]:
    """Return every origin and cache.

    Returns:
        For each, its name in the rows, its data URL, and whether it is an
        origin.
    """
    return [(label(o.svc, "origin"), o.url, True) for o in fed.origins] + [
        (label(c.svc, "cache"), c.url, False) for c in fed.caches
    ]


def check_narrow(test: Test, token: str) -> None:
    """GET the test object with token, scoped to it alone, then its sibling.

    The sibling's name begins with the object's: the token's scope is a
    path, not a prefix of names. An origin in a namespace without
    DirectReads refuses both.
    """
    for target, url, direct in targets(test.fed):
        allowed = credentials.decide(ALLOW, test.caps, "get", direct) == ALLOW
        for name, rel, data in (
            ("narrow-in", test.object, test.object_bytes["protected-a"]),
            ("narrow-out", test.sibling, test.sibling_bytes),
        ):
            answer = web.request("GET", test.url(url, rel), token=token)
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            why = ""
            if name == "narrow-in":
                why = read_problem(answer, allowed, data, "the origin takes no direct clients")
            elif answer.status not in (401, 403):
                why = "expected 401 or 403: the token is for the test object alone"
            elif answer.body == data:
                why = "refused, but the body is the sibling"
            test.row(target, "protected-a", "get", name, answer, why)


# --------------------------------------------------------------------------
# The checks of one storage scope: create-only and modify-only, in
# /protected-a, straight to each origin.


def check_scope(test: Test, name: str) -> None:
    """Present a token of the storage scope of check name at every origin.

    name is one of SCOPES. With the token, PUT a new object, PUT over an
    object, and DELETE one, each object new for the purpose (see
    write()). In SciTokens, `modify-only`'s token is of write alone,
    which grants as much.
    """
    fed = test.fed
    flag, replaces = SCOPES[name]
    token = credentials.mint_server(
        fed,
        test.session.path(f"tokens/{name}"),
        "protected-a",
        scopes=(flag,),
        profile=test.profile,
    )
    # The namespace must take writes from direct clients for any to pass.
    writes = credentials.decide(ALLOW, test.caps, "put", direct=True) == ALLOW
    cannot_delete = stores.cannot(fed, "delete")
    # Each write: its row's name, its method, whether the object is there
    # first, and whether the scope allows it.
    tried = (
        ("put-new", "PUT", False, writes),
        ("put-over", "PUT", True, writes and replaces),
        ("delete", "DELETE", True, writes and replaces),
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for op, method, existing, allowed in tried:
            if method == "DELETE" and allowed and cannot_delete:
                test.skip_row(target, "protected-a", op, name, cannot_delete)
                continue
            rel = test.flat(f"{target}-{name}-{op}")
            old = os.urandom(4096) if existing else None
            new = os.urandom(4096) if method == "PUT" else None
            answer, why = write(test, origin, "protected-a", rel, token, allowed, old, new)
            test.row(target, "protected-a", op, name, answer, why)


# --------------------------------------------------------------------------
# The traversal check.


def climbing(start: str, levels: int, rest: str) -> list[str]:
    """Return paths that climb out of namespace start, then go down rest.

    Each steps up levels times: one path for each spelling of STEPS, and
    one in steps that end in an encoded slash.
    """
    paths = [f"/{start}/" + "/".join([step] * levels) + f"/{rest}" for step in STEPS]
    paths.append(f"/{start}/" + "..%2f" * levels + rest)
    return paths


# A climb: a path, the token it presents, and the bytes it must not reach.
Climb = tuple[str, str | None, frozenset[bytes]]


def signing_keys(fed: common.Federation) -> dict[str, bytes]:
    """Return each origin's signing key, by its file name."""
    keys: dict[str, bytes] = {}
    for origin in fed.origins:
        path = common.signing_key(f"issuer-keys/{origin.svc}")
        with open(path, "rb") as f:
            keys[os.path.basename(path)] = f.read()
    return keys


def traversal_climbs(test: Test, sentinel_file: str, sentinels: frozenset[bytes]) -> dict[str, list[Climb]]:
    """Return the traversal check's climbs, by what they climb toward.

    They climb from each exported namespace, with its `server` token of
    the session's profile (none for /public), toward:

      namespace   another protected namespace's test object, where its
                  storage is not the start's own (unlike a pstore's
                  /public and /protected-a)
      store       sentinel_file, a file in each origin's store beside the
                  exports' storage (holding one of sentinels)
      filesystem  the origin's signing key, beside its store, in
                  /fed/issuer-keys, where an origin keeps its store in
                  its own filesystem (at /data)

    The symlink climbs are symlink_climbs()'s.
    """
    fed = test.fed
    tokens = {ns: None if ns == "public" else test.session.token(ns) for ns in test.session.namespaces}
    keys = signing_keys(fed)
    climbs: dict[str, list[Climb]] = {"namespace": [], "store": [], "filesystem": []}
    for start, token in tokens.items():
        for target in (ns for ns in PROTECTED if ns in fed.exports):
            if fed.storage_namespace(target) == fed.storage_namespace(start):
                continue
            forbidden = frozenset({test.object_bytes[target]})
            climbs["namespace"] += [
                (path, token, forbidden) for path in climbing(start, 1, f"{target}/{test.object}")
            ]
        climbs["store"] += [(path, token, sentinels) for path in climbing(start, 1, sentinel_file)]
        for name, key in keys.items():
            climbs["filesystem"] += [
                (path, token, frozenset({key})) for path in climbing(start, 2, f"fed/issuer-keys/{name}")
            ]
    return climbs


def symlink_climbs(
    test: Test, sentinel_file: str, sentinels: frozenset[bytes]
) -> tuple[list[Climb], list[str]]:
    """Make the traversal check's climbs that are symbolic links.

    The links are made in each store on disk, in the first exported
    protected namespace's storage, under data/auth/: one to sentinel_file,
    beside the exports' storage, by a relative path, and one to each
    origin's signing key, by the absolute path its container sees. A
    backend that follows links out of its export would serve what they
    point to.

    Returns:
        The climbs, each with the namespace's `server` token of the
        session's profile, and the links made.
    """
    start = next((ns for ns in PROTECTED if ns in test.session.namespaces), None)
    disk = [o for o in stores.unique(test.fed.origins) if not o.pstore]
    if start is None or not disk:
        return [], []
    # Each link: its name, its target, and what it must not reach.
    links = [("store", f"../../../{sentinel_file}", sentinels)]
    links += [
        (f"key-{name}", f"/fed/issuer-keys/{name}", frozenset({key}))
        for name, key in signing_keys(test.fed).items()
    ]
    made: list[str] = []
    for origin in disk:
        for name, target, _ in links:
            path = f"{origin.store_of(start)}/{test.flat(f'symlink-{name}')}"
            os.symlink(target, path)
            made.append(path)
    token = test.session.token(start)
    climbs: list[Climb] = [
        (f"/{start}/{test.flat(f'symlink-{name}')}", token, forbidden) for name, _, forbidden in links
    ]
    return climbs, made


def check_traversal(test: Test) -> None:
    """Send each climb of the traversal check to every origin and cache.

    The climbs are traversal_climbs()'s and symlink_climbs()'s, each sent
    as is (http.client normalizes nothing). None may come back with the
    bytes it climbs toward. A row per server and kind of climb.
    """
    fed = test.fed
    sentinel_file = f"traversal-{test.run}"
    written: dict[str, bytes] = {}
    links: list[str] = []
    try:
        for origin in stores.unique(fed.origins):
            data = os.urandom(4096)
            error = stores.write_file(origin.store, sentinel_file, data)
            if error:
                die(f"could not write the traversal check's file: {error}")
            written[f"{origin.store}/{sentinel_file}"] = data
        sentinels = frozenset(written.values())
        climbs = traversal_climbs(test, sentinel_file, sentinels)
        climbs["symlink"], links = symlink_climbs(test, sentinel_file, sentinels)
        for target, url, _ in targets(fed):
            for kind, kind_climbs in climbs.items():
                leaks: list[str] = []
                for path, token, forbidden in kind_climbs:
                    answer = web.request("GET", f"{url}{path}", token=token)
                    if answer.body in forbidden:
                        leaks.append(f"GET {path} got {answer.describe()} with what it climbs toward")
                heading = f"{target:<10} {kind:<11} {'get':<8} {TRAVERSAL:<11}"
                case = f"{TRAVERSAL} {target} {kind}"
                if not kind_climbs:
                    record_skip(test.report, heading, case, "nothing to climb toward in this shape")
                elif leaks:
                    print(f"{heading}       FAIL  {leaks[0]}")
                    test.report.add(case, FAIL, common.first(leaks))
                else:
                    print(f"{heading}       PASS  {len(kind_climbs)} paths")
                    test.report.add(case, PASS, f"{len(kind_climbs)} paths")
    finally:
        for path in list(written) + links:
            os.remove(path)


# --------------------------------------------------------------------------
# The hints check.


def check_hints(test: Test) -> None:
    """GET the test object at each V2 cache in each protected namespace.

    Send no token. The refusal must carry what the director's redirect
    does (Pelican's docs/pelican-http-headers.md; director.hints()):
    X-Pelican-Namespace, naming the namespace and that it takes a token,
    and X-Pelican-Authorization, naming its issuer. A client that named
    the cache itself (Client.PreferredCaches) has heard neither from a
    director, and has nowhere else to learn where to get a token. XRootD
    caches are skipped.
    """
    fed = test.fed
    namespaces = [ns for ns in PROTECTED if ns in fed.exports]
    why = ""
    if not fed.caches:
        why = "this shape has no cache"
    elif not namespaces:
        why = "this shape exports no protected namespace"
    if why:
        warn(f"skipping {HINTS}: {why}")
        test.report.add(HINTS, SKIP, why, seconds=None)
        return
    for cache in fed.caches:
        target = label(cache.svc, "cache")
        for namespace in namespaces:
            if cache.kind != "v2":
                test.skip_row(target, namespace, "get", HINTS, "an XRootD cache sends no hints")
                continue
            answer = web.request("GET", f"{cache.url}/{namespace}/{test.object}")
            save(f"{test.out}/responses/{target}-{namespace}-get-{HINTS}", answer)
            found = director.hints(answer)
            issuer = fed.issuer_of(namespace)
            problems: list[str] = []
            if answer.status not in (401, 403):
                problems.append("expected 401 or 403")
            if found.namespace != f"/{namespace}" or found.require_token is not True:
                sent = answer.header("X-Pelican-Namespace")
                problems.append(
                    f"X-Pelican-Namespace '{sent}' does not say that /{namespace} takes a token"
                )
            if found.issuers != (issuer,):
                problems.append(f"X-Pelican-Authorization names {list(found.issuers)}, not {issuer}")
            test.row(target, namespace, "get", HINTS, answer, "; ".join(problems))


# --------------------------------------------------------------------------
# The audience check: a resource server must check a token's audience
# against its own (the WLCG token profile). An origin takes its own
# audience and the profiles' "any", which every other token here names.
# Only the origins are asked: a cache serves tokens minted for any
# origin, so what it should make of an audience is not settled.


def check_audience(test: Test) -> None:
    """GET the test object, and PUT a new one, at every origin.

    Each request is in /protected-a, with a token like `server`'s but for
    OTHER_AUDIENCE: each must be refused, and the PUT store nothing.
    Skip where the origins take no direct clients, since they refuse
    everything direct anyway.
    """
    fed = test.fed
    data = test.object_bytes["protected-a"]
    token = credentials.mint_server(
        fed,
        test.session.path("tokens/audience"),
        "protected-a",
        audience=OTHER_AUDIENCE,
        profile=test.profile,
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for op in ("get", "put"):
            if not test.direct:
                test.skip_row(target, "protected-a", op, "wrong-aud", "the origin takes no direct clients")
                continue
            if op == "get":
                answer = web.request("GET", test.url(origin.url, test.object), token=token)
                why = read_problem(answer, False, data, "the token is for another audience")
            else:
                rel = test.flat(f"{target}-wrong-aud")
                answer, why = write(test, origin, "protected-a", rel, token, False, None, os.urandom(4096))
            test.row(target, "protected-a", op, "wrong-aud", answer, why)


# --------------------------------------------------------------------------
# The query-token check: XRootD caches forward a client's token to an
# origin in the authz query parameter, and a director's redirect carries
# it there too, so every server must take a token in authz, bare or after
# `Bearer `, as it takes one in the Authorization header. The space is
# sent as %20, as they send it: XRootD strips a literal `Bearer%20`, so a
# form-encoded `Bearer+` would hide the token from it.

QUERY_FORMS = {"bare": "{}", "bearer": "Bearer {}"}


def check_query_token(test: Test) -> None:
    """GET the test object at every server, with a token in the authz query.

    The tokens are the `server` one, then the `unknown` one, in
    /protected-a, in each of QUERY_FORMS: the first must get the object,
    and the second be refused. An origin that takes no direct clients
    must refuse both.
    """
    data = test.object_bytes["protected-a"]
    creds = {name: credentials.BY_NAME[name] for name in ("server", "unknown")}
    for target, url, direct in targets(test.fed):
        for form, pattern in QUERY_FORMS.items():
            for name, cred in creds.items():
                token = test.credential_token(cred, "protected-a", "get") or ""
                query = f"authz={quote(pattern.format(token), safe='')}"
                answer = web.request("GET", f"{test.url(url, test.object)}?{query}")
                save(f"{test.out}/responses/{target}-protected-a-get-authz-{form}-{name}", answer)
                allowed = credentials.expected(cred, test.caps, "get", direct) == ALLOW
                why = read_problem(answer, allowed, data)
                test.row(target, "protected-a", "get", f"authz-{form}-{name}", answer, why)


# --------------------------------------------------------------------------
# Tokens that `pelican token create` cannot make (testlib/jwt.py), and
# what a server should make of a read with one.


def raw_token(
    test: Test,
    times: Mapping[str, int] | None = None,
    drop: tuple[str, ...] = (),
    extra: Mapping[str, Any] | None = None,
    head: Mapping[str, Any] | None = None,
    secret: bytes | None = None,
) -> str:
    """Return a token like `server`'s for /protected-a, but altered.

    Its claims are as credentials.server_claims() has them in the
    session's profile, minted now. It is signed with the origins' key, as
    its header's alg says: HS256 with secret, and `none` not at all.

    Args:
        test: The test, under whose header the token is signed unless
            head is given.
        times: Each of `exp`, `iat`, or `nbf` to set, in seconds from
            now.
        drop: The claims to leave out.
        extra: The claims to set.
        head: The header to sign under instead.
        secret: The key for HS256.
    """
    fed = test.fed
    claims = credentials.server_claims(fed, "protected-a", profile=test.profile)
    now = int(time.time())
    for name, offset in (times or {}).items():
        claims[name] = now + offset
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(test.header if head is None else head, claims, key=fed.origin_key, secret=secret)


def read_problem(answer: web.Response, allowed: bool, data: bytes, why: str = "") -> str:
    """Return what is wrong with answer to a GET of an object.

    The object's bytes are data. If allowed, the answer must serve them;
    otherwise it must be refused (401 or 403), without them, because of
    why.
    """
    if allowed:
        if answer.status != 200:
            return "expected 200"
        return "" if answer.body == data else "the body is not the object"
    if answer.status not in (401, 403):
        return "expected 401 or 403" + (f": {why}" if why else "")
    return "refused, but the body is the object" if answer.body == data else ""


def read_either(answer: web.Response, data: bytes) -> tuple[str, str]:
    """Return what a server did with a token that it may take or refuse.

    answer is its answer to a GET of an object whose bytes are data.

    Returns:
        `taken` (it served them), `refused` (401 or 403, without them),
        or "" (neither); and what is wrong if it did neither.
    """
    if answer.status == 200 and answer.body == data:
        return "taken", ""
    if answer.status in (401, 403) and answer.body != data:
        return "refused", ""
    return "", "expected the object (200), or 401 or 403 without it"


# --------------------------------------------------------------------------
# The expired-in-use check: a server that has taken a token must refuse it
# once it expires, however recently it was used, even where the server
# remembers what the token authorizes.

# How long the check's token lasts, and when, in seconds after it is
# minted, each server is asked with it: every 30 seconds for three
# minutes.
IN_USE_LIFETIME = 30
IN_USE_TIMES = tuple(range(0, 181, 30))
# How long past its expiry a server may still take a token: 60 seconds of
# clock skew, and a margin.
STALE_AFTER = 70


class InUse:
    """The expired-in-use check, which runs in a thread of its own.

    The thread starts at once, and asks every server in /protected-a
    while the suite's other checks run. record() records its rows once it
    is done.

    Attributes:
        targets: Each server: its name in the rows, its data URL, and
            whether a token decides there.
        answers: What each server answered, by its name in the rows, and
            when, in seconds past the token's expiry.
    """

    def __init__(self, test: Test):
        self.test = test
        fed = test.fed
        # An origin that takes no direct clients refuses everything
        # direct.
        self.targets = [
            (target, url, credentials.decide(ALLOW, test.caps, "get", direct) == ALLOW)
            for target, url, direct in targets(fed)
        ]
        claims = credentials.server_claims(
            fed, "protected-a", lifetime=IN_USE_LIFETIME, profile=test.profile
        )
        self.minted, self.expiry = int(claims["iat"]), int(claims["exp"])
        self.token = jwt.encode(test.header, claims, key=fed.origin_key)
        self.answers: dict[str, list[tuple[int, web.Response]]] = {
            target: [] for target, _, _ in self.targets
        }
        self.thread = threading.Thread(target=self.ask, daemon=True)
        self.thread.start()

    def ask(self) -> None:
        """GET the test object at each server where a token decides.

        Ask at each of IN_USE_TIMES, but no more of a server that refused
        the token (401 or 403) before its expiry: what it does after can
        show nothing of expiry. Once none is left to ask, stop.
        """
        asked = [(target, url) for target, url, decides in self.targets if decides]
        for offset in IN_USE_TIMES:
            if not asked:
                return
            delay = self.minted + offset - time.time()
            if delay > 0:
                time.sleep(delay)
            for target, url in list(asked):
                past = int(time.time()) - self.expiry
                answer = web.request("GET", self.test.url(url, self.test.object), token=self.token)
                self.answers[target].append((past, answer))
                if past < 0 and answer.status in (401, 403):
                    asked.remove((target, url))

    def record(self) -> None:
        """Wait for the thread, then record a row per server.

        Each server must take the token before its expiry, and refuse it
        from STALE_AFTER seconds past it on; in between, either. The note
        lists what it answered, and when.
        """
        self.thread.join()
        data = self.test.object_bytes["protected-a"]
        for target, _, decides in self.targets:
            if not decides:
                self.test.skip_row(
                    target, "protected-a", "get", EXPIRED_IN_USE, "the origin takes no direct clients"
                )
                continue
            answers = self.answers[target]
            seen = ", ".join(
                f"{past:+d}s: {'none' if a.status is None else a.status}" for past, a in answers
            )
            problems: list[str] = []
            for past, answer in answers:
                if past < 0:
                    why = read_problem(answer, True, data)
                    if why:
                        problems.append(f"{why} {-past}s before its expiry")
                elif past >= STALE_AFTER:
                    why = read_problem(answer, False, data, "the token has expired")
                    if why:
                        problems.append(f"{why} {past}s past its expiry")
            if not answers:
                problems.append("it was never asked")
            last = answers[-1][1] if answers else web.Response(None)
            why = f"{common.first(problems)} (seconds past expiry: {seen})" if problems else ""
            note = f"seconds past expiry: {seen}"
            self.test.row(target, "protected-a", "get", EXPIRED_IN_USE, last, why, note)


# --------------------------------------------------------------------------
# The time-claims check: a server should allow 60 seconds of clock skew on
# each of a token's times, as the WLCG token profile recommends, and no
# more.

# Each token: its row's name, its times (see raw_token()), and whether a
# server must take it where a token decides.
TIME_CASES: tuple[tuple[str, dict[str, int], bool], ...] = (
    ("nbf+300", {"nbf": 300}, False),
    ("iat+300", {"iat": 300}, False),
    ("nbf+20", {"nbf": 20}, True),
    ("exp-120", {"iat": -600, "nbf": -600, "exp": -120}, False),
)


def check_time_claims(test: Test) -> None:
    """GET the test object with each of TIME_CASES's tokens at every server.

    The requests are in /protected-a, each with a token minted afresh,
    since a V2 cache remembers a token it refused for 30 seconds.
    """
    data = test.object_bytes["protected-a"]
    for target, url, direct in targets(test.fed):
        for name, times, takes in TIME_CASES:
            answer = web.request("GET", test.url(url, test.object), token=raw_token(test, times=times))
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            allowed = credentials.decide(ALLOW if takes else DENY, test.caps, "get", direct) == ALLOW
            test.row(target, "protected-a", "get", name, answer, read_problem(answer, allowed, data))


# --------------------------------------------------------------------------
# The malformed-claims check. The WLCG token profile requires `exp` and
# `aud`, as SciTokens 2.0 does. A server must also refuse a token with no
# `kid`, or one that no key has, and must check a signature by its key's
# alg, not the token's.


# Each profile's version that it does not define: its row's name, and the
# version claim's value.
UNDEFINED_VERSIONS = {
    credentials.WLCG: ("wlcg.ver-2", "2.0"),
    credentials.SCITOKENS: ("ver-3", "scitokens:3.0"),
}


def malformed_tokens(test: Test) -> list[tuple[str, str, bool | None]]:
    """Return the malformed-claims check's tokens.

    Each is like `server`'s for /protected-a but for one thing.

    Returns:
        For each, its row's name, the token, and whether a server must
        take it where a token decides (None: it may or may not, as for a
        version the profile does not define).
    """
    head = test.header
    no_kid = {k: v for k, v in head.items() if k != "kid"}
    # The origins' public key, as a JWK: what an attacker would key HS256
    # with, hoping that a server verifies with the key's bytes whatever
    # the token's alg.
    secret = json.dumps(
        {**jwt.public_jwk(test.fed.origin_key), "kid": head.get("kid")}, separators=(",", ":")
    ).encode()
    version, _ = credentials.VERSIONS[test.profile]
    undefined, value = UNDEFINED_VERSIONS[test.profile]
    return [
        ("no-exp", raw_token(test, drop=("exp",)), False),
        ("no-kid", raw_token(test, head=no_kid), False),
        ("unknown-kid", raw_token(test, head={**head, "kid": "pelican-test-unknown"}), False),
        ("alg-none", raw_token(test, head={**head, "alg": jwt.NONE}), False),
        ("hs256", raw_token(test, head={**head, "alg": jwt.HS256}, secret=secret), False),
        (undefined, raw_token(test, extra={version: value}), None),
        ("no-aud", raw_token(test, drop=("aud",)), False),
    ]


def check_malformed(test: Test) -> None:
    """GET the test object at every server with each of malformed_tokens().

    The requests are in /protected-a. The note of a token that may be
    taken or refused says which.
    """
    data = test.object_bytes["protected-a"]
    tokens = malformed_tokens(test)
    for target, url, direct in targets(test.fed):
        for name, token, takes in tokens:
            answer = web.request("GET", test.url(url, test.object), token=token)
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            note = ""
            if takes is None and credentials.decide(ALLOW, test.caps, "get", direct) == ALLOW:
                note, why = read_either(answer, data)
            else:
                allowed = credentials.decide(ALLOW if takes else DENY, test.caps, "get", direct)
                why = read_problem(answer, allowed == ALLOW, data)
            test.row(target, "protected-a", "get", name, answer, why, note)


# --------------------------------------------------------------------------
# The audience-specific check: an origin takes a token for its own
# audience, Origin.TokenAudience, whose default is Origin.Url; and a token
# is for every audience it lists.


def audience_of(origin: common.Origin) -> str:
    """Return the audience that origin takes as its own (Origin.Url).

    It is the scheme, host, and port of its data URL (for an XRootD
    origin, XRootD's).
    """
    parts = urlsplit(origin.url)
    return f"{parts.scheme}://{parts.netloc}"


def check_audience_specific(test: Test) -> None:
    """GET the test object at every origin with tokens that list audiences.

    In /protected-a, the tokens are like `server`'s but for the first
    origin's audience alone, which only it may take; and for the
    profile's "any" audience and OTHER_AUDIENCE, which every origin must
    take. Only the origins are asked, and skipped as in check_audience().
    """
    fed = test.fed
    data = test.object_bytes["protected-a"]
    first = fed.origins[0]
    own = audience_of(first)
    # Each token: its row's name, the token, and the one origin that may
    # take it (None: every origin).
    tokens = (
        (f"aud-{label(first.svc, 'origin')}", raw_token(test, extra={"aud": [own]}), first),
        (
            "aud-list",
            raw_token(test, extra={"aud": [credentials.ANY_AUDIENCE[test.profile], OTHER_AUDIENCE]}),
            None,
        ),
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, token, only in tokens:
            if not test.direct:
                test.skip_row(target, "protected-a", "get", name, "the origin takes no direct clients")
                continue
            answer = web.request("GET", test.url(origin.url, test.object), token=token)
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            allowed = only is None or origin == only
            why = read_problem(answer, allowed, data, f"the token is for {own} alone")
            test.row(target, "protected-a", "get", name, answer, why)


# --------------------------------------------------------------------------
# The token-lists check: a native origin takes tokens from its
# Authorization header, several separated by commas, each after a bearer
# scheme in any case (RFC 9110, section 11.1), and from the access_token
# (RFC 6750, section 2.3) and authz query parameters, each of which may
# repeat; tries each in turn; and treats a request with more than
# MAX_TOKENS as having none (extractTokens in Pelican's
# origin_serve/handlers.go). A V2 cache and XRootD take one token, from
# the header or authz, and are not asked.

MAX_TOKENS = 10
# What must become of a request whose tokens an origin treats as none: a
# 401, as for a request with none.
NO_TOKEN = "no-token"

# A way of presenting tokens: its row's name, the headers and the query
# parameters that carry them, and what must become of the request where a
# token decides (ALLOW, DENY, or NO_TOKEN).
TokenForm = tuple[str, dict[str, str], list[tuple[str, str]], str]


def token_forms(server: str, unknown: str) -> list[TokenForm]:
    """Return the token-lists check's ways of presenting tokens.

    Args:
        server: The `server` token, which must be taken.
        unknown: The `unknown` token, which must not.
    """
    many = [f"Bearer {unknown}"] * MAX_TOKENS + [f"Bearer {server}"]
    return [
        ("access-token", {}, [("access_token", server)], ALLOW),
        ("comma", {"Authorization": f"Bearer {unknown}, Bearer {server}"}, [], ALLOW),
        ("authz-twice", {}, [("authz", unknown), ("authz", server)], ALLOW),
        ("lowercase", {"Authorization": f"bearer {server}"}, [], ALLOW),
        ("basic", {"Authorization": f"Basic {server}"}, [], DENY),
        ("eleven", {"Authorization": ", ".join(many)}, [], NO_TOKEN),
    ]


def check_token_lists(test: Test) -> None:
    """Present tokens in each of token_forms() at every native origin.

    Each GETs the test object in /protected-a. XRootD origins are
    skipped.
    """
    fed = test.fed
    data = test.object_bytes["protected-a"]
    server = test.credential_token(credentials.BY_NAME["server"], "protected-a", "get") or ""
    unknown = test.credential_token(credentials.BY_NAME["unknown"], "protected-a", "get") or ""
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, headers, query, verdict in token_forms(server, unknown):
            if not fed.native_origins:
                why = f"XRootD serves the origins ({fed.origin_variant}), not Pelican"
                test.skip_row(target, "protected-a", "get", name, why)
                continue
            url = test.url(origin.url, test.object)
            if query:
                url += f"?{urlencode(query)}"
            answer = web.request("GET", url, headers=headers)
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            decided = credentials.decide(ALLOW if verdict == ALLOW else DENY, test.caps, "get", True)
            why = read_problem(answer, decided == ALLOW, data)
            if not why and verdict == NO_TOKEN and decided == DENY and test.direct:
                if answer.status != 401:
                    why = f"expected 401: more than {MAX_TOKENS} tokens count as none"
            test.row(target, "protected-a", "get", name, answer, why)


# --------------------------------------------------------------------------
# The standalone-refusals check: a standalone origin has no director to
# tell a client what a namespace wants, so it tells it itself, on every
# response, as a director's redirect does (HINT_HEADERS), and refuses a
# request with no token with 401, and one whose token falls short with
# 403, which sends the Pelican client to get the token it names. A
# federated origin leaves that to the director, and answers 401 either
# way, since a 401 tells rclone to run its token command again
# (authMiddleware in Pelican's origin_serve/handlers.go).

HINT_HEADERS = ("X-Pelican-Namespace", "X-Pelican-Authorization", "X-Pelican-Token-Generation")


def hint_problems(answer: web.Response, standalone: bool, issuer: str) -> list[str]:
    """Return what is wrong with the hints of an origin's answer.

    The answer is in /protected-a, whose issuer is issuer. A standalone
    origin must send each of HINT_HEADERS, saying that the namespace takes
    a token and naming its issuer; a federated one, none.
    """
    sent = [h for h in HINT_HEADERS if answer.header(h)]
    if not standalone:
        return [f"a federated origin sent {', '.join(sent)}"] if sent else []
    missing = [h for h in HINT_HEADERS if h not in sent]
    if missing:
        return [f"no {', '.join(missing)}"]
    found = director.hints(answer)
    problems: list[str] = []
    if found.namespace != "/protected-a" or found.require_token is not True:
        problems.append(
            f"X-Pelican-Namespace '{answer.header(HINT_HEADERS[0])}' does not say that"
            + " /protected-a takes a token"
        )
    if found.issuers != (issuer,):
        problems.append(f"X-Pelican-Authorization names {list(found.issuers)}, not {issuer}")
    if found.generation_issuer != issuer:
        problems.append(
            f"X-Pelican-Token-Generation names {found.generation_issuer or 'no issuer'}," + f" not {issuer}"
        )
    return problems


def check_standalone(test: Test) -> None:
    """GET the test object, and a missing one, at every native origin.

    Ask for the test object, in /protected-a, with no token, `unknown`'s,
    and `server`'s, and for the missing one with `server`'s: the first
    must get 401; the second 403 from a standalone origin and 401 from a
    federated one; the third the object; and the last 404. Each answer's
    hints must be as hint_problems() says. XRootD origins are skipped,
    and so are origins that take no direct clients, which refuse every
    request alike.
    """
    fed = test.fed
    data = test.object_bytes["protected-a"]
    issuer = fed.issuer_of("protected-a")
    server = test.credential_token(credentials.BY_NAME["server"], "protected-a", "get")
    unknown = test.credential_token(credentials.BY_NAME["unknown"], "protected-a", "get")
    # Each request: its row's name, what it asks for, its token, and the
    # status it must get.
    cases = (
        ("hdr-none", test.object, None, 401),
        ("hdr-unknown", test.object, unknown, 403 if fed.standalone else 401),
        ("hdr-server", test.object, server, 200),
        ("hdr-missing", test.flat("missing"), server, 404),
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, rel, token, status in cases:
            if not fed.native_origins:
                why = f"XRootD serves the origins ({fed.origin_variant}), not Pelican"
                test.skip_row(target, "protected-a", "get", name, why)
                continue
            if not test.direct:
                test.skip_row(target, "protected-a", "get", name, "the origin takes no direct clients")
                continue
            answer = web.request("GET", test.url(origin.url, rel), token=token)
            save(f"{test.out}/responses/{target}-protected-a-get-{name}", answer)
            problems: list[str] = []
            if answer.status != status:
                problems.append(f"expected {status}")
            elif status == 200 and answer.body != data:
                problems.append("the body is not the object")
            if status != 200 and answer.body == data:
                problems.append("refused, but the body is the object")
            problems += hint_problems(answer, fed.standalone, issuer)
            test.row(target, "protected-a", "get", name, answer, "; ".join(problems))


# --------------------------------------------------------------------------
# The web-token-strict check: a server should verify the tokens of its own
# web API with no clock skew, since it alone mints and checks them
# (VerifyWithKeysetStrict in Pelican's token/token_verify.go).


def web_servers(fed: common.Federation) -> list[tuple[str, str, str]]:
    """Return every origin's and cache's web API, each once.

    In the `tiny` topology, one server is both.

    Returns:
        For each, its server's name in the rows, its web URL, and the
        directory (under framework/var) of its keys.
    """
    found: dict[str, tuple[str, str]] = {}
    for origin in fed.origins:
        found.setdefault(origin.web_url, (label(origin.svc, "origin"), f"issuer-keys/{origin.svc}"))
    for cache in fed.caches:
        found.setdefault(cache.web_url, (label(cache.svc, "cache"), f"issuer-keys/{cache.svc}"))
    return [(name, url, keys) for url, (name, keys) in found.items()]


def check_web_token_strict(test: Test) -> None:
    """Ask each server's web API who is logged in (/whoami).

    Ask first with a token that it takes as its admin's
    (credentials.mint_web_user()), which it must take; then with the same
    token, but not valid for another 20 seconds, and expired 5 seconds
    ago, each of which it must refuse (401) or take for no one.
    """
    fed = test.fed
    for target, web_url, key_dir in web_servers(fed):
        key = common.signing_key(key_dir)
        out = test.session.path(f"tokens/web.{target}")
        fresh = credentials.mint_web_user(fed, out, web_url, "admin", key=key)
        head, claims = jwt.decode(fresh)
        now = int(time.time())
        early = {**claims, "iat": now, "nbf": now + 20}
        late = {**claims, "iat": now - 300, "nbf": now - 300, "exp": now - 5}
        cases = (
            ("web-fresh", fresh, True),
            ("web-nbf+20", jwt.encode(head, early, key=key), False),
            ("web-exp-5", jwt.encode(head, late, key=key), False),
        )
        for name, token, takes in cases:
            answer = web.request("GET", web_url + accounts.WHOAMI, token=token)
            save(f"{test.out}/responses/{target}-whoami-{name}", answer)
            authenticated = answer.object().get("authenticated")
            why = ""
            if takes and (answer.status != 200 or authenticated is not True):
                why = "expected 200, authenticated: the token is the server's own, and fresh"
            elif not takes and answer.status != 401:
                if answer.status != 200 or authenticated is not False:
                    why = "expected 401, or 200 and unauthenticated: the token is not valid now"
            test.row(target, "web-api", "whoami", name, answer, why)


# --------------------------------------------------------------------------
# The narrow-climb check: a token for one collection must reach nothing
# beside it, however a path climbs out of it.


def climbs_from(scope: str, rest: str) -> list[str]:
    """Return paths that climb out of collection scope, then go down rest.

    scope is in /protected-a, relative to it. The paths begin in scope,
    step up once, and go down rest: climbing()'s, but for %252e%252e,
    which a server decodes once to a name inside scope.
    """
    return [p for p in climbing(f"protected-a/{scope}", 1, rest) if "%252e" not in p]


def check_narrow_climb(test: Test) -> None:
    """With a narrow token, climb out of its collection to its sibling.

    The token is for collection data/auth/<run>-narrow in /protected-a
    alone, and the climbs are climbs_from()'s. GET the test object at
    every server, and at every origin where the namespace takes writes,
    PUT a new object, and DELETE one put there for the purpose. Every
    request must be refused (4xx), without the object, and change
    nothing. Each cache must first serve the test object with the
    server's token, so that it refuses an object it holds.
    """
    fed = test.fed
    data = test.object_bytes["protected-a"]
    scope = test.flat("narrow")
    token = credentials.mint_server(
        fed,
        test.session.path("tokens/narrow-climb"),
        "protected-a",
        scope_path=f"/{scope}",
        profile=test.profile,
    )
    for cache in fed.caches:
        target = label(cache.svc, "cache")
        held = web.request("GET", test.url(cache.url, test.object), token=test.setup)
        if web.served(held, data):
            why = "could not first read the test object through it with the server's token"
            test.row(target, "protected-a", "get", NARROW_CLIMB, held, why)
            continue
        climb(test, target, cache.url, None, "get", scope, token)
    writes = credentials.decide(ALLOW, test.caps, "put", direct=True) == ALLOW
    cannot_delete = stores.cannot(fed, "delete")
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        climb(test, target, origin.url, origin, "get", scope, token)
        if not writes:
            continue
        climb(test, target, origin.url, origin, "put", scope, token)
        if cannot_delete:
            test.skip_row(target, "protected-a", "delete", NARROW_CLIMB, cannot_delete)
        else:
            climb(test, target, origin.url, origin, "delete", scope, token)


def climb(
    test: Test,
    target: str,
    url: str,
    origin: common.Origin | None,
    op: str,
    scope: str,
    token: str,
) -> None:
    """Send op (get, put, or delete) by each of climbs_from() scope.

    Send it to the server at url (origin, if it is one) with token, a
    token for scope alone, and record a row. A GET climbs to the test
    object; a PUT, to a new object; and a DELETE, to one put there first.
    """
    data = test.object_bytes["protected-a"]
    rel = test.flat({"get": "object", "put": f"climbed-{target}"}.get(op, f"climbed-delete-{target}"))
    old = os.urandom(4096)
    if op == "delete" and origin is not None:
        error = stores.seed(origin, "protected-a", rel, old, test.setup, test.caps)
        if error:
            why = f"could not put the object to delete in place ({error})"
            test.outcome_row(target, "protected-a", op, NARROW_CLIMB, [why], "")
            return
    paths = climbs_from(scope, os.path.basename(rel))
    problems: list[str] = []
    for path in paths:
        upload = os.urandom(4096) if op == "put" else None
        answer = web.request(op.upper(), f"{url}{path}", token=token, upload=upload)
        if answer.status is None or not 400 <= answer.status < 500:
            problems.append(f"{op.upper()} {path} got {answer.describe()}, not a refusal (4xx)")
        if op == "get" and answer.body == data:
            problems.append(f"GET {path} got the object")
    if origin is not None and op != "get":
        try:
            have = test.stored(origin, "protected-a", rel)
        except stores.Unreadable:
            problems.append("could not check the store")
        else:
            if op == "put" and have is not None:
                problems.append("the store holds what a PUT climbed to")
            elif op == "delete" and have != common.digest(old):
                problems.append("the object that a DELETE climbed to is gone")
    test.outcome_row(target, "protected-a", op, NARROW_CLIMB, problems, f"{len(paths)} paths")


# --------------------------------------------------------------------------
# The nested-protected check: under `-p topo-multi-owner`, origin-2 exports
# /public/secret, which a token protects, inside the origins' /public,
# which anyone may read (fed_owner_exports in fed.sh). No server may let
# the public prefix's reads reach the protected one inside it.

# How many times the owner tries to read the object through a cache, and
# the seconds between tries: a cache learns of a namespace from the
# director, every minute or so.
NESTED_TRIES, NESTED_INTERVAL = 8, 15


def nested_export(fed: common.Federation) -> tuple[common.Owner, common.Export] | None:
    """Return another owner's export that a token protects, and its owner.

    The export is inside a namespace of the origins that anyone may read.

    Returns:
        The owner and the export, or None if there is none.
    """
    public = [ns for ns, caps in fed.exports.items() if "PublicReads" in caps]
    for owner in fed.owners:
        for export in owner.exports:
            inside = any(export.prefix.startswith(f"/{ns}/") for ns in public)
            if inside and "PublicReads" not in export.caps:
                return owner, export
    return None


def owner_read(url: str, token: str, data: bytes) -> web.Response:
    """GET url with token until it serves data, NESTED_TRIES times at most.

    Returns:
        The last answer.
    """
    answer = web.Response(None)
    for attempt in range(NESTED_TRIES):
        if attempt:
            time.sleep(NESTED_INTERVAL)
        answer = web.request("GET", url, token=token)
        if answer.status == 200 and answer.body == data:
            break
    return answer


def check_nested(test: Test) -> None:
    """Put an object in the nested export (nested_export()), then GET it.

    Put it at its owner's origin, and have the owner read it through each
    cache, so that each holds it; then GET it with no token, and with one
    that names the prefix's issuer but is signed by test-keys/unknown.pem,
    at each cache, at the owner's origin, and at each director's bare
    path, following its redirect. Each must be refused (401 or 403),
    without the object.
    """
    fed = test.fed
    found = nested_export(fed)
    if found is None:
        why = "there is no protected prefix inside a public one (-p topo-multi-owner)"
        warn(f"skipping {NESTED}: {why}")
        test.report.add(NESTED, SKIP, why, seconds=None)
        return
    owner, export = found
    storage = f"{owner.origin.store_of(export.path)}/data/auth"
    rel = test.flat("nested")
    path = f"{export.prefix}/{rel}"
    data = os.urandom(65536)
    # The owner's token puts the object in place, and has each cache hold
    # it.
    owner_token = nested_token(test, export, "owner", owner.key, RWM, credentials.WLCG)
    # Each token presented: its row's name, and the token.
    unknown = nested_token(test, export, "unknown", "test-keys/unknown.pem", R, test.profile)
    tokens = (("none", None), ("unknown", unknown))
    refusal = f"{export.prefix} takes a token"
    common.empty_dir(storage)
    try:
        error = stores.seed(owner.origin, export.path, rel, data, owner_token, export.caps)
        if error:
            why = f"could not put the object in {export.prefix} at {owner.origin.svc} ({error})"
            print(f"{'':<10} {export.name:<11} {'put':<8} {NESTED:<11}       FAIL  {why}")
            test.report.add(f"{NESTED} put", FAIL, why)
            return
        for cache in fed.caches:
            target = label(cache.svc, "cache")
            held = owner_read(f"{cache.url}{path}", owner_token, data)
            for name, token in tokens:
                if held.status != 200 or held.body != data:
                    why = "the owner could not first read the object through it"
                    test.row(target, export.name, "get", name, held, why)
                    continue
                answer = web.request("GET", f"{cache.url}{path}", token=token)
                test.row(
                    target,
                    export.name,
                    "get",
                    name,
                    answer,
                    read_problem(answer, False, data, refusal),
                )
        target = label(owner.origin.svc, "origin")
        for name, token in tokens:
            answer = web.request("GET", f"{owner.origin.url}{path}", token=token)
            test.row(
                target,
                export.name,
                "get",
                name,
                answer,
                read_problem(answer, False, data, refusal),
            )
        for url in fed.directors:
            target = director.host(url)
            for name, token in tokens:
                answer = web.request("GET", f"{url}{path}", token=token)
                note = ""
                if answer.status == 307:
                    location = answer.header("Location")
                    note = f"redirected to {director.host(location)}"
                    answer = web.request("GET", location, token=token)
                why = read_problem(answer, False, data, refusal)
                if why and note:
                    why = f"{note}, {why}"
                test.row(target, export.name, "get", name, answer, why, note)
    finally:
        common.empty_dir(storage)


def nested_token(
    test: Test,
    export: common.Export,
    name: str,
    key: str,
    scopes: tuple[str, ...],
    profile: str,
) -> str:
    """Return a token of scopes for the whole of export, naming its issuer.

    It is of profile, signed with key (a PEM file under framework/var),
    and kept as name.
    """
    out = test.session.path(f"tokens/nested.{name}")
    credentials.mint(
        test.fed,
        out,
        f"{export.prefix}/",
        key,
        export.issuer,
        "/",
        credentials.LONG,
        scopes,
        profile=profile,
    )
    return common.read_text(out)


def skip(_fed: common.Federation) -> str | None:
    """Return None: there is always something to test.

    Every shape exports a namespace, where the credentials are presented,
    even where no token decides there (`-p origin-public`).
    """
    return None


def runnable(fed: common.Federation, selected: list[str], report: Report) -> list[Credential]:
    """Return the selected credentials that can test something in the shape.

    Record the others as skipped. `server` goes first, so that each cache
    holds the test object before anything is refused: a stored object
    must be protected too. `expired` goes last, since it must wait out
    every server's clock skew.
    """
    found: list[Credential] = []
    for cred in (c for c in CREDENTIALS if c.name in selected):
        why = credentials.moot(fed, cred)
        if why:
            warn(f"skipping {cred.name}: {why}")
            report.add(cred.name, SKIP, why, seconds=None)
        else:
            found.append(cred)
    found.sort(key=lambda c: (c.name != "server", c.expired))
    return found


def put_test_objects(test: Test, narrow: bool) -> None:
    """Put each namespace's test object at every origin.

    Put it in each namespace with storage of its own (see
    stores.storage_namespaces()), with the `server` credential; and for
    `narrow`, the object's sibling in /protected-a. Earlier runs' objects
    come out first. The federation suite has already waited for each
    cache to accept the server's token.
    """
    fed = test.fed
    for origin in fed.origins:
        if not origin.pstore:
            for namespace in fed.exports:
                common.empty_dir(f"{origin.store_of(namespace)}/data/auth")
    print("Putting the test object at each origin ...")
    for origin in fed.origins:
        for namespace in stores.storage_namespaces(origin, fed.exports):
            token, caps = test.setup_tokens[namespace], fed.exports[namespace]
            data = test.object_bytes[namespace]
            error = stores.seed(origin, namespace, test.object, data, token, caps)
            if error:
                die(
                    f"could not put the test object at {origin.svc} in /{namespace} with the"
                    + f" server's own token ({error}); is it up (./fed.sh status)?"
                )
        for namespace in test.session.namespaces:
            want = common.digest(test.object_bytes[namespace])
            try:
                intact = test.stored(origin, namespace, test.object) == want
            except stores.Unreadable:
                intact = False
            if not intact:
                die(f"{origin.svc} did not store the test object intact in /{namespace}")
        if narrow:
            error = stores.seed(
                origin, "protected-a", test.sibling, test.sibling_bytes, test.setup, test.caps
            )
            if error:
                die(f"could not put the test object's sibling at {origin.svc} ({error})")


def cache_sibling(test: Test) -> None:
    """GET the object's sibling through each cache, with the server's token.

    Then `narrow-out` asks each cache for an object it holds.
    """
    for cache in test.fed.caches:
        answer = web.request("GET", test.url(cache.url, test.sibling), token=test.setup)
        why = ""
        if answer.status != 200:
            why = "expected 200: the server's token is good for the sibling"
        elif answer.body != test.sibling_bytes:
            why = "the body is not the sibling"
        test.row(label(cache.svc, "cache"), "protected-a", "get", "narrow-sibling", answer, why)


def narrow_token(test: Test) -> str:
    """Return a token like `server`'s, but for the test object alone."""
    path = test.session.path("tokens/narrow")
    return credentials.mint_server(
        test.fed, path, "protected-a", scope_path=f"/{test.object}", profile=test.profile
    )


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected checks.

    Check the keys, and start `expired-in-use`; then present each
    credential, then run the other checks, `traversal` last; then record
    `expired-in-use`'s rows.
    """
    report = results.report
    fed = session.fed
    creds = runnable(fed, selected, report)
    # The selected checks that are neither `keys` nor a credential.
    wanted = {n for n in selected if n != KEYS and n not in credentials.BY_NAME}
    reason = suite.unexported(fed)
    if reason:
        for name in (n for n in PROTECTED_A_CHECKS if n in wanted):
            warn(f"skipping {name}: {reason}")
            report.add(name, SKIP, reason, seconds=None)
            wanted.discard(name)
    narrow = NARROW in wanted
    checks = bool(creds or wanted)
    if checks:
        stores.require_dirs(fed.origins, fed.exports, "data/auth")

    out = session.var_dir(OUT)
    shutil.rmtree(out, ignore_errors=True)
    for sub in ("responses", "sources"):
        os.makedirs(f"{out}/{sub}")

    if KEYS in selected:
        check_keys(fed, report, out)
        if checks:
            print()
    if not checks:
        return

    test = Test(session, report, out)
    for namespace, data in test.object_bytes.items():
        with open(f"{out}/object-{namespace}", "wb") as f:
            f.write(data)
    put_test_objects(test, narrow)
    # It takes three minutes, which the other checks fill.
    in_use = InUse(test) if EXPIRED_IN_USE in wanted else None

    print(f"\n{'target':<10} {'ns':<11} {'op':<8} {'credential':<11} {'code':>4}  result")
    if narrow:
        cache_sibling(test)
    for cred in creds:
        check_credential(test, cred)
    # The other checks, in this order: those of one storage scope after
    # `narrow`, and `traversal` last.
    others: dict[str, Callable[[Test], None]] = {
        HINTS: check_hints,
        QUERY_TOKEN: check_query_token,
        AUDIENCE: check_audience,
        TOKEN_LISTS: check_token_lists,
        AUDIENCE_SPECIFIC: check_audience_specific,
        TIME_CLAIMS: check_time_claims,
        MALFORMED: check_malformed,
        STANDALONE: check_standalone,
        WEB_TOKEN_STRICT: check_web_token_strict,
        NARROW: lambda t: check_narrow(t, narrow_token(t)),
        **{name: partial(check_scope, name=name) for name in SCOPES},
        NARROW_CLIMB: check_narrow_climb,
        NESTED: check_nested,
        TRAVERSAL: check_traversal,
    }
    for name, check in others.items():
        if name in wanted:
            check(test)
    if in_use is not None:
        print(
            f"\nWaiting for {EXPIRED_IN_USE}, which asks each server until"
            + f" {IN_USE_TIMES[-1]}s after its token was minted ..."
        )
        in_use.record()

    passed = report.count(PASS)
    skipped = report.count(SKIP)
    ending = f", {skipped} skipped." if skipped else "."
    print(f"\n{passed} passed, {report.failed} failed{ending}")
    print(f"Responses are in framework/var/{out}/.")
