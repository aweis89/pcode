import AppKit
import Foundation

/// Resolves and launches the user's preferred external editor at an optional file line,
/// falling back to `NSWorkspace.shared.open` when no GUI editor is configured, found, or
/// launchable without a TTY.
enum PCodeExternalEditor {
    /// `UserDefaults` key for overriding `$VISUAL` / `$EDITOR` inside Pcode.app.
    static let userDefaultsKey = "PCodeExternalEditor"

    /// A resolved external editor invocation produced from an editor command string.
    struct LaunchSpec: Equatable, Hashable, Sendable {
        let executable: String
        let arguments: [String]
        /// True when the resolved command is a terminal-only editor (e.g. `nvim`, `vim`, `nano`, `emacs`)
        /// that should not be launched headless without a TTY.
        let isTerminalEditor: Bool
    }

    private static let gotoLineEditors: Set<String> = [
        "code",
        "code-insiders",
        "cursor",
        "windsurf",
        "codium",
        "vscodium",
    ]

    private static let colonLineEditors: Set<String> = [
        "zed",
        "subl",
        "sublime",
        "mate",
    ]

    private static let plusLineEditors: Set<String> = [
        "nvim",
        "vim",
        "vi",
        "nano",
        "emacs",
        "emacsclient",
        "hx",
        "helix",
        "micro",
        "bbedit",
    ]

    private static let dashLineFlagEditors: Set<String> = [
        "idea",
        "pycharm",
        "webstorm",
        "goland",
        "clion",
        "rubymine",
        "phpstorm",
        "rider",
        "fleet",
    ]

    private static let terminalOnlyEditors: Set<String> = [
        "nvim",
        "vim",
        "vi",
        "nano",
        "emacs",
        "hx",
        "helix",
        "micro",
        "pico",
        "joe",
        "kak",
    ]

    private static let fallbackSearchDirectories: [String] = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "~/.local/bin",
        "/usr/bin",
        "/bin",
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin",
        "/Applications/Cursor.app/Contents/Resources/app/bin",
        "/Applications/Zed.app/Contents/MacOS",
        "/Applications/Sublime Text.app/Contents/SharedSupport/bin",
    ]

    /// Returns the preferred editor command string by checking `UserDefaults` (`PCodeExternalEditor`),
    /// then `$VISUAL`, then `$EDITOR`.
    static func preferredEditorCommand(
        userDefaults: UserDefaults = .standard,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> String? {
        let candidates = [
            userDefaults.string(forKey: userDefaultsKey),
            environment["VISUAL"],
            environment["EDITOR"],
        ]
        for candidate in candidates {
            guard let trimmed = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !trimmed.isEmpty
            else { continue }
            return trimmed
        }
        return nil
    }

    /// Splits a shell-style editor command string (supporting single quotes, double quotes,
    /// and backslash escapes) into `[executable, flag...]`.
    static func splitCommand(_ command: String) -> [String] {
        var tokens: [String] = []
        var current = ""
        var inSingleQuote = false
        var inDoubleQuote = false
        var escaping = false
        var hasToken = false

        for ch in command {
            if escaping {
                current.append(ch)
                escaping = false
                hasToken = true
                continue
            }

            if inSingleQuote {
                if ch == "'" {
                    inSingleQuote = false
                } else {
                    current.append(ch)
                }
                hasToken = true
                continue
            }

            if inDoubleQuote {
                if ch == "\"" {
                    inDoubleQuote = false
                } else if ch == "\\" {
                    escaping = true
                } else {
                    current.append(ch)
                }
                hasToken = true
                continue
            }

            switch ch {
            case "\\":
                escaping = true
                hasToken = true
            case "'":
                inSingleQuote = true
                hasToken = true
            case "\"":
                inDoubleQuote = true
                hasToken = true
            case let c where c.isWhitespace:
                if hasToken {
                    tokens.append(current)
                    current = ""
                    hasToken = false
                }
            default:
                current.append(ch)
                hasToken = true
            }
        }

        if escaping {
            current.append("\\")
        }
        if hasToken {
            tokens.append(current)
        }
        return tokens
    }

    /// Resolves a bare command name or path to an executable file on disk, checking `$PATH`
    /// first and common macOS GUI-app CLI locations as fallback.
    static func resolveExecutable(
        _ command: String,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> String? {
        guard !command.isEmpty else { return nil }
        if command.contains("/") {
            let expanded = (command as NSString).expandingTildeInPath
            return fileManager.isExecutableFile(atPath: expanded) ? expanded : nil
        }

        var searchDirs: [String] = []
        if let pathEnv = environment["PATH"], !pathEnv.isEmpty {
            searchDirs.append(contentsOf: pathEnv.split(separator: ":").map(String.init))
        }
        searchDirs.append(contentsOf: fallbackSearchDirectories)

        var seen = Set<String>()
        for rawDir in searchDirs {
            let dir = (rawDir as NSString).expandingTildeInPath
            guard !dir.isEmpty, seen.insert(dir).inserted else { continue }
            let candidate = (dir as NSString).appendingPathComponent(command)
            if fileManager.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    /// Builds a `LaunchSpec` for opening `path` (optionally at 1-based `line`) with the given
    /// editor command string and executable resolver.
    static func launchSpec(
        path: String,
        line: Int?,
        editorEnv: String?,
        executableResolver: (String) -> String? = { resolveExecutable($0) }
    ) -> LaunchSpec? {
        guard let editorEnv else { return nil }
        let tokens = splitCommand(editorEnv)
        guard let rawCommand = tokens.first, !rawCommand.isEmpty else { return nil }
        guard let executable = executableResolver(rawCommand), !executable.isEmpty else {
            return nil
        }

        let baseArgs = Array(tokens.dropFirst())
        let editorName = (rawCommand as NSString).lastPathComponent.lowercased()
        let isTerminal = isTerminalEditor(name: editorName, baseArgs: baseArgs)
        let targetArgs = formattedTargetArguments(
            editorName: editorName,
            baseArgs: baseArgs,
            path: path,
            line: line
        )
        return LaunchSpec(
            executable: executable,
            arguments: baseArgs + targetArgs,
            isTerminalEditor: isTerminal
        )
    }

    /// Opens `path` (at `line` when supported) in the user's preferred GUI editor,
    /// falling back to `NSWorkspace.shared.open` if no GUI editor is resolved or launch fails.
    static func open(path: String, line: Int? = nil) {
        let editorEnv = preferredEditorCommand()
        _ = open(
            path: path,
            line: line,
            editorEnv: editorEnv,
            executableResolver: { resolveExecutable($0) },
            processLauncher: { spec in
                let process = Process()
                process.executableURL = URL(fileURLWithPath: spec.executable)
                process.arguments = spec.arguments
                process.standardInput = FileHandle.nullDevice
                process.standardOutput = FileHandle.nullDevice
                process.standardError = FileHandle.nullDevice
                try process.run()
            },
            workspaceOpener: { url in
                NSWorkspace.shared.open(url)
            }
        )
    }

    /// Testable core implementation of `open(path:line:)`.
    @discardableResult
    static func open(
        path: String,
        line: Int?,
        editorEnv: String?,
        executableResolver: (String) -> String?,
        processLauncher: (LaunchSpec) throws -> Void,
        workspaceOpener: (URL) -> Bool
    ) -> Bool {
        let expandedPath = (path as NSString).expandingTildeInPath
        if let spec = launchSpec(
            path: expandedPath,
            line: line,
            editorEnv: editorEnv,
            executableResolver: executableResolver
        ), !spec.isTerminalEditor {
            do {
                try processLauncher(spec)
                return true
            } catch {
                // Fall back to the macOS default handler if launching the editor binary fails.
            }
        }
        return workspaceOpener(URL(fileURLWithPath: expandedPath))
    }

    private static func isTerminalEditor(name: String, baseArgs: [String]) -> Bool {
        if terminalOnlyEditors.contains(name) {
            return true
        }
        if name == "emacsclient" {
            return baseArgs.contains { arg in
                arg == "-t" || arg == "-nw" || arg == "--tty"
            }
        }
        return false
    }

    private static func formattedTargetArguments(
        editorName: String,
        baseArgs: [String],
        path: String,
        line: Int?
    ) -> [String] {
        guard let line, line > 0 else {
            return [path]
        }
        if gotoLineEditors.contains(editorName) {
            if baseArgs.contains("--goto") || baseArgs.contains("-g") {
                return ["\(path):\(line)"]
            }
            return ["--goto", "\(path):\(line)"]
        }
        if colonLineEditors.contains(editorName) {
            return ["\(path):\(line)"]
        }
        if plusLineEditors.contains(editorName) {
            return ["+\(line)", path]
        }
        if dashLineFlagEditors.contains(editorName) {
            return ["--line", "\(line)", path]
        }
        return [path]
    }
}

/// Pure formatting and encoding helpers for inserting file references and worker actions
/// into Pcode's docked `prompt_toolkit` composer without clobbering draft text or
/// triggering accidental submit.
enum PCodeComposerInsertion {
    static let bracketedPasteStart = "\u{1b}[200~"
    static let bracketedPasteEnd = "\u{1b}[201~"

    /// Formats a prompt reference (such as `@src/auth.swift:L42-L58`) for inline insertion
    /// into the composer, optionally adding a leading space and always adding a trailing space
    /// so the user can keep typing immediately.
    static func formattedInsertion(reference: String, leadingSpace: Bool = true) -> String {
        let trimmed = reference.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        return leadingSpace ? " \(trimmed) " : "\(trimmed) "
    }

    /// Encodes `text` as UTF-8 wrapped in XTerm bracketed-paste sequences (`ESC[200~ ... ESC[201~`)
    /// so raw PTY writes into `prompt_toolkit` insert text safely (including newlines) without
    /// firing Enter/submit. Strips any embedded bracketed-paste control sequences to prevent breakout.
    static func bracketedPasteBytes(for text: String) -> Data {
        guard !text.isEmpty else { return Data() }
        let sanitized = text
            .replacingOccurrences(of: bracketedPasteEnd, with: "")
            .replacingOccurrences(of: bracketedPasteStart, with: "")
        return Data("\(bracketedPasteStart)\(sanitized)\(bracketedPasteEnd)".utf8)
    }

    /// Prefill text asking the agent to stop a running background worker.
    static func workerStopPrompt(id: String, role: String) -> String {
        "Stop \(workerLabel(id: id, role: role))."
    }

    /// Prefill text asking the agent to send a message to a running worker.
    static func workerMessagePrompt(id: String, role: String, message: String) -> String {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let label = workerLabel(id: id, role: role)
        if trimmed.isEmpty {
            return "Message \(label): "
        }
        return "Message \(label): \(trimmed)"
    }

    /// Prefill text asking the agent to answer a worker's pending `ask_parent` question.
    static func workerAnswerPrompt(id: String, role: String, answer: String) -> String {
        let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        let label = workerLabel(id: id, role: role)
        if trimmed.isEmpty {
            return "Answer \(label): "
        }
        return "Answer \(label): \(trimmed)"
    }

    private static func workerLabel(id: String, role: String) -> String {
        let cleanID = id.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanRole = role.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cleanID.isEmpty && !cleanRole.isEmpty && cleanRole.caseInsensitiveCompare(cleanID) != .orderedSame {
            return "worker \(cleanID) (\(cleanRole))"
        }
        if !cleanID.isEmpty {
            return "worker \(cleanID)"
        }
        if !cleanRole.isEmpty {
            return "worker (\(cleanRole))"
        }
        return "the worker"
    }
}
