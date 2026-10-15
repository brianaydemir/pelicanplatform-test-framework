"""The client-server contracts that only show through the clients.

These are the result ads that stash_plugin writes for HTCondor, which decide
whether a job retries; failing over from one cache to another partway
through a download, which needs the servers' ETags to agree; which token the
client presents, of those its environment offers; how `pelican token create`
finds an issuer; and the URL that `pelican object share` hands out. The
suite reads what the clients report.

The suite's objects go under /<namespace>/data/client/<run>/, put at
every origin, and are removed afterward. It needs /protected-a. The
clients present tokens of the session's profile, and
`token-create-discovery` mints its tokens in it; the objects are put in
place, the caches filled, and the stores read, with WLCG tokens either
way. The scenarios:

  plugin-ads        stash_plugin's result ads: the bytes, URL, type,
                    name, and times of a download; its attempts, the
                    cache's age of the object, and the server's
                    checksums; what a missing object, a refusal, and a
                    mixed batch say; and the exit status
  failover          with a V2 cache beside another, of either kind: a
                    download that fails partway at the first cache, whose
                    copy its chaos API spoiled, resumes at the second, by
                    Range, with the ETag unchanged; the plugin's ad
                    records both attempts
  etag-agreement    every server reports one ETag for an object, by HEAD
                    and by GET, and each cache's is one of the origins'
  token-discovery   which token the client presents: BEARER_TOKEN,
                    BEARER_TOKEN_FILE, _CONDOR_CREDS, a token the
                    director's hints rule out, one that looks right but
                    is not, and the token file a URL's scheme prefix
                    names
  token-create-discovery
                    `pelican token create` without --issuer or
                    --scope-path: the issuer whose keys match the signing
                    key, the scope from the path, and what it says when
                    no issuer matches, or none is needed
  share-url         `pelican object share`: a URL carrying a token of the
                    collection's read alone, which a plain client follows
                    through a director, and the Pelican client takes
  director-decision the plugin records the director's decision when
                    asked (Plugin.DirectorDecisionPercentage)
  skipstat-failover with several origins: a direct read with ?skipstat
                    of an object one origin alone holds comes back
  multi-source      `get` and `put` with several sources (rows G8-G10
                    and P8-P10 of Pelican's
                    docs/object-transfer-semantics.md), and ?recursive in
                    place of -r
  preferred-cache-down
                    a preferred cache that is down: with `+`, a director's
                    cache serves instead; without, the client fails to
                    reach it
  async-job         `get --async` and `copy --async`, which the client
                    agent runs: the bytes, `pelican job status` and
                    `list`, a finished job cancelled, and the agent
                    stopped
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Union
from urllib.parse import quote, urlsplit

from testlib import (
    blocks,
    cacheapi,
    common,
    credentials,
    director,
    issuer,
    jwt,
    stores,
    transfers,
    web,
)
from testlib.common import die, first
from testlib.issuer import User
from testlib.report import FAIL, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

SCENARIOS = {
    "plugin-ads": (
        "stash_plugin's result ads: bytes, URL, type, name, times, attempts, cache age,"
        " checksums, and what failures say"
    ),
    "failover": (
        "a download that fails partway at one cache resumes at the next, by Range, with the"
        " ETag unchanged (a V2 cache beside another)"
    ),
    "etag-agreement": "every server reports one ETag by HEAD and by GET; a cache's is an origin's",
    "token-discovery": (
        "which token the client presents: BEARER_TOKEN, BEARER_TOKEN_FILE, _CONDOR_CREDS, the"
        " director's hints, and a URL's scheme prefix"
    ),
    "token-create-discovery": (
        "`pelican token create` finds the issuer whose keys match the signing key, and says when"
        " none does, or none is needed"
    ),
    "share-url": (
        "`pelican object share` hands out a URL whose token reads the collection: through a"
        " director as curl, and by the client"
    ),
    "director-decision": "the plugin records the director's decision when asked",
    "skipstat-failover": (
        "a direct read with ?skipstat of an object one origin alone holds comes back"
        " (several origins)"
    ),
    "multi-source": (
        "`get` and `put` with several sources: a directory, a collection, or refused"
        " (G8-G10, P8-P10); and ?recursive"
    ),
    "preferred-cache-down": (
        "a preferred cache that is down: with `+`, a director's cache serves; without, a"
        " contact error"
    ),
    "async-job": (
        "`get --async` and `copy --async` through the client agent: the bytes, `job status`"
        " and `list`, a finished job cancelled, and the agent stopped"
    ),
}

NAMESPACE = "protected-a"
# The scenarios that present no token of the tests' minting (see
# suites/__init__.py): the share's is the embedded issuer's.
ONE_PROFILE = ("share-url",)
# A row: its case (appended to the scenario's name, if any), its status,
# and its note.
Row = tuple[str, str, str]

OBJECT_SIZE = 100_000
# `failover`'s object, and the block that cache-0's copy loses: in its
# middle, past what the cache reads before it answers, so that the
# download fails after it began.
FAILOVER_SIZE = 1 << 20
FAILOVER_BLOCK = 128
# How long a V2 cache may take to say, with Age, that it holds an object
# read whole.
HELD_WAIT = 30
# How many times skipstat-failover reads, looking for a try at the origin
# that lacks the object.
SKIPSTAT_TRIES = 5
# The plugin's exit statuses (cmd/plugin.go).
RETRYABLE_EXIT = 11
# The error the client reports when it has no token to send
# (error_codes: Authorization.TokenNotFound).
NO_TOKEN = 4010


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test, or None if there is something.

    The suite needs /protected-a.
    """
    if NAMESPACE not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def outcome(problems: Sequence[str], note: str) -> Row:
    """Return a row with no case: FAIL if there are problems, else PASS.

    Its note is the first of problems, or else note.
    """
    return ("", FAIL, first(problems)) if problems else ("", PASS, note)


# --------------------------------------------------------------------------
# ClassAds, as the plugin writes its result ads (one per line of the
# outfile, in the new ClassAd syntax): `[ Name = value; ... ]`, where a
# value is a number, a string in double quotes, true, false, undefined, a
# nested ad, or a list in braces.


class Ad(dict[str, Any]):
    """A parsed ClassAd: its attributes, by name as written."""

    def get_ci(self, name: str) -> Any:
        """Return the attribute named name, in any case, or None."""
        for key, value in self.items():
            if key.lower() == name.lower():
                return value
        return None


class _AdParser:
    """A recursive-descent parser of ClassAd text."""

    def __init__(self, text: str):
        self.text = text
        self.at = 0

    def skip_space(self) -> None:
        """Pass over blanks."""
        while self.at < len(self.text) and self.text[self.at].isspace():
            self.at += 1

    def peek(self) -> str:
        """Return the next character, or ""."""
        self.skip_space()
        return self.text[self.at] if self.at < len(self.text) else ""

    def expect(self, char: str) -> None:
        """Consume char, which must come next."""
        if self.peek() != char:
            raise ValueError(f"expected {char!r} at {self.at} in {self.text[:80]!r}")
        self.at += 1

    def ad(self) -> Ad:
        """Parse an ad, from its `[` to its `]`."""
        self.expect("[")
        found = Ad()
        while self.peek() != "]":
            name = self.name()
            self.expect("=")
            found[name] = self.value()
            if self.peek() == ";":
                self.at += 1
        self.expect("]")
        return found

    def name(self) -> str:
        """Parse an attribute's name."""
        self.skip_space()
        m = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", self.text[self.at :])
        if not m:
            raise ValueError(f"expected a name at {self.at} in {self.text[:80]!r}")
        self.at += m.end()
        return m.group(0)

    def value(self) -> Any:
        """Parse a value."""
        char = self.peek()
        if char == "[":
            return self.ad()
        if char == "{":
            self.at += 1
            items: list[Any] = []
            while self.peek() != "}":
                items.append(self.value())
                if self.peek() == ",":
                    self.at += 1
            self.expect("}")
            return items
        if char == '"':
            return self.string()
        m = re.match(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|[A-Za-z_]+", self.text[self.at :])
        if not m:
            raise ValueError(f"expected a value at {self.at} in {self.text[:80]!r}")
        self.at += m.end()
        word = m.group(0)
        if word.lower() == "true":
            return True
        if word.lower() == "false":
            return False
        if word.lower() in ("undefined", "error"):
            return None
        try:
            return int(word)
        except ValueError:
            return float(word)

    def string(self) -> str:
        """Parse a string in double quotes, with backslash escapes."""
        self.expect('"')
        found: list[str] = []
        while self.at < len(self.text):
            char = self.text[self.at]
            self.at += 1
            if char == '"':
                return "".join(found)
            if char == "\\" and self.at < len(self.text):
                escaped = self.text[self.at]
                self.at += 1
                found.append({"n": "\n", "t": "\t"}.get(escaped, escaped))
            else:
                found.append(char)
        raise ValueError("an unterminated string")


def parse_ads(text: str) -> list[Ad]:
    """Return every ad in text, the plugin's outfile.

    Raises:
        ValueError: The text cannot be read as ads.
    """
    parser = _AdParser(text)
    found: list[Ad] = []
    while parser.peek() == "[":
        found.append(parser.ad())
    if parser.peek():
        raise ValueError(f"unexpected {parser.text[parser.at : parser.at + 40]!r}")
    return found


def developer_data(ad: Ad) -> Ad:
    """Return an ad's DeveloperData, or an empty ad."""
    found = ad.get_ci("DeveloperData")
    return found if isinstance(found, Ad) else Ad()


def error_types(ad: Ad) -> list[tuple[str, int]]:
    """Return each error of an ad's TransferErrorData.

    Returns:
        Each error's ErrorType and its PelicanErrorCode (0 if none).
    """
    found: list[tuple[str, int]] = []
    for item in common.as_array(ad.get_ci("TransferErrorData")) or []:
        if isinstance(item, Ad):
            data = developer_data(item)
            code = data.get_ci("PelicanErrorCode")
            found.append((str(item.get_ci("ErrorType") or ""), int(code) if code else 0))
    return found


# --------------------------------------------------------------------------


@dataclass
class Fetched:
    """How a client run went.

    Attributes:
        status: Its exit status.
        said: Its output (the plugin's ads, if it wrote any).
        directory: The directory it ran in.
    """

    status: int
    said: str
    directory: str

    def files(self) -> list[str]:
        """Return what the directory holds."""
        try:
            return sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []


class Test:
    """The federation, the run's objects, and how the clients run.

    Attributes:
        base: Where the run's objects are in a namespace.
        tmp: The run's local directory, for the clients' files.
        token: The `server` token for /protected-a, of the session's
            profile.
        token_file: Its file.
        setup: The `server` token for /protected-a, of the WLCG profile,
            for putting objects in place, filling the caches, and reading
            the stores.
        caps: The capabilities of /protected-a.
        direct: Whether /protected-a takes direct clients.
        writes: Whether /protected-a takes writes.
        objects: What the run put in /protected-a, by rel within the
            namespace.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.base = f"data/client/{self.run}"
        self.tmp = session.path("client")
        os.makedirs(self.tmp)
        self.token = session.token(NAMESPACE)
        self.token_file = session.token_file(NAMESPACE)
        self.setup = session.setup_token(NAMESPACE)
        self.caps = self.fed.exports[NAMESPACE]
        self.direct = "DirectReads" in self.caps
        self.writes = "Writes" in self.caps
        self.objects: dict[str, bytes] = {}
        self._runs = 0

    def rel(self, name: str) -> str:
        """Return where object name of the run is in a namespace."""
        return f"{self.base}/{name}"

    def url(self, name: str, namespace: str = NAMESPACE, query: str = "") -> str:
        """Return the federation URL of object name of the run in namespace.

        Its query string is query, if any.
        """
        return f"{self.fed.url}/{namespace}/{self.rel(name)}" + (f"?{query}" if query else "")

    def path(self, name: str, namespace: str = NAMESPACE) -> str:
        """Return the path in the federation of object name of the run."""
        return f"/{namespace}/{self.rel(name)}"

    def put(
        self,
        name: str,
        data: bytes,
        namespace: str = NAMESPACE,
        origins: Optional[Sequence[common.Origin]] = None,
    ) -> None:
        """Put object name, holding data, in namespace's storage.

        Put it at every origin, or at origins (see stores.seed()): on a
        pstore origin, /public's is /protected-a's.
        """
        target = self.fed.storage_namespace(namespace)
        token = self.session.setup_token(target)
        caps = self.fed.exports[target]
        rel = self.rel(name)
        where = list(origins) if origins is not None else stores.unique(self.fed.origins)
        errors: list[str] = []
        for origin in where:
            errors += stores.make_dirs([origin], target, rel.rsplit("/", 1)[0], token)
            if not errors:
                error = stores.seed(origin, target, rel, data, token, caps)
                errors += [error] if error else []
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")
        if namespace == NAMESPACE:
            self.objects[rel] = data

    def directory(self, name: str) -> str:
        """Return a new directory of the run's for a client run."""
        self._runs += 1
        path = os.path.join(self.tmp, f"{self._runs:03d}-{name}")
        os.makedirs(path)
        return path

    def local(self, name: str, data: bytes) -> str:
        """Write data to local file name; return its path."""
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def creds(self, name: str, tokens: Mapping[str, str]) -> str:
        """Make a credentials directory (_CONDOR_CREDS); return its path.

        It is the run's, and holds each of tokens under its file name.
        """
        path = os.path.join(self.tmp, f"{name}.creds")
        os.makedirs(path, exist_ok=True)
        for file_name, token in tokens.items():
            with open(os.path.join(path, file_name), "w", encoding="utf-8") as f:
                f.write(token)
        return path

    def pelican(
        self,
        name: str,
        args: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> Fetched:
        """Run `pelican <args>`, in a directory of its own.

        Args:
            name: The name of its directory.
            args: The arguments.
            env: Its environment, or None for the session's.
            cwd: A directory to run in, in place of one of its own.
        """
        directory = cwd or self.directory(name)
        status, out, err = self.session.pelican_cmd(
            *args, env=dict(env) if env is not None else None, cwd=directory
        )
        return Fetched(status, out + err, directory)

    def plugin(
        self,
        name: str,
        urls: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        upload: bool = False,
        sources: Optional[Sequence[str]] = None,
    ) -> Fetched:
        """Run stash_plugin's transfer of urls, in a directory of its own.

        Args:
            name: The name of its directory.
            urls: What to transfer: each to the file of its last
                component, or for an upload, from sources.
            env: Its environment, or None for the session's.
            upload: Whether it is an upload.
            sources: For an upload, the local file of each of urls.

        Returns:
            Its exit status, its ads (or its output, if it wrote none), and
            the directory.
        """
        directory = self.directory(name)
        infile = os.path.join(self.tmp, f"{name}.in")
        outfile = os.path.join(self.tmp, f"{name}.ad")
        with open(infile, "w", encoding="utf-8") as f:
            for n, url in enumerate(urls):
                local = sources[n] if sources else transfers.local_name(url)
                f.write(f'[ Url="{url}"; LocalFileName="{local}" ]\n')
        command = [self.session.plugin]
        if upload:
            command.append("-upload")
        command += ["-infile", infile, "-outfile", outfile]
        try:
            done = subprocess.run(  # nosec B603
                command,
                cwd=directory,
                env=dict(env) if env is not None else self.session.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=CLIENT_TIMEOUT,
                check=False,
            )
            status, output = done.returncode, done.stderr or done.stdout
        except subprocess.TimeoutExpired:
            status, output = -1, f"timed out after {CLIENT_TIMEOUT:.0f}s"
        try:
            with open(outfile, encoding="utf-8") as f:
                ads = f.read()
        except FileNotFoundError:
            ads = ""
        return Fetched(status, ads or output, directory)

    def creds_env(self, creds: str, **extra: str) -> dict[str, str]:
        """Return the session's environment, with _CONDOR_CREDS at creds.

        Args:
            creds: A credentials directory.
            **extra: More variables to set.
        """
        env = dict(self.session.env)
        env["_CONDOR_CREDS"] = creds
        env.update(extra)
        return env

    def server_creds(self, name: str) -> str:
        """Return a credentials directory holding the server's token.

        It holds the token for /protected-a as scitokens.use, which the
        plugin tries first.
        """
        return self.creds(name, {"scitokens.use": self.token})

    def v2_caches(self) -> list[common.Cache]:
        """Return the federated V2 caches."""
        return [c for c in self.fed.federated_caches if c.kind == "v2"]

    def ask(self, method: str, server_url: str, name: str) -> web.Response:
        """Request object name of the run, by method, straight at a server.

        The server is at server_url; the request presents the token.
        """
        return web.request(
            method, f"{server_url}{self.path(name)}", token=self.token, timeout=120
        )

    def held(self, cache: common.Cache, name: str) -> bool:
        """Return whether a V2 cache says it holds all of object name.

        A cache says so with Age.
        """
        answer = web.request("HEAD", f"{cache.url}{self.path(name)}", token=self.setup)
        return answer.status == 200 and answer.header("Age").strip().isdigit()

    def fill(self, cache: common.Cache, name: str) -> Optional[str]:
        """Read object name whole through cache, which must serve it.

        For a V2 cache, wait until it says it holds it all. The read is
        setup, with the setup token.

        Returns:
            What went wrong, or None.
        """
        answer = web.request(
            "GET", f"{cache.url}{self.path(name)}", token=self.setup, timeout=120
        )
        if answer.status != 200 or answer.body != self.objects[self.rel(name)]:
            return f"{cache.svc}: a GET of {name} got {answer.describe()}"
        if cache.kind == "v2" and not common.wait_for(
            lambda: self.held(cache, name), HELD_WAIT, interval=1
        ):
            return f"{cache.svc} does not hold {name} {HELD_WAIT}s after reading it whole"
        return None


def crc32c_hex(data: bytes) -> str:
    """Return data's CRC-32C, as `stat --checksums` and the ads print it."""
    return f"{blocks.crc32c(data):08x}"


def stats_of(path: str) -> list[dict[str, Any]]:
    """Return the results in the --transfer-stats file at path.

    Returns:
        Each result, with its attempts; none if the file is unreadable.
    """
    try:
        with open(path, encoding="utf-8") as f:
            stats = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    found: list[dict[str, Any]] = []
    pending: list[Any] = [stats]
    while pending:
        item = pending.pop()
        nested = common.as_array(item)
        if nested is not None:
            pending.extend(nested)
            continue
        result = common.as_object(item)
        if result is not None and "attempts" in result:
            found.append(result)
    return found


def attempts_of(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a result's attempts, in order."""
    found = [common.as_object(a) for a in common.as_array(result.get("attempts")) or []]
    tried = [a for a in found if a is not None]
    return sorted(tried, key=lambda a: int(a.get("attemptNumber") or 0))


def endpoint_host(attempt: Mapping[str, Any]) -> str:
    """Return the host of an attempt's endpoint, as the stats name it."""
    return str(attempt.get("endpoint") or "").partition(":")[0]


# --------------------------------------------------------------------------
# plugin-ads


def ad_problems(ad: Ad, url: str, data: bytes) -> list[str]:
    """Return what is wrong with the plugin's ad of a download of url.

    The download must have succeeded, and url holds data.
    """
    problems: list[str] = []
    name = transfers.local_name(url)
    if ad.get_ci("TransferSuccess") is not True:
        return [
            f"{name}: TransferSuccess {ad.get_ci('TransferSuccess')!r}: {ad.get_ci('TransferError')}"
        ]
    for attr, want in (
        ("TransferFileBytes", len(data)),
        ("TransferTotalBytes", len(data)),
        ("TransferUrl", url),
        ("TransferType", "download"),
        ("TransferFileName", name),
    ):
        if ad.get_ci(attr) != want:
            problems.append(f"{name}: {attr} {ad.get_ci(attr)!r}, not {want!r}")
    started, ended = ad.get_ci("TransferStartTime"), ad.get_ci("TransferEndTime")
    if not isinstance(started, int) or not isinstance(ended, int) or started > ended:
        problems.append(f"{name}: TransferStartTime {started!r}, TransferEndTime {ended!r}")
    data_ad = developer_data(ad)
    attempts = data_ad.get_ci("Attempts")
    if not isinstance(attempts, int) or attempts < 1:
        problems.append(f"{name}: DeveloperData.Attempts {attempts!r}")
    if not data_ad.get_ci("Endpoint0"):
        problems.append(f"{name}: DeveloperData has no Endpoint0")
    return problems


def last_attempt(ad: Ad) -> int:
    """Return the number of the last attempt in an ad's DeveloperData."""
    numbers = [
        int(m.group(1))
        for key in developer_data(ad)
        for m in [re.fullmatch(r"Endpoint(\d+)", key)]
        if m
    ]
    return max(numbers, default=0)


def checksum_problems(
    fed: common.Federation, ad: Ad, data: bytes, cache: Optional[str]
) -> list[str]:
    """Return what is wrong with an ad's ServerChecksums.

    Whichever digests it reports must be the object's, and after a whole
    read through a V2 cache there must be a crc32c, which the cache relays
    from the origin or computes itself (local_cache/persistent_cache.go).

    Args:
        fed: The federation.
        ad: The plugin's ad of a download.
        data: The object's content.
        cache: The host that served the download (served_by()), if known.
    """
    name = ad.get_ci("TransferFileName")
    sums = developer_data(ad).get_ci("ServerChecksums")
    reported = {k.lower(): str(v) for k, v in sums.items()} if isinstance(sums, Ad) else {}
    problems: list[str] = []
    want = {"crc32c": crc32c_hex(data)}
    for kind, value in reported.items():
        if kind in want and value.lower() != want[kind]:
            problems.append(f"{name}: ServerChecksums.{kind} {value}, not {want[kind]}")
    v2 = any(c.kind == "v2" and c.svc == cache for c in fed.caches)
    if v2 and "crc32c" not in reported:
        problems.append(f"{name}: no crc32c in ServerChecksums {sorted(reported)}")
    return problems


def served_by(ad: Ad) -> str:
    """Return the host of the last attempt in an ad's DeveloperData."""
    found = developer_data(ad).get_ci(f"Endpoint{last_attempt(ad)}")
    return str(found or "").partition(":")[0]


def cache_hit_problems(fed: common.Federation, ad: Ad, cold_host: Optional[str]) -> list[str]:
    """Return what is wrong with an ad's DataAge and CacheHit.

    Those of its last attempt, which a V2 cache's Age gives, must say: on
    a cold read (cold_host is None), no hit; on a warm one at the cache
    that the cold read filled (cold_host), a hit, with an age. A warm read
    sent to another cache is cold there, and says nothing.
    """
    name = ad.get_ci("TransferFileName")
    data_ad = developer_data(ad)
    n = last_attempt(ad)
    host = served_by(ad)
    if not any(c.kind == "v2" and c.svc == host for c in fed.caches):
        return []
    hit, age = data_ad.get_ci(f"CacheHit{n}"), data_ad.get_ci(f"DataAge{n}")
    if cold_host is None and hit is True:
        return [f"{name}: cold, but CacheHit{n} is true (DataAge{n} {age!r})"]
    if cold_host == host and (hit is not True or not isinstance(age, (int, float)) or age <= 0):
        return [f"{name}: warm at {host}, but CacheHit{n} {hit!r} and DataAge{n} {age!r}"]
    return []


def exit_problem(fetch: Fetched, ads: Sequence[Ad]) -> Optional[str]:
    """Return why the plugin's exit status disagrees with its ads, or None.

    The status must be 0 if every transfer succeeded, else RETRYABLE_EXIT
    if any failure may be retried, else 1 (writeOutfile in cmd/plugin.go).
    """
    if all(ad.get_ci("TransferSuccess") is True for ad in ads):
        want = 0
    elif any(ad.get_ci("TransferRetryable") is True for ad in ads):
        want = RETRYABLE_EXIT
    else:
        want = 1
    if fetch.status != want:
        return f"exit {fetch.status}, not {want}, for ads that say what they say"
    return None


def plugin_ads(test: Test) -> list[Row]:
    """Check that stash_plugin's result ads agree with its transfers.

    The ads (cmd/plugin.go), which HTCondor reads, are those of a batch of
    two objects through a cache, cold and then warm; of an object that
    does not exist; of a refused download; and of a batch with one of
    each.
    """
    if not test.fed.federated_caches:
        return [("", SKIP, "this shape has no cache for a download to go through")]
    names = ["ads-a", "ads-b"]
    for name in names:
        test.put(name, os.urandom(OBJECT_SIZE))
    urls = [test.url(name) for name in names]
    env = test.creds_env(test.server_creds("plugin-ads"))
    rows: list[Row] = []
    cold_hosts: dict[str, str] = {}
    for case in ("cold", "warm"):
        fetch = test.plugin(f"ads-{case}", urls, env)
        try:
            ads = parse_ads(fetch.said)
        except ValueError as e:
            rows.append((case, FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}"))
            continue
        problems: list[str] = []
        if len(ads) != len(urls):
            problems.append(f"{len(ads)} ads for {len(urls)} transfers")
        for ad, url, name in zip(ads, urls, names):
            data = test.objects[test.rel(name)]
            problems += ad_problems(ad, url, data)
            if ad.get_ci("TransferSuccess") is True:
                problems += checksum_problems(test.fed, ad, data, served_by(ad))
                problems += cache_hit_problems(
                    test.fed, ad, cold_hosts.get(name) if case == "warm" else None
                )
                if case == "cold":
                    cold_hosts[name] = served_by(ad)
                if common.sha256(os.path.join(fetch.directory, name)) != common.digest(data):
                    problems.append(f"{name}: the file is not the object")
        why = exit_problem(fetch, ads)
        if why:
            problems.append(why)
        served = sorted({served_by(ad) for ad in ads if ad.get_ci("TransferSuccess") is True})
        rows.append(
            (
                case,
                *outcome(problems, f"{len(ads)} ads agree with the transfers ({served})")[1:],
            )
        )

    def failed_row(case: str, fetch: Fetched, want_type: str, want_code: Optional[int]) -> Row:
        try:
            ads = parse_ads(fetch.said)
        except ValueError as e:
            return (case, FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}")
        if len(ads) != 1:
            return (case, FAIL, f"{len(ads)} ads for one transfer")
        ad = ads[0]
        problems: list[str] = []
        if ad.get_ci("TransferSuccess") is not False:
            problems.append(f"TransferSuccess {ad.get_ci('TransferSuccess')!r}")
        if ad.get_ci("TransferRetryable") is not False:
            problems.append(f"TransferRetryable {ad.get_ci('TransferRetryable')!r}, not false")
        errors = error_types(ad)
        if not any(kind == want_type for kind, _ in errors):
            problems.append(f"ErrorType {[k for k, _ in errors]}, not {want_type}")
        if want_code is not None and not any(code == want_code for _, code in errors):
            problems.append(f"PelicanErrorCode {[c for _, c in errors]}, not {want_code}")
        why = exit_problem(fetch, ads)
        if why:
            problems.append(why)
        if fetch.files():
            problems.append(f"left {fetch.files()}")
        return (case, *outcome(problems, f"{want_type}, exit {fetch.status}")[1:])

    rows.append(
        failed_row("dne", test.plugin("ads-dne", [test.url("dne")], env), "Specification", 5011)
    )
    unknown_env = test.creds_env(
        test.creds("plugin-ads-refused", {"scitokens.use": unknown_token(test)})
    )
    rows.append(
        failed_row(
            "refused", test.plugin("ads-refused", [urls[0]], unknown_env), "Authorization", None
        )
    )
    fetch = test.plugin("ads-mixed", [urls[0], test.url("dne")], env)
    try:
        ads = parse_ads(fetch.said)
        problems = []
        if len(ads) != 2:
            problems.append(f"{len(ads)} ads for two transfers")
        else:
            problems += ad_problems(ads[0], urls[0], test.objects[test.rel(names[0])])
            if ads[1].get_ci("TransferSuccess") is not False:
                problems.append(
                    f"the missing object's TransferSuccess {ads[1].get_ci('TransferSuccess')!r}"
                )
        why = exit_problem(fetch, ads)
        if why:
            problems.append(why)
        rows.append(("mixed", *outcome(problems, f"two ads, exit {fetch.status}")[1:]))
    except ValueError as e:
        rows.append(("mixed", FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}"))
    return rows


def unknown_token(test: Test) -> str:
    """Return a token like the server's, signed by a key no server knows.

    See credentials.CREDENTIALS, `unknown`.
    """
    path = test.session.path("tokens/client.unknown")
    credentials.mint(
        test.fed,
        path,
        f"/{NAMESPACE}/",
        credentials.keys(test.fed)["unknown"],
        test.fed.issuer_of(NAMESPACE),
        "/",
        credentials.LONG,
        credentials.RWM,
        profile=test.session.profile,
    )
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


# --------------------------------------------------------------------------
# failover


def spoil(test: Test, cache: common.Cache, name: str, block: int) -> Optional[str]:
    """Have a V2 cache corrupt block of its copy of object name.

    Ask through its chaos API, as its admin.

    Returns:
        Why that failed, or None.
    """
    token = cacheapi.admin_token(test.session, cache)
    answer = cacheapi.introspect(
        cache,
        token,
        "POST",
        "chaos",
        op="corrupt",
        url=cacheapi.object_url(test.fed, test.path(name)),
        block=str(block),
        bytes=str(blocks.TAG),
    )
    if answer.status == 404:
        return f"{cache.svc} has no chaos API (Cache.EnableChaosAPI; Pelican v7.27.0 on)"
    if answer.status != 200:
        return f"{cache.svc}'s chaos API got {answer.describe()}"
    return None


def failover(test: Test) -> list[Row]:
    """Check that a download failing partway at a cache resumes at the next.

    A V2 cache and another cache (of either kind, as under
    `-p cache-mixed`) hold an object whole; the first's copy has a block
    corrupted, which it cannot repair once the object is gone from every
    origin. `pelican object get --cache <first>,<second>` must then
    succeed, with the object's bytes, the first attempt failing after
    some bytes, and the second, at the second cache, bringing the rest:
    the client resumes with a Range from where it stopped, and refuses
    to if the ETag changed (downloadObject in client/handle_http.go).
    stash_plugin, with the same preferred caches, must record both
    attempts.
    """
    feds = test.fed.federated_caches
    v2 = [c for c in feds if c.kind == "v2"]
    if not v2 or len(feds) < 2:
        return [
            ("", SKIP, "no federated V2 cache beside another cache ('-p topo-multi-cache')")
        ]
    if test.fed.sent_cache_control:
        return [
            (
                "",
                SKIP,
                f"the origins send Cache-Control {test.fed.sent_cache_control!r}, so a cache"
                + " would revalidate against origins that no longer hold the object",
            )
        ]
    first_cache = v2[0]
    second = next(c for c in feds if c.svc != first_cache.svc)
    rows: list[Row] = []
    for case in ("get", "plugin"):
        name = f"failover-{case}"
        data = os.urandom(FAILOVER_SIZE)
        test.put(name, data)
        for cache in (first_cache, second):
            why = test.fill(cache, name)
            if why:
                return rows + [(case, FAIL, why)]
        why = spoil(test, first_cache, name, FAILOVER_BLOCK)
        if why:
            return rows + [(case, SKIP if "no chaos" in why else FAIL, why)]
        for origin in test.fed.origins:
            error = stores.delete(origin, NAMESPACE, test.rel(name), test.setup)
            if error:
                return rows + [(case, FAIL, error)]
        preferred = f"{first_cache.url},{second.url}"
        if case == "get":
            fetch = test.pelican(
                name,
                [
                    "object",
                    "get",
                    "--cache",
                    preferred,
                    "--token",
                    test.token_file,
                    "--transfer-stats",
                    "stats.json",
                    test.url(name),
                    "object",
                ],
            )
            problems = get_failover_problems(fetch, data, first_cache, second)
        else:
            env = test.creds_env(
                test.server_creds(name), PELICAN_CLIENT_PREFERREDCACHES=preferred
            )
            fetch = test.plugin(name, [test.url(name)], env)
            problems = plugin_failover_problems(fetch, data, first_cache, second)
        rows.append(
            (
                case,
                *outcome(problems, f"failed at {first_cache.svc}, resumed at {second.svc}")[1:],
            )
        )
    return rows


def get_failover_problems(
    fetch: Fetched, data: bytes, first_cache: common.Cache, second: common.Cache
) -> list[str]:
    """Return what is wrong with how `pelican object get` failed over."""
    if fetch.status != 0:
        return [f"exit {fetch.status}: {last_line(fetch.said)}"]
    problems: list[str] = []
    if common.sha256(os.path.join(fetch.directory, "object")) != common.digest(data):
        problems.append("the file is not the object")
    results = stats_of(os.path.join(fetch.directory, "stats.json"))
    attempts = attempts_of(results[0]) if results else []
    if len(attempts) < 2:
        return problems + [
            f"{len(attempts)} attempt(s) in the stats, not a failure and a resume"
        ]
    hosts = [endpoint_host(a) for a in attempts]
    failed = [a for a in attempts if a.get("error")]
    if not failed or endpoint_host(failed[0]) != first_cache.svc:
        problems.append(f"no failed attempt at {first_cache.svc}: tried {hosts}")
    elif int(failed[0].get("transferFileBytes") or 0) <= 0:
        problems.append(f"the failed attempt at {first_cache.svc} brought no bytes")
    last = attempts[-1]
    if last.get("error") or endpoint_host(last) != second.svc:
        problems.append(f"the last attempt is not a success at {second.svc}: {hosts}")
    moved = sum(int(a.get("transferFileBytes") or 0) for a in attempts)
    if not problems and moved != len(data):
        problems.append(
            f"the attempts moved {moved} bytes in all, not {len(data)}: the resume did not"
            + " carry on from where the first attempt stopped"
        )
    return problems


def plugin_failover_problems(
    fetch: Fetched, data: bytes, first_cache: common.Cache, second: common.Cache
) -> list[str]:
    """Return what is wrong with how stash_plugin failed over."""
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        return [f"unreadable ads ({e}): {last_line(fetch.said)}"]
    if len(ads) != 1 or ads[0].get_ci("TransferSuccess") is not True:
        return [f"no ad of a success: {last_line(fetch.said)}"]
    ad = ads[0]
    problems: list[str] = []
    if common.sha256(os.path.join(fetch.directory, "object")) != common.digest(data):
        problems.append("the file is not the object")
    found = developer_data(ad)
    endpoints = {
        n: str(found.get_ci(f"Endpoint{n}") or "").partition(":")[0]
        for n in range(last_attempt(ad) + 1)
    }
    if endpoints.get(0) != first_cache.svc or not found.get_ci("TransferError0"):
        problems.append(f"no failed first attempt at {first_cache.svc}: {endpoints}")
    if endpoints.get(last_attempt(ad)) != second.svc:
        problems.append(f"the last attempt is not at {second.svc}: {endpoints}")
    return problems


# --------------------------------------------------------------------------
# etag-agreement


def etag_agreement(test: Test) -> tuple[str, str]:
    """Check that each server gives an object one ETag, by HEAD and GET.

    An object put at every origin: each origin that takes direct
    clients, and each federated cache, must report one ETag for it by
    HEAD and by GET, and a cache's must be one of the origins', since it
    relays the origin's (local_cache/persistent_cache_api.go). Origins
    that share a store must agree; those with stores of their own hold
    copies of their own, and may not, which the note says.
    """
    name = "etag"
    test.put(name, os.urandom(OBJECT_SIZE))
    problems: list[str] = []
    origin_etags: dict[str, str] = {}
    for origin in test.fed.origins if test.direct else []:
        etags: dict[str, str] = {}
        for method in ("HEAD", "GET"):
            answer = test.ask(method, origin.url, name)
            if answer.status != 200:
                problems.append(f"{origin.svc}: {method} got {answer.describe()}")
            elif not answer.header("ETag"):
                problems.append(f"{origin.svc}: {method} sent no ETag")
            else:
                etags[method] = answer.header("ETag")
        if len(etags) == 2 and etags["HEAD"] != etags["GET"]:
            problems.append(
                f"{origin.svc}: HEAD's ETag {etags['HEAD']} is not GET's {etags['GET']}"
            )
        if "GET" in etags:
            origin_etags[origin.svc] = etags["GET"]
    for cache in test.fed.federated_caches:
        why = test.fill(cache, name)
        if why:
            problems.append(why)
            continue
        etags = {}
        for method in ("HEAD", "GET"):
            answer = test.ask(method, cache.url, name)
            if answer.status != 200:
                problems.append(f"{cache.svc}: {method} got {answer.describe()}")
            elif not answer.header("ETag"):
                problems.append(f"{cache.svc}: {method} sent no ETag")
            else:
                etags[method] = answer.header("ETag")
        if len(etags) == 2 and etags["HEAD"] != etags["GET"]:
            problems.append(
                f"{cache.svc}: HEAD's ETag {etags['HEAD']} is not GET's {etags['GET']}"
            )
        if "GET" in etags and origin_etags and etags["GET"] not in origin_etags.values():
            problems.append(
                f"{cache.svc}'s ETag {etags['GET']} is none of the origins' {origin_etags}"
            )
    shared: dict[str, set[str]] = {}
    for origin in test.fed.origins:
        if origin.svc in origin_etags:
            shared.setdefault(origin.store, set()).add(origin_etags[origin.svc])
    for store, found in shared.items():
        if len(found) > 1:
            problems.append(f"the origins sharing framework/var/{store} report {sorted(found)}")
    if problems:
        return FAIL, first(problems)
    distinct = len(set(origin_etags.values()))
    note = f"{len(origin_etags)} origin(s), {len(test.fed.federated_caches)} cache(s)"
    if distinct > 1:
        note += f"; the origins' copies have {distinct} ETags, so a cache filled from one cannot resume at a cache filled from another"
    return PASS, note


# --------------------------------------------------------------------------
# token-discovery


def discovery_get(test: Test, name: str, url: str, env: Mapping[str, str]) -> Fetched:
    """Run `pelican object get` of url, with env, into a new directory.

    Into a directory, since a refusal of a get into a file path shows
    only as a probe no server answered (see testlib/transfers.py).
    """
    return test.pelican(name, ["object", "get", url, "."], env)


def got(fetch: Fetched, data: bytes, local: str = "object") -> Optional[str]:
    """Return why the download did not bring data, or None if it did.

    It must bring it to file local in its directory.
    """
    if fetch.status != 0:
        return f"exit {fetch.status}: {last_line(fetch.said)}"
    if common.sha256(os.path.join(fetch.directory, local)) != common.digest(data):
        return "the file is not the object"
    return None


def got_named(fetch: Fetched, data: bytes) -> Optional[str]:
    """Return got() of the discovery object, under its own name."""
    return got(fetch, data, "discovery")


def refused(fetch: Fetched) -> Optional[str]:
    """Return why the download was not refused, or None."""
    if fetch.status == 0:
        return "succeeded, but should have been refused"
    if transfers.refused(fetch.said) is None:
        return f"failed, but not by refusal: {last_line(fetch.said)}"
    return None


def token_discovery(test: Test) -> list[Row]:
    """Check which token the client presents, of those in its environment.

    The cases (tokenContentIterator in client/acquire_token.go):
    BEARER_TOKEN, BEARER_TOKEN_FILE, and _CONDOR_CREDS/scitokens.use, each
    alone, with the server's token (`bearer-token`, `bearer-token-file`,
    `condor-creds`); a token scoped elsewhere in BEARER_TOKEN beside the
    server's in _CONDOR_CREDS, which the client must pass over, since the
    director's hints say it would not do (`skips-unacceptable`); a token
    signed by a key no server knows, beside the server's, which looks
    acceptable and so is presented, and refused (`acceptable-wins`); and a
    URL whose scheme names a token file (`<name>+pelican://`, `prefix`), or
    with `_` for `.` in the name (`<na_me>+pelican://`,
    `prefix-underscore`), which selects `<name>.use` in _CONDOR_CREDS before
    scitokens.use (GetTokenName in pelican_url/parse.go).
    """
    name = "discovery"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    url = test.url(name)
    base = dict(test.session.env)
    wrong_path = test.session.credential_token(
        credentials.BY_NAME["wrong-path"], NAMESPACE, "get"
    )
    unknown = unknown_token(test)
    good_creds = test.server_creds("discovery-good")
    rows: list[Row] = []
    env = {**base, "BEARER_TOKEN": test.token}
    why = got_named(discovery_get(test, "bearer-token", url, env), data)
    rows.append(("bearer-token", FAIL, why) if why else ("bearer-token", PASS, ""))
    env = {**base, "BEARER_TOKEN_FILE": test.token_file}
    why = got_named(discovery_get(test, "bearer-token-file", url, env), data)
    rows.append(("bearer-token-file", FAIL, why) if why else ("bearer-token-file", PASS, ""))
    env = {**base, "_CONDOR_CREDS": good_creds}
    why = got_named(discovery_get(test, "condor-creds", url, env), data)
    rows.append(("condor-creds", FAIL, why) if why else ("condor-creds", PASS, ""))
    env = {**base, "BEARER_TOKEN": wrong_path or "", "_CONDOR_CREDS": good_creds}
    why = got_named(discovery_get(test, "skips-unacceptable", url, env), data)
    rows.append(
        ("skips-unacceptable", FAIL, why)
        if why
        else ("skips-unacceptable", PASS, "the wrong-path token was passed over")
    )
    env = {**base, "BEARER_TOKEN": unknown, "_CONDOR_CREDS": good_creds}
    why = refused(discovery_get(test, "acceptable-wins", url, env))
    rows.append(
        ("acceptable-wins", FAIL, why)
        if why
        else ("acceptable-wins", PASS, "the unknown key's token was presented, and refused")
    )
    # The scheme prefix: the good token under the name the prefix selects,
    # and the unknown key's as scitokens.use, which would otherwise win.
    for case, prefix, file_name in (
        ("prefix", NAMESPACE, f"{NAMESPACE}.use"),
        (
            "prefix-underscore",
            NAMESPACE.replace("-", "_"),
            f"{NAMESPACE.replace('-', '.')}.use",
        ),
    ):
        creds = test.creds(
            f"discovery-{case}", {"scitokens.use": unknown, file_name: test.token}
        )
        prefixed = f"{prefix}+{url}"
        env = {**base, "_CONDOR_CREDS": creds}
        why = got_named(discovery_get(test, case, prefixed, env), data)
        rows.append((case, FAIL, why) if why else (case, PASS, f"{file_name} was selected"))
    return rows


# --------------------------------------------------------------------------
# token-create-discovery


def create_token(
    test: Test, name: str, url: str, key: str, *flags: str
) -> tuple[Optional[dict[str, Any]], str, Fetched]:
    """Run `pelican token create url --read --private-key key`.

    It runs with flags, the session's --profile, and with no --issuer or
    --scope-path.

    Returns:
        The token's claims and the token, if it printed one (else None and
        ""), and the run.
    """
    args = ["token", "create", url, "--read", "--lifetime", "300"]
    args += ["--profile", credentials.PELICAN_PROFILES[test.session.profile]]
    args += ["--private-key", os.path.abspath(key), *flags]
    fetch = test.pelican(name, args)
    if fetch.status != 0:
        return None, "", fetch
    for line in reversed(fetch.said.splitlines()):
        try:
            return jwt.decode(line.strip())[1], line.strip(), fetch
        except ValueError:
            continue
    return None, "", fetch


def token_create_discovery(test: Test) -> list[Row]:
    """Check how `pelican token create` finds an issuer and a scope.

    Without --issuer or --scope-path (cmd/token.go): with the origins' key,
    for a path in /protected-a, the issuer is the namespace's, found as the
    first of the director's whose JWKS holds the signing key's public key,
    and the scope the path within the namespace (`origin-key`); the token
    reads the object (`reads`); with the namespace's own test key, listed
    only in its export's IssuerJwks, the issuer is found through that JWKS
    (`namespace-key`); with a key no server knows, it fails, saying that no
    issuer matches the key (`unknown-key`); for /public, which has no
    issuer, it fails, asking whether a token is needed at all, but where
    /public takes writes (`-p origin-public-writes`), and so has an issuer,
    the token's issuer is /public's (`public`); and under
    `-p topo-multi-owner`, with origin-2's key for /other, the issuer is
    origin-2's (`other-owner`).
    """
    rows: list[Row] = []
    name = "token-create/obj"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    collection = test.url("token-create") + "/"
    read = credentials.SCOPES[test.session.profile]["read"]
    want_scope = f"{read}:/{test.rel('token-create')}"
    claims, token, fetch = create_token(
        test, "create-origin-key", collection, test.fed.origin_key
    )
    if claims is None:
        rows.append(("origin-key", FAIL, f"exit {fetch.status}: {last_line(fetch.said)}"))
    else:
        problems: list[str] = []
        if claims.get("iss") != test.fed.issuer_of(NAMESPACE):
            problems.append(f"iss {claims.get('iss')!r}, not /{NAMESPACE}'s issuer")
        if claims.get("scope") != want_scope:
            problems.append(f"scope {claims.get('scope')!r}, not {want_scope!r}")
        rows.append(("origin-key", *outcome(problems, f"iss and scope {want_scope}")[1:]))
        if not problems:
            problems = []
            servers = [(o.svc, o.url) for o in test.fed.origins if test.direct]
            servers += [(c.svc, c.url) for c in test.fed.federated_caches]
            for svc, server_url in servers:
                answer = web.request("GET", f"{server_url}{test.path(name)}", token=token)
                if answer.status != 200 or answer.body != data:
                    problems.append(f"{svc}: GET with the token got {answer.describe()}")
            rows.append(("reads", *outcome(problems, f"at {len(servers)} server(s)")[1:]))
    if credentials.namespace_issuers(test.fed):
        claims, token, fetch = create_token(
            test,
            "create-namespace-key",
            collection,
            credentials.keys(test.fed)[f"ns-{NAMESPACE}"],
        )
        if claims is None:
            rows.append(
                ("namespace-key", FAIL, f"exit {fetch.status}: {last_line(fetch.said)}")
            )
        elif claims.get("iss") != test.fed.issuer_of(NAMESPACE):
            rows.append(
                ("namespace-key", FAIL, f"iss {claims.get('iss')!r}, not /{NAMESPACE}'s issuer")
            )
        else:
            rows.append(("namespace-key", PASS, "found through the export's own JWKS"))
    else:
        rows.append(("namespace-key", SKIP, "no namespace has keys of its own"))
    claims, token, fetch = create_token(
        test, "create-unknown-key", collection, credentials.keys(test.fed)["unknown"]
    )
    if claims is not None:
        rows.append(("unknown-key", FAIL, f"minted a token of issuer {claims.get('iss')!r}"))
    elif "matches your signing key" not in fetch.said:
        rows.append(
            ("unknown-key", FAIL, f"failed, but not for the key: {last_line(fetch.said)}")
        )
    else:
        rows.append(("unknown-key", PASS, "no issuer matches the key"))
    if "public" not in test.fed.exports:
        rows.append(("public", SKIP, "this shape does not export /public"))
    elif credentials.has_issuer(test.fed.exports["public"]):
        claims, token, fetch = create_token(
            test, "create-public", test.url("token-create", "public") + "/", test.fed.origin_key
        )
        want_issuer = test.fed.issuer_of("public")
        if claims is None:
            rows.append(("public", FAIL, f"exit {fetch.status}: {last_line(fetch.said)}"))
        elif claims.get("iss") != want_issuer:
            rows.append(
                ("public", FAIL, f"issuer {claims.get('iss')!r}, not /public's {want_issuer!r}")
            )
        else:
            rows.append(("public", PASS, "/public takes writes, so its issuer is found"))
    else:
        claims, token, fetch = create_token(
            test, "create-public", test.url("token-create", "public") + "/", test.fed.origin_key
        )
        if claims is not None:
            rows.append(("public", FAIL, f"minted a token of issuer {claims.get('iss')!r}"))
        elif "Are you sure you need a token" not in fetch.said:
            rows.append(
                ("public", FAIL, f"failed, but not with the hint: {last_line(fetch.said)}")
            )
        else:
            rows.append(("public", PASS, "asks whether a token is needed"))
    owners = [o for o in test.fed.owners if any(e.prefix == "/other" for e in o.exports)]
    if not owners:
        rows.append(("other-owner", SKIP, "no other owner ('-p topo-multi-owner')"))
        return rows
    owner = owners[0]
    export = next(e for e in owner.exports if e.prefix == "/other")
    claims, token, fetch = create_token(
        test,
        "create-other-owner",
        f"{test.fed.url}/other/{test.rel('token-create')}/",
        owner.key,
    )
    if claims is None:
        rows.append(("other-owner", FAIL, f"exit {fetch.status}: {last_line(fetch.said)}"))
    elif claims.get("iss") != export.issuer:
        rows.append(("other-owner", FAIL, f"iss {claims.get('iss')!r}, not {export.issuer}"))
    else:
        rows.append(("other-owner", PASS, f"iss {export.issuer}"))
    return rows


# --------------------------------------------------------------------------
# share-url


def share_url(test: Test) -> list[Row]:
    """Check the URL that `pelican object share` hands out, and its token.

    `pelican object share [--write]` of a collection in /protected-a,
    its device flow approved as the admin, whom the issuer grants the
    namespace (client/sharing_url.go, cmd/object_share.go): the URL must
    be the collection's with `?authz=<token>`, whose scope is the
    collection's read alone, and with --write its create (`url`,
    `url-write`); a GET of the object in it at a director's object
    route, with the token in the query as curl sends it, must redirect
    with `authz` in Location and no token in Link, and the redirect
    followed must serve the object (`curl`); and `pelican object get` of
    the URL, with no other token, must serve it too (`get`).
    """
    if not test.fed.embedded_issuer or test.fed.external_issuer:
        return [("", SKIP, "the share's token comes from the origins' embedded issuer")]
    if test.fed.standalone:
        return [("", SKIP, "a standalone origin has no director to find the issuer through")]
    name = "share/obj"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    collection = test.url("share")
    web_url = test.fed.origin_web_url
    admin = credentials.mint_admin(
        test.fed,
        test.session.path("tokens/client.admin"),
        credentials.local_issuer(test.fed, web_url),
        test.fed.origin_key,
    )

    def approver(issuer_url: str, user_code: str) -> None:
        issuer.approve(issuer_url, user_code, User(token=admin))

    rows: list[Row] = []
    tokens: dict[str, str] = {}
    for case, flags, want in (
        ("url", [], {f"storage.read:/{test.rel('share')}"}),
        ("url-write", ["--write"], {f"storage.create:/{test.rel('share')}"}),
    ):
        store = os.path.join(test.tmp, f"share-{case}.store")
        done = issuer.run_approving(
            test.session.pelican,
            ["object", "share", *flags, collection],
            issuer.client_env(test.session.env, store),
            test.tmp,
            approver,
        )
        if done.status != 0 or done.problems:
            why = (
                first(done.problems) if done.problems else last_line(done.stderr or done.stdout)
            )
            rows.append((case, FAIL, f"exit {done.status}: {why}"))
            continue
        printed = done.stdout.strip().splitlines()[-1].strip() if done.stdout.strip() else ""
        parts = urlsplit(printed)
        query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
        token = query.get("authz", "")
        problems: list[str] = []
        if f"{parts.scheme}://{parts.netloc}{parts.path}" != collection or not token:
            problems.append(f"printed {printed!r}, not the collection's URL with ?authz=")
        else:
            scopes: set[str] = set()
            try:
                scopes = set(str(jwt.decode(token)[1].get("scope", "")).split())
            except ValueError:
                pass
            if not want <= scopes or not scopes <= want | {
                f"storage.read:/{test.rel('share')}"
            }:
                problems.append(f"the token's scopes are {sorted(scopes)}, not {sorted(want)}")
            tokens[case] = token
        rows.append(
            (case, *outcome(problems, f"{collection}?authz=<token of {sorted(want)}>")[1:])
        )
    if "url" not in tokens:
        return rows
    token = tokens["url"]
    problems = []
    for director_url in test.fed.directors:
        target = f"{director_url}/api/v1.0/director/object{test.path(name)}?authz={quote(token, safe='')}"
        answer = web.request("GET", target)
        host = director.host(director_url)
        if answer.status != 307:
            problems.append(f"{host}: {answer.describe()}, not 307")
            continue
        location = answer.header("Location")
        if (
            f"authz={token}" not in location
            and f"authz={quote(token, safe='')}" not in location
        ):
            problems.append(f"{host}: Location carries no authz: {location[:80]}")
        if token in answer.header("Link"):
            problems.append(f"{host}: Link carries the token")
        served = web.request("GET", location)
        if served.status != 200 or served.body != data:
            problems.append(f"{host}: following Location got {served.describe()}")
    rows.append(("curl", *outcome(problems, f"{len(test.fed.directors)} director(s)")[1:]))
    fetch = test.pelican(
        "share-get", ["object", "get", f"{test.url(name)}?authz={token}", "object"]
    )
    wrong = got(fetch, data)
    if wrong and f"Error code {NO_TOKEN}" in fetch.said:
        rows.append(("get", FAIL, f"the client found no token in the URL: {wrong}"))
    elif wrong:
        rows.append(("get", FAIL, wrong))
    else:
        rows.append(("get", PASS, "the client read the token from the URL"))
    return rows


# --------------------------------------------------------------------------
# director-decision


def director_decision(test: Test) -> tuple[str, str]:
    """Check that the plugin records the director's decision when asked.

    With Plugin.DirectorDecisionPercentage at 100, the plugin asks the
    director to explain its decision (X-Pelican-Debug), and records it
    in the ad's DeveloperData.DirectorDecision: the sort method, the
    client's coordinate, and each server's weights
    (redirectInfoToClassAd in cmd/plugin.go).
    """
    if test.fed.standalone:
        return SKIP, "a standalone origin has no director"
    if not test.fed.federated_caches:
        return SKIP, "this shape has no cache for a download to go through"
    name = "decision"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    env = test.creds_env(
        test.server_creds("decision"), PELICAN_PLUGIN_DIRECTORDECISIONPERCENTAGE="100"
    )
    fetch = test.plugin("decision", [test.url(name)], env)
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        return FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}"
    if len(ads) != 1 or ads[0].get_ci("TransferSuccess") is not True:
        return FAIL, f"no ad of a success: {last_line(fetch.said)}"
    decision = developer_data(ads[0]).get_ci("DirectorDecision")
    if not isinstance(decision, Ad):
        return FAIL, f"DeveloperData.DirectorDecision is {decision!r}"
    method = decision.get_ci("DirectorSortMethod")
    client_info = decision.get_ci("ClientInfo")
    servers = common.as_array(decision.get_ci("ServersInfo")) or []
    if not method or not isinstance(client_info, Ad):
        return FAIL, f"DirectorSortMethod {method!r}, ClientInfo {client_info!r}"
    if not servers:
        return FAIL, f"ServersInfo {decision.get_ci('ServersInfo')!r}"
    return PASS, f"sorted by {method}, {len(servers)} server(s) weighed"


# --------------------------------------------------------------------------
# skipstat-failover


def skipstat_failover(test: Test) -> tuple[str, str]:
    """Check that a direct ?skipstat read finds the one origin holding it.

    With several origins, an object put in the last origin's store
    alone: `pelican object get --direct` of it with ?skipstat, which
    keeps the director from asking the origins which holds it, must
    still bring it, since the client tries the origins the director
    lists until one serves (getObjectServersToTry in
    client/handle_http.go). The note says whether the client's stats
    show a failed try at an origin that lacks it, or the client's probes
    put the holder first.
    """
    origins = stores.unique(test.fed.origins)
    if len(origins) < 2:
        return SKIP, "one origin, or origins sharing a store ('-p topo-multi-origin')"
    if not test.direct:
        return SKIP, "/protected-a takes no direct clients"
    holder = origins[-1]
    name = "skipstat"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data, origins=[holder])
    tried_first = False
    for n in range(SKIPSTAT_TRIES):
        fetch = test.pelican(
            f"skipstat-{n}",
            [
                "object",
                "get",
                "--direct",
                "--token",
                test.token_file,
                "--transfer-stats",
                "stats.json",
                test.url(name, query="skipstat"),
                "object",
            ],
        )
        why = got(fetch, data)
        if why:
            return FAIL, why
        results = stats_of(os.path.join(fetch.directory, "stats.json"))
        attempts = attempts_of(results[0]) if results else []
        if attempts and endpoint_host(attempts[0]) != holder.svc and attempts[0].get("error"):
            tried_first = True
            break
    if tried_first:
        return PASS, f"an origin without it was tried first, then {holder.svc} served"
    return PASS, f"{holder.svc} served at the first try each time ({SKIPSTAT_TRIES} reads)"


# --------------------------------------------------------------------------
# multi-source


def multi_source(test: Test) -> list[Row]:
    """Check `get` and `put` with several sources, and `?recursive`.

    These are rows G8-G10 and P8-P10 of Pelican's
    docs/object-transfer-semantics.md (cmd/object_get.go and
    object_put.go), and `?recursive` in place of -r (P6).
    """
    names = ["multi-a", "multi-b"]
    for name in names:
        test.put(name, os.urandom(OBJECT_SIZE))
    urls = [test.url(name) for name in names]
    rows: list[Row] = []
    directory = test.directory("get-g8")
    fetch = test.pelican(
        "get-g8", ["object", "get", "--token", test.token_file, *urls, directory], cwd=directory
    )
    problems: list[str] = []
    if fetch.status != 0:
        problems.append(f"exit {fetch.status}: {last_line(fetch.said)}")
    for name in names:
        if common.sha256(os.path.join(directory, name)) != common.digest(
            test.objects[test.rel(name)]
        ):
            problems.append(f"{name} is missing or wrong in the directory")
    rows.append(("get-directory", *outcome(problems, "each landed by its name (G8)")[1:]))
    for case, target, message in (
        ("get-missing-dir", os.path.join(test.tmp, "nowhere"), "Destination does not exist"),
        ("get-file", test.local("a-file", b"x"), "Destination is not a directory"),
    ):
        fetch = test.pelican(case, ["object", "get", "--token", test.token_file, *urls, target])
        if fetch.status == 0:
            rows.append((case, FAIL, "succeeded"))
        elif message not in fetch.said:
            rows.append(
                (case, FAIL, f"failed, but not saying {message!r}: {last_line(fetch.said)}")
            )
        else:
            rows.append((case, PASS, message))
    if not test.writes:
        rows.append(("put", SKIP, "/protected-a takes no writes"))
        return rows
    files = {n: os.urandom(1000 + 10 * i) for i, n in enumerate(("up-a", "up-b"))}
    sources = [test.local(f"multi/{n}", d) for n, d in files.items()]
    origins = test.fed.origins

    def landed(rel: str, data: bytes) -> bool:
        return common.digest(data) in stores.held_anywhere(
            origins, NAMESPACE, rel, {NAMESPACE: test.setup}
        )

    for case, collection, make in (
        ("put-collection", "multi-into", True),
        ("put-new", "multi-new", False),
    ):
        rel = test.rel(collection)
        if make:
            errors = stores.make_dirs(origins, NAMESPACE, rel, test.setup)
            if errors:
                rows.append((case, FAIL, first(errors)))
                continue
        fetch = test.pelican(
            case, ["object", "put", "--token", test.token_file, *sources, test.url(collection)]
        )
        problems = []
        if fetch.status != 0:
            problems.append(f"exit {fetch.status}: {last_line(fetch.said)}")
        for name, data in files.items():
            if not landed(f"{rel}/{name}", data):
                problems.append(f"no origin holds {collection}/{name}")
        rows.append(
            (case, *outcome(problems, "each landed under the collection by its name")[1:])
        )
    old = os.urandom(500)
    test.put("multi-object", old)
    fetch = test.pelican(
        "put-object",
        ["object", "put", "--token", test.token_file, *sources, test.url("multi-object")],
        env=test.session.sync_env,
    )
    problems = []
    if fetch.status == 0:
        problems.append("succeeded, though the destination is an object")
    elif "already exists" not in fetch.said:
        problems.append(f"failed, but not because the object exists: {last_line(fetch.said)}")
    if common.digest(old) not in stores.held_anywhere(
        origins, NAMESPACE, test.rel("multi-object"), {NAMESPACE: test.setup}
    ):
        problems.append("the object was changed")
    rows.append(("put-object", *outcome(problems, "refused: it already exists (P10)")[1:]))
    tree = {"t/x": os.urandom(300), "t/y": os.urandom(400)}
    for path, data in tree.items():
        test.local(f"recursive/{path}", data)
    fetch = test.pelican(
        "put-recursive-query",
        [
            "object",
            "put",
            "--token",
            test.token_file,
            os.path.join(test.tmp, "recursive"),
            test.url("multi-recursive", query="recursive"),
        ],
    )
    problems = []
    if fetch.status != 0:
        problems.append(f"exit {fetch.status}: {last_line(fetch.said)}")
    for path, data in tree.items():
        if not landed(f"{test.rel('multi-recursive')}/{path}", data):
            problems.append(f"no origin holds multi-recursive/{path}")
    rows.append(
        ("put-recursive-query", *outcome(problems, "the tree landed flat (P6, ?recursive)")[1:])
    )
    if "Listings" not in test.caps:
        rows.append(("get-recursive-query", SKIP, "/protected-a has no Listings"))
        return rows
    directory = test.directory("get-recursive-query")
    fetch = test.pelican(
        "get-recursive-query",
        [
            "object",
            "get",
            "--token",
            test.token_file,
            test.url("multi-recursive", query="recursive"),
            directory,
        ],
        cwd=directory,
    )
    problems = []
    if fetch.status != 0:
        problems.append(f"exit {fetch.status}: {last_line(fetch.said)}")
    for path, data in tree.items():
        if common.sha256(os.path.join(directory, path)) != common.digest(data):
            problems.append(f"{path} is missing or wrong")
    rows.append(
        ("get-recursive-query", *outcome(problems, "the tree came back (G5, ?recursive)")[1:])
    )
    return rows


# --------------------------------------------------------------------------
# preferred-cache-down


def preferred_cache_down(test: Test) -> list[Row]:
    """Check a preferred cache that is down, with `+` and without.

    `pelican object get --cache https://<cache>:1,+`: a preferred
    cache that answers nothing, and then the director's caches, which
    `+` adds, one of which must serve; without `+`, the client has only
    the dead cache, and must fail to reach it, by something other than a
    refusal (getPreferredCaches in client/).
    """
    caches = test.fed.federated_caches
    if not caches:
        return [("", SKIP, "this shape has no cache")]
    name = "preferred"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    dead = f"https://{director.host(caches[0].url)}:1"
    rows: list[Row] = []
    fetch = test.pelican(
        "preferred-plus",
        [
            "object",
            "get",
            "--cache",
            f"{dead},+",
            "--token",
            test.token_file,
            "--transfer-stats",
            "stats.json",
            test.url(name),
            "object",
        ],
    )
    why = got(fetch, data)
    if why:
        rows.append(("plus", FAIL, why))
    else:
        results = stats_of(os.path.join(fetch.directory, "stats.json"))
        served = [endpoint_host(attempts_of(r)[-1]) for r in results if attempts_of(r)]
        if not served or served[0] not in {c.svc for c in caches}:
            rows.append(
                ("plus", FAIL, f"served by {served or 'no host recorded'}, not a cache")
            )
        else:
            rows.append(("plus", PASS, f"served by {served[0]}"))
    fetch = test.pelican(
        "preferred-alone",
        [
            "object",
            "get",
            "--cache",
            dead,
            "--token",
            test.token_file,
            test.url(name),
            "object",
        ],
    )
    if fetch.status == 0:
        rows.append(("alone", FAIL, "succeeded, though its only cache is down"))
    elif transfers.refused(fetch.said):
        rows.append(
            ("alone", FAIL, f"failed by refusal, not by contact: {last_line(fetch.said)}")
        )
    else:
        rows.append(("alone", PASS, f"failed to reach the cache: {last_line(fetch.said)[:80]}"))
    return rows


# --------------------------------------------------------------------------
# async-job: the client agent.

# The agent's files, by the environment variable that names each
# (ClientAgent.Socket, PidFile, and DbLocation): in a directory of the
# suite's own, so that no agent of an earlier run's, or of the user's, is
# spoken to, and nothing of the run's outlives it.
AGENT_FILES = {
    "PELICAN_CLIENTAGENT_SOCKET": "agent.sock",
    "PELICAN_CLIENTAGENT_PIDFILE": "agent.pid",
    "PELICAN_CLIENTAGENT_DBLOCATION": "agent.db",
}
# How long the agent may take to stop once told to, in seconds.
AGENT_STOP_WAIT = 15
# The status of a job that finished well (client_agent/models.go).
COMPLETED = "completed"


def agent_env(test: Test) -> dict[str, str]:
    """Return the session's environment, naming the agent's files.

    They (AGENT_FILES) are in the suite's directory.
    """
    directory = os.path.join(test.tmp, "agent")
    os.makedirs(directory, exist_ok=True)
    env = dict(test.session.env)
    for name, file_name in AGENT_FILES.items():
        env[name] = os.path.join(directory, file_name)
    return env


def last_json_value(text: str) -> Any:
    """Return the last JSON value in text.

    It may span lines, as the job commands print theirs
    (json.MarshalIndent).

    Raises:
        ValueError: There is no JSON value in text.
    """
    decoder = json.JSONDecoder()
    found: list[Any] = []
    at = 0
    while True:
        m = re.search(r"[{\[]", text[at:])
        if not m:
            break
        start = at + m.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            at = start + 1
            continue
        found.append(value)
        at = end
    if not found:
        raise ValueError("no JSON in the output")
    return found[-1]


def job_id(out: str) -> str:
    """Return the job ID that an --async command printed, or ""."""
    m = re.search(r"Job created: (\S+)", out)
    return m.group(1) if m else ""


def job_status(
    pelican: Callable[..., tuple[int, str, str]], job: str
) -> tuple[Optional[dict[str, Any]], str]:
    """Return what `pelican job status --json` says of job.

    Returns:
        Its JobStatus (client_agent/models.go), and "", or else None and
        why not.
    """
    code, out, err = pelican("job", "status", "--json", job)
    if code != 0:
        return None, f"`job status`: exit {code}: {last_line(err or out)}"
    try:
        found = common.as_object(last_json_value(out))
    except ValueError as e:
        return None, f"`job status`: unreadable output ({e}): {last_line(out)}"
    if found is None:
        return None, "`job status`: the JSON is no object"
    return found, ""


def status_problems(found: Mapping[str, Any], job: str, size: int) -> list[str]:
    """Return what is wrong with a finished job's status.

    It must be completed, name the job, hold one transfer, and count the
    object's bytes.
    """
    problems: list[str] = []
    if found.get("status") != COMPLETED:
        problems.append(f"status {found.get('status')!r}, not {COMPLETED!r}")
    if found.get("job_id") != job:
        problems.append(f"job_id {found.get('job_id')!r}, not {job!r}")
    moved = common.as_array(found.get("transfers")) or []
    if len(moved) != 1:
        problems.append(f"{len(moved)} transfers, not 1")
    progress = common.as_object(found.get("progress")) or {}
    if progress.get("bytes_transferred") != size:
        problems.append(f"bytes_transferred {progress.get('bytes_transferred')!r}, not {size}")
    return problems


def stop_agent(pelican: Callable[..., tuple[int, str, str]]) -> tuple[str, str]:
    """Run `pelican client-agent stop`, and check that the agent stopped.

    `client-agent status` must say, within AGENT_STOP_WAIT seconds, that
    no agent runs.

    Returns:
        A row's status and note.
    """
    code, out, err = pelican("client-agent", "stop")
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    said = last_line(out)

    def stopped() -> bool:
        _, status_out, _ = pelican("client-agent", "status")
        return "not running" in status_out

    if not common.wait_for(stopped, AGENT_STOP_WAIT, interval=1):
        return FAIL, f"`client-agent status` says it runs {AGENT_STOP_WAIT}s after: {said}"
    return PASS, said


def async_job(test: Test) -> list[Row]:
    """Check `get --async` and `copy --async`, which the client agent runs.

    `pelican object get --async --wait` and `object copy --async
    --wait`, which hand a transfer to the client agent (`pelican
    client-agent`, a daemon of the client's own, which the first starts;
    cmd/object_get.go and client_agent/ in Pelican) and wait for it: the
    download must bring the object's bytes (`get`), and the upload land
    at an origin (`copy`, where /protected-a takes writes); `pelican job
    status --json` must report the download's job completed, with one
    transfer and the object's bytes (`status`), and `job list --json`
    list it (`list`); `job cancel` of the finished job must leave it
    completed (`cancel-finished`); and `client-agent stop` must stop the
    agent (`stop`), which runs whatever else happened. The agent's
    socket, PID file, and database are the run's own (agent_env()).
    """
    env = agent_env(test)
    name = "async"
    data = os.urandom(OBJECT_SIZE)
    test.put(name, data)
    rows: list[Row] = []

    def pelican(*args: str, cwd: Optional[str] = None) -> tuple[int, str, str]:
        return test.session.pelican_cmd(*args, env=env, cwd=cwd)

    job = ""
    try:
        directory = test.directory("async-get")
        target = os.path.join(directory, "object")
        code, out, err = pelican(
            "object",
            "get",
            "--async",
            "--wait",
            "--token",
            test.token_file,
            test.url(name),
            target,
            cwd=directory,
        )
        job = job_id(out)
        if code != 0:
            rows.append(("get", FAIL, f"exit {code}: {last_line(err or out)}"))
        elif not job:
            rows.append(("get", FAIL, f"no job ID printed: {last_line(out)}"))
        elif common.sha256(target) != common.digest(data):
            rows.append(("get", FAIL, "the file is not the object"))
        else:
            rows.append(("get", PASS, f"job {job}"))

        if not job:
            rows.append(("status", SKIP, "no job to ask about"))
        else:
            found, why = job_status(pelican, job)
            if found is None:
                rows.append(("status", FAIL, why))
            else:
                problems = status_problems(found, job, len(data))
                if problems:
                    rows.append(("status", FAIL, first(problems)))
                else:
                    rows.append(("status", PASS, f"{COMPLETED}, {len(data)} bytes"))

        code, out, err = pelican("job", "list", "--json")
        if code != 0:
            rows.append(("list", FAIL, f"exit {code}: {last_line(err or out)}"))
        else:
            try:
                listed = common.as_object(last_json_value(out)) or {}
            except ValueError as e:
                listed = {}
                rows.append(("list", FAIL, f"unreadable output ({e}): {last_line(out)}"))
            else:
                ids = [
                    str(j.get("job_id"))
                    for j in (
                        common.as_object(j) or {}
                        for j in common.as_array(listed.get("jobs")) or []
                    )
                ]
                if job and job not in ids:
                    rows.append(("list", FAIL, f"lists {ids}, not {job}"))
                else:
                    rows.append(("list", PASS, f"{len(ids)} job(s)"))

        if not test.writes:
            rows.append(("copy", SKIP, "/protected-a takes no writes"))
        else:
            upload = os.urandom(OBJECT_SIZE)
            local = test.local("async-copy/source", upload)
            rel = test.rel("async-copy")
            code, out, err = pelican(
                "object",
                "copy",
                "--async",
                "--wait",
                "--token",
                test.token_file,
                local,
                test.url("async-copy"),
                cwd=os.path.dirname(local),
            )
            held = stores.held_anywhere(
                test.fed.origins, NAMESPACE, rel, {NAMESPACE: test.setup}
            )
            if code != 0:
                rows.append(("copy", FAIL, f"exit {code}: {last_line(err or out)}"))
            elif common.digest(upload) not in held:
                rows.append(("copy", FAIL, "succeeded, but no origin holds the upload"))
            else:
                rows.append(("copy", PASS, f"job {job_id(out) or '(unnamed)'}"))

        if not job:
            rows.append(("cancel-finished", SKIP, "no job to cancel"))
        else:
            code, out, err = pelican("job", "cancel", job)
            said = f"cancel exit {code}: {last_line(out or err)}"
            found, why = job_status(pelican, job)
            if found is None:
                rows.append(("cancel-finished", FAIL, f"{said}; then {why}"))
            elif found.get("status") != COMPLETED:
                rows.append(
                    (
                        "cancel-finished",
                        FAIL,
                        f"{said}; then status {found.get('status')!r}, not {COMPLETED!r}",
                    )
                )
            else:
                rows.append(("cancel-finished", PASS, f"{said}; still {COMPLETED}"))
    finally:
        rows.append(("stop", *stop_agent(pelican)))
    return rows


# What a scenario of RUN, given the Test, returns: its rows, or for one
# row with no case, its status and note.
Rows = Union[tuple[str, str], list[Row]]

RUN: dict[str, Callable[[Test], Rows]] = {
    "plugin-ads": plugin_ads,
    "failover": failover,
    "etag-agreement": etag_agreement,
    "token-discovery": token_discovery,
    "token-create-discovery": token_create_discovery,
    "share-url": share_url,
    "director-decision": director_decision,
    "skipstat-failover": skipstat_failover,
    "multi-source": multi_source,
    "preferred-cache-down": preferred_cache_down,
    "async-job": async_job,
}


def guarded(name: str, test: Test) -> list[Row]:
    """Return scenario name's rows, or one failing row if it raised."""
    try:
        found = RUN[name](test)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # A bug, or a server answering what the suite can't read: the
        # scenario fails, and the rest still run.
        traceback.print_exc()
        return [("", FAIL, f"{type(e).__name__}: {e}")]
    if isinstance(found, list):
        return found
    return [("", found[0], found[1])]


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    tokens = session.setup_tokens()
    errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/client", tokens)
    if errors:
        die(f"could not remove earlier runs' objects: {first(errors)}")
    test = Test(session)
    print(f"Client contracts against {fed.url}, objects under /<namespace>/{test.base}/\n")
    try:
        for name in selected:
            for case, status, note in guarded(name, test):
                results.add(f"{name} {case}".strip(), status, note)
    finally:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/client", tokens)
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
        shutil.rmtree(test.tmp, ignore_errors=True)
