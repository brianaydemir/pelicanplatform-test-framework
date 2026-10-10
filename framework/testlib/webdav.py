"""WebDAV listings: sending PROPFIND, and reading the multistatus answer.

A listing is a {DAV:}multistatus of responses, one per resource: its
href, and props in propstats, each with a status. Only props whose
propstat says 200 count. Servers differ in prefixes (D:, lp1:) and in
whether an href is a path or a URL, so both are read by namespace and
by path.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional
from urllib.parse import unquote, urlsplit

from . import web

DAV = "{DAV:}"

# What `pelican object ls` asks for (gowebdav's default).
PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8" ?>'
                 b'<d:propfind xmlns:d="DAV:"><d:prop>'
                 b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
                 b"</d:prop></d:propfind>")


@dataclass(frozen=True)
class Entry:
    path: str                # the href's path, decoded, without a trailing /
    collection: bool
    size: Optional[int]      # None if no getcontentlength came


def propfind(url: str, depth: str, token: Optional[str] = None,
             body: Optional[bytes] = None) -> web.Response:
    """PROPFIND url with Depth depth (`0`, `1`, or `infinity`), and body
    if given."""
    headers = {"Depth": depth}
    if body is not None:
        headers["Content-Type"] = "application/xml"
    return web.request("PROPFIND", url, token=token, headers=headers, upload=body, timeout=120)


def parse(body: bytes) -> List[Entry]:
    """The entries of a multistatus; ValueError if body is not one."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from None
    if root.tag != f"{DAV}multistatus":
        raise ValueError(f"a <{root.tag}>, not a DAV: multistatus")
    entries = []
    for response in root.findall(f"{DAV}response"):
        href = response.findtext(f"{DAV}href", "").strip()
        if not href:
            raise ValueError("a response without an href")
        collection, size = False, None
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
        path = unquote(urlsplit(href).path).rstrip("/")
        entries.append(Entry(path, collection, size))
    return entries


def relative(entries: List[Entry], base: str) -> Dict[str, Entry]:
    """entries by their paths below base (e.g. /protected-a/data/x), with
    base itself as "". A server may put its own prefix before base, as
    `tiny` does (/api/v1.0/origin/data); ValueError for any entry that is
    not base or under it, or that appears twice. The prefix is the one
    before base's own entry, if the listing has one, and must be the same
    for every entry."""
    base = "/" + base.strip("/")
    prefixes = [e.path[:-len(base)] for e in entries if e.path.endswith(base)]
    if len(prefixes) > 1:
        raise ValueError(f"{base} is listed {len(prefixes)} times")
    found: Dict[str, Entry] = {}
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
            name = entry.path[len(prefix + base) + 1:]
        else:
            raise ValueError(f"{entry.path} is not under {base}")
        if name in found:
            raise ValueError(f"{entry.path} is listed twice")
        found[name] = entry
    return found


def problems(want: Mapping[str, Optional[int]], got: Mapping[str, Entry]) -> List[str]:
    """How a listing got differs from want: each name's size, or None
    for a collection. The listing's own entry ("") is ignored. Every
    object must have a getcontentlength, 0 included."""
    found = {name: entry for name, entry in got.items() if name}
    wrong = []
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
