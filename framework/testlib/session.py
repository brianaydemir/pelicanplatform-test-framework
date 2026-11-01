"""What every suite shares in one pass of test.py.

That is the federation, the client and its environment, tokens, and a
scratch directory. test.py makes a Session for each token profile
(credentials.PROFILES), and runs each suite once with each in turn: the
tests' tokens are of the session's profile.

Tokens are minted once and kept: the server's own, for a suite's
checks; those of the credential table (credentials.CREDENTIALS), which
the auth and transfers suites both present; and those of each server's
web API (web_token()). A suite's setup, and
its checks of what the stores hold, use the server's own WLCG tokens
whatever the session's profile (setup_token()), so that a scenario's
twin in the other pass differs only in the token it presents. The
expired tokens are minted as the session starts, so that they are long
expired by the time those suites, which run late, present them.

Each client command, with all it printed, goes in the pass's
commands_log. last_line() and the json_* functions read what a client
printed.
"""

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from . import common, credentials, report, transfers
from .credentials import RWM, WLCG, Credential

# How long a client command may run before it is killed and fails, and
# the exit status it then has (timeout(1)'s).
CLIENT_TIMEOUT = 600.0
TIMED_OUT = 124
# How long before a web API token expires a Session mints it anew, in
# seconds: a suite that keeps one it was handed may use it this long.
WEB_TOKEN_RENEW = 3000


def timed_out(seconds: float) -> str:
    """Return what a client run that was killed after seconds says."""
    return f"timed out after {seconds:.0f}s, and was killed"


def _text(output: str | bytes | None) -> str:
    """Return what a killed run printed, as text."""
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
        fed: The federation.
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
        commands_log: Where each client command is logged, under
            framework/var: data/commands.log, or
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
            run: When the run started, as YYYYMMDD-HHMMSS, if not now.
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
        self._web: dict[tuple[str, str, tuple[str, ...], str], tuple[str, float]] = {}
        self.stale_at: float | None = None
        self.commands_log = f"{self.var_dir('data/commands')}.log"
        with open(self.commands_log, "w", encoding="utf-8"):
            pass
        self._log_lock = threading.Lock()
        # Suites ask for tokens from worker threads.
        self._mint_lock = threading.Lock()
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

        That is name in the WLCG pass, and <name>-<profile> in another,
        so that neither pass empties what the other keeps.

        Args:
            name: A directory under framework/var that a suite empties as
                it starts, e.g. data/auth-test.
        """
        return name if self.profile == WLCG else f"{name}-{self.profile}"

    # The server's own tokens.

    def token_file(self, namespace: str, scopes: tuple[str, ...] = RWM, profile: str | None = None) -> str:
        """Return the file of a `server` token for namespace with scopes.

        The token is of the session's profile, unless profile names one.
        """
        profile = profile or self.profile
        key = (profile, namespace, scopes)
        with self._mint_lock:
            if key not in self._server:
                path = self.path(f"tokens/server.{profile}.{namespace}.{'-'.join(scopes)}")
                credentials.mint_server(self.fed, path, namespace, scopes, profile=profile)
                self._server[key] = path
            return self._server[key]

    def token(self, namespace: str, scopes: tuple[str, ...] = RWM, profile: str | None = None) -> str:
        """Return token_file()'s token itself."""
        return common.read_text(self.token_file(namespace, scopes, profile))

    def setup_token_file(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """Return the file of a `server` token of the WLCG profile.

        It is for a suite's setup, and its checks of what the stores hold,
        whatever the session's profile.
        """
        return self.token_file(namespace, scopes, WLCG)

    def setup_token(self, namespace: str, scopes: tuple[str, ...] = RWM) -> str:
        """Return setup_token_file()'s token itself."""
        return self.token(namespace, scopes, WLCG)

    def setup_tokens(self) -> dict[str, str]:
        """Return each exported namespace's setup_token(), by namespace."""
        return {ns: self.setup_token(ns) for ns in self.namespaces}

    # The credential table's tokens.

    def credential_file(self, cred: Credential, namespace: str, op: str) -> str | None:
        """Return the file of cred's token for op in namespace.

        The token is of the session's profile.

        Args:
            cred: The credential.
            namespace: The namespace, e.g. `protected-a`.
            op: `get` or `put`.

        Returns:
            The file; None for `none`, which has no token.
        """
        key = (cred.name, namespace, op)
        with self._mint_lock:
            if key not in self._credential:
                path = self.path(f"tokens/{cred.name}.{namespace}.{op}")
                expiry = credentials.mint_credential(self.fed, cred, namespace, op, path, self.profile)
                self._credential[key] = None if expiry is None else path
                if cred.expired and expiry is not None:
                    self.stale_at = max(self.stale_at or expiry, expiry)
            return self._credential[key]

    def credential_token(self, cred: Credential, namespace: str, op: str) -> str | None:
        """Return credential_file()'s token itself, or None."""
        path = self.credential_file(cred, namespace, op)
        return None if path is None else common.read_text(path)

    # The servers' web APIs' tokens.

    def web_token_file(
        self,
        web_url: str,
        username: str = "admin",
        extra_scopes: tuple[str, ...] = (),
        key: str | None = None,
    ) -> str:
        """Return the file of username's token for the web API at web_url.

        It is credentials.mint_web_user()'s, lasting
        credentials.WEB_TOKEN_LIFETIME: minted once for each key, and anew
        once under WEB_TOKEN_RENEW seconds of that are left.

        Args:
            web_url: The server's web URL.
            username: Its subject.
            extra_scopes: Its scopes beside credentials.WEB_SCOPE.
            key: The file of the private key that signs it; the server's
                active signing key (credentials.key_dir_of()) if None.
        """
        key = key or common.signing_key(credentials.key_dir_of(self.fed, web_url))
        cached = (web_url, username, extra_scopes, key)
        with self._mint_lock:
            path, expiry = self._web.get(cached, ("", 0.0))
            if time.time() > expiry - WEB_TOKEN_RENEW:
                signer = os.path.splitext(os.path.basename(key))[0]
                name = ".".join((urlsplit(web_url).hostname or "", username, *extra_scopes, signer))
                path = self.path(f"tokens/web.{name}")
                expiry = time.time() + credentials.WEB_TOKEN_LIFETIME
                credentials.mint_web_user(
                    self.fed,
                    f"{path}.new",
                    web_url,
                    username,
                    credentials.WEB_TOKEN_LIFETIME,
                    extra_scopes,
                    key,
                )
                # Whole, since a client may be reading the old one.
                os.replace(f"{path}.new", path)
                self._web[cached] = (path, expiry)
            return path

    def web_token(
        self,
        web_url: str,
        username: str = "admin",
        extra_scopes: tuple[str, ...] = (),
        key: str | None = None,
    ) -> str:
        """Return web_token_file()'s token itself."""
        return common.read_text(self.web_token_file(web_url, username, extra_scopes, key))

    def wait_until_stale(self) -> None:
        """Wait, if need be, until no server may accept the expired tokens.

        See credentials.wait_until_stale().
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

    def server_cmd(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = CLIENT_TIMEOUT,
    ) -> tuple[int, str, str]:
        """Run `pelican-server <args>`, as pelican_cmd() runs `pelican`.

        Args:
            *args: The arguments to `pelican-server`.
            env: The environment, if not the session's env.
            cwd: The working directory, if not the scratch directory.
            timeout: How long the run may take, in seconds.

        Returns:
            Its exit status (127 if there is no pelican-server), stdout,
            and stderr.
        """
        if self.pelican_server is None:
            return 127, "", "no pelican-server; run ./fed.sh init"
        started = time.monotonic()
        found = self._run([self.pelican_server, *args], env, cwd, timeout)
        self.log_command(["pelican-server", *args], cwd or self.tmp, time.monotonic() - started, *found)
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

    def plugin_run(
        self,
        name: str,
        ads: Sequence[tuple[str, str]],
        creds: Mapping[str, str] | None = None,
        upload: bool = False,
        cache: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> tuple[int, str, str, str]:
        """Run plugin_cmd() on transfers of its own, as HTCondor would.

        Its input and output are <name>.in and <name>.ad in tmp, and its
        credentials directory (_CONDOR_CREDS) <name>.creds.

        Args:
            name: The name of its files, e.g. `<suite>/<transfer>`, under a
                suite's own directory (path()).
            ads: Each transfer's URL and local file
                (transfers.transfer_ad()).
            creds: The token files to put in its credentials directory, by
                the name each is to have there, e.g. scitokens.use.
            upload: Whether it uploads rather than downloads.
            cache: The cache it is to use alone (Client.PreferredCaches).
            env: The environment, if not the session's.
            cwd: The working directory, if not the scratch directory.

        Returns:
            What plugin_cmd() returns.
        """
        infile, outfile = (os.path.join(self.tmp, f"{name}.{ext}") for ext in ("in", "ad"))
        with open(infile, "w", encoding="utf-8") as f:
            f.writelines(transfers.transfer_ad(url, local) for url, local in ads)
        environ = dict(self.env if env is None else env)
        if creds is not None:
            directory = os.path.join(self.tmp, f"{name}.creds")
            os.makedirs(directory, exist_ok=True)
            for creds_name, token_file in creds.items():
                shutil.copyfile(token_file, os.path.join(directory, creds_name))
            environ["_CONDOR_CREDS"] = directory
        if cache:
            environ["PELICAN_CLIENT_PREFERREDCACHES"] = cache
        return self.plugin_cmd(infile, outfile, upload, environ, cwd)

    def _run(
        self,
        command: list[str],
        env: dict[str, str] | None,
        cwd: str | None,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Run command, for pelican_cmd(), server_cmd(), or plugin_cmd()."""
        try:
            done = subprocess.run(
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
        """Append a command, its exit status, and output to commands_log.

        Each token's signature is cut out (report.redact()).

        Args:
            args: The command.
            cwd: Its working directory.
            seconds: How long it ran.
            status: Its exit status.
            out: Its stdout.
            err: Its stderr.
            files: The texts of files it read or wrote, by name, to log
                after its output.
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
    line on, which the client and server print after an error.

    Returns:
        The line, cut to 300 characters; "(no output)" if there is none.
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
