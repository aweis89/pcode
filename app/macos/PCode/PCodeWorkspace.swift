import AppKit
import Combine
import GhosttyKit
import SwiftUI
import UniformTypeIdentifiers

/// SPIKE: pcode's own window, in place of Ghostty's terminal windows, tabs and splits.
///
/// A sidebar lists the sessions open here and the recent ones pcode saved;
/// picking one shows its pane: pcode's terminal (with its docked editor,
/// popups, drawer and toasts, as in a Ghostty window) and, toggled with ⌘J,
/// a shell along the bottom in the session's directory. Sessions are not
/// tabs or windows: every one lives in this window, and switching only swaps
/// which pane is shown, so a hidden session keeps running.
///
/// On by default (see `PCodeWorkspaceMode.enabled` to turn it off). pcode runs
/// as `$PCODE_COMMAND` (default `pcode`) through the user's login shell,
/// resumed with `-c <id>` in the session's own directory.
enum PCodeWorkspaceMode {
    /// On unless turned off: `PCODE_WORKSPACE=0`, or
    /// `defaults write com.pcode.terminal PCodeWorkspace -bool false` for
    /// Ghostty's own terminal windows, tabs and splits.
    static let enabled: Bool = {
        if let env = ProcessInfo.processInfo.environment["PCODE_WORKSPACE"], !env.isEmpty {
            return env != "0"
        }
        return UserDefaults.standard.object(forKey: "PCodeWorkspace") as? Bool ?? true
    }()

    static var command: String {
        let value = ProcessInfo.processInfo.environment["PCODE_COMMAND"] ?? ""
        return value.isEmpty ? "pcode" : value
    }

    /// The user's login shell: `$SHELL`, else their account's (a Dock or
    /// Finder launch may have no `SHELL`).
    static var loginShell: String {
        if let shell = ProcessInfo.processInfo.environment["SHELL"], !shell.isEmpty { return shell }
        if let entry = getpwuid(getuid()), let shell = entry.pointee.pw_shell {
            let path = String(cString: shell)
            if !path.isEmpty { return path }
        }
        return "/bin/zsh"
    }

    /// True when `path` exists as a non-directory executable file.
    private static func isExecutableRegularFile(
        _ path: String,
        fileManager: FileManager = .default
    ) -> Bool {
        var isDir: ObjCBool = false
        return fileManager.fileExists(atPath: path, isDirectory: &isDir)
            && !isDir.boolValue
            && fileManager.isExecutableFile(atPath: path)
    }

    /// Single-quote `text` for `fish`, where `\` and `'` are escaped inside `'...'`.
    private static func fishQuoted(_ text: String) -> String {
        let escaped = text
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "'", with: "\\'")
        return "'\(escaped)'"
    }

    /// True when `directory` or any ancestor contains a `.envrc` file.
    static func hasEnvrc(in directory: String, fileManager: FileManager = .default) -> Bool {
        guard !directory.isEmpty else { return false }
        var current = URL(fileURLWithPath: directory).standardizedFileURL
        while true {
            let envrc = current.appendingPathComponent(".envrc").path
            var isDir: ObjCBool = false
            if fileManager.fileExists(atPath: envrc, isDirectory: &isDir), !isDir.boolValue {
                return true
            }
            let parent = current.deletingLastPathComponent()
            if parent.path == current.path {
                return false
            }
            current = parent
        }
    }

    /// Locate an executable named `name` on a colon-separated `PATH`.
    static func findExecutable(
        _ name: String,
        inPath path: String,
        fileManager: FileManager = .default
    ) -> String? {
        for dir in path.split(separator: ":") where dir.hasPrefix("/") {
            let candidate = URL(fileURLWithPath: String(dir))
                .appendingPathComponent(name)
                .path
            if isExecutableRegularFile(candidate, fileManager: fileManager) {
                return candidate
            }
        }
        return nil
    }

    /// `pcode` with `args`, through a login shell: an app launched from the
    /// Dock gets launchd's bare PATH, where a Homebrew or `uv tool` pcode is
    /// not, so only the user's own shell setup finds it.
    ///
    /// When `useDirenv` is true, goes through `direnv exec <directory>` if
    /// `direnv` is available on the login shell's PATH: direnv's shell hook
    /// only fires at an interactive prompt, so a direct command would miss
    /// `.envrc` (or keep a stale `DIRENV_DIR`).
    static func shellCommand(
        _ args: [String],
        directory: String? = nil,
        useDirenv: Bool = false,
        shell: String = loginShell,
        command: String = command
    ) -> String {
        let isFish = URL(fileURLWithPath: shell).lastPathComponent == "fish"
        let quote: (String) -> String = isFish ? fishQuoted : PCode.shellQuoted
        let commandToken = command.hasPrefix("/") ? quote(command) : command
        let cmdParts = ([commandToken] + args.map(quote)).joined(separator: " ")
        let inner: String
        if useDirenv, let directory {
            let direnvCmd = "exec direnv exec \(quote(directory)) \(cmdParts)"
            let plainCmd = "exec \(cmdParts)"
            if isFish {
                inner = "if type -q direnv; \(direnvCmd); else; \(plainCmd); end"
            } else {
                inner = "if command -v direnv >/dev/null 2>&1; then \(direnvCmd); else \(plainCmd); fi"
            }
        } else {
            inner = "exec \(cmdParts)"
        }
        return "\(PCode.shellQuoted(shell)) -l -c \(PCode.shellQuoted(inner))"
    }

    /// Cached login-shell environment and resolved `pcode` executable so
    /// subsequent panes skip spawning `zsh -l` (~450ms per pane).
    struct CachedShellEnv {
        let commandPath: String
        let direnvPath: String?
        let env: [String: String]
    }

    private static let cacheLock = NSLock()
    private static var cachedEnv: CachedShellEnv?
    private static var cacheStarted = false

    private static let skipCachedEnvKeys: Set<String> = [
        "PWD", "OLDPWD", "SHLVL", "_", "TERM", "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION", "COLORTERM", "TERM_SESSION_ID",
        "TERMINFO", "XDG_DATA_DIRS", "MANPATH", "VTE_VERSION", "LANGUAGE",
        "PCODE_DOCK", "PCODE_NATIVE_TRANSCRIPT", "PCODE_HUSH_LOGIN",
    ]

    private static let envSentinel = "__PCODE_ENV_START__"

    private static func isSkippedEnvKey(_ key: String) -> Bool {
        skipCachedEnvKeys.contains(key)
            || key.hasPrefix("GHOSTTY_")
            || key.hasPrefix("DYLD_")
            || key.hasPrefix("__XCODE_")
    }

    /// Warm the login-shell cache once in the background. Safe to call repeatedly.
    static func warmLoginShellCache() {
        cacheLock.lock()
        if cacheStarted {
            cacheLock.unlock()
            return
        }
        cacheStarted = true
        cacheLock.unlock()

        let shell = loginShell
        let cmd = command
        let baseEnv = ProcessInfo.processInfo.environment.filter { !isSkippedEnvKey($0.key) }

        DispatchQueue.global(qos: .userInitiated).async {
            let probe = "p=$(command -v \(PCode.shellQuoted(cmd)) 2>/dev/null) || exit 1; printf '\\0%s\\0%s\\0' \(PCode.shellQuoted(envSentinel)) \"$p\"; /usr/bin/env -0"
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: shell)
            proc.arguments = ["-l", "-c", probe]
            proc.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
            proc.environment = baseEnv
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = FileHandle.nullDevice
            proc.standardInput = FileHandle.nullDevice

            let timeout = DispatchWorkItem {
                if proc.isRunning { proc.terminate() }
            }
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5.0, execute: timeout)
            defer { timeout.cancel() }

            do {
                try proc.run()
            } catch {
                return
            }

            let data = (try? out.fileHandleForReading.readToEnd()) ?? Data()
            proc.waitUntilExit()
            guard proc.terminationStatus == 0 else { return }

            let sentinelData = Data(envSentinel.utf8)
            let chunks = data.split(separator: 0, omittingEmptySubsequences: false)
            guard let sentinelIndex = chunks.firstIndex(where: { $0 == sentinelData }),
                  sentinelIndex + 1 < chunks.count,
                  let resolvedPath = String(data: chunks[sentinelIndex + 1], encoding: .utf8),
                  resolvedPath.hasPrefix("/"),
                  FileManager.default.isExecutableFile(atPath: resolvedPath) else { return }

            // Only record vars the login shell added or modified relative to
            // baseEnv so Ghostty's own env setup and stripped vars stay intact.
            var env: [String: String] = [:]
            for chunk in chunks[(sentinelIndex + 2)...] where !chunk.isEmpty {
                guard let entry = String(data: chunk, encoding: .utf8),
                      let eq = entry.firstIndex(of: "=") else { continue }
                let key = String(entry[..<eq])
                if isSkippedEnvKey(key) { continue }
                let value = String(entry[entry.index(after: eq)...])
                if baseEnv[key] != value {
                    env[key] = value
                }
            }

            // Preserve Ghostty's bundle binary dir on PATH since env_override
            // applies after Exec.zig appends exe_dir.
            if let path = env["PATH"],
               let exeDir = Bundle.main.executableURL?.deletingLastPathComponent().path,
               !path.split(separator: ":").contains(Substring(exeDir)) {
                env["PATH"] = "\(path):\(exeDir)"
            }

            let effectivePath = env["PATH"] ?? baseEnv["PATH"] ?? ""
            let direnvPath = findExecutable("direnv", inPath: effectivePath)

            cacheLock.lock()
            cachedEnv = CachedShellEnv(commandPath: resolvedPath, direnvPath: direnvPath, env: env)
            cacheLock.unlock()
        }
    }

    /// Direct command + cached login-shell env once ready, else the login-shell fallback.
    static func launchConfig(
        _ args: [String],
        directory: String
    ) -> (command: String, env: [String: String]) {
        warmLoginShellCache()
        cacheLock.lock()
        let cached = cachedEnv
        cacheLock.unlock()
        return buildLaunchConfig(
            args,
            directory: directory,
            cached: cached,
            baseEnvironment: ProcessInfo.processInfo.environment
        )
    }

    static func buildLaunchConfig(
        _ args: [String],
        directory: String,
        cached: CachedShellEnv?,
        baseEnvironment: [String: String],
        shell: String = loginShell,
        command: String = command,
        fileManager: FileManager = .default
    ) -> (command: String, env: [String: String]) {
        guard let cached,
              isExecutableRegularFile(cached.commandPath, fileManager: fileManager) else {
            let hasDirenvDir = !(baseEnvironment["DIRENV_DIR"] ?? "").isEmpty
            let shouldUseDirenv = hasDirenvDir || hasEnvrc(in: directory, fileManager: fileManager)
            return (
                shellCommand(
                    args,
                    directory: directory,
                    useDirenv: shouldUseDirenv,
                    shell: shell,
                    command: command
                ),
                [:]
            )
        }
        let hasDirenvDir = !(cached.env["DIRENV_DIR"] ?? baseEnvironment["DIRENV_DIR"] ?? "").isEmpty
        let shouldUseDirenv = hasDirenvDir || hasEnvrc(in: directory, fileManager: fileManager)
        // Pass through `env --` so `exec -l` doesn't prepend `-` to argv[0] of
        // a venv-backed Python entry point, and paths with `=` aren't parsed as
        // variable assignments.
        var parts = ["/usr/bin/env", "--"]
        if shouldUseDirenv,
           let direnvPath = cached.direnvPath,
           isExecutableRegularFile(direnvPath, fileManager: fileManager) {
            parts += [PCode.shellQuoted(direnvPath), "exec", PCode.shellQuoted(directory)]
        }
        parts += [PCode.shellQuoted(cached.commandPath)] + args.map(PCode.shellQuoted)
        return (parts.joined(separator: " "), cached.env)
    }
}

/// A saved session, read from pcode's `sessions/<id>/session.json`.
struct PCodeSessionInfo: Identifiable, Hashable, Decodable {
    let id: String
    let workspace: String
    let updated: String
    let name: String?
    let title: String?
    let turns: Int
    /// The main checkout, for a session in one of its worktrees; absent in old sessions.
    let project: String?
    /// Precomputed at decode time off the main thread.
    let normalizedProjectPath: String
    let worktreeName: String?
    let updatedDate: Date?

    private enum CodingKeys: String, CodingKey {
        case id, workspace, updated, name, title, turns, project
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        workspace = try c.decode(String.self, forKey: .workspace)
        updated = try c.decode(String.self, forKey: .updated)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        turns = try c.decode(Int.self, forKey: .turns)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        let rawProject = project.flatMap { $0.isEmpty ? nil : $0 } ?? workspace
        normalizedProjectPath = PCodeWorkspace.normalizePath(rawProject)
        let normalizedWorkspace = PCodeWorkspace.normalizePath(workspace)
        worktreeName = normalizedWorkspace != normalizedProjectPath
            ? (normalizedWorkspace as NSString).lastPathComponent
            : nil
        updatedDate = PCodeWorkspace.parseISO8601(updated)
    }

    /// What the sidebar groups by (main checkout for worktrees, else workspace).
    var projectPath: String { project.flatMap { $0.isEmpty ? nil : $0 } ?? workspace }

    /// Whether a pcode process holds this session now: pcode keeps an
    /// exclusive `flock` on its `.lock` while it is open (`sessions.is_open`).
    /// Reopening one of those would only continue a copy of it.
    static func isOpen(_ id: String) -> Bool {
        let path = root.appendingPathComponent(id).appendingPathComponent(".lock").path
        let fd = Darwin.open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }
        guard flock(fd, LOCK_SH | LOCK_NB) == 0 else { return errno == EWOULDBLOCK }
        flock(fd, LOCK_UN)
        return false
    }

    var label: String {
        if let name, !name.isEmpty { return name }
        if let title, !title.isEmpty { return title }
        return "Untitled session"
    }

    var directoryName: String { (workspace as NSString).lastPathComponent }

    /// Sessions a running background host holds (pcode's `hosts/<id>.json`
    /// entries, as `host_protocol.list_hosts` reads them).
    static func hosted() -> Set<String> {
        struct Entry: Decodable {
            let pid: Int32
            let session_id: String?
        }
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: hostRoot, includingPropertiesForKeys: nil) else { return [] }
        let decoder = JSONDecoder()
        var ids = Set<String>()
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let entry = try? decoder.decode(Entry.self, from: data),
                  let id = entry.session_id, !id.isEmpty, entry.pid > 0,
                  // Alive and still a host, not a reused pid.
                  kill(entry.pid, 0) == 0, PCodeProcess.isPCodeOrHost(entry.pid)
            else { continue }
            ids.insert(id)
        }
        return ids
    }

    /// Return the live Unix socket path (`hosts/<host_id>.sock`) for a running hosted session, if any.
    static func hostSocketPath(forSessionID sessionID: String) -> String? {
        guard !sessionID.isEmpty else { return nil }
        struct Entry: Decodable {
            let id: String
            let pid: Int32
            let session_id: String?
        }
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: hostRoot, includingPropertiesForKeys: nil) else {
            return nil
        }
        let uid = Darwin.getuid()
        let decoder = JSONDecoder()
        for file in files where file.pathExtension == "json" {
            var jsonStat = stat()
            guard Darwin.lstat(file.path, &jsonStat) == 0,
                  (jsonStat.st_mode & S_IFMT) == S_IFREG,
                  jsonStat.st_uid == uid,
                  let data = try? Data(contentsOf: file),
                  let entry = try? decoder.decode(Entry.self, from: data),
                  PCode.valid(entry.id, length: 1...64),
                  entry.session_id == sessionID,
                  entry.pid > 0,
                  kill(entry.pid, 0) == 0,
                  PCodeProcess.isPCodeOrHost(entry.pid)
            else { continue }
            let sockURL = hostRoot.appendingPathComponent("\(entry.id).sock")
            var sockStat = stat()
            if Darwin.lstat(sockURL.path, &sockStat) == 0,
               (sockStat.st_mode & S_IFMT) == S_IFSOCK,
               sockStat.st_uid == uid
            {
                return sockURL.path
            }
        }
        return nil
    }

    /// Of `ids`, sessions a Restart Pcode moved, those ready to reopen: held
    /// by a host, or unlocked on two checks half a second apart (not in the
    /// gap between the old pcode letting go and its host taking over).
    /// Waits up to `timeout` for the rest; one still locked then, with no
    /// host, is held by some other process and left out. Blocks: call it
    /// off the main thread.
    static func awaitRestarted(_ ids: [String], timeout: TimeInterval = 5) -> Set<String> {
        guard !ids.isEmpty else { return [] }
        let deadline = Date().addingTimeInterval(timeout)
        var ready = Set<String>()
        var unlocked = Set<String>()
        while true {
            let hosted = hosted()
            for id in ids where !ready.contains(id) {
                if hosted.contains(id) {
                    ready.insert(id)
                } else if isOpen(id) {
                    unlocked.remove(id)
                } else if !unlocked.insert(id).inserted {
                    ready.insert(id)
                }
            }
            if ready.count == Set(ids).count || Date() >= deadline { break }
            Thread.sleep(forTimeInterval: 0.5)
        }
        // Unlocked at the last check, with no time left for a second.
        return ready.union(unlocked)
    }

    static var hostRoot: URL {
        let env = ProcessInfo.processInfo.environment
        if let dir = env["PCODE_HOST_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath)
        }
        return stateRoot.appendingPathComponent("pcode/hosts")
    }

    private static var stateRoot: URL {
        let state = ProcessInfo.processInfo.environment["XDG_STATE_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".local/state")
        return URL(fileURLWithPath: state)
    }

    static var root: URL {
        let env = ProcessInfo.processInfo.environment
        if let dir = env["PCODE_SESSION_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath)
        }
        return stateRoot.appendingPathComponent("pcode/sessions")
    }

    /// Saved sessions with at least one turn, newest first.
    static func load(limit: Int = 200) -> [PCodeSessionInfo] {
        let fm = FileManager.default
        guard let dirs = try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return [] }
        let decoder = JSONDecoder()
        let all = dirs.compactMap { dir -> PCodeSessionInfo? in
            guard let data = try? Data(contentsOf: dir.appendingPathComponent("session.json")),
                  let info = try? decoder.decode(PCodeSessionInfo.self, from: data),
                  info.id == dir.lastPathComponent, info.turns > 0
            else { return nil }
            return info
        }
        return Array(all.sorted { $0.updated > $1.updated }.prefix(limit))
    }
}

/// One session shown in the workspace: pcode's terminal and its optional bottom shell.
final class PCodeSessionPane: ObservableObject, Identifiable {
    let id = UUID()
    let surface: Ghostty.SurfaceView
    let directory: String
    /// The project folder this pane is grouped under (main checkout for worktrees).
    let projectPath: String
    /// Whether this pane was opened to resume an existing saved session.
    let resuming: Bool
    /// The session pcode reports (`pcode;session;<id>`), once it has.
    @Published var sessionID: String?
    /// True once pcode has painted its ready UI (dock ready, or fallback timeout).
    @Published private(set) var isReady = false
    @Published private(set) var shell: Ghostty.SurfaceView?
    @Published var shellVisible = false
    /// The session's title, for the sidebar: its host's while one holds the session,
    /// else the terminal's (`PCodePaneStateResolver`). Copied out so the sidebar does
    /// not observe the whole surface, which publishes on every resize and focus change.
    @Published private(set) var title = ""
    /// The session host's entry while one holds this pane's session.
    @Published private(set) var hostStatus: PCodeHostStatus?
    private var resolver = PCodePaneStateResolver()
    static let surfaceKeyPressed = Notification.Name("PCodeSessionPaneSurfaceKeyPressed")

    private var titleSubscription: AnyCancellable?
    private var hostSubscription: AnyCancellable?
    private var keySubscription: AnyCancellable?
    private var dockSubscription: AnyCancellable?
    private var dockReadySubscription: AnyCancellable?
    private var progressSubscription: AnyCancellable?
    private var readyFallbackWork: DispatchWorkItem?
    private var readySafetyWork: DispatchWorkItem?

    /// What the session is doing, from its host while hosted and the progress
    /// pcode reports to its terminal (OSC 9;4): a turn running, a failed one
    /// not yet seen, or idle.
    enum Activity { case idle, running, failed }
    @Published private(set) var activity: Activity = .idle
    /// Called when pcode's docked editor comes or goes, so a hidden pane can size it.
    var onDockChange: (() -> Void)?
    /// Called when the pane's activity state transitions, so collapsed folder headers refresh.
    var onActivityChange: (() -> Void)?
    /// Called when a turn ends (running to idle or failed), so the workspace
    /// can mark its response unseen if nobody is looking at it.
    var onTurnFinished: (() -> Void)?
    /// A finished turn's response that hasn't been looked at here yet. Kept
    /// by session ID in the workspace, so it survives restarts.
    @Published var unseen = false

    init(surface: Ghostty.SurfaceView, directory: String, projectPath: String? = nil, sessionID: String?) {
        self.surface = surface
        surface.pcodeIsSessionPane = true
        self.directory = directory
        self.projectPath = PCodeWorkspace.normalizePath(projectPath ?? directory)
        self.resuming = sessionID != nil
        self.sessionID = sessionID
        titleSubscription = surface.$title.removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] title in
                guard let self else { return }
                self.resolver.oscTitle = title
                self.resolve()
            }
        hostSubscription = $sessionID
            .combineLatest(PCodeHostWatcher.shared.$statuses)
            .map { sessionID, statuses in sessionID.flatMap { statuses[$0] } }
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                guard let self else { return }
                self.hostStatus = status
                self.resolver.setHost(status)
                self.resolve()
            }
        progressSubscription = surface.$progressReport
            .map { report -> Activity in
                switch report?.state {
                case .set?, .indeterminate?, .pause?: return .running
                case .error?: return .failed
                default: return .idle
                }
            }
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] activity in
                guard let self else { return }
                self.resolver.setOSCActivity(activity)
                self.resolve()
            }
        keySubscription = NotificationCenter.default.publisher(for: Self.surfaceKeyPressed, object: surface)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                self.resolver.dismissFailure()
                self.resolve()
            }
        dockSubscription = surface.pcodeDock.$surface
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                // After the publish: the new surface is in place by then.
                DispatchQueue.main.async { self?.onDockChange?() }
            }
        dockReadySubscription = surface.pcodeDock.$ready
            .filter { $0 }
            .first()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.markReady() }

        // Safety escape hatch: if pcode errors out or never enables the dock,
        // drop the boot overlay so the terminal output is always visible.
        let safety = DispatchWorkItem { [weak self] in self?.markReady() }
        readySafetyWork = safety
        DispatchQueue.main.asyncAfter(deadline: .now() + 4.5, execute: safety)
    }

    deinit {
        readyFallbackWork?.cancel()
        readySafetyWork?.cancel()
    }

    /// Publish the title and activity the resolver settles on, and announce a finished turn.
    private func resolve() {
        let title = resolver.title
        if title != self.title { self.title = title }
        let activity = resolver.activity
        guard activity != self.activity else { return }
        let finished = self.activity == .running
        self.activity = activity
        if finished { onTurnFinished?() }
        onActivityChange?()
    }

    /// Dismiss the startup overlay once pcode is live.
    func markReady() {
        guard !isReady else { return }
        readyFallbackWork?.cancel()
        readyFallbackWork = nil
        readySafetyWork?.cancel()
        readySafetyWork = nil
        isReady = true
    }

    /// Called when pcode reports its session ID: if no dock opens shortly after
    /// (e.g. dock disabled or older pcode), dismiss the boot overlay anyway.
    func noteSessionReported() {
        guard !isReady, readyFallbackWork == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.isReady else { return }
            if self.surface.existingPCodeDock?.surface == nil {
                self.markReady()
            }
        }
        readyFallbackWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8, execute: work)
    }

    /// Every terminal this pane shows: pcode's, its docked editor's, and the shell.
    var surfaces: [Ghostty.SurfaceView] {
        [surface, surface.existingPCodeDock?.surface, shell].compactMap { $0 }
    }

    /// The surface keys should go to: pcode's docked editor once shown, else its terminal.
    var keyboardSurface: Ghostty.SurfaceView {
        if let dock = surface.existingPCodeDock, dock.ready, let docked = dock.surface { return docked }
        return surface
    }

    /// Worktree directory name when this pane runs in a worktree under `projectPath`.
    var worktreeName: String? {
        let dir = PCodeWorkspace.normalizePath(directory)
        return dir != projectPath ? (dir as NSString).lastPathComponent : nil
    }

    func setShell(_ view: Ghostty.SurfaceView?) { shell = view }
}

/// One folder group in the sidebar: open panes first, then recent saved sessions.
struct PCodeProjectGroup: Identifiable {
    let path: String
    let openPanes: [PCodeSessionPane]
    let recentSessions: [PCodeSessionInfo]

    var id: String { path }

    var name: String {
        if path == PCodeWorkspace.normalizePath(NSHomeDirectory()) { return "~" }
        let last = (path as NSString).lastPathComponent
        return last.isEmpty ? path : last
    }

    var abbreviatedPath: String { PCodeWorkspace.abbreviatePath(path) }

    /// Aggregate activity across open panes so collapsed folders still surface live state.
    var activity: PCodeSessionPane.Activity? {
        if openPanes.contains(where: { $0.activity == .running }) { return .running }
        if openPanes.contains(where: { $0.activity == .failed }) { return .failed }
        if !openPanes.isEmpty { return .idle }
        return nil
    }

    /// Whether any session in the folder, open or saved, has an unseen response.
    func hasUnseen(_ unseenIDs: Set<String>) -> Bool {
        openPanes.contains(where: \.unseen) || recentSessions.contains { unseenIDs.contains($0.id) }
    }
}

/// One candidate project/directory in the unified Command Center / launcher.
struct PCodeDirectoryCandidate: Identifiable, Hashable {
    let path: String
    let name: String
    let abbreviatedPath: String
    let openCount: Int
    let recentCount: Int
    let isCurrent: Bool
    let isPinned: Bool
    let isRawPath: Bool

    var id: String { isRawPath ? "raw:\(path)" : path }
}

final class PCodeWorkspace: ObservableObject {
    @Published private(set) var panes: [PCodeSessionPane] = []
    @Published var selected: UUID?
    @Published private(set) var history: [PCodeSessionInfo] = [] {
        didSet {
            historyByID = Dictionary(history.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        }
    }
    private var historyByID: [String: PCodeSessionInfo] = [:]

    let ghostty: Ghostty.App
    let peek = PCodePeekController()
    /// Whether the workspace window is on screen; set by its controller.
    var windowVisible: () -> Bool = { false }
    /// The workspace window, for confirmation sheets; set by its controller.
    weak var window: NSWindow?
    private var observers: [NSObjectProtocol] = []
    private static let openKey = "PCodeWorkspaceOpenSessions"
    private static let selectedKey = "PCodeWorkspaceSelectedSession"
    private static let unseenKey = "PCodeWorkspaceUnseenSessions"

    /// Sessions whose latest response hasn't been looked at in this app.
    @Published private(set) var unseenIDs: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: PCodeWorkspace.unseenKey) ?? []
    ) {
        didSet { if unseenIDs != oldValue { UserDefaults.standard.set(Array(unseenIDs), forKey: Self.unseenKey) } }
    }

    init(ghostty: Ghostty.App) {
        self.ghostty = ghostty
        PCodeWorkspaceMode.warmLoginShellCache()
        peek.onTargetChange = { [weak self] in
            guard let self else { return }
            self.syncOcclusion()
            if !self.peek.isShowing {
                self.scheduleSidebarHideIfPointerOutside()
            }
        }
        peek.windowProvider = { [weak self] in self?.window }
        peek.onRestoreFocus = { [weak self] in
            guard let self, !self.showingDirectoryPicker,
                  self.window?.isKeyWindow == true,
                  let pane = self.selectedPane
            else { return }
            DispatchQueue.main.async { Ghostty.moveFocus(to: pane.keyboardSurface) }
        }
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.menuTrackingDepth += 1
            self.cancelSidebarHide()
        })
        observers.append(center.addObserver(
            forName: NSMenu.didEndTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.menuTrackingDepth = max(0, self.menuTrackingDepth - 1)
            self.scheduleSidebarHideIfPointerOutside()
        })
        observers.append(center.addObserver(
            forName: NSWindow.didEndSheetNotification, object: nil, queue: .main
        ) { [weak self] note in
            guard let self, let sheetWindow = note.object as? NSWindow,
                  sheetWindow === self.window else { return }
            self.scheduleSidebarHideIfPointerOutside()
        })
        observers.append(center.addObserver(
            forName: Ghostty.Notification.ghosttyCloseSurface, object: nil, queue: .main
        ) { [weak self] note in
            guard let view = note.object as? Ghostty.SurfaceView else { return }
            let processAlive = note.userInfo?["process_alive"] as? Bool ?? false
            // Posted from inside the surface's own close callback, which is
            // still using it: free it (or ask about it) on the next turn.
            DispatchQueue.main.async { [weak self, weak view] in
                guard let self, let view else { return }
                self.surfaceClosed(view, processAlive: processAlive)
            }
        })
        observers.append(center.addObserver(
            forName: PCodeWorkspace.sessionChanged, object: nil, queue: .main
        ) { [weak self] note in
            guard let self, let view = note.object as? Ghostty.SurfaceView,
                  let pane = self.panes.first(where: { $0.surface === view }) else { return }
            let old = pane.sessionID
            pane.sessionID = view.pcodeSession
            pane.noteSessionReported()
            self.sessionIDChanged(pane, from: old)
            self.saveOpen()
            if pane.id == self.selected { self.saveSelected() }
            self.reloadHistory()
        })
    }

    deinit {
        stopSidebarMonitoring()
        observers.forEach(NotificationCenter.default.removeObserver)
    }

    private var appearanceObserver: NSKeyValueObservation?

    /// Follow macOS light/dark: each surface needs the scheme set on it, as
    /// Ghostty's own windows do for their surface trees, for a
    /// `theme = light:…,dark:…` config to switch it (and for pcode in it to
    /// hear the change, as a mode 2031 report).
    func followAppearance() {
        appearanceObserver = NSApplication.shared.observe(\.effectiveAppearance, options: [.new]) { [weak self] _, _ in
            DispatchQueue.main.async { self?.syncColorScheme() }
        }
    }

    func syncColorScheme() {
        for pane in panes { pane.surfaces.forEach(PCode.applyColorScheme) }
    }

    /// Posted by a surface whose `pcodeSession` changed.
    static let sessionChanged = Notification.Name("com.pcode.workspace.sessionChanged")

    var selectedPane: PCodeSessionPane? { panes.first { $0.id == selected } }

    /// Reopen the sessions open at last quit, and every session a background
    /// host holds; if none reopen, stays on the inline project launcher.
    func restore() {
        // Not into a window that is quitting to restart (a Dock click while
        // its panes close): the relaunched app reopens them. Nor twice.
        guard !restarting, !restoring else { return }
        restoring = true
        hasRestored = true
        let saved = UserDefaults.standard.stringArray(forKey: Self.openKey) ?? []
        let marked = Self.restartMarker()
        let selectedID = UserDefaults.standard.string(forKey: Self.selectedKey)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // Sessions a Restart Pcode just moved may still be on their way
            // into their hosts.
            let ready = PCodeSessionInfo.awaitRestarted(marked)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.restoring = false
                Self.clearRestartMarker()
                self.reloadHistory { [weak self] all in
                    guard let self, self.panes.isEmpty else { return }
                    self.reopen(saved: saved, marked: Set(marked), ready: ready, all: all, selectedID: selectedID)
                }
            }
        }
    }

    private var restoring = false
    /// True once initial startup restore has run (so later Dock clicks don't force-reopen closed hosts).
    private(set) var hasRestored = false

    private func reopen(
        saved: [String], marked: Set<String>, ready: Set<String>,
        all: [PCodeSessionInfo], selectedID: String?
    ) {
        let infos = Dictionary(all.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        // Any session a host holds, even one a terminal is attached to as
        // well: `pcode -c` attaches to the host, and the window shows them all.
        let hosted = PCodeSessionInfo.hosted()
        let extra = all.map(\.id).filter { hosted.contains($0) && !saved.contains($0) }
        for id in saved + extra {
            guard let info = infos[id] else { continue }
            // Not one open in another process (a terminal, another window)
            // without a host: that would start a copy of it here instead.
            // A restarted one may have been seen ready before its host
            // registered.
            let openable = hosted.contains(id) || (marked.contains(id) ? ready.contains(id) : !PCodeSessionInfo.isOpen(id))
            if openable { open(info, select: false) }
        }
        if !panes.isEmpty {
            select(panes.first { $0.sessionID != nil && $0.sessionID == selectedID } ?? panes[0])
        } else {
            // Nothing reopened: stay on the inline project launcher instead of spawning a blank session.
            launcherFocusTrigger += 1
        }
    }

    /// Reload the saved sessions off the main thread; only the newest load lands.
    func reloadHistory(then done: (([PCodeSessionInfo]) -> Void)? = nil) {
        historyGeneration += 1
        let generation = historyGeneration
        let unseen = unseenIDs
        DispatchQueue.global(qos: .userInitiated).async {
            let all = PCodeSessionInfo.load(limit: .max)
            // Sessions deleted outside the app (no directory left at all).
            let gone = unseen.filter {
                !FileManager.default.fileExists(atPath: PCodeSessionInfo.root.appendingPathComponent($0).path)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, generation == self.historyGeneration else { return }
                if !gone.isEmpty { self.unseenIDs.subtract(gone) }
                self.history = Array(all.prefix(200))
                done?(all)
            }
        }
    }

    private var historyGeneration = 0

    /// Folders explicitly added to the sidebar (may have no sessions yet).
    @Published var pinnedFolders: [String] = (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.pinnedKey) ?? [])
        .map(PCodeWorkspace.normalizePath) {
        didSet { UserDefaults.standard.set(pinnedFolders, forKey: Self.pinnedKey) }
    }
    /// Folders the user hid from the sidebar (never deletes sessions on disk).
    @Published var hiddenFolders: Set<String> = Set(
        (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.hiddenKey) ?? []).map(PCodeWorkspace.normalizePath)
    ) {
        didSet { UserDefaults.standard.set(Array(hiddenFolders), forKey: Self.hiddenKey) }
    }
    /// Collapsed project folders in the sidebar.
    @Published var collapsedFolders: Set<String> = Set(
        (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.collapsedKey) ?? []).map(PCodeWorkspace.normalizePath)
    ) {
        didSet { UserDefaults.standard.set(Array(collapsedFolders), forKey: Self.collapsedKey) }
    }
    /// Folders where the user clicked "Show more…" for recent sessions.
    @Published var expandedRecentFolders: Set<String> = []
    /// Whether the ⌘⇧N floating Command Center overlay is open (used when a session pane is active).
    @Published var showingDirectoryPicker = false
    /// Incremented to focus the inline project launcher when no session pane is open.
    @Published var launcherFocusTrigger = 0

    /// Saved sidebar width in points (clamped at layout time to the current window width).
    @Published var sidebarWidth: CGFloat = {
        let stored = UserDefaults.standard.double(forKey: PCodeWorkspace.sidebarWidthKey)
        return stored > 0
            ? PCodeSidebarLayout.clampedWidth(CGFloat(stored), totalWidth: 0)
            : PCodeSidebarLayout.defaultWidth
    }() {
        didSet { UserDefaults.standard.set(Double(sidebarWidth), forKey: Self.sidebarWidthKey) }
    }

    /// When true, the sidebar hides off the left edge and slides the layout over on hover.
    @Published var sidebarAutoHide: Bool = UserDefaults.standard.bool(forKey: PCodeWorkspace.sidebarAutoHideKey) {
        didSet {
            UserDefaults.standard.set(sidebarAutoHide, forKey: Self.sidebarAutoHideKey)
            if !sidebarAutoHide {
                cancelSidebarReveal()
                cancelSidebarHide()
                sidebarRevealed = false
            }
            syncSidebarMouseMonitor()
        }
    }

    /// Whether the auto-hiding sidebar is currently slid open.
    @Published var sidebarRevealed = false

    /// True while the user is dragging the sidebar's right resize handle.
    var isResizingSidebar = false {
        didSet {
            if isResizingSidebar { cancelSidebarHide() }
        }
    }

    /// True while a Finder folder drag is hovering over the sidebar.
    var isSidebarDropTargeted = false {
        didSet {
            if isSidebarDropTargeted {
                cancelSidebarHide()
            } else {
                scheduleSidebarHideIfPointerOutside()
            }
        }
    }

    private var sidebarMonitoringActive = false
    private var sidebarMouseMonitor: Any?
    private var sidebarRevealWork: DispatchWorkItem?
    private var sidebarHideWork: DispatchWorkItem?
    private var menuTrackingDepth = 0
    private static let sidebarRevealDelay: TimeInterval = 0.05
    private static let sidebarHideDelay: TimeInterval = 0.22

    private static let pinnedKey = "PCodeWorkspacePinnedFolders"
    private static let hiddenKey = "PCodeWorkspaceHiddenFolders"
    private static let collapsedKey = "PCodeWorkspaceCollapsedFolders"
    private static let sidebarWidthKey = "PCodeWorkspaceSidebarWidth"
    private static let sidebarAutoHideKey = "PCodeWorkspaceSidebarAutoHide"

    func effectiveSidebarWidth(in totalWidth: CGFloat) -> CGFloat {
        PCodeSidebarLayout.clampedWidth(sidebarWidth, totalWidth: totalWidth)
    }

    func resetSidebarWidth() {
        sidebarWidth = PCodeSidebarLayout.defaultWidth
    }

    func toggleSidebarAutoHide() {
        cancelSidebarReveal()
        cancelSidebarHide()
        isResizingSidebar = false
        if sidebarAutoHide {
            sidebarAutoHide = false
        } else {
            sidebarRevealed = false
            sidebarAutoHide = true
            restoreFocusAfterSidebarHide()
        }
    }

    func startSidebarMonitoring() {
        sidebarMonitoringActive = true
        syncSidebarMouseMonitor()
    }

    func stopSidebarMonitoring() {
        sidebarMonitoringActive = false
        cancelSidebarReveal()
        cancelSidebarHide()
        if let sidebarMouseMonitor {
            NSEvent.removeMonitor(sidebarMouseMonitor)
            self.sidebarMouseMonitor = nil
        }
    }

    private func syncSidebarMouseMonitor() {
        if sidebarMonitoringActive && sidebarAutoHide {
            guard sidebarMouseMonitor == nil else { return }
            sidebarMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged]) { [weak self] event in
                self?.handleSidebarMouseMoved(event)
                return event
            }
        } else if let sidebarMouseMonitor {
            NSEvent.removeMonitor(sidebarMouseMonitor)
            self.sidebarMouseMonitor = nil
        }
    }

    private func handleSidebarMouseMoved(_ event: NSEvent) {
        guard sidebarAutoHide, let window, event.window === window,
              window.isKeyWindow, window.attachedSheet == nil,
              !showingDirectoryPicker
        else { return }
        let bounds = window.contentLayoutRect
        let point = event.locationInWindow
        if !sidebarRevealed {
            if event.type == .mouseMoved && NSEvent.pressedMouseButtons == 0
                && PCodeSidebarLayout.shouldReveal(at: point, in: bounds) {
                scheduleSidebarReveal()
            } else {
                cancelSidebarReveal()
            }
        } else {
            cancelSidebarReveal()
            let width = effectiveSidebarWidth(in: bounds.width)
            if PCodeSidebarLayout.isInsideRevealedZone(at: point, in: bounds, sidebarWidth: width) {
                cancelSidebarHide()
            } else {
                scheduleSidebarHide()
            }
        }
    }

    func sidebarHoverChanged(_ hovering: Bool) {
        guard sidebarAutoHide else { return }
        if hovering {
            cancelSidebarHide()
        } else {
            scheduleSidebarHideIfPointerOutside()
        }
    }

    private var canAutoHideSidebar: Bool {
        sidebarAutoHide && sidebarRevealed
            && !isResizingSidebar
            && !isSidebarDropTargeted
            && menuTrackingDepth == 0
            && !peek.isShowing
            && window?.attachedSheet == nil
    }

    private func isPointerInsideRevealedSidebar() -> Bool {
        guard let window else { return false }
        let bounds = window.contentLayoutRect
        let width = effectiveSidebarWidth(in: bounds.width)
        let point = window.mouseLocationOutsideOfEventStream
        return PCodeSidebarLayout.isInsideRevealedZone(at: point, in: bounds, sidebarWidth: width)
    }

    private func cancelSidebarReveal() {
        sidebarRevealWork?.cancel()
        sidebarRevealWork = nil
    }

    private func scheduleSidebarReveal() {
        guard sidebarRevealWork == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.sidebarRevealWork = nil
            guard self.sidebarAutoHide, !self.sidebarRevealed,
                  let window = self.window, window.isKeyWindow,
                  window.attachedSheet == nil, !self.showingDirectoryPicker,
                  NSEvent.pressedMouseButtons == 0
            else { return }
            let point = window.mouseLocationOutsideOfEventStream
            if PCodeSidebarLayout.shouldReveal(at: point, in: window.contentLayoutRect) {
                self.cancelSidebarHide()
                self.sidebarRevealed = true
            }
        }
        sidebarRevealWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.sidebarRevealDelay, execute: work)
    }

    private func cancelSidebarHide() {
        sidebarHideWork?.cancel()
        sidebarHideWork = nil
    }

    func scheduleSidebarHide() {
        guard canAutoHideSidebar else { return }
        cancelSidebarHide()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.canAutoHideSidebar, !self.isPointerInsideRevealedSidebar() else { return }
            self.hideRevealedSidebar()
        }
        sidebarHideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.sidebarHideDelay, execute: work)
    }

    func scheduleSidebarHideIfPointerOutside() {
        guard canAutoHideSidebar else { return }
        if !isPointerInsideRevealedSidebar() {
            scheduleSidebarHide()
        }
    }

    private func hideRevealedSidebar() {
        guard sidebarRevealed else { return }
        cancelSidebarHide()
        sidebarRevealed = false
        restoreFocusAfterSidebarHide()
    }

    private func restoreFocusAfterSidebarHide() {
        guard let window, window.isKeyWindow,
              window.attachedSheet == nil, !showingDirectoryPicker,
              let pane = selectedPane
        else { return }
        let target = pane.keyboardSurface
        if let responder = window.firstResponder as? NSView,
           pane.surfaces.contains(where: { responder === $0 || responder.isDescendant(of: $0) }) {
            return
        }
        DispatchQueue.main.async { Ghostty.moveFocus(to: target) }
    }

    func sidebarWindowDidResignKey() {
        cancelSidebarReveal()
        guard canAutoHideSidebar else { return }
        cancelSidebarHide()
        sidebarRevealed = false
    }

    func sidebarWindowWillClose() {
        cancelSidebarReveal()
        cancelSidebarHide()
        isResizingSidebar = false
        isSidebarDropTargeted = false
        sidebarRevealed = false
    }

    /// Canonical project path for an open pane (uses `session.json`'s `project` once loaded).
    func projectPath(for pane: PCodeSessionPane) -> String {
        info(for: pane)?.normalizedProjectPath ?? pane.projectPath
    }

    /// Sidebar groups: pinned folders, open-pane folders, and recent-session folders (excluding hidden).
    var projectGroups: [PCodeProjectGroup] {
        let openIDs = Set(panes.compactMap(\.sessionID))
        var order: [String] = []
        var seen: Set<String> = []
        func register(_ path: String) {
            if seen.insert(path).inserted { order.append(path) }
        }

        var openByProject: [String: [PCodeSessionPane]] = [:]
        for pane in panes {
            let project = projectPath(for: pane)
            register(project)
            openByProject[project, default: []].append(pane)
        }
        for path in pinnedFolders where !hiddenFolders.contains(path) {
            register(path)
        }
        var recentByProject: [String: [PCodeSessionInfo]] = [:]
        for info in history where !openIDs.contains(info.id) {
            let project = info.normalizedProjectPath
            guard !hiddenFolders.contains(project) || openByProject[project] != nil else { continue }
            register(project)
            recentByProject[project, default: []].append(info)
        }
        return order.map { path in
            PCodeProjectGroup(
                path: path,
                openPanes: openByProject[path] ?? [],
                recentSessions: recentByProject[path] ?? [])
        }
    }

    /// Projects offered by the Command Center: current project first, then all known projects.
    var pickerCandidates: [PCodeDirectoryCandidate] {
        let currentProject = selectedPane.map { projectPath(for: $0) }
        let pinnedSet = Set(pinnedFolders).subtracting(hiddenFolders)
        let groups = projectGroups
        var seen: Set<String> = []
        var result: [PCodeDirectoryCandidate] = []

        func append(_ group: PCodeProjectGroup, isCurrent: Bool) {
            guard seen.insert(group.path).inserted else { return }
            result.append(PCodeDirectoryCandidate(
                path: group.path,
                name: group.name,
                abbreviatedPath: group.abbreviatedPath,
                openCount: group.openPanes.count,
                recentCount: group.recentSessions.count,
                isCurrent: isCurrent,
                isPinned: pinnedSet.contains(group.path),
                isRawPath: false))
        }

        if let currentProject, let currentGroup = groups.first(where: { $0.path == currentProject }) {
            append(currentGroup, isCurrent: true)
        }
        for group in groups {
            append(group, isCurrent: group.path == currentProject)
        }
        // Also offer projects hidden from the sidebar so ⌘⇧N can still find and reopen them.
        var hiddenCounts: [String: Int] = [:]
        var hiddenOrder: [String] = []
        for info in history {
            let path = info.normalizedProjectPath
            guard !seen.contains(path) else { continue }
            if hiddenCounts[path] == nil { hiddenOrder.append(path) }
            hiddenCounts[path, default: 0] += 1
        }
        for path in hiddenOrder where seen.insert(path).inserted {
            let last = (path as NSString).lastPathComponent
            result.append(PCodeDirectoryCandidate(
                path: path,
                name: path == Self.normalizePath(NSHomeDirectory()) ? "~" : (last.isEmpty ? path : last),
                abbreviatedPath: Self.abbreviatePath(path),
                openCount: 0,
                recentCount: hiddenCounts[path] ?? 0,
                isCurrent: currentProject == path,
                isPinned: false,
                isRawPath: false))
        }
        let home = Self.normalizePath(NSHomeDirectory())
        if seen.insert(home).inserted {
            result.append(PCodeDirectoryCandidate(
                path: home,
                name: "~",
                abbreviatedPath: home,
                openCount: 0,
                recentCount: 0,
                isCurrent: currentProject == home,
                isPinned: pinnedSet.contains(home),
                isRawPath: false))
        }
        return result
    }

    func info(for pane: PCodeSessionPane) -> PCodeSessionInfo? {
        guard let id = pane.sessionID else { return nil }
        return historyByID[id]
    }

    /// ⌘N: new session in the current session's project, or open the picker if none is open.
    func newSession(directory: String? = nil) {
        let dir: String
        if let directory {
            dir = Self.normalizePath(directory)
        } else if let pane = selectedPane {
            dir = projectPath(for: pane)
        } else {
            openDirectoryPicker()
            return
        }
        hiddenFolders.remove(dir)
        collapsedFolders.remove(dir)
        showingDirectoryPicker = false
        guard let pane = makePane(args: [], directory: dir, projectPath: dir, sessionID: nil) else { return }
        adopt(pane)
        panes.insert(pane, at: 0)
        select(pane)
    }

    func openDirectoryPicker() {
        peek.dismiss(restoreFocus: false)
        if selectedPane == nil {
            launcherFocusTrigger += 1
            return
        }
        showingDirectoryPicker = true
    }

    func peekTarget(for info: PCodeSessionInfo) -> PCodePeekTarget {
        PCodePeekTarget(
            sessionID: info.id,
            title: info.label,
            subtitle: info.worktreeName ?? Self.relativeTime(info.updatedDate),
            directory: info.workspace,
            turnsHint: info.turns,
            updatedDate: info.updatedDate,
            isOpen: false,
            paneID: nil,
            info: info)
    }

    /// Whether the workspace window is currently in native fullscreen (no traffic lights in the title bar).
    @Published var isFullScreen = false
    /// Height of the window's titlebar region in points (matches AppKit's traffic-light bar).
    @Published var titlebarHeight: CGFloat = 28
    /// Leading clearance needed in windowed mode to clear the traffic-light buttons.
    @Published var trafficLightClearance: CGFloat = 78

    /// Collapse runs of whitespace and newlines to single spaces, matching `terminal_notify.title`.
    private static func normalizeTitle(_ text: String) -> String {
        text.components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    /// Resolve the best display title for an open pane, preferring live terminal
    /// titles (e.g. after `/rename` or auto-titling) while falling back to the
    /// saved `session.json` label when the terminal still has Ghostty's placeholder
    /// or an OSC 0 120-char truncation prefix of a longer saved label.
    static func displayTitle(paneTitle: String, savedLabel: String?) -> String {
        let normalizedLive = normalizeTitle(paneTitle)
        let isPlaceholder = normalizedLive.isEmpty || normalizedLive == "👻"
        let normalizedSaved = savedLabel.map(normalizeTitle)
        let validSaved = (normalizedSaved?.isEmpty == false) ? normalizedSaved : nil

        if !isPlaceholder {
            // `TabTitle` truncates OSC 0 to 120 Python chars (Unicode scalars); keep the
            // full saved label only when the live title actually hit that 120-char boundary,
            // so `/rename` to a shorter prefix still takes effect immediately.
            if let validSaved,
               validSaved.unicodeScalars.count > 120,
               normalizedLive.unicodeScalars.count >= 120,
               validSaved.hasPrefix(normalizedLive) {
                return validSaved
            }
            return normalizedLive
        }
        return validSaved ?? "New session"
    }

    func displayTitle(for pane: PCodeSessionPane) -> String {
        Self.displayTitle(paneTitle: pane.title, savedLabel: info(for: pane)?.label)
    }

    func projectName(for pane: PCodeSessionPane) -> String {
        let path = projectPath(for: pane)
        if path == Self.normalizePath(NSHomeDirectory()) { return "~" }
        let last = (path as NSString).lastPathComponent
        return last.isEmpty ? path : last
    }

    func peekTarget(for pane: PCodeSessionPane) -> PCodePeekTarget {
        let saved = info(for: pane)
        let title = Self.displayTitle(paneTitle: pane.title, savedLabel: saved?.label)
        return PCodePeekTarget(
            sessionID: pane.sessionID,
            title: title,
            subtitle: saved?.worktreeName ?? pane.worktreeName,
            directory: pane.directory,
            turnsHint: saved?.turns,
            updatedDate: saved?.updatedDate,
            isOpen: true,
            paneID: pane.id,
            info: saved)
    }

    func openPeekTarget(_ target: PCodePeekTarget) {
        peek.dismiss(restoreFocus: false)
        if let paneID = target.paneID, let pane = panes.first(where: { $0.id == paneID }) {
            select(pane)
        } else if let info = target.info ?? target.sessionID.flatMap({ historyByID[$0] }) {
            open(info)
        }
    }

    func dismissDirectoryPicker() {
        showingDirectoryPicker = false
        if let pane = selectedPane {
            DispatchQueue.main.async { Ghostty.moveFocus(to: pane.keyboardSurface) }
        }
    }

    func toggleCollapsed(_ path: String) {
        peek.clearHoverAndUnpinned()
        let normalized = Self.normalizePath(path)
        if collapsedFolders.contains(normalized) {
            collapsedFolders.remove(normalized)
        } else {
            collapsedFolders.insert(normalized)
        }
    }

    @discardableResult
    func addFolder(_ path: String) -> Bool {
        let normalized = Self.normalizePath(path)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: normalized, isDirectory: &isDir), isDir.boolValue else { return false }
        hiddenFolders.remove(normalized)
        collapsedFolders.remove(normalized)
        if !pinnedFolders.contains(normalized) {
            pinnedFolders.insert(normalized, at: 0)
        }
        return true
    }

    func removeFolder(_ path: String) {
        peek.clearHoverAndUnpinned()
        let normalized = Self.normalizePath(path)
        pinnedFolders.removeAll { $0 == normalized }
        hiddenFolders.insert(normalized)
    }

    /// Present a native macOS folder chooser (`NSOpenPanel`).
    func browseForFolder(startSession: Bool, onCancel: (() -> Void)? = nil) {
        if let window, window.attachedSheet != nil { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = startSession ? "New Session" : "Add Project"
        panel.message = startSession
            ? "Choose a project folder to start a new pcode session in."
            : "Choose a project folder to add to the sidebar."
        if let current = selectedPane?.directory {
            panel.directoryURL = URL(fileURLWithPath: current)
        }
        let handle: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            guard let self else { return }
            guard response == .OK, let url = panel.url else {
                DispatchQueue.main.async { onCancel?() }
                return
            }
            let path = url.standardizedFileURL.path
            self.addFolder(path)
            if startSession {
                // Next turn so AppKit finishes ordering out the sheet before moving focus to the terminal.
                DispatchQueue.main.async {
                    self.newSession(directory: path)
                }
            }
        }
        if let window {
            panel.beginSheetModal(for: window, completionHandler: handle)
        } else {
            handle(panel.runModal())
        }
    }

    func open(_ info: PCodeSessionInfo, select shouldSelect: Bool = true) {
        if shouldSelect {
            peek.dismiss(restoreFocus: false)
            showingDirectoryPicker = false
        }
        if let pane = panes.first(where: { $0.sessionID == info.id }) {
            if shouldSelect { select(pane) }
            return
        }
        guard PCode.valid(info.id, length: 1...64) else { return }
        let project = info.normalizedProjectPath
        hiddenFolders.remove(project)
        collapsedFolders.remove(project)
        guard let pane = makePane(
            args: ["-c", info.id],
            directory: info.workspace,
            projectPath: project,
            sessionID: info.id
        ) else { return }
        adopt(pane)
        panes.append(pane)
        schedulePresize()
        saveOpen()
        if shouldSelect { select(pane) }
    }

    // MARK: Path & time helpers

    static func normalizePath(_ path: String) -> String {
        let expanded = (path as NSString).expandingTildeInPath
        let standardized = (expanded as NSString).standardizingPath
        if standardized.count > 1, standardized.hasSuffix("/") {
            return String(standardized.dropLast())
        }
        return standardized
    }

    static func abbreviatePath(_ path: String) -> String {
        let home = normalizePath(NSHomeDirectory())
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~" + path.dropFirst(home.count)
        }
        return path
    }

    private static let isoWithFractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let isoBasic: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func parseISO8601(_ iso: String) -> Date? {
        isoWithFractional.date(from: iso) ?? isoBasic.date(from: iso)
    }

    static func relativeTime(_ date: Date?) -> String? {
        guard let date else { return nil }
        let seconds = max(0, Int(Date().timeIntervalSince(date)))
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3600)h" }
        if seconds < 86_400 * 14 { return "\(seconds / 86_400)d" }
        return "\(seconds / (86_400 * 7))w"
    }

    // MARK: Sidebar actions

    static func reveal(_ directory: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: directory)])
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func confirmDelete(info: PCodeSessionInfo) {
        confirm("Delete “\(info.label)”?") { [weak self] in self?.delete(id: info.id) }
    }

    func confirmDelete(pane: PCodeSessionPane) {
        guard let id = pane.sessionID else { return }
        let title = displayTitle(for: pane)
        confirm("Delete “\(title)”?", detail: "It is closed first, ending pcode in it.") { [weak self, weak pane] in
            guard let self, let pane else { return }
            self.close(pane)
            // pcode lets go of the session a moment after its terminal closes.
            self.delete(id: id, retries: 10)
        }
    }

    private func confirm(_ message: String, detail: String? = nil, then action: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = [detail, "Its transcript and history are removed. This can't be undone."]
            .compactMap { $0 }.joined(separator: " ")
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        guard let window else { return }
        alert.beginSheetModal(for: window) { if $0 == .alertFirstButtonReturn { action() } }
    }

    /// `pcode --delete-session <id>`, which refuses a session still open in
    /// a process: retried while one is letting go of it.
    private func delete(id: String, retries: Int = 0) {
        guard PCode.valid(id, length: 1...64) else { return }
        let command = "\(PCodeWorkspaceMode.command) --delete-session \(PCode.shellQuoted(id))"
        let shell = PCodeWorkspaceMode.loginShell
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: shell)
            // A login shell, as the session terminals get: `pcode` is on its PATH.
            process.arguments = ["-l", "-c", command]
            let errors = Pipe()
            process.standardError = errors
            process.standardOutput = FileHandle.nullDevice
            var status: Int32 = -1
            if (try? process.run()) != nil {
                process.waitUntilExit()
                status = process.terminationStatus
            }
            let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self else { return }
                if status != 0, retries > 0, message.contains("open in another process") {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.delete(id: id, retries: retries - 1) }
                    return
                }
                if status == 0 {
                    self.unseenIDs.remove(id)
                    self.peek.clearHover(sessionID: id)
                }
                self.reloadHistory()
                guard status != 0 else { return }
                let alert = NSAlert()
                alert.messageText = "Couldn't delete the session"
                alert.informativeText = message.isEmpty ? "pcode exited with status \(status)." : message
                if let window = self.window { alert.beginSheetModal(for: window) } else { alert.runModal() }
            }
        }
    }

    /// Tell each terminal's renderer whether it is on screen, as Ghostty's own
    /// windows do: a hidden session must not keep drawing frames that
    /// compete with the one being watched.
    func syncOcclusion() {
        let windowVisible = windowVisible()
        let peekedPaneID = peek.target?.paneID
        for pane in panes {
            let visible = windowVisible && (pane.id == selected || pane.id == peekedPaneID)
            for view in pane.surfaces {
                // Always sent: a new surface's flag starts false while its
                // renderer starts visible, so comparing would skip hiding it.
                guard let surface = view.surface else { continue }
                ghostty_surface_set_occlusion(surface, visible)
                view.isWindowVisible = visible
            }
        }
    }

    // MARK: Hidden panes' sizes

    /// The session area's size, measured by the view; hidden panes are sized to it.
    var detailSize: CGSize = .zero {
        didSet { if detailSize != oldValue { schedulePresize() } }
    }

    private var presizeWork: DispatchWorkItem?

    /// Presize once things settle: not on every frame of a live window resize
    /// (each one reflows every hidden terminal and signals every hidden pcode),
    /// and after a newly shown pane has been laid out, so it can be measured.
    func schedulePresize() {
        presizeWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.presizeHidden() }
        presizeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
    }

    /// Give every hidden pane's terminals the size they will have when shown.
    ///
    /// A hidden pane is in no window, so nothing lays its terminals out: they
    /// keep the size they were made with (a few dozen columns), pcode in them
    /// draws to that, and showing one reflows everything at once. Sized to
    /// match the shown pane, showing it changes nothing.
    func presizeHidden() {
        guard let window, detailSize.width > 0, detailSize.height > 0 else { return }
        let scale = window.backingScaleFactor
        let shown = selectedPane
        // The shown terminals' own pixel sizes, as their layout gave them
        // (scrollers and padding included), else estimates from the area.
        let transcript = Self.pixels(shown?.surface)
            ?? (UInt32(detailSize.width * scale), UInt32(detailSize.height * scale))
        let shownDock = shown?.surface.existingPCodeDrawer?.isOpen == true
            ? nil
            : Self.pixels(shown?.surface.existingPCodeDock?.surface)
        for pane in panes where pane.id != selected {
            Self.setSize(pane.surface, transcript, scale: scale)
            if let docked = pane.surface.existingPCodeDock?.surface {
                let cell = docked.cellSize.height > 0 ? docked.cellSize.height : 18
                let heightPx = UInt32(max(1, PCodeDock.cardHeight(rows: PCodeDock.gridRows, cell: cell) * scale))
                let dock: (UInt32, UInt32)
                if let drawer = pane.surface.existingPCodeDrawer, drawer.isOpen {
                    let drawerWidth = PCodeDrawer.width(
                        in: detailSize.width,
                        fraction: drawer.fraction,
                        minWidth: PCodeDrawer.dockedMinWidth) - 1
                    dock = (UInt32(max(1, (drawerWidth - 16) * scale)), heightPx)
                } else {
                    dock = shownDock ?? (UInt32(max(1, (detailSize.width - 16) * scale)), heightPx)
                }
                Self.setSize(docked, dock, scale: scale)
            }
        }
    }

    private static func pixels(_ view: Ghostty.SurfaceView?) -> (UInt32, UInt32)? {
        guard let view, view.window != nil, let surface = view.surface else { return nil }
        let size = ghostty_surface_size(surface)
        guard size.width_px > 0, size.height_px > 0 else { return nil }
        return (size.width_px, size.height_px)
    }

    private static func setSize(_ view: Ghostty.SurfaceView, _ pixels: (UInt32, UInt32), scale: CGFloat) {
        guard let surface = view.surface, view.window == nil else { return }
        let current = ghostty_surface_size(surface)
        guard current.width_px != pixels.0 || current.height_px != pixels.1 else { return }
        ghostty_surface_set_content_scale(surface, scale, scale)
        ghostty_surface_set_size(surface, pixels.0, pixels.1)
    }

    private func adopt(_ pane: PCodeSessionPane) {
        if let id = pane.sessionID, unseenIDs.contains(id) { pane.unseen = true }
        pane.onTurnFinished = { [weak self, weak pane] in
            guard let self, let pane else { return }
            if self.isViewed(pane) { self.markSeen(pane) } else { self.markUnseen(pane) }
        }
        pane.onDockChange = { [weak self] in self?.schedulePresize() }
        pane.onActivityChange = { [weak self, weak pane] in
            guard let self, let pane else { return }
            if self.collapsedFolders.contains(self.projectPath(for: pane)) {
                self.objectWillChange.send()
            }
        }
    }

    // MARK: Unseen responses

    /// Whether the user is looking at `pane`: it is the one shown, in the key
    /// window (no window is key while the app is in the background), and that
    /// window is on screen (a locked screen or asleep display leaves it key).
    private func isViewed(_ pane: PCodeSessionPane) -> Bool {
        pane.id == selected && (window?.isKeyWindow ?? false) && windowVisible()
    }

    private func markUnseen(_ pane: PCodeSessionPane) {
        if !pane.unseen {
            pane.unseen = true
            objectWillChange.send()  // collapsed folder headers
        }
        if let id = pane.sessionID, !unseenIDs.contains(id) { unseenIDs.insert(id) }
    }

    private func markSeen(_ pane: PCodeSessionPane) {
        if pane.unseen {
            pane.unseen = false
            objectWillChange.send()
        }
        if let id = pane.sessionID, unseenIDs.contains(id) { unseenIDs.remove(id) }
    }

    /// pcode switched the pane to another session (`/resume`, `/new`, or its
    /// first report): carry an unseen response over to the new ID.
    private func sessionIDChanged(_ pane: PCodeSessionPane, from old: String?) {
        guard old != pane.sessionID else { return }
        // A response that ended before the new ID arrived belongs to it, not the old one.
        let carried = pane.unseen && (old.map { unseenIDs.contains($0) } ?? true)
        if let old, carried { unseenIDs.remove(old) }
        if isViewed(pane) {
            markSeen(pane)
        } else if carried || pane.sessionID.map(unseenIDs.contains) == true {
            markUnseen(pane)
        } else if pane.unseen {
            pane.unseen = false
            objectWillChange.send()
        }
    }

    /// The window became key or came on screen: the session it shows has now been seen.
    func selectedMaybeSeen() {
        if let pane = selectedPane, isViewed(pane) { markSeen(pane) }
    }

    func select(_ pane: PCodeSessionPane) {
        peek.dismiss(restoreFocus: false)
        selected = pane.id
        saveSelected()
        if isViewed(pane) { markSeen(pane) }
        syncOcclusion()
        // The pane just hidden keeps its size; the others follow the shown one.
        schedulePresize()
        // After SwiftUI has put the pane's views in the window.
        DispatchQueue.main.async { Ghostty.moveFocus(to: pane.keyboardSurface) }
    }

    /// Ask to close `pane`: pcode's terminal reports back through the close
    /// notification, which confirms first if pcode is still running.
    func requestClose(_ pane: PCodeSessionPane) {
        guard let surface = pane.surface.surface else { return close(pane) }
        ghostty.requestClose(surface: surface)
    }

    /// Remove `pane` and end its terminals. Not from inside one of their callbacks.
    func close(_ pane: PCodeSessionPane) {
        peek.clearHover(paneID: pane.id)
        guard let index = panes.firstIndex(where: { $0 === pane }) else { return }
        panes.remove(at: index)
        if let shell = pane.shell { PCode.retire(shell) }
        PCode.retire(pane.surface)
        saveOpen()
        reloadHistory()
        if selected == pane.id {
            if panes.isEmpty {
                selected = nil
                showingDirectoryPicker = false
                launcherFocusTrigger += 1
            } else {
                select(panes[min(index, panes.count - 1)])
            }
        }
    }

    func toggleShell() {
        guard let pane = selectedPane else { return }
        if pane.shell == nil {
            var config = Ghostty.SurfaceConfiguration()
            config.workingDirectory = pane.directory
            guard let app = ghostty.app else { return }
            let shell = Ghostty.SurfaceView(app, baseConfig: config)
            PCode.applyColorScheme(to: shell)
            pane.setShell(shell)
        }
        pane.shellVisible.toggle()
        syncOcclusion()
        let target = pane.shellVisible ? pane.shell! : pane.keyboardSurface
        DispatchQueue.main.async { Ghostty.moveFocus(to: target) }
    }

    private func makePane(
        args: [String],
        directory: String,
        projectPath: String? = nil,
        sessionID: String?
    ) -> PCodeSessionPane? {
        guard let app = ghostty.app else { return nil }
        let fm = FileManager.default
        let isDir: (String) -> Bool = { path in
            var flag: ObjCBool = false
            return fm.fileExists(atPath: path, isDirectory: &flag) && flag.boolValue
        }
        let resolvedDir: String
        let resolvedProject: String
        if isDir(directory) {
            resolvedDir = directory
            resolvedProject = projectPath ?? directory
        } else if let projectPath, isDir(projectPath) {
            resolvedDir = projectPath
            resolvedProject = projectPath
        } else {
            let home = Self.normalizePath(NSHomeDirectory())
            resolvedDir = home
            resolvedProject = home
        }
        var config = Ghostty.SurfaceConfiguration()
        let launch = PCodeWorkspaceMode.launchConfig(args, directory: resolvedDir)
        config.command = launch.command
        for (key, value) in launch.env {
            config.environmentVariables[key] = value
        }
        config.workingDirectory = resolvedDir
        // The docked editor (PCodeDock) is what the workspace shows under each session.
        config.environmentVariables["PCODE_DOCK"] = "1"
        // Tell pcode the native transcript renders the session, so it leaves out
        // the editor rows only a terminal needs (tool rows, live command previews).
        config.environmentVariables["PCODE_NATIVE_TRANSCRIPT"] = "1"
        // Suppress macOS `login(1)` "Last login:" banner in pcode session panes.
        config.environmentVariables["PCODE_HUSH_LOGIN"] = "1"
        config.waitAfterCommand = false
        let view = Ghostty.SurfaceView(app, baseConfig: config)
        PCode.applyColorScheme(to: view)
        return PCodeSessionPane(
            surface: view,
            directory: resolvedDir,
            projectPath: resolvedProject,
            sessionID: sessionID)
    }

    private func surfaceClosed(_ view: Ghostty.SurfaceView, processAlive: Bool) {
        if let pane = panes.first(where: { $0.shell === view }) {
            pane.shellVisible = false
            pane.setShell(nil)
            view.freeSurface()
            Ghostty.moveFocus(to: pane.keyboardSurface)
            return
        }
        guard let pane = panes.first(where: { $0.surface === view }) else { return }
        guard processAlive, let window = view.window ?? NSApp.keyWindow else { return close(pane) }
        let alert = NSAlert()
        alert.messageText = "Close this session?"
        alert.informativeText = "pcode is still running in it. The session is saved and can be reopened from the sidebar."
        alert.addButton(withTitle: "Close")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self, weak pane] response in
            guard response == .alertFirstButtonReturn, let self, let pane else { return }
            self.close(pane)
        }
    }

    private func saveOpen() {
        guard !restarting else { return }
        UserDefaults.standard.set(panes.compactMap(\.sessionID), forKey: Self.openKey)
    }

    /// Remember the shown session, for `restore` to show again.
    private func saveSelected() {
        guard !restarting, let id = selectedPane?.sessionID else { return }
        UserDefaults.standard.set(id, forKey: Self.selectedKey)
    }

    /// Whether the app is quitting to restart (`PCodeRestart`): the saved
    /// open sessions and selection are kept as they were when it began,
    /// while pcode in each pane exits and the panes close.
    private(set) var restarting = false

    /// Save what is open and shown, then stop saving until the app quits.
    /// The sessions are marked as restarting, for the relaunched app to
    /// wait for their hosts (`PCodeSessionInfo.awaitRestarted`).
    func beginRestart() {
        saveOpen()
        saveSelected()
        let defaults = UserDefaults.standard
        defaults.set(panes.compactMap(\.sessionID), forKey: Self.restartIDsKey)
        defaults.set(Date(), forKey: Self.restartTimeKey)
        restarting = true
    }

    private static let restartIDsKey = "PCodeWorkspaceRestartIDs"
    private static let restartTimeKey = "PCodeWorkspaceRestartTime"

    /// The sessions a recent Restart Pcode moved to hosts. A marker older
    /// than the restart could take (the relaunch failed, and Pcode was
    /// opened by hand much later) is ignored.
    private static func restartMarker() -> [String] {
        let defaults = UserDefaults.standard
        guard let time = defaults.object(forKey: restartTimeKey) as? Date,
              abs(time.timeIntervalSinceNow) < 600 else { return [] }
        return defaults.stringArray(forKey: restartIDsKey) ?? []
    }

    private static func clearRestartMarker() {
        UserDefaults.standard.removeObject(forKey: restartIDsKey)
        UserDefaults.standard.removeObject(forKey: restartTimeKey)
    }
}

/// Pure geometry helpers for the workspace sidebar's width bounds and auto-hide hit zones.
enum PCodeSidebarLayout {
    static let defaultWidth: CGFloat = 250
    static let minWidth: CGFloat = 200
    static let maxWidth: CGFloat = 500
    /// Minimum width reserved for the session pane when resizing the sidebar.
    static let minDetailWidth: CGFloat = 360
    /// Width of the left-edge hot zone that reveals the auto-hidden sidebar.
    static let revealEdgeWidth: CGFloat = 10
    /// Extra margin past the revealed sidebar's right edge so resizing doesn't trigger a hide.
    static let hideGraceMargin: CGFloat = 12

    static func clampedWidth(_ proposed: CGFloat, totalWidth: CGFloat) -> CGFloat {
        let candidate = (proposed.isFinite && proposed > 0) ? proposed : defaultWidth
        let upper: CGFloat = totalWidth > 0
            ? max(minWidth, min(maxWidth, totalWidth - minDetailWidth))
            : maxWidth
        return min(upper, max(minWidth, candidate))
    }

    static func shouldReveal(at point: CGPoint, in contentBounds: CGRect) -> Bool {
        guard contentBounds.width > 0, contentBounds.height > 0 else { return false }
        return point.x >= contentBounds.minX
            && point.x <= contentBounds.minX + revealEdgeWidth
            && point.y >= contentBounds.minY
            && point.y <= contentBounds.maxY
    }

    static func isInsideRevealedZone(at point: CGPoint, in contentBounds: CGRect, sidebarWidth: CGFloat) -> Bool {
        guard contentBounds.width > 0, contentBounds.height > 0 else { return false }
        return point.x >= contentBounds.minX
            && point.x <= contentBounds.minX + sidebarWidth + hideGraceMargin
            && point.y >= contentBounds.minY
            && point.y <= contentBounds.maxY
    }
}

/// What the sidebar can select: an open pane, or a saved session to reopen.
private enum PCodeSidebarItem: Hashable {
    case pane(UUID)
    case saved(String)
}

struct PCodeWorkspaceView: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    @State private var isDropTargeted = false
    /// `workspace.sidebarWidth` when the current resize drag began.
    @State private var sidebarDragStart: CGFloat?
    /// Tracks macOS < 15 `NSCursor.resizeLeftRight` push state so pushes and pops stay balanced.
    @State private var resizeCursorPushed = false

    private static let initialRecentLimit = 5

    // A plain stack, not NavigationSplitView, and no toolbar: on macOS 26+
    // those lay glass (blur sampled every frame) over the terminal, which is
    // a scroll view, and make the window composite translucent layers.
    var body: some View {
        VStack(spacing: 0) {
            PCodeWindowTitleBar(workspace: workspace)
                .zIndex(10)
            ZStack {
                GeometryReader { geometry in
                    let totalWidth = geometry.size.width
                    let sidebarWidth = workspace.effectiveSidebarWidth(in: totalWidth)
                    let sidebarVisible = !workspace.sidebarAutoHide || workspace.sidebarRevealed
                    HStack(spacing: 0) {
                        ZStack(alignment: .trailing) {
                            if sidebarVisible {
                                sidebar
                                    .frame(width: sidebarWidth)
                                    .background(sidebarBackground)
                                    .onHover { workspace.sidebarHoverChanged($0) }
                                    .onAppear { workspace.scheduleSidebarHideIfPointerOutside() }
                                    .transition(.identity)
                            }
                        }
                        .frame(width: sidebarVisible ? sidebarWidth : 0, alignment: .trailing)
                        .clipped()

                        if sidebarVisible {
                            sidebarResizeHandle(width: sidebarWidth, totalWidth: totalWidth)
                                .transition(.identity)
                        }

                        detailPane
                    }
                    .animation(.easeOut(duration: 0.2), value: sidebarVisible)
                }

                if workspace.showingDirectoryPicker && workspace.selectedPane != nil {
                    PCodeProjectLauncher(workspace: workspace, mode: .overlay)
                        .transition(.opacity)
                }
            }
        }
        .ignoresSafeArea(.container, edges: .top)
        // Keep workspace keyboard shortcuts mounted even when the sidebar is auto-hidden.
        .background {
            Group {
                Button("") { workspace.toggleShell() }
                    .keyboardShortcut("j", modifiers: .command)
                Button("") { workspace.openDirectoryPicker() }
                    .keyboardShortcut("n", modifiers: [.command, .shift])
                Button("") { workspace.toggleSidebarAutoHide() }
                    .keyboardShortcut("s", modifiers: [.command, .option])
            }
            .opacity(0)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .onChange(of: isDropTargeted) { workspace.isSidebarDropTargeted = $0 }
        .onAppear {
            workspace.peek.startMonitoring()
            workspace.startSidebarMonitoring()
        }
        .onDisappear {
            workspace.peek.stopMonitoring()
            workspace.stopSidebarMonitoring()
        }
    }

    private var detailPane: some View {
        ZStack {
            if let pane = workspace.selectedPane {
                PCodeSessionPaneView(pane: pane)
                    .id(pane.id)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                PCodeProjectLauncher(workspace: workspace, mode: .inline)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            PCodePeekOverlay(
                peek: workspace.peek,
                resolveLivePane: { paneID in
                    guard paneID != workspace.selected else { return nil }
                    return workspace.panes.first { $0.id == paneID }
                }
            ) { target in
                workspace.openPeekTarget(target)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ghostty.config.backgroundColor, ignoresSafeAreaEdges: [])
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { workspace.detailSize = proxy.size }
                .onChange(of: proxy.size) { workspace.detailSize = $0 }
        })
    }

    private var sidebarBackground: Color {
        // Solid, a shade off the terminal's background: no vibrancy behind it.
        Color(nsColor: NSColor(ghostty.config.backgroundColor).blended(
            withFraction: 0.06, of: .white) ?? .windowBackgroundColor)
    }

    /// The sidebar's right edge: a hairline to see, a wider strip to drag (double-click resets width).
    private func sidebarResizeHandle(width: CGFloat, totalWidth: CGFloat) -> some View {
        Rectangle()
            .fill(ghostty.config.splitDividerColor)
            .frame(width: 1)
            .overlay(
                Color.clear
                    .frame(width: 9)
                    .contentShape(Rectangle())
                    .backport.pointerStyle(.resizeLeftRight)
                    .onHover { inside in
                        if #available(macOS 15, *) { return }
                        if inside {
                            if !resizeCursorPushed {
                                NSCursor.resizeLeftRight.push()
                                resizeCursorPushed = true
                            }
                        } else if resizeCursorPushed {
                            NSCursor.pop()
                            resizeCursorPushed = false
                        }
                    }
                    .onDisappear {
                        sidebarDragStart = nil
                        workspace.isResizingSidebar = false
                        if resizeCursorPushed {
                            NSCursor.pop()
                            resizeCursorPushed = false
                        }
                    }
                    .onTapGesture(count: 2) {
                        workspace.resetSidebarWidth()
                    }
                    .gesture(
                        DragGesture(coordinateSpace: .global)
                            .onChanged { gesture in
                                let start = sidebarDragStart ?? width
                                if sidebarDragStart == nil {
                                    sidebarDragStart = start
                                    workspace.isResizingSidebar = true
                                }
                                workspace.sidebarWidth = PCodeSidebarLayout.clampedWidth(
                                    start + gesture.translation.width,
                                    totalWidth: totalWidth
                                )
                            }
                            .onEnded { _ in
                                sidebarDragStart = nil
                                workspace.isResizingSidebar = false
                                workspace.scheduleSidebarHideIfPointerOutside()
                            }
                    )
            )
            .zIndex(1)
    }

    private var sidebarHeader: some View {
        HStack(spacing: 6) {
            Text("Projects")
                .font(.headline)
                .lineLimit(1)
            Spacer(minLength: 4)
            Button { workspace.toggleShell() } label: { Image(systemName: "terminal") }
                .help("Toggle the shell under this session (⌘J)")
            Button { workspace.openDirectoryPicker() } label: { Image(systemName: "folder.badge.plus") }
                .help("Open or add project… (⌘⇧N)")
            Button { workspace.newSession() } label: { Image(systemName: "square.and.pencil") }
                .help("New session in current project (⌘N)")
            Button { workspace.toggleSidebarAutoHide() } label: {
                if workspace.sidebarAutoHide {
                    Image(systemName: "sidebar.left")
                        .foregroundStyle(Color.accentColor)
                } else {
                    Image(systemName: "sidebar.left")
                }
            }
            .help(workspace.sidebarAutoHide ? "Keep sidebar visible (⌥⌘S)" : "Auto-hide sidebar (⌥⌘S)")
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var selection: Binding<PCodeSidebarItem?> {
        Binding(
            get: { workspace.selected.map(PCodeSidebarItem.pane) },
            set: { item in
                switch item {
                case .pane(let id):
                    if let pane = workspace.panes.first(where: { $0.id == id }) { workspace.select(pane) }
                case .saved(let id):
                    if let info = workspace.history.first(where: { $0.id == id }) { workspace.open(info) }
                case nil:
                    break
                }
            })
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            sidebarHeader
            sessionList
        }
        .overlay(
            RoundedRectangle(cornerRadius: 4)
                .stroke(Color.accentColor.opacity(isDropTargeted ? 0.8 : 0), lineWidth: 2)
                .padding(2)
                .allowsHitTesting(false)
        )
        .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
            handleFolderDrop(providers)
        }
        .onDisappear {
            isDropTargeted = false
            workspace.isSidebarDropTargeted = false
        }
    }

    private var sessionList: some View {
        List(selection: selection) {
            ForEach(workspace.projectGroups) { group in
                folderSection(for: group)
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
    }

    @ViewBuilder
    private func folderSection(for group: PCodeProjectGroup) -> some View {
        let collapsed = workspace.collapsedFolders.contains(group.path)
        let section = Section {
            if !collapsed {
                ForEach(group.openPanes) { pane in
                    PCodePaneRow(
                        pane: pane,
                        info: workspace.info(for: pane),
                        onHoverChange: { hovering in
                            guard pane.id != workspace.selected else { return }
                            workspace.peek.rowHoverChanged(workspace.peekTarget(for: pane), hovering: hovering)
                        }
                    ) { openMenu(pane) }
                        .tag(PCodeSidebarItem.pane(pane.id))
                        .contextMenu { openMenu(pane) }
                }
                let expanded = workspace.expandedRecentFolders.contains(group.path)
                let visibleRecent = expanded
                    ? group.recentSessions
                    : Array(group.recentSessions.prefix(Self.initialRecentLimit))
                ForEach(visibleRecent) { info in
                    PCodeSessionRow(
                        title: info.label,
                        subtitle: info.worktreeName ?? PCodeWorkspace.relativeTime(info.updatedDate),
                        activity: nil,
                        unseen: workspace.unseenIDs.contains(info.id),
                        onHoverChange: { hovering in
                            workspace.peek.rowHoverChanged(workspace.peekTarget(for: info), hovering: hovering)
                        }
                    ) {
                        recentMenu(info)
                    }
                    .tag(PCodeSidebarItem.saved(info.id))
                    .contextMenu { recentMenu(info) }
                }
                if !expanded && group.recentSessions.count > Self.initialRecentLimit {
                    let remaining = group.recentSessions.count - Self.initialRecentLimit
                    Button {
                        workspace.expandedRecentFolders.insert(group.path)
                    } label: {
                        Text("Show \(remaining) more…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.leading, 20)
                    }
                    .buttonStyle(.plain)
                }
                if group.openPanes.isEmpty && group.recentSessions.isEmpty {
                    Button {
                        workspace.newSession(directory: group.path)
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "plus")
                                .font(.caption2)
                            Text("New session")
                                .font(.caption)
                        }
                        .foregroundStyle(.secondary)
                        .padding(.leading, 20)
                        .padding(.vertical, 2)
                    }
                    .buttonStyle(.plain)
                }
            }
        } header: {
            PCodeFolderHeader(
                group: group,
                isCollapsed: collapsed,
                hasUnseen: group.hasUnseen(workspace.unseenIDs),
                onToggle: { workspace.toggleCollapsed(group.path) },
                onNewSession: { workspace.newSession(directory: group.path) }
            )
            .contextMenu { folderMenu(group) }
        }
        if #available(macOS 13.3, *) {
            section.collapsible(false)
        } else {
            section
        }
    }

    private func handleFolderDrop(_ providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            accepted = true
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                let url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let direct = item as? URL {
                    url = direct
                } else {
                    url = nil
                }
                guard let url else { return }
                let path = url.standardizedFileURL.path
                var isDir: ObjCBool = false
                guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
                    return
                }
                DispatchQueue.main.async {
                    workspace.addFolder(path)
                }
            }
        }
        return accepted
    }

    @ViewBuilder private func folderMenu(_ group: PCodeProjectGroup) -> some View {
        Button("New Session") { workspace.newSession(directory: group.path) }
        Divider()
        Button("Reveal in Finder") { PCodeWorkspace.reveal(group.path) }
        Button("Copy Path") { PCodeWorkspace.copy(group.path) }
        if group.openPanes.isEmpty {
            Divider()
            Button("Remove from Sidebar") { workspace.removeFolder(group.path) }
        }
    }

    @ViewBuilder private func openMenu(_ pane: PCodeSessionPane) -> some View {
        if pane.id != workspace.selected {
            Button {
                workspace.peek.peekFromMenu(workspace.peekTarget(for: pane))
            } label: {
                Label("Peek (⌥ Hover)", systemImage: "eye")
            }
            Divider()
        }
        Button("Close Session") { workspace.requestClose(pane) }
        if pane.sessionID != nil {
            Button("Delete Session…", role: .destructive) { workspace.confirmDelete(pane: pane) }
        }
        Divider()
        Button("Reveal Directory in Finder") { PCodeWorkspace.reveal(pane.directory) }
        if let id = pane.sessionID {
            Button("Copy Session ID") { PCodeWorkspace.copy(id) }
        }
    }

    @ViewBuilder private func recentMenu(_ info: PCodeSessionInfo) -> some View {
        Button {
            workspace.peek.peekFromMenu(workspace.peekTarget(for: info))
        } label: {
            Label("Peek (⌥ Hover)", systemImage: "eye")
        }
        Button("Open") { workspace.open(info) }
        Button("Delete Session…", role: .destructive) { workspace.confirmDelete(info: info) }
        Divider()
        Button("Reveal Directory in Finder") { PCodeWorkspace.reveal(info.workspace) }
        Button("Copy Session ID") { PCodeWorkspace.copy(info.id) }
    }
}

/// A project folder header in the sidebar: disclosure chevron, name, collapsed activity badge, and hover + button.
private struct PCodeFolderHeader: View {
    let group: PCodeProjectGroup
    let isCollapsed: Bool
    let hasUnseen: Bool
    let onToggle: () -> Void
    let onNewSession: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 5) {
            Button(action: onToggle) {
                HStack(spacing: 5) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(isCollapsed ? 0 : 90))
                        .frame(width: 10)
                    Image(systemName: isCollapsed ? "folder" : "folder.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Text(group.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(group.abbreviatedPath)

            if isCollapsed, group.activity != nil || hasUnseen {
                PCodeActivityIndicator(activity: group.activity, unseen: hasUnseen)
                    .frame(width: 12, height: 12)
            }

            Button(action: onNewSession) {
                Image(systemName: "plus")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(hovering ? 1 : 0)
            .help("New session in \(group.name)")
        }
        .textCase(nil)
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

private struct PCodePaneRow<Actions: View>: View {
    @ObservedObject var pane: PCodeSessionPane
    var info: PCodeSessionInfo?
    var onHoverChange: ((Bool) -> Void)? = nil
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        let title = PCodeWorkspace.displayTitle(paneTitle: pane.title, savedLabel: info?.label)
        PCodeSessionRow(
            title: title,
            subtitle: info?.worktreeName ?? pane.worktreeName,
            activity: pane.activity,
            unseen: pane.unseen,
            onHoverChange: onHoverChange,
            actions: actions)
    }
}

/// A sidebar row; its actions are a right-click menu and, on hover, a … button.
private struct PCodeSessionRow<Actions: View>: View {
    var title: String
    var subtitle: String?
    /// Nil for a session not open here.
    var activity: PCodeSessionPane.Activity?
    /// A finished response not yet looked at here.
    var unseen = false
    var onHoverChange: ((Bool) -> Void)? = nil
    @ViewBuilder var actions: () -> Actions
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 8) {
            PCodeActivityIndicator(activity: activity, unseen: unseen)
                .frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).lineLimit(1).truncationMode(.tail)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            Menu { actions() } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .opacity(hovering ? 1 : 0)
                .help("Session actions")
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onHover { inside in
            hovering = inside
            onHoverChange?(inside)
        }
    }
}

/// Owns the local keyDown monitor for `PCodeProjectLauncher` so it cleans up on teardown.
private final class PCodePickerKeyMonitor: ObservableObject {
    private var monitor: Any?
    var handler: ((NSEvent) -> NSEvent?)?

    func start() {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handler?(event) ?? event
        }
    }

    func stop() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
            self.monitor = nil
        }
    }

    deinit { stop() }
}

/// Unified Project Command Center: rendered inline as the empty-state launcher
/// when no session is open, or as a floating ⌘⇧N overlay over an active session.
///
/// - Fuzzy-filters known projects, or lists filesystem subdirectories when typing `/` or `~/`.
/// - `Tab` completes filesystem paths (and drills into a selected project's subdirectories).
/// - `↩` starts a new session in the selected project.
/// - `⌘↩` pins/adds the selected project to the sidebar without starting a session.
/// - `⌘O` opens the macOS folder chooser (`NSOpenPanel`); folders can also be dropped onto the card.
private struct PCodeProjectLauncher: View {
    enum Mode {
        case inline
        case overlay
    }

    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    let mode: Mode

    @StateObject private var keyMonitor = PCodePickerKeyMonitor()
    @State private var query = ""
    @State private var cachedPathSuggestions: [String] = []
    @State private var selectedIndex = 0
    @State private var hoveredID: String?
    @State private var isDropTargeted = false
    @State private var statusBanner: String?
    @State private var statusClearWork: DispatchWorkItem?
    @FocusState private var searchFocused: Bool

    private var filteredCandidates: [PCodeDirectoryCandidate] {
        let base = workspace.pickerCandidates
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return base }

        if PCodePathCompleter.isPathQuery(query) {
            let knownByPath = Dictionary(base.map { ($0.path, $0) }, uniquingKeysWith: { a, _ in a })
            let home = PCodeWorkspace.normalizePath(NSHomeDirectory())
            var result: [PCodeDirectoryCandidate] = []
            var seen: Set<String> = []

            for path in cachedPathSuggestions where seen.insert(path).inserted {
                if let known = knownByPath[path] {
                    result.append(known)
                } else {
                    let last = (path as NSString).lastPathComponent
                    result.append(PCodeDirectoryCandidate(
                        path: path,
                        name: path == home ? "~" : (last.isEmpty ? path : last),
                        abbreviatedPath: PCodeWorkspace.abbreviatePath(path),
                        openCount: 0,
                        recentCount: 0,
                        isCurrent: false,
                        isPinned: false,
                        isRawPath: true))
                }
            }
            return result
        }

        let terms = PCodePickMatch.terms(trimmed)
        return base.filter { candidate in
            let texts = [
                PCodePickMatch.casefold(candidate.name),
                PCodePickMatch.casefold(candidate.abbreviatedPath),
                PCodePickMatch.casefold(candidate.path),
            ]
            return PCodePickMatch.matches(texts, terms: terms, fuzzy: true)
        }
    }

    private func refreshPathSuggestions(for newQuery: String) {
        if PCodePathCompleter.isPathQuery(newQuery) {
            cachedPathSuggestions = PCodePathCompleter.directorySuggestions(for: newQuery)
        } else if !cachedPathSuggestions.isEmpty {
            cachedPathSuggestions = []
        }
    }

    private var cardBackground: Color {
        Color(nsColor: NSColor(ghostty.config.backgroundColor).blended(
            withFraction: 0.08, of: .white) ?? .windowBackgroundColor)
    }

    var body: some View {
        let items = filteredCandidates
        let activeIndex = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)

        Group {
            switch mode {
            case .overlay:
                ZStack(alignment: .top) {
                    Color.black.opacity(0.32)
                        .ignoresSafeArea()
                        .onTapGesture { workspace.dismissDirectoryPicker() }

                    launcherCard(items: items, activeIndex: activeIndex)
                        .frame(width: 500)
                        .shadow(color: .black.opacity(0.35), radius: 20, y: 10)
                        .padding(.top, 72)
                }

            case .inline:
                VStack(spacing: 18) {
                    VStack(spacing: 4) {
                        Text("Open a project")
                            .font(.title2.weight(.semibold))
                            .foregroundStyle(.primary)
                        Text("Press ↩ to start a session, type a path with Tab completion, or drop a folder here.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(.horizontal, 24)

                    launcherCard(items: items, activeIndex: activeIndex)
                        .frame(maxWidth: 520)
                        .shadow(color: .black.opacity(0.22), radius: 14, y: 6)
                }
                .padding(.horizontal, 32)
                .padding(.vertical, 36)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            }
        }
        .onChange(of: query) { newQuery in
            refreshPathSuggestions(for: newQuery)
            selectedIndex = 0
        }
        .onChange(of: items.count) { count in
            if count == 0 {
                selectedIndex = 0
            } else if selectedIndex >= count {
                selectedIndex = count - 1
            }
        }
        .onChange(of: workspace.launcherFocusTrigger) { _ in
            if mode == .inline {
                DispatchQueue.main.async { searchFocused = true }
            }
        }
        .onAppear {
            refreshPathSuggestions(for: query)
            selectedIndex = 0
            DispatchQueue.main.async { searchFocused = true }
            keyMonitor.handler = handleKeyEvent
            keyMonitor.start()
        }
        .onDisappear {
            statusClearWork?.cancel()
            statusClearWork = nil
            keyMonitor.stop()
            keyMonitor.handler = nil
        }
    }

    private func launcherCard(items: [PCodeDirectoryCandidate], activeIndex: Int) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter projects or type a path…", text: $query)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .focused($searchFocused)
                    .onSubmit {
                        let currentItems = filteredCandidates
                        let idx = currentItems.isEmpty ? 0 : min(selectedIndex, currentItems.count - 1)
                        if currentItems.indices.contains(idx) {
                            selectCandidate(currentItems[idx])
                        }
                    }
                if !query.isEmpty {
                    Button {
                        query = ""
                        searchFocused = true
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)

            Divider()

            if items.isEmpty {
                VStack(spacing: 6) {
                    Text("No matching projects")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Text("Type a path like ~/p/app (Tab to complete) or press ⌘O to browse.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 28)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 2) {
                            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                candidateRow(item, isSelected: index == activeIndex)
                                    .id(item.id)
                                    .onHover { inside in
                                        hoveredID = inside ? item.id : (hoveredID == item.id ? nil : hoveredID)
                                    }
                                    .onTapGesture {
                                        selectCandidate(item)
                                    }
                            }
                        }
                        .padding(6)
                    }
                    .frame(maxHeight: mode == .inline ? 340 : 280)
                    .onChange(of: activeIndex) { newIndex in
                        if items.indices.contains(newIndex) {
                            proxy.scrollTo(items[newIndex].id, anchor: .center)
                        }
                    }
                }
            }

            Divider()

            HStack(spacing: 10) {
                if let statusBanner {
                    HStack(spacing: 5) {
                        Image(systemName: "pin.fill")
                            .font(.caption2)
                            .foregroundStyle(Color.accentColor)
                        Text(statusBanner)
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                    }
                } else {
                    Text("↩ session")
                    Text("⌘↩ pin")
                    Text("⇥ complete")
                    if mode == .overlay {
                        Text("esc close")
                    }
                }
                Spacer(minLength: 8)
                Button {
                    browseFromPicker()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "folder")
                        Text("Browse… (⌘O)")
                    }
                }
                .buttonStyle(.plain)
                .foregroundStyle(.primary)
                .keyboardShortcut("o", modifiers: .command)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background {
                if mode == .overlay {
                    Button("") { workspace.dismissDirectoryPicker() }
                        .keyboardShortcut(.cancelAction)
                        .opacity(0)
                        .frame(width: 0, height: 0)
                }
            }
        }
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(
                    isDropTargeted ? Color.accentColor.opacity(0.85) : Color.primary.opacity(0.14),
                    lineWidth: isDropTargeted ? 2 : 1
                )
        )
        .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
            handleDrop(providers)
        }
        .onTapGesture { searchFocused = true }
    }

    private func browseFromPicker() {
        workspace.browseForFolder(startSession: true) {
            searchFocused = true
        }
    }

    private func candidateRow(_ item: PCodeDirectoryCandidate, isSelected: Bool) -> some View {
        let showPinButton = (isSelected || hoveredID == item.id) && !item.isPinned
        return HStack(spacing: 10) {
            Image(systemName: item.isRawPath ? "folder.badge.plus" : "folder.fill")
                .font(.system(size: 13))
                .foregroundStyle(isSelected ? Color.accentColor : .secondary)
                .frame(width: 18)

            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(item.name)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    if item.isCurrent {
                        Text("current")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.primary.opacity(0.08))
                            .clipShape(Capsule())
                    } else if item.isPinned {
                        Image(systemName: "pin.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                            .help("Pinned in sidebar")
                    }
                }
                Text(item.abbreviatedPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            if showPinButton {
                Button {
                    pinCandidate(item)
                } label: {
                    Image(systemName: "pin")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Add project to sidebar (⌘↩)")
            }

            if item.isRawPath {
                Text("Open path")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                let parts = [
                    item.openCount > 0 ? "\(item.openCount) open" : nil,
                    item.recentCount > 0 ? "\(item.recentCount) saved" : nil,
                ].compactMap { $0 }
                if !parts.isEmpty {
                    Text(parts.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        )
        .contentShape(Rectangle())
    }

    private func selectCandidate(_ item: PCodeDirectoryCandidate) {
        workspace.addFolder(item.path)
        workspace.newSession(directory: item.path)
    }

    private func pinCandidate(_ item: PCodeDirectoryCandidate) {
        if workspace.addFolder(item.path) {
            showStatus("Pinned \(item.name) to sidebar")
        }
        searchFocused = true
    }

    private func showStatus(_ message: String) {
        statusClearWork?.cancel()
        statusBanner = message
        let work = DispatchWorkItem {
            statusBanner = nil
            statusClearWork = nil
        }
        statusClearWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0, execute: work)
    }

    private func completePath() {
        let items = filteredCandidates
        let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
        let selectedPath = items.indices.contains(idx) ? items[idx].path : nil
        guard let result = PCodePathCompleter.complete(query: query, selectedPath: selectedPath) else {
            searchFocused = true
            return
        }
        query = result.completed
        refreshPathSuggestions(for: result.completed)
        searchFocused = true
        let cursorLocation = result.completed.utf16.count
        DispatchQueue.main.async {
            if let editor = workspace.window?.firstResponder as? NSTextView {
                let safeLocation = min(cursorLocation, (editor.string as NSString).length)
                editor.setSelectedRange(NSRange(location: safeLocation, length: 0))
            }
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            accepted = true
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                let url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let direct = item as? URL {
                    url = direct
                } else {
                    url = nil
                }
                guard let url else { return }
                let path = url.standardizedFileURL.path
                var isDir: ObjCBool = false
                guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
                    return
                }
                DispatchQueue.main.async {
                    workspace.addFolder(path)
                    workspace.newSession(directory: path)
                }
            }
            break
        }
        return accepted
    }

    private func handleKeyEvent(_ event: NSEvent) -> NSEvent? {
        // Let modal sheets (like NSOpenPanel), Peek, and other windows handle their own keys.
        guard event.window === workspace.window,
              event.window?.attachedSheet == nil,
              !workspace.peek.isShowing
        else { return event }
        // In inline mode, only intercept while the launcher search field has focus
        // (so keyboard navigation in the sidebar List stays untouched).
        if mode == .inline && (workspace.showingDirectoryPicker || workspace.selectedPane != nil || !searchFocused) {
            return event
        }
        // Don't hijack keys during IME composition.
        if let client = event.window?.firstResponder as? NSTextInputClient, client.hasMarkedText() {
            return event
        }
        let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
        let chars = event.charactersIgnoringModifiers?.lowercased()

        // Bare Escape
        if flags.isEmpty && event.keyCode == 53 {
            if !query.isEmpty {
                query = ""
                searchFocused = true
                return nil
            }
            if mode == .overlay {
                workspace.dismissDirectoryPicker()
                return nil
            }
            return event
        }
        // ⌘O -> browse via NSOpenPanel
        if flags == .command && chars == "o" {
            browseFromPicker()
            return nil
        }
        // Bare Up arrow or Ctrl+P
        if (flags.isEmpty && event.keyCode == 126) || (flags == .control && chars == "p") {
            let items = filteredCandidates
            if !items.isEmpty {
                let idx = min(selectedIndex, items.count - 1)
                selectedIndex = (idx - 1 + items.count) % items.count
            }
            return nil
        }
        // Bare Down arrow or Ctrl+N
        if (flags.isEmpty && event.keyCode == 125) || (flags == .control && chars == "n") {
            let items = filteredCandidates
            if !items.isEmpty {
                let idx = min(selectedIndex, items.count - 1)
                selectedIndex = (idx + 1) % items.count
            }
            return nil
        }
        // ⌘Return / ⌘Keypad Enter -> pin/add project to sidebar without starting a session
        if flags == .command && (event.keyCode == 36 || event.keyCode == 76) {
            let items = filteredCandidates
            let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
            if items.indices.contains(idx) {
                pinCandidate(items[idx])
            }
            return nil
        }
        // Bare Return / Keypad Enter -> start new session in selected project
        if flags.isEmpty && (event.keyCode == 36 || event.keyCode == 76) {
            let items = filteredCandidates
            let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
            if items.indices.contains(idx) {
                selectCandidate(items[idx])
            }
            return nil
        }
        // Bare Tab -> filesystem path completion (or drill into selected project)
        if flags.isEmpty && event.keyCode == 48 {
            completePath()
            return nil
        }
        return event
    }
}

/// Open and idle: a dot. A turn running: pcode's `dual` spinner (two
/// counter-rotating crescents, `kitty_spinner._render_dual`). Failed: red.
/// A response not yet looked at: green, open or not.
private struct PCodeActivityIndicator: View {
    var activity: PCodeSessionPane.Activity?
    var unseen = false

    var body: some View {
        switch activity {
        case .running?:
            // 30 fps is plenty for 12 points, and keeps the sidebar from
            // redrawing at the terminal's 120.
            TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                Canvas { gc, size in
                    let side = min(size.width, size.height)
                    let center = CGPoint(x: size.width / 2, y: size.height / 2)
                    Self.arc(gc, center: center, radius: side * 0.40, width: side * 0.13,
                             head: t * 3.6, sweep: 2.65, color: .accentColor)
                    Self.arc(gc, center: center, radius: side * 0.22, width: side * 0.12,
                             head: -t * 5.4, sweep: 2.45, color: .accentColor.opacity(0.75))
                }
            }
        case .failed?:
            Circle().fill(Color.red).frame(width: 6, height: 6)
        case _ where unseen:
            Circle().fill(Color.green).frame(width: 7, height: 7)
                .help("New response")
        case .idle?:
            Circle().fill(Color.secondary.opacity(0.7)).frame(width: 6, height: 6)
        case nil:
            Color.clear
        }
    }

    /// A crescent whose tail fades behind its head, as in pcode's spinner.
    private static func arc(
        _ gc: GraphicsContext, center: CGPoint, radius: CGFloat, width: CGFloat,
        head: Double, sweep: Double, color: Color
    ) {
        let steps = 12
        for step in 0..<steps {
            let from = head - sweep * Double(step + 1) / Double(steps)
            let to = head - sweep * Double(step) / Double(steps)
            var path = Path()
            path.addArc(center: center, radius: radius, startAngle: .radians(from), endAngle: .radians(to), clockwise: false)
            let fade = 1.0 - 0.85 * Double(step) / Double(steps)
            gc.stroke(path, with: .color(color.opacity(fade)), style: StrokeStyle(lineWidth: width, lineCap: step == 0 ? .round : .butt))
        }
    }
}

/// A session: pcode's terminal with everything pcode lays over it, and the bottom shell.
struct PCodeSessionPaneView: View {
    @ObservedObject var pane: PCodeSessionPane
    @EnvironmentObject var ghostty: Ghostty.App
    /// The shell's height, set by dragging its top edge; shared by every session.
    @AppStorage("PCodeWorkspaceShellHeight") private var shellHeight: Double = 200
    @State private var dragStart: Double?
    private static let minShell: CGFloat = 80
    /// pcode's own pane keeps at least this much above the shell.
    private static let minSession: CGFloat = 200

    var body: some View {
        GeometryReader { geometry in
            let maxShell = max(Self.minShell, geometry.size.height - Self.minSession)
            VStack(spacing: 0) {
                ZStack {
                    Ghostty.InspectableSurface(surfaceView: pane.surface, isSplit: false)
                    PCodeSurfaceOverlay(surfaceView: pane.surface)
                    PCodeStartupOverlay(
                        isReady: pane.isReady,
                        label: (pane.projectPath as NSString).lastPathComponent,
                        resuming: pane.resuming
                    )
                }
                if pane.shellVisible, let shell = pane.shell {
                    handle(maxShell: maxShell)
                    Ghostty.SurfaceWrapper(surfaceView: shell, isSplit: false)
                        .id(ObjectIdentifier(shell))
                        .frame(height: min(maxShell, max(Self.minShell, shellHeight)))
                }
            }
        }
    }

    /// The shell's top edge: a hairline to see, a taller strip to drag.
    private func handle(maxShell: CGFloat) -> some View {
        Rectangle()
            .fill(ghostty.config.splitDividerColor)
            .frame(height: 1)
            .overlay(
                Color.clear
                    .frame(height: 9)
                    .contentShape(Rectangle())
                    .backport.pointerStyle(.resizeUpDown)
                    .onHover { inside in
                        if #available(macOS 15, *) { return }
                        if inside { NSCursor.resizeUpDown.push() } else { NSCursor.pop() }
                    }
                    .gesture(
                        DragGesture(coordinateSpace: .global)
                            .onChanged { gesture in
                                let start = dragStart ?? shellHeight
                                dragStart = start
                                shellHeight = min(maxShell, max(Self.minShell, start - gesture.translation.height))
                            }
                            .onEnded { _ in dragStart = nil }
                    )
            )
            .zIndex(1)
    }
}

/// Custom opaque top title bar that sits in the window's traffic-light strip
/// (`.fullSizeContentView` + hidden system title, no NSToolbar / no glass).
/// Shows the active session's activity indicator, project/worktree context pill,
/// and full session title with generous horizontal room.
private struct PCodeWindowTitleBar: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    @Environment(\.controlActiveState) private var controlActiveState

    private var leadingClearance: CGFloat {
        workspace.isFullScreen ? 16 : workspace.trafficLightClearance
    }

    private var titlebarColorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            ghostty.config.backgroundColor

            Group {
                if let pane = workspace.selectedPane {
                    PCodeActiveSessionTitleContent(
                        pane: pane,
                        workspace: workspace,
                        leadingClearance: leadingClearance
                    )
                    .id(pane.id)
                } else {
                    HStack {
                        Spacer(minLength: leadingClearance)
                        Text("pcode")
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 16)
                    }
                    .onAppear { workspace.window?.title = "pcode" }
                }
            }
            .opacity(controlActiveState == .inactive ? 0.55 : 1)
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Rectangle()
                .fill(ghostty.config.splitDividerColor.opacity(0.65))
                .frame(height: 1)
        }
        .environment(\.colorScheme, titlebarColorScheme)
        .frame(height: workspace.titlebarHeight)
        .allowsHitTesting(false)
    }
}

/// Observes the active pane directly so title (`/rename`, auto-title) and activity
/// changes refresh the title bar without re-rendering the whole workspace.
private struct PCodeActiveSessionTitleContent: View {
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var workspace: PCodeWorkspace
    let leadingClearance: CGFloat

    private var savedInfo: PCodeSessionInfo? {
        workspace.info(for: pane)
    }

    private var title: String {
        workspace.displayTitle(for: pane)
    }

    private var projectName: String {
        workspace.projectName(for: pane)
    }

    private var worktreeName: String? {
        savedInfo?.worktreeName ?? pane.worktreeName
    }

    var body: some View {
        HStack(spacing: 0) {
            Spacer(minLength: leadingClearance)

            HStack(spacing: 7) {
                PCodeActivityIndicator(activity: pane.activity, unseen: false)
                    .frame(width: 10, height: 10)

                HStack(spacing: 4) {
                    Image(systemName: "folder.fill")
                        .font(.system(size: 9.5))
                        .foregroundStyle(.secondary.opacity(0.85))
                    Text(projectName)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if let worktreeName, !worktreeName.isEmpty {
                        Text("/")
                            .font(.system(size: 10.5))
                            .foregroundStyle(.tertiary)
                        Text(worktreeName)
                            .font(.system(size: 11, weight: .regular))
                            .foregroundStyle(.secondary.opacity(0.9))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(
                    Capsule(style: .continuous)
                        .fill(Color.primary.opacity(0.07))
                )
                .overlay(
                    Capsule(style: .continuous)
                        .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                )

                Text(title)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(.primary.opacity(0.92))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .layoutPriority(1)
            }

            Spacer(minLength: 16)
        }
        .onAppear { syncWindowTitle() }
        .onChange(of: title) { _ in syncWindowTitle() }
        .onChange(of: projectName) { _ in syncWindowTitle() }
    }

    private func syncWindowTitle() {
        let full = "\(projectName) — \(title)"
        if workspace.window?.title != full {
            workspace.window?.title = full
        }
    }
}

/// The one workspace window.
final class PCodeWorkspaceController: NSWindowController, NSWindowDelegate {
    private static var instance: PCodeWorkspaceController?
    let workspace: PCodeWorkspace

    /// The controller, if the window was ever made.
    static var existing: PCodeWorkspaceController? { instance }

    static func shared(_ ghostty: Ghostty.App) -> PCodeWorkspaceController {
        if let instance { return instance }
        let controller = PCodeWorkspaceController(ghostty: ghostty)
        instance = controller
        return controller
    }

    /// Show the window, restoring sessions once on first open.
    static func show(_ ghostty: Ghostty.App) {
        let controller = shared(ghostty)
        if !controller.workspace.hasRestored {
            controller.workspace.restore()
        } else if controller.workspace.panes.isEmpty && !controller.workspace.showingDirectoryPicker {
            controller.workspace.reloadHistory()
            controller.workspace.launcherFocusTrigger += 1
        }
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private init(ghostty: Ghostty.App) {
        workspace = PCodeWorkspace(ghostty: ghostty)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "pcode"
        window.titleVisibility = .hidden
        window.titlebarSeparatorStyle = .none
        // Opaque, in the terminal's own color, as Ghostty's windows are: the
        // window server then never blends anything under the terminal.
        window.titlebarAppearsTransparent = true
        window.isOpaque = true
        window.backgroundColor = NSColor(ghostty.config.backgroundColor).withAlphaComponent(1)
        window.tabbingMode = .disallowed
        window.isReleasedWhenClosed = false
        // Keep mouseMoved events flowing even when no terminal SurfaceView is open so auto-hide reveal still works.
        window.acceptsMouseMovedEvents = true
        let hosting = NSHostingController(
            rootView: PCodeWorkspaceView(workspace: workspace)
                .frame(minWidth: 700, minHeight: 400)
                .environmentObject(ghostty))
        // The window keeps its own size; by default the controller shrinks it
        // to SwiftUI's ideal size, which for a split view is tiny.
        hosting.sizingOptions = [.minSize]
        window.contentViewController = hosting
        window.setContentSize(NSSize(width: 1200, height: 800))
        window.setFrameAutosaveName("PCodeWorkspace")
        if !window.setFrameUsingName("PCodeWorkspace") || window.frame.width < 700 {
            window.setContentSize(NSSize(width: 1200, height: 800))
            window.center()
        }
        super.init(window: window)
        window.delegate = self
        workspace.window = window
        syncTitlebarMetrics(for: window)
        workspace.followAppearance()
        workspace.windowVisible = { [weak window] in
            window?.occlusionState.contains(.visible) ?? false
        }
        // A light/dark switch (`theme = light:…,dark:…`) reloads the config;
        // the titlebar and any gap must take the new background too.
        configSubscription = ghostty.$config
            .receive(on: DispatchQueue.main)
            .sink { [weak window] config in
                window?.backgroundColor = NSColor(config.backgroundColor).withAlphaComponent(1)
            }
    }

    private var configSubscription: AnyCancellable?

    func windowDidChangeOcclusionState(_ notification: Notification) {
        workspace.syncOcclusion()
        workspace.selectedMaybeSeen()
    }

    func windowDidBecomeKey(_ notification: Notification) {
        workspace.selectedMaybeSeen()
    }

    func windowDidResignKey(_ notification: Notification) {
        workspace.peek.windowDidResignKey()
        workspace.sidebarWindowDidResignKey()
    }

    func windowWillClose(_ notification: Notification) {
        if workspace.showingDirectoryPicker {
            workspace.showingDirectoryPicker = false
        }
        workspace.peek.dismiss(restoreFocus: false)
        workspace.sidebarWindowWillClose()
    }

    private func syncTitlebarMetrics(for window: NSWindow) {
        let barHeight = window.frame.height - window.contentLayoutRect.height
        if barHeight > 0 {
            workspace.titlebarHeight = barHeight
        }
        if let zoom = window.standardWindowButton(.zoomButton) {
            let maxX = zoom.convert(zoom.bounds, to: nil).maxX
            if maxX > 0 {
                workspace.trafficLightClearance = max(78, ceil(maxX) + 14)
            }
        }
    }

    func windowWillEnterFullScreen(_ notification: Notification) {
        workspace.isFullScreen = true
    }

    func windowDidFailToEnterFullScreen(_ window: NSWindow) {
        workspace.isFullScreen = false
        syncTitlebarMetrics(for: window)
    }

    func windowWillExitFullScreen(_ notification: Notification) {
        workspace.isFullScreen = false
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        workspace.isFullScreen = false
        if let window {
            syncTitlebarMetrics(for: window)
        }
    }

    func windowDidFailToExitFullScreen(_ window: NSWindow) {
        workspace.isFullScreen = true
    }

    required init?(coder: NSCoder) { fatalError("not restorable from a coder") }

    // MARK: First Responder

    /// ⌘W: a picker or popup with the keyboard closes itself; otherwise the
    /// focused shell, or the session (the docked editor stands for its session).
    @IBAction func close(_ sender: Any) {
        if workspace.showingDirectoryPicker {
            workspace.dismissDirectoryPicker()
            return
        }
        if workspace.peek.isShowing {
            workspace.peek.dismiss()
            return
        }
        if let picker = PCode.keyboardPicker(in: window) {
            picker.cancel()
            return
        }
        if let overlay = PCode.keyboardOverlay(in: window)?.surface {
            workspace.ghostty.requestClose(surface: overlay)
            return
        }
        guard let pane = workspace.selectedPane else { return }
        if pane.shellVisible, let shell = pane.shell, let surface = shell.surface,
           let responder = window?.firstResponder as? NSView,
           responder === shell || responder.isDescendant(of: shell) {
            workspace.ghostty.requestClose(surface: surface)
            return
        }
        workspace.requestClose(pane)
    }

    @IBAction func closeWindow(_ sender: Any) {
        window?.performClose(sender)
    }
}
