"""Providers split out of opencode (DeepSeek, NVIDIA ...): mapping, splitting, and show-only-when-used."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, providers, util
from collector.adapters import opencode
from collector.store import Store
from tests.test_opencode_adapter import NOW, build_db, msg


class MappingTests(unittest.TestCase):
    def test_known_provider_ids(self):
        for pid, plat in (("deepseek", "deepseek"), ("NVIDIA", "nvidia"), ("mistral", "mistral"),
                          ("google-vertex", "google"), ("github-copilot", "copilot"),
                          ("alibaba", "qwen"), ("llama", "meta"), ("xai", "xai"), ("x-ai", "xai"),
                          ("cloudflare-workers-ai", "cloudflare")):
            self.assertEqual(providers.platform_of_provider(pid), plat, pid)

    def test_unknown_and_bad_ids_stay_with_opencode(self):
        for pid in ("opencode", "acme-ai", "", None, 5):
            self.assertIsNone(providers.platform_of_provider(pid))
            self.assertEqual(opencode.platform_of(pid), "opencode")

    def test_openai_keeps_its_platform(self):
        self.assertEqual(opencode.platform_of("openai"), "openai")

    def test_no_platform_clashes_with_a_base_platform(self):
        self.assertFalse(set(providers.PLATFORMS) & {"claude", "cursor", "opencode", "openai"})
        ids = [pid for _n, pids in providers.HARNESS.values() for pid in pids]
        self.assertEqual(len(ids), len(set(ids)))


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "opencode.db"
        self.store = Store(os.path.join(self.tmp, "t.db"))
        p = mock.patch.dict(os.environ, {"TOKEN_BURN_OPENCODE_DB": str(self.db)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)
        build_db(self.db, [
            ("s1", "assistant", msg(600, (100, 20, 0, 0, 0), model=("deepseek", "deepseek-chat", None))),
            ("s1", "assistant", msg(500, (200, 40, 0, 0, 0), model=("nvidia", "llama-nim", None))),
            ("s1", "assistant", msg(400, (300, 60, 0, 0, 0), model=("acme-ai", "acme-code", None))),
        ])

    def rows(self, plat):
        return [dict(r) for r in self.store.usage_rows(plat, 0)]

    def test_rows_are_filed_by_provider(self):
        opencode.collect(self.store, NOW, None)
        self.assertEqual([r["model"] for r in self.rows("deepseek")], ["deepseek-chat"])
        self.assertEqual([r["model"] for r in self.rows("nvidia")], ["llama-nim"])
        self.assertEqual([r["model"] for r in self.rows("opencode")], ["acme-ai/acme-code"])

    def test_old_opencode_rows_move_once(self):
        # an earlier version filed everything under opencode with a provider prefix
        from collector.schema import UsageRecord
        self.store.add_usage(UsageRecord(platform="opencode", agent_kind="primary", agent_id="", session_id="s1",
                                         model="deepseek/deepseek-chat", effort_level="", input_tokens=5,
                                         output_tokens=5, cache_read_tokens=0, cache_write_tokens=0,
                                         cost_usd=0.0, timestamp=NOW - 60, requests=1))
        self.store.commit()
        opencode.collect(self.store, NOW, None)
        self.assertEqual([r["model"] for r in self.rows("opencode")], ["acme-ai/acme-code"])
        self.assertEqual(len(self.rows("deepseek")), 1)
        opencode.collect(self.store, NOW, None)     # a second pass changes nothing
        self.assertEqual(len(self.rows("deepseek")), 1)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "opencode.db"
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {"TOKEN_BURN_OPENCODE_DB": str(self.db),
                                         "TOKEN_BURN_TOOLS_HOME": self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def run_collect(self):
        entries = [e for e in collect._platforms() if e[0] == "opencode" or e[0] in providers.PLATFORMS]
        d = Path(self.tmp) / "d"
        with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                mock.patch.object(util, "USAGE_JSON", d / "usage.json"):
            return collect.run(now=NOW, platforms=entries, log=mock.Mock())

    def test_only_used_providers_appear(self):
        build_db(self.db, [("s1", "assistant", msg(60, (10, 5, 0, 0, 0), model=("deepseek", "deepseek-chat", None)))])
        plats = self.run_collect()["platforms"]
        self.assertIn("deepseek", plats)
        self.assertTrue(plats["deepseek"]["installed"])
        self.assertEqual(plats["deepseek"]["plan"]["status"], "no plan data")
        for name in providers.PLATFORMS:
            if name != "deepseek":
                self.assertNotIn(name, plats)

    def test_no_opencode_means_no_provider_columns(self):
        plats = self.run_collect()["platforms"]
        self.assertEqual(set(plats) & set(providers.PLATFORMS), set())


if __name__ == "__main__":
    unittest.main()
