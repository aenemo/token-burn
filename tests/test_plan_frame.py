"""Plan chart frame (S.planFrame in web/js/stats.js), run under node.

For the 30 min and 1 hour windows the Claude plan chart shows the whole current 5-hour session, from the
pool's reset point to its next reset. Longer windows keep the normal frame."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"
NOW = "2026-11-20T10:00:00Z"
PLAT = ("{start:'2026-11-01T00:00:00Z',plan:{pools:[{id:'five_hour',resets:'%s'}],"
        "samples:[{t:'2026-11-20T08:30:00Z',five_hour:20},{t:'2026-11-20T09:55:00Z',five_hour:30}]}}")


def run(win, resets="2026-11-20T12:00:00Z", pool="five_hour"):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.TokenBurn.stats;var P=%s;var fr=S.frame(P,'%s',Date.parse('%s'));"
          "var pf=S.planFrame(P,fr,%s);"
          "console.log(JSON.stringify({session:!!pf.session,lo:pf.xMin,hi:pf.hiMs,fr:[fr.xMin,fr.hiMs],"
          "pts:S.planStats(P,pf).pools[0].points.length}))"
          % (PLAT % resets, win, NOW, json.dumps(pool)))
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def ms(iso):
    import datetime
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class PlanFrame(unittest.TestCase):
    def test_short_windows_show_whole_session(self):
        for win in ("30m", "1h"):
            r = run(win)
            self.assertTrue(r["session"], win)
            self.assertEqual(r["lo"], ms("2026-11-20T07:00:00Z"))   # reset 12:00 minus 5 hours
            self.assertEqual(r["hi"], ms("2026-11-20T12:00:00Z"))
            self.assertEqual(r["pts"], 2)                           # both samples, not only the last 30 min

    def test_long_windows_unchanged(self):
        r = run("6h")
        self.assertFalse(r["session"])
        self.assertEqual([r["lo"], r["hi"]], r["fr"])

    def test_no_usable_reset_keeps_window(self):
        for resets in ("not a time", "2026-11-20T09:00:00Z"):     # unparseable, or already past
            r = run("30m", resets)
            self.assertFalse(r["session"], resets)
            self.assertEqual([r["lo"], r["hi"]], r["fr"])

    def test_provider_without_session_pool(self):
        self.assertFalse(run("30m", pool=None)["session"])


if __name__ == "__main__":
    unittest.main()
