#!/usr/bin/env python3
"""The choices that shape a federation, and whether smoke.sh's shapes
cover every pair of them.

  framework/matrix.py check     check smoke.sh's shapes (see below)
  framework/matrix.py pairs     list every pair of choices a shape can make
  framework/matrix.py suggest   print shapes that cover what smoke.sh's miss

Each preset makes one choice (see PRESETS); rotating every key, which
smoke.sh does after the tests in some shapes, is one more. A shape is
the choices its presets make, and every other factor's default. Some
shapes fed.sh refuses (REFUSED, which mirrors its checks), and some
smoke.sh does not run (SMOKE_LIMITS). Of the rest, every pair of choices
must be in some shape that smoke.sh runs. A pair is possible if some
such shape makes it with at most SEARCH_DEPTH other factors away from
their defaults (see completion()): there are too many shapes to try
them all.

`check` reads the shapes from `smoke.sh --table`, and reports any shape
that makes no sense or that fed.sh would refuse, any preset no shape
runs, and any pair no shape covers. It exits 1 if it found any.
`suggest` picks shapes greedily, to add to smoke.sh's until they cover
every pair, for a person to trim and paste into smoke.sh.
"""

import itertools
import os
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Each factor's levels, its default first.
FACTORS: dict[str, tuple[str, ...]] = {
    "topology": ("full", "tiny", "standalone"),
    "origin": ("posixv2", "xrootd", "pstore", "ssh", "httpsv2", "s3v2", "s3", "https"),
    "cache": ("v2", "xrootd"),
    "multi_origin": ("off", "on"),
    "multi_cache": ("off", "on"),
    "multi_owner": ("off", "on"),
    "multi_director": ("off", "on"),
    "site_local": ("off", "on"),
    "issuer": ("own", "external"),
    "direct": ("yes", "no"),
    "posc": ("off", "on"),
    "metadata": ("off", "eventual", "transactional"),
    "max_age": ("off", "on"),
    "multiuser": ("off", "on"),
    "broker": ("off", "on"),
    "transfer_api": ("off", "on"),
    "tiering": ("off", "on"),
    "privileges": ("root", "dropped"),
    "keys": ("kept", "rotated"),
}

# The choice each preset makes. `keys` is smoke.sh's, not a preset's.
PRESETS: dict[str, tuple[str, str]] = {
    "topo-basic": ("topology", "full"),
    "topo-tiny": ("topology", "tiny"),
    "topo-standalone": ("topology", "standalone"),
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
    "topo-multi-origin": ("multi_origin", "on"),
    "topo-multi-cache": ("multi_cache", "on"),
    "topo-multi-owner": ("multi_owner", "on"),
    "topo-multi-director": ("multi_director", "on"),
    "topo-site-local-cache": ("site_local", "on"),
    "auth-external-issuer": ("issuer", "external"),
    "origin-no-direct": ("direct", "no"),
    "origin-posc": ("posc", "on"),
    "origin-metadata": ("metadata", "eventual"),
    "origin-metadata-tx": ("metadata", "transactional"),
    "origin-max-age": ("max_age", "on"),
    "origin-multiuser": ("multiuser", "on"),
    "origin-broker": ("broker", "on"),
    "origin-transfer-api": ("transfer_api", "on"),
    "cache-tiered": ("tiering", "on"),
    "server-unprivileged": ("privileges", "dropped"),
}

# Presets that add a service no Pelican server uses, and so make no
# choice. Some shape must still run each.
EXTRAS: frozenset[str] = frozenset({"with-lab"})

# Presets smoke.sh does not run, and why.
NOT_RUN: dict[str, str] = {
    "auth-oidc": "logging in needs a client registered with a real identity provider",
    "with-grafana": "Grafana has nothing provisioned to check",
}

# The origins that run XRootD, and those with a backend service.
XROOTD_ORIGINS = ("xrootd", "s3", "https")
BACKED_ORIGINS = ("ssh", "httpsv2", "s3v2", "s3", "https")
# The factors that add a server to the full topology.
ADDED_SERVERS = ("multi_origin", "multi_cache", "multi_owner", "multi_director", "site_local")

# How many factors besides a pair's completion() may change.
SEARCH_DEPTH = 3

Pair = tuple[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class Shape:
    """Every factor's level, and the EXTRAS that a shape runs."""

    levels: Mapping[str, str]  # every factor's level
    extras: frozenset[str]  # the EXTRAS it runs

    def __getitem__(self, factor: str) -> str:
        return self.levels[factor]

    def pairs(self) -> set[Pair]:
        """Every pair of choices it makes."""
        choices = [(f, self.levels[f]) for f in FACTORS]
        return set(itertools.combinations(choices, 2))

    def adds_server(self) -> bool:
        """Whether it adds a server to the full topology."""
        return "on" in (self.levels[f] for f in ADDED_SERVERS)


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
        "the 'tiny' topology has no room for another service",
        lambda s: s["topology"] == "tiny" and bool(s.extras),
    ),
    (
        "the 'tiny' topology has no discovery host to serve the external issuer",
        lambda s: s["topology"] == "tiny" and s["issuer"] == "external",
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
        "POSC and metadata publishing are posixv2's",
        lambda s: (s["posc"] == "on" or s["metadata"] != "off") and s["origin"] != "posixv2",
    ),
    (
        "a pstore origin is seeded through its writes, which no-direct takes away",
        lambda s: s["direct"] == "no" and s["origin"] == "pstore",
    ),
    (
        "a second owner has no store with an ssh or pstore origin",
        lambda s: s["multi_owner"] == "on" and s["origin"] in ("ssh", "pstore"),
    ),
    ("multiuser is posixv2's", lambda s: s["multiuser"] == "on" and s["origin"] != "posixv2"),
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
        "the connection broker needs origin-0 alone, in the full topology",
        lambda s: s["broker"] == "on"
        and (s["topology"] != "full" or "on" in (s["multi_origin"], s["multi_owner"])),
    ),
    (
        "cache tiering is the V2 cache's, in the full topology",
        lambda s: s["tiering"] == "on" and (s["cache"] != "v2" or s["topology"] != "full"),
    ),
]

# The shapes fed.sh accepts that smoke.sh does not run.
SMOKE_LIMITS: list[Rule] = [
    (
        "once the servers drop privileges, the host may not write their keys",
        lambda s: s["keys"] == "rotated" and s["privileges"] == "dropped",
    ),
]


def shape_of(
    presets: Iterable[str], rotated: bool = False
) -> tuple[Optional[Shape], list[str]]:
    """The shape that presets make, rotating every key or not, and what
    is wrong with the list: an unknown preset, or two that choose
    differently for one factor. The shape is None if anything is."""
    levels = {f: choices[0] for f, choices in FACTORS.items()}
    levels["keys"] = "rotated" if rotated else "kept"
    chosen_by: dict[str, str] = {}
    extras: set[str] = set()
    wrong: list[str] = []
    for preset in presets:
        if preset in EXTRAS:
            extras.add(preset)
            continue
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
    return Shape(levels, frozenset(extras)), []


def refusals(shape: Shape) -> list[str]:
    """Why fed.sh would refuse shape, or smoke.sh would not run it."""
    return [why for why, applies in REFUSED + SMOKE_LIMITS if applies(shape)]


def changes(factors: Sequence[str], depth: int) -> Iterator[dict[str, str]]:
    """Every way to move at most depth of factors away from their
    defaults, the fewest first."""
    for count in range(depth + 1):
        for chosen in itertools.combinations(factors, count):
            for levels in itertools.product(*(FACTORS[f][1:] for f in chosen)):
                yield dict(zip(chosen, levels))


def completion(fixed: Mapping[str, str]) -> Optional[Shape]:
    """A shape that smoke.sh could run, with no EXTRAS, that makes the
    fixed choices (levels by factor), with as few other factors away from
    their defaults as can be, and no more than SEARCH_DEPTH; or None."""
    defaults = {f: choices[0] for f, choices in FACTORS.items()}
    others = [f for f in FACTORS if f not in fixed]
    for change in changes(others, SEARCH_DEPTH):
        shape = Shape({**defaults, **fixed, **change}, frozenset())
        if not refusals(shape):
            return shape
    return None


def valid_pairs() -> set[Pair]:
    """Every pair of choices that some shape smoke.sh could run makes."""
    choices = [(f, level) for f, levels in FACTORS.items() for level in levels]
    found: set[Pair] = set()
    for a, b in itertools.combinations(choices, 2):
        if a[0] != b[0] and completion(dict([a, b])) is not None:
            found.add((a, b))
    return found


def covered(shapes: Iterable[Shape]) -> set[Pair]:
    """Every pair of choices that shapes make."""
    found: set[Pair] = set()
    for shape in shapes:
        found |= shape.pairs()
    return found


def grow(start: Pair, left: set[Pair]) -> Shape:
    """A shape that makes start (see completion()), and, for each other
    factor in turn, the level that covers the most pairs of left."""
    shape = completion(dict(start))
    if shape is None:
        raise ValueError(f"no shape makes {start}, though valid_pairs() says one does")
    levels = dict(shape.levels)
    for factor, choices in FACTORS.items():
        if factor in (start[0][0], start[1][0]):
            continue
        best, gain = levels[factor], -1
        for level in choices:
            trial = Shape({**levels, factor: level}, frozenset())
            if not refusals(trial) and len(trial.pairs() & left) > gain:
                best, gain = level, len(trial.pairs() & left)
        levels[factor] = best
    return Shape(levels, frozenset())


def suggest(existing: Iterable[Shape]) -> list[Shape]:
    """Shapes that cover every valid pair that existing shapes leave
    uncovered, picked greedily: of the shapes that grow() from each pair
    still uncovered, the one that covers the most of them."""
    left = valid_pairs() - covered(existing)
    picked: list[Shape] = []
    while left:
        grown = [grow(start, left) for start in sorted(left)]
        shape = max(grown, key=lambda s: len(s.pairs() & left))
        picked.append(shape)
        left -= shape.pairs()
    return picked


def presets_of(shape: Shape) -> list[str]:
    """The presets that make shape: one per factor not at its default."""
    by_choice = {choice: preset for preset, choice in PRESETS.items()}
    chosen = [
        by_choice[(f, shape[f])]
        for f, choices in FACTORS.items()
        if f != "keys" and shape[f] != choices[0]
    ]
    return chosen + sorted(shape.extras)


# --------------------------------------------------------------------------
# smoke.sh's shapes.


@dataclass(frozen=True)
class SmokeShape:
    """A shape that smoke.sh runs: its name, whether it rotates every key,
    and its presets."""

    name: str
    rotated: bool
    presets: tuple[str, ...]


def parse_table(text: str) -> list[SmokeShape]:
    """`smoke.sh --table`: `<name>\\t<yes|no>\\t<preset> ...` per shape,
    the middle column saying whether it rotates every key."""
    found: list[SmokeShape] = []
    for line in text.splitlines():
        if line.strip():
            name, rotated, presets = line.split("\t")
            found.append(SmokeShape(name, rotated == "yes", tuple(presets.split())))
    return found


def smoke_shapes() -> list[SmokeShape]:
    """The shapes that smoke.sh runs."""
    table = subprocess.run(  # nosec B603 B607
        ["sh", os.path.join(TOP, "smoke.sh"), "--table"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return parse_table(table)


def all_presets() -> set[str]:
    """Every preset in framework/presets/ but `default`."""
    return {
        name[: -len(".sh")]
        for name in os.listdir(os.path.join(TOP, "framework", "presets"))
        if name.endswith(".sh") and name != "default.sh"
    }


def made(shapes: Sequence[SmokeShape]) -> tuple[list[Shape], list[str]]:
    """The shapes that smoke.sh's make, and what is wrong with them."""
    found: list[str] = []
    names = [s.name for s in shapes]
    for name in sorted({n for n in names if names.count(n) > 1}):
        found.append(f"two shapes are named {name}")
    shapes_made: list[Shape] = []
    for smoke in shapes:
        shape, wrong = shape_of(smoke.presets, smoke.rotated)
        if shape is None:
            found += [f"{smoke.name}: {why}" for why in wrong]
            continue
        found += [f"{smoke.name}: {why}" for why in refusals(shape)]
        shapes_made.append(shape)
    return shapes_made, found


def check(shapes: Sequence[SmokeShape]) -> list[str]:
    """What is wrong with smoke.sh's shapes."""
    shapes_made, found = made(shapes)
    run = {p for s in shapes for p in s.presets}
    for preset in sorted(all_presets() - run - set(NOT_RUN)):
        found.append(f"no shape runs {preset}")
    for preset in sorted(all_presets() - set(PRESETS) - EXTRAS - set(NOT_RUN)):
        found.append(f"{preset} is not in framework/matrix.py")
    for (f1, l1), (f2, l2) in sorted(valid_pairs() - covered(shapes_made)):
        found.append(f"no shape has both {f1}={l1} and {f2}={l2}")
    return found


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    if args[0] == "check":
        shapes = smoke_shapes()
        found = check(shapes)
        for problem in found:
            print(problem)
        if found:
            sys.exit(1)
        print(f"{len(shapes)} shapes cover all {len(valid_pairs())} pairs")
    elif args[0] == "pairs":
        for (f1, l1), (f2, l2) in sorted(valid_pairs()):
            print(f"{f1}={l1} {f2}={l2}")
    elif args[0] == "suggest":
        for shape in suggest(made(smoke_shapes())[0]):
            rotated = "yes" if shape["keys"] == "rotated" else "no"
            print(f"{rotated}\t{' '.join(presets_of(shape))}")
    else:
        print(
            f"matrix.py: unknown command: {args[0]} (try: check, pairs, suggest)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
