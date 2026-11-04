import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from ccu import cli, hook, hooks_register as hr, paths
from ccu.scheduler import get_backend, linux, macos, windows


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"CCU_DATA_DIR": str(self.tmp / "data"),
                                           "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")})
        env.start()
        self.addCleanup(env.stop)
        h = mock.patch.object(Path, "home", return_value=self.tmp / "home")
        h.start()
        self.addCleanup(h.stop)


class TestPaths(unittest.TestCase):
    def test_self_command_frozen(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(paths.self_command("serve"), [sys.executable, "serve"])

    def test_self_command_module(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            cmd = paths.self_command("hook", "claude", windowless=False)
            self.assertEqual(cmd, [sys.executable, "-m", "ccu", "hook", "claude"])

    def test_pythonw_preferred_on_windows(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "pythonw.exe").write_text("")
        with mock.patch.object(sys, "executable", str(d / "python.exe")), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.self_command("serve")[0], str(d / "pythonw.exe"))
            self.assertEqual(paths.self_command("serve", windowless=False)[0], str(d / "python.exe"))

    def test_quote_posix_and_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(paths.quote_command(["/a b/py", "-m", "ccu"]), "'/a b/py' -m ccu")
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.quote_command(["C:/a b/py.exe", "-m", "ccu"]), '"C:/a b/py.exe" -m ccu')

    def test_data_dir_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CCU_DATA_DIR", None)
            with mock.patch.dict(sys.modules, {"collector": None}):
                with mock.patch.object(sys, "platform", "darwin"):
                    self.assertIn("Application Support", str(paths.data_dir()))
                with mock.patch.object(sys, "platform", "linux"):
                    self.assertTrue(str(paths.data_dir()).endswith("claude-cursor-token-usage"))


class TestRender(unittest.TestCase):
    def test_windows_xml(self):
        for kind in ("refresh", "server"):
            x = windows.render_task_xml(["C:\\Py\\pythonw.exe", "-m", "ccu", "collect"], kind,
                                        user="PC\\me", start="2026-01-01T00:00:00")
            root = ET.fromstring(x.replace('encoding="UTF-16"', ""))
            ns = {"t": windows.NS}
            self.assertEqual(root.find(".//t:Hidden", ns).text, "true")
            self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
            self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, "C:\\Py\\pythonw.exe")
            self.assertEqual(root.find(".//t:Exec/t:Arguments", ns).text, "-m ccu collect")
            self.assertIsNotNone(root.find(".//t:LogonTrigger", ns))
            rep = root.find(".//t:TimeTrigger/t:Repetition/t:Interval", ns)
            if kind == "refresh":
                self.assertEqual(rep.text, "PT1M")
            else:
                self.assertIsNone(rep)

    def test_macos_plists(self):
        r = plistlib.loads(macos.render_plist("com.ccu.refresh", ["/x/py", "-m", "ccu", "collect"],
                                              "refresh", "/tmp").encode())
        self.assertEqual(r["StartInterval"], 60)
        self.assertTrue(r["RunAtLoad"])
        self.assertEqual(r["ProgramArguments"][-1], "collect")
        s = plistlib.loads(macos.render_plist("com.ccu.server", ["/x/py", "serve"], "server", "/tmp").encode())
        self.assertTrue(s["KeepAlive"] and s["RunAtLoad"])
        self.assertNotIn("StartInterval", s)

    def test_linux_units(self):
        self.assertIn("OnUnitActiveSec=60s", linux.render_refresh_timer())
        self.assertIn("OnBootSec=30s", linux.render_refresh_timer())
        self.assertIn("ExecStart='/a b/py' collect", linux.render_refresh_service(["/a b/py", "collect"]))
        self.assertIn("Restart=on-failure", linux.render_server_service(["/py", "serve"]))

    def test_cron_merge_idempotent(self):
        lines = linux.render_cron(["/py", "collect"], ["/py", "serve"])
        self.assertTrue(lines[0].startswith("@reboot ") and lines[1].startswith("* * * * * "))
        once = linux.merge_crontab("MAILTO=x\n", lines)
        twice = linux.merge_crontab(once, lines)
        self.assertEqual(once, twice)
        self.assertEqual(linux.merge_crontab(once, []), "MAILTO=x\n")

    def test_backend_selection(self):
        self.assertIs(get_backend("win32"), windows)
        self.assertIs(get_backend("darwin"), macos)
        self.assertIs(get_backend("linux"), linux)

    def test_windows_install_runs_schtasks(self):
        calls = []
        with mock.patch.object(windows, "run", side_effect=lambda a, **k: calls.append(a) or (0, "")):
            msgs = windows.install(["py", "-m", "ccu", "collect"], ["py", "-m", "ccu", "serve"])
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(c[0] == "schtasks" and "/XML" in c for c in calls))


class TestHooksRegister(Tmp):
    CMD = "py -m ccu hook claude"

    def test_claude_idempotent_preserves_and_uninstalls(self):
        s = paths.claude_dir() / "settings.json"
        s.parent.mkdir(parents=True)
        other = {"hooks": [{"type": "command", "command": "echo hi"}]}
        s.write_text(json.dumps({"theme": "dark", "hooks": {"SubagentStop": [other], "Stop": [other]}}))
        self.assertEqual(hr.install_claude(self.CMD), "installed")
        first = s.read_text()
        self.assertEqual(hr.install_claude(self.CMD), "already installed")
        self.assertEqual(s.read_text(), first)
        cfg = json.loads(first)
        self.assertEqual(len(cfg["hooks"]["SubagentStop"]), 2)
        self.assertEqual(cfg["theme"], "dark")
        self.assertTrue(list(s.parent.glob("settings.json.bak-ccu-*")))
        self.assertEqual(hr.uninstall_claude(), "removed")
        cfg = json.loads(s.read_text())
        self.assertEqual(cfg["hooks"], {"SubagentStop": [other], "Stop": [other]})
        self.assertEqual(hr.uninstall_claude(), "not present")

    def test_claude_keeps_original_app_entry(self):
        s = paths.claude_dir() / "settings.json"
        s.parent.mkdir(parents=True)
        legacy = {"hooks": [{"type": "command", "command": '"C:\\py.exe" "C:\\x\\hooks\\claude_subagent_hook.py"'}]}
        s.write_text(json.dumps({"hooks": {"SubagentStop": [legacy]}}))
        hr.install_claude(self.CMD)
        cfg = json.loads(s.read_text())
        self.assertEqual([h["hooks"][0]["command"] for h in cfg["hooks"]["SubagentStop"]], [legacy["hooks"][0]["command"], self.CMD])

    def test_claude_invalid_json_untouched(self):
        s = paths.claude_dir() / "settings.json"
        s.parent.mkdir(parents=True)
        s.write_text("{not json")
        with self.assertRaises(ValueError):
            hr.install_claude(self.CMD)
        self.assertEqual(s.read_text(), "{not json")

    def test_cursor_roundtrip(self):
        j = paths.cursor_hooks_path()
        j.parent.mkdir(parents=True)
        keep = {"command": "other.sh"}
        j.write_text(json.dumps({"version": 1, "hooks": {"subagentStop": [keep], "stop": [keep]}}))
        cmd = "py -m ccu hook cursor"
        self.assertEqual(hr.install_cursor(cmd), "installed")
        self.assertEqual(hr.install_cursor(cmd), "already installed")
        cfg = json.loads(j.read_text())
        self.assertEqual(len(cfg["hooks"]["subagentStop"]), 2)
        self.assertEqual(cfg["hooks"]["subagentStart"], [{"command": cmd, "timeout": 15}])
        self.assertEqual(hr.status()["cursor"], True)
        self.assertEqual(hr.uninstall_cursor(), "removed")
        cfg = json.loads(j.read_text())
        self.assertEqual(cfg["hooks"], {"subagentStop": [keep], "stop": [keep]})
        self.assertEqual(hr.uninstall_cursor(), "not present")

    def test_is_ours(self):
        self.assertTrue(hr.is_ours('"C:/Py/pythonw.exe" -m ccu hook cursor'))
        self.assertTrue(hr.is_ours("/opt/ccu/ccu hook claude"))
        self.assertFalse(hr.is_ours('powershell -File "C:\\u\\.cursor\\hooks\\ccu-subagent-hook.ps1"'))
        self.assertFalse(hr.is_ours("echo hook claude"))


class TestCursorHook(Tmp):
    def run_hook(self, raw):
        out = io.StringIO()
        self.assertEqual(hook.run_cursor(raw, out), 0)
        return out.getvalue()

    def log(self):
        f = self.tmp / "data" / "cursor-subagent-events.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_start_event(self):
        ev = {"hook_event_name": "subagentStart", "conversation_id": "c1", "model": "m",
              "subagent_id": "s1", "subagent_type": "explore", "subagent_model": "sm",
              "parent_conversation_id": "p1", "task": "SECRET TEXT", "user_email": "a@b.c"}
        self.assertEqual(self.run_hook(json.dumps(ev)), '{"permission":"allow"}\n')
        rec = self.log()[0]
        self.assertEqual(list(rec), ["hook", "recordedAt", "conversationId", "parentModel", "subagentId",
                                     "subagentType", "subagentModel", "parentConversationId", "status",
                                     "durationMs", "messageCount", "toolCallCount"])
        self.assertEqual(rec["conversationId"], "c1")
        self.assertEqual(rec["status"], "")
        self.assertIsNone(rec["durationMs"])
        self.assertRegex(rec["recordedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        raw = (self.tmp / "data" / "cursor-subagent-events.jsonl").read_text()
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("a@b.c", raw)

    def test_stop_event(self):
        ev = {"hook_event_name": "subagentStop", "subagent_id": "s1", "status": "completed",
              "duration_ms": 1200, "message_count": 4, "tool_call_count": 3}
        self.assertEqual(self.run_hook(json.dumps(ev)), "{}\n")
        rec = self.log()[0]
        self.assertEqual((rec["durationMs"], rec["messageCount"], rec["toolCallCount"]), (1200, 4, 3))

    def test_empty_and_garbage_never_raise(self):
        self.assertEqual(self.run_hook(""), '{"permission":"allow"}\n')
        self.assertEqual(self.run_hook("{garbage"), '{"permission":"allow"}\n')
        self.assertEqual(self.log(), [])

    def test_write_failure_still_replies(self):
        with mock.patch.object(hook, "append_line", side_effect=OSError("locked")):
            self.assertEqual(self.run_hook('{"hook_event_name":"subagentStop"}'), "{}\n")

    def test_append_retries(self):
        real = open
        n = {"c": 0}

        def flaky(*a, **k):
            n["c"] += 1
            if n["c"] < 3:
                raise PermissionError("busy")
            return real(*a, **k)
        with mock.patch("builtins.open", flaky), mock.patch.object(hook.time, "sleep"):
            hook.append_line(self.tmp / "x.jsonl", "{}")
        self.assertEqual((self.tmp / "x.jsonl").read_text(), "{}\n")


class TestClaudeHook(Tmp):
    def test_record(self):
        t = self.tmp / "agent.jsonl"
        t.write_text(json.dumps({"type": "assistant", "message": {"id": "a", "model": "m", "usage": {
            "input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4}}}) + "\n")
        hook.run_claude(json.dumps({"agent_transcript_path": str(t), "session_id": "s", "agent_id": "x"}))
        f = self.tmp / "data" / "claude-subagents.jsonl"
        rec = json.loads(f.read_text())
        self.assertEqual((rec["input"], rec["output"], rec["cache_read"], rec["cache_write"]), (1, 2, 3, 4))
        self.assertEqual(rec["model"], "m")

    def test_garbage_ok(self):
        self.assertEqual(hook.run_claude("nope"), 0)


class TestCli(Tmp):
    def test_parse(self):
        p = cli.build_parser()
        a = p.parse_args(["install", "--no-open", "--no-start"])
        self.assertTrue(a.no_open and a.no_start)
        self.assertTrue(p.parse_args(["uninstall", "--purge-data"]).purge_data)
        self.assertTrue(p.parse_args(["collect", "--loop"]).loop)
        self.assertEqual(p.parse_args(["hook", "cursor"]).kind, "cursor")
        with self.assertRaises(SystemExit):
            p.parse_args(["hook", "bogus"])
        with self.assertRaises(SystemExit):
            p.parse_args([])

    def test_hook_cli_exit_zero(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b""))):
            self.assertEqual(cli.main(["hook", "cursor"]), 0)
        self.assertEqual(out.getvalue(), '{"permission":"allow"}\n')

    def test_install_uninstall_flow(self):
        be = mock.MagicMock()
        be.install.return_value = ["ok"]
        be.uninstall.return_value = ["gone"]
        with mock.patch.object(cli, "get_backend", return_value=be), \
                mock.patch.object(cli.proc, "stop_server", return_value=False), \
                mock.patch.object(cli.webbrowser, "open") as op:
            self.assertEqual(cli.main(["install", "--no-open", "--no-start"]), 0)
            op.assert_not_called()
            be.install.assert_called_once()
            self.assertEqual(be.install.call_args[0][0][-1], "collect")
            self.assertEqual(be.install.call_args[0][1][-1], "serve")
            self.assertTrue(hr.status()["claude"] and hr.status()["cursor"])
            self.assertEqual(cli.main(["uninstall"]), 0)
            self.assertFalse(hr.status()["claude"] or hr.status()["cursor"])

    def test_install_opens_browser(self):
        be = mock.MagicMock()
        be.install.return_value = []
        be.start_server.return_value = True
        with mock.patch.object(cli, "get_backend", return_value=be), \
                mock.patch.object(cli.proc, "stop_server"), \
                mock.patch.object(cli.proc, "wait_health", return_value=True), \
                mock.patch.object(cli.webbrowser, "open") as op:
            cli.main(["install"])
            op.assert_called_once_with(paths.URL)

    def test_purge_data(self):
        d = self.tmp / "data"
        d.mkdir()
        (d / "usage.db").write_text("x")
        be = mock.MagicMock()
        be.uninstall.return_value = []
        with mock.patch.object(cli, "get_backend", return_value=be), \
                mock.patch.object(cli.proc, "stop_server", return_value=False):
            cli.main(["uninstall", "--purge-data"])
        self.assertFalse(d.exists())


class TestCollectArgs(unittest.TestCase):
    def test_collect_passes_explicit_argv(self):
        from unittest import mock
        from ccu import cli
        with mock.patch("collector.collect.main", return_value=0) as m:
            cli.main(["collect"])
            m.assert_called_with([])
            cli.main(["collect", "--loop"])
            m.assert_called_with(["--loop"])


if __name__ == "__main__":
    unittest.main()
