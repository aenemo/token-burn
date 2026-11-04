import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, util
from collector.schema import UsageRecord

NOW = 1_800_000_000.0
POOLS = {"five_hour": {"pct": 9.0, "resets_at": None}}


def make_collect(platform, boom=False):
    def fn(store, now, log, backfill=False):
        if boom:
            raise RuntimeError("secret-token-xyz")
        store.add_usage(UsageRecord(platform=platform, agent_kind="primary", agent_id="", session_id="s",
                                    model="m", effort_level="high", input_tokens=1, output_tokens=2,
                                    cache_read_tokens=3, cache_write_tokens=4, cost_usd=0.5,
                                    timestamp=now - 120))
        return {"status": "ok", "meta": {"backfill": backfill}}
    return fn


def fetch_ok(log):
    return "ok", POOLS


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.data = d
        patches = [mock.patch.object(util, "DATA_DIR", d),
                   mock.patch.object(util, "DB_PATH", d / "usage.db"),
                   mock.patch.object(util, "USAGE_JSON", d / "usage.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.log = mock.Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def run_collect(self, platforms, **kw):
        return collect.run(now=NOW, platforms=platforms, log=self.log, **kw)

    def test_output_shape(self):
        usage = self.run_collect([("claude", make_collect("claude"), fetch_ok),
                                  ("cursor", make_collect("cursor"), fetch_ok)])
        on_disk = json.loads((self.data / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, usage)
        self.assertEqual(set(usage["platforms"]), {"claude", "cursor"})
        for name, p in usage["platforms"].items():
            self.assertEqual(p["status"], "ok")
            self.assertEqual(p["series"][0]["key"], "m-high")
            self.assertEqual(p["series"][0]["cache"], [7])
            self.assertEqual(p["plan"]["status"], "ok")
            self.assertEqual(p["plan"]["pools"][0]["id"], "five_hour")
        self.assertFalse((self.data / "collect.lock").exists())
        self.assertEqual(self.log.info.call_args[0][1], "OK")

    def test_one_platform_failing_does_not_block_other(self):
        usage = self.run_collect([("claude", make_collect("claude", boom=True), fetch_ok),
                                  ("cursor", make_collect("cursor"), fetch_ok)])
        self.assertEqual(usage["platforms"]["claude"]["status"], "collect failed (RuntimeError)")
        self.assertEqual(usage["platforms"]["cursor"]["status"], "ok")
        self.assertEqual(len(usage["platforms"]["cursor"]["series"]), 1)
        self.assertNotIn("secret-token-xyz", repr(self.log.mock_calls))

    def test_missing_client_is_skipped_then_picked_up(self):
        present = {"cursor": False}
        cursor_collect = mock.Mock(side_effect=make_collect("cursor"))
        cursor_fetch = mock.Mock(side_effect=fetch_ok)
        plats = [("claude", make_collect("claude"), fetch_ok, lambda: True),
                 ("cursor", cursor_collect, cursor_fetch, lambda: present["cursor"])]
        usage = self.run_collect(plats)
        self.assertEqual(usage["platforms"]["cursor"], {"installed": False, "status": "not installed"})
        self.assertTrue(usage["platforms"]["claude"]["installed"])
        cursor_collect.assert_not_called()
        cursor_fetch.assert_not_called()
        self.assertEqual(self.log.info.call_args[0][1], "OK")
        # Installed later: the next run collects it with no other action.
        present["cursor"] = True
        usage = collect.run(now=NOW + 60, platforms=plats, log=self.log)
        self.assertTrue(usage["platforms"]["cursor"]["installed"])
        self.assertEqual(usage["platforms"]["cursor"]["status"], "ok")
        self.assertEqual(len(usage["platforms"]["cursor"]["series"]), 1)

    def test_install_check_error_counts_as_missing(self):
        def boom():
            raise OSError("x")
        usage = self.run_collect([("claude", make_collect("claude"), fetch_ok, boom)])
        self.assertFalse(usage["platforms"]["claude"]["installed"])

    def test_plan_failure_is_contained(self):
        def bad(log):
            raise ValueError("x")
        usage = self.run_collect([("claude", make_collect("claude"), bad)])
        self.assertEqual(usage["platforms"]["claude"]["status"], "ok")
        self.assertIn("plan check failed", usage["platforms"]["claude"]["plan"]["status"])

    def test_lock_busy_exits_quietly(self):
        with util.FileLock(self.data / "collect.lock"):
            r = self.run_collect([("claude", make_collect("claude"), fetch_ok)])
        self.assertIsNone(r)
        self.assertFalse((self.data / "usage.json").exists())

    def test_plan_throttled_on_second_run(self):
        fetch = mock.Mock(side_effect=fetch_ok)
        plats = [("claude", make_collect("claude"), fetch)]
        self.run_collect(plats)
        collect.run(now=NOW + 60, platforms=plats, log=self.log)
        self.assertEqual(fetch.call_count, 1)

    def test_main_returns_zero_when_busy(self):
        with util.FileLock(self.data / "collect.lock"):
            with mock.patch.object(collect, "_platforms", return_value=[]), \
                    mock.patch.object(util, "get_logger", return_value=self.log):
                self.assertEqual(collect.main([]), 0)


if __name__ == "__main__":
    unittest.main()
