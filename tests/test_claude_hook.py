import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "claude_subagent_hook.py"


def line(mid, model, inp, out, cr, cw, **extra):
    o = {"type": "assistant", "message": {"id": mid, "model": model, "usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}
    o.update(extra)
    return json.dumps(o) + "\n"


class TestHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"

    def run_hook(self, payload, raw=None):
        env = dict(os.environ, TOKEN_BURN_DATA_DIR=str(self.data))
        p = subprocess.run([sys.executable, str(HOOK)],
                           input=raw if raw is not None else json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        self.assertEqual(p.stderr, "")
        return p

    def records(self):
        f = self.data / "claude-subagents.jsonl"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text().splitlines()]

    def test_streamed_message_keeps_final_output(self):
        t = self.tmp / "agent-abc.jsonl"
        t.write_text(line("m1", "claude-sonnet-5", 1, 2, 3, 4) + line("m1", "claude-sonnet-5", 1, 50, 3, 4)
                     + line("m1", "claude-sonnet-5", 1, 50, 3, 4))
        self.run_hook({"session_id": "s1", "agent_type": "explore", "agent_id": "abc",
                       "agent_transcript_path": str(t)})
        r = self.records()[0]
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]), (1, 50, 3, 4))

    def test_end_to_end(self):
        t = self.tmp / "agent-abc.jsonl"
        t.write_text(
            line("m1", "claude-sonnet-5", 1, 2, 3, 4, effort="low")
            + line("m1", "claude-sonnet-5", 1, 2, 3, 4)          # duplicate id ignored
            + line("m2", "claude-sonnet-5", 10, 20, 30, 40, perTurnEffort="xhigh")
            + line("m3", "claude-sonnet-5", 100, 200, 300, 400)  # no effort: last seen stays
            + '{"type":"user","message":"secret prompt"}\n')
        self.run_hook({"session_id": "s1", "agent_type": "explore", "agent_id": "abc",
                       "agent_transcript_path": str(t)})
        recs = self.records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["session"], r["agent_type"], r["agent_id"], r["model"], r["effort"]),
                         ("s1", "explore", "abc", "claude-sonnet-5", "extra"))
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]),
                         (111, 222, 333, 444))
        self.assertRegex(r["t"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("secret", json.dumps(r))

    def test_silent_on_bad_input(self):
        self.run_hook(None, raw="not json")
        self.run_hook(None, raw="")
        self.run_hook({"agent_transcript_path": str(self.tmp / "missing.jsonl")})
        self.run_hook({"session_id": "s"})
        self.run_hook([1, 2])
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
