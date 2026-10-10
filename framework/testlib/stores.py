"""What the origins' stores hold, and putting objects straight into them.

Paths are relative to a namespace, e.g. data/0.17 for
/protected-a/data/0.17, and are as the store names them: url() encodes
them for a request. Each namespace has storage of its own, a directory
in each origin's store (common.Origin.store_of()). A pstore origin's
store is encrypted: only the running origin can read or write it, so it
is asked over HTTPS instead; and there, /public, which takes no writes,
reads /protected-a's storage.
"""

import hashlib
import os
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from typing import Optional
from urllib.parse import quote

from . import common, credentials, web, webdav


class Unreadable(Exception):
    """An origin could not say what it holds."""


# What an origin's storage cannot do, by ORIGIN_VARIANT, and why. XRootD's
# S3 plugin implements no unlink, and neither it nor the HTTPS plugin can
# give a checksum (Lfn2Pfn), as the xrootd-s3-http plugins stand. Of the
# native backends, httpsv2 and ssh compute none, since their storage is
# remote (origin_serve/backend_https.go, ssh_posixv2/origin_filesystem.go
# in Pelican), and s3v2 reports only the MD5 that the S3 service holds
# (backend_blob.go). `checksum` is for a storage that reports none, and an
# algorithm's name for one it alone lacks.
LIMITS: dict[str, dict[str, str]] = {
    "s3": {
        "delete": "XRootD's S3 plugin (libXrdS3) cannot delete",
        "checksum": "XRootD's S3 plugin (libXrdS3) computes no checksums",
    },
    "https": {"checksum": "XRootD's HTTPS plugin (libXrdHTTPServer) computes no checksums"},
    "httpsv2": {"checksum": "the httpsv2 backend computes no checksums (remote storage)"},
    "ssh": {"checksum": "the ssh backend computes no checksums (remote storage)"},
    "s3v2": {"crc32c": "the s3v2 backend reports only the MD5 that the S3 service holds"},
}

# What an origin reports of an object, as `pelican object stat --checksums`
# asks, where its storage can.
ORIGIN_DIGESTS = ("crc32c", "md5")


def url(origin: common.Origin, namespace: str, rel: str) -> str:
    """The URL of rel in namespace at origin, each character of rel that
    a URL path may not hold as is percent-encoded."""
    return f"{origin.url}/{namespace}/{quote(rel)}"


def cannot(fed: common.Federation, operation: str) -> Optional[str]:
    """Why the origins' storage cannot do operation (`delete` or
    `checksum`), if it can't."""
    return LIMITS.get(fed.origin_variant, {}).get(operation)


def digests(fed: common.Federation) -> tuple[tuple[str, ...], Optional[str]]:
    """The algorithms of ORIGIN_DIGESTS that the origins' storage reports,
    and why not the rest, if it lacks any."""
    limits = LIMITS.get(fed.origin_variant, {})
    if "checksum" in limits:
        return (), limits["checksum"]
    lacking = [limits[a] for a in ORIGIN_DIGESTS if a in limits]
    return tuple(a for a in ORIGIN_DIGESTS if a not in limits), lacking[0] if lacking else None


def held(
    origin: common.Origin, namespace: str, rel: str, token: Optional[str]
) -> Optional[str]:
    """The SHA-256 of what origin holds at rel in namespace, or None if
    nothing. token is for namespace; a read of /public ignores it."""
    if origin.pstore:
        target = url(origin, namespace, rel)
        answer = web.request("GET", target, token=token)
        if answer.status == 200:
            return hashlib.sha256(answer.body).hexdigest()
        if answer.status == 404:
            return None
        raise Unreadable(f"GET {target}: {answer.describe()}")
    return common.sha256(f"{origin.store_of(namespace)}/{rel}")


def storage_namespaces(
    origin: common.Origin, exports: Mapping[str, AbstractSet[str]]
) -> list[str]:
    """The exported namespaces (exports, by name) that have storage of
    their own at origin: each, but on a pstore origin not /public, which
    reads /protected-a's (see common.Origin.storage_namespace()). What
    origin should hold in every exported namespace goes in each of them."""
    return [
        ns
        for ns in credentials.NAMESPACES
        if ns in exports and origin.storage_namespace(ns) == ns
    ]


def held_anywhere(
    origins: Iterable[common.Origin], namespace: str, rel: str, tokens: dict[str, str]
) -> list[Optional[str]]:
    """held() at each origin, with tokens by namespace."""
    return [held(o, namespace, rel, tokens.get(namespace)) for o in unique(origins)]


def unique(origins: Iterable[common.Origin]) -> list[common.Origin]:
    """The origins, each store once: disk stores can be shared, as the
    lab server is under `ssh`, but every pstore origin has its own."""
    seen: set[str] = set()
    found: list[common.Origin] = []
    for origin in origins:
        key = origin.svc if origin.pstore else origin.store
        if key not in seen:
            seen.add(key)
            found.append(origin)
    return found


def put(
    origin: common.Origin,
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    chunked: bool = False,
) -> Optional[str]:
    """Upload data to rel in namespace straight to origin; why it failed,
    or None."""
    target = url(origin, namespace, rel)
    answer = web.request("PUT", target, token=token, upload=data, chunked=chunked)
    return None if answer.ok else f"PUT {target}: {answer.describe()}"


def put_everywhere(
    origins: Iterable[common.Origin],
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    chunked: bool = False,
) -> list[str]:
    """Upload data to rel in namespace straight to every origin, rather
    than through the director, which would pick one: under
    `topo-multi-origin`, whichever origin a later request reaches then
    holds it. Returns what went wrong."""
    return [e for e in (put(o, namespace, rel, data, token, chunked) for o in origins) if e]


def seed(
    origin: common.Origin,
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    caps: AbstractSet[str],
) -> Optional[str]:
    """Put data at rel in namespace at origin, as a test's setup: through
    the origin (put()) if the namespace takes writes, and otherwise
    straight into its storage on disk, which fed.sh ensures it has (but
    for /public on a pstore origin, which reads /protected-a's). token
    and caps are namespace's. Returns why that failed, or None."""
    if "Writes" in caps:
        return put(origin, namespace, rel, data, token)
    if origin.pstore:
        return f"/{namespace} takes no writes, and {origin.svc}'s store is encrypted"
    return write_file(origin.store_of(namespace), rel, data)


def seed_everywhere(
    origins: Iterable[common.Origin],
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    caps: AbstractSet[str],
) -> list[str]:
    """seed() at every origin (see put_everywhere()); what went wrong."""
    targets = list(origins) if "Writes" in caps else unique(origins)
    return [e for e in (seed(o, namespace, rel, data, token, caps) for o in targets) if e]


def write_file(store: str, rel: str, data: bytes) -> Optional[str]:
    """Write data to rel in store, a namespace's storage on disk (see
    common.Origin.store_of()), any directories it needs writable by any
    uid (see make_dirs()), and the file readable by any; why that failed,
    or None."""
    parts = rel.strip("/").split("/")
    try:
        for n in range(1, len(parts)):
            path = os.path.join(store, *parts[:n])
            if not os.path.isdir(path):
                os.mkdir(path)
                os.chmod(path, 0o777)  # nosec B103
        path = os.path.join(store, *parts)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o644)
    except OSError as e:
        return f"writing framework/var/{store}/{rel}: {e.strerror}"
    return None


def make_dirs(
    origins: Iterable[common.Origin], namespace: str, rel: str, token: str
) -> list[str]:
    """Make collection rel (and its parents) in namespace at every origin,
    so that it exists before a test's uploads. A disk store's are made
    directly, writable by any uid (e.g. `alice` on the lab server); a
    pstore origin is sent MKCOL for each. Returns what went wrong."""
    errors: list[str] = []
    parts = rel.strip("/").split("/")
    for origin in unique(origins):
        if not origin.pstore:
            store = origin.store_of(namespace)
            path = store
            try:
                for n in range(1, len(parts) + 1):
                    path = os.path.join(store, *parts[:n])
                    if not os.path.isdir(path):
                        os.mkdir(path)
                        os.chmod(path, 0o777)  # nosec B103
            except OSError as e:
                errors.append(f"making framework/var/{path}: {e.strerror}")
            continue
        for n in range(1, len(parts) + 1):
            target = url(origin, namespace, "/".join(parts[:n]))
            answer = web.request("MKCOL", target, token=token)
            # 405: it already exists.
            if answer.status not in (201, 405):
                errors.append(f"MKCOL {target}: {answer.describe()}")
                break
    return errors


def delete(origin: common.Origin, namespace: str, rel: str, token: str) -> Optional[str]:
    """Remove rel in namespace from origin's store, if it is there; why
    that failed, or None. A disk store's file is removed directly, and a
    pstore origin is sent DELETE."""
    if not origin.pstore:
        path = f"{origin.store_of(namespace)}/{rel}"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return f"removing framework/var/{path}: {e.strerror}"
        return None
    target = url(origin, namespace, rel)
    answer = web.request("DELETE", target, token=token)
    if answer.ok or answer.status == 404:
        return None
    return f"DELETE {target}: {answer.describe()}"


def empty_tree(origin: common.Origin, namespace: str, rel: str, token: str) -> list[str]:
    """Remove everything in collection rel in namespace from origin's
    store, but not rel itself, as common.empty_dir() does; what went
    wrong. A disk store's are removed directly. A pstore origin is listed
    a level at a time (Depth 1, which every origin answers) and sent
    DELETE for each object, then each collection, deepest first."""
    base = rel.strip("/")
    if not origin.pstore:
        path = os.path.join(origin.store_of(namespace), *base.split("/"))
        try:
            common.empty_dir(path)
        except OSError as e:
            return [f"emptying framework/var/{path}: {e.strerror}"]
        return []
    errors: list[str] = []
    doomed: list[str] = []  # parents before children
    pending = [base]
    while pending:
        here = pending.pop()
        target = url(origin, namespace, here)
        answer = webdav.propfind(target, "1", token)
        if answer.status == 404:
            continue
        try:
            if answer.status != 207:
                raise ValueError(answer.describe())
            entries = webdav.relative(webdav.parse(answer.body), f"/{namespace}/{here}")
        except ValueError as e:
            errors.append(f"PROPFIND {target}: {e}")
            continue
        for name, entry in entries.items():
            if name:
                doomed.append(f"{here}/{name}")
                if entry.collection:
                    pending.append(f"{here}/{name}")
    for doomed_rel in reversed(doomed):
        target = url(origin, namespace, doomed_rel)
        answer = web.request("DELETE", target, token=token)
        if not answer.ok and answer.status != 404:
            errors.append(f"DELETE {target}: {answer.describe()}")
    return errors


def empty_tree_everywhere(
    origins: Iterable[common.Origin],
    exports: Mapping[str, AbstractSet[str]],
    rel: str,
    tokens: Mapping[str, str],
) -> list[str]:
    """empty_tree() at every origin's store, in each exported namespace
    with storage of its own there (see storage_namespaces()), with tokens
    by namespace, which only a pstore origin needs; what went wrong."""
    errors: list[str] = []
    for origin in unique(origins):
        for namespace in storage_namespaces(origin, exports):
            errors += empty_tree(origin, namespace, rel, tokens.get(namespace, ""))
    return errors
