import unittest
from typing import Optional

from testlib import webdav


def entries(*paths, collection=False, size: Optional[int] = 1):
    return [webdav.Entry(p, collection, size) for p in paths]


class Relative(unittest.TestCase):
    """What isn't in the tree once, under its own path, mustn't count as
    part of it."""

    def test_refused(self):
        for listing in (
                # Under another path that merely contains the base.
                entries("/protected-a/x", "/elsewhere/protected-a/x/c"),
                # A name that only begins with the base's.
                entries("/protected-a/x", "/protected-a/xy"),
                # The base twice.
                entries("/protected-a/x", "/p/protected-a/x"),
                # An entry twice.
                entries("/protected-a/x", "/protected-a/x/c", "/protected-a/x/c")):
            with self.assertRaises(ValueError, msg=[e.path for e in listing]):
                webdav.relative(listing, "/protected-a/x")


class Problems(unittest.TestCase):
    def test_differences(self):
        got = webdav.relative(entries("/t", "/t/a", "/t/sub") + entries("/t/dir", collection=True),
                              "/t")
        for want in ({"a": 1, "sub": 1, "dir": None, "c": 1},  # missing c
                     {"a": 1, "dir": None},                     # also lists sub
                     {"a": 2, "sub": 1, "dir": None},           # the wrong size
                     {"a": 1, "sub": None, "dir": None},        # not a collection
                     {"a": 1, "sub": 1, "dir": 0}):             # a collection
            self.assertTrue(webdav.problems(want, got), want)

    def test_missing_size(self):
        got = webdav.relative(entries("/t/empty", size=None), "/t")
        self.assertTrue(webdav.problems({"empty": 0}, got))


if __name__ == "__main__":
    unittest.main()
