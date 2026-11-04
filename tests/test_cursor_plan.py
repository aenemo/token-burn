import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.plan import cursor_plan

FIX = Path(__file__).resolve().parent / "fixtures" / "cursor"
GET_URL = "https://cursor.com/api/usage-summary"
POST_URL2 = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
POST_URL3 = "https://cursor.com/api/dashboard/get-current-period-usage"


def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


class Net:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers):
        self.calls.append(("GET", url, headers))
        return self.responses.get(url, (500, ""))

    def post(self, url, headers, payload):
        self.calls.append(("POST", url, headers))
        return self.responses.get(url, (500, ""))


class PlanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.debug = Path(self.tmp) / "plan-percent-fields.json"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fetch(self, responses, token="TESTTOKEN"):
        net = Net(responses)
        with mock.patch.object(cursor_plan, "read_token", lambda *a, **k: token), \
                mock.patch.object(cursor_plan, "http_get", net.get), \
                mock.patch.object(cursor_plan, "http_post", net.post), \
                mock.patch.object(cursor_plan, "DEBUG_PATH", self.debug):
            result = cursor_plan.fetch(None)
        return result, net


class TestExtract(unittest.TestCase):
    def test_percent_keys_nested_and_snake_case(self):
        self.assertEqual(cursor_plan.extract_plan_percents(json.loads(fx("plan_summary_percent.json"))), (41.26, 7.0))
        self.assertEqual(cursor_plan.extract_plan_percents({"cursor_models_percent_used": 12, "other_models_percent_used": 3.456}), (12.0, 3.46))

    def test_used_limit_fallback(self):
        self.assertEqual(cursor_plan.extract_plan_percents(json.loads(fx("plan_used_limit.json"))), (12.5, 25.0))

    def test_only_one_pool_and_zero_limit(self):
        self.assertEqual(cursor_plan.extract_plan_percents({"autoPercentUsed": 5}), (5.0, None))
        self.assertEqual(cursor_plan.extract_plan_percents({"otherModelsUsed": 1, "otherModelsLimit": 0}), (None, None))

    def test_list_payload_values(self):
        self.assertEqual(cursor_plan.extract_plan_percents({"items": [{"apiPercentUsed": "9"}]}), (None, 9.0))

    def test_none_for_unknown_shapes(self):
        self.assertEqual(cursor_plan.extract_plan_percents([]), (None, None))
        self.assertEqual(cursor_plan.extract_plan_percents(json.loads(fx("plan_no_percent.json"))), (None, None))

    def test_resets_at(self):
        self.assertEqual(cursor_plan.extract_resets_at(json.loads(fx("plan_summary_percent.json"))), "2026-11-05T00:00:00Z")
        self.assertEqual(cursor_plan.extract_resets_at(json.loads(fx("plan_used_limit.json"))), "2026-11-05T00:00:00Z")
        self.assertIsNone(cursor_plan.extract_resets_at({"a": 1}))


class TestFetch(PlanBase):
    def test_first_endpoint(self):
        (status, pools), net = self.fetch({GET_URL: (200, fx("plan_summary_percent.json"))})
        self.assertEqual(status, "ok")
        self.assertEqual(pools["cursor_models"], {"pct": 41.26, "resets_at": "2026-11-05T00:00:00Z"})
        self.assertEqual(pools["other_models"]["pct"], 7.0)
        self.assertEqual(len(net.calls), 1)
        self.assertTrue(net.calls[0][2]["Cookie"].startswith("WorkosCursorSessionToken="))
        self.assertFalse(self.debug.exists())

    def test_fallback_order(self):
        responses = {
            GET_URL: (200, fx("plan_no_percent.json")),
            POST_URL2: (500, "x"),
            POST_URL3: (200, fx("plan_used_limit.json")),
        }
        (status, pools), net = self.fetch(responses)
        self.assertEqual(status, "ok")
        self.assertEqual([c[1] for c in net.calls], [GET_URL, POST_URL2, POST_URL3])
        self.assertIn("Bearer ", net.calls[1][2]["Authorization"])
        self.assertEqual((pools["cursor_models"]["pct"], pools["other_models"]["pct"]), (12.5, 25.0))

    def test_second_endpoint_wins_and_stops(self):
        (status, pools), net = self.fetch({GET_URL: (403, ""), POST_URL2: (200, fx("plan_summary_percent.json"))})
        self.assertEqual(status, "ok")
        self.assertEqual(len(net.calls), 2)

    def test_no_percent_writes_key_names_only(self):
        (status, pools), _ = self.fetch({GET_URL: (200, fx("plan_no_percent.json"))})
        self.assertIsNone(pools)
        self.assertNotEqual(status, "ok")
        text = self.debug.read_text(encoding="utf-8")
        self.assertIn("planUsage.totalSpend", text)
        self.assertNotIn("SHOULD-NOT-APPEAR", text)
        self.assertNotIn("TESTTOKEN", text)

    def test_unreachable_and_no_token(self):
        (status, pools), _ = self.fetch({})
        self.assertIsNone(pools)
        self.assertIn("unreachable", status)
        (status, pools), net = self.fetch({}, token=None)
        self.assertEqual(status, "Cursor sign-in token not found")
        self.assertEqual(net.calls, [])


if __name__ == "__main__":
    unittest.main()
