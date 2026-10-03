"""Authorization at each server directly: GET, HEAD, PUT, and DELETE to
every origin, and GET and HEAD to every cache, in every namespace, once
for each credential in the table in framework/testlib/credentials.py.
A request that should be allowed must succeed with the right bytes, or
remove the object. Any other must get 401 or 403 and change nothing.
First, the `keys` check fetches each namespace issuer's discovery
document and JWKS, and the origins' server-wide JWKS, and checks which
test keys each publishes. Last, the `narrow` check presents a token for
the test object alone, at every server, for it and then for its
sibling.

What each namespace should allow comes from credentials.expected(): a
read of /public is allowed whatever the token, a write to a namespace
without Writes (/public) is refused whatever the token, and so is every
request straight to an origin in a namespace without DirectReads
(`-p origin-no-direct`). The test objects are put in place with
stores.seed(), so the last needs no writes. The transfers suite presents the same credentials through the clients,
which refuse some of them themselves. Responses go to
framework/var/data/auth-test/, and a row per request to the results.
"""

import json
import os
import shutil
from typing import AbstractSet, FrozenSet, List, Optional, Tuple

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
SCENARIOS.update({c.name: f"a token {c.description} ({c.verdict} where a token decides)"
                  if c.key else f"{c.description} ({c.verdict} where a token decides)"
                  for c in CREDENTIALS})
SCENARIOS[NARROW] = "a token for the test object alone: it may GET the object, not its sibling"


class Test:
    def __init__(self, session: Session, report: Report):
        self.session = session
        self.report = report
        self.exports = session.fed.exports
        self.setup_tokens = session.tokens()  # by namespace
        self.run = session.run
        self.object = f"data/auth/{self.run}-object"
        self.object_bytes = os.urandom(65536)
        # For `narrow`: a name that the object's is a prefix of.
        self.sibling = f"{self.object}-sibling"
        self.sibling_bytes = os.urandom(65536)

    def token(self, cred: Credential, namespace: str, op: str) -> Optional[str]:
        """cred's token for op (`get` or `put`) in namespace, or None."""
        return self.session.credential_token(cred, namespace, op)

    def caps(self, namespace: str) -> AbstractSet[str]:
        return self.exports[namespace]

    def stored(self, origin: common.Origin, namespace: str, rel: str) -> Optional[str]:
        return stores.held(origin, namespace, rel, self.setup_tokens[namespace])

    def row(self, target: str, namespace: str, op: str, name: str,
            answer: web.Response, why: str, note: str = "") -> None:
        record(self.report, f"{target:<10} {namespace:<11} {op:<6} {name:<11}",
               f"{target} {namespace} {op} {name}", answer, why, note)

    def check_read(self, target: str, url: str, namespace: str, cred: Credential,
                   op: str, direct: bool) -> None:
        """GET or HEAD (op) the test object at url, an origin's if
        direct."""
        answer = web.request(op.upper(), f"{url}/{namespace}/{self.object}",
                             token=self.token(cred, namespace, "get"))
        save(f"{OUT}/responses/{target}-{namespace}-{op}-{cred.name}", answer)
        is_object = op == "get" and answer.body == self.object_bytes
        why = ""
        if credentials.expected(cred, self.caps(namespace), op, direct) == ALLOW:
            if answer.status != 200:
                why = "expected 200"
            elif op == "get" and not is_object:
                why = "the body is not the object"
        elif answer.status not in (401, 403):
            why = "expected 401 or 403"
        elif is_object:
            why = "refused, but the body is the object"
        self.row(target, namespace, op, cred.name, answer, why)

    def check_put(self, target: str, origin: common.Origin, namespace: str,
                  cred: Credential) -> None:
        """PUT new bytes to origin."""
        name = f"data/auth/{self.run}-{target}-{namespace}-{cred.name}"
        source = os.urandom(4096 + len(name))
        with open(f"{OUT}/sources/{target}-{namespace}-{cred.name}", "wb") as f:
            f.write(source)
        answer = web.request("PUT", f"{origin.url}/{namespace}/{name}",
                             token=self.token(cred, namespace, "put"), upload=source)
        save(f"{OUT}/responses/{target}-{namespace}-put-{cred.name}", answer)
        problems: List[str] = []
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

    def check_delete(self, target: str, origin: common.Origin, namespace: str,
                     cred: Credential) -> None:
        """DELETE an object uploaded to origin for the purpose."""
        name = f"data/auth/{self.run}-{target}-{namespace}-{cred.name}-delete"
        data = os.urandom(4096 + len(name))
        uploads = stores.upload_namespaces(origin, self.exports)
        upload_ns = namespace if namespace in uploads else uploads[0]
        error = stores.seed(origin, upload_ns, name, data, self.setup_tokens[upload_ns],
                            self.caps(upload_ns))
        if error:
            self.row(target, namespace, "delete", cred.name, web.Response(None),
                     f"could not put the object to delete in place ({error})")
            return
        answer = web.request("DELETE", f"{origin.url}/{namespace}/{name}",
                             token=self.token(cred, namespace, "put"))
        save(f"{OUT}/responses/{target}-{namespace}-delete-{cred.name}", answer)
        problems: List[str] = []
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


def record(report: Report, label: str, case: str, answer: web.Response, why: str,
           note: str = "") -> None:
    """Print label and answer's status as a row, and record case: failed
    because of why, or passed, with note."""
    code = "000" if answer.status is None else str(answer.status)
    line = f"{label} {code:>4}"
    if why:
        print(f"{line}  FAIL  {why}")
        report.add(case, FAIL, f"HTTP {code}: {why}")
    else:
        print(f"{line}  PASS" + (f"  {note}" if note else ""))
        report.add(case, PASS, f"HTTP {code}" + (f": {note}" if note else ""))


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


#---------------------------------------------------------------------------
# The keys check: what each JWKS should hold is in credentials.py
# (server_jwks_problems and namespace_jwks_problems).

def file_key_ids(path: str) -> FrozenSet[str]:
    """The kids in the JWKS file at path."""
    try:
        with open(path, "rb") as f:
            return credentials.key_ids(f.read())
    except FileNotFoundError:
        die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    except ValueError:
        die(f"framework/var/{path} is not a JWKS; remove it and run ./fed.sh keys init")


def get_jwks(url: str, name: str) -> Tuple[web.Response, Optional[FrozenSet[str]], List[str]]:
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
        why = ("every namespace trusts the external issuer" if fed.external_issuer
               else "the origins' issuer is off") + ", so no namespace has keys of its own"
        warn(f"skipping {KEYS}: {why}")
        report.add(KEYS, SKIP, why, seconds=None)
        return
    jwks = file_key_ids(credentials.SERVER_JWKS)
    own = {ns: file_key_ids(credentials.namespace_jwks(ns)) for ns in PROTECTED}

    print(f"Checking the keys that {fed.origin_web_url} publishes ...")
    print(f"\n{'issuer':<11} {'document':<10} {'code':>4}  result")
    answer, server, problems = get_jwks(f"{fed.origin_web_url}/.well-known/issuer.jwks",
                                        "keys-server-jwks")
    if server is not None:
        problems = credentials.server_jwks_problems(server, jwks, own)
    record(report, f"{'server':<11} {'jwks':<10}", f"{KEYS} server jwks", answer,
           "; ".join(problems))

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
        record(report, f"{ns:<11} {'jwks':<10}", f"{KEYS} {ns} jwks", answer,
               "; ".join(problems))


#---------------------------------------------------------------------------
# The narrow check: a token like `server`'s, but for one object, in
# /protected-a (where the token decides) at every server.

def targets(fed: common.Federation) -> List[Tuple[str, str, bool]]:
    """Every server's name in the rows, its data URL, and whether it is an
    origin."""
    return ([(label(o.svc, "origin"), o.url, True) for o in fed.origins]
            + [(label(c.svc, "cache"), c.url, False) for c in fed.caches])


def check_narrow(test: Test, fed: common.Federation, token: str) -> None:
    """GET the test object with a token scoped to it alone, then its
    sibling, whose name begins with the object's: the token's scope is a
    path, not a prefix of names. An origin in a namespace without
    DirectReads refuses both."""
    caps = fed.exports["protected-a"]
    for target, url, direct in targets(fed):
        allowed = credentials.decide(ALLOW, caps, "get", direct) == ALLOW
        for name, rel, data in (("narrow-in", test.object, test.object_bytes),
                                ("narrow-out", test.sibling, test.sibling_bytes)):
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


def skip(fed: common.Federation) -> Optional[str]:
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    report = results.report
    fed = session.fed
    origins, caches = fed.origins, fed.caches
    namespaces = session.namespaces

    # `server` goes first, so that each cache holds the test object before
    # anything is refused: a stored object must be protected too. `expired`
    # goes last, since it must wait out every server's clock skew.
    ordered = [credentials.BY_NAME[n] for n in selected if n in credentials.BY_NAME]
    ordered.sort(key=lambda c: (c.name != "server", c.expired))
    narrow = NARROW in selected

    # Skip the credentials that can test nothing in this shape.
    for cred in list(ordered):
        why = credentials.moot(fed, cred)
        if why:
            warn(f"skipping {cred.name}: {why}")
            report.add(cred.name, SKIP, why, seconds=None)
            ordered.remove(cred)
    for origin in origins:
        if (ordered or narrow) and not origin.pstore \
                and not os.path.isdir(f"{origin.store}/data/auth"):
            die(f"framework/var/{origin.store}/data/auth is missing; run ./fed.sh init")

    shutil.rmtree(OUT, ignore_errors=True)
    for sub in ("responses", "sources"):
        os.makedirs(f"{OUT}/{sub}")

    if KEYS in selected:
        check_keys(fed, report)
        if ordered or narrow:
            print()
    if not ordered and not narrow:
        return

    test = Test(session, report)
    with open(f"{OUT}/object", "wb") as f:
        f.write(test.object_bytes)

    #-----------------------------------------------------------------------
    # The test object: the same bytes at every origin, in every namespace
    # (see stores.upload_namespaces), uploaded with the `server` credential;
    # and for `narrow`, its sibling in /protected-a. Earlier runs' objects
    # come out first. The federation suite has already waited for each
    # cache to accept the server's token.

    for origin in origins:
        if not origin.pstore:
            common.empty_dir(f"{origin.store}/data/auth")

    print("Putting the test object at each origin ...")
    for origin in origins:
        for namespace in stores.upload_namespaces(origin, fed.exports):
            error = stores.seed(origin, namespace, test.object, test.object_bytes,
                                test.setup_tokens[namespace], fed.exports[namespace])
            if error:
                die(f"could not put the test object at {origin.svc} in /{namespace} with the"
                    f" server's own token ({error}); is it up (./fed.sh status)?")
        for namespace in namespaces:
            try:
                intact = test.stored(origin, namespace, test.object) \
                    == common.digest(test.object_bytes)
            except stores.Unreadable:
                intact = False
            if not intact:
                die(f"{origin.svc} did not store the test object intact in /{namespace}")
        if narrow:
            error = stores.seed(origin, "protected-a", test.sibling, test.sibling_bytes,
                                test.setup_tokens["protected-a"], fed.exports["protected-a"])
            if error:
                die(f"could not put the test object's sibling at {origin.svc} ({error})")

    #-----------------------------------------------------------------------
    # The rows.

    print(f"\n{'target':<10} {'ns':<11} {'op':<6} {'credential':<11} {'code':>4}  result")
    if narrow:
        # So that `narrow-out` asks each cache for an object it holds.
        for cache in caches:
            answer = web.request("GET", f"{cache.url}/protected-a/{test.sibling}",
                                 token=test.setup_tokens["protected-a"])
            why = ""
            if answer.status != 200:
                why = "expected 200: the server's token is good for the sibling"
            elif answer.body != test.sibling_bytes:
                why = "the body is not the sibling"
            test.row(label(cache.svc, "cache"), "protected-a", "get", "narrow-sibling",
                     answer, why)

    for cred in ordered:
        if cred.expired:
            session.wait_until_stale()
        for namespace in namespaces:
            if not credentials.applies(cred, namespace):
                continue
            for origin in origins:
                target = label(origin.svc, "origin")
                test.check_read(target, origin.url, namespace, cred, "get", direct=True)
                test.check_read(target, origin.url, namespace, cred, "head", direct=True)
                test.check_put(target, origin, namespace, cred)
                test.check_delete(target, origin, namespace, cred)
            for cache in caches:
                target = label(cache.svc, "cache")
                test.check_read(target, cache.url, namespace, cred, "get", direct=False)
                test.check_read(target, cache.url, namespace, cred, "head", direct=False)
    if narrow:
        path = session.path("tokens/narrow")
        credentials.mint(fed, path, "/protected-a/", fed.origin_key,
                         fed.issuer_of("protected-a"), f"/{test.object}", credentials.LONG,
                         credentials.RWM)
        with open(path) as f:
            check_narrow(test, fed, f.read().strip())

    passed = report.count(PASS)
    skipped = report.count(SKIP)
    print(f"\n{passed} passed, {report.failed} failed"
          + (f", {skipped} skipped." if skipped else "."))
    print(f"Responses are in framework/var/{OUT}/.")
