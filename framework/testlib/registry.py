"""The registry's own APIs (registry/registry.go and registry_ui.go in
Pelican): registering a prefix as a server or `pelican namespace
register` does, with keys of a run's own; reading the keys it holds; and
looking for private key material in a JWKS document.

A registration (POST /api/v1.0/registry) is a key-sign challenge in two
requests (keySignChallenge). The client sends a nonce and its JWKS; the
registry answers with a nonce of its own, and a payload it has signed.
The client then signs the two nonces, concatenated, with the key at
index 0 of its JWKS (ECDSA over SHA-256, ASN.1 DER, in hex:
utils.SignPayload), and sends everything back with the prefix. The
registry answers 201 if it registered the prefix, 200 if it was already
registered, 400 for a bad prefix or key, and 403 where key chaining or a
server's ownership of its name forbids it.
"""

import json
import os
import subprocess  # nosec B404
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from . import common, jwt, web

REGISTRY = "/api/v1.0/registry"
REGISTRATIONS = "/api/v1.0/registry_ui/namespaces"
SERVERS = "/api/v1.0/registry_ui/servers"
CHECK_STATUS = "/api/v1.0/registry/namespaces/check/status"

JSON = {"Content-Type": "application/json"}

# The members of a JWK that hold private key material: an EC key's d, and
# an RSA key's d, p, q, dp, dq, and qi (RFC 7518, section 6).
PRIVATE_MEMBERS = ("d", "p", "q", "dp", "dq", "qi")

# The scopes of the registry's own tokens (token_scopes/token_scopes.go).
NAMESPACE_DELETE = "pelican.namespace_delete"
EDIT_REGISTRATION = "registry.edit_registration"
DOWNTIME_CREATE = "pelican.downtime_create"
DOWNTIME_MODIFY = "pelican.downtime_modify"
DOWNTIME_DELETE = "pelican.downtime_delete"


def _openssl(args: Sequence[str], data: bytes = b"") -> bytes:
    """What `openssl <args>` writes, given data; it must succeed."""
    done = subprocess.run(  # nosec B603 B607
        ["openssl", *args], input=data, capture_output=True, check=False
    )
    if done.returncode != 0:
        common.die(f"openssl {args[0]} failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def new_key(path: str) -> str:
    """Make an EC P-256 private key at path (PKCS #8 PEM, which Pelican
    reads, as it does its own keys), unless one is there: path."""
    if not os.path.exists(path):
        _openssl(
            ["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", path]
        )
        os.chmod(path, 0o600)
    return path


def write_public_key(key: str, path: str) -> str:
    """Write PEM private key file key's public key to path, in PEM: path."""
    with open(path, "wb") as f:
        f.write(_openssl(["pkey", "-in", key, "-pubout"]))
    return path


def public_jwk(key: str) -> dict[str, str]:
    """PEM private key file key's public key, as Pelican publishes one of
    its own: with its thumbprint as its kid, its algorithm, and its use
    (registry_client.NamespaceRegister)."""
    found = jwt.public_jwk(key)
    return {**found, "kid": jwt.thumbprint(found), "alg": jwt.algorithm(key), "use": "sig"}


def jwks(keys: Sequence[str]) -> dict[str, Any]:
    """A JWKS of the public keys of keys, PEM private key files, in order:
    the first is the one a registration signs its challenge with."""
    return {"keys": [public_jwk(key) for key in keys]}


def sign_payload(key: str, payload: bytes) -> str:
    """payload's SHA-256, signed with PEM private key file key, as
    utils.SignPayload signs it (ASN.1 DER), in hex."""
    return _openssl(["dgst", "-sha256", "-sign", key], payload).hex()


def register(
    registry_url: str,
    prefix: str,
    keys: Sequence[str],
    site_name: str,
    server_id: str = "",
) -> web.Response:
    """Register prefix at the registry at registry_url, with the public
    keys of keys (PEM private key files), the first of which signs the
    challenge, as a server or `pelican namespace register` does, under
    site_name; for a server's prefix (/origins/... or /caches/...), with
    server_id, the ID it gives itself. The registry's answer to the
    challenge, if that failed, or else to the registration."""
    url = registry_url + REGISTRY
    keyset = jwks(keys)
    client_nonce = uuid.uuid4().hex + uuid.uuid4().hex
    first = web.request(
        "POST",
        url,
        upload=json.dumps({"client_nonce": client_nonce, "pubkey": keyset}).encode(),
        headers=JSON,
    )
    challenge = common.as_object(parse(first.body)) or {}
    server_nonce = str(challenge.get("server_nonce", ""))
    if first.status != 200 or not server_nonce:
        return first
    payload = client_nonce + server_nonce
    body = {
        "pubkey": keyset,
        "prefix": prefix,
        "site_name": site_name,
        "client_nonce": client_nonce,
        "server_nonce": server_nonce,
        "client_payload": payload,
        "client_signature": sign_payload(keys[0], payload.encode()),
        "server_payload": challenge.get("server_payload", ""),
        "server_signature": challenge.get("server_signature", ""),
        "access_token": "",  # nosec B105: none, as without an identity
        "identity_required": "false",
        "server_id": server_id,
    }
    return web.request("POST", url, upload=json.dumps(body).encode(), headers=JSON)


def token(
    key: str,
    scope: str,
    subject: str,
    issuer: str,
    audience: str = jwt.ANY_AUDIENCE,
    lifetime: int = 300,
    jwks_doc: Optional[bytes] = None,
) -> str:
    """A token of scope for subject, from issuer, for audience, signed
    with PEM private key file key, naming it by its kid in jwks_doc if it
    is there, and otherwise by its thumbprint (jwt.key_id()), lasting
    lifetime seconds: as a server mints those the registry takes."""
    now = int(time.time())
    claims = {
        "aud": audience,
        "exp": now + lifetime,
        "iat": now,
        "iss": issuer,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "scope": scope,
        "sub": subject,
        "wlcg.ver": "1.0",
    }
    head = jwt.header(jwt.algorithm(key), jwt.key_id(key, jwks_doc))
    return jwt.encode(head, claims, key)


def parse(body: bytes) -> Any:
    """body, decoded as one JSON document, or None if it isn't one."""
    try:
        return json.loads(body)
    except ValueError:
        return None


def documents(body: bytes) -> int:
    """How many JSON documents body holds, one after another, as a
    handler that writes an error, and then goes on to write another
    answer, sends; 0 if it doesn't start with one."""
    decoder = json.JSONDecoder()
    text = body.decode(errors="replace").strip()
    found = 0
    while text:
        try:
            _, end = decoder.raw_decode(text)
        except ValueError:
            break
        found += 1
        text = text[end:].lstrip()
    return found


def as_jwks(document: Any) -> Optional[list[dict[str, Any]]]:
    """The keys of a JWKS, given parsed or as its text, as a registration
    stores it (`pubkey`); None if it isn't one."""
    if isinstance(document, (str, bytes)):
        document = parse(document.encode() if isinstance(document, str) else document)
    listed = common.as_array((common.as_object(document) or {}).get("keys"))
    if listed is None:
        return None
    return [common.as_object(k) or {} for k in listed]


def private_members(document: Any) -> list[str]:
    """Each key in a JWKS (see as_jwks()) that holds private key
    material, by its kid, and which members it holds."""
    found: list[str] = []
    for key in as_jwks(document) or []:
        held = [m for m in PRIVATE_MEMBERS if m in key]
        if held:
            found.append(f"{key.get('kid', '(no kid)')}: {', '.join(held)}")
    return found


# The members of each kind of public key that its RFC 7638 thumbprint
# covers.
_THUMBPRINT_MEMBERS = {"EC": ("crv", "kty", "x", "y"), "RSA": ("e", "kty", "n")}


def thumbprints(document: Any) -> Optional[frozenset[str]]:
    """The RFC 7638 thumbprint of each public key in a JWKS (see
    as_jwks()), computed from its members rather than taken from its kid;
    None if it isn't a JWKS."""
    listed = as_jwks(document)
    if listed is None:
        return None
    found: set[str] = set()
    for key in listed:
        members = _THUMBPRINT_MEMBERS.get(str(key.get("kty", "")), ())
        if members and all(m in key for m in members):
            found.add(jwt.thumbprint({m: str(key[m]) for m in members}))
    return frozenset(found)


def key_thumbprints(keys: Sequence[str]) -> frozenset[str]:
    """The thumbprint of each of keys, PEM private key files."""
    return frozenset(jwt.thumbprint(jwt.public_jwk(key)) for key in keys)


def dir_keys(key_dir: str) -> list[str]:
    """The .pem files in key_dir (under framework/var): a server's keys,
    all of which it publishes."""
    try:
        names = sorted(n for n in os.listdir(key_dir) if n.endswith(".pem"))
    except FileNotFoundError:
        return []
    return [os.path.join(key_dir, n) for n in names]


def admin_metadata(registration: Mapping[str, Any]) -> dict[str, Any]:
    """A registration's admin_metadata."""
    return common.as_object(registration.get("admin_metadata")) or {}
