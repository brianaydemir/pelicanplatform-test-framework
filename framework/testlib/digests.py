"""Checksums as a server reports them in an RFC 3230 Digest header,
read as the Pelican client reads them (parseDigestHeader in Pelican's
client/handle_http.go): md5, sha (SHA-1), and sha-256 in base64; crc32,
crc32c, and adler32 in hex. XRootD writes crc32c in base64, which the
client accepts too (xrootd/xrootd#2456), and so does this.
"""

import base64
import binascii
import hashlib
import re
import zlib
from typing import Optional

from .blocks import crc32c

# What the tests ask for, one at a time, in Want-Digest.
ALGORITHMS = ("crc32c", "crc32", "adler32", "md5", "sha", "sha-256")
# Those in base64; the rest are 32-bit sums in hex.
BASE64 = ("md5", "sha", "sha-256")


def compute(algorithm: str, data: bytes) -> bytes:
    """data's digest by algorithm, one of ALGORITHMS."""
    if algorithm == "crc32c":
        return crc32c(data).to_bytes(4, "big")
    if algorithm == "crc32":
        return zlib.crc32(data).to_bytes(4, "big")
    if algorithm == "adler32":
        return zlib.adler32(data).to_bytes(4, "big")
    if algorithm == "md5":
        return hashlib.md5(data, usedforsecurity=False).digest()
    if algorithm == "sha":
        return hashlib.sha1(data, usedforsecurity=False).digest()
    return hashlib.sha256(data).digest()


def parse(header: str) -> dict[str, str]:
    """A Digest header's values, by algorithm (lowercased)."""
    found: dict[str, str] = {}
    for entry in header.split(","):
        name, sep, value = entry.strip().partition("=")
        if sep:
            found[name.strip().lower()] = value.strip()
    return found


def decode(algorithm: str, value: str) -> Optional[bytes]:
    """The digest that value, as reported for algorithm, stands for; None
    if it stands for none."""
    try:
        if algorithm in BASE64 or (algorithm == "crc32c" and re.fullmatch(r".{6}==", value)):
            return base64.b64decode(value, validate=True)
    except binascii.Error:
        return None
    # Leading zeros may be left out.
    if not re.fullmatch(r"[0-9a-fA-F]{1,8}", value):
        return None
    return int(value, 16).to_bytes(4, "big")


def check(header: str, data: bytes) -> tuple[list[str], list[str]]:
    """What is wrong with a Digest header for an object holding data, and
    the algorithms it reports rightly. An algorithm not in ALGORITHMS is
    passed over, as the client passes over one it does not know."""
    wrong: list[str] = []
    right: list[str] = []
    for algorithm, value in parse(header).items():
        if algorithm not in ALGORITHMS:
            continue
        got = decode(algorithm, value)
        if got is None:
            wrong.append(f"{algorithm} '{value}' is not a digest")
        elif got != compute(algorithm, data):
            wrong.append(f"{algorithm} '{value}' is not the object's")
        else:
            right.append(algorithm)
    return wrong, right
