import SwiftUI
import AppKit
import AgentscopeCore

public struct PanelActions {
    public var clean: () -> Void
    public var worktrees: () -> Void
    public var panel: () -> Void
    public var quit: () -> Void
    public var login: () -> Void
    public var desktopToggle: () -> Void
    public var desktopSize: (DesktopPanelSize) -> Void
    public var interval: (Double) -> Void
    public init(clean: @escaping () -> Void = {}, worktrees: @escaping () -> Void = {},
                panel: @escaping () -> Void = {}, quit: @escaping () -> Void = {},
                login: @escaping () -> Void = {}, desktopToggle: @escaping () -> Void = {},
                desktopSize: @escaping (DesktopPanelSize) -> Void = { _ in }, interval: @escaping (Double) -> Void = { _ in }) {
        self.clean = clean; self.worktrees = worktrees; self.panel = panel
        self.quit = quit; self.login = login; self.interval = interval
        self.desktopToggle = desktopToggle; self.desktopSize = desktopSize
    }
}

public func memoryText(_ value: Double?) -> String {
    guard let value, value.isFinite else { return "—" }
    return value < 1024 ? String(format: "%.0f MB", value) : String(format: "%.1f GB", value / 1024)
}

private func displayName(_ agent: String?) -> String {
    switch agent {
    case "codex": return "Codex"
    case "claude": return "Claude"
    case "opencode": return "OpenCode"
    default: return agent?.capitalized ?? "Agent"
    }
}

public struct PanelView: View {
    public var snapshot: Snapshot?
    public var online: Bool
    public var cleaning: Bool
    public var error: String?
    public var interval: Double
    public var desktopEnabled: Bool
    public var desktopSize: DesktopPanelSize
    public var loginEnabled: Bool
    public var actions: PanelActions
    public var rendering: Bool
    public var now: Date
    @State private var allSessions = false
    @State private var allAlerts = false

    public init(snapshot: Snapshot?, online: Bool, cleaning: Bool = false, error: String? = nil,
                interval: Double = 5, loginEnabled: Bool = false,
                desktopEnabled: Bool = false, desktopSize: DesktopPanelSize = .small, actions: PanelActions = PanelActions(),
                now: Date = Date(), rendering: Bool = false) {
        self.snapshot = snapshot; self.online = online; self.cleaning = cleaning; self.error = error
        self.desktopEnabled = desktopEnabled; self.desktopSize = desktopSize
        self.rendering = rendering
        self.interval = interval; self.loginEnabled = loginEnabled; self.actions = actions; self.now = now
    }
    private var sessions: [Snapshot.Session] {
        (snapshot?.sessions ?? []).sorted {
            let ranks = ["working": 0, "waiting": 1, "stuck": 2, "idle": 3, "unknown": 4]
            let a = ranks[$0.state ?? "unknown"] ?? 4, b = ranks[$1.state ?? "unknown"] ?? 4
            if a != b { return a < b }
            return ($0.mem_mb ?? 0) > ($1.mem_mb ?? 0)
        }
    }
    private var visibleSessions: [Snapshot.Session] { allSessions ? sessions : Array(sessions.prefix(6)) }
    private var agents: [String] {
        var result: [String] = []
        for session in visibleSessions {
            let name = session.agent ?? "其他"
            if !result.contains(name) { result.append(name) }
        }
        return result
    }
    private var alerts: [Snapshot.Alert] { snapshot?.alerts ?? [] }
    private var height: CGFloat {
        let rows = min(3, alerts.count)
        return min(560, 288 + CGFloat(rows * 32) + CGFloat(min(sessions.count, 6) * 66) + CGFloat(agents.count * 28))
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            overview
            if !online {
                Label(snapshot == nil ? "暂时无法读取快照，请启动 agentscope watch。" : "正在显示本地快照，连接恢复后自动更新。", systemImage: "wifi.slash")
                    .font(.caption).foregroundStyle(Color.secondary).fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(error ?? "离线，读取本地快照")
            }
            if !alerts.isEmpty { attention }
            Divider()
            if rendering {
                sessionList.frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ScrollView { sessionList.frame(maxWidth: .infinity, alignment: .leading) }
                    .scrollIndicators(.automatic)
            }
            Divider()
            maintenance
        }.padding(16).frame(width: 340, height: rendering ? nil : height)
            .foregroundStyle(Color.primary)
            .background {
                Rectangle().fill(.regularMaterial)
                    .background(Color(nsColor: .windowBackgroundColor))
            }
    }
    private var header: some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text("agentscope").font(.headline)
                    HStack(spacing: 4) {
                        Circle().fill(online ? Color.green : Color.gray).frame(width: 5, height: 5)
                        Text(online ? "已连接" : "离线·读取快照").font(.system(size: 10, weight: .medium))
                    }.padding(.horizontal, 7).padding(.vertical, 4)
                        .background(.quaternary, in: Capsule())
                        .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
                }
                if rendering {
                    Text(updated(at: now)).font(.caption).foregroundStyle(Color.secondary)
                } else {
                    TimelineView(.periodic(from: now, by: 10)) { timeline in
                        Text(updated(at: timeline.date)).font(.caption).foregroundStyle(Color.secondary)
                    }
                }
            }
            Spacer(minLength: 0)
            if rendering {
                Image(systemName: "gearshape").font(.body).foregroundStyle(Color.secondary)
            } else {
            Menu {
                Menu("刷新间隔") {
                    ForEach([2.0, 5.0, 15.0], id: \.self) { seconds in
                        Button { actions.interval(seconds) } label: {
                            if seconds == interval { Label("\(Int(seconds)) 秒", systemImage: "checkmark") }
                            else { Text("\(Int(seconds)) 秒") }
                        }
                    }
                }
                Button(action: actions.login) {
                    if loginEnabled { Label("登录时启动", systemImage: "checkmark") }
                    else { Text("登录时启动") }
                }
                Toggle("桌面浮窗", isOn: Binding(get: { desktopEnabled }, set: { _ in actions.desktopToggle() }))
                Menu("浮窗尺寸") {
                    ForEach(DesktopPanelSize.allCases, id: \.self) { size in
                        Button { actions.desktopSize(size) } label: {
                            if size == desktopSize { Label(size.title, systemImage: "checkmark") }
                            else { Text(size.title) }
                        }
                    }
                }
                Button("打开面板", action: actions.panel)
                Divider()
                Button("退出 agentscope", action: actions.quit)
            } label: { Image(systemName: "gearshape").font(.body).foregroundStyle(Color.secondary) }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .accessibilityLabel("设置")
            }
        }
    }
    private func updated(at date: Date) -> String {
        guard let text = snapshot?.generated_at else { return "更新于 —" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let parsed = parser.date(from: text) ?? ISO8601DateFormatter().date(from: text)
        guard let parsed else { return "更新于 —" }
        if abs(date.timeIntervalSince(parsed)) < 1 { return "刚刚更新" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "zh-Hans")
        formatter.unitsStyle = .full
        return "更新于 " + formatter.localizedString(for: parsed, relativeTo: date)
    }
    private var overview: some View { OverviewCards(snapshot: snapshot) }
    private var attention: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("需要关注").font(.subheadline.weight(.semibold))
            ForEach(Array((allAlerts ? alerts : Array(alerts.prefix(3))).enumerated()), id: \.offset) { _, alert in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: alertSymbol(alert.kind)).frame(width: 16)
                        .foregroundStyle(alert.level == "warn" ? Color.orange : Color.secondary)
                    Text(alert.msg ?? "—").font(.caption).lineLimit(allAlerts ? nil : 2)
                }.accessibilityElement(children: .combine)
            }
            if alerts.count > 3 {
                Button(allAlerts ? "收起" : "显示全部 \(alerts.count) 类") { allAlerts.toggle() }
                    .buttonStyle(.plain).font(.caption).foregroundStyle(Color.accentColor)
            }
        }
    }
    private func alertSymbol(_ kind: String?) -> String {
        switch kind {
        case "CONTEXT_LOW": return "exclamationmark.bubble"
        case "MEM_HIGH": return "memorychip"
        case "CWD_CONFLICT": return "doc.on.doc"
        case "IDLE": return "moon.zzz"
        default: return "exclamationmark.triangle"
        }
    }
    private var sessionList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("会话").font(.subheadline.weight(.semibold))
            if sessions.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "terminal").font(.title2).foregroundStyle(.tertiary)
                    Text(snapshot == nil ? "连接后，会话会显示在这里" : "暂无运行中的会话").font(.caption).foregroundStyle(Color.secondary)
                }.frame(maxWidth: .infinity).padding(.vertical, 16)
            }
            ForEach(agents, id: \.self) { agent in
                let group = sessions.filter { ($0.agent ?? "其他") == agent }
                let known = group.compactMap(\.mem_mb)
                Text("\(displayName(agent)) · \(group.count) 个会话 · \(memoryText(known.isEmpty ? nil : known.reduce(0, +)))")
                    .font(.subheadline.weight(.regular)).foregroundStyle(Color.secondary)
                ForEach(visibleSessions.filter { ($0.agent ?? "其他") == agent }) { session in
                    SessionRow(session: session)
                }
            }
            if sessions.count > 6 {
                Button(allSessions ? "收起会话" : "显示全部 \(sessions.count) 个") { allSessions.toggle() }
                    .font(.caption).buttonStyle(.plain).foregroundStyle(Color.accentColor)
                    .frame(maxWidth: .infinity).padding(.vertical, 4)
            }
        }
    }
    private var maintenance: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("维护").font(.subheadline.weight(.semibold))
            Button(action: actions.clean) {
                HStack {
                    Image(systemName: cleaning ? "hourglass" : "sparkles").frame(width: 16)
                    Text(cleaning ? "正在处理清理请求…" : "清理残留进程（\(snapshot?.orphans?.cleanable_groups.map(String.init) ?? "—") 组）")
                    Spacer()
                    Image(systemName: "chevron.right").font(.caption2)
                }
            }.disabled(!online || cleaning || (snapshot?.orphans?.cleanable_groups ?? 0) == 0)
            Button(action: actions.worktrees) {
                HStack {
                    Image(systemName: "externaldrive").frame(width: 16)
                    Text("可回收 \(snapshot?.reclaim?.worktrees_gb.map { String(format: "%.1f GB", $0) } ?? "—")")
                    Spacer()
                    Image(systemName: "arrow.up.right").font(.caption2)
                }
            }.foregroundStyle(Color.secondary).accessibilityHint("在终端打开 agentscope worktrees 报告")
        }.font(.callout).buttonStyle(.plain)
    }
}

struct SessionRow: View {
    var readOnly = false
    let session: Snapshot.Session
    @State private var hovering = false
    private var project: String {
        let name = displayName(session.agent)
        let project = session.project?.trimmingCharacters(in: .whitespacesAndNewlines)
        let cwd = session.cwd?.trimmingCharacters(in: .whitespacesAndNewlines)
        if cwd == "/" { return name + " 桌面" }
        if let cwd, URL(fileURLWithPath: cwd).standardizedFileURL.path == NSHomeDirectory() { return "主目录" }
        if let project, !project.isEmpty, project != "/" { return project }
        if let cwd, !cwd.isEmpty {
            let last = URL(fileURLWithPath: cwd).lastPathComponent
            if !last.isEmpty, last != "/" { return last }
        }
        return name + " 桌面"
    }
    private var detail: String {
        let pieces = [session.branch, session.model].compactMap { $0 }.filter { !$0.isEmpty }
        return pieces.joined(separator: " · ")
    }
    private var symbol: String {
        switch session.agent {
        case "claude": return "sparkle"
        case "codex": return "chevron.left.forwardslash.chevron.right"
        case "opencode": return "terminal"
        default: return "cpu"
        }
    }
    private var state: (String, Color) {
        switch session.state {
        case "working": return ("工作中", .green)
        case "waiting": return ("等待输入", .blue)
        case "stuck": return ("可能卡住", .orange)
        case "idle": return ("空闲", .gray)
        default: return ("状态未知", .gray)
        }
    }
    @ViewBuilder var body: some View {
        if readOnly {
            content.accessibilityElement(children: .combine).accessibilityValue(state.0)
        } else {
            Button {
                guard let cwd = session.cwd, !cwd.isEmpty else { return }
                let url = URL(fileURLWithPath: cwd)
                guard (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else { return }
                NSWorkspace.shared.activateFileViewerSelecting([url])
            } label: {
                content
            }.buttonStyle(.plain).onHover { hovering = $0 }
                .disabled(session.cwd?.isEmpty != false)
                .accessibilityLabel("\(project)，\(detail)，\(state.0)，内存 \(memoryText(session.mem_mb))\(session.cost_usd.map { "，成本 " + format($0, prefix: "$") } ?? "")\(session.context_pct.map { "，上下文剩余 \(Int($0))%" } ?? "")")
                .accessibilityHint("在 Finder 打开工作目录")
                .help(session.cwd ?? "工作目录未知")
        }
    }
    private var content: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: symbol).font(.body).foregroundStyle(Color.secondary).frame(width: 20)
                VStack(alignment: .leading, spacing: 3) {
                    Text(project).font(.body).lineLimit(1).truncationMode(.middle)
                    if !detail.isEmpty {
                        Text(detail).font(.caption).foregroundStyle(Color.secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 3) {
                    HStack(spacing: 5) {
                        Circle().fill(state.1).frame(width: 6, height: 6)
                        Text(memoryText(session.mem_mb)).font(.caption).monospacedDigit()
                    }
                    if let cost = session.cost_usd {
                        Text(format(cost, prefix: "$")).font(.caption2).monospacedDigit().foregroundStyle(Color.secondary)
                    }
                }
            }
            if let context = session.context_pct {
                GeometryReader { geometry in
                    Capsule().fill(.quaternary)
                        .overlay(alignment: .leading) {
                            Capsule().fill(context <= 20 ? Color.orange : Color.accentColor)
                                .frame(width: geometry.size.width * min(1, max(0, context / 100)))
                        }
                }.frame(height: 3)
                    .accessibilityLabel("上下文剩余 \(Int(context))%")
            }
        }.padding(.horizontal, 8).padding(.vertical, 6).contentShape(RoundedRectangle(cornerRadius: 8))
            .background(hovering ? Color.primary.opacity(0.05) : .clear, in: RoundedRectangle(cornerRadius: 8))

    }
}

struct OverviewCards: View {
    let snapshot: Snapshot?
    var includeSessions = true
    var body: some View {
        HStack(spacing: 8) {
            metric("负载", value: snapshot?.host?.load1.map { String(format: "%.1f", $0) } ?? "—",
                   progress: loadProgress, symbol: "gauge.with.dots.needle.33percent")
            metric("内存", value: snapshot?.host?.mem_used_pct.map { String(format: "%.0f%%", $0) } ?? "—",
                   progress: snapshot?.host?.mem_used_pct.map { $0 / 100 }, symbol: "memorychip")
            if includeSessions {
                VStack(spacing: 6) {
                    Image(systemName: "square.stack.3d.up").font(.title3).frame(height: 24).foregroundStyle(Color.secondary)
                    Text(snapshot?.sessions.map { String($0.count) } ?? "—").font(.title3).monospacedDigit()
                    Text("会话").font(.caption).foregroundStyle(Color.secondary)
                    Text(costSummary)
                        .font(.caption2).monospacedDigit().foregroundStyle(Color.secondary)
                        .accessibilityLabel(costAccessibility)
                }.frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
            }
        }
    }
    private var costSummary: String {
        if let today = snapshot?.totals?.today_cost_usd { return "今日 " + format(today, prefix: "$") }
        let known = (snapshot?.sessions ?? []).compactMap(\.cost_usd).filter(\.isFinite)
        return known.isEmpty ? "成本未知" : format(known.reduce(0, +), prefix: "$")
    }
    private var costAccessibility: String {
        snapshot?.totals?.today_cost_usd == nil ? "已知会话成本合计，" + costSummary : costSummary
    }
    private var loadProgress: Double? {
        guard let load = snapshot?.host?.load1, let cores = snapshot?.host?.ncpu, cores > 0 else { return nil }
        return load / Double(cores)
    }
    private func metric(_ title: String, value: String, progress: Double?, symbol: String) -> some View {
        VStack(spacing: 6) {
            ZStack {
                Circle().stroke(.quaternary, lineWidth: 3)
                if let progress {
                    Circle().trim(from: 0, to: min(1, max(0, progress)))
                        .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                Image(systemName: symbol).font(.system(size: 10)).foregroundStyle(Color.secondary)
            }.frame(width: 24, height: 24).accessibilityHidden(true)
            Text(value).font(.title3).monospacedDigit()
            Text(title).font(.caption).foregroundStyle(Color.secondary)
            Text(title == "负载" ? "\(snapshot?.host?.ncpu.map(String.init) ?? "—") 核心" : "已使用")
                .font(.caption2).foregroundStyle(Color.secondary)
        }.frame(maxWidth: .infinity).padding(.vertical, 12)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
            .accessibilityElement(children: .ignore).accessibilityLabel("\(title) \(value)")
    }
}
