"""Advertisements, as an origin or a cache posts them to a director.

An ad is a server_structs.OriginAdvertise (registerServerAd in Pelican's
director/director.go). The token that signs one is signed with the server's
own key, which the registry holds for its prefix (/origins/<host> or
/caches/<host>), and has the advertise scope; the director checks it against
the registry's copy of the key (verifyAdvertiseToken in
director/origin_api.go).

Servers finds each server as the first director describes it, and the key in
framework/var/issuer-keys/<host>/ that the registry holds for it; ad()
builds an ad like the server's own from that description, which lasts
AD_LIFETIME seconds, and the server's next ad, within
Server.AdvertisementInterval, replaces; and post_ad() posts an ad as a
server does.
"""

import glob
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from . import common, credentials, director, jwt, web

# The scope of an advertise token.
ADVERTISE = "pelican.advertise"
# A key that no server knows.
UNKNOWN_KEY = "test-keys/unknown.pem"
# The directors' list of servers.
SERVERS = "/api/v1.0/director_ui/servers"
# How long an ad lasts, in seconds, and how long a token signed here
# lasts: a server's last a minute, but a check may wait out a restore
# between minting one and its last use.
AD_LIFETIME = 90
TOKEN_LIFETIME = 300


class Missing(Exception):
    """The absence of something needed: a server, or its key."""


@dataclass(frozen=True)
class Server:
    """An origin or a cache, as a director describes it.

    Attributes:
        kind: "Origin" or "Cache".
        host: Its web URL's host: its service, whose keys are its own.
        detail: What /servers/<name> says of it.
    """

    kind: str
    host: str
    detail: Mapping[str, Any]

    @property
    def name(self) -> str:
        """Its name in its ads."""
        return str(self.detail.get("name", ""))

    @property
    def prefix(self) -> str:
        """Its registry prefix, e.g. /caches/cache-0."""
        return str(self.detail.get("registryPrefix", ""))

    @property
    def registered_host(self) -> str:
        """The host its registry prefix names."""
        return self.prefix.rsplit("/", 1)[-1]


class Servers:
    """The servers as the first director describes them.

    It also finds their keys, and makes tokens like their own.

    Attributes:
        director: The first director's URL.
    """

    def __init__(self, fed: common.Federation):
        self.fed = fed
        self.director = fed.directors[0]
        self._servers: dict[tuple[str, str], Server] = {}
        self._keys: dict[str, tuple[str, str]] = {}

    def server(self, kind: str, host: str) -> Server:
        """Return the server of kind whose web URL is at host.

        Args:
            kind: "Origin" or "Cache".
            host: The host of the server's web URL.

        Returns:
            The server, as the first director lists it.

        Raises:
            Missing: The first director does not list it.
        """
        if (kind, host) in self._servers:
            return self._servers[(kind, host)]
        listed = web.request("GET", f"{self.director}{SERVERS}")
        rows = common.as_array(listed.json()) or []
        for row in (common.as_object(r) or {} for r in rows):
            if row.get("type") == kind and director.host(str(row.get("webUrl", ""))) == host:
                name = str(row.get("name", ""))
                answer = web.request("GET", f"{self.director}{SERVERS}/{quote(name, safe='')}")
                found = common.as_object(answer.json())
                if found is None:
                    raise Missing(f"{SERVERS}/{name}: {answer.describe()}, not a server")
                self._servers[(kind, host)] = Server(kind, host, found)
                return self._servers[(kind, host)]
        raise Missing(f"{director.host(self.director)} lists no {kind.lower()} at {host}")

    def key(self, server: Server) -> tuple[str, str]:
        """Return server's own key, and its kid.

        Its own key is the one of its keys whose public key the registry
        holds for its prefix.

        Returns:
            The key (a PEM file under framework/var), and its kid.

        Raises:
            Missing: The registry holds none of its keys for its prefix.
        """
        if server.prefix not in self._keys:
            url = f"{self.fed.registry_url}/api/v1.0/registry{server.prefix}/.well-known/issuer.jwks"
            answer = web.request("GET", url)
            listed = common.as_array((common.as_object(answer.json()) or {}).get("keys")) or []
            for key in sorted(glob.glob(f"issuer-keys/{server.host}/*.pem")):
                jwk = jwt.public_jwk(key)
                for entry in (common.as_object(e) or {} for e in listed):
                    if all(entry.get(k) == v for k, v in jwk.items()):
                        self._keys[server.prefix] = (key, str(entry.get("kid") or ""))
            if server.prefix not in self._keys:
                raise Missing(
                    f"the registry holds none of framework/var/issuer-keys/{server.host}/'s"
                    + f" keys for {server.prefix} ({url}: {answer.describe()})"
                )
        return self._keys[server.prefix]

    def advertise_token(self, server: Server, scope: str = ADVERTISE) -> str:
        """Return a token like server's own advertise token, of scope.

        Like server's own (GetAdvertisementTok in Pelican's
        server_utils/server_utils.go), it is for the first director.
        """
        key, kid = self.key(server)
        web_url = str(server.detail.get("webUrl", ""))
        return sign(key, kid, scope, web_url, self.director, web_url)


def sign(key: str, kid: str, scope: str, issuer: str, audience: str, subject: str = "") -> str:
    """Return a token of scope, from issuer, for audience.

    It lasts TOKEN_LIFETIME seconds.

    Args:
        key: The PEM file, under framework/var, to sign it with.
        kid: The key ID to sign it under.
        scope: Its scope.
        issuer: Its issuer.
        audience: Its audience.
        subject: Its subject, if not credentials.SUBJECT.
    """
    now = int(time.time())
    claims = {
        "aud": [audience],
        "exp": now + TOKEN_LIFETIME,
        "iat": now,
        "iss": issuer,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "scope": scope,
        "sub": subject or credentials.SUBJECT,
        "wlcg.ver": "1.0",
    }
    return jwt.encode(jwt.header(jwt.algorithm(key), kid), claims, key=key)


def unknown_token(scope: str, issuer: str, audience: str) -> str:
    """Return a token of scope, from issuer, for audience.

    It is signed with UNKNOWN_KEY.
    """
    return sign(UNKNOWN_KEY, jwt.key_id(UNKNOWN_KEY), scope, issuer, audience)


def url_object(text: str) -> dict[str, str]:
    """Return the URL text as Go's encoding/json writes a url.URL.

    An ad's namespaces hold such URLs.
    """
    parts = urlsplit(text)
    return {
        "Scheme": parts.scheme,
        "Host": parts.netloc,
        "Path": parts.path,
        "RawQuery": parts.query,
    }


def namespace_ad(listed: Mapping[str, Any]) -> dict[str, Any]:
    """Return a namespace of an ad, from what a director says of it.

    Args:
        listed: What the director says (a NamespaceAdResponse).

    Returns:
        The namespace (a server_structs.NamespaceAd).
    """
    generations = [
        common.as_object(g) or {} for g in common.as_array(listed.get("tokenGeneration")) or []
    ]
    issuers = [
        common.as_object(i) or {} for i in common.as_array(listed.get("tokenIssuer")) or []
    ]
    return {
        "Caps": listed.get("capabilities") or {},
        "path": listed.get("path", ""),
        "token-generation": [
            {
                "strategy": g.get("strategy", ""),
                "vault-server": g.get("vaultServer", ""),
                "max-scope-depth": g.get("maxScopeDepth", 0),
                "issuer": url_object(str(g.get("issuer", ""))),
            }
            for g in generations
        ],
        "token-issuer": [
            {
                "base-paths": i.get("basePaths") or [],
                "restricted-paths": i.get("restrictedPaths") or [],
                "issuer": url_object(str(i.get("issuer", ""))),
            }
            for i in issuers
        ],
        "from-topology": False,
    }


def ad(server: Server, **changes: Any) -> dict[str, Any]:
    """Return an ad like server's own, as the director describes the server.

    The ad (a server_structs.OriginAdvertise) lasts AD_LIFETIME seconds.

    Args:
        server: The server whose ad to imitate.
        **changes: Values to set in the ad, by JSON name.
    """
    detail = server.detail
    now = datetime.now(timezone.utc)
    body: dict[str, Any] = {
        "name": server.name,
        "serverId": detail.get("serverId", ""),
        "version": detail.get("version", ""),
        "expiry": common.timestamp(now + timedelta(seconds=AD_LIFETIME)),
        "registry-prefix": server.prefix,
        "data-url": detail.get("url", ""),
        "web-url": detail.get("webUrl", ""),
        "capabilities": detail.get("capabilities") or {},
        "namespaces": [
            namespace_ad(common.as_object(n) or {})
            for n in common.as_array(detail.get("namespaces")) or []
        ],
        "token-issuer": [],
        "storageType": detail.get("storageType", ""),
        "directorTest": bool(detail.get("disableDirectorTest")),
        "downtimes": [],
        "requiredFeatures": [],
        "now": common.timestamp(now),
        "status": detail.get("serverStatus", ""),
    }
    if detail.get("brokerUrl"):
        body["broker-url"] = detail["brokerUrl"]
    body.update(changes)
    return body


def user_agent(kind: str, version: str) -> dict[str, str]:
    """Return the User-Agent with which a server of kind posts its ads.

    Returns:
        The header for a server at version, as the director reads it, or
        no header if version is not one.
    """
    bare = version.lstrip("v")
    if not bare[:1].isdigit():
        return {}
    return {"User-Agent": f"pelican-{kind.lower()}/{bare}"}


def post_ad(url: str, kind: str, body: Mapping[str, Any], token: Optional[str]) -> web.Response:
    """POST ad body to the director at url, as a server of kind does.

    It presents token, or no token if token is None.
    """
    headers = {"Content-Type": "application/json"}
    headers.update(user_agent(kind, str(body.get("version", ""))))
    return web.request(
        "POST",
        f"{url}/api/v1.0/director/register{kind}",
        token=token,
        upload=json.dumps(body).encode(),
        headers=headers,
    )
