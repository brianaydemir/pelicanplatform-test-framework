"""The client's other commands: `pelican object ls`, `stat`, `delete`,
`copy`, `sync`, and `du`, in /public and /protected-a, checking each
against the origins' stores.

The read-only commands run against a tree uploaded straight to every
origin, /<namespace>/data/cmd/<run>/tree/, so that they agree whichever
origin the director picks. The rest go through the director like any
client. Under `topo-multi-origin` their effects land on one origin, so
a success must show in some store, and a refusal in none. /public takes
no writes (see testlib/credentials.py), so writes to it must be refused.
Likewise, a command that needs a capability its namespace lacks (see
NEEDS), e.g. every write under `-p origin-no-direct`, must be refused,
and change nothing.

The *-no-token scenarios check that the client itself refuses, for want
of a token (Error code 4010), before it asks any server.

These commands' exit status says little about what went wrong (mostly
1 or 11), so failures are judged by what they print (see
testlib/transfers.py's FAILURE_RULES).
The run's objects are removed afterward, from a pstore origin too.
"""

import binascii
import hashlib
import json
import os
import re
import traceback
from collections import Counter
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from testlib import blocks, common, credentials, stores, transfers
from testlib.common import die
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ls-protected-a":   "`ls -l --json` of /protected-a: names, sizes, and collections",
    "ls-public":        "the same in /public, with no token",
    "ls-recursive":     "`ls -r` lists every object in the tree, by its path in it",
    "ls-missing":       "`ls` of a missing path: not found",
    "ls-no-token":      "`ls` of /protected-a with no token: the client refuses (Error code 4010)",
    "stat-protected-a": "`stat --json --checksums crc32c --checksums md5`: size and checksums",
    "stat-public":      "the same in /public, with no token",
    "stat-collection":  "`stat` of a collection says so",
    "stat-missing":     "`stat` of a missing object: not found",
    "delete-object":    "`delete` removes an object",
    "delete-recursive": "`delete -r` removes a collection and what it holds",
    "delete-nonempty":  "`delete` of a non-empty collection without -r: refused, nothing removed",
    "delete-missing":   "`delete` of a missing object: not found",
    "delete-read-only": "`delete` with a read-only token: refused, nothing removed",
    "delete-no-token":  "`delete` with no token: the client refuses (Error code 4010), nothing"
                        " removed",
    "delete-public":    "`delete` in /public, which takes no writes: refused, nothing removed",
    "copy-upload":      "`copy` of a local file to /protected-a",
    "copy-download":    "`copy` of an object to a local file",
    "copy-tpc":         "`copy` from /protected-a to /protected-a: a third-party copy",
    "copy-tpc-public":  "`copy` from /public to /protected-a: a third-party copy",
    "copy-tpc-direct":  "`copy --direct`: a third-party copy from an origin",
    "copy-to-public":   "`copy` to /public, which takes no writes: refused",
    "sync-upload":      "`sync` of a local tree to a new collection",
    "sync-unchanged":   "`sync --dry-run` of a tree the origins already hold lists nothing",
    "sync-changed":     "a local file whose size changed is the one `sync` uploads again",
    "sync-download":    "`sync` of a collection to a local directory",
    "sync-tpc":         "`sync` of a collection to another: third-party copies",
    "du-protected-a":   "`du --json`, and `du --count`'s text: bytes, objects, and collections"
                        " per collection",
    "du-public":        "the same in /public, with no token",
    "du-summarize":     "`du -s` gives only the total",
    "du-no-token":      "`du` of /protected-a with no token: the client refuses (Error code 4010)",
}

# The capabilities each scenario needs of its namespace (/public for
# ls-public and du-public, and otherwise /protected-a), beyond reading.
LISTS = frozenset({"Listings"})
WRITES = frozenset({"Writes"})
NEEDS: Dict[str, FrozenSet[str]] = {
    "ls-protected-a": LISTS, "ls-public": LISTS, "ls-recursive": LISTS, "ls-missing": LISTS,
    "delete-object": WRITES, "delete-recursive": WRITES | LISTS, "delete-nonempty": WRITES,
    "delete-missing": WRITES,
    "copy-upload": WRITES, "copy-tpc": WRITES, "copy-tpc-public": WRITES,
    "copy-tpc-direct": WRITES | {"DirectReads"},
    "sync-upload": WRITES | LISTS, "sync-unchanged": WRITES | LISTS,
    "sync-changed": WRITES | LISTS, "sync-download": LISTS, "sync-tpc": WRITES | LISTS,
    "du-protected-a": LISTS, "du-public": LISTS, "du-summarize": LISTS,
}


def needs_of(name: str) -> Tuple[str, FrozenSet[str]]:
    """The namespace whose capabilities scenario name needs, and those
    capabilities."""
    namespace = "public" if name in ("ls-public", "du-public") else "protected-a"
    return namespace, NEEDS.get(name, frozenset())


def uses_public(name: str) -> bool:
    """Whether scenario name uses /public, and so skips where it is not
    exported: each that does is named *-public, e.g. copy-tpc-public,
    which copies from it."""
    return name.endswith("-public")


# The tree the read-only commands work on, under data/cmd/<run>/tree/.
TREE = {"a": 1000, "b": 5000, "sub/c": 300, "sub/deeper/d": 7000}


#---------------------------------------------------------------------------
# Checksums, as `stat --checksums` reports them: hex.

def digest_bytes(value: str) -> Optional[bytes]:
    """The digest a reported checksum stands for."""
    try:
        return binascii.unhexlify(value.strip())
    except binascii.Error:
        return None


def checksum_matches(kind: str, value: str, data: bytes) -> bool:
    got = digest_bytes(value)
    if got is None:
        return False
    if kind == "crc32c":
        return int.from_bytes(got, "big") == blocks.crc32c(data)
    return got == hashlib.md5(data).digest()


#---------------------------------------------------------------------------
# What a failed command said.

refused = transfers.refused


def not_found(text: str) -> bool:
    return bool(re.search(r"Error code 5011|\b404\b|does not exist|not found", text, re.I))


def no_token_refusal(code: int, out: str, err: str) -> Optional[str]:
    """Why a command run with no token was not refused by the client
    itself, for want of one (Error code 4010, the `client` refusal of
    FAILURE_RULES), or None."""
    if code == 0:
        return "succeeded, but the client should have refused: no token"
    if not re.search(r"Error code 4010\b", out + err):
        return (f"failed, but the client did not refuse for want of a token (Error code 4010):"
                f" {last_line(err or out)}")
    return None


class Test:
    """What every scenario needs: tokens, the stores, and the tree."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.origins = session.fed.origins
        self.tmp = os.path.join(session.tmp, "commands")
        os.makedirs(self.tmp)
        self.run = session.run
        self.base = f"data/cmd/{self.run}"
        # The namespaces the scenarios use, and what the running one lacks
        # (see NEEDS).
        namespaces: List[str] = [ns for ns in ("public", "protected-a")
                                 if ns in session.fed.exports]
        self.lacks: FrozenSet[str] = frozenset()
        self.token_files = {ns: session.token_file(ns) for ns in namespaces}
        self.tokens = {ns: session.token(ns) for ns in namespaces}
        self.read_only = session.token_file("protected-a", credentials.R)
        self.env = session.env
        self.sync_env = session.sync_env
        self.tree = {path: os.urandom(size) for path, size in TREE.items()}

    # URLs, paths, and stores.

    def url(self, namespace: str, rel: str) -> str:
        return f"{self.fed.url}/{namespace}/{rel}"

    def local(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def held(self, namespace: str, rel: str) -> List[Optional[str]]:
        """What each store holds at rel (see stores.held_anywhere)."""
        return stores.held_anywhere(self.origins, namespace, rel, self.tokens)

    def somewhere(self, namespace: str, rel: str, data: bytes) -> bool:
        return hashlib.sha256(data).hexdigest() in self.held(namespace, rel)

    def nowhere(self, namespace: str, rel: str) -> bool:
        return all(h is None for h in self.held(namespace, rel))

    def everywhere(self, namespace: str, rel: str, data: bytes) -> bool:
        return all(h == hashlib.sha256(data).hexdigest() for h in self.held(namespace, rel))

    def gone_somewhere(self, namespace: str, rel: str) -> bool:
        return any(h is None for h in self.held(namespace, rel))

    # /public, which takes no writes, reads /protected-a's storage, on a
    # pstore origin too, so both namespaces get their objects through
    # /protected-a.

    def seed(self, rel: str, data: bytes) -> None:
        """Put data at rel in both namespaces at every origin (see
        stores.seed())."""
        token = self.tokens["protected-a"]
        parent = rel.rsplit("/", 1)[0]
        errors = stores.make_dirs(self.origins, "protected-a", parent, token)
        errors += stores.seed_everywhere(self.origins, "protected-a", rel, data, token,
                                         self.fed.exports["protected-a"])
        if errors:
            die("could not put the test objects in place: " + "; ".join(errors))

    def make_dirs(self, rel: str) -> None:
        errors = stores.make_dirs(self.origins, "protected-a", rel, self.tokens["protected-a"])
        if errors:
            die("could not make the test collections: " + "; ".join(errors))

    def seed_tree(self) -> None:
        for path, data in self.tree.items():
            self.seed(f"{self.base}/tree/{path}", data)

    # Running the client.

    def pelican_cmd(self, *args: str, env: Optional[Dict[str, str]] = None,
                    cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Run `pelican <args>`: its exit status, stdout, and stderr."""
        return self.session.pelican_cmd(*args, env=env or self.env, cwd=cwd or self.tmp)

    def token_args(self, namespace: str) -> List[str]:
        return ["-t", self.token_files[namespace]]

    def expect_refusal(self, code: int, out: str, err: str) -> Optional[str]:
        """Why a command that should have been refused wasn't, or None."""
        if code == 0:
            return "succeeded, but should have been refused"
        if refused(out + err) is None:
            return f"failed, but not by refusal: {last_line(err or out)}"
        return None

    def expect_not_found(self, code: int, out: str, err: str) -> Optional[str]:
        if self.lacks:
            return self.expect_refusal(code, out, err)
        if code == 0:
            return "succeeded, but should have failed: not found"
        if not not_found(out + err):
            return f"failed, but not with not found: {last_line(err or out)}"
        return None

    def outcome(self, code: int, out: str, err: str, passed: Callable[[], Tuple[str, str]],
                unchanged: Callable[[], Optional[str]] = lambda: None) -> Tuple[str, str]:
        """A scenario's verdict on its command: passed() if it succeeded as
        it should. Where the namespace lacks a capability that the command
        needs (self.lacks), it must instead have been refused, and
        unchanged() must find nothing changed."""
        if self.lacks:
            why = self.expect_refusal(code, out, err) or unchanged()
            if why:
                return FAIL, why
            return PASS, f"refused ({refused(out + err)}): no {', '.join(sorted(self.lacks))}"
        if code != 0:
            return FAIL, f"exit {code}: {last_line(err)}"
        return passed()


# How errors name a JSON value's type.
JSON_TYPES = {dict: "an object", list: "an array", str: "a string", int: "a number",
              float: "a number", bool: "a boolean", type(None): "null"}


def json_out(text: str, kind: type) -> Any:
    """The JSON a command printed on stdout: its last line that parses,
    which must be a kind (dict or list); a list must hold only objects.
    Raises ValueError if it isn't."""
    for line in reversed(text.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        break
    else:
        raise ValueError("no JSON in the output")
    if not isinstance(value, kind):
        raise ValueError(f"the JSON is {JSON_TYPES.get(type(value), type(value).__name__)},"
                         f" not {JSON_TYPES[kind]}")
    if isinstance(value, list) and not all(isinstance(item, dict) for item in value):
        raise ValueError("the JSON array holds more than objects")
    return value


def basename(name: str) -> str:
    return name.rstrip("/").rsplit("/", 1)[-1]


def normal_path(name: str) -> str:
    """A path the client printed, as a bare path: without a URL's scheme
    and host, doubled slashes, or a trailing slash."""
    if "://" in name:
        name = urlsplit(name).path
    return re.sub(r"/{2,}", "/", name).rstrip("/") or "/"


def tree_rel(name: str, root: str) -> str:
    """Where name is in the tree at path root, e.g. sub/c; name as
    printed if it is not in the tree."""
    path = normal_path(name)
    return path[len(root) + 1:] if path.startswith(root + "/") else name


def tree_objects(entries: Iterable[Dict[str, Any]], root: str) -> Counter:
    """How often `ls -l --json` listed each object, as (path in the tree
    at root, size), so that nesting and duplicates count."""
    return Counter((tree_rel(e["Name"], root), e.get("Size")) for e in entries
                   if not e.get("IsCollection"))


def unchanged(test: Test, objects: Dict[str, bytes]) -> Optional[str]:
    """Why every origin does not still hold objects (bytes by rel in
    /protected-a), or None."""
    changed = [rel for rel, data in objects.items() if not test.everywhere("protected-a", rel, data)]
    if changed:
        return f"refused, but an origin no longer holds {', '.join(changed)}"
    return None


def nowhere(test: Test, rels: List[str]) -> Optional[str]:
    """Why some origin holds one of rels in /protected-a, or None."""
    held = [rel for rel in rels if not test.nowhere("protected-a", rel)]
    if held:
        return f"refused, but an origin holds {', '.join(held)}"
    return None


#---------------------------------------------------------------------------
# ls

def ls_tree(test: Test, namespace: str) -> Tuple[str, str]:
    args = ["object", "ls", "-l", "--json"]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree"))

    def passed() -> Tuple[str, str]:
        try:
            entries = {basename(e["Name"]): e for e in json_out(out, list)}
        except (ValueError, KeyError, TypeError) as e:
            return FAIL, f"unreadable output ({e}): {last_line(out)}"
        want = {"a": 1000, "b": 5000, "sub": None}
        if set(entries) != set(want):
            return FAIL, f"listed {sorted(entries)}, not {sorted(want)}"
        for name, size in want.items():
            entry = entries[name]
            if size is None and not entry.get("IsCollection"):
                return FAIL, f"{name} is not listed as a collection"
            if size is not None and (entry.get("IsCollection") or entry.get("Size") != size):
                return FAIL, f"{name}: size {entry.get('Size')}, not {size}"
        return PASS, ", ".join(sorted(entries))

    return test.outcome(code, out, err, passed)


def ls_protected_a(test: Test) -> Tuple[str, str]:
    return ls_tree(test, "protected-a")


def ls_public(test: Test) -> Tuple[str, str]:
    return ls_tree(test, "public")


def ls_recursive(test: Test) -> Tuple[str, str]:
    url = test.url("protected-a", f"{test.base}/tree")
    code, out, err = test.pelican_cmd("object", "ls", "-r", "-l", "--json",
                                      *test.token_args("protected-a"), url)

    def passed() -> Tuple[str, str]:
        try:
            objects = tree_objects(json_out(out, list), normal_path(url))
        except (ValueError, KeyError, TypeError) as e:
            return FAIL, f"unreadable output ({e}): {last_line(out)}"
        want = Counter(TREE.items())
        if objects != want:
            return FAIL, f"listed {sorted(objects.elements())}, not {sorted(want.elements())}"
        return PASS, f"{sum(objects.values())} objects"

    return test.outcome(code, out, err, passed)


def ls_missing(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "ls", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree/nope"))
    return (FAIL, why) if (why := test.expect_not_found(code, out, err)) else (PASS, "")


def ls_no_token(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "ls", test.url("protected-a", f"{test.base}/tree"))
    if (why := no_token_refusal(code, out, err)):
        return FAIL, why
    return PASS, "the client refused: no token"


#---------------------------------------------------------------------------
# stat

def stat_object(test: Test, namespace: str) -> Tuple[str, str]:
    data = test.tree["b"]
    kinds = ["crc32c", "md5"]
    args = ["object", "stat", "--json"]
    for kind in kinds:
        args += ["--checksums", kind]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree/b"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        info = json_out(out, dict)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    if info.get("Size") != len(data) or info.get("IsCollection"):
        return FAIL, f"size {info.get('Size')}, collection {info.get('IsCollection')}"
    sums = {k.lower(): v for k, v in (info.get("checksums") or {}).items()}
    notes = []
    for kind in kinds:
        if kind not in sums:
            return FAIL, f"no {kind} checksum reported"
        if not checksum_matches(kind, sums[kind], data):
            return FAIL, f"{kind} {sums[kind]} does not match the object"
        notes.append(f"{kind} matches")
    return PASS, "; ".join(notes)


def stat_protected_a(test: Test) -> Tuple[str, str]:
    return stat_object(test, "protected-a")


def stat_public(test: Test) -> Tuple[str, str]:
    return stat_object(test, "public")


def stat_collection(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "stat", "--json", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree/sub"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        info = json_out(out, dict)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    if not info.get("IsCollection"):
        return FAIL, "not reported as a collection"
    return PASS, ""


def stat_missing(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "stat", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree/nope"))
    return (FAIL, why) if (why := test.expect_not_found(code, out, err)) else (PASS, "")


#---------------------------------------------------------------------------
# delete (a hidden command, which warns that it is)

def delete_object(test: Test) -> Tuple[str, str]:
    rel = f"{test.base}/del/object"
    data = os.urandom(2000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("protected-a"),
                                      test.url("protected-a", rel))

    def passed() -> Tuple[str, str]:
        if not test.gone_somewhere("protected-a", rel):
            return FAIL, "every origin still holds the object"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: unchanged(test, {rel: data}))


def delete_recursive(test: Test) -> Tuple[str, str]:
    rels = [f"{test.base}/del/tree/1", f"{test.base}/del/tree/sub/2"]
    data = {rel: os.urandom(1000) for rel in rels}
    for rel in rels:
        test.seed(rel, data[rel])
    code, out, err = test.pelican_cmd("object", "delete", "-r", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/del/tree"))

    def passed() -> Tuple[str, str]:
        held = [test.held("protected-a", rel) for rel in rels]
        # Some origin (the one the director chose) must hold none of them.
        if not any(all(h[i] is None for h in held) for i in range(len(held[0]))):
            return FAIL, "no origin is rid of the whole collection"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: unchanged(test, data))


def delete_nonempty(test: Test) -> Tuple[str, str]:
    rel = f"{test.base}/del/full/1"
    data = os.urandom(1000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/del/full"))
    if test.lacks:
        return test.outcome(code, out, err, lambda: (PASS, ""),
                            lambda: unchanged(test, {rel: data}))
    if code == 0:
        return FAIL, "succeeded, but the collection is not empty"
    if "non-empty collection" not in out + err:
        return FAIL, f"failed, but not for being non-empty: {last_line(err or out)}"
    if not test.everywhere("protected-a", rel, data):
        return FAIL, "an object in the collection was removed"
    return PASS, "refused: non-empty collection"


def delete_missing(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/del")
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/del/nope"))
    return (FAIL, why) if (why := test.expect_not_found(code, out, err)) else (PASS, "")


def delete_refused(test: Test, name: str, args: List[str]) -> Tuple[str, str]:
    """`delete` with args, which must be refused, by the client itself if
    args name no token."""
    rel = f"{test.base}/del/{name}"
    data = os.urandom(1000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *args, test.url("protected-a", rel))
    why = test.expect_refusal(code, out, err) if args else no_token_refusal(code, out, err)
    if why:
        return FAIL, why
    if not test.everywhere("protected-a", rel, data):
        return FAIL, "refused, but an origin no longer holds the object"
    return PASS, f"refused ({refused(out + err)})" if args else "the client refused: no token"


def delete_read_only(test: Test) -> Tuple[str, str]:
    return delete_refused(test, "read-only", ["-t", test.read_only])


def delete_no_token(test: Test) -> Tuple[str, str]:
    return delete_refused(test, "no-token", [])


def delete_public(test: Test) -> Tuple[str, str]:
    rel = f"{test.base}/del/public"
    data = os.urandom(1000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("public"),
                                      test.url("public", rel))
    if (why := test.expect_refusal(code, out, err)):
        return FAIL, why
    if not test.everywhere("public", rel, data):
        return FAIL, "refused, but an origin no longer holds the object"
    return PASS, f"refused ({refused(out + err)})"


#---------------------------------------------------------------------------
# copy

def copy_upload(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/copy")
    data = os.urandom(4321)
    source = test.local("copy-up", data)
    rel = f"{test.base}/copy/up"
    code, out, err = test.pelican_cmd("object", "copy", *test.token_args("protected-a"), source,
                                      test.url("protected-a", rel))

    def passed() -> Tuple[str, str]:
        if not test.somewhere("protected-a", rel, data):
            return FAIL, "no origin holds the upload"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: nowhere(test, [rel]))


def copy_download(test: Test) -> Tuple[str, str]:
    target = os.path.join(test.tmp, "copy-down")
    code, out, err = test.pelican_cmd("object", "copy", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree/b"), target)
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if common.sha256(target) != hashlib.sha256(test.tree["b"]).hexdigest():
        return FAIL, "the local file differs from the object"
    return PASS, ""


def copy_tpc(test: Test, name: str, source_ns: str, path: str,
             direct: bool = False) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/copy")
    rel = f"{test.base}/copy/{name}"
    args = ["object", "copy", "--dest-token", test.token_files["protected-a"]]
    if source_ns == "protected-a":
        args += ["--source-token", test.token_files["protected-a"]]
    if direct:
        args.append("--direct")
    code, out, err = test.pelican_cmd(*args, test.url(source_ns, f"{test.base}/tree/{path}"),
                                      test.url("protected-a", rel))

    def passed() -> Tuple[str, str]:
        if not test.somewhere("protected-a", rel, test.tree[path]):
            return FAIL, "no origin holds the copy"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: nowhere(test, [rel]))


def copy_tpc_protected_a(test: Test) -> Tuple[str, str]:
    return copy_tpc(test, "tpc", "protected-a", "b")


def copy_tpc_public(test: Test) -> Tuple[str, str]:
    return copy_tpc(test, "tpc-public", "public", "a")


def copy_tpc_direct(test: Test) -> Tuple[str, str]:
    return copy_tpc(test, "tpc-direct", "protected-a", "sub/deeper/d", direct=True)


def copy_to_public(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/copy")
    data = os.urandom(1234)
    source = test.local("copy-public", data)
    rel = f"{test.base}/copy/public"
    code, out, err = test.pelican_cmd("object", "copy", *test.token_args("public"), source,
                                      test.url("public", rel))
    if (why := test.expect_refusal(code, out, err)):
        return FAIL, why
    if not test.nowhere("public", rel):
        return FAIL, "refused, but an origin holds the upload"
    return PASS, f"refused ({refused(out + err)})"


#---------------------------------------------------------------------------
# sync (compares sizes only, and never deletes; see Pelican's
# docs/object-transfer-semantics.md for the layout: flat, like rsync)

SYNC_TREE = {"x": 100, "y": 2000, "s/z": 300}


def local_tree(test: Test, name: str) -> Tuple[str, Dict[str, bytes]]:
    files = {path: os.urandom(size) for path, size in SYNC_TREE.items()}
    for path, data in files.items():
        test.local(f"{name}/{path}", data)
    return os.path.join(test.tmp, name), files


def upload_lines(text: str) -> List[str]:
    return [line for line in text.splitlines() if "UPLOAD:" in line]


def sync_upload(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/sync")
    source, files = local_tree(test, "sync-up")
    remote = f"{test.base}/sync/up"
    code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"), source,
                                      test.url("protected-a", remote), env=test.sync_env)

    def passed() -> Tuple[str, str]:
        missing = [p for p, data in files.items()
                   if not test.somewhere("protected-a", f"{remote}/{p}", data)]
        if missing:
            return FAIL, f"no origin holds {', '.join(missing)}"
        return PASS, f"{len(files)} files"

    return test.outcome(code, out, err, passed,
                        lambda: nowhere(test, [f"{remote}/{p}" for p in files]))


def sync_unchanged(test: Test) -> Tuple[str, str]:
    source, files = local_tree(test, "sync-pre")
    remote = f"{test.base}/sync/pre"
    for path, data in files.items():
        test.seed(f"{remote}/{path}", data)
    code, out, err = test.pelican_cmd("object", "sync", "--dry-run", *test.token_args("protected-a"),
                                      source, test.url("protected-a", remote), env=test.sync_env)

    def passed() -> Tuple[str, str]:
        if upload_lines(out + err):
            return FAIL, f"would upload: {'; '.join(upload_lines(out + err))}"
        return PASS, "nothing to upload"

    return test.outcome(code, out, err, passed)


def sync_changed(test: Test) -> Tuple[str, str]:
    source, files = local_tree(test, "sync-changed")
    remote = f"{test.base}/sync/changed"
    for path, data in files.items():
        test.seed(f"{remote}/{path}", data)
    changed = os.urandom(len(files["x"]) + 50)
    test.local("sync-changed/x", changed)
    if test.lacks:
        code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"),
                                          source, test.url("protected-a", remote),
                                          env=test.sync_env)
        return test.outcome(code, out, err, lambda: (PASS, ""),
                            lambda: unchanged(test, {f"{remote}/x": files["x"]}))
    code, out, err = test.pelican_cmd("object", "sync", "--dry-run", *test.token_args("protected-a"),
                                      source, test.url("protected-a", remote), env=test.sync_env)
    if code != 0:
        return FAIL, f"dry run: exit {code}: {last_line(err)}"
    lines = upload_lines(out + err)
    if len(lines) != 1 or not re.search(r"/x\b", lines[0]):
        return FAIL, f"would upload {lines or 'nothing'}, not just x"
    code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"), source,
                                      test.url("protected-a", remote), env=test.sync_env)
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if not test.somewhere("protected-a", f"{remote}/x", changed):
        return FAIL, "no origin holds the changed file"
    return PASS, "uploaded only x"


def sync_download(test: Test) -> Tuple[str, str]:
    target = os.path.join(test.tmp, "sync-down")
    code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree"), target,
                                      env=test.sync_env)

    def passed() -> Tuple[str, str]:
        wrong = [p for p, data in test.tree.items()
                 if common.sha256(os.path.join(target, p)) != hashlib.sha256(data).hexdigest()]
        if wrong:
            return FAIL, f"missing or different locally: {', '.join(wrong)}"
        return PASS, f"{len(test.tree)} files"

    return test.outcome(code, out, err, passed)


def sync_tpc(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/sync")
    remote = f"{test.base}/sync/tpc"
    code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree"),
                                      test.url("protected-a", remote), env=test.sync_env)

    def passed() -> Tuple[str, str]:
        missing = [p for p, data in test.tree.items()
                   if not test.somewhere("protected-a", f"{remote}/{p}", data)]
        if missing:
            return FAIL, f"no origin holds {', '.join(missing)}"
        return PASS, f"{len(test.tree)} files"

    return test.outcome(code, out, err, passed,
                        lambda: nowhere(test, [f"{remote}/{p}" for p in test.tree]))


#---------------------------------------------------------------------------
# du (see Pelican's cmd/object_du.go)

# Each collection in the tree: its bytes, objects, and collections, all
# cumulative, by its path in the tree ("" for the tree itself).
DU = {"": (13300, 4, 2), "/sub": (7300, 2, 1), "/sub/deeper": (7000, 1, 0)}

# A line of `du --count`'s text: `<bytes>  <objects>  <collections>  <path>`.
DU_LINE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\S.*?)\s*")


def du_key(path: str, arg: str) -> str:
    """Where a row's path is in the tree that arg (a URL) names: "" for
    the tree itself, whose row repeats arg as given, and otherwise its
    path below it, e.g. /sub; path as printed if it is not in the tree."""
    if path.rstrip("/") == arg.rstrip("/"):
        return ""
    root, bare = normal_path(arg), normal_path(path)
    if bare == root:
        return ""
    return bare[len(root):] if bare.startswith(root + "/") else path


def du_text(text: str) -> List[Tuple[str, int, int, int]]:
    """The rows of `du --count`'s text output: each collection's path,
    bytes, objects, and collections. Lines of another form are left out."""
    rows = []
    for line in text.splitlines():
        m = DU_LINE.fullmatch(line)
        if m:
            rows.append((m.group(4), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return rows


def du_counts(rows: Iterable[Tuple[str, Any, Any, Any]],
              arg: str) -> Dict[str, Tuple[Any, Any, Any]]:
    """du's rows (path, bytes, objects, collections) by their place in the
    tree (see du_key()). Raises ValueError if two rows are of one place."""
    found: Dict[str, Tuple[Any, Any, Any]] = {}
    for path, *counts in rows:
        key = du_key(path, arg)
        if key in found:
            raise ValueError(f"two rows for {path}")
        found[key] = tuple(counts)
    return found


def du_run(test: Test, namespace: str, check: Callable[[list, str], Tuple[str, str]],
           *flags: str) -> Tuple[str, str]:
    """`du --json` of the tree, its rows judged by check(rows, the
    argument given)."""
    arg = test.url(namespace, f"{test.base}/tree")
    args = ["object", "du", "--json", *flags]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, arg)

    def passed() -> Tuple[str, str]:
        try:
            rows = json_out(out, list)
        except ValueError as e:
            return FAIL, f"unreadable output ({e}): {last_line(out)}"
        return check(rows, arg)

    return test.outcome(code, out, err, passed)


def du_tree(test: Test, namespace: str) -> Tuple[str, str]:
    """`du --json`, whose rows always have the counts, and then `du
    --count`, which adds them to the text output (and changes nothing
    else)."""
    def check(rows: list, arg: str) -> Tuple[str, str]:
        try:
            got = du_counts([(row.get("path", ""), row.get("bytes"), row.get("objects", 0),
                              row.get("collections", 0)) for row in rows], arg)
        except ValueError as e:
            return FAIL, f"--json: {e}"
        if got != DU:
            return FAIL, f"--json: got {got}, not {DU}"
        return PASS, ""

    status, note = du_run(test, namespace, check)
    if status != PASS or test.lacks:
        return status, note
    arg = test.url(namespace, f"{test.base}/tree")
    args = ["object", "du", "--count"]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, arg)
    if code != 0:
        return FAIL, f"--count: exit {code}: {last_line(err)}"
    try:
        got = du_counts(du_text(out), arg)
    except ValueError as e:
        return FAIL, f"--count: {e}"
    if got != DU:
        return FAIL, f"--count: got {got}, not {DU}: {last_line(out)}"
    return PASS, "13300 bytes, 4 objects, 2 collections, in JSON and in text"


def du_protected_a(test: Test) -> Tuple[str, str]:
    return du_tree(test, "protected-a")


def du_public(test: Test) -> Tuple[str, str]:
    return du_tree(test, "public")


def du_summarize(test: Test) -> Tuple[str, str]:
    def check(rows: list, arg: str) -> Tuple[str, str]:
        if len(rows) != 1 or rows[0].get("bytes") != 13300:
            return FAIL, f"got {rows}, not one row of 13300 bytes"
        return PASS, "13300 bytes"

    return du_run(test, "protected-a", check, "-s")


def du_no_token(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "du", test.url("protected-a", f"{test.base}/tree"))
    if (why := no_token_refusal(code, out, err)):
        return FAIL, why
    return PASS, "the client refused: no token"


RUN = {
    "ls-protected-a": ls_protected_a, "ls-public": ls_public, "ls-recursive": ls_recursive,
    "ls-missing": ls_missing, "ls-no-token": ls_no_token,
    "stat-protected-a": stat_protected_a, "stat-public": stat_public,
    "stat-collection": stat_collection, "stat-missing": stat_missing,
    "delete-object": delete_object, "delete-recursive": delete_recursive,
    "delete-nonempty": delete_nonempty, "delete-missing": delete_missing,
    "delete-read-only": delete_read_only, "delete-no-token": delete_no_token,
    "delete-public": delete_public,
    "copy-upload": copy_upload, "copy-download": copy_download, "copy-tpc": copy_tpc_protected_a,
    "copy-tpc-public": copy_tpc_public, "copy-tpc-direct": copy_tpc_direct,
    "copy-to-public": copy_to_public,
    "sync-upload": sync_upload, "sync-unchanged": sync_unchanged,
    "sync-changed": sync_changed, "sync-download": sync_download, "sync-tpc": sync_tpc,
    "du-protected-a": du_protected_a, "du-public": du_public, "du-summarize": du_summarize,
    "du-no-token": du_no_token,
}


def skip(fed: common.Federation) -> Optional[str]:
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    for origin in stores.unique(fed.origins):
        if not origin.pstore and not os.path.isdir(f"{origin.store}/data/cmd"):
            die(f"framework/var/{origin.store}/data/cmd is missing; run ./fed.sh init")
    # A pstore origin's store, which only it can change, is emptied
    # through it, with a token for each namespace with storage of its own.
    tokens = session.tokens() if any(o.pstore for o in fed.origins) else {}
    # Earlier runs' trees come out first.
    errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/cmd", tokens)
    if errors:
        die(f"could not remove earlier runs' objects: {common.first(errors)}")

    test = Test(session)
    print(f"Command test against {fed.url}, objects under /<namespace>/{test.base}/\n")
    try:
        test.seed_tree()
        for name in selected:
            if uses_public(name) and "public" not in fed.exports:
                results.add(name, SKIP, "this shape does not export /public")
                continue
            namespace, needs = needs_of(name)
            test.lacks = needs - fed.exports[namespace]
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            except Exception as e:
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # Some of the tree's collections are the origins' making, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/cmd", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {common.first(errors)}")
