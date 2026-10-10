"""Object sizes and byte ranges at the block boundaries of Pelican's
stores, and checking what a server returns for them.

pstore (an origin's encrypted store) and the V2 cache share one block
format: 4080 bytes of plaintext and a 16-byte authentication tag per
block, the last block short rather than padded (Pelican's
local_cache/schema.go). Beyond that:

- pstore keeps objects of up to 4096 bytes inline in its catalog
  (pstore/object_io.go); the V2 cache inlines only those under 4096, and
  none that it first sees through a range (local_cache/persistent_cache.go).
- pstore buffers an object of unknown length up to 1 MiB, and a declared
  one (Content-Length) under 1 MiB, and streams anything larger in
  chunks: 8,388,480 bytes for an unknown length, which is what `pelican
  object put` sends, or sized from the declared length, e.g. 2,097,120
  bytes for up to 2 MiB (local_cache/chunking.go). So a declared 2 MiB
  object spills 32 bytes into a second chunk.
- The V2 cache fetches the blocks that each read is missing, and Go's
  http.ServeContent reads 32 KiB at a time, so it fills a range miss at
  most 8 blocks at a time. pstore reads 16 blocks per read of its disk
  (local_cache/range_reader.go, storage.go).
- The XRootD cache uses 128 KiB blocks (xrootd/resources/xrootd-cache.cfg).
- The V2 cache marks an object complete once it holds every block, and
  only then sends Age (local_cache/storage.go, persistent_cache_api.go).
  The XRootD cache records the blocks it holds in a bitmap in
  <path>.cinfo (see parse_cinfo).

A range is (start, end), inclusive; (None, n) is the last n bytes, and
(start, None) everything from start. A request asks for one or more.
"""

import re
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

BLOCK = 4080
TAG = 16
INLINE = 4096
SPILL = 1 << 20
CHUNK_DECLARED_2M = 2097120
CHUNK_UNDECLARED = 8388480
XROOTD_BLOCK = 128 << 10

SIZES = (
    0, 1,
    BLOCK - 1, BLOCK, BLOCK + 1,
    INLINE - 1, INLINE, INLINE + 1,
    2 * BLOCK - 1, 2 * BLOCK, 2 * BLOCK + 1,
    XROOTD_BLOCK - 1, XROOTD_BLOCK, XROOTD_BLOCK + 1,
    SPILL - 1, SPILL, SPILL + 1,
    CHUNK_DECLARED_2M, CHUNK_DECLARED_2M + 1, 2 << 20,
    CHUNK_UNDECLARED, CHUNK_UNDECLARED + 1,
)

# Offsets worth straddling, where an object is big enough: the first block
# edges, the V2 cache's and pstore's read batches, XRootD's block, the
# spill threshold and the block edge below it, and chunk edges.
EDGES = (
    BLOCK, 2 * BLOCK, 3 * BLOCK, 8 * BLOCK, 16 * BLOCK,
    XROOTD_BLOCK, (SPILL // BLOCK) * BLOCK, SPILL,
    CHUNK_DECLARED_2M, CHUNK_UNDECLARED,
)

Range = Tuple[Optional[int], Optional[int]]
Request = Tuple[Range, ...]


def tier(size: int, declared: bool) -> str:
    """Where pstore should put an object of size: `inline`, `buffered`, or
    `streamed`, by whether its length was declared."""
    if size <= INLINE:
        return "inline"
    if declared:
        return "buffered" if size < SPILL else "streamed"
    return "buffered" if size <= SPILL else "streamed"


def header(request: Request) -> str:
    """The Range header for request."""
    specs = []
    for start, end in request:
        if start is None:
            specs.append(f"-{end}")
        elif end is None:
            specs.append(f"{start}-")
        else:
            specs.append(f"{start}-{end}")
    return "bytes=" + ",".join(specs)


def resolve(size: int, r: Range) -> Tuple[int, int]:
    """The first and last byte that r asks for in an object of size."""
    start, end = r
    if start is None:
        return max(size - (end or 0), 0), size - 1
    if end is None:
        return start, size - 1
    return start, min(end, size - 1)


def requests(size: int) -> List[Request]:
    """What to ask for of an object of size, beyond the whole of it."""
    if size == 0:
        return []
    found: List[Request] = [((0, 0),), ((size - 1, size - 1),), ((None, min(5000, size)),),
                            ((size // 2, None),)]
    for edge in (e for e in EDGES if e < size):
        found += [((edge - 1, edge),), ((edge, edge),), ((edge - 1, edge - 1),)]
    last = (size - 1) // BLOCK * BLOCK
    if last > 0:
        found += [((last - 1, last),), ((last, size - 1),)]
    if size > 2 * BLOCK + 40:
        found.append(((BLOCK - 80, 2 * BLOCK + 40),))
    if size > BLOCK + 1:
        found.append(((0, 0), (BLOCK - 1, BLOCK), (size - 1, size - 1)))
    return list(dict.fromkeys(found))


def content_range(value: str) -> Optional[Tuple[int, int, int]]:
    """A Content-Range header's first byte, last byte, and total."""
    m = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+)\s*", value)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_byteranges(content_type: str, body: bytes) -> List[Tuple[str, bytes]]:
    """The parts of a multipart/byteranges body: each one's Content-Range
    and bytes."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("multipart body without a boundary")
    delimiter = b"--" + m.group(1).encode()
    parts = []
    # A preamble comes before the first delimiter, "--" after the last.
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        found = ""
        for line in head.decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() == "content-range":
                found = value.strip()
        parts.append((found, payload))
    return parts


def merge(spans: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Inclusive spans, with those that overlap or touch joined."""
    merged: List[Tuple[int, int]] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def check(data: bytes, request: Request, status: Optional[int],
          headers: Sequence[Tuple[str, str]], body: bytes) -> Optional[str]:
    """What is wrong with a response to request for an object holding
    data, or None. One range must come back as 206 with exactly its bytes.
    Several may come back as multipart/byteranges parts, in any grouping
    that covers them. A server may instead answer 200 with the whole
    object, to several ranges or to one that covers it all (RFC 7233
    lets it ignore Range)."""
    size = len(data)
    def get(name: str) -> str:
        values = [v for k, v in headers if k.lower() == name]
        return values[-1] if values else ""

    wanted = [resolve(size, r) for r in request]
    whole = merge(wanted) == [(0, size - 1)]
    if status == 200 and (len(request) > 1 or whole):
        return None if body == data else "HTTP 200, but the body is not the object"
    if status != 206:
        return "no response" if status is None else f"HTTP {status}, not 206"

    kind = get("content-type")
    if len(request) > 1 and kind.lower().startswith("multipart/byteranges"):
        try:
            parts = parse_byteranges(kind, body)
        except ValueError as e:
            return str(e)
    else:
        parts = [(get("content-range"), body)]

    got: List[Tuple[int, int]] = []
    for value, payload in parts:
        found = content_range(value)
        if found is None:
            return f"bad Content-Range '{value}'"
        first, last, total = found
        if total != size:
            return f"Content-Range '{value}' gives the wrong size"
        if payload != data[first:last + 1]:
            return f"bytes {first}-{last} are not the object's ({len(payload)} bytes came)"
        got.append((first, last))
    covered = merge(got)
    for first, last in wanted:
        if not any(a <= first and last <= b for a, b in covered):
            return f"bytes {first}-{last} did not come back"
    if len(request) == 1 and len(parts) == 1:
        first, last, _ = content_range(parts[0][0]) or (0, 0, 0)
        if (first, last) != wanted[0]:
            return f"asked for {wanted[0][0]}-{wanted[0][1]}, got {first}-{last}"
    return None


def unsatisfiable(size: int, request: Request) -> bool:
    """Whether no range of request has a byte in an object of size."""
    for start, end in request:
        if start is None:
            if (end or 0) > 0 and size > 0:
                return False
        elif start < size:
            return False
    return True


#---------------------------------------------------------------------------
# Several versions of an object (the blocks suite's overwrite scenarios).

def which_version(versions: Dict[str, bytes], request: Optional[Request], status: Optional[int],
                  headers: Sequence[Tuple[str, str]], body: bytes) -> Tuple[Optional[str], str]:
    """Which of versions, by name, a response to request (None for the
    whole object) came from: (name, "") if it is entirely that one;
    (None, why) if it is none of them, e.g. mixed, with another version's
    size, or an error."""
    if status is None or 300 <= status < 400 or (status >= 400 and status != 416):
        return None, "no response" if status is None else f"HTTP {status}"
    lengths = [v for k, v in headers if k.lower() == "content-length"]
    if lengths and lengths[-1].strip() != str(len(body)):
        return None, f"Content-Length {lengths[-1].strip()}, but {len(body)} bytes came"
    if request is None:
        if status != 200:
            return None, f"HTTP {status}, not 200"
        for name, data in versions.items():
            if body == data:
                return name, ""
        return None, f"{len(body)} bytes, but not any one version"
    if status == 416:
        ranges = [v for k, v in headers if k.lower() == "content-range"]
        m = re.fullmatch(r"\s*bytes\s+\*/(\d+)\s*", ranges[-1]) if ranges else None
        for name, data in versions.items():
            if unsatisfiable(len(data), request) and (not m or int(m.group(1)) == len(data)):
                return name, ""
        return None, "HTTP 416, but the range is satisfiable in that version"
    problems = []
    for name, data in versions.items():
        why = check(data, request, status, headers, body)
        if why is None:
            return name, ""
        problems.append(f"{name}: {why}")
    return None, "; ".join(problems)


def freshness(cache_control: str) -> Optional[float]:
    """How many seconds a cache treats an object as fresh under an
    origin's Cache-Control, or None if it says nothing about that and the
    cache decides for itself. A V2 cache revalidates no-cache after a
    grace of 5 seconds (local_cache/cache_control.go), and none stores
    no-store or private."""
    ages = []
    no_cache = False
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        name = name.strip().lower()
        if name in ("no-store", "private"):
            return 0.0
        if name == "no-cache":
            no_cache = True
        elif name in ("max-age", "s-maxage"):
            try:
                ages.append(float(int(value.strip().strip('"'))))
            except ValueError:
                pass
    if no_cache:
        return 5.0
    return max(ages) if ages else None


#---------------------------------------------------------------------------
# Overlapping reads (the blocks suite's cache-overlap). Each is unaligned to
# both BLOCK and XROOTD_BLOCK, but for the object's own ends.

OVERLAP_SIZE = 3 * XROOTD_BLOCK + 1234


def overlap_plans() -> Dict[str, List[Request]]:
    """Reads of an OVERLAP_SIZE object, each plan in order on an object
    of its own. Every read after a plan's first covers both blocks that
    earlier ones fetched and blocks that they did not, at both block
    sizes; but `inside` asks only for what its first read fetched."""
    return {
        "head":     [((140001, 150000),), ((125001, 141000),)],
        "tail":     [((140001, 150000),), ((149001, 270000),)],
        "superset": [((140001, 150000),), ((120001, 280000),)],
        "inside":   [((120001, 280000),), ((131001, 262200),)],
        "end":      [((394001, 394449),), ((380001, 394100),)],
        "stagger":  [((1001, 9000),), ((8001, 140000),), ((139001, 265000),),
                     ((264001, 394449),)],
        "multi":    [((140001, 150000),), ((141001, 142000), (300001, 301000))],
    }


def concurrent_reads() -> List[Optional[Request]]:
    """Reads of a cold OVERLAP_SIZE object, all sent at once: the same
    range twice, ranges that overlap their neighbors, and the whole
    object (None)."""
    return [((125001, 141000),), ((125001, 141000),), ((140001, 150000),),
            ((149001, 270000),), ((262001, 300000),), ((380001, 394449),),
            ((1001, 9000),), None]


def covering(request: Request, size: int, unit: int) -> List[int]:
    """The blocks of unit bytes that request touches in an object of
    size."""
    found: Set[int] = set()
    for r in request:
        first, last = resolve(size, r)
        found.update(range(first // unit, last // unit + 1))
    return sorted(found)


#---------------------------------------------------------------------------
# Ranges that add up to a whole object (the blocks suite's cache-assemble):
# in the order read, overlapping, and unaligned to both block sizes.

ASSEMBLE_SIZE = 4 * XROOTD_BLOCK + 777
ASSEMBLE: Tuple[Tuple[int, int], ...] = (
    (262400, 330000), (0, 60000), (459001, 524400), (131001, 200000), (520001, 525064),
    (59001, 131500), (329001, 393300), (199990, 262500), (393001, 460000),
)


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for byte in data:
        c = _CRC32C[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _crc32c_table() -> List[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C = _crc32c_table()

# A .cinfo file (version 4, little-endian; XRootD's XrdPfcInfo.cc,
# Info::Write, and Pelican's cache/cinfo.go): the version; the store
# (BufferSize, FileSize, CreationTime, NoCkSumTime, AccessCnt, Status,
# AStatSize) and its CRC-32C; a bitmap of the blocks held, bit i of byte
# i/8 for block i, and a bit for one block even for an empty object;
# AStatSize access records, of CINFO_ASTAT bytes each; and a CRC-32C of
# the bitmap and records.
_CINFO_VERSION = struct.Struct("<i")
_CINFO_STORE = struct.Struct("<qqqqQIi")
_CINFO_CRC = struct.Struct("<I")
CINFO_ASTAT = 56


@dataclass(frozen=True)
class Cinfo:
    block_size: int
    file_size: int
    bitmap: bytes

    @property
    def blocks(self) -> int:
        return -(-self.file_size // self.block_size)

    @property
    def count(self) -> int:
        """How many of the object's blocks the cache holds."""
        return sum(1 for i in range(self.blocks) if self.bitmap[i // 8] >> (i % 8) & 1)

    @property
    def complete(self) -> bool:
        return self.count == self.blocks


def parse_cinfo(data: bytes) -> Cinfo:
    """The blocks a .cinfo file records; ValueError if it is not one this
    understands."""
    head = _CINFO_VERSION.size + _CINFO_STORE.size + _CINFO_CRC.size
    if len(data) < head:
        raise ValueError(f"a .cinfo of {len(data)} bytes is too short")
    (version,) = _CINFO_VERSION.unpack_from(data)
    if version != 4:
        raise ValueError(f".cinfo version {version}, not 4")
    store = data[_CINFO_VERSION.size:_CINFO_VERSION.size + _CINFO_STORE.size]
    block_size, file_size, _, _, _, _, astats = _CINFO_STORE.unpack(store)
    (crc,) = _CINFO_CRC.unpack_from(data, _CINFO_VERSION.size + _CINFO_STORE.size)
    if crc != crc32c(store):
        raise ValueError("the .cinfo's store fails its CRC")
    if block_size <= 0 or file_size < 0 or astats < 0:
        raise ValueError(f"a .cinfo with block size {block_size}, file size {file_size}")
    blocks = -(-file_size // block_size)
    size = -(-max(blocks, 1) // 8)
    bitmap = data[head:head + size]
    if len(bitmap) < size:
        raise ValueError("the .cinfo's bitmap is cut short")
    rest = head + size + astats * CINFO_ASTAT
    if len(data) == rest + _CINFO_CRC.size:
        (crc,) = _CINFO_CRC.unpack_from(data, rest)
        if crc != crc32c(data[head:rest]):
            raise ValueError("the .cinfo's bitmap fails its CRC")
    return Cinfo(block_size, file_size, bitmap)
