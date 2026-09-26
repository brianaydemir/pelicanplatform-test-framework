"""What the origins' stores hold, and putting objects straight into them.

Paths are relative to a namespace, e.g. data/0.17 for
/protected-a/data/0.17. The namespaces share an origin's /data, except
on a pstore origin, whose store is encrypted: only the running origin can
read or write it, so it is asked over HTTPS instead. There, each
protected namespace has storage of its own, and /public reads
/protected-a's.
"""

import hashlib
import os
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional

from . import common, credentials, web


class Unreadable(Exception):
    """An origin could not say what it holds."""


def held(origin: common.Origin, namespace: str, rel: str, token: Optional[str]) -> Optional[str]:
    """The SHA-256 of what origin holds at rel in namespace, or None if
    nothing. token is for namespace; a read of /public ignores it."""
    if origin.pstore:
        url = f"{origin.url}/{namespace}/{rel}"
        answer = web.request("GET", url, token=token)
        if answer.status == 200:
            return hashlib.sha256(answer.body).hexdigest()
        if answer.status == 404:
            return None
        raise Unreadable(f"GET {url}: {answer.describe()}")
    return common.sha256(f"{origin.store}/{rel}")


def upload_namespaces(origin: common.Origin,
                      exports: Mapping[str, AbstractSet[str]]) -> List[str]:
    """Where to upload what origin should hold in every exported namespace
    (exports, by name): the first protected namespace, and on a pstore
    origin, each, since each has storage of its own there. /public, which
    takes no writes, shares the first's."""
    protected = [ns for ns in credentials.PROTECTED if ns in exports]
    return protected if origin.pstore else protected[:1]


def held_anywhere(origins: Iterable[common.Origin], namespace: str, rel: str,
                  tokens: Dict[str, str]) -> List[Optional[str]]:
    """held() at each origin, with tokens by namespace."""
    return [held(o, namespace, rel, tokens.get(namespace)) for o in unique(origins)]


def unique(origins: Iterable[common.Origin]) -> List[common.Origin]:
    """The origins, each store once: disk stores can be shared, as the
    lab server is under `ssh`, but every pstore origin has its own."""
    seen = set()
    found = []
    for origin in origins:
        key = origin.svc if origin.pstore else origin.store
        if key not in seen:
            seen.add(key)
            found.append(origin)
    return found


def put(origin: common.Origin, namespace: str, rel: str, data: bytes, token: str,
        chunked: bool = False) -> Optional[str]:
    """Upload data to rel in namespace straight to origin; why it failed,
    or None."""
    url = f"{origin.url}/{namespace}/{rel}"
    answer = web.request("PUT", url, token=token, upload=data, chunked=chunked)
    return None if answer.ok else f"PUT {url}: {answer.describe()}"


def put_everywhere(origins: Iterable[common.Origin], namespace: str, rel: str, data: bytes,
                   token: str, chunked: bool = False) -> List[str]:
    """Upload data to rel in namespace straight to every origin, rather
    than through the director, which would pick one: under
    `topo-multi-origin`, whichever origin a later request reaches then holds it. Returns what
    went wrong."""
    return [e for e in (put(o, namespace, rel, data, token, chunked) for o in origins) if e]


def make_dirs(origins: Iterable[common.Origin], namespace: str, rel: str,
              token: str) -> List[str]:
    """Make collection rel (and its parents) in namespace at every origin,
    so that a test's uploads needn't rely on an origin making them. A disk
    store's are made directly, writable by any uid (e.g. `alice` on the
    lab server); a pstore origin is sent MKCOL for each. Returns what went
    wrong."""
    errors = []
    parts = rel.strip("/").split("/")
    for origin in unique(origins):
        if not origin.pstore:
            for n in range(1, len(parts) + 1):
                path = os.path.join(origin.store, *parts[:n])
                if not os.path.isdir(path):
                    os.mkdir(path)
                    os.chmod(path, 0o777)
            continue
        for n in range(1, len(parts) + 1):
            url = f"{origin.url}/{namespace}/{'/'.join(parts[:n])}"
            answer = web.request("MKCOL", url, token=token)
            # 405: it already exists.
            if answer.status not in (201, 405):
                errors.append(f"MKCOL {url}: {answer.describe()}")
                break
    return errors


def delete(origin: common.Origin, namespace: str, rel: str, token: str) -> Optional[str]:
    """Remove rel in namespace from origin's store, if it is there; why
    that failed, or None. A disk store's file is removed directly, and a
    pstore origin is sent DELETE."""
    if not origin.pstore:
        try:
            os.remove(f"{origin.store}/{rel}")
        except FileNotFoundError:
            pass
        except OSError as e:
            return f"removing framework/var/{origin.store}/{rel}: {e.strerror}"
        return None
    url = f"{origin.url}/{namespace}/{rel}"
    answer = web.request("DELETE", url, token=token)
    return None if answer.ok or answer.status == 404 else f"DELETE {url}: {answer.describe()}"
