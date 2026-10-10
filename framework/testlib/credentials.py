"""The credentials that the authorization tests present, and what each
namespace should do with them.

The transfers suite presents them through the clients, and the auth
suite straight to each server, so both mean the same thing by each name.
Each credential is for one namespace (see NAMESPACES). Only `server`,
`jwks`, and `ns-jwks` should be allowed anything a token decides. Each
of the others differs from `server` in one column of the table below
(`ns-cross` from `wrong-iss`), so that each tests one thing.

A token doesn't always decide (see expected()):

- Reads of a PublicReads namespace (/public) are allowed whatever the
  token. The servers ignore it, and the clients don't even send one
  (client/handle_http.go in Pelican), although the director rejects any
  expired token it is shown, whatever the namespace (director.go).
- Writes to a namespace without Writes (/public) are refused whatever
  the token.

/public takes no writes, so it has no issuer at all (see has_issuer()),
and no keys of its own. The credentials that need a namespace's own key
or issuer, or another namespace's, are only for /protected-a and
/protected-b, each the other's other namespace (see applies()). Where
only one of them is exported, no export lists the other's own key, so
`ns-other` and `ns-cross` are skipped. With the origins' issuer off,
every namespace has one issuer URL, and no namespace has keys of its
own, so `wrong-iss`, `ns-jwks`, `ns-other`, and `ns-cross` are skipped;
so they are with the external issuer, whose JWKS also lacks `jwks`'s
key, so `jwks` is skipped too (see moot()).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Tuple

from . import common

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

# The operations the tests perform, and which of them write.
READS = ("get", "head")
WRITES = ("put", "delete")


@dataclass(frozen=True)
class Credential:
    name: str
    key: Optional[str]  # which key signs it (see key_name()); None: no token
    issuer: str         # the issuer it claims: the namespace's ("own"), or the other's
    scope_path: str     # relative to the namespace
    get_scopes: Tuple[str, ...]  # for a read
    put_scopes: Tuple[str, ...]  # for a write
    lifetime: int
    verdict: str        # what a server should do with it where the token decides
    description: str

    def scopes(self, op: str) -> Tuple[str, ...]:
        return self.put_scopes if op in WRITES else self.get_scopes

    @property
    def expired(self) -> bool:
        """Whether its token is meant to have expired before it is used."""
        return self.lifetime == EXPIRED


CREDENTIALS = (
    #          name          key         issuer   scope path     get  put  lifetime verdict
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

BY_NAME = {c.name: c for c in CREDENTIALS}


def decide(verdict: str, caps: AbstractSet[str], op: str, direct: bool = False) -> str:
    """What should become of op (see READS and WRITES) in a namespace
    with capabilities caps, presenting a token whose verdict is verdict
    where the token decides: ALLOW or DENY. direct is for a request sent
    straight to an origin, or through a client told to read from one,
    which a namespace without DirectReads refuses whatever the token."""
    if direct and "DirectReads" not in caps:
        return DENY
    if op in READS and "PublicReads" in caps:
        return ALLOW
    if op in WRITES and "Writes" not in caps:
        return DENY
    return verdict


def expected(cred: Credential, caps: AbstractSet[str], op: str, direct: bool = False) -> str:
    """decide() for cred."""
    return decide(cred.verdict, caps, op, direct)


def token_decides(caps: AbstractSet[str], op: str) -> bool:
    """Whether a token decides op's fate in a namespace with caps."""
    if op in READS:
        return "PublicReads" not in caps
    return "Writes" in caps


# The keys that stand for a namespace's own key and the other's (see
# key_name()).
NAMESPACE_KEYS = ("ns-own", "ns-other")


def has_issuer(caps: AbstractSet[str]) -> bool:
    """Whether the origins give a namespace with capabilities caps an
    issuer, and so keys of its own: only if it takes writes or needs a
    token to read (handleIssuersIfNeeded in server_utils/origin.go)."""
    return ("Reads" in caps and "PublicReads" not in caps) or "Writes" in caps


def exported(fed: common.Federation) -> List[str]:
    """NAMESPACES that the federation exports: not every shape exports
    every one (ORIGIN_NAMESPACES in fed.sh)."""
    return [ns for ns in NAMESPACES if ns in fed.exports]


def applies(cred: Credential, namespace: str) -> bool:
    """Whether the tests present cred in namespace. A namespace without
    an issuer of its own (/public) has no own key and no other namespace,
    and a token never decides there anyway."""
    return namespace in PROTECTED or (cred.key not in NAMESPACE_KEYS and cred.issuer == "own")


def namespace_issuers(fed: common.Federation) -> bool:
    """Whether each protected namespace has an issuer URL of its own. Not
    with the origins' issuer off (ORIGIN_ENABLE_ISSUER=false), when every
    namespace has the origin's."""
    return fed.issuer_of("protected-a") != fed.issuer_of("protected-b")


def moot(fed: common.Federation, cred: Credential) -> Optional[str]:
    """Why cred can test nothing in this federation, if it can't. With
    the origins' issuer off, or the external issuer in its place,
    `wrong-iss` would claim the right issuer, and no namespace has keys of
    its own. The external issuer's JWKS holds only its own keys, so a
    token that names it but is signed with a key listed only in
    Server.IssuerJwks (`jwks`) would only repeat `unknown`. Where only one
    protected namespace is exported (e.g. `-p origin-httpsv2`), no export
    lists the other's own key, so a token signed with it (`ns-other`,
    `ns-cross`) would only repeat `unknown`."""
    if fed.external_issuer:
        why = "every namespace trusts the external issuer"
    else:
        why = "the origins' issuer is off"
    if cred.issuer != "own" and not namespace_issuers(fed):
        return f"every namespace has one issuer ({why}), so none is wrong"
    if cred.key in NAMESPACE_KEYS and not namespace_issuers(fed):
        return f"{why}, so no namespace has keys of its own"
    if cred.key == "jwks" and fed.external_issuer:
        return ("the external issuer's JWKS lacks Server.IssuerJwks's key, so the token"
                " would only repeat `unknown`")
    if cred.key == "ns-other":
        for namespace in PROTECTED:
            if namespace not in fed.exports:
                return f"/{namespace} is not exported, so no export lists its own key"
    return None


def keys(fed: common.Federation) -> Dict[str, str]:
    """Where each signing key is, under framework/var.

      origin      the origins' issuer key
      jwks        test-keys/jwks.pem, whose public key the origins list
                  only in Server.IssuerJwks
      unknown     test-keys/unknown.pem, which no server knows
      ns-<ns>     test-keys/ns-<ns>.pem, whose public key the origins
                  list only in the IssuerJwks of the /<ns> export (only
                  for the protected namespaces)
    """
    found = {"origin": fed.origin_key, "jwks": "test-keys/jwks.pem",
             "unknown": "test-keys/unknown.pem"}
    for namespace in PROTECTED:
        found[f"ns-{namespace}"] = f"test-keys/ns-{namespace}.pem"
    for name, path in found.items():
        if name != "origin" and not os.path.isfile(path):
            common.die(f"framework/var/{path} is missing; run ./fed.sh keys init")
    return found


def key_name(key: str, namespace: str) -> str:
    """Which of keys() signs a credential's token for namespace, given its
    key column: `ns-own` is the namespace's own key, and `ns-other` the
    other namespace's."""
    if key == "ns-own":
        return f"ns-{namespace}"
    if key == "ns-other":
        return f"ns-{OTHER[namespace]}"
    return key


#---------------------------------------------------------------------------
# The public keys that the origins publish beside their own (see keys()),
# under framework/var, and what each JWKS they serve should hold.

SERVER_JWKS = "issuer-jwks/test.jwks"


def namespace_jwks(namespace: str) -> str:
    return f"issuer-jwks/ns-{namespace}.jwks"


def key_ids(document: bytes) -> FrozenSet[str]:
    """The kids in a JWKS document. Raises ValueError if it isn't one."""
    parsed = json.loads(document)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):
        raise ValueError("not a JWKS")
    return frozenset(k.get("kid", "") for k in parsed["keys"] if isinstance(k, dict))


def server_jwks_problems(have: AbstractSet[str], jwks: AbstractSet[str],
                         own: Mapping[str, AbstractSet[str]]) -> List[str]:
    """What is wrong with the server-wide JWKS, whose kids are have: it
    should hold Server.IssuerJwks's (jwks), and no namespace's own (own,
    by protected namespace)."""
    problems = []
    if not jwks <= have:
        problems.append("lacks Server.IssuerJwks's key")
    for namespace in PROTECTED:
        if own[namespace] & have:
            problems.append(f"holds /{namespace}'s own key")
    return problems


def namespace_jwks_problems(namespace: str, have: AbstractSet[str],
                            server: Optional[AbstractSet[str]],
                            own: Mapping[str, AbstractSet[str]]) -> List[str]:
    """What is wrong with protected namespace's JWKS, whose kids are
    have: it should hold the server-wide JWKS's (server; None if that was
    unreadable), its own (own, by protected namespace), and nothing
    else."""
    problems = []
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


def mint(fed: common.Federation, out: str, prefix: str, key: str, issuer: str,
         scope_path: str, lifetime: int, scopes: Tuple[str, ...],
         subject: str = SUBJECT) -> None:
    """Write a token for namespace prefix (e.g. /protected-a/) to file
    out."""
    command = [common.binary("pelican"), "token", "create", fed.url + prefix,
               *[f"--{scope}" for scope in scopes],
               "--subject", subject, "--issuer", issuer,
               "--scope-path", scope_path, "--lifetime", str(lifetime),
               "--private-key", key]
    with open(out, "w") as f:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=f,
                              stderr=subprocess.PIPE, text=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        common.die(f"could not create a token for {prefix} with framework/var/{key}")


def mint_server(fed: common.Federation, out: str, namespace: str,
                scopes: Tuple[str, ...] = RWM, lifetime: int = LONG) -> str:
    """Write a `server` token for namespace to file out, and return it:
    what the tests use for their own setup and checks."""
    mint(fed, out, f"/{namespace}/", fed.origin_key, fed.issuer_of(namespace), "/",
         lifetime, scopes)
    with open(out) as f:
        return f.read().strip()


def mint_credential(fed: common.Federation, cred: Credential, namespace: str, op: str,
                    out: str) -> Optional[float]:
    """Write cred's token for op in namespace to file out, and return when
    it expires (seconds since the epoch). `none` writes nothing and
    returns None.
    `token create` logs an error, but writes the token anyway, when the
    key does not match the issuer, which is the point of `unknown` and
    `jwks`, and when the issuer has no keys to fetch, as for /public,
    which has no issuer."""
    if cred.key is None:
        return None
    issuer_ns = namespace if cred.issuer == "own" else OTHER[namespace]
    mint(fed, out, f"/{namespace}/", keys(fed)[key_name(cred.key, namespace)],
         fed.issuer_of(issuer_ns), cred.scope_path, cred.lifetime, cred.scopes(op))
    # It expires within `lifetime` of `token create` returning.
    return time.time() + cred.lifetime


def wait_until_stale(expiry: Optional[float]) -> None:
    """Sleep until 70s past expiry, if there is one. Native servers allow
    60s of clock skew, and remember a token they accepted, so no server
    may see an expired token any sooner."""
    if expiry is None:
        return
    left = int(expiry + 70 - time.time()) + 1
    if left > 0:
        print(f"Waiting {left}s until the expired token is 70s past its expiry ...")
        time.sleep(left)


def client_env(overwrites: bool = True) -> Dict[str, str]:
    """The environment for a client that should present only the token
    it is given. The client looks for tokens at --token, then in these
    variables and token files, then in _CONDOR_CREDS, and presents the
    first acceptable one, or the first it found if none is
    (client/acquire_token.go in Pelican). Most credentials' tokens are
    unacceptable on purpose, so any other token would win over them: drop
    the variables, and refuse to run with a token file. With no token,
    `pelican object` also looks in its credential store, which in the dev
    container outlives a run: give it an empty one. It then gives up
    unless its stdout is a terminal, or <PREFIX>_SKIP_TERMINAL_CHECK says
    to pretend: drop that too. Preferred caches would win over a direct
    read: drop them, however they are given. With overwrites, uploads may
    replace objects, since a pstore can't be emptied; `object sync` wants
    them off, since they turn off its skipping of uploads. Runs in
    framework/var."""
    env = dict(os.environ)
    for name in ("BEARER_TOKEN", "BEARER_TOKEN_FILE", "TOKEN", "_CONDOR_CREDS",
                 "PELICAN_CLIENT_PREFERREDCACHES", "NEAREST_CACHE", "_CONDOR_JOB_AD"):
        env.pop(name, None)
    for name in [n for n in env if n.endswith(("_SKIP_TERMINAL_CHECK", "_NEAREST_CACHE"))]:
        env.pop(name)
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")
    for path in (f"{runtime}/bt_u{uid}", f"/tmp/bt_u{uid}"):
        if os.path.exists(path):
            common.die(f"the client would present {path} instead of each scenario's token;"
                       " remove it")
    shutil.rmtree(".client-credentials", ignore_errors=True)
    env["PELICAN_CLIENT_CREDENTIALFILE"] = os.path.abspath(
        ".client-credentials/client-credentials.pem")
    if overwrites:
        env["PELICAN_CLIENT_ENABLEOVERWRITES"] = "true"
    else:
        env.pop("PELICAN_CLIENT_ENABLEOVERWRITES", None)
    return env
