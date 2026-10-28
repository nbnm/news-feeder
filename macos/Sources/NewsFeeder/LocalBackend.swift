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
        for _ in 0..<150 {
            if !child.isRunning { break }
            if await isReady(baseURL: baseURL, token: token) { return }
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
}
