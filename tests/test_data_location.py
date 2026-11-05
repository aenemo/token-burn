import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import util

_CLEAR = ("TOKEN_BURN_DATA_DIR", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "HOME",
          "USERPROFILE")


class LocationBase(unittest.TestCase):
    platform = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in _CLEAR}
        env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                   LOCALAPPDATA=str(self.root / "local"), XDG_CONFIG_HOME=str(self.root / "xdgc"),
                   XDG_STATE_HOME=str(self.root / "xdgs"))
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        if self.platform:
            q = mock.patch("sys.platform", self.platform)
            q.start()
            self.addCleanup(q.stop)

    def chosen(self):
        d = self.root / "chosen"
        return d if d.is_absolute() else d.resolve()


class LocationFilePaths(LocationBase):
    def test_windows(self):
        with mock.patch("sys.platform", "win32"):
            self.assertEqual(util.location_file(), self.root / "local" / "token-burn" / "location.json")

    def test_macos(self):
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(util.location_file(), self.root / "home" / "Library" / "Application Support"
                             / "token-burn" / "location.json")

    def test_linux_xdg_and_fallback(self):
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(util.location_file(), self.root / "xdgc" / "token-burn" / "location.json")
            del os.environ["XDG_CONFIG_HOME"]
            self.assertEqual(util.location_file(), self.root / "home" / ".config" / "token-burn"
                             / "location.json")

    def test_standard_data_dir_unchanged(self):
        with mock.patch("sys.platform", "win32"):
            self.assertEqual(util.standard_data_dir(), self.root / "local" / "token-burn" / "data")
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(util.standard_data_dir(), self.root / "home" / "Library" /
                             "Application Support" / "token-burn" / "data")
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(util.standard_data_dir(), self.root / "xdgs" / "token-burn")


class Resolution(LocationBase):
    def test_default_when_no_pointer(self):
        self.assertIsNone(util.read_location())
        self.assertEqual(util.default_data_dir(), util.standard_data_dir())

    def test_pointer_beats_default_and_env_beats_pointer(self):
        util.write_location(self.chosen())
        self.assertEqual(util.read_location(), self.chosen())
        self.assertEqual(util.default_data_dir(), self.chosen())
        os.environ["TOKEN_BURN_DATA_DIR"] = str(self.root / "env")
        self.assertEqual(util.default_data_dir(), self.root / "env")

    def test_invalid_pointers_fall_back(self):
        path = util.location_file()
        path.parent.mkdir(parents=True)
        bad = ["not json", "[]", "{}", '{"version": 2, "data_dir": "%s"}' % str(self.chosen()).replace("\\", "/"),
               '{"version": 1, "data_dir": 5}', '{"version": 1, "data_dir": ""}',
               '{"version": 1, "data_dir": "relative/dir"}', '{"version": 1}', "\x00"]
        for text in bad:
            path.write_text(text, encoding="utf-8")
            self.assertIsNone(util.read_location(), text)
            self.assertEqual(util.default_data_dir(), util.standard_data_dir(), text)

    def test_write_clear_round_trip(self):
        self.assertFalse(util.clear_location())
        util.write_location(self.chosen())
        doc = json.loads(util.location_file().read_text(encoding="utf-8"))
        self.assertEqual(doc, {"version": 1, "data_dir": str(self.chosen())})
        self.assertEqual([p.name for p in util.location_file().parent.iterdir()], ["location.json"])
        util.write_location(self.root / "other")
        self.assertEqual(util.read_location(), self.root / "other")
        self.assertTrue(util.clear_location())
        self.assertFalse(util.location_file().exists())
        self.assertEqual(util.default_data_dir(), util.standard_data_dir())

    def test_write_rejects_relative(self):
        with self.assertRaises(ValueError):
            util.write_location("relative/dir")
        self.assertFalse(util.location_file().exists())


class ModuleConstants(unittest.TestCase):
    def test_data_dir_constants_still_derived(self):
        self.assertEqual(util.DB_PATH, util.DATA_DIR / "usage.db")
        self.assertEqual(util.USAGE_JSON, util.DATA_DIR / "usage.json")


if __name__ == "__main__":
    unittest.main()
