#!/usr/bin/env -S uv run --quiet --frozen --no-dev
"""A federation's choices, and which pairs of them smoke.sh's shapes make.

  framework/matrix.py check     check smoke.sh's shapes, and list the
                                pairs of choices that none makes
  framework/matrix.py pairs     list every pair of choices a shape can make

Each preset makes one choice (PRESETS); rotating every key, which
smoke.sh does after the tests in some shapes, is one more. A shape is
the choices its presets make, and every other factor's default. fed.sh
refuses some shapes (REFUSED, which mirrors its checks), and smoke.sh
does not run some (SMOKE_LIMITS), since they would test nothing, or
nothing that simpler shapes don't. A pair of choices is possible if
some shape that smoke.sh could run makes it with at most SEARCH_DEPTH
other factors away from their defaults: there are too many shapes to
try them all.

The shapes that smoke.sh runs, in framework/smoke-shapes.tsv, are picked
by hand, and need not make every possible pair. `check` reads them from
`smoke.sh --table`, and lists each preset that no shape runs and each
possible pair that no shape makes. It also reports any shape that makes
no sense, or that fed.sh would refuse or smoke.sh not run; any preset
that PRESETS lacks; and where REFUSED and fed.sh disagree. It exits 1
if it found any of these.

REFUSED must agree with fed.sh, so `check` asks `./fed.sh check` about
it (disagreements()): fed.sh must refuse each rule's witness, the
simplest shape that the rule alone refuses (witnesses()), and accept
each shape that smoke.sh is to run.
"""

import itertools
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Keep the checkout free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position
from testlib import common

# pylint: enable=wrong-import-position

TOP = os.path.dirname(common.FRAMEWORK)

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
    "broker": ("off", "on", "firewalled"),
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
    "auth-oidc-local": ("login", "oidc"),
    "origin-no-direct": ("direct", "no"),
    "origin-posc": ("posc", "on"),
    "origin-atomic-uploads": ("atomic", "on"),
    "origin-metadata": ("metadata", "eventual"),
    "origin-metadata-tx": ("metadata", "transactional"),
    "origin-max-age": ("max_age", "on"),
    "origin-multiuser": ("multiuser", "on"),
    "origin-broker": ("broker", "on"),
    "origin-firewalled": ("broker", "firewalled"),
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
# Each choice's preset.
PRESET_OF = {choice: preset for preset, choice in PRESETS.items()}

# Presets smoke.sh does not run, and why.
NOT_RUN: dict[str, str] = {
    "with-grafana": "Grafana has nothing provisioned to check",
    "auth-oidc-cilogon": "it needs a CILogon client of the user's own",
}

# The origins that run XRootD (fed_xrootd_origin in fed.sh), and those
# with a backend service.
XROOTD_ORIGINS = ("xrootd", "s3", "https")
BACKED_ORIGINS = ("ssh", "httpsv2", "s3v2", "s3", "https")
# The factors that add a server to the full topology.
ADDED_SERVERS = ("multi_origin", "multi_cache", "multi_owner", "multi_director", "site_local")
# The levels of other factors that add one.
ADDED_LEVELS = (("transfer_api", "server"),)

# How many factors besides a pair's completion() may change.
SEARCH_DEPTH = 3

# How many factors a rule's witness (witnesses()) may move from their
# defaults: as many as the rules of REFUSED need.
WITNESS_DEPTH = 4

Pair = tuple[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class Shape:
    """Every factor's level, by factor."""

    levels: Mapping[str, str]

    def __getitem__(self, factor: str) -> str:
        """Return factor's level."""
        return self.levels[factor]

    def pairs(self) -> set[Pair]:
        """Return every pair of choices it makes."""
        choices = [(f, self.levels[f]) for f in FACTORS]
        return set(itertools.combinations(choices, 2))

    def adds_server(self) -> bool:
        """Return whether it adds a server to the full topology."""
        return "on" in (self.levels[f] for f in ADDED_SERVERS) or any(
            self.levels[f] == level for f, level in ADDED_LEVELS
        )


Rule = tuple[str, Callable[[Shape], bool]]

# The shapes fed.sh refuses: why, and whether a shape is one. Each rule
# must refuse some shape that no other does (witnesses()).
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
        lambda s: s["broker"] != "off" and s["origin"] not in XROOTD_ORIGINS,
    ),
    (
        "the SSH tunnel is the ssh origin's",
        lambda s: s["ssh_tunnel"] == "on" and s["origin"] != "ssh",
    ),
    (
        "the connection broker needs origin-0 alone, in the full topology",
        lambda s: s["broker"] != "off"
        and (s["topology"] != "full" or "on" in (s["multi_origin"], s["multi_owner"])),
    ),
    (
        "a firewalled origin is reached only through the broker, which only an XRootD cache" + " can use",
        lambda s: s["broker"] == "firewalled" and s["cache"] != "xrootd",
    ),
    (
        "a firewalled origin's own issuer is out of the caches' reach, so the exports must"
        + " trust an issuer they can reach, or take no token",
        lambda s: s["broker"] == "firewalled" and s["issuer"] != "external" and s["namespaces"] != "public",
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
        lambda s: (s["memory_cache"] == "on" or s["store"] == "bounded") and s["topology"] == "standalone",
    ),
    (
        "short tokens and export templates are the origins' issuer's, which is off",
        lambda s: s["issuer"] != "own" and (s["tokens"] == "short" or s["templates"] == "export"),
    ),
    (
        "per-export templates go on /protected-b, which the shape does not export",
        lambda s: s["templates"] == "export"
        and (
            s["origin"] in ("https", "httpsv2")
            or s["direct"] == "no"
            or s["broker"] != "off"
            or s["namespaces"] == "public"
        ),
    ),
    (
        "a pstore origin's /public reads /protected-a's storage, which the shape does not export",
        lambda s: s["namespaces"] == "public" and s["origin"] == "pstore",
    ),
    (
        "another owner's two exports inside /public are one too many for an HTTPS backend",
        lambda s: s["namespaces"] == "public"
        and s["multi_owner"] == "on"
        and s["origin"] in ("https", "httpsv2"),
    ),
    (
        "another owner's /public/secret takes a token beside its public exports, which Pelican"
        + " refuses once the origins serve only caches",
        lambda s: s["namespaces"] == "public" and s["multi_owner"] == "on" and s["direct"] == "no",
    ),
    (
        "a load balancer balances two directors",
        lambda s: s["director_ha"] == "on" and s["multi_director"] == "off",
    ),
    (
        "writes to /public are given where it is exported: not beside /protected-a alone",
        lambda s: s["public_writes"] == "on"
        and s["namespaces"] == "all"
        and (s["origin"] in ("https", "httpsv2") or s["broker"] != "off"),
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
        lambda s: s["director_ha"] == "on" and s["broker"] != "off",
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
        lambda s: s["transfer_api"] != "off" and (s["direct"] == "no" or s["namespaces"] == "public"),
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


def shape_of(presets: Iterable[str], rotated: bool = False) -> tuple[Shape | None, list[str]]:
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


def completion(fixed: Mapping[str, str]) -> Shape | None:
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


def witnesses() -> dict[str, Shape]:
    """Return each rule of REFUSED's witness, by the rule's why.

    A rule's witness is a shape that the rule refuses and no other rule
    of REFUSED does, with as few factors away from their defaults as can
    be, and no more than WITNESS_DEPTH. A rule may have none.
    """
    defaults = {f: choices[0] for f, choices in FACTORS.items()}
    found: dict[str, Shape] = {}
    for change in changes(list(FACTORS), WITNESS_DEPTH):
        shape = Shape({**defaults, **change})
        refused = [why for why, applies in REFUSED if applies(shape)]
        if len(refused) == 1:
            found.setdefault(refused[0], shape)
            if len(found) == len(REFUSED):
                break
    return found


def covered(shapes: Iterable[Shape]) -> set[Pair]:
    """Return every pair of choices that shapes make."""
    found: set[Pair] = set()
    for shape in shapes:
        found |= shape.pairs()
    return found


def presets_of(shape: Shape) -> list[str]:
    """Return the presets that make shape: one per factor not at its default, but `keys`."""
    return [
        PRESET_OF[(f, shape[f])] for f, levels in FACTORS.items() if f != "keys" and shape[f] != levels[0]
    ]


# --------------------------------------------------------------------------
# fed.sh's own checks.


def fed_refusal(presets: Sequence[str]) -> str | None:
    """Return why `./fed.sh check` refuses presets, or None if it accepts them."""
    # `default` first, so that fed.sh never falls back on the presets it
    # remembers.
    args = [arg for preset in ("default", *presets) for arg in ("-p", preset)]
    result = subprocess.run(
        ["sh", os.path.join(TOP, "fed.sh"), *args, "check"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or f"exit status {result.returncode}"


def disagreements(accepted: Sequence[tuple[str, Sequence[str]]]) -> list[str]:
    """Return where REFUSED and `./fed.sh check` disagree.

    fed.sh must refuse each rule's witness (witnesses()), and accept
    each shape of accepted. A rule with no witness is reported too.

    Args:
        accepted: Shapes that REFUSED accepts: each one's name, and its
            presets.
    """
    found_witnesses = witnesses()
    found = [
        f"no shape that moves at most {WITNESS_DEPTH} factors is refused by this rule of REFUSED"
        + f" alone: {why}"
        for why, _ in REFUSED
        if why not in found_witnesses
    ]
    witness_presets = {why: presets_of(shape) for why, shape in found_witnesses.items()}
    with ThreadPoolExecutor() as pool:
        refusals_of_accepted = list(pool.map(fed_refusal, (presets for _, presets in accepted)))
        refusals_of_witnesses = list(pool.map(fed_refusal, witness_presets.values()))
    for (name, _), refusal in zip(accepted, refusals_of_accepted):
        if refusal is not None:
            found.append(f"{name}: fed.sh refuses it, though REFUSED does not ({refusal})")
    for (why, presets), refusal in zip(witness_presets.items(), refusals_of_witnesses):
        if refusal is None:
            found.append(f"{' '.join(presets)}: fed.sh accepts it, though REFUSED does not ({why})")
    return found


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


def parse_shapes(text: str) -> list[SmokeShape]:
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
    table = subprocess.run(
        ["sh", os.path.join(TOP, "smoke.sh"), "--table"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return parse_shapes(table)


def all_presets() -> set[str]:
    """Return every preset in framework/presets/ but `default`."""
    return {
        name[: -len(".sh")]
        for name in os.listdir(os.path.join(common.FRAMEWORK, "presets"))
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


def check(shapes: Sequence[SmokeShape], valid: set[Pair]) -> tuple[list[str], list[str], set[Pair]]:
    """Return what is wrong with smoke.sh's shapes, and what they leave out.

    Args:
        valid: valid_pairs().

    Returns:
        What is wrong: any shape that makes no sense, or that fed.sh
        would refuse or smoke.sh not run; any preset that PRESETS lacks;
        and where REFUSED and fed.sh disagree. Then each preset that no
        shape runs, but NOT_RUN's; and each pair of valid that no shape
        makes. A shape that is wrong runs nothing, and makes no pair.
    """
    shapes_made, found = made(shapes)
    accepted = [(smoke, shape) for smoke, shape in shapes_made if not refusals(shape)]
    for preset in sorted(all_presets() - set(PRESETS) - set(NOT_RUN)):
        found.append(f"{preset} is not in framework/matrix.py")
    found += disagreements([(smoke.name, smoke.presets) for smoke, _ in accepted])
    run = {p for smoke, _ in accepted for p in smoke.presets}
    unrun = sorted(all_presets() - run - set(NOT_RUN))
    return found, unrun, valid - covered(shape for _, shape in accepted)


def main() -> None:
    """Run the command that the arguments name."""
    args = sys.argv[1:]
    if args[:1] in ([], ["-h"], ["--help"]):
        print((__doc__ or "").strip("\n"))
        sys.exit(0 if args else 2)
    command, rest = args[0], args[1:]
    if command == "check" and not rest:
        shapes = smoke_shapes()
        valid = valid_pairs()
        found, unrun, missing = check(shapes, valid)
        for problem in found:
            print(problem)
        for preset in unrun:
            print(f"no shape runs {preset}")
        for (f1, l1), (f2, l2) in sorted(missing):
            print(f"no shape has both {f1}={l1} and {f2}={l2}")
        print(f"{len(shapes)} shapes make {len(valid) - len(missing)} of the {len(valid)} possible pairs")
        if found:
            sys.exit(1)
    elif command == "pairs" and not rest:
        for (f1, l1), (f2, l2) in sorted(valid_pairs()):
            print(f"{f1}={l1} {f2}={l2}")
    else:
        common.die_usage(f"unknown command: {' '.join(args)} (try: check, pairs)")


if __name__ == "__main__":
    main()
