import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_burn import cli, hooks_register, paths
from token_burn.scheduler import windows


class FrozenWindows(unittest.TestCase):
    def _patch(self, exe):
        return (mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(sys, "executable", str(exe)))

    def test_token_burnw_used_when_present(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "token-burnw.exe").write_bytes(b"")
            p = self._patch(d / "token-burn.exe")
            with p[0], p[1], p[2]:
                argv = paths.self_command("collect")
                self.assertEqual(argv, [str(d / "token-burnw.exe"), "collect"])
                xml = windows.render_task_xml(argv, "refresh")
                self.assertIn("<Command>%s</Command>" % (d / "token-burnw.exe"), xml)
                self.assertEqual(paths.self_command("hook", "cursor", windowless=False)[0],
                                 str(d / "token-burn.exe"))
                self.assertTrue(hooks_register.is_ours(paths.hook_command("cursor")))

    def test_fallback_when_missing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            p = self._patch(d / "token-burn.exe")
            with p[0], p[1], p[2]:
                self.assertEqual(paths.self_command("serve"), [str(d / "token-burn.exe"), "serve"])

    def test_is_ours_token_burnw(self):
        self.assertTrue(hooks_register.is_ours("C:/x/token-burnw.exe hook claude"))


class InstallDirOverride(unittest.TestCase):
    """self_command and hook_command can point at the folder the installer copied the program to."""

    def _patch(self, exe):
        return (mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(sys, "executable", str(exe)))

    def test_install_dir_param_replaces_running_folder(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t) / "unzipped", Path(t) / "target"
            for d in (src, dst):
                d.mkdir()
                (d / "token-burn.exe").write_bytes(b"")
                (d / "token-burnw.exe").write_bytes(b"")
            p = self._patch(src / "token-burn.exe")
            with p[0], p[1], p[2]:
                self.assertEqual(paths.self_command("collect", install_dir=dst), [str(dst / "token-burnw.exe"), "collect"])
                self.assertEqual(paths.self_command("hook", "claude", windowless=False, install_dir=dst)[0],
                                 str(dst / "token-burn.exe"))
                self.assertEqual(paths.self_command("collect")[0], str(src / "token-burnw.exe"))
                self.assertIn(str(dst).replace("\\", "/"), paths.hook_command("claude", install_dir=dst))
                with paths.use_install_dir(dst):
                    self.assertEqual(paths.self_command("serve")[0], str(dst / "token-burnw.exe"))
                    self.assertIn(str(dst).replace("\\", "/"), paths.hook_command("cursor"))
                self.assertEqual(paths.self_command("serve")[0], str(src / "token-burnw.exe"))

    def test_install_dir_ignored_for_pip_installs(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(paths.self_command("serve", windowless=False, install_dir="X"),
                             [sys.executable, "-m", "token_burn", "serve"])


class NoStdStreams(unittest.TestCase):
    def test_main_with_none_streams(self):
        so, se = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = None
            with self.assertRaises(SystemExit):
                cli.main(["bogus-command"])  # argparse error must not raise anything else
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            print("ok")
        finally:
            for s in (sys.stdout, sys.stderr):
                if s not in (so, se):
                    s.close()
            sys.stdout, sys.stderr = so, se


if __name__ == "__main__":
    unittest.main()
