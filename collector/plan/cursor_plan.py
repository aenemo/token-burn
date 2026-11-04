"""Cursor plan percentages (Cursor Models and Other Models pools).

Ported from fetch_plan_percents in Fetch-CursorUsage-v1.py. Tries three
endpoints in order and returns the first response that holds a percentage.
"""

import json
import os
from pathlib import Path

from collector.adapters.cursor import bearer_headers, cookie_headers, http_get, http_post, read_token
from collector.util import DATA_DIR, iso, parse_ts

DEBUG_PATH = Path(DATA_DIR) / "plan-percent-fields.json"

CYCLE_END_KEYS = (
    "billingcycleend", "cycleend", "billingperiodend", "periodend",
    "resetsat", "resetat", "nextresetat", "resettime",
)


def as_float(value):
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flatten_fields(obj, prefix=""):
    found = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = prefix + "." + key if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.update(flatten_fields(value, path))
            else:
                found[path] = value
                found[str(key)] = value
    elif isinstance(obj, list):
        for item in obj[:8]:
            found.update(flatten_fields(item, prefix))
    return found


def first_number(fields, names):
    compact_map = {}
    for key, value in fields.items():
        text = str(key).lower().replace("_", "").replace(".", "")
        compact_map[text] = value
        compact_map[str(key).split(".")[-1].lower().replace("_", "")] = value
    for name in names:
        needle = name.lower().replace("_", "").replace(".", "")
        if needle in compact_map:
            number = as_float(compact_map[needle])
            if number is not None:
                return number
    return None


def used_limit_percent(fields, used_names, limit_names):
    used = first_number(fields, used_names)
    limit = first_number(fields, limit_names)
    if used is None or limit in (None, 0):
        return None
    return round(100.0 * used / limit, 2)


def extract_plan_percents(payload):
    if not isinstance(payload, dict):
        return None, None
    fields = flatten_fields(payload)
    cursor = first_number(fields, [
        "cursorModelsPercentUsed", "cursorModelPercentUsed", "cursorPercentUsed",
        "autoPercentUsed", "includedCursorPercentUsed",
    ])
    other = first_number(fields, [
        "otherModelsPercentUsed", "otherModelPercentUsed", "namedPercentUsed",
        "apiPercentUsed", "namedModelPercentUsed",
    ])
    if cursor is None:
        cursor = used_limit_percent(
            fields, ["cursorModelsUsed", "cursorUsed", "autoUsed"],
            ["cursorModelsLimit", "cursorLimit", "autoLimit"])
    if other is None:
        other = used_limit_percent(
            fields, ["otherModelsUsed", "namedUsed", "apiUsed"],
            ["otherModelsLimit", "namedLimit", "apiLimit"])
    if cursor is None and other is None:
        return None, None
    if cursor is not None:
        cursor = round(cursor, 2)
    if other is not None:
        other = round(other, 2)
    return cursor, other


def extract_resets_at(payload):
    """Return an ISO string for the billing cycle end, or None."""
    if not isinstance(payload, dict):
        return None
    fields = flatten_fields(payload)
    for wanted in CYCLE_END_KEYS:
        for key, value in fields.items():
            leaf = str(key).split(".")[-1].lower().replace("_", "")
            if leaf != wanted or value in (None, ""):
                continue
            text = str(value).strip()
            try:
                ts = parse_ts(int(text)) if text.isdigit() else parse_ts(text)
            except (TypeError, ValueError):
                ts = None
            if ts:
                return iso(ts)
    return None


def _write_debug(keys):
    interesting = [
        key for key in keys
        if any(part in key.lower() for part in ("percent", "plan", "cursor", "other", "auto", "api", "limit", "used"))
    ]
    try:
        os.makedirs(os.path.dirname(str(DEBUG_PATH)), exist_ok=True)
        with open(str(DEBUG_PATH), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(sorted(set(interesting))[:200], handle, indent=2)
            handle.write("\n")
    except OSError:
        pass


def fetch(log=None):
    token = read_token()
    if not token:
        return "Cursor sign-in token not found", None
    cookie = cookie_headers(token)
    bearer = bearer_headers(token)
    attempts = [
        ("GET", "https://cursor.com/api/usage-summary", cookie, None),
        ("POST", "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage", bearer, {}),
        ("POST", "https://cursor.com/api/dashboard/get-current-period-usage", cookie, {}),
    ]
    debug_keys = []
    reached = False
    for method, url, headers, body_payload in attempts:
        if method == "GET":
            status, body = http_get(url, headers)
        else:
            status, body = http_post(url, headers, body_payload)
        if status != 200:
            continue
        reached = True
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        cursor, other = extract_plan_percents(payload)
        if isinstance(payload, dict):
            debug_keys.extend(sorted(flatten_fields(payload).keys()))
        if cursor is not None or other is not None:
            resets = extract_resets_at(payload)
            pools = {}
            if cursor is not None:
                pools["cursor_models"] = {"pct": cursor, "resets_at": resets}
            if other is not None:
                pools["other_models"] = {"pct": other, "resets_at": resets}
            return "ok", pools
    if debug_keys:
        _write_debug(debug_keys)
    if not reached:
        return "plan API unreachable (offline or signed out?)", None
    return "Cursor plan percentages not found in response", None
