import Foundation

/// Server addresses from Info.plist. The app refuses to start without https URLs.
struct AppConfig: Sendable {
    let serverURL: URL
    let webURL: URL

    static func load(bundle: Bundle = .main) -> AppConfig {
        guard let serverURL = httpsURL(bundle, "LoupeServerURL"),
              let webURL = httpsURL(bundle, "LoupeWebURL") else {
            fatalError("Info.plist needs https LoupeServerURL and LoupeWebURL")
        }
        return AppConfig(serverURL: serverURL, webURL: webURL)
    }

    private static func httpsURL(_ bundle: Bundle, _ key: String) -> URL? {
        guard let text = bundle.object(forInfoDictionaryKey: key) as? String,
              let url = URL(string: text), url.scheme == "https", url.host != nil else { return nil }
        return url
    }
}
