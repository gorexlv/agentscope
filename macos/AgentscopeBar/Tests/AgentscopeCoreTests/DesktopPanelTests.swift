import XCTest
import CoreGraphics
@testable import AgentscopeCore

final class DesktopPanelTests: XCTestCase {
    let screen = CGRect(x: 0, y: 24, width: 1440, height: 850)

    func testSavedSizeDefaultsAndSwitchDimensions() {
        XCTAssertEqual(DesktopPanelSize(savedValue: nil), .small)
        XCTAssertEqual(DesktopPanelSize(savedValue: "invalid"), .small)
        XCTAssertEqual(DesktopPanelSize(savedValue: "medium"), .medium)
        XCTAssertEqual(DesktopPanelSize.small.dimensions, CGSize(width: 280, height: 184))
        XCTAssertEqual(DesktopPanelSize.medium.dimensions, CGSize(width: 340, height: 440))
    }
    func testResizePreservesTopLeftWhenSpaceAllows() {
        let origin = CGPoint(x: 120, y: 550)
        let medium = DesktopPanelLayout.frame(origin: origin, previousSize: DesktopPanelSize.small.dimensions,
                                              size: .medium, screens: [screen])
        XCTAssertEqual(medium.minX, origin.x)
        XCTAssertEqual(medium.maxY, origin.y + DesktopPanelSize.small.dimensions.height)
        let small = DesktopPanelLayout.frame(origin: medium.origin, previousSize: medium.size,
                                             size: .small, screens: [screen])
        XCTAssertEqual(small.origin, origin)
    }
    func testOffscreenPositionClampsToVisibleArea() {
        let result = DesktopPanelLayout.frame(origin: CGPoint(x: 1700, y: -400), size: .medium, screens: [screen])
        XCTAssertTrue(screen.contains(result))
        XCTAssertEqual(result.maxX, screen.maxX)
        XCTAssertEqual(result.minY, screen.minY)
    }
    func testNegativeCoordinatesRemainOnSecondScreen() {
        let left = CGRect(x: -1920, y: -200, width: 1920, height: 1080)
        let result = DesktopPanelLayout.frame(origin: CGPoint(x: -1200, y: 300), size: .small, screens: [screen, left])
        XCTAssertEqual(result.origin, CGPoint(x: -1200, y: 300))
        XCTAssertTrue(left.contains(result))
    }
    func testRemovedScreenRestoresOntoRemainingScreen() {
        let result = DesktopPanelLayout.frame(origin: CGPoint(x: -1800, y: 200), size: .medium, screens: [screen])
        XCTAssertTrue(screen.contains(result))
        XCTAssertEqual(result.minX, screen.minX)
    }
    func testInitialPlacementAndSmallDisplay() {
        let initial = DesktopPanelLayout.frame(origin: nil, size: .small, screens: [screen])
        XCTAssertEqual(initial.maxX, screen.maxX - 24)
        XCTAssertEqual(initial.maxY, screen.maxY - 24)
        let tiny = CGRect(x: 0, y: 0, width: 200, height: 150)
        XCTAssertEqual(DesktopPanelLayout.frame(origin: nil, size: .medium, screens: [tiny]), tiny)
    }
    func testInvalidPersistedPositionAndAbsentScreensAreSafe() {
        let result = DesktopPanelLayout.frame(origin: CGPoint(x: CGFloat.nan, y: 20), size: .small, screens: [screen])
        XCTAssertTrue(screen.contains(result))
        XCTAssertEqual(DesktopPanelLayout.frame(origin: nil, size: .small, screens: []).origin, .zero)
    }
}
