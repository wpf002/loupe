import Observation
import Photos
import UIKit
import WebKit

enum SyncPhase: Equatable {
    case idle
    case reading
    case uploadingLibrary(items: Int)
    case uploadingPreviews(sent: Int, eligible: Int)
    case sorting(SortStatus)
    case done(items: Int)
    case failed(String)
}

/// PHAsset is safe to read from any thread but isn't marked Sendable.
private struct AssetRef: @unchecked Sendable {
    let asset: PHAsset
}

/// Sign-in state, Photos access and the sync to loupe-web. Photos is read, never changed.
@MainActor @Observable
final class AppModel {
    static let previewConcurrency = 4
    static let sortPollInterval: Duration = .seconds(3)
    static let cookieName = "__Host-loupe"
    static let cookieLifetime: TimeInterval = 30 * 24 * 3600
    static let signedOutMessage = "Sign in again"

    let config: AppConfig
    private(set) var session: StoredSession?
    private(set) var photos: PHAuthorizationStatus
    private(set) var sync: SyncPhase = .idle
    private(set) var webRevision = 0
    private(set) var signInMessage: String?
    private var syncTask: Task<Void, Never>?

    init(config: AppConfig) {
        self.config = config
        self.session = Keychain.load()
        self.photos = PhotoLibrary.status
    }

    private var accounts: AccountClient { AccountClient(serverURL: config.serverURL) }
    var isSyncing: Bool { syncTask != nil }
    var photosOff: Bool { photos == .denied || photos == .restricted }

    func start() async {
        guard let session else { return }
        await installCookie(session.token)
        await connectPhotos()
    }

    func signIn(email: String, password: String, create: Bool) async throws {
        let session = create
            ? try await accounts.createAccount(email: email, password: password)
            : try await accounts.signIn(email: email, password: password)
        try Keychain.save(session)
        await installCookie(session.token)
        signInMessage = nil
        self.session = session
        // Ask for Photos the moment the account is signed in.
        await connectPhotos()
    }

    func connectPhotos() async {
        if photos == .notDetermined { photos = await PhotoLibrary.requestAccess() }
        if PhotoLibrary.canRead(photos) { startSync() }
    }

    /// Settings can change access while Loupe is in the background.
    func refreshPhotos() {
        let current = PhotoLibrary.status
        guard current != photos else { return }
        photos = current
        if PhotoLibrary.canRead(current) { startSync() }
    }

    func startSync() {
        guard let session, syncTask == nil, PhotoLibrary.canRead(photos) else { return }
        let client = SyncClient(webURL: config.webURL, token: session.token)
        syncTask = Task {
            await runSync(client)
            syncTask = nil
        }
    }

    func signOut() async {
        syncTask?.cancel()
        syncTask = nil
        if let session { await accounts.signOut(token: session.token) }
        Keychain.clear()
        await WKWebsiteDataStore.default().removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
        session = nil
        sync = .idle
    }

    private func runSync(_ client: SyncClient) async {
        UIApplication.shared.isIdleTimerDisabled = true
        defer { UIApplication.shared.isIdleTimerDisabled = false }
        do {
            sync = .reading
            let snapshot = await Task.detached(priority: .userInitiated) { PhotoLibrary.snapshot() }.value
            try Task.checkCancellation()
            sync = .uploadingLibrary(items: snapshot.items.count)
            let eligible = try await client.putItems(snapshot.items)
            let sent = try await uploadPreviews(client, snapshot: snapshot, eligible: eligible)
            try await sortIfNeeded(client, uploaded: sent)
            sync = .done(items: eligible)
            webRevision += 1
        } catch is CancellationError {
            sync = .idle
        } catch let error as APIError where error.status == 401 {
            await signOut()
            signInMessage = Self.signedOutMessage
        } catch {
            sync = .failed(error.localizedDescription)
        }
    }

    /// Pages through what loupe-web lacks. Re-running resumes where the last sync stopped.
    private func uploadPreviews(_ client: SyncClient, snapshot: LibrarySnapshot, eligible: Int) async throws -> Int {
        var after = ""
        var sent = 0
        sync = .uploadingPreviews(sent: 0, eligible: eligible)
        while true {
            let missing = try await client.missingPreviews(after: after)
            guard let last = missing.last else { return sent }
            after = last
            let assets = PhotoLibrary.assets(for: missing.compactMap { snapshot.identifiers[$0] })
            let refs = missing.compactMap { uuid in assets[uuid].map { (uuid, AssetRef(asset: $0)) } }
            sent += try await uploadPage(client, refs)
            sync = .uploadingPreviews(sent: sent, eligible: eligible)
        }
    }

    private func uploadPage(_ client: SyncClient, _ refs: [(String, AssetRef)]) async throws -> Int {
        try await withThrowingTaskGroup(of: Int.self) { group in
            var running = 0
            var uploaded = 0
            for (uuid, ref) in refs {
                if running == Self.previewConcurrency {
                    uploaded += try await group.next() ?? 0
                    running -= 1
                }
                group.addTask { try await Self.upload(ref, uuid: uuid, client: client) }
                running += 1
            }
            for try await count in group { uploaded += count }
            return uploaded
        }
    }

    /// An item Photos can't produce a preview for is skipped; the next sync tries it again.
    nonisolated private static func upload(_ ref: AssetRef, uuid: String, client: SyncClient) async throws -> Int {
        guard let jpeg = await PhotoLibrary.previewJPEG(ref.asset) else { return 0 }
        try await client.putPreview(uuid: uuid, jpeg: jpeg)
        return 1
    }

    private func sortIfNeeded(_ client: SyncClient, uploaded: Int) async throws {
        var status = try await client.sortStatus()
        let stale = uploaded > 0 || !status.sorted || status.embedded < status.items
        var started = false
        if !status.isRunning && stale && status.items > 0 {
            do {
                try await client.startSort()
                started = true
            } catch let error as APIError where error.status == 409 {
                // Another device started a sort. Follow that one.
            }
            status = try await client.sortStatus()
        }
        while status.isRunning {
            started = true
            sync = .sorting(status)
            try await Task.sleep(for: Self.sortPollInterval)
            status = try await client.sortStatus()
        }
        if started, status.stage == "failed" {
            throw APIError(message: status.error ?? "Sort failed", status: nil)
        }
    }

    private func installCookie(_ token: String) async {
        guard let host = config.webURL.host(), let cookie = HTTPCookie(properties: [
            .name: Self.cookieName,
            .value: token,
            .domain: host,
            .path: "/",
            .secure: "TRUE",
            .expires: Date().addingTimeInterval(Self.cookieLifetime),
            HTTPCookiePropertyKey("HttpOnly"): "TRUE",
        ]) else { return }
        await WKWebsiteDataStore.default().httpCookieStore.setCookie(cookie)
    }
}

extension SyncPhase {
    var label: String {
        switch self {
        case .idle: ""
        case .reading: "Reading Photos"
        case .uploadingLibrary(let items): "Syncing \(items.formatted()) Items"
        case .uploadingPreviews(let sent, _): "Uploading Previews · \(sent.formatted())"
        case .sorting(let status): status.stageLabel
        case .done(let items): "\(items.formatted()) Items Synced"
        case .failed(let message): message
        }
    }

    var fraction: Double? {
        switch self {
        case .uploadingPreviews(let sent, let eligible) where eligible > 0:
            min(Double(sent) / Double(eligible), 1)
        case .sorting(let status) where status.total > 0:
            min(Double(status.done) / Double(status.total), 1)
        default:
            nil
        }
    }
}

extension SortStatus {
    var stageLabel: String {
        switch stage {
        case "embed": "Reading Photos · \(done.formatted()) of \(total.formatted())"
        case "train": "Learning Pillars"
        case "classify": "Sorting"
        default: "Sorting"
        }
    }
}
