"""Cache read totals and hit rate (stats.js), run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"
START = "2026-01-01T00:00:00Z"


def run(series):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.CCU.stats;var P={start:'%s',series:[%s]};"
          "var fr=S.frame(P,'30m',Date.parse('%s')+10*60000+5000);"
          "var t=S.totals(S.seriesStats(P,fr));"
          "console.log(JSON.stringify({t:t,h:S.cacheHitRate(t)}));" % (START, series, START))
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# cache = read + write. minute 2: read 60 of 100; minute 5: read 150 of 200. input 1+2.
WITH = ("{key:'m|',model:'m',t:[2,5,99],input:[1,2,9],output:[10,20,9],cache:[100,200,9],cost:[1,2,1],"
        "cache_read:[60,150,9],sub_input:[0,0,0],sub_cache:[10,20,0],sub_cache_read:[5,10,0]}")
ABSENT = "{key:'m|',model:'m',t:[2],input:[1],output:[1],cache:[100],cost:[1]}"


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class SummaryTiles(unittest.TestCase):
    def test_cache_read_totals(self):
        r = run(WITH)
        self.assertEqual(r["t"]["cacheRead"], 210)
        self.assertEqual(r["t"]["subCacheRead"], 15)
        self.assertTrue(r["t"]["hasCacheRead"])

    def test_hit_rate(self):
        r = run(WITH)
        # 210 / (input 3 + cache 300)
        self.assertAlmostEqual(r["h"], 210 / 303)

    def test_absent_arrays(self):
        r = run(ABSENT)
        self.assertFalse(r["t"]["hasCacheRead"])
        self.assertEqual(r["t"]["cacheRead"], 0)
        self.assertIsNone(r["h"])

    def test_zero_denominator(self):
        r = run("{key:'m|',model:'m',t:[2],input:[0],output:[1],cache:[0],cost:[1],cache_read:[0]}")
        self.assertIsNone(r["h"])

    def test_mixed_payload_hides_cache_tile(self):
        r = run(WITH + "," + ABSENT.replace("'m|'", "'n|'").replace("model:'m'", "model:'n'"))
        self.assertFalse(r["t"]["hasCacheRead"])
        self.assertIsNone(r["h"])


def supported(series):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.CCU.stats;console.log(JSON.stringify(S.cacheSupported({series:[%s]})));" % series)
    return subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout.strip() == "true"


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class CacheSupported(unittest.TestCase):
    def test_empty_frame_with_supporting_data_shown(self):
        # series outside the window: frame rows empty, but support comes from the series list
        self.assertTrue(supported(WITH))
        r = run(WITH.replace("t:[2,5,99]", "t:[500,501,502]"))
        self.assertEqual(r["t"]["cacheRead"], 0)
        self.assertIsNone(r["h"])

    def test_no_series_shown(self):
        self.assertTrue(supported(""))

    def test_old_payload_hidden(self):
        self.assertFalse(supported(ABSENT))

    def test_mixed_hidden(self):
        self.assertFalse(supported(WITH + "," + ABSENT))

    def test_app_uses_supported_and_empty_text(self):
        src = (STATS.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("S.cacheSupported(plat)", src)
        self.assertIn("no usage in window", src)


def pct(f):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "console.log(window.CCU.stats.pctText(%s));" % f)
    return subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class PctText(unittest.TestCase):
    def test_never_overstates(self):
        self.assertEqual(pct("0.9996"), "99.9%")
        self.assertEqual(pct("0.99999"), "99.9%")
        self.assertEqual(pct("1"), "100%")

    def test_zero_and_tiny(self):
        self.assertEqual(pct("0"), "0%")
        self.assertEqual(pct("0.0001"), "<0.1%")

    def test_negative_clamp_and_normal(self):
        self.assertEqual(pct("-1e-12"), "0%")
        self.assertEqual(pct("0.728"), "72.8%")
        self.assertEqual(pct("0.94"), "94%")


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class AppUsesHelper(unittest.TestCase):
    def test_app_clamps_and_uses_pcttext(self):
        src = (STATS.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("S.pctText(", src)
        self.assertIn("clamp0(tot.cost - tot.subCost)", src)
        self.assertNotIn("Math.round(hit * 1000)", src)


if __name__ == "__main__":
    unittest.main()
