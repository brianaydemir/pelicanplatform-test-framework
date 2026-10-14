"""The credentials that the authorization tests present.

It also says what each namespace should do with them. The transfers
suite presents them through the clients, and the auth suite straight to
each server, so both mean the same thing by each name. Each credential
is for one namespace (see NAMESPACES). Only `server`, `jwks`, and
`ns-jwks` should be allowed anything a token decides. Each of the others
differs from `server` in one column of the table below (`ns-cross` from
`wrong-iss`), so that each tests one thing.

A token doesn't always decide (see expected()):

- Reads of a PublicReads namespace (/public) are allowed whatever the
  token. The servers ignore it, and the clients don't even send one
  (client/handle_http.go in Pelican), although the director rejects any
  expired token it is shown, whatever the namespace (director.go).
- Writes to a namespace without Writes (/public) are refused whatever
  the token.

/public takes no writes, so it has no issuer at all (see has_issuer()),
unless `-p origin-public-writes` gives it writes, and so an issuer,
which a write there must then name; either way it has no keys of its
own. The credentials that need a namespace's own key or issuer, or
another namespace's, are only for /protected-a and /protected-b, each
the other's other namespace (see applies()). Where only one of them is
exported, no export lists the other's own key, so `ns-other` and
`ns-cross` are skipped. With the origins' issuer off, every namespace
has one issuer URL, and no namespace has keys of its own, so
`wrong-iss`, `ns-jwks`, `ns-other`, and `ns-cross` are skipped; so they
are with the external issuer, whose JWKS also lacks `jwks`'s key, so
`jwks` is skipped too (see moot()).
"""

import json
import os
import shutil
import subprocess  # nosec B404
import sys
import time
import uuid
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from . import common, jwt

ALLOW = "allow"
DENY = "deny"

NAMESPACES = ("public", "protected-a", "protected-b")
# The namespaces with issuers, and keys, of their own; each is the other's
# other namespace.
PROTECTED = ("protected-a", "protected-b")
OTHER = {"protected-a": "protected-b", "protected-b": "protected-a"}

# Token lifetimes, in seconds. A long one outlasts any run.
LONG = 14400
EXPIRED = 1

# Scopes, as `pelican token create` flags.
RWM = ("read", "write", "modify")
WM = ("write", "modify")
R = ("read",)
# The WLCG scope that each flag grants.
WLCG_SCOPES = {"read": "storage.read", "write": "storage.create", "modify": "storage.modify"}

# The operations the tests perform, and which of them write.
READS = ("get", "head")
WRITES = ("put", "delete")


@dataclass(frozen=True)
class Credential:
    """A credential that the tests present, and what it should get.

    Attributes:
        key: Which key signs it (see key_name()); None: no token.
        issuer: The issuer it claims: the namespace's ("own"), or the
            other's.
        scope_path: Its scope path, relative to the namespace.
        get_scopes: Its scopes for a read.
        put_scopes: Its scopes for a write.
        lifetime: Its token's lifetime, in seconds.
        verdict: What a server should do with it where the token decides.
    """

    name: str
    key: Optional[str]
    issuer: str
    scope_path: str
    get_scopes: tuple[str, ...]
    put_scopes: tuple[str, ...]
    lifetime: int
    verdict: str
    description: str

    def scopes(self, op: str) -> tuple[str, ...]:
        """Return its scopes for op."""
        return self.put_scopes if op in WRITES else self.get_scopes

    @property
    def expired(self) -> bool:
        """Whether its token is meant to have expired before it is used."""
        return self.lifetime == EXPIRED


# fmt: off
CREDENTIALS = (
    #          name          key         issuer   scope path     get  put
    # lifetime verdict
    Credential("server",     "origin",   "own",   "/",           RWM, RWM, LONG,    ALLOW,
               "signed by the origins' issuer key"),
    Credential("jwks",       "jwks",     "own",   "/",           RWM, RWM, LONG,    ALLOW,
               "signed by a key listed only in Server.IssuerJwks"),
    Credential("ns-jwks",    "ns-own",   "own",   "/",           RWM, RWM, LONG,    ALLOW,
               "signed by a key listed only in the namespace's own IssuerJwks"),
    Credential("none",       None,       "own",   "/",           RWM, RWM, LONG,    DENY,
               "no token"),
    Credential("unknown",    "unknown",  "own",   "/",           RWM, RWM, LONG,    DENY,
               "signed by a key no server knows"),
    Credential("ns-other",   "ns-other", "own",   "/",           RWM, RWM, LONG,    DENY,
               "signed by a key listed only in the other namespace's IssuerJwks"),
    Credential("wrong-op",   "origin",   "own",   "/",           WM,  R,   LONG,    DENY,
               "the origins' key, scoped for the other operation"),
    Credential("wrong-path", "origin",   "own",   "/elsewhere/", RWM, RWM, LONG,    DENY,
               "the origins' key, scoped to /elsewhere/"),
    Credential("wrong-iss",  "origin",   "other", "/",           RWM, RWM, LONG,    DENY,
               "the origins' key, but the other namespace's issuer"),
    Credential("ns-cross",   "ns-other", "other", "/",           RWM, RWM, LONG,    DENY,
               "the other namespace's own key and issuer: a token only it accepts"),
    Credential("expired",    "origin",   "own",   "/",           RWM, RWM, EXPIRED, DENY,
               "the origins' key, expired"),
)
# fmt: on

BY_NAME = {c.name: c for c in CREDENTIALS}


def decide(verdict: str, caps: AbstractSet[str], op: str, direct: bool = False) -> str:
    """Return what should become of op in a namespace: ALLOW or DENY.

    Args:
        verdict: The verdict of the token presented, where the token
            decides.
        caps: The namespace's capabilities.
        op: The operation (see READS and WRITES).
        direct: Whether the request is sent straight to an origin, or
            through a client told to read from one, which a namespace
            without DirectReads refuses whatever the token.
    """
    if direct and "DirectReads" not in caps:
        return DENY
    if op in READS and "PublicReads" in caps:
        return ALLOW
    if op in WRITES and "Writes" not in caps:
        return DENY
    return verdict


def expected(cred: Credential, caps: AbstractSet[str], op: str, direct: bool = False) -> str:
    """Return decide() for cred."""
    return decide(cred.verdict, caps, op, direct)


def token_decides(caps: AbstractSet[str], op: str) -> bool:
    """Return whether a token decides op's fate in a namespace with caps."""
    if op in READS:
        return "PublicReads" not in caps
    return "Writes" in caps


# The keys that stand for a namespace's own key and the other's (see
# key_name()).
NAMESPACE_KEYS = ("ns-own", "ns-other")


def has_issuer(caps: AbstractSet[str]) -> bool:
    """Return whether the origins give a namespace with caps an issuer.

    With an issuer come keys of its own. Only a namespace that takes
    writes or needs a token to read gets one (handleIssuersIfNeeded in
    server_utils/origin.go).
    """
    return ("Reads" in caps and "PublicReads" not in caps) or "Writes" in caps


def exported(fed: common.Federation) -> list[str]:
    """Return the NAMESPACES that the federation exports.

    Not every shape exports every one (ORIGIN_NAMESPACES in fed.sh).
    """
    return [ns for ns in NAMESPACES if ns in fed.exports]


def probe_namespace(fed: common.Federation) -> str:
    """Return a namespace to probe the federation through, if any will do.

    That is the first exported protected namespace, or, where the origins
    export only /public (`-p origin-public`), /public.
    """
    return next((ns for ns in PROTECTED if ns in fed.exports), exported(fed)[0])


def applies(cred: Credential, namespace: str) -> bool:
    """Return whether the tests present cred in namespace.

    /public has no key of its own and no other namespace, so it gets only
    the credentials that need neither (a token decides nothing there,
    unless it takes writes, which only its issuer's tokens may:
    `-p origin-public-writes`).
    """
    return namespace in PROTECTED or (cred.key not in NAMESPACE_KEYS and cred.issuer == "own")


def namespace_issuers(fed: common.Federation) -> bool:
    """Return whether each protected namespace has its own issuer URL.

    Not with the origins' issuer off (ORIGIN_ENABLE_ISSUER=false), when
    every namespace has the origin's.
    """
    return fed.issuer_of("protected-a") != fed.issuer_of("protected-b")


def moot(fed: common.Federation, cred: Credential) -> Optional[str]:
    """Return why cred can test nothing in fed, or None if it can.

    With the origins' issuer off, or the external issuer in its place,
    `wrong-iss` would claim the right issuer, and no namespace has keys of
    its own. The external issuer's JWKS holds only its own keys, so a
    token that names it but is signed with a key listed only in
    Server.IssuerJwks (`jwks`) would only repeat `unknown`. Where only one
    protected namespace is exported (e.g. `-p origin-httpsv2`), no export
    lists the other's own key, so a token signed with it (`ns-other`,
    `ns-cross`) would only repeat `unknown`.
    """
    if fed.external_issuer:
        why = "every namespace trusts the external issuer"
    else:
        why = "the origins' issuer is off"
    if cred.issuer != "own" and not namespace_issuers(fed):
        return f"every namespace has one issuer ({why}), so none is wrong"
    if cred.key in NAMESPACE_KEYS and not namespace_issuers(fed):
        return f"{why}, so no namespace has keys of its own"
    if cred.key == "jwks" and fed.external_issuer:
        return (
            "the external issuer's JWKS lacks Server.IssuerJwks's key, so the token"
            " would only repeat `unknown`"
        )
    if cred.key == "ns-other":
        for namespace in PROTECTED:
            if namespace not in fed.exports:
                return f"/{namespace} is not exported, so no export lists its own key"
    return None


def keys(fed: common.Federation) -> dict[str, str]:
    """Return where each signing key is, under framework/var.

    origin      the origins' issuer key
    jwks        test-keys/jwks.pem, whose public key the origins list
                only in Server.IssuerJwks
    unknown     test-keys/unknown.pem, which no server knows
    ns-<ns>     test-keys/ns-<ns>.pem, whose public key the origins
                list only in the IssuerJwks of the /<ns> export (only
                for the protected namespaces)
    """
    found = {
        "origin": fed.origin_key,
        "jwks": "test-keys/jwks.pem",
        "unknown": "test-keys/unknown.pem",
    }
    for namespace in PROTECTED:
        found[f"ns-{namespace}"] = f"test-keys/ns-{namespace}.pem"
    for name, path in found.items():
        if name != "origin" and not os.path.isfile(path):
            common.die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    return found


def key_name(key: str, namespace: str) -> str:
    """Return which of keys() signs a credential's token for namespace.

    key is the credential's key column: `ns-own` is the namespace's own
    key, and `ns-other` the other namespace's.
    """
    if key == "ns-own":
        return f"ns-{namespace}"
    if key == "ns-other":
        return f"ns-{OTHER[namespace]}"
    return key


# --------------------------------------------------------------------------
# The public keys that the origins publish beside their own (see keys()),
# under framework/var, and what each JWKS they serve should hold.

SERVER_JWKS = "issuer-jwks/test.jwks"


def namespace_jwks(namespace: str) -> str:
    """Return where namespace's own public keys are, under framework/var."""
    return f"issuer-jwks/ns-{namespace}.jwks"


def key_ids(document: bytes) -> frozenset[str]:
    """Return the kids in a JWKS document.

    Raises:
        ValueError: The document is not a JWKS.
    """
    parsed = common.as_object(json.loads(document)) or {}
    listed = common.as_array(parsed.get("keys"))
    if listed is None:
        raise ValueError("not a JWKS")
    found = [common.as_object(k) for k in listed]
    return frozenset(str(k.get("kid", "")) for k in found if k is not None)


def server_jwks_problems(
    have: AbstractSet[str], jwks: AbstractSet[str], own: Mapping[str, AbstractSet[str]]
) -> list[str]:
    """Return what is wrong with the server-wide JWKS.

    It should hold Server.IssuerJwks's kids, and no namespace's own.

    Args:
        have: The JWKS's kids.
        jwks: Server.IssuerJwks's kids.
        own: Each protected namespace's own kids, by namespace.
    """
    problems: list[str] = []
    if not jwks <= have:
        problems.append("lacks Server.IssuerJwks's key")
    for namespace in PROTECTED:
        if own[namespace] & have:
            problems.append(f"holds /{namespace}'s own key")
    return problems


def public_jwks_problems(
    have: AbstractSet[str],
    server: Optional[AbstractSet[str]],
    own: Mapping[str, AbstractSet[str]],
) -> list[str]:
    """Return what is wrong with /public's JWKS, where it has an issuer.

    /public has one under `-p origin-public-writes`. Its JWKS should hold
    the server-wide JWKS's kids, and no protected namespace's own, with
    which a token for that namespace alone would otherwise write /public.

    Args:
        have: The JWKS's kids.
        server: The server-wide JWKS's kids; None if that was unreadable.
        own: Each protected namespace's own kids, by namespace.
    """
    problems: list[str] = []
    for namespace in PROTECTED:
        if own[namespace] & have:
            problems.append(f"holds /{namespace}'s own key")
    if server is None:
        problems.append("could not compare it with the server-wide JWKS")
        return problems
    missing = server - have
    if missing:
        problems.append(f"lacks the server-wide keys {', '.join(sorted(missing))}")
    extra = have - server - set().union(*own.values())
    if extra:
        problems.append(f"holds other keys {', '.join(sorted(extra))}")
    return problems


def namespace_jwks_problems(
    namespace: str,
    have: AbstractSet[str],
    server: Optional[AbstractSet[str]],
    own: Mapping[str, AbstractSet[str]],
) -> list[str]:
    """Return what is wrong with protected namespace's JWKS.

    It should hold the server-wide JWKS's kids, its own, and nothing else.

    Args:
        namespace: A protected namespace.
        have: The JWKS's kids.
        server: The server-wide JWKS's kids; None if that was unreadable.
        own: Each protected namespace's own kids, by namespace.
    """
    problems: list[str] = []
    if not own[namespace] <= have:
        problems.append(f"lacks /{namespace}'s own key")
    if own[OTHER[namespace]] & have:
        problems.append(f"holds /{OTHER[namespace]}'s own key")
    if server is None:
        problems.append("could not compare it with the server-wide JWKS")
        return problems
    missing = server - have
    if missing:
        problems.append(f"lacks the server-wide keys {', '.join(sorted(missing))}")
    extra = have - server - own[namespace] - own[OTHER[namespace]]
    if extra:
        problems.append(f"holds other keys {', '.join(sorted(extra))}")
    return problems


# The subject of the tests' tokens, which fed.sh maps to the `pelican`
# user under `origin-multiuser`.
SUBJECT = "pelican-test-framework"


def mint(
    fed: common.Federation,
    out: str,
    prefix: str,
    key: str,
    issuer: str,
    scope_path: str,
    lifetime: int,
    scopes: tuple[str, ...],
    subject: str = SUBJECT,
    raw_scopes: tuple[str, ...] = (),
    audience: str = "",
    profile: str = "",
) -> None:
    """Write a token for namespace prefix to file out.

    Args:
        fed: The federation.
        out: The file to write it to.
        prefix: The namespace prefix (e.g. /protected-a/).
        key: The file of the private key that signs it.
        issuer: The issuer it claims.
        scope_path: Its scope path, relative to the namespace.
        lifetime: Its lifetime, in seconds.
        scopes: Its scopes, as `pelican token create` flags (e.g. read).
        subject: Its subject.
        raw_scopes: Its scopes as such (e.g. pelican.transfer).
        audience: Its audience; the profile's "any" unless this names
            one.
        profile: Its profile; the WLCG token profile unless this names
            another (`scitokens2`, whose flags grant `read:` and `write:`
            scopes, and whose "any" is `ANY`).
    """
    command = [common.binary("pelican"), "token", "create", fed.url + prefix]
    command += [f"--{scope}" for scope in scopes]
    for scope in raw_scopes:
        command += ["--raw-scope", scope]
    command += ["--subject", subject, "--issuer", issuer, "--scope-path", scope_path]
    command += ["--lifetime", str(lifetime), "--private-key", key]
    if audience:
        command += ["--audience", audience]
    if profile:
        command += ["--profile", profile]
    with open(out, "w", encoding="utf-8") as f:
        done = subprocess.run(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        common.die(f"could not create a token for {prefix} with framework/var/{key}")


def mint_server(
    fed: common.Federation,
    out: str,
    namespace: str,
    scopes: tuple[str, ...] = RWM,
    scope_path: str = "/",
    subject: str = SUBJECT,
    audience: str = "",
    profile: str = "",
) -> str:
    """Write a `server` token for namespace to file out, and return it.

    It is what the tests use for their own setup and checks. A token for
    less of the namespace (scope_path), for another subject or audience,
    or of another profile is the origins' too.
    """
    issuer = fed.issuer_of(namespace)
    mint(
        fed,
        out,
        f"/{namespace}/",
        fed.origin_key,
        issuer,
        scope_path,
        LONG,
        scopes,
        subject,
        audience=audience,
        profile=profile,
    )
    with open(out, encoding="utf-8") as f:
        return f.read().strip()


def server_claims(
    fed: common.Federation,
    namespace: str,
    scopes: tuple[str, ...] = RWM,
    scope_path: str = "/",
    lifetime: int = LONG,
    subject: str = SUBJECT,
    audience: str = jwt.ANY_AUDIENCE,
) -> dict[str, Any]:
    """Return the claims of a `server` token for namespace.

    They are as `pelican token create` writes them (see mint_server()),
    for jwt.encode() to sign with the origins' key (fed.origin_key),
    after changing them as a test needs: issued now, and lasting
    lifetime seconds.
    """
    now = int(time.time())
    return {
        "aud": [audience],
        "exp": now + lifetime,
        "iat": now,
        "iss": fed.issuer_of(namespace),
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "scope": " ".join(f"{WLCG_SCOPES[s]}:{scope_path}" for s in scopes),
        "sub": subject,
        "wlcg.ver": "1.0",
    }


def mint_credential(
    fed: common.Federation, cred: Credential, namespace: str, op: str, out: str
) -> Optional[float]:
    """Write cred's token for op in namespace to file out.

    `token create` logs an error, but writes the token anyway, when the
    key does not match the issuer, which is the point of `unknown` and
    `jwks`, and when the issuer has no keys to fetch, as for /public,
    which has no issuer.

    Returns:
        When the token expires, in seconds since the epoch; None for
        `none`, which writes nothing.
    """
    if cred.key is None:
        return None
    issuer_ns = namespace if cred.issuer == "own" else OTHER[namespace]
    mint(
        fed,
        out,
        f"/{namespace}/",
        keys(fed)[key_name(cred.key, namespace)],
        fed.issuer_of(issuer_ns),
        cred.scope_path,
        cred.lifetime,
        cred.scopes(op),
    )
    # It expires within `lifetime` of `token create` returning.
    return time.time() + cred.lifetime


# The scope of a server's web API, which every user's token carries.
WEB_SCOPE = "web_ui.access"


def local_issuer(fed: common.Federation, web_url: str) -> str:
    """Return the local issuer of the server at web_url.

    That is the issuer it names in the tokens it mints for its own web
    API (GetLocalIssuerUrl in Pelican's config/config.go): its web URL,
    or <web URL>/api/v1.0/origin where an origin and a director share a
    process (`tiny`); and for a director behind the load balancer
    (`-p director-ha`), the shared name that is its
    Server.ExternalWebUrl.
    """
    if fed.director_ha and web_url in fed.directors:
        return fed.director_url
    return f"{web_url}/api/v1.0/origin" if fed.tiny else web_url


def mint_user(
    fed: common.Federation,
    out: str,
    issuer: str,
    key: str,
    username: str,
    lifetime: int = 300,
    extra_scopes: tuple[str, ...] = (),
) -> str:
    """Write and return a token that a server's web API takes as username's.

    It has scope WEB_SCOPE, and extra_scopes, and lasts lifetime seconds.
    It is from the server's local issuer (see local_issuer()) and signed
    with its own key, as `pelican cache introspect` mints the admin's
    (fetchOrGenerateWebAPIAdminToken in Pelican's cmd/cmd_utils.go). The
    server resolves the subject to its record of that user
    (extractUserFromBearerToken in web_ui/authentication.go); the
    built-in `admin` always has one.
    """
    raw_scopes = (WEB_SCOPE, *extra_scopes)
    mint(fed, out, "/", key, issuer, "/", lifetime, (), username, raw_scopes=raw_scopes)
    with open(out, encoding="utf-8") as f:
        return f.read().strip()


def mint_admin(fed: common.Federation, out: str, issuer: str, key: str) -> str:
    """Write and return the admin's token for a server (see mint_user())."""
    return mint_user(fed, out, issuer, key, "admin")


# The scopes of a server's metrics (/metrics) and of its Prometheus query
# API (/api/v1.0/prometheus/), which Monitoring.MetricAuthorization and
# PromQLAuthorization, on by default, require.
SCRAPE_SCOPE = "monitoring.scrape"
QUERY_SCOPE = "monitoring.query"


def key_dir_of(fed: common.Federation, web_url: str) -> str:
    """Return the directory of the keys of the server at web_url.

    It is under framework/var: another owner's own; the directors' load
    balancer's (`-p director-ha`), director-0's, which every director
    shares; or issuer-keys/<its host>, where fed.sh puts every other
    server's (`fed` in `tiny`).
    """
    for owner in fed.owners:
        if owner.web_url == web_url:
            return owner.key_dir
    host = urlsplit(web_url).hostname
    if fed.director_ha and host == urlsplit(fed.director_url).hostname:
        return "issuer-keys/director-0"
    return f"issuer-keys/{host}"


def web_token(
    fed: common.Federation,
    web_url: str,
    subject: str,
    scopes: tuple[str, ...],
    lifetime: int = 300,
    key: Optional[str] = None,
) -> str:
    """Return a token from the local issuer of the server at web_url.

    local_issuer() names the issuer. The token is for subject, with
    scopes (as such, e.g. SCRAPE_SCOPE), lasting lifetime seconds, and
    jwt.encode() signs it with the server's own key, or with key: like
    the token a server mints to scrape its own /metrics
    (createPromMetricToken in Pelican's web_ui/authorization.go).
    """
    key = key or common.signing_key(key_dir_of(fed, web_url))
    issuer = local_issuer(fed, web_url)
    now = int(time.time())
    claims = {
        "aud": [issuer],
        "exp": now + lifetime,
        "iat": now,
        "iss": issuer,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "scope": " ".join(scopes),
        "sub": subject,
        "wlcg.ver": "1.0",
    }
    return jwt.encode(jwt.header(jwt.algorithm(key), jwt.key_id(key)), claims, key=key)


def wait_until_stale(expiry: Optional[float]) -> None:
    """Sleep until 70s past expiry, if there is one.

    Native servers allow 60s of clock skew, and remember a token they
    accepted, so no server may see an expired token any sooner.
    """
    if expiry is None:
        return
    left = int(expiry + 70 - time.time()) + 1
    if left > 0:
        print(f"Waiting {left}s until the expired token is 70s past its expiry ...")
        time.sleep(left)


def client_env(overwrites: bool = True) -> dict[str, str]:
    """Return the environment for a client.

    The client should present only the token it is given. It looks for
    tokens at --token, then in these variables and token files, then in
    _CONDOR_CREDS, and presents the first acceptable one, or the first it
    found if none is (client/acquire_token.go in Pelican). Most
    credentials' tokens are unacceptable on purpose, so any other token
    would win over them: drop the variables, and refuse to run with a
    token file. With no token, `pelican object` also looks in its
    credential store, which in the dev container outlives a run: give it
    an empty one. It then gives up unless its stdout is a terminal, or
    <PREFIX>_SKIP_TERMINAL_CHECK says to pretend: drop that too.
    Preferred caches would win over a direct read: drop them, however
    they are given. With overwrites, uploads may replace objects, since a
    pstore can't be emptied; `object sync` wants them off, since they
    turn off its skipping of uploads. Runs in framework/var.
    """
    env = dict(os.environ)
    for name in (
        "BEARER_TOKEN",
        "BEARER_TOKEN_FILE",
        "TOKEN",
        "_CONDOR_CREDS",
        "PELICAN_CLIENT_PREFERREDCACHES",
        "NEAREST_CACHE",
        "_CONDOR_JOB_AD",
    ):
        env.pop(name, None)
    for name in [n for n in env if n.endswith(("_SKIP_TERMINAL_CHECK", "_NEAREST_CACHE"))]:
        env.pop(name)
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")
    for path in (f"{runtime}/bt_u{uid}", f"/tmp/bt_u{uid}"):  # nosec B108
        if os.path.exists(path):
            common.die(
                f"the client would present {path} instead of each scenario's token;"
                + " remove it"
            )
    shutil.rmtree(".client-credentials", ignore_errors=True)
    env["PELICAN_CLIENT_CREDENTIALFILE"] = os.path.abspath(
        ".client-credentials/client-credentials.pem"
    )
    if overwrites:
        env["PELICAN_CLIENT_ENABLEOVERWRITES"] = "true"
    else:
        env.pop("PELICAN_CLIENT_ENABLEOVERWRITES", None)
    return env
