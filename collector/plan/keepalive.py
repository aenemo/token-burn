"""Keep the Claude Code sign-in fresh by running `claude -p "ok"` about six times a day.

The Claude plan panel reads the sign-in token that Claude Code stores. When that token has expired and cannot be
renewed, the plan status becomes "sign-in expired, run claude once". Claude Code renews the token itself every
time it starts, so a tiny background run is enough to keep it valid. This runs before the plan lookup in the same
pass, so the lookup finds the renewed token and never has to rotate it itself.

Switch it off with TOKEN_BURN_CLAUDE_KEEPALIVE=0 or "claude_keepalive": false in config.json (data folder).
Each run sends one short request to the cheapest model, so it costs a few tokens of the plan. The run is silent
(no window) and its output is never stored or logged. If the sign-in itself is gone (the refresh token has been
revoked), the run fails and the plan panel keeps saying to run claude once: only an interactive sign-in fixes that.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from collector import util

INTERVAL_S = 4 * 3600          # six runs a day
RETRY_S = 3600                 # after a failed run, or when the plan panel reports an expired sign-in
TIMEOUT_S = 120
KEY = "keepalive:claude"
PROMPT = "ok"


def enabled():
    env = os.environ.get("TOKEN_BURN_CLAUDE_KEEPALIVE", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    try:
        from collector.sync import config
        return config.load(util.DATA_DIR).get("claude_keepalive") is not False
    except Exception:
        return True


def find_claude():
    """The claude program: TOKEN_BURN_CLAUDE_BIN, the PATH, then the usual install folders. None when absent."""
    env = os.environ.get("TOKEN_BURN_CLAUDE_BIN", "").strip()
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    names = ("claude.exe", "claude.cmd", "claude") if sys.platform.startswith("win") else ("claude",)
    folders = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin"]
    if os.environ.get("APPDATA"):
        folders.append(Path(os.environ["APPDATA"]) / "npm")
    for d in folders:
        for n in names:
            p = d / n
            if p.is_file():
                return str(p)
    return None


def _due(state, now, plan_status):
    last = state.get("last_attempt")
    if not isinstance(last, (int, float)) or last > now:
        return True
    wait = INTERVAL_S
    if state.get("ok") is False or str(plan_status or "").startswith("sign-in expired"):
        wait = RETRY_S
    return now - last >= wait


def run_claude(exe, workdir):
    """Run `claude -p ok` once. Returns True on exit code 0. Output is discarded."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "cwd": str(workdir), "timeout": TIMEOUT_S}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000      # no console window
    r = subprocess.run([exe, "--model", "haiku", "-p", PROMPT], **kw)
    return r.returncode == 0


def maybe_run(store, now, log=None):
    """Run the keep-alive when it is due. Returns True if a run was made. Never raises."""
    try:
        if not enabled():
            return False
        exe = find_claude()
        if exe is None:
            return False
        state = store.kv_get(KEY)
        state = state if isinstance(state, dict) else {}
        plan = store.kv_get("plan:claude")
        plan_status = plan.get("status") if isinstance(plan, dict) else ""
        if not _due(state, now, plan_status):
            return False
        # record the attempt first so a crash or a hang cannot make every pass try again
        state.update(last_attempt=now, ok=None)
        store.kv_set(KEY, state)
        store.commit()
        # its own folder, so the one-word sessions stay together and out of the user's projects
        workdir = util.DATA_DIR / "claude-keepalive"
        util.ensure_dir(workdir)
        try:
            ok = run_claude(exe, workdir)
        except subprocess.TimeoutExpired:
            ok = False
        except OSError:
            ok = False
        state["ok"] = ok
        if ok:
            state["last_ok"] = now
        store.kv_set(KEY, state)
        store.commit()
        if log:
            log.info("claude keep-alive %s", "ok" if ok else "failed")
        return True
    except Exception as e:
        if log:
            log.error("claude keep-alive error: %s", type(e).__name__)
        return False
