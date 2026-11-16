import AppKit
import SwiftUI
import AgentscopeCore
import AgentscopeUI

@main
struct Preview {
    @MainActor static func main() throws {
        let args = CommandLine.arguments
        guard args.count == 4 || (args.count == 5 && args[4] == "desktop") else {
            fputs("Usage: AgentscopePreview <snapshot.json> <output-prefix> <online|offline> [desktop]\n", stderr)
            exit(2)
        }
        _ = NSApplication.shared
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: URL(fileURLWithPath: args[1])))
        let sizes: [DesktopPanelSize?] = args.count == 5 ? DesktopPanelSize.allCases.map { Optional($0) } : [nil]
        for size in sizes {
        for (name, scheme) in [("light", ColorScheme.light), ("dark", ColorScheme.dark)] {
            NSApp.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
            let content: AnyView
            if let size {
                content = AnyView(DesktopPanelView(snapshot: snapshot, online: args[3] == "online", size: size))
            } else {
                content = AnyView(PanelView(snapshot: snapshot, online: args[3] == "online", rendering: true))
            }
            let view = content.environment(\.colorScheme, scheme)

            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            guard let image = renderer.cgImage,
                  let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
                throw ClientError.message("Failed to render preview")
            }
            let path = args[2].replacingOccurrences(of: "{appearance}", with: name).replacingOccurrences(of: "{size}", with: size?.rawValue ?? "") + ".png"
            try data.write(to: URL(fileURLWithPath: path))
            print(path)
        }
        }
    }
}
