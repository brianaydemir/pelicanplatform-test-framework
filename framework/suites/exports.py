"""What a native origin does with each export's capabilities, on its own:
Listings, DirectReads, and Copies (which Origin.DisableCopies turns
off); a third-party COPY's whole contract, its progress and its
failures, and what a failed one leaves; and a PUT of a multipart body.
These are where an origin could serve, or destroy, more than its
configuration allows.

  multipart-put            with metadata off, a PUT typed
                           multipart/form-data is stored as sent, shaped
                           as Pelican's metadata uploads are or not
  tpc-body                 a third-party COPY streams Perf Markers whose
                           byte counts never fall, then `success:
                           Created`, and the destination holds the
                           source's bytes
  tpc-failures             a COPY from a Source that is no http(s) URL
                           (400), that answers an error (502), that has
                           no such object, or that fails partway, writes
                           nothing, and leaves an object it was to
                           replace intact where uploads are staged
  disable-copies           a COPY to /public, where it takes no writes,
                           is refused; under Origin.DisableCopies, a COPY
                           to a writable export too, and OPTIONS / names
                           no COPY
  listings-capability      an export without Listings refuses a PROPFIND
                           of Depth 1 and an HTML listing, but not a
                           Depth 0 one
  direct-reads-capability  an export without DirectReads, beside others
                           with it, refuses a GET straight to the origin,
                           and serves one through a cache
  backend-token            an https or httpsv2 origin that reaches its
                           backend with a static token
                           (Origin.HttpAuthTokenFile): every request it
                           sends carries it, and never a client's, and
                           everything still works
  backend-passthrough      an httpsv2 origin that forwards the client's
                           token (Origin.HttpAuthTokenPassthrough): each
                           request of the client's carries it, and none
                           the static token

The capability scenarios, and disable-copies's `disabled` row, need
capabilities that no shape gives, so the suite restarts every origin to
give them, as the last of its configuration layers
(control.restart_with()): fed.sh's Origin.Exports restated, with
/protected-b (or, where it is not exported, /protected-a) without
Listings and /public without DirectReads, and Origin.DisableCopies.
Afterward it restores them. This needs native origins that take direct
clients, a control helper, and a topology other than `tiny`, whose
origin is the whole federation's process; elsewhere those rows skip.
The backend scenarios likewise restart the origins to reach their
backend through the `webdav-gate` service, which logs what each request
carried (see GATE_LOG).

Each scenario asks only the native origins (origin_serve/ in Pelican)
but `multipart-put` and disable-copies's `public` row, which ask an
XRootD origin too, as a control. Everything goes under
/<namespace>/data/exports/<run>/, which the suite removes afterward. A
third-party COPY's failing sources are a stub HTTP server in the dev
container (testlib/stub.py).
"""

import contextlib
import os
import re
import time
import traceback
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Optional
from urllib.parse import quote

from suites.cache_api import Findings
from suites.metadata import index
from suites.webdav import copy_or_move, root_url
from testlib import common, control, director, stores, web, webdav
from testlib.common import die, first, warn
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line
from testlib.stub import HOST as STUB_HOST
from testlib.stub import Reply, Stub

SCENARIOS = {
    "multipart-put": "with metadata off, a PUT typed multipart/form-data is stored as sent",
    "tpc-body": "a third-party COPY streams Perf Markers, then `success: Created`",
    "tpc-failures": "a COPY from a bad, failing, or missing Source writes nothing",
    "disable-copies": (
        "a COPY to /public, or under Origin.DisableCopies, is refused; OPTIONS / names no COPY"
    ),
    "listings-capability": "an export without Listings refuses Depth 1 and HTML listings",
    "direct-reads-capability": (
        "an export without DirectReads refuses a direct GET, and serves one through a cache"
    ),
    "backend-token": (
        "an HTTPS backend reached with a static token (Origin.HttpAuthTokenFile): every"
        " request carries it, and never a client's"
    ),
    "backend-passthrough": (
        "an httpsv2 backend reached with the client's token (Origin.HttpAuthTokenPassthrough):"
        " the client's requests carry it, and none the static one"
    ),
}

Row = tuple[str, str]

NAMESPACE = "protected-a"

# The rows of each scenario, in order. A scenario with cases records a
# row per case, named `<scenario> <case>`.
MULTIPART_CASES = ("pelican-shaped", "form-shaped")
FAILURE_CASES = ("bad-source", "source-error", "missing-source", "mid-stream")
ROWS: dict[str, tuple[str, ...]] = {
    "multipart-put": tuple(f"multipart-put {c}" for c in MULTIPART_CASES),
    "tpc-body": ("tpc-body",),
    "tpc-failures": tuple(f"tpc-failures {c}" for c in FAILURE_CASES),
    "disable-copies": ("disable-copies public", "disable-copies disabled"),
    "listings-capability": ("listings-capability",),
    "direct-reads-capability": ("direct-reads-capability",),
    "backend-token": ("backend-token",),
    "backend-passthrough": ("backend-passthrough",),
}
# The rows that need the origins restarted with other exports.
RESTARTED = ("disable-copies disabled", "listings-capability", "direct-reads-capability")

# A small object at every origin, as a COPY's Source where it does not
# matter, and to show that the origins serve again after a restart.
PROBE = "probe"
PROBE_SIZE = 5000
# tpc-body's source: 8 MiB, enough for the origin to send a Perf Marker
# or more (one every 2 seconds while it copies, and one at the end;
# tpc_handler.go).
SOURCE = "source"
SOURCE_SIZE = 8 << 20
# How long a COPY may go without a byte of its answer.
COPY_TIMEOUT = 300

# Pelican's metadata parts, by their names
# (Origin.Metadata.MetadataPartName and ObjectPartName; splitMultipartParts
# in origin_serve/metadata_multipart.go), and a part an HTML form sends.
METADATA_PART = b'<?xml version="1.0"?><manifest experiment="pelican-testing"/>'

# mid-stream's source claims SHORT_CLAIMED bytes, and sends SHORT_SENT.
SHORT_CLAIMED = 4 << 20
SHORT_SENT = 1 << 20
# How long the metadata recorder is given to receive an event.
EVENT_WAIT = 15

# How long the federation may take to serve again after the origins
# restart: every director must name every origin, and every cache
# answer, as the freshness suite waits.
SETTLE_WAIT = 150

# The capability restart's configuration layer: the exports restated,
# and copies off.
EXPORTS_FILE = "90-exports.yaml"
COPIES_FILE = "91-copies.yaml"
COPIES_CONFIG = "Origin:\n  DisableCopies: true\n"
# fed.sh's Origin.Exports: an origin's own, under `origin-s3`, or every
# origin's.
GENERATED_EXPORTS = (
    "generated/instance/{svc}/60-origin-exports.yaml",
    "generated/conf/60-origin-exports.yaml",
)

# The HTTPS backends' gate (`webdav-gate` in docker-compose.yaml): nginx
# in front of the `webdav` service, which relays every request and logs
# the Authorization it came with, `<seconds> <method> <path>
# "<Authorization>"` per line (`-` for none), in GATE_LOG, under
# framework/var, checking nothing. The backend scenarios restart the
# origins to reach their backend through it, as the last of their
# configuration layers (BACKEND_FILE), with the static token that
# fed.sh made in TOKEN_FILE (TOKEN_FILE_MOUNTED in the origin), or with
# the client's own.
GATE_URL = "https://webdav-gate:8444"
GATE_LOG = "data/webdav-gate/access.log"
TOKEN_FILE = "generated/http-auth-token"  # nosec B105
TOKEN_FILE_MOUNTED = "/fed/generated/http-auth-token"  # nosec B105
BACKEND_FILE = "92-backend.yaml"
# The sizes of the objects the backend scenarios move.
GATED_SIZE = 40000
# The range they read, within one.
GATED_RANGE = (100, 199)

# What every refusal of a request must be; of a listing, an origin may
# also answer that it does not allow the method (405).
REFUSED = (401, 403)
LISTING_REFUSED = (401, 403, 405)
# What a COPY to an export without Copies must get: a native origin
# routes COPY only for an export with Copies (RegisterHandlers in
# origin_serve/handlers.go), so gin answers 404 for the rest; 405 says
# the same. A COPY to /public may also be refused as a write.
NO_COPY = (404, 405)
PUBLIC_COPY_REFUSED = (401, 403, 404, 405)


class SetupError(Exception):
    """A scenario could not put in place what it checks."""


@dataclass
class Test:
    """What every scenario needs: the federation, the `server` token for
    /protected-a, the run's collection, and the stub source."""

    session: Session
    stub: Stub
    token: str
    token_file: str
    stub_down: Optional[str] = None  # why the stub does not serve, if it does not

    @property
    def fed(self) -> common.Federation:
        """The federation."""
        return self.session.fed

    @property
    def base(self) -> str:
        """The run's collection, in each namespace."""
        return f"data/exports/{self.session.run}"

    def rel(self, origin: common.Origin, name: str) -> str:
        """Object name in origin's collection of the run, in /protected-a."""
        return f"{self.base}/{origin.svc}/{name}"

    def native(self) -> Optional[str]:
        """Why the origins are not Pelican's native origins, if they are
        not."""
        if self.fed.native_origins:
            return None
        return f"XRootD serves the origins ({self.fed.origin_variant}), not Pelican"

    def writable(self) -> Optional[str]:
        """Why /protected-a cannot be written straight at the origins, if
        it cannot."""
        caps = self.fed.exports[NAMESPACE]
        if "DirectReads" not in caps:
            return "the origins take no direct clients"
        if "Writes" not in caps:
            return f"/{NAMESPACE} takes no writes"
        return None

    def put(self, origin: common.Origin, rel: str, data: bytes) -> None:
        """Put data at rel in /protected-a at origin."""
        error = stores.put(origin, NAMESPACE, rel, data, self.token)
        if error:
            raise SetupError(f"{origin.svc}: could not put {rel} in place ({error})")

    def stored(
        self,
        origin: common.Origin,
        namespace: str,
        rel: str,
        want: Optional[bytes],
        what: str,
    ) -> Optional[str]:
        """What is wrong with what origin holds at rel in namespace, which
        should be want (None: nothing), calling it what."""
        storage = origin.storage_namespace(namespace)
        token = None if storage == "public" else self.session.token(storage)
        have = stores.held(origin, namespace, rel, token)
        if want is None:
            return f"{what} is there" if have is not None else None
        if have is None:
            return f"{what} is gone"
        if have != common.digest(want):
            return f"{what} holds other bytes"
        return None

    def copy(
        self,
        origin: common.Origin,
        namespace: str,
        rel: str,
        source: str,
        source_token: Optional[str] = None,
    ) -> web.Response:
        """A third-party COPY to rel in namespace at origin, pulling from
        source, with the `server` token for /protected-a, and source_token
        for the source, if any (TransferHeaderAuthorization, which the
        origin sends the source as Authorization; forwardTransferHeaders
        in origin_serve/tpc_handler.go)."""
        headers = {"Source": source}
        if source_token is not None:
            headers["TransferHeaderAuthorization"] = f"Bearer {source_token}"
        return web.request(
            "COPY",
            stores.url(origin, namespace, rel),
            token=self.token,
            headers=headers,
            timeout=COPY_TIMEOUT,
        )


def snippet(body: bytes) -> str:
    """The start of an answer's body, for a message."""
    text = body.decode("utf-8", "replace").strip().replace("\n", " ")
    return text[:120] + ("..." if len(text) > 120 else "")


# --------------------------------------------------------------------------
# multipart-put


def multipart(boundary: str, parts: Sequence[tuple[str, bytes]]) -> bytes:
    """A multipart/form-data body (RFC 7578) of parts, each a form field's
    name and its bytes."""
    body = b""
    for name, data in parts:
        body += (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    return body + f"--{boundary}--\r\n".encode()


def multipart_case(test: Test, case: str) -> Row:
    """multipart_put()'s case, at each origin."""
    found = Findings()
    for origin in test.fed.origins:
        data = os.urandom(65536)
        boundary = f"pelican-testing-{os.urandom(12).hex()}"
        if case == "pelican-shaped":
            body = multipart(boundary, (("metadata", METADATA_PART), ("object", data)))
        else:
            body = multipart(boundary, (("file", data),))
        rel = test.rel(origin, f"multipart-{case}")
        url = stores.url(origin, NAMESPACE, rel)
        answer = web.request(
            "PUT",
            url,
            token=test.token,
            upload=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if not answer.ok:
            found.problems.append(
                f"{origin.svc}: the PUT got {answer.describe()} ({snippet(answer.body)})"
            )
            continue
        got = web.request("GET", url, token=test.token)
        if got.status != 200:
            found.problems.append(f"{origin.svc}: a GET of it got {got.describe()}")
        elif got.body == data:
            found.problems.append(f"{origin.svc}: it holds only the `object` part")
        elif got.body != body:
            found.problems.append(
                f"{origin.svc}: it holds {len(got.body)} other bytes, not the {len(body)} sent"
            )
        problem = test.stored(origin, NAMESPACE, rel, body, "its store")
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")
    return found.row(f"{len(test.fed.origins)} origin(s) stored the body as sent")


def multipart_put(test: Test) -> dict[str, Row]:
    """At each origin, PUT an object typed `multipart/form-data;
    boundary=...`, whose body is a well-formed multipart document:

      pelican-shaped  a `metadata` part, then an `object` part, as
                      Pelican's metadata uploads are
      form-shaped     one `file` part, as an HTML form sends

    then GET it: with metadata off, an origin must store each byte for
    byte as sent, as a PUT stores its body (RFC 9110, section 9.3.4).
    Pelican's native origin takes every such PUT apart
    (rewriteMultipartPUT in origin_serve/metadata_multipart.go), gated
    by Origin.Metadata.AllowMultipart, which is on by default, but not by
    Origin.Metadata.Enabled, so it stores only the first's `object`
    part, and refuses the second (400), and the rows fail there. An
    XRootD origin stores either as sent, and is the control."""
    rows = ROWS["multipart-put"]
    why = test.writable()
    if not why and test.fed.metadata != "off":
        why = (
            "the origins publish metadata, and so take a multipart PUT apart by design"
            " (Origin.Metadata.AllowMultipart)"
        )
    if why:
        return {row: (SKIP, why) for row in rows}
    return {
        row: attempt(partial(multipart_case, test, case))
        for case, row in zip(MULTIPART_CASES, rows)
    }


# --------------------------------------------------------------------------
# A third-party COPY's answer: WLCG HTTP-TPC Perf Markers, then a last
# line (tpc_handler.go):
#
#   Perf Marker
#   Stripe Index: 0
#   Stripe Bytes Transferred: <N>
#   Total Stripe Count: 1
#   End
#   ...
#   success: Created        (or failure: <why>)


@dataclass
class Progress:
    """What a COPY's answer said."""

    markers: list[int]  # each Perf Marker's Stripe Bytes Transferred, in order
    last: str  # its last line that isn't blank, or ""
    problems: list[str]  # what is malformed in it


def progress(body: bytes) -> Progress:
    """Read a COPY's answer: its Perf Markers, and its last line, which
    alone may be `success: ...` or `failure: ...`."""
    lines = [line.strip() for line in body.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    markers: list[int] = []
    problems: list[str] = []
    # The fields of the Perf Marker being read, if one is.
    fields: dict[str, str] = {}
    inside = False
    for n, line in enumerate(lines):
        if inside:
            if line == "End":
                count = fields.get("stripe bytes transferred", "")
                if count.isdigit():
                    markers.append(int(count))
                else:
                    problems.append("a Perf Marker without a Stripe Bytes Transferred")
                inside = False
                continue
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip().lower()] = value.strip()
            else:
                problems.append(f"a Perf Marker holds {line[:80]!r}")
        elif line == "Perf Marker":
            fields, inside = {}, True
        elif not (n == len(lines) - 1 and line.startswith(("success:", "failure:"))):
            problems.append(f"it holds {line[:80]!r}")
    if inside:
        problems.append("a Perf Marker has no End")
    return Progress(markers, lines[-1] if lines else "", problems)


def failed(answer: web.Response) -> Optional[str]:
    """Why a COPY's answer does not report a failure, or None: an error
    status, or a 2xx whose last line is `failure: ...`."""
    if not answer.ok:
        return None
    last = progress(answer.body).last
    if last.startswith("failure:"):
        return None
    return f"it got {answer.describe()}, ending {last[:80]!r}, not a failure"


def source_of(test: Test, origin: common.Origin) -> tuple[str, str]:
    """Where origin pulls a COPY's source in /protected-a from: the first
    federated cache's data URL, or another origin's, or its own; and its
    name."""
    caches = test.fed.federated_caches
    if caches:
        return caches[0].url, caches[0].svc
    others = [o for o in test.fed.origins if o.svc != origin.svc]
    pick = others[0] if others else origin
    return pick.url, pick.svc


# --------------------------------------------------------------------------
# tpc-body


def tpc_body(test: Test) -> dict[str, Row]:
    """At each native origin, a third-party COPY of an 8 MiB object in
    /protected-a, from the first federated cache (or another origin, or
    itself), with the `server` token for the destination and, for the
    source, in TransferHeaderAuthorization: 201, and an answer of Perf
    Markers whose Stripe Bytes Transferred never fall, nor exceed the
    object, the last the object's size, then `success: Created`; and the
    destination must then hold the object (Pelican's
    client/http_copy_test.go, TestMonitorTPC). The row's note counts the
    markers."""
    (row,) = ROWS["tpc-body"]
    why = test.native() or test.writable()
    if why:
        return {row: (SKIP, why)}
    data = os.urandom(SOURCE_SIZE)
    source_rel = f"{test.base}/{SOURCE}"
    errors = stores.put_everywhere(test.fed.origins, NAMESPACE, source_rel, data, test.token)
    if errors:
        raise SetupError(f"could not put the source in place: {first(errors)}")
    found = Findings()
    for origin in test.fed.origins:
        source_url, source_name = source_of(test, origin)
        rel = test.rel(origin, "tpc-body")
        answer = test.copy(
            origin, NAMESPACE, rel, f"{source_url}/{NAMESPACE}/{quote(source_rel)}", test.token
        )
        if answer.status != 201:
            found.problems.append(
                f"{origin.svc}: a COPY from {source_name} got {answer.describe()}"
                + f" ({snippet(answer.body)}), not 201"
            )
            continue
        said = progress(answer.body)
        found.problems += [f"{origin.svc}: {p}" for p in said.problems]
        if said.last != "success: Created":
            found.problems.append(f"{origin.svc}: the answer ends {said.last[:80]!r}")
        if any(b < a for a, b in zip(said.markers, said.markers[1:])):
            found.problems.append(f"{origin.svc}: the markers' counts fall: {said.markers}")
        if any(n > SOURCE_SIZE for n in said.markers):
            found.problems.append(
                f"{origin.svc}: a marker counts more than the object's {SOURCE_SIZE} bytes"
            )
        if said.markers and said.markers[-1] != SOURCE_SIZE:
            found.problems.append(
                f"{origin.svc}: the last marker counts {said.markers[-1]} bytes,"
                + f" not {SOURCE_SIZE}"
            )
        problem = test.stored(origin, NAMESPACE, rel, data, "the destination")
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")
        found.notes.append(
            f"{origin.svc} from {source_name}: {len(said.markers)} marker(s)"
            + ("" if said.markers else ", none")
        )
    return {row: found.row(f"{len(test.fed.origins)} origin(s)")}


# --------------------------------------------------------------------------
# tpc-failures


def bad_source(test: Test, found: Findings) -> None:
    """A Source that is not an http or https URL: 400 ("Invalid Source
    URL"), and nothing written."""
    for origin in test.fed.origins:
        for name, source in (("ftp", f"ftp://{STUB_HOST}/x"), ("file", "file:///etc/passwd")):
            rel = test.rel(origin, f"bad-source-{name}")
            answer = test.copy(origin, NAMESPACE, rel, source)
            if answer.status != 400:
                found.problems.append(
                    f"{origin.svc}: a COPY from {source} got {answer.describe()}, not 400"
                )
            problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
            if problem:
                found.problems.append(f"{origin.svc}: from {source}, {problem}")


def source_error(test: Test, found: Findings) -> None:
    """A Source that answers 404, and one that answers 500, at the stub:
    502 ("Source returned HTTP ..."), and nothing written. If the origin
    never asked the stub, its 502 shows nothing, and the row is
    inconclusive."""
    for origin in test.fed.origins:
        for code in (404, 500):
            path = f"/{test.session.run}/{origin.svc}/status-{code}"
            source = test.stub.reply(path, Reply(code, b"the stub's error\n"))
            rel = test.rel(origin, f"source-error-{code}")
            answer = test.copy(origin, NAMESPACE, rel, source)
            if not test.stub.seen(path):
                found.doubts.append(
                    f"{origin.svc} never asked the stub ({source}), and got {answer.describe()}"
                )
            elif answer.status != 502:
                found.problems.append(
                    f"{origin.svc}: a COPY from a source that answers {code} got"
                    + f" {answer.describe()}, not 502"
                )
            problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
            if problem:
                found.problems.append(
                    f"{origin.svc}: from a source that answers {code}, {problem}"
                )


def missing_source(test: Test, found: Findings) -> None:
    """A Source in /protected-a, at the first federated cache (or another
    origin, or itself), of an object that no origin holds: a failure,
    whether an error status (Pelican's is 502) or a last line of
    `failure: ...`, and nothing written. Then `pelican object copy` of
    such an object to /protected-a must exit non-zero, and no origin
    hold the destination."""
    missing = f"{test.base}/missing"
    for origin in test.fed.origins:
        source_url, source_name = source_of(test, origin)
        rel = test.rel(origin, "missing-source")
        answer = test.copy(
            origin, NAMESPACE, rel, f"{source_url}/{NAMESPACE}/{quote(missing)}", test.token
        )
        why = failed(answer)
        if why:
            found.problems.append(f"{origin.svc}: a COPY of a missing object: {why}")
        problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
        if problem:
            found.problems.append(f"{origin.svc}: from a missing object, {problem}")
        found.notes.append(f"{origin.svc} from {source_name}: {answer.describe()}")
    rel = f"{test.base}/client-missing"
    code, out, err = test.session.pelican_cmd(
        "object",
        "copy",
        "--source-token",
        test.token_file,
        "--dest-token",
        test.token_file,
        f"{test.fed.url}/{NAMESPACE}/{missing}",
        f"{test.fed.url}/{NAMESPACE}/{rel}",
    )
    if code == 0:
        found.problems.append("`pelican object copy` of a missing object exited 0")
    else:
        found.notes.append(f"`pelican object copy`: exit {code}, {last_line(err or out)}")
    for origin in stores.unique(test.fed.origins):
        problem = test.stored(
            origin, NAMESPACE, rel, None, "`pelican object copy`'s destination"
        )
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")


def mid_stream(test: Test, found: Findings) -> None:
    """Over an object X, a COPY from a stub that claims SHORT_CLAIMED
    bytes and sends SHORT_SENT, then hangs up: a failure (a 2xx whose
    last line is `failure: ...`, or an error status), and then:

      where uploads are staged (POSC, or a pstore origin)
                     X intact
      elsewhere      X intact, or nothing; never other bytes

    An origin that does not stage an upload opens its destination with
    O_TRUNC, and so has lost X before it learns that the copy fails;
    that is accepted, and the row's note says what each origin kept.
    Pelican aborts a staged upload, which keeps X, but then removes the
    destination anyway (RemoveAll, in handleCopyTPC's failure path,
    origin_serve/tpc_handler.go), so the row fails under `-p
    origin-posc` and `-p origin-pstore`. Under `-p origin-metadata`, an
    origin that kept X must publish no object.deleted for it."""
    kept: list[str] = []
    for origin in test.fed.origins:
        rel = test.rel(origin, "mid-stream")
        before = os.urandom(65536)
        test.put(origin, rel, before)
        path = f"/{test.session.run}/{origin.svc}/short"
        source = test.stub.reply(path, Reply(200, os.urandom(SHORT_SENT), length=SHORT_CLAIMED))
        answer = test.copy(origin, NAMESPACE, rel, source)
        if not test.stub.seen(path):
            found.doubts.append(
                f"{origin.svc} never asked the stub ({source}), and got {answer.describe()}"
            )
            continue
        why = failed(answer)
        if why:
            found.problems.append(
                f"{origin.svc}: a COPY from a source that fails partway: {why}"
            )
        have = stores.held(origin, NAMESPACE, rel, test.token)
        staged = test.fed.posc or origin.pstore
        if have == common.digest(before):
            kept.append(f"/{NAMESPACE}/{rel}")
            found.notes.append(f"{origin.svc} kept X")
        elif have is not None:
            found.problems.append(
                f"{origin.svc}: the object it was to replace holds other bytes"
            )
        elif staged:
            found.problems.append(
                f"{origin.svc}: the object it was to replace is gone, though uploads are staged"
            )
        else:
            found.notes.append(f"{origin.svc} lost X (uploads are not staged)")
    if kept and test.fed.metadata != "off":
        time.sleep(EVENT_WAIT)
        deleted = {row[3] for row in index() if len(row) > 3 and row[2] == "object.deleted"}
        for path in kept:
            if path in deleted:
                found.problems.append(f"the origin kept {path}, but published object.deleted")


FAILURE_CHECKS: dict[str, Callable[[Test, Findings], None]] = {
    "bad-source": bad_source,
    "source-error": source_error,
    "missing-source": missing_source,
    "mid-stream": mid_stream,
}


def failure_case(test: Test, case: str) -> Row:
    """tpc_failures()'s case, at each native origin."""
    if test.stub_down and case in ("source-error", "mid-stream"):
        return FAIL, test.stub_down
    found = Findings()
    FAILURE_CHECKS[case](test, found)
    return found.row(f"{len(test.fed.origins)} origin(s)")


def tpc_failures(test: Test) -> dict[str, Row]:
    """A row for each of FAILURE_CASES, at each native origin; see each
    case's function. Pelican's origin_serve/tpc_handler_test.go,
    TestHandleCopyTPCMidTransferFailures."""
    rows = ROWS["tpc-failures"]
    why = test.native() or test.writable()
    if why:
        return {row: (SKIP, why) for row in rows}
    return {
        row: attempt(partial(failure_case, test, case))
        for case, row in zip(FAILURE_CASES, rows)
    }


# --------------------------------------------------------------------------
# disable-copies, in the shape as it is


def copies_to_public(test: Test) -> Row:
    """At each origin, a third-party COPY to /public, which takes no
    writes, and so has no Copies (a native origin turns Copies on only
    for an export with Writes; server_utils/origin.go), from the probe,
    with /protected-a's `server` token: refused (404 or 405, as a native
    origin routes no COPY there, or 401 or 403), and nothing written.
    An XRootD origin is asked too, as a control."""
    if "public" not in test.fed.exports:
        return SKIP, "this shape does not export /public"
    if "Writes" in test.fed.exports["public"]:
        return SKIP, "/public takes writes ('-p origin-public-writes'), and so copies"
    why = test.writable()
    if why:
        return SKIP, why
    found = Findings()
    for origin in test.fed.origins:
        rel = f"{test.base}/{origin.svc}-copied"
        source = stores.url(origin, NAMESPACE, f"{test.base}/{PROBE}")
        answer = test.copy(origin, "public", rel, source, test.token)
        if answer.status not in PUBLIC_COPY_REFUSED:
            found.problems.append(
                f"{origin.svc}: a COPY to /public got {answer.describe()}, not 401, 403, 404,"
                + " or 405"
            )
        problem = test.stored(origin, "public", rel, None, "the destination in /public")
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")
        found.notes.append(f"{origin.svc}: {answer.describe()}")
    return found.row(f"{len(test.fed.origins)} origin(s) refused")


# --------------------------------------------------------------------------
# The capability restart: the origins' exports restated, with an export
# without Listings and one without DirectReads, and copies off.


@dataclass(frozen=True)
class Layout:
    """The exports that the restart changes."""

    listless: str  # the namespace without Listings
    directless: Optional[str]  # the namespace without DirectReads, if any
    caps: dict[str, list[str]]  # each changed export's capabilities, by prefix


def unrestartable(test: Test) -> Optional[str]:
    """Why the origins cannot be restarted with other exports, or None."""
    fed = test.fed
    if not fed.native_origins:
        return (
            f"XRootD serves the origins ({fed.origin_variant}); this is Pelican's native origin"
        )
    if "DirectReads" not in fed.exports[NAMESPACE]:
        return (
            "the origins take no direct clients (Origin.DisableDirectClients), with which Pelican"
            " refuses an export with DirectReads or Listings"
        )
    if fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return test.writable() or control.unavailable()


def layout_of(fed: common.Federation) -> Layout:
    """The restart's exports: /protected-b without Listings (or, where it
    is not exported, /protected-a), and /public, if exported, without
    DirectReads."""
    listless = "protected-b" if "protected-b" in fed.exports else NAMESPACE
    caps = {f"/{listless}": sorted(fed.exports[listless] - {"Listings"})}
    directless = "public" if "public" in fed.exports else None
    if directless:
        caps[f"/{directless}"] = sorted(fed.exports[directless] - {"DirectReads"})
    return Layout(listless, directless, caps)


def restate(text: str, caps: Mapping[str, Sequence[str]]) -> str:
    """fed.sh's Origin.Exports (text), with the Capabilities of each
    export in caps, by prefix, replaced. Everything else stays as fed.sh
    wrote it, since a later layer replaces a list whole."""
    lines: list[str] = []
    prefix = ""
    changed: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"\s*- FederationPrefix:\s*(\S+)\s*$", line)
        if m:
            prefix = m.group(1)
        m = re.match(r"(\s*)Capabilities:", line)
        if m and prefix in caps:
            listed = ", ".join(f'"{c}"' for c in caps[prefix])
            line = f"{m.group(1)}Capabilities: [{listed}]"
            changed.add(prefix)
        lines.append(line)
    missing = sorted(set(caps) - changed)
    if missing:
        raise SetupError(
            f"fed.sh's Origin.Exports has no Capabilities for {', '.join(missing)}"
        )
    return "\n".join(lines) + "\n"


def restart_files(svc: str, layout: Layout) -> dict[str, str]:
    """The configuration of origin svc's restart."""
    for pattern in GENERATED_EXPORTS:
        path = pattern.format(svc=svc)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            return {EXPORTS_FILE: restate(text, layout.caps), COPIES_FILE: COPIES_CONFIG}
    raise SetupError(f"framework/var/{GENERATED_EXPORTS[-1]} is missing; run ./fed.sh init")


def seed_restart(test: Test, layout: Layout) -> dict[str, bytes]:
    """Put what the restart's checks read in place, while the exports are
    as the shape has them: an object in a collection of the export
    without Listings, `tree/obj`, at every origin; and one in /public's
    storage, `public-obj`, at every store. Their bytes, by name."""
    fed = test.fed
    tree = f"{test.base}/tree"
    data = {"tree/obj": os.urandom(3000), "public-obj": os.urandom(4000)}
    token = test.session.token(layout.listless)
    errors = stores.make_dirs(fed.origins, layout.listless, tree, token)
    errors += stores.seed_everywhere(
        fed.origins,
        layout.listless,
        f"{tree}/obj",
        data["tree/obj"],
        token,
        fed.exports[layout.listless],
    )
    if layout.directless:
        rel = f"{test.base}/public-obj"
        for origin in stores.unique(fed.origins):
            storage = origin.storage_namespace(layout.directless)
            error = stores.seed(
                origin,
                storage,
                rel,
                data["public-obj"],
                test.session.token(storage),
                fed.exports[storage],
            )
            if error:
                errors.append(error)
    if errors:
        raise SetupError(f"could not put the restart's objects in place: {first(errors)}")
    return data


@contextlib.contextmanager
def restarted(
    test: Test, files: Mapping[str, Mapping[str, str]]
) -> Generator[None, None, None]:
    """Restart every origin with its files, and afterward, as it was."""
    with contextlib.ExitStack() as stack:
        for origin in test.fed.origins:
            stack.enter_context(control.restart_with(origin.svc, files[origin.svc]))
        yield


def serving(test: Test) -> Optional[str]:
    """Why /protected-a is not served as before, or None: every origin
    must answer a HEAD of the probe, every director name every origin
    for it, and every federated cache answer a HEAD of it."""
    rel = f"{test.base}/{PROBE}"
    for origin in test.fed.origins:
        answer = web.request("HEAD", stores.url(origin, NAMESPACE, rel), token=test.token)
        if answer.status != 200:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    path = f"/{NAMESPACE}/{rel}"
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.token)
        for origin in test.fed.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request("HEAD", f"{cache.url}/{NAMESPACE}/{quote(rel)}", token=test.token)
        if answer.status != 200:
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def settle(test: Test) -> Optional[str]:
    """Wait until the federation serves /protected-a as before (see
    serving()); why it doesn't within SETTLE_WAIT seconds, or None."""
    deadline = time.time() + SETTLE_WAIT
    why = serving(test)
    while why and time.time() < deadline:
        time.sleep(5)
        why = serving(test)
    return f"after {SETTLE_WAIT}s, {why}" if why else None


def copies_disabled(test: Test, _layout: Layout, _data: Mapping[str, bytes]) -> Row:
    """Under Origin.DisableCopies, at each native origin: a third-party
    COPY to /protected-a, which takes writes, from the probe, must be
    refused (404 or 405: Pelican then gives no export Copies, and routes
    no COPY; server_utils/origin.go, RegisterHandlers in
    origin_serve/handlers.go), with nothing written; and OPTIONS of the
    server's root, with no token, must answer 200, with an Allow that
    names no COPY (registerRootOptions; Pelican's
    origin_serve/root_options_test.go)."""
    found = Findings()
    for origin in test.fed.origins:
        rel = test.rel(origin, "no-copy")
        source = stores.url(origin, NAMESPACE, f"{test.base}/{PROBE}")
        answer = test.copy(origin, NAMESPACE, rel, source, test.token)
        if answer.status not in NO_COPY:
            found.problems.append(
                f"{origin.svc}: a COPY to /{NAMESPACE} got {answer.describe()}, not 404 or 405"
            )
        problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")
        options = web.request("OPTIONS", root_url(origin))
        allowed = {v.strip().upper() for v in options.header("Allow").split(",")}
        if options.status != 200:
            found.problems.append(f"{origin.svc}: OPTIONS / got {options.describe()}, not 200")
        elif "COPY" in allowed:
            found.problems.append(f"{origin.svc}: OPTIONS / allows COPY")
        found.notes.append(f"{origin.svc}: COPY {answer.describe()}")
    return found.row(f"{len(test.fed.origins)} origin(s)")


def listings_capability(test: Test, layout: Layout, data: Mapping[str, bytes]) -> Row:
    """At each native origin, of the export without Listings, with its
    `server` token: a PROPFIND of Depth 1 of a collection, and a GET of
    it with `Accept: text/html`, must be refused (401, 403, or 405); a
    PROPFIND of Depth 0 of the object in it, a stat, must answer 207,
    with its size. A PROPFIND of Depth 1 in /protected-a, which keeps
    Listings, must answer 207, so that a refusal is the export's alone.
    Nothing in Pelican's origin_serve/ reads Listings (authz.go builds
    its ACLs from PublicReads, Reads, and Writes alone), so the row fails
    there. The director already sends no deeper PROPFIND to an export
    without Listings (director/sort.go), which the listings suite
    checks; this is the origin's own check."""
    namespace = layout.listless
    token = test.session.token(namespace)
    found = Findings()
    for origin in test.fed.origins:
        tree = stores.url(origin, namespace, f"{test.base}/tree")
        listing = webdav.propfind(f"{tree}/", "1", token)
        if listing.status not in LISTING_REFUSED:
            found.problems.append(
                f"{origin.svc}: PROPFIND of Depth 1 in /{namespace} got {listing.describe()},"
                + " not 401, 403, or 405"
            )
        page = web.request("GET", f"{tree}/", token=token, headers={"Accept": "text/html"})
        if page.status not in LISTING_REFUSED:
            found.problems.append(
                f"{origin.svc}: a GET for HTML in /{namespace} got {page.describe()},"
                + " not 401, 403, or 405"
            )
        stat = webdav.propfind(f"{tree}/obj", "0", token)
        try:
            if stat.status != 207:
                raise ValueError(stat.describe())
            entries = webdav.parse(stat.body)
            if len(entries) != 1 or entries[0].size != len(data["tree/obj"]):
                raise ValueError(f"{[(e.path, e.size) for e in entries]}")
        except ValueError as e:
            found.problems.append(f"{origin.svc}: PROPFIND of Depth 0 of an object got {e}")
        if namespace != NAMESPACE:
            control_listing = webdav.propfind(
                f"{stores.url(origin, NAMESPACE, test.base)}/", "1", test.token
            )
            if control_listing.status != 207:
                found.problems.append(
                    f"{origin.svc}: PROPFIND of Depth 1 in /{NAMESPACE}, which keeps Listings,"
                    + f" got {control_listing.describe()}"
                )
        found.notes.append(
            f"{origin.svc}: Depth 1 {listing.describe()}, HTML {page.describe()}"
        )
    return found.row(f"/{namespace} without Listings")


def direct_reads_capability(test: Test, layout: Layout, data: Mapping[str, bytes]) -> Row:
    """With /public without DirectReads, while /protected-a keeps them
    (Origin.DisableDirectClients stays off): a GET of an object in
    /public straight to each native origin must be refused (401 or
    403), with none of its bytes, while one of the probe in /protected-a
    is served; and a GET of it through each federated cache must be
    served. Pelican checks DirectReads only through
    Origin.DisableDirectClients, and that only at startup
    (origin_serve/handlers.go), so the row fails there."""
    if layout.directless is None:
        return SKIP, "this shape does not export /public"
    namespace = layout.directless
    rel = f"{test.base}/public-obj"
    want = data["public-obj"]
    found = Findings()
    for origin in test.fed.origins:
        answer = web.request("GET", stores.url(origin, namespace, rel))
        if answer.status not in REFUSED:
            found.problems.append(
                f"{origin.svc}: a GET in /{namespace} straight to it got {answer.describe()},"
                + " not 401 or 403"
            )
        if want in answer.body:
            found.problems.append(f"{origin.svc}: a GET straight to it got the object's bytes")
        probe = web.request(
            "GET", stores.url(origin, NAMESPACE, f"{test.base}/{PROBE}"), token=test.token
        )
        if probe.status != 200:
            found.problems.append(
                f"{origin.svc}: a GET in /{NAMESPACE}, which keeps DirectReads, got"
                + f" {probe.describe()}"
            )
    caches = test.fed.federated_caches
    for cache in caches:
        answer = web.request("GET", f"{cache.url}/{namespace}/{quote(rel)}")
        if answer.status != 200 or answer.body != want:
            found.problems.append(
                f"{cache.svc}: a GET in /{namespace} through it got {answer.describe()}"
                + ("" if answer.status != 200 else ", with other bytes")
            )
    if not caches:
        found.notes.append("no federated cache to read it through")
    return found.row(f"/{namespace} without DirectReads")


# --------------------------------------------------------------------------
# backend-token and backend-passthrough


def ungated(test: Test, passthrough: bool) -> Optional[str]:
    """Why the origins cannot reach their backend through the gate, or
    None: their storage is no HTTPS backend; for passthrough, XRootD's
    HTTPS plugin, which has none; /protected-a that cannot be written
    straight at the origins; or no control helper."""
    fed = test.fed
    if fed.origin_variant not in ("https", "httpsv2"):
        return f"the origins' storage ({fed.origin_variant}) is no HTTPS backend"
    if passthrough and not fed.native_origins:
        return (
            "XRootD's HTTPS plugin (libXrdHTTPServer) forwards no client's token:"
            " Origin.HttpAuthTokenPassthrough is the httpsv2 backend's"
        )
    return test.writable() or control.unavailable()


def static_token() -> str:
    """The static token that fed.sh made for the origins (TOKEN_FILE)."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        raise SetupError(f"framework/var/{TOKEN_FILE} is missing; run ./fed.sh init") from None


def backend_files(svc: str, passthrough: bool) -> dict[str, str]:
    """The configuration of origin svc's restart: its backend through the
    gate, reached with the client's token, or with the static one."""
    lines = ["Origin:", f"  HttpServiceUrl: {GATE_URL}/{svc}"]
    if passthrough:
        lines.append("  HttpAuthTokenPassthrough: true")
    else:
        lines.append(f"  HttpAuthTokenFile: {TOKEN_FILE_MOUNTED}")
    return {BACKEND_FILE: "\n".join(lines) + "\n"}


def gate_log() -> list[str]:
    """The gate's log, line by line; none if there is none yet."""
    try:
        with open(GATE_LOG, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def log_entries(lines: Sequence[str]) -> list[tuple[str, str, str]]:
    """Each line's method, path, and Authorization (`-` for none)."""
    found: list[tuple[str, str, str]] = []
    for line in lines:
        m = re.match(r'\S+ (\S+) (\S+) "(.*)"$', line)
        if m:
            found.append((m.group(1), m.group(2), m.group(3)))
    return found


def describe_auth(auth: str, static: str, client: str) -> str:
    """What an Authorization in the log was, without repeating a token."""
    if auth == "-":
        return "no token"
    if auth == f"Bearer {static}":
        return "the static token"
    if auth == f"Bearer {client}":
        return "the client's token"
    return "another token"


def gated_exercise(test: Test, origin: common.Origin, mode: str) -> tuple[list[str], list[str]]:
    """Use origin's backend through the gate, straight at origin, with the
    `server` token: PUT an object with a length and one in chunks; GET
    the first whole, and a range of it, and HEAD it; PROPFIND Depth 1 of
    the run's collection, which must list both; and at a native origin,
    MKCOL a collection, MOVE the second object, and DELETE what it moved,
    which the store on disk, which the backend serves, must show. What
    went wrong, and the paths it asked for, under /protected-a."""
    problems: list[str] = []
    collection = f"{test.base}/{origin.svc}"
    declared, chunked = f"{collection}/{mode}-put", f"{collection}/{mode}-chunked"
    paths = [declared, chunked]
    data = {declared: os.urandom(GATED_SIZE), chunked: os.urandom(GATED_SIZE)}
    for rel, stream in ((declared, False), (chunked, True)):
        error = stores.put(origin, NAMESPACE, rel, data[rel], test.token, chunked=stream)
        if error:
            problems.append(error)
        problems.append(test.stored(origin, NAMESPACE, rel, data[rel], f"on disk, {rel}") or "")
    url = stores.url(origin, NAMESPACE, declared)
    answer = web.request("GET", url, token=test.token)
    if answer.status != 200 or answer.body != data[declared]:
        problems.append(f"GET {declared}: {answer.describe()}, {len(answer.body)} bytes")
    first_byte, last_byte = GATED_RANGE
    answer = web.request(
        "GET", url, token=test.token, headers={"Range": f"bytes={first_byte}-{last_byte}"}
    )
    if answer.status != 206 or answer.body != data[declared][first_byte : last_byte + 1]:
        problems.append(f"GET of a range of {declared}: {answer.describe()}")
    answer = web.request("HEAD", url, token=test.token)
    if answer.status != 200 or answer.header("Content-Length") != str(GATED_SIZE):
        problems.append(f"HEAD {declared}: {answer.describe()}")
    paths.append(collection)
    listing = stores.url(origin, NAMESPACE, collection) + "/"
    answer = webdav.propfind(listing, "1", test.token)
    try:
        if answer.status != 207:
            raise ValueError(answer.describe())
        names = set(webdav.relative(webdav.parse(answer.body), f"/{NAMESPACE}/{collection}"))
    except ValueError as e:
        problems.append(f"PROPFIND Depth 1 of {collection}: {e}")
    else:
        missing = [
            os.path.basename(p) for p in (declared, chunked) if os.path.basename(p) not in names
        ]
        if missing:
            problems.append(f"PROPFIND Depth 1 of {collection} lists no {', '.join(missing)}")
    if test.fed.native_origins:
        made = f"{collection}/{mode}-coll"
        answer = web.request("MKCOL", stores.url(origin, NAMESPACE, made), token=test.token)
        if answer.status != 201:
            problems.append(f"MKCOL {made}: {answer.describe()}, not 201")
        moved = f"{collection}/{mode}-moved"
        answer = copy_or_move(
            "MOVE",
            stores.url(origin, NAMESPACE, chunked),
            test.token,
            stores.url(origin, NAMESPACE, moved),
        )
        if answer.status not in (201, 204):
            problems.append(f"MOVE {chunked} to {moved}: {answer.describe()}, not 201 or 204")
        problems.append(
            test.stored(origin, NAMESPACE, moved, data[chunked], f"on disk, {moved}") or ""
        )
        problems.append(
            test.stored(origin, NAMESPACE, chunked, None, "on disk, the MOVE's source") or ""
        )
        answer = web.request("DELETE", stores.url(origin, NAMESPACE, moved), token=test.token)
        if not answer.ok:
            problems.append(f"DELETE {moved}: {answer.describe()}")
        problems.append(
            test.stored(origin, NAMESPACE, moved, None, f"on disk, {moved} after its DELETE")
            or ""
        )
        paths += [made, moved]
    return [f"{origin.svc}: {p}" for p in problems if p], paths


def gated_clients(test: Test, mode: str) -> tuple[list[str], str]:
    """Through the federation, with the `server` token: `pelican object
    get` of the object that gated_exercise() put at every origin, which
    a cache, or without one an origin, fetches from an origin, and so
    through the gate; and `pelican object put` of a new object, which
    lands at an origin through it. What went wrong, and the put's path
    under /protected-a."""
    problems: list[str] = []
    fed = test.fed
    origin = fed.origins[0]
    rel = f"{test.base}/{origin.svc}/{mode}-put"
    target = test.session.path(f"exports-{mode}-get")
    code, out, err = test.session.pelican_cmd(
        "object", "get", "--token", test.token_file, f"{fed.url}/{NAMESPACE}/{rel}", target
    )
    try:
        held = stores.held(origin, NAMESPACE, rel, test.token)
    except stores.Unreadable as e:
        held = None
        problems.append(str(e))
    if code != 0:
        problems.append(f"`pelican object get` of {rel}: exit {code}: {last_line(err or out)}")
    elif common.sha256(target) != held:
        problems.append(f"`pelican object get` of {rel}: the file is not the object")
    source = test.session.path(f"exports-{mode}-client")
    data = os.urandom(GATED_SIZE)
    with open(source, "wb") as f:
        f.write(data)
    uploaded = f"{test.base}/{origin.svc}/{mode}-client"
    code, out, err = test.session.pelican_cmd(
        "object", "put", "--token", test.token_file, source, f"{fed.url}/{NAMESPACE}/{uploaded}"
    )
    if code != 0:
        problems.append(
            f"`pelican object put` of {uploaded}: exit {code}: {last_line(err or out)}"
        )
    elif common.digest(data) not in stores.held_anywhere(
        fed.origins, NAMESPACE, uploaded, {NAMESPACE: test.token}
    ):
        problems.append(f"`pelican object put` of {uploaded}: no origin's store holds it")
    return problems, uploaded


def log_problems(
    test: Test,
    entries: Sequence[tuple[str, str, str]],
    static: str,
    passthrough: bool,
    direct: Sequence[str],
) -> tuple[list[str], str]:
    """What is wrong with what the gate logged since the origins were
    restarted (entries), and a note on it. With the static token, every
    request must carry it, and none a client's. With passthrough, each
    request for a path the test asked an origin for straight (direct)
    must carry the client's token, and none may carry the static one;
    the rest (the origin's own probes, and what it fetched for a cache)
    are counted. Either way, the gate must have seen a GET, a PUT, and
    a DELETE."""
    client = test.token
    if not entries:
        return [
            "the gate logged no request: the origins did not reach their backend through it"
        ], ""
    problems: list[str] = []
    if passthrough:
        asked = {f"/{NAMESPACE}/{p}" for p in direct}
        own = [e for e in entries if any(e[1].rstrip("/").endswith(p) for p in asked)]
        for method, path, auth in own:
            if auth != f"Bearer {client}":
                problems.append(
                    f"{method} {path} carried {describe_auth(auth, static, client)}, not"
                    + " the client's token"
                )
        for method, path, auth in entries:
            if auth == f"Bearer {static}":
                problems.append(f"{method} {path} carried the static token")
        without = sum(1 for _, _, auth in entries if auth == "-")
        note = (
            f"{len(entries)} requests logged: {len(own)} of the client's own, each with its"
            f" token; {without} with none"
        )
    else:
        for method, path, auth in entries:
            if auth != f"Bearer {static}":
                problems.append(
                    f"{method} {path} carried {describe_auth(auth, static, client)}, not the"
                    + " static token"
                )
        note = f"{len(entries)} requests logged, every one with the static token"
    seen = sorted({method for method, _, _ in entries})
    for method in ("GET", "PUT", "DELETE"):
        if method not in seen:
            problems.append(f"the gate saw no {method}, only {', '.join(seen)}")
    return problems, note


def gated_checks(test: Test, passthrough: bool, static: str, before: int) -> Row:
    """With the origins restarted to reach their backend through the
    gate: once the federation serves as before, use the backend straight
    at each origin (gated_exercise()) and through the federation
    (gated_clients()), then judge what the gate logged (log_problems())."""
    why = settle(test)
    if why:
        return FAIL, f"with the origins restarted, {why}"
    mode = "passthrough" if passthrough else "token"
    problems: list[str] = []
    direct: list[str] = []
    for origin in test.fed.origins:
        found, paths = gated_exercise(test, origin, mode)
        problems += found
        direct += paths
    found, uploaded = gated_clients(test, mode)
    problems += found
    direct.append(uploaded)
    entries = log_entries(gate_log()[before:])
    found, note = log_problems(test, entries, static, passthrough, direct)
    problems += found
    for problem in problems:
        print(f"    {problem}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.fed.origins)} origin(s); {note}"


def gated(test: Test, passthrough: bool) -> Row:
    """backend-token, or with passthrough backend-passthrough: restart
    every origin to reach its backend through the gate (see ungated()),
    check (gated_checks()), and restore them."""
    why = ungated(test, passthrough)
    if why:
        return SKIP, why
    static = static_token()
    files = {o.svc: backend_files(o.svc, passthrough) for o in test.fed.origins}
    how = (
        "the client's token (Origin.HttpAuthTokenPassthrough)"
        if passthrough
        else "a static token (Origin.HttpAuthTokenFile)"
    )
    print(f"\nRestarting the origins to reach their backend through {GATE_URL}, with {how}")
    before = len(gate_log())
    try:
        with restarted(test, files):
            row = attempt(partial(gated_checks, test, passthrough, static, before))
    except control.ControlError as e:
        row = (FAIL, f"restarting the origins: {e}")
        warn(f"restarting the origins to reach their backend through the gate: {e}")
    why = settle(test)
    if why:
        warn(f"with the origins restored, {why}")
    return row


RESTART_CHECKS: dict[str, Callable[[Test, Layout, Mapping[str, bytes]], Row]] = {
    "disable-copies disabled": copies_disabled,
    "listings-capability": listings_capability,
    "direct-reads-capability": direct_reads_capability,
}


def run_restarted(test: Test, rows: Sequence[str], record: Callable[[str, Row], None]) -> None:
    """Run the RESTARTED rows, with the origins restarted with the
    capability layout, and afterward restore them."""
    why = unrestartable(test)
    if why:
        for row in rows:
            record(row, (SKIP, f"needs the origins restarted with other exports, but {why}"))
        return
    layout = layout_of(test.fed)
    try:
        files = {o.svc: restart_files(o.svc, layout) for o in test.fed.origins}
        data = seed_restart(test, layout)
    except (SetupError, stores.Unreadable) as e:
        for row in rows:
            record(row, (FAIL, str(e)))
        return
    changes = ", ".join(f"{p}: {', '.join(c)}" for p, c in layout.caps.items())
    print(f"\nRestarting the origins with copies off, and {changes}")
    done: set[str] = set()
    try:
        with restarted(test, files):
            why = settle(test)
            for row in rows:
                if why:
                    record(row, (FAIL, f"with the origins restarted, {why}"))
                else:
                    record(row, attempt(partial(RESTART_CHECKS[row], test, layout, data)))
                done.add(row)
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for row in rows:
            if row not in done:
                record(row, (FAIL, f"restarting the origins: {e}"))
        warn(f"restarting the origins with other exports: {e}")
    why = settle(test)
    if why:
        warn(f"with the origins restored, {why}")


# --------------------------------------------------------------------------


def attempt(check: Callable[[], Row]) -> Row:
    """check's row; a FAIL if it could not set up, or raised."""
    try:
        return check()
    except (SetupError, stores.Unreadable) as e:
        return FAIL, str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # check fails, and the rest still run.
        traceback.print_exc()
        return FAIL, f"{type(e).__name__}: {e}"


def attempt_all(rows: Sequence[str], check: Callable[[], dict[str, Row]]) -> dict[str, Row]:
    """check's rows; each a FAIL if it could not set up, or raised."""
    try:
        return check()
    except (SetupError, stores.Unreadable) as e:
        return {row: (FAIL, str(e)) for row in rows}
    except Exception as e:  # pylint: disable=broad-exception-caught
        traceback.print_exc()
        return {row: (FAIL, f"{type(e).__name__}: {e}") for row in rows}


SCENARIO_CHECKS: dict[str, Callable[[Test], dict[str, Row]]] = {
    "multipart-put": multipart_put,
    "tpc-body": tpc_body,
    "tpc-failures": tpc_failures,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a."""
    if NAMESPACE not in fed.exports:
        return f"this shape does not export /{NAMESPACE}"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the probe in place, run the selected scenarios, those that
    need the origins restarted last, and remove what they wrote."""
    fed = session.fed
    tokens = session.tokens()
    test = Test(session, Stub(), session.token(NAMESPACE), session.token_file(NAMESPACE))

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/exports", tokens)
        if errors:
            warn(f"emptying data/exports {when}: {first(errors)}")

    def record(row: str, found: Row) -> None:
        results.add(row, *found)

    clean("before the run")
    print(f"Exports test against {fed.url}, objects under /<namespace>/{test.base}/")
    try:
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(test.stub.serving())
            except OSError as e:
                test.stub_down = f"the stub source could not listen: {e}"
                warn(test.stub_down)
            if not test.writable():
                for origin in fed.origins:
                    errors = stores.make_dirs(
                        [origin], NAMESPACE, f"{test.base}/{origin.svc}", test.token
                    )
                    if errors:
                        die(f"could not make the run's collection: {errors[0]}")
                errors = stores.put_everywhere(
                    fed.origins,
                    NAMESPACE,
                    f"{test.base}/{PROBE}",
                    os.urandom(PROBE_SIZE),
                    test.token,
                )
                if errors:
                    die(f"could not put the probe in place: {errors[0]}")
            for name in selected:
                if name in SCENARIO_CHECKS:
                    print(f"  {name} ...")
                    rows = ROWS[name]
                    found = attempt_all(rows, partial(SCENARIO_CHECKS[name], test))
                    for row in rows:
                        record(row, found[row])
            if "disable-copies" in selected:
                print("  disable-copies ...")
                record("disable-copies public", attempt(partial(copies_to_public, test)))
            later = [r for name in selected for r in ROWS[name] if r in RESTARTED]
            if later:
                run_restarted(test, later, record)
            for name in ("backend-token", "backend-passthrough"):
                if name in selected:
                    print(f"  {name} ...")
                    record(name, gated(test, name == "backend-passthrough"))
    finally:
        clean("after the run")
