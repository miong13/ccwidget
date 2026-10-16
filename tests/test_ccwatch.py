"""Unit tests for ccwatch.py (stdlib only): python3 -m unittest discover -s tests"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ccwatch  # noqa: E402


def compact(obj):
    """One transcript line, written the way Claude Code writes them (no spaces)."""
    return json.dumps(obj, separators=(",", ":"))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class LimitWindowTest(unittest.TestCase):
    def test_known_shapes(self):
        self.assertEqual(ccwatch.limit_window("five_hour"), ("5-hour", 5 * 3600))
        self.assertEqual(ccwatch.limit_window("four_hour"), ("4-hour", 4 * 3600))
        self.assertEqual(ccwatch.limit_window("seven_day"), ("weekly", 7 * 86400))
        self.assertEqual(ccwatch.limit_window("seven_day_opus"), ("weekly opus", 7 * 86400))
        self.assertEqual(ccwatch.limit_window("one_day"), ("daily", 86400))

    def test_numeric_key(self):
        self.assertEqual(ccwatch.limit_window("6_hours"), ("6-hour", 6 * 3600))

    def test_unknown_key(self):
        self.assertEqual(ccwatch.limit_window("monthly_spend"), ("monthly spend", None))

    def test_windows_sorted_shortest_first(self):
        keys = [k for k, _, _ in ccwatch.limit_windows(
            {"seven_day": {}, "five_hour": {}, "odd": {}, "seven_day_opus": {}, "junk": 3})]
        self.assertEqual(keys, ["five_hour", "seven_day", "seven_day_opus", "odd"])


class PaceTest(unittest.TestCase):
    W = 5 * 3600

    def lim(self, used, elapsed):
        return {"used_percentage": used, "resets_at": time.time() + self.W - elapsed}

    def test_unknown_window(self):
        self.assertIsNone(ccwatch.pace(self.lim(10, 100), None))

    def test_no_usage(self):
        self.assertIn("no usage", ccwatch.pace(self.lim(0, 3600), self.W)[1])

    def test_limit_reached(self):
        self.assertEqual(ccwatch.pace(self.lim(100, 3600), self.W)[1:], ("▲ limit reached", ccwatch.C_DANGER))

    def test_just_started(self):
        self.assertEqual(ccwatch.pace(self.lim(5, 60), self.W)[1], "window just started")

    def test_on_pace(self):
        frac, text, color = ccwatch.pace(self.lim(10, self.W / 2), self.W)
        self.assertAlmostEqual(frac, 0.5, places=2)
        self.assertEqual(text, "on pace for ~20% by reset")
        self.assertEqual(color, ccwatch.C_BUSY)

    def test_over_pace(self):
        _, text, color = ccwatch.pace(self.lim(80, self.W / 4), self.W)
        self.assertTrue(text.startswith("▲ limit in ~"), text)
        self.assertEqual(color, ccwatch.C_DANGER)


class ActiveTimeTest(unittest.TestCase):
    def test_gaps(self):
        midnight = 1_000_000.0
        stamps = [midnight + 3600, midnight + 3660, midnight + 3660 + ccwatch.IDLE_GAP + 1, midnight + 7300]
        total, hourly = ccwatch.active_time(stamps, midnight)
        # 60 s counts, the long gap doesn't, the last gap (7300 - 3961 = 3339 s) is a break too
        self.assertEqual(total, 60)
        self.assertEqual(hourly[1], 60)
        self.assertEqual(sum(hourly), 60)

    def test_bucket_by_later_stamp(self):
        midnight = 0.0
        total, hourly = ccwatch.active_time([3590, 3610], midnight)
        self.assertEqual((total, hourly[0], hourly[1]), (20, 0, 20))


class GitCommitTest(unittest.TestCase):
    def test_matches(self):
        for cmd in ("git commit -m x", "cd repo && git commit -am x", "git -C dir commit",
                    "make; git commit", "if true; then git commit; fi", "(git commit)"):
            self.assertTrue(ccwatch.GIT_COMMIT.search(cmd), cmd)

    def test_non_matches(self):
        for cmd in ('echo "git commit"', "git status", "git commit-tree abc", "grep 'git commit' log"):
            self.assertFalse(ccwatch.GIT_COMMIT.search(cmd), cmd)


class ScanTranscriptTest(unittest.TestCase):
    def test_title_model_activity(self):
        entries = [
            {"type": "ai-title", "aiTitle": "Fix the build"},
            {"type": "user", "message": {"content": "do it"}},
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "make\nmake test"}}]}},
        ]
        title, model, activity = ccwatch.scan_transcript(entries)
        self.assertEqual(title, "Fix the build")
        self.assertEqual(model, "claude-opus-5-5")
        self.assertEqual(activity, ("tool", "Bash", "make"))

    def test_falls_back_to_last_prompt(self):
        entries = [{"type": "last-prompt", "lastPrompt": "hello"},
                   {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}]
        title, model, activity = ccwatch.scan_transcript(entries)
        self.assertEqual(title, "hello")
        self.assertIsNone(model)
        self.assertEqual(activity, ("think", "Reading results", ""))

    def test_pretty_model(self):
        self.assertEqual(ccwatch.pretty_model("claude-opus-5-5"), "opus 5.5")
        self.assertEqual(ccwatch.pretty_model("claude-haiku-4-5-20251001"), "haiku 4.5")


class TokenLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        patcher = mock.patch.object(ccwatch, "PROJ_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(os.path.join(self.tmp, "-proj"))
        self.path = os.path.join(self.tmp, "-proj", "s1.jsonl")
        self.t0 = time.time() - 60

    def write(self, entries):
        with open(self.path, "a") as f:
            for i, e in enumerate(entries):
                e.setdefault("timestamp", iso(self.t0 + i))
                e.setdefault("sessionId", "s1")
                e.setdefault("cwd", "/work/proj")
                f.write(compact(e) + "\n")

    def test_ledger(self):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                 "output_tokens": 7}
        self.write([
            {"type": "user", "uuid": "u1", "origin": {"kind": "human"}, "message": {"content": "go"}},
            # one message split over two lines: usage counts once, the later line wins
            {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "model": "claude-opus-5-5",
                                                            "usage": dict(usage, output_tokens=1), "content": []}},
            {"type": "assistant", "uuid": "a2", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage,
                                                            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git commit -m ok"}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "git commit -m bad"}}]}},
            {"type": "user", "uuid": "u2", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1"},
                {"type": "tool_result", "tool_use_id": "t2", "is_error": True}]}},
            {"type": "user", "uuid": "u3", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3"}]},
             "toolUseResult": {"filePath": "/work/proj/a.py", "structuredPatch": [{"lines": [" x", "-y", "+z", "+w"]}]}},
            {"type": "user", "uuid": "u4", "message": {"content": [{"type": "tool_result", "tool_use_id": "t4"}]},
             "toolUseResult": {"filePath": "/work/proj/b.py", "type": "create", "content": "1\n2\n3\n"}},
            {"type": "user", "uuid": "u1", "origin": {"kind": "human"}, "message": {"content": "go"}},  # resumed repeat
            {"type": "user", "uuid": "u5", "message": {"content": "<command-name>/clear</command-name>"}},
        ])
        ledger = ccwatch.TokenLedger()
        ledger.refresh()
        p = ledger.projects["/work/proj"]
        self.assertEqual(p.prompts, 1)
        self.assertEqual(p.commits, 1)
        self.assertEqual((p.added, p.removed, len(p.files)), (5, 1, 2))
        t = ledger.totals()
        self.assertEqual((t["input"], t["cache_write"], t["cache_read"], t["output"], t["messages"]), (10, 5, 100, 7, 1))
        self.assertEqual(p.output, 7)

        # incremental: only the new line is read
        self.write([{"type": "user", "uuid": "u6", "origin": {"kind": "human"}, "message": {"content": "more"}}])
        ledger.refresh()
        self.assertEqual(ledger.projects["/work/proj"].prompts, 2)


class PruneTest(unittest.TestCase):
    def test_prune(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        sl, st = os.path.join(tmp, "statusline"), os.path.join(tmp, "state")
        os.makedirs(sl)
        os.makedirs(st)
        old = time.time() - 30 * 86400
        files = {}
        for d, name, age in ((sl, "old", old), (sl, "new", None), (sl, "live", old), (st, "gone", old), (st, "live", old)):
            path = files[(d, name)] = os.path.join(d, name + ".json")
            open(path, "w").close()
            if age:
                os.utime(path, (age, age))
        with mock.patch.object(ccwatch, "STATUSLINE_DIR", sl), mock.patch.object(ccwatch, "STATE_DIR", st):
            store = ccwatch.Store()
            live = ccwatch.Session(1)
            live.sid = "live"
            store.sessions[1] = live
            store.prune()
        self.assertEqual({k for k, p in files.items() if os.path.exists(p)},
                         {(sl, "new"), (sl, "live"), (st, "live")})


class SetupTest(unittest.TestCase):
    OTHER = {"type": "command", "command": "/usr/local/bin/cc-status"}

    def settings(self):
        return {"model": "opus", "hooks": {
            "Stop": [{"hooks": [self.OTHER]}],
            # an old ccwatch entry at a stale path, sharing its entry with another hook
            "PostToolUse": [{"hooks": [self.OTHER, {"type": "command", "command": '"/old/ccwatch-hook.sh"'}]}],
        }}

    def test_merge_is_idempotent_and_keeps_other_hooks(self):
        once = ccwatch.merge_hooks(self.settings(), '"/new/ccwatch-hook.sh"')
        self.assertEqual(ccwatch.merge_hooks(once, '"/new/ccwatch-hook.sh"'), once)
        self.assertEqual(once["model"], "opus")
        self.assertEqual(once["hooks"]["Stop"][0], {"hooks": [self.OTHER]})
        self.assertEqual(once["hooks"]["PostToolUse"][0], {"hooks": [self.OTHER]})  # stale path removed
        missing, paths = ccwatch.hook_status(once)
        self.assertEqual((missing, paths), ([], ["/new/ccwatch-hook.sh"]))
        self.assertEqual(once["hooks"]["PreToolUse"][-1]["matcher"], "AskUserQuestion|ExitPlanMode")

    def test_strip_restores(self):
        merged = ccwatch.merge_hooks({"hooks": {"Stop": [{"hooks": [self.OTHER]}]}}, "/x/ccwatch-hook.sh")
        self.assertEqual(ccwatch.strip_hooks(merged), {"hooks": {"Stop": [{"hooks": [self.OTHER]}]}})
        self.assertEqual(ccwatch.strip_hooks(ccwatch.merge_hooks({}, "/x/ccwatch-hook.sh")), {})

    def test_hook_status_wants_matchers(self):
        s = ccwatch.merge_hooks({}, "/x/ccwatch-hook.sh")
        del s["hooks"]["PreToolUse"][0]["matcher"]  # would mark every tool call as "needs you"
        self.assertEqual(ccwatch.hook_status(s)[0], ["PreToolUse"])

    def test_statusline_script(self):
        with tempfile.NamedTemporaryFile(suffix=".sh") as f:
            self.assertEqual(ccwatch.statusline_script({"statusLine": {"command": f"sh {f.name}"}}), f.name)
            self.assertEqual(ccwatch.statusline_script({"statusLine": {"command": f.name}}), f.name)
            self.assertIsNone(ccwatch.statusline_script({"statusLine": {"command": f"echo hi | {f.name}"}}))
        self.assertIsNone(ccwatch.statusline_script({}))


class WaitTest(unittest.TestCase):
    """Session._load_wait: the registry's "waiting" status decides; the hook adds details."""

    def wait(self, info, record):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        if record:
            with open(os.path.join(d, "abc.json"), "w") as f:
                json.dump(record, f)
        s = ccwatch.Session.__new__(ccwatch.Session)
        s.sid, s.info = "abc", info
        with mock.patch.object(ccwatch, "STATE_DIR", d):
            return s._load_wait()

    REC = {"state": "waiting", "reason": "permission", "tool": "Bash", "detail": "rm -rf build", "since": 1000}

    def test_registry_waiting_keeps_hook_details(self):
        self.assertEqual(self.wait({"status": "waiting", "statusUpdatedAt": 1000_500}, self.REC)["detail"], "rm -rf build")

    def test_registry_waiting_without_record(self):
        w = self.wait({"status": "waiting", "statusUpdatedAt": 2000_000, "waitingFor": "dialog open"}, None)
        self.assertEqual((w["reason"], w["detail"], w["since"]), ("dialog", "dialog open", 2000))
        # a record left from an earlier prompt doesn't describe this one
        self.assertEqual(self.wait({"status": "waiting", "statusUpdatedAt": 2000_000, "waitingFor": "input needed"},
                                   self.REC)["reason"], "input")

    def test_answered(self):
        self.assertIsNone(self.wait({"status": "busy", "statusUpdatedAt": 1010_000}, self.REC))
        self.assertIsNone(self.wait({"status": "idle", "statusUpdatedAt": 1010_000}, self.REC))
        self.assertIsNone(self.wait({"status": "busy", "statusUpdatedAt": 1010_000}, None))

    def test_registry_without_waiting_status(self):
        # older Claude Code: still "busy" from before the prompt, so the hook's flag stands
        self.assertEqual(self.wait({"status": "busy", "statusUpdatedAt": 900_000}, self.REC), self.REC)


class OpenCodeFixture(unittest.TestCase):
    """A small OpenCode database, in the 2.x layout (or 1.x with V1 = True)."""
    V1 = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db_path = os.path.join(self.tmp, "opencode.db")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self.now = int(time.time() * 1000)
        self.db = ccwatch.sqlite3.connect(self.db_path)
        self.addCleanup(self.db.close)
        if self.V1:
            self.db.executescript("""
                create table session (id text primary key, project_id text, parent_id text, directory text,
                  title text, time_created integer, time_updated integer, cost real default 0);
                create table message (id text primary key, session_id text, time_created integer,
                  time_updated integer, data text);
                create table part (id text primary key, message_id text, session_id text, time_created integer,
                  time_updated integer, data text);""")
        else:
            self.db.executescript("""
                create table session_v2 (id text primary key, project_id text, parent_id text, directory text,
                  title text, time_created integer, time_updated integer, time_archived integer, cost real default 0,
                  summary_additions integer, summary_deletions integer, time_idle integer, time_viewed integer,
                  idle_outcome text);
                create table session_message (id text primary key, session_id text, type text, seq integer,
                  time_created integer, time_updated integer, data text);
                create table kv (key text primary key, value text, time_created integer, time_updated integer);""")
            catalog = {"prov": {"models": {"big": {"name": "Big Model", "limit": {"context": 1000}}}}}
            self.db.execute("insert into kv values ('models-dev:catalog', ?, 0, 1)",
                            (json.dumps({"body": json.dumps(catalog)}),))
        self.seq = 0
        self.patch = mock.patch.object(ccwatch, "opencode_clients", lambda cwds: {})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def session(self, sid, directory="/work/oc", age=60, parent=None, title="Fix it", cost=0.5,
                idle=None, viewed=None, outcome=None):
        """idle / viewed: seconds ago that its last turn ended / was seen (2.x only)."""
        t = self.now - age * 1000
        ms = lambda secs: None if secs is None else self.now - secs * 1000
        if self.V1:
            self.db.execute("insert into session values (?, 'p', ?, ?, ?, ?, ?, ?)",
                            (sid, parent, directory, title, t - 3600_000, t, cost))
        else:
            self.db.execute("insert into session_v2 values (?, 'p', ?, ?, ?, ?, ?, null, ?, 3, 1, ?, ?, ?)",
                            (sid, parent, directory, title, t - 3600_000, t, cost, ms(idle), ms(viewed), outcome))
        self.db.commit()

    def message(self, sid, role, age=30, done=True, content=(), text="", tokens=None, cost=0.0):
        self.seq += 1
        mid = f"m{self.seq}"
        t = self.now - age * 1000
        tokens = tokens or {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 100, "write": 3}}
        model = {"providerID": "prov", "id": "big"}
        if self.V1:
            data = {"role": role, "time": {"created": t, **({"completed": t + 500} if done else {})},
                    "providerID": "prov", "modelID": "big", "tokens": tokens, "cost": cost}
            self.db.execute("insert into message values (?, ?, ?, ?, ?)", (mid, sid, t, t, json.dumps(data)))
            parts = [{"type": "text", "text": text}] if text else []
            for c in content:
                parts.append({"type": "tool", "tool": c["name"], "callID": c["id"], "state": c["state"]}
                             if c["type"] == "tool" else c)
            for i, p in enumerate(parts):
                self.db.execute("insert into part values (?, ?, ?, ?, ?, ?)",
                                (f"{mid}p{i}", mid, sid, t, t, json.dumps(p)))
        else:
            data = {"time": {"created": t, **({"completed": t + 500} if done else {})}}
            if role == "user":
                data["text"] = text
            else:
                data.update(model=model, tokens=tokens, cost=cost, content=list(content))
            self.db.execute("insert into session_message values (?, ?, ?, ?, ?, ?, ?)",
                            (mid, sid, role, self.seq, t, t, json.dumps(data)))
        self.db.commit()
        return mid

    def state(self, sid, rec):
        with open(os.path.join(self.state_dir, f"{sid}.json"), "w") as f:
            json.dump(dict(rec, pid=rec.get("pid", os.getpid())), f)

    def reader(self):
        oc = ccwatch.OpenCode(self.db_path, self.state_dir)
        self.addCleanup(oc.close)
        return oc

    def tool(self, name, inp, meta=None, status="completed", call=None):
        self.seq += 1
        return {"type": "tool", "id": call or f"c{self.seq}", "name": name,
                "state": {"status": status, "input": inp, "metadata": meta or {}}}


class OpenCodeLedgerTest(OpenCodeFixture):
    def test_today(self):
        self.session("s1")
        self.session("k1", parent="s1", title="explore (@explore subagent)")
        self.message("s1", "user", age=50, text="add a frontend")
        self.message("k1", "user", age=45, text="look around")  # a sub-agent's task prompt: not yours
        self.message("s1", "assistant", age=40, cost=0.25, content=[
            self.tool("edit", {"filePath": "/work/oc/a.js"}, {"filediff": {"file": "/work/oc/a.js", "additions": 4, "deletions": 1}}),
            self.tool("write", {"filePath": "/work/oc/b.js", "content": "1\n2\n"}),
            self.tool("bash", {"command": "git commit -m ok"}, {"exit": 0}),
            self.tool("bash", {"command": "git commit -m bad"}, {"exit": 1}),
            self.tool("edit", {"filePath": "/work/oc/c.js"}, status="running"),
        ])
        ledger = ccwatch.TokenLedger(self.reader())
        ledger.refresh()
        p = ledger.projects["/work/oc"]
        self.assertEqual(p.sources, {"opencode"})
        self.assertEqual(p.prompts, 1)
        self.assertEqual(p.commits, 1)
        self.assertEqual((p.added, p.removed, sorted(p.files)), (6, 1, ["/work/oc/a.js", "/work/oc/b.js"]))
        self.assertEqual(p.sessions, {"oc:s1"})
        t = ledger.oc.totals()
        self.assertEqual((t["input"], t["cache_write"], t["cache_read"], t["output"], t["messages"]), (10, 3, 100, 7, 1))
        self.assertAlmostEqual(t["cost"], 0.25)
        self.assertEqual(t["models"], {"Big Model": 7} if not self.V1 else {"big": 7})
        self.assertEqual(p.output, 7)

        # rescanning counts nothing twice; a new prompt is picked up
        self.message("s1", "user", age=1, text="more")
        ledger.last_scan = 0
        ledger.refresh()
        self.assertEqual((ledger.projects["/work/oc"].prompts, ledger.projects["/work/oc"].commits), (2, 1))

    def test_tool_finishing_later(self):
        """OpenCode rewrites a message as its step goes on; a tool counts once it has completed."""
        self.session("s1")
        call = self.tool("write", {"path": "notes.txt", "content": "a\nb\nc"}, status="running", call="w1")
        mid = self.message("s1", "assistant", age=20, content=[call])
        ledger = ccwatch.TokenLedger(self.reader())
        ledger.refresh()
        self.assertEqual(ledger.projects["/work/oc"].added, 0)
        call["state"]["status"] = "completed"
        if self.V1:
            self.db.execute("update part set data = ?, time_updated = ? where message_id = ?",
                            (json.dumps({"type": "tool", "tool": "write", "callID": "w1", "state": call["state"]}), self.now, mid))
        else:
            data = json.loads(self.db.execute("select data from session_message where id = ?", (mid,)).fetchone()[0])
            data["content"] = [call]
            self.db.execute("update session_message set data = ?, time_updated = ? where id = ?", (json.dumps(data), self.now, mid))
        self.db.commit()
        for _ in range(2):  # and only once
            ledger.last_scan = 0
            ledger.refresh()
        p = ledger.projects["/work/oc"]
        self.assertEqual((p.added, sorted(p.files)), (3, ["/work/oc/notes.txt"]))


class OpenCodeLedgerV1Test(OpenCodeLedgerTest):
    V1 = True


class OpenCodeLiveTest(OpenCodeFixture):
    def test_open_sessions(self):
        self.session("recent", age=60)
        self.session("old", age=ccwatch.OC_OPEN_SECS + 600)
        self.message("recent", "user", age=50, text="hi")
        self.message("recent", "assistant", age=40, content=[{"type": "text", "text": "Hello there\nmore"}],
                     tokens={"input": 100, "output": 100, "cache": {"read": 300}})
        oc = self.reader()
        oc.refresh()
        self.assertIsNone(oc.error)
        self.assertEqual(set(oc.sessions), {"recent"})
        s = oc.sessions["recent"]
        self.assertEqual((s.status, s.waiting, s.name, s.title), ("idle", False, "oc", "Fix it"))
        self.assertEqual(s.activity, ("text", "Writing", "Hello there"))
        self.assertEqual(s.model, "Big Model")
        self.assertAlmostEqual(s.sl["context_window"]["used_percentage"], 50.0)
        self.assertEqual(s.sl["cost"]["total_cost_usd"], 0.5)

    def test_working_inferred_without_plugin(self):
        self.session("s1", age=5)
        self.message("s1", "user", age=20, text="go")
        self.message("s1", "assistant", age=5, done=False, content=[self.tool("bash", {"command": "make test"}, status="running")])
        oc = self.reader()
        oc.refresh()
        s = oc.sessions["s1"]
        self.assertTrue(s.busy)
        self.assertEqual(s.activity, ("tool", "Bash", "make test"))
        self.assertAlmostEqual(s.state_secs(), 20, delta=2)  # since the prompt, not the latest step

    def test_plugin_state(self):
        self.session("s1", age=7200)  # quiet for two hours, but the plugin says it's waiting
        self.session("s2", age=60)
        self.session("k2", parent="s2", age=60)
        self.message("s1", "assistant", age=7200)
        self.state("s1", {"state": "waiting", "reason": "permission", "tool": "shell", "detail": "rm -rf build", "since": 5})
        self.state("s2", {"state": "busy", "since": 10})
        self.state("k2", {"state": "waiting", "reason": "question", "tool": "question", "detail": "Which one?", "since": 9})
        oc = self.reader()
        oc.refresh()
        self.assertEqual(oc.sessions["s1"].wait["detail"], "rm -rf build")
        w = oc.sessions["s2"].wait  # its sub-agent's question waits on you
        self.assertEqual((w["reason"], w["detail"]), ("question", "Which one?"))
        self.assertEqual([sa.desc for sa in oc.sessions["s2"].active_subs()], ["Fix it"])

        # a record from an OpenCode process that has gone is ignored
        self.state("s1", {"state": "waiting", "reason": "permission", "since": 5, "pid": 2 ** 22 + 12345})
        oc.refresh()
        self.assertNotIn("s1", oc.sessions)

    def test_attention(self):
        """A turn that ended while no OpenCode window showed it (2.x records that)."""
        if self.V1:
            return
        cases = {"done": (30, None, "succeeded", "unseen"), "bad": (30, None, "failed", "failed"),
                 "esc": (30, None, "interrupted", "interrupted"), "seen": (30, 30, "failed", None)}
        for sid, (idle, viewed, outcome, _) in cases.items():
            self.session(sid, age=20, idle=idle, viewed=viewed, outcome=outcome)
            self.message(sid, "user", age=40, text="go")
            self.message(sid, "assistant", age=30)
        oc = self.reader()
        oc.refresh()
        for sid, (*_, want) in cases.items():
            s = oc.sessions[sid]
            self.assertEqual((s.attention, s.idle), (want, True), sid)
        self.assertEqual(oc.sessions["seen"].outcome, "failed")

    def test_idle_from_database(self):
        """Without the plugin, 2.x's turn-ended time says a turn is over even if its last reply looks unfinished."""
        if self.V1:
            return
        self.session("s1", age=5, idle=4, viewed=4, outcome="interrupted")
        self.message("s1", "user", age=20, text="go")
        self.message("s1", "assistant", age=5, done=False)
        self.session("s2", age=5, idle=60, viewed=60, outcome="succeeded")  # ended before this prompt
        self.message("s2", "user", age=10, text="next")
        oc = self.reader()
        oc.refresh()
        self.assertEqual((oc.sessions["s1"].busy, oc.sessions["s1"].attention), (False, None))
        self.assertTrue(oc.sessions["s2"].busy)

    def test_unfamiliar_database(self):
        path = os.path.join(self.tmp, "other.db")
        db = ccwatch.sqlite3.connect(path)
        db.execute("create table something (x)")
        db.close()
        oc = ccwatch.OpenCode(path, self.state_dir)
        self.addCleanup(oc.close)
        oc.refresh()
        self.assertIn("unfamiliar", oc.error)
        self.assertEqual(oc.sessions, {})
        ledger = ccwatch.TokenLedger(oc)
        ledger.refresh()  # carries on without OpenCode
        self.assertEqual(oc.totals()["messages"], 0)

    def test_snapshot_and_store(self):
        self.session("s1", age=5)
        self.message("s1", "user", age=5, text="go")
        with mock.patch.object(ccwatch, "OC_DB", self.db_path), mock.patch.object(ccwatch, "OC_STATE_DIR", self.state_dir), \
                mock.patch.object(ccwatch, "SESS_DIR", os.path.join(self.tmp, "none")), \
                mock.patch.object(ccwatch, "PROJ_DIR", os.path.join(self.tmp, "none")), \
                mock.patch.object(ccwatch, "STATUSLINE_DIR", os.path.join(self.tmp, "none")), \
                mock.patch.object(ccwatch, "STATE_DIR", os.path.join(self.tmp, "none")):
            ccwatch._oc_installed["at"] = 0
            store = ccwatch.Store(opencode=True)
            self.addCleanup(lambda: store.oc and store.oc.close())
            store.refresh()
            snap = ccwatch.snapshot(store)
            self.assertEqual(snap["opencode"]["enabled"], True)
            s = next(x for x in snap["sessions"] if x["source"] == "opencode")
            self.assertEqual((s["key"], s["pid"], s["state"]), ("opencode:s1", None, "busy"))
            self.assertEqual(snap["projects"]["items"][0]["sources"], ["opencode"])
            store.set_opencode(False)
            store.refresh()
            snap = ccwatch.snapshot(store)
            self.assertEqual((snap["opencode"]["enabled"], snap["sessions"], snap["projects"]["items"]), (False, [], []))
        ccwatch._oc_installed["at"] = 0


class OpenCodeLiveV1Test(OpenCodeLiveTest):
    V1 = True

    def test_open_sessions(self):  # 1.x has no model list in the database: no context gauge
        self.session("recent", age=60)
        self.message("recent", "assistant", age=40, content=[{"type": "reasoning"}])
        oc = self.reader()
        oc.refresh()
        s = oc.sessions["recent"]
        self.assertEqual((s.activity, s.model, s.sl["context_window"]), (("think", "Thinking", ""), "big", {}))

    def test_snapshot_and_store(self):
        pass


class FocusTest(unittest.TestCase):
    PS = "\n".join([
        "    1     0 ??       /sbin/launchd",
        "  500     1 ??       /Applications/iTerm.app/Contents/MacOS/iTerm2",
        "  600     1 ??       /Users/me/Library/Application Support/iTerm2/iTermServer-3.7.4",
        "  700   600 ttys005  /usr/bin/login",
        "  800   700 ttys005  -zsh",
        "  900   800 ttys005  claude",
    ])

    def test_iterm_server_shells_belong_to_iterm(self):
        """iTerm2 runs shells under iTermServer, whose parent is launchd, not iTerm2."""
        ps = mock.Mock(stdout=self.PS)
        with mock.patch("subprocess.run", return_value=ps), \
                mock.patch("plistlib.load", return_value={"CFBundleIdentifier": "com.googlecode.iterm2"}), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"")):
            self.assertEqual(ccwatch.host_of(900), ("/Applications/iTerm.app", "com.googlecode.iterm2", 500, "/dev/ttys005"))

    def test_vscode_folder_uses_last_active_window(self):
        """While the editor runs, its open windows are only in lastActiveWindow."""
        state = {"windowsState": {"openedWindows": [], "lastActiveWindow": {"folder": "file:///work/my%20app"}}}
        with mock.patch.object(ccwatch, "load_json", return_value=state):
            self.assertEqual(ccwatch.vscode_folder("Cursor", "/work/my app/src"), "/work/my app")
            self.assertEqual(ccwatch.vscode_folder("Cursor", "/work/other"), "")


class DrawCardTest(unittest.TestCase):
    """Renders cards into a fake curses screen."""

    class Screen:
        def __init__(self, h, w):
            self.h, self.w, self.rows = h, w, [[" "] * w for _ in range(h)]

        def getmaxyx(self):
            return self.h, self.w

        def addstr(self, y, x, text, attr=0):
            for i, ch in enumerate(text):
                if 0 <= x + i < self.w:
                    self.rows[y][x + i] = ch

        def text(self):
            return "\n".join("".join(r) for r in self.rows)

    def test_waiting_card_with_sub_agents(self):  # used to crash: the wait record shadowed the card width
        s = ccwatch.OCSession("s1")
        s.info = {"cwd": "/work/oc", "status": "busy", "startedAt": time.time() * 1000}
        s.wait = {"state": "waiting", "reason": "permission", "tool": "shell", "detail": "rm -rf build", "since": time.time()}
        s.sl = {"context_window": {}, "cost": {"total_cost_usd": 0.03}}
        s.subs = {"k": ccwatch.OCSub("explore the code", time.time())}
        scr = self.Screen(14, 60)
        with mock.patch.object(ccwatch.curses, "color_pair", lambda n: 0):
            ccwatch.draw_card(ccwatch.Screen(scr), s, 0, 0, 56, ccwatch.card_height(s), 0)
        out = scr.text()
        for part in ("oc OpenCode", "NEEDS YOU", "approve ⚒ shell", "rm -rf build", "explore the code", "ctx  ? · $0.03"):
            self.assertIn(part, out)


class OpenCodePluginInstallTest(unittest.TestCase):
    def test_install_and_uninstall(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        plugin = os.path.join(tmp, "plugins", "ccwatch.js")
        settings = os.path.join(tmp, "settings.json")
        with open(settings, "w") as f:  # Claude side already set up, so only the plugin changes
            json.dump(ccwatch.merge_hooks({"statusLine": {"command": "echo x"}},
                                          ccwatch.hook_source()[1]), f)
        with mock.patch.object(ccwatch, "OC_PLUGIN", plugin), mock.patch.object(ccwatch, "SETTINGS", settings), \
                mock.patch("builtins.print"):
            self.assertEqual(ccwatch.install(), 0)
            self.assertFalse(os.path.exists(plugin))  # only with opencode=True
            self.assertEqual(ccwatch.install(opencode=True), 0)
            self.assertTrue(ccwatch.oc_plugin_installed())
            with open(plugin, "a") as f:
                f.write("// old\n")
            ccwatch.install()  # an installed plugin is kept up to date
            with open(os.path.join(ROOT, "ccwatch-opencode.js")) as f, open(plugin) as g:
                self.assertEqual(f.read(), g.read())
            ccwatch.uninstall()
            self.assertFalse(os.path.exists(plugin))
            with open(plugin, "w") as f:  # someone else's file of that name is left alone
                f.write("export default {}\n")
            ccwatch.install(opencode=True)
            ccwatch.uninstall()
            self.assertTrue(os.path.exists(plugin))


PLUGIN_DRIVER = r"""
import plugin from %s
const [mode, events] = [process.argv[2], JSON.parse(process.argv[3])]
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
if (mode === "v2") {
  async function* stream() { for (const e of events) { yield e; await sleep(5) } }
  plugin.setup({ event: { subscribe: () => stream() } })
} else {
  const hooks = await plugin.server()
  for (const e of events) { await hooks.event({ event: e }); await sleep(5) }
}
await sleep(150)
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpenCodePluginTest(unittest.TestCase):
    """Drives ccwatch-opencode.js with OpenCode's events (2.x and 1.x shapes)."""

    def run_plugin(self, mode, events):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        driver = os.path.join(home, "driver.mjs")
        with open(driver, "w") as f:
            f.write(PLUGIN_DRIVER % json.dumps(pathlib.Path(ROOT, "ccwatch-opencode.js").as_uri()))
        subprocess.run(["node", driver, mode, json.dumps(events)], env=dict(os.environ, HOME=home), check=True, timeout=30)
        d = os.path.join(home, ".cache", "ccwatch", "opencode")
        return {n[:-5]: ccwatch.load_json(os.path.join(d, n)) for n in os.listdir(d) if n.endswith(".json")} \
            if os.path.isdir(d) else {}

    def test_v2(self):
        ask = {"type": "permission.asked", "data": {"id": "per_1", "sessionID": "s1", "action": "shell",
                                                     "resources": ["rm -rf build\nmore"]}}
        recs = self.run_plugin("v2", [{"type": "session.execution.started", "data": {"sessionID": "s1"}}, ask])
        r = recs["s1"]
        self.assertEqual((r["state"], r["reason"], r["tool"], r["detail"], r["pid"] > 0),
                         ("waiting", "permission", "shell", "rm -rf build", True))
        recs = self.run_plugin("v2", [
            ask, {"type": "form.created", "data": {"form": {"id": "frm_1", "sessionID": "s1", "title": "Pick one"}}},
            {"type": "permission.replied", "data": {"sessionID": "s1", "requestID": "per_1", "reply": "once"}}])
        self.assertEqual((recs["s1"]["state"], recs["s1"]["detail"]), ("waiting", "Pick one"))  # the form is still open
        recs = self.run_plugin("v2", [
            {"type": "session.execution.started", "data": {"sessionID": "s1"}}, ask,
            {"type": "session.execution.interrupted", "data": {"sessionID": "s1"}}])
        self.assertEqual(recs["s1"]["state"], "idle")  # a turn that ended leaves no prompt open

    def test_v1(self):
        recs = self.run_plugin("v1", [
            {"type": "session.status", "properties": {"sessionID": "s1", "status": {"type": "busy"}}},
            {"type": "question.asked", "properties": {"id": "q1", "sessionID": "s1", "questions": [{"question": "Why?"}]}}])
        self.assertEqual((recs["s1"]["state"], recs["s1"]["reason"]), ("waiting", "question"))
        recs = self.run_plugin("v1", [
            {"type": "session.status", "properties": {"sessionID": "s1", "status": {"type": "busy"}}},
            {"type": "session.idle", "properties": {"sessionID": "s1"}}])
        self.assertEqual(recs["s1"]["state"], "idle")


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class HookTest(unittest.TestCase):
    def run_hook(self, home, payload):
        subprocess.run([os.path.join(ROOT, "ccwatch-hook.sh")], input=json.dumps(payload), text=True,
                       env=dict(os.environ, HOME=home), check=True)

    def test_mark_and_clear(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        state = os.path.join(home, ".cache", "ccwatch", "state", "abc.json")
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                             "tool_input": {"command": "rm -rf build\nmore"}})
        with open(state) as f:
            w = json.load(f)
        self.assertEqual((w["state"], w["reason"], w["tool"], w["detail"]), ("waiting", "permission", "Bash", "rm -rf build"))
        # a parallel call finishing while the prompt is still open leaves it alone
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "ls"}})
        self.assertTrue(os.path.exists(state))
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "rm -rf build\nmore"}})
        self.assertFalse(os.path.exists(state))
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                             "tool_input": {"command": "x"}})
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "Stop"})
        self.assertFalse(os.path.exists(state))


if __name__ == "__main__":
    unittest.main()
