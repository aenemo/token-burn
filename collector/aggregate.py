"""Build the per-platform payload and the full usage.json document."""
from collector.util import MINUTES, PLAN_INTERVAL_S, TOKEN_REFRESH_S, iso, parse_ts

AUTO = "Auto Cost"
THIRTY_DAYS_S = 30 * 86400

POOL_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "7-day all models",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
    "cursor_models": "Cursor Models",
    "other_models": "Other Models",
}


def pool_label(pool_id):
    return POOL_LABELS.get(pool_id) or str(pool_id).replace("_", " ").title()


def _tok(r):
    return r["input"] + r["output"] + r["cache_read"] + r["cache_write"]


def allocate_costs(store, platform, rows):
    """Return (list of (row, cost, estimated), stats, rates).

    Claude: session cost shared by tokens, estimated at the model rate when the session has no cost yet.
    Cursor and opencode: the row's own cost.
    """
    stats = {"sessions_without_cost": 0, "sessions_estimated": 0, "estimated_cost_usd": 0.0}
    if platform != "claude":
        return [(r, float(r["cost"] or 0.0), False) for r in rows], stats, {}
    totals = store.session_tokens(platform)
    costs = store.session_costs(platform)
    rate_c, rate_t = {}, {}
    for key, cost in costs.items():
        tot = totals.get(key, 0)
        if cost and tot > 0:
            rate_c[key[1]] = rate_c.get(key[1], 0.0) + cost
            rate_t[key[1]] = rate_t.get(key[1], 0) + tot
    rates = {m: rate_c[m] / rate_t[m] for m in rate_c if rate_t[m] > 0}
    out = []
    has_cost, est, allsess = set(), set(), set()
    est_usd = 0.0
    for r in rows:
        key = (r["session_id"], r["model"])
        tok = _tok(r)
        allsess.add(r["session_id"])
        if key in costs:
            tot = totals.get(key, 0)
            cost = costs[key] * tok / tot if tot > 0 else 0.0
            has_cost.add(r["session_id"])
            out.append((r, cost, False))
        elif r["model"] in rates:
            cost = tok * rates[r["model"]]
            est.add(r["session_id"])
            est_usd += cost
            out.append((r, cost, True))
        else:
            out.append((r, 0.0, False))
    stats["sessions_estimated"] = len(est)
    stats["sessions_without_cost"] = len(allsess - has_cost - est)
    stats["estimated_cost_usd"] = round(est_usd, 2)
    return out, stats, rates


def thin_samples(samples, now):
    """samples: [(t, {pool: {"pct", ...}})] ascending. Older than 2 days: one per 5 minutes."""
    out = []
    last_kept = None
    two_days = now - 2 * 86400
    n = len(samples)
    for i, (ts, pools) in enumerate(samples):
        if ts < two_days and last_kept is not None and ts - last_kept < 300 and i != n - 1:
            continue
        last_kept = ts
        row = {"t": iso(ts)}
        for k, v in pools.items():
            if isinstance(v, dict) and isinstance(v.get("pct"), (int, float)):
                row[k] = v["pct"]
        out.append(row)
    return out


def raw_plan_samples(store, platform, now):
    """[(t, pools)] ascending, the last 30 days of stored plan samples."""
    return store.plan_samples(platform, now - THIRTY_DAYS_S)


def plan_block_from_samples(samples, now, plan_info):
    """The payload plan block from raw samples [(t, pools)] ascending and a status dict."""
    plan_info = plan_info or {}
    pools = []
    if samples:
        for k, v in samples[-1][1].items():
            if isinstance(v, dict) and isinstance(v.get("pct"), (int, float)):
                pct = float(v["pct"])
                pools.append({"id": k, "label": pool_label(k), "now": pct,
                              "remaining": round(max(0.0, 100.0 - pct), 6),
                              "resets": v.get("resets_at")})
    return {"status": plan_info.get("status", ""),
            "last_lookup": plan_info.get("last_lookup"),
            "next_lookup": plan_info.get("next_lookup"),
            "pools": pools, "samples": thin_samples(samples, now),
            "reset_times": reset_times(samples)}


def _plan_block(store, platform, now, plan_info):
    return plan_block_from_samples(raw_plan_samples(store, platform, now), now, plan_info)


def reset_times(samples):
    """{pool: [iso, ...]} distinct reset times seen in the samples, ascending.

    Each sample records the pool's next reset, so the distinct values over the
    stored history are the past resets plus the upcoming one. Rounded to the
    minute because the API's sub-second part drifts between lookups.
    """
    out = {}
    for _, pools in samples:
        for k, v in (pools or {}).items():
            ts = parse_ts(v.get("resets_at")) if isinstance(v, dict) else None
            if ts is not None:
                out.setdefault(k, set()).add(int(round(ts / 60.0)) * 60)
    return {k: [iso(t) for t in sorted(v)] for k, v in out.items()}


def build_sessions(store, platform, start_min, allocated):
    """[{id, title, series: [{key, model, effort, t, input, output, cache, cost}]}], most tokens first.

    The same rows and allocated costs as the model series, grouped by session. A subagent whose usage
    is stored under its own session id (opencode child sessions) is folded into its parent session.
    """
    parent_of = {}
    for r in store.subagent_runs(platform, start_min * 60):
        if r["session_id"] and r["session_id"] != r["agent_id"]:
            parent_of[r["agent_id"]] = r["session_id"]
    titles = store.session_titles(platform)
    by_sess = {}
    for r, cost, _ in allocated:
        sid = r["session_id"] or ""
        if r["agent_kind"] == "subagent" and sid in parent_of:
            sid = parent_of[sid]
        model, eff = r["model"], r["effort"] or ""
        key = model if not eff else "%s-%s" % (model, eff)
        ses = by_sess.setdefault(sid, {})
        s = ses.setdefault(key, {"model": model, "effort": eff, "m": {}})
        cell = s["m"].setdefault(r["minute"] - start_min, [0, 0, 0, 0.0])
        cell[0] += r["input"]
        cell[1] += r["output"]
        cell[2] += r["cache_read"] + r["cache_write"]
        cell[3] += cost

    def tok(ses):
        return sum(c[0] + c[1] + c[2] for s in ses.values() for c in s["m"].values())

    out = []
    for sid in sorted(by_sess, key=lambda k: (-tok(by_sess[k]), k)):
        ses = by_sess[sid]
        series = []
        for key in sorted(ses):
            s = ses[key]
            idxs = sorted(s["m"])
            cells = [s["m"][i] for i in idxs]
            series.append({"key": key, "model": s["model"], "effort": s["effort"], "t": idxs,
                           "input": [c[0] for c in cells], "output": [c[1] for c in cells],
                           "cache": [c[2] for c in cells], "cost": [round(c[3], 6) for c in cells]})
        out.append({"id": sid, "title": titles.get(sid) or None, "series": series})
    return out


def build_platform_payload(store, platform, now, status_info):
    status_info = status_info or {}
    now_min = int(now // 60)
    start_min = now_min - MINUTES + 1
    rows = store.usage_rows(platform, start_min)
    rows = [r for r in rows if r["minute"] <= now_min]
    allocated, stats, rates = allocate_costs(store, platform, rows)

    series = {}
    by_agent = {}
    reqs = auto_reqs = 0
    for r, cost, estimated in allocated:
        idx = r["minute"] - start_min
        model, eff = r["model"], r["effort"] or ""
        key = model if not eff else "%s-%s" % (model, eff)
        s = series.setdefault(key, {"model": model, "effort": eff, "m": {}})
        cell = s["m"].setdefault(idx, [0, 0, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0])
        cache = r["cache_read"] + r["cache_write"]
        cell[0] += r["input"]
        cell[1] += r["output"]
        cell[2] += cache
        cell[3] += cost
        cell[4] += r["requests"]
        cell[9] += r["cache_read"]
        if r["agent_kind"] == "subagent":
            cell[5] += r["input"]
            cell[6] += r["output"]
            cell[7] += cache
            cell[8] += cost
            cell[10] += r["cache_read"]
        reqs += r["requests"]
        if model == AUTO:
            auto_reqs += r["requests"]
        if r["agent_id"]:
            a = by_agent.setdefault(r["agent_id"], [0, 0, 0, 0.0, False])
            a[0] += r["input"]
            a[1] += r["output"]
            a[2] += cache
            a[3] += cost
            a[4] = a[4] or estimated

    def total(s):
        return sum(c[0] + c[1] + c[2] for c in s["m"].values())

    sessions = build_sessions(store, platform, start_min, allocated)

    out_series = []
    for key in sorted(series, key=lambda k: (-total(series[k]), k)):
        s = series[key]
        idxs = sorted(s["m"])
        cells = [s["m"][i] for i in idxs]
        out_series.append({
            "key": key, "model": s["model"], "effort": s["effort"], "t": idxs,
            "input": [c[0] for c in cells], "output": [c[1] for c in cells],
            "cache": [c[2] for c in cells], "cost": [round(c[3], 6) for c in cells],
            "requests": [c[4] for c in cells],
            "sub_input": [c[5] for c in cells], "sub_output": [c[6] for c in cells],
            "sub_cache": [c[7] for c in cells], "sub_cost": [round(c[8], 6) for c in cells],
            "cache_read": [c[9] for c in cells], "sub_cache_read": [c[10] for c in cells]})

    runs = []
    attributed = False
    for r in sorted(store.subagent_runs(platform, start_min * 60), key=lambda r: -r["t"]):
        a = by_agent.get(r["agent_id"])
        run = {"agent_id": r["agent_id"], "agent_name": r["agent_name"],
               "agent_type": r["agent_type"], "session_id": r["session_id"],
               "model": r["model"], "effort": r["effort"], "t": iso(r["t"]),
               "status": r["status"]}
        if a is not None:
            attributed = True
            run.update(input=a[0], output=a[1], cache=a[2], cost=round(a[3], 6),
                       cost_estimated=bool(a[4]))
        elif platform == "cursor":
            run.update(input=None, output=None, cache=None, cost=None, cost_estimated=False)
        else:
            tok = r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
            rate = rates.get(r["model"])
            run.update(input=r["input"], output=r["output"],
                       cache=r["cache_read"] + r["cache_write"],
                       cost=round(tok * rate, 6) if rate else 0.0,
                       cost_estimated=bool(rate and tok))
        runs.append(run)

    meta = dict(status_info.get("meta") or {})
    if platform == "claude":
        meta.update(auto_label_requests=auto_reqs, requests=reqs, **stats)
        meta.setdefault("files", 0)
    else:
        meta["subagent_tokens_attributed"] = attributed
        if platform == "opencode":
            # some opencode models record cost 0 while using tokens, so the frontend should not present $0 as fact
            meta["cost_reported"] = not any(_tok(r) > 0 and not cost for r, cost, _ in allocated)

    return {"status": status_info.get("status", "ok"),
            "start": iso(start_min * 60), "minutes": MINUTES,
            "series": out_series, "sessions": sessions, "subagent_runs": runs,
            "plan": _plan_block(store, platform, now, status_info.get("plan")),
            "meta": meta}


def build_usage_json(platform_payloads, now, machines=None, sync=None):
    """The usage.json document. machines and sync are added only when given, so with sync off the
    output is exactly what it was before multi-computer sync existed."""
    out = {"generated_at": iso(now), "token_refresh_s": TOKEN_REFRESH_S,
           "plan_refresh_s": PLAN_INTERVAL_S, "platforms": dict(platform_payloads)}
    if machines is not None:
        out["machines"] = machines
    if sync is not None:
        out["sync"] = sync
    return out
