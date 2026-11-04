"""Server process control through a PID file in the data dir."""
import os
import subprocess
import sys
import time
import urllib.request

from token_burn import paths


def pid_file():
    return paths.data_dir() / "server.pid"


def write_pid():
    p = pid_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()), encoding="ascii")


def clear_pid():
    try:
        if read_pid() == os.getpid():
            pid_file().unlink()
    except OSError:
        pass


def read_pid():
    try:
        return int(pid_file().read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if paths.is_windows():
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def server_pid():
    pid = read_pid()
    return pid if pid_alive(pid) else None


def start_server():
    """Start the server detached. Returns its PID (the serve command writes the PID file itself)."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True}
    if paths.is_windows():
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(paths.self_command("serve"), **kw).pid


def stop_server(timeout=5.0):
    """Stop the server named in the PID file. Returns True if a process was stopped."""
    pid = server_pid()
    if pid is None:
        try:
            pid_file().unlink()
        except OSError:
            pass
        return False
    if paths.is_windows():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid) and not paths.is_windows():
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pid_file().unlink()
    except OSError:
        pass
    return True


def wait_health(seconds=20):
    url = paths.URL + "api/health"
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False
