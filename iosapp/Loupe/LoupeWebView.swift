import SwiftUI
import WebKit

/// Shows loupe-web signed in with the account's cookie. Only loupe-web loads here;
/// every other https link opens in Safari.
struct LoupeWebView: UIViewRepresentable {
    let url: URL
    let revision: Int

    func makeCoordinator() -> Coordinator { Coordinator(origin: url, revision: revision) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        view.isOpaque = false
        view.backgroundColor = .clear
        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.reload(_:)), for: .valueChanged)
        view.scrollView.refreshControl = refresh
        context.coordinator.view = view
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        guard context.coordinator.revision != revision else { return }
        context.coordinator.revision = revision
        view.reload()
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let origin: URL
        var revision: Int
        weak var view: WKWebView?

        init(origin: URL, revision: Int) {
            self.origin = origin
            self.revision = revision
        }

        private func isLoupe(_ url: URL?) -> Bool {
            url?.scheme == "https" && url?.host() == origin.host()
        }

        @objc func reload(_ control: UIRefreshControl) {
            view?.reload()
            control.endRefreshing()
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
            guard let url = action.request.url else { return .cancel }
            if isLoupe(url) || url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" { return .allow }
            if url.scheme == "https" { await UIApplication.shared.open(url) }
            return .cancel
        }

        // target=_blank links: Loupe pages load in place, others open in Safari.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard let url = action.request.url else { return nil }
            if isLoupe(url) { webView.load(URLRequest(url: url)) } else if url.scheme == "https" { UIApplication.shared.open(url) }
            return nil
        }
    }
}
