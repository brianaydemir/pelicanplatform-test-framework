import unittest

from testlib.credentials import (
    ALLOW, BY_NAME, CREDENTIALS, DENY, NAMESPACES, OTHER, PROTECTED, applies, expected,
    has_issuer, key_ids, key_name, moot, namespace_jwks_problems, server_jwks_problems,
    token_decides)

# The columns of the credential table that make up a token's claims.
COLUMNS = ("key", "issuer", "scope_path", "scopes", "lifetime")

# The capabilities fed.sh gives each namespace, and with
# `-p origin-no-direct`, which leaves only reads.
PUBLIC = frozenset({"PublicReads", "Listings", "DirectReads"})
PROTECTED_CAPS = frozenset({"Reads", "Writes", "Listings", "DirectReads"})
NO_DIRECT = frozenset({"Reads"})
EXPORTS = {"public": PUBLIC, "protected-a": PROTECTED_CAPS, "protected-b": PROTECTED_CAPS}


class FakeFederation:
    """Just the issuer URLs and exports of common.Federation."""

    def __init__(self, issuers, exports=EXPORTS):
        self.issuers = issuers
        self.exports = exports

    def issuer_of(self, namespace):
        return self.issuers[namespace]


def column(cred, name):
    if name == "scopes":
        return (cred.get_scopes, cred.put_scopes)
    return getattr(cred, name)


class CredentialTable(unittest.TestCase):
    def test_names_are_unique(self):
        self.assertEqual(len(BY_NAME), len(CREDENTIALS))

    def test_only_server_and_the_listed_keys_are_allowed(self):
        allowed = [c.name for c in CREDENTIALS if c.verdict == ALLOW]
        self.assertEqual(allowed, ["server", "jwks", "ns-jwks"])
        self.assertTrue(all(c.verdict in (ALLOW, DENY) for c in CREDENTIALS))

    def test_each_tests_one_thing(self):
        # Every credential but `server` differs from it in exactly one
        # column, so that each tests one thing; but `ns-cross` is
        # `wrong-iss` signed with the other namespace's own key.
        server = BY_NAME["server"]
        for cred in CREDENTIALS:
            if cred is server:
                continue
            basis = BY_NAME["wrong-iss"] if cred.name == "ns-cross" else server
            differ = [c for c in COLUMNS if column(cred, c) != column(basis, c)]
            self.assertEqual(len(differ), 1, f"{cred.name} differs in {differ}")

    def test_wrong_op_has_the_other_operation_scopes(self):
        cred = BY_NAME["wrong-op"]
        self.assertNotIn("read", cred.scopes("get"))
        self.assertEqual(cred.scopes("put"), ("read",))
        self.assertEqual(cred.scopes("delete"), cred.scopes("put"))

    def test_only_expired_expires(self):
        self.assertEqual([c.name for c in CREDENTIALS if c.expired], ["expired"])

    def test_which_claim_the_other_issuer(self):
        self.assertEqual([c.name for c in CREDENTIALS if c.issuer != "own"],
                         ["wrong-iss", "ns-cross"])
        self.assertEqual(OTHER, {"protected-a": "protected-b", "protected-b": "protected-a"})

    def test_namespace_keys(self):
        # ns-jwks signs with the namespace's own key, and ns-other and
        # ns-cross with the other's; the rest sign with the same key in
        # either namespace.
        for ns in PROTECTED:
            self.assertEqual(key_name(BY_NAME["ns-jwks"].key, ns), f"ns-{ns}")
            for name in ("ns-other", "ns-cross"):
                self.assertEqual(key_name(BY_NAME[name].key, ns), f"ns-{OTHER[ns]}")
            for name in ("server", "jwks", "unknown"):
                self.assertEqual(key_name(BY_NAME[name].key, ns), BY_NAME[name].key)


class Expected(unittest.TestCase):
    def test_protected_follows_the_verdict(self):
        for cred in CREDENTIALS:
            for op in ("get", "head", "put", "delete"):
                self.assertEqual(expected(cred, PROTECTED_CAPS, op), cred.verdict,
                                 (cred.name, op))

    def test_public_reads_ignore_the_token(self):
        for cred in CREDENTIALS:
            for caps in (PUBLIC, frozenset({"PublicReads"})):
                self.assertEqual(expected(cred, caps, "get"), ALLOW, cred.name)
                self.assertEqual(expected(cred, caps, "head"), ALLOW, cred.name)

    def test_writes_need_writes(self):
        for cred in CREDENTIALS:
            for op in ("put", "delete"):
                self.assertEqual(expected(cred, PUBLIC, op), DENY, cred.name)
                self.assertEqual(expected(cred, NO_DIRECT, op), DENY, cred.name)
                self.assertEqual(expected(cred, PROTECTED_CAPS, op), cred.verdict, cred.name)

    def test_applies(self):
        # /public has no issuer, so it has no own key, and no other
        # namespace.
        self.assertEqual(NAMESPACES, ("public",) + PROTECTED)
        for ns in PROTECTED:
            self.assertTrue(all(applies(c, ns) for c in CREDENTIALS), ns)
        self.assertEqual([c.name for c in CREDENTIALS if not applies(c, "public")],
                         ["ns-jwks", "ns-other", "wrong-iss", "ns-cross"])

    def test_moot(self):
        # fed.sh's fed_issuer_url: one issuer per namespace, or, with the
        # origins' issuer off, the origin's own URL for every one.
        own = {ns: f"https://origin-0:8444/api/v1.0/issuer/ns/{ns}" for ns in NAMESPACES}
        off = {ns: "https://origin-0:8444" for ns in NAMESPACES}
        # `-p origin-httpsv2`: no export lists /protected-b's own key.
        one = {"protected-a": PROTECTED_CAPS}
        for fed, want in ((FakeFederation(own), set()),
                          (FakeFederation(own, one), {"ns-other", "ns-cross"}),
                          (FakeFederation(off), {"wrong-iss", "ns-jwks", "ns-other", "ns-cross"})):
            self.assertEqual({c.name for c in CREDENTIALS if moot(fed, c)}, want)

    def test_has_issuer(self):
        self.assertTrue(has_issuer(PROTECTED_CAPS))
        self.assertTrue(has_issuer(NO_DIRECT))
        self.assertFalse(has_issuer(PUBLIC))
        self.assertFalse(has_issuer(frozenset({"PublicReads"})))

    def test_token_decides(self):
        self.assertTrue(token_decides(PROTECTED_CAPS, "get"))
        self.assertTrue(token_decides(PROTECTED_CAPS, "put"))
        self.assertFalse(token_decides(PUBLIC, "get"))
        self.assertFalse(token_decides(PUBLIC, "put"))


class Keys(unittest.TestCase):
    OWN = {"protected-a": frozenset({"a"}), "protected-b": frozenset({"b"})}
    SERVER = frozenset({"origin", "jwks"})

    def test_key_ids(self):
        self.assertEqual(key_ids(b'{"keys": [{"kid": "a"}, {"kid": "b"}]}'), {"a", "b"})
        self.assertEqual(key_ids(b'{"keys": []}'), set())
        for bad in (b"", b"[]", b'{"keys": {}}', b"<html>"):
            with self.assertRaises(ValueError, msg=bad):
                key_ids(bad)

    def test_server_jwks(self):
        jwks = frozenset({"jwks"})
        self.assertEqual(server_jwks_problems(self.SERVER, jwks, self.OWN), [])
        self.assertEqual(server_jwks_problems({"origin"}, jwks, self.OWN),
                         ["lacks Server.IssuerJwks's key"])
        self.assertEqual(server_jwks_problems(self.SERVER | {"a", "b"}, jwks, self.OWN),
                         ["holds /protected-a's own key", "holds /protected-b's own key"])

    def test_namespace_jwks(self):
        def problems(have, server=self.SERVER):
            return namespace_jwks_problems("protected-a", frozenset(have), server, self.OWN)

        self.assertEqual(problems(self.SERVER | {"a"}), [])
        self.assertEqual(problems(self.SERVER), ["lacks /protected-a's own key"])
        self.assertEqual(problems(self.SERVER | {"a", "b"}), ["holds /protected-b's own key"])
        self.assertEqual(problems({"origin", "a"}), ["lacks the server-wide keys jwks"])
        self.assertEqual(problems(self.SERVER | {"a", "x"}), ["holds other keys x"])
        self.assertEqual(problems({"a"}, server=None),
                         ["could not compare it with the server-wide JWKS"])
