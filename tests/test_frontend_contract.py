"""Static contract checks for web/ (no node needed, except one optional maths test)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
        # the built-in columns come first, then the providers opencode records (providers/harness.js)
        self.assertIn("['claude', 'cursor', 'opencode', 'openai'].concat(TokenBurn.harnessOrder", app)
        self.assertLess(html.index("providers/openai.js"), html.index("providers/harness.js"))
        self.assertLess(html.index("providers/harness.js"), html.index("js/app.js"))

    def test_columns_across_and_reorder(self):
        html, app, css = read("index.html"), read("js/app.js"), read("css/app.css")
        self.assertIn('id="perRow"', html)
        self.assertIn("MAX_PERROW = 6", app)
        self.assertIn("token-burn.perrow", app)
        self.assertIn("token-burn.order", app)
        self.assertIn("'dragstart'", app)
        self.assertIn("ev.altKey", app)
        self.assertIn(".drop-before", css)

    def test_opencode_files_and_title(self):
        html = read("index.html")
        self.assertIn('<script src="/web/js/providers/opencode.js"></script>', html)
        self.assertLess(html.index("providers/cursor.js"), html.index("providers/opencode.js"))
        self.assertLess(html.index("providers/opencode.js"), html.index("js/app.js"))
        self.assertIn("<title>Token Burn {{VERSION}}</title>", html)
        self.assertIn('<h1>Token Burn <span class="ver">{{VERSION}}</span></h1>', html)
        self.assertIn("Claude Code, Cursor, opencode or OpenAI Codex", html)
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
        for label in ("'Primary agent'", "'Subagents'", "'Cache hit rate'", "input-side tokens", "'Input : output'",
                      "Value and limits", "Limit forecast", "token-burn.fee."):
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
        self.assertIn("grid-row: span 10", css)
        self.assertIn("@media (min-width: 1101px)", css)
        self.assertIn("@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }", css)
        html = read("index.html")
        self.assertLess(html.index('id="col-claude"'), html.index('id="col-cursor"'))
        js = read("js/app.js")
        self.assertEqual(js.count("<div class=\"plat-head\">"), 1)
        self.assertEqual(js.count("'<section class=\"card\">"), 8)
        self.assertIn("<h3>Sessions</h3>", js)
        self.assertLess(js.index("<h3>Usage graph</h3>"), js.index("<h3>Sessions</h3>"))
        self.assertLess(js.index("<h3>Sessions</h3>"), js.index("<h3>Plan usage</h3>"))
        self.assertLess(js.index("<h3>Primary agent</h3>"), js.index("<h3>All sessions</h3>"))
        self.assertLess(js.index("<h3>All sessions</h3>"), js.index("<h3>Subagents</h3>"))

    def test_burn_header_animation(self):
        html = read("index.html")
        self.assertLess(html.index("css/app.css"), html.index("css/burn.css"))
        self.assertLess(html.index("js/app.js"), html.index("js/burn.js"))
        top = html[html.index('<header class="top">'):html.index("</header>")]
        self.assertLess(top.index('<h1>Token Burn <span class="ver">{{VERSION}}</span></h1>'), top.index('class="burn" aria-hidden="true"'))
        self.assertLess(top.index('class="burn" aria-hidden="true"'), top.index('class="note tagline"'))
        self.assertEqual(html.count('class="blk '), 5)
        for i in range(1, 6):  # each fireball has its own delay and a reduced-motion resting place
            self.assertIn('class="blk b%d"' % i, html)
            self.assertRegex(read("css/burn.css"), r"\.burn \.b%d \{ --d: " % i)
            self.assertRegex(read("css/burn.css"), r"\.burn \.b%d \{ --rp: " % i)
        self.assertIn('<h1>Token Burn <span class="ver">{{VERSION}}</span></h1>', html)
        css = read("css/burn.css")
        self.assertRegex(css, r"\.burn\s*\{[^}]*pointer-events:\s*none")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("animation: none", css)
        self.assertRegex(css, r"\.is-paused[^{]*\{[^}]*animation-play-state:\s*paused")
        self.assertRegex(css, r"@media \(max-width: 340px\)\s*\{[^}]*display:\s*none")
        allowed = {"translate", "scale", "opacity"}
        bodies = re.findall(r"@keyframes\s+[\w-]+\s*\{((?:[^{}]*\{[^{}]*\})+)\s*\}", css)
        self.assertGreaterEqual(len(bodies), 6)
        for body in bodies:
            for block in re.findall(r"\{([^{}]*)\}", body):
                for prop in re.findall(r"([\w-]+)\s*:", block):
                    self.assertIn(prop, allowed, "keyframe animates " + prop)
        for bad in ("url(", "@import", "http"):
            self.assertNotIn(bad, css)
        js = read("js/burn.js")
        for want in ("'use strict'", "visibilitychange", "document.hidden"):
            self.assertIn(want, js)
        for bad in ("requestAnimationFrame", "setInterval", "innerHTML", ".style."):
            self.assertNotIn(bad, js)
        self.assertNotRegex(html, r"\sstyle\s*=")

    def test_machines_ui_contract(self):
        html = read("index.html")
        self.assertLess(html.index("js/tables.js"), html.index("js/machines.js"))
        self.assertLess(html.index("js/machines.js"), html.index("js/app.js"))
        main_open = html.index('<main class="cols">')
        main_close = html.index("</main>")
        for marker in ('id="machines"', 'id="machines-box"', 'id="machines-table"', 'id="sync-notes"'):
            self.assertIn(marker, html)
        self.assertNotIn('id="machines', html[main_open:main_close])
        self.assertNotIn('id="sync-', html[main_open:main_close])
        self.assertLess(html.index('id="machines-box"'), main_open)
        self.assertRegex(html, r'<details id="machines-box"[^>]*\bhidden\b')
        self.assertRegex(html, r'id="machines"[^>]*\bhidden\b')
        app = read("js/app.js")
        self.assertIn("'token-burn.machines'", app)
        self.assertIn("localStorage.setItem(LS_MACH", app)
        self.assertIn("M.filterData", app)
        self.assertIn("is not filtered by computer", app)
        self.assertNotIn("is not filtered by computer", read("js/providers/cursor.js"))
        mjs = read("js/machines.js")
        self.assertNotIn("innerHTML", mjs)
        self.assertIn("filterData", mjs)
        self.assertIn("tokensByMachine", mjs)
        self.assertIsNone(re.search(r"\.style\.|insertAdjacentHTML|document\.write", mjs))
        css = read("css/app.css")
        self.assertIn(".btn.stale::after", css)
        self.assertIn(".machines-card", css)
        self.assertNotRegex(css, r"(?m)^[^/*\n]*\.cols[^{\n]*\.machines[^{\n]*\{")

    def test_sync_fixture_shape(self):
        d = json.loads((FIXTURES / "sample_usage_sync.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["machines"]), 2)
        self.assertEqual(sum(1 for m in d["machines"] if m["is_self"]), 1)
        for key in ("id", "name", "os", "last_seen", "state", "stale", "status", "platforms", "tokens_30d"):
            self.assertIn(key, d["machines"][0])
        self.assertEqual(len(d["sync"]["messages"]), 1)
        self.assertIn("status", d["sync"])
        ids = {m["id"] for m in d["machines"]}
        self.assertEqual(set(d["platforms"]["claude"]["machines"]), ids)
        # opencode has data on one computer only: the breakdown is still sent, with that one entry
        self.assertEqual(set(d["platforms"]["opencode"]["machines"]), {d["machines"][0]["id"]})
        self.assertNotIn("machines", d["platforms"]["cursor"])
        # per-machine breakdowns add up to the merged series
        for p in ("claude", "opencode"):
            tot = lambda ser: sum(sum(s["input"]) + sum(s["output"]) + sum(s["cache"]) for s in ser)
            parts = sum(tot(v["series"]) for v in d["platforms"][p]["machines"].values())
            self.assertEqual(parts, tot(d["platforms"][p]["series"]))

    @unittest.skipUnless(shutil.which("node"), "node not on PATH")
    def test_machines_filter_data(self):
        js = (
            "global.window={};"
            "eval(require('fs').readFileSync(process.argv[1],'utf8'));"
            "var M=window.TokenBurn.machines;"
            "var d=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));"
            "var ids=d.machines.map(function(m){return m.id;});"
            "var tot=function(ser){var n=0;ser.forEach(function(s){s.t.forEach(function(t,i){n+=s.input[i]+s.output[i]+s.cache[i];});});return n;};"
            "var one=M.filterData(d,[ids[1]]);"
            "var all=M.filterData(d,ids);"
            "var none=M.filterData({platforms:d.platforms},[ids[0]]);"
            "console.log(JSON.stringify({"
            "sameAll:all===d,sameNoKey:none.platforms===d.platforms,"
            "claudeOne:tot(one.platforms.claude.series),claudeB:tot(d.platforms.claude.machines[ids[1]].series),"
            "claudeAll:tot(d.platforms.claude.series),"
            "ocOne:one.platforms.opencode.series.length,ocOneRuns:one.platforms.opencode.subagent_runs.length,"
            "ocA:tot(M.filterData(d,[ids[0]]).platforms.opencode.series),ocAll:tot(d.platforms.opencode.series),"
            "legacy:(function(){var l=JSON.parse(JSON.stringify(d));delete l.platforms.opencode.machines;"
            "return [M.filterData(l,[ids[0]]).platforms.opencode.series.length>0,M.filterData(l,[ids[1]]).platforms.opencode.series.length>0];})(),"
            "cursorSame:one.platforms.cursor===d.platforms.cursor,"
            "runs:one.platforms.claude.subagent_runs.length,"
            "planKept:one.platforms.claude.plan===d.platforms.claude.plan,"
            "sorted:one.platforms.claude.series.map(function(s){return tot([s]);}),"
            "origUntouched:tot(d.platforms.claude.series)===tot(d.platforms.claude.series),"
            "sel:M.selected(d,{[ids[0]]:false,[ids[1]]:false}),sel1:M.selected(d,{[ids[0]]:false,bogus:false}),"
            "by:M.tokensByMachine(d,{startMs:Date.parse(d.platforms.claude.start),loIdx:0,endIdx:43199}),ids:ids"
            "}));"
        )
        out = subprocess.run(
            ["node", "-e", js, str(WEB / "js" / "machines.js"), str(FIXTURES / "sample_usage_sync.json")],
            capture_output=True, text=True, check=True).stdout
        r = json.loads(out)
        self.assertTrue(r["sameAll"])
        self.assertTrue(r["sameNoKey"])
        self.assertEqual(r["claudeOne"], r["claudeB"])
        self.assertLess(r["claudeOne"], r["claudeAll"])
        self.assertEqual(r["ocOne"], 0)          # the idle computer alone: no opencode usage, not the other's
        self.assertEqual(r["ocOneRuns"], 0)
        self.assertEqual(r["ocA"], r["ocAll"])
        self.assertEqual(r["legacy"], [True, True])   # no machines key at all: old installed-platform fallback
        self.assertTrue(r["cursorSame"])
        self.assertEqual(r["runs"], 2)
        self.assertTrue(r["planKept"])
        self.assertEqual(r["sorted"], sorted(r["sorted"], reverse=True))
        self.assertEqual(sorted(r["sel"]), sorted(r["ids"]))
        self.assertEqual(r["sel1"], [r["ids"][1]])
        self.assertEqual(r["by"][r["ids"][1]], r["claudeB"])      # absent from the opencode breakdown = zero


if __name__ == "__main__":
    unittest.main()
