"""Asking a director where it would send a request, without following it.

A director answers a GET of /api/v1.0/director/object/<path> (for a
cache) or .../origin/<path> (for an origin) with a redirect to the one it
picked, and a Link header of the servers it would have the client try,
each `<url>; rel="duplicate"; pri=N` (director/director.go in Pelican).
It also tells the client what the namespace wants of it, in the
X-Pelican-Namespace, X-Pelican-Authorization, and
X-Pelican-Token-Generation headers (server_structs/director.go), which a
server that refuses a request may send too (see hints()).

A director also lists every origin and cache for Prometheus's service
discovery (/api/v1.0/director/discoverServers): expected_targets() is
what it should list for a shape, and listing_problems() how a listing
falls short.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from . import common, web


def parse_link(value: str) -> list[str]:
    """Return the rel="duplicate" URLs of a Link header, by priority.

    The lowest pri comes first, as the client tries them.
    """
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
    """Return the host of a URL, without its port: how a server is named."""
    return urlsplit(url).hostname or ""


@dataclass
class Answer:
    """Where a director would send a request.

    Attributes:
        status: The HTTP status of its answer, or None if none came.
        location: The host the director redirected to, or "".
        listed: The hosts in its Link header, by priority.
        broker: Its X-Pelican-Broker header: the origin's connection
            broker, or "".
    """

    status: Optional[int]
    location: str
    listed: list[str]
    broker: str

    def names(self, name: str) -> bool:
        """Return whether the director picked or listed host name.

        That is, whether it sent the request there, or would have the
        client try it.
        """
        return name == self.location or name in self.listed

    def describe(self) -> str:
        """Return the answer, for a message."""
        if self.status is None:
            return "no response"
        return f"HTTP {self.status} to {self.location or '(nowhere)'}, listing {self.listed}"


@dataclass(frozen=True)
class Hints:
    """What a server's X-Pelican-* headers tell of a request's namespace.

    Each is `key=value, key=value`; X-Pelican-Authorization is one
    `issuer=<url>` per issuer.

    Attributes:
        namespace: X-Pelican-Namespace's `namespace`, e.g. /protected-a;
            "" if none came.
        require_token: X-Pelican-Namespace's `require-token`; None if
            none came.
        issuers: X-Pelican-Authorization's issuers, in order.
        generation_issuer: X-Pelican-Token-Generation's `issuer`, or "".
    """

    namespace: str
    require_token: Optional[bool]
    issuers: tuple[str, ...]
    generation_issuer: str


def _fields(value: str) -> dict[str, str]:
    """Return the values in `key=value, key=value`, by key."""
    found: dict[str, str] = {}
    for part in value.split(","):
        key, sep, item = part.strip().partition("=")
        if sep:
            found[key.strip()] = item.strip()
    return found


def hints(response: web.Response) -> Hints:
    """Return the hints in response's headers."""
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
    """Return where director would send a GET of path.

    Args:
        director: The director's URL.
        route: Either `object` (for a cache) or `origin`.
        path: The path to GET.
        token: A bearer token to present, if any.
        query: If given, follows the path, e.g. `directread` for a
            client's direct read, which only an origin with DirectReads
            may serve; without it, the `origin` route serves caches too.
    """
    url = f"{director}/api/v1.0/director/{route}{path}" + (f"?{query}" if query else "")
    answer = web.request("GET", url, token=token)
    return Answer(
        answer.status,
        host(answer.header("Location")) if answer.header("Location") else "",
        [host(u) for u in parse_link(answer.header("Link"))],
        answer.header("X-Pelican-Broker"),
    )


def naming(directors: Sequence[str], route: str, path: str, name: str) -> list[str]:
    """Return the hosts of directors that would send a GET of path to name.

    A director that would have the client try host name counts too.

    Args:
        directors: The directors' URLs.
        route: As for ask().
        path: The path to GET.
        name: The host to look for.
    """
    return [host(url) for url in directors if ask(url, route, path).names(name)]


def expected_targets(fed: common.Federation) -> dict[tuple[str, str], str]:
    """Return each server that service discovery should list.

    Returns:
        Each server's web URL, by its type and target (its web URL's host
        and port).
    """
    found: dict[tuple[str, str], str] = {}
    origins = [o.url for o in fed.origins] + [o.origin.url for o in fed.owners]
    for kind, urls in (("Origin", origins), ("Cache", [c.url for c in fed.federated_caches])):
        for url in urls:
            web_url = common.web_url_of(url)
            found[(kind, urlsplit(web_url).netloc)] = web_url
    return found


def listing_problems(fed: common.Federation, listed: Any) -> list[str]:
    """Return what is wrong with listed, a director's service discovery.

    It must list each of expected_targets() once, with its type, name,
    web URL, and data URL (on the same host) as labels, and nothing else.
    """
    want = expected_targets(fed)
    problems: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for item in (common.as_object(i) or {} for i in common.as_array(listed) or []):
        targets = [str(t) for t in common.as_array(item.get("targets")) or []]
        labels = common.as_object(item.get("labels")) or {}
        if len(targets) != 1:
            problems.append(f"an item has targets {targets}, not one")
            continue
        key = (str(labels.get("server_type", "")), targets[0])
        seen[key] = seen.get(key, 0) + 1
        if key not in want:
            problems.append(f"it lists {key[0]} {key[1]}, which is not in the shape")
            continue
        web_url = want[key]
        if labels.get("server_web_url") != web_url:
            problems.append(
                f"{key[1]}'s server_web_url is {labels.get('server_web_url')!r}, not {web_url}"
            )
        if host(str(labels.get("server_url", ""))) != host(web_url):
            problems.append(
                f"{key[1]}'s server_url {labels.get('server_url')!r} is another host's"
            )
        if not labels.get("server_name"):
            problems.append(f"{key[1]} has no server_name")
    problems += [f"it lists {k[0]} {k[1]} {n} times" for k, n in seen.items() if n > 1]
    problems += [f"it lacks {k[0]} {k[1]}" for k in want if k not in seen]
    return problems
