import AppKit
import FeedCore
import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            ConnectionSettings().tabItem { Label("Connection", systemImage: "network") }
            ModelSettings().tabItem { Label("Models", systemImage: "cpu") }
            ProfileSettings().tabItem { Label("Profile", systemImage: "person.text.rectangle") }
            ReadingAppearanceControls().tabItem { Label("Appearance", systemImage: "paintbrush") }
            SourceSettings().tabItem { Label("Sources", systemImage: "dot.radiowaves.left.and.right") }
            DigestSettings().tabItem { Label("Digest & Telegram", systemImage: "sun.max") }
        }.padding(16)
    }
}

struct ProfileSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var profile = ""
    @State private var enabled = true
    @State private var working = false
    @State private var error: String?
    @State private var notice: String?

    var body: some View {
        Form {
            Section("Your news profile") {
                Text("Describe your work, interests, tools, and what deserves your attention. Be specific about releases and topics you want to follow or avoid.")
                    .font(.callout).foregroundStyle(.secondary)
                TextEditor(text: $profile).font(.body)
                    .frame(minHeight: 135, maxHeight: 180).padding(8)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25)))
                    .accessibilityLabel("News relevance profile")
                Toggle("Prioritize news for my profile", isOn: $enabled)
                Text("Jev labels each story and its topics High, Medium, or Low relevance to your profile. Your profile and the story's available text are sent through your configured Secondstack gateway. Saving changes refreshes assessments automatically.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button(working ? "Saving…" : "Save Profile") {
                        Task {
                            working = true; error = nil; notice = nil
                            do { try await model.saveProfile(profile, enabled: enabled); notice = "Profile saved. Relevance assessments refresh in the background." }
                            catch { self.error = error.localizedDescription }
                            working = false
                        }
                    }.disabled(working || !model.online || profile.count > 6000)
                    Button("Retry Missing Assessments") {
                        Task {
                            working = true; error = nil
                            do { try await model.refreshRelevance(); notice = "Retrying missing assessments in the background." }
                            catch { self.error = error.localizedDescription }
                            working = false
                        }
                    }.disabled(working || !model.online || model.status?.ranking == true || !enabled || profile.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            Section("Relevance model") {
                LabeledContent("Model", value: model.status?.jevModel ?? "typesafe/jev-1.13")
                LabeledContent("Gateway key", value: model.status?.jevConfigured == true ? "Configured · shared with summaries" : "Add a key in Models")
                if model.status?.ranking == true { Label("Assessing stories…", systemImage: "arrow.triangle.2.circlepath") }
                Text("Relevance tags reflect the match to your profile. Confidence describes how clearly the model chose a rubric level; it is not a guarantee of accuracy. Popularity remains a separate Hacker News signal.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
            if let notice { Text(notice).font(.caption).foregroundStyle(.secondary) }
        }.formStyle(.grouped)
            .onAppear { profile = model.preferences.profile ?? ""; enabled = model.preferences.relevanceEnabled ?? true }
    }
}

struct ConnectionSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var url = ""
    @State private var token = ""
    @State private var connecting = false

    var body: some View {
        Form {
            Section {
                TextField("Backend URL", text: $url).textContentType(.URL)
                SecureField("Backend access key", text: $token)
                Text("The access key is stored in macOS Keychain. Configure your gateway in Models and Telegram on the backend.")
                    .font(.caption).foregroundStyle(.secondary)
                Button(connecting ? "Connecting…" : "Save & Connect") {
                    Task { connecting = true; await model.connect(url: url, token: token); connecting = false }
                }.disabled(connecting || url.isEmpty)
            } header: { Text("Your collection service") }
            Section("Status") {
                LabeledContent("Connection", value: model.online ? "Connected" : "Offline")
                LabeledContent("LiteLLM", value: model.status?.llmConfigured == true ? "Configured" : "Awaiting configuration")
                LabeledContent("Telegram", value: model.status?.telegramConfigured == true ? "Configured" : "Awaiting configuration")
                if let error = model.status?.workerError { Text("Worker: \(error)").foregroundStyle(.orange) }
                if model.status?.demo == true { Label("Demo uses fictional examples. External collection and messaging are disabled.", systemImage: "flask").foregroundStyle(.orange) }
            }
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.formStyle(.grouped)
        .onAppear { url = model.backendURL; token = model.token }
        .onChange(of: url) { _, value in token = CredentialStore.read(for: value) }
    }
}

struct ModelSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var configuration: GatewayConfiguration?
    @State private var models: [GatewayModel] = []
    @State private var selection = ""
    @State private var virtualKey = ""
    @FocusState private var keyFocused: Bool
    @State private var working = false
    @State private var error: String?
    @State private var notice: String?

    private var canSave: Bool {
        !working && model.online && models.contains { $0.id == selection } && selection != configuration?.model
    }

    var body: some View {
        Form {
            Section("T1A LiteLLM gateway") {
                if let configuration {
                    LabeledContent("API base") { Text(configuration.baseUrl).textSelection(.enabled) }
                    LabeledContent("Calls") { Text(configuration.completionsUrl).textSelection(.enabled) }
                        .font(.caption)
                    LabeledContent("Access", value: configuration.apiKeyConfigured ? "Virtual key saved" : "Enter a virtual key")
                } else {
                    Text("Connect to your collection service to configure the gateway.").foregroundStyle(.secondary)
                }
            }
            Section("Gateway access") {
                VStack(alignment: .leading, spacing: 8) {
                    Text(configuration?.apiKeyConfigured == true ? "Replace LiteLLM API key" : "LiteLLM API key")
                        .font(.headline)
                    HStack(spacing: 8) {
                        SecureField("Paste your gateway virtual key", text: $virtualKey)
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: .infinity)
                            .focused($keyFocused)
                            .accessibilityLabel("LiteLLM API key")
                            .privacySensitive()
                        Button("Paste") {
                            if let value = NSPasteboard.general.string(forType: .string) {
                                virtualKey = value.trimmingCharacters(in: .whitespacesAndNewlines)
                            }
                            keyFocused = true
                        }.accessibilityLabel("Paste LiteLLM API key")
                    }
                }.padding(.vertical, 4)
                Text("The key is sent to and stored on your collection service. It is never included in archive sync. Leave this field empty to keep the saved key.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Save Key & Load Models") {
                    Task { await saveKey() }
                }.disabled(working || !model.online || virtualKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            Section("Summaries") {
                Picker("Summary model", selection: $selection) {
                    Text("Choose a model").tag("")
                    if !selection.isEmpty && !models.contains(where: { $0.id == selection }) {
                        Text(selection + " (not in loaded catalog)").tag(selection)
                    }
                    ForEach(models) { item in Text(item.id).tag(item.id) }
                }.disabled(working || models.isEmpty || !model.online)
                HStack {
                    Button("Reload Models") { Task { await reload() } }
                        .disabled(working || !model.online || configuration?.apiKeyConfigured != true)
                    Button("Use Selected Model") { Task { await saveModel() } }.disabled(!canSave)
                    if working { ProgressView().controlSize(.small) }
                }
                Text("Models come from the gateway's live catalog. The selected model is used for new summaries and must support structured JSON outputs. Existing summaries remain in your archive.")
                    .font(.caption).foregroundStyle(.secondary)
                if model.status?.demo == true {
                    Label("Demo stories remain fictional; this demo does not make summary calls.", systemImage: "flask")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
            if let notice { Text(notice).font(.caption).foregroundStyle(.secondary) }
        }.formStyle(.grouped)
        .task(id: model.backendURL) {
            await reload()
            if configuration?.apiKeyConfigured != true { keyFocused = true }
        }
    }

    @MainActor private func loadCatalog() async throws {
        let catalog = try await model.api.gatewayModels()
        models = catalog.models
        selection = catalog.selectedModel
        if models.isEmpty { notice = "This virtual key has no available models." }
    }

    @MainActor private func reload() async {
        guard !working else { return }
        working = true; error = nil; notice = nil
        defer { working = false }
        do {
            configuration = try await model.api.gatewayConfiguration()
            selection = configuration?.model ?? ""
            models = []
            if configuration?.apiKeyConfigured == true { try await loadCatalog() }
        } catch { self.error = error.localizedDescription }
    }

    @MainActor private func saveKey() async {
        working = true; error = nil; notice = nil
        defer { working = false }
        do {
            configuration = try await model.api.configureGateway(apiKey: virtualKey.trimmingCharacters(in: .whitespacesAndNewlines))
            virtualKey = ""
            try await loadCatalog()
            await model.sync()
            notice = models.isEmpty ? "This virtual key has no available models." : "Key saved. Choose a summary model."
        } catch { self.error = error.localizedDescription }
    }

    @MainActor private func saveModel() async {
        working = true; error = nil; notice = nil
        defer { working = false }
        do {
            configuration = try await model.api.configureGateway(model: selection)
            await model.sync()
            notice = "Summary model saved for the next collection cycle."
        } catch { self.error = error.localizedDescription }
    }
}

struct SourceSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var editor: FeedSource?
    @State private var isNew = false
    @State private var categoryEditor: FeedCategory?
    @State private var isNewCategory = false
    @State private var managingCategories = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Follow what matters").font(.title3.weight(.semibold))
                    Text("Combine sources into categories and check collection status.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Categories") { managingCategories.toggle() }
                Button { isNew = true; editor = FeedSource() } label: { Label("Add Source", systemImage: "plus") }
                    .disabled(!model.online)
            }
            if managingCategories {
                HStack {
                    Text("Categories").font(.headline)
                    Spacer()
                    Button("Add Category") { isNewCategory = true; categoryEditor = FeedCategory() }.disabled(!model.online)
                }
                List(model.categories) { category in
                    HStack {
                        Label(category.name, systemImage: "folder")
                        Spacer()
                        Text("\(model.sources.filter { $0.categoryId == category.id }.count) sources").foregroundStyle(.secondary)
                        Button("Rename") { isNewCategory = false; categoryEditor = category }.disabled(!model.online)
                        Button("Remove", role: .destructive) { Task { await model.deleteCategory(category) } }.disabled(!model.online)
                    }
                }
                Text("Removing a category moves its sources to Uncategorized.").font(.caption).foregroundStyle(.secondary)
            } else {
            List(model.sources) { source in
                HStack(alignment: .center, spacing: 14) {
                    Image(systemName: source.enabled ? "dot.radiowaves.left.and.right" : "pause.circle").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(source.name).font(.headline)
                        Text(model.categories.first { $0.id == source.categoryId }?.name ?? "Uncategorized")
                            .font(.caption).foregroundStyle(ReaderPalette.accent)
                        Text("\(source.kindLabel) · every \(source.refreshMinutes) min")
                            .font(.caption).foregroundStyle(.secondary)
                        if let error = source.lastError { Text(error).font(.caption).foregroundStyle(.orange).lineLimit(2) }
                        else if let refreshed = source.lastRefreshedAt, let date = parseTimestamp(refreshed) {
                            Text("Refreshed \(date.formatted())").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    Toggle("Enabled", isOn: Binding(get: { source.enabled }, set: { value in
                        var changed = source; changed.enabled = value
                        Task { do { try await model.saveSource(changed, isNew: false) } catch { model.error = error.localizedDescription } }
                    })).labelsHidden().toggleStyle(.switch).controlSize(.small).disabled(!model.online)
                    Button("Edit") { isNew = false; editor = source }.disabled(!model.online)
                }.padding(.vertical, 6)
                    .contextMenu { Button("Remove Source", role: .destructive) { Task { await model.deleteSource(source) } }.disabled(!model.online) }
            }
            Text("Removing a source keeps its downloaded articles in your archive.").font(.caption).foregroundStyle(.secondary)
            }
        }.padding(12)
        .sheet(item: $editor) { source in
            SourceEditor(source: source, isNew: isNew).environmentObject(model)
        }
        .sheet(item: $categoryEditor) { category in
            CategoryEditor(category: category, isNew: isNewCategory).environmentObject(model)
        }
    }
}

struct SourceEditor: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State var source: FeedSource
    let isNew: Bool
    @State private var working = false
    @State private var error: String?
    @State private var preview: SourcePreview?
    @State private var topicText = ""

    private var configuredSource: FeedSource {
        var value = source
        value.topics = topicText.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return value
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(isNew ? "Add a source" : "Edit source").font(.title2.weight(.semibold))
            Form {
                TextField("Name", text: $source.name)
                Picker("Category", selection: Binding(get: { source.categoryId ?? "" }, set: { source.categoryId = $0.isEmpty ? nil : $0 })) {
                    Text("Uncategorized").tag("")
                    ForEach(model.categories) { Text($0.name).tag($0.id) }
                }
                Picker("Source type", selection: $source.kind) {
                    Text("RSS / Atom").tag("rss")
                    Text("Website").tag("website")
                    Text("GitHub releases").tag("github")
                    Text("GitHub organization").tag("github_org")
                    Text("GitHub Trending · daily top 10").tag("github_trending")
                    Text("Hacker News").tag("hacker_news")
                }
                TextField("URL", text: $source.url)
                if source.kind == "github_org" {
                    Text("New public repositories and published releases appear in one feed. Repositories do not become individual source subscriptions.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if source.kind == "github" {
                    TextField("Release tag pattern", text: Binding(get: { source.releaseTagPattern ?? "" }, set: { source.releaseTagPattern = $0 }))
                    Text("Optional regular expression for version tags. Leave empty to include all published releases.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if source.kind == "github_trending" {
                    Text("The daily top 10 across all languages, kept in GitHub's order. Your profile adds relevance tags without filtering the list.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    TextField("Topic filters", text: $topicText)
                    Text("Optional, comma-separated. An article must match at least one topic.").font(.caption).foregroundStyle(.secondary)
                }
                Stepper("Refresh every \(source.refreshMinutes) minutes", value: $source.refreshMinutes, in: 15...10080, step: 15)
                Toggle("Enabled", isOn: $source.enabled)
                if source.kind == "website" {
                    TextField("Article link selector", text: $source.linkSelector)
                    TextField("Article path pattern", text: $source.articlePattern)
                    Text("Example: a[href] and ^/news/[^/]+/?$. Websites with dynamic content may need a dedicated connector.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.formStyle(.grouped)
            if let preview {
                VStack(alignment: .leading, spacing: 4) {
                    if let note = preview.note { Text(note) }
                    ForEach(preview.articles, id: \.url) { item in Text("• " + item.title).lineLimit(1) }
                }.font(.caption).foregroundStyle(.secondary)
            }
            if let error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Button("Preview") {
                    Task {
                        working = true; error = nil
                        do { preview = try await model.api.preview(configuredSource) } catch { self.error = error.localizedDescription }
                        working = false
                    }
                }.disabled(working || source.url.isEmpty)
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    Task {
                        working = true; error = nil
                        do { try await model.saveSource(configuredSource, isNew: isNew); dismiss() }
                        catch { self.error = error.localizedDescription }
                        working = false
                    }
                }.keyboardShortcut(.defaultAction).disabled(working || source.name.isEmpty || source.url.isEmpty)
            }
        }.padding(24).frame(width: 600, height: source.kind == "website" || source.kind == "github" ? 690 : 585)
            .onAppear { topicText = source.topics.joined(separator: ", ") }
            .onChange(of: source.kind) { _, kind in
                if kind == "github_trending" {
                    source.url = "https://github.com/trending?since=daily"
                    topicText = ""
                }
            }
    }
}

struct CategoryEditor: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State var category: FeedCategory
    let isNew: Bool
    @State private var working = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(isNew ? "Add a category" : "Rename category").font(.title2.weight(.semibold))
            TextField("Name", text: $category.name)
            Text("Assign sources to this category in the source editor.").font(.caption).foregroundStyle(.secondary)
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    Task {
                        working = true
                        do { try await model.saveCategory(category, isNew: isNew); dismiss() }
                        catch { self.error = error.localizedDescription }
                        working = false
                    }
                }.keyboardShortcut(.defaultAction).disabled(working || category.name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }.padding(24).frame(width: 430)
    }
}

struct DigestSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var preferences = Preferences()
    @State private var topicText = ""
    @State private var configuration: TelegramConfiguration?
    @State private var botToken = ""
    @State private var channel = ""
    @State private var targets: [TelegramTarget] = []
    @State private var confirmRetry = false
    @State private var working = false
    @State private var error: String?
    @State private var notice: String?
    @FocusState private var tokenFocused: Bool

    var body: some View {
        Form {
            Section("Telegram bot") {
                if let configuration, !configuration.botUsername.isEmpty {
                    LabeledContent("Bot", value: "@" + configuration.botUsername)
                }
                HStack {
                    SecureField(configuration?.botTokenConfigured == true ? "Replace bot token" : "BotFather token", text: $botToken)
                        .textFieldStyle(.roundedBorder).focused($tokenFocused)
                        .accessibilityLabel("Telegram bot token").privacySensitive()
                    Button("Paste") {
                        botToken = NSPasteboard.general.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                        tokenFocused = true
                    }.accessibilityLabel("Paste Telegram bot token")
                    Button("Save Bot") {
                        perform {
                            configuration = try await model.api.saveTelegramBot(botToken.trimmingCharacters(in: .whitespacesAndNewlines))
                            botToken = ""; notice = "Bot verified and saved"
                            await model.sync()
                        }
                    }.disabled(botToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || working || !model.online)
                }
                Text("The token stays on your collection service and is never returned to the app. Leave the field empty to keep the saved token.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Destination channel") {
                TextField("Channel", text: $channel, prompt: Text("@your_channel or https://t.me/your_channel"))
                Text("Create a Telegram channel, add your bot as an administrator, and allow it to post messages. Enter the channel's own link, rather than the bot's profile. Private channels can use Find Channels after adding the bot.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Find Channels") {
                        perform {
                            targets = try await model.api.telegramTargets().filter { $0.type == "channel" }
                            if targets.isEmpty { notice = "No channels found yet. Add the bot as a channel administrator, then try again." }
                        }
                    }.disabled(working || !model.online || configuration?.botTokenConfigured != true)
                    Button("Verify & Connect Channel") {
                        perform {
                            configuration = try await model.api.connectTelegramChannel(channel.trimmingCharacters(in: .whitespacesAndNewlines))
                            channel = configuration?.chatId ?? channel
                            notice = "Channel verified · the bot can publish here"
                            await model.sync()
                        }
                    }.disabled(working || !model.online || channel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || configuration?.botTokenConfigured != true)
                    if working { ProgressView().controlSize(.small) }
                }
                if !targets.isEmpty {
                    Picker("Discovered channels", selection: $channel) {
                        Text("Choose a channel").tag("")
                        ForEach(targets) { Text($0.title).tag($0.id) }
                    }
                }
                if configuration?.verified == true {
                    Label("Connected to " + (configuration?.title ?? "channel"), systemImage: "checkmark.circle.fill")
                        .foregroundStyle(ReaderPalette.accent)
                }
            }
            Section("Daily digest") {
                Toggle("Publish a daily digest to the channel", isOn: $preferences.telegramEnabled)
                    .disabled(!model.online || (configuration?.verified != true && !preferences.telegramEnabled))
                Stepper("Up to \(preferences.digestSize) stories", value: $preferences.digestSize, in: 1...20)
                Picker("Publish daily at", selection: $preferences.deliveryHour) {
                    ForEach(0..<24) { hour in Text(String(format: "%02d:00", hour)).tag(hour) }
                }
                TextField("Timezone", text: $preferences.timezone)
                TextField("Digest topic interests", text: $topicText)
                Text("Each digest includes prioritized stories, TLDRs, and source links. Daily posts are tracked to prevent duplicate publication. A missed run is checked after this Mac wakes or you log in; the Mac must be on and online.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Save Schedule") {
                        perform {
                            preferences.topics = topicText.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                            await model.savePreferences(preferences)
                            configuration = try await model.api.telegramConfiguration()
                            preferences = model.preferences
                            notice = model.error == nil ? "Daily schedule saved" : nil
                        }
                    }.disabled(working || !model.online)
                    Button("Publish Today's Digest Now") {
                        perform {
                            let delivery = try await model.api.publishDailyDigest()
                            if delivery.state == "sent" { notice = "Today's digest has been published" }
                            else { error = delivery.error ?? "Delivery status: " + delivery.state }
                            configuration = try await model.api.telegramConfiguration()
                            await model.sync()
                        }
                    }.disabled(working || !model.online || configuration?.verified != true)
                }
                if model.backgroundDeliveryActive {
                    Label("Background delivery is enabled · News Feeder can be closed", systemImage: "clock.badge.checkmark")
                        .font(.caption).foregroundStyle(.secondary)
                } else if preferences.telegramEnabled {
                    Text("Keep your collection service running for automatic delivery.").font(.caption).foregroundStyle(.secondary)
                }
                if let issue = model.backgroundDeliveryError { Text(issue).font(.caption).foregroundStyle(.orange) }
            }
            if let delivery = configuration?.lastDelivery {
                Section("Latest daily delivery") {
                    LabeledContent("Status", value: delivery.state.capitalized)
                    LabeledContent("Posts", value: "\(delivery.sentCount) / \(delivery.totalCount)")
                    if let issue = delivery.error { Text(issue).font(.caption).foregroundStyle(.orange) }
                    if delivery.state == "uncertain" || delivery.state == "sending" {
                        Text("Check the channel before posting again. An interrupted send is not automatically repeated.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if delivery.state != "sent" {
                        Button("Retry Missing Posts…") { confirmRetry = true }.disabled(working || !model.online)
                    }
                }
            }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
            if let notice { Text(notice).font(.caption).foregroundStyle(.secondary) }
        }.formStyle(.grouped)
        .confirmationDialog("Have you checked the channel?", isPresented: $confirmRetry) {
            Button("The pending post is missing — retry") {
                if let delivery = configuration?.lastDelivery {
                    perform {
                        let result = try await model.api.retryTelegramDelivery(delivery.id)
                        configuration = try await model.api.telegramConfiguration()
                        if result.state == "sent" { notice = "Missing posts published" }
                        else { error = result.error ?? "Delivery status: " + result.state }
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Retry only after checking that the next pending post did not arrive. Posts already confirmed as sent will be skipped.")
        }
        .onAppear {
            preferences = model.preferences
            topicText = preferences.topics.joined(separator: ", ")
        }
        .task {
            do {
                configuration = try await model.api.telegramConfiguration()
                channel = configuration?.chatId ?? ""
            } catch { self.error = error.localizedDescription }
        }
    }

    private func perform(_ operation: @escaping @MainActor () async throws -> Void) {
        Task {
            working = true; error = nil; notice = nil
            do { try await operation() }
            catch { self.error = error.localizedDescription }
            working = false
        }
    }
}
