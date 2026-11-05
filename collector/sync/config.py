"""Sync settings in config.json (data dir) and the shared folder naming rules (standard library only)."""
import json
import os
import re
import unicodedata
import uuid
from pathlib import Path

from collector import util

CONFIG_NAME = "config.json"
SHARED_DIRNAME = "token-burn"
MACHINE_ID_RE = re.compile(r"[0-9a-f]{32}")
NAME_MAX = 40


def config_path(data_dir):
    return Path(data_dir) / CONFIG_NAME


def load(data_dir):
    """config.json as a dict, or {} when it is missing, unreadable or not a JSON object."""
    try:
        with open(config_path(data_dir), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save(data_dir, cfg):
    """Atomic write, 0600 on POSIX. Keeps every key of cfg (install_dir, version, ...)."""
    util.ensure_dir(data_dir)
    util.atomic_write_json(config_path(data_dir), cfg, mode=0o600)


def sync_settings(cfg):
    """The sync block with defaults filled and types checked. enabled is False unless usable."""
    raw = cfg.get("sync") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    machine_id = raw.get("machine_id")
    machine_id = machine_id if valid_machine_id(machine_id) else ""
    folder = raw.get("folder")
    folder = folder if isinstance(folder, str) else ""
    name = raw.get("machine_name")
    joined = raw.get("joined_at")
    return {"enabled": bool(raw.get("enabled") is True and machine_id and folder),
            "folder": folder, "machine_id": machine_id,
            "machine_name": name if isinstance(name, str) else "",
            "joined_at": joined if isinstance(joined, str) else "",
            "adopted": raw.get("adopted") is True}


def new_machine_id():
    return uuid.uuid4().hex


def valid_machine_id(s):
    return isinstance(s, str) and MACHINE_ID_RE.fullmatch(s) is not None


def clean_name(s):
    """Display name: control characters removed, whitespace collapsed, 1 to 40 chars, else ValueError."""
    if not isinstance(s, str):
        raise ValueError("name must be text")
    s = "".join(ch for ch in s if ch.isspace() or unicodedata.category(ch)[0] != "C")
    s = " ".join(s.split())
    if not s:
        raise ValueError("name cannot be empty")
    if len(s) > NAME_MAX:
        raise ValueError("name must be %d characters or fewer" % NAME_MAX)
    return s


def shared_root(folder):
    """<folder>/token-burn, where machines/ and control/ live."""
    return Path(folder) / SHARED_DIRNAME


def normalise_folder(path):
    """Absolute folder string: ~ and variables expanded, quotes stripped; the token-burn folder maps to its parent."""
    s = str(path).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    p = Path(os.path.expandvars(os.path.expanduser(s))).resolve()
    if p.name.lower() == SHARED_DIRNAME:
        p = p.parent
    return str(p)
