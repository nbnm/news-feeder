import Foundation

public struct APIError: LocalizedError, Sendable {
    public let message: String
    public init(message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct APIClient: Sendable {
    public let baseURL: String
    public let token: String
    public init(baseURL: String, token: String) { self.baseURL = baseURL; self.token = token }

    private func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil,
                                       requestID: String? = nil) async throws -> T {
        let base = baseURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + path), ["http", "https"].contains(url.scheme ?? ""),
              url.host != nil else { throw APIError(message: "Enter a valid backend URL in Settings.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = path == "/telegram/daily" ? 660 : 120
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !token.isEmpty { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if let requestID { request.setValue(requestID, forHTTPHeaderField: "X-Request-ID") }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw APIError(message: "No backend response.") }
        guard (200..<300).contains(response.statusCode) else {
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let validation = (object?["detail"] as? [[String: Any]])?.prefix(3)
                .compactMap { $0["msg"] as? String }.joined(separator: " · ")
            let message = object?["detail"] as? String ?? validation ?? "Backend returned HTTP \(response.statusCode)."
            throw APIError(message: message)
        }
        do { return try makeDecoder().decode(T.self, from: data) }
        catch { throw APIError(message: "The backend returned an incompatible response: \(error.localizedDescription)") }
    }

    public func articles() async throws -> [Article] { try await request("/articles") }
    public func sources() async throws -> [FeedSource] { try await request("/sources") }
    public func categories() async throws -> [FeedCategory] { try await request("/categories") }
    public func saveCategory(_ category: FeedCategory, isNew: Bool) async throws -> FeedCategory {
        try await request(isNew ? "/categories" : "/categories/\(category.id)", method: isNew ? "POST" : "PUT",
                          body: makeEncoder().encode(category))
    }
    public func deleteCategory(_ category: FeedCategory) async throws {
        let _: [String: Bool] = try await request("/categories/\(category.id)", method: "DELETE")
    }
    public func status() async throws -> BackendStatus { try await request("/status") }
    public func telegramConfiguration() async throws -> TelegramConfiguration { try await request("/telegram/config") }
    public func saveTelegramBot(_ token: String) async throws -> TelegramConfiguration {
        try await request("/telegram/bot", method: "PUT", body: JSONSerialization.data(withJSONObject: ["bot_token": token]))
    }
    public func connectTelegramDestination(_ chatID: String, type: String) async throws -> TelegramConfiguration {
        try await request("/telegram/config", method: "PUT", body: JSONSerialization.data(withJSONObject: ["chat_id": chatID, "target_type": type]))
    }
    public func telegramTargets() async throws -> [TelegramTarget] { try await request("/telegram/targets") }
    public func publishDailyDigest() async throws -> DeliveryResult {
        try await request("/telegram/daily", method: "POST", body: JSONSerialization.data(withJSONObject: ["scheduled": false]))
    }
    public func retryTelegramDelivery(_ id: String) async throws -> DeliveryResult {
        try await request("/telegram/deliveries/\(id)/retry", method: "POST",
                          body: JSONSerialization.data(withJSONObject: ["confirmed_unsent": true]))
    }
    public func gatewayConfiguration() async throws -> GatewayConfiguration { try await request("/llm/config") }
    public func gatewayModels() async throws -> GatewayCatalog { try await request("/llm/models") }
    public func configureGateway(model: String? = nil, apiKey: String? = nil) async throws -> GatewayConfiguration {
        var body: [String: String] = [:]
        if let model { body["model"] = model }
        if let apiKey { body["api_key"] = apiKey }
        return try await request("/llm/config", method: "PUT", body: JSONSerialization.data(withJSONObject: body))
    }
    public func digests() async throws -> [Digest] { try await request("/digests") }
    public func preferences() async throws -> Preferences { try await request("/preferences") }
    public func makeDigest() async throws -> Digest { try await request("/digests", method: "POST") }
    public func refresh() async throws {
        let _: [String: Bool] = try await request("/refresh", method: "POST")
    }
    public func saveSource(_ source: FeedSource, isNew: Bool) async throws -> FeedSource {
        var body = try JSONSerialization.jsonObject(with: makeEncoder().encode(source)) as! [String: Any]
        // Explicit null clears a category; omission preserves it for old clients.
        if source.categoryId == nil { body["category_id"] = NSNull() }
        return try await request(isNew ? "/sources" : "/sources/\(source.id)", method: isNew ? "POST" : "PUT",
                                 body: JSONSerialization.data(withJSONObject: body))
    }
    public func deleteSource(_ source: FeedSource) async throws {
        let _: [String: Bool] = try await request("/sources/\(source.id)", method: "DELETE")
    }
    public func preview(_ source: FeedSource) async throws -> SourcePreview {
        try await request("/sources/preview", method: "POST", body: makeEncoder().encode(source))
    }
    public func savePreferences(_ preferences: Preferences) async throws -> Preferences {
        try await request("/preferences", method: "PUT", body: makeEncoder().encode(preferences))
    }
    public func saveProfile(_ profile: String, enabled: Bool) async throws -> Preferences {
        let body: [String: Any] = ["profile": profile, "relevance_enabled": enabled]
        return try await request("/profile", method: "PUT", body: JSONSerialization.data(withJSONObject: body))
    }
    public func refreshRelevance() async throws {
        let _: [String: Bool] = try await request("/relevance/refresh", method: "POST")
    }
    public func sendTelegram(kind: String, articleID: String? = nil) async throws -> DeliveryResult {
        var body = ["kind": kind]
        if let articleID { body["article_id"] = articleID }
        return try await request("/telegram/send", method: "POST", body: JSONSerialization.data(withJSONObject: body),
                                 requestID: UUID().uuidString)
    }
}
