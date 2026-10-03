"""Paths, messages, the command line, and what fed.sh derived from the
shape, for test.py, its suites, and init-data.py.

They run in framework/var (see enter_var), so the paths they use are
relative to it. Messages name paths from the top of the checkout.
"""

import fnmatch
import glob
import hashlib
import io
import json
import os
import platform
import sys
import time
from dataclasses import dataclass
from functools import cached_property
from typing import (Callable, Dict, FrozenSet, List, Mapping, NoReturn, Optional, Sequence, Set,
                    Tuple)

from . import report as report_module

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.path.join(FRAMEWORK, "var")

PROG = os.path.basename(sys.argv[0])

# smoke.sh logs what test.py prints; keep stdout in step with stderr.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(line_buffering=True)


class Died(SystemExit):
    """What die() raises: test.py records it as the suite's end."""

    def __init__(self, message: str, status: int):
        super().__init__(status)
        self.message = message


def die(message: str) -> NoReturn:
    print(f"{PROG}: {message}", file=sys.stderr)
    raise Died(message, 1)


def die_usage(message: str) -> NoReturn:
    print(f"{PROG}: {message}", file=sys.stderr)
    raise Died(message, 2)


def warn(message: str) -> None:
    print(f"{PROG}: warning: {message}", file=sys.stderr)


#---------------------------------------------------------------------------
# Choosing what to run: test.py's arguments.

def select(args: Sequence[str], suites: Mapping[str, Sequence[str]]) -> Dict[str, List[str]]:
    """The scenarios that args ask for, by suite: each suite in the order
    of suites, and its scenarios in their order, each once. An argument
    is a suite (e.g. `auth`), or `<suite>/<scenario>`; either part may be
    a shell-style pattern (e.g. 'transfers/plugin-*'). No args means
    every scenario. An argument that matches nothing exits 2."""
    wanted: Dict[str, Set[str]] = {}
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
    return {suite: [n for n in names if n in wanted[suite]]
            for suite, names in suites.items() if suite in wanted}


#---------------------------------------------------------------------------
# framework/var and the Pelican binaries in it.

def enter_var() -> None:
    try:
        os.chdir(VAR)
    except OSError:
        die("framework/var is missing; run ./fed.sh init on the host")


def binary(name: str) -> str:
    """The path of framework/var/bin/<os>/<name>, which must exist."""
    path = os.path.join("bin", platform.system().lower(), name)
    if not os.path.exists(path):
        die(f"framework/var/{path} is missing; run ./fed.sh init on the host")
    return path


def digest(data: bytes) -> str:
    """The SHA-256 of data."""
    return hashlib.sha256(data).hexdigest()


def sha256(path: str) -> Optional[str]:
    """The file's SHA-256, or None if there is no such file."""
    found = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                found.update(chunk)
    except FileNotFoundError:
        return None
    return found.hexdigest()


def wait_for(condition: Callable[[], bool], seconds: float, interval: float = 0.5) -> bool:
    """Whether condition holds within seconds, asking every interval."""
    deadline = time.time() + seconds
    while not condition():
        if time.time() >= deadline:
            return False
        time.sleep(interval)
    return True


def shown(path: Optional[str]) -> str:
    """How a message names path, which is under framework/var, or a URL;
    "" for none."""
    if not path:
        return ""
    return path if "://" in path else f"framework/var/{path}"


def first(problems: Sequence[str]) -> str:
    """The first of problems, and how many more there are."""
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    return problems[0] + more


def empty_dir(path: str) -> None:
    """Remove everything in directory path, but not path itself."""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))


#---------------------------------------------------------------------------
# What fed.sh derived from the shape: framework/var/generated/.

@dataclass(frozen=True)
class Origin:
    svc: str    # the compose service
    url: str    # where it serves its exports
    store: str  # the directory (under framework/var) that is its StoragePrefix
    kind: str   # "pstore" if the store is encrypted, and `store` is its plain copy

    @property
    def pstore(self) -> bool:
        return self.kind == "pstore"


@dataclass(frozen=True)
class Export:
    """An export of another owner's origin (see Owner)."""
    prefix: str             # its federation prefix, e.g. /public/other
    like: str               # the namespace it mirrors, e.g. `public`
    caps: FrozenSet[str]    # its capabilities
    issuer: str             # the issuer URL it has, or would have

    @property
    def path(self) -> str:
        """Its prefix without the slashes around it, e.g. public/other,
        for URLs like <origin>/<path>/<rel>."""
        return self.prefix.strip("/")

    @property
    def name(self) -> str:
        """How rows and files name it, e.g. public-other."""
        return self.path.replace("/", "-")


@dataclass(frozen=True)
class Owner:
    """A running origin of another owner (`topo-multi-owner`), with a key
    and an issuer of its own, and exports of its own."""
    origin: Origin
    key_dir: str    # the directory (under framework/var) of its issuer keys
    web_url: str    # where it serves its web API, e.g. its server-wide JWKS
    exports: Tuple[Export, ...]

    @property
    def key(self) -> str:
        """Its active signing key."""
        return signing_key(self.key_dir)


def signing_key(key_dir: str) -> str:
    """A server's active signing key: the .pem in key_dir (under
    framework/var) that sorts first, as Pelican picks it."""
    keys = sorted(glob.glob(os.path.join(key_dir, "*.pem")))
    if not keys:
        die(f"no key in framework/var/{key_dir}/; run ./fed.sh init")
    return keys[0]


def parse_owners(owners: str, exports: str) -> List[Owner]:
    """generated/owners, `<service> <URL> <store> <key dir> <web URL>` per
    line, with each owner's lines of generated/owner-exports,
    `<service> <prefix> <namespace> <capability>,... <issuer URL>`."""
    found: Dict[str, List[Export]] = {}
    for line in exports.splitlines():
        if line.strip():
            svc, prefix, like, caps, issuer = line.split()
            found.setdefault(svc, []).append(Export(prefix, like, frozenset(caps.split(",")),
                                                    issuer))
    result = []
    for line in owners.splitlines():
        if line.strip():
            svc, url, store, key_dir, web_url = line.split()
            result.append(Owner(Origin(svc, url, store, ""), key_dir, web_url,
                                tuple(found.get(svc, ()))))
    return result


@dataclass(frozen=True)
class Cache:
    svc: str   # the compose service
    url: str   # its data URL
    kind: str  # "v2" (native) or "xrootd"


class Federation:
    """framework/var/generated/, read as needed. fed.sh documents each
    file where it writes them (fed_generate)."""

    def _read(self, name: str) -> str:
        path = os.path.join("generated", name)
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            die(f"framework/var/{path} is missing; run ./fed.sh init")

    def _line(self, name: str) -> str:
        return self._read(name).strip()

    @cached_property
    def url(self) -> str:
        return self._line("federation-url")

    @cached_property
    def issuers(self) -> Dict[str, str]:
        """The issuer URL the origins give each namespace, by name, or
        would give it: /public has no issuer, and the shape may not export
        every namespace. `<namespace> <URL>` per line."""
        found: Dict[str, str] = {}
        for line in self._read("issuer-urls").splitlines():
            if line.strip():
                namespace, url = line.split()
                found[namespace] = url
        return found

    def issuer_of(self, namespace: str) -> str:
        """The issuer URL of namespace, e.g. `protected-a`."""
        return self.issuers[namespace]

    @cached_property
    def external_issuer(self) -> bool:
        """EXTERNAL_ISSUER: whether the exports trust the federation's
        external issuer instead of the origins' (see
        presets/auth-external-issuer.sh)."""
        return self._line("external-issuer") == "true"

    @cached_property
    def origin_web_url(self) -> str:
        """The web URL of the origin whose issuer the namespaces name, which
        serves the server-wide JWKS."""
        return self._line("origin-web-url")

    @cached_property
    def exports(self) -> Dict[str, FrozenSet[str]]:
        """Each exported namespace's capabilities, by name (e.g.
        `protected-a`): `<prefix> <capability>,...` per line."""
        found: Dict[str, FrozenSet[str]] = {}
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
    def origins(self) -> List[Origin]:
        """One per running origin: `<service> <URL> <store> [pstore]`."""
        rows = [line.split() for line in self._read("origins").splitlines() if line.strip()]
        return [Origin(r[0], r[1], r[2], r[3] if len(r) > 3 else "") for r in rows]

    @cached_property
    def owners(self) -> List[Owner]:
        """The running origins of other owners, if any (see parse_owners())."""
        return parse_owners(self._read("owners"), self._read("owner-exports"))

    @cached_property
    def caches(self) -> List[Cache]:
        """One per running cache: `<service> <URL> <kind>`."""
        rows = [line.split() for line in self._read("caches").splitlines() if line.strip()]
        return [Cache(r[0], r[1], r[2]) for r in rows]

    @cached_property
    def origin_variant(self) -> str:
        """ORIGIN_VARIANT: a directory under framework/config.d/origin/."""
        return self._line("origin-variant")

    @cached_property
    def posc(self) -> bool:
        return self._line("origin-posc") == "true"

    @cached_property
    def origin_cache_control(self) -> str:
        """ORIGIN_CACHE_CONTROL: the Cache-Control that the origins send,
        or "" for none (see presets/origin-max-age.sh)."""
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
    def directors(self) -> List[str]:
        """Every director's URL, from the federation's discovery
        document."""
        found = json.loads(self._read("discovery.json"))
        return found.get("director_advertise_endpoints") or [found["director_endpoint"]]

    @cached_property
    def metadata(self) -> str:
        """`off`, `eventual`, or `transactional`."""
        return self._line("origin-metadata")

    @property
    def tiny(self) -> bool:
        """The `tiny` topology, where one host is everything."""
        return self.url.startswith("pelican://fed:")


#---------------------------------------------------------------------------
# A result per scenario, for the suites that run scenarios one by one.

class Results:
    """Rows of `<scenario> <status> <note>` (see report.STATUSES) for one
    suite, printed briefly as they come and in full by print(), and
    recorded in its report. A suite that prints rows its own way records
    them in the report directly."""

    def __init__(self, names: Sequence[str], report: report_module.Report):
        self.name_width = max((len(n) for n in names), default=0)
        self.status_width = max(len(s) for s in report_module.STATUSES)
        self.rows: List[str] = []
        self.report = report

    @property
    def failed(self) -> int:
        return self.report.failed

    def add(self, scenario: str, status: str, note: str = "") -> None:
        self.report.add(scenario, status, note)
        row = f"{scenario:<{self.name_width}} {status:<{self.status_width}} {note}"
        self.rows.append(row.rstrip())
        print(f"  {scenario:<{self.name_width}} {status}")

    def print(self) -> None:
        if not self.rows:
            return
        print()
        for row in self.rows:
            print(row)
