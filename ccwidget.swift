// ccwidget — floating, always-on-top desktop widget for ccwatch.
//
// Runs `ccwatch.py --json` as a child process and renders its once-a-second
// snapshots in a translucent panel that floats above other windows, follows
// you across Spaces and full-screen apps, and never takes keyboard focus.
//
// Build: ./build-widget.sh   (creates ccwidget.app next to ccwatch.py)
// Drag anywhere to move · click the chevron to compact · right-click for options
// It also lives in the menu bar: click the icon for the same view in a popover,
// right-click it for the options menu.

import AppKit
import Combine
import SwiftUI

// MARK: - Feed data (mirrors snapshot() in ccwatch.py)

struct Snapshot: Decodable {
    var sessions: [Sess] = []
    var limits: [Limit] = []
    var limitsStale: String?
    var today: Today?
    var projects: Projects?
}

struct Sess: Decodable {
    var pid: Int?
    var name: String
    var title: String?
    var project: String?
    var model: String?
    var state: String
    var status: String?
    var doing: String?
    var detail: String?
    var elapsed: String?
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
            let last = s.split(separator: "\n").last.map(String.init) ?? s
            DispatchQueue.main.async { self?.problem = last }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping else { return }
                self.problem = self.problem ?? "ccwatch stopped; restarting…"
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.start() }
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
            guard s.pid != nil, inside != hovering else { return }
            hovering = inside
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
        .onDisappear { if hovering { NSCursor.pop() } }
        .onTapGesture {
            if let pid = s.pid { Feed.focus(pid: pid) }
        }
        .help(s.pid == nil ? "" : "Click to show this session's window")
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

// MARK: - Widget

struct WidgetView: View {
    @ObservedObject var feed: Feed
    @AppStorage("compact") var compact = false
    var chrome = true  // false inside the menu bar popover, which brings its own frame
    var onSize: (CGSize) -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { tl in
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

// MARK: - App


final class WidgetPanel: NSPanel {
    override var canBecomeKey: Bool { false }  // never steal keyboard focus
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let feed = Feed()
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

        let view = WidgetView(feed: feed, onSize: { [weak self] in self?.fit($0) })
        let host = NSHostingView(rootView: view)
        host.sizingOptions = []
        panel.contentView = host

        // Native right-click (or control-click) menu. A SwiftUI .contextMenu on a view
        // that redraws ten times a second, in a panel that never becomes key, drops
        // hovers and clicks on its submenus; catching the click here also means it
        // works whichever subview is under the pointer.
        NSEvent.addLocalMonitorForEvents(matching: [.rightMouseDown, .leftMouseDown]) { [weak self] event in
            guard let self = self, let window = event.window, let view = window.contentView,
                  window === self.panel || window === self.popover.contentViewController?.view.window,
                  event.type == .rightMouseDown || event.modifierFlags.contains(.control) else { return event }
            NSMenu.popUpContextMenu(self.buildMenu(), with: event, for: view)
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
        setUpStatusItem()
        feed.start()
    }

    // MARK: Menu bar icon

    func setUpStatusItem() {
        let content = NSHostingController(rootView: WidgetView(feed: feed, chrome: false, onSize: { [weak self] size in
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
    func buildMenu() -> NSMenu {
        let menu = NSMenu()
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
        menu.addItem(item("Open full dashboard", #selector(openDashboard)))
        menu.addItem(.separator())
        menu.addItem(item("Quit ccwidget", #selector(quit)))
        return menu
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
        i.target = self
        return i
    }

    @objc func setOpacity(_ sender: NSMenuItem) {
        let a = Double(sender.tag) / 100
        panel.alphaValue = a
        UserDefaults.standard.set(a, forKey: "opacity")
    }

    func popoverWillClose(_ note: Notification) {
        popoverClosed = Date()
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
