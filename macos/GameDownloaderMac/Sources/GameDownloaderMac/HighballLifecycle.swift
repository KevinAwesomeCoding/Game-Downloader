import AppKit
import Foundation

@MainActor
enum HighballLifecycle {
    private static let appURL = URL(fileURLWithPath: "/Applications/Highball.app", isDirectory: true)

    /// Highball's supported CLI writes bottle settings; quit the GUI cleanly first
    /// so it cannot later save a stale in-memory copy over those changes.
    static func closeForConfiguration() async throws -> Bool {
        guard let app = NSWorkspace.shared.runningApplications.first(where: {
            $0.bundleURL?.standardizedFileURL.path == appURL.standardizedFileURL.path
        }) else { return false }
        guard app.terminate() else {
            throw HighballReadinessError.commandFailed("Highball could not close for setup. Save your work there, close it, then retry.")
        }
        for _ in 0..<100 {
            if app.isTerminated { return true }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw HighballReadinessError.commandFailed("Highball is still open. Close it and retry the setup.")
    }

    static func reopenIfNeeded(_ wasRunning: Bool) {
        guard wasRunning else { return }
        NSWorkspace.shared.open(appURL)
    }
}
