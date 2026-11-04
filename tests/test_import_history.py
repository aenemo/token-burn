import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from collector import import_history as ih
from collector import util
from collector.store import Store

NOW = 1792554663.0  # 2026-10-21T03:51:03Z


def write(path, lines):
    Path(path).write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines) + "\n",
                          encoding="utf-8")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "t.db")
        self.out = []
        self.claude = self.dir / "plan_samples.jsonl"
        self.cursor = self.dir / "plan-percent-history.jsonl"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_import(self, claude=None, cursor=None):
        return ih.run(claude or self.claude, cursor or self.cursor, now=NOW, store=self.store,
                      out=self.out.append)

    def test_claude_import_normalises_and_skips_old_and_bad(self):
        write(self.claude, [
            {"t": "2026-10-20T06:46:01Z", "pools": {
                "five_hour": {"pct": 24.0, "resets_at": "2026-10-20T08:59:59.656235+00:00"},
                "seven_day": {"pct": 6.0, "resets_at": None}}},
            {"t": "2026-08-01T00:00:00Z", "pools": {"five_hour": {"pct": 1.0, "resets_at": None}}},  # too old
            "not json",
            {"t": "bad", "pools": {"five_hour": {"pct": 1.0}}},
            {"t": "2026-10-20T07:00:00Z", "pools": {"five_hour": {"pct": "x"}}},  # no usable pool
        ])
        res = self.run_import()
        self.assertEqual(res["claude"], {"read": 2, "added": 1, "duplicate": 0, "old": 1})
        s = self.store.plan_samples("claude", 0)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][0], util.parse_ts("2026-10-20T06:46:01Z"))
        self.assertEqual(s[0][1]["five_hour"], {"pct": 24.0, "resets_at": "2026-10-20T08:59:59Z"})
        self.assertEqual(s[0][1]["seven_day"], {"pct": 6.0, "resets_at": None})

    def test_cursor_import_reads_naive_time_as_local(self):
        write(self.cursor, [
            {"recordedAt": "2026-10-19T10:44:10", "bucket": "x", "cursorModelsPercent": 9.5, "otherModelsPercent": 0.0},
            {"recordedAt": "2026-10-19T10:48:01", "cursorModelsPercent": 10.0, "otherModelsPercent": None},
            {"recordedAt": "2026-10-19T10:50:00", "cursorModelsPercent": None, "otherModelsPercent": None},
            {"recordedAt": "2026-09-01T10:50:00", "cursorModelsPercent": 1.0, "otherModelsPercent": 1.0},
        ])
        res = self.run_import()
        self.assertEqual(res["cursor"], {"read": 3, "added": 2, "duplicate": 0, "old": 1})
        s = self.store.plan_samples("cursor", 0)
        want = dt.datetime(2026, 10, 19, 10, 44, 10).astimezone().timestamp()
        self.assertEqual(s[0][0], want)
        self.assertEqual(s[0][1], {"cursor_models": {"pct": 9.5, "resets_at": None},
                                   "other_models": {"pct": 0.0, "resets_at": None}})
        self.assertEqual(list(s[1][1]), ["cursor_models"])

    def test_idempotent_and_existing_sample_kept(self):
        write(self.claude, [{"t": "2026-10-20T06:46:01Z", "pools": {"five_hour": {"pct": 24.0}}}])
        write(self.cursor, [{"recordedAt": "2026-10-19T10:44:10", "cursorModelsPercent": 9.5}])
        t = util.parse_ts("2026-10-20T06:46:01Z")
        self.store.add_plan_sample("claude", t, {"five_hour": {"pct": 99.0, "resets_at": None}})
        first = self.run_import()
        self.assertEqual(first["claude"]["duplicate"], 1)
        self.assertEqual(first["cursor"]["added"], 1)
        second = self.run_import()
        self.assertEqual(second["cursor"], {"read": 1, "added": 0, "duplicate": 1, "old": 0})
        self.assertEqual(len(self.store.plan_samples("claude", 0)), 1)
        self.assertEqual(self.store.plan_samples("claude", 0)[0][1]["five_hour"]["pct"], 99.0)
        self.assertEqual(len(self.store.plan_samples("cursor", 0)), 1)

    def test_missing_files_are_skipped_with_a_message(self):
        res = self.run_import(self.dir / "nope1.jsonl", self.dir / "nope2.jsonl")
        self.assertEqual(res, {"claude": None, "cursor": None})
        self.assertEqual(len(self.out), 2)
        self.assertTrue(all("skipped" in m for m in self.out))

    def test_source_files_are_not_modified(self):
        write(self.claude, [{"t": "2026-10-20T06:46:01Z", "pools": {"five_hour": {"pct": 24.0}}}])
        before = (self.claude.read_bytes(), self.claude.stat().st_mtime_ns)
        self.run_import()
        self.assertEqual((self.claude.read_bytes(), self.claude.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
