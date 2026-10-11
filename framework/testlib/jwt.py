"""JSON Web Tokens of any header and claims, for the tokens that
`pelican token create` cannot make (credentials.mint()): one that lacks
a claim, or has one of any JSON type (`aud` as a list, numeric times);
one with any `kid`, or none; and one signed with HS256 or `none`, as an
attacker might send.

encode() signs ES256 and RS256 with the openssl CLI, since the standard
library cannot, and HS256 with hmac; verify() checks an ES256 token's
signature against a JWKS, with the openssl CLI too. The keys that the federation's
servers and the tests sign with are EC P-256 PEM files (ES256).
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Mapping
from typing import Any, Optional

from . import common

# The algorithms encode() can sign with.
ES256, RS256, HS256, NONE = "ES256", "RS256", "HS256", "none"

# The WLCG token profile's audience for any server, which `pelican token
# create` names unless told otherwise.
ANY_AUDIENCE = "https://wlcg.cern.ch/jwt/v1/any"

# DER's object identifiers, as encoded, of the keys key_id() knows.
_EC_KEY = bytes.fromhex("2a8648ce3d0201")  # id-ecPublicKey
_P256 = bytes.fromhex("2a8648ce3d030107")  # prime256v1
_RSA_KEY = bytes.fromhex("2a864886f70d010101")  # rsaEncryption


def b64(data: bytes) -> str:
    """data in base64url, unpadded, as JWS encodes each part."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    """The bytes that b64() made text from."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _openssl(args: list[str], data: bytes = b"") -> bytes:
    """What `openssl <args>` writes, given data; it must succeed."""
    if shutil.which("openssl") is None:
        common.die("the openssl command is missing, which signs ES256 and RS256 tokens")
    done = subprocess.run(  # nosec B603 B607
        ["openssl", *args], input=data, capture_output=True, check=False
    )
    if done.returncode != 0:
        common.die(f"openssl {args[0]} failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _der(data: bytes) -> list[bytes]:
    """The contents of each DER element in data, in order."""
    found: list[bytes] = []
    at = 0
    while at < len(data):
        length = data[at + 1]
        at += 2
        if length & 0x80:
            size = length & 0x7F
            length = int.from_bytes(data[at : at + size], "big")
            at += size
        found.append(data[at : at + length])
        at += length
    return found


def _pair(data: bytes) -> tuple[bytes, bytes]:
    """The contents of the two elements of the DER SEQUENCE that data
    is."""
    found = _der(_der(data)[0])
    if len(found) != 2:
        raise ValueError(f"a DER SEQUENCE of {len(found)} elements, not 2")
    return found[0], found[1]


def _public_key(key: str) -> tuple[list[bytes], bytes]:
    """The algorithm's object identifier and parameters, and the contents
    of the BIT STRING, of PEM private key file key's public key (its
    SubjectPublicKeyInfo)."""
    spki, bits = _pair(_openssl(["pkey", "-in", key, "-pubout", "-outform", "DER"]))
    return _der(spki), bits[1:]


def public_jwk(key: str) -> dict[str, str]:
    """PEM private key file key's public key, as a JWK's members that its
    RFC 7638 thumbprint covers: an EC P-256 or an RSA key's."""
    oids, bits = _public_key(key)
    if oids == [_EC_KEY, _P256] and len(bits) == 65 and bits[0] == 4:
        return {"crv": "P-256", "kty": "EC", "x": b64(bits[1:33]), "y": b64(bits[33:])}
    if oids[0] == _RSA_KEY:
        n, e = _pair(bits)
        return {"e": b64(e.lstrip(b"\0")), "kty": "RSA", "n": b64(n.lstrip(b"\0"))}
    common.die(f"{common.shown(key)} is neither an EC P-256 nor an RSA key")


def thumbprint(jwk: Mapping[str, str]) -> str:
    """The RFC 7638 thumbprint (SHA-256) of a public_jwk(): the kid that
    Pelican gives a key (jwk.AssignKeyID)."""
    members = json.dumps(dict(sorted(jwk.items())), separators=(",", ":"))
    return b64(hashlib.sha256(members.encode()).digest())


def key_id(key: str, jwks: Optional[bytes] = None) -> str:
    """The kid to name PEM private key file key by, as `pelican token
    create` picks it: the kid of the key in JWKS document jwks (e.g. an
    issuer's, or framework/var/issuer-jwks/) with the same public key,
    if there is one; otherwise the key's thumbprint()."""
    jwk = public_jwk(key)
    if jwks is not None:
        parsed = common.as_object(json.loads(jwks)) or {}
        for listed in common.as_array(parsed.get("keys")) or []:
            found = common.as_object(listed) or {}
            if all(found.get(k) == v for k, v in jwk.items()) and "kid" in found:
                return str(found["kid"])
    return thumbprint(jwk)


def algorithm(key: str) -> str:
    """The algorithm that PEM private key file key signs with: ES256 for an
    EC P-256 key, RS256 for an RSA one."""
    return ES256 if public_jwk(key)["kty"] == "EC" else RS256


def header(alg: str = ES256, kid: Optional[str] = None) -> dict[str, Any]:
    """A JWS header, as Pelican writes one: alg, kid (unless None), and typ."""
    found: dict[str, Any] = {"alg": alg}
    if kid is not None:
        found["kid"] = kid
    found["typ"] = "JWT"
    return found


def _ecdsa(der: bytes) -> bytes:
    """An ECDSA P-256 signature in the raw form JWS requires (r || s, 32
    bytes each), from the DER form openssl writes."""
    r, s = _pair(der)
    return int.from_bytes(r, "big").to_bytes(32, "big") + int.from_bytes(s, "big").to_bytes(
        32, "big"
    )


def encode(
    head: Mapping[str, Any],
    claims: Mapping[str, Any],
    key: Optional[str] = None,
    secret: Optional[bytes] = None,
    alg: Optional[str] = None,
) -> str:
    """A compact JWT of header head and claims, both as they are, signed
    by alg, or by the header's own "alg" if alg is None (so a header may
    name an algorithm other than the one that signed it):

      ES256, RS256  with key, a PEM private key file's path (under
                    framework/var), whose kind must suit
      HS256         with secret, any bytes, e.g. a public key's JWKS, as
                    in the classic key-confusion attack
      none          no signature: the token ends in "."
    """
    alg = str(head.get("alg", NONE)) if alg is None else alg
    signing_input = f"{b64(_json(head))}.{b64(_json(claims))}"
    if alg == NONE:
        signature = b""
    elif alg == HS256:
        if secret is None:
            raise ValueError("HS256 needs a secret")
        signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    elif alg in (ES256, RS256):
        if key is None:
            raise ValueError(f"{alg} needs a key")
        if algorithm(key) != alg:
            raise ValueError(f"{common.shown(key)} cannot sign {alg}")
        der = _openssl(["dgst", "-sha256", "-sign", key], signing_input.encode())
        signature = _ecdsa(der) if alg == ES256 else der
    else:
        raise ValueError(f"cannot sign {alg}")
    return f"{signing_input}.{b64(signature)}"


def decode(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A compact JWT's header and claims, unverified. Raises ValueError if
    it isn't one."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("not a compact JWT")
    head = common.as_object(json.loads(unb64(parts[0])))
    claims = common.as_object(json.loads(unb64(parts[1])))
    if head is None or claims is None:
        raise ValueError("not a compact JWT")
    return head, claims


# The DER of an EC P-256 SubjectPublicKeyInfo up to its point, which is
# 0x04, then x and y, 32 bytes each.
_P256_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def _der_integer(value: bytes) -> bytes:
    """A DER INTEGER of the unsigned big-endian value."""
    value = value.lstrip(b"\0") or b"\0"
    if value[0] & 0x80:
        value = b"\0" + value
    return bytes([0x02, len(value)]) + value


def verify(token: str, jwks: bytes) -> Optional[str]:
    """The kid of the key in JWKS document jwks that signed token, a
    compact JWT signed by ES256, as its header names it; None if its alg is
    not ES256, or none of the keys with its kid (or, without one, none of
    the EC P-256 keys) signed it. The openssl command checks each
    signature."""
    head, _ = decode(token)
    if head.get("alg") != ES256:
        return None
    signing_input, _, signature = token.strip().rpartition(".")
    raw = unb64(signature)
    if len(raw) != 64:
        return None
    body = _der_integer(raw[:32]) + _der_integer(raw[32:])
    der_signature = bytes([0x30, len(body)]) + body
    parsed = common.as_object(json.loads(jwks)) or {}
    for listed in common.as_array(parsed.get("keys")) or []:
        jwk = common.as_object(listed) or {}
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            continue
        if "kid" in head and jwk.get("kid") != head["kid"]:
            continue
        point = unb64(str(jwk.get("x", ""))) + unb64(str(jwk.get("y", "")))
        if len(point) != 64:
            continue
        spki = base64.encodebytes(_P256_SPKI + b"\x04" + point).decode("ascii")
        pem = f"-----BEGIN PUBLIC KEY-----\n{spki}-----END PUBLIC KEY-----\n"
        if _openssl_verifies(pem, der_signature, signing_input.encode()):
            return str(jwk.get("kid", ""))
    return None


def _openssl_verifies(pem: str, signature: bytes, data: bytes) -> bool:
    """Whether DER signature is of data's SHA-256 by public key pem."""
    if shutil.which("openssl") is None:
        common.die("the openssl command is missing, which checks ES256 signatures")
    with tempfile.TemporaryDirectory() as tmp:
        key, sig = os.path.join(tmp, "key.pem"), os.path.join(tmp, "sig.der")
        with open(key, "w", encoding="ascii") as f:
            f.write(pem)
        with open(sig, "wb") as f:
            f.write(signature)
        done = subprocess.run(  # nosec B603 B607
            ["openssl", "dgst", "-sha256", "-verify", key, "-signature", sig],
            input=data,
            capture_output=True,
            check=False,
        )
    return done.returncode == 0
