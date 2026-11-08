"""Controlling the federation's services in the middle of a run.

A test may restart, stop, start, pause, unpause, or kill a service, or
restart one with configuration of its own. It may stop and resume the
XRootD daemons in a service, leaving the rest of it running. It may ask
what state a service's container is in, and which servers have exited
on their own.

The dev container has no Docker, so a helper on the host,
framework/control.py, acts for the tests: `./fed.sh test` runs one
beside them, and `./fed.sh control` runs one for a test.py run by hand.
They talk through files under framework/var/control/:

  serving             the helper's process ID, while one serves
  requests/<id>.req   a request: {"action": ..., "service": ...}
  requests/<id>.res   the helper's answer: {"ok": ..., "output": ...}
  config/<service>/   configuration of a test's own: the last of the
                      service's configuration layers
                      (/fed/control.instance; see
                      framework/etc/pelican.yaml), empty but while
                      restart_with() runs

The helper takes the actions in ACTIONS, one at a time, on the shape's
services but `dev`, and refuses anything else. Each call here returns
once its action is done, and raises ControlError if it failed. A
Pelican server that is restarted, started, or unpaused must also answer
on its web port within SERVE_WAIT seconds.

A scenario should put back whatever it stops, pauses, kills, or
configures, even if it fails. The helper puts back what is left only
when it stops serving, and the next helper to start, only the
configuration.
"""

import contextlib
import functools
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence

from . import common, web

DIR = os.path.join(common.VAR, "control")
SERVING = os.path.join(DIR, "serving")
REQUESTS = os.path.join(DIR, "requests")
CONFIG = os.path.join(DIR, "config")

# The actions that signal the XRootD daemons in a service's container,
# rather than the container, each by its signal: `docker compose exec`
# of `pkill -<signal> -x xrootd` there.
SIGNALS = {"freeze-xrootd": "STOP", "thaw-xrootd": "CONT"}
# What the helper does, each by `docker compose <action> <service>` but
# `ping`, which only answers; `state` and `exited`, which only look
# (`docker compose ps`); and those of SIGNALS.
ACTIONS = (
    "ping",
    "state",
    "exited",
    "restart",
    "stop",
    "start",
    "pause",
    "unpause",
    "kill",
    "freeze-xrootd",
    "thaw-xrootd",
)
# What puts back what each action leaves undone.
UNDO = {"stop": "start", "kill": "start", "pause": "unpause", "freeze-xrootd": "thaw-xrootd"}

# How long the helper lets an action run, and how long a request waits
# for its answer, in seconds.
ACTION_TIMEOUT = 300
ANSWER_WAIT = ACTION_TIMEOUT + 30
# How long unavailable() waits for a helper just started to serve, and
# then for it to answer.
STARTUP_WAIT, PING_WAIT = 10, 15
# How long a Pelican server may take to answer on its web port after it
# starts: the services' health checks give it 90 seconds
# (docker-compose.yaml), and a cache waits for its origin.
SERVE_WAIT = 180

# The Pelican servers (fed.sh's fed_services), each of which is the host
# of its name, with its web port at 8444 (config.d/base/20-server.yaml);
# `fed` is the tiny topology's, and `transfer` a transfer server of its
# own.
PELICAN_SERVER = re.compile(r"(?:director|origin|cache)-\d+|registry|transfer|fed")

# What a file of restart_with() may be named.
CONFIG_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.yaml")


class ControlError(Exception):
    """An action failed, or the helper did not answer."""


def _ask(action: str, service: str, seconds: float = ANSWER_WAIT) -> str:
    """Ask the helper to do action to service, and return what it said.

    Raises:
        ControlError: It did not answer within seconds, or the action
            failed.
    """
    ident = f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}"
    request = os.path.join(REQUESTS, f"{ident}.req")
    answer = os.path.join(REQUESTS, f"{ident}.res")
    with open(f"{request}.tmp", "w", encoding="utf-8") as f:
        json.dump({"action": action, "service": service}, f)
    os.rename(f"{request}.tmp", request)
    what = f"{action} {service}".rstrip()
    if not common.wait_for(lambda: os.path.exists(answer), seconds, interval=0.2):
        with contextlib.suppress(FileNotFoundError):
            os.remove(request)
        raise ControlError(f"{what}: the control helper did not answer in {seconds:.0f}s")
    with open(answer, encoding="utf-8") as f:
        found = common.as_object(json.load(f)) or {}
    os.remove(answer)
    output = str(found.get("output", "")).strip()
    if found.get("ok") is not True:
        raise ControlError(f"{what}: {output or 'failed'}")
    return output


@functools.lru_cache(maxsize=None)
def unavailable() -> str | None:
    """Return why the tests can't control the services, or None if they can.

    They can't where no helper serves this run (as when test.py runs by
    hand in the dev container, without `./fed.sh control`), or it does not
    answer. Asked once.
    """
    if not common.wait_for(lambda: os.path.exists(SERVING), STARTUP_WAIT):
        return (
            "no control helper serves this run: ./fed.sh test runs one, and"
            " ./fed.sh control on the host runs one for a test.py run by hand"
        )
    try:
        _ask("ping", "", PING_WAIT)
    except ControlError as e:
        return str(e)
    return None


def unrestartable(fed: common.Federation) -> str | None:
    """Return why the tests can't restart an origin alone, or None.

    The tiny topology's origin is the whole federation's process.
    """
    if fed.tiny:
        return "the tiny topology's origin is the whole federation's process"
    return unavailable()


def pelican_server(svc: str) -> bool:
    """Return whether svc is a Pelican server."""
    return PELICAN_SERVER.fullmatch(svc) is not None


def answers(svc: str) -> bool:
    """Return whether Pelican server svc answers on its web port.

    Whatever it says counts, as its health check asks
    (framework/service-health.sh).
    """
    url = f"https://{svc}:8444/api/v1.0/health"
    return web.request("GET", url, timeout=5).status is not None


def wait_serving(svc: str, seconds: float = SERVE_WAIT) -> None:
    """Wait until svc, if it is a Pelican server, answers on its web port.

    Raises:
        ControlError: It doesn't within seconds, or it exited on its own
            first (crashed()).
    """
    if not pelican_server(svc):
        return
    deadline = time.time() + seconds
    while not answers(svc):
        why = crashed([svc])
        if why:
            raise ControlError(f"{why}, before it answered on its web port")
        if time.time() >= deadline:
            raise ControlError(f"{svc} did not answer on its web port within {seconds:.0f}s")
        time.sleep(1)


def exited() -> dict[str, int]:
    """Return each Pelican server that exited on its own, by its exit code.

    That is one whose container exited, or died, without a test's
    stopping or killing it, as one that panics as it starts.

    Returns:
        The servers; none where no helper serves this run, or it does
        not answer, since knowing is only a way to stop waiting sooner.
    """
    if unavailable():
        return {}
    try:
        output = _ask("exited", "", PING_WAIT)
    except ControlError:
        return {}
    found: dict[str, int] = {}
    for line in output.splitlines():
        svc, _, code = line.strip().partition(" ")
        if pelican_server(svc):
            found[svc] = int(code) if code.isdigit() else -1
    return found


def crashed(services: Sequence[str] | None = None) -> str | None:
    """Return which Pelican server exited on its own (exited()), or None.

    Args:
        services: The servers that count, if not every one.
    """
    for svc, code in sorted(exited().items()):
        if services is None or svc in services:
            return f"{svc} exited on its own (exit code {code})"
    return None


def settle(problem: Callable[[], str | None], seconds: float, interval: float = 5) -> str | None:
    """Ask problem() what is wrong until it finds nothing, or seconds pass.

    It is asked at once, and then every interval seconds, as the
    federation is waited for after a restart. Each time it finds
    something, a Pelican server that exited on its own (crashed()) ends
    the wait, since none comes back by itself; one that panics as it
    starts may answer its health check first.

    Returns:
        None once problem() finds nothing; otherwise which server exited,
        or after seconds, what problem() last found.
    """
    deadline = time.time() + seconds
    found = problem()
    while found:
        why = crashed()
        if why:
            return why
        if time.time() >= deadline:
            return f"after {seconds:.0f}s, {found}"
        time.sleep(interval)
        found = problem()
    return None


def restart(svc: str) -> None:
    """Restart svc (`docker compose restart`), and wait until it serves.

    Raises:
        ControlError: The action failed, the helper did not answer, or svc
            did not answer on its web port within SERVE_WAIT seconds.
    """
    _ask("restart", svc)
    wait_serving(svc)


def stop(svc: str) -> None:
    """Stop svc (`docker compose stop`): SIGTERM, then SIGKILL.

    Raises:
        ControlError: The action failed, or the helper did not answer.
    """
    _ask("stop", svc)


def start(svc: str) -> None:
    """Start svc, stopped or killed, and wait until it serves.

    Raises:
        ControlError: The action failed, the helper did not answer, or svc
            did not answer on its web port within SERVE_WAIT seconds.
    """
    _ask("start", svc)
    wait_serving(svc)


def pause(svc: str) -> None:
    """Freeze svc's processes (`docker compose pause`).

    Its ports take connections, but nothing answers.

    Raises:
        ControlError: The action failed, or the helper did not answer.
    """
    _ask("pause", svc)


def unpause(svc: str) -> None:
    """Thaw svc, and wait until it serves.

    Raises:
        ControlError: The action failed, the helper did not answer, or svc
            did not answer on its web port within SERVE_WAIT seconds.
    """
    _ask("unpause", svc)
    wait_serving(svc)


def kill(svc: str) -> None:
    """Kill svc (`docker compose kill`: SIGKILL).

    It has no chance to shut down cleanly.

    Raises:
        ControlError: The action failed, or the helper did not answer.
    """
    _ask("kill", svc)


def freeze_xrootd(svc: str) -> None:
    """Stop the XRootD daemons in svc's container (SIGSTOP).

    The rest of the server runs on. XRootD's port takes connections, from
    the kernel's backlog, but XRootD answers nothing on them.

    Raises:
        ControlError: The action failed, as where svc runs no XRootD, or
            the helper did not answer.
    """
    _ask("freeze-xrootd", svc)


def thaw_xrootd(svc: str) -> None:
    """Resume the XRootD daemons in svc's container (SIGCONT).

    Raises:
        ControlError: The action failed, as where svc runs no XRootD, or
            the helper did not answer.
    """
    _ask("thaw-xrootd", svc)


def state(svc: str) -> str:
    """Return the state of svc's container, as `docker compose ps` says it.

    That is `running`, `exited`, `paused`, and so on; "" if it has no
    container. A server that exited on its own, as an origin that lost
    its storage may, shows here, and start() brings it back.

    Raises:
        ControlError: The helper did not answer, or could not look.
    """
    return _ask("state", svc).split("\n", maxsplit=1)[0].strip()


@contextlib.contextmanager
def restart_with(svc: str, files: Mapping[str, str], serves: bool = True) -> Generator[None, None, None]:
    """Restart svc with files as the last of its configuration layers.

    That layer wins over every other. Afterward, even on failure, remove
    the files and restart svc again, so that what comes later sees the
    shape as it was. If the restart or the block raised, a failed
    restore is only warned of, so as not to replace what was raised: a
    suite would catch a ControlError in place of test.py's Stalled, and
    carry on with a pass it was told to abandon.

    Args:
        svc: The Pelican server to restart.
        files: YAML text, by file name (e.g. `90-test.yaml`).
        serves: Whether to wait, as restart() does, until svc serves
            with files; a test that expects it not to looks for itself
            (see state()). Afterward, it waits either way.

    Raises:
        ControlError: There is no directory for svc under config/ (it is
            no Pelican server, or ./fed.sh has not made it), a name in
            files is not a configuration file's (CONFIG_FILE), or a
            restart failed.
    """
    directory = os.path.join(CONFIG, svc)
    if not os.path.isdir(directory):
        raise ControlError(
            f"framework/var/control/config/{svc} is missing: {svc} is no Pelican server,"
            + " or ./fed.sh has not made it"
        )
    for name in files:
        if not CONFIG_FILE.fullmatch(name):
            raise ControlError(f"not a configuration file's name: {name!r}")
    written: list[str] = []

    def restore() -> None:
        for path in written:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        restart(svc)

    try:
        for name, text in files.items():
            path = os.path.join(directory, name)
            written.append(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        if serves:
            restart(svc)
        else:
            _ask("restart", svc)
        yield
    except BaseException:
        try:
            restore()
        except ControlError as e:
            common.warn(f"restoring {svc}: {e}")
        raise
    restore()


@contextlib.contextmanager
def restart_all_with(files_by_svc: Mapping[str, Mapping[str, str]]) -> Generator[None, None, None]:
    """Run restart_with() for each service, in order, with its own files.

    Afterward, even on failure, each is restored, in reverse order.

    Args:
        files_by_svc: Each service's files, by service: YAML text, by
            file name.

    Raises:
        ControlError: A restart or a restore failed.
    """
    with contextlib.ExitStack() as stack:
        for svc, files in files_by_svc.items():
            stack.enter_context(restart_with(svc, files))
        yield
