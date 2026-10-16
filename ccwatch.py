#!/usr/bin/env python3
"""ccwatch — animated terminal dashboard of running Claude Code sessions.

Data sources (all local; ccwatch only reads them, and only ever deletes stale
files from its own ~/.cache/ccwatch):
  ~/.claude/sessions/<pid>.json            live registry written by each claude process
  ~/.claude/projects/*/<sessionId>.jsonl   session transcript (current tool, title, model)
  ~/.claude/projects/*/<sessionId>/subagents/*.jsonl   sub-agent transcripts
  ~/.cache/ccwatch/statusline/<sessionId>.json  status line input saved by
      ~/.claude/statusline-command.sh (cost, context %, plan rate limits)
  ~/.cache/ccwatch/state/<sessionId>.json  "waiting on you" flag written by
      ccwatch-hook.sh (permission prompts, questions, MCP input requests)
With --opencode, OpenCode sessions too:
  ~/.local/share/opencode/opencode.db      OpenCode's database, opened read-only
  ~/.cache/ccwatch/opencode/<sessionId>.json  working / idle / "waiting on you", written by
      the ccwatch OpenCode plugin (ccwatch-opencode.js)

The usage and "projects today" panels come from one incremental scan of every
transcript changed today (tokens, time spent, prompts, edits, commits per directory).

Keys: q quit · i hide/show idle sessions · u compact/expand usage and projects panels
      o show/hide OpenCode sessions (when OpenCode is installed)
Usage: ccwatch.py [--once] [--json] [--opencode]   (--json streams one snapshot per second;
                                     with --once, just one. --opencode adds OpenCode sessions)
       ccwatch.py --focus <pid>        bring the terminal/editor window running that session forward
       ccwatch.py --install [--opencode] [--dry-run]   add the hook and status line capture to
                                     ~/.claude (and with --opencode, the OpenCode plugin)
       ccwatch.py --uninstall [--dry-run]   remove them again
       ccwatch.py --doctor [--opencode]     check every data source and say what's missing
"""
import curses
import glob
import json
import os
import re
import sqlite3
import sys
import time
from collections import deque
from datetime import date, datetime

HOME = os.path.expanduser("~")
SESS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJ_DIR = os.path.join(HOME, ".claude", "projects")
STATUSLINE_DIR = os.path.join(HOME, ".cache", "ccwatch", "statusline")
STATE_DIR = os.path.join(HOME, ".cache", "ccwatch", "state")

FPS = 12
DATA_INTERVAL = 1.0        # seconds between filesystem polls
SUB_ACTIVE_SECS = 30       # sub-agent counts as active if its transcript changed this recently
TAIL_BYTES = 256 * 1024
SPARK_LEN = 20
MAX_CARD_W = 80
LEDGER_INTERVAL = 5.0      # seconds between scans for today's token totals
IDLE_GAP = 300             # pauses longer than this between transcript events don't count as time spent
PRUNE_INTERVAL = 3600      # seconds between clean-ups of ~/.cache/ccwatch
CAPTURE_MAX_AGE = 8 * 86400   # status line captures older than this are deleted (longest limit window + a day)
STATE_MAX_AGE = 86400      # "needs you" flags of sessions that are gone and this old are deleted

XDG_DATA = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
OC_DB = os.path.join(XDG_DATA, "opencode", "opencode.db")
OC_PLUGIN_NAME = "ccwatch-opencode.js"
OC_PLUGIN = os.path.join(XDG_CONFIG, "opencode", "plugins", "ccwatch.js")
OC_STATE_DIR = os.path.join(HOME, ".cache", "ccwatch", "opencode")
OC_BINS = ("/opt/homebrew/bin/opencode", "/usr/local/bin/opencode", "~/.opencode/bin/opencode",
           "~/.local/bin/opencode", "~/.bun/bin/opencode", "~/.npm-global/bin/opencode")
OC_OPEN_SECS = 600         # an idle OpenCode session no terminal shows keeps its card this long
OC_STALE_SECS = 600        # without the plugin, an unfinished reply this quiet counts as abandoned
OC_CLIENT_INTERVAL = 5.0   # seconds between looks for running OpenCode terminals

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPARK = "▁▁▂▃▄▅▆▇█"  # index 0 = no activity

C_BUSY, C_IDLE, C_WARN, C_ACCENT, C_SUB, C_DIM, C_DANGER = range(1, 8)


# ─── data ────────────────────────────────────────────────────────────────────

def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_tail(path, n=TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > n:
        lines = lines[1:]  # first line is probably partial
    out = []
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def summarize_input(inp):
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "command", "file_path", "pattern", "url", "query", "prompt", "skill",
                "filePath", "path"):  # the last two: OpenCode's names
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            if key in ("file_path", "filePath", "path"):
                v = os.path.basename(v.rstrip("/")) or v
            return v.strip().splitlines()[0]
    return ""


def pretty_model(m):
    if not m:
        return ""
    m = m.replace("claude-", "")
    m = re.sub(r"-\d{8}$", "", m)
    m = re.sub(r"-(\d+)-(\d+)$", r" \1.\2", m)
    return m.replace("-", " ")


def scan_transcript(entries):
    """Walk the transcript backwards for title, model and the latest activity."""
    title = prompt = model = activity = None
    for e in reversed(entries):
        t = e.get("type")
        if title is None and t in ("custom-title", "ai-title"):
            title = e.get("customTitle") or e.get("aiTitle")
        if prompt is None and t == "last-prompt":
            prompt = e.get("lastPrompt")
        if t in ("assistant", "user") and not e.get("isSidechain") and not e.get("isMeta"):
            msg = e.get("message") or {}
            if model is None and t == "assistant":
                model = msg.get("model")
            if activity is None:
                content = msg.get("content")
                if t == "assistant" and isinstance(content, list) and content:
                    last = content[-1]
                    kind = last.get("type")
                    if kind == "tool_use":
                        activity = ("tool", last.get("name", "?"), summarize_input(last.get("input")))
                    elif kind == "text":
                        lines = (last.get("text") or "").strip().splitlines()
                        activity = ("text", "Writing", lines[0] if lines else "")
                    else:
                        activity = ("think", "Thinking", "")
                elif t == "user":
                    if isinstance(content, list) and any(c.get("type") == "tool_result" for c in content if isinstance(c, dict)):
                        activity = ("think", "Reading results", "")
                    else:
                        activity = ("think", "Reading prompt", "")
        if title and model and activity:
            break
    return title or prompt, model, activity


class SubAgent:
    def __init__(self, path):
        self.path = path
        self.desc = None
        self.mtime = 0
        meta = path[:-len(".jsonl")] + ".meta.json"
        try:
            with open(meta) as f:
                m = json.load(f)
            self.desc = m.get("description")
            self.kind = m.get("agentType", "agent")
        except (OSError, ValueError):
            self.kind = "agent"
        if not self.desc:
            self.desc = os.path.basename(path)[:-6]


class Session:
    source = "claude"
    attention = None  # OpenCode only: a finished turn you haven't seen ("unseen", "failed", "interrupted")
    outcome = None    # OpenCode only: how its last turn ended

    def __init__(self, pid):
        self.pid = pid
        self.sid = None
        self.info = {}
        self.transcript = None
        self.size = None
        self.spark = deque([0] * SPARK_LEN, maxlen=SPARK_LEN)
        self.title = self.model = self.activity = None
        self.subs = {}
        self.first_seen = time.time()
        self.sl = {}  # latest status line input for this session
        self.wait = None  # waiting-on-you record from ccwatch-hook.sh

    def update(self, info):
        self.info = info
        sid = info.get("sessionId")
        if sid != self.sid:  # new session in same process (e.g. /clear)
            self.sid, self.transcript, self.size, self.subs, self.sl = sid, None, None, {}, {}
        self.sl = load_json(os.path.join(STATUSLINE_DIR, f"{sid}.json")) or self.sl
        if not self.transcript or not os.path.exists(self.transcript):
            hits = glob.glob(os.path.join(PROJ_DIR, "*", f"{sid}.jsonl"))
            self.transcript = hits[0] if hits else None

        delta = 0
        if self.transcript:
            try:
                size = os.path.getsize(self.transcript)
            except OSError:
                size = None
            if size is not None:
                if self.size is not None:
                    delta = max(0, size - self.size)
                if size != self.size:
                    self.title, self.model, self.activity = scan_transcript(read_tail(self.transcript))
                self.size = size
        self.spark.append(delta)

        self.wait = self._load_wait()

        if self.transcript:
            sub_dir = os.path.join(os.path.dirname(self.transcript), sid, "subagents")
            for p in glob.glob(os.path.join(sub_dir, "*.jsonl")):
                sa = self.subs.get(p) or SubAgent(p)
                try:
                    sa.mtime = os.path.getmtime(p)
                except OSError:
                    continue
                self.subs[p] = sa

    def _load_wait(self):
        """Whether the session is blocked on you, with the hook's record for details.

        Claude Code marks the session "waiting" in its registry for as long as a
        prompt or dialog is open, so that decides. The hook's flag alone can't:
        parallel tool calls, subagents and background tasks keep writing to the
        transcript and firing PostToolUse while a prompt is still open. Older
        Claude Code versions don't report "waiting"; for them the flag stands
        until the session's status changes after it (answered, denied, interrupted).
        """
        w = load_json(os.path.join(STATE_DIR, f"{self.sid}.json"))
        if not w or w.get("state") != "waiting":
            w = None
        updated = (self.info.get("statusUpdatedAt") or 0) / 1000
        if self.info.get("status") == "waiting":
            if w and (w.get("since") or 0) >= updated - 5:
                return w
            # no hook record for this prompt (a dialog, or the hook isn't installed)
            why = self.info.get("waitingFor") or ""
            reason = {"permission prompt": "permission", "sandbox request": "permission",
                      "input needed": "input", "goal proposal": "question"}.get(why, "dialog")
            return {"state": "waiting", "reason": reason, "tool": "", "detail": why, "since": int(updated or time.time())}
        if w and updated > (w.get("since") or 0) + 2:
            return None
        return w

    @property
    def waiting(self):
        return self.wait is not None

    @property
    def status(self):
        return (self.info.get("status") or "unknown").lower()

    @property
    def busy(self):
        return self.status in ("busy", "working", "running", "active")

    @property
    def idle(self):
        return self.status == "idle"

    @property
    def name(self):
        return self.info.get("name") or os.path.basename(self.info.get("cwd", "")) or f"pid {self.pid}"

    @property
    def project(self):
        return os.path.basename(self.info.get("cwd", "").rstrip("/")) or "?"

    def state_secs(self):
        ts = self.info.get("statusUpdatedAt") or self.info.get("updatedAt")
        return time.time() - ts / 1000 if ts else 0

    def uptime_secs(self):
        ts = self.info.get("startedAt")
        return time.time() - ts / 1000 if ts else 0

    def active_subs(self):
        now = time.time()
        subs = [s for s in self.subs.values() if now - s.mtime < SUB_ACTIVE_SECS]
        return sorted(subs, key=lambda s: -s.mtime)


# `git commit` where a shell command starts, not merely mentioned in a string
GIT_COMMIT = re.compile(r"(?:^|[;&|(]|\bthen|\bdo)\s*git\s+(?:-[Cc]\s+\S+\s+)*commit(?![\w-])", re.M)


def active_time(stamps, midnight):
    """Seconds spent, overall and per hour of today, from sorted event times.

    Gaps up to IDLE_GAP count as time spent (Claude working, or you reading and
    typing); longer gaps count as a break.
    """
    total, hourly = 0.0, [0.0] * 24
    for a, b in zip(stamps, stamps[1:]):
        gap = b - a
        if 0 < gap <= IDLE_GAP:
            total += gap
            hourly[min(23, max(0, int((b - midnight) // 3600)))] += gap
    return total, hourly


class Project:
    """Today's work in one directory, across all of its sessions and sub-agents."""

    def __init__(self, cwd):
        self.cwd = cwd
        self.name = os.path.basename(cwd.rstrip("/")) or cwd
        self.branch = ""
        self.sessions = set()
        self.stamps = []           # epoch time of every message today
        self.prompts = 0
        self.files = set()         # files changed by Edit / Write
        self.added = self.removed = 0
        self.commits = 0
        self.sources = set()       # "claude", "opencode"
        # derived in TokenLedger.summarize()
        self.active, self.hourly = 0.0, [0.0] * 24
        self.first = self.last = 0.0
        self.output = 0


class TokenLedger:
    """Today's token usage and per-project work across every transcript
    (main sessions and sub-agents).

    Reads each file incrementally; assistant messages are written in several
    lines sharing one message id, so usage is keyed by id and the last one wins.
    With an OpenCode reader, its sessions' work joins the same projects.
    """

    def __init__(self, oc=None):
        self.oc = oc
        self.day = None
        self.last_scan = 0.0
        self._reset(None)

    def _reset(self, day):
        self.day = day
        if self.oc:
            self.oc.reset_day(day)
        self.offsets = {}
        self.usage = {}
        self.projects = {}         # cwd -> Project
        self.sess_proj = {}        # sessionId -> cwd it started in
        self.seen = set()          # uuids of user entries already counted
        self.pending_commits = {}  # tool_use id of a `git commit` -> cwd
        self.active, self.hourly = 0.0, [0.0] * 24  # union over all projects
        self.dirty = True

    def refresh(self):
        today = date.today()
        if today != self.day:
            self._reset(today)
        midnight = datetime.combine(today, datetime.min.time()).timestamp()
        paths = glob.glob(os.path.join(PROJ_DIR, "*", "*.jsonl"))
        paths += glob.glob(os.path.join(PROJ_DIR, "*", "*", "subagents", "*.jsonl"))
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < midnight or st.st_size == self.offsets.get(path, 0):
                continue
            self._read(path, self.offsets.get(path, 0))
        if self.oc:
            self.oc.scan(self, midnight)
        if self.dirty:
            self.summarize(midnight)
        self.last_scan = time.time()

    def _read(self, path, start):
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offsets[path] = start + end + 1
        for line in data[:end].split(b"\n"):
            if b'"type":"assistant"' not in line and b'"type":"user"' not in line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            t = e.get("type")
            if ts.date() != self.day or t not in ("assistant", "user"):
                continue
            sid = e.get("sessionId")
            key = self.sess_proj.get(sid) or e.get("cwd")
            if not key:
                continue
            if sid:
                self.sess_proj.setdefault(sid, key)
                key = self.sess_proj[sid]
            p = self.projects.get(key)
            if p is None:
                p = self.projects[key] = Project(key)
            p.sessions.add(sid)
            p.sources.add("claude")
            p.stamps.append(ts.timestamp())
            if e.get("gitBranch") and e["gitBranch"] != "HEAD":
                p.branch = e["gitBranch"]
            self.dirty = True
            msg = e.get("message") or {}
            if t == "assistant":
                self._assistant(msg, e, ts, key)
            else:
                self._user(msg, e, p)

    def _assistant(self, msg, e, ts, key):
        u = msg.get("usage")
        if isinstance(u, dict):
            self.usage[msg.get("id") or e.get("uuid")] = (
                u.get("input_tokens") or 0,
                u.get("cache_creation_input_tokens") or 0,
                u.get("cache_read_input_tokens") or 0,
                u.get("output_tokens") or 0,
                ts.hour,
                msg.get("model") or "",
                key,
            )
        content = msg.get("content")
        for c in content if isinstance(content, list) else ():
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                cmd = (c.get("input") or {}).get("command")
                if isinstance(cmd, str) and GIT_COMMIT.search(cmd):
                    self.pending_commits[c.get("id")] = key

    def _user(self, msg, e, p):
        uuid = e.get("uuid")
        if uuid in self.seen:  # resumed sessions can repeat earlier entries
            return
        self.seen.add(uuid)
        content = msg.get("content")
        results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if results:
            for c in results:  # a commit counts once its command succeeded
                key = self.pending_commits.pop(c.get("tool_use_id"), None)
                if key in self.projects and not c.get("is_error"):
                    self.projects[key].commits += 1
            r = e.get("toolUseResult")
            if isinstance(r, dict) and r.get("filePath"):
                if r.get("type") == "create" and isinstance(r.get("content"), str):
                    p.files.add(r["filePath"])
                    p.added += len(r["content"].splitlines())
                elif r.get("structuredPatch"):
                    p.files.add(r["filePath"])
                    for hunk in r["structuredPatch"]:
                        for ln in hunk.get("lines") or ():
                            if ln.startswith("+"):
                                p.added += 1
                            elif ln.startswith("-"):
                                p.removed += 1
            return
        if e.get("isSidechain") or e.get("isMeta"):
            return
        origin = e.get("origin")
        if isinstance(origin, dict):
            human = origin.get("kind") == "human"
        else:  # older transcripts: skip slash-command and system wrappers
            text = content if isinstance(content, str) else next(
                (c.get("text", "") for c in content or () if isinstance(c, dict) and c.get("type") == "text"), "")
            human = not text.lstrip().startswith("<")
        if human:
            p.prompts += 1

    def summarize(self, midnight):
        every = []
        for p in self.projects.values():
            p.stamps.sort()
            p.active, p.hourly = active_time(p.stamps, midnight)
            p.first, p.last = p.stamps[0], p.stamps[-1]
            p.output = 0
            every.extend(p.stamps)
        every.sort()
        self.active, self.hourly = active_time(every, midnight)
        for *_, out, _hour, _model, key in self.usage.values():
            if key in self.projects:
                self.projects[key].output += out
        for key, out in (self.oc.project_output() if self.oc else {}).items():
            if key in self.projects:
                self.projects[key].output += out
        self.dirty = False

    def totals(self):
        inp = cw = cr = out = 0
        hourly = [0] * 24          # output tokens per hour of today
        models = {}                # output tokens per model
        for a, b, c, d, hour, model, _key in self.usage.values():
            inp += a; cw += b; cr += c; out += d
            hourly[hour] += d
            if model and not model.startswith("<"):  # skip "<synthetic>" placeholders
                models[model] = models.get(model, 0) + d
        return {"input": inp, "cache_write": cw, "cache_read": cr, "output": out,
                "total": inp + cw + cr + out, "messages": len(self.usage),
                "hourly": hourly, "models": models}


# ─── OpenCode (opt-in: --opencode) ───────────────────────────────────────────
# OpenCode keeps its sessions in one SQLite database. ccwatch opens it read-only
# and reads only the session and message tables (never the account or credential
# ones). The database can't say that a session waits on you; the ccwatch plugin
# (ccwatch-opencode.js) records that, and working / idle, in OC_STATE_DIR.

_oc_installed = {"at": 0.0, "value": False}

# subcommands that aren't an interactive session (`opencode`, `opencode run`, `opencode mini` are)
OC_TOOLS = {"serve", "service", "web", "acp", "api", "attach", "auth", "debug", "mcp", "models", "plugin",
            "session", "stats", "export", "import", "github", "pr", "upgrade", "update", "uninstall", "agent"}


def opencode_installed():
    """Whether OpenCode is on this Mac: its database, or its command in a usual place.
    Checked once a minute (mid-update, the command can be missing for a while)."""
    import shutil
    if time.time() - _oc_installed["at"] > 60:
        found = os.path.exists(OC_DB) or bool(shutil.which("opencode")) or any(
            os.access(os.path.expanduser(p), os.X_OK) for p in OC_BINS)
        _oc_installed.update(at=time.time(), value=found)
    return _oc_installed["value"]


def opencode_clients(cwds):
    """{directory: pid} of the OpenCode terminals running now (not its background
    service). `cwds` caches each pid's working directory, so lsof only runs for new ones."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    procs = {}  # pid -> directory given on the command line, or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        argv = parts[1:]
        if os.path.basename(argv[0]) in ("node", "bun") and len(argv) > 1:
            argv = argv[1:]
        if os.path.basename(argv[0]) not in ("opencode", "opencode.exe"):
            continue
        words = [a for a in argv[1:] if not a.startswith("-")]
        if words and words[0] in OC_TOOLS:
            continue
        arg = words[0] if words and os.path.isabs(words[0]) and os.path.isdir(words[0]) else ""
        procs[int(parts[0])] = arg.rstrip("/")
    new = [p for p in procs if not procs[p] and p not in cwds]
    if new:
        lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"
        try:
            out = subprocess.run([lsof, "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, new))],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        pid = None
        for line in out.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("n") and pid:
                cwds[pid] = line[1:].rstrip("/")
    for p in list(cwds):
        if p not in procs:
            del cwds[p]
    return {(d or cwds.get(p)): p for p, d in procs.items() if d or cwds.get(p)}


def oc_v2_message(kind, data, created=0):
    """An OpenCode message in one shape whichever database layout it came from:
    {role, created, completed (ms), model (provider, id), tokens, cost, content, text}."""
    t = data.get("time") or {}
    m = data.get("model") or {}
    return {"role": kind, "created": t.get("created") or created, "completed": t.get("completed"),
            "model": (m.get("providerID") or "", m.get("id") or m.get("modelID") or ""),
            "tokens": data.get("tokens") or {}, "cost": data.get("cost") or 0,
            "content": [c for c in data.get("content") or () if isinstance(c, dict)],
            "text": data.get("text") if isinstance(data.get("text"), str) else ""}


def oc_v1_message(data, parts, created=0):
    """oc_v2_message() for OpenCode 1.x, whose message content lives in separate parts."""
    content, text = [], []
    for p in parts:
        kind = p.get("type")
        if kind == "tool":
            content.append({"type": "tool", "id": p.get("callID") or p.get("id"), "name": p.get("tool") or "",
                            "state": p.get("state") or {}})
        elif kind == "text":
            content.append({"type": "text", "text": p.get("text") or ""})
            if not p.get("synthetic"):
                text.append(p.get("text") or "")
        elif kind == "reasoning":
            content.append({"type": "reasoning"})
    t = data.get("time") or {}
    m = data.get("model") or {}
    return {"role": data.get("role") or "", "created": t.get("created") or created, "completed": t.get("completed"),
            "model": (data.get("providerID") or m.get("providerID") or "", data.get("modelID") or m.get("modelID") or ""),
            "tokens": data.get("tokens") or {}, "cost": data.get("cost") or 0, "content": content,
            "text": "\n".join(text)}


def oc_activity(msg):
    """("tool" | "text" | "think", label, detail) from a session's newest message, like scan_transcript()."""
    if msg["role"] != "assistant":
        return ("think", "Reading prompt", "")
    for item in reversed(msg["content"]):
        kind = item.get("type")
        if kind == "tool":
            name = item.get("name") or "?"
            return ("tool", name[:1].upper() + name[1:], summarize_input((item.get("state") or {}).get("input")))
        if kind == "text":
            lines = (item.get("text") or "").strip().splitlines()
            return ("text", "Writing", lines[0] if lines else "")
        if kind == "reasoning":
            return ("think", "Thinking", "")
    return ("think", "Thinking", "")


def oc_used(tokens):
    """Tokens in the context window after a reply: what went in, cached or not, plus what came out."""
    cache = tokens.get("cache") or {}
    return sum(int(v or 0) for v in (tokens.get("input"), cache.get("read"), cache.get("write"),
                                     tokens.get("output"), tokens.get("reasoning")))


class OCSub:
    """An OpenCode sub-agent: a child session, shown like a Claude sub-agent."""
    kind = "agent"

    def __init__(self, desc, mtime):
        self.desc, self.mtime = desc, mtime


class OCSession(Session):
    """An open OpenCode session. It has everything Session has, so the cards, the
    widget feed and the projects panel handle both the same way."""
    source = "opencode"

    def __init__(self, sid):
        super().__init__(None)
        self.sid = sid
        self.key = self.size = self.msg = None  # newest message: (id, updated), its size, the message
        self.ctx = None

    def update(self, oc, row, states, kids, pid):
        now = time.time()
        self.pid = pid  # the terminal running it, when one is open in its directory
        key, size, msg, turn = oc.latest(self.sid, self.key)
        if msg is not None:
            self.msg = msg
        if self.size is not None and key:  # the newest message grew, or a new one began
            self.spark.append(max(0, size - self.size) if self.key and key[0] == self.key[0] else size)
        else:
            self.spark.append(0)
        self.key, self.size = key, size
        msg = self.msg

        rec = states.get(self.sid)
        idle_at = row.get("time_idle")  # OpenCode 2.0.30+ records when each turn ended, and how
        if rec:  # the plugin knows
            busy, since = rec["state"] != "idle", rec.get("since") or now
        else:  # infer it: a reply still being written, or a prompt not yet answered
            fresh = now - (row.get("time_updated") or 0) / 1000 < OC_STALE_SECS
            if idle_at and turn:  # a turn is over once OpenCode marks it idle (2.0.30+)
                busy = fresh and idle_at < turn
            else:
                busy = bool(msg) and fresh and (msg["role"] == "user" or not msg["completed"])
            done = idle_at or (msg and msg["role"] == "assistant" and msg["completed"])
            since = ((turn if busy else done) or row.get("time_updated") or 0) / 1000
        self.info = {"cwd": row.get("directory") or "", "status": "busy" if busy else "idle",
                     "statusUpdatedAt": since * 1000, "startedAt": row.get("time_created")}

        self.wait = None  # a prompt open in it or in one of its sub-agents
        for r in [rec] + [states.get(k["id"]) for k in kids]:
            if r and r.get("state") == "waiting":
                self.wait = {"state": "waiting", "reason": r.get("reason") or "permission", "tool": r.get("tool") or "",
                             "detail": r.get("detail") or "", "since": r.get("since") or now}
                break

        # a turn that ended while no OpenCode window showed it: OpenCode marks it viewed once one does
        self.outcome = row.get("idle_outcome") if idle_at else None
        unseen = bool(idle_at) and (row.get("time_viewed") or 0) < idle_at
        self.attention = None if busy or self.wait or not unseen else \
            {"failed": "failed", "interrupted": "interrupted"}.get(self.outcome, "unseen")

        title = (row.get("title") or "").strip()
        self.title = None if not title or title.startswith("New session - ") else title
        if msg and msg["role"] == "assistant":
            self.model = oc.model_name(msg["model"])
            limit = oc.context_limit(msg["model"])
            self.ctx = min(100.0, oc_used(msg["tokens"]) / limit * 100) if limit else None
        self.activity = oc_activity(msg) if msg else None
        self.sl = {"context_window": {"used_percentage": self.ctx} if self.ctx is not None else {},
                   "cost": {"total_cost_usd": row.get("cost"), "total_lines_added": row.get("summary_additions"),
                            "total_lines_removed": row.get("summary_deletions")}}
        self.subs = {}
        for k in kids:
            r = states.get(k["id"])
            active = r and r.get("state") != "idle"
            self.subs[k["id"]] = OCSub(k.get("title") or "sub-agent", now if active else (k.get("time_updated") or 0) / 1000)


class OpenCode:
    """OpenCode's open sessions and today's usage, read from its database (read-only)
    and the ccwatch plugin's state files. Every read is guarded: if OpenCode changes
    its database, ccwatch reports it in `error` and carries on with Claude alone."""

    COLS = ("id", "parent_id", "directory", "title", "time_created", "time_updated", "time_archived",
            "cost", "summary_additions", "summary_deletions",
            "time_idle", "time_viewed", "idle_outcome")  # the last three: OpenCode 2.0.30+

    def __init__(self, path=None, state_dir=None):
        self.path = path or OC_DB
        self.state_dir = state_dir or OC_STATE_DIR
        self.db = None
        self.v2 = True           # session_v2 / session_message (2.x); False: session / message / part (1.x)
        self.cols = ""
        self.error = None
        self.rows = {}           # session id -> its row, for sessions changed recently
        self.rows_mark = 0       # newest time_updated read, ms
        self.sessions = {}       # session id -> OCSession, the open ones
        self.clients, self.client_cwds, self.clients_at = {}, {}, 0.0
        self.catalog, self.catalog_key, self.catalog_at = {}, None, 0.0
        self.reset_day(None)

    def reset_day(self, day):
        self.day = day
        self.midnight = 0.0
        self.mark = 0            # newest message time_updated read today, ms
        self.seen = {}           # message id -> time_updated last counted
        self.usage = {}          # message id -> (input, cache write, cache read, output, hour, model, project, cost)
        self.counted = set()     # prompts and tool calls already counted
        self.stamped = set()     # message timestamps already added to a project

    # database ----------------------------------------------------------------

    def connect(self):
        if self.db is None:
            if not os.path.exists(self.path):
                raise sqlite3.OperationalError(f"no database at {tilde(self.path)}")
            from urllib.parse import quote
            db = sqlite3.connect(f"file:{quote(self.path)}?mode=ro", uri=True, timeout=1)
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
            if {"session_v2", "session_message"} <= tables:
                self.v2 = True
            elif {"session", "message", "part"} <= tables:
                self.v2 = False
            else:
                db.close()
                raise sqlite3.OperationalError("unfamiliar database layout; an OpenCode update may have changed it")
            have = {r[1] for r in db.execute(f"pragma table_info({self.table})")}
            self.cols = ", ".join(c for c in self.COLS if c in have)
            self.db = db
        return self.db

    @property
    def table(self):
        return "session_v2" if self.v2 else "session"

    def close(self):
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None

    def _guard(self, fn, *args):
        try:
            fn(*args)
            self.error = None
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as e:
            self.error = f"OpenCode: {e}"
            self.close()  # reconnect next time, in case OpenCode replaced the file

    def _load_rows(self, since):
        for r in self.db.execute(f"select {self.cols} from {self.table} where time_updated >= ?", (since,)):
            d = dict(r)
            self.rows[d["id"]] = d
            self.rows_mark = max(self.rows_mark, d.get("time_updated") or 0)

    def row(self, sid):
        if sid and sid not in self.rows:
            r = self.db.execute(f"select {self.cols} from {self.table} where id = ?", (sid,)).fetchone()
            if r:
                self.rows[sid] = dict(r)
        return self.rows.get(sid)

    def _newest_in(self, directory):
        """The most recently used top-level session in a directory (the one its open terminal shows)."""
        archived = " and time_archived is null" if "time_archived" in self.cols else ""
        r = self.db.execute(f"select {self.cols} from {self.table} where directory = ? and parent_id is null"
                            f"{archived} order by time_updated desc limit 1", (directory,)).fetchone()
        if r:
            self.rows[r["id"]] = dict(r)
            return self.rows[r["id"]]
        return None

    def latest(self, sid, have=None):
        """(key, size, message, turn) for a session's newest user or assistant message.
        The message is read only when its key (id, time updated) differs from `have`;
        turn is when its newest prompt was sent (ms)."""
        db = self.db
        if self.v2:
            r = db.execute("select id, type, time_created, time_updated, length(data) from session_message "
                           "where session_id = ? and type in ('user', 'assistant') order by seq desc limit 1",
                           (sid,)).fetchone()
            if not r:
                return None, 0, None, None
            turn = db.execute("select max(time_created) from session_message where session_id = ? and type = 'user'",
                              (sid,)).fetchone()[0]
            key = (r[0], r[3])
            if key == have:
                return key, r[4], None, turn
            data = json.loads(db.execute("select data from session_message where id = ?", (r[0],)).fetchone()[0])
            return key, r[4], oc_v2_message(r[1], data, r[2]), turn
        r = db.execute("select id, time_created, time_updated, length(data) from message where session_id = ? "
                       "order by time_created desc, id desc limit 1", (sid,)).fetchone()
        if not r:
            return None, 0, None, None
        turn = db.execute("select max(time_created) from message where session_id = ? "
                          "and json_extract(data, '$.role') = 'user'", (sid,)).fetchone()[0]
        newest, total = db.execute("select max(time_updated), sum(length(data)) from part where message_id = ?",
                                   (r[0],)).fetchone()
        key, size = (r[0], max(r[2], newest or 0)), r[3] + (total or 0)
        if key == have:
            return key, size, None, turn
        data = json.loads(db.execute("select data from message where id = ?", (r[0],)).fetchone()[0])
        parts = [json.loads(p[0]) for p in db.execute("select data from part where message_id = ? order by id", (r[0],))]
        return key, size, oc_v1_message(data, parts, r[1]), turn

    # models ------------------------------------------------------------------

    def _load_catalog(self):
        """Display names and context windows of models, from the models.dev list OpenCode caches."""
        if time.time() - self.catalog_at < 600:
            return
        self.catalog_at = time.time()
        try:
            r = self.db.execute("select time_updated from kv where key = 'models-dev:catalog'").fetchone()
        except sqlite3.Error:  # 1.x keeps it elsewhere
            return
        if not r or r[0] == self.catalog_key:
            return
        value = json.loads(self.db.execute("select value from kv where key = 'models-dev:catalog'").fetchone()[0])
        body = value.get("body", value) if isinstance(value, dict) else {}
        body = json.loads(body) if isinstance(body, str) else body
        catalog = {}
        for prov, p in (body or {}).items():
            for mid, m in ((p or {}).get("models") or {}).items() if isinstance(p, dict) else ():
                if isinstance(m, dict):
                    catalog[(prov, mid)] = (m.get("name") or mid, (m.get("limit") or {}).get("context"))
        self.catalog, self.catalog_key = catalog, r[0]

    def model_name(self, model):
        return (self.catalog.get(tuple(model)) or (None,))[0] or model[1]

    def context_limit(self, model):
        return (self.catalog.get(tuple(model)) or (None, None))[1]

    # live sessions -------------------------------------------------------------

    def states(self):
        """The plugin's newest record per session, from OpenCode processes still running."""
        out = {}
        for path in glob.glob(os.path.join(self.state_dir, "*.json")):
            rec = load_json(path)
            if isinstance(rec, dict) and rec.get("state") and isinstance(rec.get("pid"), int) and pid_alive(rec["pid"]):
                out[os.path.basename(path)[:-len(".json")]] = rec
        return out

    def refresh(self):
        self._guard(self._refresh)

    def _refresh(self):
        now = time.time()
        self.connect()
        self._load_catalog()
        if self.rows_mark:
            self._load_rows(self.rows_mark)
        else:
            midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
            self._load_rows(int(min(midnight, now - OC_OPEN_SECS) * 1000))
        if now - self.clients_at >= OC_CLIENT_INTERVAL:
            self.clients, self.clients_at = opencode_clients(self.client_cwds), now
        states = self.states()

        # open: changed in the last OC_OPEN_SECS, working or waiting, or shown in an open terminal
        want = {sid: r for sid, r in self.rows.items() if not r.get("parent_id") and not r.get("time_archived")
                and (r.get("time_updated") or 0) >= (now - OC_OPEN_SECS) * 1000}
        for sid, rec in states.items():
            r = self.row(sid) if rec.get("state") != "idle" else None
            root = self.row(r.get("parent_id")) if r and r.get("parent_id") else r  # a sub-agent shows on its parent
            if root and not root.get("parent_id"):
                want[root["id"]] = root
        for directory in self.clients:
            if not any(r.get("directory") == directory for r in want.values()):
                r = self._newest_in(directory)
                if r:
                    want[r["id"]] = r

        for sid, r in want.items():
            s = self.sessions.get(sid) or OCSession(sid)
            kids = [k for k in self.rows.values() if k.get("parent_id") == sid]
            s.update(self, r, states, kids, self.clients.get(r.get("directory")))
            self.sessions[sid] = s
        for sid in list(self.sessions):
            if sid not in want:
                del self.sessions[sid]

    # today's usage -------------------------------------------------------------

    def scan(self, ledger, midnight):
        """Add today's OpenCode work to the ledger's projects: time, prompts, edits, commits, tokens."""
        self._guard(self._scan, ledger, midnight)

    def _scan(self, ledger, midnight):
        if self.day != ledger.day:
            self.reset_day(ledger.day)
        self.midnight = midnight
        self.connect()
        self._load_catalog()
        top = self.mark
        for mid, sid, updated, load in self._changed(int(midnight * 1000)):
            top = max(top, updated)
            if self.seen.get(mid) != updated:
                self.seen[mid] = updated
                self._count(ledger, mid, sid, load())
        self.mark = top

    def _changed(self, since):
        """(message id, session id, time updated, loader) for today's messages changed since the last scan."""
        db = self.db
        if self.v2:
            q = ("select id, session_id, type, time_created, time_updated, data from session_message "
                 "where time_created >= ? and time_updated >= ? and type in ('user', 'assistant')")
            for r in db.execute(q, (since, self.mark)).fetchall():
                yield r[0], r[1], r[4], lambda r=r: oc_v2_message(r[2], json.loads(r[5]), r[3])
            return
        ids = {r[0] for r in db.execute("select id from message where time_created >= ? and time_updated >= ?",
                                        (since, self.mark))}
        ids |= {r[0] for r in db.execute("select distinct message_id from part where time_created >= ? "
                                         "and time_updated >= ?", (since, self.mark))}
        ids = sorted(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            parts = {}
            for m, updated, data in db.execute(f"select message_id, time_updated, data from part "
                                               f"where message_id in ({marks}) order by id", chunk):
                parts.setdefault(m, []).append((updated, data))
            for r in db.execute(f"select id, session_id, time_created, time_updated, data from message "
                                f"where id in ({marks})", chunk).fetchall():
                ps = parts.get(r[0], [])
                updated = max([r[3]] + [u for u, _ in ps])
                yield r[0], r[1], updated, lambda r=r, ps=ps: oc_v1_message(
                    json.loads(r[4]), [json.loads(d) for _, d in ps], r[2])

    def _count(self, ledger, mid, sid, msg):
        row = self.row(sid) or {}
        key = (row.get("directory") or "").rstrip("/")
        if not key or msg["created"] / 1000 < self.midnight:
            return
        p = ledger.projects.get(key)
        if p is None:
            p = ledger.projects[key] = Project(key)
        p.sessions.add("oc:" + (row.get("parent_id") or sid))
        p.sources.add("opencode")
        ledger.dirty = True
        for tag, ms in (("sent", msg["created"]), ("done", msg["completed"])):
            if ms and (mid, tag) not in self.stamped:
                self.stamped.add((mid, tag))
                p.stamps.append(ms / 1000)
        if msg["role"] == "user":  # a prompt you typed; a sub-agent's task prompt doesn't count
            if not row.get("parent_id") and msg["text"].strip() and (mid, "prompt") not in self.counted:
                self.counted.add((mid, "prompt"))
                p.prompts += 1
            return
        if msg["role"] != "assistant":
            return
        tok = msg["tokens"]
        cache = tok.get("cache") or {}
        self.usage[mid] = (int(tok.get("input") or 0), int(cache.get("write") or 0), int(cache.get("read") or 0),
                           int(tok.get("output") or 0) + int(tok.get("reasoning") or 0),
                           datetime.fromtimestamp(msg["created"] / 1000).hour, self.model_name(msg["model"]), key,
                           float(msg["cost"] or 0))
        for i, item in enumerate(msg["content"]):
            st = item.get("state") or {}
            if item.get("type") == "tool" and st.get("status") == "completed" and (mid, item.get("id") or i) not in self.counted:
                self.counted.add((mid, item.get("id") or i))
                self._tool(p, item.get("name") or "", st)

    @staticmethod
    def _tool(p, name, st):
        """Files, lines and commits from one finished tool call."""
        inp, meta = st.get("input") or {}, st.get("metadata") or {}
        if name in ("bash", "shell"):  # a commit counts once its command succeeded
            cmd = inp.get("command")
            if isinstance(cmd, str) and GIT_COMMIT.search(cmd) and meta.get("exit") in (0, None):
                p.commits += 1
            return
        diffs = [meta["filediff"]] if isinstance(meta.get("filediff"), dict) else \
            [d for d in meta.get("files") or () if isinstance(d, dict)]
        for d in diffs:
            path = d.get("file") or d.get("filePath") or d.get("path")
            if path:
                p.files.add(os.path.normpath(os.path.join(p.cwd, path)))  # 2.x tools may give it relative
                p.added += int(d.get("additions") or 0)
                p.removed += int(d.get("deletions") or 0)
        if not diffs and name == "write" and isinstance(inp.get("content"), str):
            path = inp.get("filePath") or inp.get("path")
            if path:
                p.files.add(os.path.normpath(os.path.join(p.cwd, path)))
                p.added += len(inp["content"].splitlines())

    def project_output(self):
        out = {}
        for *_, o, _hour, _model, key, _cost in self.usage.values():
            out[key] = out.get(key, 0) + o
        return out

    def project_cost(self):
        out = {}
        for *_, key, cost in self.usage.values():
            out[key] = out.get(key, 0.0) + cost
        return out

    def totals(self):
        """Today's OpenCode tokens, cost and replies, shaped like TokenLedger.totals()."""
        inp = cw = cr = out = 0
        cost, hourly, models = 0.0, [0] * 24, {}
        for a, b, c, d, hour, model, _key, usd in self.usage.values():
            inp += a; cw += b; cr += c; out += d; cost += usd
            hourly[hour] += d
            if model:
                models[model] = models.get(model, 0) + d
        return {"input": inp, "cache_write": cw, "cache_read": cr, "output": out, "cost": cost,
                "total": inp + cw + cr + out, "messages": len(self.usage), "hourly": hourly, "models": models}


def oc_plugin_installed():
    return MARKER_JS in (read_text(OC_PLUGIN) or "")


class Store:
    def __init__(self, opencode=False):
        self.sessions = {}
        self.oc = None
        self.ledger = TokenLedger()
        self.limits = None       # rate_limits from the newest status line capture
        self.limits_at = 0.0
        self.cost_today = 0.0    # summed session cost of every capture written today
        self.cost_sessions = 0
        self.project_cost = {}   # cwd -> summed session cost today
        self.last_prune = 0.0
        self.set_opencode(opencode)

    def set_opencode(self, on):
        """Start or stop showing OpenCode; today's projects are rescanned to add or drop its work."""
        if bool(on) == bool(self.oc):
            return
        if self.oc:
            self.oc.close()
        self.oc = OpenCode() if on else None
        self.ledger = TokenLedger(self.oc)

    def all_sessions(self):
        return list(self.sessions.values()) + (list(self.oc.sessions.values()) if self.oc else [])

    def prune(self):
        """Delete ccwatch's own stale cache files: old status line captures, and
        "needs you" flags left behind by sessions that crashed (or OpenCode plugin
        records of sessions long finished). The terminal dashboard and the widget's
        feed may both do this at once, so a file that has already gone is fine."""
        now = time.time()
        live = {s.sid for s in self.all_sessions()}
        for pattern, max_age in ((os.path.join(STATUSLINE_DIR, "*.json"), CAPTURE_MAX_AGE),
                                 (os.path.join(STATE_DIR, "*.json"), STATE_MAX_AGE),
                                 (os.path.join(OC_STATE_DIR, "*.json"), STATE_MAX_AGE)):
            for path in glob.glob(pattern):
                if os.path.basename(path)[:-len(".json")] in live:
                    continue
                try:
                    if now - os.path.getmtime(path) > max_age:
                        os.remove(path)
                except OSError:
                    pass
        self.last_prune = now

    def refresh(self):
        seen = set()
        for path in glob.glob(os.path.join(SESS_DIR, "*.json")):
            try:
                with open(path) as f:
                    info = json.load(f)
                pid = int(info.get("pid") or os.path.basename(path).split(".")[0])
            except (OSError, ValueError):
                continue
            if not pid_alive(pid):
                continue
            seen.add(pid)
            s = self.sessions.get(pid) or Session(pid)
            s.update(info)
            self.sessions[pid] = s
        for pid in list(self.sessions):
            if pid not in seen:
                del self.sessions[pid]
        if self.oc:
            self.oc.refresh()

        # plan limits are account-wide: take them from whichever session reported last
        captures = []
        for path in glob.glob(os.path.join(STATUSLINE_DIR, "*.json")):
            try:
                captures.append((os.path.getmtime(path), path))
            except OSError:
                pass
        captures.sort(reverse=True)
        for mtime, path in captures[:5]:
            d = load_json(path)
            if d and d.get("rate_limits"):
                self.limits, self.limits_at = d["rate_limits"], mtime
                break

        # session costs are cumulative, so a session that began before midnight counts in full
        midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        cost, n, by_proj = 0.0, 0, {}
        for mtime, path in captures:
            if mtime < midnight:
                break
            d = load_json(path) or {}
            c = (d.get("cost") or {}).get("total_cost_usd")
            if c:
                cost += c
                n += 1
                key = self.ledger.sess_proj.get(d.get("session_id")) or d.get("cwd")
                by_proj[key] = by_proj.get(key, 0.0) + c
        for key, c in (self.oc.project_cost() if self.oc else {}).items():
            if c:
                by_proj[key] = by_proj.get(key, 0.0) + c
        self.cost_today, self.cost_sessions, self.project_cost = cost, n, by_proj

        if time.time() - self.ledger.last_scan >= LEDGER_INTERVAL:
            self.ledger.refresh()
        if time.time() - self.last_prune >= PRUNE_INTERVAL:
            self.prune()

    def ordered(self):
        return sorted(self.all_sessions(),
                      key=lambda s: (not s.waiting, not s.busy, not s.attention, s.idle, -(s.info.get("startedAt") or 0)))

    def projects_today(self):
        """Today's projects by time spent, each with the liveliest state of its open sessions."""
        rank = {"wait": 3, "busy": 2, "idle": 1, "other": 1}
        live = {}
        for s in self.all_sessions():
            key = self.ledger.sess_proj.get(s.sid) or (s.info.get("cwd") or "").rstrip("/")
            st = state_of(s)
            if rank[st] > rank.get(live.get(key), 0):
                live[key] = st
        projects = sorted(self.ledger.projects.values(), key=lambda p: (-p.active, -p.last))
        return [(p, live.get(p.cwd), self.project_cost.get(p.cwd)) for p in projects]


# ─── formatting ──────────────────────────────────────────────────────────────

def fmt_dur(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def fmt_hm(secs):
    """Coarse duration for totals: 45s → <1m, 48m, 2h05m."""
    mins = int(max(0, secs)) // 60
    if mins < 1:
        return "<1m"
    return f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60:02d}m"


def fmt_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def fmt_reset(epoch):
    secs = epoch - time.time()
    if secs <= 0:
        return "now"
    if secs < 86400:
        return f"in {fmt_dur(secs)}"
    return datetime.fromtimestamp(epoch).strftime("%a %H:%M")


def bar(frac, width):
    """Bar with eighth-block resolution."""
    frac = min(1.0, max(0.0, frac))
    cells = frac * width
    full = int(cells)
    part = "▏▎▍▌▋▊▉"[int((cells - full) * 7) - 1] if cells - full >= 1 / 7 and full < width else ""
    return "█" * full + part, width - full - len(part)


def sparkline(values):
    vals = list(values)
    top = max(vals) or 1
    return "".join(SPARK[0 if v == 0 else max(1, round(v / top * (len(SPARK) - 1)))] for v in vals)


def clip(text, width):
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


# ─── sprites ─────────────────────────────────────────────────────────────────
# Each sprite is 6 rows x 11 cols. Row 0 holds the antenna / floating z's.

def sprite(state, f):
    if state == "busy":
        blink = (f // 2) % 20 == 0
        eyes = "─ ─" if blink else ("◉ ◉" if (f // 12) % 4 else "◔ ◔")
        arms = "  ╱ ███ ─  " if (f // 2) % 2 else "  ─ ███ ╲  "
        kb = ("▀▄" * 8)[(f // 2) % 2:][:9]
        return [
            "     ┬     ",
            "  ╭─────╮  ",
            f"  │ {eyes} │  ",
            "  ╰──┬──╯  ",
            arms,
            f" {kb} ",
        ]
    if state == "idle":
        rows = [
            "           ",
            "  ╭─────╮  ",
            "  │ ─ ─ │  ",
            "  ╰──┬──╯  ",
            "  │ ███ │  ",
            "  ▔▔▔▔▔▔▔  ",
        ]
        step = (f // 5) % 4
        if step < 3:  # a 'z' drifting up and right
            r, c = 2 - step, 9 + min(step, 1)
            ch = "zZz"[step]
            rows[r] = rows[r][:c] + ch + rows[r][c + 1:]
        return rows
    if state == "wait":  # waving for attention
        up = (f // 3) % 2
        rows = [
            "     ?     " if (f // 6) % 2 else "     !     ",
            "  ╭─────╮ o" if up else "  ╭─────╮  ",
            "  │ ◉ ◉ │╱ " if up else "  │ ◉ ◉ │ o",
            "  ╰──┬──╯  " if up else "  ╰──┬──╯╱ ",
            "  ╱ ███    ",
            "  ▔▔▔▔▔▔▔  ",
        ]
        return rows
    # unknown status
    eyes = "◉ ◉" if (f // 4) % 2 else "◎ ◎"
    return [
        "     !     " if (f // 3) % 2 else "           ",
        "  ╭─────╮  ",
        f"  │ {eyes} │  ",
        "  ╰──┬──╯  ",
        "  ╲ ███ ╱  ",
        "  ▔▔▔▔▔▔▔  ",
    ]


EMPTY_BOT = [
    "  ╭─────╮   z",
    "  │ ─ ─ │  Z ",
    "  ╰──┬──╯ z  ",
    "  │ ███ │    ",
]


# ─── rendering ───────────────────────────────────────────────────────────────

class Screen:
    def __init__(self, scr):
        self.scr = scr
        self.h, self.w = scr.getmaxyx()

    def put(self, y, x, text, attr=0):
        if y < 0 or y >= self.h or x >= self.w or x < 0:
            return
        text = text[: self.w - x - (1 if y == self.h - 1 else 0)]
        if not text:
            return
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass


def state_of(s):
    if s.waiting:
        return "wait"
    return "busy" if s.busy else ("idle" if s.idle else "other")


def state_color(s):
    return curses.color_pair({"busy": C_BUSY, "idle": C_IDLE, "other": C_WARN, "wait": C_WARN}[state_of(s)])


def card_height(s):
    return 9 + min(3, len(s.active_subs()))


ATTENTION = {  # an OpenCode turn that ended unseen: (badge, color, verb)
    "unseen": (" ✓ DONE · UNSEEN ", C_BUSY, "done"),
    "failed": (" ✗ FAILED ", C_DANGER, "failed"),
    "interrupted": (" ■ INTERRUPTED ", C_WARN, "interrupted"),
}


def draw_card(sc, s, y, x, w, h, f):
    st = state_of(s)
    att = ATTENTION.get(s.attention) if st == "idle" else None
    col = curses.color_pair(att[1]) if att else state_color(s)
    border = col | (curses.A_BOLD if st == "busy" else curses.A_DIM if st == "idle" and not att else 0)
    if st == "wait" and (f // 6) % 2:  # pulse
        border = col | curses.A_BOLD
    dim = curses.color_pair(C_DIM) | curses.A_DIM

    # frame — busy cards get a light that travels along the top edge
    top = list("╭" + "─" * (w - 2) + "╮")
    sc.put(y, x, "".join(top), border)
    if st == "busy":
        pos = 1 + (f % (w - 2))
        sc.put(y, x + pos, "━", col | curses.A_BOLD)
    for r in range(1, h - 1):
        sc.put(y + r, x, "│", border)
        sc.put(y + r, x + w - 1, "│", border)
    sc.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", border)
    oc = s.source == "opencode"
    name = clip(s.name, w - (33 if oc else 22))
    sc.put(y, x + 2, f" {name} ", curses.A_BOLD)
    if oc:
        sc.put(y, x + 4 + len(name), "OpenCode ", curses.color_pair(C_SUB) | curses.A_BOLD)

    badge = {"busy": f" {SPIN[f % len(SPIN)]} WORKING ", "idle": att[0] if att else " ○ IDLE ",
             "wait": " ▲ NEEDS YOU " if (f // 6) % 2 else " △ NEEDS YOU "}.get(st, f" ! {s.status.upper()} ")
    sc.put(y, x + w - len(badge) - 2, badge, col | curses.A_BOLD | (curses.A_REVERSE if st != "idle" or att else 0))

    # robot
    rows = sprite(st, f)
    for i, row in enumerate(rows):
        sc.put(y + 1 + i, x + 1, row, col | (curses.A_DIM if st == "idle" else 0))
    if st == "busy":  # antenna light
        light = "●" if (f // 4) % 2 else "○"
        sc.put(y + 1, x + 6, light, curses.color_pair(C_ACCENT) | curses.A_BOLD)

    # info column
    ix, iw = x + 13, w - 15
    line = y + 1
    sc.put(line, ix, clip(s.title or "(untitled session)", iw), curses.A_BOLD)
    line += 1
    sc.put(line, ix, clip(f"▸ {s.project}", iw), curses.color_pair(C_ACCENT))
    line += 1

    act = s.activity
    if st == "wait":
        wt = s.wait  # not `w`: that's the card's width, still needed for the sub-agent lines
        label = {"permission": "approve", "question": "answer", "input": "input"}.get(wt.get("reason"), "respond")
        tool = f" ⚒ {wt['tool']}" if wt.get("tool") else ""
        sc.put(line, ix, clip(f"{label}{tool}", iw), col | curses.A_BOLD)
        if wt.get("detail"):
            sc.put(line + 1, ix + 5, clip(wt["detail"], iw - 5), 0)
    elif act:
        kind, label, detail = act
        prefix = "now " if st == "busy" else "last"
        if kind == "tool":
            text = f"{prefix} ⚒ {label}"
        else:
            text = f"{prefix} {label.lower()}"
            if st == "busy" and kind == "think":
                text += "." * (1 + (f // 4) % 3)
        sc.put(line, ix, clip(text, iw), (col | curses.A_BOLD) if st == "busy" else dim)
        if detail:
            sc.put(line + 1, ix + 5, clip(detail, iw - 5), 0 if st == "busy" else dim)
    line += 2

    if st == "wait":
        meta = f"waiting {fmt_dur(time.time() - (s.wait.get('since') or time.time()))}"
    else:
        verb = "working" if st == "busy" else (att[2] if att else "idle") if st == "idle" else s.status
        meta = f"{verb} {fmt_dur(s.state_secs())}"
    meta += f" · up {fmt_dur(s.uptime_secs())}"
    if s.model:
        meta += f" · {pretty_model(s.model)}"
    sc.put(line, ix, clip(meta, iw), dim)
    line += 1

    spark = sparkline(s.spark)
    sc.put(line, ix, "io ", dim)
    sc.put(line, ix + 3, clip(spark, iw - 3), curses.color_pair(C_ACCENT if st == "busy" else C_DIM))
    total = len(s.subs)
    if total:
        tag = f" ◆ {len(s.active_subs())}/{total} agents"
        sc.put(line, ix + 3 + min(len(spark), iw - 3), clip(tag, iw - 3 - len(spark)), curses.color_pair(C_SUB))
    line += 1

    ctx = (s.sl.get("context_window") or {}).get("used_percentage")
    cost = s.sl.get("cost") or {}
    if ctx is not None:
        filled, empty = bar(ctx / 100, 10)
        sc.put(line, ix, "ctx", dim)
        sc.put(line, ix + 4, filled, curses.color_pair(level_color(ctx)))
        sc.put(line, ix + 4 + len(filled), "░" * empty, dim)
        extra = f" {ctx:>3.0f}%"
        if cost.get("total_cost_usd") is not None:
            extra += f" · ${cost['total_cost_usd']:.2f}"
        if cost.get("total_lines_added") is not None:
            extra += f" · +{cost['total_lines_added']}/-{cost.get('total_lines_removed', 0)}"
        sc.put(line, ix + 14, clip(extra, iw - 14), dim)
    elif oc:  # a model OpenCode's catalog doesn't size, e.g. a local one
        extra = "ctx  ?" + (f" · ${cost['total_cost_usd']:.2f}" if cost.get("total_cost_usd") is not None else "")
        if cost.get("total_lines_added") is not None:
            extra += f" · +{cost['total_lines_added']}/-{cost.get('total_lines_removed') or 0}"
        sc.put(line, ix, clip(extra, iw), dim)
    else:
        sc.put(line, ix, "ctx  (waiting for status line)", dim)

    # sub-agents
    for i, sa in enumerate(s.active_subs()[:3]):
        r = y + 8 + i
        spin = SPIN[(f + i * 3) % len(SPIN)]
        sc.put(r, x + 3, f"└ {spin} ", curses.color_pair(C_SUB) | curses.A_BOLD)
        sc.put(r, x + 7, clip(f"{sa.desc}", w - 10), curses.color_pair(C_SUB))


def draw_header(sc, sessions, f, show_idle, compact=False, opencode=None):
    title = " CLAUDE COMMAND CENTER "
    busy = sum(s.busy and not s.waiting for s in sessions)
    subs = sum(len(s.active_subs()) for s in sessions)
    # shimmer: a highlight that sweeps across the title
    pos = (f // 1) % (len(title) + 20)
    for i, ch in enumerate(title):
        attr = curses.color_pair(C_ACCENT) | curses.A_BOLD
        if abs(i - pos) <= 1:
            attr |= curses.A_REVERSE
        sc.put(0, 1 + i, ch, attr)
    n = len(sessions)
    stats = f"{n} session{'s' * (n != 1)} · {busy} working · {subs} sub-agent{'s' * (subs != 1)}"
    stats = clip(stats, sc.w - len(title) - 3 - 12)  # leave room for the clock
    sc.put(0, len(title) + 3, stats, curses.color_pair(C_DIM))
    waiting = sum(s.waiting for s in sessions)
    if waiting:
        alert = f" ▲ {waiting} need{'s' * (waiting == 1)} you "
        sc.put(0, len(title) + 3 + len(stats) + 2, alert,
               curses.color_pair(C_WARN) | curses.A_BOLD | (curses.A_REVERSE if (f // 6) % 2 else 0))
    clock = time.strftime("%H:%M:%S")
    sc.put(0, sc.w - len(clock) - 2, clock, curses.color_pair(C_DIM))
    hint = f"q quit · i {'hide' if show_idle else 'show'} idle · u {'expand' if compact else 'compact'} usage"
    if opencode is not None:  # only offered when OpenCode is installed
        hint += f" · o {'hide' if opencode else 'show'} OpenCode"
    sc.put(sc.h - 1, 1, hint, curses.color_pair(C_DIM) | curses.A_DIM)


def level_color(pct):
    return C_BUSY if pct < 50 else C_WARN if pct < 80 else C_DANGER


class Easer:
    """Animates displayed bar values toward their targets."""

    def __init__(self):
        self.vals = {}

    def __call__(self, key, target):
        cur = self.vals.get(key, 0.0)
        cur += (target - cur) * 0.15
        if abs(target - cur) < 0.05:
            cur = target
        self.vals[key] = cur
        return cur


EASE = Easer()


NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen".split())}
UNIT_SECS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}


def limit_window(key):
    """(label, seconds) for a rate_limits key, read from its name so a changed window
    needs no code change: five_hour -> ("5-hour", 18000), four_hour -> ("4-hour", 14400),
    seven_day -> ("weekly", 604800), seven_day_opus -> ("weekly opus", 604800).
    Unknown shapes get a plain label and no length (so no pace or elapsed marker)."""
    m = re.match(r"^([a-z]+|\d+)_(minute|hour|day|week)s?(?:_(\w+))?$", key)
    n = m and (int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS.get(m.group(1)))
    if not n:
        return key.replace("_", " "), None
    unit, extra = m.group(2), m.group(3)
    secs = n * UNIT_SECS[unit]
    label = "weekly" if secs == 7 * 86400 else "daily" if secs == 86400 else f"{n}-{unit}"
    return label + (" " + extra.replace("_", " ") if extra else ""), secs


def limit_windows(limits):
    """[(key, label, seconds)] for every window in a rate_limits blob, shortest first."""
    out = [(k, *limit_window(k)) for k, v in (limits or {}).items() if isinstance(v, dict)]
    return sorted(out, key=lambda w: (w[2] is None, w[2] or 0, w[0]))


def pace(lim, window):
    """Where usage is heading by the reset, extrapolating the rate so far.

    Returns (elapsed_fraction, text, color) or None when the window is unknown.
    """
    used, resets = lim.get("used_percentage"), lim.get("resets_at")
    if used is None or not resets or not window:
        return None
    elapsed = time.time() - (resets - window)
    frac = min(1.0, max(0.0, elapsed / window))
    if used >= 100:
        return frac, "▲ limit reached", C_DANGER
    if used <= 0:
        return frac, "no usage yet this window", C_DIM
    if frac < 0.03:
        return frac, "window just started", C_DIM
    projected = used / frac
    if projected >= 100:
        secs = (100 - used) / (used / elapsed)
        if time.time() + secs < resets:
            return frac, f"▲ limit in ~{fmt_dur(secs)} at this pace", C_DANGER
    color = C_BUSY if projected < 70 else C_WARN if projected < 100 else C_DANGER
    return frac, f"on pace for ~{min(projected, 999):.0f}% by reset", color


def sparkbar(values):
    top = max(values) or 1
    return "".join(SPARK[0 if v <= 0 else max(1, round(v / top * (len(SPARK) - 1)))] for v in values)


def usage_rows(store, f, width, compact):
    """Logical rows of the usage panel; each row is (label, [groups]), each group [(text, attr)]."""
    dim = curses.color_pair(C_DIM) | curses.A_DIM
    accent = curses.color_pair(C_ACCENT) | curses.A_BOLD
    rows = []

    limits = store.limits or {}
    bw = max(10, min(36, width - (64 if not compact else 40)))
    for key, label, window in limit_windows(limits):
        lim = limits.get(key) or {}
        pct = lim.get("used_percentage")
        if pct is None:
            continue
        shown = EASE(key, float(pct))
        filled, empty = bar(shown / 100, bw)
        col = curses.color_pair(level_color(pct))
        p = pace(lim, window)
        # the bar carries a marker showing how far through the window we are
        cells = [(filled, col | curses.A_BOLD), ("░" * empty, dim)]
        if p:
            mark = min(bw - 1, int(p[0] * bw))
            text = filled + "░" * empty
            cells = []
            for i, ch in enumerate(text):
                if i == mark:
                    cells.append(("┃", accent if (f // 8) % 4 else curses.color_pair(C_ACCENT)))
                elif i < len(filled):
                    cells.append((ch, col | curses.A_BOLD))
                else:
                    cells.append((ch, dim))
        groups = [cells + [(f" {shown:3.0f}%", col | curses.A_BOLD)]]
        if lim.get("resets_at"):
            groups.append([("resets ", dim), (fmt_reset(lim["resets_at"]), 0)])
        if p and not compact:
            groups.append([(p[1], curses.color_pair(p[2]) | (curses.A_BOLD if p[2] == C_DANGER else 0))])
        rows.append((label, groups))
    if not limits:
        rows.append(("limits", [[("waiting for a status line update from any session", dim)]]))
    elif time.time() - store.limits_at > 300:
        rows.append(("", [[(f"limits as of {fmt_dur(time.time() - store.limits_at)} ago", curses.color_pair(C_WARN))]]))

    t = store.ledger.totals()
    work = t["input"] + t["cache_write"] + t["output"]  # tokens actually processed, minus cache reads
    hit = t["cache_read"] / (t["input"] + t["cache_write"] + t["cache_read"] or 1)
    now_h = datetime.now().hour
    spark = sparkbar(t["hourly"][: now_h + 1])
    today = [[("│", dim),
              (spark[:-1], curses.color_pair(C_ACCENT)),
              (spark[-1:], accent | (curses.A_REVERSE if (f // 6) % 2 else 0)),
              ("·" * (23 - now_h), dim),
              ("│", dim)]] if not compact else []
    today += [[(fmt_tokens(t["output"]), accent), (" out", dim)],
              [(fmt_tokens(work), curses.A_BOLD), (" processed", dim)]]
    if not compact:
        today += [[(f"+{fmt_tokens(t['cache_read'])}", 0), (f" cache reads ({hit:.0%} hit)", dim)],
                  [(str(t["messages"]), curses.A_BOLD), (" replies", dim)]]
    rows.append(("today", today))

    if not compact:
        models = sorted(t["models"].items(), key=lambda kv: -kv[1])
        total_out = sum(v for _, v in models) or 1
        mg = []
        for i, (m, v) in enumerate(models[:4]):
            share = v / total_out
            mbar, _ = bar(share, 8)
            mg.append([(pretty_model(m) + " ", curses.A_BOLD),
                       (mbar.ljust(8, "·"), curses.color_pair((C_SUB, C_ACCENT, C_BUSY, C_WARN)[i])),
                       (f" {share:.0%}", dim)])
        if mg:
            rows.append(("models", mg))

    open_cost = sum((s.sl.get("cost") or {}).get("total_cost_usd") or 0 for s in store.sessions.values())
    cost = [[("≈$", dim), (f"{store.cost_today:.2f}", accent), (" today", dim)]]
    if store.cost_sessions:
        cost[0].append((f" ({store.cost_sessions} session{'s' * (store.cost_sessions != 1)})", dim))
    cost.append([("≈$", dim), (f"{open_cost:.2f}", curses.A_BOLD), (" in open sessions", dim)])
    if not compact:
        cost.append([("api-equivalent; subscriptions aren't billed per token", dim)])
    rows.append(("cost", cost))
    if store.oc:
        rows.append(("opencode", opencode_groups(store, compact)))
    return rows


def opencode_groups(store, compact):
    """The usage panel's OpenCode row: today's tokens, cost, replies and models."""
    dim = curses.color_pair(C_DIM) | curses.A_DIM
    if store.oc.error:
        return [[(clip(store.oc.error, 70), curses.color_pair(C_WARN))]]
    t = store.oc.totals()
    if not t["messages"]:
        return [[("no OpenCode activity yet today", dim)]]
    groups = [[(fmt_tokens(t["output"]), curses.color_pair(C_SUB) | curses.A_BOLD), (" out", dim)],
              [("≈$", dim), (f"{t['cost']:.2f}", curses.A_BOLD), (" today", dim)]]
    if not compact:
        groups.insert(1, [(fmt_tokens(t["input"] + t["cache_write"] + t["output"]), curses.A_BOLD), (" processed", dim)])
        groups.append([(str(t["messages"]), curses.A_BOLD), (" replies", dim)])
        total = sum(t["models"].values()) or 1
        for m, v in sorted(t["models"].items(), key=lambda kv: -kv[1])[:2]:
            groups.append([(pretty_model(m) + " ", curses.A_BOLD), (f"{v / total:.0%}", dim)])
    return groups


def label_width(store):
    return 10 if store.oc else 8  # room for "opencode"


def layout_rows(rows, width, label_w):
    """Flow each row's groups into physical lines no wider than width."""
    lines = []
    for label, groups in rows:
        line, x = [(label.ljust(label_w), curses.A_BOLD)], label_w
        for g in groups:
            gw = sum(len(t) for t, _ in g)
            if x > label_w and x + 3 + gw > width:
                lines.append(line)
                line, x = [(" " * label_w, 0)], label_w
            if x > label_w:
                line.append(("   ", 0))
                x += 3
            line.extend(g)
            x += gw
        lines.append(line)
    return lines


def project_glyph(state, f):
    if state == "wait":
        return ("▲" if (f // 6) % 2 else "△"), curses.color_pair(C_WARN) | curses.A_BOLD
    if state == "busy":
        return SPIN[f % len(SPIN)], curses.color_pair(C_BUSY) | curses.A_BOLD
    if state:
        return "●", curses.color_pair(C_IDLE)
    return "·", curses.color_pair(C_DIM) | curses.A_DIM


def projects_row(store, f):
    """One-line projects summary for the compact usage panel."""
    dim = curses.color_pair(C_DIM) | curses.A_DIM
    projects = store.projects_today()
    groups = [[(fmt_hm(store.ledger.active), curses.color_pair(C_ACCENT) | curses.A_BOLD), (" active", dim)]]
    for p, state, _ in projects[:4]:
        glyph, gattr = project_glyph(state, f)
        groups.append([(glyph + " ", gattr), (p.name, curses.A_BOLD), (" " + fmt_hm(p.active), dim)])
    if len(projects) > 4:
        groups.append([(f"+{len(projects) - 4} more", dim)])
    return ("worked", groups)


# (key, header, width) in display order; PROJECT_DROP is the order columns go when space runs out
PROJECT_COLS = (("time", "time", 5), ("hours", "", 24), ("prompts", "prompts", 7), ("lines", "lines", 9),
                ("files", "files", 5), ("commits", "commits", 7), ("cost", "cost", 7), ("out", "out", 6),
                ("branch", "branch", 14))
PROJECT_DROP = ("branch", "out", "files", "cost", "commits", "lines", "prompts", "hours")
NAME_MIN, NAME_MAX = 14, 22


def project_panel(store, f, width, max_rows):
    """Lines of the projects panel, plus the texts for its top-right and bottom-right border."""
    dim = curses.color_pair(C_DIM) | curses.A_DIM
    accent = curses.color_pair(C_ACCENT)
    L = store.ledger
    projects = store.projects_today()
    if not projects:
        return [[("no Claude activity yet today", dim)]], "", ""

    cols = list(PROJECT_COLS)
    for key in PROJECT_DROP:
        if 2 + NAME_MIN + sum(2 + w for _, _, w in cols) <= width:
            break
        cols = [c for c in cols if c[0] != key]
    longest = max(len(p.name) for p, _, _ in projects)
    name_w = max(NAME_MIN, min(NAME_MAX, longest, width - 2 - sum(2 + w for _, _, w in cols)))
    now_h = datetime.now().hour

    def num(n, w, attr=curses.A_BOLD):
        return [(f"{n:>{w}}", attr)] if n else [("·".rjust(w), dim)]

    def cells(p, state, cost):
        out = {"time": [(fmt_hm(p.active).rjust(5), curses.A_BOLD)],
               "prompts": num(p.prompts, 7), "files": num(len(p.files), 5), "commits": num(p.commits, 7),
               "out": num(fmt_tokens(p.output) if p.output else 0, 6, 0),
               "cost": num(f"${cost:.2f}" if cost else 0, 7, 0),
               "branch": [(clip(p.branch, 14).ljust(14), dim)]}
        if p.added or p.removed:
            add, rem = f"+{fmt_tokens(p.added)}", f"-{fmt_tokens(p.removed)}"
            out["lines"] = [(" " * max(0, 9 - len(add) - 1 - len(rem)), 0), (add, curses.color_pair(C_BUSY)),
                            (" ", 0), (rem, curses.color_pair(C_DANGER))]
        else:
            out["lines"] = num(0, 9)
        hours = []
        for h in range(24):
            v = p.hourly[h]
            if h > now_h:
                hours.append(("·", dim))
            elif v <= 0:
                hours.append((SPARK[0], dim))
            else:
                attr = accent | curses.A_BOLD
                if h == now_h and state in ("busy", "wait") and (f // 6) % 2:
                    attr |= curses.A_REVERSE
                hours.append((SPARK[max(1, min(len(SPARK) - 1, round(v / 3600 * (len(SPARK) - 1))))], attr))
        out["hours"] = hours
        return out

    axis = list(" " * 24)
    for h in (0, 6, 12, 18):
        axis[h:h + len(str(h))] = str(h)
    header = [("  " + "project".ljust(name_w), dim)]
    for key, label, w in cols:
        text = "".join(axis) if key == "hours" else label.ljust(w) if key == "branch" else label.rjust(w)
        header.append(("  " + text, dim))

    lines = [header] if max_rows >= 3 else []
    room = max(1, max_rows - len(lines))
    shown = projects if len(projects) <= room else projects[: room - 1]
    for p, state, cost in shown:
        glyph, gattr = project_glyph(state, f)
        line = [(glyph + " ", gattr), (clip(p.name, name_w).ljust(name_w), curses.A_BOLD if state else 0)]
        row = cells(p, state, cost)
        for key, _, _ in cols:
            line.append(("  ", 0))
            line.extend(row[key])
        lines.append(line)
    if len(shown) < len(projects):
        rest = projects[len(shown):]
        lines.append([(clip(f"  +{len(rest)} more: " + ", ".join(f"{p.name} {fmt_hm(p.active)}" for p, _, _ in rest),
                            width), dim)])

    first = min(p.first for p, _, _ in projects)
    right = f" {fmt_hm(L.active)} active · since {datetime.fromtimestamp(first):%H:%M} "
    sessions = sum(len(p.sessions) for p, _, _ in projects)
    commits = sum(p.commits for p, _, _ in projects)
    foot = (f" {len(projects)} project{'s' * (len(projects) != 1)} · {sessions} session{'s' * (sessions != 1)}"
            f" · {sum(p.prompts for p, _, _ in projects)} prompts"
            f" · +{fmt_tokens(sum(p.added for p, _, _ in projects))}"
            f" -{fmt_tokens(sum(p.removed for p, _, _ in projects))} lines"
            + (f" · {commits} commit{'s' * (commits != 1)}" if commits else "") + " ")
    return lines, right, foot


def draw_box(sc, y, x, w, h, title, right="", foot=""):
    dim = curses.color_pair(C_DIM) | curses.A_DIM
    sc.put(y, x, "╭" + "─" * (w - 2) + "╮", dim)
    sc.put(y, x + 2, f" {title} ", curses.color_pair(C_ACCENT) | curses.A_BOLD)
    room = w - len(title) - 8
    if right and room > 10:
        right = clip(right, room)
        sc.put(y, x + w - len(right) - 2, right, dim)
    for r in range(1, h - 1):
        sc.put(y + r, x, "│", dim)
        sc.put(y + r, x + w - 1, "│", dim)
    sc.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", dim)
    if foot and w > 20:
        foot = clip(foot, w - 4)
        sc.put(y + h - 1, x + w - len(foot) - 2, foot, dim)


def draw_lines(sc, y, x, lines):
    for r, line in enumerate(lines):
        cx = x
        for text, attr in line:
            sc.put(y + r, cx, text, attr)
            cx += len(text)


PROJECTS_STACKED = 5   # max lines of the projects panel when it sits below the usage panel
SIDE_USAGE_W = 76      # usage panel width when the two panels sit side by side


def draw_panels(sc, store, f, y, width, compact=False):
    """Usage and today's projects: side by side when wide, stacked otherwise.

    Compact mode (or a narrow terminal) drops the frames and folds projects into
    a single usage row. Returns rows used.
    """
    if compact or width < 60:
        rows = usage_rows(store, f, width - 2, compact) + [projects_row(store, f)]
        lines = layout_rows(rows, width - 2, label_width(store))
        draw_lines(sc, y, 1, lines)
        return len(lines)

    pw = min(100, width - SIDE_USAGE_W - 1)
    if pw >= 64:  # side by side, both boxes as tall as the taller one
        uw = width - pw - 1
        ulines = layout_rows(usage_rows(store, f, uw - 4, False), uw - 4, label_width(store))
        plines, right, foot = project_panel(store, f, pw - 4, max(len(ulines), 5))
        h = max(len(ulines), len(plines)) + 2
        draw_box(sc, y, 0, uw, h, "USAGE")
        draw_lines(sc, y + 1, 2, ulines)
        draw_box(sc, y, uw + 1, pw, h, "PROJECTS TODAY", right, foot)
        draw_lines(sc, y + 1, uw + 3, plines)
        return h

    ulines = layout_rows(usage_rows(store, f, width - 4, False), width - 4, label_width(store))
    draw_box(sc, y, 0, width, len(ulines) + 2, "USAGE")
    draw_lines(sc, y + 1, 2, ulines)
    y2 = y + len(ulines) + 2
    plines, right, foot = project_panel(store, f, width - 4, PROJECTS_STACKED)
    draw_box(sc, y2, 0, width, len(plines) + 2, "PROJECTS TODAY", right, foot)
    draw_lines(sc, y2 + 1, 2, plines)
    return len(ulines) + len(plines) + 4


def draw_empty(sc, f, opencode=False):
    cy, cx = sc.h // 2 - 3, sc.w // 2 - 7
    for i, row in enumerate(EMPTY_BOT):
        sc.put(cy + i, cx, row, curses.color_pair(C_IDLE) | curses.A_DIM)
    msg = ("No Claude or OpenCode sessions running" if opencode else "No Claude sessions running") + "." * ((f // 6) % 4)
    sc.put(cy + len(EMPTY_BOT) + 1, sc.w // 2 - (19 if opencode else 13), msg, curses.color_pair(C_DIM))


def draw(scr, store, f, show_idle, compact=False):
    sc = Screen(scr)
    sessions = store.ordered()
    draw_header(sc, sessions, f, show_idle, compact, bool(store.oc) if opencode_installed() else None)
    visible = [s for s in sessions if show_idle or not s.idle or s.attention]
    cols = max(1, min(len(visible), sc.w // 56))
    w = min(MAX_CARD_W, (sc.w - 1) // cols)
    grid_w = cols * w - 1 if visible else min(sc.w - 1, MAX_CARD_W * 2)
    top = 2 + draw_panels(sc, store, f, 2, grid_w, compact) + 1
    if not visible:
        draw_empty(sc, f, bool(store.oc))
        return

    y = top
    for row_start in range(0, len(visible), cols):
        row = visible[row_start:row_start + cols]
        h = max(card_height(s) for s in row)
        if y + h > sc.h - 1:
            more = len(visible) - row_start
            sc.put(sc.h - 1, sc.w - 20, f"+{more} more ↓ (enlarge)", curses.color_pair(C_WARN))
            break
        for i, s in enumerate(row):
            draw_card(sc, s, y, i * w, w - 1, h, f)
        y += h


def run(scr, opencode=False):
    curses.curs_set(0)
    scr.timeout(int(1000 / FPS))
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_BUSY, curses.COLOR_GREEN, bg)
    curses.init_pair(C_IDLE, curses.COLOR_BLUE, bg)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
    curses.init_pair(C_SUB, curses.COLOR_MAGENTA, bg)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(C_DANGER, curses.COLOR_RED, bg)

    sc = Screen(scr)  # the first scan of today's transcripts can take a second or two
    sc.put(sc.h // 2, max(0, sc.w // 2 - 18), "scanning today's Claude transcripts…", curses.color_pair(C_DIM))
    scr.refresh()
    store = Store(opencode)
    show_idle = True
    compact = False
    frame = 0
    last_poll = 0.0
    while True:
        now = time.time()
        if now - last_poll >= DATA_INTERVAL:
            store.refresh()
            last_poll = now
        ch = scr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            break
        if ch in (ord("i"), ord("I")):
            show_idle = not show_idle
        if ch in (ord("u"), ord("U")):
            compact = not compact
        if ch in (ord("o"), ord("O")) and opencode_installed():
            store.set_opencode(not store.oc)
            last_poll = 0.0
        scr.erase()
        draw(scr, store, frame, show_idle, compact)
        scr.refresh()
        frame += 1


def once(opencode=False):
    store = Store(opencode)
    store.refresh()
    time.sleep(DATA_INTERVAL)
    store.refresh()
    sessions = store.ordered()
    for key, label, window in limit_windows(store.limits):
        lim = (store.limits or {}).get(key)
        if lim:
            p = pace(lim, window)
            print(f"{label}: {lim.get('used_percentage')}% used, resets {fmt_reset(lim.get('resets_at', 0))}"
                  + (f", {p[0]:.0%} of window elapsed, {p[1]}" if p else ""))
    t = store.ledger.totals()
    print(f"today: {fmt_tokens(t['output'])} out, {fmt_tokens(t['input'] + t['cache_write'] + t['output'])} processed,"
          f" +{fmt_tokens(t['cache_read'])} cache reads, {t['messages']} replies")
    if t["models"]:
        print("models: " + ", ".join(f"{pretty_model(m)} {fmt_tokens(v)} out" for m, v in sorted(t["models"].items(), key=lambda kv: -kv[1])))
    print(f"cost: ≈${store.cost_today:.2f} today ({store.cost_sessions} sessions)")
    if store.oc:
        o = store.oc.totals()
        print(f"opencode: {store.oc.error}" if store.oc.error else
              f"opencode: {fmt_tokens(o['output'])} out, {fmt_tokens(o['input'] + o['cache_write'] + o['output'])} processed,"
              f" ≈${o['cost']:.2f}, {o['messages']} replies"
              + "".join(f", {pretty_model(m)} {fmt_tokens(v)} out" for m, v in sorted(o["models"].items(), key=lambda kv: -kv[1])))
    projects = store.projects_today()
    if projects:
        print(f"projects: {fmt_hm(store.ledger.active)} active today")
    for p, state, cost in projects:
        live = {"wait": "needs you", "busy": "working", None: ""}.get(state, "open")
        print(f"  {p.name:<22} {fmt_hm(p.active):>6}  {len(p.sessions)} sess · {p.prompts} prompts"
              f" · {len(p.files)} files +{p.added}/-{p.removed} · {p.commits} commits"
              f" · {fmt_tokens(p.output)} out" + (f" · ${cost:.2f}" if cost else "")
              + (f"  [{p.branch}]" if p.branch else "") + (f"  ({live})" if live else "")
              + ("  via OpenCode" if p.sources == {"opencode"} else "  + OpenCode" if "opencode" in p.sources else ""))
    if not sessions:
        print("No Claude or OpenCode sessions running." if store.oc else "No Claude sessions running.")
    for s in sessions:
        act = s.activity
        act_s = f"{act[1]} {act[2]}".strip() if act else "-"
        status = "WAITING" if s.waiting else s.attention.upper() if s.attention else s.status
        print(f"[{status:>7}] {s.name}  " + ("OpenCode " if s.source == "opencode" else "")
              + f"{'sid=' + s.sid if s.pid is None else f'pid={s.pid}'}  {s.project}")
        if s.waiting:
            print(f"          needs you: {s.wait.get('reason')} {s.wait.get('tool', '')} {s.wait.get('detail', '')}".rstrip())
        print(f"          {s.title or '(untitled)'}  · {pretty_model(s.model)} · up {fmt_dur(s.uptime_secs())}")
        print(f"          {act_s[:100]}")
        if s.sl:
            ctx = (s.sl.get("context_window") or {}).get("used_percentage")
            cost = (s.sl.get("cost") or {}).get("total_cost_usd")
            print(f"          ctx {'?' if ctx is None else f'{ctx:.0f}'}% · ${cost or 0:.2f}")
        for sa in s.active_subs():
            print(f"          └ {sa.desc}")


def snapshot(store):
    """Everything the dashboard shows, as plain data (for the --json feed)."""
    now = time.time()
    level = {C_BUSY: "ok", C_WARN: "warn", C_DANGER: "danger", C_DIM: "dim"}
    sessions = []
    for s in store.ordered():
        st = state_of(s)
        if st == "wait":
            w = s.wait
            label = {"permission": "approve", "question": "answer", "input": "input"}.get(w.get("reason"), "respond")
            doing = f"{label} ⚒ {w['tool']}" if w.get("tool") else label
            detail, elapsed = w.get("detail") or "", now - (w.get("since") or now)
        else:
            kind, label, detail = s.activity or ("", "", "")
            doing = f"⚒ {label}" if kind == "tool" else label.lower()
            elapsed = s.state_secs()
        spark = list(s.spark)
        top = max(spark) or 1
        cost = s.sl.get("cost") or {}
        sessions.append({
            "pid": s.pid, "sid": s.sid, "cwd": s.info.get("cwd"), "transcript": s.transcript,
            "source": s.source, "key": f"{s.source}:{s.sid if s.pid is None else s.pid}",
            "name": s.name, "title": s.title or "", "project": s.project, "model": pretty_model(s.model),
            "state": st, "status": s.status, "doing": doing, "detail": detail or "",
            "attention": s.attention, "outcome": s.outcome,
            "elapsed": fmt_dur(elapsed), "state_secs": round(max(0, elapsed), 1), "uptime": fmt_dur(s.uptime_secs()),
            "ctx": (s.sl.get("context_window") or {}).get("used_percentage"),
            "cost": cost.get("total_cost_usd"),
            "subs": [sa.desc for sa in s.active_subs()], "subs_total": len(s.subs),
            "spark": [0 if v == 0 else max(1, round(v / top * 8)) for v in spark],
        })

    limits = []
    for key, label, window in limit_windows(store.limits):
        lim = (store.limits or {}).get(key) or {}
        if lim.get("used_percentage") is None:
            continue
        p = pace(lim, window)
        limits.append({"key": key, "label": label, "window": window, "used": lim["used_percentage"],
                       "resets_at": lim.get("resets_at"),
                       "resets": fmt_reset(lim["resets_at"]) if lim.get("resets_at") else "",
                       "elapsed": p[0] if p else None, "pace": p[1] if p else "",
                       "level": level.get(p[2], "dim") if p else "dim"})
    stale = now - store.limits_at > 300 and store.limits

    t = store.ledger.totals()
    models = sorted(t["models"].items(), key=lambda kv: -kv[1])
    total_out = sum(v for _, v in models) or 1
    today = {
        "output": t["output"], "processed": t["input"] + t["cache_write"] + t["output"],
        "cache_read": t["cache_read"], "replies": t["messages"],
        "hit": t["cache_read"] / (t["input"] + t["cache_write"] + t["cache_read"] or 1),
        "hourly": t["hourly"], "now_hour": datetime.now().hour,
        "cost": store.cost_today, "cost_sessions": store.cost_sessions,
        "cost_open": sum((s.sl.get("cost") or {}).get("total_cost_usd") or 0 for s in store.sessions.values()),
        "models": [{"name": pretty_model(m), "share": v / total_out} for m, v in models[:4]],
    }

    projects = store.projects_today()
    items = [{"name": p.name, "state": state, "time": fmt_hm(p.active), "active_secs": p.active,
              "hourly": p.hourly, "prompts": p.prompts, "files": len(p.files), "added": p.added,
              "removed": p.removed, "commits": p.commits, "cost": cost, "output": p.output,
              "branch": p.branch, "sessions": len(p.sessions), "sources": sorted(p.sources)}
             for p, state, cost in projects]
    first = min((p.first for p, _, _ in projects), default=0)
    return {
        "time": now, "sessions": sessions, "limits": limits,
        "limits_stale": f"limits as of {fmt_dur(now - store.limits_at)} ago" if stale else None,
        "today": today,
        "opencode": opencode_snapshot(store),
        "setup": dict(setup_status(), opencode=oc_plugin_installed() if store.oc else None),
        "projects": {"active": fmt_hm(store.ledger.active), "first": datetime.fromtimestamp(first).strftime("%H:%M") if first else "",
                     "items": items},
    }


def opencode_snapshot(store):
    """Whether OpenCode is installed (the widget offers its menu item only then), and
    with --opencode, today's OpenCode usage."""
    if not opencode_installed():
        return None
    out = {"installed": True, "enabled": bool(store.oc)}
    if store.oc:
        t = store.oc.totals()
        total = sum(t["models"].values()) or 1
        out.update(error=store.oc.error, today={
            "output": t["output"], "processed": t["input"] + t["cache_write"] + t["output"],
            "cache_read": t["cache_read"], "replies": t["messages"], "cost": t["cost"], "hourly": t["hourly"],
            "models": [{"name": pretty_model(m), "share": v / total}
                       for m, v in sorted(t["models"].items(), key=lambda kv: -kv[1])[:3]]})
    return out


def stream(single=False, opencode=False):
    """--json: one snapshot per line, every DATA_INTERVAL seconds, until the reader goes away."""
    store = Store(opencode)
    try:
        while True:
            store.refresh()
            print(json.dumps(snapshot(store), ensure_ascii=False), flush=True)
            if single:
                return
            time.sleep(DATA_INTERVAL)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


# ─── setup: --install / --uninstall / --doctor ──────────────────────────────

SETTINGS = os.path.join(HOME, ".claude", "settings.json")
HOOK_NAME = "ccwatch-hook.sh"
MARKER = "# ccwatch:"
MARKER_JS = "// ccwatch:"  # first line of ccwatch-opencode.js
OWN_STATUSLINE = os.path.join(HOME, ".claude", "ccwatch-statusline.sh")
# event -> matcher (None: every occurrence); the table under "Waiting hook" in the README
HOOK_EVENTS = {
    "PermissionRequest": None, "PreToolUse": "AskUserQuestion|ExitPlanMode", "Elicitation": None,
    "Notification": "permission_prompt|elicitation_dialog",
    "PostToolUse": None, "PostToolUseFailure": None, "PermissionDenied": None, "ElicitationResult": None,
    "UserPromptSubmit": None, "Stop": None, "StopFailure": None, "SessionEnd": None,
}
CAPTURE_BLOCK = """# ccwatch: save the latest status line input per session (cost, context, plan limits)
cc_dir="$HOME/.cache/ccwatch/statusline"
cc_sid=$(echo "$input" | jq -r '.session_id // empty')
if [ -n "$cc_sid" ]; then
  mkdir -p "$cc_dir" && echo "$input" > "$cc_dir/$cc_sid.json.tmp" && mv "$cc_dir/$cc_sid.json.tmp" "$cc_dir/$cc_sid.json"
fi
"""
OWN_STATUSLINE_TEXT = """#!/bin/sh
# Status line written by ccwatch.py --install: the model and context use, and a
# copy of Claude Code's status data for the ccwatch dashboard and widget.
input=$(cat)

""" + CAPTURE_BLOCK + """
echo "$input" | jq -r '"\\(.model.display_name // "Claude") · ctx \\(.context_window.used_percentage // 0 | floor)%"'
"""


def tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


def find_jq():
    import shutil
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        if os.access(os.path.join(d, "jq"), os.X_OK):
            return os.path.join(d, "jq")
    return shutil.which("jq")


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def write_text(path, text, like=None):
    """Write atomically, keeping the permissions of `like` (the file being replaced)."""
    tmp = path + ".tmp-ccwatch"
    with open(tmp, "w") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(like or path).st_mode & 0o7777)
    except OSError:
        pass
    os.replace(tmp, path)


def backup(path, suffix):
    """Copy path to path+suffix, unless an earlier backup is already there."""
    import shutil
    if os.path.exists(path) and not os.path.exists(path + suffix):
        shutil.copy2(path, path + suffix)
        return path + suffix
    return None


def load_settings():
    """(settings dict, error). A missing file is an empty dict; a broken one is an error."""
    if not os.path.exists(SETTINGS):
        return {}, None
    d = load_json(SETTINGS)
    if not isinstance(d, dict):
        return None, f"{tilde(SETTINGS)} isn't valid JSON; fix it first"
    return d, None


def is_ours(h):
    return isinstance(h, dict) and HOOK_NAME in str(h.get("command") or "")


def strip_hooks(settings, prune=True):
    """A copy of settings without any ccwatch-hook.sh entries; other hooks are left alone.
    With prune, events left with no entries are dropped too."""
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for ev in list(hooks):
        entries = hooks[ev]
        if not isinstance(entries, list):
            continue
        kept = []
        for e in entries:
            inner = e.get("hooks") if isinstance(e, dict) else None
            if isinstance(inner, list) and any(is_ours(h) for h in inner):
                inner = [h for h in inner if not is_ours(h)]
                if not inner:
                    continue
                e = dict(e, hooks=inner)
            kept.append(e)
        hooks[ev] = kept
        if prune and not kept:
            del hooks[ev]
    if prune and not hooks:
        del out["hooks"]
    return out


def merge_hooks(settings, command):
    """settings with exactly one ccwatch hook entry per HOOK_EVENTS event, running
    `command`. Earlier ccwatch entries (say, from an old path) are replaced; running
    it again changes nothing."""
    out = strip_hooks(settings, prune=False)
    if not isinstance(out.get("hooks"), dict):
        out["hooks"] = {}
    hooks = out["hooks"]
    for ev, matcher in HOOK_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 5, "async": True}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        if not isinstance(hooks.get(ev), list):
            hooks[ev] = []
        hooks[ev].append(entry)
    for ev in [ev for ev, v in hooks.items() if v == []]:
        del hooks[ev]
    return out


def hook_status(settings):
    """(events missing a ccwatch hook with the right matcher, hook script paths in use)."""
    import shlex
    have, paths = {}, set()
    for ev, entries in ((settings or {}).get("hooks") or {}).items():
        for e in entries if isinstance(entries, list) else ():
            if not isinstance(e, dict):
                continue
            for h in e.get("hooks") or ():
                if is_ours(h):
                    have.setdefault(ev, set()).add(e.get("matcher") or None)
                    try:
                        paths.add(os.path.expanduser(shlex.split(h["command"])[-1]))
                    except (ValueError, IndexError):
                        pass
    missing = [ev for ev, m in HOOK_EVENTS.items() if m not in have.get(ev, ())]
    return missing, sorted(paths)


def statusline_script(settings):
    """The script file that statusLine.command runs, when it's a plain `[sh] <file>`."""
    import shlex
    cmd = (((settings or {}).get("statusLine") or {}).get("command") or "").strip()
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    if len(parts) == 2 and os.path.basename(parts[0]) in ("sh", "bash", "zsh", "dash"):
        parts = parts[1:]
    if len(parts) != 1:
        return None
    path = os.path.expanduser(os.path.expandvars(parts[0]))
    return path if os.path.isfile(path) else None


def hook_source():
    """Path of the hook script the settings should point at. A packaged app's copy
    moves with every update, so it's copied to ~/.claude first (by install())."""
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, HOOK_NAME)
    if ".app/Contents/" in here + "/":
        return bundled, os.path.join(HOME, ".claude", HOOK_NAME)
    return bundled, bundled


INPUT_LINE = re.compile(r"^\s*input=\$\(cat\)\s*$", re.M)


def install(dry=False, opencode=False):
    """Add the hook entries and the status line capture, and with opencode, the OpenCode
    plugin (an installed one is kept up to date either way). Prints what it did; returns
    an exit code."""
    import shlex
    import shutil
    say = print
    settings, err = load_settings()
    if err:
        say("✗ " + err)
        return 1
    changes = 0
    src, hook = hook_source()
    if not os.path.isfile(src):
        say(f"✗ {HOOK_NAME} not found next to ccwatch.py")
        return 1

    # 1. the hook script, copied out of an app bundle so updates don't break the path
    if src != hook and read_text(src) != read_text(hook):
        changes += 1
        say(f"+ copy {HOOK_NAME} to {tilde(hook)}")
        if not dry:
            os.makedirs(os.path.dirname(hook), exist_ok=True)
            shutil.copyfile(src, hook)
    if not dry and not os.access(hook, os.X_OK):
        os.chmod(hook, 0o755)

    # 2. hook entries
    new = merge_hooks(settings, shlex.quote(hook) if " " not in hook else '"' + hook + '"')
    if new == settings:
        say(f"✓ hooks already set up ({len(HOOK_EVENTS)} events)")
    else:
        changes += 1
        say(f"+ add ccwatch hook entries for {len(HOOK_EVENTS)} events to {tilde(SETTINGS)}")

    # 3. status line capture
    script = statusline_script(new)
    statusline = None  # new text for the status line script
    if script:
        text = read_text(script) or ""
        if MARKER in text:
            say(f"✓ status line {tilde(script)} already saves captures")
        elif INPUT_LINE.search(text):
            m = INPUT_LINE.search(text)
            statusline = text[:m.end()] + "\n" + CAPTURE_BLOCK + text[m.end():]
            changes += 1
            say(f"+ add the capture block to {tilde(script)} (backup: {tilde(script)}.bak)")
        else:
            say(f"! {tilde(script)} has no `input=$(cat)` line; add this after it reads its input:\n")
            say(CAPTURE_BLOCK)
    elif not (new.get("statusLine") or {}).get("command"):
        script, statusline = OWN_STATUSLINE, OWN_STATUSLINE_TEXT
        new["statusLine"] = {"type": "command", "command": "sh " + shlex.quote(OWN_STATUSLINE)}
        changes += 1
        say(f"+ create {tilde(OWN_STATUSLINE)} and use it as the status line")
    else:
        say("! the status line is an inline command; make it save its input like this (the input in $input):\n")
        say(CAPTURE_BLOCK)

    # 4. the OpenCode plugin
    plugin = None  # its new text
    if opencode or oc_plugin_installed():
        src_js = read_text(os.path.join(os.path.dirname(os.path.abspath(__file__)), OC_PLUGIN_NAME))
        if src_js is None:
            say(f"✗ {OC_PLUGIN_NAME} not found next to ccwatch.py")
            return 1
        if read_text(OC_PLUGIN) == src_js:
            say(f"✓ OpenCode plugin {tilde(OC_PLUGIN)} is up to date")
        elif os.path.exists(OC_PLUGIN) and not oc_plugin_installed():
            say(f"! {tilde(OC_PLUGIN)} exists and isn't ccwatch's; move it aside to install the OpenCode plugin")
        else:
            plugin = src_js
            changes += 1
            say(f"+ {'update' if os.path.exists(OC_PLUGIN) else 'add'} the OpenCode plugin {tilde(OC_PLUGIN)}"
                " (it records which sessions wait on you)")

    if not find_jq():
        say("! jq not found: the hook and the status line need it (brew install jq)")
    if dry or not changes:
        if dry:
            say("(dry run: nothing written)" if changes else "nothing to do")
        return 0

    if new != settings:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        b = backup(SETTINGS, ".bak-ccwatch")
        if b:
            say(f"  backup: {tilde(b)}")
        write_text(SETTINGS, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    if statusline is not None:
        if os.path.exists(script):
            backup(script, ".bak")
        write_text(script, statusline)
        if script == OWN_STATUSLINE:
            os.chmod(script, 0o755)
    if plugin is not None:
        os.makedirs(os.path.dirname(OC_PLUGIN), exist_ok=True)
        write_text(OC_PLUGIN, plugin)
    say("done. Restart running Claude sessions if they don't pick up the hooks; status data appears after their next reply.")
    if plugin is not None:
        say("     OpenCode 2.x loads the plugin by itself within seconds; OpenCode 1.x needs a restart.")
    return 0


def uninstall(dry=False):
    """Remove the hook entries and the status line capture block."""
    settings, err = load_settings()
    if err:
        print("✗ " + err)
        return 1
    new = strip_hooks(settings)
    did = []
    if new != settings:
        did.append(f"- remove ccwatch hook entries from {tilde(SETTINGS)}")
    script = statusline_script(new)
    text = read_text(script) if script else None
    statusline = None
    if script == OWN_STATUSLINE:
        new.pop("statusLine", None)
        did.append(f"- remove the status line {tilde(OWN_STATUSLINE)}")
    elif text and MARKER in text:
        lines = text.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith(MARKER))
        end = next((i for i in range(start, len(lines)) if lines[i].strip() == "fi"), None)
        if end is not None:
            end += 1 if end + 1 < len(lines) and not lines[end + 1].strip() else 0
            statusline = "\n".join(lines[:start] + lines[end + 1:])
            if start > 0 and not lines[start - 1].strip() and statusline.count("\n\n\n"):
                statusline = statusline.replace("\n\n\n", "\n\n", 1)
            did.append(f"- remove the capture block from {tilde(script)}")
    if oc_plugin_installed():
        did.append(f"- remove the OpenCode plugin {tilde(OC_PLUGIN)}")
    for line in did or ["nothing to remove"]:
        print(line)
    if dry or not did:
        if dry and did:
            print("(dry run: nothing written)")
        return 0
    if new != settings:
        backup(SETTINGS, ".bak-ccwatch")
        write_text(SETTINGS, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    if script == OWN_STATUSLINE:
        os.remove(OWN_STATUSLINE)
    elif statusline is not None:
        backup(script, ".bak")
        write_text(script, statusline)
    if oc_plugin_installed():
        os.remove(OC_PLUGIN)
    print("done. ~/.cache/ccwatch is left in place; rm -rf it to delete the saved data.")
    return 0


def doctor_checks(opencode=False):
    """[(ok, label, hint)]: ok is True, False (broken) or None (a warning)."""
    import platform
    out = []

    def add(ok, label, hint=""):
        out.append((ok, label, hint))

    add(sys.version_info >= (3, 8), f"Python {platform.python_version()}", "ccwatch needs Python 3.8 or later")
    jq = find_jq()
    add(bool(jq), f"jq at {jq}" if jq else "jq not found", "brew install jq; the hook and the status line use it")

    live = set()
    for path in glob.glob(os.path.join(SESS_DIR, "*.json")):
        info = load_json(path) or {}
        try:
            pid = int(info.get("pid") or os.path.basename(path).split(".")[0])
        except ValueError:
            continue
        if pid_alive(pid):
            live.add(info.get("sessionId"))
    add(os.path.isdir(SESS_DIR), f"session registry: {len(live)} live session{'s' * (len(live) != 1)}",
        "~/.claude/sessions is missing; this Claude Code version may not write it")
    add(os.path.isdir(PROJ_DIR), "transcripts in ~/.claude/projects", "no transcripts yet; start a Claude session")

    settings, err = load_settings()
    if err:
        add(False, err)
        settings = {}
    missing, paths = hook_status(settings)
    add(not missing, f"hooks for all {len(HOOK_EVENTS)} events" if not missing
        else f"hooks missing for: {', '.join(missing)}", "run: ccwatch.py --install")
    for p in paths:
        add(os.access(p, os.X_OK), f"hook script {tilde(p)}",
            "missing or not executable; run: ccwatch.py --install")

    script = statusline_script(settings)
    if not (settings.get("statusLine") or {}).get("command"):
        add(False, "no status line configured", "run: ccwatch.py --install")
    elif script:
        ok = MARKER in (read_text(script) or "")
        add(ok, f"status line {tilde(script)}" + (" saves captures" if ok else " doesn't save captures"),
            "run: ccwatch.py --install")
    else:
        add(None, "status line is an inline command; can't check it saves captures",
            "see `ccwatch.py --install --dry-run` for the block to add")

    caps = [os.path.getmtime(p) for p in glob.glob(os.path.join(STATUSLINE_DIR, "*.json"))]
    if caps:
        age = time.time() - max(caps)
        add(True if age < 86400 else None, f"newest status line capture {fmt_dur(age)} ago",
            "captures update after each reply in a session")
    else:
        add(None, "no status line captures yet", "they appear after a session's next reply")

    stale = [p for p in glob.glob(os.path.join(STATE_DIR, "*.json"))
             if os.path.basename(p)[:-len(".json")] not in live]
    add(True if not stale else None, f"{len(stale)} stale \"needs you\" flag{'s' * (len(stale) != 1)}",
        "left by sessions that ended abruptly; ccwatch deletes them after a day")
    if opencode:
        out.extend(opencode_checks())
    return out


def opencode_checks():
    out = []

    def add(ok, label, hint=""):
        out.append((ok, label, hint))

    if not opencode_installed():
        add(False, "OpenCode not found", "install it from opencode.ai, or turn OpenCode monitoring off")
        return out
    oc = OpenCode()
    oc.refresh()
    oc.close()
    if oc.error:
        add(False, oc.error, "ccwatch reads OpenCode's database read-only; a newer OpenCode may have changed it")
    else:
        add(True, f"OpenCode database {tilde(oc.path)} ({'2.x' if oc.v2 else '1.x'} layout, "
                  f"{len(oc.sessions)} open session{'s' * (len(oc.sessions) != 1)})")
        add(True if oc.catalog else None, f"OpenCode model list: {len(oc.catalog)} models",
            "without it, OpenCode cards show no context gauge")
    if not oc_plugin_installed():
        add(None, "OpenCode plugin not installed: OpenCode sessions never show \"needs you\"",
            "run: ccwatch.py --install --opencode")
    else:
        recs = glob.glob(os.path.join(OC_STATE_DIR, "*.json"))
        live = oc.states()
        if live:
            add(True, f"OpenCode plugin {tilde(OC_PLUGIN)} is reporting ({len(live)} session{'s' * (len(live) != 1)})")
        elif recs:
            add(None, f"OpenCode plugin {tilde(OC_PLUGIN)}: no report from a running OpenCode",
                "OpenCode 2.x reloads plugins by itself; on 1.x, restart OpenCode")
        else:
            add(None, f"OpenCode plugin {tilde(OC_PLUGIN)}: nothing reported yet",
                "it reports once a session starts working (on OpenCode 1.x, restart it first)")
    return out


def doctor(opencode=False):
    checks = doctor_checks(opencode)
    for ok, label, hint in checks:
        mark = {True: "✓", False: "✗", None: "!"}[ok]
        print(f"{mark} {label}" + (f"\n    {hint}" if hint and ok is not True else ""))
    return 1 if any(ok is False for ok, _, _ in checks) else 0


_setup = {"key": None, "at": 0.0, "value": None}


def setup_status():
    """{hooks, statusline}: whether each is set up, for the widget's set-up prompt.
    Re-read when settings.json changes, or every 30 s."""
    try:
        key = os.path.getmtime(SETTINGS)
    except OSError:
        key = None
    if _setup["value"] is None or key != _setup["key"] or time.time() - _setup["at"] > 30:
        settings, _ = load_settings()
        settings = settings or {}
        script = statusline_script(settings)
        captured = MARKER in (read_text(script) or "") if script else False
        _setup.update(key=key, at=time.time(), value={
            "hooks": not hook_status(settings)[0],
            "statusline": captured or bool(glob.glob(os.path.join(STATUSLINE_DIR, "*.json"))),
        })
    return _setup["value"]


# ─── focus: bring a session's window to the front (--focus <pid>) ───────────

# VS Code family: bundle id -> folder under ~/Library/Application Support
VSCODE_LIKE = {"com.microsoft.VSCode": "Code", "com.microsoft.VSCodeInsiders": "Code - Insiders",
               "com.todesktop.230313mzl4w4u92": "Cursor", "com.exafunction.windsurf": "Windsurf",
               "com.vscodium": "VSCodium"}

ITERM_FOCUS = """on run argv
  tell application id "com.googlecode.iterm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is item 1 of argv then
            select w
            select t
            select s
            activate
            return "tab"
          end if
        end repeat
      end repeat
    end repeat
    activate
  end tell
  return "app"
end run"""

TERMINAL_FOCUS = """on run argv
  tell application id "com.apple.Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is item 1 of argv then
          set selected of t to true
          set index of w to 1
          activate
          return "tab"
        end if
      end repeat
    end repeat
    activate
  end tell
  return "app"
end run"""

# JetBrains IDEs have no AppleScript dictionary: raise the project's window through
# System Events (needs Accessibility permission). Titles read "<project> – <file>".
RAISE_WINDOW = """on run argv
  tell application "System Events"
    set p to first process whose unix id is ((item 1 of argv) as integer)
    repeat with w in windows of p
      if name of w is item 2 of argv or name of w starts with (item 2 of argv) & " – " then
        set frontmost of p to true
        perform action "AXRaise" of w
        return "window"
      end if
    end repeat
  end tell
  return "app"
end run"""


# iTerm2 3.5+ runs shells under iTermServer, a child of launchd rather than of iTerm2:
# helper in an ancestor's path -> the running app's executable
HOST_HELPERS = {"/iTerm2/iTermServer": ".app/Contents/MacOS/iTerm2"}


def host_of(pid):
    """(bundle path, bundle id, app pid, tty) of the app a claude process runs inside, or None."""
    import plistlib
    import subprocess
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,tty=,comm="], capture_output=True, text=True).stdout
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    if pid not in procs:
        return None
    tty = procs[pid][1]
    bundle, app, p = None, None, pid
    while p in procs and p > 1:  # the outermost .app ancestor is the host (helpers live inside it)
        comm = procs[p][2]
        if ".app/" in comm:
            bundle, app = comm[:comm.index(".app/") + 4], p
        elif not bundle:
            for helper, exe in HOST_HELPERS.items():  # a daemon outside the app that the app owns
                if helper in comm:
                    app = next((q for q, (_, _, c) in procs.items() if c.endswith(exe)), None)
                    if app:
                        c = procs[app][2]
                        bundle = c[:c.index(".app/") + 4]
        p = procs[p][0]
    if not bundle:
        return None
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            bid = plistlib.load(f).get("CFBundleIdentifier", "")
    except (OSError, ValueError):
        bid = ""
    return bundle, bid, app, f"/dev/{tty}" if tty not in ("??", "") else ""


def vscode_folder(support, cwd):
    """The open editor window folder that contains cwd (longest match), from the app's window state."""
    from urllib.parse import unquote, urlparse
    state = load_json(os.path.join(HOME, "Library", "Application Support", support,
                                   "User", "globalStorage", "storage.json")) or {}
    best = ""
    ws = state.get("windowsState") or {}
    # openedWindows is only written when the app quits; while it runs, lastActiveWindow is current
    for w in (ws.get("openedWindows") or []) + [ws.get("lastActiveWindow") or {}]:
        uri = w.get("folder") or ""
        if not uri.startswith("file://"):
            continue
        folder = unquote(urlparse(uri).path).rstrip("/")
        if (cwd == folder or cwd.startswith(folder + "/")) and len(folder) > len(best):
            best = folder
    return best


def idea_project(cwd):
    """Display name of the JetBrains project at or above cwd (.idea/.name, else its folder name)."""
    d = cwd
    while d and d != "/":
        if os.path.isdir(os.path.join(d, ".idea")):
            try:
                with open(os.path.join(d, ".idea", ".name")) as f:
                    return f.read().strip() or os.path.basename(d)
            except OSError:
                return os.path.basename(d)
        d = os.path.dirname(d)
    return ""


def focus(pid):
    """Bring the window running claude process `pid` forward. Never opens a new window:
    when the exact tab or project can't be found, it only activates the app."""
    import subprocess
    host = host_of(pid)
    if not host:
        return "no host app found"
    bundle, bid, app, tty = host
    cwd = ((load_json(os.path.join(SESS_DIR, f"{pid}.json")) or {}).get("cwd") or "").rstrip("/")
    if not cwd:  # not a claude process: an OpenCode terminal's directory
        cwd = next((d for d, p in opencode_clients({}).items() if p == pid), "")
    name = os.path.basename(bundle)[:-4]
    script = {"com.googlecode.iterm2": ITERM_FOCUS, "com.apple.Terminal": TERMINAL_FOCUS}.get(bid)
    if script and tty:
        r = subprocess.run(["osascript", "-e", script, tty], capture_output=True, text=True)
        if r.returncode == 0:
            return f"{name} {r.stdout.strip()}"
    if (bid.startswith("com.jetbrains.") or bid == "com.google.android.studio") and cwd:
        project = idea_project(cwd)
        if project:
            r = subprocess.run(["osascript", "-e", RAISE_WINDOW, str(app), project], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip() == "window":
                return f"{name} window {project}"
    target = vscode_folder(VSCODE_LIKE[bid], cwd) if bid in VSCODE_LIKE and cwd else ""
    # opening a folder that's already open in the editor just focuses its window
    subprocess.run(["open", "-a", bundle] + ([target] if target else []))
    return f"{name} {'window ' + target if target else 'app'}"


if __name__ == "__main__":
    if "--install" in sys.argv:
        sys.exit(install(dry="--dry-run" in sys.argv, opencode="--opencode" in sys.argv))
    elif "--uninstall" in sys.argv:
        sys.exit(uninstall(dry="--dry-run" in sys.argv))
    elif "--doctor" in sys.argv:
        sys.exit(doctor(opencode="--opencode" in sys.argv))
    elif "--focus" in sys.argv:
        i = sys.argv.index("--focus")
        print(focus(int(sys.argv[i + 1])) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit()
              else "usage: ccwatch.py --focus <pid>")
    elif "--json" in sys.argv:
        stream(single="--once" in sys.argv, opencode="--opencode" in sys.argv)
    elif "--once" in sys.argv:
        once(opencode="--opencode" in sys.argv)
    else:
        os.environ.setdefault("ESCDELAY", "25")
        try:
            curses.wrapper(run, "--opencode" in sys.argv)
        except KeyboardInterrupt:
            pass
