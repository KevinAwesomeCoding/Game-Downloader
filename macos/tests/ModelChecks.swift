import Foundation

// Only for this standalone model-check executable. Production uses SwiftPM's resource bundle.
extension Bundle { static var module: Bundle { .main } }

@main
enum ModelChecks {
    static func main() throws {
        let json = #"{"id":"fixture","name":"Fixture","zipUrl":[{"url":"https://example.com/a","name":"g.part1.rar"},{"url":"https://example.com/b","name":"g.part2.rar"}],"fixZipUrl":"https://example.com/repair"}"#
        let game = try JSONDecoder().decode(Game.self, from: Data(json.utf8))
        precondition(game.isAvailable && game.hasFix && game.zipURL == "https://example.com/a")
        let colored = try JSONDecoder().decode(Game.self, from: Data(##"{"id":"colored","name":"Colored","cardColor":"#d05a8d"}"##.utf8))
        let invalid = try JSONDecoder().decode(Game.self, from: Data(##"{"id":"invalid","name":"Invalid","cardColor":"#zzzzzz"}"##.utf8))
        precondition(colored.validCardColor == "#d05a8d" && invalid.validCardColor == nil && game.validCardColor == nil)
        precondition(colored.morePlayersGame() == nil)
        print("PASS: catalog card colors validate and missing colors fall back")
        var job = InstallJob(id: "fixture-job", game: game, installFolder: "/tmp/fixture")
        job.status = "paused"
        job.done = 65536
        let restored = try JSONDecoder().decode(InstallJob.self, from: JSONEncoder().encode(job))
        precondition(restored.status == "paused" && restored.done == 65536 && restored.game == game)
        print("PASS: multipart catalog and persisted job round trip")

        let steam = try JSONDecoder().decode(Game.self, from: Data(#"{"id":"fixture","name":"Fixture","zipUrl":"steam://install/480","fixZipUrl":"https://example.com/repair","type":"fix"}"#.utf8))
        precondition(steam.isSteamEntry && steam.hasFix && steam.type == "fix")
        let event = try JSONDecoder().decode(WorkerEvent.self, from: Data(#"{"status":"installed","message":"Installed","installPath":"/tmp/fixture","recipeAvailable":false}"#.utf8))
        precondition(event.status == "installed" && event.recipeAvailable == false)
        precondition(!job.registered && !job.isActive)
        print("PASS: Steam routing metadata and install/registration separation")

        let catalog = try JSONDecoder().decode([Game].self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        precondition(!catalog.isEmpty && Set(catalog.map(\.id)).count == catalog.count)
        precondition(catalog.contains(where: \.isSteamEntry))
        print("PASS: current catalog decodes (\(catalog.count) entries with unique IDs)")

        for id in ["mimesis", "peak", "schedule-i"] {
            guard let normal = catalog.first(where: { $0.id == id }),
                  let more = normal.morePlayersGame() else { fatalError("Missing edition for \(id)") }
            precondition(more.id == id + "--more-players")
            precondition(more.name == normal.name + " (More Players)")
            precondition(more.zipURL == normal.morePlayers?.zipUrl.firstURL)
            precondition(!more.hasFix && more.zipBackupSource == nil)
            precondition(more.macCompatibility?.tier == "Unverified")
            let saved = try JSONEncoder().encode(InstallJob(id: "job-\(id)", game: more, installFolder: "/tmp"))
            let restored = try JSONDecoder().decode(InstallJob.self, from: saved)
            precondition(restored.game == more && restored.game.zipURL == more.zipURL && !restored.game.hasFix)
        }
        precondition(catalog.first(where: { $0.id == "schedule-i" })?.zipURL == "https://archive.org/download/schedule-i-0.4.7f-12-12-player/Schedule-I-0.4.7f12-normal.zip")
        print("PASS: separate edition downloads, repair isolation, and persisted jobs")
    }
}
