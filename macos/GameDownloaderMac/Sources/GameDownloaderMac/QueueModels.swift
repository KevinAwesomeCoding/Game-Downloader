import Foundation

struct InstallJob: Codable, Identifiable {
    let id: String
    let game: Game
    let installFolder: String
    var status = "queued"
    var message = "Waiting in queue"
    var done: Int64 = 0
    var total: Int64 = 0
    var installPath: String?
    var recipeAvailable = false
    var registered = false
    var bottle: String?

    var isInstalled: Bool { installPath != nil }
    var isActive: Bool { ["downloading", "extracting", "repairing", "stopping"].contains(status) }
}

struct WorkerEvent: Decodable {
    let status: String
    let message: String
    let done: Int64?
    let total: Int64?
    let installPath: String?
    let recipeAvailable: Bool?
    let rarAvailable: Bool?
    let logPath: String?
}
