import Foundation
import UserNotifications

final class DownloadCompletionNotifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = DownloadCompletionNotifier()

    private let center = UNUserNotificationCenter.current()
    private let lock = NSLock()
    private var authorizationResolved = false
    private var notificationsAllowed = false
    private var pendingGames: [String] = []

    private override init() {
        super.init()
        center.delegate = self
    }

    func prepareForDownload() {
        center.getNotificationSettings { [weak self] settings in
            guard let self else { return }
            if settings.authorizationStatus == .notDetermined {
                self.center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    self.resolveAuthorization(granted)
                }
            } else {
                let allowed = settings.authorizationStatus == .authorized
                    || settings.authorizationStatus == .provisional
                self.resolveAuthorization(allowed)
            }
        }
    }

    func notifyDownloadCompleted(for gameName: String) {
        lock.lock()
        if !authorizationResolved {
            pendingGames.append(gameName)
            lock.unlock()
            return
        }
        let allowed = notificationsAllowed
        lock.unlock()

        if allowed { scheduleNotification(for: gameName) }
    }

    private func resolveAuthorization(_ allowed: Bool) {
        lock.lock()
        authorizationResolved = true
        notificationsAllowed = allowed
        let games = allowed ? pendingGames : []
        pendingGames.removeAll()
        lock.unlock()

        for game in games { scheduleNotification(for: game) }
    }

    private func scheduleNotification(for gameName: String) {
        let content = UNMutableNotificationContent()
        content.title = "Download complete"
        content.body = "\(gameName) is installed and ready in Downloads."
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: "game-download-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        center.add(request)
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
