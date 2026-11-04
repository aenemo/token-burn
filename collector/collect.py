"""Collector entry point: python -m collector.collect [--backfill].

Runs both platform adapters, the throttled plan lookups, prunes old rows and
rewrites USAGE_JSON atomically. One platform failing never blocks the other.
"""
import argparse
import os
import sys
import time

from collector import util
from collector.aggregate import build_platform_payload, build_usage_json
from collector.plan import maybe_lookup
from collector.store import Store


def _platforms():
    # Imported lazily so a broken module only fails its own platform.
    from collector.adapters import claude as claude_adapter
    from collector.adapters import cursor as cursor_adapter
    from collector.plan import claude_plan, cursor_plan
    return [("claude", claude_adapter.collect, claude_plan.fetch, claude_installed),
            ("cursor", cursor_adapter.collect, cursor_plan.fetch, cursor_installed)]


def claude_installed():
    """Claude Code is present once it has made its config folder (transcripts or sign-in)."""
    d = util.claude_dir()
    return ((d / "projects").is_dir() or (d / ".credentials.json").is_file()
            or bool(os.environ.get("CCU_CLAUDE_TOKEN")))


def cursor_installed():
    """Cursor is present once it has created its state database."""
    return util.cursor_state_db().is_file()


def _is_installed(entry, log):
    if len(entry) < 4 or entry[3] is None:
        return True
    try:
        return bool(entry[3]())
    except Exception as e:
        log.error("%s install check failed: %s", entry[0], type(e).__name__)
        return False


def _not_installed_payload(name):
    return {"installed": False, "status": "not installed"}


def run(backfill=False, now=None, platforms=None, log=None):
    """Do one collection pass. Returns the usage dict, or None if another run holds the lock."""
    log = log or util.get_logger("collect")
    now = time.time() if now is None else now
    lock = util.FileLock(util.DATA_DIR / "collect.lock")
    try:
        lock.acquire()
    except util.LockBusy:
        return None
    try:
        if platforms is None:
            platforms = _platforms()
        infos = {}
        payloads = {}
        installed = {e[0]: _is_installed(e, log) for e in platforms}
        with Store() as store:
            for entry in platforms:
                name, collect_fn, fetch_fn = entry[:3]
                if not installed[name]:
                    continue
                try:
                    res = collect_fn(store, now, log, backfill=backfill) or {}
                    status = res.get("status") or "ok"
                    meta = res.get("meta") or {}
                    store.commit()
                except Exception as e:  # never let one platform block the other
                    log.error("%s collect failed: %s", name, type(e).__name__)
                    status, meta = "collect failed (%s)" % type(e).__name__, {}
                try:
                    plan = maybe_lookup(store, name, fetch_fn, now, log)
                except Exception as e:
                    log.error("%s plan lookup failed: %s", name, type(e).__name__)
                    plan = {"status": "plan check failed (%s)" % type(e).__name__,
                            "last_lookup": None, "next_lookup": None}
                infos[name] = {"status": status, "meta": meta, "plan": plan}
            store.prune(int(now // 60) - util.MINUTES)
            store.commit()
            for entry in platforms:
                name = entry[0]
                if not installed[name]:
                    payloads[name] = _not_installed_payload(name)
                    continue
                try:
                    payloads[name] = build_platform_payload(store, name, now, infos[name])
                    payloads[name]["installed"] = True
                except Exception as e:
                    log.error("%s payload failed: %s", name, type(e).__name__)
        usage = build_usage_json(payloads, now)
        util.atomic_write_json(util.USAGE_JSON, usage)
        parts = []
        for name, p in payloads.items():
            if not p.get("installed", True):
                parts.append("%s=not-installed" % name)
                continue
            reqs = sum(sum(s["requests"]) for s in p["series"])
            parts.append("%s=%s(series=%d,requests=%d,plan=%s)" % (
                name, p["status"], len(p["series"]), reqs, p["plan"]["status"]))
        ok = len(payloads) == len(platforms) and all(
            p["status"] in ("ok", "not installed") for p in payloads.values())
        log.info("%s %s%s", "OK" if ok else "PARTIAL", " ".join(parts), " backfill" if backfill else "")
        return usage
    finally:
        lock.release()


def run_loop(interval=util.TOKEN_REFRESH_S, run_fn=None, sleep=time.sleep, max_iterations=None, log=None):
    """Collect forever, sleeping `interval` seconds between passes. Never raises (except Ctrl-C)."""
    log = log or util.get_logger("collect")
    run_fn = run_fn or run
    n = 0
    while max_iterations is None or n < max_iterations:
        n += 1
        try:
            run_fn(log=log)  # run() takes the FileLock itself and returns None when busy
        except Exception as e:
            log.error("collect loop pass failed: %s", type(e).__name__)
        if max_iterations is not None and n >= max_iterations:
            break
        sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Collect Claude and Cursor token usage.")
    ap.add_argument("--backfill", action="store_true", help="re-read the full 30 days")
    ap.add_argument("--loop", action="store_true",
                    help="keep running, collecting every %d seconds" % util.TOKEN_REFRESH_S)
    args = ap.parse_args(argv)
    if args.loop:
        try:
            if args.backfill:
                run(backfill=True)
            run_loop(util.TOKEN_REFRESH_S)
        except KeyboardInterrupt:
            pass
        return 0
    try:
        usage = run(backfill=args.backfill)
    except Exception as e:
        util.get_logger("collect").error("collect crashed: %s", type(e).__name__)
        return 1
    if usage is None:
        util.get_logger("collect").info("skipped: another collection is running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
