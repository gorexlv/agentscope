import AppKit
import ServiceManagement
import AgentscopeCore

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?
    private let client = Client()
    private var snapshot: Snapshot?
    private var online = false
    private var refreshing = false
    private var cleaning = false
    private var flow = CleanupFlow()
    private var lastError: String?
    private var interval: Double {
        let saved = UserDefaults.standard.double(forKey: "refreshInterval")
        return [2.0, 5.0, 15.0].contains(saved) ? saved : 5
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        render(); schedule(); refresh()
    }
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
    private func add(_ menu: NSMenu, _ title: String, action: Selector? = nil, object: Any? = nil) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self; entry.representedObject = object; menu.addItem(entry)
        return entry
    }
    private func render() {
        let title = (snapshot?.title ?? "L — · ⚠ —") + (online ? "" : " · ?")
        item.button?.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: snapshot?.hasWarning == true ? NSColor.systemOrange : NSColor.labelColor])
        let menu = NSMenu()
        _ = add(menu, online ? "agentscope · daemon 已连接" : "daemon 未运行")
        _ = add(menu, "更新：\(snapshot?.generated_at ?? "—")")
        if let lastError { _ = add(menu, "读取失败：\(lastError)") }
        menu.addItem(.separator())
        _ = add(menu, "会话")
        if let sessions = snapshot?.sessions {
            if sessions.isEmpty { _ = add(menu, "无会话") }
            for session in sessions {
                _ = add(menu, session.label, action: session.cwd == nil ? nil : #selector(openDirectory(_:)), object: session.cwd)
            }
        } else { _ = add(menu, "—") }
        menu.addItem(.separator())
        _ = add(menu, "告警")
        if let alerts = snapshot?.alerts {
            if alerts.isEmpty { _ = add(menu, "无告警") }
            for alert in alerts { _ = add(menu, "\(alert.level ?? "—") · \(alert.msg ?? "—")") }
        } else { _ = add(menu, "—") }
        menu.addItem(.separator())
        let count = snapshot?.orphans?.cleanable_groups
        let clean = add(menu, cleaning ? "正在处理清理请求…" : "清理 \(count.map(String.init) ?? "—") 个孤儿组…", action: #selector(cleanOrphans))
        clean.isEnabled = online && !cleaning && (count ?? 0) > 0
        menu.autoenablesItems = false
        for entry in menu.items where entry.action == nil { entry.isEnabled = false }
        _ = add(menu, "可回收：\(format(snapshot?.reclaim?.worktrees_gb, suffix: " GB")) · \(snapshot?.reclaim?.removable.map(String.init) ?? "—") 个工作树", action: #selector(worktrees))
        _ = add(menu, "打开 panel", action: #selector(panel))
        let refreshMenu = NSMenu()
        for seconds in [2, 5, 15] {
            let choice = add(refreshMenu, "\(seconds) 秒", action: #selector(changeInterval(_:)), object: seconds)
            choice.state = interval == Double(seconds) ? .on : .off
        }
        let refreshEntry = add(menu, "刷新间隔"); refreshEntry.submenu = refreshMenu
        let login = add(menu, "登录时启动", action: #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(.separator())
        _ = add(menu, "退出", action: #selector(quit))
        item.menu = menu
    }
    @objc private func openDirectory(_ sender: NSMenuItem) {
        if let path = sender.representedObject as? String {
            let url = URL(fileURLWithPath: path)
            guard (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else { return }
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }
    @objc private func changeInterval(_ sender: NSMenuItem) {
        guard let seconds = sender.representedObject as? Int else { return }
        UserDefaults.standard.set(seconds, forKey: "refreshInterval"); schedule(); render()
    }
    private func terminal(_ command: String) {
        // Commands are fixed literals; no snapshot data is interpolated into shell code.
        let source = "tell application \"Terminal\"\nactivate\ndo script \"\(command)\"\nend tell"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if let error { message("无法打开终端", error.description) }
    }
    @objc private func worktrees() { terminal("agentscope worktrees") }
    @objc private func panel() { terminal("agentscope panel") }
    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
            if SMAppService.mainApp.status == .requiresApproval { message("需要批准登录项", "请在系统设置 → 通用 → 登录项中允许 AgentscopeBar。") }
        } catch { message("登录项设置失败", error.localizedDescription) }
        render()
    }
    @objc private func quit() { NSApp.terminate(nil) }
    private func message(_ title: String, _ detail: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = detail; alert.addButton(withTitle: "好"); alert.runModal()
    }
    @objc private func cleanOrphans() {
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
                    message("清理结果", "已清理：\(reply.cleaned?.map(String.init).joined(separator: ", ") ?? "—")\n跳过：\(skipped)")
                case .failure(let error): message("未取得清理结果", "\(error.localizedDescription)\n请求可能已执行，请刷新查看；不会自动重试。")
                }
            }
        }
    }
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
