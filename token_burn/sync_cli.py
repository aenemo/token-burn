"""token-burn sync ... (standard library only; the collector.sync modules are imported inside functions)."""
import json
import re
import time
from pathlib import Path

from token_burn import installer, paths

_HEX_PREFIX = re.compile(r"[0-9a-f]{8,32}")
KV_KEYS = ("sync:last_hash", "sync:last_write", "sync:last_token", "sync:conflict")


def add_parser(sub):
    p = sub.add_parser("sync", help="share usage between your computers through a synced folder")
    ssub = p.add_subparsers(dest="sync_cmd")
    ssub.required = True
    ssub.add_parser("status", help="show whether sync is on and its latest status")
    e = ssub.add_parser("enable", help="turn sync on (asks questions in a terminal)")
    e.add_argument("--folder", metavar="PATH", help="the synced folder to share through")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true", help="start a new shared folder")
    g.add_argument("--join", action="store_true", help="join an existing shared folder")
    e.add_argument("--machine-name", metavar="NAME", help="this computer's name")
    e.add_argument("--yes", action="store_true",
                   help="ask no questions (for scripts; needs --folder). Enabling sync has no confirmation to skip")
    d = ssub.add_parser("disable", help="turn sync off")
    d.add_argument("--remove-my-data", action="store_true",
                   help="also delete this computer's file from the shared folder")
    d.add_argument("--yes", action="store_true",
                   help="accepted for scripts. Turning sync off asks nothing; only --remove-my-data asks, and "
                        "this skips that question")
    ssub.add_parser("machines", help="list the computers in the shared folder")
    r = ssub.add_parser("rename", help="rename this computer, or another one with --machine")
    r.add_argument("new_name", metavar="NEW_NAME")
    r.add_argument("--machine", metavar="NAME", help="rename the computer named NAME instead of this one")
    t = ssub.add_parser("retire", help="stop warning about a computer that has not reported")
    t.add_argument("name", metavar="NAME")
    u = ssub.add_parser("unretire", help="undo retire")
    u.add_argument("name", metavar="NAME")
    x = ssub.add_parser("remove", help="delete a computer's history from the shared folder")
    x.add_argument("name", metavar="NAME")
    x.add_argument("--yes", action="store_true",
                   help="delete without asking you to type the computer's name (needed when not in a terminal)")
    i = ssub.add_parser("reset-id", help="give this computer a new ID (for a cloned computer)")
    i.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ssub.add_parser("now", help="collect and sync once now")
    return p


def _setup():
    from collector.sync import config
    data_dir = paths.data_dir()
    cfg = config.load(data_dir)
    return config, data_dir, cfg, config.sync_settings(cfg)


def _need_sync(con, st):
    if not st["enabled"]:
        raise installer.Failure("Sync is off. Run token-burn sync enable first.")


def _confirm(con, args, question, default=False):
    """True to go ahead. --yes confirms; without a terminal and without --yes the action is refused."""
    if getattr(args, "yes", False):
        return True
    if not installer.is_interactive():
        raise installer.Refused("This needs confirmation. Run it in a terminal, or add --yes.")
    return con.yn(question, default)


def _rows(st, now, data_dir=None):
    """Peers plus this computer, each {id, name, os, last_seen, state, status, is_self}."""
    from collector.sync import export
    rows = installer.list_peers(st["folder"], st["machine_id"], now, data_dir)
    for r in rows:
        r["is_self"] = False
    me = {"id": st["machine_id"], "name": st["machine_name"] or st["machine_id"][:8], "os": export.os_name(),
          "hostname": export.hostname(), "last_seen": now, "state": "active", "status": "this computer",
          "is_self": True}
    return [me] + rows


def resolve(con, rows, text):
    """The row matching text: effective name (case-insensitive) or an 8 to 32 character id prefix."""
    low = text.strip().lower()
    hits = [r for r in rows if r["name"].lower() == low]
    if not hits and _HEX_PREFIX.fullmatch(low):
        hits = [r for r in rows if r["id"].startswith(low)]
    if not hits:
        raise installer.Failure("No computer named '%s'. Run token-burn sync machines to see them." % text)
    if len(hits) > 1:
        con.say("More than one computer matches '%s':" % text)
        for r in hits:
            con.say("  %s  (id %s)" % (r["name"], r["id"][:8]))
        raise installer.Refused("Use the 8-character id instead of the name.")
    return hits[0]


def _usage_sync_block(data_dir):
    try:
        with open(Path(data_dir) / "usage.json", "r", encoding="utf-8") as f:
            doc = json.load(f)
        block = doc.get("sync")
        return block if isinstance(block, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def cmd_status(con, args):
    config, data_dir, cfg, st = _setup()
    say = con.say
    if not st["enabled"]:
        say("Sync: off")
        say("Run token-burn sync enable to share usage between your computers.")
        return 0
    say("Sync: on")
    found = (Path(st["folder"]) / "token-burn").is_dir()
    say("Shared folder: %s%s" % (Path(st["folder"]) / "token-burn", "" if found else " (not found)"))
    say("This computer: %s (id %s)" % (st["machine_name"], st["machine_id"][:8]))
    block = _usage_sync_block(data_dir)
    if block.get("status"):
        say("Status: %s" % block["status"])
    if block.get("last_export"):
        say("Last export: %s" % block["last_export"])
    for m in block.get("messages", []) if isinstance(block.get("messages"), list) else []:
        say("  %s" % m)
    return 0


def cmd_machines(con, args):
    config, data_dir, cfg, st = _setup()
    _need_sync(con, st)
    now = time.time()
    rows = _rows(st, now, data_dir)
    w = max(len(r["name"]) for r in rows)
    con.say("%-*s  %-14s  %-14s  %-8s  %s" % (w, "Name", "OS", "Last seen", "State", "ID"))
    for r in rows:
        seen = "this computer" if r["is_self"] else installer.ago(now - r["last_seen"]) if r["last_seen"] else "unknown"
        con.say("%-*s  %-14s  %-14s  %-8s  %s" % (w, r["name"], r["os"], seen, r["state"], r["id"][:8]))
        if r["status"] and not r["is_self"] and r["status"] != "ok":
            con.say("    %s" % r["status"])
    return 0


def cmd_enable(con, args):
    config, data_dir, cfg, st = _setup()
    interactive = installer.is_interactive() and not args.yes and not args.folder
    if not interactive and not args.folder:
        raise installer.BadArgs("--folder PATH is needed when not running in a terminal.")
    mode = "join" if args.join else "new" if args.new else None
    plan = installer.plan_sync(con, cfg, data_dir, folder=args.folder, mode=mode, name=args.machine_name,
                               interactive=interactive, offer_off=False)
    if plan is None:
        return 0
    installer.apply_sync_plan(con, plan, cfg, data_dir)
    # A computer that never ran the installer has no config yet: write the same header the installer does.
    cfg["version"] = cfg.get("version") or 1
    cfg["app_version"] = installer.app_version()
    config.save(data_dir, cfg)
    con.say("Sync is on. Shared folder: %s. This computer: %s." % (Path(plan["folder"]) / "token-burn",
                                                                   plan["machine_name"]))
    return _now(con, quiet=True)


def cmd_disable(con, args):
    config, data_dir, cfg, st = _setup()
    if not st["enabled"]:
        con.say("Sync is already off.")
        return 0
    if args.remove_my_data:
        from collector.sync import peers
        if not _confirm(con, args, "Delete this computer's file from the shared folder? Other computers will "
                                   "stop showing its history."):
            raise installer.Cancelled()
        peers.delete_machine_file(st["folder"], st["machine_id"])
        peers.write_control(st["folder"], st["machine_id"], "removed", None, st["machine_id"], time.time())
        con.say("This computer's file was removed from the shared folder.")
    sync = dict(cfg.get("sync") or {})
    sync["enabled"] = False
    cfg["sync"] = sync
    config.save(data_dir, cfg)
    con.say("Sync is off. Usage stays on this computer only.")
    return 0


def cmd_rename(con, args):
    from collector.sync import config as cfgmod, peers
    config, data_dir, cfg, st = _setup()
    _need_sync(con, st)
    try:
        new = cfgmod.clean_name(args.new_name)
    except ValueError as e:
        raise installer.BadArgs("Invalid name: %s" % e)
    now = time.time()
    rows = _rows(st, now, data_dir)
    target = resolve(con, rows, args.machine) if args.machine else rows[0]
    clash = installer.name_taken([r for r in rows if r["state"] != "removed"], new, ignore_ids=(target["id"],))
    if clash:
        raise installer.Refused("The name '%s' is already used by another computer." % new)
    if target["is_self"]:
        sync = dict(cfg.get("sync") or {})
        sync["machine_name"] = new
        cfg["sync"] = sync
        config.save(data_dir, cfg)
        _rename_self_control(con, st, new, now)
        con.say("This computer is now called '%s'." % new)
        return _now(con, quiet=True)
    peers.write_control(st["folder"], target["id"], target["state"], new, st["machine_id"], now)
    con.say("'%s' is now called '%s'." % (target["name"], new))
    return 0


def _rename_self_control(con, st, new, now):
    """Another computer may have renamed this one through a control file, which outranks the computer's own
    name. Write the new name there too so it wins everywhere. Nothing is written when there is no such file."""
    from collector.sync import peers
    try:
        control = peers.read_control(st["folder"], st["machine_id"])
        if control is not None:
            peers.write_control(st["folder"], st["machine_id"], control["state"], new, st["machine_id"], now)
    except (OSError, ValueError) as e:
        con.say("Could not update the name in the shared folder's control file (%s)." % e)


def _set_state(con, args, state):
    from collector.sync import peers
    config, data_dir, cfg, st = _setup()
    _need_sync(con, st)
    now = time.time()
    rows = _rows(st, now, data_dir)
    target = resolve(con, rows, args.name)
    if target["is_self"]:
        raise installer.Refused("This computer is running, so it cannot be retired.")
    if target["state"] == state:
        con.say("'%s' is already %s." % (target["name"], state if state != "active" else "active (not retired)"))
        return 0
    peers.write_control(st["folder"], target["id"], state, None, st["machine_id"], now)
    con.say("'%s' is now %s." % (target["name"], "retired" if state == "retired" else "active again"))
    return 0


def cmd_remove(con, args):
    from collector.sync import peers
    config, data_dir, cfg, st = _setup()
    _need_sync(con, st)
    now = time.time()
    rows = _rows(st, now, data_dir)
    target = resolve(con, rows, args.name)
    if target["is_self"]:
        raise installer.Refused("That is this computer. Use token-burn sync disable --remove-my-data instead.")
    if not args.yes:
        if not installer.is_interactive():
            raise installer.Refused("This needs confirmation. Run it in a terminal, or add --yes.")
        typed = con.ask("This deletes the history of '%s' from the shared folder for every computer. Type the "
                        "name to confirm:" % target["name"])
        if typed.strip().lower() != target["name"].lower():
            raise installer.Refused("The name did not match. Nothing was removed.")
    peers.delete_machine_file(st["folder"], target["id"])
    peers.write_control(st["folder"], target["id"], "removed", None, st["machine_id"], now)
    con.say("'%s' was removed from the shared folder." % target["name"])
    return 0


def _clear_kv(data_dir):
    db = Path(data_dir) / "usage.db"
    if not db.is_file():
        return
    from collector.store import Store
    store = Store(db)
    try:
        for k in KV_KEYS:
            store.db.execute("DELETE FROM kv WHERE key=?", (k,))
        store.db.commit()
    finally:
        store.close()


def cmd_reset_id(con, args):
    config, data_dir, cfg, st = _setup()
    if not st["machine_id"]:
        raise installer.Failure("Sync has not been set up on this computer.")
    if not _confirm(con, args, "Give this computer a new ID? Use this when another computer is writing with "
                               "this one's ID (a cloned computer). The old file stays in the shared folder "
                               "until you remove it."):
        raise installer.Cancelled()
    sync = dict(cfg.get("sync") or {})
    sync["machine_id"] = config.new_machine_id()
    sync["adopted"] = False
    cfg["sync"] = sync
    config.save(data_dir, cfg)
    _clear_kv(data_dir)
    con.say("This computer has a new ID (%s)." % sync["machine_id"][:8])
    return _now(con, quiet=True)


def _now(con, quiet=False):
    from collector import collect
    config, data_dir, cfg, st = _setup()
    if not st["enabled"] and not quiet:
        con.say("Sync is off; collecting local data only.")
    result = collect.run()
    if result is None:
        con.say("Another collection is already running; try again in a minute.")
    else:
        con.say("Collected.")
    return 0


def cmd_sync(args, con=None):
    con = con or installer.Console()
    handlers = {"status": cmd_status, "enable": cmd_enable, "disable": cmd_disable, "machines": cmd_machines,
                "rename": cmd_rename, "retire": lambda c, a: _set_state(c, a, "retired"),
                "unretire": lambda c, a: _set_state(c, a, "active"), "remove": cmd_remove,
                "reset-id": cmd_reset_id, "now": lambda c, a: _now(c)}
    try:
        return handlers[args.sync_cmd](con, args)
    except installer.Cancelled:
        con.say("Cancelled. Nothing was changed.")
        return 3
    except installer.Refused as e:
        con.say(str(e))
        return 3
    except installer.BadArgs as e:
        con.err("error: %s" % e)
        return 2
    except installer.Failure as e:
        con.err("error: %s" % e)
        return 1
