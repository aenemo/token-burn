import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parity_check  # noqa: E402


def series(key, scale=1.0):
    return {"key": key, "model": key, "effort": None, "t": [0, 1],
            "input": [round(500 * scale), None], "output": [round(300 * scale), 0],
            "cache": [round(2000 * scale), 0], "cost": [1.0 * scale, 0.0],
            "requests": [round(1000 * scale), 0],
            "sub_input": [0, 0], "sub_output": [0, 0], "sub_cache": [0, 0], "sub_cost": [0, 0]}


def doc(scale=1.0, keys=("a", "b")):
    plat = {"status": "ok", "start": 0, "minutes": 60,
            "series": [series(k, scale) for k in keys],
            "subagent_runs": [], "plan": {"x": 1}, "meta": {}, "installed": True}
    return {"generated_at": 0, "token_refresh_s": 60, "plan_refresh_s": 300,
            "platforms": {"claude": plat, "cursor": json.loads(json.dumps(plat))}}


class ParityCheckTest(unittest.TestCase):
    def run_cli(self, ref, cand, tol="1"):
        with tempfile.TemporaryDirectory() as d:
            rp, cp = os.path.join(d, "ref.json"), os.path.join(d, "cand.json")
            for path, content in ((rp, ref), (cp, cand)):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(content, f)
            with contextlib.redirect_stdout(io.StringIO()):
                return parity_check.main(["--reference-url", rp, "--candidate-url", cp,
                                          "--tolerance", tol])

    def test_identical(self):
        self.assertEqual(self.run_cli(doc(), doc()), 0)

    def test_small_drift_within_tolerance(self):
        self.assertEqual(self.run_cli(doc(), doc(1.005)), 0)

    def test_large_drift_fails(self):
        self.assertEqual(self.run_cli(doc(), doc(1.05)), 1)

    def test_missing_series_key_fails(self):
        self.assertEqual(self.run_cli(doc(), doc(keys=("a",))), 1)

    def test_missing_input_file_fails(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = parity_check.main(["--reference-url", "nope1.json", "--candidate-url", "nope2.json"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
