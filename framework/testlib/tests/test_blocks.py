import struct
import unittest

from testlib import blocks
from testlib.blocks import BLOCK, SIZES, check, header, merge, requests, resolve, tier

DATA = bytes(range(256)) * 64  # 16384 bytes


class Sizes(unittest.TestCase):
    def test_sizes_are_distinct_and_in_order(self):
        self.assertEqual(list(SIZES), sorted(set(SIZES)))

    def test_each_edge_is_straddled(self):
        for edge in (BLOCK, blocks.INLINE, 2 * BLOCK, blocks.XROOTD_BLOCK, blocks.SPILL):
            self.assertIn(edge - 1, SIZES)
            self.assertIn(edge, SIZES)
            self.assertIn(edge + 1, SIZES)
        # One chunk exactly, and one byte into a second.
        for edge in (blocks.CHUNK_DECLARED_2M, blocks.CHUNK_UNDECLARED):
            self.assertIn(edge, SIZES)
            self.assertIn(edge + 1, SIZES)

    def test_chunk_sizes_are_whole_blocks(self):
        self.assertEqual(blocks.CHUNK_DECLARED_2M % BLOCK, 0)
        self.assertEqual(blocks.CHUNK_UNDECLARED % BLOCK, 0)

    def test_tiers(self):
        self.assertEqual(tier(0, True), "inline")
        self.assertEqual(tier(4096, True), "inline")
        self.assertEqual(tier(4097, True), "buffered")
        self.assertEqual(tier(blocks.SPILL - 1, True), "buffered")
        # The spill threshold itself: streamed if declared, buffered if not.
        self.assertEqual(tier(blocks.SPILL, True), "streamed")
        self.assertEqual(tier(blocks.SPILL, False), "buffered")
        self.assertEqual(tier(blocks.SPILL + 1, False), "streamed")


class Ranges(unittest.TestCase):
    def test_header(self):
        self.assertEqual(header(((0, 0),)), "bytes=0-0")
        self.assertEqual(header(((None, 500),)), "bytes=-500")
        self.assertEqual(header(((10, None),)), "bytes=10-")
        self.assertEqual(header(((0, 0), (5, 9))), "bytes=0-0,5-9")

    def test_resolve(self):
        self.assertEqual(resolve(100, (None, 10)), (90, 99))
        self.assertEqual(resolve(100, (None, 500)), (0, 99))
        self.assertEqual(resolve(100, (40, None)), (40, 99))
        self.assertEqual(resolve(100, (40, 49)), (40, 49))

    def test_requests_are_satisfiable(self):
        for size in SIZES:
            for request in requests(size):
                for r in request:
                    first, last = resolve(size, r)
                    self.assertTrue(0 <= first <= last < size, (size, request))

    def test_requests_straddle_block_edges(self):
        found = requests(3 * BLOCK + 1)
        for k in (1, 2, 3):
            self.assertIn(((k * BLOCK - 1, k * BLOCK),), found)
        self.assertTrue(any(len(r) > 1 for r in found))
        self.assertEqual(requests(0), [])

    def test_merge(self):
        self.assertEqual(merge([(5, 9), (0, 4), (20, 30), (25, 26)]), [(0, 9), (20, 30)])


def response(request, parts, total=len(DATA), multipart=True):
    """A 206 answer carrying parts, [(first, last)], of DATA."""
    if len(parts) == 1 and not multipart:
        first, last = parts[0]
        return 206, [("Content-Range", f"bytes {first}-{last}/{total}")], DATA[first:last + 1]
    body = b""
    for first, last in parts:
        body += (f"--XYZ\r\nContent-Type: application/octet-stream\r\n"
                 f"Content-Range: bytes {first}-{last}/{total}\r\n\r\n").encode()
        body += DATA[first:last + 1] + b"\r\n"
    body += b"--XYZ--\r\n"
    return 206, [("Content-Type", "multipart/byteranges; boundary=XYZ")], body


class Check(unittest.TestCase):
    def test_single_range(self):
        request = ((4079, 4080),)
        status, headers, body = response(request, [(4079, 4080)], multipart=False)
        self.assertIsNone(check(DATA, request, status, headers, body))
        self.assertEqual(check(DATA, request, 200, [], DATA), "HTTP 200, not 206")
        self.assertIn("are not the object's", check(DATA, request, 206, headers, b"xx"))
        wrong = [("Content-Range", f"bytes 4079-4081/{len(DATA)}")]
        self.assertIsNotNone(check(DATA, request, 206, wrong, DATA[4079:4082]))
        self.assertIn("wrong size", check(DATA, request, 206,
                                          [("Content-Range", "bytes 4079-4080/9")], body))

    def test_whole_object_as_200(self):
        self.assertIsNone(check(DATA, ((None, len(DATA)),), 200, [], DATA))
        self.assertIsNone(check(DATA[:1], ((0, 0),), 200, [], DATA[:1]))
        self.assertIsNotNone(check(DATA, ((0, 10),), 200, [], DATA))

    def test_multi_range(self):
        request = ((0, 0), (4079, 4080), (16383, 16383))
        status, headers, body = response(request, [(0, 0), (4079, 4080), (16383, 16383)])
        self.assertIsNone(check(DATA, request, status, headers, body))
        # Grouped differently, but covering every range, is fine too.
        status, headers, body = response(request, [(0, 4080), (16383, 16383)])
        self.assertIsNone(check(DATA, request, status, headers, body))
        # A range left out is not.
        status, headers, body = response(request, [(0, 0), (4079, 4080)])
        self.assertIn("did not come back", check(DATA, request, status, headers, body))
        # A server may ignore a multi-range request and send everything.
        self.assertIsNone(check(DATA, request, 200, [], DATA))
        self.assertIsNotNone(check(DATA, request, 200, [], DATA[:-1]))


def blocks_of(request, unit, size=blocks.OVERLAP_SIZE):
    return set(blocks.covering(request, size, unit))


def unaligned(request, size):
    """Whether no range starts or ends on either block edge, but for the
    object's own ends."""
    for r in request:
        first, last = resolve(size, r)
        for unit in (BLOCK, blocks.XROOTD_BLOCK):
            if first != 0 and first % unit == 0:
                return False
            if last != size - 1 and (last + 1) % unit == 0:
                return False
    return True


class Overlap(unittest.TestCase):
    def test_later_reads_span_cached_and_uncached_blocks(self):
        for unit in (BLOCK, blocks.XROOTD_BLOCK):
            for name, plan in blocks.overlap_plans().items():
                cached = set()
                for n, request in enumerate(plan):
                    touched = blocks_of(request, unit)
                    if n:
                        if name == "inside":
                            self.assertLessEqual(touched, cached, (name, unit))
                        else:
                            self.assertTrue(touched & cached, (name, n, unit))
                            self.assertTrue(touched - cached, (name, n, unit))
                    cached |= touched

    def test_superset_is_uncached_on_both_sides(self):
        first, later = blocks.overlap_plans()["superset"]
        for unit in (BLOCK, blocks.XROOTD_BLOCK):
            cached, touched = blocks_of(first, unit), blocks_of(later, unit)
            self.assertLess(min(touched), min(cached))
            self.assertGreater(max(touched), max(cached))

    def test_inside_crosses_both_xrootd_edges(self):
        (start, end), = blocks.overlap_plans()["inside"][1]
        for k in (1, 2):
            self.assertTrue(start < k * blocks.XROOTD_BLOCK <= end)

    def test_reads_are_unaligned_and_satisfiable(self):
        reads = [r for plan in blocks.overlap_plans().values() for r in plan]
        reads += [r for r in blocks.concurrent_reads() if r]
        for request in reads:
            self.assertTrue(unaligned(request, blocks.OVERLAP_SIZE), request)
            self.assertFalse(blocks.unsatisfiable(blocks.OVERLAP_SIZE, request))

    def test_concurrent_reads(self):
        reads = blocks.concurrent_reads()
        self.assertEqual(reads.count(None), 1)
        ranges = [r for r in reads if r]
        self.assertLess(len(set(ranges)), len(ranges), "one range is asked for twice")
        spans = sorted(set(r[0] for r in ranges))
        self.assertTrue(any(a[1] >= b[0] for a, b in zip(spans, spans[1:])))


class Assemble(unittest.TestCase):
    def test_covers_the_object(self):
        self.assertEqual(merge(blocks.ASSEMBLE), [(0, blocks.ASSEMBLE_SIZE - 1)])
        spans = sorted(blocks.ASSEMBLE)
        self.assertNotEqual(list(blocks.ASSEMBLE), spans)
        for a, b in zip(spans, spans[1:]):
            self.assertGreaterEqual(a[1], b[0], "each overlaps the next")

    def test_first_read_leaves_it_partial(self):
        first = (blocks.ASSEMBLE[0],)
        for unit in (BLOCK, blocks.XROOTD_BLOCK):
            total = -(-blocks.ASSEMBLE_SIZE // unit)
            self.assertLess(len(blocks_of(first, unit, blocks.ASSEMBLE_SIZE)), total)

    def test_unaligned(self):
        for r in blocks.ASSEMBLE:
            self.assertTrue(unaligned((r,), blocks.ASSEMBLE_SIZE), r)


def cinfo(file_size, held, block_size=blocks.XROOTD_BLOCK, version=4, astats=0,
          bad_store=False, bad_tail=False):
    store = struct.pack("<qqqqQIi", block_size, file_size, 1, 2, 3, 0, astats)
    crc = blocks.crc32c(store) ^ (1 if bad_store else 0)
    count = -(-file_size // block_size)
    bitmap = bytearray(-(-count // 8))
    for i in held:
        bitmap[i // 8] |= 1 << (i % 8)
    tail = bytes(bitmap) + bytes(blocks.CINFO_ASTAT * astats)
    return (struct.pack("<i", version) + store + struct.pack("<I", crc) + tail
            + struct.pack("<I", blocks.crc32c(tail) ^ (1 if bad_tail else 0)))


class Cinfo(unittest.TestCase):
    def test_crc32c(self):
        self.assertEqual(blocks.crc32c(b"123456789"), 0xE3069283)

    def test_partial_and_complete(self):
        size = 4 * blocks.XROOTD_BLOCK + 777
        found = blocks.parse_cinfo(cinfo(size, [2]))
        self.assertEqual((found.blocks, found.count, found.complete), (5, 1, False))
        found = blocks.parse_cinfo(cinfo(size, range(5), astats=2))
        self.assertEqual((found.count, found.complete), (5, True))
        self.assertTrue(blocks.parse_cinfo(cinfo(0, [])).complete)

    def test_refused(self):
        size = 3 * blocks.XROOTD_BLOCK
        for bad in (cinfo(size, [0], version=3), cinfo(size, [0], bad_store=True),
                    cinfo(size, [0], bad_tail=True), cinfo(size, [0])[:20],
                    cinfo(9 * blocks.XROOTD_BLOCK, [0])[:56]):
            with self.assertRaises(ValueError):
                blocks.parse_cinfo(bad)
        # A tail of another length is not checked: records may differ in size.
        self.assertEqual(blocks.parse_cinfo(cinfo(size, [0]) + b"x").count, 1)


V1 = bytes(range(256)) * 40
V2 = bytes(reversed(range(256))) * 40


class Versions(unittest.TestCase):
    VERSIONS = {"v1": V1, "v2": V2 + b"more"}

    def test_whole(self):
        self.assertEqual(blocks.which_version(self.VERSIONS, None, 200, [], V1), ("v1", ""))
        self.assertEqual(blocks.which_version(self.VERSIONS, None, 200, [], V2 + b"more")[0], "v2")
        self.assertIsNone(blocks.which_version(self.VERSIONS, None, 200, [], V1[:5000] + V2[5000:])[0])
        self.assertIsNone(blocks.which_version(self.VERSIONS, None, 200,
                                               [("Content-Length", "9")], V1)[0])
        self.assertEqual(blocks.which_version(self.VERSIONS, None, 503, [], b"")[0], blocks.REFUSED)

    def test_ranges(self):
        request = ((10, 19),)
        for name, data in self.VERSIONS.items():
            headers = [("Content-Range", f"bytes 10-19/{len(data)}")]
            self.assertEqual(blocks.which_version(self.VERSIONS, request, 206, headers,
                                                  data[10:20]), (name, ""))
        # v2's bytes with v1's total are neither.
        headers = [("Content-Range", f"bytes 10-19/{len(V1)}")]
        self.assertIsNone(blocks.which_version(self.VERSIONS, request, 206, headers, V2[10:20])[0])

    def test_unsatisfiable(self):
        request = ((len(V1), len(V1) + 1),)
        self.assertTrue(blocks.unsatisfiable(len(V1), request))
        self.assertFalse(blocks.unsatisfiable(len(V1) + 4, request))
        self.assertTrue(blocks.unsatisfiable(0, ((None, 5),)))
        headers = [("Content-Range", f"bytes */{len(V1)}")]
        self.assertEqual(blocks.which_version(self.VERSIONS, request, 416, headers, b"")[0], "v1")
        # Satisfiable in v2, so a 416 claiming v2's size is wrong.
        headers = [("Content-Range", f"bytes */{len(V2) + 4}")]
        self.assertIsNone(blocks.which_version(self.VERSIONS, request, 416, headers, b"")[0])

    def test_freshness(self):
        self.assertEqual(blocks.freshness("max-age=30"), 30)
        self.assertEqual(blocks.freshness("public, max-age=30, s-maxage=60"), 60)
        self.assertEqual(blocks.freshness("no-cache, must-revalidate"), 5)
        self.assertEqual(blocks.freshness("no-store"), 0)
        self.assertIsNone(blocks.freshness(""))
        self.assertIsNone(blocks.freshness("public"))
