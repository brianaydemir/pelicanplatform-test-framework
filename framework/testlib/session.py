"""What every suite shares in one pass of test.py.

That is the federation, the client and its environment, tokens, and a
scratch directory. test.py makes a Session for each token profile
(credentials.PROFILES), in framework/var, and runs each suite once with
each in turn: the tests' tokens are of the session's profile.

Tokens are minted once and kept: the server's own, for a suite's
checks, and those of the credential table (credentials.CREDENTIALS),
which the auth and transfers suites both present. A suite's setup, and
its checks of what the stores hold, use the server's own WLCG tokens
whatever the session's profile (setup_token()), so that each scenario's
twin tests only the token it presents. The expired tokens are minted as
the session starts, so that they are long expired by the time those
suites, which run late, present them.

pelican_cmd() and plugin_cmd() log each client command, with all it
printed, in the pass's commands_log. last_line() and the json_*
functions read what a client printed.
"""

import json
import os
import shlex
import subprocess  # nosec B404
import threading
import time
from collections.abc import Mapping
from typing import Any, cast

from . import common, credentials, report
from .credentials import RWM, WLCG, Credential

# How long a client command may run before it is killed and fails, and
# the exit status it then has (timeout(1)'s).
CLIENT_TIMEOUT = 600.0
TIMED_OUT = 124


def timed_out(seconds: float) -> str:
    """Return what a client run that was killed after seconds says."""
    return f"timed out after {seconds:.0f}s, and was killed"


def _text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output.decode(errors="replace") if isinstance(output, bytes) else output


def _read(path: str) -> str:
    """Return the text of the file at path, or "" if there is none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


class Session:
    """What every suite shares in one pass of test.py.

    Attributes:
        profile: The profile of the tests' tokens (see
            credentials.PROFILES).
        pelican: The pelican client's absolute path.
        plugin: The stash_plugin's absolute path.
        pelican_server: The pelican-server's absolute path; None if there
            is none, when only the scenarios that need it skip.
        run: The pass's ID: when the run started, as YYYYMMDD-HHMMSS, and
            for a profile other than WLCG's, `-sci`, so that the passes
            name their objects apart.
        tmp: The pass's scratch directory.
        env: The environment for a client that should present only the
            token it is given.
        sync_env: The same, for `object sync`, which wants overwrites off.
        namespaces: The namespaces that the federation exports.
        stale_at: When the last expired token expired.
        commands_log: Where pelican_cmd() and plugin_cmd() log each
            client command, under framework/var: data/commands.log, or
            data/commands-<profile>.log in another profile's pass.
    """

    def __init__(
        self,
        tmp: str,
        profile: str = WLCG,
        fed: common.Federation | None = None,
        run: str | None = None,
    ):
        """Start a pass.

        Args:
            tmp: Where to make the pass's scratch directory, named after
                its profile.
            profile: The profile of the tests' tokens.
            fed: The federation, if another pass already read it.
            run: When the run started, as YYYYMMDD-HHMMSS; now unless
                given.
        """
        self.fed = fed or common.Federation()
        self.profile = profile
        self.pelican = os.path.abspath(common.binary("pelican"))
        self.plugin = os.path.abspath(common.binary("stash_plugin"))
        server = common.server_binary()
        self.pelican_server = os.path.abspath(server) if server else None
        stamp = run or time.strftime("%Y%m%d-%H%M%S")
        self.run = stamp if profile == WLCG else f"{stamp}-sci"
        self.tmp = os.path.join(tmp, profile)
        os.makedirs(self.path("tokens"))
        self.env = credentials.client_env()
        self.sync_env = credentials.client_env(overwrites=False)
        self.namespaces: list[str] = credentials.exported(self.fed)
        self._server: dict[tuple[str, str, tuple[str, ...]], str] = {}
        self._credential: dict[tuple[str, str, str], str | None] = {}
        self.stale_at: float | None = None
        self.commands_log = f"{self.var_dir('data/commands')}.log"
        with open(self.commands_log, "w", encoding="utf-8"):
            pass
        self._log_lock = threading.Lock()
        for cred in credentials.CREDENTIALS:
            if cred.expired and not credentials.moot(self.fed, cred):
                for namespace in self.namespaces:
                    if credentials.applies(cred, namespace):
                        for op in ("get", "put"):
                            self.credential_file(cred, namespace, op)

    def path(self, name: str) -> str:
        """Return the path of name in the pass's scratch directory."""
        return os.path.join(self.tmp, name)

    def var_dir(self, name: str) -> str:
        """Return where the pass keeps what it would keep in name.

        name is a directory under framework/var, e.g. data/auth-test,
        which a suite empties as it starts: it is the WLCG pass's, and
        <name>-<profile> another's, so that one pass keeps what the other
        leaves.
        """
        return name if self.profile == WLCG else f"{name}-{self.profile}"

    # The server's own tokens.

    def token_file(self, namespace: str, scopes: tuple[str, ...] = RWM, profile: str | None = None) -> str:
        """Return the file of a `server` token for namespace with scopes.

        The token is of the session's profile, unless profile names one.
        """
        profile = profile or self.profile
        key = (profile, namespace, scopes)
        if key not in self._server:
            path = self.path(f"tokens/server.{profile}.{namespace}.{'-'.join(scopes)}")
            credentials.mint_server(self.fed, path, namespace, scopes, profile=profile)
            self._server[key] = path
        return self._server[key]

    def token(self, namespace: str, scopes: tuple[str, ...] = RWM, profile: str | None = None) -> str:
        """Return the same token itself."""
        with open(self.token_file(namespace, scopes, profile), encoding="utf-8") as f:
            return f.read().strip()

    def setup_token_file(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """Return the file of a `server` token of the WLCG profile.

        It is for a suite's setup, and its checks of what the stores hold,
        whatever the session's profile.
        """
        return self.token_file(namespace, scopes, WLCG)

    def setup_token(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """Return the same token itself."""
        return self.token(namespace, scopes, WLCG)

    def setup_tokens(self) -> dict[str, str]:
        """Return each exported namespace's setup_token(), by namespace."""
        return {ns: self.setup_token(ns) for ns in self.namespaces}

    # The credential table's tokens.

    def credential_file(self, cred: Credential, namespace: str, op: str) -> str | None:
        """Return the file of cred's token for op in namespace.

        op is `get` or `put`. The token is of the session's profile.

        Returns:
            The file; None for `none`, which has no token.
        """
        key = (cred.name, namespace, op)
        if key not in self._credential:
            path = self.path(f"tokens/{cred.name}.{namespace}.{op}")
            expiry = credentials.mint_credential(self.fed, cred, namespace, op, path, self.profile)
            self._credential[key] = None if expiry is None else path
            if cred.expired and expiry is not None:
                self.stale_at = max(self.stale_at or expiry, expiry)
        return self._credential[key]

    def credential_token(self, cred: Credential, namespace: str, op: str) -> str | None:
        """Return the same token itself, or None."""
        path = self.credential_file(cred, namespace, op)
        if path is None:
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def wait_until_stale(self) -> None:
        """Wait, if need be, until no server may accept the expired tokens.

        See credentials.wait_until_stale.
        """
        credentials.wait_until_stale(self.stale_at)

    # The client.

    def pelican_cmd(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = CLIENT_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run `pelican <args>` with no terminal.

        With no token, `pelican object` would otherwise try to acquire one
        interactively. A run that outlasts timeout is killed, and fails
        with "timed out" in its stderr. Each run goes in commands_log.

        Args:
            *args: The arguments to `pelican`.
            env: The environment, if not the session's env.
            cwd: The working directory, if not the scratch directory.
            timeout: How long the run may take, in seconds.

        Returns:
            Its exit status, stdout, and stderr.
        """
        started = time.monotonic()
        found = self._run([self.pelican, *args], env, cwd, timeout)
        self.log_command(["pelican", *args], cwd or self.tmp, time.monotonic() - started, *found)
        return found

    def plugin_cmd(
        self,
        infile: str,
        outfile: str,
        upload: bool = False,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = CLIENT_TIMEOUT,
    ) -> tuple[int, str, str, str]:
        """Run stash_plugin, as HTCondor does, on the transfers in infile.

        It writes an ad of each transfer's result to outfile. A run that
        outlasts timeout is killed, as pelican_cmd() kills one. Each run
        goes in commands_log, with its transfers and its ads.

        Args:
            infile: The transfers, a ClassAd each (transfers.transfer_ad()).
            outfile: Where it is to write its ads.
            upload: Whether it uploads (-upload) rather than downloads.
            env: The environment, if not the session's env.
            cwd: The working directory, if not the scratch directory.
            timeout: How long the run may take, in seconds.

        Returns:
            Its exit status, stdout, stderr, and ads ("" if it wrote none).
        """
        args = [*(["-upload"] if upload else []), "-infile", infile, "-outfile", outfile]
        started = time.monotonic()
        status, out, err = self._run([self.plugin, *args], env, cwd, timeout)
        ads = _read(outfile)
        files = {"infile": _read(infile), "outfile": ads}
        seconds = time.monotonic() - started
        self.log_command(["stash_plugin", *args], cwd or self.tmp, seconds, status, out, err, files)
        return status, out, err, ads

    def _run(
        self,
        command: list[str],
        env: dict[str, str] | None,
        cwd: str | None,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Run command, for pelican_cmd() or plugin_cmd()."""
        try:
            done = subprocess.run(  # nosec B603
                command,
                env=env or self.env,
                cwd=cwd or self.tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            return done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            return TIMED_OUT, _text(e.stdout), _text(e.stderr) + f"\n{timed_out(timeout)}\n"

    def log_command(
        self,
        args: list[str],
        cwd: str,
        seconds: float,
        status: int,
        out: str,
        err: str,
        files: Mapping[str, str] | None = None,
    ) -> None:
        """Append a command, its exit status, and its output to commands_log.

        files are the texts of files it read or wrote, by name, which go
        after its output. Each token's signature is cut out
        (report.redact()).
        """
        sections = [("stdout", out), ("stderr", err), *(files or {}).items()]
        said = "".join(f"--- {name}\n{text.rstrip()}\n" for name, text in sections if text.strip())
        entry = (
            f"==> {time.strftime('%Y-%m-%d %H:%M:%S')} {shlex.join(args)}\n"
            + f"    in {cwd}: exit {status} after {seconds:.1f}s\n{said}\n"
        )
        with self._log_lock, open(self.commands_log, "a", encoding="utf-8") as f:
            f.write(report.redact(entry))


def last_line(text: str) -> str:
    """Return the last line of a command's output that says something.

    Passed over are blank lines; the client's "Errors are retryable",
    which it logs after the error itself; and everything from a `Usage:`
    line on, which the client and server print after an error. The line
    is cut to 300 characters; "(no output)" if there is none.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    usage = next((i for i, line in enumerate(lines) if line.strip() == "Usage:"), len(lines))
    lines = lines[:usage] or lines
    said = [line for line in lines if not line.rstrip().endswith('"Errors are retryable"')]
    return (said or lines or ["(no output)"])[-1][:300]


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
    """Return the JSON in a command's stdout: its last line that parses.

    Raises:
        ValueError: No line parses.
    """
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("no JSON in the output")


def json_type(value: Any) -> str:
    """Return how a message names value's JSON type."""
    name = type(value).__name__
    return JSON_TYPES.get(name, name)


def json_object(text: str) -> dict[str, Any]:
    """Return last_json(), which must be an object.

    Raises:
        ValueError: There is no JSON, or it is not an object.
    """
    value = last_json(text)
    if not isinstance(value, dict):
        raise ValueError(f"the JSON is {json_type(value)}, not an object")
    return cast(dict[str, Any], value)


def json_objects(text: str) -> list[dict[str, Any]]:
    """Return last_json(), which must be an array of objects.

    Raises:
        ValueError: There is no JSON, or it is not an array of objects.
    """
    value = last_json(text)
    items = common.as_array(value)
    if items is None:
        raise ValueError(f"the JSON is {json_type(value)}, not an array")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("the JSON array holds more than objects")
    return cast(list[dict[str, Any]], items)
