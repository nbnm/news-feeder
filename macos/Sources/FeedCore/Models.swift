import Foundation

public struct Citation: Codable, Sendable, Hashable {
    public let quote: String
}

public struct ArticleSummary: Codable, Sendable, Equatable {
    public let tldr: String?
    public let whatChanged: String
    public let whyItMatters: String
    public let keyPoints: [String]
    public let topics: [String]
    public let impact: String
    public let citations: [Citation]
}

public struct Buzz: Codable, Sendable, Equatable {
    public let label: String
    public let points: Int
    public let comments: Int
    public let pointsPerHour: Double?
    public let observedAt: String
    public let url: String
}

public struct RepositoryMetadata: Codable, Sendable, Equatable {
    public let fullName: String
    public let description: String
    public let language: String?
    public let starsToday: Int?
    public let totalStars: Int?
    public let rank: Int
    public let observedAt: String
    public let trendingUrl: String
}

public struct TrendingSnapshot: Codable, Sendable, Hashable {
    public let observedAt: String
    public let articleIds: [String]
}

public enum RelevanceLevel: String, Sendable {
    case high = "High"
    case medium = "Medium"
    case low = "Low"

    public init(score: Double) {
        self = score >= 75 ? .high : score >= 40 ? .medium : .low
    }
}

public struct TopicRelevance: Codable, Sendable, Equatable {
    public let topic: String
    public let score: Double
    public let confidence: Double
    public var level: RelevanceLevel { RelevanceLevel(score: score) }
}

public struct Relevance: Codable, Sendable, Equatable {
    public let score: Double
    public let confidence: Double
    public let topics: [TopicRelevance]
    public let model: String
    public let profileHash: String
    public let assessedAt: String
    public let evidence: String
    public var level: RelevanceLevel { RelevanceLevel(score: score) }
}

public struct Article: Codable, Identifiable, Sendable, Equatable {
    public let id: String
    public let sourceId: String
    public let sourceName: String
    public let sourceIds: [String]?
    public let title: String
    public let url: String
    public let publishedAt: String?
    public let discoveredAt: String
    public let text: String
    public let contentState: String
    public let extractionError: String?
    public let summary: ArticleSummary?
    public let summaryState: String
    public let summaryError: String?
    public let tldr: String?
    public let tldrKind: String?
    public let buzz: Buzz?
    public let relevance: Relevance?
    public let relevanceState: String?
    public let relevanceError: String?
    public let repository: RepositoryMetadata?
    public var isRead = false
    public var isSaved = false

    enum CodingKeys: String, CodingKey {
        case id, sourceId, sourceIds, sourceName, title, url, publishedAt, discoveredAt, text, contentState
        case extractionError, summary, summaryState, summaryError, tldr, tldrKind, buzz
        case relevance, relevanceState, relevanceError
        case repository
    }

    public var displayTitle: String { repository?.fullName ?? title }

    public var previewText: String {
        tldr ?? summary?.tldr ?? summary?.whatChanged ?? (text.isEmpty ? title : String(text.prefix(360)))
    }

    public func currentRelevance(for preferences: Preferences) -> Relevance? {
        guard preferences.relevanceEnabled != false,
              let profile = preferences.profile, !profile.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let relevance, relevance.profileHash == preferences.profileHash else { return nil }
        return relevance
    }

    public var feedDate: Date {
        parseTimestamp(publishedAt ?? "") ?? parseTimestamp(discoveredAt) ?? .distantPast
    }

    public var previewLabel: String {
        if tldrKind == "headline" { return "Headline only · source text unavailable" }
        return summary == nil ? "Source excerpt · generated TLDR pending" : "Generated TLDR"
    }
}

public struct FeedSource: Codable, Identifiable, Sendable, Hashable {
    public var id: String
    public var name: String
    public var kind: String
    public var url: String
    public var enabled: Bool
    public var topics: [String]
    public var refreshMinutes: Int
    public var linkSelector: String
    public var articlePattern: String
    public var lastRefreshedAt: String?
    public var lastError: String?
    public var categoryId: String?
    public var releaseTagPattern: String?
    public var trending: TrendingSnapshot?

    public var kindLabel: String {
        switch kind {
        case "github_org": "GitHub organization"
        case "github": "GitHub releases"
        case "github_trending": "GitHub Trending"
        case "hacker_news": "Hacker News"
        case "rss": "RSS / Atom"
        case "website": "Website"
        default: kind
        }
    }

    public init(name: String = "", kind: String = "rss", url: String = "") {
        id = UUID().uuidString
        self.name = name; self.kind = kind; self.url = url
        enabled = true; topics = []; refreshMinutes = 60
        linkSelector = "a[href]"; articlePattern = ""
        trending = nil
    }
}

// Source membership and order come from the last successful snapshot. Older
// repository entries remain in the archive without leaking into today's top ten.
public func trendingArticles(_ articles: [Article], snapshot: TrendingSnapshot?, search: String = "") -> [Article] {
    let byID = Dictionary(uniqueKeysWithValues: articles.map { ($0.id, $0) })
    return (snapshot?.articleIds ?? []).compactMap { byID[$0] }.filter { article in
        search.isEmpty || (article.displayTitle + " " + article.text + " " + (article.repository?.description ?? ""))
            .localizedCaseInsensitiveContains(search)
    }
}

public struct Digest: Codable, Identifiable, Sendable, Equatable {
    public let id: String
    public let generatedAt: String
    public let startAt: String
    public let endAt: String
    public let articles: [Article]
}

public struct Preferences: Codable, Sendable, Equatable {
    public var topics: [String] = []
    public var digestSize: Int = 8
    public var deliveryHour: Int = 8
    public var timezone: String = "America/Toronto"
    public var telegramEnabled: Bool = false
    // Optional fields keep existing offline archives readable after an upgrade.
    public var profile: String?
    public var relevanceEnabled: Bool?
    public var profileHash: String?
    public init() {}
}

public struct BackendStatus: Codable, Sendable, Equatable {
    public let demo: Bool
    public let llmConfigured: Bool
    public let telegramConfigured: Bool
    public let collecting: Bool
    public let workerError: String?
    public let ranking: Bool?
    public let jevConfigured: Bool?
    public let jevModel: String?
    public let telegramBackgroundConfigured: Bool?
    public let telegramTargetType: String?
    public let instanceId: String?
}

public struct TelegramConfiguration: Codable, Sendable {
    public let configured: Bool
    public let botTokenConfigured: Bool
    public let chatId: String
    public let targetType: String
    public let title: String
    public let botUsername: String
    public let verified: Bool
    public let dailyEnabled: Bool
    public let nextDeliveryAt: String?
    public let lastDelivery: TelegramDeliveryStatus?
}

public struct TelegramDeliveryStatus: Codable, Sendable {
    public let id: String
    public let state: String
    public let sentCount: Int
    public let totalCount: Int
    public let error: String?
    public let createdAt: String
}

public struct TelegramTarget: Codable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let type: String
}

public struct GatewayConfiguration: Codable, Sendable, Equatable {
    public let baseUrl: String
    public let modelsUrl: String
    public let completionsUrl: String
    public let model: String
    public let apiKeyConfigured: Bool
    public let configured: Bool
}

public struct GatewayModel: Codable, Identifiable, Sendable, Equatable {
    public let id: String
}

public struct GatewayCatalog: Codable, Sendable, Equatable {
    public let models: [GatewayModel]
    public let selectedModel: String
}

public struct DeliveryResult: Codable, Sendable, Equatable {
    public let state: String
    public let sentCount: Int
    public let totalCount: Int
    public let error: String?
}

public struct SourcePreview: Codable, Sendable, Equatable {
    public struct Item: Codable, Sendable, Equatable { public let title: String; public let url: String }
    public let articles: [Item]
    public let note: String?
}

public func makeDecoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
}

public func makeEncoder() -> JSONEncoder {
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    return encoder
}

public func parseTimestamp(_ text: String) -> Date? {
    // Value-type parsing strategies avoid creating tens of thousands of
    // ISO8601DateFormatter instances during archive loads and feed sorting.
    guard !text.isEmpty else { return nil }
    return (try? Date(text, strategy: .iso8601.year().month().day().dateSeparator(.dash)
        .time(includingFractionalSeconds: true).timeZone(separator: .colon)))
        ?? (try? Date(text, strategy: .iso8601))
}

public struct FeedCategory: Codable, Identifiable, Sendable, Hashable {
    public var id: String
    public var name: String
    public init(name: String = "") { id = UUID().uuidString; self.name = name }
}
