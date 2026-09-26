import unittest
from types import SimpleNamespace

from suites import SUITES
from testlib import credentials, transfers
from testlib.common import Origin


class Interface(unittest.TestCase):
    def test_order(self):
        self.assertEqual(list(SUITES), ["federation", "commands", "listings", "blocks", "posc",
                                        "metadata", "users", "owners", "auth", "transfers"])

    def test_every_suite_has_scenarios_skip_and_run(self):
        for name, module in SUITES.items():
            self.assertTrue(module.SCENARIOS, name)
            self.assertTrue(all(isinstance(d, str) and d for d in module.SCENARIOS.values()), name)
            self.assertTrue(callable(module.skip) and callable(module.run), name)
            self.assertLessEqual(set(getattr(module, "REQUIRED", ())), set(module.SCENARIOS), name)

    def test_only_federation_is_required(self):
        self.assertEqual([n for n, m in SUITES.items() if getattr(m, "REQUIRED", ())],
                         ["federation"])

    def test_each_scenario_runs_something(self):
        for name, module in SUITES.items():
            run = getattr(module, "RUN", None)
            if run is None:
                continue
            missing = set(module.SCENARIOS) - set(run)
            # posc runs `stalled` and `sized` from one upload.
            if name == "posc":
                missing -= {"stalled", "sized"}
            self.assertFalse(missing, name)
            self.assertLessEqual(set(run), set(module.SCENARIOS), name)

    def test_owners_covers_its_table(self):
        from testlib import owners
        self.assertEqual(list(SUITES["owners"].SCENARIOS),
                         ["ready", "keys", "routing", *(c.name for c in owners.CREDENTIALS),
                          *SUITES["owners"].CLIENT])

    def test_auth_and_transfers_cover_the_tables(self):
        auth = SUITES["auth"].SCENARIOS
        self.assertEqual(list(auth), ["keys", *(c.name for c in credentials.CREDENTIALS), "narrow"])
        self.assertEqual(list(SUITES["transfers"].SCENARIOS),
                         [s.name for s in transfers.SCENARIOS])


FULL = frozenset({"Reads", "Writes", "Listings", "DirectReads"})
PUBLIC = frozenset({"PublicReads", "Listings", "DirectReads"})


class Skip(unittest.TestCase):
    @staticmethod
    def fed(posc=False, kind="", metadata="off", exports=None, multiuser=False,
            drop_privileges=False, variant="posixv2"):
        if exports is None:
            exports = {"public": PUBLIC, "protected-a": FULL, "protected-b": FULL}
        return SimpleNamespace(posc=posc, metadata=metadata, exports=exports,
                               multiuser=multiuser, drop_privileges=drop_privileges,
                               origin_variant=variant, owners=[],
                               origins=[Origin("origin-0", "https://origin-0:8443", "s", kind)])

    def test_posc(self):
        posc = SUITES["posc"]
        self.assertIsNotNone(posc.skip(self.fed()))
        self.assertIsNone(posc.skip(self.fed(posc=True)))
        self.assertIsNone(posc.skip(self.fed(kind="pstore")))

    def test_metadata(self):
        metadata = SUITES["metadata"]
        self.assertIsNotNone(metadata.skip(self.fed()))
        self.assertIsNone(metadata.skip(self.fed(metadata="eventual")))
        self.assertIsNone(metadata.skip(self.fed(metadata="transactional")))

    def test_posc_and_metadata_need_writes(self):
        reads = {"protected-a": frozenset({"Reads"})}
        self.assertIsNotNone(SUITES["posc"].skip(self.fed(posc=True, exports=reads)))
        self.assertIsNotNone(SUITES["metadata"].skip(self.fed(metadata="eventual", exports=reads)))

    def test_users(self):
        users = SUITES["users"]
        self.assertIsNotNone(users.skip(self.fed()))
        self.assertIsNone(users.skip(self.fed(multiuser=True)))
        self.assertIsNone(users.skip(self.fed(drop_privileges=True)))
        self.assertIsNotNone(users.skip(self.fed(drop_privileges=True, variant="s3v2")))
        reads = {"protected-a": frozenset({"Reads"})}
        self.assertIsNotNone(users.skip(self.fed(multiuser=True, exports=reads)))

    def test_owners(self):
        from testlib.common import parse_owners
        from testlib.tests.test_owners import EXPORTS, OWNERS
        suite = SUITES["owners"]
        self.assertIsNotNone(suite.skip(self.fed()))
        fed = self.fed()
        fed.owners = parse_owners(OWNERS, EXPORTS)
        self.assertIsNone(suite.skip(fed))
        fed.exports = {"protected-b": FULL}
        self.assertIsNotNone(suite.skip(fed))

    def test_the_rest_apply_with_protected_a(self):
        for name in ("federation", "commands", "listings", "blocks", "auth", "transfers"):
            self.assertIsNone(SUITES[name].skip(self.fed()), name)
            self.assertIsNone(SUITES[name].skip(self.fed(exports={"protected-a": FULL})), name)

    def test_some_need_protected_a(self):
        fed = self.fed(exports={"public": PUBLIC, "protected-b": FULL})
        for name in ("commands", "listings", "blocks"):
            self.assertIsNotNone(SUITES[name].skip(fed), name)
        for name in ("federation", "auth", "transfers"):
            self.assertIsNone(SUITES[name].skip(fed), name)
