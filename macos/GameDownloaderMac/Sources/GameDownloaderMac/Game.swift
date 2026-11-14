import Foundation

struct Game: Codable, Identifiable, Hashable {
    struct MorePlayersEdition: Codable, Hashable {
        let zipUrl: CatalogLinks
        let version: String?
        let description: String?
    }
    struct Compatibility: Codable, Hashable {
        let tier: String?
        let riskLevel: String?
        let reasoning: String?
        let confidence: String?
    }

    let id: String
    let steamAppId: String?
    let name: String
    let description: String?
    let version: String?
    let size: String?
    let players: String?
    let thumbnail: String?
    let cardColor: String?
    let requirementsPlatform: String?
    let requirements: [String: String]?
    let macRequirements: [String: String]?
    let zipSource: CatalogLinks?
    let updateSource: CatalogLinks?
    let updateBackupSource: CatalogLinks?
    let fixSource: CatalogLinks?
    let zipBackupSource: CatalogLinks?
    let fixBackupSource: CatalogLinks?
    let type: String?
    let macCompatibility: Compatibility?
    let morePlayers: MorePlayersEdition?

    enum CodingKeys: String, CodingKey {
        case id, steamAppId, name, description, version, size, players, thumbnail, cardColor
        case requirementsPlatform, requirements, macRequirements, type
        case zipSource = "zipUrl"
        case updateSource = "updateZipUrl"
        case updateBackupSource = "updateZipBackupUrl"
        case fixSource = "fixZipUrl"
        case zipBackupSource = "zipBackupUrl"
        case fixBackupSource = "fixBackupUrl"
        case macCompatibility, morePlayers
    }

    var zipURL: String? { zipSource?.firstURL ?? zipBackupSource?.firstURL }
    var isSteamEntry: Bool { zipURL?.hasPrefix("steam://") == true }
    var isAvailable: Bool { zipURL != nil }
    var hasFix: Bool { fixSource?.firstURL != nil || fixBackupSource?.firstURL != nil }

    /// Match the catalog color format accepted by the Windows cards.
    var validCardColor: String? {
        guard let cardColor, cardColor.count == 7, cardColor.first == "#",
              cardColor.dropFirst().allSatisfy({ $0.isASCII && $0.isHexDigit }) else { return nil }
        return cardColor
    }

    /// A separate queue and folder identity keeps the original repair away from the modded archive.
    func morePlayersGame() -> Game? {
        guard let edition = morePlayers, edition.zipUrl.firstURL != nil,
              let encoded = try? JSONEncoder().encode(self),
              var raw = (try? JSONSerialization.jsonObject(with: encoded)) as? [String: Any],
              let source = try? JSONEncoder().encode(edition.zipUrl),
              let sourceValue = try? JSONSerialization.jsonObject(with: source, options: .fragmentsAllowed)
        else { return nil }
        raw["id"] = id + "--more-players"
        raw["name"] = name + " (More Players)"
        raw["zipUrl"] = sourceValue
        raw["version"] = edition.version ?? ""
        raw["description"] = edition.description ?? ""
        raw["macCompatibility"] = ["tier": "Unverified", "confidence": "Low",
                                   "reasoning": "The more-players edition has not been tested on Mac."]
        for key in ["morePlayers", "zipBackupUrl", "updateZipUrl", "updateZipBackupUrl", "fixZipUrl", "fixBackupUrl"] { raw.removeValue(forKey: key) }
        guard let data = try? JSONSerialization.data(withJSONObject: raw) else { return nil }
        return try? JSONDecoder().decode(Game.self, from: data)
    }

    var downloadSizeInBytes: Int64? {
        guard let size else { return nil }
        let pattern = #"^\s*(\d+(?:\.\d+)?)\s*(MB|GB|TB)\s*$"#
        guard let match = Self.firstMatch(pattern, in: size), match.count == 3,
              let amount = Double(match[1]) else { return nil }
        let multiplier: Double
        switch match[2].uppercased() {
        case "MB": multiplier = 1_048_576
        case "GB": multiplier = 1_073_741_824
        case "TB": multiplier = 1_099_511_627_776
        default: return nil
        }
        return Int64(amount * multiplier)
    }

    /// Best maximum player count explicitly stated by the catalog; nil when unbounded or unspecified.
    var listedMaximumPlayers: Int? {
        guard let players else { return nil }
        let versus = Self.allMatches(#"(?i)(\d+)\s*(?:v|vs\.?)\s*(\d+)"#, in: players)
            .compactMap { match -> Int? in
                guard match.count == 3, let lhs = Int(match[1]), let rhs = Int(match[2]) else { return nil }
                return lhs + rhs
            }
        let ranges = Self.allMatches(#"(?i)(\d+)\s*(?:-|–|—|to)\s*(\d+)"#, in: players)
            .compactMap { $0.count == 3 ? Int($0[2]) : nil }
        let upperBounds = Self.allMatches(#"(?i)up\s+to\s+(\d+)"#, in: players)
            .compactMap { $0.count == 2 ? Int($0[1]) : nil }
        let exactCounts = Self.allMatches(#"(?i)(\d+)\s+players?\b"#, in: players)
            .compactMap { $0.count == 2 ? Int($0[1]) : nil }
        return (versus + ranges + upperBounds + exactCounts).max()
    }

    private static func firstMatch(_ pattern: String, in value: String) -> [String]? {
        allMatches(pattern, in: value).first
    }

    private static func allMatches(_ pattern: String, in value: String) -> [[String]] {
        guard let expression = try? NSRegularExpression(pattern: pattern) else { return [] }
        let range = NSRange(value.startIndex..., in: value)
        return expression.matches(in: value, range: range).map { match in
            (0..<match.numberOfRanges).compactMap { index in
                guard let range = Range(match.range(at: index), in: value) else { return nil }
                return String(value[range])
            }
        }
    }

    static func load() throws -> [Game] {
        guard let url = AppResources.bundle.url(
            forResource: "games", withExtension: "json", subdirectory: "Resources"
        ) else {
            throw CatalogError.missingResource
        }
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode([Game].self, from: data)
    }

    enum CatalogError: LocalizedError {
        case missingResource
        var errorDescription: String? { "The bundled games.json catalog is missing." }
    }
}

enum CatalogLinks: Codable, Hashable {
    struct Part: Codable, Hashable { let url: String; let name: String? }
    case text(String)
    case list([Part])

    var firstURL: String? {
        let candidate: String?
        switch self {
        case .text(let value): candidate = value.components(separatedBy: CharacterSet(charactersIn: ",\n")).first?.trimmingCharacters(in: .whitespaces)
        case .list(let values): candidate = values.first?.url
        }
        guard let candidate, !candidate.isEmpty else { return nil }
        return candidate
    }
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(String.self) { self = .text(value) }
        else if let values = try? container.decode([String].self) { self = .list(values.map { Part(url: $0, name: nil) }) }
        else { self = .list(try container.decode([Part].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .text(let value): try container.encode(value)
        case .list(let values): try container.encode(values)
        }
    }
}
