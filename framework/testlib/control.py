"""Controlling the federation's services in the middle of a run:
restarting, stopping, starting, pausing, unpausing, or killing one, or
restarting one with configuration of a test's own.

The dev container has no Docker, so a helper on the host,
framework/control.py, acts for the tests: `./fed.sh test` runs one
beside them, and `./fed.sh control` runs one for a test.py run by hand.
They talk through files under framework/var/control/:

  serving             the helper's process ID, while one serves
  requests/<id>.req   a request: {"action": ..., "service": ...}
  requests/<id>.res   the helper's answer: {"ok": ..., "output": ...}
  config/<service>/   configuration of a test's own: the last of the
                      service's configuration layers
                      (/fed/control.instance; see framework/etc/pelican.yaml),
                      and empty but while restart_with() runs

The helper takes the actions in ACTIONS, one at a time, on the shape's
services but `dev`, and refuses anything else. Each call here returns
once its action is done, and raises ControlError if it failed. A Pelican
server that is restarted, started, or unpaused must also answer on its
web port again within SERVE_WAIT seconds. A scenario should put back
whatever it stops, pauses, kills, or configures, even if it fails; the
helper puts back what is left only when it stops serving.
"""

import contextlib
import functools
import json
import os
import re
import time
import uuid
from collections.abc import Generator, Mapping
from typing import Optional

from . import common, web

DIR = os.path.join(common.VAR, "control")
SERVING = os.path.join(DIR, "serving")
REQUESTS = os.path.join(DIR, "requests")
CONFIG = os.path.join(DIR, "config")

# What the helper does, each by `docker compose <action> <service>` but
# `ping`, which only answers; and what puts back what an action leaves
# undone.
ACTIONS = ("ping", "restart", "stop", "start", "pause", "unpause", "kill")
UNDO = {"stop": "start", "kill": "start", "pause": "unpause"}

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

# The Pelican servers, each of which is the host of its name, with its
# web port at 8444 (config.d/base/20-server.yaml); `fed` is the tiny
# topology's.
PELICAN_SERVER = re.compile(r"(?:director|origin|cache)-\d+|registry|fed")

# What a file of restart_with() may be named.
CONFIG_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.yaml")


class ControlError(Exception):
    """An action failed, or the helper did not answer."""


def _ask(action: str, service: str, seconds: float = ANSWER_WAIT) -> str:
    """Ask the helper to do action to service: what it said."""
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
def unavailable() -> Optional[str]:
    """Why the tests can't control the services here, or None: no helper
    serves this run (as when test.py runs by hand in the dev container,
    without `./fed.sh control`), or it does not answer. Asked once."""
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


def pelican_server(svc: str) -> bool:
    """Whether svc is a Pelican server."""
    return PELICAN_SERVER.fullmatch(svc) is not None


def answers(svc: str) -> bool:
    """Whether Pelican server svc answers on its web port, whatever it
    says, as its health check asks (framework/service-health.sh)."""
    url = f"https://{svc}:8444/api/v1.0/health"
    return web.request("GET", url, timeout=5).status is not None


def wait_serving(svc: str, seconds: float = SERVE_WAIT) -> None:
    """Wait until svc, if it is a Pelican server, answers on its web
    port; raise ControlError if it doesn't within seconds."""
    if pelican_server(svc) and not common.wait_for(lambda: answers(svc), seconds, interval=1):
        raise ControlError(f"{svc} did not answer on its web port within {seconds:.0f}s")


def restart(svc: str) -> None:
    """Restart svc (`docker compose restart`), and wait until it serves."""
    _ask("restart", svc)
    wait_serving(svc)


def stop(svc: str) -> None:
    """Stop svc (`docker compose stop`): SIGTERM, then SIGKILL."""
    _ask("stop", svc)


def start(svc: str) -> None:
    """Start svc, stopped or killed, and wait until it serves."""
    _ask("start", svc)
    wait_serving(svc)


def pause(svc: str) -> None:
    """Freeze svc's processes (`docker compose pause`): its ports take
    connections, but nothing answers."""
    _ask("pause", svc)


def unpause(svc: str) -> None:
    """Thaw svc, and wait until it serves."""
    _ask("unpause", svc)
    wait_serving(svc)


def kill(svc: str) -> None:
    """Kill svc (`docker compose kill`: SIGKILL), with no chance to shut
    down cleanly."""
    _ask("kill", svc)


@contextlib.contextmanager
def restart_with(svc: str, files: Mapping[str, str]) -> Generator[None, None, None]:
    """Restart svc with files, YAML text by file name (e.g.
    `90-test.yaml`), as the last of its configuration layers, which wins
    over every other; and afterward, even on failure, remove them and
    restart it again, so that what comes later sees the shape as it
    was."""
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
    try:
        for name, text in files.items():
            path = os.path.join(directory, name)
            written.append(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        restart(svc)
        yield
    finally:
        for path in written:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        restart(svc)
