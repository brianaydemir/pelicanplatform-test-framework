"""WebDAV methods at the origins, and how each is authorized, beyond the
GET, HEAD, PUT, DELETE, and PROPFIND that the other suites send.
Pelican's native origin serves every method through
golang.org/x/net/webdav, behind its own checks of a request's token
(getActionFromMethod, which maps POST, MKCOL, LOCK, UNLOCK, PROPPATCH,
and COPY to storage.create, and MOVE to storage.modify) and of a COPY's
or MOVE's Destination (checkCopyMoveDestination), both in
origin_serve/handlers.go. The scenarios that send those methods ask
each native origin, and skip an XRootD origin; `symlink-writes` and
`stored-html` ask every origin, so that an XRootD origin is a control.

  post-create-only       POST, which x/net serves as GET, with a token
                         of storage.create or storage.read alone:
                         refused, with no object's bytes
  copy-move-destination  COPY and MOVE within /protected-a move the
                         bytes; to a Destination in another export,
                         however its `..` is spelled, or outside the
                         token's scope, refused (403), and with none
                         (400) or on another server (502), nothing done
  copy-move-scopes       storage.create may COPY to a new path, but not
                         MOVE, nor COPY over an object; with
                         `Overwrite: F`, neither replaces one (412)
  mkcol                  MKCOL makes a collection (201), but not with a
                         read token, nor where one is (405), nor with a
                         body (415)
  lock                   a LOCK with no Timeout is refused, or bounded,
                         and makes no file
  symlink-writes         writes through a symbolic link in a store that
                         leads out of it change nothing outside, and
                         those under a linked directory are refused; a
                         HEAD reports no digest of what it leads to
  stored-html            an HTML or SVG object is served with
                         `X-Content-Type-Options: nosniff`, and typed so
                         that a browser does not render it, or as an
                         attachment
  webdav-semantics       OPTIONS of the server's root names the WebDAV
                         verbs; PUT onto a collection and DELETE of the
                         export's root are refused; PROPPATCH sets no
                         property
  cache-methods          at each cache, the methods a cache serves for
                         no one (POST, MKCOL, COPY, MOVE, PROPPATCH,
                         and LOCK): a V2 cache answers 405, an XRootD
                         cache refuses, and no origin's store changes

Everything goes under /protected-a/data/webdav/<run>/<origin>/, and
what `cache-methods` sends the caches under .../caches/, which the
suite removes afterward, with what a Destination could have reached
in /protected-b and /public. Every token is the origins' own, for
/protected-a, of the scopes each scenario names.
"""

import os
import re
import shutil
import traceback
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass, field
from typing import Optional, Union

from testlib import common, credentials, digests, stores, web, webdav
from testlib.common import first, warn
from testlib.credentials import R
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session
from testlib.webdav import copy_or_move, root_url

SCENARIOS = {
    "post-create-only": (
        "POST, served as GET, with a storage.create or storage.read token: refused, no bytes"
    ),
    "copy-move-destination": (
        "COPY and MOVE to another export, outside the token's scope, or another server:"
        " refused, nothing changed"
    ),
    "copy-move-scopes": (
        "storage.create may COPY to a new path, not MOVE or COPY over an object;"
        " `Overwrite: F` replaces nothing"
    ),
    "mkcol": "MKCOL makes a collection, but not with a read token, where one is, or with a body",
    "lock": "a LOCK with no Timeout, with a storage.create token, is refused or bounded",
    "symlink-writes": (
        "PUT, DELETE, and MKCOL through a symbolic link out of a store change nothing outside"
    ),
    "stored-html": "an HTML or SVG object comes with nosniff, as a type no browser renders",
    "webdav-semantics": (
        "OPTIONS of the root names the verbs; PUT onto a collection, DELETE of the root,"
        " and PROPPATCH change nothing"
    ),
    "cache-methods": (
        "POST, MKCOL, COPY, MOVE, PROPPATCH, and LOCK at each cache: 405 from a V2 cache,"
        " refused by an XRootD one, and no origin changed"
    ),
}

NAMESPACE = "protected-a"

# What every refusal of a token, or of what it may do, must be.
REFUSED = (401, 403)

# A server that is none of the federation's, for a Destination.
FOREIGN_HOST = "elsewhere.invalid"

# The Destinations of copy-move-destination that leave /protected-a:
# each one's name, its path after the origin's data URL, with {rel} for
# a path in the namespace it reaches, and that namespace.
ESCAPES = (
    ("..", "/protected-a/../protected-b/{rel}", "protected-b"),
    ("%2e%2e", "/protected-a/%2e%2e/protected-b/{rel}", "protected-b"),
    ("%2E%2E", "/protected-a/%2E%2E/protected-b/{rel}", "protected-b"),
    (".%2e", "/protected-a/.%2e/protected-b/{rel}", "protected-b"),
    ("/protected-b", "/protected-b/{rel}", "protected-b"),
    ("/public", "/public/{rel}", "public"),
)

# A LOCK's body: an exclusive write lock (RFC 4918, section 9.10).
LOCK_INFO = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<D:lockinfo xmlns:D="DAV:">'
    b"<D:lockscope><D:exclusive/></D:lockscope>"
    b"<D:locktype><D:write/></D:locktype>"
    b"<D:owner>pelican-testing</D:owner>"
    b"</D:lockinfo>"
)

# PROPPATCH's dead properties, none of which a server here keeps.
PROPS_NS = "urn:pelican-testing"
PROPS = (f"{{{PROPS_NS}}}color", f"{{{PROPS_NS}}}shape")
PROPPATCH = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:pelican-testing">'
    b"<D:set><D:prop><Z:color>red</Z:color><Z:shape>round</Z:shape></D:prop></D:set>"
    b"</D:propertyupdate>"
)

# What the root's Allow must name. COPY is there because /protected-a
# takes writes, for which Pelican turns on Copies (registerRootOptions in
# origin_serve/handlers.go).
VERBS = ("OPTIONS", "GET", "HEAD", "PUT", "DELETE", "PROPFIND", "MKCOL", "COPY", "MOVE")

# The methods a cache serves for no one: it serves objects and listings
# (GET, HEAD, and PROPFIND), and relays a PUT or a DELETE to an origin
# (the blocks suite's `write-through`), and that is all a V2 cache
# routes, so Gin answers the rest 405 (HandleMethodNotAllowed in
# Pelican's web_ui/ui.go). Each is sent with the `server` token, which
# would let an origin honor it, so a cache that relayed one would show.
CACHE_METHODS = ("POST", "MKCOL", "COPY", "MOVE", "PROPPATCH", "LOCK")

# Objects a browser would run script in, if it rendered them.
PAGES = {
    "x.html": b"<!doctype html><html><body><script>alert(document.domain)</script></body></html>",
    "x.svg": (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b"<script>alert(document.domain)</script></svg>"
    ),
}
# Types that a browser shows as text or saves, but does not render, if
# it may not sniff.
INERT_TYPES = ("application/octet-stream", "text/plain")


class SetupError(Exception):
    """A scenario could not put in place what it checks."""


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a, or one that takes no
    writes or no direct clients."""
    caps = fed.exports.get(NAMESPACE)
    if caps is None:
        return f"this shape does not export /{NAMESPACE}"
    if "DirectReads" not in caps:
        return "the origins take no direct clients"
    if "Writes" not in caps:
        return f"/{NAMESPACE} takes no writes"
    return None


def no_landings() -> list[tuple[common.Origin, str, str]]:
    """Test.landings, to begin with."""
    return []


@dataclass
class Test:
    """What every scenario needs: the federation, its tokens for
    /protected-a, and the paths outside the run's collection that a
    scenario's requests could have written, to remove afterward."""

    session: Session
    token: str  # read, create, and modify
    create_token: str  # storage.create alone
    read_token: str  # storage.read alone
    landings: list[tuple[common.Origin, str, str]] = field(default_factory=no_landings)

    @property
    def fed(self) -> common.Federation:
        """The federation."""
        return self.session.fed

    def base(self, origin: common.Origin) -> str:
        """origin's collection of the run, in /protected-a."""
        return f"data/webdav/{self.session.run}/{origin.svc}"

    def native(self) -> Optional[str]:
        """Why the origins are not Pelican's native origins, if they are
        not."""
        if self.fed.native_origins:
            return None
        return f"XRootD serves the origins ({self.fed.origin_variant}), not Pelican"

    def seed(self, origin: common.Origin, rel: str) -> bytes:
        """Put new bytes at rel in /protected-a at origin, with the
        `server` token, and return them."""
        data = os.urandom(4096)
        error = stores.put(origin, NAMESPACE, rel, data, self.token)
        if error:
            raise SetupError(f"{origin.svc}: could not put {rel} in place ({error})")
        return data

    def storage_token(self, origin: common.Origin, namespace: str) -> Optional[str]:
        """A `server` token for the namespace whose storage namespace reads
        at origin (none for /public, which has no issuer)."""
        storage = origin.storage_namespace(namespace)
        return None if storage == "public" else self.session.token(storage)

    def stored(
        self, origin: common.Origin, namespace: str, rel: str, want: Optional[bytes], what: str
    ) -> Optional[str]:
        """What is wrong with what origin holds at rel in namespace, which
        should be want (None: nothing), calling it what."""
        try:
            have = stores.held(origin, namespace, rel, self.storage_token(origin, namespace))
        except stores.Unreadable as e:
            return f"could not check {what}: {e}"
        if want is None:
            return f"{what} is there" if have is not None else None
        if have is None:
            return f"{what} is gone"
        if have != common.digest(want):
            return f"{what} holds other bytes"
        return None

    def collection(self, origin: common.Origin, rel: str) -> Optional[bool]:
        """Whether rel in /protected-a at origin is a collection (False: an
        object), by a PROPFIND of Depth 0, or None if it is not there;
        stores.Unreadable if the origin does not say."""
        url = at(origin, rel)
        answer = webdav.propfind(url, "0", self.token)
        if answer.status == 404:
            return None
        try:
            if answer.status != 207:
                raise ValueError(answer.describe())
            entries = webdav.parse(answer.body)
            if len(entries) != 1:
                raise ValueError(f"{len(entries)} entries at Depth 0")
        except ValueError as e:
            raise stores.Unreadable(f"PROPFIND {url}: {e}") from None
        return entries[0].collection

    def made(self, origin: common.Origin, rel: str, want: bool, what: str) -> Optional[str]:
        """What is wrong with what origin holds at rel in /protected-a, which
        should be a collection if want, and otherwise nothing."""
        try:
            kind = self.collection(origin, rel)
        except stores.Unreadable as e:
            return f"could not check {what}: {e}"
        if want and kind is None:
            return f"{what}: nothing is there"
        if want and not kind:
            return f"{what}: it is an object"
        if not want and kind is not None:
            return f"{what}: {rel} is there"
        return None

    def make_dirs(self, origin: common.Origin, *rels: str) -> None:
        """Make each collection of rels in /protected-a at origin."""
        for rel in rels:
            errors = stores.make_dirs([origin], NAMESPACE, rel, self.token)
            if errors:
                raise SetupError(f"{origin.svc}: could not make {rel} ({errors[0]})")


def at(origin: common.Origin, rel: str) -> str:
    """The URL of rel in /protected-a at origin."""
    return stores.url(origin, NAMESPACE, rel)


class Findings:
    """What a scenario found wrong at each server (an origin, or for
    `cache-methods` a cache), and what it noted."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []

    def add(self, server: Union[common.Origin, common.Cache], problem: Optional[str]) -> None:
        """Record problem, if any, at server, and print it."""
        if problem:
            self.problems.append(f"{server.svc}: {problem}")
            print(f"    {server.svc}: {problem}")

    def note(self, server: Union[common.Origin, common.Cache], note: str) -> None:
        """Note what server did."""
        self.notes.append(f"{server.svc}: {note}")

    def result(self, passed: str) -> tuple[str, str]:
        """FAIL with the problems, or PASS with passed and the notes."""
        if self.problems:
            return FAIL, first(self.problems)
        return PASS, "; ".join([passed, *self.notes])


# --------------------------------------------------------------------------
# post-create-only


def post_create_only(test: Test) -> tuple[str, str]:
    """POST an object straight to each native origin, with a token of
    storage.create alone, then of storage.read alone: each must be
    refused, with none of the object's bytes. x/net's handler serves a
    POST as a GET (webdav.go), but Pelican authorizes POST as a create
    (getActionFromMethod), so a storage.create token, which may not
    read, gets the object; and a storage.read token is refused, since
    a POST counts as a write, which is as it should be."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    for origin in test.fed.origins:
        rel = f"{test.base(origin)}/post"
        data = test.seed(origin, rel)
        url = at(origin, rel)
        for name, token in (
            ("storage.create", test.create_token),
            ("storage.read", test.read_token),
        ):
            answer = web.request("POST", url, token=token)
            if answer.status not in REFUSED:
                found.add(
                    origin, f"POST with {name} alone got {answer.describe()}, not 401 or 403"
                )
            if data in answer.body:
                found.add(origin, f"POST with {name} alone got the object's bytes")
            found.add(origin, test.stored(origin, NAMESPACE, rel, data, "the object"))
    return found.result(f"{len(test.fed.origins)} origin(s) refused both")


# --------------------------------------------------------------------------
# copy-move-destination


def copy_move_destination(test: Test) -> tuple[str, str]:
    """At each native origin, COPY and MOVE an object in /protected-a,
    with the `server` token:

      within /protected-a     201 or 204, and the bytes at the
                              destination (and for MOVE, gone from the
                              source)
      each of ESCAPES         403, nothing at the destination, and the
                              source as it was
      outside a token's scope with a token for <base>/a alone, from a/
                              to b/: 403, nothing changed
      no Destination          400, nothing changed
      another server          502, nothing changed

    x/net's handler neither cleans a Destination nor authorizes it, so
    Pelican's checkCopyMoveDestination does both (origin_serve/
    handlers.go), and leaves the last two to x/net. Pelican's own tests:
    origin_serve/pstore_serving_test.go."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    fed = test.fed
    stamp = test.session.run
    for origin in fed.origins:
        base = test.base(origin)
        narrow = credentials.mint_server(
            fed,
            test.session.path(f"tokens/webdav-narrow-{origin.svc}"),
            NAMESPACE,
            scope_path=f"/{base}/a",
        )
        test.make_dirs(origin, f"{base}/a", f"{base}/b")
        for method in ("COPY", "MOVE"):
            m = method.lower()

            # Within /protected-a.
            src, dst = f"{base}/{m}-src", f"{base}/{m}-dst"
            data = test.seed(origin, src)
            what = f"{method} within /{NAMESPACE}"
            answer = copy_or_move(method, at(origin, src), test.token, at(origin, dst))
            if answer.status not in (201, 204):
                found.add(origin, f"{what} got {answer.describe()}, not 201 or 204")
            found.add(
                origin, test.stored(origin, NAMESPACE, dst, data, f"{what}: the destination")
            )
            kept = None if method == "MOVE" else data
            found.add(origin, test.stored(origin, NAMESPACE, src, kept, f"{what}: the source"))

            # Out of the export.
            for n, (name, pattern, namespace) in enumerate(ESCAPES):
                src = f"{base}/{m}-escape-{n}"
                data = test.seed(origin, src)
                rel = f"data/webdav-{stamp}-{origin.svc}-{m}-{n}"
                test.landings.append((origin, namespace, rel))
                what = f"{method} to {name}"
                destination = origin.url + pattern.format(rel=rel)
                answer = copy_or_move(method, at(origin, src), test.token, destination)
                if answer.status != 403:
                    found.add(origin, f"{what} got {answer.describe()}, not 403")
                found.add(
                    origin,
                    test.stored(origin, namespace, rel, None, f"{what}: /{namespace}/{rel}"),
                )
                found.add(
                    origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source")
                )

            # Out of the token's scope.
            src, dst = f"{base}/a/{m}-src", f"{base}/b/{m}-dst"
            data = test.seed(origin, src)
            what = f"{method} out of a token's scope"
            answer = copy_or_move(method, at(origin, src), narrow, at(origin, dst))
            if answer.status != 403:
                found.add(origin, f"{what} got {answer.describe()}, not 403")
            found.add(
                origin, test.stored(origin, NAMESPACE, dst, None, f"{what}: the destination")
            )
            found.add(origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source"))

            # No Destination.
            src = f"{base}/{m}-no-destination"
            data = test.seed(origin, src)
            what = f"{method} with no Destination"
            answer = copy_or_move(method, at(origin, src), test.token, None)
            if answer.status != 400:
                found.add(origin, f"{what} got {answer.describe()}, not 400")
            found.add(origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source"))

            # Another server.
            src, dst = f"{base}/{m}-foreign", f"{base}/{m}-foreign-dst"
            data = test.seed(origin, src)
            what = f"{method} to {FOREIGN_HOST}"
            destination = f"https://{FOREIGN_HOST}/{NAMESPACE}/{dst}"
            answer = copy_or_move(method, at(origin, src), test.token, destination)
            if answer.status != 502:
                found.add(origin, f"{what} got {answer.describe()}, not 502")
            found.add(origin, test.stored(origin, NAMESPACE, dst, None, f"{what}: {dst}"))
            found.add(origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source"))
    return found.result(
        f"{len(fed.origins)} origin(s): COPY and MOVE within /{NAMESPACE} worked,"
        + f" and {len(ESCAPES) + 3} others each were refused"
    )


# --------------------------------------------------------------------------
# copy-move-scopes


def copy_move_scopes(test: Test) -> tuple[str, str]:
    """At each native origin, in /protected-a: with a storage.create
    token, a MOVE must be refused, since it removes its source; a COPY
    to a new path must succeed (201); and a COPY over an object must be
    refused, since replacing an object takes storage.modify (the WLCG
    token profile). x/net's COPY replaces an object unless sent
    `Overwrite: F`, though Pelican authorizes every COPY as a create
    (getActionFromMethod, whose comment says COPY is purely additive).
    Then, with the `server` token, COPY and MOVE onto an object with
    `Overwrite: F` must get 412 and change nothing (RFC 4918, section
    10.6)."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    for origin in test.fed.origins:
        base = test.base(origin)

        # MOVE with storage.create.
        src, dst = f"{base}/scope-move-src", f"{base}/scope-move-dst"
        data = test.seed(origin, src)
        what = "MOVE with storage.create alone"
        answer = copy_or_move("MOVE", at(origin, src), test.create_token, at(origin, dst))
        if answer.status not in REFUSED:
            found.add(origin, f"{what} got {answer.describe()}, not 401 or 403")
        found.add(origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source"))
        found.add(origin, test.stored(origin, NAMESPACE, dst, None, f"{what}: the destination"))

        # COPY with storage.create to a new path.
        src, dst = f"{base}/scope-copy-src", f"{base}/scope-copy-new"
        data = test.seed(origin, src)
        what = "COPY with storage.create alone to a new path"
        answer = copy_or_move("COPY", at(origin, src), test.create_token, at(origin, dst))
        if answer.status != 201:
            found.add(origin, f"{what} got {answer.describe()}, not 201")
        found.add(origin, test.stored(origin, NAMESPACE, dst, data, f"{what}: the destination"))

        # COPY with storage.create over an object.
        src, dst = f"{base}/scope-over-src", f"{base}/scope-over-dst"
        data = test.seed(origin, src)
        old = test.seed(origin, dst)
        what = "COPY with storage.create alone over an object"
        answer = copy_or_move("COPY", at(origin, src), test.create_token, at(origin, dst))
        if answer.status not in REFUSED:
            found.add(origin, f"{what} got {answer.describe()}, not 401 or 403")
        found.add(origin, test.stored(origin, NAMESPACE, dst, old, f"{what}: the object"))

        # Overwrite: F.
        for method in ("COPY", "MOVE"):
            m = method.lower()
            src, dst = f"{base}/{m}-no-overwrite-src", f"{base}/{m}-no-overwrite-dst"
            data = test.seed(origin, src)
            old = test.seed(origin, dst)
            what = f"{method} onto an object with Overwrite: F"
            answer = copy_or_move(
                method, at(origin, src), test.token, at(origin, dst), overwrite="F"
            )
            if answer.status != 412:
                found.add(origin, f"{what} got {answer.describe()}, not 412")
            found.add(origin, test.stored(origin, NAMESPACE, src, data, f"{what}: the source"))
            found.add(origin, test.stored(origin, NAMESPACE, dst, old, f"{what}: the object"))
    return found.result(f"{len(test.fed.origins)} origin(s)")


# --------------------------------------------------------------------------
# mkcol


def mkcol(test: Test) -> tuple[str, str]:
    """At each native origin, MKCOL in /protected-a: with a storage.read
    token, refused, making nothing; with the `server` token, a new
    collection (201), which a PROPFIND then shows; the same again, 405,
    since it exists; and with a body, 415, making nothing (RFC 4918,
    section 9.3.1). The note says what each origin answered where a
    collection exists, which some backends answer 201 (MkdirAll, or an
    object store's marker)."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    for origin in test.fed.origins:
        base = test.base(origin)
        rel = f"{base}/mkcol-read"
        answer = web.request("MKCOL", at(origin, rel), token=test.read_token)
        if answer.status not in REFUSED:
            found.add(
                origin, f"MKCOL with storage.read alone got {answer.describe()}, not 401 or 403"
            )
        found.add(origin, test.made(origin, rel, False, "MKCOL with storage.read alone"))

        rel = f"{base}/mkcol-new"
        url = at(origin, rel)
        answer = web.request("MKCOL", url, token=test.token)
        if answer.status != 201:
            found.add(origin, f"MKCOL of a new collection got {answer.describe()}, not 201")
        found.add(origin, test.made(origin, rel, True, "MKCOL of a new collection"))
        answer = web.request("MKCOL", url, token=test.token)
        if answer.status != 405:
            found.add(origin, f"MKCOL where a collection is got {answer.describe()}, not 405")
        found.note(origin, f"MKCOL where a collection is: {answer.describe()}")
        found.add(origin, test.made(origin, rel, True, "MKCOL where a collection is"))

        rel = f"{base}/mkcol-body"
        answer = web.request(
            "MKCOL",
            at(origin, rel),
            token=test.token,
            upload=b'<?xml version="1.0" encoding="utf-8"?><x/>',
            headers={"Content-Type": "application/xml"},
        )
        if answer.status != 415:
            found.add(origin, f"MKCOL with a body got {answer.describe()}, not 415")
        found.add(origin, test.made(origin, rel, False, "MKCOL with a body"))
    return found.result(f"{len(test.fed.origins)} origin(s)")


# --------------------------------------------------------------------------
# lock


def lock_timeout(body: bytes) -> Optional[str]:
    """The timeout of the lock that a LOCK's answer describes, or None if
    it names none."""
    try:
        root = ET.fromstring(body)  # nosec B314
    except ET.ParseError:
        return None
    found = root.find(f".//{webdav.DAV}timeout")
    return None if found is None else (found.text or "").strip()


def bounded(timeout: Optional[str]) -> bool:
    """Whether a lock's timeout is a number of seconds, more than none.
    x/net writes a lock that never ends as `Second-0`."""
    match = re.fullmatch(r"Second-(\d+)", timeout or "")
    return match is not None and int(match.group(1)) > 0


def lock(test: Test) -> tuple[str, str]:
    """At each native origin, with a storage.create token and no Timeout,
    LOCK a new path and an existing object. Pelican doesn't use locks,
    but x/net keeps them in memory (webdav.NewMemLS() in origin_serve/
    handlers.go), forever when no Timeout is given, and makes an empty
    file for a LOCK of a new path, beside POSC and the metadata events.
    Each LOCK must be refused, or the lock granted must have a timeout,
    and the new path must hold nothing. Then, with the `server` token
    and no lock token, a PUT of the new path and a DELETE of the object
    (the note says what each got), and an UNLOCK of each lock granted,
    which must succeed: otherwise the path stays locked until the origin
    restarts."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    headers = {"Content-Type": "application/xml; charset=utf-8"}
    for origin in test.fed.origins:
        base = test.base(origin)
        new, old = f"{base}/lock-new", f"{base}/lock-old"
        test.seed(origin, old)
        granted: list[tuple[str, str]] = []  # each lock granted: its path, and its token
        statuses: list[str] = []
        for rel, what in ((new, "a new path"), (old, "an object")):
            url = at(origin, rel)
            answer = web.request(
                "LOCK", url, token=test.create_token, upload=LOCK_INFO, headers=headers
            )
            if answer.status is None:
                found.add(origin, f"LOCK of {what} got no response")
                continue
            if not answer.ok:
                statuses.append(f"LOCK of {what} refused ({answer.status})")
                continue
            lock_token = answer.header("Lock-Token")
            if lock_token:
                granted.append((url, lock_token))
            else:
                found.add(origin, f"LOCK of {what} got {answer.status} with no Lock-Token")
            timeout = lock_timeout(answer.body)
            statuses.append(f"LOCK of {what} granted ({answer.status}, timeout {timeout})")
            if not bounded(timeout):
                found.add(
                    origin,
                    f"LOCK of {what} with no Timeout got a lock that never ends"
                    + f" (timeout '{timeout}')",
                )
        found.add(origin, test.stored(origin, NAMESPACE, new, None, "the LOCK's new path"))
        if granted:
            put = web.request("PUT", at(origin, new), token=test.token, upload=b"x")
            delete = web.request("DELETE", at(origin, old), token=test.token)
            statuses.append(
                f"with no lock token, PUT got {put.describe()}, DELETE {delete.describe()}"
            )
        for url, lock_token in granted:
            answer = web.request(
                "UNLOCK", url, token=test.create_token, headers={"Lock-Token": lock_token}
            )
            if not answer.ok:
                found.add(
                    origin,
                    f"UNLOCK of {url} got {answer.describe()}; it stays locked until"
                    + f" {origin.svc} restarts",
                )
        found.note(origin, ", ".join(statuses))
    return found.result(f"{len(test.fed.origins)} origin(s)")


# --------------------------------------------------------------------------
# symlink-writes


@dataclass(frozen=True)
class Decoys:
    """What symlink-writes's links lead to, beside the namespaces' storage
    in a store: a file, and a directory holding one."""

    file: str  # under framework/var
    directory: str
    inner: str  # the file in directory
    file_bytes: bytes
    inner_bytes: bytes


def listing(directory: str) -> frozenset[str]:
    """The names in directory, or none if it is not there."""
    try:
        return frozenset(os.listdir(directory))
    except FileNotFoundError:
        return frozenset()


def make_decoys(test: Test, origin: common.Origin) -> tuple[Decoys, str]:
    """Put the decoys in origin's store, writable by any uid, so that only
    the server can stop a write that reaches them; and the links, in the
    run's collection in /protected-a's storage: `link-file` to the file
    and `link-dir` to the directory, by relative paths. The decoys, and
    the directory of the links."""
    stamp = test.session.run
    decoys = Decoys(
        f"{origin.store}/webdav-{stamp}-decoy",
        f"{origin.store}/webdav-{stamp}-decoy-dir",
        f"{origin.store}/webdav-{stamp}-decoy-dir/inner",
        os.urandom(4096),
        os.urandom(4096),
    )
    shutil.rmtree(decoys.directory, ignore_errors=True)
    os.mkdir(decoys.directory)
    os.chmod(decoys.directory, 0o777)  # nosec B103
    for path, data in ((decoys.file, decoys.file_bytes), (decoys.inner, decoys.inner_bytes)):
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o666)  # nosec B103
    links = f"{origin.store_of(NAMESPACE)}/{test.base(origin)}/links"
    shutil.rmtree(links, ignore_errors=True)
    os.makedirs(links)
    os.chmod(links, 0o777)  # nosec B103
    os.symlink(os.path.relpath(decoys.file, links), f"{links}/link-file")
    os.symlink(os.path.relpath(decoys.directory, links), f"{links}/link-dir")
    return decoys, links


def remove_decoys(decoys: Decoys, links: str) -> None:
    """Remove the decoys and the links."""
    for name in ("link-file", "link-dir"):
        try:
            os.remove(f"{links}/{name}")
        except FileNotFoundError:
            pass
    shutil.rmtree(links, ignore_errors=True)
    shutil.rmtree(decoys.directory, ignore_errors=True)
    try:
        os.remove(decoys.file)
    except FileNotFoundError:
        pass


def decoys_problems(decoys: Decoys, store: str, entries: frozenset[str]) -> list[str]:
    """What has become of the decoys, and of the store around them, which
    held entries before."""
    problems: list[str] = []
    for path, data in ((decoys.file, decoys.file_bytes), (decoys.inner, decoys.inner_bytes)):
        have = common.sha256(path)
        if have is None:
            problems.append(f"framework/var/{path} is gone")
        elif have != common.digest(data):
            problems.append(f"framework/var/{path} holds other bytes")
    extra = sorted(listing(decoys.directory) - {"inner"})
    if extra:
        problems.append(f"framework/var/{decoys.directory} also holds {', '.join(extra)}")
    extra = sorted(listing(store) - entries)
    if extra:
        problems.append(f"framework/var/{store} also holds {', '.join(extra)}")
    return problems


def symlink_writes(test: Test) -> tuple[str, str]:
    """At each origin whose store is on disk, through links in
    /protected-a's storage that lead out of it (make_decoys()), with the
    `server` token: a HEAD of `link-file` with `Want-Digest: crc32c`
    must report no digest of the file it leads to; a PUT and an MKCOL
    under `link-dir`, and a DELETE of the file in it, must be refused
    (4xx); and a PUT and a DELETE of `link-file` may be refused, or
    replace or remove the link itself, but not what it leads to. Each
    must leave the decoys as they were, and nothing new beside them.
    Pelican's posixv2 refuses a link that leaves its export
    (os.Root); an XRootD posix origin follows it; the rclone backends
    and the lab server skip links. Pelican's own tests:
    e2e_fed_tests/posixv2_security_test.go, origin_serve/
    checksum_symlink_test.go."""
    origins = [o for o in test.fed.origins if not o.pstore]
    if not origins:
        return SKIP, "a pstore origin's store is encrypted, and holds no links"
    found = Findings()
    for origin in origins:
        base = test.base(origin)
        entries = listing(origin.store)
        decoys, links = make_decoys(test, origin)
        try:
            entries |= {os.path.basename(decoys.file), os.path.basename(decoys.directory)}
            answer = web.request(
                "HEAD",
                at(origin, f"{base}/links/link-file"),
                token=test.token,
                headers={"Want-Digest": "crc32c"},
            )
            _, right = digests.check(answer.header("Digest"), decoys.file_bytes)
            if right:
                found.add(origin, f"HEAD of link-file reports the decoy's {', '.join(right)}")
            # Each request, and whether it may succeed on the link itself.
            requests: list[tuple[str, str, Optional[bytes], bool]] = [
                ("PUT", "link-file", os.urandom(4096), True),
                ("PUT", f"link-dir/new-{origin.svc}", os.urandom(4096), False),
                ("MKCOL", f"link-dir/sub-{origin.svc}", None, False),
                ("DELETE", "link-dir/inner", None, False),
                ("DELETE", "link-file", None, True),
            ]
            codes: list[str] = []
            for method, rel, data, own in requests:
                answer = web.request(
                    method, at(origin, f"{base}/links/{rel}"), token=test.token, upload=data
                )
                codes.append(f"{method} {rel} {answer.describe()}")
                if own and answer.ok:
                    continue
                if answer.status is None or not 400 <= answer.status < 500:
                    found.add(origin, f"{method} of {rel} got {answer.describe()}, not 4xx")
            for problem in decoys_problems(decoys, origin.store, entries):
                found.add(origin, problem)
            found.note(origin, ", ".join(codes))
        finally:
            remove_decoys(decoys, links)
    return found.result(f"{len(origins)} origin(s)")


# --------------------------------------------------------------------------
# stored-html


def stored_html(test: Test) -> tuple[str, str]:
    """PUT an HTML page and an SVG image, each with a script, to each
    origin, and GET them back: each must come with
    `X-Content-Type-Options: nosniff`, and either a type that a browser
    does not render (INERT_TYPES) or `Content-Disposition: attachment`.
    An origin serves its data on the host and port of its web UI and its
    login cookie, so an object rendered as a page could run script as
    the origin's web UI, against its admin. x/net's handler types an
    object by its name's extension (http.ServeContent), and the only
    nosniff in Pelican is the V2 cache's (local_cache/
    persistent_cache_api.go)."""
    found = Findings()
    for origin in test.fed.origins:
        for name, data in PAGES.items():
            rel = f"{test.base(origin)}/{name}"
            error = stores.put(origin, NAMESPACE, rel, data, test.token)
            if error:
                raise SetupError(f"{origin.svc}: could not put {rel} in place ({error})")
            answer = web.request("GET", at(origin, rel), token=test.token)
            kind = answer.header("Content-Type")
            disposition = answer.header("Content-Disposition")
            if answer.status != 200 or answer.body != data:
                found.add(origin, f"GET {name} got {answer.describe()}, not the object")
                continue
            if answer.header("X-Content-Type-Options").lower() != "nosniff":
                found.add(origin, f"GET {name} has no X-Content-Type-Options: nosniff")
            inert = kind.split(";")[0].strip().lower() in INERT_TYPES
            if not inert and not disposition.lower().startswith("attachment"):
                found.add(
                    origin,
                    f"GET {name} is typed '{kind}', with no Content-Disposition: attachment",
                )
            found.note(origin, f"{name} '{kind}'" + (f", {disposition}" if disposition else ""))
    return found.result(f"{len(test.fed.origins)} origin(s)")


# --------------------------------------------------------------------------
# webdav-semantics


def propstat_problems(body: bytes) -> list[str]:
    """What is wrong with a PROPPATCH's multistatus: each of PROPS must
    have a propstat, and none may succeed."""
    try:
        root = ET.fromstring(body)  # nosec B314
    except ET.ParseError as e:
        return [f"the answer is not XML ({e})"]
    if root.tag != f"{webdav.DAV}multistatus":
        return [f"the answer is a <{root.tag}>, not a DAV: multistatus"]
    seen: dict[str, str] = {}
    for propstat in root.iter(f"{webdav.DAV}propstat"):
        status = propstat.findtext(f"{webdav.DAV}status", "").split()
        code = status[1] if len(status) > 1 else "?"
        for prop in propstat.findall(f"{webdav.DAV}prop"):
            for child in prop:
                seen[child.tag] = code
    problems: list[str] = []
    for name in PROPS:
        local = name.split("}")[1]
        if name not in seen:
            problems.append(f"no propstat for {local}")
        elif seen[name].startswith("2"):
            problems.append(f"{local} was set ({seen[name]})")
    return problems


def webdav_semantics(test: Test) -> tuple[str, str]:
    """At each native origin:

      OPTIONS of the server's root, with no token: 200, with a DAV
        header, and an Allow naming VERBS (registerRootOptions in
        origin_serve/handlers.go; Pelican's
        origin_serve/root_options_test.go)
      PUT onto a collection: 405 or 409, with what it holds intact
      PROPPATCH of an object, setting dead properties that no server
        here keeps: 207, with a failing status for each; or refused
        (403, 405, or 501); the object intact
      DELETE of /protected-a's root: 403 or 405, and nothing removed

    The DELETEs come last, at every origin, after everything else, with
    the `server` token: if one is honored, it takes /protected-a's
    storage with it."""
    why = test.native()
    if why:
        return SKIP, why
    found = Findings()
    sentinels: dict[str, bytes] = {}
    for origin in test.fed.origins:
        base = test.base(origin)
        sentinels[origin.svc] = test.seed(origin, f"{base}/root-sentinel")

        answer = web.request("OPTIONS", root_url(origin))
        allowed = {v.strip().upper() for v in answer.header("Allow").split(",")}
        if answer.status != 200:
            found.add(origin, f"OPTIONS of the root got {answer.describe()}, not 200")
        else:
            if not answer.header("DAV"):
                found.add(origin, "OPTIONS of the root has no DAV header")
            missing = [v for v in VERBS if v not in allowed]
            if missing:
                found.add(origin, f"OPTIONS of the root does not allow {', '.join(missing)}")

        test.make_dirs(origin, f"{base}/coll")
        inside = f"{base}/coll/obj"
        data = test.seed(origin, inside)
        answer = web.request("PUT", at(origin, f"{base}/coll"), token=test.token, upload=b"x")
        if answer.status not in (405, 409):
            found.add(origin, f"PUT onto a collection got {answer.describe()}, not 405 or 409")
        found.add(
            origin,
            test.stored(origin, NAMESPACE, inside, data, "PUT onto a collection: its object"),
        )
        found.note(origin, f"PUT onto a collection: {answer.describe()}")

        rel = f"{base}/proppatch"
        data = test.seed(origin, rel)
        answer = web.request(
            "PROPPATCH",
            at(origin, rel),
            token=test.token,
            upload=PROPPATCH,
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        if answer.status == 207:
            for problem in propstat_problems(answer.body):
                found.add(origin, f"PROPPATCH: {problem}")
        elif answer.status not in (403, 405, 501):
            found.add(origin, f"PROPPATCH got {answer.describe()}, not 207, 403, 405, or 501")
        found.add(origin, test.stored(origin, NAMESPACE, rel, data, "PROPPATCH: the object"))
        found.note(origin, f"PROPPATCH: {answer.describe()}")

    for origin in test.fed.origins:
        rel = f"{test.base(origin)}/root-sentinel"
        answer = web.request("DELETE", f"{origin.url}/{NAMESPACE}/", token=test.token)
        if answer.status not in (403, 405):
            found.add(
                origin, f"DELETE of /{NAMESPACE}'s root got {answer.describe()}, not 403 or 405"
            )
        found.add(
            origin,
            test.stored(
                origin,
                NAMESPACE,
                rel,
                sentinels[origin.svc],
                f"DELETE of /{NAMESPACE}'s root: {rel}",
            ),
        )
        found.note(origin, f"DELETE of the root: {answer.describe()}")
    return found.result(f"{len(test.fed.origins)} origin(s)")


# --------------------------------------------------------------------------
# cache-methods


def cache_request(method: str, url: str, token: str, destination: str) -> web.Response:
    """Send method to url, with token, as a client that meant it would: a
    COPY or MOVE to destination, a LOCK and a PROPPATCH with their
    bodies, and a POST with one of its own."""
    if method in ("COPY", "MOVE"):
        return copy_or_move(method, url, token, destination)
    bodies = {
        "LOCK": (LOCK_INFO, "application/xml; charset=utf-8"),
        "PROPPATCH": (PROPPATCH, "application/xml; charset=utf-8"),
        "POST": (b"x=1", "application/x-www-form-urlencoded"),
    }
    if method in bodies:
        body, content_type = bodies[method]
        return web.request(
            method, url, token=token, upload=body, headers={"Content-Type": content_type}
        )
    return web.request(method, url, token=token)


def cache_methods(test: Test) -> tuple[str, str]:
    """At each cache, each of CACHE_METHODS, with the `server` token: of an
    object of the run's own, which every origin holds (POST, COPY, MOVE,
    PROPPATCH, and LOCK), or of a new path beside it (MKCOL), with a COPY
    or MOVE's Destination beside it too. A V2 cache must answer 405; an
    XRootD cache must refuse (4xx or 5xx), as its HTTP server sees fit,
    which the note records. Whatever the answer, no origin's store may
    change: the object must keep its bytes, and nothing may appear at
    the new path or the Destination."""
    caches = test.fed.caches
    if not caches:
        return SKIP, "this shape has no cache"
    fed = test.fed
    base = f"data/webdav/{test.session.run}/caches"
    errors = stores.make_dirs(fed.origins, NAMESPACE, base, test.token)
    if errors:
        raise SetupError(f"could not make {base} ({errors[0]})")
    found = Findings()
    for cache in caches:
        rel = f"{base}/{cache.svc}-obj"
        data = os.urandom(4096)
        errors = stores.put_everywhere(fed.origins, NAMESPACE, rel, data, test.token)
        if errors:
            raise SetupError(f"could not put {rel} at every origin ({errors[0]})")
        new = f"{base}/{cache.svc}-mkcol"
        destination = f"{base}/{cache.svc}-dest"
        answered: list[str] = []
        for method in CACHE_METHODS:
            target = f"{cache.url}/{NAMESPACE}/{new if method == 'MKCOL' else rel}"
            answer = cache_request(
                method, target, test.token, f"{cache.url}/{NAMESPACE}/{destination}"
            )
            answered.append(f"{method} {answer.status}")
            if cache.kind == "v2" and answer.status != 405:
                found.add(cache, f"{method} got {answer.describe()}, not 405")
            elif answer.status is None or not 400 <= answer.status < 600:
                found.add(cache, f"{method} got {answer.describe()}, not a refusal")
            for origin in stores.unique(fed.origins):
                what = f"after {method} at {cache.svc}"
                found.add(
                    origin, test.stored(origin, NAMESPACE, rel, data, f"{what}: the object")
                )
                found.add(
                    origin,
                    test.stored(
                        origin, NAMESPACE, destination, None, f"{what}: the Destination"
                    ),
                )
                found.add(origin, test.made(origin, new, False, f"{what}: the new path"))
        found.note(cache, ", ".join(answered))
    return found.result(f"{len(caches)} cache(s)")


RUN = {
    "post-create-only": post_create_only,
    "copy-move-destination": copy_move_destination,
    "copy-move-scopes": copy_move_scopes,
    "mkcol": mkcol,
    "lock": lock,
    "symlink-writes": symlink_writes,
    "stored-html": stored_html,
    "webdav-semantics": webdav_semantics,
    "cache-methods": cache_methods,
}


def clean(test: Test, when: str) -> None:
    """Empty data/webdav in /protected-a at every store, and remove what
    a Destination could have reached outside it."""
    fed = test.fed
    errors = stores.empty_tree_everywhere(
        fed.origins, {NAMESPACE: fed.exports[NAMESPACE]}, "data/webdav", {NAMESPACE: test.token}
    )
    for origin, namespace, rel in test.landings:
        storage = origin.storage_namespace(namespace)
        error = stores.delete(origin, storage, rel, test.storage_token(origin, namespace) or "")
        if error:
            errors.append(error)
    if errors:
        warn(f"cleaning up {when}: {first(errors)}")


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Make the run's collection at every origin, run the selected
    scenarios, and remove what they wrote."""
    fed = session.fed
    test = Test(
        session,
        session.token(NAMESPACE),
        session.token(NAMESPACE, ("write",)),
        session.token(NAMESPACE, R),
    )
    clean(test, "before the run")
    try:
        for origin in fed.origins:
            errors = stores.make_dirs([origin], NAMESPACE, test.base(origin), test.token)
            if errors:
                common.die(f"could not make the run's collection: {errors[0]}")
        for name in selected:
            print(f"  {name} ...")
            try:
                status, note = RUN[name](test)
            except SetupError as e:
                status, note = FAIL, str(e)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        clean(test, "after the run")
