"""Windows: Task Scheduler through schtasks.exe with task XML (no admin rights needed)."""
import datetime as dt
import os
import subprocess
import tempfile
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
