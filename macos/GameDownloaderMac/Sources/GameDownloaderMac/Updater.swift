import AppKit
import CryptoKit
import Foundation

struct AppUpdate: Identifiable, Equatable {
    let id: String
    let version: String
    let notes: String
    let releaseURL: URL
    let downloadURL: URL?
    let sha256: String?
    let manualReason: String?

    var canSelfUpdate: Bool { downloadURL != nil && sha256 != nil && manualReason == nil }
}

struct UpdateNoticeState: Equatable {
    private(set) var update: AppUpdate?
    private(set) var detailsPresented = false

    var indicatorVisible: Bool { update != nil }

    mutating func offer(_ update: AppUpdate) { self.update = update }
    mutating func openDetails() { if update != nil { detailsPresented = true } }
    mutating func later() { detailsPresented = false }
}

enum AppUpdater {
    struct Release: Decodable {
        struct Asset: Decodable {
            let name: String
            let browser_download_url: URL
            let digest: String?
        }
        let tag_name: String
        let html_url: URL
        let body: String?
        let assets: [Asset]
    }
    private static let endpoint = URL(string: "https://gitlab.com/kevinanil-projects/Game-Downloader/-/releases/permalink/latest/downloads/release.json")!
    private static let expectedBundleID = "com.kevinawesomecoding.GameInstaller"
    static let protectedInstallMessage = "macOS is running a protected copy of Game Installer. Move Game Installer to /Applications (or your personal ~/Applications folder), open it there, then retry the update. No update was downloaded."

    static func isTranslocated(_ appURL: URL) -> Bool {
        appURL.standardizedFileURL.pathComponents.contains("AppTranslocation")
    }

    static func preflight(appURL: URL, requiredBytes: Int64,
                          storageURL: URL? = nil,
                          availableBytes: (URL) -> Int64?,
                          canReplaceBundle: (URL) -> Bool) throws {
        guard !isTranslocated(appURL) else {
            throw UpdateError.manualInstallRequired(protectedInstallMessage)
        }
        let parent = appURL.deletingLastPathComponent()
        guard canReplaceBundle(appURL) else {
            throw UpdateError.manualInstallRequired(protectedInstallMessage)
        }
        for location in [parent, FileManager.default.temporaryDirectory] + (storageURL.map { [$0] } ?? []) {
            if let available = availableBytes(location), available < requiredBytes {
                throw UpdateError.insufficientDiskSpace(required: requiredBytes, available: available)
            }
        }
    }

    static func consumeStatus() -> String? {
        guard Bundle.main.bundleIdentifier == expectedBundleID else { return nil }
        guard let support = try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                         appropriateFor: nil, create: false)
            .appending(path: "Game-Downloader/Updates/update-status.txt"),
              let status = try? String(contentsOf: support, encoding: .utf8) else { return nil }
        try? FileManager.default.removeItem(at: support)
        switch status.trimmingCharacters(in: .whitespacesAndNewlines) {
        case "installed": return "Game Installer was updated and restarted successfully."
        case "failed": return "The update could not be applied. The previous app was restored and remains available; the update is still available to retry."
        default: return nil
        }
    }

    static func check() async throws -> AppUpdate? {
        guard Bundle.main.bundleIdentifier == expectedBundleID else { return nil }
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("GameInstaller-updater", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw UpdateError.httpStatus((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        let release = try JSONDecoder().decode(Release.self, from: data)
        let current = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
        #if arch(arm64)
        return resolve(release, currentVersion: current, architecture: "arm64")
        #else
        return nil
        #endif
    }

    static func resolve(_ release: Release, currentVersion: String, architecture: String) -> AppUpdate? {
        guard compare(release.tag_name, currentVersion) > 0 else { return nil }
        guard architecture.lowercased() == "arm64" else { return nil }
        let compatibleNames = ["GameInstaller-macOS-arm64.zip", "GameInstaller-macOS.zip"]
        guard let asset = release.assets.first(where: { compatibleNames.contains($0.name) }) else {
            return AppUpdate(id: release.tag_name, version: release.tag_name,
                             notes: release.body ?? "No release notes provided.", releaseURL: release.html_url,
                             downloadURL: nil, sha256: nil,
                             manualReason: "This release does not include a compatible Apple silicon app download.")
        }
        let digest = asset.digest?.replacingOccurrences(of: "sha256:", with: "").lowercased()
        guard let digest, digest.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else {
            return AppUpdate(id: release.tag_name, version: release.tag_name,
                             notes: release.body ?? "No release notes provided.", releaseURL: release.html_url,
                             downloadURL: nil, sha256: nil,
                             manualReason: "This release has no valid SHA-256 checksum. Review and install it manually.")
        }
        return AppUpdate(id: release.tag_name, version: release.tag_name,
                         notes: release.body ?? "No release notes provided.", releaseURL: release.html_url,
                         downloadURL: asset.browser_download_url, sha256: digest, manualReason: nil)
    }

    static func compare(_ lhs: String, _ rhs: String) -> Int {
        func parts(_ value: String) -> [Int] {
            value.dropFirst(value.hasPrefix("v") ? 1 : 0)
                .split(separator: "-").flatMap { $0.split(separator: ".") }.compactMap { Int($0) }
        }
        let a = parts(lhs), b = parts(rhs)
        for index in 0..<max(a.count, b.count) {
            let left = index < a.count ? a[index] : 0
            let right = index < b.count ? b[index] : 0
            if left != right { return left < right ? -1 : 1 }
        }
        return 0
    }

    static func install(_ update: AppUpdate, report: @escaping @MainActor (String, Double?) -> Void) async throws {
        guard update.canSelfUpdate, let url = update.downloadURL, let expectedHash = update.sha256 else {
            throw UpdateError.manualInstallRequired(update.manualReason ?? "This release must be installed manually.")
        }
        let app = Bundle.main.bundleURL
        let parent = app.deletingLastPathComponent()
        let fm = FileManager.default
        let requiredBytes = try requiredUpdateSpace(for: app)
        let support = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appending(path: "Game-Downloader/Updates", directoryHint: .isDirectory)
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        try preflight(appURL: app, requiredBytes: requiredBytes, storageURL: support,
                      availableBytes: availableDiskSpace(at:), canReplaceBundle: canReplaceUpdateBundle(_:))

        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 120)
        await report("Downloading update…", 0)
        let (temporary, response) = try await download(request) { fraction in
            Task { @MainActor in
                report("Downloading update…", fraction)
            }
        }
        defer { try? fm.removeItem(at: temporary) }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw UpdateError.httpStatus((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
        await report("Verifying SHA-256 checksum…", nil)
        guard try sha256(of: temporary) == expectedHash else { throw UpdateError.invalidChecksum }
        await report("Checking app archive…", nil)
        try validateZip(temporary)

        let statusFile = support.appending(path: "update-status.txt")
        try? fm.removeItem(at: statusFile)
        let archive = support.appending(path: "update-\(UUID().uuidString).zip")
        try fm.moveItem(at: temporary, to: archive)
        let scriptURL = support.appending(path: "install-update-\(UUID().uuidString).sh")
        let stage = parent.appending(path: ".game-installer-stage-\(UUID().uuidString)")
        let previous = parent.appending(path: ".game-installer-previous-\(UUID().uuidString).app")
        let pid = ProcessInfo.processInfo.processIdentifier
        let bundleID = Bundle.main.bundleIdentifier ?? expectedBundleID
        let body = installScript(appPath: app.path, archivePath: archive.path,
                                 stagePath: stage.path, previousPath: previous.path,
                                 statusPath: statusFile.path, processID: pid, bundleID: bundleID)
        try body.write(to: scriptURL, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: scriptURL.path)
        await report("Verified. Closing the app to install and restart…", nil)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [scriptURL.path]
        try process.run()
    }

    private static func requiredUpdateSpace(for app: URL) throws -> Int64 {
        let enumerator = FileManager.default.enumerator(at: app, includingPropertiesForKeys: [.fileSizeKey], options: [.skipsHiddenFiles])
        var installedSize: Int64 = 0
        while let item = enumerator?.nextObject() as? URL {
            installedSize += Int64((try? item.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        // Keep room for the archive, expanded app, and previous app while staging.
        return max(512 * 1024 * 1024, installedSize * 2 + 128 * 1024 * 1024)
    }

    private static func availableDiskSpace(at url: URL) -> Int64? {
        let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeAvailableCapacityKey])
        return values?.volumeAvailableCapacityForImportantUsage ?? values?.volumeAvailableCapacity.map(Int64.init)
    }

    private static func canReplaceUpdateBundle(_ app: URL) -> Bool {
        let parent = app.deletingLastPathComponent()
        guard FileManager.default.isWritableFile(atPath: app.path) else { return false }
        let probe = parent.appending(path: ".game-installer-update-check-\(UUID().uuidString)")
        do {
            try Data().write(to: probe, options: .atomic)
            try FileManager.default.removeItem(at: probe)
            return true
        } catch {
            try? FileManager.default.removeItem(at: probe)
            return false
        }
    }

    private static func download(_ request: URLRequest,
                                 progress: @escaping (Double) -> Void) async throws -> (URL, URLResponse) {
        let delegate = UpdateDownloadDelegate(progress: progress)
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        delegate.session = session
        return try await withCheckedThrowingContinuation { continuation in
            delegate.continuation = continuation
            session.downloadTask(with: request).resume()
        }
    }

    static func installScript(appPath: String, archivePath: String, stagePath: String,
                              previousPath: String, statusPath: String,
                              processID: Int32, bundleID: String) -> String {
        """
        #!/bin/bash
        set -euo pipefail
        APP=\(shellQuote(appPath))
        ZIP=\(shellQuote(archivePath))
        STAGE=\(shellQuote(stagePath))
        PREVIOUS=\(shellQuote(previousPath))
        STATUS=\(shellQuote(statusPath))
        PID=\(processID)
        BUNDLE_ID=\(shellQuote(bundleID))
        fail_update() {
          echo failed > "$STATUS"
          rm -rf "$STAGE" "$ZIP"
          rm -f "$0"
          exit 1
        }
        mkdir -p "$STAGE" || fail_update
        ditto -x -k "$ZIP" "$STAGE" || fail_update
        NEW="$STAGE/Game Installer.app"
        if [ ! -d "$NEW" ] || [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$NEW/Contents/Info.plist" 2>/dev/null)" != "$BUNDLE_ID" ]; then fail_update; fi
        for i in {1..30}; do
          if [ "$i" -eq 12 ] && kill -0 "$PID" 2>/dev/null; then
            # Ask Launch Services to deliver the standard app quit event too.
            osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
          fi
          if [ "$i" -eq 22 ] && kill -0 "$PID" 2>/dev/null; then
            # SwiftUI can occasionally keep the process alive after normal quit requests.
            kill -TERM "$PID" 2>/dev/null || true
          fi
          if ! kill -0 "$PID" 2>/dev/null; then break; fi
          sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then fail_update; fi
        mv "$APP" "$PREVIOUS" || fail_update
        if mv "$NEW" "$APP"; then
          echo installed > "$STATUS"
          rm -rf "$PREVIOUS" "$STAGE" "$ZIP"
          open "$APP"
        else
          if [ -e "$APP" ]; then rm -rf "$APP"; fi
          if ! mv "$PREVIOUS" "$APP"; then
            echo "failed:previous-app-preserved-at:$PREVIOUS" > "$STATUS"
            rm -rf "$STAGE"
            rm -f "$0"
            exit 1
          fi
          fail_update
        fi
        rm -f "$0"
        """
    }

    private static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let data = try handle.read(upToCount: 1024 * 1024), !data.isEmpty { hash.update(data: data) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func validateZip(_ url: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        process.arguments = ["-tqq", url.path]
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw UpdateError.invalidArchive }
    }

    private static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}

private final class UpdateDownloadDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    var continuation: CheckedContinuation<(URL, URLResponse), Error>?
    var session: URLSession?
    private let progress: (Double) -> Void

    init(progress: @escaping (Double) -> Void) { self.progress = progress }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        guard totalBytesExpectedToWrite > 0 else { return }
        progress(min(1, max(0, Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))))
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {
        do {
            let destination = FileManager.default.temporaryDirectory
                .appending(path: "game-installer-update-\(UUID().uuidString).zip")
            try FileManager.default.moveItem(at: location, to: destination)
            guard let response = downloadTask.response else { throw UpdateError.invalidArchive }
            continuation?.resume(returning: (destination, response))
        } catch {
            continuation?.resume(throwing: error)
        }
        continuation = nil
        self.session?.finishTasksAndInvalidate()
        self.session = nil
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error, let continuation else { return }
        continuation.resume(throwing: error)
        self.continuation = nil
        self.session?.invalidateAndCancel()
        self.session = nil
    }
}

enum UpdateError: LocalizedError {
    case httpStatus(Int)
    case invalidChecksum
    case invalidArchive
    case manualInstallRequired(String)
    case insufficientDiskSpace(required: Int64, available: Int64)

    var errorDescription: String? {
        switch self {
        case .httpStatus(let status): return "The update server returned HTTP \(status). Try again or open the release page."
        case .invalidChecksum: return "The SHA-256 checksum did not match. The downloaded update was rejected."
        case .invalidArchive: return "The downloaded update is not a valid app archive. It was not installed."
        case .manualInstallRequired(let message): return message
        case .insufficientDiskSpace(let required, let available):
            return "There isn’t enough free disk space to install this update. Free at least \(ByteCountFormatter.string(fromByteCount: required - available, countStyle: .file)) and try again. The update has not been downloaded or installed."
        }
    }
}
