import contextlib
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from collector import util
from collector.sync import config, peers
from token_burn import data_cli, hooks_register as hr, installer, paths
from token_burn import install_location as il
from tests.test_install_location import make_app
from tests.wp3_support import FakeConsole, Isolated

PEER_A = "a1" * 16
PEER_B = "b2" * 16
VERSION = installer.app_version()


class InstallerCase(Isolated):
    def run_install(self, argv, answers=(), wizard=None):
        """Run `install` with scripted answers. wizard=True pretends stdin is a terminal."""
        con = FakeConsole(answers)
        if wizard is not None:
            self.interactive.return_value = wizard
        rc = installer.run_install(self.parse("install", *argv), con)
        return rc, con

    def cfg(self, data=None):
        return config.load(data or self.data)

    def frozen(self, src):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "frozen", True, create=True))
        stack.enter_context(mock.patch.object(sys, "executable", str(Path(src) / "token-burn.exe")))
        self.addCleanup(stack.close)
        return stack

    def data_reset(self):
        import shutil
        shutil.rmtree(str(self.data), ignore_errors=True)
        self.backend.reset_mock()

    def assert_installed(self):
        self.backend.install.assert_called()
        self.assertTrue(hr.status()["claude"] and hr.status()["cursor"])

    def assert_not_installed(self):
        self.backend.install.assert_not_called()
        self.assertFalse(hr.status()["claude"] or hr.status()["cursor"])


class TestWizardFlows(InstallerCase):
    def test_this_computer_only(self):
        rc, con = self.run_install([], ["", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        text = "\n".join(con.prompts)
        self.assertIn("Press Enter to accept, or type C to change them:", text)
        self.assertIn("Do you use Token Burn on more than one computer?", text)
        self.assertIn("1) This computer only", text)
        self.assertIn("2) Start a new shared folder (first computer)", text)
        self.assertIn("3) Join an existing shared folder (I already set up another computer)", text)
        self.assertIn("Choose 1, 2 or 3 [1]:", text)
        self.assertIn("Install now? [Y/n]", text)
        self.assertIn("Welcome to Token Burn. It shows your Claude Code, Cursor and opencode token usage "
                      "on a private page at http://127.0.0.1:8421/. Nothing leaves this computer unless you "
                      "choose a shared folder in the next step.", con.text)
        self.assertIn("Sharing:        this computer only", con.text)
        self.assertIn("Background tasks: every minute, plus the dashboard at log on", con.text)
        self.assertEqual(self.cfg()["sync"], {"enabled": False})
        self.assertEqual(self.cfg()["app_version"], VERSION)
        self.assertEqual(self.cfg()["data_dir"], str(self.data))
        self.assert_installed()
        self.browser.assert_called_once()
        self.collect_now.assert_not_called()
        self.assertIn("Done. Dashboard: http://127.0.0.1:8421/", con.text)

    def test_confirm_declined_changes_nothing(self):
        rc, con = self.run_install([], ["", "1", "n"], wizard=True)
        self.assertEqual(rc, 3)
        self.assert_not_installed()
        self.assertFalse((self.data / "config.json").exists())

    def test_input_closed_is_a_cancel(self):
        rc, con = self.run_install([], [""], wizard=True)
        self.assertEqual(rc, 3)
        self.assert_not_installed()

    def test_new_shared_folder(self):
        shared = self.make_shared()
        rc, con = self.run_install([], ["", "2", str(shared), "Office PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertTrue(s["enabled"])
        self.assertEqual((s["machine_name"], s["adopted"]), ("Office PC", False))
        self.assertEqual(Path(s["folder"]), shared)
        self.assertTrue(config.valid_machine_id(s["machine_id"]))
        root = shared / "token-burn"
        self.assertTrue((root / "machines").is_dir() and (root / "control").is_dir())
        self.assertIn("Token Burn", (root / "README.txt").read_text())
        self.collect_now.assert_called_once()
        if sys.platform.startswith("win"):  # OneDrive pinning (attrib +P) is Windows only
            self.pin.assert_called_once()
            self.assertIn("Asking OneDrive to always keep these files on this computer.", con.text)
        else:
            self.pin.assert_not_called()
        self.assertIn("Found these synced folders:", con.text)
        self.assertIn("Shared folder:  %s" % (shared / "token-burn"), con.text)
        self.assertIn("This computer:  Office PC", con.text)
        self.assertIn("On your other computers, run the installer and choose 'Join an existing shared "
                      "folder', then pick the same folder.", con.text)
        self.assertIn("Press Enter to choose a folder in a window, type a number above, or type or paste a "
                      "folder path:", "\n".join(con.prompts))

    def test_number_picks_detected_root_and_picker_used_on_enter(self):
        self.picker.return_value = self.make_shared("Picked")
        rc, con = self.run_install([], ["", "2", "", "N1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        title, initial = self.picker.call_args[0]
        self.assertEqual(title, "Choose a synced folder for Token Burn")
        self.assertEqual(Path(initial), self.onedrive)
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), self.onedrive / "Picked")

    def test_picker_unavailable_falls_back_to_typed_path_and_join_title(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop")
        rc, con = self.run_install([], ["", "3", "", str(shared), "Desktop", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.picker.call_args[0][0], "Choose the folder you picked on your other computer")
        self.assertIn("The folder window is not available or was cancelled.", con.text)

    def test_number_selects_root(self):
        rc, con = self.run_install([], ["", "2", "1", "N", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), self.onedrive)

    def test_join_lists_existing_computers(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop", os_name="macOS 14", age_s=240)
        self.write_peer(shared, PEER_B, "Server", os_name="Linux", age_s=3 * 3600)
        rc, con = self.run_install([], ["", "3", str(shared), "Desktop", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Computers already using this folder:", con.text)
        self.assertRegex(con.text, r"Laptop\s+macOS 14\s+last seen 4 min ago")
        self.assertRegex(con.text, r"Server\s+Linux\s+last seen 3 h ago")
        s = config.sync_settings(self.cfg())
        self.assertEqual(s["machine_name"], "Desktop")
        self.assertFalse(s["adopted"])
        self.assertNotIn("On your other computers", con.text)

    def test_adopt_by_hostname(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Office PC", hostname="OFFICE-PC")
        self.write_peer(shared, PEER_B, "Laptop", hostname="LAPTOP")
        with mock.patch.object(installer, "short_hostname", return_value="OFFICE-PC"):
            rc, con = self.run_install([], ["", "3", str(shared), "y", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Is this computer 'Office PC' (for example, you reinstalled Windows or Token Burn)? "
                      "Its history will stay under one name. [Y/n]", "\n".join(con.prompts))
        s = config.sync_settings(self.cfg())
        self.assertEqual((s["machine_id"], s["machine_name"], s["adopted"]), (PEER_A, "Office PC", True))
        self.assertNotIn("Name for this computer", "\n".join(con.prompts))
        self.assertTrue((self.data / ("adopted-%s.json.gz" % PEER_A)).is_file())

    def test_adopt_declined_gets_new_identity_and_name_prompt(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Office PC", hostname="OFFICE-PC")
        with mock.patch.object(installer, "short_hostname", return_value="OFFICE-PC"):
            rc, con = self.run_install([], ["", "3", str(shared), "n", "Office PC 2", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertNotEqual(s["machine_id"], PEER_A)
        self.assertEqual((s["machine_name"], s["adopted"]), ("Office PC 2", False))
        self.assertFalse(list(self.data.glob("adopted-*")))

    def test_duplicate_name_refused_then_accepted(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop", age_s=3 * 3600)
        rc, con = self.run_install([], ["", "3", str(shared), "laptop", "Desktop", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("The name 'laptop' is already used by another computer (last seen 3 h ago). Please "
                      "choose a different name.", con.text)
        self.assertEqual(config.sync_settings(self.cfg())["machine_name"], "Desktop")

    def test_name_default_is_short_hostname_and_invalid_name_reprompts(self):
        shared = self.make_shared()
        with mock.patch.object(installer, "short_hostname", return_value="OFFICE-PC"):
            rc, con = self.run_install([], ["", "2", str(shared), "x" * 41, "", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Name for this computer [OFFICE-PC]:", "\n".join(con.prompts))
        self.assertEqual(config.sync_settings(self.cfg())["machine_name"], "OFFICE-PC")

    def test_not_synced_warning_declined_then_accepted(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        rc, con = self.run_install([], ["", "2", str(plain), "n", str(plain), "y", "PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        warn = "This folder does not look like it is synced to your other computers."
        self.assertEqual("\n".join(con.prompts).count(warn), 2)
        self.assertIn("[y/N]", [p for p in con.prompts if warn in p][0])
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), plain)

    def test_nonexistent_folder_create_prompt(self):
        target = self.onedrive / "NewFolder"
        rc, con = self.run_install([], ["", "2", str(target), "n", str(target), "", "PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("That folder does not exist. Create it? [Y/n]", "\n".join(con.prompts))
        self.assertTrue((target / "token-burn" / "machines").is_dir())

    def test_work_onedrive_notice(self):
        work = self.home / "OneDrive - Contoso"
        work.mkdir()
        with mock.patch.dict("os.environ", {"OneDriveCommercial": str(work)}):
            rc, con = self.run_install([], ["", "2", str(work), "n", str(work), "", "PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        notice = [p for p in con.prompts if "This is a work OneDrive." in p]
        self.assertEqual(len(notice), 2)
        self.assertIn("Use it anyway? [Y/n]", notice[0])
        self.assertIn("never prompts, code or passwords", notice[0])
        self.assertNotIn("does not look like it is synced", "\n".join(con.prompts))

    def test_new_mode_on_used_folder_offers_to_join(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop")
        rc, con = self.run_install([], ["", "2", str(shared), "", "Desktop", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("This folder is already used by 1 computers. Join them instead? [Y/n]",
                      "\n".join(con.prompts))
        self.assertIn("Computers already using this folder:", con.text)
        self.assertNotIn("On your other computers", con.text)

    def test_join_with_nothing_found(self):
        empty = self.make_shared("Empty")
        q = ("No Token Burn files were found in this folder. Check you picked the same folder as on your other "
             "computer (it contains a 'token-burn' folder). Start a new shared folder here instead? [y/N]")
        rc, con = self.run_install([], ["", "3", str(empty), "y", "PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn(q, "\n".join(con.prompts))
        self.assertIn("On your other computers", con.text)
        other = self.make_shared("Other")
        self.write_peer(other, PEER_A, "Laptop")
        self.data_reset()
        rc, con = self.run_install([], ["", "3", str(empty), "n", str(other), "Desk", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), other)

    def test_unwritable_shared_folder_asks_again(self):
        bad, good = self.make_shared("Bad"), self.make_shared("Good")
        real = peers.ensure_layout

        def layout(folder):
            if Path(folder) == bad:
                raise PermissionError("read-only")
            return real(folder)
        with mock.patch.object(peers, "ensure_layout", side_effect=layout):
            rc, con = self.run_install([], ["", "2", str(bad), str(good), "PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Cannot write to this folder (read-only). Choose another folder.", con.text)
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), good)

    def test_stale_computer_retired(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "MacBook", age_s=12 * 86400)
        rc, con = self.run_install([], ["", "3", str(shared), "y", "Desk", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("'MacBook' has not reported for 12 days. If you replaced it, retire it so it stops "
                      "showing a warning (its history still counts). Retire it now? [y/N]",
                      "\n".join(con.prompts))
        self.assertEqual(peers.read_control(str(shared), PEER_A)["state"], "retired")

    def test_tombstone_means_new_id_and_tombstone_deleted(self):
        shared = self.make_shared()
        old_id = "c3" * 16
        self.write_peer(shared, PEER_A, "Laptop")
        config.save(self.data, {"version": 1, "sync": {"enabled": True, "folder": str(shared),
                                                       "machine_id": old_id, "machine_name": "Old"}})
        peers.write_control(str(shared), old_id, "removed", None, PEER_A, time.time())
        rc, con = self.run_install([], ["2", "1", str(shared), "", "Old", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertNotEqual(s["machine_id"], old_id)
        self.assertIsNone(peers.read_control(str(shared), old_id))


class TestRepairMenu(InstallerCase):
    def existing(self, **sync):
        sync.setdefault("enabled", True)
        config.save(self.data, {"version": 1, "app_version": "0.1.0", "sync": sync})

    def test_menu_text_and_repair_keeps_sync(self):
        shared = self.make_shared()
        self.existing(folder=str(shared), machine_id=PEER_A, machine_name="Office PC", joined_at="x")
        rc, con = self.run_install([], ["1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Token Burn is already set up here. Sync: on, folder %s, this computer 'Office PC'" % shared,
                      con.text)
        prompt = con.prompts[0]
        for item in ("1) Keep these settings and repair", "2) Change sync settings", "3) Change data folder",
                     "4) Cancel"):
            self.assertIn(item, prompt)
        s = config.sync_settings(self.cfg())
        self.assertEqual((s["enabled"], s["machine_id"], s["machine_name"]), (True, PEER_A, "Office PC"))
        self.assertEqual(self.cfg()["app_version"], VERSION)
        self.assert_installed()
        self.assertNotIn("Do you use Token Burn on more than one computer?", "\n".join(con.prompts))

    def test_menu_shows_sync_off(self):
        self.existing(enabled=False)
        rc, con = self.run_install([], ["1", ""], wizard=True)
        self.assertIn("Sync: off.", con.text)

    def test_cancel(self):
        self.existing(enabled=False)
        rc, con = self.run_install([], ["4"], wizard=True)
        self.assertEqual(rc, 3)
        self.assert_not_installed()

    def test_change_sync_settings(self):
        shared = self.make_shared("Second")
        self.existing(folder=str(self.make_shared("First")), machine_id=PEER_A, machine_name="Office PC")
        rc, con = self.run_install([], ["2", "1", str(shared), "Office PC", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertEqual(Path(s["folder"]), shared)
        self.assertEqual(s["machine_id"], PEER_A)        # the identity survives a folder change

    def test_non_interactive_reinstall_of_existing_keeps_sync(self):
        shared = self.make_shared()
        self.existing(folder=str(shared), machine_id=PEER_A, machine_name="Office PC")
        rc, con = self.run_install(["--yes"], [])
        self.assertEqual(rc, 0, con.text)
        self.assertTrue(config.sync_settings(self.cfg())["enabled"])


class TestNonInteractive(InstallerCase):
    def setUp(self):
        Isolated.setUp(self)
        p = mock.patch("builtins.input", side_effect=AssertionError("input() must not be called"))
        p.start()
        self.addCleanup(p.stop)

    def test_plain_install_without_terminal_is_this_computer_only(self):
        rc, con = self.run_install(["--no-open"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(con.prompts, [])
        self.assertEqual(self.cfg()["sync"], {"enabled": False})
        self.assert_installed()
        self.browser.assert_not_called()

    def test_yes_means_no_sync_even_with_a_terminal(self):
        rc, con = self.run_install(["--yes"], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(con.prompts, [])
        self.assertFalse(config.sync_settings(self.cfg())["enabled"])

    def test_no_sync_and_sync_folder_flags_skip_the_wizard_with_a_terminal(self):
        rc, con = self.run_install(["--no-sync"], wizard=True)
        self.assertEqual((rc, con.prompts), (0, []))
        shared = self.make_shared()
        rc, con = self.run_install(["--sync-folder", str(shared), "--machine-name", "PC"], wizard=True)
        self.assertEqual((rc, con.prompts), (0, []))

    def test_sync_folder_new(self):
        shared = self.make_shared()
        rc, con = self.run_install(["--sync-folder", str(shared), "--new", "--machine-name", "Office PC", "--yes",
                                    "--no-open"])
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertEqual((s["enabled"], s["machine_name"]), (True, "Office PC"))
        self.assertTrue((shared / "token-burn" / "machines").is_dir())
        self.collect_now.assert_called_once()
        self.assertEqual(con.prompts, [])

    def test_token_burn_subfolder_is_normalised_to_parent(self):
        shared = self.make_shared()
        rc, con = self.run_install(["--sync-folder", str(shared / "token-burn"), "--new", "--machine-name", "PC"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(Path(self.cfg()["sync"]["folder"]), shared)

    def test_auto_mode_joins_when_computers_exist(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop")
        rc, con = self.run_install(["--sync-folder", str(shared), "--machine-name", "Desktop"])
        self.assertEqual(rc, 0, con.text)
        self.assertNotIn("On your other computers", con.text)
        self.assertEqual(config.sync_settings(self.cfg())["machine_name"], "Desktop")

    def test_missing_folder_is_created(self):
        target = self.onedrive / "Made"
        rc, con = self.run_install(["--sync-folder", str(target), "--new", "--machine-name", "PC"])
        self.assertEqual(rc, 0, con.text)
        self.assertTrue((target / "token-burn" / "control").is_dir())

    def test_duplicate_name_refused(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Laptop")
        rc, con = self.run_install(["--sync-folder", str(shared), "--machine-name", "LAPTOP"])
        self.assertEqual(rc, 3)
        self.assertIn("already used by another computer", con.text)
        self.assert_not_installed()

    def test_join_with_nothing_found_is_refused(self):
        shared = self.make_shared()
        rc, con = self.run_install(["--sync-folder", str(shared), "--join", "--machine-name", "PC"])
        self.assertEqual(rc, 3)
        self.assert_not_installed()

    def test_adopt(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Office PC", hostname="OFFICE-PC")
        rc, con = self.run_install(["--sync-folder", str(shared), "--adopt", "office pc"])
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(self.cfg())
        self.assertEqual((s["machine_id"], s["machine_name"], s["adopted"]), (PEER_A, "Office PC", True))
        self.assertTrue((self.data / ("adopted-%s.json.gz" % PEER_A)).is_file())

    def test_adopt_unknown_name_refused(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Office PC")
        rc, con = self.run_install(["--sync-folder", str(shared), "--adopt", "Nobody"])
        self.assertEqual(rc, 3)

    def test_bad_argument_combinations_exit_2(self):
        shared = str(self.make_shared())
        for argv in (["--new"], ["--join"], ["--machine-name", "X"], ["--adopt", "X"],
                     ["--no-sync", "--sync-folder", shared], ["--sync-folder", shared, "--adopt", "X", "--new"],
                     ["--no-sync", "--machine-name", "X"]):
            rc, con = self.run_install(argv)
            self.assertEqual(rc, 2, argv)
        self.assert_not_installed()

    def test_invalid_machine_name_exit_2(self):
        shared = self.make_shared()
        rc, con = self.run_install(["--sync-folder", str(shared), "--new", "--machine-name", "x" * 50])
        self.assertEqual(rc, 2)

    def test_no_start_skips_server_and_scheduler_check(self):
        rc, con = self.run_install(["--no-start", "--no-open"])
        self.assertEqual(rc, 0)
        self.backend.start_server.assert_not_called()
        self.backend.check_refresh.assert_not_called()
        self.backend.install.assert_called_once()

    def test_data_dir_flag_forbidden_with_env_var(self):
        rc, con = self.run_install(["--data-dir", str(self.tmp / "elsewhere")])
        self.assertEqual(rc, 3)
        self.assertIn("TOKEN_BURN_DATA_DIR", con.text)


class TestDataFolderChoice(InstallerCase):
    data_env = False

    def test_wizard_change_data_folder_writes_pointer(self):
        chosen = self.tmp / "MyData"
        rc, con = self.run_install([], ["c", str(chosen), "", "", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(util.read_location(), chosen)
        self.assertEqual(paths.data_dir(), chosen)
        self.assertEqual(config.load(chosen)["data_dir"], str(chosen))
        self.assertIn("Where should Token Burn keep its data?", "\n".join(con.prompts))
        self.assertIn("Data:    %s" % util.standard_data_dir(), con.text)

    def test_wizard_refuses_synced_data_folder_then_accepts(self):
        good = self.tmp / "Good"
        rc, con = self.run_install([], ["c", str(self.onedrive / "TB"), str(good), "", "", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Keep the data folder on this computer", con.text)
        self.assertEqual(util.read_location(), good)

    def test_default_data_folder_leaves_no_pointer(self):
        rc, con = self.run_install([], ["", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIsNone(util.read_location())
        self.assertTrue((util.standard_data_dir() / "config.json").is_file())

    def test_data_dir_flag(self):
        chosen = self.tmp / "FlagData"
        rc, con = self.run_install(["--data-dir", str(chosen)])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(util.read_location(), chosen)
        self.assertTrue((chosen / "config.json").is_file())

    def test_data_dir_flag_refusals(self):
        rc, con = self.run_install(["--data-dir", str(self.onedrive / "TB")])
        self.assertEqual(rc, 3)
        self.assertIsNone(util.read_location())
        rc, con = self.run_install(["--data-dir", str(self.faketemp / "d")])
        self.assertEqual(rc, 3)
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "a.txt").write_text("x")
        rc, con = self.run_install(["--data-dir", str(foreign)])
        self.assertEqual(rc, 3)

    def test_repair_menu_change_data_folder_moves_existing_data(self):
        std = util.standard_data_dir()
        std.mkdir(parents=True)
        (std / "usage.db").write_bytes(b"x")
        config.save(std, {"version": 1, "sync": {"enabled": False}})
        chosen = self.tmp / "Moved"
        with mock.patch.object(data_cli, "move_data") as move:
            rc, con = self.run_install([], ["3", str(chosen), "", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        move.assert_called_once()
        self.assertEqual(Path(move.call_args[0][1]), chosen)
        self.assertEqual(move.call_args[1]["delete_old"], "ask")


class TestFrozenInstall(InstallerCase):
    def setUp(self):
        Isolated.setUp(self)
        self.src = make_app(self.tmp / "unzipped")
        self.target = self.tmp / "Programs" / "Token Burn"
        self.frozen(self.src)

    def test_temp_folder_refused(self):
        tmp_app = make_app(self.faketemp / "token-burn")
        self.frozen(tmp_app)
        rc, con = self.run_install([], [], wizard=True)
        self.assertEqual(rc, 3)
        self.assertIn("Token Burn is running from a temporary folder, probably straight from the zip. Please "
                      "unzip the whole folder first, to %s, then run it again from there." % il.recommended_dir(),
                      con.text)
        self.assert_not_installed()
        self.assertEqual(con.prompts, [])

    def test_missing_internal_folder_refused(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        (bare / "token-burn.exe").write_bytes(b"x")
        self.frozen(bare)
        rc, con = self.run_install(["--yes"])
        self.assertEqual(rc, 3)
        self.assert_not_installed()

    def test_copy_then_register_everything_at_the_target(self):
        rc, con = self.run_install(["--install-dir", str(self.target), "--yes", "--no-open"])
        self.assertEqual(rc, 0, con.text)
        self.assertTrue((self.target / "token-burn.exe").is_file())
        self.assertTrue((self.target / "_internal" / "lib.dll").is_file())
        self.assertTrue(self.src.exists())
        refresh, serve = self.backend.install.call_args[0]
        launcher = "token-burnw.exe" if sys.platform.startswith("win") else "token-burn.exe"  # windowless twin
        self.assertEqual(Path(refresh[0]), self.target / launcher)
        self.assertEqual(refresh[1:], ["collect"])
        self.assertEqual(Path(serve[0]), self.target / launcher)
        settings = (self.home / ".claude" / "settings.json").read_text()
        self.assertIn(str(self.target).replace("\\", "/") + "/token-burn.exe", settings)
        self.assertNotIn(str(self.src).replace("\\", "/"), settings)
        hooks = (self.home / ".cursor" / "hooks.json").read_text()
        self.assertIn(str(self.target).replace("\\", "/") + "/token-burn.exe", hooks)
        cfg = self.cfg()
        self.assertEqual(cfg["install_dir"], str(self.target))
        self.assertEqual(cfg["app_version"], VERSION)
        if sys.platform.startswith("win"):  # Apps and features entry and Start Menu shortcut
            self.winreg.CreateKeyEx.assert_called_once()
            self.assertTrue(il.start_menu_shortcut().is_file())
        else:
            self.winreg.CreateKeyEx.assert_not_called()
            if sys.platform != "darwin":  # Linux gets a desktop entry; macOS has no OS integration
                self.assertTrue(il.desktop_entry_path().is_file())
        self.backend.check_refresh.assert_called_once()

    def test_yes_without_install_dir_uses_the_recommended_folder(self):
        rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 0, con.text)
        self.assertTrue((il.recommended_dir() / "token-burn.exe").is_file())
        self.assertEqual(self.cfg()["install_dir"], str(il.recommended_dir()))

    def test_running_from_the_target_skips_the_copy(self):
        self.frozen(self.src)
        with mock.patch.object(il, "stage_copy") as stage:
            rc, con = self.run_install(["--install-dir", str(self.src), "--yes"])
        self.assertEqual(rc, 0, con.text)
        stage.assert_not_called()
        self.assertEqual(self.cfg()["install_dir"], str(self.src))

    def test_refused_install_folders_exit_3(self):
        for bad in (self.faketemp / "x", self.home / "Downloads" / "tb", self.onedrive / "tb"):
            rc, con = self.run_install(["--install-dir", str(bad), "--yes"])
            self.assertEqual(rc, 3, bad)
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "f.txt").write_text("x")
        rc, con = self.run_install(["--install-dir", str(foreign), "--yes"])
        self.assertEqual(rc, 3)
        self.assert_not_installed()

    def test_wizard_folder_screen_default_and_change(self):
        rc, con = self.run_install([], ["", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Token Burn will use these folders:", con.text)
        self.assertIn("  Program: %s" % il.recommended_dir(), con.text)
        self.assertTrue((il.recommended_dir() / "token-burn.exe").is_file())
        self.data_reset()
        import shutil
        shutil.rmtree(str(il.recommended_dir()))
        mine = self.tmp / "MyApps" / "TB"
        rc, con = self.run_install([], ["c", str(mine), "", "", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Where should Token Burn be installed?\n  Recommended: %s" % il.recommended_dir(),
                      "\n".join(con.prompts))
        self.assertIn("Press Enter to use the recommended folder, type B to browse for a folder, or type or "
                      "paste a folder path:", "\n".join(con.prompts))
        self.assertTrue((mine / "token-burn.exe").is_file())

    def test_wizard_browse_uses_picker_with_install_title(self):
        mine = self.tmp / "Picked" / "TB"
        self.picker.return_value = mine
        rc, con = self.run_install([], ["c", "b", "", "", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(self.picker.call_args[0][0], "Choose where to install Token Burn")
        self.assertTrue((mine / "token-burn.exe").is_file())

    def test_wizard_refusal_reprompts(self):
        mine = self.tmp / "OK" / "TB"
        rc, con = self.run_install([], ["c", str(self.onedrive / "TB"), str(mine), "", "", "1", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("must not live in a synced folder", con.text)
        self.assertTrue((mine / "token-burn.exe").is_file())


class TestUpgrade(InstallerCase):
    def setUp(self):
        Isolated.setUp(self)
        self.target = make_app(self.tmp / "Programs" / "Token Burn")
        (self.target / "token-burn.exe").write_bytes(b"old exe")
        (self.target / "old-only.txt").write_text("x")
        il.write_manifest(self.target, "0.1.0")          # a 0.2 install owns old-only.txt, so the upgrade removes it
        self.data.mkdir()
        (self.data / "usage.db").write_bytes(b"data")
        config.save(self.data, {"version": 1, "app_version": "0.1.0", "install_dir": str(self.target),
                                "sync": {"enabled": True, "folder": str(self.make_shared()),
                                         "machine_id": PEER_A, "machine_name": "Office PC"}})
        self.src = make_app(self.tmp / "newzip")
        (self.src / "token-burn.exe").write_bytes(b"new exe")
        self.frozen(self.src)

    def test_wizard_upgrade_prompt_and_replace(self):
        rc, con = self.run_install([], ["1", "y", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertIn("Token Burn 0.1.0 is installed in %s. Upgrade it to %s? [Y/n]" % (self.target, VERSION),
                      "\n".join(con.prompts))
        self.assertEqual((self.target / "token-burn.exe").read_bytes(), b"new exe")
        self.assertFalse((self.target / "old-only.txt").exists())
        self.assertEqual((self.data / "usage.db").read_bytes(), b"data")
        s = config.sync_settings(self.cfg())
        self.assertEqual((s["enabled"], s["machine_id"]), (True, PEER_A))
        self.assertEqual(self.cfg()["app_version"], VERSION)
        self.backend.uninstall.assert_called()          # tasks stopped for the swap
        installer.proc.stop_server.assert_called()
        self.backend.install.assert_called()            # and registered again
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["Token Burn"])

    def test_upgrade_declined(self):
        rc, con = self.run_install([], ["1", "n"], wizard=True)
        self.assertEqual(rc, 3)
        self.assertEqual((self.target / "token-burn.exe").read_bytes(), b"old exe")
        self.backend.install.assert_not_called()

    def test_non_interactive_upgrade_needs_no_questions(self):
        with mock.patch("builtins.input", side_effect=AssertionError("input() called")):
            rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual((self.target / "token-burn.exe").read_bytes(), b"new exe")
        self.assertEqual(con.prompts, [])

    def test_locked_files_report_folder_and_restore_old_tasks(self):
        err = il.LockedError(self.target)
        with mock.patch.object(il, "upgrade_files", side_effect=err):
            rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 1)
        self.assertIn(str(self.target), con.text)
        self.assertIn("a program is using them", con.text)
        self.assertIn("previous version is still installed", con.text)
        self.assertEqual((self.target / "token-burn.exe").read_bytes(), b"old exe")
        self.backend.install.assert_called_once()       # the old version's tasks were put back
        self.assertEqual(Path(self.backend.install.call_args[0][0][0]).parent, self.target)

    def test_same_version_wording(self):
        cfg = self.cfg()
        cfg["app_version"] = VERSION
        config.save(self.data, cfg)
        rc, con = self.run_install([], ["1", "n"], wizard=True)
        self.assertIn("Replace it with this copy?", "\n".join(con.prompts))


class TestSchedulerCheck(InstallerCase):
    def setUp(self):
        Isolated.setUp(self)
        self.src = make_app(self.tmp / "unzipped")
        self.frozen(self.src)
        self.backend.check_refresh.return_value = (False, "the task failed with error 0x80070002")

    def test_failure_in_custom_folder_offers_recommended(self):
        custom = self.tmp / "Custom" / "TB"
        self.backend.check_refresh.side_effect = [(False, "the task failed with error 0x80070002"), (True, "ok")]
        rc, con = self.run_install(["--install-dir", str(custom)], ["1", "", "y"], wizard=True)
        self.assertEqual(rc, 0, con.text)
        q = [p for p in con.prompts if "Move Token Burn to the recommended folder" in p]
        self.assertEqual(len(q), 1)
        self.assertIn("0x80070002", con.text)
        self.assertTrue((il.recommended_dir() / "token-burn.exe").is_file())
        self.assertFalse(custom.exists())
        self.assertEqual(self.cfg()["install_dir"], str(il.recommended_dir()))
        self.assertEqual(Path(self.backend.install.call_args[0][0][0]).parent, il.recommended_dir())

    def test_failure_declined_keeps_custom_folder(self):
        custom = self.tmp / "Custom" / "TB"
        rc, con = self.run_install(["--install-dir", str(custom)], ["1", "", "n"], wizard=True)
        self.assertEqual(rc, 0, con.text)
        self.assertTrue((custom / "token-burn.exe").is_file())
        self.assertFalse(il.recommended_dir().exists())

    def test_failure_without_terminal_only_reports(self):
        custom = self.tmp / "Custom" / "TB"
        rc, con = self.run_install(["--install-dir", str(custom), "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("The background task did not run", con.text)
        self.assertEqual(con.prompts, [])

    def test_check_exceptions_never_fail_the_install(self):
        self.backend.check_refresh.side_effect = RuntimeError("boom")
        rc, con = self.run_install(["--install-dir", str(self.tmp / "TB"), "--yes"])
        self.assertEqual(rc, 0, con.text)


class TestUninstall(InstallerCase):
    def uninstall(self, argv=(), answers=(), tty=False):
        con = FakeConsole(answers)
        self.interactive.return_value = tty
        rc = installer.run_uninstall(self.parse("uninstall", *argv), con)
        return rc, con

    def setup_install(self, frozen=True):
        self.target = make_app(self.tmp / "Programs" / "Token Burn")
        self.data.mkdir()
        (self.data / "usage.db").write_bytes(b"d")
        shared = self.make_shared()
        config.save(self.data, {"version": 1, "install_dir": str(self.target),
                                "sync": {"enabled": True, "folder": str(shared), "machine_id": PEER_A,
                                         "machine_name": "Office PC"}})
        self.shared = shared
        self.write_peer(shared, PEER_A, "Office PC")
        hr.install_claude("x token-burn hook claude")

    def test_removes_everything_but_keeps_data_and_shared_history(self):
        self.setup_install()
        with mock.patch.object(sys, "platform", "win32"):
            il.register_os_integration(self.target, VERSION)
            rc, con = self.uninstall(["--yes"])
        self.assertEqual(rc, 0, con.text)
        self.backend.uninstall.assert_called_once()
        self.assertFalse(hr.status()["claude"])
        self.winreg.DeleteKey.assert_called_once()
        self.assertFalse(il.start_menu_shortcut().exists())
        self.assertFalse(self.target.exists())
        self.assertTrue((self.data / "usage.db").exists())
        self.assertTrue((self.shared / "token-burn" / "machines" / ("%s.json.gz" % PEER_A)).exists())
        self.assertIn("Your history in the shared folder was kept, so other computers still show it. To remove "
                      "it, run token-burn sync remove \"Office PC\" on any computer, or use "
                      "--remove-from-shared-folder now.", con.text)

    def test_asks_before_deleting_program_folder_and_data(self):
        self.setup_install()
        rc, con = self.uninstall(["--purge-data"], ["y", "n"], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertFalse(self.data.exists())
        self.assertTrue(self.target.exists())
        prompts = "\n".join(con.prompts)
        self.assertIn("Delete the data folder", prompts)
        self.assertIn("Also delete the program folder %s? [Y/n]" % self.target, prompts)
        self.assertIn("Program folder kept", con.text)

    def test_program_folder_deleted_on_yes_answer(self):
        self.setup_install()
        rc, con = self.uninstall([], [""], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.data / "usage.db").exists())

    def test_non_interactive_without_yes_keeps_program_folder(self):
        self.setup_install()
        rc, con = self.uninstall([])
        self.assertEqual((rc, con.prompts), (0, []))
        self.assertTrue(self.target.exists())

    def test_purge_declined_keeps_data(self):
        self.setup_install()
        rc, con = self.uninstall(["--purge-data"], ["n", "n"], tty=True)
        self.assertTrue((self.data / "usage.db").exists())
        self.assertIn("Data kept at", con.text)

    def test_remove_from_shared_folder(self):
        self.setup_install()
        rc, con = self.uninstall(["--remove-from-shared-folder", "--yes"])
        self.assertEqual(rc, 0, con.text)
        self.assertFalse((self.shared / "token-burn" / "machines" / ("%s.json.gz" % PEER_A)).exists())
        self.assertEqual(peers.read_control(str(self.shared), PEER_A)["state"], "removed")
        self.assertNotIn("history in the shared folder was kept", con.text)

    def test_interactive_flag_prompts_and_waits(self):
        self.setup_install()
        rc, con = self.uninstall(["--interactive"], ["", ""], tty=False)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(con.prompts[-1], "Press Enter to close this window. ")
        self.assertFalse(self.target.exists())

    def test_pip_install_touches_no_os_integration(self):
        rc, con = self.uninstall(["--yes"])
        self.assertEqual(rc, 0)
        self.winreg.DeleteKey.assert_not_called()

    def test_purge_refuses_dangerous_paths(self):
        self.assertFalse(installer._purge_ok(self.home))
        self.assertFalse(installer._purge_ok(Path(self.home.anchor)))
        self.assertTrue(installer._purge_ok(self.data))


class TestUninstallPointer(InstallerCase):
    data_env = False

    def test_uninstall_keeps_location_pointer_when_data_is_kept(self):
        chosen = self.tmp / "MyData"
        chosen.mkdir()
        (chosen / "usage.db").write_bytes(b"x")
        util.write_location(chosen)
        con = FakeConsole()
        rc = installer.run_uninstall(self.parse("uninstall"), con)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(util.read_location(), chosen)       # a reinstall finds the same folder and ID
        self.assertTrue((chosen / "usage.db").exists())

    def test_purge_removes_location_pointer(self):
        chosen = self.tmp / "MyData"
        chosen.mkdir()
        (chosen / "usage.db").write_bytes(b"x")
        util.write_location(chosen)
        con = FakeConsole()
        rc = installer.run_uninstall(self.parse("uninstall", "--purge-data"), con)
        self.assertEqual(rc, 0, con.text)
        self.assertIsNone(util.read_location())
        self.assertFalse(chosen.exists())

    def test_purge_deletes_the_chosen_folder(self):
        chosen = self.tmp / "MyData"
        chosen.mkdir()
        (chosen / "usage.db").write_bytes(b"x")
        util.write_location(chosen)
        con = FakeConsole()
        installer.run_uninstall(self.parse("uninstall", "--purge-data"), con)
        self.assertFalse(chosen.exists())
        self.assertIsNone(util.read_location())


class TestUpgradeFailures(InstallerCase):
    """Upgrade problems after the background tasks were stopped."""
    setUp = TestUpgrade.setUp

    def test_copy_failure_restores_tasks_and_exits_with_a_message(self):
        with mock.patch.object(il.shutil, "copytree", side_effect=OSError("no space left")):
            rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 1)
        self.assertIn("no space left", con.text)
        self.assertIn("previous version is still installed", con.text)
        self.assertEqual((self.target / "token-burn.exe").read_bytes(), b"old exe")
        self.backend.install.assert_called_once()           # tasks and server for the old version restored
        self.backend.start_server.assert_called()
        self.assertEqual(Path(self.backend.install.call_args[0][0][0]).parent, self.target)
        self.assertNotIn("Traceback", con.text)

    def test_raw_oserror_from_the_swap_is_handled_too(self):
        with mock.patch.object(il, "upgrade_files", side_effect=OSError("disk error")):
            rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 1)
        self.assertIn("previous version is still installed", con.text)
        self.backend.install.assert_called_once()

    def test_failed_rollback_is_reported_honestly_and_tasks_are_not_reregistered(self):
        old = self.target.parent / "Token Burn.old-20261105-101010"
        err = il.RollbackError(old, self.target, "Upgrading failed.")
        with mock.patch.object(il, "upgrade_files", side_effect=err):
            rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 1)
        self.assertIn(str(old), con.text)
        self.assertIn(str(self.target), con.text)
        self.assertIn("NOT in place", con.text)
        self.assertNotIn("previous version is still installed", con.text)
        self.backend.install.assert_not_called()             # would point at a missing folder
        self.backend.start_server.assert_not_called()

    def test_upgrade_marks_the_folder_with_the_new_version(self):
        rc, con = self.run_install(["--yes", "--no-open"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(il.read_manifest(self.target)["version"], VERSION)

    def test_a_folder_with_other_files_is_refused_as_an_install_folder(self):
        """A recorded install folder that also holds someone's files is not treated as ours."""
        unmanaged = self.tmp / "Tools"
        unmanaged.mkdir()
        (unmanaged / "token-burn.exe").write_bytes(b"x")
        (unmanaged / "git.exe").write_bytes(b"precious")
        rc, con = self.run_install(["--yes", "--no-open", "--install-dir", str(unmanaged)])
        self.assertEqual(rc, 3)
        self.assertIn("not empty", con.text)
        self.assertEqual((unmanaged / "git.exe").read_bytes(), b"precious")


class TestIdReuse(InstallerCase):
    data_env = False

    def test_installing_into_a_folder_that_holds_an_identity_reuses_it(self):
        kept = self.tmp / "KeptData"
        kept.mkdir()
        shared = self.make_shared()
        config.save(kept, {"version": 1, "app_version": "0.2.0", "data_dir": str(kept),
                           "sync": {"enabled": True, "folder": str(shared), "machine_id": PEER_A,
                                    "machine_name": "Office PC", "joined_at": "2026-11-05T03:00:00Z"}})
        rc, con = self.run_install(["--data-dir", str(kept), "--sync-folder", str(shared), "--yes", "--no-open",
                                    "--no-start"])
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(config.load(kept))
        self.assertEqual(s["machine_id"], PEER_A)
        self.assertEqual(s["machine_name"], "Office PC")
        self.assertEqual(util.read_location(), kept)

    def test_reinstall_after_uninstall_keeps_the_pointer_and_the_id(self):
        kept = self.tmp / "KeptData"
        kept.mkdir()
        (kept / "usage.db").write_bytes(b"x")
        shared = self.make_shared()
        config.save(kept, {"version": 1, "app_version": "0.2.0",
                           "sync": {"enabled": True, "folder": str(shared), "machine_id": PEER_A,
                                    "machine_name": "Office PC"}})
        util.write_location(kept)
        con = FakeConsole()
        self.assertEqual(installer.run_uninstall(self.parse("uninstall"), con), 0, con.text)
        self.assertEqual(util.read_location(), kept)
        rc, con = self.run_install(["--sync-folder", str(shared), "--yes", "--no-open", "--no-start"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(config.sync_settings(config.load(kept))["machine_id"], PEER_A)

    def test_wizard_adopt_prompt_defaults_to_yes(self):
        shared = self.make_shared()
        self.write_peer(shared, PEER_A, "Office PC", hostname="OFFICE-PC")
        with mock.patch.object(installer, "short_hostname", return_value="OFFICE-PC"):
            rc, con = self.run_install([], ["", "3", str(shared), "", ""], wizard=True)
        self.assertEqual(rc, 0, con.text)
        s = config.sync_settings(config.load(il.standard_data_dir()))
        self.assertEqual((s["machine_id"], s["adopted"]), (PEER_A, True))


class TestUninstallSafety(InstallerCase):
    uninstall = TestUninstall.uninstall
    setup_install = TestUninstall.setup_install

    def test_purge_deletes_only_token_burn_files(self):
        self.setup_install()
        (self.data / "holiday-photos.jsonl").write_text("not ours")
        (self.data / "notes.txt").write_text("not ours")
        (self.data / "collect.log").write_text("ours")
        rc, con = self.uninstall(["--purge-data", "--yes"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["holiday-photos.jsonl", "notes.txt"])
        self.assertIn("other files", con.text)

    def test_program_folder_with_other_files_keeps_them(self):
        self.setup_install()
        (self.target / "my-own-notes.txt").write_text("keep")
        il.write_manifest(self.target, VERSION, ["token-burn.exe", "token-burnw.exe", "_internal", "README.md"])
        rc, con = self.uninstall(["--yes"])
        self.assertEqual(rc, 0, con.text)
        self.assertEqual([p.name for p in self.target.iterdir()], ["my-own-notes.txt"])

    def test_interactive_uninstall_deletes_the_folder_only_after_the_pause(self):
        self.setup_install()
        events = []
        real_ask = FakeConsole.ask

        def ask(con, prompt, default=""):
            events.append("ask: " + prompt)
            return real_ask(con, prompt, default)
        with mock.patch.object(FakeConsole, "ask", ask), \
                mock.patch.object(il, "remove_program_dir", side_effect=lambda *a, **k: events.append("remove")):
            rc, con = self.uninstall(["--interactive"], ["y", ""], tty=True)
        self.assertEqual(rc, 0, con.text)
        self.assertEqual(events[-2][:20], "ask: Press Enter to ")
        self.assertEqual(events[-1], "remove")
        self.assertEqual(events.count("remove"), 1)

    def test_non_interactive_uninstall_removes_the_folder_straight_away(self):
        self.setup_install()
        events = []
        with mock.patch.object(il, "remove_program_dir", side_effect=lambda *a, **k: events.append("remove")):
            rc, con = self.uninstall(["--yes"])
        self.assertEqual(events, ["remove"])


class TestIsInteractive(unittest.TestCase):
    """installer.is_interactive is patched out by Isolated; these tests use the real one with mocks."""

    def stdin(self, tty):
        s = mock.MagicMock()
        s.isatty.return_value = tty
        return s

    def test_posix_uses_isatty(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.object(sys, "stdin", self.stdin(True)):
                self.assertTrue(installer.is_interactive())
            with mock.patch.object(sys, "stdin", self.stdin(False)):
                self.assertFalse(installer.is_interactive())

    def test_windows_nul_redirect_is_not_interactive(self):
        """isatty() is true for NUL on Windows; GetConsoleMode tells a real console from it."""
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "stdin", self.stdin(True)):
            with mock.patch.object(installer, "_windows_console_stdin", return_value=False):
                self.assertFalse(installer.is_interactive())
            with mock.patch.object(installer, "_windows_console_stdin", return_value=True):
                self.assertTrue(installer.is_interactive())
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "stdin", self.stdin(False)):
            with mock.patch.object(installer, "_windows_console_stdin", return_value=True) as w:
                self.assertFalse(installer.is_interactive())
            w.assert_not_called()

    def test_console_mode_check_uses_the_stdin_handle(self):
        import ctypes
        kernel = mock.MagicMock()
        kernel.GetStdHandle.return_value = 7
        with mock.patch.object(ctypes, "windll", mock.MagicMock(kernel32=kernel), create=True):
            kernel.GetConsoleMode.return_value = 1
            self.assertTrue(installer._windows_console_stdin())
            kernel.GetStdHandle.assert_called_with(-10)
            kernel.GetConsoleMode.return_value = 0               # NUL or a pipe
            self.assertFalse(installer._windows_console_stdin())
            kernel.GetStdHandle.return_value = 0                 # no stdin handle at all
            self.assertFalse(installer._windows_console_stdin())


class TestHelpers(unittest.TestCase):
    def test_ago(self):
        self.assertEqual(installer.ago(10), "just now")
        self.assertEqual(installer.ago(240), "4 min ago")
        self.assertEqual(installer.ago(3 * 3600), "3 h ago")
        self.assertEqual(installer.ago(12 * 86400), "12 days ago")

    def test_wizard_wanted(self):
        class A:
            yes = sync_folder = None
            no_sync = False
        a = A()
        with mock.patch.object(installer, "is_interactive", return_value=True):
            self.assertTrue(installer.wizard_wanted(a))
            a.yes = True
            self.assertFalse(installer.wizard_wanted(a))
            a.yes, a.sync_folder = False, "x"
            self.assertFalse(installer.wizard_wanted(a))
            a.sync_folder, a.no_sync = None, True
            self.assertFalse(installer.wizard_wanted(a))
        with mock.patch.object(installer, "is_interactive", return_value=False):
            a.no_sync = False
            self.assertFalse(installer.wizard_wanted(a))

    def test_is_interactive_handles_missing_stdin(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(installer.is_interactive())

    def test_console_yn_and_choose_defaults(self):
        con = FakeConsole(["", "x", "Yes", "n", "9", "2", ""])
        self.assertTrue(con.yn("q", True))
        self.assertFalse(con.yn("q", False))       # 'x' gives the default
        self.assertTrue(con.yn("q", False))
        self.assertFalse(con.yn("q", True))
        self.assertEqual(con.choose("pick", 3, 1), 2)       # 9 is out of range and asks again
        self.assertEqual(con.choose("pick", 3, 1), 1)

    def test_collect_now_in_process_or_child(self):
        from collector import collect
        with mock.patch.object(collect, "run") as run:
            self.assertTrue(installer.collect_now(util.DATA_DIR))
            run.assert_called_once()
        with mock.patch.object(installer.subprocess if hasattr(installer, "subprocess") else __import__("subprocess"),
                               "run") as sub:
            self.assertTrue(installer.collect_now(Path(util.DATA_DIR) / "other"))
        env = sub.call_args[1]["env"]
        self.assertEqual(env["TOKEN_BURN_DATA_DIR"], str(Path(util.DATA_DIR) / "other"))


class TestWizardPause(InstallerCase):
    def test_press_enter_to_close(self):
        con = FakeConsole(["", "1", "", ""])
        self.interactive.return_value = True
        rc = installer.run_wizard_and_pause(self.parse("install"), con)
        self.assertEqual(rc, 0)
        self.assertEqual(con.prompts[-1], "Press Enter to close this window. ")

    def test_pause_after_refusal_too(self):
        con = FakeConsole([""])
        with mock.patch.object(installer, "run_install", return_value=3):
            self.assertEqual(installer.run_wizard_and_pause(self.parse("install"), con), 3)
        self.assertEqual(len(con.prompts), 1)


if __name__ == "__main__":
    unittest.main()
