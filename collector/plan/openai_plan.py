"""OpenAI plan and credit lookup from Codex's own local files (no network calls, no sign-in file).

Each Codex token_count event carries a rate_limits block: the plan type, the credit balance and, on plans that
have them, the 5-hour and weekly usage windows. This reads the newest such block from the most recently
written rollout files. Only that block is parsed; message text is never read. Business or unlimited plans
report no windows and no balance, so then only the plan type and the credits note are kept.
"""
import json
import time
from pathlib import Path

from collector import util

INFO_KEY = "_info"            # stored beside the pools; has no "pct", so it is never drawn as a pool
FILES = 8                     # newest rollout files to look at
TAIL = 512 * 1024             # bytes read from the end of each file
WINDOWS = {300: "five_hour", 10080: "seven_day"}


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _tail_limits(path):
    """(timestamp, rate_limits) of the last token_count event with a rate_limits block, or None."""
    try:
        size = path.stat().st_size
        with open(str(path), "rb") as f:
            f.seek(max(0, size - TAIL))
            data = f.read()
    except OSError:
        return None
    best = None
    for raw in data.split(b"\n"):
        if b'"rate_limits"' not in raw or b'"token_count"' not in raw:
            continue
        try:
            j = json.loads(raw)
        except ValueError:
            continue
        p = j.get("payload") if isinstance(j, dict) else None
        rl = p.get("rate_limits") if isinstance(p, dict) else None
        ts = util.parse_ts(j.get("timestamp"))
        if isinstance(rl, dict) and ts is not None and (best is None or ts >= best[0]):
            best = (ts, rl)
    return best


def newest_limits(home=None):
    home = Path(home) if home else util.codex_home()
    files = []
    try:
        for p in home.rglob("rollout-*.jsonl"):
            try:
                files.append((p.stat().st_mtime, p))
            except OSError:
                pass
    except OSError:
        return None
    best = None
    for _, p in sorted(files, key=lambda x: x[0], reverse=True)[:FILES]:
        hit = _tail_limits(p)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    return best


def _pool(window, now):
    """{"pct", "resets_at"} from a rate_limits window, or None when it has no usage figure or already reset."""
    if not isinstance(window, dict):
        return None
    pct = _num(window.get("used_percent"))
    if pct is None:
        return None
    resets = util.parse_ts(window.get("resets_at"))
    if resets is None:
        secs = _num(window.get("resets_in_seconds"))
        resets = now + secs if secs is not None else None
    if resets is not None and resets < now:
        return None                   # a finished window: its figure is out of date
    out = {"pct": round(max(0.0, pct), 2)}
    if resets is not None:
        out["resets_at"] = util.iso(resets)
    return out


def parse(rate_limits, now):
    """(pools, info) from a rate_limits block. pools is {id: {pct, resets_at}}, info a small dict."""
    pools = {}
    for slot in ("primary", "secondary"):
        w = rate_limits.get(slot)
        pool = _pool(w, now)
        if pool:
            mins = _num(w.get("window_minutes"))
            pools[WINDOWS.get(int(mins)) if mins is not None and int(mins) in WINDOWS else slot] = pool
    cr = rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else {}
    info = {"plan_type": rate_limits.get("plan_type") if isinstance(rate_limits.get("plan_type"), str) else None,
            "unlimited": cr.get("unlimited") is True,
            "has_credits": cr.get("has_credits") is True,
            "balance": _num(cr.get("balance"))}
    return pools, info


def fetch(log=None):
    """Return (status, pools or None). pools holds the usage windows plus an info entry."""
    hit = newest_limits()
    if hit is None:
        return "no plan data", None
    now = time.time()
    pools, info = parse(hit[1], now)
    if not pools and not info["plan_type"] and info["balance"] is None and not info["unlimited"]:
        return "no plan data", None
    pools[INFO_KEY] = info
    return "ok", pools
