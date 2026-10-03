import unittest
from collections import Counter

from suites import commands

ARG = "pelican://fed:8444/protected-a/data/cmd/r/tree"
ROOT = "/protected-a/data/cmd/r/tree"


class NoToken(unittest.TestCase):
    """Only the client's own refusal counts, not a server's."""

    def test_no_token_refusal(self):
        self.assertIsNotNone(commands.no_token_refusal(0, "", ""))
        self.assertIsNotNone(commands.no_token_refusal(1, "", "HTTP 403: forbidden"))


class Listing(unittest.TestCase):
    """A listing that flattens, repeats, or strays from the tree must not
    match it."""

    def test_nesting_and_duplicates_count(self):
        flat = [{"Name": f"{ROOT}/c", "Size": 300}]
        nested = [{"Name": f"{ROOT}/sub/c", "Size": 300}]
        self.assertNotEqual(commands.tree_objects(flat, ROOT), commands.tree_objects(nested, ROOT))
        self.assertNotEqual(commands.tree_objects(nested * 2, ROOT), Counter({("sub/c", 300): 1}))

    def test_outside_the_tree(self):
        self.assertEqual(commands.tree_rel("/elsewhere/a", ROOT), "/elsewhere/a")
        self.assertEqual(commands.tree_rel(f"{ROOT}x/a", ROOT), f"{ROOT}x/a")


class Du(unittest.TestCase):
    def test_outside_the_tree(self):
        self.assertEqual(commands.du_key("/elsewhere", ARG), "/elsewhere")
        # The tree's path, not merely its prefix.
        self.assertEqual(commands.du_key(f"{ROOT}x/sub", ARG), f"{ROOT}x/sub")

    def test_duplicate_rows(self):
        with self.assertRaisesRegex(ValueError, "two rows"):
            commands.du_counts([(ARG, 1, 1, 0), (ROOT, 1, 1, 0)], ARG)


if __name__ == "__main__":
    unittest.main()
