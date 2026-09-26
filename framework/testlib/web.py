"""HTTP(S) requests straight to a server, bypassing the Pelican client.

Like curl without -L: a redirect is an answer, not something to follow.
"""

import http.client
import ssl
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

# The federation's CA, in the dev container.
CA_FILE = "/certs/ca.crt"

_context: Optional[ssl.SSLContext] = None


def tls_context() -> ssl.SSLContext:
    global _context
    if _context is None:
        _context = ssl.create_default_context(cafile=CA_FILE)
    return _context


@dataclass
class Response:
    status: Optional[int]  # None if no response came
    body: bytes = b""
    headers: List[Tuple[str, str]] = field(default_factory=list)

    def header(self, name: str) -> str:
        """The last value of header name, or ""."""
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        return values[-1] if values else ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def describe(self) -> str:
        return "no response" if self.status is None else f"HTTP {self.status}"


def _pieces(data: bytes, size: int = 1 << 16) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start:start + size]


def request(method: str, url: str, token: Optional[str] = None,
            upload: Union[bytes, str, None] = None,
            headers: Optional[Mapping[str, str]] = None,
            timeout: float = 60, chunked: bool = False) -> Response:
    """Send method to url, presenting bearer token (the token itself, not
    a file), with upload (bytes, or the path of a file) as the body. With
    chunked, the body goes in chunks with no Content-Length, as the
    Pelican client sends it. timeout is for each read or write of the
    socket, not the whole exchange."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port, timeout=timeout, context=tls_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
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
        return Response(answer.status, answer.read(), answer.getheaders())
    except (OSError, http.client.HTTPException):
        return Response(None)
    finally:
        conn.close()
