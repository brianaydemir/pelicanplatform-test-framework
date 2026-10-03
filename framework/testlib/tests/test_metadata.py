import os
import tempfile
import unittest

from suites import metadata


class Same(unittest.TestCase):
    def test_type_strict(self):
        for got, want in ((1, True), (True, 1), (1.0, 1), ("1", 1), (None, False)):
            self.assertFalse(metadata.same(got, want), (got, want))


class Delivered(unittest.TestCase):
    """Only a delivery of that object, answered with that status, counts."""

    def test_delivered(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as var:
            os.chdir(var)
            try:
                os.makedirs(metadata.CAPTURES)
                with open(f"{metadata.CAPTURES}/index.tsv", "w") as f:
                    f.write("3\t422\tobject.committed\t/protected-a/data/metadata/reject-r\te2\n")
                self.assertFalse(metadata.delivered("/protected-a/data/metadata/reject-r", "503"))
                self.assertFalse(metadata.delivered("/protected-a/data/metadata/other", "422"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
