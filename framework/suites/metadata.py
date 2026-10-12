"""Metadata publishing (`-p origin-metadata` or `-p origin-metadata-tx`):
upload objects with and without metadata, and check the events that
reach the recorder (framework/var/data/metadata/); the URLs by which an
uploader follows or cancels its event; the events that a MOVE, a COPY,
and a collection's DELETE send, and those the reconcile sweep must not;
the first origin's admin APIs over its record of objects and its publish
queue, and the CLI over the record; and refusing uploads while a
namespace's queue is full. With publishing off, only `queue-admin off`
runs: the queue's admin API must say that publishing is off.

Objects go under /protected-a/data/metadata/. `fields` and `blob` use
`pelican object put` through the director; the rest go straight to the
first origin in framework/var/generated/origins, since the client
doesn't show the origin's X-Pelican-Metadata-Status.

`queue-admin` and `backpressure` restart the first origin with its
retries backed off for ten minutes, so that an event the recorder always
fails stays queued, and in eventual mode with caps on the namespace's
queue; `catalog`'s reconcile rows restart it with a reconcile sweep
every few seconds (control.restart_with()). Each restores it afterward,
and waits until the federation serves again. Without a control helper,
or in the `tiny` topology, whose origin is the whole federation's
process, those rows skip, but for `queue-admin`'s, which then run at the
origin as it is.
"""

import base64
import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import partial
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

from testlib import accounts, common, control, credentials, director, metrics, stores, web
from testlib.common import die, first, shown, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
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
    "status-urls": "a queued or rejected upload's status URLs: read, cancelled, gone once done",
    "catalog": (
        "MOVE, COPY, and a collection's DELETE send their events; the reconcile"
        " sweep sends none of its own"
    ),
    "object-metadata-cli": "the record of objects: its admin API and CLI, for the admin alone",
    "queue-admin": "the publish queue's admin API, for the admin alone: show, retry, delete",
    "backpressure": "a namespace's full queue refuses uploads until it drains (eventual)",
}

# A row: its status and note.
Row = tuple[str, str]

CAPTURES = "data/metadata"
NAMESPACE = "/protected-a"
PREFIX = f"{NAMESPACE}/data/metadata"

# The cases of each scenario that has them, each a row named
# `<scenario> <case>`. With publishing off, `queue-admin` has one, `off`.
CASES = {
    "status-urls": ("queued", "rejected", "cancel"),
    "catalog": (
        "move",
        "copy",
        "collection-delete",
        "reconcile-cancelled",
        "reconcile-rejected",
        "reconcile-observed",
    ),
    "object-metadata-cli": ("api", "cli"),
    "queue-admin": ("auth", "admin", "retry", "delete"),
    "backpressure": ("rows", "bytes", "drained"),
}
RECONCILE_CASES = ("reconcile-cancelled", "reconcile-rejected", "reconcile-observed")

# The first origin's web API (origin/origin_ui.go in Pelican): its
# publish queue's admin routes, the status URLs it hands an uploader, and
# its record of objects (Origin.Metadata.TrackAccess, which the metadata
# presets turn on).
QUEUE = "/api/v1.0/origin_ui/metadata_queue"
STATUS_ROUTE = "/api/v1.0/origin_ui/metadata_publish/"
OBJECTS = "/api/v1.0/origin_ui/object_metadata"

# What the origin tells an uploader of its event, in the PUT's answer.
STATUS_HEADER = "X-Pelican-Metadata-Status"
QUERY_URL = "X-Pelican-Metadata-Query-Url"
MANAGE_URL = "X-Pelican-Metadata-Manage-Url"

# The ordinary user, whom the first origin's admin creates once, and
# later runs reuse; and how long the suite's tokens for the origin's web
# API last, which the restarts may outlast.
USER = "test-metadata"
TOKEN_LIFETIME = 3600

# How long an event that should come at once may take to; how long a
# cancelled event must then not be delivered (longer than the retries,
# 5 to 10 seconds apart at first, by MinBackoff in
# config.d/base/40-origin.yaml); and how long a retried event may take to
# be delivered again.
EVENT_WAIT = 30
QUIET_WAIT = 20
RETRY_WAIT = 30

# The restarts: the configuration's file; how long a failed delivery is
# put off; the cap on the namespace's pending rows, above those it has
# when the suite asks; and on its pending blob bytes, which one blob of
# that size fills.
CONFIG_FILE = "90-metadata.yaml"
HELD_BACKOFF = "10m"
ROW_HEADROOM = 2
BLOB_CAP = 4096
# The reconcile restart: how often the sweep runs, how long an object
# must be at rest before it is a candidate, and how long the suite
# watches the recorder.
RECONCILE_INTERVAL = 15
RECONCILE_SETTLE = 5
RECONCILE_WATCH = 45
# How long the federation may take to serve again after a restart.
SETTLE_WAIT = 150

BACKPRESSURE = "pelican_origin_metadata_backpressure_refusals_total"


class Problem(Exception):
    """A scenario could not do what it needed to judge Pelican: it fails,
    with this as its note."""


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
        self.web = self.origin.web_url
        # What the scenarios made under data/metadata/, by name, to remove
        # afterward; and of those, the objects whose events the recorder
        # always fails, which are deleted through the first origin, so
        # that it records them gone, before the run's queue rows are.
        self.made: set[str] = set(uploaded(self.run))
        self.held: set[str] = set()
        self.admin_file = session.path("tokens/metadata.admin")
        self._admin: Optional[str] = None
        self._user: Optional[str] = None

    def url(self, name: str) -> str:
        """Object name's URL at the first origin."""
        return f"{self.origin.url}{PREFIX}/{quote(name)}"

    def admin(self) -> str:
        """The first origin's admin's bearer token (see web_token()), whose
        file is admin_file."""
        if self._admin is None:
            self._admin = web_token(self.fed, self.origin, self.admin_file, "admin")
        return self._admin

    def user(self) -> str:
        """An ordinary user's bearer token for the first origin's web API:
        USER's, whom its admin creates first, if need be. The admin, and
        then the user, agree to its AUP, without which it would refuse
        the user for that, not for being no admin."""
        if self._user is None:
            admin = accounts.Api(self.web, self.admin())
            accounts.agree_to_aup(admin)
            accounts.ensure_user(admin, USER)
            out = self.session.path("tokens/metadata.user")
            token = web_token(self.fed, self.origin, out, USER)
            accounts.agree_to_aup(accounts.Api(self.web, token))
            self._user = token
        return self._user

    def api(self, method: str, route: str, token: Optional[str]) -> accounts.Answer:
        """Send method to route of the first origin's web API, presenting
        token, if any."""
        return accounts.Api(self.web, token).call(method, route)

    def queue_rows(self) -> list[dict[str, Any]]:
        """Every row of the first origin's publish queue in the namespace,
        pending or rejected, as its admin lists them, a page at a time.
        Raises Problem if it won't list them."""
        rows: list[dict[str, Any]] = []
        while True:
            query = urlencode({"namespace": NAMESPACE, "limit": 1000, "offset": len(rows)})
            answer = self.api("GET", f"{QUEUE}?{query}", self.admin())
            if answer.status != 200:
                raise Problem(f"listing the publish queue: {answer.describe()}")
            page = [
                common.as_object(r) or {}
                for r in common.as_array(answer.object.get("rows")) or []
            ]
            rows += page
            if len(page) < 1000:
                return rows

    def rows_for(self, path: str) -> list[dict[str, Any]]:
        """queue_rows() for object path."""
        return [r for r in self.queue_rows() if r.get("object_path") == path]

    def pending(self) -> int:
        """How many rows of the namespace are pending in the first origin's
        publish queue, as its health route says. Raises Problem if it won't
        say."""
        answer = self.api("GET", f"{QUEUE}/_health", self.admin())
        if answer.status != 200:
            raise Problem(f"GET {QUEUE}/_health: {answer.describe()}")
        counts = common.as_object(answer.object.get("per_namespace")) or {}
        return int(counts.get(NAMESPACE) or 0)

    def drop(self, event_id: str) -> Optional[str]:
        """Delete event_id's row from the first origin's publish queue, as
        its admin; why that failed, or None."""
        answer = self.api("DELETE", f"{QUEUE}/{quote(event_id)}", self.admin())
        if answer.status in (200, 204, 404):
            return None
        return f"deleting queue row {event_id}: {answer.describe()}"

    def drop_rows(self, paths: Sequence[str]) -> list[str]:
        """Delete the first origin's queue rows for paths; what went wrong."""
        doomed = [r for r in self.queue_rows() if r.get("object_path") in paths]
        return [e for e in (self.drop(str(r.get("event_id", ""))) for r in doomed) if e]

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

    def await_event(
        self, path: str, kind: str, seconds: int = 120, after: int = 0
    ) -> tuple[bool, str]:
        """(True, the ID of a capture of a `kind` event for object path that
        the verifier accepted, among those since after, a time.time_ns()),
        waiting up to seconds; on a timeout, (False, why)."""
        for _ in range(seconds):
            accepted = [row[0] for row in since(after) if row[1:4] == ["200", kind, path]]
            if accepted:
                return True, accepted[-1]
            time.sleep(1)
        last = [row for row in since(after) if row[3:4] == [path]]
        if not last:
            return False, f"no {kind} event arrived"
        return False, last_delivery(last[-1])

    def await_all(
        self, wanted: Sequence[tuple[str, tuple[str, ...]]], after: int, seconds: float
    ) -> list[str]:
        """Wait up to seconds for an event that the verifier accepted, since
        after (a time.time_ns()), for each object path in wanted, of one of
        its kinds; what never came, each said as `<kinds> for <path>`."""

        def missing() -> list[str]:
            rows = [row for row in since(after) if row[1:2] == ["200"]]
            return [
                f"{' or '.join(kinds)} for {path}"
                for path, kinds in wanted
                if not any(row[2:4] == [kind, path] for row in rows for kind in kinds)
            ]

        common.wait_for(lambda: not missing(), seconds, 1)
        return missing()

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


def since(after: int) -> list[list[str]]:
    """index()'s rows for the deliveries that arrived after after, a
    time.time_ns(): each row's ID is when its delivery arrived, by the
    recorder's clock, which is the dev container's too (both are
    containers on one host)."""
    return [
        row for row in index() if len(row) >= 5 and row[0].isdigit() and int(row[0]) > after
    ]


def deliveries(after: int, path: str = "", event_id: str = "") -> list[list[str]]:
    """since(after)'s rows for object path, or for event event_id."""
    return [
        row
        for row in since(after)
        if (path and row[3] == path) or (event_id and row[4] == event_id)
    ]


def web_token(fed: common.Federation, origin: common.Origin, out: str, username: str) -> str:
    """A bearer token that origin's web API takes as username's
    (credentials.mint_user()), lasting the whole suite, written to out."""
    return credentials.mint_user(
        fed,
        out,
        credentials.local_issuer(fed, origin.web_url),
        common.signing_key(f"issuer-keys/{origin.svc}"),
        username,
        TOKEN_LIFETIME,
    )


def fetch(method: str, url: str, token: Optional[str] = None) -> accounts.Answer:
    """Send method to url, a whole URL, as accounts.Api.call() does."""
    answer = web.request(method, url, token=token)
    try:
        data = json.loads(answer.body) if answer.body else None
    except ValueError:
        data = None
    return accounts.Answer(answer.status, data)


def go_time(text: Any) -> Optional[float]:
    """A time as Go's encoding/json writes it (RFC 3339, perhaps with
    nanoseconds), in Unix time; None if text is none."""
    found = re.fullmatch(
        r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", str(text)
    )
    if found is None:
        return None
    zone = "+00:00" if found.group(3) == "Z" else found.group(3)
    whole = datetime.fromisoformat(found.group(1) + zone).timestamp()
    return whole + float(f"0.{found.group(2) or 0}")


def outcome(problems: Sequence[str], note: str) -> Row:
    """FAIL with the first of problems, if any; else PASS with note."""
    return (FAIL, first(problems)) if problems else (PASS, note)


def guarded(check: Callable[[], Row]) -> Row:
    """check's row; a FAIL if it could not set up, or raised."""
    try:
        return check()
    except (Problem, accounts.ApiError, stores.Unreadable) as e:
        return FAIL, str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # row fails, and the rest still run.
        traceback.print_exc()
        return FAIL, f"{type(e).__name__}: {e}"


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
    """The names of the objects that the first seven scenarios upload in
    run run_id."""
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


# --------------------------------------------------------------------------
# status-urls


def issued(answer: web.Response, name: str, wanted: str) -> tuple[str, str]:
    """The query and manage URLs that answer, to the upload of object
    name, hands the uploader, whose X-Pelican-Metadata-Status must be
    wanted; raises Problem if the upload failed, says otherwise, or lacks
    either URL."""
    if not answer.ok:
        raise Problem(f"uploading {name} got {answer.describe()}")
    got = answer.header(STATUS_HEADER)
    if got != wanted:
        stale = got == "published" and name.startswith("fail-always-")
        hint = (
            "; the recorder predates fail-always-*: ./fed.sh restart metadata" if stale else ""
        )
        raise Problem(f"uploading {name}: {STATUS_HEADER} '{got}', not '{wanted}'{hint}")
    urls = (answer.header(QUERY_URL), answer.header(MANAGE_URL))
    for header, url in zip((QUERY_URL, MANAGE_URL), urls):
        if not url:
            raise Problem(f"the {wanted} upload of {name} carries no {header}")
        if not urlsplit(url).path.startswith(STATUS_ROUTE):
            raise Problem(f"{header} is {url}, not under {STATUS_ROUTE}")
    if urls[0] == urls[1]:
        raise Problem(f"{QUERY_URL} and {MANAGE_URL} are both {urls[0]}")
    return urls


def read_status(url: str, path: str, state: str) -> Optional[str]:
    """Why a GET of status URL url does not say that object path's event
    is in state, or None."""
    answer = fetch("GET", url)
    if answer.status != 200:
        return f"GET {url} got {answer.describe()}, not 200"
    found = answer.object
    if found.get("object_path") != path or found.get("state") != state:
        return (
            f"GET {url} says {found.get('object_path')!r} is {found.get('state')!r},"
            f" not {path!r} {state!r}"
        )
    return None


def not_gone(urls: Sequence[str]) -> Optional[str]:
    """Why a GET of each of urls does not answer 404, or None."""
    for url in urls:
        answer = fetch("GET", url)
        if answer.status != 404:
            return f"GET {url} got {answer.describe()}, not 404"
    return None


def urls_queued(test: Test) -> Row:
    """A delivery that fails once: the upload hands out both URLs, each of
    which reads the event as pending, and answers 404 once the event is
    delivered, which deletes it."""
    name = f"fail-once-{test.run}-urls"
    path = f"{PREFIX}/{name}"
    test.made.add(name)
    urls = issued(test.direct_put(os.urandom(4096), name, 'step="status-urls"'), name, "queued")
    problems = [p for p in (read_status(url, path, "pending") for url in urls) if p]
    if problems:
        return FAIL, first(problems)
    arrived, why = test.await_event(path, "object.committed", EVENT_WAIT)
    if not arrived:
        return FAIL, f"the event was never delivered: {why}"
    if not common.wait_for(lambda: not_gone(urls) is None, 10):
        return FAIL, f"once the event was delivered, {not_gone(urls)}"
    return PASS, "both URLs read it pending, and 404 once it was delivered"


def urls_rejected(test: Test) -> Row:
    """A delivery rejected with 422: the upload hands out both URLs, each
    of which reads the event as rejected, which it stays; the manage URL
    removes it."""
    name = f"reject-{test.run}-urls"
    path = f"{PREFIX}/{name}"
    test.made.add(name)
    urls = issued(
        test.direct_put(os.urandom(4096), name, 'step="status-urls"'), name, "rejected"
    )
    problems = [p for p in (read_status(url, path, "rejected") for url in urls) if p]
    if problems:
        return FAIL, first(problems)
    removed = fetch("DELETE", urls[1])
    if removed.status != 200:
        return FAIL, f"DELETE through the manage URL got {removed.describe()}, not 200"
    why = not_gone(urls)
    if why:
        return FAIL, f"once it was deleted, {why}"
    return PASS, "both URLs read it rejected, and the manage URL deleted it"


def urls_cancel(test: Test) -> Row:
    """A delivery that always fails: the query URL may only read the
    event, so its DELETE gets 403, and the event stays; the manage URL's
    DELETE cancels it (200), after which both URLs answer 404, and the
    recorder gets no more deliveries of it, though the next retry was due
    within seconds."""
    name = f"fail-always-{test.run}-urls"
    path = f"{PREFIX}/{name}"
    test.made.add(name)
    test.held.add(name)
    query, manage = issued(
        test.direct_put(os.urandom(4096), name, 'step="status-urls"'), name, "queued"
    )
    why = read_status(query, path, "pending")
    if why:
        return FAIL, why
    event_id = str(fetch("GET", query).object.get("event_id", ""))
    problems: list[str] = []
    refused = fetch("DELETE", query)
    if refused.status != 403:
        problems.append(f"DELETE through the query URL got {refused.describe()}, not 403")
    why = read_status(query, path, "pending")
    if why:
        problems.append(f"after the query URL's DELETE, {why}")
    cancelled = fetch("DELETE", manage)
    after = time.time_ns()
    if cancelled.status != 200 or cancelled.object.get("cancelled") is not True:
        problems.append(
            f"DELETE through the manage URL got {cancelled.describe()}, not a cancel"
        )
    if problems:
        return FAIL, first(problems)
    why = not_gone((query, manage))
    if why:
        return FAIL, f"once it was cancelled, {why}"
    time.sleep(QUIET_WAIT)
    later = deliveries(after, event_id=event_id) if event_id else deliveries(after, path=path)
    if later:
        return FAIL, f"the recorder got {len(later)} deliveries of it after it was cancelled"
    return PASS, (
        f"the query URL's DELETE got 403; the manage URL's cancelled it, and in {QUIET_WAIT}s"
        " it was not delivered again"
    )


def status_urls(test: Test) -> dict[str, Row]:
    """The URLs by which an uploader follows or cancels its event, which
    an eventual-mode upload hands out whenever its event outlives it
    (handleMetadataStatus and handleMetadataCancel in
    origin_serve/metadata_status_api.go)."""
    if test.mode != "eventual":
        why = "transactional mode hands out no status URLs: an event never outlives its upload"
        return {case: (SKIP, why) for case in CASES["status-urls"]}
    return {
        "queued": guarded(lambda: urls_queued(test)),
        "rejected": guarded(lambda: urls_rejected(test)),
        "cancel": guarded(lambda: urls_cancel(test)),
    }


# --------------------------------------------------------------------------
# catalog


def committed(test: Test, name: str, metadata: str) -> None:
    """Upload object name straight to the first origin, with
    X-Pelican-Object-Metadata metadata, and wait for its object.committed
    event; raises Problem if it does not come."""
    test.made.add(name.split("/", 1)[0])
    answer = test.direct_put(os.urandom(4096), name, metadata)
    arrived, why = test.await_event(f"{PREFIX}/{name}", "object.committed", EVENT_WAIT)
    if not arrived:
        raise Problem(f"uploading {name} ({answer.describe()}): {why}")


def catalog_move(test: Test) -> Row:
    """MOVE an object to a new name: the new path must get an
    object.committed (or object.updated) event, and the old one an
    object.deleted. Pelican records the rename in its own record of
    objects, but publishes nothing (aferoFileSystem.Rename in
    origin_serve/filesystem.go)."""
    src, dst = f"{test.run}-move-from", f"{test.run}-move-to"
    test.made.add(dst)
    committed(test, src, 'step="move"')
    after = time.time_ns()
    answer = web.request(
        "MOVE", test.url(src), token=test.token, headers={"Destination": test.url(dst)}
    )
    if answer.status not in (201, 204):
        return FAIL, f"MOVE got {answer.describe()}, not 201"
    if test.object_file(dst) is None:
        return FAIL, f"MOVE got {answer.describe()}, but nothing is at the new path"
    missing = test.await_all(
        [
            (f"{PREFIX}/{dst}", ("object.committed", "object.updated")),
            (f"{PREFIX}/{src}", ("object.deleted",)),
        ],
        after,
        EVENT_WAIT,
    )
    return outcome(
        [f"after the MOVE, no {m}" for m in missing], f"{answer.describe()}; both events"
    )


def catalog_copy(test: Test) -> Row:
    """COPY an object that was uploaded with custom metadata to a new
    name: the copy's object.committed event must carry the source's
    fields, compared type for type. The COPY carries none of its own."""
    src, dst = f"{test.run}-copy-from", f"{test.run}-copy-to"
    test.made.add(dst)
    custom = {"step": "copy", "experiment": "pelican-testing"}
    committed(test, src, 'step="copy", experiment="pelican-testing"')
    after = time.time_ns()
    answer = web.request(
        "COPY", test.url(src), token=test.token, headers={"Destination": test.url(dst)}
    )
    if answer.status not in (201, 204):
        return FAIL, f"COPY got {answer.describe()}, not 201"
    arrived, capture = test.await_event(
        f"{PREFIX}/{dst}", "object.committed", EVENT_WAIT, after
    )
    if not arrived:
        return FAIL, f"after the COPY ({answer.describe()}), {capture}"
    where = f"{shown(CAPTURES)}/{capture}.event.json"
    got = event(capture, "object")
    wrong = [k for k, v in custom.items() if not same(got.get(k), v)]
    if wrong:
        return FAIL, f"{where} lacks the source's {', '.join(wrong)}"
    return PASS, where


def catalog_collection_delete(test: Test) -> Row:
    """DELETE a collection that holds two objects: each must get an
    object.deleted event. Pelican publishes one, for the collection's own
    path, which the note mentions (aferoFileSystem.RemoveAll in
    origin_serve/filesystem.go)."""
    collection = f"{test.run}-collection"
    children = [f"{collection}/one", f"{collection}/two"]
    for child in children:
        committed(test, child, 'step="collection"')
    after = time.time_ns()
    answer = web.request("DELETE", test.url(collection), token=test.token)
    if not answer.ok:
        return FAIL, f"DELETE of the collection got {answer.describe()}"
    missing = test.await_all(
        [(f"{PREFIX}/{child}", ("object.deleted",)) for child in children], after, EVENT_WAIT
    )
    own = [r for r in since(after) if r[2:4] == ["object.deleted", f"{PREFIX}/{collection}"]]
    also = "; one came for the collection itself" if own else ""
    return outcome(
        [f"after the collection's DELETE, no {m}{also}" for m in missing],
        f"{answer.describe()}; one for each object{also}",
    )


def catalog(test: Test) -> dict[str, Row]:
    """The events that changes to the store other than an upload send.
    The reconcile rows run later (reconcile_rows())."""
    return {
        "move": guarded(lambda: catalog_move(test)),
        "copy": guarded(lambda: catalog_copy(test)),
        "collection-delete": guarded(lambda: catalog_collection_delete(test)),
    }


def tracked(test: Test, path: str) -> bool:
    """Whether the first origin's record of objects has a live row for
    object path."""
    query = urlencode({"namespace": NAMESPACE, "path": path})
    return test.api("GET", f"{OBJECTS}/lookup?{query}", test.admin()).status == 200


def cancelled_object(test: Test) -> str:
    """reconcile-cancelled's object: an upload whose event the recorder
    always fails, cancelled through its manage URL. Its path."""
    name = f"fail-always-{test.run}-reconcile"
    test.made.add(name)
    test.held.add(name)
    answer = test.direct_put(os.urandom(4096), name, 'step="reconcile"')
    manage = issued(answer, name, "queued")[1]
    cancelled = fetch("DELETE", manage)
    if cancelled.status != 200:
        raise Problem(f"cancelling {name} through its manage URL got {cancelled.describe()}")
    return f"{PREFIX}/{name}"


def rejected_object(test: Test) -> str:
    """reconcile-rejected's object: an upload whose event the recorder
    rejects (422). Its path."""
    name = f"reject-{test.run}-reconcile"
    test.made.add(name)
    issued(test.direct_put(os.urandom(4096), name, 'step="reconcile"'), name, "rejected")
    return f"{PREFIX}/{name}"


def observed_object(test: Test) -> str:
    """reconcile-observed's object: one written straight to the first
    origin's store, which the origin then serves, and so records, as an
    object it did not write. Its path."""
    name = f"{test.run}-observed"
    test.made.add(name)
    error = stores.write_file(
        test.origin.store_of("protected-a"), f"data/metadata/{name}", os.urandom(4096)
    )
    if error:
        raise Problem(error)
    answer = web.request("HEAD", test.url(name), token=test.token)
    if answer.status != 200:
        raise Problem(f"HEAD of {name}, written to the store, got {answer.describe()}")
    return f"{PREFIX}/{name}"


def reconcile_config() -> str:
    """The reconcile restart's configuration: a sweep every
    RECONCILE_INTERVAL seconds, of objects at rest for RECONCILE_SETTLE."""
    lines = [
        "Origin:",
        "  Metadata:",
        "    ReconcileEnabled: true",
        f"    ReconcileInterval: {RECONCILE_INTERVAL}s",
        f"    ReconcileSettleWindow: {RECONCILE_SETTLE}s",
    ]
    return "\n".join(lines) + "\n"


def reconcile_rows(test: Test) -> dict[str, Row]:
    """The reconcile sweep re-enqueues an object whose publish was lost,
    as to a crash between the commit and the queue's write
    (Origin.Metadata.ReconcileEnabled), and only such an object: one this
    origin wrote, whose event never reached the catalog. Make an event
    cancelled through its manage URL, one the catalog rejected, and an
    object the origin did not write but has seen; restart the first origin
    with the sweep every few seconds; and watch the recorder for
    RECONCILE_WATCH seconds, in which no delivery for any of them may
    come. Pelican's sweep takes every live record without a publish of
    its etag and with no pending queue row (ReconcileCandidates in
    origin_serve/object_metadata.go), so it is predicted to republish all
    three, the rejected one at every sweep."""
    if test.mode != "eventual":
        only = "eventual mode only: a transactional event is never cancelled or kept rejected"
        return {case: (SKIP, only) for case in RECONCILE_CASES}
    why = unrestartable(test)
    if why:
        return {case: (SKIP, why) for case in RECONCILE_CASES}
    makers = {
        "reconcile-cancelled": cancelled_object,
        "reconcile-rejected": rejected_object,
        "reconcile-observed": observed_object,
    }
    rows: dict[str, Row] = {}
    watched: dict[str, str] = {}
    for case, maker in makers.items():
        path = ""
        try:
            path = maker(test)
        except (Problem, accounts.ApiError) as e:
            rows[case] = (FAIL, str(e))
            continue
        except Exception as e:  # pylint: disable=broad-exception-caught
            traceback.print_exc()
            rows[case] = (FAIL, f"{type(e).__name__}: {e}")
            continue
        if common.wait_for(partial(tracked, test, path), 10):
            watched[case] = path
        else:
            rows[case] = (
                INCONCLUSIVE,
                f"the first origin has no record of {path}, so the sweep has none to republish",
            )
    after = time.time_ns()

    def watch() -> None:
        time.sleep(RECONCILE_WATCH)
        for case, path in watched.items():
            found = deliveries(after, path=path)
            if found:
                rows[case] = (
                    FAIL,
                    (
                        f"the sweep republished it: {len(found)} deliveries of {found[-1][2]}"
                        f" in {RECONCILE_WATCH}s, the last answered {found[-1][1]}"
                    ),
                )
            else:
                rows[case] = (PASS, f"no delivery in {RECONCILE_WATCH}s of sweeps")

    if watched:
        failed = restart_for(test, reconcile_config(), watch)
        for case in watched:
            rows.setdefault(case, (FAIL, failed or "the restart did not finish"))
    return {case: rows[case] for case in RECONCILE_CASES}


# --------------------------------------------------------------------------
# object-metadata-cli


def refusals(test: Test, routes: Sequence[tuple[str, str]]) -> list[str]:
    """What is wrong with how the first origin answers each of routes (a
    method and a route of its web API): with no token, it must get 401,
    and as USER, an ordinary user, 403."""
    problems: list[str] = []
    user = test.user()
    for method, route in routes:
        for who, token, wanted in (("no one", None, 401), (USER, user, 403)):
            answer = test.api(method, route, token)
            if answer.status != wanted:
                problems.append(
                    f"{method} {route} as {who} got {answer.describe()}, not {wanted}"
                )
    return problems


def object_routes(path: str) -> list[tuple[str, str]]:
    """The routes of the record of objects that the CLI's `list`, `get`,
    and `history` each ask, for the namespace and object path, as their
    flags in cli_args() ask them (cmd/origin_object_metadata.go)."""
    listed = urlencode({"namespace": NAMESPACE, "limit": 5})
    looked_up = urlencode({"namespace": NAMESPACE, "path": path, "history": 5})
    history = urlencode({"namespace": NAMESPACE, "path": path, "limit": 100})
    return [
        ("list", f"{OBJECTS}?{listed}"),
        ("get", f"{OBJECTS}/lookup?{looked_up}"),
        ("history", f"{OBJECTS}/history?{history}"),
    ]


def cli_args(subcommand: str, path: str) -> list[str]:
    """The flags of `origin object-metadata <subcommand>` that ask what
    object_routes() names for it."""
    flags = {
        "list": ["--namespace", NAMESPACE, "--limit", "5"],
        "get": ["--namespace", NAMESPACE, "--path", path, "--history", "5"],
        "history": ["--namespace", NAMESPACE, "--path", path, "--limit", "100"],
    }
    return [subcommand, *flags[subcommand]]


def tracked_object(test: Test) -> tuple[str, int]:
    """Upload an object straight to the first origin, and wait until its
    record of objects has it; its path and size. Raises Problem if
    either fails."""
    name = f"{test.run}-tracked"
    test.made.add(name)
    data = os.urandom(3000)
    answer = test.direct_put(data, name, 'step="tracked"')
    if not answer.ok:
        raise Problem(f"uploading {name} got {answer.describe()}")
    path = f"{PREFIX}/{name}"
    if not common.wait_for(lambda: tracked(test, path), 10):
        raise Problem(f"the record of objects has no live row for {path}")
    return path, len(data)


def objects_api(test: Test, path: str, size: int) -> Row:
    """The record of objects' admin routes (RegisterObjectMetadataAdminAPI
    in origin_serve/object_metadata_admin.go): each refuses no one (401)
    and an ordinary user (403); the admin's lookup has the object, of its
    size, and its history a `commit`."""
    routes = dict(object_routes(path))
    problems = refusals(test, [("GET", route) for route in routes.values()])
    admin = test.admin()
    listed = test.api("GET", routes["list"], admin)
    if listed.status != 200:
        problems.append(f"the admin's list got {listed.describe()}")
    live = common.as_object(test.api("GET", routes["get"], admin).object.get("live")) or {}
    if live.get("object_path") != path or not same(live.get("size"), size):
        found = f"{live.get('object_path')!r} of {live.get('size')!r} bytes"
        problems.append(f"the admin's lookup has {found}, not {path!r} of {size}")
    history = (
        common.as_array(test.api("GET", routes["history"], admin).object.get("rows")) or []
    )
    kinds = [(common.as_object(r) or {}).get("event_type") for r in history]
    if "commit" not in kinds:
        problems.append(f"the admin's history of {path} has {kinds}, no commit")
    return outcome(problems, f"401, 403, and the admin's: the object, {size} bytes, committed")


def steady(value: Any) -> Any:
    """value, a decoded JSON answer, without the access times that the
    origin records as it pleases (`last_accessed`)."""
    found = common.as_object(value)
    if found is not None:
        return {k: steady(v) for k, v in found.items() if k != "last_accessed"}
    items = common.as_array(value)
    if items is not None:
        return [steady(v) for v in items]
    return value


def objects_cli(test: Test, path: str) -> Row:
    """`pelican-server origin object-metadata list`, `get`, and `history`
    at the first origin, with the admin's token file: each must print
    what the route it asks answers the admin."""
    server = test.session.pelican_server
    if server is None:
        return SKIP, "no pelican-server, which has `origin object-metadata`; run ./fed.sh init"
    test.admin()
    problems: list[str] = []
    for subcommand, route in object_routes(path):
        args = cli_args(subcommand, path)
        command = [server, "origin", "object-metadata", *args]
        command += ["--server", test.web, "--token", test.admin_file, "--json"]
        try:
            done = subprocess.run(  # nosec B603
                command,
                env=test.session.env,
                cwd=test.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"`{subcommand}` timed out after 120s")
            continue
        if done.returncode != 0:
            problems.append(f"`{subcommand}`: exit {done.returncode}: {last_line(done.stderr)}")
            continue
        try:
            printed = json.loads(done.stdout)
        except ValueError:
            problems.append(f"`{subcommand}` printed no JSON: {last_line(done.stdout)}")
            continue
        answer = test.api("GET", route, test.admin())
        if answer.status != 200 or steady(printed) != steady(answer.data):
            problems.append(f"`{subcommand}` printed {printed}, the API {answer.data}")
    return outcome(problems, "list, get, and history, as the API says")


def object_metadata_cli(test: Test) -> dict[str, Row]:
    """The first origin's record of objects (Origin.Metadata.TrackAccess):
    its admin API, and the CLI over it."""
    try:
        path, size = tracked_object(test)
    except Problem as e:
        return {case: (FAIL, str(e)) for case in CASES["object-metadata-cli"]}
    return {
        "api": guarded(lambda: objects_api(test, path, size)),
        "cli": guarded(lambda: objects_cli(test, path)),
    }


# --------------------------------------------------------------------------
# queue-admin


def held_event(test: Test) -> tuple[str, str]:
    """Queue an event that the recorder always fails, in either mode, as
    the first origin publishes object.deleted for what it deletes: write
    an object straight to its store, and DELETE it through the origin.
    Its event's ID and object path, once its first delivery has failed;
    raises Problem if it is not queued within EVENT_WAIT seconds."""
    name = f"fail-always-{test.run}-held"
    path = f"{PREFIX}/{name}"
    test.made.add(name)
    error = stores.write_file(
        test.origin.store_of("protected-a"), f"data/metadata/{name}", b"held\n"
    )
    if error:
        raise Problem(error)
    answer = web.request("DELETE", test.url(name), token=test.token)
    if not answer.ok:
        raise Problem(f"DELETE of {name} got {answer.describe()}")
    found: list[dict[str, Any]] = []

    def attempted() -> bool:
        found[:] = [r for r in test.rows_for(path) if int(r.get("attempts") or 0) >= 1]
        return bool(found)

    if not common.wait_for(attempted, EVENT_WAIT, 1):
        raise Problem(f"no queue row for {path}'s object.deleted, tried once, in {EVENT_WAIT}s")
    return str(found[0].get("event_id", "")), path


def queue_routes(event_id: str) -> list[tuple[str, str]]:
    """The publish queue's admin routes (RegisterMetadataAdminAPI in
    origin_serve/metadata_admin.go), for event event_id."""
    row = f"{QUEUE}/{quote(event_id)}"
    return [
        ("GET", QUEUE),
        ("GET", f"{QUEUE}/_health"),
        ("GET", row),
        ("DELETE", row),
        ("POST", f"{row}/retry"),
    ]


def queue_auth(test: Test, event_id: str) -> Row:
    """Each route refuses no one (401) and an ordinary user (403), and the
    event is still there for the admin."""
    problems = refusals(test, queue_routes(event_id))
    kept = test.api("GET", f"{QUEUE}/{quote(event_id)}", test.admin())
    if kept.status != 200:
        problems.append(f"afterward, the admin's GET of the event got {kept.describe()}")
    return outcome(problems, f"each of {len(queue_routes(event_id))} routes: 401 and 403")


def queue_views(test: Test, event_id: str, path: str) -> Row:
    """The admin's list has the event, the queue's health counts it, and
    a GET of it shows it; a GET of an event that isn't there gets 404."""
    admin = test.admin()
    problems: list[str] = []
    listed = [r for r in test.queue_rows() if r.get("event_id") == event_id]
    if not listed or listed[0].get("object_path") != path:
        problems.append(f"the admin's list lacks {event_id}, for {path}")
    health = test.api("GET", f"{QUEUE}/_health", admin)
    state = health.object.get("state")
    pending = int(
        (common.as_object(health.object.get("per_namespace")) or {}).get(NAMESPACE) or 0
    )
    if health.status != 200 or state not in ("healthy", "warning", "error") or pending < 1:
        problems.append(f"the queue's health got {health.describe()}: {health.data}")
    row = test.api("GET", f"{QUEUE}/{quote(event_id)}", admin)
    attempts = row.object.get("attempts")
    if row.status != 200 or row.object.get("object_path") != path or not attempts:
        problems.append(f"the admin's GET of {event_id} got {row.describe()}: {row.data}")
    unknown = test.api("GET", f"{QUEUE}/no-such-event-{test.run}", admin)
    if unknown.status != 404:
        problems.append(
            f"the admin's GET of an event that isn't there got {unknown.describe()}"
        )
    return outcome(
        problems, f"listed; health {state}, {pending} pending; tried {attempts} times"
    )


def queue_retry(test: Test, event_id: str) -> Row:
    """With the event's next delivery minutes away, the admin's retry must
    answer 200 and have it delivered again within RETRY_WAIT seconds.
    Pelican's handler moves the row's next attempt to now, but wakes no
    worker (retryQueueRowHandler in origin_serve/metadata_admin.go), which
    sleeps until the attempt it last saw due."""
    row = test.api("GET", f"{QUEUE}/{quote(event_id)}", test.admin())
    due = go_time(row.object.get("next_attempt_at"))
    if row.status != 200 or due is None:
        return FAIL, f"the admin's GET of {event_id} got {row.describe()}: {row.data}"
    wait = due - time.time()
    if wait < 2 * RETRY_WAIT:
        return INCONCLUSIVE, f"its next delivery is due in {wait:.0f}s anyway, too soon to tell"
    after = time.time_ns()
    answer = test.api("POST", f"{QUEUE}/{quote(event_id)}/retry", test.admin())
    if answer.status != 200:
        return FAIL, f"retry got {answer.describe()}, not 200"
    if not common.wait_for(lambda: bool(deliveries(after, event_id=event_id)), RETRY_WAIT, 1):
        return FAIL, (
            f"retry got {answer.describe()}, but the event was not delivered again in"
            f" {RETRY_WAIT}s; it was due in {wait:.0f}s"
        )
    return PASS, f"{answer.describe()}, and delivered again, though due in {wait:.0f}s"


def queue_delete(test: Test, event_id: str, path: str) -> Row:
    """The admin's DELETE of the event removes it: a GET of it gets 404,
    and the list lacks it."""
    route = f"{QUEUE}/{quote(event_id)}"
    answer = test.api("DELETE", route, test.admin())
    if answer.status not in (200, 204):
        return FAIL, f"DELETE got {answer.describe()}"
    problems: list[str] = []
    after = test.api("GET", route, test.admin())
    if after.status != 404:
        problems.append(f"afterward, the admin's GET of it got {after.describe()}, not 404")
    if test.rows_for(path):
        problems.append("afterward, the admin's list still has it")
    return outcome(problems, f"{answer.describe()}; then 404, and not listed")


def queue_admin(test: Test, unrestarted: Optional[str]) -> dict[str, Row]:
    """The publish queue's admin API, with an event held queued. Unless
    the first origin was restarted with its retries put off
    (unrestarted, why not, is None), its next delivery is seconds away,
    and the retry row skips."""
    try:
        event_id, path = held_event(test)
    except (Problem, accounts.ApiError) as e:
        return {case: (FAIL, f"holding an event: {e}") for case in CASES["queue-admin"]}
    return {
        "auth": guarded(lambda: queue_auth(test, event_id)),
        "admin": guarded(lambda: queue_views(test, event_id, path)),
        "retry": (
            (SKIP, unrestarted) if unrestarted else guarded(lambda: queue_retry(test, event_id))
        ),
        "delete": guarded(lambda: queue_delete(test, event_id, path)),
    }


def queue_off(session: Session) -> Row:
    """With publishing off, the queue's admin routes still refuse no one
    (401), and tell the admin that publishing is off (503)."""
    origin = session.fed.origins[0]
    token = web_token(session.fed, origin, session.path("tokens/metadata.admin"), "admin")
    problems: list[str] = []
    answer = accounts.Api(origin.web_url, None).call("GET", QUEUE)
    if answer.status != 401:
        problems.append(f"GET {QUEUE} with no token got {answer.describe()}, not 401")
    for route in (QUEUE, f"{QUEUE}/_health"):
        answer = accounts.Api(origin.web_url, token).call("GET", route)
        if answer.status != 503:
            problems.append(f"GET {route} as the admin got {answer.describe()}, not 503")
    return outcome(problems, "401 with no token, and 503 for the admin")


# --------------------------------------------------------------------------
# backpressure


def past_cap(test: Test, name: str, limit: str, state: str) -> Row:
    """With the namespace's queue at its cap (state says how), an ordinary
    upload of object name must be refused with a 5xx, which says to come
    back later, and leave nothing; and the origin must count the refusal,
    for the cap on limit (`rows` or `bytes`). Pelican returns the close's
    error, which x/net's WebDAV handler answers with 405."""
    test.made.add(name)
    before = metrics.fetch(test.web) or metrics.EMPTY
    answer = test.direct_put(os.urandom(4096), name, 'step="refused"')
    counted = metrics.delta(
        before, metrics.fetch(test.web) or metrics.EMPTY, BACKPRESSURE, limit=limit
    )
    problems: list[str] = []
    if answer.status is None or answer.status // 100 != 5:
        problems.append(f"with {state}, an upload got {answer.describe()}, not a 5xx")
    left = test.object_file(name)
    if left is not None:
        problems.append(f"with {state}, the refused upload left {shown(left)}")
    if counted < 1:
        problems.append(f'{BACKPRESSURE}{{limit="{limit}"}} did not rise')
    return outcome(problems, f"with {state}: {answer.describe()}, nothing left, and counted")


def cap_rows(test: Test, cap: int, filled: list[str]) -> Row:
    """Upload objects whose events the recorder always fails until the
    namespace has cap rows pending (Origin.Metadata.MaxQueuedPerNamespace),
    each of which must be taken; then an ordinary upload must be refused
    (past_cap()). filled gets the objects' names."""
    pending = test.pending()
    for n in range(cap + ROW_HEADROOM):
        if pending >= cap:
            break
        name = f"fail-always-{test.run}-fill-{n}"
        test.made.add(name)
        test.held.add(name)
        filled.append(name)
        try:
            issued(test.direct_put(os.urandom(1024), name, 'step="fill"'), name, "queued")
        except Problem as e:
            return FAIL, f"with {pending} of {cap} rows pending, {e}"
        pending = test.pending()
    if pending < cap:
        return FAIL, f"the namespace never had {cap} rows pending, but {pending}"
    return past_cap(
        test, f"{test.run}-refused-rows", "rows", f"{pending} rows pending, the cap"
    )


def with_blob(boundary: str, metadata: bytes, data: bytes) -> bytes:
    """A multipart/form-data body (RFC 7578) of the parts Pelican takes
    apart by name (splitMultipartParts in origin_serve/metadata_multipart.go):
    `metadata`, whose bytes are metadata, then `object`, data."""
    body = b""
    for name, part in (("metadata", metadata), ("object", data)):
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        body += head.encode() + part + b"\r\n"
    return body + f"--{boundary}--\r\n".encode()


def cap_bytes(test: Test, filled: list[str], blobs: list[str]) -> Row:
    """With filled's rows deleted, one upload whose event the recorder
    always fails, with a blob of BLOB_CAP bytes, must be taken, and fill
    the namespace's pending blob bytes
    (Origin.Metadata.MaxQueuedBytesPerNamespace); then an ordinary upload
    must be refused (past_cap()). blobs gets its name."""
    errors = test.drop_rows([f"{PREFIX}/{n}" for n in filled])
    if errors:
        raise Problem(f"draining the rows: {errors[0]}")
    name = f"fail-always-{test.run}-blob"
    test.made.add(name)
    test.held.add(name)
    blobs.append(name)
    boundary = f"pelican-testing-{os.urandom(12).hex()}"
    answer = web.request(
        "PUT",
        test.url(name),
        token=test.token,
        upload=with_blob(boundary, os.urandom(BLOB_CAP), b"blob\n"),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Pelican-Object-Metadata": 'step="blob"',
        },
    )
    try:
        issued(answer, name, "queued")
    except Problem as e:
        return FAIL, f"with no blob pending, {e}"
    return past_cap(
        test, f"{test.run}-refused-bytes", "bytes", f"{BLOB_CAP} blob bytes pending"
    )


def drained(test: Test, held: Sequence[str]) -> Row:
    """With the held rows deleted through the admin API, an ordinary
    upload must be taken again, and stored."""
    errors = test.drop_rows([f"{PREFIX}/{n}" for n in held])
    if errors:
        raise Problem(f"draining the queue: {errors[0]}")
    name = f"{test.run}-drained"
    test.made.add(name)
    data = os.urandom(4096)
    answer = test.direct_put(data, name, 'step="drained"')
    if not answer.ok:
        return FAIL, f"once the queue drained, an upload got {answer.describe()}"
    if not test.holds(name, data):
        return (
            FAIL,
            f"once the queue drained, an upload got {answer.describe()}, but isn't stored",
        )
    return PASS, f"{answer.describe()}, {STATUS_HEADER}: {answer.header(STATUS_HEADER)}"


def backpressure(test: Test, cap: int) -> dict[str, Row]:
    """The namespace's queue capped at cap rows and BLOB_CAP blob bytes:
    each cap refuses uploads once reached, until the queue drains."""
    filled: list[str] = []
    blobs: list[str] = []
    return {
        "rows": guarded(lambda: cap_rows(test, cap, filled)),
        "bytes": guarded(lambda: cap_bytes(test, filled, blobs)),
        "drained": guarded(lambda: drained(test, filled + blobs)),
    }


# --------------------------------------------------------------------------
# Restarting the first origin.


def unrestartable(test: Test) -> Optional[str]:
    """Why the first origin cannot be restarted with configuration of the
    suite's own, or None."""
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


def held_config(cap: Optional[int]) -> str:
    """The configuration of `queue-admin`'s and `backpressure`'s restart:
    a failed delivery is put off for HELD_BACKOFF, every time; and, given
    cap, the namespace's queue capped at cap rows and BLOB_CAP bytes."""
    lines = ["Origin:", "  Metadata:", f"    MinBackoff: {HELD_BACKOFF}"]
    lines.append(f"    MaxBackoff: {HELD_BACKOFF}")
    if cap is not None:
        lines.append(f"    MaxQueuedPerNamespace: {cap}")
        lines.append(f"    MaxQueuedBytesPerNamespace: {BLOB_CAP}")
    return "\n".join(lines) + "\n"


def serving(test: Test) -> Optional[str]:
    """Why the namespace is not served as before, or None: every origin
    must answer a HEAD of the probe, every director name every origin for
    it, and every federated cache answer a HEAD of it."""
    path = f"{PREFIX}/{test.run}-probe"
    for origin in test.fed.origins:
        answer = web.request("HEAD", f"{origin.url}{path}", token=test.token)
        if answer.status != 200:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.token)
        for origin in test.fed.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request("HEAD", f"{cache.url}{path}", token=test.token)
        if answer.status != 200:
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def settle(test: Test) -> Optional[str]:
    """Wait until the federation serves the namespace as before (see
    serving()); why it doesn't within SETTLE_WAIT seconds, or None."""
    deadline = time.time() + SETTLE_WAIT
    why = serving(test)
    while why and time.time() < deadline:
        time.sleep(5)
        why = serving(test)
    return f"after {SETTLE_WAIT}s, {why}" if why else None


def restart_for(test: Test, text: str, body: Callable[[], None]) -> Optional[str]:
    """Put the probe in every store; restart the first origin with text as
    the last of its configuration layers; run body; restore the origin,
    and wait until the federation serves again. Why the restart, or
    restoring, failed, or None."""
    name = f"{test.run}-probe"
    test.made.add(name)
    for origin in stores.unique(test.fed.origins):
        error = stores.write_file(
            origin.store_of("protected-a"), f"data/metadata/{name}", b"probe\n"
        )
        if error:
            return f"putting the probe: {error}"
    svc = test.origin.svc
    print(f"\nRestarting {svc} with:\n{text}")
    try:
        with control.restart_with(svc, {CONFIG_FILE: text}):
            body()
    except control.ControlError as e:
        warn(f"restarting {svc}, or restoring it: {e}")
        return f"restarting {svc}: {e}"
    why = settle(test)
    if why:
        warn(f"with {svc} restored, {why}")
    return None


def held_rows(test: Test, wanted: Sequence[str]) -> dict[str, Row]:
    """`queue-admin`'s and `backpressure`'s rows, of those in wanted, by
    row name: in one restart of the first origin (held_config()), or,
    where it can't be restarted, `queue-admin`'s at the origin as it is."""
    rows: dict[str, Row] = {}

    def add(scenario: str, found: dict[str, Row]) -> None:
        rows.update({f"{scenario} {case}": row for case, row in found.items()})

    why = unrestartable(test)
    if why:
        if "queue-admin" in wanted:
            add("queue-admin", queue_admin(test, why))
        if "backpressure" in wanted:
            add("backpressure", {case: (SKIP, why) for case in CASES["backpressure"]})
        return rows
    wanted = list(wanted)
    cap: Optional[int] = None
    if "backpressure" in wanted:
        wanted.remove("backpressure")
        if test.mode != "eventual":
            exempt = "transactional uploads are exempt: their events never outlive them"
            add("backpressure", {case: (SKIP, exempt) for case in CASES["backpressure"]})
        else:
            try:
                cap = test.pending() + ROW_HEADROOM
                wanted.append("backpressure")
            except Problem as e:
                add("backpressure", {case: (FAIL, str(e)) for case in CASES["backpressure"]})
    if not wanted:
        return rows

    def body() -> None:
        if "queue-admin" in wanted:
            add("queue-admin", queue_admin(test, None))
        if cap is not None:
            add("backpressure", backpressure(test, cap))

    failed = restart_for(test, held_config(cap), body)
    for scenario in wanted:
        for case in CASES[scenario]:
            rows.setdefault(
                f"{scenario} {case}", (FAIL, failed or "the restart did not finish")
            )
    return rows


# --------------------------------------------------------------------------


CHECKS: dict[str, Callable[[Test], dict[str, Row]]] = {
    "status-urls": status_urls,
    "catalog": catalog,
    "object-metadata-cli": object_metadata_cli,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: publishing, but no writes. With
    publishing off, `queue-admin off` runs."""
    if fed.metadata in ("eventual", "transactional"):
        if "Writes" not in fed.exports.get("protected-a", ()):
            return "/protected-a takes no writes"
    return None


def clean(test: Test) -> None:
    """Remove what the run made from every store, and its rows from the
    first origin's publish queue. The objects whose events the recorder
    always fails are first deleted through the origin, so that it records
    them gone, rather than find them gone later and queue an object.deleted
    that is never delivered; what it queues now goes with the rest."""
    errors: list[str] = []
    for name in sorted(test.held):
        answer = web.request("DELETE", test.url(name), token=test.token)
        if not answer.ok and answer.status != 404:
            errors.append(f"DELETE of {name}: {answer.describe()}")
    for name in sorted(test.made):
        for origin in stores.unique(test.fed.origins):
            rel = f"data/metadata/{name}"
            path = f"{origin.store_of('protected-a')}/{rel}"
            if os.path.isdir(path):
                try:
                    shutil.rmtree(path)
                except OSError as e:
                    errors.append(f"removing framework/var/{path}: {e.strerror}")
                continue
            error = stores.delete(origin, "protected-a", rel, test.token)
            if error:
                errors.append(error)
    try:
        # The deletes' events are queued as they are published: give them
        # time, twice, if there were any.
        for _ in range(2 if test.held else 1):
            if test.held:
                time.sleep(3)
            doomed = [r for r in test.queue_rows() if test.run in str(r.get("object_path", ""))]
            errors += [e for e in (test.drop(str(r.get("event_id", ""))) for r in doomed) if e]
    except (Problem, accounts.ApiError) as e:
        errors.append(str(e))
    if errors:
        warn(f"could not remove all the run made: {first(errors)}")


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Wait for the recorder, run the selected scenarios, the restarts'
    last, and remove what they made. With publishing off, run only
    `queue-admin off`."""
    if session.fed.metadata not in ("eventual", "transactional"):
        off = "metadata publishing is off ('-p origin-metadata' or '-p origin-metadata-tx')"
        for name in selected:
            if name == "queue-admin":
                results.add("queue-admin off", *guarded(lambda: queue_off(session)))
            else:
                results.add(name, SKIP, off)
        return

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
            if name in CHECKS:
                for case, row in CHECKS[name](test).items():
                    results.add(f"{name} {case}", *row)
            elif name in RUN:
                results.add(name, *guarded(partial(RUN[name], test)))
        held = [name for name in ("queue-admin", "backpressure") if name in selected]
        if held:
            for row_name, row in held_rows(test, held).items():
                results.add(row_name, *row)
        if "catalog" in selected:
            for case, row in reconcile_rows(test).items():
                results.add(f"catalog {case}", *row)
    finally:
        clean(test)
