#!/usr/bin/env python3
"""Claude Code SubagentStop hook. Appends token totals to data/claude-subagents.jsonl.

Never writes prompt text. Always exits 0 and prints nothing. Standard library only,
no package imports. CCU_DATA_DIR overrides the data folder (used by tests).
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EFFORT_NAMES = {"xhigh": "extra", "extra_high": "extra", "extra-high": "extra"}


def data_dir():
    env = os.environ.get("CCU_DATA_DIR")
    return Path(env) if env else ROOT / "data"


def n(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def norm_effort(e):
    if not isinstance(e, str) or not e.strip():
        return ""
    e = e.strip().lower()
    return EFFORT_NAMES.get(e, e)


def run():
    # Read bytes and decode as UTF-8: the console code page would mangle non-ASCII paths.
    stdin = sys.stdin.buffer if sys.stdin is not None else None
    raw = stdin.read().decode("utf-8-sig", errors="replace") if stdin is not None else ""
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        return
    tp = payload.get("agent_transcript_path")
    if not tp:
        return
    p = Path(tp)
    if not p.is_file():
        return
    by_id = {}      # message id -> usage tuple; a streamed message repeats with growing output_tokens
    anon = []       # usage of lines without a message id
    model = None
    effort = ""
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") != "assistant":
                continue
            msg = o.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            e = norm_effort(o.get("perTurnEffort") or o.get("effort"))
            if e:
                effort = e
            u = msg["usage"]
            tup = (n(u.get("input_tokens")), n(u.get("output_tokens")),
                   n(u.get("cache_read_input_tokens")), n(u.get("cache_creation_input_tokens")))
            mid = msg.get("id")
            if mid:
                if mid not in by_id or tup[1] > by_id[mid][1]:
                    by_id[mid] = tup
            else:
                anon.append(tup)
            if msg.get("model"):
                model = msg["model"]
    inp = outp = cr = cw = 0
    for tup in list(by_id.values()) + anon:
        inp += tup[0]
        outp += tup[1]
        cr += tup[2]
        cw += tup[3]
    rec = {
        "t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": payload.get("session_id"),
        "agent_type": payload.get("agent_type"),
        "agent_id": payload.get("agent_id"),
        "model": model,
        "effort": effort,
        "input": inp, "output": outp, "cache_read": cr, "cache_write": cw,
    }
    out = data_dir() / "claude-subagents.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        pass
    sys.exit(0)
