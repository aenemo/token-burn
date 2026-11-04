"""Rolling-window series maths in web/js/stats.js, run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"
START = "2026-01-01T00:00:00Z"


def run(body):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.TokenBurn.stats;var P={start:'%s'};%s" % (START, body))
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class RollingSeries(unittest.TestCase):
    # series A: minutes 2,5,6 ; input/output/cache (tokens) and cost
    A = ("{t:[2,5,6],input:[1,2,3],output:[10,20,30],cache:[100,200,300],cost:[0.5,1,1.5]}")
    B = ("{t:[0,6,9],input:[1,1,1],output:[0,0,0],cache:[0,0,0],cost:[2,2,2]}")

    def frame(self, end, w="30m"):
        # now = start + end minutes (+ a few seconds)
        return "var fr=S.frame(P,'%s',Date.parse('%s')+%d*60000+5000);" % (w, START, end)

    def test_hand_computed(self):
        # tokens/min: m2=111, m5=222, m6=333. span 3, window 30m ending idx 8 (lo<0 is fine)
        r = run(self.frame(8) + "console.log(JSON.stringify(S.rollingSeries(%s,'tokens',fr,3,720)))" % self.A)
        by = {round((p["x"] - 1767225600000) / 60000): p["y"] for p in r}
        self.assertEqual(by[8], 333)          # minutes 6..8
        self.assertEqual(by[7], 555)          # 5..7
        self.assertEqual(by[5], 222)          # 3..5: only m5
        self.assertEqual(by[2], 111)

    def test_tokens_vs_cost(self):
        r = run(self.frame(8) + "console.log(JSON.stringify([S.rollingSeries(%s,'tokens',fr,100,720),"
                "S.rollingSeries(%s,'cost',fr,100,720)]))" % (self.A, self.A))
        self.assertEqual(r[0][-1]["y"], 666)
        self.assertAlmostEqual(r[1][-1]["y"], 3.0)

    def test_partial_span_larger_than_history(self):
        r = run(self.frame(8) + "console.log(JSON.stringify([S.rollingSeries(%s,'tokens',fr,100000,720),"
                "S.rollingInfo(P,fr,100000),S.rollingInfo(P,fr,3)]))" % self.A)
        self.assertEqual(r[0][-1]["y"], 666)
        self.assertTrue(r[1]["partial"])
        self.assertEqual(r[1]["historyStartMs"], 1767225600000)
        self.assertLess(r[1]["lookbackMs"], r[1]["historyStartMs"])
        # frame lo = 8-30+1 < 0 so even a 3 minute span is partial relative to window start? lookback = lo-2 <0
        self.assertTrue(r[2]["partial"])

    def test_not_partial_when_history_long_enough(self):
        r = run(self.frame(100) + "console.log(JSON.stringify(S.rollingInfo(P,fr,30)))")
        self.assertFalse(r["partial"])  # lo=71, lookback=42

    def test_empty_series(self):
        r = run(self.frame(8) + "console.log(JSON.stringify([S.rollingSeries({},'tokens',fr,5,720),"
                "S.rollingSeries({t:[]},'cost',fr,5,720),S.rollingTotal([],'tokens',fr,5,720)]))")
        for pts in r:
            self.assertEqual(len(pts), 30)
            self.assertTrue(all(p["y"] == 0 for p in pts))

    def test_total_equals_sum_of_series(self):
        r = run(self.frame(8) + "var rows=[{series:%s},{series:%s}];"
                "console.log(JSON.stringify([S.rollingTotal(rows,'tokens',fr,4,720),"
                "S.rollingSeries(rows[0].series,'tokens',fr,4,720),S.rollingSeries(rows[1].series,'tokens',fr,4,720)]))"
                % (self.A, self.B))
        tot, a, b = r
        self.assertEqual(len(tot), len(a))
        for i in range(len(tot)):
            self.assertEqual(tot[i]["x"], a[i]["x"])
            self.assertAlmostEqual(tot[i]["y"], a[i]["y"] + b[i]["y"])

    def test_final_point_is_last_span_minutes(self):
        # span 4 ending idx 9 -> minutes 6..9 : A m6=333 ; B m6=1,m9=1
        r = run(self.frame(9) + "var rows=[{series:%s},{series:%s}];"
                "console.log(JSON.stringify(S.rollingTotal(rows,'tokens',fr,4,720)))" % (self.A, self.B))
        self.assertEqual(r[-1]["y"], 333 + 1 + 1)
        self.assertEqual(r[-1]["x"], 1767225600000 + 9 * 60000)

    def test_span_covering_window_matches_cumulative_when_history_permits(self):
        # Assert: when span == W and data starts at/after loIdx-... i.e. all data lies inside the window
        # (history before loIdx is empty), rolling(final) == cumulative(final). Here lo<0 so all data is inside.
        r = run(self.frame(9) + "var s=%s;console.log(JSON.stringify([S.rollingSeries(s,'tokens',fr,fr.W,720),"
                "S.cumulativeSeries(s,'tokens',fr)]))" % self.A)
        self.assertEqual(r[0][-1]["y"], r[1][-1]["y"])
        self.assertEqual(r[0][-1]["x"], r[1][-1]["x"])

    def test_span_covering_window_differs_when_history_before_window(self):
        # Window 30m ending at 100 holds no data; span of 1000 reaches back to the data.
        r = run(self.frame(100) + "var s=%s;console.log(JSON.stringify([S.rollingSeries(s,'tokens',fr,1000,720),"
                "S.cumulativeSeries(s,'tokens',fr)]))" % self.A)
        self.assertEqual(r[0][-1]["y"], 666)
        self.assertEqual(r[1][-1]["y"], 0)

    def test_max_points_cap_and_last_point(self):
        r = run("var fr=S.frame(P,'7d',Date.parse('%s')+20000*60000);"
                "console.log(JSON.stringify([S.rollingSeries(%s,'tokens',fr,60,100),"
                "S.rollingSeries(%s,'tokens',fr,60,720),fr]))" % (START, self.A, self.A))
        small, dflt, fr = r
        self.assertLessEqual(len(small), 100)
        self.assertLessEqual(len(dflt), 720)
        for pts in (small, dflt):
            self.assertEqual(pts[-1]["x"], fr["startMs"] + fr["endIdx"] * 60000)
            self.assertGreaterEqual(pts[0]["x"], fr["loMs"])
            xs = [p["x"] for p in pts]
            self.assertEqual(xs, sorted(xs))

    def test_exports_present(self):
        r = run("console.log(JSON.stringify(Object.keys(S)))")
        for k in ["WINDOWS", "DEFAULT_WINDOW", "windowDef", "frame", "seriesStats", "totals",
                  "cumulativeSeries", "runsInWindow", "planStats", "SPANS", "DEFAULT_SPAN",
                  "spanDef", "rollingSeries", "rollingTotal", "rollingInfo"]:
            self.assertIn(k, r)

    def test_spans(self):
        r = run("console.log(JSON.stringify([S.SPANS.map(function(x){return x.k}),S.DEFAULT_SPAN,"
                "S.spanDef('7d'),S.spanDef('nope')]))")
        self.assertEqual(r[0], ["30m", "1h", "6h", "12h", "24h", "7d", "30d"])
        self.assertEqual(r[1], "24h")
        self.assertEqual(r[2]["min"], 10080)
        self.assertEqual(r[3]["k"], "24h")


if __name__ == "__main__":
    unittest.main()
