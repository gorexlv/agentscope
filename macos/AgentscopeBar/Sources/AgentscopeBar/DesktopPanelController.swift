import AppKit
import SwiftUI
import Combine
import AgentscopeCore
import AgentscopeUI

private final class DesktopPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private struct DesktopContent: View {
    @ObservedObject var model: AppModel
    var body: some View {
        GeometryReader { geometry in
            let ideal = model.desktopSize.dimensions
            DesktopPanelView(snapshot: model.snapshot, online: model.online, size: model.desktopSize)
                .scaleEffect(min(1, min(geometry.size.width / ideal.width, geometry.size.height / ideal.height)), anchor: .topLeading)
                .frame(width: geometry.size.width, height: geometry.size.height, alignment: .topLeading)
        }
    }
}

private final class DesktopHostingView: NSHostingView<DesktopContent> {
    var openPanel: () -> Void = {}
    private var down: NSEvent?
    private var dragged = false
    override func hitTest(_ point: NSPoint) -> NSView? { bounds.contains(point) ? self : nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { down = event; dragged = false }
    override func mouseDragged(with event: NSEvent) {
        guard let down, !dragged,
              hypot(event.locationInWindow.x - down.locationInWindow.x,
                    event.locationInWindow.y - down.locationInWindow.y) > 3 else { return }
        dragged = true
        window?.performDrag(with: down)
    }
    override func mouseUp(with event: NSEvent) {
        if down != nil && !dragged { openPanel() }
        down = nil
    }
    override func accessibilityPerformPress() -> Bool { openPanel(); return true }
}

final class DesktopPanelController: NSObject, NSWindowDelegate {
    private let model: AppModel
    private let panel: DesktopPanel
    private var observers: [AnyCancellable] = []
    private var screenObserver: NSObjectProtocol?
    private var layingOut = false
    private var currentSize: DesktopPanelSize
    private let defaults: UserDefaults

    init(model: AppModel, defaults: UserDefaults = .standard, openPanel: @escaping () -> Void) {
        self.model = model; self.defaults = defaults; self.currentSize = model.desktopSize
        panel = DesktopPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        super.init()
        panel.delegate = self
        panel.isReleasedWhenClosed = false
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        panel.isExcludedFromWindowsMenu = true
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = false
        panel.isOpaque = false; panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.title = "agentscope 桌面浮窗"
        let host = DesktopHostingView(rootView: DesktopContent(model: model))
        host.sizingOptions = []
        host.openPanel = openPanel
        host.setAccessibilityRole(.button)
        host.setAccessibilityHelp("拖动以移动，点击打开菜单栏面板")
        panel.contentView = host
        relayout(restoring: true)
        observers.append(model.$desktopEnabled.dropFirst().receive(on: RunLoop.main).sink { [weak self] enabled in
            self?.setVisible(enabled)
        })
        observers.append(model.$desktopSize.dropFirst().receive(on: RunLoop.main).sink { [weak self] size in
            self?.resize(to: size)
        })
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            self?.relayout(restoring: false)
        }
        setVisible(model.desktopEnabled)
    }
    deinit { if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) } }
    private func setVisible(_ enabled: Bool) {
        if enabled { relayout(restoring: false); panel.orderFrontRegardless() }
        else { panel.orderOut(nil) }
    }
    private func resize(to size: DesktopPanelSize) {
        let previous = panel.frame.size
        currentSize = size
        apply(origin: panel.frame.origin, previous: previous)
    }
    private func relayout(restoring: Bool) {
        var origin: CGPoint? = panel.frame.origin
        if restoring {
            if let saved = defaults.array(forKey: "desktopPanelOrigin") as? [Double], saved.count == 2 {
                origin = CGPoint(x: saved[0], y: saved[1])
            } else { origin = nil }
        }
        apply(origin: origin)
    }
    private func apply(origin: CGPoint?, previous: CGSize? = nil) {
        let screens = NSScreen.screens.map(\.visibleFrame)
        guard !screens.isEmpty else { return }
        layingOut = true
        let frame = DesktopPanelLayout.frame(origin: origin, previousSize: previous,
                                              size: currentSize, screens: screens)
        panel.setFrame(frame, display: true)
        layingOut = false
        savePosition()
    }
    func windowDidMove(_ notification: Notification) { if !layingOut { savePosition() } }
    private func savePosition() {
        defaults.set([Double(panel.frame.origin.x), Double(panel.frame.origin.y)], forKey: "desktopPanelOrigin")
    }
}
