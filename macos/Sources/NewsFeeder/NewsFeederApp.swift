import AppKit
import SwiftUI

final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    private var terminating = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let icon = NSImage(contentsOf: url) {
            NSApplication.shared.applicationIconImage = icon
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminating else { return .terminateLater }
        terminating = true
        Task { @MainActor in
            await LocalBackend.shared.stop()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main
struct NewsFeederApp: App {
    @NSApplicationDelegateAdaptor(ApplicationDelegate.self) private var applicationDelegate
    @StateObject private var model = AppModel()
    @StateObject private var reading = ReadingAppearance()

    var body: some Scene {
        WindowGroup("News Feeder") {
            ReaderView().environmentObject(model).environmentObject(reading)
                .preferredColorScheme(reading.theme.colorScheme)
                .frame(minWidth: 960, minHeight: 640)
                .task {
                    NSApplication.shared.setActivationPolicy(.regular)
                    NSApplication.shared.activate(ignoringOtherApps: true)
                    await model.start()
                }
        }
        .defaultSize(width: 1280, height: 820)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Sync Archive") { Task { await model.sync() } }.keyboardShortcut("r")
                Button("Collect Sources") { Task { await model.collect() } }.keyboardShortcut("r", modifiers: [.command, .shift])
            }
            CommandGroup(after: .toolbar) {
                Divider()
                Button("Increase Text Size") { reading.changeSize(by: 1) }
                    .keyboardShortcut("+", modifiers: .command).disabled(reading.bodySize >= 26)
                Button("Decrease Text Size") { reading.changeSize(by: -1) }
                    .keyboardShortcut("-", modifiers: .command).disabled(reading.bodySize <= 14)
                Button("Actual Text Size") { reading.fontSize = 18 }.keyboardShortcut("0")
            }
        }
        Settings {
            SettingsView().environmentObject(model).environmentObject(reading)
                .preferredColorScheme(reading.theme.colorScheme)
                .frame(width: 680, height: 560)
        }
    }
}
