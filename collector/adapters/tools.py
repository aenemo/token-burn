"""Readers for standalone AI tools that keep their own local usage files.

Gemini CLI -> Google, Qwen Code -> Qwen, Mistral Vibe -> Mistral, GitHub Copilot CLI -> GitHub Copilot and
Grok Build -> xAI Grok. Each fills the same platform column that the opencode provider split fills (see
collector/providers.py), so a provider's usage is the sum of what opencode and the tool itself recorded.

Everything is read-only and tolerant: the formats come from the tools' documentation and from other usage
trackers, not from this project's own samples, so a record is read by looking for the usual token field names
(input, output, cached, thoughts, ...) rather than by one fixed layout, and anything unreadable is skipped.
Message text is never stored or logged. None of these tools report a cost, so cost is always 0 and the dashboard
says token use is the figure to rely on.

Large append-only files are read once and then only from where the last pass stopped (the byte offset is kept
in the store). Cumulative counters (Vibe meta.json, Grok signals.json) add only what grew since the last pass.
"""
import hashlib
import json
import os
from pathlib import Path

from collector import util
from collector.schema import UsageRecord

DAYS = 30
STALE_S = 24 * 3600
MAX_DEPTH = 6
MAX_LINE = 4 * 1024 * 1024

IN_KEYS = ("input", "inputTokens", "input_tokens", "promptTokens", "prompt_tokens", "promptTokenCount",
           "session_prompt_tokens")
OUT_KEYS = ("output", "outputTokens", "output_tokens", "completionTokens", "completion_tokens",
            "candidatesTokenCount", "session_completion_tokens")
CACHE_READ_KEYS = ("cached", "cachedTokens", "cached_tokens", "cachedContentTokenCount", "cacheReadTokens",
                   "cache_read_tokens", "cache_read_input_tokens", "cachedInputTokens", "cached_input_tokens",
                   "session_cached_tokens", "cacheRead")
CACHE_WRITE_KEYS = ("cacheWriteTokens", "cache_write_tokens", "cache_creation_input_tokens",
                    "cacheCreationTokens", "cacheWrite")
REASON_KEYS = ("thoughts", "thoughtsTokens", "thoughtsTokenCount", "reasoning", "reasoningTokens",
               "reasoning_tokens")
TOOL_KEYS = ("tool", "toolTokens", "toolUsePromptTokenCount")
UNCACHED_INPUT_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens")   # input already excludes cache
CONTAINERS = ("tokens", "usageMetadata", "usage", "_meta", "update", "message", "data", "metrics", "stats")
TS_KEYS = ("timestamp", "ts", "time", "createdAt", "created_at", "created")


# ---- small helpers

def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _first(d, keys):
    for k in keys:
        v = _int(d.get(k))
        if v is not None:
            return v
    return None


def extract(d, add_reasoning=True):
    """(input, output, cache_read, cache_write) from one dict of token counters, or None when it has none.
    Input counts that include the cached part have it subtracted, so the four numbers never overlap."""
    if not isinstance(d, dict):
        return None
    i, o = _first(d, IN_KEYS), _first(d, OUT_KEYS)
    if i is None and o is None:
        return None
    i, o = i or 0, o or 0
    cr, cw = _first(d, CACHE_READ_KEYS) or 0, _first(d, CACHE_WRITE_KEYS) or 0
    if not any(k in d for k in UNCACHED_INPUT_KEYS):
        i = max(i - cr, 0)
    if add_reasoning:
        o += _first(d, REASON_KEYS) or 0
    i += _first(d, TOOL_KEYS) or 0
    return (i, o, cr, cw)


def find_usage(obj, add_reasoning=True, depth=3):
    """The first non-empty token counters in obj or in a dict nested up to depth levels below it."""
    if not isinstance(obj, dict) or depth < 0:
        return None
    got = extract(obj, add_reasoning)
    if got and sum(got) > 0:
        return got
    kids = [obj[k] for k in CONTAINERS if isinstance(obj.get(k), dict)]
    kids += [v for k, v in obj.items() if k not in CONTAINERS and isinstance(v, dict)]
    for kid in kids:
        got = find_usage(kid, add_reasoning, depth - 1)
        if got:
            return got
    return None


def _ts(obj, fallback):
    for k in TS_KEYS:
        t = util.parse_ts(obj.get(k)) if isinstance(obj, dict) else None
        if t:
            return t
    return fallback


def _model(obj, default=""):
    if isinstance(obj, dict):
        for k in ("model", "modelId", "model_id", "modelName", "model_name", "current_model_id", "active_model"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return default


def home():
    """The folder the tools' dot folders live in. TOKEN_BURN_TOOLS_HOME replaces it for every tool (tests, trials)."""
    env = os.environ.get("TOKEN_BURN_TOOLS_HOME")
    return Path(env) if env else Path.home()


def tool_root(env_name, dot_folder):
    """A tool's data folder: its own environment variable (unless TOKEN_BURN_TOOLS_HOME is set), else ~/<dot_folder>."""
    env = None if os.environ.get("TOKEN_BURN_TOOLS_HOME") else os.environ.get(env_name)
    return Path(env) if env else home() / dot_folder


def _walk(root, match, cutoff, depth=MAX_DEPTH):
    """Files under root (at most depth levels down) that match(name, dirpath) and changed since cutoff."""
    out = []
    root = Path(root)
    if not root.is_dir():
        return out
    base = len(root.parts)
    for dp, dns, fns in os.walk(str(root)):
        if len(Path(dp).parts) - base >= depth:
            dns[:] = []
        for fn in fns:
            if not match(fn, dp):
                continue
            p = os.path.join(dp, fn)
            try:
                if os.stat(p).st_mtime >= cutoff:
                    out.append(p)
            except OSError:
                continue
    return sorted(out)


def _fid(path):
    return hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:10]


def _iter_jsonl(path, state, needles=(b"oken",)):
    """Yield (start_offset, dict) for each complete JSON line after state["off"], advancing state["off"].
    A line is parsed only when it contains one of the needles (cheap filter for big files)."""
    size = os.stat(path).st_size
    pos = int(state.get("off") or 0)
    if size < pos:
        pos = 0                     # the file was replaced or truncated: start again (seen keys stop double counting)
    with open(path, "rb") as f:
        f.seek(pos)
        while True:
            start = pos
            line = f.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = f.readline(MAX_LINE)
                pos = f.tell()
                state["off"] = pos
                continue
            complete = line.endswith(b"\n")
            obj = None
            if any(n in line for n in needles):
                try:
                    obj = json.loads(line)
                except ValueError:
                    obj = None
                if obj is None and not complete:
                    break           # half written line: read it on the next pass
            pos = start + len(line)
            state["off"] = pos
            if isinstance(obj, dict):
                yield start, obj


def _read_json(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(32 * 1024 * 1024)
        return json.loads(raw)
    except (OSError, ValueError):
        return None


def _sig(path):
    st = os.stat(path)
    return [st.st_size, int(st.st_mtime)]


class _Ctx:
    """What a reader needs: the store, the platform it fills, the 30 day cutoff and a saved state dict."""

    def __init__(self, store, plat, reader, now, log):
        self.store, self.plat, self.now, self.log = store, plat, now, log
        self.cutoff = now - DAYS * 86400
        self.key = "tools:%s:%s" % (plat, reader)
        old = store.kv_get(self.key)
        self.old = old if isinstance(old, dict) else {}
        self.new = {}
        self.count = 0

    def file_state(self, path):
        st = self.old.get(path)
        st = dict(st) if isinstance(st, dict) else {}
        self.new[path] = st
        return st

    def emit(self, key, sid, model, ts, tokens):
        """Add one request's tokens. key de-duplicates (None for deltas that are tracked in the file state)."""
        i, o, cr, cw = tokens
        ts = min(float(ts), self.now)
        if i + o + cr + cw <= 0 or ts < self.cutoff:
            return False
        if key is not None and not self.store.seen_add(self.plat, key, int(ts // 60)):
            return False
        self.store.add_usage(UsageRecord(
            platform=self.plat, agent_kind="primary", agent_id="", session_id=sid or "unknown",
            model=model or "unknown", effort_level="", input_tokens=i, output_tokens=o,
            cache_read_tokens=cr, cache_write_tokens=cw, cost_usd=0.0, timestamp=ts, requests=1))
        self.count += 1
        return True

    def save(self):
        self.store.kv_set(self.key, self.new)
        self.store.commit()


def _record_usage(ctx, path, state, off, obj, mtime, sid, add_reasoning=True):
    got = find_usage(obj, add_reasoning)
    if not got:
        return
    ident = obj.get("id") if isinstance(obj.get("id"), (str, int)) else off
    model = _model(obj) or state.get("model", "")
    ctx.emit("tool|%s|%s" % (_fid(path), ident), sid, model, _ts(obj, mtime), got)


# ---- Gemini CLI -> google

def gemini(store, now, log):
    ctx = _Ctx(store, "google", "gemini", now, log)
    base = Path(os.environ["GEMINI_CLI_HOME"]) if os.environ.get("GEMINI_CLI_HOME") \
        and not os.environ.get("TOKEN_BURN_TOOLS_HOME") else home()      # Gemini CLI keeps .gemini inside this
    _chat_files(ctx, base / ".gemini" / "tmp")
    ctx.save()
    return ctx.count


def _chat_files(ctx, base, accept=None):
    """Gemini-format chat files (session-*.jsonl or session-*.json under a chats folder); also used for old Qwen."""
    def match(fn, dp):
        return fn.startswith("session-") and fn.endswith((".json", ".jsonl")) and "chats" in Path(dp).parts

    for path in _walk(base, match, ctx.cutoff):
        try:
            mtime = os.stat(path).st_mtime
            state = ctx.file_state(path)
            stem = Path(path).stem
            if path.endswith(".jsonl"):
                for off, obj in _iter_jsonl(path, state, (b"oken", b"sessionId")):
                    if isinstance(obj.get("sessionId"), str):
                        state["sid"] = obj["sessionId"]
                    if accept is None or accept(obj):
                        _record_usage(ctx, path, state, off, obj, mtime, state.get("sid") or stem)
            else:
                sig = _sig(path)
                if state.get("sig") == sig:
                    continue
                state["sig"] = sig
                doc = _read_json(path)
                msgs = doc.get("messages") if isinstance(doc, dict) else doc if isinstance(doc, list) else []
                sid = doc.get("sessionId") if isinstance(doc, dict) and isinstance(doc.get("sessionId"), str) else stem
                for n, m in enumerate(msgs if isinstance(msgs, list) else []):
                    if isinstance(m, dict) and (accept is None or accept(m)):
                        _record_usage(ctx, path, state, "m%d" % n, m, mtime, sid)
        except OSError:
            continue


# ---- Qwen Code -> qwen

_QWEN_SKIP_TYPES = ("system", "user", "tool_result", "telemetry")


def _qwen_accept(obj):
    return obj.get("type") not in _QWEN_SKIP_TYPES       # api_response telemetry repeats the assistant records


def qwen(store, now, log):
    ctx = _Ctx(store, "qwen", "qwen", now, log)
    root = tool_root("QWEN_DATA_DIR", ".qwen")
    if not os.environ.get("TOKEN_BURN_TOOLS_HOME") and os.environ.get("QWEN_RUNTIME_DIR") \
            and not os.environ.get("QWEN_DATA_DIR"):
        root = Path(os.environ["QWEN_RUNTIME_DIR"])
    ledger_dir = root / "usage"
    has_ledger = ledger_dir.is_dir() and any(ledger_dir.glob("token-usage-*.jsonl"))
    if has_ledger:
        # one line per model call, with the model and counts: the best source, so the chat logs are not read too
        def match(fn, dp):
            return fn.startswith("token-usage-") and fn.endswith(".jsonl")
        for path in _walk(ledger_dir, match, ctx.cutoff, depth=1):
            try:
                mtime = os.stat(path).st_mtime
                state = ctx.file_state(path)
                for off, obj in _iter_jsonl(path, state):
                    sid = obj.get("sessionId") if isinstance(obj.get("sessionId"), str) else Path(path).stem
                    _record_usage(ctx, path, state, off, obj, mtime, sid)
            except OSError:
                continue
    else:
        def match(fn, dp):
            return fn.endswith(".jsonl") and Path(dp).name == "chats"
        for path in _walk(root / "projects", match, ctx.cutoff, depth=3):
            try:
                mtime = os.stat(path).st_mtime
                state = ctx.file_state(path)
                for off, obj in _iter_jsonl(path, state):
                    if _qwen_accept(obj):
                        sid = obj.get("sessionId") if isinstance(obj.get("sessionId"), str) else Path(path).stem
                        _record_usage(ctx, path, state, off, obj, mtime, sid)
            except OSError:
                continue
        _chat_files(ctx, root / "tmp", _qwen_accept)     # versions 0.1 to 0.3 used Gemini CLI's layout
    ctx.save()
    return ctx.count


# ---- GitHub Copilot CLI -> copilot

def copilot(store, now, log):
    ctx = _Ctx(store, "copilot", "copilot", now, log)
    root = tool_root("COPILOT_HOME", ".copilot") / "session-state"

    def match(fn, dp):
        return fn == "events.jsonl"

    needles = (b"shutdown", b"assistant.message", b"model")
    for path in _walk(root, match, ctx.cutoff, depth=3):
        try:
            st_ = os.stat(path)
            state = ctx.file_state(path)
            sid = Path(path).parent.name
            pend = state.get("pend") if isinstance(state.get("pend"), dict) else {}
            for off, ev in _iter_jsonl(path, state, needles):
                typ = ev.get("type") if isinstance(ev.get("type"), str) else ""
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                t = _ts(ev, st_.st_mtime)
                if typ in ("session.start", "session.model_change", "session.resume"):
                    m = _model(data) or next((data[k] for k in ("selectedModel", "newModel")
                                              if isinstance(data.get(k), str)), "")
                    if m:
                        state["model"] = m
                elif typ == "session.shutdown":
                    metrics = data.get("modelMetrics")
                    done = False
                    if isinstance(metrics, dict):
                        for model, mm in metrics.items():
                            u = find_usage(mm if isinstance(mm, dict) else {}, add_reasoning=False)
                            if u and ctx.emit("tool|%s|%s|%s" % (_fid(path), off, model), sid, str(model), t, u):
                                done = True
                    if not done:
                        u = find_usage(data, add_reasoning=False)
                        if u:
                            ctx.emit("tool|%s|%s" % (_fid(path), off), sid, state.get("model", ""), t, u)
                    pend = {}       # the shutdown totals cover the messages counted so far
                elif typ == "assistant.message":
                    out = _int(data.get("outputTokens"))
                    if out:
                        m = state.get("model", "") or "unknown"
                        cur = pend.get(m) or [0, t]
                        pend[m] = [cur[0] + out, t]
            state["pend"] = pend
            if pend and ctx.now - st_.st_mtime > STALE_S:
                # a session that never wrote a shutdown event: count the output tokens its messages recorded
                for m, (out, t) in pend.items():
                    ctx.emit("tool|%s|pend|%s" % (_fid(path), m), sid, m, t, (0, out, 0, 0))
                state["pend"] = {}
        except OSError:
            continue
    ctx.save()
    return ctx.count


# ---- Grok Build CLI -> xai

def grok(store, now, log):
    ctx = _Ctx(store, "xai", "grok", now, log)
    root = tool_root("GROK_HOME", ".grok") / "sessions"

    def match(fn, dp):
        return fn in ("updates.jsonl", "signals.json")

    dirs = {}
    for path in _walk(root, match, ctx.cutoff, depth=4):
        dirs.setdefault(os.path.dirname(path), []).append(path)
    for d, paths in dirs.items():
        try:
            state = ctx.file_state(d)
            sid = os.path.basename(d)
            summary = _read_json(os.path.join(d, "summary.json"))
            model = _model(summary) if isinstance(summary, dict) else ""
            model = model or state.get("model", "") or "grok"
            state["model"] = model
            upd = os.path.join(d, "updates.jsonl")
            if upd in paths:
                mtime = os.stat(upd).st_mtime
                for off, obj in _iter_jsonl(upd, state):
                    got = find_usage(obj)
                    if got:
                        state["u"] = True
                        ctx.emit("tool|%s|%s" % (_fid(upd), off), sid, _model(obj, model), _ts(obj, mtime), got)
            sig = os.path.join(d, "signals.json")
            if sig in paths and not state.get("u"):
                _cumulative(ctx, state, sig, sid, model)
        except OSError:
            continue
    ctx.save()
    return ctx.count


def _cumulative(ctx, state, path, sid, model, add_reasoning=True, doc=None):
    """Add only what a cumulative counter file gained since the last pass (all of it the first time)."""
    s = _sig(path)
    if state.get("sig") == s:
        return
    state["sig"] = s
    doc = doc if doc is not None else _read_json(path)
    cur = find_usage(doc, add_reasoning) if isinstance(doc, dict) else None
    if not cur:
        return
    last = state.get("last") if isinstance(state.get("last"), list) and len(state["last"]) == 4 else [0, 0, 0, 0]
    if any(c < p for c, p in zip(cur, last)):
        last = [0, 0, 0, 0]         # the counters restarted
    delta = tuple(c - p for c, p in zip(cur, last))
    state["last"] = list(cur)
    ctx.emit(None, sid, model, s[1], delta)


# ---- Mistral Vibe -> mistral

def _vibe_files(fn, dp):
    return fn == "meta.json" or (fn.startswith("session_") and fn.endswith(".json")) or fn.endswith(".jsonl")


def vibe(store, now, log):
    ctx = _Ctx(store, "mistral", "vibe", now, log)
    root = tool_root("VIBE_HOME", ".vibe") / "logs" / "session"
    files = _walk(root, _vibe_files, ctx.cutoff, depth=4)
    cumulative_dirs = set()
    for path in files:
        if path.endswith(".json"):
            try:
                state = ctx.file_state(path)
                doc = _read_json(path)
                if not isinstance(doc, dict):
                    continue
                if find_usage(doc, add_reasoning=False):
                    cumulative_dirs.add(os.path.dirname(path))
                sid = doc.get("session_id") if isinstance(doc.get("session_id"), str) else (
                    os.path.basename(os.path.dirname(path)) if os.path.basename(path) == "meta.json"
                    else Path(path).stem)
                model = _model(doc) or _model(doc.get("config")) or _model(doc.get("metadata")) \
                    or state.get("model", "") or "mistral-vibe"
                state["model"] = model
                _cumulative(ctx, state, path, sid, model, add_reasoning=False, doc=doc)
            except OSError:
                continue
    for path in files:
        # per-reply journal events (newer versions), only where no cumulative counters exist beside them
        if not path.endswith(".jsonl") or os.path.dirname(path) in cumulative_dirs:
            continue
        try:
            mtime = os.stat(path).st_mtime
            state = ctx.file_state(path)
            sid = os.path.basename(os.path.dirname(path)) or Path(path).stem
            for off, obj in _iter_jsonl(path, state):
                got = find_usage(obj, add_reasoning=False)
                if got:
                    ctx.emit("tool|%s|%s" % (_fid(path), off), sid, _model(obj) or "mistral-vibe",
                             _ts(obj, mtime), got)
        except OSError:
            continue
    ctx.save()
    return ctx.count


# ---- entry point used by collect.py

READERS = {
    "google": (gemini,),
    "qwen": (qwen,),
    "copilot": (copilot,),
    "xai": (grok,),
    "mistral": (vibe,),
}


def collect_platform(plat, store, now, log, backfill=False):
    """Run the standalone tool readers for one provider platform. A broken reader never stops the others."""
    requests = 0
    for fn in READERS.get(plat, ()):
        try:
            requests += fn(store, now, log) or 0
        except Exception as e:      # never raise into the scheduler
            if log:
                log.error("%s tool reader failed: %s", fn.__name__, type(e).__name__)
    return {"status": "ok", "meta": {"requests": requests} if requests else {}}
