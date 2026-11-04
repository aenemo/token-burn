"""Static contract checks for web/ (no node needed, except one optional maths test)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(rel):
    return (WEB / rel).read_text(encoding="utf-8")


class FrontendContract(unittest.TestCase):
    def test_filters_order_and_default(self):
        src = read("js/stats.js")
        block = src[src.index("var WINDOWS"):src.index("];", src.index("var WINDOWS"))]
        got = re.findall(r"k: '([^']+)', min: (\d+), label: '([^']+)'", block)
        self.assertEqual(
            [(k, int(m), l) for k, m, l in got],
            [("30m", 30, "30 min"), ("1h", 60, "1 hour"), ("6h", 360, "6 hours"),
             ("12h", 720, "12 hours"), ("24h", 1440, "24 hours"),
             ("7d", 10080, "7 days"), ("30d", 43200, "30 days")])
        self.assertIn("var DEFAULT_WINDOW = '12h';", src)

    def test_refresh_fallbacks(self):
        src = read("js/app.js")
        self.assertRegex(src, r"REFRESH_MS = 60000\b")
        self.assertRegex(src, r"PLAN_REFRESH_S = 600\b")
        self.assertIn("token_refresh_s", src)
        self.assertIn("plan_refresh_s", src)

    def test_column_order(self):
        html = read("index.html")
        self.assertLess(html.index('id="col-claude"'), html.index('id="col-cursor"'))
        self.assertLess(html.index('id="col-cursor"'), html.index('id="col-opencode"'))
        app = read("js/app.js")
        self.assertLess(app.index("providers.claude, $('col-claude')"),
                        app.index("providers.cursor, $('col-cursor')"))
        self.assertLess(app.index("providers.cursor, $('col-cursor')"),
                        app.index("providers.opencode, $('col-opencode')"))

    def test_opencode_files_and_title(self):
        html = read("index.html")
        self.assertIn('<script src="/web/js/providers/opencode.js"></script>', html)
        self.assertLess(html.index("providers/cursor.js"), html.index("providers/opencode.js"))
        self.assertLess(html.index("providers/opencode.js"), html.index("js/app.js"))
        self.assertIn("<title>Token Burn</title>", html)
        self.assertIn("<h1>Token Burn</h1>", html)
        self.assertIn("Claude Code, Cursor or opencode", html)
        self.assertIn("Turn on a provider above to see its usage.", html)
        oc = read("js/providers/opencode.js")
        self.assertRegex(oc, r"id:\s*'opencode'")
        self.assertRegex(oc, r"agentSplit:\s*true")
        self.assertRegex(oc, r"summaryTiles:\s*true")
        self.assertRegex(oc, r"cacheHitsTile:\s*true")
        self.assertRegex(oc, r"planUnavailable:\s*true")
        self.assertNotIn("resetMarkers", oc)
        self.assertIn("meta.cost_reported === false", oc)
        self.assertNotIn("planUnavailable", read("js/providers/claude.js"))
        self.assertNotIn("planUnavailable", read("js/providers/cursor.js"))

    def test_provider_toggles(self):
        html = read("index.html")
        for p in ("claude", "cursor", "opencode"):
            self.assertRegex(html, r'data-prov="%s" aria-pressed="true"' % p)
        app = read("js/app.js")
        self.assertIn("'token-burn.providers'", app)
        self.assertIn("localStorage.setItem(LS_PROV", app)
        self.assertIn("setProperty('--n'", app)
        self.assertIn("function applyToggles", app)
        self.assertIn("aria-disabled", app)
        self.assertIn("aria-describedby", app)
        self.assertRegex(app, r"function render\(\) \{\s*var n = applyToggles\(\);")
        css = read("css/app.css")
        self.assertIn("repeat(var(--n, 3), minmax(0, 1fr))", css)
        self.assertIn(".col-opencode", css)

    def test_no_inline_script_or_handlers(self):
        html = read("index.html")
        for m in re.finditer(r"<script\b([^>]*)>", html, re.I):
            self.assertIn("src=", m.group(1), "inline <script> found")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler")
        self.assertNotIn("javascript:", html.lower())

    def test_graph_mode_switch_and_span_picker(self):
        app = read("js/app.js")
        self.assertIn("'token-burn.graph.'", app)
        self.assertIn("'token-burn.span.'", app)
        self.assertIn("this.graph = 'cumulative'", app)
        self.assertIn("this.span = DEFAULT_SPAN", app)
        self.assertIn("S.DEFAULT_SPAN", app)
        self.assertIn("S.SPANS.forEach", app)
        self.assertIn("buildGraphControls", app)
        self.assertIn("Total over the last", app)
        self.assertIn("data-graph", app)
        self.assertIn("updateRolling", read("js/charts.js"))
        self.assertIn("isTotal", read("js/charts.js"))
        self.assertIn("updateRolling", app)

    def test_selected_series_callout(self):
        app = read("js/app.js")
        self.assertIn("'use-callout'", app)
        self.assertIn("this.q('.chart-box').appendChild(this.callout)", app)
        self.assertIn("prototype.updateCallout", app)
        body = app[app.index("prototype.updateCallout"):app.index("function tile(")]
        self.assertIn("sel != null", body)
        self.assertIn("box.hidden = true", body)
        self.assertIn("box.hidden = false", body)
        self.assertIn("' in the last '", body)
        self.assertIn("F.tokens", body)
        self.assertIn("F.usd", body)
        self.assertIn("rollingSeries", body)
        self.assertIn("this.updateCallout()", app[app.index("prototype.applyHighlight"):app.index("prototype.updateCallout")])
        self.assertIn("this.updateCallout()", app[app.index("prototype.render"):app.index("prototype.applyChipsOnly")])
        css = read("css/app.css")
        self.assertRegex(css, r"\.use-callout\s*\{[^}]*pointer-events:\s*none")
        self.assertIsNone(re.search(r"\.(onclick|onmouse\w+)\s*=|setAttribute\('on", app))

    def test_agents_split_switch(self):
        app = read("js/app.js")
        charts = read("js/charts.js")
        self.assertIn("token-burn.agents.", app)
        self.assertIn("agents-btns", app)
        self.assertIn("this.agents = 'all'", app)
        self.assertIn("borderDash", charts)
        self.assertRegex(read("js/providers/claude.js"), r"agentSplit:\s*true")
        self.assertNotRegex(read("js/providers/cursor.js"), r"agentSplit:\s*true")
        self.assertIsNone(re.search(r"on(click|change|load)\s*=", app))
        self.assertRegex(read("js/providers/claude.js"), r"summaryTiles:\s*true")
        self.assertNotIn("summaryTiles", read("js/providers/cursor.js"))
        self.assertRegex(read("js/providers/claude.js"), r"cacheHitsTile:\s*true")
        self.assertRegex(read("js/providers/cursor.js"), r"cacheHitsTile:\s*true")
        for label in ("'Primary agent'", "'Subagents'", "'Cache hits'", "input-side tokens"):
            self.assertIn(label, app)

    def test_vendored_chart_exists(self):
        self.assertTrue((WEB / "vendor" / "chart.umd.min.js").is_file())
        self.assertIn("/web/vendor/chart.umd.min.js", read("index.html"))

    @unittest.skipUnless(shutil.which("node"), "node not on PATH")
    def test_frame_maths(self):
        js = (
            "global.window={};"
            "eval(require('fs').readFileSync(process.argv[1],'utf8'));"
            "var S=window.TokenBurn.stats;"
            "var p={start:'2026-01-01T00:00:00Z'};"
            "var now=Date.parse('2026-01-01T01:00:30Z');"
            "console.log(JSON.stringify(S.frame(p,'30m',now)));"
        )
        out = subprocess.run(["node", "-e", js, str(WEB / "js" / "stats.js")],
                             capture_output=True, text=True, check=True).stdout
        f = json.loads(out)
        s = 1767225600000  # 2026-01-01T00:00:00Z
        self.assertEqual(f["endIdx"], 60)
        self.assertEqual(f["W"], 30)
        self.assertEqual(f["loIdx"], 31)
        self.assertEqual(f["loMs"], s + 31 * 60000)
        self.assertEqual(f["hiMs"], s + 61 * 60000)

    def test_columns_use_subgrid_alignment(self):
        css = read("css/app.css")
        self.assertIn("@supports (grid-template-rows: subgrid)", css)
        self.assertIn("grid-template-rows: subgrid", css)
        self.assertIn("grid-row: span 7", css)
        self.assertIn("@media (min-width: 1101px)", css)
        self.assertIn("@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }", css)
        html = read("index.html")
        self.assertLess(html.index('id="col-claude"'), html.index('id="col-cursor"'))
        js = read("js/app.js")
        self.assertEqual(js.count("<div class=\"plat-head\">"), 1)
        self.assertEqual(js.count("'<section class=\"card\">"), 5)


if __name__ == "__main__":
    unittest.main()
