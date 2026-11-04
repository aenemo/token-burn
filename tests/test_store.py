import os
import tempfile
import unittest
from pathlib import Path

from collector.schema import SubagentRun, UsageRecord
from collector.store import Store


def rec(ts=6000.0, **kw):
    d = dict(platform="claude", agent_kind="primary", agent_id="", session_id="s1", model="m",
             effort_level="", input_tokens=1, output_tokens=2, cache_read_tokens=3,
             cache_write_tokens=4, cost_usd=0.0, timestamp=ts)
    d.update(kw)
    return UsageRecord(**d)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_wal_and_tables(self):
        mode = self.store.db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_total_tokens(self):
        self.assertEqual(rec().total_tokens, 10)

    def test_add_usage_sums_same_key(self):
        self.store.add_usage(rec(6000.0, cost_usd=0.5))
        self.store.add_usage(rec(6010.0, cost_usd=0.25))  # same minute (100)
        self.store.add_usage(rec(6070.0))                  # next minute
        rows = self.store.usage_rows("claude", 0)
        self.assertEqual([r["minute"] for r in rows], [100, 101])
        self.assertEqual((rows[0]["input"], rows[0]["output"], rows[0]["cache_read"],
                          rows[0]["cache_write"], rows[0]["requests"]), (2, 4, 6, 8, 2))
        self.assertAlmostEqual(rows[0]["cost"], 0.75)

    def test_distinct_keys_stay_separate(self):
        self.store.add_usage(rec(effort_level="high"))
        self.store.add_usage(rec(effort_level="low"))
        self.store.add_usage(rec(agent_kind="subagent", agent_id="a"))
        self.store.add_usage(rec(platform="cursor"))
        self.assertEqual(len(self.store.usage_rows("claude", 0)), 3)
        self.assertEqual(len(self.store.usage_rows("cursor", 0)), 1)

    def test_since_minute_filter(self):
        self.store.add_usage(rec(6000.0))
        self.store.add_usage(rec(6070.0))
        self.assertEqual(len(self.store.usage_rows("claude", 101)), 1)

    def test_session_tokens_accumulate(self):
        self.store.add_usage(rec(6000.0))
        self.store.add_usage(rec(9000.0))
        self.store.add_usage(rec(9000.0, model="other"))
        self.assertEqual(self.store.session_tokens("claude"),
                         {("s1", "m"): 20, ("s1", "other"): 10})
        self.assertEqual(self.store.session_tokens("cursor"), {})

    def test_seen_add(self):
        self.assertTrue(self.store.seen_add("claude", "k", 5))
        self.assertFalse(self.store.seen_add("claude", "k", 5))
        self.assertTrue(self.store.seen_add("cursor", "k", 5))

    def test_session_cost_last_wins(self):
        self.store.set_session_cost("claude", "s1", "m", 1.0)
        self.store.set_session_cost("claude", "s1", "m", 2.5)
        self.assertEqual(self.store.session_costs("claude"), {("s1", "m"): 2.5})

    def test_kv(self):
        self.assertEqual(self.store.kv_get("x", 7), 7)
        self.store.kv_set("x", {"a": [1, 2]})
        self.assertEqual(self.store.kv_get("x"), {"a": [1, 2]})

    def test_plan_samples(self):
        self.store.add_plan_sample("claude", 100.0, {"five_hour": {"pct": 1.0, "resets_at": None}})
        self.store.add_plan_sample("claude", 50.0, {"five_hour": {"pct": 0.5, "resets_at": "z"}})
        s = self.store.plan_samples("claude", 60.0)
        self.assertEqual(s, [(100.0, {"five_hour": {"pct": 1.0, "resets_at": None}})])
        self.assertEqual([t for t, _ in self.store.plan_samples("claude", 0)], [50.0, 100.0])

    def test_upsert_subagent_run_merges(self):
        self.store.upsert_subagent_run(SubagentRun("cursor", "a1", "explore", "explore · a1", "s",
                                                   "m", "", 100.0, status="started"))
        self.store.upsert_subagent_run(SubagentRun("cursor", "a1", "", "", "", "m2", "high", 50.0,
                                                   status="completed", input_tokens=7))
        self.store.upsert_subagent_run(SubagentRun("cursor", "a1", "", "", "", "", "", 40.0))
        (r,) = self.store.subagent_runs("cursor", 0)
        self.assertEqual((r["agent_type"], r["agent_name"], r["session_id"]),
                         ("explore", "explore · a1", "s"))
        self.assertEqual((r["model"], r["effort"], r["status"]), ("m2", "high", "completed"))
        self.assertEqual(r["t"], 100.0)
        self.assertEqual(r["input"], 7)
        self.assertEqual(self.store.subagent_runs("claude", 0), [])

    def test_prune(self):
        old_min, new_min = 100, 10000
        self.store.add_usage(rec(old_min * 60.0, session_id="old"))
        self.store.add_usage(rec(new_min * 60.0, session_id="new"))
        self.store.add_usage(rec(old_min * 60.0, session_id="mixed"))
        self.store.add_usage(rec(new_min * 60.0, session_id="mixed"))
        for s in ("old", "new", "mixed"):
            self.store.set_session_cost("claude", s, "m", 1.0)
        self.store.seen_add("claude", "old", old_min)
        self.store.seen_add("claude", "new", new_min)
        self.store.add_plan_sample("claude", old_min * 60.0, {"p": {"pct": 1, "resets_at": None}})
        self.store.add_plan_sample("claude", new_min * 60.0, {"p": {"pct": 1, "resets_at": None}})
        self.store.upsert_subagent_run(SubagentRun("claude", "ao", "", "", "", "", "", old_min * 60.0))
        self.store.upsert_subagent_run(SubagentRun("claude", "an", "", "", "", "", "", new_min * 60.0))
        self.store.prune(5000)
        self.assertEqual({r["minute"] for r in self.store.usage_rows("claude", 0)}, {new_min})
        self.assertEqual(set(self.store.session_tokens("claude")), {("new", "m"), ("mixed", "m")})
        # lifetime total of a surviving session keeps pruned tokens
        self.assertEqual(self.store.session_tokens("claude")[("mixed", "m")], 20)
        self.assertEqual(set(self.store.session_costs("claude")), {("new", "m"), ("mixed", "m")})
        self.assertTrue(self.store.seen_add("claude", "old", old_min))   # was pruned
        self.assertFalse(self.store.seen_add("claude", "new", new_min))
        self.assertEqual([t for t, _ in self.store.plan_samples("claude", 0)], [new_min * 60.0])
        self.assertEqual([r["agent_id"] for r in self.store.subagent_runs("claude", 0)], ["an"])

    def test_context_manager_commits(self):
        p = Path(self.tmp.name) / "cm.db"
        with Store(p) as s:
            s.add_usage(rec())
        with Store(p) as s:
            self.assertEqual(len(s.usage_rows("claude", 0)), 1)

    def test_context_manager_rolls_back_on_error(self):
        p = Path(self.tmp.name) / "rb.db"
        with self.assertRaises(RuntimeError):
            with Store(p) as s:
                s.add_usage(rec())
                raise RuntimeError("x")
        with Store(p) as s:
            self.assertEqual(len(s.usage_rows("claude", 0)), 0)


class SeenAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_seen_raise(self):
        st = Store(self.path)
        try:
            self.assertTrue(st.seen_add("claude", "k", 1, 5))
            self.assertFalse(st.seen_add("claude", "k", 1, 9))
            self.assertIsNone(st.seen_raise("claude", "k", 5))
            self.assertEqual(st.seen_raise("claude", "k", 8), 5)
            self.assertIsNone(st.seen_raise("claude", "k", 8))
            self.assertIsNone(st.seen_raise("claude", "missing", 8))
        finally:
            st.close()

    def test_plan_sample_if_absent(self):
        st = Store(self.path)
        try:
            self.assertTrue(st.add_plan_sample_if_absent("claude", 100.0, {"a": {"pct": 1.0}}))
            self.assertFalse(st.add_plan_sample_if_absent("claude", 100.0, {"a": {"pct": 2.0}}))
            self.assertEqual(st.plan_samples("claude", 0), [(100.0, {"a": {"pct": 1.0}})])
        finally:
            st.close()

    def test_old_seen_table_is_migrated_and_claude_data_reset(self):
        import sqlite3
        con = sqlite3.connect(str(self.path))
        con.executescript("""
            CREATE TABLE seen(platform TEXT, key TEXT, minute INT, PRIMARY KEY(platform, key));
            CREATE TABLE usage_minute(platform TEXT, minute INT, session_id TEXT, agent_kind TEXT, agent_id TEXT,
              model TEXT, effort TEXT, input INT, output INT, cache_read INT, cache_write INT, cost REAL,
              requests INT, PRIMARY KEY(platform, minute, session_id, agent_kind, agent_id, model, effort));
            INSERT INTO seen VALUES('claude','a',1); INSERT INTO seen VALUES('cursor','b',1);
            INSERT INTO usage_minute VALUES('claude',1,'s','primary','','m','',1,1,1,1,0,1);
            INSERT INTO usage_minute VALUES('cursor',1,'s','primary','','m','',1,1,1,1,0,1);
            CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO kv VALUES('claude:offsets','{"x":1}'); INSERT INTO kv VALUES('cursor:last_ts','5');
        """)
        con.commit()
        con.close()
        st = Store(self.path)
        try:
            self.assertEqual(len(st.usage_rows("claude", 0)), 0)
            self.assertEqual(len(st.usage_rows("cursor", 0)), 1)
            self.assertIsNone(st.kv_get("claude:offsets"))
            self.assertEqual(st.kv_get("cursor:last_ts"), 5)
            self.assertFalse(st.seen_add("cursor", "b", 1))
            self.assertTrue(st.seen_add("claude", "a", 1))
            st.commit()
        finally:
            st.close()
        st = Store(self.path)  # a second open must not reset again
        try:
            self.assertFalse(st.seen_add("claude", "a", 1))
        finally:
            st.close()


if __name__ == "__main__":
    unittest.main()
