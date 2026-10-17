#!/usr/bin/env python3
"""A federation's choices, and the shapes that smoke.sh runs by default.

  framework/matrix.py check     check smoke.sh's shapes (see below)
  framework/matrix.py pairs     list every pair of choices a shape can make
  framework/matrix.py generate  rewrite SHAPES_FILE, keeping what it can
  framework/matrix.py generate --fresh
                                rewrite SHAPES_FILE from scratch

Each preset makes one choice (see PRESETS); rotating every key, which
smoke.sh does after the tests in some shapes, is one more. A shape is
the choices its presets make, and every other factor's default. Some
shapes fed.sh refuses (REFUSED, which mirrors its checks), and some
smoke.sh does not run (SMOKE_LIMITS), since they would test nothing, or
nothing that simpler shapes don't. Of the rest, every pair of choices
must be in some shape that smoke.sh runs. A pair is possible if some
such shape makes it with at most SEARCH_DEPTH other factors away from
their defaults (see completion()): there are too many shapes to try
them all.

smoke.sh runs the BASELINES, and as many other shapes as are useful,
each as simple as it can be: each makes at most MAX_CHOICES choices,
makes some pair that no other shape makes, and would leave some pair
uncovered if any one of its choices were the default instead.
SHAPES_FILE lists them, as `generate` writes it.

`check` reads the shapes from `smoke.sh --table`, and reports any shape
that makes no sense, that fed.sh would refuse, or that breaks the rules
above; any preset no shape runs; and any pair no shape covers. It exits
1 if it found any. `generate` keeps the shapes in SHAPES_FILE that still
pass (or none, with --fresh), picks shapes greedily until they cover
every pair, then simplifies them, and drops any that adds nothing.
"""

import itertools
import os
import subprocess  # nosec B404
import sys
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Each factor's levels, its default first.
FACTORS: dict[str, tuple[str, ...]] = {
    "topology": ("full", "tiny", "standalone"),
    "caches": ("one", "none"),
    "namespaces": ("all", "public"),
    "public_writes": ("off", "on"),
    "origin": ("posixv2", "xrootd", "pstore", "ssh", "httpsv2", "s3v2", "s3", "https"),
    "cache": ("v2", "xrootd", "mixed"),
    "multi_origin": ("off", "on"),
    "multi_cache": ("off", "on"),
    "multi_owner": ("off", "on"),
    "multi_director": ("off", "on"),
    "site_local": ("off", "on"),
    "issuer": ("own", "external", "none"),
    "login": ("local", "oidc"),
    "direct": ("yes", "no"),
    "posc": ("off", "on"),
    "atomic": ("off", "on"),
    "metadata": ("off", "eventual", "transactional"),
    "max_age": ("off", "on"),
    "multiuser": ("off", "on"),
    "broker": ("off", "on"),
    "ssh_tunnel": ("off", "on"),
    "transfer_api": ("off", "on", "server"),
    "tiering": ("off", "on"),
    "memory_cache": ("off", "on"),
    "store": ("unbounded", "bounded"),
    "privileges": ("root", "dropped"),
    "default_response": ("cache", "origin"),
    "discovery": ("host", "director"),
    "director_ha": ("off", "on"),
    "approval": ("off", "on"),
    "tokens": ("long", "short"),
    "templates": ("global", "export"),
    "keys": ("kept", "rotated"),
}

# The choice each preset makes. `keys` is smoke.sh's, not a preset's.
PRESETS: dict[str, tuple[str, str]] = {
    "topo-basic": ("topology", "full"),
    "topo-tiny": ("topology", "tiny"),
    "topo-standalone": ("topology", "standalone"),
    "topo-no-cache": ("caches", "none"),
    "origin-public": ("namespaces", "public"),
    "origin-public-writes": ("public_writes", "on"),
    "origin-posixv2": ("origin", "posixv2"),
    "origin-xrootd": ("origin", "xrootd"),
    "origin-pstore": ("origin", "pstore"),
    "origin-ssh": ("origin", "ssh"),
    "origin-httpsv2": ("origin", "httpsv2"),
    "origin-s3v2": ("origin", "s3v2"),
    "origin-s3": ("origin", "s3"),
    "origin-https": ("origin", "https"),
    "cache-v2": ("cache", "v2"),
    "cache-xrootd": ("cache", "xrootd"),
    "cache-mixed": ("cache", "mixed"),
    "topo-multi-origin": ("multi_origin", "on"),
    "topo-multi-cache": ("multi_cache", "on"),
    "topo-multi-owner": ("multi_owner", "on"),
    "topo-multi-director": ("multi_director", "on"),
    "topo-site-local-cache": ("site_local", "on"),
    "auth-external-issuer": ("issuer", "external"),
    "auth-no-issuer": ("issuer", "none"),
    "auth-oidc": ("login", "oidc"),
    "origin-no-direct": ("direct", "no"),
    "origin-posc": ("posc", "on"),
    "origin-atomic-uploads": ("atomic", "on"),
    "origin-metadata": ("metadata", "eventual"),
    "origin-metadata-tx": ("metadata", "transactional"),
    "origin-max-age": ("max_age", "on"),
    "origin-multiuser": ("multiuser", "on"),
    "origin-broker": ("broker", "on"),
    "origin-ssh-tunnel": ("ssh_tunnel", "on"),
    "origin-transfer-api": ("transfer_api", "on"),
    "topo-transfer-server": ("transfer_api", "server"),
    "cache-tiered": ("tiering", "on"),
    "cache-memory": ("memory_cache", "on"),
    "cache-small": ("store", "bounded"),
    "server-unprivileged": ("privileges", "dropped"),
    "director-default-origin": ("default_response", "origin"),
    "director-discovery": ("discovery", "director"),
    "director-ha": ("director_ha", "on"),
    "registry-approval": ("approval", "on"),
    "origin-short-tokens": ("tokens", "short"),
    "origin-export-templates": ("templates", "export"),
}

# Presets smoke.sh does not run, and why.
NOT_RUN: dict[str, str] = {
    "with-grafana": "Grafana has nothing provisioned to check",
}

# The origins that run XRootD, and those with a backend service.
XROOTD_ORIGINS = ("xrootd", "s3", "https")
BACKED_ORIGINS = ("ssh", "httpsv2", "s3v2", "s3", "https")
# The factors that add a server to the full topology.
ADDED_SERVERS = ("multi_origin", "multi_cache", "multi_owner", "multi_director", "site_local")
# The levels of other factors that add one.
ADDED_LEVELS = (("transfer_api", "server"),)

# How many factors besides a pair's completion() may change.
SEARCH_DEPTH = 3

# How many choices a shape that smoke.sh runs may make (Shape.choices()).
# A simpler shape points at fewer presets when it fails; at 8, the
# shapes are about as many as with no limit.
MAX_CHOICES = 8

# The shapes that smoke.sh runs whatever else it runs: references whose
# failures are none of the other presets' doing. By name: the presets,
# whether it rotates every key, and why it is run.
BASELINES: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "default": (
        ("topo-basic", "origin-posixv2", "cache-v2"),
        True,
        "The framework's own defaults, what `./fed.sh up` runs, each named.",
    ),
    "pelican-defaults": (
        ("origin-xrootd", "cache-xrootd", "auth-no-issuer", "director-discovery"),
        False,
        "Pelican's own defaults: an XRootD origin (Origin.StorageType posix),"
        + " XRootD caches (Cache.EnableV2 off), no issuer, and the director as"
        + " the discovery host, as Pelican's documentation sets up a federation.",
    ),
}

# The shapes that smoke.sh runs, which `generate` writes.
SHAPES_FILE = os.path.join(TOP, "framework", "smoke-shapes.tsv")

Pair = tuple[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class Shape:
    """Every factor's level, by factor."""

    levels: Mapping[str, str]

    def __getitem__(self, factor: str) -> str:
        return self.levels[factor]

    def pairs(self) -> set[Pair]:
        """Return every pair of choices it makes."""
        choices = [(f, self.levels[f]) for f in FACTORS]
        return set(itertools.combinations(choices, 2))

    def choices(self) -> int:
        """Return how many factors it moves from their defaults."""
        return sum(self.levels[f] != levels[0] for f, levels in FACTORS.items())

    def adds_server(self) -> bool:
        """Return whether it adds a server to the full topology."""
        return "on" in (self.levels[f] for f in ADDED_SERVERS) or any(
            self.levels[f] == level for f, level in ADDED_LEVELS
        )


Rule = tuple[str, Callable[[Shape], bool]]

# The shapes fed.sh refuses: why, and whether a shape is one.
REFUSED: list[Rule] = [
    (
        "the 'tiny' topology has no room for the origin's backend",
        lambda s: s["topology"] == "tiny" and s["origin"] in BACKED_ORIGINS,
    ),
    (
        "the 'tiny' topology's cache is always V2",
        lambda s: s["topology"] == "tiny" and s["cache"] != "v2",
    ),
    (
        "the 'tiny' topology has no room for another server",
        lambda s: s["topology"] == "tiny" and s.adds_server(),
    ),
    (
        "the 'tiny' topology has no discovery host to serve the external issuer",
        lambda s: s["topology"] == "tiny" and s["issuer"] == "external",
    ),
    (
        "the 'tiny' topology's director is its discovery host already",
        lambda s: s["topology"] == "tiny" and s["discovery"] == "director",
    ),
    (
        "a standalone origin needs a native backend",
        lambda s: s["topology"] == "standalone" and s["origin"] in XROOTD_ORIGINS,
    ),
    (
        "the 'standalone' topology has no cache",
        lambda s: s["topology"] == "standalone" and s["cache"] != "v2",
    ),
    (
        "the 'standalone' topology has no room for another server",
        lambda s: s["topology"] == "standalone" and s.adds_server(),
    ),
    (
        "a standalone origin takes only direct clients",
        lambda s: s["topology"] == "standalone" and s["direct"] == "no",
    ),
    (
        "the metadata verifier needs a registry, which a standalone origin lacks",
        lambda s: s["topology"] == "standalone" and s["metadata"] != "off",
    ),
    (
        "a standalone origin has no director to send bare paths anywhere",
        lambda s: s["topology"] == "standalone" and s["default_response"] != "cache",
    ),
    (
        "a standalone origin has no registry to require approval",
        lambda s: s["topology"] == "standalone" and s["approval"] == "on",
    ),
    (
        "a standalone origin has no director to serve the federation's metadata",
        lambda s: s["topology"] == "standalone" and s["discovery"] == "director",
    ),
    (
        "running no cache is the full topology's choice",
        lambda s: s["caches"] == "none" and s["topology"] != "full",
    ),
    (
        "running no cache leaves no room for another",
        lambda s: s["caches"] == "none" and "on" in (s["multi_cache"], s["site_local"]),
    ),
    (
        "running no cache makes the kind of cache moot",
        lambda s: s["caches"] == "none" and s["cache"] != "v2",
    ),
    (
        "running no cache leaves none to tier, to give a memory cache, or to bound",
        lambda s: s["caches"] == "none"
        and (s["tiering"] == "on" or s["memory_cache"] == "on" or s["store"] == "bounded"),
    ),
    (
        "origins that serve only caches serve no one without a cache",
        lambda s: s["caches"] == "none" and s["direct"] == "no",
    ),
    (
        "POSC and metadata publishing are posixv2's",
        lambda s: (s["posc"] == "on" or s["metadata"] != "off") and s["origin"] != "posixv2",
    ),
    (
        "XRootD's atomic uploads are its posix origin's",
        lambda s: s["atomic"] == "on" and s["origin"] != "xrootd",
    ),
    (
        "Origin.CacheControl is the native origins': an XRootD origin sends none",
        lambda s: s["max_age"] == "on" and s["origin"] in XROOTD_ORIGINS,
    ),
    (
        "a pstore origin is seeded through its writes, which no-direct takes away",
        lambda s: s["direct"] == "no" and s["origin"] == "pstore",
    ),
    (
        "a second owner has no store with an ssh or pstore origin",
        lambda s: s["multi_owner"] == "on" and s["origin"] in ("ssh", "pstore"),
    ),
    (
        "multiuser needs a POSIX origin",
        lambda s: s["multiuser"] == "on" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    ("multiuser needs root", lambda s: s["multiuser"] == "on" and s["privileges"] == "dropped"),
    (
        "an ssh origin's key is unreadable once the servers drop privileges",
        lambda s: s["privileges"] == "dropped" and s["origin"] == "ssh",
    ),
    (
        "the connection broker relays only to XRootD",
        lambda s: s["broker"] == "on" and s["origin"] not in XROOTD_ORIGINS,
    ),
    (
        "the SSH tunnel is the ssh origin's",
        lambda s: s["ssh_tunnel"] == "on" and s["origin"] != "ssh",
    ),
    (
        "the connection broker needs origin-0 alone, in the full topology",
        lambda s: s["broker"] == "on"
        and (s["topology"] != "full" or "on" in (s["multi_origin"], s["multi_owner"])),
    ),
    (
        "mixed kinds of cache put XRootD at cache-1, which needs topo-multi-cache",
        lambda s: s["cache"] == "mixed" and s["multi_cache"] == "off",
    ),
    (
        "cache tiering is the V2 cache's, in the full topology",
        lambda s: s["tiering"] == "on" and (s["cache"] != "v2" or s["topology"] != "full"),
    ),
    (
        "the memory cache is the V2 cache's",
        lambda s: s["memory_cache"] == "on" and s["cache"] != "v2",
    ),
    (
        "a standalone origin has no cache to give a memory cache or to bound",
        lambda s: (s["memory_cache"] == "on" or s["store"] == "bounded")
        and s["topology"] == "standalone",
    ),
    (
        "short tokens and export templates are the origins' issuer's, which is off",
        lambda s: s["issuer"] != "own"
        and (s["tokens"] == "short" or s["templates"] == "export"),
    ),
    (
        "per-export templates go on /protected-b, which the shape does not export",
        lambda s: s["templates"] == "export"
        and (
            s["origin"] in ("https", "httpsv2")
            or s["direct"] == "no"
            or s["broker"] == "on"
            or s["namespaces"] == "public"
        ),
    ),
    (
        "a pstore origin's /public reads /protected-a's storage, which the shape does not export",
        lambda s: s["namespaces"] == "public" and s["origin"] == "pstore",
    ),
    (
        "another owner's /public/secret takes a token beside its public exports, which Pelican"
        + " refuses once the origins serve only caches",
        lambda s: s["namespaces"] == "public"
        and s["multi_owner"] == "on"
        and s["direct"] == "no",
    ),
    (
        "a load balancer balances two directors",
        lambda s: s["director_ha"] == "on" and s["multi_director"] == "off",
    ),
    (
        "writes to /public are given where it is exported: not beside /protected-a alone",
        lambda s: s["public_writes"] == "on"
        and s["namespaces"] == "all"
        and (s["origin"] in ("https", "httpsv2") or s["broker"] == "on"),
    ),
    (
        "writes to /public need direct clients, without which Pelican allows only reads",
        lambda s: s["public_writes"] == "on" and s["direct"] == "no",
    ),
    (
        "a pstore origin's /public reads /protected-a's storage, where a write would land",
        lambda s: s["public_writes"] == "on" and s["origin"] == "pstore",
    ),
    (
        "the connection broker's state is one director's, which a load balancer hides",
        lambda s: s["director_ha"] == "on" and s["broker"] == "on",
    ),
]

# The shapes fed.sh accepts that smoke.sh does not run.
SMOKE_LIMITS: list[Rule] = [
    (
        "a transfer server runs over posixv2 and xrootd origins only: what it tests beyond"
        + " origin-0's API is the server's own confinement and issuer, not the origins' storage",
        lambda s: s["transfer_api"] == "server" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "a transfer API runs only where /protected-a takes writes: without them, the"
        + " transfer-api suite has nowhere to copy to, and skips",
        lambda s: s["transfer_api"] != "off"
        and (s["direct"] == "no" or s["namespaces"] == "public"),
    ),
    (
        "once the servers drop privileges, the host may not write their keys",
        lambda s: s["keys"] == "rotated" and s["privileges"] == "dropped",
    ),
    (
        "mixed kinds of cache run over posixv2 and xrootd origins only: what they test is"
        + " between the caches",
        lambda s: s["cache"] == "mixed" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "a federation without caches runs over posixv2 and xrootd origins only: what it"
        + " tests is the directors' and the clients' way around the missing cache",
        lambda s: s["caches"] == "none" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "the identity provider runs over posixv2 and xrootd origins only: a login is the"
        + " servers' web UI's, not their storage's",
        lambda s: s["login"] == "oidc" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "origins of public data alone run over posixv2 and xrootd only: what they test is"
        + " the federation's way with a namespace that has no issuer, not their storage",
        lambda s: s["namespaces"] == "public" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "the directors' load balancer runs over posixv2 and xrootd origins only: what it"
        + " tests is the directors, not the origins' storage",
        lambda s: s["director_ha"] == "on" and s["origin"] not in ("posixv2", "xrootd"),
    ),
    (
        "writes to /public run over posixv2 and xrootd origins only: what they test is the"
        + " federation's way with a namespace that anyone reads and a token writes, not the"
        + " origins' storage",
        lambda s: s["public_writes"] == "on" and s["origin"] not in ("posixv2", "xrootd"),
    ),
]


def shape_of(
    presets: Iterable[str], rotated: bool = False
) -> tuple[Optional[Shape], list[str]]:
    """Return the shape that presets make, and what is wrong with them.

    Args:
        presets: Names of presets.
        rotated: Whether the shape rotates every key.

    Returns:
        The shape, or None if anything is wrong; and what is wrong with
        presets: an unknown preset, one that smoke.sh does not run, or two
        that choose differently for one factor.
    """
    levels = {f: choices[0] for f, choices in FACTORS.items()}
    levels["keys"] = "rotated" if rotated else "kept"
    chosen_by: dict[str, str] = {}
    wrong: list[str] = []
    for preset in presets:
        if preset not in PRESETS:
            why = NOT_RUN.get(preset)
            wrong.append(f"{preset}: not run ({why})" if why else f"{preset}: no such preset")
            continue
        factor, level = PRESETS[preset]
        other = chosen_by.get(factor)
        if other and other != preset and PRESETS[other][1] != level:
            wrong.append(f"{other} and {preset} both choose {factor}")
            continue
        chosen_by[factor] = preset
        levels[factor] = level
    if wrong:
        return None, wrong
    return Shape(levels), []


def refusals(shape: Shape) -> list[str]:
    """Return why fed.sh would refuse shape or smoke.sh would not run it."""
    return [why for why, applies in REFUSED + SMOKE_LIMITS if applies(shape)]


def changes(factors: Sequence[str], depth: int) -> Iterator[dict[str, str]]:
    """Yield every way to move at most depth of factors from their defaults.

    Yields:
        The new levels of the factors that a way moves, by factor; the
        ways that move the fewest come first.
    """
    for count in range(depth + 1):
        for chosen in itertools.combinations(factors, count):
            for levels in itertools.product(*(FACTORS[f][1:] for f in chosen)):
                yield dict(zip(chosen, levels))


def completion(fixed: Mapping[str, str]) -> Optional[Shape]:
    """Return a shape that smoke.sh could run that makes the fixed choices.

    The shape has as few other factors away from their defaults as can
    be, and no more than SEARCH_DEPTH.

    Args:
        fixed: The choices to make, as levels by factor.

    Returns:
        The shape, or None if there is none.
    """
    defaults = {f: choices[0] for f, choices in FACTORS.items()}
    others = [f for f in FACTORS if f not in fixed]
    for change in changes(others, SEARCH_DEPTH):
        shape = Shape({**defaults, **fixed, **change})
        if not refusals(shape):
            return shape
    return None


def valid_pairs() -> set[Pair]:
    """Return the pairs of choices that a shape smoke.sh could run makes."""
    choices = [(f, level) for f, levels in FACTORS.items() for level in levels]
    found: set[Pair] = set()
    for a, b in itertools.combinations(choices, 2):
        if a[0] != b[0] and completion(dict([a, b])) is not None:
            found.add((a, b))
    return found


def covered(shapes: Iterable[Shape]) -> set[Pair]:
    """Return every pair of choices that shapes make."""
    found: set[Pair] = set()
    for shape in shapes:
        found |= shape.pairs()
    return found


def grow(start: Pair, left: set[Pair]) -> Shape:
    """Return a shape that makes start, and covers pairs of left greedily.

    The shape makes start as completion() does, and, for each other
    factor in turn, the level that covers the most pairs of left, the
    default on a tie, without making more than MAX_CHOICES choices.
    """
    shape = completion(dict(start))
    if shape is None:
        raise ValueError(f"no shape makes {start}, though valid_pairs() says one does")
    levels = dict(shape.levels)
    for factor, choices in FACTORS.items():
        if factor in (start[0][0], start[1][0]):
            continue
        best, gain = levels[factor], -1
        for level in choices:
            trial = Shape({**levels, factor: level})
            if trial.choices() > MAX_CHOICES or refusals(trial):
                continue
            if len(trial.pairs() & left) > gain:
                best, gain = level, len(trial.pairs() & left)
        levels[factor] = best
    return Shape(levels)


def pair_counts(shapes: Iterable[Shape], valid: set[Pair]) -> Counter[Pair]:
    """Return how many of shapes make each pair of valid."""
    counts: Counter[Pair] = Counter()
    for shape in shapes:
        counts.update(shape.pairs() & valid)
    return counts


def adds_nothing(shape: Shape, counts: Counter[Pair], valid: set[Pair]) -> bool:
    """Return whether another shape makes every pair of valid that shape does.

    counts is pair_counts() of every shape, shape among them.
    """
    return all(counts[p] > 1 for p in shape.pairs() & valid)


def simpler(shape: Shape, counts: Counter[Pair], valid: set[Pair]) -> Optional[Shape]:
    """Return shape with one of its choices the default, if that loses no pair.

    The simpler shape must be one that smoke.sh could run, and make every
    pair of valid that shape makes and no other does. counts is
    pair_counts() of every shape, shape among them.

    Returns:
        The first such shape, by FACTORS' order, or None.
    """
    for factor, levels in FACTORS.items():
        if shape[factor] == levels[0]:
            continue
        trial = Shape({**shape.levels, factor: levels[0]})
        if refusals(trial):
            continue
        kept = trial.pairs()
        if all(counts[p] > 1 or p in kept for p in shape.pairs() & valid):
            return trial
    return None


def baseline_shapes() -> list[Shape]:
    """Return the shapes that BASELINES make."""
    found: list[Shape] = []
    for name, (presets, rotated, _) in BASELINES.items():
        shape, wrong = shape_of(presets, rotated)
        if shape is None:
            raise ValueError(f"the baseline {name}: {'; '.join(wrong)}")
        found.append(shape)
    return found


def generate(current: Iterable[Shape]) -> list[Shape]:
    """Return the shapes that smoke.sh should run, but the baselines.

    Of current, it keeps each that smoke.sh could run that makes at most
    MAX_CHOICES choices. It adds shapes greedily until every pair is
    covered: of the shapes that grow() from each pair still uncovered,
    the one that covers the most, the simpler on a tie. Then, until
    nothing changes, it makes each shape simpler that can be (simpler()),
    and drops each that adds nothing (adds_nothing()), those that make
    the most choices first.

    Returns:
        The shapes, the simplest first.
    """
    valid = valid_pairs()
    baselines = baseline_shapes()
    picked = [s for s in current if s.choices() <= MAX_CHOICES and not refusals(s)]
    left = valid - covered([*baselines, *picked])
    while left:
        grown = [grow(start, left) for start in sorted(left)]
        best = max(grown, key=lambda s: (len(s.pairs() & left), -s.choices()))
        picked.append(best)
        left -= best.pairs()
    changed = True
    while changed:
        changed = False
        counts = pair_counts([*baselines, *picked], valid)
        for n, shape in enumerate(picked):
            trial = simpler(shape, counts, valid)
            if trial is not None:
                counts.subtract(shape.pairs() & valid)
                counts.update(trial.pairs() & valid)
                picked[n] = trial
                changed = True
        for shape in sorted(picked, key=lambda s: (-s.choices(), name_of(s))):
            if adds_nothing(shape, counts, valid):
                counts.subtract(shape.pairs() & valid)
                picked.remove(shape)
                changed = True
    return sorted(picked, key=lambda s: (s.choices(), name_of(s)))


# The factors that a shape's name begins with (see name_of()).
NAMED_FIRST = ("topology", "caches", "origin", "cache")


def presets_of(shape: Shape) -> list[str]:
    """Return the presets that make shape.

    Returns:
        One per factor not at its default, but `keys`: those of
        NAMED_FIRST first, then the rest in FACTORS' order, as name_of()
        names them.
    """
    by_choice = {choice: preset for preset, choice in PRESETS.items()}
    order = [*NAMED_FIRST, *(f for f in FACTORS if f not in NAMED_FIRST)]
    return [
        by_choice[(f, shape[f])] for f in order if f != "keys" and shape[f] != FACTORS[f][0]
    ]


def name_of(shape: Shape) -> str:
    """Return the name that smoke.sh gives shape, if it is no baseline.

    The name is the topology, if not the full one, and the origin; or
    `nocache`, if there is no cache, and the origin; or else the origin
    and the kind of cache. Then come the other choices, in FACTORS'
    order, each its preset's name without the prefix (`topo-`,
    `origin-`, ...), and `rotated` if it rotates every key: e.g.
    `xrootd-v2-multi-cache-no-direct`, or `tiny-pstore-small-rotated`.
    """
    if shape["topology"] != "full":
        parts = [shape["topology"], shape["origin"]]
    elif shape["caches"] == "none":
        parts = ["nocache", shape["origin"]]
    else:
        parts = [shape["origin"], shape["cache"]]
    by_choice = {choice: preset for preset, choice in PRESETS.items()}
    for factor, levels in FACTORS.items():
        if factor in NAMED_FIRST or shape[factor] == levels[0]:
            continue
        if factor == "keys":
            parts.append("rotated")
        else:
            parts.append(by_choice[(factor, shape[factor])].split("-", 1)[1])
    return "-".join(parts)


# --------------------------------------------------------------------------
# smoke.sh's shapes.


@dataclass(frozen=True)
class SmokeShape:
    """A shape that smoke.sh runs.

    Attributes:
        rotated: Whether it rotates every key.
    """

    name: str
    rotated: bool
    presets: tuple[str, ...]


def parse_table(text: str) -> list[SmokeShape]:
    """Return the shapes in the output of `smoke.sh --table`.

    Args:
        text: That output: `<name>\\t<yes|no>\\t<preset> ...` per shape,
            the middle column saying whether it rotates every key.
    """
    found: list[SmokeShape] = []
    for line in text.splitlines():
        if line.strip():
            name, rotated, presets = line.split("\t")
            found.append(SmokeShape(name, rotated == "yes", tuple(presets.split())))
    return found


def smoke_shapes() -> list[SmokeShape]:
    """Return the shapes that smoke.sh runs."""
    table = subprocess.run(  # nosec B603 B607
        ["sh", os.path.join(TOP, "smoke.sh"), "--table"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return parse_table(table)


def all_presets() -> set[str]:
    """Return every preset in framework/presets/ but `default`."""
    return {
        name[: -len(".sh")]
        for name in os.listdir(os.path.join(TOP, "framework", "presets"))
        if name.endswith(".sh") and name != "default.sh"
    }


def made(shapes: Sequence[SmokeShape]) -> tuple[list[tuple[SmokeShape, Shape]], list[str]]:
    """Return smoke.sh's shapes, each with what it makes, and what is wrong."""
    found: list[str] = []
    names = [s.name for s in shapes]
    for name in sorted({n for n in names if names.count(n) > 1}):
        found.append(f"two shapes are named {name}")
    shapes_made: list[tuple[SmokeShape, Shape]] = []
    for smoke in shapes:
        shape, wrong = shape_of(smoke.presets, smoke.rotated)
        if shape is None:
            found += [f"{smoke.name}: {why}" for why in wrong]
            continue
        found += [f"{smoke.name}: {why}" for why in refusals(shape)]
        shapes_made.append((smoke, shape))
    return shapes_made, found


def check(shapes: Sequence[SmokeShape]) -> list[str]:
    """Return what is wrong with smoke.sh's shapes."""
    shapes_made, found = made(shapes)
    by_name = {smoke.name: smoke for smoke in shapes}
    for name, (presets, rotated, _) in BASELINES.items():
        smoke = by_name.get(name)
        if smoke is None:
            found.append(f"no shape is the baseline {name}")
        elif set(smoke.presets) != set(presets) or smoke.rotated != rotated:
            found.append(f"{name}: not the baseline that BASELINES describes")
    valid = valid_pairs()
    counts = pair_counts((shape for _, shape in shapes_made), valid)
    for smoke, shape in shapes_made:
        if smoke.name in BASELINES:
            continue
        if shape.choices() > MAX_CHOICES:
            found.append(
                f"{smoke.name}: makes {shape.choices()} choices, more than {MAX_CHOICES}"
            )
        if smoke.name != name_of(shape):
            found.append(f"{smoke.name}: should be named {name_of(shape)}")
        trial = simpler(shape, counts, valid)
        if adds_nothing(shape, counts, valid):
            found.append(f"{smoke.name}: makes no pair that another shape does not")
        elif trial is not None:
            found.append(f"{smoke.name}: {name_of(trial)}, which is simpler, covers as much")
    run = {p for s in shapes for p in s.presets}
    for preset in sorted(all_presets() - run - set(NOT_RUN)):
        found.append(f"no shape runs {preset}")
    for preset in sorted(all_presets() - set(PRESETS) - set(NOT_RUN)):
        found.append(f"{preset} is not in framework/matrix.py")
    for (f1, l1), (f2, l2) in sorted(valid - covered(shape for _, shape in shapes_made)):
        found.append(f"no shape has both {f1}={l1} and {f2}={l2}")
    return found


# The head of SHAPES_FILE.
SHAPES_HEADER = """\
# The shapes that ./smoke.sh runs when it is given none: one per line,
# its name, whether it rotates every key after the tests and runs them
# again (yes or no), and its presets, separated by tabs.
#
# `framework/matrix.py generate` writes this file (see "The smoke
# matrix" in docs/development.md). Rather than edit it, change
# framework/matrix.py, and run that again; `framework/matrix.py check`
# checks it.
"""


def write_shapes(shapes: Sequence[Shape]) -> None:
    """Write SHAPES_FILE: the BASELINES, then shapes, in their order."""

    def row(name: str, rotated: bool, presets: Iterable[str]) -> str:
        return f"{name}\t{'yes' if rotated else 'no'}\t{' '.join(presets)}\n"

    text = SHAPES_HEADER
    for name, (presets, rotated, why) in BASELINES.items():
        text += "#\n" + "".join(f"# {line}\n" for line in textwrap.wrap(why, 70))
        text += row(name, rotated, presets)
    text += "#\n# The rest, the simplest first.\n"
    for shape in shapes:
        text += row(name_of(shape), shape["keys"] == "rotated", presets_of(shape))
    with open(SHAPES_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    command, rest = args[0], args[1:]
    if command == "generate" and rest in ([], ["--fresh"]):
        before = smoke_shapes() if os.path.exists(SHAPES_FILE) else []
        current = [] if rest else [s for b, s in made(before)[0] if b.name not in BASELINES]
        picked = generate(current)
        write_shapes(picked)
        old, new = {s.name for s in before}, {name_of(s) for s in picked} | set(BASELINES)
        for name in sorted(new - old):
            print(f"added {name}")
        for name in sorted(old - new):
            print(f"removed {name}")
        print(f"{len(new)} shapes cover all {len(valid_pairs())} pairs")
    elif command == "check" and not rest:
        shapes = smoke_shapes()
        found = check(shapes)
        for problem in found:
            print(problem)
        if found:
            sys.exit(1)
        print(f"{len(shapes)} shapes cover all {len(valid_pairs())} pairs")
    elif command == "pairs" and not rest:
        for (f1, l1), (f2, l2) in sorted(valid_pairs()):
            print(f"{f1}={l1} {f2}={l2}")
    else:
        print(
            f"matrix.py: unknown command: {' '.join(args)} (try: check, pairs, generate)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
