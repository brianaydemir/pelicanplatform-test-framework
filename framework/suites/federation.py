"""The federation itself: that it serves at all, and that its servers
know of each other. test.py runs `ready` and `caches` before any other
suite, whatever it is asked for, since nothing else can pass without
them; and it stops if `ready` can't pass.

The suite uploads an object straight to every origin, under
/<namespace>/data/federation/<run>/, and reads it back:

  ready      through the federation (the director and a cache), with
             `pelican object get`: 20 tries, 15 seconds apart, since
             servers just started take a while to find each other
  caches     straight from each cache, with the server's token: a new
             cache refuses a namespace's tokens (403) until it has learned
             the namespace's issuers from the director
  directors  each director must name every cache for it, and every
             origin (or, where the namespace takes no direct clients, no
             origin, answering 405), within 60 seconds (not in the tiny
             topology)
"""

import os
import time
from typing import List, Optional, Tuple

from testlib import common, credentials, director, stores, web
from testlib.common import die
from testlib.report import FAIL, PASS, SKIP
from testlib.session import Session, last_line

SCENARIOS = {
    "ready":     "an object uploaded to every origin comes back through the federation",
    "caches":    "each cache serves it with the server's token (a new one refuses at first)",
    "directors": "each director names every cache for it, and every origin that takes direct reads",
}

# The scenarios that run whatever test.py is asked for.
REQUIRED = ("ready", "caches")

# How long `ready` and `caches` wait: tries, and seconds between them.
TRIES, INTERVAL = 20, 15
# How long `directors` waits, in seconds.
DIRECTORS_WAIT = 60


def skip(fed: common.Federation) -> Optional[str]:
    return None


class Probe:
    def __init__(self, session: Session):
        self.session = session
        self.fed = session.fed
        self.base = f"data/federation/{session.run}"
        self.rel = f"{self.base}/probe"
        self.data = os.urandom(4096)
        # The protected namespace that caches and directors are asked about,
        # and the namespace `ready` reads: /public, with no token, where
        # there is one. /public reads the first protected namespace's
        # storage.
        self.protected = next(ns for ns in credentials.PROTECTED if ns in self.fed.exports)
        self.read_ns = session.namespaces[0]
        self.uploaded = False

    def upload(self) -> Optional[str]:
        """Put the object at every origin, once (see stores.seed()); why
        that failed, or None."""
        if self.uploaded:
            return None
        for origin in stores.unique(self.fed.origins):
            for ns in stores.upload_namespaces(origin, self.fed.exports):
                token = self.session.token(ns)
                errors = stores.make_dirs([origin], ns, self.base, token)
                if errors:
                    return errors[0]
                error = stores.seed(origin, ns, self.rel, self.data, token, self.fed.exports[ns])
                if error:
                    return error
        self.uploaded = True
        return None

    def get(self) -> Optional[str]:
        """`pelican object get` of the object through the federation; what
        went wrong, or None."""
        target = self.session.path("federation-probe")
        if os.path.exists(target):
            os.remove(target)
        token = [] if self.read_ns == "public" else ["--token",
                                                     self.session.token_file(self.read_ns)]
        code, out, err = self.session.pelican_cmd(
            "object", "get", *token, f"{self.fed.url}/{self.read_ns}/{self.rel}", target)
        if code != 0:
            return f"`pelican object get`: exit {code}: {last_line(err or out)}"
        if common.sha256(target) != common.digest(self.data):
            return "`pelican object get` succeeded, but the file is not the object"
        return None


def ready(probe: Probe) -> Tuple[str, str]:
    """Dies if the federation never serves: nothing after it could pass."""
    why: Optional[str] = None
    for n in range(1, TRIES + 1):
        why = probe.upload() or probe.get()
        if why is None:
            return PASS, f"{probe.read_ns}, after {n} tries" if n > 1 else probe.read_ns
        if n == 1:
            print(f"Waiting for the federation to serve {probe.fed.url}/{probe.read_ns}/"
                  f"{probe.rel} ...")
        if n < TRIES:
            time.sleep(INTERVAL)
    die(f"gave up waiting for the federation after {TRIES} tries, {INTERVAL}s apart ({why})."
        " Is it up (./fed.sh status)? Was it initialized (./fed.sh init) for this shape?")


def caches(probe: Probe) -> Tuple[str, str]:
    token = probe.session.token(probe.protected)
    problems = []
    for cache in probe.fed.caches:
        url = f"{cache.url}/{probe.protected}/{probe.rel}"
        answer = web.request("GET", url, token=token)
        for n in range(1, TRIES):
            if answer.status != 403:
                break
            if n == 1:
                print(f"Waiting for {cache.svc} to accept the server's token ...")
            time.sleep(INTERVAL)
            answer = web.request("GET", url, token=token)
        if answer.status != 200:
            problems.append(f"{cache.svc}: {answer.describe()}")
        elif answer.body != probe.data:
            problems.append(f"{cache.svc}: the body is not the object")
    if problems:
        return FAIL, common.first(problems)
    return PASS, f"{len(probe.fed.caches)} cache(s)"


def directors(probe: Probe) -> Tuple[str, str]:
    """Where each director would send a GET of the object, for a cache
    (`object`) and for an origin: both lists must name every one. Where
    the namespace takes no direct clients, the director must find no
    origin for a direct read (405), and name none."""
    fed = probe.fed
    if fed.tiny:
        return SKIP, "the tiny topology's director, cache, and origin are one server"
    path = f"/{probe.protected}/{probe.rel}"
    origin_hosts = [director.host(o.url) for o in fed.origins]
    direct = "DirectReads" in fed.exports[probe.protected]
    wanted = {"object": [director.host(c.url) for c in fed.caches],
              "origin": origin_hosts if direct else []}

    def unnamed() -> List[str]:
        found = []
        for url in fed.directors:
            for route, hosts in wanted.items():
                answer = director.ask(url, route, path)
                found += [f"{director.host(url)} ({route}) did not name {host}: {answer.describe()}"
                          for host in hosts if not answer.names(host)]
                if route == "origin" and not direct:
                    named = [h for h in origin_hosts if answer.names(h)]
                    if answer.status != 405 or named:
                        found.append(f"{director.host(url)} ({route}) should find no origin"
                                     f" (405): {answer.describe()}")
        return found

    missing: List[str] = []

    def all_named() -> bool:
        missing[:] = unnamed()
        return not missing

    if not common.wait_for(all_named, DIRECTORS_WAIT, interval=5):
        return FAIL, f"after {DIRECTORS_WAIT}s, {common.first(missing)}"
    origins = f"{len(fed.origins)} origin(s)" if direct else "no origin (no direct clients)"
    return PASS, f"{len(fed.directors)} director(s): {origins}, {len(fed.caches)} cache(s)"


RUN = {"ready": ready, "caches": caches, "directors": directors}


def run(session: Session, selected: List[str], results: common.Results) -> None:
    disk_stores = [o.store for o in stores.unique(session.fed.origins) if not o.pstore]
    for store in disk_stores:
        if not os.path.isdir(f"{store}/data/federation"):
            die(f"framework/var/{store}/data/federation is missing; run ./fed.sh init")
        # Earlier runs' objects come out first.
        common.empty_dir(f"{store}/data/federation")
    probe = Probe(session)
    print(f"Probing {session.fed.url} with /<namespace>/{probe.rel}\n")
    try:
        for name in selected:
            results.add(name, *RUN[name](probe))
    finally:
        # The run's collections are made from the dev container, so on a
        # Linux host they are root's, and beyond init-data.py's reach.
        for store in disk_stores:
            common.empty_dir(f"{store}/data/federation")
