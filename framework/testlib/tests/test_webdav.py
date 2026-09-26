import unittest

from testlib import webdav

# As golang.org/x/net/webdav (the native origins) writes it.
NATIVE = b"""<?xml version="1.0" encoding="UTF-8"?><D:multistatus xmlns:D="DAV:">
<D:response><D:href>/private/data/x/tree/</D:href><D:propstat><D:prop>
<D:resourcetype><D:collection xmlns:D="DAV:"/></D:resourcetype></D:prop>
<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/private/data/x/tree/a%20b</D:href><D:propstat><D:prop>
<D:resourcetype></D:resourcetype><D:getcontentlength>1000</D:getcontentlength></D:prop>
<D:status>HTTP/1.1 200 OK</D:status></D:propstat>
<D:propstat><D:prop><D:getetag/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>
</D:response>
<D:response><D:href>/private/data/x/tree/sub/</D:href><D:propstat><D:prop>
<D:resourcetype><D:collection/></D:resourcetype></D:prop>
<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
</D:multistatus>"""

# As the V2 cache synthesizes it from its metadata: no length for 0 bytes.
CACHE = b"""<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/private/data/x/empty</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""

# As XrdHttp writes it, with its own prefixes and full URLs.
XROOTD = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:ns1="http://apache.org/dav/props/" xmlns:lp1="DAV:">
<D:response><D:href>https://origin-0:8443/private/data/x/tree/b</D:href>
<D:propstat><D:prop><lp1:getcontentlength>5000</lp1:getcontentlength><lp1:resourcetype/>
</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
</D:multistatus>"""


class Parse(unittest.TestCase):
    def test_native(self):
        entries = webdav.relative(webdav.parse(NATIVE), "/private/data/x/tree")
        self.assertEqual(set(entries), {"", "a b", "sub"})
        self.assertTrue(entries[""].collection)
        self.assertEqual((entries["a b"].collection, entries["a b"].size), (False, 1000))
        self.assertTrue(entries["sub"].collection)

    def test_cache_and_xrootd(self):
        (entry,) = webdav.parse(CACHE)
        self.assertEqual((entry.path, entry.collection, entry.size),
                         ("/private/data/x/empty", False, None))
        (entry,) = webdav.parse(XROOTD)
        self.assertEqual((entry.path, entry.size), ("/private/data/x/tree/b", 5000))

    def test_prefix(self):
        tiny = NATIVE.replace(b"<D:href>/private", b"<D:href>/api/v1.0/origin/data/private")
        self.assertEqual(set(webdav.relative(webdav.parse(tiny), "/private/data/x/tree")),
                         {"", "a b", "sub"})
        with self.assertRaises(ValueError):
            webdav.relative(webdav.parse(NATIVE), "/private/data/y")

    def test_not_a_multistatus(self):
        for bad in (b"", b"<html/>", b'{"error": "x"}', b'<a xmlns="DAV:"/>'):
            with self.assertRaises(ValueError):
                webdav.parse(bad)


class Problems(unittest.TestCase):
    def entries(self):
        return webdav.relative(webdav.parse(NATIVE), "/private/data/x/tree")

    def test_match(self):
        self.assertEqual(webdav.problems({"a b": 1000, "sub": None}, self.entries()), [])

    def test_differences(self):
        found = webdav.problems({"a b": 999, "sub": 5, "c": 1}, self.entries())
        self.assertEqual(found, ["missing c", "a b is 1000 bytes, not 999", "sub is a collection"])
        self.assertEqual(webdav.problems({"sub": None}, self.entries()), ["also lists a b"])

    def test_missing_size_is_zero(self):
        got = webdav.relative(webdav.parse(CACHE), "/private/data/x")
        self.assertEqual(webdav.problems({"empty": 0}, got), [])
        self.assertEqual(webdav.problems({"empty": 3}, got), ["empty is 0 bytes, not 3"])
