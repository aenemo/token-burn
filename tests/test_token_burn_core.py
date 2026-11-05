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

from token_burn import cli, hook, hooks_register as hr, paths
from tests.wp3_support import Isolated
from token_burn.scheduler import get_backend, linux, macos, windows


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"TOKEN_BURN_DATA_DIR": str(self.tmp / "data"),
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
            self.assertEqual(cmd, [sys.executable, "-m", "token_burn", "hook", "claude"])

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
            self.assertEqual(paths.quote_command(["/a b/py", "-m", "token_burn"]), "'/a b/py' -m token_burn")
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.quote_command(["C:/a b/py.exe", "-m", "token_burn"]), '"C:/a b/py.exe" -m token_burn')

    def test_data_dir_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOKEN_BURN_DATA_DIR", None)
            with mock.patch.dict(sys.modules, {"collector": None}):
                with mock.patch.object(sys, "platform", "darwin"):
                    self.assertIn("Application Support", str(paths.data_dir()))
                with mock.patch.object(sys, "platform", "linux"):
                    self.assertTrue(str(paths.data_dir()).endswith("claude-cursor-token-usage"))


class TestRender(unittest.TestCase):
    def test_windows_xml(self):
        for kind in ("refresh", "server"):
            x = windows.render_task_xml(["C:\\Py\\pythonw.exe", "-m", "token_burn", "collect"], kind,
                                        user="PC\\me", start="2026-01-01T00:00:00")
            root = ET.fromstring(x.replace('encoding="UTF-16"', ""))
            ns = {"t": windows.NS}
            self.assertEqual(root.find(".//t:Hidden", ns).text, "true")
            self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
            self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, "C:\\Py\\pythonw.exe")
            self.assertEqual(root.find(".//t:Exec/t:Arguments", ns).text, "-m token_burn collect")
            self.assertIsNotNone(root.find(".//t:LogonTrigger", ns))
            rep = root.find(".//t:TimeTrigger/t:Repetition/t:Interval", ns)
            if kind == "refresh":
                self.assertEqual(rep.text, "PT1M")
            else:
                self.assertIsNone(rep)

    def test_macos_plists(self):
        r = plistlib.loads(macos.render_plist("com.token-burn.refresh", ["/x/py", "-m", "token_burn", "collect"],
                                              "refresh", "/tmp").encode())
        self.assertEqual(r["StartInterval"], 60)
        self.assertTrue(r["RunAtLoad"])
        self.assertEqual(r["ProgramArguments"][-1], "collect")
        s = plistlib.loads(macos.render_plist("com.token-burn.server", ["/x/py", "serve"], "server", "/tmp").encode())
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
            msgs = windows.install(["py", "-m", "token_burn", "collect"], ["py", "-m", "token_burn", "serve"])
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(c[0] == "schtasks" and "/XML" in c for c in calls))


class TestHooksRegister(Tmp):
    CMD = "py -m token_burn hook claude"

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
        self.assertTrue(list(s.parent.glob("settings.json.bak-token-burn-*")))
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
        cmd = "py -m token_burn hook cursor"
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
        self.assertTrue(hr.is_ours('"C:/Py/pythonw.exe" -m token_burn hook cursor'))
        self.assertTrue(hr.is_ours("/opt/token-burn/token-burn hook claude"))
        self.assertFalse(hr.is_ours('powershell -File "C:\\u\\.cursor\\hooks\\token-burn-subagent-hook.ps1"'))
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


class TestCli(Isolated):
    def test_parse(self):
        p = cli.build_parser()
        a = p.parse_args(["install", "--no-open", "--no-start"])
        self.assertTrue(a.no_open and a.no_start)
        self.assertTrue(p.parse_args(["uninstall", "--purge-data"]).purge_data)
        self.assertTrue(p.parse_args(["collect", "--loop"]).loop)
        self.assertEqual(p.parse_args(["hook", "cursor"]).kind, "cursor")
        with self.assertRaises(SystemExit):
            p.parse_args(["hook", "bogus"])
        self.assertIsNone(p.parse_args([]).cmd)

    def test_parse_install_flags(self):
        a = cli.build_parser().parse_args(
            ["install", "--install-dir", "A", "--data-dir", "B", "--sync-folder", "C", "--join",
             "--machine-name", "N", "--adopt", "O", "--yes", "--no-open", "--no-start"])
        self.assertEqual((a.install_dir, a.data_dir, a.sync_folder, a.machine_name, a.adopt),
                         ("A", "B", "C", "N", "O"))
        self.assertTrue(a.join and a.yes and not a.new and not a.no_sync)
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["install", "--new", "--join"])
        u = cli.build_parser().parse_args(["uninstall", "--remove-from-shared-folder", "--interactive"])
        self.assertTrue(u.remove_from_shared_folder and u.interactive and not u.purge_data)

    def test_parse_sync_and_data(self):
        p = cli.build_parser()
        self.assertEqual(p.parse_args(["sync", "status"]).sync_cmd, "status")
        e = p.parse_args(["sync", "enable", "--folder", "F", "--new", "--machine-name", "M", "--yes"])
        self.assertEqual((e.folder, e.machine_name, e.new, e.yes), ("F", "M", True, True))
        self.assertTrue(p.parse_args(["sync", "disable", "--remove-my-data", "--yes"]).remove_my_data)
        r = p.parse_args(["sync", "rename", "New", "--machine", "Old"])
        self.assertEqual((r.new_name, r.machine), ("New", "Old"))
        for c in ("retire", "unretire", "remove"):
            self.assertEqual(p.parse_args(["sync", c, "X"]).name, "X")
        for c in ("machines", "reset-id", "now"):
            self.assertEqual(p.parse_args(["sync", c]).sync_cmd, c)
        self.assertEqual(p.parse_args(["data", "move", "D", "--yes"]).folder, "D")
        self.assertEqual(p.parse_args(["data", "status"]).data_cmd, "status")
        with self.assertRaises(SystemExit):
            p.parse_args(["sync"])

    def test_no_args_not_frozen_prints_help(self):
        out = io.StringIO()
        with mock.patch.object(sys, "frozen", False, create=True), mock.patch.object(sys, "stdout", out), \
                mock.patch("token_burn.installer.run_wizard_and_pause") as wiz:
            self.assertEqual(cli.main([]), 0)
        wiz.assert_not_called()
        self.assertIn("usage: token-burn", out.getvalue())
        self.assertIn("install", out.getvalue())

    def test_no_args_frozen_runs_wizard(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch("token_burn.installer.run_wizard_and_pause", return_value=0) as wiz:
            self.assertEqual(cli.main([]), 0)
        wiz.assert_called_once()
        self.assertEqual(wiz.call_args[0][0].cmd, "install")

    def test_hook_cli_exit_zero(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b""))):
            self.assertEqual(cli.main(["hook", "cursor"]), 0)
        self.assertEqual(out.getvalue(), '{"permission":"allow"}\n')

    def test_install_uninstall_flow(self):
        self.assertEqual(cli.main(["install", "--no-open", "--no-start"]), 0)
        self.browser.assert_not_called()
        self.backend.install.assert_called_once()
        self.assertEqual(self.backend.install.call_args[0][0][-1], "collect")
        self.assertEqual(self.backend.install.call_args[0][1][-1], "serve")
        self.assertTrue(hr.status()["claude"] and hr.status()["cursor"])
        self.assertEqual(cli.main(["uninstall"]), 0)
        self.assertFalse(hr.status()["claude"] or hr.status()["cursor"])

    def test_install_opens_browser(self):
        cli.main(["install"])
        self.browser.assert_called_once_with(paths.URL)

    def test_install_never_prompts_without_a_terminal(self):
        with mock.patch("builtins.input", side_effect=AssertionError("input() called")):
            self.assertEqual(cli.main(["install", "--no-open", "--no-start"]), 0)
            self.assertEqual(cli.main(["uninstall"]), 0)

    def test_purge_data(self):
        d = self.data
        d.mkdir()
        (d / "usage.db").write_text("x")
        cli.main(["uninstall", "--purge-data"])
        self.assertFalse(d.exists())

    def test_uninstall_keeps_data_by_default(self):
        self.data.mkdir()
        (self.data / "usage.db").write_text("x")
        cli.main(["uninstall"])
        self.assertTrue((self.data / "usage.db").exists())


class TestWindowsTaskStatus(unittest.TestCase):
    BS = chr(92)
    HEADER = '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author"'

    def csv_row(self, last_run, result, status="Ready"):
        task = self.BS + "TokenBurn" + self.BS + "Refresh"
        return ('"PC1","%s","6/11/2026 9:01:00 AM","%s","Interactive only","%s","%s","me"'
                % (task, status, last_run, result))

    def fake_run(self, rows, calls=None):
        """schtasks stub: /Query returns the next CSV row (the last one repeats), /Run succeeds."""
        rows = list(rows)

        def run(argv, **kw):
            if calls is not None:
                calls.append(argv)
            if "/Run" in argv:
                return 0, "SUCCESS"
            row = rows.pop(0) if len(rows) > 1 else rows[0]
            return 0, row + chr(10)
        return run

    def test_last_result_is_column_six_not_last_run_time(self):
        row = self.csv_row("6/11/2026 9:00:03 AM", "267009")
        with mock.patch.object(windows, "run", return_value=(0, row)):
            self.assertEqual(windows.last_result(windows.REFRESH), 267009)
            self.assertEqual(windows.task_state(windows.REFRESH), ("6/11/2026 9:00:03 AM", 267009))

    def test_negative_hex_and_text_results(self):
        self.assertEqual(windows.parse_result("-2147024894"), -2147024894)
        self.assertEqual(windows.parse_result("0x80070002"), 0x80070002)
        self.assertEqual(windows.parse_result("0"), 0)
        self.assertIsNone(windows.parse_result("N/A"))
        self.assertIsNone(windows.parse_result("6/11/2026 9:00:03 AM"))
        row = self.csv_row("6/11/2026 9:00:03 AM", "Nicht zutreffend")
        with mock.patch.object(windows, "run", return_value=(0, row)):
            self.assertIsNone(windows.last_result(windows.REFRESH))

    def test_failed_query_and_short_row(self):
        with mock.patch.object(windows, "run", return_value=(1, "ERROR")):
            self.assertIsNone(windows.last_result(windows.REFRESH))
        with mock.patch.object(windows, "run", return_value=(0, '"a","b","c"')):
            self.assertIsNone(windows.task_state(windows.REFRESH))

    def test_check_refresh_waits_for_last_run_time_to_change(self):
        old = self.csv_row("6/11/2026 9:00:03 AM", "0")
        running = self.csv_row("6/11/2026 9:01:07 AM", "267009", status="Running")
        done = self.csv_row("6/11/2026 9:01:07 AM", "0")
        calls, sleeps = [], []
        with mock.patch.object(windows, "run", side_effect=self.fake_run([old, old, old, running, done], calls)):
            ok, detail = windows.check_refresh(wait_s=20, sleep=sleeps.append)
        self.assertEqual((ok, detail), (True, "ok"))
        self.assertEqual(len(sleeps), 4)            # old result ignored until the run time moved and it finished
        self.assertTrue(any("/Run" in c for c in calls))

    def test_check_refresh_reports_error_from_the_new_run(self):
        old = self.csv_row("6/11/2026 9:00:03 AM", "0")
        failed = self.csv_row("6/11/2026 9:01:07 AM", "-2147024894")
        with mock.patch.object(windows, "run", side_effect=self.fake_run([old, failed])):
            ok, detail = windows.check_refresh(sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("0x80070002", detail)

    def test_check_refresh_times_out_when_the_task_never_starts(self):
        old = self.csv_row("6/11/2026 9:00:03 AM", "0")
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(windows, "run", side_effect=self.fake_run([old])):
            ok, detail = windows.check_refresh(wait_s=20, sleep=lambda s: None, clock=lambda: next(clock))
        self.assertFalse(ok)
        self.assertIn("did not start", detail)

    def test_check_refresh_run_refused(self):
        with mock.patch.object(windows, "run", return_value=(1, "ERROR: denied")):
            ok, detail = windows.check_refresh(sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("would not start", detail)


class TestCollectArgs(unittest.TestCase):
    def test_collect_passes_explicit_argv(self):
        from unittest import mock
        from token_burn import cli
        with mock.patch("collector.collect.main", return_value=0) as m:
            cli.main(["collect"])
            m.assert_called_with([])
            cli.main(["collect", "--loop"])
            m.assert_called_with(["--loop"])


if __name__ == "__main__":
    unittest.main()
