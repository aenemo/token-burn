import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, util
from collector.aggregate import build_platform_payload, build_usage_json
from collector.schema import UsageRecord
from collector.store import Store
from collector.sync import merge, validate
from collector.util import MINUTES, iso

NOW = 1_800_000_000.0           # divisible by 60
NOW_MIN = int(NOW // 60)
START_MIN = NOW_MIN - MINUTES + 1
A = "a" * 32
B = "b" * 32
C = "c" * 32
SELF = "5" * 32
FP_SELF = "5a" * 8
FP_A = "aa" * 8
FP_B = "bb" * 8
INFO = {"status": "ok", "meta": {}, "plan": {"status": "ok", "last_lookup": "2026-01-01T00:00:00Z",
                                             "next_lookup": "2026-01-01T00:10:00Z"}}
NOT_INSTALLED = {"installed": False, "status": "not installed"}


def at(idx):
    return (START_MIN + idx) * 60.0 + 5


def rec(idx, platform="claude", inp=0, out=0, cr=0, cw=0, **kw):
    d = dict(platform=platform, agent_kind="primary", agent_id="", session_id="s1", model="m",
             effort_level="", input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
             cache_write_tokens=cw, cost_usd=0.0, timestamp=at(idx))
    d.update(kw)
    return UsageRecord(**d)


def raw_series(key, idxs, inp=0, out=0, cache=0, cost=0.0, reqs=1, model=None, **extra):
    """A machine-file series with absolute minutes START_MIN + idx."""
    n = len(idxs)
    s = {"key": key, "model": model or key, "effort": "", "m": [START_MIN + i for i in idxs],
         "input": [inp] * n, "output": [out] * n, "cache": [cache] * n, "cost": [cost] * n,
         "requests": [reqs] * n}
    for f in ("sub_input", "sub_output", "sub_cache", "sub_cost", "cache_read", "sub_cache_read"):
        s[f] = [0] * n
    s.update(extra)
    return s


def plat(series=(), runs=(), samples=(), last_lookup=None, installed=True):
    return {"installed": installed, "status": "ok", "series": list(series),
            "subagent_runs": list(runs),
            "plan": {"last_lookup": last_lookup, "samples": [list(x) for x in samples]}}


def peer(mid, platforms, name="Peer", control=None, status="ok", written=NOW - 60, fp=None):
    doc = {"format": "token-burn-machine", "schema": 1, "machine_id": mid, "display_name": name,
           "hostname": name.upper(), "os": "Linux", "app_version": "0.2.0", "written_at": iso(written),
           "platforms": platforms}
    if fp:
        doc["host_fp"] = fp
    clean, _ = validate.parse_machine(doc, mid, NOW)
    return {"id": mid, "doc": clean, "status": status, "from_cache": False, "control": control,
            "mtime": written}


def ctl(state, name=""):
    return {"state": state, "display_name": name, "by": "", "at": ""}


def cells(series_list):
    """{(key, t): [input, output, cache, cost, requests]} from payload series."""
    out = {}
    for s in series_list:
        for i, t in enumerate(s["t"]):
            out[(s["key"], t)] = [s["input"][i], s["output"][i], s["cache"][i], s["cost"][i],
                                  s["requests"][i]]
    return out


def run_rec(aid, t, tokens=True):
    return {"agent_id": aid, "agent_name": "n", "agent_type": "explore", "model": "m", "effort": "",
            "t": iso(t), "status": "completed", "input": 1 if tokens else None,
            "output": 2 if tokens else None, "cache": 3 if tokens else None,
            "cost": 0.1 if tokens else None, "cost_estimated": False}


class MergeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.store.close)

    def local(self, name="claude", records=()):
        for r in records:
            self.store.add_usage(r)
        p = build_platform_payload(self.store, name, NOW, INFO)
        p["installed"] = True
        return p

    def merge(self, name, local, peers, samples=None):
        return merge.merge_platform(name, local, peers, NOW, SELF, samples)


class ClaudeOpencodeAdd(MergeBase):
    def test_claude_adds_across_three_computers(self):
        local = self.local(records=[rec(10, inp=100, out=10, cr=50, cost_usd=1.0, requests=2)])
        pa = peer(A, {"claude": plat([raw_series("m", [10, 20], inp=5, out=1, cache=2, cost=0.5)])})
        pb = peer(B, {"claude": plat([raw_series("m", [20], inp=7, out=2, cache=3, cost=0.25)])})
        out = self.merge("claude", local, [pa, pb])
        c = cells(out["series"])
        self.assertEqual(c[("m", 10)][:2], [105, 11])
        self.assertEqual(c[("m", 20)][:3], [12, 3, 5])
        self.assertAlmostEqual(c[("m", 20)][3], 0.75)
        self.assertEqual(c[("m", 10)][4], 3)
        self.assertEqual(out["meta"]["machines"], 3)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(set(out["machines"]), {SELF, A, B})
        self.assertEqual(cells(out["machines"][A]["series"])[("m", 10)][0], 5)
        self.assertEqual(cells(out["machines"][SELF]["series"])[("m", 10)][0], 100)

    def test_input_payload_not_modified(self):
        local = self.local(records=[rec(10, inp=100)])
        before = json.dumps(local, sort_keys=True)
        self.merge("claude", local, [peer(A, {"claude": plat([raw_series("m", [10], inp=5)])})])
        self.assertEqual(json.dumps(local, sort_keys=True), before)

    def test_opencode_adds_and_cost_reported_false_when_peer_cost_zero(self):
        local = self.local("opencode", [rec(5, "opencode", inp=10, cost_usd=0.1)])
        self.assertTrue(local["meta"]["cost_reported"])
        pa = peer(A, {"opencode": plat([raw_series("m", [5], inp=20, cost=0.0)])})
        out = self.merge("opencode", local, [pa])
        self.assertEqual(cells(out["series"])[("m", 5)][0], 30)
        self.assertFalse(out["meta"]["cost_reported"])

    def test_breakdown_only_with_two_machines_with_data(self):
        idle = self.local("opencode")
        out = self.merge("opencode", idle, [peer(B, {"opencode": plat([raw_series("m", [1], inp=1)])})])
        self.assertEqual(set(out["machines"]), {B})      # one computer with data, still a breakdown
        self.assertEqual(out["meta"]["machines"], 1)
        local = self.local(records=[rec(10, inp=100)])
        out = self.merge("claude", local, [peer(B, {"claude": plat([raw_series("m", [10], inp=1)])})])
        self.assertEqual(len(out["machines"]), 2)

    def test_t_rebasing_and_window_edges(self):
        local = self.local(records=[rec(0, inp=1)])
        pa = peer(A, {"claude": plat([raw_series("m", [0, 7, MINUTES - 1], inp=2)])})
        out = self.merge("claude", local, [pa])
        self.assertEqual(out["series"][0]["t"], [0, 7, MINUTES - 1])
        self.assertEqual(out["series"][0]["input"], [3, 2, 2])
        self.assertEqual(out["start"], local["start"])

    def test_series_sorted_by_total(self):
        local = self.local(records=[rec(1, inp=10, model="small")])
        pa = peer(A, {"claude": plat([raw_series("big", [1], inp=1000)])})
        out = self.merge("claude", local, [pa])
        self.assertEqual([s["key"] for s in out["series"]], ["big", "small"])

    def test_runs_dedup_and_window(self):
        run = run_rec("x1", NOW - 100)
        old = run_rec("old", NOW - 40 * 86400)
        newer = run_rec("x2", NOW - 10)
        local = self.local(records=[rec(1, inp=1)])
        pa = peer(A, {"claude": plat([raw_series("m", [1], inp=1)], runs=[run, run, old, newer])})
        out = self.merge("claude", local, [pa])
        self.assertEqual([r["agent_id"] for r in out["subagent_runs"]], ["x2", "x1"])
        self.assertEqual(out["subagent_runs"][0]["session_id"], "")

    def test_local_not_installed_peers_have_data(self):
        pa = peer(A, {"claude": plat([raw_series("m", [3], inp=9)], runs=[run_rec("r", NOW - 5)],
                                     last_lookup="2026-01-02T00:00:00Z",
                                     samples=[(NOW - 100, {"five_hour": {"pct": 12.0}})])})
        out = self.merge("claude", NOT_INSTALLED, [pa])
        self.assertTrue(out["installed"])
        self.assertEqual(out["status"], "ok (from other computers)")
        self.assertEqual(out["series"][0]["t"], [3])
        self.assertEqual(out["plan"]["status"], "ok (from other computers)")
        self.assertEqual(out["plan"]["last_lookup"], "2026-01-02T00:00:00Z")
        self.assertEqual(out["plan"]["pools"][0]["now"], 12.0)
        self.assertTrue(out["meta"]["subagent_tokens_attributed"])
        self.assertEqual(out["start"], iso(START_MIN * 60))

    def test_not_installed_and_nothing_from_peers_unchanged(self):
        self.assertIs(self.merge("claude", NOT_INSTALLED, [peer(A, {"cursor": plat()})]), NOT_INSTALLED)
        self.assertIs(self.merge("claude", NOT_INSTALLED, []), NOT_INSTALLED)
        self.assertIs(self.merge("claude", NOT_INSTALLED,
                                 [peer(A, {"claude": plat(installed=False)})]), NOT_INSTALLED)

    def test_retired_counts_removed_excluded(self):
        local = self.local(records=[rec(1, inp=1)])
        ret = peer(A, {"claude": plat([raw_series("m", [1], inp=10)])}, control=ctl("retired"))
        rem = peer(B, {"claude": plat([raw_series("m", [1], inp=100)])}, control=ctl("removed"))
        nodoc = {"id": C, "doc": None, "status": "not downloaded yet", "from_cache": False,
                 "control": None, "mtime": 0}
        out = self.merge("claude", local, [ret, rem, nodoc])
        self.assertEqual(cells(out["series"])[("m", 1)][0], 11)
        self.assertNotIn(B, out["machines"])


class SamePhysicalComputer(MergeBase):
    """Finding 4 and 5: ids that share a host_fp read the same logs, so combine by max, not add."""

    def merge_fp(self, name, local, peers, adopted=None):
        return merge.merge_platform(name, local, peers, NOW, SELF, None, self_fp=FP_SELF, adopted=adopted)

    def test_old_id_of_this_computer_is_folded_by_max(self):
        local = self.local(records=[rec(10, inp=100), rec(11, inp=7)])
        old = peer(A, {"claude": plat([raw_series("m", [10, 12], inp=100)])}, fp=FP_SELF)   # same logs, old id
        out = self.merge_fp("claude", local, [old])
        c = cells(out["series"])
        self.assertEqual(c[("m", 10)][0], 100)      # not 200
        self.assertEqual(c[("m", 11)][0], 7)
        self.assertEqual(c[("m", 12)][0], 100)      # only the old id saw it: kept
        self.assertEqual(out["meta"]["machines"], 1)
        self.assertEqual(set(out["machines"]), {SELF})

    def test_without_host_fp_peers_still_add(self):
        local = self.local(records=[rec(10, inp=100)])
        old = peer(A, {"claude": plat([raw_series("m", [10], inp=100)])})
        out = self.merge_fp("claude", local, [old])
        self.assertEqual(cells(out["series"])[("m", 10)][0], 200)

    def test_peer_group_uses_max_and_newest_member_represents(self):
        local = self.local(records=[rec(10, inp=1)])
        older = peer(A, {"claude": plat([raw_series("m", [10, 11], inp=50)])}, name="Mac", fp=FP_A,
                     written=NOW - 5000)
        newer = peer(B, {"claude": plat([raw_series("m", [10], inp=50)])}, name="Mac", fp=FP_A,
                     written=NOW - 60)
        out = self.merge_fp("claude", local, [older, newer])
        c = cells(out["series"])
        self.assertEqual(c[("m", 10)][0], 51)       # 1 (this computer) + max(50, 50)
        self.assertEqual(c[("m", 11)][0], 50)
        self.assertEqual(set(out["machines"]), {SELF, B})
        self.assertEqual(out["meta"]["machines"], 2)
        parts = sum(sum(s["input"]) for m in out["machines"].values() for s in m["series"])
        self.assertEqual(parts, sum(sum(s["input"]) for s in out["series"]))
        rows = merge.machine_rows({"id": SELF, "name": "Me", "last_seen": NOW, "status": "ok"},
                                  {"claude": local}, [older, newer], NOW, self_fp=FP_SELF)
        by = {r["id"]: r for r in rows}
        self.assertEqual(by[A]["superseded_by"], B)
        self.assertEqual(by[A]["superseded_by_name"], "Mac")
        self.assertFalse(by[A]["stale"])
        self.assertEqual(by[A]["state"], "active")
        self.assertIsNone(by[B]["superseded_by"])
        self.assertIsNone(by[SELF]["superseded_by"])

    def test_old_id_of_this_computer_is_superseded_by_self(self):
        old = peer(A, {"claude": plat()}, name="Old me", fp=FP_SELF)
        rows = merge.machine_rows({"id": SELF, "name": "Me", "last_seen": NOW, "status": "ok"},
                                  {}, [old], NOW, self_fp=FP_SELF)
        self.assertEqual(rows[1]["superseded_by"], SELF)
        self.assertEqual(rows[1]["superseded_by_name"], "Me")

    def test_run_copies_from_the_same_computer_are_dropped(self):
        local = self.local(records=[rec(10, inp=1)])
        local["subagent_runs"] = [dict(run_rec("orig", at(5)), session_id="s")]
        old = peer(A, {"claude": plat([raw_series("m", [10], inp=1)],
                                      runs=[run_rec("hashedold", at(5))])}, fp=FP_SELF)
        out = self.merge_fp("claude", local, [old])
        self.assertEqual(len(out["subagent_runs"]), 1)

    def test_adopted_history_joins_this_computers_own_view(self):
        local = self.local(records=[rec(10, inp=100)])
        adopted, _ = validate.parse_machine(
            {"format": "token-burn-machine", "schema": 1, "machine_id": SELF, "written_at": iso(NOW - 99),
             "platforms": {"claude": plat([raw_series("m", [10, 11], inp=60)])}}, SELF, NOW)
        out = self.merge_fp("claude", local, [], adopted=adopted["platforms"]["claude"])
        c = cells(out["series"])
        self.assertEqual(c[("m", 10)][0], 100)      # per-cell max with local, not added
        self.assertEqual(c[("m", 11)][0], 60)       # earlier history shows up
        self.assertEqual(self.merge_fp("claude", local, []), local)    # nothing adopted: untouched

    def test_breakdown_with_one_machine_having_data(self):
        idle_local = self.local("claude")
        idle_peer = peer(A, {"claude": plat()})
        out = self.merge("claude", idle_local, [idle_peer])
        self.assertEqual(out["machines"], {})               # key present: nobody has data
        busy = self.local("claude", records=[rec(10, inp=5)])
        out = self.merge("claude", busy, [idle_peer])
        self.assertEqual(set(out["machines"]), {SELF})      # present, one entry; A is absent (zero)
        self.assertEqual(out["meta"]["machines"], 1)
        # Cursor never gets a breakdown
        cur = self.local("cursor", records=[rec(10, "cursor", inp=5)])
        self.assertNotIn("machines", self.merge("cursor", cur, [peer(A, {"cursor": plat()})]))
        # a single computer (no peers) is unchanged
        self.assertIs(self.merge("claude", busy, []), busy)


class CursorLarger(MergeBase):
    def test_not_double_counted_and_larger_wins(self):
        local = self.local("cursor", [rec(4, "cursor", inp=100, out=10, cost_usd=0.5, requests=2),
                                      rec(5, "cursor", inp=10)])
        same = peer(A, {"cursor": plat([raw_series("m", [4], inp=100, out=10, cost=0.5, reqs=2)])})
        out = self.merge("cursor", local, [same])
        self.assertEqual(cells(out["series"])[("m", 4)], [100, 10, 0, 0.5, 2])
        self.assertNotIn("machines", out)
        bigger = peer(B, {"cursor": plat([raw_series("m", [4, 5], inp=500, out=1, reqs=9)])})
        out = self.merge("cursor", local, [same, bigger])
        c = cells(out["series"])
        self.assertEqual(c[("m", 4)][0], 500)
        self.assertEqual(c[("m", 4)][4], 9)
        self.assertEqual(c[("m", 5)][0], 500)

    def test_ties_prefer_cost_then_requests(self):
        local = self.local("cursor", [rec(4, "cursor", inp=100, cost_usd=0.1, requests=1)])
        pa = peer(A, {"cursor": plat([raw_series("m", [4], inp=100, cost=0.4, reqs=1)])})
        pb = peer(B, {"cursor": plat([raw_series("m", [4], inp=100, cost=0.4, reqs=5)])})
        out = self.merge("cursor", local, [pa, pb])
        self.assertEqual(cells(out["series"])[("m", 4)], [100, 0, 0, 0.4, 5])

    def test_sub_is_max_sub_capped_by_chosen_total(self):
        local = self.local("cursor", [rec(4, "cursor", inp=100, agent_kind="subagent", agent_id="g")])
        pa = peer(A, {"cursor": plat([raw_series("m", [4], inp=300)])})
        out = self.merge("cursor", local, [pa])
        s = out["series"][0]
        self.assertEqual(s["input"], [300])
        self.assertEqual(s["sub_input"], [100])
        # the other computer's sub is larger than the chosen total: capped at the total
        pb = peer(B, {"cursor": plat([raw_series("m", [4], inp=40, sub_input=[40])])})
        pc = peer(C, {"cursor": plat([raw_series("m", [4], inp=30, sub_input=[30])])})
        out = self.merge("cursor", NOT_INSTALLED, [pb, pc])
        self.assertEqual(out["series"][0]["input"], [40])
        self.assertEqual(out["series"][0]["sub_input"], [40])

    def test_runs_union_prefer_tokens(self):
        local = self.local("cursor", [rec(1, "cursor", inp=1)])
        local["subagent_runs"] = [run_rec("g", NOW - 50, tokens=False), run_rec("h", NOW - 20, tokens=False)]
        pa = peer(A, {"cursor": plat([raw_series("m", [1], inp=1)],
                                     runs=[run_rec("g", NOW - 80), run_rec("i", NOW - 30)])})
        out = self.merge("cursor", local, [pa])
        by_id = {r["agent_id"]: r for r in out["subagent_runs"]}
        self.assertEqual(set(by_id), {"g", "h", "i"})
        self.assertEqual(by_id["g"]["input"], 1)


class Plans(MergeBase):
    def test_union_dedup_and_newest_pools(self):
        samples = [(NOW - 600, {"five_hour": {"pct": 10.0, "resets_at": "2026-11-05T06:00:00Z"}}),
                   (NOW - 300, {"five_hour": {"pct": 20.0, "resets_at": "2026-11-05T06:00:00Z"}})]
        local = self.local(records=[rec(1, inp=1)])
        pa = peer(A, {"claude": plat([], samples=[(NOW - 300.2, {"five_hour": {"pct": 99.0}}),
                                                  (NOW - 100, {"five_hour": {
                                                      "pct": 30.0, "resets_at": "2026-11-05T11:00:00Z"}})],
                                     last_lookup="2030-01-01T00:00:00Z")})
        out = self.merge("claude", local, [pa], samples)
        plan = out["plan"]
        self.assertEqual(len(plan["samples"]), 3)          # NOW-300.2 and NOW-300 share round(t)
        self.assertEqual(plan["pools"][0]["now"], 30.0)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["next_lookup"], "2026-01-01T00:10:00Z")
        self.assertEqual(plan["last_lookup"], "2030-01-01T00:00:00Z")
        self.assertEqual(len(plan["reset_times"]["five_hour"]), 2)

    def test_local_plan_kept_when_peers_have_none(self):
        local = self.local(records=[rec(1, inp=1)])
        out = self.merge("claude", local, [peer(A, {"claude": plat([raw_series("m", [1], inp=1)])})], [])
        self.assertEqual(out["plan"], local["plan"])


class MachineRowsTests(unittest.TestCase):
    def test_rows(self):
        local = {"claude": {"installed": True, "series": [
                     {"input": [1, 2], "output": [0, 0], "cache": [10, 0]}]},
                 "cursor": {"installed": True, "series": [{"input": [999], "output": [0], "cache": [0]}]},
                 "opencode": {"installed": False}}
        pa = peer(A, {"claude": plat([raw_series("m", [1], inp=100, out=10, cache=5)]),
                      "cursor": plat([raw_series("m", [1], inp=999)])},
                  name="Zed", written=NOW - 4000)
        pb = peer(B, {"opencode": plat([raw_series("m", [1], inp=50)])}, name="alpha",
                   control=ctl("retired", "Old PC"), written=NOW - 90000)
        pc = peer(C, {"claude": plat()}, name="gone", control=ctl("removed"))
        info = {"id": SELF, "name": "Me", "os": "Windows 11", "hostname": "ME", "app_version": "0.2.0",
                "last_seen": NOW, "status": "ok", "state": "active"}
        rows = merge.machine_rows(info, local, [pa, pb, pc], NOW)
        self.assertEqual([r["name"] for r in rows], ["Me", "Old PC", "Zed"])
        me, old, zed = rows
        self.assertTrue(me["is_self"])
        self.assertFalse(old["is_self"])
        self.assertEqual(me["tokens_30d"], 13)
        self.assertEqual(me["platforms"], ["claude", "cursor"])
        self.assertEqual(zed["tokens_30d"], 115)           # Cursor not counted
        self.assertTrue(zed["stale"])
        self.assertEqual(old["state"], "retired")
        self.assertFalse(old["stale"])
        self.assertEqual(old["tokens_30d"], 50)
        self.assertFalse(me["stale"])
        self.assertEqual(zed["last_seen"], iso(NOW - 4000))
        self.assertEqual(zed["os"], "Linux")

    def test_self_stale_and_docless_peer(self):
        info = {"id": SELF, "name": "Me", "last_seen": NOW - 7200, "state": "active"}
        nodoc = {"id": C, "doc": None, "status": "not downloaded yet", "from_cache": False,
                 "control": None, "mtime": NOW - 10}
        rows = merge.machine_rows(info, {}, [nodoc], NOW)
        self.assertTrue(rows[0]["stale"])
        self.assertEqual(rows[1]["name"], "Computer cccccccc")
        self.assertEqual(rows[1]["status"], "not downloaded yet")
        self.assertEqual(rows[1]["tokens_30d"], 0)


class UsageJsonTests(unittest.TestCase):
    def test_unchanged_when_sync_off(self):
        payloads = {"claude": dict(NOT_INSTALLED)}
        out = build_usage_json(payloads, NOW)
        self.assertEqual(list(out), ["generated_at", "token_refresh_s", "plan_refresh_s", "platforms"])
        self.assertEqual(json.dumps(out), json.dumps(
            {"generated_at": iso(NOW), "token_refresh_s": util.TOKEN_REFRESH_S,
             "plan_refresh_s": util.PLAN_INTERVAL_S, "platforms": payloads}))
        out = build_usage_json(payloads, NOW, machines=[{"id": "x"}], sync={"enabled": True})
        self.assertEqual(out["machines"], [{"id": "x"}])
        self.assertEqual(out["sync"], {"enabled": True})

    def test_collect_without_config_has_no_sync_keys(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                    mock.patch.object(util, "USAGE_JSON", d / "usage.json"):
                usage = collect.run(now=NOW, platforms=[("claude", lambda s, n, l, backfill=False: {},
                                                          lambda log: ("ok", {}))], log=mock.Mock())
        self.assertEqual(set(usage), {"generated_at", "token_refresh_s", "plan_refresh_s", "platforms"})


if __name__ == "__main__":
    unittest.main()
