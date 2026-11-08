"""Merge this computer's payloads with the other computers' machine files (pure functions).

All minutes are absolute (epoch minutes) while merging; the result is rebased to the local
payload's `start`. Claude and opencode ADD across computers (each computer only sees its own
sessions). Cursor takes the LARGER value per cell because Cursor usage is account-wide, so every
computer reports the same usage and adding would double count it.

Machine ids that share a host_fp (the same physical computer after a lost id, a reset-id or a
reinstall) read the same local logs, so within such a group cells combine by MAX, never ADD. The
newest written member of a group represents it; older members are "superseded by" it.
"""
import copy

from collector.aggregate import plan_block_from_samples
from collector.sync import validate
from collector.util import MINUTES, iso, parse_ts

FIELDS = validate.SERIES_FIELDS      # same order as the cells in collector/aggregate.py
COST_IDX = (3, 8)
ADD_PLATFORMS = ("claude", "opencode")
FROM_OTHERS = "ok (from other computers)"
STALE_S = 3600
THIRTY_DAYS_S = 30 * 86400
TOKEN_PLATFORMS = ("claude", "opencode")     # what tokens_30d counts (Cursor is account-wide)


def _new_cell():
    return [0, 0, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0]


def _is_removed(peer):
    c = peer.get("control")
    return bool(c) and c.get("state") == "removed"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


# ---- cells

def local_cells(payload, start_min):
    """{key: {"model", "effort", "cells": {abs_minute: cell}}} from a built payload (relative t)."""
    out = {}
    for s in (payload or {}).get("series") or []:
        if not isinstance(s, dict) or not isinstance(s.get("t"), list):
            continue
        model, effort = s.get("model") or "", s.get("effort") or ""
        key = s.get("key") or ("%s-%s" % (model, effort) if effort else model)
        ent = out.setdefault(key, {"model": model, "effort": effort, "cells": {}})
        cols = [s.get(f) or [] for f in FIELDS]
        for i, t in enumerate(s["t"]):
            cell = ent["cells"].setdefault(start_min + int(t), _new_cell())
            for j, col in enumerate(cols):
                if i < len(col):
                    cell[j] += _num(col[i])
    return out


def doc_cells(plat_doc):
    """The same structure from a validated machine-file platform block (absolute `m`)."""
    out = {}
    for s in (plat_doc or {}).get("series") or []:
        ent = out.setdefault(s["key"], {"model": s["model"], "effort": s["effort"], "cells": {}})
        for i, m in enumerate(s["m"]):
            cell = ent["cells"].setdefault(m, _new_cell())
            for j, f in enumerate(FIELDS):
                cell[j] += s[f][i]
    return out


def _has_cells(cells_map):
    return any(ent["cells"] for ent in cells_map.values())


def _total(cell):
    return cell[0] + cell[1] + cell[2]


def _pick_larger(cands):
    """Cursor: the cell with the largest token total (ties: cost, then requests), with each sub_x
    limited to the chosen total and to the largest sub_x seen on any computer."""
    best = max(cands, key=lambda c: (_total(c), c[3], c[4]))
    out = list(best)
    max_sub = [max(c[j] for c in cands) for j in range(len(best))]
    for sub, tot in ((5, 0), (6, 1), (7, 2), (8, 3)):
        out[sub] = min(best[tot], max_sub[sub])
    out[10] = min(best[9], max_sub[10], out[7])
    return out


def combine(cells_maps, how):
    """One cells map from several. how is "add", "larger" or "max" (the same data read twice)."""
    merged = {}
    for cm in cells_maps:
        for key, ent in cm.items():
            m = merged.setdefault(key, {"model": ent["model"], "effort": ent["effort"], "cands": {}})
            for minute, cell in ent["cells"].items():
                m["cands"].setdefault(minute, []).append(cell)
    out = {}
    for key, m in merged.items():
        cells = {}
        for minute, cands in m["cands"].items():
            if how == "larger":
                cells[minute] = _pick_larger(cands)
            elif how == "max":
                cells[minute] = [max(c[j] for c in cands) for j in range(len(cands[0]))]
            else:
                acc = _new_cell()
                for c in cands:
                    for j, v in enumerate(c):
                        acc[j] += v
                cells[minute] = acc
        out[key] = {"model": m["model"], "effort": m["effort"], "cells": cells}
    return out


def series_out(cells_map, start_min):
    """Payload-shaped series list: t relative to start_min, 0 <= t < MINUTES, biggest first."""
    items = []
    for key, ent in cells_map.items():
        ts = sorted(m - start_min for m in ent["cells"] if 0 <= m - start_min < MINUTES)
        if not ts:
            continue
        cells = [ent["cells"][start_min + t] for t in ts]
        s = {"key": key, "model": ent["model"], "effort": ent["effort"], "t": ts}
        for j, f in enumerate(FIELDS):
            vals = [c[j] for c in cells]
            s[f] = [round(float(v), 6) for v in vals] if j in COST_IDX else [int(v) for v in vals]
        items.append((-sum(_total(c) for c in cells), key, s))
    items.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in items]


# ---- sessions

def local_sessions(payload, start_min, salt):
    """{match id: {"id", "title", "series": cells map of [input, output, cache, cost]}} from a local
    payload. The match id is the hashed id peers export, so the same session read under another
    machine id of this computer (or a Cursor conversation seen elsewhere) lines up."""
    out = {}
    for ses in (payload or {}).get("sessions") or []:
        if not isinstance(ses, dict) or not isinstance(ses.get("series"), list):
            continue
        raw = str(ses.get("id") or "")
        ent = out.setdefault(validate.session_hash(salt, raw),
                             {"id": raw, "title": ses.get("title") or None, "series": {},
                              "title_local": bool(ses.get("title_local"))})
        for s in ses["series"]:
            if not isinstance(s, dict) or not isinstance(s.get("t"), list):
                continue
            model, effort = s.get("model") or "", s.get("effort") or ""
            key = s.get("key") or ("%s-%s" % (model, effort) if effort else model)
            se = ent["series"].setdefault(key, {"model": model, "effort": effort, "cells": {}})
            cols = [s.get(f) or [] for f in validate.SESSION_FIELDS]
            for i, t in enumerate(s["t"]):
                cell = se["cells"].setdefault(start_min + int(t), [0, 0, 0, 0.0])
                for j, col in enumerate(cols):
                    if i < len(col):
                        cell[j] += _num(col[i])
    return out


def doc_sessions(plat_doc):
    """The same structure from a validated machine-file platform block (absolute `m`)."""
    out = {}
    for ses in (plat_doc or {}).get("sessions") or []:
        ent = out.setdefault(ses["id"], {"id": ses["id"], "title": ses["title"], "series": {}})
        for s in ses["series"]:
            se = ent["series"].setdefault(s["key"], {"model": s["model"], "effort": s["effort"],
                                                     "cells": {}})
            for i, m in enumerate(s["m"]):
                se["cells"][m] = [s[f][i] for f in validate.SESSION_FIELDS]
    return out


def combine_sessions(maps):
    """Union of session maps. One id is one session (ids are unique per physical computer, and a
    Cursor conversation is the same on every computer), so repeated cells take the per-field max.
    The first map's id and title win; a later title fills a missing one, and a real title replaces a
    label built from this computer's folder names."""
    out = {}
    for sm in maps:
        for mid, ent in sm.items():
            cur = out.get(mid)
            if cur is None:
                out[mid] = {"id": ent["id"], "title": ent["title"],
                            "title_local": bool(ent.get("title_local")),
                            "series": {k: {"model": v["model"], "effort": v["effort"],
                                           "cells": {m: list(c) for m, c in v["cells"].items()}}
                                       for k, v in ent["series"].items()}}
                continue
            if ent["title"] and (not cur["title"] or (cur.get("title_local") and not ent.get("title_local"))):
                cur["title"], cur["title_local"] = ent["title"], bool(ent.get("title_local"))
            for key, se in ent["series"].items():
                cs = cur["series"].setdefault(key, {"model": se["model"], "effort": se["effort"],
                                                    "cells": {}})
                for m, c in se["cells"].items():
                    old = cs["cells"].get(m)
                    cs["cells"][m] = list(c) if old is None else [max(a, b) for a, b in zip(old, c)]
    return out


def sessions_out(sessions, start_min):
    """Payload-shaped sessions list: t relative to start_min, 0 <= t < MINUTES, most tokens first."""
    items = []
    for ent in sessions.values():
        series, tok = [], 0
        for key in sorted(ent["series"]):
            se = ent["series"][key]
            ts = sorted(m - start_min for m in se["cells"] if 0 <= m - start_min < MINUTES)
            if not ts:
                continue
            cells = [se["cells"][start_min + t] for t in ts]
            tok += sum(c[0] + c[1] + c[2] for c in cells)
            s = {"key": key, "model": se["model"], "effort": se["effort"], "t": ts}
            for j, f in enumerate(validate.SESSION_FIELDS):
                vals = [c[j] for c in cells]
                s[f] = [round(float(v), 6) for v in vals] if f == "cost" else [int(v) for v in vals]
            series.append(s)
        if series:
            items.append((-tok, ent["id"], {"id": ent["id"], "title": ent["title"], "series": series}))
    items.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in items]


# ---- runs

def _peer_run(run):
    r = dict(run)
    r.setdefault("session_id", "")
    return r


def _run_t(run):
    ts = parse_ts(run.get("t"))
    return ts if ts is not None else 0.0


def merge_runs(run_lists, lo_ts, larger=False):
    """Concatenate, de-duplicate on agent_id, newest first, only those at or after lo_ts.

    Claude and opencode keep the first copy of an id. Cursor prefers the record that has tokens,
    else the newest.
    """
    by_id = {}
    for runs in run_lists:
        for r in runs:
            if not isinstance(r, dict) or _run_t(r) < lo_ts:
                continue
            aid = r.get("agent_id")
            cur = by_id.get(aid)
            if cur is None:
                by_id[aid] = r
            elif larger:
                cur_has, new_has = cur.get("input") is not None, r.get("input") is not None
                if (new_has and not cur_has) or (new_has == cur_has and _run_t(r) > _run_t(cur)):
                    by_id[aid] = r
    return sorted(by_id.values(), key=lambda r: -_run_t(r))


# ---- groups (same physical computer)

SELF_GROUP = "self"
ADOPTED_ID = "__adopted__"


def _written(peer):
    doc = peer.get("doc") or {}
    ts = parse_ts(doc.get("written_at")) if isinstance(doc.get("written_at"), str) else None
    return ts if ts is not None else 0.0


def peer_groups(peers, self_id, self_fp):
    """{group key: [machine ids]} over this computer and every peer that has a document.

    Peers sharing a non-empty host_fp form one group (the group holding this computer is keyed
    SELF_GROUP); peers without host_fp are each their own group.
    """
    groups = {SELF_GROUP: [self_id]}
    for p in peers or []:
        if not p.get("doc") or _is_removed(p):
            continue
        fp = p["doc"].get("host_fp") or ""
        if fp and self_fp and fp == self_fp:
            key = SELF_GROUP
        elif fp:
            key = "fp:" + fp
        else:
            key = "id:" + p["id"]
        groups.setdefault(key, []).append(p["id"])
    return groups


def group_reps(peers, self_id, self_fp):
    """(groups, {group key: representative id}). This computer represents its own group; otherwise
    the newest written member does."""
    by_id = {p["id"]: p for p in peers or []}
    groups = peer_groups(peers, self_id, self_fp)
    reps = {}
    for key, ids in groups.items():
        if key == SELF_GROUP:
            reps[key] = self_id
        else:
            reps[key] = max(ids, key=lambda i: (_written(by_id[i]), i))
    return groups, reps


def superseded_map(peers, self_id, self_fp):
    """{superseded machine id: id of the computer that replaced it}."""
    groups, reps = group_reps(peers, self_id, self_fp)
    return {i: reps[k] for k, ids in groups.items() for i in ids if i != reps[k]}


def _run_key(r):
    return (round(_run_t(r)), r.get("model") or "", r.get("input"), r.get("output"), r.get("cache"))


def _dedupe_same_computer(run_lists):
    """Runs read from the same logs under different machine ids carry different hashed agent ids;
    drop copies with the same time, model and tokens. The first list wins."""
    seen, out = set(), []
    for runs in run_lists:
        for r in runs:
            k = _run_key(r)
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


# ---- platform

def _peer_plat(peer, platform):
    doc = peer.get("doc")
    if not doc or _is_removed(peer):
        return None
    plat = (doc.get("platforms") or {}).get(platform)
    if not isinstance(plat, dict) or plat.get("installed") is False:
        return None
    return plat


def _plan_of(plat):
    p = plat.get("plan") if isinstance(plat, dict) else None
    return p if isinstance(p, dict) else {}


def _newest(a, b):
    if a is None:
        return b
    if b is None:
        return a
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return b if tb > ta else a


def merge_platform(platform, local_payload, peers, now, self_id, local_plan_samples=None,
                   self_fp=None, adopted=None):
    """The merged payload for one platform.

    local_payload: this computer's payload (never modified). peers: peers.scan items (removed
    computers and items without a document are skipped; retired computers still count).
    local_plan_samples: raw [(t, pools)] from the local store. self_fp: this computer's host_fp
    (peer files with the same one are this computer's older ids). adopted: the validated
    platform block from adopted-<id>.json.gz, merged by per-cell max like the export does.
    Returns local_payload itself when there is nothing to merge for this platform.
    """
    local = local_payload if isinstance(local_payload, dict) else {}
    local_installed = bool(local) and local.get("installed", True) is not False
    peer_plats = []
    for p in peers or []:
        plat = _peer_plat(p, platform)
        if plat is not None:
            peer_plats.append((p["id"], plat))
    peer_any = [(i, pl) for i, pl in peer_plats
                if pl.get("series") or pl.get("subagent_runs")
                or _plan_of(pl).get("samples") or _plan_of(pl).get("last_lookup")]
    adopted = adopted if isinstance(adopted, dict) and (
        adopted.get("series") or adopted.get("subagent_runs")) else None
    how = "add" if platform in ADD_PLATFORMS else "larger"
    machine_count = 1 + sum(1 for p in peers or [] if not _is_removed(p))
    breakdown = how == "add" and machine_count >= 2 and (local_installed or bool(peer_plats))
    if not peer_any and adopted is None and not breakdown:
        return local_payload
    now_min = int(now // 60)
    start_ts = parse_ts(local.get("start")) if local_installed else None
    start_min = int(start_ts // 60) if start_ts is not None else now_min - MINUTES + 1
    lo_ts = start_min * 60

    by_id = {p["id"]: p for p in peers or []}
    groups, reps = group_reps(peers, self_id, self_fp)
    gkey = {i: k for k, ids in groups.items() for i in ids}
    contrib = []     # (id, group key, cells_map, runs) for every computer with an installed platform
    gsess = {}       # group key: [session maps]
    if local_installed:
        runs = [r for r in local.get("subagent_runs") or [] if isinstance(r, dict)]
        contrib.append((self_id, SELF_GROUP, local_cells(local, start_min), runs))
        salt = validate.session_salt(platform, self_fp, self_id)
        gsess.setdefault(SELF_GROUP, []).append(local_sessions(local, start_min, salt))
    if adopted is not None:
        contrib.append((ADOPTED_ID, SELF_GROUP, doc_cells(adopted),
                        [_peer_run(r) for r in adopted.get("subagent_runs") or []]))
        gsess.setdefault(SELF_GROUP, []).append(doc_sessions(adopted))
    for mid, pl in sorted(peer_plats, key=lambda x: (-_written(by_id[x[0]]), x[0])):
        g = gkey.get(mid, "id:" + mid)
        contrib.append((mid, g, doc_cells(pl), [_peer_run(r) for r in pl.get("subagent_runs") or []]))
        gsess.setdefault(g, []).append(doc_sessions(pl))
    gsess = {g: combine_sessions(maps) for g, maps in gsess.items()}

    gcells, gruns = {}, {}
    for _, g, cm, rs in contrib:
        gcells.setdefault(g, []).append(cm)
        gruns.setdefault(g, []).append(rs)
    gcells = {g: (cms[0] if len(cms) == 1 else combine(cms, "max")) for g, cms in gcells.items()}
    gruns = {g: (rl[0] if len(rl) == 1 else _dedupe_same_computer(rl)) for g, rl in gruns.items()}

    merged = combine(list(gcells.values()), how)
    runs = merge_runs(list(gruns.values()), lo_ts, larger=(how == "larger"))

    out = copy.deepcopy(local) if local_installed else {
        "status": FROM_OTHERS, "start": iso(start_min * 60), "minutes": MINUTES,
        "series": [], "subagent_runs": [], "plan": {}, "meta": {}, "installed": True}
    out["series"] = series_out(merged, start_min)
    out["sessions"] = sessions_out(combine_sessions(list(gsess.values())), start_min)
    out["subagent_runs"] = runs
    if not local_installed:
        out["status"] = FROM_OTHERS

    with_data = [g for g in gcells if _has_cells(gcells[g]) or gruns[g]]
    meta = out.setdefault("meta", {})
    meta["machines"] = len(with_data)
    peer_runs = [r for c in contrib if c[0] != self_id or not local_installed for r in c[3]]
    if not local_installed:
        meta["subagent_tokens_attributed"] = any(r.get("input") is not None for r in peer_runs)
    if platform == "opencode":
        missing = any(_total(cell) > 0 and not cell[3] for c in contrib
                      if c[0] != self_id or not local_installed
                      for ent in c[2].values() for cell in ent["cells"].values())
        meta["cost_reported"] = bool(meta.get("cost_reported", True)) and not missing

    if breakdown:
        # Whenever 2 or more computers are listed, every computer with data has an entry (possibly
        # the only one), so a computer missing from here means zero rather than "unknown".
        out["machines"] = {
            reps[g]: {"series": series_out(gcells[g], start_min),
                      "sessions": sessions_out(gsess.get(g) or {}, start_min),
                      "subagent_runs": merge_runs([gruns[g]], lo_ts)}
            for g in with_data if g in reps}

    peer_has_plan = any(_plan_of(pl).get("samples") or _plan_of(pl).get("last_lookup")
                        for _, pl in peer_plats)
    if peer_has_plan or not local_installed:
        by_t = {}
        for _, pl in peer_plats:
            for t, pools in _plan_of(pl).get("samples") or []:
                by_t[round(t)] = (float(t), pools)
        for t, pools in local_plan_samples or []:
            by_t[round(t)] = (float(t), pools)
        samples = sorted((s for s in by_t.values() if s[0] >= now - THIRTY_DAYS_S),
                         key=lambda s: s[0])
        lp = local.get("plan") if local_installed and isinstance(local.get("plan"), dict) else {}
        last = lp.get("last_lookup")
        for _, pl in peer_plats:
            last = _newest(last, _plan_of(pl).get("last_lookup"))
        info = {"status": lp.get("status", "") if local_installed else FROM_OTHERS,
                "last_lookup": last, "next_lookup": lp.get("next_lookup")}
        out["plan"] = plan_block_from_samples(samples, now, info)
    return out


# ---- machines list

def window_tokens(platform_block):
    """Input + output + cache over every minute of a platform block (payload or machine file)."""
    total = 0
    for s in (platform_block or {}).get("series") or []:
        for j in range(len(s.get("input") or [])):
            total += _num(s["input"][j]) + _num(s["output"][j]) + _num(s["cache"][j])
    return total


def peer_name(peer):
    c = peer.get("control") or {}
    doc = peer.get("doc") or {}
    return (c.get("display_name") or doc.get("display_name") or doc.get("hostname")
            or "Computer %s" % str(peer.get("id", ""))[:8])


def machine_rows(self_info, local_payloads, peers, now, self_fp=None):
    """The top-level `machines` list.

    self_info: {id, name, os, hostname, app_version, last_seen (epoch s), status, state}.
    local_payloads: this computer's payloads before merging. Removed computers are left out.
    Self first, then by name. self_fp: this computer's host_fp; rows of the same physical computer
    (older ids) get `superseded_by` (the newer id) and `superseded_by_name`, otherwise both null.
    """
    sup = superseded_map(peers, self_info["id"], self_fp)
    names = {self_info["id"]: self_info["name"]}
    for p in peers or []:
        names[p["id"]] = peer_name(p)

    def stale(state, last_seen):
        return state == "active" and last_seen is not None and now - last_seen > STALE_S

    rows = []
    plats = [n for n, p in (local_payloads or {}).items()
             if isinstance(p, dict) and p.get("installed", True) is not False]
    ls = self_info.get("last_seen")
    state = self_info.get("state") or "active"
    rows.append({"id": self_info["id"], "name": self_info["name"], "os": self_info.get("os", ""),
                 "hostname": self_info.get("hostname", ""),
                 "app_version": self_info.get("app_version", ""), "is_self": True,
                 "last_seen": iso(ls) if ls is not None else None, "state": state,
                 "stale": stale(state, ls), "status": self_info.get("status") or "ok",
                 "superseded_by": None, "superseded_by_name": None, "platforms": plats,
                 "tokens_30d": sum(window_tokens(local_payloads[n]) for n in plats
                                   if n in TOKEN_PLATFORMS)})
    for p in peers or []:
        if _is_removed(p):
            continue
        doc = p.get("doc")
        control = p.get("control") or {}
        cstate = control.get("state")
        state = cstate if cstate in ("active", "retired") else "active"
        if doc:
            ls = doc.get("last_seen")
            dplats = [n for n in validate.PLATFORMS
                      if isinstance((doc.get("platforms") or {}).get(n), dict)
                      and doc["platforms"][n].get("installed") is not False]
            tokens = sum(window_tokens(doc["platforms"][n]) for n in dplats if n in TOKEN_PLATFORMS)
            os_, host, ver = doc.get("os", ""), doc.get("hostname", ""), doc.get("app_version", "")
        else:
            ls = p.get("mtime") or None
            dplats, tokens, os_, host, ver = [], 0, "", "", ""
        rows.append({"id": p["id"], "name": peer_name(p), "os": os_, "hostname": host,
                     "app_version": ver, "is_self": False,
                     "last_seen": iso(ls) if ls is not None else None, "state": state,
                     "stale": stale(state, ls) and p["id"] not in sup,   # a replaced id is quiet by design
                     "status": p.get("status") or "ok",
                     "superseded_by": sup.get(p["id"]),
                     "superseded_by_name": names.get(sup.get(p["id"])),
                     "platforms": dplats, "tokens_30d": tokens})
    rows[1:] = sorted(rows[1:], key=lambda r: (r["name"].lower(), r["id"]))
    return rows
