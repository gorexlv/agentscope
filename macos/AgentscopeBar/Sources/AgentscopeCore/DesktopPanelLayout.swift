import Foundation
import CoreGraphics

public enum DesktopPanelSize: String, CaseIterable {
    case small, medium
    public init(savedValue: String?) { self = Self(rawValue: savedValue ?? "") ?? .small }
    public var dimensions: CGSize {
        switch self {
        case .small: return CGSize(width: 280, height: 184)
        case .medium: return CGSize(width: 340, height: 440)
        }
    }
    public var title: String { self == .small ? "小" : "中" }
}

public enum DesktopPanelLayout {
    /// Select the screen with most overlap, or the nearest visible screen after removal.
    /// Resize keeps the top-left anchor; the result fits even unusually small displays.
    public static func frame(origin: CGPoint?, previousSize: CGSize? = nil,
                             size: DesktopPanelSize, screens: [CGRect]) -> CGRect {
        let visible = screens.filter { !$0.isEmpty && !$0.isInfinite && !$0.isNull
            && $0.origin.x.isFinite && $0.origin.y.isFinite && $0.width.isFinite && $0.height.isFinite }
        let targetSize = size.dimensions
        guard let first = visible.first else { return CGRect(origin: .zero, size: targetSize) }
        var point = origin ?? CGPoint(x: first.maxX - targetSize.width - 24, y: first.maxY - targetSize.height - 24)
        if !point.x.isFinite || !point.y.isFinite { point = first.origin }
        if let previousSize { point.y += previousSize.height - targetSize.height }
        let proposed = CGRect(origin: point, size: targetSize)
        let area: (CGRect) -> CGFloat = { rect in
            let intersection = rect.intersection(proposed)
            return intersection.isNull ? 0 : intersection.width * intersection.height
        }
        let distance: (CGRect) -> CGFloat = { rect in
            let x = proposed.midX - min(rect.maxX, max(rect.minX, proposed.midX))
            let y = proposed.midY - min(rect.maxY, max(rect.minY, proposed.midY))
            return x * x + y * y
        }
        let screen = visible.sorted {
            area($0) == area($1) ? distance($0) < distance($1) : area($0) > area($1)
        }.first!
        let fitting = CGSize(width: min(targetSize.width, screen.width), height: min(targetSize.height, screen.height))
        return CGRect(x: min(max(point.x, screen.minX), screen.maxX - fitting.width),
                      y: min(max(point.y, screen.minY), screen.maxY - fitting.height),
                      width: fitting.width, height: fitting.height)
    }
}
