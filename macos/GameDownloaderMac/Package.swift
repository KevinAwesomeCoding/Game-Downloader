// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "GameDownloaderMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "GameDownloaderMac", targets: ["GameDownloaderMac"]),
    ],
    targets: [
        .executableTarget(
            name: "GameDownloaderMac",
            resources: [.copy("Resources")]
        ),
    ]
)
