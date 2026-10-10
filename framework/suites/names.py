"""Objects whose names a URL or a listing must encode: a space, `%`,
`+`, `#`, `?`, `&`, `;`, quotes, angle brackets, brackets, `*`, and
letters beyond ASCII. Each sits beside decoys, objects whose names a
server or client that mishandles the encoding would take for its own:
`x%41y` for `xAy`, `a+b` and `a%20b` for `a b`, `hash#tag` for `hash`,
and `what?q=1` for `what`. Any bytes but the object's own fail, and
another's are named.

Objects go under /protected-a/data/names/<run>/, put at every origin
with stores.seed(). A URL names each with every character but letters,
digits, and `-._~` percent-encoded (stores.url()); the client is given
it so too. A listing's hrefs are decoded before they are compared.

  origin-put   a PUT of each name lands in each store on disk as a file
               of that name (where /protected-a takes writes)
  origin-get   each origin serves each object by its name
  cache-get    each cache does, cold
  origin-list  each origin lists every name, once, with its size
  cache-list   each federated cache does, as it relays the listing
  client-get   `pelican object get` of every object into a directory
               writes a file of each one's name, with its bytes
  client-put   `pelican object put` of a file of each name into a
               collection lands as the object of that name
  client-ls    `pelican object ls` lists every name
"""

import os
import traceback
from typing import Optional
from urllib.parse import quote

from suites.commands import json_objects
from testlib import common, stores, web, webdav
from testlib.common import die, first
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "origin-put": "a PUT of each name lands in each store on disk under that name",
    "origin-get": "each origin serves each object by its name, and no decoy",
    "cache-get": "each cache serves each object by its name, cold, and no decoy",
    "origin-list": "each origin lists every name once, decoded, with its size",
    "cache-list": "each federated cache lists every name once, decoded, with its size",
    "client-get": "`pelican object get` writes a file of each object's name, with its bytes",
    "client-put": "`pelican object put` of a file of each name lands as that object",
    "client-ls": "`pelican object ls` lists every name",
}

NAMES: tuple[str, ...] = (
    "plain",
    "a b",
    "a+b",
    "a%20b",
    "xAy",
    "x%41y",
    "hash",
    "hash#tag",
    "what",
    "what?q=1",
    "semi;colon",
    "and&eq=1",
    'it\'s "quoted"',
    "<angle>&amp;",
    "[brackets]",
    "star*",
    "café-日本",
    "~tilde",
)


class Test:
    """The objects, by name, where they are, and the token."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.origins = stores.unique(self.fed.origins)
        self.base = f"data/names/{session.run}"
        # Where client-put uploads, beside the objects.
        self.put_base = f"{self.base}-put"
        self.tmp = session.path("names")
        os.makedirs(self.tmp)
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        self.caps = self.fed.exports["protected-a"]
        # Each a size of its own, so that a listing shows which is which.
        self.objects = {name: os.urandom(1000 + 37 * n) for n, name in enumerate(NAMES)}
        # What went wrong putting them in place.
        self.unseeded: list[str] = []

    def rel(self, name: str) -> str:
        """Where object name is in /protected-a."""
        return f"{self.base}/{name}"

    def url(self, server: str, name: str) -> str:
        """The URL of object name at a server's data URL."""
        return f"{server}/protected-a/{quote(self.rel(name))}"

    def which(self, data: bytes) -> str:
        """The name of the object whose bytes data are, or ""."""
        return next((name for name, held in self.objects.items() if held == data), "")

    def wrong(self, name: str, data: bytes) -> Optional[str]:
        """Why data, as served for object name, are not its bytes."""
        if data == self.objects[name]:
            return None
        other = self.which(data)
        if other:
            return f"{name!r}: served {other!r}'s bytes"
        return f"{name!r}: {len(data)} bytes that are no object's"

    def seed(self) -> None:
        """Put every object at every origin. What an origin refuses is
        left out, and recorded in unseeded: the scenarios then fail for
        it."""
        errors = stores.make_dirs(self.origins, "protected-a", self.base, self.token)
        if errors:
            die(f"could not make the run's collection: {first(errors)}")
        for name, data in self.objects.items():
            self.unseeded += stores.seed_everywhere(
                self.origins, "protected-a", self.rel(name), data, self.token, self.caps
            )


def origin_put(test: Test) -> tuple[str, str]:
    """A PUT of each name (made as the test began) lands in each store on
    disk as a file of that name, with its bytes, and nothing else."""
    if "Writes" not in test.caps:
        return SKIP, "/protected-a takes no writes, so each name was written in each store"
    disk = [o for o in test.origins if not o.pstore]
    problems = list(test.unseeded)
    if not disk and not problems:
        return SKIP, "no store on disk: a pstore origin's names are its own"
    for origin in disk:
        directory = f"{origin.store_of('protected-a')}/{test.base}"
        found = set(os.listdir(directory))
        extra = sorted(found - set(NAMES))
        if extra:
            problems.append(f"{origin.svc}: framework/var/{directory} also holds {extra}")
        for name in NAMES:
            if name not in found:
                problems.append(f"{origin.svc}: framework/var/{directory} lacks {name!r}")
                continue
            with open(os.path.join(directory, name), "rb") as f:
                why = test.wrong(name, f.read())
            if why:
                problems.append(f"{origin.svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(disk)} store(s)"


def served(test: Test, servers: list[tuple[str, str]]) -> tuple[str, str]:
    """Each object, by its name, from each of servers (name, data URL)."""
    problems: list[str] = []
    for svc, server in servers:
        for name in NAMES:
            answer = web.request("GET", test.url(server, name), token=test.token)
            if answer.status != 200:
                problems.append(f"{svc}: {name!r}: {answer.describe()}")
                continue
            why = test.wrong(name, answer.body)
            if why:
                problems.append(f"{svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers)} server(s)"


def origin_get(test: Test) -> tuple[str, str]:
    """Each origin serves each object by its name."""
    if "DirectReads" not in test.caps:
        return SKIP, "/protected-a takes no direct clients"
    return served(test, [(o.svc, o.url) for o in test.origins])


def cache_get(test: Test) -> tuple[str, str]:
    """Each cache serves each object by its name, cold."""
    if not test.fed.caches:
        return SKIP, "this shape has no cache"
    return served(test, [(c.svc, c.url) for c in test.fed.caches])


def listed(test: Test, servers: list[tuple[str, str]]) -> tuple[str, str]:
    """Depth 1 of the run's collection at each of servers (name, data
    URL): every name once, with its size."""
    path = f"/protected-a/{test.base}"
    want: dict[str, Optional[int]] = {n: len(data) for n, data in test.objects.items()}
    problems: list[str] = []
    for svc, server in servers:
        answer = webdav.propfind(f"{server}{quote(path)}/", "1", test.token)
        if answer.status != 207:
            problems.append(f"{svc}: {answer.describe()}, not 207")
            continue
        try:
            got = webdav.relative(webdav.parse(answer.body), path)
        except ValueError as e:
            problems.append(f"{svc}: {e}")
            continue
        wrong = webdav.problems(want, got)
        if wrong:
            problems.append(f"{svc}: {'; '.join(wrong)}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(servers)} server(s)"


def origin_list(test: Test) -> tuple[str, str]:
    """Each origin lists every name."""
    if not {"Listings", "DirectReads"} <= test.caps:
        return SKIP, "/protected-a may not be listed straight from an origin"
    return listed(test, [(o.svc, o.url) for o in test.origins])


def cache_list(test: Test) -> tuple[str, str]:
    """Each federated cache lists every name (site-local caches list
    their own way; see the sitelocal suite)."""
    if not test.fed.federated_caches:
        return SKIP, "this shape has no federated cache"
    if "Listings" not in test.caps:
        return SKIP, "/protected-a has no Listings"
    return listed(test, [(c.svc, c.url) for c in test.fed.federated_caches])


def client_get(test: Test) -> tuple[str, str]:
    """`pelican object get` of every object, into a directory: a file of
    each one's name, with its bytes, and nothing else."""
    target = os.path.join(test.tmp, "client-get")
    os.makedirs(target)
    urls = [f"{test.fed.url}/protected-a/{quote(test.rel(name))}" for name in NAMES]
    code, out, err = test.session.pelican_cmd(
        "object", "get", "--token", test.token_file, *urls, target
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    found = set(os.listdir(target))
    if found != set(NAMES):
        return FAIL, f"wrote {sorted(found - set(NAMES))}, lacking {sorted(set(NAMES) - found)}"
    for name in NAMES:
        with open(os.path.join(target, name), "rb") as f:
            why = test.wrong(name, f.read())
        if why:
            return FAIL, why
    return PASS, f"{len(NAMES)} files"


def client_put(test: Test) -> tuple[str, str]:
    """`pelican object put` of a file of each name into a new collection:
    an object of each name, with the file's bytes, at some origin; and in
    a store on disk, no file of any other name."""
    if "Writes" not in test.caps:
        return SKIP, "/protected-a takes no writes"
    collection = test.put_base
    errors = stores.make_dirs(test.origins, "protected-a", collection, test.token)
    if errors:
        return FAIL, errors[0]
    directory = os.path.join(test.tmp, "client-put")
    os.makedirs(directory)
    files = {name: os.urandom(len(data)) for name, data in test.objects.items()}
    for name, data in files.items():
        with open(os.path.join(directory, name), "wb") as f:
            f.write(data)
    code, out, err = test.session.pelican_cmd(
        "object",
        "put",
        "--token",
        test.token_file,
        *(os.path.join(directory, name) for name in NAMES),
        f"{test.fed.url}/protected-a/{quote(collection)}/",
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    tokens = {"protected-a": test.token}
    for name, data in files.items():
        held = stores.held_anywhere(test.origins, "protected-a", f"{collection}/{name}", tokens)
        if common.digest(data) not in held:
            return FAIL, f"no origin holds {name!r} as its file"
    for origin in test.origins:
        if not origin.pstore:
            directory = f"{origin.store_of('protected-a')}/{collection}"
            extra = sorted(set(os.listdir(directory)) - set(NAMES))
            if extra:
                return FAIL, f"{origin.svc} also holds {extra}"
    return PASS, f"{len(files)} files"


def client_ls(test: Test) -> tuple[str, str]:
    """`pelican object ls -l --json` of the run's collection: every name,
    once, with its size."""
    if "Listings" not in test.caps:
        return SKIP, "/protected-a has no Listings"
    code, out, err = test.session.pelican_cmd(
        "object",
        "ls",
        "-l",
        "--json",
        "--token",
        test.token_file,
        f"{test.fed.url}/protected-a/{quote(test.base)}",
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    try:
        entries = json_objects(out)
    except ValueError as e:
        return FAIL, f"unreadable output ({e}): {last_line(out)}"
    listed_names = [str(e.get("Name", "")).rstrip("/").rsplit("/", 1)[-1] for e in entries]
    sizes = {name: e.get("Size") for name, e in zip(listed_names, entries)}
    if sorted(listed_names) != sorted(NAMES):
        return FAIL, f"listed {sorted(listed_names)}, not {sorted(NAMES)}"
    wrong = [n for n in NAMES if sizes[n] != len(test.objects[n])]
    if wrong:
        return FAIL, f"wrong sizes for {wrong}"
    return PASS, f"{len(NAMES)} names"


RUN = {
    "origin-put": origin_put,
    "origin-get": origin_get,
    "cache-get": cache_get,
    "origin-list": origin_list,
    "cache-list": cache_list,
    "client-get": client_get,
    "client-put": client_put,
    "client-ls": client_ls,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no /protected-a."""
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the objects in place, run the selected scenarios, and remove the
    run's objects."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/names"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()

    def clean(when: str) -> None:
        errors = stores.empty_tree_everywhere(fed.origins, fed.exports, "data/names", tokens)
        if errors:
            common.warn(f"emptying data/names {when}: {first(errors)}")

    # Earlier runs' objects come out first.
    clean("before the run")
    test = Test(session)
    print(f"Names test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        test.seed()
        for error in test.unseeded:
            common.warn(error)
        for name in selected:
            try:
                status, note = RUN[name](test)
            except stores.Unreadable as e:
                status, note = FAIL, f"could not read a store: {e}"
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        clean("after the run")
