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
OPENAI = "openai"            # opencode's provider id for OpenAI / ChatGPT; collected as the OpenAI platform
SPLIT_KEY = "opencode:openai_split"
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

# Titles of top-level sessions. A subagent's usage is shown under its parent session, so child titles are not needed.
_TITLE_QUERY = """
SELECT id, title FROM session_v2
WHERE (parent_id IS NULL OR parent_id = '') AND title IS NOT NULL AND title <> '' AND time_updated >= ?
"""


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _s(v):
    return v if isinstance(v, str) else ""


def platform_of(provider):
    """OpenAI-provider requests belong to the OpenAI platform; everything else stays with opencode."""
    return OPENAI if _s(provider).strip().lower() == OPENAI else PLATFORM


def _split_openai(store):
    """Once: OpenAI rows stored under opencode by earlier versions move to the OpenAI platform. They are
    dropped here and the next read refetches the 30 days (opencode rows already seen are skipped)."""
    if store.kv_get(SPLIT_KEY):
        return
    store.drop_model_prefix(PLATFORM, OPENAI + "/")
    store.kv_set(LAST_TS_KEY, None)
    store.kv_set(SPLIT_KEY, 1)


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


def _read_rows(path, start_ms, title_ms=0):
    """Rows from the opencode db, or None for an unsupported layout. Only these sqlite errors are opencode's."""
    try:
        con = connect(path)
        try:
            if not _has_v2(con):
                return None
            rows = con.execute(_QUERY, (start_ms,)).fetchall()
            try:
                titles = con.execute(_TITLE_QUERY, (title_ms,)).fetchall()
            except sqlite3.Error:
                try:  # a layout without time_updated: every top-level title
                    titles = con.execute(_TITLE_QUERY.split(" AND time_updated")[0]).fetchall()
                except sqlite3.Error:
                    titles = []
            return rows, titles
        finally:
            con.close()
    except sqlite3.Error as e:
        raise _SourceError() from e


def _collect(store, now, log, backfill):
    path = util.opencode_db()
    if not path.is_file():
        return {"status": "opencode database not found", "meta": {}}
    cutoff_ms = int((now - DAYS * 86400) * 1000)
    _split_openai(store)
    last_ts = store.kv_get(LAST_TS_KEY)
    full = bool(backfill) or not isinstance(last_ts, (int, float))
    start_ms = cutoff_ms if full else max(cutoff_ms, int(last_ts) - OVERLAP_MS)
    got = _read_rows(path, start_ms, cutoff_ms)
    if got is None:
        return {"status": UNSUPPORTED, "meta": {}}
    rows, titles = got
    for sid, title in titles:
        if isinstance(sid, str) and isinstance(title, str):
            store.upsert_session_title(PLATFORM, sid, title, "app")
    oa_sessions = set()

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
        plat = platform_of(prov)
        if not store.seen_add(plat, "oc|%s" % mid, int(ts // 60)):
            continue
        is_sub = bool(parent)
        label, effort = model_label(prov, mod), effort_of(variant)
        if plat == OPENAI:
            label = _s(mod).strip() or label     # "gpt-6.1-terra", the same name the Codex app uses
            oa_sessions.update(x for x in (sess, parent) if isinstance(x, str) and x)
        store.add_usage(UsageRecord(
            platform=plat,
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
            r = runs.setdefault((plat, sess), {"type": _s(agent), "parent": _s(parent), "t": 0.0,
                                               "model": "", "effort": ""})
            if ts >= r["t"]:
                r.update(t=ts, model=label, effort=effort)

    for (plat, sid), r in runs.items():
        store.upsert_subagent_run(SubagentRun(
            platform=plat, agent_id=sid, agent_type=r["type"],
            agent_name=agent_name(r["type"], sid), session_id=r["parent"],
            model=r["model"], effort_level=r["effort"], t=r["t"], status="completed"))
    # titles for the OpenAI platform: the sessions it has usage for, and their parents
    known = oa_sessions | set(store.session_ids(OPENAI))
    known.update(r["session_id"] for r in store.subagent_runs(OPENAI, 0) if r["session_id"])
    for sid, title in titles:
        if sid in known and isinstance(title, str):
            store.upsert_session_title(OPENAI, sid, title, "app")
    if inflight_from is not None:
        newest = min(newest, inflight_from - 1) if newest else 0
    if newest:
        store.kv_set(LAST_TS_KEY, newest)
    store.commit()
    return {"status": "ok", "meta": {"requests": requests, "subagent_requests": sub_requests}}
