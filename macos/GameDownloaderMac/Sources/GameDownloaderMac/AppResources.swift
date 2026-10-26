import Foundation

/// Resolves SwiftPM resources both in a normal macOS app bundle and when run
/// directly from SwiftPM's build directory.
enum AppResources {
    static let bundle: Bundle = {
        let resourceBundleName = "GameDownloaderMac_GameDownloaderMac"
        if let resourceURL = Bundle.main.resourceURL?.appendingPathComponent("\(resourceBundleName).bundle"),
           let resourceBundle = Bundle(url: resourceURL) {
            return resourceBundle
        }
        return .module
    }()
}
