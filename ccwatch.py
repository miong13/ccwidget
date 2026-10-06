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

The usage and "projects today" panels come from one incremental scan of every
transcript changed today (tokens, time spent, prompts, edits, commits per directory).

Keys: q quit · i hide/show idle sessions · u compact/expand usage and projects panels
Usage: ccwatch.py [--once] [--json]   (--json streams one snapshot per second; with --once, just one)
       ccwatch.py --focus <pid>        bring the terminal/editor window running that session forward
"""
import curses
import glob
import json
import os
import re
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
    for key in ("description", "command", "file_path", "pattern", "url", "query", "prompt", "skill"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            if key == "file_path":
                v = os.path.basename(v)
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
        """The hook's waiting flag, unless the session has visibly moved on since.

        No hook fires when you deny a prompt, so a flag can outlive the wait;
        new transcript output or the session going idle afterwards clears it.
        """
        w = load_json(os.path.join(STATE_DIR, f"{self.sid}.json"))
        if not w or w.get("state") != "waiting":
            return None
        since = w.get("since") or 0
        try:
            if self.transcript and os.path.getmtime(self.transcript) > since + 2:
                return None
        except OSError:
            pass
        if self.info.get("status") == "idle" and (self.info.get("statusUpdatedAt") or 0) / 1000 > since + 2:
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
        # derived in TokenLedger.summarize()
        self.active, self.hourly = 0.0, [0.0] * 24
        self.first = self.last = 0.0
        self.output = 0


class TokenLedger:
    """Today's token usage and per-project work across every transcript
    (main sessions and sub-agents).

    Reads each file incrementally; assistant messages are written in several
    lines sharing one message id, so usage is keyed by id and the last one wins.
    """

    def __init__(self):
        self.day = None
        self.last_scan = 0.0
        self._reset(None)

    def _reset(self, day):
        self.day = day
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


class Store:
    def __init__(self):
        self.sessions = {}
        self.ledger = TokenLedger()
        self.limits = None       # rate_limits from the newest status line capture
        self.limits_at = 0.0
        self.cost_today = 0.0    # summed session cost of every capture written today
        self.cost_sessions = 0
        self.project_cost = {}   # cwd -> summed session cost today
        self.last_prune = 0.0

    def prune(self):
        """Delete ccwatch's own stale cache files: old status line captures, and
        "needs you" flags left behind by sessions that crashed. The terminal
        dashboard and the widget's feed may both do this at once, so a file that
        has already gone is fine."""
        now = time.time()
        live = {s.sid for s in self.sessions.values()}
        for pattern, max_age in ((os.path.join(STATUSLINE_DIR, "*.json"), CAPTURE_MAX_AGE),
                                 (os.path.join(STATE_DIR, "*.json"), STATE_MAX_AGE)):
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
        self.cost_today, self.cost_sessions, self.project_cost = cost, n, by_proj

        if time.time() - self.ledger.last_scan >= LEDGER_INTERVAL:
            self.ledger.refresh()
        if time.time() - self.last_prune >= PRUNE_INTERVAL:
            self.prune()

    def ordered(self):
        return sorted(self.sessions.values(),
                      key=lambda s: (not s.waiting, not s.busy, s.idle, -(s.info.get("startedAt") or 0)))

    def projects_today(self):
        """Today's projects by time spent, each with the liveliest state of its open sessions."""
        rank = {"wait": 3, "busy": 2, "idle": 1, "other": 1}
        live = {}
        for s in self.sessions.values():
            key = self.ledger.sess_proj.get(s.sid) or s.info.get("cwd")
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


def draw_card(sc, s, y, x, w, h, f):
    st = state_of(s)
    col = state_color(s)
    border = col | (curses.A_BOLD if st == "busy" else curses.A_DIM if st == "idle" else 0)
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
    sc.put(y, x + 2, f" {clip(s.name, w - 22)} ", curses.A_BOLD)

    badge = {"busy": f" {SPIN[f % len(SPIN)]} WORKING ", "idle": " ○ IDLE ",
             "wait": " ▲ NEEDS YOU " if (f // 6) % 2 else " △ NEEDS YOU "}.get(st, f" ! {s.status.upper()} ")
    sc.put(y, x + w - len(badge) - 2, badge, col | curses.A_BOLD | (curses.A_REVERSE if st != "idle" else 0))

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
        w = s.wait
        label = {"permission": "approve", "question": "answer", "input": "input"}.get(w.get("reason"), "respond")
        tool = f" ⚒ {w['tool']}" if w.get("tool") else ""
        sc.put(line, ix, clip(f"{label}{tool}", iw), col | curses.A_BOLD)
        if w.get("detail"):
            sc.put(line + 1, ix + 5, clip(w["detail"], iw - 5), 0)
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
        verb = "working" if st == "busy" else "idle" if st == "idle" else s.status
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
    else:
        sc.put(line, ix, "ctx  (waiting for status line)", dim)

    # sub-agents
    for i, sa in enumerate(s.active_subs()[:3]):
        r = y + 8 + i
        spin = SPIN[(f + i * 3) % len(SPIN)]
        sc.put(r, x + 3, f"└ {spin} ", curses.color_pair(C_SUB) | curses.A_BOLD)
        sc.put(r, x + 7, clip(f"{sa.desc}", w - 10), curses.color_pair(C_SUB))


def draw_header(sc, sessions, f, show_idle, compact=False):
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
    return rows


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
        lines = layout_rows(rows, width - 2, 8)
        draw_lines(sc, y, 1, lines)
        return len(lines)

    pw = min(100, width - SIDE_USAGE_W - 1)
    if pw >= 64:  # side by side, both boxes as tall as the taller one
        uw = width - pw - 1
        ulines = layout_rows(usage_rows(store, f, uw - 4, False), uw - 4, 8)
        plines, right, foot = project_panel(store, f, pw - 4, max(len(ulines), 5))
        h = max(len(ulines), len(plines)) + 2
        draw_box(sc, y, 0, uw, h, "USAGE")
        draw_lines(sc, y + 1, 2, ulines)
        draw_box(sc, y, uw + 1, pw, h, "PROJECTS TODAY", right, foot)
        draw_lines(sc, y + 1, uw + 3, plines)
        return h

    ulines = layout_rows(usage_rows(store, f, width - 4, False), width - 4, 8)
    draw_box(sc, y, 0, width, len(ulines) + 2, "USAGE")
    draw_lines(sc, y + 1, 2, ulines)
    y2 = y + len(ulines) + 2
    plines, right, foot = project_panel(store, f, width - 4, PROJECTS_STACKED)
    draw_box(sc, y2, 0, width, len(plines) + 2, "PROJECTS TODAY", right, foot)
    draw_lines(sc, y2 + 1, 2, plines)
    return len(ulines) + len(plines) + 4


def draw_empty(sc, f):
    cy, cx = sc.h // 2 - 3, sc.w // 2 - 7
    for i, row in enumerate(EMPTY_BOT):
        sc.put(cy + i, cx, row, curses.color_pair(C_IDLE) | curses.A_DIM)
    msg = "No Claude sessions running" + "." * ((f // 6) % 4)
    sc.put(cy + len(EMPTY_BOT) + 1, sc.w // 2 - 13, msg, curses.color_pair(C_DIM))


def draw(scr, store, f, show_idle, compact=False):
    sc = Screen(scr)
    sessions = store.ordered()
    draw_header(sc, sessions, f, show_idle, compact)
    visible = [s for s in sessions if show_idle or not s.idle]
    cols = max(1, min(len(visible), sc.w // 56))
    w = min(MAX_CARD_W, (sc.w - 1) // cols)
    grid_w = cols * w - 1 if visible else min(sc.w - 1, MAX_CARD_W * 2)
    top = 2 + draw_panels(sc, store, f, 2, grid_w, compact) + 1
    if not visible:
        draw_empty(sc, f)
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


def run(scr):
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
    store = Store()
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
        scr.erase()
        draw(scr, store, frame, show_idle, compact)
        scr.refresh()
        frame += 1


def once():
    store = Store()
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
    projects = store.projects_today()
    if projects:
        print(f"projects: {fmt_hm(store.ledger.active)} active today")
    for p, state, cost in projects:
        live = {"wait": "needs you", "busy": "working", None: ""}.get(state, "open")
        print(f"  {p.name:<22} {fmt_hm(p.active):>6}  {len(p.sessions)} sess · {p.prompts} prompts"
              f" · {len(p.files)} files +{p.added}/-{p.removed} · {p.commits} commits"
              f" · {fmt_tokens(p.output)} out" + (f" · ${cost:.2f}" if cost else "")
              + (f"  [{p.branch}]" if p.branch else "") + (f"  ({live})" if live else ""))
    if not sessions:
        print("No Claude sessions running.")
    for s in sessions:
        act = s.activity
        act_s = f"{act[1]} {act[2]}".strip() if act else "-"
        status = "WAITING" if s.waiting else s.status
        print(f"[{status:>7}] {s.name}  pid={s.pid}  {s.project}")
        if s.waiting:
            print(f"          needs you: {s.wait.get('reason')} {s.wait.get('tool', '')} {s.wait.get('detail', '')}".rstrip())
        print(f"          {s.title or '(untitled)'}  · {pretty_model(s.model)} · up {fmt_dur(s.uptime_secs())}")
        print(f"          {act_s[:100]}")
        if s.sl:
            ctx = (s.sl.get("context_window") or {}).get("used_percentage")
            cost = (s.sl.get("cost") or {}).get("total_cost_usd")
            print(f"          ctx {ctx}% · ${cost or 0:.2f}")
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
            "name": s.name, "title": s.title or "", "project": s.project, "model": pretty_model(s.model),
            "state": st, "status": s.status, "doing": doing, "detail": detail or "",
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
              "branch": p.branch, "sessions": len(p.sessions)} for p, state, cost in projects]
    first = min((p.first for p, _, _ in projects), default=0)
    return {
        "time": now, "sessions": sessions, "limits": limits,
        "limits_stale": f"limits as of {fmt_dur(now - store.limits_at)} ago" if stale else None,
        "today": today,
        "projects": {"active": fmt_hm(store.ledger.active), "first": datetime.fromtimestamp(first).strftime("%H:%M") if first else "",
                     "items": items},
    }


def stream(single=False):
    """--json: one snapshot per line, every DATA_INTERVAL seconds, until the reader goes away."""
    store = Store()
    try:
        while True:
            store.refresh()
            print(json.dumps(snapshot(store), ensure_ascii=False), flush=True)
            if single:
                return
            time.sleep(DATA_INTERVAL)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


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
    for w in (state.get("windowsState") or {}).get("openedWindows") or []:
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
    if "--focus" in sys.argv:
        i = sys.argv.index("--focus")
        print(focus(int(sys.argv[i + 1])) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit()
              else "usage: ccwatch.py --focus <pid>")
    elif "--json" in sys.argv:
        stream(single="--once" in sys.argv)
    elif "--once" in sys.argv:
        once()
    else:
        os.environ.setdefault("ESCDELAY", "25")
        try:
            curses.wrapper(run)
        except KeyboardInterrupt:
            pass
