import SwiftUI
import AppKit
import AgentscopeCore

/// Presentation only: the desktop controller handles click/drag and owns no data source.
public struct DesktopPanelView: View {
    public let snapshot: Snapshot?
    public let online: Bool
    public let size: DesktopPanelSize
    public init(snapshot: Snapshot?, online: Bool, size: DesktopPanelSize) {
        self.snapshot = snapshot; self.online = online; self.size = size
    }
    private var sessions: [Snapshot.Session] {
        let ranks = ["working": 0, "waiting": 1, "stuck": 2, "idle": 3, "unknown": 4]
        return (snapshot?.sessions ?? []).sorted {
            let a = ranks[$0.state ?? "unknown"] ?? 4, b = ranks[$1.state ?? "unknown"] ?? 4
            return a == b ? ($0.mem_mb ?? 0) > ($1.mem_mb ?? 0) : a < b
        }
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("agentscope").font(.headline)
                    Spacer()
                    Label(snapshot?.alerts.map { String($0.count) } ?? "—", systemImage: "exclamationmark.bubble")
                        .font(.caption).monospacedDigit()
                        .foregroundStyle(snapshot?.hasWarning == true ? Color.orange : Color.secondary)
                        .accessibilityLabel("告警 \(snapshot?.alerts.map { String($0.count) } ?? "未知") 类")
                }
                if size == .medium {
                    HStack(spacing: 5) {
                        Circle().fill(online ? Color.green : Color.gray).frame(width: 5, height: 5)
                        Text(online ? "已连接" : "离线·读取快照")
                        Spacer()
                        Text(updated).lineLimit(1)
                    }.font(.caption2).foregroundStyle(Color.secondary)
                }
            }
            OverviewCards(snapshot: snapshot, includeSessions: size == .medium)
            if size == .medium {
                VStack(alignment: .leading, spacing: 4) {
                    Text("会话").font(.subheadline.weight(.semibold))
                    if sessions.isEmpty {
                        Label(snapshot == nil ? "等待本地快照" : "暂无运行中的会话", systemImage: "terminal")
                            .font(.caption).foregroundStyle(Color.secondary).padding(.vertical, 16)
                    }
                    ForEach(Array(sessions.prefix(3))) { session in
                        SessionRow(readOnly: true, session: session)
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                Divider()
                HStack {
                    Image(systemName: "externaldrive")
                    Text("可回收 \(snapshot?.reclaim?.worktrees_gb.map { String(format: "%.1f GB", $0) } ?? "—")")
                    Spacer()
                    Image(systemName: "arrow.up.right").font(.caption2)
                }.font(.caption).foregroundStyle(Color.secondary)
            }
        }
        .padding(16).frame(width: size.dimensions.width, height: size.dimensions.height)
        .foregroundStyle(Color.primary)
        .background {
            RoundedRectangle(cornerRadius: 20).fill(.regularMaterial)
                .background(Color(nsColor: .windowBackgroundColor), in: RoundedRectangle(cornerRadius: 20))
        }
        .clipShape(RoundedRectangle(cornerRadius: 20))
        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("agentscope 桌面浮窗，\(size.title)尺寸")
    }
    private var updated: String {
        guard let value = snapshot?.generated_at else { return "更新于 —" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = parser.date(from: value) ?? ISO8601DateTime(value) else { return "更新于 —" }
        if abs(Date().timeIntervalSince(date)) < 1 { return "刚刚更新" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "zh-Hans")
        return "更新于 " + formatter.localizedString(for: date, relativeTo: Date())
    }
    private func ISO8601DateTime(_ text: String) -> Date? { ISO8601DateFormatter().date(from: text) }
}
