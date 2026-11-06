import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_burn import folder_picker as fp


def completed(rc=0, out=b""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tbfp-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)


class TestPickFolder(Tmp):
    def test_windows_argv_and_env(self):
        title, initial = "Choose where $(evil) to install", str(self.tmp / "My Folder; calc")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(fp.subprocess, "run", return_value=completed(0, (str(self.tmp) + "\r\n").encode())) as run:
            got = fp.pick_folder(title, initial)
        self.assertEqual(got, self.tmp)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "powershell.exe")
        for flag in ("-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-Command"):
            self.assertIn(flag, argv)
        script = argv[-1]
        self.assertNotIn(title, script)
        self.assertNotIn(initial, script)
        self.assertIn("$env:TB_INITIAL", script)
        self.assertIn("$env:TB_TITLE", script)
        self.assertIn("TopMost", script)
        self.assertIn("ShowNewFolderButton", script)
        env = run.call_args[1]["env"]
        self.assertEqual((env["TB_TITLE"], env["TB_INITIAL"]), (title, initial))
        self.assertFalse(run.call_args[1].get("shell"))

    def test_windows_cancel_and_failure(self):
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(fp.subprocess, "run", return_value=completed(0, b"")):
                self.assertIsNone(fp.pick_folder("t", "i"))
            with mock.patch.object(fp.subprocess, "run", return_value=completed(1, b"x")):
                self.assertIsNone(fp.pick_folder("t", "i"))
            with mock.patch.object(fp.subprocess, "run", side_effect=FileNotFoundError()):
                self.assertIsNone(fp.pick_folder("t", "i"))
            with mock.patch.object(fp.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
                self.assertIsNone(fp.pick_folder("t", "i"))

    def test_macos_argv(self):
        title, initial = "Pick 'one'", "/Users/me/It's here"
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(fp.subprocess, "run", return_value=completed(0, b"/Users/me/Dropbox/\n")) as run:
            got = fp.pick_folder(title, initial)
        self.assertEqual(got, Path("/Users/me/Dropbox/"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], [title, initial])
        script_text = " ".join(argv[1:-2])
        self.assertIn("on run argv", script_text)
        self.assertIn("item 1 of argv", script_text)
        self.assertNotIn(title, script_text)
        self.assertNotIn(initial, script_text)
        self.assertFalse(run.call_args[1].get("shell"))

    def test_macos_cancel(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(fp.subprocess, "run", return_value=completed(1, b"")):
            self.assertIsNone(fp.pick_folder("t", "/x"))

    def test_linux_needs_a_display(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(fp.subprocess, "run") as run:
            self.assertIsNone(fp.pick_folder("t", "/x"))
        run.assert_not_called()

    def test_linux_zenity_then_kdialog_then_nothing(self):
        env = {"DISPLAY": ":0"}
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(fp.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n == "zenity" else None), \
                    mock.patch.object(fp.subprocess, "run", return_value=completed(0, b"/home/me/Docs\n")) as run:
                self.assertEqual(fp.pick_folder("Title", "/home/me"), Path("/home/me/Docs"))
            argv = run.call_args[0][0]
            self.assertEqual(argv[:3], ["zenity", "--file-selection", "--directory"])
            self.assertIn("--title=Title", argv)
            self.assertIn("--filename=/home/me/", argv)
            with mock.patch.object(fp.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n == "kdialog" else None), \
                    mock.patch.object(fp.subprocess, "run", return_value=completed(0, b"/home/me/K\n")) as run:
                self.assertEqual(fp.pick_folder("Title", "/home/me"), Path("/home/me/K"))
            self.assertEqual(run.call_args[0][0][:2], ["kdialog", "--getexistingdirectory"])
            with mock.patch.object(fp.shutil, "which", return_value=None), \
                    mock.patch.object(fp.subprocess, "run") as run:
                self.assertIsNone(fp.pick_folder("Title", "/home/me"))
            run.assert_not_called()
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "w"}, clear=True), \
                mock.patch.object(fp.shutil, "which", return_value="/usr/bin/zenity"), \
                mock.patch.object(fp.subprocess, "run", return_value=completed(1, b"")):
            self.assertIsNone(fp.pick_folder("t", "/x"))

    def test_typed_path(self):
        self.assertEqual(fp.typed_path('  "%s"  ' % self.tmp), self.tmp)
        self.assertEqual(fp.typed_path("'%s'" % self.tmp), self.tmp)
        self.assertIsNone(fp.typed_path("   "))
        with mock.patch.dict(os.environ, {"TB_X": str(self.tmp)}):
            self.assertEqual(fp.typed_path("$TB_X" if os.name != "nt" else "%TB_X%"), self.tmp)


class TestDetectRoots(Tmp):
    def _home(self):
        home = self.tmp / "home"
        home.mkdir()
        return home

    def test_windows_roots_from_env_and_dropbox_info(self):
        home = self._home()
        work = home / "OneDrive - Contoso"
        personal = home / "OneDrive"
        dbx = home / "DropboxPersonal"
        dbx_biz = home / "DropboxBiz"
        icloud = home / "iCloudDrive"
        for d in (work, personal, dbx, dbx_biz, icloud):
            d.mkdir()
        appdata = home / "AppData" / "Roaming"
        (appdata / "Dropbox").mkdir(parents=True)
        (appdata / "Dropbox" / "info.json").write_text(json.dumps(
            {"personal": {"path": str(dbx)}, "business": {"path": str(dbx_biz)}}))
        env = {"OneDriveCommercial": str(work), "OneDriveConsumer": str(personal), "OneDrive": str(work),
               "APPDATA": str(appdata), "LOCALAPPDATA": str(home / "AppData" / "Local")}
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "home", return_value=home):
            roots = fp.detect_sync_roots()
        got = {str(p): (label, kind) for label, p, kind in roots}
        self.assertEqual(got[str(work)], ("OneDrive - Contoso", "onedrive"))
        self.assertEqual(got[str(personal)][1], "onedrive")
        self.assertEqual(got[str(dbx)][1], "dropbox")
        self.assertEqual(got[str(dbx_biz)][1], "dropbox")
        self.assertEqual(got[str(icloud)], ("iCloud Drive", "icloud"))
        # "OneDrive" env var repeats the work folder: listed once
        self.assertEqual(len([1 for _l, p, _k in roots if p == work]), 1)

    def test_macos_roots(self):
        home = self._home()
        icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        cloud = home / "Library" / "CloudStorage"
        (cloud / "OneDrive-Contoso").mkdir(parents=True)
        (cloud / "GoogleDrive-me@x.com").mkdir()
        icloud.mkdir(parents=True)
        (home / "Dropbox").mkdir()
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(Path, "home", return_value=home):
            roots = fp.detect_sync_roots()
        kinds = {p.name: k for _l, p, k in roots}
        self.assertEqual(kinds["com~apple~CloudDocs"], "icloud")
        self.assertEqual(kinds["OneDrive-Contoso"], "onedrive")
        self.assertEqual(kinds["GoogleDrive-me@x.com"], "gdrive")
        self.assertEqual(kinds["Dropbox"], "dropbox")

    def test_linux_roots(self):
        home = self._home()
        (home / ".dropbox").mkdir()
        (home / "Dropbox").mkdir()
        (home / "OneDrive").mkdir()
        (home / ".dropbox" / "info.json").write_text(json.dumps({"personal": {"path": str(home / "Dropbox")}}))
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(Path, "home", return_value=home):
            roots = fp.detect_sync_roots()
        self.assertEqual(sorted(k for _l, _p, k in roots), ["dropbox", "onedrive"])

    def test_bad_info_json_is_ignored(self):
        home = self._home()
        (home / ".dropbox").mkdir()
        (home / ".dropbox" / "info.json").write_text("{not json")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(Path, "home", return_value=home):
            self.assertEqual(fp.detect_sync_roots(), [])


class TestLooksSynced(Tmp):
    def test_under_detected_root(self):
        root = self.tmp / "Sync"
        (root / "sub").mkdir(parents=True)
        ok, label = fp.looks_synced(root / "sub", [("My Sync", root, "other")])
        self.assertEqual((ok, label), (True, "My Sync"))

    def test_unc(self):
        self.assertEqual(fp.looks_synced("\\\\server\\share\\x", [])[0], True)
        self.assertEqual(fp.looks_synced("//server/share/x", [])[0], True)
        self.assertTrue(fp.is_unc("\\\\server\\share"))
        self.assertFalse(fp.is_unc("C:\\x"))

    def test_cloud_markers_in_path(self):
        for part in ("OneDrive - Contoso", "Dropbox", "Google Drive", "My Drive", "CloudStorage",
                     "Mobile Documents", "iCloud Drive"):
            self.assertTrue(fp.looks_synced(self.tmp / part / "x", [])[0], part)

    def test_plain_local_folder(self):
        self.assertEqual(fp.looks_synced(self.tmp / "Documents", []), (False, ""))

    def test_remote_drive_and_volumes(self):
        with mock.patch.object(fp, "_is_remote_drive", return_value=True):
            self.assertTrue(fp.looks_synced(self.tmp, [])[0])
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(fp.looks_synced("/Volumes/NAS/stuff", [])[0])
            self.assertFalse(fp.looks_synced("/Volumes/Macintosh HD/Users/me", [])[0])

    def test_never_raises(self):
        self.assertEqual(fp.looks_synced(None, []), (False, ""))
        self.assertEqual(fp.looks_synced("\x00bad", None)[0] in (True, False), True)


class TestLocations(Tmp):
    def test_temp_roots_and_zip_preview(self):
        fake = self.tmp / "faketmp"
        (fake / "app").mkdir(parents=True)
        with mock.patch.object(fp, "_temp_roots", return_value=[str(fake)]):
            self.assertTrue(fp.is_temp_location(fake / "app"))
            self.assertFalse(fp.is_temp_location(self.tmp / "other"))
            self.assertTrue(fp.is_temp_location(self.tmp / "Temp1_token-burn.zip" / "token-burn"))
            if os.name == "nt":  # backslashes are not separators on macOS and Linux
                self.assertTrue(fp.is_temp_location("C:\\Users\\me\\AppData\\Local\\Temp\\Temp2_x.zip\\y"))

    def test_real_temp_dir_is_temp(self):
        self.assertTrue(fp.is_temp_location(tempfile.gettempdir()))
        self.assertTrue(fp.is_temp_location("/private/var/folders/ab/cd/T/x"))

    def test_downloads_and_work_onedrive(self):
        home = self.tmp / "home"
        with mock.patch.object(Path, "home", return_value=home):
            self.assertTrue(fp.is_downloads_folder(home / "Downloads"))
            self.assertTrue(fp.is_downloads_folder(home / "Downloads" / "token-burn"))
            self.assertFalse(fp.is_downloads_folder(home / "Documents"))
        com = self.tmp / "OneDrive - Contoso"
        with mock.patch.dict(os.environ, {"OneDriveCommercial": str(com)}):
            self.assertTrue(fp.is_work_onedrive(com / "x"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(fp.is_work_onedrive(self.tmp / "OneDrive - Fabrikam" / "y"))
            self.assertFalse(fp.is_work_onedrive(self.tmp / "OneDrive" / "y"))


class TestPin(unittest.TestCase):
    def test_pin_runs_attrib_with_argv_list(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(fp.subprocess, "run", return_value=completed(0)) as run:
            self.assertTrue(fp.pin_onedrive("C:\\Users\\me\\OneDrive\\token-burn"))
        self.assertEqual(run.call_args[0][0], ["attrib", "+P", "-U", "C:\\Users\\me\\OneDrive\\token-burn", "/S", "/D"])
        self.assertFalse(run.call_args[1].get("shell"))

    def test_pin_failures_ignored_and_other_os_skipped(self):
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(fp.subprocess, "run", side_effect=OSError("no attrib")):
                self.assertFalse(fp.pin_onedrive("x"))
            with mock.patch.object(fp.subprocess, "run", return_value=completed(5)):
                self.assertFalse(fp.pin_onedrive("x"))
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(fp.subprocess, "run") as run:
            self.assertFalse(fp.pin_onedrive("x"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
