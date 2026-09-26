"""Metadata publishing (`-p origin-metadata` or `-p origin-metadata-tx`):
upload objects with and without metadata, and check the events that
reach the recorder (framework/var/data/metadata/). With publishing off,
the suite is skipped.

Objects go under /protected-a/data/metadata/. `fields` and `blob` use
`pelican object put` through the director; the rest go straight to the
first origin in framework/var/generated/origins, since the client
doesn't show the origin's X-Pelican-Metadata-Status.
"""

import base64
import json
import os
import time
from typing import List, Optional, Tuple

from testlib import common, web
from testlib.common import die
from testlib.report import FAIL, PASS
from testlib.session import Session, last_line

SCENARIOS = {
    "fields": "`object put --metadata-file`: the event carries the fields",
    "blob":   "`--metadata-body` too: the event carries the blob, byte for byte",
    "update": "overwriting an object sends object.updated",
    "delete": "deleting an object sends object.deleted",
    "status": "the origin answers X-Pelican-Metadata-Status: published",
    "retry":  "a delivery that fails once: queued, then delivered (eventual)"
              " or rolled back (transactional)",
    "reject": "a delivery rejected with 422: kept (eventual) or rolled back (transactional)",
}

CAPTURES = "data/metadata"
PREFIX = "/protected-a/data/metadata"


class Test:
    """What every scenario needs: a token, the first origin, and the
    stores to look in."""

    def __init__(self, session: Session):
        fed = session.fed
        self.session = session
        self.fed = fed
        self.mode = fed.metadata
        self.origin = fed.origins[0]
        self.stores = sorted({o.store for o in fed.origins})
        self.tmp = session.path("metadata")
        os.makedirs(self.tmp)
        self.run = session.run
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        # The custom fields that `fields` and `blob` send.
        self.fields = {"experiment": "pelican-test-framework",
                       "run_number": int(self.run.split("-")[0]), "is_test": True,
                       "ratio": 0.25}
        self.fields_file = os.path.join(self.tmp, "fields.json")
        with open(self.fields_file, "w") as f:
            json.dump(self.fields, f, indent=2)

    def object_file(self, name: str) -> Optional[str]:
        """The object's file in the first store that has it, if any."""
        for store in self.stores:
            path = f"{store}/data/metadata/{name}"
            if os.path.exists(path):
                return path
        return None

    def holds(self, name: str, data: bytes) -> bool:
        """Whether a store holds object name, with exactly data."""
        path = self.object_file(name)
        if path is None:
            return False
        with open(path, "rb") as f:
            return f.read() == data

    def check_stored(self, name: str, data: bytes,
                     capture: str) -> Optional[Tuple[str, str]]:
        """A failure if no store holds object name with exactly data."""
        path = self.object_file(name)
        if path is None:
            return FAIL, f"event {capture}, but the object is in no store"
        if not self.holds(name, data):
            return FAIL, f"{path} differs from what was uploaded"
        return None

    def await_event(self, path: str, kind: str) -> Tuple[bool, str]:
        """(True, the ID of a capture of a `kind` event for object path that
        the verifier accepted), waiting up to 120 seconds; on a timeout,
        (False, why)."""
        for _ in range(120):
            accepted = [row[0] for row in index() if row[1:4] == ["200", kind, path]]
            if accepted:
                return True, accepted[-1]
            time.sleep(1)
        last = [row[0] for row in index() if row[3:4] == [path]]
        if not last:
            return False, f"no {kind} event arrived"
        with open(f"{CAPTURES}/{last[-1]}.json") as f:
            capture = json.load(f)
        return False, (f"last delivery got HTTP {capture.get('status')}:"
                       f" {str(capture.get('response', '')).rstrip()}")

    def direct_put(self, data: bytes, name: str, metadata: str) -> web.Response:
        """Upload data as object name straight to the first origin, with
        X-Pelican-Object-Metadata metadata."""
        return web.request("PUT", f"{self.origin.url}{PREFIX}/{name}", token=self.token,
                           upload=data, headers={"X-Pelican-Object-Metadata": metadata})

    def client_put(self, name: str, data: bytes, *flags: str) -> Tuple[bool, str]:
        """Upload data as object name with `pelican object put` and flags;
        (whether it succeeded, the last line it logged)."""
        source = os.path.join(self.tmp, name)
        with open(source, "wb") as f:
            f.write(data)
        code, out, err = self.session.pelican_cmd(
            "object", "put", "--token", self.token_file, *flags, source,
            f"{self.fed.url}{PREFIX}/{name}")
        return code == 0, last_line(err or out)


def index() -> List[List[str]]:
    """The recorder's index.tsv, whose columns are <id> <status> <type>
    <path> <event id>."""
    try:
        with open(f"{CAPTURES}/index.tsv") as f:
            return [line.rstrip("\n").split("\t") for line in f]
    except FileNotFoundError:
        return []


def event(capture: str) -> dict:
    with open(f"{CAPTURES}/{capture}.event.json") as f:
        return json.load(f)


#---------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.


def fields(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-fields"
    data = os.urandom(65536)
    ok, last = test.client_put(name, data, "--metadata-file", test.fields_file)
    if not ok:
        return FAIL, f"object put failed: {last}"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    got = event(capture).get("object", {})
    if any(got.get(k) != v for k, v in test.fields.items()):
        return FAIL, f"{CAPTURES}/{capture}.event.json lacks fields.json's fields"
    return test.check_stored(name, data, capture) or (PASS, f"{CAPTURES}/{capture}.event.json")


def blob(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-blob"
    data = os.urandom(65536)
    sent = (f'<?xml version="1.0" encoding="UTF-8"?>\n<run id="{test.run}">\n'.encode()
            + base64.encodebytes(os.urandom(3000)) + b"</run>\n")
    blob_file = os.path.join(test.tmp, "blob.xml")
    with open(blob_file, "wb") as f:
        f.write(sent)
    ok, last = test.client_put(name, data, "--metadata-file", test.fields_file,
                               "--metadata-body", blob_file)
    if not ok:
        return FAIL, f"object put failed: {last}"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    meta = event(capture).get("metadata") or {}
    if meta.get("content_type") != "application/xml" or meta.get("size") != len(sent):
        return FAIL, f"{CAPTURES}/{capture}.event.json: wrong or missing .metadata"
    with open(f"{CAPTURES}/{capture}.blob", "rb") as f:
        if f.read() != sent:
            return FAIL, f"{CAPTURES}/{capture}.blob differs from the blob sent"
    return test.check_stored(name, data, capture) or (PASS, f"{CAPTURES}/{capture}.blob")


def update(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-update"
    answer = test.direct_put(os.urandom(4096), name, 'step="create"')
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, f"creating it ({answer.describe()}): {capture}"
    data = os.urandom(8192)
    answer = test.direct_put(data, name, 'step="update"')
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.updated")
    if not arrived:
        return FAIL, f"overwriting it ({answer.describe()}): {capture}"
    if not test.holds(name, data):
        return FAIL, "the store doesn't hold the new bytes"
    return PASS, f"{CAPTURES}/{capture}.event.json"


def delete(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-delete"
    answer = test.direct_put(os.urandom(4096), name, 'step="create"')
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, f"creating it ({answer.describe()}): {capture}"
    answer = web.request("DELETE", f"{test.origin.url}{PREFIX}/{name}", token=test.token)
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.deleted")
    if not arrived:
        return FAIL, f"deleting it ({answer.describe()}): {capture}"
    left = test.object_file(name)
    if left is not None:
        return FAIL, f"event {capture}, but {left} is still there"
    return PASS, f"{CAPTURES}/{capture}.event.json"


def status(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-status"
    answer = test.direct_put(os.urandom(4096), name, 'step="status"')
    got = answer.header("X-Pelican-Metadata-Status")
    if got != "published":
        return FAIL, f"{answer.describe()}, X-Pelican-Metadata-Status '{got}'"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    return PASS, f"{CAPTURES}/{capture}.event.json"


def fault(test: Test, scenario: str, name: str, eventual_status: str) -> Tuple[str, str]:
    """The recorder fails deliveries for objects named like name (see
    framework/metadata-server/recorder.py). In transactional mode the
    upload must fail with a 5xx and leave nothing behind; in eventual
    mode it must succeed, with X-Pelican-Metadata-Status eventual_status."""
    answer = test.direct_put(os.urandom(4096), name, f'step="{scenario}"')
    got = answer.header("X-Pelican-Metadata-Status")
    left = test.object_file(name)
    if test.mode == "transactional":
        if answer.status is None or answer.status // 100 != 5:
            return FAIL, f"{answer.describe()}, not a 5xx, although the delivery failed"
        if left is not None:
            return FAIL, f"{answer.describe()}, but {left} was not rolled back"
        return PASS, answer.describe()
    if not answer.ok:
        return FAIL, answer.describe()
    if got != eventual_status:
        return FAIL, f"X-Pelican-Metadata-Status '{got}', not '{eventual_status}'"
    if left is None:
        return FAIL, "the object is in no store"
    if scenario == "retry":
        arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
        if not arrived:
            return FAIL, f"never redelivered: {capture}"
    return PASS, f"X-Pelican-Metadata-Status: {got}"


def retry(test: Test) -> Tuple[str, str]:
    return fault(test, "retry", f"fail-once-{test.run}", "queued")


def reject(test: Test) -> Tuple[str, str]:
    return fault(test, "reject", f"reject-{test.run}", "rejected")


RUN = {"fields": fields, "blob": blob, "update": update, "delete": delete,
       "status": status, "retry": retry, "reject": reject}


def skip(fed: common.Federation) -> Optional[str]:
    if fed.metadata not in ("eventual", "transactional"):
        return "metadata publishing is off ('-p origin-metadata' or '-p origin-metadata-tx')"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    print("Waiting for the recorder at http://metadata:9999 ...")
    for n in range(30):
        if web.request("GET", "http://metadata:9999/healthz", timeout=5).status == 200:
            break
        if n == 29:
            die("the recorder is not answering; see './fed.sh logs metadata'")
        time.sleep(2)

    test = Test(session)
    print(f"Metadata test ({test.mode} mode) against {test.origin.svc},"
          f" objects {PREFIX}/{test.run}-*\n")
    for name in selected:
        results.add(name, *RUN[name](test))
