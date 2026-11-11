"""Install wizard, non-interactive install, upgrade and uninstall (standard library only).

Interactive questions go through Console so tests can inject fake input and print. Without a terminal,
or with --yes, --sync-folder or --no-sync, nothing here ever calls input().
"""
import datetime as dt
import os
import re
import shutil
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from token_burn import folder_picker, hooks_register, paths, proc
from token_burn import install_location as il
from token_burn.scheduler import get_backend

STALE_RETIRE_S = 7 * 86400
_MACHINE_NAME = re.compile(r"[0-9a-f]{32}\.json\.gz")
_ICLOUD_STUB = re.compile(r"\.[0-9a-f]{32}\.json\.gz\.icloud")


class Cancelled(Exception):
    """The user said no or closed the input."""


class Refused(Exception):
    """Exit code 3: refused (duplicate name, conflict, temporary folder, cancelled)."""


class BadArgs(Exception):
    """Exit code 2."""


class Failure(Exception):
    """Exit code 1."""


class Console:
    """input() and print() behind one object. Tests pass fake functions."""

    def __init__(self, input_fn=None, print_fn=None, err_fn=None):
        self._input = input_fn
        self._print = print_fn
        self._err = err_fn

    def say(self, msg=""):
        (self._print or print)(msg)

    def err(self, msg):
        if self._err:
            self._err(msg)
        else:
            print(msg, file=sys.stderr)

    def ask(self, prompt, default=""):
        try:
            text = (self._input or input)(prompt + " ")
        except (EOFError, KeyboardInterrupt):
            raise Cancelled()
        text = (text or "").strip()
        return text if text else default

    def yn(self, prompt, default):
        """prompt without the [Y/n] suffix. Anything not starting with y or n gives the default."""
        a = self.ask("%s %s" % (prompt, "[Y/n]" if default else "[y/N]")).lower()
        if a.startswith("y"):
            return True
        if a.startswith("n"):
            return False
        return bool(default)

    def choose(self, prompt, count, default=1):
        """prompt is the full text including the options. Returns an int 1..count."""
        while True:
            a = self.ask(prompt)
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= count:
                return int(a)


def _windows_console_stdin():
    """True when standard input is a real console. On Windows isatty() is also true for NUL, so ask the console
    API: GetConsoleMode succeeds only for a console handle."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.GetStdHandle.restype = ctypes.c_void_p
        k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        handle = k.GetStdHandle(-10)         # STD_INPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        mode = ctypes.c_uint32()
        return bool(k.GetConsoleMode(handle, ctypes.byref(mode)))
    except (AttributeError, OSError, ValueError):
        return False


def is_interactive():
    try:
        if not (bool(sys.stdin) and sys.stdin.isatty()):
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if sys.platform.startswith("win"):
        return _windows_console_stdin()
    return True


def wizard_wanted(args):
    return is_interactive() and not (getattr(args, "yes", False) or getattr(args, "sync_folder", None)
                                     or getattr(args, "no_sync", False))


def app_version():
    try:
        from token_burn import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def short_hostname():
    from collector.sync import export
    return export.hostname().split(".")[0] or "this-computer"


def ago(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return "just now"
    if s < 5400:
        return "%d min ago" % round(s / 60.0)
    if s < 2 * 86400:
        return "%d h ago" % round(s / 3600.0)
    return "%d days ago" % (s // 86400)


# ---- shared folder helpers (also used by sync_cli)

def count_machine_files(folder, own_id=""):
    from collector.sync import config
    mdir = config.shared_root(folder) / "machines"
    try:
        names = os.listdir(mdir)
    except OSError:
        return 0
    n = 0
    for name in names:
        if _MACHINE_NAME.fullmatch(name) or _ICLOUD_STUB.fullmatch(name):
            if own_id and name.startswith(own_id):
                continue
            n += 1
    return n


def list_peers(folder, self_id, now=None, data_dir=None):
    """Other computers in the shared folder, removed ones left out.

    Rows: {id, name, os, hostname, last_seen (epoch or None), state, status}. Sorted by name."""
    from collector.sync import peers
    now = time.time() if now is None else now
    tmp = None
    if data_dir is None:
        tmp = tempfile.mkdtemp(prefix="token-burn-scan-")
        data_dir = tmp
    try:
        scan = peers.scan(folder, self_id, now, data_dir)
        rows = []
        for item in scan:
            control = item.get("control")
            state = control["state"] if control else "active"
            if state == "removed":
                continue
            doc = item.get("doc") or {}
            name = (control or {}).get("display_name") or doc.get("display_name") or doc.get("hostname") \
                or item["id"][:8]
            seen = doc.get("last_seen")
            if seen is None:
                seen = item.get("mtime") or None
            rows.append({"id": item["id"], "name": name, "os": doc.get("os", ""),
                         "hostname": doc.get("hostname", ""), "last_seen": seen, "state": state,
                         "status": item.get("status", "")})
        rows.sort(key=lambda r: r["name"].lower())
        return rows
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def name_taken(rows, name, ignore_ids=()):
    low = name.lower()
    for r in rows:
        if r["id"] in ignore_ids:
            continue
        if r["name"].lower() == low:
            return r
    return None


def _seen_text(row, now):
    return ago(now - row["last_seen"]) if row.get("last_seen") else "unknown"


# ---- sync planning (wizard and `sync enable`)

def _choose_folder(con, mode, own_id=""):
    """Interactive folder step. Returns (folder Path, mode). Loops until usable."""
    from collector.sync import config, peers
    roots = folder_picker.detect_sync_roots()
    title = ("Choose a synced folder for Token Burn" if mode == "new"
             else "Choose the folder you picked on your other computer")
    while True:
        if roots:
            con.say("Found these synced folders:")
            for i, (label, path, _kind) in enumerate(roots, 1):
                con.say("  %d) %s (%s)" % (i, label, path))
        a = con.ask("Press Enter to choose a folder in a window, type a number above, or type or paste a "
                    "folder path:")
        if a == "":
            initial = roots[0][1] if roots else Path.home()
            picked = folder_picker.pick_folder(title, initial)
            if picked is None:
                con.say("The folder window is not available or was cancelled.")
                picked = folder_picker.typed_path(con.ask("Type or paste the folder path:"))
                if picked is None:
                    continue
        elif a.isdigit() and 1 <= int(a) <= len(roots):
            picked = roots[int(a) - 1][1]
        else:
            picked = folder_picker.typed_path(a)
            if picked is None:
                continue
        folder = Path(config.normalise_folder(picked))
        if not folder.exists():
            if not con.yn("That folder does not exist. Create it?", True):
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                con.say("Cannot write to this folder (%s). Choose another folder." % e)
                continue
        if folder_picker.is_work_onedrive(folder):
            if not con.yn("This is a work OneDrive. Your employer's IT policy may limit what you can store "
                          "there. Token Burn only stores token counts, costs, model names and this computer's "
                          "name and OS, never prompts, code or passwords. Use it anyway?", True):
                continue
        elif not folder_picker.looks_synced(folder, roots)[0]:
            if not con.yn("This folder does not look like it is synced to your other computers. Token Burn "
                          "writes a file here every few minutes; your sync app (OneDrive, Dropbox, iCloud "
                          "Drive, Google Drive) or a network share must copy it to the other computers. "
                          "Use it anyway?", False):
                continue
        n = count_machine_files(folder, own_id)
        if mode == "new" and n > 0:
            if con.yn("This folder is already used by %d computers. Join them instead?" % n, True):
                mode = "join"
        elif mode == "join" and n == 0:
            if con.yn("No Token Burn files were found in this folder. Check you picked the same folder as on "
                      "your other computer (it contains a 'token-burn' folder). Start a new shared folder "
                      "here instead?", False):
                mode = "new"
            else:
                continue
        try:
            peers.ensure_layout(folder)
        except OSError as e:
            con.say("Cannot write to this folder (%s). Choose another folder." % e)
            continue
        return folder, mode


def plan_sync(con, cfg, data_dir, folder=None, mode=None, name=None, adopt=None, interactive=False,
              offer_off=False, now=None):
    """Work out the sync settings. Returns None (this computer only) or a plan dict for apply_sync_plan.

    Raises Refused, BadArgs or Failure for the non-interactive problems. Prompts only when interactive."""
    from collector.sync import config, peers
    now = time.time() if now is None else now
    st = config.sync_settings(cfg)
    if interactive and mode is None:
        if offer_off:
            n = con.choose("Do you use Token Burn on more than one computer?\n"
                           "  1) This computer only\n"
                           "  2) Start a new shared folder (first computer)\n"
                           "  3) Join an existing shared folder (I already set up another computer)\n"
                           "Choose 1, 2 or 3 [1]:", 3, 1)
            if n == 1:
                return None
            mode = "new" if n == 2 else "join"
        else:
            n = con.choose("Is this the first computer using the shared folder?\n"
                           "  1) Start a new shared folder (first computer)\n"
                           "  2) Join an existing shared folder (I already set up another computer)\n"
                           "Choose 1 or 2 [1]:", 2, 1)
            mode = "new" if n == 1 else "join"
    own_id = st["machine_id"]
    if interactive:
        folder, mode = _choose_folder(con, mode or "new", own_id)
    else:
        if not folder:
            raise BadArgs("A shared folder is needed: use --sync-folder PATH.")
        folder = Path(config.normalise_folder(folder))
        if not folder.exists():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise Failure("Cannot write to this folder (%s). Choose another folder." % e)
            con.say("Created %s" % folder)
        if not folder_picker.is_work_onedrive(folder) and not folder_picker.looks_synced(folder)[0]:
            con.say("Note: this folder does not look like it is synced to your other computers.")
        n = count_machine_files(folder, own_id)
        if mode is None:
            mode = "join" if (n > 0 or adopt) else "new"
        if mode == "join" and n == 0:
            raise Refused("No Token Burn files were found in %s. Check you picked the same folder as on your "
                          "other computer, or use --new." % folder)
        try:
            peers.ensure_layout(folder)
        except OSError as e:
            raise Failure("Cannot write to this folder (%s). Choose another folder." % e)
    rows = list_peers(folder, own_id, now)
    if rows and interactive:
        con.say("Computers already using this folder:")
        w = max(len(r["name"]) for r in rows)
        for r in rows:
            tag = "  (retired)" if r["state"] == "retired" else ""
            con.say("  %-*s  %-12s  last seen %s%s" % (w, r["name"], r["os"], _seen_text(r, now), tag))
    # identity
    machine_id, adopted, adopted_from = own_id, False, None
    delete_tombstone = None
    if own_id:
        control = peers.read_control(str(folder), own_id)
        if control and control["state"] == "removed":
            delete_tombstone = own_id
            machine_id = ""
    chosen_name = None
    if adopt:
        if st["machine_id"]:
            raise Refused("This computer already has an identity. Run token-burn sync reset-id first, then "
                          "adopt.")
        hit = name_taken(rows, adopt)
        if hit is None:
            raise Refused("No computer named '%s' was found in the shared folder." % adopt)
        machine_id, adopted, adopted_from, chosen_name = hit["id"], True, hit["id"], hit["name"]
    elif interactive and mode == "join" and not st["machine_id"]:
        host = short_hostname().lower()
        for r in rows:
            if r["state"] == "active" and r["hostname"].split(".")[0].lower() == host:
                if con.yn("Is this computer '%s' (for example, you reinstalled Windows or Token Burn)? Its "
                          "history will stay under one name." % r["name"], True):
                    machine_id, adopted, adopted_from, chosen_name = r["id"], True, r["id"], r["name"]
                    break
    if not machine_id:
        machine_id = config.new_machine_id()
    # stale computers
    retire = []
    if interactive:
        for r in rows:
            if r["id"] == adopted_from or r["state"] != "active" or not r["last_seen"]:
                continue
            if now - r["last_seen"] > STALE_RETIRE_S:
                days = int((now - r["last_seen"]) // 86400)
                if con.yn("'%s' has not reported for %d days. If you replaced it, retire it so it stops "
                          "showing a warning (its history still counts). Retire it now?" % (r["name"], days),
                          False):
                    retire.append(r["id"])
    # name
    others = [r for r in rows if r["id"] != adopted_from and r["id"] != machine_id]
    default_name = chosen_name or st["machine_name"] or short_hostname()
    if chosen_name:
        final_name = chosen_name
    elif interactive:
        while True:
            cand = con.ask("Name for this computer [%s]:" % default_name, default_name)
            try:
                cand = config.clean_name(cand)
            except ValueError as e:
                con.say("%s. Please choose a different name." % str(e).capitalize())
                continue
            clash = name_taken(others, cand)
            if clash:
                con.say("The name '%s' is already used by another computer (last seen %s). Please choose a "
                        "different name." % (cand, _seen_text(clash, now)))
                continue
            final_name = cand
            break
    else:
        try:
            final_name = config.clean_name(name or default_name)
        except ValueError as e:
            raise BadArgs("Invalid computer name: %s" % e)
        clash = name_taken(others, final_name)
        if clash:
            raise Refused("The name '%s' is already used by another computer (last seen %s). Choose a "
                          "different name with --machine-name." % (final_name, _seen_text(clash, now)))
    joined = st["joined_at"] if (machine_id == st["machine_id"] and st["joined_at"]) else _iso(now)
    return {"folder": str(folder), "mode": mode, "machine_id": machine_id, "machine_name": final_name,
            "adopted": adopted or (adopted_from is None and st["adopted"] and machine_id == st["machine_id"]),
            "adopted_from": adopted_from, "retire": retire, "delete_tombstone": delete_tombstone,
            "joined_at": joined, "peers": len(rows)}


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_sync_plan(con, plan, cfg, data_dir, now=None):
    """Carry out a plan_sync result: shared folder files, adopted history, retirements. Updates cfg in place."""
    from collector.sync import peers
    now = time.time() if now is None else now
    folder = plan["folder"]
    try:
        peers.ensure_layout(folder)
    except OSError as e:
        raise Failure("Cannot write to the shared folder (%s)." % e)
    if plan.get("delete_tombstone"):
        peers.delete_control(folder, plan["delete_tombstone"])
    if plan.get("adopted_from"):
        src = peers.machine_file(folder, plan["adopted_from"])
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src), str(Path(data_dir) / ("adopted-%s.json.gz" % plan["adopted_from"])))
        except OSError as e:
            con.say("Could not copy the earlier history of '%s' (%s); it will be rebuilt from this computer."
                    % (plan["machine_name"], e))
    for rid in plan.get("retire", []):
        try:
            peers.write_control(folder, rid, "retired", None, plan["machine_id"], now)
        except (OSError, ValueError) as e:
            con.say("Could not retire a computer (%s)." % e)
    root = str(Path(folder) / "token-burn")
    low = folder.lower()
    if sys.platform.startswith("win") and "onedrive" in low:
        con.say("Asking OneDrive to always keep these files on this computer.")
        folder_picker.pin_onedrive(root)
    elif sys.platform == "darwin" and ("mobile documents" in low or "icloud" in low):
        con.say("Tip: in Finder, right-click the token-burn folder and choose Keep Downloaded so iCloud does "
                "not evict the files.")
    cfg["sync"] = {"enabled": True, "folder": folder, "machine_id": plan["machine_id"],
                   "machine_name": plan["machine_name"], "joined_at": plan["joined_at"],
                   "adopted": bool(plan["adopted"])}
    return cfg


# ---- background tasks, hooks, dashboard

def stop_background(backend, say):
    for m in backend.uninstall():
        say(m)
    proc.stop_server()


def register_hooks(install_dir, say):
    with paths.use_install_dir(install_dir):
        for name, fn, kind in (("Claude hook", hooks_register.install_claude, "claude"),
                               ("Cursor hooks", hooks_register.install_cursor, "cursor")):
            try:
                say("%s: %s" % (name, fn(paths.hook_command(kind))))
            except Exception as e:
                say("%s: FAILED (%s)" % (name, e))


def register_background(backend, install_dir, say):
    with paths.use_install_dir(install_dir):
        refresh = paths.self_command("collect")
        serve = paths.self_command("serve")
    for m in backend.install(refresh, serve):
        say(m)


def start_dashboard(backend, install_dir, say, open_browser):
    with paths.use_install_dir(install_dir):
        started = backend.start_server()
        if not started:
            proc.start_server()
    if proc.wait_health(20):
        say("Dashboard is up at %s" % paths.URL)
    else:
        say("Server did not answer on %s yet; check the log in the data folder." % paths.URL)
    if open_browser:
        try:
            webbrowser.open(paths.URL)
        except Exception:
            pass


def collect_now(data_dir):
    """One collection pass for data_dir (in this process when it is the active folder, else a child process)."""
    from collector import util
    if il.same_path(util.DATA_DIR, data_dir):
        from collector import collect
        collect.run()
        return True
    import subprocess
    env = dict(os.environ, TOKEN_BURN_DATA_DIR=str(data_dir))
    try:
        subprocess.run(paths.self_command("collect", windowless=False), env=env, timeout=180,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---- install

def _validate_args(args):
    if args.new and args.join:
        raise BadArgs("Use either --new or --join, not both.")
    if args.no_sync and (args.sync_folder or args.new or args.join or args.machine_name or args.adopt):
        raise BadArgs("--no-sync cannot be combined with sync options.")
    if (args.new or args.join or args.machine_name or args.adopt) and not args.sync_folder:
        raise BadArgs("--new, --join, --machine-name and --adopt need --sync-folder PATH.")
    if args.adopt and args.new:
        raise BadArgs("--adopt means joining the shared folder; it cannot be combined with --new.")


def _menu_for_existing(con, st):
    if st["enabled"]:
        sync_txt = "on, folder %s, this computer '%s'" % (st["folder"], st["machine_name"])
    else:
        sync_txt = "off"
    con.say("Token Burn is already set up here. Sync: %s." % sync_txt)
    n = con.choose("  1) Keep these settings and repair\n  2) Change sync settings\n  3) Change data folder\n"
                   "  4) Cancel\nChoose 1, 2, 3 or 4 [1]:", 4, 1)
    return ("repair", "sync", "data", "cancel")[n - 1]


def _ask_folder(con, intro, default, title, validator, choice_label):
    """Prompt loop for one folder. default is used on Enter; B opens the native picker."""
    while True:
        a = con.ask("%s\n%s\nPress Enter to %s, type B to browse for a folder, or type or paste a folder "
                    "path:" % (intro[0], intro[1], choice_label))
        if a == "":
            cand = default
        elif a.lower() == "b":
            cand = folder_picker.pick_folder(title, default)
            if cand is None:
                con.say("The folder window is not available or was cancelled. Type or paste a folder path "
                        "instead.")
                continue
        else:
            cand = a
        path, err = validator(cand)
        if err:
            con.say(err)
            continue
        if not path.exists() and not con.yn("That folder does not exist. Create it?", True):
            continue
        return path


def _folder_screen(con, program, data, src, env_locked, cur_data):
    """The combined program and data folder screen. program is None for pip installs."""
    def check_program(p):
        return il.check_install_dir(p, data_dir=data_ref[0], src_dir=src)

    def check_data(p):
        return il.check_data_dir(p, program_dir=program_ref[0], current=cur_data)
    program_ref, data_ref = [program], [data]
    while True:
        con.say("Token Burn will use these folders:")
        if program_ref[0] is not None:
            con.say("  Program: %s" % program_ref[0])
        con.say("  Data:    %s%s" % (data_ref[0], "  (set by TOKEN_BURN_DATA_DIR)" if env_locked else ""))
        a = con.ask("Press Enter to accept, or type C to change them:")
        if a == "":
            bad = None
            if program_ref[0] is not None:
                _p, bad = check_program(program_ref[0])
            if bad is None and not env_locked:
                _d, bad = check_data(data_ref[0])
            if bad is None:
                return program_ref[0], data_ref[0]
            con.say(bad)
        elif a.lower() != "c":
            continue
        if program_ref[0] is not None:
            rec = il.recommended_dir()
            program_ref[0] = _ask_folder(
                con, ("Where should Token Burn be installed?", "  Recommended: %s" % rec), rec,
                "Choose where to install Token Burn", check_program, "use the recommended folder")
        if not env_locked:
            data_ref[0] = _ask_folder(
                con, ("Where should Token Burn keep its data?", "  Current: %s" % data_ref[0]), data_ref[0],
                "Choose where Token Burn keeps its data", check_data, "keep it")


def run_install(args, con=None):
    con = con or Console()
    try:
        return _install(args, con)
    except Cancelled:
        con.say("Cancelled. Nothing more was changed.")
        return 3
    except Refused as e:
        con.say(str(e))
        return 3
    except BadArgs as e:
        con.err("error: %s" % e)
        return 2
    except (Failure, il.InstallLocationError) as e:
        con.err("error: %s" % e)
        return 1


def _install(args, con):
    from collector.sync import config
    say = con.say
    ver = app_version()
    wizard = wizard_wanted(args)
    _validate_args(args)
    src = il.running_dir()
    if src is not None:
        ok, msg = il.check_running_location(src)
        if not ok:
            raise Refused(msg)
    env_locked = bool(os.environ.get("TOKEN_BURN_DATA_DIR"))
    cur_data = paths.data_dir()
    cfg = config.load(cur_data)
    st = config.sync_settings(cfg)
    existing = bool(cfg)
    old_dir = cfg.get("install_dir") if isinstance(cfg.get("install_dir"), str) else None
    old_dir = Path(old_dir) if old_dir and il.is_install(old_dir) else None
    choice = "fresh"
    if wizard:
        say("Welcome to Token Burn. It shows your Claude Code, Cursor, opencode and OpenAI token usage on a private "
            "page at %s. Nothing leaves this computer unless you choose a shared folder in the next step."
            % paths.URL)
        if existing:
            choice = _menu_for_existing(con, st)
            if choice == "cancel":
                raise Cancelled()
    elif existing:
        choice = "repair"

    # program folder and data folder
    program = None
    data_dir = cur_data
    if args.data_dir:
        if env_locked and not il.same_path(args.data_dir, cur_data):
            raise Refused("TOKEN_BURN_DATA_DIR is set, so the data folder cannot be changed here. Remove the "
                          "variable first.")
        d, err = il.check_data_dir(args.data_dir, program_dir=old_dir, current=cur_data)
        if err:
            raise Refused(err)
        data_dir = d
    if src is not None:
        if args.install_dir:
            program, err = il.check_install_dir(args.install_dir, data_dir=data_dir, src_dir=src)
            if err:
                raise Refused(err)
        elif old_dir is not None:
            program = old_dir
        elif not wizard or existing:
            program, err = il.check_install_dir(il.recommended_dir(), data_dir=data_dir, src_dir=src)
            if err:
                raise Refused("The recommended folder cannot be used: %s Use --install-dir PATH." % err)
        else:
            program = il.recommended_dir()
    if wizard and choice == "fresh" and not args.install_dir and not args.data_dir:
        program, data_dir = _folder_screen(con, program, data_dir, src, env_locked, cur_data)
    data_move = None
    if wizard and choice == "data":
        if env_locked:
            raise Refused("TOKEN_BURN_DATA_DIR is set, so the data folder cannot be changed here.")
        data_dir = _ask_folder(
            con, ("Where should Token Burn keep its data?", "  Current: %s" % cur_data), cur_data,
            "Choose where Token Burn keeps its data",
            lambda p: il.check_data_dir(p, program_dir=program or old_dir, current=cur_data), "keep it")
    if not il.same_path(data_dir, cur_data):
        data_move = data_dir
        target_cfg = config.load(data_dir)
        if config.sync_settings(target_cfg)["machine_id"]:
            # The chosen folder already holds this computer's identity (a kept data folder): reuse it.
            cfg, st, existing = target_cfg, config.sync_settings(target_cfg), True

    # upgrade prompt
    upgrade = bool(src is not None and program is not None and il.is_install(program)
                   and not il.same_path(src, program))
    if upgrade and wizard:
        old_ver = cfg.get("app_version") or "an earlier version"
        if old_ver == ver:
            q = "Token Burn %s is installed in %s. Replace it with this copy?" % (ver, program)
        else:
            q = "Token Burn %s is installed in %s. Upgrade it to %s?" % (old_ver, program, ver)
        if not con.yn(q, True):
            raise Cancelled()

    # sync
    sync_plan, sync_off = None, False
    if args.no_sync:
        sync_off = True
    elif args.sync_folder:
        sync_plan = plan_sync(con, cfg, data_dir, folder=args.sync_folder,
                              mode="join" if args.join else "new" if args.new else None,
                              name=args.machine_name, adopt=args.adopt, interactive=False)
    elif wizard and choice in ("fresh", "sync"):
        sync_plan = plan_sync(con, cfg, data_dir, interactive=True, offer_off=(choice == "fresh"))
        if sync_plan is None:
            sync_off = True
    elif not existing:
        sync_off = True        # --yes or no terminal with no sync flags: this computer only

    # confirm
    if wizard:
        say("Ready to install:")
        if program is not None:
            say("  Program folder: %s" % program)
        say("  Data folder:    %s" % data_dir)
        if sync_plan:
            say("  Shared folder:  %s" % (Path(sync_plan["folder"]) / "token-burn"))
            say("  This computer:  %s" % sync_plan["machine_name"])
        elif sync_off or not st["enabled"]:
            say("  Sharing:        this computer only")
        else:
            say("  Shared folder:  %s" % (Path(st["folder"]) / "token-burn"))
            say("  This computer:  %s" % st["machine_name"])
        say("  Background tasks: every minute, plus the dashboard at log on")
        if not con.yn("Install now?", True):
            raise Cancelled()
    return _execute(con, args, cfg, cur_data, data_dir, data_move, program, src, upgrade, sync_plan, sync_off,
                    wizard, ver, env_locked)


def _execute(con, args, cfg, cur_data, data_dir, data_move, program, src, upgrade, sync_plan, sync_off,
             wizard, ver, env_locked):
    from collector import util
    from collector.sync import config
    say = con.say
    backend = get_backend()
    say("Data folder: %s" % data_dir)
    # 1. data folder (a pointer, or a move of existing data)
    if data_move is not None:
        if (Path(cur_data) / "usage.db").is_file():
            from token_burn import data_cli
            data_cli.move_data(con, data_move, program, restart=False, backend=backend,
                               delete_old="ask" if wizard else ("yes" if args.yes else "no"))
        else:
            util.ensure_dir(data_dir)
            if not env_locked:
                il.apply_data_dir(data_dir)
    else:
        util.ensure_dir(data_dir)
    # 2. program files
    created = False
    if program is not None and src is not None:
        if upgrade:
            say("Upgrading Token Burn in %s" % program)
            stop_background(backend, say)
            il.release_folder(program, say)
        try:
            exclude = [data_dir, cur_data]
            if upgrade:
                res = il.upgrade_files(src, program, say, exclude, version=ver)
            else:
                res = il.install_files(src, program, say, exclude, version=ver)
                created = res == "installed"
        except il.RollbackError as e:
            say(str(e))
            raise Failure("The upgrade failed and the previous version is NOT in place, so the background tasks "
                          "and the dashboard were left stopped. Rename %s to %s, then run the installer again."
                          % (e.old, e.dst))
        except (il.InstallLocationError, OSError, shutil.Error) as e:
            if upgrade:
                say(str(e))
                say("Restoring the previous version's background tasks.")
                register_background(backend, program, say)
                start_dashboard(backend, program, say, False)
                raise Failure("The upgrade did not finish; the previous version is still installed.")
            if isinstance(e, il.InstallLocationError):
                raise
            raise Failure("Token Burn could not copy its files to %s (%s)." % (program, e))
        say("Program folder: %s (%s)" % (program, res))
    # 3. config.json
    cfg = config.load(data_dir) or cfg
    cfg["version"] = cfg.get("version") or 1
    cfg["app_version"] = ver
    cfg["data_dir"] = str(data_dir)
    if program is not None:
        cfg["install_dir"] = str(program)
    if sync_plan:
        apply_sync_plan(con, sync_plan, cfg, data_dir)
    elif sync_off:
        s = cfg.get("sync") if isinstance(cfg.get("sync"), dict) else {}
        s = dict(s)
        s["enabled"] = False
        cfg["sync"] = s
    config.save(data_dir, cfg)
    # 4. hooks, tasks, integration, server
    proc.stop_server()
    register_hooks(program, say)
    register_background(backend, program, say)
    if program is not None:
        for m in il.register_os_integration(program, ver):
            say(m)
    check = (True, "")
    if not args.no_start:
        start_dashboard(backend, program, say, False)
        check = _check_scheduler(backend)
        if not check[0]:
            check = _scheduler_failed(con, args, backend, program, src, created, data_dir, cfg, ver, check,
                                      wizard)
            program = check[2]
    if sync_plan:
        collect_now(data_dir)
    say("Done. Dashboard: %s" % paths.URL)
    if sync_plan and sync_plan["mode"] == "new":
        say("On your other computers, run the installer and choose 'Join an existing shared folder', then "
            "pick the same folder.")
    if not args.no_start and not args.no_open:
        try:
            webbrowser.open(paths.URL)
        except Exception:
            pass
    return 0


def _check_scheduler(backend):
    fn = getattr(backend, "check_refresh", None)
    if fn is None:
        return True, "not verified"
    try:
        ok, detail = fn()
    except Exception as e:
        return True, "not verified (%s)" % e
    return bool(ok), detail


def _scheduler_failed(con, args, backend, program, src, created, data_dir, cfg, ver, check, wizard):
    """The Refresh task did not run. Offer the recommended folder when a custom one was used. Returns a tuple
    (ok, detail, program)."""
    from collector.sync import config
    say = con.say
    rec = il.recommended_dir()
    say("The background task did not run from %s (%s)." % (program or "this install", check[1]))
    custom = program is not None and src is not None and not il.same_path(program, rec)
    if not custom:
        say("Run token-burn status for details, or see docs/TROUBLESHOOTING.md.")
        return False, check[1], program
    if not (wizard and con.yn("Some Windows setups cannot start background tasks from some folders. Move "
                              "Token Burn to the recommended folder %s?" % rec, True)):
        say("You can move Token Burn later by running the installer again and choosing the recommended "
            "folder.")
        return False, check[1], program
    target, err = il.check_install_dir(rec, data_dir=data_dir, src_dir=src)
    if err:
        say("The recommended folder cannot be used: %s" % err)
        return False, check[1], program
    try:
        if il.is_install(target):
            stop_background(backend, say)
            il.release_folder(target, say)
            il.upgrade_files(src, target, say, [data_dir], version=ver)
        else:
            il.install_files(src, target, say, [data_dir], version=ver)
    except (il.InstallLocationError, OSError, shutil.Error) as e:
        say(str(e))
        return False, check[1], program
    cfg["install_dir"] = str(target)
    config.save(data_dir, cfg)
    stop_background(backend, say)
    register_hooks(target, say)
    register_background(backend, target, say)
    for m in il.register_os_integration(target, ver):
        say(m)
    start_dashboard(backend, target, say, False)
    ok, detail = _check_scheduler(backend)
    if ok and created and il.is_install(program) and not il.same_path(program, target):
        il.remove_program_dir(program, say, data_dir)
    if not ok:
        say("The background task still did not run (%s)." % detail)
    return ok, detail, target


def run_wizard_and_pause(args, con=None):
    """Frozen build started without arguments: run the install, then wait for Enter."""
    con = con or Console()
    rc = run_install(args, con)
    try:
        con.ask("Press Enter to close this window.")
    except Cancelled:
        pass
    return rc


# ---- uninstall

def run_uninstall(args, con=None):
    con = con or Console()
    after = []          # work that must wait until this process is about to exit (deleting its own program folder)
    try:
        rc = _uninstall(args, con, after)
    except Cancelled:
        con.say("Cancelled.")
        rc = 3
    except Refused as e:
        con.say(str(e))
        rc = 3
    except (Failure, il.InstallLocationError) as e:
        con.err("error: %s" % e)
        rc = 1
    if getattr(args, "interactive", False):
        try:
            con.ask("Press Enter to close this window.")
        except Cancelled:
            pass
    for fn in after:
        try:
            fn()
        except Exception as e:
            con.err("error: %s" % e)
            rc = 1
    return rc


def _can_ask(args):
    return not getattr(args, "yes", False) and (getattr(args, "interactive", False) or is_interactive())


def _purge_ok(path):
    p = Path(path)
    try:
        r = p.resolve()
    except (OSError, RuntimeError):
        return False
    return r.parent != r and len(r.parts) >= 3 and not il.same_path(r, Path.home())


def _release_and_remove(install_dir, say, data_dir):
    """End anything still running from the program folder (never this process), then delete it."""
    left = il.release_folder(install_dir, say)
    if left:
        say("Still running from %s: %s" % (install_dir, ", ".join("%s (PID %d)" % (n, p) for p, n, _e in left)))
    return il.remove_program_dir(install_dir, say, data_dir)


def _uninstall(args, con, after=None):
    from collector import util
    from collector.sync import config, peers
    say = con.say
    backend = get_backend()
    data_dir = paths.data_dir()
    cfg = config.load(data_dir)
    st = config.sync_settings(cfg)
    install_dir = cfg.get("install_dir") if isinstance(cfg.get("install_dir"), str) else None
    ask = _can_ask(args)
    for m in backend.uninstall():
        say(m)
    say("Server: %s" % ("stopped" if proc.stop_server() else "not running"))
    for name, fn in (("Claude hook", hooks_register.uninstall_claude),
                     ("Cursor hooks", hooks_register.uninstall_cursor)):
        try:
            say("%s: %s" % (name, fn()))
        except Exception as e:
            say("%s: FAILED (%s)" % (name, e))
    if install_dir or il.running_dir() is not None:
        for m in il.unregister_os_integration(install_dir or il.running_dir()):
            say(m)
    if args.remove_from_shared_folder and st["machine_id"] and st["folder"]:
        try:
            peers.delete_machine_file(st["folder"], st["machine_id"])
            peers.write_control(st["folder"], st["machine_id"], "removed", None, st["machine_id"], time.time())
            say("Removed this computer from the shared folder.")
        except (OSError, ValueError) as e:
            say("Could not remove this computer from the shared folder (%s)." % e)
    elif st["enabled"]:
        say("Your history in the shared folder was kept, so other computers still show it. To remove it, run "
            "token-burn sync remove \"%s\" on any computer, or use --remove-from-shared-folder now."
            % st["machine_name"])
    if args.purge_data:
        go = True
        if ask:
            go = con.yn("Delete the data folder %s? This also removes this computer's ID." % data_dir, False)
        if go and not _purge_ok(data_dir):
            say("Data folder not deleted (unsafe path): %s" % data_dir)
        elif go:
            _gone, removed = il.remove_data_files(data_dir)
            if removed or not Path(data_dir).exists():
                say("Data folder deleted: %s" % data_dir)
            elif (Path(data_dir) / "usage.db").exists():
                say("Data folder not fully deleted (in use?): %s" % data_dir)
            else:
                say("Token Burn's data files were deleted; other files in %s were left alone." % data_dir)
            if not (Path(data_dir) / "usage.db").exists() and not os.environ.get("TOKEN_BURN_DATA_DIR")                     and util.read_location() is not None:
                # Only a purge forgets the data folder; keeping the data keeps the pointer, so a reinstall
                # finds the same folder and the same computer ID.
                util.clear_location()
                say("Data folder pointer (location.json): removed")
        else:
            say("Data kept at %s" % data_dir)
    else:
        say("Data kept at %s (use --purge-data to delete)" % data_dir)
    if install_dir and Path(install_dir).is_dir():
        if args.yes:
            go = True
        elif ask:
            go = con.yn("Also delete the program folder %s?" % install_dir, True)
        else:
            go = False
        if go and after is not None and getattr(args, "interactive", False):
            # This window is still open and the program is running from the folder: delete after the pause.
            say("The program folder will be deleted when this window closes: %s" % install_dir)
            after.append(lambda: _release_and_remove(install_dir, say, data_dir))
        elif go:
            _release_and_remove(install_dir, say, data_dir)
        else:
            say("Program folder kept at %s" % install_dir)
    return 0
