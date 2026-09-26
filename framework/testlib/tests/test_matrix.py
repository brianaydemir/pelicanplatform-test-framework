import itertools
import os
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor

import matrix

FED = os.path.join(matrix.TOP, "fed.sh")


def accepts(presets):
    """Whether `fed.sh -p ... check` accepts presets."""
    args = ["sh", FED]
    for preset in presets:
        args += ["-p", preset]
    return subprocess.run(args + ["check"], capture_output=True).returncode == 0


class Model(unittest.TestCase):
    def test_shape_of(self):
        shape, problems = matrix.shape_of(["topo-tiny", "origin-xrootd"], rotated=True)
        self.assertEqual(problems, [])
        self.assertEqual((shape["topology"], shape["origin"], shape["cache"], shape["keys"]),
                         ("tiny", "xrootd", "v2", "rotated"))

    def test_shape_of_refuses_two_choices(self):
        shape, problems = matrix.shape_of(["origin-metadata", "origin-metadata-tx"])
        self.assertIsNone(shape)
        self.assertIn("both choose metadata", problems[0])

    def test_shape_of_names_what_is_not_run(self):
        shape, problems = matrix.shape_of(["with-grafana", "no-such"])
        self.assertIsNone(shape)
        self.assertIn("not run", problems[0])
        self.assertIn("no such preset", problems[1])

    def test_presets_of_round_trip(self):
        for presets in (["topo-tiny", "origin-pstore", "origin-max-age"],
                        ["origin-httpsv2", "topo-multi-owner", "with-lab"]):
            shape, _ = matrix.shape_of(presets)
            self.assertEqual(matrix.shape_of(matrix.presets_of(shape))[0], shape)

    def test_every_other_level_has_a_preset(self):
        chosen = set(matrix.PRESETS.values())
        for factor, levels in matrix.FACTORS.items():
            if factor != "keys":
                self.assertTrue({(factor, level) for level in levels[1:]} <= chosen, factor)

    def test_every_preset_is_known(self):
        known = set(matrix.PRESETS) | matrix.EXTRAS | set(matrix.NOT_RUN)
        self.assertEqual(matrix.all_presets() - known, set())
        self.assertEqual(known - matrix.all_presets(), set())

    def test_parse_table(self):
        shapes = matrix.parse_table("a\tyes\tx y\nb\tno\t\n")
        self.assertEqual(shapes, [matrix.SmokeShape("a", True, ("x", "y")),
                                  matrix.SmokeShape("b", False, ())])

    def test_check_finds_problems(self):
        shapes = [matrix.SmokeShape("a", True, ("server-unprivileged",)),
                  matrix.SmokeShape("a", False, ())]
        found = matrix.check(shapes)
        self.assertIn("two shapes are named a", found)
        self.assertTrue(any(p.startswith("a: once the servers drop") for p in found))
        self.assertTrue(any(p.startswith("no shape runs ") for p in found))
        self.assertTrue(any(p.startswith("no shape has both ") for p in found))


class Smoke(unittest.TestCase):
    """smoke.sh's shapes, against the model and fed.sh."""

    @classmethod
    def setUpClass(cls):
        cls.shapes = matrix.smoke_shapes()

    def test_shapes_cover_every_pair(self):
        self.assertEqual(matrix.check(self.shapes), [])

    def test_fed_accepts_every_shape(self):
        with ThreadPoolExecutor(8) as pool:
            verdicts = list(pool.map(lambda s: accepts(s.presets), self.shapes))
        refused = [s.name for s, ok in zip(self.shapes, verdicts) if not ok]
        self.assertEqual(refused, [])


class Fed(unittest.TestCase):
    """REFUSED against fed.sh, for every pair of presets that choose
    different factors."""

    def test_refused_matches_fed(self):
        presets = sorted(set(matrix.PRESETS) | matrix.EXTRAS)
        pairs = []
        for a, b in itertools.combinations(presets, 2):
            if a in matrix.PRESETS and b in matrix.PRESETS \
                    and matrix.PRESETS[a][0] == matrix.PRESETS[b][0]:
                continue
            shape, problems = matrix.shape_of([a, b])
            self.assertEqual(problems, [])
            refused = any(applies(shape) for _, applies in matrix.REFUSED)
            pairs.append(((a, b), refused))
        with ThreadPoolExecutor(8) as pool:
            verdicts = list(pool.map(lambda p: accepts(p[0]), pairs))
        wrong = [f"{a} {b}: fed.sh {'accepts' if ok else 'refuses'}"
                 for ((a, b), refused), ok in zip(pairs, verdicts) if ok == refused]
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
