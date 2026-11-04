import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.adapters import cursor
from collector.store import Store

FIX = Path(__file__).resolve().parent / "fixtures" / "cursor"
ROOT = Path(__file__).resolve().parent.parent
NOW = 1792000300.0


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakePost:
    """Scripted http_post. responses maps url -> list of (status, body) per call."""

    def __init__(self, responses):
        self.responses = {u: list(v) for u, v in responses.items()}
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        queue = self.responses.get(url) or [(404, "")]
        return queue.pop(0) if len(queue) > 1 else queue[0]


U1, U2 = cursor.EVENT_URLS


def ok(obj):
    return 200, json.dumps(obj)


class AdapterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.hook_log = Path(self.tmp) / "hooks.jsonl"
        patches = [
            mock.patch.object(cursor, "read_token", lambda *a, **k: "TESTTOKEN"),
            mock.patch.object(cursor, "CURSOR_HOOK_LOG", self.hook_log),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)

    def run_collect(self, post, now=NOW, backfill=False):
        with mock.patch.object(cursor, "http_post", post):
            return cursor.collect(self.store, now, None, backfill=backfill)

    def rows(self):
        return self.store.usage_rows("cursor", 0)

    def by_model(self):
        out = {}
        for r in self.rows():
            out.setdefault((r["model"], r["effort"], r["agent_kind"]), []).append(r)
        return out


class TestParsing(unittest.TestCase):
    def test_token_parts_split_and_cost(self):
        ev = load("events_page1.json")["usageEventsDisplay"][0]
        self.assertEqual(cursor.token_parts(ev), (100, 200, 3000, 400, 0.125))

    def test_cost_falls_back_to_total_cents(self):
        ev = {"tokenUsage": {"inputTokens": 1, "totalCents": 50}}
        self.assertEqual(cursor.token_parts(ev)[4], 0.5)
        self.assertEqual(cursor.token_parts({})[4], 0.0)

    def test_model_resolution(self):
        self.assertIsNone(cursor.event_model_name({"model": "default"}))
        self.assertEqual(cursor.event_model_name({"model": "default", "resolvedModel": "grok-4.7-high"}), "grok-4.7-high")
        self.assertEqual(cursor.event_model_name({"model": "Auto", "tokenUsage": {"model": "gpt-5"}}), "gpt-5")
        self.assertEqual(cursor.event_model_name({"someModelField": "x-model"}), "x-model")
        self.assertIsNone(cursor.event_model_name({"model": " Unknown Model "}))

    def test_event_list_key_fallbacks(self):
        self.assertEqual(cursor.event_list({"usageEvents": [1]}), ([1], None))
        self.assertEqual(cursor.event_list({"events": [2], "totalCount": 9}), ([2], 9))
        self.assertEqual(cursor.event_list([]), ([], None))


class TestCollect(AdapterBase):
    def test_basic_ingest(self):
        post = FakePost({U1: [ok(load("events_page1.json"))]})
        res = self.run_collect(post)
        self.assertEqual(res["status"], "ok")
        meta = res["meta"]
        self.assertEqual(meta["event_count"], 5)
        self.assertEqual(meta["headless_requests"], 1)
        self.assertEqual(meta["default_fallback_requests"], 1)
        self.assertFalse(meta["truncated"])
        self.assertTrue(meta["tokens_available"])
        groups = self.by_model()
        opus = groups[("claude-opus-5-5", "medium", "primary")][0]
        self.assertEqual((opus["input"], opus["output"], opus["cache_read"], opus["cache_write"]), (100, 200, 3000, 400))
        self.assertAlmostEqual(opus["cost"], 0.125)
        self.assertIn(("Auto Cost", "", "primary"), groups)
        self.assertIn(("grok-4.7-high", "", "primary") if False else ("grok-4.7", "high", "primary"), groups)
        self.assertIn(("gpt-5", "extra", "primary"), groups)
        self.assertIn(("Claude Opus 5.5", "balanced", "primary"), groups)
        # first request used cookie headers, with a 30 day window
        url, headers, payload = post.calls[0]
        self.assertTrue(headers["Cookie"].startswith("WorkosCursorSessionToken="))
        self.assertEqual(int(payload["endDate"]) - int(payload["startDate"]), 30 * 86400 * 1000)
        self.assertEqual(self.store.kv_get("cursor:last_ts"), 1792000240000)

    def test_endpoint_fallback(self):
        post = FakePost({U1: [(500, "boom")], U2: [ok(load("events_page1.json"))]})
        res = self.run_collect(post)
        self.assertEqual(res["status"], "ok")
        self.assertEqual([c[0] for c in post.calls][:2], [U1, U2])
        self.assertIn("Bearer ", post.calls[1][1]["Authorization"])
        self.assertEqual(post.calls[1][1]["Connect-Protocol-Version"], "1")
        self.assertEqual(len(self.rows()), 5)

    def test_all_endpoints_fail_returns_message(self):
        res = self.run_collect(FakePost({U1: [(401, "no")], U2: [(401, "no")]}))
        self.assertIn("unavailable", res["status"])
        self.assertNotIn("TESTTOKEN", json.dumps(res))
        self.assertEqual(self.rows(), [])

    def test_missing_token(self):
        with mock.patch.object(cursor, "read_token", lambda *a, **k: None):
            res = cursor.collect(self.store, NOW, None)
        self.assertEqual(res["status"], "Cursor sign-in token not found")

    def test_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        with mock.patch.object(cursor, "http_post", boom):
            res = cursor.collect(self.store, NOW, None)
        self.assertNotEqual(res["status"], "ok")

    def _page(self, start, n, total):
        return ok({"totalUsageEventsCount": total, "usageEventsDisplay": [
            {"timestamp": str(1792000000000 + (start + i) * 1000), "model": "m-high", "conversationId": "c",
             "chargedCents": 1, "tokenUsage": {"inputTokens": 1, "outputTokens": 1}} for i in range(n)]})

    def test_paging(self):
        post = FakePost({U1: [self._page(0, 100, 250), self._page(100, 100, 250), self._page(200, 50, 250)]})
        res = self.run_collect(post)
        self.assertEqual(res["meta"]["event_count"], 250)
        self.assertFalse(res["meta"]["truncated"])
        self.assertEqual([c[2]["page"] for c in post.calls], [1, 2, 3])
        self.assertTrue(all(c[2]["pageSize"] == 100 for c in post.calls))
        self.assertEqual(sum(r["requests"] for r in self.rows()), 250)

    def test_truncated_at_100_pages(self):
        post = FakePost({U1: [self._page(0, 100, None) for _ in range(2)]})
        # total unknown and every page full: stops at page 100 and flags truncated
        body = json.loads(self._page(0, 100, None)[1])
        body.pop("totalUsageEventsCount")
        post = FakePost({U1: [(200, json.dumps(body))]})
        res = self.run_collect(post)
        self.assertTrue(res["meta"]["truncated"])
        self.assertEqual(len(post.calls), 100)

    def test_incremental_window_and_dedupe(self):
        full = FakePost({U1: [ok(load("events_page1.json"))]})
        self.run_collect(full)
        before = sum(r["input"] for r in self.rows())
        last = self.store.kv_get("cursor:last_ts")
        again = FakePost({U1: [ok(load("events_page1.json"))]})
        res = self.run_collect(again, now=NOW + 60)
        payload = again.calls[0][2]
        self.assertEqual(int(payload["startDate"]), int(last) - 2 * 3600 * 1000)
        self.assertEqual(int(payload["endDate"]), int((NOW + 60) * 1000))
        self.assertEqual(sum(r["input"] for r in self.rows()), before)
        self.assertEqual(res["meta"]["event_count"], 5)  # cumulative, nothing new
        # a genuinely new event is added once
        extra = load("events_page1.json")
        extra["usageEventsDisplay"].append({"timestamp": "1792000300000", "model": "m-low", "conversationId": "z",
                                            "tokenUsage": {"inputTokens": 9}})
        extra["totalUsageEventsCount"] = 6
        res = self.run_collect(FakePost({U1: [ok(extra)]}), now=NOW + 120)
        self.assertEqual(res["meta"]["event_count"], 6)
        self.assertEqual(sum(r["input"] for r in self.rows()), before + 9)
        self.assertEqual(self.store.kv_get("cursor:last_ts"), 1792000300000)

    def test_backfill_uses_full_window_even_with_last_ts(self):
        self.store.kv_set("cursor:last_ts", 1792000000000)
        post = FakePost({U1: [ok(load("events_page1.json"))]})
        self.run_collect(post, backfill=True)
        p = post.calls[0][2]
        self.assertEqual(int(p["endDate"]) - int(p["startDate"]), 30 * 86400 * 1000)


class TestSubagents(AdapterBase):
    def write_hooks(self, *items):
        self.hook_log.write_text("\n".join(json.dumps(i) for i in items) + "\n", encoding="utf-8")

    def test_attribution_and_runs(self):
        self.write_hooks(
            {"hook": "subagentStart", "recordedAt": "2026-10-14T17:46:00+00:00", "conversationId": "conv-sub",
             "parentConversationId": "conv-main", "subagentId": "sub-123456789", "subagentType": "explore",
             "subagentModel": "grok-4.7-high"},
            {"hook": "subagentStop", "recordedAt": "2026-10-14T17:50:00+00:00", "conversationId": "conv-sub",
             "parentConversationId": "conv-main", "subagentId": "sub-123456789", "status": "completed"},
            {"hook": "subagentStart", "recordedAt": "2026-10-14T17:50:00+00:00", "conversationId": "x",
             "subagentId": "hook-self-test", "subagentType": "hook-self-test"},
        )
        self.run_collect(FakePost({U1: [ok(load("events_page1.json"))]}))
        groups = self.by_model()
        sub_rows = [r for rs in groups.values() for r in rs if r["agent_kind"] == "subagent"]
        self.assertEqual(len(sub_rows), 1)
        self.assertEqual(sub_rows[0]["agent_id"], "conv-sub")
        self.assertEqual(sub_rows[0]["session_id"], "conv-sub")
        prim = [r for rs in groups.values() for r in rs if r["agent_kind"] == "primary"]
        self.assertEqual(len(prim), 4)
        runs = {r["agent_id"]: r for r in self.store.subagent_runs("cursor", 0)}
        self.assertEqual(list(runs), ["sub-123456789"])
        run = runs["sub-123456789"]
        self.assertEqual((run["agent_type"], run["model"], run["effort"], run["status"]),
                         ("explore", "grok-4.7", "high", "completed"))
        self.assertEqual(run["agent_name"], "explore · sub-123")
        self.assertEqual(run["session_id"], "conv-main")

    def test_subagent_id_used_as_conversation_id(self):
        ids = cursor.subagent_conversation_ids([
            {"hook": "subagentStart", "conversationId": "p", "subagentId": "s1"}])
        self.assertEqual(ids, {"s1"})  # parent conversation is not marked as subagent

    def test_missing_hook_log_is_fine(self):
        res = self.run_collect(FakePost({U1: [ok(load("events_page1.json"))]}))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.store.subagent_runs("cursor", 0), [])


if __name__ == "__main__":
    unittest.main()
