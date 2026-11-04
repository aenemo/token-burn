"""Primary/subagent split (agentSplitRows) in web/js/stats.js, run under node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"
START = "2026-01-01T00:00:00Z"


def run(body):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.CCU.stats;var P={start:'%s'};"
          "var fr=S.frame(P,'30m',Date.parse('%s')+10*60000+5000);"
          "function mk(s){P.series=s;return S.seriesStats(P,fr);}%s" % (START, START, body))
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# minutes 2,5: totals 111/222 tokens, cost 1/2 ; subagent part 11 / 22 tokens, cost 0.25/0.5
SER_A = ("{key:'m|hi',model:'m',effort:'hi',t:[2,5],input:[1,2],output:[10,20],cache:[100,200],cost:[1,2],"
         "sub_input:[1,2],sub_output:[0,0],sub_cache:[10,20],sub_cost:[0.25,0.5]}")
SER_N = "{key:'n|',model:'n',t:[3],input:[5],output:[5],cache:[0],cost:[1]}"
SER_C = ("{key:'c|',model:'c',t:[4],input:[1],output:[1],cache:[1],cost:[1],"
         "sub_input:[5],sub_output:[0],sub_cache:[0],sub_cost:[3]}")


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class AgentSplit(unittest.TestCase):
    def split(self, series, metric="tokens"):
        return run("var rows=mk([%s]);console.log(JSON.stringify(S.agentSplitRows(rows,P,fr,'%s')))"
                   % (series, metric))

    def test_hand_computed(self):
        p, s = self.split(SER_A)
        self.assertEqual(p["tokens"], 333 - 33)
        self.assertEqual(s["tokens"], 33)
        self.assertAlmostEqual(p["cost"], 2.25)
        self.assertAlmostEqual(s["cost"], 0.75)
        self.assertEqual(p["series"]["input"], [0, 0])
        self.assertEqual(p["series"]["cache"], [90, 180])
        self.assertEqual(s["series"]["cache"], [10, 20])

    def test_sum_equals_base(self):
        r = run("var rows=mk([%s]);var d=S.agentSplitRows(rows,P,fr,'tokens');"
                "console.log(JSON.stringify([rows[0],d[0],d[1]]))" % SER_A)
        b, p, s = r
        self.assertEqual(p["tokens"] + s["tokens"], b["tokens"])
        self.assertAlmostEqual(p["cost"] + s["cost"], b["cost"])

    def test_no_sub_only_primary(self):
        rows = self.split(SER_N)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent"], "primary")
        self.assertEqual(rows[0]["tokens"], 10)

    def test_clamping(self):
        # primary input and cost clamp at 0 (output/cache remain, so the row is still emitted)
        p, s = self.split(SER_C)
        self.assertEqual(p["series"]["input"], [0])
        self.assertEqual(p["series"]["cost"], [0])
        self.assertEqual(s["series"]["input"], [5])

    def test_only_subagent_usage(self):
        s = "{key:'s|',model:'s',t:[1],input:[4],output:[0],cache:[0],cost:[2],sub_input:[4],sub_output:[0],sub_cache:[0],sub_cost:[2]}"
        for metric in ("tokens", "cost"):
            rows = self.split(s, metric)
            self.assertEqual([r["agent"] for r in rows], ["subagent"])

    def test_both_zero_yields_none(self):
        z = "{key:'z|',model:'z',t:[1],input:[0],output:[0],cache:[0],cost:[0]}"
        for metric in ("tokens", "cost"):
            self.assertEqual(self.split(z, metric), [])

    def test_keys_and_fields(self):
        p, s = self.split(SER_A)
        self.assertEqual(p["key"], "m|hi (primary)")
        self.assertEqual(s["key"], "m|hi (subagents)")
        self.assertEqual((p["baseKey"], s["baseKey"]), ("m|hi", "m|hi"))
        self.assertEqual((p["agent"], s["agent"]), ("primary", "subagent"))
        for r in (p, s):
            self.assertEqual(r["model"], "m")
            self.assertEqual(r["effort"], "hi")
            self.assertEqual(r["series"]["t"], [2, 5])
            self.assertEqual(r["subTokens"], 0)

    def test_order_preserved(self):
        r = run("var rows=mk([%s,%s]);console.log(JSON.stringify(S.agentSplitRows(rows,P,fr,'tokens').map(function(x){return x.key})))"
                % (SER_A, SER_N))
        self.assertEqual(r, ["m|hi (primary)", "m|hi (subagents)", "n| (primary)"])

    def test_cumulative_and_rolling(self):
        r = run("var rows=mk([%s]);var d=S.agentSplitRows(rows,P,fr,'tokens');var o=[];"
                "['tokens','cost'].forEach(function(m){"
                "var c=d.map(function(x){var q=S.cumulativeSeries(x.series,m,fr);return q[q.length-1].y});"
                "var g=d.map(function(x){var q=S.rollingSeries(x.series,m,fr,100,720);return q[q.length-1].y});"
                "var cb=S.cumulativeSeries(rows[0].series,m,fr),gb=S.rollingSeries(rows[0].series,m,fr,100,720);"
                "o.push([c,g,cb[cb.length-1].y,gb[gb.length-1].y]);});console.log(JSON.stringify(o))" % SER_A)
        for c, g, cb, gb in r:
            self.assertAlmostEqual(c[0] + c[1], cb)
            self.assertAlmostEqual(g[0] + g[1], gb)

    def test_metric_cost_omits_zero_cost_sub(self):
        s = "{key:'z|',model:'z',t:[1],input:[2],output:[0],cache:[0],cost:[1],sub_input:[1],sub_output:[0],sub_cache:[0],sub_cost:[0]}"
        self.assertEqual(len(self.split(s, "cost")), 1)
        self.assertEqual(len(self.split(s, "tokens")), 2)
        self.assertEqual(len(self.split(SER_A, "cost")), 2)

    def test_exports(self):
        r = run("console.log(JSON.stringify([Object.keys(S),S.AGENT_SUFFIX]))")
        for k in ["WINDOWS", "DEFAULT_WINDOW", "windowDef", "frame", "seriesStats", "totals", "cumulativeSeries",
                  "runsInWindow", "planStats", "SPANS", "DEFAULT_SPAN", "spanDef", "rollingSeries",
                  "rollingTotal", "rollingInfo", "agentSplitRows", "AGENT_SUFFIX"]:
            self.assertIn(k, r[0])
        self.assertEqual(r[1], {"primary": " (primary)", "subagent": " (subagents)"})


if __name__ == "__main__":
    unittest.main()
