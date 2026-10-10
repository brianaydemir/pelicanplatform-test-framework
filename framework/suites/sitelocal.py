"""A site-local cache (`-p topo-site-local-cache`): cache-2, which the
director does not know of, and so serves only clients that name it
(Client.PreferredCaches). It fetches objects through a federated cache,
presenting the client's token, as a client would, and asks an origin
for listings.

Objects go under /<namespace>/data/sitelocal/<run>/ in every origin's
store, in the storage of the namespace they are read in (see
stores.seed()), each read once, so that it is cold wherever it is read:

  get-<ns>         `pelican object get --cache <cache-2>`, with the
                   server's token where the namespace takes one: the
                   bytes, served by cache-2 alone
  plugin-get-<ns>  stash_plugin, with cache-2 as its preferred cache
  get-plus         `--cache <cache-2>,+`: cache-2 still serves it
  refused          a token signed by a key no server knows: refused,
                   and nothing written
  upstream         a cold read through cache-2 leaves the object in a
                   federated V2 cache, which held none of it before
                   (Cache-Control: only-if-cached)
  list             Depth 1 of a collection through cache-2, where
                   /protected-a may be listed straight from an origin,
                   and otherwise a refusal
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import traceback
from typing import Optional

from testlib import common, credentials, stores, transfers, web, webdav
from testlib.common import die, first
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP
from testlib.session import CLIENT_TIMEOUT, Session, last_line

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

# What every object is: its size.
SIZE = 10000
# A key that no server knows (see credentials.keys()).
UNKNOWN_KEY = "test-keys/unknown.pem"
# The collection that `list` lists, and what it holds.
LISTED = {"a": 1000, "b": 2000}


def skip(fed: common.Federation) -> Optional[str]:
    """Why there is nothing to test: no site-local cache."""
    if not any(c.site_local for c in fed.caches):
        return "there is no site-local cache ('-p topo-site-local-cache')"
    if "protected-a" not in fed.exports:
        return "this shape does not export /protected-a"
    return None


class Test:
    """The site-local cache, the run's objects, and the tokens."""

    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.cache = next(c for c in self.fed.caches if c.site_local)
        self.origins = stores.unique(self.fed.origins)
        self.base = f"data/sitelocal/{session.run}"
        self.tmp = session.path("sitelocal")
        os.makedirs(self.tmp)
        self.token_files = {
            ns: session.token_file(ns) for ns in credentials.PROTECTED if ns in self.fed.exports
        }
        self.objects: dict[str, bytes] = {}

    def put(self, namespace: str, name: str, data: bytes) -> None:
        """Put object name, holding data, in namespace's storage at every
        origin: on a pstore origin, /public's is /protected-a's."""
        target = self.fed.storage_namespace(namespace)
        token = self.session.token(target)
        caps = self.fed.exports[target]
        rel = f"{self.base}/{name}"
        errors: list[str] = []
        for origin in self.origins:
            errors += stores.make_dirs([origin], target, rel.rsplit("/", 1)[0], token)
            if not errors:
                error = stores.seed(origin, target, rel, data, token, caps)
                errors += [error] if error else []
        if errors:
            die(f"could not put {rel} in place: {first(errors)}")
        self.objects[name] = data

    def url(self, namespace: str, name: str) -> str:
        """The federation URL of object name in namespace."""
        return f"{self.fed.url}/{namespace}/{self.base}/{name}"

    def token_args(self, namespace: str) -> list[str]:
        """The client's arguments for the server's token, where the
        namespace takes one."""
        if namespace not in self.token_files:
            return []
        return ["--token", self.token_files[namespace]]


def served_by_stats(path: str) -> list[str]:
    """The hosts in a `--transfer-stats` file (see
    transfers.stats_endpoints()), or none if it is unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return transfers.stats_endpoints(json.load(f))
    except (FileNotFoundError, ValueError):
        return []


def client_get(test: Test, namespace: str, name: str, caches: str) -> tuple[str, str]:
    """`pelican object get --cache caches` of object name in namespace, to
    a file, so that the client asks no server before it transfers."""
    data = os.urandom(SIZE)
    test.put(namespace, name, data)
    target = os.path.join(test.tmp, name)
    stats = f"{target}.json"
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        caches,
        "--transfer-stats",
        stats,
        *test.token_args(namespace),
        test.url(namespace, name),
        target,
    )
    if code != 0:
        return FAIL, f"exit {code}: {last_line(err or out)}"
    if common.sha256(target) != common.digest(data):
        return FAIL, "the file is not the object"
    hosts = served_by_stats(stats)
    if hosts != [test.cache.svc]:
        return FAIL, f"served by {hosts or 'no host recorded'}, not {test.cache.svc}"
    return PASS, f"served by {test.cache.svc}"


def get(test: Test, namespace: str) -> tuple[str, str]:
    """client_get() of a new object, preferring the site-local cache
    alone."""
    return client_get(test, namespace, f"get-{namespace}", test.cache.url)


def plugin_get(test: Test, namespace: str) -> tuple[str, str]:
    """stash_plugin's download of a new object, with the site-local cache
    as its preferred cache, and the server's token in its credentials
    directory where the namespace takes one."""
    name = f"plugin-get-{namespace}"
    data = os.urandom(SIZE)
    test.put(namespace, name, data)
    creds = os.path.join(test.tmp, f"{name}.creds")
    os.makedirs(creds)
    if namespace in test.token_files:
        shutil.copyfile(test.token_files[namespace], os.path.join(creds, "server.use"))
    infile, outfile = os.path.join(test.tmp, f"{name}.in"), os.path.join(test.tmp, f"{name}.ad")
    with open(infile, "w", encoding="utf-8") as f:
        f.write(f'[ Url="{test.url(namespace, name)}"; LocalFileName="{name}" ]\n')
    env = dict(test.session.env)
    env["_CONDOR_CREDS"] = creds
    env["PELICAN_CLIENT_PREFERREDCACHES"] = test.cache.url
    try:
        done = subprocess.run(  # nosec B603
            [test.session.plugin, "-infile", infile, "-outfile", outfile],
            cwd=test.tmp,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLIENT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {CLIENT_TIMEOUT:.0f}s"
    try:
        with open(outfile, encoding="utf-8") as f:
            ad = f.read()
    except FileNotFoundError:
        ad = ""
    if not transfers.plugin_passed(ad):
        return FAIL, f"exit {done.returncode}: {last_line(done.stderr or done.stdout or ad)}"
    if common.sha256(os.path.join(test.tmp, name)) != common.digest(data):
        return FAIL, "the file is not the object"
    endpoints = re.findall(r'Endpoint(\d+) = "([^":]*)', ad)
    host = max(endpoints, key=lambda e: int(e[0]))[1] if endpoints else ""
    if host != test.cache.svc:
        return FAIL, f"served by {host or 'no host recorded'}, not {test.cache.svc}"
    return PASS, f"served by {test.cache.svc}"


def get_plus(test: Test) -> tuple[str, str]:
    """`--cache <site-local>,+`: the client may fall back to the
    director's caches, but need not."""
    return client_get(test, "protected-a", "get-plus", f"{test.cache.url},+")


def refused(test: Test) -> tuple[str, str]:
    """A token that no server accepts, through the site-local cache, which
    must pass the refusal on, and write nothing."""
    name = "refused"
    test.put("protected-a", name, os.urandom(SIZE))
    token = test.session.path("tokens/sitelocal-unknown")
    issuer = test.fed.issuer_of("protected-a")
    credentials.mint(
        test.fed,
        token,
        "/protected-a/",
        UNKNOWN_KEY,
        issuer,
        "/",
        credentials.LONG,
        credentials.R,
    )
    target = os.path.join(test.tmp, name)
    code, out, err = test.session.pelican_cmd(
        "object",
        "get",
        "--cache",
        test.cache.url,
        "--token",
        token,
        test.url("protected-a", name),
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


def cached_upstream(cache: common.Cache, path: str, token: str) -> Optional[bool]:
    """Whether a federated V2 cache holds path (a HEAD with
    Cache-Control: only-if-cached answers 200, or 504 if not); None if it
    answers otherwise."""
    answer = web.request(
        "HEAD", f"{cache.url}{path}", token=token, headers={"Cache-Control": "only-if-cached"}
    )
    return {200: True, 504: False}.get(answer.status or 0)


def upstream(test: Test) -> tuple[str, str]:
    """A cold read through the site-local cache leaves the object in a
    federated V2 cache, which held none of it before: the site-local
    cache fetched it through one. An XRootD cache cannot be asked
    whether it holds an object without fetching it."""
    feds = [c for c in test.fed.federated_caches if c.kind == "v2"]
    if not feds:
        return INCONCLUSIVE, "no federated V2 cache to ask what it holds"
    name = "upstream"
    test.put("protected-a", name, os.urandom(SIZE))
    path = f"/protected-a/{test.base}/{name}"
    token = test.session.token("protected-a")
    before = {c.svc: cached_upstream(c, path, token) for c in feds}
    if any(held is not False for held in before.values()):
        return FAIL, f"before the read, only-if-cached got {before}, not 504 everywhere"
    answer = web.request("GET", f"{test.cache.url}{path}", token=token)
    if answer.status != 200 or answer.body != test.objects[name]:
        return FAIL, f"GET through {test.cache.svc}: {answer.describe()}"
    after = {c.svc: cached_upstream(c, path, token) for c in feds}
    holders = [svc for svc, held in after.items() if held]
    if not holders:
        return FAIL, f"after the read, only-if-cached got {after}: no federated cache holds it"
    return PASS, f"{', '.join(holders)} holds it"


def listing(test: Test) -> tuple[str, str]:
    """Depth 1 of a collection through the site-local cache, which asks an
    origin: listed, where /protected-a may be listed straight from one
    (Listings and DirectReads); refused (401, 403, or 405) otherwise."""
    for name, size in LISTED.items():
        test.put("protected-a", f"listed/{name}", os.urandom(size))
    path = f"/protected-a/{test.base}/listed"
    token = test.session.token("protected-a")
    answer = webdav.propfind(f"{test.cache.url}{path}/", "1", token)
    lacking = sorted({"Listings", "DirectReads"} - test.fed.exports["protected-a"])
    if lacking:
        if answer.status in (401, 403, 405):
            return PASS, f"refused ({answer.describe()}): no {' or '.join(lacking)}"
        return FAIL, f"{answer.describe()}, not a refusal (401, 403, or 405)"
    if answer.status != 207:
        return FAIL, f"{answer.describe()}, not 207"
    try:
        got = webdav.relative(webdav.parse(answer.body), path)
    except ValueError as e:
        return FAIL, str(e)
    wrong = webdav.problems(dict(LISTED), got)
    if wrong:
        return FAIL, "; ".join(wrong)
    return PASS, f"{len(LISTED)} objects"


def run(session: Session, selected: list[str], results: common.Results) -> None:
    """Run the selected scenarios, and remove the run's objects."""
    fed = session.fed
    for origin in stores.unique(fed.origins):
        for ns in fed.exports:
            directory = f"{origin.store_of(ns)}/data/sitelocal"
            if not origin.pstore and not os.path.isdir(directory):
                die(f"framework/var/{directory} is missing; run ./fed.sh init")
    tokens = session.tokens()
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
            if name.endswith("-public") and "public" not in fed.exports:
                results.add(name, SKIP, "this shape does not export /public")
                continue
            try:
                status, note = scenarios[name]()
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A bug, or a server answering what the suite can't read:
                # the scenario fails, and the rest still run.
                traceback.print_exc()
                status, note = FAIL, f"{type(e).__name__}: {e}"
            results.add(name, status, note)
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        errors = stores.empty_tree_everywhere(
            fed.origins, fed.exports, "data/sitelocal", tokens
        )
        if errors:
            common.warn(f"could not remove the run's objects: {first(errors)}")
