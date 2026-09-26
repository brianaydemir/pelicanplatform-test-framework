import os
import stat
import tempfile
import unittest

from testlib import stores
from testlib.common import Origin

READS = frozenset({"Reads"})


class Seed(unittest.TestCase):
    def test_without_writes_goes_to_disk(self):
        with tempfile.TemporaryDirectory() as store:
            origin = Origin("origin-0", "https://origin-0:8444", store, "")
            self.assertIsNone(stores.seed(origin, "protected-a", "data/x/y/z", b"abc", "t", READS))
            with open(os.path.join(store, "data/x/y/z"), "rb") as f:
                self.assertEqual(f.read(), b"abc")
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, "data/x")).st_mode), 0o777)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, "data/x/y/z")).st_mode),
                             0o644)

    def test_everywhere_writes_each_store_once(self):
        with tempfile.TemporaryDirectory() as store:
            origins = [Origin("origin-0", "u0", store, ""), Origin("origin-1", "u1", store, "")]
            self.assertEqual(stores.seed_everywhere(origins, "protected-a", "a", b"1", "t", READS),
                             [])
            self.assertTrue(os.path.isfile(os.path.join(store, "a")))

    def test_not_a_pstore(self):
        origin = Origin("origin-0", "u", "nowhere", "pstore")
        self.assertIn("encrypted", stores.seed(origin, "protected-a", "a", b"", "t", READS))


if __name__ == "__main__":
    unittest.main()
