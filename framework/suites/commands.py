"""The client's other commands: `pelican object ls`, `stat`, `delete`,
`copy`, `sync`, and `du`, in /public and /protected-a, checking each
against the origins' stores.

The read-only commands run against a tree uploaded straight to every
origin, /<namespace>/data/cmd/<run>/tree/, so that they agree whichever
origin the director picks. The rest go through the director like any
client. Under `topo-multi-origin` their effects land on one origin, so
a success must show in some store, and a refusal in none. /public takes
no writes (see testlib/credentials.py), so writes to it must be refused.

These commands exit 0, 1, or 11, whatever went wrong, so failures are
judged by what they print (see testlib/transfers.py's FAILURE_RULES).
"""

import base64
import binascii
import hashlib
import json
import os
import re
from typing import Dict, List, Optional, Tuple

from testlib import common, credentials, stores, transfers
from testlib.common import die
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ls-protected-a":   "`ls -l --json` of /protected-a: names, sizes, and collections",
    "ls-public":        "the same in /public, with no token",
    "ls-recursive":     "`ls -r` lists every object in the tree",
    "ls-missing":       "`ls` of a missing path: not found",
    "ls-no-token":      "`ls` of /protected-a with no token: refused",
    "stat-protected-a": "`stat --json --checksums crc32c --checksums md5`: size and checksums",
    "stat-public":      "the same in /public, with no token",
    "stat-collection":  "`stat` of a collection says so",
    "stat-missing":     "`stat` of a missing object: not found",
    "delete-object":    "`delete` removes an object",
    "delete-recursive": "`delete -r` removes a collection and what it holds",
    "delete-nonempty":  "`delete` of a non-empty collection without -r: refused, nothing removed",
    "delete-missing":   "`delete` of a missing object: not found",
    "delete-read-only": "`delete` with a read-only token: refused, nothing removed",
    "delete-no-token":  "`delete` with no token: refused, nothing removed",
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
    "du-protected-a":   "`du --json --count`: bytes, objects, and collections per collection",
    "du-public":        "the same in /public, with no token",
    "du-summarize":     "`du -s` gives only the total",
    "du-no-token":      "`du` of /protected-a with no token: refused",
}

# The tree the read-only commands work on, under data/cmd/<run>/tree/.
TREE = {"a": 1000, "b": 5000, "sub/c": 300, "sub/deeper/d": 7000}


#---------------------------------------------------------------------------
# Checksums, as `stat --checksums` reports them: hex (a base64 digest is
# also accepted).

def _crc32c_table() -> List[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C = _crc32c_table()


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for byte in data:
        c = _CRC32C[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def digest_bytes(value: str) -> Optional[bytes]:
    """The digest a reported checksum stands for: hex, or else base64."""
    value = value.strip()
    try:
        return binascii.unhexlify(value if len(value) % 2 == 0 else "0" + value)
    except binascii.Error:
        pass
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error:
        return None


def checksum_matches(kind: str, value: str, data: bytes) -> bool:
    got = digest_bytes(value)
    if got is None:
        return False
    if kind == "crc32c":
        return int.from_bytes(got, "big") == crc32c(data)
    return got == hashlib.md5(data).digest()


#---------------------------------------------------------------------------
# What a failed command said.

def refused(text: str) -> Optional[str]:
    """Why a command's output shows it was refused, or None if it doesn't."""
    categories = {transfers.classify(line) for line in text.splitlines()}
    for category in ("client", "director", "unsupported", "server"):
        if category in categories:
            return category
    if re.search(r"\b40[13]\b|unauthori[sz]ed|forbidden", text, re.I):
        return "server"
    if "failed to retrieve token" in text:
        return "client"
    return None


def not_found(text: str) -> bool:
    return bool(re.search(r"Error code 5011|\b404\b|does not exist|not found", text, re.I))


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
        # The namespaces the scenarios use.
        namespaces = ("public", "protected-a")
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
        """Put data at rel in both namespaces at every origin."""
        token = self.tokens["protected-a"]
        parent = rel.rsplit("/", 1)[0]
        errors = stores.make_dirs(self.origins, "protected-a", parent, token)
        errors += stores.put_everywhere(self.origins, "protected-a", rel, data, token)
        if errors:
            die("could not upload the test objects: " + "; ".join(errors))

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
        if code == 0:
            return "succeeded, but should have failed: not found"
        if not not_found(out + err):
            return f"failed, but not with not found: {last_line(err or out)}"
        return None


def json_out(text: str):
    """The JSON a command printed on stdout: its last line that parses."""
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("no JSON in the output")


def basename(name: str) -> str:
    return name.rstrip("/").rsplit("/", 1)[-1]


#---------------------------------------------------------------------------
# ls

def ls_tree(test: Test, namespace: str) -> Tuple[str, str]:
    args = ["object", "ls", "-l", "--json"]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        entries = {basename(e["Name"]): e for e in json_out(out)}
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


def ls_protected_a(test: Test) -> Tuple[str, str]:
    return ls_tree(test, "protected-a")


def ls_public(test: Test) -> Tuple[str, str]:
    return ls_tree(test, "public")


def ls_recursive(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "ls", "-r", "-l", "--json",
                                      *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        objects = {(basename(e["Name"]), e.get("Size")) for e in json_out(out)
                   if not e.get("IsCollection")}
    except (ValueError, KeyError, TypeError) as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    want = {(basename(path), size) for path, size in TREE.items()}
    if objects != want:
        return FAIL, f"listed {sorted(objects)}, not {sorted(want)}"
    return PASS, f"{len(objects)} objects"


def ls_missing(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "ls", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree/nope"))
    return (FAIL, why) if (why := test.expect_not_found(code, out, err)) else (PASS, "")


def ls_no_token(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "ls", test.url("protected-a", f"{test.base}/tree"))
    if (why := test.expect_refusal(code, out, err)):
        return FAIL, why
    return PASS, f"refused ({refused(out + err)})"


#---------------------------------------------------------------------------
# stat

def stat_object(test: Test, namespace: str) -> Tuple[str, str]:
    """An ssh origin sends no digests (ssh_posixv2 in Pelican), and `stat
    --checksums` fails when none come back, so there only the size is
    checked."""
    data = test.tree["b"]
    kinds = [] if test.fed.origin_variant == "ssh" else ["crc32c", "md5"]
    args = ["object", "stat", "--json"]
    for kind in kinds:
        args += ["--checksums", kind]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, test.url(namespace, f"{test.base}/tree/b"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    try:
        info = json_out(out)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    if info.get("Size") != len(data) or info.get("IsCollection"):
        return FAIL, f"size {info.get('Size')}, collection {info.get('IsCollection')}"
    sums = {k.lower(): v for k, v in (info.get("checksums") or {}).items()}
    notes = [] if kinds else ["size only: an ssh origin sends no checksums"]
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
        info = json_out(out)
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
    test.seed(rel, os.urandom(2000))
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("protected-a"),
                                      test.url("protected-a", rel))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if not test.gone_somewhere("protected-a", rel):
        return FAIL, "every origin still holds the object"
    return PASS, ""


def delete_recursive(test: Test) -> Tuple[str, str]:
    rels = [f"{test.base}/del/tree/1", f"{test.base}/del/tree/sub/2"]
    for rel in rels:
        test.seed(rel, os.urandom(1000))
    code, out, err = test.pelican_cmd("object", "delete", "-r", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/del/tree"))
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    held = [test.held("protected-a", rel) for rel in rels]
    # Some origin (the one the director chose) must hold none of them.
    if not any(all(h[i] is None for h in held) for i in range(len(held[0]))):
        return FAIL, "no origin is rid of the whole collection"
    return PASS, ""


def delete_nonempty(test: Test) -> Tuple[str, str]:
    rel = f"{test.base}/del/full/1"
    data = os.urandom(1000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/del/full"))
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
    rel = f"{test.base}/del/{name}"
    data = os.urandom(1000)
    test.seed(rel, data)
    code, out, err = test.pelican_cmd("object", "delete", *args, test.url("protected-a", rel))
    if (why := test.expect_refusal(code, out, err)):
        return FAIL, why
    if not test.everywhere("protected-a", rel, data):
        return FAIL, "refused, but an origin no longer holds the object"
    return PASS, f"refused ({refused(out + err)})"


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
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if not test.somewhere("protected-a", rel, data):
        return FAIL, "no origin holds the upload"
    return PASS, ""


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
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if not test.somewhere("protected-a", rel, test.tree[path]):
        return FAIL, "no origin holds the copy"
    return PASS, ""


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
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    missing = [p for p, data in files.items()
               if not test.somewhere("protected-a", f"{remote}/{p}", data)]
    if missing:
        return FAIL, f"no origin holds {', '.join(missing)}"
    return PASS, f"{len(files)} files"


def sync_unchanged(test: Test) -> Tuple[str, str]:
    source, files = local_tree(test, "sync-pre")
    remote = f"{test.base}/sync/pre"
    for path, data in files.items():
        test.seed(f"{remote}/{path}", data)
    code, out, err = test.pelican_cmd("object", "sync", "--dry-run", *test.token_args("protected-a"),
                                      source, test.url("protected-a", remote), env=test.sync_env)
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    if upload_lines(out + err):
        return FAIL, f"would upload: {'; '.join(upload_lines(out + err))}"
    return PASS, "nothing to upload"


def sync_changed(test: Test) -> Tuple[str, str]:
    source, files = local_tree(test, "sync-changed")
    remote = f"{test.base}/sync/changed"
    for path, data in files.items():
        test.seed(f"{remote}/{path}", data)
    changed = os.urandom(len(files["x"]) + 50)
    test.local("sync-changed/x", changed)
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
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    wrong = [p for p, data in test.tree.items()
             if common.sha256(os.path.join(target, p)) != hashlib.sha256(data).hexdigest()]
    if wrong:
        return FAIL, f"missing or different locally: {', '.join(wrong)}"
    return PASS, f"{len(test.tree)} files"


def sync_tpc(test: Test) -> Tuple[str, str]:
    test.make_dirs(f"{test.base}/sync")
    remote = f"{test.base}/sync/tpc"
    code, out, err = test.pelican_cmd("object", "sync", *test.token_args("protected-a"),
                                      test.url("protected-a", f"{test.base}/tree"),
                                      test.url("protected-a", remote), env=test.sync_env)
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err)}"
    missing = [p for p, data in test.tree.items()
               if not test.somewhere("protected-a", f"{remote}/{p}", data)]
    if missing:
        return FAIL, f"no origin holds {', '.join(missing)}"
    return PASS, f"{len(test.tree)} files"


#---------------------------------------------------------------------------
# du

def du_rows(test: Test, namespace: str, *flags: str) -> Tuple[Optional[str], list, str]:
    """(why it failed, or None; the rows; the argument given)."""
    arg = test.url(namespace, f"{test.base}/tree")
    args = ["object", "du", "--json", *flags]
    if namespace == "protected-a":
        args += test.token_args(namespace)
    code, out, err = test.pelican_cmd(*args, arg)
    if code != 0:
        return f"exit {code}: {last_line(err)}", [], arg
    try:
        return None, json_out(out), arg
    except ValueError as e:
        return f"unreadable output ({e}): {last_line(out)}", [], arg


def du_tree(test: Test, namespace: str) -> Tuple[str, str]:
    why, rows, arg = du_rows(test, namespace, "--count")
    if why:
        return FAIL, why
    # The argument's own row repeats it as given; the rest are bare paths.
    got = {}
    for row in rows:
        path = row.get("path", "")
        key = "" if path == arg else path.split("/tree", 1)[-1]
        got[key] = (row.get("bytes"), row.get("objects", 0), row.get("collections", 0))
    want = {"": (13300, 4, 2), "/sub": (7300, 2, 1), "/sub/deeper": (7000, 1, 0)}
    if got != want:
        return FAIL, f"got {got}, not {want}"
    return PASS, "13300 bytes, 4 objects, 2 collections"


def du_protected_a(test: Test) -> Tuple[str, str]:
    return du_tree(test, "protected-a")


def du_public(test: Test) -> Tuple[str, str]:
    return du_tree(test, "public")


def du_summarize(test: Test) -> Tuple[str, str]:
    why, rows, arg = du_rows(test, "protected-a", "-s")
    if why:
        return FAIL, why
    if len(rows) != 1 or rows[0].get("bytes") != 13300:
        return FAIL, f"got {rows}, not one row of 13300 bytes"
    return PASS, "13300 bytes"


def du_no_token(test: Test) -> Tuple[str, str]:
    code, out, err = test.pelican_cmd("object", "du", test.url("protected-a", f"{test.base}/tree"))
    if (why := test.expect_refusal(code, out, err)):
        return FAIL, why
    return PASS, f"refused ({refused(out + err)})"


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
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    fed = session.fed
    disk_stores = [o.store for o in stores.unique(fed.origins) if not o.pstore]
    for store in disk_stores:
        if not os.path.isdir(f"{store}/data/cmd"):
            die(f"framework/var/{store}/data/cmd is missing; run ./fed.sh init")
        # Earlier runs' trees come out first.
        common.empty_dir(f"{store}/data/cmd")

    test = Test(session)
    print(f"Command test against {fed.url}, objects under /<namespace>/{test.base}/\n")
    try:
        test.seed_tree()
        for name in selected:
            # Each scenario in /public is named *-public.
            if name.endswith("-public") and "public" not in fed.exports:
                results.add(name, SKIP, "this shape does not export /public")
                continue
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            results.add(name, status, note)
    finally:
        # Some of the tree's collections are the origins' making, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        for store in disk_stores:
            common.empty_dir(f"{store}/data/cmd")
