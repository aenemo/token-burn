import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, providers, util
from collector.adapters import opencode
from collector.aggregate import build_platform_payload
from collector.plan import opencode_plan
from collector.store import Store

NOW = 1792000000.0
MS = int(NOW * 1000)

SCHEMA = """
CREATE TABLE session_v2(id TEXT PRIMARY KEY, parent_id TEXT, agent TEXT, model TEXT,
  title TEXT, cost REAL DEFAULT 0, tokens_input INT DEFAULT 0, tokens_output INT DEFAULT 0);
CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
  seq INT NOT NULL, time_created INT NOT NULL, time_updated INT NOT NULL, data TEXT NOT NULL);
"""


def msg(created_s, tokens, typ="assistant", model=("acme-ai", "acme-code-deep", "default"),
        cost=0, completed=True):
    """Return (time_updated_ms, data) for a message created created_s seconds before NOW."""
    c = int(MS - created_s * 1000)
    data = {"time": {"created": c}, "text": "SECRET PROMPT",
            "model": {"id": model[1], "providerID": model[0], "variant": model[2]},
            "cost": cost, "tokens": {"input": tokens[0], "output": tokens[1], "reasoning": tokens[2],
                                     "cache": {"read": tokens[3], "write": tokens[4]}}}
    if completed and typ == "assistant":
        data["time"]["completed"] = c + 5000
    if completed and typ == "compaction":
        data["status"] = "completed"
    return c + 6000, c, data


def build_db(path, rows, sessions=(("s1", None, "build"), ("c1", "s1", "explore"))):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for sid, parent, agent in sessions:
        con.execute("INSERT INTO session_v2(id, parent_id, agent, title) VALUES(?,?,?,?)",
                    (sid, parent, agent, "private title"))
    for i, (sid, typ, (upd, created, data)) in enumerate(rows):
        con.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                    ("m%d" % i, sid, typ, i, created, upd, json.dumps(data)))
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "opencode.db"
        self.store = Store(os.path.join(self.tmp, "t.db"))
        p = mock.patch.dict(os.environ, {"TOKEN_BURN_OPENCODE_DB": str(self.db)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)

    def run_collect(self, now=NOW, **kw):
        return opencode.collect(self.store, now, None, **kw)

    def usage(self):
        return [dict(r) for r in self.store.usage_rows("opencode", 0)]


class MappingTests(Base):
    def setUp(self):
        super().setUp()
        build_db(self.db, [
            ("s1", "assistant", msg(600, (100, 20, 5, 300, 7), cost=0.25)),
            ("s1", "assistant", msg(500, (0, 0, 0, 0, 0))),
            ("s1", "compaction", msg(400, (50, 10, 2, 0, 0), typ="compaction",
                                     model=("opencode", "big-pickle", None))),
            ("c1", "assistant", msg(300, (10, 1, 0, 40, 0), model=("x", "m", "high"))),
            ("s1", "user", msg(200, (999, 999, 0, 0, 0), typ="user")),
        ])

    def test_mapping_and_skips(self):
        res = self.run_collect()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["meta"]["requests"], 3)
        rows = {(r["model"], r["effort"], r["agent_kind"]): r for r in self.usage()}
        self.assertEqual(set(rows), {("acme-ai/acme-code-deep", "", "primary"),
                                     ("opencode/big-pickle", "", "primary"),
                                     ("x/m", "high", "subagent")})
        a = rows[("acme-ai/acme-code-deep", "", "primary")]
        self.assertEqual((a["input"], a["output"], a["cache_read"], a["cache_write"], a["requests"]),
                         (100, 25, 300, 7, 1))
        self.assertAlmostEqual(a["cost"], 0.25)
        comp = rows[("opencode/big-pickle", "", "primary")]
        self.assertEqual((comp["input"], comp["output"]), (50, 12))
        self.assertEqual(rows[("x/m", "high", "subagent")]["agent_id"], "c1")

    def test_timestamp_uses_completed(self):
        self.run_collect()
        r = [r for r in self.usage() if r["model"] == "acme-ai/acme-code-deep"][0]
        self.assertEqual(r["minute"], int((MS - 600 * 1000 + 5000) / 1000 // 60))

    def test_subagent_run(self):
        self.run_collect()
        runs = self.store.subagent_runs("opencode", 0)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual((r["agent_id"], r["agent_type"], r["session_id"], r["model"], r["effort"]),
                         ("c1", "explore", "s1", "x/m", "high"))
        self.assertNotIn("private", r["agent_name"])
        self.assertEqual(r["agent_name"], "explore · c1")  # session id shorter than 6 characters

    def test_idempotent_rerun_and_backfill(self):
        self.run_collect()
        before = self.usage()
        self.run_collect(now=NOW + 60)
        self.run_collect(now=NOW + 120, backfill=True)
        self.assertEqual(self.usage(), before)

    def test_incremental_picks_up_new_rows(self):
        self.run_collect()
        con = sqlite3.connect(str(self.db))
        upd, created, data = msg(-30, (7, 3, 0, 0, 0))
        con.execute("INSERT INTO session_message VALUES('new','s1','assistant',99,?,?,?)",
                    (created, upd, json.dumps(data)))
        con.commit()
        con.close()
        res = self.run_collect(now=NOW + 60)
        self.assertEqual(res["meta"]["requests"], 1)
        self.assertEqual(sum(r["requests"] for r in self.usage()), 4)

    def _insert(self, mid, sess, typ, seq, m):
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT OR REPLACE INTO session_message VALUES(?,?,?,?,?,?,?)",
                    (mid, sess, typ, seq, m[1], m[0], json.dumps(m[2])))
        con.commit()
        con.close()

    def test_incomplete_recent_deferred_then_counted_with_final_tokens(self):
        self._insert("fly", "s1", "assistant", 98, msg(10, (5, 5, 0, 0, 0), completed=False))
        self.assertEqual(self.run_collect()["meta"]["requests"], 3)
        self.assertEqual(self.run_collect(now=NOW + 1200)["meta"]["requests"], 0)
        self._insert("fly", "s1", "assistant", 98, msg(10, (50, 60, 0, 0, 0)))
        self.assertEqual(self.run_collect(now=NOW + 1300)["meta"]["requests"], 1)
        fly = [r for r in self.usage() if r["output"] == 60]
        self.assertEqual(len(fly), 1)
        self.assertEqual(fly[0]["output"], 60)
        self.assertEqual(sum(r["requests"] for r in self.usage()), 4)

    def test_incomplete_compaction_deferred(self):
        self._insert("cmp", "s1", "compaction", 96, msg(10, (9, 9, 0, 0, 0), typ="compaction", completed=False))
        self.assertEqual(self.run_collect()["meta"]["requests"], 3)

    def test_stale_incomplete_counted_once(self):
        self._insert("old", "s1", "assistant", 95, msg(26 * 3600, (7, 8, 0, 0, 0), completed=False))
        self.assertEqual(self.run_collect()["meta"]["requests"], 4)
        self.assertEqual(self.run_collect(now=NOW + 60)["meta"]["requests"], 0)
        self.assertEqual(sum(r["requests"] for r in self.usage()), 4)

    def test_malformed_json_row_skipped(self):
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT INTO session_message VALUES('bad','s1','assistant',94,?,?,?)",
                    (MS - 1000, MS - 900, "{not json"))
        con.commit()
        con.close()
        res = self.run_collect()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["meta"]["requests"], 3)

    def test_subagent_name_uses_last_six_characters(self):
        self.assertEqual(opencode.agent_name("explore", "ses_abcdef123456"), "explore · 123456")

    def test_store_errors_propagate(self):
        with mock.patch.object(self.store, "seen_add", side_effect=sqlite3.OperationalError("database is locked")):
            res = self.run_collect()
        self.assertNotEqual(res["status"], opencode.BUSY_STATUS)
        self.assertNotEqual(res["status"], "opencode database unreadable")

    def test_wal_db_not_modified(self):
        con = sqlite3.connect(str(self.db))
        con.execute("PRAGMA journal_mode=WAL")
        upd, created, data = msg(30, (3, 4, 0, 0, 0))
        con.execute("INSERT INTO session_message VALUES('wal','s1','assistant',90,?,?,?)",
                    (created, upd, json.dumps(data)))
        con.commit()  # leave the writer open so the WAL stays live
        try:
            before = self.db.read_bytes()
            res = self.run_collect()
            self.assertEqual(self.db.read_bytes(), before)
        finally:
            con.close()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["meta"]["requests"], 4)

    def test_backfill_window(self):
        old = msg(31 * 86400, (1, 1, 0, 0, 0))
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT INTO session_message VALUES('old','s1','assistant',97,?,?,?)",
                    (old[1], old[0], json.dumps(old[2])))
        con.commit()
        con.close()
        self.run_collect(backfill=True)
        self.assertEqual(sum(r["requests"] for r in self.usage()), 3)

    def test_read_only(self):
        before = self.db.read_bytes()
        self.run_collect()
        self.run_collect(now=NOW + 60, backfill=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual([p.name for p in Path(self.tmp).glob("opencode.db*")], ["opencode.db"])


class StatusTests(Base):
    def test_missing_db(self):
        res = self.run_collect()
        self.assertEqual(res["status"], "opencode database not found")
        self.assertEqual(self.usage(), [])
        self.assertFalse(collect.opencode_installed())

    def test_unsupported_layout(self):
        con = sqlite3.connect(str(self.db))
        con.execute("CREATE TABLE message(id TEXT, data TEXT)")
        con.commit()
        con.close()
        self.assertTrue(collect.opencode_installed())
        res = self.run_collect()
        self.assertEqual(res["status"], "unsupported opencode data format")
        self.assertEqual(self.usage(), [])

    def test_locked_db(self):
        build_db(self.db, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0)))])
        lock = sqlite3.connect(str(self.db), isolation_level=None)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(opencode, "connect", lambda p: sqlite3.connect(
                    p.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)):
                res = self.run_collect()
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        self.assertEqual(res["status"], opencode.BUSY_STATUS)
        self.assertEqual(self.usage(), [])

    def test_util_path(self):
        with mock.patch.dict(os.environ, {"TOKEN_BURN_OPENCODE_DB": ""}), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x"}):
            self.assertEqual(util.opencode_db(), Path("/x") / "opencode" / "opencode.db")

    def test_plan_fetch(self):
        self.assertEqual(opencode_plan.fetch(None), ("no plan data", None))


class PayloadTests(Base):
    def test_payload_arrays_and_meta(self):
        build_db(self.db, [
            ("s1", "assistant", msg(600, (100, 20, 5, 300, 7))),
            ("c1", "assistant", msg(300, (10, 1, 0, 40, 2))),
        ])
        self.run_collect()
        p = build_platform_payload(self.store, "opencode", NOW, {"status": "ok"})
        self.assertEqual(p["status"], "ok")
        self.assertIs(p["meta"]["cost_reported"], False)
        self.assertIs(p["meta"]["subagent_tokens_attributed"], True)
        s = p["series"][0]
        self.assertEqual(s["key"], "acme-ai/acme-code-deep")
        self.assertEqual(sum(s["input"]), 110)
        self.assertEqual(sum(s["output"]), 26)
        self.assertEqual(sum(s["cache"]), 349)
        self.assertEqual(sum(s["cache_read"]), 340)
        self.assertEqual(sum(s["sub_input"]), 10)
        self.assertEqual(sum(s["sub_cache"]), 42)
        self.assertEqual(sum(s["sub_cache_read"]), 40)
        self.assertEqual(sum(s["requests"]), 2)
        run = p["subagent_runs"][0]
        self.assertEqual((run["agent_id"], run["input"], run["output"], run["cache"]),
                         ("c1", 10, 1, 42))

    def test_cost_reported_true(self):
        build_db(self.db, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0), cost=0.5))])
        self.run_collect()
        p = build_platform_payload(self.store, "opencode", NOW, {"status": "ok"})
        self.assertIs(p["meta"]["cost_reported"], True)
        self.assertAlmostEqual(sum(p["series"][0]["cost"]), 0.5)

    def test_cost_reported_false_when_mixed(self):
        build_db(self.db, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0), cost=0.5)),
                           ("s1", "assistant", msg(120, (2, 2, 0, 0, 0), cost=0))])
        self.run_collect()
        p = build_platform_payload(self.store, "opencode", NOW, {"status": "ok"})
        self.assertIs(p["meta"]["cost_reported"], False)

    def test_cost_reported_true_when_empty(self):
        build_db(self.db, [])
        self.run_collect()
        p = build_platform_payload(self.store, "opencode", NOW, {"status": "ok"})
        self.assertIs(p["meta"]["cost_reported"], True)

    def test_claude_and_cursor_meta_unchanged(self):
        p = build_platform_payload(self.store, "cursor", NOW, {"status": "ok"})
        self.assertNotIn("cost_reported", p["meta"])

    def test_collect_run_wires_opencode(self):
        build_db(self.db, [("s1", "assistant", msg(60, (1, 1, 0, 0, 0)))])
        names = [e[0] for e in collect._platforms()]
        self.assertEqual(names, ["claude", "cursor", "opencode", "openai"] + list(providers.PLATFORMS))
        entry = [e for e in collect._platforms() if e[0] == "opencode"]
        d = Path(self.tmp) / "d"
        with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"),                 mock.patch.object(util, "USAGE_JSON", d / "usage.json"):
            usage = collect.run(now=NOW, platforms=entry, log=mock.Mock())
        p = usage["platforms"]["opencode"]
        self.assertTrue(p["installed"])
        self.assertEqual(p["status"], "ok")
        self.assertEqual(p["plan"]["status"], "no plan data")
        self.assertEqual(p["plan"]["pools"], [])


if __name__ == "__main__":
    unittest.main()
