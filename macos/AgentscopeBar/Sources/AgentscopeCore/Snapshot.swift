import Foundation

public struct Snapshot: Decodable {
    public var schema: Int?
    public var generated_at: String?
    public var host: Host?
    public var sessions: [Session]?
    public var alerts: [Alert]?
    public var orphans: Orphans?
    public var reclaim: Reclaim?
    public struct Host: Decodable { public var load1: Double? }
    public struct Session: Decodable {
        public var agent: String?
        public var state: String?
        public var cwd: String?
        public var mem_mb: Double?
        public var cost_usd: Double?
        public var label: String {
            "\(agent ?? "—") · \(state ?? "—") · \(format(mem_mb, suffix: " MB")) · \(format(cost_usd, prefix: "$"))"
        }
    }
    public struct Alert: Decodable { public var level: String?; public var msg: String? }
    public struct Orphans: Decodable { public var groups: Int?; public var cleanable_groups: Int? }
    public struct Reclaim: Decodable { public var worktrees_gb: Double?; public var removable: Int? }
    public var title: String { "L \(format(host?.load1)) · ⚠ \(alerts.map { String($0.count) } ?? "—")" }
    public var hasWarning: Bool { alerts?.contains { $0.level == "warn" } ?? false }
}
public func format(_ value: Double?, prefix: String = "", suffix: String = "") -> String {
    guard let value, value.isFinite else { return "—" }
    return prefix + String(format: "%.2f", value) + suffix
}
public struct Target: Decodable, Equatable {
    public var pgid: Int?
    public var size: Int?
    public var cwd: String?
    public var names: [String]?
    public var label: String { "PGID \(pgid.map(String.init) ?? "—") · \(size.map(String.init) ?? "—") 个进程 · \(cwd ?? "—") · \(names?.joined(separator: ", ") ?? "—")" }
}
public struct Reply: Decodable {
    public var ok: Bool?
    public var error: String?
    public var snapshot: Snapshot?
    public var source: String?
    public var as_of: String?
    public var token: String?
    public var targets: [Target]?
    public var cleaned: [Int]?
    public var skipped: [Skipped]?
    public struct Skipped: Decodable { public var pgid: Int?; public var reason: String? }
}
public enum ClientError: Error, LocalizedError {
    case message(String)
    public var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}
public struct CleanupFlow {
    public enum State: Equatable { case idle, previewing, awaitingConfirmation(String, Date), executing, finished }
    public private(set) var state: State = .idle
    public private(set) var targets: [Target] = []
    public init() {}
    public mutating func begin() -> [String: Any] {
        state = .previewing; targets = []
        return ["op": "clean", "dry_run": true]
    }
    public mutating func preview(_ reply: Reply, now: Date = Date()) throws {
        guard state == .previewing, reply.ok == true, let token = reply.token, !token.isEmpty,
              let targets = reply.targets else { cancel(); throw ClientError.message("清理预演响应无效") }
        self.targets = targets; state = .awaitingConfirmation(token, now)
    }
    public mutating func confirm(now: Date = Date()) throws -> [String: Any] {
        guard case .awaitingConfirmation(let token, let issued) = state,
              now.timeIntervalSince(issued) >= 0, now.timeIntervalSince(issued) < 60,
              !targets.isEmpty else { cancel(); throw ClientError.message("预演已过期或没有目标，请重新预演") }
        state = .executing
        return ["op": "clean", "dry_run": false, "token": token]
    }
    public mutating func cancel() { state = .idle; targets = [] }
    public mutating func finish() { state = .finished; targets = [] }
}
