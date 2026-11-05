import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.sync import config, peers, validate
from collector.util import MINUTES, iso

NOW = 1_800_000_000.0
NOW_MIN = int(NOW // 60)
SELF = "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7"
PEER = "0123456789abcdef0123456789abcdef"
PEER2 = "fedcba9876543210fedcba9876543210"
MIB = 1024 * 1024


def series(**over):
    s = {"key": "claude-opus-medium", "model": "claude-opus", "effort": "medium",
         "m": [NOW_MIN - 10, NOW_MIN - 5], "input": [120, 80], "output": [900, 400],
         "cache": [337285, 110740], "cache_read": [300000, 100000], "cost": [0.76, 0.05],
         "requests": [3, 1], "sub_input": [0, 10], "sub_output": [0, 50], "sub_cache": [0, 2000],
         "sub_cache_read": [0, 1800], "sub_cost": [0.0, 0.0012]}
    s.update(over)
    return s


def machine_doc(mid=PEER, **over):
    doc = {"format": "token-burn-machine", "schema": 1, "machine_id": mid, "display_name": "MacBook",
           "hostname": "MBP", "os": "macOS 15", "app_version": "0.2.0", "written_at": iso(NOW - 30),
           "write_token": "9f1c2a7b3d4e5f60", "minutes": MINUTES,
           "platforms": {
               "claude": {"installed": True, "status": "ok", "series": [series()],
                          "subagent_runs": [{"agent_id": "b81f0c2e9a7d4c11", "agent_name": "explore",
                                             "agent_type": "explore", "model": "claude-sonnet-5",
                                             "effort": "medium", "t": iso(NOW - 600), "status": "completed",
                                             "input": 1200, "output": 3400, "cache": 250000, "cost": 0.41,
                                             "cost_estimated": False, "session_id": "SECRET"}],
                          "plan": {"last_lookup": iso(NOW - 60),
                                   "samples": [[NOW - 100, {"five_hour": {"pct": 9.0,
                                                                          "resets_at": "2026-11-05T06:29:59Z"}}]]}},
               "cursor": {"installed": False}}}
    doc.update(over)
    return doc


def gz(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
    return gzip.compress(raw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.folder = root / "shared"
        self.mdir = self.folder / "token-burn" / "machines"
        self.cdir = self.folder / "token-burn" / "control"
        self.mdir.mkdir(parents=True)
        self.cdir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, mid, obj, name=None):
        path = self.mdir / (name or "%s.json.gz" % mid)
        path.write_bytes(obj if isinstance(obj, bytes) and name is None and False else
                         (gz(obj) if not isinstance(obj, bytes) else obj))
        return path

    def scan(self, now=NOW):
        return peers.scan(self.folder, SELF, now, self.data)

    def one(self, now=NOW):
        res = self.scan(now)
        self.assertEqual(len(res), 1)
        return res[0]


class ScanBasicsTests(Base):
    def test_good_file(self):
        self.put(PEER, machine_doc())
        item = self.one()
        self.assertEqual(item["id"], PEER)
        self.assertEqual(item["status"], "ok")
        self.assertFalse(item["from_cache"])
        self.assertIsNone(item["control"])
        doc = item["doc"]
        self.assertEqual(doc["display_name"], "MacBook")
        self.assertEqual(doc["last_seen"], NOW - 30)
        s = doc["platforms"]["claude"]["series"][0]
        self.assertEqual(s["m"], [NOW_MIN - 10, NOW_MIN - 5])
        self.assertEqual(s["input"], [120, 80])
        self.assertFalse(doc["platforms"]["cursor"]["installed"])
        run = doc["platforms"]["claude"]["subagent_runs"][0]
        self.assertNotIn("session_id", run)
        self.assertEqual(run["input"], 1200)
        sample = doc["platforms"]["claude"]["plan"]["samples"][0]
        self.assertEqual(sample, [NOW - 100, {"five_hour": {"pct": 9.0, "resets_at": "2026-11-05T06:29:59Z"}}])

    def test_missing_folder_gives_empty_result(self):
        res = peers.scan(Path(self.tmp.name) / "nope", SELF, NOW, self.data)
        self.assertEqual(list(res), [])
        self.assertEqual(res.ignored_files, 0)

    def test_own_file_is_skipped(self):
        self.put(SELF, machine_doc(SELF))
        self.assertEqual(len(self.scan()), 0)

    def test_ignores_copies_tmp_and_odd_names(self):
        self.put(PEER, machine_doc())
        for name in ("%s-MBP.json.gz" % PEER, "%s (1).json.gz" % PEER, ".tmp-%s-123.json.gz" % PEER,
                     "%s.json.gz.tmp" % PEER, "notes.txt", "%s.json.gz.bak" % PEER,
                     "%s Conflicted Copy.json.gz" % PEER, "short.json.gz"):
            (self.mdir / name).write_bytes(b"junk")
        (self.mdir / "subdir").mkdir()
        res = self.scan()
        self.assertEqual([r["id"] for r in res], [PEER])
        self.assertEqual(res.ignored_files, 8)
        self.assertEqual(res[0]["status"], "ok")

    def test_icloud_stub(self):
        (self.mdir / (".%s.json.gz.icloud" % PEER)).write_bytes(b"stub")
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], "not downloaded yet")

    def test_icloud_stub_uses_cache(self):
        self.put(PEER, machine_doc())
        self.assertEqual(self.one()["status"], "ok")
        os.remove(self.mdir / ("%s.json.gz" % PEER))
        (self.mdir / (".%s.json.gz.icloud" % PEER)).write_bytes(b"stub")
        item = self.one()
        self.assertTrue(item["from_cache"])
        self.assertEqual(item["status"], "not downloaded yet; showing the last good copy")
        self.assertEqual(item["doc"]["display_name"], "MacBook")

    def test_real_file_beats_stub(self):
        self.put(PEER, machine_doc())
        (self.mdir / (".%s.json.gz.icloud" % PEER)).write_bytes(b"stub")
        self.assertEqual(self.one()["status"], "ok")

    def test_unreadable_file_on_windows_style_placeholder(self):
        self.put(PEER, machine_doc())
        self.assertEqual(self.one()["status"], "ok")
        real_open = open

        def fake_open(path, *a, **kw):
            if str(path).startswith(str(self.mdir)):
                raise OSError("cloud file not available")
            return real_open(path, *a, **kw)
        with mock.patch("collector.sync.validate.open", side_effect=fake_open, create=True):
            item = self.one()
        self.assertTrue(item["from_cache"])
        self.assertTrue(item["status"].startswith("not downloaded yet"))

    @unittest.skipUnless(os.name == "posix", "symlinks need POSIX")
    def test_symlink_skipped(self):
        target = Path(self.tmp.name) / "real.json.gz"
        target.write_bytes(gz(machine_doc()))
        os.symlink(target, self.mdir / ("%s.json.gz" % PEER))
        res = self.scan()
        self.assertEqual(len(res), 0)
        self.assertEqual(res.ignored_files, 1)

    def test_newest_first_and_cap_at_64(self):
        ids = ["%032x" % (i + 1) for i in range(70)]
        for i, mid in enumerate(ids):
            path = self.put(mid, machine_doc(mid))
            os.utime(path, (1000 + i, 1000 + i))
        res = self.scan()
        self.assertEqual(len(res), 64)
        self.assertTrue(res.too_many)
        self.assertIn("too many computers", res.messages[0])
        self.assertEqual(res[0]["id"], ids[-1])
        self.assertNotIn(ids[0], [r["id"] for r in res])


class FailureTests(Base):
    def test_truncated_gzip_without_cache(self):
        raw = gz(machine_doc())
        self.put(PEER, raw[: len(raw) // 2])
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], validate.DAMAGED)

    def test_truncated_gzip_falls_back_to_cache(self):
        raw = gz(machine_doc())
        self.put(PEER, raw)
        self.assertEqual(self.one()["status"], "ok")
        self.assertTrue((self.data / "peer-cache" / ("%s.json.gz" % PEER)).is_file())
        self.put(PEER, raw[: len(raw) // 2])
        item = self.one()
        self.assertTrue(item["from_cache"])
        self.assertEqual(item["status"], validate.DAMAGED + "; showing the last good copy")
        self.assertEqual(item["doc"]["platforms"]["claude"]["series"][0]["input"], [120, 80])

    def test_cache_not_overwritten_by_bad_file(self):
        self.put(PEER, machine_doc())
        self.scan()
        cache = self.data / "peer-cache" / ("%s.json.gz" % PEER)
        good = cache.read_bytes()
        self.put(PEER, b"garbage")
        self.scan()
        self.assertEqual(cache.read_bytes(), good)

    def test_crc_error(self):
        raw = bytearray(gz(machine_doc()))
        raw[-6] ^= 0xFF           # corrupt the CRC32
        self.put(PEER, bytes(raw))
        self.assertEqual(self.one()["status"], validate.DAMAGED)

    def test_not_gzip(self):
        self.put(PEER, b"plain text")
        self.assertEqual(self.one()["status"], validate.DAMAGED)

    def test_gzip_bomb_rejected(self):
        bomb = gzip.compress(b"\x00" * (64 * MIB + 1), compresslevel=1)
        self.assertLess(len(bomb), 16 * MIB)
        self.put(PEER, bomb)
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], "file is too large when unpacked")

    def test_bomb_cap_is_a_parameter(self):
        path = self.mdir / "x.gz"
        path.write_bytes(gzip.compress(b"a" * 5000))
        with self.assertRaises(validate.ReadError):
            validate.read_gz_capped(path, max_plain=1000)
        self.assertEqual(len(validate.read_gz_capped(path, max_plain=5000)), 5000)

    def test_compressed_size_cap(self):
        path = self.mdir / ("%s.json.gz" % PEER)
        with open(path, "wb") as f:
            f.truncate(16 * MIB + 1)
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], "file is too large")

    def test_id_mismatch(self):
        self.put(PEER, machine_doc(PEER2))
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertIn("does not match", item["status"])

    def test_newer_schema(self):
        self.put(PEER, machine_doc(schema=2))
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], "made by a newer Token Burn; update this computer")

    def test_non_dict_and_wrong_format(self):
        self.put(PEER, [1, 2, 3])
        self.assertIsNone(self.one()["doc"])
        self.put(PEER, machine_doc(format="something-else"))
        self.assertIsNone(self.one()["doc"])
        self.put(PEER, b"")
        self.assertIsNone(self.one()["doc"])

    def test_deep_nesting(self):
        self.put(PEER, gzip.compress(b"[" * 200000 + b"]" * 200000))
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertEqual(item["status"], validate.DAMAGED)

    def test_deep_nesting_inside_object(self):
        deep = b'{"format":"token-burn-machine","x":' + b"[" * 100000 + b"]" * 100000 + b"}"
        self.put(PEER, gzip.compress(deep))
        self.assertIsNone(self.one()["doc"])

    def test_bad_written_at(self):
        self.put(PEER, machine_doc(written_at="yesterday-ish"))
        self.assertIsNone(self.one()["doc"])
        self.put(PEER, machine_doc(written_at=12345))
        self.assertIsNone(self.one()["doc"])


class ValidationTests(Base):
    def parse(self, doc, now=NOW):
        return validate.parse_machine(json.loads(json.dumps(doc)), PEER, now)

    def claude_series(self, doc):
        return doc["platforms"]["claude"]["series"]

    def with_series(self, **over):
        d = machine_doc()
        d["platforms"]["claude"]["series"] = [series(**over)]
        return d

    def test_nan_and_text_values_reject_series(self):
        for bad in (float("nan"), float("inf"), "12", None, [1], True):
            d = self.with_series(cost=[bad, 0.1])
            clean, status = self.parse(d)
            self.assertEqual(status, "ok")
            self.assertEqual(self.claude_series(clean), [], repr(bad))
            d = self.with_series(input=[bad, 5])
            clean, _ = self.parse(d)
            self.assertEqual(self.claude_series(clean), [], repr(bad))

    def test_negative_and_huge_values_clamped(self):
        d = self.with_series(input=[-5, 10 ** 15], cost=[-1.0, 1e9], requests=[-3, 10 ** 9])
        clean, _ = self.parse(d)
        (s,) = self.claude_series(clean)
        self.assertEqual(s["input"], [0, 10 ** 12])
        self.assertEqual(s["cost"], [0.0, 1e6])
        self.assertEqual(s["requests"], [0, 10 ** 7])

    def test_sub_values_clamped_to_totals(self):
        d = self.with_series(input=[10, 10], sub_input=[50, 5], cache=[100, 100], sub_cache=[100, 999],
                             cache_read=[100, 100], sub_cache_read=[500, 20], cost=[1.0, 1.0],
                             sub_cost=[5.0, 0.5])
        clean, _ = self.parse(d)
        (s,) = self.claude_series(clean)
        self.assertEqual(s["sub_input"], [10, 5])
        self.assertEqual(s["sub_cache"], [100, 100])
        self.assertEqual(s["sub_cache_read"], [100, 20])
        self.assertEqual(s["sub_cost"], [1.0, 0.5])

    def test_out_of_window_minutes_dropped_not_rejected(self):
        lo = NOW_MIN - MINUTES + 1
        d = self.with_series(m=[lo - 5, lo, NOW_MIN, NOW_MIN + 5, NOW_MIN + 6], input=[1, 2, 3, 4, 5],
                             output=[1] * 5, cache=[1] * 5, cost=[0.0] * 5, requests=[1] * 5,
                             sub_input=[0] * 5, sub_output=[0] * 5, sub_cache=[0] * 5, sub_cost=[0.0] * 5,
                             cache_read=[0] * 5, sub_cache_read=[0] * 5)
        clean, _ = self.parse(d)
        (s,) = self.claude_series(clean)
        self.assertEqual(s["m"], [lo, NOW_MIN, NOW_MIN + 5])
        self.assertEqual(s["input"], [2, 3, 4])

    def test_non_ascending_or_ragged_series_dropped(self):
        for over in ({"m": [NOW_MIN - 5, NOW_MIN - 5]}, {"m": [NOW_MIN - 5, NOW_MIN - 9]},
                     {"input": [1]}, {"m": ["a", "b"]}, {"m": [1.5, 2.5]}):
            clean, _ = self.parse(self.with_series(**over))
            self.assertEqual(self.claude_series(clean), [], over)

    def test_point_and_series_caps(self):
        n = validate.MAX_POINTS + 1
        d = self.with_series(m=list(range(NOW_MIN - n, NOW_MIN)), input=[1] * n)
        clean, _ = self.parse(d)
        self.assertEqual(self.claude_series(clean), [])
        d = machine_doc()
        d["platforms"]["claude"]["series"] = [series(key="k%d" % i, model="m%d" % i) for i in range(350)]
        clean, _ = self.parse(d)
        self.assertEqual(len(self.claude_series(clean)), validate.MAX_SERIES)

    def test_optional_sub_fields_default_to_zero(self):
        s = series()
        for f in ("sub_input", "sub_output", "sub_cache", "sub_cost", "sub_cache_read", "cache_read"):
            del s[f]
        d = machine_doc()
        d["platforms"]["claude"]["series"] = [s]
        clean, _ = self.parse(d)
        (cs,) = self.claude_series(clean)
        self.assertEqual(cs["sub_input"], [0, 0])
        self.assertEqual(cs["cache_read"], [0, 0])

    def test_unknown_platforms_and_fields_ignored(self):
        d = machine_doc()
        d["platforms"]["evil"] = {"installed": True, "series": [series()]}
        d["platforms"]["claude"]["__proto__"] = {"x": 1}
        d["extra"] = "<script>"
        clean, _ = self.parse(d)
        self.assertEqual(set(clean["platforms"]), {"claude", "cursor"})
        self.assertNotIn("extra", clean)
        self.assertNotIn("__proto__", clean["platforms"]["claude"])
        self.assertNotIn("write_token", clean)

    def test_strings_capped_and_stripped(self):
        d = machine_doc(display_name="A\x00B" + "x" * 100, hostname="h" * 200, os="o\x1b" * 100,
                        app_version="9" * 50)
        d["platforms"]["claude"]["status"] = "s\x07" * 200
        d["platforms"]["claude"]["series"][0]["model"] = "m" * 300
        clean, _ = self.parse(d)
        self.assertEqual(len(clean["display_name"]), 40)
        self.assertTrue(clean["display_name"].startswith("AB"))
        self.assertEqual(len(clean["hostname"]), 64)
        self.assertNotIn("\x1b", clean["os"])
        self.assertLessEqual(len(clean["os"]), 40)
        self.assertEqual(len(clean["app_version"]), 20)
        self.assertEqual(len(clean["platforms"]["claude"]["status"]), 120)
        self.assertEqual(len(self.claude_series(clean)[0]["model"]), 120)

    def test_runs_whitelisted_and_capped(self):
        d = machine_doc()
        runs = [{"agent_id": "a%d" % i, "t": iso(NOW - i), "input": None, "extra": "x"}
                for i in range(validate.MAX_RUNS + 10)]
        runs.append({"agent_id": "", "t": iso(NOW)})              # no id
        runs.append({"agent_id": "bad-time", "t": "nope"})
        runs.append("not a dict")
        d["platforms"]["claude"]["subagent_runs"] = runs
        clean, _ = self.parse(d)
        out = clean["platforms"]["claude"]["subagent_runs"]
        self.assertEqual(len(out), validate.MAX_RUNS)
        self.assertNotIn("extra", out[0])
        self.assertIsNone(out[0]["input"])

    def test_plan_samples_checked(self):
        d = machine_doc()
        d["platforms"]["claude"]["plan"]["samples"] = [
            [NOW - 10, {"five_hour": {"pct": 9.0, "resets_at": "x" * 50}}],       # long resets_at dropped
            [float("nan"), {"five_hour": {"pct": 1.0}}],                         # bad t
            [NOW - 20, {"five_hour": {"pct": 5000}}],                            # pct out of range
            [NOW - 30, {"five_hour": {"pct": -1}}],
            [NOW - 40, {"p%d" % i: {"pct": 1.0} for i in range(30)}],            # too many pools
            [NOW - 50, "bad"], "bad", [1],
            [NOW - 60, {"seven_day": {"pct": 12.5}}]]
        clean, _ = self.parse(d)
        samples = clean["platforms"]["claude"]["plan"]["samples"]
        self.assertEqual(samples[0], [NOW - 10, {"five_hour": {"pct": 9.0}}])
        self.assertEqual(len(samples[1][1]), validate.MAX_POOLS)
        self.assertEqual(samples[2], [NOW - 60, {"seven_day": {"pct": 12.5}}])
        self.assertEqual(len(samples), 3)

    def test_plan_sample_count_cap(self):
        d = machine_doc()
        d["platforms"]["claude"]["plan"]["samples"] = [[float(i), {"a": {"pct": 1.0}}]
                                                       for i in range(validate.MAX_SAMPLES + 50)]
        clean, _ = self.parse(d)
        self.assertEqual(len(clean["platforms"]["claude"]["plan"]["samples"]), validate.MAX_SAMPLES)

    def test_future_written_at_skew(self):
        d = machine_doc(written_at=iso(NOW + 3600))
        d["platforms"]["claude"]["series"] = [series(m=[NOW_MIN - 1, NOW_MIN + 3])]
        d["platforms"]["claude"]["series"][0].update(input=[1, 2], output=[1, 2], cache=[1, 2], cost=[0.0, 0.0],
                                                    requests=[1, 1], sub_input=[0, 0], sub_output=[0, 0],
                                                    sub_cache=[0, 0], sub_cost=[0.0, 0.0],
                                                    cache_read=[0, 0], sub_cache_read=[0, 0])
        clean, status = self.parse(d)
        self.assertEqual(status, "clock is ahead by 60 min")
        self.assertEqual(clean["last_seen"], NOW)
        self.assertEqual(clean["platforms"]["claude"]["series"][0]["m"], [NOW_MIN - 1])

    def test_small_skew_tolerated(self):
        clean, status = self.parse(machine_doc(written_at=iso(NOW + 300)))
        self.assertEqual(status, "ok")
        self.assertEqual(clean["last_seen"], NOW + 300)

    def test_skew_through_scan(self):
        self.put(PEER, machine_doc(written_at=iso(NOW + 7200)))
        item = self.one()
        self.assertEqual(item["status"], "clock is ahead by 120 min")
        self.assertEqual(item["doc"]["last_seen"], NOW)


class ControlTests(Base):
    def test_write_and_read_controls(self):
        doc = peers.write_control(self.folder, PEER, "retired", "Old Mac", SELF, NOW)
        self.assertEqual(doc["state"], "retired")
        got = peers.read_controls(self.folder)
        self.assertEqual(set(got), {PEER})
        self.assertEqual(got[PEER]["display_name"], "Old Mac")
        self.assertEqual(got[PEER]["by"], SELF)
        self.assertEqual(got[PEER]["at"], iso(NOW))
        self.assertEqual(os.listdir(self.cdir), ["%s.json" % PEER])
        # a later change without a name keeps the name
        peers.write_control(self.folder, PEER, "active", None, SELF, NOW + 5)
        self.assertEqual(peers.read_control(self.folder, PEER)["display_name"], "Old Mac")
        self.assertEqual(peers.read_control(self.folder, PEER)["state"], "active")

    def test_write_control_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            peers.write_control(self.folder, "../x", "retired", "n", SELF, NOW)
        with self.assertRaises(ValueError):
            peers.write_control(self.folder, PEER, "exploded", "n", SELF, NOW)
        with self.assertRaises(ValueError):
            peers.write_control(self.folder, PEER, "retired", "x" * 41, SELF, NOW)

    def test_bad_control_files_ignored(self):
        good = {"format": "token-burn-control", "schema": 1, "machine_id": PEER, "state": "retired",
                "display_name": "N", "by": SELF, "at": iso(NOW)}
        cases = {
            PEER: dict(good, state="weird"),
            PEER2: dict(good, machine_id=PEER),                      # id mismatch
        }
        for mid, doc in cases.items():
            (self.cdir / ("%s.json" % mid)).write_text(json.dumps(doc), encoding="utf-8")
        (self.cdir / ("%s-copy.json" % PEER)).write_text(json.dumps(good), encoding="utf-8")
        (self.cdir / "notes.json").write_text("{}", encoding="utf-8")
        self.assertEqual(peers.read_controls(self.folder), {})
        big = dict(good, display_name="x", pad="y" * 5000)
        (self.cdir / ("%s.json" % PEER)).write_text(json.dumps(big), encoding="utf-8")
        self.assertIsNone(peers.read_control(self.folder, PEER))             # over 4 KB
        (self.cdir / ("%s.json" % PEER)).write_text("[1]", encoding="utf-8")
        self.assertIsNone(peers.read_control(self.folder, PEER))

    def test_control_whitelists_fields(self):
        good = {"format": "token-burn-control", "schema": 1, "machine_id": PEER, "state": "retired",
                "display_name": "N\x00ame", "by": "not-an-id", "at": "garbage", "evil": "x"}
        (self.cdir / ("%s.json" % PEER)).write_text(json.dumps(good), encoding="utf-8")
        c = peers.read_control(self.folder, PEER)
        self.assertEqual(c["display_name"], "Name")
        self.assertEqual(c["by"], "")
        self.assertEqual(c["at"], "")
        self.assertNotIn("evil", c)

    def test_retired_attached_and_data_kept(self):
        self.put(PEER, machine_doc())
        peers.write_control(self.folder, PEER, "retired", "Old Mac", SELF, NOW)
        item = self.one()
        self.assertEqual(item["control"]["state"], "retired")
        self.assertIsNotNone(item["doc"])

    def test_removed_machine_is_not_read(self):
        self.put(PEER, machine_doc())
        peers.write_control(self.folder, PEER, "removed", "Old Mac", SELF, NOW)
        item = self.one()
        self.assertEqual(item["status"], "removed")
        self.assertIsNone(item["doc"])
        self.assertEqual(item["control"]["state"], "removed")

    def test_delete_machine_file(self):
        path = self.put(PEER, machine_doc())
        self.assertTrue(peers.delete_machine_file(self.folder, PEER))
        self.assertFalse(path.exists())
        self.assertFalse(peers.delete_machine_file(self.folder, PEER))
        self.assertFalse(peers.delete_machine_file(self.folder, "../../etc"))

    def test_machine_file_rejects_bad_ids(self):
        for bad in ("", "../x", PEER.upper(), PEER + "0"):
            with self.assertRaises(ValueError):
                peers.machine_file(self.folder, bad)

    def test_ensure_layout(self):
        fresh = Path(self.tmp.name) / "fresh"
        fresh.mkdir()
        peers.ensure_layout(fresh)
        root = fresh / "token-burn"
        self.assertTrue((root / "machines").is_dir() and (root / "control").is_dir())
        self.assertIn("Token Burn", (root / "README.txt").read_text(encoding="utf-8"))


class CacheTests(Base):
    def cache(self, mid=PEER):
        return self.data / "peer-cache" / ("%s.json.gz" % mid)

    def test_cache_written_atomically_and_matches_source(self):
        path = self.put(PEER, machine_doc())
        self.scan()
        self.assertEqual(self.cache().read_bytes(), path.read_bytes())
        self.assertEqual(os.listdir(self.cache().parent), ["%s.json.gz" % PEER])

    def test_cache_removed_after_30_days_gone(self):
        self.put(PEER, machine_doc())
        self.scan(NOW)
        os.remove(self.mdir / ("%s.json.gz" % PEER))
        self.scan(NOW + 29 * 86400)
        self.assertTrue(self.cache().exists())
        self.scan(NOW + 31 * 86400)
        self.assertFalse(self.cache().exists())

    def test_cache_kept_while_source_present_even_if_old(self):
        self.put(PEER, machine_doc())
        self.scan(NOW)
        self.scan(NOW + 90 * 86400)
        self.assertTrue(self.cache().exists())

    def test_cache_kept_while_source_failing(self):
        self.put(PEER, machine_doc())
        self.scan(NOW)
        self.put(PEER, b"garbage")
        self.scan(NOW + 60 * 86400)
        self.assertTrue(self.cache().exists())

    def test_foreign_files_in_cache_dir_untouched(self):
        self.cache().parent.mkdir(parents=True)
        other = self.cache().parent / "keep.txt"
        other.write_text("x")
        self.scan()
        self.assertTrue(other.exists())

    def test_bad_cache_is_ignored(self):
        self.cache().parent.mkdir(parents=True)
        self.cache().write_bytes(b"garbage")
        self.put(PEER, b"garbage too")
        item = self.one()
        self.assertIsNone(item["doc"])
        self.assertFalse(item["from_cache"])


if __name__ == "__main__":
    unittest.main()
