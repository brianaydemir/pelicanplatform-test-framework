"""POSC (persist on successful close; `-p origin-posc`): interrupt
uploads to an origin and check what its store shows. On a pstore origin
(`-p origin-pstore`), which commits a new version only when an upload
completes, run the same uploads and check what the origins serve instead;
the scenarios about POSC's staging files are skipped. Neither: the suite
is skipped.

Uploads go straight to the first origin in
framework/var/generated/origins, under /protected-a/data/posc/. Only
`client` goes through the director.
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
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from testlib import common, stores, web
from testlib.common import die, wait_for
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session

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


def partial_put(url: str, mode: str, total: int, send: int, hold: float, token: str,
                sent: Optional[threading.Event] = None) -> Optional[str]:
    """An upload cut short: send the headers and `send` bytes, hold the
    connection for `hold` seconds, then close its sending side as a dying
    client would (no TLS close_notify). mode is `sized`, claiming a
    Content-Length of total, or `chunked`. Sets sent once the bytes are
    out. Returns the status that comes back, if any."""
    u = urlsplit(url)
    sock = socket.create_connection((u.hostname, u.port or 443), timeout=60)
    try:
        # TLS over memory buffers, so that the socket can be half-closed
        # without TLS saying goodbye.
        tls_in, tls_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = web.tls_context().wrap_bio(tls_in, tls_out, server_hostname=u.hostname)

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

        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                flush()
                if not pump():
                    raise OSError("connection closed during the TLS handshake")
        flush()

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
                    sent: Optional[threading.Event] = None) -> Optional[str]:
        """partial_put() to object name. A connection that fails counts as
        no response."""
        try:
            return partial_put(f"{self.base_url}/{name}", mode, total, send, hold,
                               self.token, sent)
        except OSError as e:
            print(f"{common.PROG}: {name}: {e}", file=sys.stderr)
            return None


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
        return FAIL, f"{path} differs from what was uploaded"
    if not wait_for(lambda: test.new_staging() is None, 10):
        return FAIL, f"staging file left behind: {test.new_staging()}"
    return PASS, path


def stalled_and_sized(test: Test, results: common.Results, stalled: bool,
                      sized: bool) -> None:
    """One upload that stalls, then is cut short: `stalled` looks while it
    stalls, and `sized` after. It stalls for less when no one looks."""
    name = f"{test.run}-stalled"
    test.mark()
    sent = threading.Event()
    outcome = {}

    def upload() -> None:
        outcome["status"] = test.partial_put(name, "sized", 2 * MiB, MiB, 15 if stalled else 2,
                                             sent)

    helper = threading.Thread(target=upload)
    helper.start()

    if not stalled:
        pass
    elif not sent.wait(30):
        results.add("stalled", FAIL, "could not start the upload")
    elif not wait_for(lambda: test.new_staging() is not None, 10):
        results.add("stalled", FAIL, "no staging file appeared")
    else:
        metrics = web.request("GET", f"{test.origin_web}/metrics").body.decode(errors="replace")
        active = re.findall(r"^pelican_origin_posc_active_uploads (\S+)", metrics, re.M)
        answer = web.request("HEAD", f"{test.base_url}/{name}", token=test.token)
        path = test.object_file(name)
        if path is not None:
            results.add("stalled", FAIL, f"{path} is visible before the upload finished")
        elif answer.status != 404:
            results.add("stalled", FAIL, f"HEAD got {answer.describe()}, not 404")
        elif not active or float(active[0]) < 1:
            results.add("stalled", FAIL,
                        f"pelican_origin_posc_active_uploads is '{active[0] if active else ''}'")
        else:
            results.add("stalled", PASS, test.new_staging() or "")

    helper.join()
    if not sized:
        return
    status = outcome.get("status")
    path = test.found(name)
    wrong = wrong_answer(status)
    if path is not None:
        results.add("sized", FAIL, f"{http(status)}, and {path} holds the partial upload")
    elif not wait_for(lambda: test.new_staging() is None, 10):
        results.add("sized", FAIL, f"{http(status)}; staging file left: {test.new_staging()}")
    elif wrong:
        results.add("sized", FAIL, wrong)
    else:
        results.add("sized", PASS, http(status))


def chunked(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-chunked"
    test.mark()
    status = test.partial_put(name, "chunked", 0, MiB, 2)
    time.sleep(2)
    path = test.found(name)
    if path is not None:
        return FAIL, f"{http(status)}, and {path} holds {test.size(path)} bytes of the partial upload"
    if test.new_staging() is not None:
        return FAIL, f"{http(status)}; staging file left: {test.new_staging()}"
    wrong = wrong_answer(status)
    if wrong:
        return FAIL, wrong
    return PASS, http(status)


def overwrite(test: Test) -> Tuple[str, str]:
    name = f"{test.run}-overwrite"
    original = os.urandom(MiB)
    answer = test.full_put(original, name)
    path = test.found(name)
    if path is None:
        return INCONCLUSIVE, f"could not upload the original ({answer.describe()})"
    status = test.partial_put(name, "sized", 2 * MiB, MiB, 2)
    time.sleep(2)
    if test.found(name) is None:
        return FAIL, f"{http(status)}; the original is gone"
    if not test.holds(name, original):
        return FAIL, f"{http(status)}; {path} no longer holds the original"
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
    test.mark()
    with open(os.path.join(test.tmp, "client.log"), "wb") as log:
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
    finished = upload.poll() is not None
    upload.kill()
    upload.wait()
    os.remove(source)
    time.sleep(3)
    path = test.found(name)
    try:
        if not seen:
            return INCONCLUSIVE, ("the upload finished before it could be interrupted" if finished
                                  else "could not see the upload begin")
        if path is not None:
            return FAIL, f"{path} holds {test.size(path)} of {total} bytes"
        return PASS, ""
    finally:
        # Don't leave up to 1 GiB behind.
        if path is not None:
            test.remove(name)


def hidden(test: Test) -> Tuple[str, str]:
    answer = web.request("GET", f"{test.origin.url}/protected-a/", token=test.token,
                         headers={"Accept": "text/html"})
    if answer.status != 200:
        return INCONCLUSIVE, f"the listing got {answer.describe()}"
    if b".pelican-posc" in answer.body:
        return FAIL, f"{test.origin.url}/protected-a/ lists .pelican-posc"
    return PASS, ""


RUN = {"commit": commit, "chunked": chunked, "overwrite": overwrite, "client": client,
       "hidden": hidden}


def skip(fed: common.Federation) -> Optional[str]:
    if not fed.posc and not fed.origins[0].pstore:
        return "POSC is off and the origins are not pstore ('-p origin-posc' or '-p origin-pstore')"
    return None


def run(session: Session, selected: List[str], results: common.Results) -> None:
    test = Test(session)
    kind = "pstore" if test.pstore else "POSC"
    print(f"{kind} test against {test.origin.svc} ({test.origin.url}),"
          f" objects {test.base_url}/{test.run}-*\n")
    for name in selected:
        if test.pstore and name in POSC_ONLY:
            results.add(name, SKIP, "POSC staging only; pstore commits a version at close")
        elif name not in ("stalled", "sized"):
            results.add(name, *RUN[name](test))
        # One upload serves both; run it for whichever comes first.
        elif name == "stalled" or "stalled" not in selected or test.pstore:
            stalled_and_sized(test, results, "stalled" in selected and not test.pstore,
                              "sized" in selected)
