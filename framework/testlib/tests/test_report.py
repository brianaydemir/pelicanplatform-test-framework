import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

import report as converter
from testlib import common, report
from testlib.report import FAIL, PASS, Report


class Failures(unittest.TestCase):
    """A failure that goes unrecorded, or uncounted, passes."""

    def test_a_suite_that_stops_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            for suite, cases in (("dying", ()), ("stopped", ("one",))):
                r = Report(suite, tmp)
                for case in cases:
                    r.add(case, PASS)
                r.write("it stopped")
                with open(os.path.join(tmp, f"{suite}.tsv")) as f:
                    rows = report.parse_table(f.read())
                self.assertEqual(rows[-1]["status"], FAIL, suite)
                self.assertEqual(r.failed, 1, suite)

    def test_unknown_status(self):
        with self.assertRaises(ValueError):
            Report("suite", "/nonexistent").add("x", "FAILED")

    def test_junit(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "default"))
            with open(os.path.join(tmp, "default", "x.tsv"), "w") as f:
                f.write(report.format_table(report.COLUMNS, [
                    {"suite": "transfers", "case": c, "status": s, "seconds": "", "note": ""}
                    for c, s in (("exist", PASS), ("dne", FAIL))]))
            merged = os.path.join(tmp, "results.tsv")
            with open(merged, "w") as f:
                f.write(converter.merge([os.path.join(tmp, "default")]))
            top = ET.fromstring(converter.junit(converter.load(merged)))
        self.assertEqual(top.get("failures"), "1")
        self.assertIsNotNone(top.find("testsuite/testcase[@name='dne']/failure"))


class Select(unittest.TestCase):
    def test_everything(self):
        suites = {"federation": ["ready", "caches"], "auth": ["keys", "server", "narrow"]}
        self.assertEqual(common.select([], suites), suites)


if __name__ == "__main__":
    unittest.main()
