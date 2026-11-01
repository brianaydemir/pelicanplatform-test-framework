"""What the origins' stores hold, and putting objects straight into them.

Paths are relative to a namespace, e.g. data/0.17 for
/protected-a/data/0.17, and unencoded (url() encodes them). Each
namespace has storage of its own, a directory in each origin's store
(common.Origin.store_of()). A pstore origin's store is encrypted: only
the running origin can read or write it, so it is asked over HTTPS
instead, or its admin asks its storage API; and there, /public, which
takes no writes, reads /protected-a's storage.
"""

import os
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any
from urllib.parse import quote

from . import common, credentials, web, webdav
from .session import Session


class Unreadable(Exception):
    """An origin could not say what it holds."""


# What an origin's storage cannot do, by ORIGIN_VARIANT, and why.
# `checksum` is for a storage that reports none, and an algorithm's name
# for one it alone lacks. XRootD's S3 plugin implements no unlink, and
# neither it nor the HTTPS plugin can give a checksum (Lfn2Pfn). Of the
# native backends, httpsv2 and ssh compute none, since their storage is
# remote (origin_serve/backend_https.go, ssh_posixv2/origin_filesystem.go
# in Pelican), and s3v2 reports only the MD5 that the S3 service holds
# (backend_blob.go).
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

# An upload to an s3v2 origin with no Content-Length, as the clients
# upload, goes to the S3 service in parts of the AWS SDK's default size
# once it outgrows one (gocloud.dev's s3blob, which the backend uses), and
# the service keeps no MD5 of an object held in parts. A declared length
# sends the object whole.
S3_PART = 5 << 20

# A pstore origin's storage API, on its web port, which answers its admin
# alone (RegisterStorageAPI in Pelican's origin_serve/storage_api.go): ls,
# stat, and digest of a path in the store's catalog, and usage.
STORAGE_API = "/api/v1.0/origin/storage/pstore"


def url(origin: common.Origin, namespace: str, rel: str) -> str:
    """Return the URL of rel in namespace at origin, percent-encoded."""
    return f"{origin.url}/{namespace}/{quote(rel)}"


def cannot(fed: common.Federation, operation: str) -> str | None:
    """Return why the origins' storage cannot do operation, or None.

    Args:
        fed: The federation.
        operation: `delete` or `checksum`.
    """
    return LIMITS.get(fed.origin_variant, {}).get(operation)


def not_native(fed: common.Federation) -> str | None:
    """Return why the origins are not Pelican's native ones, or None."""
    if fed.native_origins:
        return None
    return f"XRootD serves the origins ({fed.origin_variant}), not Pelican"


def digests(fed: common.Federation) -> tuple[tuple[str, ...], str | None]:
    """Return which digests the origins' storage reports, and why not all.

    An XRootD origin's multiuser checksum path has no crc32c
    (xrootd/resources/xrootd-origin.cfg in Pelican).

    Returns:
        The algorithms of ORIGIN_DIGESTS that the storage reports; and why
        not the rest, or None if it lacks none.
    """
    limits = dict(LIMITS.get(fed.origin_variant, {}))
    if fed.origin_variant == "posix" and fed.multiuser:
        limits["crc32c"] = "XRootD's multiuser checksum path computes no crc32c"
    if "checksum" in limits:
        return (), limits["checksum"]
    lacking = [limits[a] for a in ORIGIN_DIGESTS if a in limits]
    return tuple(a for a in ORIGIN_DIGESTS if a not in limits), lacking[0] if lacking else None


def has_digests(fed: common.Federation, size: int, declared: bool) -> bool:
    """Return whether the origins report digests(fed) of an object.

    They report none of an s3v2 origin's object that arrived in parts
    (see S3_PART).

    Args:
        fed: The federation.
        size: The object's size, in bytes.
        declared: Whether it was uploaded with a Content-Length, rather
            than in chunks.
    """
    if not digests(fed)[0]:
        return False
    return fed.origin_variant != "s3v2" or declared or size <= S3_PART


def held(origin: common.Origin, namespace: str, rel: str, token: str | None) -> str | None:
    """Return the SHA-256 of what origin holds at rel in namespace.

    Args:
        origin: The origin to ask.
        namespace: The namespace, e.g. `protected-a`.
        rel: The object's path, relative to namespace.
        token: A token for namespace; a read of /public ignores it.

    Returns:
        The digest, or None if origin holds nothing there.

    Raises:
        Unreadable: A pstore origin answered neither with the object nor
            with 404.
    """
    if origin.pstore:
        target = url(origin, namespace, rel)
        answer = web.request("GET", target, token=token)
        if answer.status == 200:
            return common.digest(answer.body)
        if answer.status == 404:
            return None
        raise Unreadable(f"GET {target}: {answer.describe()}")
    return common.sha256(f"{origin.store_of(namespace)}/{rel}")


def catalog(origin: common.Origin, namespace: str, rel: str) -> str:
    """Return the path of rel in namespace in a pstore origin's catalog.

    The storage API takes it. It is under the export's StoragePrefix
    (fed_storage_prefix in fed.sh), e.g. /protected-a/data/0.17;
    /public's is /protected-a's (common.Origin.storage_namespace()).
    """
    return f"/{common.storage_dir(origin.storage_namespace(namespace))}/{rel}"


def storage_route(kind: str, path: str) -> str:
    """Return the storage API's route of kind for path in the catalog.

    Each segment of path is encoded, as the CLI sends it.

    Args:
        kind: `ls`, `stat`, or `digest`.
        path: The path in the catalog (catalog()).
    """
    return f"{kind}{quote(path)}"


def storage_admin(session: Session, origin: common.Origin, key: str | None = None) -> str:
    """Return a token that origin's storage API takes as its admin's.

    It is Session.web_token()'s.

    Args:
        session: The pass.
        origin: The pstore origin.
        key: The PEM file to sign it with, if not origin's signing key.
    """
    return session.web_token(origin.web_url, key=key)


def catalog_answer(
    session: Session,
    origin: common.Origin,
    kind: str,
    namespace: str,
    rel: str,
    token: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return what a pstore origin's storage API says of rel in namespace.

    Its admin asks.

    Args:
        session: The pass, whose scratch directory keeps the token.
        origin: The pstore origin.
        kind: `stat`; `ls`, of a collection, which lists up to 1,000
            entries; or `digest`.
        namespace: The namespace, e.g. `protected-a`.
        rel: The path, relative to namespace.
        token: The admin's token; storage_admin()'s if None.

    Returns:
        Its answer, a JSON object, and ""; or None, and why there is none.
    """
    route = storage_route(kind, catalog(origin, namespace, rel))
    answer = web.request(
        "GET",
        f"{origin.web_url}{STORAGE_API}/{route}",
        token=token or storage_admin(session, origin),
    )
    found = common.as_object(answer.json()) if answer.status == 200 else None
    if found is None:
        return None, f"the storage API's {kind} of {rel} answered {answer.describe()}"
    return found, ""


def written_at(
    session: Session, origin: common.Origin, namespace: str, rel: str
) -> tuple[float | None, str]:
    """Return when origin's store last wrote rel in namespace, in Unix time.

    A store on disk keeps it as the file's modification time; a pstore
    origin's catalog, as its entry's, which its storage API's `stat`
    gives in RFC 3339, in the origins' zone (TZ_STORAGE).

    Returns:
        The time, and ""; or None, and why the store did not say.
    """
    if not origin.pstore:
        path = f"{origin.store_of(namespace)}/{rel}"
        try:
            return os.stat(path).st_mtime, ""
        except OSError as e:
            return None, f"{common.shown(path)}: {e.strerror}"
    found, why = catalog_answer(session, origin, "stat", namespace, rel)
    if found is None:
        return None, why
    when = common.parse_rfc3339(str(found.get("modified", "")))
    if when is None:
        return None, f"the storage API's stat of {rel} gave no time: {found.get('modified')!r}"
    return when.timestamp(), ""


def storage_namespaces(origin: common.Origin, exports: Mapping[str, AbstractSet[str]]) -> list[str]:
    """Return the exported namespaces with storage of their own at origin.

    That is each of exports but, on a pstore origin, /public, which reads
    /protected-a's (common.Origin.storage_namespace()). What origin
    should hold in every exported namespace goes in each of these.
    """
    return [ns for ns in credentials.NAMESPACES if ns in exports and origin.storage_namespace(ns) == ns]


def held_anywhere(
    origins: Iterable[common.Origin], namespace: str, rel: str, tokens: dict[str, str]
) -> list[str | None]:
    """Return held() at each store of origins, with tokens by namespace.

    Raises:
        Unreadable: A pstore origin could not say what it holds.
    """
    return [held(o, namespace, rel, tokens.get(namespace)) for o in unique(origins)]


def unique(origins: Iterable[common.Origin]) -> list[common.Origin]:
    """Return the origins, each store once.

    Disk stores can be shared, as the lab server's is under `ssh`, but
    every pstore origin has its own.
    """
    seen: set[str] = set()
    found: list[common.Origin] = []
    for origin in origins:
        key = origin.svc if origin.pstore else origin.store
        if key not in seen:
            seen.add(key)
            found.append(origin)
    return found


def require_dirs(origins: Iterable[common.Origin], namespaces: Iterable[str], rel: str) -> None:
    """End the suite unless each disk store has rel in each of namespaces.

    `./fed.sh init` makes them. A pstore origin's store, which only it can
    change, is not looked at.
    """
    wanted = list(namespaces)
    for origin in unique(origins):
        if origin.pstore:
            continue
        for namespace in wanted:
            directory = f"{origin.store_of(namespace)}/{rel}"
            if not os.path.isdir(directory):
                common.die(f"{common.shown(directory)} is missing; run ./fed.sh init")


def put(
    origin: common.Origin,
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    chunked: bool = False,
) -> str | None:
    """Upload data to rel in namespace straight to origin.

    Args:
        origin: The origin.
        namespace: The namespace, e.g. `protected-a`.
        rel: Where to put it, relative to namespace.
        data: The object.
        token: A token for namespace.
        chunked: Whether to send it in chunks, with no Content-Length.

    Returns:
        Why it failed, or None if it did not.
    """
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
    """Upload data to rel in namespace straight to every origin.

    Not through the director, which would pick one: under
    `topo-multi-origin`, whichever origin a later request reaches then
    holds it.

    Returns:
        What went wrong; empty if nothing did.
    """
    return [e for e in (put(o, namespace, rel, data, token, chunked) for o in origins) if e]


def seed(
    origin: common.Origin,
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    caps: AbstractSet[str],
) -> str | None:
    """Put data at rel in namespace at origin, as a test's setup.

    It goes through the origin (put()) if the namespace takes writes, and
    otherwise straight into its storage on disk (write_file()).

    Args:
        origin: The origin to put it at.
        namespace: The namespace, e.g. `protected-a`.
        rel: Where to put it, relative to namespace.
        data: The object.
        token: A token for namespace.
        caps: Namespace's capabilities (e.g. `Writes`).

    Returns:
        Why that failed, or None if nothing did.
    """
    if "Writes" in caps:
        return put(origin, namespace, rel, data, token)
    if origin.pstore:
        return f"/{namespace} takes no writes, and {origin.svc}'s store is encrypted"
    return write_file(origin.store_of(namespace), rel, data)


def put_in_place(
    session: Session, origins: Iterable[common.Origin], namespace: str, rel: str, data: bytes
) -> list[str]:
    """Seed data at rel in namespace's storage at origins, as setup.

    Its collection is made first. On a pstore origin, /public's storage
    is /protected-a's. The session's setup token for the storage is
    presented. Once something goes wrong, nothing more is seeded.

    Returns:
        What went wrong; empty if nothing did.
    """
    target = session.fed.storage_namespace(namespace)
    token = session.setup_token(target)
    caps = session.fed.exports[target]
    errors: list[str] = []
    for origin in origins:
        errors += make_dirs([origin], target, rel.rsplit("/", 1)[0], token)
        if not errors:
            error = seed(origin, target, rel, data, token, caps)
            errors += [error] if error else []
    return errors


def seed_everywhere(
    origins: Iterable[common.Origin],
    namespace: str,
    rel: str,
    data: bytes,
    token: str,
    caps: AbstractSet[str],
) -> list[str]:
    """Run seed() at every origin (see put_everywhere()).

    Where the namespace takes no writes, which seed() then writes on
    disk, it runs at each store once.

    Returns:
        What went wrong; empty if nothing did.
    """
    targets = list(origins) if "Writes" in caps else unique(origins)
    return [e for e in (seed(o, namespace, rel, data, token, caps) for o in targets) if e]


def _make_tree(store: str, parts: Sequence[str]) -> None:
    """Make directory parts in store, and its parents, where missing.

    Each is made writable by any uid (e.g. `alice` on the lab server).

    Raises:
        OSError: One could not be made.
    """
    for n in range(1, len(parts) + 1):
        path = os.path.join(store, *parts[:n])
        if not os.path.isdir(path):
            os.mkdir(path)
            os.chmod(path, 0o777)


def write_file(store: str, rel: str, data: bytes) -> str | None:
    """Write data to rel in store, a namespace's storage on disk.

    Any directories that rel needs are made (_make_tree()), and the file
    is readable by any uid.

    Args:
        store: The storage, as common.Origin.store_of() names it.
        rel: Where to write, relative to store.
        data: The object.

    Returns:
        Why that failed, or None if nothing did.
    """
    parts = rel.strip("/").split("/")
    try:
        _make_tree(store, parts[:-1])
        path = os.path.join(store, *parts)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o644)
    except OSError as e:
        return f"writing {common.shown(store)}/{rel}: {e.strerror}"
    return None


def write_file_everywhere(
    origins: Iterable[common.Origin], namespace: str, rel: str, data: bytes
) -> list[str]:
    """Run write_file() for rel in namespace at each store of origins once.

    Returns:
        What went wrong; empty if nothing did.
    """
    errors = [write_file(origin.store_of(namespace), rel, data) for origin in unique(origins)]
    return [error for error in errors if error]


def make_dirs(origins: Iterable[common.Origin], namespace: str, rel: str, token: str) -> list[str]:
    """Make collection rel, and its parents, in namespace at every origin.

    This is so that it exists before a test's uploads. A disk store's are
    made directly (_make_tree()); a pstore origin is sent MKCOL for each.

    Returns:
        What went wrong; empty if nothing did.
    """
    errors: list[str] = []
    parts = rel.strip("/").split("/")
    for origin in unique(origins):
        if not origin.pstore:
            try:
                _make_tree(origin.store_of(namespace), parts)
            except OSError as e:
                errors.append(f"making {common.shown(e.filename)}: {e.strerror}")
            continue
        for n in range(1, len(parts) + 1):
            target = url(origin, namespace, "/".join(parts[:n]))
            answer = web.request("MKCOL", target, token=token)
            # 405: it already exists.
            if answer.status not in (201, 405):
                errors.append(f"MKCOL {target}: {answer.describe()}")
                break
    return errors


def delete(origin: common.Origin, namespace: str, rel: str, token: str) -> str | None:
    """Remove rel in namespace from origin's store, if it is there.

    A disk store's file is removed directly, and a pstore origin is sent
    DELETE.

    Returns:
        Why that failed, or None if nothing did.
    """
    if not origin.pstore:
        path = f"{origin.store_of(namespace)}/{rel}"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return f"removing {common.shown(path)}: {e.strerror}"
        return None
    target = url(origin, namespace, rel)
    answer = web.request("DELETE", target, token=token)
    if answer.ok or answer.status == 404:
        return None
    return f"DELETE {target}: {answer.describe()}"


def empty_tree(origin: common.Origin, namespace: str, rel: str, token: str) -> list[str]:
    """Remove everything in collection rel in namespace from origin's store.

    rel itself stays, as common.empty_dir() leaves it. A disk store's
    contents are removed directly. A pstore origin is listed a level at a
    time (Depth 1, which every origin answers) and sent DELETE for each
    object, then each collection, deepest first.

    Returns:
        What went wrong; empty if nothing did.
    """
    base = rel.strip("/")
    if not origin.pstore:
        path = os.path.join(origin.store_of(namespace), *base.split("/"))
        try:
            common.empty_dir(path)
        except OSError as e:
            return [f"emptying {common.shown(path)}: {e.strerror}"]
        return []
    errors: list[str] = []
    doomed: list[str] = []  # parents before children
    pending = [base]
    while pending:
        here = pending.pop()
        target = url(origin, namespace, here)
        answer, entries, why = webdav.listing(target, "1", f"/{namespace}/{here}", token)
        if answer.status == 404:
            continue
        if entries is None:
            errors.append(f"PROPFIND {target}: {why}")
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
    """Run empty_tree() at every origin's store, and return what went wrong.

    It empties rel in each exported namespace with storage of its own
    there (storage_namespaces()).

    Args:
        origins: The origins; each store is emptied once.
        exports: Each exported namespace's capabilities, by name.
        rel: The collection to empty, relative to each namespace.
        tokens: A token for each namespace, by name, which only a pstore
            origin needs.
    """
    errors: list[str] = []
    for origin in unique(origins):
        for namespace in storage_namespaces(origin, exports):
            errors += empty_tree(origin, namespace, rel, tokens.get(namespace, ""))
    return errors
