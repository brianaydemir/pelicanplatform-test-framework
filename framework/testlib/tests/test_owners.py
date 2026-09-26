import unittest
from types import SimpleNamespace

from testlib import common, owners
from testlib.credentials import ALLOW, DENY
from testlib.owners import BY_NAME, CREDENTIALS, SIDES, Pair, applies, expected, verdict

PUBLIC = frozenset({"PublicReads", "Listings", "DirectReads"})
FULL = frozenset({"Reads", "Writes", "Listings", "DirectReads"})
READS = frozenset({"Reads"})

OWNERS = """\
origin-2 https://origin-2:8444 data/origin/2 issuer-keys/origin-2 https://origin-2:8444
"""
EXPORTS = """\
origin-2 /public/other public PublicReads,Listings,DirectReads https://origin-2:8444/api/v1.0/issuer/ns/public/other
origin-2 /other protected-a Reads,Writes,Listings,DirectReads https://origin-2:8444/api/v1.0/issuer/ns/other
"""


def fed(exports=None, owner_exports=EXPORTS):
    if exports is None:
        exports = {"public": PUBLIC, "protected-a": FULL, "protected-b": FULL}
    return SimpleNamespace(exports=exports, owners=common.parse_owners(OWNERS, owner_exports))


class ParseOwners(unittest.TestCase):
    def test_default(self):
        (owner,) = common.parse_owners(OWNERS, EXPORTS)
        self.assertEqual(owner.origin, common.Origin("origin-2", "https://origin-2:8444",
                                                     "data/origin/2", ""))
        self.assertEqual((owner.key_dir, owner.web_url), ("issuer-keys/origin-2",
                                                          "https://origin-2:8444"))
        public, other = owner.exports
        self.assertEqual((public.prefix, public.like, public.caps), ("/public/other", "public",
                                                                     PUBLIC))
        self.assertEqual((public.path, public.name), ("public/other", "public-other"))
        self.assertEqual((other.path, other.name, other.caps), ("other", "other", FULL))
        self.assertEqual(other.issuer, "https://origin-2:8444/api/v1.0/issuer/ns/other")

    def test_xrootd_and_no_direct(self):
        xrootd = OWNERS.replace("https://origin-2:8444 data", "https://origin-2:8443 data")
        exports = ("origin-2 /other protected-a Reads"
                   " https://origin-2:8444/api/v1.0/issuer/ns/other\n")
        (owner,) = common.parse_owners(xrootd, exports)
        self.assertEqual(owner.origin.url, "https://origin-2:8443")
        self.assertEqual(owner.web_url, "https://origin-2:8444")
        self.assertEqual([e.caps for e in owner.exports], [READS])

    def test_none(self):
        self.assertEqual(common.parse_owners("", ""), [])


class Table(unittest.TestCase):
    def test_names_are_unique(self):
        self.assertEqual(len(BY_NAME), len(CREDENTIALS))

    def test_only_each_sides_own_token_is_allowed_there(self):
        for side in SIDES:
            allowed = [c.name for c in CREDENTIALS if verdict(c, side) == ALLOW]
            self.assertEqual(allowed, [side])

    def test_pairs(self):
        self.assertEqual([p.export.prefix for p in owners.pairs(fed())], ["/public/other", "/other"])
        # A namespace the origins don't export has no pair.
        self.assertEqual([p.export.prefix for p in owners.pairs(fed({"protected-a": FULL}))],
                         ["/other"])

    def test_names(self):
        pair = owners.pairs(fed())[0]
        self.assertEqual((pair.namespace("owner"), pair.name("owner")),
                         ("public/other", "public-other"))
        self.assertEqual((pair.namespace("origins"), pair.name("origins")), ("public", "public"))

    def test_public_other(self):
        f = fed()
        pair = owners.pairs(f)[0]
        for cred in CREDENTIALS:
            for side in SIDES:
                for op in ("get", "head"):
                    self.assertEqual(expected(f, cred, pair, side, op, True), ALLOW)
                for op in ("put", "delete"):
                    self.assertEqual(expected(f, cred, pair, side, op, False), DENY)
        self.assertEqual([c.name for c in CREDENTIALS if applies(c, pair)],
                         ["owner", "origins", "none"])

    def test_no_direct(self):
        exports = "origin-2 /other protected-a Reads https://origin-2:8444/api/v1.0/issuer/ns/other\n"
        f = fed({"protected-a": READS}, exports)
        (pair,) = owners.pairs(f)
        owner = BY_NAME["owner"]
        self.assertEqual(expected(f, owner, pair, "owner", "get", False), ALLOW)
        self.assertEqual(expected(f, owner, pair, "owner", "get", True), DENY)
        self.assertEqual(expected(f, owner, pair, "owner", "put", False), DENY)

    def test_cross(self):
        f = fed()
        pair = owners.pairs(f)[1]
        self.assertIsInstance(pair, Pair)
        self.assertEqual(expected(f, BY_NAME["owner"], pair, "owner", "put", True), ALLOW)
        self.assertEqual(expected(f, BY_NAME["owner"], pair, "origins", "get", False), DENY)
        self.assertEqual(expected(f, BY_NAME["origins"], pair, "owner", "get", False), DENY)
        self.assertEqual(expected(f, BY_NAME["origins"], pair, "origins", "put", True), ALLOW)


if __name__ == "__main__":
    unittest.main()
