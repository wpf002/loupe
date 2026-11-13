// Loupe.app launcher. Starts the Python backend (`loupe mac`) as a child process,
// so PhotoKit attributes access to Loupe.app, then shows the setup and studio
// pages in a native window. Only 127.0.0.1 on the backend's port loads in the
// window. Every other link opens in the default browser.

import Cocoa
import WebKit

let urlPrefix = "LOUPE_URL "
let backendWaitSeconds = 30.0

struct LaunchConfig: Decodable {
    let python: String
    let project: String
    let serverURL: String
    let webURL: String?
}

func loadConfig() throws -> LaunchConfig {
    guard let url = Bundle.main.url(forResource: "launch", withExtension: "json") else {
        throw NSError(domain: "Loupe", code: 1, userInfo: [NSLocalizedDescriptionKey: "launch.json is missing from the app"])
    }
    return try JSONDecoder().decode(LaunchConfig.self, from: Data(contentsOf: url))
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backend: Process?
    var backendInput = Pipe()
    var origin: URL?
    var buffered = Data()
    var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        do {
            try startBackend(loadConfig())
        } catch {
            fail(error.localizedDescription)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + backendWaitSeconds) { [weak self] in
            if self?.origin == nil { self?.fail("Loupe didn't start in time.") }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        // Closing stdin tells the backend to stop. Terminate covers a backend that hangs.
        try? backendInput.fileHandleForWriting.close()
        backend?.terminate()
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Loupe", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 800),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Loupe"
        window.minSize = NSSize(width: 720, height: 560)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("LoupeMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func startBackend(_ config: LaunchConfig) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: config.python)
        process.arguments = ["-m", "loupe", "mac"]
        process.currentDirectoryURL = URL(fileURLWithPath: config.project)
        var environment = ProcessInfo.processInfo.environment
        environment["LOUPE_SERVER_URL"] = config.serverURL
        if let webURL = config.webURL {
            environment["LOUPE_WEB_URL"] = webURL
        }
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardInput = backendInput
        let output = Pipe()
        process.standardOutput = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            DispatchQueue.main.async { self?.read(chunk) }
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                if self.origin == nil || finished.terminationStatus != 0 {
                    self.fail("Loupe stopped (exit \(finished.terminationStatus)).")
                }
            }
        }
        try process.run()
        backend = process
    }

    func read(_ chunk: Data) {
        guard origin == nil, !chunk.isEmpty else { return }
        buffered.append(chunk)
        guard let text = String(data: buffered, encoding: .utf8) else { return }
        for line in text.split(separator: "\n") where line.hasPrefix(urlPrefix) {
            guard let url = URL(string: String(line.dropFirst(urlPrefix.count))),
                  url.host == "127.0.0.1" else { continue }
            origin = url
            webView.load(URLRequest(url: url))
            return
        }
    }

    func isLocal(_ url: URL?) -> Bool {
        guard let url, let origin else { return false }
        return url.scheme == "http" && url.host == origin.host && url.port == origin.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if isLocal(action.request.url) {
            decisionHandler(.allow)
            return
        }
        if let url = action.request.url, ["https", "x-apple.systempreferences"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    // Links with target=_blank arrive here. Local ones load in place; others go to the browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isLocal(url) { webView.load(URLRequest(url: url)) } else if url.scheme == "https" { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Loupe Can't Start"
        alert.informativeText = message
        alert.addButton(withTitle: "Quit")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
