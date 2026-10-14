"""WebDAV listings: sending PROPFIND, and reading the multistatus answer.

A listing is a {DAV:}multistatus of responses, one per resource: its
href, and props in propstats, each with a status. Only props whose
propstat says 200 count. Servers differ in prefixes (D:, lp1:) and in
whether an href is a path or a URL, so both are read by namespace and
by path. copy_or_move() sends a COPY or a MOVE, and root_url() is where
OPTIONS of an origin's root goes.
"""

import xml.etree.ElementTree as ET  # nosec B405
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import unquote, urlsplit

from . import common, web

DAV = "{DAV:}"

# What `pelican object ls` asks for (gowebdav's default).
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8" ?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)


@dataclass(frozen=True)
class Entry:
    """A resource in a listing.

    Attributes:
        path: The href's path, decoded, without a trailing /.
        size: Its getcontentlength; None if none came.
        modified: Its getlastmodified; None if none came.
        etag: Its getetag, as sent; None if none came.
    """

    path: str
    collection: bool
    size: Optional[int]
    modified: Optional[datetime] = None
    etag: Optional[str] = None


def propfind(
    url: str, depth: str, token: Optional[str] = None, body: Optional[bytes] = None
) -> web.Response:
    """PROPFIND url with Depth depth, and body if given.

    depth is `0`, `1`, or `infinity`.
    """
    headers = {"Depth": depth}
    if body is not None:
        headers["Content-Type"] = "application/xml"
    return web.request("PROPFIND", url, token=token, headers=headers, upload=body, timeout=120)


def parse(body: bytes) -> list[Entry]:
    """Return the entries of the multistatus body.

    Raises:
        ValueError: The body is not a well-formed multistatus.
    """
    try:
        root = ET.fromstring(body)  # nosec B314
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from None
    if root.tag != f"{DAV}multistatus":
        raise ValueError(f"a <{root.tag}>, not a DAV: multistatus")
    entries: list[Entry] = []
    for response in root.findall(f"{DAV}response"):
        href = response.findtext(f"{DAV}href", "").strip()
        if not href:
            raise ValueError("a response without an href")
        collection, size, modified = False, None, None
        etag: Optional[str] = None
        for propstat in response.findall(f"{DAV}propstat"):
            status = propstat.findtext(f"{DAV}status", "").split()
            if len(status) < 2 or status[1] != "200":
                continue
            for prop in propstat.findall(f"{DAV}prop"):
                kind = prop.find(f"{DAV}resourcetype")
                if kind is not None and kind.find(f"{DAV}collection") is not None:
                    collection = True
                length = prop.findtext(f"{DAV}getcontentlength")
                if length is not None and length.strip():
                    try:
                        size = int(length)
                    except ValueError:
                        raise ValueError(f"{href}: getcontentlength '{length}'") from None
                date = prop.findtext(f"{DAV}getlastmodified")
                if date is not None and date.strip():
                    # An HTTP date (RFC 4918 says so; RFC 9110 names the form).
                    try:
                        modified = parsedate_to_datetime(date.strip())
                    except (TypeError, ValueError):
                        raise ValueError(f"{href}: getlastmodified '{date}'") from None
                tag = prop.findtext(f"{DAV}getetag")
                if tag is not None and tag.strip():
                    etag = tag.strip()
        path = unquote(urlsplit(href).path).rstrip("/")
        entries.append(Entry(path, collection, size, modified, etag))
    return entries


def relative(entries: list[Entry], base: str) -> dict[str, Entry]:
    """Return entries by their paths below base (e.g. /protected-a/data/x).

    base itself is "". A server may put its own prefix before base, as
    `tiny` does (/api/v1.0/origin/data). If the listing has base's own
    entry, the prefix is the one before it, and must be the same for
    every entry; otherwise, each entry's is what precedes base in its
    path.

    Raises:
        ValueError: An entry is not base or under it, or a path is listed
            twice.
    """
    base = "/" + base.strip("/")
    prefixes = [e.path[: -len(base)] for e in entries if e.path.endswith(base)]
    if len(prefixes) > 1:
        raise ValueError(f"{base} is listed {len(prefixes)} times")
    found: dict[str, Entry] = {}
    for entry in entries:
        prefix: Optional[str]
        if prefixes:
            prefix = prefixes[0]
        else:
            at = entry.path.find(f"{base}/")
            prefix = entry.path[:at] if at >= 0 else None
        if prefix is not None and entry.path == prefix + base:
            name = ""
        elif prefix is not None and entry.path.startswith(f"{prefix}{base}/"):
            name = entry.path[len(prefix + base) + 1 :]
        else:
            raise ValueError(f"{entry.path} is not under {base}")
        if name in found:
            raise ValueError(f"{entry.path} is listed twice")
        found[name] = entry
    return found


def problems(want: Mapping[str, Optional[int]], got: Mapping[str, Entry]) -> list[str]:
    """Return how a listing got differs from want.

    The listing's own entry ("") is ignored. Every object must have a
    getcontentlength, 0 included.

    Args:
        want: Each name's size, or None for a collection.
        got: The listing, by name, as relative() returns it.
    """
    found = {name: entry for name, entry in got.items() if name}
    wrong: list[str] = []
    missing = sorted(set(want) - set(found))
    extra = sorted(set(found) - set(want))
    if missing:
        wrong.append(f"missing {', '.join(missing[:5])}" + (" ..." if len(missing) > 5 else ""))
    if extra:
        wrong.append(f"also lists {', '.join(extra[:5])}" + (" ..." if len(extra) > 5 else ""))
    for name in sorted(set(want) & set(found)):
        size, entry = want[name], found[name]
        if size is None:
            if not entry.collection:
                wrong.append(f"{name} is not a collection")
        elif entry.collection:
            wrong.append(f"{name} is a collection")
        elif entry.size is None:
            wrong.append(f"{name} has no getcontentlength")
        elif entry.size != size:
            wrong.append(f"{name} is {entry.size} bytes, not {size}")
    return wrong


def copy_or_move(
    method: str,
    url: str,
    token: str,
    destination: Optional[str],
    overwrite: Optional[str] = None,
) -> web.Response:
    """Send COPY or MOVE (method) of url to destination.

    There is no Destination header if destination is None, and an
    Overwrite header only if overwrite is given.
    """
    headers: dict[str, str] = {}
    if destination is not None:
        headers["Destination"] = destination
    if overwrite is not None:
        headers["Overwrite"] = overwrite
    return web.request(method, url, token=token, headers=headers)


def root_url(origin: common.Origin) -> str:
    """Return the root URL of origin's server.

    Its OPTIONS names the methods it serves (registerRootOptions in
    Pelican's origin_serve/handlers.go).
    """
    parts = urlsplit(origin.url)
    return f"{parts.scheme}://{parts.netloc}/"
