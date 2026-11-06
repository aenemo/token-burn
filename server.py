#!/usr/bin/env python3
"""Local dashboard server. Binds 127.0.0.1 only; refuses non-loopback clients.

Routes: / and /index.html -> web/index.html, /web/* static,
        /api/usage.json -> data/usage.json, /api/health -> {"ok": true, ...}.
Stdlib only. Runs under pythonw (no stdout reliance).

The data folder can be overridden with the TOKEN_BURN_DATA_DIR environment variable
or the data_dir argument of make_server (used by the tests).
"""
import argparse
import datetime as dt
import ipaddress
import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent

try:
    from token_burn import __version__ as VERSION
except ImportError:  # server.py run on its own, without the package beside it
    VERSION = ""


def _web_dir():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ROOT)) / "web"
    src = ROOT / "web"
    if (src / "index.html").is_file():
        return src
    installed = ROOT / "token_burn" / "web"  # wheel install: web/ shipped as package token_burn.web
    return installed if (installed / "index.html").is_file() else src


WEB = _web_dir()
DEFAULT_PORT = 8421
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; frame-ancestors 'none'")
LOG_MAX = 1024 * 1024

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def default_data_dir():
    from collector import util
    return util.default_data_dir()


def log(data_dir, msg):
    try:
        data_dir = Path(data_dir)
        from collector import util
        util.ensure_dir(data_dir)
        path = data_dir / "server.log"
        if path.exists() and path.stat().st_size > LOG_MAX:
            try:
                os.replace(path, path.with_name("server.log.1"))
            except OSError:
                pass
        # local time with numeric offset, same style as collect.log (never a bare "Z")
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (stamp, msg))
    except Exception:
        pass


def say(msg):
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg + "\n")
    except Exception:
        pass


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port; disable it.
    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True
    data_dir = None
    web_dir = None


class Handler(BaseHTTPRequestHandler):
    server_version = "token-burn-dashboard/1"

    def log_message(self, fmt, *args):
        pass

    def _loopback(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _host_ok(self):
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path):
        try:
            body = path.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if (ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml")) \
                and "charset" not in ctype:
            ctype += "; charset=utf-8"
        self._send(200, body, ctype)

    def _index(self, path):
        """The dashboard page, with {{VERSION}} filled in from token_burn.__version__."""
        try:
            body = path.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        body = body.replace(b"{{VERSION}}", VERSION.encode("utf-8"))
        self._send(200, body, "text/html; charset=utf-8")

    def _health(self):
        generated = None
        try:
            with open(self.server.data_dir / "usage.json", "rb") as f:
                generated = json.loads(f.read().decode("utf-8")).get("generated_at")
        except Exception:
            generated = None
        self._json(200, {"ok": True, "generated_at": generated})

    def _handle(self):
        try:
            if not self._loopback():
                return self._send(403, b"forbidden")
            if not self._host_ok():
                return self._send(403, b"forbidden")
            if self.command not in ("GET", "HEAD"):
                return self._send(405, b"method not allowed")
            path = unquote(urlsplit(self.path).path)
            web = self.server.web_dir
            if path in ("/", "/index.html"):
                return self._index(web / "index.html")
            if path == "/api/usage.json":
                p = self.server.data_dir / "usage.json"
                if not p.is_file():
                    return self._json(404, {"error": "usage.json not generated yet"})
                return self._file(p)
            if path == "/api/health":
                return self._health()
            if path.startswith("/web/"):
                rel = path[len("/web/"):]
                if "\x00" in rel:
                    return self._send(403, b"forbidden")
                target = (web / rel).resolve()
                try:
                    target.relative_to(web.resolve())
                except ValueError:
                    return self._send(403, b"forbidden")
                if target.is_file():
                    return self._file(target)
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log(self.server.data_dir, "handler error: %s" % type(e).__name__)
            try:
                self._send(500, b"server error")
            except Exception:
                pass

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_OPTIONS = _handle


def make_server(port, data_dir=None, web_dir=None):
    srv = Server(("127.0.0.1", port), Handler)
    srv.data_dir = Path(data_dir) if data_dir else default_data_dir()
    srv.web_dir = Path(web_dir) if web_dir else WEB
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser()
    try:
        default_port = int(os.environ.get("TOKEN_BURN_PORT") or DEFAULT_PORT)
    except ValueError:
        default_port = DEFAULT_PORT
    ap.add_argument("--port", type=int, default=default_port)
    args = ap.parse_args(argv)
    data_dir = default_data_dir()
    try:
        srv = make_server(args.port, data_dir)
    except OSError as e:
        msg = ("Port %d on 127.0.0.1 is already in use or unavailable (%s). "
               "Another copy may be running; use --port to choose another."
               % (args.port, e.strerror or type(e).__name__))
        log(data_dir, msg)
        say(msg)
        return 2
    log(data_dir, "listening on 127.0.0.1:%d" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
