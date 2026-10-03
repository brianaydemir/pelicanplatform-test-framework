import unittest

from testlib.credentials import (BY_NAME, CREDENTIALS, NAMESPACES, PROTECTED, applies, moot,
                                 namespace_jwks_problems, server_jwks_problems)

# The columns of the credential table that make up a token's claims.
COLUMNS = ("key", "issuer", "scope_path", "scopes", "lifetime")

PROTECTED_CAPS = frozenset({"Reads", "Writes", "Listings", "DirectReads"})
EXPORTS = {"public": frozenset({"PublicReads", "Listings", "DirectReads"}),
           "protected-a": PROTECTED_CAPS, "protected-b": PROTECTED_CAPS}


class FakeFederation:
    """Just the issuer URLs, exports, and issuer mode of
    common.Federation."""

    def __init__(self, issuers, exports=EXPORTS, external_issuer=False):
        self.issuers = issuers
        self.exports = exports
        self.external_issuer = external_issuer

    def issuer_of(self, namespace):
        return self.issuers[namespace]


def column(cred, name):
    if name == "scopes":
        return (cred.get_scopes, cred.put_scopes)
    return getattr(cred, name)


class Table(unittest.TestCase):
    """A credential that a server should refuse for two reasons passes
    even if the server checks only one of them."""

    def test_each_tests_one_thing(self):
        # Every credential but `server` differs from it in exactly one
        # column; but `ns-cross` is `wrong-iss` signed with the other
        # namespace's own key.
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


class Skipped(unittest.TestCase):
    """A credential that isn't presented, or is skipped as moot, tests
    nothing."""

    def test_applies(self):
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
        # `-p auth-external-issuer`: the external issuer for every one.
        external = {ns: "https://discovery:8444/issuer" for ns in NAMESPACES}
        # `-p origin-httpsv2`: no export lists /protected-b's own key.
        one = {"protected-a": PROTECTED_CAPS}
        for fed, want in ((FakeFederation(own), set()),
                          (FakeFederation(own, one), {"ns-other", "ns-cross"}),
                          (FakeFederation(off), {"wrong-iss", "ns-jwks", "ns-other", "ns-cross"}),
                          (FakeFederation(external, external_issuer=True),
                           {"jwks", "wrong-iss", "ns-jwks", "ns-other", "ns-cross"})):
            self.assertEqual({c.name for c in CREDENTIALS if moot(fed, c)}, want)


class Keys(unittest.TestCase):
    OWN = {"protected-a": frozenset({"a"}), "protected-b": frozenset({"b"})}
    SERVER = frozenset({"origin", "jwks"})

    def test_server_jwks(self):
        jwks = frozenset({"jwks"})
        self.assertTrue(server_jwks_problems({"origin"}, jwks, self.OWN))
        self.assertTrue(server_jwks_problems(self.SERVER | {"a"}, jwks, self.OWN))

    def test_namespace_jwks(self):
        def problems(have, server=self.SERVER):
            return namespace_jwks_problems("protected-a", frozenset(have), server, self.OWN)

        for have in (self.SERVER, self.SERVER | {"a", "b"}, {"origin", "a"},
                     self.SERVER | {"a", "x"}):
            self.assertTrue(problems(have), sorted(have))
        self.assertTrue(problems({"a"}, server=None))


if __name__ == "__main__":
    unittest.main()
