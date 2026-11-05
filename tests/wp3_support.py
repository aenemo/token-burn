"""Shared test scaffolding for the installer, sync CLI and data CLI tests.

Everything is isolated: a temp HOME, patched environment, a mocked scheduler, mocked process control,
mocked browser, folder picker and registry. Nothing here touches the real machine.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_burn import cli, folder_picker, installer
from token_burn import install_location as il

ENV_KEYS = ("TOKEN_BURN_DATA_DIR", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME",
            "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "OneDrive", "OneDriveCommercial",
            "OneDriveConsumer", "TEMP", "TMP", "TMPDIR")


class FakeConsole(installer.Console):
    """Answers prompts from a list; records everything said."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.lines = []
        self.errors = []
        self.prompts = []
        installer.Console.__init__(self, self._in, self.lines.append, self.errors.append)

    def _in(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError()
        return self.answers.pop(0)

    @property
    def text(self):
        return "\n".join(self.lines + self.errors)


def fake_backend():
    be = mock.MagicMock()
    be.install.return_value = ["task: registered"]
    be.uninstall.return_value = ["task: removed"]
    be.start_server.return_value = True
    be.check_refresh.return_value = (True, "ok")
    return be


class Isolated(unittest.TestCase):
    """Temp HOME and data folder; scheduler, process control, browser, registry and pickers mocked."""

    data_env = True      # set TOKEN_BURN_DATA_DIR to tmp/data (False: leave it unset, the pointer decides)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tbtest-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        env = {"CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
               "APPDATA": str(self.home / "AppData" / "Roaming"), "USERPROFILE": str(self.home),
               "HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local" / "share"),
               "XDG_STATE_HOME": str(self.home / ".local" / "state"),
               "XDG_CONFIG_HOME": str(self.home / ".config")}
        if self.data_env:
            env["TOKEN_BURN_DATA_DIR"] = str(self.data)
        self.onedrive = self.home / "OneDrive"
        self.onedrive.mkdir()
        env["OneDrive"] = str(self.onedrive)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("OneDriveCommercial", "OneDriveConsumer") + (() if self.data_env else ("TOKEN_BURN_DATA_DIR",)):
            os.environ.pop(k, None)
        self.patch(Path, "home", return_value=self.home)
        self.backend = fake_backend()
        self.patch(installer, "get_backend", return_value=self.backend)
        self.patch(installer, "proc")
        self.patch(il, "proc")
        il.proc.processes_using.return_value = []
        installer.proc.wait_health.return_value = True
        installer.proc.stop_server.return_value = False
        self.browser = self.patch(installer.webbrowser, "open")
        self.interactive = self.patch(installer, "is_interactive", return_value=False)
        self.pin = self.patch(folder_picker, "pin_onedrive", return_value=True)
        self.picker = self.patch(folder_picker, "pick_folder", return_value=None)
        self.collect_now = self.patch(installer, "collect_now", return_value=True)
        self.winreg = mock.MagicMock()
        self.patch(il, "_winreg", return_value=self.winreg)
        out = mock.patch.object(sys, "stdout", io.StringIO())
        out.start()
        self.addCleanup(out.stop)
        self.faketemp = self.tmp / "faketemp"
        self.patch(folder_picker, "is_temp_location", side_effect=lambda p: "faketemp" in Path(p).parts)

    def patch(self, target, name, **kw):
        m = mock.patch.object(target, name, **kw)
        started = m.start()
        self.addCleanup(m.stop)
        return started

    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def make_shared(self, name="Shared"):
        d = self.onedrive / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_peer(self, folder, machine_id, name, hostname="OTHER-PC", os_name="Windows 11", age_s=60,
                   control=None):
        """A valid machines/<id>.json.gz written by 'another computer'."""
        import gzip
        import json
        import time
        from collector import util
        from collector.sync import config, peers
        peers.ensure_layout(folder)
        doc = {"format": "token-burn-machine", "schema": 1, "machine_id": machine_id, "display_name": name,
               "hostname": hostname, "os": os_name, "app_version": "0.2.0",
               "written_at": util.iso(time.time() - age_s), "write_token": "t", "minutes": 43200,
               "platforms": {}}
        raw = gzip.compress(json.dumps(doc).encode("utf-8"))
        path = config.shared_root(folder) / "machines" / ("%s.json.gz" % machine_id)
        path.write_bytes(raw)
        os.utime(str(path), (time.time() - age_s, time.time() - age_s))
        if control:
            peers.write_control(folder, machine_id, control, None, "a" * 32, time.time())
        return path
