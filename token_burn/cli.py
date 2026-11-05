"""Command line entry point: python -m token_burn <install|uninstall|status|serve|collect|hook|sync|data>."""
import argparse
import atexit
import os
import sys

from token_burn import hooks_register, paths, proc
from token_burn.scheduler import get_backend

EXIT_HELP = """Exit codes: 0 ok, 1 error, 2 bad arguments, 3 refused (duplicate name, conflict, cancelled, temporary folder)."""


def build_parser():
    ap = argparse.ArgumentParser(prog="token-burn", description="Claude, Cursor and opencode token usage dashboard",
                                 epilog=EXIT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("install", help="install: choose folders, optionally share between computers, then register "
                                       "hooks and background tasks and start the dashboard")
    p.add_argument("--install-dir", metavar="PATH", help="program folder (zip builds); default is the recommended one")
    p.add_argument("--data-dir", metavar="PATH", help="data folder (default: the standard one for this computer)")
    p.add_argument("--sync-folder", metavar="PATH", help="share usage through this synced folder")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true", help="start a new shared folder")
    g.add_argument("--join", action="store_true", help="join an existing shared folder")
    p.add_argument("--no-sync", action="store_true", help="this computer only")
    p.add_argument("--machine-name", metavar="NAME", help="this computer's name in the shared folder")
    p.add_argument("--adopt", metavar="NAME", help="take over an existing computer's identity (reinstall)")
    p.add_argument("--yes", action="store_true", help="accept defaults and confirmations (no questions)")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    p.add_argument("--no-start", action="store_true", help="do not start the server now")
    p = sub.add_parser("uninstall", help="remove hooks, background tasks and stop the server")
    p.add_argument("--purge-data", action="store_true", help="also delete the data folder")
    p.add_argument("--remove-from-shared-folder", action="store_true",
                   help="also delete this computer's history from the shared folder")
    p.add_argument("--interactive", action="store_true", help="ask questions and wait at the end (Apps and features)")
    p.add_argument("--yes", action="store_true", help="do not ask; also delete the program folder")
    sub.add_parser("status", help="show what is installed and running")
    p = sub.add_parser("serve", help="run the dashboard server in the foreground")
    p.add_argument("--port", type=int, default=None)
    p = sub.add_parser("collect", help="collect usage once (or in a loop)")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--backfill", action="store_true")
    p = sub.add_parser("hook", help="agent hook handler (reads JSON on stdin)")
    p.add_argument("kind", choices=("claude", "cursor"))
    from token_burn import data_cli, sync_cli
    sync_cli.add_parser(sub)
    data_cli.add_parser(sub)
    return ap


def say(msg):
    print(msg)


def cmd_install(args):
    from token_burn import installer
    return installer.run_install(args)


def cmd_uninstall(args):
    from token_burn import installer
    return installer.run_uninstall(args)


def cmd_sync(args):
    from token_burn import sync_cli
    return sync_cli.cmd_sync(args)


def cmd_data(args):
    from token_burn import data_cli
    return data_cli.cmd_data(args)


def cmd_status(args):
    say("Data folder: %s" % paths.data_dir())
    try:
        from token_burn import data_cli
        _d, how, note = data_cli.status_info()
        say("Data folder chosen by: %s" % how)
        if note:
            say("Note: %s" % note)
        from collector.sync import config
        d = config.load(paths.data_dir()).get("install_dir")
        if d:
            say("Program folder: %s" % d)
    except Exception:
        pass
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
            "serve": cmd_serve, "collect": cmd_collect, "hook": cmd_hook, "sync": cmd_sync, "data": cmd_data}


def _ensure_std_streams():
    """GUI-subsystem exes (token-burnw.exe) start with sys.stdout/sys.stderr None."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main(argv=None):
    _ensure_std_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        if getattr(sys, "frozen", False):
            # Double-clicked program: run the install wizard, then wait so the window does not vanish.
            from token_burn import installer
            return installer.run_wizard_and_pause(parser.parse_args(["install"]))
        parser.print_help()
        return 0
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
