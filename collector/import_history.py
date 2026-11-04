"""One-off import of plan usage history from the two older single-platform apps.

    python -m collector.import_history --claude-src PATH --cursor-src PATH  (either or both)

Reads the old apps' files read-only (they are never opened for writing) and inserts the
samples into plan_samples. Idempotent: a sample is skipped when the same platform and time
already exist. Anything older than 30 days is ignored.

Sources:
  Claude  data\\plan_samples.jsonl          {"t": iso, "pools": {pool: {"pct", "resets_at"}}}
  Cursor  data\\plan-percent-history.jsonl  {"recordedAt" (naive local time), "cursorModelsPercent",
                                            "otherModelsPercent"}
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from collector import util
from collector.store import Store

WINDOW_S = 30 * 86400


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _lines(path):
    """Yield parsed JSON objects from a JSONL file, skipping blank and bad lines."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict):
                yield o


def claude_samples(path):
    """Yield (epoch, pools) from the Claude app's plan_samples.jsonl."""
    for o in _lines(path):
        t = util.parse_ts(o.get("t"))
        raw = o.get("pools")
        if t is None or not isinstance(raw, dict):
            continue
        pools = {}
        for k, v in raw.items():
            pct = _num(v.get("pct")) if isinstance(v, dict) else None
            if pct is None:
                continue
            rs = util.parse_ts(v.get("resets_at")) if v.get("resets_at") else None
            pools[str(k)] = {"pct": pct, "resets_at": util.iso(rs) if rs is not None else None}
        if pools:
            yield t, pools


def _local_epoch(text):
    """Epoch seconds for an ISO time; a naive time is read as local time (DST aware)."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    try:
        d = dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
    except ValueError:
        return None
    return d.astimezone().timestamp() if d.tzinfo is None else d.timestamp()


def cursor_samples(path):
    """Yield (epoch, pools) from the Cursor app's plan-percent-history.jsonl."""
    for o in _lines(path):
        t = _local_epoch(o.get("recordedAt"))
        if t is None:
            continue
        pools = {}
        for key, pool in (("cursorModelsPercent", "cursor_models"), ("otherModelsPercent", "other_models")):
            pct = _num(o.get(key))
            if pct is not None:
                pools[pool] = {"pct": pct, "resets_at": None}
        if pools:
            yield t, pools


def import_file(store, platform, path, reader, now):
    """Insert one source file. Returns a dict of counts, or None if the file is missing."""
    path = Path(path)
    if not path.is_file():
        return None
    cutoff = now - WINDOW_S
    c = {"read": 0, "added": 0, "duplicate": 0, "old": 0}
    for t, pools in reader(path):
        c["read"] += 1
        if t < cutoff:
            c["old"] += 1
        elif store.add_plan_sample_if_absent(platform, t, pools):
            c["added"] += 1
        else:
            c["duplicate"] += 1
    return c


def run(claude_src=None, cursor_src=None, now=None, store=None, out=print):
    """Import both sources. Returns {"claude": counts-or-None, "cursor": ...}."""
    now = time.time() if now is None else now
    jobs = tuple(j for j in (("claude", claude_src, claude_samples),
                             ("cursor", cursor_src, cursor_samples)) if j[1])
    own = store is None
    if own:
        store = Store()
    results = {}
    try:
        for platform, path, reader in jobs:
            c = import_file(store, platform, path, reader, now)
            results[platform] = c
            if c is None:
                out("%s: source not found, skipped (%s)" % (platform, path))
            else:
                out("%s: read %d, added %d, already present %d, older than 30 days %d (%s)" % (
                    platform, c["read"], c["added"], c["duplicate"], c["old"], path))
        store.commit()
    finally:
        if own:
            store.close()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import plan usage history from the older Claude and Cursor apps.")
    ap.add_argument("--claude-src", default=None, help="path to plan_samples.jsonl of the Claude app")
    ap.add_argument("--cursor-src", default=None, help="path to plan-percent-history.jsonl of the Cursor app")
    args = ap.parse_args(argv)
    if not args.claude_src and not args.cursor_src:
        ap.error("give at least one of --claude-src or --cursor-src")
    try:
        run(args.claude_src, args.cursor_src)
    except Exception as e:
        print("import failed: %s" % type(e).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
