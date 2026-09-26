import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

import report as converter
from testlib import common, report
from testlib.report import FAIL, INCONCLUSIVE, PASS, SKIP, Report


class Table(unittest.TestCase):
    def test_escape_round_trip(self):
        for text in ("plain", "tab\there", "two\nlines", "back\\slash\\t", ""):
            self.assertEqual(report.unescape(report.escape(text)), text)
            self.assertNotIn("\t", report.escape(text))
            self.assertNotIn("\n", report.escape(text))

    def test_format_and_parse(self):
        rows = [{"suite": "s", "case": "c", "status": PASS, "seconds": "1.0", "note": "a\tb"}]
        text = report.format_table(report.COLUMNS, rows)
        self.assertEqual(text.splitlines()[0], "\t".join(report.COLUMNS))
        self.assertEqual(report.parse_table(text), rows)

    def test_slug(self):
        self.assertEqual(report.slug("transfers (rotated keys)"), "transfers-rotated-keys")


class Writing(unittest.TestCase):
    def test_rows_and_setup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Report("suite", tmp)
            r.add("one", PASS, "fine")
            r.add("two", FAIL, "broken", seconds=None)
            r.write()
            with open(os.path.join(tmp, "suite.tsv")) as f:
                rows = report.parse_table(f.read())
            self.assertEqual([(x["case"], x["status"]) for x in rows],
                             [("one", PASS), ("two", FAIL)])
            self.assertEqual(rows[1]["seconds"], "")
            self.assertEqual((r.failed, r.count(PASS), r.count(SKIP)), (1, 1, 0))

            dying = Report("dying", tmp)
            dying.write("the federation is down")
            with open(os.path.join(tmp, "dying.tsv")) as f:
                rows = report.parse_table(f.read())
            self.assertEqual([(x["case"], x["status"], x["note"]) for x in rows],
                             [("setup", FAIL, "the federation is down")])

            r.write("it stopped")
            with open(os.path.join(tmp, "suite.tsv")) as f:
                rows = report.parse_table(f.read())
            self.assertEqual((rows[-1]["case"], rows[-1]["status"]), ("aborted", FAIL))

    def test_tag(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.dict(os.environ, {"RESULTS_TAG": "rotated-keys"}):
            r = Report("auth", tmp)
            r.write()
            self.assertEqual(r.suite, "auth-rotated-keys")
            self.assertEqual(os.listdir(tmp), ["auth-rotated-keys.tsv"])

    def test_unknown_status(self):
        with self.assertRaises(ValueError):
            Report("suite", "/nonexistent").add("x", "MAYBE")


class Converting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        for shape, rows in (("default", [("transfers", "exist", PASS, "ok"),
                                         ("transfers", "dne", FAIL, "a | b")]),
                            ("pstore", [("blocks", "integrity", SKIP, "no pstore"),
                                        ("posc", "client", INCONCLUSIVE, "too fast")])):
            os.makedirs(os.path.join(base, shape))
            table = report.format_table(report.COLUMNS, [
                {"suite": s, "case": c, "status": st, "seconds": "2.0", "note": n}
                for s, c, st, n in rows])
            with open(os.path.join(base, shape, "x.tsv"), "w") as f:
                f.write(table)
        self.merged = os.path.join(base, "results.tsv")
        with open(self.merged, "w") as f:
            f.write(converter.merge([os.path.join(base, "default"),
                                     os.path.join(base, "pstore") + "/"]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge(self):
        rows = converter.load(self.merged)
        self.assertEqual([r["shape"] for r in rows], ["default", "default", "pstore", "pstore"])

    def test_junit(self):
        top = ET.fromstring(converter.junit(converter.load(self.merged)))
        self.assertEqual((top.get("tests"), top.get("failures"), top.get("skipped")),
                         ("4", "1", "2"))
        suites = {s.get("name"): s for s in top.iter("testsuite")}
        self.assertEqual(set(suites), {"default/transfers", "pstore/blocks", "pstore/posc"})
        failure = suites["default/transfers"].find("testcase[@name='dne']/failure")
        self.assertIsNotNone(failure)
        self.assertEqual(failure.get("message"), "a | b")
        skipped = suites["pstore/posc"].find("testcase/skipped")
        self.assertIsNotNone(skipped)
        self.assertTrue((skipped.get("message") or "").startswith("inconclusive"))

    def test_markdown(self):
        text = converter.markdown(converter.load(self.merged))
        self.assertIn("| default | transfers | 1 | 1 | 0 | 0 |", text)
        self.assertIn("**default / transfers / dne**: a \\| b", text)


class Select(unittest.TestCase):
    SUITES = {"federation": ["ready", "caches"],
              "transfers": ["exist", "direct-exist", "plugin-exist", "plugin-direct-exist"],
              "auth": ["keys", "server", "narrow"]}

    def select(self, *args):
        return common.select(list(args), self.SUITES)

    def test_everything(self):
        self.assertEqual(self.select(), self.SUITES)

    def test_suites_and_patterns(self):
        self.assertEqual(self.select("auth"), {"auth": ["keys", "server", "narrow"]})
        self.assertEqual(self.select("transfers/*direct-*"),
                         {"transfers": ["direct-exist", "plugin-direct-exist"]})
        self.assertEqual(self.select("*/ready", "*/keys"),
                         {"federation": ["ready"], "auth": ["keys"]})

    def test_order_is_the_suites_and_each_once(self):
        self.assertEqual(self.select("auth/narrow", "transfers/exist", "auth/keys", "auth"),
                         {"transfers": ["exist"], "auth": ["keys", "server", "narrow"]})

    def test_nothing_matches(self):
        for arg in ("nope", "nope/ready", "auth/nope", "transfers/nope-*"):
            with self.assertRaises(SystemExit) as caught, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.select(arg)
            self.assertEqual(caught.exception.code, 2, arg)
