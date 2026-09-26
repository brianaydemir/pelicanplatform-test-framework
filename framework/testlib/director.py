"""Asking a director where it would send a request, without following it.

A director answers a GET of /api/v1.0/director/object/<path> (for a
cache) or .../origin/<path> (for an origin) with a redirect to the one it
picked, and a Link header of the servers it would have the client try,
each `<url>; rel="duplicate"; pri=N` (director/director.go in Pelican).
"""

import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlsplit

from . import web


def parse_link(value: str) -> List[str]:
    """The rel="duplicate" URLs of a Link header, by priority (lowest
    pri first, as the client tries them)."""
    found = []
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
    status: Optional[int]
    location: str     # the host the director redirected to, or ""
    listed: List[str]  # the hosts in its Link header, by priority

    def names(self, name: str) -> bool:
        """Whether the director sent the request to host name, or would
        have the client try it."""
        return name == self.location or name in self.listed

    def describe(self) -> str:
        if self.status is None:
            return "no response"
        return f"HTTP {self.status} to {self.location or '(nowhere)'}, listing {self.listed}"


def ask(director: str, route: str, path: str, token: Optional[str] = None) -> Answer:
    """Where director would send a GET of path: route `object` (a cache)
    or `origin`."""
    answer = web.request("GET", f"{director}/api/v1.0/director/{route}{path}", token=token)
    return Answer(answer.status, host(answer.header("Location")) if answer.header("Location")
                  else "", [host(u) for u in parse_link(answer.header("Link"))])
