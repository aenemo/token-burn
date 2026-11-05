"""Native folder picker, synced-folder detection and location checks (standard library only).

Nothing here raises for ordinary problems: a missing tool or a cancelled dialog gives None, and the
detection helpers fall back to "not synced". Paths and titles are passed to the picker programs
through environment variables or argv, never interpolated into script text, and shell=True is never used.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PICK_TIMEOUT_S = 600

# Windows PowerShell script. Reads the title and initial folder from the environment.
_PS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -AssemblyName System.Drawing\n"
    "$owner = New-Object System.Windows.Forms.Form\n"
    "$owner.TopMost = $true\n"
    "$owner.ShowInTaskbar = $false\n"
    "$owner.StartPosition = 'Manual'\n"
    "$owner.Location = New-Object System.Drawing.Point(-2000, -2000)\n"
    "$owner.Size = New-Object System.Drawing.Size(1, 1)\n"
    "$owner.Show()\n"
    "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog\n"
    "$dlg.Description = $env:TB_TITLE\n"
    "$dlg.ShowNewFolderButton = $true\n"
    "if ($env:TB_INITIAL -and (Test-Path -LiteralPath $env:TB_INITIAL)) { $dlg.SelectedPath = $env:TB_INITIAL }\n"
    "if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }\n"
    "$owner.Dispose()\n"
)

_APPLESCRIPT = ("on run argv", "POSIX path of (choose folder with prompt (item 1 of argv) "
                "default location (POSIX file (item 2 of argv)))", "end run")

_TEMP_ZIP_PART = re.compile(r"^temp\d+_.*\.zip$", re.IGNORECASE)


# ---- native picker

def powershell_argv():
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
            "-Command", _PS_SCRIPT]


def _clean_output(text):
    s = (text or "").strip().strip("\r\n")
    return s.strip()


def pick_folder(title, initial=None):
    """Open the native folder dialog. Returns a Path, or None when cancelled or unavailable."""
    initial = str(initial) if initial else str(Path.home())
    try:
        if sys.platform.startswith("win"):
            env = dict(os.environ, TB_TITLE=title, TB_INITIAL=initial)
            p = subprocess.run(powershell_argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, timeout=PICK_TIMEOUT_S)
            return _result(p.returncode, p.stdout)
        if sys.platform == "darwin":
            argv = ["osascript"]
            for line in _APPLESCRIPT:
                argv += ["-e", line]
            argv += [title, initial]
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=PICK_TIMEOUT_S)
            return _result(p.returncode, p.stdout)
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return None
        start = initial.rstrip("/") + "/"
        if shutil.which("zenity"):
            argv = ["zenity", "--file-selection", "--directory", "--title=" + title, "--filename=" + start]
        elif shutil.which("kdialog"):
            argv = ["kdialog", "--getexistingdirectory", start, "--title", title]
        else:
            return None
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PICK_TIMEOUT_S)
        return _result(p.returncode, p.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _result(rc, out):
    if rc != 0:
        return None
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    text = _clean_output(out)
    if not text:
        return None
    return Path(text)


def typed_path(text):
    """Typed fallback: quotes stripped, ~ and variables expanded, resolved. None for empty input."""
    s = str(text or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if not s:
        return None
    try:
        return Path(os.path.expandvars(os.path.expanduser(s))).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


# ---- sync root detection

def _dir(p):
    try:
        return Path(p).is_dir()
    except OSError:
        return False


def _add(out, seen, label, path, kind):
    try:
        p = Path(path)
        key = os.path.normcase(str(p))
    except (TypeError, ValueError):
        return
    if key in seen or not _dir(p):
        return
    seen.add(key)
    out.append((label, p, kind))


def _dropbox_info(out, seen, info_path):
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError, RecursionError):
        return
    if not isinstance(info, dict):
        return
    for key, label in (("personal", "Dropbox (personal)"), ("business", "Dropbox (business)")):
        entry = info.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            _add(out, seen, label, entry["path"], "dropbox")


def detect_sync_roots():
    """[(label, Path, kind)] for synced folders that exist on this computer. Never raises."""
    out, seen = [], set()
    try:
        home = Path.home()
        env = os.environ
        if sys.platform.startswith("win"):
            for var, label in (("OneDriveCommercial", "OneDrive - work"), ("OneDriveConsumer", "OneDrive (personal)"),
                               ("OneDrive", "OneDrive")):
                v = env.get(var)
                if v:
                    name = Path(v).name
                    if var == "OneDriveCommercial" and name.lower().startswith("onedrive - "):
                        label = name
                    _add(out, seen, label, v, "onedrive")
            for var in ("APPDATA", "LOCALAPPDATA"):
                base = env.get(var)
                if base:
                    _dropbox_info(out, seen, Path(base) / "Dropbox" / "info.json")
            _add(out, seen, "iCloud Drive", home / "iCloudDrive", "icloud")
            for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
                _add(out, seen, "Google Drive (%s:)" % letter, "%s:\\My Drive" % letter, "gdrive")
            _add(out, seen, "Google Drive", home / "Google Drive", "gdrive")
        elif sys.platform == "darwin":
            _add(out, seen, "iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs", "icloud")
            cs = home / "Library" / "CloudStorage"
            try:
                names = sorted(os.listdir(cs))
            except OSError:
                names = []
            for n in names:
                low = n.lower()
                kind = ("onedrive" if "onedrive" in low else "dropbox" if "dropbox" in low
                        else "gdrive" if "google" in low else "other")
                _add(out, seen, n, cs / n, kind)
            _dropbox_info(out, seen, home / ".dropbox" / "info.json")
            _add(out, seen, "Dropbox", home / "Dropbox", "dropbox")
        else:
            _dropbox_info(out, seen, home / ".dropbox" / "info.json")
            _add(out, seen, "Dropbox", home / "Dropbox", "dropbox")
            _add(out, seen, "OneDrive", home / "OneDrive", "onedrive")
            _add(out, seen, "Google Drive", home / "Google Drive", "gdrive")
    except Exception:
        pass
    return out


# ---- location classification

_CLOUD_MARKERS = (("onedrive", "OneDrive"), ("dropbox", "Dropbox"), ("google drive", "Google Drive"),
                  ("my drive", "Google Drive"), ("cloudstorage", "a cloud storage folder"),
                  ("mobile documents", "iCloud Drive"), ("icloud", "iCloud Drive"))


def is_unc(path):
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


def _is_remote_drive(path):
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            return False
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def _is_relative_to(path, root):
    """True when path is root or inside it (compared as given, then with symlinks and short names resolved)."""
    for a, b in ((path, root), (_resolved(path), _resolved(root))):
        try:
            Path(os.path.normcase(str(a))).relative_to(Path(os.path.normcase(str(b))))
            return True
        except ValueError:
            continue
    return False


def _resolved(p):
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(p)


def looks_synced(path, roots=None):
    """(True, label) when path is inside a synced folder, a network share or a cloud-named folder."""
    try:
        p = Path(path)
        if is_unc(p):
            return True, "a network share"
        roots = detect_sync_roots() if roots is None else roots
        for label, root, _kind in roots:
            if _is_relative_to(p, root):
                return True, label
        if _is_remote_drive(p):
            return True, "a network drive"
        if sys.platform == "darwin":
            s = p.as_posix()
            if s.startswith("/Volumes/") and not s.startswith("/Volumes/Macintosh HD"):
                return True, "an external or network volume"
        for part in p.parts:
            low = part.lower()
            for marker, label in _CLOUD_MARKERS:
                if marker in low:
                    return True, label
    except Exception:
        pass
    return False, ""


def _temp_roots():
    roots = set()
    for v in (tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP"), os.environ.get("TMPDIR"),
              "/tmp", "/var/tmp", "/private/var/folders", "/var/folders", "/private/tmp"):
        if v:
            roots.add(str(v))
    return sorted(roots)


def is_temp_location(path):
    """True for a temporary folder (%TEMP%, /tmp, /private/var/folders) or a zip preview extraction path."""
    try:
        p = Path(path)
        for part in p.parts:
            if _TEMP_ZIP_PART.match(part):
                return True
        try:
            p = p.resolve()
        except (OSError, RuntimeError):
            pass
        for r in _temp_roots():
            rp = Path(r)
            try:
                rp = rp.resolve()
            except (OSError, RuntimeError):
                pass
            if _is_relative_to(p, rp):
                return True
    except Exception:
        pass
    return False


def is_downloads_folder(path):
    """True when path is the user's Downloads folder or inside it."""
    try:
        d = Path.home() / "Downloads"
        return _is_relative_to(Path(path), d)
    except Exception:
        return False


def is_work_onedrive(path):
    """True for a work OneDrive (env OneDriveCommercial, or a path part starting 'OneDrive - ')."""
    try:
        p = Path(path)
        com = os.environ.get("OneDriveCommercial")
        if com and _is_relative_to(p, com):
            return True
        return any(part.lower().startswith("onedrive - ") for part in p.parts)
    except Exception:
        return False


def pin_onedrive(path):
    """Ask OneDrive to keep the files on this computer (attrib +P -U). Failures are ignored."""
    if not sys.platform.startswith("win"):
        return False
    try:
        p = subprocess.run(["attrib", "+P", "-U", str(path), "/S", "/D"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
