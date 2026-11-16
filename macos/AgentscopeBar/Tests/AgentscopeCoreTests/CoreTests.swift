import XCTest
@testable import AgentscopeCore

final class CoreTests: XCTestCase {
    func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(text.utf8))
    }
    func testContractFixture() throws {
        let url = Bundle.module.url(forResource: "snapshot", withExtension: "json", subdirectory: "Fixtures")!
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        XCTAssertEqual(snapshot.title, "L 2.10 · ⚠ 1")
        XCTAssertEqual(snapshot.sessions?.first?.mem_mb, 410)
        XCTAssertEqual(snapshot.reclaim?.removable, 47)
    }
    func testPreviewMetadataAndCleanableCount() throws {
        let snapshot = try decode(Snapshot.self, #"{"orphans":{"groups":4,"cleanable_groups":2}}"#)
        XCTAssertEqual(snapshot.orphans?.cleanable_groups, 2)
        let reply = try decode(Reply.self, #"{"ok":true,"source":"cache","as_of":"2026-11-16T10:00:00+08:00","token":"t","targets":[]}"#)
        XCTAssertEqual(reply.source, "cache")
        XCTAssertEqual(reply.as_of, "2026-11-16T10:00:00+08:00")
    }
    func testMissingAndNullFields() throws {
        let empty = try decode(Snapshot.self, "{}")
        XCTAssertEqual(empty.title, "L — · ⚠ —")
        XCTAssertFalse(empty.hasWarning)
        let partial = try decode(Snapshot.self, #"{"host":null,"sessions":[{}],"alerts":[{"level":"warn"}],"new_field":true}"#)
        XCTAssertEqual(partial.sessions?.first?.label, "— · — · — · —")
        XCTAssertTrue(partial.hasWarning)
    }
    func testCleanupRequiresPreviewAndConfirmation() throws {
        var flow = CleanupFlow()
        XCTAssertThrowsError(try flow.confirm())
        XCTAssertEqual(flow.begin()["dry_run"] as? Bool, true)
        let reply = try decode(Reply.self, #"{"ok":true,"token":"opaque","targets":[{"pgid":42,"size":3,"cwd":"/tmp","names":["node"]}]}"#)
        let now = Date()
        try flow.preview(reply, now: now)
        XCTAssertEqual(flow.targets.first?.pgid, 42)
        let request = try flow.confirm(now: now.addingTimeInterval(1))
        XCTAssertEqual(request["token"] as? String, "opaque")
        XCTAssertEqual(request["dry_run"] as? Bool, false)
        XCTAssertThrowsError(try flow.confirm())
    }
    func testCleanupCancellationExpirationAndInvalidPreview() throws {
        let reply = try decode(Reply.self, #"{"ok":true,"token":"token","targets":[{"pgid":42}]}"#)
        var flow = CleanupFlow(); let now = Date()
        _ = flow.begin(); try flow.preview(reply, now: now); flow.cancel()
        XCTAssertThrowsError(try flow.confirm(now: now))
        _ = flow.begin(); try flow.preview(reply, now: now)
        XCTAssertThrowsError(try flow.confirm(now: now.addingTimeInterval(60)))
        _ = flow.begin()
        XCTAssertThrowsError(try flow.preview(decode(Reply.self, #"{"ok":true}"#)))
        XCTAssertThrowsError(try flow.confirm())
        _ = flow.begin(); try flow.preview(decode(Reply.self, #"{"ok":true,"token":"t","targets":[]}"#))
        XCTAssertThrowsError(try flow.confirm())
    }
    func testBusyFailureDiffersFromUnknownExecutionOutcome() {
        let busy = cleanupFailureMessage(ClientError.busy)
        XCTAssertEqual(busy.title, "清理未执行")
        XCTAssertTrue(busy.detail.contains("daemon 正忙，未执行。请重新预演。"))
        XCTAssertFalse(busy.detail.contains("可能已执行"))
        let unknown = cleanupFailureMessage(ClientError.message("daemon 响应超时，请稍后重试"))
        XCTAssertEqual(unknown.title, "未取得清理结果")
        XCTAssertTrue(unknown.detail.contains("请求可能已执行，请刷新查看"))
    }
    func testDryRunDaemonPreviewCannotBeExecuted() throws {
        let reply = try decode(Reply.self, #"{"ok":true,"token":"t","executable":false,"targets":[{"pgid":42}]}"#)
        var flow = CleanupFlow()
        _ = flow.begin()
        XCTAssertThrowsError(try flow.preview(reply)) { error in
            XCTAssertEqual(error.localizedDescription, "daemon 处于预演模式，无法执行")
        }
        XCTAssertThrowsError(try flow.confirm())
    }
    func testCleanupResultsIncludeGoneSeparately() throws {
        let reply = try decode(Reply.self, #"{"ok":true,"cleaned":[10],"gone":[20],"skipped":[]}"#)
        XCTAssertEqual(reply.cleaned, [10])
        XCTAssertEqual(reply.gone, [20])
    }
    func testOfflineFallback() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try Data(#"{"host":{"load1":0},"alerts":[]}"#.utf8).write(to: directory.appendingPathComponent("snapshot.json"))
        let (snapshot, online) = try Client(directory: directory).refresh()
        XCTAssertFalse(online)
        XCTAssertEqual(snapshot.title, "L 0.00 · ⚠ 0")
    }
}
