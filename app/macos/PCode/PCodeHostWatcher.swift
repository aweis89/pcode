import Combine
import Darwin
import Foundation

/// What a running session host says about itself in `hosts/<id>.json`
/// (`HostEntry` in `pcode.host_protocol`), as far as a workspace pane needs it.
struct PCodeHostStatus: Equatable {
    let hostID: String
    let pid: Int32
    let sessionID: String
    /// "starting", "idle" or "working" ("stopped" entries are never returned by `parse`).
    let state: String
    /// The session's name once it has one, else its first prompt.
    let title: String
    /// How the last finished turn ended: "", "done", "failed" or "cancelled".
    let outcome: String
    /// Finished turns so far: tells one failed turn from the next.
    let turns: Int
    /// A turn finished with no terminal attached. Always false while the pane's
    /// own pcode is attached, so the workspace keeps its own unseen tracking.
    let unseen: Bool
    let updated: Double

    var isWorking: Bool { state == "working" }

    /// Parse one entry; nil for anything that isn't a usable, running-looking host
    /// of a session (no id, no session yet, bad pid, or a remembered stopped host).
    static func parse(_ data: Data) -> PCodeHostStatus? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let hostID = obj["id"] as? String, PCode.valid(hostID, length: 1...64),
              let pid = (obj["pid"] as? NSNumber)?.int32Value, pid > 0,
              let sessionID = obj["session_id"] as? String, !sessionID.isEmpty
        else { return nil }
        let state = (obj["state"] as? String) ?? ""
        guard state != "stopped" else { return nil }
        return PCodeHostStatus(
            hostID: hostID,
            pid: pid,
            sessionID: sessionID,
            state: state,
            title: (obj["title"] as? String) ?? "",
            outcome: (obj["outcome"] as? String) ?? "",
            turns: (obj["turns"] as? NSNumber)?.intValue ?? 0,
            unseen: (obj["unseen"] as? Bool) ?? false,
            updated: (obj["updated"] as? NSNumber)?.doubleValue ?? 0)
    }

    /// Key entries by session; if two claim one session, the latest write wins.
    static func bySession(_ statuses: [PCodeHostStatus]) -> [String: PCodeHostStatus] {
        var map: [String: PCodeHostStatus] = [:]
        for status in statuses where (map[status.sessionID]?.updated ?? -.infinity) < status.updated {
            map[status.sessionID] = status
        }
        return map
    }
}

/// Publishes every running host's `hosts/<id>.json`, keyed by session id.
///
/// Hosts write their entry with write-then-rename (`host_protocol._write`), so a
/// watch on the file itself would go stale after the first update; this watches
/// the directory, whose contents change on every such rename. Events are
/// coalesced and the (small) directory is rescanned off the main thread. A slow
/// poll backs it up for what makes no directory event: a host that died
/// without removing its entry, and the directory appearing later.
final class PCodeHostWatcher {
    static let shared = PCodeHostWatcher()

    @Published private(set) var statuses: [String: PCodeHostStatus] = [:]

    private static let coalesce: DispatchTimeInterval = .milliseconds(30)
    private static let pollInterval: TimeInterval = 5

    private let directory: URL
    private let queue = DispatchQueue(label: "pcode.host.watcher", qos: .utility)
    // Below: touched only on `queue`.
    private var source: DispatchSourceFileSystemObject?
    private var scanPending = false
    /// Pids already confirmed to be pcode hosts, so a rescan only needs `kill(pid, 0)`.
    private var verifiedPIDs = Set<Int32>()
    private var pollTimer: DispatchSourceTimer?

    init(directory: URL = PCodeSessionInfo.hostRoot) {
        self.directory = directory
        queue.async { [weak self] in self?.start() }
    }

    deinit {
        source?.cancel()
        pollTimer?.cancel()
    }

    private func start() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.pollInterval, repeating: Self.pollInterval, leeway: .seconds(1))
        timer.setEventHandler { [weak self] in
            self?.openSourceIfNeeded()
            self?.scan()
        }
        timer.resume()
        pollTimer = timer
        openSourceIfNeeded()
        scan()
    }

    private func openSourceIfNeeded() {
        guard source == nil else { return }
        let fd = Darwin.open(directory.path, O_EVTONLY | O_DIRECTORY | O_CLOEXEC)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .delete, .rename], queue: queue)
        source.setEventHandler { [weak self, weak source] in
            guard let self else { return }
            if let flags = source?.data, !flags.isDisjoint(with: [.delete, .rename]) {
                // The directory itself went away or moved: reopen on the next poll.
                source?.cancel()
                self.source = nil
            }
            self.scheduleScan()
        }
        source.setCancelHandler { Darwin.close(fd) }
        source.resume()
        self.source = source
    }

    private func scheduleScan() {
        guard !scanPending else { return }
        scanPending = true
        queue.asyncAfter(deadline: .now() + Self.coalesce) { [weak self] in
            guard let self else { return }
            self.scanPending = false
            self.scan()
        }
    }

    private func scan() {
        let map = PCodeHostStatus.bySession(readEntries().filter(isAlive))
        DispatchQueue.main.async { [weak self] in
            guard let self, self.statuses != map else { return }
            self.statuses = map
        }
    }

    private func readEntries() -> [PCodeHostStatus] {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { return [] }
        let uid = Darwin.getuid()
        return files.compactMap { file in
            guard file.pathExtension == "json" else { return nil }
            var st = stat()
            guard Darwin.lstat(file.path, &st) == 0,
                  (st.st_mode & S_IFMT) == S_IFREG,
                  st.st_uid == uid,
                  let data = try? Data(contentsOf: file),
                  let status = PCodeHostStatus.parse(data),
                  // The file is named for the host it describes.
                  status.hostID == file.deletingPathExtension().lastPathComponent
            else { return nil }
            return status
        }
    }

    private func isAlive(_ status: PCodeHostStatus) -> Bool {
        guard kill(status.pid, 0) == 0 else {
            verifiedPIDs.remove(status.pid)
            return false
        }
        if verifiedPIDs.contains(status.pid) { return true }
        // Alive and still a host, not a reused pid.
        guard PCodeProcess.isPCodeOrHost(status.pid) else { return false }
        verifiedPIDs.insert(status.pid)
        return true
    }
}

/// A workspace pane's title and activity: from its session host while one holds
/// the session, else from what pcode writes to its terminal (OSC 0 and OSC 9;4).
///
/// With a host, a turn's running state and a failed outcome come from the host
/// entry. Two things it doesn't carry stay on the OSC source (the editor's own
/// terminal): a `!command` running at the prompt, and the user dismissing the
/// failed mark by typing (pcode's `TabProgress` clears the bar on a key press).
struct PCodePaneStateResolver: Equatable {
    var oscTitle = ""
    private(set) var oscActivity: PCodeSessionPane.Activity = .idle
    private(set) var host: PCodeHostStatus?
    /// The host's `turns` when the failed mark was dismissed; it stays down until another turn ends.
    private(set) var dismissedTurns: Int?

    mutating func setOSCActivity(_ activity: PCodeSessionPane.Activity) {
        if oscActivity == .failed, activity == .idle, let host {
            dismissedTurns = host.turns
        }
        oscActivity = activity
    }

    mutating func setHost(_ status: PCodeHostStatus?) {
        if status?.hostID != host?.hostID { dismissedTurns = nil }
        host = status
    }

    var title: String {
        if let host, !host.title.isEmpty { return host.title }
        return oscTitle
    }

    var activity: PCodeSessionPane.Activity {
        guard let host else { return oscActivity }
        if host.isWorking || oscActivity == .running { return .running }
        if host.outcome == "failed", dismissedTurns != host.turns { return .failed }
        return .idle
    }
}
