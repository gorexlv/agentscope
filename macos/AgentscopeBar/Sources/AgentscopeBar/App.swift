import AppKit
import SwiftUI
import AgentscopeUI
import ServiceManagement
import AgentscopeCore

final class AppModel: ObservableObject {
    private var timer: Timer?
    private let client = Client()
    @Published var snapshot: Snapshot?
    @Published var online = false
    private var refreshing = false
    @Published var cleaning = false
    private var flow = CleanupFlow()
    @Published var lastError: String?
    @Published var loginEnabled = SMAppService.mainApp.status == .enabled
    @Published var refreshInterval = 5.0
    private var interval: Double {
        let saved = UserDefaults.standard.double(forKey: "refreshInterval")
        return [2.0, 5.0, 15.0].contains(saved) ? saved : 5
    }
    init() { refreshInterval = interval; schedule(); refresh() }
    private func schedule() {
        timer?.invalidate()
        timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(timer!, forMode: .common)
    }
    private func refresh() {
        guard !refreshing else { return }; refreshing = true
        DispatchQueue.global(qos: .utility).async { [self] in
            let result = Result { try client.refresh() }
            DispatchQueue.main.async { [self] in
                refreshing = false
                switch result {
                case .success(let value): snapshot = value.0; online = value.1; lastError = nil
                case .failure(let error): online = false; lastError = error.localizedDescription
                }
                render()
            }
        }
    }
    private func render() { objectWillChange.send() }
    func actions() -> PanelActions {
        PanelActions(clean: { self.cleanOrphans() }, worktrees: { self.worktrees() },
                     panel: { self.panel() }, quit: { NSApp.terminate(nil) },
                     login: { self.toggleLogin() }, interval: { seconds in
                         UserDefaults.standard.set(seconds, forKey: "refreshInterval")
                         self.refreshInterval = seconds; self.schedule()
                     })
    }
    private func terminal(_ command: String) {
        // Commands are fixed literals; no snapshot data is interpolated into shell code.
        let source = "tell application \"Terminal\"\nactivate\ndo script \"\(command)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if let error { message("无法打开终端", error.description) }
    }
    private func worktrees() { terminal("agentscope worktrees") }
    private func panel() { terminal("agentscope panel") }
    private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
            if SMAppService.mainApp.status == .requiresApproval { message("需要批准登录项", "请在系统设置 → 通用 → 登录项中允许 AgentscopeBar。") }
        } catch { message("登录项设置失败", error.localizedDescription) }
        loginEnabled = SMAppService.mainApp.status == .enabled
        render()
    }
    private func quit() { NSApp.terminate(nil) }
    private func message(_ title: String, _ detail: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = detail; alert.addButton(withTitle: "好"); alert.runModal()
    }
    private func cleanOrphans() {
        guard online, !cleaning else { return }
        cleaning = true
        let request = flow.begin(); render()
        DispatchQueue.global(qos: .utility).async { [self] in
            let result = Result { try client.request(request) }
            DispatchQueue.main.async { [self] in
                do {
                    try flow.preview(result.get())
                    guard !flow.targets.isEmpty else { flow.cancel(); cleaning = false; render(); message("无需清理", "预演没有发现可清理的孤儿组。"); return }
                    let alert = NSAlert(); alert.alertStyle = .warning
                    alert.messageText = "确认清理 \(flow.targets.count) 个孤儿组？"
                    let preview = try result.get()
                    alert.informativeText = "以下进程组将被终止。执行前 daemon 会再次校验；预演令牌 60 秒后失效。\n来源：\(preview.source ?? "—") · 数据时间：\(preview.as_of ?? "—")\n清理最多等待 30 秒。"
                    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 200))
                    let text = NSTextView(frame: scroll.bounds); text.isEditable = false
                    text.string = flow.targets.map(\.label).joined(separator: "\n\n")
                    scroll.documentView = text; scroll.hasVerticalScroller = true; alert.accessoryView = scroll
                    alert.addButton(withTitle: "取消"); alert.addButton(withTitle: "确认清理")
                    NSApp.activate(ignoringOtherApps: true)
                    guard alert.runModal() == .alertSecondButtonReturn else { flow.cancel(); cleaning = false; render(); return }
                    execute(try flow.confirm())
                } catch { flow.cancel(); cleaning = false; render(); message("清理未执行", error.localizedDescription) }
            }
        }
    }
    private func execute(_ request: [String: Any]) {
        DispatchQueue.global(qos: .utility).async { [self] in
            let result = Result { try client.request(request) }
            DispatchQueue.main.async { [self] in
                flow.finish(); cleaning = false; render(); refresh()
                switch result {
                case .success(let reply):
                    let skipped = reply.skipped?.map { "\($0.pgid.map(String.init) ?? "—")：\($0.reason ?? "—")" }.joined(separator: "\n") ?? "—"
                    message("清理结果", "已清理：\(reply.cleaned?.map(String.init).joined(separator: ", ") ?? "—")\n已自行退出：\(reply.gone?.map(String.init).joined(separator: ", ") ?? "—")\n跳过：\(skipped)")
                case .failure(let error):
                    let failure = cleanupFailureMessage(error)
                    message(failure.title, failure.detail)
                }
            }
        }
    }
}
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}
@main
struct AgentscopeBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = AppModel()
    var body: some Scene {
        MenuBarExtra {
            PanelView(snapshot: model.snapshot, online: model.online, cleaning: model.cleaning,
                      error: model.lastError, interval: model.refreshInterval,
                      loginEnabled: model.loginEnabled, actions: model.actions())
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "gauge.with.dots.needle.33percent")
                    .foregroundStyle(model.snapshot?.hasWarning == true ? Color.orange : Color.primary)
                Text(model.snapshot?.host?.load1.map { String(format: "%.1f", $0) } ?? "—")
                    .monospacedDigit()
                let count = model.snapshot?.alerts?.filter { $0.level == "warn" }.count ?? 0
                if count > 0 {
                    Text("\(count)").font(.caption2.bold()).padding(.horizontal, 4)
                        .background(.orange.opacity(0.2), in: Capsule())
                        .accessibilityLabel("\(count) 类警告")
                }
            }.accessibilityLabel("agentscope 系统负载与告警")
        }.menuBarExtraStyle(.window)
    }
}
