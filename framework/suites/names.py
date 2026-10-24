"""Objects whose names a URL or a listing must encode (testlib/names.py).

Each is beside decoys, objects whose names a server or client that
mishandles the encoding would take for its own. Any bytes but the
object's own fail, and another's are named.

Objects go under /protected-a/data/names/<run>/, put at every origin
with stores.seed(), with the token of the session's profile that every
request presents, since `origin-put` checks those PUTs; the stores are
read with WLCG tokens either way. A URL names each with every character
but letters,
digits, and `-._~` percent-encoded (stores.url()); the client is given
it so too. A listing's hrefs are decoded before they are compared.

  origin-put   a PUT of each name lands in each store under that name:
               on disk as a file, and in a pstore origin's catalog as an
               entry (where /protected-a takes writes)
  origin-get   each origin serves each object by its name
  cache-get    each cache does, cold
  director-get each director's redirect, followed as a plain HTTP client
               follows it, leads to each object by its name
  origin-list  each origin lists every name, once, with its size
  cache-list   each federated cache does, as it relays the listing
  client-get   `pelican object get` of each object, in a command of its
               own, into a directory writes a file of its name, with
               its bytes: through a cache, direct, and from each
               federated cache, named
  client-put   `pelican object put` of a file of each name, in a
               command of its own, lands as the object of that name
  plugin-get   the same as client-get, through stash_plugin, which
               reads each object's URL and file from a ClassAd, as
               HTCondor writes it, with the name's quotes escaped
  plugin-put   the same as client-put, through stash_plugin
  client-ls    `pelican object ls` lists every name

The client commands, and the plugin's runs, move one object each, since
the client asks the director once for each namespace, and reuses its
answer for the rest of the namespace's objects (cachedDirectorInfo in
Pelican's client/director.go), which would hide a name that the client
mishandles in the director's answer.
"""

import json
import os
import shutil
import traceback
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import quote

from testlib import common, digests, director, stores, transfers, web, webdav
from testlib.common import die, first
from testlib.names import NAMES
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, json_objects, last_line

SCENARIOS = {
    "origin-put": "a PUT of each name lands under that name in each store, a pstore's too",
    "origin-get": "each origin serves each object by its name, and no decoy",
    "cache-get": "each cache serves each object by its name, cold, and no decoy",
    "director-get": (
        "each director's redirect, followed as a plain HTTP client, leads to each object by"
        " its name, and no decoy"
    ),
    "origin-list": "each origin lists every name once, decoded, with its size",
    "cache-list": "each federated cache lists every name once, decoded, with its size",
    "client-get": (
        "`pelican object get` of each object alone writes a file of its name, with its bytes:"
        " through a cache, direct, and from each federated cache, named"
    ),
    "client-put": "`pelican object put` of a file of each name, alone, lands as that object",
    "plugin-get": (
        "stash_plugin's get of each object alone writes a file of its name, with its bytes:"
        " through a cache, direct, and from each federated cache, named"
    ),
    "plugin-put": "stash_plugin's put of a file of each name, alone, lands as that object",
    "client-ls": "`pelican object ls` lists every name",
}


class Test:
    """The objects, by name, where they are, and the token.

    Attributes:
        origins: The origins, each store once.
        base: Where the objects are in /protected-a.
        put_base: Where the put scenarios upload, each to a collection of
            its own, beside the objects.
        tmp: The test's scratch directory.
        token_file: The file that holds token.
        token: A token for /protected-a, of the session's profile.
        setup: A token for /protected-a, of the WLCG profile, for making
            collections and reading the stores.
        caps: The capabilities of /protected-a.
        objects: Each object's bytes, by name.
        unseeded: What went wrong putting the objects in place.
    """

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.origins = stores.unique(self.fed.origins)
        self.base = f"data/names/{session.run}"
        self.put_base = f"{self.base}-put"
        self.tmp = session.path("names")
        os.makedirs(self.tmp)
        self.token_file = session.token_file("protected-a")
        self.token = session.token("protected-a")
        self.setup = session.setup_token("protected-a")
        self.caps = self.fed.exports["protected-a"]
        # Each a size of its own, so that a listing shows which is which.
        self.objects = {name: os.urandom(1000 + 37 * n) for n, name in enumerate(NAMES)}
        self.unseeded: list[str] = []

    def rel(self, name: str) -> str:
        """Return where object name is in /protected-a."""
        return f"{self.base}/{name}"

    def url(self, server: str, name: str) -> str:
        """Return the URL of object name at a server's data URL."""
        return f"{server}/protected-a/{quote(self.rel(name))}"

    def which(self, data: bytes) -> str:
        """Return the name of the object whose bytes data are, or ""."""
        return next((name for name, held in self.objects.items() if held == data), "")

    def wrong(self, name: str, data: bytes) -> Optional[str]:
        """Return why data, as served for object name, are not its bytes.

        Returns:
            Why not, or None if they are.
        """
        if data == self.objects[name]:
            return None
        other = self.which(data)
        if other:
            return f"{name!r}: served {other!r}'s bytes"
        return f"{name!r}: {len(data)} bytes that are no object's"

    def seed(self) -> None:
        """Put every object at every origin.

        What an origin refuses is left out, and recorded in unseeded: the
        scenarios then fail for it.
        """
        errors = stores.make_dirs(self.origins, "protected-a", self.base, self.setup)
        if errors:
            die(f"could not make the run's collection: {first(errors)}")
        for name, data in self.objects.items():
            self.unseeded += stores.seed_everywhere(
                self.origins, "protected-a", self.rel(name), data, self.token, self.caps
            )


def catalogued_sizes(
    test: Test, origin: common.Origin, collection: str
) -> tuple[Optional[dict[str, Any]], str]:
    """Return the size of each entry of collection in a pstore's catalog.

    collection is in /protected-a, and the origin's admin lists it
    through its storage API (stores.catalog_answer()).

    Returns:
        Each entry's size, by its name as the catalog holds it, and "";
        or None, and why the origin did not say.
    """
    found, why = stores.catalog_answer(test.session, origin, "ls", "protected-a", collection)
    if found is None:
        return None, why
    entries = common.objects(common.as_array(found.get("entries")) or [])
    return {str(e.get("name")): e.get("size") for e in entries}, ""


def catalogued(test: Test, origin: common.Origin) -> list[str]:
    """Return why a pstore origin's catalog does not hold each name.

    Its storage API must list the run's collection as one entry of each
    name, with its object's size, and nothing else; and the checksums
    that the store recorded of each, as it wrote it, must be its
    object's.

    Returns:
        What is wrong; empty if nothing is.
    """
    sizes, why = catalogued_sizes(test, origin, test.base)
    if sizes is None:
        return [why]
    problems: list[str] = []
    extra = sorted(set(sizes) - set(NAMES))
    if extra:
        problems.append(f"its catalog also holds {extra}")
    for name in NAMES:
        data = test.objects[name]
        if name not in sizes:
            problems.append(f"its catalog lacks {name!r}")
            continue
        if sizes[name] != len(data):
            # Each object is a size of its own.
            other = next((n for n, d in test.objects.items() if len(d) == sizes[name]), "")
            held = f"{other!r}'s size" if other else f"{sizes[name]} bytes"
            problems.append(f"{name!r}: its catalog holds {held}, not its own")
            continue
        found, why = stores.catalog_answer(
            test.session, origin, "digest", "protected-a", test.rel(name)
        )
        if found is None:
            problems.append(why)
            continue
        header = str(found.get("digest", ""))
        wrong, right = digests.check(header, data)
        if wrong or not right:
            problems.append(f"{name!r}: its recorded checksums '{header}' are not its bytes'")
    return problems


def origin_put(test: Test) -> tuple[str, str]:
    """Check that a PUT of each name lands in each store, by name.

    The PUTs were made as the test began. In a store on disk, each lands
    as a file of that name, with its bytes, and nothing else does; in a
    pstore origin's catalog, as an entry of that name, which checksums
    the store recorded of its bytes (catalogued()).
    """
    if "Writes" not in test.caps:
        return SKIP, "/protected-a takes no writes, so each name was written in each store"
    problems = list(test.unseeded)
    for origin in test.origins:
        if origin.pstore:
            problems += [f"{origin.svc}: {p}" for p in catalogued(test, origin)]
            continue
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
    return PASS, f"{len(test.origins)} store(s)"


def served(test: Test, servers: list[tuple[str, str]]) -> tuple[str, str]:
    """Check that each of servers serves each object by its name.

    Args:
        test: The test, as a scenario takes it.
        servers: Each server's name and data URL.

    Returns:
        The status and note, as a scenario returns them.
    """
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
    """Check that each origin serves each object by its name."""
    if "DirectReads" not in test.caps:
        return SKIP, "/protected-a takes no direct clients"
    return served(test, [(o.svc, o.url) for o in test.origins])


def cache_get(test: Test) -> tuple[str, str]:
    """Check that each cache serves each object by its name, cold."""
    if not test.fed.caches:
        return SKIP, "this shape has no cache"
    return served(test, [(c.svc, c.url) for c in test.fed.caches])


def director_get(test: Test) -> tuple[str, str]:
    """Check that each director's redirect leads to each object by its name.

    Each director's object route, asked for each object by its name,
    sends a plain HTTP client to a federated cache, or, with none, to an
    origin, by a redirect whose Location names the object again: the
    director takes the name decoded and encodes it anew (getRedirectURL
    in Pelican's director/director.go), so a name it mangled on the way
    would fetch a decoy, or nothing. The client follows the Location as
    it is, as curl -L does, and must get the object's bytes.
    """
    if not test.fed.directors:
        return SKIP, "a standalone origin has no director"
    urls = [c.url for c in test.fed.federated_caches] or [o.url for o in test.fed.origins]
    kind = "a cache" if test.fed.federated_caches else "an origin"
    problems: list[str] = []
    for url in test.fed.directors:
        svc = director.host(url)
        for name in NAMES:
            route = test.url(f"{url}/api/v1.0/director/object", name)
            answer = web.request("GET", route, token=test.token)
            where = answer.header("Location")
            if answer.status != 307 or not where:
                problems.append(f"{svc}: {name!r}: {answer.describe()}, not a redirect")
                continue
            if not any(where.startswith(u + "/") for u in urls):
                problems.append(f"{svc}: {name!r}: redirected to {where}, not {kind}")
                continue
            answer = web.request("GET", where, token=test.token)
            if answer.status != 200:
                problems.append(f"{svc}: {name!r}: {answer.describe()} at {where}")
                continue
            why = test.wrong(name, answer.body)
            if why:
                problems.append(f"{svc}: {why}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(test.fed.directors)} director(s), each leading to {kind}"


def listed(test: Test, servers: list[tuple[str, str]]) -> tuple[str, str]:
    """Check that each of servers lists every name once, with its size.

    Each lists the run's collection at depth 1.

    Args:
        test: The test, as a scenario takes it.
        servers: Each server's name and data URL.

    Returns:
        The status and note, as a scenario returns them.
    """
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
    """Check that each origin lists every name."""
    if not {"Listings", "DirectReads"} <= test.caps:
        return SKIP, "/protected-a may not be listed straight from an origin"
    return listed(test, [(o.svc, o.url) for o in test.origins])


def cache_list(test: Test) -> tuple[str, str]:
    """Check that each federated cache lists every name.

    Site-local caches list their own way.
    """
    if not test.fed.federated_caches:
        return SKIP, "this shape has no federated cache"
    if "Listings" not in test.caps:
        return SKIP, "/protected-a has no Listings"
    return listed(test, [(c.svc, c.url) for c in test.fed.federated_caches])


def fetched(test: Test, target: str, got: list[str]) -> list[str]:
    """Return why directory target does not hold the files it should.

    It must hold a file of each name in got, those of the gets that
    succeeded, with its object's bytes, and nothing else: a get that
    failed may leave no file, since the clients download into a
    temporary file, and rename it only once the object is whole.

    Returns:
        What is wrong; empty if nothing is.
    """
    found = set(os.listdir(target))
    problems = [f"{n!r}: left a file, though its get failed" for n in sorted(found - set(got))]
    for name in got:
        if name not in found:
            problems.append(f"{name!r}: no file, though its get succeeded")
            continue
        with open(os.path.join(target, name), "rb") as f:
            why = test.wrong(name, f.read())
        if why:
            problems.append(why)
    return problems


def ways(test: Test) -> tuple[list[tuple[str, Optional[common.Cache]]], list[str]]:
    """Return the ways a client reaches the objects in this shape.

    They are: through the cache the director picks; direct from an origin,
    where /protected-a takes direct clients; and from each federated cache
    in turn, named, which alone may serve it, since the client builds that
    cache's URL of each name itself.

    Returns:
        Each way's key (`cache`, `direct`, or `named-<cache>`) and the
        cache it names; and notes on the ways that the shape lacks.
    """
    found: list[tuple[str, Optional[common.Cache]]] = [("cache", None)]
    notes: list[str] = []
    if "DirectReads" in test.caps:
        found.append(("direct", None))
    else:
        notes.append("direct: skipped, /protected-a takes no direct clients")
    found += [(f"named-{c.svc}", c) for c in test.fed.federated_caches]
    if not test.fed.federated_caches:
        notes.append("named: skipped, no federated cache")
    return found, notes


def served_elsewhere(hosts: list[str], cache: Optional[common.Cache]) -> Optional[str]:
    """Return which servers other than the named cache served an object.

    Args:
        hosts: The hosts that served it, as the client recorded them.
        cache: The cache the client named, if any; with none, any may.

    Returns:
        Who, or None if only the named cache did.
    """
    if cache is None:
        return None
    others = sorted({h for h in hosts if h != director.host(cache.url)})
    if hosts and not others:
        return None
    return f"served by {', '.join(others) or 'no server recorded'}, not {cache.svc}"


def judged(done: dict[str, list[str]], problems: list[str]) -> tuple[str, str]:
    """Return the status and note of a get by each way.

    Args:
        done: For each way's key, the names whose gets succeeded.
        problems: What went wrong.
    """
    if problems:
        return FAIL, first(problems)
    return PASS, "; ".join(f"{key}: {len(got)} files" for key, got in done.items())


def client_get(test: Test) -> tuple[str, str]:
    """Check that `pelican object get` writes a file of each object's name.

    A get of each object, in a command of its own (see above), into a
    directory, must write a file of its name, with its bytes, and the
    directory nothing else, in each of the ways a client reaches an
    object (see ways()): --direct for an origin, and --cache for a
    cache it names.
    """
    found, notes = ways(test)
    done: dict[str, list[str]] = {}
    problems: list[str] = []
    for key, cache in found:
        flags = ["--direct"] if key == "direct" else ["--cache", cache.url] if cache else []
        target = os.path.join(test.tmp, f"client-get-{key}")
        os.makedirs(target)
        got: list[str] = []
        for n, name in enumerate(NAMES):
            stats = os.path.join(test.tmp, f"client-get-{key}-{n}.stats.json")
            code, out, err = test.session.pelican_cmd(
                "object",
                "get",
                "--token",
                test.token_file,
                "--transfer-stats",
                stats,
                *flags,
                f"{test.fed.url}/protected-a/{quote(test.rel(name))}",
                target,
            )
            if code != 0:
                problems.append(f"{key}: {name!r}: exit {code}: {last_line(err or out)}")
                continue
            try:
                with open(stats, encoding="utf-8") as f:
                    hosts = transfers.stats_endpoints(json.load(f))
            except (OSError, ValueError):
                hosts = []
            elsewhere = served_elsewhere(hosts, cache)
            if elsewhere:
                problems.append(f"{key}: {name!r} {elsewhere}")
            got.append(name)
        problems += [f"{key}: {p}" for p in fetched(test, target, got)]
        done[key] = got
    status, note = judged(done, problems)
    return status, "; ".join([note, *notes])


def plugin_one(
    test: Test, label: str, url: str, local: str, upload: bool, cache: Optional[common.Cache]
) -> tuple[bool, str]:
    """Run stash_plugin on one transfer, with the token for /protected-a.

    The token is the only one in the plugin's credentials directory
    (_CONDOR_CREDS), and the transfer the only ad in its input.

    Args:
        test: The test.
        label: A name for the plugin's files in test.tmp.
        url: The object's URL.
        local: Its local file.
        upload: Whether to upload, rather than download.
        cache: The cache to use alone (Client.PreferredCaches), if any.

    Returns:
        Whether its result ad says the transfer succeeded, and the ad
        (or, where it wrote none, its output).
    """
    creds = os.path.join(test.tmp, f"{label}.creds")
    os.makedirs(creds)
    shutil.copyfile(test.token_file, os.path.join(creds, "protected-a.use"))
    infile, outfile = (os.path.join(test.tmp, f"{label}.{ext}") for ext in ("in", "ad"))
    with open(infile, "w", encoding="utf-8") as f:
        f.write(transfers.transfer_ad(url, local))
    env = dict(test.session.env)
    env["_CONDOR_CREDS"] = creds
    if cache is not None:
        env["PELICAN_CLIENT_PREFERREDCACHES"] = cache.url
    _, out, err, ads = test.session.plugin_cmd(infile, outfile, upload, env, test.tmp)
    return transfers.plugin_passed(ads), ads or err or out


def plugin_get(test: Test) -> tuple[str, str]:
    """Check that stash_plugin writes a file of each object's name.

    As client_get(), through the plugin, which takes each object's URL and
    local file from an ad of its input: a URL with ?directread for an
    origin, and Client.PreferredCaches for a cache it names. The local
    file is the name's own path, as HTCondor would give it, escaped as a
    ClassAd string.
    """
    found, notes = ways(test)
    done: dict[str, list[str]] = {}
    problems: list[str] = []
    for key, cache in found:
        query = transfers.DIRECT_QUERY if key == "direct" else ""
        target = os.path.join(test.tmp, f"plugin-get-{key}")
        os.makedirs(target)
        got: list[str] = []
        for n, name in enumerate(NAMES):
            url = f"{test.fed.url}/protected-a/{quote(test.rel(name))}{query}"
            local = os.path.join(target, name)
            passed, said = plugin_one(test, f"plugin-get-{key}-{n}", url, local, False, cache)
            if not passed:
                why = transfers.plugin_error(said) or last_line(said)
                problems.append(f"{key}: {name!r}: failed: {why}")
                continue
            elsewhere = served_elsewhere(transfers.plugin_endpoints(said), cache)
            if elsewhere:
                problems.append(f"{key}: {name!r} {elsewhere}")
            got.append(name)
        problems += [f"{key}: {p}" for p in fetched(test, target, got)]
        done[key] = got
    status, note = judged(done, problems)
    return status, "; ".join([note, *notes])


def put_by(test: Test, label: str, put: Callable[[str, str], Optional[str]]) -> tuple[str, str]:
    """Check that a put of a file of each name lands as that object.

    A put of each, alone, to its object's URL in a new collection must
    make an object of the name, with the file's bytes, at some origin;
    and no store may hold one of any other name there: on disk, no file,
    and in a pstore origin's catalog, as its storage API lists it, no
    entry.

    Args:
        test: The test.
        label: The scenario's name, for its collection and files.
        put: What puts one file: called with the file's path and the
            object's URL, it returns why the put failed, or None.
    """
    if "Writes" not in test.caps:
        return SKIP, "/protected-a takes no writes"
    collection = f"{test.put_base}/{label}"
    errors = stores.make_dirs(test.origins, "protected-a", collection, test.setup)
    if errors:
        return FAIL, errors[0]
    directory = os.path.join(test.tmp, label)
    os.makedirs(directory)
    files = {name: os.urandom(len(data)) for name, data in test.objects.items()}
    for name, data in files.items():
        with open(os.path.join(directory, name), "wb") as f:
            f.write(data)
    problems: list[str] = []
    put_names: list[str] = []
    for name in NAMES:
        url = f"{test.fed.url}/protected-a/{quote(collection)}/{quote(name)}"
        why = put(os.path.join(directory, name), url)
        if why:
            problems.append(f"{name!r}: {why}")
        else:
            put_names.append(name)
    tokens = {"protected-a": test.setup}
    for name in put_names:
        held = stores.held_anywhere(test.origins, "protected-a", f"{collection}/{name}", tokens)
        if common.digest(files[name]) not in held:
            problems.append(f"no origin holds {name!r} as its file")
    for origin in test.origins:
        if origin.pstore:
            sizes, why = catalogued_sizes(test, origin, collection)
            if sizes is None:
                problems.append(f"{origin.svc}: {why}")
                continue
            held_names = set(sizes)
        else:
            held_names = set(os.listdir(f"{origin.store_of('protected-a')}/{collection}"))
        extra = sorted(held_names - set(NAMES))
        if extra:
            problems.append(f"{origin.svc} also holds {extra}")
    if problems:
        return FAIL, first(problems)
    return PASS, f"{len(files)} files"


def client_put(test: Test) -> tuple[str, str]:
    """Check that `pelican object put` of each name lands as that object.

    Each file goes in a command of its own (see above); put_by() judges
    what lands.
    """

    def put(source: str, url: str) -> Optional[str]:
        code, out, err = test.session.pelican_cmd(
            "object", "put", "--token", test.token_file, source, url
        )
        return f"exit {code}: {last_line(err or out)}" if code != 0 else None

    return put_by(test, "client-put", put)


def plugin_put(test: Test) -> tuple[str, str]:
    """Check that stash_plugin's put of each name lands as that object.

    Each file goes in a run of its own, as the only ad of its input (see
    plugin_one()); put_by() judges what lands.
    """
    count = [0]

    def put(source: str, url: str) -> Optional[str]:
        count[0] += 1
        passed, said = plugin_one(test, f"plugin-put-{count[0]}", url, source, True, None)
        return None if passed else f"failed: {transfers.plugin_error(said) or last_line(said)}"

    return put_by(test, "plugin-put", put)


def client_ls(test: Test) -> tuple[str, str]:
    """Check that `pelican object ls` lists every name once, with its size.

    It lists the run's collection, with -l --json.
    """
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


# The scenarios, by name: each takes the Test, and returns its status and
# a note.
RUN = {
    "origin-put": origin_put,
    "origin-get": origin_get,
    "cache-get": cache_get,
    "director-get": director_get,
    "origin-list": origin_list,
    "cache-list": cache_list,
    "client-get": client_get,
    "client-put": client_put,
    "plugin-get": plugin_get,
    "plugin-put": plugin_put,
    "client-ls": client_ls,
}


def skip(fed: common.Federation) -> Optional[str]:
    """Return why there is nothing to test: no /protected-a."""
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Seed the objects, run the selected scenarios, then remove them."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        directory = f"{origin.store_of('protected-a')}/data/names"
        if not origin.pstore and not os.path.isdir(directory):
            die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.setup_tokens()

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
