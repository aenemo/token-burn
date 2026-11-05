"""Multi-computer sync facade: export this computer, read the others, merge, list the computers.

apply() is the only entry point the collector uses. It never raises; if anything goes wrong the
local payloads are returned untouched with a status the dashboard can show.
"""
import copy
import os

from collector import aggregate, util
from collector.sync import config, export, merge, peers, validate

__all__ = ["apply"]


def _export_status_text(store, folder, status):
    """(sync status text, self state) for a maybe_write result."""
    if status in (export.WRITTEN, export.UNCHANGED, export.THROTTLED):
        return "ok", "active"
    if status == export.CONFLICT:
        return store.kv_get(export.KV_CONFLICT) or "conflicting computer ID", "active"
    if status == export.REMOVED:
        return export.REMOVED_MESSAGE, "removed"
    if status == export.FOLDER_MISSING:
        return "shared folder not found: %s" % folder, "active"
    return status, "active"


def _messages(scan, rows_names):
    out = list(scan.messages)
    for item in scan:
        if item["status"] in ("", "ok", "removed"):
            continue
        out.append("%s: %s" % (rows_names[item["id"]], item["status"]))
    return out


def _apply(store, payloads, now, log, data_dir):
    cfg = config.load(data_dir)
    s = config.sync_settings(cfg)
    if not s["enabled"]:
        return payloads, {"enabled": False}, None
    folder = s["folder"]
    self_id = s["machine_id"]
    names = [n for n in payloads if n in validate.PLATFORMS]
    local = copy.deepcopy(payloads)
    plan_samples = {n: aggregate.raw_plan_samples(store, n, now) for n in names}
    adopted = export.load_adopted(data_dir, self_id, now)     # before build_export may delete it
    doc = export.build_export(local, plan_samples, cfg, now, data_dir=data_dir)
    self_fp = doc.get("host_fp") or None
    exp = export.maybe_write(store, folder, doc, now, adopted=s["adopted"])
    status_text, self_state = _export_status_text(store, folder, exp)
    scan = peers.scan(folder, self_id, now, data_dir)

    merged = dict(payloads)
    for name in names:
        merged[name] = merge.merge_platform(
            name, payloads[name], scan, now, self_id, plan_samples[name], self_fp=self_fp,
            adopted=adopted["platforms"].get(name) if adopted else None)

    control = peers.read_control(folder, self_id) if os.path.isdir(str(folder)) else None
    if control and control["state"] in ("retired", "removed"):
        self_state = control["state"]
    last_write = store.kv_get(export.KV_WRITE)
    last_export = float(last_write) if isinstance(last_write, (int, float)) else None
    self_info = {"id": self_id,
                 "name": (control or {}).get("display_name") or doc["display_name"],
                 "os": doc["os"], "hostname": doc["hostname"], "app_version": doc["app_version"],
                 "last_seen": now if exp == export.WRITTEN or last_export is None else last_export,
                 "status": status_text, "state": self_state}
    rows = merge.machine_rows(self_info, payloads, scan, now, self_fp=self_fp)
    row_names = {r["id"]: r["name"] for r in rows}
    sync_block = {"enabled": True, "status": status_text,
                  "folder_found": os.path.isdir(str(folder)),
                  "last_export": util.iso(last_export) if last_export is not None else None,
                  "ignored_files": scan.ignored_files,
                  "messages": _messages(scan, row_names)}
    if self_state == "removed" and export.REMOVED_MESSAGE not in sync_block["messages"]:
        sync_block["messages"].append(export.REMOVED_MESSAGE)
    log.info("sync %s export=%s computers=%d ignored=%d", status_text, exp, len(rows),
             scan.ignored_files)
    return merged, sync_block, rows


def apply(store, payloads, now, log, data_dir=None):
    """Return (payloads, sync_block, machines_list). Never raises.

    Sync off: (payloads, {"enabled": False}, None). On any error: the payloads unchanged,
    {"enabled": True, "status": "sync failed (<ExcName>)"} and None.
    """
    data_dir = util.DATA_DIR if data_dir is None else data_dir
    try:
        return _apply(store, payloads, now, log, data_dir)
    except Exception as e:
        try:
            log.error("sync failed: %s", type(e).__name__)
        except Exception:
            pass
        return payloads, {"enabled": True, "status": "sync failed (%s)" % type(e).__name__}, None
