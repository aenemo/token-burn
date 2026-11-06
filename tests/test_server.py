import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
from token_burn import __version__  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="token-burn-test-"))
        cls.data = cls.tmp / "data"
        cls.web = cls.tmp / "web"
        (cls.web / "js").mkdir(parents=True)
        cls.data.mkdir()
        (cls.web / "index.html").write_text("<html>hi {{VERSION}}</html>", encoding="utf-8")
        (cls.web / "js" / "app.js").write_text("var a=1;", encoding="utf-8")
        (cls.tmp / "secret.txt").write_text("secret", encoding="utf-8")
        cls.srv = server.make_server(0, cls.data, cls.web)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path)
            r = c.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            c.close()

    def usage_path(self):
        return self.data / "usage.json"

    def test_index_routes(self):
        for p in ("/", "/index.html"):
            s, h, b = self.req("GET", p)
            self.assertEqual(s, 200)
            self.assertEqual(b, ("<html>hi %s</html>" % __version__).encode("utf-8"))
            self.assertIn("text/html", h["content-type"])
            self.assertEqual(h["cache-control"], "no-store")
            self.assertEqual(h["content-length"], str(len(b)))

    def test_real_index_shows_version_in_title_and_heading(self):
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Token Burn {{VERSION}}</title>", html)
        self.assertIn("{{VERSION}}</", html.split("<h1", 1)[1].split("</h1>", 1)[0] + "</")

    def test_pyproject_version_matches_package(self):
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "%s"' % __version__, text)

    def test_static(self):
        s, h, b = self.req("GET", "/web/js/app.js")
        self.assertEqual(s, 200)
        self.assertEqual(b, b"var a=1;")
        self.assertIn("javascript", h["content-type"])
        self.assertEqual(h["cache-control"], "no-store")

    def test_static_missing(self):
        s, h, b = self.req("GET", "/web/nope.js")
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(b), {"error": "not found"})

    def test_traversal_rejected(self):
        for p in ("/web/../secret.txt", "/web/%2e%2e/secret.txt",
                  "/web/..%2fsecret.txt", "/web/js/../../secret.txt",
                  "/web/..%5csecret.txt"):
            s, h, b = self.req("GET", p)
            self.assertIn(s, (403, 404), p)
            self.assertNotEqual(b, b"secret", p)

    def test_usage_missing_then_present(self):
        if self.usage_path().exists():
            self.usage_path().unlink()
        s, h, b = self.req("GET", "/api/usage.json")
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(b), {"error": "usage.json not generated yet"})
        self.assertIn("application/json", h["content-type"])
        self.usage_path().write_text(json.dumps({"generated_at": "2026-10-21T03:51:03Z"}),
                                     encoding="utf-8")
        s, h, b = self.req("GET", "/api/usage.json")
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(b)["generated_at"], "2026-10-21T03:51:03Z")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("application/json", h["content-type"])

    def test_health(self):
        if self.usage_path().exists():
            self.usage_path().unlink()
        s, h, b = self.req("GET", "/api/health")
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(b), {"ok": True, "generated_at": None})
        self.usage_path().write_text(json.dumps({"generated_at": "X"}), encoding="utf-8")
        s, h, b = self.req("GET", "/api/health")
        self.assertEqual(json.loads(b), {"ok": True, "generated_at": "X"})
        self.assertEqual(h["cache-control"], "no-store")
        self.usage_path().write_text("not json", encoding="utf-8")
        s, h, b = self.req("GET", "/api/health")
        self.assertEqual(json.loads(b), {"ok": True, "generated_at": None})

    def test_unknown_route_404_json(self):
        s, h, b = self.req("GET", "/nothing")
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(b), {"error": "not found"})
        self.assertEqual(h["cache-control"], "no-store")

    def test_head(self):
        s, h, b = self.req("HEAD", "/")
        self.assertEqual(s, 200)
        self.assertEqual(b, b"")

    def test_other_methods_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            s, h, b = self.req(m, "/api/health")
            self.assertEqual(s, 405, m)
            self.assertEqual(h["cache-control"], "no-store")

    def test_non_loopback_403(self):
        h = server.Handler.__new__(server.Handler)
        h.client_address = ("192.168.1.5", 1234)
        self.assertFalse(h._loopback())
        h.client_address = ("127.0.0.1", 1234)
        self.assertTrue(h._loopback())
        h.client_address = ("::1", 1234)
        self.assertTrue(h._loopback())

    def test_env_data_dir(self):
        old = os.environ.get("TOKEN_BURN_DATA_DIR")
        os.environ["TOKEN_BURN_DATA_DIR"] = str(self.data)
        try:
            self.assertEqual(server.default_data_dir(), self.data)
        finally:
            if old is None:
                del os.environ["TOKEN_BURN_DATA_DIR"]
            else:
                os.environ["TOKEN_BURN_DATA_DIR"] = old

    def test_reuse_address_off_and_log_rotation(self):
        self.assertFalse(server.Server.allow_reuse_address)
        big = self.data / "server.log"
        big.write_bytes(b"x" * (server.LOG_MAX + 10))
        server.log(self.data, "hello")
        self.assertTrue((self.data / "server.log.1").exists())
        self.assertIn("hello", big.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
