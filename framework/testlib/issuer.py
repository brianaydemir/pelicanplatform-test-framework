"""Pelican's embedded OAuth2 issuer, as a client of it: tokens for a
user through the device flow (RFC 8628) and the authorization code flow
(RFC 6749, with PKCE), approved on the user's behalf; and what a client
does with a token afterward: refresh it, exchange it (RFC 8693), revoke
it (RFC 7009), and introspect it (RFC 7662).

A client asks the issuer to start a flow. In the device flow, it polls
for the token while the user approves the flow at the issuer's
verification page; in the code flow, the user is sent back from the
issuer's authorization endpoint with a code, which the client trades
for the token. Here the tests play the user too: both endpoints take
the same credentials as the server's web API, a bearer token
(credentials.mint_user()) or the cookie of a web login
(accounts.login()), so the tests approve with one, as Pelican's own
integration tests do (oauth2/issuer/integration_test.go). The issuer
grants what the user is allowed, narrowed to what the client asked for
and may carry (handleDeviceVerifySubmit and handleAuthorize in
oauth2/issuer/handlers.go).

A client that registers itself is bound to the first user who approves
a flow for it, and the issuer allows an address five registrations,
then one a minute (oauth2/issuer/rate_limit.go). So the tests register
none: the server's admin creates the clients they need through the
issuer's admin API, and every user uses them.

An origin serves an issuer for each export that has one, at
<web URL>/api/v1.0/issuer/ns<prefix>, and a local issuer for its own
web API at <web URL>/api/v1.0/issuer/ns/pelican/local-issuer, whose
tokens name credentials.local_issuer() as their issuer.
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess  # nosec B404
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from . import common, web

ROUTE = "/api/v1.0/issuer/ns"
ADMIN_ROUTE = "/api/v1.0/issuer/admin/ns"
LOCAL_NAMESPACE = "/pelican/local-issuer"

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CODE_GRANT = "authorization_code"
REFRESH_GRANT = "refresh_token"
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # nosec B105

# The audience the issuer gives every token: the WLCG profile's "any".
ANY_AUDIENCE = "https://wlcg.cern.ch/jwt/v1/any"

# The scopes the issuer grants whoever asks, if the client may carry them
# (isStandardScope in oauth2/issuer/handlers.go).
STANDARD_SCOPES = frozenset({"openid", "offline_access", "wlcg", "profile", "email"})

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

# The client every issuer seeds for browsers, which has no secret and
# proves itself with PKCE (Issuer.PublicClientID).
PUBLIC_CLIENT = "pelican-public-client"

# Where a client of the tests' is sent back with a code. Nothing listens
# there: the tests read the code from the redirect itself.
REDIRECT_URI = "https://localhost/pelican-test-framework"

# How many times to ask for the token after approving, a second apart.
POLLS = 12

JSON = {"Content-Type": "application/json"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class IssuerError(Exception):
    """The issuer did not do as asked."""


@dataclass(frozen=True)
class User:
    """How the issuer knows the user who approves a flow, as the server's
    web API knows them: by a bearer token (credentials.mint_user()), or
    by the cookie of a web login (accounts.login()), which also carries
    the groups the user was in when they logged in."""

    token: Optional[str] = None
    cookie: Optional[str] = None

    @property
    def headers(self) -> dict[str, str]:
        """The header that identifies the user."""
        if self.token is not None:
            return {"Authorization": f"Bearer {self.token}"}
        if self.cookie is not None:
            return {"Cookie": f"login={self.cookie}"}
        return {}


@dataclass(frozen=True)
class Client:
    """A client of one issuer."""

    url: str  # where the issuer serves: <web URL>/api/v1.0/issuer/ns<namespace>
    client_id: str
    client_secret: str  # "" for a public client, which has none
    device_url: str  # its device authorization endpoint
    token_url: str  # its token endpoint
    authorize_url: str  # its authorization endpoint
    redirect_uri: str = ""  # where the code flow sends the user back

    @property
    def public(self) -> bool:
        """Whether the client has no secret."""
        return not self.client_secret

    @property
    def auth(self) -> dict[str, str]:
        """The header that authenticates the client; none if public."""
        if self.public:
            return {}
        pair = f"{self.client_id}:{self.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}


@dataclass(frozen=True)
class Tokens:
    """What the token endpoint issued."""

    access: str
    refresh: Optional[str]  # only with offline_access, from a client that may refresh
    expires_in: int  # the access token's lifetime, in seconds
    raw: dict[str, Any] = field(default_factory=dict[str, Any])


def url(web_url: str, namespace: str) -> str:
    """Where the server at web_url serves its issuer of namespace."""
    return f"{web_url}{ROUTE}{namespace}"


def decoded(answer: web.Response) -> dict[str, Any]:
    """The JSON object in answer's body, or {}."""
    try:
        return common.as_object(json.loads(answer.body)) or {}
    except ValueError:
        return {}


def error_of(answer: web.Response) -> str:
    """The OAuth2 error in answer, e.g. invalid_grant, or ""."""
    return str(decoded(answer).get("error", ""))


def failed(what: str, answer: web.Response) -> IssuerError:
    """An error saying how what went."""
    return IssuerError(
        f"{what}: {answer.describe()}: {answer.body[:200].decode(errors='replace')}"
    )


def discovery(issuer_url: str) -> dict[str, Any]:
    """An issuer's discovery document. Raises IssuerError if it serves
    none."""
    target = f"{issuer_url}/.well-known/openid-configuration"
    found = web.request("GET", target)
    document = decoded(found)
    if found.status != 200 or "token_endpoint" not in document:
        raise failed(f"GET {target}", found)
    return document


def client_of(
    web_url: str, namespace: str, client_id: str, client_secret: str, redirect_uri: str = ""
) -> Client:
    """A Client for an existing client of the server's issuer of
    namespace, with the issuer's endpoints looked up."""
    issuer_url = url(web_url, namespace)
    endpoints = discovery(issuer_url)
    return Client(
        issuer_url,
        client_id,
        client_secret,
        str(endpoints["device_authorization_endpoint"]),
        str(endpoints["token_endpoint"]),
        str(endpoints["authorization_endpoint"]),
        redirect_uri,
    )


def create_client(
    web_url: str,
    namespace: str,
    admin_token: str,
    grants: Sequence[str] = (DEVICE_GRANT, REFRESH_GRANT),
    allowed_scopes: Sequence[str] = CLIENT_SCOPES,
    redirect_uris: Sequence[str] = (),
    public: bool = False,
    name: str = "pelican-test-framework",
) -> Client:
    """Have the server at web_url, as its admin, create a client of its
    issuer of namespace (LOCAL_NAMESPACE for its local one), allowed the
    grants and allowed_scopes, and look up the issuer's endpoints."""
    target = f"{web_url}{ADMIN_ROUTE}{namespace}/clients"
    body: dict[str, Any] = {
        "client_name": name,
        "grant_types": list(grants),
        "scopes": list(allowed_scopes),
        "public": public,
    }
    if redirect_uris:
        body["redirect_uris"] = list(redirect_uris)
    made = web.request(
        "POST", target, token=admin_token, upload=json.dumps(body).encode(), headers=JSON
    )
    client = decoded(made)
    if made.status != 201 or not client.get("client_id"):
        raise failed(f"POST {target}", made)
    return client_of(
        web_url,
        namespace,
        str(client["client_id"]),
        str(client.get("client_secret") or ""),
        redirect_uris[0] if redirect_uris else "",
    )


def delete_client(
    web_url: str, namespace: str, admin_token: str, client_id: str
) -> Optional[str]:
    """Have the server's admin delete a client of its issuer of namespace;
    why that failed, or None."""
    target = f"{web_url}{ADMIN_ROUTE}{namespace}/clients/{client_id}"
    gone = web.request("DELETE", target, token=admin_token)
    return None if gone.ok else f"DELETE {target}: {gone.describe()}"


def post(client: Client, target: str, form: dict[str, str]) -> web.Response:
    """POST form to target as client: authenticated by its secret, or, if
    it has none, naming itself in the form."""
    body = dict(form)
    if client.public:
        body["client_id"] = client.client_id
    return web.request(
        "POST", target, upload=urlencode(body).encode(), headers={**FORM, **client.auth}
    )


def tokens_of(answer: web.Response, what: str) -> Tokens:
    """The tokens in the token endpoint's answer. Raises IssuerError if
    it issued none."""
    got = decoded(answer)
    if answer.status != 200 or not got.get("access_token"):
        raise failed(what, answer)
    refresh_token = got.get("refresh_token")
    return Tokens(
        str(got["access_token"]),
        str(refresh_token) if refresh_token else None,
        int(got.get("expires_in") or 0),
        got,
    )


# --------------------------------------------------------------------------
# The device flow.


def start_device(client: Client, wanted: Sequence[str]) -> web.Response:
    """Ask client's issuer to start a device flow for the scopes wanted."""
    return post(client, client.device_url, {"scope": " ".join(wanted)})


def started(answer: web.Response, client: Client) -> dict[str, Any]:
    """The flow that start_device() started. Raises IssuerError if the
    issuer started none."""
    flow = decoded(answer)
    if answer.status != 200 or not flow.get("device_code") or not flow.get("user_code"):
        raise failed(f"POST {client.device_url}", answer)
    return flow


def poll(client: Client, device_code: str) -> web.Response:
    """Ask client's issuer for the flow's token once."""
    return post(
        client, client.token_url, {"grant_type": DEVICE_GRANT, "device_code": device_code}
    )


def redeem(client: Client, device_code: str) -> Tokens:
    """The flow's tokens, polling (POLLS times, a second apart) while the
    issuer says to wait."""
    for _ in range(POLLS):
        polled = poll(client, device_code)
        if polled.status == 200:
            return tokens_of(polled, f"POST {client.token_url}")
        if error_of(polled) not in ("authorization_pending", "slow_down"):
            raise failed(f"POST {client.token_url}", polled)
        time.sleep(1)
    raise IssuerError(f"{client.url} never issued the token it approved")


def consent_page(issuer_url: str, user_code: str, user: User) -> web.Response:
    """The issuer's verification page for user_code, as user: what the
    user is asked to approve, and the CSRF cookie the approval needs."""
    return web.request(
        "GET", f"{issuer_url}/device?user_code={user_code}", headers=user.headers
    )


def decide(issuer_url: str, user_code: str, user: User, action: str) -> web.Response:
    """Approve or deny (action) the device flow with user_code at
    issuer_url, as user, sending back the CSRF cookie the verification
    page set."""
    page = f"{issuer_url}/device"
    shown = consent_page(issuer_url, user_code, user)
    csrf = shown.cookie("csrf_token")
    if shown.status != 200 or not csrf:
        raise failed(f"GET {page}", shown)
    body = {"user_code": user_code, "action": action, "csrf_token": csrf}
    cookies = f"csrf_token={csrf}"
    if user.cookie is not None:
        cookies += f"; login={user.cookie}"
    headers = {**JSON, **user.headers, "Cookie": cookies}
    return web.request("POST", page, upload=json.dumps(body).encode(), headers=headers)


def approve(issuer_url: str, user_code: str, user: User) -> None:
    """Approve the device flow with user_code at issuer_url, as user."""
    done = decide(issuer_url, user_code, user, "approve")
    if done.status != 200 or decoded(done).get("status") != "approved":
        raise failed(f"approving {user_code} at {issuer_url}/device", done)


def device_tokens(client: Client, user: User, wanted: Sequence[str]) -> Tokens:
    """Tokens from client's issuer for user through the device flow,
    asking for the scopes wanted, which the issuer narrows to what the
    user is allowed."""
    flow = started(start_device(client, wanted), client)
    approve(client.url, str(flow["user_code"]), user)
    return redeem(client, str(flow["device_code"]))


def token(client: Client, user: User, wanted: Sequence[str]) -> str:
    """The access token of device_tokens()."""
    return device_tokens(client, user, wanted).access


# --------------------------------------------------------------------------
# The authorization code flow, with PKCE (RFC 7636).


def pkce() -> tuple[str, str]:
    """A PKCE verifier, and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize(
    client: Client,
    user: User,
    wanted: Sequence[str],
    challenge: str,
    state: str,
    redirect_uri: Optional[str] = None,
) -> web.Response:
    """Ask client's issuer, as user, to authorize client for the scopes
    wanted: a redirect (303) back to the client's redirect URI (or
    redirect_uri) with a code, or an error."""
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri if redirect_uri is None else redirect_uri,
        "scope": " ".join(wanted),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return web.request(
        "GET", f"{client.authorize_url}?{urlencode(query)}", headers=user.headers
    )


def redirected(answer: web.Response) -> dict[str, str]:
    """The query of the redirect in answer, each parameter's first value;
    {} if it redirects nowhere."""
    if answer.status not in (302, 303):
        return {}
    query = parse_qs(urlsplit(answer.header("Location")).query)
    return {key: values[0] for key, values in query.items() if values}


def code_tokens(client: Client, code: str, verifier: str) -> web.Response:
    """Trade the code for tokens, proving the PKCE verifier."""
    form = {
        "grant_type": CODE_GRANT,
        "code": code,
        "redirect_uri": client.redirect_uri,
        "code_verifier": verifier,
    }
    return post(client, client.token_url, form)


def code_flow(client: Client, user: User, wanted: Sequence[str]) -> Tokens:
    """Tokens from client's issuer for user through the code flow, asking
    for the scopes wanted."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(12)
    sent = authorize(client, user, wanted, challenge, state)
    back = redirected(sent)
    if not back.get("code"):
        raise failed(f"GET {client.authorize_url}", sent)
    if back.get("state") != state:
        raise IssuerError(f"{client.url} sent back state {back.get('state')!r}, not {state!r}")
    return tokens_of(code_tokens(client, back["code"], verifier), f"POST {client.token_url}")


# --------------------------------------------------------------------------
# What a client does with a token.


def refresh(client: Client, refresh_token: str) -> web.Response:
    """Ask client's issuer for new tokens with the refresh token."""
    return post(
        client, client.token_url, {"grant_type": REFRESH_GRANT, "refresh_token": refresh_token}
    )


def exchange(
    client: Client, subject: str, wanted: Sequence[str] = (), audience: str = ""
) -> web.Response:
    """Ask client's issuer to exchange the access token subject for one of
    the scopes wanted (the subject's, if none), for audience if given."""
    form = {
        "grant_type": EXCHANGE_GRANT,
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN_TYPE,
    }
    if wanted:
        form["scope"] = " ".join(wanted)
    if audience:
        form["audience"] = audience
    return post(client, client.token_url, form)


def revoke(client: Client, tok: str, hint: str) -> web.Response:
    """Revoke tok, an access_token or refresh_token (hint), at client's
    issuer."""
    return post(client, f"{client.url}/revoke", {"token": tok, "token_type_hint": hint})


def introspect(client: Client, tok: str, hint: str = "") -> dict[str, Any]:
    """What client's issuer says of tok: active, and if so its scope,
    sub, and exp; {} if it would not say."""
    form = {"token": tok}
    if hint:
        form["token_type_hint"] = hint
    said = decoded(post(client, f"{client.url}/introspect", form))
    return said if "active" in said else {}


def userinfo(issuer_url: str, access: str) -> web.Response:
    """The issuer's userinfo endpoint, presenting access."""
    return web.request("GET", f"{issuer_url}/userinfo", token=access)


def claims(tok: str) -> dict[str, Any]:
    """A token's claims, not verified."""
    try:
        payload = tok.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return common.as_object(json.loads(base64.urlsafe_b64decode(padded))) or {}
    except (IndexError, ValueError):
        return {}


def header(tok: str) -> dict[str, Any]:
    """A token's header: its kid and alg."""
    try:
        head = tok.split(".")[0]
        padded = head + "=" * (-len(head) % 4)
        return common.as_object(json.loads(base64.urlsafe_b64decode(padded))) or {}
    except (IndexError, ValueError):
        return {}


def scopes(tok: str) -> frozenset[str]:
    """A token's scopes."""
    return frozenset(str(claims(tok).get("scope", "")).split())


def groups(tok: str) -> frozenset[str]:
    """A token's wlcg.groups: the groups that earned it something."""
    return frozenset(str(g) for g in (common.as_array(claims(tok).get("wlcg.groups")) or []))


# --------------------------------------------------------------------------
# The Pelican client, acquiring tokens from the issuer itself.


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


@dataclass
class ClientRun:
    """How a client that acquires its own tokens ran."""

    status: int
    stdout: str
    stderr: str
    approvals: int  # how many device flows it started, which the tests approved
    problems: list[str]  # what went wrong approving


def client_env(base: dict[str, str], store: str) -> dict[str, str]:
    """The environment for a client acting as one user, from base
    (session.env): it may acquire a token with no terminal, and keeps its
    credentials in store, apart from other users', since the issuer binds
    a client that registers itself to the first user who approves a flow
    for it."""
    env = dict(base)
    env["PELICAN_SKIP_TERMINAL_CHECK"] = "1"
    os.makedirs(store, exist_ok=True)
    env["PELICAN_CLIENT_CREDENTIALFILE"] = os.path.join(store, "client-credentials.pem")
    return env


def run_approving(
    pelican: str,
    args: Sequence[str],
    env: dict[str, str],
    cwd: str,
    approver: Callable[[str, str], None],
    timeout: float = 180.0,
) -> ClientRun:
    """Run `pelican <args>` (the binary at pelican) in env, approving each
    device flow it announces on its stderr with approver(issuer URL, user
    code), which raises IssuerError if it cannot. A run that outlasts
    timeout is killed."""
    with subprocess.Popen(  # nosec B603
        [pelican, *args],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("the client's output is not piped")
        stdout = proc.stdout
        out: list[str] = []
        collector = threading.Thread(target=lambda: out.append(stdout.read()))
        collector.start()
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        done = ClientRun(0, "", "", 0, [])
        err: list[str] = []
        for line in proc.stderr:
            err.append(line)
            flow = approval_url(line)
            if flow:
                done.approvals += 1
                try:
                    approver(flow[0], flow[1])
                except IssuerError as e:
                    done.problems.append(str(e))
        done.status = proc.wait()
        killer.cancel()
        collector.join()
        done.stdout, done.stderr = "".join(out), "".join(err)
        if done.status < 0:
            done.problems.append(f"the client was killed after {timeout:.0f}s")
        return done
