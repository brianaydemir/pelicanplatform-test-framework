"""What every suite shares in one run of test.py: the federation, the
client and its environment, tokens, and a scratch directory.

test.py makes one Session, in framework/var, and hands it to each suite
in turn. Tokens are minted once and kept: the server's own, for a
suite's setup and checks, and those of the credential table
(credentials.CREDENTIALS), which the auth and transfers suites both
present. The expired ones are minted as the session starts, so that they
are long past their expiry by the time those suites, which run late,
present them. last_line() and the json_* functions read what a client
printed.
"""

import json
import os
import subprocess  # nosec B404
import time
from typing import Any, Optional, Union, cast

from . import common, credentials
from .credentials import RWM, Credential

# How long a client command may run before it is killed and fails, and
# the exit status it then has (timeout(1)'s).
CLIENT_TIMEOUT = 600.0
TIMED_OUT = 124


def timed_out(seconds: float) -> str:
    """What a client run that was killed after seconds says."""
    return f"timed out after {seconds:.0f}s, and was killed"


def _text(output: Union[str, bytes, None]) -> str:
    if output is None:
        return ""
    return output.decode(errors="replace") if isinstance(output, bytes) else output


class Session:
    """What every suite shares in one run of test.py."""

    def __init__(self, tmp: str):
        self.fed = common.Federation()
        self.pelican = os.path.abspath(common.binary("pelican"))
        self.plugin = os.path.abspath(common.binary("stash_plugin"))
        # None if there is none: only the scenarios that need it skip.
        server = common.server_binary()
        self.pelican_server = os.path.abspath(server) if server else None
        self.run = time.strftime("%Y%m%d-%H%M%S")
        self.tmp = tmp
        os.makedirs(self.path("tokens"))
        # For a client that should present only the token it is given; and
        # for `object sync`, which wants overwrites off.
        self.env = credentials.client_env()
        self.sync_env = credentials.client_env(overwrites=False)
        self.namespaces: list[str] = credentials.exported(self.fed)
        self._server: dict[tuple[str, tuple[str, ...]], str] = {}
        self._credential: dict[tuple[str, str, str], Optional[str]] = {}
        # When the last expired token expired.
        self.stale_at: Optional[float] = None
        for cred in credentials.CREDENTIALS:
            if cred.expired and not credentials.moot(self.fed, cred):
                for namespace in self.namespaces:
                    if credentials.applies(cred, namespace):
                        for op in ("get", "put"):
                            self.credential_file(cred, namespace, op)

    def path(self, name: str) -> str:
        """name in the session's scratch directory."""
        return os.path.join(self.tmp, name)

    # The server's own tokens.

    def token_file(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """A `server` token for namespace with scopes: its file."""
        key = (namespace, scopes)
        if key not in self._server:
            path = self.path(f"tokens/server.{namespace}.{'-'.join(scopes)}")
            credentials.mint_server(self.fed, path, namespace, scopes)
            self._server[key] = path
        return self._server[key]

    def token(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """The same token itself."""
        with open(self.token_file(namespace, scopes), encoding="utf-8") as f:
            return f.read().strip()

    def tokens(self) -> dict[str, str]:
        """A `server` token for each exported namespace, by namespace."""
        return {ns: self.token(ns) for ns in self.namespaces}

    # The credential table's tokens.

    def credential_file(self, cred: Credential, namespace: str, op: str) -> Optional[str]:
        """cred's token for op (`get` or `put`) in namespace: its file, or
        None for `none`, which has no token."""
        key = (cred.name, namespace, op)
        if key not in self._credential:
            path = self.path(f"tokens/{cred.name}.{namespace}.{op}")
            expiry = credentials.mint_credential(self.fed, cred, namespace, op, path)
            self._credential[key] = None if expiry is None else path
            if cred.expired and expiry is not None:
                self.stale_at = max(self.stale_at or expiry, expiry)
        return self._credential[key]

    def credential_token(self, cred: Credential, namespace: str, op: str) -> Optional[str]:
        """The same token itself, or None."""
        path = self.credential_file(cred, namespace, op)
        if path is None:
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def wait_until_stale(self) -> None:
        """Wait, if need be, until no server may accept the expired tokens
        (see credentials.wait_until_stale)."""
        credentials.wait_until_stale(self.stale_at)

    # The client.

    def pelican_cmd(
        self,
        *args: str,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = CLIENT_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run `pelican <args>` with no terminal: its exit status, stdout,
        and stderr. With no token, `pelican object` would otherwise try to
        acquire one interactively. A run that outlasts timeout is killed,
        and fails with "timed out" in its stderr."""
        try:
            done = subprocess.run(  # nosec B603
                [self.pelican, *args],
                env=env or self.env,
                cwd=cwd or self.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            return TIMED_OUT, _text(e.stdout), _text(e.stderr) + f"\n{timed_out(timeout)}\n"
        return done.returncode, done.stdout, done.stderr


def last_line(text: str) -> str:
    """The last line of a command's output that isn't blank, briefly."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else "(no output)"


# How errors name a JSON value's type, by its Python type's name.
JSON_TYPES = {
    "dict": "an object",
    "list": "an array",
    "str": "a string",
    "int": "a number",
    "float": "a number",
    "bool": "a boolean",
    "NoneType": "null",
}


def last_json(text: str) -> Any:
    """The JSON a command printed on stdout: its last line that parses.
    Raises ValueError if none does."""
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("no JSON in the output")


def json_type(value: Any) -> str:
    """How a message names value's JSON type."""
    name = type(value).__name__
    return JSON_TYPES.get(name, name)


def json_object(text: str) -> dict[str, Any]:
    """last_json(), which must be an object. Raises ValueError if it
    isn't."""
    value = last_json(text)
    if not isinstance(value, dict):
        raise ValueError(f"the JSON is {json_type(value)}, not an object")
    return cast(dict[str, Any], value)


def json_objects(text: str) -> list[dict[str, Any]]:
    """last_json(), which must be an array of objects. Raises ValueError
    if it isn't."""
    value = last_json(text)
    items = common.as_array(value)
    if items is None:
        raise ValueError(f"the JSON is {json_type(value)}, not an array")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("the JSON array holds more than objects")
    return cast(list[dict[str, Any]], items)
