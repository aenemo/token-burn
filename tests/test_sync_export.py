import gzip
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.aggregate import build_platform_payload
from collector.schema import SubagentRun, UsageRecord
from collector.store import Store
from collector.sync import config, export, peers, validate
from collector.util import MINUTES, iso

NOW = 1_800_000_000.0           # divisible by 60
NOW_MIN = int(NOW // 60)
START_MIN = NOW_MIN - MINUTES + 1
ID = "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7"
OTHER_ID = "0123456789abcdef0123456789abcdef"
FP = "0f1e2d3c4b5a6978"
OTHER_FP = "8796a5b4c3d2e1f0"
INFO = {"status": "ok", "meta": {}, "plan": {"status": "ok", "last_lookup": None, "next_lookup": None}}
CFG = {"version": 1, "sync": {"enabled": True, "folder": "unused", "machine_id": ID,
                              "machine_name": "Office PC", "joined_at": "2026-11-05T03:00:00Z",
                              "adopted": False}}


def at(idx):
    return (START_MIN + idx) * 60.0 + 5


def rec(idx, platform="claude", inp=0, out=0, cr=0, cw=0, **kw):
    d = dict(platform=platform, agent_kind="primary", agent_id="", session_id="s1", model="m",
             effort_level="", input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
             cache_write_tokens=cw, cost_usd=0.0, timestamp=at(idx))
    d.update(kw)
    return UsageRecord(**d)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.folder = root / "shared"
        self.folder.mkdir()
        self.store = Store(root / "t.db")
        p = mock.patch.object(export, "hostname", return_value="OFFICE-PC")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(export, "host_fp", return_value=FP)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def payloads(self, now=NOW):
        out = {}
        for name in ("claude", "opencode"):
            out[name] = build_platform_payload(self.store, name, now, INFO)
            out[name]["installed"] = True
        out["cursor"] = {"installed": False, "status": "not installed"}
        return out

    def samples(self, name="claude", now=NOW):
        return {name: self.store.plan_samples(name, now - 30 * 86400)}

    def build(self, now=NOW, cfg=CFG, data_dir=None, plan=None):
        return export.build_export(self.payloads(now), plan if plan is not None else self.samples(),
                                   cfg, now, "0.2.0", data_dir=data_dir)

    def own_path(self):
        return self.folder / "token-burn" / "machines" / ("%s.json.gz" % ID)

    def read_own(self):
        with gzip.open(self.own_path(), "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def write(self, now=NOW, force=False, doc=None):
        return export.maybe_write(self.store, self.folder, doc or self.build(now), now, force=force)


class BuildTests(Base):
    def test_shape_and_absolute_minutes(self):
        self.store.add_usage(rec(10, inp=100, out=50, cr=300, cw=20, cost_usd=0.5, requests=2))
        self.store.add_usage(rec(20, inp=7, out=1))
        doc = self.build()
        self.assertEqual(doc["format"], "token-burn-machine")
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["machine_id"], ID)
        self.assertEqual(doc["display_name"], "Office PC")
        self.assertEqual(doc["hostname"], "OFFICE-PC")
        self.assertEqual(doc["app_version"], "0.2.0")
        self.assertEqual(doc["minutes"], MINUTES)
        self.assertEqual(set(doc["platforms"]), {"claude", "cursor", "opencode"})
        self.assertEqual(doc["platforms"]["cursor"], {"installed": False})
        (s,) = doc["platforms"]["claude"]["series"]
        self.assertEqual(s["m"], [START_MIN + 10, START_MIN + 20])
        self.assertEqual(s["input"], [100, 7])
        self.assertEqual(s["cache"], [320, 0])
        self.assertEqual(s["cache_read"], [300, 0])
        self.assertEqual(s["requests"], [2, 1])
        self.assertEqual(s["key"], "m")
        for f in validate.SERIES_FIELDS:
            self.assertEqual(len(s[f]), 2, f)
        self.assertEqual(doc["platforms"]["opencode"]["series"], [])

    def test_export_parses_back_through_validation(self):
        self.store.add_usage(rec(10, inp=100, out=50, cr=300, cw=20, cost_usd=0.5))
        doc = self.build()
        doc["written_at"] = iso(NOW)
        clean, status = validate.parse_machine(json.loads(json.dumps(doc)), ID, NOW)
        self.assertEqual(status, "ok")
        self.assertEqual(clean["platforms"]["claude"]["series"][0]["m"], [START_MIN + 10])
        self.assertFalse(clean["platforms"]["cursor"]["installed"])

    def test_subagent_cells_and_run_hashing(self):
        self.store.add_usage(rec(5, inp=10, out=5, agent_kind="subagent", agent_id="raw-agent-1"))
        self.store.upsert_subagent_run(SubagentRun(
            "claude", "raw-agent-1", "explore", "explore - raw-ag", "s1", "m", "", at(5), "completed", 10, 5))
        doc = self.build()
        (s,) = doc["platforms"]["claude"]["series"]
        self.assertEqual(s["sub_input"], [10])
        (run,) = doc["platforms"]["claude"]["subagent_runs"]
        hashed = export.hash_agent_id(ID, "raw-agent-1")
        self.assertEqual(run["agent_id"], hashed)
        self.assertRegex(hashed, r"^[0-9a-f]{16}$")
        self.assertEqual(run["agent_name"], "explore \u00b7 %s" % hashed[:7])
        self.assertNotIn("session_id", run)
        self.assertEqual(run["input"], 10)

    def test_planted_secrets_never_in_the_file(self):
        secret_session = "SECRET-SESSION-4c1d9e"
        secret_agent = "raw-agent-SECRETID"
        self.store.add_usage(rec(5, inp=10, session_id=secret_session, agent_kind="subagent",
                                 agent_id=secret_agent))
        self.store.upsert_subagent_run(SubagentRun(
            "claude", secret_agent, "explore", "name-" + secret_agent, secret_session, "m", "",
            at(5), "completed", 10, 5))
        pay = self.payloads()
        pay["claude"]["meta"]["api_key"] = "sk-ant-SECRETKEY"
        pay["claude"]["meta"]["files"] = 12
        pay["claude"]["plan"]["status"] = "C:\\Users\\bob\\.claude\\creds"
        pay["claude"]["subagent_runs"][0]["cwd"] = "/home/bob/secret-project"
        pay["claude"]["series"][0]["session_id"] = secret_session
        samples = {"claude": [(NOW - 100, {"five_hour": {"pct": 9.0, "resets_at": "2026-11-05T06:29:59Z",
                                                          "token": "sk-ant-SECRETTOKEN"}})]}
        doc = export.build_export(pay, samples, CFG, NOW, "0.2.0")
        self.assertEqual(export.maybe_write(self.store, self.folder, doc, NOW), export.WRITTEN)
        with gzip.open(self.own_path(), "rb") as f:
            text = f.read().decode("utf-8")
        for needle in (secret_session, secret_agent, "SECRETKEY", "SECRETTOKEN", "bob", "creds",
                       "session_id", "cwd", "meta", "files", "secret-project", "next_lookup"):
            self.assertNotIn(needle, text, needle)
        sample = json.loads(text)["platforms"]["claude"]["plan"]["samples"][0]
        self.assertEqual(sample[1], {"five_hour": {"pct": 9.0, "resets_at": "2026-11-05T06:29:59Z"}})

    def test_status_trimmed_and_control_chars_removed(self):
        pay = self.payloads()
        pay["claude"]["status"] = "ok\x00\x1b" + "x" * 300
        doc = export.build_export(pay, {}, CFG, NOW, "0.2.0")
        st = doc["platforms"]["claude"]["status"]
        self.assertEqual(len(st), 120)
        self.assertNotIn("\x00", st)

    def test_plan_samples_thinned_and_sanitised(self):
        old = NOW - 5 * 86400
        samples = [(old + i * 60, {"five_hour": {"pct": float(i % 50)}}) for i in range(30)]
        samples.append((NOW - 60, {"five_hour": {"pct": float("nan")}, "seven_day": {"pct": 5.0}}))
        samples.append((NOW - 40 * 86400, {"five_hour": {"pct": 1.0}}))   # beyond 30 days
        doc = self.build(plan={"claude": samples})
        got = doc["platforms"]["claude"]["plan"]["samples"]
        self.assertLess(len(got), 10)                      # 30 one-minute samples thinned to 5 minutes
        self.assertEqual(got[-1][1], {"seven_day": {"pct": 5.0}})
        self.assertTrue(all(t >= NOW - 30 * 86400 for t, _ in got))

    def test_body_hash_ignores_write_fields(self):
        self.store.add_usage(rec(10, inp=100))
        a = self.build(now=NOW)
        b = self.build(now=NOW)
        b["written_at"] = "2030-01-01T00:00:00Z"
        b["write_token"] = "abc"
        self.assertEqual(export.body_hash(a), export.body_hash(b))
        b["display_name"] = "Other"
        self.assertNotEqual(export.body_hash(a), export.body_hash(b))

    def test_unconfigured_raises(self):
        with self.assertRaises(ValueError):
            export.build_export({}, {}, {}, NOW, "0.2.0")


class WriteTests(Base):
    def test_first_write_atomic_gzip(self):
        self.store.add_usage(rec(10, inp=100))
        self.assertEqual(self.write(), export.WRITTEN)
        mdir = self.own_path().parent
        self.assertEqual(sorted(os.listdir(mdir)), ["%s.json.gz" % ID])        # no tmp left
        self.assertTrue((self.folder / "token-burn" / "README.txt").is_file())
        self.assertTrue((self.folder / "token-burn" / "control").is_dir())
        raw = self.own_path().read_bytes()
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(raw[4:8], b"\x00\x00\x00\x00")                           # mtime 0
        doc = self.read_own()
        self.assertEqual(doc["written_at"], iso(NOW))
        self.assertRegex(doc["write_token"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.store.kv_get(export.KV_TOKEN), doc["write_token"])
        self.assertEqual(self.store.kv_get(export.KV_WRITE), NOW)
        self.assertEqual(self.store.kv_get(export.KV_HASH), export.body_hash(doc))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_file_mode_0600(self):
        self.write()
        self.assertEqual(os.stat(self.own_path()).st_mode & 0o777, 0o600)

    def test_throttle_300s(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.store.add_usage(rec(10, inp=100))
        self.assertEqual(self.write(NOW + 299), export.THROTTLED)
        self.assertEqual(self.read_own()["platforms"]["claude"]["series"], [])
        self.assertEqual(self.write(NOW + 300), export.WRITTEN)
        self.assertEqual(len(self.read_own()["platforms"]["claude"]["series"]), 1)

    def test_unchanged_is_not_written(self):
        self.store.add_usage(rec(10, inp=100))
        self.write(NOW)
        before = self.own_path().read_bytes()
        self.assertEqual(self.write(NOW + 600), export.UNCHANGED)
        self.assertEqual(self.own_path().read_bytes(), before)
        self.assertEqual(self.store.kv_get(export.KV_WRITE), NOW)

    def test_heartbeat_after_1800s(self):
        self.assertEqual(export.HEARTBEAT_S, 1800)
        self.write(NOW)
        self.assertEqual(self.write(NOW + 1799), export.UNCHANGED)
        self.assertEqual(self.write(NOW + 1800), export.WRITTEN)
        self.assertEqual(self.read_own()["written_at"], iso(NOW + 1800))
        self.assertEqual(self.store.kv_get(export.KV_WRITE), NOW + 1800)

    def test_force_skips_throttle_and_unchanged(self):
        self.write(NOW)
        self.assertEqual(self.write(NOW + 1, force=True), export.WRITTEN)
        self.assertEqual(self.read_own()["written_at"], iso(NOW + 1))

    def test_folder_missing(self):
        doc = self.build()
        status = export.maybe_write(self.store, self.folder / "nope", doc, NOW)
        self.assertEqual(status, export.FOLDER_MISSING)
        self.assertFalse((self.folder / "nope").exists())

    def test_old_own_tmp_files_cleaned(self):
        mdir = self.folder / "token-burn" / "machines"
        mdir.mkdir(parents=True)
        old = mdir / (".tmp-%s-111.json.gz" % ID)
        fresh = mdir / (".tmp-%s-222.json.gz" % ID)
        theirs = mdir / (".tmp-%s-333.json.gz" % OTHER_ID)
        for f in (old, fresh, theirs):
            f.write_bytes(b"x")
        os.utime(old, (NOW - 7200, NOW - 7200))
        os.utime(theirs, (NOW - 7200, NOW - 7200))
        os.utime(fresh, (NOW - 60, NOW - 60))
        self.assertEqual(self.write(), export.WRITTEN)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(theirs.exists())

    def test_failed_replace_leaves_no_partial_file(self):
        with mock.patch.object(peers.os, "replace", side_effect=PermissionError("locked")), \
                mock.patch.object(peers.time, "sleep"):
            status = self.write()
        self.assertEqual(status, "write failed (PermissionError)")
        self.assertFalse(self.own_path().exists())
        self.assertEqual(os.listdir(self.own_path().parent), [])
        self.assertIsNone(self.store.kv_get(export.KV_TOKEN))

    def test_replace_retries_on_permission_error(self):
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("locked")
            return real(a, b)
        with mock.patch.object(peers.os, "replace", side_effect=flaky), \
                mock.patch.object(peers.time, "sleep"):
            self.assertEqual(self.write(), export.WRITTEN)
        self.assertTrue(self.own_path().is_file())


class ConflictTests(Base):
    def foreign_file(self, hostname, token="ffffffffffffffff", written_at=NOW + 50, fp=None):
        """A file for this id written by another writer. fp None = an older file without host_fp."""
        doc = self.build()
        doc["hostname"] = hostname
        if fp is None:
            doc.pop("host_fp", None)
        else:
            doc["host_fp"] = fp
        doc["write_token"] = token
        doc["written_at"] = iso(written_at)
        path = self.own_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(export._gzip_bytes(doc))

    def test_other_writer_detected(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.foreign_file("MACBOOK", written_at=NOW + 100)
        self.store.add_usage(rec(10, inp=1))                                  # content changed, so a write is due
        status = self.write(NOW + 400, doc=self.build(NOW + 400))
        self.assertEqual(status, export.CONFLICT)
        msg = self.store.kv_get(export.KV_CONFLICT)
        self.assertIn("MACBOOK", msg)
        self.assertIn("token-burn sync reset-id", msg)
        self.assertEqual(self.read_own()["write_token"], "ffffffffffffffff")   # not overwritten
        # exports stay stopped until reset-id clears the key
        self.assertEqual(self.write(NOW + 4000, force=True), export.CONFLICT)

    def test_own_token_is_not_a_conflict(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.store.add_usage(rec(10, inp=1))
        self.assertEqual(self.write(NOW + 400), export.WRITTEN)
        self.assertIsNone(self.store.kv_get(export.KV_CONFLICT))

    def test_older_foreign_file_is_not_a_conflict(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.foreign_file("MACBOOK", written_at=NOW - 500)
        self.assertEqual(self.write(NOW + 400, force=True), export.WRITTEN)

    def test_reinstall_same_hostname_proceeds(self):
        self.foreign_file("office-pc")            # no remembered token, hostname matches (any case)
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.assertNotEqual(self.read_own()["write_token"], "ffffffffffffffff")

    def test_first_write_different_hostname_conflicts(self):
        self.foreign_file("MACBOOK")
        self.assertEqual(self.write(NOW), export.CONFLICT)
        self.assertIn("MACBOOK", self.store.kv_get(export.KV_CONFLICT))

    def test_damaged_own_file_is_not_a_conflict(self):
        path = self.own_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x1f\x8b\x08garbage")
        self.assertEqual(self.write(NOW), export.WRITTEN)

    def test_check_conflict_direct(self):
        self.assertIsNone(export.check_conflict(self.store, self.folder, ID, "OFFICE-PC"))
        self.foreign_file("MACBOOK")
        self.assertIsNotNone(export.check_conflict(self.store, self.folder, ID, "OFFICE-PC"))
        self.assertIsNone(export.check_conflict(self.store, self.folder, ID, "macbook"))

    def adopt_write(self, now=NOW, adopted=True):
        return export.maybe_write(self.store, self.folder, self.build(now), now, adopted=adopted)

    def test_same_host_fp_with_foreign_token_is_not_a_conflict(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        # the store lost its memory (restored usage.db) while the file moved on
        self.foreign_file("OFFICE-PC", fp=FP, written_at=NOW + 100)
        self.store.add_usage(rec(10, inp=1))
        self.assertEqual(self.write(NOW + 400, doc=self.build(NOW + 400)), export.WRITTEN)
        self.assertIsNone(self.store.kv_get(export.KV_CONFLICT))
        self.assertEqual(self.store.kv_get(export.KV_TOKEN), self.read_own()["write_token"])
        # the same with no remembered token at all, even with a different hostname (renamed computer)
        self.store.kv_set(export.KV_TOKEN, None)
        self.foreign_file("RENAMED", fp=FP)
        self.assertEqual(self.write(NOW + 800, force=True), export.WRITTEN)

    def test_different_host_fp_is_a_conflict(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        self.foreign_file("OFFICE-PC", fp=OTHER_FP, written_at=NOW + 100)     # same hostname, other hardware
        self.store.add_usage(rec(10, inp=1))
        self.assertEqual(self.write(NOW + 400, doc=self.build(NOW + 400)), export.CONFLICT)
        self.store.kv_set(export.KV_CONFLICT, None)
        self.store.kv_set(export.KV_TOKEN, None)
        self.assertEqual(self.write(NOW + 800, force=True), export.CONFLICT)

    def test_token_is_committed_before_the_file_is_replaced(self):
        seen = {}

        def crash(path, data):
            seen["token"] = self.store.kv_get(export.KV_TOKEN)
            raise KeyboardInterrupt("crash between the token and the file")
        with mock.patch.object(peers, "atomic_write_bytes", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.write(NOW)
        self.assertTrue(seen["token"])
        self.assertFalse(self.own_path().exists())
        # crash after the replace but before the commit: the token in the file is unknown to the store
        self.assertEqual(self.write(NOW + 400, force=True), export.WRITTEN)
        self.store.kv_set(export.KV_TOKEN, "restoredfrombkup")
        self.store.kv_set(export.KV_WRITE, NOW - 10000.0)
        self.assertEqual(self.write(NOW + 800, force=True), export.WRITTEN)
        self.assertIsNone(self.store.kv_get(export.KV_CONFLICT))

    def test_failed_write_restores_the_remembered_state(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        token = self.store.kv_get(export.KV_TOKEN)
        with mock.patch.object(peers, "atomic_write_bytes", side_effect=PermissionError("locked")):
            self.assertEqual(self.write(NOW + 400, force=True), "write failed (PermissionError)")
        self.assertEqual(self.store.kv_get(export.KV_TOKEN), token)
        self.assertEqual(self.read_own()["write_token"], token)

    def test_adoption_on_a_different_hostname_exports_normally(self):
        self.foreign_file("OLD-LAPTOP")           # an older file (no host_fp), other hostname, no token here
        self.assertEqual(self.adopt_write(NOW), export.WRITTEN)
        self.assertEqual(self.store.kv_get(export.KV_ADOPT_SEEDED), ID)
        self.assertIsNone(self.store.kv_get(export.KV_CONFLICT))
        self.store.add_usage(rec(10, inp=1))
        self.assertEqual(self.adopt_write(NOW + 400), export.WRITTEN)     # keeps working
        # the special case is one-off: a foreign writer afterwards is still a conflict
        self.foreign_file("OLD-LAPTOP", written_at=NOW + 900)
        self.store.add_usage(rec(11, inp=1))
        self.assertEqual(self.adopt_write(NOW + 1000), export.CONFLICT)

    def test_adoption_flag_off_still_conflicts_on_other_hostname(self):
        self.foreign_file("OLD-LAPTOP")
        self.assertEqual(self.adopt_write(NOW, adopted=False), export.CONFLICT)

    def test_adoption_seed_is_per_machine_id(self):
        self.store.kv_set(export.KV_ADOPT_SEEDED, OTHER_ID)       # left over from an older id
        self.foreign_file("OLD-LAPTOP")
        self.assertEqual(self.adopt_write(NOW), export.WRITTEN)


class HostFingerprintTests(unittest.TestCase):
    def test_hashed_salted_and_never_the_raw_id(self):
        import hashlib
        from collector import util
        raw = "1b4e28ba-2fa1-11d2-883f-b9a761bde3fb"
        with mock.patch.object(util, "_raw_machine_id", return_value=raw):
            fp = util.host_fingerprint()
            self.assertRegex(fp, r"^[0-9a-f]{16}$")
            self.assertNotEqual(fp, raw)
            self.assertNotIn(raw.replace("-", "")[:16], fp)
            self.assertEqual(fp, hashlib.sha256(("%s:%s" % (util.HOST_FP_SALT, raw)).encode())
                             .hexdigest()[:16])
        with mock.patch.object(util, "_raw_machine_id", return_value="another-id"):
            self.assertNotEqual(util.host_fingerprint(), fp)

    def test_fallback_when_no_machine_id(self):
        from collector import util
        with mock.patch.object(util, "_raw_machine_id", return_value=""):
            fp = util.host_fingerprint()
        self.assertRegex(fp, r"^[0-9a-f]{16}$")

    def test_real_machine_id_is_stable_and_not_exported(self):
        from collector import util
        self.assertEqual(util.host_fingerprint(), util.host_fingerprint())
        raw = util._raw_machine_id()
        self.assertNotEqual(util.host_fingerprint(), raw)

    def test_export_header_carries_it_and_validation_is_optional(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(export, "host_fp", return_value=FP):
            doc = export.build_export({}, {}, CFG, NOW, "0.2.0")
        self.assertEqual(doc["host_fp"], FP)
        clean, status = validate.parse_machine(dict(doc, written_at=iso(NOW)), ID, NOW)
        self.assertEqual((clean["host_fp"], status), (FP, "ok"))
        for bad in ("XYZ", "0f1e2d3c4b5a69781", "0F1E2D3C4B5A6978", 5, None):
            c, _ = validate.parse_machine(dict(doc, written_at=iso(NOW), host_fp=bad), ID, NOW)
            self.assertEqual(c["host_fp"], "")
        legacy = {k: v for k, v in doc.items() if k != "host_fp"}
        c, st = validate.parse_machine(dict(legacy, written_at=iso(NOW)), ID, NOW)
        self.assertEqual((c["host_fp"], st), ("", "ok"))


class TombstoneTests(Base):
    def test_removed_stops_export(self):
        self.assertEqual(self.write(NOW), export.WRITTEN)
        before = self.own_path().read_bytes()
        peers.write_control(self.folder, ID, "removed", "Office PC", OTHER_ID, NOW + 10)
        self.assertEqual(self.write(NOW + 4000, force=True), export.REMOVED)
        self.assertEqual(self.own_path().read_bytes(), before)
        self.assertIn("token-burn sync enable", export.REMOVED_MESSAGE)

    def test_retired_still_exports(self):
        peers.write_control(self.folder, ID, "retired", "Office PC", OTHER_ID, NOW)
        self.assertEqual(self.write(NOW), export.WRITTEN)

    def test_deleting_tombstone_allows_export(self):
        peers.write_control(self.folder, ID, "removed", None, OTHER_ID, NOW)
        self.assertEqual(self.write(NOW), export.REMOVED)
        self.assertTrue(peers.delete_control(self.folder, ID))
        self.assertEqual(self.write(NOW), export.WRITTEN)


class AdoptedTests(Base):
    def make_adopted(self, store_fill, now=NOW):
        """adopted-<id>.json.gz built from a second store, as the installer would copy it."""
        other = Store(Path(self.tmp.name) / "adopted.db")
        try:
            store_fill(other)
            pay = {"claude": build_platform_payload(other, "claude", now, INFO)}
            pay["claude"]["installed"] = True
            doc = export.build_export(pay, {"claude": []}, CFG, now, "0.1.0")
        finally:
            other.close()
        self.data.mkdir(exist_ok=True)
        (self.data / ("adopted-%s.json.gz" % ID)).write_bytes(export._gzip_bytes(doc))

    def test_per_cell_max_union_and_runs(self):
        def fill(st):
            st.add_usage(rec(10, inp=500, out=10, requests=5))       # larger than local
            st.add_usage(rec(30, inp=77))                           # only in adopted
            st.add_usage(rec(40, inp=5, model="old-model"))
            st.upsert_subagent_run(SubagentRun("claude", "raw-old", "explore", "n", "s", "m", "", at(30), "completed", 3, 2))
        self.make_adopted(fill)
        self.store.add_usage(rec(10, inp=100, out=90))
        self.store.add_usage(rec(20, inp=9))
        doc = self.build(data_dir=self.data)
        series = {s["key"]: s for s in doc["platforms"]["claude"]["series"]}
        m = series["m"]
        cells = dict(zip(m["m"], zip(m["input"], m["output"], m["requests"])))
        self.assertEqual(cells[START_MIN + 10], (500, 90, 5))        # max per field
        self.assertEqual(cells[START_MIN + 20], (9, 0, 1))
        self.assertEqual(cells[START_MIN + 30][0], 77)
        self.assertIn("old-model", series)
        self.assertEqual(len(doc["platforms"]["claude"]["subagent_runs"]), 1)

    def test_adopted_alone_when_platform_absent_locally(self):
        self.make_adopted(lambda st: st.add_usage(rec(10, inp=500)))
        pay = {"claude": {"installed": False, "status": "not installed"}}
        doc = export.build_export(pay, {}, CFG, NOW, "0.2.0", data_dir=self.data)
        self.assertTrue(doc["platforms"]["claude"]["installed"])
        self.assertEqual(doc["platforms"]["claude"]["series"][0]["input"], [500])

    def test_adopted_deleted_once_all_minutes_expired(self):
        self.make_adopted(lambda st: st.add_usage(rec(10, inp=500)))
        path = self.data / ("adopted-%s.json.gz" % ID)
        self.build(data_dir=self.data)
        self.assertTrue(path.exists())                               # still in the window
        later = NOW + 31 * 86400
        pay = {"claude": {"installed": False, "status": "not installed"}}
        export.build_export(pay, {}, CFG, later, "0.2.0", data_dir=self.data)
        self.assertFalse(path.exists())

    def test_unreadable_adopted_is_kept_and_ignored(self):
        self.data.mkdir()
        path = self.data / ("adopted-%s.json.gz" % ID)
        path.write_bytes(b"not gzip")
        self.build(data_dir=self.data)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
