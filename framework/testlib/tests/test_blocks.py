import unittest

from testlib import blocks
from testlib.blocks import check

DATA = bytes(range(256)) * 64  # 16384 bytes


def multipart(parts):
    """A 206 answer carrying parts, [(first, last)], of DATA."""
    body = b""
    for first, last in parts:
        body += (f"--XYZ\r\nContent-Type: application/octet-stream\r\n"
                 f"Content-Range: bytes {first}-{last}/{len(DATA)}\r\n\r\n").encode()
        body += DATA[first:last + 1] + b"\r\n"
    body += b"--XYZ--\r\n"
    return 206, [("Content-Type", "multipart/byteranges; boundary=XYZ")], body


class Check(unittest.TestCase):
    def test_single_range(self):
        request = ((4079, 4080),)
        good = [("Content-Range", f"bytes 4079-4080/{len(DATA)}")]
        for status, headers, body in (
                (200, [], DATA),
                (206, good, b"xx"),
                (206, [("Content-Range", f"bytes 4079-4081/{len(DATA)}")], DATA[4079:4082]),
                (206, [("Content-Range", "bytes 4079-4080/9")], DATA[4079:4081])):
            self.assertIsNotNone(check(DATA, request, status, headers, body), (status, headers))

    def test_multi_range(self):
        request = ((0, 0), (4079, 4080), (16383, 16383))
        self.assertIsNotNone(check(DATA, request, *multipart([(0, 0), (4079, 4080)])))
        self.assertIsNotNone(check(DATA, request, 200, [], DATA[:-1]))


V1 = bytes(range(256)) * 40
V2 = bytes(reversed(range(256))) * 40


class Versions(unittest.TestCase):
    """A response that mixes versions, or claims another's size, is
    neither."""

    VERSIONS = {"v1": V1, "v2": V2 + b"more"}

    def which(self, request, status, headers, body):
        return blocks.which_version(self.VERSIONS, request, status, headers, body)[0]

    def test_whole(self):
        self.assertIsNone(self.which(None, 200, [], V1[:5000] + V2[5000:]))
        self.assertIsNone(self.which(None, 200, [("Content-Length", "9")], V1))

    def test_ranges(self):
        headers = [("Content-Range", f"bytes 10-19/{len(V1)}")]
        self.assertIsNone(self.which(((10, 19),), 206, headers, V2[10:20]))

    def test_unsatisfiable(self):
        request = ((len(V1), len(V1) + 1),)
        self.assertFalse(blocks.unsatisfiable(len(V1) + 4, request))
        headers = [("Content-Range", f"bytes */{len(V2) + 4}")]
        self.assertIsNone(self.which(request, 416, headers, b""))

    def test_freshness(self):
        # None skips waiting for the cache to serve the new version.
        for control in ("max-age=30", "public, s-maxage=60", "no-cache", "no-store", "private"):
            self.assertIsNotNone(blocks.freshness(control), control)


def cinfo(file_size, held):
    """A .cinfo of version 4 with no access records, as XRootD writes it
    (see blocks.parse_cinfo), recording held blocks of XRootD's size."""
    def le(value, size):
        return value.to_bytes(size, "little")

    store = b"".join((le(blocks.XROOTD_BLOCK, 8), le(file_size, 8), le(1700000000, 8), le(0, 8),
                      le(0, 8), le(3, 4), le(0, 4)))
    bits = (file_size - 1) // blocks.XROOTD_BLOCK + 1 if file_size else 1
    bitmap = bytearray((bits - 1) // 8 + 1)
    for i in held:
        bitmap[i // 8] |= 1 << (i % 8)
    return (le(4, 4) + store + le(blocks.crc32c(store), 4) + bytes(bitmap)
            + le(blocks.crc32c(bytes(bitmap)), 4))


class Cinfo(unittest.TestCase):
    """An XRootD cache that lacks a block must not read as complete."""

    def test_count(self):
        size = 9 * blocks.XROOTD_BLOCK
        found = blocks.parse_cinfo(cinfo(size, [0, 8]))
        self.assertEqual((found.blocks, found.count, found.complete), (9, 2, False))
        found = blocks.parse_cinfo(cinfo(size, [i for i in range(9) if i != 7]))
        self.assertEqual((found.count, found.complete), (8, False))
        self.assertTrue(blocks.parse_cinfo(cinfo(size, range(9))).complete)


if __name__ == "__main__":
    unittest.main()
