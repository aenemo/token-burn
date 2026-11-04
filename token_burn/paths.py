"""Locations and self-invocation helpers (standard library only)."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

PORT = 8421
URL = "http://127.0.0.1:%d/" % PORT


def data_dir():
    """TOKEN_BURN_DATA_DIR, else the collector's DATA_DIR when importable, else a per-OS default."""
    env = os.environ.get("TOKEN_BURN_DATA_DIR")
    if env:
        return Path(env)
    try:
        from collector import util
        return Path(util.DATA_DIR)
    except Exception:
        pass
    home = Path.home()
    if is_windows():
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "ClaudeCursorTokenUsage"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "ClaudeCursorTokenUsage"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "claude-cursor-token-usage"


def claude_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def cursor_hooks_path():
    return Path.home() / ".cursor" / "hooks.json"


def is_windows():
    return sys.platform.startswith("win")


def self_command(*args, windowless=True):
    """argv list that launches this program. On Windows, prefer token-burnw.exe (frozen) or pythonw.exe (no console window)
    unless windowless=False (hooks need stdout, which pythonw discards)."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if windowless and is_windows():
            # token-burnw.exe is the GUI-subsystem twin of token-burn.exe (no console window).
            w = Path(exe).with_name("token-burnw.exe")
            if w.is_file():
                exe = str(w)
        return [exe] + list(args)
    exe = sys.executable
    if windowless and is_windows():
        pw = Path(exe).with_name("pythonw.exe")
        if pw.is_file():
            exe = str(pw)
    return [exe, "-m", "token_burn"] + list(args)


def quote_command(argv):
    """Shell-quote an argv list for the current OS."""
    if is_windows():
        return subprocess.list2cmdline([str(a) for a in argv])
    return shlex.join([str(a) for a in argv])


def hook_command(kind):
    """Command string stored in the Claude / Cursor hook config."""
    argv = self_command("hook", kind, windowless=False)
    if is_windows():
        # Forward slashes survive both cmd.exe and the Git Bash shell Claude Code may use.
        argv = [argv[0].replace("\\", "/")] + argv[1:]
    return quote_command(argv)
