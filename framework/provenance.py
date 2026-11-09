#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Record how a run of the tests came about.

  framework/provenance.py write DIR CMD...   DIR/provenance.txt, for fed.sh
  framework/provenance.py source             what Git says of the checkout,
                                             for smoke.sh

`write` is for fed.sh's `test` command, as the tests start. CMD is what
it runs in the dev container. Its standard input holds the knobs (see
fed_note_knobs in fed.sh), one per line: the knob's name, the last
source to change it, and its value, separated by tabs. Run it from the
top of the checkout, with the environment that Compose needs.
provenance.txt has:

  Tests       CMD, when it started, and the host and Docker it ran on
  Framework   the checkout's commit, and its changes outside local/:
              what `git status` says of them, and a SHA-256 of them all.
              In a copy that smoke.sh made, which Git knows nothing of,
              what `source` said of the checkout as smoke.sh copied it
              (local/framework-source.txt).
  Settings    each knob, as local/environment.cfg would set it, and the
              last source to change it
  Containers  each container's image, by name and ID, when the image was
              built, and the Pelican in it, as its log says as it starts
  Binaries    each Pelican binary that fed.sh or the tests run: its
              SHA-256, and what its --version says. Those in
              framework/var/bin/linux/, which the dev container may
              write, run only there, as does its own pelican-server, if
              it has one; those in framework/var/bin/host/ on the host.

Local/ is the user's, so it is no part of the framework's changes:
Settings and the logs' config/ record what of it a run used.

What it cannot learn, it says so, and goes on: no run is stopped for
want of its provenance.
"""

# A script, which fed.sh and smoke.sh name as it is.
# pylint: disable=invalid-name

import hashlib
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common

# pylint: enable=wrong-import-position

# Git's pathspec for the framework's own files.
OWN = (".", ":(exclude)local")

# What smoke.sh puts in each of its copies of the checkout: what `source`
# said of the checkout.
COPY_SOURCE = os.path.join("local", "framework-source.txt")

# The Pelican binaries that fed.sh and the tests run: each is in both
# framework/var/bin/linux/ and framework/var/bin/host/.
BINARIES = ("pelican", "pelican-server")

# What Pelican says of itself as it starts, in a log, and in --version.
LOGGED = re.compile(r'msg="(Version|Build Date|Build Commit): ([^"]*)"')
VERSION = re.compile(r"^(Version|Build Date|Build Commit): (.*)$")

# How far into a container's log Pelican must have said what it is.
LOG_LINES = 500

# How long any one command may take.
TIMEOUT = 60

# Run in the dev container: for each binary, its path, SHA-256, and
# --version's lines, separated by tabs; first those named, then the
# container's own pelican-server, if it has one, marked `own`.
DEV_SCRIPT = r"""
describe() {
  sum=$(sha256sum "$1" 2>/dev/null) || sum=""
  printf '%s\t%s\t%s\t' "$2" "$1" "${sum%% *}"
  "$1" --version 2>/dev/null | tr '\n' '\t'
  printf '\n'
}
for f in "$@"; do
  [ -f "$f" ] && describe "$f" named
done
own=$(command -v pelican-server 2>/dev/null) && describe "${own}" own
exit 0
"""


def run(args: Sequence[str]) -> bytes | None:
    """Return what args print, or None if they cannot be run, or fail."""
    try:
        done = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def run_text(args: Sequence[str]) -> str | None:
    """Return what args print, as text, or None as run() does."""
    out = run(args)
    return None if out is None else out.decode(errors="replace")


def pelican(said: dict[str, str]) -> str:
    """Return what Pelican said of itself, from its version lines."""
    parts = [f"Pelican {said['Version']}"] if "Version" in said else []
    if "Build Commit" in said:
        parts.append(f"commit {said['Build Commit']}")
    if "Build Date" in said:
        parts.append(f"built {said['Build Date']}")
    return ", ".join(parts) if parts else "says no version"


def utc(stamp: str) -> str:
    """Return Docker's timestamp in UTC, to the second, as Pelican gives its own.

    Docker gives nanoseconds, and an image built here the local offset.
    """
    trimmed = re.sub(r"\.[0-9]+", "", stamp.strip())
    try:
        when = datetime.fromisoformat(trimmed)
    except ValueError:
        return trimmed
    if when.tzinfo is None:
        return trimmed
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def version_lines(lines: Sequence[str]) -> dict[str, str]:
    """Return what --version's lines say: version, build date, and commit."""
    said: dict[str, str] = {}
    for line in lines:
        match = VERSION.match(line.strip())
        if match:
            said[match[1]] = match[2]
    return said


# --------------------------------------------------------------------------
# The framework's source


def changes_digest() -> str | None:
    """Return a SHA-256 of the checkout's changes outside local/.

    It covers the difference of the tracked files from HEAD, and each
    untracked file that Git does not ignore, by name and content (a
    symlink by its target), so two checkouts of the same commit with the
    same changes have the same digest.
    """
    diff = run(["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv", "--", *OWN])
    others = run(["git", "ls-files", "-z", "--others", "--exclude-standard", "--", *OWN])
    if diff is None or others is None:
        return None
    digest = hashlib.sha256(diff)
    for name in sorted(n for n in others.split(b"\0") if n):
        digest.update(name + b"\0")
        try:
            if stat.S_ISLNK(os.lstat(name).st_mode):
                content = b"symlink\0" + os.fsencode(os.readlink(name))
            else:
                with open(name, "rb") as f:
                    content = f.read()
        except OSError:
            content = b"unreadable\0"
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def source() -> list[str]:
    """Return what Git says of the checkout: its commit, and changes.

    A copy of the checkout that smoke.sh made is no Git checkout of its
    own, even where it is inside one, so it gets what smoke.sh left in it.
    """
    top = run_text(["git", "rev-parse", "--show-toplevel"])
    if top is None or os.path.realpath(top.strip()) != os.path.realpath("."):
        try:
            with open(COPY_SOURCE, encoding="utf-8") as f:
                copied = f.read().splitlines()
        except OSError:
            return ["not a Git checkout, nor a copy that smoke.sh made of one"]
        return ["a copy that smoke.sh made of the checkout, of which Git said:"] + [
            f"  {line}" for line in copied
        ]
    head = run_text(["git", "log", "-1", "--format=%H%x09%cs%x09%s"])
    if head is None:
        return ["a Git checkout, of no commit that Git can name"]
    commit, date, subject = (head.rstrip("\n").split("\t", 2) + ["", ""])[:3]
    branch = (run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "").strip()
    on = f"on {branch}" if branch and branch != "HEAD" else "detached"
    lines = [f"commit {commit} ({on}), {date}: {subject}"]
    status = run_text(["git", "status", "--porcelain=v1", "--", *OWN])
    digest = changes_digest()
    if status is None or digest is None:
        lines.append("changes outside local/: unknown (git failed)")
    elif not status.strip():
        lines.append("no changes outside local/")
    else:
        lines.append(f"changes outside local/, SHA-256 {digest}:")
        lines += [f"  {line}" for line in status.rstrip("\n").split("\n")]
    return lines


# --------------------------------------------------------------------------
# What ran


def settings(knobs: Sequence[str]) -> list[str]:
    """Return each knob, as an assignment, with where it got its value."""
    rows: list[tuple[str, str]] = []
    for line in knobs:
        if not line.strip():
            continue
        name, origin, value = (line.split("\t", 2) + ["", ""])[:3]
        if value:
            rows.append((f"{name}={shlex.quote(value)}", origin or "unknown"))
        else:
            rows.append((f"{name}=", origin or "not set"))
    width = max((len(r[0]) for r in rows if len(r[0]) <= 48), default=0)
    return [f"{assignment:<{width}}  # {origin}" for assignment, origin in rows]


def container_ids() -> list[str] | None:
    """Return the federation's containers' IDs, and the dev container's.

    Under the tiny topology, the dev container is in docker-compose.yaml
    alone, which COMPOSE_FILE does not name.

    Returns:
        The IDs, or None if Compose cannot say.
    """
    ids: list[str] = []
    for args in (
        ["docker", "compose", "ps", "--all", "--quiet"],
        ["docker", "compose", "-f", "framework/docker-compose.yaml", "ps", "--all", "--quiet", "dev"],
    ):
        out = run_text(args)
        if out is None:
            return None
        ids += [found for found in out.split() if found not in ids]
    return ids


def logged_pelican(container: str) -> str | None:
    """Return what Pelican said of itself as the container started, if it did."""
    said: dict[str, str] = {}
    try:
        with subprocess.Popen(
            ["docker", "logs", container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as proc:
            if proc.stdout is None:
                return None
            try:
                for n, raw in enumerate(proc.stdout):
                    if n >= LOG_LINES or "Build Commit" in said:
                        break
                    match = LOGGED.search(raw.decode(errors="replace"))
                    if match:
                        said.setdefault(match[1], match[2])
            finally:
                proc.kill()
    except OSError:
        return None
    return pelican(said) if said else None


def containers() -> list[str]:
    """Return each container's image, by name and ID, and its Pelican."""
    ids = container_ids()
    if ids is None:
        return ["unknown (docker compose ps failed)"]
    if not ids:
        return ["none"]
    out = run_text(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.service"}}\t{{.Id}}\t{{.Config.Image}}\t{{.Image}}',
            *ids,
        ]
    )
    if out is None:
        return ["unknown (docker inspect failed)"]
    lines: list[str] = []
    for line in sorted(out.splitlines()):
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        service, container, image, image_id = fields
        created = run_text(["docker", "image", "inspect", "--format", "{{.Created}}", image_id])
        built = f", built {utc(created)}" if created else ""
        lines += [service, f"  image {image}{built}", f"  ID {image_id}"]
        said = logged_pelican(container)
        if said:
            lines.append(f"  {said}, as its log says")
    return lines


def binaries() -> list[str]:
    """Return each Pelican binary's SHA-256, and what its --version says."""
    lines: list[str] = []
    dev = run_text(
        [
            "docker",
            "compose",
            "-f",
            "framework/docker-compose.yaml",
            "exec",
            "-T",
            "dev",
            "sh",
            "-c",
            DEV_SCRIPT,
            "sh",
            *(f"/scratch/framework/var/bin/linux/{name}" for name in BINARIES),
        ]
    )
    if dev is None:
        lines.append("framework/var/bin/linux/: unknown (the dev container cannot say)")
    for line in (dev or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        kind, path, digest, *said = fields
        lines += [
            path.removeprefix("/scratch/"),
            f"  SHA-256 {digest or 'unknown'}",
            f"  {pelican(version_lines(said))}",
        ]
        if kind == "own":
            # See common.server_binary().
            lines.append("  the dev container's own, which the tests run, not framework/var/bin/linux/'s")
    for name in BINARIES:
        path = os.path.join("framework", "var", "bin", "host", name)
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
        said = (run_text([path, "--version"]) or "").splitlines()
        lines += [path, f"  SHA-256 {digest}", f"  {pelican(version_lines(said))}"]
    return lines


def host() -> str:
    """Return the host's system, and Docker's version."""
    docker = (run_text(["docker", "version", "--format", "{{.Server.Version}}"]) or "").strip()
    return f"{platform.system()} {platform.release()} {platform.machine()}, Docker {docker or 'unknown'}"


def write(out_dir: str, cmd: Sequence[str], knobs: Sequence[str]) -> None:
    """Write out_dir/provenance.txt (see the usage)."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = [
        ("Tests", [shlex.join(cmd), f"started {started}", f"on {host()}"]),
        ("Framework", source()),
        ("Settings (each knob, and the last source to change it)", settings(knobs)),
        ("Containers (each one's image, and the Pelican in it)", containers()),
        ("Binaries (each one's SHA-256, and what its --version says)", binaries()),
    ]
    text = "\n\n".join(title + "\n" + "\n".join(f"  {line}" for line in body) for title, body in sections)
    with open(os.path.join(out_dir, "provenance.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in ([], ["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    command, rest = args[0], args[1:]
    if command == "write":
        if len(rest) < 2:
            common.die_usage("write needs DIR and the command that runs the tests")
        write(rest[0], rest[1:], sys.stdin.read().splitlines())
    elif command == "source":
        if rest:
            common.die_usage("source takes no arguments")
        print("\n".join(source()))
    else:
        common.die_usage(f"unknown command: {command} (try: write, source)")


if __name__ == "__main__":
    main()
