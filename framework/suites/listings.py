"""Listings, at each origin, each director, and each cache.

The suite sends PROPFIND at depths 0, 1, and infinity, in every
namespace, over a small tree. It also checks who may list /protected-a,
a GET of a collection, which is no object, and the links of an origin's
HTML listing, which a browser follows.

Objects go under /<namespace>/data/listings/<run>/, put straight in
every origin's store (stores.seed(); a pstore origin's /public reads
/protected-a's storage). Each namespace's tree also holds an object of
its own, `only-<namespace>`, so that a server that lists another
namespace's tree lists the wrong one.

Who may list depends on the namespace's capabilities (may_list()).
Depth 0 is a stat; anything deeper needs Listings. An origin answers
only direct clients, which a namespace without DirectReads has none of
(`-p origin-no-direct`). A director redirects a PROPFIND to an origin. A
cache answers Depth 0 of anything it may read, and relays deeper
listings to an origin. A PROPFIND that may not be answered must be
refused, with 401, 403, or 405.

Every PROPFIND and GET presents a token of the session's profile. The
objects are put in place, and the stores read, with WLCG tokens.

See docs/tests/listings.md.
"""

import os
import time
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from functools import partial
from html.parser import HTMLParser
from typing import override
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from testlib import common, credentials, stores, suite, web, webdav
from testlib.common import die, first, outcome
from testlib.names import NAMES
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, json_objects, last_line
from testlib.suite import Row

SCENARIOS = {
    "origin-depth-0": "each origin: Depth 0 of an object, and of a collection",
    "origin-depth-1": "each origin: Depth 1 lists a collection's members",
    "origin-infinity": "each origin: Depth infinity lists the whole tree, or is refused",
    "director": "each director redirects a PROPFIND to an origin, which lists or refuses",
    "cache-depth-0": "each cache: Depth 0 of an object it lacks, then of one it holds",
    "cache-depth-1": "each cache: Depth 0 and 1 of a collection, as the origins list it",
    "cache-infinity": "each cache: Depth infinity, as at the origins",
    "cache-body": "each cache: Depth 1 with a propfind body, as the client sends",
    "protected-a-no-token": "listing /protected-a with no token is refused by every origin and cache",
    "protected-a-scope": (
        "listing /protected-a with a token for elsewhere is refused by every origin and cache"
    ),
    "public-no-token": "listing /public with no token is allowed by every origin and cache",
    "new-object": "an object is listed as soon as it is uploaded, and not once removed",
    "collection-get": "a GET of a collection is no object's bytes, and no cache keeps it as one",
    "html-links": "each origin's HTML listing links to each object by its name, and no decoy",
    "plain-object-listing": "Depth 1 of an object, at each origin and cache, and `ls` of it, list it alone",
}

# The scenarios that present no token (see suites/__init__.py).
ONE_PROFILE = ("protected-a-no-token", "public-no-token")

# The tree under <base>/tree, in every namespace: each object's size (paths
# below it).
TREE = {"a": 1000, "b": 5000, "empty": 0, "sub/c": 300, "sub/deeper/d": 7000}
# Depth 1 of tree/: sizes, and None for a collection.
CHILDREN = {"a": 1000, "b": 5000, "empty": 0, "sub": None}
# Depth infinity of tree/.
EVERYTHING = {**TREE, "sub": None, "sub/deeper": None}
# The size of the object that only one namespace's tree holds.
OWN = 777
# The size of the object each cache first sees through a PROPFIND.
PROBE = 2345
# How a server refuses a PROPFIND.
REFUSALS = (401, 403, 405)
# How far an object's getlastmodified may be from its file's modification
# time, in seconds. A time rendered in the wrong zone is hours off.
MODIFIED_SLACK = 60


def may_list(caps: AbstractSet[str], depth: str, role: str) -> bool:
    """Return whether a server in role should answer a PROPFIND at depth.

    Depth 0 is a stat, and anything deeper a listing, which needs
    Listings. An origin answers only direct clients; a cache answers
    Depth 0 of anything it may read; and a director answers by
    redirecting to an origin, Depth 0 where the namespace may be read at
    all (Reads or PublicReads), as for a GET (director/sort.go).

    Args:
        caps: The namespace's capabilities.
        depth: `0`, `1`, or `infinity`.
        role: `origin`, `cache`, or `director`.
    """
    if role == "origin" and "DirectReads" not in caps:
        return False
    if depth == "0":
        return role != "director" or bool({"Reads", "PublicReads"} & set(caps))
    return "Listings" in caps


class Test(suite.Test):
    """The tree in each namespace, and the stores and servers to ask.

    The base's tokens are /protected-a's.

    Attributes:
        origins: The origins, each store once.
        caches: The federated caches.
        namespaces: The exported namespaces.
        tokens: Each namespace's `server` token, by namespace, of the
            session's profile.
        elsewhere: A token like `server`'s for /protected-a, but to read
            <base>/elsewhere alone.
    """

    def __init__(self, session: Session):
        super().__init__(session, "listings")
        self.origins = stores.unique(self.fed.origins)
        # Site-local caches answer listings their own way.
        self.caches = self.fed.federated_caches
        self.namespaces = session.namespaces
        self.tokens = {ns: session.token(ns) for ns in self.namespaces}
        self.elsewhere = credentials.mint_server(
            self.fed,
            session.path("listings-elsewhere.token"),
            "protected-a",
            scopes=credentials.R,
            scope_path=f"/{self.base}/elsewhere",
            profile=session.profile,
        )

    def path_in(self, namespace: str, rel: str = "tree") -> str:
        """Return the path of rel, in the run's collection, in namespace."""
        return f"/{namespace}/{self.rel(rel)}"

    def own(self, namespace: str) -> str:
        """Return the object that only namespace's tree holds.

        It is named for the namespace whose storage namespace reads (see
        Federation.storage_namespace()).
        """
        return f"only-{self.fed.storage_namespace(namespace)}"

    def children(self, namespace: str) -> dict[str, int | None]:
        """Return Depth 1 of namespace's tree/."""
        return {**CHILDREN, self.own(namespace): OWN}

    def everything(self, namespace: str) -> dict[str, int | None]:
        """Return Depth infinity of namespace's tree/."""
        return {**EVERYTHING, self.own(namespace): OWN}

    def refusal(self, url: str, depth: str, token: str | None) -> str | None:
        """Return what is wrong with how url answers a PROPFIND at depth.

        url should refuse it.

        Returns:
            What is wrong, or None if it refuses.
        """
        answer = webdav.propfind(url, depth, token)
        if answer.status == 207:
            return f"Depth {depth}: listed it, but should have refused"
        if answer.status not in REFUSALS:
            return f"Depth {depth}: {answer.describe()}, not a refusal (401, 403, or 405)"
        return None

    def seed(self) -> list[str]:
        """Put the tree, and a probe object for each cache, in place.

        Returns:
            What went wrong.
        """
        errors: list[str] = []
        for origin in self.origins:
            for ns in stores.storage_namespaces(origin, self.fed.exports):
                setup = self.session.setup_token(ns)
                for rel in ("tree/sub/deeper", "probe"):
                    errors += stores.make_dirs([origin], ns, self.rel(rel), setup)
                if errors:
                    return errors
                objects = {f"tree/{p}": size for p, size in TREE.items()}
                objects[f"tree/{self.own(ns)}"] = OWN
                objects.update({f"probe/{c.svc}": PROBE for c in self.caches})
                for rel, size in objects.items():
                    error = stores.seed(
                        origin, ns, self.rel(rel), os.urandom(size), setup, self.fed.exports[ns]
                    )
                    if error:
                        return [error]
        return errors

    def compare(
        self,
        url: str,
        depth: str,
        base: str,
        want: Mapping[str, int | None],
        token: str | None,
        body: bytes | None = None,
    ) -> str | None:
        """Return what is wrong with url's listing at depth against want."""
        _, got, why = webdav.listing(url, depth, base, token, body)
        if got is None:
            return why
        wrong = webdav.problems(want, got)
        return f"Depth {depth}: {'; '.join(wrong)}" if wrong else None

    def ls(self, rel: str) -> tuple[dict[str, tuple[bool, int]] | None, str]:
        """Run `pelican object ls -l --json` of rel in /protected-a.

        Returns:
            Each name's collection flag and size, and ""; or None, and why
            it failed.
        """
        code, out, err = self.session.pelican_cmd(
            "object", "ls", "-l", "--json", "-t", self.token_file, self.pelican_url(self.rel(rel))
        )
        if code != 0:
            return None, f"exit {code}: {last_line(err)}"
        try:
            return {
                e["Name"].rstrip("/").rsplit("/", 1)[-1]: (bool(e.get("IsCollection")), e.get("Size") or 0)
                for e in json_objects(out)
            }, ""
        except (ValueError, KeyError, AttributeError):
            return None, f"no listing in the output: {last_line(out)}"


# --------------------------------------------------------------------------
# The scenarios. Each returns its result and a note.


def modified_problem(test: Test, origin: common.Origin, ns: str, entry: webdav.Entry) -> str:
    """Return why entry's getlastmodified is not when its file was written.

    The file is the object's in origin's store: on disk, its modification
    time; in a pstore origin's catalog, its entry's (stores.written_at()).
    The origins and the tests keep different time zones (TZ_STORAGE), so a
    time rendered in the wrong one shows.

    Returns:
        Why, or "" if the time is right.
    """
    if entry.modified is None:
        return "no getlastmodified"
    written, why = stores.written_at(test.session, origin, ns, test.rel("tree/b"))
    if written is None:
        return why
    off = entry.modified.timestamp() - written
    if abs(off) > MODIFIED_SLACK:
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(written))
        return f"getlastmodified is {off:+.0f}s from the file's modification time, {when}"
    return ""


def origin_depth_0(test: Test) -> Row:
    """Check Depth 0 of an object, and of a collection, at each origin.

    The object's getlastmodified must be when its file was written.
    """
    problems: list[str] = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path_in(ns)
            for rel, want in (("b", 5000), ("", None)):
                target = f"{path}/{rel}" if rel else f"{path}/"
                if not may_list(test.fed.exports[ns], "0", "origin"):
                    why = test.refusal(f"{origin.url}{target}", "0", test.tokens[ns])
                    if why:
                        problems.append(f"{origin.svc} {target}: {why}")
                    continue
                _, got, why = webdav.listing(
                    f"{origin.url}{target}",
                    "0",
                    f"{path}/{rel}" if rel else path,
                    test.tokens[ns],
                )
                if got is None:
                    problems.append(f"{origin.svc} {target}: {why}")
                    continue
                entry = got.get("")
                if set(got) != {""} or entry is None:
                    problems.append(f"{origin.svc} {target}: Depth 0 listed {sorted(got)}")
                elif want is None and not entry.collection:
                    problems.append(f"{origin.svc} {target}: not a collection")
                elif want is not None and (entry.collection or entry.size != want):
                    problems.append(f"{origin.svc} {target}: {entry.size} bytes, not {want}")
                elif want is not None and (why := modified_problem(test, origin, ns, entry)):
                    problems.append(f"{origin.svc} {target}: {why}")
    return outcome(problems, f"{len(test.origins)} origin(s)")


def origin_depth_1(test: Test) -> Row:
    """Check that each origin lists a collection's members at Depth 1."""
    problems: list[str] = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path_in(ns)
            url = f"{origin.url}{path}/"
            if may_list(test.fed.exports[ns], "1", "origin"):
                why = test.compare(url, "1", path, test.children(ns), test.tokens[ns])
            else:
                why = test.refusal(url, "1", test.tokens[ns])
            if why:
                problems.append(f"{origin.svc} {path}/: {why}")
    return outcome(problems, f"{len(test.origins)} origin(s)")


def infinity(
    test: Test,
    url: str,
    namespace: str,
    token: str | None,
    refusable: bool = True,
) -> tuple[str | None, str]:
    """Check Depth infinity of namespace's tree at url.

    RFC 4918 lets a server refuse it with 403, unless refusable is False,
    as for a cache whose origins answer it.

    Returns:
        What is wrong, or None; and a note.
    """
    answer, got, why = webdav.listing(url, "infinity", test.path_in(namespace), token)
    if answer.status == 403:
        if refusable:
            return None, "refused (403)"
        return "Depth infinity: refused (403), though no origin refuses it", ""
    if got is None:
        return why, ""
    if set(got) - {""} == set(test.children(namespace)):
        return "Depth infinity listed only the first level, as Depth 1 would", ""
    wrong = webdav.problems(test.everything(namespace), got)
    return (f"Depth infinity: {'; '.join(wrong)}" if wrong else None), "the whole tree"


def origin_infinity(test: Test) -> Row:
    """Check that each origin lists the whole tree at Depth infinity.

    An origin may refuse it instead.
    """
    problems: list[str] = []
    notes: list[str] = []
    for origin in test.origins:
        for ns in test.namespaces:
            path = test.path_in(ns)
            url = f"{origin.url}{path}/"
            if may_list(test.fed.exports[ns], "infinity", "origin"):
                why, note = infinity(test, url, ns, test.tokens[ns])
            else:
                why, note = test.refusal(url, "infinity", test.tokens[ns]), "refused"
            if why:
                problems.append(f"{origin.svc} {path}/: {why}")
            elif note not in notes:
                notes.append(note)
    return outcome(problems, ", ".join(notes))


def director(test: Test) -> Row:
    """Check that each director redirects a PROPFIND to an origin.

    The origin's listing must then be right; or, where the director may
    not answer it (see may_list()), the PROPFIND must come back as a
    refusal. Where the origin takes no direct clients, it must refuse
    what the director sent there.
    """
    origins = {urlsplit(o.url).netloc for o in test.fed.origins}
    problems: list[str] = []
    for director_url in test.fed.directors:
        for ns in test.namespaces:
            path = test.path_in(ns)
            for target, depth, base, want in (
                (f"{path}/b", "0", f"{path}/b", None),
                (f"{path}/", "1", path, test.children(ns)),
            ):
                where = f"{urlsplit(director_url).netloc} {target}"
                url = f"{director_url}{target}"
                if not may_list(test.fed.exports[ns], depth, "director"):
                    if why := test.refusal(url, depth, test.tokens[ns]):
                        problems.append(f"{where}: {why}")
                    continue
                answer = webdav.propfind(url, depth, test.tokens[ns])
                location = answer.header("Location")
                if answer.status not in (301, 302, 303, 307, 308) or not location:
                    problems.append(f"{where}: {answer.describe()}, not a redirect")
                    continue
                parts = urlsplit(location)
                if parts.netloc not in origins:
                    problems.append(f"{where}: redirected to {parts.netloc}, not an origin")
                    continue
                if not parts.path.rstrip("/").endswith(target.rstrip("/")):
                    problems.append(f"{where}: redirected to {parts.path}")
                    continue
                if not may_list(test.fed.exports[ns], depth, "origin"):
                    if why := test.refusal(location, depth, test.tokens[ns]):
                        problems.append(f"{where} -> {parts.netloc}: {why}")
                    continue
                if want is None:
                    _, got, why = webdav.listing(location, depth, base, test.tokens[ns])
                    if got is None:
                        problems.append(f"{where} -> {parts.netloc}: {why}")
                    elif set(got) != {""} or got[""].size != 5000:
                        problems.append(f"{where} -> {parts.netloc}: not the object")
                elif why := test.compare(location, depth, base, want, test.tokens[ns]):
                    problems.append(f"{where} -> {parts.netloc}: {why}")
    return outcome(problems, f"{len(test.fed.directors)} director(s)")


def cache_depth_0(test: Test) -> Row:
    """Check Depth 0 of each cache's own probe object.

    Ask before the cache holds it, then after a GET.
    """
    problems: list[str] = []
    for cache in test.caches:
        path = test.path_in("protected-a", f"probe/{cache.svc}")
        url = f"{cache.url}{path}"
        for when in ("cold", "cached"):
            if when == "cached":
                answer = web.request("GET", url, token=test.token)
                if answer.status != 200 or len(answer.body) != PROBE:
                    problems.append(f"{cache.svc}: GET {path}: {answer.describe()}")
                    break
            _, got, why = webdav.listing(url, "0", path, test.token)
            if got is None:
                problems.append(f"{cache.svc} ({when}): {why}")
            elif set(got) != {""} or got[""].collection or got[""].size != PROBE:
                problems.append(f"{cache.svc} ({when}): not the {PROBE}-byte object")
    return outcome(problems, f"{len(test.caches)} cache(s)")


def cache_depth_1(test: Test) -> Row:
    """Check Depth 0 and 1 of a collection at each cache.

    Each must list it as the origins do.
    """
    problems: list[str] = []
    caps = test.caps
    for cache in test.caches:
        path = test.path_in("protected-a")
        url = f"{cache.url}{path}/"
        _, got, why = webdav.listing(url, "0", path, test.token)
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        if set(got) != {""} or not got[""].collection:
            problems.append(f"{cache.svc}: Depth 0 of {path}/ is not one collection")
            continue
        if not may_list(caps, "1", "cache"):
            if refusal := test.refusal(url, "1", test.token):
                problems.append(f"{cache.svc}: {refusal}")
            continue
        _, got, why = webdav.listing(url, "1", path, test.token)
        if got is None:
            problems.append(f"{cache.svc}: {why}")
            continue
        wrong = webdav.problems(test.children("protected-a"), got)
        if wrong:
            problems.append(f"{cache.svc}: Depth 1: {'; '.join(wrong)}")
    return outcome(problems, f"{len(test.caches)} cache(s)")


def origins_refuse_infinity(test: Test, ns: str) -> bool | None:
    """Return whether an origin refuses Depth infinity of ns's tree (403).

    If one does, a cache that relays it to one then may.

    Returns:
        Whether one does, or None if no origin may be asked, as where ns
        takes no direct clients.
    """
    if not may_list(test.fed.exports[ns], "infinity", "origin"):
        return None
    path = test.path_in(ns)
    return any(
        webdav.propfind(f"{o.url}{path}/", "infinity", test.tokens[ns]).status == 403 for o in test.origins
    )


def cache_infinity(test: Test) -> Row:
    """Check that each cache lists the whole tree at Depth infinity.

    A cache relays it to an origin. It may refuse with 403 only if an
    origin does; where no origin may be asked, its 403 is taken on trust,
    as the note says.
    """
    problems: list[str] = []
    notes: list[str] = []
    caps = test.caps
    refused = None
    if may_list(caps, "infinity", "cache"):
        refused = origins_refuse_infinity(test, "protected-a")
    for cache in test.caches:
        path = test.path_in("protected-a")
        url = f"{cache.url}{path}/"
        if may_list(caps, "infinity", "cache"):
            why, note = infinity(test, url, "protected-a", test.token, refused is not False)
            if note == "refused (403)":
                note += ", as an origin does" if refused else ", which no origin could be asked to confirm"
        else:
            why, note = test.refusal(url, "infinity", test.token), "refused"
        if why:
            problems.append(f"{cache.svc}: {why}")
        else:
            notes.append(f"{cache.svc}: {note}")
    return outcome(problems, ", ".join(notes))


def cache_body(test: Test) -> Row:
    """Check that each cache follows the director's redirect of a PROPFIND.

    A cache relays a PROPFIND through the director, and must follow its
    redirect, with the request's body, to answer it.
    """
    problems: list[str] = []
    caps = test.caps
    for cache in test.caches:
        path = test.path_in("protected-a")
        url = f"{cache.url}{path}/"
        if not may_list(caps, "1", "cache"):
            if why := test.refusal(url, "1", test.token):
                problems.append(f"{cache.svc}: {why}")
            continue
        answer, got, why = webdav.listing(url, "1", path, test.token, webdav.PROPFIND_BODY)
        if answer.status is not None and 300 <= answer.status < 400:
            problems.append(
                f"{cache.svc}: answered with the director's {answer.status}, to"
                + f" {urlsplit(answer.header('Location')).netloc}"
            )
        elif got is None:
            problems.append(f"{cache.svc}: {why}")
        elif wrong := webdav.problems(test.children("protected-a"), got):
            problems.append(f"{cache.svc}: {'; '.join(wrong)}")
    return outcome(problems, f"{len(test.caches)} cache(s)")


def servers(test: Test) -> list[tuple[str, str, str]]:
    """Return every origin and cache: its name, data URL, and role."""
    return [(o.svc, o.url, "origin") for o in test.origins] + [(c.svc, c.url, "cache") for c in test.caches]


def refused_everywhere(test: Test, token: str | None) -> Row:
    """Check that every server refuses to list /protected-a with token.

    token is none, or one for elsewhere. A listing of an object, and of a
    collection, must each be refused: with 401 or 403, or with 405 where
    the server would not list it anyway.
    """
    problems: list[str] = []
    caps = test.caps
    for svc, url, role in servers(test):
        path = test.path_in("protected-a")
        for target, depth in ((f"{path}/b", "0"), (f"{path}/", "1")):
            answer = webdav.propfind(f"{url}{target}", depth, token)
            allowed = (401, 403) if may_list(caps, depth, role) else REFUSALS
            if answer.status == 207:
                problems.append(f"{svc} Depth {depth} {target}: listed it")
            elif answer.status not in allowed:
                problems.append(
                    f"{svc} Depth {depth} {target}: {answer.describe()},"
                    + f" not {' or '.join(map(str, allowed))}"
                )
    return outcome(problems, f"{len(servers(test))} server(s)")


def protected_a_no_token(test: Test) -> Row:
    """Check refused_everywhere() with no token."""
    return refused_everywhere(test, None)


def protected_a_scope(test: Test) -> Row:
    """Check refused_everywhere() with a token for elsewhere."""
    return refused_everywhere(test, test.elsewhere)


def public_no_token(test: Test) -> Row:
    """Check that anyone may list /public, as far as its capabilities allow.

    /public has PublicReads (see may_list()).
    """
    problems: list[str] = []
    path = test.path_in("public")
    for svc, url, role in servers(test):
        if may_list(test.fed.exports["public"], "1", role):
            why = test.compare(f"{url}{path}/", "1", path, test.children("public"), None)
        else:
            why = test.refusal(f"{url}{path}/", "1", None)
        if why:
            problems.append(f"{svc}: {why}")
    return outcome(problems, f"{len(servers(test))} server(s)")


def new_object(test: Test) -> Row:
    """Check that a new object is listed at once, and not once removed.

    The object is put straight in every origin's store, and must be
    listed at once by each origin and cache (which relays Depth 1), and
    by the client; once removed from every origin, by none.
    """
    caps = test.caps
    if "Listings" not in caps:
        return SKIP, "/protected-a has no Listings"
    rel = test.rel("tree/new")
    errors = stores.seed_everywhere(test.origins, "protected-a", rel, os.urandom(1234), test.setup, caps)
    if errors:
        return FAIL, errors[0]
    path = test.path_in("protected-a")
    problems: list[str] = []
    for when, want in (
        ("after the upload", {**test.children("protected-a"), "new": 1234}),
        ("after the removal", test.children("protected-a")),
    ):
        if when == "after the removal":
            for origin in test.origins:
                if why := stores.delete(origin, "protected-a", rel, test.setup):
                    return FAIL, why
        for svc, url, role in servers(test):
            if not may_list(caps, "1", role):
                continue
            if why := test.compare(f"{url}{path}/", "1", path, want, test.token):
                problems.append(f"{svc} {when}: {why}")
        listed, why = test.ls("tree")
        if listed is None:
            problems.append(f"`pelican object ls` {when}: {why}")
        elif ("new" in listed) != ("new" in want):
            listed_or_not = "listed" if "new" in listed else "not listed"
            problems.append(f"`pelican object ls` {when}: {listed_or_not}")
    return outcome(problems, "listed at once, and not once removed")


def collection_get(test: Test) -> Row:
    """Check a GET of a collection, tree/sub, which is no object.

    An origin asked without Accept: text/html, as by a client that
    expects bytes, may refuse or answer a listing, but never an object's
    bytes. Asked with it, it must answer an HTML listing. A federated
    cache must not keep what an origin answers as the collection's
    object: its answer must be no object's bytes, and a V2 cache's 200
    must be typed application/octet-stream, with X-Content-Type-Options:
    nosniff, and carry no Age, on a second GET either. The object in the
    collection must then come back intact. The origins are asked only if
    they take direct clients.
    """
    caps = test.caps
    token = test.token
    path = test.path_in("protected-a", "tree/sub")
    want = stores.held(test.origins[0], "protected-a", test.rel("tree/sub/c"), test.setup)
    if want is None:
        return FAIL, f"{test.origins[0].svc}'s store lacks tree/sub/c"

    def is_object(body: bytes) -> bool:
        return common.digest(body) == want

    problems: list[str] = []
    origins = test.origins if may_list(caps, "0", "origin") else []
    for origin in origins:
        for accept in ("", "text/html"):
            headers = {"Accept": accept} if accept else None
            answer = web.request("GET", f"{origin.url}{path}", token=token, headers=headers)
            kind = answer.header("Content-Type").lower()
            what = f"{origin.svc}: GET {path}{' as a browser' if accept else ''}"
            if is_object(answer.body):
                problems.append(f"{what}: served the object in it")
            elif accept and answer.status != 200:
                problems.append(f"{what}: {answer.describe()}, not a listing")
            elif accept and not kind.startswith("text/html"):
                problems.append(f"{what}: Content-Type '{kind}', not text/html")
    for cache in test.caches:
        url = f"{cache.url}{path}"
        what = f"{cache.svc}: GET {path}"
        for when in ("", ", again"):
            answer = web.request("GET", url, token=token)
            kind = answer.header("Content-Type").lower()
            if is_object(answer.body):
                problems.append(f"{what}{when}: served the object in it")
            elif cache.kind == "v2" and answer.header("Age"):
                problems.append(f"{what}{when}: Age, so it keeps the listing as the object")
            elif cache.kind == "v2" and answer.status == 200:
                if not kind.startswith("application/octet-stream"):
                    problems.append(f"{what}{when}: Content-Type '{kind}', not octet-stream")
                elif answer.header("X-Content-Type-Options").lower() != "nosniff":
                    problems.append(f"{what}{when}: no X-Content-Type-Options: nosniff")
        answer = web.request("GET", f"{cache.url}{path}/c", token=token)
        if answer.status != 200 or not is_object(answer.body):
            problems.append(
                f"{cache.svc}: GET {path}/c after the collection: {answer.describe()},"
                + " or not the object"
            )
    return outcome(problems, f"{len(origins)} origin(s), {len(test.caches)} cache(s)")


class Anchors(HTMLParser):
    """The links (<a href>) of an HTML page, as a browser reads them.

    Character references are resolved.

    Attributes:
        links: Each link's href, and its text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    @override
    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href.strip(), "".join(self._text).strip()))
            self._href = None


def anchors(page: bytes) -> list[tuple[str, str]]:
    """Return the links of an HTML page (see Anchors)."""
    parser = Anchors()
    parser.feed(page.decode("utf-8", "replace"))
    parser.close()
    return parser.links


# What a browser leaves as it is in a URL's path, and in its query: every
# printable ASCII character but these (the WHATWG URL standard's path and
# query percent-encode sets). It percent-encodes the rest, and anything
# beyond ASCII as UTF-8, but leaves a `%` as it is.
PATH_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in '"#<>?`{}')
QUERY_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in "\"#<>'")


def followed(page: str, href: str) -> str:
    """Return the URL a browser asks for, following a link to href on page.

    page is the page's URL. The URL is href resolved against page,
    without its fragment, and percent-encoded as PATH_SAFE and QUERY_SAFE
    say.
    """
    parts = urlsplit(urljoin(page, href))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe=PATH_SAFE),
            quote(parts.query, safe=QUERY_SAFE),
            "",
        )
    )


def is_html(answer: web.Response) -> bool:
    """Return whether answer is an HTML page."""
    kind = answer.header("Content-Type").lower()
    return answer.status == 200 and kind.startswith("text/html")


# html-links' collection, beside the objects of NAMES: its name, and the
# size of the one object in it.
HTML_SUB = "sub"
HTML_INNER = 4321


def link_problems(
    test: Test, svc: str, page: str, listing: web.Response, objects: Mapping[str, bytes]
) -> list[str]:
    """Return what is wrong with the links of listing, at origin svc.

    listing is the HTML listing at URL page of a collection that holds
    objects (by name) and HTML_SUB. A link is known by its text, the
    entry's name (a collection's with a / after it), as a browser's user
    knows it.
    """
    token = test.token
    host = urlsplit(page).netloc
    problems: list[str] = []
    linked: set[str] = set()
    for href, text in anchors(listing.body):
        url = followed(page, href)
        if urlsplit(url).netloc != host:
            continue
        name = text.rstrip("/")
        what = f"{svc}: the link to {name!r} ({href})"
        if name in objects:
            linked.add(name)
            answer = web.request("GET", url, token=token)
            other = next((n for n, data in objects.items() if data == answer.body), "")
            if answer.status != 200:
                problems.append(f"{what}: {answer.describe()}")
            elif other != name:
                bytes_of = f"{other!r}'s bytes" if other else "bytes that are no object's"
                problems.append(f"{what}: {bytes_of}")
        elif name in ("..", HTML_SUB):
            # The parent's listing must link to this collection, and the
            # child's to the object in it.
            linked.add(name)
            want = "html" if name == ".." else "inner"
            answer = web.request("GET", url, token=token, headers={"Accept": "text/html"})
            if not is_html(answer):
                problems.append(f"{what}: {answer.describe()}, not an HTML listing")
            elif want not in [t.rstrip("/") for _, t in anchors(answer.body)]:
                problems.append(f"{what}: a listing without {want!r}")
        else:
            problems.append(f"{svc}: a link to {text!r} ({href}), which is no entry")
    missing = [n for n in [*objects, HTML_SUB] if n not in linked]
    if missing:
        more = f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""
        problems.append(f"{svc}: no link to {missing[0]!r}{more}")
    return problems


def html_links(test: Test) -> Row:
    """Check the links of each origin's HTML listing.

    The listing (a GET with Accept: text/html) is of a collection that
    holds an object for each of NAMES, decoys among them, and a
    collection, HTML_SUB. Each link, followed as a browser follows it
    (followed()), must bring the object of its name, never a decoy's
    bytes or a 404; the link to HTML_SUB, or to the parent (..), if there
    is one, must bring its listing. Every entry must have a link. A
    native origin's listing must carry Cache-Control: no-store, so that
    no cache keeps it. An origin that answers no HTML listing is passed
    over, and named in the note, since collection-get fails it. The
    origins are asked only if they take direct clients.
    """
    caps = test.caps
    if not may_list(caps, "0", "origin"):
        return SKIP, "/protected-a takes no direct clients"
    token = test.token
    setup = test.setup
    rel = test.rel("html")
    # Each a size of its own.
    objects = {name: os.urandom(1000 + 37 * n) for n, name in enumerate(NAMES)}
    errors = stores.make_dirs(test.origins, "protected-a", f"{rel}/{HTML_SUB}", setup)
    for name, data in [*objects.items(), (f"{HTML_SUB}/inner", os.urandom(HTML_INNER))]:
        if errors:
            break
        errors += stores.seed_everywhere(test.origins, "protected-a", f"{rel}/{name}", data, setup, caps)
    if errors:
        return FAIL, errors[0]
    problems: list[str] = []
    unlisted: list[str] = []
    for origin in test.origins:
        page = origin.url + quote(f"/protected-a/{rel}/")
        answer = web.request("GET", page, token=token, headers={"Accept": "text/html"})
        if not is_html(answer):
            unlisted.append(origin.svc)
            continue
        control = answer.header("Cache-Control")
        if test.fed.native_origins and "no-store" not in control.lower():
            problems.append(f"{origin.svc}: the listing's Cache-Control is '{control}'")
        problems += link_problems(test, origin.svc, page, answer, objects)
    if problems:
        return FAIL, first(problems)
    if len(unlisted) == len(test.origins):
        return SKIP, "no origin answers an HTML listing (see collection-get)"
    note = f"{len(test.origins) - len(unlisted)} origin(s)"
    if unlisted:
        note += f"; no HTML listing from {', '.join(unlisted)}"
    return PASS, note


def plain_object_listing(test: Test) -> Row:
    """Check Depth 1 of an object, tree/b, which is no collection.

    A PROPFIND at every origin and cache must answer 207, listing the
    object alone, with its size, as Depth 0 would (RFC 4918, section
    9.1); or, where the server may not list (may_list()), a refusal.
    Where /protected-a has Listings, `pelican object ls -l --json` of it
    must list it alone.
    """
    caps = test.caps
    token = test.token
    path = test.path_in("protected-a", "tree/b")
    problems: list[str] = []
    for svc, url, role in servers(test):
        target = f"{url}{path}"
        if not may_list(caps, "1", role):
            if why := test.refusal(target, "1", token):
                problems.append(f"{svc}: {why}")
            continue
        _, got, why = webdav.listing(target, "1", path, token)
        if got is None:
            problems.append(f"{svc}: {why}")
        elif set(got) != {""}:
            problems.append(f"{svc}: Depth 1 of the object listed {sorted(got)}")
        elif got[""].collection or got[""].size != TREE["b"]:
            problems.append(f"{svc}: Depth 1 of the object: not the {TREE['b']}-byte object")
    note = f"{len(servers(test))} server(s)"
    if "Listings" in caps:
        listed, why = test.ls("tree/b")
        if listed is None:
            problems.append(f"`pelican object ls` of the object: {why}")
        elif listed != {"b": (False, TREE["b"])}:
            problems.append(f"`pelican object ls` of the object listed {listed}")
        note += ", and the client"
    return outcome(problems, note)


RUN = {
    "origin-depth-0": origin_depth_0,
    "origin-depth-1": origin_depth_1,
    "origin-infinity": origin_infinity,
    "director": director,
    "cache-depth-0": cache_depth_0,
    "cache-depth-1": cache_depth_1,
    "cache-infinity": cache_infinity,
    "cache-body": cache_body,
    "protected-a-no-token": protected_a_no_token,
    "protected-a-scope": protected_a_scope,
    "public-no-token": public_no_token,
    "new-object": new_object,
    "collection-get": collection_get,
    "html-links": html_links,
    "plain-object-listing": plain_object_listing,
}


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None if there is something.

    There is nothing without /protected-a.
    """
    return suite.unexported(fed)


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Put the tree in place, run the selected scenarios, and clean up.

    Cleaning up removes the run's objects.
    """
    fed = session.fed
    stores.require_dirs(fed.origins, fed.exports, "data/listings")
    test = Test(session)
    # Earlier runs' objects come out first.
    test.clean("before the run")
    print(f"Listing test against {fed.url}, objects under /protected-a/{test.base}/\n")
    try:
        errors = test.seed()
        if errors:
            die(f"could not put the tree in place: {errors[0]}")
        for name in selected:
            why = suite.unexported(fed, "public") if name == "public-no-token" else None
            if why:
                results.add(name, SKIP, why)
                continue
            if name == "director" and not fed.directors:
                results.add(name, SKIP, "a standalone origin has no director")
                continue
            if name.startswith("cache-") and not test.caches:
                results.add(name, SKIP, "this shape has no federated cache")
                continue
            results.add(name, *suite.attempt(partial(RUN[name], test), (stores.Unreadable,)))
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        test.clean("after the run")
