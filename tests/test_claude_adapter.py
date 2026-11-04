import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from collector import util
from collector.adapters import claude
from collector.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "claude"
LOG = logging.getLogger("test-claude")
LOG.addHandler(logging.NullHandler())


def iso(ts):
    return util.iso(ts)


def asst(ts, mid, rid, model="claude-opus-5-5", inp=10, out=20, cr=300, cw=40, sess="s1",
         effort=None, per_turn=None, sidechain=False, agent=None):
    o = {"type": "assistant", "timestamp": iso(ts), "sessionId": sess, "requestId": rid,
         "isSidechain": sidechain,
         "message": {"id": mid, "model": model,
                     "usage": {"input_tokens": inp, "output_tokens": out,
                               "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}
    if effort is not None:
        o["effort"] = effort
    if per_turn is not None:
        o["perTurnEffort"] = per_turn
    if agent:
        o["agentId"] = agent
    return json.dumps(o) + "\n"


def cost_state(sess, usage):
    return json.dumps({"type": "cost-state", "sessionId": sess,
                       "modelUsage": {m: {"costUSD": c} for m, c in usage.items()}}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cdir = self.tmp / "claude"
        self.proj = self.cdir / "projects" / "-proj"
        self.proj.mkdir(parents=True)
        self.store = Store(self.tmp / "t.db")
        self.addCleanup(self.store.close)
        self.hook_log = self.tmp / "claude-subagents.jsonl"
        for p in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cdir)}),
                  mock.patch.object(util, "CLAUDE_HOOK_LOG", self.hook_log)):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def run_collect(self, **kw):
        res = claude.collect(self.store, self.now, LOG, **kw)
        self.assertEqual(res["status"], "ok", res)
        return res

    def rows(self):
        return self.store.usage_rows("claude", 0)

    def total(self, col):
        return sum(r[col] for r in self.rows())

    def sub_file(self, sess, agent):
        d = self.proj / sess / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        return d / ("agent-%s.jsonl" % agent)


class TestAdapter(Base):
    def test_token_split_and_kind(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 100, "m1", "r1", inp=1, out=2, cr=3, cw=4))
        res = self.run_collect()
        self.assertEqual(res["meta"]["requests"], 1)
        self.assertEqual(res["meta"]["files"], 1)
        r = self.rows()[0]
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]), (1, 2, 3, 4))
        self.assertEqual(r["agent_kind"], "primary")
        self.assertEqual(r["agent_id"], "")
        self.assertEqual(r["model"], "claude-opus-5-5")

    def test_incremental_two_runs_and_partial_line(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 200, "m1", "r1"))
        self.run_collect()
        self.assertEqual(self.total("requests"), 1)
        off = self.store.kv_get("claude:offsets")[str(f)]
        self.assertEqual(off, f.stat().st_size)
        # second run: one full line plus a partial (no newline) line
        partial = asst(self.now - 100, "m3", "r3").rstrip("\n")
        with open(f, "a") as fh:
            fh.write(asst(self.now - 150, "m2", "r2") + partial)
        self.run_collect()
        self.assertEqual(self.total("requests"), 2)
        # the partial line completes on the third run
        with open(f, "a") as fh:
            fh.write("\n")
        self.run_collect()
        self.assertEqual(self.total("requests"), 3)

    def test_shrink_resets_offset_and_dedupe_protects(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 300, "m1", "r1") + asst(self.now - 200, "m2", "r2"))
        self.run_collect()
        self.assertEqual(self.total("requests"), 2)
        # rewrite smaller with one old (deduped) and one new line
        f.write_text(asst(self.now - 300, "m1", "r1"))
        self.assertLess(f.stat().st_size, self.store.kv_get("claude:offsets")[str(f)])
        self.run_collect()
        self.assertEqual(self.total("requests"), 2)
        f.write_text(asst(self.now - 59, "m1", "r1") + asst(self.now - 60, "m9", "r9"))
        self.run_collect()
        self.assertEqual(self.total("requests"), 3)

    def test_dedupe_same_message_across_lines_and_files(self):
        line = asst(self.now - 100, "m1", "r1")
        (self.proj / "s1.jsonl").write_text(line + line)
        (self.proj / "s2.jsonl").write_text(line)
        res = self.run_collect()
        self.assertEqual(res["meta"]["requests"], 1)
        self.assertEqual(self.total("requests"), 1)

    def test_streamed_message_output_growth_is_counted_once(self):
        # same message id: first copy has a partial output_tokens, later copies the final one
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 100, "m1", "r1", out=5)
                     + asst(self.now - 99, "m1", "r1", out=300)
                     + asst(self.now - 98, "m1", "r1", out=300))
        res = self.run_collect()
        self.assertEqual(self.total("output"), 300)
        self.assertEqual(self.total("input"), 10)
        self.assertEqual(self.total("cache_read"), 300)
        self.assertEqual(self.total("requests"), 1)
        self.assertEqual(res["meta"]["requests"], 1)
        self.assertEqual(self.store.session_tokens("claude")[("s1", "claude-opus-5-5")], 10 + 300 + 300 + 40)
        # growth arriving in a later run (after the first was already stored) adds only the difference
        with open(f, "a") as h:
            h.write(asst(self.now - 97, "m1", "r1", out=450))
        self.run_collect()
        self.assertEqual(self.total("output"), 450)
        self.assertEqual(self.total("requests"), 1)
        self.run_collect(backfill=True)
        self.assertEqual(self.total("output"), 450)

    def test_backfill_resets_offsets_without_double_count(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 100, "m1", "r1"))
        self.run_collect()
        self.store.kv_set("claude:offsets", {str(f): 5})
        self.run_collect(backfill=True)
        self.assertEqual(self.total("requests"), 1)
        self.assertEqual(self.store.kv_get("claude:offsets")[str(f)], f.stat().st_size)

    def test_old_files_and_old_lines_skipped(self):
        old = self.proj / "old.jsonl"
        old.write_text(asst(self.now - 40 * 86400, "m1", "r1"))
        t = self.now - 40 * 86400
        os.utime(old, (t, t))
        (self.proj / "new.jsonl").write_text(asst(self.now - 31 * 86400, "m2", "r2")
                                             + asst(self.now - 60, "m3", "r3"))
        res = self.run_collect()
        self.assertEqual(res["meta"]["files"], 1)
        self.assertEqual(self.total("requests"), 1)

    def test_sidechain_in_main_file_vs_subagent_file(self):
        (self.proj / "s1.jsonl").write_text(
            asst(self.now - 100, "m1", "r1")
            + asst(self.now - 90, "m2", "r2", sidechain=True, agent="abc1234567"))
        self.sub_file("s1", "zzz9999999").write_text(
            asst(self.now - 80, "m3", "r3", sidechain=True, agent="ignored-by-stem", sess="s1"))
        self.run_collect()
        by = {(r["agent_kind"], r["agent_id"]): r["requests"] for r in self.rows()}
        self.assertEqual(by, {("primary", ""): 1, ("subagent", "abc1234567"): 1,
                              ("subagent", "zzz9999999"): 1})

    def test_subagent_file_session_default_from_dir(self):
        line = json.loads(asst(self.now - 80, "m3", "r3", sidechain=True))
        del line["sessionId"]
        self.sub_file("parent-sess", "aaa1111111").write_text(json.dumps(line) + "\n")
        self.run_collect()
        self.assertEqual(self.rows()[0]["session_id"], "parent-sess")

    def test_main_file_session_default_from_stem(self):
        line = json.loads(asst(self.now - 80, "m3", "r3"))
        del line["sessionId"]
        (self.proj / "stem-sess.jsonl").write_text(json.dumps(line) + "\n")
        self.run_collect()
        self.assertEqual(self.rows()[0]["session_id"], "stem-sess")

    def test_effort(self):
        (self.proj / "s1.jsonl").write_text(
            asst(self.now - 100, "m1", "r1", effort="xhigh")
            + asst(self.now - 100, "m2", "r2", effort="low", per_turn="Medium")
            + asst(self.now - 100, "m3", "r3"))
        self.run_collect()
        got = sorted(r["effort"] for r in self.rows())
        self.assertEqual(got, ["", "extra", "medium"])

    def test_auto_cost_labels(self):
        self.assertEqual(claude.label("default"), "Auto Cost")
        self.assertEqual(claude.label(" AUTO "), "Auto Cost")
        self.assertEqual(claude.label("<synthetic>"), "Auto Cost")
        self.assertEqual(claude.label(""), "Auto Cost")
        self.assertEqual(claude.label(None), "Auto Cost")
        self.assertEqual(claude.label("claude-haiku-5"), "claude-haiku-5")
        (self.proj / "s1.jsonl").write_text(
            asst(self.now - 100, "m1", "r1", model="<synthetic>")
            + asst(self.now - 100, "m2", "r2", model="default")
            + asst(self.now - 100, "m3", "r3", model="claude-haiku-5"))
        res = self.run_collect()
        self.assertEqual(res["meta"]["auto_label_requests"], 2)
        self.assertEqual({r["model"] for r in self.rows()}, {"Auto Cost", "claude-haiku-5"})

    def test_cost_state_last_wins_and_label_merge(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 100, "m1", "r1")
                     + cost_state("s1", {"claude-opus-5-5": 1.0, "default": 0.25, "auto": 0.25}))
        self.run_collect()
        c = self.store.session_costs("claude")
        self.assertAlmostEqual(c[("s1", "claude-opus-5-5")], 1.0)
        self.assertAlmostEqual(c[("s1", "Auto Cost")], 0.5)
        with open(f, "a") as fh:
            fh.write(cost_state("s1", {"claude-opus-5-5": 2.5}))
        self.run_collect()
        c = self.store.session_costs("claude")
        self.assertAlmostEqual(c[("s1", "claude-opus-5-5")], 2.5)

    def test_non_dict_and_bad_json_ignored(self):
        (self.proj / "s1.jsonl").write_text('"assistant"\n[1,"assistant"]\n{"assistant": broken\n'
                                            + asst(self.now - 100, "m1", "r1"))
        self.run_collect()
        self.assertEqual(self.total("requests"), 1)

    def test_uuid_fallback_dedupe(self):
        o = json.loads(asst(self.now - 100, "m1", "r1"))
        o["message"].pop("id")
        o.pop("requestId")
        o["uuid"] = "u-1"
        line = json.dumps(o) + "\n"
        (self.proj / "s1.jsonl").write_text(line + line)
        self.run_collect()
        self.assertEqual(self.total("requests"), 1)

    def test_subagent_runs_from_files_and_meta(self):
        shutil.copy(FIX / "subagent.meta.json", self.sub_file("s1", "a1b2c3d4e5").with_suffix(".meta.json"))
        self.sub_file("s1", "a1b2c3d4e5").write_text(
            asst(self.now - 300, "m1", "r1", model="claude-sonnet-5", effort="low", sidechain=True)
            + asst(self.now - 100, "m2", "r2", model="claude-sonnet-5", per_turn="high", sidechain=True))
        self.run_collect()
        runs = self.store.subagent_runs("claude", 0)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["agent_id"], "a1b2c3d4e5")
        self.assertEqual(r["agent_type"], "explore")
        self.assertEqual(r["agent_name"], "explore · a1b2c3d")
        self.assertEqual(r["model"], "claude-sonnet-5")
        self.assertEqual(r["effort"], "high")
        self.assertEqual(r["status"], "completed")
        self.assertAlmostEqual(r["t"], int(self.now - 100), delta=1)

    def test_subagent_without_meta_named_subagent(self):
        self.sub_file("s1", "ffff000011").write_text(
            asst(self.now - 100, "m1", "r1", sidechain=True))
        self.run_collect()
        r = self.store.subagent_runs("claude", 0)[0]
        self.assertEqual(r["agent_type"], "")
        self.assertEqual(r["agent_name"], "subagent · ffff000")

    def test_hook_log_join(self):
        aid = "a1b2c3d4e5"
        self.sub_file("s1", aid).write_text(
            asst(self.now - 100, "m1", "r1", model="claude-sonnet-5", effort="low", sidechain=True))
        hook_t = self.now - 50
        recs = [
            {"t": iso(hook_t), "session": "s1", "agent_type": "general-purpose", "agent_id": aid,
             "model": "claude-sonnet-5", "effort": "xhigh",
             "input": 11, "output": 22, "cache_read": 33, "cache_write": 44},
            # hook-only run, transcript gone
            {"t": iso(self.now - 500), "session": "s9", "agent_type": "plan", "agent_id": "gone1234567",
             "model": "claude-opus-5-5", "effort": "", "input": 5, "output": 6, "cache_read": 7, "cache_write": 8},
            {"t": iso(self.now - 500), "agent_id": None},  # ignored
            {"t": iso(self.now - 40 * 86400), "agent_id": "ancient", "model": "x"},  # too old
        ]
        self.hook_log.write_text("\n".join(json.dumps(x) for x in recs) + "\nnot json\n")
        self.run_collect()
        runs = {r["agent_id"]: r for r in self.store.subagent_runs("claude", 0)}
        self.assertEqual(set(runs), {aid, "gone1234567"})
        r = runs[aid]
        self.assertEqual(r["agent_type"], "general-purpose")
        self.assertEqual(r["agent_name"], "general-purpose · a1b2c3d")
        self.assertEqual(r["effort"], "extra")
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]), (11, 22, 33, 44))
        self.assertAlmostEqual(r["t"], int(hook_t), delta=1)
        g = runs["gone1234567"]
        self.assertEqual((g["agent_type"], g["session_id"], g["model"]), ("plan", "s9", "claude-opus-5-5"))
        # a second run is idempotent
        self.run_collect()
        self.assertEqual(len(self.store.subagent_runs("claude", 0)), 2)

    def test_missing_dir_is_ok(self):
        shutil.rmtree(self.cdir)
        res = self.run_collect()
        self.assertEqual(res["meta"], {"auto_label_requests": 0, "requests": 0, "files": 0})

    def test_fatal_error_returns_status(self):
        with mock.patch.object(claude, "discover", side_effect=RuntimeError("boom")):
            res = claude.collect(self.store, self.now, LOG)
        self.assertNotEqual(res["status"], "ok")
        self.assertIn("meta", res)


if __name__ == "__main__":
    unittest.main()
