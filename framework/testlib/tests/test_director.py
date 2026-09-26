import unittest

from testlib import director


class Link(unittest.TestCase):
    def test_by_priority(self):
        value = ('<https://cache-1:8444/private/x?authz=a,b>; rel="duplicate"; pri=2; depth=1, '
                 '<https://cache-0:8444/private/x>; rel="duplicate"; pri=1; depth=1, '
                 '<https://elsewhere/x>; rel="alternate"; pri=0')
        self.assertEqual(director.parse_link(value),
                         ["https://cache-0:8444/private/x", "https://cache-1:8444/private/x?authz=a,b"])
        self.assertEqual(director.parse_link(""), [])

    def test_answer(self):
        answer = director.Answer(307, "cache-0", ["cache-0", "cache-1"])
        self.assertTrue(answer.names("cache-1"))
        self.assertFalse(answer.names("cache-2"))
        self.assertEqual(director.host("https://cache-1:8444/x"), "cache-1")
