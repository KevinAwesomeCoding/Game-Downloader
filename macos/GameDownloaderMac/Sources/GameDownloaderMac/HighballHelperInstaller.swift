import CryptoKit
import Foundation

enum HighballHelperInstaller {
    private struct Manifest: Decodable {
        let highballVersion: String
        let sourceCommit: String
        let steamIntegration: String?
        let binarySHA256: String

        enum CodingKeys: String, CodingKey {
            case highballVersion = "highball_version"
            case sourceCommit = "source_commit"
            case steamIntegration = "steam_integration"
            case binarySHA256 = "binary_sha256"
        }
    }

    private static let version = "0.10.22"
    private static let sourceCommit = "daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e"
    private static let managedFiles = [
        "highball", "build-manifest.json", "LICENSE.txt",
        "SOURCE-README.txt", "Highball-0.10.22-source.tar.gz", "highball-steam.patch",
    ]

    static func install() throws -> String {
        let appURL = URL(fileURLWithPath: "/Applications/Highball.app", isDirectory: true)
        guard let app = Bundle(url: appURL),
              app.infoDictionary?["CFBundleShortVersionString"] as? String == version else {
            throw HelperInstallError.highballVersion
        }

        guard let bundledBinary = AppResources.bundle.url(
            forResource: "highball", withExtension: nil,
            subdirectory: "Resources/HighballHelper"
        ) else {
            throw HelperInstallError.resourcesMissing
        }
        let bundledDirectory = bundledBinary.deletingLastPathComponent()
        let bundledManifest = bundledDirectory.appending(path: "build-manifest.json")
        guard FileManager.default.isExecutableFile(atPath: bundledBinary.path),
              let manifestData = try? Data(contentsOf: bundledManifest),
              let manifest = try? JSONDecoder().decode(Manifest.self, from: manifestData) else {
            throw HelperInstallError.resourcesMissing
        }
        guard manifest.highballVersion == version,
              manifest.sourceCommit == sourceCommit,
              manifest.steamIntegration == "steam-path-v1" else {
            throw HelperInstallError.provenanceMismatch
        }
        let expectedHash = try sha256(of: bundledBinary)
        guard expectedHash == manifest.binarySHA256 else {
            throw HelperInstallError.bundleHashMismatch
        }

        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "Game-Downloader/HighballHelper", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        let destination = support.appending(path: version, directoryHint: .isDirectory)
        if FileManager.default.fileExists(atPath: destination.path) {
            try rejectSymlink(destination)
            let existing = destination.appending(path: "highball")
            let existingManifest = destination.appending(path: "build-manifest.json")
            if FileManager.default.isExecutableFile(atPath: existing.path),
               let data = try? Data(contentsOf: existingManifest),
               let installed = try? JSONDecoder().decode(Manifest.self, from: data),
               installed.highballVersion == version,
               installed.sourceCommit == sourceCommit,
               installed.steamIntegration == "steam-path-v1",
               installed.binarySHA256 == expectedHash,
               managedFiles.allSatisfy({ FileManager.default.fileExists(atPath: destination.appending(path: $0).path) }),
               (try? sha256(of: existing)) == expectedHash {
                return "Highball helper is already installed and matches this app."
            }
        }

        let stage = support.appending(path: ".staging-\(version)-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: false)
        var stageExists = true
        defer {
            if stageExists { try? FileManager.default.removeItem(at: stage) }
        }

        for name in managedFiles {
            let source = bundledDirectory.appending(path: name)
            guard FileManager.default.fileExists(atPath: source.path) else {
                throw HelperInstallError.resourcesMissing
            }
            try FileManager.default.copyItem(at: source, to: stage.appending(path: name))
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: stage.appending(path: "highball").path)
        guard try sha256(of: stage.appending(path: "highball")) == expectedHash else {
            throw HelperInstallError.stagedHashMismatch
        }

        var backup: URL?
        if FileManager.default.fileExists(atPath: destination.path) {
            let previous = support.appending(path: ".previous-\(version)-\(UUID().uuidString)", directoryHint: .isDirectory)
            try FileManager.default.moveItem(at: destination, to: previous)
            backup = previous
        }
        do {
            try FileManager.default.moveItem(at: stage, to: destination)
            stageExists = false
        } catch {
            if let backup, !FileManager.default.fileExists(atPath: destination.path) {
                try? FileManager.default.moveItem(at: backup, to: destination)
            }
            throw error
        }
        return "Installed the verified Highball \(version) helper."
    }

    private static func sha256(of file: URL) throws -> String {
        let digest = SHA256.hash(data: try Data(contentsOf: file))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private static func rejectSymlink(_ url: URL) throws {
        let values = try url.resourceValues(forKeys: [.isSymbolicLinkKey])
        if values.isSymbolicLink == true { throw HelperInstallError.unsafeDestination }
    }
}

private enum HelperInstallError: LocalizedError {
    case highballVersion
    case resourcesMissing
    case provenanceMismatch
    case bundleHashMismatch
    case stagedHashMismatch
    case unsafeDestination

    var errorDescription: String? {
        switch self {
        case .highballVersion:
            return "Install Highball 0.10.22 before installing its Game Installer helper."
        case .resourcesMissing:
            return "The matching Highball helper files are missing from this Mac app. Re-download the Mac app."
        case .provenanceMismatch:
            return "The bundled helper is for a different Highball build. It was not installed."
        case .bundleHashMismatch:
            return "The bundled Highball helper failed its integrity check. It was not installed."
        case .stagedHashMismatch:
            return "The staged Highball helper failed its integrity check. The existing helper was kept."
        case .unsafeDestination:
            return "The existing helper folder is a symbolic link. It was left untouched."
        }
    }
}
