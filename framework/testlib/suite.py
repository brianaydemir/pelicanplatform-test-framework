"""What a suite's Test class builds on.

A suite's objects go in its area, data/<area>/ in each namespace, which
`./fed.sh init` makes (init-data.py's SUITE_DIRS); each pass's go in
its own collection there, data/<area>/<run> (Test.base).
"""

import os
from collections.abc import Callable, Iterable, Mapping
from functools import cached_property
from urllib.parse import quote

from . import blocks, common, control, digests, director, report, stores, web
from .session import Session

# How long, after a restart, every director may take to name the origins,
# and every cache to reach them.
SETTLE_WAIT = 150
# How long a V2 cache may take to say, with Age, that it holds an object
# it has served whole.
HELD_WAIT = 30

# The namespace of a suite's objects, unless its Test names another.
NAMESPACE = "protected-a"

# A row's status and note.
Row = tuple[str, str]
# A row's case (appended to its scenario's name, if any), status, and
# note.
CaseRow = tuple[str, str, str]


class SetupError(Exception):
    """A scenario could not set itself up; the message is its row's note."""


def unexported(fed: common.Federation, namespace: str = NAMESPACE) -> str | None:
    """Return why the shape has no namespace to test, or None if it does."""
    if namespace in fed.exports:
        return None
    return f"this shape does not export /{namespace}"


def attempt(check: Callable[[], Row], expected: tuple[type[Exception], ...] = ()) -> Row:
    """Return check()'s row, or a FAIL if it raised (common.guarded()).

    Args:
        check: What returns the row.
        expected: The exceptions, beside SetupError, by which check()
            reports a problem.
    """
    return common.guarded(check, lambda note: (report.FAIL, note), (SetupError, *expected))


def attempt_all(
    rows: Iterable[str],
    check: Callable[[], dict[str, Row]],
    expected: tuple[type[Exception], ...] = (),
) -> dict[str, Row]:
    """Return check()'s rows, by name, each a FAIL if check() raised.

    Args:
        rows: The names of the rows that check() returns.
        check: What returns the rows.
        expected: As for attempt().
    """
    return common.guarded(
        check, lambda note: {row: (report.FAIL, note) for row in rows}, (SetupError, *expected)
    )


def attempt_cases[T](
    check: Callable[[], T], expected: tuple[type[Exception], ...] = ()
) -> T | list[CaseRow]:
    """Return check()'s rows, or a FAIL with no case if it raised.

    Args:
        check: What returns the rows (or, for a scenario that may
            return either, its row).
        expected: As for attempt().
    """
    return common.guarded(check, lambda note: [("", report.FAIL, note)], (SetupError, *expected))


def record(results: common.Results, scenario: str, found: Row | list[CaseRow] | Mapping[str, Row]) -> None:
    """Record what a scenario found: its row, or its cases' rows.

    A case's row is named `<scenario> <case>`, or `<scenario>` if its
    case is "".

    Args:
        results: The suite's results.
        scenario: The scenario's name.
        found: Its row; its cases' rows; or its rows, by case.
    """
    if isinstance(found, tuple):
        rows = [("", *found)]
    elif isinstance(found, list):
        rows = found
    else:
        rows = [(case, *row) for case, row in found.items()]
    for case, status, note in rows:
        results.add(f"{scenario} {case}".strip(), status, note)


def check_restarted[T](
    files_by_svc: Mapping[str, Mapping[str, str]],
    servers: str,
    checks: Mapping[str, Callable[[], T]],
    add: Callable[[str, T | Row], None],
    *,
    ready: Callable[[], str | None] | None,
    restored: Callable[[], str | None] | None,
    expected: tuple[type[Exception], ...] = (),
) -> set[str]:
    """Run checks with servers restarted, and then restore them.

    Each server is restarted with its files as the last of its
    configuration layers (control.restart_all_with()). Then each check
    runs, in order, and its result is recorded, a FAIL if it raised (as
    attempt()); but if ready() finds something wrong, each fails with
    that instead. If restarting, or restoring, fails, what is left
    unrecorded fails. Afterward, restored() may wait until the
    federation is as before; what it finds is only warned of.

    Args:
        files_by_svc: Each server's files, by service: YAML text, by
            file name.
        servers: What the servers are, for the notes ("the origins").
        checks: What returns each check's result, by name.
        add: Takes each check's name, and its result or row.
        ready: Waits until the restarted servers can be checked, and
            returns why not, or None; None to not wait.
        restored: Likewise, once they are restored; None to not wait.
        expected: As for attempt().

    Returns:
        The names of the checks recorded while the servers were
        restarted.
    """
    done: set[str] = set()
    try:
        with control.restart_all_with(files_by_svc):
            why = ready() if ready else None
            for name, check in checks.items():
                if why:
                    add(name, (report.FAIL, f"with {servers} restarted, {why}"))
                else:
                    add(
                        name,
                        common.guarded(check, lambda note: (report.FAIL, note), (SetupError, *expected)),
                    )
                done.add(name)
    except control.ControlError as e:
        for name in checks:
            if name not in done:
                add(name, (report.FAIL, f"restarting {servers}: {e}"))
        common.warn(f"restarting or restoring {servers}: {e}")
    why = restored() if restored else None
    if why:
        common.warn(f"with {servers} restored, {why}")
    return done


def attempt_restarted(
    files_by_svc: Mapping[str, Mapping[str, str]],
    servers: str,
    check: Callable[[], Row],
    *,
    restored: Callable[[], str | None] | None,
    expected: tuple[type[Exception], ...] = (),
) -> Row:
    """Return check()'s row, with servers restarted (check_restarted()).

    Args:
        files_by_svc: As for check_restarted().
        servers: Likewise.
        check: What returns the row.
        restored: As for check_restarted().
        expected: As for attempt().
    """
    rows: dict[str, Row] = {}
    check_restarted(
        files_by_svc,
        servers,
        {"": check},
        rows.__setitem__,
        ready=None,
        restored=restored,
        expected=expected,
    )
    return rows[""]


def clean(
    session: Session,
    area: str,
    when: str,
    origins: Iterable[common.Origin] | None = None,
    fatal: bool = False,
) -> None:
    """Empty data/<area> in every namespace at every origin.

    Args:
        session: The pass, whose setup tokens a pstore origin needs.
        area: The suite's area.
        when: When this is, for the message, e.g. "before the run".
        origins: The origins, if not every one.
        fatal: Whether a failure ends the suite, not just warns.
    """
    fed = session.fed
    rel = f"data/{area}"
    origins = fed.origins if origins is None else list(origins)
    tokens = session.setup_tokens() if any(o.pstore for o in origins) else {}
    errors = stores.empty_tree_everywhere(origins, fed.exports, rel, tokens)
    if errors:
        message = f"emptying {rel} {when}: {common.first(errors)}"
        if fatal:
            common.die(message)
        else:
            common.warn(message)


def leaf(rel: str) -> str:
    """Return rel's last segment, for a message."""
    return rel.rsplit("/", 1)[-1]


class Test:
    """The pass, and the run's collection in one namespace.

    Attributes:
        session: The pass.
        fed: The federation.
        run: The pass's ID (Session.run).
        area: The suite's area.
        base: The run's collection: data/<area>/<run>.
        namespace: The namespace of the suite's objects.
        made: The collections that make() has made at every origin.
    """

    _runs = 0

    def __init__(self, session: Session, area: str, namespace: str = NAMESPACE):
        self.session = session
        self.fed = session.fed
        self.run = session.run
        self.area = area
        self.base = f"data/{area}/{session.run}"
        self.namespace = namespace
        self.made: set[str] = set()
        if namespace in self.fed.exports:
            # Minting asks the federation: mint now, not mid-scenario.
            session.token_file(namespace)
            session.setup_token_file(namespace)

    @cached_property
    def tmp(self) -> str:
        """The run's scratch directory, for the clients' files."""
        path = self.session.path(self.area)
        os.makedirs(path, exist_ok=True)
        return path

    @cached_property
    def token(self) -> str:
        """A `server` token for the namespace, of the session's profile."""
        return self.session.token(self.namespace)

    @cached_property
    def token_file(self) -> str:
        """The file of token."""
        return self.session.token_file(self.namespace)

    @cached_property
    def setup(self) -> str:
        """A `server` token for the namespace, of the WLCG profile.

        It is for putting objects in place and checking the shape, not
        for what a scenario presents.
        """
        return self.session.setup_token(self.namespace)

    @property
    def caps(self) -> frozenset[str]:
        """The namespace's capabilities."""
        return self.fed.exports[self.namespace]

    @property
    def direct(self) -> bool:
        """Whether the namespace takes direct clients."""
        return "DirectReads" in self.caps

    @property
    def writes(self) -> bool:
        """Whether the namespace takes writes."""
        return "Writes" in self.caps

    def directory(self, name: str) -> str:
        """Return a new directory in tmp, for one client run."""
        self._runs += 1
        path = os.path.join(self.tmp, f"{self._runs:03d}-{name}")
        os.makedirs(path)
        return path

    def local(self, name: str, data: bytes) -> str:
        """Write data to file name in tmp, and return its path."""
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def rel(self, *parts: str) -> str:
        """Return the path of parts, within the run's collection."""
        return "/".join([self.base, *parts])

    def path(self, rel: str) -> str:
        """Return rel's path in the federation."""
        return f"/{self.namespace}/{rel}"

    def url(self, server_url: str, rel: str) -> str:
        """Return rel's URL at the server at server_url."""
        return f"{server_url}/{self.namespace}/{quote(rel)}"

    def pelican_url(self, rel: str, query: str = "", namespace: str | None = None) -> str:
        """Return rel's pelican:// URL, unencoded, with query if any.

        rel is in namespace, if given, not the Test's.
        """
        url = f"{self.fed.url}/{namespace or self.namespace}/{rel}"
        return f"{url}?{query}" if query else url

    def make(self, rel: str) -> str | None:
        """Make collection rel, and its parents, at every origin, with setup.

        Returns:
            What went wrong, or None.
        """
        errors = stores.make_dirs(self.fed.origins, self.namespace, rel, self.setup)
        if errors:
            return errors[0]
        self.made.add(rel)
        return None

    def put(self, objects: Mapping[str, bytes]) -> str | None:
        """Put objects, by rel, and their collections, in every store.

        Returns:
            What went wrong, or None.
        """
        origins = stores.unique(self.fed.origins)
        for rel, data in objects.items():
            parent = rel.rsplit("/", 1)[0]
            if parent not in self.made:
                why = self.make(parent)
                if why:
                    return why
            errors = stores.seed_everywhere(origins, self.namespace, rel, data, self.setup, self.caps)
            if errors:
                return errors[0]
        return None

    def ask(
        self,
        method: str,
        server_url: str,
        rel: str,
        headers: Mapping[str, str] | None = None,
        timeout: float = 120,
    ) -> web.Response:
        """Send method for rel to the server at server_url, with token."""
        return web.request(
            method, self.url(server_url, rel), token=self.token, headers=headers, timeout=timeout
        )

    def which_version(
        self,
        cache: common.Cache,
        rel: str,
        versions: Mapping[str, bytes],
        request: blocks.Request | None,
    ) -> tuple[str | None, str]:
        """Return which of versions a read of rel through cache came back as.

        The read is of rel whole, or of request's ranges (see
        blocks.which_version()). A whole read asks for a digest too
        (Want-Digest), which, if one comes, must be that version's: a stale
        one would fail the client's check of bytes that are right.

        Args:
            cache: The cache to read through.
            rel: The object.
            versions: Each version's bytes, by name.
            request: The ranges to read, or None for the whole object.

        Returns:
            The version's name and ""; or None, and why it is none.
        """
        headers = {"Range": blocks.range_header(request)} if request else {"Want-Digest": "crc32c"}
        answer = self.ask("GET", cache.url, rel, headers)
        version, why = blocks.which_version(dict(versions), request, answer)
        if version and request is None:
            wrong, _ = digests.check(answer.header("Digest"), versions[version])
            if wrong:
                return None, f"served {version}, but with a Digest that is not its ({common.first(wrong)})"
        return version, why

    def age(self, cache: common.Cache, rel: str) -> int | None:
        """Return the Age with which a V2 cache says it holds all of rel.

        It is asked with setup. A cache's Age starts at 0 once it holds
        the whole object.

        Returns:
            The Age, or None if it gives none, or does not answer 200.
        """
        answer = web.request("HEAD", self.url(cache.url, rel), token=self.setup)
        return web.age(answer) if answer.status == 200 else None

    def held(self, cache: common.Cache, rel: str) -> bool:
        """Return whether a V2 cache says it holds all of rel (see age())."""
        return self.age(cache, rel) is not None

    def wait_held(self, cache: common.Cache, rel: str, seconds: float = HELD_WAIT) -> bool:
        """Return whether a V2 cache says it holds all of rel within seconds."""
        return common.wait_for(lambda: self.held(cache, rel), seconds, interval=1)

    def cached(self, cache: common.Cache, rel: str) -> bool | None:
        """Return whether a V2 cache holds all of rel, by only-if-cached.

        A HEAD with only-if-cached, which fetches nothing, gets 200 if it
        does, and 504 if not. It is asked with setup.

        Returns:
            Whether it does, or None if the HEAD got neither.
        """
        answer = web.request("HEAD", self.url(cache.url, rel), token=self.setup, headers=web.ONLY_IF_CACHED)
        if answer.status == 200:
            return True
        if answer.status == 504:
            return False
        return None

    def wait_cached(self, cache: common.Cache, rel: str, seconds: float = HELD_WAIT) -> bool:
        """Return whether a V2 cache holds all of rel within seconds (cached())."""
        return common.wait_for(lambda: self.cached(cache, rel) is True, seconds, interval=1)

    def fill(self, cache: common.Cache, rel: str, data: bytes) -> str | None:
        """Read rel whole through cache, which must serve data.

        A V2 cache must then say it holds it all (wait_held()). The read
        is with setup.

        Returns:
            What went wrong, or None.
        """
        name = leaf(rel)
        answer = web.request("GET", self.url(cache.url, rel), token=self.setup, timeout=120)
        why = web.served(answer, data)
        if why:
            return f"{cache.svc}: a GET of {name} got {why}"
        if cache.kind == "v2" and not self.wait_held(cache, rel):
            return f"{cache.svc} does not hold {name} {HELD_WAIT}s after reading it whole"
        return None

    def serving(self, probe: str, size: int | None = None) -> str | None:
        """Return why the federation does not serve probe, or None.

        Every origin must answer a HEAD of it, if the namespace takes
        direct clients; every director must name every origin for it; and
        every federated cache must answer a HEAD of it, of size bytes if
        given. Each is asked with setup: this checks the shape, not the
        pass's tokens.

        Args:
            probe: The rel of an object at every origin.
            size: Its size.
        """
        for origin in self.fed.origins if self.direct else []:
            answer = web.request("HEAD", stores.url(origin, self.namespace, probe), token=self.setup)
            if answer.status != 200:
                return f"{origin.svc}: HEAD of the probe got {answer.describe()}"
        for url in self.fed.directors:
            where = director.ask(url, "origin", self.path(probe), token=self.setup)
            for origin in self.fed.origins:
                if not where.names(director.host(origin.url)):
                    return f"{director.host(url)} does not name {origin.svc}: {where.describe()}"
        for cache in self.fed.federated_caches:
            answer = web.request("HEAD", self.url(cache.url, probe), token=self.setup)
            if answer.status != 200 or (size is not None and answer.header("Content-Length") != str(size)):
                return f"{cache.svc}: HEAD of the probe got {answer.describe()}"
        return None

    def settle(self, probe: str, size: int | None = None, seconds: float = SETTLE_WAIT) -> str | None:
        """Wait until the federation serves probe (see serving()).

        A server that exits on its own ends the wait (control.settle()).

        Returns:
            Why it doesn't within seconds, or None.
        """
        return control.settle(lambda: self.serving(probe, size), seconds)

    def clean(self, when: str, fatal: bool = False) -> None:
        """Empty the area at every origin (see clean())."""
        clean(self.session, self.area, when, fatal=fatal)
