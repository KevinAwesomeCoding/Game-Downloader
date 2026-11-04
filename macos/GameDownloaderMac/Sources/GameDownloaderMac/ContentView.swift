import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    @Published var games: [Game] = []
    @Published var readiness: ReadinessReport?
    @Published var readinessError: String?
    @Published var isChecking = false
    @Published var isRefreshingCatalog = false
    @Published var isInstallingHelper = false
    @Published var helperInstallMessage: String?
    @Published var catalogError: String?
    @Published var catalogMessage: String?
    @Published var catalogSource = UserDefaults.standard.string(forKey: "catalogSource") ?? "url" {
        didSet {
            UserDefaults.standard.set(catalogSource, forKey: "catalogSource")
            if oldValue != catalogSource { loadCatalog() }
        }
    }
    @Published var catalogURL: String = {
        let current = UserDefaults.standard.string(forKey: "catalogURL")
        let previous = [
            "https://gitlab.com/api/v4/projects/88321703/releases/permalink/latest/downloads/games.json",
            "https://gitlab.com/kevinanil-projects/Game-Downloader/-/releases/permalink/latest/downloads/games.json",
        ]
        if current == nil || previous.contains(current!) {
            return "https://gitlab.com/kevinanil-projects/Game-Downloader/-/raw/main/games.json"
        }
        return current!
    }() {
        didSet { UserDefaults.standard.set(catalogURL, forKey: "catalogURL") }
    }
    @Published var autoUpdateCatalog = UserDefaults.standard.bool(forKey: "autoUpdateCatalog") {
        didSet {
            UserDefaults.standard.set(autoUpdateCatalog, forKey: "autoUpdateCatalog")
            if autoUpdateCatalog && catalogSource == "url" {
                Task { await refreshCatalog() }
            }
        }
    }
    @Published var catalogJSONDraft = ""
    @Published var localCatalogName: String?
    @Published var jobs: [InstallJob] = []
    @Published var ownedInstallations: [OwnedGameInstallation] = []
    @Published var downloadEstimates: [String: String] = [:]
    @Published var actionMessage: String?
    @Published var actionLogPath: String?
    @Published var operationBusy = false
    @Published var rarAvailable = false
    @Published var updateNotice = UpdateNoticeState()
    @Published var updateMessage: String?
    @Published var updateAlertMessage: String?
    @Published var isInstallingUpdate = false
    @Published var updateProgress: Double?
    @Published var shouldQuitForUpdate = false
    @Published var isCheckingForAppUpdate = false
    @Published var selectedBottle = UserDefaults.standard.string(forKey: "selectedBottle") ?? "" {
        didSet { UserDefaults.standard.set(selectedBottle, forKey: "selectedBottle") }
    }
    @Published var gameFolderAccessGranted = UserDefaults.standard.bool(forKey: "gameFolderAccessGranted")
    @Published var gameFolderAccessMessage: String?
    @Published var installFolder = UserDefaults.standard.string(forKey: "installFolder") ?? FileManager.default.homeDirectoryForCurrentUser
        .appending(path: "Games/Game Installer", directoryHint: .isDirectory).path {
        didSet { UserDefaults.standard.set(installFolder, forKey: "installFolder") }
    }
    var installSession: WorkerSession?
    var operationSession: WorkerSession?
    var activeJobID: String?
    var stopIntents: [String: String] = [:]
    var queueWritable = true
    private var downloadSamples: [String: (date: Date, bytes: Int64)] = [:]

    var highballAppDetected: Bool {
        let app = URL(fileURLWithPath: "/Applications/Highball.app", isDirectory: true)
        return Bundle(url: app)?.infoDictionary?["CFBundleShortVersionString"] as? String
            == HighballReadiness.version
    }

    var highballSetupComplete: Bool {
        guard let readiness,
              readiness.status == "ready",
              let runtime = readiness.readiness else { return false }
        return runtime.bottles.contains(where: \.ready)
    }

    var setupStatusMessage: String {
        switch readiness?.status {
        case "no_engines": return "Open Highball and finish installing its Windows engine. Then check setup again."
        case "no_bottles": return "Open Highball and finish creating a Windows environment. Then check setup again."
        case "needs_attention": return "Highball found an environment that needs attention. Open Highball, resolve its engine or renderer setup, then check again."
        default: return "Highball setup is not ready yet. Finish its first-run setup, then check again."
        }
    }

    var helperNeedsSetup: Bool {
        readinessError?.localizedCaseInsensitiveContains("helper") == true
    }

    var updateInstallSafe: Bool {
        !operationBusy && !jobs.contains { $0.isActive || $0.status == "queued" }
    }

    init() {
        updateMessage = AppUpdater.consumeStatus()
        updateAlertMessage = updateMessage
        loadCatalog()
        if catalogSource == "url" && autoUpdateCatalog {
            Task { await refreshCatalog() }
        }
        restoreQueue()
        restoreGameFolderAccess()
    }

    func checkForAppUpdate(manual: Bool = false) async {
        guard !isCheckingForAppUpdate else { return }
        isCheckingForAppUpdate = true
        defer { isCheckingForAppUpdate = false }
        do {
            let update = try await AppUpdater.check()
            if let update { updateNotice.offer(update); updateMessage = nil }
            else if manual { updateAlertMessage = "No compatible update is available." }
        } catch {
            if manual { updateAlertMessage = "Could not check for updates: \(error.localizedDescription)" }
        }
    }

    func installAppUpdate() {
        guard let update = updateNotice.update else { return }
        isInstallingUpdate = true
        Task {
            do {
                try await AppUpdater.install(update) { [weak self] message, progress in
                    self?.updateMessage = message
                    self?.updateProgress = progress
                }
                await MainActor.run {
                    self.shouldQuitForUpdate = true
                }
            }
            catch {
                isInstallingUpdate = false
                updateProgress = nil
                updateMessage = "Update could not be installed: \(error.localizedDescription)"
                updateAlertMessage = updateMessage
            }
        }
    }

    func restoreGameFolderAccess() {
        guard gameFolderAccessGranted else { return }
        let url = URL(fileURLWithPath: installFolder, isDirectory: true)
        let probe = url.appending(path: ".game-installer-access-\(UUID().uuidString)")
        do {
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue else {
                throw CocoaError(.fileNoSuchFile)
            }
            try Data("access check".utf8).write(to: probe, options: .atomic)
            try FileManager.default.removeItem(at: probe)
        } catch {
            try? FileManager.default.removeItem(at: probe)
            gameFolderAccessGranted = false
            UserDefaults.standard.set(false, forKey: "gameFolderAccessGranted")
            gameFolderAccessMessage = "The saved Games folder can no longer be used. Choose it again to restore access. \(error.localizedDescription)"
        }
        scanOwnedGames()
    }

    func requestGameFolderAccess() {
        let panel = NSOpenPanel()
        panel.title = "Choose Your Games Folder"
        panel.message = "Game Installer needs read and write access to store and manage game files in this folder."
        panel.prompt = "Allow Access"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        let preferred = URL(fileURLWithPath: installFolder, isDirectory: true)
        let gamesFolder = FileManager.default.homeDirectoryForCurrentUser.appending(path: "Games", directoryHint: .isDirectory)
        panel.directoryURL = FileManager.default.fileExists(atPath: preferred.deletingLastPathComponent().path)
            ? preferred.deletingLastPathComponent() : gamesFolder
        guard panel.runModal() == .OK, let selected = panel.url else { return }

        let probe = selected.appending(path: ".game-installer-access-\(UUID().uuidString)")
        do {
            try Data("access check".utf8).write(to: probe, options: .atomic)
            try FileManager.default.removeItem(at: probe)
            installFolder = selected.path
            gameFolderAccessGranted = true
            gameFolderAccessMessage = nil
            UserDefaults.standard.set(true, forKey: "gameFolderAccessGranted")
            scanOwnedGames()
        } catch {
            try? FileManager.default.removeItem(at: probe)
            gameFolderAccessGranted = false
            UserDefaults.standard.set(false, forKey: "gameFolderAccessGranted")
            gameFolderAccessMessage = "Game Installer could not create and remove a file in that folder. Choose another folder or check its macOS permissions. (error.localizedDescription)"
        }
    }

    func makeDefaultGamesFolder() {
        let folder = FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "Games/Game Installer", directoryHint: .isDirectory)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let probe = folder.appending(path: ".game-installer-access-\(UUID().uuidString)")
            try Data("access check".utf8).write(to: probe, options: .atomic)
            try FileManager.default.removeItem(at: probe)
            installFolder = folder.path
            gameFolderAccessGranted = true
            gameFolderAccessMessage = nil
            UserDefaults.standard.set(true, forKey: "gameFolderAccessGranted")
            scanOwnedGames()
        } catch {
            gameFolderAccessGranted = false
            UserDefaults.standard.set(false, forKey: "gameFolderAccessGranted")
            gameFolderAccessMessage = "Could not create or use ~/Games/Game Installer. (error.localizedDescription)"
        }
    }

    func scanOwnedGames() {
        guard gameFolderAccessGranted else {
            ownedInstallations = []
            return
        }
        let root = URL(fileURLWithPath: installFolder, isDirectory: true)
        guard let folders = try? FileManager.default.contentsOfDirectory(
            at: root, includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
            options: [.skipsHiddenFiles]
        ) else {
            ownedInstallations = []
            return
        }
        let folderByName = Dictionary(folders.compactMap { folder -> (String, URL)? in
            guard let values = try? folder.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  values.isDirectory == true, values.isSymbolicLink != true,
                  let contents = try? FileManager.default.contentsOfDirectory(atPath: folder.path),
                  !contents.isEmpty else { return nil }
            return (folder.lastPathComponent.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current), folder)
        }, uniquingKeysWith: { first, _ in first })

        let editions = games.flatMap { game in
            [game] + (game.morePlayersGame().map { [$0] } ?? [])
        }
        ownedInstallations = editions.compactMap { game in
            let folderName = Self.gameFolderName(game.name)
                .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
            guard let folder = folderByName[folderName] else { return nil }
            let path = folder.standardizedFileURL.path
            let job = jobs.first { $0.game.id == game.id && $0.installPath.map { URL(fileURLWithPath: $0).standardizedFileURL.path == path } == true }
            return OwnedGameInstallation(game: game, folderURL: folder, job: job)
        }
    }

    private static func gameFolderName(_ name: String) -> String {
        let invalid = CharacterSet(charactersIn: "<>:\"/\\|?*\u{0000}-\u{001F}")
        return name.components(separatedBy: invalid).joined().trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "."))
    }

    func updateDownloadEstimate(jobID: String, status: String, done: Int64, total: Int64) {
        guard status == "downloading", total > 0 else {
            downloadEstimates.removeValue(forKey: jobID)
            downloadSamples.removeValue(forKey: jobID)
            return
        }

        let now = Date()
        guard let previous = downloadSamples[jobID], done >= previous.bytes else {
            downloadSamples[jobID] = (now, done)
            downloadEstimates[jobID] = "Calculating time remaining…"
            return
        }
        let elapsed = now.timeIntervalSince(previous.date)
        guard elapsed >= 1 else { return }

        let bytesPerSecond = Double(done - previous.bytes) / elapsed
        downloadSamples[jobID] = (now, done)
        guard bytesPerSecond > 0 else {
            downloadEstimates[jobID] = "Waiting for download speed…"
            return
        }

        let secondsRemaining = Double(max(total - done, 0)) / bytesPerSecond
        let speed = ByteCountFormatter.string(fromByteCount: max(Int64(bytesPerSecond), 1), countStyle: .file)
        downloadEstimates[jobID] = "\(speed)/s • about \(Self.formatDuration(secondsRemaining)) left"
    }

    private static func formatDuration(_ seconds: Double) -> String {
        let remaining = max(Int(seconds.rounded(.up)), 0)
        if remaining < 60 { return "\(remaining) sec" }
        let minutes = remaining / 60
        if minutes < 60 { return "\(minutes) min \(remaining % 60) sec" }
        let hours = minutes / 60
        return "\(hours) hr \(minutes % 60) min"
    }

    func loadCatalog() {
        do {
            if catalogSource == "local" || catalogSource == "paste" {
                let data = try Data(contentsOf: managedCatalogURL())
                games = try Self.decodeCatalog(data)
                catalogJSONDraft = String(decoding: data, as: UTF8.self)
                catalogError = nil
            } else if catalogSource == "cached", let data = try? Data(contentsOf: managedCatalogURL()) {
                games = try Self.decodeCatalog(data)
                catalogError = nil
            } else if catalogSource == "url", let data = try? Data(contentsOf: managedCatalogURL()) {
                games = try Self.decodeCatalog(data)
                catalogError = nil
            } else {
                // Keep bundled catalog order: it represents when entries were added.
                games = try Game.load()
                catalogError = nil
            }
            scanOwnedGames()
        } catch {
            do {
                games = try Game.load()
                scanOwnedGames()
                catalogError = "Could not load the selected catalog; showing the bundled catalog. \(error.localizedDescription)"
            } catch {
                catalogError = error.localizedDescription
            }
        }
    }

    func refreshCatalog() async {
        isRefreshingCatalog = true
        catalogError = nil
        defer { isRefreshingCatalog = false }

        do {
            if catalogSource == "local" || catalogSource == "paste" || catalogSource == "cached" {
                let data = try Data(contentsOf: managedCatalogURL())
                games = try Self.decodeCatalog(data)
                catalogJSONDraft = String(decoding: data, as: UTF8.self)
                scanOwnedGames()
                catalogMessage = "Loaded \(games.count) games from your local catalog."
                return
            }
            guard let url = Self.normalizedCatalogURL(catalogURL) else {
                throw CatalogRefreshError.invalidURL
            }
            var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                                     timeoutInterval: 20)
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let response = response as? HTTPURLResponse,
                  (200..<300).contains(response.statusCode) else {
                let code = (response as? HTTPURLResponse)?.statusCode.description ?? "unknown"
                throw CatalogRefreshError.httpStatus(code)
            }
            let refreshed = try Self.decodeCatalog(data)
            try data.write(to: managedCatalogURL(), options: .atomic)
            // Preserve games.json order so the library's "added" sort reflects the catalog.
            games = refreshed
            scanOwnedGames()
            catalogMessage = "Catalog refreshed: \(games.count) games."
        } catch {
            catalogError = "Could not refresh the selected catalog. Your current game list was kept. \(error.localizedDescription)"
        }
    }

    func chooseLocalCatalog() {
        let panel = NSOpenPanel()
        panel.title = "Choose games.json"
        panel.prompt = "Use Catalog"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let data = try Data(contentsOf: url)
            let parsed = try Self.decodeCatalog(data)
            try data.write(to: managedCatalogURL(), options: .atomic)
            localCatalogName = url.lastPathComponent
            catalogJSONDraft = String(decoding: data, as: UTF8.self)
            catalogSource = "local"
            games = parsed
            scanOwnedGames()
            catalogMessage = "Loaded \(games.count) games from \(url.lastPathComponent)."
            catalogError = nil
        } catch {
            catalogError = "That file could not be used as a game catalog. \(error.localizedDescription)"
        }
    }

    func applyPastedCatalog() {
        do {
            let data = Data(catalogJSONDraft.utf8)
            let parsed = try Self.decodeCatalog(data)
            try data.write(to: managedCatalogURL(), options: .atomic)
            if catalogSource != "local" { catalogSource = "paste" }
            games = parsed
            scanOwnedGames()
            catalogMessage = "Applied your JSON catalog with \(games.count) games."
            catalogError = nil
        } catch {
            catalogError = "This JSON catalog could not be used. \(error.localizedDescription)"
        }
    }

    func saveCatalogEdits() { applyPastedCatalog() }

    func saveCatalogLink() {
        guard let url = Self.normalizedCatalogURL(catalogURL) else {
            catalogError = "Enter an HTTPS games.json link or Pastebin URL."
            return
        }
        catalogURL = url.absoluteString
        catalogSource = "url"
        catalogMessage = "Catalog link saved."
        Task { await refreshCatalog() }
    }

    private func managedCatalogURL() throws -> URL {
        let base = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                               appropriateFor: nil, create: true)
        let folder = base.appending(path: "Game-Downloader", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder.appending(path: "games.json")
    }

    private static func normalizedCatalogURL(_ value: String) -> URL? {
        var candidate = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty else { return nil }
        if let parsed = URLComponents(string: candidate),
           let host = parsed.host?.lowercased(), host == "pastebin.com" || host == "www.pastebin.com" {
            let parts = parsed.path.split(separator: "/").map(String.init)
            guard let id = parts.last else { return nil }
            candidate = "https://pastebin.com/raw/\(id)"
        }
        guard let url = URL(string: candidate), url.scheme?.lowercased() == "https" else { return nil }
        return url
    }

    private static func decodeCatalog(_ data: Data) throws -> [Game] {
        let decoded = try JSONDecoder().decode([Game].self, from: data)
        guard !decoded.isEmpty else { throw CatalogRefreshError.empty }
        guard Set(decoded.map(\.id)).count == decoded.count else { throw CatalogRefreshError.duplicateIDs }
        return decoded
    }

    private enum CatalogRefreshError: LocalizedError {
        case empty
        case duplicateIDs
        case invalidURL
        case httpStatus(String)

        var errorDescription: String? {
            switch self {
            case .empty: return "The online catalog was empty."
            case .duplicateIDs: return "The online catalog contains duplicate game IDs."
            case .invalidURL: return "Enter a valid HTTPS link to a raw games.json file or Pastebin paste."
            case .httpStatus(let code): return "The catalog server returned HTTP \(code)."
            }
        }
    }

    func checkHighball() async {
        isChecking = true
        readinessError = nil
        defer { isChecking = false }
        do {
            let result = try await HighballReadiness.check()
            readiness = result
            if result.readiness == nil {
                readinessError = result.detail ?? "Highball helper checks need attention."
            } else if let bottles = result.readiness?.bottles,
                      !bottles.contains(where: { $0.name == selectedBottle }) {
                let readyBottles = bottles.filter(\.ready)
                if readyBottles.count == 1 {
                    // Highball creates the first environment with its own name; don't assume "Games".
                    selectedBottle = readyBottles[0].name
                }
            }
        } catch {
            readiness = nil
            readinessError = error.localizedDescription
        }
    }

    func installHighballHelper() async {
        isInstallingHelper = true
        helperInstallMessage = nil
        defer { isInstallingHelper = false }
        do {
            let message = try await Task.detached(priority: .userInitiated) {
                try HighballHelperInstaller.install()
            }.value
            helperInstallMessage = message
            await checkHighball()
        } catch {
            readiness = nil
            readinessError = error.localizedDescription
            helperInstallMessage = nil
        }
    }

    func chooseInstallFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose Game Installer Folder"
        panel.message = "Choose where downloaded games should be stored. Game Installer will verify that it can manage files there."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: installFolder, isDirectory: true)
        if panel.runModal() == .OK, let url = panel.url {
            let probe = url.appending(path: ".game-installer-access-\(UUID().uuidString)")
            do {
                try Data("access check".utf8).write(to: probe, options: .atomic)
                try FileManager.default.removeItem(at: probe)
                installFolder = url.path
                gameFolderAccessGranted = true
                gameFolderAccessMessage = nil
                scanOwnedGames()
                UserDefaults.standard.set(true, forKey: "gameFolderAccessGranted")
            } catch {
                try? FileManager.default.removeItem(at: probe)
                gameFolderAccessMessage = "Could not use that folder: \(error.localizedDescription)"
            }
        }
    }
}

struct OwnedGameInstallation: Identifiable {
    let game: Game
    let folderURL: URL
    let job: InstallJob?

    var id: String { game.id }
}

private enum InstallerPage: String, CaseIterable {
    case library = "Library"
    case owned = "Games Owned"
    case downloads = "Downloads"
    case settings = "Settings"
}

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var page: InstallerPage = .library
    @State private var search = ""
    @AppStorage("librarySortOrder") private var librarySortOrder = LibrarySortOrder.oldestFirst.rawValue
    @State private var selectedGame: Game?
    @State private var cleanupTarget: InstallJob?
    @State private var showingUpdateDetails = false

    private let columns = [GridItem(.adaptive(minimum: 285, maximum: 360), spacing: 22)]

    private var filteredGames: [Game] {
        let matches = model.games.filter {
            LibrarySearch.matches(query: search, name: $0.name, description: $0.description)
        }
        let order = LibrarySortOrder(rawValue: librarySortOrder) ?? .oldestFirst
        switch order {
        case .oldestFirst:
            return matches
        case .newestFirst:
            return Array(matches.reversed())
        case .nameAscending:
            return matches.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        case .nameDescending:
            return matches.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedDescending }
        case .sizeAscending:
            return sortByCatalogMetric(matches, descending: false, key: \.downloadSizeInBytes)
        case .sizeDescending:
            return sortByCatalogMetric(matches, descending: true, key: \.downloadSizeInBytes)
        case .playersDescending:
            return sortByCatalogMetric(matches, descending: true, key: \.listedMaximumPlayers)
        case .playersAscending:
            return sortByCatalogMetric(matches, descending: false, key: \.listedMaximumPlayers)
        case .installedFirst:
            let installedIDs = Set(model.ownedInstallations.map(\.game.id))
            let catalogPositions = Dictionary(uniqueKeysWithValues: model.games.enumerated().map { ($0.element.id, $0.offset) })
            return matches.sorted {
                let leftInstalled = installedIDs.contains($0.id) || installedIDs.contains($0.id + "--more-players")
                let rightInstalled = installedIDs.contains($1.id) || installedIDs.contains($1.id + "--more-players")
                if leftInstalled != rightInstalled { return leftInstalled }
                return (catalogPositions[$0.id] ?? .max) < (catalogPositions[$1.id] ?? .max)
            }
        }
    }

    private func sortByCatalogMetric<Value: Comparable>(
        _ games: [Game], descending: Bool, key: (Game) -> Value?
    ) -> [Game] {
        let catalogPositions = Dictionary(uniqueKeysWithValues: model.games.enumerated().map { ($0.element.id, $0.offset) })
        return games.sorted { left, right in
            let leftValue = key(left)
            let rightValue = key(right)
            if let leftValue, let rightValue, leftValue != rightValue {
                return descending ? leftValue > rightValue : leftValue < rightValue
            }
            if leftValue != nil, rightValue == nil { return true }
            if leftValue == nil, rightValue != nil { return false }
            return (catalogPositions[left.id] ?? .max) < (catalogPositions[right.id] ?? .max)
        }
    }

    var body: some View {
        Group {
        if !model.gameFolderAccessGranted {
            fileAccessSetup
        } else {
        VStack(spacing: 0) {
            header
            Divider().overlay(Color.white.opacity(0.08))
            if let message = model.actionMessage {
                HStack {
                    if model.operationBusy { ProgressView().controlSize(.small) }
                    Text(message).font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    if let path = model.actionLogPath, message.contains(path) {
                        Button("Open log") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }.controlSize(.small)
                    }
                    if !model.operationBusy { Button { model.actionMessage = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain) }
                }.padding(12).background(Color.teal.opacity(0.12))
            }
            if model.highballSetupComplete {
                pageContent
            } else {
                firstLaunchSetup
            }
        }
        .frame(minWidth: 960, minHeight: 680)
        .background(Color(red: 0.055, green: 0.071, blue: 0.11))
        .preferredColorScheme(.dark)
        .sheet(item: $selectedGame) { game in
            GameDetailsSheet(game: game, model: model) { selected in
                model.enqueue(selected)
                selectedGame = nil
                if !selected.isSteamEntry { page = .downloads }
            }
                .frame(minWidth: 720, idealWidth: 820, minHeight: 620, idealHeight: 760)
        }
        .task {
            guard model.gameFolderAccessGranted else { return }
            await model.checkHighball()
            if model.highballSetupComplete && !model.operationBusy {
                model.beginOperation(["tools"], jobID: nil, bottle: model.selectedBottle)
            }
        }
        }
        }
        .sheet(isPresented: $showingUpdateDetails, onDismiss: { model.updateNotice.later() }) {
            if let update = model.updateNotice.update {
                UpdateDetailsView(update: update, model: model) {
                    model.updateNotice.later()
                    showingUpdateDetails = false
                }
                .frame(minWidth: 480, idealWidth: 560, minHeight: 340, idealHeight: 440)
            }
        }
        .task {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled else { return }
            await model.checkForAppUpdate()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(120))
                guard !Task.isCancelled else { return }
                await model.checkForAppUpdate()
            }
        }
        .onChange(of: model.shouldQuitForUpdate) { _, shouldQuit in
            guard shouldQuit else { return }
            showingUpdateDetails = false
            // Let SwiftUI dismiss the sheet before sending the normal quit request.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                NSApp.terminate(nil)
            }
        }
        .alert("Update status", isPresented: Binding(
            get: { model.updateAlertMessage != nil },
            set: { if !$0 { model.updateAlertMessage = nil } }
        )) {
            Button("OK") { model.updateAlertMessage = nil }
        } message: {
            Text(model.updateAlertMessage ?? "")
        }
    }

    private var fileAccessSetup: some View {
        VStack(spacing: 20) {
            Image(systemName: "folder.badge.gearshape")
                .font(.system(size: 48)).foregroundStyle(.teal)
            Text("Choose a Games folder")
                .font(.largeTitle.bold())
            Text("Game Installer needs access to one folder so it can save game files and manage its temporary install files. Choose a location to continue.")
                .font(.body).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 520)
            VStack(alignment: .leading, spacing: 8) {
                Label("Games stay in the folder you choose; Highball entries point to them.", systemImage: "checkmark.circle")
                Label("The installer checks that it can create and remove its own files there.", systemImage: "checkmark.circle")
                Label("It only removes verified download archives when you choose Clean download files. Installed games and the app are kept.", systemImage: "checkmark.circle")
                Label("No Full Disk Access, Accessibility, or Automation permission is needed.", systemImage: "checkmark.circle")
            }
            .font(.callout).foregroundStyle(.secondary)
            .frame(maxWidth: 540, alignment: .leading)
            if let message = model.gameFolderAccessMessage {
                Text(message).font(.callout).foregroundStyle(.orange)
                    .frame(maxWidth: 540).textSelection(.enabled)
            }
            HStack(spacing: 12) {
                Button("Make Games Folder") { model.makeDefaultGamesFolder() }
                    .buttonStyle(.borderedProminent).tint(.teal).controlSize(.large)
                Button("Choose Existing Folder…") { model.requestGameFolderAccess() }
                    .buttonStyle(.bordered).controlSize(.large)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(36)
        .background(Color(red: 0.055, green: 0.071, blue: 0.11))
    }

    private var header: some View {
        HStack(spacing: 22) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Game Installer")
                    .font(.system(size: 21, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                Text("Mac library · Highball")
                    .font(.caption)
                    .foregroundStyle(Color(red: 0.55, green: 0.59, blue: 0.68))
            }
            Spacer(minLength: 12)
            if model.highballSetupComplete {
                HStack(spacing: 6) {
                    ForEach(InstallerPage.allCases, id: \.self) { item in
                        Button {
                            page = item
                        } label: {
                            Text(item == .downloads && !model.jobs.isEmpty ? "Downloads (\(model.jobs.count))" :
                                (item == .owned && !model.ownedInstallations.isEmpty ? "Games Owned (\(model.ownedInstallations.count))" : item.rawValue))
                                .font(.system(size: 13, weight: .semibold))
                                .padding(.horizontal, 13).padding(.vertical, 9)
                                .background(page == item ? Color.white.opacity(0.12) : .clear,
                                            in: RoundedRectangle(cornerRadius: 8))
                                .foregroundStyle(page == item ? .white : Color.white.opacity(0.72))
                        }
                        .buttonStyle(.plain)
                    }
                }
                Button { model.requestSteam("steam://install/480") } label: {
                    Label("Get Spacewar", systemImage: "gamecontroller.fill")
                }
                .buttonStyle(InstallerButtonStyle(tint: Color(red: 0.24, green: 0.38, blue: 0.62)))
                .help("Hand installation to Windows Steam inside the selected Highball bottle")
                .disabled(model.operationBusy || model.selectedBottle.isEmpty)
                Button { model.requestSteam("steam://open/main") } label: {
                    Label("Launch Steam", systemImage: "play.fill")
                }
                .buttonStyle(InstallerButtonStyle(tint: Color(red: 0.20, green: 0.25, blue: 0.36)))
                .help("Open Windows Steam inside the selected Highball bottle")
                .disabled(model.operationBusy || model.selectedBottle.isEmpty)
            }
            updateCheckButton
            Button {
                Task {
                    await model.refreshCatalog()
                    await model.checkHighball()
                }
            } label: {
                if model.isRefreshingCatalog || model.isChecking {
                    ProgressView().controlSize(.small)
                } else {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
            }
            .buttonStyle(InstallerButtonStyle(tint: Color(red: 0.08, green: 0.78, blue: 0.67), darkText: true))
            .disabled(model.isRefreshingCatalog || model.isChecking)
        }
        .padding(.horizontal, 25).padding(.vertical, 16)
        .background(Color(red: 0.075, green: 0.09, blue: 0.145))
    }

    private var updateCheckButton: some View {
        let update = model.updateNotice.update
        return Button {
            if update != nil {
                model.updateNotice.openDetails()
                showingUpdateDetails = true
            } else {
                Task { await model.checkForAppUpdate(manual: true) }
            }
        } label: {
            if model.isCheckingForAppUpdate && update == nil {
                ProgressView().controlSize(.small)
            } else if let update {
                Label("Update available · \(update.version)", systemImage: "arrow.down.circle.fill")
            } else {
                Label("Check updates", systemImage: "arrow.clockwise")
            }
        }
        .buttonStyle(InstallerButtonStyle(tint: update == nil
            ? Color(red: 0.20, green: 0.25, blue: 0.36)
            : Color(red: 0.95, green: 0.61, blue: 0.18), darkText: update != nil))
        .disabled(model.isCheckingForAppUpdate && update == nil)
        .help(update == nil ? "Check GitLab for a compatible app update" : "Show release notes and update options")
    }

    @ViewBuilder private var pageContent: some View {
        switch page {
        case .library: libraryPage
        case .owned: ownedGamesPage
        case .downloads: downloadsPage
        case .settings: settingsPage
        }
    }

    private var firstLaunchSetup: some View {
        VStack(alignment: .leading, spacing: 22) {
            Label("Set up Highball to continue", systemImage: "wineglass")
                .font(.system(size: 26, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
            Text("Game Installer needs Highball and its matching helper before it can download or install games. This one-time setup makes sure Windows games have an environment to install into.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            setupStep(number: "1", title: "Install and open Highball", detail: "Install Highball 0.10.22, open it, and finish creating a Windows environment.") {
                if let url = URL(string: "https://gethighball.com/docs/install/") {
                    NSWorkspace.shared.open(url)
                }
            } actionLabel: {
                Label("Get Highball", systemImage: "arrow.up.right.square")
            }

            setupStep(number: "2", title: "Install the Game Installer helper", detail: "The matching helper is included with this app. Install it once after Highball is installed.") {
                if model.helperNeedsSetup {
                    Task { await model.installHighballHelper() }
                }
            } actionLabel: {
                if model.helperNeedsSetup {
                    if model.isInstallingHelper {
                        Label("Installing helper…", systemImage: "arrow.down.circle")
                    } else {
                        Label("Install or repair helper", systemImage: "arrow.down.circle")
                    }
                } else {
                    Label(model.highballAppDetected ? "Helper verified" : "Waiting for Highball", systemImage: model.highballAppDetected ? "checkmark.circle.fill" : "clock")
                }
            }
            .disabled(!model.helperNeedsSetup || model.isInstallingHelper || model.isChecking || !model.highballAppDetected)

            if let error = model.readinessError {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                if error.localizedCaseInsensitiveContains("Python 3 was not found") {
                    Text("Python 3 is also required by the Mac download worker. Install it, then check setup again.")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("Get Python 3") {
                        if let url = URL(string: "https://www.python.org/downloads/macos/") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                }
            } else if let status = model.readiness?.status, status != "ready" {
                Text(model.setupStatusMessage)
                    .font(.callout).foregroundStyle(.orange)
            }
            if let message = model.helperInstallMessage {
                Text(message).font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                Button {
                    Task { await model.checkHighball() }
                } label: {
                    if model.isChecking { ProgressView().controlSize(.small) }
                    else { Label("Check setup", systemImage: "arrow.clockwise") }
                }
                .disabled(model.isChecking || model.isInstallingHelper)
                Spacer()
                Text("Downloads unlock when Highball, the helper, and a Windows environment are ready.")
                    .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.trailing)
            }
        }
        .padding(34)
        .frame(maxWidth: 690, alignment: .leading)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func setupStep<LabelView: View>(number: String, title: String, detail: String,
                                             action: @escaping () -> Void,
                                             @ViewBuilder actionLabel: () -> LabelView) -> some View {
        HStack(alignment: .top, spacing: 15) {
            Text(number).font(.headline).foregroundStyle(.black)
                .frame(width: 30, height: 30).background(Color.teal, in: Circle())
            VStack(alignment: .leading, spacing: 7) {
                Text(title).font(.headline).foregroundStyle(.white)
                Text(detail).font(.callout).foregroundStyle(.secondary)
                Button(action: action, label: actionLabel).buttonStyle(.borderedProminent).tint(.teal)
            }
            Spacer(minLength: 0)
        }
        .padding(18)
        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white.opacity(0.08)))
    }

    private var libraryPage: some View {
        VStack(spacing: 0) {
            if let error = model.readinessError {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "wineglass").foregroundStyle(.orange)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Finish Highball setup once").font(.headline)
                        Text(error).font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Setup steps") { page = .settings }.buttonStyle(.bordered)
                }
                .padding(14)
                .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 25).padding(.top, 14)
            }
            HStack(spacing: 12) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search games…", text: $search)
                    .textFieldStyle(.plain)
                if !search.isEmpty {
                    Button { search = "" } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }.buttonStyle(.plain)
                }
            }
            .padding(11)
            .background(Color(red: 0.04, green: 0.055, blue: 0.095), in: RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.09)))
            .padding(.horizontal, 25).padding(.top, 18).padding(.bottom, 15)

            HStack {
                Text("Game library").font(.title2.bold()).foregroundStyle(.white)
                Text("\(filteredGames.count) games")
                    .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Color.white.opacity(0.08), in: Capsule())
                Spacer()
                Menu {
                    Picker("Sort by", selection: $librarySortOrder) {
                        ForEach(LibrarySortOrder.allCases) { order in
                            Text(order.label).tag(order.rawValue)
                        }
                    }
                } label: {
                    Label("Sort: \(LibrarySortOrder(rawValue: librarySortOrder)?.shortLabel ?? LibrarySortOrder.oldestFirst.shortLabel)", systemImage: "arrow.up.arrow.down")
                }
                .menuStyle(.borderlessButton)
                if let error = model.catalogError {
                    Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        .font(.caption)
                }
            }
            .padding(.horizontal, 25).padding(.bottom, 14)

            if filteredGames.isEmpty {
                ContentUnavailableView(search.isEmpty ? "No games available" : "No matching games",
                                       systemImage: "gamecontroller",
                                       description: Text("Try another search."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 18) {
                        ForEach(filteredGames) { game in
                            GameCard(game: game) { selectedGame = game }
                        }
                    }
                    .padding(.horizontal, 25).padding(.bottom, 25)
                }
            }
            footer
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Image(systemName: "folder").foregroundStyle(.teal)
            Text("Install folder")
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(model.installFolder).lineLimit(1).truncationMode(.middle)
                .font(.caption.monospaced()).foregroundStyle(.white.opacity(0.75))
            Button("Choose…", action: model.chooseInstallFolder).buttonStyle(.plain)
                .foregroundStyle(.teal).font(.caption.weight(.semibold))
            Spacer()
            Label("Local Mac demo", systemImage: "apple.logo")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 25).padding(.vertical, 12)
        .background(Color(red: 0.075, green: 0.09, blue: 0.145))
    }

    private var downloadsPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Downloads").font(.largeTitle.bold())
            Text("One install runs at a time. Pause, cancel, and retry preserve download data. Archive extraction restarts safely from staging.")
                .font(.callout).foregroundStyle(.secondary)
            if model.jobs.isEmpty {
                ContentUnavailableView("No downloads yet", systemImage: "arrow.down.circle",
                                       description: Text("Choose a game from Library to install it."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 14) {
                        ForEach(model.jobs) { job in
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text(job.game.name).font(.headline)
                                    Spacer()
                                    Text(job.status.capitalized).font(.caption).foregroundStyle(job.status == "failed" ? .orange : .teal)
                                }
                                Text(job.message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                                if job.isActive {
                                    if job.total > 0 {
                                        ProgressView(value: min(Double(job.done), Double(job.total)), total: Double(job.total))
                                        Text(job.status == "downloading" ? "\(ByteCountFormatter.string(fromByteCount: job.done, countStyle: .file)) / \(ByteCountFormatter.string(fromByteCount: job.total, countStyle: .file))" : "\(job.done) of \(job.total) files")
                                            .font(.caption).foregroundStyle(.secondary)
                                        if job.status == "downloading", let estimate = model.downloadEstimates[job.id] {
                                            Text(estimate).font(.caption.weight(.medium)).foregroundStyle(.teal)
                                        }
                                    } else { ProgressView().progressViewStyle(.linear) }
                                }
                                Text(job.installPath ?? job.installFolder).font(.caption.monospaced()).foregroundStyle(.secondary)
                                    .lineLimit(2).textSelection(.enabled)
                                HStack {
                                    if job.isActive || job.status == "queued" {
                                        Button("Pause") { model.stopJob(job.id, cancel: false) }.disabled(job.status == "stopping")
                                        Button("Cancel") { model.stopJob(job.id, cancel: true) }.disabled(job.status == "stopping")
                                    } else if !job.isInstalled {
                                        Button(job.status == "failed" ? "Retry" : "Resume") { model.retryJob(job.id) }
                                    }
                                    if let path = job.installPath {
                                        Button("Show in Finder") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                                        if job.recipeAvailable {
                                            Button("Add with recorded recipe") { model.runHighball(job) }.disabled(model.operationBusy)
                                        }
                                        Button(job.registered ? "Change .exe" : "Choose .exe & add") { model.chooseExecutable(job) }.disabled(model.operationBusy)
                                        if job.registered {
                                            Button("Play in Highball") { model.runHighball(job, launch: true) }.disabled(model.operationBusy)
                                            Text(job.bottle ?? "").font(.caption).foregroundStyle(.secondary)
                                        Button("Clean download files") { cleanupTarget = job }
                                            .disabled(model.operationBusy)
                                            .help("Remove verified archive copies; keep the installed game and its receipt.")
                                        }
                                    }
                                    Button("Download files") { NSWorkspace.shared.open(AppModel.supportFolder.appending(path: job.id)) }
                                        .disabled(!FileManager.default.fileExists(atPath: AppModel.supportFolder.appending(path: job.id).path))
                                    if !job.isActive && job.status != "queued" {
                                        Button("Remove from list") { model.removeJob(job.id) }
                                            .disabled(model.activeJobID == job.id || model.operationBusy)
                                            .help("Installed files and download data remain on disk.")
                                    }
                                }.controlSize(.small)
                                if job.recipeAvailable {
                                    Text("Recorded local settings match this archive build. Install location and full gameplay still need verification; multiplayer checks are separate.")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
            }
        }
        .padding(28)
        .confirmationDialog("Remove downloaded archive files?", isPresented: Binding(
            get: { cleanupTarget != nil }, set: { if !$0 { cleanupTarget = nil } }), presenting: cleanupTarget) { job in
                Button("Remove Download Files", role: .destructive) {
                    model.cleanDownloads(job)
                    cleanupTarget = nil
                }
                Button("Cancel", role: .cancel) { cleanupTarget = nil }
            } message: { job in
                Text("For \(job.game.name), remove only archive files verified against the completed install receipt. The installed game, partial downloads, and receipt stay.")
            }
    }

    private var ownedGamesPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Games Owned").font(.largeTitle.bold())
                    Text("Games found in your selected Games folder.").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button { model.scanOwnedGames() } label: {
                    Label("Scan folder", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.bordered)
            }
            if model.ownedInstallations.isEmpty {
                ContentUnavailableView("No downloaded catalog games found", systemImage: "folder",
                                       description: Text("Game Installer checks the selected Games folder. Download a game or choose Scan folder to check again."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 18) {
                        ForEach(model.ownedInstallations) { installation in
                            OwnedGameCard(installation: installation,
                                          play: { model.playOwnedGame(installation) },
                                          showInFinder: { NSWorkspace.shared.activateFileViewerSelecting([installation.folderURL]) })
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.horizontal, 2)
                    .padding(.bottom, 24)
                }
            }
        }
        .padding(28)
        .onAppear { model.scanOwnedGames() }
    }

    private var settingsPage: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Settings").font(.largeTitle.bold())
                GroupBox("Install location") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(model.installFolder).lineLimit(2).textSelection(.enabled)
                            .font(.callout.monospaced()).foregroundStyle(.secondary)
                        Button("Choose folder…", action: model.chooseInstallFolder)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Game catalog") {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("Catalog source", selection: $model.catalogSource) {
                            Text("Game-Downloader online catalog").tag("url")
                            Text("Local games.json file").tag("local")
                            Text("Paste or edit JSON").tag("paste")
                            Text("Saved catalog (offline)").tag("cached")
                        }
                        if model.catalogSource == "url" {
                            TextField("HTTPS link or Pastebin URL", text: $model.catalogURL)
                                .textFieldStyle(.roundedBorder)
                            Button("Save link and refresh") { model.saveCatalogLink() }
                            Toggle("Automatically update from this link when Game Installer opens",
                                   isOn: $model.autoUpdateCatalog)
                            Text("Pastebin links are converted to their raw view. Other links must point directly to a games.json file and use HTTPS.")
                                .font(.caption).foregroundStyle(.secondary)
                        } else if model.catalogSource == "local" {
                            HStack {
                                Button("Choose games.json…") { model.chooseLocalCatalog() }
                                if let name = model.localCatalogName { Text(name).font(.caption).foregroundStyle(.secondary) }
                            }
                            TextEditor(text: $model.catalogJSONDraft)
                                .font(.system(.caption, design: .monospaced))
                                .frame(minHeight: 180)
                                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.secondary.opacity(0.35)))
                            Button("Save edits to local catalog") { model.saveCatalogEdits() }
                        } else if model.catalogSource == "paste" {
                            Text("Paste a games.json array below, edit it here, then apply it. The catalog is saved in Game Installer's application support folder.")
                                .font(.caption).foregroundStyle(.secondary)
                            TextEditor(text: $model.catalogJSONDraft)
                                .font(.system(.caption, design: .monospaced))
                                .frame(minHeight: 180)
                                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.secondary.opacity(0.35)))
                            HStack {
                                Button("Choose games.json…") { model.chooseLocalCatalog() }
                                Button("Apply pasted JSON") { model.applyPastedCatalog() }
                            }
                        } else {
                            Text("Uses the last saved catalog without checking the network.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        HStack {
                            Button {
                                Task { await model.refreshCatalog() }
                            } label: {
                                if model.isRefreshingCatalog { ProgressView().controlSize(.small) }
                                else { Label("Refresh catalog now", systemImage: "arrow.clockwise") }
                            }
                            .disabled(model.isRefreshingCatalog)
                            if let message = model.catalogMessage {
                                Text(message).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        if let error = model.catalogError {
                            Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                readinessCard
                GroupBox("Game launch bottle") {
                    VStack(alignment: .leading, spacing: 10) {
                        Picker("Highball bottle", selection: $model.selectedBottle) {
                            Text(model.selectedBottle.isEmpty ? "Choose a bottle" : model.selectedBottle).tag(model.selectedBottle)
                            ForEach((model.readiness?.readiness?.bottles ?? []).filter { $0.name != model.selectedBottle }, id: \.name) { bottle in
                                Text(bottle.name).tag(bottle.name)
                            }
                        }
                        Text("Steam's executable is detected and configured in Highball > Settings > Environments > Environment settings > Advanced > Steam executable.")
                            .font(.caption).foregroundStyle(.secondary)
                        Text("Registration adds a separate Game Installer pin. Close Highball before adding programs to avoid stale settings. Existing pins are never replaced.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Open Highball") {
                            let app = URL(fileURLWithPath: "/Applications/Highball.app")
                            if FileManager.default.fileExists(atPath: app.path) {
                                NSWorkspace.shared.open(app)
                            } else if let url = URL(string: "https://gethighball.com/docs/install/") {
                                NSWorkspace.shared.open(url)
                            }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Archive support") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(model.rarAvailable ? "ZIP, RAR, and 7z extraction available" : "ZIP built in · bundled RAR/7z helpers unavailable")
                        Text("RAR and 7z support is included in the app and works without Homebrew. Complete split RAR sets use the bundled UnRAR helper. Unsupported methods keep downloaded files so you can retry.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Check archive tools") { model.beginOperation(["tools"], jobID: nil, bottle: model.selectedBottle) }
                            .disabled(model.operationBusy)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("App updates") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Checks GitLab for updates at launch and every two minutes. Downloads are verified with SHA-256 before installation.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Check for updates") { Task { await model.checkForAppUpdate(manual: true) } }
                            .disabled(model.isInstallingUpdate || model.isCheckingForAppUpdate)
                        if model.isInstallingUpdate { ProgressView("Preparing update…") }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Highball integration") {
                    Text("After installation, Game Installer looks for the game executable, preferring a match to the game title, and adds it to the selected Highball bottle. If it cannot choose confidently, use Choose .exe & add; after registration, Change .exe lets you correct the selection. Recorded compatibility settings apply only to matching tested builds; other games use Highball defaults plus any detected Wine DLL-loading overrides.")
                        .foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(28)
        }
    }

    private var readinessCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label("Highball readiness", systemImage: "wineglass")
                        .font(.headline)
                    Spacer()
                    Button {
                        Task { await model.checkHighball() }
                    } label: {
                        if model.isChecking { ProgressView().controlSize(.small) }
                        else { Label("Check again", systemImage: "arrow.clockwise") }
                    }
                    .disabled(model.isChecking)
                }
                if let report = model.readiness, let runtime = report.readiness {
                    Label(report.status == "ready" ? "Ready" : "Needs attention",
                          systemImage: report.status == "ready" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(report.status == "ready" ? .green : .orange)
                    ForEach(runtime.bottles, id: \.name) { bottle in
                        Text("\(bottle.name) · \(bottle.renderer) · \(bottle.ready ? "ready" : bottle.issues.joined(separator: ", "))")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                } else if let error = model.readinessError {
                    Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    if error.localizedCaseInsensitiveContains("Highball 0.10.22 was not found") {
                        Text("Install Highball, open it, and press Get started. Highball creates your first Windows environment during setup. Return here and choose Check again when it is ready.")
                            .font(.callout).foregroundStyle(.secondary)
                        Button("Get Highball") {
                            if let url = URL(string: "https://gethighball.com/docs/install/") {
                                NSWorkspace.shared.open(url)
                            }
                        }
                    } else if error.localizedCaseInsensitiveContains("helper") {
                        Text("Game Installer includes the matching Highball helper and its source license. Install it once to enable Highball checks and game registration.")
                            .font(.callout).foregroundStyle(.secondary)
                        Button {
                            Task { await model.installHighballHelper() }
                        } label: {
                            if model.isInstallingHelper {
                                Label("Installing helper…", systemImage: "arrow.down.circle")
                            } else {
                                Label("Install or repair helper", systemImage: "arrow.down.circle")
                            }
                        }
                        .disabled(model.isInstallingHelper || model.isChecking)
                    }
                } else {
                    Text("Checking Highball, its engine, and bottles…").foregroundStyle(.secondary)
                }
                if let message = model.helperInstallMessage {
                    Text(message).font(.callout).foregroundStyle(.secondary)
                }
                if let runtime = model.readiness?.readiness, runtime.bottles.isEmpty {
                    Text("No environment is ready yet. Finish Highball’s first-run setup, then check again.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label("Highball", systemImage: "wineglass")
        }
    }
}

private enum LibrarySortOrder: String, CaseIterable, Identifiable {
    case oldestFirst
    case newestFirst
    case nameAscending
    case nameDescending
    case sizeAscending
    case sizeDescending
    case playersDescending
    case playersAscending
    case installedFirst

    var id: String { rawValue }

    var label: String {
        switch self {
        case .oldestFirst: "Oldest added first"
        case .newestFirst: "Newest added first"
        case .nameAscending: "Name: A to Z"
        case .nameDescending: "Name: Z to A"
        case .sizeAscending: "Download size: smallest first"
        case .sizeDescending: "Download size: largest first"
        case .playersDescending: "Player count: most first"
        case .playersAscending: "Player count: fewest first"
        case .installedFirst: "Installed games first"
        }
    }

    var shortLabel: String {
        switch self {
        case .oldestFirst: "Oldest added"
        case .newestFirst: "Newest added"
        case .nameAscending: "Name A–Z"
        case .nameDescending: "Name Z–A"
        case .sizeAscending: "Smallest download"
        case .sizeDescending: "Largest download"
        case .playersDescending: "Most players"
        case .playersAscending: "Fewest players"
        case .installedFirst: "Installed first"
        }
    }
}

private struct OwnedGameCard: View {
    let installation: OwnedGameInstallation
    let play: () -> Void
    let showInFinder: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button(action: play) {
                VStack(alignment: .leading, spacing: 0) {
                    AsyncImage(url: URL(string: installation.game.thumbnail ?? "")) { phase in
                        if let image = phase.image {
                            image.resizable().scaledToFill()
                        } else {
                            ZStack {
                                Color(red: 0.04, green: 0.055, blue: 0.095)
                                Image(systemName: "gamecontroller.fill").font(.system(size: 36)).foregroundStyle(.teal)
                            }
                        }
                    }
                    .frame(height: 150).frame(maxWidth: .infinity).clipped()
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(installation.game.name).font(.system(size: 16, weight: .bold))
                                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            Text(installation.job?.registered == true ? "Ready in Highball" : "Found in Games folder")
                                .font(.caption).foregroundStyle(.secondary)
                                .lineLimit(1).minimumScaleFactor(0.85)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        Spacer(minLength: 8)
                        Image(systemName: "play.circle.fill").font(.system(size: 30)).foregroundStyle(.teal)
                    }
                    .foregroundStyle(.white).padding(13)
                }
                .background(Color(red: 0.09, green: 0.115, blue: 0.18), in: RoundedRectangle(cornerRadius: 13))
                .clipShape(RoundedRectangle(cornerRadius: 13))
                .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.white.opacity(0.08), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .help("Play \(installation.game.name) in Highball")

            Button(action: showInFinder) {
                Label("Show in Finder", systemImage: "folder")
            }
            .buttonStyle(.bordered).controlSize(.small)
        }
    }
}

private struct GameCard: View {
    let game: Game
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 0) {
                artwork
                VStack(alignment: .leading, spacing: 8) {
                    Text(game.name).font(.system(size: 16, weight: .bold)).foregroundStyle(.white)
                        .lineLimit(1)
                    Text(game.description ?? "No description provided.")
                        .font(.system(size: 12.5)).foregroundStyle(Color(red: 0.68, green: 0.71, blue: 0.78))
                        .lineLimit(2).multilineTextAlignment(.leading).frame(height: 34, alignment: .topLeading)
                    if let tier = game.macCompatibility?.tier {
                        CompatBadge(tier: tier, confidence: game.macCompatibility?.confidence)
                    }
                    HStack(spacing: 6) {
                        if let version = game.version, !version.isEmpty { Text("v\(version)") }
                        if let size = game.size, !size.isEmpty { Text("• \(size)") }
                        if game.hasFix { Text("• + patch") }
                    }
                    .font(.system(size: 11)).foregroundStyle(Color(red: 0.55, green: 0.58, blue: 0.66))
                    if let players = game.players, !players.isEmpty {
                        Label(players, systemImage: "person.2.fill")
                            .font(.system(size: 11, weight: .semibold)).foregroundStyle(Color(red: 0.58, green: 0.77, blue: 0.98))
                            .lineLimit(1)
                    }
                    HStack {
                        Label(game.hasFix ? "Fix / Patch" : (game.isAvailable ? "Available" : "Coming soon"),
                              systemImage: game.hasFix ? "wrench.and.screwdriver.fill" : "circle.fill")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(game.hasFix ? .purple : (game.isAvailable ? .teal : .yellow))
                        Spacer()
                        Image(systemName: "arrow.up.right")
                            .font(.caption.weight(.bold)).foregroundStyle(.white.opacity(0.55))
                    }
                    .padding(.top, 2)
                }
                .padding(13)
            }
            .background(Color(red: 0.09, green: 0.115, blue: 0.18), in: RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.white.opacity(0.08), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 13))
            .contentShape(RoundedRectangle(cornerRadius: 13))
        }
        .buttonStyle(GameCardButtonStyle())
        .help("View \(game.name)")
    }

    private var artwork: some View {
        AsyncImage(url: URL(string: game.thumbnail ?? "")) { phase in
            switch phase {
            case .success(let image): image.resizable().scaledToFill()
            case .empty: placeholder("Loading artwork…")
            case .failure: placeholder(game.name)
            @unknown default: placeholder(game.name)
            }
        }
        .frame(height: 132).frame(maxWidth: .infinity).clipped()
        .background(Color(red: 0.04, green: 0.055, blue: 0.095))
        .overlay(alignment: .topTrailing) {
            if game.hasFix {
                Text("PATCH")
                    .font(.system(size: 9, weight: .heavy))
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .background(.purple.opacity(0.88), in: Capsule()).padding(9)
            }
        }
    }

    private func placeholder(_ title: String) -> some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.08, green: 0.12, blue: 0.2), Color(red: 0.04, green: 0.055, blue: 0.095)], startPoint: .topLeading, endPoint: .bottomTrailing)
            Label(title, systemImage: "gamecontroller.fill")
                .font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.58))
                .lineLimit(1).padding(.horizontal, 12)
        }
    }
}

private struct CompatBadge: View {
    let tier: String
    let confidence: String?

    private var tint: Color {
        switch tier {
        case "Native", "Runs great": .green
        case "Runs with a recipe": .yellow
        case "Partial": .orange
        case "Not yet": .red
        default: .gray
        }
    }

    var body: some View {
        Text("●  \(tier)\(confidence == "Low" ? " · estimated" : "")")
            .font(.system(size: 10, weight: .bold))
            .padding(.horizontal, 8).padding(.vertical, 4)
            .foregroundStyle(tint)
            .background(tint.opacity(0.13), in: RoundedRectangle(cornerRadius: 5))
            .lineLimit(1)
            .help("See the game details for the evidence and limits behind this Mac compatibility status.")
    }
}

private struct RequirementsGroup: View {
    private struct RequirementRow: Identifiable {
        let label: String
        let value: String
        var id: String { label }
    }

    let title: String
    let note: String
    let requirements: [String: String]

    private var rows: [RequirementRow] {
        let order = ["os", "cpu", "ram", "gpu", "directx", "graphicsApi", "storage", "additionalNotes"]
        return order.compactMap { key in
            guard let value = requirements[key], !value.isEmpty else { return nil }
            let label = switch key {
            case "os": "Operating system"
            case "cpu": "Processor"
            case "ram": "Memory"
            case "gpu": "Graphics"
            case "directx": "DirectX"
            case "graphicsApi": "Graphics API"
            case "storage": "Storage"
            default: "Notes"
            }
            return RequirementRow(label: label, value: value)
        }
    }

    var body: some View {
        GroupBox(title) {
            VStack(alignment: .leading, spacing: 8) {
                Text(note).font(.caption).foregroundStyle(.secondary)
                ForEach(rows) { row in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(row.label).font(.callout.weight(.semibold))
                            .frame(width: 112, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(row.value).font(.callout).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct GameDetailsSheet: View {
    let game: Game
    @ObservedObject var model: AppModel
    let onInstall: (Game) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var useMorePlayers = false

    private var selectedEdition: Game? {
        useMorePlayers ? game.morePlayersGame() : game
    }

    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 15) {
            HStack {
                Text(game.name).font(.title.bold()).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark.circle.fill").font(.title2).foregroundStyle(.secondary) }
                    .buttonStyle(.plain)
            }
            AsyncImage(url: URL(string: game.thumbnail ?? "")) { image in
                image.resizable().scaledToFill()
            } placeholder: {
                LinearGradient(colors: [.gray.opacity(0.35), .black.opacity(0.2)], startPoint: .topLeading, endPoint: .bottomTrailing)
                    .overlay(Image(systemName: "gamecontroller.fill").font(.largeTitle).foregroundStyle(.white.opacity(0.55)))
            }
            .frame(height: 180).frame(maxWidth: .infinity).clipShape(RoundedRectangle(cornerRadius: 10))
            Text(game.description ?? "No description provided.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let edition = game.morePlayers, edition.zipUrl.firstURL != nil {
                Picker("Edition", selection: $useMorePlayers) {
                    Text("Normal").tag(false)
                    Text("More Players").tag(true)
                }.pickerStyle(.segmented)
                Text(useMorePlayers ? (edition.description ?? "More-players edition.") : "Original game download and its configured repair, if available.")
                    .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if useMorePlayers, let version = edition.version, !version.isEmpty {
                    Text("More Players version: \(version)").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let compatibility = selectedEdition?.macCompatibility {
                GroupBox("Mac compatibility notes") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(compatibility.tier ?? "Not yet assessed").font(.headline)
                        if let reason = compatibility.reasoning { Text(reason).foregroundStyle(.secondary) }
                        HStack {
                            if let risk = compatibility.riskLevel { Text("Risk: \(risk)") }
                            if let confidence = compatibility.confidence { Text("Confidence: \(confidence)") }
                        }.font(.caption).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let requirements = game.requirements, !requirements.isEmpty {
                RequirementsGroup(
                    title: "Windows build minimums",
                    note: "These publisher-listed requirements apply to the Windows build used with Highball; they are not Mac hardware equivalents.",
                    requirements: requirements
                )
            }
            if let requirements = game.macRequirements, !requirements.isEmpty {
                RequirementsGroup(
                    title: "Native macOS minimums",
                    note: "Steam lists a separate macOS build for this game. These requirements apply to that native version.",
                    requirements: requirements
                )
            }
            HStack(spacing: 16) {
                if !useMorePlayers, let size = game.size { Label(size, systemImage: "internaldrive") }
                if !useMorePlayers, let players = game.players { Label(players, systemImage: "person.2") }
                if !useMorePlayers, let version = game.version { Text("v\(version)") }
            }.font(.callout).foregroundStyle(.secondary)
            Spacer()
            Text("\(selectedEdition?.isSteamEntry == true ? "This entry opens Windows Steam in the selected Highball bottle. Its repair-only flow is not implemented yet." : model.hasRecordedHighballRecipe(for: selectedEdition?.id ?? "") ? "Downloads the game to the selected folder, then adds its verified executable, title, cover, and saved settings to the selected Highball bottle. Missing Steam or Visual C++ components can be set up first. The game does not launch automatically." : "Downloads the game to the selected folder, detects a likely game executable, and adds it to the selected Highball bottle with default settings plus any detected DLL-loading overrides. If detection is uncertain, choose the .exe yourself; you can change it later.")")
                .font(.footnote).foregroundStyle(.secondary)
            Text(model.installFolder).font(.caption.monospaced()).lineLimit(2).foregroundStyle(.secondary)
            HStack {
                Button("Choose folder…", action: model.chooseInstallFolder)
                Spacer()
                if game.id == "among-us", let setupURL = URL(string: "https://github.com/OhMyGuus/BetterCrewLink/releases/download/v3.2.2/Better-CrewLink-Setup-3.2.2.exe") {
                    Button("Download BetterCrewLink setup") {
                        NSWorkspace.shared.open(setupURL)
                    }
                    .help("Downloads the Windows setup separately. Highball installation and voice compatibility are not yet verified.")
                }
                Button(selectedEdition?.isSteamEntry == true ? "Open in Windows Steam" : (model.hasRecordedHighballRecipe(for: selectedEdition?.id ?? "") ? "Download & add to Highball" : "Download & install")) {
                    if let selectedEdition { onInstall(selectedEdition) }
                }
                    .buttonStyle(.borderedProminent).tint(.teal)
                    .disabled(selectedEdition?.isAvailable != true || (selectedEdition?.isSteamEntry == true && model.operationBusy))
            }
        }
        .padding(22).background(Color(red: 0.055, green: 0.071, blue: 0.11))
        }
        .frame(minWidth: 680, idealWidth: 780, maxWidth: 900,
               minHeight: 580, idealHeight: 720, maxHeight: 900)
        .background(Color(red: 0.055, green: 0.071, blue: 0.11))
    }
}

private struct InstallerButtonStyle: ButtonStyle {
    var tint: Color
    var darkText = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .bold))
            .padding(.horizontal, 11).padding(.vertical, 8)
            .foregroundStyle(darkText ? Color.black.opacity(0.82) : .white)
            .background(tint.opacity(configuration.isPressed ? 0.68 : 0.88), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct UpdateDetailsView: View {
    let update: AppUpdate
    @ObservedObject var model: AppModel
    let later: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Update available", systemImage: "arrow.down.circle.fill")
                    .font(.title2.bold()).foregroundStyle(.orange)
                Spacer()
                Text(update.version).font(.callout.monospaced()).foregroundStyle(.secondary)
            }
            Divider()
            ScrollView {
                Text(update.notes.isEmpty ? "No release notes were provided." : update.notes)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            if let reason = update.manualReason {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if model.isInstallingUpdate, let message = model.updateMessage {
                VStack(alignment: .leading, spacing: 7) {
                    HStack(spacing: 8) {
                        if model.updateProgress == nil { ProgressView().controlSize(.small) }
                        Text(message).font(.caption)
                        Spacer(minLength: 0)
                        if let progress = model.updateProgress {
                            Text("\(Int(progress * 100))%")
                                .font(.caption.monospacedDigit().weight(.semibold))
                        }
                    }
                    if let progress = model.updateProgress {
                        ProgressView(value: progress).tint(.teal)
                    }
                }
                Text("If Game Installer stays open, press Command-Q to finish installing. It will reopen when the update is ready.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let message = model.updateMessage {
                Text(message).font(.caption).foregroundStyle(.red)
            }
            if update.canSelfUpdate && !model.updateInstallSafe {
                Text("Finish or cancel active installs and Highball operations before updating.")
                    .font(.caption).foregroundStyle(.orange)
            }
            HStack {
                Button("Later", action: later).keyboardShortcut(.cancelAction)
                Spacer()
                Button("Open release page") { NSWorkspace.shared.open(update.releaseURL) }
                if update.canSelfUpdate {
                    Button("Install update") { model.installAppUpdate() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.isInstallingUpdate || !model.updateInstallSafe)
                }
            }
        }
        .padding(24)
    }
}

private struct GameCardButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.teal.opacity(configuration.isPressed ? 0.85 : 0), lineWidth: 1.5))
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}
