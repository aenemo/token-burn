"""Two computers in one process: separate data dirs and stores, one shared folder."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, sync, util
from collector.aggregate import build_platform_payload, build_usage_json
from collector.schema import UsageRecord
from collector.store import Store
from collector.sync import config, export
from collector.util import MINUTES

NOW = 1_800_000_000.0
NOW_MIN = int(NOW // 60)
START_MIN = NOW_MIN - MINUTES + 1
ID_A = "a1" * 16
ID_B = "b2" * 16
INFO = {"status": "ok", "meta": {}, "plan": {"status": "ok", "last_lookup": None, "next_lookup": None}}


def at(idx):
    return (START_MIN + idx) * 60.0 + 5


def rec(idx, platform, inp, out=0, **kw):
    d = dict(platform=platform, agent_kind="primary", agent_id="", session_id="s1", model="m",
             effort_level="", input_tokens=inp, output_tokens=out, cache_read_tokens=0,
             cache_write_tokens=0, cost_usd=0.0, timestamp=at(idx))
    d.update(kw)
    return UsageRecord(**d)


class Computer:
    def __init__(self, root, name, machine_id, folder, hostname, fp=None):
        self.data = Path(root) / name
        self.data.mkdir()
        self.name = name
        self.hostname = hostname
        self.fp = fp or hashlib.sha256(hostname.encode()).hexdigest()[:16]
        self.store = Store(self.data / "usage.db")
        cfg = {"version": 1, "sync": {"enabled": True, "folder": str(folder), "machine_id": machine_id,
                                      "machine_name": name, "joined_at": "2026-11-05T03:00:00Z",
                                      "adopted": False}}
        config.save(self.data, cfg)

    def payloads(self, installed, now=NOW):
        out = {}
        for p in ("claude", "cursor", "opencode"):
            if p in installed:
                out[p] = build_platform_payload(self.store, p, now, INFO)
                out[p]["installed"] = True
            else:
                out[p] = {"installed": False, "status": "not installed"}
        return out

    def apply(self, installed, now=NOW):
        with mock.patch.object(export, "hostname", return_value=self.hostname),                 mock.patch.object(export, "host_fp", return_value=self.fp):
            return sync.apply(self.store, self.payloads(installed, now), now, mock.Mock(), data_dir=self.data)


class TwoComputers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.folder = root / "shared"
        self.folder.mkdir()
        self.a = Computer(root, "Office PC", ID_A, self.folder, "OFFICE")
        self.b = Computer(root, "MacBook", ID_B, self.folder, "MAC")
        self.addCleanup(self.a.store.close)
        self.addCleanup(self.b.store.close)
        # Claude on both, different usage
        self.a.store.add_usage(rec(10, "claude", 100, 10))
        self.a.store.add_usage(rec(11, "claude", 1))
        self.b.store.add_usage(rec(10, "claude", 40, 4))
        self.b.store.add_usage(rec(12, "claude", 7))
        # Cursor: the same account usage appears on both
        for c in (self.a, self.b):
            c.store.add_usage(rec(20, "cursor", 500, 50))
        # opencode only on B
        self.b.store.add_usage(rec(30, "opencode", 9))

    @staticmethod
    def cell(platform_block, key, t, field):
        for s in platform_block["series"]:
            if s["key"] == key and t in s["t"]:
                return s[field][s["t"].index(t)]
        return 0

    def test_merge_and_machines_list(self):
        sets_a, sets_b = {"claude", "cursor"}, {"claude", "cursor", "opencode"}
        self.a.apply(sets_a)                                   # writes A's file
        pb, sync_b, rows_b = self.b.apply(sets_b)              # B reads A, writes B
        pa, sync_a, rows_a = self.a.apply(sets_a)             # A now reads B (write throttled)

        for payloads in (pa, pb):
            claude = payloads["claude"]
            self.assertEqual(self.cell(claude, "m", 10, "input"), 140)
            self.assertEqual(self.cell(claude, "m", 10, "output"), 14)
            self.assertEqual(self.cell(claude, "m", 11, "input"), 1)
            self.assertEqual(self.cell(claude, "m", 12, "input"), 7)
            self.assertEqual(set(claude["machines"]), {ID_A, ID_B})
            cursor = payloads["cursor"]
            self.assertEqual(self.cell(cursor, "m", 20, "input"), 500)    # not doubled
            self.assertNotIn("machines", cursor)
            oc = payloads["opencode"]
            self.assertEqual(self.cell(oc, "m", 30, "input"), 9)
            self.assertTrue(oc["installed"])
        self.assertEqual(pa["opencode"]["status"], "ok (from other computers)")
        self.assertEqual(pb["opencode"]["status"], "ok")
        self.assertEqual(set(pa["opencode"]["machines"]), {ID_B})    # breakdown with one computer having data

        for rows, self_id in ((rows_a, ID_A), (rows_b, ID_B)):
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["is_self"])
            self.assertEqual(rows[0]["id"], self_id)
            self.assertFalse(rows[1]["is_self"])
            self.assertEqual(sum(r["is_self"] for r in rows), 1)
        self.assertEqual({r["name"] for r in rows_a}, {"Office PC", "MacBook"})
        self.assertEqual(rows_a[0]["tokens_30d"], 100 + 10 + 1)
        self.assertEqual(rows_a[1]["tokens_30d"], 40 + 4 + 7 + 9)
        for s in (sync_a, sync_b):
            self.assertTrue(s["enabled"])
            self.assertEqual(s["status"], "ok")
            self.assertTrue(s["folder_found"])
            self.assertEqual(s["messages"], [])

    def test_build_usage_json_and_no_reexport_of_merged_data(self):
        sets = {"claude", "cursor"}
        self.a.apply(sets)
        self.b.apply(sets)
        # A's second pass merges B, but A's own file must still hold only A's usage
        self.a.apply(sets, NOW + 400)
        from collector.sync import peers, validate
        raw = validate.read_gz_capped(peers.machine_file(self.folder, ID_A))
        doc = json.loads(raw.decode("utf-8"))
        claude = doc["platforms"]["claude"]["series"][0]
        self.assertEqual(sum(claude["input"]), 101)
        usage = build_usage_json(self.a.payloads(sets), NOW, machines=[], sync={"enabled": True})
        self.assertIn("machines", usage)

    def test_collect_run_with_sync_enabled(self):
        sets = {"claude"}
        self.b.apply(sets)       # B's file is in the shared folder
        d = self.a.data
        plat = [("claude", lambda s, n, l, backfill=False: {}, lambda log: ("ok", {}))]
        with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                mock.patch.object(util, "USAGE_JSON", d / "usage.json"), \
                mock.patch.object(export, "hostname", return_value="OFFICE"),                 mock.patch.object(export, "host_fp", return_value=self.a.fp), \
                mock.patch.object(collect, "claude_installed", return_value=True):
            self.a.store.commit()
            usage = collect.run(now=NOW, platforms=[plat[0] + (lambda: True,)], log=mock.Mock())
        self.assertEqual(usage["sync"]["status"], "ok")
        self.assertEqual(len(usage["machines"]), 2)
        self.assertEqual(self.cell(usage["platforms"]["claude"], "m", 10, "input"), 140)
        on_disk = json.loads((d / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["machines"], usage["machines"])

    def test_folder_missing_keeps_local_data(self):
        self.a.apply({"claude"})
        import shutil
        shutil.rmtree(self.folder)
        payloads, block, rows = self.a.apply({"claude"})
        self.assertFalse(block["folder_found"])
        self.assertTrue(block["status"].startswith("shared folder not found"))
        self.assertEqual(self.cell(payloads["claude"], "m", 10, "input"), 100)
        self.assertEqual(len(rows), 1)

    def test_sync_off_and_failure(self):
        off = Path(self.tmp.name) / "off"
        off.mkdir()
        payloads = self.a.payloads({"claude"})
        out, block, rows = sync.apply(self.a.store, payloads, NOW, mock.Mock(), data_dir=off)
        self.assertIs(out, payloads)
        self.assertEqual(block, {"enabled": False})
        self.assertIsNone(rows)
        log = mock.Mock()
        with mock.patch.object(export, "build_export", side_effect=RuntimeError("boom")):
            out, block, rows = sync.apply(self.a.store, payloads, NOW, log, data_dir=self.a.data)
        self.assertIs(out, payloads)
        self.assertEqual(block, {"enabled": True, "status": "sync failed (RuntimeError)"})
        self.assertIsNone(rows)


class LostId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)     # registered first, so it runs after the stores close

    """Computer A loses its id (wiped data dir) and re-joins under a new one, re-reading the same logs."""

    def test_totals_do_not_double_and_old_id_is_superseded(self):
        ID_A2 = "a3" * 16
        root = Path(self.tmp.name)
        folder = root / "shared"
        folder.mkdir()
        a = Computer(root, "Office PC", ID_A, folder, "OFFICE")
        b = Computer(root, "MacBook", ID_B, folder, "MAC")
        self.addCleanup(a.store.close)
        self.addCleanup(b.store.close)
        for c, n in ((a, 100), (b, 40)):
            c.store.add_usage(rec(10, "claude", n))
        a.store.add_usage(rec(11, "claude", 5))
        a.apply({"claude"})
        b.apply({"claude"})
        # A is reinstalled: data dir wiped, new id, same logs re-read, same physical computer
        a.store.close()
        a2 = Computer(root, "Office PC again", ID_A2, folder, "OFFICE", fp=a.fp)
        self.addCleanup(a2.store.close)
        a2.store.add_usage(rec(10, "claude", 100))
        a2.store.add_usage(rec(11, "claude", 5))
        p2, _, rows2 = a2.apply({"claude"})
        pb, _, rowsb = b.apply({"claude"})
        cell = TwoComputers.cell
        for payloads in (p2, pb):
            self.assertEqual(cell(payloads["claude"], "m", 10, "input"), 140)    # 100 + 40, not 240
            self.assertEqual(cell(payloads["claude"], "m", 11, "input"), 5)
            total = sum(sum(s["input"]) for s in payloads["claude"]["series"])
            self.assertEqual(total, 145)
        self.assertEqual(set(p2["claude"]["machines"]), {ID_A2, ID_B})
        self.assertEqual(set(pb["claude"]["machines"]), {ID_A2, ID_B})
        old_on_b = [r for r in rowsb if r["id"] == ID_A][0]
        self.assertEqual(old_on_b["superseded_by"], ID_A2)
        self.assertFalse(old_on_b["stale"])
        old_on_a2 = [r for r in rows2 if r["id"] == ID_A][0]
        self.assertEqual(old_on_a2["superseded_by"], ID_A2)
        self.assertEqual(sum(1 for r in rowsb if r["superseded_by"]), 1)

    def test_old_id_data_missing_from_new_logs_is_kept(self):
        ID_A2 = "a3" * 16
        root = Path(self.tmp.name)
        folder = root / "shared"
        folder.mkdir()
        a = Computer(root, "Office PC", ID_A, folder, "OFFICE")
        b = Computer(root, "MacBook", ID_B, folder, "MAC")
        self.addCleanup(a.store.close)
        self.addCleanup(b.store.close)
        a.store.add_usage(rec(10, "claude", 100))
        a.apply({"claude"})
        a.store.close()
        a2 = Computer(root, "Office PC again", ID_A2, folder, "OFFICE", fp=a.fp)
        self.addCleanup(a2.store.close)
        a2.store.add_usage(rec(12, "claude", 3))      # the old transcripts have since been deleted
        p2, _, _ = a2.apply({"claude"})
        cell = TwoComputers.cell
        self.assertEqual(cell(p2["claude"], "m", 10, "input"), 100)   # history from the old id
        self.assertEqual(cell(p2["claude"], "m", 12, "input"), 3)

    def test_adopted_history_appears_in_the_adopting_computers_own_view(self):
        from collector.sync import peers
        root = Path(self.tmp.name)
        folder = root / "shared"
        folder.mkdir()
        old = Computer(root, "Old", ID_A, folder, "OLD")
        old.store.add_usage(rec(10, "claude", 100))
        old.apply({"claude"})
        new = Computer(root, "New", ID_A, folder, "NEWHOST")      # replacement computer, same id adopted
        self.addCleanup(old.store.close)
        self.addCleanup(new.store.close)
        import shutil
        shutil.copy(peers.machine_file(folder, ID_A), new.data / ("adopted-%s.json.gz" % ID_A))
        cfg = config.load(new.data)
        cfg["sync"]["adopted"] = True
        config.save(new.data, cfg)
        new.store.add_usage(rec(11, "claude", 7))
        payloads, block, _ = new.apply({"claude"})
        cell = TwoComputers.cell
        self.assertEqual(block["status"], "ok")      # adoption on a different hostname is no conflict
        self.assertEqual(cell(payloads["claude"], "m", 10, "input"), 100)   # earlier history
        self.assertEqual(cell(payloads["claude"], "m", 11, "input"), 7)
        doc = json.loads(__import__("gzip").decompress(
            peers.machine_file(folder, ID_A).read_bytes()).decode("utf-8"))
        self.assertEqual(sum(doc["platforms"]["claude"]["series"][0]["input"]), 107)


if __name__ == "__main__":
    unittest.main()
