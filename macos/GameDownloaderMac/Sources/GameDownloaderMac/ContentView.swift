import AppKit
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var games: [Game] = []
    @Published var readiness: ReadinessReport?
    @Published var readinessError: String?
    @Published var isChecking = false
    @Published var catalogError: String?
    @Published var jobs: [InstallJob] = []
    @Published var downloadEstimates: [String: String] = [:]
    @Published var actionMessage: String?
    @Published var actionLogPath: String?
    @Published var operationBusy = false
    @Published var rarAvailable = false
    @Published var selectedBottle = UserDefaults.standard.string(forKey: "selectedBottle") ?? "Games" {
        didSet { UserDefaults.standard.set(selectedBottle, forKey: "selectedBottle") }
    }
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

    init() { loadCatalog(); restoreQueue() }

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
            games = try Game.load().sorted {
                $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
            }
            catalogError = nil
        } catch {
            catalogError = error.localizedDescription
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
            }
        } catch {
            readiness = nil
            readinessError = error.localizedDescription
        }
    }

    func chooseInstallFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose Game Installer Folder"
        panel.message = "Choose where downloaded games should be stored."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: installFolder, isDirectory: true)
        if panel.runModal() == .OK, let url = panel.url { installFolder = url.path }
    }
}

private enum InstallerPage: String, CaseIterable {
    case library = "Library"
    case downloads = "Downloads"
    case settings = "Settings"
}

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var page: InstallerPage = .library
    @State private var search = ""
    @State private var selectedGame: Game?
    @State private var cleanupTarget: InstallJob?

    private let columns = [GridItem(.adaptive(minimum: 245, maximum: 330), spacing: 18)]

    private var filteredGames: [Game] {
        guard !search.isEmpty else { return model.games }
        return model.games.filter {
            $0.name.localizedCaseInsensitiveContains(search)
                || ($0.description?.localizedCaseInsensitiveContains(search) ?? false)
        }
    }

    var body: some View {
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
            pageContent
        }
        .frame(minWidth: 960, minHeight: 680)
        .background(Color(red: 0.055, green: 0.071, blue: 0.11))
        .preferredColorScheme(.dark)
        .sheet(item: $selectedGame) { game in
            GameDetailsSheet(game: game, model: model) {
                model.enqueue(game)
                selectedGame = nil
                if !game.isSteamEntry { page = .downloads }
            }
                .frame(minWidth: 520, minHeight: 440)
        }
        .task {
            await model.checkHighball()
            if !model.operationBusy { model.beginOperation(["tools"], jobID: nil, bottle: model.selectedBottle) }
        }
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
            HStack(spacing: 6) {
                ForEach(InstallerPage.allCases, id: \.self) { item in
                    Button {
                        page = item
                    } label: {
                        Text(item == .downloads && !model.jobs.isEmpty ? "Downloads (\(model.jobs.count))" : item.rawValue)
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
            Button {
                if page == .library { model.loadCatalog() }
                Task { await model.checkHighball() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(InstallerButtonStyle(tint: Color(red: 0.08, green: 0.78, blue: 0.67), darkText: true))
        }
        .padding(.horizontal, 25).padding(.vertical, 16)
        .background(Color(red: 0.075, green: 0.09, blue: 0.145))
    }

    @ViewBuilder private var pageContent: some View {
        switch page {
        case .library: libraryPage
        case .downloads: downloadsPage
        case .settings: settingsPage
        }
    }

    private var libraryPage: some View {
        VStack(spacing: 0) {
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
                                        Button("Choose .exe & add") { model.chooseExecutable(job) }.disabled(model.operationBusy)
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
                readinessCard
                GroupBox("Game launch bottle") {
                    VStack(alignment: .leading, spacing: 10) {
                        Picker("Highball bottle", selection: $model.selectedBottle) {
                            Text(model.selectedBottle.isEmpty ? "Choose a bottle" : model.selectedBottle).tag(model.selectedBottle)
                            ForEach((model.readiness?.readiness?.bottles ?? []).filter { $0.name != model.selectedBottle }, id: \.name) { bottle in
                                Text(bottle.name).tag(bottle.name)
                            }
                        }
                        Text("Registration adds a separate Game Installer pin. Close Highball before adding programs to avoid stale settings. Existing pins are never replaced.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Open Highball") { NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Highball.app")) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Archive support") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(model.rarAvailable ? "ZIP and local RAR/7z tools available" : "ZIP supported · RAR/7z needs local unar and lsar tools")
                        Text("This development build uses Python 3 and optional tools from /opt/homebrew/bin or /usr/local/bin. Unsupported archive methods show an error and keep downloads. No extraction tools are bundled for distribution yet.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Check archive tools") { model.beginOperation(["tools"], jobID: nil, bottle: model.selectedBottle) }
                            .disabled(model.operationBusy)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("Highball integration") {
                    Text("Installation, registration, and launch are separate actions. Recorded recipes apply only when the downloaded archives match the tested build. Other games require selecting an executable and use Highball defaults. Compatibility badges do not confirm gameplay or multiplayer.")
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
                } else {
                    Text("Checking Highball, its engine, and bottles…").foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label("Highball", systemImage: "wineglass")
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
            .help("Catalog prediction. Separate startup, gameplay, hosting, invite and joining tests are required.")
    }
}

private struct GameDetailsSheet: View {
    let game: Game
    @ObservedObject var model: AppModel
    let onInstall: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 15) {
            HStack {
                Text(game.name).font(.title.bold())
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
            Text(game.description ?? "No description provided.").foregroundStyle(.secondary)
            if let compatibility = game.macCompatibility {
                GroupBox("Catalog compatibility prediction") {
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
            HStack(spacing: 16) {
                if let size = game.size { Label(size, systemImage: "internaldrive") }
                if let players = game.players { Label(players, systemImage: "person.2") }
                if let version = game.version { Text("v\(version)") }
            }.font(.callout).foregroundStyle(.secondary)
            Spacer()
            Text("\(game.isSteamEntry ? "This entry opens Windows Steam in the selected Highball bottle. Its repair-only flow is not implemented yet." : "Downloads and any configured repair install into the selected folder. Add to Highball afterward; no game launches automatically.")")
                .font(.footnote).foregroundStyle(.secondary)
            Text(model.installFolder).font(.caption.monospaced()).lineLimit(2).foregroundStyle(.secondary)
            HStack {
                Button("Choose folder…", action: model.chooseInstallFolder)
                Spacer()
                Button(game.isSteamEntry ? "Open in Windows Steam" : "Download & install", action: onInstall)
                    .buttonStyle(.borderedProminent).tint(.teal)
                    .disabled(!game.isAvailable || (game.isSteamEntry && model.operationBusy))
            }
        }
        .padding(22).background(Color(red: 0.055, green: 0.071, blue: 0.11))
        }
        .frame(width: 600, height: 660)
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

private struct GameCardButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.teal.opacity(configuration.isPressed ? 0.85 : 0), lineWidth: 1.5))
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}
