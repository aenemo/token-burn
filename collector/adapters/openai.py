"""OpenAI adapter: reads Codex (the OpenAI Codex app and CLI) usage from its local files, read-only.

Codex keeps a thread list in <codex home>/state_<n>.sqlite and one rollout JSONL file per thread. Each model
request writes a token_count event with its token usage, so those lines are the usage records. Only the
timestamp, the token counts and the model and effort of each turn are read: no message or prompt text is
parsed, stored or logged, and the sign-in file is never opened. Thread names come from the separate "name"
field only (the "title" field repeats the first prompt, so it is never used).

OpenAI models used through opencode are collected by the opencode adapter, which files them under the same
OpenAI platform. The ChatGPT web and desktop apps keep no token counts on the computer, so they cannot be
tracked.
"""
import json
import re
import sqlite3
from pathlib import Path

from collector import util
from collector.effort import normalise_effort
from collector.schema import SubagentRun, UsageRecord

PLATFORM = "openai"
DAYS = 30
OFFSETS_KEY = "openai:offsets"
BUSY_STATUS = "Codex database busy, will retry"

_STATE_RE = re.compile(r"state_(\d+)\.sqlite$")
_WANT = ("id", "rollout_path", "name", "model", "reasoning_effort", "updated_at", "attached_thread_parent_id",
         "agent_role", "agent_nickname", "title", "first_user_message", "preview")


def _s(v):
    return v if isinstance(v, str) else ""


def num(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def state_db(home=None):
    """The newest state_<n>.sqlite in the Codex folder, or None."""
    home = Path(home) if home else util.codex_home()
    best = None
    try:
        for p in home.iterdir():
            m = _STATE_RE.match(p.name)
            if m and p.is_file() and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), p)
    except OSError:
        return None
    return best[1] if best else None


def _connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def installed():
    """Codex is present once it has a state database; opencode's OpenAI requests count too."""
    if state_db() is not None:
        return True
    path = util.opencode_db()
    if not path.is_file():
        return False
    try:
        con = _connect(path)
        try:
            return con.execute("SELECT 1 FROM session_message WHERE json_extract(data, '$.model.providerID')"
                               " = 'openai' LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _threads(path, cutoff_s):
    """[{id, rollout, name, model, effort, parent, role, nickname}] for threads updated inside the window."""
    con = _connect(path)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
        cols = [c for c in _WANT if c in have]
        if "id" not in cols or "rollout_path" not in cols:
            return []
        rows = con.execute("SELECT %s FROM threads" % ", ".join(cols)).fetchall()
        edges = {}
        try:
            edges = {c: p for c, p in con.execute(
                "SELECT child_thread_id, parent_thread_id FROM thread_spawn_edges")}
        except sqlite3.Error:
            pass
    finally:
        con.close()
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        upd = d.get("updated_at")
        if isinstance(upd, (int, float)):
            upd = upd / 1000.0 if upd > 1e11 else float(upd)
            if upd < cutoff_s:
                continue
        tid = _s(d.get("id"))
        if not tid:
            continue
        name = _s(d.get("name")).strip()
        if name and name in (_s(d.get("title")), _s(d.get("first_user_message")), _s(d.get("preview"))):
            name = ""       # a copy of the prompt, not a name
        out.append({"id": tid, "rollout": _s(d.get("rollout_path")), "name": name,
                    "model": _s(d.get("model")).strip(), "effort": normalise_effort(d.get("reasoning_effort")),
                    "parent": _s(d.get("attached_thread_parent_id")) or edges.get(tid) or "",
                    "role": _s(d.get("agent_role")), "nickname": _s(d.get("agent_nickname"))})
    return out


def read_rollout(path, state):
    """Parse the new complete lines of one rollout file.

    state is [byte offset, last total token count, model, effort]. Returns (events, new state), where an
    event is (ordinal, timestamp, model, effort, input, cached, cache_write, output). Lines that are not
    token_count or turn_context events are skipped without being parsed."""
    off, last_total, model, effort = state
    try:
        size = path.stat().st_size
    except OSError:
        return [], state
    if size < off:
        off, last_total = 0, 0          # the file was rewritten: read it again from the start
    events = []
    pos = off
    with open(str(path), "rb") as f:
        f.seek(off)
        for raw in f:
            if not raw.endswith(b"\n"):
                break                    # a line still being written: pick it up next time
            pos += len(raw)
            if b'"token_count"' in raw:
                tc = True
            elif b'"turn_context"' in raw:
                tc = False
            else:
                continue
            try:
                j = json.loads(raw)
            except ValueError:
                continue
            payload = j.get("payload") if isinstance(j, dict) else None
            if not isinstance(payload, dict):
                continue
            if not tc:
                if j.get("type") == "turn_context":
                    model = _s(payload.get("model")).strip() or model
                    effort = normalise_effort(payload.get("effort")) or effort
                continue
            if j.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                total = int(total)
                if total == last_total:
                    continue             # the same total repeated: nothing new was used
                last_total = total
            last = info.get("last_token_usage")
            if not isinstance(last, dict):
                continue
            inp, cached, cw, out = (num(last.get("input_tokens")), num(last.get("cached_input_tokens")),
                                    num(last.get("cache_write_input_tokens")), num(last.get("output_tokens")))
            if inp + out <= 0:
                continue
            ts = util.parse_ts(j.get("timestamp"))
            if ts is None:
                continue
            events.append((j.get("ordinal") if isinstance(j.get("ordinal"), int) else pos, ts, model, effort,
                           inp, cached, cw, out))
    return events, [pos, last_total, model, effort]


def collect(store, now, log, backfill=False):
    try:
        return _collect(store, now, log, backfill)
    except sqlite3.Error as e:
        busy = any(w in str(e).lower() for w in ("locked", "busy"))
        if log:
            log.warning("openai adapter: %s", "database busy" if busy else type(e).__name__)
        return {"status": BUSY_STATUS if busy else "Codex database unreadable", "meta": {}}
    except Exception as e:  # never raise into the scheduler
        if log:
            log.error("openai adapter failed: %s", type(e).__name__)
        return {"status": "OpenAI collection failed (%s)" % type(e).__name__, "meta": {}}


def _collect(store, now, log, backfill):
    home = util.codex_home()
    db = state_db(home)
    if db is None:
        return {"status": "ok", "meta": {}}       # no Codex: only opencode's OpenAI requests, if any
    cutoff = now - DAYS * 86400
    offsets = store.kv_get(OFFSETS_KEY)
    if not isinstance(offsets, dict) or backfill:
        offsets = {}
    threads = _threads(db, cutoff)
    requests = sub_requests = 0
    runs = {}
    for th in threads:
        tid = th["id"]
        if th["name"]:
            store.upsert_session_title(PLATFORM, tid, th["name"], "app")
        path = Path(th["rollout"]) if th["rollout"] else None
        if path is None:
            continue
        if not path.is_absolute():
            path = home / path
        prev = offsets.get(tid)
        state = [int(prev[0]), int(prev[1]), _s(prev[2]), _s(prev[3])] if (
            isinstance(prev, list) and len(prev) == 4 and isinstance(prev[0], int)
            and isinstance(prev[1], int)) else [0, 0, th["model"], th["effort"]]
        events, state = read_rollout(path, state)
        offsets[tid] = state
        is_sub = bool(th["parent"])
        for ordinal, ts, model, effort, inp, cached, cw, out in events:
            if ts < cutoff or not store.seen_add(PLATFORM, "cx|%s|%s" % (tid, ordinal), int(ts // 60)):
                continue
            label = model or th["model"] or "unknown"
            cached = min(cached, inp)
            cw = min(cw, inp - cached)
            store.add_usage(UsageRecord(
                platform=PLATFORM, agent_kind="subagent" if is_sub else "primary",
                agent_id=tid if is_sub else "", session_id=tid, model=label, effort_level=effort,
                input_tokens=inp - cached - cw, output_tokens=out, cache_read_tokens=cached,
                cache_write_tokens=cw, cost_usd=0.0, timestamp=ts, requests=1))
            requests += 1
            if is_sub:
                sub_requests += 1
                r = runs.setdefault(tid, {"type": th["role"], "parent": th["parent"], "name": th["nickname"],
                                          "t": 0.0, "model": "", "effort": ""})
                if ts >= r["t"]:
                    r.update(t=ts, model=label, effort=effort)
    for tid, r in runs.items():
        store.upsert_subagent_run(SubagentRun(
            platform=PLATFORM, agent_id=tid, agent_type=r["type"],
            agent_name="%s · %s" % (r["name"] or r["type"] or "subagent", tid[-6:]),
            session_id=r["parent"], model=r["model"], effort_level=r["effort"], t=r["t"], status="completed"))
    live = {th["id"] for th in threads}
    store.kv_set(OFFSETS_KEY, {k: v for k, v in offsets.items() if k in live})
    store.commit()
    return {"status": "ok", "meta": {"requests": requests, "subagent_requests": sub_requests}}
