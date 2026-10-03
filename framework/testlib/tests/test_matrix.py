import itertools
import os
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor

import matrix

FED = os.path.join(matrix.TOP, "fed.sh")


def verdict(presets):
    """`fed.sh -p ... check` on presets: its exit status and stderr. It
    exits 0 if it accepts them, 1 if a check refuses the shape (die), and
    2 if two presets choose one knob (die_usage), as for any other usage
    error."""
    args = ["sh", FED]
    for preset in presets:
        args += ["-p", preset]
    done = subprocess.run(args + ["check"], capture_output=True, text=True)
    return done.returncode, done.stderr.strip()


class Smoke(unittest.TestCase):
    """A pair of choices that no shape makes is never tested."""

    def test_shapes_cover_every_pair(self):
        self.assertEqual(matrix.check(matrix.smoke_shapes()), [])


class Fed(unittest.TestCase):
    """A shape that the model wrongly takes for refused leaves its pairs
    untested. So the model is checked against fed.sh, for every pair of
    presets: REFUSED for
    those that choose different factors, and shape_of() for those that
    choose one factor differently, which fed.sh must refuse as two
    choices. A refusal must be the one the model predicts, not any
    failure."""

    def test_refused_matches_fed(self):
        presets = sorted(set(matrix.PRESETS) | matrix.EXTRAS)
        pairs = []
        for a, b in itertools.combinations(presets, 2):
            shape, problems = matrix.shape_of([a, b])
            if problems:
                self.assertIsNone(shape)
                self.assertIn("both choose", problems[0])
                want = 2
            else:
                want = 1 if any(applies(shape) for _, applies in matrix.REFUSED) else 0
            pairs.append(((a, b), want))
        with ThreadPoolExecutor(8) as pool:
            verdicts = list(pool.map(lambda p: verdict(p[0]), pairs))
        wrong = []
        for ((a, b), want), (code, err) in zip(pairs, verdicts):
            if code != want or (want == 2 and "both set" not in err):
                wrong.append(f"{a} {b}: fed.sh exits {code}, not {want}: {err}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
