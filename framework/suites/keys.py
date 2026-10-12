"""What happens when a server's issuer keys change while it runs: the
registry learning a key the origins add, the directors going on
accepting the origins' and a cache's advertisements once each signs
with a new key, every server taking tokens signed with it, the
issuer's own tokens carrying its kid, a V2 cache's master key, sealed
with its keys, and the database backups that every server writes. The
suite writes keys into the services' key directories under
framework/var/issuer-keys/ (the dev container mounts the checkout
read-write), as `./fed.sh keys add` and `keys rotate` do, and puts them
back as they were: a key it added is removed, and a key it made the
signer is made verify-only, by its prefix, since a V2 cache must keep
the keys that sealed its store. It runs last: a rotation takes a server
out of every director's routing until each is put back, and what it
leaves in the servers' key caches lasts up to 15 minutes. It skips under
`-p server-unprivileged`, whose servers' keys the host may not write.
The scenarios:

  hot-add      a verify-only key added to the origins shows in their
               JWKS, and the registry's for the namespace and for
               origin-0, within 2 minutes, and goes when it is removed;
               no director drops an origin meanwhile
  hot-rotate   a new signing key on the origins, and another on the first
               V2 cache, for 3 minutes: whether every director goes on
               naming origin-0 and the cache (they don't: D5, R4), how
               soon each origin and cache takes tokens signed with the
               origins' new key, that each takes the old key's
               throughout, how soon the issuer's tokens carry the new
               kid, and whether the registry learns each new key
  master-key   a V2 cache's master key must be sealed with each of its
               keys, before and after a restart (it is not: C4); and,
               destructively, with every key but the newest retired,
               the cache must still open its store; afterward the keys
               and the master key file are put back
  backups      every server writes an encrypted backup of its database
               (Server.DatabaseBackup), which `pelican-server server
               database backup verify` opens with the server's keys
"""

import glob
import json
import os
import shutil
import subprocess  # nosec B404
import time
import traceback
from collections.abc import Callable, Sequence
from typing import Optional, Union
from urllib.parse import urlsplit

from testlib import (
    accounts,
    common,
    control,
    credentials,
    director,
    issuer,
    jwt,
    metrics,
    registry,
    stores,
    web,
)
from testlib.common import die, first, warn
from testlib.issuer import User
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "hot-add": (
        "a verify-only key added to the origins reaches their JWKS and the registry's within"
        " 2 minutes, and goes when removed; the directors keep naming them"
    ),
    "hot-rotate": (
        "a new signing key on the origins and the first V2 cache: the directors' routing,"
        " tokens of either key at every server, the issuer's kid, and the registry's keys"
    ),
    "master-key": (
        "a V2 cache's master key is sealed with each of its keys, before and after a"
        " restart; with the rest retired, it still opens its store (destructive)"
    ),
    "backups": (
        "every server backs up its database, encrypted, which `pelican-server server database"
        " backup verify` opens with its keys"
    ),
}

NAMESPACE = "protected-a"
# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]
Rows = Union[tuple[str, str], list[Row]]

# How long the registry and a server's JWKS may take to show a key added
# or removed: the servers reread their key directories every minute
# (LaunchIssuerKeysDirRefresh in launcher_utils/).
KEY_WAIT = 120
# How long hot-rotate watches, and how often it asks.
ROTATE_WINDOW = 180
ROTATE_INTERVAL = 15
# How long the directors may take to name a server again once its keys
# are back, or it has restarted.
NAMED_WAIT = 150
# How long a V2 cache may take to say, with Age, that it holds an object.
HELD_WAIT = 30
# A V2 cache's master key file, under its store (local_cache/encryption.go).
MASTER_KEY = "persistent-cache/masterkey.json"
# Where each service backs its database up (Server.DatabaseBackup.Location
# in config.d/base/20-server.yaml), under its state; each backup is PEM
# (database/backup.go).
BACKUPS = "backups"
BACKUP_MAGIC = b"-----BEGIN "
SQLITE_MAGIC = b"SQLite format 3"
OBJECT_SIZE = 200_000


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: servers whose keys the host may not
    write, the tiny topology, whose one process holds every key, or no
    /protected-a."""
    if fed.drop_privileges:
        return "under -p server-unprivileged, the host may not write the servers' keys"
    if fed.tiny:
        return "the tiny topology's one process holds every server's keys"
    if NAMESPACE not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def outcome(problems: Sequence[str], note: str) -> Row:
    """A row with no case: FAIL with the first of problems, or PASS with
    note."""
    return ("", FAIL, first(problems)) if problems else ("", PASS, note)


# --------------------------------------------------------------------------
# Keys on disk, as fed.sh names them: NN-<stamp>.pem, the one that sorts
# first the signer.


def key_dirs_of(fed: common.Federation, what: str) -> list[str]:
    """The key directories (under framework/var) that hold one keyset:
    the origins' (origin-0's and origin-1's, which share a key), or a
    cache's."""
    if what == "origins":
        return [f"issuer-keys/{o.svc}" for o in fed.origins]
    return [f"issuer-keys/{what}"]


def prefixes_in(key_dir: str) -> list[int]:
    """The NN prefixes of the keys in key_dir."""
    found: list[int] = []
    for path in glob.glob(os.path.join(key_dir, "[0-9][0-9]-*.pem")):
        found.append(int(os.path.basename(path)[:2]))
    return sorted(found)


def new_key(test: "Test", dirs: Sequence[str], first_of_all: bool) -> str:
    """Make a key in each of dirs, named to sort first of its keys
    (first_of_all: the signer, as `keys rotate` names one) or last (a
    verify-only key, as `keys add` does), the same key in each; its
    name. Dies if there is no room, or no key to sort around."""
    used = sorted({n for d in dirs for n in prefixes_in(d)})
    if not used:
        die(f"no NN-*.pem key in framework/var/{dirs[0]}; run ./fed.sh keys init")
    number = used[0] - 1 if first_of_all else used[-1] + 1
    if not 0 <= number <= 99:
        die(f"no room for another key prefix in framework/var/{dirs[0]}")
    name = f"{number:02d}-keys-{test.run}.pem"
    made = registry.new_key(os.path.join(test.tmp, name))
    for key_dir in dirs:
        shutil.copyfile(made, os.path.join(key_dir, name))
        os.chmod(os.path.join(key_dir, name), 0o640)
    return name


def remove_key(dirs: Sequence[str], name: str) -> None:
    """Remove key name from each of dirs, and any .jwks beside it."""
    for key_dir in dirs:
        for ext in (".pem", ".jwks"):
            path = os.path.join(key_dir, name[: -len(".pem")] + ext)
            if os.path.exists(path):
                os.remove(path)


def demote_key(dirs: Sequence[str], name: str) -> str:
    """Rename key name in each of dirs to sort last, so that the key it
    displaced signs again: its new name."""
    used = sorted({n for d in dirs for n in prefixes_in(d) if f"{n:02d}" != name[:2]})
    number = (used[-1] if used else 50) + 1
    new_name = f"{number:02d}{name[2:]}"
    for key_dir in dirs:
        os.replace(os.path.join(key_dir, name), os.path.join(key_dir, new_name))
    return new_name


def key_ids_at(url: str) -> Optional[frozenset[str]]:
    """The kids in the JWKS at url; None if it serves none."""
    answer = web.request("GET", url)
    if answer.status != 200:
        return None
    try:
        return credentials.key_ids(answer.body)
    except ValueError:
        return None


class Test:
    """The federation, the run's keys and objects, and what the scenarios
    share."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.base = f"data/keys/{self.run}"
        self.tmp = session.path("keys")
        os.makedirs(self.tmp)
        self.caps = self.fed.exports[NAMESPACE]
        self.token = session.token(NAMESPACE)
        self.rel = f"{self.base}/probe"
        self.path = f"/{NAMESPACE}/{self.rel}"
        self.data = os.urandom(OBJECT_SIZE)
        self._probed = False
        # The origins' keys before anything changed, and the first V2
        # cache's.
        self.origin_dirs = key_dirs_of(self.fed, "origins")
        self.origin_key = self.fed.origin_key
        self.cache = next((c for c in self.fed.federated_caches if c.kind == "v2"), None)
        self.cache_key = (
            common.signing_key(f"issuer-keys/{self.cache.svc}") if self.cache else ""
        )
        # The keys the scenarios added, by directory, to put back.
        self.added: list[tuple[list[str], str]] = []
        self.rotated: list[tuple[list[str], str]] = []
        self._cache_name: Optional[str] = None

    def put_probe(self) -> None:
        """Put the probe object at every origin, once."""
        if self._probed:
            return
        self._probed = True
        errors = stores.make_dirs(self.fed.origins, NAMESPACE, self.base, self.token)
        errors += stores.seed_everywhere(
            self.fed.origins, NAMESPACE, self.rel, self.data, self.token, self.caps
        )
        if errors:
            die(f"could not put the probe in place: {first(errors)}")

    def origin_host(self) -> str:
        """origin-0's host, as the directors name it."""
        return director.host(self.fed.origins[0].url)

    def registry_jwks(self, prefix: str) -> str:
        """The URL of the registry's JWKS for prefix."""
        return f"{self.fed.registry_url}/api/v1.0/registry{prefix}/.well-known/issuer.jwks"

    def origin_prefix(self) -> str:
        """origin-0's registry prefix: /origins/<host:port> of its web URL
        (GetOriginNs)."""
        return f"/origins/{urlsplit(self.fed.origins[0].web_url).netloc}"

    def cache_name(self) -> str:
        """The first V2 cache's name, as it records it, by which its
        registry prefix is /caches/<name> (GetCacheNs); "" if it will not
        say."""
        if self._cache_name is None:
            self._cache_name = ""
            if self.cache:
                api = accounts.Api(
                    self.cache.web_url,
                    credentials.mint_admin(
                        self.fed,
                        self.session.path(f"tokens/keys.admin.{self.cache.svc}"),
                        credentials.local_issuer(self.fed, self.cache.web_url),
                        self.cache_key,
                    ),
                )
                answer = api.call("GET", "/api/v1.0/server/localMetadata")
                if answer.status == 200:
                    self._cache_name = str(answer.object.get("name") or "")
        return self._cache_name

    def named(self, route: str, host: str) -> list[str]:
        """The directors that would send a GET of the probe, for route, to
        host."""
        return director.naming(self.fed.directors, route, self.path, host)

    def wait_named(self, route: str, host: str, seconds: float = NAMED_WAIT) -> bool:
        """Whether every director names host for the probe within
        seconds."""
        return common.wait_for(
            lambda: len(self.named(route, host)) == len(self.fed.directors), seconds, 5
        )

    def signed_with(self, key: str) -> str:
        """A token like the `server` token for /protected-a, signed with
        key (see credentials.server_claims())."""
        claims = credentials.server_claims(self.fed, NAMESPACE)
        return jwt.encode(jwt.header(jwt.algorithm(key), jwt.key_id(key)), claims, key=key)

    def data_servers(self) -> list[tuple[str, str]]:
        """Each origin that takes direct clients, and each federated
        cache: its service and its data URL."""
        found = [(o.svc, o.url) for o in self.fed.origins if "DirectReads" in self.caps]
        found += [(c.svc, c.url) for c in self.fed.federated_caches]
        return found

    def served(self, url: str, token: str) -> bool:
        """Whether a GET of the probe at the server at url, with token,
        serves it."""
        answer = web.request("GET", f"{url}{self.path}", token=token, timeout=60)
        return answer.status == 200 and answer.body == self.data


def put_back(test: Test) -> None:
    """Remove the keys the scenarios added, and demote those they made
    signers, then wait until every director names origin-0 again."""
    for dirs, name in test.added:
        remove_key(dirs, name)
    test.added.clear()
    for dirs, name in test.rotated:
        try:
            demote_key(dirs, name)
        except OSError as e:
            warn(f"could not demote {name} in {dirs}: {e}")
    test.rotated.clear()
    if test.fed.directors and not test.wait_named("origin", test.origin_host()):
        warn(f"{NAMED_WAIT}s after the keys were put back, not every director names origin-0")


# --------------------------------------------------------------------------
# hot-add


def hot_add(test: Test) -> list[Row]:
    """A verify-only key, written into the origins' key directories so
    that it sorts last: within KEY_WAIT seconds, origin-0's own JWKS
    must list it (`origin-jwks`), and so must the registry's for the
    namespace (`registry-namespace`) and for origin-0's own prefix
    (`registry-server`), since an origin pushes its keys to the registry
    when they change (updateNamespacesPubKey in launcher_utils/, which
    signs the update with a key the registry already holds); meanwhile
    no director may drop origin-0 (`routing`). Then the key is removed,
    and each must be rid of it as soon (`removed`)."""
    fed = test.fed
    if not fed.registry_url:
        return [("", SKIP, "a standalone origin registers nothing")]
    test.put_probe()
    name = new_key(test, test.origin_dirs, first_of_all=False)
    test.added.append((test.origin_dirs, name))
    kid = jwt.key_id(os.path.join(test.origin_dirs[0], name))
    sources = {
        "origin-jwks": f"{fed.origin_web_url}/.well-known/issuer.jwks",
        "registry-namespace": test.registry_jwks(f"/{NAMESPACE}"),
        "registry-server": test.registry_jwks(test.origin_prefix()),
    }
    host = test.origin_host()
    rows: list[Row] = []
    seen: dict[str, float] = {}
    dropped: Optional[float] = None
    started = time.monotonic()
    while time.monotonic() - started < KEY_WAIT and len(seen) < len(sources):
        for case, url in sources.items():
            if case not in seen and kid in (key_ids_at(url) or ()):
                seen[case] = time.monotonic() - started
        if dropped is None and len(test.named("origin", host)) != len(fed.directors):
            dropped = time.monotonic() - started
        time.sleep(5)
    for case in sources:
        if case in seen:
            rows.append((case, PASS, f"the key appeared after {seen[case]:.0f}s"))
        else:
            rows.append((case, FAIL, f"the key never appeared in {KEY_WAIT}s"))
    remove_key(test.origin_dirs, name)
    test.added.remove((test.origin_dirs, name))
    gone: dict[str, float] = {}
    started = time.monotonic()
    while time.monotonic() - started < KEY_WAIT and len(gone) < len(sources):
        for case, url in sources.items():
            if case not in gone and kid not in (key_ids_at(url) or {kid}):
                gone[case] = time.monotonic() - started
        if dropped is None and len(test.named("origin", host)) != len(fed.directors):
            dropped = time.monotonic() - started
        time.sleep(5)
    if len(gone) < len(sources):
        still = [case for case in sources if case not in gone]
        rows.append(
            ("removed", FAIL, f"{KEY_WAIT}s after the key was removed, it is still in {still}")
        )
    else:
        rows.append(("removed", PASS, f"gone within {max(gone.values()):.0f}s"))
    if dropped is not None:
        rows.append(("routing", FAIL, f"a director dropped origin-0 {dropped:.0f}s in"))
    elif fed.directors:
        rows.append(
            ("routing", PASS, f"{len(fed.directors)} director(s) named origin-0 throughout")
        )
    return rows


# --------------------------------------------------------------------------
# hot-rotate


def hot_rotate(test: Test) -> list[Row]:
    """A new signing key on the origins, and another on the first V2
    cache, watched for ROTATE_WINDOW seconds, every ROTATE_INTERVAL:
    every director must go on naming origin-0 and the cache
    (`directors`), each origin and cache must come to take tokens signed
    with the origins' new key (`new-key`), and must take the old key's
    throughout (`old-key`), and the issuer's tokens must come to carry
    the new kid (`issuer-kid`); the registry must come to list the
    origins' new key (`registry-origins`) and the cache's
    (`registry-cache`). A director checks an ad against the keys it
    fetched for the server's prefix up to Director.AdvertisementTTL (15
    minutes) before, and never refetches for a kid it lacks
    (verifyAdvertiseToken in director/origin_api.go), so once their ads
    carry the new kid the directors refuse them, and drop origin-0 and
    the cache (D5); and a cache never pushes a new key to the registry
    (R4). Origins and caches keep a namespace's keyset for up to 15
    minutes too (origin_serve/authz.go, local_cache/cache_authz.go), so
    `new-key` is inconclusive if the window is not enough."""
    fed = test.fed
    if not fed.registry_url or not fed.directors:
        return [("", SKIP, "a standalone origin has no registry or director")]
    test.put_probe()
    cache_name = test.cache_name()
    old_tokens = test.signed_with(test.origin_key)
    client: Optional[issuer.Client] = None
    admin_web = ""
    if fed.embedded_issuer and not fed.external_issuer:
        admin_web = credentials.mint_admin(
            test.fed,
            test.session.path("tokens/keys.admin.origin"),
            credentials.local_issuer(fed, fed.origin_web_url),
            test.origin_key,
        )
        client = issuer.create_client(
            fed.origin_web_url,
            f"/{NAMESPACE}",
            admin_web,
            name=f"pelican-test-framework keys {test.run}",
        )
    origin_name = new_key(test, test.origin_dirs, first_of_all=True)
    test.rotated.append((test.origin_dirs, origin_name))
    new_origin_key = os.path.join(test.origin_dirs[0], origin_name)
    new_kid = jwt.key_id(new_origin_key)
    cache_dirs: list[str] = []
    cache_kid = ""
    if test.cache:
        cache_dirs = key_dirs_of(fed, test.cache.svc)
        cache_name_on_disk = new_key(test, cache_dirs, first_of_all=True)
        test.rotated.append((cache_dirs, cache_name_on_disk))
        cache_kid = jwt.key_id(os.path.join(cache_dirs[0], cache_name_on_disk))
    new_tokens = test.signed_with(new_origin_key)
    # Under the external issuer, the exports trust none of the origins'
    # keys, so no token of either says anything.
    data_servers = [] if fed.external_issuer else test.data_servers()
    watched = [("origin-0", "origin", test.origin_host())]
    if test.cache:
        watched.append((test.cache.svc, "object", director.host(test.cache.url)))
    dropped: dict[str, float] = {}
    accepted: dict[str, float] = {}
    refused_old: dict[str, float] = {}
    issuer_kid_at: Optional[float] = None
    registry_at: dict[str, float] = {}
    started = time.monotonic()
    while True:
        now = time.monotonic() - started
        for svc, route, host in watched:
            if svc not in dropped and len(test.named(route, host)) != len(fed.directors):
                dropped[svc] = now
        for svc, url in data_servers:
            if svc not in accepted and test.served(url, new_tokens):
                accepted[svc] = now
            if svc not in refused_old and not test.served(url, old_tokens):
                refused_old[svc] = now
        if client and issuer_kid_at is None:
            try:
                minted = issuer.token(client, User(token=admin_web), ("storage.read:/",))
                if issuer.header(minted).get("kid") == new_kid:
                    issuer_kid_at = now
            except issuer.IssuerError:
                pass
        if "origins" not in registry_at and new_kid in (
            key_ids_at(test.registry_jwks(f"/{NAMESPACE}")) or ()
        ):
            registry_at["origins"] = now
        if (
            cache_name
            and "cache" not in registry_at
            and cache_kid in (key_ids_at(test.registry_jwks(f"/caches/{cache_name}")) or ())
        ):
            registry_at["cache"] = now
        if now >= ROTATE_WINDOW:
            break
        time.sleep(ROTATE_INTERVAL)
    rows: list[Row] = []
    for svc, _, _ in watched:
        if svc in dropped:
            rows.append(
                (
                    f"directors {svc}",
                    FAIL,
                    f"a director dropped it {dropped[svc]:.0f}s after the rotation",
                )
            )
        else:
            rows.append(
                (f"directors {svc}", PASS, f"named by every director for {ROTATE_WINDOW}s")
            )
    if fed.external_issuer:
        rows.append(
            ("tokens", SKIP, "the exports trust the external issuer, not the origins' keys")
        )
    for svc, _ in data_servers:
        if svc in accepted:
            rows.append(
                (f"new-key {svc}", PASS, f"took the new key's token {accepted[svc]:.0f}s in")
            )
        else:
            rows.append(
                (
                    f"new-key {svc}",
                    INCONCLUSIVE,
                    f"refused it for all {ROTATE_WINDOW}s (its keyset may be cached for up to 15 minutes)",
                )
            )
        if svc in refused_old:
            rows.append(
                (
                    f"old-key {svc}",
                    FAIL,
                    f"refused the old key's token {refused_old[svc]:.0f}s in",
                )
            )
        else:
            rows.append((f"old-key {svc}", PASS, "took the old key's token throughout"))
    if client:
        if issuer_kid_at is None:
            rows.append(
                (
                    "issuer-kid",
                    FAIL,
                    f"the issuer's tokens never carried the new kid in {ROTATE_WINDOW}s",
                )
            )
        else:
            rows.append(("issuer-kid", PASS, f"the new kid {issuer_kid_at:.0f}s in"))
        issuer.delete_client(fed.origin_web_url, f"/{NAMESPACE}", admin_web, client.client_id)
    else:
        rows.append(("issuer-kid", SKIP, "the origins run no issuer of their own"))
    if "origins" in registry_at:
        rows.append(
            (
                "registry-origins",
                PASS,
                f"listed the origins' new key {registry_at['origins']:.0f}s in",
            )
        )
    else:
        rows.append(
            ("registry-origins", FAIL, f"never listed the origins' new key in {ROTATE_WINDOW}s")
        )
    if not test.cache:
        rows.append(("registry-cache", SKIP, "no federated V2 cache"))
    elif not cache_name:
        rows.append(
            ("registry-cache", INCONCLUSIVE, f"{test.cache.svc} would not say its name")
        )
    elif "cache" in registry_at:
        rows.append(
            (
                "registry-cache",
                PASS,
                f"listed the cache's new key {registry_at['cache']:.0f}s in",
            )
        )
    else:
        rows.append(
            ("registry-cache", FAIL, f"never listed the cache's new key in {ROTATE_WINDOW}s")
        )
    put_back(test)
    return rows


# --------------------------------------------------------------------------
# master-key


def master_key_kids(cache: common.Cache) -> Optional[frozenset[str]]:
    """The kids with which a V2 cache's master key file seals its master
    key; None if there is no such file."""
    path = f"data/cache/{cache.svc.rsplit('-', 1)[-1]}/{MASTER_KEY}"
    try:
        with open(path, encoding="utf-8") as f:
            found = common.as_object(json.load(f)) or {}
    except (FileNotFoundError, ValueError):
        return None
    return frozenset(str(k) for k in (common.as_object(found.get("keys")) or {}))


def master_key_path(cache: common.Cache) -> str:
    """The master key file of cache, under framework/var."""
    return f"data/cache/{cache.svc.rsplit('-', 1)[-1]}/{MASTER_KEY}"


def held_with_age(test: Test, cache: common.Cache) -> Optional[int]:
    """The Age with which cache serves the probe, if it serves it from its
    store; None otherwise."""
    answer = web.request("GET", f"{cache.url}{test.path}", token=test.token, timeout=120)
    if answer.status != 200 or answer.body != test.data:
        return None
    age = answer.header("Age").strip()
    return int(age) if age.isdigit() else None


def master_key(test: Test) -> list[Row]:
    """The first V2 cache's master key file must seal the master key with
    each of the cache's keys, a verify-only key the suite adds included,
    before a restart (`sealed`) and after one (`sealed-after-restart`):
    Pelican re-seals it only through a key-change callback that the
    launcher registers for its local cache module, not for the cache
    server, and loading never re-saves it (C4). Then, destructively,
    with every key but the newest removed from the cache's key
    directory, the cache restarted must still open its store, and serve
    the probe it held, with Age, fetching nothing (`retired`): a master
    key it cannot unseal is replaced by a new one, with which its store
    cannot be read. Afterward the keys and the master key file are put
    back, and the cache restarted again."""
    fed = test.fed
    cache = test.cache
    if cache is None:
        return [("", SKIP, "no federated V2 cache")]
    why = control.unavailable()
    if why:
        return [("", SKIP, why)]
    test.put_probe()
    if master_key_kids(cache) is None:
        return [("", FAIL, f"framework/var/{master_key_path(cache)} is missing or unreadable")]
    dirs = key_dirs_of(fed, cache.svc)
    if len(prefixes_in(dirs[0])) < 2:
        name = new_key(test, dirs, first_of_all=False)
        test.added.append((dirs, name))
        print(f"Added {name} to {cache.svc}'s keys; waiting a minute for it to load ...")
        time.sleep(65)
    keys = sorted(glob.glob(os.path.join(dirs[0], "[0-9][0-9]-*.pem")))
    kids = {jwt.key_id(k): k for k in keys}
    host = director.host(cache.url)
    rows: list[Row] = []

    def sealed_row(case: str) -> Row:
        found = master_key_kids(cache)
        if found is None:
            return (case, FAIL, "the master key file is missing or unreadable")
        missing = sorted(os.path.basename(kids[k]) for k in kids if k not in found)
        if missing:
            return (case, FAIL, f"not sealed with {', '.join(missing)}")
        return (case, PASS, f"sealed with all {len(kids)} keys")

    rows.append(sealed_row("sealed"))
    if held_with_age(test, cache) is None:
        answer = web.request("GET", f"{cache.url}{test.path}", token=test.token, timeout=120)
        if answer.status != 200 or answer.body != test.data:
            return rows + [
                ("sealed-after-restart", FAIL, f"a GET of the probe got {answer.describe()}")
            ]
        if not common.wait_for(lambda: held_with_age(test, cache) is not None, HELD_WAIT, 1):
            return rows + [
                ("sealed-after-restart", FAIL, f"no Age within {HELD_WAIT}s of a whole read")
            ]
    try:
        control.restart(cache.svc)
    except control.ControlError as e:
        return rows + [("sealed-after-restart", FAIL, str(e))]
    if not test.wait_named("object", host):
        return rows + [
            (
                "sealed-after-restart",
                FAIL,
                f"{NAMED_WAIT}s after the restart, not every director named it",
            )
        ]
    rows.append(sealed_row("sealed-after-restart"))
    # The destructive part: keep the newest key alone.
    newest = keys[-1]
    retired = [k for k in keys if k != newest]
    kept = os.path.join(test.tmp, f"kept-{cache.svc}")
    os.makedirs(kept, exist_ok=True)
    for key in retired:
        shutil.copyfile(key, os.path.join(kept, os.path.basename(key)))
    shutil.copyfile(master_key_path(cache), os.path.join(kept, "masterkey.json"))
    before = sent_bytes(fed)
    problems: list[str] = []
    try:
        control.stop(cache.svc)
        for key in retired:
            for key_dir in dirs:
                os.remove(os.path.join(key_dir, os.path.basename(key)))
        try:
            control.start(cache.svc)
            if not test.wait_named("object", host):
                problems.append(
                    f"{NAMED_WAIT}s after it started with the old keys retired, not every director named it"
                )
            else:
                age = held_with_age(test, cache)
                if age is None:
                    problems.append(
                        "with the old keys retired, it does not serve the probe from its store"
                    )
                after = sent_bytes(fed)
                if before is not None and after is not None and after - before >= OBJECT_SIZE:
                    problems.append(
                        "with the old keys retired, the origins sent the probe again"
                    )
        except control.ControlError as e:
            problems.append(f"with the old keys retired, {e}")
    finally:
        restored = restore_cache(test, cache, dirs, kept, retired)
        if restored:
            problems.append(restored)
    rows.append(
        (
            "retired",
            *outcome(problems, f"opened its store with {os.path.basename(newest)} alone")[1:],
        )
    )
    return rows


def restore_cache(
    test: Test, cache: common.Cache, dirs: Sequence[str], kept: str, retired: Sequence[str]
) -> Optional[str]:
    """Put cache's retired keys and master key file back, and restart it;
    why that failed, or None."""
    try:
        control.stop(cache.svc)
    except control.ControlError as e:
        warn(str(e))
    for key in retired:
        for key_dir in dirs:
            shutil.copyfile(
                os.path.join(kept, os.path.basename(key)),
                os.path.join(key_dir, os.path.basename(key)),
            )
            os.chmod(os.path.join(key_dir, os.path.basename(key)), 0o640)
    shutil.copyfile(os.path.join(kept, "masterkey.json"), master_key_path(cache))
    try:
        control.start(cache.svc)
    except control.ControlError as e:
        return (
            f"once its keys were put back, {e}; run ./reset.sh if {cache.svc} does not recover"
        )
    if not test.wait_named("object", director.host(cache.url)):
        return (
            f"{NAMED_WAIT}s after its keys were put back, not every director names {cache.svc}"
        )
    return None


def sent_bytes(fed: common.Federation) -> Optional[float]:
    """What the native origins have sent for GETs, in bytes; None where
    they keep no such counter, or one did not answer."""
    if not fed.native_origins:
        return None
    total = 0.0
    for origin in fed.origins:
        found = metrics.fetch(origin.web_url)
        if found is None:
            return None
        total += found.value(
            "pelican_http_bytes_total", server_type="origin", direction="out", method="GET"
        )
    return total


# --------------------------------------------------------------------------
# backups


def services_of(fed: common.Federation) -> list[tuple[str, str]]:
    """Each Pelican server's service, and its key directory."""
    found = [(o.svc, f"issuer-keys/{o.svc}") for o in fed.origins]
    found += [(o.origin.svc, o.key_dir) for o in fed.owners]
    found += [(c.svc, f"issuer-keys/{c.svc}") for c in fed.caches]
    found += [(director.host(u), f"issuer-keys/{director.host(u)}") for u in fed.directors]
    if fed.registry_url:
        host = director.host(fed.registry_url)
        found.append((host, f"issuer-keys/{host}"))
    return found


def verify_backup(
    server: str, path: str, env: dict[str, str], cwd: str
) -> tuple[int, str, str]:
    """`pelican-server server database backup verify path`: its exit
    status, stdout, and stderr."""
    try:
        done = subprocess.run(  # nosec B603
            [server, "server", "database", "backup", "verify", path],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after 120s"
    return done.returncode, done.stdout, done.stderr


def backups(test: Test) -> list[Row]:
    """Each server's backups, under framework/var/state/<service>/backups/
    (Server.DatabaseBackup.Location): at least one, since a server with
    none makes one as it starts (LaunchPeriodicBackup in
    database/backup.go); each PEM, never a plain SQLite file; and each
    opened by `pelican-server server database backup verify`, given a
    copy of the server's keys, which skips without a pelican-server."""
    rows: list[Row] = []
    server = test.session.pelican_server
    for svc, key_dir in services_of(test.fed):
        found = sorted(glob.glob(f"state/{svc}/{BACKUPS}/*.bak"))
        if not found:
            rows.append((svc, FAIL, f"no backup in framework/var/state/{svc}/{BACKUPS}/"))
            continue
        problems: list[str] = []
        for path in found:
            try:
                with open(path, "rb") as f:
                    head = f.read(len(SQLITE_MAGIC))
            except OSError as e:
                problems.append(f"{os.path.basename(path)}: {e}")
                continue
            if head.startswith(SQLITE_MAGIC):
                problems.append(f"{os.path.basename(path)} is a plain SQLite file")
            elif not head.startswith(BACKUP_MAGIC):
                problems.append(f"{os.path.basename(path)} is not PEM")
        if problems:
            rows.append((svc, FAIL, first(problems)))
            continue
        if server is None:
            rows.append(
                (
                    svc,
                    PASS,
                    f"{len(found)} encrypted backup(s); no pelican-server to verify them",
                )
            )
            continue
        keys = os.path.join(test.tmp, f"keys-{svc}")
        if os.path.isdir(keys):
            shutil.rmtree(keys)
        shutil.copytree(key_dir, keys)
        for key in glob.glob(os.path.join(keys, "*.pem")):
            os.chmod(key, 0o600)
        env = dict(test.session.env)
        env["PELICAN_ISSUERKEYSDIRECTORY"] = os.path.abspath(keys)
        verified = 0
        for path in found:
            status, out, err = verify_backup(server, os.path.abspath(path), env, test.tmp)
            if status != 0:
                problems.append(
                    f"verify {os.path.basename(path)}: exit {status}: {last_line(err or out)}"
                )
                break
            verified += 1
        rows.append((svc, *outcome(problems, f"{verified} encrypted backup(s) verified")[1:]))
    return rows


# --------------------------------------------------------------------------

RUN: dict[str, Callable[[Test], Rows]] = {
    "hot-add": hot_add,
    "hot-rotate": hot_rotate,
    "master-key": master_key,
    "backups": backups,
}


def guarded(name: str, test: Test) -> list[Row]:
    """Scenario name's rows; one failing row if it raised."""
    try:
        found = RUN[name](test)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]
    if isinstance(found, list):
        return found
    return [("", found[0], found[1])]


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, put the keys back, and remove the
    run's objects."""
    fed = session.fed
    test = Test(session)
    print(f"Keys against {fed.url}, the probe at /{NAMESPACE}/{test.rel}\n")
    try:
        for name in selected:
            for case, status, note in guarded(name, test):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        put_back(test)
        tokens = session.tokens()
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/keys", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
        shutil.rmtree(test.tmp, ignore_errors=True)
