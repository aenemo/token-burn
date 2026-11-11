import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, util
from collector.adapters import openai, opencode
from collector.plan import openai_plan
from collector.aggregate import build_platform_payload
from collector.store import Store
from tests.test_opencode_adapter import NOW, build_db, msg

WEB = Path(__file__).resolve().parent.parent / "web"
SECRET = "SECRET PROMPT TEXT"


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def token_line(ordinal, ago_s, inp, cached, out, total, reasoning=0):
    return json.dumps({"timestamp": iso(NOW - ago_s), "ordinal": ordinal, "type": "event_msg", "payload": {
        "type": "token_count", "message": SECRET,
        "info": {"total_token_usage": {"total_tokens": total},
                 "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                      "cache_write_input_tokens": 0, "output_tokens": out,
                                      "reasoning_output_tokens": reasoning, "total_tokens": inp + out}}}}) + "\n"


def context_line(model="gpt-6.1-terra", effort="medium"):
    return json.dumps({"timestamp": iso(NOW - 900), "type": "turn_context",
                       "payload": {"model": model, "effort": effort, "cwd": "C:/private"}}) + "\n"


def text_line():
    return json.dumps({"type": "response_item", "payload": {"type": "message", "content": SECRET}}) + "\n"


class CodexBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "codex"
        self.home.mkdir()
        self.oc = Path(self.tmp) / "opencode.db"
        self.store = Store(os.path.join(self.tmp, "t.db"))
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home), "TOKEN_BURN_OPENCODE_DB": str(self.oc)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)

    def state(self, threads, edges=()):
        con = sqlite3.connect(str(self.home / "state_5.sqlite"))
        con.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, name TEXT, title TEXT, first_user_message TEXT,"
                    " preview TEXT, model TEXT, reasoning_effort TEXT, updated_at INT, attached_thread_parent_id TEXT,"
                    " agent_role TEXT, agent_nickname TEXT)")
        con.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT)")
        for t in threads:
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", t)
        for e in edges:
            con.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", e)
        con.commit()
        con.close()

    def rollout(self, name, *lines):
        p = self.home / name
        p.write_text("".join(lines), encoding="utf8")
        return p

    def thread(self, tid, path, name="A named chat", title=SECRET, parent=None):
        return (tid, str(path), name, title, SECRET, SECRET, "gpt-6.1-terra", "medium", int(NOW - 60), parent,
                None, None)

    def run_collect(self, backfill=False):
        return openai.collect(self.store, NOW, mock.Mock(), backfill=backfill)


class CodexAdapterTests(CodexBase):
    def test_records_tokens_model_and_titles(self):
        p = self.rollout("r1.jsonl", text_line(), context_line(),
                         token_line(1, 800, 20944, 0, 40, 20984, 10),
                         token_line(2, 700, 21024, 20864, 50, 42008, 18),
                         token_line(3, 600, 21024, 20864, 50, 63068, 18))
        self.state([self.thread("t1", p)])
        res = self.run_collect()
        self.assertEqual(res["status"], "ok")
        rows = self.store.usage_rows("openai", 0)
        self.assertEqual(sum(r["requests"] for r in rows), 3)
        self.assertEqual({r["model"] for r in rows}, {"gpt-6.1-terra"})
        self.assertEqual({r["effort"] for r in rows}, {"medium"})
        # input excludes cached tokens, which are counted as cache reads; reasoning is already inside output
        self.assertEqual(sum(r["input"] for r in rows), 20944 + 160 + 160)
        self.assertEqual(sum(r["cache_read"] for r in rows), 20864 * 2)
        self.assertEqual(sum(r["output"] for r in rows), 140)
        self.assertEqual(sum(r["cost"] for r in rows), 0)
        self.assertEqual(self.store.session_titles("openai"), {"t1": "A named chat"})
        p2 = build_platform_payload(self.store, "openai", NOW, {"status": "ok"})
        self.assertIs(p2["meta"]["cost_reported"], False)

    def test_prompt_text_is_never_stored(self):
        p = self.rollout("r1.jsonl", text_line(), context_line(), token_line(1, 60, 10, 0, 5, 15))
        self.state([self.thread("t1", p, name=SECRET)])      # a name equal to the prompt is not a name
        self.run_collect()
        self.assertEqual(self.store.session_titles("openai"), {})
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("C:/private", dump)

    def test_repeated_totals_and_reruns_count_once(self):
        p = self.rollout("r1.jsonl", context_line(), token_line(1, 60, 10, 0, 5, 15), token_line(2, 59, 10, 0, 5, 15))
        self.state([self.thread("t1", p)])
        self.run_collect()
        self.run_collect()
        self.assertEqual(sum(r["requests"] for r in self.store.usage_rows("openai", 0)), 1)
        self.run_collect(backfill=True)          # a full re-read is still safe: seen keys block repeats
        self.assertEqual(sum(r["requests"] for r in self.store.usage_rows("openai", 0)), 1)

    def test_new_lines_are_read_and_a_partial_line_waits(self):
        p = self.rollout("r1.jsonl", context_line(), token_line(1, 120, 10, 0, 5, 15))
        self.state([self.thread("t1", p)])
        self.run_collect()
        full = token_line(2, 60, 20, 0, 6, 41)
        with open(str(p), "a", encoding="utf8") as f:
            f.write(full[:40])
        self.run_collect()
        self.assertEqual(sum(r["requests"] for r in self.store.usage_rows("openai", 0)), 1)
        with open(str(p), "a", encoding="utf8") as f:
            f.write(full[40:])
        self.run_collect()
        rows = self.store.usage_rows("openai", 0)
        self.assertEqual(sum(r["requests"] for r in rows), 2)
        self.assertEqual(sum(r["input"] for r in rows), 30)

    def test_old_usage_and_old_threads_are_skipped(self):
        p = self.rollout("r1.jsonl", context_line(), token_line(1, 40 * 86400, 10, 0, 5, 15))
        old = self.rollout("r2.jsonl", context_line(), token_line(1, 60, 10, 0, 5, 15))
        stale = self.thread("t2", old)
        stale = stale[:8] + (int(NOW - 40 * 86400),) + stale[9:]
        self.state([self.thread("t1", p), stale])
        self.run_collect()
        self.assertEqual(self.store.usage_rows("openai", 0), [])

    def test_subagent_threads(self):
        a = self.rollout("a.jsonl", context_line(), token_line(1, 60, 10, 0, 5, 15))
        b = self.rollout("b.jsonl", context_line(), token_line(1, 50, 7, 0, 3, 10))
        self.state([self.thread("main", a), self.thread("kid", b, name="")], edges=[("main", "kid", "done")])
        res = self.run_collect()
        self.assertEqual(res["meta"]["subagent_requests"], 1)
        payload = build_platform_payload(self.store, "openai", NOW, {"status": "ok"})
        self.assertEqual([r["agent_id"] for r in payload["subagent_runs"]], ["kid"])
        self.assertEqual(payload["subagent_runs"][0]["session_id"], "main")

    def test_missing_and_broken_sources_never_raise(self):
        self.assertEqual(self.run_collect(), {"status": "ok", "meta": {}})          # no Codex at all
        (self.home / "state_5.sqlite").write_bytes(b"not a database")
        self.assertIn("unreadable", self.run_collect()["status"])
        self.setUp_threads_with_missing_rollout()

    def setUp_threads_with_missing_rollout(self):
        (self.home / "state_5.sqlite").unlink()
        self.state([self.thread("t1", self.home / "gone.jsonl")])
        self.assertEqual(self.run_collect()["status"], "ok")

    def test_newest_state_database_wins(self):
        (self.home / "state_4.sqlite").write_bytes(b"")
        (self.home / "state_12.sqlite").write_bytes(b"")
        (self.home / "state_x.sqlite").write_bytes(b"")
        self.assertEqual(openai.state_db().name, "state_12.sqlite")


class InstalledTests(CodexBase):
    def test_installed_rules(self):
        self.assertFalse(openai.installed())
        build_db(self.oc, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0)))])
        self.assertFalse(openai.installed())                         # opencode without OpenAI requests
        os.remove(str(self.oc))
        build_db(self.oc, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0), model=("openai", "gpt-6.1-sol", "")))])
        self.assertTrue(openai.installed())
        os.remove(str(self.oc))
        self.state([])
        self.assertTrue(openai.installed())
        self.assertTrue(collect.openai_installed())


class OpencodeRoutingTests(CodexBase):
    def run_oc(self):
        return opencode.collect(self.store, NOW, mock.Mock())

    def test_openai_provider_rows_go_to_the_openai_platform(self):
        build_db(self.oc, [
            ("s1", "assistant", msg(120, (10, 5, 0, 100, 0), model=("openai", "gpt-6.1-sol", "high"))),
            ("s1", "assistant", msg(60, (3, 2, 0, 0, 0))),
            ("c1", "assistant", msg(30, (4, 1, 0, 0, 0), model=("OpenAI", "gpt-6.1-sol", "")))])
        self.run_oc()
        oa = self.store.usage_rows("openai", 0)
        oc = self.store.usage_rows("opencode", 0)
        self.assertEqual({r["model"] for r in oa}, {"gpt-6.1-sol"})
        self.assertEqual({r["model"] for r in oc}, {"acme-ai/acme-code-deep"})
        self.assertEqual(sum(r["input"] + r["output"] + r["cache_read"] for r in oa), 10 + 5 + 100 + 4 + 1)
        self.assertEqual(sum(r["input"] + r["output"] for r in oc), 5)
        self.assertEqual(self.store.session_titles("openai"), {"s1": "private title"})   # c1 folds into its parent
        runs = build_platform_payload(self.store, "openai", NOW, {"status": "ok"})["subagent_runs"]
        self.assertEqual([r["agent_id"] for r in runs], ["c1"])
        self.assertEqual(self.store.subagent_runs("opencode", 0), [])
        self.run_oc()                                                    # a second pass adds nothing
        self.assertEqual(len(self.store.usage_rows("openai", 0)), len(oa))

    def test_earlier_openai_rows_move_once_without_double_counting(self):
        build_db(self.oc, [
            ("s1", "assistant", msg(120, (10, 5, 0, 0, 0), model=("openai", "gpt-6.1-sol", ""))),
            ("s1", "assistant", msg(60, (3, 2, 0, 0, 0)))])
        # what an earlier version stored: everything under opencode, including the OpenAI request
        from collector.schema import UsageRecord
        self.store.add_usage(UsageRecord("opencode", "primary", "", "s1", "openai/gpt-6.1-sol", "", 10, 5, 0, 0,
                                         0.0, NOW - 120))
        self.store.add_usage(UsageRecord("opencode", "primary", "", "s1", "acme-ai/acme-code-deep", "", 3, 2, 0, 0,
                                         0.0, NOW - 60))
        self.store.seen_add("opencode", "oc|m0", int((NOW - 120) // 60))
        self.store.seen_add("opencode", "oc|m1", int((NOW - 60) // 60))
        self.store.kv_set(opencode.LAST_TS_KEY, int(NOW * 1000))
        self.store.commit()
        self.run_oc()
        self.assertEqual(sum(r["input"] + r["output"] for r in self.store.usage_rows("openai", 0)), 15)
        self.assertEqual({r["model"] for r in self.store.usage_rows("opencode", 0)}, {"acme-ai/acme-code-deep"})
        self.assertEqual(sum(r["input"] + r["output"] for r in self.store.usage_rows("opencode", 0)), 5)
        self.run_oc()
        self.assertEqual(sum(r["input"] + r["output"] for r in self.store.usage_rows("openai", 0)), 15)


def limits_line(ago_s, limits):
    import time
    return json.dumps({"timestamp": iso(time.time() - ago_s), "type": "event_msg", "payload": {
        "type": "token_count", "info": None, "rate_limits": limits}}) + "\n"


class PlanTests(CodexBase):
    BUSINESS = {"limit_id": "codex", "primary": None, "secondary": None, "plan_type": "business",
                "credits": {"has_credits": True, "unlimited": True, "balance": None}}

    def test_unlimited_plan_gives_info_and_no_pools(self):
        self.rollout("rollout-a.jsonl", limits_line(60, self.BUSINESS))
        status, pools = openai_plan.fetch()
        self.assertEqual(status, "ok")
        self.assertEqual(list(pools), ["_info"])
        self.assertEqual(pools["_info"]["plan_type"], "business")
        self.assertTrue(pools["_info"]["unlimited"])
        self.assertIsNone(pools["_info"]["balance"])

    def test_windows_and_balance(self):
        import time
        now = time.time()
        rl = {"primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": int(now + 3600)},
              "secondary": {"used_percent": 40, "window_minutes": 10080, "resets_in_seconds": 86400},
              "plan_type": "plus", "credits": {"has_credits": True, "unlimited": False, "balance": "41.5"}}
        self.rollout("rollout-a.jsonl", limits_line(60, rl))
        status, pools = openai_plan.fetch()
        self.assertEqual(pools["five_hour"]["pct"], 12.5)
        self.assertEqual(pools["seven_day"]["pct"], 40.0)
        self.assertIn("resets_at", pools["seven_day"])
        self.assertEqual(pools["_info"]["balance"], 41.5)

    def test_finished_window_is_ignored_and_newest_event_wins(self):
        import time
        old = {"primary": {"used_percent": 99, "window_minutes": 300, "resets_at": int(time.time() - 10)},
               "plan_type": "plus", "credits": None}
        newer = dict(self.BUSINESS)
        self.rollout("rollout-a.jsonl", limits_line(600, old), limits_line(60, newer))
        _, pools = openai_plan.fetch()
        self.assertEqual(list(pools), ["_info"])
        pools, info = openai_plan.parse(old, time.time())
        self.assertEqual(pools, {})

    def test_no_data_and_junk(self):
        self.assertEqual(openai_plan.fetch(), ("no plan data", None))
        self.rollout("rollout-a.jsonl", limits_line(60, {"primary": "x", "credits": 5}),
                     '{"type": "event_msg", "payload": {"type": "token_count", "rate_limits": \n')
        self.assertEqual(openai_plan.fetch(), ("no plan data", None))

    def test_plan_block_carries_info_and_sync_ignores_it(self):
        self.rollout("rollout-a.jsonl", limits_line(60, self.BUSINESS))
        status, pools = openai_plan.fetch()
        self.store.add_plan_sample("openai", NOW - 30, pools)
        self.store.commit()
        block = build_platform_payload(self.store, "openai", NOW, {"status": "ok", "plan": {"status": "ok"}})["plan"]
        self.assertEqual(block["pools"], [])
        self.assertEqual(block["info"]["plan_type"], "business")
        self.assertEqual(block["samples"], [{"t": block["samples"][0]["t"]}])


class SyncAndFrontendTests(unittest.TestCase):
    def test_sync_knows_the_platform(self):
        from collector.sync import merge, validate
        self.assertIn("openai", validate.PLATFORMS)
        self.assertIn("openai", merge.ADD_PLATFORMS)
        self.assertIn("openai", merge.TOKEN_PLATFORMS)

    def test_dashboard_column_toggle_and_provider(self):
        html = (WEB / "index.html").read_text(encoding="utf8")
        app = (WEB / "js" / "app.js").read_text(encoding="utf8")
        css = (WEB / "css" / "app.css").read_text(encoding="utf8")
        self.assertRegex(html, r'data-prov="openai" aria-pressed="true"')
        self.assertLess(html.index('id="col-opencode"'), html.index('id="col-openai"'))
        self.assertLess(html.index("providers/opencode.js"), html.index("providers/openai.js"))
        self.assertLess(html.index("providers/openai.js"), html.index("js/app.js"))
        self.assertLess(app.index("providers.opencode, $('col-opencode')"), app.index("providers.openai, $('col-openai')"))
        self.assertIn(".col-openai", css)
        self.assertIn("--openai-soft", css)
        self.assertIn("'openai'", (WEB / "js" / "machines.js").read_text(encoding="utf8"))
        oa = (WEB / "js" / "providers" / "openai.js").read_text(encoding="utf8")
        self.assertRegex(oa, r"id:\s*'openai'")
        self.assertIn("planUnavailable", oa)
        self.assertIn("planInfo: true", oa)
        self.assertIn("Credit balance", app)
        self.assertIn("ChatGPT web and desktop apps keep no token counts", oa)


if __name__ == "__main__":
    unittest.main()
