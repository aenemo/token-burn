"""SQLite store (WAL) for per-minute usage, session totals, subagent runs and plan samples."""
import json
import os
import sqlite3
from pathlib import Path

from collector import util
from collector.schema import SubagentRun, UsageRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_minute(
  platform TEXT, minute INT, session_id TEXT, agent_kind TEXT, agent_id TEXT, model TEXT, effort TEXT,
  input INT, output INT, cache_read INT, cache_write INT, cost REAL, requests INT,
  PRIMARY KEY(platform, minute, session_id, agent_kind, agent_id, model, effort));
CREATE TABLE IF NOT EXISTS session_tokens(
  platform TEXT, session_id TEXT, model TEXT, tokens INT,
  PRIMARY KEY(platform, session_id, model));
CREATE TABLE IF NOT EXISTS session_cost(
  platform TEXT, session_id TEXT, model TEXT, cost REAL,
  PRIMARY KEY(platform, session_id, model));
CREATE TABLE IF NOT EXISTS subagent_runs(
  platform TEXT, agent_id TEXT, agent_type TEXT, agent_name TEXT, session_id TEXT, model TEXT,
  effort TEXT, t REAL, status TEXT, input INT, output INT, cache_read INT, cache_write INT,
  PRIMARY KEY(platform, agent_id));
CREATE TABLE IF NOT EXISTS plan_samples(
  platform TEXT, t REAL, pools TEXT, PRIMARY KEY(platform, t));
CREATE TABLE IF NOT EXISTS seen(
  platform TEXT, key TEXT, minute INT, val INT DEFAULT 0, PRIMARY KEY(platform, key));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_usage_minute ON usage_minute(platform, minute);
CREATE INDEX IF NOT EXISTS ix_seen_minute ON seen(minute);
"""


class Store:
    def __init__(self, path=None):
        path = Path(path) if path is not None else util.DB_PATH
        util.ensure_dir(path.parent)
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=30)
        if os.name == "posix":
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """seen.val (output tokens already counted) arrived after the first release.

        Claude transcripts repeat a streamed message with growing output_tokens, and the first
        copy undercounts. Rows counted before val existed cannot be corrected, so the Claude data
        (all derived from transcripts) is dropped once and rebuilt from the files on the next run.
        """
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(seen)")]
        if "val" in cols:
            return
        self.db.execute("ALTER TABLE seen ADD COLUMN val INT DEFAULT 0")
        for table in ("usage_minute", "seen", "session_tokens", "session_cost"):
            self.db.execute("DELETE FROM %s WHERE platform='claude'" % table)
        self.db.execute("DELETE FROM kv WHERE key='claude:offsets'")

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def commit(self):
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        self.close()
        return False

    # ---- usage
    def add_usage(self, rec: UsageRecord):
        minute = int(rec.timestamp // 60)
        self.db.execute(
            "INSERT INTO usage_minute(platform, minute, session_id, agent_kind, agent_id, model, effort,"
            " input, output, cache_read, cache_write, cost, requests) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(platform, minute, session_id, agent_kind, agent_id, model, effort) DO UPDATE SET"
            " input=input+excluded.input, output=output+excluded.output,"
            " cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write,"
            " cost=cost+excluded.cost, requests=requests+excluded.requests",
            (rec.platform, minute, rec.session_id, rec.agent_kind, rec.agent_id, rec.model,
             rec.effort_level, int(rec.input_tokens), int(rec.output_tokens),
             int(rec.cache_read_tokens), int(rec.cache_write_tokens), float(rec.cost_usd),
             int(rec.requests)))
        self.db.execute(
            "INSERT INTO session_tokens(platform, session_id, model, tokens) VALUES(?,?,?,?)"
            " ON CONFLICT(platform, session_id, model) DO UPDATE SET tokens=tokens+excluded.tokens",
            (rec.platform, rec.session_id, rec.model, int(rec.total_tokens)))

    def seen_add(self, platform, key, minute, val=0):
        cur = self.db.execute("INSERT OR IGNORE INTO seen(platform, key, minute, val) VALUES(?,?,?,?)",
                              (platform, key, int(minute), int(val)))
        return cur.rowcount == 1

    def seen_raise(self, platform, key, val):
        """Record a larger val for an already seen key. Returns the previous val, or None if val is not larger."""
        row = self.db.execute("SELECT val FROM seen WHERE platform=? AND key=?", (platform, key)).fetchone()
        prev = int(row["val"] or 0) if row else 0
        if row is None or int(val) <= prev:
            return None
        self.db.execute("UPDATE seen SET val=? WHERE platform=? AND key=?", (int(val), platform, key))
        return prev

    def set_session_cost(self, platform, session_id, model, cost):
        self.db.execute("INSERT OR REPLACE INTO session_cost(platform, session_id, model, cost)"
                        " VALUES(?,?,?,?)", (platform, session_id, model, float(cost)))

    # ---- subagent runs
    def upsert_subagent_run(self, run: SubagentRun):
        old = self.db.execute("SELECT * FROM subagent_runs WHERE platform=? AND agent_id=?",
                              (run.platform, run.agent_id)).fetchone()

        def pick_s(new, field):
            return new if new else (old[field] if old and old[field] else "")

        def pick_i(new, field):
            return int(new) if new else (int(old[field] or 0) if old else 0)

        t = max(float(run.t or 0), float(old["t"] or 0) if old else 0.0)
        self.db.execute(
            "INSERT OR REPLACE INTO subagent_runs(platform, agent_id, agent_type, agent_name, session_id,"
            " model, effort, t, status, input, output, cache_read, cache_write) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.platform, run.agent_id, pick_s(run.agent_type, "agent_type"),
             pick_s(run.agent_name, "agent_name"), pick_s(run.session_id, "session_id"),
             pick_s(run.model, "model"), pick_s(run.effort_level, "effort"), t,
             pick_s(run.status, "status"), pick_i(run.input_tokens, "input"),
             pick_i(run.output_tokens, "output"), pick_i(run.cache_read_tokens, "cache_read"),
             pick_i(run.cache_write_tokens, "cache_write")))

    # ---- plan samples
    def add_plan_sample(self, platform, t, pools):
        self.db.execute("INSERT OR REPLACE INTO plan_samples(platform, t, pools) VALUES(?,?,?)",
                        (platform, float(t), json.dumps(pools, separators=(",", ":"))))

    def add_plan_sample_if_absent(self, platform, t, pools):
        """Insert unless a sample with the same platform and t exists. Returns True if inserted."""
        cur = self.db.execute("INSERT OR IGNORE INTO plan_samples(platform, t, pools) VALUES(?,?,?)",
                              (platform, float(t), json.dumps(pools, separators=(",", ":"))))
        return cur.rowcount == 1

    # ---- kv
    def kv_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)",
                        (key, json.dumps(value, separators=(",", ":"))))

    # ---- reads
    def usage_rows(self, platform, since_minute):
        return self.db.execute("SELECT * FROM usage_minute WHERE platform=? AND minute>=?"
                               " ORDER BY minute", (platform, int(since_minute))).fetchall()

    def session_tokens(self, platform):
        return {(r["session_id"], r["model"]): r["tokens"] for r in self.db.execute(
            "SELECT session_id, model, tokens FROM session_tokens WHERE platform=?", (platform,))}

    def session_costs(self, platform):
        return {(r["session_id"], r["model"]): r["cost"] for r in self.db.execute(
            "SELECT session_id, model, cost FROM session_cost WHERE platform=?", (platform,))}

    def subagent_runs(self, platform, since_t):
        return self.db.execute("SELECT * FROM subagent_runs WHERE platform=? AND t>=? ORDER BY t",
                               (platform, float(since_t))).fetchall()

    def plan_samples(self, platform, since_t):
        out = []
        for r in self.db.execute("SELECT t, pools FROM plan_samples WHERE platform=? AND t>=?"
                                 " ORDER BY t", (platform, float(since_t))):
            try:
                pools = json.loads(r["pools"])
            except ValueError:
                continue
            if isinstance(pools, dict):
                out.append((r["t"], pools))
        return out

    # ---- maintenance
    def prune(self, cutoff_minute):
        """Drop rows older than cutoff_minute (callers pass 'now minus 30 days')."""
        cutoff_t = int(cutoff_minute) * 60
        c = int(cutoff_minute)
        self.db.execute("DELETE FROM usage_minute WHERE minute<?", (c,))
        self.db.execute("DELETE FROM seen WHERE minute<?", (c,))
        self.db.execute("DELETE FROM plan_samples WHERE t<?", (cutoff_t,))
        self.db.execute("DELETE FROM subagent_runs WHERE t<?", (cutoff_t,))
        for table in ("session_tokens", "session_cost"):
            self.db.execute(
                "DELETE FROM %s WHERE NOT EXISTS (SELECT 1 FROM usage_minute u WHERE"
                " u.platform=%s.platform AND u.session_id=%s.session_id)" % (table, table, table))
