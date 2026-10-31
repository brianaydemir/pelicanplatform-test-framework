"""Object sizes and byte ranges at the block boundaries of Pelican's stores.

It also checks what a server returns for them.

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
  most 8 blocks at a time. The block store that pstore and the V2 cache
  share reads 16 blocks per read of its disk (local_cache/range_reader.go,
  storage.go).
- The XRootD cache uses 128 KiB blocks (xrootd/resources/xrootd-cache.cfg).
- XRootD's HTTP server, in front of an XRootD origin or cache, reads a
  single range by reads of up to 8 MiB, and several ranges by vector
  reads, a round at a time: each of at most 512 chunks, of at most
  512 KiB each, and 8 MiB in all (XrdHttpReadRangeHandler::splitRanges
  in XRootD).
- The V2 cache marks an object complete once it holds every block, and
  only then sends Age (local_cache/storage.go, persistent_cache_api.go).
  The XRootD cache records the blocks it holds in a bitmap in
  <path>.cinfo (see parse_cinfo).

A range is (start, end), inclusive; (None, n) is the last n bytes, and
(start, None) everything from start. A request asks for one or more.

A cache's store is on disk under framework/var/data/cache/<n>/
(cache_dir()): store_bytes() is what a cache's objects take there,
read_cinfo() what an XRootD cache's .cinfo records, and xrootd_holds()
whether it holds all of an object.
"""

import os
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from . import common, web

BLOCK = 4080
TAG = 16
INLINE = 4096
SPILL = 1 << 20
CHUNK_DECLARED_2M = 2097120
CHUNK_UNDECLARED = 8388480
XROOTD_BLOCK = 128 << 10
READV_CHUNKS = 512
READV_CHUNK = 512 << 10
READ_MAX = 8 << 20

SIZES = (
    0,
    1,
    BLOCK - 1,
    BLOCK,
    BLOCK + 1,
    INLINE - 1,
    INLINE,
    INLINE + 1,
    2 * BLOCK - 1,
    2 * BLOCK,
    2 * BLOCK + 1,
    XROOTD_BLOCK - 1,
    XROOTD_BLOCK,
    XROOTD_BLOCK + 1,
    SPILL - 1,
    SPILL,
    SPILL + 1,
    CHUNK_DECLARED_2M,
    CHUNK_DECLARED_2M + 1,
    2 << 20,
    CHUNK_UNDECLARED,
    CHUNK_UNDECLARED + 1,
    READ_MAX + 2,
)

# Offsets worth straddling, where an object is big enough: the first block
# edges, the V2 cache's and pstore's read batches, XRootD's block, the
# spill threshold and the block edge below it, and chunk edges.
EDGES = (
    BLOCK,
    2 * BLOCK,
    3 * BLOCK,
    8 * BLOCK,
    16 * BLOCK,
    XROOTD_BLOCK,
    (SPILL // BLOCK) * BLOCK,
    SPILL,
    CHUNK_DECLARED_2M,
    CHUNK_UNDECLARED,
)

Range = tuple[int | None, int | None]
Request = tuple[Range, ...]


def tier(size: int, declared: bool) -> str:
    """Return where pstore should put an object of size.

    Returns:
        One of `inline`, `buffered`, or `streamed`, by whether its length
        was declared.
    """
    if size <= INLINE:
        return "inline"
    if declared:
        return "buffered" if size < SPILL else "streamed"
    return "buffered" if size <= SPILL else "streamed"


def stored(size: int) -> int:
    """Return how many bytes the V2 cache's file of an object of size takes.

    It holds the object's blocks, and a tag for each (CalculateFileSize
    in Pelican's local_cache).
    """
    return size + TAG * -(-size // BLOCK)


def header(request: Request) -> str:
    """Return the Range header for request."""
    specs: list[str] = []
    for start, end in request:
        if start is None:
            specs.append(f"-{end}")
        elif end is None:
            specs.append(f"{start}-")
        else:
            specs.append(f"{start}-{end}")
    return "bytes=" + ",".join(specs)


def resolve(size: int, r: Range) -> tuple[int, int]:
    """Return the first and last byte r asks for in an object of size."""
    start, end = r
    if start is None:
        return max(size - (end or 0), 0), size - 1
    if end is None:
        return start, size - 1
    return start, min(end, size - 1)


def requests(size: int) -> list[Request]:
    """Return what to ask of an object of size, beyond the whole of it."""
    if size == 0:
        return []
    found: list[Request] = [
        ((0, 0),),
        ((size - 1, size - 1),),
        ((None, min(5000, size)),),
        ((size // 2, None),),
        # Past the end: a suffix longer than the object is all of it, and
        # a range ends at the object's last byte.
        ((None, size + 100),),
        ((max(size - 10, 0), size + 1000),),
    ]
    for edge in (e for e in EDGES if e < size):
        found += [((edge - 1, edge),), ((edge, edge),), ((edge - 1, edge - 1),)]
    last = (size - 1) // BLOCK * BLOCK
    if last > 0:
        found += [((last - 1, last),), ((last, size - 1),)]
    if size > 2 * BLOCK + 40:
        found.append(((BLOCK - 80, 2 * BLOCK + 40),))
    if size > BLOCK + 1:
        found.append(((0, 0), (BLOCK - 1, BLOCK), (size - 1, size - 1)))
    found += readv_edges(size)
    return list(dict.fromkeys(found))


def readv_edges(size: int) -> list[Request]:
    """Return requests at the edges of XRootD's reads of an object of size.

    They are, where the object is big enough: several ranges, of which the
    first fills one vector read chunk (READV_CHUNK), and then one byte
    more; as many one-byte ranges as one vector read holds
    (READV_CHUNKS), and one more; two ranges a byte more than one round's
    worth (READ_MAX) between them; and one range a byte more than a
    read's worth, short of the whole object.
    """
    found: list[Request] = []
    if size > READV_CHUNK + 1:
        found += [
            ((0, READV_CHUNK - 1), (size - 1, size - 1)),
            ((0, READV_CHUNK), (size - 1, size - 1)),
        ]
    for count in (READV_CHUNKS, READV_CHUNKS + 1):
        step = size // count
        if step >= 2:
            found.append(tuple((n * step, n * step) for n in range(count)))
    if size >= READ_MAX + 2:
        middle = size // 2
        found += [((0, middle), (middle + 2, size - 1)), ((1, None),)]
    return found


def content_range(value: str) -> tuple[int, int, int] | None:
    """Return a Content-Range header's first byte, last byte, and total.

    Returns:
        The three, or None if value is not `bytes <first>-<last>/<total>`.
    """
    m = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+)\s*", value)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_byteranges(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    """Return the parts of a multipart/byteranges body.

    Returns:
        Each part's Content-Range ("" if it has none) and bytes.

    Raises:
        ValueError: The content type names no boundary.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("multipart body without a boundary")
    delimiter = b"--" + m.group(1).encode()
    parts: list[tuple[str, bytes]] = []
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


def merge(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return inclusive spans, with those that overlap or touch joined."""
    merged: list[tuple[int, int]] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def check(data: bytes, request: Request, answer: web.Response) -> str | None:
    """Return what is wrong with a response to request, or None if nothing.

    A range with no byte in the object is passed over, as a server passes
    it over among others (RFC 9110); if every range is such, the answer
    must be 416. One range must come back as 206 with exactly its bytes.
    Several may come back as multipart/byteranges parts, in any grouping
    that covers them. A server may instead answer 200 with the whole
    object, to several ranges or to one that covers it all (RFC 9110 lets
    it ignore Range).

    Args:
        data: The object's bytes.
        request: The ranges asked for.
        answer: The response.
    """
    size = len(data)
    status, get, body = answer.status, answer.header, answer.body
    satisfiable = [r for r in request if not unsatisfiable(size, (r,))]
    if not satisfiable:
        if status == 416:
            return None
        return answer.describe() if status is None else f"HTTP {status}, not 416, for no byte"
    wanted = [resolve(size, r) for r in satisfiable]
    whole = merge(wanted) == [(0, size - 1)]
    if status == 200 and (len(request) > 1 or whole):
        return None if body == data else "HTTP 200, but the body is not the object"
    if status != 206:
        return answer.describe() if status is None else f"HTTP {status}, not 206"

    kind = get("content-type")
    if len(request) > 1 and kind.lower().startswith("multipart/byteranges"):
        try:
            parts = parse_byteranges(kind, body)
        except ValueError as e:
            return str(e)
    else:
        parts = [(get("content-range"), body)]

    got: list[tuple[int, int]] = []
    for value, payload in parts:
        found = content_range(value)
        if found is None:
            return f"bad Content-Range '{value}'"
        first, last, total = found
        if total != size:
            return f"Content-Range '{value}' gives the wrong size"
        if payload != data[first : last + 1]:
            return f"bytes {first}-{last} are not the object's ({len(payload)} bytes came)"
        got.append((first, last))
    covered = merge(got)
    for first, last in wanted:
        if not any(a <= first and last <= b for a, b in covered):
            return f"bytes {first}-{last} did not come back"
    if len(wanted) == 1 and len(parts) == 1:
        first, last, _ = content_range(parts[0][0]) or (0, 0, 0)
        if (first, last) != wanted[0]:
            return f"asked for {wanted[0][0]}-{wanted[0][1]}, got {first}-{last}"
    return None


def past_end(size: int) -> list[Request]:
    """Return ranges that start at or after the end of an object of size."""
    return [((size, None),), ((size, size),), ((size + 1000, size + 2000),)]


def unsatisfiable(size: int, request: Request) -> bool:
    """Return whether request asks for no byte of an object of size."""
    for start, end in request:
        if start is None:
            if (end or 0) > 0 and size > 0:
                return False
        elif start < size:
            return False
    return True


# --------------------------------------------------------------------------
# Several versions of an object.


def which_version(
    versions: dict[str, bytes], request: Request | None, answer: web.Response
) -> tuple[str | None, str]:
    """Return which of versions a response to request came from.

    Args:
        versions: Each version's bytes, by name.
        request: The ranges asked for, or None for the whole object.
        answer: The response.

    Returns:
        The version's name and "", if the response is entirely that
        one's; or None and why, if it is none of them, e.g. mixed, with
        another version's size, or an error.
    """
    status, body = answer.status, answer.body
    if status is None or 300 <= status < 400 or (status >= 400 and status != 416):
        return None, answer.describe()
    length = answer.header("content-length").strip()
    if length and length != str(len(body)):
        return None, f"Content-Length {length}, but {len(body)} bytes came"
    if request is None:
        if status != 200:
            return None, f"HTTP {status}, not 200"
        for name, data in versions.items():
            if body == data:
                return name, ""
        return None, f"{len(body)} bytes, but not any one version"
    if status == 416:
        m = re.fullmatch(r"\s*bytes\s+\*/(\d+)\s*", answer.header("content-range"))
        for name, data in versions.items():
            if unsatisfiable(len(data), request) and (not m or int(m.group(1)) == len(data)):
                return name, ""
        return None, "HTTP 416, but the range is satisfiable in that version"
    problems: list[str] = []
    for name, data in versions.items():
        why = check(data, request, answer)
        if why is None:
            return name, ""
        problems.append(f"{name}: {why}")
    return None, "; ".join(problems)


def freshness(cache_control: str) -> float | None:
    """Return how many seconds a shared cache treats an object as fresh.

    A shared cache takes s-maxage over max-age (RFC 9111, section
    5.2.2.10), and stores nothing under no-store or private. A V2 cache
    revalidates a no-cache copy at most every 5 seconds
    (NoCacheGracePeriod in local_cache/cache_control.go).

    Args:
        cache_control: The origin's Cache-Control.

    Returns:
        The seconds, or None if cache_control says nothing about that and
        the cache decides for itself.
    """
    ages: dict[str, float] = {}
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
                ages[name] = float(int(value.strip().strip('"')))
            except ValueError:
                pass
    if no_cache:
        return 5.0
    return ages.get("s-maxage", ages.get("max-age"))


# An object overwritten at the origins: its size, and by how much the new
# version grows or shrinks.
OVERWRITE_SIZE = 3 * XROOTD_BLOCK + 7
OVERWRITE_CHANGE = 3 * BLOCK + 11


# --------------------------------------------------------------------------
# Overlapping reads. Each is unaligned to both BLOCK and XROOTD_BLOCK, but
# for the object's own ends.

OVERLAP_SIZE = 3 * XROOTD_BLOCK + 1234


def overlap_plans() -> dict[str, list[Request]]:
    """Return plans of reads of an OVERLAP_SIZE object, by name.

    Each plan's reads go in order, on an object of its own. Every read
    after a plan's first covers both blocks that earlier ones fetched and
    blocks that they did not, at both block sizes; but `inside` asks only
    for what its first read fetched.
    """
    return {
        "head": [((140001, 150000),), ((125001, 141000),)],
        "tail": [((140001, 150000),), ((149001, 270000),)],
        "superset": [((140001, 150000),), ((120001, 280000),)],
        "inside": [((120001, 280000),), ((131001, 262200),)],
        "end": [((394001, 394449),), ((380001, 394100),)],
        "stagger": [
            ((1001, 9000),),
            ((8001, 140000),),
            ((139001, 265000),),
            ((264001, 394449),),
        ],
        "multi": [((140001, 150000),), ((141001, 142000), (300001, 301000))],
    }


def concurrent_reads() -> list[Request | None]:
    """Return reads of a cold OVERLAP_SIZE object, to send all at once.

    They are the same range twice, ranges that overlap their neighbors,
    and the whole object (None).
    """
    return [
        ((125001, 141000),),
        ((125001, 141000),),
        ((140001, 150000),),
        ((149001, 270000),),
        ((262001, 300000),),
        ((380001, 394449),),
        ((1001, 9000),),
        None,
    ]


def covering(request: Request, size: int, unit: int) -> list[int]:
    """Return the blocks that request touches in an object of size.

    Returns:
        The indexes of the blocks of unit bytes, in order.
    """
    found: set[int] = set()
    for r in request:
        first, last = resolve(size, r)
        found.update(range(first // unit, last // unit + 1))
    return sorted(found)


# --------------------------------------------------------------------------
# Ranges that add up to a whole object: in the order read, overlapping,
# and unaligned to both block sizes.

ASSEMBLE_SIZE = 4 * XROOTD_BLOCK + 777
ASSEMBLE: tuple[tuple[int, int], ...] = (
    (262400, 330000),
    (0, 60000),
    (459001, 524400),
    (131001, 200000),
    (520001, 525064),
    (59001, 131500),
    (329001, 393300),
    (199990, 262500),
    (393001, 460000),
)


def crc32c(data: bytes) -> int:
    """Return the CRC-32C (Castagnoli) of data."""
    c = 0xFFFFFFFF
    for byte in data:
        c = _CRC32C[(c ^ byte) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _crc32c_table() -> list[int]:
    table: list[int] = []
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
    """What an XRootD cache's .cinfo records.

    Attributes:
        bitmap: The blocks the cache holds: bit i of byte i/8 for block i.
    """

    block_size: int
    file_size: int
    bitmap: bytes

    @property
    def blocks(self) -> int:
        """How many blocks the object has."""
        return -(-self.file_size // self.block_size)

    @property
    def count(self) -> int:
        """How many of the object's blocks the cache holds."""
        return sum(1 for i in range(self.blocks) if self.bitmap[i // 8] >> (i % 8) & 1)

    @property
    def complete(self) -> bool:
        """Whether the cache holds every block."""
        return self.count == self.blocks


def parse_cinfo(data: bytes) -> Cinfo:
    """Return the blocks a .cinfo file records.

    Raises:
        ValueError: The data is not a .cinfo file that this understands.
    """
    head = _CINFO_VERSION.size + _CINFO_STORE.size + _CINFO_CRC.size
    if len(data) < head:
        raise ValueError(f"a .cinfo of {len(data)} bytes is too short")
    (version,) = _CINFO_VERSION.unpack_from(data)
    if version != 4:
        raise ValueError(f".cinfo version {version}, not 4")
    store = data[_CINFO_VERSION.size : _CINFO_VERSION.size + _CINFO_STORE.size]
    block_size, file_size, _, _, _, _, astats = _CINFO_STORE.unpack(store)
    (crc,) = _CINFO_CRC.unpack_from(data, _CINFO_VERSION.size + _CINFO_STORE.size)
    if crc != crc32c(store):
        raise ValueError("the .cinfo's store fails its CRC")
    if block_size <= 0 or file_size < 0 or astats < 0:
        raise ValueError(f"a .cinfo with block size {block_size}, file size {file_size}")
    blocks = -(-file_size // block_size)
    size = -(-max(blocks, 1) // 8)
    bitmap = data[head : head + size]
    if len(bitmap) < size:
        raise ValueError("the .cinfo's bitmap is cut short")
    rest = head + size + astats * CINFO_ASTAT
    if len(data) == rest + _CINFO_CRC.size:
        (crc,) = _CINFO_CRC.unpack_from(data, rest)
        if crc != crc32c(data[head:rest]):
            raise ValueError("the .cinfo's bitmap fails its CRC")
    return Cinfo(block_size, file_size, bitmap)


# --------------------------------------------------------------------------
# A cache's store on disk.


def cache_dir(cache: common.Cache) -> str:
    """Return where cache keeps its store (under framework/var).

    The cache mounts it at /data.
    """
    name = cache.svc[len("cache-") :] if cache.svc.startswith("cache-") else cache.svc
    return f"data/cache/{name}"


def store_bytes(cache: common.Cache) -> int | None:
    """Return how many bytes a cache's object files take in its store.

    Its store is the one it mounts at /data. A V2 cache's object files
    are in persistent-cache/objects/ (local_cache/storage.go in
    Pelican), counted by size. An XRootD cache's are in its data space,
    data/ (Cache.DataLocations), counted by the disk blocks they take, as
    XRootD counts them against Cache.FilesMaxSize (ResourceMonitor in
    XrdPfc). A file that the cache removes meanwhile counts for nothing.

    Returns:
        The total, or None if cache has no such store.
    """
    xrootd = cache.kind == "xrootd"
    root = f"{cache_dir(cache)}/{'data' if xrootd else 'persistent-cache/objects'}"
    if not os.path.isdir(root):
        return None
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                info = os.stat(os.path.join(directory, name))
            except FileNotFoundError:
                continue
            total += info.st_blocks * 512 if xrootd else info.st_size
    return total


def read_space_file(cache: common.Cache, name: str) -> tuple[bytes | None, str]:
    """Return a file of an XRootD cache's, by its name in namespace/.

    The cache links each file from namespace/, where its path in the
    federation puts it, to where it keeps it, in its data space (an
    object's bytes) or its meta space (its .cinfo), by a path in its own
    mount.

    Args:
        cache: The cache.
        name: The file's path under namespace/, e.g.
            protected-a/data/0.1.cinfo.

    Returns:
        A pair: the file's bytes and "", or None and why it could not be
        read.
    """
    root = cache_dir(cache)
    link = f"{root}/namespace/{name}"
    try:
        target = os.readlink(link)
    except FileNotFoundError:
        return None, f"framework/var/{link} is missing"
    except OSError:
        target = link
    if target.startswith("/data/"):
        target = f"{root}/{target[len('/data/'):]}"
    try:
        with open(target, "rb") as f:
            return f.read(), ""
    except FileNotFoundError:
        return None, f"framework/var/{target} is missing"
    except OSError as e:
        return None, f"framework/var/{target}: {e.strerror}"


def read_cinfo(cache: common.Cache, rel: str) -> tuple[Cinfo | None, str]:
    """Return what an XRootD cache's .cinfo of rel in /protected-a records.

    Returns:
        A pair: the record and "", or None and why it could not be read.
    """
    found, why = read_space_file(cache, f"protected-a/{rel}.cinfo")
    if found is None:
        return None, why
    try:
        return parse_cinfo(found), ""
    except ValueError as e:
        return None, f"the .cinfo of /protected-a/{rel}: {e}"


def xrootd_stored(cache: common.Cache, rel: str, data: bytes) -> tuple[int, list[str], str]:
    """Compare what an XRootD cache wrote of rel in /protected-a with data.

    The cache writes each block it fetches into a plain file of the
    object's, at the block's place, and records it in the object's
    .cinfo once the file holds it (XrdPfc's synced bitmap). So each block
    that the .cinfo records must be, in the file, data's bytes at that
    place; one it does not record yet is not compared. This reads the
    cache's store on disk, apart from anything the cache says.

    Returns:
        How many blocks were compared; what is wrong; and, where none
        could be, why not.
    """
    info, why = read_cinfo(cache, rel)
    if info is None:
        return 0, [], why
    if info.file_size != len(data):
        return 0, [f"its .cinfo records {info.file_size} bytes, not {len(data)}"], ""
    held, why = read_space_file(cache, f"protected-a/{rel}")
    if held is None:
        return 0, [f"its .cinfo records blocks, but {why}"], ""
    problems: list[str] = []
    if len(held) > len(data):
        problems.append(f"its file holds {len(held)} bytes, more than the object's")
    compared = 0
    for i in range(info.blocks):
        if not info.bitmap[i // 8] >> (i % 8) & 1:
            continue
        start = i * info.block_size
        want = data[start : start + info.block_size]
        if held[start : start + len(want)] != want:
            problems.append(f"block {i} (bytes {start}-{start + len(want) - 1}) is not the object's")
        compared += 1
    return compared, problems, ""


def xrootd_holds(cache: common.Cache, rel: str) -> bool | None:
    """Return whether an XRootD cache holds all of rel in /protected-a.

    Its .cinfo says (read_cinfo()); the cache removes it with the object
    when it purges the object.

    Returns:
        Whether it does, or None if the .cinfo is there but unreadable.
    """
    info, why = read_cinfo(cache, rel)
    if info is not None:
        return info.complete
    return False if why.endswith(" is missing") else None
