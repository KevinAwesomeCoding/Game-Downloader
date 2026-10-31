import Foundation

@main
enum UpdateModelChecks {
    static func main() throws {
        let hash = String(repeating: "a", count: 64)
        let release = AppUpdater.Release(
            tag_name: "v1.4-20261027-84",
            description: "Release notes",
            _links: .init(releaseURL: URL(string: "https://gitlab.com/example/app/-/releases/v1.4-20261027-84")!),
            assets: .init(links: [.init(name: "GameInstaller-macOS-arm64.zip",
                                       url: URL(string: "https://example.invalid/app.zip")!, direct_asset_url: nil)])
        )
        let checksums = ["GameInstaller-macOS-arm64.zip": hash]
        precondition(AppUpdater.resolve(release, checksums: checksums, currentVersion: "v1.4-20261027-84", architecture: "arm64") == nil)
        precondition(AppUpdater.resolve(release, checksums: checksums, currentVersion: "v1.3", architecture: "x86_64") == nil)
        guard let update = AppUpdater.resolve(release, checksums: checksums, currentVersion: "v1.3", architecture: "arm64") else {
            fatalError("Expected a compatible Apple silicon update")
        }
        precondition(update.canSelfUpdate && update.notes == "Release notes")
        let legacyRelease = AppUpdater.Release(
            tag_name: release.tag_name, description: nil, _links: release._links,
            assets: .init(links: [.init(name: "GameInstaller-macOS.zip",
                                       url: URL(string: "https://example.invalid/legacy.zip")!, direct_asset_url: nil)])
        )
        precondition(AppUpdater.resolve(legacyRelease, checksums: ["GameInstaller-macOS.zip": hash], currentVersion: "v1.3", architecture: "arm64")?.canSelfUpdate == true)

        var notice = UpdateNoticeState()
        precondition(!notice.indicatorVisible && !notice.detailsPresented)
        notice.offer(update)
        precondition(notice.indicatorVisible && !notice.detailsPresented)
        notice.openDetails()
        notice.later()
        precondition(notice.indicatorVisible && !notice.detailsPresented)
        notice.openDetails()
        precondition(notice.detailsPresented)

        let script = AppUpdater.installScript(appPath: "/Applications/Game Installer.app",
                                              archivePath: "/tmp/update.zip",
                                              stagePath: "/Applications/.stage",
                                              previousPath: "/Applications/.previous.app",
                                              statusPath: "/Users/user/Library/Application Support/Game-Downloader/Updates/update-status.txt",
                                              processID: 1234,
                                              bundleID: "com.kevinawesomecoding.GameInstaller")
        precondition(script.contains("ditto -x -k"))
        precondition(script.contains("mv \"$APP\" \"$PREVIOUS\""))
        precondition(script.contains("mv \"$PREVIOUS\" \"$APP\""))
        precondition(script.contains("echo installed > \"$STATUS\""))
        precondition(script.contains("echo failed > \"$STATUS\""))
        precondition(script.contains("open \"$APP\""))
        precondition(script.contains("kill -TERM \"$PID\""))

        precondition(LibrarySearch.matches(query: "  cafe  ", name: "Café Racer", description: nil))
        precondition(LibrarySearch.matches(query: "co-op arcade", name: "Example", description: "Arcade co-op"))
        precondition(!LibrarySearch.matches(query: "co-op missing", name: "Example", description: "Arcade co-op"))
        precondition(LibrarySearch.matches(query: "   ", name: "Example", description: nil))

        let missingChecksum = AppUpdater.Release(
            tag_name: "v1.5-20261027-85", description: nil, _links: release._links,
            assets: .init(links: [.init(name: "GameInstaller-macOS-arm64.zip",
                                       url: URL(string: "https://example.invalid/app.zip")!, direct_asset_url: nil)])
        )
        let manual = AppUpdater.resolve(missingChecksum, checksums: [:], currentVersion: "v1.4-20261027-84", architecture: "arm64")
        precondition(manual != nil && manual?.canSelfUpdate == false && manual?.manualReason != nil)
        print("PASS: macOS update selection, reminder state, and install/rollback script")
    }
}
