"""What the transfers suite runs, and what it expects of each run: the
one place that says so. The suite builds its batches from it, runs them,
and judges the results with it; framework/init-data.py writes the
objects that the gets fetch.

A scenario is a set of transfers through one client, in one namespace,
with one credential. Each runs through `pelican object get` or `put`,
and again through stash_plugin as plugin-<scenario>, with the same
batches. Each get also has a direct-<scenario> twin, which asks the
director for an origin instead of a cache. A batch is one client
invocation, of as many objects as the next of LOAD's sizes (see
batch_count()).

What a scenario should do depends on the namespace's capabilities in
the shape (see outcome()). /public, which has no issuer, gets only the
credentials that apply to it (see credentials.applies()). A shape that
exports one namespace has batches only for its scenarios (see
exported()).
"""

import dataclasses
import enum
import re
from dataclasses import dataclass
from typing import (AbstractSet, Callable, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Set, Tuple)

from . import credentials
from .credentials import ALLOW, CREDENTIALS, NAMESPACES, Credential

# Each namespace's capabilities, by name (see common.Federation.exports).
Exports = Mapping[str, AbstractSet[str]]


class Outcome(enum.Enum):
    PASS = "pass"              # every transfer succeeds, byte for byte
    NOT_FOUND = "not found"    # every transfer fails: there is no such object
    REFUSED = "refused"        # every transfer is refused, and nothing moves


@dataclass(frozen=True)
class Scenario:
    name: str
    client: str       # "pelican" or "plugin"
    route: str        # a get's: "cache", or "direct" (to an origin); a put's: "origin"
    op: str           # "get" or "put"
    namespace: str    # one of NAMESPACES
    credential: Optional[Credential]  # None: no token, and no refusal expected
    missing: bool     # the objects never exist
    collection: str   # where its objects are, e.g. /public/data/
    stem: str         # a get's objects are <stem>.<n>
    description: str

    @property
    def store_dir(self) -> str:
        """Its collection in an origin's store, e.g.
        data/put/put-protected-a-jwks."""
        return rel_path(self.collection).rstrip("/")

    @property
    def direct(self) -> bool:
        return self.route == "direct"


def exported(scenario: Scenario, exports: Exports) -> bool:
    """Whether the federation exports scenario's namespace: not every
    shape exports every one (ORIGIN_NAMESPACES in fed.sh)."""
    return scenario.namespace in exports


def outcome(scenario: Scenario, exports: Exports) -> Outcome:
    """What scenario should do in a federation with these exports."""
    if scenario.missing:
        return Outcome.NOT_FOUND
    if scenario.credential is None:
        return Outcome.PASS
    verdict = credentials.expected(scenario.credential, exports[scenario.namespace],
                                   scenario.op)
    return Outcome.PASS if verdict == ALLOW else Outcome.REFUSED


def _gets(route: str) -> List[Scenario]:
    prefix = "direct-" if route == "direct" else ""
    how = "from an origin" if route == "direct" else "through a cache"
    scenarios = [
        Scenario(f"{prefix}exist", "pelican", route, "get", "public", None, False,
                 "/public/data/", "0", f"get /public/data/0.<n> {how}, with no token"),
        Scenario(f"{prefix}dne", "pelican", route, "get", "public", None, True,
                 "/public/data/", "9", f"get /public/data/9.<n> {how}, which never exists"),
    ]
    for ns in NAMESPACES:
        for cred in CREDENTIALS:
            # A /public get with no token is `exist`.
            if not credentials.applies(cred, ns) or (ns == "public" and cred.key is None):
                continue
            scenarios.append(Scenario(
                f"{prefix}get-{ns}-{cred.name}", "pelican", route, "get", ns, cred, False,
                f"/{ns}/data/", "0", f"get /{ns}/data/0.<n> {how}: {cred.description}"))
    return scenarios


def _puts() -> List[Scenario]:
    scenarios = []
    for ns in NAMESPACES:
        for cred in CREDENTIALS:
            if not credentials.applies(cred, ns):
                continue
            # Each put scenario writes a collection of its own.
            name = f"put-{ns}-{cred.name}"
            collection = f"/{ns}/data/put/{name}/"
            scenarios.append(Scenario(
                name, "pelican", "origin", "put", ns, cred, False, collection, "",
                f"put to {collection}: {cred.description}"))
    return scenarios


def plugin_twin(scenario: Scenario) -> Scenario:
    """The same scenario through stash_plugin. Only its uploads go
    elsewhere, so that the two clients never write the same object."""
    name = f"plugin-{scenario.name}"
    collection = scenario.collection
    description = scenario.description
    if scenario.op == "put":
        collection = f"/{scenario.namespace}/data/put/{name}/"
        description = description.replace(scenario.collection, collection)
    return dataclasses.replace(scenario, name=name, client="plugin", collection=collection,
                               description=description)


PELICAN_SCENARIOS = tuple(_gets("cache") + _gets("direct") + _puts())
PLUGIN_SCENARIOS = tuple(plugin_twin(s) for s in PELICAN_SCENARIOS)
SCENARIOS = PELICAN_SCENARIOS + PLUGIN_SCENARIOS
BY_NAME = {s.name: s for s in SCENARIOS}


#---------------------------------------------------------------------------
# Batches.
#
# On disk, a batch is in stash_plugin's input format, one ad per object:
#
#   [ Url="<federation><collection><name>"; LocalFileName="<name>" ]
#
# LocalFileName is always the URL's last component, because that is
# where `pelican object get` puts an object and what `pelican object put`
# names an upload after. So a batch is its URLs, and LocalFileName is
# derived from them (local_name), never chosen on its own. A direct get
# through the plugin asks for it with ?directread on each URL, since the
# plugin has no flag for it; `pelican object get` is given --direct.

@dataclass(frozen=True)
class Load:
    objects: int                 # objects per origin, named <origin>.<n>
    batches: int                 # batches for a scenario whose token matters
    refused_batches: int         # batches for the rest
    sizes: Tuple[int, ...]       # objects per batch, taken in turn


# A basic check, not a stress test: `exist`, `dne`, and each scenario
# that a token should let through move one object, then two (a put of two
# goes to a collection rather than an object's URL); the rest move one.
LOAD = Load(objects=16, batches=2, refused_batches=1, sizes=(1, 2))
# How many batches run at once.
CONCURRENCY = 16


@dataclass(frozen=True)
class Batch:
    scenario: Scenario
    index: int
    urls: Tuple[str, ...]

    @property
    def id(self) -> str:
        """Its path under input/, output/, log/, and files/."""
        return f"{self.scenario.name}/{self.index}"

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(local_name(url) for url in self.urls)

    @property
    def destination(self) -> str:
        """Where a `pelican object put` of the batch goes: the object's URL,
        or the collection of several."""
        if len(self.urls) == 1:
            return self.urls[0]
        return self.urls[0].rsplit("/", 1)[0] + "/"


DIRECT_QUERY = "?directread"


def local_name(url: str) -> str:
    return url.split("?", 1)[0].rsplit("/", 1)[1]


def rel_path(url: str) -> str:
    """Where an object is in an origin's store: its path without the
    federation, the namespace, or a query, e.g. data/0.17."""
    path = re.sub(r"^[a-z]+://[^/]*", "", url.split("?", 1)[0])
    return path.split("/", 2)[2]


def batch_url(federation: str, scenario: Scenario, name: str) -> str:
    query = DIRECT_QUERY if scenario.direct and scenario.client == "plugin" else ""
    return f"{federation}{scenario.collection}{name}{query}"


def format_batch(batch: Batch) -> str:
    return "".join(f'[ Url="{url}"; LocalFileName="{local_name(url)}" ]\n'
                   for url in batch.urls)


def parse_batch(scenario: Scenario, index: int, text: str) -> Batch:
    return Batch(scenario, index, tuple(re.findall(r'Url="([^"]*)"', text)))


def batch_count(scenario: Scenario, exports: Exports, load: Load) -> int:
    """load.batches for `exist`, `dne`, and every scenario that a token
    should let through; load.refused_batches for the rest: refusals, and
    scenarios whose token can't matter (e.g. reads of /public)."""
    if scenario.credential is None:
        return load.batches
    if (outcome(scenario, exports) is Outcome.PASS
            and credentials.token_decides(exports[scenario.namespace], scenario.op)):
        return load.batches
    return load.refused_batches


def make_batches(federation: str, exports: Exports, load: Load,
                 pick: Callable[[int], int]) -> Dict[str, List[Batch]]:
    """Every exported scenario's batches. Batch <x> holds as many objects
    as the <x mod n>th of the n sizes. A get's are distinct, each picked
    by pick(load.objects); a put's are named <x>.<z>. A plugin twin
    gets its scenario's objects, in its own collection."""
    batches: Dict[str, List[Batch]] = {}
    for scenario in PELICAN_SCENARIOS:
        if not exported(scenario, exports):
            continue
        picks: List[List[str]] = []
        for x in range(batch_count(scenario, exports, load)):
            size = load.sizes[x % len(load.sizes)]
            if scenario.op == "put":
                picks.append([f"{x}.{z}" for z in range(size)])
                continue
            numbers: List[int] = []
            while len(numbers) < size:
                n = pick(load.objects)
                if n not in numbers:
                    numbers.append(n)
            picks.append([f"{scenario.stem}.{n}" for n in numbers])
        for each in (scenario, plugin_twin(scenario)):
            batches[each.name] = [
                Batch(each, x, tuple(batch_url(federation, each, name) for name in names))
                for x, names in enumerate(picks)]
    return batches


#---------------------------------------------------------------------------
# Why a transfer failed, from what the client said about it: the lines
# failure_lines() picks from the pelican client's log or the plugin's
# result ads. The first rule whose patterns all match wins.
#
# A server's refusal reads differently depending on where it came.
# `pelican object get` stats each object before fetching it, asking the
# director without a token (client/main.go in Pelican), so a refused get
# fails at the stat, with "HTTP 403: the server refused the credential"
# (client/acquire_token.go), and the director never sees its token.
# Transfers say "request failed (HTTP status 403)", "permission denied",
# or "server returned 401 Unauthorized" (client/handle_http.go,
# error_helpers.go).

FAILURE_RULES = (
    # category      patterns                                    meaning
    ("client",      (r"Error code 4010",),                      "it found no token to send"),
    ("director",    (r"Contact\.Director Error", r"(?<![\w.])401(?![\w.])"),
                                                                "the director rejected the token"),
    ("unsupported", (r"405", r"none support the request|could not find an origin that supports"),
                                                                "no origin allows it in the namespace"),
    ("server",      (r"HTTP (status )?40[13]\b|server returned 40[13]\b|permission denied",),
                                                                "an origin or cache answered 401 or 403"),
    # With several origins, the director checks that one has the object.
    ("not found",   (r"Error code 5011|No sources reported possession of the object",),
                                                                "there is no such object"),
)
REFUSALS = {"client", "director", "unsupported", "server"}


def classify(line: str) -> str:
    """The category of one failure, or `other`."""
    for category, patterns, _ in FAILURE_RULES:
        if all(re.search(p, line) for p in patterns):
            return category
    return "other"


def combine(categories: Iterable[str]) -> str:
    """One category for a batch's failures: the one they all share,
    `mixed` for different refusals, and otherwise `other`. A batch that
    failed without saying why is `other` too."""
    kinds = set(categories)
    if len(kinds) == 1:
        return kinds.pop()
    if kinds and kinds <= REFUSALS:
        return "mixed"
    return "other"


def failure_lines(client: str, text: str) -> List[str]:
    """The pelican client logs `Failure getting|putting <src>: <error>`
    for each object that failed, and stops at the first. The plugin writes
    a result ad per object, one per line."""
    marker = "TransferSuccess = false" if client == "plugin" else r"Failure (getting|putting) "
    return [line for line in text.splitlines() if re.search(marker, line)]


def plugin_passed(text: str) -> bool:
    """Whether the plugin's result ads say every transfer succeeded."""
    return "TransferSuccess = true" in text and "TransferSuccess = false" not in text


#---------------------------------------------------------------------------
# The verdict.

@dataclass(frozen=True)
class Observation:
    passed: bool                # the client said every transfer succeeded
    failure: Optional[str]      # if not, why (see combine)
    local: Mapping[str, str]    # the SHA-256 of each file in the batch's directory


def judge(batch: Batch, expected: Outcome, seen: Observation, refs: Mapping[str, str],
          stores: Mapping[str, Set[str]]) -> List[str]:
    """What is wrong with how batch went; nothing, if it went as expected.
    refs is the SHA-256 of each test object at the origins, and stores
    those of what the origins hold in each put collection, both by
    rel_path()."""
    scenario = batch.scenario
    problems: List[str] = []

    if expected is Outcome.PASS:
        if not seen.passed:
            return [f"failed ({seen.failure}), but should have passed"]
        for url in batch.urls:
            name, rel = local_name(url), rel_path(url)
            have = seen.local.get(name)
            if scenario.op == "get":
                if have is None:
                    problems.append(f"{name}: missing locally")
                elif rel not in refs:
                    problems.append(f"{name}: not at any origin")
                elif have != refs[rel]:
                    problems.append(f"{name}: bytes differ from the origin's")
            else:
                if have is None:
                    problems.append(f"{name}: source missing")
                elif rel not in stores:
                    problems.append(f"{name}: not at any origin")
                elif have not in stores[rel]:
                    problems.append(f"{name}: bytes differ at the origin")
        return problems

    if expected is Outcome.NOT_FOUND:
        if seen.passed:
            problems.append("passed, but should have failed: not found")
        elif seen.failure != "not found":
            problems.append(f"failed ({seen.failure}), but not with not found")
        return problems

    # Refused: whatever the client says, check that nothing moved.
    if seen.passed:
        problems.append("passed, but should have been refused")
    elif seen.failure not in REFUSALS | {"mixed"}:
        problems.append(f"failed ({seen.failure}), but was not refused")
    for url in batch.urls:
        name, rel = local_name(url), rel_path(url)
        if scenario.op == "get":
            if rel in refs and seen.local.get(name) == refs[rel]:
                problems.append(f"{name}: arrived although refused")
        elif rel in stores:
            problems.append(f"{name}: exists at an origin although refused")
    return problems


def summarize_failures(failures: Sequence[str]) -> str:
    """e.g. `server 5, mixed 1`, in the order of FAILURE_RULES."""
    order = [rule[0] for rule in FAILURE_RULES] + ["mixed", "other"]
    return ", ".join(f"{kind} {failures.count(kind)}" for kind in order if kind in failures)
