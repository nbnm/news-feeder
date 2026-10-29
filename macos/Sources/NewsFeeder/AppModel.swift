import AppKit
import CryptoKit
import FeedCore
import Foundation
import Security
import SwiftUI

enum CredentialStore {
    static func read(for account: String) -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "NewsFeeder.Backend", kSecAttrAccount as String: account,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func save(_ token: String, for account: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "NewsFeeder.Backend", kSecAttrAccount as String: account]
        if token.isEmpty { SecItemDelete(query as CFDictionary); return }
        let data = Data(token.utf8)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            status = SecItemAdd(insert as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw APIError(message: "Could not save the credential to Keychain.") }
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var articles: [Article] = [] {
        didSet {
            articlesByID = Dictionary(uniqueKeysWithValues: articles.map { ($0.id, $0) })
            unreadCount = articles.reduce(0) { $0 + ($1.isRead ? 0 : 1) }
        }
    }
    @Published var categories: [FeedCategory] = []
    private(set) var articlesByID: [String: Article] = [:]
    private(set) var unreadCount = 0
    private var feedIndex = FeedIndex(articles: [], preferences: Preferences())
    @Published var sources: [FeedSource] = []
    @Published var digests: [FeedCore.Digest] = []
    @Published var preferences = Preferences()
    @Published var status: BackendStatus?
    @Published var busy = false
    @Published var sending = false
    @Published var online = false
    @Published var error: String?
    @Published var notice: String?
    @Published var backgroundDeliveryActive = false
    @Published var backgroundDeliveryError: String?
    @Published var backendURL: String
    @Published var token: String
    private var cache: FeedCache?
    private var cachedMode = false
    private var preferencesRevision = 0
    private var syncPending = false

    init() {
        let forcedURL = ProcessInfo.processInfo.environment["NEWS_FEEDER_BACKEND_URL"]
        let savedURL = UserDefaults.standard.string(forKey: "backendURL")
        let helper = Bundle.main.resourceURL?.appendingPathComponent("Backend/NewsFeederService")
        let initialURL = startupBackendURL(environmentURL: forcedURL, savedURL: savedURL,
            savedDemoMode: UserDefaults.standard.bool(forKey: "demo:" + (savedURL ?? "")),
            hasBundledBackend: helper.map { FileManager.default.isExecutableFile(atPath: $0.path) } ?? false)
        if forcedURL == nil, let savedURL, savedURL != initialURL {
            UserDefaults.standard.set(initialURL, forKey: "backendURL")
        }
        backendURL = initialURL
        token = CredentialStore.read(for: initialURL)
        cachedMode = UserDefaults.standard.bool(forKey: "demo:" + backendURL)
        do { try openCache(demo: cachedMode) }
        catch { self.error = "Could not open the local archive: \(error.localizedDescription)" }
    }

    func start() async { await loadCache(); await sync() }

    var api: APIClient { APIClient(baseURL: backendURL, token: token) }
    func visibleArticles(section: String, search: String, sort: String) -> [Article] {
        feedIndex.articles(in: section, byID: articlesByID, sources: sources, search: search, sort: sort)
    }

    private func openCache(demo: Bool) throws {
        let root = ProcessInfo.processInfo.environment["NEWS_FEEDER_DATA_DIR"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("NewsFeeder")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let hash = SHA256.hash(data: Data(backendURL.utf8)).map { String(format: "%02x", $0) }.joined()
        cache = try FeedCache(path: root.appendingPathComponent(hash + (demo ? "-demo" : "") + ".sqlite").path)
        cachedMode = demo
    }

    private func apply(_ snapshot: ArchiveSnapshot, preserveFlags: Bool = true) {
        var incoming = snapshot.articles
        if preserveFlags {
            for index in incoming.indices {
                if let local = articlesByID[incoming[index].id] {
                    incoming[index].isRead = local.isRead
                    incoming[index].isSaved = local.isSaved
                }
            }
        }
        if articles != incoming || preferences != snapshot.preferences {
            feedIndex = snapshot.index
        }
        if articles != incoming { articles = incoming }
        if sources != snapshot.sources { sources = snapshot.sources }
        if categories != snapshot.categories { categories = snapshot.categories }
        if digests != snapshot.digests { digests = snapshot.digests }
        if preferences != snapshot.preferences { preferences = snapshot.preferences }
    }

    private func loadCache() async {
        guard let cache else { return }
        let revision = preferencesRevision
        do {
            let snapshot = try await Task.detached(priority: .utility) { try ArchiveSnapshot(cache: cache) }.value
            guard self.cache === cache, preferencesRevision == revision else { return }
            apply(snapshot)
        } catch { self.error = "Could not load the local archive: \(error.localizedDescription)" }
    }

    func connect(url: String, token: String) async {
        do {
            let url = url.trimmingCharacters(in: .whitespacesAndNewlines)
            try CredentialStore.save(token, for: url)
            self.backendURL = url; self.token = token
            UserDefaults.standard.set(url, forKey: "backendURL")
            status = nil; online = false
            try openCache(demo: UserDefaults.standard.bool(forKey: "demo:" + url))
            articles = []; sources = []; categories = []; digests = []
            await loadCache()
            await sync()
        } catch { self.error = error.localizedDescription }
    }

    func sync() async {
        guard !busy else { syncPending = true; return }
        busy = true
        let requestedPreferencesRevision = preferencesRevision
        let client = api
        defer {
            busy = false
            if syncPending {
                syncPending = false
                Task { await sync() }
            }
        }
        do {
            try await LocalBackend.shared.ensureRunning(baseURL: client.baseURL, token: client.token)
            let newStatus = try await client.status()
            async let remoteArticles = client.articles()
            async let remoteSources = client.sources()
            async let remoteCategories = client.categories()
            async let remoteDigests = client.digests()
            async let remotePreferences = client.preferences()
            let (items, feeds, groups, digests, prefs) = try await (remoteArticles, remoteSources, remoteCategories, remoteDigests, remotePreferences)
            guard client.baseURL == backendURL, client.token == token else { return }
            if newStatus.demo != cachedMode { try openCache(demo: newStatus.demo) }
            guard let cache else { return }
            let cachedArticles = try await Task.detached(priority: .utility) {
                try cache.merge(items)
                try cache.store(feeds, named: "sources")
                try cache.store(groups, named: "categories")
                try cache.store(digests, named: "digests")
                return try cache.articles()
            }.value
            guard self.cache === cache else { return }
            // Reconcile flags changed while archive work ran in the background.
            var incoming = cachedArticles
            for index in incoming.indices {
                if let local = articlesByID[incoming[index].id] {
                    incoming[index].isRead = local.isRead; incoming[index].isSaved = local.isSaved
                }
            }
            let currentPrefs = requestedPreferencesRevision == preferencesRevision ? prefs : preferences
            let needsIndex = articles != incoming || preferences != currentPrefs
            if articles != incoming { articles = incoming }
            if sources != feeds { sources = feeds }
            if categories != groups { categories = groups }
            if self.digests != digests { self.digests = digests }
            if preferences != currentPrefs { preferences = currentPrefs }
            try await persistPreferences(rebuildIndex: needsIndex)
            if status != newStatus { status = newStatus }
            await configureDeliverySchedule()
            online = true; error = nil
            UserDefaults.standard.set(newStatus.demo, forKey: "demo:" + backendURL)
        } catch {
            online = false
            self.error = "Could not sync. Your downloaded stories remain available. \(error.localizedDescription)"
        }
    }

    func collect() async {
        do { try await api.refresh(); notice = "Collecting enabled sources…"; await sync() }
        catch { self.error = error.localizedDescription }
    }

    func markRead(_ id: String) {
        guard let index = articles.firstIndex(where: { $0.id == id }), !articles[index].isRead else { return }
        articles[index].isRead = true
        let cache = cache
        Task {
            do { try await Task.detached(priority: .utility) { try cache?.mark(id, read: true) }.value }
            catch { self.error = error.localizedDescription }
        }
    }

    func toggleSaved(_ id: String) {
        guard let index = articles.firstIndex(where: { $0.id == id }) else { return }
        let saved = !articles[index].isSaved
        articles[index].isSaved = saved
        let cache = cache
        Task {
            do { try await Task.detached(priority: .utility) { try cache?.mark(id, saved: saved) }.value }
            catch { self.error = error.localizedDescription }
        }
    }

    func saveCategory(_ category: FeedCategory, isNew: Bool) async throws {
        _ = try await api.saveCategory(category, isNew: isNew)
        await sync()
    }

    func deleteCategory(_ category: FeedCategory) async {
        do { try await api.deleteCategory(category); await sync() }
        catch { self.error = error.localizedDescription }
    }

    func saveSource(_ source: FeedSource, isNew: Bool) async throws {
        _ = try await api.saveSource(source, isNew: isNew)
        await sync()
    }

    func deleteSource(_ source: FeedSource) async {
        do { try await api.deleteSource(source); await sync() }
        catch { self.error = error.localizedDescription }
    }

    func savePreferences(_ value: Preferences) async {
        do {
            preferences = try await api.savePreferences(value)
            preferencesRevision += 1
            try await persistPreferences()
            await configureDeliverySchedule()
            notice = "Digest preferences saved"
        } catch { self.error = error.localizedDescription }
    }

    func saveProfile(_ profile: String, enabled: Bool) async throws {
        preferences = try await api.saveProfile(profile, enabled: enabled)
        preferencesRevision += 1
        try await persistPreferences()
        notice = enabled ? "Profile saved" : "Profile saved · prioritization disabled"
        await sync()
    }

    private func configureDeliverySchedule() async {
        do {
            backgroundDeliveryActive = try await LocalBackend.shared.configureDailyDelivery(
                enabled: preferences.telegramEnabled && status?.telegramBackgroundConfigured == true, baseURL: backendURL)
            backgroundDeliveryError = nil
        } catch { backgroundDeliveryError = error.localizedDescription }
    }

    private func persistPreferences(rebuildIndex: Bool = true) async throws {
        // A save may arrive during a background sync. Repeat with the latest
        // revision so neither cached settings nor feed order can become stale.
        while true {
            let prefs = preferences
            let items = articles
            let cache = cache
            let revision = preferencesRevision
            let index = try await Task.detached(priority: .utility) {
                try cache?.store(prefs, named: "preferences")
                return rebuildIndex ? FeedIndex(articles: items, preferences: prefs) : nil
            }.value
            guard self.cache === cache else { return }
            if revision != preferencesRevision { continue }
            if let index { feedIndex = index; objectWillChange.send() }
            return
        }
    }

    func refreshRelevance() async throws {
        try await api.refreshRelevance()
        notice = "Relevance refresh requested"
        await sync()
    }

    func makeDigest() async {
        guard !busy else { return }
        busy = true
        do { _ = try await api.makeDigest(); busy = false; await sync(); notice = "Digest prepared" }
        catch { busy = false; self.error = error.localizedDescription }
    }

    func sendTelegram(kind: String, articleID: String? = nil) async {
        guard !sending else { return }
        sending = true
        defer { sending = false }
        do {
            let result = try await api.sendTelegram(kind: kind, articleID: articleID)
            if result.state == "sent" { notice = "Sent to your Telegram chat" }
            else { error = result.error ?? "Delivery is \(result.state). Check the chat before retrying." }
        } catch { self.error = error.localizedDescription }
    }
}
