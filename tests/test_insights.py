"""Input vs output ratio, value for money and limit forecast maths in web/js/stats.js, run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"
START = "2026-01-01T00:00:00Z"
H = 3600000


def run(body):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.TokenBurn.stats;var T0=Date.parse('%s');%s" % (START, body))
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class IoRatio(unittest.TestCase):
    def test_ratio_counts_cache_on_input_side(self):
        r = run("console.log(JSON.stringify(S.ioRatio({input:100,cache:900,output:250})))")
        self.assertEqual(r["inputSide"], 1000)
        self.assertEqual(r["output"], 250)
        self.assertAlmostEqual(r["ratio"], 4.0)

    def test_no_output_gives_null(self):
        self.assertIsNone(run("console.log(JSON.stringify(S.ioRatio({input:5,cache:0,output:0})))")["ratio"])

    def test_totals_sum_output(self):
        r = run("var P={start:'%s',series:[{key:'a',model:'a',t:[1,2],input:[1,2],output:[10,20],cache:[0,0],cost:[0,0]},"
                "{key:'b',model:'b',t:[3],input:[4],output:[5],cache:[6],cost:[0]}]};"
                "var fr=S.frame(P,'30m',T0+10*60000);console.log(JSON.stringify(S.totals(S.seriesStats(P,fr))))" % START)
        self.assertEqual(r["output"], 35)
        self.assertEqual(r["input"], 7)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class ValueStats(unittest.TestCase):
    def value(self, now_days, fee, ts="[60,120]", cost="[3,7]"):
        return run("var P={start:'%s',series:[{key:'a',model:'a',t:%s,input:[1,1],output:[1,1],cache:[0,0],cost:%s}]};"
                   "console.log(JSON.stringify(S.valueStats(P,T0+%d*86400000,%s)))" % (START, ts, cost, now_days, fee))

    def test_full_month_multiple(self):
        v = self.value(40, 5, ts="[%d,%d]" % (15 * 1440, 35 * 1440))  # both inside the last 30 days
        self.assertAlmostEqual(v["cost"], 10)
        self.assertAlmostEqual(v["days"], 30)
        self.assertAlmostEqual(v["feeForDays"], 5)
        self.assertAlmostEqual(v["multiple"], 2.0)

    def test_old_usage_outside_30_days_excluded(self):
        v = self.value(40, 5, ts="[60,%d]" % (35 * 1440))
        self.assertAlmostEqual(v["cost"], 7)

    def test_short_history_prorates_fee(self):
        v = self.value(10, 30)
        self.assertAlmostEqual(v["days"], 10)
        self.assertAlmostEqual(v["feeForDays"], 10)
        self.assertAlmostEqual(v["multiple"], 1.0)
        self.assertAlmostEqual(v["perMonth"], 30)

    def test_no_fee(self):
        for fee in ("null", "0", "-3", "NaN"):
            v = self.value(10, fee)
            self.assertIsNone(v["multiple"])
            self.assertIsNone(v["fee"])


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class Forecast(unittest.TestCase):
    def fc(self, points, cur, resets_h, lookback_h=1):
        now = 10 * H
        pts = ",".join("{x:%d,y:%s}" % (now - int(m * 60000), y) for m, y in points)
        res = "null" if resets_h is None else str(now + int(resets_h * H))
        return run("console.log(JSON.stringify(S.forecastPool([%s],%s,%s,%d,%d)))"
                   % (pts, cur, res, now, int(lookback_h * H)))

    def test_hits_limit_before_reset(self):
        # 10 pp in 30 min = 20 pp/h; from 60% the last 40 pp take 2 h, reset in 3 h
        f = self.fc([(30, 50), (0, 60)], 60, 3)
        self.assertEqual(f["state"], "full")
        self.assertAlmostEqual(f["fullMs"], 12 * H, delta=1)
        self.assertAlmostEqual(f["ratePerHour"], 20)

    def test_resets_first(self):
        f = self.fc([(30, 50), (0, 60)], 60, 1)
        self.assertEqual(f["state"], "reset_first")
        self.assertAlmostEqual(f["atResetPct"], 80)

    def test_at_limit(self):
        self.assertEqual(self.fc([], 100, 1)["state"], "at_limit")

    def test_idle_when_flat(self):
        self.assertEqual(self.fc([(40, 30), (0, 30)], 30, 2)["state"], "idle")

    def test_unknown_without_enough_span(self):
        self.assertEqual(self.fc([(5, 30), (0, 31)], 31, 2)["state"], "unknown")
        self.assertEqual(self.fc([(0, 31)], 31, 2)["state"], "unknown")

    def test_only_samples_since_last_reset(self):
        # dropped from 90 to 2 twenty-five minutes ago; rate from the 3 points after the drop: 8 pp in 25 min
        f = self.fc([(50, 80), (30, 90), (25, 2), (10, 6), (0, 10)], 10, 4)
        self.assertAlmostEqual(f["ratePerHour"], 8 / 25 * 60, places=5)

    def test_lookback_excludes_old_samples(self):
        f = self.fc([(120, 0), (30, 50), (0, 60)], 60, None)
        self.assertAlmostEqual(f["ratePerHour"], 20)
        self.assertEqual(f["state"], "full")

    def test_lookback_by_pool(self):
        r = run("console.log(JSON.stringify([S.forecastLookback('five_hour'),S.forecastLookback('seven_day')]))")
        self.assertEqual(r, [H, 24 * H])


if __name__ == "__main__":
    unittest.main()
