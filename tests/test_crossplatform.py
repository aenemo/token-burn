import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import server
from collector import collect, import_history, util
from collector.plan import claude_plan


def _env(**kw):
    base = {k: v for k, v in os.environ.items()
            if k not in ("CCU_DATA_DIR", "CURSOR_STATE_DB", "APPDATA", "LOCALAPPDATA",
                         "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CCU_CLAUDE_TOKEN", "CCU_PORT")}
    base.update(kw)
    return mock.patch.dict(os.environ, base, clear=True)


class TestPaths(unittest.TestCase):
    def test_cursor_db_windows(self):
        with _env(APPDATA="C:/A"), mock.patch("sys.platform", "win32"):
            self.assertEqual(util.cursor_state_db(), Path("C:/A/Cursor/User/globalStorage/state.vscdb"))

    def test_cursor_db_mac(self):
        with _env(), mock.patch("sys.platform", "darwin"):
            self.assertEqual(util.cursor_state_db(), Path.home() / "Library" / "Application Support" /
                             "Cursor" / "User" / "globalStorage" / "state.vscdb")

    def test_cursor_db_linux(self):
        with mock.patch("sys.platform", "linux"):
            with _env(XDG_CONFIG_HOME="/x"):
                self.assertEqual(util.cursor_state_db(), Path("/x/Cursor/User/globalStorage/state.vscdb"))
            with _env():
                self.assertEqual(util.cursor_state_db(), Path.home() / ".config" / "Cursor" / "User" /
                                 "globalStorage" / "state.vscdb")

    def test_cursor_db_override(self):
        with _env(CURSOR_STATE_DB="/o/s.db"):
            self.assertEqual(util.cursor_state_db(), Path("/o/s.db"))

    def test_data_dir(self):
        with mock.patch("sys.platform", "win32"), _env(LOCALAPPDATA="C:/L"):
            self.assertEqual(util.default_data_dir(), Path("C:/L/ccu/data"))
        with mock.patch("sys.platform", "darwin"), _env():
            self.assertEqual(util.default_data_dir(),
                             Path.home() / "Library" / "Application Support" / "ccu" / "data")
        with mock.patch("sys.platform", "linux"):
            with _env(XDG_STATE_HOME="/s"):
                self.assertEqual(util.default_data_dir(), Path("/s/ccu"))
            with _env():
                self.assertEqual(util.default_data_dir(), Path.home() / ".local" / "state" / "ccu")
        with _env(CCU_DATA_DIR="/d"):
            self.assertEqual(util.default_data_dir(), Path("/d"))

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_permissions(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = tmp / "sub" / "x.json"
        util.atomic_write_json(f, {"a": 1})
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(f.parent.stat().st_mode & 0o777, 0o700)


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cred = self.tmp / ".credentials.json"
        self.expired = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r", "expiresAt": 1}}
        self.cred.write_text(json.dumps(self.expired))
        p = _env(CLAUDE_CONFIG_DIR=str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        usage = {"five_hour": {"utilization": 5.0, "resets_at": "2026-10-21T06:29:59Z"}}
        self.calls = []

        def fake(req, timeout=None):
            self.calls.append(req)
            if "oauth/token" in req.full_url:
                return io.BytesIO(json.dumps({"access_token": "new", "refresh_token": "r2",
                                              "expires_in": 3600}).encode())
            return io.BytesIO(json.dumps(usage).encode())
        pp = mock.patch("urllib.request.urlopen", side_effect=fake)
        pp.start()
        self.addCleanup(pp.stop)

    def test_env_token_used_no_refresh(self):
        os.environ["CCU_CLAUDE_TOKEN"] = "envtok"
        status, pools = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertEqual(self.calls[0].get_header("Authorization"), "Bearer envtok")
        self.assertEqual(json.loads(self.cred.read_text()), self.expired)

    def test_env_token_401_is_neutral(self):
        import urllib.error
        os.environ["CCU_CLAUDE_TOKEN"] = "envtok"
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("http://x", 401, "e", {}, None)):
            status, pools = claude_plan.fetch(None)
        self.assertEqual(status, claude_plan.UNAVAILABLE)
        self.assertIsNone(pools)

    def test_keychain_used_and_never_written(self):
        kc = {"claudeAiOauth": {"accessToken": "kctok", "expiresAt": 9999999999999}}
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps(kc)))
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", run):
            status, _ = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertEqual(self.calls[0].get_header("Authorization"), "Bearer kctok")
        self.assertEqual(run.call_args[0][0][:3], ["security", "find-generic-password", "-s"])
        self.assertEqual(json.loads(self.cred.read_text()), self.expired)

    def test_keychain_expired_no_refresh(self):
        kc = {"claudeAiOauth": {"accessToken": "k", "refreshToken": "r", "expiresAt": 1}}
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps(kc)))
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", run):
            status, pools = claude_plan.fetch(None)
        self.assertEqual(status, claude_plan.UNAVAILABLE)
        self.assertEqual(self.calls, [])

    def test_keychain_failure_falls_back_to_file_without_writeback_on_mac(self):
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("subprocess.run", side_effect=OSError("no security")):
            status, _ = claude_plan.fetch(None)
        self.assertEqual(status, claude_plan.UNAVAILABLE)  # expired file token, mac: never refresh
        self.assertEqual(json.loads(self.cred.read_text()), self.expired)

    def test_file_refresh_still_writes_back_off_mac(self):
        with mock.patch("sys.platform", "linux"):
            status, _ = claude_plan.fetch(None)
        self.assertEqual(status, "ok")
        self.assertEqual(json.loads(self.cred.read_text())["claudeAiOauth"]["accessToken"], "new")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_writeback_preserves_mode(self):
        os.chmod(self.cred, 0o640)
        with mock.patch("sys.platform", "linux"):
            claude_plan.fetch(None)
        self.assertEqual(self.cred.stat().st_mode & 0o777, 0o640)


class TestCollectLoop(unittest.TestCase):
    def test_loop_survives_exceptions_and_sleeps(self):
        calls, sleeps = [], []

        def run_fn(log=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
        collect.run_loop(60, run_fn=run_fn, sleep=sleeps.append, max_iterations=3,
                         log=mock.Mock())
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [60, 60])

    def test_constants_unchanged(self):
        self.assertEqual((util.TOKEN_REFRESH_S, util.PLAN_INTERVAL_S, util.MINUTES), (60, 590, 43200))

    def test_installed_via_projects_or_env(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with _env(CLAUDE_CONFIG_DIR=str(tmp)):
            self.assertFalse(collect.claude_installed())
            (tmp / "projects").mkdir()
            self.assertTrue(collect.claude_installed())
            shutil.rmtree(tmp / "projects")
            os.environ["CCU_CLAUDE_TOKEN"] = "t"
            self.assertTrue(collect.claude_installed())


class TestImportCli(unittest.TestCase):
    def test_requires_args(self):
        with self.assertRaises(SystemExit):
            import_history.main([])
        self.assertFalse(hasattr(import_history, "DEFAULT_CLAUDE_SRC"))


class TestServerSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "web").mkdir()
        (cls.tmp / "web" / "index.html").write_text("<html></html>")
        cls.srv = server.make_server(0, cls.tmp, cls.tmp / "web")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.putrequest("GET", "/", skip_host=True)
        if host is not None:
            c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        r.read()
        c.close()
        return r

    def test_host_allowlist(self):
        for h in ("127.0.0.1:%d", "localhost:%d", "[::1]:%d"):
            self.assertEqual(self.get(h % self.port).status, 200, h)
        for h in ("evil.example:%d" % self.port, "127.0.0.1", None, "localhost:1"):
            self.assertEqual(self.get(h).status, 403, h)

    def test_headers(self):
        r = self.get("127.0.0.1:%d" % self.port)
        self.assertEqual(r.getheader("Content-Security-Policy"), server.CSP)
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")

    def test_reuse_address_per_os(self):
        self.assertEqual(server.Server.allow_reuse_address, not os.name == "nt")

    def test_frozen_web_dir(self):
        with mock.patch.object(server.sys, "frozen", True, create=True), \
                mock.patch.object(server.sys, "_MEIPASS", "/m", create=True):
            self.assertEqual(server._web_dir(), Path("/m") / "web")


if __name__ == "__main__":
    unittest.main()
