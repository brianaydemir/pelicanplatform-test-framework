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
import traceback
from typing import Any, Optional

from testlib import common, stores, web
from testlib.common import die, shown
from testlib.report import FAIL, PASS
from testlib.session import Session, last_line

SCENARIOS = {
    "fields": "`object put --metadata-file`: the event carries the fields",
    "blob": "`--metadata-body` too: the event carries the blob, byte for byte",
    "update": "overwriting an object sends object.updated",
    "delete": "deleting an object sends object.deleted",
    "status": "the origin answers X-Pelican-Metadata-Status: published",
    "retry": (
        "a delivery that fails once: queued, then delivered (eventual)"
        " or rolled back (transactional)"
    ),
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
        # /protected-a's storage in each store.
        self.stores = sorted({o.store_of("protected-a") for o in fed.origins})
        self.tmp = session.path("metadata")
        os.makedirs(self.tmp)
        self.run = session.run
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        # The custom fields that `fields` and `blob` send.
        self.fields = {
            "experiment": "pelican-test-framework",
            "run_number": int(self.run.split("-")[0]),
            "is_test": True,
            "ratio": 0.25,
        }
        self.fields_file = os.path.join(self.tmp, "fields.json")
        with open(self.fields_file, "w", encoding="utf-8") as f:
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

    def check_stored(self, name: str, data: bytes, capture: str) -> Optional[tuple[str, str]]:
        """A failure if no store holds object name with exactly data."""
        path = self.object_file(name)
        if path is None:
            return FAIL, f"event {capture}, but the object is in no store"
        if not self.holds(name, data):
            return FAIL, f"{shown(path)} differs from what was uploaded"
        return None

    def await_event(self, path: str, kind: str) -> tuple[bool, str]:
        """(True, the ID of a capture of a `kind` event for object path that
        the verifier accepted), waiting up to 120 seconds; on a timeout,
        (False, why)."""
        for _ in range(120):
            accepted = [row[0] for row in index() if row[1:4] == ["200", kind, path]]
            if accepted:
                return True, accepted[-1]
            time.sleep(1)
        last = [row for row in index() if row[3:4] == [path]]
        if not last:
            return False, f"no {kind} event arrived"
        return False, last_delivery(last[-1])

    def direct_put(self, data: bytes, name: str, metadata: str) -> web.Response:
        """Upload data as object name straight to the first origin, with
        X-Pelican-Object-Metadata metadata."""
        return web.request(
            "PUT",
            f"{self.origin.url}{PREFIX}/{name}",
            token=self.token,
            upload=data,
            headers={"X-Pelican-Object-Metadata": metadata},
        )

    def client_put(self, name: str, data: bytes, *flags: str) -> tuple[bool, str]:
        """Upload data as object name with `pelican object put` and flags;
        (whether it succeeded, the last line it logged)."""
        source = os.path.join(self.tmp, name)
        with open(source, "wb") as f:
            f.write(data)
        code, out, err = self.session.pelican_cmd(
            "object",
            "put",
            "--token",
            self.token_file,
            *flags,
            source,
            f"{self.fed.url}{PREFIX}/{name}",
        )
        return code == 0, last_line(err or out)


def index() -> list[list[str]]:
    """The recorder's index.tsv, whose columns are <id> <status> <type>
    <path> <event id>."""
    try:
        with open(f"{CAPTURES}/index.tsv", encoding="utf-8") as f:
            return [line.rstrip("\n").split("\t") for line in f]
    except FileNotFoundError:
        return []


def event(capture: str, part: str) -> dict[str, Any]:
    """A part of the captured event (e.g. "object"), or {} if it has
    none."""
    with open(f"{CAPTURES}/{capture}.event.json", encoding="utf-8") as f:
        whole = common.as_object(json.load(f)) or {}
    return common.as_object(whole.get(part)) or {}


def last_delivery(row: list[str]) -> str:
    """How the delivery that index() row records went: the status and
    answer in its <id>.json, or, if that can't be read, the row's."""
    try:
        with open(f"{CAPTURES}/{row[0]}.json", encoding="utf-8") as f:
            capture = json.load(f)
        got, answer = capture.get("status"), str(capture.get("response", "")).rstrip()
    except (OSError, ValueError, AttributeError):
        got, answer = (row[1] if len(row) > 1 else "?"), "(no record of the answer)"
    return f"last delivery got HTTP {got}: {answer}"


def delivered(path: str, code: str) -> bool:
    """Whether index() records a delivery for object path that the
    recorder answered with code."""
    return any(row[1:2] == [code] and row[3:4] == [path] for row in index())


def same(got: object, want: object) -> bool:
    """Whether a value from an event's JSON is want, type and all: 1 is
    not True, nor 1.0."""
    return json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)


# --------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.


def fields(test: Test) -> tuple[str, str]:
    """`object put --metadata-file`: the event carries the fields."""
    name = f"{test.run}-fields"
    data = os.urandom(65536)
    ok, last = test.client_put(name, data, "--metadata-file", test.fields_file)
    if not ok:
        return FAIL, f"object put failed: {last}"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    got = event(capture, "object")
    wrong = [k for k, v in test.fields.items() if k not in got or not same(got[k], v)]
    if wrong:
        return FAIL, (
            f"{shown(CAPTURES)}/{capture}.event.json lacks fields.json's"
            f" {', '.join(wrong)}, or has them with other values or types"
        )
    return test.check_stored(name, data, capture) or (
        PASS,
        f"{shown(CAPTURES)}/{capture}.event.json",
    )


def blob(test: Test) -> tuple[str, str]:
    """`--metadata-body` too: the event carries the blob, byte for byte."""
    name = f"{test.run}-blob"
    data = os.urandom(65536)
    sent = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<run id="{test.run}">\n'.encode()
        + base64.encodebytes(os.urandom(3000))
        + b"</run>\n"
    )
    blob_file = os.path.join(test.tmp, "blob.xml")
    with open(blob_file, "wb") as f:
        f.write(sent)
    ok, last = test.client_put(
        name, data, "--metadata-file", test.fields_file, "--metadata-body", blob_file
    )
    if not ok:
        return FAIL, f"object put failed: {last}"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    meta = event(capture, "metadata")
    if meta.get("content_type") != "application/xml" or meta.get("size") != len(sent):
        return FAIL, f"{shown(CAPTURES)}/{capture}.event.json: wrong or missing .metadata"
    with open(f"{CAPTURES}/{capture}.blob", "rb") as f:
        if f.read() != sent:
            return FAIL, f"{shown(CAPTURES)}/{capture}.blob differs from the blob sent"
    return test.check_stored(name, data, capture) or (PASS, f"{shown(CAPTURES)}/{capture}.blob")


def update(test: Test) -> tuple[str, str]:
    """Overwriting an object sends object.updated."""
    name = f"{test.run}-update"
    answer = test.direct_put(os.urandom(4096), name, 'step="create"')
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, f"creating it ({answer.describe()}): {capture}"
    created = event(capture, "object")
    data = os.urandom(8192)
    answer = test.direct_put(data, name, 'step="update"')
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.updated")
    if not arrived:
        return FAIL, f"overwriting it ({answer.describe()}): {capture}"
    where = f"{shown(CAPTURES)}/{capture}.event.json"
    got = event(capture, "object")
    if not same(got.get("size"), len(data)):
        return FAIL, f"{where} has size {got.get('size')!r}, not the new {len(data)}"
    # The event's ETag is the backend's, which on POSIXv2 is not the one a
    # PUT or GET answers with; so it is only checked to have changed.
    if got.get("etag") and got.get("etag") == created.get("etag"):
        return FAIL, f"{where} has the original's etag, {got.get('etag')}"
    if not same(got.get("step"), "update"):
        return FAIL, f"{where} has step {got.get('step')!r}, not 'update'"
    if not test.holds(name, data):
        return FAIL, "the store doesn't hold the new bytes"
    return PASS, where


def delete(test: Test) -> tuple[str, str]:
    """Deleting an object sends object.deleted."""
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
        return FAIL, f"event {capture}, but {shown(left)} is still there"
    return PASS, f"{shown(CAPTURES)}/{capture}.event.json"


def status(test: Test) -> tuple[str, str]:
    """The origin answers X-Pelican-Metadata-Status: published."""
    name = f"{test.run}-status"
    answer = test.direct_put(os.urandom(4096), name, 'step="status"')
    got = answer.header("X-Pelican-Metadata-Status")
    if got != "published":
        return FAIL, f"{answer.describe()}, X-Pelican-Metadata-Status '{got}'"
    arrived, capture = test.await_event(f"{PREFIX}/{name}", "object.committed")
    if not arrived:
        return FAIL, capture
    return PASS, f"{shown(CAPTURES)}/{capture}.event.json"


def fault(test: Test, scenario: str, name: str, eventual_status: str) -> tuple[str, str]:
    """The recorder fails deliveries for objects named like name (see
    framework/metadata-server/recorder.py). In transactional mode the
    upload must fail with a 5xx and leave nothing behind; in eventual
    mode it must succeed, with X-Pelican-Metadata-Status eventual_status."""
    answer = test.direct_put(os.urandom(4096), name, f'step="{scenario}"')
    got = answer.header("X-Pelican-Metadata-Status")
    if test.mode == "transactional":
        refused = "503" if scenario == "retry" else "422"
        if answer.status is None or answer.status // 100 != 5:
            return FAIL, f"{answer.describe()}, not a 5xx, although the delivery failed"
        if not common.wait_for(lambda: delivered(f"{PREFIX}/{name}", refused), 10):
            return (
                FAIL,
                f"{answer.describe()}, but the recorder answered no delivery with {refused}",
            )
        if not common.wait_for(lambda: test.object_file(name) is None, 10):
            return FAIL, (
                f"{answer.describe()}, but {shown(test.object_file(name))} was not"
                " rolled back"
            )
        return PASS, f"{answer.describe()} after HTTP {refused} from the recorder"
    left = test.object_file(name)
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


def retry(test: Test) -> tuple[str, str]:
    """fault() for a delivery that fails once."""
    return fault(test, "retry", f"fail-once-{test.run}", "queued")


def reject(test: Test) -> tuple[str, str]:
    """fault() for a delivery rejected with 422."""
    return fault(test, "reject", f"reject-{test.run}", "rejected")


def uploaded(run_id: str) -> list[str]:
    """The names of the objects that the scenarios upload in run run_id."""
    names = [f"{run_id}-{s}" for s in ("fields", "blob", "update", "delete", "status")]
    return names + [f"fail-once-{run_id}", f"reject-{run_id}"]


RUN = {
    "fields": fields,
    "blob": blob,
    "update": update,
    "delete": delete,
    "status": status,
    "retry": retry,
    "reject": reject,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no publishing, or no writes."""
    if fed.metadata not in ("eventual", "transactional"):
        return "metadata publishing is off ('-p origin-metadata' or '-p origin-metadata-tx')"
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Wait for the recorder, run the selected scenarios, and remove the
    objects."""
    print("Waiting for the recorder at http://metadata:9999 ...")
    for n in range(30):
        if web.request("GET", "http://metadata:9999/healthz", timeout=5).status == 200:
            break
        if n == 29:
            die("the recorder is not answering; see './fed.sh logs metadata'")
        time.sleep(2)

    test = Test(session)
    print(
        f"Metadata test ({test.mode} mode) against {test.origin.svc},"
        + f" objects {PREFIX}/{test.run}-*\n"
    )
    try:
        for name in selected:
            try:
                result, note = RUN[name](test)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                result, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, result, note)
    finally:
        # What the scenarios uploaded, from every store.
        errors: list[str] = []
        for name in uploaded(test.run):
            for origin in stores.unique(session.fed.origins):
                error = stores.delete(
                    origin, "protected-a", f"data/metadata/{name}", test.token
                )
                if error:
                    errors.append(error)
        if errors:
            common.warn(f"could not remove the objects uploaded: {common.first(errors)}")
