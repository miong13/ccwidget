// ccwatch: OpenCode plugin that records each session's state for the ccwatch dashboard and ccwidget.
//
// Installed by `ccwatch.py --install --opencode` as ~/.config/opencode/plugins/ccwatch.js.
// It writes ~/.cache/ccwatch/opencode/<session id>.json whenever a session starts
// working, goes idle, or waits on you (a permission prompt or a question). It only
// writes those files: it never answers, changes or delays a prompt or a reply.
//
// OpenCode 2.x loads `setup`, whose event stream is an async iterable of { type, data };
// 1.x loads `server`, whose event hook gets { type, properties }. Both end up in handle().
import { mkdir, rename, rm, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

const DIR = join(homedir(), ".cache", "ccwatch", "opencode")

const status = new Map()   // session id -> { state: "busy" | "idle", since }
const asks = new Map()     // session id -> Map(request id -> { reason, tool, detail, since })
const queue = new Map()    // session id -> the write in progress, so writes land in order

const now = () => Date.now() / 1000
const firstLine = (s) => String(s ?? "").trim().split("\n")[0].slice(0, 200)

function save(sid) {
  const open = [...(asks.get(sid)?.values() ?? [])]
  const st = status.get(sid) ?? { state: "busy", since: now() }
  const rec = open.length
    ? { state: "waiting", ...open[0], pending: open.length }
    : { state: st.state, since: st.since }
  rec.session = sid
  rec.pid = process.pid  // ccwatch ignores records whose OpenCode process has gone
  rec.updated = now()
  const path = join(DIR, `${sid}.json`)
  const write = (queue.get(sid) ?? Promise.resolve())
    .then(async () => {
      await mkdir(DIR, { recursive: true })
      await writeFile(path + ".tmp", JSON.stringify(rec))
      await rename(path + ".tmp", path)
    })
    .catch(() => {})
  queue.set(sid, write)
}

function setStatus(sid, state) {
  if (!sid) return
  if (status.get(sid)?.state !== state) status.set(sid, { state, since: now() })
  if (state === "idle") asks.delete(sid)  // a turn that ended has no prompt left open
  save(sid)
}

function ask(sid, id, rec) {
  if (!sid) return
  if (!asks.has(sid)) asks.set(sid, new Map())
  asks.get(sid).set(id ?? String(Math.random()), { ...rec, since: now() })
  save(sid)
}

function answer(sid, id) {
  if (!sid) return
  const open = asks.get(sid)
  if (open && id && open.has(id)) open.delete(id)
  else open?.clear()
  save(sid)
}

const list = (v) => (Array.isArray(v) ? v.join(" ") : v)

async function handle(event) {
  try {
    const p = event?.data ?? event?.properties ?? {}
    switch (event?.type) {
      case "session.execution.started":   // 2.x
        setStatus(p.sessionID, "busy")
        break
      case "session.execution.succeeded":
      case "session.execution.failed":
      case "session.execution.interrupted":
        setStatus(p.sessionID, "idle")
        break
      case "session.status":              // 1.x
        setStatus(p.sessionID, p.status?.type === "idle" ? "idle" : "busy")
        break
      case "session.idle":
        setStatus(p.sessionID, "idle")
        break
      case "permission.asked":    // 1.x before 1.1: permission.updated
      case "permission.updated":
        ask(p.sessionID, p.id, {
          reason: "permission",
          tool: p.action ?? p.permission ?? p.type ?? "",
          detail: firstLine(p.message ?? p.title ?? list(p.resources) ?? list(p.patterns) ?? p.pattern),
        })
        break
      case "permission.replied":
      case "permission.rejected":
        answer(p.sessionID, p.requestID ?? p.permissionID ?? p.id)
        break
      case "form.created":        // 2.x questions
        ask(p.form?.sessionID, p.form?.id, { reason: "question", tool: "question", detail: firstLine(p.form?.title) })
        break
      case "question.asked":      // 1.x questions
        ask(p.sessionID, p.id, {
          reason: "question",
          tool: "question",
          detail: firstLine(p.questions?.[0]?.question ?? p.questions?.[0]?.header),
        })
        break
      case "form.replied":
      case "form.cancelled":
        answer(p.sessionID, p.id)
        break
      case "question.replied":
      case "question.rejected":
        answer(p.sessionID, p.requestID)
        break
      case "session.deleted": {
        const sid = p.info?.id ?? p.sessionID
        if (sid) {
          status.delete(sid)
          asks.delete(sid)
          await rm(join(DIR, `${sid}.json`), { force: true })
        }
        break
      }
    }
  } catch {
    // never let monitoring get in the way of a session
  }
}

export default {
  id: "ccwatch",
  // OpenCode 2.x
  setup(ctx) {
    const abort = new AbortController()
    ;(async () => {
      for await (const event of ctx.event.subscribe({ signal: abort.signal })) await handle(event)
    })().catch(() => {})
    return () => abort.abort()
  },
  // OpenCode 1.x
  server: async () => ({ event: async ({ event }) => handle(event) }),
}
