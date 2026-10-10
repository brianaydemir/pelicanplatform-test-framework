"""Two owners' namespaces side by side (`topo-multi-owner`): which
credentials each should accept, for the owners suite.

Another owner's origin (origin-2; see common.Owner) exports prefixes
that mirror the origins' namespaces: /other is like /protected-a, and
/public/other like /public, nested in it. Each such pair has two sides:
`owner`, the other owner's prefix, and `origins`, the origins' namespace.
Each credential below is presented on both sides of each pair. Only a
token that one side's own key signs and that names that side's own
issuer should be allowed anything a token decides, and only on that
side: the owners' keys and issuers are theirs alone. What the side's
capabilities then allow comes from credentials.decide(), as for the
origins' own credentials.
"""

import glob
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Optional

from . import common, credentials
from .credentials import ALLOW, DENY

SIDES = ("owner", "origins")


@dataclass(frozen=True)
class OwnerCredential:
    """A credential that the owners suite presents."""

    name: str
    key: Optional[str]  # whose key signs it: a side, `jwks`, or `ns-jwks`; None: no token
    issuer: str  # whose issuer it names: a side
    description: str


CREDENTIALS = (
    OwnerCredential("owner", "owner", "owner", "the other owner's key and issuer"),
    OwnerCredential("origins", "origins", "origins", "the origins' key and issuer"),
    OwnerCredential(
        "owner-key", "owner", "origins", "the other owner's key, naming the origins' issuer"
    ),
    OwnerCredential(
        "origins-key", "origins", "owner", "the origins' key, naming the other owner's issuer"
    ),
    OwnerCredential(
        "jwks",
        "jwks",
        "owner",
        "a key the origins list in Server.IssuerJwks, naming the other owner's issuer",
    ),
    OwnerCredential(
        "ns-jwks",
        "ns-jwks",
        "owner",
        "a key an origins' export lists in its IssuerJwks, naming the other owner's issuer",
    ),
    OwnerCredential("none", None, "owner", "no token"),
)

BY_NAME = {c.name: c for c in CREDENTIALS}


@dataclass(frozen=True)
class Pair:
    """Another owner's export, and the origins' namespace it mirrors."""

    owner: common.Owner
    export: common.Export

    @property
    def like(self) -> str:
        """The origins' namespace that the export mirrors."""
        return self.export.like

    def namespace(self, side: str) -> str:
        """The side's namespace, as URLs name it: e.g. `other`, or
        `protected-a`."""
        return self.export.path if side == "owner" else self.like

    def name(self, side: str) -> str:
        """The side's namespace, as rows and files name it."""
        return self.export.name if side == "owner" else self.like


def pairs(fed: common.Federation) -> list[Pair]:
    """Every export of another owner whose namespace the origins also
    export."""
    return [
        Pair(owner, export)
        for owner in fed.owners
        for export in owner.exports
        if export.like in fed.exports
    ]


def caps(fed: common.Federation, pair: Pair, side: str) -> AbstractSet[str]:
    """The capabilities of side of pair."""
    return pair.export.caps if side == "owner" else fed.exports[pair.like]


def verdict(cred: OwnerCredential, side: str) -> str:
    """What the side should do with cred where a token decides."""
    return ALLOW if cred.key == side and cred.issuer == side else DENY


def expected(
    fed: common.Federation, cred: OwnerCredential, pair: Pair, side: str, op: str, direct: bool
) -> str:
    """What should become of op on side of pair, presenting cred: ALLOW or
    DENY (see credentials.decide())."""
    return credentials.decide(verdict(cred, side), caps(fed, pair, side), op, direct)


def applies(cred: OwnerCredential, pair: Pair) -> bool:
    """Whether the suite presents cred in pair. /public and /public/other
    have no issuer, and a token decides nothing there: only a token of
    each owner, and none, are presented."""
    return pair.like in credentials.PROTECTED or cred.name in ("owner", "origins", "none")


def key_path(fed: common.Federation, cred: OwnerCredential, pair: Pair) -> str:
    """The key that signs cred's token, under framework/var."""
    if cred.key == "owner":
        return pair.owner.key
    if cred.key == "origins":
        return fed.origin_key
    if cred.key == "jwks":
        return "test-keys/jwks.pem"
    return f"test-keys/ns-{pair.like}.pem"


def issuer_url(fed: common.Federation, cred: OwnerCredential, pair: Pair) -> str:
    """The issuer URL that cred's token names."""
    return pair.export.issuer if cred.issuer == "owner" else fed.issuer_of(pair.like)


def mint(
    fed: common.Federation, cred: OwnerCredential, pair: Pair, side: str, out: str
) -> Optional[str]:
    """Write cred's token for side of pair to file out, and return it; None
    for `none`. Its scopes are for reading and writing the whole
    namespace, so only its key and issuer decide."""
    if cred.key is None:
        return None
    credentials.mint(
        fed,
        out,
        f"/{pair.namespace(side)}/",
        key_path(fed, cred, pair),
        issuer_url(fed, cred, pair),
        "/",
        credentials.LONG,
        credentials.RWM,
    )
    with open(out, encoding="utf-8") as f:
        return f.read().strip()


def key_ids(key_dir: str) -> tuple[str, ...]:
    """The kids of the keys in key_dir (under framework/var): the .jwks
    beside each .pem (see fed_key_make in fed.sh)."""
    found: list[str] = []
    for pem in sorted(glob.glob(f"{key_dir}/*.pem")):
        try:
            with open(pem[: -len(".pem")] + ".jwks", "rb") as f:
                found += sorted(credentials.key_ids(f.read()))
        except (FileNotFoundError, ValueError):
            continue
    return tuple(found)
