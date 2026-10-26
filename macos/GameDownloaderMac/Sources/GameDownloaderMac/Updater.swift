import AppKit
import CryptoKit
import Foundation

struct AppUpdate: Identifiable {
    let id: String
    let version: String
    let notes: String
    let url: URL
    let sha256: String
}

enum AppUpdater {
    private struct Release: Decodable {
        struct Asset: Decodable {
            let name: String
            let browser_download_url: URL
            let digest: String?
        }
        let tag_name: String
        let body: String?
        let assets: [Asset]
    }
    private static let endpoint = URL(string: "https://api.github.com/repos/KevinAwesomeCoding/Game-Downloader/releases/latest")!

    static func check() async throws -> AppUpdate? {
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("GameInstaller-updater", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return nil }
        let release = try JSONDecoder().decode(Release.self, from: data)
        guard compare(release.tag_name, Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0") > 0,
              let asset = release.assets.first(where: { $0.name == "GameInstaller-macOS.zip" }),
              let digest = asset.digest?.lowercased().replacingOccurrences(of: "sha256:", with: ""),
              digest.count == 64 else { return nil }
        return AppUpdate(id: release.tag_name, version: release.tag_name, notes: release.body ?? "No release notes provided.", url: asset.browser_download_url, sha256: digest)
    }

    static func install(_ update: AppUpdate) async throws {
        let (data, _) = try await URLSession.shared.data(from: update.url)
        let actual = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard actual == update.sha256 else { throw CocoaError(.fileReadCorruptFile) }
        let fm = FileManager.default
        let support = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appending(path: "Game-Downloader/Updates", directoryHint: .isDirectory)
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        let archive = support.appending(path: "update-\(UUID().uuidString).zip")
        try data.write(to: archive, options: .atomic)
        let app = Bundle.main.bundleURL
        let currentPID = ProcessInfo.processInfo.processIdentifier
        let script = support.appending(path: "install-update-\(UUID().uuidString).sh")
        let body = """
        #!/bin/bash
        set -euo pipefail
        APP=\(shellQuote(app.path))
        ZIP=\(shellQuote(archive.path))
        ROOT=\(shellQuote(support.path))
        PID=\(currentPID)
        STAGE="$ROOT/stage-$$"
        PREVIOUS="$ROOT/previous-$$.app"
        mkdir -p "$STAGE"
        ditto -x -k "$ZIP" "$STAGE"
        NEW="$STAGE/Game Installer.app"
        for i in {1..90}; do
          if ! kill -0 "$PID" 2>/dev/null; then break; fi
          sleep 1
        done
        mv "$APP" "$PREVIOUS"
        if mv "$NEW" "$APP"; then
          rm -rf "$PREVIOUS" "$STAGE" "$ZIP"
          open "$APP"
        else
          mv "$PREVIOUS" "$APP"
          open "$APP"
          exit 1
        fi
        rm -f "$0"
        """
        try body.write(to: script, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path]
        try process.run()
        NSApp.terminate(nil)
    }

    private static func compare(_ lhs: String, _ rhs: String) -> Int {
        func parts(_ value: String) -> [Int] { value.dropFirst(value.hasPrefix("v") ? 1 : 0).split(separator: "-").flatMap { $0.split(separator: ".") }.compactMap { Int($0) } }
        let a = parts(lhs), b = parts(rhs)
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0, y = i < b.count ? b[i] : 0
            if x != y { return x < y ? -1 : 1 }
        }
        return 0
    }

    private static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
