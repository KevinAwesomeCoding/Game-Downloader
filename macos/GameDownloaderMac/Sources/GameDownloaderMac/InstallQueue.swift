import AppKit
import Foundation

@MainActor
final class WorkerSession {
    private let process = Process()
    private let input = Pipe()
    private var stopped = false

    init(arguments: [String], event: @escaping @MainActor (WorkerEvent) -> Void,
         completion: @escaping @MainActor (Int32, String?) -> Void) throws {
        guard let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw HighballReadinessError.pythonMissing
        }
        guard let script = AppResources.bundle.url(forResource: "worker", withExtension: "py", subdirectory: "Resources/Backend") else {
            throw HighballReadinessError.commandFailed("The bundled Mac install worker is missing. Reinstall the Mac app.")
        }
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-B", "-u", script.path] + arguments
        process.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        process.standardInput = input
        let output = Pipe()
        process.standardOutput = output
        // Worker converts errors to JSON; Python loader diagnostics still need to reach the UI.
        process.standardError = output
        try process.run()
        let process = self.process
        Task.detached {
            var pending = Data()
            var diagnostics = ""
            while true {
                let chunk = output.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                pending.append(chunk)
                while let newline = pending.firstIndex(of: 10) {
                    let line = pending.prefix(upTo: newline)
                    pending.removeSubrange(...newline)
                    if let decoded = try? JSONDecoder().decode(WorkerEvent.self, from: line) {
                        await event(decoded)
                    } else {
                        diagnostics += String(data: line, encoding: .utf8) ?? "Invalid worker output"
                        diagnostics = String(diagnostics.suffix(3000))
                    }
                }
            }
            process.waitUntilExit()
            if !pending.isEmpty { diagnostics += String(data: pending, encoding: .utf8) ?? "" }
            await completion(process.terminationStatus, diagnostics.isEmpty ? nil : diagnostics)
        }
    }

    func stop() {
        guard !stopped else { return }
        stopped = true
        try? input.fileHandleForWriting.write(contentsOf: Data("stop\n".utf8))
    }
}

extension AppModel {
    static var supportFolder: URL {
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "Game-Downloader/MacPreview", directoryHint: .isDirectory)
    }

    func restoreQueue() {
        let location = Self.supportFolder.appending(path: "queue.json")
        if FileManager.default.fileExists(atPath: location.path) {
            do {
                jobs = try JSONDecoder().decode([InstallJob].self, from: Data(contentsOf: location))
                for index in jobs.indices where jobs[index].isActive || jobs[index].status == "queued" {
                    jobs[index].status = "paused"
                    jobs[index].message = "Previous session ended. Resume keeps existing download data."
                }
                for index in jobs.indices where jobs[index].isInstalled {
                    let receiptURL = Self.supportFolder.appending(path: jobs[index].id).appending(path: "receipt.json")
                    guard let data = try? Data(contentsOf: receiptURL),
                          let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                          receipt["jobID"] as? String == jobs[index].id,
                          receipt["gameID"] as? String == jobs[index].game.id,
                          let registration = receipt["registration"] as? [String: Any],
                          registration["complete"] as? Bool == true,
                          let bottle = registration["bottle"] as? String else { continue }
                    jobs[index].registered = true
                    jobs[index].bottle = bottle
                    jobs[index].targetBottle = bottle
                    jobs[index].status = "registered"
                    jobs[index].message = "Added to Highball. Play sends the launch request through its checked helper."
                }
                saveQueue()
            } catch {
                queueWritable = false
                actionMessage = "Saved queue could not be read; it was kept at \(location.path). \(error.localizedDescription)"
            }
        }
        scanOwnedGames()
    }

    func saveQueue() {
        guard queueWritable else { return }
        do {
            try FileManager.default.createDirectory(at: Self.supportFolder, withIntermediateDirectories: true)
            try JSONEncoder().encode(jobs).write(to: Self.supportFolder.appending(path: "queue.json"), options: .atomic)
        } catch {
            let failure = error as NSError
            if (failure.domain == NSPOSIXErrorDomain && failure.code == Int(ENOSPC)) || failure.code == NSFileWriteOutOfSpaceError {
                actionMessage = "The disk is full, so Game Installer could not save its queue. Free disk space, then restart Game Installer to continue. Existing queue data was kept."
            } else {
                actionMessage = "Could not save the queue: \(error.localizedDescription)"
            }
        }
    }

    func enqueue(_ game: Game) {
        guard !operationBusy else { actionMessage = "A Highball action is still running."; return }
        guard gameFolderAccessGranted else {
            actionMessage = "Choose and authorize a Games folder before downloading."
            return
        }
        guard highballSetupComplete else {
            actionMessage = "Finish Highball setup before downloading or installing games."
            return
        }
        guard let bottle = readiness?.readiness?.bottles.first(where: { $0.name == selectedBottle }), bottle.ready else {
            actionMessage = "Choose a ready Highball bottle in Settings before downloading this game."
            return
        }
        guard queueWritable else { actionMessage = "The saved queue needs repair before adding downloads. Its original file was preserved."; return }
        let targetBottle = selectedBottle
        if game.isSteamEntry {
            let uri = game.zipURL ?? ""
            let required = Self.requiredHighballRecipes(for: game.id)
            let existing = Set(bottle.recipes.split(separator: ",").map(String.init))
            let missing = required.filter { !existing.contains($0) }
            guard !missing.isEmpty else { requestSteam(uri, bottle: targetBottle); return }

            let names = missing.map(Self.prerequisiteName).joined(separator: " and ")
            let alert = NSAlert()
            alert.messageText = "Prepare Highball for \(game.name)?"
            alert.informativeText = "Game Installer can set up \(names) in the \(targetBottle) bottle before opening Windows Steam. These components are shared with other games in that bottle."
            alert.addButton(withTitle: "Set Up & Open Steam")
            alert.addButton(withTitle: "Open Steam Only")
            alert.addButton(withTitle: "Cancel")
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                prepareSteamPrerequisites(missing, thenOpen: uri, bottle: targetBottle)
            case .alertSecondButtonReturn:
                requestSteam(uri, confirmed: true, bottle: targetBottle)
            default:
                break
            }
            return
        }
        guard !jobs.contains(where: { $0.game.id == game.id && $0.status != "cancelled" && $0.installFolder == installFolder }) else {
            actionMessage = "This game is already in Downloads. Resume or retry its existing entry."
            return
        }
        let required = Self.requiredHighballRecipes(for: game.id)
        let existing = Set(bottle.recipes.split(separator: ",").map(String.init))
        let missing = required.filter { !existing.contains($0) }
        var prepareRecipes: [String]? = missing.isEmpty ? nil : missing
        // Add the installed game to Highball by default. If prerequisite setup
        // is missing, the prompt below still lets the user choose Download Only.
        var automaticAdd = true
        if !missing.isEmpty {
            let alert = NSAlert()
            alert.messageText = "Prepare Highball for \(game.name)?"
            let names = missing.map(Self.prerequisiteName).joined(separator: " and ")
            alert.informativeText = "Game Installer can install \(names) in the \(selectedBottle) bottle before downloading. These components are shared with other games in that bottle. Steam sign-in remains yours. Highball will briefly close and reopen while setup and game registration run."
            alert.addButton(withTitle: "Set Up & Download")
            alert.addButton(withTitle: "Download Only")
            alert.addButton(withTitle: "Cancel")
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                break
            case .alertSecondButtonReturn:
                prepareRecipes = nil
                automaticAdd = false
            default:
                return
            }
        }
        var job = InstallJob(id: UUID().uuidString, game: game, installFolder: installFolder)
        job.targetBottle = selectedBottle
        job.prepareRecipes = prepareRecipes
        job.automaticAdd = automaticAdd
        jobs.append(job)
        saveQueue()
        startNext()
    }

    static func requiredHighballRecipes(for gameID: String) -> [String] {
        guard let url = AppResources.bundle.url(
            forResource: "recipes", withExtension: "json", subdirectory: "Resources/Backend"
        ), let data = try? Data(contentsOf: url),
           let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let deltas = root["deltas"] as? [String: [String: Any]],
           let delta = deltas[gameID],
           let required = delta["requiredRecipes"] as? [String] else { return [] }
        return required
    }

    func hasRecordedHighballRecipe(for gameID: String) -> Bool {
        !Self.requiredHighballRecipes(for: gameID).isEmpty
    }

    private static func prerequisiteName(_ id: String) -> String {
        switch id {
        case "steam": return "Windows Steam"
        case "vcrun2022": return "Visual C++ 2015–2022 runtime"
        default: return id
        }
    }

    func startNext() {
        guard highballSetupComplete,
              !operationBusy,
              installSession == nil,
              let job = jobs.first(where: { $0.status == "queued" }) else { return }
        activeJobID = job.id
        if let recipes = job.prepareRecipes, !recipes.isEmpty {
            startPreparation(job, recipes: recipes)
        } else {
            startInstallation(job)
        }
    }

    private func startPreparation(_ job: InstallJob, recipes: [String]) {
        guard let bottle = job.targetBottle, !bottle.isEmpty else {
            updateJob(job.id) { $0.status = "failed"; $0.message = "Choose a Highball bottle before setting up prerequisites." }
            activeJobID = nil
            startNext()
            return
        }
        updateJob(job.id) { $0.status = "preparing"; $0.message = "Preparing \(recipes.joined(separator: " and ")) in Highball…" }
        saveQueue()
        Task { @MainActor in
            let wasRunning: Bool
            do {
                wasRunning = try await HighballLifecycle.closeForConfiguration()
            } catch {
                updateJob(job.id) { $0.status = "failed"; $0.message = error.localizedDescription }
                activeJobID = nil
                saveQueue()
                startNext()
                return
            }
            if let intent = stopIntents.removeValue(forKey: job.id) {
                HighballLifecycle.reopenIfNeeded(wasRunning)
                updateJob(job.id) { $0.status = intent; $0.message = "\(intent.capitalized) before Highball setup began. No game files were downloaded." }
                activeJobID = nil
                saveQueue()
                startNext()
                return
            }
            do {
                installSession = try WorkerSession(arguments: ["prepare", "--bottle", bottle] + recipes.flatMap { ["--recipe", $0] }) { [weak self] event in
                    guard let self else { return }
                    self.updateJob(job.id) { item in
                        if item.status != "stopping" && event.status != "prepared" { item.status = event.status }
                        item.message = event.message
                    }
                } completion: { [weak self] code, diagnostics in
                    guard let self else { return }
                    HighballLifecycle.reopenIfNeeded(wasRunning)
                    let stopIntent = self.stopIntents.removeValue(forKey: job.id)
                    if code == 0 && stopIntent == nil {
                        self.updateJob(job.id) {
                            $0.prepareRecipes = nil
                            $0.status = "queued"
                            $0.message = "Highball prerequisites are ready. Starting the game download…"
                        }
                    } else {
                        self.updateJob(job.id) {
                            $0.status = stopIntent ?? "failed"
                            $0.message = diagnostics ?? (stopIntent == nil
                                ? "Highball prerequisite setup failed. Download has not started; retry to continue."
                                : "\(stopIntent!.capitalized). Completed Highball setup steps were kept.")
                        }
                    }
                    self.installSession = nil
                    self.activeJobID = nil
                    self.saveQueue()
                    self.startNext()
                }
            } catch {
                HighballLifecycle.reopenIfNeeded(wasRunning)
                updateJob(job.id) { $0.status = "failed"; $0.message = error.localizedDescription }
                installSession = nil
                activeJobID = nil
                saveQueue()
                startNext()
            }
        }
    }

    private func startInstallation(_ job: InstallJob) {
        do {
            DownloadCompletionNotifier.shared.prepareForDownload()
            try FileManager.default.createDirectory(at: Self.supportFolder, withIntermediateDirectories: true)
            let jobFile = Self.supportFolder.appending(path: "\(job.id).json")
            try JSONEncoder().encode(job).write(to: jobFile, options: .atomic)
            updateJob(job.id) { $0.status = "downloading"; $0.message = "Starting installation…" }
            installSession = try WorkerSession(arguments: ["install", "--job", jobFile.path]) { [weak self] event in
                guard let self else { return }
                self.updateJob(job.id) { item in
                    if item.status != "stopping" { item.status = event.status }
                    item.message = event.message
                    item.done = event.done ?? item.done
                    item.total = event.total ?? item.total
                    item.installPath = event.installPath ?? item.installPath
                    item.recipeAvailable = event.recipeAvailable ?? item.recipeAvailable
                }
                self.updateDownloadEstimate(jobID: job.id, status: event.status,
                                            done: event.done ?? 0, total: event.total ?? 0,
                                            archiveRemaining: event.archiveRemaining,
                                            archiveTotal: event.archiveTotal, partIndex: event.partIndex)
                if event.status == "downloading", let done = event.archiveDone,
                   let total = event.archiveTotal, total > 0 {
                    self.archiveProgress[job.id] = (done, total)
                } else if event.status != "downloading" || event.archiveTotal == 0 {
                    self.archiveProgress.removeValue(forKey: job.id)
                }
            } completion: { [weak self] code, diagnostics in
                guard let self else { return }
                self.updateDownloadEstimate(jobID: job.id, status: "finished", done: 0, total: 0,
                                            archiveRemaining: nil, archiveTotal: nil, partIndex: nil)
                self.archiveProgress.removeValue(forKey: job.id)
                let hadStopIntent = self.stopIntents[job.id] != nil
                self.updateJob(job.id) { item in
                    if let intent = self.stopIntents.removeValue(forKey: job.id) {
                        item.status = item.isInstalled ? "installed" : intent
                        item.message = item.isInstalled ? "Installation completed before the stop request. Files were kept." : "\(intent.capitalized). Download data has been preserved."
                    } else if code != 0 && item.status != "failed" {
                        item.status = "failed"
                        item.message = diagnostics ?? "Install worker exited \(code). Retry to resume."
                    } else if code == 0 && !item.isInstalled {
                        item.status = "failed"
                        item.message = "Worker finished without confirming the installation. Retry to check the saved receipt."
                    }
                }
                let completedWithoutStop = code == 0 && !hadStopIntent
                    && self.jobs.first(where: { $0.id == job.id })?.isInstalled == true
                if completedWithoutStop {
                    DownloadCompletionNotifier.shared.notifyDownloadCompleted(for: job.game.name)
                }
                self.installSession = nil
                self.activeJobID = nil
                self.saveQueue()
                if let updated = self.jobs.first(where: { $0.id == job.id }),
                   updated.isInstalled, updated.automaticAdd == true, !updated.registered {
                    self.addAutomaticallyToHighball(updated)
                } else {
                    self.startNext()
                }
            }
        } catch {
            updateJob(job.id) { $0.status = "failed"; $0.message = error.localizedDescription }
            installSession = nil
            activeJobID = nil
            startNext()
        }
    }

    func updateJob(_ id: String, _ change: (inout InstallJob) -> Void) {
        guard let index = jobs.firstIndex(where: { $0.id == id }) else { return }
        change(&jobs[index])
        // Progress stays live; transitions persist without writing on every downloaded chunk.
        if jobs[index].done == 0 || ["installed", "failed", "registered", "stopped"].contains(jobs[index].status) { saveQueue() }
    }

    func stopJob(_ id: String, cancel: Bool) {
        let status = cancel ? "cancelled" : "paused"
        if id == activeJobID {
            stopIntents[id] = status
            updateJob(id) { $0.status = "stopping"; $0.message = "Stopping safely; downloaded files will be kept…" }
            installSession?.stop()
        } else {
            updateJob(id) { $0.status = status; $0.message = "\(status.capitalized). Download files kept." }
        }
        saveQueue()
    }

    func retryJob(_ id: String) {
        updateJob(id) { $0.status = "queued"; $0.message = "Queued to resume"; $0.done = 0; $0.total = 0 }
        saveQueue()
        startNext()
    }

    func removeJob(_ id: String) {
        guard id != activeJobID, !operationBusy else { return }
        jobs.removeAll { $0.id == id }
        saveQueue()  // No cache, receipt or installed file is deleted.
    }

    func cleanDownloads(_ job: InstallJob) {
        guard job.isInstalled, job.registered, job.id != activeJobID, !operationBusy else {
            actionMessage = "Only an installed game already added to Highball can have its download files cleaned."
            return
        }
        let jobFile = Self.supportFolder.appending(path: "\(job.id).json")
        beginOperation(["cleanup", "--job", jobFile.path, "--registered"], jobID: job.id, bottle: selectedBottle)
    }

    func playOwnedGame(_ installation: OwnedGameInstallation) {
        var job = installation.job
        if job == nil {
            var discovered = InstallJob(id: UUID().uuidString, game: installation.game, installFolder: installFolder)
            discovered.installPath = installation.folderURL.path
            discovered.status = "installed"
            discovered.message = "Found in the selected Games folder."
            discovered.recipeAvailable = false
            do {
                let cache = Self.supportFolder.appending(path: discovered.id, directoryHint: .isDirectory)
                try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
                let receipt: [String: Any] = [
                    "jobID": discovered.id,
                    "gameID": installation.game.id,
                    "gameName": installation.game.name,
                    "thumbnail": installation.game.thumbnail ?? "",
                    "installPath": installation.folderURL.path,
                    "mainHashes": [String](),
                    "repairHashes": [String](),
                    "recordedRecipeAvailable": false
                ]
                let receiptData = try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted])
                try receiptData.write(to: cache.appending(path: "receipt.json"), options: .atomic)
                try JSONEncoder().encode(discovered).write(
                    to: Self.supportFolder.appending(path: "\(discovered.id).json"), options: .atomic
                )
                jobs.append(discovered)
                saveQueue()
                scanOwnedGames()
                job = discovered
            } catch {
                actionMessage = "Could not prepare this discovered game for Highball: \(error.localizedDescription)"
                return
            }
        }

        guard let job else { return }
        do {
            try JSONEncoder().encode(job).write(
                to: Self.supportFolder.appending(path: "\(job.id).json"), options: .atomic
            )
        } catch {
            actionMessage = "Could not prepare the Highball game entry: \(error.localizedDescription)"
            return
        }
        if job.registered || !(job.game.steamAppId ?? "").isEmpty {
            runHighball(job, launch: true)
        } else {
            runHighball(job)
        }
    }

    func runHighball(_ job: InstallJob, manualExecutable: String? = nil, launch: Bool = false) {
        guard operationSession == nil else { actionMessage = "Another Highball action is still running."; return }
        var args = [launch ? "launch" : "register", "--job", Self.supportFolder.appending(path: "\(job.id).json").path,
                    "--bottle", job.targetBottle ?? selectedBottle]
        if let executable = manualExecutable { args += ["--manual", "--executable", executable] }
        beginOperation(args, jobID: job.id, bottle: job.targetBottle ?? selectedBottle)
    }

    func installBetterCrewLink(_ job: InstallJob) {
        guard !operationBusy, operationSession == nil, installSession == nil,
              job.registered, let bottle = job.bottle, !bottle.isEmpty else {
            actionMessage = "Add Among Us to Highball first and wait for other actions to finish."
            return
        }
        operationBusy = true
        actionMessage = "Preparing bundled BetterCrewLink setup…"
        Task { @MainActor in
            do {
                let wasRunning = try await HighballLifecycle.closeForConfiguration()
                beginOperation(["bettercrewlink", "--job", Self.supportFolder.appending(path: "\(job.id).json").path],
                               jobID: nil, bottle: bottle, completion: {
                    HighballLifecycle.reopenIfNeeded(wasRunning)
                }, allowReservedBusy: true)
            } catch {
                operationBusy = false
                actionMessage = "Could not prepare BetterCrewLink setup: \(error.localizedDescription)"
            }
        }
    }

    func setupItchLogin(_ job: InstallJob) {
        guard job.registered, let bottle = job.bottle, !bottle.isEmpty else {
            actionMessage = "Add Among Us to Highball before setting up itch.io login."
            return
        }
        let client = itchClientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !client.isEmpty else {
            actionMessage = "Add your itch.io OAuth client ID in Settings first."
            return
        }
        beginOperation(["itch-login", "--job", Self.supportFolder.appending(path: "\(job.id).json").path,
                        "--itch-client-id", client], jobID: nil, bottle: bottle)
    }

    private func addAutomaticallyToHighball(_ job: InstallJob) {
        guard operationSession == nil else {
            updateJob(job.id) { $0.message = "Installed. Highball is busy; retry adding it when ready." }
            startNext()
            return
        }
        let bottle = job.targetBottle ?? selectedBottle
        guard !bottle.isEmpty else {
            updateJob(job.id) { $0.message = "Installed. Choose a Highball bottle, then retry adding it." }
            startNext()
            return
        }
        operationBusy = true
        actionMessage = "Adding \(job.game.name) to Highball…"
        Task { @MainActor in
            let wasRunning: Bool
            do {
                wasRunning = try await HighballLifecycle.closeForConfiguration()
            } catch {
                operationBusy = false
                updateJob(job.id) { $0.message = "Installed, but automatic Highball registration could not continue: \(error.localizedDescription)" }
                startNext()
                return
            }
            beginOperation(
                ["register", "--job", Self.supportFolder.appending(path: "\(job.id).json").path, "--bottle", bottle],
                jobID: job.id, bottle: bottle, completion: {
                HighballLifecycle.reopenIfNeeded(wasRunning)
                if let current = self.jobs.first(where: { $0.id == job.id }), !current.registered {
                    self.updateJob(job.id) { $0.message = "Installed, but automatic registration could not finish. Use Choose .exe & add to select or change the game executable." }
                }
                self.startNext()
            }, allowReservedBusy: true)
        }
    }

    func chooseExecutable(_ job: InstallJob) {
        guard let root = job.installPath else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose the game executable"
        panel.message = "Choose an .exe inside this game. No inferred renderer or DLL fixes will be applied."
        panel.directoryURL = URL(fileURLWithPath: root)
        if panel.runModal() == .OK, let executable = panel.url {
            let prefix = URL(fileURLWithPath: root).resolvingSymlinksInPath().path + "/"
            let path = executable.resolvingSymlinksInPath().path
            guard path.hasPrefix(prefix), executable.pathExtension.lowercased() == "exe" else {
                actionMessage = "Choose an .exe inside this game's installed folder."; return
            }
            runHighball(job, manualExecutable: String(path.dropFirst(prefix.count)))
        }
    }

    func requestSteam(_ uri: String) {
        requestSteam(uri, confirmed: false, bottle: selectedBottle)
    }

    private func requestSteam(_ uri: String, bottle: String) {
        requestSteam(uri, confirmed: false, bottle: bottle)
    }

    private func requestSteam(_ uri: String, confirmed: Bool, bottle: String) {
        guard operationSession == nil, installSession == nil, !operationBusy else { actionMessage = "A Highball action is still running."; return }
        if !confirmed {
            let isLaunch = uri == "steam://open/main"
            let alert = NSAlert()
            alert.messageText = isLaunch ? "Launch Windows Steam in Highball?" : "Open Windows Steam in Highball?"
            alert.informativeText = isLaunch
                ? "This opens Windows Steam in the selected bottle (\(bottle)). Steam must be installed there."
                : "This sends \(uri) to Steam in the selected bottle (\(bottle)). Windows Steam must be installed and signed in there. Steam handles its own download."
            alert.addButton(withTitle: isLaunch ? "Launch Windows Steam" : "Open Windows Steam")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        beginOperation(["steam", "--bottle", bottle, "--uri", uri], jobID: nil, bottle: bottle)
    }

    private func prepareSteamPrerequisites(_ recipes: [String], thenOpen uri: String, bottle: String) {
        guard operationSession == nil, installSession == nil else {
            actionMessage = "Another Highball action is still running."
            return
        }
        operationBusy = true
        actionMessage = "Preparing \(recipes.joined(separator: " and ")) in Highball…"
        actionLogPath = nil
        Task { @MainActor in
            let wasRunning: Bool
            do {
                wasRunning = try await HighballLifecycle.closeForConfiguration()
            } catch {
                operationBusy = false
                actionMessage = error.localizedDescription
                return
            }
            do {
                operationSession = try WorkerSession(
                    arguments: ["prepare", "--bottle", bottle] + recipes.flatMap { ["--recipe", $0] }
                ) { [weak self] event in
                    guard let self else { return }
                    self.actionMessage = event.message
                    self.actionLogPath = event.logPath ?? self.actionLogPath
                } completion: { [weak self] code, diagnostics in
                    guard let self else { return }
                    HighballLifecycle.reopenIfNeeded(wasRunning)
                    self.operationSession = nil
                    self.operationBusy = false
                    if code == 0 {
                        self.requestSteam(uri, confirmed: true, bottle: bottle)
                    } else {
                        self.actionMessage = diagnostics ?? "Highball prerequisite setup failed. Steam was not opened."
                    }
                }
            } catch {
                HighballLifecycle.reopenIfNeeded(wasRunning)
                operationSession = nil
                operationBusy = false
                actionMessage = error.localizedDescription
            }
        }
    }

    func beginOperation(_ arguments: [String], jobID: String?, bottle: String,
                        completion: (() -> Void)? = nil, allowReservedBusy: Bool = false) {
        guard (!operationBusy || allowReservedBusy), installSession == nil, operationSession == nil else {
            actionMessage = "Another Highball action is still running."
            return
        }
        actionMessage = "Checking Highball…"
        actionLogPath = nil
        operationBusy = true
        do {
            operationSession = try WorkerSession(arguments: arguments) { [weak self] event in
                guard let self else { return }
                self.actionMessage = event.message
                self.actionLogPath = event.logPath ?? self.actionLogPath
                if event.status == "registered", let id = jobID {
                    self.updateJob(id) { $0.registered = true; $0.bottle = bottle; $0.targetBottle = bottle; $0.status = "registered"; $0.message = event.message }
                    self.saveQueue()
                    self.scanOwnedGames()
                } else if event.status == "failed", let id = jobID {
                    self.updateJob(id) { $0.status = "installed"; $0.message = event.message }
                }
                if event.status == "tools" { self.rarAvailable = event.rarAvailable ?? false }
                if event.status == "installed", let id = jobID {
                    if let path = event.installPath {
                        self.updateJob(id) { $0.installPath = path; $0.status = "installed"; $0.message = event.message; $0.recipeAvailable = event.recipeAvailable ?? false }
                        self.scanOwnedGames()
                    }
                }
            } completion: { [weak self] code, diagnostics in
                guard let self else { return }
                if let diagnostics, code != 0 { self.actionMessage = diagnostics }
                self.operationSession = nil
                self.operationBusy = false
                completion?()
                self.startNext()
            }
        } catch {
            actionMessage = error.localizedDescription
            operationBusy = false
            completion?()
            startNext()
        }
    }
}
