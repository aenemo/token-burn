"""Claude Code transcript adapter (incremental).

Ported from the Claude Token Usage Graphs collector. Reads transcripts, never
records prompts or task text and never logs tokens or message content.
"""
import json
import os
from pathlib import Path

from collector import util
from collector.effort import normalise_effort
from collector.schema import SubagentRun, UsageRecord

PLATFORM = "claude"
AUTO = "Auto Cost"
AUTO_LABELS = {"default", "auto", "<synthetic>", ""}
DAYS = 30
OFFSETS_KEY = "claude:offsets"
TITLES_KEY = "claude:titles_v1"
# Session titles come only from these entries. last-prompt holds prompt text and is never read.
TITLE_TYPES = {"ai-title": ("aiTitle", "ai"), "custom-title": ("customTitle", "custom")}


def label(model):
    if not isinstance(model, str):
        return AUTO
    m = model.strip()
    if m.lower() in AUTO_LABELS:
        return AUTO
    return m


def effort_of(o):
    e = o.get("perTurnEffort") or o.get("effort")
    if not isinstance(e, str) or not e.strip():
        return ""
    return normalise_effort(e)


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def discover(cdir):
    """Return [(path, is_subagent_file)]."""
    proj = Path(cdir) / "projects"
    files = []
    if not proj.is_dir():
        return files
    for p in proj.glob("*/*.jsonl"):
        files.append((p, False))
    for p in proj.glob("*/*/subagents/agent-*.jsonl"):
        files.append((p, True))
    return files


def agent_name(agent_type, agent_id):
    return "%s · %s" % (agent_type or "subagent", (agent_id or "")[:7])


def _meta_type(path):
    """agentType from a sibling agent-<id>.meta.json, or ''."""
    try:
        with open(path.with_name(path.stem + ".meta.json"), "r", encoding="utf-8") as f:
            m = json.load(f)
        t = m.get("agentType") if isinstance(m, dict) else None
        return t if isinstance(t, str) else ""
    except (OSError, ValueError):
        return ""


def _is_title_line(raw):
    return b'"ai-title"' in raw or b'"custom-title"' in raw


def _take_title(store, o, session_default):
    """Store the title from an ai-title or custom-title entry. True when o was a title entry."""
    spec = TITLE_TYPES.get(o.get("type"))
    if spec is None:
        return False
    title = o.get(spec[0])
    sess = o.get("sessionId") or session_default
    if isinstance(title, str) and isinstance(sess, str):
        store.upsert_session_title(PLATFORM, sess, title, spec[1])
    return True


def _scan_titles(store, path, is_sub_file):
    """Read only the title entries of a whole file (one-off backfill for files read before titles existed)."""
    if is_sub_file:
        return
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        for raw in f:
            if not _is_title_line(raw):
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if isinstance(o, dict):
                _take_title(store, o, path.stem)


def _process_file(store, path, is_sub_file, offsets, cutoff_min, stats, runs):
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return
    off = offsets.get(key, 0)
    if not isinstance(off, int) or off < 0 or size < off:
        off = 0  # shrunk or rotated: re-read (dedupe protects counts)
    if size == off:
        offsets[key] = off
        return
    try:
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read()
    except OSError:
        return
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return
    chunk = chunk[:cut + 1]
    new_off = off + len(chunk)
    session_default = path.parent.parent.name if is_sub_file else path.stem
    file_agent = ""
    if is_sub_file and path.stem.startswith("agent-"):
        file_agent = path.stem[len("agent-"):]
    for raw in chunk.split(b"\n"):
        if not raw:
            continue
        is_cost = b'"cost-state"' in raw
        if not is_cost and b'"assistant"' not in raw and not _is_title_line(raw):
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        if _take_title(store, o, session_default):
            continue
        typ = o.get("type")
        sess = o.get("sessionId") or session_default
        if typ == "cost-state":
            mu = o.get("modelUsage")
            if isinstance(mu, dict):
                agg = {}
                for m, v in mu.items():
                    if isinstance(v, dict):
                        lab = label(m)
                        agg[lab] = agg.get(lab, 0.0) + float(num(v.get("costUSD")))
                for lab, c in agg.items():
                    store.set_session_cost(PLATFORM, sess, lab, c)
            continue
        if typ != "assistant":
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        ts = util.parse_ts(o.get("timestamp"))
        if ts is None:
            continue
        minute = int(ts // 60)
        if minute < cutoff_min:
            continue
        mid, rid = msg.get("id"), o.get("requestId")
        dkey = None
        if mid or rid:
            dkey = "%s|%s" % (mid or "", rid or "")
        elif o.get("uuid"):
            dkey = "u|%s" % o["uuid"]
        out_tok = int(num(usage.get("output_tokens")))
        is_dup = False
        if dkey is not None and not store.seen_add(PLATFORM, dkey, minute, out_tok):
            # A streamed message is written several times and the first copy can carry a
            # partial output_tokens. Count only the growth, never the repeated input and cache.
            prev = store.seen_raise(PLATFORM, dkey, out_tok)
            if prev is None:
                continue
            is_dup = True
        is_sub = bool(o.get("isSidechain")) or is_sub_file
        aid = ""
        if is_sub:
            oa = o.get("agentId")
            aid = file_agent or (oa if isinstance(oa, str) else "")
        lab = label(msg.get("model"))
        eff = "" if lab == AUTO else effort_of(o)
        store.add_usage(UsageRecord(
            platform=PLATFORM,
            agent_kind="subagent" if is_sub else "primary",
            agent_id=aid,
            session_id=sess,
            model=lab,
            effort_level=eff,
            input_tokens=0 if is_dup else int(num(usage.get("input_tokens"))),
            output_tokens=out_tok - prev if is_dup else out_tok,
            cache_read_tokens=0 if is_dup else int(num(usage.get("cache_read_input_tokens"))),
            cache_write_tokens=0 if is_dup else int(num(usage.get("cache_creation_input_tokens"))),
            cost_usd=0.0,
            timestamp=ts,
            requests=0 if is_dup else 1,
        ))
        if is_dup:
            continue
        stats["requests"] += 1
        if lab == AUTO:
            stats["auto"] += 1
        if aid:
            r = runs.get(aid)
            if r is None:
                r = runs[aid] = {"session": sess, "model": "", "effort": "", "t": 0.0,
                                 "type": _meta_type(path) if is_sub_file else ""}
            if ts >= r["t"]:
                r["t"] = ts
                if lab != AUTO or not r["model"]:
                    r["model"] = lab
                if eff:
                    r["effort"] = eff
    offsets[key] = new_off


def _read_hook_log(path, cutoff_epoch, hook_runs):
    """Latest hook record per agent_id."""
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            aid = o.get("agent_id")
            ts = util.parse_ts(o.get("t"))
            if not isinstance(aid, str) or not aid or ts is None or ts < cutoff_epoch:
                continue
            prev = hook_runs.get(aid)
            if prev is None or ts >= prev[0]:
                hook_runs[aid] = (ts, o)


def _s(v):
    return v if isinstance(v, str) else ""


def collect(store, now, log, backfill=False):
    stats = {"requests": 0, "auto": 0}
    nfiles = 0
    try:
        cutoff_epoch = now - DAYS * 86400
        cutoff_min = int(cutoff_epoch // 60)
        offsets = {} if backfill else store.kv_get(OFFSETS_KEY, {})
        if not isinstance(offsets, dict):
            offsets = {}
        runs = {}
        titles_done = bool(store.kv_get(TITLES_KEY, False))
        for path, is_sub in discover(util.claude_dir()):
            try:
                if path.stat().st_mtime < cutoff_epoch:
                    continue
            except OSError:
                continue
            nfiles += 1
            try:
                if not titles_done and offsets.get(str(path)):
                    _scan_titles(store, path, is_sub)
                _process_file(store, path, is_sub, offsets, cutoff_min, stats, runs)
            except Exception as e:
                log.warning("file error %s: %s", path.name, type(e).__name__)
        for k in [k for k in offsets if not os.path.exists(k)]:
            del offsets[k]
        store.kv_set(OFFSETS_KEY, offsets)
        store.kv_set(TITLES_KEY, True)

        hook_runs = {}
        _read_hook_log(util.CLAUDE_HOOK_LOG, cutoff_epoch, hook_runs)
        for aid in set(runs) | set(hook_runs):
            r = runs.get(aid) or {}
            h = hook_runs.get(aid)
            ho = h[1] if h else {}
            atype = _s(ho.get("agent_type")) or r.get("type", "")
            model = label(ho["model"]) if _s(ho.get("model")) else r.get("model", "")
            if r.get("model") and (not model or (model == AUTO and r["model"] != AUTO)):
                model = r["model"]
            t = max(r.get("t", 0.0), h[0] if h else 0.0)
            store.upsert_subagent_run(SubagentRun(
                platform=PLATFORM,
                agent_id=aid,
                agent_type=atype,
                agent_name=agent_name(atype, aid),
                session_id=_s(ho.get("session")) or r.get("session", ""),
                model=model,
                effort_level=normalise_effort(ho.get("effort")) or r.get("effort", ""),
                t=t,
                status="completed",
                input_tokens=int(num(ho.get("input"))),
                output_tokens=int(num(ho.get("output"))),
                cache_read_tokens=int(num(ho.get("cache_read"))),
                cache_write_tokens=int(num(ho.get("cache_write"))),
            ))
        try:
            store.commit()
        except Exception:
            pass
        return {"status": "ok", "meta": {"auto_label_requests": stats["auto"],
                                         "requests": stats["requests"], "files": nfiles}}
    except Exception as e:
        try:
            log.error("claude adapter error: %s", type(e).__name__)
        except Exception:
            pass
        return {"status": "claude collect failed (%s)" % type(e).__name__,
                "meta": {"auto_label_requests": stats["auto"],
                         "requests": stats["requests"], "files": nfiles}}
