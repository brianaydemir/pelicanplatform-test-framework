"""Paths, messages, arguments, and what fed.sh derived from the shape.

They are for test.py, its suites, and init-data.py, which run in
framework/var (see enter_var), so the paths they use are relative to it.
Messages name paths from the top of the checkout.
"""

import fnmatch
import glob
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import Any, NoReturn, Optional, cast
from urllib.parse import urlsplit

from . import report as report_module

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.path.join(FRAMEWORK, "var")

PROG = os.path.basename(sys.argv[0])

# smoke.sh logs what test.py prints; keep stdout in step with stderr.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportUnknownMemberType]


class Died(SystemExit):
    """What die() raises: test.py records it as the suite's end."""

    def __init__(self, message: str, status: int):
        super().__init__(status)
        self.message = message


def die(message: str) -> NoReturn:
    """Print message, and end the suite, or the program.

    Raises:
        Died: Always, with exit status 1.
    """
    print(f"{PROG}: {message}", file=sys.stderr)
    raise Died(message, 1)


def die_usage(message: str) -> NoReturn:
    """Print message, and end the suite or the program, for a usage error.

    Raises:
        Died: Always, with exit status 2.
    """
    print(f"{PROG}: {message}", file=sys.stderr)
    raise Died(message, 2)


def warn(message: str) -> None:
    """Print message as a warning."""
    print(f"{PROG}: warning: {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# Choosing what to run: test.py's arguments.


def select(args: Sequence[str], suites: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Return the scenarios that args ask for, by suite.

    Args:
        args: Suites (e.g. `auth`), or `<suite>/<scenario>`; either part
            may be a shell-style pattern (e.g. 'transfers/plugin-*').
            Empty means every scenario.
        suites: Each suite's scenarios, in the order they run.

    Returns:
        Each suite that args name, in the order of suites, with its
        scenarios in their order, each once.

    Raises:
        Died: An argument matches nothing (exit status 2).
    """
    wanted: dict[str, set[str]] = {}
    for arg in args:
        suite_part, sep, scenario_part = arg.partition("/")
        matched = fnmatch.filter(list(suites), suite_part)
        if not matched:
            die_usage(f"no such suite: {suite_part} (see -l)")
        found = False
        for suite in matched:
            names = fnmatch.filter(suites[suite], scenario_part) if sep else list(suites[suite])
            if names:
                found = True
                wanted.setdefault(suite, set()).update(names)
        if not found:
            die_usage(f"no scenario matches {arg} (see -l)")
    if not args:
        return {suite: list(names) for suite, names in suites.items()}
    return {
        suite: [n for n in names if n in wanted[suite]]
        for suite, names in suites.items()
        if suite in wanted
    }


# --------------------------------------------------------------------------
# framework/var and the Pelican binaries in it.


def as_object(value: Any) -> Optional[dict[str, Any]]:
    """Return value, a parsed JSON value, if it is an object; else None."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def as_array(value: Any) -> Optional[list[Any]]:
    """Return value, a parsed JSON value, if it is an array; else None."""
    if not isinstance(value, list):
        return None
    # mypy takes value for a list[Any] here, and so would call a cast
    # redundant.
    items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
    return items


def objects(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Return the JSON objects among items, parsed JSON values."""
    return [found for found in (as_object(item) for item in items) if found is not None]


def enter_var() -> None:
    """Change to framework/var."""
    try:
        os.chdir(VAR)
    except OSError:
        die("framework/var is missing; run ./fed.sh init on the host")


def binary(name: str) -> str:
    """Return the path of framework/var/bin/<os>/<name>; it must exist."""
    path = os.path.join("bin", platform.system().lower(), name)
    if not os.path.exists(path):
        die(f"framework/var/{path} is missing; run ./fed.sh init on the host")
    return path


def server_binary() -> Optional[str]:
    """Return the pelican-server to run server-only commands with.

    The client lacks those commands.

    Returns:
        The container's own, if it has one; otherwise
        framework/var/bin/<os>/pelican-server, if there is one; otherwise
        None.
    """
    found = shutil.which("pelican-server")
    if found is not None:
        return found
    path = os.path.join("bin", platform.system().lower(), "pelican-server")
    return path if os.path.exists(path) else None


def digest(data: bytes) -> str:
    """Return the SHA-256 of data."""
    return hashlib.sha256(data).hexdigest()


def sha256(path: str) -> Optional[str]:
    """Return the file's SHA-256, or None if there is no such file."""
    found = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                found.update(chunk)
    except FileNotFoundError:
        return None
    return found.hexdigest()


def wait_for(condition: Callable[[], bool], seconds: float, interval: float = 0.5) -> bool:
    """Return whether condition holds within seconds.

    It asks every interval.
    """
    deadline = time.time() + seconds
    while not condition():
        if time.time() >= deadline:
            return False
        time.sleep(interval)
    return True


def shown(path: Optional[str]) -> str:
    """Return how a message names path: one under framework/var, or a URL.

    Returns:
        The name; "" if there is no path.
    """
    if not path:
        return ""
    return path if "://" in path else f"framework/var/{path}"


def first(problems: Sequence[str]) -> str:
    """Return the first of problems, and how many more there are."""
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    return problems[0] + more


@dataclass
class Findings:
    """What a case found: problems, doubts, and notes.

    Attributes:
        problems: Problems, which fail the case.
        doubts: Doubts, which leave it inconclusive.
        notes: Notes on what went right.
    """

    problems: list[str] = field(default_factory=list[str])
    doubts: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])

    def row(self, passed: str) -> tuple[str, str]:
        """Return the case's status, and a note on it.

        Args:
            passed: The note's start, if the case passed.

        Returns:
            FAIL, with the first problem; INCONCLUSIVE, with the doubts; or
            PASS, with passed and the notes.
        """
        if self.problems:
            return report_module.FAIL, first(self.problems)
        if self.doubts:
            return report_module.INCONCLUSIVE, "; ".join(self.doubts)
        return report_module.PASS, "; ".join([passed] + self.notes)


def timestamp(when: datetime) -> str:
    """Return when, a UTC time, as Go's encoding/json reads a time.Time.

    That is RFC 3339, to the second.
    """
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_dir(path: str) -> None:
    """Remove everything in directory path, but not path itself."""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))


# --------------------------------------------------------------------------
# What fed.sh derived from the shape: framework/var/generated/.


def storage_namespace(namespace: str, pstore: bool) -> str:
    """Return the namespace whose storage namespace reads at an origin.

    It is namespace's own (e.g. protected-a), but on a pstore origin,
    /public reads /protected-a's, since nothing can write /public
    (fed_storage_dir in fed.sh).
    """
    return "protected-a" if pstore and namespace == "public" else namespace


def storage_dir(namespace: str) -> str:
    """Return the directory of a namespace's storage in an origin's store.

    It is the namespace's name, e.g. protected-a, with any / of a nested
    one as -, e.g. public-other for /public/other (fed_storage_dir in
    fed.sh).
    """
    return namespace.strip("/").replace("/", "-")


def web_url_of(data_url: str) -> str:
    """Return the web URL of the server that serves data at data_url.

    That is where its web API is: every server's web port is 8444
    (config.d/base/20-server.yaml), whatever port its data is on.
    """
    return f"https://{urlsplit(data_url).hostname}:8444"


@dataclass(frozen=True)
class Origin:
    """A running origin.

    Attributes:
        svc: The compose service.
        url: Where it serves its exports.
        store: Its store, a directory under framework/var (see store_of()).
        kind: "pstore" if the store is encrypted, and `store` holds its
            plain copy.
    """

    svc: str
    url: str
    store: str
    kind: str

    @property
    def pstore(self) -> bool:
        """Whether its store is a pstore."""
        return self.kind == "pstore"

    @property
    def web_url(self) -> str:
        """Its web URL (see web_url_of())."""
        return web_url_of(self.url)

    def storage_namespace(self, namespace: str) -> str:
        """Return the namespace whose storage namespace reads here.

        See storage_namespace().
        """
        return storage_namespace(namespace, self.pstore)

    def store_of(self, namespace: str) -> str:
        """Return the directory of namespace's storage in the store.

        It is under framework/var; on a pstore origin, it is of the store's
        plain copy.
        """
        return f"{self.store}/{storage_dir(self.storage_namespace(namespace))}"


@dataclass(frozen=True)
class Export:
    """An export of another owner's origin (see Owner).

    Attributes:
        prefix: Its federation prefix, e.g. /public/other.
        like: The namespace it mirrors, e.g. `public`; `-` for none
            (/public/secret).
        caps: Its capabilities.
        issuer: The issuer URL it has, or would have.
    """

    prefix: str
    like: str
    caps: frozenset[str]
    issuer: str

    @property
    def path(self) -> str:
        """Its prefix without the slashes around it, e.g. public/other.

        It is for URLs like <origin>/<path>/<rel>.
        """
        return self.prefix.strip("/")

    @property
    def name(self) -> str:
        """How rows and files name it, e.g. public-other."""
        return self.path.replace("/", "-")


@dataclass(frozen=True)
class Owner:
    """A running origin of another owner (`topo-multi-owner`).

    It has a key and an issuer of its own, and exports of its own.

    Attributes:
        key_dir: The directory (under framework/var) of its issuer keys.
        web_url: Where it serves its web API, e.g. its server-wide JWKS.
    """

    origin: Origin
    key_dir: str
    web_url: str
    exports: tuple[Export, ...]

    @property
    def key(self) -> str:
        """Its active signing key."""
        return signing_key(self.key_dir)


def signing_key(key_dir: str) -> str:
    """Return a server's active signing key.

    It is the .pem in key_dir (under framework/var) that sorts first, as
    Pelican picks it.
    """
    keys = sorted(glob.glob(os.path.join(key_dir, "*.pem")))
    if not keys:
        die(f"no key in framework/var/{key_dir}/; run ./fed.sh init")
    return keys[0]


def parse_owners(owners: str, exports: str) -> list[Owner]:
    """Parse generated/owners, with each owner's lines of owner-exports.

    Args:
        owners: generated/owners:
            `<service> <URL> <store> <key dir> <web URL>` per line.
        exports: generated/owner-exports:
            `<service> <prefix> <namespace> <capability>,... <issuer URL>`
            per line.
    """
    found: dict[str, list[Export]] = {}
    for line in exports.splitlines():
        if line.strip():
            svc, prefix, like, caps, issuer = line.split()
            found.setdefault(svc, []).append(
                Export(prefix, like, frozenset(caps.split(",")), issuer)
            )
    result: list[Owner] = []
    for line in owners.splitlines():
        if line.strip():
            svc, url, store, key_dir, web_url = line.split()
            result.append(
                Owner(Origin(svc, url, store, ""), key_dir, web_url, tuple(found.get(svc, ())))
            )
    return result


@dataclass(frozen=True)
class Cache:
    """A running cache.

    Attributes:
        svc: The compose service.
        url: Its data URL.
        kind: "v2" (native) or "xrootd".
        role: "federated", or "site-local": the director does not know of
            it.
    """

    svc: str
    url: str
    kind: str
    role: str

    @property
    def site_local(self) -> bool:
        """Whether it is a site-local cache, which a client must name."""
        return self.role == "site-local"

    @property
    def web_url(self) -> str:
        """Its web URL (see web_url_of())."""
        return web_url_of(self.url)


@dataclass(frozen=True)
class IssuerLifetimes:
    """How long the embedded issuer's tokens last, in seconds.

    Attributes:
        access: Issuer.AccessTokenLifetime.
        refresh: Issuer.RefreshTokenLifetime.
        grace: Issuer.RefreshTokenGracePeriod: how long a used refresh
            token still works.
        code: Issuer.AuthorizationCodeLifetime: authorization and device
            codes.
    """

    access: int
    refresh: int
    grace: int
    code: int


# The origins that XRootD serves (ORIGIN_VARIANT); the rest are Pelican's
# native origins.
XROOTD_ORIGINS = ("posix", "https", "s3")


@dataclass(frozen=True)
class TransferApi:
    """Where the transfer API is served.

    It is origin-0's (`-p origin-transfer-api`), or a transfer server's
    own (`-p topo-transfer-server`).

    Attributes:
        url: Its web URL.
        issuer: The issuer of its tokens.
        key_dir: The directory (under framework/var) of the key that signs
            them.
        svc: The compose service that serves it.
        confined: Whether it confines jobs to the origin's exports.
    """

    url: str
    issuer: str
    key_dir: str
    svc: str
    confined: bool


@dataclass(frozen=True)
class TierTarget:
    """Where a V2 cache tiers objects (`-p cache-tiered`).

    Attributes:
        svc: The cache's compose service.
        mode: "redirect": it sends clients to the target; or "proxy".
        threshold: The size, in bytes, from which it tiers an object.
        directory: The target's directory, under framework/var.
    """

    svc: str
    mode: str
    threshold: int
    directory: str


class Federation:
    """framework/var/generated/, read as needed.

    fed.sh documents each file where it writes them (fed_generate).
    """

    def _read(self, name: str) -> str:
        path = os.path.join("generated", name)
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            die(f"framework/var/{path} is missing; run ./fed.sh init")

    def _line(self, name: str) -> str:
        return self._read(name).strip()

    @cached_property
    def url(self) -> str:
        """The federation's URL, e.g. pelican://discovery:8444."""
        return self._line("federation-url")

    @cached_property
    def issuers(self) -> dict[str, str]:
        """The issuer URL that the origins give each namespace, by name.

        Or that they would give it: /public has no issuer, and the shape
        may not export every namespace. `<namespace> <URL>` per line.
        """
        found: dict[str, str] = {}
        for line in self._read("issuer-urls").splitlines():
            if line.strip():
                namespace, url = line.split()
                found[namespace] = url
        return found

    def issuer_of(self, namespace: str) -> str:
        """Return the issuer URL of namespace, e.g. `protected-a`."""
        return self.issuers[namespace]

    @cached_property
    def external_issuer(self) -> bool:
        """EXTERNAL_ISSUER: whether the exports trust the external issuer.

        That is the federation's, instead of the origins' (see
        presets/auth-external-issuer.sh).
        """
        return self._line("external-issuer") == "true"

    @cached_property
    def embedded_issuer(self) -> bool:
        """ORIGIN_ENABLE_ISSUER: whether the origins' embedded issuer runs.

        It is Pelican's embedded OAuth2 issuer, which gives each protected
        namespace an issuer of its own (see presets/default.sh).
        """
        return self._line("origin-enable-issuer") == "true"

    @cached_property
    def origin_web_url(self) -> str:
        """The web URL of the origin whose issuer the namespaces name.

        It serves the server-wide JWKS.
        """
        return self._line("origin-web-url")

    @cached_property
    def exports(self) -> dict[str, frozenset[str]]:
        """Each exported namespace's capabilities, by name.

        Names are like `protected-a`. `<prefix> <capability>,...` per line.
        """
        found: dict[str, frozenset[str]] = {}
        for line in self._read("exports").splitlines():
            if line.strip():
                prefix, caps = line.split()
                found[prefix.strip("/")] = frozenset(caps.split(","))
        return found

    @cached_property
    def origin_key(self) -> str:
        """The origins' active signing key (see signing_key())."""
        return signing_key(self._line("origin-key-dir"))

    @cached_property
    def origins(self) -> list[Origin]:
        """One per running origin: `<service> <URL> <store> [pstore]`."""
        rows = [line.split() for line in self._read("origins").splitlines() if line.strip()]
        return [Origin(r[0], r[1], r[2], r[3] if len(r) > 3 else "") for r in rows]

    @cached_property
    def owners(self) -> list[Owner]:
        """The running origins of other owners (see parse_owners())."""
        return parse_owners(self._read("owners"), self._read("owner-exports"))

    @cached_property
    def caches(self) -> list[Cache]:
        """One per running cache: `<service> <URL> <kind> <role>`."""
        rows = [line.split() for line in self._read("caches").splitlines() if line.strip()]
        return [Cache(*r) for r in rows]

    @property
    def federated_caches(self) -> list[Cache]:
        """The caches that the directors know of."""
        return [c for c in self.caches if not c.site_local]

    @cached_property
    def origin_variant(self) -> str:
        """ORIGIN_VARIANT: a directory under framework/config.d/origin/."""
        return self._line("origin-variant")

    @property
    def native_origins(self) -> bool:
        """Whether Pelican serves the origins itself, rather than XRootD.

        See origin_serve/ in Pelican, and XROOTD_ORIGINS.
        """
        return self.origin_variant not in XROOTD_ORIGINS

    @property
    def sent_cache_control(self) -> str:
        """The Cache-Control that the origins send.

        It is ORIGIN_CACHE_CONTROL (origin_cache_control) at native
        origins, and nothing at XRootD ones, which have no
        Origin.CacheControl.
        """
        return self.origin_cache_control if self.native_origins else ""

    def storage_namespace(self, namespace: str) -> str:
        """Return the namespace whose storage namespace reads at an origin.

        The origins are all of one ORIGIN_VARIANT (see storage_namespace()).
        """
        return storage_namespace(namespace, self.origin_variant == "pstore")

    @cached_property
    def posc(self) -> bool:
        """ORIGIN_POSC (see presets/origin-posc.sh)."""
        return self._line("origin-posc") == "true"

    @cached_property
    def atomic_uploads(self) -> bool:
        """ORIGIN_ATOMIC_UPLOADS (see presets/origin-atomic-uploads.sh)."""
        return self._line("origin-atomic-uploads") == "true"

    @cached_property
    def origin_cache_control(self) -> str:
        """ORIGIN_CACHE_CONTROL: the Cache-Control that native origins send.

        It is "" for none (see presets/origin-max-age.sh).
        """
        return self._line("origin-cache-control")

    @cached_property
    def multiuser(self) -> bool:
        """ORIGIN_MULTIUSER (see presets/origin-multiuser.sh)."""
        return self._line("origin-multiuser") == "true"

    @cached_property
    def drop_privileges(self) -> bool:
        """SERVER_DROP_PRIVILEGES (see presets/server-unprivileged.sh)."""
        return self._line("server-drop-privileges") == "true"

    @cached_property
    def discovery(self) -> dict[str, Any]:
        """The federation's discovery document, as fed.sh expects it."""
        return as_object(json.loads(self._read("discovery.json"))) or {}

    @cached_property
    def directors(self) -> list[str]:
        """Every director's URL, from the federation's discovery document.

        There are none for a standalone origin.
        """
        found = self.discovery
        urls = as_array(found.get("director_advertise_endpoints")) or [
            found["director_endpoint"]
        ]
        return [str(url) for url in urls if url]

    @cached_property
    def director_url(self) -> str:
        """The director's URL that clients use.

        It comes from the federation's discovery document. Under
        `-p director-ha`, it is the load balancer's; it is "" for a
        standalone origin.
        """
        return str(self.discovery.get("director_endpoint") or "")

    @cached_property
    def director_ha(self) -> bool:
        """DIRECTOR_HA: whether the directors sit behind a load balancer.

        It is at director_url, and each director has the shared name as
        its external URL (see presets/director-ha.sh).
        """
        return self._line("director-ha") == "true"

    @property
    def registry_url(self) -> str:
        """The registry's web URL, from the federation's discovery document.

        It is "" for a standalone origin, which has none.
        """
        return str(self.discovery.get("namespace_registration_endpoint") or "")

    @cached_property
    def director_default_response(self) -> str:
        """DIRECTOR_DEFAULT_RESPONSE: `cache` or `origin`.

        It is where a director sends a GET of a bare path (see
        presets/director-default-origin.sh).
        """
        return self._line("director-default-response")

    @cached_property
    def registry_approval(self) -> bool:
        """REGISTRY_REQUIRE_APPROVAL: whether registrations need approval.

        If so, a registration serves nothing until an administrator
        approves it (see presets/registry-approval.sh).
        """
        return self._line("registry-approval") == "true"

    @cached_property
    def issuer_lifetimes(self) -> IssuerLifetimes:
        """How long the origins' embedded issuer's tokens last.

        `<access> <refresh> <grace> <code>` seconds (see
        presets/origin-short-tokens.sh).
        """
        access, refresh, grace, code = self._line("issuer-lifetimes").split()
        return IssuerLifetimes(int(access), int(refresh), int(grace), int(code))

    @cached_property
    def export_templates(self) -> bool:
        """ORIGIN_EXPORT_TEMPLATES (see presets/origin-export-templates.sh).

        Whether the /protected-b export has authorization templates of its
        own.
        """
        return self._line("export-templates") == "true"

    @cached_property
    def metadata(self) -> str:
        """`off`, `eventual`, or `transactional`."""
        return self._line("origin-metadata")

    @cached_property
    def broker(self) -> bool:
        """ORIGIN_BROKER (see presets/origin-broker.sh)."""
        return self._line("origin-broker") == "true"

    @cached_property
    def transfer_api(self) -> Optional[TransferApi]:
        """Where the transfer API is served, if it is (see TransferApi).

        `<URL> <issuer> <key dir> <service> <exports|none>`, or `off`.
        """
        fields = self._line("transfer-api").split()
        if fields == ["off"]:
            return None
        url, issuer, key_dir, svc, confined = fields
        return TransferApi(url, issuer, key_dir, svc, confined == "exports")

    @cached_property
    def tiering(self) -> dict[str, TierTarget]:
        """Where each cache tiers objects (`-p cache-tiered`), by service.

        `<service> <mode> <threshold> <directory>` per line, none if off.
        """
        found: dict[str, TierTarget] = {}
        for line in self._read("tiering").splitlines():
            if line.strip():
                svc, mode, threshold, directory = line.split()
                found[svc] = TierTarget(svc, mode, int(threshold), directory)
        return found

    @cached_property
    def eviction(self) -> dict[str, tuple[int, int]]:
        """Each bounded cache's water marks, by service (`-p cache-small`).

        They are the high water mark, in bytes, at which its store evicts,
        and the low one, down to which it does. `<service> <high> <low>`
        per line, none if the stores are unbounded.
        """
        found: dict[str, tuple[int, int]] = {}
        for line in self._read("eviction").splitlines():
            if line.strip():
                svc, high, low = line.split()
                found[svc] = (int(high), int(low))
        return found

    @cached_property
    def oidc_issuer(self) -> Optional[str]:
        """The OIDC issuer URL of the identity provider (`-p auth-oidc`).

        The servers send their users to log in through it. It is None if
        the shape runs none (see presets/auth-oidc.sh).
        """
        found = self._line("oidc")
        return None if found == "off" else found

    @cached_property
    def pelican_version(self) -> Optional[str]:
        """The version that every server should report.

        It is PELICAN_TAG's, without the `v`; None if the shape runs images
        of the user's own, whose versions no one knows in advance.
        """
        found = self._line("pelican-version")
        return None if found == "custom" else found

    @cached_property
    def topology(self) -> str:
        """TOPOLOGY: `full`, `tiny`, or `standalone`."""
        return self._line("topology")

    @property
    def tiny(self) -> bool:
        """The `tiny` topology, where one host is everything."""
        return self.topology == "tiny"

    @property
    def standalone(self) -> bool:
        """The `standalone` topology: one origin, no director or cache."""
        return self.topology == "standalone"


# --------------------------------------------------------------------------
# A result per scenario, for the suites that run scenarios one by one.


class Results:
    """Rows of `<scenario> <status> <note>` for one suite.

    The statuses are report.STATUSES. Rows are printed briefly as they
    come and in full by print(), and recorded in the suite's report. A
    suite that prints rows its own way records them in the report
    directly.
    """

    def __init__(self, names: Sequence[str], report: report_module.Report):
        self.name_width = max((len(n) for n in names), default=0)
        self.status_width = max(len(s) for s in report_module.STATUSES)
        self.rows: list[str] = []
        self.report = report

    @property
    def failed(self) -> int:
        """How many rows failed."""
        return self.report.failed

    def add(self, scenario: str, status: str, note: str = "") -> None:
        """Record a row, and print it briefly."""
        self.report.add(scenario, status, note)
        row = f"{scenario:<{self.name_width}} {status:<{self.status_width}} {note}"
        self.rows.append(row.rstrip())
        print(f"  {scenario:<{self.name_width}} {status}")

    def print(self) -> None:
        """Print every row in full."""
        if not self.rows:
            return
        print()
        for row in self.rows:
            print(row)
