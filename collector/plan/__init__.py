"""Shared plan lookup throttle."""
from collector.util import PLAN_INTERVAL_S, iso


def maybe_lookup(store, platform, fetch_fn, now, log):
    """Call fetch_fn(log) -> (status, pools) at most once per PLAN_INTERVAL_S per platform.

    A failure leaves last_ok alone, so it retries on the next run.
    """
    key = "plan:" + platform
    st = store.kv_get(key, None)
    if not isinstance(st, dict):
        st = {}
    last_ok = st.get("last_ok")
    if last_ok is not None and 0 <= now - last_ok < PLAN_INTERVAL_S:
        status = st.get("status") or "ok"
    else:
        try:
            status, pools = fetch_fn(log)
        except Exception as e:
            status, pools = "plan check failed (%s)" % type(e).__name__, None
        st["last_attempt"] = now
        if pools:
            store.add_plan_sample(platform, now, pools)
            last_ok = now
            st["last_ok"] = now
            if not status:
                status = "ok"
        st["status"] = status
        store.kv_set(key, st)
        store.commit()
    return {"status": status,
            "last_lookup": iso(last_ok) if last_ok is not None else None,
            "next_lookup": iso(last_ok + PLAN_INTERVAL_S) if last_ok is not None else None}
