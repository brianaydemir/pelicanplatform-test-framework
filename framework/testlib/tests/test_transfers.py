import random
import unittest

from testlib import transfers
from testlib.credentials import CREDENTIALS, PROTECTED, applies
from testlib.transfers import (
    BY_NAME, LOAD, PELICAN_SCENARIOS, PLUGIN_SCENARIOS, SCENARIOS, Batch, Load, Observation,
    Outcome, batch_count, classify, combine, judge, local_name, outcome, rel_path)

FED = "pelican://director-0:8444"

# What fed.sh writes to generated/exports by default, which pstore's
# shares: /public takes no writes.
DEFAULT = {"public": frozenset({"PublicReads", "Listings", "DirectReads"}),
           "protected-a": frozenset({"Reads", "Writes", "Listings", "DirectReads"}),
           "protected-b": frozenset({"Reads", "Writes", "Listings", "DirectReads"})}

TOKENS = ("server", "jwks", "ns-jwks", "unknown", "ns-other", "wrong-op", "wrong-path",
          "wrong-iss", "ns-cross", "expired")
CREDS = ("none",) + TOKENS
# Where a token decides, only these should be let through.
ALLOWED = ("server", "jwks", "ns-jwks")
# /public has no issuer, so it gets only the credentials that need none.
PUBLIC_CREDS = ("server", "jwks", "none", "unknown", "wrong-op", "wrong-path", "expired")

# Client output in the shape of Pelican's errors (error_codes.go), which
# read "<type> Error: Error code <n>: <message>".
CLI = "Failure getting pelican://director-0:8444/protected-a/data/0.3: "
SAMPLES = {
    "client": CLI + "Authorization.TokenNotFound Error: Error code 4010: credential is required",
    "director": CLI + "Contact.Director Error: Error code 3001: error while querying the"
                " director at https://director-0:8444: 401: Unauthorized",
    "server": CLI + "Authorization Error: Error code 4000: request failed (HTTP status 403):"
              " permission denied: server rejected the token",
    "not found": CLI + "Specification.FileNotFound Error: Error code 5011: 404: the object"
                 " does not exist in this namespace",
    "other": CLI + "Transfer.TimedOut Error: Error code 6002: timed out",
}

# The director's 405s (director/director.go, client/director.go): a get it
# can't send anywhere, and a put or delete to a namespace without Writes.
UNSUPPORTED = (
    CLI + "failed to get namespace information for remote URL: error while querying the"
    " director at https://director-0:8444: Contact.Director Error: Error code 3001: 405:"
    " Discovered sources for the namespace, but none support the request: no origins found"
    " that support the 'get' request type with queries 'DirectReads'",
    "Failure putting x.0: the director returned status code 405, indicating it understood the"
    " request but could not find an origin that supports PUT/DELETE operations for object:"
    " /public/data/put/put-public-server/x.0.",
)


def batches(exports=DEFAULT, load=LOAD):
    return transfers.make_batches(FED, exports, load, random.Random(1).randrange)


class ScenarioTable(unittest.TestCase):
    def test_every_scenario_has_a_plugin_twin(self):
        # Gets: exist, dne, 11 credentials in each protected namespace, and
        # 6 in /public (none is exist), through a cache and direct; puts: 11
        # in each protected namespace, and 7 in /public.
        self.assertEqual(len(PELICAN_SCENARIOS), 2 * (2 + 2 * 11 + 6) + 2 * 11 + 7)
        self.assertEqual(len(SCENARIOS), 2 * len(PELICAN_SCENARIOS))
        self.assertEqual(len(BY_NAME), len(SCENARIOS))
        for s, twin in zip(PELICAN_SCENARIOS, PLUGIN_SCENARIOS):
            self.assertEqual(twin.name, f"plugin-{s.name}")
            self.assertEqual((twin.client, s.client), ("plugin", "pelican"))
            self.assertEqual((twin.op, twin.route, twin.namespace, twin.credential, twin.missing),
                             (s.op, s.route, s.namespace, s.credential, s.missing))

    def test_every_get_has_a_direct_twin(self):
        gets = [s for s in PELICAN_SCENARIOS if s.op == "get"]
        cached = {s.name for s in gets if s.route == "cache"}
        direct = {s.name for s in gets if s.route == "direct"}
        self.assertEqual({f"direct-{n}" for n in cached}, direct)
        self.assertTrue(all(s.route == "origin" for s in PELICAN_SCENARIOS if s.op == "put"))

    def test_public_gets_only_the_credentials_that_need_no_issuer(self):
        self.assertEqual(tuple(c.name for c in CREDENTIALS if applies(c, "public")),
                         PUBLIC_CREDS)
        public = {s.credential.name for s in PELICAN_SCENARIOS
                  if s.namespace == "public" and s.credential is not None}
        self.assertEqual(public, set(PUBLIC_CREDS))

    def test_expectations(self):
        expected = {s.name: outcome(s, DEFAULT) for s in SCENARIOS}
        for prefix in ("", "direct-", "plugin-", "plugin-direct-"):
            self.assertEqual(expected[f"{prefix}exist"], Outcome.PASS)
            self.assertEqual(expected[f"{prefix}dne"], Outcome.NOT_FOUND)
            for ns in PROTECTED:
                for cred in CREDS:
                    want = Outcome.PASS if cred in ALLOWED else Outcome.REFUSED
                    self.assertEqual(expected[f"{prefix}get-{ns}-{cred}"], want)
            for cred in PUBLIC_CREDS:
                # Reads of /public are allowed whatever the token.
                if cred != "none":
                    self.assertEqual(expected[f"{prefix}get-public-{cred}"], Outcome.PASS)
        for prefix in ("", "plugin-"):
            for ns in PROTECTED:
                for cred in CREDS:
                    allowed = Outcome.PASS if cred in ALLOWED else Outcome.REFUSED
                    self.assertEqual(expected[f"{prefix}put-{ns}-{cred}"], allowed)
            for cred in PUBLIC_CREDS:
                # /public takes no writes.
                self.assertEqual(expected[f"{prefix}put-public-{cred}"], Outcome.REFUSED)

    def test_each_put_scenario_writes_its_own_collection(self):
        puts = [s for s in SCENARIOS if s.op == "put"]
        self.assertEqual(len({s.collection for s in puts}), len(puts))
        self.assertEqual(len({s.store_dir for s in puts}), len(puts))
        for s in puts:
            self.assertEqual(s.collection, f"/{s.namespace}/data/put/{s.name}/")
            self.assertEqual(s.store_dir, f"data/put/{s.name}")
            self.assertIn(s.collection, s.description)


class Batches(unittest.TestCase):
    def test_counts_and_sizes(self):
        made = batches()
        self.assertEqual(set(made), set(BY_NAME))
        for name, list_ in made.items():
            s = BY_NAME[name]
            # In /public, a token never decides: reads are allowed, and
            # writes refused.
            heavy = s.credential is None or (s.credential.name in ALLOWED
                                             and s.namespace != "public")
            want = [1, 2] if heavy else [1]
            self.assertEqual(batch_count(s, DEFAULT, LOAD), len(want), name)
            self.assertEqual([len(b.urls) for b in list_], want, name)

    def test_totals(self):
        # Invocations in the default shape: 178 scenarios, and a second
        # batch for the 36 whose token should let them through, and for
        # the 8 `exist`s and `dne`s.
        self.assertEqual(len(SCENARIOS), 178)
        self.assertEqual(sum(len(b) for b in batches(DEFAULT).values()), 222)

    def test_sizes_are_taken_in_turn(self):
        load = Load(objects=8, batches=5, refused_batches=2, sizes=(1, 2, 4))
        made = batches(load=load)
        self.assertEqual([len(b.urls) for b in made["exist"]], [1, 2, 4, 1, 2])
        self.assertEqual([len(b.urls) for b in made["get-protected-a-unknown"]], [1, 2])

    def test_only_exported_namespaces(self):
        # A shape that exports only /protected-a (`-p origin-httpsv2`),
        # whose scenarios are as they are with every namespace.
        one = {"protected-a": DEFAULT["protected-a"]}
        made = batches(one)
        self.assertEqual(set(made), {s.name for s in SCENARIOS if s.namespace == "protected-a"})
        for name, list_ in made.items():
            self.assertEqual(len(list_), len(batches()[name]), name)

    def test_gets_pick_distinct_objects(self):
        for list_ in batches().values():
            for b in list_:
                self.assertEqual(len(set(b.urls)), len(b.urls), b.id)

    def test_twins_are_identical_but_for_the_put_collection_and_directread(self):
        made = batches()
        for s in PELICAN_SCENARIOS:
            mine, twins = made[s.name], made[f"plugin-{s.name}"]
            self.assertEqual([b.names for b in mine], [b.names for b in twins])
            for a, b in zip(mine, twins):
                if s.op == "get" and s.direct:
                    # The plugin has no flag for a direct read.
                    self.assertEqual(tuple(u + "?directread" for u in a.urls), b.urls)
                elif s.op == "get":
                    self.assertEqual(a.urls, b.urls)
                else:
                    self.assertTrue(all("/put/put-" in u for u in a.urls))
                    self.assertTrue(all("/put/plugin-put-" in u for u in b.urls))

    def test_urls_are_in_the_scenario_collection(self):
        for name, list_ in batches().items():
            s = BY_NAME[name]
            for b in list_:
                for url in b.urls:
                    self.assertTrue(url.startswith(FED + s.collection), url)
                    if s.op == "get":
                        self.assertRegex(local_name(url), rf"^{s.stem}\.\d+$")

    def test_round_trip(self):
        for list_ in batches().values():
            for b in list_:
                text = transfers.format_batch(b)
                self.assertEqual(transfers.parse_batch(b.scenario, b.index, text), b)
                # LocalFileName is always the URL's last component.
                for line, url in zip(text.splitlines(), b.urls):
                    self.assertIn(f'LocalFileName="{local_name(url)}"', line)

    def test_destination(self):
        s = BY_NAME["put-protected-a-server"]
        one = Batch(s, 0, (f"{FED}/protected-a/data/put/put-protected-a-server/0.0",))
        two = Batch(s, 1, (f"{FED}/protected-a/data/put/put-protected-a-server/1.0",
                           f"{FED}/protected-a/data/put/put-protected-a-server/1.1"))
        self.assertEqual(one.destination, one.urls[0])
        self.assertEqual(two.destination, f"{FED}/protected-a/data/put/put-protected-a-server/")

    def test_rel_path_and_local_name(self):
        self.assertEqual(rel_path(f"{FED}/public/data/0.17"), "data/0.17")
        self.assertEqual(rel_path(f"{FED}/public/data/0.17?directread"), "data/0.17")
        self.assertEqual(rel_path(f"{FED}/protected-a/data/put/put-protected-a-jwks/3.1"),
                         "data/put/put-protected-a-jwks/3.1")
        self.assertEqual(local_name(f"{FED}/public/data/0.17?directread"), "0.17")


class Classify(unittest.TestCase):
    def test_director_not_found(self):
        # A direct read under `topo-multi-origin` (director/director.go).
        line = (CLI + "Contact.Director Error: Error code 3001: 404: No sources reported"
                " possession of the object: object /public/data/9.5 could not be found")
        self.assertEqual(classify(line), "not found")

    def test_401_in_a_name_is_not_a_refusal(self):
        line = CLI.replace("0.3", "9.401") + "Contact.Director Error: Error code 3001: 404"
        self.assertNotEqual(classify(line), "director")

    def test_samples(self):
        for category, line in SAMPLES.items():
            self.assertEqual(classify(line), category, line)
        for line in UNSUPPORTED:
            self.assertEqual(classify(line), "unsupported", line)

    def test_server_refusals(self):
        # `pelican object get` stats each object first (client/main.go),
        # so a refused get fails there (client/acquire_token.go). The rest
        # are how a transfer reports one (client/handle_http.go,
        # error_helpers.go).
        stat = CLI + ("failed to stat remote source \"/protected-a/data/0.3\" while deciding"
                      " destination layout: failed to do the stat: ")
        for line in (
                stat + "HTTP 403: the server refused the credential; the cached credential"
                " has been discarded",
                stat + "HTTP 401: authentication failed 4 consecutive times, including with"
                " freshly acquired credentials",
                CLI + "Authorization Error: Error code 4000: request failed (HTTP status 401):",
                CLI + "server returned 401 Unauthorized: no valid credential was presented"
                " for this object",
                CLI + "permission denied: your credential does not grant access to this object"):
            self.assertEqual(classify(line), "server", line)

    def test_plugin_ad(self):
        ad = ('[ TransferSuccess = false; TransferError = "' + SAMPLES["server"][len(CLI):]
              + '"; TransferUrl = "pelican://director-0:8444/protected-a/data/0.3" ]')
        self.assertEqual(transfers.failure_lines("plugin", ad), [ad])
        self.assertEqual(classify(ad), "server")

    def test_failure_lines(self):
        log = "\n".join(["INFO something", SAMPLES["client"], "exit status 7"])
        self.assertEqual(transfers.failure_lines("pelican", log), [SAMPLES["client"]])

    def test_combine(self):
        self.assertEqual(combine(["server", "server"]), "server")
        self.assertEqual(combine(["server", "director"]), "mixed")
        self.assertEqual(combine(["server", "other"]), "other")
        self.assertEqual(combine(["server", "not found"]), "other")
        self.assertEqual(combine(["not found"] * 4), "not found")
        self.assertEqual(combine([]), "other")


PASS, REFUSED, NOT_FOUND = Outcome.PASS, Outcome.REFUSED, Outcome.NOT_FOUND


class Judge(unittest.TestCase):
    def setUp(self):
        self.get = Batch(BY_NAME["get-protected-a-server"], 0,
                         (f"{FED}/protected-a/data/0.1", f"{FED}/protected-a/data/0.2"))
        self.refs = {"data/0.1": "h1", "data/0.2": "h2"}

    def test_pass(self):
        ok = Observation(True, None, {"0.1": "h1", "0.2": "h2"})
        self.assertEqual(judge(self.get, PASS, ok, self.refs, {}), [])

    def test_pass_that_failed(self):
        failed = Observation(False, "server", {})
        self.assertEqual(judge(self.get, PASS, failed, self.refs, {}),
                         ["failed (server), but should have passed"])

    def test_pass_with_wrong_bytes(self):
        seen = Observation(True, None, {"0.1": "h1", "0.2": "bad"})
        self.assertEqual(judge(self.get, PASS, seen, self.refs, {}),
                         ["0.2: bytes differ from the origin's"])
        seen = Observation(True, None, {"0.1": "h1"})
        self.assertEqual(judge(self.get, PASS, seen, self.refs, {}), ["0.2: missing locally"])

    def test_direct_urls(self):
        get = Batch(BY_NAME["plugin-direct-get-protected-a-server"], 0,
                    tuple(u + "?directread" for u in self.get.urls))
        ok = Observation(True, None, {"0.1": "h1", "0.2": "h2"})
        self.assertEqual(judge(get, PASS, ok, self.refs, {}), [])

    def test_put(self):
        put = Batch(BY_NAME["put-protected-a-jwks"], 3,
                    (f"{FED}/protected-a/data/put/put-protected-a-jwks/3.0",))
        stores = {"data/put/put-protected-a-jwks/3.0": {"h"}}
        self.assertEqual(judge(put, PASS, Observation(True, None, {"3.0": "h"}), {}, stores), [])
        self.assertEqual(judge(put, PASS, Observation(True, None, {"3.0": "h"}), {}, {}),
                         ["3.0: not at any origin"])
        self.assertEqual(judge(put, PASS, Observation(True, None, {"3.0": "x"}), {}, stores),
                         ["3.0: bytes differ at the origin"])

    def test_refused(self):
        get = Batch(BY_NAME["get-protected-a-unknown"], 0, self.get.urls)
        for how in ("client", "director", "unsupported", "server", "mixed"):
            self.assertEqual(judge(get, REFUSED, Observation(False, how, {}), self.refs, {}), [])
        self.assertEqual(judge(get, REFUSED, Observation(False, "other", {}), self.refs, {}),
                         ["failed (other), but was not refused"])
        self.assertEqual(judge(get, REFUSED, Observation(False, "not found", {}), self.refs, {}),
                         ["failed (not found), but was not refused"])
        self.assertEqual(judge(get, REFUSED, Observation(True, None, {}), self.refs, {}),
                         ["passed, but should have been refused"])

    def test_refused_but_moved(self):
        get = Batch(BY_NAME["get-protected-a-unknown"], 0, self.get.urls)
        leaked = Observation(False, "server", {"0.2": "h2"})
        self.assertEqual(judge(get, REFUSED, leaked, self.refs, {}),
                         ["0.2: arrived although refused"])
        put = Batch(BY_NAME["plugin-put-public-server"], 0,
                    (f"{FED}/public/data/put/plugin-put-public-server/0.0",))
        stored = {"data/put/plugin-put-public-server/0.0": {"h"}}
        seen = Observation(False, "unsupported", {"0.0": "h"})
        self.assertEqual(judge(put, REFUSED, seen, {}, stored),
                         ["0.0: exists at an origin although refused"])

    def test_not_found(self):
        dne = Batch(BY_NAME["dne"], 0, (f"{FED}/public/data/9.5",))
        self.assertEqual(judge(dne, NOT_FOUND, Observation(False, "not found", {}), {}, {}), [])
        self.assertEqual(judge(dne, NOT_FOUND, Observation(False, "server", {}), {}, {}),
                         ["failed (server), but not with not found"])
        self.assertEqual(judge(dne, NOT_FOUND, Observation(True, None, {}), {}, {}),
                         ["passed, but should have failed: not found"])


if __name__ == "__main__":
    unittest.main()
