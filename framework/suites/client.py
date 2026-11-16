"""The client-server contracts that only show through the clients.

These are the result ads that stash_plugin writes for HTCondor, which
decide whether a job retries; failing over from one cache to another
partway through a download, which needs the servers' ETags to agree;
which token the client presents, of those its environment offers; what
each set of storage scopes lets each client do; how
`pelican token create` finds an issuer; and the URL that
`pelican object share` hands out. The suite reads what the clients
report. See docs/tests/client-contracts.md.

The suite's objects go under /protected-a/data/client/<run>/, put in
every origin's store, and are removed afterward. It needs /protected-a.
The clients present tokens of the session's profile, and
`token-create-discovery` mints its tokens in it; the objects are put in
place, the caches filled, and the stores read, with WLCG tokens either
way.
"""

import itertools
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urlsplit

from testlib import (
    cacheapi,
    common,
    credentials,
    digests,
    director,
    issuer,
    jwt,
    stores,
    suite,
    transfers,
    web,
)
from testlib.common import die, first, outcome
from testlib.issuer import User
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line, not_refused
from testlib.suite import NAMESPACE, CaseRow

SCENARIOS = {
    "plugin-ads": (
        "stash_plugin's result ads: bytes, URL, type, name, times, attempts, cache age,"
        " checksums, what failures say, and an upload's"
    ),
    "failover": (
        "a download that fails partway at one cache resumes at the next, by Range, with the"
        " ETag unchanged (a V2 cache beside another)"
    ),
    "etag-agreement": "every server reports one ETag by HEAD and by GET; a cache's is an origin's",
    "token-discovery": (
        "which token the client and the plugin present: BEARER_TOKEN, BEARER_TOKEN_FILE,"
        " _CONDOR_CREDS, the director's hints, and a URL's scheme prefix"
    ),
    "url-forms": (
        "a bare path with the federation configured, by -f, or in the environment; osdf:///"
        " and stash:/// with -f; and osdf:// with two slashes, by the client and the plugin"
    ),
    "token-create-discovery": (
        "`pelican token create` finds the issuer whose keys match the signing key, and says when"
        " none does, or none is needed"
    ),
    "share-url": (
        "`pelican object share` hands out a URL whose token reads the collection: through a"
        " director as curl, and by both clients; with --write, each client puts with it"
    ),
    "storage-scopes": (
        "each set of storage scopes, by the client, the plugin, and curl: get, direct get,"
        " delete, and put of a new object or over one, alone or in a tree, overwrites off and on"
    ),
    "director-decision": "the plugin records the director's decision when asked",
    "skipstat-failover": (
        "a direct read with ?skipstat of an object one origin alone holds comes back, by the client"
        " and the plugin (several origins)"
    ),
    "multi-source": (
        "`get` and `put` with several sources: a directory, a collection, or refused"
        " (G8-G10, P8-P10); and ?recursive, by the client and the plugin"
    ),
    "preferred-cache-down": (
        "a preferred cache that is down: with `+`, a director's cache serves; without, a contact"
        " error; by the client and the plugin"
    ),
    "async-job": (
        "`get --async` and `copy --async` through the client agent: the bytes, `job status`"
        " and `list`, a finished job cancelled, and the agent stopped"
    ),
}

# The scenarios that present no token of the tests' minting (see
# suites/__init__.py): the share's is the embedded issuer's.
ONE_PROFILE = ("share-url",)

OBJECT_SIZE = 100_000
# `failover`'s object, and the block that cache-0's copy loses: in its
# middle, past what the cache reads before it answers, so that the
# download fails after it began.
FAILOVER_SIZE = 1 << 20
FAILOVER_BLOCK = 128
# How long to wait out the issuer's limit on client registrations, five
# from an address and then one a minute (as the issuer suite's
# `registration` does), which a run soon after another may find spent.
REGISTRATION_WAIT = 61
# How many times skipstat-failover reads, looking for a try at the origin
# that lacks the object.
SKIPSTAT_TRIES = 5
# The plugin's exit statuses (cmd/plugin.go).
RETRYABLE_EXIT = 11
# The error the client reports when it has no token to send
# (error_codes: Authorization.TokenNotFound).
NO_TOKEN = 4010


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None if there is something.

    The suite needs /protected-a.
    """
    return suite.unexported(fed)


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


class Test(suite.Test):
    """The run's objects in /protected-a, and how the clients run."""

    def __init__(self, session: Session):
        super().__init__(session, "client")

    def place(self, name: str, data: bytes, origins: Sequence[common.Origin] | None = None) -> None:
        """Put object name, holding data, in every store, or at origins.

        See stores.put_in_place() and stores.unique().
        """
        rel = self.rel(name)
        where = list(origins) if origins is not None else stores.unique(self.fed.origins)
        errors = stores.put_in_place(self.session, where, self.namespace, rel, data)
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")

    def somewhere(self, rel: str, data: bytes) -> bool:
        """Return whether some origin holds data at rel in /protected-a."""
        return common.digest(data) in stores.held_anywhere(
            self.fed.origins, self.namespace, rel, {self.namespace: self.setup}
        )

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
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
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
        env: Mapping[str, str] | None = None,
        upload: bool = False,
        sources: Sequence[str] | None = None,
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
        local = sources or [transfers.local_name(url) for url in urls]
        status, out, err, ads = self.session.plugin_run(
            f"{self.area}/{name}", list(zip(urls, local)), upload=upload, env=env, cwd=directory
        )
        return Fetched(status, ads or err or out, directory)

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

    def aged(self, cache: common.Cache, rel: str) -> bool:
        """Return whether a cache says it has held rel a second or more."""
        return (self.age(cache, rel) or 0) >= 1


def attempt_failed(attempt: Mapping[str, Any]) -> bool:
    """Return whether the stats say an attempt failed.

    A success's `error` is null, and a failure's is not.
    """
    return attempt.get("error") is not None


# --------------------------------------------------------------------------
# plugin-ads


def ad_problems(ad: Ad, url: str, data: bytes, kind: str = "download") -> list[str]:
    """Return what is wrong with the plugin's ad of a transfer of url.

    The transfer, a download or an upload (kind), must have succeeded,
    and url holds data. An upload's TransferFileName is the local file's
    base name, which here is the object's.
    """
    problems: list[str] = []
    name = transfers.local_name(url)
    if ad.get_ci("TransferSuccess") is not True:
        return [f"{name}: TransferSuccess {ad.get_ci('TransferSuccess')!r}: {ad.get_ci('TransferError')}"]
    for attr, want in (
        ("TransferFileBytes", len(data)),
        ("TransferTotalBytes", len(data)),
        ("TransferUrl", url),
        ("TransferType", kind),
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
        int(m.group(1)) for key in developer_data(ad) for m in [re.fullmatch(r"Endpoint(\d+)", key)] if m
    ]
    return max(numbers, default=0)


def checksum_problems(fed: common.Federation, ad: Ad, data: bytes, cache: str | None) -> list[str]:
    """Return what is wrong with an ad's ServerChecksums.

    Whichever digests it reports, of those the client knows
    (digests.CLIENT_ALGORITHMS), must be the object's, and after a whole
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
    want = {kind: digests.compute(kind, data).hex() for kind in digests.CLIENT_ALGORITHMS}
    for kind, value in reported.items():
        if kind in want and value.lower() != want[kind]:
            problems.append(f"{name}: ServerChecksums.{kind} {value}, not {want[kind]}")
    if sums is not None and not isinstance(sums, Ad):
        problems.append(f"{name}: ServerChecksums {sums!r}, not a nested ad")
    v2 = any(c.kind == "v2" and c.svc == cache for c in fed.caches)
    if v2 and "crc32c" not in reported:
        if sums is None:
            problems.append(f"{name}: no ServerChecksums, so no crc32c")
        elif isinstance(sums, Ad):
            problems.append(f"{name}: no crc32c in ServerChecksums, only {sorted(reported)}")
    return problems


def served_by(ad: Ad) -> str:
    """Return the host of the last attempt in an ad's DeveloperData."""
    found = developer_data(ad).get_ci(f"Endpoint{last_attempt(ad)}")
    return str(found or "").partition(":")[0]


def cache_hit_problems(fed: common.Federation, ad: Ad, cold_host: str | None) -> list[str]:
    """Return what is wrong with an ad's DataAge and CacheHit.

    Those of its last attempt, which a cache's Age gives, must say: on a
    cold read (cold_host is None), no hit; on a warm one at the cache
    that the cold read filled (cold_host), a hit, with an age. A warm read
    sent to another cache is cold there, and says nothing.
    """
    name = ad.get_ci("TransferFileName")
    data_ad = developer_data(ad)
    n = last_attempt(ad)
    host = served_by(ad)
    if not any(c.svc == host for c in fed.caches):
        return []
    hit, age = data_ad.get_ci(f"CacheHit{n}"), data_ad.get_ci(f"DataAge{n}")
    if cold_host is None and hit is True:
        return [f"{name}: cold, but CacheHit{n} is true (DataAge{n} {age!r})"]
    if cold_host == host and (hit is not True or not isinstance(age, (int, float)) or age <= 0):
        return [f"{name}: warm at {host}, but CacheHit{n} {hit!r} and DataAge{n} {age!r}"]
    return []


def ads_by_url(ads: Sequence[Ad], urls: Sequence[str]) -> tuple[dict[str, Ad], list[str]]:
    """Return a batch's ads by their TransferUrl, and what is amiss.

    The plugin writes each transfer's ad as it finishes, so their order is
    not the batch's (cmd/plugin.go).

    Returns:
        The ad of each of urls that has one, and the problems: an ad of a
        URL the batch did not name, a second ad of one, or none.
    """
    found: dict[str, Ad] = {}
    problems: list[str] = []
    for ad in ads:
        url = str(ad.get_ci("TransferUrl") or "")
        if url not in urls:
            problems.append(f"an ad of {url!r}, which the batch did not name")
        elif url in found:
            problems.append(f"{transfers.local_name(url)}: two ads")
        else:
            found[url] = ad
    problems += [f"{transfers.local_name(url)}: no ad" for url in urls if url not in found]
    return found, problems


def exit_problem(fetch: Fetched, ads: Sequence[Ad]) -> str | None:
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


def failed_ad_row(case: str, fetch: Fetched, want_type: str, want_code: int | None) -> CaseRow:
    """Return case's row: the plugin's ad of one transfer, which failed.

    The ad must say that it failed, and is not to be retried, with an
    error of type want_type, and of code want_code if one is given; the
    exit status must agree (exit_problem()); and the plugin must have
    left nothing in its directory.
    """
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
    return (case, *outcome(problems, f"{want_type}, exit {fetch.status}"))


def wait_aged(test: Test, cold_hosts: Mapping[str, str]) -> list[str]:
    """Wait until each cache that served a cold read has held it a second.

    A cache says it holds an object, with Age, a moment after serving it
    whole; a read before then is cold there again. The plugin counts a
    hit only once Age is above 0 (cmd/plugin.go), so wait for a second
    of it.

    Args:
        test: The run.
        cold_hosts: The host that served each object's cold read, by the
            object's name.

    Returns:
        A problem for each cache that did not say so within
        suite.HELD_WAIT seconds.
    """
    problems: list[str] = []
    for name, host in cold_hosts.items():
        for cache in test.fed.federated_caches:
            if director.host(cache.url) != host:
                continue
            if not common.wait_for(partial(test.aged, cache, test.rel(name)), suite.HELD_WAIT, interval=1):
                problems.append(
                    f"{cache.svc} did not say it holds {name}, with an Age of a"
                    + f" second or more, within {suite.HELD_WAIT}s of the cold read"
                )
    return problems


def download_ads(
    test: Test, name: str, urls: Sequence[str], objects: Mapping[str, bytes], env: Mapping[str, str]
) -> tuple[list[str], dict[str, Ad]] | str:
    """Have stash_plugin download a batch, and check its ads.

    Each of urls must have one ad (ads_by_url()), which must agree with
    its transfer (ad_problems()) and report the object's digests
    (checksum_problems()); the file must be the object; and the exit
    status must agree with the ads (exit_problem()).

    Args:
        test: The run.
        name: The name of the plugin's directory.
        urls: Each object's URL, in objects' order.
        objects: Each object's content, by name.
        env: The plugin's environment.

    Returns:
        The problems, and the ad of each download that succeeded, by its
        object's name; or, if the ads cannot be read, why.
    """
    fetch = test.plugin(name, urls, env)
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        return f"unreadable ads ({e}): {last_line(fetch.said)}"
    by_url, problems = ads_by_url(ads, urls)
    succeeded: dict[str, Ad] = {}
    for url, (obj, data) in zip(urls, objects.items()):
        ad = by_url.get(url)
        if ad is None:
            continue
        problems += ad_problems(ad, url, data)
        if ad.get_ci("TransferSuccess") is True:
            succeeded[obj] = ad
            problems += checksum_problems(test.fed, ad, data, served_by(ad))
            if not common.file_holds(os.path.join(fetch.directory, obj), data):
                problems.append(f"{obj}: the file is not the object")
    why = exit_problem(fetch, ads)
    if why:
        problems.append(why)
    return problems, succeeded


def routed_ads(
    test: Test, case: str, objects: Mapping[str, bytes], env: Mapping[str, str], query: str, servers: str
) -> CaseRow:
    """Return case's row: the ads of a batch that the client routes itself.

    Each download must be served by one of the case's servers, and one
    served by an origin may not count as a cache hit.

    Args:
        test: The run.
        case: The row's case, and what the plugin's directory is named
            after.
        objects: Each object's content, by name.
        env: The plugin's environment.
        query: Each URL's query, if any.
        servers: Which servers must serve: `origin`, or the first
            federated cache's service.
    """
    urls = [test.pelican_url(test.rel(name), query) for name in objects]
    found = download_ads(test, f"ads-{case}", urls, objects, env)
    if isinstance(found, str):
        return (case, FAIL, found)
    problems, ads = found
    origins = {director.host(o.url) for o in test.fed.origins}
    hosts = origins if servers == "origin" else {servers}
    for name, ad in ads.items():
        host = served_by(ad)
        if host not in hosts:
            problems.append(f"{name}: served by {host!r}, not {sorted(hosts)}")
        hit = developer_data(ad).get_ci(f"CacheHit{last_attempt(ad)}")
        if host in origins and hit is True:
            problems.append(f"{name}: served by {host}, an origin, but CacheHit is true")
    served = sorted({served_by(ad) for ad in ads.values()})
    return (case, *outcome(problems, f"{len(ads)} ads agree with the transfers ({served})"))


def plugin_ads(test: Test) -> list[CaseRow]:
    """Check that stash_plugin's result ads agree with its transfers.

    The ads (cmd/plugin.go), which HTCondor reads, are those of a batch of
    two objects by the director's choice, cold and then warm; of the same
    batch from an origin (?directread), and from the first federated
    cache, named (Client.PreferredCaches); of an object that does not
    exist; of a refused download; of a batch with one of each; and, where
    /protected-a takes writes, of an upload of two files, and of one
    refused.
    """
    objects = {name: os.urandom(OBJECT_SIZE) for name in ("ads-a", "ads-b")}
    for name, data in objects.items():
        test.place(name, data)
    names = list(objects)
    urls = [test.pelican_url(test.rel(name)) for name in names]
    env = test.creds_env(test.server_creds("plugin-ads"))
    rows: list[CaseRow] = []
    cold_hosts: dict[str, str] = {}
    for case in ("cold", "warm"):
        waited = wait_aged(test, cold_hosts) if case == "warm" else []
        found = download_ads(test, f"ads-{case}", urls, objects, env)
        if isinstance(found, str):
            rows.append((case, FAIL, found))
            continue
        problems, ads = found
        problems = waited + problems
        for name, ad in ads.items():
            problems += cache_hit_problems(test.fed, ad, cold_hosts.get(name) if case == "warm" else None)
            if case == "cold":
                cold_hosts[name] = served_by(ad)
        served = sorted({served_by(ad) for ad in ads.values()})
        rows.append((case, *outcome(problems, f"{len(ads)} ads agree with the transfers ({served})")))

    if test.direct:
        rows.append(routed_ads(test, "direct", objects, env, "directread", "origin"))
    else:
        rows.append(("direct", SKIP, "/protected-a takes no direct reads"))
    if test.fed.federated_caches:
        cache = test.fed.federated_caches[0]
        named_env = {**env, "PELICAN_CLIENT_PREFERREDCACHES": cache.url}
        rows.append(routed_ads(test, "named", objects, named_env, "", director.host(cache.url)))
    else:
        rows.append(("named", SKIP, "this shape has no federated cache to name"))

    rows.append(
        failed_ad_row(
            "dne", test.plugin("ads-dne", [test.pelican_url(test.rel("dne"))], env), "Specification", 5011
        )
    )
    unknown_env = test.creds_env(test.creds("plugin-ads-refused", {"scitokens.use": unknown_token(test)}))
    rows.append(
        failed_ad_row("refused", test.plugin("ads-refused", [urls[0]], unknown_env), "Authorization", None)
    )
    # A failed download ends a batch: the plugin writes the ads of what
    # finished before it, then the failure's, and no more (cmd/plugin.go).
    # So the existing object's ad may come first, or not at all.
    missing = test.pelican_url(test.rel("dne"))
    fetch = test.plugin("ads-mixed", [urls[0], missing], env)
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        rows.append(("mixed", FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}"))
    else:
        problems = list[str]()
        if not ads:
            problems.append("no ads for two transfers")
        elif len(ads) > 2:
            problems.append(f"{len(ads)} ads for two transfers")
        else:
            last = ads[-1]
            if last.get_ci("TransferUrl") != missing:
                problems.append(f"the last ad is of {last.get_ci('TransferUrl')!r}, not {missing!r}")
            elif last.get_ci("TransferSuccess") is not False:
                problems.append(f"the missing object's TransferSuccess {last.get_ci('TransferSuccess')!r}")
            for ad in ads[:-1]:
                problems += ad_problems(ad, urls[0], objects[names[0]])
        why = exit_problem(fetch, ads)
        if why:
            problems.append(why)
        note = f"{len(ads)} ad(s), the last the missing object's failure, exit {fetch.status}"
        rows.append(("mixed", *outcome(problems, note)))
    rows += upload_ads(test)
    return rows


def upload_ads(test: Test) -> list[CaseRow]:
    """Return the rows of stash_plugin's ads of an upload (`-upload`).

    Of a batch of two files to /protected-a (`upload`): each ad must say
    `TransferSuccess`, `TransferType` `upload`, `TransferFileName` the
    local file's name, the bytes, the URL, and the times, as
    ad_problems() says, and name an `Endpoint0`; the exit status must be
    0; and an origin must hold each file. Of an upload with a token
    signed by a key no server knows (`upload-refused`): an error of type
    `Authorization`, and no origin may hold the file.
    """
    if not test.writes:
        reason = "/protected-a takes no writes"
        return [("upload", SKIP, reason), ("upload-refused", SKIP, reason)]
    rows: list[CaseRow] = []
    files = {"ads-up-a": os.urandom(OBJECT_SIZE), "ads-up-b": os.urandom(OBJECT_SIZE // 2)}
    sources = [test.local(f"ads-up/{name}", data) for name, data in files.items()]
    urls = [test.pelican_url(test.rel(name)) for name in files]
    env = test.creds_env(test.server_creds("plugin-ads-upload"))
    fetch = test.plugin("ads-upload", urls, env, upload=True, sources=sources)
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        rows.append(("upload", FAIL, f"unreadable ads ({e}): {last_line(fetch.said)}"))
    else:
        by_url, problems = ads_by_url(ads, urls)
        for url, (name, data) in zip(urls, files.items()):
            ad = by_url.get(url)
            if ad is None:
                continue
            problems += ad_problems(ad, url, data, "upload")
            if not test.somewhere(test.rel(name), data):
                problems.append(f"{name}: no origin holds it")
        why = exit_problem(fetch, ads)
        if why:
            problems.append(why)
        served = sorted({served_by(ad) for ad in ads if ad.get_ci("TransferSuccess") is True})
        rows.append(("upload", *outcome(problems, f"{len(ads)} ads agree with the uploads ({served})")))
    unknown_env = test.creds_env(
        test.creds("plugin-ads-upload-refused", {"scitokens.use": unknown_token(test)})
    )
    name = "ads-up-refused"
    source = test.local(f"ads-up/{name}", os.urandom(OBJECT_SIZE // 4))
    fetch = test.plugin(
        "ads-upload-refused", [test.pelican_url(test.rel(name))], unknown_env, upload=True, sources=[source]
    )
    row = failed_ad_row("upload-refused", fetch, "Authorization", None)
    if any(stores.held_anywhere(test.fed.origins, NAMESPACE, test.rel(name), {NAMESPACE: test.setup})):
        row = (
            "upload-refused",
            FAIL,
            "an origin holds the file, though the upload was refused",
        )
    rows.append(row)
    return rows


def unknown_token(test: Test) -> str:
    """Return a token like the server's, signed by a key no server knows.

    See credentials.CREDENTIALS, `unknown`.
    """
    return credentials.mint(
        test.fed,
        test.session.path("tokens/client.unknown"),
        f"/{NAMESPACE}/",
        credentials.UNKNOWN_KEY,
        test.fed.issuer_of(NAMESPACE),
        credentials.RWM,
        profile=test.session.profile,
    )


# --------------------------------------------------------------------------
# failover


def spoil(test: Test, cache: common.Cache, name: str, block: int) -> str | None:
    """Have a V2 cache corrupt block of its copy of object name.

    Ask through its chaos API, as its admin.

    Returns:
        Why that failed, or None.
    """
    token = cacheapi.admin_token(test.session, cache)
    url = cacheapi.object_url(test.fed, test.path(test.rel(name)))
    answer = cacheapi.corrupt(cache, token, url, block)
    if answer.status == 404:
        return f"{cache.svc} has no chaos API (Cache.EnableChaosAPI)"
    if answer.status != 200:
        return f"{cache.svc}'s chaos API got {answer.describe()}"
    return None


def failover(test: Test) -> list[CaseRow]:
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
    attempts. With several origins, the director asks them which holds
    the object, and none does once it is removed, so the scenario skips.
    """
    feds = test.fed.federated_caches
    v2 = [c for c in feds if c.kind == "v2"]
    if not v2 or len(feds) < 2:
        return [("", SKIP, "no federated V2 cache beside another cache ('-p topo-multi-cache')")]
    if test.fed.sent_cache_control:
        return [
            (
                "",
                SKIP,
                f"the origins send Cache-Control {test.fed.sent_cache_control!r}, so a cache"
                + " would revalidate against origins that no longer hold the object",
            )
        ]
    if len(test.fed.origins) > 1:
        return [
            (
                "",
                SKIP,
                "several origins, which the director asks for the object, and which no longer hold it",
            )
        ]
    first_cache = v2[0]
    second = next(c for c in feds if c.svc != first_cache.svc)
    rows: list[CaseRow] = []
    for case in ("get", "plugin"):
        name = f"failover-{case}"
        data = os.urandom(FAILOVER_SIZE)
        test.place(name, data)
        for cache in (first_cache, second):
            why = test.fill(cache, test.rel(name), data)
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
                    test.pelican_url(test.rel(name)),
                    "object",
                ],
            )
            problems = get_failover_problems(fetch, data, first_cache, second)
        else:
            env = test.creds_env(test.server_creds(name), PELICAN_CLIENT_PREFERREDCACHES=preferred)
            url = test.pelican_url(test.rel(name))
            fetch = test.plugin(name, [url], env)
            problems = plugin_failover_problems(fetch, url, data, first_cache, second)
        rows.append((case, *outcome(problems, f"failed at {first_cache.svc}, resumed at {second.svc}")))
    return rows


def get_failover_problems(
    fetch: Fetched, data: bytes, first_cache: common.Cache, second: common.Cache
) -> list[str]:
    """Return what is wrong with how `pelican object get` failed over."""
    if fetch.status != 0:
        return [f"exit {fetch.status}: {last_line(fetch.said)}"]
    problems: list[str] = []
    if not common.file_holds(os.path.join(fetch.directory, "object"), data):
        problems.append("the file is not the object")
    results = transfers.stats_results(transfers.read_stats(os.path.join(fetch.directory, "stats.json")))
    attempts = transfers.attempts_of(results[0]) if results else []
    if len(attempts) < 2:
        return problems + [f"{len(attempts)} attempt(s) in the stats, not a failure and a resume"]
    hosts = [transfers.endpoint_host(a) for a in attempts]
    failed = [a for a in attempts if attempt_failed(a)]
    if not failed or transfers.endpoint_host(failed[0]) != first_cache.svc:
        problems.append(f"no failed attempt at {first_cache.svc}: tried {hosts}")
    elif int(failed[0].get("transferFileBytes") or 0) <= 0:
        problems.append(f"the failed attempt at {first_cache.svc} brought no bytes")
    last = attempts[-1]
    if attempt_failed(last) or transfers.endpoint_host(last) != second.svc:
        problems.append(f"the last attempt is not a success at {second.svc}: {hosts}")
    moved = sum(int(a.get("transferFileBytes") or 0) for a in attempts)
    if not problems and moved != len(data):
        problems.append(
            f"the attempts moved {moved} bytes in all, not {len(data)}: the resume did not"
            + " carry on from where the first attempt stopped"
        )
    return problems


def plugin_failover_problems(
    fetch: Fetched, url: str, data: bytes, first_cache: common.Cache, second: common.Cache
) -> list[str]:
    """Return what is wrong with how stash_plugin failed over.

    It fetched url, which holds data, into the file of its last
    component (Test.plugin()).
    """
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        return [f"unreadable ads ({e}): {last_line(fetch.said)}"]
    if len(ads) != 1 or ads[0].get_ci("TransferSuccess") is not True:
        return [f"no ad of a success: {last_line(fetch.said)}"]
    ad = ads[0]
    problems: list[str] = []
    local = os.path.join(fetch.directory, transfers.local_name(url))
    if not common.file_holds(local, data):
        problems.append("the file is not the object")
    found = developer_data(ad)
    endpoints = {
        n: str(found.get_ci(f"Endpoint{n}") or "").partition(":")[0] for n in range(last_attempt(ad) + 1)
    }
    if endpoints.get(0) != first_cache.svc or not found.get_ci("TransferError0"):
        problems.append(f"no failed first attempt at {first_cache.svc}: {endpoints}")
    if endpoints.get(last_attempt(ad)) != second.svc:
        problems.append(f"the last attempt is not at {second.svc}: {endpoints}")
    return problems


# --------------------------------------------------------------------------
# etag-agreement


def head_and_get_etag(test: Test, svc: str, url: str, rel: str) -> tuple[str, list[str]]:
    """Return the ETag that svc, at url, reports of rel, and what is amiss.

    A server may send no ETag at all (RFC 9110, section 8.8.3); one that
    it sends by HEAD or by GET, it must send by both, the same.

    Returns:
        The GET's ETag, or "" if it sent none; and the problems.
    """
    problems: list[str] = []
    etags: dict[str, str] = {}
    for method in ("HEAD", "GET"):
        answer = test.ask(method, url, rel)
        if answer.status != 200:
            problems.append(f"{svc}: {method} got {answer.describe()}")
        elif answer.header("ETag"):
            etags[method] = answer.header("ETag")
    if problems:
        return etags.get("GET", ""), problems
    if len(etags) == 1:
        sent = next(iter(etags))
        lacking = "GET" if sent == "HEAD" else "HEAD"
        problems.append(f"{svc}: {lacking} sent no ETag, but {sent} sent {etags[sent]}")
    elif len(etags) == 2 and etags["HEAD"] != etags["GET"]:
        problems.append(f"{svc}: HEAD's ETag {etags['HEAD']} is not GET's {etags['GET']}")
    return etags.get("GET", ""), problems


def etag_agreement(test: Test) -> tuple[str, str]:
    """Check that each server gives an object one ETag, by HEAD and GET.

    An object put at every origin: each origin that takes direct
    clients, and each federated cache, must report one ETag for it by
    HEAD and by GET, or none by either (head_and_get_etag()), and a
    cache's must be one of the origins'. Origins that share a store must
    agree. Those with stores of their own hold copies of their own,
    whose ETags may differ; the note says so, and names the servers that
    send none, at which a client cannot tell that an object changed
    between attempts.
    """
    data = os.urandom(OBJECT_SIZE)
    test.place("etag", data)
    rel = test.rel("etag")
    problems: list[str] = []
    origin_etags: dict[str, str] = {}
    silent: list[str] = []
    for origin in test.fed.origins if test.direct else []:
        etag, why = head_and_get_etag(test, origin.svc, origin.url, rel)
        problems += why
        if etag:
            origin_etags[origin.svc] = etag
        elif not why:
            silent.append(origin.svc)
    for cache in test.fed.federated_caches:
        fill_problem = test.fill(cache, rel, data)
        if fill_problem:
            problems.append(fill_problem)
            continue
        etag, why = head_and_get_etag(test, cache.svc, cache.url, rel)
        problems += why
        if not etag and not why:
            silent.append(cache.svc)
        if etag and origin_etags and etag not in origin_etags.values():
            problems.append(f"{cache.svc}'s ETag {etag} is none of the origins' {origin_etags}")
    shared: dict[str, set[str]] = {}
    for origin in test.fed.origins:
        if origin.svc in origin_etags:
            shared.setdefault(origin.store, set()).add(origin_etags[origin.svc])
    for store, found in shared.items():
        if len(found) > 1:
            problems.append(f"the origins sharing framework/var/{store} report {sorted(found)}")
    distinct = len(set(origin_etags.values()))
    note = f"{len(origin_etags)} origin(s), {len(test.fed.federated_caches)} cache(s)"
    if distinct > 1:
        note += (
            f"; the origins' copies have {distinct} ETags, so a client cannot resume at a cache"
            " filled from another origin"
        )
    if silent:
        note += (
            f"; {', '.join(silent)} sent no ETag, so a client resuming there cannot tell a changed object"
        )
    return outcome(problems, note)


# --------------------------------------------------------------------------
# token-discovery


def discovery_get(test: Test, name: str, url: str, env: Mapping[str, str]) -> Fetched:
    """Run `pelican object get` of url, with env, into a new directory.

    Into a directory, as the transfers suite gets, so that a refusal
    reads as testlib/transfers.py expects.
    """
    return test.pelican(name, ["object", "get", url, "."], env)


def exit_problems(fetch: Fetched) -> list[str]:
    """Return the client's exit status, as a problem, unless it is 0."""
    return [f"exit {fetch.status}: {last_line(fetch.said)}"] if fetch.status != 0 else []


def got(fetch: Fetched, data: bytes, local: str = "object") -> str | None:
    """Return why the download did not bring data, or None if it did.

    It must bring it to file local in its directory.
    """
    if fetch.status != 0:
        return f"exit {fetch.status}: {last_line(fetch.said)}"
    if not common.file_holds(os.path.join(fetch.directory, local), data):
        return "the file is not the object"
    return None


def got_named(fetch: Fetched, data: bytes) -> str | None:
    """Return got() of the discovery object, under its own name."""
    return got(fetch, data, "discovery")


def refused(fetch: Fetched) -> str | None:
    """Return why the download was not refused, or None (not_refused())."""
    return not_refused(fetch.status, fetch.said)


def plugin_refused(fetch: Fetched) -> str | None:
    """Return why the plugin's download was not refused, or None.

    Its ad must report an error of type `Authorization`.
    """
    if fetch.status == 0:
        return "succeeded, but should have been refused"
    try:
        ads = parse_ads(fetch.said)
    except ValueError as e:
        return f"unreadable ads ({e}): {last_line(fetch.said)}"
    if not any(kind == "Authorization" for ad in ads for kind, _ in error_types(ad)):
        return f"failed, but not by refusal: {last_line(fetch.said)}"
    return None


# How each client runs a token-discovery case: its row prefix, how it
# gets an object, and how a refusal shows.
Discoverer = tuple[
    str,
    Callable[[Test, str, str, Mapping[str, str]], Fetched],
    Callable[[Fetched], str | None],
]
DISCOVERERS: tuple[Discoverer, ...] = (
    ("", discovery_get, refused),
    ("plugin-", lambda test, name, url, env: test.plugin(name, [url], env), plugin_refused),
)


def token_discovery(test: Test) -> list[CaseRow]:
    """Check which token each client presents, of those in its environment.

    The cases (tokenContentIterator in client/acquire_token.go, which
    stash_plugin runs too, through the client's transfer engine):
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
    scitokens.use (GetTokenName in pelican_url/parse.go). Each runs by
    `pelican object get`, and again by stash_plugin, as `plugin-<case>`.
    """
    name = "discovery"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data)
    url = test.pelican_url(test.rel(name))
    base = dict(test.session.env)
    wrong_path = test.session.credential_token(credentials.BY_NAME["wrong-path"], NAMESPACE, "get")
    unknown = unknown_token(test)
    good_creds = test.server_creds("discovery-good")
    rows: list[CaseRow] = []
    for prefix, get, denied in DISCOVERERS:

        def row(case: str, why: str | None, passed: str = "", client: str = prefix) -> CaseRow:
            return (client + case, FAIL, why) if why else (client + case, PASS, passed)

        env = {**base, "BEARER_TOKEN": test.token}
        rows.append(row("bearer-token", got_named(get(test, f"{prefix}bearer-token", url, env), data)))
        env = {**base, "BEARER_TOKEN_FILE": test.token_file}
        rows.append(
            row(
                "bearer-token-file",
                got_named(get(test, f"{prefix}bearer-token-file", url, env), data),
            )
        )
        env = {**base, "_CONDOR_CREDS": good_creds}
        rows.append(row("condor-creds", got_named(get(test, f"{prefix}condor-creds", url, env), data)))
        env = {**base, "BEARER_TOKEN": wrong_path or "", "_CONDOR_CREDS": good_creds}
        rows.append(
            row(
                "skips-unacceptable",
                got_named(get(test, f"{prefix}skips-unacceptable", url, env), data),
                "the wrong-path token was passed over",
            )
        )
        env = {**base, "BEARER_TOKEN": unknown, "_CONDOR_CREDS": good_creds}
        rows.append(
            row(
                "acceptable-wins",
                denied(get(test, f"{prefix}acceptable-wins", url, env)),
                "the unknown key's token was presented, and refused",
            )
        )
        # The scheme prefix: the good token under the name the prefix
        # selects, and the unknown key's as scitokens.use, which would
        # otherwise win.
        for case, scheme, file_name in (
            ("prefix", NAMESPACE, f"{NAMESPACE}.use"),
            (
                "prefix-underscore",
                NAMESPACE.replace("-", "_"),
                f"{NAMESPACE.replace('-', '.')}.use",
            ),
        ):
            creds = test.creds(
                f"{prefix}discovery-{case}", {"scitokens.use": unknown, file_name: test.token}
            )
            prefixed = f"{scheme}+{url}"
            env = {**base, "_CONDOR_CREDS": creds}
            why = got_named(get(test, f"{prefix}{case}", prefixed, env), data)
            rows.append(row(case, why, f"{file_name} was selected"))
    return rows


# --------------------------------------------------------------------------
# url-forms


def url_forms(test: Test) -> list[CaseRow]:
    """Check the forms of a URL that the clients take, of one object.

    Pelican's URLs (pelican_url/parse.go): a bare path, which needs a
    federation to be given, as the dev container's configuration does
    (`configured`), as `-f` does (`flag`), and as
    PELICAN_FEDERATION_DISCOVERYURL does (`env`); `osdf:///` and
    `stash:///` URLs, which take the OSDF's federation unless one is
    given, as it is here, by `-f` (`osdf`, `stash`); and `osdf://` with
    two slashes, which the client reads as three (`osdf-two-slashes`).
    Each must get the object by `pelican object get`. Where /protected-a
    takes writes, `pelican object put` to an `osdf:///` URL, with `-f`,
    must land the file at an origin (`put-osdf`). And stash_plugin,
    which HTCondor hands `osdf:///` URLs, with the federation in its
    environment, must get the object (`plugin-osdf`).
    """
    name = "forms"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data)
    path = test.path(test.rel(name))
    discovery = test.fed.discovery_url
    base = dict(test.session.env)
    base.pop("PELICAN_FEDERATION_DISCOVERYURL", None)
    token = ["--token", test.token_file]
    rows: list[CaseRow] = []

    def get(case: str, args: Sequence[str], env: Mapping[str, str], note: str) -> CaseRow:
        fetch = test.pelican(case, ["object", "get", *args, "."], env)
        why = got(fetch, data, name)
        return (case, FAIL, why) if why else (case, PASS, note)

    if test.fed.standalone:
        rows.append(("configured", SKIP, "a standalone origin's client is told of no federation"))
    else:
        rows.append(get("configured", [*token, path], base, "the configured federation"))
    rows.append(get("flag", ["-f", discovery, *token, path], base, f"-f {discovery}"))
    env = {**base, "PELICAN_FEDERATION_DISCOVERYURL": discovery}
    rows.append(get("env", [*token, path], env, "PELICAN_FEDERATION_DISCOVERYURL"))
    for case, url in (
        ("osdf", f"osdf://{path}"),
        ("stash", f"stash://{path}"),
        ("osdf-two-slashes", f"osdf:/{path}"),
    ):
        rows.append(get(case, ["-f", discovery, *token, url], base, url))
    if not test.writes:
        rows.append(("put-osdf", SKIP, "/protected-a takes no writes"))
    else:
        upload = os.urandom(OBJECT_SIZE // 2)
        source = test.local("forms-up", upload)
        url = f"osdf://{test.path(test.rel('forms-up'))}"
        fetch = test.pelican("put-osdf", ["object", "put", "-f", discovery, *token, source, url])
        problems = exit_problems(fetch)
        if not test.somewhere(test.rel("forms-up"), upload):
            problems.append("no origin holds the file")
        rows.append(("put-osdf", *outcome(problems, f"landed by {url}")))
    env = test.creds_env(test.server_creds("forms"), PELICAN_FEDERATION_DISCOVERYURL=discovery)
    url = f"osdf://{path}"
    fetch = test.plugin("plugin-osdf", [url], env)
    why = got(fetch, data, name)
    rows.append(("plugin-osdf", FAIL, why) if why else ("plugin-osdf", PASS, url))
    return rows


# --------------------------------------------------------------------------
# token-create-discovery


def create_token(
    test: Test, name: str, url: str, key: str, *flags: str
) -> tuple[dict[str, Any] | None, str, Fetched]:
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


def issued_row(
    case: str, claims: dict[str, Any] | None, fetch: Fetched, issuer_url: str, *, named: str, passed: str
) -> CaseRow:
    """Return case's row: create_token() minted a token of issuer_url.

    A failure names issuer_url as named; a pass says passed.
    """
    if claims is None:
        return (case, FAIL, f"exit {fetch.status}: {last_line(fetch.said)}")
    if claims.get("iss") != issuer_url:
        return (case, FAIL, f"iss {claims.get('iss')!r}, not {named}")
    return (case, PASS, passed)


def declined_row(
    case: str, claims: dict[str, Any] | None, fetch: Fetched, hint: str, *, saying: str, passed: str
) -> CaseRow:
    """Return case's row: create_token() minted nothing, saying hint.

    A failure without hint says that it failed, but not saying (e.g.
    "with the hint"); a pass says passed.
    """
    if claims is not None:
        return (case, FAIL, f"minted a token of issuer {claims.get('iss')!r}")
    if hint not in fetch.said:
        return (case, FAIL, f"failed, but not {saying}: {last_line(fetch.said)}")
    return (case, PASS, passed)


def token_create_discovery(test: Test) -> list[CaseRow]:
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
    rows: list[CaseRow] = []
    name = "token-create/obj"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data)
    collection = test.pelican_url(test.rel("token-create")) + "/"
    read = credentials.SCOPES[test.session.profile]["read"]
    want_scope = f"{read}:/{test.rel('token-create')}"
    claims, token, fetch = create_token(test, "create-origin-key", collection, test.fed.origin_key)
    if claims is None:
        rows.append(("origin-key", FAIL, f"exit {fetch.status}: {last_line(fetch.said)}"))
    else:
        problems: list[str] = []
        if claims.get("iss") != test.fed.issuer_of(NAMESPACE):
            problems.append(f"iss {claims.get('iss')!r}, not /{NAMESPACE}'s issuer")
        if claims.get("scope") != want_scope:
            problems.append(f"scope {claims.get('scope')!r}, not {want_scope!r}")
        rows.append(("origin-key", *outcome(problems, f"iss and scope {want_scope}")))
        if not problems:
            servers = [(o.svc, o.url) for o in test.fed.origins if test.direct]
            servers += [(c.svc, c.url) for c in test.fed.federated_caches]
            for svc, server_url in servers:
                answer = web.request("GET", test.url(server_url, test.rel(name)), token=token)
                if answer.status != 200 or answer.body != data:
                    problems.append(f"{svc}: GET with the token got {answer.describe()}")
            rows.append(("reads", *outcome(problems, f"at {len(servers)} server(s)")))
    if credentials.namespace_issuers(test.fed):
        claims, _, fetch = create_token(
            test,
            "create-namespace-key",
            collection,
            credentials.namespace_key(NAMESPACE),
        )
        rows.append(
            issued_row(
                "namespace-key",
                claims,
                fetch,
                test.fed.issuer_of(NAMESPACE),
                named=f"/{NAMESPACE}'s issuer",
                passed="found through the export's own JWKS",
            )
        )
    else:
        rows.append(("namespace-key", SKIP, "no namespace has keys of its own"))
    claims, _, fetch = create_token(test, "create-unknown-key", collection, credentials.UNKNOWN_KEY)
    rows.append(
        declined_row(
            "unknown-key",
            claims,
            fetch,
            "matches your signing key",
            saying="for the key",
            passed="no issuer matches the key",
        )
    )
    why = suite.unexported(test.fed, "public")
    if why:
        rows.append(("public", SKIP, why))
    else:
        claims, _, fetch = create_token(
            test, "create-public", f"{test.fed.url}/public/{test.rel('token-create')}/", test.fed.origin_key
        )
        if credentials.has_issuer(test.fed.exports["public"]):
            want_issuer = test.fed.issuer_of("public")
            rows.append(
                issued_row(
                    "public",
                    claims,
                    fetch,
                    want_issuer,
                    named=f"/public's {want_issuer!r}",
                    passed="/public takes writes, so its issuer is found",
                )
            )
        else:
            rows.append(
                declined_row(
                    "public",
                    claims,
                    fetch,
                    "Are you sure you need a token",
                    saying="with the hint",
                    passed="asks whether a token is needed",
                )
            )
    owners = [o for o in test.fed.owners if any(e.prefix == "/other" for e in o.exports)]
    if not owners:
        rows.append(("other-owner", SKIP, "no other owner ('-p topo-multi-owner')"))
        return rows
    owner = owners[0]
    export = next(e for e in owner.exports if e.prefix == "/other")
    claims, _, fetch = create_token(
        test,
        "create-other-owner",
        f"{test.fed.url}/other/{test.rel('token-create')}/",
        owner.key,
    )
    rows.append(
        issued_row(
            "other-owner", claims, fetch, export.issuer, named=export.issuer, passed=f"iss {export.issuer}"
        )
    )
    return rows


# --------------------------------------------------------------------------
# share-url


def share_once(
    test: Test,
    flags: Sequence[str],
    collection: str,
    store: str,
    approver: Callable[[str, str], None],
) -> issuer.ClientRun:
    """Run `pelican object share <flags> <collection>`, approving its flow.

    The client keeps its credentials in store.
    """
    return issuer.run_approving(
        test.session.pelican,
        ["object", "share", *flags, collection],
        issuer.client_env(test.session.env, store),
        test.tmp,
        approver,
        log=test.session.log_command,
    )


def share_url(test: Test) -> list[CaseRow]:
    """Check the URL that `pelican object share` hands out, and its token.

    `pelican object share [--write]` of a collection in /protected-a,
    its device flow approved as the admin, whom the issuer grants the
    namespace (client/sharing_url.go, cmd/object_share.go): the URL must
    be the collection's with `?authz=<token>`, whose scope is the
    collection's read alone, and with --write its create (`url`,
    `url-write`); a GET of the object in it at a director's object
    route, and at its origin route where /protected-a takes direct
    reads, with the token in the query as curl sends it, must redirect
    with `authz` in Location and no token in Link, and the redirect
    followed must serve the object (`curl`); and `pelican object get` of
    the URL, with no other token, must serve it too (`get`), as must
    stash_plugin, with its credentials directory empty, as a job given
    the URL would run it (`plugin`). With the --write URL's token in its
    query, each client must land a new object in the collection
    (share_put()).
    """
    if not test.fed.embedded_issuer or test.fed.external_issuer:
        return [("", SKIP, "the share's token comes from the origins' embedded issuer")]
    if test.fed.standalone:
        return [("", SKIP, "a standalone origin has no director to find the issuer through")]
    name = "share/obj"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data)
    collection = test.pelican_url(test.rel("share"))
    admin = test.session.web_token(test.fed.origin_web_url)

    def approver(issuer_url: str, user_code: str) -> None:
        issuer.approve(issuer_url, user_code, User(token=admin))

    rows: list[CaseRow] = []
    tokens: dict[str, str] = {}
    for case, flags, want in (
        ("url", [], {f"storage.read:/{test.rel('share')}"}),
        ("url-write", ["--write"], {f"storage.create:/{test.rel('share')}"}),
    ):
        store = os.path.join(test.tmp, f"share-{case}.store")
        done = share_once(test, flags, collection, store, approver)
        if done.status != 0 and "rate_limit_exceeded" in done.stderr + done.stdout:
            # The client registers itself with the issuer first, which
            # allows an address five registrations, then one a minute.
            time.sleep(REGISTRATION_WAIT)
            done = share_once(test, flags, collection, store, approver)
        if done.status != 0 or done.problems:
            why = first(done.problems) if done.problems else last_line(done.stderr or done.stdout)
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
            scopes = jwt.scopes_of(token)
            if set(scopes) != want:
                problems.append(f"the token's scopes are {sorted(scopes)}, not {sorted(want)}")
            tokens[case] = token
        rows.append((case, *outcome(problems, f"{collection}?authz=<token of {sorted(want)}>")))
    if "url" not in tokens:
        return rows
    token = tokens["url"]
    path = test.path(test.rel(name))
    problems = []
    routes = ["object", "origin"] if test.direct else ["object"]
    for director_url in test.fed.directors:
        for route in routes:
            target = f"{director_url}/api/v1.0/director/{route}{path}?authz={quote(token, safe='')}"
            answer = web.request("GET", target)
            what = f"{director.host(director_url)} ({route})"
            if answer.status != 307:
                problems.append(f"{what}: {answer.describe()}, not 307")
                continue
            location = answer.header("Location")
            if f"authz={token}" not in location and f"authz={quote(token, safe='')}" not in location:
                problems.append(f"{what}: Location carries no authz: {location[:80]}")
            if token in answer.header("Link"):
                problems.append(f"{what}: Link carries the token")
            served = web.request("GET", location)
            if served.status != 200 or served.body != data:
                problems.append(f"{what}: following Location got {served.describe()}")
    note = f"{len(test.fed.directors)} director(s), by the {' and '.join(routes)} route(s)"
    rows.append(("curl", *outcome(problems, note)))
    fetch = test.pelican(
        "share-get", ["object", "get", f"{test.pelican_url(test.rel(name))}?authz={token}", "object"]
    )
    wrong = got(fetch, data)
    if wrong and f"Error code {NO_TOKEN}" in fetch.said:
        rows.append(("get", FAIL, f"the client found no token in the URL: {wrong}"))
    elif wrong:
        rows.append(("get", FAIL, wrong))
    else:
        rows.append(("get", PASS, "the client read the token from the URL"))
    shared = f"{test.pelican_url(test.rel(name))}?authz={token}"
    fetch = test.plugin("share-plugin", [shared], env=test.creds_env(test.creds("share-plugin", {})))
    if not transfers.plugin_passed(fetch.said):
        why = transfers.plugin_error(fetch.said) or last_line(fetch.said)
        rows.append(("plugin", FAIL, f"exit {fetch.status}: {why}"))
    elif not common.file_holds(os.path.join(fetch.directory, transfers.local_name(shared)), data):
        rows.append(("plugin", FAIL, "the file is not the object"))
    else:
        rows.append(("plugin", PASS, "stash_plugin read the token from the URL"))
    rows += share_put(test, tokens.get("url-write"))
    return rows


def share_put(test: Test, token: str | None) -> list[CaseRow]:
    """Check each client's put to a URL with a --write share's token.

    With no other token, a new object in the shared collection, put to
    its URL with ?authz=<token>, must land in an origin's store: by
    `pelican object put` (`put`); by stash_plugin, with its credentials
    directory empty (`plugin-put`); and by a PUT of its bare path at
    each director, followed as curl follows it (`curl-put`).
    """
    cases = ("put", "plugin-put", "curl-put")
    if token is None:
        return [(case, SKIP, "no --write share's token to put with") for case in cases]
    if not test.writes:
        return [(case, SKIP, "/protected-a takes no writes") for case in cases]
    rows: list[CaseRow] = []
    data = os.urandom(OBJECT_SIZE)
    source = os.path.join(test.directory("share-put-source"), "put")
    with open(source, "wb") as f:
        f.write(data)

    rel = test.rel("share/put")
    put = test.pelican("share-put", ["object", "put", source, f"{test.pelican_url(rel)}?authz={token}"])
    if put.status != 0:
        rows.append(("put", FAIL, f"exit {put.status}: {last_line(put.said)}"))
    elif not test.somewhere(rel, data):
        rows.append(("put", FAIL, "succeeded, but no origin holds the object"))
    else:
        rows.append(("put", PASS, "the client read the --write share's token from the URL"))

    rel = test.rel("share/plugin-put")
    shared = f"{test.pelican_url(rel)}?authz={token}"
    env = test.creds_env(test.creds("share-plugin-put", {}))
    put = test.plugin("share-plugin-put", [shared], env, upload=True, sources=[source])
    if not transfers.plugin_passed(put.said):
        why = transfers.plugin_error(put.said) or last_line(put.said)
        rows.append(("plugin-put", FAIL, f"exit {put.status}: {why}"))
    elif not test.somewhere(rel, data):
        rows.append(("plugin-put", FAIL, "succeeded, but no origin holds the object"))
    else:
        rows.append(("plugin-put", PASS, "stash_plugin read the --write share's token from the URL"))

    problems: list[str] = []
    for n, director_url in enumerate(test.fed.directors):
        rel = test.rel(f"share/curl-put-{n}")
        target = f"{director_url}{test.path(rel)}?authz={quote(token, safe='')}"
        _, answer = web.follow("PUT", target, upload=data)[-1]
        host = director.host(director_url)
        if not answer.ok:
            problems.append(f"{host}: the PUT, followed, got {answer.describe()}")
        elif not test.somewhere(rel, data):
            problems.append(f"{host}: the PUT succeeded, but no origin holds the object")
    rows.append(("curl-put", *outcome(problems, f"{len(test.fed.directors)} director(s)")))
    return rows


# --------------------------------------------------------------------------
# storage-scopes

# The clients that storage-scopes runs, as the transfers suite names
# them: `pelican`, stash_plugin, and a plain HTTP client, such as curl.
# The first two are Pelican's, which honor Client.EnableOverwrites.
SCOPE_CLIENTS = ("pelican", "plugin", "http")
PELICAN_CLIENTS = ("pelican", "plugin")
# The flags of `pelican token create` whose sets storage-scopes presents.
SCOPE_FLAGS = ("read", "write", "modify")
# The WLCG scope that each SciTokens scope stands for, as Pelican and
# XRootD read it (token_scopes.GrantedScopes in Pelican): write is
# storage.modify.
WLCG_EQUIVALENT = {"read": "storage.read", "write": "storage.modify"}
# What a storage-scopes case must do: succeed; be refused, changing
# nothing; or keep the object as it was, since it exists and overwrites
# are off.
SCOPE_PASS, SCOPE_REFUSED, SCOPE_KEPT = "pass", "refused", "kept"
# How many cases run at once.
SCOPE_CONCURRENCY = 8
# The size of storage-scopes' objects. An upload this small is sent whole
# before a refusal comes back. An XRootD origin refuses a larger one by
# closing the connection as the client sends it, which the clients report
# as a broken connection (Contact.ConnectionReset), not a refusal.
SCOPE_SIZE = 1000


@dataclass(frozen=True)
class ScopeOp:
    """An operation of storage-scopes.

    Attributes:
        name: Its rows' name, e.g. put-tree-over-overwrites.
        kind: "get", "direct-get", "put", or "delete".
        clients: The clients that run it.
        existing: Whether its object is there first.
        tree: Whether it puts a directory, recursively, rather than a file.
        overwrites: Whether the Pelican clients run with
            Client.EnableOverwrites.
    """

    name: str
    kind: str
    clients: tuple[str, ...]
    existing: bool = False
    tree: bool = False
    overwrites: bool = False

    @property
    def writes(self) -> bool:
        """Whether it writes."""
        return self.kind in ("put", "delete")


def _scope_puts() -> list[ScopeOp]:
    """Return storage-scopes' puts.

    Each puts a new object or one over an existing object, as a file or
    in a tree (recursively), with Client.EnableOverwrites off, as by
    default, or on. A plain HTTP client has no trees and no such setting:
    it runs put-new and put-over alone.
    """
    ops: list[ScopeOp] = []
    for tree, existing, overwrites in itertools.product((False, True), repeat=3):
        name = "put-" + ("tree-" if tree else "") + ("over" if existing else "new")
        name += "-overwrites" if overwrites else ""
        clients = PELICAN_CLIENTS if tree or overwrites else SCOPE_CLIENTS
        ops.append(ScopeOp(name, "put", clients, existing, tree, overwrites))
    return ops


SCOPE_OPS = (
    ScopeOp("get", "get", SCOPE_CLIENTS, existing=True),
    ScopeOp("direct-get", "direct-get", SCOPE_CLIENTS, existing=True),
    *_scope_puts(),
    ScopeOp("delete", "delete", ("pelican", "http"), existing=True),
)


def scope_sets(profile: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return each distinct set of storage scopes in profile.

    There are seven in WLCG's profile, of storage.read, storage.create,
    and storage.modify, and three in SciTokens', of read and write, which
    stands for the other two.

    Returns:
        Each set's name, its scopes without `storage.` joined by `+`
        (e.g. read+create), and flags of `pelican token create` that
        grant it.
    """
    found: dict[str, tuple[str, ...]] = {}
    names = credentials.SCOPES[profile]
    for n in range(1, len(SCOPE_FLAGS) + 1):
        for flags in itertools.combinations(SCOPE_FLAGS, n):
            scopes = dict.fromkeys(names[flag] for flag in flags)
            found.setdefault("+".join(s.removeprefix("storage.") for s in scopes), flags)
    return list(found.items())


def scope_grants(profile: str, flags: Sequence[str]) -> tuple[bool, bool, bool]:
    """Return whether a token of flags may read, create, and modify.

    Per the WLCG token profile, storage.modify implies storage.create.
    """
    held = {WLCG_EQUIVALENT.get(s, s) for s in (credentials.SCOPES[profile][f] for f in flags)}
    modify = "storage.modify" in held
    return "storage.read" in held, modify or "storage.create" in held, modify


def scope_expected(op: ScopeOp, client: str, grants: tuple[bool, bool, bool]) -> str:
    """Return what op by client must do with a token of grants.

    A get takes storage.read; a put of a new object, storage.create; a
    delete, or a put over an object, storage.modify. With overwrites off,
    a Pelican client must leave an existing object as it was, whatever
    the token (Client.EnableOverwrites; row P2 of Pelican's
    docs/object-transfer-semantics.md).
    """
    read, create, modify = grants
    if op.kind in ("get", "direct-get"):
        allowed = read
    elif op.kind == "delete":
        allowed = modify
    elif not op.existing:
        allowed = create
    elif client in PELICAN_CLIENTS and not op.overwrites:
        return SCOPE_KEPT
    else:
        allowed = modify
    return SCOPE_PASS if allowed else SCOPE_REFUSED


@dataclass(frozen=True)
class ScopeRun:
    """How a storage-scopes case's command went.

    Attributes:
        ok: Whether it succeeded.
        said: What it said: the client's output, the plugin's ads, or a
            plain HTTP client's last answer.
        refusal: How it reads as a refusal (transfers.refused()), or
            None if it doesn't.
        got: What a get brought, or None.
    """

    ok: bool
    said: str
    refusal: str | None
    got: bytes | None = None


def read_file(path: str) -> bytes | None:
    """Return what file path holds, or None if there is none."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def scope_run(
    test: Test, client: str, op: ScopeOp, slug: str, flags: tuple[str, ...], target: str, source: str | None
) -> ScopeRun:
    """Run op by client, with a token of flags, and return how it went.

    Args:
        test: The Test.
        client: One of SCOPE_CLIENTS.
        op: The operation.
        slug: The case's name, for its files.
        flags: The token's flags.
        target: The path of what op reads, writes, or deletes, in
            /protected-a: the object, or for a tree, its collection.
        source: What a put uploads: a file, or a directory for a tree.
    """
    url = test.pelican_url(target)
    data = read_file(source) if source and not op.tree else None
    if client == "http":
        method = {"get": "GET", "direct-get": "GET", "put": "PUT", "delete": "DELETE"}[op.kind]
        query = transfers.DIRECT_QUERY if op.kind == "direct-get" else ""
        hops = web.follow(
            method,
            f"{test.fed.director_url}{test.path(target)}{query}",
            test.session.token(NAMESPACE, flags),
            data,
        )
        last = hops[-1][1]
        ok = last.ok and (method != "GET" or last.status == 200)
        refusal = "server" if last.status in (401, 403) else None
        return ScopeRun(ok, last.describe(), refusal, last.body if ok and method == "GET" else None)
    env = dict(test.session.env if op.overwrites else test.session.sync_env)
    if client == "plugin":
        env["_CONDOR_CREDS"] = test.creds(slug, {"scitokens.use": test.session.token(NAMESPACE, flags)})
        query = {"direct-get": "directread", "put": "recursive" if op.tree else ""}.get(op.kind, "")
        plugin_url = f"{url}?{query}" if query else url
        if op.kind == "put":
            fetch = test.plugin(slug, [plugin_url], env, upload=True, sources=[source or ""])
        else:
            fetch = test.plugin(slug, [plugin_url], env)
        ok = fetch.status == 0 and transfers.plugin_passed(fetch.said)
        brought = (
            read_file(os.path.join(fetch.directory, transfers.local_name(url)))
            if op.kind != "put"
            else None
        )
        return ScopeRun(ok, fetch.said, transfers.refused(fetch.said), brought)
    args = ["object", "delete" if op.kind == "delete" else op.kind.removeprefix("direct-")]
    args += ["--token", test.session.token_file(NAMESPACE, flags)]
    if op.kind == "direct-get":
        args.append("--direct")
    if op.kind == "put":
        args += [*(["--recursive"] if op.tree else []), source or "", url]
    elif op.kind == "delete":
        args.append(url)
    else:
        args += [url, "."]
    fetch = test.pelican(slug, args, env)
    brought = (
        read_file(os.path.join(fetch.directory, target.rsplit("/", 1)[-1])) if op.kind != "put" else None
    )
    return ScopeRun(fetch.status == 0, fetch.said, transfers.refused(fetch.said), brought)


def scope_holdings(test: Test, rel: str) -> list[str | None]:
    """Return what each store holds at rel in /protected-a.

    Raises:
        stores.Unreadable: A pstore origin could not say.
    """
    return stores.held_anywhere(test.fed.origins, NAMESPACE, rel, {NAMESPACE: test.setup})


def scope_case_skip(test: Test, op: ScopeOp, client: str) -> str | None:
    """Return why op by client can test nothing in this shape, or None."""
    if op.writes and not test.writes:
        return f"/{NAMESPACE} takes no writes"
    if op.kind == "delete" and (why := stores.cannot(test.fed, "delete")):
        return why
    if op.kind == "direct-get" and not test.direct:
        return f"/{NAMESPACE} takes no direct reads"
    if client == "http" and not test.fed.director_url:
        return "this shape has no director for a plain HTTP client to ask"
    return None


def scope_case(
    test: Test, client: str, op: ScopeOp, label: str, flags: tuple[str, ...], read: tuple[str, bytes]
) -> CaseRow:
    """Run storage-scopes' case of op by client with scope set label.

    Args:
        test: The Test.
        client: One of SCOPE_CLIENTS.
        op: The operation.
        label: The set's name (scope_sets()).
        flags: Flags that grant it.
        read: The object that the gets read, under the run's collection,
            and its bytes.
    """
    case = f"{client} {op.name} {label}"
    why = scope_case_skip(test, op, client)
    if why:
        return case, SKIP, why
    expected = scope_expected(op, client, scope_grants(test.session.profile, flags))
    slug = f"scopes-{client}-{op.name}-{label.replace('+', '-')}"
    old = os.urandom(SCOPE_SIZE)
    new = os.urandom(SCOPE_SIZE + 1)
    source: str | None = None
    if op.kind in ("get", "direct-get"):
        name, old = read
        target = test.rel(name)
    else:
        target = test.rel("scopes", slug)
        name = f"scopes/{slug}/obj" if op.tree else f"scopes/{slug}"
        if op.existing:
            test.place(name, old)
        if op.kind == "put":
            source = test.local(f"scopes-src/{slug}/obj", new)
            if op.tree:
                source = os.path.dirname(source)
    rel = test.rel(name)
    ran = scope_run(test, client, op, slug, flags, target, source)

    problems: list[str] = []
    try:
        held = scope_holdings(test, rel) if op.writes else []
    except stores.Unreadable as e:
        return case, FAIL, f"could not check the stores: {e}"
    if expected == SCOPE_PASS:
        if not ran.ok:
            return case, FAIL, f"failed, but should have succeeded: {last_line(ran.said)}"
        if op.kind == "put" and common.digest(new) not in held:
            problems.append("succeeded, but no origin holds the new bytes")
        elif op.kind == "delete" and None not in held:
            problems.append("succeeded, but every origin still holds the object")
        elif not op.writes and ran.got != old:
            problems.append("succeeded, but did not bring the object")
        return case, *outcome(problems, "succeeded")
    kept = expected == SCOPE_KEPT
    exists = kept and "already exists" in ran.said
    if ran.ok and not (kept and op.tree):
        problems.append(
            "succeeded, though the object exists and overwrites are off"
            if kept
            else "succeeded, but should have been refused"
        )
    elif not ran.ok and ran.refusal is None and not exists:
        why = "by refusal or because the object exists" if kept else "by refusal"
        problems.append(f"failed, but not {why}: {last_line(ran.said)}")
    if not op.writes and ran.got == old:
        problems.append("brought the object, though refused")
    elif op.existing and op.writes and any(h != common.digest(old) for h in held):
        problems.append("an origin no longer holds the object as it was")
    elif not op.existing and any(h is not None for h in held):
        problems.append("an origin holds the object")
    if ran.ok:
        note = "succeeded, leaving the object as it was"
    elif exists:
        note = "refused: it already exists"
    else:
        note = f"refused ({ran.refusal})"
    return case, *outcome(problems, note)


def storage_scopes(test: Test) -> list[CaseRow]:
    """Check what each set of storage scopes lets each client do.

    Every distinct set of the pass's profile (scope_sets()) is presented
    by `pelican` (with --token), stash_plugin (in its credentials
    directory), and a plain HTTP client (which asks the director for the
    bare path and follows its redirect, as curl does), to each of
    SCOPE_OPS in /protected-a: a get, by the director's choice of server
    and from an origin; a put of a new object, or over one, as a file or
    in a tree; and a delete. Each case must do what scope_expected()
    says, and change nothing it may not. Each runs on objects of its own,
    SCOPE_CONCURRENCY at a time.
    """
    read = ("scopes/obj", os.urandom(SCOPE_SIZE))
    test.place(*read)
    jobs = [
        (client, op, label, flags)
        for label, flags in scope_sets(test.session.profile)
        for op in SCOPE_OPS
        for client in op.clients
    ]

    def run_case(job: tuple[str, ScopeOp, str, tuple[str, ...]]) -> CaseRow:
        return scope_case(test, *job, read)

    with common.pool(SCOPE_CONCURRENCY) as pool:
        return list(pool.map(run_case, jobs))


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
    test.place(name, data)
    env = test.creds_env(test.server_creds("decision"), PELICAN_PLUGIN_DIRECTORDECISIONPERCENTAGE="100")
    fetch = test.plugin("decision", [test.pelican_url(test.rel(name))], env)
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


def skipstat_failover(test: Test) -> list[CaseRow]:
    """Check that a direct ?skipstat read finds the one origin holding it.

    With several origins, an object put in the last origin's store
    alone: `pelican object get --direct` of it with ?skipstat, which
    keeps the director from asking the origins which holds it, must
    still bring it, since the client tries the origins the director
    lists until one serves (getObjectServersToTry in
    client/handle_http.go). The note says whether the client's stats
    show a failed try at an origin that lacks it, or the client's probes
    put the holder first. stash_plugin's read of it with
    ?directread&skipstat must likewise bring it, served by the holder
    (`plugin`).
    """
    origins = stores.unique(test.fed.origins)
    if len(origins) < 2:
        return [("", SKIP, "one origin, or origins sharing a store ('-p topo-multi-origin')")]
    if not test.direct:
        return [("", SKIP, "/protected-a takes no direct clients")]
    holder = origins[-1]
    name = "skipstat"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data, origins=[holder])
    return [
        ("", *skipstat_get(test, name, data, holder)),
        ("plugin", *skipstat_plugin(test, name, data, holder)),
    ]


def skipstat_get(test: Test, name: str, data: bytes, holder: common.Origin) -> tuple[str, str]:
    """Return skipstat_failover()'s row for `pelican object get`."""
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
                test.pelican_url(test.rel(name), "skipstat"),
                "object",
            ],
        )
        why = got(fetch, data)
        if why:
            return FAIL, why
        results = transfers.stats_results(transfers.read_stats(os.path.join(fetch.directory, "stats.json")))
        attempts = transfers.attempts_of(results[0]) if results else []
        if attempts and transfers.endpoint_host(attempts[0]) != holder.svc and attempt_failed(attempts[0]):
            tried_first = True
            break
    if tried_first:
        return PASS, f"an origin without it was tried first, then {holder.svc} served"
    return PASS, f"{holder.svc} served at the first try each time ({SKIPSTAT_TRIES} reads)"


def skipstat_plugin(test: Test, name: str, data: bytes, holder: common.Origin) -> tuple[str, str]:
    """Return skipstat_failover()'s row for stash_plugin."""
    url = test.pelican_url(test.rel(name), "directread&skipstat")
    env = test.creds_env(test.server_creds("skipstat-plugin"))
    for n in range(SKIPSTAT_TRIES):
        fetch = test.plugin(f"skipstat-plugin-{n}", [url], env)
        why = got(fetch, data, name)
        if why:
            return FAIL, why
        served = transfers.plugin_endpoints(fetch.said)
        if served != [holder.svc]:
            return FAIL, f"served by {served or 'no host recorded'}, not {holder.svc}"
    return PASS, f"{holder.svc} served each of {SKIPSTAT_TRIES} reads"


# --------------------------------------------------------------------------
# multi-source


def multi_source(test: Test) -> list[CaseRow]:
    """Check `get` and `put` with several sources, and `?recursive`.

    These are rows G8-G10 and P8-P10 of Pelican's
    docs/object-transfer-semantics.md (cmd/object_get.go and
    object_put.go), and `?recursive` in place of -r (P6).
    """
    objects = {name: os.urandom(OBJECT_SIZE) for name in ("multi-a", "multi-b")}
    for name, data in objects.items():
        test.place(name, data)
    urls = [test.pelican_url(test.rel(name)) for name in objects]
    rows: list[CaseRow] = []
    directory = test.directory("get-g8")
    fetch = test.pelican(
        "get-g8", ["object", "get", "--token", test.token_file, *urls, directory], cwd=directory
    )
    problems = exit_problems(fetch)
    for name, data in objects.items():
        if not common.file_holds(os.path.join(directory, name), data):
            problems.append(f"{name} is missing or wrong in the directory")
    rows.append(("get-directory", *outcome(problems, "each landed by its name (G8)")))
    for case, target, message in (
        ("get-missing-dir", os.path.join(test.tmp, "nowhere"), "Destination does not exist"),
        ("get-file", test.local("a-file", b"x"), "Destination is not a directory"),
    ):
        fetch = test.pelican(case, ["object", "get", "--token", test.token_file, *urls, target])
        if fetch.status == 0:
            rows.append((case, FAIL, "succeeded"))
        elif message not in fetch.said:
            rows.append((case, FAIL, f"failed, but not saying {message!r}: {last_line(fetch.said)}"))
        else:
            rows.append((case, PASS, message))
    if not test.writes:
        rows.append(("put", SKIP, "/protected-a takes no writes"))
        return rows
    files = {n: os.urandom(1000 + 10 * i) for i, n in enumerate(("up-a", "up-b"))}
    sources = [test.local(f"multi/{n}", d) for n, d in files.items()]
    for case, collection, make in (
        ("put-collection", "multi-into", True),
        ("put-new", "multi-new", False),
    ):
        rel = test.rel(collection)
        if make:
            why = test.make(rel)
            if why:
                rows.append((case, FAIL, why))
                continue
        fetch = test.pelican(
            case,
            ["object", "put", "--token", test.token_file, *sources, test.pelican_url(test.rel(collection))],
        )
        problems = exit_problems(fetch)
        for name, data in files.items():
            if not test.somewhere(f"{rel}/{name}", data):
                problems.append(f"no origin holds {collection}/{name}")
        rows.append((case, *outcome(problems, "each landed under the collection by its name")))
    old = os.urandom(500)
    test.place("multi-object", old)
    fetch = test.pelican(
        "put-object",
        ["object", "put", "--token", test.token_file, *sources, test.pelican_url(test.rel("multi-object"))],
        env=test.session.sync_env,
    )
    problems = list[str]()
    if fetch.status == 0:
        problems.append("succeeded, though the destination is an object")
    elif "already exists" not in fetch.said:
        problems.append(f"failed, but not because the object exists: {last_line(fetch.said)}")
    if not test.somewhere(test.rel("multi-object"), old):
        problems.append("the object was changed")
    rows.append(("put-object", *outcome(problems, "refused: it already exists (P10)")))
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
            test.pelican_url(test.rel("multi-recursive"), "recursive"),
        ],
    )
    problems = exit_problems(fetch)
    for path, data in tree.items():
        if not test.somewhere(f"{test.rel('multi-recursive')}/{path}", data):
            problems.append(f"no origin holds multi-recursive/{path}")
    rows.append(("put-recursive-query", *outcome(problems, "the tree landed flat (P6, ?recursive)")))
    # stash_plugin, which HTCondor hands a URL with ?recursive for a tree,
    # uploads it to a collection of its own.
    plugin_env = test.creds_env(test.server_creds("multi-plugin"))
    fetch = test.plugin(
        "plugin-put-recursive-query",
        [test.pelican_url(test.rel("plugin-recursive"), "recursive")],
        plugin_env,
        upload=True,
        sources=[os.path.join(test.tmp, "recursive")],
    )
    problems = exit_problems(fetch)
    for path, data in tree.items():
        if not test.somewhere(f"{test.rel('plugin-recursive')}/{path}", data):
            problems.append(f"no origin holds plugin-recursive/{path}")
    rows.append(
        (
            "plugin-put-recursive-query",
            *outcome(problems, "the tree landed flat (?recursive, by the plugin)"),
        )
    )
    if "Listings" not in test.caps:
        why = "/protected-a has no Listings"
        rows += [("get-recursive-query", SKIP, why), ("plugin-get-recursive-query", SKIP, why)]
        return rows
    directory = test.directory("get-recursive-query")
    fetch = test.pelican(
        "get-recursive-query",
        [
            "object",
            "get",
            "--token",
            test.token_file,
            test.pelican_url(test.rel("multi-recursive"), "recursive"),
            directory,
        ],
        cwd=directory,
    )
    problems = exit_problems(fetch)
    for path, data in tree.items():
        if not common.file_holds(os.path.join(directory, path), data):
            problems.append(f"{path} is missing or wrong")
    rows.append(("get-recursive-query", *outcome(problems, "the tree came back (G5, ?recursive)")))
    # The plugin's download of the tree, to its directory (`.`): the
    # plugin lands a download whose destination is a directory under the
    # object's name in it (parseDestination in cmd/plugin.go), a path that
    # is new, under which the tree's entries land (G6).
    fetch = test.plugin(
        "plugin-get-recursive-query",
        [test.pelican_url(test.rel("multi-recursive"), "recursive")],
        plugin_env,
        sources=["."],
    )
    problems = exit_problems(fetch)
    landed = os.path.join(fetch.directory, "multi-recursive")
    if not all(common.file_holds(os.path.join(landed, path), data) for path, data in tree.items()):
        problems.append(
            "the tree is missing or wrong under multi-recursive/; the directory holds" + f" {fetch.files()}"
        )
    note = "the tree came back under the collection's name (G6, ?recursive)"
    rows.append(("plugin-get-recursive-query", *outcome(problems, note)))
    return rows


# --------------------------------------------------------------------------
# preferred-cache-down


def preferred_cache_down(test: Test) -> list[CaseRow]:
    """Check a preferred cache that is down, with `+` and without.

    `pelican object get --cache https://<cache>:1,+`: a preferred
    cache that answers nothing, and then the director's caches, which
    `+` adds, one of which must serve; without `+`, the client has only
    the dead cache, and must fail to reach it, by something other than a
    refusal (getPreferredCaches in client/). stash_plugin, given the
    same in Client.PreferredCaches, must do the same (`plugin-plus`,
    `plugin-alone`).
    """
    caches = test.fed.federated_caches
    if not caches:
        return [("", SKIP, "this shape has no cache")]
    name = "preferred"
    data = os.urandom(OBJECT_SIZE)
    test.place(name, data)
    dead = f"https://{director.host(caches[0].url)}:1"
    rows: list[CaseRow] = []
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
            test.pelican_url(test.rel(name)),
            "object",
        ],
    )
    why = got(fetch, data)
    if why:
        rows.append(("plus", FAIL, why))
    else:
        served = transfers.stats_endpoints(
            transfers.read_stats(os.path.join(fetch.directory, "stats.json"))
        )
        if not served or served[0] not in {c.svc for c in caches}:
            rows.append(("plus", FAIL, f"served by {served or 'no host recorded'}, not a cache"))
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
            test.pelican_url(test.rel(name)),
            "object",
        ],
    )
    if fetch.status == 0:
        rows.append(("alone", FAIL, "succeeded, though its only cache is down"))
    elif transfers.refused(fetch.said):
        rows.append(("alone", FAIL, f"failed by refusal, not by contact: {last_line(fetch.said)}"))
    else:
        rows.append(("alone", PASS, f"failed to reach the cache: {last_line(fetch.said)[:80]}"))
    url = test.pelican_url(test.rel(name))
    creds = test.server_creds("preferred-plugin")
    fetch = test.plugin(
        "preferred-plugin-plus", [url], test.creds_env(creds, PELICAN_CLIENT_PREFERREDCACHES=f"{dead},+")
    )
    why = got(fetch, data, name)
    if why:
        rows.append(("plugin-plus", FAIL, why))
    else:
        served = transfers.plugin_endpoints(fetch.said)
        if not served or served[-1] not in {c.svc for c in caches}:
            rows.append(("plugin-plus", FAIL, f"served by {served or 'no host recorded'}, not a cache"))
        else:
            rows.append(("plugin-plus", PASS, f"served by {served[-1]}"))
    fetch = test.plugin(
        "preferred-plugin-alone", [url], test.creds_env(creds, PELICAN_CLIENT_PREFERREDCACHES=dead)
    )
    if fetch.status == 0 or transfers.plugin_passed(fetch.said):
        rows.append(("plugin-alone", FAIL, "succeeded, though its only cache is down"))
    elif transfers.refused(fetch.said):
        rows.append(("plugin-alone", FAIL, f"failed by refusal, not by contact: {last_line(fetch.said)}"))
    elif os.path.exists(os.path.join(fetch.directory, name)):
        rows.append(("plugin-alone", FAIL, "failed, but left a file of the object's name"))
    else:
        rows.append(("plugin-alone", PASS, f"failed to reach the cache: {last_line(fetch.said)[:80]}"))
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


def job_status(pelican: Callable[..., tuple[int, str, str]], job: str) -> tuple[dict[str, Any] | None, str]:
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


def async_job(test: Test) -> list[CaseRow]:
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
    test.place(name, data)
    rows: list[CaseRow] = []

    def pelican(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
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
            test.pelican_url(test.rel(name)),
            target,
            cwd=directory,
        )
        job = job_id(out)
        if code != 0:
            rows.append(("get", FAIL, f"exit {code}: {last_line(err or out)}"))
        elif not job:
            rows.append(("get", FAIL, f"no job ID printed: {last_line(out)}"))
        elif not common.file_holds(target, data):
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
                rows.append(("status", *outcome(problems, f"{COMPLETED}, {len(data)} bytes")))

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
                ids = [str(j.get("job_id")) for j in common.objects(listed.get("jobs"))]
                if not job:
                    rows.append(("list", SKIP, "no job to look for"))
                elif job not in ids:
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
                test.pelican_url(test.rel("async-copy")),
                cwd=os.path.dirname(local),
            )
            held = test.somewhere(rel, upload)
            if code != 0:
                rows.append(("copy", FAIL, f"exit {code}: {last_line(err or out)}"))
            elif not held:
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
Rows = suite.Row | list[CaseRow]

RUN: dict[str, Callable[[Test], Rows]] = {
    "plugin-ads": plugin_ads,
    "failover": failover,
    "etag-agreement": etag_agreement,
    "token-discovery": token_discovery,
    "url-forms": url_forms,
    "token-create-discovery": token_create_discovery,
    "share-url": share_url,
    "storage-scopes": storage_scopes,
    "director-decision": director_decision,
    "skipstat-failover": skipstat_failover,
    "multi-source": multi_source,
    "preferred-cache-down": preferred_cache_down,
    "async-job": async_job,
}


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    test = Test(session)
    test.clean("before the run", fatal=True)
    print(f"Client contracts against {fed.url}, objects under /{NAMESPACE}/{test.base}/\n")
    try:
        for name in selected:
            suite.record(results, name, suite.attempt_cases(partial(RUN[name], test)))
    finally:
        test.clean("after the run")
        shutil.rmtree(test.tmp, ignore_errors=True)
