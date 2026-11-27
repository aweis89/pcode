import Combine
import Foundation

/// One quota window (e.g. `5h` or `wk`) for a subscription provider.
struct PCodeUsageWindow: Equatable, Decodable, Identifiable {
    let key: String
    let label: String
    let usedPercent: Int
    let resetAt: Double?

    var id: String { key }

    init(key: String, label: String, usedPercent: Int, resetAt: Double? = nil) {
        self.key = key
        self.label = label
        self.usedPercent = usedPercent
        self.resetAt = resetAt
    }

    private enum CodingKeys: String, CodingKey {
        case key
        case label
        case usedPercent = "used_percent"
        case resetAt = "reset_at"
    }

    /// Label shown in the titlebar pill. For the weekly window, replaces the static `wk`
    /// with the compact time remaining until reset (e.g. `6d5h`) when `resetAt` is in the future.
    func displayLabel(now: Date = Date()) -> String {
        if key == "weekly", let resetAt, resetAt.isFinite {
            let remaining = resetAt - now.timeIntervalSince1970
            if remaining > 0 {
                return Self.compactDuration(remaining)
            }
        }
        return label
    }

    static func compactDuration(_ seconds: TimeInterval) -> String {
        let totalMinutes = Int(seconds / 60)
        let days = totalMinutes / 1440
        let hours = (totalMinutes % 1440) / 60
        let minutes = totalMinutes % 60
        if days > 0 {
            return hours > 0 ? "\(days)d\(hours)h" : "\(days)d"
        }
        if hours > 0 {
            return minutes > 0 ? "\(hours)h\(minutes)m" : "\(hours)h"
        }
        return "\(max(minutes, 1))m"
    }
}

/// A model-family quota group (used by Antigravity: Gemini vs. Claude/GPT).
struct PCodeUsageGroup: Equatable, Decodable {
    let name: String
    let windows: [PCodeUsageWindow]
}

/// Decoded payload from `pcode --usage-json <provider>`.
struct PCodeProviderUsage: Equatable, Decodable {
    let provider: String
    let windows: [PCodeUsageWindow]
    let groups: [PCodeUsageGroup]?

    /// The quota windows that apply to `model` (selecting the matching group for Antigravity).
    func windows(forModel model: String) -> [PCodeUsageWindow] {
        if provider == "antigravity", let groups, !groups.isEmpty {
            let modelName = PCodeSessionMetadataDisplay.splitModel(model).name.lowercased()
            if modelName.contains("claude") || modelName.contains("gpt") {
                if let match = groups.first(where: {
                    let name = $0.name.lowercased()
                    return name.contains("claude") || name.contains("gpt")
                }), !match.windows.isEmpty {
                    return match.windows
                }
            } else if modelName.contains("gemini") {
                if let match = groups.first(where: { $0.name.lowercased().contains("gemini") }),
                   !match.windows.isEmpty {
                    return match.windows
                }
            }
            if let first = groups.first(where: { !$0.windows.isEmpty }) {
                return first.windows
            }
        }
        return windows
    }
}

/// App-wide cache of subscription quota percentages (`claude`, `openai-codex`, `antigravity`)
/// for the native window titlebar.
///
/// One fetch per provider serves every pane using that provider, with TTL and
/// failure backoff so multiple panes or logged-out providers never stampede.
final class PCodeUsageStore: ObservableObject {
    @Published private(set) var usageByProvider: [String: PCodeProviderUsage] = [:]

    static let supportedProviders: Set<String> = ["claude", "openai-codex", "antigravity"]

    private var lastSuccess: [String: Date] = [:]
    private var lastAttempt: [String: Date] = [:]
    private var inFlight: Set<String> = []
    private let ttl: TimeInterval
    private let turnRefreshCooldown: TimeInterval
    private let failureBackoff: TimeInterval
    private let fetcher: (String, String, @escaping @Sendable (PCodeProviderUsage?) -> Void) -> Void

    init(
        ttl: TimeInterval = 180,
        turnRefreshCooldown: TimeInterval = 60,
        failureBackoff: TimeInterval = 120,
        fetcher: ((String, String, @escaping @Sendable (PCodeProviderUsage?) -> Void) -> Void)? = nil
    ) {
        self.ttl = ttl
        self.turnRefreshCooldown = turnRefreshCooldown
        self.failureBackoff = failureBackoff
        self.fetcher = fetcher ?? Self.fetchFromCLI
    }

    static func provider(forModel model: String) -> String? {
        guard let provider = PCodeSessionMetadataDisplay.splitModel(model).provider,
              supportedProviders.contains(provider)
        else { return nil }
        return provider
    }

    func windows(forModel model: String) -> [PCodeUsageWindow] {
        guard let provider = Self.provider(forModel: model),
              let usage = usageByProvider[provider]
        else { return [] }
        return usage.windows(forModel: model)
    }

    /// Refresh the provider's quota in the background when the cache is missing or stale.
    func refreshIfNeeded(
        forModel model: String,
        directory: String,
        afterTurn: Bool = false,
        now: Date = Date()
    ) {
        guard let provider = Self.provider(forModel: model) else { return }
        guard !inFlight.contains(provider) else { return }
        if let attempt = lastAttempt[provider],
           lastSuccess[provider].map({ attempt > $0 }) ?? true {
            if now.timeIntervalSince(attempt) < failureBackoff { return }
        } else if let success = lastSuccess[provider] {
            let minAge = afterTurn ? turnRefreshCooldown : ttl
            if now.timeIntervalSince(success) < minAge { return }
        }

        inFlight.insert(provider)
        lastAttempt[provider] = now
        fetcher(provider, directory) { [weak self] result in
            let apply = {
                guard let self else { return }
                self.inFlight.remove(provider)
                if let result {
                    self.usageByProvider[provider] = result
                    self.lastSuccess[provider] = now
                }
            }
            if Thread.isMainThread {
                apply()
            } else {
                DispatchQueue.main.async(execute: apply)
            }
        }
    }

    private static func fetchFromCLI(
        provider: String,
        directory: String,
        completion: @escaping @Sendable (PCodeProviderUsage?) -> Void
    ) {
        DispatchQueue.global(qos: .utility).async {
            var isDir: ObjCBool = false
            let dir = (FileManager.default.fileExists(atPath: directory, isDirectory: &isDir) && isDir.boolValue)
                ? directory
                : NSHomeDirectory()
            PCodeWorkspaceMode.awaitLoginShellCache(timeout: 2.0)
            let launch = PCodeWorkspaceMode.launchConfig(["--usage-json", provider], directory: dir)
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/sh")
            proc.arguments = ["-c", "exec \(launch.command)"]
            proc.currentDirectoryURL = URL(fileURLWithPath: dir)
            proc.environment = ProcessInfo.processInfo.environment.merging(launch.env) { _, new in new }
            let stdout = Pipe()
            proc.standardOutput = stdout
            proc.standardError = FileHandle.nullDevice
            proc.standardInput = FileHandle.nullDevice

            let lock = NSLock()
            var finished = false
            let finish: @Sendable (PCodeProviderUsage?) -> Void = { result in
                lock.lock()
                let first = !finished
                finished = true
                lock.unlock()
                if first { completion(result) }
            }

            let timeout = DispatchWorkItem {
                let pid = proc.processIdentifier
                if proc.isRunning {
                    proc.terminate()
                    if pid > 0 {
                        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1.0) {
                            if proc.isRunning { _ = kill(pid, SIGKILL) }
                        }
                    }
                }
                try? stdout.fileHandleForReading.close()
                finish(nil)
            }
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 35.0, execute: timeout)
            defer { timeout.cancel() }

            do {
                try proc.run()
            } catch {
                finish(nil)
                return
            }

            let data = (try? stdout.fileHandleForReading.readToEnd()) ?? Data()
            proc.waitUntilExit()
            guard proc.terminationStatus == 0,
                  let decoded = try? JSONDecoder().decode(PCodeProviderUsage.self, from: data)
            else {
                finish(nil)
                return
            }
            finish(decoded)
        }
    }
}
