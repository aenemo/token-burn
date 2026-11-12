"""Standalone tool readers (Gemini CLI, Qwen Code, Copilot CLI, Grok Build, Mistral Vibe).

The fixtures follow the documented layouts of those tools, not recorded real files.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from collector import collect, providers, util
from collector.adapters import tools
from collector.store import Store

NOW = time.time()


def iso(t):
    return util.iso(t)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        p = mock.patch.dict(os.environ, {"TOKEN_BURN_TOOLS_HOME": str(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)

    def write(self, rel, lines, mode="w"):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(x) + "\n" for x in lines) if isinstance(lines, list) else json.dumps(lines)
        with open(p, mode, encoding="utf-8") as f:
            f.write(text)
        return p

    def run_reader(self, plat):
        return tools.collect_platform(plat, self.store, NOW, None)

    def rows(self, plat):
        return [dict(r) for r in self.store.usage_rows(plat, 0)]

    def totals(self, plat):
        r = self.rows(plat)
        return tuple(sum(x[k] for x in r) for k in ("input", "output", "cache_read", "cache_write", "requests"))


class ExtractTests(unittest.TestCase):
    def test_cached_input_is_not_counted_twice(self):
        self.assertEqual(tools.extract({"input": 12000, "output": 300, "cached": 9000, "thoughts": 150, "tool": 0}),
                         (3000, 450, 9000, 0))

    def test_anthropic_style_input_is_already_uncached(self):
        self.assertEqual(tools.extract({"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 900,
                                        "cache_creation_input_tokens": 50}), (100, 5, 900, 50))

    def test_nothing_found(self):
        for bad in (None, {}, {"text": "hi"}, {"input": True}, {"input": -5}, [], "x"):
            self.assertIsNone(tools.extract(bad))

    def test_nested_usage_is_found(self):
        got = tools.find_usage({"type": "assistant", "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4,
                                                                       "cachedContentTokenCount": 2}})
        self.assertEqual(got, (8, 4, 2, 0))


class GeminiTests(Base):
    def test_jsonl_session(self):
        self.write(".gemini/tmp/abc/chats/session-1.jsonl", [
            {"sessionId": "S1", "startTime": iso(NOW - 600)},
            {"id": "m1", "type": "user", "timestamp": iso(NOW - 500), "content": "hello"},
            {"id": "m2", "type": "gemini", "timestamp": iso(NOW - 400), "model": "gemini-3-pro",
             "tokens": {"input": 1000, "output": 100, "cached": 400, "thoughts": 50, "tool": 0, "total": 1150}},
        ])
        self.run_reader("google")
        self.assertEqual(self.totals("google"), (600, 150, 400, 0, 1))
        r = self.rows("google")[0]
        self.assertEqual((r["model"], r["session_id"], r["cost"]), ("gemini-3-pro", "S1", 0))

    def test_second_pass_adds_nothing_and_new_lines_are_added(self):
        p = self.write(".gemini/tmp/abc/chats/session-1.jsonl", [
            {"id": "m2", "timestamp": iso(NOW - 400), "model": "g", "tokens": {"input": 10, "output": 5}}])
        self.run_reader("google")
        self.run_reader("google")
        self.assertEqual(self.totals("google")[4], 1)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "m3", "timestamp": iso(NOW - 300), "model": "g",
                                "tokens": {"input": 20, "output": 6}}) + "\n")
        self.run_reader("google")
        self.assertEqual(self.totals("google"), (30, 11, 0, 0, 2))

    def test_half_written_line_waits(self):
        p = self.write(".gemini/tmp/abc/chats/session-1.jsonl", [
            {"id": "a", "timestamp": iso(NOW - 400), "model": "g", "tokens": {"input": 10, "output": 5}}])
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"id": "b", "tokens": {"input": 7,')
        self.run_reader("google")
        self.assertEqual(self.totals("google")[4], 1)
        with open(p, "a", encoding="utf-8") as f:
            f.write(' "output": 3}, "model": "g", "timestamp": "%s"}\n' % iso(NOW - 100))
        self.run_reader("google")
        self.assertEqual(self.totals("google"), (17, 8, 0, 0, 2))

    def test_old_json_session(self):
        self.write(".gemini/tmp/abc/chats/session-2.json", {"sessionId": "S2", "messages": [
            {"id": "a", "type": "gemini", "timestamp": iso(NOW - 90), "model": "gemini-x",
             "tokens": {"input": 50, "output": 10, "cached": 0, "thoughts": 0, "tool": 0}},
            {"id": "b", "type": "user", "content": "hi"}]})
        self.run_reader("google")
        self.assertEqual(self.totals("google"), (50, 10, 0, 0, 1))

    def test_old_records_are_ignored(self):
        self.write(".gemini/tmp/abc/chats/session-3.jsonl", [
            {"id": "a", "timestamp": iso(NOW - 40 * 86400), "model": "g", "tokens": {"input": 10, "output": 5}}])
        self.run_reader("google")
        self.assertEqual(self.rows("google"), [])

    def test_missing_folder_is_fine(self):
        self.assertEqual(self.run_reader("google")["status"], "ok")


class QwenTests(Base):
    def test_ledger_is_preferred_over_chat_logs(self):
        self.write(".qwen/usage/token-usage-2026-11.jsonl", [
            {"timestamp": iso(NOW - 100), "sessionId": "q1", "model": "qwen3-coder", "authType": "qwen-oauth",
             "inputTokens": 100, "outputTokens": 20, "cachedTokens": 30, "thoughtsTokens": 5}])
        self.write(".qwen/projects/-work-app/chats/q1.jsonl", [
            {"type": "assistant", "timestamp": iso(NOW - 100), "sessionId": "q1", "model": "qwen3-coder",
             "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20}}])
        self.run_reader("qwen")
        self.assertEqual(self.totals("qwen"), (70, 25, 30, 0, 1))

    def test_chat_logs_without_a_ledger(self):
        self.write(".qwen/projects/-work-app/chats/q2.jsonl", [
            {"type": "user", "timestamp": iso(NOW - 120), "sessionId": "q2", "message": {"role": "user"}},
            {"type": "assistant", "timestamp": iso(NOW - 100), "sessionId": "q2", "model": "qwen3-coder-plus",
             "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 8, "cachedContentTokenCount": 10}},
            {"type": "system", "subtype": "api_response", "timestamp": iso(NOW - 99), "sessionId": "q2",
             "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 8}}])
        self.run_reader("qwen")
        self.assertEqual(self.totals("qwen"), (30, 8, 10, 0, 1))


class CopilotTests(Base):
    def session(self, sid, events, age=0):
        p = self.write(".copilot/session-state/%s/events.jsonl" % sid, events)
        if age:
            os.utime(p, (NOW - age, NOW - age))
        return p

    def test_shutdown_totals_per_model(self):
        self.session("c1", [
            {"type": "session.start", "timestamp": iso(NOW - 900), "data": {"selectedModel": "gpt-5"}},
            {"type": "assistant.message", "timestamp": iso(NOW - 800), "data": {"outputTokens": 40}},
            {"type": "session.shutdown", "timestamp": iso(NOW - 700), "data": {"modelMetrics": {
                "gpt-5": {"requests": {"count": 3}, "usage": {"inputTokens": 1000, "outputTokens": 200,
                                                              "cacheReadTokens": 600, "cacheWriteTokens": 0}},
                "claude-sonnet-4": {"usage": {"inputTokens": 50, "outputTokens": 5}}}}}])
        self.run_reader("copilot")
        self.assertEqual(self.totals("copilot"), (450, 205, 600, 0, 2))
        self.assertEqual({r["model"] for r in self.rows("copilot")}, {"gpt-5", "claude-sonnet-4"})
        self.run_reader("copilot")
        self.assertEqual(self.totals("copilot")[4], 2)

    def test_open_session_waits_for_its_shutdown(self):
        self.session("c2", [
            {"type": "session.start", "timestamp": iso(NOW - 900), "data": {"selectedModel": "gpt-5"}},
            {"type": "assistant.message", "timestamp": iso(NOW - 800), "data": {"outputTokens": 40}}])
        self.run_reader("copilot")
        self.assertEqual(self.rows("copilot"), [])

    def test_abandoned_session_counts_message_output(self):
        self.session("c3", [
            {"type": "session.start", "timestamp": iso(NOW - 5 * 86400), "data": {"selectedModel": "gpt-5"}},
            {"type": "assistant.message", "timestamp": iso(NOW - 5 * 86400), "data": {"outputTokens": 40}},
            {"type": "assistant.message", "timestamp": iso(NOW - 5 * 86400), "data": {"outputTokens": 2}}],
            age=2 * 86400)
        self.run_reader("copilot")
        self.run_reader("copilot")
        self.assertEqual(self.totals("copilot"), (0, 42, 0, 0, 1))


class GrokTests(Base):
    def test_updates_with_usage(self):
        d = ".grok/sessions/-work-app/g1/"
        self.write(d + "summary.json", {"agent_name": "grok", "current_model_id": "grok-build-1"})
        self.write(d + "updates.jsonl", [
            {"timestamp": iso(NOW - 300), "update": {"sessionUpdate": "agent_message_chunk", "content": "hi"}},
            {"timestamp": iso(NOW - 200), "update": {"sessionUpdate": "turn_end"},
             "_meta": {"usage": {"inputTokens": 500, "outputTokens": 60, "cachedTokens": 100}}}])
        self.write(d + "signals.json", {"tokens": {"input": 500, "output": 60}})
        self.run_reader("xai")
        self.assertEqual(self.totals("xai"), (400, 60, 100, 0, 1))
        self.assertEqual(self.rows("xai")[0]["model"], "grok-build-1")
        self.run_reader("xai")
        self.assertEqual(self.totals("xai")[4], 1)

    def test_signals_are_cumulative(self):
        d = ".grok/sessions/-work-app/g2/"
        sig = self.write(d + "signals.json", {"tokens": {"input": 100, "output": 10}})
        self.run_reader("xai")
        self.assertEqual(self.totals("xai"), (100, 10, 0, 0, 1))
        self.write(d + "signals.json", {"tokens": {"input": 160, "output": 25}})
        os.utime(sig, (NOW + 5, NOW + 5))
        self.run_reader("xai")
        self.assertEqual(self.totals("xai"), (160, 25, 0, 0, 2))


class VibeTests(Base):
    def test_meta_counters_are_cumulative(self):
        d = ".vibe/logs/session/session_20261110_abc/"
        meta = self.write(d + "meta.json", {"session_id": "v1", "config": {"model": "devstral-2"},
                                            "stats": {"session_prompt_tokens": 1000, "session_completion_tokens": 100,
                                                      "session_cached_tokens": 400}})
        self.run_reader("mistral")
        self.assertEqual(self.totals("mistral"), (600, 100, 400, 0, 1))
        self.run_reader("mistral")
        self.assertEqual(self.totals("mistral")[4], 1)
        self.write(d + "meta.json", {"session_id": "v1", "config": {"model": "devstral-2"},
                                     "stats": {"session_prompt_tokens": 1500, "session_completion_tokens": 160,
                                               "session_cached_tokens": 600}})
        os.utime(meta, (NOW + 5, NOW + 5))
        self.run_reader("mistral")
        self.assertEqual(self.totals("mistral"), (900, 160, 600, 0, 2))
        self.assertEqual(self.rows("mistral")[0]["model"], "devstral-2")

    def test_journal_events(self):
        self.write(".vibe/logs/session/unified/v2/events.jsonl", [
            {"type": "reply", "timestamp": iso(NOW - 100), "model": "mistral-large",
             "usage": {"input_tokens": 80, "output_tokens": 12, "cached_input_tokens": 20, "reasoning_tokens": 3}},
            {"type": "tool", "timestamp": iso(NOW - 90)}])
        self.run_reader("mistral")
        self.assertEqual(self.totals("mistral"), (60, 12, 20, 0, 1))


class WiringTests(Base):
    def test_collect_run_shows_a_column_only_for_used_tools(self):
        self.write(".gemini/tmp/abc/chats/session-1.jsonl", [
            {"id": "m", "timestamp": iso(NOW - 60), "model": "gemini-3-pro",
             "tokens": {"input": 10, "output": 5}}])
        entries = [e for e in collect._platforms() if e[0] in providers.PLATFORMS]
        d = Path(self.tmp) / "d"
        with mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                mock.patch.object(util, "USAGE_JSON", d / "usage.json"):
            plats = collect.run(now=NOW, platforms=entries, log=mock.Mock())["platforms"]
        self.assertTrue(plats["google"]["installed"])
        self.assertFalse(plats["google"]["meta"].get("cost_reported", True))
        self.assertEqual({n for n in plats} & set(providers.PLATFORMS), {"google"})

    def test_a_broken_reader_does_not_stop_collect(self):
        entries = [e for e in collect._platforms() if e[0] in providers.PLATFORMS]
        d = Path(self.tmp) / "d"
        with mock.patch.object(tools, "gemini", side_effect=RuntimeError("boom")), \
                mock.patch.dict(tools.READERS, {"google": (tools.gemini,)}), \
                mock.patch.object(util, "DATA_DIR", d), mock.patch.object(util, "DB_PATH", d / "usage.db"), \
                mock.patch.object(util, "USAGE_JSON", d / "usage.json"):
            usage = collect.run(now=NOW, platforms=entries, log=mock.Mock())
        self.assertIsNotNone(usage)

    def test_every_provider_reader_belongs_to_a_known_platform(self):
        self.assertLessEqual(set(tools.READERS), set(providers.PLATFORMS))


if __name__ == "__main__":
    unittest.main()
