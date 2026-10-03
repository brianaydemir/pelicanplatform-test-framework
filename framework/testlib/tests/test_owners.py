import unittest
from types import SimpleNamespace

from testlib import common, owners

PUBLIC = frozenset({"PublicReads", "Listings", "DirectReads"})
FULL = frozenset({"Reads", "Writes", "Listings", "DirectReads"})

OWNERS = """\
origin-2 https://origin-2:8444 data/origin/2 issuer-keys/origin-2 https://origin-2:8444
"""
EXPORTS = """\
origin-2 /public/other public PublicReads,Listings,DirectReads https://origin-2:8444/api/v1.0/issuer/ns/public/other
origin-2 /other protected-a Reads,Writes,Listings,DirectReads https://origin-2:8444/api/v1.0/issuer/ns/other
"""


def fed(exports=None):
    if exports is None:
        exports = {"public": PUBLIC, "protected-a": FULL, "protected-b": FULL}
    return SimpleNamespace(exports=exports, owners=common.parse_owners(OWNERS, EXPORTS))


class Pairs(unittest.TestCase):
    """A pair left out is never checked."""

    def test_pairs(self):
        self.assertEqual([p.export.prefix for p in owners.pairs(fed())], ["/public/other", "/other"])
        # A namespace the origins don't export has no pair.
        self.assertEqual([p.export.prefix for p in owners.pairs(fed({"protected-a": FULL}))],
                         ["/other"])


if __name__ == "__main__":
    unittest.main()
