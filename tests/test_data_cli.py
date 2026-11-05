import json
import os
import sqlite3
from pathlib import Path
from unittest import mock

from collector import util
from collector.store import Store
from collector.sync import config
from token_burn import data_cli, installer, paths
from tests.test_install_location import make_app
from tests.wp3_support import FakeConsole, Isolated


def make_data(folder, rows=3):
    """A small but real data folder: database, config, logs and a peer cache."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    st = Store(folder / "usage.db")
    for i in range(rows):
        st.kv_set("key%d" % i, "value%d" % i)
    st.db.execute("INSERT INTO usage_minute(platform, minute, session_id, agent_kind, agent_id, model, effort, "
                  "input, output, cache_read, cache_write, cost, requests) "
                  "VALUES('claude', 29000000, 's', 'primary', '', 'm', '', 1, 2, 3, 4, 0.5, 1)")
    st.db.commit()
    st.close()
    (folder / "config.json").write_text(json.dumps({"version": 1, "data_dir": str(folder),
                                                    "sync": {"enabled": False}}))
    (folder / "collect.log").write_text("log")
    (folder / "collect.log.1").write_text("old log")
    (folder / "claude-subagents.jsonl").write_text('{"a":1}\n')
    (folder / "cursor-subagent-events.jsonl").write_text('{"b":2}\n')
    (folder / ("adopted-%s.json.gz" % ("a" * 32))).write_bytes(b"gz")
    (folder / "peer-cache").mkdir()
    (folder / "peer-cache" / ("%s.json.gz" % ("b" * 32))).write_bytes(b"cache")
    (folder / "keep-me.txt").write_text("not ours")
    return folder


def counts(path):
    c = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: c.execute('SELECT COUNT(*) FROM "%s"' % n).fetchone()[0] for n in names}
    finally:
        c.close()


class DataCase(Isolated):
    data_env = False

    def run_data(self, *argv, answers=(), tty=False):
        con = FakeConsole(answers)
        self.interactive.return_value = tty
        rc = data_cli.cmd_data(self.parse("data", *argv), con)
        return rc, con


class TestResolutionOrder(DataCase):
    def test_default_pointer_env(self):
        self.assertEqual(paths.data_dir(), util.standard_data_dir())
        self.assertEqual(util.default_data_dir(), util.standard_data_dir())
        chosen = self.tmp / "Pointed"
        util.write_location(chosen)
        self.assertEqual(paths.data_dir(), chosen)
        with mock.patch.dict(os.environ, {"TOKEN_BURN_DATA_DIR": str(self.tmp / "FromEnv")}):
            self.assertEqual(paths.data_dir(), self.tmp / "FromEnv")
            self.assertEqual(util.default_data_dir(), self.tmp / "FromEnv")

    def test_invalid_pointer_falls_back_to_default_and_status_reports_it(self):
        for bad in ('{"version": 1, "data_dir": "relative/path"}', "{not json", '{"version": 2, "data_dir": "C:\\\\x"}'):
            util.location_file().parent.mkdir(parents=True, exist_ok=True)
            util.location_file().write_text(bad)
            self.assertEqual(paths.data_dir(), util.standard_data_dir())
            rc, con = self.run_data("status")
            self.assertEqual(rc, 0)
            self.assertIn("not valid, so the default folder is used", con.text)


class TestStatus(DataCase):
    def test_default_folder_missing(self):
        rc, con = self.run_data("status")
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Data folder: %s" % util.standard_data_dir(), con.text)
        self.assertIn("Chosen by: default for this computer", con.text)
        self.assertIn("The folder does not exist yet.", con.text)

    def test_pointer_size_and_database(self):
        chosen = make_data(self.tmp / "Chosen")
        util.write_location(chosen)
        rc, con = self.run_data("status")
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Chosen by: location.json", con.text)
        self.assertRegex(con.text, r"Size: [\d.]+ (bytes|KB|MB) in \d+ files")
        self.assertIn("usage.db present", con.text)

    def test_environment_variable(self):
        with mock.patch.dict(os.environ, {"TOKEN_BURN_DATA_DIR": str(self.tmp / "E")}):
            rc, con = self.run_data("status")
        self.assertIn("environment variable TOKEN_BURN_DATA_DIR", con.text)

    def test_human_size(self):
        self.assertEqual(data_cli.human_size(12), "12 bytes")
        self.assertEqual(data_cli.human_size(1500), "1.5 KB")
        self.assertEqual(data_cli.human_size(2500000), "2.5 MB")


class TestMove(DataCase):
    def setUp(self):
        Isolated.setUp(self)
        self.old = make_data(util.standard_data_dir())
        self.new = self.tmp / "NewData"
        self.before = counts(self.old / "usage.db")

    def test_copies_verifies_points_restarts_and_keeps_old(self):
        rc, con = self.run_data("move", str(self.new))
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(counts(self.new / "usage.db"), self.before)
        self.assertGreater(sum(self.before.values()), 0)
        for name in ("config.json", "collect.log", "collect.log.1", "claude-subagents.jsonl",
                     "cursor-subagent-events.jsonl", "adopted-%s.json.gz" % ("a" * 32)):
            self.assertTrue((self.new / name).is_file(), name)
        self.assertEqual((self.new / "peer-cache" / ("%s.json.gz" % ("b" * 32))).read_bytes(), b"cache")
        self.assertFalse((self.new / "keep-me.txt").exists())
        self.assertEqual(util.read_location(), self.new)
        self.assertEqual(config.load(self.new)["data_dir"], str(self.new))
        self.assertTrue((self.old / "usage.db").exists())                # old copy kept
        self.assertIn("The old data was kept at", con.text)
        self.assertIn("Verified the copy", con.text)
        self.backend.uninstall.assert_called_once()                       # tasks stopped first
        installer.proc.stop_server.assert_called()
        self.backend.install.assert_called_once()                         # then registered again
        self.backend.start_server.assert_called_once()
        self.assertEqual(paths.data_dir(), self.new)

    def test_yes_deletes_old_copy_but_leaves_foreign_files(self):
        rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertFalse((self.old / "usage.db").exists())
        self.assertFalse((self.old / "config.json").exists())
        self.assertFalse((self.old / "peer-cache").exists())
        self.assertTrue((self.old / "keep-me.txt").exists())
        self.assertIn("other files were left", con.text)

    def test_asks_before_deleting_old_copy(self):
        rc, con = self.run_data("move", str(self.new), answers=["", "n"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Delete the old data folder %s? [y/N]" % self.old, con.prompts[-1])
        self.assertTrue((self.old / "usage.db").exists())
        self.assertTrue(con.prompts[0].startswith("Move the data from"))
        rc, con = self.run_data("move", str(self.tmp / "Third"), answers=["", "y"], tty=True)
        self.assertEqual(rc, 0, con.text)

    def test_declining_the_move_changes_nothing(self):
        rc, con = self.run_data("move", str(self.new), answers=["n"], tty=True)
        self.assertEqual(rc, 3)
        self.assertIsNone(util.read_location())
        self.assertFalse(self.new.exists())
        self.backend.uninstall.assert_not_called()

    def test_move_back_to_standard_folder_removes_pointer(self):
        self.run_data("move", str(self.new), "--yes")
        for p in self.old.glob("keep-me.txt"):
            p.unlink()
        self.assertIsNotNone(util.read_location())
        rc, con = self.run_data("move", str(util.standard_data_dir()), "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertIsNone(util.read_location())
        self.assertFalse(util.location_file().exists())
        self.assertEqual(counts(util.standard_data_dir() / "usage.db"), self.before)

    def test_mismatch_aborts_and_leaves_everything_alone(self):
        real = data_cli._sqlite_copy

        def bad_copy(src, dst):
            real(src, dst)
            c = sqlite3.connect(str(dst))
            c.execute("DELETE FROM kv")
            c.commit()
            c.close()
        with mock.patch.object(data_cli, "_sqlite_copy", bad_copy):
            rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("does not match", con.text)
        self.assertIsNone(util.read_location())
        self.assertEqual(counts(self.old / "usage.db"), self.before)
        self.assertTrue((self.old / "config.json").exists())
        self.backend.install.assert_called_once()             # tasks restored at the old location

    def test_copy_error_aborts(self):
        with mock.patch.object(data_cli, "copy_data", side_effect=OSError("disk full")):
            rc, con = self.run_data("move", str(self.new))
        self.assertEqual(rc, 1)
        self.assertIn("disk full", con.text + "".join(con.errors))
        self.assertIsNone(util.read_location())

    def test_refusals(self):
        refused = [self.onedrive / "TB", self.faketemp / "d", "\\\\nas\\share\\tb", self.old, self.old / "sub"]
        for target in refused:
            rc, con = self.run_data("move", str(target), "--yes")
            self.assertEqual(rc, 3, target)
        self.backend.uninstall.assert_not_called()
        self.assertIsNone(util.read_location())
        # a folder already holding Token Burn data
        other = make_data(self.tmp / "Other")
        rc, con = self.run_data("move", str(other), "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("already holds Token Burn data", con.text)
        # a foreign non-empty folder
        foreign = self.tmp / "Foreign"
        foreign.mkdir()
        (foreign / "x.txt").write_text("x")
        self.assertEqual(self.run_data("move", str(foreign), "--yes")[0], 3)

    def test_refuses_folder_inside_program_folder(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        cfg = config.load(self.old)
        cfg["install_dir"] = str(prog)
        config.save(self.old, cfg)
        rc, con = self.run_data("move", str(prog / "data"), "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("inside the program folder", con.text)

    def test_refused_with_environment_variable(self):
        with mock.patch.dict(os.environ, {"TOKEN_BURN_DATA_DIR": str(self.old)}):
            rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("TOKEN_BURN_DATA_DIR", con.text)

    def test_no_database_yet_still_moves_files(self):
        (self.old / "usage.db").unlink()
        for p in self.old.glob("usage.db-*"):
            p.unlink()
        rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertTrue((self.new / "config.json").is_file())
        self.assertFalse((self.new / "usage.db").exists())

    def test_verify_copy_reports_per_table_mismatch(self):
        other = self.tmp / "other.db"
        st = Store(other)
        st.close()
        with self.assertRaises(installer.Failure) as cm:
            data_cli.verify_copy(self.old / "usage.db", other)
        self.assertIn("kv", str(cm.exception))
        self.assertEqual(data_cli.verify_copy(self.old / "usage.db", self.old / "usage.db"), self.before)

    def test_source_database_opened_read_only_for_verification(self):
        calls = []
        real = sqlite3.connect

        def spy(target, *a, **k):
            calls.append(str(target))
            return real(target, *a, **k)
        with mock.patch.object(data_cli.sqlite3, "connect", spy):
            data_cli.verify_copy(self.old / "usage.db", self.old / "usage.db")
        self.assertTrue(all("mode=ro" in c for c in calls))


class TestMoveSafety(DataCase):
    def setUp(self):
        Isolated.setUp(self)
        self.old = make_data(util.standard_data_dir())
        self.new = self.tmp / "NewData"

    def test_delete_old_removes_only_known_token_burn_files(self):
        (self.old / "holiday.jsonl").write_text("not ours")
        (self.old / "notes.log").write_text("not ours")
        (self.old / "other.db").write_text("not ours")
        rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(sorted(p.name for p in self.old.iterdir()),
                         ["holiday.jsonl", "keep-me.txt", "notes.log", "other.db"])
        self.assertTrue((self.new / "usage.db").is_file())
        self.assertIn("other files were left", con.text)

    def test_delete_old_keeps_a_config_that_is_not_ours(self):
        (self.old / "config.json").write_text('{"name": "someone else"}')
        (self.old / "keep-me.txt").unlink()
        data_cli._delete_old(self.old, ["usage.db", "config.json"], lambda m: None)
        self.assertEqual([p.name for p in self.old.iterdir()], ["config.json"])

    def test_hook_log_lines_written_during_the_move_are_carried_over(self):
        log = self.old / "claude-subagents.jsonl"
        real = data_cli.il.apply_data_dir

        def switch(path):
            with open(str(log), "a", encoding="utf-8") as f:       # a hook still wrote to the old folder
                f.write('{"late":1}\n')
            return real(path)
        with mock.patch.object(data_cli.il, "apply_data_dir", side_effect=switch):
            rc, con = self.run_data("move", str(self.new))
        self.assertEqual(rc, 0, con.text)
        self.assertEqual((self.new / "claude-subagents.jsonl").read_text(), '{"a":1}\n{"late":1}\n')
        self.assertEqual((self.new / "cursor-subagent-events.jsonl").read_text(), '{"b":2}\n')
        self.assertIn("Carried over", con.text)

    def test_log_tail_is_copied_before_the_old_copy_is_deleted(self):
        log = self.old / "claude-subagents.jsonl"
        real = data_cli.il.apply_data_dir

        def switch(path):
            with open(str(log), "a", encoding="utf-8") as f:
                f.write('{"late":2}\n')
            return real(path)
        with mock.patch.object(data_cli.il, "apply_data_dir", side_effect=switch):
            rc, con = self.run_data("move", str(self.new), "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertIn('{"late":2}', (self.new / "claude-subagents.jsonl").read_text())
        self.assertFalse(log.exists())

    def test_copy_log_tails_function(self):
        src = self.tmp / "s"
        dst = self.tmp / "d"
        src.mkdir()
        dst.mkdir()
        (src / "a.jsonl").write_bytes(b"one\ntwo\nthree\n")
        (dst / "a.jsonl").write_bytes(b"one\n")
        self.assertEqual(data_cli.copy_log_tails(src, dst, {"a.jsonl": 4, "missing.jsonl": 0}), 10)
        self.assertEqual((dst / "a.jsonl").read_bytes(), b"one\ntwo\nthree\n")
