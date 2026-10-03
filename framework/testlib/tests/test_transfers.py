import random
import unittest

from testlib import report, transfers
from testlib.transfers import (BY_NAME, LOAD, SCENARIOS, Batch, Observation, Outcome,
                               check_served_by, classify, combine, judge)

FED = "pelican://director-0:8444"

# What fed.sh writes to generated/exports by default.
DEFAULT = {"public": frozenset({"PublicReads", "Listings", "DirectReads"}),
           "protected-a": frozenset({"Reads", "Writes", "Listings", "DirectReads"}),
           "protected-b": frozenset({"Reads", "Writes", "Listings", "DirectReads"})}

CLI = "Failure getting pelican://director-0:8444/protected-a/data/0.3: "
SERVER_REFUSAL = (CLI + "Authorization Error: Error code 4000: request failed (HTTP status 403):"
                  " permission denied: server rejected the token")


def batches(exports):
    return transfers.make_batches(FED, exports, LOAD, random.Random(1).randrange)


class Coverage(unittest.TestCase):
    """A scenario that drops out of the table, or runs no batch, would
    leave no row or pass having moved nothing."""

    def test_the_table(self):
        self.assertEqual(len(SCENARIOS), 178)

    def test_every_exported_scenario_moves_something(self):
        one = {"protected-a": DEFAULT["protected-a"]}
        for exports, names in ((DEFAULT, set(BY_NAME)),
                               # `dne` moves to /protected-a (see placed()).
                               (one, {s.name for s in SCENARIOS
                                      if s.namespace == "protected-a" or s.missing})):
            made = batches(exports)
            self.assertEqual(set(made), names)
            for name, list_ in made.items():
                self.assertTrue(list_, name)
                self.assertTrue(all(b.urls for b in list_), name)


class Refusals(unittest.TestCase):
    """What must not pass for a refusal: a failure beside a 401 or 403
    that no refusal explains, or a local "permission denied"."""

    def test_classify(self):
        for line in (
                CLI.replace("0.3", "9.401") + "Contact.Director Error: Error code 3001: 404",
                CLI + "server returned 403 Forbidden: either your credential does not grant"
                " access to this object; then: request failed (HTTP status 500): oops",
                CLI + "Authorization Error: Error code 4000: request failed (HTTP status 403);"
                " Transfer.HeaderTimeout Error: Error code 6004: timed out waiting for headers",
                CLI + "HTTP 403: refused; dial tcp 10.0.0.1:8443: connect: connection refused",
                CLI + "server returned 403 Forbidden; Specification.FileNotFound Error: Error"
                " code 5011: 404",
                CLI + "Authorization Error: Error code 4000: open /tmp/x/0.3: permission denied",
                CLI + "mkdir /tmp/x: permission denied",
                CLI + "Authorization Error: Error code 4000: permission denied when opening local"
                " object: \"x.0\": open x.0: permission denied"):
            self.assertNotIn(classify(line), transfers.REFUSALS, line)

    def test_combine(self):
        self.assertEqual(combine(["server", "other"]), "other")
        self.assertEqual(combine(["server", "not found"]), "other")
        self.assertEqual(combine([]), "other")

    def test_refused(self):
        for text in (
                SERVER_REFUSAL + "\n" + CLI + "request failed (HTTP status 500): oops",
                "HTTP 403 Forbidden\nserver returned 502 Bad Gateway",
                "HTTP 403 Forbidden\ntimed out after 600s, and was killed",
                CLI + "Contact.Director Error: Error code 3001: error while querying the director"
                " at https://director-0:8444: 503: Service Unavailable; 401",
                CLI + "Authorization Error: Error code 4000: open /tmp/x/0.3: permission denied",
                "dial tcp 10.0.0.1:8443: connect: connection refused\nHTTP 401"):
            self.assertIsNone(transfers.refused(text), text)


class Judge(unittest.TestCase):
    def setUp(self):
        self.urls = (f"{FED}/protected-a/data/0.1", f"{FED}/protected-a/data/0.2")
        self.get = Batch(BY_NAME["get-protected-a-server"], 0, self.urls)
        self.refused = Batch(BY_NAME["get-protected-a-unknown"], 0, self.urls)
        self.refs = {"data/0.1": "h1", "data/0.2": "h2"}

    def test_pass(self):
        for seen in (Observation(False, "server", {}),
                     Observation(True, None, {"0.1": "h1", "0.2": "bad"}),
                     Observation(True, None, {"0.1": "h1"})):
            self.assertTrue(judge(self.get, Outcome.PASS, seen, self.refs, {}), seen)

    def test_put(self):
        put = Batch(BY_NAME["put-protected-a-jwks"], 3,
                    (f"{FED}/protected-a/data/put/put-protected-a-jwks/3.0",))
        stores = {"data/put/put-protected-a-jwks/3.0": {"h"}}
        self.assertTrue(judge(put, Outcome.PASS, Observation(True, None, {"3.0": "h"}), {}, {}))
        self.assertTrue(judge(put, Outcome.PASS, Observation(True, None, {"3.0": "x"}), {},
                              stores))

    def test_refused(self):
        for seen in (Observation(True, None, {}), Observation(False, "other", {}),
                     Observation(False, "not found", {}),
                     Observation(False, "server", {"0.2": "h2"})):
            self.assertTrue(judge(self.refused, Outcome.REFUSED, seen, self.refs, {}), seen)
        put = Batch(BY_NAME["plugin-put-public-server"], 0,
                    (f"{FED}/public/data/put/plugin-put-public-server/0.0",))
        stored = {"data/put/plugin-put-public-server/0.0": {"h"}}
        self.assertTrue(judge(put, Outcome.REFUSED, Observation(False, "unsupported", {}), {},
                              stored))

    def test_not_found(self):
        dne = Batch(BY_NAME["dne"], 0, (f"{FED}/public/data/9.5",))
        for seen in (Observation(True, None, {}), Observation(False, "server", {})):
            self.assertTrue(judge(dne, Outcome.NOT_FOUND, seen, {}, {}), seen)


class ServedBy(unittest.TestCase):
    """A director that routes around its caches, or a cache that serves
    direct reads, would pass every scenario."""

    ORIGINS = frozenset({"origin-0", "origin-1"})

    def check(self, route, served):
        return check_served_by(route, served, self.ORIGINS, False)[0]

    def test_cache(self):
        served = {"public": {"cache-0": 3}, "protected-a": {"origin-0": 2}}
        self.assertEqual(self.check("cache", served), report.FAIL)

    def test_direct(self):
        served = {"public": {"origin-0": 2}, "protected-a": {"cache-0": 1}}
        self.assertEqual(self.check("direct", served), report.FAIL)

    def test_nothing_recorded(self):
        for route in ("cache", "direct"):
            self.assertEqual(self.check(route, {"public": {}}), report.FAIL)

    def test_the_last_attempt_served_it(self):
        stats = [[{"attempts": [{"attemptNumber": 1, "endpoint": "origin-0:8444"},
                                {"attemptNumber": 0, "endpoint": "cache-0:8443"}]}]]
        self.assertEqual(transfers.stats_endpoints(stats), ["origin-0"])


if __name__ == "__main__":
    unittest.main()
