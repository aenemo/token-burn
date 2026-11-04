import tempfile
import unittest
from pathlib import Path

from collector.plan import maybe_lookup
from collector.store import Store
from collector.util import PLAN_INTERVAL_S, iso

POOLS = {"five_hour": {"pct": 9.0, "resets_at": "2026-10-21T06:29:59Z"}}


class Fetcher:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, log):
        self.calls += 1
        r = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(r, Exception):
            raise r
        return r


class PlanThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_first_call_samples_and_reports(self):
        f = Fetcher(("ok", POOLS))
        r = maybe_lookup(self.store, "claude", f, 10000.0, None)
        self.assertEqual(f.calls, 1)
        self.assertEqual(r, {"status": "ok", "last_lookup": iso(10000), "next_lookup": iso(10590)})
        self.assertEqual(self.store.plan_samples("claude", 0), [(10000.0, POOLS)])

    def test_no_call_within_interval(self):
        f = Fetcher(("ok", POOLS))
        maybe_lookup(self.store, "claude", f, 10000.0, None)
        for dt_ in (1, 60, 300, PLAN_INTERVAL_S - 1):
            r = maybe_lookup(self.store, "claude", f, 10000.0 + dt_, None)
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["last_lookup"], iso(10000))
        self.assertEqual(f.calls, 1)
        self.assertEqual(len(self.store.plan_samples("claude", 0)), 1)

    def test_call_after_interval(self):
        f = Fetcher(("ok", POOLS))
        maybe_lookup(self.store, "claude", f, 10000.0, None)
        r = maybe_lookup(self.store, "claude", f, 10000.0 + PLAN_INTERVAL_S, None)
        self.assertEqual(f.calls, 2)
        self.assertEqual(r["last_lookup"], iso(10590))
        self.assertEqual(len(self.store.plan_samples("claude", 0)), 2)

    def test_failure_retries_next_minute(self):
        f = Fetcher(("plan API unreachable (offline?)", None), ("ok", POOLS))
        r = maybe_lookup(self.store, "claude", f, 10000.0, None)
        self.assertEqual(r, {"status": "plan API unreachable (offline?)",
                             "last_lookup": None, "next_lookup": None})
        r = maybe_lookup(self.store, "claude", f, 10060.0, None)
        self.assertEqual(f.calls, 2)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["last_lookup"], iso(10060))

    def test_failure_after_success_keeps_last_ok(self):
        f = Fetcher(("ok", POOLS), ("sign-in expired, run claude once", None))
        maybe_lookup(self.store, "claude", f, 10000.0, None)
        t = 10000.0 + PLAN_INTERVAL_S
        r = maybe_lookup(self.store, "claude", f, t, None)
        self.assertEqual(r["status"], "sign-in expired, run claude once")
        self.assertEqual(r["last_lookup"], iso(10000))
        maybe_lookup(self.store, "claude", f, t + 60, None)  # still due, so retries
        self.assertEqual(f.calls, 3)

    def test_exception_is_a_failure(self):
        f = Fetcher(ValueError("boom"))
        r = maybe_lookup(self.store, "cursor", f, 10000.0, None)
        self.assertEqual(r["status"], "plan check failed (ValueError)")
        self.assertIsNone(r["last_lookup"])

    def test_platforms_throttled_independently(self):
        f1, f2 = Fetcher(("ok", POOLS)), Fetcher(("ok", POOLS))
        maybe_lookup(self.store, "claude", f1, 10000.0, None)
        maybe_lookup(self.store, "cursor", f2, 10001.0, None)
        self.assertEqual((f1.calls, f2.calls), (1, 1))

    def test_state_persists_across_store_instances(self):
        f = Fetcher(("ok", POOLS))
        maybe_lookup(self.store, "claude", f, 10000.0, None)
        s2 = Store(Path(self.tmp.name) / "t.db")
        try:
            maybe_lookup(s2, "claude", f, 10100.0, None)
        finally:
            s2.close()
        self.assertEqual(f.calls, 1)


if __name__ == "__main__":
    unittest.main()
