// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "AgentscopeBar", platforms: [.macOS(.v14)], products: [
    .executable(name: "AgentscopeBar", targets: ["AgentscopeBar"]),
    .library(name: "AgentscopeCore", targets: ["AgentscopeCore"])
], targets: [
    .target(name: "AgentscopeCore"),
    .executableTarget(name: "AgentscopeBar", dependencies: ["AgentscopeCore"]),
    .testTarget(name: "AgentscopeCoreTests", dependencies: ["AgentscopeCore"], resources: [.copy("Fixtures")])
])
