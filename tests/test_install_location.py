import importlib
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from token_burn import paths
from token_burn import install_location as il
from tests.wp3_support import Isolated


def make_app(folder, marker="token-burn.exe", extra=True):
    """A fake unzipped PyInstaller folder."""
    folder = Path(folder)
    (folder / "_internal").mkdir(parents=True, exist_ok=True)
    (folder / marker).write_bytes(b"exe")
    (folder / "token-burnw.exe").write_bytes(b"exew")
    if extra:
        (folder / "_internal" / "lib.dll").write_bytes(b"dll")
        (folder / "README.md").write_text("readme")
    return folder


class TestRecommended(Isolated):
    def test_windows(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(il.recommended_dir(), self.home / "AppData" / "Local" / "Programs" / "Token Burn")

    def test_macos(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(il.recommended_dir(), self.home / "Applications" / "Token Burn")

    def test_linux_honours_xdg(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(il.recommended_dir(), self.home / ".local" / "share" / "token-burn")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "xdg")}):
                self.assertEqual(il.recommended_dir(), self.tmp / "xdg" / "token-burn")
            self.assertEqual(il.linux_link_path(), self.home / ".local" / "bin" / "token-burn")


class TestRunningLocation(Isolated):
    def test_missing_internal_folder_refused(self):
        d = self.tmp / "app"
        d.mkdir()
        ok, msg = il.check_running_location(d)
        self.assertFalse(ok)
        self.assertIn("temporary folder, probably straight from the zip", msg)
        self.assertIn(str(il.recommended_dir()), msg)

    def test_temp_folder_refused(self):
        d = make_app(self.faketemp / "token-burn")
        self.assertFalse(il.check_running_location(d)[0])

    def test_good_folder_accepted_and_non_frozen_skipped(self):
        d = make_app(self.tmp / "unzipped")
        self.assertEqual(il.check_running_location(d), (True, ""))
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(il.running_dir())
            self.assertEqual(il.check_running_location(), (True, ""))
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(d / "token-burn.exe")):
            self.assertEqual(il.running_dir(), d.resolve())


class TestCheckInstallDir(Isolated):
    def test_refusals(self):
        folder = self.tmp / "ok"
        self.assertEqual(il.check_install_dir(folder)[1], None)
        p, err = il.check_install_dir(self.faketemp / "x")
        self.assertIsNone(p)
        self.assertIn("temporary", err)
        _p, err = il.check_install_dir(self.home / "Downloads" / "token-burn")
        self.assertIn("Downloads", err)
        _p, err = il.check_install_dir(self.onedrive / "Apps" / "token-burn")
        self.assertIn("synced folder", err)
        _p, err = il.check_install_dir(self.tmp / "Dropbox" / "x")
        self.assertIn("Dropbox", err)
        _p, err = il.check_install_dir("\\\\server\\share\\token-burn")
        self.assertIn("network", err)
        _p, err = il.check_install_dir("   ")
        self.assertIn("No folder", err)

    def test_foreign_non_empty_refused_install_and_empty_accepted(self):
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "notes.txt").write_text("mine")
        _p, err = il.check_install_dir(foreign)
        self.assertIn("not empty", err)
        mine = make_app(self.tmp / "mine")
        self.assertEqual(il.check_install_dir(mine)[1], None)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(il.check_install_dir(empty)[1], None)
        afile = self.tmp / "afile"
        afile.write_text("x")
        self.assertIn("file", il.check_install_dir(afile)[1])

    def test_needs_write_access(self):
        with mock.patch.object(il, "is_writable", return_value=False):
            self.assertIn("administrator", il.check_install_dir(self.tmp / "x")[1])

    def test_not_inside_source_and_data_folder_not_inside(self):
        src = make_app(self.tmp / "unzipped")
        self.assertIn("inside the folder you are running", il.check_install_dir(src / "sub", src_dir=src)[1])
        self.assertEqual(il.check_install_dir(src, src_dir=src)[1], None)
        prog = self.tmp / "prog"
        self.assertIn("data folder is inside", il.check_install_dir(prog, data_dir=prog / "data")[1])

    def test_tilde_and_variables_expand(self):
        p, err = il.check_install_dir("~/TB Program")
        self.assertIsNone(err)
        self.assertEqual(p, (self.home / "TB Program").resolve())

    def test_is_writable_walks_up_to_existing_parent(self):
        self.assertTrue(il.is_writable(self.tmp / "a" / "b" / "c"))
        with mock.patch.object(il.os, "open", side_effect=PermissionError()):
            self.assertFalse(il.is_writable(self.tmp / "a"))


class TestCheckDataDir(Isolated):
    def test_refusals(self):
        _p, err = il.check_data_dir(self.faketemp / "d")
        self.assertIn("temporary", err)
        _p, err = il.check_data_dir(self.onedrive / "TB")
        self.assertIn("Keep the data folder on this computer", err)
        self.assertIn("shared folder step", err)
        _p, err = il.check_data_dir("\\\\nas\\share\\tb")
        self.assertIn("network", err)
        prog = make_app(self.tmp / "prog")
        _p, err = il.check_data_dir(prog / "data", program_dir=prog)
        self.assertIn("inside the program folder", err)

    def test_foreign_non_empty_refused_but_token_burn_data_allowed(self):
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "x.txt").write_text("x")
        self.assertIn("not empty", il.check_data_dir(foreign)[1])
        ours = self.tmp / "ours"
        ours.mkdir()
        (ours / "usage.db").write_bytes(b"db")
        (ours / "extra.log").write_text("x")
        self.assertEqual(il.check_data_dir(ours)[1], None)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(il.check_data_dir(empty)[1], None)
        self.assertEqual(il.check_data_dir(self.tmp / "new" / "deeper")[1], None)

    def test_current_folder_is_always_fine(self):
        cur = self.onedrive / "weird"
        self.assertEqual(il.check_data_dir(cur, current=cur)[1], None)


class TestCopyAndUpgrade(Isolated):
    def test_install_copies_through_temp_sibling_then_renames(self):
        src = make_app(self.tmp / "unzipped")
        dst = self.tmp / "Programs" / "Token Burn"
        seen = []
        real = il.os.rename
        il.os.rename = lambda a, b: (seen.append((Path(a).name, Path(b).name)), real(a, b))[1]
        self.addCleanup(setattr, il.os, "rename", real)
        self.assertEqual(il.install_files(src, dst), "installed")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].startswith("Token Burn.new-"))
        self.assertEqual(seen[0][1], "Token Burn")
        self.assertEqual((dst / "token-burn.exe").read_bytes(), b"exe")
        self.assertEqual((dst / "_internal" / "lib.dll").read_bytes(), b"dll")
        self.assertEqual([p.name for p in dst.parent.iterdir()], ["Token Burn"])
        self.assertTrue(src.exists())

    def test_data_folder_is_never_copied(self):
        src = make_app(self.tmp / "unzipped")
        data = src / "data"
        data.mkdir()
        (data / "usage.db").write_bytes(b"secret")
        dst = self.tmp / "target"
        il.install_files(src, dst, exclude=[data])
        self.assertFalse((dst / "data").exists())
        self.assertTrue((dst / "README.md").exists())

    def test_skip_when_already_in_place(self):
        src = make_app(self.tmp / "same")
        with mock.patch.object(il, "stage_copy") as st:
            self.assertEqual(il.install_files(src, src), "in place")
            self.assertEqual(il.upgrade_files(src, src), "in place")
        st.assert_not_called()

    def test_empty_target_folder_is_replaced(self):
        src = make_app(self.tmp / "unzipped")
        dst = self.tmp / "empty"
        dst.mkdir()
        self.assertEqual(il.install_files(src, dst), "installed")
        self.assertTrue((dst / "token-burn.exe").exists())

    def test_upgrade_replaces_files_and_keeps_data_and_config(self):
        old = make_app(self.tmp / "Programs" / "Token Burn")
        (old / "old-only.txt").write_text("gone after upgrade")
        il.write_manifest(old, "0.1.0")
        (old / "token-burn.exe").write_bytes(b"old exe")
        self.data.mkdir()
        (self.data / "usage.db").write_bytes(b"data")
        (self.data / "config.json").write_text("{}")
        new = make_app(self.tmp / "newzip")
        (new / "token-burn.exe").write_bytes(b"new exe")
        said = []
        self.assertEqual(il.upgrade_files(new, old, said.append), "upgraded")
        self.assertEqual((old / "token-burn.exe").read_bytes(), b"new exe")
        self.assertFalse((old / "old-only.txt").exists())
        self.assertEqual((self.data / "usage.db").read_bytes(), b"data")
        self.assertTrue((self.data / "config.json").exists())
        self.assertEqual([p.name for p in old.parent.iterdir()], ["Token Burn"])
        self.assertEqual(said, [])

    def test_locked_files_retry_then_succeed(self):
        old = make_app(self.tmp / "Programs" / "Token Burn")
        new = make_app(self.tmp / "newzip")
        (new / "token-burn.exe").write_bytes(b"new exe")
        real = il.os.rename
        calls = {"n": 0}

        def flaky(a, b):
            if Path(b).name.startswith("Token Burn.old-") and calls["n"] < 2:
                calls["n"] += 1
                raise PermissionError("in use")
            return real(a, b)
        said, sleeps = [], []
        with mock.patch.object(il.os, "rename", flaky):
            res = il.upgrade_files(new, old, said.append, sleep=sleeps.append, delay=0.5)
        self.assertEqual(res, "upgraded")
        self.assertEqual(sleeps, [0.5, 0.5])
        self.assertEqual(len([m for m in said if "Waiting for" in m]), 1)
        self.assertIn(str(old), said[0])
        self.assertEqual((old / "token-burn.exe").read_bytes(), b"new exe")

    def test_locked_forever_names_the_folder_and_leaves_old_install(self):
        old = make_app(self.tmp / "Programs" / "Token Burn")
        (old / "token-burn.exe").write_bytes(b"old exe")
        new = make_app(self.tmp / "newzip")
        with mock.patch.object(il.os, "rename", side_effect=PermissionError("in use")):
            with self.assertRaises(il.LockedError) as cm:
                il.upgrade_files(new, old, lambda m: None, retries=3, delay=0, sleep=lambda s: None)
        self.assertIn(str(old), str(cm.exception))
        self.assertIn("a program is using them", str(cm.exception))
        self.assertEqual((old / "token-burn.exe").read_bytes(), b"old exe")
        self.assertEqual([p.name for p in old.parent.iterdir()], ["Token Burn"])    # staged copy cleaned up

    def test_second_rename_failure_restores_old_program(self):
        old = make_app(self.tmp / "Programs" / "Token Burn")
        (old / "token-burn.exe").write_bytes(b"old exe")
        new = make_app(self.tmp / "newzip")
        real = il.os.rename

        def fail_new_into_place(a, b):
            if ".new-" in Path(a).name:
                raise PermissionError("denied")
            return real(a, b)
        with mock.patch.object(il.os, "rename", fail_new_into_place):
            with self.assertRaises(il.LockedError):
                il.upgrade_files(new, old, lambda m: None, retries=20, delay=0.05, sleep=time.sleep)
        self.assertEqual((old / "token-burn.exe").read_bytes(), b"old exe")
        self.assertEqual([p.name for p in old.parent.iterdir()], ["Token Burn"])


class TestWindowsIntegration(Isolated):
    def test_start_menu_and_apps_entry_written_and_removed(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        with mock.patch.object(sys, "platform", "win32"):
            msgs = il.register_os_integration(prog, "0.2.0")
            shortcut = self.home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Token Burn.url"
            self.assertEqual(il.start_menu_shortcut(), shortcut)
            self.assertIn("URL=http://127.0.0.1:8421/", shortcut.read_text())
            self.assertIn("[InternetShortcut]", shortcut.read_text())
            wr = self.winreg
            wr.CreateKeyEx.assert_called_once()
            self.assertEqual(wr.CreateKeyEx.call_args[0][1], il.UNINSTALL_KEY)
            self.assertTrue(il.UNINSTALL_KEY.endswith("Uninstall\\TokenBurn"))
            values = {c[0][1]: c[0][4] for c in wr.SetValueEx.call_args_list}
            self.assertEqual(values["DisplayName"], "Token Burn")
            self.assertEqual(values["DisplayVersion"], "0.2.0")
            self.assertEqual(values["Publisher"], "aenemo")
            self.assertEqual(values["InstallLocation"], str(prog))
            self.assertEqual(values["UninstallString"], '"%s" uninstall --interactive' % (prog / "token-burn.exe"))
            self.assertEqual((values["NoModify"], values["NoRepair"]), (1, 1))
            self.assertTrue(any("Apps and features" in m and "added" in m for m in msgs))
            msgs = il.unregister_os_integration(prog)
            self.assertFalse(shortcut.exists())
            wr.DeleteKey.assert_called_once_with(wr.HKEY_CURRENT_USER, il.UNINSTALL_KEY)
            self.assertTrue(any("Start Menu" in m and "removed" in m for m in msgs))

    def test_missing_registry_key_is_not_an_error(self):
        self.winreg.DeleteKey.side_effect = FileNotFoundError()
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(il.unregister_os_integration(), [])

    def test_registry_failure_reported(self):
        self.winreg.CreateKeyEx.side_effect = OSError("denied")
        with mock.patch.object(sys, "platform", "win32"):
            msgs = il.register_os_integration(self.tmp, "0.2.0")
        self.assertTrue(any("FAILED" in m for m in msgs))


class TestLinuxIntegration(Isolated):
    def test_desktop_entry_and_symlink(self):
        prog = make_app(self.tmp / "share" / "token-burn", marker="token-burn")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(il.os, "symlink") as sym:
            msgs = il.register_os_integration(prog, "0.2.0")
            entry = self.home / ".local" / "share" / "applications" / "token-burn.desktop"
            text = entry.read_text()
            self.assertIn("Name=Token Burn", text)
            self.assertIn("Exec=xdg-open http://127.0.0.1:8421/", text)
            sym.assert_called_once_with(str(prog / "token-burn"), str(self.home / ".local" / "bin" / "token-burn"))
            self.assertTrue(any("Desktop entry" in m for m in msgs))
            real_unlink = os.unlink
            # only the command link is a symlink; everything else must still resolve normally
            link = str(self.home / ".local" / "bin" / "token-burn")
            with mock.patch.object(il.os.path, "islink", side_effect=lambda p: str(p) == link), \
                    mock.patch.object(il.os, "readlink", return_value=str(prog / "token-burn")), \
                    mock.patch.object(il.os, "unlink", side_effect=lambda p: real_unlink(p) if str(p).endswith(".desktop") else None) as unlink:
                msgs = il.unregister_os_integration(prog)
            self.assertFalse(entry.exists())
            unlink.assert_any_call(str(self.home / ".local" / "bin" / "token-burn"))
            self.assertTrue(any("Command link: removed" in m for m in msgs))

    def test_macos_has_no_integration(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(il.register_os_integration(self.tmp, "0.2.0"), [])
            self.assertEqual(il.unregister_os_integration(), [])
        self.winreg.CreateKeyEx.assert_not_called()

    def test_real_symlink_replaced_only_when_ours(self):
        if os.name != "posix":
            self.skipTest("symlinks need POSIX")
        prog = make_app(self.tmp / "share" / "token-burn", marker="token-burn")
        link = self.home / ".local" / "bin" / "token-burn"
        link.parent.mkdir(parents=True)
        with mock.patch.object(sys, "platform", "linux"):
            il.register_os_integration(prog, "0.2.0")
            self.assertEqual(os.readlink(str(link)), str(prog / "token-burn"))
            il.register_os_integration(prog, "0.2.0")      # idempotent
            il.unregister_os_integration(prog)
        self.assertFalse(link.exists() or os.path.islink(str(link)))


class TestRemoveProgramDir(Isolated):
    def test_windows_defers_when_running_from_the_folder(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        said = []
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(il, "running_dir", return_value=prog), \
                mock.patch.object(il.subprocess, "Popen") as popen:
            self.assertEqual(il.remove_program_dir(prog, said.append, self.data), "scheduled")
        cmd = popen.call_args[0][0]
        self.assertIn("ping -n 3 127.0.0.1", cmd)
        self.assertIn("PID eq %d" % os.getpid(), cmd)           # waits for this process to exit first
        self.assertIn('rmdir /s /q "%s"' % prog.resolve(), cmd)
        self.assertTrue(cmd.startswith("cmd.exe"))
        self.assertFalse(popen.call_args[1].get("shell"))
        self.assertTrue(prog.exists())

    def test_windows_deletes_directly_when_not_running_from_it(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(il, "running_dir", return_value=None), \
                mock.patch.object(il.subprocess, "Popen") as popen:
            self.assertEqual(il.remove_program_dir(prog, lambda m: None, self.data), "deleted")
        popen.assert_not_called()
        self.assertFalse(prog.exists())

    def test_posix_deletes_directly(self):
        prog = make_app(self.tmp / "share" / "token-burn", marker="token-burn")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(il, "running_dir", return_value=prog):
            self.assertEqual(il.remove_program_dir(prog, lambda m: None, self.data), "deleted")
        self.assertFalse(prog.exists())

    def test_refuses_folders_that_are_not_installs(self):
        other = self.tmp / "Documents"
        other.mkdir()
        (other / "mine.txt").write_text("x")
        said = []
        self.assertEqual(il.remove_program_dir(other, said.append, self.data), "kept")
        self.assertTrue((other / "mine.txt").exists())
        self.assertEqual(il.remove_program_dir(self.home, said.append), "kept")
        self.assertEqual(il.remove_program_dir(Path(self.home.anchor), said.append), "kept")

    def test_refuses_when_data_folder_is_inside(self):
        prog = make_app(self.tmp / "prog")
        self.assertFalse(il.safe_to_delete(prog, prog / "data"))

    def test_unsafe_characters_not_passed_to_cmd(self):
        prog = make_app(self.tmp / "Tok&en %PATH% Burn")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(il, "running_dir", return_value=prog), \
                mock.patch.object(il.subprocess, "Popen") as popen:
            self.assertEqual(il.remove_program_dir(prog, lambda m: None, self.data), "kept")
        popen.assert_not_called()


class TestInstallMarker(Isolated):
    def test_folder_with_only_a_program_file_is_not_an_install(self):
        """~/.local/bin after the Linux symlink, or C:/Tools after a flat copy, must never count as an install."""
        d = self.tmp / "bin"
        d.mkdir()
        (d / "token-burn").write_bytes(b"x")
        (d / "other-tool").write_bytes(b"y")
        self.assertFalse(il.is_install(d))
        (d / "token-burn.exe").write_bytes(b"x")
        self.assertFalse(il.is_install(d))                 # still no _internal

    def test_marker_needs_internal_and_program(self):
        d = make_app(self.tmp / "app")
        il.write_manifest(d, "0.2.0")
        self.assertTrue(il.is_install(d))
        self.assertEqual(il.read_manifest(d)["version"], "0.2.0")
        self.assertIn("token-burn.exe", il.read_manifest(d)["entries"])
        self.assertNotIn(il.MARKER, il.read_manifest(d)["entries"])
        (d / "_internal" / "lib.dll").unlink()
        (d / "_internal").rmdir()
        self.assertFalse(il.is_install(d))
        only_marker = self.tmp / "marker-only"
        only_marker.mkdir()
        il.write_manifest(only_marker, "0.2.0", ["token-burn.exe"])
        self.assertFalse(il.is_install(only_marker))

    def test_symlinked_program_does_not_count(self):
        if os.name != "posix":
            self.skipTest("symlinks need POSIX")
        d = self.tmp / "bin"
        (d / "_internal").mkdir(parents=True)
        real = make_app(self.tmp / "real", marker="token-burn")
        os.symlink(str(real / "token-burn"), str(d / "token-burn"))
        self.assertFalse(il.is_install(d))

    def test_old_layout_without_marker_accepted_only_when_clean(self):
        d = make_app(self.tmp / "v01")
        self.assertTrue(il.is_install(d))                   # exe, _internal, README.md: a 0.1.x install
        (d / "my-notes.txt").write_text("mine")
        self.assertFalse(il.is_install(d))
        self.assertNotIn("my-notes.txt", il.install_entries(d))

    def test_corrupt_marker_is_ignored(self):
        d = make_app(self.tmp / "app")
        (d / "extra.txt").write_text("x")
        (d / il.MARKER).write_text("{not json")
        self.assertIsNone(il.read_manifest(d))
        self.assertFalse(il.is_install(d))
        (d / il.MARKER).write_text('{"format": "token-burn-install", "entries": ["../evil"]}')
        self.assertIsNone(il.read_manifest(d))

    def test_check_install_dir_refuses_unmanaged_folders(self):
        tools = self.tmp / "Tools"
        tools.mkdir()
        (tools / "token-burn.exe").write_bytes(b"x")
        (tools / "git.exe").write_bytes(b"x")
        path, err = il.check_install_dir(tools)
        self.assertIsNone(path)
        self.assertIn("not empty", err)
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "token-burn").write_bytes(b"link stand-in")
        (local_bin / "pip").write_bytes(b"x")
        self.assertIsNotNone(il.check_install_dir(local_bin)[1])
        self.assertFalse(il.safe_to_delete(local_bin))
        self.assertFalse(il.safe_to_delete(tools))

    def test_install_writes_marker_listing_what_was_copied(self):
        src = make_app(self.tmp / "unzipped")
        dst = self.tmp / "Programs" / "Token Burn"
        il.install_files(src, dst, version="0.2.0")
        m = il.read_manifest(dst)
        self.assertEqual(m["version"], "0.2.0")
        self.assertEqual(sorted(m["entries"]), sorted(["token-burn.exe", "token-burnw.exe", "_internal", "README.md"]))
        self.assertTrue(il.is_install(dst))
        self.assertFalse((src / il.MARKER).exists())

    def test_in_place_records_only_the_known_release_files(self):
        d = make_app(self.tmp / "flat")
        self.assertEqual(il.install_files(d, d, version="0.2.0"), "in place")
        self.assertIsNotNone(il.read_manifest(d))
        (d / "user-file.txt").write_text("keep")
        self.assertEqual(il.upgrade_files(d, d, version="0.2.1"), "in place")
        self.assertNotIn("user-file.txt", il.read_manifest(d)["entries"])
        self.assertEqual(il.foreign_entries(d), ["user-file.txt"])

    def test_upgrade_refuses_a_folder_that_is_not_an_install(self):
        other = self.tmp / "Documents"
        other.mkdir()
        (other / "token-burn.exe").write_bytes(b"x")
        (other / "thesis.docx").write_bytes(b"precious")
        new = make_app(self.tmp / "newzip")
        with self.assertRaises(il.InstallLocationError):
            il.upgrade_files(new, other)
        self.assertEqual((other / "thesis.docx").read_bytes(), b"precious")
        self.assertEqual([p.name for p in other.parent.iterdir() if p.name.startswith("Documents")], ["Documents"])

    def test_upgrade_keeps_files_that_are_not_ours(self):
        flat = make_app(self.tmp / "flat")
        (flat / "token-burn.exe").write_bytes(b"old exe")
        il.write_manifest(flat, "0.2.0", ["token-burn.exe", "token-burnw.exe", "_internal", "README.md"])
        (flat / "notes.txt").write_text("mine")
        (flat / "sub").mkdir()
        (flat / "sub" / "x.txt").write_text("mine too")
        new = make_app(self.tmp / "newzip")
        (new / "token-burn.exe").write_bytes(b"new exe")
        (new / "_internal" / "newlib.dll").write_bytes(b"n")
        self.assertEqual(il.upgrade_files(new, flat, version="0.3.0"), "upgraded")
        self.assertEqual((flat / "token-burn.exe").read_bytes(), b"new exe")
        self.assertTrue((flat / "_internal" / "newlib.dll").exists())
        self.assertEqual((flat / "notes.txt").read_text(), "mine")
        self.assertEqual((flat / "sub" / "x.txt").read_text(), "mine too")
        self.assertEqual(il.read_manifest(flat)["version"], "0.3.0")
        self.assertEqual([p.name for p in flat.iterdir() if p.name.startswith(".token-burn-old")], [])
        self.assertEqual([p.name for p in flat.parent.iterdir() if p.name.startswith("flat.")], [])

    def test_upgrade_refuses_when_a_foreign_file_has_a_release_name(self):
        flat = make_app(self.tmp / "flat", extra=False)
        il.write_manifest(flat, "0.2.0", ["token-burn.exe", "token-burnw.exe", "_internal"])
        (flat / "README.md").write_text("the user's own readme")
        (flat / "thing.txt").write_text("x")
        new = make_app(self.tmp / "newzip")
        with self.assertRaises(il.InstallLocationError) as cm:
            il.upgrade_files(new, flat)
        self.assertIn("README.md", str(cm.exception))
        self.assertEqual((flat / "README.md").read_text(), "the user's own readme")
        self.assertEqual((flat / "token-burn.exe").read_bytes(), b"exe")

    def test_stage_copy_failure_is_a_clean_error(self):
        src = make_app(self.tmp / "unzipped")
        dst = self.tmp / "Programs" / "Token Burn"
        with mock.patch.object(il.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertRaises(il.InstallLocationError) as cm:
                il.stage_copy(src, dst)
        self.assertIn("disk full", str(cm.exception))
        self.assertEqual([p.name for p in dst.parent.iterdir()], [])

    def test_failed_rollback_raises_rollback_error_naming_the_folders(self):
        """Moving the old folder aside works; putting the new one in and putting the old one back both fail."""
        old = make_app(self.tmp / "Programs" / "Token Burn")
        new = make_app(self.tmp / "newzip")
        real = il.os.rename
        calls = {"n": 0}

        def rename(a, b):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(a, b)
            raise PermissionError("in use")
        with mock.patch.object(il.os, "rename", rename):
            with self.assertRaises(il.RollbackError) as cm:
                il.upgrade_files(new, old, lambda m: None, retries=2, delay=0, sleep=lambda s: None)
        self.assertEqual(cm.exception.dst, str(old))
        self.assertIn("Token Burn.old-", cm.exception.old)
        self.assertTrue(Path(cm.exception.old).is_dir())
        self.assertIn(cm.exception.old, str(cm.exception))
        self.assertIn(str(old), str(cm.exception))


class TestRemoveOnlyOurs(Isolated):
    def test_posix_removes_only_listed_entries_and_keeps_the_folder(self):
        prog = make_app(self.tmp / "tools", marker="token-burn")
        il.write_manifest(prog, "0.2.0", ["token-burn", "token-burnw.exe", "_internal", "README.md"])
        (prog / "mine.txt").write_text("keep")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(il, "running_dir", return_value=None):
            said = []
            self.assertEqual(il.remove_program_dir(prog, said.append, self.data), "deleted")
        self.assertEqual([p.name for p in prog.iterdir()], ["mine.txt"])
        self.assertIn("left alone", said[0])

    def test_removes_whole_folder_when_it_holds_exactly_our_entries(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        il.write_manifest(prog, "0.2.0")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(il, "running_dir", return_value=None):
            self.assertEqual(il.remove_program_dir(prog, lambda m: None, self.data), "deleted")
        self.assertFalse(prog.exists())

    def test_windows_script_deletes_only_listed_entries(self):
        prog = make_app(self.tmp / "Tools")
        il.write_manifest(prog, "0.2.0")
        (prog / "mine.txt").write_text("keep")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(il, "running_dir", return_value=prog), \
                mock.patch.object(il.subprocess, "Popen") as popen:
            self.assertEqual(il.remove_program_dir(prog, lambda m: None, self.data), "scheduled")
        cmd = popen.call_args[0][0]
        root = str(prog.resolve())
        self.assertNotIn('rmdir /s /q "%s"' % root, cmd)                       # never the whole folder
        self.assertIn('rmdir /s /q "%s"' % str(prog.resolve() / "_internal"), cmd)
        self.assertIn('del /f /q "%s"' % str(prog.resolve() / "token-burn.exe"), cmd)
        self.assertNotIn("mine.txt", cmd)
        self.assertTrue(cmd.rstrip().endswith('rmdir "%s"' % root))             # plain rmdir: fails if not empty
        self.assertIn("PID eq %d" % os.getpid(), cmd)
        self.assertTrue(cmd.index("tasklist") < cmd.index("rmdir"))
        self.assertTrue((prog / "mine.txt").exists())

    def test_windows_script_waits_for_this_process_before_deleting_the_whole_folder(self):
        prog = make_app(self.tmp / "Programs" / "Token Burn")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(il, "running_dir", return_value=prog), \
                mock.patch.object(il.subprocess, "Popen") as popen:
            il.remove_program_dir(prog, lambda m: None, self.data)
        cmd = popen.call_args[0][0]
        self.assertIn("for /l %i in (1,1,30)", cmd)                             # about 60 seconds at most
        self.assertTrue(cmd.index("tasklist") < cmd.index('rmdir /s /q "%s"' % prog.resolve()))
        self.assertTrue(prog.exists())

    def test_linux_link_removed_only_when_it_points_into_the_install(self):
        prog = make_app(self.tmp / "share" / "token-burn", marker="token-burn")
        link = self.home / ".local" / "bin" / "token-burn"
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(il.os.path, "islink", side_effect=lambda p: str(p) == str(link)), \
                mock.patch.object(il.os.path, "lexists", return_value=True), \
                mock.patch.object(il.os, "unlink") as unlink:
            with mock.patch.object(il.os, "readlink", return_value=str(self.tmp / "elsewhere" / "token-burn")):
                msgs = il.unregister_os_integration(prog)
            unlink.assert_not_called()
            self.assertTrue(any("left alone" in m for m in msgs))
            self.assertFalse(any("Command link: removed" in m for m in msgs))
            with mock.patch.object(il.os, "readlink", return_value=str(prog / "token-burn")):
                il.unregister_os_integration(None)           # no recorded install: nothing is removed
            unlink.assert_not_called()
            with mock.patch.object(il.os, "readlink", return_value=str(prog / "token-burn")):
                il.unregister_os_integration(prog)
            unlink.assert_any_call(str(link))

    def test_real_foreign_symlink_survives_unregister(self):
        if os.name != "posix":
            self.skipTest("symlinks need POSIX")
        prog = make_app(self.tmp / "share" / "token-burn", marker="token-burn")
        other = self.tmp / "other-tool"
        other.write_bytes(b"x")
        link = self.home / ".local" / "bin" / "token-burn"
        link.parent.mkdir(parents=True)
        os.symlink(str(other), str(link))
        with mock.patch.object(sys, "platform", "linux"):
            il.unregister_os_integration(prog)
        self.assertTrue(os.path.islink(str(link)))


class TestDataFolderValidation(Isolated):
    def test_config_json_alone_is_not_enough(self):
        for text in ('{"name": "my-app", "version": "1.2.3"}', "[]", "not json", "{}", '{"version": 1}'):
            d = self.tmp / "other"
            d.mkdir(exist_ok=True)
            (d / "config.json").write_text(text)
            (d / "photo.jpg").write_bytes(b"x")
            path, err = il.check_data_dir(d)
            self.assertIsNone(path, text)
            self.assertIn("does not hold Token Burn data", err)
            self.assertFalse(il.is_data_dir(d))

    def test_token_burn_config_or_database_accepted(self):
        a = self.tmp / "a"
        a.mkdir()
        (a / "config.json").write_text('{"version": 1, "app_version": "0.2.0", "data_dir": "x"}')
        (a / "other.txt").write_text("x")
        self.assertIsNone(il.check_data_dir(a)[1])
        b = self.tmp / "b"
        b.mkdir()
        (b / "config.json").write_text('{"sync": {"enabled": true, "machine_id": "' + "a" * 32 + '"}}')
        (b / "other.txt").write_text("x")
        self.assertIsNone(il.check_data_dir(b)[1])
        c = self.tmp / "c"
        c.mkdir()
        (c / "usage.db").write_bytes(b"x")
        (c / "other.txt").write_text("x")
        self.assertIsNone(il.check_data_dir(c)[1])

    def test_remove_data_files_deletes_only_known_names(self):
        d = self.tmp / "data"
        (d / "peer-cache").mkdir(parents=True)
        (d / "peer-cache" / "x.json.gz").write_bytes(b"x")
        for n in ("usage.db", "usage.db-wal", "usage.db-shm", "usage.json", "collect.log", "collect.log.1",
                  "server.log", "collect.lock", "server.pid", "claude-subagents.jsonl",
                  "cursor-subagent-events.jsonl", "plan-percent-fields.json", "adopted-" + "a" * 32 + ".json.gz",
                  "config.json.tmp123"):
            (d / n).write_text("x")
        (d / "config.json").write_text('{"version": 1, "app_version": "0.2.0"}')
        for n in ("holiday.jsonl", "notes.log", "budget.json", "photos.db"):
            (d / n).write_text("not ours")
        (d / "mydir").mkdir()
        gone, removed = il.remove_data_files(d)
        self.assertFalse(removed)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["budget.json", "holiday.jsonl", "mydir", "notes.log", "photos.db"])
        self.assertIn("config.json", gone)
        self.assertIn("peer-cache", gone)

    def test_remove_data_files_keeps_a_config_that_is_not_ours_and_removes_empty_folder(self):
        d = self.tmp / "data"
        d.mkdir()
        (d / "config.json").write_text('{"name": "other"}')
        (d / "usage.db").write_bytes(b"x")
        gone, removed = il.remove_data_files(d)
        self.assertEqual(gone, ["usage.db"])
        self.assertTrue((d / "config.json").exists())
        d2 = self.tmp / "data2"
        d2.mkdir()
        (d2 / "usage.db").write_bytes(b"x")
        self.assertEqual(il.remove_data_files(d2), (["usage.db"], True))
        self.assertFalse(d2.exists())


class TestDataPointer(Isolated):
    data_env = False

    def test_custom_folder_writes_pointer_and_standard_removes_it(self):
        from collector import util
        chosen = self.tmp / "MyData"
        self.assertEqual(il.apply_data_dir(chosen), "custom")
        self.assertEqual(util.read_location(), chosen)
        self.assertEqual(paths.data_dir(), chosen)
        self.assertEqual(il.apply_data_dir(il.standard_data_dir()), "default")
        self.assertIsNone(util.read_location())
        self.assertFalse(util.location_file().exists())
        self.assertEqual(paths.data_dir(), util.standard_data_dir())


if __name__ == "__main__":
    unittest.main()


class FakeTable:
    """A fake process table: list of [pid, ppid, exe]; terminate_pid removes the process (or does not)."""

    def __init__(self, rows, stubborn=()):
        self.rows = [list(r) for r in rows]
        self.stubborn = set(stubborn)
        self.killed = []

    def listing(self):
        return [tuple(r) for r in self.rows]

    def terminate(self, pid, grace=3.0):
        self.killed.append(pid)
        if pid not in self.stubborn:
            self.rows = [r for r in self.rows if r[0] != pid]


class TestReleaseFolder(Isolated):
    def setUp(self):
        Isolated.setUp(self)
        self.prog = make_app(self.tmp / "Programs" / "token-burn")
        self.exe = str(self.prog / "token-burnw.exe")
        self.hook = str(self.prog / "token-burn.exe")
        self.outside = str(self.tmp / "elsewhere" / "token-burnw.exe")

    def use(self, table):
        self.table = table
        real = importlib.import_module("token_burn.proc")
        self.patch(real, "list_processes", side_effect=table.listing)
        self.patch(real, "terminate_pid", side_effect=table.terminate)
        self.patch(il, "proc", new=real)

    def test_only_processes_inside_the_folder_are_found(self):
        self.use(FakeTable([(500, 1, self.exe), (501, 1, self.outside), (502, 1, str(self.prog) + "-old" + os.sep + "token-burn.exe"),
                            (503, 1, str(self.prog / "_internal" / "x.exe")), (504, 1, "")]))
        found = il.proc.processes_using(self.prog)
        self.assertEqual([p for p, _n, _e in found], [500, 503])
        self.assertEqual(found[0][1], "token-burnw.exe")

    def test_current_process_parent_and_ancestors_excluded(self):
        me = os.getpid()
        self.use(FakeTable([(me, 900, self.exe), (os.getppid(), 1, self.exe), (900, 901, self.exe),
                            (901, 1, self.exe), (777, 1, self.exe)]))
        self.assertEqual([p for p, _n, _e in il.proc.processes_using(self.prog)], [777])

    def test_terminates_only_what_is_inside_and_says_so(self):
        self.use(FakeTable([(500, 1, self.exe), (501, 1, self.outside), (os.getpid(), 1, self.hook)]))
        said, naps = [], []
        left = il.release_folder(self.prog, said.append, wait_s=1.0, sleep=naps.append)
        self.assertEqual(left, [])
        self.assertEqual(self.table.killed, [500])
        self.assertIn("Stopped token-burnw.exe (PID 500)", said)
        self.assertEqual(len(naps), 2)                       # waited its full 1.0 s (2 x 0.5 s) first
        self.assertEqual([r[0] for r in self.table.rows], [501, os.getpid()])

    def test_nothing_to_do_returns_at_once(self):
        self.use(FakeTable([(501, 1, self.outside)]))
        naps = []
        self.assertEqual(il.release_folder(self.prog, None, sleep=naps.append), [])
        self.assertEqual(naps, [])
        self.assertEqual(self.table.killed, [])

    def test_waits_for_processes_that_exit_on_their_own(self):
        self.use(FakeTable([(500, 1, self.exe)]))
        naps = []

        def nap(s):
            naps.append(s)
            if len(naps) == 3:
                self.table.rows = []
        self.assertEqual(il.release_folder(self.prog, lambda m: None, sleep=nap), [])
        self.assertEqual(self.table.killed, [])                # it left by itself, so nothing was ended
        self.assertEqual(len(naps), 3)

    def test_returns_what_will_not_die(self):
        self.use(FakeTable([(500, 1, self.exe)], stubborn=[500]))
        left = il.release_folder(self.prog, lambda m: None, wait_s=0.5, sleep=lambda s: None)
        self.assertEqual([p for p, _n, _e in left], [500])

    def test_locked_error_lists_the_holders(self):
        self.use(FakeTable([(500, 1, self.exe), (501, 1, self.hook)]))
        with self.assertRaises(il.LockedError) as cm:
            il.rename_retry(self.tmp / "nope", self.tmp / "dest", self.prog, None, retries=2, delay=0,
                            sleep=lambda s: None)
        msg = str(cm.exception)
        self.assertIn(str(self.prog), msg)
        self.assertIn("token-burnw.exe (PID 500)", msg)
        self.assertIn("token-burn.exe (PID 501)", msg)

    def test_locked_error_without_holders_names_likely_causes(self):
        self.use(FakeTable([]))
        with self.assertRaises(il.LockedError) as cm:
            il.rename_retry(self.tmp / "nope", self.tmp / "dest", self.prog, None, retries=2, delay=0,
                            sleep=lambda s: None)
        msg = str(cm.exception)
        for word in ("Explorer", "terminal", "editor", "antivirus", str(self.prog)):
            self.assertIn(word, msg)

    def test_uninstall_path_releases_before_deleting(self):
        from token_burn import installer
        order = []
        with mock.patch.object(il, "release_folder", side_effect=lambda *a, **k: order.append("release") or []), \
                mock.patch.object(il, "remove_program_dir", side_effect=lambda *a, **k: order.append("remove")):
            installer._release_and_remove(self.prog, lambda m: None, self.data)
        self.assertEqual(order, ["release", "remove"])


class TestLegacyDetection(Isolated):
    def test_empty_stray_dirs_are_accepted_and_kept(self):
        d = make_app(self.tmp / "v01")
        (d / "empty").mkdir()
        (d / "nested" / "deeper").mkdir(parents=True)
        self.assertTrue(il.is_install(d))
        p, err = il.check_install_dir(d)
        self.assertIsNone(err)
        self.assertTrue((d / "empty").is_dir())
        self.assertTrue((d / "nested" / "deeper").is_dir())

    def test_stray_file_refused_and_named(self):
        d = make_app(self.tmp / "v01")
        (d / "empty").mkdir()
        (d / "notes.txt").write_text("x")
        (d / "nonempty").mkdir()
        (d / "nonempty" / "f.dat").write_text("x")
        self.assertFalse(il.is_install(d))
        p, err = il.check_install_dir(d)
        self.assertIsNone(p)
        self.assertIn("notes.txt", err)
        self.assertIn("nonempty", err)
        self.assertNotIn("empty,", err.replace("nonempty", ""))

    def test_refusal_lists_at_most_five(self):
        d = self.tmp / "docs"
        d.mkdir()
        for i in range(8):
            (d / ("f%d.txt" % i)).write_text("x")
        p, err = il.check_install_dir(d)
        self.assertIn("f0.txt", err)
        self.assertIn("f4.txt", err)
        self.assertNotIn("f5.txt", err)
        self.assertIn("3 more", err)
