"""HTTP(S) requests straight to a server, bypassing the Pelican client.

Like curl without -L: a redirect is an answer, not something to follow.
Unlike http.client, it keeps a chunked body's trailers, such as the
X-Transfer-Status that a V2 cache sends, when asked (`X-Transfer-Status:
true` and `TE: trailers`), to report a failure after its headers.
"""

import functools
import http.client
import json
import socket
import ssl
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Union
from urllib.parse import urlsplit

# The federation's CA, in the dev container.
CA_FILE = "/certs/ca.crt"


@functools.lru_cache(maxsize=None)
def tls_context() -> ssl.SSLContext:
    """Return a TLS context that trusts the federation's CA, made once."""
    return ssl.create_default_context(cafile=CA_FILE)


@dataclass
class Response:
    """A server's answer, or the lack of one.

    Attributes:
        status: Its status; None if no response came.
        trailers: Those after a chunked body.
    """

    status: Optional[int]
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    trailers: tuple[tuple[str, str], ...] = ()

    def header(self, name: str) -> str:
        """Return the last value of header name, or ""."""
        return _last(self.headers, name)

    def trailer(self, name: str) -> str:
        """Return the last value of trailer name, or ""."""
        return _last(self.trailers, name)

    def cookie(self, name: str) -> str:
        """Return the value of cookie name that the answer set, or ""."""
        for key, value in self.headers:
            if key.lower() == "set-cookie" and value.startswith(f"{name}="):
                return value[len(name) + 1 :].split(";", 1)[0]
        return ""

    @property
    def ok(self) -> bool:
        """Whether the status is a success (2xx)."""
        return self.status is not None and 200 <= self.status < 300

    def describe(self) -> str:
        """Return the status, for a message."""
        return "no response" if self.status is None else f"HTTP {self.status}"

    def json(self) -> Any:
        """Return the body, as JSON; None if it is not JSON."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def _last(fields: tuple[tuple[str, str], ...], name: str) -> str:
    """Return the last value of field name in fields, or ""."""
    values = [v for k, v in fields if k.lower() == name.lower()]
    return values[-1] if values else ""


def save(path: str, answer: Response) -> None:
    """Keep answer's body at path.

    If it has none (a HEAD's), keep its headers.
    """
    with open(path, "wb") as f:
        if answer.body:
            f.write(answer.body)
        else:
            f.write("".join(f"{k}: {v}\n" for k, v in answer.headers).encode())


class _Answer(http.client.HTTPResponse):
    """An HTTPResponse that keeps the trailers after a chunked body.

    HTTPResponse reads them and throws them away, in
    _read_and_discard_trailer() (so named in Python 3.9 through 3.14, at
    least). Reading the body to its end, with read(), reads them.
    """

    trailers: tuple[tuple[str, str], ...] = ()

    def _read_and_discard_trailer(self) -> None:
        found: list[tuple[str, str]] = []
        while True:
            line = self.fp.readline(1 << 16)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            found.append((name.strip(), value.strip()))
        self.trailers = tuple(found)


class _HTTPConnection(http.client.HTTPConnection):
    response_class = _Answer


class _HTTPSConnection(http.client.HTTPSConnection):
    response_class = _Answer


def _pieces(data: bytes, size: int = 1 << 16) -> Iterator[bytes]:
    """Yield data, size bytes at a time."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _connect(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """Return a connection to url's server, and the target to ask it for."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = _HTTPSConnection(
            parts.hostname or "", parts.port, timeout=timeout, context=tls_context()
        )
    else:
        conn = _HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
    return conn, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def request(
    method: str,
    url: str,
    token: Optional[str] = None,
    upload: Union[bytes, str, None] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 60,
    chunked: bool = False,
) -> Response:
    """Send method to url, and return the answer.

    A chunked answer's trailers are in the response's trailers.

    Args:
        method: The HTTP method (e.g. `GET`).
        url: Where to send it.
        token: A bearer token to present: the token itself, not a file.
        upload: The body: bytes, or the path of a file.
        headers: More headers to send.
        timeout: Seconds for each read or write of the socket, not the
            whole exchange.
        chunked: Whether to send the body in chunks with no
            Content-Length, as the Pelican client sends it.
    """
    conn, target = _connect(url, timeout)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    data: Optional[bytes] = None
    if isinstance(upload, str):
        with open(upload, "rb") as f:
            data = f.read()
    else:
        data = upload
    body: Union[bytes, Iterable[bytes], None] = data
    if chunked and data is not None:
        body = _pieces(data)
    try:
        conn.request(method, target, body=body, headers=sent, encode_chunked=chunked)
        answer = conn.getresponse()
        body = answer.read()
        trailers = answer.trailers if isinstance(answer, _Answer) else ()
        return Response(answer.status, body, tuple(answer.getheaders()), trailers)
    except (OSError, http.client.HTTPException):
        return Response(None)
    finally:
        conn.close()


@dataclass
class Timed:
    """A GET, and when its body came, in seconds from when it was sent.

    Attributes:
        first: When the first bytes asked for had come, if they did.
        whole: When the whole body had come, if it did.
    """

    response: Response
    first: Optional[float]
    whole: Optional[float]


def timed_get(
    url: str,
    token: Optional[str],
    first: int,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 60,
) -> Timed:
    """GET url, as request() does, timing the first `first` bytes, then all.

    The times are as a client that reads a body as it streams in sees
    them.
    """
    conn, target = _connect(url, timeout)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    head: Optional[float] = None
    try:
        conn.request("GET", target, headers=sent)
        answer = conn.getresponse()
        body = answer.read(first)
        if len(body) == first:
            head = time.monotonic() - started
        body += answer.read()
        whole = time.monotonic() - started
        return Timed(Response(answer.status, body, tuple(answer.getheaders())), head, whole)
    except (OSError, http.client.HTTPException):
        return Timed(Response(None), head, None)
    finally:
        conn.close()


def statuses_before_body(
    method: str,
    url: str,
    token: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 10,
) -> tuple[int, ...]:
    """Return the statuses that url's server sends before a request's body.

    Send method's request line and headers to url, but none of the body
    they announce, as a client waiting on `Expect: 100-continue` does,
    and read what the server answers meanwhile. http.client can't say:
    it passes over a 100 Continue, and sends a body whole.

    Returns:
        Each status the server sends, any interim one (100 Continue)
        first, up to the first final one. Fewer if the server says
        nothing more within timeout, or hangs up; none if it never
        answers.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    lines = [f"{method} {target} HTTP/1.1", f"Host: {parts.netloc}", "Connection: close"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    found: list[int] = []
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            conn = raw
            if parts.scheme == "https":
                conn = tls_context().wrap_socket(raw, server_hostname=host)
        except OSError:
            raw.close()
            raise
        with conn:
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            pending = b""
            while not found or found[-1] < 200:
                head, sep, rest = pending.partition(b"\r\n\r\n")
                if sep:
                    found.append(int(head.split(b"\r\n", 1)[0].split(b" ", 2)[1]))
                    pending = rest
                    continue
                more = conn.recv(1 << 16)
                if not more:
                    break
                pending += more
    except (OSError, ValueError, IndexError):
        pass
    return tuple(found)


def read_part(
    url: str,
    token: Optional[str],
    size: int,
    timeout: float = 60,
    headers: Optional[Mapping[str, str]] = None,
) -> Response:
    """GET url, with headers, and hang up once size bytes of the body come.

    So does a client that gives up. This is also how to ask for a range
    of an object too large to read whole, from a server that might
    ignore Range and send it all.

    Returns:
        The response, with those bytes.
    """
    conn, target = _connect(url, timeout)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    try:
        conn.request("GET", target, headers=sent)
        answer = conn.getresponse()
        return Response(answer.status, answer.read(size), tuple(answer.getheaders()))
    except (OSError, http.client.HTTPException):
        return Response(None)
    finally:
        conn.close()
