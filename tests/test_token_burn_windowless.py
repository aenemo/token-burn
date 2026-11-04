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
