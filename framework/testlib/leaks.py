"""Tokens whose signatures reach a client, or a log, in the clear.

A token's signature is what makes it good: whoever holds a token whole
may present it, as its holder, until it expires. What a server logs, or
says of a failure, and what a client prints of one, may quote a URL or
a header that carries a token, and so hand it to whoever reads them.
Its header and claims alone grant nothing, and say what it was, so only
a token whose signature is whole counts (find()).

test.py records two rows for each pass of a suite (docs/tests/README.md,
Token leaks):

- `token-leaks clients`: what reached the tests as a client. Of each
  HTTP answer from a Pelican server (PELICAN_HOST), its trailers, such
  as the X-Transfer-Status in which a V2 cache reports a failure after
  its headers; and its headers and body if its status is 4xx or 5xx,
  if it answers a COPY (whose body reports a third-party copy as it
  goes, and how it ended), or if it is of a cache's own APIs
  (CACHE_API_ROUTES) or of a route that reports what a server did in
  the background (STATUS_ROUTES), whatever its status (answer()). Of
  each client command, its stderr; its stdout, for the commands that
  report there what failed (reports_on_stdout()); the stash_plugin's
  ads; and the pelican client's --transfer-stats file (command()).
- `token-leaks logs`: the Pelican servers' logs, XRootD's lines
  included, from where the previous pass's read left off (when the run
  began, for the first) until the pass ended, which the control helper
  reads (logs(); framework/control.py).

A token that the tests themselves presented counts too: what is said of
a failure is kept, as in a job's log, long after the request.

This module imports nothing of the framework's: testlib.web reports each
answer to it, and the control helper, on the host, scans the logs with
it.
"""

import base64
import binascii
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

# The rows that test.py records for each pass of a suite.
CLIENTS_ROW = "token-leaks clients"
LOGS_ROW = "token-leaks logs"
ROWS = (CLIENTS_ROW, LOGS_ROW)

# A JWT in its compact form: a base64url header (which begins `{"`, so
# `eyJ`), claims, and what stands where the signature goes, which
# signed() judges.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# How many bytes each JWS algorithm's signature has (RFC 7518, and RFC
# 8037's EdDSA, of Ed25519). An RSA signature is as long as its key's
# modulus: RSA_SIGNATURE_BYTES at least, for a key of 1024 bits.
SIGNATURE_BYTES = {
    "HS256": 32,
    "HS384": 48,
    "HS512": 64,
    "ES256": 64,
    "ES384": 96,
    "ES512": 132,
    "EdDSA": 64,
}
RSA_SIGNATURE_BYTES = 128
# The least a signature of an algorithm not known here may be, in bytes:
# HS256's. Pelican's own redaction, `.REDACTED`, decodes to six.
LEAST_SIGNATURE_BYTES = 32

# The hosts whose answers count: the Pelican servers (testlib.control's
# PELICAN_SERVER), and `director`, the proxy in front of several
# directors, which passes on theirs. The framework's own services, such
# as the identity provider, do not count.
PELICAN_HOST = re.compile(r"(?:director|origin|cache)(?:-\d+)?|registry|transfer|fed")

# A cache's own APIs (local_cache/prestage_evict_api.go, and
# cache_api.go, in Pelican), whose answers count whatever their status:
# prestage streams `failure: <why>` lines in a 200.
CACHE_API_ROUTES = ("/pelican/api/v1.0/prestage", "/pelican/api/v1.0/evict", "/api/v1.0/cache")
# The routes that report what a server did in the background, failures
# included, in a 200: the transfer API's jobs (and their event
# streams), each component's health (whose message may be an error's,
# such as a self-test's, or the director's test's), the metadata
# publish queue (each event's `last_error`), and the servers that a
# director lists, with their status.
STATUS_ROUTES = (
    "/api/v1.0/transfer/jobs",
    "/api/v1.0/metrics/health",
    "/api/v1.0/origin_ui/metadata_queue",
    "/api/v1.0/director_ui/servers",
)

# The most of a body, or of a file, that is scanned, in bytes.
SCANNED = 16 << 20
# How many findings a row's note gives, and how much of a log's line a
# finding keeps, in characters.
NOTED = 3
QUOTED_LINE = 300


def _unb64(text: str) -> bytes:
    """Return the bytes of base64url text, unpadded, as JWS encodes them.

    Raises:
        binascii.Error: text is not base64url.
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _part(text: str) -> dict[str, Any]:
    """Return the JSON object that a token's header or claims encode; {}."""
    try:
        found = json.loads(_unb64(text))
    except (binascii.Error, ValueError):
        return {}
    return cast(dict[str, Any], found) if isinstance(found, dict) else {}


def signed(token: str) -> bool:
    """Return whether a compact JWT's signature is whole.

    It is whole if it decodes to as many bytes as its header's `alg`
    makes, or for an algorithm not known here, at least
    LEAST_SIGNATURE_BYTES. A token of `alg` none, one whose signature was
    cut out or cut short, as by a log's own redaction, and one whose
    header is not a JSON object, has none.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return False
    alg = _part(parts[0]).get("alg")
    if not isinstance(alg, str) or alg.lower() == "none":
        return False
    try:
        size = len(_unb64(parts[2]))
    except (binascii.Error, ValueError):
        return False
    if alg in SIGNATURE_BYTES:
        return size == SIGNATURE_BYTES[alg]
    if alg[:2] in ("RS", "PS"):
        return size >= RSA_SIGNATURE_BYTES
    return size >= LEAST_SIGNATURE_BYTES


def find(text: str) -> list[str]:
    """Return each token in text whose signature is whole, once each."""
    return list(dict.fromkeys(m.group(0) for m in _JWT.finditer(text) if signed(m.group(0))))


def unsigned(token: str) -> str:
    """Return a token's header and claims, without its signature."""
    return ".".join(token.split(".")[:2])


def describe(token: str) -> str:
    """Return who issued token, to whom, and for what, by its claims.

    The claims are taken as they are, unverified. A token's signature
    need not be there.
    """
    parts = token.split(".")
    claims = _part(parts[1]) if len(parts) > 1 else {}
    said: list[str] = []
    for name in ("iss", "sub", "scope"):
        value = claims.get(name)
        if value is not None:
            text = str(value)
            said.append(f"{name} {text[:80]}{'...' if len(text) > 80 else ''}")
    return ", ".join(said) or "no iss, sub, or scope"


@dataclass(frozen=True)
class Finding:
    """A token, whole, where it should not be.

    Attributes:
        where: Where it was: an answer, a command's output, or a log.
        token: Its header and claims, without its signature.
        note: More to say of it, briefly, such as that the request
            presented it, or how many lines of a log quote it; "" if
            nothing.
        quote: The first line of a log that quotes it, its signatures
            cut out; "" for what is not a log.
    """

    where: str
    token: str
    note: str = ""
    quote: str = ""

    def describe(self) -> str:
        """Return what it was, and where, for a message."""
        said = f"{self.where}: {describe(self.token)}"
        return f"{said} ({self.note})" if self.note else said


def _under(path: str, routes: Iterable[str]) -> bool:
    """Return whether path is one of routes, or below one."""
    return any(path == route or path.startswith(f"{route}/") for route in routes)


def reports_on_stdout(args: Sequence[str]) -> bool:
    """Return whether a client command reports on its stdout what failed.

    `pelican job` reports each of a job's transfers, its error included
    (cmd/job_status.go in Pelican); `pelican-server cache introspect` and
    `origin introspect`, what a server found (cmd/cache_introspect.go).

    Args:
        args: The command: the client's path or name, then its arguments.
    """
    if not args:
        return False
    name, words = os.path.basename(args[0]), list(args[1:3])
    if name == "pelican":
        return words[:1] == ["job"]
    if name == "pelican-server":
        return words in (["cache", "introspect"], ["origin", "introspect"])
    return False


def output_files(args: Sequence[str], cwd: str) -> list[tuple[str, str]]:
    """Return the files that a client command writes of what came of it.

    They are the pelican client's --transfer-stats, and the stash_plugin's
    -outfile, its ads.

    Args:
        args: The command: the client's path or name, then its arguments.
        cwd: Its working directory, from which a relative path is taken.

    Returns:
        Each file's name for a message, and its path.
    """
    if not args:
        return []
    name = os.path.basename(args[0])
    options = {
        "pelican": ("--transfer-stats", "--transfer-stats file"),
        "stash_plugin": ("-outfile", "ads"),
    }
    if name not in options:
        return []
    option, shown = options[name]
    found: list[tuple[str, str]] = []
    rest = list(args[1:])
    for n, arg in enumerate(rest):
        if arg == option and n + 1 < len(rest):
            found.append((shown, os.path.join(cwd, rest[n + 1])))
        elif arg.startswith(f"{option}="):
            found.append((shown, os.path.join(cwd, arg.split("=", 1)[1])))
    return found


def shown_command(args: Sequence[str]) -> str:
    """Return a client command's name and its first words, for a message.

    E.g. `pelican object get`, or `stash_plugin`.
    """
    if not args:
        return "a command"
    words = [os.path.basename(args[0])]
    for arg in args[1:]:
        if arg.startswith("-") or len(words) == 3 or "/" in arg:
            break
        words.append(arg)
    return f"`{' '.join(words)}`"


def _read(path: str) -> str:
    """Return the start of the file at path, as text; "" if there is none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(SCANNED)
    except OSError:
        return ""


class Watch:
    """What reached the tests as a client, scanned for tokens whole.

    testlib.web reports each answer, and testlib.session each client
    command; take() hands over what was found, and starts afresh. Suites
    report from several threads at once.

    Attributes:
        answers: How many answers were scanned, in part or whole.
        commands: How many commands' output was scanned.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._found: dict[tuple[str, str], Finding] = {}
        self.answers = 0
        self.commands = 0

    def _scan(self, where: str, parts: Iterable[tuple[str, str]], presented: str = "") -> None:
        """Record each token whole in parts, each of what was where.

        Args:
            where: Where they were.
            parts: Each part's name, such as `body`, and its text.
            presented: What the request sent, its URL and headers, so as
                to say of a token that it was the request's own.
        """
        for part, text in parts:
            for token in find(text):
                note = "the request's own" if token in presented else ""
                finding = Finding(f"{where}, in its {part}", unsigned(token), note)
                with self._lock:
                    self._found.setdefault((finding.where, finding.token), finding)

    def answer(
        self,
        method: str,
        url: str,
        sent: Mapping[str, str],
        status: int | None,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        trailers: Sequence[tuple[str, str]],
    ) -> None:
        """Scan what of an HTTP answer counts (see the module's docstring).

        Args:
            method: The request's method.
            url: Where it went.
            sent: Its headers.
            status: The answer's status; None if none came.
            headers: The answer's headers.
            body: The answer's body, or as much as was read.
            trailers: The answer's trailers.
        """
        parts = urlsplit(url)
        if status is None or not PELICAN_HOST.fullmatch(parts.hostname or ""):
            return
        path = parts.path or "/"
        scanned: list[tuple[str, str]] = []
        if (
            status >= 400
            or method == "COPY"
            or _under(path, CACHE_API_ROUTES)
            or _under(path, STATUS_ROUTES)
        ):
            scanned.append(("headers", "\n".join(f"{k}: {v}" for k, v in headers)))
            scanned.append(("body", body[:SCANNED].decode("utf-8", errors="replace")))
        if trailers:
            scanned.append(("trailers", "\n".join(f"{k}: {v}" for k, v in trailers)))
        if not scanned:
            return
        with self._lock:
            self.answers += 1
        presented = url + "\n" + "\n".join(f"{k}: {v}" for k, v in sent.items())
        where = f"HTTP {status} to {method} {parts.netloc}{path}"
        self._scan(where, scanned, presented)

    def command(self, args: Sequence[str], cwd: str, out: str, err: str) -> None:
        """Scan what of a client command's output counts.

        That is its stderr; its stdout, if reports_on_stdout(); and its
        output_files(), as they are once it has ended.

        Args:
            args: The command: the client's path or name, then its
                arguments.
            cwd: Its working directory.
            out: Its stdout.
            err: Its stderr.
        """
        scanned = [("stderr", err)]
        if reports_on_stdout(args):
            scanned.append(("stdout", out))
        scanned += [(shown, _read(path)) for shown, path in output_files(args, cwd)]
        with self._lock:
            self.commands += 1
        self._scan(shown_command(args), scanned)

    def take(self) -> tuple[list[Finding], int, int]:
        """Return what was found, and how many answers and commands were scanned.

        Then start afresh.
        """
        with self._lock:
            found = list(self._found.values())
            counts = self.answers, self.commands
            self._found.clear()
            self.answers = self.commands = 0
        return found, *counts


# What the suites' answers and commands are reported to.
WATCH = Watch()


def answer(
    method: str,
    url: str,
    sent: Mapping[str, str],
    status: int | None,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    trailers: Sequence[tuple[str, str]],
) -> None:
    """Report an HTTP answer to WATCH (Watch.answer())."""
    WATCH.answer(method, url, sent, status, headers, body, trailers)


def command(args: Sequence[str], cwd: str, out: str, err: str) -> None:
    """Report a client command's output to WATCH (Watch.command())."""
    WATCH.command(args, cwd, out, err)


def scan_log(service: str, text: str) -> tuple[int, list[dict[str, Any]]]:
    """Return how many lines a service's log has, and those with a token whole.

    For the control helper, which sends the result back as JSON. Each
    line is quoted with `<token>` in place of every token in it, whose
    claims the finding gives, and cut short.

    Returns:
        The number of lines, and for each line with a token whole, a
        JSON object of `service`, `line`, and `tokens` (each without its
        signature).
    """
    lines = text.splitlines()
    found: list[dict[str, Any]] = []
    for line in lines:
        tokens = find(line)
        if tokens:
            quoted = _JWT.sub("<token>", line)
            if len(quoted) > QUOTED_LINE:
                quoted = quoted[:QUOTED_LINE] + " ..."
            found.append({"service": service, "line": quoted, "tokens": [unsigned(t) for t in tokens]})
    return len(lines), found


def logs(found: Sequence[Mapping[str, Any]]) -> list[Finding]:
    """Return the findings of the lines that scan_log() found, once a token.

    A token that several lines of a service's log quote is one finding,
    which says how many, and quotes the first.
    """
    lines: dict[tuple[str, str], list[str]] = {}
    for entry in found:
        service, line = str(entry.get("service", "")), str(entry.get("line", ""))
        tokens: Any = entry.get("tokens")
        for token in cast(list[Any], tokens) if isinstance(tokens, list) else []:
            lines.setdefault((service, str(token)), []).append(line)
    return [
        Finding(f"{service}'s log", token, f"{len(quoted)} line(s)", quoted[0])
        for (service, token), quoted in lines.items()
    ]


def summary(found: Sequence[Finding]) -> str:
    """Return a row's note of findings: how many, and the first NOTED."""
    shown = [f.describe() for f in found[:NOTED]]
    more = f"; and {len(found) - NOTED} more" if len(found) > NOTED else ""
    return f"{len(found)} token(s) whole: " + "; ".join(shown) + more
