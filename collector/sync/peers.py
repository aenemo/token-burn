"""Read the other computers' files from the shared folder, with a last-good cache for partial syncs.

Only file names that are exactly <32 hex>.json.gz are ever opened. Everything else in machines/ is
counted in ignored_files (conflict copies, temp files, iCloud stubs for other names).
"""
import json
import os
import re
import time
from pathlib import Path

from collector import util
from collector.sync import config, validate

MAX_FILES = 64
CACHE_KEEP_S = 30 * 86400
CACHE_DIR = "peer-cache"
README_TEXT = ("Shared usage files for Token Burn. Holds token counts only. "
               "Safe to delete if unused.\n")

_MACHINE_FILE = re.compile(r"([0-9a-f]{32})\.json\.gz")
_ICLOUD_STUB = re.compile(r"\.([0-9a-f]{32})\.json\.gz\.icloud")
_CONTROL_FILE = re.compile(r"([0-9a-f]{32})\.json")


class ScanResult(list):
    """List of PeerResult dicts plus ignored_files, messages and too_many."""

    def __init__(self):
        list.__init__(self)
        self.ignored_files = 0
        self.messages = []
        self.too_many = False


def machines_dir(folder):
    return config.shared_root(folder) / "machines"


def control_dir(folder):
    return config.shared_root(folder) / "control"


def machine_file(folder, machine_id):
    if not config.valid_machine_id(machine_id):
        raise ValueError("invalid machine id")
    return machines_dir(folder) / ("%s.json.gz" % machine_id)


def ensure_layout(folder):
    """Create token-burn/machines, token-burn/control and README.txt (0700 dirs on POSIX)."""
    root = config.shared_root(folder)
    util.ensure_dir(root / "machines")
    util.ensure_dir(root / "control")
    readme = root / "README.txt"
    if not readme.exists():
        with open(readme, "w", encoding="utf-8", newline="\n") as f:
            f.write(README_TEXT)


def _replace_retry(src, dst):
    last = None
    for _ in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.2)
    raise last


def atomic_write_bytes(path, data, mode=0o600):
    """Write to a temp sibling then os.replace (retrying for Windows locks)."""
    path = Path(path)
    tmp = path.with_name(".tmp-%s-%d" % (path.stem.split(".")[0], os.getpid()) + "".join(path.suffixes))
    with open(tmp, "wb") as f:
        f.write(data)
    if os.name == "posix":
        try:
            os.chmod(str(tmp), mode)
        except OSError:
            pass
    try:
        _replace_retry(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---- control files

def read_control(folder, machine_id):
    """Clean control dict for one machine, or None."""
    if not config.valid_machine_id(machine_id):
        return None
    path = control_dir(folder) / ("%s.json" % machine_id)
    try:
        raw = validate.read_raw_capped(path, validate.MAX_CONTROL)
        return validate.parse_control(validate.load_json_bytes(raw), machine_id)
    except validate.ReadError:
        return None


def read_controls(folder):
    """{machine_id: clean control dict} for every readable control file."""
    out = {}
    try:
        names = os.listdir(control_dir(folder))
    except OSError:
        return out
    for name in names:
        m = _CONTROL_FILE.fullmatch(name)
        if not m:
            continue
        c = read_control(folder, m.group(1))
        if c is not None:
            out[m.group(1)] = c
    return out


def write_control(folder, target_id, state, name, by, now):
    """Write control/<target_id>.json and return the document.

    name None keeps the display_name of an existing control file. by is the writer's machine id.
    """
    if not config.valid_machine_id(target_id):
        raise ValueError("invalid machine id")
    if state not in validate.CONTROL_STATES:
        raise ValueError("invalid state")
    if name is None:
        old = read_control(folder, target_id)
        name = old["display_name"] if old else ""
    doc = {"format": "token-burn-control", "schema": 1, "machine_id": target_id, "state": state,
           "display_name": config.clean_name(name) if name else "",
           "by": by if config.valid_machine_id(by) else "", "at": util.iso(now)}
    util.ensure_dir(control_dir(folder))
    atomic_write_bytes(control_dir(folder) / ("%s.json" % target_id),
                       json.dumps(doc, sort_keys=True).encode("utf-8"))
    return doc


def delete_control(folder, machine_id):
    """Remove control/<id>.json (used when a computer rejoins). True if a file was removed."""
    try:
        os.remove(control_dir(folder) / ("%s.json" % machine_id))
        return True
    except (OSError, ValueError):
        return False


def delete_machine_file(folder, machine_id):
    """Remove machines/<id>.json.gz. True if a file was removed."""
    try:
        os.remove(machine_file(folder, machine_id))
        return True
    except (OSError, ValueError):
        return False


# ---- scan

def _cache_path(data_dir, machine_id):
    return Path(data_dir) / CACHE_DIR / ("%s.json.gz" % machine_id)


def _load_clean(raw, machine_id, now):
    """(clean_doc, status) from raw gzip bytes; raises ReadError."""
    doc = validate.load_json_bytes(validate.decompress_capped(raw))
    clean, status = validate.parse_machine(doc, machine_id, now)
    if clean is None:
        raise validate.ReadError(status)
    return clean, status


def _store_cache(data_dir, machine_id, raw, now):
    path = _cache_path(data_dir, machine_id)
    try:
        try:
            same = path.is_file() and path.stat().st_size == len(raw) and path.read_bytes() == raw
        except OSError:
            same = False
        if not same:
            util.ensure_dir(path.parent)
            atomic_write_bytes(path, raw)
        os.utime(str(path), (now, now))  # mtime = last time the source was seen
    except OSError:
        pass


def _from_cache(data_dir, machine_id, now):
    path = _cache_path(data_dir, machine_id)
    if not path.is_file():
        return None
    try:
        clean, _ = _load_clean(validate.read_raw_capped(path), machine_id, now)
        return clean
    except validate.ReadError:
        return None


def _clean_cache(data_dir, present, now):
    d = Path(data_dir) / CACHE_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        m = _MACHINE_FILE.fullmatch(name)
        if not m or m.group(1) in present:
            continue
        try:
            if now - (d / name).stat().st_mtime > CACHE_KEEP_S:
                os.remove(d / name)
        except OSError:
            pass


def scan(folder, self_id, now, data_dir):
    """PeerResult dicts for every other computer: {id, doc, status, from_cache, control, mtime}.

    doc is the validated machine file (or the last good cached copy), None when nothing usable
    exists or the computer was removed. The result is a list with ignored_files, messages and
    too_many attributes. Never raises for file problems.
    """
    res = ScanResult()
    mdir = machines_dir(folder)
    try:
        entries = list(os.scandir(mdir))
    except OSError:
        return res
    controls = read_controls(folder)
    files, stubs = {}, {}
    for e in entries:
        name = e.name
        m = _MACHINE_FILE.fullmatch(name)
        s = _ICLOUD_STUB.fullmatch(name)
        try:
            is_link = e.is_symlink()
            is_file = e.is_file(follow_symlinks=False)
        except OSError:
            res.ignored_files += 1
            continue
        if is_link or not is_file or not (m or s):
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir:
                res.ignored_files += 1
            continue
        mid = (m or s).group(1)
        if mid == self_id:
            continue
        try:
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            mtime = 0.0
        if m:
            files[mid] = (e.path, mtime)
        else:
            stubs[mid] = (e.path, mtime)
    for mid, v in stubs.items():
        if mid not in files:
            files[mid] = (None, v[1])
    ordered = sorted(files.items(), key=lambda kv: (-kv[1][1], kv[0]))
    if len(ordered) > MAX_FILES:
        res.too_many = True
        res.messages.append("too many computers (reading the %d most recently updated)" % MAX_FILES)
    present = set(files)
    for mid, (path, mtime) in ordered[:MAX_FILES]:
        control = controls.get(mid)
        item = {"id": mid, "doc": None, "status": "", "from_cache": False,
                "control": control, "mtime": mtime}
        res.append(item)
        if control is not None and control["state"] == "removed":
            item["status"] = "removed"
            continue
        reason = "not downloaded yet"
        if path is not None:
            try:
                raw = validate.read_raw_capped(path)
                clean, status = _load_clean(raw, mid, now)
                item["doc"], item["status"] = clean, status
                _store_cache(data_dir, mid, raw, now)
                continue
            except validate.ReadError as e:
                reason = e.reason
            _touch_cache(data_dir, mid, now)
        else:
            _touch_cache(data_dir, mid, now)
        cached = _from_cache(data_dir, mid, now)
        if cached is not None:
            item["doc"], item["from_cache"] = cached, True
            item["status"] = "%s; showing the last good copy" % reason
        else:
            item["status"] = reason
    _clean_cache(data_dir, present, now)
    return res


def _touch_cache(data_dir, machine_id, now):
    try:
        os.utime(str(_cache_path(data_dir, machine_id)), (now, now))
    except OSError:
        pass
