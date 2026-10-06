// ccwidget — floating, always-on-top desktop widget for ccwatch.
//
// Runs `ccwatch.py --json` as a child process and renders its once-a-second
// snapshots in a translucent panel that floats above other windows, follows
// you across Spaces and full-screen apps, and never takes keyboard focus.
//
// Build: ./build-widget.sh   (creates ccwidget.app next to ccwatch.py)
// Drag anywhere to move · click the chevron to compact · right-click for options
// It also lives in the menu bar: click the icon for the same view in a popover,
// right-click it for the options menu. While a session waits on you, a hovering
// robot assistant holds up a card of those sessions; click one to jump to it.
// Notifications say when a long turn finishes or a plan limit runs high, and
// ⌃⌥⌘J / ⌃⌥⌘W jump to the waiting session / show or hide the floating panel.

import AppKit
import Carbon.HIToolbox
import Combine
import SwiftUI
import UserNotifications

// MARK: - Feed data (mirrors snapshot() in ccwatch.py)

struct Snapshot: Decodable {
    var sessions: [Sess] = []
    var limits: [Limit] = []
    var limitsStale: String?
    var today: Today?
    var projects: Projects?
    var setup: Setup?
}

/// Whether the hook ("needs you") and the status line capture (limits, ctx, cost) are set up.
struct Setup: Decodable {
    var hooks: Bool
    var statusline: Bool
}

struct Sess: Decodable {
    var pid: Int?
    var sid: String?
    var cwd: String?
    var transcript: String?
    var name: String
    var title: String?
    var project: String?
    var model: String?
    var state: String
    var status: String?
    var doing: String?
    var detail: String?
    var elapsed: String?
    var stateSecs: Double?   // seconds in the current state (since the wait began, when waiting)
    var uptime: String?
    var ctx: Double?
    var cost: Double?
    var subs: [String]?
    var subsTotal: Int?
    var spark: [Int]?
}

struct Limit: Decodable {
    var key: String?
    var label: String
    var window: Double?   // seconds, read by ccwatch from the key name (five_hour → 18000)
    var used: Double
    var resetsAt: Double?
    var resets: String?
    var elapsed: Double?
    var pace: String?
    var level: String?
}

struct ModelShare: Decodable {
    var name: String
    var share: Double
}

struct Today: Decodable {
    var output: Int
    var processed: Int
    var cacheRead: Int
    var replies: Int
    var hit: Double
    var hourly: [Int]
    var nowHour: Int
    var cost: Double
    var costSessions: Int?
    var costOpen: Double
    var models: [ModelShare]?
}

struct Proj: Decodable {
    var name: String
    var state: String?
    var time: String
    var activeSecs: Double
    var hourly: [Double]
    var prompts: Int
    var files: Int
    var added: Int
    var removed: Int
    var commits: Int
    var cost: Double?
    var output: Int
    var branch: String?
    var sessions: Int
}

struct Projects: Decodable {
    var active: String
    var first: String?
    var items: [Proj]
}

// MARK: - Feed process

final class Feed: ObservableObject {
    @Published var snap: Snapshot?
    @Published var problem: String?
    @Published var updated = Date.distantPast

    private var proc: Process?
    private var buffer = Data()
    private var stopping = false
    private var failures = 0     // restarts in a row without a good snapshot, for the backoff
    private var lastError = ""   // last stderr line; shown only if the feed dies
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static func locateScript() -> String? {
        if let env = ProcessInfo.processInfo.environment["CCWATCH_PY"] { return env }
        // A packaged app (./package.sh) carries its own copy in Contents/Resources;
        // a dev build (./build-widget.sh) sits next to ccwatch.py, and a bare binary
        // may sit in the same folder.
        let here = Bundle.main.bundleURL
        let dirs = [Bundle.main.resourceURL, here.deletingLastPathComponent(), here].compactMap { $0 }
        for dir in dirs {
            let p = dir.appendingPathComponent("ccwatch.py").path
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        return nil
    }

    static func locatePython() -> String {
        // apps launched from Finder get a bare PATH, so look in the usual places
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return "/usr/bin/python3"
    }

    func start() {
        guard let script = Feed.locateScript() else {
            problem = "ccwatch.py not found inside or next to ccwidget.app"
            return
        }
        buffer = Data()
        lastError = ""
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Feed.locatePython())
        p.arguments = ["-B", script, "--json"]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            DispatchQueue.main.async { self?.consume(d) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
            // a warning on stderr isn't a failure; keep it in case the feed dies
            let last = s.split(separator: "\n").last.map(String.init) ?? s
            DispatchQueue.main.async { self?.lastError = last }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping else { return }
                // 3 s, 6 s, 12 s … up to a minute while it keeps failing
                let delay = min(60, 3 * pow(2, Double(self.failures)))
                self.failures += 1
                self.problem = (self.lastError.isEmpty ? "ccwatch stopped" : self.lastError)
                    + "; restarting in \(Int(delay))s…"
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.start() }
            }
        }
        do {
            try p.run()
            proc = p
        } catch {
            problem = "couldn't start python3: \(error.localizedDescription)"
        }
    }

    /// Bring the terminal/editor window running a session to the front (ccwatch.py --focus).
    static func focus(pid: Int) {
        guard let script = locateScript() else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: locatePython())
        p.arguments = ["-B", script, "--focus", String(pid)]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    /// Run ccwatch.py with these arguments (e.g. --doctor) and return everything it printed.
    static func run(_ args: [String]) -> String {
        guard let script = locateScript() else { return "ccwatch.py not found inside or next to ccwidget.app" }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: locatePython())
        p.arguments = ["-B", script] + args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { return "couldn't start python3: \(error.localizedDescription)" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func stop() {
        stopping = true
        proc?.terminate()
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        var latest: Data?
        while let nl = buffer.firstIndex(of: 0x0A) {
            latest = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        guard let line = latest else { return }
        do {
            snap = try decoder.decode(Snapshot.self, from: line)
            updated = Date()
            problem = nil
            failures = 0
        } catch {
            problem = "bad data from ccwatch: \(error)"
        }
    }
}

// MARK: - Look

enum Palette {
    static let busy = Color(nsColor: .systemGreen)
    static let idle = Color(nsColor: .systemBlue)
    static let warn = adaptive(light: .systemOrange, dark: .systemYellow)  // yellow is unreadable on white
    static let danger = Color(nsColor: .systemRed)
    static let accent = Color(nsColor: .systemTeal)
    static let sub = Color(nsColor: .systemPurple)
    static let dim = Color.primary.opacity(0.5)
    static let faint = Color.primary.opacity(0.12)
    /// Solid widget background, following the system Light/Dark appearance.
    static let background = adaptive(light: NSColor(white: 0.97, alpha: 1), dark: NSColor(white: 0.11, alpha: 1))

    static func adaptive(light: NSColor, dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        })
    }

    static func level(_ pct: Double) -> Color { pct < 50 ? busy : pct < 80 ? warn : danger }

    static func pace(_ level: String?) -> Color {
        switch level {
        case "ok": return busy
        case "warn": return warn
        case "danger": return danger
        default: return dim
        }
    }
}

func fmtTokens(_ n: Int) -> String {
    let d = Double(n)
    if d >= 1e9 { return String(format: "%.1fB", d / 1e9) }
    if d >= 1e6 { return String(format: "%.1fM", d / 1e6) }
    if d >= 1e3 { return String(format: "%.1fk", d / 1e3) }
    return "\(n)"
}

func fmtCount(_ n: Int) -> String { n >= 10_000 ? fmtTokens(n) : "\(n)" }

/// A tiny computer monitor. Working: lines of script type out and scroll up the
/// screen. Needs you: the screen flashes yellow with a "!". Idle: a dark screen
/// with a slowly blinking prompt.
struct MonitorIcon: View {
    let state: String
    let frame: Int
    var tint: Color = Palette.busy
    var width: CGFloat = 16

    // drawn on a 16 x 14 grid, then scaled to `width`
    private static let screen = CGRect(x: 2.5, y: 2.5, width: 11, height: 6.5)
    private static let pitch: CGFloat = 1.75     // distance between script lines
    private static let framesPerLine = 4         // 10 fps: a new line every 0.4s

    var body: some View {
        Canvas { ctx, size in
            ctx.scaleBy(x: size.width / 16, y: size.height / 14)
            let bezelColor = state == "idle" ? Palette.dim : state == "wait" ? Palette.warn : tint
            // stand, then bezel
            ctx.fill(Path(CGRect(x: 7, y: 11, width: 2, height: 1.5)), with: .color(bezelColor.opacity(0.8)))
            ctx.fill(Path(roundedRect: CGRect(x: 4.5, y: 12.5, width: 7, height: 1.2), cornerRadius: 0.6),
                     with: .color(bezelColor.opacity(0.8)))
            let bezel = Path(roundedRect: CGRect(x: 0.75, y: 0.75, width: 14.5, height: 10), cornerRadius: 1.8)
            ctx.fill(bezel, with: .color(Color.black.opacity(0.55)))
            ctx.stroke(bezel, with: .color(bezelColor), lineWidth: 1.3)

            let s = Self.screen
            var screenCtx = ctx
            screenCtx.clip(to: Path(s))
            switch state {
            case "wait":
                let on = (frame / 5) % 2 == 0
                screenCtx.fill(Path(s), with: .color(Palette.warn.opacity(on ? 0.9 : 0.35)))
                let mark = Color.black.opacity(on ? 0.85 : 0.6)
                screenCtx.fill(Path(roundedRect: CGRect(x: 7.3, y: 3.3, width: 1.4, height: 3.2), cornerRadius: 0.5), with: .color(mark))
                screenCtx.fill(Path(ellipseIn: CGRect(x: 7.3, y: 7, width: 1.4, height: 1.4)), with: .color(mark))
            case "busy":
                drawScript(&screenCtx, in: s)
            default:  // idle: prompt and a slow blinking cursor
                screenCtx.fill(Path(CGRect(x: s.minX + 0.8, y: s.maxY - 2.2, width: 1.6, height: 1)),
                               with: .color(Palette.dim))
                if (frame / 8) % 2 == 0 {
                    screenCtx.fill(Path(CGRect(x: s.minX + 3, y: s.maxY - 2.6, width: 1.3, height: 1.8)),
                                   with: .color(Palette.dim))
                }
            }
        }
        .frame(width: width, height: width * 14 / 16)
    }

    /// Script lines of pseudo-random indent, length and colour scroll upward;
    /// the newest line at the bottom is typed out with a cursor at its end.
    private func drawScript(_ ctx: inout GraphicsContext, in s: CGRect) {
        let colors = [tint, tint, Palette.accent, Palette.sub, Color.white.opacity(0.8)]
        let step = frame / Self.framesPerLine          // index of the line being typed
        let typed = CGFloat(frame % Self.framesPerLine + 1) / CGFloat(Self.framesPerLine)
        let rows = Int(s.height / Self.pitch) + 2
        for back in 0..<rows {
            let n = step - back
            let h = UInt32(truncatingIfNeeded: n &* 2_654_435_761) >> 8
            let indent = CGFloat(h % 3) * 1.5
            var length = CGFloat(2 + Int((h >> 3) % 7))
            length = min(length, s.width - 1.6 - indent)
            if back == 0 { length *= typed }
            let y = s.maxY - 1.6 - CGFloat(back) * Self.pitch  // whole-line scroll, like a terminal
            let x = s.minX + 0.8 + indent
            ctx.fill(Path(CGRect(x: x, y: y, width: max(0.5, length), height: 0.9)),
                     with: .color(colors[Int((h >> 6) % UInt32(colors.count))]))
            if back == 0, (frame / 2) % 2 == 0 {  // cursor
                ctx.fill(Path(CGRect(x: x + length + 0.4, y: y - 0.4, width: 0.9, height: 1.6)), with: .color(.white))
            }
        }
    }
}

/// Status glyph shared by sessions and projects: a monitor for open sessions, a dot otherwise.
struct StateGlyph: View {
    let state: String?
    let frame: Int

    var body: some View {
        Group {
            switch state {
            case "busy", "wait", "idle":
                MonitorIcon(state: state!, frame: frame)
            case nil:
                Text("·").foregroundColor(Palette.dim)
            default:
                Text("!").foregroundColor(Palette.warn)
            }
        }
        .font(.system(size: 11, weight: .bold, design: .monospaced))
        .frame(width: 16)
    }
}

/// 24 thin bars, one per hour of today; future hours are faint dots.
struct HourStrip: View {
    let values: [Double]
    let nowHour: Int
    let scale: Double  // value that fills a bar
    var color: Color = Palette.accent
    var height: CGFloat = 12

    var body: some View {
        Canvas { ctx, size in
            let n = 24
            let w = size.width / CGFloat(n)
            for h in 0..<n {
                let x = CGFloat(h) * w
                if h > nowHour {
                    let dot = CGRect(x: x + w / 2 - 0.75, y: size.height - 1.5, width: 1.5, height: 1.5)
                    ctx.fill(Path(ellipseIn: dot), with: .color(Palette.faint))
                    continue
                }
                let v = h < values.count ? values[h] : 0
                let frac = scale > 0 ? min(1, v / scale) : 0
                let bh = v > 0 ? max(2, CGFloat(frac) * size.height) : 1
                let r = CGRect(x: x + 0.5, y: size.height - bh, width: max(1, w - 1), height: bh)
                ctx.fill(Path(roundedRect: r, cornerRadius: 0.75), with: .color(v > 0 ? color : Palette.faint))
            }
        }
        .frame(height: height)
    }
}

/// Plan-limit bar with a marker for how far through the window we are.
struct LimitBar: View {
    let used: Double
    let elapsed: Double?
    let frame: Int

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.faint)
                Capsule().fill(Palette.level(used))
                    .frame(width: max(used > 0 ? 3 : 0, g.size.width * CGFloat(min(used, 100) / 100)))
                if let e = elapsed {
                    Rectangle().fill(Palette.accent)
                        .frame(width: 2, height: g.size.height + 4)
                        .offset(x: max(0, g.size.width * CGFloat(e) - 1))
                        .opacity((frame / 8) % 4 == 0 ? 0.5 : 1)
                }
            }
        }
        .frame(height: 6)
    }
}

// MARK: - Sections

struct Header: View {
    let snap: Snapshot?
    let frame: Int
    @Binding var compact: Bool

    var body: some View {
        let sessions = snap?.sessions ?? []
        let working = sessions.filter { $0.state == "busy" }.count
        let waiting = sessions.filter { $0.state == "wait" }.count
        HStack(spacing: 6) {
            MonitorIcon(state: waiting > 0 ? "wait" : working > 0 ? "busy" : "idle", frame: frame, width: 22)
            Text("CLAUDE")
                .font(.system(size: 11, weight: .heavy, design: .rounded))
                .foregroundColor(Palette.accent)
                .tracking(1.5)
            Text("\(sessions.count) session\(sessions.count == 1 ? "" : "s") · \(working) working")
                .font(.system(size: 10.5))
                .foregroundColor(Palette.dim)
                .lineLimit(1)
            Spacer(minLength: 4)
            if waiting > 0 {
                Text(compact ? "▲ \(waiting)" : "▲ \(waiting) need\(waiting == 1 ? "s" : "") you")
                    .font(.system(size: 10, weight: .bold))
                    .lineLimit(1)
                    .fixedSize()
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Palette.warn.opacity((frame / 6) % 2 == 0 ? 0.95 : 0.6)))
                    .foregroundColor(.black)
            }
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { compact.toggle() }
            } label: {
                Image(systemName: compact ? "chevron.down" : "chevron.up")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundColor(Palette.dim)
                    .frame(width: 16, height: 16)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(compact ? "Expand" : "Compact")
        }
    }
}

struct SectionTitle: View {
    let title: String
    var trailing: String = ""

    var body: some View {
        HStack {
            Text(title)
                .font(.system(size: 9, weight: .bold))
                .tracking(1.2)
                .foregroundColor(Palette.dim)
            Spacer()
            if !trailing.isEmpty {
                Text(trailing).font(.system(size: 9.5)).foregroundColor(Palette.dim).monospacedDigit()
            }
        }
        .padding(.top, 2)
    }
}

/// Context-window fill: a short bar and its percentage, green / yellow / red like the dashboard.
struct CtxGauge: View {
    let pct: Double

    var body: some View {
        HStack(spacing: 3) {
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.faint)
                Capsule().fill(Palette.level(pct)).frame(width: max(2, 28 * CGFloat(min(pct, 100)) / 100))
            }
            .frame(width: 28, height: 4)
            Text("\(Int(pct.rounded()))%")
                .font(.system(size: 9.5, weight: pct >= 80 ? .bold : .regular))
                .foregroundColor(pct >= 80 ? Palette.level(pct) : Palette.dim)
                .monospacedDigit()
        }
        .help("Context window \(Int(pct.rounded()))% full" + (pct >= 80 ? " · time to /compact" : ""))
    }
}

/// The session row under the pointer, for the right-click menu (see AppDelegate).
enum HoverTarget {
    static var session: Sess?
}

struct SessionRow: View {
    let s: Sess
    let frame: Int
    let compact: Bool
    @State private var hovering = false

    var stateColor: Color {
        switch s.state {
        case "busy": return Palette.busy
        case "wait": return Palette.warn
        case "idle": return Palette.dim
        default: return Palette.warn
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                StateGlyph(state: s.state, frame: frame)
                Text(s.title?.isEmpty == false ? s.title! : s.name)
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundColor(s.state == "idle" ? .primary.opacity(0.7) : .primary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 4)
                if let ctx = s.ctx, !compact || ctx >= 80 {  // compact: only as a warning
                    CtxGauge(pct: ctx)
                }
                Text(s.elapsed ?? "")
                    .font(.system(size: 10))
                    .foregroundColor(stateColor)
                    .monospacedDigit()
            }
            HStack(spacing: 4) {
                Text(s.doing ?? "")
                    .font(.system(size: 10, weight: s.state == "idle" ? .regular : .medium))
                    .foregroundColor(s.state == "idle" ? Palette.dim : stateColor)
                    .lineLimit(1)
                    .layoutPriority(1)
                Text(s.detail ?? "")
                    .font(.system(size: 10))
                    .foregroundColor(Palette.dim)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 0)
                if !compact {
                    if let subs = s.subs, !subs.isEmpty {
                        Text("◆ \(subs.count)").font(.system(size: 9.5, weight: .bold)).foregroundColor(Palette.sub)
                    }
                    Text("▸ \(s.project ?? "")")
                        .font(.system(size: 9.5))
                        .foregroundColor(Palette.accent.opacity(0.8))
                        .lineLimit(1)
                }
            }
            .padding(.leading, 21)
            if !compact {
                ForEach(Array((s.subs ?? []).prefix(2).enumerated()), id: \.offset) { i, desc in
                    HStack(spacing: 4) {
                        Text("└")
                        MonitorIcon(state: "busy", frame: frame + i * 3, tint: Palette.sub, width: 12)
                        Text(desc).lineLimit(1)
                    }
                    .font(.system(size: 9.5))
                    .foregroundColor(Palette.sub)
                    .padding(.leading, 21)
                }
            }
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 6)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(s.state == "wait" ? Palette.warn.opacity((frame / 6) % 2 == 0 ? 0.16 : 0.08)
                      : Color.primary.opacity(hovering ? 0.1 : 0.05))
        )
        .contentShape(RoundedRectangle(cornerRadius: 8))
        .onHover { inside in  // click jumps to the session's window
            if inside {
                HoverTarget.session = s
            } else if HoverTarget.session?.pid == s.pid {
                HoverTarget.session = nil
            }
            guard s.pid != nil, inside != hovering else { return }
            hovering = inside
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
        .onDisappear {
            if hovering { NSCursor.pop() }
            if HoverTarget.session?.pid == s.pid { HoverTarget.session = nil }
        }
        .onTapGesture {
            if let pid = s.pid { Feed.focus(pid: pid) }
        }
        .help(tooltip)
    }

    private var tooltip: String {
        var facts = [s.model, s.uptime.map { "up \($0)" }].compactMap { $0?.isEmpty == false ? $0 : nil }
        if let ctx = s.ctx { facts.append("ctx \(Int(ctx.rounded()))%") }
        if let cost = s.cost { facts.append(String(format: "≈$%.2f", cost)) }
        var lines = [facts.joined(separator: " · ")].filter { !$0.isEmpty }
        if s.pid != nil { lines.append("Click to show this session's window · right-click for more") }
        return lines.joined(separator: "\n")
    }
}

struct UsageSection: View {
    let snap: Snapshot
    let frame: Int
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if !compact { SectionTitle(title: "USAGE", trailing: snap.limitsStale ?? "") }
            if snap.limits.isEmpty {
                Text("waiting for a status line update")
                    .font(.system(size: 10)).foregroundColor(Palette.dim)
            }
            ForEach(snap.limits, id: \.label) { lim in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(lim.label)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(.primary.opacity(0.85))
                            .frame(width: 44, alignment: .leading)
                        LimitBar(used: lim.used, elapsed: lim.elapsed, frame: frame)
                        Text("\(Int(lim.used.rounded()))%")
                            .font(.system(size: 10.5, weight: .bold))
                            .foregroundColor(Palette.level(lim.used))
                            .monospacedDigit()
                            .frame(width: 34, alignment: .trailing)
                    }
                    if !compact {
                        HStack(spacing: 4) {
                            Text("resets \(lim.resets ?? "")").foregroundColor(Palette.dim)
                            Spacer(minLength: 4)
                            Text(lim.pace ?? "").foregroundColor(Palette.pace(lim.level))
                        }
                        .font(.system(size: 9.5))
                        .lineLimit(1)
                        .padding(.leading, 50)
                    }
                }
            }
            if let t = snap.today, !compact {
                HStack(alignment: .bottom, spacing: 8) {
                    HourStrip(values: t.hourly.map(Double.init), nowHour: t.nowHour,
                              scale: Double(t.hourly.max() ?? 1), height: 18)
                        .frame(width: 96)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 3) {
                            Text(fmtTokens(t.output)).fontWeight(.bold).foregroundColor(Palette.accent)
                            Text("out ·").foregroundColor(Palette.dim)
                            Text(fmtTokens(t.processed)).fontWeight(.semibold)
                            Text("processed").foregroundColor(Palette.dim)
                        }
                        HStack(spacing: 3) {
                            Text(String(format: "≈$%.2f", t.cost)).fontWeight(.bold)
                            Text("today ·").foregroundColor(Palette.dim)
                            Text("\(t.replies)").fontWeight(.semibold)
                            Text("replies ·").foregroundColor(Palette.dim)
                            Text("\(Int((t.hit * 100).rounded()))% cache").foregroundColor(Palette.dim)
                        }
                    }
                    .font(.system(size: 10))
                    .monospacedDigit()
                    .lineLimit(1)
                }
                .padding(.top, 2)
            }
        }
    }
}

struct ProjectsSection: View {
    let projects: Projects
    let nowHour: Int
    let frame: Int
    let compact: Bool

    var body: some View {
        let limit = compact ? 3 : 6
        let shown = Array(projects.items.prefix(limit))
        VStack(alignment: .leading, spacing: 3) {
            SectionTitle(title: "PROJECTS TODAY",
                         trailing: "\(projects.active) active" + (projects.first?.isEmpty == false ? " · since \(projects.first!)" : ""))
            ForEach(shown, id: \.name) { p in
                HStack(spacing: 5) {
                    StateGlyph(state: p.state, frame: frame)
                    Text(p.name)
                        .font(.system(size: 11, weight: p.state == nil ? .regular : .semibold))
                        .foregroundColor(p.state == nil ? .primary.opacity(0.75) : .primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if !compact {
                        HourStrip(values: p.hourly, nowHour: nowHour, scale: 3600, height: 10)
                            .frame(width: 60)
                    }
                    Text(p.time)
                        .font(.system(size: 10.5, weight: .semibold))
                        .monospacedDigit()
                        .frame(width: 42, alignment: .trailing)
                    if !compact {
                        Group {
                            if p.added + p.removed > 0 {
                                HStack(spacing: 2) {
                                    Text("+\(fmtCount(p.added))").foregroundColor(Palette.busy)
                                    Text("−\(fmtCount(p.removed))").foregroundColor(Palette.danger)
                                }
                            } else {
                                Text("\(p.prompts) prompt\(p.prompts == 1 ? "" : "s")").foregroundColor(Palette.dim)
                            }
                        }
                        .font(.system(size: 9.5))
                        .monospacedDigit()
                        .lineLimit(1)
                        .frame(width: 66, alignment: .trailing)
                    }
                }
                .help("\(p.name)\(p.branch.map { $0.isEmpty ? "" : " [\($0)]" } ?? "")\n"
                      + "\(p.time) spent · \(p.sessions) session\(p.sessions == 1 ? "" : "s") · \(p.prompts) prompts\n"
                      + "\(p.files) files · +\(p.added) −\(p.removed) lines · \(p.commits) commits"
                      + (p.cost.map { String(format: " · ≈$%.2f", $0) } ?? ""))
            }
            if projects.items.count > shown.count {
                Text("+\(projects.items.count - shown.count) more")
                    .font(.system(size: 9.5)).foregroundColor(Palette.dim).padding(.leading, 21)
            }
            if projects.items.isEmpty {
                Text("no Claude activity yet today").font(.system(size: 10)).foregroundColor(Palette.dim)
            }
        }
    }
}

/// Shown while the hook or the status line capture is missing, e.g. on a new Mac.
struct SetupPrompt: View {
    let setup: Setup

    var body: some View {
        let what = setup.hooks ? "Plan limits aren't set up"
            : setup.statusline ? "“Needs you” alerts aren't set up"
            : "“Needs you” alerts and limits aren't set up"
        HStack(spacing: 6) {
            Image(systemName: "wrench.and.screwdriver").foregroundColor(Palette.warn)
            Text(what).foregroundColor(.primary.opacity(0.8)).lineLimit(1)
            Spacer(minLength: 4)
            Button {
                NSApp.sendAction(#selector(AppDelegate.runSetup), to: NSApp.delegate, from: nil)
            } label: {
                Text("Set up…").font(.system(size: 10, weight: .semibold)).foregroundColor(Palette.accent)
            }
            .buttonStyle(.plain)
            .help("Add the ccwatch hook and status line capture to ~/.claude (shows the changes first)")
        }
        .font(.system(size: 10))
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 8).fill(Palette.warn.opacity(0.12)))
    }
}

// MARK: - Widget

/// Whether a host window is on screen; while it isn't, the widget stops animating.
final class Visibility: ObservableObject {
    @Published var visible: Bool
    init(_ visible: Bool) { self.visible = visible }
}

struct WidgetView: View {
    @ObservedObject var feed: Feed
    @ObservedObject var visibility: Visibility
    @AppStorage("compact") var compact = false
    var chrome = true  // false inside the menu bar popover, which brings its own frame
    var onSize: (CGSize) -> Void

    var body: some View {
        // 10 fps while something is working or waiting, 2 fps when all is idle, none while hidden
        let animating = feed.snap?.sessions.contains { $0.state == "busy" || $0.state == "wait" } ?? true
        TimelineView(.animation(minimumInterval: animating ? 0.1 : 0.5, paused: !visibility.visible)) { tl in
            let frame = Int(tl.date.timeIntervalSinceReferenceDate * 10)
            content(frame: frame)
                .frame(width: compact ? 300 : 370)
                .fixedSize(horizontal: false, vertical: true)
                .onGeometryChange(for: CGSize.self) { $0.size } action: { onSize($0) }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    func content(frame: Int) -> some View {
        let snap = feed.snap
        VStack(alignment: .leading, spacing: 7) {
            Header(snap: snap, frame: frame, compact: $compact)
            if let setup = snap?.setup, !(setup.hooks && setup.statusline) {
                SetupPrompt(setup: setup)
            }
            if let snap = snap {
                let sessions = compact
                    ? Array(snap.sessions.filter { $0.state != "idle" }.prefix(4))
                    : Array(snap.sessions.prefix(6))
                if snap.sessions.isEmpty {
                    Text("No Claude sessions running" + String(repeating: ".", count: (frame / 6) % 4))
                        .font(.system(size: 10.5)).foregroundColor(Palette.dim)
                }
                ForEach(Array(sessions.enumerated()), id: \.offset) { _, s in
                    SessionRow(s: s, frame: frame, compact: compact)
                }
                if snap.sessions.count > sessions.count {
                    Text("+\(snap.sessions.count - sessions.count) more\(compact ? " (idle hidden)" : "")")
                        .font(.system(size: 9.5)).foregroundColor(Palette.dim).padding(.leading, 6)
                }
                Divider().overlay(Palette.faint)
                UsageSection(snap: snap, frame: frame, compact: compact)
                if let projects = snap.projects {
                    Divider().overlay(Palette.faint)
                    ProjectsSection(projects: projects, nowHour: snap.today?.nowHour ?? 23, frame: frame, compact: compact)
                }
            } else if feed.problem == nil {
                Text("scanning today's Claude transcripts…")
                    .font(.system(size: 10.5)).foregroundColor(Palette.dim)
            }
            if let problem = feed.problem {
                Text(problem).font(.system(size: 9.5)).foregroundColor(Palette.warn).lineLimit(3)
            } else if feed.snap != nil, Date().timeIntervalSince(feed.updated) > 10 {
                Text("no update for \(Int(Date().timeIntervalSince(feed.updated)))s")
                    .font(.system(size: 9.5)).foregroundColor(Palette.warn)
            }
        }
        .padding(12)
        .background(Palette.background)
        .clipShape(RoundedRectangle(cornerRadius: chrome ? 16 : 0, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(chrome ? Palette.faint : .clear, lineWidth: 1))
        .foregroundColor(.primary)
    }
}

// MARK: - About

/// Author and version, baked into Info.plist and Contents/Resources by build-widget.sh:
/// CCAuthorName / CCAuthorEmail (from git config, or AUTHOR_NAME / AUTHOR_EMAIL),
/// avatar.png (copied from the project folder) and the VERSION file.
struct AboutView: View {
    private let info = Bundle.main.infoDictionary ?? [:]
    private var name: String { info["CCAuthorName"] as? String ?? NSFullUserName() }
    private var email: String? { (info["CCAuthorEmail"] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    private var version: String { info["CFBundleShortVersionString"] as? String ?? "?" }
    /// Packaged apps carry their own ccwatch.py; dev builds run the one beside them.
    private var packaged: Bool { Bundle.main.url(forResource: "ccwatch", withExtension: "py") != nil }
    private var avatar: NSImage? { Bundle.main.url(forResource: "avatar", withExtension: "png").flatMap(NSImage.init(contentsOf:)) }

    var body: some View {
        VStack(spacing: 10) {
            Group {
                if let avatar = avatar {
                    Image(nsImage: avatar).resizable().scaledToFill()
                        .background(Color.white)  // transparent avatars stay legible in Dark mode
                } else {
                    Text(name.split(separator: " ").prefix(2).compactMap(\.first).map(String.init).joined())
                        .font(.system(size: 30, weight: .semibold, design: .rounded))
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Palette.accent)
                }
            }
            .frame(width: 88, height: 88)
            .clipShape(Circle())
            .overlay(Circle().stroke(Palette.faint, lineWidth: 1))
            .padding(.top, 6)

            Text(name).font(.system(size: 15, weight: .semibold))
            if let email = email, let url = URL(string: "mailto:\(email)") {
                Text(email)
                    .font(.system(size: 12))
                    .foregroundColor(Palette.accent)
                    .onTapGesture { NSWorkspace.shared.open(url) }
                    .onHover { inside in if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() } }
                    .help("Send an email")
            }

            Divider().padding(.horizontal, 20).padding(.vertical, 2)

            Text("ccwidget").font(.system(size: 12, weight: .heavy, design: .rounded)).foregroundColor(Palette.accent)
            Text("Claude Code command center").font(.system(size: 11)).foregroundColor(Palette.dim)
            Text("Version \(version)" + (packaged ? "" : " · dev build"))
                .font(.system(size: 11, design: .monospaced))
                .foregroundColor(Palette.dim)
                .textSelection(.enabled)
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 20)
        .frame(width: 280)
        .background(Palette.background)
    }
}

// MARK: - Hovering assistant

/// What the hovering assistant shows; AppDelegate fills it from each snapshot.
final class AssistantModel: ObservableObject {
    @Published var waiting: [Sess] = []
    @Published var shown = false  // false while the panel is hidden, so the animation stops
    @Published var leaving = false  // flying off: full thrust, no floor shadow
}

/// The assistant's head: antenna with a pulsing light, metal head, dark visor with
/// eyes that glance down at the card and blink every few seconds. Drawn on an
/// 84 x 66 grid; the neck runs down behind the card the robot holds.
struct RobotHead: View {
    let t: Double  // seconds; drives the blink and the antenna light
    var happy = false  // ^ ^ eyes once nothing needs you

    var body: some View {
        Canvas { ctx, size in
            ctx.scaleBy(x: size.width / 84, y: size.height / 66)
            let outline = Color(white: 0.3)
            let glow = Color(nsColor: .systemTeal)
            // antenna, its light pulsing like the menu bar dot
            ctx.fill(Path(CGRect(x: 40.8, y: 7, width: 2.4, height: 9)), with: .color(outline))
            let pulse = 0.5 + 0.5 * sin(t * 2 * .pi / 1.2)
            let bulb = CGRect(x: 37, y: 0, width: 10, height: 10)
            ctx.fill(Path(ellipseIn: bulb.insetBy(dx: -4, dy: -4)), with: .color(Color(nsColor: .systemOrange).opacity(0.35 * pulse)))
            ctx.fill(Path(ellipseIn: bulb), with: .color(Color(nsColor: .systemOrange).opacity(0.55 + 0.45 * pulse)))
            ctx.stroke(Path(ellipseIn: bulb), with: .color(outline), lineWidth: 1.2)
            // neck, ears, head
            ctx.fill(Path(CGRect(x: 35, y: 56, width: 14, height: 10)), with: .color(Color(white: 0.6)))
            for x in [5.5, 72.5] {
                let ear = Path(roundedRect: CGRect(x: x, y: 27, width: 6, height: 17), cornerRadius: 3)
                ctx.fill(ear, with: .color(Color(white: 0.72)))
                ctx.stroke(ear, with: .color(outline), lineWidth: 1.2)
            }
            let head = Path(roundedRect: CGRect(x: 11, y: 15, width: 62, height: 44), cornerRadius: 14)
            ctx.fill(head, with: .linearGradient(Gradient(colors: [Color(white: 0.97), Color(white: 0.74)]),
                                                 startPoint: CGPoint(x: 42, y: 15), endPoint: CGPoint(x: 42, y: 59)))
            ctx.stroke(head, with: .color(outline), lineWidth: 1.5)
            let visor = Path(roundedRect: CGRect(x: 18, y: 22, width: 48, height: 29), cornerRadius: 9)
            ctx.fill(visor, with: .color(Color(red: 0.11, green: 0.14, blue: 0.19)))
            if happy {
                for x in [32.0, 52.0] {
                    var eye = Path()
                    eye.move(to: CGPoint(x: x - 4.5, y: 36))
                    eye.addQuadCurve(to: CGPoint(x: x + 4.5, y: 36), control: CGPoint(x: x, y: 27))
                    ctx.stroke(eye, with: .color(glow.opacity(0.3)), style: StrokeStyle(lineWidth: 5, lineCap: .round))
                    ctx.stroke(eye, with: .color(glow), style: StrokeStyle(lineWidth: 2.4, lineCap: .round))
                }
                var grin = Path()
                grin.move(to: CGPoint(x: 35, y: 42))
                grin.addQuadCurve(to: CGPoint(x: 49, y: 42), control: CGPoint(x: 42, y: 49))
                ctx.stroke(grin, with: .color(glow), style: StrokeStyle(lineWidth: 2, lineCap: .round))
                return
            }
            // eyes, looking down at the card; a quick blink every 3.7 s
            let eyeH = t.truncatingRemainder(dividingBy: 3.7) < 0.13 ? 1.2 : 8.0
            for x in [32.0, 52.0] {
                let eye = CGRect(x: x - 3.5, y: 34 - eyeH / 2, width: 7, height: eyeH)
                ctx.fill(Path(ellipseIn: eye.insetBy(dx: -2.5, dy: -2.5)), with: .color(glow.opacity(0.25)))
                ctx.fill(Path(roundedRect: eye, cornerRadius: min(3.5, eyeH / 2)), with: .color(glow))
            }
            var mouth = Path()
            mouth.move(to: CGPoint(x: 37, y: 43))
            mouth.addQuadCurve(to: CGPoint(x: 47, y: 43), control: CGPoint(x: 42, y: 47))
            ctx.stroke(mouth, with: .color(glow), style: StrokeStyle(lineWidth: 1.8, lineCap: .round))
        }
    }
}

/// A robot mitt gripping the top edge of the card.
struct RobotHand: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 5)
            .fill(LinearGradient(colors: [Color(white: 0.95), Color(white: 0.72)], startPoint: .top, endPoint: .bottom))
            .overlay(RoundedRectangle(cornerRadius: 5).stroke(Color(white: 0.3), lineWidth: 1.2))
            .overlay(HStack(spacing: 3.5) {  // fingers
                ForEach(0..<3, id: \.self) { _ in Capsule().fill(Color(white: 0.3)).frame(width: 1, height: 5) }
            }.offset(y: 2))
            .frame(width: 18, height: 14)
    }
}

/// The thruster flame that keeps the robot in the air; it flickers, and burns
/// twice as long at full thrust when the robot flies off.
struct Thruster: View {
    let t: Double
    var boost = false

    var body: some View {
        let flicker = 0.75 + 0.25 * sin(t * 37) * sin(t * 23)
        Canvas { ctx, size in
            var flame = Path()
            let w = size.width, h = size.height * (boost ? 1 : 0.55) * flicker
            flame.move(to: CGPoint(x: 0, y: 0))
            flame.addQuadCurve(to: CGPoint(x: w / 2, y: h), control: CGPoint(x: w * 0.1, y: h * 0.6))
            flame.addQuadCurve(to: CGPoint(x: w, y: 0), control: CGPoint(x: w * 0.9, y: h * 0.6))
            ctx.fill(flame, with: .linearGradient(
                Gradient(colors: [Color(nsColor: .systemTeal), Color(nsColor: .systemTeal).opacity(0)]),
                startPoint: .zero, endPoint: CGPoint(x: 0, y: h)))
        }
        .frame(width: boost ? 30 : 26, height: 40)
    }
}

/// Clippy for Claude Code: a robot that hovers on the desktop, holding a card of
/// the sessions waiting on you (a permission prompt, a question). Click one to
/// bring its terminal or editor window forward; × hides it until another session
/// needs you. AppDelegate shows it only while something is waiting.
struct AssistantView: View {
    @ObservedObject var model: AssistantModel
    var onDismiss: () -> Void = {}
    var onSize: (CGSize) -> Void = { _ in }

    static let cardWidth: CGFloat = 290

    var body: some View {
        Group {
            if model.shown {
                TimelineView(.animation(minimumInterval: 1.0 / 30)) { tl in
                    content(t: tl.date.timeIntervalSinceReferenceDate)
                }
            }
        }
        .fixedSize()
        .onGeometryChange(for: CGSize.self) { $0.size } action: { onSize($0) }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
    }

    func content(t: Double) -> some View {
        let bob = sin(t * 2 * .pi / 2.6) * 4  // the hover: up and down every 2.6 s
        return VStack(spacing: 0) {
            ZStack(alignment: .top) {
                RoundedRectangle(cornerRadius: 12)  // shoulders; the rest of the body is behind the card
                    .fill(LinearGradient(colors: [Color(white: 0.93), Color(white: 0.7)], startPoint: .top, endPoint: .bottom))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color(white: 0.3), lineWidth: 1.5))
                    .frame(width: 128, height: 40)
                    .padding(.top, 60)
                RobotHead(t: t, happy: model.waiting.isEmpty)
                    .frame(width: 84, height: 66)
                    .rotationEffect(.degrees(sin(t * 0.9) * 4), anchor: .bottom)
                VStack(spacing: -8) {
                    card(frame: Int(t * 10))
                    Thruster(t: t, boost: model.leaving).zIndex(-1)
                }
                .padding(.top, 72)
                HStack {
                    RobotHand()
                    Spacer()
                    RobotHand()
                }
                .frame(width: 136)
                .padding(.top, 66)
            }
            .offset(y: bob)
            Ellipse()  // shadow on the "floor", tighter and darker as the robot dips
                .fill(Color.black.opacity(0.16 - bob * 0.015))
                .frame(width: 150 - bob * 5, height: 9)
                .blur(radius: 2)
                .opacity(model.leaving ? 0 : 1)
                .animation(.easeOut(duration: 0.3), value: model.leaving)
                .padding(.top, 4)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(width: Self.cardWidth + 24)
    }

    func card(frame: Int) -> some View {
        let waiting = model.waiting
        let shown = Array(waiting.prefix(4))
        return VStack(alignment: .leading, spacing: 5) {
            if waiting.isEmpty {  // the last one was just answered; the robot is about to fly off
                HStack(spacing: 7) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 18))
                        .foregroundColor(Palette.busy)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("All caught up").font(.system(size: 12.5, weight: .semibold))
                        Text("Nothing needs you right now").font(.system(size: 10)).foregroundColor(Palette.dim)
                    }
                }
                .padding(.vertical, 2)
            } else {
                HStack(spacing: 6) {
                    Text("NEEDS YOU")
                        .font(.system(size: 10.5, weight: .heavy, design: .rounded))
                        .tracking(1.3)
                        .foregroundColor(Palette.warn)
                    Text("\(waiting.count)")
                        .font(.system(size: 10, weight: .bold))
                        .monospacedDigit()
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(Palette.warn.opacity((frame / 6) % 2 == 0 ? 0.95 : 0.6)))
                        .foregroundColor(.black)
                    Spacer()
                    Button(action: onDismiss) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundColor(Palette.dim)
                            .frame(width: 16, height: 16)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Hide until another session needs you")
                }
                ForEach(Array(shown.enumerated()), id: \.offset) { _, s in
                    SessionRow(s: s, frame: frame, compact: false)
                }
                if waiting.count > shown.count {
                    Text("+\(waiting.count - shown.count) more").font(.system(size: 9.5)).foregroundColor(Palette.dim)
                }
                Text("Click a session to jump to its window")
                    .font(.system(size: 9.5))
                    .foregroundColor(Palette.dim)
            }
        }
        .padding(.horizontal, 10)
        .padding(.top, 14)  // clear of the hands
        .padding(.bottom, 10)
        .frame(width: Self.cardWidth, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Palette.background))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke((waiting.isEmpty ? Palette.busy : Palette.warn).opacity(0.7), lineWidth: 1.5))
        .shadow(color: .black.opacity(0.25), radius: 8, y: 3)
        .foregroundColor(.primary)
    }
}

// MARK: - Notifications

/// "45s", "5m12s", "1h05m"
func fmtDuration(_ secs: Double) -> String {
    let s = Int(max(0, secs))
    if s < 60 { return "\(s)s" }
    if s < 3600 { return String(format: "%dm%02ds", s / 60, s % 60) }
    return String(format: "%dh%02dm", s / 3600, s % 3600 / 60)
}

/// macOS notifications from the snapshots: a long turn finished, a session needs
/// you (off by default; the hovering assistant covers that), a plan limit
/// crossed 80%, 95% or 100%. Clicking one about a session jumps to its window.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    enum Kind: String, CaseIterable {
        case finished = "notifyFinished", waiting = "notifyWaiting", limits = "notifyLimits"

        var title: String {
            switch self {
            case .finished: return "When a Long Turn Finishes"
            case .waiting: return "When a Session Needs You"
            case .limits: return "Plan Limit Warnings"
            }
        }

        var enabled: Bool { UserDefaults.standard.object(forKey: rawValue) as? Bool ?? (self != .waiting) }
    }

    /// Seconds a turn must run before its end is worth a notification.
    static var finishedAfter: Double { UserDefaults.standard.object(forKey: "notifyAfter") as? Double ?? 120 }
    static let thresholds = [100, 95, 80]

    private var watch: AnyCancellable?
    private var last: [Int: (state: String, secs: Double)] = [:]  // by pid, from the previous snapshot
    private var primed = false  // the first snapshot only sets the baseline

    init(feed: Feed) {
        super.init()
        UNUserNotificationCenter.current().delegate = self
        watch = feed.$snap.receive(on: RunLoop.main).sink { [weak self] in self?.update($0) }
    }

    private func update(_ snap: Snapshot?) {
        guard let snap = snap else { return }
        var now: [Int: (state: String, secs: Double)] = [:]
        for s in snap.sessions {
            guard let pid = s.pid else { continue }
            now[pid] = (s.state, s.stateSecs ?? 0)
            guard primed, let prev = last[pid] else { continue }
            let title = s.title?.isEmpty == false ? s.title! : s.name
            if prev.state == "busy", s.state == "idle", Kind.finished.enabled, prev.secs >= Self.finishedAfter {
                post(id: "finished-\(pid)", title: "Finished: \(title)",
                     body: "\(s.project ?? s.name) · worked \(fmtDuration(prev.secs))", pid: pid)
            }
            if prev.state != "wait", s.state == "wait", Kind.waiting.enabled {
                post(id: "waiting-\(pid)", title: "Needs you: \(title)",
                     body: [s.doing, s.detail].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "),
                     pid: pid)
            }
        }
        last = now
        primed = true
        checkLimits(snap.limits)
    }

    /// Each threshold fires once per window; what has fired is kept across launches.
    private func checkLimits(_ limits: [Limit]) {
        let d = UserDefaults.standard
        let nowEpoch = Date().timeIntervalSince1970
        var fired = (d.dictionary(forKey: "limitAlerts") as? [String: Int] ?? [:])
            .filter { (Double($0.key.split(separator: "@").last ?? "") ?? 0) * 3600 > nowEpoch - 3600 }  // drop past windows
        for l in limits {
            guard let key = l.key, let at = l.resetsAt,
                  let hit = Self.thresholds.first(where: { l.used >= Double($0) }) else { continue }
            let id = "\(key)@\(Int(at / 3600))"  // the hour of the reset, in case resets_at jitters
            guard hit > fired[id] ?? 0 else { continue }
            fired[id] = hit
            guard Kind.limits.enabled else { continue }
            let resets = AppDelegate.untilReset(l).map { "resets in \($0)" } ?? ""
            post(id: "limit-\(id)", title: hit >= 100 ? "\(l.label.capitalized) limit reached" : "\(l.label.capitalized) limit \(Int(l.used))% used",
                 body: [resets, l.pace ?? ""].filter { !$0.isEmpty }.joined(separator: " · "), pid: nil)
        }
        d.set(fired, forKey: "limitAlerts")
    }

    /// Asks for permission the first time; after that macOS remembers the answer.
    private func post(id: String, title: String, body: String, pid: Int?) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if let pid = pid { content.userInfo = ["pid": pid] }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { ok, _ in
            guard ok else { return }
            center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let pid = response.notification.request.content.userInfo["pid"] as? Int {
            DispatchQueue.main.async { Feed.focus(pid: pid) }
        }
        done()
    }
}

// MARK: - Global shortcuts

/// System-wide shortcuts through Carbon's RegisterEventHotKey, which needs no
/// Accessibility permission. Each is ⌃⌥⌘ plus a key.
final class HotKeys {
    private var refs: [EventHotKeyRef] = []
    private var handler: EventHandlerRef?
    private var actions: [UInt32: () -> Void] = [:]

    /// keys: (virtual key code, action)
    func register(_ keys: [(Int, () -> Void)]) {
        unregister()
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, me in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            let hotKeys = Unmanaged<HotKeys>.fromOpaque(me!).takeUnretainedValue()
            DispatchQueue.main.async { hotKeys.actions[id.id]?() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), &handler)
        for (n, (code, action)) in keys.enumerated() {
            var ref: EventHotKeyRef?
            let id = EventHotKeyID(signature: OSType(0x6363_7767), id: UInt32(n + 1))  // 'ccwg'
            // a key another app already holds just doesn't register
            if RegisterEventHotKey(UInt32(code), UInt32(controlKey | optionKey | cmdKey), id,
                                   GetApplicationEventTarget(), 0, &ref) == noErr, let ref = ref {
                refs.append(ref)
                actions[id.id] = action
            }
        }
    }

    func unregister() {
        refs.forEach { UnregisterEventHotKey($0) }
        refs = []
        actions = [:]
        if let h = handler { RemoveEventHandler(h) }
        handler = nil
    }
}

// MARK: - App


final class WidgetPanel: NSPanel {
    override var canBecomeKey: Bool { false }  // never steal keyboard focus
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let feed = Feed()
    let panelVisibility = Visibility(false)
    let popoverVisibility = Visibility(false)
    var panel: WidgetPanel!
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    private var statusKey = ""
    private var popoverClosed = Date.distantPast
    private var watch: AnyCancellable?
    private var appearanceWatch: NSKeyValueObservation?

    func applicationDidFinishLaunching(_ note: Notification) {
        panel = WidgetPanel(contentRect: NSRect(x: 0, y: 0, width: 370, height: 300),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.alphaValue = UserDefaults.standard.object(forKey: "opacity") as? Double ?? 1

        let view = WidgetView(feed: feed, visibility: panelVisibility, onSize: { [weak self] in self?.fit($0) })
        let host = NSHostingView(rootView: view)
        host.sizingOptions = []
        panel.contentView = host

        // Native right-click (or control-click) menu. A SwiftUI .contextMenu on a view
        // that redraws ten times a second, in a panel that never becomes key, drops
        // hovers and clicks on its submenus; catching the click here also means it
        // works whichever subview is under the pointer.
        NSEvent.addLocalMonitorForEvents(matching: [.rightMouseDown, .leftMouseDown]) { [weak self] event in
            guard let self = self, let window = event.window, let view = window.contentView,
                  window === self.panel || window === self.assistantPanel
                    || window === self.popover.contentViewController?.view.window,
                  event.type == .rightMouseDown || event.modifierFlags.contains(.control) else { return event }
            NSMenu.popUpContextMenu(self.buildMenu(session: HoverTarget.session), with: event, for: view)
            return nil
        }

        if !panel.setFrameUsingName("ccwidget"), let screen = NSScreen.main {  // first run: top-right corner
            let v = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: v.maxX - 370 - 16, y: v.maxY - 300 - 16))
        }
        panel.setFrameAutosaveName("ccwidget")
        if UserDefaults.standard.object(forKey: "floating") as? Bool ?? true {
            panel.orderFrontRegardless()
        }
        // covered by a full-screen app, on another display that's asleep, or hidden from the menu
        NotificationCenter.default.addObserver(forName: NSWindow.didChangeOcclusionStateNotification,
                                               object: panel, queue: .main) { [weak self] _ in
            guard let self = self else { return }
            self.panelVisibility.visible = self.panel.occlusionState.contains(.visible)
        }
        panelVisibility.visible = panel.isVisible
        setUpStatusItem()
        setUpAssistant()
        feed.start()
        notifier = Notifier(feed: feed)
        updateHotKeys()
    }

    // MARK: Notifications and shortcuts

    var notifier: Notifier?
    let hotKeys = HotKeys()

    var hotKeysOn: Bool { UserDefaults.standard.object(forKey: "hotkeys") as? Bool ?? true }

    func updateHotKeys() {
        guard hotKeysOn else { return hotKeys.unregister() }
        hotKeys.register([(kVK_ANSI_J, { [weak self] in self?.jumpToWaiting() }),
                          (kVK_ANSI_W, { [weak self] in self?.toggleFloating() })])
    }

    /// ⌃⌥⌘J: the session that has waited longest, or the popover when none is waiting.
    func jumpToWaiting() {
        let waiting = (feed.snap?.sessions ?? []).filter { $0.state == "wait" && $0.pid != nil }
        if let s = waiting.max(by: { ($0.stateSecs ?? 0) < ($1.stateSecs ?? 0) }), let pid = s.pid {
            Feed.focus(pid: pid)
        } else if let button = statusItem.button, !popover.isShown {
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    @objc func toggleHotKeys() {
        UserDefaults.standard.set(!hotKeysOn, forKey: "hotkeys")
        updateHotKeys()
    }

    @objc func toggleNotification(_ sender: NSMenuItem) {
        guard let kind = (sender.representedObject as? String).flatMap(Notifier.Kind.init(rawValue:)) else { return }
        UserDefaults.standard.set(!kind.enabled, forKey: kind.rawValue)
    }

    // MARK: Hovering assistant

    let assistant = AssistantModel()
    var assistantPanel: WidgetPanel!
    private var assistantVisible = false
    private var dismissed = Set<Int>()  // pids hidden with ×, until they stop waiting
    private var assistantWatch: AnyCancellable?

    func setUpAssistant() {
        let p = WidgetPanel(contentRect: NSRect(x: 0, y: 0, width: AssistantView.cardWidth + 24, height: 200),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = false  // the card and the floor draw their own
        p.hidesOnDeactivate = false
        let host = NSHostingView(rootView: AssistantView(model: assistant, onDismiss: { [weak self] in
            guard let self = self else { return }
            self.dismissed = Set(self.assistant.waiting.map { $0.pid ?? 0 })
            self.setAssistant(visible: false)
        }, onSize: { [weak self] in self?.fitAssistant($0) }))
        host.sizingOptions = []
        p.contentView = host
        assistantPanel = p
        if !p.setFrameUsingName("assistant"), let screen = NSScreen.main {  // first run: bottom-right corner
            let v = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: v.maxX - p.frame.width - 24, y: v.minY + 24))
        }
        if !NSScreen.screens.contains(where: { $0.visibleFrame.intersects(p.frame) }), let screen = NSScreen.main {
            let v = screen.visibleFrame  // quit mid-flight, or its display was unplugged
            p.setFrameOrigin(NSPoint(x: v.maxX - p.frame.width - 24, y: v.minY + 24))
        }
        p.setFrameAutosaveName("assistant")
        assistantWatch = feed.$snap.receive(on: RunLoop.main).sink { [weak self] in self?.updateAssistant($0) }
    }

    private var assistantSize = CGSize.zero

    /// Resize to the content, growing upward so the robot stays where it hovers.
    func fitAssistant(_ size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        assistantSize = size
        var f = assistantPanel.frame
        guard abs(f.width - size.width) > 0.5 || abs(f.height - size.height) > 0.5 else { return }
        f.size = size
        assistantPanel.setFrame(f, display: true)
    }

    /// Shown while any session waits on you, unless switched off in the menu or
    /// dismissed with × (it comes back when a different session starts waiting).
    func updateAssistant(_ snap: Snapshot?) {
        let waiting = (snap?.sessions ?? []).filter { $0.state == "wait" }
        let pids = Set(waiting.map { $0.pid ?? 0 })
        dismissed.formIntersection(pids)
        assistant.waiting = waiting
        let enabled = UserDefaults.standard.object(forKey: "assistant") as? Bool ?? true
        // a short "All caught up" when the last one was answered; otherwise straight off
        setAssistant(visible: enabled && !pids.subtracting(dismissed).isEmpty, pause: waiting.isEmpty ? 1.2 : 0)
    }

    /// Fades in where it was left. It leaves by flying up off the top of the screen,
    /// after `pause` seconds of "All caught up" when the last session was just answered.
    func setAssistant(visible: Bool, pause: Double = 0) {
        guard visible != assistantVisible else { return }
        assistantVisible = visible
        let p = assistantPanel!
        if !visible {
            DispatchQueue.main.asyncAfter(deadline: .now() + pause) { [weak self] in
                guard let self = self, !self.assistantVisible, self.flightHome == nil else { return }
                self.flyAway()
            }
            return
        }
        assistant.leaving = false
        assistant.shown = true
        if let home = flightHome {  // a session needs you mid-flight: fly back down
            flightHome = nil
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.4
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                p.animator().setFrame(NSRect(origin: home, size: p.frame.size), display: true)
                p.animator().alphaValue = 1
            }) { [weak self] in  // the card may have changed size on the way down
                guard let self = self else { return }
                self.fitAssistant(self.assistantSize)
            }
        } else if !p.isVisible {
            p.alphaValue = 0
            p.orderFrontRegardless()
            NSAnimationContext.runAnimationGroup { $0.duration = 0.3; p.animator().alphaValue = 1 }
        }  // else it was still showing "All caught up": just carry on
    }

    private var flightHome: NSPoint?  // where the robot hovered before it flew off

    /// Full thrust and lift-off: slow at first, then faster, until it's past the top
    /// of the screen. Then it's hidden and put back on its spot for next time.
    private func flyAway() {
        let p = assistantPanel!
        let home = p.frame.origin
        flightHome = home
        assistant.leaving = true
        let top = (p.screen ?? NSScreen.main)?.frame.maxY ?? home.y + 1200
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 1.1
            ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.5, 0, 0.9, 0.6)
            p.animator().setFrame(NSRect(origin: NSPoint(x: home.x, y: top + 40), size: p.frame.size), display: true)
            p.animator().alphaValue = 0.3  // gone either way if another display sits above
        }) { [weak self] in
            guard let self = self, !self.assistantVisible else { return }  // called back meanwhile
            p.orderOut(nil)
            p.setFrameOrigin(home)  // also re-saves the spot, which the flight overwrote
            p.alphaValue = 1
            self.flightHome = nil
            self.assistant.shown = false
            self.assistant.leaving = false
        }
    }

    @objc func toggleAssistant() {
        let on = !(UserDefaults.standard.object(forKey: "assistant") as? Bool ?? true)
        UserDefaults.standard.set(on, forKey: "assistant")
        dismissed = []
        updateAssistant(feed.snap)
    }

    // MARK: Menu bar icon

    func setUpStatusItem() {
        let content = NSHostingController(rootView: WidgetView(feed: feed, visibility: popoverVisibility, chrome: false, onSize: { [weak self] size in
            guard let self = self, size.width > 0, size.height > 0, self.popover.contentSize != size else { return }
            self.popover.contentSize = size
        }))
        content.sizingOptions = []
        popover.contentViewController = content
        popover.contentSize = NSSize(width: 370, height: 300)
        popover.behavior = .transient  // closes on any click outside it, e.g. after jumping to a session
        popover.delegate = self

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // Apps can't choose where their icon sits; ⌘-drag moves it, and macOS saves the
        // spot under this name (in our defaults), so it comes back there on every launch.
        statusItem.autosaveName = "ccwidget"
        if let button = statusItem.button {
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.imagePosition = .imageLeading
            // The dotted icon is drawn in the label color, so redraw it when the
            // menu bar flips between light and dark (wallpaper or system appearance).
            appearanceWatch = button.observe(\.effectiveAppearance) { [weak self] _, _ in
                DispatchQueue.main.async { self?.drawStatusIcon() }
            }
        }
        updateStatus(nil)
        watch = feed.$snap.receive(on: RunLoop.main).sink { [weak self] in self?.updateStatus($0) }
    }

    /// Menu bar robot, recoloured from the latest snapshot:
    /// - status dot: blinking orange while a session needs you, green while any is
    ///   working, none when all are idle; the count beside it is the number waiting
    ///   (orange) or working;
    /// - head colour: weekly usage (amber from 50%, red from 90%);
    /// - gauge under the head: the short (5-hour) window's usage, same colours, and
    ///   a red "↻ 1h20m" countdown beside the icon once that window is used up.
    func updateStatus(_ snap: Snapshot?) {
        guard let button = statusItem.button else { return }
        let sessions = snap?.sessions ?? []
        let working = sessions.filter { $0.state == "busy" }.count
        let waiting = sessions.filter { $0.state == "wait" }.count
        // The windows come from ccwatch with their length, so a 4- or 6-hour session
        // window works unchanged: the shortest is the session gauge, the longest
        // (plain over per-model, e.g. seven_day over seven_day_opus) colours the head.
        let timed = (snap?.limits ?? []).filter { ($0.window ?? 0) > 0 }
        let short = timed.min { $0.window! < $1.window! }
        let long = timed.filter { $0.window! > (short?.window ?? 0) }
            .max { ($0.window!, -($0.key?.count ?? 0)) < ($1.window!, -($1.key?.count ?? 0)) }
        let countdown = short.flatMap { $0.used >= 100 ? Self.untilReset($0) : nil }

        let key = "\(sessions.count)/\(working)/\(waiting)/\(long.map { Int($0.used) } ?? -1)/"
            + "\(short.map { Int($0.used) } ?? -1)/\(countdown ?? "")"
        guard key != statusKey else { return }
        statusKey = key

        statusDot = waiting > 0 ? .systemOrange : working > 0 ? .systemGreen : nil
        statusBody = UsageLevel(long?.used)
        statusGauge = short?.used
        blink?.invalidate()
        blink = nil
        blinkOn = true
        if waiting > 0 {
            blink = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
                guard let self = self else { return }
                self.blinkOn.toggle()
                self.drawStatusIcon()
            }
        }
        drawStatusIcon()

        let font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold)
        let count = waiting > 0 ? waiting : working
        let title = NSMutableAttributedString(string: count > 0 ? "\(count)" : "",
            attributes: waiting > 0 ? [.font: font, .foregroundColor: NSColor.systemOrange] : [.font: font])
        if let countdown = countdown {
            title.append(NSAttributedString(string: (count > 0 ? " " : "") + "↻\(countdown)",
                attributes: [.font: font, .foregroundColor: NSColor.systemRed]))
        }
        button.attributedTitle = title
        var tip = snap == nil ? ["ccwidget: waiting for ccwatch…"]
            : ["Claude: \(sessions.count) session\(sessions.count == 1 ? "" : "s") · \(working) working · \(waiting) need\(waiting == 1 ? "s" : "") you"]
        for l in [short, long].compactMap({ $0 }) {
            tip.append("\(l.label): \(Int(l.used))% used" + (Self.untilReset(l).map { " · resets in \($0)" } ?? ""))
        }
        button.toolTip = tip.joined(separator: "\n")
    }

    /// "48m", "1h20m" or "2d03h" until a window resets; nil when unknown or past.
    static func untilReset(_ l: Limit) -> String? {
        guard let at = l.resetsAt else { return nil }
        let mins = Int((at - Date().timeIntervalSince1970) / 60 + 0.999)
        guard mins > 0 else { return nil }
        if mins >= 1440 { return String(format: "%dd%02dh", mins / 1440, mins % 1440 / 60) }
        return mins >= 60 ? String(format: "%dh%02dm", mins / 60, mins % 60) : "\(mins)m"
    }

    private var statusDot: NSColor?
    private var statusBody = UsageLevel.normal
    private var statusGauge: Double?
    private var blink: Timer?
    private var blinkOn = true

    func drawStatusIcon() {
        let dot = statusDot.map { blinkOn ? $0 : $0.withAlphaComponent(0.3) }
        statusItem.button?.image = Self.robotIcon(dot: dot, body: statusBody, gauge: statusGauge)
    }

    /// Usage colour steps shared by the head (weekly) and the gauge (5-hour).
    enum UsageLevel {
        case normal, amber, red
        init(_ pct: Double?) {
            let p = pct ?? 0
            self = p >= 90 ? .red : p >= 50 ? .amber : .normal
        }
        func color(dark: Bool) -> NSColor {
            switch self {
            case .normal: return dark ? .white : .black
            case .amber: return dark ? NSColor(red: 1, green: 0.76, blue: 0.03, alpha: 1)
                                     : NSColor(red: 0.85, green: 0.55, blue: 0, alpha: 1)
            case .red: return .systemRed
            }
        }
    }

    /// A 20-pt robot head (cut-out eyes and mouth, ears, antenna), optionally with
    /// a status dot in the top-right corner (inside a transparent ring so it reads
    /// against the head) and a usage gauge underneath. With nothing coloured it is a
    /// template image, so macOS tints it like any menu bar icon; otherwise it is
    /// drawn black or white to match the menu bar, plus the colours.
    static func robotIcon(dot: NSColor?, body: UsageLevel = .normal, gauge: Double? = nil) -> NSImage {
        let size = NSSize(width: 20, height: 20)
        let lift: CGFloat = gauge == nil ? 1 : 2.6  // room for the gauge under the head
        let image = NSImage(size: size, flipped: false) { _ in
            let dark = NSAppearance.currentDrawing().bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            if let pct = gauge {
                let track = NSRect(x: 3, y: 0.3, width: 13, height: 1.9)
                UsageLevel.normal.color(dark: dark).withAlphaComponent(0.3).set()
                NSBezierPath(roundedRect: track, xRadius: 0.95, yRadius: 0.95).fill()
                if pct > 0 {
                    var fill = track
                    fill.size.width = max(track.height, track.width * CGFloat(min(pct, 100)) / 100)
                    UsageLevel(pct).color(dark: dark).set()
                    NSBezierPath(roundedRect: fill, xRadius: 0.95, yRadius: 0.95).fill()
                }
            }
            NSGraphicsContext.current?.saveGraphicsState()
            let shift = NSAffineTransform()
            shift.translateX(by: 0, yBy: lift)
            shift.concat()
            if dot != nil {
                let clip = NSBezierPath(rect: NSRect(x: 0, y: -lift, width: size.width, height: size.height))
                clip.appendOval(in: NSRect(x: 16 - 4.6, y: 13.5 - 4.6, width: 9.2, height: 9.2))
                clip.windingRule = .evenOdd
                clip.addClip()
            }
            body.color(dark: dark).set()
            let head = NSBezierPath(roundedRect: NSRect(x: 3, y: 2, width: 13, height: 10.5), xRadius: 3, yRadius: 3)
            head.appendOval(in: NSRect(x: 5.6, y: 6.4, width: 2.8, height: 2.8))  // eyes
            head.appendOval(in: NSRect(x: 10.6, y: 6.4, width: 2.8, height: 2.8))
            head.appendRoundedRect(NSRect(x: 7.3, y: 3.6, width: 4.4, height: 1.3), xRadius: 0.65, yRadius: 0.65)  // mouth
            head.windingRule = .evenOdd
            head.fill()
            NSBezierPath(roundedRect: NSRect(x: 1.3, y: 5, width: 1.7, height: 4), xRadius: 0.8, yRadius: 0.8).fill()  // ears
            NSBezierPath(roundedRect: NSRect(x: 16, y: 5, width: 1.7, height: 4), xRadius: 0.8, yRadius: 0.8).fill()
            NSBezierPath(rect: NSRect(x: 8.85, y: 12.5, width: 1.3, height: 2.2)).fill()  // antenna
            NSBezierPath(ovalIn: NSRect(x: 7.9, y: 14.2, width: 3.2, height: 3.2)).fill()
            NSGraphicsContext.current?.restoreGraphicsState()
            if let dot = dot {
                dot.set()
                NSBezierPath(ovalIn: NSRect(x: 16 - 3.3, y: 13.5 + lift - 3.3, width: 6.6, height: 6.6)).fill()
            }
            return true
        }
        image.isTemplate = dot == nil && body == .normal && UsageLevel(gauge) == .normal
        image.accessibilityDescription = "Claude sessions"
        return image
    }

    /// The Finder / Login Items icon: the menu bar robot, white with a green dot, on a
    /// dark rounded tile laid out on Apple's 1024-pt icon grid. Written as an .iconset
    /// by `ccwidget --iconset <dir>`, which package.sh turns into AppIcon.icns.
    static func writeIconset(to dir: String) throws {
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for pt in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let n = pt * scale
                let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: n, pixelsHigh: n, bitsPerSample: 8,
                                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
                let u = CGFloat(n) / 1024
                let tile = NSBezierPath(roundedRect: NSRect(x: 100 * u, y: 100 * u, width: 824 * u, height: 824 * u),
                                        xRadius: 185 * u, yRadius: 185 * u)
                NSGraphicsContext.saveGraphicsState()
                let shadow = NSShadow()
                shadow.shadowOffset = NSSize(width: 0, height: -10 * u)
                shadow.shadowBlurRadius = 24 * u
                shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
                shadow.set()
                NSColor(white: 0.1, alpha: 1).setFill()
                tile.fill()
                NSGraphicsContext.restoreGraphicsState()
                NSGradient(starting: NSColor(red: 0.20, green: 0.23, blue: 0.28, alpha: 1),
                           ending: NSColor(red: 0.07, green: 0.08, blue: 0.10, alpha: 1))!.draw(in: tile, angle: -90)
                let w = 600 * u, h = w
                NSAppearance(named: .darkAqua)!.performAsCurrentDrawingAppearance {
                    robotIcon(dot: .systemGreen).draw(in: NSRect(x: 512 * u - w / 2 + 10 * u, y: 512 * u - h / 2 - 6 * u, width: w, height: h))
                }
                NSGraphicsContext.restoreGraphicsState()
                let name = "icon_\(pt)x\(pt)\(scale == 2 ? "@2x" : "").png"
                try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
            }
        }
    }

    @objc func statusClicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            popover.performClose(nil)
            statusItem.menu = buildMenu()
            sender.performClick(nil)  // pops the menu up under the icon and tracks it until closed
            statusItem.menu = nil     // so the next left-click opens the popover again
        } else if popover.isShown {
            popover.performClose(nil)
        } else if Date().timeIntervalSince(popoverClosed) > 0.3 {  // not the click that just closed it

            NSApp.activate(ignoringOtherApps: true)  // lets the transient popover take clicks and close on outside ones
            popover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .minY)
        }
    }

    /// Resize to the content, keeping the top edge where it is.
    func fit(_ size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        var f = panel.frame
        guard abs(f.width - size.width) > 0.5 || abs(f.height - size.height) > 0.5 else { return }
        let top = f.maxY
        f.size = size
        f.origin.y = top - size.height
        panel.setFrame(f, display: true)
    }

    static let opacities = [100, 90, 80, 70, 60, 50]

    /// Built fresh on each right-click so the checkmarks match the current settings.
    /// Right-clicking a session row puts that session's actions at the top.
    func buildMenu(session: Sess? = nil) -> NSMenu {
        let menu = NSMenu()
        if let s = session, s.pid != nil {
            let header = NSMenuItem(title: s.title?.isEmpty == false ? s.title! : s.name, action: nil, keyEquivalent: "")
            header.isEnabled = false
            menu.addItem(header)
            let actions: [(String, Selector, Bool)] = [
                ("Jump to Window", #selector(jumpToSession(_:)), true),
                ("Copy Resume Command", #selector(copyResumeCommand(_:)), s.sid != nil && s.cwd != nil),
                ("Reveal Project in Finder", #selector(revealProject(_:)), s.cwd != nil),
                ("Reveal Transcript in Finder", #selector(revealTranscript(_:)), s.transcript != nil),
            ]
            for (title, action, enabled) in actions where enabled {
                let i = item(title, action)
                i.representedObject = s
                menu.addItem(i)
            }
            menu.addItem(.separator())
        }
        menu.addItem(item("About ccwidget", #selector(showAbout)))
        menu.addItem(.separator())
        let compact = UserDefaults.standard.bool(forKey: "compact")
        menu.addItem(item(compact ? "Expand" : "Compact", #selector(toggleCompact)))
        let opacity = NSMenuItem(title: "Opacity", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let current = Int((panel.alphaValue * 100).rounded())
        for pct in Self.opacities {
            let i = item("\(pct)%", #selector(setOpacity(_:)))
            i.tag = pct
            i.state = pct == current ? .on : .off
            sub.addItem(i)
        }
        opacity.submenu = sub
        menu.addItem(opacity)
        let floating = item("Show floating widget", #selector(toggleFloating))
        floating.state = panel.isVisible ? .on : .off
        menu.addItem(floating)
        let helper = item("Show hovering assistant", #selector(toggleAssistant))
        helper.state = UserDefaults.standard.object(forKey: "assistant") as? Bool ?? true ? .on : .off
        menu.addItem(helper)
        let notifications = NSMenuItem(title: "Notifications", action: nil, keyEquivalent: "")
        let nsub = NSMenu()
        for kind in Notifier.Kind.allCases {
            let i = item(kind == .finished ? "\(kind.title) (\(fmtDuration(Notifier.finishedAfter))+)" : kind.title,
                         #selector(toggleNotification(_:)))
            i.representedObject = kind.rawValue
            i.state = kind.enabled ? .on : .off
            nsub.addItem(i)
        }
        notifications.submenu = nsub
        menu.addItem(notifications)
        let keys = item("Global Shortcuts: ⌃⌥⌘J Jump · ⌃⌥⌘W Panel", #selector(toggleHotKeys))
        keys.state = hotKeysOn ? .on : .off
        menu.addItem(keys)
        menu.addItem(item("Open full dashboard", #selector(openDashboard)))
        menu.addItem(item("Set Up Hooks & Status Line…", #selector(runSetup)))
        menu.addItem(item("Run Diagnostics…", #selector(runDoctor)))
        menu.addItem(.separator())
        menu.addItem(item("Quit ccwidget", #selector(quit)))
        return menu
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
        i.target = self
        return i
    }

    @objc func jumpToSession(_ sender: NSMenuItem) {
        if let pid = (sender.representedObject as? Sess)?.pid { Feed.focus(pid: pid) }
    }

    /// `cd '<project>' && claude --resume <session id>`, ready to paste into a terminal.
    @objc func copyResumeCommand(_ sender: NSMenuItem) {
        guard let s = sender.representedObject as? Sess, let sid = s.sid, let cwd = s.cwd else { return }
        let quoted = "'" + cwd.replacingOccurrences(of: "'", with: "'\\''") + "'"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("cd \(quoted) && claude --resume \(sid)", forType: .string)
    }

    @objc func revealProject(_ sender: NSMenuItem) {
        guard let cwd = (sender.representedObject as? Sess)?.cwd else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: cwd)])
    }

    @objc func revealTranscript(_ sender: NSMenuItem) {
        guard let path = (sender.representedObject as? Sess)?.transcript else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    /// Shows what `ccwatch.py --install` would change, then does it once confirmed.
    @objc func runSetup() {
        popover.performClose(nil)
        NSApp.activate(ignoringOtherApps: true)
        let preview = Feed.run(["--install", "--dry-run"])
        guard preview.split(separator: "\n").contains(where: { $0.hasPrefix("+ ") }) else {
            showText("Already set up", preview)
            return
        }
        let alert = NSAlert()
        alert.messageText = "Set up the hook and status line?"
        alert.informativeText = "These changes are made in ~/.claude, keeping a backup of each file edited:"
        alert.accessoryView = Self.monospaced(preview.replacingOccurrences(of: "\n(dry run: nothing written)", with: ""))
        alert.addButton(withTitle: "Set Up")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        showText("Setup", Feed.run(["--install"]))
    }

    @objc func runDoctor() {
        popover.performClose(nil)
        NSApp.activate(ignoringOtherApps: true)
        showText("ccwatch diagnostics", Feed.run(["--doctor"]))
    }

    private func showText(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.accessoryView = Self.monospaced(text)
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    /// Command output for an alert, in a monospaced wrapping label.
    static func monospaced(_ text: String) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        label.isSelectable = true
        label.preferredMaxLayoutWidth = 480
        label.frame = NSRect(origin: .zero, size: NSSize(width: 480, height: label.fittingSize.height))
        return label
    }

    @objc func setOpacity(_ sender: NSMenuItem) {
        let a = Double(sender.tag) / 100
        panel.alphaValue = a
        UserDefaults.standard.set(a, forKey: "opacity")
    }

    func popoverWillShow(_ note: Notification) {
        popoverVisibility.visible = true
    }

    func popoverWillClose(_ note: Notification) {
        popoverClosed = Date()
    }

    func popoverDidClose(_ note: Notification) {
        popoverVisibility.visible = false
    }

    var aboutWindow: NSWindow?

    @objc func showAbout() {
        if aboutWindow == nil {
            let w = NSWindow(contentRect: .zero, styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "About ccwidget"
            w.contentView = NSHostingView(rootView: AboutView())
            w.isReleasedWhenClosed = false
            w.level = .floating
            w.center()
            aboutWindow = w
        }
        popover.performClose(nil)
        NSApp.activate(ignoringOtherApps: true)  // an accessory app has to ask to come forward
        aboutWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func toggleFloating() {  // the menu bar icon stays either way
        let show = !panel.isVisible
        if show { panel.orderFrontRegardless() } else { panel.orderOut(nil) }
        UserDefaults.standard.set(show, forKey: "floating")
    }

    @objc func toggleCompact() {  // the view's @AppStorage("compact") picks this up
        let d = UserDefaults.standard
        d.set(!d.bool(forKey: "compact"), forKey: "compact")
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }

    @objc func openDashboard() {
        guard let script = Feed.locateScript() else { return }
        let cmd = "\(Feed.locatePython()) '\(script.replacingOccurrences(of: "'", with: "'\\''"))'"
        let quoted = cmd.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let iterm = FileManager.default.fileExists(atPath: "/Applications/iTerm.app")
        let source = iterm
            ? "tell application \"iTerm\"\nactivate\ncreate window with default profile command \"\(quoted)\"\nend tell"
            : "tell application \"Terminal\"\nactivate\ndo script \"\(quoted)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
    }

    func applicationWillTerminate(_ note: Notification) {
        feed.stop()
    }
}

#if !SNAPSHOT  // the offscreen test renderer supplies its own entry point
@main
enum Main {
    static let delegate = AppDelegate()

    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--iconset"), i + 1 < args.count {  // used by package.sh
            do { try AppDelegate.writeIconset(to: args[i + 1]) } catch { print(error); exit(1) }
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)  // no Dock icon or app menu; just the menu bar icon
        app.delegate = delegate
        app.run()
    }
}
#endif
