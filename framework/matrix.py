#!/usr/bin/env python3
"""The choices that shape a federation, and whether smoke.sh's shapes
cover every pair of them.

  framework/matrix.py check     check smoke.sh's shapes (see below)
  framework/matrix.py pairs     list every pair of choices a shape can make
  framework/matrix.py suggest   print a list of shapes that covers them

Each preset makes one choice (see PRESETS); rotating every key, which
smoke.sh does after the tests in some shapes, is one more. A shape is
the choices its presets make, and every other factor's default. Some
shapes fed.sh refuses (REFUSED, which mirrors its checks), and some
smoke.sh does not run (SMOKE_LIMITS). Of the rest, every pair of choices
must be in some shape that smoke.sh runs.

`check` reads the shapes from `smoke.sh --table`, and reports any shape
that makes no sense or that fed.sh would refuse, any preset no shape
runs, and any pair no shape covers. It exits 1 if it found any. `suggest`
picks shapes greedily, for a person to trim and paste into smoke.sh.
"""

import sys

sys.dont_write_bytecode = True

import itertools  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import (Callable, Dict, FrozenSet, Iterable, Iterator, List, Mapping,  # noqa: E402
                    Optional, Sequence, Set, Tuple)

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Each factor's levels, its default first.
FACTORS: Dict[str, Tuple[str, ...]] = {
    "topology": ("full", "tiny"),
    "origin": ("posixv2", "xrootd", "pstore", "ssh", "httpsv2", "s3v2"),
    "cache": ("v2", "xrootd"),
    "multi_origin": ("off", "on"),
    "multi_cache": ("off", "on"),
    "multi_owner": ("off", "on"),
    "issuer": ("own", "external"),
    "direct": ("yes", "no"),
    "posc": ("off", "on"),
    "metadata": ("off", "eventual", "transactional"),
    "max_age": ("off", "on"),
    "multiuser": ("off", "on"),
    "privileges": ("root", "dropped"),
    "keys": ("kept", "rotated"),
}

# The choice each preset makes. `keys` is smoke.sh's, not a preset's.
PRESETS: Dict[str, Tuple[str, str]] = {
    "topo-basic": ("topology", "full"),
    "topo-tiny": ("topology", "tiny"),
    "origin-posixv2": ("origin", "posixv2"),
    "origin-xrootd": ("origin", "xrootd"),
    "origin-pstore": ("origin", "pstore"),
    "origin-ssh": ("origin", "ssh"),
    "origin-httpsv2": ("origin", "httpsv2"),
    "origin-s3v2": ("origin", "s3v2"),
    "cache-v2": ("cache", "v2"),
    "cache-xrootd": ("cache", "xrootd"),
    "topo-multi-origin": ("multi_origin", "on"),
    "topo-multi-cache": ("multi_cache", "on"),
    "topo-multi-owner": ("multi_owner", "on"),
    "auth-external-issuer": ("issuer", "external"),
    "origin-no-direct": ("direct", "no"),
    "origin-posc": ("posc", "on"),
    "origin-metadata": ("metadata", "eventual"),
    "origin-metadata-tx": ("metadata", "transactional"),
    "origin-max-age": ("max_age", "on"),
    "origin-multiuser": ("multiuser", "on"),
    "server-unprivileged": ("privileges", "dropped"),
}

# Presets that add a service no Pelican server uses, and so make no
# choice. Some shape must still run each.
EXTRAS: FrozenSet[str] = frozenset({"with-lab"})

# Presets smoke.sh does not run, and why.
NOT_RUN: Dict[str, str] = {
    "auth-oidc": "logging in needs a client registered with a real identity provider",
    "with-grafana": "Grafana has nothing provisioned to check",
}


@dataclass(frozen=True)
class Shape:
    levels: Mapping[str, str]  # every factor's level
    extras: FrozenSet[str]     # the EXTRAS it runs

    def __getitem__(self, factor: str) -> str:
        return self.levels[factor]

    def pairs(self) -> Set[Tuple[Tuple[str, str], Tuple[str, str]]]:
        choices = [(f, self.levels[f]) for f in FACTORS]
        return set(itertools.combinations(choices, 2))


Rule = Tuple[str, Callable[[Shape], bool]]

# The shapes fed.sh refuses: why, and whether a shape is one.
REFUSED: List[Rule] = [
    ("the 'tiny' topology has no room for the origin's backend",
     lambda s: s["topology"] == "tiny" and s["origin"] in ("ssh", "httpsv2", "s3v2")),
    ("the 'tiny' topology's cache is always V2",
     lambda s: s["topology"] == "tiny" and s["cache"] != "v2"),
    ("the 'tiny' topology has no room for another server",
     lambda s: s["topology"] == "tiny"
     and "on" in (s["multi_origin"], s["multi_cache"], s["multi_owner"])),
    ("the 'tiny' topology has no room for another service",
     lambda s: s["topology"] == "tiny" and bool(s.extras)),
    ("the 'tiny' topology has no discovery host to serve the external issuer",
     lambda s: s["topology"] == "tiny" and s["issuer"] == "external"),
    ("POSC and metadata publishing are posixv2's",
     lambda s: (s["posc"] == "on" or s["metadata"] != "off") and s["origin"] != "posixv2"),
    ("a pstore origin is seeded through its writes, which no-direct takes away",
     lambda s: s["direct"] == "no" and s["origin"] == "pstore"),
    ("a second owner has no store with an ssh or pstore origin",
     lambda s: s["multi_owner"] == "on" and s["origin"] in ("ssh", "pstore")),
    ("multiuser is posixv2's",
     lambda s: s["multiuser"] == "on" and s["origin"] != "posixv2"),
    ("multiuser needs root",
     lambda s: s["multiuser"] == "on" and s["privileges"] == "dropped"),
    ("an ssh origin's key is unreadable once the servers drop privileges",
     lambda s: s["privileges"] == "dropped" and s["origin"] == "ssh"),
]

# The shapes fed.sh accepts that smoke.sh does not run.
SMOKE_LIMITS: List[Rule] = [
    ("once the servers drop privileges, the host may not write their keys",
     lambda s: s["keys"] == "rotated" and s["privileges"] == "dropped"),
]


def shape_of(presets: Iterable[str], rotated: bool = False) -> Tuple[Optional[Shape], List[str]]:
    """The shape that presets make, rotating every key or not, and what
    is wrong with the list: an unknown preset, or two that choose
    differently for one factor. The shape is None if anything is."""
    levels = {f: levels[0] for f, levels in FACTORS.items()}
    levels["keys"] = "rotated" if rotated else "kept"
    chosen_by: Dict[str, str] = {}
    extras: Set[str] = set()
    problems: List[str] = []
    for preset in presets:
        if preset in EXTRAS:
            extras.add(preset)
            continue
        if preset not in PRESETS:
            why = NOT_RUN.get(preset)
            problems.append(f"{preset}: not run ({why})" if why else f"{preset}: no such preset")
            continue
        factor, level = PRESETS[preset]
        other = chosen_by.get(factor)
        if other and other != preset and PRESETS[other][1] != level:
            problems.append(f"{other} and {preset} both choose {factor}")
            continue
        chosen_by[factor] = preset
        levels[factor] = level
    if problems:
        return None, problems
    return Shape(levels, frozenset(extras)), []


def problems(shape: Shape) -> List[str]:
    """Why fed.sh would refuse shape, or smoke.sh would not run it."""
    return [why for why, applies in REFUSED + SMOKE_LIMITS if applies(shape)]


def every_shape() -> Iterator[Shape]:
    """Every shape that smoke.sh could run, with no EXTRAS."""
    for values in itertools.product(*FACTORS.values()):
        shape = Shape(dict(zip(FACTORS, values)), frozenset())
        if not problems(shape):
            yield shape


Pair = Tuple[Tuple[str, str], Tuple[str, str]]


def valid_pairs() -> Set[Pair]:
    """Every pair of choices that some shape smoke.sh could run makes."""
    found: Set[Pair] = set()
    for shape in every_shape():
        found |= shape.pairs()
    return found


def uncovered(shapes: Iterable[Shape]) -> Set[Pair]:
    covered: Set[Pair] = set()
    for shape in shapes:
        covered |= shape.pairs()
    return valid_pairs() - covered


def suggest() -> List[Shape]:
    """Shapes that cover every valid pair, picked greedily: each covers
    the most pairs still uncovered, the first such in every_shape's
    order."""
    candidates = [(shape, frozenset(shape.pairs())) for shape in every_shape()]
    left = set().union(*(pairs for _, pairs in candidates))
    picked: List[Shape] = []
    while left:
        shape, pairs = max(candidates, key=lambda c: len(c[1] & left))
        picked.append(shape)
        left -= pairs
    return picked


def presets_of(shape: Shape) -> List[str]:
    """The presets that make shape: one per factor not at its default."""
    by_choice = {choice: preset for preset, choice in PRESETS.items()}
    return [by_choice[(f, shape[f])] for f in FACTORS
            if f != "keys" and shape[f] != FACTORS[f][0]] + sorted(shape.extras)


#---------------------------------------------------------------------------
# smoke.sh's shapes.

@dataclass(frozen=True)
class SmokeShape:
    name: str
    rotated: bool
    presets: Tuple[str, ...]


def parse_table(text: str) -> List[SmokeShape]:
    """`smoke.sh --table`: `<name>\\t<yes|no>\\t<preset> ...` per shape,
    the middle column saying whether it rotates every key."""
    found = []
    for line in text.splitlines():
        if line.strip():
            name, rotated, presets = line.split("\t")
            found.append(SmokeShape(name, rotated == "yes", tuple(presets.split())))
    return found


def smoke_shapes() -> List[SmokeShape]:
    table = subprocess.run(["sh", os.path.join(TOP, "smoke.sh"), "--table"],
                           check=True, capture_output=True, text=True).stdout
    return parse_table(table)


def all_presets() -> Set[str]:
    """Every preset in framework/presets/ but `default`."""
    return {name[:-len(".sh")] for name in os.listdir(os.path.join(TOP, "framework", "presets"))
            if name.endswith(".sh") and name != "default.sh"}


def check(shapes: Sequence[SmokeShape]) -> List[str]:
    """What is wrong with smoke.sh's shapes."""
    found: List[str] = []
    names = [s.name for s in shapes]
    for name in sorted({n for n in names if names.count(n) > 1}):
        found.append(f"two shapes are named {name}")
    made: List[Shape] = []
    for smoke in shapes:
        shape, wrong = shape_of(smoke.presets, smoke.rotated)
        if shape is None:
            found += [f"{smoke.name}: {why}" for why in wrong]
            continue
        found += [f"{smoke.name}: {why}" for why in problems(shape)]
        made.append(shape)
    run = {p for s in shapes for p in s.presets}
    for preset in sorted(all_presets() - run - set(NOT_RUN)):
        found.append(f"no shape runs {preset}")
    for preset in sorted(all_presets() - set(PRESETS) - EXTRAS - set(NOT_RUN)):
        found.append(f"{preset} is not in framework/matrix.py")
    for (f1, l1), (f2, l2) in sorted(uncovered(made)):
        found.append(f"no shape has both {f1}={l1} and {f2}={l2}")
    return found


def main() -> None:
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
        for shape in suggest():
            rotated = "yes" if shape["keys"] == "rotated" else "no"
            print(f"{rotated}\t{' '.join(presets_of(shape))}")
    else:
        print(f"matrix.py: unknown command: {args[0]} (try: check, pairs, suggest)",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
