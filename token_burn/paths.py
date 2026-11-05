"""Locations and self-invocation helpers (standard library only)."""
import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

PORT = 8421
URL = "http://127.0.0.1:%d/" % PORT


def data_dir():
    """TOKEN_BURN_DATA_DIR, else the folder in location.json, else the per-OS default (collector.util decides)."""
    env = os.environ.get("TOKEN_BURN_DATA_DIR")
    if env:
        return Path(env)
    try:
        from collector import util
        return Path(util.default_data_dir())
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


_install_dir_override = None


@contextlib.contextmanager
def use_install_dir(install_dir):
    """Within the block self_command and hook_command point at install_dir (frozen builds only)."""
    global _install_dir_override
    old = _install_dir_override
    _install_dir_override = Path(install_dir) if install_dir else None
    try:
        yield
    finally:
        _install_dir_override = old


def self_command(*args, windowless=True, install_dir=None):
    """argv list that launches this program. On Windows, prefer token-burnw.exe (frozen) or pythonw.exe (no console window)
    unless windowless=False (hooks need stdout, which pythonw discards).

    install_dir (a frozen build only) points the command at that program folder instead of the running one,
    so tasks and hooks follow the files the installer copied there."""
    install_dir = install_dir or _install_dir_override
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if install_dir:
            exe = str(Path(install_dir) / Path(exe).name)
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


def hook_command(kind, install_dir=None):
    """Command string stored in the Claude / Cursor hook config."""
    argv = self_command("hook", kind, windowless=False, install_dir=install_dir)
    if is_windows():
        # Forward slashes survive both cmd.exe and the Git Bash shell Claude Code may use.
        argv = [argv[0].replace("\\", "/")] + argv[1:]
    return quote_command(argv)
