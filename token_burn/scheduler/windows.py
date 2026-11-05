"""Windows: Task Scheduler through schtasks.exe with task XML (no admin rights needed)."""
import csv
import datetime as dt
import os
import subprocess
import tempfile
import time
from xml.sax.saxutils import escape

from token_burn.scheduler import run

FOLDER = "\\TokenBurn"
REFRESH = FOLDER + "\\Refresh"
SERVER = FOLDER + "\\WebServer"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _split(argv):
    return argv[0], subprocess.list2cmdline([str(a) for a in argv[1:]])


def render_task_xml(argv, kind, user=None, start=None):
    """kind 'refresh' (logon + every minute) or 'server' (at logon). Returns XML text."""
    exe, args = _split(argv)
    start = start or dt.datetime.now().replace(microsecond=0).isoformat()
    uid = ("<UserId>%s</UserId>" % escape(user)) if user else ""
    logon = "<LogonTrigger><Enabled>true</Enabled>%s</LogonTrigger>" % uid
    if kind == "refresh":
        triggers = (
            "<TimeTrigger><Repetition><Interval>PT1M</Interval></Repetition>"
            "<StartBoundary>%s</StartBoundary><Enabled>true</Enabled></TimeTrigger>" % start + logon)
        limit = "PT10M"
        desc = "Claude, Cursor and opencode token usage: collects usage every minute (no model tokens used)."
    else:
        triggers = logon
        limit = "PT0S"
        desc = "Claude, Cursor and opencode token usage: local dashboard server on 127.0.0.1 only."
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="%s">\n'
        "  <RegistrationInfo><Description>%s</Description></RegistrationInfo>\n"
        "  <Triggers>%s</Triggers>\n"
        '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <ExecutionTimeLimit>%s</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>\n'
        "</Task>\n" % (NS, escape(desc), triggers, limit, escape(exe), escape(args)))


def _create(name, xml):
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-16") as f:
            f.write(xml)
        return run(["schtasks", "/Create", "/TN", name, "/XML", tmp, "/F"])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def exists(name):
    return run(["schtasks", "/Query", "/TN", name])[0] == 0


def install(refresh_argv, serve_argv):
    msgs = []
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    user = user if os.environ.get("USERNAME") else None
    for name, argv, kind in ((REFRESH, refresh_argv, "refresh"), (SERVER, serve_argv, "server")):
        rc, out = _create(name, render_task_xml(argv, kind, user=user))
        msgs.append("%s: %s" % (name, "registered" if rc == 0 else "FAILED (%s)" % out.strip()))
    return msgs


def uninstall():
    msgs = []
    for name in (REFRESH, SERVER):
        if exists(name):
            run(["schtasks", "/End", "/TN", name])
            rc, out = run(["schtasks", "/Delete", "/TN", name, "/F"])
            msgs.append("%s: %s" % (name, "removed" if rc == 0 else "FAILED (%s)" % out.strip()))
    return msgs


def start_server():
    rc, _ = run(["schtasks", "/Run", "/TN", SERVER])
    run(["schtasks", "/Run", "/TN", REFRESH])
    return rc == 0


def status():
    parts = []
    for name in (REFRESH, SERVER):
        parts.append("%s: %s" % (name, "registered" if exists(name) else "missing"))
    return "; ".join(parts)


RESULT_RUNNING = 267009      # 0x41301, SCHED_S_TASK_RUNNING
RESULT_NEVER_RUN = 267011    # 0x41303, SCHED_S_TASK_HAS_NOT_RUN


def parse_result(text):
    """A Last Result cell as an int: decimal (also negative) or 0x hex. None for anything else (for example
    the 'N/A' or text a localised Windows prints)."""
    t = (text or "").strip()
    try:
        if t.lower().startswith(("0x", "-0x")):
            return int(t, 16)
        return int(t, 10)
    except ValueError:
        return None


def task_state(name):
    """(Last Run Time text, Last Result int or None) from `schtasks /Query /FO CSV /V /NH`, or None when the
    query failed. The columns are HostName, TaskName, Next Run Time, Status, Logon Mode, Last Run Time, Last
    Result, ...: the order does not change with the display language, so positions 5 and 6 are used, and a
    value that does not parse as a number is treated as unknown rather than guessed."""
    rc, out = run(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/NH", "/V"])
    if rc != 0:
        return None
    for row in csv.reader(out.splitlines()):
        if len(row) >= 7:
            return row[5].strip(), parse_result(row[6])
    return None


def last_result(name):
    """The task's Last Result as an int, or None when it cannot be read."""
    state = task_state(name)
    return state[1] if state else None


def check_refresh(wait_s=20, sleep=time.sleep, clock=time.time):
    """Run the Refresh task once and check that it ran. Polls until the task's Last Run Time changes from what it
    was before the /Run and it is no longer running, or wait_s seconds pass. Returns (ok, detail)."""
    before = task_state(REFRESH)
    rc, out = run(["schtasks", "/Run", "/TN", REFRESH])
    if rc != 0:
        return False, "Task Scheduler would not start the task (%s)" % out.strip()
    end = clock() + wait_s
    state = None
    while True:
        sleep(2)
        state = task_state(REFRESH)
        if state is None:
            break                                    # cannot read the task: not verified
        started = before is None or state[0] != before[0]
        if started and state[1] != RESULT_RUNNING:
            break
        if clock() >= end:
            break
    if state is None or state[1] is None:
        return True, "not verified"
    started = before is None or state[0] != before[0]
    result = state[1]
    if result == RESULT_RUNNING:
        return True, "ok"
    if not started:
        return False, "the task did not start within %d seconds" % wait_s
    if result == 0:
        return True, "ok"
    if result == RESULT_NEVER_RUN:
        return False, "the task did not run"
    return False, "the task failed with error 0x%08X" % (result & 0xFFFFFFFF)
