import Foundation

struct SortStatus: Decodable, Sendable, Equatable {
    let stage: String
    let done: Int
    let total: Int
    let error: String?
    let items: Int
    let embedded: Int
    let sorted: Bool

    var isRunning: Bool { ["embed", "train", "classify", "albums"].contains(stage) }
}

/// loupe-web's /sync/v1 API, signed in with the account's Bearer token.
struct SyncClient: Sendable {
    static let previewPage = 200

    let webURL: URL
    let token: String

    private struct ItemsBody: Encodable { let items: [PhoneItem] }
    private struct ItemsReply: Decodable { let items: Int }
    private struct MissingReply: Decodable { let missing: [String] }

    private func request(_ path: String, method: String, query: [URLQueryItem] = []) -> URLRequest {
        var url = webURL.appending(path: "sync/v1/" + path)
        if !query.isEmpty { url.append(queryItems: query) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    /// Replaces the account's library with this list. Returns how many items are eligible.
    func putItems(_ items: [PhoneItem]) async throws -> Int {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        var request = request("items", method: "PUT")
        request.setValue("application/gzip", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = HTTP.uploadTimeout
        request.httpBody = try Gzip.compress(try encoder.encode(ItemsBody(items: items)))
        return try JSONDecoder().decode(ItemsReply.self, from: try await HTTP.send(request)).items
    }

    func missingPreviews(after: String) async throws -> [String] {
        let request = request("previews/missing", method: "GET", query: [
            URLQueryItem(name: "after", value: after),
            URLQueryItem(name: "limit", value: String(Self.previewPage)),
        ])
        return try JSONDecoder().decode(MissingReply.self, from: try await HTTP.send(request)).missing
    }

    func putPreview(uuid: String, jpeg: Data) async throws {
        var request = request("previews/\(uuid)", method: "PUT")
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = jpeg
        _ = try await HTTP.send(request)
    }

    func startSort() async throws {
        _ = try await HTTP.send(request("sort", method: "POST"))
    }

    func sortStatus() async throws -> SortStatus {
        try JSONDecoder().decode(SortStatus.self, from: try await HTTP.send(request("sort", method: "GET")))
    }
}
