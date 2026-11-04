"""Command line entry point: python -m token_burn <install|uninstall|status|serve|collect|hook>."""
import argparse
import atexit
import os
import shutil
import sys
import webbrowser

from token_burn import hooks_register, paths, proc
from token_burn.scheduler import get_backend


def build_parser():
    ap = argparse.ArgumentParser(prog="token-burn", description="Claude + Cursor token usage dashboard")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    p = sub.add_parser("install", help="register hooks and background tasks, start the dashboard")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    p.add_argument("--no-start", action="store_true", help="do not start the server now")
    p = sub.add_parser("uninstall", help="remove hooks, background tasks and stop the server")
    p.add_argument("--purge-data", action="store_true", help="also delete the data folder")
    sub.add_parser("status", help="show what is installed and running")
    p = sub.add_parser("serve", help="run the dashboard server in the foreground")
    p.add_argument("--port", type=int, default=None)
    p = sub.add_parser("collect", help="collect usage once (or in a loop)")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--backfill", action="store_true")
    p = sub.add_parser("hook", help="agent hook handler (reads JSON on stdin)")
    p.add_argument("kind", choices=("claude", "cursor"))
    return ap


def say(msg):
    print(msg)


def cmd_install(args):
    backend = get_backend()
    refresh = paths.self_command("collect")
    serve = paths.self_command("serve")
    say("Data folder: %s" % paths.data_dir())
    for name, fn in (("Claude hook", hooks_register.install_claude),
                     ("Cursor hooks", hooks_register.install_cursor)):
        try:
            say("%s: %s" % (name, fn()))
        except Exception as e:
            say("%s: FAILED (%s)" % (name, e))
    # Replace any old server before the scheduler (re)starts it.
    proc.stop_server()
    for m in backend.install(refresh, serve):
        say(m)
    if not args.no_start:
        started = backend.start_server()
        if not started:
            proc.start_server()
        if proc.wait_health(20):
            say("Dashboard is up at %s" % paths.URL)
        else:
            say("Server did not answer on %s yet; check the log in the data folder." % paths.URL)
        if not args.no_open:
            try:
                webbrowser.open(paths.URL)
            except Exception:
                pass
    return 0


def cmd_uninstall(args):
    backend = get_backend()
    for m in backend.uninstall():
        say(m)
    say("Server: %s" % ("stopped" if proc.stop_server() else "not running"))
    for name, fn in (("Claude hook", hooks_register.uninstall_claude),
                     ("Cursor hooks", hooks_register.uninstall_cursor)):
        try:
            say("%s: %s" % (name, fn()))
        except Exception as e:
            say("%s: FAILED (%s)" % (name, e))
    if args.purge_data:
        shutil.rmtree(str(paths.data_dir()), ignore_errors=True)
        if paths.data_dir().exists():
            say("Data folder not fully deleted (in use?): %s" % paths.data_dir())
        else:
            say("Data folder deleted: %s" % paths.data_dir())
    else:
        say("Data kept at %s (use --purge-data to delete)" % paths.data_dir())
    return 0


def cmd_status(args):
    say("Data folder: %s" % paths.data_dir())
    say("Scheduler: %s" % get_backend().status())
    h = hooks_register.status()
    say("Claude hook: %s" % ("registered" if h.get("claude") else "not registered"))
    say("Cursor hooks: %s" % ("registered" if h.get("cursor") else "not registered"))
    pid = proc.server_pid()
    say("Server: %s" % ("running (pid %d)" % pid if pid else "no PID file / not running"))
    say("Dashboard answering: %s" % ("yes" if proc.wait_health(2) else "no"))
    return 0


def cmd_serve(args):
    import server
    proc.write_pid()
    atexit.register(proc.clear_pid)
    argv = ["--port", str(args.port)] if args.port else []
    return server.main(argv) or 0


def cmd_collect(args):
    from collector import collect
    argv = [f for f, on in (('--loop', args.loop), ('--backfill', args.backfill)) if on]
    return collect.main(argv) or 0


def cmd_hook(args):
    from token_burn import hook
    return hook.run_claude() if args.kind == "claude" else hook.run_cursor()


HANDLERS = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status,
            "serve": cmd_serve, "collect": cmd_collect, "hook": cmd_hook}


def _ensure_std_streams():
    """GUI-subsystem exes (token-burnw.exe) start with sys.stdout/sys.stderr None."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main(argv=None):
    _ensure_std_streams()
    args = build_parser().parse_args(argv)
    try:
        rc = HANDLERS[args.cmd](args)
    except KeyboardInterrupt:
        rc = 130
    except Exception as e:
        if args.cmd == "hook":
            return 0
        print("error: %s" % e, file=sys.stderr)
        rc = 1
    return rc if isinstance(rc, int) else 0
