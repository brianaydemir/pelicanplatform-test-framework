"""What a pstore origin does that is its own, beyond serving objects.

That is its administrative storage API and CLI, its scheduled
verification passes, refusing writes once it is full, and backing up
its catalog.

  storage-api       at each pstore origin, the storage API
                    (/api/v1.0/origin/storage/pstore/...) answers its
                    admin alone; lists in pages; and agrees with the
                    data port and the metrics; at another native origin,
                    it is absent (404)
  introspect-cli    `pelican-server origin introspect` says what the
                    storage API says, with an admin token's file or one
                    it mints, and fails for a missing file
  scheduled-passes  at each pstore origin, the index check and the data
                    scan run and find nothing wrong, and an object
                    overwritten and one deleted are reclaimed
  capacity          the first origin's store, bounded just above what it
                    holds and filled: a PUT that would not fit gets 507
                    before its body, with or without Expect:
                    100-continue, and in chunks midway, leaving nothing;
                    after a DELETE, it fits
  backups           the first origin backs up its catalog at startup,
                    then once an interval, keeping only the newest
  restore           with the first origin stopped, one of those backups
                    is restored into its store by the offline CLI
                    (`pelican-server origin pstore metadata-restore`),
                    which must then export every object the backup
                    held, byte for byte, and none written after it; and
                    the origin, started again, must serve them so

A pstore is an encrypted store that only the running origin can read
(origin_serve/backend_pstore.go and pstore/ in Pelican). The suite
writes under /<ns>/data/pstore/<run>/, in the first protected namespace
that takes writes, at every pstore origin, and removes it afterward. The
requests that its checks make of the data port present tokens of the
session's profile; what it puts in place, and the requests that see
whether the federation serves again, present WLCG tokens either way, so
that `introspect-cli`, `scheduled-passes`, and `backups`, which present
nothing else but an admin's tokens, run in the WLCG pass alone.

Nothing bounds a store, or backs one up, as the shape is: a bound would
bound every suite's writes. So for `capacity`, `backups`, and `restore`
the suite restarts the first origin, once, with its one storage
directory (Origin.PStoreLocation) bounded at what it holds and a few MiB
more (Origin.PStoreStorageDirs), and with metadata backups every
BACKUP_INTERVAL to a directory of the run's own in its state
(Origin.PStoreMetadataBackup*), as the last of its configuration layers
(control.restart_with()); and afterward restores it, and waits until the
federation serves again. `restore` also stops the origin for a while,
and starts it again on the restored catalog. That needs a control
helper, and a topology other than `tiny`, whose origin is the whole
federation's process; otherwise the three skip, and say why. The
scheduled passes run as the shape is
(config.d/origin/pstore/50-origin.yaml shortens their intervals), before
the restart, which would start them over.
"""

import glob
import json
import math
import os
import re
import shutil
import subprocess  # nosec B404
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

from testlib import (
    blocks,
    common,
    control,
    credentials,
    digests,
    director,
    metrics,
    stores,
    web,
)
from testlib.common import first, warn
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "storage-api": "the pstore storage API: its admin alone; pages; agrees with the data port",
    "introspect-cli": "`pelican-server origin introspect` says what the storage API says",
    "scheduled-passes": "the index check and data scan run clean, and reclaim old versions",
    "capacity": "a full store refuses a PUT with 507 before its body, and takes it once freed",
    "backups": "catalog backups at startup and every interval, keeping only the newest",
    "restore": (
        "a backup restored offline into the store serves what it held, byte for byte, and"
        " nothing written after it"
    ),
}

# The scenarios that present no storage token beyond their setup's (see
# suites/__init__.py).
ONE_PROFILE = ("introspect-cli", "scheduled-passes", "backups")

# The protected namespaces, of which the suite writes in the first that
# takes writes.
NAMESPACES = ("protected-a", "protected-b")

# The storage API, under each origin's web URL (RegisterStorageAPI in
# Pelican's origin_serve/storage_api.go).
API = "/api/v1.0/origin/storage/pstore"

# How long the suite's own web tokens last, in seconds: the whole suite.
TOKEN_LIFETIME = 3600
# An ordinary user, whom no server makes an admin. No server need know of
# them: a token of the server's own making names its user.
USER = "test-pstore"

# A row: its status, and its note.
Row = tuple[str, str]

# How long a pstore origin may take to republish its capacity gauges:
# its janitor does so once a sweep, every 30 seconds (janitorInterval in
# pstore/gc.go).
GAUGE_WAIT = 45

# storage-api: a collection of LISTED entries, each an object but `sub`,
# a collection, listed PAGE at a time. The objects' sizes straddle what
# the store holds in its catalog (blocks.INLINE).
LISTED = tuple(f"obj-{n}" for n in range(7)) + ("sub",)
PAGE = 3
# An object held in a block file, and one held in the catalog.
FILED, INLINED = "obj-3", "obj-0"

# scheduled-passes: the passes, how long the first of each may take to
# come (an interval, 1m, after the origin starts), and how long the next,
# after the objects change, and their reclamation.
PASSES = ("index_check", "data_scan")
# The index check's findings must be none, but writes still under way,
# or abandoned less than 10 minutes ago, which only the janitor reclaims,
# and which fsck reports only to be seen (PendingInstances in
# pstore/fsck.go; appendReclaimGrace in gc.go).
PENDING = "pending_instances"
FIRST_PASS_WAIT = 150
NEXT_PASS_WAIT = 150
# The size of the object overwritten and of the one deleted: in a block
# file, not the catalog.
RECLAIM_SIZE = 100000

# The first origin's store (Origin.PStoreLocation, in
# config.d/origin/pstore/50-origin.yaml), which the restart bounds.
STORE = "/data/pstore"
# The configuration file of the restart.
CONFIG_FILE = "90-pstore.yaml"

# capacity: the bound, above what the store holds when the suite asks;
# how much room the store is filled down to; the largest object that
# fills it, which the store writes whole, at its exact length, below its
# spill threshold (spillThreshold in pstore/object_io.go); how far past
# the room left the sized PUT goes, still below the threshold; and how
# far the chunked one goes, which the origin reads up to the room left,
# so that what it leaves unread, which Go's server drains before it
# answers, is under the 256 KiB it drains (maxPostHandlerReadBytes).
HEADROOM = 8 << 20
ROOM = 512 << 10
FILL_MAX = blocks.SPILL - (64 << 10)
SIZED_OVER = 256 << 10
CHUNKED_OVER = 128 << 10
# How close to ROOM the fill must leave the room, given the sizes of
# objects that are not in the catalog.
ROOM_SLACK = blocks.stored(blocks.INLINE + 1)
# How long the server may take to answer a PUT's headers alone.
HEADERS_WAIT = 10
# How long a delete may take to give its space back: the janitor
# reclaims once a sweep.
RECLAIM_WAIT = 90

# backups: where the restart has the origin write them, in its state
# (/var/lib/pelican, framework/var/state/<service>), a directory per run;
# how often; and how many it keeps. The scheduled-pass metrics name their
# pass METADATA_BACKUP.
BACKUPS = "pstore-backups"
STATE = "/var/lib/pelican"
BACKUP_INTERVAL = 20
BACKUPS_KEPT = 3
METADATA_BACKUP = "metadata_backup"
# How long after the restart the startup backup may take to appear; how
# far past an interval the next may come, since an interval is the gap
# between the end of one and the start of the next.
STARTUP_WAIT = 60
INTERVAL_SLACK = 10
# A backup's name and its first bytes, the format's magic and version 1
# (snapshotOnce in pstore/backup.go; backupMagic in backup_crypto.go).
BACKUP_NAME = re.compile(r"pstore-metadata-(\d{8}T\d{6}Z)\.pmb")
BACKUP_MAGIC = b"PELICAN-PSTORE-BACKUP\x00\x01"

# restore: the objects it writes before the backup that it restores, of
# sizes the store keeps in its catalog (up to blocks.INLINE) and in block
# files, one past the spill threshold (blocks.SPILL); how long after them
# a backup taken after them may take to come, an interval, its slack,
# and the pass itself; and the object written after that backup, which
# the restored catalog must lack. The store's parts, which the CLI needs
# beside each other (pstore/recover.go in Pelican): the catalog, the
# block files, and the master key (local_cache/database.go, storage.go,
# and encryption.go).
RESTORE_SIZES = (100, blocks.INLINE, blocks.INLINE + 1, 100000, blocks.SPILL + 5)
RESTORE_WAIT = BACKUP_INTERVAL + INTERVAL_SLACK + 30
LATE = "late"
CATALOG_DIR = "db"
OBJECTS_DIR = "objects"
MASTER_KEY = "masterkey.json"
# How long a CLI command on the store may take, and what a successful
# restore prints (runPStoreRestore in Pelican's cmd/origin_pstore.go).
CLI_TIMEOUT = 300
RESTORED = "Restored the catalog from"

# How long, after the restore, every director may take to name every
# origin, and every cache to reach them.
SETTLE_WAIT = 150
# A small object that shows the federation serves the namespace again.
PROBE = "probe"


class Test:
    """The run's objects, the tokens, and the shape.

    Attributes:
        namespace: The namespace that the suite writes in.
        base: The run's collection, within the namespace.
        token: A `server` token for the namespace, of the session's
            profile.
        setup: A `server` token for the namespace, of the WLCG profile,
            for putting objects in place.
        pstores: The pstore origins.
        tmp: A directory of the run's own, for the CLI's files.
        objects: The storage API's collection, by rel (see
            listing_objects()).
    """

    def __init__(self, session: Session, namespace: str):
        self.session = session
        self.fed = session.fed
        self.namespace = namespace
        self.base = f"data/pstore/{session.run}"
        self.token = session.token(namespace)
        self.setup = session.setup_token(namespace)
        self.pstores = [o for o in self.fed.origins if o.pstore]
        self.tmp = session.path("pstore")
        os.makedirs(self.tmp, exist_ok=True)
        self._tokens: dict[tuple[str, str], str] = {}
        self.objects = listing_objects(self)
        # What went wrong putting the collection at each origin that has
        # it, by service.
        self._listed: dict[str, Optional[str]] = {}

    def rel(self, *parts: str) -> str:
        """Return where parts are, under the run's collection."""
        return "/".join([self.base, *parts])

    def url(self, origin: common.Origin, rel: str) -> str:
        """Return rel's URL at origin's data port."""
        return stores.url(origin, self.namespace, rel)

    def catalog(self, origin: common.Origin, rel: str) -> str:
        """Return rel's path in origin's store, which the storage API takes.

        It is under the export's StoragePrefix (fed_storage_prefix in
        fed.sh).
        """
        return f"/{common.storage_dir(origin.storage_namespace(self.namespace))}/{rel}"

    def web_token(self, origin: common.Origin, user: str) -> str:
        """Return the file of a token that origin's web API takes as user's.

        The token is minted once, by credentials.mint_user().
        """
        key = (origin.svc, user)
        if key not in self._tokens:
            out = self.session.path(f"tokens/pstore.{origin.svc}.{user}")
            credentials.mint_user(
                self.fed,
                out,
                credentials.local_issuer(self.fed, origin.web_url),
                common.signing_key(f"issuer-keys/{origin.svc}"),
                user,
                TOKEN_LIFETIME,
            )
            self._tokens[key] = out
        return self._tokens[key]

    def admin_file(self, origin: common.Origin) -> str:
        """Return the file of the admin's token for origin's web API."""
        return self.web_token(origin, "admin")

    def admin(self, origin: common.Origin) -> str:
        """Return the admin's token for origin's web API."""
        return read_token(self.admin_file(origin))

    def user(self, origin: common.Origin) -> str:
        """Return USER's token for origin's web API."""
        return read_token(self.web_token(origin, USER))

    def api(
        self, origin: common.Origin, path: str, token: Optional[str], **query: Any
    ) -> web.Response:
        """GET path of origin's storage API, with query, presenting token.

        path is e.g. `usage`, or `ls/<path>`.
        """
        url = f"{origin.web_url}{API}/{path}"
        if query:
            url += "?" + urlencode(query)
        return web.request("GET", url, token=token)

    def listed(self, origin: common.Origin) -> Optional[str]:
        """Put the storage API's collection at origin, once.

        Returns:
            What went wrong, or None.
        """
        if origin.svc not in self._listed:
            puts = (self.put(origin, rel, data) for rel, data in self.objects.items())
            self._listed[origin.svc] = next((why for why in puts if why), None)
        return self._listed[origin.svc]

    def put(self, origin: common.Origin, rel: str, data: bytes) -> Optional[str]:
        """Upload data to rel at origin, with its length.

        The store makes rel's collections.

        Returns:
            What went wrong, or None.
        """
        return stores.put(origin, self.namespace, rel, data, self.setup)


def read_token(path: str) -> str:
    """Return the token in the file at path."""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def decoded(answer: web.Response) -> Any:
    """Return answer's body as JSON, or None if it is not JSON."""
    try:
        return json.loads(answer.body)
    except ValueError:
        return None


def route(kind: str, path: str) -> str:
    """Return the storage API's route of kind for the store's path.

    kind is `ls`, `stat`, or `digest`. Each segment of path is encoded,
    as the CLI sends it.
    """
    return f"{kind}{quote(path)}"


def usage(test: Test, origin: common.Origin) -> tuple[Optional[dict[str, Any]], str]:
    """Return what origin's storage API tells the admin of its usage.

    Returns:
        The object, and ""; or None, and why.
    """
    answer = test.api(origin, "usage", test.admin(origin))
    found = common.as_object(decoded(answer)) if answer.status == 200 else None
    if found is None or not all(
        isinstance(found.get(k), int) for k in ("usedBytes", "capacityBytes")
    ):
        return None, f"usage answered {answer.describe()}, without its figures"
    return found, ""


def outcome(problems: Sequence[str], note: str) -> Row:
    """Return FAIL with the first of problems, if any, or PASS with note."""
    return (FAIL, first(problems)) if problems else (PASS, note)


def fetch_metrics(origin: common.Origin) -> metrics.Snapshot:
    """Return origin's metrics, or none if it does not answer."""
    return metrics.fetch(origin.web_url) or metrics.EMPTY


# --------------------------------------------------------------------------
# storage-api


def absent(test: Test, origin: common.Origin) -> Row:
    """Check that the storage API is absent (404) at origin.

    At a native origin whose store is not a pstore, every route of the
    storage API must be 404, with no token and with the admin's: the
    origin registers none (RegisterStorageAPI).
    """
    problems: list[str] = []
    routes = ("usage", route("ls", "/"), route("stat", "/"), route("digest", "/x"))
    for path in routes:
        for who, token in (("no token", None), ("the admin", test.admin(origin))):
            answer = test.api(origin, path, token)
            if answer.status != 404:
                problems.append(f"{path}, with {who}: {answer.describe()}, not 404")
    return outcome(problems, f"{len(routes)} routes, 404")


def listing_objects(test: Test) -> dict[str, bytes]:
    """Return the storage-api collection's objects, by rel.

    There is one per LISTED, but `sub`, which holds one.
    """
    found = {
        test.rel("api", name): os.urandom(100 + 3000 * n)
        for n, name in enumerate(LISTED)
        if name != "sub"
    }
    found[test.rel("api", "sub", "inner")] = os.urandom(10)
    return found


def authz(test: Test, origin: common.Origin, _objects: Mapping[str, bytes]) -> Row:
    """Check that each route of the storage API answers its admin alone.

    Each route, with no token, must get 401; with the namespace's
    `server` token, which is not of the server's making for its web API,
    401; as an ordinary user, 403; and as the admin, 200. AuthHandler
    says who the caller is, and AdminAuthHandler whether they are the
    admin; registering the second alone would refuse everyone with 401
    (RegisterStorageAPI in origin_serve/storage_api.go).
    """
    directory = test.catalog(origin, test.rel("api"))
    routes = (
        route("ls", directory),
        route("stat", f"{directory}/{FILED}"),
        route("digest", f"{directory}/{FILED}"),
        "usage",
    )
    tries = (
        ("no token", None, 401),
        ("the server token", test.token, 401),
        (USER, test.user(origin), 403),
        ("the admin", test.admin(origin), 200),
    )
    problems: list[str] = []
    for path in routes:
        for who, token, wanted in tries:
            answer = test.api(origin, path, token)
            if answer.status != wanted:
                problems.append(
                    f"{path.split('/', 1)[0]}, as {who}: {answer.describe()}, not {wanted}"
                )
    return outcome(problems, f"{len(routes)} routes, {len(tries)} callers")


def entries(answer: web.Response) -> tuple[Optional[list[dict[str, Any]]], bool]:
    """Return the entries of an `ls` answer, and its `more`.

    Returns:
        The entries, and whether more are to come; None and False if
        answer is not a page of a listing.
    """
    found = common.as_object(decoded(answer)) if answer.status == 200 else None
    items = common.as_array((found or {}).get("entries"))
    if found is None or items is None:
        return None, False
    rows = [common.as_object(e) for e in items]
    if any(r is None for r in rows):
        return None, False
    return [r for r in rows if r is not None], found.get("more") is True


def listing(test: Test, origin: common.Origin, objects: Mapping[str, bytes]) -> Row:
    """Check that `ls` lists the collection, whole and in pages.

    `ls` of the collection must list every entry, in name order, each
    with its path, size, and kind, and no more to come. Then PAGE at a
    time: each page at most PAGE, `more` on every page but the last, and
    `after` the last name continuing it, the pages together the whole
    listing, with no entry twice and none left out. `ls` of an object
    must get 400, and of nothing, 404.
    """
    admin = test.admin(origin)
    directory = test.catalog(origin, test.rel("api"))
    problems: list[str] = []
    whole, more = entries(test.api(origin, route("ls", directory), admin))
    if whole is None:
        return FAIL, "ls of the collection did not answer 200 with a listing"
    names = [str(e.get("name")) for e in whole]
    if names != sorted(LISTED):
        problems.append(f"ls lists {names}, not {sorted(LISTED)}")
    if more:
        problems.append("ls of the whole collection says more is to come")
    for entry in whole:
        name = str(entry.get("name"))
        if entry.get("path") != f"{directory}/{name}":
            problems.append(f"{name}'s path is {entry.get('path')!r}")
        if bool(entry.get("isDir")) != (name == "sub"):
            problems.append(f"{name}'s isDir is {entry.get('isDir')!r}")
        data = objects.get(test.rel("api", name))
        if data is not None and entry.get("size") != len(data):
            problems.append(f"{name}'s size is {entry.get('size')!r}, not {len(data)}")
    paged: list[str] = []
    after = ""
    pages = 0
    while pages <= len(LISTED):
        query: dict[str, Any] = {"limit": PAGE}
        if after:
            query["after"] = after
        page, more = entries(test.api(origin, route("ls", directory), admin, **query))
        pages += 1
        if page is None:
            problems.append(f"page {pages} did not answer 200 with a listing")
            break
        if len(page) > PAGE:
            problems.append(f"page {pages} has {len(page)} entries, more than {PAGE}")
        paged += [str(e.get("name")) for e in page]
        if not more or not page:
            break
        after = paged[-1]
    if paged != names:
        problems.append(f"the pages list {paged}, not {names}")
    wanted_pages = math.ceil(len(LISTED) / PAGE)
    if pages != wanted_pages:
        problems.append(f"{pages} pages of {PAGE}, not {wanted_pages}")
    for path, wanted in ((f"{directory}/{FILED}", 400), (f"{directory}/nothing", 404)):
        answer = test.api(origin, route("ls", path), admin)
        if answer.status != wanted:
            problems.append(
                f"ls of {path.rsplit('/', 1)[-1]}: {answer.describe()}, not {wanted}"
            )
    return outcome(problems, f"{len(names)} entries, in {pages} pages of {PAGE}")


def stat(test: Test, origin: common.Origin, objects: Mapping[str, bytes]) -> Row:
    """Check that `stat` says what a HEAD at the data port says.

    `stat` of an object in a block file, and of one in the catalog, must
    say its name, path, and size, and an ETag, each as a HEAD at the
    data port says them; of the collection `sub`, that it is one, with
    no ETag; and of nothing, 404.
    """
    admin = test.admin(origin)
    problems: list[str] = []
    for name in (FILED, INLINED):
        rel = test.rel("api", name)
        path = test.catalog(origin, rel)
        found = common.as_object(decoded(test.api(origin, route("stat", path), admin)))
        if found is None:
            problems.append(f"stat of {name} did not answer 200 with an object")
            continue
        head = web.request("HEAD", test.url(origin, rel), token=test.token)
        if head.status != 200:
            problems.append(f"HEAD of {name}: {head.describe()}")
            continue
        wanted = {
            "name": name,
            "path": path,
            "isDir": False,
            "size": len(objects[rel]),
            "etag": head.header("ETag"),
        }
        if head.header("Content-Length") != str(len(objects[rel])):
            problems.append(f"HEAD of {name}: Content-Length {head.header('Content-Length')}")
        if not wanted["etag"]:
            problems.append(f"HEAD of {name}: no ETag")
        for key, value in wanted.items():
            if found.get(key) != value:
                problems.append(f"stat of {name}: {key} {found.get(key)!r}, not {value!r}")
    path = test.catalog(origin, test.rel("api", "sub"))
    found = common.as_object(decoded(test.api(origin, route("stat", path), admin))) or {}
    if found.get("isDir") is not True or found.get("etag"):
        problems.append(
            f"stat of sub: isDir {found.get('isDir')!r}, etag {found.get('etag')!r}"
        )
    answer = test.api(origin, route("stat", test.catalog(origin, test.rel("nothing"))), admin)
    if answer.status != 404:
        problems.append(f"stat of nothing: {answer.describe()}, not 404")
    return outcome(problems, f"{FILED}, {INLINED}, and sub, as HEAD says")


def digest(test: Test, origin: common.Origin, objects: Mapping[str, bytes]) -> Row:
    """Check that `digest` gives the checksums the store recorded.

    `digest` of an object must give the checksums the store recorded as
    it wrote it (md5, sha, and crc32c; ingestAlgorithms in
    pstore/checksum.go), each the object's, and each what a HEAD with
    Want-Digest of it alone gets at the data port; of a collection, 400.
    """
    admin = test.admin(origin)
    rel = test.rel("api", FILED)
    data = objects[rel]
    problems: list[str] = []
    found = common.as_object(
        decoded(test.api(origin, route("digest", test.catalog(origin, rel)), admin))
    )
    header = str((found or {}).get("digest", ""))
    wrong, right = digests.check(header, data)
    problems += [f"digest: {w}" for w in wrong]
    for algorithm in ("md5", "sha", "crc32c"):
        if algorithm not in right:
            problems.append(f"digest '{header}' gives no {algorithm}")
    for algorithm, value in digests.parse(header).items():
        head = web.request(
            "HEAD", test.url(origin, rel), token=test.token, headers={"Want-Digest": algorithm}
        )
        got = digests.parse(head.header("Digest")).get(algorithm)
        if got != value:
            problems.append(f"Want-Digest {algorithm} at the data port: {got!r}, not {value!r}")
    path = test.catalog(origin, test.rel("api", "sub"))
    answer = test.api(origin, route("digest", path), admin)
    if answer.status != 400:
        problems.append(f"digest of a collection: {answer.describe()}, not 400")
    return outcome(problems, f"'{header}', as Want-Digest says")


def gauges_agree(
    test: Test, origin: common.Origin, seconds: float
) -> tuple[Optional[str], str]:
    """Wait until origin's capacity gauges say what its usage says.

    The gauges are pelican_pstore_used_bytes and
    pelican_pstore_limit_bytes, and the usage is the storage API's; they
    may take a janitor's sweep to agree.

    Returns:
        Why they don't within seconds, or None; and the figures.
    """
    deadline = time.time() + seconds
    while True:
        found, why = usage(test, origin)
        if found is None:
            return why, ""
        snapshot = fetch_metrics(origin)
        got = (
            snapshot.value("pelican_pstore_used_bytes"),
            snapshot.value("pelican_pstore_limit_bytes"),
        )
        wanted = (found["usedBytes"], found["capacityBytes"])
        if got == wanted and snapshot.has("pelican_pstore_used_bytes"):
            return None, f"{wanted[0]} of {wanted[1] or 'unbounded'}"
        if time.time() >= deadline:
            return (
                f"the gauges say {got[0]:.0f} used of {got[1]:.0f}, the API"
                + f" {wanted[0]} of {wanted[1]}, {seconds}s on"
            ), ""
        time.sleep(3)


def usage_row(test: Test, origin: common.Origin, objects: Mapping[str, bytes]) -> Row:
    """Check that `usage` is right, and that the capacity gauges agree.

    `usage` must name the backend, count at least what the collection
    holds, and say the store is unbounded, as the shape has it; and the
    capacity gauges must come to say the same (gauges_agree()).
    """
    found, why = usage(test, origin)
    if found is None:
        return FAIL, why
    problems: list[str] = []
    if found.get("backend") != "pstore":
        problems.append(f"usage's backend is {found.get('backend')!r}")
    held = sum(len(d) for d in objects.values())
    if found["usedBytes"] < held:
        problems.append(f"usage says {found['usedBytes']} bytes used, less than the {held} put")
    if found["capacityBytes"] != 0:
        problems.append(f"usage says a capacity of {found['capacityBytes']}, not 0 (unbounded)")
    stale, figures = gauges_agree(test, origin, GAUGE_WAIT)
    if stale:
        problems.append(stale)
    return outcome(problems, f"{figures} bytes, as the gauges say")


API_CASES: tuple[tuple[str, Callable[[Test, common.Origin, Mapping[str, bytes]], Row]], ...] = (
    ("authz", authz),
    ("ls", listing),
    ("stat", stat),
    ("digest", digest),
    ("usage", usage_row),
)


def storage_api(test: Test) -> dict[str, Row]:
    """Check the storage API at each origin.

    At a pstore origin, each of API_CASES runs over a collection put
    there; at another native one, absent().
    """
    rows: dict[str, Row] = {}
    for origin in test.fed.origins:
        if not origin.pstore:
            rows[f"{origin.svc} absent"] = absent(test, origin)
            continue
        why = test.listed(origin)
        for case, check in API_CASES:
            if why and case != "authz":
                rows[f"{origin.svc} {case}"] = (FAIL, f"putting the collection: {why}")
            else:
                rows[f"{origin.svc} {case}"] = check(test, origin, test.objects)
    return rows


# --------------------------------------------------------------------------
# introspect-cli


@dataclass
class Cli:
    """`pelican-server origin introspect` at one origin.

    Attributes:
        server: The pelican-server to run.
    """

    test: Test
    server: str
    origin: common.Origin

    def run(
        self, *args: str, token_file: Optional[str], env: Optional[Mapping[str, str]] = None
    ) -> tuple[int, str, str]:
        """Run `origin introspect <args> --json` at the origin.

        It gets --token token_file, if given, and runs with env added to
        the session's environment.

        Returns:
            Its exit status, stdout, and stderr.
        """
        command = [self.server, "origin", "introspect", *args, "--server", self.origin.web_url]
        if token_file is not None:
            command += ["--token", token_file]
        command.append("--json")
        try:
            done = subprocess.run(  # nosec B603
                command,
                env={**self.test.session.env, **(env or {})},
                cwd=self.test.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, "", "timed out after 120s"
        return done.returncode, done.stdout, done.stderr

    def compare(
        self, args: Sequence[str], api_route: str, env: Optional[Mapping[str, str]] = None
    ) -> Optional[str]:
        """Return why introspect disagrees with the storage API, or None.

        `introspect <args>`, with the admin's token file, or with none
        and env, must print what api_route of the storage API answers
        the admin.
        """
        token_file = None if env is not None else self.test.admin_file(self.origin)
        status, out, err = self.run(*args, token_file=token_file, env=env)
        if status != 0:
            return f"`introspect {' '.join(args)}`: exit {status}: {last_line(err)}"
        try:
            printed = json.loads(out)
        except ValueError:
            return f"`introspect {' '.join(args)}` printed no JSON: {last_line(out)}"
        answer = self.test.api(self.origin, api_route, self.test.admin(self.origin))
        wanted = decoded(answer)
        if answer.status != 200 or printed != wanted:
            return f"`introspect {' '.join(args)}` printed {printed}, the API {wanted}"
        return None


def minting_env(test: Test, origin: common.Origin) -> dict[str, str]:
    """Return the environment in which the CLI mints the admin's token.

    Given no token file, the CLI mints it as origin would, with a copy
    of origin's signing key as its issuer keys, and origin's web URL as
    its own; and under `tiny`, where the origin shares its process with
    a director, which changes its local issuer, the modules that say so
    (originAndDirectorColocated in Pelican's cmd/cmd_utils.go). A copy,
    since Pelican makes a key in an issuer key directory that has none.
    """
    keys = os.path.join(test.tmp, f"keys-{origin.svc}")
    os.makedirs(keys, exist_ok=True)
    key = os.path.join(keys, "issuer.pem")
    shutil.copyfile(common.signing_key(f"issuer-keys/{origin.svc}"), key)
    os.chmod(key, 0o600)
    env = {
        "PELICAN_ISSUERKEYSDIRECTORY": os.path.abspath(keys),
        "PELICAN_SERVER_EXTERNALWEBURL": origin.web_url,
    }
    if test.fed.tiny:
        env["PELICAN_SERVER_MODULES"] = "origin,director"
    return env


def introspect_cli(test: Test) -> dict[str, Row]:
    """Check that `introspect` says what the storage API says.

    At the first pstore origin, with the admin's token file (`token`):
    `introspect usage`, `ls`, `stat`, and `digest` of the storage-api
    collection must each print what the storage API answers. With a
    token file that is missing (`missing`), it must fail. With none, but
    the origin's key (`minted`), it must mint a token the origin takes,
    and print the usage.
    """
    server = test.session.pelican_server
    if server is None:
        skipped = "no pelican-server, which has `origin introspect`; run ./fed.sh init"
        return {case: (SKIP, skipped) for case in ("token", "missing", "minted")}
    origin = test.pstores[0]
    cli = Cli(test, server, origin)
    directory = test.catalog(origin, test.rel("api"))
    filed = f"{directory}/{FILED}"
    rows: dict[str, Row] = {}
    why = test.listed(origin)
    if why:
        rows["token"] = (FAIL, f"putting the collection: {why}")
    else:
        problems = [
            p
            for p in (
                cli.compare(["usage"], "usage"),
                cli.compare(["ls", directory], f"{route('ls', directory)}?limit=1000"),
                cli.compare(["stat", filed], route("stat", filed)),
                cli.compare(["digest", filed], route("digest", filed)),
            )
            if p
        ]
        rows["token"] = outcome(problems, "usage, ls, stat, and digest, as the API says")
    missing = os.path.join(test.tmp, "no-such-token")
    status, out, err = cli.run("usage", token_file=missing)
    if status == 0:
        rows["missing"] = (FAIL, f"with a missing token file, exit 0: {last_line(out)}")
    elif "not found" not in err.lower():
        rows["missing"] = (FAIL, f"exit {status}, not for the missing file: {last_line(err)}")
    else:
        rows["missing"] = (PASS, f"exit {status}: {last_line(err)}")
    failed = cli.compare(["usage"], "usage", env=minting_env(test, origin))
    rows["minted"] = (FAIL, failed) if failed else (PASS, "a minted admin token")
    return rows


# --------------------------------------------------------------------------
# scheduled-passes


def pass_counts(snapshot: metrics.Snapshot) -> dict[str, float]:
    """Return how many times each of PASSES has begun."""
    return {
        p: snapshot.value("pelican_pstore_scheduled_passes_total", **{"pass": p})
        for p in PASSES
    }


def last_success(snapshot: metrics.Snapshot) -> dict[str, float]:
    """Return when each of PASSES last succeeded, in Unix time.

    It is 0 for a pass that never has.
    """
    return {
        p: snapshot.value(
            "pelican_pstore_scheduled_pass_last_success_timestamp_seconds", **{"pass": p}
        )
        for p in PASSES
    }


def wait_metrics(
    origin: common.Origin, done: Callable[[metrics.Snapshot], bool], seconds: float
) -> tuple[bool, metrics.Snapshot]:
    """Fetch origin's metrics every 3 seconds until done(), or seconds pass.

    done() says whether they show what it waits for.

    Returns:
        Whether they did, and the last.
    """
    deadline = time.time() + seconds
    while True:
        snapshot = fetch_metrics(origin)
        if done(snapshot) or time.time() >= deadline:
            return done(snapshot), snapshot
        time.sleep(3)


def passes_at(test: Test, origin: common.Origin) -> Row:
    """Check that origin's passes run clean, and reclaim old versions.

    Once the index check and the data scan have each succeeded, an
    object is put and overwritten, and another put and deleted. The
    janitor must then reclaim the two old versions
    (pelican_pstore_reclaimed_objects_total by 2, and
    reclaimed_bytes_total by their sizes), and each pass run again, and
    succeed. None may have failed
    (pelican_pstore_scheduled_pass_failures_total); the index check must
    find nothing (every pelican_pstore_fsck_findings 0); and the data
    scan must have verified objects, and found none changed
    (pelican_pstore_data_scan_mismatches_total 0).
    """
    ok, snapshot = wait_metrics(
        origin,
        lambda s: all(v > 0 for v in last_success(s).values()),
        FIRST_PASS_WAIT,
    )
    if not ok:
        return FAIL, f"no pass of each of {', '.join(PASSES)} succeeded in {FIRST_PASS_WAIT}s"
    old, new = os.urandom(RECLAIM_SIZE), os.urandom(RECLAIM_SIZE)
    for rel, data in (
        (test.rel("passes", "overwritten"), old),
        (test.rel("passes", "gone"), old),
    ):
        why = test.put(origin, rel, data)
        if why:
            return FAIL, why
    before = fetch_metrics(origin)
    why = test.put(origin, test.rel("passes", "overwritten"), new)
    why = why or stores.delete(origin, test.namespace, test.rel("passes", "gone"), test.setup)
    if why:
        return FAIL, why
    changed, begun = time.time(), pass_counts(fetch_metrics(origin))

    def reclaimed(s: metrics.Snapshot) -> tuple[float, float]:
        return (
            metrics.delta(before, s, "pelican_pstore_reclaimed_objects_total"),
            metrics.delta(before, s, "pelican_pstore_reclaimed_bytes_total"),
        )

    def settled(s: metrics.Snapshot) -> bool:
        objects, freed = reclaimed(s)
        counts, last = pass_counts(s), last_success(s)
        return (
            objects >= 2
            and freed >= 2 * RECLAIM_SIZE
            and all(counts[p] > begun[p] and last[p] >= int(changed) for p in PASSES)
        )

    ok, snapshot = wait_metrics(origin, settled, NEXT_PASS_WAIT)
    problems: list[str] = []
    objects, freed = reclaimed(snapshot)
    if objects < 2 or freed < 2 * RECLAIM_SIZE:
        pending = snapshot.series("pelican_pstore_reclamation_pending")
        problems.append(
            f"{NEXT_PASS_WAIT}s on, {objects:.0f} versions ({freed:.0f} bytes) reclaimed, not"
            + f" the 2 replaced ({2 * RECLAIM_SIZE}); pending: {dict(pending) or 'none'}"
        )
    elif not ok:
        problems.append(
            f"no pass of each of {', '.join(PASSES)} succeeded in {NEXT_PASS_WAIT}s"
        )
    for p in PASSES:
        failed = snapshot.value("pelican_pstore_scheduled_pass_failures_total", **{"pass": p})
        if failed:
            problems.append(f"{failed:.0f} {p} passes failed")
    findings = {
        dict(labels).get("kind", ""): value
        for labels, value in snapshot.series("pelican_pstore_fsck_findings").items()
    }
    if not findings:
        problems.append("no pelican_pstore_fsck_findings after an index check")
    for kind, value in findings.items():
        if value and kind != PENDING:
            problems.append(f"the index check found {value:.0f} {kind}")
    mismatches = snapshot.value("pelican_pstore_data_scan_mismatches_total")
    if mismatches:
        problems.append(f"the data scan found {mismatches:.0f} objects changed")
    verified = snapshot.value("pelican_pstore_data_scan_objects_total")
    if not verified:
        problems.append("the data scans verified no object")
    aside = f"; {findings[PENDING]:.0f} {PENDING}" if findings.get(PENDING) else ""
    return outcome(
        problems,
        f"{objects:.0f} versions reclaimed; {verified:.0f} objects scanned clean{aside}",
    )


def scheduled_passes(test: Test) -> dict[str, Row]:
    """Run passes_at() at each pstore origin."""
    return {origin.svc: passes_at(test, origin) for origin in test.pstores}


# --------------------------------------------------------------------------
# The restart, for capacity and backups.


def config_text(bound: Optional[int], backups: Optional[str]) -> str:
    """Return the restart's configuration.

    It bounds the store's one directory at bound bytes, if given, and
    sends backups to the directory backups, if given.
    """
    lines = ["Origin:"]
    if bound is not None:
        lines += ["  PStoreStorageDirs:", f"    - Path: {STORE}", f"      MaxSize: {bound}"]
    if backups is not None:
        lines += [
            f"  PStoreMetadataBackupLocation: {backups}",
            f"  PStoreMetadataBackupInterval: {BACKUP_INTERVAL}s",
            f"  PStoreMetadataBackupsToKeep: {BACKUPS_KEPT}",
        ]
    return "\n".join(lines) + "\n"


def serving(test: Test) -> Optional[str]:
    """Return why the namespace is not served as before, or None.

    Every origin must answer a HEAD of the probe, every director name
    every origin for it, and every federated cache answer a HEAD of it,
    with the setup token.
    """
    rel = test.rel(PROBE)
    for origin in test.fed.origins:
        answer = web.request("HEAD", test.url(origin, rel), token=test.setup)
        if answer.status != 200:
            return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
    path = f"/{test.namespace}/{rel}"
    for url in test.fed.directors:
        where = director.ask(url, "origin", path, token=test.setup)
        for origin in test.fed.origins:
            if not where.names(director.host(origin.url)):
                return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
    for cache in test.fed.federated_caches:
        answer = web.request(
            "HEAD", f"{cache.url}/{test.namespace}/{quote(rel)}", token=test.setup
        )
        if answer.status != 200:
            return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
    return None


def settle(test: Test) -> Optional[str]:
    """Wait until the federation serves the namespace as before.

    Returns:
        Why it doesn't within SETTLE_WAIT seconds (see serving()), or
        None.
    """
    deadline = time.time() + SETTLE_WAIT
    why = serving(test)
    while why and time.time() < deadline:
        time.sleep(5)
        why = serving(test)
    return f"after {SETTLE_WAIT}s, {why}" if why else None


# --------------------------------------------------------------------------
# capacity


def fits(room: int) -> int:
    """Return the largest object size that takes room bytes or fewer.

    The object is one that the store writes whole at its exact length,
    beyond the catalog. It is 0 if none does.
    """
    size = room - blocks.TAG * -(-room // (blocks.BLOCK + blocks.TAG))
    while size > blocks.INLINE and blocks.stored(size) > room:
        size -= 1
    return size if size > blocks.INLINE else 0


@dataclass
class Capacity:
    """capacity's state, at the first origin, bounded at `bound` bytes.

    Attributes:
        rows: The cases' rows so far, by case.
        fillers: The rels of the fillers put.
    """

    test: Test
    origin: common.Origin
    bound: int
    rows: dict[str, Row] = field(default_factory=dict[str, Row])
    fillers: list[str] = field(default_factory=list[str])

    def room(self) -> int:
        """Return the bytes left under the bound, as the storage API says.

        Raises:
            ValueError: The storage API can't say.
        """
        found, why = usage(self.test, self.origin)
        if found is None:
            raise ValueError(why)
        return int(found["capacityBytes"]) - int(found["usedBytes"])

    def refusals(self) -> float:
        """Return pelican_pstore_write_failures_total{reason="no_space"}."""
        return fetch_metrics(self.origin).value(
            "pelican_pstore_write_failures_total", reason="no_space"
        )

    def fill(self) -> Row:
        """Fill the store until ROOM bytes or a little more are left.

        The fillers are objects written whole. Each must be taken, and
        use what it should take (blocks.stored()), less what the janitor
        reclaims meanwhile.
        """
        found, why = usage(self.test, self.origin)
        if found is None:
            return FAIL, why
        if found["capacityBytes"] != self.bound:
            return FAIL, f"usage says a capacity of {found['capacityBytes']}, not {self.bound}"
        start = int(found["usedBytes"])
        before = fetch_metrics(self.origin)
        content = stored = 0
        room = self.bound - start
        while room > ROOM + ROOM_SLACK and len(self.fillers) < 64:
            size = min(FILL_MAX, fits(room - ROOM))
            if not size:
                break
            rel = self.test.rel("capacity", f"filler-{len(self.fillers)}")
            answer = web.request(
                "PUT", self.test.url(self.origin, rel), self.test.setup, os.urandom(size)
            )
            if not answer.ok:
                return FAIL, f"a filler of {size} bytes, with {room} left: {answer.describe()}"
            self.fillers.append(rel)
            content += size
            stored += blocks.stored(size)
            room = self.room()
        if not ROOM <= room <= ROOM + ROOM_SLACK:
            return (
                FAIL,
                f"{room} bytes left after {len(self.fillers)} fillers, not about {ROOM}",
            )
        used = self.bound - room
        freed = metrics.delta(
            before, fetch_metrics(self.origin), "pelican_pstore_reclaimed_bytes_total"
        )
        # Each filler takes its bytes and a tag per block (blocks.stored());
        # allow a block more apiece.
        most = stored + blocks.BLOCK * len(self.fillers)
        if not start + content - freed <= used <= start + most:
            return FAIL, (
                f"{used - start} bytes more used after fillers of {content} bytes, which"
                + f" should take {stored} ({freed:.0f} reclaimed meanwhile)"
            )
        return PASS, f"{len(self.fillers)} fillers, {room} bytes left of {self.bound}"

    def refused(self, case: str, send: Callable[[int], tuple[Optional[str], str]]) -> Row:
        """Check that send()'s PUT, of more than the room left, is refused.

        It must also count as one no_space write failure.

        Args:
            case: What the PUT is, for the note.
            send: Given the room left, make a PUT of more, and say why it
                was not refused, or None, and a note.
        """
        before = self.refusals()
        room = self.room()
        why, note = send(room)
        risen = self.refusals() - before
        if why:
            return FAIL, why
        if risen != 1:
            return FAIL, f"no_space write failures rose by {risen:.0f}, not 1"
        return PASS, f"{case}: {note}, with {room} bytes left"

    def sized(self, expect: bool) -> Callable[[int], tuple[Optional[str], str]]:
        """Return a send() for refused() that PUTs headers alone.

        The PUT sends its headers, with a Content-Length past the room
        left, but not its body, with `Expect: 100-continue` if expect: it
        must get 507, and nothing else first, before the body comes
        (checkBackendCapacity in origin_serve/handlers.go; HasCapacityFor
        in pstore/capacity.go).
        """

        def send(room: int) -> tuple[Optional[str], str]:
            headers = {"Content-Length": str(room + SIZED_OVER)}
            if expect:
                headers["Expect"] = "100-continue"
            url = self.test.url(self.origin, self.test.rel("capacity", "sized"))
            got = web.statuses_before_body(
                "PUT", url, self.test.token, headers, timeout=HEADERS_WAIT
            )
            if got != (507,):
                shown = ", then ".join(str(s) for s in got) or "nothing"
                return f"a PUT's headers alone got {shown}, not just 507", ""
            return None, f"507 for {room + SIZED_OVER} bytes, before the body"

        return send

    def chunked(self, room: int) -> tuple[Optional[str], str]:
        """PUT more than the room left in chunks, as a send() for refused().

        It must get 507, which the origin must not turn into the WebDAV
        handler's 405, or a 500 (statusForBackendError in
        origin_serve/error_handling.go); with no object left, at the data
        port or in the storage API, and no space held.
        """
        rel = self.test.rel("capacity", "chunked")
        found, why = usage(self.test, self.origin)
        if found is None:
            return why, ""
        answer = web.request(
            "PUT",
            self.test.url(self.origin, rel),
            self.test.token,
            os.urandom(room + CHUNKED_OVER),
            chunked=True,
        )
        if answer.status != 507:
            return (
                f"a chunked PUT of {room + CHUNKED_OVER} bytes: {answer.describe()}, not 507",
                "",
            )
        head = web.request("HEAD", self.test.url(self.origin, rel), token=self.test.token)
        if head.status != 404:
            return f"after the refused chunked PUT, a HEAD of it got {head.describe()}", ""
        path = route("stat", self.test.catalog(self.origin, rel))
        stat_answer = self.test.api(self.origin, path, self.test.admin(self.origin))
        if stat_answer.status != 404:
            return f"after the refused chunked PUT, stat of it got {stat_answer.describe()}", ""
        after, why = usage(self.test, self.origin)
        if after is None:
            return why, ""
        if after["usedBytes"] > found["usedBytes"]:
            return (
                f"the refused chunked PUT left {after['usedBytes'] - found['usedBytes']} bytes"
                + " used"
            ), ""
        return None, f"507 for {room + CHUNKED_OVER} bytes in chunks, nothing left"

    def freed(self) -> Row:
        """Check that a DELETE frees room for the sized PUT refused before.

        After a DELETE of a filler, the janitor must give its space back,
        within RECLAIM_WAIT seconds, so that the sized PUT, refused
        before, now fits, and reads back.
        """
        size = self.room() + SIZED_OVER
        if not self.fillers:
            return FAIL, "no filler to delete"
        why = stores.delete(self.origin, self.test.namespace, self.fillers[0], self.test.setup)
        if why:
            return FAIL, why
        need = blocks.stored(size)
        if not common.wait_for(lambda: self.room() >= need, RECLAIM_WAIT, interval=3):
            return FAIL, f"{RECLAIM_WAIT}s after a DELETE, {self.room()} bytes left, not {need}"
        before = self.refusals()
        data = os.urandom(size)
        rel = self.test.rel("capacity", "sized")
        answer = web.request("PUT", self.test.url(self.origin, rel), self.test.token, data)
        if not answer.ok:
            return FAIL, f"the sized PUT, once freed: {answer.describe()}"
        back = web.request("GET", self.test.url(self.origin, rel), token=self.test.token)
        if back.status != 200 or back.body != data:
            return (
                FAIL,
                f"the sized PUT, once freed, reads back {back.describe()}, not the object",
            )
        if self.refusals() != before:
            return FAIL, "a PUT that fit counted as a no_space write failure"
        return PASS, f"{size} bytes, once a DELETE freed room"

    def gauges(self) -> Row:
        """Check that the capacity gauges agree with the storage API.

        pelican_pstore_limit_bytes must be the bound, and with
        pelican_pstore_used_bytes, come to say what the storage API's
        usage says (gauges_agree()).
        """
        found, why = usage(self.test, self.origin)
        if found is None:
            return FAIL, why
        stale, figures = gauges_agree(self.test, self.origin, GAUGE_WAIT)
        if stale:
            return FAIL, stale
        if found["capacityBytes"] != self.bound:
            return FAIL, f"usage says a capacity of {found['capacityBytes']}, not {self.bound}"
        return PASS, f"{figures} bytes"

    def run(self) -> dict[str, Row]:
        """Run each case, in order, and return the rows.

        Once one fails, those that need it do too.
        """
        steps: tuple[tuple[str, Callable[[], Row]], ...] = (
            ("fill", self.fill),
            ("sized", lambda: self.refused("Content-Length", self.sized(False))),
            ("expect", lambda: self.refused("Expect: 100-continue", self.sized(True))),
            ("chunked", lambda: self.refused("chunked", self.chunked)),
            ("freed", self.freed),
            ("gauges", self.gauges),
        )
        for case, step in steps:
            filled = self.rows.get("fill", (PASS, ""))[0] == PASS
            if case not in ("fill", "gauges") and not filled:
                self.rows[case] = (FAIL, "the store was not filled")
                continue
            self.rows[case] = guarded(step)
        return self.rows


def guarded(step: Callable[[], Row]) -> Row:
    """Return step(), or a FAIL for the exception it raised.

    Such an exception is a bug, or a server answering what the suite
    can't read.
    """
    try:
        return step()
    except Exception as e:  # pylint: disable=broad-exception-caught
        traceback.print_exc()
        return FAIL, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# backups


def backup_time(name: str) -> Optional[float]:
    """Return when a backup named name was written, in Unix time.

    Returns:
        The time, to the second; None if name is not a backup's.
    """
    found = BACKUP_NAME.fullmatch(name)
    if found is None:
        return None
    when = datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ")
    return when.replace(tzinfo=timezone.utc).timestamp()


@dataclass
class Backups:
    """backups' state, at the first origin.

    Its backups go to a directory in its state (see inside and outside).

    Attributes:
        asked: When its restart was asked for, in Unix time.
        first: The startup backup's name, once started() has found it.
    """

    test: Test
    origin: common.Origin
    asked: float
    first: str = ""

    @property
    def inside(self) -> str:
        """The directory, as the origin sees it."""
        return f"{STATE}/{BACKUPS}/{self.test.session.run}"

    @property
    def outside(self) -> str:
        """The directory, under framework/var."""
        return f"state/{self.origin.svc}/{BACKUPS}/{self.test.session.run}"

    def names(self) -> list[str]:
        """Return what is in the directory, sorted.

        It is nothing if the directory is not there.
        """
        try:
            return sorted(os.listdir(self.outside))
        except FileNotFoundError:
            return []

    def snapshots(self) -> list[str]:
        """Return the backups in the directory, oldest first."""
        return [n for n in self.names() if backup_time(n) is not None]

    def started(self) -> Row:
        """Check that a backup comes at startup (the `startup` case).

        Right after the restart, a backup must appear within
        STARTUP_WAIT seconds, written before an interval had passed
        since the origin started (process_start_time_seconds), as the
        first of its backup passes.
        """
        if not common.wait_for(lambda: bool(self.snapshots()), STARTUP_WAIT, interval=1):
            return (
                FAIL,
                f"no backup in framework/var/{self.outside} {STARTUP_WAIT}s after the restart",
            )
        self.first = self.snapshots()[0]
        written = backup_time(self.first) or 0.0
        snapshot = fetch_metrics(self.origin)
        began = snapshot.value("process_start_time_seconds")
        passes = snapshot.value(
            "pelican_pstore_scheduled_passes_total", **{"pass": METADATA_BACKUP}
        )
        problems: list[str] = []
        if written < int(self.asked) - 1:
            problems.append(f"{self.first} was written before the restart")
        if began and written - began >= BACKUP_INTERVAL:
            problems.append(
                f"{self.first} was written {written - began:.0f}s after the origin started,"
                + f" not within its interval of {BACKUP_INTERVAL}s"
            )
        if passes < 1:
            problems.append("no metadata_backup pass counted for it")
        how = (
            f"{written - began:.0f}s after the origin started" if began else "after the restart"
        )
        return outcome(problems, f"{self.first}, {how}")

    def finished(self) -> dict[str, Row]:
        """Check that backups keep to schedule, and only the newest stay.

        Once BACKUPS_KEPT + 1 backup passes have begun: `schedule`, the
        backups must be an interval apart, give or take INTERVAL_SLACK,
        and none of their passes failed; `files`, the directory must hold
        BACKUPS_KEPT backups, the startup one pruned, each named for when
        it was written and beginning with the format's magic, no
        `.partial` file and nothing else; and
        pelican_pstore_metadata_backup_last_size_bytes must be the newest
        one's size.
        """
        if not self.first:
            why = "no startup backup to go on from"
            return {"schedule": (FAIL, why), "files": (FAIL, why)}
        wait = (BACKUPS_KEPT + 1) * (BACKUP_INTERVAL + INTERVAL_SLACK) + 30
        ok, snapshot = wait_metrics(
            self.origin,
            lambda s: s.value(
                "pelican_pstore_scheduled_passes_total", **{"pass": METADATA_BACKUP}
            )
            >= BACKUPS_KEPT + 1,
            wait,
        )
        if not ok:
            why = f"fewer than {BACKUPS_KEPT + 1} backup passes in {wait}s"
            return {"schedule": (FAIL, why), "files": (FAIL, why)}
        kept = self.snapshots()
        problems: list[str] = []
        times = [backup_time(n) or 0.0 for n in kept]
        for earlier, later, name in zip(times, times[1:], kept[1:]):
            gap = later - earlier
            if not BACKUP_INTERVAL - 1 <= gap <= BACKUP_INTERVAL + INTERVAL_SLACK:
                problems.append(
                    f"{name} came {gap:.0f}s after the one before, not {BACKUP_INTERVAL}s"
                )
        failed = snapshot.value(
            "pelican_pstore_scheduled_pass_failures_total", **{"pass": METADATA_BACKUP}
        )
        if failed:
            problems.append(f"{failed:.0f} backup passes failed")
        return {
            "schedule": outcome(problems, f"{len(kept)} backups, {BACKUP_INTERVAL}s apart"),
            "files": guarded(self.files),
        }

    def files(self) -> Row:
        """Run the `files` case (see finished())."""
        problems: list[str] = []
        names = self.names()
        if any(n.endswith(".partial") for n in names):
            # One may be under way; a moment later it is not.
            time.sleep(2)
            names = self.names()
        kept = [n for n in names if backup_time(n) is not None]
        problems += [f"{n} is no backup" for n in names if backup_time(n) is None]
        if len(kept) != BACKUPS_KEPT:
            problems.append(f"{len(kept)} backups kept, not {BACKUPS_KEPT}")
        if self.first in kept:
            problems.append(f"the startup backup, {self.first}, was not pruned")
        for name in kept:
            with open(os.path.join(self.outside, name), "rb") as f:
                if f.read(len(BACKUP_MAGIC)) != BACKUP_MAGIC:
                    problems.append(f"{name} does not begin with the backup format's magic")
        newest: list[str] = []
        size = 0.0
        for _ in range(3):
            newest = self.snapshots()[-1:]
            size = fetch_metrics(self.origin).value(
                "pelican_pstore_metadata_backup_last_size_bytes"
            )
            if newest == self.snapshots()[-1:]:
                break
        if not newest:
            problems.append("no backup to compare the size gauge with")
        else:
            on_disk = os.path.getsize(os.path.join(self.outside, newest[0]))
            if size != on_disk:
                problems.append(
                    f"the size gauge says {size:.0f}, {newest[0]} is {on_disk} bytes"
                )
        return outcome(problems, f"{', '.join(kept)}")

    def remove(self) -> None:
        """Remove the directory."""
        try:
            shutil.rmtree(self.outside)
        except FileNotFoundError:
            pass
        except OSError as e:
            warn(f"removing framework/var/{self.outside}: {e.strerror}")


# --------------------------------------------------------------------------
# restore


def keys_env(test: Test, origin: common.Origin) -> dict[str, str]:
    """Return the environment in which the pstore CLI opens origin's store.

    It has a copy of every key in origin's issuer key directory as its
    own, since the catalog's master key and each backup are sealed to
    them (openPStoreForCLI in Pelican's cmd/origin_pstore.go). A copy,
    so that Pelican never writes a key of its own among the origin's.
    """
    keys = os.path.join(test.tmp, f"restore-keys-{origin.svc}")
    os.makedirs(keys, exist_ok=True)
    for path in sorted(glob.glob(f"issuer-keys/{origin.svc}/*.pem")):
        copy = os.path.join(keys, os.path.basename(path))
        shutil.copyfile(path, copy)
        os.chmod(copy, 0o600)
    return {"PELICAN_ISSUERKEYSDIRECTORY": os.path.abspath(keys)}


def give_back(path: str, uid: int, gid: int) -> Optional[str]:
    """Make path, and everything under it, belong to uid and gid.

    What the origin wrote belongs to them, so an origin that dropped
    privileges can then still open what the tests wrote as root.

    Returns:
        Why that failed, or None.
    """
    try:
        for root, dirs, files in os.walk(path):
            for name in [*dirs, *files]:
                os.chown(os.path.join(root, name), uid, gid)
        os.chown(path, uid, gid)
    except OSError as e:
        return f"giving framework/var/{path} back to {uid}:{gid}: {e.strerror}"
    return None


def exported_files(directory: str) -> dict[str, bytes]:
    """Return every file under directory, by name, with its bytes."""
    found: dict[str, bytes] = {}
    for root, _, files in os.walk(directory):
        for name in files:
            with open(os.path.join(root, name), "rb") as f:
                found[name] = f.read()
    return found


@dataclass
class Restore:
    """restore's state at the first origin.

    Attributes:
        server: The pelican-server to run.
        data: The objects written before the backup, by object name.
        copied: The backup restored, copied under test.tmp.
        aside: Where the catalog that the restore replaces is set aside
            while the restored one is tried.
        swapped: Whether the restored catalog is in place.
    """

    test: Test
    origin: common.Origin
    backups: Backups
    server: str
    data: dict[str, bytes] = field(default_factory=dict[str, bytes])
    copied: str = ""
    aside: str = ""
    swapped: bool = False

    @property
    def store(self) -> str:
        """The store, under framework/var.

        It is STORE (/data/pstore) in the origin, whose /data is
        origin.store.
        """
        return f"{self.origin.store}/{STORE[len('/data/') :]}"

    @property
    def catalog(self) -> str:
        """The store's catalog directory, under framework/var."""
        return f"{self.store}/{CATALOG_DIR}"

    def rel(self, name: str) -> str:
        """Return where the scenario's object name is in the namespace."""
        return self.test.rel("restore", name)

    def cli(self, *args: str) -> tuple[int, str, str]:
        """Run `pelican-server origin pstore <args>` on the store.

        It runs with the origin's keys.

        Returns:
            Its exit status, stdout, and stderr.
        """
        command = [self.server, "origin", "pstore", *args]
        command += ["--location", os.path.abspath(self.store)]
        try:
            done = subprocess.run(  # nosec B603
                command,
                env={**self.test.session.env, **keys_env(self.test, self.origin)},
                cwd=self.test.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=CLI_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"timed out after {CLI_TIMEOUT}s"
        return done.returncode, done.stdout, done.stderr

    def written(self) -> Row:
        """Write the objects, and wait for a backup of them (`objects`).

        Write an object of each of RESTORE_SIZES, with its length,
        straight to the origin, and wait for a backup taken after them,
        which is copied under test.tmp before anything else is written;
        then write LATE, which the origin must serve.
        """
        for size in RESTORE_SIZES:
            data = os.urandom(size)
            why = self.test.put(self.origin, self.rel(str(size)), data)
            if why:
                return FAIL, why
            self.data[str(size)] = data
        # A backup's name is its UTC second; one begun a full second after
        # the last write ended holds every object.
        after = int(time.time()) + 2
        if not common.wait_for(
            lambda: any((backup_time(n) or 0) >= after for n in self.backups.snapshots()),
            RESTORE_WAIT,
            interval=1,
        ):
            return FAIL, f"no backup taken after the objects within {RESTORE_WAIT}s"
        name = next(n for n in self.backups.snapshots() if (backup_time(n) or 0) >= after)
        self.copied = os.path.join(self.test.tmp, f"restore-{name}")
        shutil.copyfile(os.path.join(self.backups.outside, name), self.copied)
        late = os.urandom(5000)
        why = self.test.put(self.origin, self.rel(LATE), late)
        if why:
            return FAIL, why
        answer = web.request(
            "GET", self.test.url(self.origin, self.rel(LATE)), token=self.test.token
        )
        if answer.status != 200 or answer.body != late:
            return FAIL, f"{LATE}, written after the backup, {answer.describe()}"
        return PASS, f"{len(RESTORE_SIZES)} objects, backup {name}, then {LATE}"

    def set_aside(self) -> Optional[str]:
        """Set the stopped origin's catalog aside.

        The store then holds its block files and master key alone, as a
        restore wants.

        Returns:
            Why that failed, or None.
        """
        for part in (CATALOG_DIR, OBJECTS_DIR, MASTER_KEY):
            if not os.path.exists(f"{self.store}/{part}"):
                return f"framework/var/{self.store} has no {part}"
        self.aside = f"{self.store}/{CATALOG_DIR}.{self.test.session.run}.before-restore"
        try:
            os.rename(self.catalog, self.aside)
        except OSError as e:
            self.aside = ""
            return f"setting framework/var/{self.catalog} aside: {e.strerror}"
        return None

    def put_back(self) -> None:
        """Put the catalog set aside back, discarding the restored one."""
        if not self.aside:
            return
        try:
            if os.path.exists(self.catalog):
                shutil.rmtree(self.catalog)
            os.rename(self.aside, self.catalog)
            self.aside = ""
        except OSError as e:
            warn(f"putting framework/var/{self.aside} back: {e.strerror}")

    def restored(self) -> Row:
        """Restore the copied backup into the store (`cli`).

        `metadata-restore` must succeed, and say so.
        """
        status, out, err = self.cli("metadata-restore", self.copied)
        if status != 0:
            return FAIL, f"exit {status}: {last_line(err or out)}"
        if RESTORED not in out:
            return FAIL, f"exit 0, but did not say that it restored: {last_line(out)}"
        self.swapped = True
        return PASS, last_line(out)

    def exported(self) -> Row:
        """Check that the restored store exports what the backup held.

        `export --verify` of the scenario's collection (`export`), from
        the restored store, must write every object the backup held, byte
        for byte, verified against its recorded checksums, and not LATE.
        """
        directory = os.path.join(self.test.tmp, "restore-export")
        shutil.rmtree(directory, ignore_errors=True)
        root = self.test.catalog(self.origin, self.test.rel("restore"))
        status, out, err = self.cli("export", "--to", directory, "--verify", root)
        if status != 0:
            return FAIL, f"exit {status}: {last_line(err or out)}"
        found = exported_files(directory)
        problems: list[str] = []
        for name, data in self.data.items():
            if name not in found:
                problems.append(f"{name} was not exported")
            elif found[name] != data:
                problems.append(f"{name} was exported with other bytes")
        if LATE in found:
            problems.append(f"{LATE}, written after the backup, was exported")
        extra = sorted(set(found) - set(self.data) - {LATE})
        if extra:
            problems.append(f"exported besides: {', '.join(extra)}")
        return outcome(problems, f"{len(self.data)} objects, verified, and no {LATE}")

    def served(self) -> Row:
        """Check that the origin serves the restored catalog (`served`).

        Started on it, the origin must serve every object the backup
        held, byte for byte, and answer 404 for LATE.
        """
        why = settle(self.test)
        if why:
            return FAIL, f"once the origin started on the restored catalog, {why}"
        problems: list[str] = []
        for name, data in self.data.items():
            answer = web.request(
                "GET", self.test.url(self.origin, self.rel(name)), token=self.test.token
            )
            if answer.status != 200:
                problems.append(f"{name}: {answer.describe()}")
            elif answer.body != data:
                problems.append(f"{name}: not its bytes")
        answer = web.request(
            "GET", self.test.url(self.origin, self.rel(LATE)), token=self.test.token
        )
        if answer.status != 404:
            problems.append(f"{LATE}, written after the backup: {answer.describe()}, not 404")
        return outcome(problems, f"{len(self.data)} objects served; {LATE} gone")

    def run(self) -> dict[str, Row]:
        """Run every case, in order, and return the rows.

        The cases are the objects and the backup, with the origin
        running; the restore and the export, with it stopped; and what
        it serves once started again. Whatever happens, the origin is
        started again, on the catalog set aside unless the restored one
        served.
        """
        rows: dict[str, Row] = {"objects": guarded(self.written)}
        if rows["objects"][0] != PASS:
            return rows
        stopped = False
        try:
            control.stop(self.origin.svc)
            stopped = True
            why = self.set_aside()
            if why:
                rows["cli"] = (FAIL, why)
                return rows
            rows["cli"] = guarded(self.restored)
            if rows["cli"][0] != PASS:
                return rows
            rows["export"] = guarded(self.exported)
        except control.ControlError as e:
            rows["cli"] = (FAIL, str(e))
            return rows
        finally:
            if stopped:
                if not self.swapped:
                    self.put_back()
                else:
                    found = os.stat(f"{self.store}/{MASTER_KEY}")
                    why = give_back(self.catalog, found.st_uid, found.st_gid)
                    if why:
                        warn(why)
                try:
                    control.start(self.origin.svc)
                except control.ControlError as e:
                    warn(f"starting {self.origin.svc} again: {e}")
        rows["served"] = guarded(self.served)
        if rows["served"][0] != PASS:
            # Leave the rest of the run the catalog that served before.
            try:
                control.stop(self.origin.svc)
                self.put_back()
                control.start(self.origin.svc)
            except control.ControlError as e:
                warn(f"putting {self.origin.svc}'s catalog back: {e}")
        elif self.aside:
            shutil.rmtree(self.aside, ignore_errors=True)
            self.aside = ""
        return rows


# --------------------------------------------------------------------------
# Running the restart's scenarios.


def unrestartable(test: Test) -> Optional[str]:
    """Return why the suite cannot restart the first origin, or None.

    The restart is with configuration of the suite's own.
    """
    if test.fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return control.unavailable()


def restarted(test: Test, wanted: Sequence[str], add: Callable[[str, str, Row], None]) -> None:
    """Run the scenarios in wanted with the first origin restarted.

    Restart the first origin for the scenarios in wanted (`capacity`,
    `backups`, and `restore`), run them, restore it, and wait until the
    federation serves again. add() records a row: its scenario, case,
    and result. `restore` needs the backups that `backups` configures,
    and the `pelican-server` that has the CLI.
    """
    origin = test.fed.origins[0]
    why = unrestartable(test)
    if why:
        for scenario in wanted:
            add(scenario, "", (SKIP, why))
        return
    if "restore" in wanted and test.session.pelican_server is None:
        add(
            "restore",
            "",
            (SKIP, "no pelican-server, which has `origin pstore`; run ./fed.sh init"),
        )
        wanted = [s for s in wanted if s != "restore"]
    bound: Optional[int] = None
    if "capacity" in wanted:
        found, why = usage(test, origin)
        if found is None:
            add("capacity", "", (FAIL, f"before the restart, {why}"))
            wanted = [s for s in wanted if s != "capacity"]
        else:
            bound = int(found["usedBytes"]) + HEADROOM
    if not wanted:
        return
    backups = (
        Backups(test, origin, time.time())
        if "backups" in wanted or "restore" in wanted
        else None
    )
    text = config_text(bound, backups.inside if backups else None)
    print(f"\nRestarting {origin.svc} with:\n{text}")
    done: set[str] = set()
    try:
        with control.restart_with(origin.svc, {CONFIG_FILE: text}):
            startup = guarded(backups.started) if backups else None
            # restore comes before capacity, which would leave the store
            # too full for its objects, and backups' later cases last,
            # once the schedule has run on past restore's stop and start.
            if backups and "restore" in wanted:
                server = test.session.pelican_server or ""
                for case, row in Restore(test, origin, backups, server).run().items():
                    add("restore", case, row)
                done.add("restore")
            if bound is not None:
                for case, row in Capacity(test, origin, bound).run().items():
                    add("capacity", case, row)
                done.add("capacity")
            if backups and startup and "backups" in wanted:
                add("backups", "startup", startup)
                try:
                    rows = backups.finished()
                except Exception as e:  # pylint: disable=broad-exception-caught
                    traceback.print_exc()
                    rows = {"": (FAIL, f"{type(e).__name__}: {e}")}
                for case, row in rows.items():
                    add("backups", case, row)
                done.add("backups")
    except control.ControlError as e:
        # Restarting, or restoring, failed: what is left unrun fails.
        for scenario in wanted:
            if scenario not in done:
                add(scenario, "", (FAIL, f"restarting {origin.svc}: {e}"))
        warn(f"restarting {origin.svc}, or restoring it: {e}")
    finally:
        if backups:
            backups.remove()
    why = settle(test)
    if why:
        warn(f"with {origin.svc} restored, {why}")


# --------------------------------------------------------------------------


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None.

    There is nothing with XRootD origins, whose stores are no pstore,
    and which serve no storage API of Pelican's to be absent.
    """
    if not fed.native_origins:
        return "XRootD serves the origins, which have no pstore"
    return None


def writable(fed: common.Federation) -> Optional[str]:
    """Return the first protected namespace that takes writes, or None."""
    return next((ns for ns in NAMESPACES if "Writes" in fed.exports.get(ns, ())), None)


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, the restart's last.

    Then remove the run's objects.
    """
    fed = session.fed
    pstore = any(o.pstore for o in fed.origins)
    namespace = writable(fed)

    def add(scenario: str, case: str, row: Row) -> None:
        results.add(f"{scenario} {case}".strip(), *row)

    if namespace is None:
        for scenario in selected:
            add(
                scenario,
                "",
                (SKIP, "this shape exports no protected namespace that takes writes"),
            )
        return
    test = Test(session, namespace)
    tokens = session.setup_tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(test.pstores, fed.exports, "data/pstore", tokens)
        if errors:
            warn(f"emptying data/pstore {when}: {first(errors)}")

    if pstore:
        clean("before the run")
    print(f"pstore test against {fed.url}, objects under /{namespace}/{test.base}/")
    try:
        later = [s for s in selected if s in ("capacity", "backups", "restore")]
        for scenario in selected:
            if scenario in later:
                continue
            if not pstore and scenario != "storage-api":
                add(
                    scenario,
                    "",
                    (SKIP, f"the origins' store is {fed.origin_variant}, no pstore"),
                )
                continue
            check = {
                "storage-api": storage_api,
                "introspect-cli": introspect_cli,
                "scheduled-passes": scheduled_passes,
            }[scenario]
            try:
                rows = check(test)
            except Exception as e:  # pylint: disable=broad-exception-caught
                traceback.print_exc()
                rows = {"": (FAIL, f"{type(e).__name__}: {e}")}
            for case, row in rows.items():
                add(scenario, case, row)
        if later and not fed.origins[0].pstore:
            for scenario in later:
                add(
                    scenario,
                    "",
                    (SKIP, f"the origins' store is {fed.origin_variant}, no pstore"),
                )
        elif later:
            errors = stores.put_everywhere(
                fed.origins, namespace, test.rel(PROBE), b"probe", test.setup
            )
            if errors:
                for scenario in later:
                    add(scenario, "", (FAIL, f"putting the probe: {errors[0]}"))
            else:
                restarted(test, later, add)
    finally:
        if pstore:
            clean("after the run")
