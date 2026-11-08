"""Build this computer's export from LOCAL payloads and write it to the shared folder.

The export is built before any merge, so data from other computers is never re-exported. It holds
token counts, costs, model names, hashed agent and session ids, session titles (only from Claude
ai-title and custom-title entries, the opencode session title and the Cursor chat name), plan
percentages and this computer's name and OS. It never holds prompts, raw session or agent ids, paths
or credentials.
"""
import gzip
import hashlib
import json
import math
import os
import platform as _platform
import secrets
import socket
from pathlib import Path

from collector.sync import config, peers, validate
from collector.util import MINUTES, host_fingerprint, iso, parse_ts

HEARTBEAT_S = 1800      # rewrite an unchanged file this often so an idle running computer is not stale
THROTTLE_S = 300        # never write more often than this
TMP_MAX_AGE_S = 3600
THIN_AFTER_S = 2 * 86400
THIRTY_DAYS_S = 30 * 86400

KV_HASH = "sync:last_hash"
KV_WRITE = "sync:last_write"
KV_TOKEN = "sync:last_token"
KV_CONFLICT = "sync:conflict"
KV_ADOPT_SEEDED = "sync:adopt_seeded"    # machine id whose adopted file was accepted once

WRITTEN = "written"
UNCHANGED = "unchanged"
THROTTLED = "throttled"
CONFLICT = "conflict"
REMOVED = "removed"
FOLDER_MISSING = "folder missing"
REMOVED_MESSAGE = ("This computer was removed from the shared folder. "
                   "Run token-burn sync enable to rejoin.")

_FIELDS = validate.SERIES_FIELDS    # same order as the cells in collector/aggregate.py
_COST_IDX = (3, 8)


def default_app_version():
    try:
        from token_burn import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def hostname():
    try:
        return validate.strip_control(socket.gethostname(), 64) or "unknown"
    except OSError:
        return "unknown"


def host_fp():
    """This physical computer's fingerprint (16 hex), see collector.util.host_fingerprint."""
    return host_fingerprint()


def os_name():
    try:
        system = _platform.system()
        if system == "Windows":
            name = "Windows %s" % _platform.release()
        elif system == "Darwin":
            name = "macOS %s" % (_platform.mac_ver()[0] or "").split(".")[0]
        else:
            name = system or "Unknown"
    except Exception:
        name = "Unknown"
    return validate.strip_control(name, 40) or "Unknown"


def hash_agent_id(machine_id, original):
    """Stable 16 hex id that cannot be reversed to the original agent id."""
    return hashlib.sha256(("%s:%s" % (machine_id, original)).encode("utf-8")).hexdigest()[:16]


# ---- series

def _new_cell():
    return [0, 0, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0]


def _payload_cells(payload, now_min):
    """{key: {"model", "effort", "cells": {abs_minute: cell}}} from a built payload (relative t)."""
    start = parse_ts(payload.get("start"))
    start_min = int(start // 60) if start is not None else now_min - MINUTES + 1
    lo = now_min - MINUTES + 1
    out = {}
    for s in payload.get("series") or []:
        if not isinstance(s, dict) or not isinstance(s.get("t"), list):
            continue
        model = validate.strip_control(str(s.get("model") or ""), 120)
        effort = validate.strip_control(str(s.get("effort") or ""), 120)
        key = validate.strip_control(str(s.get("key") or ""), 120) or (
            "%s-%s" % (model, effort) if effort else model)
        ent = out.setdefault(key, {"model": model, "effort": effort, "cells": {}})
        cols = [s.get(f) or [] for f in _FIELDS]
        for i, t in enumerate(s["t"]):
            m = start_min + int(t)
            if m < lo or m > now_min:
                continue
            cell = ent["cells"].setdefault(m, _new_cell())
            for j, col in enumerate(cols):
                if i < len(col) and isinstance(col[i], (int, float)):
                    cell[j] += col[i]
    return out


def _doc_cells(plat_doc):
    """The same structure from a validated machine-file platform block."""
    out = {}
    for s in plat_doc.get("series") or []:
        ent = out.setdefault(s["key"], {"model": s["model"], "effort": s["effort"], "cells": {}})
        for i, m in enumerate(s["m"]):
            ent["cells"][m] = [s[f][i] for f in _FIELDS]
    return out


def _max_merge(base, other):
    """Per-cell max of two series maps, in place on base."""
    for key, ent in other.items():
        b = base.setdefault(key, {"model": ent["model"], "effort": ent["effort"], "cells": {}})
        for m, cell in ent["cells"].items():
            cur = b["cells"].get(m)
            b["cells"][m] = list(cell) if cur is None else [max(x, y) for x, y in zip(cur, cell)]


def _series_out(cells_map):
    out = []
    for key in sorted(cells_map):
        ent = cells_map[key]
        ms = sorted(ent["cells"])
        if not ms:
            continue
        s = {"key": key, "model": ent["model"], "effort": ent["effort"], "m": ms}
        for j, f in enumerate(_FIELDS):
            vals = [ent["cells"][m][j] for m in ms]
            s[f] = [round(float(v), 6) for v in vals] if j in _COST_IDX else [int(v) for v in vals]
        out.append(s)
    return out


# ---- sessions

def _payload_sessions(payload, now_min, salt):
    """{hashed id: {"title", "series": {key: {"model", "effort", "cells": {abs_minute: [4]}}}}}."""
    start = parse_ts(payload.get("start"))
    start_min = int(start // 60) if start is not None else now_min - MINUTES + 1
    lo = now_min - MINUTES + 1
    out = {}
    for ses in payload.get("sessions") or []:
        if not isinstance(ses, dict) or not isinstance(ses.get("series"), list):
            continue
        sid = validate.session_hash(salt, str(ses.get("id") or ""))
        title = None if ses.get("title_local") else ses.get("title")
        title = validate.strip_control(title, validate.TITLE_MAX) if isinstance(title, str) else ""
        ent = out.setdefault(sid, {"title": title or None, "series": {}})
        for s in ses["series"]:
            if not isinstance(s, dict) or not isinstance(s.get("t"), list):
                continue
            model = validate.strip_control(str(s.get("model") or ""), 120)
            effort = validate.strip_control(str(s.get("effort") or ""), 120)
            key = validate.strip_control(str(s.get("key") or ""), 120) or (
                "%s-%s" % (model, effort) if effort else model)
            se = ent["series"].setdefault(key, {"model": model, "effort": effort, "cells": {}})
            cols = [s.get(f) or [] for f in validate.SESSION_FIELDS]
            for i, t in enumerate(s["t"]):
                m = start_min + int(t)
                if m < lo or m > now_min:
                    continue
                cell = se["cells"].setdefault(m, [0, 0, 0, 0.0])
                for j, col in enumerate(cols):
                    if i < len(col) and isinstance(col[i], (int, float)):
                        cell[j] += col[i]
    return out


def _doc_sessions(plat_doc):
    out = {}
    for ses in plat_doc.get("sessions") or []:
        ent = out.setdefault(ses["id"], {"title": ses["title"], "series": {}})
        for s in ses["series"]:
            se = ent["series"].setdefault(s["key"], {"model": s["model"], "effort": s["effort"],
                                                     "cells": {}})
            for i, m in enumerate(s["m"]):
                se["cells"][m] = [s[f][i] for f in validate.SESSION_FIELDS]
    return out


def _max_merge_sessions(base, other):
    for sid, ent in other.items():
        b = base.setdefault(sid, {"title": ent["title"], "series": {}})
        if not b["title"]:
            b["title"] = ent["title"]
        _max_merge(b["series"], ent["series"])


def _sessions_out(sessions):
    def tok(ent):
        return sum(c[0] + c[1] + c[2] for se in ent["series"].values() for c in se["cells"].values())
    out = []
    for sid in sorted(sessions, key=lambda k: (-tok(sessions[k]), k)):
        ent = sessions[sid]
        series = []
        for key in sorted(ent["series"]):
            se = ent["series"][key]
            ms = sorted(se["cells"])
            if not ms:
                continue
            s = {"key": key, "model": se["model"], "effort": se["effort"], "m": ms}
            for j, f in enumerate(validate.SESSION_FIELDS):
                vals = [se["cells"][m][j] for m in ms]
                s[f] = [round(float(v), 6) for v in vals] if f == "cost" else [int(v) for v in vals]
            series.append(s)
        if series:
            out.append({"id": sid, "title": ent["title"],
                        "series": series[:validate.MAX_SESSION_SERIES]})
    return out[:validate.MAX_SESSIONS]


# ---- runs and plan

def _export_runs(machine_id, runs, lo_ts):
    out = []
    for r in runs or []:
        if not isinstance(r, dict) or r.get("agent_id") in (None, ""):
            continue
        ts = parse_ts(r.get("t"))
        if ts is None or ts < lo_ts:
            continue
        aid = hash_agent_id(machine_id, r["agent_id"])
        atype = validate.strip_control(str(r.get("agent_type") or ""), 40)
        run = {"agent_id": aid, "agent_name": "%s · %s" % (atype or "subagent", aid[:7]),
               "agent_type": atype, "model": validate.strip_control(str(r.get("model") or ""), 120),
               "effort": validate.strip_control(str(r.get("effort") or ""), 120), "t": iso(ts),
               "status": validate.strip_control(str(r.get("status") or ""), 120)}
        for f in ("input", "output", "cache"):
            v = validate.as_int(r.get(f))
            run[f] = None if v is None else max(0, v)
        c = validate.as_float(r.get("cost"))
        run["cost"] = None if c is None else round(max(0.0, c), 6)
        run["cost_estimated"] = r.get("cost_estimated") is True
        out.append(run)
    return out


def _merge_runs(local, adopted):
    by_id = {r["agent_id"]: r for r in adopted}
    by_id.update({r["agent_id"]: r for r in local})
    runs = sorted(by_id.values(), key=lambda r: (r["t"], r["agent_id"]), reverse=True)
    return runs[:validate.MAX_RUNS]


def _clean_pools(pools):
    out = {}
    if not isinstance(pools, dict):
        return out
    for pid, v in list(pools.items())[:validate.MAX_POOLS]:
        if not isinstance(pid, str) or not isinstance(v, dict):
            continue
        pct = validate.as_float(v.get("pct"))
        if pct is None or pct < 0 or pct > 1000:
            continue
        pool = {"pct": pct}
        ra = v.get("resets_at")
        if isinstance(ra, str) and len(ra) <= 40:
            pool["resets_at"] = validate.strip_control(ra, 40)
        out[validate.strip_control(pid, 60)] = pool
    return out


def thin_raw(samples, now):
    """Like aggregate.thin_samples but on raw (t, pools): older than 2 days, one per 5 minutes."""
    out = []
    last_kept = None
    cutoff = now - THIN_AFTER_S
    n = len(samples)
    for i, (ts, pools) in enumerate(samples):
        if ts < cutoff and last_kept is not None and ts - last_kept < 300 and i != n - 1:
            continue
        last_kept = ts
        out.append((ts, pools))
    return out


def _export_plan(local_samples, last_lookup, adopted_plan, now):
    by_t = {}
    for t, pools in (adopted_plan or {}).get("samples", []):
        by_t[round(t)] = (float(t), pools)
    for item in local_samples or []:
        try:
            t, pools = item
            t = float(t)
        except (TypeError, ValueError):
            continue
        if math.isfinite(t):
            by_t[round(t)] = (t, pools)
    samples = []
    for t, pools in sorted(by_t.values(), key=lambda s: s[0]):
        if t < now - THIRTY_DAYS_S:
            continue
        pools = _clean_pools(pools)
        if pools:
            samples.append((t, pools))
    samples = thin_raw(samples, now)
    ll = last_lookup if isinstance(last_lookup, str) and parse_ts(last_lookup) is not None else None
    if ll is None:
        ll = (adopted_plan or {}).get("last_lookup")
    return {"last_lookup": ll, "samples": [[t, p] for t, p in samples]}


# ---- build

def load_adopted(data_dir, machine_id, now):
    """Validated adopted-<id>.json.gz, or None when absent or unreadable."""
    if data_dir is None:
        return None
    path = Path(data_dir) / ("adopted-%s.json.gz" % machine_id)
    try:
        doc = validate.load_json_bytes(validate.read_gz_capped(path))
    except validate.ReadError:
        return None
    clean, _ = validate.parse_machine(doc, machine_id, now)
    return clean


def build_export(local_payloads, local_plan_samples, cfg, now, app_version=None, data_dir=None):
    """The machine export document (a dict, not yet written).

    local_payloads: {platform: payload} as build_platform_payload returns (relative `t`, `start`).
    local_plan_samples: {platform: [(t, pools), ...]} raw samples from the store.
    data_dir: when given, adopted-<id>.json.gz there is merged in by per-cell max, and deleted once
    it no longer holds any minute inside the 30 day window.
    """
    s = config.sync_settings(cfg)
    machine_id = s["machine_id"]
    if not machine_id:
        raise ValueError("sync is not configured")
    now_min = int(now // 60)
    lo_ts = (now_min - MINUTES + 1) * 60
    adopted = load_adopted(data_dir, machine_id, now)
    plats = {}
    for name in validate.PLATFORMS:
        payload = (local_payloads or {}).get(name)
        ad = adopted["platforms"].get(name) if adopted else None
        if not isinstance(payload, dict) or payload.get("installed") is False:
            if ad is None:
                plats[name] = {"installed": False}
                continue
            payload = {}
        cells = _payload_cells(payload, now_min)
        if ad:
            _max_merge(cells, _doc_cells(ad))
        sessions = _payload_sessions(payload, now_min,
                                     validate.session_salt(name, host_fp(), machine_id))
        if ad:
            _max_merge_sessions(sessions, _doc_sessions(ad))
        runs = _export_runs(machine_id, payload.get("subagent_runs"), lo_ts)
        if ad:
            runs = _merge_runs(runs, ad["subagent_runs"])
        plan_block = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        plats[name] = {
            "installed": True,
            "status": validate.strip_control(str(payload.get("status") or (ad or {}).get("status")
                                                 or "ok"), 120),
            "series": _series_out(cells),
            "sessions": _sessions_out(sessions),
            "subagent_runs": runs[:validate.MAX_RUNS],
            "plan": _export_plan((local_plan_samples or {}).get(name), plan_block.get("last_lookup"),
                                 ad["plan"] if ad else None, now)}
    if adopted is not None and data_dir is not None and not any(
            s["m"] for p in adopted["platforms"].values() for s in p["series"]):
        try:
            os.remove(Path(data_dir) / ("adopted-%s.json.gz" % machine_id))
        except OSError:
            pass
    return {"format": "token-burn-machine", "schema": 1, "machine_id": machine_id,
            "display_name": validate.strip_control(s["machine_name"], config.NAME_MAX),
            "hostname": hostname(), "host_fp": host_fp(), "os": os_name(),
            "app_version": validate.strip_control(app_version or default_app_version(), 20),
            "written_at": iso(now), "write_token": "", "minutes": MINUTES, "platforms": plats}


def body_hash(doc):
    """sha256 of the canonical body, ignoring written_at and write_token."""
    body = {k: v for k, v in doc.items() if k not in ("written_at", "write_token")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


# ---- write

def _read_own_header(path):
    try:
        return validate.load_json_bytes(validate.read_gz_capped(path))
    except validate.ReadError:
        return None


def check_conflict(store, folder, machine_id, hostname, host_fp=None, adopted=False):
    """None, or a message when another computer is writing this computer's file.

    A file whose host_fp equals this computer's is the same computer (a crash between the file
    replace and the store commit, or usage.db restored from a backup): never a conflict. Only a
    different host_fp is one. A file without host_fp (older version) follows the hostname rule: the
    write_token must be the one this computer wrote last, and the first write after an install (no
    remembered token) is a reinstall when the hostname matches, a conflict when it differs.
    adopted: the config says this computer adopted an existing identity; the existing file is
    accepted once (no remembered token yet), whatever its hostname.
    """
    path = peers.machine_file(folder, machine_id)
    if not path.exists():
        return None
    doc = _read_own_header(path)
    if doc is None:
        return None
    last_token = store.kv_get(KV_TOKEN)
    host = validate.strip_control(str(doc.get("hostname") or ""), 64) or "unknown"
    msg = ("Another computer (hostname %s) is writing with this computer's ID. "
           "Run token-burn sync reset-id on one of them." % host)
    if last_token and doc.get("write_token") == last_token:
        return None
    if not last_token and adopted and store.kv_get(KV_ADOPT_SEEDED) != machine_id:
        return None
    file_fp = doc.get("host_fp")
    file_fp = file_fp if isinstance(file_fp, str) and validate.HOST_FP_RE.fullmatch(file_fp) else ""
    if file_fp and host_fp:
        if file_fp == host_fp:
            return None
        if not last_token:
            return msg
    elif not last_token:
        same = str(doc.get("hostname") or "").strip().lower() == str(hostname or "").strip().lower()
        return None if same else msg
    written = parse_ts(doc.get("written_at")) if isinstance(doc.get("written_at"), str) else None
    last_write = store.kv_get(KV_WRITE)
    if written is not None and isinstance(last_write, (int, float)) and written > last_write:
        return msg
    return None


def _clean_tmp(mdir, machine_id, now):
    prefix = ".tmp-%s-" % machine_id
    try:
        names = os.listdir(mdir)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix):
            try:
                if now - (mdir / name).stat().st_mtime > TMP_MAX_AGE_S:
                    os.remove(mdir / name)
            except OSError:
                pass


def _gzip_bytes(doc):
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=6, mtime=0) as g:
        g.write(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return buf.getvalue()


def maybe_write(store, folder, doc, now, force=False, adopted=False):
    """Write doc to machines/<id>.json.gz when due. Returns a status string.

    WRITTEN, UNCHANGED (same content, heartbeat not due), THROTTLED (under 300 s since the last
    write), CONFLICT (message in kv sync:conflict), REMOVED (tombstone), FOLDER_MISSING, or
    "write failed (<ExcName>)". force skips the throttle and the unchanged check only. adopted:
    see check_conflict.
    """
    machine_id = doc["machine_id"]
    if not config.valid_machine_id(machine_id):
        raise ValueError("invalid machine id")
    if not os.path.isdir(str(folder)):
        return FOLDER_MISSING
    control = peers.read_control(folder, machine_id)
    if control is not None and control["state"] == "removed":
        return REMOVED
    if store.kv_get(KV_CONFLICT):
        return CONFLICT
    last_write = store.kv_get(KV_WRITE)
    since = now - last_write if isinstance(last_write, (int, float)) else None
    if not force and since is not None and 0 <= since < THROTTLE_S:
        return THROTTLED
    digest = body_hash(doc)
    heartbeat_due = since is None or since < 0 or since >= HEARTBEAT_S
    if not force and digest == store.kv_get(KV_HASH) and not heartbeat_due:
        return UNCHANGED
    msg = check_conflict(store, folder, machine_id, doc.get("hostname"), doc.get("host_fp"),
                          adopted=adopted)
    if msg:
        store.kv_set(KV_CONFLICT, msg)
        store.commit()
        return CONFLICT
    token = secrets.token_hex(8)
    out = dict(doc)
    out["written_at"] = iso(now)
    out["write_token"] = token
    # Remember the new token (committed) BEFORE the file is replaced. A crash in between leaves a file
    # with a token this store does not know, which is harmless: same host_fp, or written_at is not
    # later than sync:last_write. The other order could lock exports forever.
    prev = {k: store.kv_get(k) for k in (KV_HASH, KV_WRITE, KV_TOKEN, KV_CONFLICT, KV_ADOPT_SEEDED)}
    store.kv_set(KV_HASH, digest)
    store.kv_set(KV_WRITE, float(now))
    store.kv_set(KV_TOKEN, token)
    store.kv_set(KV_CONFLICT, None)
    if adopted:
        store.kv_set(KV_ADOPT_SEEDED, machine_id)
    store.commit()
    try:
        peers.ensure_layout(folder)
        path = peers.machine_file(folder, machine_id)
        peers.atomic_write_bytes(path, _gzip_bytes(out))
        _clean_tmp(path.parent, machine_id, now)
    except OSError as e:
        for k, v in prev.items():
            store.kv_set(k, v)
        store.commit()
        return "write failed (%s)" % type(e).__name__
    return WRITTEN
