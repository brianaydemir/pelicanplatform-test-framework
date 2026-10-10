"""Asking a director where it would send a request, without following it.

A director answers a GET of /api/v1.0/director/object/<path> (for a
cache) or .../origin/<path> (for an origin) with a redirect to the one it
picked, and a Link header of the servers it would have the client try,
each `<url>; rel="duplicate"; pri=N` (director/director.go in Pelican).
It also tells the client what the namespace wants of it, in the
X-Pelican-Namespace, X-Pelican-Authorization, and
X-Pelican-Token-Generation headers (server_structs/director.go), which a
server that refuses a request may send too (see hints()).
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from . import web


def parse_link(value: str) -> list[str]:
    """The rel="duplicate" URLs of a Link header, by priority (lowest
    pri first, as the client tries them)."""
    found: list[tuple[int, int, str]] = []
    for n, part in enumerate(re.split(r",(?=\s*<)", value)):
        m = re.match(r"\s*<([^>]*)>(.*)", part)
        if not m:
            continue
        params = m.group(2)
        if not re.search(r';\s*rel="?duplicate"?', params):
            continue
        pri = re.search(r";\s*pri=(\d+)", params)
        found.append((int(pri.group(1)) if pri else 1 << 30, n, m.group(1)))
    return [url for _, _, url in sorted(found)]


def host(url: str) -> str:
    """The host of a URL, without its port: how a server is named."""
    return urlsplit(url).hostname or ""


@dataclass
class Answer:
    """Where a director would send a request."""

    status: Optional[int]
    location: str  # the host the director redirected to, or ""
    listed: list[str]  # the hosts in its Link header, by priority
    broker: str  # its X-Pelican-Broker header: the origin's connection broker, or ""

    def names(self, name: str) -> bool:
        """Whether the director sent the request to host name, or would
        have the client try it."""
        return name == self.location or name in self.listed

    def describe(self) -> str:
        """The answer, for a message."""
        if self.status is None:
            return "no response"
        return f"HTTP {self.status} to {self.location or '(nowhere)'}, listing {self.listed}"


@dataclass(frozen=True)
class Hints:
    """What a server's X-Pelican-* headers tell a client about the
    namespace of what it asked for. Each is `key=value, key=value`;
    X-Pelican-Authorization is one `issuer=<url>` per issuer."""

    namespace: str  # X-Pelican-Namespace's `namespace`, e.g. /protected-a; "" if none came
    require_token: Optional[bool]  # its `require-token`; None if none came
    issuers: tuple[str, ...]  # X-Pelican-Authorization's issuers, in order
    generation_issuer: str  # X-Pelican-Token-Generation's `issuer`, or ""


def _fields(value: str) -> dict[str, str]:
    """`key=value, key=value` by key."""
    found: dict[str, str] = {}
    for part in value.split(","):
        key, sep, item = part.strip().partition("=")
        if sep:
            found[key.strip()] = item.strip()
    return found


def hints(response: web.Response) -> Hints:
    """The hints in response's headers."""
    namespace = _fields(response.header("X-Pelican-Namespace"))
    require = namespace.get("require-token")
    issuers = [
        _fields(v).get("issuer", "")
        for k, v in response.headers
        if k.lower() == "x-pelican-authorization"
    ]
    return Hints(
        namespace.get("namespace", ""),
        None if require is None else require.lower() == "true",
        tuple(i for i in issuers if i),
        _fields(response.header("X-Pelican-Token-Generation")).get("issuer", ""),
    )


def ask(
    director: str, route: str, path: str, token: Optional[str] = None, query: str = ""
) -> Answer:
    """Where director would send a GET of path: route `object` (a cache)
    or `origin`. query, if given, follows the path, e.g. `directread` for a
    client's direct read, which only an origin with DirectReads may serve;
    without it, the `origin` route serves caches too."""
    url = f"{director}/api/v1.0/director/{route}{path}" + (f"?{query}" if query else "")
    answer = web.request("GET", url, token=token)
    return Answer(
        answer.status,
        host(answer.header("Location")) if answer.header("Location") else "",
        [host(u) for u in parse_link(answer.header("Link"))],
        answer.header("X-Pelican-Broker"),
    )
