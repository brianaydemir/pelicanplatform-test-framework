"""Uploads that an origin persists only on successful close (POSC).

POSC (`-p origin-posc`): interrupt uploads to an origin, or race two, and
check what its store shows, and what it serves meanwhile. The same for
XRootD's atomic uploads (`-p origin-atomic-uploads`), which stage each
upload beside the exports' storage rather than inside it, and for a pstore
origin (`-p origin-pstore`), which commits a new version only when an upload
completes, judged by what the origins serve. None of the three: the suite is
skipped.

Uploads go straight to the first origin in
framework/var/generated/origins, under /protected-a/data/posc/, with a
token of the session's profile; what the stores hold is read, and
removed, with a WLCG token either way. Only `client` goes through the
director. An interruption is judged only once
the upload is seen under way: a new staging file, or growth in
pstore/objects on pstore. Afterward, /protected-a/data/posc is emptied on
every store.

`posc-expiry` checks the native POSC's reaping of abandoned staging
files, and its guard on the staging directory. Its `idle` row restarts
the first origin with a short Origin.Posc.FileTimeout and keepalive
(control.restart_with()), restores it afterward, and waits until the
federation serves again; without a control helper, or in the `tiny`
topology, whose origin is the whole federation's process, it skips.
"""

import glob
import os
import re
import shutil
import socket
import ssl
import subprocess  # nosec B404
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
from typing import Optional
from urllib.parse import unquote, urlsplit

from testlib import blocks, common, control, director, metrics, stores, web, webdav
from testlib.common import die, first, shown, wait_for, warn
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "commit": "a complete upload lands, and leaves no staging file",
    "stalled": "while an upload stalls, only its staging file exists",
    "sized": "an upload cut short of its Content-Length leaves nothing",
    "chunked": "a chunked upload cut short leaves nothing",
    "overwrite": "the old object is served during an overwrite, and intact once it is cut",
    "concurrent": "two whole uploads of one object at once leave one of them, whole",
    "client": "killing `pelican object put` midway leaves nothing",
    "hidden": "neither a PROPFIND nor an HTML listing of the export shows .pelican-posc",
    "posc-expiry": "stale staging files are reaped, an idle upload kept, and staging guarded",
}

# A row: its status and note.
Row = tuple[str, str]

# posc-expiry's cases, each a row named `posc-expiry <case>`.
EXPIRY_CASES = ("mode", "stale", "staging-put", "staging-mkcol", "idle")

# The scenarios that each kind of staging cannot have, and why. A pstore
# origin has no staging file to watch, and XRootD stages uploads outside
# the exports, where there is nothing to hide.
SKIPS: dict[str, dict[str, str]] = {
    "pstore": {
        "stalled": "pstore commits a version at close, with no staging file",
        "hidden": "pstore commits a version at close, with no staging file",
        "posc-expiry": "pstore commits a version at close, with no staging file",
    },
    "atomic": {
        "hidden": "XRootD stages uploads outside the exports, so there is nothing to hide",
        "posc-expiry": "only the native POSC has Origin.Posc.FileTimeout and its reaper",
    },
}

# Where XRootD's atomic uploads are staged, in each origin's store:
# Origin.UploadTempLocation, which fed.sh sets (fed_generate).
ATOMIC_DIR = ".in-progress"

# The gauge of uploads that POSC is staging, and the count of staging
# files it has reaped.
POSC_ACTIVE = "pelican_origin_posc_active_uploads"
POSC_EXPIRED = "pelican_origin_posc_expired_total"

# posc-expiry: how long ago a planted staging file was last modified, to
# be stale under the default Origin.Posc.FileTimeout (1h); `idle`'s
# restart's FileTimeout and KeepaliveInterval, in seconds, how old a file
# it plants is, stale under that timeout but not the default, and how
# long its upload idles, past the timeout; and how long the reaper, which
# sweeps every 5 seconds (expireLoop in origin_serve/posc.go), may take.
STALE_AGE = 2 * 3600
FILE_TIMEOUT = 20
KEEPALIVE = 5
SHORT_STALE_AGE = 40
IDLE = 35
REAP_WAIT = 15
CONFIG_FILE = "90-posc.yaml"
# How long the federation may take to serve again after the restart.
SETTLE_WAIT = 150

MIB = 1 << 20


class CouldNotConnect(Exception):
    """partial_put()'s failure to connect, or finish the TLS handshake."""


def partial_put(
    url: str,
    mode: str,
    total: int,
    send: int,
    hold: float,
    token: str,
    sent: Optional[threading.Event] = None,
    release: Optional[threading.Event] = None,
) -> Optional[str]:
    """Start an upload to url, and cut it short.

    Send the headers and `send` bytes, hold the connection for `hold`
    seconds or until release is set, then close its sending side as a dying
    client would (no TLS close_notify).

    Args:
        url: The object's URL.
        mode: `sized`, claiming a Content-Length of total, or `chunked`.
        total: The Content-Length to claim, if mode is `sized`.
        send: How many bytes to send.
        hold: How long to hold the connection, in seconds, at most.
        token: The bearer token to present.
        sent: An event to set once the bytes are out.
        release: An event that, once set, ends the hold.

    Returns:
        The status that comes back, if any.

    Raises:
        CouldNotConnect: It never got as far as sending.
    """
    u = urlsplit(url)
    try:
        sock = socket.create_connection((u.hostname, u.port or 443), timeout=60)
    except OSError as e:
        raise CouldNotConnect(str(e)) from e
    try:
        # TLS over memory buffers, so that the socket can be half-closed
        # without TLS saying goodbye.
        tls_in, tls_out = ssl.MemoryBIO(), ssl.MemoryBIO()

        def flush() -> None:
            data = tls_out.read()
            if data:
                sock.sendall(data)

        def pump() -> bool:
            data = sock.recv(65536)
            if not data:
                tls_in.write_eof()
                return False
            tls_in.write(data)
            return True

        try:
            tls = web.tls_context().wrap_bio(tls_in, tls_out, server_hostname=u.hostname)
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    flush()
                    if not pump():
                        raise OSError("connection closed during the TLS handshake") from None
            flush()
        except OSError as e:  # including ssl.SSLError
            raise CouldNotConnect(f"TLS handshake: {e}") from e

        target = u.path + (f"?{u.query}" if u.query else "")
        length = f"Content-Length: {total}" if mode == "sized" else "Transfer-Encoding: chunked"
        head = [
            f"PUT {target} HTTP/1.1",
            f"Host: {u.netloc}",
            f"Authorization: Bearer {token}",
            length,
        ]
        body = os.urandom(send)
        tls.write(("\r\n".join(head) + "\r\n\r\n").encode())
        tls.write(body if mode == "sized" else b"%x\r\n%s\r\n" % (len(body), body))
        flush()
        if sent is not None:
            sent.set()
        if release is not None:
            release.wait(hold)
        else:
            time.sleep(hold)

        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # the server hung up first; it may have answered
        reply = b""
        try:
            while b"\r\n" not in reply:
                try:
                    reply += tls.read(4096)
                except ssl.SSLWantReadError:
                    if not pump():
                        break
        except (ssl.SSLError, OSError):
            pass
        fields = reply.split(b"\r\n", 1)[0].split()
        return fields[1].decode() if len(fields) > 1 else None
    finally:
        sock.close()


def http(status: Optional[str]) -> str:
    """Return how partial_put's status reads in a note."""
    return f"HTTP {status}" if status else "no response"


def wrong_answer(status: Optional[str]) -> Optional[str]:
    """Return why partial_put's status is wrong for an upload cut short.

    Returns:
        Why, or None if it is not wrong.
    """
    if status and status.startswith("2"):
        return f"{http(status)} for an incomplete upload"
    if status == "405":
        return f"{http(status)} (Method Not Allowed) for a PUT"
    return None


class Test:
    """What every scenario needs: a token, the first origin, and the stores.

    The director may send `client` to any origin, so every store is watched.
    A pstore origin's store is asked over HTTPS what it holds.

    Attributes:
        pelican: The path of the pelican client.
        env: The environment to run the client in.
        origin: The first origin, to which uploads go.
        origin_web: The first origin's web URL, with no path.
        kind: How uploads are staged: `posc`, `atomic` (XRootD's), or
            `pstore`.
        pstore: Whether kind is `pstore`.
        origins: The origins, each store once.
        stores: /protected-a's storage in each store.
        roots: The stores themselves, where a pstore keeps its objects and
            XRootD its staging files.
        tmp: A directory of the suite's own, for files of its own.
        run: The run's ID, with which each object's name begins.
        base_url: Where the objects go, at the first origin.
        token_file: A file that holds token.
        token: The token for /protected-a, of the session's profile, which
            the uploads present.
        setup: The token for /protected-a, of the WLCG profile, for
            finding, reading, and removing what the stores hold.
        before: The staging files there were at mark().
        before_sizes: On pstore, the sizes of its objects' files at mark().
    """

    def __init__(self, session: Session):
        fed = session.fed
        self.fed = fed
        self.pelican = session.pelican
        self.env = session.env
        self.origin = fed.origins[0]
        self.origin_web = re.sub(r"^(https://[^/]*).*", r"\1", self.origin.url)
        if fed.posc:
            self.kind = "posc"
        elif fed.atomic_uploads:
            self.kind = "atomic"
        else:
            self.kind = "pstore"
        self.pstore = self.kind == "pstore"
        self.origins = stores.unique(fed.origins)
        self.stores = sorted({o.store_of("protected-a") for o in fed.origins})
        self.roots = sorted({o.store for o in fed.origins})
        self.tmp = session.path("posc")
        os.makedirs(self.tmp)
        self.run = session.run
        self.base_url = f"{self.origin.url}/protected-a/data/posc"
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        self.setup = session.setup_token("protected-a")
        self.before: set[str] = set()
        self.before_sizes: dict[str, int] = {}
        if self.pstore:
            errors = stores.make_dirs(fed.origins, "protected-a", "data/posc", self.setup)
            if errors:
                die(f"could not make /protected-a/data/posc: {errors[0]}")

    def staging(self) -> set[str]:
        """Return the staging files.

        Under POSC, they are <storage>/.pelican-posc/<user>/in_progress.*,
        where the storage is /protected-a's, and the user the token's
        subject; under XRootD's atomic uploads, whatever is under
        <store>/.in-progress/<user>/ (ATOMIC_DIR); on pstore, none.
        """
        found: set[str] = set()
        if self.kind == "posc":
            for store in self.stores:
                found.update(
                    glob.glob(f"{store}/.pelican-posc/**/in_progress.*", recursive=True)
                )
        elif self.kind == "atomic":
            for root in self.roots:
                found.update(glob.glob(f"{root}/{ATOMIC_DIR}/**", recursive=True))
        return {f for f in found if os.path.isfile(f)}

    def pstore_sizes(self) -> dict[str, int]:
        """Return each pstore origin's objects' files, and their sizes."""
        found: dict[str, int] = {}
        for root in self.roots:
            for path in glob.glob(f"{root}/pstore/objects/**", recursive=True):
                if os.path.isfile(path):
                    found[path] = os.path.getsize(path)
        return found

    def mark(self) -> None:
        """Remember the staging files there are now.

        On pstore, remember how big its objects' files are.
        """
        self.before = self.staging()
        if self.pstore:
            self.before_sizes = self.pstore_sizes()

    def under_way(self) -> bool:
        """Return whether an upload has begun to arrive since mark().

        It has if there is a new staging file, or on pstore, an objects'
        file that is new or bigger.
        """
        if not self.pstore:
            return self.new_staging() is not None
        now = self.pstore_sizes()
        return any(size > self.before_sizes.get(path, -1) for path, size in now.items())

    def new_staging(self) -> Optional[str]:
        """Return a staging file that has appeared since mark(), if any."""
        added = sorted(self.staging() - self.before)
        return added[0] if added else None

    def object_file(self, name: str) -> Optional[str]:
        """Return the object's file in the first store with one, if any."""
        for store in self.stores:
            path = f"{store}/data/posc/{name}"
            if os.path.exists(path):
                return path
        return None

    def found(self, name: str) -> Optional[str]:
        """Return where the object is, if anywhere.

        Returns:
            Its file, or on pstore, its URL at the first origin that serves
            it; None if it is nowhere.

        Raises:
            stores.Unreadable: On pstore, an origin answers a HEAD of it
                with neither 200 nor 404.
        """
        if not self.pstore:
            return self.object_file(name)
        for origin in self.origins:
            url = f"{origin.url}/protected-a/data/posc/{name}"
            answer = web.request("HEAD", url, token=self.setup)
            if answer.status == 200:
                return url
            if answer.status != 404:
                raise stores.Unreadable(f"HEAD {url}: {answer.describe()}")
        return None

    def size(self, where: str) -> str:
        """Return how big the object found() at where is."""
        if not self.pstore:
            return str(size_of(where))
        return web.request("HEAD", where, token=self.setup).header("Content-Length") or "?"

    def holds(self, name: str, data: bytes) -> bool:
        """Return whether the object that found() finds holds data."""
        if not self.pstore:
            path = self.object_file(name)
            return path is not None and holds(path, data)
        return common.digest(data) in stores.held_anywhere(
            self.origins, "protected-a", f"data/posc/{name}", {"protected-a": self.setup}
        )

    def remove(self, name: str) -> None:
        """Remove the object, wherever it is."""
        for origin in self.origins:
            stores.delete(origin, "protected-a", f"data/posc/{name}", self.setup)

    def full_put(self, data: bytes, name: str) -> web.Response:
        """Upload all of data to object name."""
        return web.request("PUT", f"{self.base_url}/{name}", token=self.token, upload=data)

    def partial_put(
        self,
        name: str,
        mode: str,
        total: int,
        send: int,
        hold: float,
        sent: Optional[threading.Event] = None,
        release: Optional[threading.Event] = None,
    ) -> Optional[str]:
        """Make a partial_put() to object name, with the test's token.

        A connection that fails once it is made counts as no response.

        Raises:
            CouldNotConnect: It never got as far as sending.
        """
        try:
            return partial_put(
                f"{self.base_url}/{name}", mode, total, send, hold, self.token, sent, release
            )
        except OSError as e:
            print(f"{common.PROG}: {name}: {e}", file=sys.stderr)
            return None


class Upload:
    """A Test.partial_put() in a thread of its own.

    That way, the stores can be watched while it is held open: until
    finish(), or for hold seconds at most.

    Attributes:
        sent: Set once the bytes are out.
        released: Set by finish(), to let the connection go.
        status: Afterward, what came back.
        error: Afterward, why it never got under way, if it didn't.
    """

    def __init__(
        self, test: Test, name: str, mode: str, total: int, send: int, hold: float = 60
    ):
        self.sent = threading.Event()
        self.released = threading.Event()
        self.status: Optional[str] = None
        self.error: Optional[str] = None
        self.thread = threading.Thread(
            target=self._put, args=(test, name, mode, total, send, hold), daemon=True
        )
        self.thread.start()

    def _put(
        self, test: Test, name: str, mode: str, total: int, send: int, hold: float
    ) -> None:
        try:
            self.status = test.partial_put(
                name, mode, total, send, hold, self.sent, self.released
            )
        except CouldNotConnect as e:
            self.error = f"could not connect: {e}"
        except Exception as e:  # pylint: disable=broad-exception-caught
            # The thread's end: the scenario fails with it.
            self.error = f"{type(e).__name__}: {e}"

    def started(self, seconds: float = 30) -> bool:
        """Return whether the bytes are out within seconds."""
        wait_for(lambda: self.sent.is_set() or not self.thread.is_alive(), seconds, 0.1)
        return self.sent.is_set()

    def finish(self) -> None:
        """Let the connection go, and wait for the answer."""
        self.released.set()
        self.thread.join()


def watch(test: Test, upload: Upload) -> bool:
    """Return whether upload is seen under way while it is held open.

    It must be seen (Test.under_way()) within 10 seconds of its bytes going
    out.
    """
    return upload.started() and wait_for(test.under_way, 10)


def unseen(test: Test, upload: Upload) -> str:
    """Return why an upload not seen under way fails, once finished.

    A refusal (e.g. 400, 401, 403, or 411) or no answer leaves nothing to
    judge the interruption by.
    """
    if upload.error:
        return upload.error
    what = "pstore/objects did not grow" if test.pstore else "no staging file appeared"
    return f"the upload never got under way: {what} ({http(upload.status)})"


def holds(path: str, data: bytes) -> bool:
    """Return whether the file at path holds data."""
    with open(path, "rb") as f:
        return f.read() == data


def size_of(path: str) -> int:
    """Return the size of the file at path."""
    return os.path.getsize(path)


# --------------------------------------------------------------------------
# The scenarios.


def commit(test: Test) -> tuple[str, str]:
    """Check that a complete upload lands, and leaves no staging file."""
    name = f"{test.run}-commit"
    data = os.urandom(MIB)
    test.mark()
    answer = test.full_put(data, name)
    if answer.status not in (200, 201, 204):
        return FAIL, f"upload got {answer.describe()}"
    path = test.found(name)
    if path is None:
        return FAIL, f"upload got {answer.describe()}, but no object is in the store"
    if not test.holds(name, data):
        return FAIL, f"{shown(path)} differs from what was uploaded"
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"staging file left behind: {shown(test.new_staging())}"
    return PASS, shown(path)


def held_open(test: Test, name: str) -> tuple[str, str]:
    """Return `stalled`'s verdict on object name's upload.

    Call it while the upload is held open and its staging file exists.
    Only POSC counts the upload in a metric. The HEAD presents the setup
    token: it checks what the origin shows, not the pass's tokens.
    """
    answer = web.request("HEAD", f"{test.base_url}/{name}", token=test.setup)
    path = test.object_file(name)
    if path is not None:
        return FAIL, f"{shown(path)} is visible before the upload finished"
    if answer.status != 404:
        return FAIL, f"HEAD got {answer.describe()}, not 404"
    if test.kind == "posc":
        found = metrics.fetch(test.origin_web) or metrics.EMPTY
        if not found.has(POSC_ACTIVE):
            return FAIL, f"/metrics has no {POSC_ACTIVE}"
        if found.value(POSC_ACTIVE) < 1:
            return FAIL, f"{POSC_ACTIVE} is {found.value(POSC_ACTIVE):g}"
    return PASS, shown(test.new_staging())


def stalled(test: Test) -> tuple[str, str]:
    """Check that while an upload stalls, only its staging file exists.

    While it is held open, there is no object, a HEAD answers 404, and
    under POSC, the origin counts the upload as active.
    """
    name = f"{test.run}-stalled"
    test.mark()
    upload = Upload(test, name, "sized", 2 * MIB, MIB)
    verdict: Optional[tuple[str, str]] = None
    try:
        if watch(test, upload):
            verdict = held_open(test, name)
    finally:
        upload.finish()
    if verdict is None:
        return FAIL, unseen(test, upload)
    return verdict


def interrupted(test: Test, scenario: str, mode: str, total: int, send: int) -> tuple[str, str]:
    """Check that an upload in mode, cut short, leaves nothing.

    The upload is held open until it is seen under way, then cut short:
    nothing may be left of it, and the answer, if any, may not be a success.

    Args:
        test: The test.
        scenario: The scenario's name, which names the object.
        mode: As for partial_put().
        total: As for partial_put().
        send: As for partial_put().

    Returns:
        The scenario's status and note.
    """
    name = f"{test.run}-{scenario}"
    test.mark()
    upload = Upload(test, name, mode, total, send)
    try:
        seen = watch(test, upload)
    finally:
        upload.finish()
    if not seen:
        return FAIL, unseen(test, upload)
    status = upload.status
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"{http(status)}; staging file left: {shown(test.new_staging())}"
    path = test.found(name)
    if path is not None:
        size = test.size(path)
        return (
            FAIL,
            f"{http(status)}, and {shown(path)} holds {size} bytes of the partial upload",
        )
    wrong = wrong_answer(status)
    if wrong:
        return FAIL, wrong
    return PASS, http(status)


def sized(test: Test) -> tuple[str, str]:
    """Check that an upload short of its Content-Length leaves nothing."""
    return interrupted(test, "sized", "sized", 2 * MIB, MIB)


def chunked(test: Test) -> tuple[str, str]:
    """Check that a chunked upload cut short leaves nothing.

    It sends a bit more than pstore buffers before spilling into
    pstore/objects, so that a pstore origin shows the upload under way too.
    """
    return interrupted(test, "chunked", "chunked", 0, blocks.SPILL + 64 * 1024)


def overwrite(test: Test) -> tuple[str, str]:
    """Check that an overwrite cut short leaves the old object intact.

    The overwrite is held open until it is seen under way, then cut short.
    Meanwhile, the origin must serve the old object; afterward, the old
    object must be intact, and the answer, if any, not a success.
    """
    name = f"{test.run}-overwrite"
    original = os.urandom(MIB)
    answer = test.full_put(original, name)
    path = test.found(name)
    if path is None:
        return FAIL, f"the original's upload did not land ({answer.describe()})"
    test.mark()
    upload = Upload(test, name, "sized", 2 * MIB, MIB)
    served: Optional[web.Response] = None
    try:
        seen = watch(test, upload)
        if seen:
            served = web.request("GET", f"{test.base_url}/{name}", token=test.token)
    finally:
        upload.finish()
    if served is None:
        return FAIL, unseen(test, upload)
    status = upload.status
    if served.status != 200 or served.body != original:
        return FAIL, (
            f"while the overwrite was under way, a GET got {served.describe()},"
            f" with {len(served.body)} bytes that are not the original"
        )
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"{http(status)}; staging file left: {shown(test.new_staging())}"
    if test.found(name) is None:
        return FAIL, f"{http(status)}; the original is gone"
    if not test.holds(name, original):
        return FAIL, f"{http(status)}; {shown(path)} no longer holds the original"
    wrong = wrong_answer(status)
    if wrong:
        return FAIL, wrong
    return PASS, http(status)


def concurrent(test: Test) -> tuple[str, str]:
    """Check that two whole uploads of one object at once leave one, whole.

    The uploads, of different bytes to one new object, start together: once
    both are answered, the object must be all of one whose upload succeeded,
    and no staging file may be left.
    """
    name = f"{test.run}-concurrent"
    versions = [os.urandom(8 * MIB) for _ in range(2)]
    barrier = threading.Barrier(len(versions))

    def put(data: bytes) -> web.Response:
        barrier.wait(timeout=60)
        return test.full_put(data, name)

    test.mark()
    with ThreadPoolExecutor(len(versions)) as pool:
        answers = list(pool.map(put, versions))
    said = ", ".join(a.describe() for a in answers)
    succeeded = [n for n, answer in enumerate(answers) if answer.ok]
    if not succeeded:
        return FAIL, f"neither upload succeeded ({said})"
    if test.found(name) is None:
        return FAIL, f"{said}, but there is no object"
    whole = [n for n in succeeded if test.holds(name, versions[n])]
    if not whole:
        return FAIL, f"{said}, but the object is not all of either upload that succeeded"
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"{said}; staging file left: {shown(test.new_staging())}"
    return PASS, f"{said}; the object is upload {whole[0] + 1}"


def client(test: Test) -> tuple[str, str]:
    """Check that killing `pelican object put` midway leaves nothing."""
    name = f"{test.run}-client"
    total = 1 << 30
    # Sparse: the client reads 1 GiB of zeros, with no 1 GiB written first.
    source = os.path.join(test.tmp, "client")
    with open(source, "wb") as f:
        f.truncate(total)
    log_file = os.path.join(test.tmp, "client.log")
    args = ["object", "put", "--token", test.token_file, source]
    args.append(f"{test.fed.url}/protected-a/data/posc/{name}")
    test.mark()
    with (
        open(log_file, "wb") as log,
        subprocess.Popen(  # nosec B603
            [test.pelican, *args],
            env=test.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        ) as upload,
    ):
        # Kill it once the upload is under way, unless it finishes first,
        # or is not seen to begin within 30 seconds.
        seen = False
        for _ in range(300):
            if upload.poll() is not None:
                break
            if test.under_way():
                seen = True
                break
            time.sleep(0.1)
        code = upload.poll()
        upload.kill()
    os.remove(source)
    time.sleep(3)
    path = test.found(name)
    try:
        if not seen:
            if code is None:
                return INCONCLUSIVE, "could not see the upload begin"
            if code != 0:
                with open(log_file, encoding="utf-8", errors="replace") as f:
                    return FAIL, f"`pelican object put` exited {code}: {last_line(f.read())}"
            return INCONCLUSIVE, "the upload finished before it could be interrupted"
        if path is not None:
            return FAIL, f"{shown(path)} holds {test.size(path)} of {total} bytes"
        if not wait_for(lambda: test.new_staging() is None, 10):
            return FAIL, f"staging file left: {shown(test.new_staging())}"
        return PASS, ""
    finally:
        # Don't leave up to 1 GiB behind.
        if path is not None:
            test.remove(name)


class Links(HTMLParser):
    """The names an HTML listing links to.

    Attributes:
        names: The last segment of each href's path, decoded.
    """

    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        href = dict(attrs).get("href")
        if tag == "a" and href:
            self.names.add(unquote(urlsplit(href).path).rstrip("/").rsplit("/", 1)[-1])


def linked(body: bytes) -> set[str]:
    """Return the names the HTML listing body links to."""
    links = Links()
    links.feed(body.decode(errors="replace"))
    links.close()
    return links.names


def hidden(test: Test) -> tuple[str, str]:
    """Check that no listing of the export shows .pelican-posc.

    It lists the export while an upload is held open, so that .pelican-posc
    is there to hide: by PROPFIND (Depth 1), as `pelican object ls` does,
    and as a browser does, by a GET for HTML, whose listing links to each
    entry. The export holds data/, which both must show. Pelican hides the
    staging directory from PROPFIND, but its HTML listing reads the
    directory itself and shows it, so the row fails there.
    """
    name = f"{test.run}-hidden"
    posc_dir = f"{test.origin.store_of('protected-a')}/.pelican-posc"
    url = f"{test.origin.url}/protected-a/"
    test.mark()
    upload = Upload(test, name, "sized", 2 * MIB, MIB)
    answer: Optional[web.Response] = None
    listed: Optional[web.Response] = None
    try:
        staged = upload.started() and wait_for(lambda: test.new_staging() is not None, 10)
        there = os.path.isdir(posc_dir)
        if staged and there:
            listed = webdav.propfind(url, "1", test.token)
            answer = web.request("GET", url, token=test.token, headers={"Accept": "text/html"})
    finally:
        upload.finish()
    if answer is None or listed is None:
        if not staged:
            return FAIL, upload.error or f"no staging file appeared ({http(upload.status)})"
        return FAIL, f"{shown(posc_dir)} does not exist, although an upload is staged"
    if listed.status != 207:
        return FAIL, f"PROPFIND {url}: {listed.describe()}, not 207"
    try:
        entries = webdav.relative(webdav.parse(listed.body), "/protected-a")
    except ValueError as e:
        return FAIL, f"PROPFIND {url}: {e}"
    if "data" not in entries:
        return FAIL, f"PROPFIND {url} does not list data/, so it is not the export's listing"
    if ".pelican-posc" in entries:
        return FAIL, f"PROPFIND {url} lists .pelican-posc"
    if answer.status != 200:
        return FAIL, f"the HTML listing got {answer.describe()}, not 200"
    names = linked(answer.body)
    if "data" not in names:
        return FAIL, f"{url} does not link to data/, so it is not the export's listing"
    if ".pelican-posc" in names:
        return FAIL, f"the HTML listing of {url} shows .pelican-posc"
    return PASS, f"{shown(posc_dir)} exists, and neither listing shows it"


# --------------------------------------------------------------------------
# posc-expiry


class Problem(Exception):
    """A row's failure to do what it needs to judge Pelican.

    The row fails, with this as its note.
    """


def guarded(check: Callable[[], Row]) -> Row:
    """Return check's row, or a FAIL if it could not set up, or raised."""
    try:
        return check()
    except Problem as e:
        return FAIL, str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the row
        # fails, and the rest still run.
        traceback.print_exc()
        return FAIL, f"{type(e).__name__}: {e}"


def staging_root(test: Test) -> str:
    """Return the first origin's POSC staging directory in /protected-a."""
    return f"{test.origin.store_of('protected-a')}/.pelican-posc"


def planted_dir(test: Test) -> str:
    """Return the staging directory of the suite's own, as if a user's.

    posc-expiry plants staging files in it.
    """
    return f"{staging_root(test)}/expiry-{test.run}"


def plant(test: Test, age: float) -> str:
    """Plant a staging file in planted_dir(), as an abandoned upload leaves.

    The file was last modified age seconds ago. The suite does not make
    POSC's staging directory: posc-expiry's first upload makes it, as its
    user's.

    Returns:
        The file's path.

    Raises:
        Problem: POSC has no staging directory yet, or the file could not
            be written.
    """
    if not os.path.isdir(staging_root(test)):
        raise Problem(
            f"{shown(staging_root(test))} does not exist, though an upload has been staged"
        )
    rel = f".pelican-posc/expiry-{test.run}/in_progress.{int(time.time())}.{os.getpid()}"
    error = stores.write_file(test.origin.store_of("protected-a"), rel, b"abandoned\n")
    if error:
        raise Problem(error)
    path = f"{test.origin.store_of('protected-a')}/{rel}"
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def expired(test: Test) -> float:
    """Return POSC_EXPIRED at the first origin; 0 if it does not answer."""
    return (metrics.fetch(test.origin_web) or metrics.EMPTY).value(POSC_EXPIRED)


def reaped(test: Test, age: float, timeout: str) -> list[str]:
    """Plant a stale staging file, and return what goes wrong in reaping it.

    The reaper must remove the file within REAP_WAIT seconds, and count it
    in POSC_EXPIRED, since it is older than Origin.Posc.FileTimeout.

    Args:
        test: The test.
        age: How long ago the file was last modified, in seconds.
        timeout: Origin.Posc.FileTimeout, as a note shows it.
    """
    before = expired(test)
    path = plant(test, age)
    problems: list[str] = []
    if not wait_for(lambda: not os.path.exists(path), REAP_WAIT):
        problems.append(
            (
                f"{shown(path)}, last modified {age:.0f}s ago, outlived"
                f" Origin.Posc.FileTimeout ({timeout}) by {REAP_WAIT}s"
            )
        )
    elif not wait_for(lambda: expired(test) > before, 5):
        problems.append(f"{POSC_EXPIRED} did not rise from {before:g}")
    return problems


def expiry_mode(test: Test) -> Row:
    """Check that a complete upload's object has mode 0644, as without POSC.

    0644 is the 0666 with which the WebDAV handler opens a PUT's file, less
    the origin's umask, 022. POSC opens its staging file with the mode asked
    for, and renames it into place (TestPoscFinalPermMatchesRequested). This
    upload, the scenario's first, also makes POSC's staging directory, if
    there is none yet.
    """
    name = f"{test.run}-mode"
    answer = test.full_put(os.urandom(4096), name)
    if not answer.ok:
        return FAIL, f"upload got {answer.describe()}"
    path = test.object_file(name)
    if path is None:
        return FAIL, f"upload got {answer.describe()}, but no object is in the store"
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o644:
        return FAIL, f"{shown(path)} has mode {mode:04o}, not 0644"
    return PASS, f"{shown(path)}: 0644"


def expiry_stale(test: Test) -> Row:
    """Check that a staging file abandoned two hours ago is reaped.

    Two hours is past the default Origin.Posc.FileTimeout of an hour, so the
    file is reaped at the next sweep, and counted (expireFiles in
    origin_serve/posc.go).
    """
    problems = reaped(test, STALE_AGE, "1h, the default")
    if problems:
        return FAIL, first(problems)
    return PASS, f"reaped within {REAP_WAIT}s, and counted"


def expiry_staging_put(test: Test) -> Row:
    """Check that a PUT straight into the staging directory writes nothing.

    POSC hides the directory, so the origin must answer as for a path whose
    parent is missing (409), and write nothing there.
    """
    name = f"{test.run}-put"
    url = f"{test.origin.url}/protected-a/.pelican-posc/{name}"
    answer = web.request("PUT", url, token=test.token, upload=b"staged\n")
    problems: list[str] = []
    if answer.status != 409:
        problems.append(f"PUT {url} got {answer.describe()}, not 409")
    left = f"{staging_root(test)}/{name}"
    if os.path.exists(left):
        problems.append(f"PUT {url} ({answer.describe()}) left {shown(left)}")
    return (FAIL, first(problems)) if problems else (PASS, answer.describe())


def expiry_staging_mkcol(test: Test) -> Row:
    """Check that a MKCOL straight into the staging directory makes nothing.

    POSC refuses it (poscFileSystem.Mkdir's EIO), so the origin must answer
    405, and make nothing there.
    """
    name = f"{test.run}-mkcol"
    url = f"{test.origin.url}/protected-a/.pelican-posc/{name}"
    answer = web.request("MKCOL", url, token=test.token)
    problems: list[str] = []
    if answer.status != 405:
        problems.append(f"MKCOL {url} got {answer.describe()}, not 405")
    left = f"{staging_root(test)}/{name}"
    if os.path.exists(left):
        problems.append(f"MKCOL {url} ({answer.describe()}) made {shown(left)}")
    return (FAIL, first(problems)) if problems else (PASS, answer.describe())


def expiry_idle(test: Test) -> Row:
    """Check that an idle upload outlives a short Origin.Posc.FileTimeout.

    With Origin.Posc.FileTimeout FILE_TIMEOUT and KeepaliveInterval
    KEEPALIVE seconds: a planted staging file, stale under that timeout but
    not the default, is reaped, so the restart's timeout is in force; and an
    upload that sends half its bytes, then idles IDLE seconds, past the
    timeout, keeps its staging file, whose keepalive refreshes its mtime
    (touchOpenFiles), and completes whole once it sends the rest.
    """
    problems = reaped(test, SHORT_STALE_AGE, f"{FILE_TIMEOUT}s")
    if problems:
        return FAIL, f"the short timeout is not in force: {first(problems)}"
    name = f"{test.run}-idle"
    data = os.urandom(2 * MIB)
    url = urlsplit(f"{test.base_url}/{name}")
    test.mark()
    conn = HTTPSConnection(
        url.hostname or "", url.port or 443, timeout=120, context=web.tls_context()
    )
    try:
        conn.putrequest("PUT", url.path)
        conn.putheader("Authorization", f"Bearer {test.token}")
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        conn.send(data[:MIB])
        if not wait_for(lambda: test.new_staging() is not None, 10):
            return FAIL, "no staging file appeared for the upload"
        staged = test.new_staging() or ""
        time.sleep(IDLE)
        kept = os.path.exists(staged)
        conn.send(data[MIB:])
        answer = conn.getresponse()
        answer.read()
        status = answer.status
    except (OSError, HTTPException) as e:
        return FAIL, f"the upload failed: {type(e).__name__}: {e}"
    finally:
        conn.close()
    if not kept:
        problems.append(f"{shown(staged)} was reaped while its upload idled {IDLE}s")
    if status not in (200, 201, 204):
        problems.append(f"the upload got HTTP {status}, having idled {IDLE}s")
    elif not test.holds(name, data):
        problems.append(f"HTTP {status}, but the object is not the whole upload")
    if problems:
        return FAIL, first(problems)
    return PASS, f"idled {IDLE}s, past the {FILE_TIMEOUT}s timeout; HTTP {status}, whole"


def unrestartable(test: Test) -> Optional[str]:
    """Return why the first origin cannot be restarted, or None if it can.

    The restart would give it configuration of the suite's own.
    """
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


def serving(test: Test) -> Optional[str]:
    """Return why the namespace is not served as before, or None if it is.

    Every origin must answer a HEAD of the probe, every director name every
    origin for it, and every federated cache answer a HEAD of it, with the
    setup token.
    """
    path = f"/protected-a/data/posc/{test.run}-probe"
    for origin in test.fed.origins:
        answer = web.request("HEAD", f"{origin.url}{path}", token=test.setup)
        if answer.status != 200:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.setup)
        for origin in test.fed.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request("HEAD", f"{cache.url}{path}", token=test.setup)
        if answer.status != 200:
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def settle(test: Test) -> Optional[str]:
    """Wait until the federation serves the namespace as before.

    Returns:
        Why it doesn't within SETTLE_WAIT seconds (see serving()), or None
        if it does.
    """
    deadline = time.time() + SETTLE_WAIT
    why = serving(test)
    while why and time.time() < deadline:
        time.sleep(5)
        why = serving(test)
    return f"after {SETTLE_WAIT}s, {why}" if why else None


def idle_restarted(test: Test) -> Row:
    """Return expiry_idle()'s row, with the first origin restarted for it.

    The restart gives the origin the short timeout and keepalive. Then
    restore it, and wait until the federation serves again.
    """
    why = unrestartable(test)
    if why:
        return SKIP, why
    for origin in stores.unique(test.fed.origins):
        error = stores.write_file(
            origin.store_of("protected-a"), f"data/posc/{test.run}-probe", b"probe\n"
        )
        if error:
            return FAIL, f"putting the probe: {error}"
    text = "\n".join(
        [
            "Origin:",
            "  Posc:",
            f"    FileTimeout: {FILE_TIMEOUT}s",
            f"    KeepaliveInterval: {KEEPALIVE}s",
        ]
    )
    svc = test.origin.svc
    print(f"\nRestarting {svc} with:\n{text}")
    rows: list[Row] = []
    try:
        with control.restart_with(svc, {CONFIG_FILE: text + "\n"}):
            rows.append(guarded(lambda: expiry_idle(test)))
    except control.ControlError as e:
        # Restarting, or restoring, failed: the row fails if it never ran.
        warn(f"restarting {svc}, or restoring it: {e}")
        if not rows:
            rows.append((FAIL, f"restarting {svc}: {e}"))
    why = settle(test)
    if why:
        warn(f"with {svc} restored, {why}")
    return rows[0]


def posc_expiry(test: Test) -> dict[str, Row]:
    """Return posc-expiry's rows, by case.

    The cases check the native POSC's reaping of abandoned staging files,
    and its guard on its staging directory.
    """
    return {
        "mode": guarded(lambda: expiry_mode(test)),
        "stale": guarded(lambda: expiry_stale(test)),
        "staging-put": guarded(lambda: expiry_staging_put(test)),
        "staging-mkcol": guarded(lambda: expiry_staging_mkcol(test)),
        "idle": idle_restarted(test),
    }


# The scenarios but posc-expiry, by name. Each takes the Test, and
# returns its status and note; it may raise CouldNotConnect.
RUN = {
    "commit": commit,
    "stalled": stalled,
    "sized": sized,
    "chunked": chunked,
    "overwrite": overwrite,
    "concurrent": concurrent,
    "client": client,
    "hidden": hidden,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None if there is something.

    There is nothing without staging of uploads, or without writes.
    """
    if not fed.posc and not fed.atomic_uploads and not fed.origins[0].pstore:
        return (
            "the origins stage no uploads: POSC and XRootD's atomic uploads are off, and they"
            " are not pstore ('-p origin-posc', '-p origin-atomic-uploads', or"
            " '-p origin-pstore')"
        )
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes"
    return None


KINDS = {"posc": "POSC", "atomic": "XRootD atomic upload", "pstore": "pstore"}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the objects."""
    test = Test(session)
    print(
        f"{KINDS[test.kind]} test against {test.origin.svc} ({test.origin.url}),"
        + f" objects {test.base_url}/{test.run}-*\n"
    )
    skips = SKIPS.get(test.kind, {})
    try:
        for name in selected:
            if name in skips:
                results.add(name, SKIP, skips[name])
                continue
            if name == "posc-expiry":
                for case, row in posc_expiry(test).items():
                    results.add(f"{name} {case}", *row)
                continue
            try:
                status, note = RUN[name](test)
            except CouldNotConnect as e:
                status, note = FAIL, f"could not connect: {e}"
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # Every scenario's objects, and whatever an interrupted upload left;
        # and posc-expiry's own staging directory.
        errors = stores.empty_tree_everywhere(
            session.fed.origins, session.fed.exports, "data/posc", session.setup_tokens()
        )
        if test.kind == "posc":
            try:
                shutil.rmtree(planted_dir(test))
            except FileNotFoundError:
                pass
            except OSError as e:
                errors.append(f"removing framework/var/{planted_dir(test)}: {e.strerror}")
        if errors:
            warn(f"could not empty /protected-a/data/posc: {first(errors)}")
