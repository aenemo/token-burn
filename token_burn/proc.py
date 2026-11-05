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


# ---- finding and ending the processes that run from a folder

def _windows_processes():
    """[(pid, ppid, exe path)] for every process this user can query, through ToolHelp (no PowerShell)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ENTRY)]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    rows = []
    try:
        e = ENTRY()
        e.dwSize = ctypes.sizeof(ENTRY)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            exe = ""
            h = k.OpenProcess(0x1000, False, e.th32ProcessID)      # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buf))
                    if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        exe = buf.value
                finally:
                    k.CloseHandle(h)
            rows.append((int(e.th32ProcessID), int(e.th32ParentProcessID), exe))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return rows


def _posix_processes():
    """[(pid, ppid, exe path)]: /proc on Linux, ps on macOS (best effort)."""
    rows = []
    if os.path.isdir("/proc/self"):
        for n in os.listdir("/proc"):
            if not n.isdigit():
                continue
            try:
                exe = os.readlink("/proc/%s/exe" % n)
                with open("/proc/%s/stat" % n, "r") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            rows.append((int(n), ppid, exe))
        return rows
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def list_processes():
    return _windows_processes() if paths.is_windows() else _posix_processes()


def _norm(path):
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(str(path)))


def _inside(path, root):
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def processes_using(folder, listing=None):
    """[(pid, name, exe path)] of running processes whose program file is inside folder. Never includes this
    process, its parent, or any process this one descends from."""
    rows = (listing or list_processes)()
    parents = {pid: ppid for pid, ppid, _exe in rows}
    skip = {os.getpid(), os.getppid()}
    pid, hops = os.getpid(), 0
    while pid in parents and hops < 64:
        pid, hops = parents[pid], hops + 1
        skip.add(pid)
    out = []
    for pid, _ppid, exe in rows:
        if pid in skip or pid <= 0 or not exe or not _inside(exe, folder):
            continue
        out.append((pid, os.path.basename(exe), exe))
    return sorted(out)


def terminate_pid(pid, grace=3.0):
    """End one process (and, on Windows, the processes it started). Callers must have checked that the process
    belongs to Token Burn's own folder."""
    if paths.is_windows():
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
                       creationflags=0x08000000)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    end = time.time() + grace
    while time.time() < end and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
