"""macOS: launchd LaunchAgents in ~/Library/LaunchAgents."""
import os
import plistlib
from pathlib import Path

from token_burn import paths
from token_burn.scheduler import run

REFRESH_LABEL = "com.token-burn.refresh"
SERVER_LABEL = "com.token-burn.server"


def agents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(label):
    return agents_dir() / (label + ".plist")


def render_plist(label, argv, kind, log_dir):
    """kind 'refresh': every 60 s + at load. kind 'server': at load, kept alive."""
    d = {
        "Label": label,
        "ProgramArguments": [str(a) for a in argv],
        "RunAtLoad": True,
        "StandardOutPath": str(Path(log_dir) / (label + ".out.log")),
        "StandardErrorPath": str(Path(log_dir) / (label + ".err.log")),
        "ProcessType": "Background",
    }
    if kind == "refresh":
        d["StartInterval"] = 60
    else:
        d["KeepAlive"] = True
    return plistlib.dumps(d).decode("utf-8")


def _domain():
    return "gui/%d" % os.getuid()


def _load(label):
    run(["launchctl", "bootout", "%s/%s" % (_domain(), label)])
    return run(["launchctl", "bootstrap", _domain(), str(plist_path(label))])


def install(refresh_argv, serve_argv):
    msgs = []
    log_dir = paths.data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)
    for label, argv, kind in ((REFRESH_LABEL, refresh_argv, "refresh"), (SERVER_LABEL, serve_argv, "server")):
        plist_path(label).write_text(render_plist(label, argv, kind, log_dir), encoding="utf-8")
        rc, out = _load(label)
        msgs.append("%s: %s" % (label, "loaded" if rc == 0 else "written, load FAILED (%s)" % out.strip()))
    return msgs


def uninstall():
    msgs = []
    for label in (REFRESH_LABEL, SERVER_LABEL):
        p = plist_path(label)
        if p.is_file():
            run(["launchctl", "bootout", "%s/%s" % (_domain(), label)])
            p.unlink()
            msgs.append("%s: removed" % label)
    return msgs


def start_server():
    # bootstrap with RunAtLoad already started it
    return plist_path(SERVER_LABEL).is_file()


def status():
    parts = []
    for label in (REFRESH_LABEL, SERVER_LABEL):
        loaded = run(["launchctl", "print", "%s/%s" % (_domain(), label)])[0] == 0
        parts.append("%s: %s" % (label, "loaded" if loaded else "not loaded"))
    return "; ".join(parts)
