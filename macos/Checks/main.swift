import FeedCore
import Foundation
import GRDB

@main
struct CacheChecks {
    static func main() throws {
        guard startupBackendURL(environmentURL: nil, savedURL: "http://127.0.0.1:8765", savedDemoMode: true,
                                hasBundledBackend: true) == "http://127.0.0.1:8000",
              startupBackendURL(environmentURL: nil, savedURL: "https://my-server.example", savedDemoMode: false,
                                hasBundledBackend: true) == "https://my-server.example",
              startupBackendURL(environmentURL: "http://127.0.0.1:8765", savedURL: nil, savedDemoMode: true,
                                hasBundledBackend: true) == "http://127.0.0.1:8765",
              startupBackendURL(environmentURL: nil, savedURL: "http://127.0.0.1:8765", savedDemoMode: false,
                                hasBundledBackend: true) == "http://127.0.0.1:8765" else {
            fatalError("Installed launch must migrate the retired demo without changing explicit backend choices")
        }
        if let path = ProcessInfo.processInfo.environment["FEED_BENCHMARK_ARCHIVE"] {
            let start = Date()
            let archived = try FeedCache(path: path).articles()
            let loaded = Date()
            for _ in 0..<3 {
                _ = archived.sorted { $0.feedDate > $1.feedDate }
            }
            print("Archive \(archived.count): load \(loaded.timeIntervalSince(start))s; three date sorts \(Date().timeIntervalSince(loaded))s")
            let index = FeedIndex(articles: archived, preferences: Preferences())
            let byID = Dictionary(uniqueKeysWithValues: archived.map { ($0.id, $0) })
            let switchStart = Date()
            for i in 0..<100 {
                _ = index.articles(in: ["all", "unread", "saved"][i % 3], byID: byID, sources: [])
            }
            print("100 library section filters: \(Date().timeIntervalSince(switchStart))s")
            return
        }
        let fixture = """
        {"id":"one","source_id":"source","source_name":"Example","title":"Offline check",
         "url":"https://example.com/story","published_at":null,"discovered_at":"2026-10-01T12:00:00Z",
         "text":"Archived body","content_state":"full","extraction_error":null,"summary":null,
         "summary_state":"pending","summary_error":null,"tldr":"A real story preview.",
         "tldr_kind":"source_excerpt","buzz":null}
        """
        let article = try makeDecoder().decode(Article.self, from: Data(fixture.utf8))
        let telegram = try makeDecoder().decode(TelegramConfiguration.self, from: Data(#"{"configured":true,"bot_token_configured":true,"chat_id":"-1001234","target_type":"channel","title":"News","bot_username":"example_news_bot","verified":true,"daily_enabled":true,"next_delivery_at":"2026-10-30T12:00:00+00:00","last_delivery":{"id":"delivery-one","state":"sent","sent_count":2,"total_count":2,"error":null,"created_at":"2026-10-29T12:00:00+00:00"}}"#.utf8))
        guard telegram.verified, telegram.chatId == "-1001234", telegram.lastDelivery?.sentCount == 2,
              telegram.lastDelivery?.id == "delivery-one" else {
            fatalError("Telegram channel configuration and delivery receipts did not decode")
        }
        guard article.previewText == "A real story preview.", article.previewLabel.contains("Source excerpt") else {
            fatalError("Article TLDR did not decode")
        }
        let archivedPreferences = try makeDecoder().decode(Preferences.self, from: Data(
            #"{"topics":[],"digest_size":8,"delivery_hour":8,"timezone":"America/Toronto","telegram_enabled":false}"#.utf8))
        guard archivedPreferences.profile == nil, article.relevance == nil else {
            fatalError("Existing archives must remain readable")
        }
        var object = try JSONSerialization.jsonObject(with: Data(fixture.utf8)) as! [String: Any]
        object["relevance"] = ["score": 82.5, "confidence": 0.7, "topics": [
            ["topic": "APIs", "score": 90.0, "confidence": 0.8]], "model": "jev-1.13.0",
            "profile_hash": "profile-one", "assessed_at": "2026-10-01T12:00:00Z", "evidence": "summary"]
        object["relevance_state"] = "ready"
        let scored = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: object))
        var preferences = Preferences()
        preferences.profile = "I follow AI APIs."; preferences.profileHash = "profile-one"; preferences.relevanceEnabled = true
        guard scored.currentRelevance(for: preferences)?.topics.first?.score == 90,
              scored.currentRelevance(for: preferences)?.level == .high,
              scored.currentRelevance(for: preferences)?.topics.first?.level == .high else {
            fatalError("Jev story/topic response did not decode")
        }
        preferences.profileHash = "profile-two"
        guard scored.currentRelevance(for: preferences) == nil else { fatalError("Stale profile scores must be hidden") }
        preferences.profileHash = "profile-one"; preferences.relevanceEnabled = false
        guard scored.currentRelevance(for: preferences) == nil else { fatalError("Disabled scores must be hidden") }
        let gatewayFixture = """
        {"base_url":"https://secondstack.t1a.io/gateway/api/v1",
         "models_url":"https://secondstack.t1a.io/gateway/api/v1/models",
         "completions_url":"https://secondstack.t1a.io/gateway/api/v1/chat/completions",
         "model":"t1a-fast","api_key_configured":true,"configured":true}
        """
        let gateway = try makeDecoder().decode(GatewayConfiguration.self, from: Data(gatewayFixture.utf8))
        let catalog = try makeDecoder().decode(GatewayCatalog.self,
            from: Data(#"{"models":[{"id":"t1a-fast"}],"selected_model":"t1a-fast"}"#.utf8))
        guard gateway.completionsUrl.hasSuffix("/chat/completions"), gateway.apiKeyConfigured,
              catalog.models.first?.id == gateway.model, catalog.selectedModel == gateway.model else {
            fatalError("Gateway response did not decode")
        }
        let path = FileManager.default.temporaryDirectory.appendingPathComponent("feeder-\(UUID()).sqlite").path
        defer { try? FileManager.default.removeItem(atPath: path) }
        let cache = try FeedCache(path: path)
        try cache.merge([article])
        try cache.mark(article.id, read: true, saved: true)
        try cache.merge([article])
        let synced = try cache.articles()
        guard synced.count == 1, synced[0].isRead, synced[0].isSaved,
              synced[0].text == "Archived body" else { fatalError("Sync lost local reading state") }
        let reopened = try FeedCache(path: path)
        guard try reopened.articles()[0].isSaved else { fatalError("Archive did not persist") }
        let sources = [FeedSource(name: "A")]
        try cache.store(sources, named: "sources")
        guard try reopened.load([FeedSource].self, named: "sources")?.first?.name == "A" else {
            fatalError("Source snapshot did not persist")
        }
        try cache.merge([scored])
        preferences.relevanceEnabled = true
        try cache.store(preferences, named: "preferences")
        guard let profile = try reopened.load(Preferences.self, named: "preferences"),
              try reopened.articles()[0].currentRelevance(for: profile)?.score == 82.5,
              try reopened.articles()[0].isSaved else { fatalError("Offline relevance/profile or reading state lost") }
        let observer = try DatabaseQueue(path: path)
        func dataVersion() throws -> Int {
            try observer.read { try Int.fetchOne($0, sql: "PRAGMA data_version")! }
        }
        let unchangedVersion = try dataVersion()
        for _ in 0..<20 {
            try cache.merge([scored])
            try cache.store(sources, named: "sources")
            try cache.store(preferences, named: "preferences")
        }
        guard try dataVersion() == unchangedVersion else {
            fatalError("Unchanged background sync must not write to the archive")
        }
        object["text"] = "Updated source text"
        let updated = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: object))
        try cache.merge([updated])
        let changedVersion = try dataVersion()
        guard changedVersion != unchangedVersion,
              try reopened.articles()[0].text == "Updated source text",
              try reopened.articles()[0].isRead, try reopened.articles()[0].isSaved else {
            fatalError("Changed content must persist without losing reading state")
        }
        preferences.profile = "Updated interests"
        try cache.store(preferences, named: "preferences")
        guard try dataVersion() != changedVersion,
              try reopened.load(Preferences.self, named: "preferences")?.profile == "Updated interests" else {
            fatalError("Changed settings must still persist")
        }
        object["repository"] = ["full_name": "org/agents", "description": "Agent tools", "language": "Python",
            "stars_today": 1200, "total_stars": 42000, "rank": 1,
            "observed_at": "2026-10-02T12:00:00Z", "trending_url": "https://github.com/trending?since=daily"]
        let repository = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: object))
        object["id"] = "two"
        let secondRepository = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: object))
        let snapshot = try makeDecoder().decode(TrendingSnapshot.self, from: Data(
            #"{"observed_at":"2026-10-02T12:00:00Z","article_ids":["two","one"]}"#.utf8))
        guard repository.displayTitle == "org/agents", repository.repository?.starsToday == 1200,
              trendingArticles([repository, secondRepository], snapshot: snapshot).map(\.id) == ["two", "one"],
              trendingArticles([repository], snapshot: nil).isEmpty,
              trendingArticles([repository], snapshot: snapshot, search: "no-match").isEmpty else {
            fatalError("Trending membership, GitHub order, search, or repository metadata lost")
        }
        try cache.merge([repository, secondRepository])
        var trendingSource = FeedSource(name: "GitHub Trending", kind: "github_trending", url: "https://github.com/trending?since=daily")
        trendingSource.trending = snapshot
        try cache.store([trendingSource], named: "sources")
        guard try reopened.load([FeedSource].self, named: "sources")?.first?.trending?.articleIds == ["two", "one"],
              try reopened.articles().first(where: { $0.id == "one" })?.repository?.totalStars == 42000 else {
            fatalError("Trending snapshot and repository metadata must remain available offline")
        }
        var category = FeedCategory(name: "Databricks")
        category.id = "category-one"
        var groupedSource = FeedSource(name: "Databricks Labs")
        groupedSource.id = "labs"; groupedSource.categoryId = category.id
        var shared = try JSONSerialization.jsonObject(with: Data(fixture.utf8)) as! [String: Any]
        shared["id"] = "shared"; shared["source_id"] = "community"
        shared["source_ids"] = ["community", "labs"]
        shared["title"] = "Café data quality"; shared["published_at"] = "2026-10-02T15:30:00.123456+00:00"
        let sharedArticle = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: shared))
        preferences.profile = "AI APIs"; preferences.profileHash = "profile-one"; preferences.relevanceEnabled = true
        let index = FeedIndex(articles: [scored, sharedArticle], preferences: preferences)
        var byID = [scored.id: scored, sharedArticle.id: sharedArticle]
        guard index.articles(in: "category:category-one", byID: byID, sources: [groupedSource]).map(\.id) == ["shared"],
              index.articles(in: "labs", byID: byID, sources: [groupedSource], search: "cafe").map(\.id) == ["shared"],
              index.articles(in: "all", byID: byID, sources: [], sort: "latest").map(\.id) == ["shared", "one"],
              index.articles(in: "all", byID: byID, sources: []).map(\.id) == ["one", "shared"],
              parseTimestamp("2026-10-02T15:30:00.123456+00:00") != nil else {
            fatalError("Category membership, shared-source provenance, search, or date/priority ordering failed")
        }
        byID["shared"]?.isRead = true; byID["shared"]?.isSaved = true
        guard index.articles(in: "unread", byID: byID, sources: []).map(\.id) == ["one"],
              index.articles(in: "saved", byID: byID, sources: []).map(\.id) == ["shared"] else {
            fatalError("Reading flags must take effect without rebuilding the index")
        }
        trendingSource.categoryId = category.id
        let trendingIndex = FeedIndex(articles: [repository, secondRepository], preferences: preferences)
        guard trendingIndex.articles(in: "category:category-one", byID: ["one": repository, "two": secondRepository],
                                    sources: [trendingSource]).map(\.id) == ["two", "one"] else {
            fatalError("A GitHub Trending category must preserve current membership and GitHub order")
        }
        try cache.store([category], named: "categories")
        groupedSource.releaseTagPattern = "^v[0-9]"
        try cache.store([groupedSource], named: "sources")
        try cache.merge([sharedArticle])
        var releaseSource = groupedSource
        releaseSource.kind = "github"; releaseSource.releaseTagPattern = "^v?[0-9]+[.][0-9]+[.][0-9]+$"
        shared["url"] = "https://github.com/mlflow/mlflow/releases/tag/v3.17.0"
        let stableRelease = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: shared))
        shared["id"] = "catalog"; shared["url"] = "https://github.com/mlflow/mlflow/releases/tag/model-catalog/latest"
        let catalogRelease = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: shared))
        shared["id"] = "preview"; shared["url"] = "https://github.com/mlflow/mlflow/releases/tag/v3.18.0rc0"
        let previewRelease = try makeDecoder().decode(Article.self, from: JSONSerialization.data(withJSONObject: shared))
        let releaseIndex = FeedIndex(articles: [stableRelease, catalogRelease, previewRelease], preferences: preferences)
        let releases = [stableRelease.id: stableRelease, catalogRelease.id: catalogRelease, previewRelease.id: previewRelease]
        guard releaseIndex.articles(in: "category:category-one", byID: releases, sources: [releaseSource]).map(\.id) == ["shared"],
              releaseIndex.articles(in: "labs", byID: releases, sources: [releaseSource]).map(\.id) == ["shared"],
              releaseIndex.articles(in: "all", byID: releases, sources: [releaseSource]).count == 3 else {
            fatalError("Release filters must apply to source/category views while retaining the full archive")
        }
        guard try reopened.load([FeedCategory].self, named: "categories")?.first?.id == category.id,
              try reopened.load([FeedSource].self, named: "sources")?.first?.categoryId == category.id,
              try reopened.load([FeedSource].self, named: "sources")?.first?.releaseTagPattern == "^v[0-9]",
              try reopened.articles().first(where: { $0.id == "shared" })?.sourceIds == ["community", "labs"] else {
            fatalError("Categories, assignments, release filters, and membership must remain available offline")
        }
        print("Passed: category and source views, shared provenance, cached search/order, immediate reading flags, and offline category configuration")
        print("Passed: GitHub Trending metadata, order, membership, search, and offline persistence")
        print("Passed: gateway and Jev decoding, stale-score invalidation, offline profile/scores, source snapshots, deduplication, reading-state merge, and unchanged sync performs no database writes")
    }
}
