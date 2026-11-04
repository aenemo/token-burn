"""Idempotent registration of the Claude and Cursor hooks (backs up files, keeps other entries)."""
import datetime as dt
import json
import re
import shutil
from pathlib import Path

from ccu import paths

CURSOR_EVENTS = ("subagentStart", "subagentStop")
# Ours: "... ccu hook claude|cursor" (module or frozen exe). The original app's hooks are left alone so both can run side by side.
_OURS = re.compile(r"(^|[\s/\\\"'])ccu[\w.\-]*[\"']?\s+hook\s+(claude|cursor)\b")
_LEGACY = ()


def is_ours(command):
    c = str(command or "")
    return bool(_OURS.search(c)) or any(m in c for m in _LEGACY)


def claude_settings_path():
    return paths.claude_dir() / "settings.json"


def read_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        raise ValueError("%s is not valid JSON; not touching it." % path)
    if not isinstance(obj, dict):
        raise ValueError("%s is not a JSON object; not touching it." % path)
    return obj


def backup(path):
    path = Path(path)
    if not path.is_file():
        return None
    bak = path.with_name(path.name + ".bak-ccu-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(str(path), str(bak))
    return bak


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _strip_claude(entries):
    """Remove our hook commands from Claude-style entries. Returns (kept, removed, exact_cmds)."""
    kept, removed, cmds = [], 0, []
    for entry in entries:
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(hooks, list):
            kept.append(entry)
            continue
        keep = [h for h in hooks if not (isinstance(h, dict) and is_ours(h.get("command")))]
        cmds += [h.get("command") for h in hooks if isinstance(h, dict) and is_ours(h.get("command"))]
        removed += len(hooks) - len(keep)
        if len(keep) == len(hooks):
            kept.append(entry)
        elif keep:
            e = dict(entry)
            e["hooks"] = keep
            kept.append(e)
    return kept, removed, cmds


def install_claude(command=None, settings=None):
    command = command or paths.hook_command("claude")
    settings = Path(settings) if settings else claude_settings_path()
    cfg = read_json(settings) or {}
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    cur = hooks.get("SubagentStop")
    cur = cur if isinstance(cur, list) else []
    kept, removed, cmds = _strip_claude(cur)
    if removed == 1 and cmds == [command]:
        return "already installed"
    backup(settings)
    kept.append({"hooks": [{"type": "command", "command": command}]})
    hooks["SubagentStop"] = kept
    cfg["hooks"] = hooks
    write_json(settings, cfg)
    return "installed"


def uninstall_claude(settings=None):
    settings = Path(settings) if settings else claude_settings_path()
    cfg = read_json(settings)
    hooks = cfg.get("hooks") if cfg else None
    cur = hooks.get("SubagentStop") if isinstance(hooks, dict) else None
    if not isinstance(cur, list):
        return "not present"
    kept, removed, _ = _strip_claude(cur)
    if not removed:
        return "not present"
    backup(settings)
    if kept:
        hooks["SubagentStop"] = kept
    else:
        del hooks["SubagentStop"]
        if not hooks:
            del cfg["hooks"]
    write_json(settings, cfg)
    return "removed"


def install_cursor(command=None, hooks_json=None):
    command = command or paths.hook_command("cursor")
    hooks_json = Path(hooks_json) if hooks_json else paths.cursor_hooks_path()
    cfg = read_json(hooks_json) or {}
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    good = True
    for ev in CURSOR_EVENTS:
        cur = hooks.get(ev)
        ours = [h for h in (cur if isinstance(cur, list) else [])
                if isinstance(h, dict) and is_ours(h.get("command"))]
        if len(ours) != 1 or ours[0].get("command") != command:
            good = False
    if good and cfg.get("version") is not None:
        return "already installed"
    backup(hooks_json)
    cfg.setdefault("version", 1)
    for ev in CURSOR_EVENTS:
        cur = hooks.get(ev)
        cur = cur if isinstance(cur, list) else []
        kept = [h for h in cur if not (isinstance(h, dict) and is_ours(h.get("command")))]
        kept.append({"command": command, "timeout": 15})
        hooks[ev] = kept
    cfg["hooks"] = hooks
    write_json(hooks_json, cfg)
    return "installed"


def uninstall_cursor(hooks_json=None):
    hooks_json = Path(hooks_json) if hooks_json else paths.cursor_hooks_path()
    cfg = read_json(hooks_json)
    hooks = cfg.get("hooks") if cfg else None
    if not isinstance(hooks, dict):
        return "not present"
    changed = False
    for ev in CURSOR_EVENTS:
        cur = hooks.get(ev)
        if not isinstance(cur, list):
            continue
        kept = [h for h in cur if not (isinstance(h, dict) and is_ours(h.get("command")))]
        if len(kept) != len(cur):
            if not changed:
                backup(hooks_json)
            changed = True
            if kept:
                hooks[ev] = kept
            else:
                del hooks[ev]
    if not changed:
        return "not present"
    write_json(hooks_json, cfg)
    return "removed"


def status():
    """Dict of {'claude': bool, 'cursor': bool}: whether our hooks are registered."""
    out = {}
    try:
        cfg = read_json(claude_settings_path()) or {}
        entries = (cfg.get("hooks") or {}).get("SubagentStop") or []
        out["claude"] = _strip_claude(entries)[1] > 0
    except Exception:
        out["claude"] = False
    try:
        cfg = read_json(paths.cursor_hooks_path()) or {}
        hooks = cfg.get("hooks") or {}
        out["cursor"] = all(
            any(isinstance(h, dict) and is_ours(h.get("command")) for h in (hooks.get(ev) or []))
            for ev in CURSOR_EVENTS)
    except Exception:
        out["cursor"] = False
    return out
