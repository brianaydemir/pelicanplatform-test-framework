"""A stub HTTP server in the dev container, for the servers to fetch from.

The federation's servers fetch from it as a third-party COPY's source,
say, which must answer as a test tells it: an error, or a body shorter
than the length it claims.

The dev container is `dev` on the federation's network, which every
container shares (docker-compose.yaml; docker-compose.tiny.yaml joins
it), so a server reaches the stub at http://dev:<port>/, on a port that
the kernel picks, which needs no mapping to the host. Origins fetch
from private addresses only because
framework/config.d/base/40-origin.yaml allows them
(Server.SSRFProtection.AllowedCIDRs).
"""

import contextlib
import http.server
import threading
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Optional

# The dev container's host name on the federation's network.
HOST = "dev"


@dataclass(frozen=True)
class Reply:
    """What the stub answers a GET of a path.

    Attributes:
        length: The Content-Length to claim, if not the body's: a larger
            one makes the body end early, as a source that fails partway
            does.
    """

    status: int = 200
    body: bytes = b""
    length: Optional[int] = None


@dataclass(frozen=True)
class Seen:
    """A request that the stub received."""

    method: str
    path: str
    headers: tuple[tuple[str, str], ...]

    def header(self, name: str) -> str:
        """Return the last value of header name, or "" if it has none."""
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        return values[-1] if values else ""


class Stub:
    """The stub: its replies by path, and the requests it has received.

    Use serving() to run it.

    Attributes:
        port: The port it listens on, once serving() has started it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._replies: dict[str, Reply] = {}
        self._seen: list[Seen] = []
        self.port = 0

    def url(self, path: str) -> str:
        """Return the URL at which a server reaches path at the stub."""
        return f"http://{HOST}:{self.port}{path}"

    def reply(self, path: str, reply: Reply) -> str:
        """Answer a GET of path with reply from now on; return its URL."""
        with self._lock:
            self._replies[path] = reply
        return self.url(path)

    def seen(self, path: str) -> list[Seen]:
        """Return the requests for path the stub has received, in order."""
        with self._lock:
            return [s for s in self._seen if s.path == path]

    def answer(self, method: str, path: str, headers: tuple[tuple[str, str], ...]) -> Reply:
        """Record a request, and return the reply to it."""
        with self._lock:
            self._seen.append(Seen(method, path, headers))
            return self._replies.get(path, Reply(404, b"no such path\n"))

    @contextlib.contextmanager
    def serving(self) -> Generator[None, None, None]:
        """Serve, in a thread of its own, until the block ends.

        Raises:
            OSError: The stub cannot listen.
        """
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """The stub's request handler.

            It answers each GET or HEAD as the stub's reply for its path
            says. It speaks HTTP/1.0 (BaseHTTPRequestHandler's
            protocol_version), so the connection closes after each
            answer, and a client reads a body shorter than its
            Content-Length to an early end.
            """

            def reply(self, body: bool) -> None:
                """Send the stub's reply, with its body if body."""
                found = stub.answer(self.command, self.path, tuple(self.headers.items()))
                self.send_response(found.status)
                self.send_header("Content-Type", "application/octet-stream")
                length = len(found.body) if found.length is None else found.length
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if body:
                    self.wfile.write(found.body)

            def do_GET(self) -> None:  # pylint: disable=invalid-name
                """Answer a GET."""
                self.reply(True)

            def do_HEAD(self) -> None:  # pylint: disable=invalid-name
                """Answer a HEAD."""
                self.reply(False)

            # pylint: disable-next=redefined-builtin
            def log_message(self, format: str, *args: Any) -> None:
                """Log nothing: the tests read what the stub saw instead."""

        # Every container on the federation's network must reach it.
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)  # nosec B104
        server.daemon_threads = True
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
