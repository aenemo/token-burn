"""Shared constants and small helpers (standard library only)."""
import datetime as dt
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def standard_data_dir():
    """The per-OS default data folder (ignores TOKEN_BURN_DATA_DIR and the location pointer)."""
    home = Path.home()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "token-burn" / "data"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "token-burn" / "data"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else home / ".local" / "state") / "token-burn"


def location_file():
    """Fixed per-OS path of location.json, the pointer to a user-chosen data folder."""
    home = Path.home()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "token-burn" / "location.json"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "token-burn" / "location.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "token-burn" / "location.json"


def read_location():
    """The data folder named by location.json, or None when the file is missing or invalid.

    Valid means a JSON object {"version": 1, "data_dir": <absolute path string>}.
    """
    try:
        with open(location_file(), "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return None
    d = doc.get("data_dir")
    if not isinstance(d, str) or not d.strip() or chr(0) in d:
        return None
    p = Path(d)
    return p if p.is_absolute() else None


def write_location(data_dir):
    """Atomically write location.json pointing at data_dir (must be absolute)."""
    p = Path(data_dir)
    if not p.is_absolute():
        raise ValueError("data folder must be an absolute path")
    atomic_write_json(location_file(), {"version": 1, "data_dir": str(p)})


def clear_location():
    """Remove location.json. True if a file was removed."""
    try:
        os.remove(location_file())
        return True
    except OSError:
        return False


def default_data_dir():
    """TOKEN_BURN_DATA_DIR, else the folder in location.json, else the per-OS user data folder."""
    env = os.environ.get("TOKEN_BURN_DATA_DIR")
    if env:
        return Path(env)
    return read_location() or standard_data_dir()


def ensure_dir(path):
    """mkdir -p; new directories are 0700 on POSIX."""
    path = Path(path)
    if os.name == "posix":
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = default_data_dir()
DB_PATH = DATA_DIR / "usage.db"
USAGE_JSON = DATA_DIR / "usage.json"
CLAUDE_HOOK_LOG = DATA_DIR / "claude-subagents.jsonl"
CURSOR_HOOK_LOG = DATA_DIR / "cursor-subagent-events.jsonl"

MINUTES = 43200            # 30 days of 1-minute buckets
TOKEN_REFRESH_S = 60
PLAN_INTERVAL_S = 590      # about 10 minutes, less slack for task start jitter
DEFAULT_PORT = 8421
LOCK_STALE_S = 300
LOG_MAX = 1024 * 1024


class LockBusy(Exception):
    """Another live process holds the lock."""


def atomic_write_json(path, obj, mode=None):
    """Write JSON to path + '.tmp<pid>' then os.replace, retrying (Windows locks)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, separators=(",", ":"))
    if mode is not None and os.name == "posix":
        try:
            os.chmod(str(tmp), mode)
        except OSError:
            pass
    last = None
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.2)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last


class FileLock:
    """O_EXCL lock file. A lock older than stale_s is taken over."""

    def __init__(self, path, stale_s=LOCK_STALE_S):
        self.path = Path(path)
        self.stale_s = stale_s
        self._held = False

    def acquire(self):
        ensure_dir(self.path.parent)
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                self._held = True
                return
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue  # vanished between calls: retry
                if age > self.stale_s:
                    try:
                        os.remove(self.path)
                    except OSError:
                        raise LockBusy(str(self.path))
                    continue
                raise LockBusy(str(self.path))
            except OSError:
                raise LockBusy(str(self.path))
        raise LockBusy(str(self.path))

    def release(self):
        if self._held:
            self._held = False
            try:
                os.remove(self.path)
            except OSError:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class _LocalIsoFormatter(logging.Formatter):
    """Local time with a numeric UTC offset, e.g. 2026-10-21T14:11:25+10:00 (never a bare 'Z')."""

    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


class _SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):  # never let logging break a run
        pass


def get_logger(name="collect"):
    """Logger writing to DATA_DIR/<name>.log, rotated to .log.1 at 1 MB."""
    logger = logging.getLogger("token_burn." + name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(h, "_token_burn", False) for h in logger.handlers):
        try:
            ensure_dir(DATA_DIR)
            h = _SafeRotatingHandler(str(DATA_DIR / (name + ".log")), maxBytes=LOG_MAX,
                                     backupCount=1, encoding="utf-8", delay=True)
            h._token_burn = True
            h.setFormatter(_LocalIsoFormatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(h)
        except OSError:
            logger.addHandler(logging.NullHandler())
    return logger


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    """ISO 8601 (Z or offset), or epoch ms/s (number or numeric string) -> epoch seconds, else None."""
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v / 1000.0 if abs(v) >= 1e11 else v
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        v = float(s)
        return v / 1000.0 if abs(v) >= 1e11 else v
    except ValueError:
        pass
    if len(s) < 10:
        return None
    try:
        s2 = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        pass
    try:  # fractional seconds with odd digit counts on older Pythons
        d = dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return d.replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def claude_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def opencode_db():
    """opencode's SQLite file (TOKEN_BURN_OPENCODE_DB overrides; same relative path on every OS)."""
    env = os.environ.get("TOKEN_BURN_OPENCODE_DB")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "opencode" / "opencode.db"


def cursor_state_db():
    """Cursor's state.vscdb for this OS (CURSOR_STATE_DB overrides)."""
    env = os.environ.get("CURSOR_STATE_DB")
    if env:
        return Path(env)
    tail = Path("Cursor") / "User" / "globalStorage" / "state.vscdb"
    home = Path.home()
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / tail
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / tail
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / tail


# ---- physical computer fingerprint (multi-computer sync)

HOST_FP_SALT = "token-burn-host-fp-v1"


def _raw_machine_id():
    """A stable OS identifier for this physical computer, or "" when none can be read.

    Read only, never exported: host_fingerprint() hashes it.
    """
    try:
        if sys.platform.startswith("win"):
            import winreg
            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, access) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        if sys.platform == "darwin":
            import re
            import subprocess
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=10).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            return m.group(1).strip() if m else ""
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(p, "r", encoding="ascii", errors="ignore") as f:
                    s = f.read().strip()
            except OSError:
                continue
            if s:
                return s
    except Exception:
        pass
    return ""


def host_fingerprint():
    """16 hex characters identifying this physical computer, privacy safe.

    sha256 of the OS machine id salted with a fixed app string; the raw id is never exported.
    Falls back to a hash of the host name and OS when no machine id is available.
    """
    import hashlib
    import platform
    import socket
    raw = _raw_machine_id()
    if not raw:
        try:
            host = socket.gethostname()
        except OSError:
            host = ""
        raw = "fallback:%s:%s" % (host, platform.system())
    return hashlib.sha256(("%s:%s" % (HOST_FP_SALT, raw)).encode("utf-8")).hexdigest()[:16]
