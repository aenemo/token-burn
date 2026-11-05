"""token-burn data status | move FOLDER (standard library only)."""
import fnmatch
import os
import shutil
import sqlite3
from pathlib import Path

from token_burn import install_location as il
from token_burn import installer, paths

COPY_FILES = ("config.json", "usage.json")
COPY_PATTERNS = ("*.log", "*.log.*", "*.jsonl", "adopted-*.json.gz")
COPY_DIRS = ("peer-cache",)


def add_parser(sub):
    p = sub.add_parser("data", help="show or move the data folder")
    dsub = p.add_subparsers(dest="data_cmd")
    dsub.required = True
    dsub.add_parser("status", help="show the data folder and its size")
    m = dsub.add_parser("move", help="move the data folder to another folder on this computer")
    m.add_argument("folder", metavar="FOLDER")
    m.add_argument("--yes", action="store_true", help="do not ask for confirmation; delete the old copy")
    return p


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1000.0


def folder_size(path):
    total, count = 0, 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def status_info():
    """(data_dir, how, note) where how is 'environment', 'pointer' or 'default'."""
    from collector import util
    env = os.environ.get("TOKEN_BURN_DATA_DIR")
    if env:
        return Path(env), "environment variable TOKEN_BURN_DATA_DIR", ""
    pointer = util.read_location()
    note = ""
    if pointer is None and util.location_file().is_file():
        note = "location.json is present but not valid, so the default folder is used (%s)" % util.location_file()
    if pointer is not None:
        return pointer, "location.json (%s)" % util.location_file(), note
    return util.standard_data_dir(), "default for this computer", note


def data_status(con):
    say = con.say
    path, how, note = status_info()
    say("Data folder: %s" % path)
    say("Chosen by: %s" % how)
    if note:
        say("Note: %s" % note)
    if Path(path).is_dir():
        size, count = folder_size(path)
        say("Size: %s in %d files" % (human_size(size), count))
        say("Database: %s" % ("usage.db present" if (Path(path) / "usage.db").is_file() else "no usage.db yet"))
    else:
        say("The folder does not exist yet.")
    return 0


# ---- move

def _sqlite_copy(src, dst):
    s = sqlite3.connect(str(src), timeout=30)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _ro(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=30)


def _row_counts(path):
    con = _ro(path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute('SELECT COUNT(*) FROM "%s"' % n.replace('"', '""')).fetchone()[0] for n in names}
    finally:
        con.close()


def verify_copy(src_db, dst_db):
    """Open both databases read-only and compare row counts per table. Raises installer.Failure on a mismatch."""
    try:
        a, b = _row_counts(src_db), _row_counts(dst_db)
    except sqlite3.Error as e:
        raise installer.Failure("The copied database could not be read back (%s). The old data is untouched." % e)
    if a != b:
        bad = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        raise installer.Failure("The copied database does not match (tables: %s). The old data is untouched."
                                % ", ".join(bad))
    return a


def copy_data(src, dst, say, sizes=None):
    """Copy Token Burn's files from src to dst. Returns the list of top-level names copied. When sizes is a dict
    it receives {name: bytes copied} for the *.jsonl hook logs, so later appends can be carried over."""
    src, dst = Path(src), Path(dst)
    copied = []
    db = src / "usage.db"
    if db.is_file():
        _sqlite_copy(db, dst / "usage.db")
        copied.append("usage.db")
    try:
        names = sorted(os.listdir(str(src)))
    except OSError:
        names = []
    for n in names:
        p = src / n
        if p.is_file() and (n in COPY_FILES or any(fnmatch.fnmatch(n, pat) for pat in COPY_PATTERNS)):
            shutil.copy2(str(p), str(dst / n))
            copied.append(n)
            if sizes is not None and n.endswith(".jsonl"):
                sizes[n] = (dst / n).stat().st_size
        elif p.is_dir() and n in COPY_DIRS:
            shutil.copytree(str(p), str(dst / n), dirs_exist_ok=True)
            copied.append(n)
    say("Copied %d items to %s" % (len(copied), dst))
    return copied


def copy_log_tails(src, dst, sizes, say=None):
    """Hook logs (*.jsonl) keep growing while the data is copied. Append whatever was written to the old
    folder after the copy to the new one. Returns the number of bytes carried over."""
    total = 0
    for n, size in sorted(sizes.items()):
        try:
            with open(str(Path(src) / n), "rb") as f:
                f.seek(size)
                tail = f.read()
            if tail:
                with open(str(Path(dst) / n), "ab") as f:
                    f.write(tail)
                total += len(tail)
        except OSError as e:
            if say:
                say("Could not carry over the latest lines of %s (%s); they stay in the old folder." % (n, e))
    return total


def _delete_old(src, copied, say):
    """Delete only Token Burn's own files from the old folder, then the folder if that left it empty."""
    _gone, removed = il.remove_data_files(src)
    if removed:
        say("Old data folder deleted: %s" % src)
    else:
        say("Old Token Burn files deleted; other files were left in %s" % src)


def move_data(con, target, program_dir=None, restart=True, backend=None, delete_old="ask"):
    """Move the data folder. delete_old is 'ask', 'yes' or 'no'. Raises installer.Refused or Failure."""
    from collector import util
    from collector.sync import config
    say = con.say
    if os.environ.get("TOKEN_BURN_DATA_DIR"):
        raise installer.Refused("TOKEN_BURN_DATA_DIR is set, so the data folder cannot be moved. Remove the "
                                "variable first.")
    current = paths.data_dir()
    path, err = il.check_data_dir(target, program_dir=program_dir)
    if err:
        raise installer.Refused(err)
    if il.same_path(path, current):
        raise installer.Refused("The data is already in that folder.")
    if il.is_inside(path, current) or il.is_inside(current, path):
        raise installer.Refused("The new folder and the current one must not contain each other.")
    if (path / "usage.db").is_file():
        raise installer.Refused("That folder already holds Token Burn data. Choose an empty folder.")
    cfg = config.load(current)
    install_dir = cfg.get("install_dir") if isinstance(cfg.get("install_dir"), str) else None
    install_dir = install_dir if install_dir and il.is_install(install_dir) else None
    backend = backend or installer.get_backend()
    say("Stopping the background tasks and the dashboard.")
    installer.stop_background(backend, say)
    copied = []
    sizes = {}
    try:
        util.ensure_dir(path)
        copied = copy_data(current, path, say, sizes)
        if "usage.db" in copied:
            counts = verify_copy(current / "usage.db", path / "usage.db")
            say("Verified the copy: %d tables, %d rows." % (len(counts), sum(counts.values())))
    except (OSError, shutil.Error, sqlite3.Error, installer.Failure) as e:
        say("The move did not finish: %s" % e)
        if restart:
            _restart(backend, install_dir, say)
        if isinstance(e, installer.Failure):
            raise
        raise installer.Failure("The data could not be copied (%s). The old data is untouched." % e)
    new_cfg = config.load(path)
    if new_cfg:
        new_cfg["data_dir"] = str(path)
        config.save(path, new_cfg)
    il.apply_data_dir(path)
    say("Data folder is now %s" % path)
    if copy_log_tails(current, path, sizes, say):
        say("Carried over hook log lines written during the move.")
    if restart:
        _restart(backend, install_dir, say)
    if delete_old == "yes" or (delete_old == "ask" and con.yn(
            "Delete the old data folder %s?" % current, False)):
        _delete_old(current, copied, say)
    else:
        say("The old data was kept at %s. You can delete it once you are happy with the new location." % current)
    return path


def _restart(backend, install_dir, say):
    installer.register_background(backend, install_dir, say)
    installer.start_dashboard(backend, install_dir, say, False)


# ---- command

def cmd_data(args, con=None):
    con = con or installer.Console()
    try:
        if args.data_cmd == "status":
            return data_status(con)
        if args.data_cmd == "move":
            if not args.yes and installer.is_interactive():
                src = paths.data_dir()
                if not con.yn("Move the data from %s to %s? The background tasks stop for a moment."
                              % (src, args.folder), True):
                    raise installer.Cancelled()
            move_data(con, args.folder, _program_dir(), delete_old="yes" if args.yes else (
                "ask" if installer.is_interactive() else "no"))
            return 0
    except installer.Cancelled:
        con.say("Cancelled.")
        return 3
    except installer.Refused as e:
        con.say(str(e))
        return 3
    except (installer.Failure, il.InstallLocationError) as e:
        con.err("error: %s" % e)
        return 1
    return 2


def _program_dir():
    from collector.sync import config
    cfg = config.load(paths.data_dir())
    d = cfg.get("install_dir")
    return d if isinstance(d, str) and d else il.running_dir()
