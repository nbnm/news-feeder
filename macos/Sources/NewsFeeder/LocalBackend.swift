import AppKit
import FeedCore
import Foundation

/// Installed builds own their local collector. Development launchers and remote
/// connections retain their existing lifecycle and configuration.
@MainActor
final class LocalBackend {
    static let shared = LocalBackend()
    private var process: Process?
    private var output: FileHandle?
    private var starting = false
    private var activeToken = ""
    private var scheduledDelivery: Bool?

    func ensureRunning(baseURL: String, token: String) async throws {
        guard ProcessInfo.processInfo.environment["NEWS_FEEDER_BACKEND_URL"] == nil,
              baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == "http://127.0.0.1:8000",
              let resources = Bundle.main.resourceURL else { return }
        let executable = resources.appendingPathComponent("Backend/NewsFeederService")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { return }
        while starting { try await Task.sleep(for: .milliseconds(100)) }
        if process?.isRunning == true, activeToken == token { return }
        starting = true
        defer { starting = false }
        if process?.isRunning == true { await stop() }
        // Reuse a deliberately started local development service if available.
        if await isReady(baseURL: baseURL, token: token) { return }

        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("NewsFeeder")
        let service = support.appendingPathComponent("service")
        let logs = support.appendingPathComponent("logs")
        for directory in [support, service, logs] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
        }
        let logURL = logs.appendingPathComponent("service.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil,
                attributes: [.posixPermissions: 0o600])
        }
        try output?.close()
        output = try FileHandle(forWritingTo: logURL)
        try output?.seekToEnd()
        let child = Process()
        child.executableURL = executable
        child.currentDirectoryURL = service
        child.arguments = ["--port", "8000", "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        var environment = ProcessInfo.processInfo.environment
        // The installed service never depends on Homebrew, a venv, or the source
        // checkout. The private, writable database is outside the signed bundle.
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        environment["DATABASE_URL"] = "sqlite:///" + service.appendingPathComponent("newsfeeder.db").path
        environment["DEMO_MODE"] = "false"
        if !token.isEmpty { environment["APP_API_KEY"] = token }
        environment["PYTHONUNBUFFERED"] = "1"
        child.environment = environment
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = output
        child.standardError = output
        try child.run()
        process = child
        activeToken = token
        for attempt in 0..<150 {
            if await isReady(baseURL: baseURL, token: token) { return }
            if !child.isRunning && attempt > 30 { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        await stop()
        throw APIError(message: "The local service could not start. Its log is in Library/Application Support/NewsFeeder/logs/service.log.")
    }

    private func isReady(baseURL: String, token: String) async -> Bool {
        guard let url = URL(string: baseURL + "/status") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1
        if !token.isEmpty { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return false }
        return (try? makeDecoder().decode(BackendStatus.self, from: data)) != nil
    }

    func stop() async {
        guard let child = process else { return }
        if child.isRunning {
            child.terminate()
            for _ in 0..<50 {
                if !child.isRunning { break }
                try? await Task.sleep(for: .milliseconds(100))
            }
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
        process = nil
        try? output?.close()
        output = nil
    }

    func configureDailyDelivery(enabled: Bool, baseURL: String) async throws -> Bool {
        guard ProcessInfo.processInfo.environment["NEWS_FEEDER_BACKEND_URL"] == nil,
              baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == "http://127.0.0.1:8000",
              let executable = Bundle.main.resourceURL?.appendingPathComponent("Backend/NewsFeederService"),
              FileManager.default.isExecutableFile(atPath: executable.path) else { return false }
        if scheduledDelivery == enabled { return enabled }
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("NewsFeeder")
        let agents = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
        let plist = agents.appendingPathComponent("app.newsfeeder.digest.plist")
        let domain = "gui/\(getuid())"
        let label = "app.newsfeeder.digest"
        if !enabled {
            if FileManager.default.fileExists(atPath: plist.path) {
                _ = try await Self.launchctl(["bootout", domain + "/" + label])
                try FileManager.default.removeItem(at: plist)
            }
            scheduledDelivery = false
            return false
        }
        try FileManager.default.createDirectory(at: agents, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: support.appendingPathComponent("logs"), withIntermediateDirectories: true)
        let job: [String: Any] = [
            "Label": label, "ProgramArguments": [executable.path, "--daily-digest"],
            "WorkingDirectory": support.appendingPathComponent("service").path,
            "EnvironmentVariables": ["DATABASE_URL": "sqlite:///" + support.appendingPathComponent("service/newsfeeder.db").path,
                                     "DEMO_MODE": "false", "PYTHONUNBUFFERED": "1"],
            "StartCalendarInterval": [0, 15, 30, 45].map { ["Minute": $0] },
            "RunAtLoad": true, "ProcessType": "Background", "LowPriorityIO": true,
            "StandardOutPath": support.appendingPathComponent("logs/digest.log").path,
            "StandardErrorPath": support.appendingPathComponent("logs/digest.log").path,
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: job, format: .xml, options: 0)
        let unchanged = (try? Data(contentsOf: plist)) == data
        if unchanged {
            let loaded = try await Self.launchctl(["print", domain + "/" + label])
            if loaded == 0 {
                scheduledDelivery = true
                return true
            }
        }
        _ = try await Self.launchctl(["bootout", domain + "/" + label])
        try data.write(to: plist, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: plist.path)
        guard try await Self.launchctl(["bootstrap", domain, plist.path]) == 0 else {
            throw APIError(message: "Background delivery could not be enabled on this Mac. Keep News Feeder open for automatic publishing.")
        }
        scheduledDelivery = true
        return true
    }

    private nonisolated static func launchctl(_ arguments: [String]) async throws -> Int32 {
        try await Task.detached(priority: .utility) {
            let command = Process()
            command.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            command.arguments = arguments
            command.standardOutput = FileHandle.nullDevice
            command.standardError = FileHandle.nullDevice
            try command.run()
            command.waitUntilExit()
            return command.terminationStatus
        }.value
    }
}
