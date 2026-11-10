import Foundation
import XCTest
@testable import GameDownloaderMac

final class UpdaterTests: XCTestCase {
    func testTranslocatedInstallIsRejectedBeforeWriteProbe() throws {
        let app = URL(fileURLWithPath: "/private/var/folders/x/AppTranslocation/abc/d/Game Installer.app")
        var probed = false
        XCTAssertThrowsError(try AppUpdater.preflight(appURL: app, requiredBytes: 1,
                                                      availableBytes: { _ in 10 },
                                                      canReplaceBundle: { _ in probed = true; return true })) { error in
            XCTAssertTrue((error as? UpdateError)?.localizedDescription.contains("protected copy") == true)
        }
        XCTAssertFalse(probed)
    }

    func testUnwritableAppParentIsRejected() {
        let app = URL(fileURLWithPath: "/Applications/Game Installer.app")
        XCTAssertThrowsError(try AppUpdater.preflight(appURL: app, requiredBytes: 1,
                                                      availableBytes: { _ in 10 },
                                                      canReplaceBundle: { _ in false })) { error in
            XCTAssertTrue((error as? UpdateError)?.localizedDescription.contains("~/Applications") == true)
        }
    }

    func testInsufficientDiskSpaceHasSpecificError() {
        let app = URL(fileURLWithPath: "/Applications/Game Installer.app")
        XCTAssertThrowsError(try AppUpdater.preflight(appURL: app, requiredBytes: 100,
                                                      availableBytes: { _ in 10 },
                                                      canReplaceBundle: { _ in true })) { error in
            guard case UpdateError.insufficientDiskSpace(let required, let available) = error else {
                return XCTFail("Expected an insufficient disk space error, got \(error)")
            }
            XCTAssertEqual(required, 100)
            XCTAssertEqual(available, 10)
            XCTAssertTrue(error.localizedDescription.contains("Free at least"))
        }
    }

    func testWritableUpdateReplacesAppAndRelaunches() throws {
        try AppUpdater.preflight(appURL: URL(fileURLWithPath: "/Applications/Game Installer.app"), requiredBytes: 100,
                                 availableBytes: { _ in 1_000 }, canReplaceBundle: { _ in true })
        try runInstallScript(failNewAppMove: false) { app, status, opened in
            XCTAssertTrue(FileManager.default.fileExists(atPath: app.appending(path: "new-marker").path))
            XCTAssertEqual(try String(contentsOf: status, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), "installed")
            XCTAssertTrue(FileManager.default.fileExists(atPath: opened.path))
        }
    }

    func testFailedReplacementRestoresPreviousApp() throws {
        try runInstallScript(failNewAppMove: true) { app, status, opened in
            XCTAssertTrue(FileManager.default.fileExists(atPath: app.appending(path: "old-marker").path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: app.appending(path: "new-marker").path))
            XCTAssertEqual(try String(contentsOf: status, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), "failed")
            XCTAssertFalse(FileManager.default.fileExists(atPath: opened.path))
        }
    }

    private func runInstallScript(failNewAppMove: Bool,
                                  verify: (URL, URL, URL) throws -> Void) throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "updater-test-\(UUID().uuidString)", directoryHint: .isDirectory)
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let app = root.appending(path: "Game Installer.app", directoryHint: .isDirectory)
        try fm.createDirectory(at: app, withIntermediateDirectories: true)
        try Data("old".utf8).write(to: app.appending(path: "old-marker"))
        let archive = root.appending(path: "update.zip")
        try Data().write(to: archive)
        let stage = root.appending(path: "stage", directoryHint: .isDirectory)
        let previous = root.appending(path: "previous.app", directoryHint: .isDirectory)
        let status = root.appending(path: "status.txt")
        let bin = root.appending(path: "bin", directoryHint: .isDirectory)
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)
        let opened = root.appending(path: "opened", directoryHint: .isDirectory)

        try writeExecutable("""
        #!/bin/bash
        mkdir -p "$4/Game Installer.app/Contents"
        echo bundle > "$4/Game Installer.app/Contents/Info.plist"
        echo new > "$4/Game Installer.app/new-marker"
        """, to: bin.appending(path: "ditto"))
        try writeExecutable("#!/bin/bash\necho com.kevinawesomecoding.GameInstaller\n", to: bin.appending(path: "PlistBuddy"))
        try writeExecutable("#!/bin/bash\ntouch \"\(opened.path)\"\n", to: bin.appending(path: "open"))
        let shouldFail = failNewAppMove ? "1" : "0"
        try writeExecutable("""
        #!/bin/bash
        if [ "\(shouldFail)" = "1" ] && [[ "$1" == *"Game Installer.app" ]] && [ "$2" == "\(app.path)" ] && [[ "$1" == *"stage/"* ]]; then exit 1; fi
        /bin/mv "$@"
        """, to: bin.appending(path: "mv"))

        let script = AppUpdater.installScript(appPath: app.path, archivePath: archive.path,
                                              stagePath: stage.path, previousPath: previous.path,
                                              statusPath: status.path, processID: 99999999,
                                              bundleID: "com.kevinawesomecoding.GameInstaller")
        let scriptURL = root.appending(path: "install.sh")
        try writeExecutable(script, to: scriptURL)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [scriptURL.path]
        process.environment = ProcessInfo.processInfo.environment.merging(["PATH": "\(bin.path):/usr/bin:/bin:/usr/sbin:/sbin"]) { _, new in new }
        try process.run()
        process.waitUntilExit()
        try verify(app, status, opened)
    }

    private func writeExecutable(_ contents: String, to url: URL) throws {
        try contents.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }
}
