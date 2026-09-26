import unittest
from types import SimpleNamespace

from suites import SUITES
from testlib import credentials, transfers
from testlib.common import Origin


class Interface(unittest.TestCase):
    def test_order(self):
        self.assertEqual(list(SUITES), ["federation", "commands", "listings", "blocks", "posc",
                                        "metadata", "auth", "transfers"])

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

    def test_auth_and_transfers_cover_the_tables(self):
        auth = SUITES["auth"].SCENARIOS
        self.assertEqual(list(auth), ["keys", *(c.name for c in credentials.CREDENTIALS), "narrow"])
        self.assertEqual(list(SUITES["transfers"].SCENARIOS),
                         [s.name for s in transfers.SCENARIOS])


class Skip(unittest.TestCase):
    @staticmethod
    def fed(posc=False, kind="", metadata="off"):
        return SimpleNamespace(posc=posc, metadata=metadata,
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

    def test_the_rest_always_apply(self):
        for name in ("federation", "commands", "listings", "blocks", "auth", "transfers"):
            self.assertIsNone(SUITES[name].skip(self.fed()), name)
