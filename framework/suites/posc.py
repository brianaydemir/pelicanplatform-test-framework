"""POSC (persist on successful close; `-p origin-posc`): interrupt
uploads to an origin and check what its store shows. On a pstore origin
(`-p origin-pstore`), which commits a new version only when an upload
completes, run the same uploads and check what the origins serve instead;
the scenarios about POSC's staging files are skipped. Neither: the suite
is skipped.

Uploads go straight to the first origin in
framework/var/generated/origins, under /protected-a/data/posc/. Only
`client` goes through the director. An interruption is judged only once
the upload is seen under way: a new staging file under POSC, or growth in
pstore/objects on pstore. Afterward, /protected-a/data/posc is emptied on
every store.
"""

import glob
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from testlib import blocks, common, stores, web
from testlib.common import die, shown, wait_for
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "commit":    "a complete upload lands, and leaves no staging file",
    "stalled":   "while an upload stalls, only its staging file exists",
    "sized":     "an upload cut short of its Content-Length leaves nothing",
    "chunked":   "a chunked upload cut short leaves nothing",
    "overwrite": "an overwrite cut short leaves the old object intact",
    "client":    "killing `pelican object put` midway leaves nothing",
    "hidden":    "an HTML listing of the export does not show .pelican-posc",
}

# The scenarios about POSC's staging, which a pstore origin has none of.
POSC_ONLY = ("stalled", "hidden")

MiB = 1 << 20


class CouldNotConnect(Exception):
    """partial_put() could not connect, or finish the TLS handshake."""


def partial_put(url: str, mode: str, total: int, send: int, hold: float, token: str,
                sent: Optional[threading.Event] = None,
                release: Optional[threading.Event] = None) -> Optional[str]:
    """An upload cut short: send the headers and `send` bytes, hold the
    connection for `hold` seconds or until release is set, then close its
    sending side as a dying client would (no TLS close_notify). mode is
    `sized`, claiming a Content-Length of total, or `chunked`. Sets sent
    once the bytes are out. Returns the status that comes back, if any;
    raises CouldNotConnect if it never got as far as sending."""
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
                        raise OSError("connection closed during the TLS handshake")
            flush()
        except OSError as e:  # including ssl.SSLError
            raise CouldNotConnect(f"TLS handshake: {e}") from e

        target = u.path + (f"?{u.query}" if u.query else "")
        length = f"Content-Length: {total}" if mode == "sized" else "Transfer-Encoding: chunked"
        head = [f"PUT {target} HTTP/1.1", f"Host: {u.netloc}",
                f"Authorization: Bearer {token}", length]
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
    """How partial_put's status reads in a note."""
    return f"HTTP {status}" if status else "no response"


def wrong_answer(status: Optional[str]) -> Optional[str]:
    """Why partial_put's status is the wrong answer to an upload cut
    short, if it is."""
    if status and status.startswith("2"):
        return f"{http(status)} for an incomplete upload"
    if status == "405":
        return f"{http(status)} (Method Not Allowed) for a PUT"
    return None


class Test:
    """What every scenario needs: a token, the first origin, and the
    stores to watch. The director may send `client` to any origin, so
    every store is watched. A pstore origin's store is asked over HTTPS
    what it holds."""

    def __init__(self, session: Session):
        fed = session.fed
        self.fed = fed
        self.pelican = session.pelican
        self.env = session.env
        self.origin = fed.origins[0]
        self.origin_web = re.sub(r"^(https://[^/]*).*", r"\1", self.origin.url)
        self.pstore = not fed.posc
        self.origins = stores.unique(fed.origins)
        self.stores = sorted({o.store for o in fed.origins})
        self.tmp = session.path("posc")
        os.makedirs(self.tmp)
        self.run = session.run
        self.base_url = f"{self.origin.url}/protected-a/data/posc"
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        self.before: Set[str] = set()
        self.before_sizes: Dict[str, int] = {}
        if self.pstore:
            errors = stores.make_dirs(fed.origins, "protected-a", "data/posc", self.token)
            if errors:
                die(f"could not make /protected-a/data/posc: {errors[0]}")

    def staging(self) -> Set[str]:
        """Staging files: <store>/.pelican-posc/<user>/in_progress.*, where
        the user is the token's subject."""
        found = set()
        for store in self.stores:
            found.update(glob.glob(f"{store}/.pelican-posc/**/in_progress.*", recursive=True))
        return {f for f in found if os.path.isfile(f)}

    def pstore_sizes(self) -> Dict[str, int]:
        """The files of each pstore origin's objects, and their sizes."""
        found = {}
        for store in self.stores:
            for path in glob.glob(f"{store}/pstore/objects/**", recursive=True):
                if os.path.isfile(path):
                    found[path] = os.path.getsize(path)
        return found

    def mark(self) -> None:
        """Remember the staging files there are now, or on pstore, how big
        its objects' files are."""
        self.before = self.staging()
        if self.pstore:
            self.before_sizes = self.pstore_sizes()

    def under_way(self) -> bool:
        """Whether an upload has begun to arrive since mark(): a staging
        file, or on pstore, an objects' file that is new or bigger."""
        if not self.pstore:
            return self.new_staging() is not None
        now = self.pstore_sizes()
        return any(size > self.before_sizes.get(path, -1) for path, size in now.items())

    def new_staging(self) -> Optional[str]:
        """A staging file that has appeared since mark(), if any."""
        added = sorted(self.staging() - self.before)
        return added[0] if added else None

    def object_file(self, name: str) -> Optional[str]:
        """The object's file in the first store that has it, if any."""
        for store in self.stores:
            path = f"{store}/data/posc/{name}"
            if os.path.exists(path):
                return path
        return None

    def found(self, name: str) -> Optional[str]:
        """Where the object is, if anywhere: its file, or on pstore, the
        first origin that serves it."""
        if not self.pstore:
            return self.object_file(name)
        for origin in self.origins:
            url = f"{origin.url}/protected-a/data/posc/{name}"
            answer = web.request("HEAD", url, token=self.token)
            if answer.status == 200:
                return url
            if answer.status != 404:
                raise stores.Unreadable(f"HEAD {url}: {answer.describe()}")
        return None

    def size(self, where: str) -> str:
        """How big the object found() at where is."""
        if not self.pstore:
            return str(size_of(where))
        return web.request("HEAD", where, token=self.token).header("Content-Length") or "?"

    def holds(self, name: str, data: bytes) -> bool:
        """Whether the object that found() finds holds data."""
        if not self.pstore:
            path = self.object_file(name)
            return path is not None and holds(path, data)
        return common.digest(data) in stores.held_anywhere(
            self.origins, "protected-a", f"data/posc/{name}", {"protected-a": self.token})

    def remove(self, name: str) -> None:
        """Remove the object, wherever it is."""
        for origin in self.origins:
            stores.delete(origin, "protected-a", f"data/posc/{name}", self.token)

    def full_put(self, data: bytes, name: str) -> web.Response:
        return web.request("PUT", f"{self.base_url}/{name}", token=self.token, upload=data)

    def partial_put(self, name: str, mode: str, total: int, send: int, hold: float,
                    sent: Optional[threading.Event] = None,
                    release: Optional[threading.Event] = None) -> Optional[str]:
        """partial_put() to object name. A connection that fails once the
        upload is sent counts as no response; one that never connects
        raises CouldNotConnect."""
        try:
            return partial_put(f"{self.base_url}/{name}", mode, total, send, hold,
                               self.token, sent, release)
        except OSError as e:
            print(f"{common.PROG}: {name}: {e}", file=sys.stderr)
            return None


class Upload:
    """A Test.partial_put() in a thread of its own, so that the stores can
    be watched while it is held open: until finish(), or for hold seconds
    at most. Afterward, status is what came back, and error why it never
    got under way, if it didn't."""

    def __init__(self, test: Test, name: str, mode: str, total: int, send: int,
                 hold: float = 60):
        self.sent = threading.Event()
        self.released = threading.Event()
        self.status: Optional[str] = None
        self.error: Optional[str] = None
        self.thread = threading.Thread(target=self._put, args=(test, name, mode, total, send, hold),
                                       daemon=True)
        self.thread.start()

    def _put(self, test: Test, name: str, mode: str, total: int, send: int, hold: float) -> None:
        try:
            self.status = test.partial_put(name, mode, total, send, hold, self.sent, self.released)
        except CouldNotConnect as e:
            self.error = f"could not connect: {e}"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def started(self, seconds: float = 30) -> bool:
        """Whether the bytes are out within seconds."""
        wait_for(lambda: self.sent.is_set() or not self.thread.is_alive(), seconds, 0.1)
        return self.sent.is_set()

    def finish(self) -> None:
        """Let the connection go, and wait for the answer."""
        self.released.set()
        self.thread.join()


def watch(test: Test, upload: Upload) -> bool:
    """Whether upload is seen under way (Test.under_way()) within 10
    seconds of its bytes going out, while it is held open."""
    return upload.started() and wait_for(test.under_way, 10)


def unseen(test: Test, upload: Upload) -> str:
    """Why an upload that was not seen under way fails, once finished: a
    refusal (e.g. 400, 401, 403, or 411) or no answer leaves nothing to
    judge the interruption by."""
    if upload.error:
        return upload.error
    what = "pstore/objects did not grow" if test.pstore else "no staging file appeared"
    return f"the upload never got under way: {what} ({http(upload.status)})"


def holds(path: str, data: bytes) -> bool:
    with open(path, "rb") as f:
        return f.read() == data


def size_of(path: str) -> int:
    return os.path.getsize(path)


#---------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.

def commit(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-commit"
    data = os.urandom(MiB)
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


def stalled_check(test: Test, name: str) -> Tuple[str, str]:
    """`stalled`, while object name's upload is held open and its staging
    file exists."""
    metrics = web.request("GET", f"{test.origin_web}/metrics").body.decode(errors="replace")
    active = re.findall(r"^pelican_origin_posc_active_uploads (\S+)", metrics, re.M)
    answer = web.request("HEAD", f"{test.base_url}/{name}", token=test.token)
    path = test.object_file(name)
    if path is not None:
        return FAIL, f"{shown(path)} is visible before the upload finished"
    if answer.status != 404:
        return FAIL, f"HEAD got {answer.describe()}, not 404"
    if not active or float(active[0]) < 1:
        return FAIL, f"pelican_origin_posc_active_uploads is '{active[0] if active else ''}'"
    return PASS, shown(test.new_staging())


def cut_short(test: Test, name: str, upload: Upload) -> Tuple[str, str]:
    """`sized` or `chunked`, once object name's upload, seen under way,
    has been cut short: nothing is left of it, and the answer, if any,
    is not a success."""
    status = upload.status
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"{http(status)}; staging file left: {shown(test.new_staging())}"
    path = test.found(name)
    if path is not None:
        return FAIL, (f"{http(status)}, and {shown(path)} holds {test.size(path)} bytes of the"
                      " partial upload")
    wrong = wrong_answer(status)
    if wrong:
        return FAIL, wrong
    return PASS, http(status)


def stalled_and_sized(test: Test, add: Callable[[str, str, str], None], stalled: bool,
                      sized: bool) -> None:
    """One upload, held open and then cut short: `stalled` looks while it
    is held, and `sized` after. Each is added as it is decided."""
    name = f"{test.run}-stalled"
    test.mark()
    upload = Upload(test, name, "sized", 2 * MiB, MiB)
    try:
        seen = watch(test, upload)
        if stalled and seen:
            add("stalled", *stalled_check(test, name))
    finally:
        upload.finish()
    if stalled and not seen:
        add("stalled", FAIL, upload.error or ("no staging file appeared" if upload.sent.is_set()
                                              else "could not start the upload"))
    if sized:
        add("sized", *(cut_short(test, name, upload) if seen else (FAIL, unseen(test, upload))))


def chunked(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-chunked"
    test.mark()
    # A bit more than pstore buffers before spilling into pstore/objects,
    # so that a pstore origin shows the upload under way too.
    upload = Upload(test, name, "chunked", 0, blocks.SPILL + 64 * 1024)
    try:
        seen = watch(test, upload)
    finally:
        upload.finish()
    if not seen:
        return FAIL, unseen(test, upload)
    return cut_short(test, name, upload)


def overwrite(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-overwrite"
    original = os.urandom(MiB)
    answer = test.full_put(original, name)
    path = test.found(name)
    if path is None:
        return FAIL, f"the original's upload did not land ({answer.describe()})"
    status = test.partial_put(name, "sized", 2 * MiB, MiB, 2)
    time.sleep(2)
    if test.found(name) is None:
        return FAIL, f"{http(status)}; the original is gone"
    if not test.holds(name, original):
        return FAIL, f"{http(status)}; {shown(path)} no longer holds the original"
    wrong = wrong_answer(status)
    if wrong:
        return FAIL, wrong
    return PASS, http(status)


def client(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-client"
    total = 1 << 30
    # Sparse: the client reads 1 GiB of zeros, with no 1 GiB written first.
    source = os.path.join(test.tmp, "client")
    with open(source, "wb") as f:
        f.truncate(total)
    log_file = os.path.join(test.tmp, "client.log")
    test.mark()
    with open(log_file, "wb") as log:
        upload = subprocess.Popen(
            [test.pelican, "object", "put", "--token", test.token_file, source,
             f"{test.fed.url}/protected-a/data/posc/{name}"],
            env=test.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    # Kill it once the upload is under way, unless it finishes first, or
    # is not seen to begin within 30 seconds.
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
    upload.wait()
    os.remove(source)
    time.sleep(3)
    path = test.found(name)
    try:
        if not seen:
            if code is None:
                return INCONCLUSIVE, "could not see the upload begin"
            if code != 0:
                with open(log_file, errors="replace") as f:
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


def hidden(test: Test) -> Tuple[str, str]:
    """Lists the export while an upload is held open, so that
    .pelican-posc is there to hide. Pelican's HTML listing has a row per
    entry, `<a href="...">name</a>`, and the export holds data/."""
    name = f"{test.run}-hidden"
    posc_dir = f"{test.origin.store}/.pelican-posc"
    url = f"{test.origin.url}/protected-a/"
    test.mark()
    upload = Upload(test, name, "sized", 2 * MiB, MiB)
    answer: Optional[web.Response] = None
    try:
        staged = upload.started() and wait_for(lambda: test.new_staging() is not None, 10)
        there = os.path.isdir(posc_dir)
        if staged and there:
            answer = web.request("GET", url, token=test.token, headers={"Accept": "text/html"})
    finally:
        upload.finish()
    if answer is None:
        if not staged:
            return FAIL, upload.error or f"no staging file appeared ({http(upload.status)})"
        return FAIL, f"{shown(posc_dir)} does not exist, although an upload is staged"
    if answer.status != 200:
        return FAIL, f"the listing got {answer.describe()}, not 200"
    if b">data</a>" not in answer.body:
        return FAIL, f"{url} does not list data/, so it is not the export's listing"
    if b".pelican-posc" in answer.body:
        return FAIL, f"{url} lists .pelican-posc"
    return PASS, f"{shown(posc_dir)} exists"


RUN = {"commit": commit, "chunked": chunked, "overwrite": overwrite, "client": client,
       "hidden": hidden}


def skip(fed: common.Federation) -> Optional[str]:
    if not fed.posc and not fed.origins[0].pstore:
        return "POSC is off and the origins are not pstore ('-p origin-posc' or '-p origin-pstore')"
    if "Writes" not in fed.exports.get("protected-a", ()):
        return "/protected-a takes no writes"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    test = Test(session)
    kind = "pstore" if test.pstore else "POSC"
    print(f"{kind} test against {test.origin.svc} ({test.origin.url}),"
          f" objects {test.base_url}/{test.run}-*\n")
    added: Set[str] = set()

    def add(scenario: str, status: str, note: str = "") -> None:
        added.add(scenario)
        results.add(scenario, status, note)

    try:
        for name in selected:
            if test.pstore and name in POSC_ONLY:
                add(name, SKIP, "POSC staging only; pstore commits a version at close")
                continue
            covers = [name]
            if name in ("stalled", "sized"):
                # One upload serves both; run it for whichever comes first.
                stalled = "stalled" in selected and not test.pstore
                if name == "sized" and stalled:
                    continue
                covers = [s for s, on in (("stalled", stalled), ("sized", "sized" in selected))
                          if on]
            try:
                if name in ("stalled", "sized"):
                    stalled_and_sized(test, add, "stalled" in covers, "sized" in covers)
                else:
                    add(name, *RUN[name](test))
            except CouldNotConnect as e:
                for scenario in (s for s in covers if s not in added):
                    add(scenario, FAIL, f"could not connect: {e}")
            except Exception as e:
                for scenario in (s for s in covers if s not in added):
                    add(scenario, FAIL, f"{type(e).__name__}: {e}")
    finally:
        # Every scenario's objects, and whatever an interrupted upload left.
        errors = stores.empty_tree_everywhere(session.fed.origins, session.fed.exports,
                                              "data/posc", session.tokens())
        if errors:
            common.warn(f"could not empty /protected-a/data/posc: {common.first(errors)}")
