"""Per-OS background scheduling. Each backend exposes:
install(refresh_argv, serve_argv) -> [messages], uninstall() -> [messages],
start_server() -> bool (True if the backend started it), status() -> str.
"""
import subprocess
import sys


def get_backend(platform=None):
    platform = platform or sys.platform
    if platform.startswith("win"):
        from ccu.scheduler import windows as m
    elif platform == "darwin":
        from ccu.scheduler import macos as m
    else:
        from ccu.scheduler import linux as m
    return m


def run(argv, **kw):
    """Run a command, never raising. Returns (returncode, combined output); (127, msg) if missing."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, **kw)
        return p.returncode, p.stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
