import os
import stat
import tempfile
import unittest
from pathlib import Path

from collector.sync import config

ID = "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7"


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_loads_empty(self):
        self.assertEqual(config.load(self.dir), {})
        self.assertFalse(config.sync_settings({})["enabled"])

    def test_round_trip_keeps_extra_keys(self):
        cfg = {"version": 1, "install_dir": "X",
               "sync": {"enabled": True, "folder": "F", "machine_id": ID, "machine_name": "Office PC",
                        "joined_at": "2026-11-05T03:00:00Z", "adopted": False}}
        config.save(self.dir, cfg)
        self.assertEqual(config.load(self.dir), cfg)
        self.assertEqual(config.config_path(self.dir).name, "config.json")
        s = config.sync_settings(config.load(self.dir))
        self.assertTrue(s["enabled"])
        self.assertEqual(s["machine_id"], ID)
        self.assertEqual(s["machine_name"], "Office PC")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_file_is_0600(self):
        config.save(self.dir, {"version": 1})
        mode = stat.S_IMODE(os.stat(config.config_path(self.dir)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_temp_left_after_save(self):
        config.save(self.dir, {"version": 1})
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_corrupt_or_non_dict_loads_empty(self):
        self.dir.mkdir()
        config.config_path(self.dir).write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load(self.dir), {})
        config.config_path(self.dir).write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(config.load(self.dir), {})

    def test_disabled_and_incomplete_settings(self):
        self.assertFalse(config.sync_settings({"sync": {"enabled": False}})["enabled"])
        self.assertFalse(config.sync_settings({"sync": {"enabled": True, "folder": "F"}})["enabled"])
        self.assertFalse(config.sync_settings(
            {"sync": {"enabled": True, "folder": "F", "machine_id": "../evil"}})["enabled"])
        self.assertFalse(config.sync_settings(
            {"sync": {"enabled": "yes", "folder": "F", "machine_id": ID}})["enabled"])
        self.assertEqual(config.sync_settings({"sync": "bad"})["machine_id"], "")


class IdAndNameTests(unittest.TestCase):
    def test_new_machine_id_is_valid_and_unique(self):
        a, b = config.new_machine_id(), config.new_machine_id()
        self.assertTrue(config.valid_machine_id(a))
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_valid_machine_id(self):
        self.assertTrue(config.valid_machine_id(ID))
        for bad in ("", ID.upper(), ID[:-1], ID + "0", "../" + ID[3:], ID[:-1] + "g",
                    ID[:10] + "\n" + ID[11:], None, 5, ID + "\n"):
            self.assertFalse(config.valid_machine_id(bad), repr(bad))

    def test_clean_name(self):
        self.assertEqual(config.clean_name("  Office   PC \t"), "Office PC")
        self.assertEqual(config.clean_name("Of\x00fi\x1bce"), "Office")
        self.assertEqual(config.clean_name("a\nb"), "a b")
        self.assertEqual(config.clean_name("x" * 40), "x" * 40)
        for bad in ("", "   ", "\x00\x01", "x" * 41, None, 7):
            with self.assertRaises(ValueError):
                config.clean_name(bad)


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_root(self):
        self.assertEqual(config.shared_root(self.base), self.base / "token-burn")

    def test_normalise_strips_quotes_and_resolves(self):
        p = config.normalise_folder('  "%s"  ' % (self.base / "a" / ".." / "b"))
        self.assertEqual(p, str(self.base / "b"))

    def test_normalise_maps_token_burn_to_parent(self):
        self.assertEqual(config.normalise_folder(self.base / "token-burn"), str(self.base))
        self.assertEqual(config.normalise_folder(self.base / "Token-Burn"), str(self.base))
        self.assertEqual(config.normalise_folder(self.base / "token-burn-other"),
                         str(self.base / "token-burn-other"))

    def test_normalise_expands_home_and_vars(self):
        old = os.environ.get("TB_TEST_DIR")
        os.environ["TB_TEST_DIR"] = str(self.base)
        try:
            self.assertEqual(config.normalise_folder("$TB_TEST_DIR" if os.name == "posix"
                                                     else "%TB_TEST_DIR%"), str(self.base))
        finally:
            if old is None:
                os.environ.pop("TB_TEST_DIR", None)
            else:
                os.environ["TB_TEST_DIR"] = old
        self.assertEqual(config.normalise_folder("~"), str(Path.home().resolve()))


if __name__ == "__main__":
    unittest.main()
