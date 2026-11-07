#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Check that no container can change what the host runs.

  framework/check-mounts.py     check, and exit 1 if a rule is broken

Run by fed.sh before it starts a container, with the environment that
Compose needs. It reads the bind mounts of every service, in every
profile, of both Compose files (`docker compose config`), and checks:

  writable   A container writes only below one of WRITABLE, and the dev
             container its /app, which neither is nor holds this checkout.
             So nothing in a container changes the framework's own code,
             what fed.sh generates and reads, or the host's binaries
             (framework/var/bin/host/).
  nested     No bind source is below a directory that a container writes.
             A container can replace anything below such a directory with
             a symlink, and Docker resolves a bind source on the host
             each time the container that mounts it starts: the symlink
             would give that container whatever it points to.
  symlinks   No bind source in the checkout has a symlink in its path, as
             one left from before these rules would.
  sockets    No bind source is a socket, such as Docker's.

See "Isolation" in docs/development.md.
"""

# A script, which fed.sh names as it is.
# pylint: disable=invalid-name

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import cast, override

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common

# pylint: enable=wrong-import-position

TOP = os.path.dirname(common.FRAMEWORK)
COMPOSE_FILES = ("framework/docker-compose.yaml", "framework/docker-compose.tiny.yaml")

# Where, under the top of the checkout, a container may write.
WRITABLE = (
    "framework/var/data",
    "framework/var/state",
    "framework/var/issuer-keys",
    "framework/var/control/requests",
    "framework/var/control/config",
    "framework/var/results",
    "framework/var/dev",
    "framework/var/bin/linux",
    "framework/var/grafana",
)


@dataclass(frozen=True)
class Mount:
    """A bind mount of a service.

    Attributes:
        service: The service's name.
        source: The absolute path on the host.
        target: Where the service mounts it.
        writable: Whether the service may write it.
    """

    service: str
    source: str
    target: str
    writable: bool

    @override
    def __str__(self) -> str:
        how = "read-write" if self.writable else "read-only"
        return f"{self.service} mounts {shown(self.source)} {how} at {self.target}"


def shown(path: str) -> str:
    """Return how a message names path: from the top of the checkout, if in it."""
    return os.path.relpath(path, TOP) if below_or_at(path, TOP) else path


def below_or_at(path: str, directory: str) -> bool:
    """Return whether path is directory or is below it."""
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def below(path: str, directory: str) -> bool:
    """Return whether path is strictly below directory."""
    return path != directory and below_or_at(path, directory)


def compose_config(compose_file: str) -> dict[str, object]:
    """Return compose_file's configuration, every profile's, as Compose resolves it."""
    env = dict(os.environ, COMPOSE_PROFILES="*")
    try:
        done = subprocess.run(
            ["docker", "compose", "-f", os.path.join(TOP, compose_file), "config", "--format", "json"],
            cwd=TOP,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        common.die(f"cannot run docker compose: {e}")
    if done.returncode != 0:
        common.die(f"`docker compose config` of {compose_file} failed: {done.stderr.strip()}")
    try:
        found = common.as_object(json.loads(done.stdout))
    except ValueError:
        found = None
    if found is None:
        common.die(f"`docker compose config` of {compose_file} gave no object")
    return found


def mounts_of(config: dict[str, object]) -> list[Mount]:
    """Return the bind mounts of config's services, its secrets' files included."""
    found: list[Mount] = []
    services = common.as_object(config.get("services")) or {}
    for name, value in services.items():
        service = common.as_object(value) or {}
        volumes = service.get("volumes")
        for volume in cast(list[object], volumes) if isinstance(volumes, list) else []:
            entry = common.as_object(volume) or {}
            if entry.get("type") != "bind":
                continue
            found.append(
                Mount(name, str(entry["source"]), str(entry.get("target", "")), not entry.get("read_only"))
            )
    # Compose bind-mounts each secret's file, read-only, into the services
    # that name it.
    secrets = common.as_object(config.get("secrets")) or {}
    for name, value in secrets.items():
        secret = common.as_object(value) or {}
        if "file" in secret:
            found.append(Mount(f"secret {name}", str(secret["file"]), "(a secret)", False))
    return found


def writable_problems(mounts: list[Mount]) -> list[str]:
    """Return how mounts break the `writable` rule."""
    problems: list[str] = []
    allowed = [os.path.join(TOP, rel) for rel in WRITABLE]
    for mount in mounts:
        if not mount.writable:
            continue
        if mount.service == "dev" and mount.target == "/app":
            if below_or_at(mount.source, TOP) or below_or_at(TOP, mount.source):
                problems.append(f"{mount}, which is or holds this checkout")
            continue
        if not any(below_or_at(mount.source, a) for a in allowed):
            problems.append(f"{mount}, outside where a container may write")
    return problems


def nested_problems(mounts: list[Mount]) -> list[str]:
    """Return how mounts break the `nested` rule."""
    problems: list[str] = []
    writable = sorted({m.source: m for m in mounts if m.writable}.values(), key=lambda m: m.source)
    for mount in sorted(mounts, key=lambda m: (m.source, m.service)):
        for parent in writable:
            if below(mount.source, parent.source):
                problems.append(f"{mount}, below what {parent.service} writes ({shown(parent.source)})")
                break
    return problems


def symlink_problems(mounts: list[Mount]) -> list[str]:
    """Return how mounts break the `symlinks` rule."""
    problems: list[str] = []
    for source in sorted({m.source for m in mounts if below(m.source, TOP)}):
        path = TOP
        for part in os.path.relpath(source, TOP).split(os.sep):
            path = os.path.join(path, part)
            if os.path.islink(path):
                problems.append(f"{shown(path)}, in the path of a bind source, is a symlink")
                break
            if not os.path.lexists(path):
                break
    return problems


def socket_problems(mounts: list[Mount]) -> list[str]:
    """Return how mounts break the `sockets` rule."""
    problems: list[str] = []
    for mount in mounts:
        try:
            found = os.stat(mount.source)
        except OSError:
            continue
        if stat.S_ISSOCK(found.st_mode):
            problems.append(f"{mount}, a socket")
    return problems


def main() -> None:
    """Check the rules, and say how each is broken."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return
    if args:
        common.die_usage("takes no arguments (see --help)")
    mounts: list[Mount] = []
    for compose_file in COMPOSE_FILES:
        mounts += mounts_of(compose_config(compose_file))
    problems = (
        writable_problems(mounts)
        + nested_problems(mounts)
        + symlink_problems(mounts)
        + socket_problems(mounts)
    )
    if problems:
        for problem in dict.fromkeys(problems):
            print(f"{common.PROG}: {problem}", file=sys.stderr)
        common.die("refusing to start a container that could change what the host runs (see --help)")


if __name__ == "__main__":
    main()
