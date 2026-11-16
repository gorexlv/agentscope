import Foundation
import Darwin

public struct Client {
    public let directory: URL
    public init(directory: URL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["AGENTSCOPE_STATE_DIR"] ?? NSHomeDirectory() + "/.local/state/agentscope")) {
        self.directory = directory
    }
    public func refresh() throws -> (Snapshot, Bool) {
        if let reply = try? request(["op": "snapshot"]), reply.ok == true, let snapshot = reply.snapshot { return (snapshot, true) }
        return (try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: directory.appendingPathComponent("snapshot.json"))), false)
    }
    public func request(_ request: [String: Any]) throws -> Reply {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw failure() }
        defer { close(fd) }
        let budget: Double = request["op"] as? String == "clean" ? 30 : 5
        let deadline = ProcessInfo.processInfo.systemUptime + budget
        func updateTimeout() throws {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { throw ClientError.message("daemon 响应超时，请稍后重试") }
            var value = timeval(tv_sec: Int(remaining), tv_usec: Int32((remaining - floor(remaining)) * 1_000_000))
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &value, socklen_t(MemoryLayout<timeval>.size))
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &value, socklen_t(MemoryLayout<timeval>.size))
        }
        var timeout = timeval(tv_sec: Int(budget), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var noSignal: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let path = Array(directory.appendingPathComponent("agentscope.sock").path.utf8) + [0]
        guard path.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw ClientError.message("Socket 路径过长") }
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: path) }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard connected == 0 else { throw failure() }
        var payload = try JSONSerialization.data(withJSONObject: request)
        payload.append(10)
        try payload.withUnsafeBytes { bytes in
            var sent = 0
            while sent < bytes.count {
                try updateTimeout()
                let count = send(fd, bytes.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
                guard count > 0 else { throw failure() }; sent += count
            }
        }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 8192)
        while data.count < 8 * 1024 * 1024 {
            try updateTimeout()
            let count = recv(fd, &buffer, buffer.count, 0)
            guard count > 0 else { throw failure() }
            data.append(contentsOf: buffer.prefix(count))
            if let end = data.firstIndex(of: 10) {
                let reply = try JSONDecoder().decode(Reply.self, from: data[..<end])
                guard reply.ok == true else {
                    if reply.error?.contains("busy") == true { throw ClientError.busy }
                    throw ClientError.message(reply.error ?? "Daemon 请求失败")
                }
                return reply
            }
        }
        throw ClientError.message("Daemon 响应超时或过大")
    }
    private func failure() -> ClientError {
        if errno == EAGAIN || errno == EWOULDBLOCK || errno == ETIMEDOUT {
            return .message("daemon 响应超时，请稍后重试")
        }
        return .message(String(cString: strerror(errno)))
    }
}
