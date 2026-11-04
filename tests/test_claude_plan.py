import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from collector.plan import claude_plan

FIX = Path(__file__).resolve().parent / "fixtures" / "claude"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def resp(obj):
    return FakeResp(json.dumps(obj).encode("utf-8"))


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


class TestClaudePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cred = self.tmp / ".credentials.json"
        shutil.copy(FIX / "credentials.json", self.cred)
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)
        self.usage = json.loads((FIX / "usage_response.json").read_text())

    def urlopen(self, *effects):
        """Patch urlopen; each effect is a response object or an exception to raise. Records requests."""
        calls = []
        seq = list(effects)

        def fake(req, timeout=None):
            calls.append(req)
            e = seq.pop(0)
            if isinstance(e, BaseException):
                raise e
            return e
        p = mock.patch("urllib.request.urlopen", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def set_expiry(self, ms):
        c = json.loads(self.cred.read_text())
        c["claudeAiOauth"]["expiresAt"] = ms
        self.cred.write_text(json.dumps(c))

    def test_ok(self):
        calls = self.urlopen(resp(self.usage))
        status, pools = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertEqual(set(pools), {"five_hour", "seven_day"})  # null utilisation and non-pools skipped
        self.assertEqual(pools["five_hour"], {"pct": 9.0, "resets_at": "2026-10-21T06:29:59Z"})
        self.assertEqual(pools["seven_day"]["pct"], 10.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer fake-access-token")
        self.assertEqual(calls[0].get_header("Anthropic-beta"), "oauth-2025-04-20")

    def test_no_credentials(self):
        self.cred.unlink()
        self.urlopen()
        self.assertEqual(claude_plan.fetch(None), ("no sign-in found, run claude once", None))

    def test_401_refresh_then_200_writes_back(self):
        calls = self.urlopen(http_error(401),
                             resp({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
                             resp(self.usage))
        status, pools = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertIsNotNone(pools)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2].get_header("Authorization"), "Bearer new-access")
        c = json.loads(self.cred.read_text())
        self.assertEqual(c["claudeAiOauth"]["accessToken"], "new-access")
        self.assertEqual(c["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertGreater(c["claudeAiOauth"]["expiresAt"], time.time() * 1000)
        self.assertEqual(c["otherKey"], {"keep": True})  # other fields survive
        self.assertEqual(list(self.tmp.glob(".credentials.json.tmp*")), [])

    def test_expired_token_refreshes_first(self):
        self.set_expiry(1000)
        calls = self.urlopen(resp({"access_token": "fresh"}), resp(self.usage))
        status, _ = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertEqual(calls[0].full_url, claude_plan.TOKEN_URL)
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer fresh")

    def test_401_then_refresh_fails(self):
        self.urlopen(http_error(401), http_error(400))
        self.assertEqual(claude_plan.fetch(None), ("sign-in expired, run claude once", None))

    def test_401_twice(self):
        self.urlopen(http_error(401), resp({"access_token": "a"}), http_error(401))
        self.assertEqual(claude_plan.fetch(None), ("sign-in expired, run claude once", None))

    def test_expired_without_refresh_token(self):
        c = json.loads(self.cred.read_text())
        c["claudeAiOauth"].pop("refreshToken")
        c["claudeAiOauth"]["expiresAt"] = 1
        self.cred.write_text(json.dumps(c))
        self.urlopen()
        self.assertEqual(claude_plan.fetch(None), ("sign-in expired, run claude once", None))

    def test_429(self):
        self.urlopen(http_error(429))
        self.assertEqual(claude_plan.fetch(None), ("plan API rate limited, will retry", None))

    def test_500(self):
        self.urlopen(http_error(500))
        self.assertEqual(claude_plan.fetch(None), ("plan API error (HTTP 500)", None))

    def test_network_error(self):
        self.urlopen(urllib.error.URLError("offline"))
        self.assertEqual(claude_plan.fetch(None), ("plan API unreachable (offline?)", None))

    def test_timeout(self):
        self.urlopen(TimeoutError())
        self.assertEqual(claude_plan.fetch(None), ("plan API unreachable (offline?)", None))

    def test_no_pools(self):
        self.urlopen(resp({"extra_usage": {"is_enabled": False}}))
        self.assertEqual(claude_plan.fetch(None), ("plan API returned no pools", None))

    def test_unexpected_exception(self):
        self.urlopen(ValueError("bad"))
        self.assertEqual(claude_plan.fetch(None), ("plan check failed (ValueError)", None))


if __name__ == "__main__":
    unittest.main()
