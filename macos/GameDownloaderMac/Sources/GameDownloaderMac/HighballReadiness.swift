import CryptoKit
import Foundation

struct ReadinessReport: Decodable {
    struct Runtime: Decodable {
        struct Engine: Decodable {
            struct Renderer: Decodable {
                let available: Bool
                let licenseAccepted: Bool
                enum CodingKeys: String, CodingKey {
                    case available
                    case licenseAccepted = "license_accepted"
                }
            }
            let id: String
            let wineVersion: String
            let complete: Bool
            let missingFiles: [String]
            let renderers: [String: Renderer]
            enum CodingKeys: String, CodingKey {
                case id, complete, renderers
                case wineVersion = "wine_version"
                case missingFiles = "missing_files"
            }
        }

        struct Bottle: Decodable {
            let name: String
            let engine: String
            let renderer: String
            let pins: Int
            let recipes: String
            let ready: Bool
            let issues: [String]
        }

        let status: String
        let home: String
        let engines: [Engine]
        let bottles: [Bottle]
        let damagedBottles: [[String: String]]
        enum CodingKeys: String, CodingKey {
            case status, home, engines, bottles
            case damagedBottles = "damaged_bottles"
        }
    }

    let status: String
    let detail: String?
    let readiness: Runtime?
}

enum HighballReadinessError: LocalizedError {
    case appMissing
    case helperMissing
    case helperVersionMismatch
    case helperHashMismatch
    case pythonMissing
    case reportInvalid(String)
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .appMissing: return "Highball 0.10.22 was not found in /Applications."
        case .helperMissing: return "The local Highball helper is not installed."
        case .helperVersionMismatch: return "The helper manifest does not match Highball 0.10.22."
        case .helperHashMismatch: return "The helper does not match its local build manifest."
        case .pythonMissing: return "Python 3 was not found. It is required for the Highball readiness check."
        case .reportInvalid(let detail): return "The readiness report could not be read: \(detail)"
        case .commandFailed(let detail): return detail
        }
    }
}

enum HighballReadiness {
    static let version = "0.10.22"
    static let reviewedCommit = "daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e"

    static func check() async throws -> ReadinessReport {
        try await Task.detached(priority: .userInitiated) {
            try runCheck()
        }.value
    }

    private static func runCheck() throws -> ReadinessReport {
        let app = URL(fileURLWithPath: "/Applications/Highball.app", isDirectory: true)
        guard let appInfo = Bundle(url: app),
              appInfo.infoDictionary?["CFBundleShortVersionString"] as? String == version else {
            throw HighballReadinessError.appMissing
        }

        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "Game-Downloader/HighballHelper/\(version)", directoryHint: .isDirectory)
        let helper = support.appending(path: "highball")
        let manifestURL = support.appending(path: "build-manifest.json")
        guard FileManager.default.isExecutableFile(atPath: helper.path),
              let manifestData = try? Data(contentsOf: manifestURL),
              let manifest = try? JSONSerialization.jsonObject(with: manifestData) as? [String: Any] else {
            throw HighballReadinessError.helperMissing
        }

        guard manifest["highball_version"] as? String == version,
              manifest["source_commit"] as? String == reviewedCommit else {
            throw HighballReadinessError.helperVersionMismatch
        }
        let binaryData = try Data(contentsOf: helper)
        let digest = SHA256.hash(data: binaryData).map { String(format: "%02x", $0) }.joined()
        guard manifest["binary_sha256"] as? String == digest else {
            throw HighballReadinessError.helperHashMismatch
        }

        guard let python = pythonExecutable() else { throw HighballReadinessError.pythonMissing }
        guard let script = AppResources.bundle.url(forResource: "readiness", withExtension: "py", subdirectory: "Resources") else {
            throw HighballReadinessError.reportInvalid("The bundled readiness checker is missing.")
        }
        let process = Process()
        process.executableURL = python
        process.arguments = [script.path, "--app", app.path, "--helper", helper.path, "--manifest", manifestURL.path]
        process.environment = ProcessInfo.processInfo.environment.merging([
            "LC_ALL": "C", "TERM": "dumb", "NO_COLOR": "1",
        ]) { _, new in new }
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard let report = try? JSONDecoder().decode(ReadinessReport.self, from: data) else {
                let detail = String(data: data, encoding: .utf8) ?? "invalid output"
                throw HighballReadinessError.reportInvalid(detail)
            }
            if report.status == "readiness_failed" {
                throw HighballReadinessError.commandFailed(report.detail ?? "Highball readiness failed.")
            }
            return report
        } catch let error as HighballReadinessError {
            throw error
        } catch {
            throw HighballReadinessError.commandFailed(error.localizedDescription)
        }
    }

    private static func pythonExecutable() -> URL? {
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.map { URL(fileURLWithPath: $0) }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }
}
