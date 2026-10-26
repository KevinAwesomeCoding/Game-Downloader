import SwiftUI

@main
struct GameDownloaderMacApp: App {
    @StateObject private var model = AppModel()

    init() {
        _ = DownloadCompletionNotifier.shared
    }

    var body: some Scene {
        WindowGroup("Game Installer") {
            ContentView(model: model)
                .frame(minWidth: 900, minHeight: 620)
                .preferredColorScheme(.dark)
        }
        .windowResizability(.contentSize)
    }
}
