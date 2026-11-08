#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""Control the federation's services for the tests in the dev container.

  framework/control.py serve   serve the tests' requests until stopped,
                               or until the process that started it ends

The dev container has no Docker, so this helper on the host acts for
the tests. Run it through fed.sh, whose environment Docker Compose
needs: `./fed.sh test` runs one beside the tests; `./fed.sh control`
runs one in the foreground, for a test.py run by hand in the dev
container. One serves at a time: another that finds it serving leaves
the tests to it.

The requests come through files under framework/var/control/ (see
framework/testlib/control.py). The helper does each, one at a time, on
the shape's services but `dev`:

- most with `docker compose <action> <service>`;
- `state` and `exited` with `docker compose ps`, which says what state
  a container is in, and which exited without the tests' stopping or
  killing them;
- `freeze-xrootd` and `thaw-xrootd` by signaling the XRootD daemons in
  the service's container, with `docker compose exec`.

On stopping, the helper puts back what the tests left: it removes their
configuration from control/config/, and restarts each running service
it was in; and it starts, unpauses, or thaws what they stopped, killed,
paused, or froze. SIGTERM or SIGHUP stops it once the request in hand
is done, so that it knows what that request's action did. On starting,
it removes any configuration still left, as by a helper that was
killed, and restarts each running service it was in; what else an
earlier helper did, it cannot know.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Any

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common, control

# pylint: enable=wrong-import-position

# Where Compose finds COMPOSE_FILE, as fed.sh names it.
TOP = os.path.dirname(common.FRAMEWORK)
# How often it looks for requests, in seconds.
POLL = 0.2


def say(message: str) -> None:
    """Print message, as the helper's."""
    print(f"{common.PROG}: {message}", file=sys.stderr, flush=True)


def compose(*args: str) -> tuple[bool, str]:
    """Run `docker compose <args>`.

    Returns:
        Whether it succeeded, and its output, or on failure, why it failed.
    """
    command = f"docker compose {' '.join(args)}"
    try:
        done = subprocess.run(
            ["docker", "compose", *args],
            cwd=TOP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=control.ACTION_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"`{command}` ran over {control.ACTION_TIMEOUT}s, and was killed"
    except OSError as e:
        return False, f"`{command}`: {e}"
    output = done.stdout.strip()
    if done.returncode != 0:
        return False, f"`{command}` exited {done.returncode}: {output}"
    return True, output


def compose_args(action: str, svc: str) -> list[str]:
    """Return the `docker compose` arguments that do action to svc."""
    signal_name = control.SIGNALS.get(action)
    if signal_name is not None:
        return ["exec", "-T", svc, "pkill", f"-{signal_name}", "-x", "xrootd"]
    return [action, svc]


class Helper:
    """What does the tests' requests, and puts back what they left undone.

    Attributes:
        services: The shape's services that the tests may control: all
            but `dev`.
        undo: The action that puts each service back, by service.
        stopping: Whether a signal has asked it to stop serving (stop()).
    """

    def __init__(self) -> None:
        ok, output = compose("config", "--services")
        if not ok:
            common.die(f"cannot list the shape's services: {output}")
        self.services = sorted(set(output.split()) - {"dev"})
        self.undo: dict[str, str] = {}
        self.stopping = False

    def stop(self, _signum: int, _frame: FrameType | None) -> None:
        """Have serving end once the request in hand is done.

        Ending in the middle of a request would kill its `docker compose`,
        which may have acted all the same, before what puts it back is
        recorded in undo.
        """
        self.stopping = True

    def handle(self, request: dict[str, Any]) -> tuple[bool, str]:
        """Do what request asks.

        Args:
            request: The request's JSON object: its `action`, and its
                `service`.

        Returns:
            Whether it was done, and what to say.
        """
        action = str(request.get("action", ""))
        svc = str(request.get("service", ""))
        if action not in control.ACTIONS:
            return False, f"no such action: {action!r} (try: {', '.join(control.ACTIONS)})"
        if action == "ping":
            return True, "serving"
        if action == "exited":
            return self.exited()
        if svc not in self.services:
            return False, (
                f"the shape has no service {svc!r} that tests may control"
                f" (try: {', '.join(self.services)})"
            )
        if action == "state":
            return compose("ps", "--all", "--format", "{{.State}}", svc)
        say(f"{action} {svc}")
        ok, output = compose(*compose_args(action, svc))
        if ok:
            if action in control.UNDO:
                self.undo[svc] = control.UNDO[action]
            else:
                self.undo.pop(svc, None)
        else:
            say(output)
        return ok, output

    def exited(self) -> tuple[bool, str]:
        """Say which services exited on their own, each with its exit code.

        Those the tests stopped or killed, which the helper starts again,
        are passed over.

        Returns:
            Whether `docker compose ps` answered, and a line `<service>
            <exit code>` for each.
        """
        ok, output = compose("ps", "--all", "--format", "{{.Service}} {{.State}} {{.ExitCode}}")
        if not ok:
            return ok, output
        found: list[str] = []
        for line in output.splitlines():
            svc, state, code = (line.split() + ["", "", ""])[:3]
            if svc in self.services and state in ("exited", "dead") and self.undo.get(svc) != "start":
                found.append(f"{svc} {code}")
        return True, "\n".join(found)

    def put_back(self) -> None:
        """Put back what the tests left.

        Remove configuration that they left in control/config/, and start,
        unpause, or thaw what they left stopped, killed, paused, or
        frozen; then restart each running service that had configuration
        of theirs.
        """
        configured: set[str] = set()
        # The dev container writes each config/<svc>/, so the helper
        # follows no symlink there (see common.open_dir).
        config = common.open_dir(control.CONFIG)
        try:
            for svc in sorted(os.listdir(config)):
                try:
                    directory = common.open_dir(svc, config)
                except OSError:
                    continue
                try:
                    for name in sorted(os.listdir(directory)):
                        shown = f"framework/var/control/config/{svc}/{name}"
                        if common.is_dir_entry(name, directory):
                            say(f"leaving {shown}, a directory, which no test makes")
                            continue
                        say(f"removing {shown}, a test's")
                        os.unlink(name, dir_fd=directory)
                        configured.add(svc)
                finally:
                    os.close(directory)
        finally:
            os.close(config)
        for svc, action in sorted(self.undo.items()):
            say(f"{action} {svc}, which a test left so")
            ok, output = compose(*compose_args(action, svc))
            if not ok:
                say(output)
            elif action == "start":
                configured.discard(svc)
        self.undo.clear()
        if not configured:
            return
        _, output = compose("ps", "--services", "--status", "running")
        for svc in sorted(configured & set(output.split())):
            say(f"restart {svc}, to drop a test's configuration")
            ok, output = compose("restart", svc)
            if not ok:
                say(output)


def answer(requests: int, name: str, ok: bool, output: str) -> None:
    """Answer request name in directory requests, and remove it.

    The dev container writes the directory, so the answer replaces
    whatever is in its place rather than being written through it (see
    common.replace_file).
    """
    response = name[: -len(".req")] + ".res"
    common.replace_file(response, json.dumps({"ok": ok, "output": output}).encode(), 0o644, requests)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=requests)


def serve_requests(helper: Helper, parent: int, requests: int) -> None:
    """Answer requests, oldest first, until stopped or the parent ends.

    Args:
        helper: What does the requests.
        parent: The process ID of the process that started the helper.
        requests: The requests' directory, open.
    """
    while os.getppid() == parent and not helper.stopping:
        for name in sorted(os.listdir(requests)):
            if helper.stopping:
                return
            if not name.endswith(".req"):
                continue
            try:
                data, _ = common.read_file(name, requests)
                request = common.as_object(json.loads(data))
            except FileNotFoundError:
                continue  # given up on
            except (OSError, ValueError) as e:
                answer(requests, name, False, f"cannot read the request: {e}")
                continue
            if request is None:
                answer(requests, name, False, "the request is not a JSON object")
                continue
            answer(requests, name, *helper.handle(request))
        time.sleep(POLL)


def serve() -> None:
    """Serve the tests' requests, unless another helper does."""
    if "COMPOSE_FILE" not in os.environ:
        common.die_usage("run it through fed.sh: ./fed.sh control")
    os.makedirs(control.REQUESTS, exist_ok=True)
    os.makedirs(control.CONFIG, exist_ok=True)
    # pylint: disable-next=consider-using-with  # held while serving
    lock = open(os.path.join(control.DIR, "lock"), "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("another helper serves the tests already")
        return
    parent = os.getppid()
    helper = Helper()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, helper.stop)
    requests = common.open_dir(control.REQUESTS)
    # What is here is an earlier run's: no test asks before `serving` is.
    for name in os.listdir(requests):
        if not common.is_dir_entry(name, requests):
            os.unlink(name, dir_fd=requests)
    helper.put_back()
    with open(control.SERVING, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
    try:
        serve_requests(helper, parent, requests)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(control.SERVING)
        helper.put_back()
        os.close(requests)


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return
    if args != ["serve"]:
        common.die_usage("usage: framework/control.py serve (see --help)")
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
