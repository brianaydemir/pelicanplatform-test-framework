"""Another owner beside the origins (`-p topo-multi-owner`): origin-2,
with a key and an issuer of its own, exporting /other (like /protected-a)
and /public/other (like /public, and nested in it), as far as the shape
exports those (see framework/testlib/owners.py).

  ready       origin-2's objects come back through the federation, and
              from each cache: 20 tries, 15 seconds apart, while the
              servers learn of it
  keys        origin-2 and its issuer publish its keys, and none of the
              origins' or the tests'; the origins publish none of its
  routing     each director sends a client's direct read (?directread)
              of origin-2's prefixes to it alone, and of the origins' to
              them alone, or where the export takes no direct clients,
              finds no origin (405) and names none; and through each
              cache, /public/other/... is origin-2's object, never what
              origin-0's /public holds at other/..., while
              /public/others-<run>/... is origin-0's
  <credential>
              each credential of the table in testlib/owners.py, on both
              sides of each pair: GET, HEAD, PUT, and DELETE straight to
              the side's origins, and GET and HEAD to each cache
  get, direct-get, put, cross-get, cross-put
              the client: each owner's objects through a cache and from
              its origin, an upload with origin-2's token, and each
              owner's token in the other's namespace

`ready` runs first, whatever else is asked for, since nothing else can
pass until origin-2 serves; the suite stops if it can't pass.

Objects go under data/owners/<run>/ in each namespace's storage, and are
removed after the suite, from a pstore origin too. Responses go to
framework/var/data/owners-test/.
"""

import json
import os
import shutil
import time
import traceback
from typing import Optional

from suites.auth import label, record, record_skip, save
from testlib import common, credentials, director, owners, stores, transfers, web
from testlib.common import die, first
from testlib.credentials import ALLOW
from testlib.owners import CREDENTIALS, OwnerCredential, Pair
from testlib.report import FAIL, PASS, SKIP, Report
from testlib.session import Session, last_line

OUT = "data/owners-test"

CLIENT = {
    "get": "`pelican object get` of each of origin-2's exports, through a cache",
    "direct-get": "the same with --direct, from origin-2, where the export allows it",
    "put": "`pelican object put` to /other with origin-2's token, where it takes writes",
    "cross-get": "`pelican object get` with each owner's token in the other's namespace: refused",
    "cross-put": "`pelican object put` likewise: refused, and nothing stored",
}

SCENARIOS = {
    "ready": "origin-2's objects come back through the federation and each cache",
    "keys": "origin-2 and its issuer publish its keys alone; the origins none of them",
    "routing": "directors and caches send each prefix to its owner, by whole path segments",
}
SCENARIOS.update({c.name: f"{c.description}, on both sides of each pair" for c in CREDENTIALS})
SCENARIOS.update(CLIENT)

# How long `ready` waits: tries, and seconds between them.
TRIES, INTERVAL = 20, 15


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no other owner, or no pair."""
    if not fed.owners:
        return "there is no other owner ('-p topo-multi-owner')"
    if not owners.pairs(fed):
        return "the origins export nothing that another owner mirrors"
    return None


class Test:
    """The pairs, the stores, and the credentials' tokens."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.pairs = owners.pairs(self.fed)
        self.origins = stores.unique(self.fed.origins)
        # The storage of every namespace on either side of a pair, which the
        # suite writes in: the other owners', then the origins'.
        self.storages = list(
            dict.fromkeys(
                [
                    owner.origin.store_of(e.path)
                    for owner in self.fed.owners
                    for e in owner.exports
                ]
                + [o.store_of(p.like) for o in self.origins for p in self.pairs]
            )
        )
        self.base = f"data/owners/{session.run}"
        self._tokens: dict[tuple[str, str, str], tuple[Optional[str], Optional[str]]] = {}

    def token(
        self, cred: OwnerCredential, pair: Pair, side: str
    ) -> tuple[Optional[str], Optional[str]]:
        """cred's token for side of pair, and its file; (None, None) for
        `none`."""
        key = (cred.name, pair.export.name, side)
        if key not in self._tokens:
            path = self.session.path(f"tokens/owners.{cred.name}.{pair.name(side)}")
            token = owners.mint(self.fed, cred, pair, side, path)
            self._tokens[key] = (token, path if token else None)
        return self._tokens[key]

    def stores_of(self, pair: Pair, side: str) -> list[common.Origin]:
        """The origins of side of pair."""
        return [pair.owner.origin] if side == "owner" else self.origins

    @staticmethod
    def path(origin: common.Origin, pair: Pair, side: str, rel: str) -> str:
        """Where rel is in origin's storage of side's namespace of pair."""
        return f"{origin.store_of(pair.namespace(side))}/{rel}"

    def holders(self, pair: Pair, rel: str) -> list[str]:
        """The origins of either side of pair whose storage of the side
        holds rel."""
        sides = [(pair.owner.origin, "owner")] + [(o, "origins") for o in self.origins]
        return [o.svc for o, side in sides if common.sha256(self.path(o, pair, side, rel))]

    @staticmethod
    def object_rel(side: str, n: int = 0) -> str:
        """One of the side's test objects (see init-data.py)."""
        return f"data/{'2' if side == 'owner' else '0'}.{n}"


def hosts_named(answer: director.Answer, hosts: list[str]) -> list[str]:
    """Those of hosts that answer names."""
    return [h for h in hosts if answer.names(h)]


def reader(pair: Pair) -> OwnerCredential:
    """Who reads the other owner's side of pair: origin-2's token where a
    token decides, and none in /public/other."""
    return owners.BY_NAME["owner" if pair.like in credentials.PROTECTED else "none"]


# --------------------------------------------------------------------------
# ready


def ready(test: Test) -> tuple[str, str]:
    """Dies if origin-2 never serves: nothing after it could pass."""
    why = ""
    for n in range(1, TRIES + 1):
        why = not_ready(test)
        if not why:
            return PASS, f"after {n} tries" if n > 1 else ""
        if n == 1:
            print("Waiting for the federation to serve origin-2's objects ...")
        if n < TRIES:
            time.sleep(INTERVAL)
    die(f"gave up waiting for origin-2 after {TRIES} tries, {INTERVAL}s apart ({why})")


def not_ready(test: Test) -> str:
    """Why origin-2's objects do not yet come back, or ""."""
    fed = test.fed
    for pair in test.pairs:
        rel = test.object_rel("owner")
        want = common.sha256(test.path(pair.owner.origin, pair, "owner", rel))
        token, token_file = test.token(reader(pair), pair, "owner")
        target = test.session.path(f"owners-ready-{pair.export.name}")
        if os.path.exists(target):
            os.remove(target)
        args = ["--token", token_file] if token_file else []
        code, out, err = test.session.pelican_cmd(
            "object", "get", *args, f"{fed.url}{pair.export.prefix}/{rel}", target
        )
        if code != 0:
            return (
                f"`pelican object get` of {pair.export.prefix}/{rel}: {last_line(err or out)}"
            )
        if common.sha256(target) != want:
            return f"`pelican object get` of {pair.export.prefix}/{rel}: not the object"
        for cache in fed.caches:
            answer = web.request("GET", f"{cache.url}{pair.export.prefix}/{rel}", token=token)
            if answer.status != 200 or common.digest(answer.body) != want:
                return f"{cache.svc}: GET {pair.export.prefix}/{rel}: {answer.describe()}"
    return ""


# --------------------------------------------------------------------------
# keys


def get_kids(url: str, name: str) -> tuple[Optional[frozenset[str]], str]:
    """The kids of the JWKS at url, kept as name, or why there are none."""
    answer = web.request("GET", url)
    save(f"{OUT}/responses/{name}", answer)
    if answer.status != 200:
        return None, f"{url}: {answer.describe()}"
    try:
        return credentials.key_ids(answer.body), ""
    except ValueError:
        return None, f"{url}: not a JWKS"


def keys(test: Test) -> tuple[str, str]:
    """Each owner and its issuers publish its keys alone, and the origins
    none."""
    fed = test.fed
    problems: list[str] = []
    origins_kids = set(owners.key_ids(os.path.dirname(fed.origin_key)))
    test_kids: set[str] = set()
    for path in [credentials.SERVER_JWKS] + [
        credentials.namespace_jwks(ns) for ns in credentials.PROTECTED
    ]:
        try:
            with open(path, "rb") as f:
                test_kids |= credentials.key_ids(f.read())
        except (FileNotFoundError, ValueError):
            die(f"framework/var/{path} is missing or not a JWKS; run ./fed.sh keys init")
    checked = 0
    for owner in fed.owners:
        owner_kids = set(owners.key_ids(owner.key_dir))
        if not owner_kids:
            die(f"no key in framework/var/{owner.key_dir}/; run ./fed.sh init")
        others = origins_kids | test_kids
        documents = [(f"{owner.web_url}/.well-known/issuer.jwks", f"{owner.origin.svc}-server")]
        for export in owner.exports:
            if not credentials.has_issuer(export.caps):
                continue
            answer = web.request("GET", f"{export.issuer}/.well-known/openid-configuration")
            save(f"{OUT}/responses/keys-{export.name}-discovery", answer)
            want = f"{export.issuer}/.well-known/issuer.jwks"
            try:
                have = json.loads(answer.body).get("jwks_uri") if answer.status == 200 else None
            except (ValueError, AttributeError):
                have = None
            if have != want:
                problems.append(
                    f"{export.prefix}'s discovery document: {answer.describe()},"
                    + f" jwks_uri {have}, not {want}"
                )
            documents.append((want, f"{export.name}-jwks"))
        for url, name in documents:
            kids, why = get_kids(url, f"keys-{name}")
            checked += 1
            if kids is None:
                problems.append(why)
                continue
            if not owner_kids <= kids:
                problems.append(f"{url} lacks {owner.origin.svc}'s keys")
            if kids & others:
                problems.append(f"{url} holds keys that are not {owner.origin.svc}'s")
        # The other way: the origins publish none of its keys.
        theirs = [(f"{fed.origin_web_url}/.well-known/issuer.jwks", "origins-server")]
        for pair in test.pairs:
            if credentials.has_issuer(fed.exports[pair.like]):
                theirs.append(
                    (f"{fed.issuer_of(pair.like)}/.well-known/issuer.jwks", f"{pair.like}-jwks")
                )
        for url, name in dict.fromkeys(theirs):
            kids, why = get_kids(url, f"keys-{name}")
            checked += 1
            if kids is None:
                problems.append(why)
            elif kids & owner_kids:
                problems.append(f"{url} holds {owner.origin.svc}'s keys")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{checked} JWKS"


# --------------------------------------------------------------------------
# routing


def routing(test: Test) -> tuple[str, str]:
    """A client's direct read (?directread) of each side of each pair must
    go to the side's own origins alone; where the side's export takes no
    direct clients, the director must find no origin (405), and name
    none, not even the other side's, whose prefix may enclose it."""
    fed = test.fed
    problems: list[str] = []
    origin_hosts = [director.host(o.url) for o in fed.origins]
    notes: list[str] = []
    for pair in test.pairs:
        owner_host = director.host(pair.owner.origin.url)
        # Each side: an object there, its origins, and whether its export
        # takes direct reads.
        sides = [
            (
                f"{pair.export.prefix}/{test.object_rel('owner')}",
                [owner_host],
                "DirectReads" in pair.export.caps,
            ),
            (
                f"/{pair.like}/{test.object_rel('origins')}",
                origin_hosts,
                "DirectReads" in fed.exports[pair.like],
            ),
        ]
        for url in fed.directors:
            for path, hosts, direct in sides:
                answer = director.ask(url, "origin", path, query="directread")
                wanted = hosts if direct else []
                unwanted = hosts_named(
                    answer, [h for h in origin_hosts + [owner_host] if h not in wanted]
                )
                what = f"{director.host(url)}, for a direct read of {path},"
                if wanted and not hosts_named(answer, wanted):
                    problems.append(f"{what} named none of {wanted}: {answer.describe()}")
                if unwanted:
                    problems.append(f"{what} named {unwanted}")
                if not direct and answer.status != 405:
                    problems.append(f"{what} should find no origin (405): {answer.describe()}")
        notes.append(
            f"{len(sides) * len(fed.directors)} direct read(s) of {pair.export.prefix}"
            + f" and /{pair.like}"
        )
    public = next((p for p in test.pairs if p.like == "public"), None)
    if public is None:
        notes.append("no /public/other")
    else:
        problems += nested(test, public)
        notes.append("/public/other and its sibling through each cache")
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(notes)


def nested(test: Test, pair: Pair) -> list[str]:
    """/public/other/<rel> must be origin-2's, never what origin-0's
    /public holds at other/<rel> (a decoy); and /public/others-<run>/probe
    origin-0's, since a prefix matches whole path segments."""
    fed = test.fed
    rel = f"{test.base}/routed"
    mine, decoy, sibling = os.urandom(3000), os.urandom(3000), os.urandom(3000)
    sibling_rel = f"others-{test.session.run}/probe"
    errors = [stores.write_file(pair.owner.origin.store_of(pair.export.path), rel, mine)]
    for origin in test.origins:
        errors.append(stores.write_file(origin.store_of(pair.like), f"other/{rel}", decoy))
        errors.append(stores.write_file(origin.store_of(pair.like), sibling_rel, sibling))
    if any(errors):
        return [e for e in errors if e]
    problems: list[str] = []
    for cache in fed.caches:
        for path, want in ((f"/public/other/{rel}", mine), (f"/public/{sibling_rel}", sibling)):
            answer = web.request("GET", f"{cache.url}{path}")
            if answer.status != 200:
                problems.append(f"{cache.svc}: GET {path}: {answer.describe()}")
            elif answer.body == decoy:
                problems.append(
                    f"{cache.svc}: GET {path}: served what origin-0's /public holds"
                    + " at other/..."
                )
            elif answer.body != want:
                problems.append(f"{cache.svc}: GET {path}: not the object")
    return problems


def clean_nested(test: Test) -> None:
    """Remove what nested() put in the origins' storage of /public."""
    for origin in test.origins:
        public = origin.store_of("public")
        shutil.rmtree(f"{public}/other", ignore_errors=True)
        shutil.rmtree(f"{public}/others-{test.session.run}", ignore_errors=True)


# --------------------------------------------------------------------------
# The credentials, straight to each server.


def check_credential(test: Test, cred: OwnerCredential, report: Report) -> None:
    """Present cred on both sides of each pair, at every server."""
    fed = test.fed
    for pair in test.pairs:
        if not owners.applies(cred, pair):
            continue
        for side in owners.SIDES:
            ns, name = pair.namespace(side), pair.name(side)
            token, _ = test.token(cred, pair, side)
            rel = test.object_rel(side)
            want = common.sha256(test.path(test.stores_of(pair, side)[0], pair, side, rel))
            targets: list[tuple[str, str, Optional[common.Origin]]] = [
                (label(o.svc, "origin"), o.url, o) for o in test.stores_of(pair, side)
            ]
            targets += [(label(c.svc, "cache"), c.url, None) for c in fed.caches]
            for target, url, origin in targets:
                direct = origin is not None
                for op in ("get", "head"):
                    answer = web.request(op.upper(), f"{url}/{ns}/{rel}", token=token)
                    save(f"{OUT}/responses/{cred.name}-{target}-{name}-{op}", answer)
                    ok = owners.expected(fed, cred, pair, side, op, direct) == ALLOW
                    why = ""
                    if ok and answer.status != 200:
                        why = "expected 200"
                    elif ok and op == "get" and common.digest(answer.body) != want:
                        why = "the body is not the object"
                    elif not ok and answer.status not in (401, 403):
                        why = "expected 401 or 403"
                    elif not ok and op == "get" and common.digest(answer.body) == want:
                        why = "refused, but the body is the object"
                    row(report, cred, target, name, op, answer, why)
                if origin is not None:
                    check_put(test, report, cred, pair, side, target, origin, token)
                    check_delete(test, report, cred, pair, side, target, origin, token)


def row(
    report: Report,
    cred: OwnerCredential,
    target: str,
    name: str,
    op: str,
    answer: web.Response,
    why: str,
) -> None:
    """Print and record the row of one request."""
    record(
        report,
        f"{cred.name:<11} {target:<10} {name:<12} {op:<6}",
        f"{cred.name} {target} {name} {op}",
        answer,
        why,
    )


def check_put(
    test: Test,
    report: Report,
    cred: OwnerCredential,
    pair: Pair,
    side: str,
    target: str,
    origin: common.Origin,
    token: Optional[str],
) -> None:
    """PUT new bytes to side of pair at origin, with token."""
    ns, name = pair.namespace(side), pair.name(side)
    rel = f"{test.base}/{cred.name}-{target}-{name}-put"
    data = os.urandom(4096)
    answer = web.request("PUT", f"{origin.url}/{ns}/{rel}", token=token, upload=data)
    save(f"{OUT}/responses/{cred.name}-{target}-{name}-put", answer)
    held = test.holders(pair, rel)
    why = ""
    if owners.expected(test.fed, cred, pair, side, "put", True) == ALLOW:
        if not answer.ok:
            why = "expected 2xx"
        elif common.sha256(test.path(origin, pair, side, rel)) != common.digest(data):
            why = f"{origin.svc}'s store does not hold the object"
    elif answer.status not in (401, 403):
        why = "expected 401 or 403"
    elif held:
        why = f"refused, but {', '.join(held)} hold the object"
    row(report, cred, target, name, "put", answer, why)


def check_delete(
    test: Test,
    report: Report,
    cred: OwnerCredential,
    pair: Pair,
    side: str,
    target: str,
    origin: common.Origin,
    token: Optional[str],
) -> None:
    """DELETE an object put in origin's store for it, with token."""
    ns, name = pair.namespace(side), pair.name(side)
    cannot_delete = stores.cannot(test.fed, "delete")
    if cannot_delete and owners.expected(test.fed, cred, pair, side, "delete", True) == ALLOW:
        heading = f"{cred.name:<11} {target:<10} {name:<12} {'delete':<6}"
        record_skip(report, heading, f"{cred.name} {target} {name} delete", cannot_delete)
        return
    rel = f"{test.base}/{cred.name}-{target}-{name}-delete"
    error = stores.write_file(origin.store_of(pair.namespace(side)), rel, os.urandom(4096))
    if error:
        row(
            report,
            cred,
            target,
            name,
            "delete",
            web.Response(None),
            f"could not put the object to delete in place ({error})",
        )
        return
    answer = web.request("DELETE", f"{origin.url}/{ns}/{rel}", token=token)
    save(f"{OUT}/responses/{cred.name}-{target}-{name}-delete", answer)
    there = common.sha256(test.path(origin, pair, side, rel)) is not None
    why = ""
    if owners.expected(test.fed, cred, pair, side, "delete", True) == ALLOW:
        if not answer.ok:
            why = "expected 2xx"
        elif there:
            why = "the store still holds the object"
    elif answer.status not in (401, 403):
        why = "expected 401 or 403"
    elif not there:
        why = "refused, but the store no longer holds the object"
    row(report, cred, target, name, "delete", answer, why)


# --------------------------------------------------------------------------
# The client.


def client_get(
    test: Test, pair: Pair, side: str, cred: OwnerCredential, direct: bool, tag: str
) -> Optional[str]:
    """`pelican object get` of one of side's objects with cred's token,
    into a directory, so that the client stats the object first and a
    refusal reads as the server's (see testlib/transfers.py); what went
    wrong, or None."""
    fed = test.fed
    _, token_file = test.token(cred, pair, side)
    rel = test.object_rel(side, 1)
    into = test.session.path(f"owners-{tag}")
    shutil.rmtree(into, ignore_errors=True)
    os.makedirs(into)
    target = os.path.join(into, rel.rsplit("/", 1)[-1])
    stats = f"{into}.json"
    if os.path.exists(stats):
        os.remove(stats)
    args = ["--token", token_file] if token_file else []
    if direct:
        args.append("--direct")
    ns = pair.namespace(side)
    code, out, err = test.session.pelican_cmd(
        "object", "get", *args, "--transfer-stats", stats, f"{fed.url}/{ns}/{rel}", into
    )
    what = f"{'direct ' if direct else ''}get of /{ns}/{rel} ({cred.name})"
    if owners.expected(fed, cred, pair, side, "get", direct) != ALLOW:
        if code == 0:
            return f"{what}: succeeded, but should have been refused"
        if transfers.refused(out + err) is None:
            return f"{what}: failed, but not by refusal: {last_line(err or out)}"
        return None
    if code != 0:
        return f"{what}: exit {code}: {last_line(err or out)}"
    if common.sha256(target) != common.sha256(
        test.path(test.stores_of(pair, side)[0], pair, side, rel)
    ):
        return f"{what}: not the object"
    try:
        with open(stats, encoding="utf-8") as f:
            hosts = transfers.stats_endpoints(json.load(f))
    except (FileNotFoundError, ValueError):
        return f"{what}: no transfer stats"
    servers = (
        {director.host(o.url) for o in test.stores_of(pair, side)}
        if direct
        else {director.host(c.url) for c in fed.caches}
    )
    if not hosts or not set(hosts) <= servers:
        return f"{what}: served by {sorted(set(hosts))}, not {sorted(servers)}"
    return None


def client_put(
    test: Test, pair: Pair, side: str, cred: OwnerCredential, tag: str
) -> Optional[str]:
    """`pelican object put` of a new object to side with cred's token; what
    went wrong, or None."""
    fed = test.fed
    _, token_file = test.token(cred, pair, side)
    data = os.urandom(5000)
    source = test.session.path(f"owners-{tag}")
    with open(source, "wb") as f:
        f.write(data)
    rel = f"{test.base}/{tag}"
    ns = pair.namespace(side)
    args = ["--token", token_file] if token_file else []
    code, out, err = test.session.pelican_cmd(
        "object", "put", *args, source, f"{fed.url}/{ns}/{rel}"
    )
    what = f"put to /{ns}/{rel} ({cred.name})"
    held = test.holders(pair, rel)
    if owners.expected(fed, cred, pair, side, "put", False) != ALLOW:
        if code == 0:
            return f"{what}: succeeded, but should have been refused"
        if transfers.refused(out + err) is None:
            return f"{what}: failed, but not by refusal: {last_line(err or out)}"
        if held:
            return f"{what}: refused, but {', '.join(held)} hold it"
        return None
    if code != 0:
        return f"{what}: exit {code}: {last_line(err or out)}"
    right = [
        o
        for o in test.stores_of(pair, side)
        if common.sha256(test.path(o, pair, side, rel)) == common.digest(data)
    ]
    if not right:
        return f"{what}: no store of the side holds it"
    return None


def client(test: Test, name: str) -> tuple[str, str]:
    """The client scenario name, for each pair."""
    owner, theirs = owners.BY_NAME["owner"], owners.BY_NAME["origins"]
    problems: list[str] = []
    done = 0
    for pair in test.pairs:
        tag = f"{name}-{pair.export.name}"
        found: list[Optional[str]] = []
        if name in ("get", "direct-get"):
            found.append(
                client_get(test, pair, "owner", reader(pair), name == "direct-get", tag)
            )
        elif pair.like not in credentials.PROTECTED:
            continue
        elif name == "put":
            found.append(client_put(test, pair, "owner", owner, tag))
        elif name == "cross-get":
            found.append(client_get(test, pair, "owner", theirs, False, f"{tag}-owner"))
            found.append(client_get(test, pair, "origins", owner, False, f"{tag}-origins"))
        else:
            found.append(client_put(test, pair, "owner", theirs, f"{tag}-owner"))
            found.append(client_put(test, pair, "origins", owner, f"{tag}-origins"))
        done += len(found)
        problems += [p for p in found if p]
    if not done:
        return SKIP, "origin-2 exports no namespace that takes a token"
    if problems:
        return FAIL, first(problems)
    return PASS, f"{done} command(s)"


# --------------------------------------------------------------------------

CHECKS = {"ready": ready, "keys": keys, "routing": routing}


def clean(test: Test) -> list[str]:
    """Empty data/owners in the storage of every namespace on either side
    of a pair (see run()); what went wrong. A pstore origin's encrypted
    store, which only it can change, is emptied through it, and its plain
    copy, which the suite writes too, directly."""
    fed = test.fed
    errors: list[str] = []
    for storage in test.storages:
        try:
            common.empty_dir(f"{storage}/data/owners")
        except OSError as e:
            errors.append(f"emptying framework/var/{storage}/data/owners: {e.strerror}")
    pstores = [o for o in test.origins if o.pstore]
    if pstores:
        errors += stores.empty_tree_everywhere(
            pstores, fed.exports, "data/owners", test.session.tokens()
        )
    return errors


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, `ready` first, and remove the suite's
    objects."""
    fed = session.fed
    test = Test(session)
    for storage in test.storages:
        if not os.path.isdir(f"{storage}/data/owners"):
            die(f"framework/var/{storage}/data/owners is missing; run ./fed.sh init")
    # Earlier runs' objects come out first.
    errors = clean(test)
    if errors:
        die(f"could not remove earlier runs' objects: {first(errors)}")
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(f"{OUT}/responses")
    for storage in test.storages:
        os.makedirs(f"{storage}/{test.base}")
        os.chmod(f"{storage}/{test.base}", 0o777)  # nosec B103
    print(
        f"Another owner: {', '.join(o.origin.svc for o in fed.owners)}; pairs:"
        + f" {', '.join(f'{p.export.prefix} ~ /{p.like}' for p in test.pairs)}\n"
    )
    header = False
    try:
        # Nothing else can pass until origin-2 serves.
        if selected and "ready" not in selected:
            ready(test)
        for name in selected:
            try:
                if name in CHECKS:
                    results.add(name, *CHECKS[name](test))
                elif name in CLIENT:
                    results.add(name, *client(test, name))
                else:
                    if not header:
                        print(
                            f"\n{'credential':<11} {'target':<10} {'ns':<12} {'op':<6}"
                            + f" {'code':>4}  result"
                        )
                        header = True
                    check_credential(test, owners.BY_NAME[name], results.report)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                results.add(name, FAIL, f"{type(e).__name__}: {e}")
    finally:
        clean_nested(test)
        # The origins' files are theirs, and on a Linux host beyond
        # init-data.py's reach.
        errors = clean(test)
        if errors:
            common.warn(f"could not remove the suite's objects: {first(errors)}")
