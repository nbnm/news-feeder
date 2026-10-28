import AppKit
import SwiftUI

enum ReadingFont: String, CaseIterable, Identifiable {
    case system, serif, rounded
    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: "System (SF Pro)"
        case .serif: "Serif"
        case .rounded: "Rounded"
        }
    }

    var design: Font.Design {
        switch self {
        case .system: .default
        case .serif: .serif
        case .rounded: .rounded
        }
    }
}

enum ReadingTheme: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var label: String { rawValue.capitalized }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

enum ReaderPalette {
    static let accent = Color(nsColor: NSColor(name: NSColor.Name("NewsFeederAccent")) { appearance in
        if appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua {
            return NSColor(red: 0.58, green: 0.82, blue: 0.76, alpha: 1)
        }
        return NSColor(red: 0.13, green: 0.34, blue: 0.32, alpha: 1)
    })
}

@MainActor
final class ReadingAppearance: ObservableObject {
    private let defaults: UserDefaults
    @Published var typeface: ReadingFont {
        didSet { defaults.set(typeface.rawValue, forKey: "reading.font") }
    }
    @Published var fontSize: Double {
        didSet { defaults.set(bodySize, forKey: "reading.fontSize") }
    }
    @Published var theme: ReadingTheme {
        didSet {
            defaults.set(theme.rawValue, forKey: "reading.theme")
            applyTheme()
        }
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        typeface = ReadingFont(rawValue: defaults.string(forKey: "reading.font") ?? "") ?? .system
        let storedSize = defaults.object(forKey: "reading.fontSize") as? Double ?? 18
        fontSize = storedSize.isFinite ? min(26, max(14, storedSize)) : 18
        theme = ReadingTheme(rawValue: defaults.string(forKey: "reading.theme") ?? "") ?? .system
        applyTheme()
    }

    var bodySize: Double { fontSize.isFinite ? min(26, max(14, fontSize)) : 18 }
    var feedBodySize: Double { max(12, bodySize - 4) }
    var feedTitleSize: Double { max(14, bodySize - 2) }
    var metadataSize: Double { min(14, max(11, bodySize - 6)) }
    var tagSize: Double { min(13, max(11, bodySize - 7)) }
    var lineSpacing: CGFloat { CGFloat(bodySize * 0.3) }

    func font(size: Double? = nil, weight: Font.Weight = .regular) -> Font {
        .system(size: CGFloat(size ?? bodySize), weight: weight, design: typeface.design)
    }

    func changeSize(by amount: Double) { fontSize = min(26, max(14, bodySize + amount)) }
    func resetTypography() { typeface = .system; fontSize = 18 }

    private func applyTheme() {
        switch theme {
        case .system: NSApplication.shared.appearance = nil
        case .light: NSApplication.shared.appearance = NSAppearance(named: .aqua)
        case .dark: NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
        }
    }
}

struct ReadingAppearanceControls: View {
    @EnvironmentObject private var reading: ReadingAppearance

    var body: some View {
        Form {
            Section("Appearance") {
                Picker("Theme", selection: $reading.theme) {
                    ForEach(ReadingTheme.allCases) { theme in Text(theme.label).tag(theme) }
                }.pickerStyle(.segmented)
            }
            Section("Reading text") {
                Picker("Font", selection: $reading.typeface) {
                    ForEach(ReadingFont.allCases) { font in Text(font.label).tag(font) }
                }
                LabeledContent("Text size", value: "\(Int(reading.bodySize)) pt")
                HStack(spacing: 12) {
                    Button { reading.changeSize(by: -1) } label: { Text("A−") }
                        .accessibilityLabel("Smaller text").help("Decrease reading text size")
                        .disabled(reading.bodySize <= 14)
                    Slider(value: $reading.fontSize, in: 14...26, step: 1)
                        .accessibilityLabel("Reading text size")
                    Button { reading.changeSize(by: 1) } label: { Text("A+") }
                        .accessibilityLabel("Larger text").help("Increase reading text size")
                        .disabled(reading.bodySize >= 26)
                }
                Text("Adjusts feed headlines, previews, summaries, article text, and source evidence. Changes are saved on this Mac.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Reset Typography") { reading.resetTypography() }
            }
            Section("Preview") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Your industry, in focus.").font(reading.font(size: reading.bodySize + 4, weight: .semibold))
                    Text("New models, API releases, and practical updates — selected for your profile.")
                        .font(reading.font()).lineSpacing(reading.lineSpacing)
                }.padding(.vertical, 8)
            }
        }.formStyle(.grouped)
    }
}
