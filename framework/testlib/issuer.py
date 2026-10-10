"""Pelican's embedded OAuth2 issuer, as a client of it: a token for a
user through the device flow (RFC 8628), approved on the user's behalf.

A client asks the issuer to start a flow, and polls for the token while
the user approves the flow at the issuer's verification page. Here the
tests play the user too: the page takes the same bearer token as the
server's web API (credentials.mint_user()), so the tests approve with
one, as Pelican's own integration tests do
(oauth2/issuer/integration_test.go). The issuer grants what the user is
allowed, narrowed to what the client asked for and may carry
(handleDeviceVerifySubmit in oauth2/issuer/handlers.go).

A client that registers itself is bound to the first user who approves
a flow for it, and the issuer allows an address five registrations,
then one a minute (oauth2/issuer/rate_limit.go). So the tests register
none: the server's admin creates one client of each issuer through the
issuer's admin API, and every user uses it.

An origin serves an issuer for each export that has one, at
<web URL>/api/v1.0/issuer/ns<prefix>, and a local issuer for its own
web API at <web URL>/api/v1.0/issuer/ns/pelican/local-issuer, whose
tokens name credentials.local_issuer() as their issuer.
"""

import base64
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from . import common, web

ROUTE = "/api/v1.0/issuer/ns"
LOCAL_NAMESPACE = "/pelican/local-issuer"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# What a client the tests create may carry: what Pelican gives a client
# that registers itself (handleDynamicClientRegistration), and the scope
# a share's token carries.
CLIENT_SCOPES = (
    "openid",
    "offline_access",
    "wlcg",
    "storage.read:/",
    "storage.modify:/",
    "storage.create:/",
    "collection.read:/",
    "collection.create:/",
    "collection.modify:/",
    "collection.delete:/",
    "share.access:/",
)

# How many times to ask for the token after approving, a second apart.
POLLS = 12

JSON = {"Content-Type": "application/json"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class IssuerError(Exception):
    """The issuer did not do as asked."""


@dataclass(frozen=True)
class Client:
    """A client of one issuer."""

    url: str  # where the issuer serves: <web URL>/api/v1.0/issuer/ns<namespace>
    client_id: str
    client_secret: str
    device_url: str  # its device authorization endpoint
    token_url: str  # its token endpoint

    @property
    def auth(self) -> dict[str, str]:
        """The header that authenticates the client."""
        pair = f"{self.client_id}:{self.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}


def url(web_url: str, namespace: str) -> str:
    """Where the server at web_url serves its issuer of namespace."""
    return f"{web_url}{ROUTE}{namespace}"


def decoded(answer: web.Response) -> dict[str, Any]:
    """The JSON object in answer's body, or {}."""
    try:
        return common.as_object(json.loads(answer.body)) or {}
    except ValueError:
        return {}


def failed(what: str, answer: web.Response) -> IssuerError:
    """An error saying how what went."""
    return IssuerError(
        f"{what}: {answer.describe()}: {answer.body[:200].decode(errors='replace')}"
    )


def create_client(web_url: str, namespace: str, admin_token: str) -> Client:
    """Have the server at web_url, as its admin, create a client of its
    issuer of namespace (LOCAL_NAMESPACE for its local one), and look up
    the issuer's endpoints."""
    target = f"{web_url}/api/v1.0/issuer/admin/ns{namespace}/clients"
    body = {
        "client_name": "pelican-test-framework",
        "grant_types": [DEVICE_GRANT],
        "scopes": list(CLIENT_SCOPES),
    }
    made = web.request(
        "POST", target, token=admin_token, upload=json.dumps(body).encode(), headers=JSON
    )
    client = decoded(made)
    if made.status != 201 or not client.get("client_id"):
        raise failed(f"POST {target}", made)
    issuer_url = url(web_url, namespace)
    discovery = f"{issuer_url}/.well-known/openid-configuration"
    found = web.request("GET", discovery)
    endpoints = decoded(found)
    if found.status != 200 or "device_authorization_endpoint" not in endpoints:
        raise failed(f"GET {discovery}", found)
    return Client(
        issuer_url,
        str(client["client_id"]),
        str(client["client_secret"]),
        str(endpoints["device_authorization_endpoint"]),
        str(endpoints["token_endpoint"]),
    )


def cookie(answer: web.Response, name: str) -> str:
    """The value of cookie name that answer set, or ""."""
    for key, value in answer.headers:
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value[len(name) + 1 :].split(";", 1)[0]
    return ""


def approve(issuer_url: str, user_code: str, user_token: str) -> None:
    """Approve the device flow with user_code at issuer_url, as the user
    whose web API token user_token is. The verification page sets a CSRF
    cookie, which the approval sends back."""
    page = f"{issuer_url}/device"
    shown = web.request("GET", f"{page}?user_code={user_code}", token=user_token)
    csrf = cookie(shown, "csrf_token")
    if shown.status != 200 or not csrf:
        raise failed(f"GET {page}", shown)
    body = {"user_code": user_code, "action": "approve", "csrf_token": csrf}
    done = web.request(
        "POST",
        page,
        token=user_token,
        upload=json.dumps(body).encode(),
        headers={**JSON, "Cookie": f"csrf_token={csrf}"},
    )
    if done.status != 200 or decoded(done).get("status") != "approved":
        raise failed(f"approving {user_code} at {page}", done)


def token(client: Client, user_token: str, wanted: Sequence[str]) -> str:
    """A token from client's issuer for the user whose web API token
    user_token is, asking for the scopes wanted, which the issuer narrows
    to what the user is allowed."""
    form = urlencode({"scope": " ".join(wanted)}).encode()
    started = web.request(
        "POST", client.device_url, upload=form, headers={**FORM, **client.auth}
    )
    flow = decoded(started)
    if started.status != 200 or not flow.get("device_code"):
        raise failed(f"POST {client.device_url}", started)
    approve(client.url, str(flow["user_code"]), user_token)
    form = urlencode({"grant_type": DEVICE_GRANT, "device_code": flow["device_code"]}).encode()
    for _ in range(POLLS):
        polled = web.request(
            "POST", client.token_url, upload=form, headers={**FORM, **client.auth}
        )
        got = decoded(polled)
        if polled.status == 200 and got.get("access_token"):
            return str(got["access_token"])
        if got.get("error") not in ("authorization_pending", "slow_down"):
            raise failed(f"POST {client.token_url}", polled)
        time.sleep(1)
    raise IssuerError(f"{client.url} never issued the token it approved")


def claims(tok: str) -> dict[str, Any]:
    """A token's claims, not verified."""
    try:
        payload = tok.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return common.as_object(json.loads(base64.urlsafe_b64decode(padded))) or {}
    except (IndexError, ValueError):
        return {}


def scopes(tok: str) -> frozenset[str]:
    """A token's scopes."""
    return frozenset(str(claims(tok).get("scope", "")).split())


def approval_url(line: str) -> Optional[tuple[str, str]]:
    """The issuer and user code that a client's line of output, naming
    the issuer's verification page
    (<web URL>/view/issuer/device?namespace=...&user_code=...), asks the
    user to approve; None if the line names none."""
    for word in line.split():
        parts = urlsplit(word)
        query = parse_qs(parts.query)
        if parts.path.endswith("/view/issuer/device") and "user_code" in query:
            namespace = query.get("namespace", [""])[0]
            web_url = f"{parts.scheme}://{parts.netloc}"
            return url(web_url, namespace), query["user_code"][0]
    return None
