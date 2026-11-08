"""Session titles (store, Claude, opencode, Cursor), the sessions payload, and sessions through sync."""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from collector import util
from collector.adapters import claude, cursor, opencode
from collector.aggregate import build_platform_payload
from collector.schema import SubagentRun, UsageRecord
from collector.store import Store, clean_title
from collector.sync import export, merge, validate
from collector.util import MINUTES, iso

LOG = logging.getLogger("test-sessions")
LOG.addHandler(logging.NullHandler())
NOW = 1_800_000_000.0
NOW_MIN = int(NOW // 60)
START_MIN = NOW_MIN - MINUTES + 1
INFO = {"status": "ok", "meta": {}, "plan": {"status": "ok", "last_lookup": None, "next_lookup": None}}
ID_A = "a" * 32
ID_B = "b" * 32
FP_A = "aa" * 8
FP_B = "bb" * 8


def at(idx):
    return (START_MIN + idx) * 60.0 + 5


def rec(idx, platform="claude", sess="s1", model="m", inp=0, out=0, cr=0, cw=0, cost=0.0, **kw):
    d = dict(platform=platform, agent_kind="primary", agent_id="", session_id=sess, model=model,
             effort_level="", input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
             cache_write_tokens=cw, cost_usd=cost, timestamp=at(idx))
    d.update(kw)
    return UsageRecord(**d)


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = Store(self.tmp / "t.db")
        self.addCleanup(self.store.close)


# ---- store

class StoreTitles(StoreBase):
    def test_custom_beats_ai_and_equal_source_replaces(self):
        st = self.store
        self.assertTrue(st.upsert_session_title("claude", "s1", "First AI", "ai"))
        self.assertTrue(st.upsert_session_title("claude", "s1", "Newer AI", "ai"))
        self.assertEqual(st.session_titles("claude"), {"s1": "Newer AI"})
        self.assertTrue(st.upsert_session_title("claude", "s1", "My name", "custom"))
        self.assertFalse(st.upsert_session_title("claude", "s1", "Later AI", "ai"))
        self.assertEqual(st.session_titles("claude"), {"s1": "My name"})
        self.assertFalse(st.upsert_session_title("claude", "s1", "My name", "custom"))

    def test_clean_and_cap(self):
        self.assertEqual(clean_title("a\tb\n\x07c​  d"), "a b c d")
        self.assertEqual(len(clean_title("x" * 500)), 120)
        self.assertFalse(self.store.upsert_session_title("claude", "s1", " \x00 ", "ai"))
        self.assertFalse(self.store.upsert_session_title("claude", "", "Title", "ai"))
        self.assertEqual(self.store.session_titles("claude"), {})

    def test_platforms_are_separate_and_prune_drops_orphans(self):
        st = self.store
        st.add_usage(rec(10, sess="keep"))
        st.upsert_session_title("claude", "keep", "Kept", "ai")
        st.upsert_session_title("claude", "gone", "Gone", "ai")
        st.upsert_session_title("cursor", "keep", "Other platform", "chat")
        st.prune(START_MIN)
        self.assertEqual(st.session_titles("claude"), {"keep": "Kept"})
        self.assertEqual(st.session_titles("cursor"), {})


# ---- Claude

def asst(ts, mid, sess="s1", inp=10, out=20):
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "sessionId": sess, "requestId": mid,
                       "message": {"id": mid, "model": "claude-opus-5-5",
                                   "usage": {"input_tokens": inp, "output_tokens": out}}}) + "\n"


def title_line(kind, text, sess="s1"):
    key = "aiTitle" if kind == "ai-title" else "customTitle"
    return json.dumps({"type": kind, key: text, "sessionId": sess}) + "\n"


class ClaudeTitles(StoreBase):
    def setUp(self):
        super().setUp()
        self.cdir = self.tmp / "claude"
        self.proj = self.cdir / "projects" / "-proj"
        self.proj.mkdir(parents=True)
        for p in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cdir)}),
                  mock.patch.object(util, "CLAUDE_HOOK_LOG", self.tmp / "hook.jsonl")):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def collect(self):
        res = claude.collect(self.store, self.now, LOG)
        self.assertEqual(res["status"], "ok", res)

    def test_latest_ai_title_and_custom_wins(self):
        f = self.proj / "s1.jsonl"
        f.write_text(asst(self.now - 100, "m1") + title_line("ai-title", "Early")
                     + title_line("ai-title", "Fix the login page")
                     + json.dumps({"type": "last-prompt", "lastPrompt": "SECRET PROMPT", "sessionId": "s1"}) + "\n")
        self.collect()
        self.assertEqual(self.store.session_titles("claude"), {"s1": "Fix the login page"})
        with open(f, "a") as fh:
            fh.write(title_line("custom-title", "Login work") + title_line("ai-title", "Another"))
        self.collect()
        self.assertEqual(self.store.session_titles("claude"), {"s1": "Login work"})
        self.assertNotIn("SECRET", json.dumps(self.store.session_titles("claude")))

    def test_title_lines_do_not_change_usage(self):
        (self.proj / "s1.jsonl").write_text(title_line("ai-title", "T") + asst(self.now - 50, "m1", inp=7, out=3))
        self.collect()
        rows = self.store.usage_rows("claude", 0)
        self.assertEqual(sum(r["input"] for r in rows), 7)
        self.assertEqual(sum(r["requests"] for r in rows), 1)

    def test_one_off_backfill_reads_titles_behind_the_offset(self):
        f = self.proj / "s2.jsonl"
        f.write_text(title_line("ai-title", "Old title", sess="s2") + asst(self.now - 100, "m1", sess="s2"))
        # as left by a version without titles: the file is fully read and the titles flag is unset
        self.store.kv_set(claude.OFFSETS_KEY, {str(f): f.stat().st_size})
        self.collect()
        self.assertEqual(self.store.session_titles("claude"), {"s2": "Old title"})
        self.assertTrue(self.store.kv_get(claude.TITLES_KEY))
        with mock.patch.object(claude, "_scan_titles") as scan:
            self.collect()
        scan.assert_not_called()


# ---- opencode

OC_SCHEMA = """
CREATE TABLE session_v2(id TEXT PRIMARY KEY, parent_id TEXT, agent TEXT, model TEXT, title TEXT,
  time_updated INT DEFAULT 0);
CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
  seq INT NOT NULL, time_created INT NOT NULL, time_updated INT NOT NULL, data TEXT NOT NULL);
"""


class OpencodeTitles(StoreBase):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "opencode.db"
        p = mock.patch.dict(os.environ, {"TOKEN_BURN_OPENCODE_DB": str(self.db)})
        p.start()
        self.addCleanup(p.stop)

    def build(self):
        ms = int(NOW * 1000)
        con = sqlite3.connect(str(self.db))
        con.executescript(OC_SCHEMA)
        con.execute("INSERT INTO session_v2 VALUES('s1', NULL, 'build', '', 'Parent work', ?)", (ms,))
        con.execute("INSERT INTO session_v2 VALUES('c1', 's1', 'explore', '', 'Child title', ?)", (ms,))
        con.execute("INSERT INTO session_v2 VALUES('old', NULL, 'build', '', 'Too old', 0)")
        for i, sid in enumerate(("s1", "c1")):
            c = ms - (100 - i) * 1000
            data = {"time": {"created": c, "completed": c + 1000},
                    "model": {"id": "deep", "providerID": "acme", "variant": "default"}, "cost": 0.5,
                    "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
            con.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                        ("m%d" % i, sid, "assistant", i, c, c + 2000, json.dumps(data)))
        con.commit()
        con.close()

    def test_top_level_titles_and_child_folded_into_parent(self):
        self.build()
        res = opencode.collect(self.store, NOW, None)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(self.store.session_titles("opencode"), {"s1": "Parent work"})
        p = build_platform_payload(self.store, "opencode", NOW, INFO)
        self.assertEqual([s["id"] for s in p["sessions"]], ["s1"])
        ses = p["sessions"][0]
        self.assertEqual(ses["title"], "Parent work")
        self.assertEqual(sum(sum(s["input"]) for s in ses["series"]), 20)
        self.assertAlmostEqual(sum(sum(s["cost"]) for s in ses["series"]), 1.0)


# ---- Cursor

def fake_vscdb(path, entries):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value BLOB)")
    for k, v in entries.items():
        con.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:" + k, v))
    con.commit()
    con.close()


class CursorChatNames(StoreBase):
    def test_name_subtitle_missing_and_bad_rows(self):
        db = self.tmp / "state.vscdb"
        fake_vscdb(db, {"c1": json.dumps({"name": "Refactor parser", "subtitle": "x"}),
                        "c2": json.dumps({"name": "", "subtitle": "Edited app.js"}),
                        "c3": json.dumps({"name": "  "}), "c4": "not json",
                        "c5": json.dumps([1, 2]).encode("utf-8")})
        got = cursor.chat_names(["c1", "c2", "c3", "c4", "c5", "missing", "", None], db)
        self.assertEqual(got, {"c1": "Refactor parser", "c2": "Edited app.js"})

    def test_absent_or_unreadable_database(self):
        self.assertEqual(cursor.chat_names(["c1"], self.tmp / "nope.vscdb"), {})
        bad = self.tmp / "bad.vscdb"
        bad.write_bytes(b"not a database at all" * 100)
        self.assertEqual(cursor.chat_names(["c1"], bad), {})
        empty = self.tmp / "empty.vscdb"
        sqlite3.connect(str(empty)).close()
        self.assertEqual(cursor.chat_names(["c1"], empty), {})

    def test_refresh_once_a_day_per_unknown_id(self):
        db = self.tmp / "state.vscdb"
        fake_vscdb(db, {"c1": json.dumps({"name": "Chat one"})})
        self.store.add_usage(rec(10, platform="cursor", sess="c1"))
        self.store.add_usage(rec(11, platform="cursor", sess="c2"))
        self.assertEqual(cursor.refresh_titles(self.store, NOW, db), 2)
        self.assertEqual(self.store.session_titles("cursor"), {"c1": "Chat one"})
        self.assertEqual(cursor.refresh_titles(self.store, NOW + 60, db), 0)
        self.assertEqual(cursor.refresh_titles(self.store, NOW + 86400, db), 1)   # only c2 is retried


# ---- aggregate

class SessionsPayload(StoreBase):
    def test_totals_match_series_and_titles(self):
        st = self.store
        st.add_usage(rec(10, sess="s1", model="opus", inp=100, out=10, cr=5, cost=1.0))
        st.add_usage(rec(11, sess="s1", model="sonnet", inp=50, cost=0.5))
        st.add_usage(rec(12, sess="s2", model="opus", inp=500, out=1, cost=2.0))
        st.add_usage(rec(13, sess="s1", model="opus", inp=1, agent_kind="subagent", agent_id="ag1"))
        st.upsert_session_title("claude", "s1", "Session one", "ai")
        for name in ("claude", "cursor"):
            if name == "cursor":
                st.add_usage(rec(14, platform="cursor", sess="conv", model="gpt", inp=9, cost=0.1))
            p = build_platform_payload(st, name, NOW, INFO)
            series_tok = sum(sum(s["input"]) + sum(s["output"]) + sum(s["cache"]) for s in p["series"])
            ses_tok = sum(sum(x["input"]) + sum(x["output"]) + sum(x["cache"])
                          for ses in p["sessions"] for x in ses["series"])
            self.assertEqual(series_tok, ses_tok)
            series_cost = sum(sum(s["cost"]) for s in p["series"])
            ses_cost = sum(sum(x["cost"]) for ses in p["sessions"] for x in ses["series"])
            self.assertAlmostEqual(series_cost, ses_cost, places=5)
        p = build_platform_payload(st, "claude", NOW, INFO)
        self.assertEqual([s["id"] for s in p["sessions"]], ["s2", "s1"])   # most tokens first
        s1 = p["sessions"][1]
        self.assertEqual(s1["title"], "Session one")
        self.assertEqual(sorted(x["key"] for x in s1["series"]), ["opus", "sonnet"])
        self.assertIsNone(p["sessions"][0]["title"])
        self.assertEqual(set(s1["series"][0]), {"key", "model", "effort", "t", "input", "output", "cache", "cost"})


# ---- sync

class SessionsSync(StoreBase):
    def setUp(self):
        super().setUp()
        for name, val in (("hostname", "PC-A"), ("host_fp", FP_A)):
            p = mock.patch.object(export, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def cfg(self, mid):
        return {"version": 1, "sync": {"enabled": True, "folder": "unused", "machine_id": mid,
                                       "machine_name": "PC", "joined_at": "2026-11-05T03:00:00Z",
                                       "adopted": False}}

    def payloads(self):
        out = {n: build_platform_payload(self.store, n, NOW, INFO) for n in ("claude", "cursor", "opencode")}
        for p in out.values():
            p["installed"] = True
        return out

    def test_export_hashes_ids_and_caps_titles(self):
        self.store.add_usage(rec(10, sess="raw-session-id", inp=5))
        self.store.upsert_session_title("claude", "raw-session-id", "Title " + "x" * 200, "ai")
        doc = export.build_export(self.payloads(), {}, self.cfg(ID_A), NOW, app_version="t")
        text = json.dumps(doc)
        self.assertNotIn("raw-session-id", text)
        ses = doc["platforms"]["claude"]["sessions"]
        self.assertEqual(len(ses), 1)
        self.assertRegex(ses[0]["id"], r"^[0-9a-f]{16}$")
        self.assertEqual(ses[0]["id"], validate.session_hash(FP_A, "raw-session-id"))
        self.assertLessEqual(len(ses[0]["title"]), validate.TITLE_MAX)
        clean, status = validate.parse_machine(doc, ID_A, NOW)
        self.assertEqual(status, "ok")
        self.assertEqual(clean["platforms"]["claude"]["sessions"][0]["series"][0]["m"], [START_MIN + 10])

    def test_validate_drops_bad_sessions_and_caps(self):
        good = {"key": "m", "model": "m", "effort": "", "m": [START_MIN + 1], "input": [1], "output": [0],
                "cache": [0], "cost": [0.0]}
        raw = [{"id": "f" * 16, "title": "T\x07itle " + "y" * 300, "series": [good]},
               {"id": "f" * 16, "title": "dup", "series": [good]},
               {"id": "raw-id", "title": "bad id", "series": [good]},
               {"id": "e" * 16, "title": None, "series": [dict(good, input=["x"])]},
               {"id": "d" * 16, "title": 5, "series": [dict(good, m=[1])]},     # out of window
               "nonsense"]
        out = validate._clean_sessions(raw, START_MIN, NOW_MIN)
        self.assertEqual([s["id"] for s in out], ["f" * 16])
        self.assertTrue(out[0]["title"].startswith("Title "))
        self.assertEqual(len(out[0]["title"]), validate.TITLE_MAX)
        many = [{"id": "%016x" % i, "title": None, "series": [good]} for i in range(validate.MAX_SESSIONS + 5)]
        self.assertEqual(len(validate._clean_sessions(many, START_MIN, NOW_MIN)), validate.MAX_SESSIONS)

    def peer(self, mid, fp, store_fill, cursor_fill=None):
        st = Store(self.tmp / ("%s.db" % mid[:4]))
        self.addCleanup(st.close)
        store_fill(st)
        if cursor_fill:
            cursor_fill(st)
        pay = {n: build_platform_payload(st, n, NOW, INFO) for n in ("claude", "cursor", "opencode")}
        with mock.patch.object(export, "host_fp", return_value=fp):
            doc = export.build_export(pay, {}, self.cfg(mid), NOW, app_version="t")
        doc["host_fp"] = fp
        clean, _ = validate.parse_machine(doc, mid, NOW)
        return {"id": mid, "doc": clean, "status": "ok", "from_cache": False, "control": None,
                "mtime": NOW - 60}

    def test_merge_adds_computers_and_keeps_titles(self):
        self.store.add_usage(rec(10, sess="local", inp=100))
        self.store.upsert_session_title("claude", "local", "Local work", "ai")
        local = build_platform_payload(self.store, "claude", NOW, INFO)

        def fill(st):
            st.add_usage(rec(20, sess="remote", inp=40))
            st.upsert_session_title("claude", "remote", "Remote work", "ai")
        pb = self.peer(ID_B, FP_B, fill)
        out = merge.merge_platform("claude", local, [pb], NOW, ID_A, self_fp=FP_A)
        titles = {s["title"]: s for s in out["sessions"]}
        self.assertEqual(set(titles), {"Local work", "Remote work"})
        self.assertEqual(titles["Local work"]["id"], "local")   # this computer keeps its own ids
        tok = sum(sum(x["input"]) for s in out["sessions"] for x in s["series"])
        self.assertEqual(tok, sum(sum(s["input"]) for s in out["series"]))
        self.assertEqual(set(out["machines"]), {ID_A, ID_B})
        self.assertEqual([s["title"] for s in out["machines"][ID_B]["sessions"]], ["Remote work"])

    def test_same_computer_under_old_id_is_not_double_counted(self):
        self.store.add_usage(rec(10, sess="local", inp=100))
        local = build_platform_payload(self.store, "claude", NOW, INFO)
        old = self.peer(ID_B, FP_A, lambda st: st.add_usage(rec(10, sess="local", inp=100)))
        out = merge.merge_platform("claude", local, [old], NOW, ID_A, self_fp=FP_A)
        self.assertEqual(len(out["sessions"]), 1)
        self.assertEqual(sum(sum(x["input"]) for s in out["sessions"] for x in s["series"]), 100)

    def test_cursor_conversation_lines_up_across_computers(self):
        self.store.add_usage(rec(10, platform="cursor", sess="conv1", inp=70))
        local = build_platform_payload(self.store, "cursor", NOW, INFO)

        def fill(st):
            st.add_usage(rec(10, platform="cursor", sess="conv1", inp=70))
            st.upsert_session_title("cursor", "conv1", "Named elsewhere", "chat")
        pb = self.peer(ID_B, FP_B, lambda st: None, fill)
        out = merge.merge_platform("cursor", local, [pb], NOW, ID_A, self_fp=FP_A)
        self.assertEqual(len(out["sessions"]), 1)
        self.assertEqual(out["sessions"][0]["title"], "Named elsewhere")
        self.assertEqual(sum(out["sessions"][0]["series"][0]["input"]), 70)


# ---- dashboard maths (node)

STATS = Path(__file__).resolve().parent.parent / "web" / "js" / "stats.js"


def node(body):
    js = ("global.window={};eval(require('fs').readFileSync(process.argv[1],'utf8'));"
          "var S=window.TokenBurn.stats;var T0=Date.parse('2026-01-01T00:00:00Z');%s" % body)
    out = subprocess.run(["node", "-e", js, str(STATS)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


PLAT = ("{start:'2026-01-01T00:00:00Z',sessions:["
        "{id:'aaaaaaaa-1111',title:'Cheap but long',series:["
        "{key:'opus',model:'opus',t:[1,8],input:[100,900],output:[0,0],cache:[0,0],cost:[0.1,0.1]},"
        "{key:'haiku',model:'haiku',t:[9],input:[50],output:[0],cache:[0],cost:[0.01]}]},"
        "{id:'bbbbbbbbbbbb',title:null,series:[{key:'opus',model:'opus',t:[9],input:[10],output:[0],cache:[0],cost:[5]}]},"
        "{id:'agent-123456789',title:'  ',series:[{key:'gpt',model:'gpt',t:[2],input:[1],output:[0],cache:[0],cost:[0]}]}"
        "]}")


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class SessionStatsJs(unittest.TestCase):
    def test_window_sort_and_labels(self):
        r = node("var P=%s;var fr=S.frame(P,'30m',T0+10*60000);fr.loIdx=5;"
                 "console.log(JSON.stringify({t:S.sessionStats(P,fr,'claude','tokens'),"
                 "c:S.sessionStats(P,fr,'claude','cost'),cur:S.sessionStats(P,S.frame(P,'30m',T0+10*60000),'cursor','tokens')}))" % PLAT)
        t = r["t"]
        self.assertEqual([x["label"] for x in t], ["Cheap but long", "Session bbbbbbbb"])   # agent- session is outside
        self.assertEqual(t[0]["tokens"], 950)
        self.assertEqual(t[0]["keys"], ["opus", "haiku"])
        self.assertEqual(t[0]["lastMs"], r and t[0]["lastMs"])
        self.assertEqual(t[0]["lastMs"], 1767225600000 + 9 * 60000)
        self.assertEqual([x["id"] for x in r["c"]], ["bbbbbbbbbbbb", "aaaaaaaa-1111"])
        labels = {x["id"]: x["label"] for x in r["cur"]}
        self.assertEqual(labels["agent-123456789"], "Cursor agent 123456")
        self.assertEqual(labels["bbbbbbbbbbbb"], "Conversation bbbbbb")

    def test_missing_sessions_key_is_empty(self):
        r = node("var P={start:'2026-01-01T00:00:00Z',series:[]};"
                 "console.log(JSON.stringify(S.sessionStats(P,S.frame(P,'12h',T0),'claude','tokens')))")
        self.assertEqual(r, [])


if __name__ == "__main__":
    unittest.main()


# ---- Cursor: conversation search, placeholders and project labels (0.4.1)

class CursorMoreNames(StoreBase):
    def search_db(self, rows):
        path = self.tmp / "conversation-search.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE conversations(fts_rowid INTEGER PRIMARY KEY, source TEXT, scope TEXT, id TEXT,"
                    " title TEXT, branches TEXT)")
        for cid, title in rows:
            con.execute("INSERT INTO conversations(source, scope, id, title, branches) VALUES('local','x',?,?,'')",
                        (cid, title))
        con.commit()
        con.close()
        return path

    def projects(self):
        root = self.tmp / "projects"
        work = self.tmp / "disk" / "My Work" / "RAG installer"
        work.mkdir(parents=True)
        slug = "-".join(cursor._slug(x) for x in work.parts[1:] if x)
        drive = work.parts[0]
        slug = (drive[0] + "-" + slug) if drive[1:2] == ":" else slug
        t = root / slug / "agent-transcripts"
        (t / "agent-11111111-2222-3333-4444-555555555555").mkdir(parents=True)
        (t / "66666666-7777-8888-9999-000000000000.jsonl").write_text("{}\n")
        (root / "gone-folder-Temp-11112222-3333-4444-5555-666677778888" / "agent-transcripts"
         / "agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").mkdir(parents=True)
        return root

    def test_search_titles_and_placeholders(self):
        path = self.search_db([("bc-1", "Cloud task"), ("c2", "New Agent"), ("c3", "")])
        self.assertEqual(cursor.search_titles(["bc-1", "c2", "c3", "nope"], path), {"bc-1": "Cloud task"})
        self.assertEqual(cursor.search_titles(["bc-1"], self.tmp / "absent.db"), {})
        db = self.tmp / "state.vscdb"
        fake_vscdb(db, {"c1": json.dumps({"name": "New Agent", "subtitle": "Edited x"})})
        self.assertEqual(cursor.chat_names(["c1"], db), {"c1": "Edited x"})

    def test_project_labels_resolve_real_folder_names(self):
        root = self.projects()
        got = cursor.project_labels(["agent-11111111-2222-3333-4444-555555555555",
                                     "66666666-7777-8888-9999-000000000000",
                                     "agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "agent-unknown"], root)
        self.assertEqual(got["agent-11111111-2222-3333-4444-555555555555"], "Agent 111111 · RAG installer")
        self.assertEqual(got["66666666-7777-8888-9999-000000000000"], "Agent 666666 · RAG installer")
        self.assertEqual(got["agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"], "Agent aaaaaa · temp folder")
        self.assertNotIn("agent-unknown", got)
        self.assertEqual(cursor.project_labels(["x"], self.tmp / "missing"), {})

    def test_label_rules(self):
        self.assertEqual(cursor.project_label("zz-nowhere-config-openchamber-chats-2026-11-02-session-"
                                              "31b9daf5-2909-4516-bc62-a6659f87230a"),
                         "openchamber")
        self.assertEqual(cursor.project_label("zz-nowhere-Some-Project"), "zz-nowhere-Some-Project")
        with mock.patch.object(cursor, "_resolve_slug",
                               return_value=[".config", "openchamber", "chats", "2026-11-02",
                                             "session-31b9daf5-2909-4516-bc62-a6659f87230a"]):
            self.assertEqual(cursor.project_label("x"), "openchamber")
        with mock.patch.object(cursor, "_resolve_slug", return_value=["Users", Path.home().name]):
            self.assertEqual(cursor.project_label("x"), "home folder")

    def test_refresh_prefers_real_names_and_rechecks_labels(self):
        root = self.projects()
        sid = "agent-11111111-2222-3333-4444-555555555555"
        self.store.add_usage(rec(10, platform="cursor", sess=sid))
        self.store.add_usage(rec(11, platform="cursor", sess="bc-1"))
        db, search = self.tmp / "state.vscdb", self.search_db([("bc-1", "Cloud task")])
        fake_vscdb(db, {})
        cursor.refresh_titles(self.store, NOW, db, search, root)
        self.assertEqual(self.store.session_title_sources("cursor"),
                         {sid: ("Agent 111111 · RAG installer", "project"), "bc-1": ("Cloud task", "chat")})
        p = build_platform_payload(self.store, "cursor", NOW, INFO)
        flags = {s["id"]: s.get("title_local", False) for s in p["sessions"]}
        self.assertEqual(flags, {sid: True, "bc-1": False})
        # a project label is retried daily, and a real name replaces it
        db.unlink()
        fake_vscdb(db, {sid: json.dumps({"name": "Real chat"})})
        self.assertEqual(cursor.refresh_titles(self.store, NOW + 60, db, search, root), 0)
        self.assertEqual(cursor.refresh_titles(self.store, NOW + 86400, db, search, root), 1)
        self.assertEqual(self.store.session_titles("cursor")[sid], "Real chat")


class LocalLabelsStayLocal(SessionsSync):
    def test_project_label_not_exported_and_peer_title_wins(self):
        sid = "conv1"
        self.store.add_usage(rec(10, platform="cursor", sess=sid, inp=70))
        self.store.upsert_session_title("cursor", sid, "Agent conv1 · secret-folder", "project")
        doc = export.build_export(self.payloads(), {}, self.cfg(ID_A), NOW, app_version="t")
        self.assertNotIn("secret-folder", json.dumps(doc))
        self.assertIsNone(doc["platforms"]["cursor"]["sessions"][0]["title"])
        local = build_platform_payload(self.store, "cursor", NOW, INFO)

        def fill(st):
            st.add_usage(rec(10, platform="cursor", sess=sid, inp=70))
            st.upsert_session_title("cursor", sid, "Named elsewhere", "chat")
        pb = self.peer(ID_B, FP_B, lambda st: None, fill)
        out = merge.merge_platform("cursor", local, [pb], NOW, ID_A, self_fp=FP_A)
        self.assertEqual(out["sessions"][0]["title"], "Named elsewhere")
        solo = merge.merge_platform("cursor", local, [], NOW, ID_A, self_fp=FP_A)
        self.assertEqual(solo["sessions"][0]["title"], "Agent conv1 · secret-folder")
