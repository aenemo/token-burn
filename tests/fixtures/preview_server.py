"""Tiny preview server for the web UI (stdlib only, loopback only, port 8431).

Serves ROOT/web at / and /web/*, and tests/fixtures/sample_usage.json at /api/usage.json with every ISO
timestamp shifted so the fixture looks freshly generated (the page anchors its windows to Date.now()).
Run: python tests/fixtures/preview_server.py
"""
import json
import mimetypes
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
FIXTURE = Path(__file__).resolve().parent / "sample_usage.json"
PORT = 8431
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift(node, delta):
    if isinstance(node, dict):
        return {k: shift(v, delta) for k, v in node.items()}
    if isinstance(node, list):
        return [shift(v, delta) for v in node]
    if isinstance(node, str) and ISO.match(node):
        return fmt(parse(node) + delta)
    return node


def build():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = time.time()
    # Whole-minute shift keeps the minute indexes lined up with "start".
    delta = (int(now // 60) - int(parse(doc["generated_at"]) // 60)) * 60
    doc = shift(doc, delta)
    doc["generated_at"] = fmt(now)
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/usage.json":
            return self._send(200, build(), "application/json")
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path.startswith("/web/"):
            rel = path[len("/web/"):]
        else:
            return self._send(404, b"not found", "text/plain")
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        self._send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def log_message(self, fmt_, *args):
        sys.stderr.write("%s\n" % (fmt_ % args))


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Preview on http://127.0.0.1:%d" % PORT)
    srv.serve_forever()
