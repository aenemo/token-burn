import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import util


class AtomicWriteTests(unittest.TestCase):
    def test_writes_and_replaces(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "x.json"
            util.atomic_write_json(p, {"a": 1})
            util.atomic_write_json(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 2})
            self.assertEqual([f.name for f in p.parent.iterdir()], ["x.json"])

    def test_retries_on_permission_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            real = os.replace
            calls = {"n": 0}

            def flaky(a, b):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError("locked")
                return real(a, b)

            with mock.patch("collector.util.os.replace", flaky), \
                    mock.patch("collector.util.time.sleep") as sl:
                util.atomic_write_json(p, [1])
            self.assertEqual(calls["n"], 3)
            self.assertEqual(sl.call_count, 2)
            self.assertEqual(json.loads(p.read_text()), [1])

    def test_gives_up_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            with mock.patch("collector.util.os.replace", side_effect=PermissionError("no")), \
                    mock.patch("collector.util.time.sleep"):
                with self.assertRaises(PermissionError):
                    util.atomic_write_json(p, {})
            self.assertEqual(list(Path(d).iterdir()), [])


class FileLockTests(unittest.TestCase):
    def test_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.lock"
            with util.FileLock(p):
                self.assertTrue(p.exists())
                with self.assertRaises(util.LockBusy):
                    with util.FileLock(p):
                        pass
                self.assertTrue(p.exists())  # failed contender must not remove it
            self.assertFalse(p.exists())
            with util.FileLock(p):
                pass

    def test_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.lock"
            p.write_text("999")
            old = os.stat(p).st_mtime - 301
            os.utime(p, (old, old))
            with util.FileLock(p, stale_s=300):
                self.assertTrue(p.exists())
                self.assertNotEqual(p.read_text(), "999")
            self.assertFalse(p.exists())

    def test_fresh_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.lock"
            p.write_text("999")
            with self.assertRaises(util.LockBusy):
                util.FileLock(p).acquire()
            self.assertTrue(p.exists())


class ParseTsTests(unittest.TestCase):
    def test_variants(self):
        base = 1792554663.0  # 2026-10-21T03:51:03Z
        self.assertEqual(util.parse_ts("2026-10-21T03:51:03Z"), base)
        self.assertEqual(util.parse_ts("2026-10-21T03:51:03+00:00"), base)
        self.assertEqual(util.parse_ts("2026-10-21T13:51:03+10:00"), base)
        self.assertEqual(util.parse_ts("2026-10-21T03:51:03"), base)
        self.assertAlmostEqual(util.parse_ts("2026-10-21T03:51:03.250Z"), base + 0.25, places=3)
        self.assertEqual(util.parse_ts(base), base)
        self.assertEqual(util.parse_ts(int(base * 1000)), base)
        self.assertEqual(util.parse_ts(str(int(base * 1000))), base)
        self.assertEqual(util.parse_ts(str(int(base))), base)

    def test_invalid(self):
        for v in (None, "", "garbage", "2026-99-99T00:00:00Z", True, [], {}):
            self.assertIsNone(util.parse_ts(v), v)

    def test_iso_round_trip(self):
        self.assertEqual(util.iso(1792554663), "2026-10-21T03:51:03Z")
        self.assertEqual(util.parse_ts(util.iso(1792554663)), 1792554663)


class MiscTests(unittest.TestCase):
    def test_claude_dir_env(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "C:\\x\\cfg"}):
            self.assertEqual(util.claude_dir(), Path("C:\\x\\cfg"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(util.claude_dir(), Path.home() / ".claude")

    def test_constants(self):
        self.assertEqual(util.MINUTES, 43200)
        self.assertEqual(util.PLAN_INTERVAL_S, 590)
        self.assertEqual(util.DEFAULT_PORT, 8421)

    def test_logger_timestamp_is_local_with_offset_not_z(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(util, "DATA_DIR", Path(d)):
                lg = util.get_logger("unit-test-stamp")
                try:
                    lg.info("hello")
                finally:
                    for h in list(lg.handlers):
                        h.close()
                        lg.removeHandler(h)
                text = (Path(d) / "unit-test-stamp.log").read_text(encoding="utf-8")
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d INFO hello\n$")

    def test_logger_writes_and_rotates(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(util, "DATA_DIR", Path(d)), \
                    mock.patch.object(util, "LOG_MAX", 200):
                name = "unit-test-log"
                lg = util.get_logger(name)
                try:
                    for i in range(40):
                        lg.info("line %d %s", i, "x" * 20)
                finally:
                    for h in list(lg.handlers):
                        h.close()
                        lg.removeHandler(h)
                names = sorted(f.name for f in Path(d).iterdir())
                self.assertIn(name + ".log", names)
                self.assertIn(name + ".log.1", names)
                self.assertEqual(len(names), 2)


if __name__ == "__main__":
    unittest.main()
