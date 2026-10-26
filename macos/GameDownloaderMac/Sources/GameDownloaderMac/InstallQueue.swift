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
            } catch {
                queueWritable = false
                actionMessage = "Saved queue could not be read; it was kept at \(location.path). \(error.localizedDescription)"
            }
        }
    }

    func saveQueue() {
        guard queueWritable else { return }
        do {
            try FileManager.default.createDirectory(at: Self.supportFolder, withIntermediateDirectories: true)
            try JSONEncoder().encode(jobs).write(to: Self.supportFolder.appending(path: "queue.json"), options: .atomic)
        } catch { actionMessage = "Could not save the queue: \(error.localizedDescription)" }
    }

    func enqueue(_ game: Game) {
        guard highballSetupComplete else {
            actionMessage = "Finish Highball setup before downloading or installing games."
            return
        }
        guard queueWritable else { actionMessage = "The saved queue needs repair before adding downloads. Its original file was preserved."; return }
        if game.isSteamEntry { requestSteam(game.zipURL ?? ""); return }
        guard !jobs.contains(where: { $0.game.id == game.id && $0.status != "cancelled" && $0.installFolder == installFolder }) else {
            actionMessage = "This game is already in Downloads. Resume or retry its existing entry."
            return
        }
        jobs.append(InstallJob(id: UUID().uuidString, game: game, installFolder: installFolder))
        saveQueue()
        startNext()
    }

    func startNext() {
        guard highballSetupComplete,
              installSession == nil,
              let job = jobs.first(where: { $0.status == "queued" }) else { return }
        do {
            try FileManager.default.createDirectory(at: Self.supportFolder, withIntermediateDirectories: true)
            let jobFile = Self.supportFolder.appending(path: "\(job.id).json")
            try JSONEncoder().encode(job).write(to: jobFile, options: .atomic)
            updateJob(job.id) { $0.status = "downloading"; $0.message = "Starting installation…" }
            activeJobID = job.id
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
                                            done: event.done ?? 0, total: event.total ?? 0)
            } completion: { [weak self] code, diagnostics in
                guard let self else { return }
                self.updateDownloadEstimate(jobID: job.id, status: "finished", done: 0, total: 0)
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
                self.installSession = nil
                self.activeJobID = nil
                self.saveQueue()
                self.startNext()
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

    func runHighball(_ job: InstallJob, manualExecutable: String? = nil, launch: Bool = false) {
        guard operationSession == nil else { actionMessage = "Another Highball action is still running."; return }
        var args = [launch ? "launch" : "register", "--job", Self.supportFolder.appending(path: "\(job.id).json").path,
                    "--bottle", selectedBottle]
        if let executable = manualExecutable { args += ["--manual", "--executable", executable] }
        beginOperation(args, jobID: job.id, bottle: selectedBottle)
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
        guard operationSession == nil else { actionMessage = "A Highball action is still running."; return }
        let alert = NSAlert()
        alert.messageText = "Open Windows Steam in Highball?"
        alert.informativeText = "This sends \(uri) to Steam in the selected bottle (\(selectedBottle)). Windows Steam must be installed and signed in there. Steam handles its own download."
        alert.addButton(withTitle: "Open Windows Steam")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn {
            beginOperation(["steam", "--bottle", selectedBottle, "--uri", uri], jobID: nil, bottle: selectedBottle)
        }
    }

    func beginOperation(_ arguments: [String], jobID: String?, bottle: String) {
        actionMessage = "Checking Highball…"
        actionLogPath = nil
        operationBusy = true
        do {
            operationSession = try WorkerSession(arguments: arguments) { [weak self] event in
                guard let self else { return }
                self.actionMessage = event.message
                self.actionLogPath = event.logPath ?? self.actionLogPath
                if event.status == "registered", let id = jobID {
                    self.updateJob(id) { $0.registered = true; $0.bottle = bottle; $0.message = event.message }
                    self.saveQueue()
                }
                if event.status == "tools" { self.rarAvailable = event.rarAvailable ?? false }
            } completion: { [weak self] code, diagnostics in
                guard let self else { return }
                if let diagnostics, code != 0 { self.actionMessage = diagnostics }
                self.operationSession = nil
                self.operationBusy = false
            }
        } catch { actionMessage = error.localizedDescription; operationBusy = false }
    }
}
