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


def default_data_dir():
    """CCU_DATA_DIR, else the per-OS user data folder."""
    env = os.environ.get("CCU_DATA_DIR")
    if env:
        return Path(env)
    home = Path.home()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "ccu" / "data"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "ccu" / "data"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else home / ".local" / "state") / "ccu"


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
    logger = logging.getLogger("ccu." + name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(h, "_ccu", False) for h in logger.handlers):
        try:
            ensure_dir(DATA_DIR)
            h = _SafeRotatingHandler(str(DATA_DIR / (name + ".log")), maxBytes=LOG_MAX,
                                     backupCount=1, encoding="utf-8", delay=True)
            h._ccu = True
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
