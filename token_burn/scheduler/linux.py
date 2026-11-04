"""Linux: systemd --user units, with a cron fallback when systemd --user is unavailable."""
import os
import shlex
from pathlib import Path

from token_burn.scheduler import run

CRON_TAG = "# token-burn"
UNITS = ("token-burn-refresh.timer", "token-burn-refresh.service", "token-burn-server.service")


def unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def render_refresh_service(argv):
    return ("[Unit]\nDescription=Claude, Cursor and opencode token usage collector\n\n"
            "[Service]\nType=oneshot\nExecStart=%s\n" % shlex.join([str(a) for a in argv]))


def render_refresh_timer():
    return ("[Unit]\nDescription=Run the token usage collector every minute\n\n"
            "[Timer]\nOnBootSec=30s\nOnUnitActiveSec=60s\nUnit=token-burn-refresh.service\n\n"
            "[Install]\nWantedBy=timers.target\n")


def render_server_service(argv):
    return ("[Unit]\nDescription=Claude, Cursor and opencode token usage dashboard (127.0.0.1 only)\n\n"
            "[Service]\nExecStart=%s\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n" % shlex.join([str(a) for a in argv]))


def render_cron(refresh_argv, serve_argv):
    r = shlex.join([str(a) for a in refresh_argv])
    s = shlex.join([str(a) for a in serve_argv])
    return ["@reboot %s >/dev/null 2>&1 %s" % (s, CRON_TAG),
            "* * * * * %s >/dev/null 2>&1 %s" % (r, CRON_TAG)]


def merge_crontab(existing, new_lines):
    """Existing crontab text with our tagged lines replaced by new_lines (empty to remove)."""
    kept = [ln for ln in existing.splitlines() if not ln.rstrip().endswith(CRON_TAG)]
    return "\n".join(kept + list(new_lines)) + "\n"


def systemd_available():
    rc, _ = run(["systemctl", "--user", "show-environment"])
    return rc == 0


def _crontab_get():
    rc, out = run(["crontab", "-l"])
    return out if rc == 0 else ""


def _crontab_set(text):
    return run(["crontab", "-"], input=text)


def install(refresh_argv, serve_argv):
    if not systemd_available():
        rc, out = _crontab_set(merge_crontab(_crontab_get(), render_cron(refresh_argv, serve_argv)))
        return ["cron fallback: %s" % ("installed" if rc == 0 else "FAILED (%s)" % out.strip())]
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "token-burn-refresh.service").write_text(render_refresh_service(refresh_argv), encoding="utf-8")
    (d / "token-burn-refresh.timer").write_text(render_refresh_timer(), encoding="utf-8")
    (d / "token-burn-server.service").write_text(render_server_service(serve_argv), encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"])
    msgs = []
    for u in ("token-burn-refresh.timer", "token-burn-server.service"):
        rc, out = run(["systemctl", "--user", "enable", "--now", u])
        msgs.append("%s: %s" % (u, "enabled" if rc == 0 else "FAILED (%s)" % out.strip()))
    return msgs


def uninstall():
    msgs = []
    if systemd_available():
        for u in ("token-burn-refresh.timer", "token-burn-server.service"):
            run(["systemctl", "--user", "disable", "--now", u])
        removed = False
        for name in UNITS:
            p = unit_dir() / name
            if p.is_file():
                p.unlink()
                removed = True
        run(["systemctl", "--user", "daemon-reload"])
        if removed:
            msgs.append("systemd user units: removed")
    cur = _crontab_get()
    if CRON_TAG in cur:
        _crontab_set(merge_crontab(cur, []))
        msgs.append("cron entries: removed")
    return msgs


def start_server():
    # `enable --now` started it; with cron there is nothing to start until reboot
    return (unit_dir() / "token-burn-server.service").is_file()


def status():
    if (unit_dir() / "token-burn-server.service").is_file():
        t = run(["systemctl", "--user", "is-active", "token-burn-refresh.timer"])[1].strip()
        s = run(["systemctl", "--user", "is-active", "token-burn-server.service"])[1].strip()
        return "systemd: token-burn-refresh.timer %s; token-burn-server.service %s" % (t, s)
    if CRON_TAG in _crontab_get():
        return "cron fallback installed"
    return "no scheduler installed"
