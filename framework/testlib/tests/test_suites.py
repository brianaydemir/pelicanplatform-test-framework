import unittest
from types import SimpleNamespace

from suites import SUITES
from testlib.common import Origin, parse_owners
from testlib.tests.test_owners import EXPORTS, OWNERS

FULL = frozenset({"Reads", "Writes", "Listings", "DirectReads"})
PUBLIC = frozenset({"PublicReads", "Listings", "DirectReads"})


def fed(posc=False, kind="", metadata="off", exports=None, multiuser=False,
        drop_privileges=False, owners=False):
    if exports is None:
        exports = {"public": PUBLIC, "protected-a": FULL, "protected-b": FULL}
    return SimpleNamespace(posc=posc, metadata=metadata, exports=exports,
                           multiuser=multiuser, drop_privileges=drop_privileges,
                           origin_variant="posixv2",
                           owners=parse_owners(OWNERS, EXPORTS) if owners else [],
                           origins=[Origin("origin-0", "https://origin-0:8443", "s", kind)])


class Skip(unittest.TestCase):
    """A suite that skips a shape it should test passes there, having
    tested nothing."""

    def runs(self, name, *feds):
        for f in feds:
            self.assertIsNone(SUITES[name].skip(f), name)

    def test_features(self):
        self.runs("posc", fed(posc=True), fed(kind="pstore"))
        self.runs("metadata", fed(metadata="eventual"), fed(metadata="transactional"))
        self.runs("users", fed(multiuser=True), fed(drop_privileges=True))
        self.runs("owners", fed(owners=True))

    def test_the_rest(self):
        only_a = {"protected-a": FULL}
        without_a = {"public": PUBLIC, "protected-b": FULL}
        for name in ("federation", "commands", "listings", "blocks", "auth", "transfers"):
            self.runs(name, fed(), fed(exports=only_a))
        for name in ("federation", "auth", "transfers"):
            self.runs(name, fed(exports=without_a))
