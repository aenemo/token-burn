"""Where Token Burn lives: program folder and data folder choices, copying, upgrades and OS integration.

Per user, no administrator rights. Standard library only. Nothing here prompts; the wizard in
installer.py asks the questions and calls these functions.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from token_burn import folder_picker, paths

APP_NAME = "Token Burn"
PUBLISHER = "aenemo"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TokenBurn"
EXE_NAMES = ("token-burn.exe", "token-burn")
MARKER = ".token-burn-install.json"
# What a release zip holds at the top level. A 0.1.x install has no marker file; it is recognised only when
# it holds nothing but these.
DIST_ENTRIES = ("token-burn.exe", "token-burnw.exe", "token-burn", "_internal", "README.md", "install.sh",
                "Install Token Burn.command")
LOCK_RETRIES = 10
LOCK_DELAY_S = 3.0


class InstallLocationError(Exception):
    """A problem the user can act on; the message is printed as is."""


class RollbackError(InstallLocationError):
    """An upgrade failed and the previous program could not be put back. old is where it is now."""

    def __init__(self, old, dst, why=""):
        self.old, self.dst = str(old), str(dst)
        InstallLocationError.__init__(
            self, "%sThe previous version could not be put back. Its files are in %s: rename that to %s by hand "
                  "(close any program using the folder first)." % ((why + " ") if why else "", self.old, self.dst))


class LockedError(InstallLocationError):
    """Files in the program folder are in use."""

    def __init__(self, folder):
        self.folder = str(folder)
        InstallLocationError.__init__(
            self, "Token Burn cannot replace the files in %s because a program is using them. Close any "
                  "window or program using that folder (for example a running token-burn.exe or a terminal "
                  "opened there), then run the installer again." % self.folder)


# ---- standard locations

def _is_windows():
    return sys.platform.startswith("win")


def recommended_dir():
    """Per-OS recommended program folder."""
    home = Path.home()
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "Programs" / APP_NAME
    if sys.platform == "darwin":
        return home / "Applications" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "token-burn"


def linux_link_path():
    return Path.home() / ".local" / "bin" / "token-burn"


def start_menu_shortcut():
    base = os.environ.get("APPDATA")
    base = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / (APP_NAME + ".url")


def desktop_entry_path():
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "applications" / "token-burn.desktop"


def running_dir():
    """The program folder of a frozen build, or None when running from Python source or pip."""
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return Path(sys.executable).parent
    return None


def _has_exe(p):
    """A real program file (not a symlink such as ~/.local/bin/token-burn) named token-burn(.exe)."""
    return any((p / n).is_file() and not (p / n).is_symlink() for n in EXE_NAMES)


def read_manifest(path):
    """The install marker as a dict {version, entries}, or None when missing or not ours."""
    try:
        with open(Path(path) / MARKER, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != "token-burn-install":
        return None
    entries = doc.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    for n in entries:
        if not isinstance(n, str) or not n or n in (".", "..") or "/" in n or "\\" in n or n == MARKER:
            return None
    return {"version": str(doc.get("version", "")), "entries": list(entries)}


def write_manifest(folder, version, entries=None):
    """Write the marker listing what Token Burn put in folder. entries defaults to everything in it."""
    folder = Path(folder)
    if entries is None:
        entries = [n for n in os.listdir(str(folder)) if n != MARKER]
    doc = {"format": "token-burn-install", "version": str(version or "unknown"), "entries": sorted(entries)}
    with open(str(folder / MARKER), "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")


def _legacy_layout(p):
    """A 0.1.x install (no marker): the program, _internal and nothing but the known release files."""
    try:
        names = os.listdir(str(p))
    except OSError:
        return False
    return (p / "_internal").is_dir() and _has_exe(p) and all(n in DIST_ENTRIES for n in names)


def is_install(path):
    """True when path is a Token Burn program folder: the program and _internal, plus either the install
    marker or (an older install) a layout holding nothing else. A folder that merely holds a file called
    token-burn (such as ~/.local/bin) is not an install."""
    try:
        p = Path(path)
        if not p.is_dir() or not (p / "_internal").is_dir() or not _has_exe(p):
            return False
        return read_manifest(p) is not None or _legacy_layout(p)
    except OSError:
        return False


def install_entries(path):
    """The top-level names Token Burn owns in an install folder (manifest, else the known release files)."""
    p = Path(path)
    m = read_manifest(p)
    if m is not None:
        return list(m["entries"])
    try:
        return [n for n in os.listdir(str(p)) if n in DIST_ENTRIES]
    except OSError:
        return []


def foreign_entries(path):
    """Names in an install folder that Token Burn did not put there."""
    own = set(install_entries(path)) | {MARKER}
    try:
        return sorted(n for n in os.listdir(str(path)) if n not in own)
    except OSError:
        return []


def exe_in(path):
    for n in EXE_NAMES:
        if (Path(path) / n).is_file():
            return Path(path) / n
    return Path(path) / EXE_NAMES[0 if _is_windows() else 1]


def same_path(a, b):
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def is_inside(path, root):
    """True when path is root or inside it."""
    try:
        Path(os.path.normcase(str(Path(path).resolve()))).relative_to(
            Path(os.path.normcase(str(Path(root).resolve()))))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# ---- checks

def check_running_location(exe_dir=None):
    """(ok, message). A frozen build must have its _internal folder and not run from a temp or zip preview path."""
    d = exe_dir if exe_dir is not None else running_dir()
    if d is None:
        return True, ""
    if not (Path(d) / "_internal").is_dir() or folder_picker.is_temp_location(d):
        return False, ("Token Burn is running from a temporary folder, probably straight from the zip. "
                       "Please unzip the whole folder first, to %s, then run it again from there."
                       % recommended_dir())
    return True, ""


def _first_existing(path):
    p = Path(path)
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


def is_writable(path):
    """True when files can be created in path (or in its nearest existing parent)."""
    base = _first_existing(path)
    if not base.is_dir():
        return False
    probe = base / (".token-burn-write-test-%d" % os.getpid())
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.remove(str(probe))
        return True
    except OSError:
        return False


def _resolve(path):
    s = str(path).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if not s:
        raise InstallLocationError("No folder was given.")
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p.absolute()


def _is_empty_dir(p):
    try:
        return not any(Path(p).iterdir())
    except OSError:
        return False


def check_install_dir(path, data_dir=None, src_dir=None):
    """(Path, None) when path is a good program folder, else (None, reason)."""
    try:
        p = _resolve(path)
    except InstallLocationError as e:
        return None, str(e)
    if folder_picker.is_unc(path) or folder_picker.is_unc(p):
        return None, "Token Burn cannot be installed on a network folder. Choose a folder on this computer."
    if folder_picker.is_temp_location(p):
        return None, "That is a temporary folder, which can be cleaned up at any time. Choose another folder."
    if folder_picker.is_downloads_folder(p):
        return None, "That is in your Downloads folder. Choose a permanent folder, for example the recommended one."
    synced, label = folder_picker.looks_synced(p)
    if synced:
        return None, ("That folder is inside %s. The program must not live in a synced folder. "
                      "Choose a folder on this computer only." % (label or "a synced folder"))
    if p.parent == p:
        return None, "Choose a folder, not a drive root."
    if p.exists() and not p.is_dir():
        return None, "That path is a file, not a folder."
    if src_dir is not None and not same_path(p, src_dir) and is_inside(p, src_dir):
        return None, "That folder is inside the folder you are running Token Burn from. Choose another folder."
    if data_dir is not None and is_inside(data_dir, p):
        return None, "The data folder is inside that folder, and upgrades replace it. Choose another folder."
    if p.is_dir() and not _is_empty_dir(p) and not is_install(p):
        return None, ("That folder is not empty and is not a Token Burn install that Token Burn can manage "
                      "(an install holds the program, an _internal folder and nothing else it did not put "
                      "there). Choose an empty or new folder.")
    if not is_writable(p):
        return None, ("Token Burn cannot write to that folder (it may need administrator rights). "
                      "Choose a folder in your user profile.")
    return p, None


def is_token_burn_config(cfg):
    """True when cfg (a parsed config.json) looks like Token Burn's: the installer's version and app_version, or
    a sync block with sync settings."""
    if not isinstance(cfg, dict):
        return False
    if "version" in cfg and "app_version" in cfg:
        return True
    sync = cfg.get("sync")
    return isinstance(sync, dict) and any(k in sync for k in ("machine_id", "enabled", "folder"))


def _load_json_file(path):
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return None


def is_data_dir(path):
    """True when path holds Token Burn data: usage.db, or a config.json that is Token Burn's own."""
    p = Path(path)
    try:
        if (p / "usage.db").is_file():
            return True
        return (p / "config.json").is_file() and is_token_burn_config(_load_json_file(p / "config.json"))
    except OSError:
        return False


def check_data_dir(path, program_dir=None, current=None):
    """(Path, None) when path is a good data folder, else (None, reason)."""
    try:
        p = _resolve(path)
    except InstallLocationError as e:
        return None, str(e)
    if current is not None and same_path(p, current):
        return p, None
    if folder_picker.is_unc(path) or folder_picker.is_unc(p):
        return None, ("That is a network folder. SQLite file locking is unreliable on network shares, so the "
                      "data folder must be on this computer.")
    if folder_picker.is_temp_location(p):
        return None, "That is a temporary folder, which can be cleaned up at any time. Choose another folder."
    synced, label = folder_picker.looks_synced(p)
    if synced:
        return None, ("That folder is inside %s. Keep the data folder on this computer: a sync app can corrupt "
                      "the live database. To combine computers, use the shared folder step instead."
                      % (label or "a synced folder"))
    if p.parent == p:
        return None, "Choose a folder, not a drive root."
    if program_dir is not None and is_inside(p, program_dir):
        return None, "That folder is inside the program folder, which upgrades replace. Choose another folder."
    if p.exists() and not p.is_dir():
        return None, "That path is a file, not a folder."
    if p.is_dir() and not _is_empty_dir(p) and not is_data_dir(p):
        return None, "That folder is not empty and does not hold Token Burn data. Choose an empty or new folder."
    if not is_writable(p):
        return None, "Token Burn cannot write to that folder. Choose another folder."
    return p, None


# ---- data folder pointer

def standard_data_dir():
    from collector import util
    return Path(util.standard_data_dir())


def apply_data_dir(chosen):
    """Write location.json for a non-default data folder, or remove it for the standard one."""
    from collector import util
    if same_path(chosen, util.standard_data_dir()):
        util.clear_location()
        return "default"
    util.write_location(Path(chosen))
    return "custom"


# ---- copying and upgrading

def _stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ignore_for(exclude):
    skip = [Path(e) for e in (exclude or [])]

    def ignore(directory, names):
        out = [n for n in names if n == "__pycache__"]
        for n in names:
            full = Path(directory) / n
            if any(same_path(full, s) for s in skip):
                out.append(n)
        return out
    return ignore


def stage_copy(src, dst, exclude=None, stamp=None, version=None):
    """copytree src into a temp sibling of dst and write the install marker in it. Returns the staged Path.
    Raises InstallLocationError when the copy fails (the staged folder is removed)."""
    dst = Path(dst)
    staged = dst.parent / ("%s.new-%s" % (dst.name, stamp or _stamp()))
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            shutil.rmtree(str(staged), ignore_errors=True)
        shutil.copytree(str(src), str(staged), symlinks=True, ignore=_ignore_for(exclude))
        write_manifest(staged, version)
    except (OSError, shutil.Error) as e:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise InstallLocationError("Token Burn could not copy its files to %s (%s). Nothing was replaced."
                                   % (dst, e))
    return staged


def rename_retry(src, dst, folder, say=None, retries=LOCK_RETRIES, delay=LOCK_DELAY_S, sleep=time.sleep):
    """os.rename with retries for locked folders. Raises LockedError naming folder when it stays locked."""
    told = False
    for i in range(max(1, retries)):
        try:
            os.rename(str(src), str(dst))
            return
        except OSError:
            if i == retries - 1:
                break
            if not told and say:
                say("Waiting for %s to be released. Close any program using it (for example a running "
                    "token-burn.exe or a terminal opened there)..." % folder)
                told = True
            sleep(delay)
    raise LockedError(folder)


def _write_in_place_manifest(dst, version):
    """Running from the final folder: record the known release files that are there (never the rest)."""
    try:
        if read_manifest(dst) is not None and read_manifest(dst)["version"] == str(version or "unknown"):
            return
        if is_install(dst):
            write_manifest(dst, version, install_entries(dst))
    except OSError:
        pass


def install_files(src, dst, say=None, exclude=None, retries=LOCK_RETRIES, delay=LOCK_DELAY_S, sleep=time.sleep,
                  version=None):
    """Fresh install: copy src to dst through a temp sibling. Returns 'in place' or 'installed'."""
    dst = Path(dst)
    if same_path(src, dst):
        _write_in_place_manifest(dst, version)
        return "in place"
    if dst.is_dir() and _is_empty_dir(dst):
        try:
            dst.rmdir()
        except OSError:
            pass
    if dst.exists():
        raise InstallLocationError("%s already exists." % dst)
    staged = stage_copy(src, dst, exclude, version=version)
    try:
        rename_retry(staged, dst, dst, say, retries, delay, sleep)
    except InstallLocationError:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    return "installed"


def _remove_path(p):
    p = Path(p)
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(str(p), ignore_errors=True)
    except OSError:
        pass


def _swap_entries(staged, dst, owned, stamp, say, retries, delay, sleep):
    """Upgrade a folder that also holds files that are not ours: move only our entries out, the new ones in."""
    dst = Path(dst)
    staged = Path(staged)
    new_names = os.listdir(str(staged))
    clash = sorted(n for n in new_names if n != MARKER and (dst / n).exists() and n not in owned)
    if clash:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise InstallLocationError(
            "%s already holds files that Token Burn did not install and that have the same names as its own: "
            "%s. Move or rename them, then run the installer again." % (dst, ", ".join(clash)))
    backup = dst / (".token-burn-old-%s" % stamp)
    moved, placed = [], []
    try:
        backup.mkdir()
        for n in list(owned) + [MARKER]:
            if (dst / n).exists() or (dst / n).is_symlink():
                rename_retry(dst / n, backup / n, dst, say, retries, delay, sleep)
                moved.append(n)
        for n in new_names:
            rename_retry(staged / n, dst / n, dst, say, retries, delay, sleep)
            placed.append(n)
    except (InstallLocationError, OSError) as e:
        for n in placed:
            _remove_path(dst / n)
        failed = []
        for n in reversed(moved):
            try:
                os.rename(str(backup / n), str(dst / n))
            except OSError:
                failed.append(n)
        shutil.rmtree(str(staged), ignore_errors=True)
        if failed:
            raise RollbackError(backup, dst, "Upgrading %s failed (%s)." % (dst, e))
        shutil.rmtree(str(backup), ignore_errors=True)
        if isinstance(e, InstallLocationError):
            raise
        raise InstallLocationError("Token Burn could not replace its files in %s (%s). The previous version "
                                   "was kept." % (dst, e))
    shutil.rmtree(str(backup), ignore_errors=True)
    shutil.rmtree(str(staged), ignore_errors=True)
    if backup.exists() and say:
        say("The previous files could not be fully deleted. You can delete %s later." % backup)


def upgrade_files(src, dst, say=None, exclude=None, retries=LOCK_RETRIES, delay=LOCK_DELAY_S, sleep=time.sleep,
                  version=None):
    """Replace the program in dst with src. When dst holds only Token Burn's files, copy to a temp sibling,
    move the old folder to .old-<ts>, move the new one into place and delete the old one. When it also holds
    files that are not Token Burn's, only Token Burn's own entries are swapped. Returns 'in place' or
    'upgraded'. Raises RollbackError when the previous program could not be put back."""
    dst = Path(dst)
    if same_path(src, dst):
        _write_in_place_manifest(dst, version)
        return "in place"
    if not is_install(dst):
        raise InstallLocationError("%s is not a Token Burn install that Token Burn can manage, so it was not "
                                   "replaced." % dst)
    stamp = _stamp()
    owned = install_entries(dst)
    staged = stage_copy(src, dst, exclude, stamp, version)
    if foreign_entries(dst):
        _swap_entries(staged, dst, owned, stamp, say, retries, delay, sleep)
        return "upgraded"
    old = dst.parent / ("%s.old-%s" % (dst.name, stamp))
    try:
        rename_retry(dst, old, dst, say, retries, delay, sleep)
    except InstallLocationError:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    try:
        rename_retry(staged, dst, dst, say, retries, delay, sleep)
    except InstallLocationError as e:
        shutil.rmtree(str(staged), ignore_errors=True)
        try:
            rename_retry(old, dst, dst, say, retries, delay, sleep)      # put the old program back
        except InstallLocationError:
            raise RollbackError(old, dst, "Upgrading %s failed (%s)." % (dst, e))
        raise
    shutil.rmtree(str(old), ignore_errors=True)
    if old.exists() and say:
        say("The previous copy could not be fully deleted. You can delete %s later." % old)
    return "upgraded"


# ---- OS integration

def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def register_os_integration(install_dir, version, say=None):
    """Start Menu entry and Apps and features (Windows), .desktop entry and symlink (Linux). Returns messages."""
    msgs = []
    install_dir = Path(install_dir)
    if _is_windows():
        url = start_menu_shortcut()
        try:
            url.parent.mkdir(parents=True, exist_ok=True)
            url.write_text("[InternetShortcut]\r\nURL=%s\r\n" % paths.URL, encoding="ascii")
            msgs.append("Start Menu shortcut: added")
        except OSError as e:
            msgs.append("Start Menu shortcut: FAILED (%s)" % e)
        wr = _winreg()
        if wr is None:
            msgs.append("Apps and features entry: not available")
        else:
            exe = str(exe_in(install_dir))
            try:
                key = wr.CreateKeyEx(wr.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, wr.KEY_WRITE)
                try:
                    for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", str(version)),
                                        ("Publisher", PUBLISHER), ("InstallLocation", str(install_dir)),
                                        ("DisplayIcon", exe),
                                        ("UninstallString", '"%s" uninstall --interactive' % exe)):
                        wr.SetValueEx(key, name, 0, wr.REG_SZ, value)
                    wr.SetValueEx(key, "NoModify", 0, wr.REG_DWORD, 1)
                    wr.SetValueEx(key, "NoRepair", 0, wr.REG_DWORD, 1)
                finally:
                    wr.CloseKey(key)
                msgs.append("Apps and features entry: added")
            except OSError as e:
                msgs.append("Apps and features entry: FAILED (%s)" % e)
    elif sys.platform != "darwin":
        entry = desktop_entry_path()
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("[Desktop Entry]\nType=Application\nName=%s\n"
                             "Comment=Claude Code, Cursor and opencode token usage dashboard\n"
                             "Exec=xdg-open %s\nIcon=utilities-system-monitor\nTerminal=false\n"
                             "Categories=Development;Utility;\n" % (APP_NAME, paths.URL), encoding="utf-8")
            msgs.append("Desktop entry: added")
        except OSError as e:
            msgs.append("Desktop entry: FAILED (%s)" % e)
        link = linux_link_path()
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if os.path.islink(str(link)):
                os.unlink(str(link))
            if link.exists():
                msgs.append("Command link: %s already exists, left alone" % link)
            else:
                os.symlink(str(exe_in(install_dir)), str(link))
                msgs.append("Command link: %s" % link)
        except OSError as e:
            msgs.append("Command link: FAILED (%s)" % e)
    return msgs


def _link_is_ours(link, install_dir):
    """True when link is a symlink pointing at a program inside install_dir."""
    if not install_dir or not os.path.islink(str(link)):
        return False
    try:
        target = os.readlink(str(link))
    except OSError:
        return False
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(str(link)), target)
    return is_inside(target, install_dir)


def unregister_os_integration(install_dir=None):
    """Remove everything register_os_integration added. Returns messages. The Linux command link is removed
    only when it is a symlink pointing into install_dir."""
    msgs = []
    if _is_windows():
        url = start_menu_shortcut()
        if url.is_file():
            try:
                url.unlink()
                msgs.append("Start Menu shortcut: removed")
            except OSError as e:
                msgs.append("Start Menu shortcut: FAILED (%s)" % e)
        wr = _winreg()
        if wr is not None:
            try:
                wr.DeleteKey(wr.HKEY_CURRENT_USER, UNINSTALL_KEY)
                msgs.append("Apps and features entry: removed")
            except FileNotFoundError:
                pass
            except OSError as e:
                msgs.append("Apps and features entry: FAILED (%s)" % e)
    elif sys.platform != "darwin":
        entry = desktop_entry_path()
        if entry.is_file():
            try:
                entry.unlink()
                msgs.append("Desktop entry: removed")
            except OSError as e:
                msgs.append("Desktop entry: FAILED (%s)" % e)
        link = linux_link_path()
        if _link_is_ours(link, install_dir):
            try:
                os.unlink(str(link))
                msgs.append("Command link: removed")
            except OSError as e:
                msgs.append("Command link: FAILED (%s)" % e)
        elif os.path.lexists(str(link)):
            msgs.append("Command link: %s is not a link to this install, left alone" % link)
    return msgs


# ---- deleting the program folder

def safe_to_delete(path, data_dir=None):
    """True only for a real Token Burn program folder that is not the home folder, a drive root or the data folder."""
    try:
        p = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    if p.parent == p or same_path(p, Path.home()) or len(p.parts) < 3:
        return False
    if data_dir is not None and (is_inside(data_dir, p) or is_inside(p, data_dir)):
        return False
    return is_install(p)


def _cmd_safe(path):
    return not any(c in str(path) for c in '%"^&|<>!\r\n')


def remove_owned_entries(path):
    """Delete only what Token Burn installed in path (the manifest entries, or the known release files of an
    older install), then the marker, then the folder itself if that left it empty. Returns the names of owned
    entries that are still there."""
    p = Path(path)
    for n in install_entries(p):
        _remove_path(p / n)
    left = [n for n in install_entries(p) if (p / n).exists() or (p / n).is_symlink()]
    if not left:
        _remove_path(p / MARKER)
        try:
            p.rmdir()
        except OSError:
            pass
    return left


def _delete_script(p, owned, pid):
    """cmd line that waits (up to about 60 seconds) for process pid to exit, then deletes the owned entries.
    None when a name is not safe to put in a cmd line."""
    names = list(owned)
    if not _cmd_safe(p) or not all(_cmd_safe(n) for n in names):
        return None
    wait = ('for /l %%i in (1,1,30) do @(tasklist /FI "PID eq %d" /NH 2>nul | find " %d " >nul && '
            'ping -n 3 127.0.0.1 >nul)' % (pid, pid))
    try:
        exact = set(os.listdir(str(p))) <= set(names) | {MARKER}
    except OSError:
        exact = False
    if exact:
        return 'cmd.exe /d /c %s & rmdir /s /q "%s"' % (wait, p)
    steps = []
    for n in names:
        if (p / n).is_dir() and not (p / n).is_symlink():
            steps.append('rmdir /s /q "%s"' % (p / n))
        else:
            steps.append('del /f /q "%s"' % (p / n))
    steps.append('del /f /q "%s"' % (p / MARKER))
    steps.append('rmdir "%s"' % p)
    return "cmd.exe /d /c %s & %s" % (wait, " & ".join(steps))


def remove_program_dir(path, say=None, data_dir=None):
    """Delete Token Burn's own files from the program folder (never anything else in it), and the folder when
    that leaves it empty. Windows cannot delete a running exe, so when this process runs from the folder (or
    the files are locked) a detached cmd waits for this process to exit and then deletes them. Returns
    'deleted', 'scheduled', or 'kept'."""
    say = say or (lambda m: None)
    if not safe_to_delete(path, data_dir):
        say("Program folder not deleted (it does not look like a Token Burn install): %s" % path)
        return "kept"
    p = Path(path)
    owned = install_entries(p)
    mine = running_dir()
    inside = mine is not None and is_inside(mine, p)
    if _is_windows() and inside:
        left = list(owned)
    else:
        left = remove_owned_entries(p)
    if not left:
        say("Program folder deleted: %s" % p if not p.exists()
            else "Token Burn's program files were deleted; other files in %s were left alone." % p)
        return "deleted"
    if _is_windows():
        cmd = _delete_script(p, left, os.getpid())
        if cmd is None:
            say("Program folder could not be deleted automatically; delete it yourself: %s" % p)
            return "kept"
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=0x00000008 | 0x00000200 | 0x08000000)
            say("Program folder will be deleted a few seconds after this window closes: %s" % p)
            return "scheduled"
        except OSError as e:
            say("Program folder not deleted (%s): %s" % (e, p))
            return "kept"
    say("Program folder not fully deleted (in use?): %s" % p)
    return "kept"


# ---- deleting the data folder

DATA_FILES = ("usage.db", "usage.db-wal", "usage.db-shm", "usage.db-journal", "usage.json", "collect.log",
              "server.log", "collect.lock", "server.pid", "claude-subagents.jsonl", "cursor-subagent-events.jsonl",
              "plan-percent-fields.json")
DATA_DIRS = ("peer-cache",)
_DATA_PATTERNS = (re.compile(r"(collect|server)\.log\.\d+"), re.compile(r"adopted-[0-9a-f]{32}\.json\.gz"),
                  re.compile(r"(usage|config)\.json\.tmp\d+"), re.compile(r"\.token-burn-write-test-\d+"))


def data_entries(path):
    """Names in a data folder that Token Burn created (config.json only when it is Token Burn's own)."""
    p = Path(path)
    try:
        names = os.listdir(str(p))
    except OSError:
        return []
    out = []
    for n in names:
        full = p / n
        if (n in DATA_FILES and full.is_file()) or (n in DATA_DIRS and full.is_dir() and not full.is_symlink())                 or (full.is_file() and any(r.fullmatch(n) for r in _DATA_PATTERNS)):
            out.append(n)
        elif n == "config.json" and full.is_file() and is_token_burn_config(_load_json_file(full)):
            out.append(n)
    return sorted(out)


def remove_data_files(path, say=None):
    """Delete only Token Burn's own files from a data folder, then the folder if that left it empty.
    Returns (names deleted, folder removed)."""
    p = Path(path)
    gone = []
    for n in data_entries(p):
        _remove_path(p / n)
        if not (p / n).exists():
            gone.append(n)
    removed = False
    try:
        p.rmdir()
        removed = True
    except OSError:
        pass
    return gone, removed
