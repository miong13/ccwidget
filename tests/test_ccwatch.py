"""Unit tests for ccwatch.py (stdlib only): python3 -m unittest discover -s tests"""
import json
import os
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
        self.run_hook(home, {"session_id": "abc", "hook_event_name": "PostToolUse"})
        self.assertFalse(os.path.exists(state))


if __name__ == "__main__":
    unittest.main()
