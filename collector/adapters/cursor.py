"""Cursor adapter: pulls usage events from the Cursor dashboard API.

Ported from Token Usage Graph\\Fetch-CursorUsage-v1.py. Token parts are kept
separate (input, output, cache read, cache write). The sign-in token is read
from Cursor's own state.vscdb (read-only) and is never logged or written.
"""

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from collector import safe_storage
from collector.effort import split_cursor_model
from collector.schema import SubagentRun, UsageRecord
from collector.util import CURSOR_HOOK_LOG, cursor_state_db

PLATFORM = "cursor"
PAGE_SIZE = 100
MAX_PAGES = 100
OVERLAP_MS = 2 * 3600 * 1000
WINDOW_S = 30 * 86400
LAST_TS_KEY = "cursor:last_ts"
META_KEY = "cursor:meta"
AUTO_COST = "Auto Cost"

EVENT_URLS = (
    "https://cursor.com/api/dashboard/get-filtered-usage-events",
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetFilteredUsageEvents",
)

GENERIC_MODEL_NAMES = {"", "default", "auto", "unknown", "unknown model", "n/a", "none"}


# ---------------------------------------------------------------- sign-in

TOKEN_NOT_FOUND = "Cursor sign-in token not found"
TOKEN_ENCRYPTED = "Cursor sign-in token is encrypted and could not be decrypted on this PC"
_token_problem = TOKEN_NOT_FOUND


def token_problem():
    """Status text for the last read_token() that returned None."""
    return _token_problem


def read_token(db_path=None):
    """Return the access token string, or None. Opens the database read-only.

    Newer Cursor builds store the token encrypted (safe storage); it is decrypted
    in memory only, and token_problem() says why a read failed.
    """
    global _token_problem
    _token_problem = TOKEN_NOT_FOUND
    path = Path(db_path) if db_path else Path(cursor_state_db())
    if not path.exists():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        for table in ("ItemTable", "cursorDiskKV"):
            for key in ("cursorAuth/accessToken", "cursorAuth/access_token"):
                try:
                    row = con.execute("SELECT value FROM " + table + " WHERE key = ?", (key,)).fetchone()
                except sqlite3.Error:
                    continue
                if row and row[0]:
                    value = row[0]
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    value = str(value).strip().strip('"')
                    if safe_storage.is_encrypted(value):
                        value = safe_storage.decrypt_value(value, path)
                        if not value:
                            _token_problem = TOKEN_ENCRYPTED
                            continue
                    if value:
                        return value
        return None
    finally:
        con.close()


def cookie_headers(token):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://cursor.com",
        "Cookie": "WorkosCursorSessionToken=" + token,
    }


def bearer_headers(token):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "Connect-Protocol-Version": "1",
    }


# ---------------------------------------------------------------- http

def _request(request):
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, body[:500]
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def http_get(url, headers):
    return _request(urllib.request.Request(url, headers=headers, method="GET"))


def http_post(url, headers, payload):
    data = json.dumps(payload).encode("utf-8")
    return _request(urllib.request.Request(url, data=data, headers=headers, method="POST"))


# ---------------------------------------------------------------- events

def event_list(payload):
    if not isinstance(payload, dict):
        return [], None
    events = payload.get("usageEventsDisplay") or payload.get("usageEvents") or payload.get("events") or []
    total = payload.get("totalUsageEventsCount")
    if total is None:
        total = payload.get("totalCount")
    return events, total


def as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def token_parts(event):
    """Return (input, output, cache_read, cache_write, dollars)."""
    usage = event.get("tokenUsage") or {}
    if not isinstance(usage, dict):
        usage = {}
    cents = event.get("chargedCents")
    if cents is None:
        cents = usage.get("totalCents")
    try:
        dollars = float(cents or 0) / 100.0
    except (TypeError, ValueError):
        dollars = 0.0
    return (
        as_int(usage.get("inputTokens")),
        as_int(usage.get("outputTokens")),
        as_int(usage.get("cacheReadTokens")),
        as_int(usage.get("cacheWriteTokens")),
        dollars,
    )


def is_generic_model_name(name):
    return str(name or "").strip().lower() in GENERIC_MODEL_NAMES


def candidate_model_names(event):
    names = []
    if not isinstance(event, dict):
        return names
    preferred = (
        "resolvedModel", "underlyingModel", "actualModel", "routedModel", "selectedModel",
        "customModel", "modelIntent", "modelName", "model_id", "modelId", "providerModel", "model",
    )
    for key in preferred:
        value = event.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            names.append(text)
    usage = event.get("tokenUsage")
    if isinstance(usage, dict):
        for key in ("model", "modelIntent", "resolvedModel", "underlyingModel"):
            value = usage.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                names.append(text)
    for key, value in event.items():
        if "model" not in str(key).lower():
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                names.append(text)
    return names


def event_model_name(event):
    for name in candidate_model_names(event):
        if not is_generic_model_name(name):
            return name
    return None


def dedupe_key(event, ms, in_t, out_t, cache_t):
    return "%s|%s|%s|%s|%s|%s|%s" % (
        ms, event.get("conversationId") or "", event_model_name(event) or event.get("model") or "",
        in_t, out_t, cache_t, event.get("chargedCents"),
    )


# ---------------------------------------------------------------- hook log

def _hook_time(text):
    """Hook timestamps are ISO with an offset, or legacy naive local time."""
    if not text:
        return None
    try:
        s = str(text).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except ValueError:
        return None


def read_hook_log(path=None):
    """Return a list of hook records, skipping self-tests and bad lines."""
    path = Path(path or CURSOR_HOOK_LOG)
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("subagentId") == "hook-self-test" or item.get("subagentType") == "hook-self-test":
                    continue
                out.append(item)
    except OSError:
        pass
    return out


def subagent_conversation_ids(records):
    """Ids that Cursor events can carry as conversationId for subagent work.

    A hook record whose parentConversationId is set describes a subagent's own
    conversation (conversationId). The subagentId is also treated as a
    conversation id, since Cursor may use it that way.
    """
    ids = set()
    for item in records:
        sub_id = str(item.get("subagentId") or "")
        conv = str(item.get("conversationId") or "")
        parent = str(item.get("parentConversationId") or "")
        if parent and conv and conv != parent:
            ids.add(conv)
        if sub_id:
            ids.add(sub_id)
    return ids


def subagent_runs(records, now):
    runs = {}
    for item in records:
        sub_id = str(item.get("subagentId") or "")
        if not sub_id:
            continue
        run = runs.setdefault(sub_id, {"type": "", "model": "", "session": "", "status": "", "t": 0.0})
        t = _hook_time(item.get("recordedAt")) or now
        hook = item.get("hook")
        if item.get("subagentType"):
            run["type"] = str(item["subagentType"])
        if item.get("subagentModel"):
            run["model"] = str(item["subagentModel"])
        session = str(item.get("parentConversationId") or item.get("conversationId") or "")
        if session:
            run["session"] = session
        if hook == "subagentStop":
            run["status"] = str(item.get("status") or "completed")
        elif hook == "subagentStart" and not run["status"]:
            run["status"] = "started"
        run["t"] = max(run["t"], t)
    result = []
    for sub_id, run in runs.items():
        model, effort = split_cursor_model(run["model"]) if run["model"] else ("", "")
        result.append(SubagentRun(
            platform=PLATFORM,
            agent_id=sub_id,
            agent_type=run["type"],
            agent_name="%s · %s" % (run["type"] or "subagent", sub_id[:7]),
            session_id=run["session"],
            model=model,
            effort_level=effort,
            t=run["t"],
            status=run["status"],
        ))
    return result


# ---------------------------------------------------------------- fetch

def _now_seconds(now):
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def fetch_events(token, start_ms, end_ms):
    """Return (events, truncated, url, status). status is "ok" or a message."""
    attempts = ((EVENT_URLS[0], cookie_headers(token)), (EVENT_URLS[1], bearer_headers(token)))
    chosen = None
    last_status = None
    for url, headers in attempts:
        status, body = http_post(url, headers, {
            "startDate": str(start_ms), "endDate": str(end_ms), "page": 1, "pageSize": PAGE_SIZE})
        last_status = status
        if status != 200:
            continue
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            chosen = (url, headers, payload)
            break
    if chosen is None:
        return [], False, None, "Cursor usage unavailable (status %s), sign in to Cursor" % last_status
    url, headers, first = chosen
    events, total = event_list(first)
    events = list(events)
    page = 1
    truncated = False
    while True:
        if total is not None and len(events) >= int(total):
            break
        if page == 1 and len(events) < PAGE_SIZE:
            break
        if page >= MAX_PAGES:
            truncated = True
            break
        page += 1
        status, body = http_post(url, headers, {
            "startDate": str(start_ms), "endDate": str(end_ms), "page": page, "pageSize": PAGE_SIZE})
        if status != 200:
            return [], False, url, "Cursor stopped returning usage on page %d (status %s)" % (page, status)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return [], False, url, "Cursor returned data that was not JSON"
        batch, total_page = event_list(payload)
        if total_page is not None:
            total = total_page
        if not batch:
            break
        events.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return events, truncated, url, "ok"


def collect(store, now, log, backfill=False):
    try:
        return _collect(store, now, log, backfill)
    except Exception as exc:  # never raise into the scheduler
        if log:
            log.error("cursor adapter failed: %s", type(exc).__name__)
        return {"status": "Cursor collection failed (%s)" % type(exc).__name__, "meta": {}}


def _collect(store, now, log, backfill):
    now_s = _now_seconds(now)
    token = read_token()
    if not token:
        return {"status": token_problem(), "meta": {}}

    last_ts = store.kv_get(LAST_TS_KEY)
    full = bool(backfill) or not last_ts
    if full:
        start_ms = int((now_s - WINDOW_S) * 1000)
    else:
        start_ms = int(float(last_ts) - OVERLAP_MS)
    end_ms = int(now_s * 1000)

    events, truncated, _url, status = fetch_events(token, start_ms, end_ms)
    if status != "ok":
        return {"status": status, "meta": {}}

    hook_records = read_hook_log()
    sub_conv = subagent_conversation_ids(hook_records)

    meta = store.kv_get(META_KEY) or {}
    counts = {"event_count": 0, "headless_requests": 0, "default_fallback_requests": 0}
    if not full:
        for k in counts:
            counts[k] = as_int(meta.get(k))
    tokens_available = bool(meta.get("tokens_available")) and not full
    newest = float(last_ts) if (last_ts and not full) else 0.0

    for event in events:
        if not isinstance(event, dict):
            continue
        ms = as_int(event.get("timestamp"))
        if ms <= 0:
            continue
        newest = max(newest, float(ms))
        in_t, out_t, cr_t, cw_t, dollars = token_parts(event)
        minute = int((ms / 1000.0) // 60)
        key = dedupe_key(event, ms, in_t, out_t, cr_t + cw_t)
        is_new = store.seen_add(PLATFORM, key, minute)
        name = event_model_name(event)
        fallback = name is None
        if fallback:
            name = AUTO_COST
        # Backfill recounts everything fetched; incremental runs count new events only.
        if full or is_new:
            counts["event_count"] += 1
            if event.get("isHeadless"):
                counts["headless_requests"] += 1
            if fallback:
                counts["default_fallback_requests"] += 1
        if in_t + out_t + cr_t + cw_t > 0:
            tokens_available = True
        if not is_new:
            continue
        model, effort = split_cursor_model(name)
        conv = str(event.get("conversationId") or "")
        is_sub = bool(conv) and conv in sub_conv
        store.add_usage(UsageRecord(
            platform=PLATFORM,
            agent_kind="subagent" if is_sub else "primary",
            agent_id=conv if is_sub else "",
            session_id=conv,
            model=model,
            effort_level=effort,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=cr_t,
            cache_write_tokens=cw_t,
            cost_usd=dollars,
            timestamp=ms / 1000.0,
            requests=1,
        ))

    for run in subagent_runs(hook_records, now_s):
        store.upsert_subagent_run(run)

    if newest:
        store.kv_set(LAST_TS_KEY, newest)
    result_meta = dict(counts)
    result_meta["truncated"] = bool(truncated)
    result_meta["tokens_available"] = bool(tokens_available)
    store.kv_set(META_KEY, result_meta)
    store.commit()
    return {"status": "ok", "meta": result_meta}
