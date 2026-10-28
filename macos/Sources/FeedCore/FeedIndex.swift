import Foundation

/// Build on sync, then switch sections by filtering preordered IDs. Date parsing
/// and sorting never run inside SwiftUI's body or in response to a selection.
public struct FeedIndex: Sendable {
    private let latest: [String]
    private let priority: [String]
    private let sourceIDs: [String: Set<String>]
    private let searchText: [String: String]

    public init(articles: [Article], preferences: Preferences) {
        let entries = articles.map { ($0.id, $0.feedDate, $0.currentRelevance(for: preferences)?.score) }
        latest = entries.sorted { $0.1 == $1.1 ? $0.0 < $1.0 : $0.1 > $1.1 }.map(\.0)
        priority = entries.sorted { left, right in
            if (left.2 != nil) != (right.2 != nil) { return left.2 != nil }
            if left.2 != right.2 { return (left.2 ?? 0) > (right.2 ?? 0) }
            return left.1 == right.1 ? left.0 < right.0 : left.1 > right.1
        }.map(\.0)
        sourceIDs = Dictionary(uniqueKeysWithValues: articles.map {
            ($0.id, Set(($0.sourceIds ?? []) + [$0.sourceId]))
        })
        searchText = Dictionary(uniqueKeysWithValues: articles.map {
            ($0.id, Self.normalized($0.displayTitle + " " + $0.text + " " + $0.sourceName))
        })
    }

    private static func normalized(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
    }

    public func articles(in section: String, byID: [String: Article], sources: [FeedSource],
                         search: String = "", sort: String = "priority") -> [Article] {
        let categorySources = sources.filter { "category:" + ($0.categoryId ?? "") == section }
        let directSource = sources.first { $0.id == section }
            ?? (categorySources.count == 1 && categorySources[0].kind == "github_trending" ? categorySources[0] : nil)
        let members = Set(sources.filter { "category:" + ($0.categoryId ?? "") == section }.map(\.id))
        // Trending categories show the current snapshot, not historical repos.
        let snapshotIDs = Set(sources.filter { members.contains($0.id) && $0.kind == "github_trending" }
            .flatMap { $0.trending?.articleIds ?? [] })
        let ordinaryMembers = Set(sources.filter { members.contains($0.id) && $0.kind != "github_trending" }.map(\.id))
        let selectedSources = sources.filter { $0.id == section || ordinaryMembers.contains($0.id) }
        let order = directSource?.kind == "github_trending" ? directSource?.trending?.articleIds ?? []
            : sort == "latest" ? latest : priority
        let query = Self.normalized(search)
        return order.compactMap { id -> Article? in
            guard let article = byID[id] else { return nil }
            let memberships = sourceIDs[id] ?? []
            let belongs = section == "all" || (section == "unread" && !article.isRead)
                || (section == "saved" && article.isSaved)
                || (directSource?.kind == "github_trending")
                || selectedSources.contains { source in
                    memberships.contains(source.id) && Self.matchesReleaseFilter(article, source: source)
                } || snapshotIDs.contains(id)
            guard belongs && (query.isEmpty || searchText[id]?.contains(query) == true) else { return nil }
            return article
        }
    }

    private static func matchesReleaseFilter(_ article: Article, source: FeedSource) -> Bool {
        guard source.kind == "github", let pattern = source.releaseTagPattern, !pattern.isEmpty else { return true }
        guard let path = URL(string: article.url)?.path,
              let separator = path.range(of: "/releases/tag/") else { return false }
        let tag = String(path[separator.upperBound...]).removingPercentEncoding ?? String(path[separator.upperBound...])
        return tag.range(of: pattern, options: .regularExpression) != nil
    }
}

public struct ArchiveSnapshot: Sendable {
    public let articles: [Article]
    public let sources: [FeedSource]
    public let categories: [FeedCategory]
    public let digests: [Digest]
    public let preferences: Preferences
    public let index: FeedIndex

    public init(cache: FeedCache) throws {
        articles = try cache.articles()
        sources = try cache.load([FeedSource].self, named: "sources") ?? []
        categories = try cache.load([FeedCategory].self, named: "categories") ?? []
        digests = try cache.load([Digest].self, named: "digests") ?? []
        preferences = try cache.load(Preferences.self, named: "preferences") ?? Preferences()
        index = FeedIndex(articles: articles, preferences: preferences)
    }
}
