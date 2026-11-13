import Foundation

/// Sign In and Create Account against loupe-server. The same token signs in to loupe-web.
struct AccountClient: Sendable {
    let serverURL: URL

    private struct Credentials: Encodable {
        let email: String
        let password: String
    }

    private struct SessionReply: Decodable {
        let token: String
        let email: String
    }

    func signIn(email: String, password: String) async throws -> StoredSession {
        try await start("v1/sessions", email: email, password: password)
    }

    func createAccount(email: String, password: String) async throws -> StoredSession {
        try await start("v1/accounts", email: email, password: password)
    }

    func signOut(token: String) async {
        var request = URLRequest(url: serverURL.appending(path: "v1/sessions"))
        request.httpMethod = "DELETE"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        // Signing out locally always works. A failed revoke leaves a token that expires on its own.
        _ = try? await HTTP.send(request)
    }

    private func start(_ path: String, email: String, password: String) async throws -> StoredSession {
        let trimmed = email.trimmingCharacters(in: .whitespacesAndNewlines)
        let request = try HTTP.json(
            serverURL.appending(path: path), method: "POST",
            body: Credentials(email: trimmed, password: password))
        let reply = try JSONDecoder().decode(SessionReply.self, from: try await HTTP.send(request))
        return StoredSession(token: reply.token, email: reply.email)
    }
}
