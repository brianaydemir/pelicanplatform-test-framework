"""The client's commands beyond the transfers suite's: `pelican object
ls`, `stat`, `delete`, `copy`, `sync`, and `du`, in /public and
/protected-a; and `get` and `put` where Pelican's
docs/object-transfer-semantics.md says what each must do with what
already exists, a collection, a directory, or a tree (its rows, e.g. G1
or P4, are named where they are tested). Each is checked against the
origins' stores, or the local files.

The read-only commands run against a tree uploaded straight to every
origin, /<namespace>/data/cmd/<run>/tree/, so that they agree whichever
origin the director picks: in each namespace, the same paths and sizes,
but bytes of its own; `stat`'s modification time must be the file's.
The rest go through the director like any
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
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Optional, cast
from urllib.parse import urlsplit

from testlib import blocks, common, credentials, stores, transfers
from testlib.common import die
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ls-protected-a": "`ls -l --json` of /protected-a: names, sizes, and collections",
    "ls-public": "the same in /public, with no token",
    "ls-recursive": "`ls -r` lists every object in the tree, by its path in it",
    "ls-missing": "`ls` of a missing path: not found",
    "ls-no-token": "`ls` of /protected-a with no token: the client refuses (Error code 4010)",
    "stat-protected-a": "`stat --json --checksums ...`: size, modification time, and checksums",
    "stat-public": "the same in /public, with no token",
    "stat-collection": "`stat` of a collection says so",
    "stat-missing": "`stat` of a missing object: not found",
    "delete-object": "`delete` removes an object",
    "delete-recursive": "`delete -r` removes a collection and what it holds",
    "delete-nonempty": "`delete` of a non-empty collection without -r: refused, nothing removed",
    "delete-missing": "`delete` of a missing object: not found",
    "delete-read-only": "`delete` with a read-only token: refused, nothing removed",
    "delete-no-token": (
        "`delete` with no token: the client refuses (Error code 4010), nothing removed"
    ),
    "delete-public": "`delete` in /public, which takes no writes: refused, nothing removed",
    "get-existing-file": (
        "`get` over a larger local file leaves only the object; a failed one, the file"
    ),
    "get-collection": "`get` of a collection without -r: refused, and nothing written (G4)",
    "get-recursive": "`get -r` lays the tree out flat, in an existing directory or a new one",
    "put-exists": "with overwrites off, `put` to an existing object: refused, nothing changed",
    "put-overwrite": "with overwrites on, `put` of a smaller file over an object replaces it",
    "put-into-collection": "`put` of a file to a collection lands in it, by the file's name",
    "put-directory": "`put` of a directory without -r: refused, and nothing uploaded (P4)",
    "put-recursive": "`put -r` of a tree, empty file and all, lays it out flat (P5, P6)",
    "copy-upload": "`copy` of a local file to /protected-a",
    "copy-download": "`copy` of an object to a local file",
    "copy-tpc": "`copy` from /protected-a to /protected-a: a third-party copy",
    "copy-tpc-public": "`copy` from /public to /protected-a: a third-party copy",
    "copy-tpc-direct": "`copy --direct`: a third-party copy from an origin",
    "copy-to-public": "`copy` to /public, which takes no writes: refused",
    "sync-upload": "`sync` of a local tree to a new collection",
    "sync-unchanged": "`sync --dry-run` of a tree the origins already hold lists nothing",
    "sync-changed": "a local file whose size changed is the one `sync` uploads again",
    "sync-download": "`sync` of a collection to a local directory",
    "sync-tpc": "`sync` of a collection to another: third-party copies",
    "du-protected-a": (
        "`du --json`, and `du --count`'s text: bytes, objects, and collections"
        " per collection"
    ),
    "du-public": "the same in /public, with no token",
    "du-summarize": "`du -s` gives only the total",
    "du-no-token": "`du` of /protected-a with no token: the client refuses (Error code 4010)",
}

# The capabilities each scenario needs of its namespace (/public for
# ls-public and du-public, and otherwise /protected-a), beyond reading.
LISTS = frozenset({"Listings"})
WRITES = frozenset({"Writes"})
NEEDS: dict[str, frozenset[str]] = {
    "ls-protected-a": LISTS,
    "ls-public": LISTS,
    "ls-recursive": LISTS,
    "ls-missing": LISTS,
    "delete-object": WRITES,
    "delete-recursive": WRITES | LISTS,
    "delete-nonempty": WRITES,
    "delete-missing": WRITES,
    "get-collection": LISTS,
    "get-recursive": LISTS,
    "put-exists": WRITES,
    "put-overwrite": WRITES,
    "put-into-collection": WRITES | LISTS,
    "put-recursive": WRITES,
    "copy-upload": WRITES,
    "copy-tpc": WRITES,
    "copy-tpc-public": WRITES,
    "copy-tpc-direct": WRITES | {"DirectReads"},
    "sync-upload": WRITES | LISTS,
    "sync-unchanged": WRITES | LISTS,
    "sync-changed": WRITES | LISTS,
    "sync-download": LISTS,
    "sync-tpc": WRITES | LISTS,
    "du-protected-a": LISTS,
    "du-public": LISTS,
    "du-summarize": LISTS,
}


def needs_of(name: str) -> tuple[str, frozenset[str]]:
    """The namespace whose capabilities scenario name needs, and those
    capabilities."""
    namespace = "public" if name in ("ls-public", "du-public") else "protected-a"
    return namespace, NEEDS.get(name, frozenset())


def uses_public(name: str) -> bool:
    """Whether scenario name uses /public, and so skips where it is not
    exported: each that does is named *-public, e.g. copy-tpc-public,
    which copies from it."""
    return name.endswith("-public")


# The scenarios that need the origins' storage to delete an object.
DELETES = ("delete-object", "delete-recursive", "delete-nonempty", "delete-missing")

# The tree the read-only commands work on, under data/cmd/<run>/tree/.
TREE = {"a": 1000, "b": 5000, "sub/c": 300, "sub/deeper/d": 7000}


# --------------------------------------------------------------------------
# Checksums, as `stat --checksums` reports them: hex.


def digest_bytes(value: str) -> Optional[bytes]:
    """The digest a reported checksum stands for."""
    try:
        return binascii.unhexlify(value.strip())
    except binascii.Error:
        return None


def checksum_matches(kind: str, value: str, data: bytes) -> bool:
    """Whether a reported checksum of kind is data's."""
    got = digest_bytes(value)
    if got is None:
        return False
    if kind == "crc32c":
        return int.from_bytes(got, "big") == blocks.crc32c(data)
    return got == hashlib.md5(data, usedforsecurity=False).digest()


# How far `stat`'s ModTime may be from the object's file's modification
# time, in seconds. A time parsed in the wrong zone is hours off.
MODIFIED_SLACK = 60


def parse_rfc3339(value: str) -> Optional[datetime]:
    """The time an RFC 3339 string (as Go writes a time.Time in JSON)
    stands for; None if it is not one. Python 3.9 reads neither `Z` nor
    more than six fractional digits."""
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
    text = re.sub(r"[Zz]$", "+00:00", text)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


# --------------------------------------------------------------------------
# What a failed command said.

refused = transfers.refused


def not_found(text: str) -> bool:
    """Whether a command's output says that there is no such object."""
    return bool(re.search(r"Error code 5011|\b404\b|does not exist|not found", text, re.I))


def no_token_refusal(code: int, out: str, err: str, standalone: bool) -> Optional[str]:
    """Why a command run with no token was not refused by the client
    itself, for want of one (Error code 4010, the `client` refusal of
    FAILURE_RULES), or None. A standalone origin has no director to tell
    the client that a namespace takes a token, so there the client may
    ask the origin, which must refuse."""
    if code == 0:
        return "succeeded, but should have been refused: no token"
    if re.search(r"Error code 4010\b", out + err):
        return None
    if standalone and refused(out + err):
        return None
    return (
        f"failed, but the client did not refuse for want of a token (Error code 4010):"
        f" {last_line(err or out)}"
    )


class Test:
    """What every scenario needs: tokens, the stores, and the trees."""

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
        self.namespaces: list[str] = [
            ns for ns in ("public", "protected-a") if ns in session.fed.exports
        ]
        self.lacks: frozenset[str] = frozenset()
        self.token_files = {ns: session.token_file(ns) for ns in self.namespaces}
        self.tokens = {ns: session.token(ns) for ns in self.namespaces}
        self.read_only = session.token_file("protected-a", credentials.R)
        self.env = session.env
        self.sync_env = session.sync_env
        # Each namespace's tree, of TREE's paths and sizes, but bytes of
        # its own: on a pstore origin, /public's is /protected-a's, whose
        # storage it reads.
        own = {
            self.fed.storage_namespace(ns): {
                path: os.urandom(size) for path, size in TREE.items()
            }
            for ns in self.namespaces
        }
        self.trees = {ns: own[self.fed.storage_namespace(ns)] for ns in self.namespaces}

    # URLs, paths, and stores.

    def url(self, namespace: str, rel: str) -> str:
        """The federation URL of rel in namespace."""
        return f"{self.fed.url}/{namespace}/{rel}"

    def local(self, name: str, data: bytes) -> str:
        """Write data to local file name: its path."""
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def held(self, namespace: str, rel: str) -> list[Optional[str]]:
        """What each store holds at rel (see stores.held_anywhere)."""
        return stores.held_anywhere(self.origins, namespace, rel, self.tokens)

    def somewhere(self, namespace: str, rel: str, data: bytes) -> bool:
        """Whether some store holds data at rel in namespace."""
        return hashlib.sha256(data).hexdigest() in self.held(namespace, rel)

    def nowhere(self, namespace: str, rel: str) -> bool:
        """Whether no store holds anything at rel in namespace."""
        return all(h is None for h in self.held(namespace, rel))

    def everywhere(self, namespace: str, rel: str, data: bytes) -> bool:
        """Whether every store holds data at rel in namespace."""
        return all(h == hashlib.sha256(data).hexdigest() for h in self.held(namespace, rel))

    def gone_somewhere(self, namespace: str, rel: str) -> bool:
        """Whether some store holds nothing at rel in namespace."""
        return any(h is None for h in self.held(namespace, rel))

    def seed(self, namespace: str, rel: str, data: bytes) -> None:
        """Put data at rel in namespace's storage at every origin (see
        stores.seed()): on a pstore origin, /public's is /protected-a's."""
        target = self.fed.storage_namespace(namespace)
        token = self.tokens[target]
        parent = rel.rsplit("/", 1)[0]
        errors = stores.make_dirs(self.origins, target, parent, token)
        errors += stores.seed_everywhere(
            self.origins, target, rel, data, token, self.fed.exports[target]
        )
        if errors:
            die("could not put the test objects in place: " + "; ".join(errors))

    def make_dirs(self, rel: str) -> None:
        """Make collection rel in /protected-a at every origin."""
        errors = stores.make_dirs(self.origins, "protected-a", rel, self.tokens["protected-a"])
        if errors:
            die("could not make the test collections: " + "; ".join(errors))

    def seed_tree(self) -> None:
        """Put each namespace's tree in place at every origin."""
        for namespace in dict.fromkeys(
            self.fed.storage_namespace(ns) for ns in self.namespaces
        ):
            for path, data in self.trees[namespace].items():
                self.seed(namespace, f"{self.base}/tree/{path}", data)

    # Running the client.

    def pelican_cmd(
        self, *args: str, env: Optional[dict[str, str]] = None, cwd: Optional[str] = None
    ) -> tuple[int, str, str]:
        """Run `pelican <args>`: its exit status, stdout, and stderr."""
        return self.session.pelican_cmd(*args, env=env or self.env, cwd=cwd or self.tmp)

    def token_args(self, namespace: str) -> list[str]:
        """The client's arguments for namespace's token."""
        return ["-t", self.token_files[namespace]]

    def expect_refusal(self, code: int, out: str, err: str) -> Optional[str]:
        """Why a command that should have been refused wasn't, or None."""
        if code == 0:
            return "succeeded, but should have been refused"
        if refused(out + err) is None:
            return f"failed, but not by refusal: {last_line(err or out)}"
        return None

    def expect_not_found(self, code: int, out: str, err: str) -> Optional[str]:
        """Why a command that should have found nothing did, or None."""
        if self.lacks:
            return self.expect_refusal(code, out, err)
        if code == 0:
            return "succeeded, but should have failed: not found"
        if not not_found(out + err):
            return f"failed, but not with not found: {last_line(err or out)}"
        return None

    def outcome(
        self,
        code: int,
        out: str,
        err: str,
        passed: Callable[[], tuple[str, str]],
        untouched: Callable[[], Optional[str]] = lambda: None,
    ) -> tuple[str, str]:
        """A scenario's verdict on its command: passed() if it succeeded as
        it should. Where the namespace lacks a capability that the command
        needs (self.lacks), it must instead have been refused, and
        untouched() must find nothing changed."""
        if self.lacks:
            why = self.expect_refusal(code, out, err) or untouched()
            if why:
                return FAIL, why
            return PASS, f"refused ({refused(out + err)}): no {', '.join(sorted(self.lacks))}"
        if code != 0:
            return FAIL, f"exit {code}: {last_line(err)}"
        return passed()


# How errors name a JSON value's type, by its Python type's name.
JSON_TYPES = {
    "dict": "an object",
    "list": "an array",
    "str": "a string",
    "int": "a number",
    "float": "a number",
    "bool": "a boolean",
    "NoneType": "null",
}


def last_json(text: str) -> Any:
    """The JSON a command printed on stdout: its last line that parses.
    Raises ValueError if none does."""
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("no JSON in the output")


def json_type(value: Any) -> str:
    """How a message names value's JSON type."""
    name = type(value).__name__
    return JSON_TYPES.get(name, name)


def json_object(text: str) -> dict[str, Any]:
    """last_json(), which must be an object. Raises ValueError if it
    isn't."""
    value = last_json(text)
    if not isinstance(value, dict):
        raise ValueError(f"the JSON is {json_type(value)}, not an object")
    return cast(dict[str, Any], value)


def json_objects(text: str) -> list[dict[str, Any]]:
    """last_json(), which must be an array of objects. Raises ValueError
    if it isn't."""
    value = last_json(text)
    items = common.as_array(value)
    if items is None:
        raise ValueError(f"the JSON is {json_type(value)}, not an array")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("the JSON array holds more than objects")
    return cast(list[dict[str, Any]], items)


def basename(name: str) -> str:
    """The last component of a path that the client printed."""
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
    return path[len(root) + 1 :] if path.startswith(root + "/") else name


def tree_objects(entries: Iterable[dict[str, Any]], root: str) -> Counter[tuple[str, Any]]:
    """How often `ls -l --json` listed each object, as (path in the tree
    at root, size), so that nesting and duplicates count."""
    return Counter(
        (tree_rel(e["Name"], root), e.get("Size")) for e in entries if not e.get("IsCollection")
    )


def unchanged(test: Test, objects: dict[str, bytes]) -> Optional[str]:
    """Why every origin does not still hold objects (bytes by rel in
    /protected-a), or None."""
    changed = [
        rel for rel, data in objects.items() if not test.everywhere("protected-a", rel, data)
    ]
    if changed:
        return f"refused, but an origin no longer holds {', '.join(changed)}"
    return None


def nowhere(test: Test, rels: list[str]) -> Optional[str]:
    """Why some origin holds one of rels in /protected-a, or None."""
    held = [rel for rel in rels if not test.nowhere("protected-a", rel)]
    if held:
        return f"refused, but an origin holds {', '.join(held)}"
    return None


# --------------------------------------------------------------------------
# ls


def ls_tree(test: Test, namespace: str) -> tuple[str, str]:
    """`ls -l --json` of the tree in namespace."""
    args = ["object", "ls", "-l", "--json"]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree"))

    def passed() -> tuple[str, str]:
        try:
            entries = {basename(e["Name"]): e for e in json_objects(out)}
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


def ls_protected_a(test: Test) -> tuple[str, str]:
    """ls_tree() in /protected-a."""
    return ls_tree(test, "protected-a")


def ls_public(test: Test) -> tuple[str, str]:
    """ls_tree() in /public, with no token."""
    return ls_tree(test, "public")


def ls_recursive(test: Test) -> tuple[str, str]:
    """`ls -r` lists every object in the tree, by its path in it."""
    url = test.url("protected-a", f"{test.base}/tree")
    code, out, err = test.pelican_cmd(
        "object", "ls", "-r", "-l", "--json", *test.token_args("protected-a"), url
    )

    def passed() -> tuple[str, str]:
        try:
            objects = tree_objects(json_objects(out), normal_path(url))
        except (ValueError, KeyError, TypeError) as e:
            return FAIL, f"unreadable output ({e}): {last_line(out)}"
        want = Counter(TREE.items())
        if objects != want:
            return FAIL, f"listed {sorted(objects.elements())}, not {sorted(want.elements())}"
        return PASS, f"{sum(objects.values())} objects"

    return test.outcome(code, out, err, passed)


def ls_missing(test: Test) -> tuple[str, str]:
    """`ls` of a missing path: not found."""
    code, out, err = test.pelican_cmd(
        "object",
        "ls",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/nope"),
    )
    why = test.expect_not_found(code, out, err)
    if why:
        return FAIL, why
    return PASS, ""


def ls_no_token(test: Test) -> tuple[str, str]:
    """`ls` of /protected-a with no token: refused."""
    code, out, err = test.pelican_cmd(
        "object", "ls", test.url("protected-a", f"{test.base}/tree")
    )
    why = no_token_refusal(code, out, err, test.fed.standalone)
    if why:
        return FAIL, why
    return PASS, f"refused ({refused(out + err)}): no token"


# --------------------------------------------------------------------------
# stat


def modified_problem(test: Test, namespace: str, info: dict[str, Any]) -> Optional[str]:
    """Why `stat`'s ModTime is not when the object's file was written in
    the stores, if they are on disk and it is not. The origins and the
    tests keep different time zones (TZ_STORAGE), so a time parsed in the
    wrong one shows."""
    when = parse_rfc3339(str(info.get("ModTime", "")))
    if when is None:
        return f"ModTime {info.get('ModTime')!r} is not a time"
    disk = [o for o in stores.unique(test.origins) if not o.pstore]
    if not disk:
        return None
    rel = f"{test.base}/tree/b"
    written = min(os.stat(f"{o.store_of(namespace)}/{rel}").st_mtime for o in disk)
    off = when.timestamp() - written
    if abs(off) > MODIFIED_SLACK:
        shown = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(written))
        return f"ModTime is {off:+.0f}s from the file's modification time, {shown}"
    return None


def stat_object(test: Test, namespace: str) -> tuple[str, str]:
    """`stat` of an object: its size, when it was written, and its
    checksums, those the origins' storage can report."""
    data = test.trees[namespace]["b"]
    kinds, no_digests = stores.digests(test.fed)
    args = ["object", "stat", "--json"]
    for kind in kinds:
        args += ["--checksums", kind]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree/b"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        info = json_object(out)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    if info.get("Size") != len(data) or info.get("IsCollection"):
        return FAIL, f"size {info.get('Size')}, collection {info.get('IsCollection')}"
    if why := modified_problem(test, namespace, info):
        return FAIL, why
    reported = common.as_object(info.get("checksums")) or {}
    sums = {k.lower(): str(v) for k, v in reported.items()}
    notes = ["size and ModTime match"]
    for kind in kinds:
        if kind not in sums:
            return FAIL, f"no {kind} checksum reported"
        if not checksum_matches(kind, sums[kind], data):
            return FAIL, f"{kind} {sums[kind]} does not match the object"
        notes.append(f"{kind} matches")
    if no_digests:
        notes.append(no_digests)
    return PASS, "; ".join(notes)


def stat_protected_a(test: Test) -> tuple[str, str]:
    """stat_object() in /protected-a."""
    return stat_object(test, "protected-a")


def stat_public(test: Test) -> tuple[str, str]:
    """stat_object() in /public, with no token."""
    return stat_object(test, "public")


def stat_collection(test: Test) -> tuple[str, str]:
    """`stat` of a collection says so."""
    code, out, err = test.pelican_cmd(
        "object",
        "stat",
        "--json",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/sub"),
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        info = json_object(out)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    if not info.get("IsCollection"):
        return FAIL, "not reported as a collection"
    return PASS, ""


def stat_missing(test: Test) -> tuple[str, str]:
    """`stat` of a missing object: not found."""
    code, out, err = test.pelican_cmd(
        "object",
        "stat",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/nope"),
    )
    why = test.expect_not_found(code, out, err)
    if why:
        return FAIL, why
    return PASS, ""


# --------------------------------------------------------------------------
# delete (a hidden command, which warns that it is)


def delete_object(test: Test) -> tuple[str, str]:
    """`delete` removes an object."""
    rel = f"{test.base}/del/object"
    data = os.urandom(2000)
    test.seed("protected-a", rel, data)
    code, out, err = test.pelican_cmd(
        "object", "delete", *test.token_args("protected-a"), test.url("protected-a", rel)
    )

    def passed() -> tuple[str, str]:
        if not test.gone_somewhere("protected-a", rel):
            return FAIL, "every origin still holds the object"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: unchanged(test, {rel: data}))


def delete_recursive(test: Test) -> tuple[str, str]:
    """`delete -r` removes a collection and what it holds."""
    rels = [f"{test.base}/del/tree/1", f"{test.base}/del/tree/sub/2"]
    data = {rel: os.urandom(1000) for rel in rels}
    for rel in rels:
        test.seed("protected-a", rel, data[rel])
    code, out, err = test.pelican_cmd(
        "object",
        "delete",
        "-r",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/del/tree"),
    )

    def passed() -> tuple[str, str]:
        held = [test.held("protected-a", rel) for rel in rels]
        # Some origin (the one the director chose) must hold none of them.
        if not any(all(h[i] is None for h in held) for i in range(len(held[0]))):
            return FAIL, "no origin is rid of the whole collection"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: unchanged(test, data))


def delete_nonempty(test: Test) -> tuple[str, str]:
    """`delete` of a non-empty collection without -r: refused."""
    rel = f"{test.base}/del/full/1"
    data = os.urandom(1000)
    test.seed("protected-a", rel, data)
    code, out, err = test.pelican_cmd(
        "object",
        "delete",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/del/full"),
    )
    if test.lacks:
        return test.outcome(
            code, out, err, lambda: (PASS, ""), lambda: unchanged(test, {rel: data})
        )
    if code == 0:
        return FAIL, "succeeded, but the collection is not empty"
    if "non-empty collection" not in out + err:
        return FAIL, f"failed, but not for being non-empty: {last_line(err or out)}"
    if not test.everywhere("protected-a", rel, data):
        return FAIL, "an object in the collection was removed"
    return PASS, "refused: non-empty collection"


def delete_missing(test: Test) -> tuple[str, str]:
    """`delete` of a missing object: not found."""
    test.make_dirs(f"{test.base}/del")
    code, out, err = test.pelican_cmd(
        "object",
        "delete",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/del/nope"),
    )
    why = test.expect_not_found(code, out, err)
    if why:
        return FAIL, why
    return PASS, ""


def delete_refused(test: Test, name: str, args: list[str]) -> tuple[str, str]:
    """`delete` with args, which must be refused, by the client itself if
    args name no token."""
    rel = f"{test.base}/del/{name}"
    data = os.urandom(1000)
    test.seed("protected-a", rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *args, test.url("protected-a", rel))
    if args:
        why = test.expect_refusal(code, out, err)
    else:
        why = no_token_refusal(code, out, err, test.fed.standalone)
    if why:
        return FAIL, why
    if not test.everywhere("protected-a", rel, data):
        return FAIL, "refused, but an origin no longer holds the object"
    return PASS, f"refused ({refused(out + err)})"


def delete_read_only(test: Test) -> tuple[str, str]:
    """`delete` with a read-only token: refused."""
    return delete_refused(test, "read-only", ["-t", test.read_only])


def delete_no_token(test: Test) -> tuple[str, str]:
    """`delete` with no token: refused."""
    return delete_refused(test, "no-token", [])


def delete_public(test: Test) -> tuple[str, str]:
    """`delete` in /public, which takes no writes: refused."""
    rel = f"{test.base}/del/public"
    data = os.urandom(1000)
    test.seed("public", rel, data)
    code, out, err = test.pelican_cmd(
        "object", "delete", *test.token_args("public"), test.url("public", rel)
    )
    if why := test.expect_refusal(code, out, err):
        return FAIL, why
    if not test.everywhere("public", rel, data):
        return FAIL, "refused, but an origin no longer holds the object"
    return PASS, f"refused ({refused(out + err)})"


# --------------------------------------------------------------------------
# get and put (rows G1 to P6 of Pelican's docs/object-transfer-semantics.md)

# The local tree that put-recursive uploads, with an empty file.
PUT_TREE = {"e": 0, "f": 3000, "d/g": 100, "d/h/i": 2500}


def local_files(directory: str) -> dict[str, str]:
    """The SHA-256 of every file under directory, by its path there."""
    found: dict[str, str] = {}
    for root, _, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            found[os.path.relpath(path, directory)] = common.sha256(path) or ""
    return found


def temporary_files(path: str) -> list[str]:
    """What the client leaves of a download to path: its temporary files,
    .<name>.*, beside it."""
    directory, name = os.path.split(path)
    return sorted(n for n in os.listdir(directory) if n.startswith(f".{name}."))


def get_existing_file(test: Test) -> tuple[str, str]:
    """`get` onto a local file three times the object's size: the file
    must then hold the object alone (G1). Then a `get` of a missing
    object onto it must fail, and leave it as it was. Neither may leave a
    temporary file."""
    data = test.trees["protected-a"]["a"]
    target = test.local("existing", os.urandom(3 * len(data)))
    code, out, err = test.pelican_cmd(
        "object",
        "get",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/a"),
        target,
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    if common.sha256(target) != common.digest(data):
        return FAIL, f"the file holds {os.path.getsize(target)} bytes, not the object alone"
    code, out, err = test.pelican_cmd(
        "object",
        "get",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/nope"),
        target,
    )
    if code == 0:
        return FAIL, "a get of a missing object succeeded"
    if not not_found(out + err):
        return (
            FAIL,
            f"a get of a missing object failed, but not with not found: {last_line(err)}",
        )
    if common.sha256(target) != common.digest(data):
        return FAIL, "a get of a missing object changed the file"
    left = temporary_files(target)
    if left:
        return FAIL, f"left {', '.join(left)}"
    return PASS, "replaced, then left alone"


def get_collection(test: Test) -> tuple[str, str]:
    """`get` of a collection without -r into an existing directory: the
    client must refuse it, rather than write the collection's listing
    into a file (G4). Where the namespace cannot be listed, the client
    cannot tell, and must fail anyway. Either way, nothing may be written
    in the directory."""
    target = os.path.join(test.tmp, "get-collection")
    os.makedirs(target)
    code, out, err = test.pelican_cmd(
        "object",
        "get",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/sub"),
        target,
    )
    if code == 0:
        return FAIL, "succeeded, though the source is a collection and -r was not given"
    if os.listdir(target):
        return FAIL, f"failed, but wrote {', '.join(sorted(os.listdir(target)))}"
    if test.lacks:
        return PASS, f"failed: no {', '.join(sorted(test.lacks))}"
    if "is a collection" not in out + err:
        return FAIL, f"failed, but not for being a collection: {last_line(err or out)}"
    return PASS, "refused: a collection"


def get_recursive(test: Test) -> tuple[str, str]:
    """`get -r` of the tree, into an existing directory (G5) and into a
    path that does not exist (G6): either way, the tree's objects land
    flat under it, as the tree holds them, and nothing else. Where the
    namespace cannot be listed, it must fail, and write nothing."""
    want = {path: common.digest(data) for path, data in test.trees["protected-a"].items()}
    for label, exists in (("an existing directory", True), ("a new one", False)):
        target = os.path.join(test.tmp, f"get-recursive-{'old' if exists else 'new'}")
        if exists:
            os.makedirs(target)
        code, out, err = test.pelican_cmd(
            "object",
            "get",
            "-r",
            *test.token_args("protected-a"),
            test.url("protected-a", f"{test.base}/tree"),
            target,
        )
        if test.lacks:
            if code == 0:
                return FAIL, f"into {label}: succeeded, though the tree cannot be listed"
            if os.path.isdir(target) and local_files(target):
                return FAIL, f"into {label}: failed, but wrote {sorted(local_files(target))}"
            continue
        if code != 0:
            return FAIL, f"into {label}: exit {code}: {last_line(err or out)}"
        got = local_files(target)
        if set(got) != set(want):
            return FAIL, f"into {label}: got {sorted(got)}, not {sorted(want)}"
        wrong = sorted(p for p in want if got[p] != want[p])
        if wrong:
            return FAIL, f"into {label}: {', '.join(wrong)} differ from the objects"
    if test.lacks:
        return PASS, f"failed: no {', '.join(sorted(test.lacks))}"
    return PASS, f"{len(want)} objects, flat, both ways"


def put_exists(test: Test) -> tuple[str, str]:
    """With overwrites off, as by default, `put` to an existing object:
    the client must refuse, and leave it intact (P2)."""
    rel = f"{test.base}/put/exists"
    old = os.urandom(2000)
    test.seed("protected-a", rel, old)
    source = test.local("put-exists", os.urandom(1000))
    code, out, err = test.pelican_cmd(
        "object",
        "put",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", rel),
        env=test.sync_env,
    )
    if code == 0:
        return FAIL, "succeeded, though the object exists and overwrites are off"
    if not test.lacks and "already exists" not in out + err:
        return FAIL, f"failed, but not because the object exists: {last_line(err or out)}"
    why = unchanged(test, {rel: old})
    if why:
        return FAIL, why
    return PASS, "refused: it already exists"


def put_overwrite(test: Test) -> tuple[str, str]:
    """With overwrites on, `put` of a file a fifth an object's size over
    it: the origin that takes it must then hold the new bytes alone."""
    rel = f"{test.base}/put/overwrite"
    old, new = os.urandom(5000), os.urandom(1000)
    test.seed("protected-a", rel, old)
    source = test.local("put-overwrite", new)
    code, out, err = test.pelican_cmd(
        "object", "put", *test.token_args("protected-a"), source, test.url("protected-a", rel)
    )

    def passed() -> tuple[str, str]:
        if not test.somewhere("protected-a", rel, new):
            return FAIL, "no origin holds the new bytes alone"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: unchanged(test, {rel: old}))


def put_into_collection(test: Test) -> tuple[str, str]:
    """`put` of a file to an existing collection: it lands in the
    collection, named as the file is (P3-cli)."""
    collection = f"{test.base}/put/into"
    test.make_dirs(collection)
    data = os.urandom(1500)
    source = test.local("into-me", data)
    code, out, err = test.pelican_cmd(
        "object",
        "put",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", collection),
    )
    rel = f"{collection}/into-me"

    def passed() -> tuple[str, str]:
        if not test.somewhere("protected-a", rel, data):
            return FAIL, f"no origin holds {rel}"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: nowhere(test, [rel]))


def put_directory(test: Test) -> tuple[str, str]:
    """`put` of a local directory without -r: the client must refuse,
    and upload nothing (P4)."""
    test.make_dirs(f"{test.base}/put")
    source, files = local_tree(test, "put-directory")
    remote = f"{test.base}/put/directory"
    code, out, err = test.pelican_cmd(
        "object",
        "put",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", remote),
    )
    if code == 0:
        return FAIL, "succeeded, though the source is a directory and -r was not given"
    if "is a directory" not in out + err:
        return FAIL, f"failed, but not for being a directory: {last_line(err or out)}"
    why = nowhere(test, [f"{remote}/{p}" for p in files])
    if why:
        return FAIL, why
    return PASS, "refused: a directory"


def put_recursive(test: Test) -> tuple[str, str]:
    """`put -r` of a local tree with an empty file in it, to a collection
    that does not exist (P5), and into one that does (P6): either way,
    its files land flat under it, as the tree holds them, and not under
    the local directory's name."""
    test.make_dirs(f"{test.base}/put")
    files = {path: os.urandom(size) for path, size in PUT_TREE.items()}
    for path, data in files.items():
        test.local(f"put-recursive/{path}", data)
    source = os.path.join(test.tmp, "put-recursive")
    for label, exists in (("a new collection", False), ("an existing one", True)):
        remote = f"{test.base}/put/recursive-{'old' if exists else 'new'}"
        if exists:
            test.make_dirs(remote)
        code, out, err = test.pelican_cmd(
            "object",
            "put",
            "-r",
            *test.token_args("protected-a"),
            source,
            test.url("protected-a", remote),
        )
        rels = [f"{remote}/{p}" for p in files]
        if test.lacks:
            return test.outcome(code, out, err, lambda: (PASS, ""), lambda: nowhere(test, rels))
        if code != 0:
            return FAIL, f"to {label}: exit {code}: {last_line(err or out)}"
        missing = [
            p
            for p, data in files.items()
            if not test.somewhere("protected-a", f"{remote}/{p}", data)
        ]
        if missing:
            nested = [f"{remote}/put-recursive/{p}" for p in missing]
            if nowhere(test, nested):
                return FAIL, f"to {label}: nested under put-recursive/, not flat"
            return FAIL, f"to {label}: no origin holds {', '.join(missing)}"
    return PASS, f"{len(files)} files, flat, both ways"


# --------------------------------------------------------------------------
# copy


def copy_upload(test: Test) -> tuple[str, str]:
    """`copy` of a local file to /protected-a."""
    test.make_dirs(f"{test.base}/copy")
    data = os.urandom(4321)
    source = test.local("copy-up", data)
    rel = f"{test.base}/copy/up"
    code, out, err = test.pelican_cmd(
        "object", "copy", *test.token_args("protected-a"), source, test.url("protected-a", rel)
    )

    def passed() -> tuple[str, str]:
        if not test.somewhere("protected-a", rel, data):
            return FAIL, "no origin holds the upload"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: nowhere(test, [rel]))


def copy_download(test: Test) -> tuple[str, str]:
    """`copy` of an object to a local file."""
    target = os.path.join(test.tmp, "copy-down")
    code, out, err = test.pelican_cmd(
        "object",
        "copy",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree/b"),
        target,
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    if common.sha256(target) != hashlib.sha256(test.trees["protected-a"]["b"]).hexdigest():
        return FAIL, "the local file differs from the object"
    return PASS, ""


def copy_tpc(
    test: Test, name: str, source_ns: str, path: str, direct: bool = False
) -> tuple[str, str]:
    """`copy` from source_ns to /protected-a: a third-party copy."""
    test.make_dirs(f"{test.base}/copy")
    rel = f"{test.base}/copy/{name}"
    args = ["object", "copy", "--dest-token", test.token_files["protected-a"]]
    if source_ns == "protected-a":
        args += ["--source-token", test.token_files["protected-a"]]
    if direct:
        args.append("--direct")
    code, out, err = test.pelican_cmd(
        *args, test.url(source_ns, f"{test.base}/tree/{path}"), test.url("protected-a", rel)
    )

    def passed() -> tuple[str, str]:
        if not test.somewhere("protected-a", rel, test.trees[source_ns][path]):
            return FAIL, "no origin holds the copy"
        return PASS, ""

    return test.outcome(code, out, err, passed, lambda: nowhere(test, [rel]))


def copy_tpc_protected_a(test: Test) -> tuple[str, str]:
    """copy_tpc() from /protected-a."""
    return copy_tpc(test, "tpc", "protected-a", "b")


def copy_tpc_public(test: Test) -> tuple[str, str]:
    """copy_tpc() from /public."""
    return copy_tpc(test, "tpc-public", "public", "a")


def copy_tpc_direct(test: Test) -> tuple[str, str]:
    """copy_tpc() with --direct, from an origin."""
    return copy_tpc(test, "tpc-direct", "protected-a", "sub/deeper/d", direct=True)


def copy_to_public(test: Test) -> tuple[str, str]:
    """`copy` to /public, which takes no writes: refused."""
    test.make_dirs(f"{test.base}/copy")
    data = os.urandom(1234)
    source = test.local("copy-public", data)
    rel = f"{test.base}/copy/public"
    code, out, err = test.pelican_cmd(
        "object", "copy", *test.token_args("public"), source, test.url("public", rel)
    )
    if why := test.expect_refusal(code, out, err):
        return FAIL, why
    if not test.nowhere("public", rel):
        return FAIL, "refused, but an origin holds the upload"
    return PASS, f"refused ({refused(out + err)})"


# --------------------------------------------------------------------------
# sync (compares sizes only, and never deletes; see Pelican's
# docs/object-transfer-semantics.md for the layout: flat, like rsync)

SYNC_TREE = {"x": 100, "y": 2000, "s/z": 300}


def local_tree(test: Test, name: str) -> tuple[str, dict[str, bytes]]:
    """Write SYNC_TREE under local directory name: its path, and its files'
    bytes."""
    files = {path: os.urandom(size) for path, size in SYNC_TREE.items()}
    for path, data in files.items():
        test.local(f"{name}/{path}", data)
    return os.path.join(test.tmp, name), files


def upload_lines(text: str) -> list[str]:
    """The lines of `sync --dry-run`'s output that name an upload."""
    return [line for line in text.splitlines() if "UPLOAD:" in line]


def sync_upload(test: Test) -> tuple[str, str]:
    """`sync` of a local tree to a new collection."""
    test.make_dirs(f"{test.base}/sync")
    source, files = local_tree(test, "sync-up")
    remote = f"{test.base}/sync/up"
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", remote),
        env=test.sync_env,
    )

    def passed() -> tuple[str, str]:
        missing = [
            p
            for p, data in files.items()
            if not test.somewhere("protected-a", f"{remote}/{p}", data)
        ]
        if missing:
            return FAIL, f"no origin holds {', '.join(missing)}"
        return PASS, f"{len(files)} files"

    return test.outcome(
        code, out, err, passed, lambda: nowhere(test, [f"{remote}/{p}" for p in files])
    )


def sync_unchanged(test: Test) -> tuple[str, str]:
    """`sync --dry-run` of a tree the origins already hold lists nothing."""
    source, files = local_tree(test, "sync-pre")
    remote = f"{test.base}/sync/pre"
    for path, data in files.items():
        test.seed("protected-a", f"{remote}/{path}", data)
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        "--dry-run",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", remote),
        env=test.sync_env,
    )

    def passed() -> tuple[str, str]:
        if upload_lines(out + err):
            return FAIL, f"would upload: {'; '.join(upload_lines(out + err))}"
        return PASS, "nothing to upload"

    return test.outcome(code, out, err, passed)


def sync_changed(test: Test) -> tuple[str, str]:
    """A local file whose size changed is the one `sync` uploads again."""
    source, files = local_tree(test, "sync-changed")
    remote = f"{test.base}/sync/changed"
    for path, data in files.items():
        test.seed("protected-a", f"{remote}/{path}", data)
    changed = os.urandom(len(files["x"]) + 50)
    test.local("sync-changed/x", changed)
    if test.lacks:
        code, out, err = test.pelican_cmd(
            "object",
            "sync",
            *test.token_args("protected-a"),
            source,
            test.url("protected-a", remote),
            env=test.sync_env,
        )
        return test.outcome(
            code,
            out,
            err,
            lambda: (PASS, ""),
            lambda: unchanged(test, {f"{remote}/x": files["x"]}),
        )
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        "--dry-run",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", remote),
        env=test.sync_env,
    )
    if code != 0:
        return FAIL, f"dry run: exit {code}: {last_line(err)}"
    lines = upload_lines(out + err)
    if len(lines) != 1 or not re.search(r"/x\b", lines[0]):
        return FAIL, f"would upload {lines or 'nothing'}, not just x"
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        *test.token_args("protected-a"),
        source,
        test.url("protected-a", remote),
        env=test.sync_env,
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if not test.somewhere("protected-a", f"{remote}/x", changed):
        return FAIL, "no origin holds the changed file"
    return PASS, "uploaded only x"


def sync_download(test: Test) -> tuple[str, str]:
    """`sync` of a collection to a local directory."""
    target = os.path.join(test.tmp, "sync-down")
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree"),
        target,
        env=test.sync_env,
    )

    def passed() -> tuple[str, str]:
        wrong = [
            p
            for p, data in test.trees["protected-a"].items()
            if common.sha256(os.path.join(target, p)) != hashlib.sha256(data).hexdigest()
        ]
        if wrong:
            return FAIL, f"missing or different locally: {', '.join(wrong)}"
        return PASS, f"{len(test.trees['protected-a'])} files"

    return test.outcome(code, out, err, passed)


def sync_tpc(test: Test) -> tuple[str, str]:
    """`sync` of a collection to another: third-party copies."""
    test.make_dirs(f"{test.base}/sync")
    remote = f"{test.base}/sync/tpc"
    code, out, err = test.pelican_cmd(
        "object",
        "sync",
        *test.token_args("protected-a"),
        test.url("protected-a", f"{test.base}/tree"),
        test.url("protected-a", remote),
        env=test.sync_env,
    )

    def passed() -> tuple[str, str]:
        missing = [
            p
            for p, data in test.trees["protected-a"].items()
            if not test.somewhere("protected-a", f"{remote}/{p}", data)
        ]
        if missing:
            return FAIL, f"no origin holds {', '.join(missing)}"
        return PASS, f"{len(test.trees['protected-a'])} files"

    return test.outcome(
        code, out, err, passed, lambda: nowhere(test, [f"{remote}/{p}" for p in TREE])
    )


# --------------------------------------------------------------------------
# du (see Pelican's cmd/object_du.go)

# Each collection in the tree: its bytes, objects, and collections, all
# cumulative, by its path in the tree ("" for the tree itself).
DU = {"": (13300, 4, 2), "/sub": (7300, 2, 1), "/sub/deeper": (7000, 1, 0)}

# A line of `du --count`'s text: `<bytes>  <objects>  <collections>
# <path>`.
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
    return bare[len(root) :] if bare.startswith(root + "/") else path


def du_text(text: str) -> list[tuple[str, int, int, int]]:
    """The rows of `du --count`'s text output: each collection's path,
    bytes, objects, and collections. Lines of another form are left out."""
    rows: list[tuple[str, int, int, int]] = []
    for line in text.splitlines():
        m = DU_LINE.fullmatch(line)
        if m:
            rows.append((m.group(4), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return rows


def du_counts(
    rows: Iterable[tuple[str, Any, Any, Any]], arg: str
) -> dict[str, tuple[Any, Any, Any]]:
    """du's rows (path, bytes, objects, collections) by their place in the
    tree (see du_key()). Raises ValueError if two rows are of one place."""
    found: dict[str, tuple[Any, Any, Any]] = {}
    for path, *counts in rows:
        key = du_key(path, arg)
        if key in found:
            raise ValueError(f"two rows for {path}")
        found[key] = tuple(counts)
    return found


def du_run(
    test: Test,
    namespace: str,
    check: Callable[[list[dict[str, Any]], str], tuple[str, str]],
    *flags: str,
) -> tuple[str, str]:
    """`du --json` of the tree, its rows judged by check(rows, the
    argument given)."""
    arg = test.url(namespace, f"{test.base}/tree")
    args = ["object", "du", "--json", *flags]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, arg)

    def passed() -> tuple[str, str]:
        try:
            rows = json_objects(out)
        except ValueError as e:
            return FAIL, f"unreadable output ({e}): {last_line(out)}"
        return check(rows, arg)

    return test.outcome(code, out, err, passed)


def du_tree(test: Test, namespace: str) -> tuple[str, str]:
    """`du --json`, whose rows always have the counts, and then `du
    --count`, which adds them to the text output (and changes nothing
    else)."""

    def check(rows: list[dict[str, Any]], arg: str) -> tuple[str, str]:
        try:
            got = du_counts(
                [
                    (
                        row.get("path", ""),
                        row.get("bytes"),
                        row.get("objects", 0),
                        row.get("collections", 0),
                    )
                    for row in rows
                ],
                arg,
            )
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


def du_protected_a(test: Test) -> tuple[str, str]:
    """du_tree() in /protected-a."""
    return du_tree(test, "protected-a")


def du_public(test: Test) -> tuple[str, str]:
    """du_tree() in /public, with no token."""
    return du_tree(test, "public")


def du_summarize(test: Test) -> tuple[str, str]:
    """`du -s` gives only the total."""

    def check(rows: list[dict[str, Any]], _arg: str) -> tuple[str, str]:
        if len(rows) != 1 or rows[0].get("bytes") != 13300:
            return FAIL, f"got {rows}, not one row of 13300 bytes"
        return PASS, "13300 bytes"

    return du_run(test, "protected-a", check, "-s")


def du_no_token(test: Test) -> tuple[str, str]:
    """`du` of /protected-a with no token: refused."""
    code, out, err = test.pelican_cmd(
        "object", "du", test.url("protected-a", f"{test.base}/tree")
    )
    why = no_token_refusal(code, out, err, test.fed.standalone)
    if why:
        return FAIL, why
    return PASS, f"refused ({refused(out + err)}): no token"


RUN = {
    "ls-protected-a": ls_protected_a,
    "ls-public": ls_public,
    "ls-recursive": ls_recursive,
    "ls-missing": ls_missing,
    "ls-no-token": ls_no_token,
    "stat-protected-a": stat_protected_a,
    "stat-public": stat_public,
    "stat-collection": stat_collection,
    "stat-missing": stat_missing,
    "delete-object": delete_object,
    "delete-recursive": delete_recursive,
    "delete-nonempty": delete_nonempty,
    "delete-missing": delete_missing,
    "delete-read-only": delete_read_only,
    "delete-no-token": delete_no_token,
    "delete-public": delete_public,
    "get-existing-file": get_existing_file,
    "get-collection": get_collection,
    "get-recursive": get_recursive,
    "put-exists": put_exists,
    "put-overwrite": put_overwrite,
    "put-into-collection": put_into_collection,
    "put-directory": put_directory,
    "put-recursive": put_recursive,
    "copy-upload": copy_upload,
    "copy-download": copy_download,
    "copy-tpc": copy_tpc_protected_a,
    "copy-tpc-public": copy_tpc_public,
    "copy-tpc-direct": copy_tpc_direct,
    "copy-to-public": copy_to_public,
    "sync-upload": sync_upload,
    "sync-unchanged": sync_unchanged,
    "sync-changed": sync_changed,
    "sync-download": sync_download,
    "sync-tpc": sync_tpc,
    "du-protected-a": du_protected_a,
    "du-public": du_public,
    "du-summarize": du_summarize,
    "du-no-token": du_no_token,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a."""
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the tree in place, run the selected scenarios, and remove the
    run's objects."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        for ns in fed.exports:
            directory = f"{origin.store_of(ns)}/data/cmd"
            if not origin.pstore and not os.path.isdir(directory):
                die(f"framework/var/{directory} is missing; run ./fed.sh init")
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
            cannot_delete = stores.cannot(fed, "delete")
            if cannot_delete and name in DELETES:
                results.add(name, SKIP, cannot_delete)
                continue
            namespace, needs = needs_of(name)
            test.lacks = needs - fed.exports[namespace]
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            except Exception as e:  # pylint: disable=broad-exception-caught
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
