#!/usr/bin/env python3
"""Control the federation's services for the tests in the dev container.

The dev container has no Docker.

  framework/control.py serve   serve the tests' requests until stopped,
                               or until the process that started it ends

Run it through fed.sh, whose environment Docker Compose needs.
`./fed.sh test` runs one beside the tests; `./fed.sh control` runs one
in the foreground, for a test.py run by hand in the dev container. One
serves at a time: another that finds it serving leaves the tests to it.

framework/testlib/control.py describes the requests, which come through
files under framework/var/control/. Each is done, one at a time, with
`docker compose <action> <service>`, on the shape's services but `dev`.
On starting and on stopping, the helper puts back what the tests left:
it removes their configuration from control/config/, and restarts each
service it was in, if running; and it starts, or unpauses, what they
stopped, killed, or paused.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess  # nosec B404
import sys
import time
from types import FrameType
from typing import Any, NoReturn, Optional

# No __pycache__ in the checkout, where the dev container would leave it
# root's.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common, control

# pylint: enable=wrong-import-position

PROG = "control.py"
# Where Compose finds COMPOSE_FILE, as fed.sh names it.
TOP = os.path.dirname(common.FRAMEWORK)
# How often it looks for requests, in seconds.
POLL = 0.2


def say(message: str) -> None:
    """Print message, as the helper's."""
    print(f"{PROG}: {message}", file=sys.stderr, flush=True)


def die(message: str, status: int = 1) -> NoReturn:
    """Print message, and exit."""
    say(message)
    sys.exit(status)


def compose(*args: str) -> tuple[bool, str]:
    """Run `docker compose <args>`.

    Returns:
        Whether it succeeded, and its output, or on failure, why it failed.
    """
    command = f"docker compose {' '.join(args)}"
    try:
        done = subprocess.run(  # nosec B603 B607
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


class Helper:
    """What does the tests' requests, and puts back what they left undone.

    Attributes:
        services: The shape's services that the tests may control: all
            but `dev`.
        undo: The action that puts each service back, by service.
    """

    def __init__(self) -> None:
        ok, output = compose("config", "--services")
        if not ok:
            die(f"cannot list the shape's services: {output}")
        self.services = sorted(set(output.split()) - {"dev"})
        self.undo: dict[str, str] = {}

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
        if svc not in self.services:
            return False, (
                f"the shape has no service {svc!r} that tests may control"
                f" (try: {', '.join(self.services)})"
            )
        say(f"{action} {svc}")
        ok, output = compose(action, svc)
        if ok:
            if action in control.UNDO:
                self.undo[svc] = control.UNDO[action]
            else:
                self.undo.pop(svc, None)
        else:
            say(output)
        return ok, output

    def put_back(self) -> None:
        """Put back what the tests left.

        Remove configuration that they left in control/config/, and start,
        or unpause, what they left stopped, killed, or paused; then
        restart each running service that had configuration of theirs.
        """
        configured: set[str] = set()
        for svc in sorted(os.listdir(control.CONFIG)):
            directory = os.path.join(control.CONFIG, svc)
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                say(f"removing framework/var/control/config/{svc}/{name}, a test's")
                os.remove(os.path.join(directory, name))
                configured.add(svc)
        for svc, action in sorted(self.undo.items()):
            say(f"{action} {svc}, which a test left so")
            ok, output = compose(action, svc)
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


def answer(path: str, ok: bool, output: str) -> None:
    """Answer request path, and remove it."""
    found = path[: -len(".req")] + ".res"
    with open(f"{found}.tmp", "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "output": output}, f)
    os.rename(f"{found}.tmp", found)
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def serve_requests(helper: Helper, parent: int) -> None:
    """Answer requests, oldest first, until the helper's parent ends.

    Args:
        helper: What does the requests.
        parent: The process ID of the process that started the helper.
    """
    while os.getppid() == parent:
        for name in sorted(os.listdir(control.REQUESTS)):
            if not name.endswith(".req"):
                continue
            path = os.path.join(control.REQUESTS, name)
            try:
                with open(path, encoding="utf-8") as f:
                    request = common.as_object(json.load(f))
            except FileNotFoundError:
                continue  # given up on
            except (OSError, ValueError) as e:
                answer(path, False, f"cannot read the request: {e}")
                continue
            if request is None:
                answer(path, False, "the request is not a JSON object")
                continue
            answer(path, *helper.handle(request))
        time.sleep(POLL)


def stopped(_signum: int, _frame: Optional[FrameType]) -> NoReturn:
    """Stop serving, putting back what the tests left (see serve())."""
    sys.exit(0)


def serve() -> None:
    """Serve the tests' requests, unless another helper does."""
    if "COMPOSE_FILE" not in os.environ:
        die("run it through fed.sh: ./fed.sh control", 2)
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
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stopped)
    helper = Helper()
    # What is here is an earlier run's: no test asks before `serving` is.
    for name in os.listdir(control.REQUESTS):
        os.remove(os.path.join(control.REQUESTS, name))
    helper.put_back()
    with open(control.SERVING, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
    try:
        serve_requests(helper, parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(control.SERVING)
        helper.put_back()


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        return
    if args != ["serve"]:
        die("usage: framework/control.py serve (see --help)", 2)
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
