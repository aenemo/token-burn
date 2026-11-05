"""Size caps and strict validation for machine and control files read from the shared folder.

Nothing here evaluates, imports or executes anything from the files: they are decompressed with a
size limit, parsed as JSON, and every field is type checked, clamped and copied into a fresh dict.
"""
import gzip
import io
import json
import math
import os
import re
import stat
import unicodedata
import zlib

from collector.sync import config
from collector.util import MINUTES, iso, parse_ts

MAX_COMPRESSED = 16 * 1024 * 1024
MAX_PLAIN = 64 * 1024 * 1024
MAX_CONTROL = 4096
CHUNK = 1024 * 1024

PLATFORMS = ("claude", "cursor", "opencode")
SERIES_FIELDS = ("input", "output", "cache", "cost", "requests", "sub_input", "sub_output",
                 "sub_cache", "sub_cost", "cache_read", "sub_cache_read")
_REQUIRED = ("input", "output", "cache", "cost", "requests")
_COST_FIELDS = ("cost", "sub_cost")
MAX_POINTS = 50000
MAX_SERIES = 300
MAX_RUNS = 5000
MAX_SAMPLES = 20000
MAX_POOLS = 20
MAX_TOKENS = 10 ** 12
MAX_COST = 1e6
MAX_REQUESTS = 10 ** 7
SKEW_S = 600
CONTROL_STATES = ("active", "retired", "removed")

HOST_FP_RE = re.compile(r"[0-9a-f]{16}")

DAMAGED = "incomplete or damaged (still syncing?)"


class ReadError(Exception):
    """A file could not be read. .reason is a short message safe to show."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


def strip_control(s, cap):
    """Remove control characters and trim to cap characters."""
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    return s.strip()[:cap]


# ---- reading

def read_raw_capped(path, max_compressed=MAX_COMPRESSED):
    """Bytes of a regular, non-symlink file no larger than max_compressed."""
    try:
        st = os.lstat(path)
    except OSError:
        raise ReadError("not downloaded yet")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ReadError("not a regular file")
    if st.st_size > max_compressed:
        raise ReadError("file is too large")
    try:
        with open(path, "rb") as f:
            data = f.read(max_compressed + 1)
    except OSError:
        raise ReadError("not downloaded yet")
    if len(data) > max_compressed:
        raise ReadError("file is too large")
    return data


def decompress_capped(raw, max_plain=MAX_PLAIN):
    """Decompress gzip bytes in 1 MiB chunks, aborting above max_plain bytes."""
    out = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as g:
            while True:
                chunk = g.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_plain:
                    raise ReadError("file is too large when unpacked")
                out.append(chunk)
    except ReadError:
        raise
    except (EOFError, OSError, zlib.error):  # BadGzipFile is an OSError
        raise ReadError(DAMAGED)
    return b"".join(out)


def read_gz_capped(path, max_compressed=MAX_COMPRESSED, max_plain=MAX_PLAIN):
    """Decompressed bytes of a gzip file; raises ReadError with a displayable reason."""
    return decompress_capped(read_raw_capped(path, max_compressed), max_plain)


def load_json_bytes(data):
    """Top-level JSON object from bytes, or ReadError."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError, MemoryError):
        raise ReadError(DAMAGED)
    if not isinstance(doc, dict):
        raise ReadError("not a Token Burn file")
    return doc


# ---- type checkers

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def as_int(v):
    """int for an int or an integral finite float, else None."""
    if is_int(v):
        return v
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return int(v)
    return None


def as_float(v):
    """Finite float for a real number (not bool), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _str(v, cap, default=""):
    return strip_control(v, cap) if isinstance(v, str) else default


def _clamp(v, hi):
    return max(0, min(v, hi))


# ---- series

def _clean_series(raw, lo_min, hi_min):
    """One series dict, or None when malformed.

    Out-of-window minutes are dropped, not rejected. Negative or oversized numbers are clamped;
    values that are not numbers at all (text, NaN) reject the series.
    """
    if not isinstance(raw, dict):
        return None
    model = _str(raw.get("model"), 120)
    effort = _str(raw.get("effort"), 120)
    key = _str(raw.get("key"), 120) or (("%s-%s" % (model, effort)) if effort else model)
    m = raw.get("m")
    if not model or not isinstance(m, list) or len(m) > MAX_POINTS:
        return None
    n = len(m)
    cols = {}
    for f in SERIES_FIELDS:
        v = raw.get(f)
        if v is None and f not in _REQUIRED:
            v = [0] * n
        if not isinstance(v, list) or len(v) != n:
            return None
        cols[f] = v
    last = None
    keep = []
    mins = []
    for i, mv in enumerate(m):
        mv = as_int(mv)
        if mv is None or (last is not None and mv <= last):
            return None
        last = mv
        mins.append(mv)
        if lo_min <= mv <= hi_min:
            keep.append(i)
    out = {"key": key, "model": model, "effort": effort, "m": [mins[i] for i in keep]}
    for f in SERIES_FIELDS:
        vals = []
        for i in keep:
            v = cols[f][i]
            if f in _COST_FIELDS:
                v = as_float(v)
                if v is None:
                    return None
                v = round(max(0.0, min(v, MAX_COST)), 6)
            else:
                v = as_int(v)
                if v is None:
                    return None
                v = _clamp(v, MAX_REQUESTS if f == "requests" else MAX_TOKENS)
            vals.append(v)
        out[f] = vals
    # each sub_* is a share of its total and cannot exceed it
    for sub, tot in (("sub_input", "input"), ("sub_output", "output"), ("sub_cache", "cache"),
                     ("sub_cost", "cost"), ("cache_read", "cache"), ("sub_cache_read", "cache_read")):
        out[sub] = [min(a, b) for a, b in zip(out[sub], out[tot])]
    out["sub_cache_read"] = [min(a, b) for a, b in zip(out["sub_cache_read"], out["sub_cache"])]
    return out


def _clean_runs(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for r in raw[:MAX_RUNS]:
        if not isinstance(r, dict):
            continue
        aid = _str(r.get("agent_id"), 64)
        ts = parse_ts(r.get("t")) if isinstance(r.get("t"), str) else None
        if not aid or ts is None:
            continue
        run = {"agent_id": aid, "agent_name": _str(r.get("agent_name"), 80),
               "agent_type": _str(r.get("agent_type"), 40), "model": _str(r.get("model"), 120),
               "effort": _str(r.get("effort"), 120), "t": iso(ts),
               "status": _str(r.get("status"), 120)}
        for f in ("input", "output", "cache"):
            v = as_int(r.get(f))
            run[f] = None if v is None else _clamp(v, MAX_TOKENS)
        c = as_float(r.get("cost"))
        run["cost"] = None if c is None else round(max(0.0, min(c, MAX_COST)), 6)
        run["cost_estimated"] = r.get("cost_estimated") is True
        out.append(run)
    return out


def _clean_plan(raw):
    out = {"last_lookup": None, "samples": []}
    if not isinstance(raw, dict):
        return out
    ll = raw.get("last_lookup")
    if isinstance(ll, str) and parse_ts(ll) is not None:
        out["last_lookup"] = strip_control(ll, 40)
    samples = raw.get("samples")
    if isinstance(samples, list):
        for s in samples[-MAX_SAMPLES:]:
            if not isinstance(s, (list, tuple)) or len(s) != 2:
                continue
            t = as_float(s[0])
            if t is None or not isinstance(s[1], dict):
                continue
            pools = {}
            for pid, pv in list(s[1].items())[:MAX_POOLS]:
                if not isinstance(pid, str) or not isinstance(pv, dict):
                    continue
                pct = as_float(pv.get("pct"))
                if pct is None or pct < 0 or pct > 1000:
                    continue
                pool = {"pct": pct}
                ra = pv.get("resets_at")
                if isinstance(ra, str) and len(ra) <= 40:
                    pool["resets_at"] = strip_control(ra, 40)
                pools[strip_control(pid, 60)] = pool
            if pools:
                out["samples"].append([t, pools])
    return out


def clean_platform(raw, lo_min, hi_min):
    """Whitelisted copy of one platform block."""
    out = {"installed": raw.get("installed") is not False,
           "status": _str(raw.get("status"), 120, "ok"),
           "series": [], "subagent_runs": _clean_runs(raw.get("subagent_runs")),
           "plan": _clean_plan(raw.get("plan"))}
    series = raw.get("series")
    if isinstance(series, list):
        for s in series[:MAX_SERIES]:
            cs = _clean_series(s, lo_min, hi_min)
            if cs is not None:
                out["series"].append(cs)
    return out


# ---- documents

def parse_machine(doc, expect_id, now):
    """(clean_doc, status). clean_doc is None when the file must be rejected; status says why.

    On success status is "ok", or "clock is ahead by N min" when written_at is in the future.
    clean_doc["last_seen"] is epoch seconds, clamped to now.
    """
    if not isinstance(doc, dict) or doc.get("format") != "token-burn-machine":
        return None, "not a Token Burn file"
    schema = as_int(doc.get("schema"))
    if schema is None:
        return None, "not a Token Burn file"
    if schema > 1:
        return None, "made by a newer Token Burn; update this computer"
    if schema != 1:
        return None, "not a Token Burn file"
    if not config.valid_machine_id(expect_id) or doc.get("machine_id") != expect_id:
        return None, "computer ID does not match the file name"
    written = parse_ts(doc.get("written_at")) if isinstance(doc.get("written_at"), str) else None
    if written is None:
        return None, "missing write time"
    status = "ok"
    last_seen = written
    now_min = int(now // 60)
    hi = now_min + 5
    if written > now + SKEW_S:
        status = "clock is ahead by %d min" % int((written - now) // 60)
        last_seen = now
        hi = now_min
    lo = now_min - MINUTES + 1
    out = {"format": "token-burn-machine", "schema": 1, "machine_id": expect_id,
           "display_name": _str(doc.get("display_name"), config.NAME_MAX),
           "hostname": _str(doc.get("hostname"), 64), "os": _str(doc.get("os"), 40),
           "app_version": _str(doc.get("app_version"), 20),
           "written_at": iso(written), "last_seen": last_seen, "platforms": {}}
    fp = doc.get("host_fp")
    out["host_fp"] = fp if isinstance(fp, str) and HOST_FP_RE.fullmatch(fp) else ""   # optional
    plats = doc.get("platforms")
    if isinstance(plats, dict):
        for name in PLATFORMS:
            raw = plats.get(name)
            if isinstance(raw, dict):
                out["platforms"][name] = clean_platform(raw, lo, hi)
    return out, status


def parse_control(doc, expect_id):
    """Clean control dict, or None."""
    if not isinstance(doc, dict) or doc.get("format") != "token-burn-control":
        return None
    if as_int(doc.get("schema")) != 1 or doc.get("machine_id") != expect_id:
        return None
    state = doc.get("state")
    if state not in CONTROL_STATES:
        return None
    by = doc.get("by")
    at = parse_ts(doc.get("at")) if isinstance(doc.get("at"), str) else None
    return {"format": "token-burn-control", "schema": 1, "machine_id": expect_id, "state": state,
            "display_name": _str(doc.get("display_name"), config.NAME_MAX),
            "by": by if config.valid_machine_id(by) else "",
            "at": iso(at) if at is not None else ""}
