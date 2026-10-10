"""Authorization at each server directly: GET, HEAD, PUT, and DELETE to
every origin, and GET and HEAD to every cache, in every namespace, once
for each credential in the table in framework/testlib/credentials.py.
A request that should be allowed must succeed with the right bytes (a
HEAD, with the object's length), or remove the object. Any other must
get 401 or 403 and change nothing. First, the `keys` check fetches each
namespace issuer's discovery document and JWKS, and the origins'
server-wide JWKS, and checks which test keys each publishes. Last, the
`narrow` check presents a token for the test object alone in
/protected-a, at every server, for it and then for its sibling; the
`create-only` and `modify-only` checks present tokens of one storage
scope each at every origin, which may create an object, replace one,
and delete one, as the WLCG token profile says; and the `traversal`
check sends paths that climb out of their namespace with `..`, which
must reach neither another namespace's test object nor a file beyond
an export's storage.

Each namespace has storage of its own, and a test object of its own
bytes (but on a pstore origin, /public reads /protected-a's), so a
server that answers from another namespace's is caught.

What each namespace should allow comes from credentials.expected(): a
read of /public is allowed whatever the token, a write to a namespace
without Writes (/public) is refused whatever the token, and so is every
request straight to an origin in a namespace without DirectReads
(`-p origin-no-direct`). The test objects are put in place with
stores.seed(), so the last needs no writes. The transfers suite
presents the same credentials through the clients, which refuse some
of them themselves. Responses go to framework/var/data/auth-test/, and
a row per request to the results.
"""

import json
import os
import shutil
from collections.abc import Set as AbstractSet
from typing import Optional

from testlib import common, credentials, stores, web
from testlib.common import die, warn
from testlib.credentials import ALLOW, CREDENTIALS, PROTECTED, Credential
from testlib.report import FAIL, PASS, SKIP, Report
from testlib.session import Session

OUT = "data/auth-test"

# The checks that are not credentials.
KEYS = "keys"
NARROW = "narrow"

SCENARIOS = {KEYS: "each issuer's discovery document and JWKS, and the server-wide JWKS"}
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
    "`..` in a path, however spelled, reaches no other namespace, nor beyond an export's storage"
)

# Spellings of a step up a path: each server must keep a request within
# the namespace that its path begins with, however it is spelled.
STEPS = ("..", "%2e%2e", "%2E%2E", ".%2e", "%252e%252e")


class Test:
    """The test object, the setup tokens, and the rows recorded."""

    def __init__(self, session: Session, report: Report):
        self.session = session
        self.report = report
        self.exports = session.fed.exports
        self.setup_tokens = session.tokens()  # by namespace
        self.run = session.run
        self.object = f"data/auth/{self.run}-object"
        # The test object's bytes in each namespace: on a pstore origin,
        # /public's are /protected-a's, whose storage it reads.
        own = {ns: os.urandom(65536) for ns in session.namespaces}
        self.object_bytes = {ns: own[session.fed.storage_namespace(ns)] for ns in own}
        # For `narrow`: a name that the object's is a prefix of.
        self.sibling = f"{self.object}-sibling"
        self.sibling_bytes = os.urandom(65536)

    def token(self, cred: Credential, namespace: str, op: str) -> Optional[str]:
        """cred's token for op (`get` or `put`) in namespace, or None."""
        return self.session.credential_token(cred, namespace, op)

    def caps(self, namespace: str) -> AbstractSet[str]:
        """namespace's capabilities."""
        return self.exports[namespace]

    def whose(self, body: bytes) -> Optional[str]:
        """The namespace whose test object body is, if any."""
        return next((ns for ns, data in self.object_bytes.items() if data == body), None)

    def stored(self, origin: common.Origin, namespace: str, rel: str) -> Optional[str]:
        """What origin holds at rel in namespace (see stores.held())."""
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
        """GET or HEAD (op) the test object at url, an origin's if
        direct."""
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
        """DELETE an object uploaded to origin for the purpose, unless the
        DELETE should succeed and the origin's storage cannot delete."""
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


def record(
    report: Report, heading: str, case: str, answer: web.Response, why: str, note: str = ""
) -> None:
    """Print heading and answer's status as a row, and record case: failed
    because of why, or passed, with note."""
    code = "000" if answer.status is None else str(answer.status)
    line = f"{heading} {code:>4}"
    if why:
        print(f"{line}  FAIL  {why}")
        report.add(case, FAIL, f"HTTP {code}: {why}")
    else:
        print(f"{line}  PASS" + (f"  {note}" if note else ""))
        report.add(case, PASS, f"HTTP {code}" + (f": {note}" if note else ""))


def record_skip(report: Report, heading: str, case: str, why: str) -> None:
    """Print heading as a row, and record case as skipped because of why."""
    print(f"{heading}       SKIP  {why}")
    report.add(case, SKIP, why)


def save(path: str, answer: web.Response) -> None:
    """Keep a response's body (or a HEAD's headers)."""
    with open(path, "wb") as f:
        if answer.body:
            f.write(answer.body)
        else:
            f.write("".join(f"{k}: {v}\n" for k, v in answer.headers).encode())


def label(svc: str, role: str) -> str:
    """What rows call service svc in role (origin or cache). In `tiny`,
    one service is both."""
    return svc if svc.startswith(f"{role}-") else f"{svc}-{role}"


# --------------------------------------------------------------------------
# The keys check: what each JWKS should hold is in credentials.py
# (server_jwks_problems and namespace_jwks_problems).


def file_key_ids(path: str) -> frozenset[str]:
    """The kids in the JWKS file at path."""
    try:
        with open(path, "rb") as f:
            return credentials.key_ids(f.read())
    except FileNotFoundError:
        die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    except ValueError:
        die(f"framework/var/{path} is not a JWKS; remove it and run ./fed.sh keys init")


def get_jwks(url: str, name: str) -> tuple[web.Response, Optional[frozenset[str]], list[str]]:
    """GET the JWKS at url, keeping the response as name. Return the
    answer, its kids (None if it is not a JWKS), and what is wrong with
    it if it is not."""
    answer = web.request("GET", url)
    save(f"{OUT}/responses/{name}", answer)
    if answer.status != 200:
        return answer, None, ["expected 200"]
    try:
        return answer, credentials.key_ids(answer.body), []
    except ValueError:
        return answer, None, ["the body is not a JWKS"]


def check_keys(fed: common.Federation, report: Report) -> None:
    """The server-wide JWKS, then each exported protected namespace's
    discovery document and JWKS. /public has no issuer."""
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


# --------------------------------------------------------------------------
# The narrow check: a token like `server`'s, but for one object, in
# /protected-a (where the token decides) at every server.


def targets(fed: common.Federation) -> list[tuple[str, str, bool]]:
    """Every server's name in the rows, its data URL, and whether it is an
    origin."""
    return [(label(o.svc, "origin"), o.url, True) for o in fed.origins] + [
        (label(c.svc, "cache"), c.url, False) for c in fed.caches
    ]


def check_narrow(test: Test, fed: common.Federation, token: str) -> None:
    """GET the test object with a token scoped to it alone, then its
    sibling, whose name begins with the object's: the token's scope is a
    path, not a prefix of names. An origin in a namespace without
    DirectReads refuses both."""
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
    """Present a token of the storage scope that check name stands for
    (see SCOPES) at every origin: a PUT of a new object, a PUT over an
    object, and a DELETE of one, each object new for the purpose (see
    scope_write())."""
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
    """Send method (a PUT, of new bytes, or a DELETE) for rel in
    /protected-a straight to origin with token, putting an object there
    first if existing: the answer, and what is wrong with it. It must
    succeed if allowed, and leave the new bytes, or for a DELETE nothing;
    otherwise it must be refused, and leave what was there."""
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
    """What is wrong with how origin answered a write of rel in
    /protected-a: it must have succeeded if allowed, and otherwise been
    refused; either way, the store must then hold want (None: nothing)."""
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
    """Paths that begin in namespace start, step up levels times, in each
    spelling of STEPS, then go down rest; and one whose steps end in an
    encoded slash."""
    paths = [f"/{start}/" + "/".join([step] * levels) + f"/{rest}" for step in STEPS]
    paths.append(f"/{start}/" + "..%2f" * levels + rest)
    return paths


# A climb: a path, the token it presents, and the bytes it must not reach.
Climb = tuple[str, Optional[str], frozenset[bytes]]


def traversal_climbs(
    test: Test, fed: common.Federation, sentinel: str, sentinels: frozenset[bytes]
) -> dict[str, list[Climb]]:
    """The climbs of the traversal check, by what they climb toward, from
    each exported namespace, with its `server` token (none for /public):

      namespace   another protected namespace's test object, where its
                  storage is not the start's own (unlike a pstore's
                  /public and /protected-a)
      store       sentinel, a file in each origin's store beside the
                  exports' storage (holding one of sentinels)
      filesystem  the origin's signing key, beside its store, in
                  /fed/issuer-keys, where an origin keeps its store in
                  its own filesystem (at /data)"""
    tokens = {
        ns: None if ns == "public" else test.setup_tokens[ns] for ns in test.session.namespaces
    }
    keys: dict[str, bytes] = {}
    for origin in fed.origins:
        path = common.signing_key(f"issuer-keys/{origin.svc}")
        with open(path, "rb") as f:
            keys[os.path.basename(path)] = f.read()
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


def check_traversal(test: Test, fed: common.Federation) -> None:
    """Send each climb of traversal_climbs() to every origin and cache,
    each as is (http.client normalizes nothing): it must not come back
    with the bytes it climbs toward. A row per server and kind of climb."""
    sentinel = f"traversal-{test.run}"
    written: dict[str, bytes] = {}
    try:
        for origin in stores.unique(fed.origins):
            data = os.urandom(4096)
            error = stores.write_file(origin.store, sentinel, data)
            if error:
                die(f"could not write the traversal check's file: {error}")
            written[f"{origin.store}/{sentinel}"] = data
        climbs = traversal_climbs(test, fed, sentinel, frozenset(written.values()))
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
        for path in written:
            os.remove(path)


def skip(_fed: common.Federation) -> Optional[str]:
    """Never: every shape exports a namespace that a token protects."""
    return None


def runnable(fed: common.Federation, selected: list[str], report: Report) -> list[Credential]:
    """The selected credentials, but for those that can test nothing in
    this shape, which are recorded as skipped. `server` goes first, so
    that each cache holds the test object before anything is refused: a
    stored object must be protected too. `expired` goes last, since it
    must wait out every server's clock skew."""
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
    """Put each namespace's test object at every origin, in each namespace
    with storage of its own (see stores.storage_namespaces()), with the
    `server` credential; and for `narrow`, its sibling in /protected-a.
    Earlier runs' objects come out first. The federation suite has
    already waited for each cache to accept the server's token."""
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
    """GET the test object's sibling through each cache with the server's
    token, so that `narrow-out` asks each cache for an object it holds."""
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
    """A token like `server`'s, but for the test object alone."""
    path = test.session.path("tokens/narrow")
    return credentials.mint_server(fed, path, "protected-a", scope_path=f"/{test.object}")


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Check the keys, then present each credential, then `narrow`, then
    each storage scope alone, then traversal."""
    report = results.report
    fed = session.fed
    creds = runnable(fed, selected, report)
    narrow = NARROW in selected
    scopes = [name for name in SCOPES if name in selected]
    if "protected-a" not in fed.exports:
        reason = "this shape does not export /protected-a"
        for name in ([NARROW] if narrow else []) + scopes:
            warn(f"skipping {name}: {reason}")
            report.add(name, SKIP, reason, seconds=None)
        narrow, scopes = False, []
    traversal = TRAVERSAL in selected
    if creds or narrow or scopes or traversal:
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
        if creds or narrow or scopes or traversal:
            print()
    if not creds and not narrow and not scopes and not traversal:
        return

    test = Test(session, report)
    for namespace, data in test.object_bytes.items():
        with open(f"{OUT}/object-{namespace}", "wb") as f:
            f.write(data)
    put_test_objects(test, fed, narrow)

    print(f"\n{'target':<10} {'ns':<11} {'op':<8} {'credential':<11} {'code':>4}  result")
    if narrow:
        cache_sibling(test, fed)
    for cred in creds:
        check_credential(test, fed, cred)
    if narrow:
        check_narrow(test, fed, narrow_token(test, fed))
    for name in scopes:
        check_scope(test, fed, name)
    if traversal:
        check_traversal(test, fed)

    passed = report.count(PASS)
    skipped = report.count(SKIP)
    ending = f", {skipped} skipped." if skipped else "."
    print(f"\n{passed} passed, {report.failed} failed{ending}")
    print(f"Responses are in framework/var/{OUT}/.")
