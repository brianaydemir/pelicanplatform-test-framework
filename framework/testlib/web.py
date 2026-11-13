"""HTTP(S) requests straight to a server, bypassing the Pelican client.

request() is like curl without -L: a redirect is an answer, not
something to follow; follow() follows them, as curl -L does.
Unlike http.client, it keeps a chunked body's trailers, such as the
X-Transfer-Status that a V2 cache sends, when asked (`X-Transfer-Status:
true` and `TE: trailers`), to report a failure after its headers.

Each exchange is logged, once log_to() says where, as `curl -v` shows
one: the request and its headers (`>`), then the answer's (`<`). Each
answer is also scanned for tokens whose signatures came back in the
clear (testlib.leaks).
"""

import functools
import http.client
import json
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit

from . import leaks

# The federation's CA, in the dev container.
CA_FILE = "/certs/ca.crt"

# What asks a cache to answer only from what it holds, and otherwise 504.
ONLY_IF_CACHED = {"Cache-Control": "only-if-cached"}

# How much of an answer's body its log entry shows, in bytes.
LOGGED_BODY = 512

# How many requests follow() makes at most, if fewer than curl's default
# of 50 redirects (--max-redirs): a director's redirect is one, and a
# tiering cache's one more.
HOPS = 5


@functools.lru_cache(maxsize=None)
def tls_context() -> ssl.SSLContext:
    """Return a TLS context that trusts the federation's CA, made once."""
    return ssl.create_default_context(cafile=CA_FILE)


@dataclass
class Response:
    """A server's answer, or the lack of one.

    Attributes:
        status: Its status; None if no whole response came.
        body: Its body.
        headers: Its headers.
        trailers: Those after a chunked body.
        error: If none came, what went wrong, e.g. a status then a body
            that broke off.
    """

    status: int | None
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    trailers: tuple[tuple[str, str], ...] = ()
    error: str = ""

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
        """Return the status, for a message, or why none came."""
        if self.status is not None:
            return f"HTTP {self.status}"
        return f"no response ({self.error})" if self.error else "no response"

    def json(self) -> Any:
        """Return the body, as JSON; None if it is not JSON."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def object(self) -> dict[str, Any]:
        """Return the body, as a JSON object; {} if it is not one."""
        found = self.json()
        return cast(dict[str, Any], found) if isinstance(found, dict) else {}


def _last(fields: tuple[tuple[str, str], ...], name: str) -> str:
    """Return the last value of field name in fields, or ""."""
    values = [v for k, v in fields if k.lower() == name.lower()]
    return values[-1] if values else ""


def served(answer: Response, data: bytes) -> str | None:
    """Return why answer is not a 200 holding data, or None."""
    if answer.status != 200:
        return answer.describe()
    if answer.body != data:
        return f"{len(answer.body)} bytes, not the object"
    return None


def age(answer: Response) -> int | None:
    """Return answer's Age, in seconds, or None if it has none."""
    value = answer.header("Age").strip()
    return int(value) if value.isdigit() else None


def multipart(boundary: str, parts: Sequence[tuple[str, bytes]]) -> bytes:
    """Return a multipart/form-data body (RFC 7578) of parts.

    Args:
        boundary: The boundary between parts.
        parts: Each a form field's name, and its bytes, sent as a file of
            that name.
    """
    body = b""
    for name, data in parts:
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        body += head.encode() + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode()


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

    HTTPResponse reads them and throws them away, in its private
    _read_and_discard_trailer(), which this overrides. Reading the body
    to its end, with read(), reads them.
    """

    trailers: tuple[tuple[str, str], ...] = ()

    def _read_and_discard_trailer(self) -> None:
        """Read the trailers, and keep them."""
        found: list[tuple[str, str]] = []
        while True:
            line = self.fp.readline(1 << 16)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            found.append((name.strip(), value.strip()))
        self.trailers = tuple(found)


class _HTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection whose answers keep their trailers."""

    response_class = _Answer


class _HTTPSConnection(http.client.HTTPSConnection):
    """An HTTPSConnection whose answers keep their trailers."""

    response_class = _Answer


def _pieces(data: bytes, size: int = 1 << 16) -> Iterator[bytes]:
    """Yield data, size bytes at a time."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _failure(status: int | None, error: Exception) -> str:
    """Return what went wrong, for a Response's error.

    Args:
        status: The status that came before error, if one did.
        error: What went wrong.
    """
    return repr(error) if status is None else f"HTTP {status}, then {error!r}"


def _with_token(headers: Mapping[str, str] | None, token: str | None) -> dict[str, str]:
    """Return headers, with token as a bearer token if one is given."""
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return sent


def _target(parts: SplitResult) -> str:
    """Return the request target of a URL: its path and query."""
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


# What log_to() was last given, if anything: it is called with each
# exchange's entry.
_LOGS: list[Callable[[str], None]] = []


def log_to(write: Callable[[str], None] | None) -> None:
    """Log each exchange from here on by calling write with its entry.

    An entry holds the request and its headers; what came of it, and how
    long that took; and the answer's headers, trailers, and the start of
    its body, if that is text. None logs nothing.
    """
    _LOGS[:] = [] if write is None else [write]


def _excerpt(body: bytes) -> str:
    """Return the start of body, for a log entry; "" if it is not text."""
    head = body[:LOGGED_BODY]
    if any(byte < 0x20 and byte not in b"\t\n\r" for byte in head):
        return ""
    text = head.decode(errors="replace").rstrip()
    return f"{text} ..." if len(body) > LOGGED_BODY else text


def _log_exchange(
    method: str,
    url: str,
    sent: Mapping[str, str],
    began: float,
    outcome: str,
    answer: Response | None = None,
) -> None:
    """Log an exchange, if log_to() said where.

    Args:
        method: The request's method.
        url: Where it went.
        sent: Its headers.
        began: When it was sent (time.time()).
        outcome: What came of it.
        answer: The answer, if one is to be shown.
    """
    if not _LOGS:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(began))
    lines = [
        f"==> {stamp}.{int(began % 1 * 1000):03d} {method} {url}",
        f"    {outcome} after {time.time() - began:.2f}s",
    ]
    lines += [f"> {name}: {value}" for name, value in sent.items()]
    if answer is not None:
        lines += [f"< {name}: {value}" for name, value in answer.headers]
        lines += [f"< {name}: {value} (trailer)" for name, value in answer.trailers]
        text = _excerpt(answer.body)
        if text:
            lines += ["--- body", text]
    for write in list(_LOGS):
        write("\n".join(lines) + "\n\n")


def _scan(method: str, url: str, sent: Mapping[str, str], answer: Response) -> None:
    """Scan answer for tokens whole, as testlib.leaks has it."""
    leaks.answer(method, url, sent, answer.status, answer.headers, answer.body, answer.trailers)


def _received(answer: Response) -> str:
    """Return what came of an exchange, for its log entry."""
    return f"{answer.describe()}, with {len(answer.body)} bytes"


def _connect(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """Return a connection to url's server, and the target to ask it for."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = _HTTPSConnection(
            parts.hostname or "", parts.port, timeout=timeout, context=tls_context()
        )
    else:
        conn = _HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
    return conn, _target(parts)


def request(
    method: str,
    url: str,
    token: str | None = None,
    upload: bytes | str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
    chunked: bool = False,
    json_body: Any = None,
    form: Mapping[str, str] | None = None,
) -> Response:
    """Send method to url, and return the answer, trailers and all.

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
        json_body: A body to send as JSON, in place of upload.
        form: Fields to send as a form, in place of upload.
    """
    conn, target = _connect(url, timeout)
    sent = _with_token(headers, token)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        sent.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urlencode(form).encode()
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(upload, str):
        with open(upload, "rb") as f:
            data = f.read()
    else:
        data = upload
    body: bytes | Iterable[bytes] | None = data
    if chunked and data is not None:
        body = _pieces(data)
    status: int | None = None
    began = time.time()
    try:
        conn.request(method, target, body=body, headers=sent, encode_chunked=chunked)
        answer = conn.getresponse()
        status = answer.status
        body = answer.read()
        trailers = answer.trailers if isinstance(answer, _Answer) else ()
        found = Response(answer.status, body, tuple(answer.getheaders()), trailers)
    except (OSError, http.client.HTTPException) as e:
        found = Response(None, error=_failure(status, e))
    finally:
        conn.close()
    outcome = _received(found)
    if data is not None:
        outcome = f"sent {len(data)} bytes{' in chunks' if chunked else ''}; {outcome}"
    _log_exchange(method, url, sent, began, outcome, found)
    _scan(method, url, sent, found)
    return found


def follow(
    method: str,
    url: str,
    token: str | None = None,
    upload: bytes | str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120,
) -> list[tuple[str, Response]]:
    """Send method to url, and follow its redirects, as `curl -L` does.

    The Authorization header goes with the first request, and with a
    redirect to the same host; curl drops it when the host changes (since
    7.58.0, unless told --location-trusted), as Go's and Python's HTTP
    clients do, so a redirect to another host is followed as given, with
    the token in its authz query parameter if the director put it there.
    The other headers, and a body, go again with each request.

    Args:
        method: The HTTP method.
        url: Where to send it first.
        token: The bearer token to present, if any.
        upload: The body, as for request().
        headers: More headers to send.
        timeout: As for request().

    Returns:
        Each request's URL and answer, in order, up to HOPS of them.
    """
    hops: list[tuple[str, Response]] = []
    current, send_token = url, token
    for _ in range(HOPS):
        answer = request(method, current, token=send_token, upload=upload, headers=headers, timeout=timeout)
        hops.append((current, answer))
        location = answer.header("Location")
        if answer.status not in (301, 302, 307, 308) or not location:
            break
        following = _resolve(current, location)
        if urlsplit(following).hostname != urlsplit(current).hostname:
            send_token = None
        current = following
    return hops


def _resolve(base: str, location: str) -> str:
    """Return the URL that a Location names, in an answer from base.

    An absolute URL, or an absolute path, is taken as it is: urljoin()
    would drop a `;` that ends a path, taking it for the start of
    parameters with none.
    """
    if urlsplit(location).scheme:
        return location
    if location.startswith("/") and not location.startswith("//"):
        parts = urlsplit(base)
        return f"{parts.scheme}://{parts.netloc}{location}"
    return urljoin(base, location)


@dataclass
class Timed:
    """A GET, and when its body came, in seconds from when it was sent.

    Attributes:
        response: The answer.
        first: When the first bytes asked for had come, if they did.
        whole: When the whole body had come, if it did.
    """

    response: Response
    first: float | None
    whole: float | None


def timed_get(
    url: str,
    token: str | None,
    first: int,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> Timed:
    """GET url, as request() does, timing the first bytes, then all.

    The times are as a client that reads a body as it streams in sees
    them.

    Args:
        url: Where to send it.
        token: A bearer token to present, if any.
        first: How many bytes to time first.
        headers: More headers to send.
        timeout: As for request().
    """
    conn, target = _connect(url, timeout)
    sent = _with_token(headers, token)
    began = time.time()
    started = time.monotonic()
    head: float | None = None
    whole: float | None = None
    status: int | None = None
    try:
        conn.request("GET", target, headers=sent)
        answer = conn.getresponse()
        status = answer.status
        body = answer.read(first)
        if len(body) == first:
            head = time.monotonic() - started
        body += answer.read()
        whole = time.monotonic() - started
        found = Response(answer.status, body, tuple(answer.getheaders()))
    except (OSError, http.client.HTTPException) as e:
        found = Response(None, error=_failure(status, e))
    finally:
        conn.close()
    outcome = _received(found)
    if head is not None:
        outcome += f", the first {first} after {head:.2f}s"
    _log_exchange("GET", url, sent, began, outcome, found)
    _scan("GET", url, sent, found)
    return Timed(found, head, whole)


def statuses_before_body(
    method: str,
    url: str,
    token: str | None = None,
    headers: Mapping[str, str] | None = None,
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
    lines = [f"{method} {_target(parts)} HTTP/1.1", f"Host: {parts.netloc}", "Connection: close"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    found: list[int] = []
    began = time.time()
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
    statuses = ", ".join(str(status) for status in found) or "no status"
    _log_exchange(method, url, _with_token(headers, token), began, f"with no body sent, {statuses}")
    return tuple(found)


def read_part(
    url: str,
    token: str | None,
    size: int,
    timeout: float = 60,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """GET url, with headers, and hang up once size bytes of the body come.

    So does a client that gives up. This is also how to ask for a range
    of an object too large to read whole, from a server that might
    ignore Range and send it all.

    Returns:
        The answer, with those bytes of its body.
    """
    conn, target = _connect(url, timeout)
    sent = _with_token(headers, token)
    status: int | None = None
    began = time.time()
    try:
        conn.request("GET", target, headers=sent)
        answer = conn.getresponse()
        status = answer.status
        found = Response(answer.status, answer.read(size), tuple(answer.getheaders()))
    except (OSError, http.client.HTTPException) as e:
        found = Response(None, error=_failure(status, e))
    finally:
        conn.close()
    _log_exchange("GET", url, sent, began, f"{_received(found)}, and no more read", found)
    _scan("GET", url, sent, found)
    return found
