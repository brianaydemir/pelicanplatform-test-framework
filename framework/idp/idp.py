"""A small OpenID Connect provider, the `idp` service.

It stands in for an identity provider such as CILogon under
`-p auth-oidc`: the servers send their users here to log in
(OIDC.Issuer), and the registry asks it for a user's identity by the
device flow (`pelican namespace register --with-identity`). Python's
standard library alone: the servers read its ID tokens without checking
their signatures, so it signs them itself, RS256 with a key it makes as
it starts, and serves the public key as its JWKS.

Users come from a JSON file (IDP_USERS), by username:

  {"<username>": {"password": ..., "name": ..., "email": ...,
                  "groups": [...], "preferred_username": ...,
                  "sub": ..., "claims": {...}}}

Every field but the password is optional: preferred_username is the
username, sub is <issuer>/users/<username>, groups are none, and claims
are added to what userinfo and the ID token say. The groups come
whatever the scope, as CILogon's isMemberOf does, since Pelican asks a
provider it does not know for `openid email profile` alone. One client
is registered, whose ID and secret are in IDP_CLIENT_ID_FILE and
IDP_CLIENT_SECRET_FILE (the files that Pelican's servers read), and
whose redirect URIs are any server's OAuth callback (REDIRECT_URI).

  GET  /.well-known/openid-configuration   discovery
  GET  /jwks                               the signing key
  GET  /authorize                          the login form
  POST /authorize                          a login: a code sent back to
                                           the client's redirect URI
  POST /token                              the authorization_code,
                                           device_code, and refresh_token
                                           grants
  GET  /userinfo, POST /userinfo           the user's claims, for a
                                           bearer token in the header,
                                           the form, or the query
  POST /device/code                        a device flow's codes
  GET  /device, POST /device               the device flow's verification
                                           page, and a login that
                                           approves a user code
  GET  /healthz                            200

Every request is logged to IDP_LOG_DIR/requests.log, a JSON object per
line: when, the method and path, the status, and the grant, client, and
user it was for, if any; never a secret, code, or token.
"""

import base64
import functools
import hashlib
import html
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, cast
from urllib.parse import parse_qs, urlencode, urlsplit

ISSUER = os.environ.get("IDP_ISSUER", "https://idp:9007")
PORT = int(os.environ.get("IDP_PORT", "9007"))
USERS_FILE = os.environ.get("IDP_USERS", "/fed/idp/users.json")
CLIENT_ID_FILE = os.environ.get("IDP_CLIENT_ID_FILE", "/fed/generated/oidc-client-id")
CLIENT_SECRET_FILE = os.environ.get(
    "IDP_CLIENT_SECRET_FILE", "/fed/generated/oidc-client-secret"
)
LOG_DIR = os.environ.get("IDP_LOG_DIR", "/var/log/idp")
CERT_FILE = os.environ.get("IDP_CERT", "/certs/tls.crt")
KEY_FILE = os.environ.get("IDP_KEY", "/certs/tls.key")
RSA_BITS = int(os.environ.get("IDP_RSA_BITS", "2048"))

# How long each thing lasts, in seconds.
ACCESS_LIFETIME = 3600
REFRESH_LIFETIME = 86400
CODE_LIFETIME = 300
DEVICE_LIFETIME = 600
# How often a client may poll a device flow.
DEVICE_INTERVAL = 1

SCOPES = ("openid", "profile", "email", "groups", "offline_access")
# A Pelican server's OAuth callback, on any host and port.
REDIRECT_URI = re.compile(r"https://[A-Za-z0-9.-]+(:\d+)?/api/v1\.0/auth/oauth/callback")

CODE_GRANT = "authorization_code"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"

# DigestInfo for SHA-256 (RFC 8017, section 9.2, note 1).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
SMALL_PRIMES = [
    p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p**0.5) + 1, 2))
]


def b64(data: bytes) -> str:
    """Return data in base64url, unpadded (RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def now() -> int:
    """Return the time, in whole seconds."""
    return int(time.time())


def as_dict(value: Any) -> dict[str, Any]:
    """Return value, a parsed JSON value, if it is an object; else {}."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """Return value, a parsed JSON value, if it is an array; else []."""
    if not isinstance(value, list):
        return []
    # mypy takes value for a list[Any] here, and so would call a cast
    # redundant.
    items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
    return items


# --------------------------------------------------------------------------
# The signing key: RSA, made here, since nothing else is at hand.


def is_probable_prime(n: int) -> bool:
    """Return whether n is prime, by trial division, then Miller-Rabin.

    Miller-Rabin runs 32 rounds.
    """
    if n < 2:
        return False
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(32):
        a = 2 + secrets.randbelow(n - 3)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def random_prime(bits: int) -> int:
    """Return a prime of bits bits, its top two bits set.

    With them set, a product of two has 2 * bits bits.
    """
    while True:
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if is_probable_prime(candidate):
            return candidate


class RsaKey:
    """An RSA key pair, and what signs and publishes with it.

    Attributes:
        size: The modulus's length, in bytes.
        kid: The key's ID: its RFC 7638 thumbprint.
        jwk: The public key, as a JWK.
    """

    def __init__(self, bits: int):
        e = 65537
        while True:
            p, q = random_prime(bits // 2), random_prime(bits // 2)
            if p == q:
                continue
            phi = (p - 1) * (q - 1)
            if phi % e:
                break
        self.n, self.e, self.d = p * q, e, pow(e, -1, phi)
        self.size = (self.n.bit_length() + 7) // 8
        members = {"e": b64(self.bytes_of(e)), "kty": "RSA", "n": b64(self.bytes_of(self.n))}
        serialized = json.dumps(members, separators=(",", ":"), sort_keys=True)
        # As Pelican names its own keys.
        self.kid = b64(hashlib.sha256(serialized.encode()).digest())
        self.jwk = {**members, "kid": self.kid, "alg": "RS256", "use": "sig"}

    @staticmethod
    def bytes_of(value: int) -> bytes:
        """Return value, big-endian, as short as can be."""
        return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")

    def sign(self, data: bytes) -> bytes:
        """Return data's RSASSA-PKCS1-v1_5 SHA-256 signature (RFC 8017)."""
        digest_info = SHA256_PREFIX + hashlib.sha256(data).digest()
        padding = b"\xff" * (self.size - len(digest_info) - 3)
        encoded = b"\x00\x01" + padding + b"\x00" + digest_info
        return pow(int.from_bytes(encoded, "big"), self.d, self.n).to_bytes(self.size, "big")

    def jwt(self, claims: dict[str, Any]) -> str:
        """Return claims as a compact JWS, RS256, naming the key."""
        header = {"alg": "RS256", "kid": self.kid, "typ": "JWT"}
        signing_input = (
            b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + b64(json.dumps(claims, separators=(",", ":")).encode())
        )
        return signing_input + "." + b64(self.sign(signing_input.encode()))


# --------------------------------------------------------------------------
# The users and the client.


def read_file(path: str) -> str:
    """Return the stripped contents of the file at path."""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Users:
    """The users, from USERS_FILE, read again whenever it changes.

    Rereading it lets a user added while the provider runs log in.

    Attributes:
        stamp: The file's modification time when last read; None before.
        found: The users as last read, by username.
    """

    def __init__(self, path: str):
        self.path = path
        self.stamp: Optional[float] = None
        self.found: dict[str, dict[str, Any]] = {}

    def load(self) -> dict[str, dict[str, Any]]:
        """Return the users, by username."""
        try:
            stamp = os.stat(self.path).st_mtime
        except OSError:
            return self.found
        if stamp != self.stamp:
            with open(self.path, encoding="utf-8") as f:
                parsed = as_dict(json.load(f))
            self.found = {}
            for name, user in parsed.items():
                record = as_dict(user)
                if record:
                    self.found[name] = record
            self.stamp = stamp
        return self.found

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Return username, if password is theirs; None otherwise."""
        user = self.load().get(username)
        if user is None or not password:
            return None
        if not secrets.compare_digest(str(user.get("password", "")), password):
            return None
        return username

    def claims(self, username: str, scope: list[str]) -> dict[str, Any]:
        """Return what userinfo and the ID token say of username.

        Which claims depends on scope, the scopes granted: profile claims
        with `profile`, and email with `email`; sub, iss, and groups
        always, since Pelican asks a provider it does not know for
        `openid email profile` alone (OIDC.Scopes), and reads the groups
        from whatever claim Issuer.OIDCGroupClaim names, as CILogon sends
        isMemberOf unasked.
        """
        user = self.load().get(username, {})
        sub = str(user.get("sub") or f"{ISSUER}/users/{username}")
        found: dict[str, Any] = {"sub": sub, "iss": ISSUER}
        if "profile" in scope:
            found["preferred_username"] = str(user.get("preferred_username") or username)
            found["name"] = str(user.get("name") or username)
        if "email" in scope:
            found["email"] = str(user.get("email") or f"{username}@idp.test")
            found["email_verified"] = True
        found["groups"] = [str(g) for g in as_list(user.get("groups"))]
        found.update(as_dict(user.get("claims")))
        return found


def wanted_scope(asked: str) -> list[str]:
    """Return the scopes granted of those asked, a space-separated list.

    Those granted are those in SCOPES, or all of SCOPES if none of them
    is asked for.
    """
    found = [s for s in asked.split() if s in SCOPES]
    return found or list(SCOPES)


# --------------------------------------------------------------------------
# The provider's state: codes, tokens, and device flows, in memory.


class Provider:
    """The key, the users, the client, and what has been issued.

    Attributes:
        codes: Authorization codes' grants, by code.
        access: Access tokens' grants, by token.
        refresh: Refresh tokens' grants, by token.
        devices: Device flows, by device code.
        user_codes: The device flows' device codes, by user code.
    """

    def __init__(self) -> None:
        self.key = RsaKey(RSA_BITS)
        self.users = Users(USERS_FILE)
        self.client_id = read_file(CLIENT_ID_FILE)
        self.client_secret = read_file(CLIENT_SECRET_FILE)
        self.lock = threading.Lock()
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.user_codes: dict[str, str] = {}

    def client_ok(self, client_id: str, client_secret: Optional[str]) -> bool:
        """Return whether client_id and client_secret are the client's.

        client_secret is checked only if one is given.
        """
        if client_id != self.client_id:
            return False
        if client_secret is None:
            return True
        return secrets.compare_digest(client_secret, self.client_secret)

    def sweep(self) -> None:
        """Forget what has expired."""
        moment = now()
        for table in (self.codes, self.access, self.refresh):
            for value in [v for v, row in table.items() if row["expires"] <= moment]:
                del table[value]
        for value in [v for v, row in self.devices.items() if row["expires"] <= moment]:
            self.user_codes.pop(self.devices[value]["user_code"], None)
            del self.devices[value]

    def issue_code(self, user: str, redirect_uri: str, scope: list[str], nonce: str) -> str:
        """Return a code for user, to be redeemed at redirect_uri."""
        code = secrets.token_urlsafe(32)
        with self.lock:
            self.sweep()
            self.codes[code] = {
                "user": user,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "nonce": nonce,
                "expires": now() + CODE_LIFETIME,
            }
        return code

    def redeem_code(self, code: str, redirect_uri: str) -> Optional[dict[str, Any]]:
        """Return the code's grant, once.

        Returns:
            The grant; None if there is none, or if redirect_uri is not
            the one it was issued for.
        """
        with self.lock:
            self.sweep()
            found = self.codes.pop(code, None)
        if found is None or found["redirect_uri"] != redirect_uri:
            return None
        return found

    def issue_tokens(self, user: str, scope: list[str], nonce: str = "") -> dict[str, Any]:
        """Return the token endpoint's answer for user.

        Returns:
            The answer: an access token, a refresh token, and with
            `openid` an ID token.
        """
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        moment = now()
        with self.lock:
            self.sweep()
            self.access[access] = {
                "user": user,
                "scope": scope,
                "expires": moment + ACCESS_LIFETIME,
            }
            self.refresh[refresh] = {
                "user": user,
                "scope": scope,
                "expires": moment + REFRESH_LIFETIME,
            }
        answer: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",  # nosec B105
            "expires_in": ACCESS_LIFETIME,
            "refresh_token": refresh,
            "scope": " ".join(scope),
        }
        if "openid" in scope:
            claims = {
                **self.users.claims(user, scope),
                "aud": self.client_id,
                "exp": moment + ACCESS_LIFETIME,
                "iat": moment,
                "auth_time": moment,
            }
            if nonce:
                claims["nonce"] = nonce
            answer["id_token"] = self.key.jwt(claims)
        return answer

    def redeem_refresh(self, token: str) -> Optional[dict[str, Any]]:
        """Return the refresh token's grant, once; None if there is none."""
        with self.lock:
            self.sweep()
            return self.refresh.pop(token, None)

    def bearer(self, token: str) -> Optional[dict[str, Any]]:
        """Return the access token's grant; None if there is none."""
        with self.lock:
            self.sweep()
            return self.access.get(token)

    def start_device(self, scope: list[str]) -> dict[str, Any]:
        """Return a new device flow's codes (RFC 8628, section 3.2)."""
        device_code = secrets.token_urlsafe(32)
        user_code = "-".join(secrets.token_hex(2).upper() for _ in range(2))
        with self.lock:
            self.sweep()
            self.devices[device_code] = {
                "user_code": user_code,
                "scope": scope,
                "user": None,
                "expires": now() + DEVICE_LIFETIME,
            }
            self.user_codes[user_code] = device_code
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": f"{ISSUER}/device",
            "verification_uri_complete": f"{ISSUER}/device?user_code={user_code}",
            "expires_in": DEVICE_LIFETIME,
            "interval": DEVICE_INTERVAL,
        }

    def approve_device(self, user_code: str, user: str) -> bool:
        """Approve the device flow with user_code as user.

        Returns:
            Whether there was one to approve.
        """
        with self.lock:
            self.sweep()
            device_code = self.user_codes.get(user_code.strip().upper())
            if device_code is None:
                return False
            self.devices[device_code]["user"] = user
            return True

    def redeem_device(self, device_code: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Return the device flow's state, and once approved, its grant.

        Returns:
            The state, `pending`, `expired`, `invalid`, or `approved`; and
            with `approved`, once, the grant, else None.
        """
        with self.lock:
            moment = now()
            found = self.devices.get(device_code)
            if found is None:
                return "invalid", None
            if found["expires"] <= moment:
                self.user_codes.pop(found["user_code"], None)
                del self.devices[device_code]
                return "expired", None
            if found["user"] is None:
                return "pending", None
            self.user_codes.pop(found["user_code"], None)
            del self.devices[device_code]
            return "approved", found


@functools.lru_cache(maxsize=None)
def provider() -> Provider:
    """Return the provider, made once."""
    return Provider()


# --------------------------------------------------------------------------
# The pages.

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:sans-serif;margin:3em;max-width:30em}}label{{display:block;margin:.6em 0}}
input{{width:100%;box-sizing:border-box}}.error{{color:#b00}}</style></head>
<body><h1>{title}</h1>{body}</body></html>
"""


def login_form(action: str, fields: dict[str, str], error: str = "") -> str:
    """Return a page with a login form posting fields, hidden, to action.

    Any user_code among fields is shown instead, for the user to edit.
    """
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items()
    )
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    shown = ""
    if "user_code" in fields:
        shown = f'<label>Code<input name="user_code" value="{html.escape(fields["user_code"])}"></label>'
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in fields.items()
            if k != "user_code"
        )
    body = (
        f'{message}<form method="post" action="{html.escape(action)}">{hidden}{shown}'
        '<label>Username<input name="username" autofocus></label>'
        '<label>Password<input name="password" type="password"></label>'
        '<button type="submit">Log in</button></form>'
    )
    return PAGE.format(title="pelican-testing identity provider", body=body)


def message_page(title: str, text: str) -> str:
    """Return a page that says text."""
    return PAGE.format(title=html.escape(title), body=f"<p>{html.escape(text)}</p>")


# --------------------------------------------------------------------------
# The requests.


class Handler(BaseHTTPRequestHandler):
    """One request.

    Attributes:
        noted: What record() says of this request, beyond the method and
            path.
    """

    protocol_version = "HTTP/1.1"
    server_version = "pelican-testing-idp/1"

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        """Log nothing: record() logs."""

    noted: dict[str, Any]

    def record(self, status: int) -> None:
        """Log the request and its status."""
        entry = {
            "time": round(time.time(), 3),
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "status": status,
            **getattr(self, "noted", {}),
        }
        line = json.dumps(entry, separators=(",", ":"))
        print(line, flush=True)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "requests.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"cannot write {LOG_DIR}/requests.log: {e}", file=sys.stderr, flush=True)

    def reply(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Send status and body."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.record(status)

    def reply_json(
        self, status: int, data: Any, headers: Optional[dict[str, str]] = None
    ) -> None:
        """Send data as JSON."""
        self.reply(status, json.dumps(data).encode(), "application/json", headers)

    def reply_html(self, status: int, page: str) -> None:
        """Send page."""
        self.reply(status, page.encode(), "text/html; charset=utf-8")

    def oauth_error(self, status: int, error: str, description: str) -> None:
        """Send an OAuth error (RFC 6749, section 5.2)."""
        self.noted["error"] = error
        self.reply_json(status, {"error": error, "error_description": description})

    def read_form(self) -> dict[str, str]:
        """Return the request's body, a form: each field's first value."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        return {k: v[0] for k, v in parse_qs(body.decode(errors="replace")).items()}

    def query(self) -> dict[str, str]:
        """Return the request's query: each field's first value."""
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def client_credentials(self, form: dict[str, str]) -> tuple[str, Optional[str]]:
        """Return the client's ID and secret (RFC 6749, section 2.3.1).

        Returns:
            The ID and the secret, from HTTP Basic authentication or the
            form; the secret None if none came.
        """
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                pair = base64.b64decode(auth[6:]).decode()
            except (ValueError, UnicodeDecodeError):
                return "", None
            client_id, _, secret = pair.partition(":")
            return client_id, secret
        return form.get("client_id", ""), form.get("client_secret")

    # Routing.

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Answer a GET."""
        self.noted = {}
        path = urlsplit(self.path).path
        if path == "/.well-known/openid-configuration":
            self.discovery()
        elif path == "/jwks":
            self.reply_json(200, {"keys": [provider().key.jwk]})
        elif path == "/healthz":
            self.reply(200, b"ok\n", "text/plain")
        elif path == "/authorize":
            self.authorize_page(self.query())
        elif path == "/userinfo":
            self.userinfo(self.query())
        elif path == "/device":
            self.device_page(self.query())
        elif path == "/":
            self.reply_html(200, message_page("pelican-testing identity provider", ISSUER))
        else:
            self.reply_json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:  # pylint: disable=invalid-name
        """Answer a HEAD: as a GET, without the body."""
        self.do_GET()

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """Answer a POST."""
        self.noted = {}
        path = urlsplit(self.path).path
        form = self.read_form()
        if path == "/authorize":
            self.authorize_login(form)
        elif path == "/token":
            self.token(form)
        elif path == "/userinfo":
            self.userinfo(form)
        elif path == "/device/code":
            self.device_code(form)
        elif path == "/device":
            self.device_login(form)
        else:
            self.reply_json(404, {"error": "not_found"})

    # The endpoints.

    def discovery(self) -> None:
        """Send the provider's metadata (OpenID Connect Discovery 1.0)."""
        self.reply_json(
            200,
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "userinfo_endpoint": f"{ISSUER}/userinfo",
                "jwks_uri": f"{ISSUER}/jwks",
                "device_authorization_endpoint": f"{ISSUER}/device/code",
                "response_types_supported": ["code"],
                "grant_types_supported": [CODE_GRANT, DEVICE_GRANT, REFRESH_GRANT],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": list(SCOPES),
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "claims_supported": [
                    "sub",
                    "iss",
                    "aud",
                    "exp",
                    "iat",
                    "name",
                    "preferred_username",
                    "email",
                    "email_verified",
                    "groups",
                ],
                "code_challenge_methods_supported": ["S256", "plain"],
            },
        )

    def authorize_fields(self, given: dict[str, str]) -> Optional[dict[str, str]]:
        """Check and return the authorization request's parameters.

        Returns:
            The parameters (RFC 6749, section 4.1.1); None, having
            answered, if any is wrong.
        """
        fields = {
            k: given.get(k, "") for k in ("response_type", "client_id", "redirect_uri", "scope")
        }
        for k in ("state", "nonce"):
            if given.get(k):
                fields[k] = given[k]
        self.noted["client_id"] = fields["client_id"]
        if not provider().client_ok(fields["client_id"], None):
            self.reply_json(
                400, {"error": "invalid_client", "error_description": "unknown client"}
            )
            return None
        if not REDIRECT_URI.fullmatch(fields["redirect_uri"]):
            self.reply_json(
                400,
                {"error": "invalid_request", "error_description": "redirect_uri not allowed"},
            )
            return None
        if fields["response_type"] != "code":
            self.redirect_error(fields, "unsupported_response_type")
            return None
        return fields

    def redirect_error(self, fields: dict[str, str], error: str) -> None:
        """Send the client's user back with an error."""
        self.noted["error"] = error
        self.redirect(
            fields["redirect_uri"], {"error": error, "state": fields.get("state", "")}
        )

    def redirect(self, redirect_uri: str, params: dict[str, str]) -> None:
        """Send the user to redirect_uri with params in its query."""
        sent = {k: v for k, v in params.items() if v}
        joiner = "&" if "?" in redirect_uri else "?"
        location = redirect_uri + joiner + urlencode(sent)
        self.reply(302, b"", "text/plain", {"Location": location})

    def authorize_page(self, query: dict[str, str]) -> None:
        """Answer GET /authorize with the login form."""
        fields = self.authorize_fields(query)
        if fields is not None:
            self.reply_html(200, login_form("/authorize", fields))

    def authorize_login(self, form: dict[str, str]) -> None:
        """Answer POST /authorize, a login, with the code it earns."""
        fields = self.authorize_fields(form)
        if fields is None:
            return
        user = provider().users.authenticate(form.get("username", ""), form.get("password", ""))
        if user is None:
            self.noted["error"] = "bad login"
            self.reply_html(
                401, login_form("/authorize", fields, "Wrong username or password.")
            )
            return
        self.noted["user"] = user
        code = provider().issue_code(
            user, fields["redirect_uri"], wanted_scope(fields["scope"]), fields.get("nonce", "")
        )
        self.redirect(fields["redirect_uri"], {"code": code, "state": fields.get("state", "")})

    def token(self, form: dict[str, str]) -> None:
        """Answer POST /token, by its grant type."""
        grant = form.get("grant_type", "")
        self.noted["grant_type"] = grant
        client_id, secret = self.client_credentials(form)
        self.noted["client_id"] = client_id
        if secret is None or not provider().client_ok(client_id, secret):
            self.reply_json(
                401,
                {
                    "error": "invalid_client",
                    "error_description": "client authentication failed",
                },
                {"WWW-Authenticate": 'Basic realm="idp"'},
            )
            return
        if grant == CODE_GRANT:
            found = provider().redeem_code(form.get("code", ""), form.get("redirect_uri", ""))
            if found is None:
                self.oauth_error(400, "invalid_grant", "unknown, used, or expired code")
                return
            self.noted["user"] = found["user"]
            self.reply_json(
                200, provider().issue_tokens(found["user"], found["scope"], found["nonce"])
            )
        elif grant == DEVICE_GRANT:
            state, found = provider().redeem_device(form.get("device_code", ""))
            if state == "pending":
                self.oauth_error(
                    400, "authorization_pending", "the user has not approved it yet"
                )
            elif state == "expired":
                self.oauth_error(400, "expired_token", "the device code has expired")
            elif found is None:
                self.oauth_error(400, "invalid_grant", "unknown or used device code")
            else:
                self.noted["user"] = found["user"]
                self.reply_json(200, provider().issue_tokens(found["user"], found["scope"]))
        elif grant == REFRESH_GRANT:
            found = provider().redeem_refresh(form.get("refresh_token", ""))
            if found is None:
                self.oauth_error(
                    400, "invalid_grant", "unknown, used, or expired refresh token"
                )
                return
            self.noted["user"] = found["user"]
            self.reply_json(200, provider().issue_tokens(found["user"], found["scope"]))
        else:
            self.oauth_error(400, "unsupported_grant_type", f"no such grant: {grant}")

    def userinfo(self, params: dict[str, str]) -> None:
        """Answer GET or POST /userinfo with the claims of the token's user.

        The token comes in the Authorization header, or as access_token in
        params, the form or the query (RFC 6750).
        """
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else params.get("access_token", "")
        found = provider().bearer(token) if token else None
        if found is None:
            self.noted["error"] = "invalid_token"
            self.reply_json(
                401,
                {"error": "invalid_token"},
                {"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            return
        self.noted["user"] = found["user"]
        self.reply_json(200, provider().users.claims(found["user"], found["scope"]))

    def device_code(self, form: dict[str, str]) -> None:
        """Answer POST /device/code: start a device flow (RFC 8628)."""
        client_id, secret = self.client_credentials(form)
        self.noted["client_id"] = client_id
        if not provider().client_ok(client_id, secret):
            self.oauth_error(401, "invalid_client", "unknown client")
            return
        self.reply_json(200, provider().start_device(wanted_scope(form.get("scope", ""))))

    def device_page(self, query: dict[str, str]) -> None:
        """Answer GET /device with the verification page.

        The user code is filled in if the query names one.
        """
        self.reply_html(200, login_form("/device", {"user_code": query.get("user_code", "")}))

    def device_login(self, form: dict[str, str]) -> None:
        """Answer POST /device, a login that approves a user code."""
        fields = {"user_code": form.get("user_code", "")}
        user = provider().users.authenticate(form.get("username", ""), form.get("password", ""))
        if user is None:
            self.noted["error"] = "bad login"
            self.reply_html(401, login_form("/device", fields, "Wrong username or password."))
            return
        self.noted["user"] = user
        if not provider().approve_device(fields["user_code"], user):
            self.noted["error"] = "unknown code"
            self.reply_html(
                404, login_form("/device", fields, "No such code, or it has expired.")
            )
            return
        self.reply_html(
            200, message_page("Approved", f"Approved as {user}. You may close this page.")
        )


def main() -> None:
    """Serve."""
    started = time.monotonic()
    made = provider()
    print(
        f"{ISSUER}: serving on port {PORT}, key {made.key.kid[:8]}... made in"
        + f" {time.monotonic() - started:.1f}s, {len(made.users.load())} users",
        flush=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    server = ThreadingHTTPServer(("", PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
