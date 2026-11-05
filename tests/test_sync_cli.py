import json
import time
from pathlib import Path
from unittest import mock

from collector.store import Store
from collector.sync import config, peers
from token_burn import sync_cli
from tests.wp3_support import FakeConsole, Isolated

SELF_ID = "5e" * 16
PEER_A = "a1" * 16
PEER_B = "b2" * 16
PEER_C = "c3" * 16


class SyncCase(Isolated):
    def setUp(self):
        Isolated.setUp(self)
        self.shared = self.make_shared()
        self.data.mkdir()
        config.save(self.data, {"version": 1, "sync": {"enabled": True, "folder": str(self.shared),
                                                       "machine_id": SELF_ID, "machine_name": "Office PC",
                                                       "joined_at": "2026-11-05T03:00:00Z", "adopted": False}})
        self.write_peer(self.shared, PEER_A, "Laptop", os_name="macOS 14", age_s=600)
        self.write_peer(self.shared, PEER_B, "Server", os_name="Linux", age_s=3 * 86400)
        self.collect = mock.patch("collector.collect.run", return_value={"ok": True})
        self.run_mock = self.collect.start()
        self.addCleanup(self.collect.stop)

    def sync(self, *argv, answers=(), tty=False):
        con = FakeConsole(answers)
        self.interactive.return_value = tty
        rc = sync_cli.cmd_sync(self.parse("sync", *argv), con)
        return rc, con

    def sync_block(self):
        return config.sync_settings(config.load(self.data))

    def control(self, mid):
        return peers.read_control(str(self.shared), mid)

    def machine_file_exists(self, mid):
        return peers.machine_file(str(self.shared), mid).exists()


class TestStatusAndMachines(SyncCase):
    def test_status_off(self):
        config.save(self.data, {"version": 1, "sync": {"enabled": False}})
        rc, con = self.sync("status")
        self.assertEqual(rc, 0)
        self.assertIn("Sync: off", con.text)

    def test_status_on_reads_usage_json(self):
        (self.data / "usage.json").write_text(json.dumps({"sync": {
            "enabled": True, "status": "ok", "last_export": "2026-11-05T03:00:00Z",
            "messages": ["MacBook: incomplete or damaged (still syncing?); showing the last good copy"]}}))
        rc, con = self.sync("status")
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Sync: on", con.text)
        self.assertIn(str(self.shared / "token-burn"), con.text)
        self.assertIn("This computer: Office PC (id %s)" % SELF_ID[:8], con.text)
        self.assertIn("Status: ok", con.text)
        self.assertIn("Last export: 2026-11-05T03:00:00Z", con.text)
        self.assertIn("MacBook: incomplete or damaged", con.text)

    def test_status_folder_missing(self):
        config.save(self.data, {"sync": {"enabled": True, "folder": str(self.tmp / "gone"),
                                         "machine_id": SELF_ID, "machine_name": "X"}})
        rc, con = self.sync("status")
        self.assertIn("(not found)", con.text)

    def test_machines_table(self):
        peers.write_control(str(self.shared), PEER_B, "retired", "Old server", SELF_ID, time.time())
        rc, con = self.sync("machines")
        self.assertEqual(rc, 0, con.text)
        self.assertRegex(con.text, r"Name\s+OS\s+Last seen\s+State\s+ID")
        self.assertRegex(con.text, r"Office PC\s+\S.*this computer\s+active\s+%s" % SELF_ID[:8])
        self.assertRegex(con.text, r"Laptop\s+macOS 14\s+10 min ago\s+active\s+%s" % PEER_A[:8])
        self.assertRegex(con.text, r"Old server\s+Linux\s+3 days ago\s+retired\s+%s" % PEER_B[:8])

    def test_removed_computers_are_hidden(self):
        peers.write_control(str(self.shared), PEER_B, "removed", None, SELF_ID, time.time())
        rc, con = self.sync("machines")
        self.assertNotIn("Server", con.text)

    def test_commands_need_sync_on(self):
        config.save(self.data, {"version": 1, "sync": {"enabled": False}})
        for argv in (["machines"], ["rename", "X"], ["retire", "X"], ["remove", "X", "--yes"]):
            rc, con = self.sync(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("Sync is off", con.text)


class TestRename(SyncCase):
    def test_rename_self_updates_config_and_syncs(self):
        rc, con = self.sync("rename", "  Main   PC ")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.sync_block()["machine_name"], "Main PC")
        self.run_mock.assert_called_once()

    def test_rename_other_writes_control_file_and_keeps_state(self):
        peers.write_control(str(self.shared), PEER_A, "retired", None, SELF_ID, time.time())
        rc, con = self.sync("rename", "Study Mac", "--machine", "laptop")
        self.assertEqual(rc, 0, con.text)
        c = self.control(PEER_A)
        self.assertEqual((c["display_name"], c["state"], c["by"]), ("Study Mac", "retired", SELF_ID))
        self.run_mock.assert_not_called()
        rc, con = self.sync("machines")
        self.assertIn("Study Mac", con.text)

    def test_rename_self_via_machine_flag(self):
        rc, con = self.sync("rename", "HQ", "--machine", "OFFICE PC")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.sync_block()["machine_name"], "HQ")

    def test_duplicate_name_refused(self):
        rc, con = self.sync("rename", "LAPTOP")
        self.assertEqual(rc, 3)
        self.assertEqual(self.sync_block()["machine_name"], "Office PC")
        rc, con = self.sync("rename", "office pc", "--machine", "Server")
        self.assertEqual(rc, 3)
        self.assertIsNone(self.control(PEER_B))

    def test_renaming_to_own_current_name_is_allowed(self):
        self.assertEqual(self.sync("rename", "office pc")[0], 0)

    def test_invalid_name_exit_2(self):
        self.assertEqual(self.sync("rename", "x" * 41)[0], 2)
        self.assertEqual(self.sync("rename", "   ")[0], 2)

    def test_unknown_machine_exit_1(self):
        rc, con = self.sync("rename", "X", "--machine", "Nobody")
        self.assertEqual(rc, 1)
        self.assertIn("No computer named 'Nobody'", con.text + "".join(con.errors))


class TestRetire(SyncCase):
    def test_retire_and_unretire(self):
        rc, con = self.sync("retire", "server")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.control(PEER_B)["state"], "retired")
        rc, con = self.sync("retire", "server")
        self.assertIn("already retired", con.text)
        rc, con = self.sync("unretire", "Server")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.control(PEER_B)["state"], "active")

    def test_retire_keeps_control_name_override(self):
        peers.write_control(str(self.shared), PEER_B, "active", "Rack", SELF_ID, time.time())
        self.assertEqual(self.sync("retire", "Rack")[0], 0)
        self.assertEqual(self.control(PEER_B)["display_name"], "Rack")

    def test_cannot_retire_self(self):
        rc, con = self.sync("retire", "Office PC")
        self.assertEqual(rc, 3)
        self.assertIsNone(self.control(SELF_ID))


class TestRemove(SyncCase):
    def test_typed_name_confirms(self):
        rc, con = self.sync("remove", "Server", answers=["server"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Type the name to confirm:", con.prompts[0])
        self.assertFalse(self.machine_file_exists(PEER_B))
        self.assertEqual(self.control(PEER_B)["state"], "removed")
        self.assertTrue(self.machine_file_exists(PEER_A))

    def test_wrong_typed_name_removes_nothing(self):
        rc, con = self.sync("remove", "Server", answers=["Servr"], tty=True)
        self.assertEqual(rc, 3)
        self.assertTrue(self.machine_file_exists(PEER_B))
        self.assertIsNone(self.control(PEER_B))

    def test_yes_skips_the_typing(self):
        rc, con = self.sync("remove", "Server", "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(con.prompts, [])
        self.assertFalse(self.machine_file_exists(PEER_B))
        self.assertEqual(self.control(PEER_B)["state"], "removed")

    def test_without_terminal_or_yes_is_refused(self):
        with mock.patch("builtins.input", side_effect=AssertionError("input() called")):
            rc, con = self.sync("remove", "Server")
        self.assertEqual(rc, 3)
        self.assertTrue(self.machine_file_exists(PEER_B))

    def test_cannot_remove_self(self):
        rc, con = self.sync("remove", "Office PC", "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("sync disable --remove-my-data", con.text)


class TestAmbiguousNames(SyncCase):
    def setUp(self):
        SyncCase.setUp(self)
        self.write_peer(self.shared, PEER_C, "Laptop", os_name="Windows 11", age_s=100)

    def test_lists_candidates_and_refuses(self):
        rc, con = self.sync("remove", "laptop", "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("More than one computer matches 'laptop'", con.text)
        self.assertIn("id %s" % PEER_A[:8], con.text)
        self.assertIn("id %s" % PEER_C[:8], con.text)
        self.assertIn("8-character id", con.text)
        self.assertTrue(self.machine_file_exists(PEER_A) and self.machine_file_exists(PEER_C))

    def test_id_prefix_picks_one(self):
        rc, con = self.sync("remove", PEER_C[:8], "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertFalse(self.machine_file_exists(PEER_C))
        self.assertTrue(self.machine_file_exists(PEER_A))

    def test_resolve_rules(self):
        rows = sync_cli._rows(config.sync_settings(config.load(self.data)), time.time(), None)
        con = FakeConsole()
        self.assertEqual(sync_cli.resolve(con, rows, "SERVER")["id"], PEER_B)
        self.assertEqual(sync_cli.resolve(con, rows, SELF_ID[:10])["id"], SELF_ID)
        with self.assertRaises(Exception):
            sync_cli.resolve(con, rows, "zzzz")


class TestResetId(SyncCase):
    def make_kv(self):
        st = Store(self.data / "usage.db")
        for k in sync_cli.KV_KEYS + ("other",):
            st.kv_set(k, "v")
        st.db.commit()
        st.close()

    def test_new_id_clears_kv_and_leaves_old_file(self):
        self.write_peer(self.shared, SELF_ID, "Office PC")
        self.make_kv()
        st = Store(self.data / "usage.db")
        self.assertEqual(st.kv_get("sync:last_hash"), "v")
        st.close()
        rc, con = self.sync("reset-id", "--yes")
        self.assertEqual(rc, 0, con.text)
        s = self.sync_block()
        self.assertNotEqual(s["machine_id"], SELF_ID)
        self.assertTrue(config.valid_machine_id(s["machine_id"]))
        self.assertEqual(s["machine_name"], "Office PC")
        self.assertTrue(self.machine_file_exists(SELF_ID))           # the old file stays
        st = Store(self.data / "usage.db")
        try:
            self.assertTrue(all(st.kv_get(k) is None for k in sync_cli.KV_KEYS))
            self.assertEqual(st.kv_get("other"), "v")
        finally:
            st.close()
        self.run_mock.assert_called_once()

    def test_works_without_a_database(self):
        self.assertEqual(self.sync("reset-id", "--yes")[0], 0)
        self.assertFalse((self.data / "usage.db").exists())

    def test_needs_confirmation(self):
        rc, con = self.sync("reset-id")
        self.assertEqual(rc, 3)
        self.assertEqual(self.sync_block()["machine_id"], SELF_ID)
        rc, con = self.sync("reset-id", answers=["n"], tty=True)
        self.assertEqual(rc, 3)
        rc, con = self.sync("reset-id", answers=["y"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertNotEqual(self.sync_block()["machine_id"], SELF_ID)

    def test_no_sync_set_up(self):
        config.save(self.data, {"version": 1})
        self.assertEqual(self.sync("reset-id", "--yes")[0], 1)


class TestDisableEnableNow(SyncCase):
    def test_disable_keeps_id_and_folder(self):
        rc, con = self.sync("disable")
        self.assertEqual(rc, 0, con.text)
        raw = config.load(self.data)["sync"]
        self.assertFalse(raw["enabled"])
        self.assertEqual((raw["machine_id"], raw["machine_name"], raw["folder"]),
                         (SELF_ID, "Office PC", str(self.shared)))
        self.assertIn("Sync is off", con.text)

    def test_disable_when_already_off(self):
        self.sync("disable")
        rc, con = self.sync("disable")
        self.assertEqual(rc, 0)
        self.assertIn("already off", con.text)

    def test_disable_remove_my_data(self):
        self.write_peer(self.shared, SELF_ID, "Office PC")
        rc, con = self.sync("disable", "--remove-my-data", "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertFalse(self.machine_file_exists(SELF_ID))
        self.assertEqual(self.control(SELF_ID)["state"], "removed")
        self.assertFalse(self.sync_block()["enabled"])

    def test_disable_remove_my_data_needs_confirmation(self):
        self.write_peer(self.shared, SELF_ID, "Office PC")
        rc, con = self.sync("disable", "--remove-my-data")
        self.assertEqual(rc, 3)
        self.assertTrue(self.machine_file_exists(SELF_ID))
        self.assertTrue(self.sync_block()["enabled"])
        rc, con = self.sync("disable", "--remove-my-data", answers=["y"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertFalse(self.machine_file_exists(SELF_ID))

    def test_enable_with_flags(self):
        config.save(self.data, {"version": 1, "install_dir": "keep-me", "sync": {"enabled": False}})
        other = self.make_shared("Other")
        with mock.patch("builtins.input", side_effect=AssertionError("input() called")):
            rc, con = self.sync("enable", "--folder", str(other), "--new", "--machine-name", "Desk", "--yes")
        self.assertEqual(rc, 0, con.text)
        raw = config.load(self.data)
        s = config.sync_settings(raw)
        self.assertEqual((s["enabled"], s["machine_name"], Path(s["folder"])), (True, "Desk", other))
        self.assertEqual(raw["install_dir"], "keep-me")
        self.assertTrue((other / "token-burn" / "machines").is_dir())
        self.run_mock.assert_called_once()

    def test_enable_join_existing(self):
        config.save(self.data, {"version": 1})
        rc, con = self.sync("enable", "--folder", str(self.shared), "--join", "--machine-name", "Desk")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.sync_block()["machine_name"], "Desk")

    def test_enable_join_nothing_found_refused(self):
        config.save(self.data, {"version": 1})
        rc, con = self.sync("enable", "--folder", str(self.make_shared("Empty")), "--join")
        self.assertEqual(rc, 3)
        self.assertFalse(self.sync_block()["enabled"])

    def test_enable_duplicate_name_refused(self):
        config.save(self.data, {"version": 1})
        rc, con = self.sync("enable", "--folder", str(self.shared), "--machine-name", "laptop")
        self.assertEqual(rc, 3)

    def test_enable_needs_folder_without_terminal(self):
        config.save(self.data, {"version": 1})
        rc, con = self.sync("enable")
        self.assertEqual(rc, 2)

    def test_enable_interactive(self):
        config.save(self.data, {"version": 1})
        other = self.make_shared("Wizard")
        rc, con = self.sync("enable", answers=["1", str(other), "Desk"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(Path(self.sync_block()["folder"]), other)
        self.assertIn("Is this the first computer using the shared folder?", con.prompts[0])

    def test_now_collects(self):
        rc, con = self.sync("now")
        self.assertEqual(rc, 0, con.text)
        self.run_mock.assert_called_once()
        self.assertIn("Collected.", con.text)

    def test_now_when_busy(self):
        self.run_mock.return_value = None
        rc, con = self.sync("now")
        self.assertEqual(rc, 0)
        self.assertIn("Another collection is already running", con.text)

    def test_now_with_sync_off_still_collects(self):
        config.save(self.data, {"version": 1, "sync": {"enabled": False}})
        rc, con = self.sync("now")
        self.assertEqual(rc, 0)
        self.assertIn("Sync is off", con.text)
        self.run_mock.assert_called_once()


class TestSyncFixes(SyncCase):
    def test_rename_self_overrides_an_earlier_control_name(self):
        peers.write_control(str(self.shared), SELF_ID, "active", "Name a peer chose", PEER_A, time.time())
        rc, con = self.sync("rename", "Home PC")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.sync_block()["machine_name"], "Home PC")
        self.assertEqual(self.control(SELF_ID)["display_name"], "Home PC")
        self.assertEqual(self.control(SELF_ID)["state"], "active")

    def test_rename_self_without_control_file_writes_none(self):
        rc, con = self.sync("rename", "Home PC")
        self.assertEqual(rc, 0, con.text)
        self.assertIsNone(self.control(SELF_ID))

    def test_enable_on_a_computer_that_never_ran_the_installer_writes_the_header(self):
        from token_burn import installer
        (self.data / "config.json").unlink()
        folder = self.make_shared("Fresh")
        rc, con = self.sync("enable", "--folder", str(folder), "--new", "--machine-name", "Brand New", "--yes")
        self.assertEqual(rc, 0, con.text)
        cfg = config.load(self.data)
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(cfg["app_version"], installer.app_version())
        self.assertTrue(config.sync_settings(cfg)["enabled"])

    def test_enable_keeps_an_existing_config_version(self):
        cfg = config.load(self.data)
        cfg["version"] = 1
        cfg["app_version"] = "0.1.0"
        cfg["install_dir"] = "somewhere"
        config.save(self.data, cfg)
        folder = self.make_shared("Fresh")
        rc, con = self.sync("enable", "--folder", str(folder), "--new", "--machine-name", "Brand New", "--yes")
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(config.load(self.data)["install_dir"], "somewhere")

    def help_text(self, *argv):
        import contextlib
        import io
        from token_burn import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cli.build_parser().parse_args(list(argv) + ["--help"])
            except SystemExit:
                pass
        return " ".join(buf.getvalue().split())

    def test_help_texts_say_what_the_flags_do(self):
        self.assertIn("rename the computer named NAME instead of this one", self.help_text("sync", "rename"))
        self.assertNotIn("rename this computer instead of the one named NAME", self.help_text("sync", "rename"))
        self.assertIn("without asking you to type the computer's name", self.help_text("sync", "remove"))
        self.assertIn("Turning sync off asks nothing", self.help_text("sync", "disable"))
        self.assertIn("ask no questions", self.help_text("sync", "enable"))
