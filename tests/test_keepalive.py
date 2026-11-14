"""Claude sign-in keep-alive: when it runs, when it does not, and that it never breaks a collection pass."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, util
from collector.plan import keepalive
from collector.store import Store

NOW = 1_800_000_000.0


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.store.close)
        for p in (mock.patch.dict(os.environ, {"TOKEN_BURN_CLAUDE_KEEPALIVE": "1"}),
                  mock.patch.object(util, "DATA_DIR", Path(self.tmp) / "data"),
                  mock.patch.object(keepalive, "find_claude", return_value="claude-bin")):
            p.start()
            self.addCleanup(p.stop)

    def run_at(self, t, result=True):
        with mock.patch.object(keepalive, "run_claude", return_value=result) as m:
            ran = keepalive.maybe_run(self.store, t, None)
        return ran, m

    def test_first_run_then_about_every_six_hours(self):
        with mock.patch.object(keepalive.random, "uniform", return_value=0):
            ran, m = self.run_at(NOW)
            self.assertTrue(ran)
            self.assertEqual(m.call_args[0][0], "claude-bin")
            self.assertFalse(self.run_at(NOW + 5 * 3600)[0])
            self.assertFalse(self.run_at(NOW + 6 * 3600 - 1)[0])
            self.assertTrue(self.run_at(NOW + 6 * 3600)[0])

    def test_wait_is_six_hours_plus_or_minus_up_to_an_hour(self):
        with mock.patch.object(keepalive.random, "uniform", return_value=-3600):
            self.assertTrue(self.run_at(NOW)[0])
            self.assertFalse(self.run_at(NOW + 5 * 3600 - 1)[0])
            self.assertTrue(self.run_at(NOW + 5 * 3600)[0])
        with mock.patch.object(keepalive.random, "uniform", return_value=3600):
            t = NOW + 10 * 3600
            self.assertTrue(self.run_at(t)[0])
            self.assertFalse(self.run_at(t + 7 * 3600 - 1)[0])
            self.assertTrue(self.run_at(t + 7 * 3600)[0])

    def test_real_offsets_stay_within_an_hour_and_vary(self):
        waits = set()
        for i in range(30):
            self.store.kv_set(keepalive.KEY, {})
            self.run_at(NOW)
            w = self.store.kv_get(keepalive.KEY)["interval"]
            self.assertTrue(5 * 3600 <= w <= 7 * 3600)
            waits.add(round(w))
        self.assertGreater(len(waits), 5)

    def test_a_damaged_interval_falls_back_to_six_hours(self):
        self.store.kv_set(keepalive.KEY, {"last_attempt": NOW, "ok": True, "interval": 10})
        self.assertFalse(self.run_at(NOW + 3600)[0])
        self.assertTrue(self.run_at(NOW + 7 * 3600)[0])

    def test_about_four_runs_in_a_day_of_minute_passes(self):
        runs = 0
        with mock.patch.object(keepalive, "run_claude", return_value=True):
            for minute in range(0, 24 * 60):
                runs += keepalive.maybe_run(self.store, NOW + minute * 60, None)
        self.assertIn(runs, (3, 4, 5))

    def test_failure_retries_after_an_hour_not_every_pass(self):
        self.assertTrue(self.run_at(NOW, result=False)[0])
        self.assertFalse(self.run_at(NOW + 30 * 60)[0])
        self.assertTrue(self.run_at(NOW + 3600 + 1)[0])

    def test_expired_sign_in_brings_the_retry_forward(self):
        self.assertTrue(self.run_at(NOW)[0])
        self.store.kv_set("plan:claude", {"status": "sign-in expired, run claude once"})
        self.assertFalse(self.run_at(NOW + 30 * 60)[0])
        self.assertTrue(self.run_at(NOW + 3600 + 1)[0])

    def test_timeout_and_missing_program_are_failures_not_errors(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertTrue(keepalive.maybe_run(self.store, NOW, None))
        self.assertIs(self.store.kv_get(keepalive.KEY)["ok"], False)
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError()):
            self.assertTrue(keepalive.maybe_run(self.store, NOW + 3601, None))

    def test_off_by_environment(self):
        with mock.patch.dict(os.environ, {"TOKEN_BURN_CLAUDE_KEEPALIVE": "0"}):
            self.assertFalse(self.run_at(NOW)[0])

    def test_off_by_config_file(self):
        with mock.patch.dict(os.environ, {"TOKEN_BURN_CLAUDE_KEEPALIVE": ""}):
            self.assertTrue(keepalive.enabled())
            from collector.sync import config
            config.save(util.DATA_DIR, {"claude_keepalive": False})
            self.assertFalse(keepalive.enabled())

    def test_no_claude_program_means_no_run(self):
        with mock.patch.object(keepalive, "find_claude", return_value=None):
            self.assertFalse(self.run_at(NOW)[0])

    def test_command_is_the_cheap_one_word_prompt_with_no_window_output(self):
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as m:
            self.assertTrue(keepalive.run_claude("c", self.tmp))
        args, kw = m.call_args
        self.assertEqual(args[0], ["c", "--model", "haiku", "-p", "ok"])
        self.assertIs(kw["stdout"], subprocess.DEVNULL)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_collect_runs_it_for_claude_only(self):
        d = Path(self.tmp) / "d"

        def make(name):
            return (name, lambda store, now, log, backfill=False: {"status": "ok", "meta": {}},
                    lambda log=None: ("ok", None))
        with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                mock.patch.object(util, "USAGE_JSON", d / "usage.json"), \
                mock.patch.object(keepalive, "maybe_run", return_value=False) as m:
            collect.run(now=NOW, platforms=[make("claude"), make("cursor")], log=mock.Mock())
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
