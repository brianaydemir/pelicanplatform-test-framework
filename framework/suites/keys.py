"""Issuer keys changed while the servers run.

The suite adds and rotates keys in the services' key directories under
framework/var/issuer-keys/, as `./fed.sh keys add` and `keys rotate`
do, and restores them afterward. A key it made the signer is kept as a
verify-only key, since V2 caches and pstore origins need the keys that
sealed their stores. It runs last, since servers cache keys for up to
15 minutes, so its changes outlast it. It skips under
`-p server-unprivileged`, whose servers' keys the host may not write,
and the tiny topology, whose one process holds every key.

Every scenario runs in the WLCG pass alone, since a second pass would
change the keys again. `hot-rotate` presents every profile's tokens
itself; the rows of each profile but WLCG end in its name (e.g.
`new-key origin-0 scitokens`). See docs/tests/keys.md.
"""

import functools
import glob
import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from testlib import (
    accounts,
    blocks,
    common,
    control,
    credentials,
    director,
    issuer,
    jwt,
    metrics,
    registry,
    stores,
    suite,
    web,
)
from testlib.common import die, first, outcome, warn
from testlib.issuer import User
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line
from testlib.suite import NAMESPACE, CaseRow

SCENARIOS = {
    "hot-add": (
        "a verify-only key added to the origins reaches origin-0's JWKS and the registry's"
        " within 2 minutes, and goes when removed; the directors keep naming origin-0"
    ),
    "hot-rotate": (
        "a new signing key on the origins and the first V2 cache: the directors' routing,"
        " tokens of either key at every server, the issuer's kid, and the registry's keys"
    ),
    "master-key": (
        "a V2 cache's master key is sealed with each of its keys, before and after a"
        " restart; with the rest retired, it still opens its store (destructive)"
    ),
    "pstore-master-key": (
        "a pstore origin's master key is sealed with each of its keys, before and after a"
        " restart; with the rest retired, it still opens its store (destructive)"
    ),
    "backups": (
        "every server backs up its database, encrypted, which `pelican-server server database"
        " backup verify` opens with its keys"
    ),
}

# See suites/__init__.py.
ONE_PROFILE = tuple(SCENARIOS)

# How long the registry and a server's JWKS may take to show a key added
# or removed: the servers reread their key directories every minute.
KEY_WAIT = 120
# How long hot-rotate watches, and how often it asks.
ROTATE_WINDOW = 180
ROTATE_INTERVAL = 15
# How long master-key waits for a V2 cache to serve the probe, which a
# failed hot-rotate can keep it from for a while.
PROBE_WAIT = 150
# How a server answers the probe for a token (Test.probe()): it serves
# the probe, or refuses the token, by one of REFUSALS.
SERVED, REFUSED = "served", "refused"
REFUSALS = (401, 403)
# The master key file of a V2 cache, under its store, and of a pstore
# origin, under its /data (Origin.PStoreLocation, /data/pstore, in
# config.d/origin/pstore/50-origin.yaml).
MASTER_KEY = "persistent-cache/masterkey.json"
PSTORE_MASTER_KEY = "pstore/masterkey.json"
# Where each service backs up its database, under its state
# (Server.DatabaseBackup.Location in config.d/base/20-server.yaml), and
# how a backup, which is PEM, and a plain SQLite file begin.
BACKUPS = "backups"
BACKUP_MAGIC = b"-----BEGIN "
SQLITE_MAGIC = b"SQLite format 3"
# The probe's name, in the run's collection, and size.
PROBE = "probe"
PROBE_SIZE = 200_000


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None.

    There is nothing where the host may not write the servers' keys, in
    the tiny topology, whose one process holds every key, or without
    /protected-a.
    """
    if fed.drop_privileges:
        return "under -p server-unprivileged, the host may not write the servers' keys"
    if fed.tiny:
        return "the tiny topology's one process holds every server's keys"
    return suite.unexported(fed)


# --------------------------------------------------------------------------
# Keys on disk, as fed.sh names them: NN-<stamp>.pem, the one that sorts
# first the signer.


def key_dirs_of(fed: common.Federation, what: str) -> list[str]:
    """Return the key directories, under framework/var, of one keyset.

    Args:
        fed: The federation.
        what: Whose keyset: `origins` for the origins' (origin-0's and
            origin-1's, which share a key), or a cache's service for its
            own.
    """
    if what == "origins":
        return [f"issuer-keys/{o.svc}" for o in fed.origins]
    return [f"issuer-keys/{what}"]


def prefixes_in(key_dir: str) -> list[int]:
    """Return the NN prefixes of the keys in key_dir."""
    found: list[int] = []
    for path in glob.glob(os.path.join(key_dir, "[0-9][0-9]-*.pem")):
        found.append(int(os.path.basename(path)[:2]))
    return sorted(found)


def new_key(test: "Test", dirs: Sequence[str], first_of_all: bool) -> str:
    """Make the same new key in each of dirs, and return its name.

    It is named to sort first of its keys (first_of_all: the signer, as
    `keys rotate` names one) or last (a verify-only key, as `keys add`
    does). Dies if there is no room, or no key to sort around.
    """
    used = sorted({n for d in dirs for n in prefixes_in(d)})
    if not used:
        die(f"no NN-*.pem key in framework/var/{dirs[0]}; run ./fed.sh keys init")
    number = used[0] - 1 if first_of_all else used[-1] + 1
    if not 0 <= number <= 99:
        die(f"no room for another key prefix in framework/var/{dirs[0]}")
    name = f"{number:02d}-keys-{test.run}.pem"
    # Another keyset's key may have had this name, so the scratch copy
    # lasts only this call: registry.new_key would reuse one left behind.
    made = os.path.join(test.tmp, name)
    try:
        registry.new_key(made)
        for key_dir in dirs:
            shutil.copyfile(made, os.path.join(key_dir, name))
            os.chmod(os.path.join(key_dir, name), 0o640)
    finally:
        if os.path.exists(made):
            os.remove(made)
    return name


def remove_key(dirs: Sequence[str], name: str) -> None:
    """Remove key name from each of dirs, and any .jwks beside it."""
    for key_dir in dirs:
        for ext in (".pem", ".jwks"):
            path = os.path.join(key_dir, name[: -len(".pem")] + ext)
            if os.path.exists(path):
                os.remove(path)


def demote_key(dirs: Sequence[str], name: str) -> str:
    """Rename key name in each of dirs to sort last; return its new name.

    The key it displaced then signs again.
    """
    used = sorted({n for d in dirs for n in prefixes_in(d) if f"{n:02d}" != name[:2]})
    number = (used[-1] if used else 50) + 1
    new_name = f"{number:02d}{name[2:]}"
    for key_dir in dirs:
        os.replace(os.path.join(key_dir, name), os.path.join(key_dir, new_name))
    return new_name


def key_ids_at(url: str) -> frozenset[str] | None:
    """Return the kids in the JWKS at url, or None if it serves none."""
    answer = web.request("GET", url)
    if answer.status != 200:
        return None
    try:
        return jwt.key_ids(answer.body)
    except ValueError:
        return None


class Test(suite.Test):
    """The probe in /protected-a, and the keys that the scenarios change.

    Attributes:
        data: The probe's bytes.
        origin_dirs: The origins' key directories.
        origin_key: The origins' signing key, before anything changed.
        cache: The first federated V2 cache, if any.
        cache_key: Its signing key, before anything changed.
        added: The keys the scenarios added, by directory, to remove.
        rotated: The keys the scenarios made signers, by directory, to
            demote.
    """

    def __init__(self, session: Session):
        super().__init__(session, "keys")
        self.data = os.urandom(PROBE_SIZE)
        self._probed = False
        self.origin_dirs = key_dirs_of(self.fed, "origins")
        self.origin_key = self.fed.origin_key
        self.cache = next((c for c in self.fed.federated_caches if c.kind == "v2"), None)
        self.cache_key = common.signing_key(f"issuer-keys/{self.cache.svc}") if self.cache else ""
        self.added: list[tuple[list[str], str]] = []
        self.rotated: list[tuple[list[str], str]] = []
        self._cache_name: str | None = None

    def put_probe(self) -> None:
        """Put the probe at every origin, once."""
        if self._probed:
            return
        self._probed = True
        why = self.put({self.rel(PROBE): self.data})
        if why:
            die(f"could not put the probe in place: {why}")

    def origin_host(self) -> str:
        """Return origin-0's host, as the directors name it."""
        return director.host(self.fed.origins[0].url)

    def registry_jwks(self, prefix: str) -> str:
        """Return the URL of the registry's JWKS for prefix."""
        return f"{self.fed.registry_url}{registry.prefix_route(prefix)}/.well-known/issuer.jwks"

    def origin_prefix(self) -> str:
        """Return origin-0's registry prefix (GetOriginNs).

        That is /origins/<host:port> of its web URL.
        """
        return f"/origins/{urlsplit(self.fed.origins[0].web_url).netloc}"

    def cache_name(self) -> str:
        """Return the first V2 cache's name, as it records it.

        Its registry prefix is /caches/<name> (GetCacheNs).

        Returns:
            The name, or "" if there is no such cache, or it will not say.
        """
        if self._cache_name is None:
            self._cache_name = ""
            if self.cache:
                token = self.session.web_token(self.cache.web_url, key=self.cache_key)
                api = accounts.Api(self.cache.web_url, token)
                answer = api.call("GET", "/api/v1.0/server/localMetadata")
                if answer.status == 200:
                    self._cache_name = str(answer.object().get("name") or "")
        return self._cache_name

    def named(self, route: str, host: str) -> list[str]:
        """Return the directors that would send a GET of the probe to host.

        Args:
            route: The route to ask of: `object` (for a cache) or
                `origin`.
            host: A server's host, as the directors name it.
        """
        return director.naming(self.fed.directors, route, self.path(self.rel(PROBE)), host)

    def wait_named(self, route: str, host: str, seconds: float = suite.SETTLE_WAIT) -> str | None:
        """Wait until every director names host for the probe.

        See director.wait_named().

        Returns:
            Why they did not all within seconds, or None if they did.
        """
        return director.wait_named(self.fed.directors, route, self.path(self.rel(PROBE)), host, seconds)

    def signed_with(self, key: str, profile: str) -> str:
        """Return a token like the `server` token, but signed with key.

        Its claims are /protected-a's, of profile (see
        credentials.server_claims()).
        """
        claims = credentials.server_claims(self.fed, NAMESPACE, profile=profile)
        return jwt.sign(claims, key)

    def data_servers(self) -> list[tuple[str, str]]:
        """Return each origin that takes direct clients, and each cache.

        Returns:
            The service and data URL of each such origin, and of each
            federated cache.
        """
        found = [(o.svc, o.url) for o in self.fed.origins if self.direct]
        found += [(c.svc, c.url) for c in self.fed.federated_caches]
        return found

    def served(self, url: str, token: str) -> bool:
        """Return whether the server at url serves the probe for token."""
        return self.probe(url, token) == SERVED

    def probe(self, url: str, token: str) -> str:
        """Return how the server at url answers the probe for token.

        Returns:
            SERVED, if it serves the probe; REFUSED, if it refuses the
            token (REFUSALS); or else what it answered, for a message.
        """
        answer = web.request("GET", self.url(url, self.rel(PROBE)), token=token, timeout=60)
        if answer.status == 200 and answer.body == self.data:
            return SERVED
        if answer.status in REFUSALS:
            return REFUSED
        return "HTTP 200, not the probe" if answer.status == 200 else answer.describe()


def put_back(test: Test) -> None:
    """Remove the keys the scenarios added, and demote those made signers.

    Then wait until every director names origin-0 again.
    """
    for dirs, name in test.added:
        remove_key(dirs, name)
    test.added.clear()
    for dirs, name in test.rotated:
        try:
            demote_key(dirs, name)
        except OSError as e:
            warn(f"could not demote {name} in {dirs}: {e}")
    test.rotated.clear()
    why = test.wait_named("origin", test.origin_host()) if test.fed.directors else None
    if why:
        warn(f"with the keys put back, {why}")


# --------------------------------------------------------------------------
# hot-add


def hot_add(test: Test) -> list[CaseRow]:
    """Check that a verify-only key added to the origins comes and goes.

    The key sorts last in the origins' key directories. Within KEY_WAIT
    seconds, it must appear in origin-0's JWKS (`origin-jwks`), and in
    the registry's for the namespace (`registry-namespace`) and for
    origin-0's own prefix (`registry-server`), since an origin pushes
    its keys to the registry when they change. Once the key is removed,
    each must drop it as soon (`removed`). No director may drop origin-0
    meanwhile (`routing`).
    """
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
    rows: list[CaseRow] = []
    # How far into a watch a director first dropped origin-0, if one did.
    dropped: list[float] = []

    def watch(present: bool) -> dict[str, float]:
        """Return how soon each source listed the key, or not (present).

        Each is asked until it does, for KEY_WAIT seconds at most; one
        whose JWKS does not answer, or holds no key, does neither.
        """
        found: dict[str, float] = {}
        started = time.monotonic()
        while time.monotonic() - started < KEY_WAIT and len(found) < len(sources):
            for case, url in sources.items():
                if case in found:
                    continue
                kids = key_ids_at(url)
                if kids and (kid in kids) == present:
                    found[case] = time.monotonic() - started
            if not dropped and len(test.named("origin", host)) != len(fed.directors):
                dropped.append(time.monotonic() - started)
            time.sleep(5)
        return found

    seen = watch(present=True)
    for case in sources:
        if case in seen:
            rows.append((case, PASS, f"the key appeared after {seen[case]:.0f}s"))
        else:
            rows.append((case, FAIL, f"the key never appeared in {KEY_WAIT}s"))
    remove_key(test.origin_dirs, name)
    test.added.remove((test.origin_dirs, name))
    gone = watch(present=False)
    if len(gone) < len(sources):
        still = [case for case in sources if case not in gone]
        rows.append(("removed", FAIL, f"{KEY_WAIT}s after the key was removed, it is still in {still}"))
    else:
        rows.append(("removed", PASS, f"gone within {max(gone.values()):.0f}s"))
    if dropped:
        rows.append(("routing", FAIL, f"a director dropped origin-0 {dropped[0]:.0f}s in"))
    elif fed.directors:
        rows.append(("routing", PASS, f"{len(fed.directors)} director(s) named origin-0 throughout"))
    return rows


# --------------------------------------------------------------------------
# hot-rotate


def hot_rotate(test: Test) -> list[CaseRow]:
    """Check new signing keys on the origins and the first V2 cache.

    The keys are watched for ROTATE_WINDOW seconds, every
    ROTATE_INTERVAL. Every director must keep naming origin-0 and the
    cache (`directors`). Each origin and cache must come to take tokens
    signed with the origins' new key (`new-key`; inconclusive if the
    window ends first, since servers cache a namespace's keys for up to
    15 minutes), and must take the old key's throughout (`old-key`; a
    refusal, by one of REFUSALS, fails it, and any other answer but the
    probe leaves it inconclusive). The issuer's tokens must come to
    carry the new kid (`issuer-kid`). The registry must come to list the
    origins' new key (`registry-origins`) and the cache's
    (`registry-cache`). Tokens of every profile are presented, each
    profile in rows of its own.
    """
    fed = test.fed
    if not fed.registry_url or not fed.directors:
        return [("", SKIP, "a standalone origin has no registry or director")]
    test.put_probe()
    cache_name = test.cache_name()
    old_tokens = {p: test.signed_with(test.origin_key, p) for p in credentials.PROFILES}
    client: issuer.Client | None = None
    admin_web = ""
    if fed.embedded_issuer and not fed.external_issuer:
        admin_web = test.session.web_token(fed.origin_web_url, key=test.origin_key)
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
    new_tokens = {p: test.signed_with(new_origin_key, p) for p in credentials.PROFILES}
    # Under the external issuer, the exports trust none of the origins'
    # keys, so neither key's tokens can show anything.
    data_servers = [] if fed.external_issuer else test.data_servers()
    watched = [("origin-0", "origin", test.origin_host())]
    if test.cache:
        watched.append((test.cache.svc, "object", director.host(test.cache.url)))
    dropped: dict[str, float] = {}
    # By server and profile.
    accepted: dict[tuple[str, str], float] = {}
    refused_old: dict[tuple[str, str], float] = {}
    # The first answer to an old key's token that was neither the probe
    # nor a refusal.
    unanswered_old: dict[tuple[str, str], tuple[float, str]] = {}
    issuer_kid_at: float | None = None
    registry_at: dict[str, float] = {}
    started = time.monotonic()
    while True:
        now = time.monotonic() - started
        for svc, route, host in watched:
            if svc not in dropped and len(test.named(route, host)) != len(fed.directors):
                dropped[svc] = now
        for svc, url in data_servers:
            for profile in credentials.PROFILES:
                key = (svc, profile)
                if key not in accepted and test.served(url, new_tokens[profile]):
                    accepted[key] = now
                if key not in refused_old:
                    got = test.probe(url, old_tokens[profile])
                    if got == REFUSED:
                        refused_old[key] = now
                    elif got != SERVED and key not in unanswered_old:
                        unanswered_old[key] = (now, got)
        if client and issuer_kid_at is None:
            try:
                minted = issuer.token(client, User(token=admin_web), ("storage.read:/",))
                if jwt.header_of(minted).get("kid") == new_kid:
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
    rows: list[CaseRow] = []
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
            rows.append((f"directors {svc}", PASS, f"named by every director for {ROTATE_WINDOW}s"))
    if fed.external_issuer:
        rows.append(("tokens", SKIP, "the exports trust the external issuer, not the origins' keys"))
    for svc, _ in data_servers:
        for profile in credentials.PROFILES:
            key = (svc, profile)
            case = svc if profile == credentials.WLCG else f"{svc} {profile}"
            if key in accepted:
                rows.append(
                    (
                        f"new-key {case}",
                        PASS,
                        f"took the new key's token {accepted[key]:.0f}s in",
                    )
                )
            else:
                rows.append(
                    (
                        f"new-key {case}",
                        INCONCLUSIVE,
                        f"did not take it in {ROTATE_WINDOW}s (servers may cache keys for 15 minutes)",
                    )
                )
            if key in refused_old:
                rows.append(
                    (
                        f"old-key {case}",
                        FAIL,
                        f"refused the old key's token {refused_old[key]:.0f}s in",
                    )
                )
            elif key in unanswered_old:
                at, got = unanswered_old[key]
                rows.append(
                    (
                        f"old-key {case}",
                        INCONCLUSIVE,
                        f"never refused the old key's token, but {at:.0f}s in answered it"
                        + f" with {got}, neither the probe nor a refusal",
                    )
                )
            else:
                rows.append((f"old-key {case}", PASS, "took the old key's token throughout"))
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
        rows.append(("registry-origins", FAIL, f"never listed the origins' new key in {ROTATE_WINDOW}s"))
    if not test.cache:
        rows.append(("registry-cache", SKIP, "no federated V2 cache"))
    elif not cache_name:
        rows.append(("registry-cache", INCONCLUSIVE, f"{test.cache.svc} would not say its name"))
    elif "cache" in registry_at:
        rows.append(
            (
                "registry-cache",
                PASS,
                f"listed the cache's new key {registry_at['cache']:.0f}s in",
            )
        )
    else:
        rows.append(("registry-cache", FAIL, f"never listed the cache's new key in {ROTATE_WINDOW}s"))
    put_back(test)
    return rows


# --------------------------------------------------------------------------
# master-key


def sealed_with(path: str) -> frozenset[str] | None:
    """Return the kids with which the master key file at path is sealed.

    path is under framework/var. The file seals the master key once for
    each kid, under `keys` (local_cache/encryption.go).

    Returns:
        The kids in the file, or None if it is missing or unreadable.
    """
    try:
        with open(path, encoding="utf-8") as f:
            found = common.as_object(json.load(f)) or {}
    except (FileNotFoundError, ValueError):
        return None
    return frozenset(str(k) for k in (common.as_object(found.get("keys")) or {}))


def master_key_path(cache: common.Cache) -> str:
    """Return the master key file of cache, under framework/var."""
    return f"{blocks.cache_dir(cache)}/{MASTER_KEY}"


def pstore_key_path(origin: common.Origin) -> str:
    """Return a pstore origin's master key file, under framework/var."""
    return f"{origin.store}/{PSTORE_MASTER_KEY}"


def sealed_row(case: str, path: str, keys: Sequence[str]) -> CaseRow:
    """Return a row: whether master key file path is sealed with keys.

    keys are the files of private keys, each of whose kids it must hold.
    """
    found = sealed_with(path)
    if found is None:
        return (case, FAIL, "the master key file is missing or unreadable")
    missing = sorted(os.path.basename(k) for k in keys if jwt.key_id(k) not in found)
    if missing:
        return (case, FAIL, f"not sealed with {', '.join(missing)}")
    return (case, PASS, f"sealed with all {len(keys)} keys")


def with_verify_only_key(test: Test, dirs: Sequence[str], whose: str) -> None:
    """Add a verify-only key to dirs, if they hold one key alone.

    Then wait a minute for it to load, as the servers reread their key
    directories every minute.

    Args:
        test: What the scenarios share.
        dirs: The key directories of one keyset.
        whose: Whose keys they are, for a message, e.g. `cache-0's`.
    """
    if len(prefixes_in(dirs[0])) >= 2:
        return
    name = new_key(test, dirs, first_of_all=False)
    test.added.append((list(dirs), name))
    print(f"Added {name} to {whose} keys; waiting a minute for it to load ...")
    time.sleep(65)


def set_aside(test: Test, svc: str, retired: Sequence[str], key_file: str) -> str:
    """Copy the keys that svc is to lose, and its master key file, aside.

    Returns:
        The directory they are copied to, under test.tmp.
    """
    kept = os.path.join(test.tmp, f"kept-{svc}")
    os.makedirs(kept, exist_ok=True)
    for key in retired:
        shutil.copyfile(key, os.path.join(kept, os.path.basename(key)))
    shutil.copyfile(key_file, os.path.join(kept, "masterkey.json"))
    return kept


def put_keys_back(
    svc: str, dirs: Sequence[str], kept: str, retired: Sequence[str], key_file: str
) -> str | None:
    """Put svc's retired keys and master key file back, and start it again.

    It is stopped first.

    Args:
        svc: A V2 cache or a pstore origin.
        dirs: The key directories that the keys were retired from.
        kept: Where set_aside() copied them, and the master key file.
        retired: The retired keys.
        key_file: svc's master key file, under framework/var.

    Returns:
        Why it did not start again, or None if it did.
    """
    try:
        control.stop(svc)
    except control.ControlError as e:
        warn(str(e))
    for key in retired:
        for key_dir in dirs:
            shutil.copyfile(
                os.path.join(kept, os.path.basename(key)),
                os.path.join(key_dir, os.path.basename(key)),
            )
            os.chmod(os.path.join(key_dir, os.path.basename(key)), 0o640)
    shutil.copyfile(os.path.join(kept, "masterkey.json"), key_file)
    try:
        control.start(svc)
    except control.ControlError as e:
        return f"once its keys were put back, {e}; run ./reset.sh if {svc} does not recover"
    return None


def held_with_age(test: Test, cache: common.Cache) -> int | None:
    """Return the Age with which cache serves the probe.

    Returns:
        The Age, or None if cache does not serve the probe from its store.
    """
    answer = web.request("GET", test.url(cache.url, test.rel(PROBE)), token=test.setup, timeout=120)
    if web.served(answer, test.data):
        return None
    return web.age(answer)


def hold_probe(test: Test, cache: common.Cache) -> str | None:
    """Have cache hold the probe, by reading it whole.

    The cache has PROBE_WAIT seconds to serve it, and must then serve it
    with an Age (held_with_age()).

    Returns:
        Why it does not hold it, or None if it does.
    """
    probe = test.rel(PROBE)
    if held_with_age(test, cache) is not None:
        return None

    def read() -> str | None:
        answer = web.request("GET", test.url(cache.url, probe), token=test.setup, timeout=120)
        return web.served(answer, test.data)

    why = common.wait_last(read, lambda found: found is None, PROBE_WAIT, 5)
    if why is not None:
        return f"{PROBE_WAIT}s on, a GET of the probe got {why}"
    if not common.wait_for(lambda: held_with_age(test, cache) is not None, suite.HELD_WAIT, 1):
        return f"no Age within {suite.HELD_WAIT}s of a whole read of the probe"
    return None


def master_key(test: Test) -> list[CaseRow]:
    """Check that a V2 cache's master key is sealed with each of its keys.

    The first V2 cache's master key file must seal the master key with
    each of the cache's keys, a verify-only key the suite adds included,
    before a restart (`sealed`) and after one (`sealed-after-restart`).
    Then, destructively, once the cache holds the probe (hold_probe();
    `retired` is inconclusive if it never does), every key but the
    newest is removed from the cache's key directory. The cache,
    restarted, must still open its store, and serve the probe it held,
    with Age, fetching nothing (`retired`). Afterward the keys and the
    master key file are put back, and the cache restarted again.
    """
    fed = test.fed
    cache = test.cache
    if cache is None:
        return [("", SKIP, "no federated V2 cache")]
    why = control.unavailable()
    if why:
        return [("", SKIP, why)]
    test.put_probe()
    path = master_key_path(cache)
    if sealed_with(path) is None:
        return [("", FAIL, f"framework/var/{path} is missing or unreadable")]
    dirs = key_dirs_of(fed, cache.svc)
    with_verify_only_key(test, dirs, f"{cache.svc}'s")
    keys = sorted(glob.glob(os.path.join(dirs[0], "[0-9][0-9]-*.pem")))
    host = director.host(cache.url)
    rows: list[CaseRow] = [sealed_row("sealed", path, keys)]
    try:
        control.restart(cache.svc)
    except control.ControlError as e:
        return rows + [("sealed-after-restart", FAIL, str(e))]
    why = test.wait_named("object", host)
    if why:
        return rows + [("sealed-after-restart", FAIL, f"after the restart, {why}")]
    rows.append(sealed_row("sealed-after-restart", path, keys))
    why = hold_probe(test, cache)
    if why:
        return rows + [("retired", INCONCLUSIVE, why)]
    # The destructive part: keep the newest key alone.
    newest = keys[-1]
    retired = [k for k in keys if k != newest]
    kept = set_aside(test, cache.svc, retired, path)
    before = metrics.origins_sent_bytes(fed)
    problems: list[str] = []
    try:
        control.stop(cache.svc)
        for key in retired:
            for key_dir in dirs:
                os.remove(os.path.join(key_dir, os.path.basename(key)))
        try:
            control.start(cache.svc)
            why = test.wait_named("object", host)
            if why:
                problems.append(f"once it started with the old keys retired, {why}")
            else:
                age = held_with_age(test, cache)
                if age is None:
                    problems.append("with the old keys retired, it does not serve the probe from its store")
                after = metrics.origins_sent_bytes(fed)
                if before is not None and after is not None and after - before >= PROBE_SIZE:
                    problems.append("with the old keys retired, the origins sent the probe again")
        except control.ControlError as e:
            problems.append(f"with the old keys retired, {e}")
    finally:
        restored = restore_cache(test, cache, dirs, kept, retired)
        if restored:
            problems.append(restored)
    rows.append(("retired", *outcome(problems, f"opened its store with {os.path.basename(newest)} alone")))
    return rows


def restore_cache(
    test: Test, cache: common.Cache, dirs: Sequence[str], kept: str, retired: Sequence[str]
) -> str | None:
    """Put cache's retired keys and master key file back, and restart it.

    That is put_keys_back(); then every director must name it again.

    Args:
        test: What the scenarios share.
        cache: The V2 cache.
        dirs: Its key directories.
        kept: Where the retired keys and the master key file were copied.
        retired: The retired keys, in dirs[0].

    Returns:
        Why that failed, or None if it did not.
    """
    why = put_keys_back(cache.svc, dirs, kept, retired, master_key_path(cache))
    if why:
        return why
    why = test.wait_named("object", director.host(cache.url))
    return f"with its keys put back, {why}" if why else None


# --------------------------------------------------------------------------
# pstore-master-key


def stored_probe(test: Test, origin: common.Origin, key: str | None = None) -> str | None:
    """Return why a pstore origin's storage API does not stat the probe.

    Its admin asks, by a token signed with key, or with the origin's
    signing key (stores.storage_admin()); the answer must give the
    probe's size.

    Returns:
        Why not, or None if it does.
    """
    token = stores.storage_admin(test.session, origin, key)
    found, why = stores.catalog_answer(test.session, origin, "stat", NAMESPACE, test.rel(PROBE), token)
    if found is None:
        return why
    if found.get("size") != PROBE_SIZE:
        return f"the storage API's stat of the probe gave its size as {found.get('size')!r}"
    return None


def pstore_master_key(test: Test) -> list[CaseRow]:
    """Check a pstore origin's master key, as master_key() a V2 cache's.

    The first pstore origin's store is encrypted with a master key that the
    master key file in it seals with each of the origin's keys. Given a
    verify-only key on the origins, which the suite adds if hot-rotate left
    none, the file must seal the master key with every key in the origin's
    key directory, before a restart (`sealed`) and after one
    (`sealed-after-restart`). Then, destructively, every key but the newest
    is removed from the origin's own key directory (a replica keeps them).
    The origin, restarted, must still open its store, keep its master key
    file, and have its storage API stat the probe for its admin, by a token
    signed with that key (`retired`). Afterward the keys and the master key
    file are put back, and the origin restarted again, whose storage API
    must then stat the probe.
    """
    origin = next((o for o in test.fed.origins if o.pstore), None)
    if origin is None:
        return [("", SKIP, "no pstore origin")]
    why = control.unavailable()
    if why:
        return [("", SKIP, why)]
    test.put_probe()
    path = pstore_key_path(origin)
    if sealed_with(path) is None:
        return [("", FAIL, f"framework/var/{path} is missing or unreadable")]
    with_verify_only_key(test, test.origin_dirs, "the origins'")
    own = f"issuer-keys/{origin.svc}"
    keys = sorted(glob.glob(os.path.join(own, "[0-9][0-9]-*.pem")))
    rows: list[CaseRow] = [sealed_row("sealed", path, keys)]
    try:
        control.restart(origin.svc)
    except control.ControlError as e:
        return rows + [("sealed-after-restart", FAIL, str(e))]
    why = stored_probe(test, origin)
    if why:
        return rows + [("sealed-after-restart", FAIL, f"after the restart, {why}")]
    rows.append(sealed_row("sealed-after-restart", path, keys))
    # The destructive part: keep the newest key alone.
    newest = keys[-1]
    retired = [k for k in keys if k != newest]
    sealed = sealed_with(path) or frozenset()
    kept = set_aside(test, origin.svc, retired, path)
    problems: list[str] = []
    try:
        control.stop(origin.svc)
        for key in retired:
            os.remove(os.path.join(own, os.path.basename(key)))
        try:
            control.start(origin.svc)
            why = stored_probe(test, origin, newest)
            if why:
                problems.append(f"with the old keys retired, {why}")
        except control.ControlError as e:
            problems.append(f"with the old keys retired, {e}")
        now = sealed_with(path)
        if now is not None and not now & sealed:
            problems.insert(
                0,
                "with the old keys retired, it replaced its master key with one sealed with"
                + f" {os.path.basename(newest)} alone",
            )
    finally:
        why = put_keys_back(origin.svc, [own], kept, retired, path)
        if why is None and (unstored := stored_probe(test, origin)):
            why = (
                f"once its keys were put back, {unstored}; run ./reset.sh if {origin.svc}"
                + " does not recover"
            )
        if why:
            problems.append(why)
    rows.append(("retired", *outcome(problems, f"opened its store with {os.path.basename(newest)} alone")))
    return rows


# --------------------------------------------------------------------------
# backups


def services_of(fed: common.Federation) -> list[tuple[str, str]]:
    """Return each Pelican server's service, and its key directory."""
    found = [(o.svc, f"issuer-keys/{o.svc}") for o in fed.origins]
    found += [(o.origin.svc, o.key_dir) for o in fed.owners]
    found += [(c.svc, f"issuer-keys/{c.svc}") for c in fed.caches]
    found += [(director.host(u), f"issuer-keys/{director.host(u)}") for u in fed.directors]
    if fed.registry_url:
        host = director.host(fed.registry_url)
        found.append((host, f"issuer-keys/{host}"))
    return found


def backups(test: Test) -> list[CaseRow]:
    """Check each server's backups of its database.

    They are under framework/var/state/<service>/backups/
    (Server.DatabaseBackup.Location). There must be at least one, since
    a server with none makes one as it starts. Each must be PEM, never a
    plain SQLite file, and open with `pelican-server server database
    backup verify`, given a copy of the server's keys; without a
    pelican-server, that check is left out.
    """
    rows: list[CaseRow] = []
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
        if test.session.pelican_server is None:
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
        # The command resolves the federation's discovery as it starts,
        # which the dev container's configuration names for every
        # federation but a standalone origin's own (see
        # common.Federation.discovery_url).
        if test.fed.standalone:
            env["PELICAN_FEDERATION_DISCOVERYURL"] = test.fed.discovery_url
        verified = 0
        for path in found:
            status, out, err = test.session.server_cmd(
                "server",
                "database",
                "backup",
                "verify",
                os.path.abspath(path),
                env=env,
                cwd=test.tmp,
                timeout=120,
            )
            if status != 0:
                problems.append(f"verify {os.path.basename(path)}: exit {status}: {last_line(err or out)}")
                break
            verified += 1
        rows.append((svc, *outcome(problems, f"{verified} encrypted backup(s) verified")))
    return rows


# --------------------------------------------------------------------------

RUN: dict[str, Callable[[Test], list[CaseRow]]] = {
    "hot-add": hot_add,
    "hot-rotate": hot_rotate,
    "master-key": master_key,
    "pstore-master-key": pstore_master_key,
    "backups": backups,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, then put the keys back.

    The run's objects are removed too.
    """
    test = Test(session)
    print(f"Keys against {session.fed.url}, the probe at {test.path(test.rel(PROBE))}\n")
    try:
        for name in selected:
            suite.record(results, name, suite.attempt_cases(functools.partial(RUN[name], test)))
    finally:
        put_back(test)
        test.clean("after the run")
        shutil.rmtree(test.tmp, ignore_errors=True)
