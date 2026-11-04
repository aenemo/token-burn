"""Claude plan usage lookup (OAuth usage endpoint). Never logs tokens."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from collector import util

USER_AGENT = "claude-token-usage-graphs/1"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _http_json(url, headers=None, body=None):
    data = None
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


UNAVAILABLE = "sign-in unavailable, run claude"
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _keychain_credentials():
    """macOS only, read-only. Returns the parsed credentials dict or None (fails soft)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout.strip())
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("claudeAiOauth"), dict):
        return data
    return None


def resolve_credentials(cred_path):
    """Return (source, full, oa) with source in env/keychain/file, or (None, None, None).

    Order: TOKEN_BURN_CLAUDE_TOKEN (access token only) -> macOS keychain -> credentials file.
    """
    tok = os.environ.get("TOKEN_BURN_CLAUDE_TOKEN", "").strip()
    if tok:
        oa = {"accessToken": tok}
        return "env", {"claudeAiOauth": oa}, oa
    if sys.platform == "darwin":
        full = _keychain_credentials()
        if full:
            return "keychain", full, full["claudeAiOauth"]
    full = _load_json(cred_path, None)
    if isinstance(full, dict) and isinstance(full.get("claudeAiOauth"), dict):
        return "file", full, full["claudeAiOauth"]
    return None, None, None


def _refresh(cred_path, oa):
    rt = oa.get("refreshToken")
    if not rt:
        return None
    resp = _http_json(TOKEN_URL, body={"grant_type": "refresh_token", "refresh_token": rt,
                                       "client_id": CLIENT_ID})
    at = resp.get("access_token")
    if not at:
        return None
    exp = int((time.time() + float(resp.get("expires_in") or 3600)) * 1000)
    # re-read just before writing so other fields (and concurrent changes) survive
    full = _load_json(cred_path, None)
    if not isinstance(full, dict):
        return None
    cur = full.setdefault("claudeAiOauth", {})
    cur["accessToken"] = at
    if resp.get("refresh_token"):
        cur["refreshToken"] = resp["refresh_token"]
    cur["expiresAt"] = exp
    mode = None
    if os.name == "posix":
        try:
            mode = os.stat(str(cred_path)).st_mode & 0o777
        except OSError:
            mode = 0o600
    util.atomic_write_json(cred_path, full, mode=mode)
    return at


def _norm_reset(v):
    if v is None or v == "":
        return None
    ts = util.parse_ts(v)
    return util.iso(ts) if ts is not None else None


def fetch(log=None):
    """Return (status, pools or None). pools: id -> {pct, resets_at}."""
    cred_path = util.claude_dir() / ".credentials.json"
    source, full, oa = resolve_credentials(cred_path)
    if source is None:
        return "no sign-in found, run claude once", None
    # rotated tokens are only ever written back to the file, never on macOS or for env/keychain
    can_refresh = source == "file" and sys.platform != "darwin"
    refresh_msg = "sign-in expired, run claude once" if can_refresh else UNAVAILABLE
    token = oa.get("accessToken")
    try:
        exp_ms = float(oa.get("expiresAt") or 0)
    except (TypeError, ValueError):
        exp_ms = 0
    refreshed = False
    resp = None
    try:
        if source != "env" and (not token or exp_ms <= (time.time() + 60) * 1000):
            if not can_refresh:
                return UNAVAILABLE, None
            token = _refresh(cred_path, oa)
            refreshed = True
            if not token:
                return refresh_msg, None
        for _attempt in range(2):
            try:
                resp = _http_json(USAGE_URL, headers={
                    "Authorization": "Bearer " + token,
                    "anthropic-beta": "oauth-2025-04-20"})
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed and can_refresh:
                    token = _refresh(cred_path, oa)
                    refreshed = True
                    if not token:
                        return refresh_msg, None
                    continue
                raise
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return refresh_msg, None
        if e.code == 429:
            return "plan API rate limited, will retry", None
        return "plan API error (HTTP %d)" % e.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return "plan API unreachable (offline?)", None
    except Exception as e:
        return "plan check failed (%s)" % type(e).__name__, None
    pools = {}
    if isinstance(resp, dict):
        for k, v in resp.items():
            if isinstance(v, dict) and "utilization" in v:
                u = v.get("utilization")
                if isinstance(u, (int, float)) and not isinstance(u, bool):
                    pools[k] = {"pct": float(u), "resets_at": _norm_reset(v.get("resets_at"))}
    if not pools:
        return "plan API returned no pools", None
    return "ok", pools
