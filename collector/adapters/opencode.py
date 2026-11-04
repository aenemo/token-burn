"""opencode adapter: reads usage from opencode's local SQLite database (read-only).

Only the v2 layout (session_v2 + session_message) is supported. Model calls are the
assistant and compaction messages. Message text and the credential tables are never
read or logged: the query pulls just the time, model, cost and token fields.
"""
import sqlite3

from collector import util
from collector.effort import normalise_effort
from collector.schema import SubagentRun, UsageRecord

PLATFORM = "opencode"
DAYS = 30
OVERLAP_MS = 2 * 3600 * 1000
STALE_S = 24 * 3600
LAST_TS_KEY = "opencode:last_ts"
BUSY_STATUS = "opencode database busy, will retry"
UNSUPPORTED = "unsupported opencode data format"

_QUERY = """
SELECT m.id, m.session_id, m.type, m.time_updated,
  json_extract(m.data, '$.time.created'), json_extract(m.data, '$.time.completed'),
  json_extract(m.data, '$.model.providerID'), json_extract(m.data, '$.model.id'),
  json_extract(m.data, '$.model.variant'), json_extract(m.data, '$.cost'),
  json_extract(m.data, '$.tokens.input'), json_extract(m.data, '$.tokens.output'),
  json_extract(m.data, '$.tokens.reasoning'), json_extract(m.data, '$.tokens.cache.read'),
  json_extract(m.data, '$.tokens.cache.write'), s.parent_id, s.agent,
  json_extract(m.data, '$.status')
FROM session_message m LEFT JOIN session_v2 s ON s.id = m.session_id
WHERE m.type IN ('assistant', 'compaction') AND m.time_updated >= ? AND json_valid(m.data)
ORDER BY m.time_updated, m.id
"""


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _s(v):
    return v if isinstance(v, str) else ""


def model_label(provider, model_id):
    provider, model_id = _s(provider).strip(), _s(model_id).strip()
    if provider and model_id:
        return "%s/%s" % (provider, model_id)
    return model_id or provider or "unknown"


def effort_of(variant):
    e = normalise_effort(variant)
    return "" if e == "default" else e


def agent_name(agent_type, agent_id):
    return "%s · %s" % (agent_type or "subagent", (agent_id or "")[-6:])


def connect(path):
    """Open strictly read-only; a short busy timeout so a running opencode is tolerated."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def _has_v2(con):
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"session_v2", "session_message"} <= names


class _SourceError(Exception):
    """Wraps a sqlite error raised while opening or querying the opencode database."""


def collect(store, now, log, backfill=False):
    try:
        return _collect(store, now, log, backfill)
    except _SourceError as se:
        e = se.__cause__
        busy = any(w in str(e).lower() for w in ("locked", "busy"))
        if log:
            log.warning("opencode adapter: %s", "database busy" if busy else type(e).__name__)
        return {"status": BUSY_STATUS if busy else "opencode database unreadable", "meta": {}}
    except Exception as e:  # never raise into the scheduler
        if log:
            log.error("opencode adapter failed: %s", type(e).__name__)
        return {"status": "opencode collection failed (%s)" % type(e).__name__, "meta": {}}


def _read_rows(path, start_ms):
    """Rows from the opencode db, or None for an unsupported layout. Only these sqlite errors are opencode's."""
    try:
        con = connect(path)
        try:
            if not _has_v2(con):
                return None
            return con.execute(_QUERY, (start_ms,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise _SourceError() from e


def _collect(store, now, log, backfill):
    path = util.opencode_db()
    if not path.is_file():
        return {"status": "opencode database not found", "meta": {}}
    cutoff_ms = int((now - DAYS * 86400) * 1000)
    last_ts = store.kv_get(LAST_TS_KEY)
    full = bool(backfill) or not isinstance(last_ts, (int, float))
    start_ms = cutoff_ms if full else max(cutoff_ms, int(last_ts) - OVERLAP_MS)
    rows = _read_rows(path, start_ms)
    if rows is None:
        return {"status": UNSUPPORTED, "meta": {}}

    newest = 0 if full else int(last_ts)
    inflight_from = None
    requests = sub_requests = 0
    runs = {}
    for (mid, sess, typ, updated, created, completed, prov, mod, variant, cost,
         t_in, t_out, t_reason, t_cr, t_cw, parent, agent, status) in rows:
        updated = int(num(updated))
        in_t, out_t = int(num(t_in)), int(num(t_out)) + int(num(t_reason))
        cr_t, cw_t = int(num(t_cr)), int(num(t_cw))
        finished = bool(completed) if typ == "assistant" else _s(status).lower() == "completed"
        if not finished and now - updated / 1000.0 < STALE_S:
            # unfinished (partial tokens): leave the cursor before it so the next run reads it again.
            # Rows untouched for over 24 hours are abandoned and counted as they stand.
            inflight_from = updated if inflight_from is None else min(inflight_from, updated)
            continue
        newest = max(newest, updated)
        if in_t + out_t + cr_t + cw_t <= 0:
            continue
        ts = util.parse_ts(completed or created)
        if ts is None:
            ts = updated / 1000.0
        if ts * 1000 < cutoff_ms:
            continue
        if not store.seen_add(PLATFORM, "oc|%s" % mid, int(ts // 60)):
            continue
        is_sub = bool(parent)
        label, effort = model_label(prov, mod), effort_of(variant)
        store.add_usage(UsageRecord(
            platform=PLATFORM,
            agent_kind="subagent" if is_sub else "primary",
            agent_id=sess if is_sub else "",
            session_id=sess,
            model=label,
            effort_level=effort,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=cr_t,
            cache_write_tokens=cw_t,
            cost_usd=float(num(cost)),
            timestamp=ts,
            requests=1,
        ))
        requests += 1
        if is_sub:
            sub_requests += 1
            r = runs.setdefault(sess, {"type": _s(agent), "parent": _s(parent), "t": 0.0,
                                       "model": "", "effort": ""})
            if ts >= r["t"]:
                r.update(t=ts, model=label, effort=effort)

    for sid, r in runs.items():
        store.upsert_subagent_run(SubagentRun(
            platform=PLATFORM, agent_id=sid, agent_type=r["type"],
            agent_name=agent_name(r["type"], sid), session_id=r["parent"],
            model=r["model"], effort_level=r["effort"], t=r["t"], status="completed"))
    if inflight_from is not None:
        newest = min(newest, inflight_from - 1) if newest else 0
    if newest:
        store.kv_set(LAST_TS_KEY, newest)
    store.commit()
    return {"status": "ok", "meta": {"requests": requests, "subagent_requests": sub_requests}}
