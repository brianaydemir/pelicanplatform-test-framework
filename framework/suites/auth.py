"""Authorization at each server directly, in every namespace.

GET, HEAD, PUT, and DELETE go to every origin, and GET and HEAD to every
cache, once for each credential in the table in
framework/testlib/credentials.py. A request that should be allowed must
succeed with the right bytes (a HEAD, with the object's length), or remove
the object. Any other must get 401 or 403 and change nothing. First, the
`keys` check fetches each namespace issuer's discovery document and JWKS,
and the origins' server-wide JWKS, and checks which test keys each
publishes. Last, the `narrow` check presents a token for the test object
alone in /protected-a, at every server, for it and then for its sibling; the
`create-only` and `modify-only` checks present tokens of one storage scope
each at every origin, which may create an object, replace one, and delete
one, as the WLCG token profile says; the `traversal` check sends paths that
climb out of their namespace with `..`, which must reach neither another
namespace's test object nor a file beyond an export's storage; the `hints`
check asks each V2 cache for the test object with no token, whose refusal
must say which namespace it is and name its issuer, as the director's
redirect does; the `audience` check presents the origins a token like
`server`'s but for another audience, which they must refuse; and the
`query-token` check presents the `server` and `unknown` tokens in the authz
query parameter instead of the header, as XRootD caches forward them, which
every server must take as it takes the header.

Other checks present tokens that `pelican token create` cannot make
(testlib/jwt.py), each wrong in one way, in /protected-a: times ahead
of the clock or past it (`time-claims`), and a token presented before
and after it expires (`expired-in-use`, which runs in a thread of its
own beside the rest, since it takes three minutes); claims missing or
malformed, and signatures by none or HS256 (`malformed-claims`); an
audience of one origin (`audience-specific`); SciTokens scopes
(`scitokens-profile`); and to a server's own web API, which allows no
clock skew (`web-token-strict`). `narrow-climb` climbs out of a narrow
token's scope with `..`; `nested-protected` asks for a protected prefix
inside a public one (/public/secret, under `-p topo-multi-owner`);
`token-lists` presents several tokens at once, and in the query, to the
native origins; and `standalone-refusals` checks how a native origin
refuses, and what it tells a client, standalone and federated.

Each namespace has storage of its own, and a test object of its own
bytes (but on a pstore origin, /public reads /protected-a's), so a
server that answers from another namespace's is caught.

What each namespace should allow comes from credentials.expected(): a read
of /public is allowed whatever the token, a write to a namespace without
Writes (/public, unless `-p origin-public-writes` gives it writes) is
refused whatever the token, and so is every request straight to an origin in
a namespace without DirectReads (`-p origin-no-direct`). The test objects
are put in place with stores.seed(), so the last needs no writes. Responses
go to framework/var/data/auth-test/, and a row per request to the results.
"""

import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from functools import cached_property
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

from testlib import accounts, common, credentials, director, jwt, stores, web
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
    EXPIRED_IN_USE: (
        "a token in use as it expires: taken before, refused from 70s past its expiry on"
    ),
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
        "modify-only": "storage.modify alone: it may create, replace, and delete an object",
    }
)

# The checks of one storage scope each: the `pelican token create` flag
# that grants it, and whether it may replace or delete an object (the WLCG
# token profile; storage.modify implies storage.create).
SCOPES = {"create-only": ("write", False), "modify-only": ("modify", True)}

TRAVERSAL = "traversal"
SCENARIOS[TRAVERSAL] = (
    "`..` in a path, however spelled, and a symbolic link in a store, reach no other"
    " namespace, nor beyond an export's storage"
)

# Spellings of a step up a path: each server must keep a request within
# the namespace that its path begins with, however it is spelled.
STEPS = ("..", "%2e%2e", "%2E%2E", ".%2e", "%252e%252e")

HINTS = "hints"
SCENARIOS[HINTS] = (
    "a V2 cache's refusal of a request with no token names the namespace and its issuer"
)

AUDIENCE = "audience"
SCENARIOS[AUDIENCE] = (
    "a token for another audience: every origin refuses it, and stores nothing"
)
# An audience that no server here answers to.
OTHER_AUDIENCE = "https://elsewhere.invalid"

QUERY_TOKEN = "query-token"  # nosec B105
SCENARIOS[QUERY_TOKEN] = (
    "a token in the authz query parameter, bare or after `Bearer `, counts as the header's"
)

TOKEN_LISTS = "token-lists"  # nosec B105
SCENARIOS[TOKEN_LISTS] = (
    "a native origin tries each token of a request, in its header and query, up to 10"
)
AUDIENCE_SPECIFIC = "audience-specific"
SCENARIOS[AUDIENCE_SPECIFIC] = (
    "a token for the first origin's audience is taken there alone; one that lists `any` too,"
    " everywhere"
)
TIME_CLAIMS = "time-claims"
SCENARIOS[TIME_CLAIMS] = (
    "nbf or iat ahead by more than 60s of clock skew is refused, by less is not; exp past is"
    " refused"
)
MALFORMED = "malformed-claims"
SCENARIOS[MALFORMED] = (
    "no exp, kid, or aud, an unknown kid, or a signature by none or HS256: refused"
)
SCITOKENS = "scitokens-profile"
SCENARIOS[SCITOKENS] = "SciTokens 2.0 scopes (read:/, write:/): what each server does with them"
STANDALONE = "standalone-refusals"
SCENARIOS[STANDALONE] = (
    "a native origin refuses with 401 or 403 and token hints standalone, with 401 and none"
    " federated"
)
WEB_TOKEN_STRICT = "web-token-strict"  # nosec B105
SCENARIOS[WEB_TOKEN_STRICT] = (
    "a server's web API refuses its own token 20s before its nbf or 5s past its exp"
)
NARROW_CLIMB = "narrow-climb"
SCENARIOS[NARROW_CLIMB] = (
    "a token for one collection reaches nothing beside it with `..`, however spelled"
)
NESTED = "nested-protected"
SCENARIOS[NESTED] = (
    "a protected prefix inside a public one (/public/secret) is served to no one without a"
    " token"
)

# The checks, beyond the credentials, that ask for /protected-a's test
# object or write beside it, so that a shape that doesn't export it skips
# them.
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
    SCITOKENS,
    STANDALONE,
    NARROW_CLIMB,
)


class Test:
    """The test object, the setup tokens, and the rows recorded.

    Attributes:
        setup_tokens: A `server` token for each exported namespace, by
            namespace.
        object: The test object's path in each namespace.
        object_bytes: The test object's bytes in each namespace.
        sibling: For `narrow`: a name that the object's is a prefix of.
    """

    def __init__(self, session: Session, report: Report):
        self.session = session
        self.report = report
        self.exports = session.fed.exports
        self.setup_tokens = session.tokens()
        self.run = session.run
        self.object = f"data/auth/{self.run}-object"
        # On a pstore origin, /public's bytes are /protected-a's, whose
        # storage it reads.
        own = {ns: os.urandom(65536) for ns in session.namespaces}
        self.object_bytes = {ns: own[session.fed.storage_namespace(ns)] for ns in own}
        self.sibling = f"{self.object}-sibling"
        self.sibling_bytes = os.urandom(65536)

    def token(self, cred: Credential, namespace: str, op: str) -> Optional[str]:
        """Return cred's token for op (`get` or `put`) in namespace.

        Returns:
            The token, or None for `none`, which has no token.
        """
        return self.session.credential_token(cred, namespace, op)

    @cached_property
    def header(self) -> dict[str, Any]:
        """The header of the `server` token for /protected-a.

        It is as `pelican token create` wrote it: its alg, and the kid of
        the origins' key in their issuer's JWKS. raw_token() signs under
        it.
        """
        return jwt.decode(self.setup_tokens["protected-a"])[0]

    def caps(self, namespace: str) -> AbstractSet[str]:
        """Return namespace's capabilities."""
        return self.exports[namespace]

    def whose(self, body: bytes) -> Optional[str]:
        """Return the namespace whose test object body is, if any."""
        return next((ns for ns, data in self.object_bytes.items() if data == body), None)

    def stored(self, origin: common.Origin, namespace: str, rel: str) -> Optional[str]:
        """Return what origin holds at rel in namespace (see stores.held()).

        Raises:
            stores.Unreadable: The origin could not say what it holds.
        """
        return stores.held(origin, namespace, rel, self.setup_tokens[namespace])

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
        record(
            self.report,
            f"{target:<10} {namespace:<11} {op:<8} {name:<11}",
            f"{target} {namespace} {op} {name}",
            answer,
            why,
            note,
        )

    def check_read(
        self, target: str, url: str, namespace: str, cred: Credential, op: str, direct: bool
    ) -> None:
        """GET or HEAD (op) the test object at url.

        url is an origin's if direct.
        """
        answer = web.request(
            op.upper(),
            f"{url}/{namespace}/{self.object}",
            token=self.token(cred, namespace, "get"),
        )
        save(f"{OUT}/responses/{target}-{namespace}-{op}-{cred.name}", answer)
        data = self.object_bytes[namespace]
        whose = self.whose(answer.body) if op == "get" else None
        length = answer.header("Content-Length")
        why = ""
        if credentials.expected(cred, self.caps(namespace), op, direct) == ALLOW:
            if answer.status != 200:
                why = "expected 200"
            elif op == "get" and answer.body != data:
                why = (
                    f"the body is /{whose}'s object" if whose else "the body is not the object"
                )
            elif op == "head" and length != str(len(data)):
                why = f"Content-Length '{length}', not the object's {len(data)}"
        elif answer.status not in (401, 403):
            why = "expected 401 or 403"
        elif whose:
            why = f"refused, but the body is /{whose}'s object"
        self.row(target, namespace, op, cred.name, answer, why)

    def check_put(
        self, target: str, origin: common.Origin, namespace: str, cred: Credential
    ) -> None:
        """PUT new bytes to origin."""
        name = f"data/auth/{self.run}-{target}-{namespace}-{cred.name}"
        source = os.urandom(4096 + len(name))
        with open(f"{OUT}/sources/{target}-{namespace}-{cred.name}", "wb") as f:
            f.write(source)
        answer = web.request(
            "PUT",
            f"{origin.url}/{namespace}/{name}",
            token=self.token(cred, namespace, "put"),
            upload=source,
        )
        save(f"{OUT}/responses/{target}-{namespace}-put-{cred.name}", answer)
        problems: list[str] = []
        try:
            have = self.stored(origin, namespace, name)
        except stores.Unreadable:
            have, unreadable = None, True
        else:
            unreadable = False
        if credentials.expected(cred, self.caps(namespace), "put", direct=True) == ALLOW:
            if not answer.ok:
                problems.append("expected 2xx")
            elif unreadable:
                problems.append("could not read the object back")
            elif have is None:
                problems.append("the store has no such object")
            elif have != common.digest(source):
                problems.append("the store holds different bytes")
        else:
            if answer.status not in (401, 403):
                problems.append("expected 401 or 403")
            if unreadable:
                problems.append("could not check the store")
            elif have is not None:
                problems.append("the store holds the object")
        self.row(target, namespace, "put", cred.name, answer, "; ".join(problems))

    def check_delete(
        self, target: str, origin: common.Origin, namespace: str, cred: Credential
    ) -> None:
        """DELETE an object uploaded to origin for the purpose.

        If the DELETE should succeed and the origin's storage cannot
        delete, skip the row instead.
        """
        cannot_delete = stores.cannot(self.session.fed, "delete")
        if (
            cannot_delete
            and credentials.expected(cred, self.caps(namespace), "delete", True) == ALLOW
        ):
            heading = f"{target:<10} {namespace:<11} {'delete':<8} {cred.name:<11}"
            record_skip(
                self.report, heading, f"{target} {namespace} delete {cred.name}", cannot_delete
            )
            return
        name = f"data/auth/{self.run}-{target}-{namespace}-{cred.name}-delete"
        data = os.urandom(4096 + len(name))
        upload_ns = origin.storage_namespace(namespace)
        error = stores.seed(
            origin, upload_ns, name, data, self.setup_tokens[upload_ns], self.caps(upload_ns)
        )
        if error:
            self.row(
                target,
                namespace,
                "delete",
                cred.name,
                web.Response(None),
                f"could not put the object to delete in place ({error})",
            )
            return
        answer = web.request(
            "DELETE",
            f"{origin.url}/{namespace}/{name}",
            token=self.token(cred, namespace, "put"),
        )
        save(f"{OUT}/responses/{target}-{namespace}-delete-{cred.name}", answer)
        problems: list[str] = []
        try:
            have = self.stored(origin, namespace, name)
        except stores.Unreadable:
            have, unreadable = None, True
        else:
            unreadable = False
        if credentials.expected(cred, self.caps(namespace), "delete", direct=True) == ALLOW:
            if not answer.ok:
                problems.append("expected 2xx")
            elif unreadable:
                problems.append("could not check the store")
            elif have is not None:
                problems.append("the store still holds the object")
        else:
            if answer.status not in (401, 403):
                problems.append("expected 401 or 403")
            if unreadable:
                problems.append("could not check the store")
            elif have != common.digest(data):
                problems.append("the store no longer holds the object")
        self.row(target, namespace, "delete", cred.name, answer, "; ".join(problems))


# --------------------------------------------------------------------------
# The keys check: what each JWKS should hold is in credentials.py
# (server_jwks_problems and namespace_jwks_problems).


def file_key_ids(path: str) -> frozenset[str]:
    """Return the kids in the JWKS file at path."""
    try:
        with open(path, "rb") as f:
            return credentials.key_ids(f.read())
    except FileNotFoundError:
        die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    except ValueError:
        die(f"framework/var/{path} is not a JWKS; remove it and run ./fed.sh keys init")


def get_jwks(url: str, name: str) -> tuple[web.Response, Optional[frozenset[str]], list[str]]:
    """GET the JWKS at url, keeping the response as name.

    Returns:
        The answer, its kids (None if it is not a JWKS), and what is wrong
        with it if it is not.
    """
    answer = web.request("GET", url)
    save(f"{OUT}/responses/{name}", answer)
    if answer.status != 200:
        return answer, None, ["expected 200"]
    try:
        return answer, credentials.key_ids(answer.body), []
    except ValueError:
        return answer, None, ["the body is not a JWKS"]


def check_keys(fed: common.Federation, report: Report) -> None:
    """Check the keys that the origins publish.

    First the server-wide JWKS, then each exported protected namespace's
    discovery document and JWKS. /public has no issuer, unless it takes
    writes (`-p origin-public-writes`): then its JWKS must hold the
    server-wide keys, and neither protected namespace's own, which would
    otherwise write it.
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
        f"{fed.origin_web_url}/.well-known/issuer.jwks", "keys-server-jwks"
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
        save(f"{OUT}/responses/keys-{ns}-discovery", answer)
        why = ""
        if answer.status != 200:
            why = "expected 200"
        else:
            try:
                have = json.loads(answer.body).get("jwks_uri")
            except (ValueError, AttributeError):
                why = "the body is not a discovery document"
            else:
                if have != jwks_uri:
                    why = f"jwks_uri is {have}, not {jwks_uri}"
        record(report, f"{ns:<11} {'discovery':<10}", f"{KEYS} {ns} discovery", answer, why)

        answer, kids, problems = get_jwks(jwks_uri, f"keys-{ns}-jwks")
        if kids is not None:
            problems = credentials.namespace_jwks_problems(ns, kids, server, own)
        record(
            report, f"{ns:<11} {'jwks':<10}", f"{KEYS} {ns} jwks", answer, "; ".join(problems)
        )

    if "public" in fed.exports and credentials.has_issuer(fed.exports["public"]):
        jwks_uri = f"{fed.issuer_of('public')}/.well-known/issuer.jwks"
        answer, kids, problems = get_jwks(jwks_uri, "keys-public-jwks")
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
        Each one's name in the rows, its data URL, and whether it is an
        origin.
    """
    return [(label(o.svc, "origin"), o.url, True) for o in fed.origins] + [
        (label(c.svc, "cache"), c.url, False) for c in fed.caches
    ]


def check_narrow(test: Test, fed: common.Federation, token: str) -> None:
    """GET the test object with token, scoped to it alone, then its sibling.

    The sibling's name begins with the object's: the token's scope is a
    path, not a prefix of names. An origin in a namespace without
    DirectReads refuses both.
    """
    caps = fed.exports["protected-a"]
    for target, url, direct in targets(fed):
        allowed = credentials.decide(ALLOW, caps, "get", direct) == ALLOW
        for name, rel, data in (
            ("narrow-in", test.object, test.object_bytes["protected-a"]),
            ("narrow-out", test.sibling, test.sibling_bytes),
        ):
            answer = web.request("GET", f"{url}/protected-a/{rel}", token=token)
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
            why = ""
            if name == "narrow-in" and not allowed:
                if answer.status not in (401, 403):
                    why = "expected 401 or 403: the origin takes no direct clients"
                elif answer.body == data:
                    why = "refused, but the body is the object"
            elif name == "narrow-in":
                if answer.status != 200:
                    why = "expected 200"
                elif answer.body != data:
                    why = "the body is not the object"
            elif answer.status not in (401, 403):
                why = "expected 401 or 403: the token is for the test object alone"
            elif answer.body == data:
                why = "refused, but the body is the sibling"
            test.row(target, "protected-a", "get", name, answer, why)


# --------------------------------------------------------------------------
# The checks of one storage scope: create-only and modify-only, in
# /protected-a, straight to each origin.


def check_scope(test: Test, fed: common.Federation, name: str) -> None:
    """Present a token of the storage scope of check name at every origin.

    name is one of SCOPES. With the token, PUT a new object, PUT over an
    object, and DELETE one, each object new for the purpose (see
    scope_write()).
    """
    flag, replaces = SCOPES[name]
    token = credentials.mint_server(
        fed, test.session.path(f"tokens/{name}"), "protected-a", scopes=(flag,)
    )
    # The namespace must take writes from direct clients for any to pass.
    writes = credentials.decide(ALLOW, fed.exports["protected-a"], "put", direct=True) == ALLOW
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
                heading = f"{target:<10} {'protected-a':<11} {op:<8} {name:<11}"
                record_skip(
                    test.report, heading, f"{target} protected-a {op} {name}", cannot_delete
                )
                continue
            rel = f"data/auth/{test.run}-{target}-{name}-{op}"
            answer, why = scope_write(test, origin, rel, method, token, existing, allowed)
            test.row(target, "protected-a", op, name, answer, why)


def scope_write(
    test: Test,
    origin: common.Origin,
    rel: str,
    method: str,
    token: str,
    existing: bool,
    allowed: bool,
) -> tuple[web.Response, str]:
    """Send method for rel in /protected-a straight to origin with token.

    method is a PUT, of new bytes, or a DELETE. If existing, put an
    object there first. The request must succeed if allowed, and leave
    the new bytes, or for a DELETE nothing; otherwise it must be refused,
    and leave what was there.

    Returns:
        The answer, and what is wrong with it.
    """
    old = os.urandom(4096) if existing else None
    if old is not None:
        error = stores.seed(
            origin,
            "protected-a",
            rel,
            old,
            test.setup_tokens["protected-a"],
            test.caps("protected-a"),
        )
        if error:
            return web.Response(None), f"could not put the object in place ({error})"
    new = os.urandom(4096) if method == "PUT" else None
    answer = web.request(
        method, stores.url(origin, "protected-a", rel), token=token, upload=new
    )
    want = new if allowed else old
    return answer, scope_problem(test, origin, rel, answer, allowed, want)


def scope_problem(
    test: Test,
    origin: common.Origin,
    rel: str,
    answer: web.Response,
    allowed: bool,
    want: Optional[bytes],
) -> str:
    """Return what is wrong with how origin answered a write of rel.

    The write, in /protected-a, must have succeeded if allowed, and
    otherwise been refused; either way, the store must then hold want
    (None: nothing).
    """
    problems: list[str] = []
    if allowed and not answer.ok:
        problems.append("expected 2xx")
    elif not allowed and answer.status not in (401, 403):
        problems.append("expected 401 or 403")
    try:
        have = test.stored(origin, "protected-a", rel)
    except stores.Unreadable:
        problems.append("could not check the store")
    else:
        if want is None and have is not None:
            problems.append("the store holds the object")
        elif want is not None and have is None:
            problems.append("the store has no such object")
        elif want is not None and have != common.digest(want):
            problems.append("the store holds other bytes")
    return "; ".join(problems)


# --------------------------------------------------------------------------
# The traversal check.


def climbing(start: str, levels: int, rest: str) -> list[str]:
    """Return paths that climb out of namespace start, then go down rest.

    They step up levels times, one in each spelling of STEPS, and one
    more in steps that end in an encoded slash.
    """
    paths = [f"/{start}/" + "/".join([step] * levels) + f"/{rest}" for step in STEPS]
    paths.append(f"/{start}/" + "..%2f" * levels + rest)
    return paths


# A climb: a path, the token it presents, and the bytes it must not reach.
Climb = tuple[str, Optional[str], frozenset[bytes]]


def signing_keys(fed: common.Federation) -> dict[str, bytes]:
    """Return each origin's signing key, by its file name."""
    keys: dict[str, bytes] = {}
    for origin in fed.origins:
        path = common.signing_key(f"issuer-keys/{origin.svc}")
        with open(path, "rb") as f:
            keys[os.path.basename(path)] = f.read()
    return keys


def traversal_climbs(
    test: Test, fed: common.Federation, sentinel: str, sentinels: frozenset[bytes]
) -> dict[str, list[Climb]]:
    """Return the traversal check's climbs, by what they climb toward.

    They climb from each exported namespace, with its `server` token
    (none for /public), toward:

      namespace   another protected namespace's test object, where its
                  storage is not the start's own (unlike a pstore's
                  /public and /protected-a)
      store       sentinel, a file in each origin's store beside the
                  exports' storage (holding one of sentinels)
      filesystem  the origin's signing key, beside its store, in
                  /fed/issuer-keys, where an origin keeps its store in
                  its own filesystem (at /data)

    The symlink climbs are symlink_climbs()'s.
    """
    tokens = {
        ns: None if ns == "public" else test.setup_tokens[ns] for ns in test.session.namespaces
    }
    keys = signing_keys(fed)
    climbs: dict[str, list[Climb]] = {"namespace": [], "store": [], "filesystem": []}
    for start, token in tokens.items():
        for target in (ns for ns in PROTECTED if ns in fed.exports):
            if fed.storage_namespace(target) == fed.storage_namespace(start):
                continue
            forbidden = frozenset({test.object_bytes[target]})
            climbs["namespace"] += [
                (path, token, forbidden)
                for path in climbing(start, 1, f"{target}/{test.object}")
            ]
        climbs["store"] += [(path, token, sentinels) for path in climbing(start, 1, sentinel)]
        for name, key in keys.items():
            climbs["filesystem"] += [
                (path, token, frozenset({key}))
                for path in climbing(start, 2, f"fed/issuer-keys/{name}")
            ]
    return climbs


def symlink_climbs(
    test: Test, fed: common.Federation, sentinel: str, sentinels: frozenset[bytes]
) -> tuple[list[Climb], list[str]]:
    """Make the traversal check's climbs that are symbolic links.

    The links are made in each store on disk, in the first exported
    protected namespace's storage, under data/auth/: one to sentinel,
    beside the exports' storage, by a relative path, and one to each
    origin's signing key, by the absolute path its container sees. A
    backend that follows links would serve what they point to (Pelican's
    posixv2 refuses one that leaves the export; XRootD's posix follows
    it).

    Returns:
        The climbs, each with the namespace's `server` token, and the
        links made.
    """
    start = next((ns for ns in PROTECTED if ns in test.session.namespaces), None)
    disk = [o for o in stores.unique(fed.origins) if not o.pstore]
    if start is None or not disk:
        return [], []
    # Each link: its name, its target, and what it must not reach.
    links = [("store", f"../../../{sentinel}", sentinels)]
    links += [
        (f"key-{name}", f"/fed/issuer-keys/{name}", frozenset({key}))
        for name, key in signing_keys(fed).items()
    ]
    made: list[str] = []
    for origin in disk:
        for name, target, _ in links:
            path = f"{origin.store_of(start)}/data/auth/{test.run}-symlink-{name}"
            os.symlink(target, path)
            made.append(path)
    token = test.setup_tokens[start]
    climbs: list[Climb] = [
        (f"/{start}/data/auth/{test.run}-symlink-{name}", token, forbidden)
        for name, _, forbidden in links
    ]
    return climbs, made


def check_traversal(test: Test, fed: common.Federation) -> None:
    """Send each climb of the traversal check to every origin and cache.

    The climbs are traversal_climbs()'s and symlink_climbs()'s, each sent
    as is (http.client normalizes nothing). Each must not come back with
    the bytes it climbs toward. A row per server and kind of climb.
    """
    sentinel = f"traversal-{test.run}"
    written: dict[str, bytes] = {}
    links: list[str] = []
    try:
        for origin in stores.unique(fed.origins):
            data = os.urandom(4096)
            error = stores.write_file(origin.store, sentinel, data)
            if error:
                die(f"could not write the traversal check's file: {error}")
            written[f"{origin.store}/{sentinel}"] = data
        sentinels = frozenset(written.values())
        climbs = traversal_climbs(test, fed, sentinel, sentinels)
        climbs["symlink"], links = symlink_climbs(test, fed, sentinel, sentinels)
        for target, url, _ in targets(fed):
            for kind, kind_climbs in climbs.items():
                leaks: list[str] = []
                for path, token, forbidden in kind_climbs:
                    answer = web.request("GET", f"{url}{path}", token=token)
                    if answer.body in forbidden:
                        leaks.append(
                            f"GET {path} got {answer.describe()} with what it climbs toward"
                        )
                heading = f"{target:<10} {kind:<11} {'get':<8} {TRAVERSAL:<11}"
                case = f"{TRAVERSAL} {target} {kind}"
                if not kind_climbs:
                    record_skip(
                        test.report, heading, case, "nothing to climb toward in this shape"
                    )
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


def check_hints(test: Test, fed: common.Federation) -> None:
    """GET the test object at each V2 cache in each protected namespace.

    Send no token. The refusal must carry what the director's redirect
    does: X-Pelican-Namespace, naming the namespace and that it takes a
    token, and X-Pelican-Authorization, naming its issuer (see
    director.hints()). A client that named the cache itself
    (Client.PreferredCaches) has heard neither from a director, and has
    nowhere else to learn where to get a token (Pelican's
    local_cache/persistent_cache_api.go, on its main). An XRootD cache
    sends none, and is passed over.
    """
    for cache in fed.caches:
        target = label(cache.svc, "cache")
        for namespace in (ns for ns in PROTECTED if ns in fed.exports):
            heading = f"{target:<10} {namespace:<11} {'get':<8} {HINTS:<11}"
            case = f"{target} {namespace} get {HINTS}"
            if cache.kind != "v2":
                record_skip(test.report, heading, case, "an XRootD cache sends no hints")
                continue
            answer = web.request("GET", f"{cache.url}/{namespace}/{test.object}")
            save(f"{OUT}/responses/{target}-{namespace}-get-{HINTS}", answer)
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
                problems.append(
                    f"X-Pelican-Authorization names {list(found.issuers)}, not {issuer}"
                )
            record(test.report, heading, case, answer, "; ".join(problems))


# --------------------------------------------------------------------------
# The audience check: a resource server must check a token's audience
# against its own (the WLCG token profile). Pelican's origins accept their
# own URL and the profiles' wildcards, which every other token here names
# (origin_serve/authz.go, and the audience in the scitokens configuration
# of an XRootD origin). Only the origins are asked: a cache serves tokens
# minted for any origin, so what it should make of an audience is not
# settled.


def check_audience(test: Test, fed: common.Federation) -> None:
    """GET the test object, and PUT a new one, at every origin.

    Each request is in /protected-a, with a token like `server`'s but for
    OTHER_AUDIENCE: each must be refused, and the PUT store nothing.
    Where the origins take no direct clients, they refuse everything
    direct anyway, so the rows are skipped.
    """
    caps = fed.exports["protected-a"]
    token = credentials.mint_server(
        fed, test.session.path("tokens/audience"), "protected-a", audience=OTHER_AUDIENCE
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for op in ("get", "put"):
            heading = f"{target:<10} {'protected-a':<11} {op:<8} {'wrong-aud':<11}"
            case = f"{target} protected-a {op} wrong-aud"
            if "DirectReads" not in caps:
                record_skip(test.report, heading, case, "the origin takes no direct clients")
                continue
            if op == "get":
                answer = web.request(
                    "GET", stores.url(origin, "protected-a", test.object), token=token
                )
                why = ""
                if answer.status not in (401, 403):
                    why = "expected 401 or 403: the token is for another audience"
                elif answer.body == test.object_bytes["protected-a"]:
                    why = "refused, but the body is the object"
            else:
                rel = f"data/auth/{test.run}-{target}-wrong-aud"
                answer, why = scope_write(test, origin, rel, "PUT", token, False, False)
            test.row(target, "protected-a", op, "wrong-aud", answer, why)


# --------------------------------------------------------------------------
# The query-token check: XRootD caches forward a client's token to an
# origin as ?authz=Bearer%20<token> (and older ones bare), and a director's
# redirect carries it the same way, so every server must take a token in
# the authz query parameter, with or without the `Bearer ` prefix, as it
# takes one in the Authorization header (Pelican's origin_serve/handlers.go
# and local_cache/persistent_cache_api.go; XRootD's XrdSciTokensAccess).

QUERY_FORMS = {"bare": "{}", "bearer": "Bearer {}"}


def check_query_token(test: Test, fed: common.Federation) -> None:
    """GET the test object at every server, with a token in the authz query.

    The tokens are the `server` one, then the `unknown` one, in
    /protected-a, in each of QUERY_FORMS: the first must get the object,
    and the second be refused. An origin that takes no direct clients
    must refuse both.
    """
    caps = fed.exports["protected-a"]
    creds = {name: credentials.BY_NAME[name] for name in ("server", "unknown")}
    for target, url, direct in targets(fed):
        for form, pattern in QUERY_FORMS.items():
            for name, cred in creds.items():
                token = test.token(cred, "protected-a", "get") or ""
                query = urlencode({"authz": pattern.format(token)})
                answer = web.request("GET", f"{url}/protected-a/{test.object}?{query}")
                save(f"{OUT}/responses/{target}-protected-a-get-authz-{form}-{name}", answer)
                allowed = credentials.expected(cred, caps, "get", direct) == ALLOW
                why = ""
                if allowed and answer.status != 200:
                    why = "expected 200: the token in the query is the server's"
                elif allowed and answer.body != test.object_bytes["protected-a"]:
                    why = "the body is not the object"
                elif not allowed and answer.status not in (401, 403):
                    why = "expected 401 or 403"
                elif not allowed and answer.body == test.object_bytes["protected-a"]:
                    why = "refused, but the body is the object"
                test.row(target, "protected-a", "get", f"authz-{form}-{name}", answer, why)


# --------------------------------------------------------------------------
# Tokens that `pelican token create` cannot make (testlib/jwt.py), and
# what a server should make of a read.


def raw_token(
    test: Test,
    times: Optional[Mapping[str, int]] = None,
    drop: tuple[str, ...] = (),
    extra: Optional[Mapping[str, Any]] = None,
    head: Optional[Mapping[str, Any]] = None,
    secret: Optional[bytes] = None,
) -> str:
    """Return a token like `server`'s for /protected-a, but altered.

    Its claims are as credentials.server_claims() has them, minted now.
    It is signed with the origins' key, as its header's alg says: HS256
    with secret, and `none` not at all.

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
    fed = test.session.fed
    claims = credentials.server_claims(fed, "protected-a")
    now = int(time.time())
    for name, offset in (times or {}).items():
        claims[name] = now + offset
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(
        test.header if head is None else head, claims, key=fed.origin_key, secret=secret
    )


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


def write_either(
    test: Test, origin: common.Origin, rel: str, token: str
) -> tuple[web.Response, str, str]:
    """PUT new bytes to rel in /protected-a straight to origin with token.

    The origin may take the token or refuse it.

    Returns:
        The answer; `taken` if it stored them, `refused` (401 or 403) if
        it stored nothing, or "" (neither); and what is wrong if it did
        neither.
    """
    new = os.urandom(4096)
    answer = web.request("PUT", stores.url(origin, "protected-a", rel), token=token, upload=new)
    if answer.ok:
        return answer, "taken", scope_problem(test, origin, rel, answer, True, new)
    if answer.status in (401, 403):
        return answer, "refused", scope_problem(test, origin, rel, answer, False, None)
    return answer, "", "expected 2xx, or 401 or 403"


# --------------------------------------------------------------------------
# The expired-in-use check: Pelican's native origin and V2 cache remember
# what a token authorizes for 5 minutes, check its expiry only when they
# first see it, and keep it another 5 minutes each time it is used
# (origin_serve/authz.go and local_cache/cache_authz.go: a ttlcache, whose
# Get extends an entry's life), so a token in use may never expire there.

# How long the check's token lasts, and when, in seconds after it is
# minted, each server is asked with it: every 30 seconds for three
# minutes.
IN_USE_LIFETIME = 30
IN_USE_TIMES = tuple(range(0, 181, 30))
# How long past its expiry a server may still take a token: native servers
# allow 60 seconds of clock skew.
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

    def __init__(self, test: Test, fed: common.Federation):
        self.test = test
        caps = fed.exports["protected-a"]
        # An origin that takes no direct clients refuses everything
        # direct.
        self.targets = [
            (target, url, credentials.decide(ALLOW, caps, "get", direct) == ALLOW)
            for target, url, direct in targets(fed)
        ]
        claims = credentials.server_claims(fed, "protected-a", lifetime=IN_USE_LIFETIME)
        self.minted, self.expiry = int(claims["iat"]), int(claims["exp"])
        self.token = jwt.encode(test.header, claims, key=fed.origin_key)
        self.answers: dict[str, list[tuple[int, web.Response]]] = {
            target: [] for target, _, _ in self.targets
        }
        self.thread = threading.Thread(target=self.ask, daemon=True)
        self.thread.start()

    def ask(self) -> None:
        """GET the test object at each server where a token decides.

        Ask at each of IN_USE_TIMES.
        """
        for offset in IN_USE_TIMES:
            delay = self.minted + offset - time.time()
            if delay > 0:
                time.sleep(delay)
            for target, url, decides in self.targets:
                if decides:
                    past = int(time.time()) - self.expiry
                    answer = web.request(
                        "GET", f"{url}/protected-a/{self.test.object}", token=self.token
                    )
                    self.answers[target].append((past, answer))

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
                heading = f"{target:<10} {'protected-a':<11} {'get':<8} {EXPIRED_IN_USE:<11}"
                case = f"{target} protected-a get {EXPIRED_IN_USE}"
                record_skip(
                    self.test.report, heading, case, "the origin takes no direct clients"
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
# The time-claims check: native servers allow 60 seconds of clock skew on
# each of a token's times (ClockSkewLeeway in Pelican's
# token/token_verify.go, as the WLCG token profile recommends), and no
# more.

# Each token: its row's name, its times (see raw_token()), and whether a
# server must take it where a token decides.
TIME_CASES: tuple[tuple[str, dict[str, int], bool], ...] = (
    ("nbf+300", {"nbf": 300}, False),
    ("iat+300", {"iat": 300}, False),
    ("nbf+20", {"nbf": 20}, True),
    ("exp-120", {"iat": -600, "nbf": -600, "exp": -120}, False),
)


def check_time_claims(test: Test, fed: common.Federation) -> None:
    """GET the test object with each of TIME_CASES's tokens at every server.

    The requests are in /protected-a, each with a token minted afresh,
    since a V2 cache remembers a token it refused for 30 seconds.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    for target, url, direct in targets(fed):
        for name, times, takes in TIME_CASES:
            answer = web.request(
                "GET", f"{url}/protected-a/{test.object}", token=raw_token(test, times=times)
            )
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
            allowed = credentials.decide(ALLOW if takes else DENY, caps, "get", direct) == ALLOW
            test.row(
                target, "protected-a", "get", name, answer, read_problem(answer, allowed, data)
            )


# --------------------------------------------------------------------------
# The malformed-claims check. The WLCG token profile requires `exp` and
# `aud`. Pelican's native servers verify a token with jwx
# (token/token_verify.go), which checks `exp` only if a token has one, and
# refuses a token without a `kid`, or of one their keys lack, and checks
# its signature by the alg of the key, not the token; a native origin
# checks `aud` only if a token has one (origin_serve/authz.go), and a V2
# cache not at all (local_cache/cache_authz.go).


def malformed_tokens(
    test: Test, fed: common.Federation
) -> list[tuple[str, str, Optional[bool]]]:
    """Return the malformed-claims check's tokens.

    Each is like `server`'s for /protected-a but for one thing.

    Returns:
        For each, its row's name, the token, and whether a server must
        take it where a token decides (None: it may or may not, as for a
        `wlcg.ver` the profile does not define).
    """
    head = test.header
    no_kid = {k: v for k, v in head.items() if k != "kid"}
    # The origins' public key, as a JWK: what an attacker would key HS256
    # with, hoping that a server verifies with its key's bytes whatever
    # the token's alg.
    secret = json.dumps(
        {**jwt.public_jwk(fed.origin_key), "kid": head.get("kid")}, separators=(",", ":")
    ).encode()
    return [
        ("no-exp", raw_token(test, drop=("exp",)), False),
        ("no-kid", raw_token(test, head=no_kid), False),
        ("unknown-kid", raw_token(test, head={**head, "kid": "pelican-test-unknown"}), False),
        ("alg-none", raw_token(test, head={**head, "alg": jwt.NONE}), False),
        ("hs256", raw_token(test, head={**head, "alg": jwt.HS256}, secret=secret), False),
        ("wlcg.ver-2", raw_token(test, extra={"wlcg.ver": "2.0"}), None),
        ("no-aud", raw_token(test, drop=("aud",)), False),
    ]


def check_malformed(test: Test, fed: common.Federation) -> None:
    """GET the test object at every server with each of malformed_tokens().

    The requests are in /protected-a. The note of a token that may be
    taken or refused says which.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    tokens = malformed_tokens(test, fed)
    for target, url, direct in targets(fed):
        for name, token, takes in tokens:
            answer = web.request("GET", f"{url}/protected-a/{test.object}", token=token)
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
            note = ""
            if takes is None and credentials.decide(ALLOW, caps, "get", direct) == ALLOW:
                note, why = read_either(answer, data)
            else:
                allowed = credentials.decide(ALLOW if takes else DENY, caps, "get", direct)
                why = read_problem(answer, allowed == ALLOW, data)
            test.row(target, "protected-a", "get", name, answer, why, note)


# --------------------------------------------------------------------------
# The audience-specific check: an origin takes a token for its own
# audience, Origin.TokenAudience, whose default is Origin.Url, and the
# profiles' wildcards (origin_serve/authz.go, and the audience in an XRootD
# origin's scitokens configuration); and a token is for every audience it
# lists.


def audience_of(origin: common.Origin) -> str:
    """Return the audience that origin takes as its own (Origin.Url).

    It is the scheme, host, and port of its data URL (for an XRootD
    origin, XRootD's).
    """
    parts = urlsplit(origin.url)
    return f"{parts.scheme}://{parts.netloc}"


def check_audience_specific(test: Test, fed: common.Federation) -> None:
    """GET the test object at every origin with tokens that list audiences.

    In /protected-a, the tokens are like `server`'s but for the first
    origin's audience alone, which only it may take; and for the WLCG
    "any" audience and OTHER_AUDIENCE, which every origin must take. Only
    the origins are asked, as in check_audience(); and where they take no
    direct clients, they refuse everything direct anyway, so the rows are
    skipped.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    first = fed.origins[0]
    own = audience_of(first)
    # Each token: its row's name, the token, and the one origin that may
    # take it (None: every origin).
    tokens = (
        (f"aud-{label(first.svc, 'origin')}", raw_token(test, extra={"aud": [own]}), first),
        ("aud-list", raw_token(test, extra={"aud": [jwt.ANY_AUDIENCE, OTHER_AUDIENCE]}), None),
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, token, only in tokens:
            if "DirectReads" not in caps:
                heading = f"{target:<10} {'protected-a':<11} {'get':<8} {name:<11}"
                case = f"{target} protected-a get {name}"
                record_skip(test.report, heading, case, "the origin takes no direct clients")
                continue
            answer = web.request(
                "GET", stores.url(origin, "protected-a", test.object), token=token
            )
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
            allowed = only is None or origin == only
            why = read_problem(answer, allowed, data, f"the token is for {own} alone")
            test.row(target, "protected-a", "get", name, answer, why)


# --------------------------------------------------------------------------
# The scitokens-profile check: a SciTokens 2.0 token's scopes are `read:`
# and `write:`, which XRootD understands; Pelican's native servers compare
# a token's scopes with the WLCG profile's `storage.*` ones
# (token_scopes/token_scope_utils.go), so they may grant it nothing.
# Whether they should is not settled, so the rows say what each server
# did, and fail only if it did neither, or half.


def check_scitokens(test: Test, fed: common.Federation) -> None:
    """Present a token of the SciTokens profile at every origin and cache.

    The token is like `server`'s, but of that profile
    (`pelican token create --profile scitokens2`, with read:/ and
    write:/). With it, GET the test object in /protected-a at every
    origin and cache, and PUT a new object at every origin. Each may take
    the token or refuse it: but wholly either way, serving the object or
    none of it, and storing the PUT's bytes or nothing. Where the
    namespace would refuse the request whatever the token, it must be
    refused.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    token = credentials.mint_server(
        fed,
        test.session.path("tokens/scitokens"),
        "protected-a",
        scopes=("read", "write"),
        profile="scitokens2",
    )
    for target, url, direct in targets(fed):
        answer = web.request("GET", f"{url}/protected-a/{test.object}", token=token)
        save(f"{OUT}/responses/{target}-protected-a-get-scitokens", answer)
        note = ""
        if credentials.decide(ALLOW, caps, "get", direct) == ALLOW:
            note, why = read_either(answer, data)
        else:
            why = read_problem(answer, False, data)
        test.row(target, "protected-a", "get", "scitokens", answer, why, note)
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        rel = f"data/auth/{test.run}-{target}-scitokens"
        note = ""
        if credentials.decide(ALLOW, caps, "put", direct=True) == ALLOW:
            answer, note, why = write_either(test, origin, rel, token)
        else:
            answer, why = scope_write(test, origin, rel, "PUT", token, False, False)
        test.row(target, "protected-a", "put", "scitokens", answer, why, note)


# --------------------------------------------------------------------------
# The token-lists check: a native origin takes tokens from its
# Authorization header, several separated by commas, each after a bearer
# scheme in any case (RFC 9110, section 11.1), and from the access_token
# (RFC 6750, section 2.3) and authz query parameters, each of which may
# repeat; tries each in turn; and treats a request with more than
# MAX_TOKENS as having none (extractTokens in Pelican's
# origin_serve/handlers.go; its main caps them, v26.0.0-rc.3 does not).
# A V2 cache and XRootD take one token, from the header or authz.

MAX_TOKENS = 10
# What an origin must make of a request whose tokens it treats as none: a
# 401, which says the request had none.
NO_TOKEN = "no-token"  # nosec B105

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


def check_token_lists(test: Test, fed: common.Federation) -> None:
    """Present tokens in each of token_forms() at every native origin.

    Each GETs the test object in /protected-a. An XRootD origin is passed
    over.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    server = test.token(credentials.BY_NAME["server"], "protected-a", "get") or ""
    unknown = test.token(credentials.BY_NAME["unknown"], "protected-a", "get") or ""
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, headers, query, verdict in token_forms(server, unknown):
            if not fed.native_origins:
                heading = f"{target:<10} {'protected-a':<11} {'get':<8} {name:<11}"
                case = f"{target} protected-a get {name}"
                why = f"XRootD serves the origins ({fed.origin_variant}), not Pelican"
                record_skip(test.report, heading, case, why)
                continue
            url = stores.url(origin, "protected-a", test.object)
            if query:
                url += f"?{urlencode(query)}"
            answer = web.request("GET", url, headers=headers)
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
            decided = credentials.decide(ALLOW if verdict == ALLOW else DENY, caps, "get", True)
            why = read_problem(answer, decided == ALLOW, data)
            if not why and verdict == NO_TOKEN and decided == DENY and "DirectReads" in caps:
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
            f"X-Pelican-Token-Generation names {found.generation_issuer or 'no issuer'},"
            + f" not {issuer}"
        )
    return problems


def check_standalone(test: Test, fed: common.Federation) -> None:
    """GET the test object, and a missing one, at every native origin.

    Ask for the test object, in /protected-a, with no token, `unknown`'s,
    and `server`'s, and for the missing one with `server`'s: the first
    must get 401; the second 403 from a standalone origin and 401 from a
    federated one; the third the object; and the last 404. Each answer's
    hints must be as hint_problems() says. An XRootD origin is passed
    over, and so is an origin that takes no direct clients, which refuses
    every request alike.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    issuer = fed.issuer_of("protected-a")
    server = test.token(credentials.BY_NAME["server"], "protected-a", "get")
    unknown = test.token(credentials.BY_NAME["unknown"], "protected-a", "get")
    # Each request: its row's name, what it asks for, its token, and the
    # status it must get.
    cases = (
        ("hdr-none", test.object, None, 401),
        ("hdr-unknown", test.object, unknown, 403 if fed.standalone else 401),
        ("hdr-server", test.object, server, 200),
        ("hdr-missing", f"data/auth/{test.run}-missing", server, 404),
    )
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        for name, rel, token, status in cases:
            heading = f"{target:<10} {'protected-a':<11} {'get':<8} {name:<11}"
            case = f"{target} protected-a get {name}"
            if not fed.native_origins:
                why = f"XRootD serves the origins ({fed.origin_variant}), not Pelican"
                record_skip(test.report, heading, case, why)
                continue
            if "DirectReads" not in caps:
                record_skip(test.report, heading, case, "the origin takes no direct clients")
                continue
            answer = web.request("GET", stores.url(origin, "protected-a", rel), token=token)
            save(f"{OUT}/responses/{target}-protected-a-get-{name}", answer)
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
# The web-token-strict check: a server verifies the tokens of its own web
# API with no clock skew, since it alone mints and checks them
# (VerifyWithKeysetStrict in Pelican's token/token_verify.go, which
# extractUserFromBearerToken in web_ui/authentication.go calls).


def web_servers(fed: common.Federation) -> list[tuple[str, str, str]]:
    """Return every origin's and cache's web API, each once.

    In the `tiny` topology, one server is both.

    Returns:
        For each, its server's name in the rows, its web URL, and the
        directory (under framework/var) of its keys.
    """
    found: dict[str, tuple[str, str]] = {}
    for origin in fed.origins:
        found.setdefault(
            origin.web_url, (label(origin.svc, "origin"), f"issuer-keys/{origin.svc}")
        )
    for cache in fed.caches:
        found.setdefault(cache.web_url, (label(cache.svc, "cache"), f"issuer-keys/{cache.svc}"))
    return [(name, url, keys) for url, (name, keys) in found.items()]


def check_web_token_strict(test: Test, fed: common.Federation) -> None:
    """Ask each server's web API who is logged in (/whoami).

    Ask first with a token that it takes as its admin's
    (credentials.mint_admin()), which it must take; then with the same
    token, but not valid for another 20 seconds, and expired 5 seconds
    ago, each of which it must refuse (401) or take for no one.
    """
    for target, web_url, key_dir in web_servers(fed):
        key = common.signing_key(key_dir)
        issuer = credentials.local_issuer(fed, web_url)
        fresh = credentials.mint_admin(
            fed, test.session.path(f"tokens/web.{target}"), issuer, key
        )
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
            save(f"{OUT}/responses/{target}-whoami-{name}", answer)
            try:
                found = common.as_object(json.loads(answer.body)) or {}
            except ValueError:
                found = {}
            authenticated = found.get("authenticated")
            why = ""
            if takes and (answer.status != 200 or authenticated is not True):
                why = "expected 200, authenticated: the token is the server's own, and fresh"
            elif not takes and answer.status != 401:
                if answer.status != 200 or authenticated is not False:
                    why = "expected 401, or 200 and unauthenticated: the token is not valid now"
            test.row(target, "web-api", "whoami", name, answer, why)


# --------------------------------------------------------------------------
# The narrow-climb check: a token for one collection must reach nothing
# beside it, however a path climbs out of it. Pelican cleans a request's
# path before it authorizes it, and so authorizes a climb as where it
# leads (authMiddleware and hasPathPrefix in origin_serve/).


def climbs_from(scope: str, rest: str) -> list[str]:
    """Return paths that climb out of collection scope, then go down rest.

    scope is in /protected-a, relative to it. The paths begin in scope,
    step up once, and go down rest: climbing()'s, but for %252e%252e,
    which a server decodes once to a name inside scope.
    """
    return [p for p in climbing(f"protected-a/{scope}", 1, rest) if "%252e" not in p]


def check_narrow_climb(test: Test, fed: common.Federation) -> None:
    """With a narrow token, climb out of its collection to its sibling.

    The token is for collection data/auth/<run>-narrow in /protected-a
    alone, and the climbs are climbs_from()'s. GET the test object at
    every server, and at every origin where the namespace takes writes,
    PUT a new object, and DELETE one put there for the purpose. Every
    request must be refused (4xx), without the object, and change
    nothing. Each cache must first serve the test object with the
    server's token, so that it refuses an object it holds.
    """
    caps = fed.exports["protected-a"]
    data = test.object_bytes["protected-a"]
    scope = f"data/auth/{test.run}-narrow"
    token = credentials.mint_server(
        fed, test.session.path("tokens/narrow-climb"), "protected-a", scope_path=f"/{scope}"
    )
    for cache in fed.caches:
        target = label(cache.svc, "cache")
        held = web.request(
            "GET",
            f"{cache.url}/protected-a/{test.object}",
            token=test.setup_tokens["protected-a"],
        )
        if held.status != 200 or held.body != data:
            why = "could not first read the test object through it with the server's token"
            test.row(target, "protected-a", "get", NARROW_CLIMB, held, why)
            continue
        climb(test, target, cache.url, None, "get", scope, token)
    writes = credentials.decide(ALLOW, caps, "put", direct=True) == ALLOW
    cannot_delete = stores.cannot(fed, "delete")
    for origin in fed.origins:
        target = label(origin.svc, "origin")
        climb(test, target, origin.url, origin, "get", scope, token)
        if not writes:
            continue
        climb(test, target, origin.url, origin, "put", scope, token)
        if cannot_delete:
            heading = f"{target:<10} {'protected-a':<11} {'delete':<8} {NARROW_CLIMB:<11}"
            case = f"{target} protected-a delete {NARROW_CLIMB}"
            record_skip(test.report, heading, case, cannot_delete)
        else:
            climb(test, target, origin.url, origin, "delete", scope, token)


def climb(
    test: Test,
    target: str,
    url: str,
    origin: Optional[common.Origin],
    op: str,
    scope: str,
    token: str,
) -> None:
    """Send op (get, put, or delete) by each of climbs_from() scope.

    Send it to the server at url, origin if it is one, with token, a
    token for scope alone; and record a row. A GET climbs to the test
    object; a PUT to a new object; and a DELETE to one put there first.
    """
    data = test.object_bytes["protected-a"]
    setup = test.setup_tokens["protected-a"]
    name = {"get": f"{test.run}-object", "put": f"{test.run}-climbed-{target}"}.get(
        op, f"{test.run}-climbed-delete-{target}"
    )
    rel = f"data/auth/{name}"
    heading = f"{target:<10} {'protected-a':<11} {op:<8} {NARROW_CLIMB:<11}"
    case = f"{target} protected-a {op} {NARROW_CLIMB}"
    old = os.urandom(4096)
    if op == "delete" and origin is not None:
        error = stores.seed(origin, "protected-a", rel, old, setup, test.caps("protected-a"))
        if error:
            why = f"could not put the object to delete in place ({error})"
            print(f"{heading}       FAIL  {why}")
            test.report.add(case, FAIL, why)
            return
    paths = climbs_from(scope, name)
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
    if problems:
        print(f"{heading}       FAIL  {problems[0]}")
        test.report.add(case, FAIL, common.first(problems))
    else:
        print(f"{heading}       PASS  {len(paths)} paths")
        test.report.add(case, PASS, f"{len(paths)} paths")


# --------------------------------------------------------------------------
# The nested-protected check: under `-p topo-multi-owner`, origin-2 exports
# /public/secret, which a token protects, inside the origins' /public,
# which anyone may read (fed_owner_exports in fed.sh). A V2 cache grants
# the read of every public namespace on its whole path, and authorizes a
# path by any grant that encloses it (getAcls and authorize in Pelican's
# local_cache/cache_authz.go), so it may serve /public/secret to anyone.
# An XRootD cache's authfile denies the inner prefix outright
# (constructAuthEntry in xrootd/authorization.go).

# How many times the owner tries to read the object through a cache, and
# the seconds between tries: a cache learns of a namespace from the
# director, every minute or so.
NESTED_TRIES, NESTED_INTERVAL = 8, 15


def nested_export(fed: common.Federation) -> Optional[tuple[common.Owner, common.Export]]:
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


def check_nested(test: Test, fed: common.Federation) -> None:
    """Put an object in the nested export (nested_export()), then GET it.

    Put it at its owner's origin, and have the owner read it through each
    cache, so that each holds it; then GET it with no token, and with one
    that names the prefix's issuer but is signed by test-keys/unknown.pem,
    at each cache, at the owner's origin, and at each director's bare
    path, following its redirect. Each must be refused (401 or 403),
    without the object.
    """
    found = nested_export(fed)
    if found is None:
        why = "there is no protected prefix inside a public one (-p topo-multi-owner)"
        warn(f"skipping {NESTED}: {why}")
        test.report.add(NESTED, SKIP, why, seconds=None)
        return
    owner, export = found
    storage = f"{owner.origin.store_of(export.path)}/data/auth"
    rel = f"data/auth/{test.run}-nested"
    path = f"{export.prefix}/{rel}"
    data = os.urandom(65536)
    owner_token = nested_token(test, export, "owner", owner.key, RWM)
    # Each token presented: its row's name, and the token.
    tokens = (
        ("none", None),
        ("unknown", nested_token(test, export, "unknown", "test-keys/unknown.pem", R)),
    )
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
    test: Test, export: common.Export, name: str, key: str, scopes: tuple[str, ...]
) -> str:
    """Return a token of scopes for the whole of export, naming its issuer.

    It is signed with key (a PEM file under framework/var), and kept as
    name.
    """
    out = test.session.path(f"tokens/nested.{name}")
    credentials.mint(
        test.session.fed,
        out,
        f"{export.prefix}/",
        key,
        export.issuer,
        "/",
        credentials.LONG,
        scopes,
    )
    with open(out, encoding="utf-8") as f:
        return f.read().strip()


def skip(_fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None if there is something.

    There is always something: every shape exports a namespace that a
    token protects.
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


def put_test_objects(test: Test, fed: common.Federation, narrow: bool) -> None:
    """Put each namespace's test object at every origin.

    Put it in each namespace with storage of its own (see
    stores.storage_namespaces()), with the `server` credential; and for
    `narrow`, the object's sibling in /protected-a. Earlier runs' objects
    come out first. The federation suite has already waited for each
    cache to accept the server's token.
    """
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
            token, caps = test.setup_tokens["protected-a"], fed.exports["protected-a"]
            error = stores.seed(
                origin, "protected-a", test.sibling, test.sibling_bytes, token, caps
            )
            if error:
                die(f"could not put the test object's sibling at {origin.svc} ({error})")


def cache_sibling(test: Test, fed: common.Federation) -> None:
    """GET the object's sibling through each cache, with the server's token.

    That way, `narrow-out` asks each cache for an object it holds.
    """
    for cache in fed.caches:
        answer = web.request(
            "GET",
            f"{cache.url}/protected-a/{test.sibling}",
            token=test.setup_tokens["protected-a"],
        )
        why = ""
        if answer.status != 200:
            why = "expected 200: the server's token is good for the sibling"
        elif answer.body != test.sibling_bytes:
            why = "the body is not the sibling"
        test.row(label(cache.svc, "cache"), "protected-a", "get", "narrow-sibling", answer, why)


def check_credential(test: Test, fed: common.Federation, cred: Credential) -> None:
    """Present cred in every namespace it applies to, at every server."""
    if cred.expired:
        test.session.wait_until_stale()
    for namespace in test.session.namespaces:
        if not credentials.applies(cred, namespace):
            continue
        for origin in fed.origins:
            target = label(origin.svc, "origin")
            test.check_read(target, origin.url, namespace, cred, "get", direct=True)
            test.check_read(target, origin.url, namespace, cred, "head", direct=True)
            test.check_put(target, origin, namespace, cred)
            test.check_delete(target, origin, namespace, cred)
        for cache in fed.caches:
            target = label(cache.svc, "cache")
            test.check_read(target, cache.url, namespace, cred, "get", direct=False)
            test.check_read(target, cache.url, namespace, cred, "head", direct=False)


def narrow_token(test: Test, fed: common.Federation) -> str:
    """Return a token like `server`'s, but for the test object alone."""
    path = test.session.path("tokens/narrow")
    return credentials.mint_server(fed, path, "protected-a", scope_path=f"/{test.object}")


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
    if "protected-a" not in fed.exports:
        reason = "this shape does not export /protected-a"
        for name in (n for n in PROTECTED_A_CHECKS if n in wanted):
            warn(f"skipping {name}: {reason}")
            report.add(name, SKIP, reason, seconds=None)
            wanted.discard(name)
    narrow = NARROW in wanted
    scopes = [name for name in SCOPES if name in wanted]
    checks = bool(creds or wanted)
    if checks:
        for origin in fed.origins:
            for namespace in fed.exports:
                directory = f"{origin.store_of(namespace)}/data/auth"
                if not origin.pstore and not os.path.isdir(directory):
                    die(f"framework/var/{directory} is missing; run ./fed.sh init")

    shutil.rmtree(OUT, ignore_errors=True)
    for sub in ("responses", "sources"):
        os.makedirs(f"{OUT}/{sub}")

    if KEYS in selected:
        check_keys(fed, report)
        if checks:
            print()
    if not checks:
        return

    test = Test(session, report)
    for namespace, data in test.object_bytes.items():
        with open(f"{OUT}/object-{namespace}", "wb") as f:
            f.write(data)
    put_test_objects(test, fed, narrow)
    # It takes three minutes, which the other checks fill.
    in_use = InUse(test, fed) if EXPIRED_IN_USE in wanted else None

    print(f"\n{'target':<10} {'ns':<11} {'op':<8} {'credential':<11} {'code':>4}  result")
    if narrow:
        cache_sibling(test, fed)
    for cred in creds:
        check_credential(test, fed, cred)
    # The other checks, in this order: those of one storage scope after
    # `narrow`, and `traversal` last.
    others: dict[str, Callable[[Test, common.Federation], None]] = {
        HINTS: check_hints,
        QUERY_TOKEN: check_query_token,
        AUDIENCE: check_audience,
        TOKEN_LISTS: check_token_lists,
        AUDIENCE_SPECIFIC: check_audience_specific,
        TIME_CLAIMS: check_time_claims,
        MALFORMED: check_malformed,
        SCITOKENS: check_scitokens,
        STANDALONE: check_standalone,
        WEB_TOKEN_STRICT: check_web_token_strict,
        NARROW: lambda t, f: check_narrow(t, f, narrow_token(t, f)),
    }
    for name, check in others.items():
        if name in wanted:
            check(test, fed)
    for name in scopes:
        check_scope(test, fed, name)
    for name, check in ((NARROW_CLIMB, check_narrow_climb), (NESTED, check_nested)):
        if name in wanted:
            check(test, fed)
    if TRAVERSAL in wanted:
        check_traversal(test, fed)
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
    print(f"Responses are in framework/var/{OUT}/.")
