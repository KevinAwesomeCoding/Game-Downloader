import Foundation

struct InstallJob: Codable, Identifiable {
    let id: String
    let game: Game
    let installFolder: String
    var workspacePath: String?
    var status = "queued"
    var message = "Waiting in queue"
    var done: Int64 = 0
    var total: Int64 = 0
    var installPath: String?
    var recipeAvailable = false
    var registered = false
    var bottle: String?
    var targetBottle: String?
    var prepareRecipes: [String]?
    var automaticAdd: Bool?

    var isInstalled: Bool { installPath != nil }
    var isActive: Bool { ["preparing", "downloading", "extracting", "repairing", "stopping"].contains(status) }
}

struct WorkerEvent: Decodable {
    let status: String
    let message: String
    let done: Int64?
    let total: Int64?
    let archiveDone: Int64?
    let archiveTotal: Int64?
    let archiveRemaining: Int64?
    let partIndex: Int?
    let installPath: String?
    let recipeAvailable: Bool?
    let rarAvailable: Bool?
    let logPath: String?
    let workspacePath: String?
}
