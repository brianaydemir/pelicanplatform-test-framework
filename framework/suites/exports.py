"""What a native origin does with each export's capabilities, on its own.

See docs/tests/exports-and-copies.md.

No shape gives what several scenarios need, so the suite restarts
origins as the last of their configuration layers
(control.restart_with()), and restores them afterward:

  - for RESTARTED, every origin, if all are native and take direct
    clients, with fed.sh's Origin.Exports restated (/protected-b, or
    /protected-a, without Listings; /public without DirectReads) and
    Origin.DisableCopies;
  - for nested-export, every origin, XRootD's too, with an export of the
    run's own nested in /public (nest());
  - for the backend scenarios, the https and httpsv2 origins, reaching
    their backend through the `webdav-gate` service, which logs what
    each request carried (GATE_LOG);
  - for sentinel, the first origin, of any kind, with a SentinelLocation
    for every export.

Each restart needs a control helper and a topology other than `tiny`,
whose origin is the whole federation's process; elsewhere its rows skip.

Each scenario asks only the native origins (origin_serve/ in Pelican),
but multipart-put and disable-copies's `public` row, which ask XRootD
origins too, as a control; nested-export, which asks every origin; and
backend-token, which asks XRootD's `https` origins too. Objects go under
/<namespace>/data/exports/<run>/ (Test.base). A COPY's failing sources
are a stub HTTP server in the dev container (testlib/stub.py). The
requests that the scenarios check, among them the client's that
backend-passthrough forwards, present tokens of the session's profile;
setup presents WLCG tokens either way.
"""

import contextlib
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

from testlib import common, control, credentials, jwt, registry, stores, suite, web, webdav
from testlib.common import Findings, die, first, outcome, warn
from testlib.recorder import index
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line
from testlib.stub import HOST as STUB_HOST
from testlib.stub import Reply, Stub
from testlib.suite import Row
from testlib.webdav import copy_or_move, root_url

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
    "nested-export": (
        "an export nested in /public, which takes a token: the origin serves both, and the"
        " nested one to its own token alone"
    ),
    "backend-token": (
        "an HTTPS backend reached with a static token (Origin.HttpAuthTokenFile): every"
        " request carries it, and never a client's"
    ),
    "backend-passthrough": (
        "an httpsv2 backend reached with the client's token (Origin.HttpAuthTokenPassthrough):"
        " the client's requests carry it, and none the static one"
    ),
    "sentinel": (
        "an origin starts where each export's storage holds its SentinelLocation, and refuses"
        " to where one lacks it"
    ),
}

NAMESPACE = "protected-a"

# The rows of each scenario, in order. A scenario with cases records a
# row per case, named `<scenario> <case>`.
MULTIPART_CASES = ("pelican-shaped", "form-shaped")
FAILURE_CASES = ("bad-source", "source-error", "missing-source", "mid-stream")
NESTED_CASES = ("served", "token", "wrong-scope", "protected")
SENTINEL_CASES = ("present", "missing")
ROWS: dict[str, tuple[str, ...]] = {
    "multipart-put": tuple(f"multipart-put {c}" for c in MULTIPART_CASES),
    "tpc-body": ("tpc-body",),
    "tpc-failures": tuple(f"tpc-failures {c}" for c in FAILURE_CASES),
    "disable-copies": ("disable-copies public", "disable-copies disabled"),
    "listings-capability": ("listings-capability",),
    "direct-reads-capability": ("direct-reads-capability",),
    "nested-export": tuple(f"nested-export {c}" for c in NESTED_CASES),
    "backend-token": ("backend-token",),
    "backend-passthrough": ("backend-passthrough",),
    "sentinel": tuple(f"sentinel {c}" for c in SENTINEL_CASES),
}
# The rows that need the origins restarted with other exports.
RESTARTED = ("disable-copies disabled", "listings-capability", "direct-reads-capability")
# The scenarios that present no storage token of the tests' minting (see
# suites/__init__.py): sentinel's origin reads its sentinel itself.
ONE_PROFILE = ("sentinel",)

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

# The `metadata` part of a body shaped as Pelican's metadata uploads are
# (Origin.Metadata.MetadataPartName and ObjectPartName name the parts;
# splitMultipartParts in origin_serve/metadata_multipart.go).
METADATA_PART = b'<?xml version="1.0"?><manifest experiment="pelican-testing"/>'

# mid-stream's source claims SHORT_CLAIMED bytes, and sends SHORT_SENT.
SHORT_CLAIMED = 4 << 20
SHORT_SENT = 1 << 20
# How long the metadata recorder is given to receive an event.
EVENT_WAIT = 15

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

# The nested export's restart: its configuration layer, fed.sh's
# Origin.Exports with the export added (nest()); the export's name
# under the run's collection in /public, and the object beside it; and
# their size. Its tokens last a while.
NESTED_FILE = "93-nested.yaml"
NESTED = "secret"
OPEN = "open"
NESTED_SIZE = 3000
NESTED_TOKEN_LIFETIME = 600

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
TOKEN_FILE = "generated/http-auth-token"
TOKEN_FILE_MOUNTED = "/fed/generated/http-auth-token"
BACKEND_FILE = "92-backend.yaml"
# The sizes of the objects the backend scenarios move.
GATED_SIZE = 40000
# The range they read, within one.
GATED_RANGE = (100, 199)

# sentinel's restart: its configuration layer, fed.sh's Origin.Exports
# with a SentinelLocation for each export (with_sentinel()), and how
# long the first origin may take to give up when its sentinel is
# missing.
SENTINEL_FILE = "94-sentinel.yaml"
SENTINEL_WAIT = 120

# What every refusal of a request must be; of a listing, an origin may
# also answer that it does not allow the method (405).
REFUSED = (401, 403)
LISTING_REFUSED = (401, 403, 405)
# What a COPY to an export without Copies must get: 405, or 404, as from
# an origin that routes no COPY there. A COPY to /public may also be
# refused as a write.
NO_COPY = (404, 405)
PUBLIC_COPY_REFUSED = (401, 403, 404, 405)

# Why the origins cannot take an export with DirectReads or Listings.
UNDIRECTED = (
    "the origins take no direct clients (Origin.DisableDirectClients), with which Pelican"
    " refuses an export with DirectReads or Listings"
)


class SetupError(Exception):
    """A scenario could not put in place what it checks."""


# The exceptions by which a check reports a problem (suite.attempt()).
EXPECTED = (SetupError, stores.Unreadable)


class Test(suite.Test):
    """The pass, the run's collection in /protected-a, and the stub source.

    Attributes:
        stub: The stub source.
        stub_down: Why the stub does not serve, if it does not.
    """

    def __init__(self, session: Session, stub: Stub):
        super().__init__(session, "exports", NAMESPACE)
        self.stub = stub
        self.stub_down: str | None = None

    def writable(self) -> str | None:
        """Return why direct clients cannot write /protected-a, or None."""
        if not self.direct:
            return "the origins take no direct clients"
        if not self.writes:
            return f"/{NAMESPACE} takes no writes"
        return None

    def put_at(self, origin: common.Origin, rel: str, data: bytes) -> None:
        """Put data at rel in /protected-a at origin alone.

        Raises:
            SetupError: The PUT failed.
        """
        error = stores.put(origin, NAMESPACE, rel, data, self.setup)
        if error:
            raise SetupError(f"{origin.svc}: could not put {rel} in place ({error})")

    def stored(
        self,
        origin: common.Origin,
        namespace: str,
        rel: str,
        want: bytes | None,
        what: str,
    ) -> str | None:
        """Return what is wrong with what origin holds at rel in namespace.

        Args:
            want: What origin should hold there; None for nothing.
            what: What the answer calls the object.

        Returns:
            What is wrong, or None.

        Raises:
            stores.Unreadable: The origin could not say what it holds.
        """
        storage = origin.storage_namespace(namespace)
        token = None if storage == "public" else self.session.setup_token(storage)
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
        source_token: str | None = None,
    ) -> web.Response:
        """Send a third-party COPY to rel in namespace at origin.

        The origin pulls from source. The COPY carries the `server` token
        for /protected-a, and source_token for the source, if any
        (TransferHeaderAuthorization, which the origin sends the source as
        Authorization; forwardTransferHeaders in
        origin_serve/tpc_handler.go).
        """
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
    """Return the start of an answer's body, for a message."""
    text = body.decode("utf-8", "replace").strip().replace("\n", " ")
    return text[:120] + ("..." if len(text) > 120 else "")


# --------------------------------------------------------------------------
# multipart-put


def multipart(boundary: str, parts: Sequence[tuple[str, bytes]]) -> bytes:
    """Return a multipart/form-data body (RFC 7578) of parts.

    Args:
        boundary: The boundary between parts.
        parts: Each a form field's name, and its bytes.
    """
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
    """Return the row of multipart_put()'s case, at each origin."""
    found = Findings()
    for origin in test.fed.origins:
        data = os.urandom(65536)
        boundary = f"pelican-testing-{os.urandom(12).hex()}"
        if case == "pelican-shaped":
            body = multipart(boundary, (("metadata", METADATA_PART), ("object", data)))
        else:
            body = multipart(boundary, (("file", data),))
        rel = test.rel(origin.svc, f"multipart-{case}")
        url = stores.url(origin, NAMESPACE, rel)
        answer = web.request(
            "PUT",
            url,
            token=test.token,
            upload=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if not answer.ok:
            found.problems.append(f"{origin.svc}: the PUT got {answer.describe()} ({snippet(answer.body)})")
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
    """At each origin, PUT a multipart/form-data object, then GET it.

    The object is typed `multipart/form-data; boundary=...`, and its body
    is a well-formed multipart document:

      pelican-shaped  a `metadata` part, then an `object` part, as
                      Pelican's metadata uploads are
      form-shaped     one `file` part, as an HTML form sends

    With metadata off, an origin must store each byte for byte as sent,
    as a PUT stores its body (RFC 9110, section 9.3.4); taking the parts
    apart (Origin.Metadata.AllowMultipart) is for metadata publishing
    alone. XRootD origins are asked too, as a control.
    """
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
        row: suite.attempt(partial(multipart_case, test, case), EXPECTED)
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
    """What a COPY's answer said.

    Attributes:
        markers: Each Perf Marker's Stripe Bytes Transferred, in order.
        last: Its last line that isn't blank, or "".
        problems: What is malformed in it.
    """

    markers: list[int]
    last: str
    problems: list[str]


def progress(body: bytes) -> Progress:
    """Read a COPY's answer: its Perf Markers, and its last line.

    Only the last line may be `success: ...` or `failure: ...`.
    """
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


def failed(answer: web.Response) -> str | None:
    """Return why a COPY's answer does not report a failure, or None.

    It reports one by an error status, or a 2xx whose last line is
    `failure: ...`.
    """
    if not answer.ok:
        return None
    last = progress(answer.body).last
    if last.startswith("failure:"):
        return None
    return f"it got {answer.describe()}, ending {last[:80]!r}, not a failure"


def source_of(test: Test, origin: common.Origin) -> tuple[str, str]:
    """Return where origin pulls a COPY's source in /protected-a from.

    Returns:
        The first federated cache's data URL, or another origin's, or its
        own; and that server's name.
    """
    caches = test.fed.federated_caches
    if caches:
        return caches[0].url, caches[0].svc
    others = [o for o in test.fed.origins if o.svc != origin.svc]
    pick = others[0] if others else origin
    return pick.url, pick.svc


# --------------------------------------------------------------------------
# tpc-body


def tpc_body(test: Test) -> dict[str, Row]:
    """Check a third-party COPY of an 8 MiB object at each native origin.

    The COPY is in /protected-a, from the first federated cache (or
    another origin, or itself), with the `server` token for the
    destination and, for the source, in TransferHeaderAuthorization. It
    must get 201, and an answer of Perf Markers whose Stripe Bytes
    Transferred never fall, nor exceed the object, the last the object's
    size, then `success: Created`; and the destination must then hold the
    object. The row's note counts the markers.
    """
    (row,) = ROWS["tpc-body"]
    why = stores.not_native(test.fed) or test.writable()
    if why:
        return {row: (SKIP, why)}
    data = os.urandom(SOURCE_SIZE)
    source_rel = test.rel(SOURCE)
    errors = stores.put_everywhere(test.fed.origins, NAMESPACE, source_rel, data, test.setup)
    if errors:
        raise SetupError(f"could not put the source in place: {first(errors)}")
    found = Findings()
    for origin in test.fed.origins:
        source_url, source_name = source_of(test, origin)
        rel = test.rel(origin.svc, "tpc-body")
        answer = test.copy(origin, NAMESPACE, rel, test.url(source_url, source_rel), test.token)
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
                f"{origin.svc}: the last marker counts {said.markers[-1]} bytes," + f" not {SOURCE_SIZE}"
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
    """Check a COPY from a Source that is not an http or https URL.

    It must get 400 ("Invalid Source URL"), and write nothing.
    """
    for origin in test.fed.origins:
        for name, source in (("ftp", f"ftp://{STUB_HOST}/x"), ("file", "file:///etc/passwd")):
            rel = test.rel(origin.svc, f"bad-source-{name}")
            answer = test.copy(origin, NAMESPACE, rel, source)
            if answer.status != 400:
                found.problems.append(
                    f"{origin.svc}: a COPY from {source} got {answer.describe()}, not 400"
                )
            problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
            if problem:
                found.problems.append(f"{origin.svc}: from {source}, {problem}")


def source_error(test: Test, found: Findings) -> None:
    """Check COPYs from a Source at the stub that answers 404, or 500.

    Each must get 502 ("Source returned HTTP ..."), and write nothing. If
    the origin never asked the stub, its 502 shows nothing, and the row
    is inconclusive.
    """
    for origin in test.fed.origins:
        for code in (404, 500):
            path = f"/{test.run}/{origin.svc}/status-{code}"
            source = test.stub.reply(path, Reply(code, b"the stub's error\n"))
            rel = test.rel(origin.svc, f"source-error-{code}")
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
                found.problems.append(f"{origin.svc}: from a source that answers {code}, {problem}")


def missing_source(test: Test, found: Findings) -> None:
    """Check a COPY from a Source of an object that no origin holds.

    The Source is in /protected-a, at the first federated cache (or
    another origin, or itself). The COPY must fail, whether by an error
    status or a last line of `failure: ...`, and write nothing. Then
    `pelican object copy` of such an object to /protected-a must exit
    non-zero, and no origin hold the destination.
    """
    missing = test.rel("missing")
    for origin in test.fed.origins:
        source_url, source_name = source_of(test, origin)
        rel = test.rel(origin.svc, "missing-source")
        answer = test.copy(origin, NAMESPACE, rel, test.url(source_url, missing), test.token)
        why = failed(answer)
        if why:
            found.problems.append(f"{origin.svc}: a COPY of a missing object: {why}")
        problem = test.stored(origin, NAMESPACE, rel, None, "the destination")
        if problem:
            found.problems.append(f"{origin.svc}: from a missing object, {problem}")
        found.notes.append(f"{origin.svc} from {source_name}: {answer.describe()}")
    rel = test.rel("client-missing")
    code, out, err = test.session.pelican_cmd(
        "object",
        "copy",
        "--source-token",
        test.token_file,
        "--dest-token",
        test.token_file,
        test.pelican_url(missing),
        test.pelican_url(rel),
    )
    if code == 0:
        found.problems.append("`pelican object copy` of a missing object exited 0")
    else:
        found.notes.append(f"`pelican object copy`: exit {code}, {last_line(err or out)}")
    for origin in stores.unique(test.fed.origins):
        problem = test.stored(origin, NAMESPACE, rel, None, "`pelican object copy`'s destination")
        if problem:
            found.problems.append(f"{origin.svc}: {problem}")


def mid_stream(test: Test, found: Findings) -> None:
    """Check a COPY over an object X from a Source that fails partway.

    The Source is a stub that claims SHORT_CLAIMED bytes, sends
    SHORT_SENT, and hangs up. The COPY must fail (an error status, or a
    2xx whose last line is `failure: ...`), and then:

      where uploads are staged (POSC, or a pstore origin)
                     X intact
      elsewhere      X intact, or nothing; never other bytes

    An origin that does not stage an upload writes its destination in
    place, and so may lose X before it learns that the copy fails; the
    row's note says what each origin kept. Under `-p origin-metadata`,
    an origin that kept X must publish no object.deleted for it.
    """
    kept: list[str] = []
    for origin in test.fed.origins:
        rel = test.rel(origin.svc, "mid-stream")
        before = os.urandom(65536)
        test.put_at(origin, rel, before)
        path = f"/{test.run}/{origin.svc}/short"
        source = test.stub.reply(path, Reply(200, os.urandom(SHORT_SENT), length=SHORT_CLAIMED))
        answer = test.copy(origin, NAMESPACE, rel, source)
        if not test.stub.seen(path):
            found.doubts.append(
                f"{origin.svc} never asked the stub ({source}), and got {answer.describe()}"
            )
            continue
        why = failed(answer)
        if why:
            found.problems.append(f"{origin.svc}: a COPY from a source that fails partway: {why}")
        have = stores.held(origin, NAMESPACE, rel, test.setup)
        staged = test.fed.posc or origin.pstore
        if have == common.digest(before):
            kept.append(test.path(rel))
            found.notes.append(f"{origin.svc} kept X")
        elif have is not None:
            found.problems.append(f"{origin.svc}: the object it was to replace holds other bytes")
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


# Each of FAILURE_CASES: its check, which adds what it finds at every
# origin to the Findings it is given.
FAILURE_CHECKS: dict[str, Callable[[Test, Findings], None]] = {
    "bad-source": bad_source,
    "source-error": source_error,
    "missing-source": missing_source,
    "mid-stream": mid_stream,
}


def failure_case(test: Test, case: str) -> Row:
    """Return the row of tpc_failures()'s case, at each native origin."""
    if test.stub_down and case in ("source-error", "mid-stream"):
        return FAIL, test.stub_down
    found = Findings()
    FAILURE_CHECKS[case](test, found)
    return found.row(f"{len(test.fed.origins)} origin(s)")


def tpc_failures(test: Test) -> dict[str, Row]:
    """Check each of FAILURE_CASES at each native origin.

    Each case has a row; see its function.
    """
    rows = ROWS["tpc-failures"]
    why = stores.not_native(test.fed) or test.writable()
    if why:
        return {row: (SKIP, why) for row in rows}
    return {
        row: suite.attempt(partial(failure_case, test, case), EXPECTED)
        for case, row in zip(FAILURE_CASES, rows)
    }


# --------------------------------------------------------------------------
# disable-copies, in the shape as it is


def copies_to_public(test: Test) -> Row:
    """Check a third-party COPY to /public at each origin.

    /public takes no writes, and so has no Copies (Origin.DisableCopies
    gives Copies only to an export with Writes). The COPY, from the
    probe, with /protected-a's `server` token, must be refused
    (PUBLIC_COPY_REFUSED), and write nothing. XRootD origins are asked
    too, as a control.
    """
    why = suite.unexported(test.fed, "public")
    if why:
        return SKIP, why
    if "Writes" in test.fed.exports["public"]:
        return SKIP, "/public takes writes ('-p origin-public-writes'), and so copies"
    why = test.writable()
    if why:
        return SKIP, why
    found = Findings()
    for origin in test.fed.origins:
        rel = test.rel(f"{origin.svc}-copied")
        source = stores.url(origin, NAMESPACE, test.rel(PROBE))
        answer = test.copy(origin, "public", rel, source, test.token)
        if answer.status not in PUBLIC_COPY_REFUSED:
            found.problems.append(
                f"{origin.svc}: a COPY to /public got {answer.describe()}, not 401, 403, 404," + " or 405"
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
    """The exports that the restart changes.

    Attributes:
        listless: The namespace without Listings.
        directless: The namespace without DirectReads, if any.
        caps: Each changed export's capabilities, by prefix.
    """

    listless: str
    directless: str | None
    caps: dict[str, list[str]]


def unrestartable(test: Test) -> str | None:
    """Return why the origins cannot be restarted with other exports.

    Returns:
        Why, or None if they can.
    """
    why = stores.not_native(test.fed)
    if why:
        return why
    if not test.direct:
        return UNDIRECTED
    return test.writable() or control.unrestartable(test.fed)


def layout_of(fed: common.Federation) -> Layout:
    """Return the restart's exports.

    They are /protected-b without Listings (or, where it is not exported,
    /protected-a), and /public, if exported, without DirectReads.
    """
    listless = "protected-b" if "protected-b" in fed.exports else NAMESPACE
    caps = {f"/{listless}": sorted(fed.exports[listless] - {"Listings"})}
    directless = "public" if "public" in fed.exports else None
    if directless:
        caps[f"/{directless}"] = sorted(fed.exports[directless] - {"DirectReads"})
    return Layout(listless, directless, caps)


def restate(text: str, caps: Mapping[str, Sequence[str]]) -> str:
    """Return text, fed.sh's Origin.Exports, with Capabilities replaced.

    The Capabilities of each export in caps, by prefix, are replaced.
    Everything else stays as fed.sh wrote it, since a later layer
    replaces a list whole.

    Raises:
        SetupError: text has no Capabilities for an export in caps.
    """
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
        raise SetupError(f"fed.sh's Origin.Exports has no Capabilities for {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def generated_exports(svc: str) -> str:
    """Return fed.sh's Origin.Exports for origin svc (GENERATED_EXPORTS).

    Raises:
        SetupError: It is missing.
    """
    for pattern in GENERATED_EXPORTS:
        path = pattern.format(svc=svc)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    raise SetupError(f"framework/var/{GENERATED_EXPORTS[-1]} is missing; run ./fed.sh init")


def restart_files(svc: str, layout: Layout) -> dict[str, str]:
    """Return the configuration of origin svc's restart.

    Returns:
        YAML text, by file name.

    Raises:
        SetupError: fed.sh's Origin.Exports is missing, or lacks an
            export's Capabilities.
    """
    text = generated_exports(svc)
    return {EXPORTS_FILE: restate(text, layout.caps), COPIES_FILE: COPIES_CONFIG}


def seed_restart(test: Test, layout: Layout) -> dict[str, bytes]:
    """Put what the restart's checks read in place.

    Do so while the exports are as the shape has them: an object in a
    collection of the export without Listings, `tree/obj`, at every
    origin; and one in /public's storage, `public-obj`, at every store.

    Returns:
        Their bytes, by name.

    Raises:
        SetupError: An object could not be put in place.
    """
    fed = test.fed
    tree = test.rel("tree")
    data = {"tree/obj": os.urandom(3000), "public-obj": os.urandom(4000)}
    token = test.session.setup_token(layout.listless)
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
        rel = test.rel("public-obj")
        for origin in stores.unique(fed.origins):
            storage = origin.storage_namespace(layout.directless)
            error = stores.seed(
                origin,
                storage,
                rel,
                data["public-obj"],
                test.session.setup_token(storage),
                fed.exports[storage],
            )
            if error:
                errors.append(error)
    if errors:
        raise SetupError(f"could not put the restart's objects in place: {first(errors)}")
    return data


def copies_disabled(test: Test, _layout: Layout, _data: Mapping[str, bytes]) -> Row:
    """Check a native origin's COPY and OPTIONS under Origin.DisableCopies.

    At each native origin, a third-party COPY to /protected-a, which
    takes writes, from the probe, must be refused (NO_COPY), since no
    export then has Copies, and write nothing; and OPTIONS of the
    server's root, with no token, must answer 200, with an Allow that
    names no COPY.
    """
    found = Findings()
    for origin in test.fed.origins:
        rel = test.rel(origin.svc, "no-copy")
        source = stores.url(origin, NAMESPACE, test.rel(PROBE))
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
    """Check the export without Listings at each native origin.

    With the export's `server` token, a PROPFIND of Depth 1 of a
    collection, and a GET of it with `Accept: text/html`, must be refused
    (401, 403, or 405); a PROPFIND of Depth 0 of the object in it, a
    stat, must answer 207, with its size. A PROPFIND of Depth 1 in
    /protected-a, which keeps Listings, must answer 207, so that a
    refusal is the export's alone. The directors' part, sending no
    deeper PROPFIND to such an export, is the `listings` suite's; this
    is the origin's own.
    """
    namespace = layout.listless
    token = test.session.token(namespace)
    found = Findings()
    for origin in test.fed.origins:
        tree = stores.url(origin, namespace, test.rel("tree"))
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
        found.notes.append(f"{origin.svc}: Depth 1 {listing.describe()}, HTML {page.describe()}")
    return found.row(f"/{namespace} without Listings")


def direct_reads_capability(test: Test, layout: Layout, data: Mapping[str, bytes]) -> Row:
    """Check /public without DirectReads, while /protected-a keeps them.

    Origin.DisableDirectClients stays off. A GET of an object in /public
    straight to each native origin must be refused (401 or 403), with
    none of its bytes, while one of the probe in /protected-a is served;
    and a GET of it through each federated cache must be served.
    """
    if layout.directless is None:
        return SKIP, "this shape does not export /public"
    namespace = layout.directless
    rel = test.rel("public-obj")
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
        probe = web.request("GET", stores.url(origin, NAMESPACE, test.rel(PROBE)), token=test.token)
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
# nested-export


def unnestable(test: Test) -> str | None:
    """Return why no export can be nested in /public, or None."""
    why = suite.unexported(test.fed, "public")
    if why:
        return why
    if not test.direct:
        return UNDIRECTED
    return control.unrestartable(test.fed)


def nested_issuer(fed: common.Federation, prefix: str) -> str:
    """Return the issuer URL that the origins give an export of prefix.

    It is derived as fed.sh derives /protected-a's (fed_issuer_url):
    with the embedded issuer, each namespace has one of its own, under
    the origin's /api/v1.0/issuer/ns; otherwise every namespace has one,
    the origin itself, or the external issuer.
    """
    known = fed.issuer_of(NAMESPACE)
    route = f"/api/v1.0/issuer/ns/{NAMESPACE}"
    if known.endswith(route):
        return known[: -len(route)] + "/api/v1.0/issuer/ns" + prefix
    return known


def nest(text: str, prefix: str, caps: Sequence[str], issuer: str) -> str:
    """Return text, fed.sh's Origin.Exports, with an export of prefix added.

    The export is nested in /public: a copy of /public's entry, with
    prefix as its FederationPrefix, caps as its Capabilities, and what
    of prefix is below /public appended to its StoragePrefix, so that
    its storage is a directory of /public's. Where fed.sh pinned the
    exports' issuers (IssuerUrls), the export's is pinned to issuer.
    Everything else stays as fed.sh wrote it, since a later layer
    replaces a list whole.

    Raises:
        SetupError: text has no entry for /public, or one without a
            StoragePrefix.
    """
    lines = text.rstrip("\n").split("\n")
    starts = [i for i, line in enumerate(lines) if re.match(r"\s*- FederationPrefix:", line)]
    public = [i for i in starts if re.match(r"\s*- FederationPrefix:\s*/public\s*$", lines[i])]
    if not public:
        raise SetupError("fed.sh's Origin.Exports has no entry for /public")
    start = public[0]
    end = next((i for i in starts if i > start), len(lines))
    below = prefix[len("/public") :]
    listed = ", ".join(f'"{c}"' for c in caps)
    entry: list[str] = []
    for line in lines[start:end]:
        if not line.strip() or re.match(r"\s*(IssuerUrls|IssuerJwks):", line):
            continue
        if re.match(r"\s*- FederationPrefix:", line):
            line = re.sub(r"(FederationPrefix:\s*).*", lambda m: m.group(1) + prefix, line)
        elif re.match(r"\s*Capabilities:", line):
            line = re.sub(r"(Capabilities:\s*).*", lambda m: f"{m.group(1)}[{listed}]", line)
            entry.append(line)
            if "IssuerUrls:" in text:
                indent = line[: len(line) - len(line.lstrip())]
                entry.append(f'{indent}IssuerUrls: ["{issuer}"]')
            continue
        elif re.match(r"\s*StoragePrefix:", line):
            m = re.match(r"(\s*StoragePrefix:\s*)(\S+)", line)
            if m is None:
                raise SetupError("fed.sh's Origin.Exports gives /public no StoragePrefix")
            line = f"{m.group(1)}{m.group(2).rstrip('/')}{below}"
        entry.append(line)
    return "\n".join(lines) + "\n\n" + "\n".join(entry) + "\n"


def nested_files(svc: str, prefix: str, caps: Sequence[str], issuer: str) -> dict[str, str]:
    """Return the configuration of origin svc's restart with the nested export.

    Returns:
        YAML text, by file name.

    Raises:
        SetupError: fed.sh's Origin.Exports is missing, or has no /public.
    """
    return {NESTED_FILE: nest(generated_exports(svc), prefix, caps, issuer)}


def seed_nested(test: Test) -> dict[str, bytes]:
    """Put the nested export's objects in /public's storage, at every store.

    Do so while the exports are as the shape has them: OPEN, beside the
    nested export, and NESTED/obj, inside it.

    Returns:
        Their bytes, by name.

    Raises:
        SetupError: An object could not be put in place.
    """
    data = {OPEN: os.urandom(NESTED_SIZE), f"{NESTED}/obj": os.urandom(NESTED_SIZE)}
    errors: list[str] = []
    for origin in stores.unique(test.fed.origins):
        storage = origin.storage_namespace("public")
        token = test.session.setup_token(storage)
        errors += stores.make_dirs([origin], storage, test.rel(NESTED), token)
        for name, content in data.items():
            error = stores.seed(
                origin,
                storage,
                test.rel(name),
                content,
                token,
                test.fed.exports[storage],
            )
            if error:
                errors.append(error)
    if errors:
        raise SetupError(f"could not put the nested export's objects in place: {first(errors)}")
    return data


def nested_token(test: Test, prefix: str, scope_path: str) -> str:
    """Return a read token for the export of prefix, of the session's profile.

    It is like the `server` token, but names the issuer that the origins
    give the export (nested_issuer()), and is scoped to scope_path within
    it. The origins' key signs it.
    """
    fed = test.fed
    body = credentials.server_claims(
        fed,
        NAMESPACE,
        ("read",),
        scope_path,
        NESTED_TOKEN_LIFETIME,
        profile=test.session.profile,
    )
    body["iss"] = nested_issuer(fed, prefix)
    return jwt.sign(body, fed.origin_key)


def unregister(test: Test, prefix: str) -> str | None:
    """Remove prefix's registration, which the origins made as they started.

    A DELETE of /api/v1.0/registry/<prefix> takes a token of
    pelican.namespace_delete signed with a key registered for the prefix,
    here the origins' (deleteNamespaceHandler in registry/registry.go),
    as `pelican namespace delete` mints one. A prefix never registered
    (400), as when the origins never started with the export, or one
    already gone (404), leaves nothing to do.

    Returns:
        Why the registration stays, or None if it is gone, or there is
        no registry (a standalone origin).
    """
    if not test.fed.registry_url:
        return None
    url = f"{test.fed.registry_url}/api/v1.0/registry{quote(prefix, safe='/:')}"
    token = registry.token(
        test.fed.origin_key, registry.NAMESPACE_DELETE, "origin", url, audience="registry"
    )
    answer = web.request("DELETE", url, token=token)
    if answer.status in (200, 400, 404):
        return None
    return f"DELETE of {prefix}'s registration got {answer.describe()}"


def nested_get(test: Test, origin: common.Origin, name: str, token: str | None) -> web.Response:
    """GET object name of the nested export's collection at origin, in /public."""
    return web.request("GET", stores.url(origin, "public", test.rel(name)), token=token)


def nested_served(test: Test, prefix: str, data: Mapping[str, bytes], _token: str, _wrong: str) -> Row:
    """Check that each origin serves OPEN, beside the nested export, to anyone.

    /public is as it was, so a GET with no token must get the object.
    """
    found = Findings()
    for origin in test.fed.origins:
        answer = nested_get(test, origin, OPEN, None)
        if answer.status != 200:
            found.problems.append(f"{origin.svc}: a GET of {OPEN} with no token got {answer.describe()}")
        elif answer.body != data[OPEN]:
            found.problems.append(f"{origin.svc}: a GET of {OPEN} with no token got other bytes")
    return found.row(f"/public beside {prefix}, at {len(test.fed.origins)} origin(s)")


def nested_with_token(test: Test, prefix: str, data: Mapping[str, bytes], token: str, _wrong: str) -> Row:
    """Check that each origin serves the nested export's object to its token.

    A GET of NESTED/obj with a read token for the export, from its
    issuer, must get the object.
    """
    found = Findings()
    for origin in test.fed.origins:
        answer = nested_get(test, origin, f"{NESTED}/obj", token)
        if answer.status != 200:
            found.problems.append(f"{origin.svc}: a GET with the export's token got {answer.describe()}")
        elif answer.body != data[f"{NESTED}/obj"]:
            found.problems.append(f"{origin.svc}: a GET with the export's token got other bytes")
    return found.row(f"{prefix}, with its token, at {len(test.fed.origins)} origin(s)")


def nested_refused(test: Test, token: str | None, data: Mapping[str, bytes], how: str) -> Findings:
    """Return what is wrong with each origin's answer to a GET it must refuse.

    A GET of NESTED/obj with token (how says what it is) must be refused
    (401 or 403), with none of the object's bytes.
    """
    found = Findings()
    for origin in test.fed.origins:
        answer = nested_get(test, origin, f"{NESTED}/obj", token)
        if answer.status not in REFUSED:
            found.problems.append(f"{origin.svc}: a GET {how} got {answer.describe()}, not 401 or 403")
        if data[f"{NESTED}/obj"] in answer.body:
            found.problems.append(f"{origin.svc}: a GET {how} got the object's bytes")
    return found


def nested_wrong_scope(test: Test, prefix: str, data: Mapping[str, bytes], _token: str, wrong: str) -> Row:
    """Check that a token of the export's issuer scoped elsewhere is refused."""
    found = nested_refused(test, wrong, data, "with a token for /elsewhere/ in the export")
    return found.row(f"{prefix}, with a token scoped elsewhere, at {len(test.fed.origins)} origin(s)")


def nested_protected(test: Test, prefix: str, data: Mapping[str, bytes], _token: str, _wrong: str) -> Row:
    """Check that no origin serves the nested export's object to no token.

    The export takes a token, though it is nested in /public, which
    anyone may read.
    """
    found = nested_refused(test, None, data, "with no token")
    return found.row(f"{prefix}, with no token, at {len(test.fed.origins)} origin(s)")


NestedCheck = Callable[[Test, str, Mapping[str, bytes], str, str], Row]
NESTED_CHECKS: dict[str, NestedCheck] = {
    "served": nested_served,
    "token": nested_with_token,
    "wrong-scope": nested_wrong_scope,
    "protected": nested_protected,
}


def nested_export(test: Test) -> dict[str, Row]:
    """Check an export nested in /public, which takes a token, at each origin.

    An operator may nest a protected prefix in a public one on one
    origin, as another owner may register one inside it (the `auth`
    suite's `nested-protected`, under `-p topo-multi-owner`); Pelican
    picks the export of a path by its longest prefix. So every origin,
    XRootD's too, is restarted with the shape's exports and one more,
    nested in /public, /public/<the run's collection>/secret, with
    /protected-a's capabilities (nested_files()), after the objects it
    serves are put in /public's storage (seed_nested()). Each origin
    must then serve /public as before (`served`), and the export's
    object to a token for the export (`token`), but not to a token of
    its issuer scoped elsewhere (`wrong-scope`), nor to no token
    (`protected`). Afterward the origins are restored, and the export's
    registration, which they made as they started, is removed
    (unregister()).
    """
    rows = ROWS["nested-export"]
    why = unnestable(test)
    if why:
        return {row: (SKIP, f"needs the origins restarted with a nested export, but {why}") for row in rows}
    fed = test.fed
    prefix = f"/public/{test.rel(NESTED)}"
    caps = sorted(fed.exports[NAMESPACE])
    issuer = nested_issuer(fed, prefix)
    try:
        files = {o.svc: nested_files(o.svc, prefix, caps, issuer) for o in fed.origins}
        data = seed_nested(test)
    except SetupError as e:
        return {row: (FAIL, str(e)) for row in rows}
    print(f"\nRestarting the origins with {prefix} exported, nested in /public")
    found: dict[str, Row] = {}
    try:
        with control.restart_all_with(files):
            why = test.settle(test.rel(PROBE))
            token = nested_token(test, prefix, "/")
            wrong = nested_token(test, prefix, "/elsewhere/")
            for case in NESTED_CASES:
                if why:
                    found[f"nested-export {case}"] = (
                        FAIL,
                        f"with the origins restarted, {why}",
                    )
                else:
                    found[f"nested-export {case}"] = suite.attempt(
                        partial(NESTED_CHECKS[case], test, prefix, data, token, wrong), EXPECTED
                    )
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for row in rows:
            found.setdefault(row, (FAIL, f"restarting the origins with {prefix} nested in /public: {e}"))
        warn(f"restarting the origins with {prefix} nested in /public: {e}")
    why = test.settle(test.rel(PROBE))
    if why:
        warn(f"with the origins restored, {why}")
    error = unregister(test, prefix)
    if error:
        warn(f"{prefix} stays registered: {error}")
    return found


# --------------------------------------------------------------------------
# backend-token and backend-passthrough


def ungated(test: Test, passthrough: bool) -> str | None:
    """Return why the origins cannot reach their backend through the gate.

    Args:
        passthrough: Whether they are to forward the client's token.

    Returns:
        Why, or None if they can.
    """
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
    """Return the static token fed.sh made for the origins (TOKEN_FILE).

    Raises:
        SetupError: TOKEN_FILE cannot be read.
    """
    try:
        return common.read_text(TOKEN_FILE)
    except OSError:
        raise SetupError(f"framework/var/{TOKEN_FILE} is missing; run ./fed.sh init") from None


def backend_files(svc: str, passthrough: bool) -> dict[str, str]:
    """Return the configuration of origin svc's restart.

    The origin reaches its backend through the gate, with the client's
    token if passthrough, or else with the static one.

    Returns:
        YAML text, by file name.
    """
    lines = ["Origin:", f"  HttpServiceUrl: {GATE_URL}/{svc}"]
    if passthrough:
        lines.append("  HttpAuthTokenPassthrough: true")
    else:
        lines.append(f"  HttpAuthTokenFile: {TOKEN_FILE_MOUNTED}")
    return {BACKEND_FILE: "\n".join(lines) + "\n"}


def gate_log() -> list[str]:
    """Return the gate's log, line by line; none if there is none yet."""
    try:
        with open(GATE_LOG, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def log_entries(lines: Sequence[str]) -> list[tuple[str, str, str]]:
    """Return each line's method, path, and Authorization (`-` for none)."""
    found: list[tuple[str, str, str]] = []
    for line in lines:
        m = re.match(r'\S+ (\S+) (\S+) "(.*)"$', line)
        if m:
            found.append((m.group(1), m.group(2), m.group(3)))
    return found


def describe_auth(auth: str, static: str, client: str) -> str:
    """Return what an Authorization in the log was, repeating no token."""
    if auth == "-":
        return "no token"
    if auth == f"Bearer {static}":
        return "the static token"
    if auth == f"Bearer {client}":
        return "the client's token"
    return "another token"


def gated_exercise(test: Test, origin: common.Origin, mode: str) -> tuple[list[str], list[str]]:
    """Use origin's backend through the gate, straight at origin.

    With the `server` token, PUT an object with a length and one in
    chunks; GET the first whole, and a range of it, and HEAD it; PROPFIND
    Depth 1 of the run's collection, which must list both; and at a
    native origin, MKCOL a collection, MOVE the second object, and DELETE
    what it moved, which the store on disk, which the backend serves,
    must show.

    Returns:
        What went wrong, and the paths it asked for, under /protected-a.
    """
    problems: list[str] = []
    collection = test.rel(origin.svc)
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
    answer = web.request("GET", url, token=test.token, headers={"Range": f"bytes={first_byte}-{last_byte}"})
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
        missing = [os.path.basename(p) for p in (declared, chunked) if os.path.basename(p) not in names]
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
        problems.append(test.stored(origin, NAMESPACE, moved, data[chunked], f"on disk, {moved}") or "")
        problems.append(test.stored(origin, NAMESPACE, chunked, None, "on disk, the MOVE's source") or "")
        answer = web.request("DELETE", stores.url(origin, NAMESPACE, moved), token=test.token)
        if not answer.ok:
            problems.append(f"DELETE {moved}: {answer.describe()}")
        problems.append(
            test.stored(origin, NAMESPACE, moved, None, f"on disk, {moved} after its DELETE") or ""
        )
        paths += [made, moved]
    return [f"{origin.svc}: {p}" for p in problems if p], paths


def gated_clients(test: Test, mode: str) -> tuple[list[str], str]:
    """Get and put objects through the federation, and so through the gate.

    With the `server` token, run `pelican object get` of the object that
    gated_exercise() put at every origin, which a cache, or without one
    an origin, fetches from an origin, and so through the gate; and
    `pelican object put` of a new object, which lands at an origin
    through it.

    Returns:
        What went wrong, and the put's path under /protected-a.
    """
    problems: list[str] = []
    fed = test.fed
    origin = fed.origins[0]
    rel = test.rel(origin.svc, f"{mode}-put")
    target = os.path.join(test.tmp, f"{mode}-get")
    code, out, err = test.session.pelican_cmd(
        "object", "get", "--token", test.token_file, test.pelican_url(rel), target
    )
    try:
        held = stores.held(origin, NAMESPACE, rel, test.setup)
    except stores.Unreadable as e:
        held = None
        problems.append(str(e))
    if code != 0:
        problems.append(f"`pelican object get` of {rel}: exit {code}: {last_line(err or out)}")
    elif common.sha256(target) != held:
        problems.append(f"`pelican object get` of {rel}: the file is not the object")
    data = os.urandom(GATED_SIZE)
    source = test.local(f"{mode}-client", data)
    uploaded = test.rel(origin.svc, f"{mode}-client")
    code, out, err = test.session.pelican_cmd(
        "object", "put", "--token", test.token_file, source, test.pelican_url(uploaded)
    )
    if code != 0:
        problems.append(f"`pelican object put` of {uploaded}: exit {code}: {last_line(err or out)}")
    elif common.digest(data) not in stores.held_anywhere(
        fed.origins, NAMESPACE, uploaded, {NAMESPACE: test.setup}
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
    """Return what is wrong with what the gate logged, and a note on it.

    With the static token, every request must carry it, and none a
    client's. With passthrough, each request for a path that the test
    asked an origin for straight must carry the client's token, and none
    may carry the static one; the rest (the origin's own probes, and what
    it fetched for a cache) are counted. Either way, the gate must have
    seen a GET, a PUT, and a DELETE.

    Args:
        entries: What the gate logged since the origins were restarted.
        static: The static token.
        passthrough: Whether the origins forward the client's token.
        direct: The paths that the test asked an origin for straight.
    """
    client = test.token
    if not entries:
        return ["the gate logged no request: the origins did not reach their backend through it"], ""
    problems: list[str] = []
    if passthrough:
        asked = {test.path(p) for p in direct}
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
    """Use the backend through the gate, and judge what the gate logged.

    With the origins restarted to reach their backend through the gate,
    and once the federation serves again, use the backend straight at
    each origin (gated_exercise()) and through the federation
    (gated_clients()), then judge what the gate logged (log_problems()).

    Args:
        passthrough: Whether the origins forward the client's token.
        static: The static token.
        before: How many lines the gate had logged before the restart.
    """
    why = test.settle(test.rel(PROBE))
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
    return outcome(problems, f"{len(test.fed.origins)} origin(s); {note}")


def gated(test: Test, passthrough: bool) -> Row:
    """Check backend-token, or with passthrough backend-passthrough.

    Restart every origin to reach its backend through the gate (see
    ungated()), check (gated_checks()), and restore them.
    """
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
        with control.restart_all_with(files):
            row = suite.attempt(partial(gated_checks, test, passthrough, static, before), EXPECTED)
    except control.ControlError as e:
        row = (FAIL, f"restarting the origins: {e}")
        warn(f"restarting the origins to reach their backend through the gate: {e}")
    why = test.settle(test.rel(PROBE))
    if why:
        warn(f"with the origins restored, {why}")
    return row


# Each of the RESTARTED rows: its check, which, given the restart's
# Layout and the bytes of what seed_restart() put, by name, returns the
# row.
RESTART_CHECKS: dict[str, Callable[[Test, Layout, Mapping[str, bytes]], Row]] = {
    "disable-copies disabled": copies_disabled,
    "listings-capability": listings_capability,
    "direct-reads-capability": direct_reads_capability,
}


def run_restarted(test: Test, rows: Sequence[str], record: Callable[[str, Row], None]) -> None:
    """Run the RESTARTED rows with the origins restarted, then restore them.

    Args:
        rows: The rows to run.
        record: Takes each row, and its result.
    """
    why = unrestartable(test)
    if why:
        for row in rows:
            record(row, (SKIP, f"needs the origins restarted with other exports, but {why}"))
        return
    layout = layout_of(test.fed)
    try:
        files = {o.svc: restart_files(o.svc, layout) for o in test.fed.origins}
        data = seed_restart(test, layout)
    except EXPECTED as e:
        for row in rows:
            record(row, (FAIL, str(e)))
        return
    changes = ", ".join(f"{p}: {', '.join(c)}" for p, c in layout.caps.items())
    print(f"\nRestarting the origins with copies off, and {changes}")
    done: set[str] = set()
    try:
        with control.restart_all_with(files):
            why = test.settle(test.rel(PROBE))
            for row in rows:
                if why:
                    record(row, (FAIL, f"with the origins restarted, {why}"))
                else:
                    record(row, suite.attempt(partial(RESTART_CHECKS[row], test, layout, data), EXPECTED))
                done.add(row)
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for row in rows:
            if row not in done:
                record(row, (FAIL, f"restarting the origins: {e}"))
        warn(f"restarting the origins with other exports: {e}")
    why = test.settle(test.rel(PROBE))
    if why:
        warn(f"with the origins restored, {why}")


# --------------------------------------------------------------------------
# sentinel


def with_sentinel(text: str, name: str) -> str:
    """Return text, fed.sh's Origin.Exports, with a SentinelLocation each.

    Each export names name, a file in its storage (beneath its
    StoragePrefix), without which the origin must refuse to start.

    Raises:
        SetupError: text lists no export.
    """
    lines: list[str] = []
    for line in text.splitlines():
        lines.append(line)
        m = re.match(r"(\s*)- FederationPrefix:", line)
        if m:
            lines.append(f"{m.group(1)}  SentinelLocation: {name}")
    if len(lines) == len(text.splitlines()):
        raise SetupError("fed.sh's Origin.Exports lists no export")
    return "\n".join(lines) + "\n"


def sentinel_storages(test: Test, origin: common.Origin) -> list[str]:
    """Return the namespaces whose storage holds origin's exports' sentinel.

    Each export's own, but a pstore's /public, which reads /protected-a's.
    """
    found = {origin.storage_namespace(ns) for ns in test.fed.exports}
    return sorted(found)


def sentinel_present(test: Test, origin: common.Origin, text: str) -> Row:
    """Restart origin with a sentinel that each export's storage holds.

    It must start, and serve the probe again.
    """
    name = f"sentinel-{test.run}"
    for storage in sentinel_storages(test, origin):
        error = stores.seed(
            origin,
            storage,
            name,
            b"mounted\n",
            test.session.setup_token(storage),
            test.fed.exports[storage],
        )
        if error:
            raise SetupError(f"could not put the sentinel in place: {error}")
    try:
        with control.restart_with(origin.svc, {SENTINEL_FILE: with_sentinel(text, name)}):
            answer = web.request("HEAD", stores.url(origin, NAMESPACE, test.rel(PROBE)), token=test.setup)
            if answer.status != 200:
                return (
                    FAIL,
                    f"with the sentinel in place, HEAD of the probe got {answer.describe()}",
                )
    except control.ControlError as e:
        return FAIL, f"with the sentinel in place: {e}"
    finally:
        for storage in sentinel_storages(test, origin):
            stores.delete(origin, storage, name, test.session.setup_token(storage))
    return PASS, f"{origin.svc} started, and serves"


def sentinel_missing(test: Test, origin: common.Origin, text: str) -> Row:
    """Restart origin with a sentinel that no storage holds.

    It must exit within SENTINEL_WAIT seconds, rather than serve exports
    whose storage may not be there.
    """
    name = f"sentinel-{test.run}-missing"
    try:
        with control.restart_with(origin.svc, {SENTINEL_FILE: with_sentinel(text, name)}, serves=False):
            started = time.monotonic()
            if common.wait_for(
                lambda: control.state(origin.svc) not in ("running", "restarting"),
                SENTINEL_WAIT,
                interval=2,
            ):
                took = time.monotonic() - started
                return PASS, f"{origin.svc} exited {took:.0f}s after it started"
            state = "answers on its web port" if control.answers(origin.svc) else "runs"
            return FAIL, f"{SENTINEL_WAIT}s after it started, {origin.svc} {state}"
    except control.ControlError as e:
        return FAIL, str(e)


def sentinel_location(test: Test) -> dict[str, Row]:
    """Check that the first origin starts only where its sentinels are.

    It is restarted with every export naming a SentinelLocation: one
    that each export's storage holds, with which it must start and serve
    (`present`); and one that none does, with which it must exit
    (`missing`). Afterward it is restored, and the federation must serve
    again.
    """
    rows = ROWS["sentinel"]
    why = test.writable() or control.unrestartable(test.fed)
    if why:
        return {row: (SKIP, why) for row in rows}
    origin = test.fed.origins[0]
    text = generated_exports(origin.svc)
    print(f"\nRestarting {origin.svc} with a SentinelLocation for each export")
    found = {"sentinel present": suite.attempt(partial(sentinel_present, test, origin, text), EXPECTED)}
    if found["sentinel present"][0] == PASS:
        found["sentinel missing"] = suite.attempt(partial(sentinel_missing, test, origin, text), EXPECTED)
    else:
        found["sentinel missing"] = (
            INCONCLUSIVE,
            f"{origin.svc} did not start with its sentinels in place either, so its refusal"
            + " would show nothing",
        )
    why = test.settle(test.rel(PROBE))
    if why:
        warn(f"with {origin.svc} restored, {why}")
    return found


# --------------------------------------------------------------------------


# Each scenario checked whole: its check, which returns a Row for each of
# the scenario's ROWS, by row. A check may raise SetupError or
# stores.Unreadable, which fails its rows.
SCENARIO_CHECKS: dict[str, Callable[[Test], dict[str, Row]]] = {
    "multipart-put": multipart_put,
    "tpc-body": tpc_body,
    "tpc-failures": tpc_failures,
}


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None."""
    return suite.unexported(fed)


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the probe in place, run the selected scenarios, and clean up.

    Those that need the origins restarted run last; cleaning up removes
    what they wrote.
    """
    fed = session.fed
    test = Test(session, Stub())

    def record(row: str, found: Row) -> None:
        results.add(row, *found)

    test.clean("before the run")
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
                    errors = stores.make_dirs([origin], NAMESPACE, test.rel(origin.svc), test.setup)
                    if errors:
                        die(f"could not make the run's collection: {errors[0]}")
                errors = stores.put_everywhere(
                    fed.origins,
                    NAMESPACE,
                    test.rel(PROBE),
                    os.urandom(PROBE_SIZE),
                    test.setup,
                )
                if errors:
                    die(f"could not put the probe in place: {errors[0]}")
            for name in selected:
                if name in SCENARIO_CHECKS:
                    print(f"  {name} ...")
                    rows = ROWS[name]
                    found = suite.attempt_all(rows, partial(SCENARIO_CHECKS[name], test), EXPECTED)
                    for row in rows:
                        record(row, found[row])
            if "disable-copies" in selected:
                print("  disable-copies ...")
                record("disable-copies public", suite.attempt(partial(copies_to_public, test), EXPECTED))
            later = [r for name in selected for r in ROWS[name] if r in RESTARTED]
            if later:
                run_restarted(test, later, record)
            if "nested-export" in selected:
                print("  nested-export ...")
                for row, result in nested_export(test).items():
                    record(row, result)
            for name in ("backend-token", "backend-passthrough"):
                if name in selected:
                    print(f"  {name} ...")
                    record(name, gated(test, name == "backend-passthrough"))
            if "sentinel" in selected:
                print("  sentinel ...")
                found = suite.attempt_all(ROWS["sentinel"], partial(sentinel_location, test), EXPECTED)
                for row, result in found.items():
                    record(row, result)
    finally:
        test.clean("after the run")
