"""A site-local cache (`-p topo-site-local-cache`).

It is cache-2, which the director does not know of, and so serves only
clients that name it (Client.PreferredCaches). It fetches objects
through a federated cache, presenting the client's token, as a client
would, and asks an origin for listings.

Objects go under /<namespace>/data/sitelocal/<run>/ in every origin's
store, in the storage of the namespace they are read in
(stores.put_in_place()), each read once, so that it is cold wherever it
is read. The tokens that the clients and requests present are of the
session's profile; the objects are put in place, and the federated
caches asked what they hold, with WLCG tokens either way.

See docs/tests/site-local-cache.md.
"""

import os

from testlib import common, credentials, stores, suite, transfers, web, webdav
from testlib.common import die, first
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import Session, last_line
from testlib.suite import Row

SCENARIOS = {
    "get-public": "`pelican object get --cache` of /public: served by the site-local cache",
    "get-protected-a": "the same in /protected-a, with the server's token",
    "plugin-get-public": "stash_plugin, preferring the site-local cache, of /public",
    "plugin-get-protected-a": "the same in /protected-a, with the server's token",
    "get-plus": "`--cache <site-local>,+`: the site-local cache still serves it",
    "refused": "a token signed by a key no server knows: refused, and nothing written",
    "upstream": "a cold read through the site-local cache fills a federated V2 cache",
    "list": "Depth 1 through the site-local cache: listed where origins list, else refused",
}

# The scenarios that present no token (see suites/__init__.py).
ONE_PROFILE = ("get-public", "plugin-get-public")

# What every object is: its size.
SIZE = 10000
# The collection that `list` lists, and what it holds.
LISTED = {"a": 1000, "b": 2000}


def skip(fed: common.Federation) -> str | None:
    """Return why there is nothing to test, or None if there is something.

    There is nothing without a site-local cache, or without /protected-a.
    """
    if not any(c.site_local for c in fed.caches):
        return "there is no site-local cache ('-p topo-site-local-cache')"
    return suite.unexported(fed)


class Test(suite.Test):
    """The site-local cache, and the run's collection in each namespace.

    Attributes:
        cache: The site-local cache.
        origins: The origins, each store once.
    """

    def __init__(self, session: Session):
        super().__init__(session, "sitelocal")
        self.cache = next(c for c in self.fed.caches if c.site_local)
        self.origins = stores.unique(self.fed.origins)

    def seed(self, namespace: str, rel: str, data: bytes) -> None:
        """Put data at rel in namespace's storage at every origin.

        On a pstore origin, /public's storage is /protected-a's.
        """
        errors = stores.put_in_place(self.session, self.origins, namespace, rel, data)
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")

    def token_file_of(self, namespace: str) -> str | None:
        """Return the file of namespace's `server` token.

        Returns:
            The file, or None where the namespace takes no token.
        """
        return self.session.token_file(namespace) if namespace in credentials.PROTECTED else None


def client_get(test: Test, namespace: str, name: str, caches: str) -> Row:
    """Run `pelican object get --cache caches` of object name in namespace.

    The object is new, and goes to a file, so that the client asks no
    server before it transfers. The site-local cache alone must serve it.
    """
    data = os.urandom(SIZE)
    rel = test.rel(name)
    test.seed(namespace, rel, data)
    target = os.path.join(test.tmp, name)
    stats = f"{target}.json"
    token_file = test.token_file_of(namespace)
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        caches,
        "--transfer-stats",
        stats,
        *(["--token", token_file] if token_file else []),
        test.pelican_url(rel, namespace=namespace),
        target,
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    if not common.file_holds(target, data):
        return FAIL, "the file is not the object"
    hosts = transfers.stats_endpoints(transfers.read_stats(stats))
    if hosts != [test.cache.svc]:
        return FAIL, f"served by {hosts or 'no host recorded'}, not {test.cache.svc}"
    return PASS, f"served by {test.cache.svc}"


def get(test: Test, namespace: str) -> Row:
    """Run client_get(), preferring the site-local cache alone."""
    return client_get(test, namespace, f"get-{namespace}", test.cache.url)


def plugin_get(test: Test, namespace: str) -> Row:
    """Run stash_plugin's download of a new object.

    The site-local cache is its preferred cache, and the server's token
    is in its credentials directory where the namespace takes one.
    """
    name = f"plugin-get-{namespace}"
    data = os.urandom(SIZE)
    rel = test.rel(name)
    test.seed(namespace, rel, data)
    token_file = test.token_file_of(namespace)
    status, out, err, ad = test.session.plugin_run(
        f"sitelocal/{name}",
        [(test.pelican_url(rel, namespace=namespace), name)],
        creds={"server.use": token_file} if token_file else {},
        cache=test.cache.url,
        cwd=test.tmp,
    )
    if not transfers.plugin_passed(ad):
        return FAIL, f"exit {status}: {last_line(err or out or ad)}"
    if not common.file_holds(os.path.join(test.tmp, name), data):
        return FAIL, "the file is not the object"
    hosts = transfers.plugin_endpoints(ad)
    host = hosts[-1] if hosts else ""
    if host != test.cache.svc:
        return FAIL, f"served by {host or 'no host recorded'}, not {test.cache.svc}"
    return PASS, f"served by {test.cache.svc}"


def get_plus(test: Test) -> Row:
    """Run client_get() with `--cache <site-local>,+`.

    The client may fall back to the director's caches, but need not.
    """
    return client_get(test, "protected-a", "get-plus", f"{test.cache.url},+")


def refused(test: Test) -> Row:
    """Check a token that no server accepts, through the site-local cache.

    The cache must pass the refusal on, and the client write nothing.
    """
    name = "refused"
    rel = test.rel(name)
    test.seed("protected-a", rel, os.urandom(SIZE))
    token = test.session.path("tokens/sitelocal-unknown")
    issuer = test.fed.issuer_of("protected-a")
    credentials.mint(
        test.fed,
        token,
        "/protected-a/",
        credentials.UNKNOWN_KEY,
        issuer,
        credentials.R,
        profile=test.session.profile,
    )
    target = os.path.join(test.tmp, name)
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        test.cache.url,
        "--token",
        token,
        test.pelican_url(rel),
        target,
    )
    if code == 0:
        return FAIL, "succeeded, but should have been refused"
    why = transfers.refused(out + err)
    if why is None:
        return FAIL, f"failed, but not by refusal: {last_line(err or out)}"
    if common.sha256(target) is not None:
        return FAIL, "refused, but the file was written"
    return PASS, f"refused ({why})"


def upstream(test: Test) -> Row:
    """Check that the site-local cache fetches through a federated V2 cache.

    A cold read through it must leave the object in a federated V2
    cache, which held none of it before. An XRootD cache cannot be asked
    whether it holds an object without fetching it.
    """
    feds = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not feds:
        return INCONCLUSIVE, "no federated V2 cache to ask what it holds"
    rel = test.rel("upstream")
    data = os.urandom(SIZE)
    test.seed("protected-a", rel, data)
    path = test.path(rel)
    before = {c.svc: test.cached(c, rel) for c in feds}
    if any(held is not False for held in before.values()):
        return FAIL, f"before the read, only-if-cached got {before}, not 504 everywhere"
    why = web.served(web.request("GET", f"{test.cache.url}{path}", token=test.token), data)
    if why:
        return FAIL, f"GET through {test.cache.svc}: {why}"
    after = {c.svc: test.cached(c, rel) for c in feds}
    holders = [svc for svc, held in after.items() if held]
    if not holders:
        return FAIL, f"after the read, only-if-cached got {after}: no federated cache holds it"
    return PASS, f"{', '.join(holders)} holds it"


def listing(test: Test) -> Row:
    """Check Depth 1 of a collection through the site-local cache.

    The cache asks an origin. The collection must be listed where
    /protected-a may be listed straight from one (Listings and
    DirectReads), and the listing refused (401, 403, or 405) otherwise.
    """
    for name, size in LISTED.items():
        test.seed("protected-a", test.rel("listed", name), os.urandom(size))
    path = test.path(test.rel("listed"))
    answer, got, why = webdav.listing(f"{test.cache.url}{path}/", "1", path, test.token)
    lacking = sorted({"Listings", "DirectReads"} - test.caps)
    if lacking:
        if answer.status in (401, 403, 405):
            return PASS, f"refused ({answer.describe()}): no {' or '.join(lacking)}"
        return FAIL, f"{answer.describe()}, not a refusal (401, 403, or 405)"
    if got is None:
        return FAIL, why
    wrong = webdav.problems(dict(LISTED), got)
    if wrong:
        return FAIL, "; ".join(wrong)
    return PASS, f"{len(LISTED)} objects"


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    stores.require_dirs(fed.origins, fed.exports, "data/sitelocal")
    test = Test(session)
    print(
        f"Site-local cache {test.cache.svc} ({test.cache.url}),"
        + f" objects under /protected-a/{test.base}/\n"
    )
    scenarios = {
        "get-public": lambda: get(test, "public"),
        "get-protected-a": lambda: get(test, "protected-a"),
        "plugin-get-public": lambda: plugin_get(test, "public"),
        "plugin-get-protected-a": lambda: plugin_get(test, "protected-a"),
        "get-plus": lambda: get_plus(test),
        "refused": lambda: refused(test),
        "upstream": lambda: upstream(test),
        "list": lambda: listing(test),
    }
    try:
        for name in selected:
            why = suite.unexported(fed, "public") if name.endswith("-public") else None
            if why:
                results.add(name, SKIP, why)
                continue
            results.add(name, *suite.attempt(scenarios[name]))
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        test.clean("after the run")
