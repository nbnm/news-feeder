import AppKit
import FeedCore
import SwiftUI

private let ink = ReaderPalette.accent

struct ReaderView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var reading: ReadingAppearance
    @State private var section = "all"
    @State private var selectedID: String?
    @State private var search = ""
    @State private var showSummary = true
    @State private var showReadingControls = false
    @AppStorage("feedSort") private var feedSort = "priority"

    private var trendingSource: FeedSource? {
        if let source = model.sources.first(where: { $0.id == section && $0.kind == "github_trending" }) { return source }
        let members = model.sources.filter { "category:" + ($0.categoryId ?? "") == section }
        return members.count == 1 && members[0].kind == "github_trending" ? members[0] : nil
    }

    private var selected: Article? { selectedID.flatMap { model.articlesByID[$0] } }
    private var title: String {
        switch section {
        case "all": "All stories"
        case "unread": "Unread"
        case "saved": "Saved"
        case "digest": "Daily digest"
        default: model.categories.first { "category:" + $0.id == section }?.name
            ?? model.sources.first { $0.id == section }?.name ?? "Archive"
        }
    }

    var body: some View {
        let visibleArticles = model.visibleArticles(section: section, search: search, sort: feedSort)
        NavigationSplitView {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "newspaper.fill").font(.title2).foregroundStyle(ink)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("News Feeder").font(.headline)
                        Text("Your industry, in focus.").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }.padding(20)
                List(selection: $section) {
                    Section("Library") {
                        Label("All stories", systemImage: "tray.full").tag("all")
                        HStack { Label("Unread", systemImage: "circle"); Spacer(); Text("\(model.unreadCount)").foregroundStyle(.secondary) }.tag("unread")
                        Label("Saved", systemImage: "bookmark").tag("saved")
                        Label("Daily digest", systemImage: "sun.max").tag("digest")
                    }
                    Section("Categories") {
                        ForEach(model.categories) { category in
                            DisclosureGroup {
                                ForEach(model.sources.filter { $0.categoryId == category.id }) { source in
                                    sourceRow(source).tag(source.id)
                                }
                            } label: {
                                Label(category.name, systemImage: "folder")
                            }.tag("category:" + category.id)
                        }
                    }
                    if model.sources.contains(where: { $0.categoryId == nil }) {
                        Section("Uncategorized") {
                            ForEach(model.sources.filter { $0.categoryId == nil }) { source in
                                sourceRow(source).tag(source.id)
                            }
                        }
                    }
                }.listStyle(.sidebar)
                VStack(alignment: .leading, spacing: 9) {
                    if model.status?.demo == true {
                        Label("Demo · fictional stories", systemImage: "flask").foregroundStyle(.orange)
                    }
                    HStack {
                        Circle().fill(model.online ? ink : Color.secondary).frame(width: 6, height: 6)
                        Text(model.online ? "Connected" : "Offline archive").foregroundStyle(.secondary)
                        Spacer()
                        SettingsLink { Image(systemName: "gearshape") }.buttonStyle(.plain).help("Settings")
                    }
                }.font(.caption).padding(16)
            }
            .navigationSplitViewColumnWidth(min: 205, ideal: 230, max: 280)
        } content: {
            Group {
            if section == "digest" {
                DigestListView(selectedID: $selectedID)
            } else {
                VStack(spacing: 0) {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(title).font(.title2.weight(.semibold))
                            Text(trendingSource == nil ? "\(visibleArticles.count) stories in your archive" : "\(visibleArticles.count) repositories · daily trending")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if model.busy || model.status?.collecting == true || model.status?.ranking == true { ProgressView().controlSize(.small) }
                    }.padding(20)
                    HStack {
                        if let source = trendingSource {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Daily top 10 · GitHub order").font(.subheadline)
                                if let snapshot = source.trending, let date = parseTimestamp(snapshot.observedAt) {
                                    Text("Updated \(date.formatted(date: .abbreviated, time: .shortened))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        } else {
                            Picker("Sort stories", selection: $feedSort) {
                                Text("For you").tag("priority")
                                Text("Latest").tag("latest")
                            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 220)
                        }
                        Spacer()
                    }.padding(.horizontal, 20).padding(.bottom, 12)
                    Divider()
                    if visibleArticles.isEmpty {
                        ContentUnavailableView(search.isEmpty ? "Your feed starts here" : "No matching stories",
                            systemImage: search.isEmpty ? "newspaper" : "magnifyingglass",
                            description: Text(search.isEmpty ? "Configure your sources, then collect the latest updates." : "Try another title, topic, or source."))
                    } else {
                        List(visibleArticles, selection: $selectedID) { article in
                            ArticleRow(article: article, isSelected: selectedID == article.id, sourceLabel: trendingSource?.name).tag(article.id)
                                .contextMenu {
                                    Button(article.isSaved ? "Unsave" : "Save") { model.toggleSaved(article.id) }
                                    Button("Open Source") { if let url = URL(string: article.url) { NSWorkspace.shared.open(url) } }
                                }
                        }.listStyle(.inset).scrollContentBackground(.hidden)
                    }
                }.searchable(text: $search, prompt: "Search your archive")
            }
            }.navigationSplitViewColumnWidth(min: 300, ideal: 350, max: 500)
        } detail: {
            if let article = selected {
                ArticleReader(article: article, showSummary: $showSummary)
            } else {
                ContentUnavailableView("A little context goes a long way", systemImage: "text.alignleft",
                    description: Text("Choose a story to read its summary, evidence, and original text."))
            }
        }
        .tint(ink)
        .toolbar {
            ToolbarItemGroup {
                Button { showReadingControls.toggle() } label: { Label("Reading Appearance", systemImage: "textformat.size") }
                    .help("Change font, text size, and theme")
                    .popover(isPresented: $showReadingControls, arrowEdge: .top) {
                        ReadingAppearanceControls().environmentObject(reading)
                            .frame(width: 360, height: 510)
                    }
                Button { Task { await model.collect() } } label: { Label("Collect", systemImage: "arrow.clockwise") }
                    .disabled(model.busy || model.status?.collecting == true || !model.online)
                    .help("Fetch enabled sources and sync the archive")
                Button { Task { await model.sendTelegram(kind: "feed") } } label: { Label("Send Feed", systemImage: "paperplane") }
                    .disabled(model.status?.telegramConfigured != true || model.sending || !model.online)
                    .help("Send your prioritized feed to your configured Telegram chat")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if let error = model.error {
                HStack { Image(systemName: "exclamationmark.circle"); Text(error).lineLimit(2); Spacer(); Button("Dismiss") { model.error = nil } }
                    .font(.caption).padding(10).background(.regularMaterial)
            } else if let notice = model.notice {
                HStack { Image(systemName: "checkmark.circle"); Text(notice); Spacer(); Button("Dismiss") { model.notice = nil } }
                    .font(.caption).padding(10).background(.regularMaterial)
            }
        }
        .onChange(of: selectedID) { _, id in if let id { model.markRead(id) } }
        .onChange(of: section) { _, _ in selectedID = nil }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                if Task.isCancelled { break }
                await model.sync()
                if model.status?.collecting != true && model.notice == "Collecting enabled sources…" { model.notice = nil }
            }
        }
    }
    private func sourceRow(_ source: FeedSource) -> some View {
        HStack {
            Image(systemName: source.kind == "github_org" ? "building.2" : "dot.radiowaves.left.and.right")
                .foregroundStyle(source.enabled ? ink : .secondary)
            Text(source.name).lineLimit(1)
            if source.lastError != nil { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange) }
        }
    }

}

struct RelevanceTag: View {
    @EnvironmentObject private var reading: ReadingAppearance
    let level: RelevanceLevel
    var showContext = false
    var isSelected = false

    private var tint: Color {
        switch level {
        case .high: ink
        case .medium: .orange
        case .low: .secondary
        }
    }

    var body: some View {
        Text(level.rawValue + (showContext ? " relevance" : ""))
            .font(.system(size: reading.tagSize, weight: .semibold))
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(isSelected ? Color.white : Color.primary)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background((isSelected ? Color.white : tint).opacity(0.10), in: Capsule())
            .overlay(Capsule().stroke((isSelected ? Color.white : tint).opacity(0.25), lineWidth: 0.5))
            .accessibilityLabel(level.rawValue + " relevance to your profile")
    }
}

struct ArticleRow: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var reading: ReadingAppearance
    let article: Article
    var isSelected = false
    var sourceLabel: String?

    private var primaryText: Color { isSelected ? .white : .primary }
    private var secondaryText: Color { isSelected ? .white.opacity(0.85) : .secondary }
    private var accent: Color { isSelected ? .white : ink }
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Circle().fill(article.isRead ? Color.clear : accent).frame(width: 5, height: 5)
                Text((sourceLabel ?? article.sourceName).uppercased()).font(.system(size: reading.metadataSize, weight: .semibold))
                    .tracking(0.4).foregroundStyle(secondaryText).lineLimit(1)
                Spacer()
                if let relevance = article.currentRelevance(for: model.preferences) {
                    RelevanceTag(level: relevance.level, showContext: true, isSelected: isSelected)
                        .help("Jev relevance to your saved profile")
                }
                if article.isSaved { Image(systemName: "bookmark.fill").font(.caption).foregroundStyle(accent) }
            }
            Text(article.displayTitle).font(reading.font(size: reading.feedTitleSize, weight: article.isRead ? .medium : .semibold))
                .foregroundStyle(primaryText).lineLimit(3).lineSpacing(2)
            VStack(alignment: .leading, spacing: 4) {
                Text(article.summary == nil ? (article.tldrKind == "headline" ? "TLDR · HEADLINE" : "TLDR · SOURCE EXCERPT") : "TLDR")
                    .font(.system(size: reading.metadataSize, weight: .semibold)).foregroundStyle(accent)
                Text(article.previewText).font(reading.font(size: reading.feedBodySize))
                    .foregroundStyle(primaryText.opacity(0.85)).lineSpacing(3).lineLimit(3)
            }
            if let repository = article.repository {
                RepositoryStatsView(repository: repository).foregroundStyle(secondaryText)
            }
            HStack(spacing: 8) {
                if let topic = article.summary?.topics.first { Text(topic).foregroundStyle(accent).lineLimit(1) }
                if let date = parseTimestamp(article.publishedAt ?? article.discoveredAt) {
                    Text(date, style: .relative).foregroundStyle(secondaryText).lineLimit(1)
                }
                Spacer()
                if let buzz = article.buzz { Label("\(buzz.points)", systemImage: "bubble.left").foregroundStyle(secondaryText) }
            }.font(.system(size: reading.metadataSize))
        }.padding(.vertical, 12)
    }
}

struct RepositoryStatsView: View {
    @EnvironmentObject private var reading: ReadingAppearance
    let repository: RepositoryMetadata

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text("#\(repository.rank) on GitHub daily")
                if let language = repository.language { Text("·"); Text(language) }
            }
            HStack(spacing: 8) {
                if let stars = repository.starsToday {
                    Label("+\(stars.formatted()) today", systemImage: "star")
                }
                if let total = repository.totalStars { Text("\(total.formatted()) total stars") }
            }
        }.font(.system(size: reading.metadataSize))
    }
}

struct ArticleReader: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var reading: ReadingAppearance
    let article: Article
    @Binding var showSummary: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 25) {
                HStack {
                    Text(article.sourceName).font(.system(size: reading.metadataSize, weight: .medium)).foregroundStyle(ink)
                    Spacer()
                    Button { model.toggleSaved(article.id) } label: { Image(systemName: article.isSaved ? "bookmark.fill" : "bookmark") }
                        .buttonStyle(.plain).help(article.isSaved ? "Unsave story" : "Save story")
                    ShareLink(item: URL(string: article.url) ?? URL(string: "https://example.com")!) { Image(systemName: "square.and.arrow.up") }
                        .buttonStyle(.plain)
                }
                VStack(alignment: .leading, spacing: 14) {
                    Text(article.displayTitle).font(reading.font(size: reading.bodySize + 10, weight: .semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    HStack {
                        if let date = parseTimestamp(article.publishedAt ?? article.discoveredAt) { Text(date, style: .date) }
                        Text("·")
                        Text("\(max(1, article.text.split(separator: " ").count / 220)) min read")
                        Spacer()
                        Link("Source ↗", destination: URL(string: article.url)!).foregroundStyle(ink)
                    }.font(.system(size: reading.metadataSize)).foregroundStyle(.secondary)
                }
                if let repository = article.repository {
                    VStack(alignment: .leading, spacing: 10) {
                        RepositoryStatsView(repository: repository)
                        if let date = parseTimestamp(repository.observedAt) {
                            Text("Trending snapshot: \(date.formatted(date: .abbreviated, time: .shortened))")
                                .font(.system(size: reading.metadataSize)).foregroundStyle(.secondary)
                        }
                        Link("GitHub Trending ↗", destination: URL(string: repository.trendingUrl)!)
                            .font(.system(size: reading.metadataSize)).foregroundStyle(ink)
                    }
                }
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("TLDR").font(.system(size: reading.metadataSize, weight: .semibold)).tracking(1).foregroundStyle(ink)
                    Text(article.previewText).font(reading.font()).lineSpacing(reading.lineSpacing)
                    Text(article.previewLabel).font(.system(size: reading.metadataSize)).foregroundStyle(.secondary)
                }
                Picker("Reading mode", selection: $showSummary) {
                    Text("Summary & evidence").tag(true)
                    Text("Article text").tag(false)
                }.pickerStyle(.segmented).labelsHidden().font(.system(size: 13)).frame(maxWidth: 340)

                if showSummary {
                    if let summary = article.summary {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("WHAT CHANGED").font(.system(size: reading.metadataSize, weight: .semibold)).tracking(1).foregroundStyle(.secondary)
                            Text(summary.whatChanged).font(reading.font()).lineSpacing(reading.lineSpacing)
                        }
                        VStack(alignment: .leading, spacing: 10) {
                            Text("WHY IT MATTERS").font(.system(size: reading.metadataSize, weight: .semibold)).tracking(1).foregroundStyle(ink)
                            Text(summary.whyItMatters).font(reading.font()).lineSpacing(reading.lineSpacing)
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).background(ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
                        if !summary.keyPoints.isEmpty {
                            VStack(alignment: .leading, spacing: 12) {
                                ForEach(summary.keyPoints, id: \.self) { point in
                                    HStack(alignment: .top, spacing: 9) { Text("•").foregroundStyle(ink); Text(point).lineSpacing(reading.lineSpacing) }
                                }
                            }
                        }
                        Label("Practical impact: \(summary.impact) · model assessment", systemImage: "slider.horizontal.3")
                            .font(.system(size: reading.metadataSize)).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 12) {
                            Text("SOURCE EVIDENCE").font(.system(size: reading.metadataSize, weight: .semibold)).tracking(1).foregroundStyle(.secondary)
                            ForEach(Array(summary.citations.enumerated()), id: \.offset) { index, citation in
                                Text("[\(index + 1)] “\(citation.quote)”").font(reading.font(size: reading.bodySize - 2))
                                    .foregroundStyle(.primary.opacity(0.85)).lineSpacing(reading.lineSpacing)
                            }
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Summary \(article.summaryState)", systemImage: "text.bubble").font(.headline)
                            Text(article.summaryError ?? "Choose a model in Settings → Models to generate summaries. Available source text is still readable.")
                                .foregroundStyle(.secondary)
                        }
                    }
                    Divider()
                    VStack(alignment: .leading, spacing: 10) {
                        Text("HACKER NEWS BUZZ").font(.system(size: reading.metadataSize, weight: .semibold)).tracking(1).foregroundStyle(.secondary)
                        if let buzz = article.buzz {
                            HStack { Text("\(buzz.points) points"); Text("·"); Text("\(buzz.comments) comments"); Spacer(); Text(buzz.label.capitalized).foregroundStyle(ink) }
                            if let growth = buzz.pointsPerHour { Text("Observed growth: \(growth, specifier: "%.1f") points/hour").font(.caption) }
                            if let date = parseTimestamp(buzz.observedAt) { Text("Observed \(date.formatted())").font(.caption).foregroundStyle(.secondary) }
                            Link("Read discussion ↗", destination: URL(string: buzz.url)!).font(.caption)
                        } else {
                            Text("Insufficient evidence. No matching Hacker News discussion has been observed.").foregroundStyle(.secondary)
                        }
                    }
                } else {
                    Label(article.repository != nil ? (article.contentState == "full" ? "Repository README" : "GitHub Trending description") : article.contentState == "full" ? "Extracted article text" : article.contentState == "excerpt" ? "Available excerpt" : "Text unavailable",
                          systemImage: "doc.text").font(.caption).foregroundStyle(.secondary)
                    if let message = article.extractionError { Text(message).font(.caption).foregroundStyle(.orange) }
                    Text(article.text.isEmpty ? "Open the source to read this article." : article.text)
                        .font(reading.font()).lineSpacing(reading.lineSpacing)
                }
                if model.status?.telegramConfigured == true {
                    Button { Task { await model.sendTelegram(kind: "feed", articleID: article.id) } } label: {
                        Label("Send this story to Telegram", systemImage: "paperplane")
                    }.disabled(model.sending || !model.online)
                }
                Spacer(minLength: 30)
            }.font(reading.font()).textSelection(.enabled).padding(35)
                .frame(maxWidth: min(760, reading.bodySize * 38), alignment: .leading).frame(maxWidth: .infinity)
        }.background(Color(nsColor: .textBackgroundColor))
    }
}

struct DigestListView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var selectedID: String?

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 14) {
                Text("Daily digest").font(.title2.weight(.semibold))
                Text("The developments worth your attention.").font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Prepare Digest") { Task { await model.makeDigest() } }.disabled(model.busy || !model.online)
                    Button { Task { await model.sendTelegram(kind: "digest") } } label: { Image(systemName: "paperplane") }
                        .disabled(model.status?.telegramConfigured != true || model.sending || !model.online)
                        .help("Send the latest digest to Telegram")
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
            Divider()
            if model.digests.isEmpty {
                ContentUnavailableView("A quieter way to keep up", systemImage: "sun.max",
                    description: Text("Prepare a digest from the stories collected since your previous digest."))
            } else {
                List(selection: $selectedID) {
                    ForEach(model.digests) { digest in
                        Section {
                            if digest.articles.isEmpty { Text("No new stories in this period").foregroundStyle(.secondary) }
                            ForEach(digest.articles) { article in
                                ArticleRow(article: model.articles.first { $0.id == article.id } ?? article,
                                           isSelected: selectedID == article.id).tag(article.id)
                            }
                        } header: {
                            Text(parseTimestamp(digest.endAt)?.formatted(date: .abbreviated, time: .shortened) ?? digest.endAt)
                        }
                    }
                }.listStyle(.inset)
            }
        }
    }
}
