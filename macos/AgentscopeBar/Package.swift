// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "AgentscopeBar", platforms: [.macOS(.v14)], products: [
    .executable(name: "AgentscopeBar", targets: ["AgentscopeBar"]),
    .executable(name: "AgentscopePreview", targets: ["AgentscopePreview"]),
    .library(name: "AgentscopeCore", targets: ["AgentscopeCore"])
], targets: [
    .target(name: "AgentscopeCore"),
    .target(name: "AgentscopeUI", dependencies: ["AgentscopeCore"]),
    .executableTarget(name: "AgentscopeBar", dependencies: ["AgentscopeCore", "AgentscopeUI"]),
    .executableTarget(name: "AgentscopePreview", dependencies: ["AgentscopeCore", "AgentscopeUI"]),
    .testTarget(name: "AgentscopeCoreTests", dependencies: ["AgentscopeCore", "AgentscopeUI"], resources: [.copy("Fixtures")])
])
