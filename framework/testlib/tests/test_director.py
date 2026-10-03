import unittest

from testlib import director


class Link(unittest.TestCase):
    """A server left out of the Link header can't be found where it
    shouldn't be."""

    def test_every_duplicate(self):
        value = ('<https://cache-1:8444/protected-a/x?authz=a,b>; rel="duplicate"; pri=2; depth=1, '
                 '<https://cache-0:8444/protected-a/x>; rel="duplicate"; pri=1; depth=1')
        self.assertEqual(sorted(director.parse_link(value)),
                         ["https://cache-0:8444/protected-a/x",
                          "https://cache-1:8444/protected-a/x?authz=a,b"])


if __name__ == "__main__":
    unittest.main()
