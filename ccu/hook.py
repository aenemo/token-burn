"""Hook handlers for `ccu hook claude|cursor`. Never raise, always exit 0."""
import datetime as dt
import json
import sys
import time
from pathlib import Path

from ccu import paths

EFFORT_NAMES = {"xhigh": "extra", "extra_high": "extra", "extra-high": "extra"}
CLAUDE_LOG = "claude-subagents.jsonl"
CURSOR_LOG = "cursor-subagent-events.jsonl"
ALLOW = '{"permission":"allow"}'


def _n(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _norm_effort(e):
    if not isinstance(e, str) or not e.strip():
        return ""
    e = e.strip().lower()
    return EFFORT_NAMES.get(e, e)


def append_line(path, line, attempts=5):
    """Append one line, retrying (parallel subagents can fire hooks together)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    for attempt in range(1, attempts + 1):
        try:
            with open(path, "ab") as f:
                f.write(data)
            return True
        except OSError:
            if attempt >= attempts:
                raise
            time.sleep(0.04 * attempt)
    return False


def _read_stdin():
    stdin = sys.stdin.buffer if sys.stdin is not None else None
    return stdin.read().decode("utf-8-sig", errors="replace") if stdin is not None else ""


def claude_record(payload):
    """Build the claude-subagents.jsonl record from a SubagentStop payload, or None."""
    tp = payload.get("agent_transcript_path")
    if not tp:
        return None
    p = Path(tp)
    if not p.is_file():
        return None
    by_id, anon, model, effort = {}, [], None, ""
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
            e = _norm_effort(o.get("perTurnEffort") or o.get("effort"))
            if e:
                effort = e
            u = msg["usage"]
            tup = (_n(u.get("input_tokens")), _n(u.get("output_tokens")),
                   _n(u.get("cache_read_input_tokens")), _n(u.get("cache_creation_input_tokens")))
            mid = msg.get("id")
            if mid:
                if mid not in by_id or tup[1] > by_id[mid][1]:
                    by_id[mid] = tup
            else:
                anon.append(tup)
            if msg.get("model"):
                model = msg["model"]
    tot = [0, 0, 0, 0]
    for tup in list(by_id.values()) + anon:
        for i in range(4):
            tot[i] += tup[i]
    return {
        "t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": payload.get("session_id"),
        "agent_type": payload.get("agent_type"),
        "agent_id": payload.get("agent_id"),
        "model": model,
        "effort": effort,
        "input": tot[0], "output": tot[1], "cache_read": tot[2], "cache_write": tot[3],
    }


def _s(v):
    """PowerShell [string] cast: null becomes empty."""
    return "" if v is None else str(v)


def cursor_record(ev, now=None):
    now = now or dt.datetime.now().astimezone()
    return {
        "hook": _s(ev.get("hook_event_name")),
        "recordedAt": now.isoformat(timespec="seconds"),
        "conversationId": _s(ev.get("conversation_id")),
        "parentModel": _s(ev.get("model")),
        "subagentId": _s(ev.get("subagent_id")),
        "subagentType": _s(ev.get("subagent_type")),
        "subagentModel": _s(ev.get("subagent_model")),
        "parentConversationId": _s(ev.get("parent_conversation_id")),
        "status": _s(ev.get("status")),
        "durationMs": ev.get("duration_ms"),
        "messageCount": ev.get("message_count"),
        "toolCallCount": ev.get("tool_call_count"),
    }


def cursor_reply(name):
    return "{}" if name == "subagentStop" else ALLOW


def run_claude(raw=None):
    try:
        raw = _read_stdin() if raw is None else raw
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return 0
        rec = claude_record(payload)
        if rec:
            append_line(paths.data_dir() / CLAUDE_LOG, json.dumps(rec))
    except Exception:
        pass
    return 0


def run_cursor(raw=None, out=None):
    out = out or sys.stdout
    name = ""
    reply = ALLOW
    try:
        raw = _read_stdin() if raw is None else raw
        if raw and raw.strip():
            ev = json.loads(raw)
            if isinstance(ev, dict):
                name = _s(ev.get("hook_event_name"))
                reply = cursor_reply(name)
                append_line(paths.data_dir() / CURSOR_LOG,
                            json.dumps(cursor_record(ev), separators=(",", ":")))
    except Exception:
        reply = cursor_reply(name)
    try:
        out.write(reply + "\n")
        out.flush()
    except Exception:
        pass
    return 0
