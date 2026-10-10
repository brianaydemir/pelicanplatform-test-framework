"""HTTP(S) requests straight to a server, bypassing the Pelican client.

Like curl without -L: a redirect is an answer, not something to follow.
"""

import functools
import http.client
import ssl
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import urlsplit

# The federation's CA, in the dev container.
CA_FILE = "/certs/ca.crt"


@functools.lru_cache(maxsize=None)
def tls_context() -> ssl.SSLContext:
    """A TLS context that trusts the federation's CA, made once."""
    return ssl.create_default_context(cafile=CA_FILE)


@dataclass
class Response:
    """A server's answer, or the lack of one."""

    status: Optional[int]  # None if no response came
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()

    def header(self, name: str) -> str:
        """The last value of header name, or ""."""
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        return values[-1] if values else ""

    @property
    def ok(self) -> bool:
        """Whether the status is a success (2xx)."""
        return self.status is not None and 200 <= self.status < 300

    def describe(self) -> str:
        """The status, for a message."""
        return "no response" if self.status is None else f"HTTP {self.status}"


def _pieces(data: bytes, size: int = 1 << 16) -> Iterator[bytes]:
    """data, size bytes at a time."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _connect(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """A connection to url's server, and the target to ask it for."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port, timeout=timeout, context=tls_context()
        )
    else:
        conn = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
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
    """Send method to url, presenting bearer token (the token itself, not
    a file), with upload (bytes, or the path of a file) as the body. With
    chunked, the body goes in chunks with no Content-Length, as the
    Pelican client sends it. timeout is for each read or write of the
    socket, not the whole exchange."""
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
        return Response(answer.status, answer.read(), tuple(answer.getheaders()))
    except (OSError, http.client.HTTPException):
        return Response(None)
    finally:
        conn.close()


def read_part(url: str, token: Optional[str], size: int, timeout: float = 60) -> Response:
    """GET url, and hang up once size bytes of the body have come, as a
    client that gives up does: the response, with those bytes."""
    conn, target = _connect(url, timeout)
    sent = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        conn.request("GET", target, headers=sent)
        answer = conn.getresponse()
        return Response(answer.status, answer.read(size), tuple(answer.getheaders()))
    except (OSError, http.client.HTTPException):
        return Response(None)
    finally:
        conn.close()
