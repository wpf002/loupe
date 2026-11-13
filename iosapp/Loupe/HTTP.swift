import Foundation

struct APIError: LocalizedError {
    let message: String
    let status: Int?

    var errorDescription: String? { message }
}

/// One URLSession for every call. Request timeouts are per call; the resource cap covers big uploads.
enum HTTP {
    static let requestTimeout: TimeInterval = 60
    static let uploadTimeout: TimeInterval = 600

    static let session: URLSession = {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = requestTimeout
        configuration.timeoutIntervalForResource = uploadTimeout
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        return URLSession(configuration: configuration)
    }()

    static func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError(message: "No response from Loupe", status: nil)
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError(message: message(in: data) ?? "Loupe said \(http.statusCode)", status: http.statusCode)
        }
        return data
    }

    /// loupe-web answers {"error": "…"}; loupe-server answers {"error": {"message": "…"}}.
    static func message(in data: Data) -> String? {
        guard let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let text = body["error"] as? String { return text }
        if let nested = body["error"] as? [String: Any] { return nested["message"] as? String }
        return nil
    }

    static func json(_ url: URL, method: String, body: some Encodable, token: String? = nil) throws -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        request.httpBody = try JSONEncoder().encode(body)
        return request
    }
}
