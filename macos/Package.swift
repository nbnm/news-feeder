// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "NewsFeeder",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "NewsFeeder", targets: ["NewsFeeder"]),
        .executable(name: "NewsFeederChecks", targets: ["NewsFeederChecks"]),
    ],
    dependencies: [.package(url: "https://github.com/groue/GRDB.swift.git", from: "7.0.0")],
    targets: [
        .target(name: "FeedCore", dependencies: [.product(name: "GRDB", package: "GRDB.swift")]),
        .executableTarget(name: "NewsFeeder", dependencies: ["FeedCore"]),
        .executableTarget(name: "NewsFeederChecks", dependencies: ["FeedCore", .product(name: "GRDB", package: "GRDB.swift")], path: "Checks"),
    ]
)
