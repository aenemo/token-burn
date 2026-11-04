import tempfile
import unittest
from pathlib import Path

from collector.aggregate import (POOL_LABELS, build_platform_payload, build_usage_json,
                                 pool_label, thin_samples)
from collector.schema import SubagentRun, UsageRecord
from collector.store import Store
from collector.util import MINUTES, iso

NOW = 1_800_000_000.0           # divisible by 60
NOW_MIN = int(NOW // 60)
START_MIN = NOW_MIN - MINUTES + 1


def at(idx):
    """Epoch seconds for the given minute index from the window start."""
    return (START_MIN + idx) * 60.0 + 5


def rec(idx, platform="claude", inp=0, out=0, cr=0, cw=0, **kw):
    d = dict(platform=platform, agent_kind="primary", agent_id="", session_id="s1", model="m",
             effort_level="", input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
             cache_write_tokens=cw, cost_usd=0.0, timestamp=at(idx))
    d.update(kw)
    return UsageRecord(**d)


INFO = {"status": "ok", "meta": {}, "plan": {"status": "ok", "last_lookup": None, "next_lookup": None}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def payload(self, platform="claude", info=INFO):
        return build_platform_payload(self.store, platform, NOW, info)


class ClaudeCostTests(Base):
    def test_costed_allocation_by_tokens(self):
        self.store.add_usage(rec(10, inp=100))
        self.store.add_usage(rec(20, inp=300))
        self.store.set_session_cost("claude", "s1", "m", 4.0)
        p = self.payload()
        (s,) = p["series"]
        self.assertEqual(s["cost"], [1.0, 3.0])
        self.assertEqual(p["meta"]["sessions_estimated"], 0)
        self.assertEqual(p["meta"]["sessions_without_cost"], 0)
        self.assertEqual(p["meta"]["estimated_cost_usd"], 0.0)

    def test_allocation_uses_lifetime_tokens_including_pruned(self):
        self.store.add_usage(rec(10, inp=100))
        self.store.add_usage(rec(-100000, inp=300))   # far outside the window
        self.store.set_session_cost("claude", "s1", "m", 4.0)
        (s,) = self.payload()["series"]
        self.assertEqual(s["cost"], [1.0])

    def test_estimated_from_model_rate(self):
        self.store.add_usage(rec(10, inp=400))
        self.store.set_session_cost("claude", "s1", "m", 4.0)          # rate 0.01 per token
        self.store.add_usage(rec(30, session_id="s2", inp=200))        # no cost yet
        self.store.add_usage(rec(31, session_id="s3", inp=50))
        p = self.payload()
        (s,) = p["series"]
        self.assertEqual(s["t"], [10, 30, 31])
        self.assertEqual(s["cost"], [4.0, 2.0, 0.5])
        self.assertEqual(p["meta"]["sessions_estimated"], 2)
        self.assertEqual(p["meta"]["estimated_cost_usd"], 2.5)
        self.assertEqual(p["meta"]["sessions_without_cost"], 0)

    def test_no_rate_means_zero_and_without_cost(self):
        self.store.add_usage(rec(10, inp=100, model="m2"))
        self.store.add_usage(rec(11, inp=100, model="m2", session_id="s9"))
        p = self.payload()
        self.assertEqual(p["series"][0]["cost"], [0.0, 0.0])
        self.assertEqual(p["meta"]["sessions_without_cost"], 2)
        self.assertEqual(p["meta"]["sessions_estimated"], 0)

    def test_rate_is_per_model(self):
        self.store.add_usage(rec(10, inp=100, model="a"))
        self.store.set_session_cost("claude", "s1", "a", 1.0)
        self.store.add_usage(rec(11, inp=100, model="b", session_id="s2"))
        p = self.payload()
        by = {s["key"]: s for s in p["series"]}
        self.assertEqual(by["b"]["cost"], [0.0])
        self.assertEqual(p["meta"]["sessions_without_cost"], 1)

    def test_effort_levels_share_session_cost(self):
        self.store.add_usage(rec(10, inp=100, effort_level="high"))
        self.store.add_usage(rec(10, inp=300, effort_level="low"))
        self.store.set_session_cost("claude", "s1", "m", 8.0)
        by = {s["key"]: s for s in self.payload()["series"]}
        self.assertEqual(by["m-low"]["cost"], [6.0])
        self.assertEqual(by["m-high"]["cost"], [2.0])

    def test_meta_counts(self):
        self.store.add_usage(rec(1, inp=1, model="Auto Cost"))
        self.store.add_usage(rec(2, inp=1, model="Auto Cost"))
        self.store.add_usage(rec(3, inp=1))
        info = dict(INFO, meta={"files": 78, "requests": 99})
        meta = self.payload(info=info)["meta"]
        self.assertEqual(meta["auto_label_requests"], 2)
        self.assertEqual(meta["requests"], 3)
        self.assertEqual(meta["files"], 78)
        for k in ("sessions_without_cost", "sessions_estimated", "estimated_cost_usd"):
            self.assertIn(k, meta)


class SeriesTests(Base):
    def test_shape_sparse_and_sub_arrays(self):
        self.store.add_usage(rec(5, inp=10, out=20, cr=30, cw=40, effort_level="medium"))
        self.store.add_usage(rec(5, inp=1, out=2, cr=3, cw=4, effort_level="medium",
                                 agent_kind="subagent", agent_id="ag"))
        self.store.add_usage(rec(9, inp=100, effort_level="medium"))
        self.store.set_session_cost("claude", "s1", "m", 1.11)
        p = self.payload()
        self.assertEqual(p["start"], iso(START_MIN * 60))
        self.assertEqual(p["minutes"], MINUTES)
        (s,) = p["series"]
        self.assertEqual(s["key"], "m-medium")
        self.assertEqual((s["model"], s["effort"]), ("m", "medium"))
        self.assertEqual(s["t"], [5, 9])
        self.assertEqual(s["input"], [11, 100])
        self.assertEqual(s["output"], [22, 0])
        self.assertEqual(s["cache"], [77, 0])
        self.assertEqual(s["requests"], [2, 1])
        self.assertEqual(s["sub_input"], [1, 0])
        self.assertEqual(s["sub_output"], [2, 0])
        self.assertEqual(s["sub_cache"], [7, 0])
        total = 11 + 22 + 77 + 100
        self.assertAlmostEqual(s["cost"][0], 1.11 * (11 + 22 + 77) / total, places=5)
        self.assertAlmostEqual(s["sub_cost"][0], 1.11 * 10 / total, places=5)
        self.assertEqual(s["sub_cost"][1], 0.0)
        n = len(s["t"])
        for k in ("input", "output", "cache", "cost", "requests", "sub_input", "sub_output",
                  "sub_cache", "sub_cost"):
            self.assertEqual(len(s[k]), n, k)

    def test_cache_read_arrays(self):
        self.store.add_usage(rec(5, inp=10, cr=70, cw=7, agent_kind="subagent"))
        self.store.add_usage(rec(5, inp=1, cr=3, cw=0))
        self.store.add_usage(rec(9, inp=2, cr=0, cw=4))
        (s,) = self.payload()["series"]
        n = len(s["cache"])
        self.assertEqual(len(s["cache_read"]), n)
        self.assertEqual(len(s["sub_cache_read"]), n)
        self.assertEqual(s["cache"], [80, 4])
        self.assertEqual(s["cache_read"], [73, 0])
        self.assertEqual(s["sub_cache_read"], [70, 0])
        self.assertEqual(s["sub_cache"], [77, 0])
        for a, b in zip(s["cache_read"], s["cache"]):
            self.assertLessEqual(a, b)
        for a, b in zip(s["sub_cache_read"], s["sub_cache"]):
            self.assertLessEqual(a, b)
        self.assertEqual(s["input"], [11, 2])

    def test_key_without_effort_and_sort_order(self):
        self.store.add_usage(rec(1, inp=10, model="small"))
        self.store.add_usage(rec(1, inp=1000, model="big", effort_level="high"))
        self.store.add_usage(rec(1, inp=500, model="mid"))
        self.store.add_usage(rec(2, inp=500, model="aaa"))
        keys = [s["key"] for s in self.payload()["series"]]
        self.assertEqual(keys, ["big-high", "aaa", "mid", "small"])

    def test_window_bounds(self):
        self.store.add_usage(rec(-1, inp=5))                 # before the window
        self.store.add_usage(rec(0, inp=5))
        self.store.add_usage(rec(MINUTES - 1, inp=5))
        self.store.add_usage(rec(MINUTES, inp=5))            # in the future
        (s,) = self.payload()["series"]
        self.assertEqual(s["t"], [0, MINUTES - 1])

    def test_cursor_uses_row_cost(self):
        self.store.add_usage(rec(3, platform="cursor", inp=10, cost_usd=0.25))
        self.store.add_usage(rec(3, platform="cursor", inp=10, cost_usd=0.5))
        self.store.add_usage(rec(4, platform="cursor", inp=10, cost_usd=0.0))
        p = self.payload("cursor", dict(INFO, meta={"event_count": 3}))
        self.assertEqual(p["series"][0]["cost"], [0.75, 0.0])
        self.assertEqual(p["meta"]["event_count"], 3)
        self.assertNotIn("sessions_estimated", p["meta"])
        self.assertIs(p["meta"]["subagent_tokens_attributed"], False)

    def test_platform_isolation_and_status(self):
        self.store.add_usage(rec(3, platform="cursor", inp=10))
        p = self.payload("claude", dict(INFO, status="sign-in expired"))
        self.assertEqual(p["series"], [])
        self.assertEqual(p["status"], "sign-in expired")


class SubagentRunTests(Base):
    def test_claude_recomputed_from_rows(self):
        self.store.add_usage(rec(5, inp=100, out=100, agent_kind="subagent", agent_id="ag1"))
        self.store.add_usage(rec(6, cr=100, cw=100, agent_kind="subagent", agent_id="ag1"))
        self.store.add_usage(rec(7, inp=400))
        self.store.set_session_cost("claude", "s1", "m", 8.0)
        self.store.upsert_subagent_run(SubagentRun("claude", "ag1", "explore", "explore · ag1", "s1",
                                                   "m", "medium", at(6), "completed",
                                                   input_tokens=1, output_tokens=1))
        (r,) = self.payload()["subagent_runs"]
        self.assertEqual((r["input"], r["output"], r["cache"]), (100, 100, 200))
        self.assertAlmostEqual(r["cost"], 8.0 * 400 / 800)
        self.assertFalse(r["cost_estimated"])
        self.assertEqual(r["t"], iso(at(6)))
        self.assertEqual((r["agent_name"], r["agent_type"], r["status"]),
                         ("explore · ag1", "explore", "completed"))

    def test_claude_estimated_flag_from_rows(self):
        self.store.add_usage(rec(1, inp=100))
        self.store.set_session_cost("claude", "s1", "m", 1.0)
        self.store.add_usage(rec(5, inp=100, session_id="s2", agent_kind="subagent", agent_id="ag"))
        self.store.upsert_subagent_run(SubagentRun("claude", "ag", "t", "t · ag", "s2", "m", "", at(5)))
        (r,) = self.payload()["subagent_runs"]
        self.assertTrue(r["cost_estimated"])
        self.assertAlmostEqual(r["cost"], 1.0)

    def test_claude_fallback_to_hook_tokens(self):
        self.store.add_usage(rec(1, inp=100))
        self.store.set_session_cost("claude", "s1", "m", 1.0)   # rate 0.01
        self.store.upsert_subagent_run(SubagentRun("claude", "gone", "t", "t · gone", "sx", "m", "",
                                                   at(5), "completed", 10, 20, 30, 40))
        self.store.upsert_subagent_run(SubagentRun("claude", "norate", "t", "t · nor", "sx", "zz", "",
                                                   at(4), "completed", 5, 5, 5, 5))
        runs = {r["agent_id"]: r for r in self.payload()["subagent_runs"]}
        g = runs["gone"]
        self.assertEqual((g["input"], g["output"], g["cache"]), (10, 20, 70))
        self.assertAlmostEqual(g["cost"], 1.0)
        self.assertTrue(g["cost_estimated"])
        n = runs["norate"]
        self.assertEqual((n["cost"], n["cost_estimated"]), (0.0, False))

    def test_runs_sorted_newest_first(self):
        for i, t in enumerate((3, 9, 6)):
            self.store.upsert_subagent_run(SubagentRun("claude", "a%d" % i, "", "", "s", "m", "", at(t)))
        self.assertEqual([r["agent_id"] for r in self.payload()["subagent_runs"]], ["a1", "a2", "a0"])

    def test_cursor_without_rows_is_null(self):
        self.store.upsert_subagent_run(SubagentRun("cursor", "c1", "explore", "explore · c1", "cv",
                                                   "m", "", at(2), "completed"))
        p = self.payload("cursor")
        (r,) = p["subagent_runs"]
        for k in ("input", "output", "cache", "cost"):
            self.assertIsNone(r[k], k)
        self.assertIs(p["meta"]["subagent_tokens_attributed"], False)

    def test_cursor_with_rows(self):
        self.store.add_usage(rec(2, platform="cursor", inp=10, out=5, cr=3, cw=2, cost_usd=0.4,
                                 agent_kind="subagent", agent_id="c1", session_id="cv"))
        self.store.upsert_subagent_run(SubagentRun("cursor", "c1", "explore", "explore · c1", "cv",
                                                   "m", "", at(2), "completed"))
        self.store.upsert_subagent_run(SubagentRun("cursor", "c2", "", "", "cv", "m", "", at(3)))
        p = self.payload("cursor")
        runs = {r["agent_id"]: r for r in p["subagent_runs"]}
        self.assertEqual((runs["c1"]["input"], runs["c1"]["output"], runs["c1"]["cache"]), (10, 5, 5))
        self.assertAlmostEqual(runs["c1"]["cost"], 0.4)
        self.assertIsNone(runs["c2"]["cost"])
        self.assertIs(p["meta"]["subagent_tokens_attributed"], True)


class PlanTests(Base):
    def test_pools_remaining_and_labels(self):
        self.store.add_plan_sample("claude", NOW - 600, {"five_hour": {"pct": 4.0, "resets_at": None}})
        self.store.add_plan_sample("claude", NOW - 10, {
            "five_hour": {"pct": 9.0, "resets_at": "2026-10-21T06:29:59Z"},
            "seven_day": {"pct": 120.0, "resets_at": None},
            "weird_pool": {"pct": 1.5, "resets_at": None}})
        info = dict(INFO, plan={"status": "ok", "last_lookup": "L", "next_lookup": "N"})
        plan = self.payload(info=info)["plan"]
        self.assertEqual((plan["status"], plan["last_lookup"], plan["next_lookup"]), ("ok", "L", "N"))
        pools = {p["id"]: p for p in plan["pools"]}
        self.assertEqual(pools["five_hour"], {"id": "five_hour", "label": "5-hour session", "now": 9.0,
                                              "remaining": 91.0, "resets": "2026-10-21T06:29:59Z"})
        self.assertEqual(pools["seven_day"]["remaining"], 0)
        self.assertEqual(pools["weird_pool"]["label"], "Weird Pool")
        self.assertEqual(plan["samples"][0], {"t": iso(NOW - 600), "five_hour": 4.0})
        self.assertEqual(plan["samples"][1]["seven_day"], 120.0)

    def test_no_samples(self):
        plan = self.payload()["plan"]
        self.assertEqual((plan["pools"], plan["samples"]), ([], []))

    def test_thinning(self):
        two_days = 2 * 86400
        old0 = NOW - two_days - 3600
        samples = [(old0 + 60 * i, {"p": {"pct": float(i)}}) for i in range(20)]    # old, 1/min
        samples += [(NOW - 100 + 10 * i, {"p": {"pct": 50.0 + i}}) for i in range(5)]  # recent
        out = thin_samples(samples, NOW)
        old = [r for r in out if r["t"] < iso(NOW - two_days)]
        self.assertEqual([r["p"] for r in old], [0.0, 5.0, 10.0, 15.0])
        self.assertEqual(len([r for r in out if r["t"] >= iso(NOW - two_days)]), 5)

    def test_thinning_keeps_last_sample(self):
        old0 = NOW - 3 * 86400
        samples = [(old0, {"p": {"pct": 1.0}}), (old0 + 60, {"p": {"pct": 2.0}})]
        out = thin_samples(samples, NOW)
        self.assertEqual([r["p"] for r in out], [1.0, 2.0])

    def test_labels(self):
        self.assertEqual(POOL_LABELS["cursor_models"], "Cursor Models")
        self.assertEqual(POOL_LABELS["other_models"], "Other Models")
        self.assertEqual(POOL_LABELS["seven_day_opus"], "7-day Opus")
        self.assertEqual(POOL_LABELS["seven_day_sonnet"], "7-day Sonnet")
        self.assertEqual(pool_label("seven_day_foo"), "Seven Day Foo")


class UsageJsonTests(Base):
    def test_top_level(self):
        a = self.payload("claude")
        b = self.payload("cursor")
        doc = build_usage_json({"claude": a, "cursor": b}, NOW)
        self.assertEqual(doc["generated_at"], iso(NOW))
        self.assertEqual(doc["token_refresh_s"], 60)
        self.assertEqual(doc["plan_refresh_s"], 590)
        self.assertEqual(set(doc["platforms"]), {"claude", "cursor"})
        self.assertEqual(set(a), {"status", "start", "minutes", "series", "subagent_runs", "plan", "meta"})
        import json
        json.dumps(doc)


if __name__ == "__main__":
    unittest.main()


class ResetTimesTests(unittest.TestCase):
    def test_distinct_rounded_and_sorted(self):
        from collector.aggregate import reset_times
        samples = [
            (1.0, {"five_hour": {"pct": 1.0, "resets_at": "2026-10-21T06:29:59.963034+00:00"},
                   "seven_day": {"pct": 2.0, "resets_at": "2026-10-26T07:00:00Z"}}),
            (2.0, {"five_hour": {"pct": 3.0, "resets_at": "2026-10-21T06:30:00.120000+00:00"}}),
            (3.0, {"five_hour": {"pct": 4.0, "resets_at": "2026-10-21T01:30:00Z"},
                   "seven_day": {"pct": 5.0, "resets_at": None}}),
        ]
        self.assertEqual(reset_times(samples), {
            "five_hour": ["2026-10-21T01:30:00Z", "2026-10-21T06:30:00Z"],
            "seven_day": ["2026-10-26T07:00:00Z"]})
