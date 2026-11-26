import AppKit
import Combine
import Darwin
import Foundation
import GhosttyKit
import SwiftUI

/// Drives the live `PCodeTranscriptDocumentView` that renders a workspace session.
///
/// Connects via `PCodeHostSocketClient` (`observe_transcript` v1) to the session's
/// background host (`pcode --host`), preloading settled `transcript.jsonl` history
/// while a resumed session's host boots and publishing `ConnectionState` so the
/// pane can show native startup, error and host-stopped cards instead of tailing
/// the journal.
final class PCodeLiveTranscriptController: ObservableObject {
    enum ConnectionState: Equatable {
        /// Waiting for the session host to boot and open its socket.
        case starting
        /// Connected to the session host's `observe_transcript` stream.
        case connected
        /// Automatically relaunching a session host that didn't start or stopped unexpectedly.
        case relaunching(attempt: Int, maxAttempts: Int)
        /// The session host stopped or disconnected; polling to reconnect (e.g. across `/restart`).
        case hostStopped(String)
        /// The session host failed to start even after automatic retries; user can click Reopen.
        case failedToStart(String)
        /// The session host reported a handshake or protocol error.
        case error(String)

        var showsAction: Bool {
            switch self {
            case .failedToStart, .error: return true
            case .starting, .connected, .relaunching, .hostStopped: return false
            }
        }
    }

    /// Result of asking the workspace to relaunch a pane's missing or dead host.
    enum RelaunchOutcome: Equatable {
        /// A fresh pane/host was launched; the current controller will be replaced or reconnect.
        case relaunched(attempt: Int, maxAttempts: Int)
        /// The host is still legitimately booting or waiting for user input; keep polling.
        case keepWaiting
        /// Automatic retries are exhausted or relaunch is unsafe (e.g. open in another terminal).
        case failed(String)
    }

    @Published var paceMode: PCodeStreamingPaceMode = PCodeStreamingPaceMode.loadDefaultFromPreferences()
    /// Busy, prompt state, turn outcome and session id as the session host reports them.
    @Published private(set) var sessionState = PCodeHostSessionState()
    /// Host connection lifecycle state for native startup, error and host-stopped cards.
    @Published private(set) var connectionState: ConnectionState = .starting

    /// Asked when the host does not appear in time (or when the user clicks Reopen).
    /// Receives `(isManual, unavailableDuration)` where `unavailableDuration` is how long
    /// the host has been missing/unreachable in this wait cycle.
    var onRequestRelaunch: ((_ isManual: Bool, _ unavailableDuration: TimeInterval) -> RelaunchOutcome)?
    /// Called once connected so the workspace can reset retry counters for this pane.
    var onConnected: (() -> Void)?
    /// Non-zero when this pane was just created by an automatic host relaunch.
    var initialRelaunchAttempt: Int = 0
    var maxRelaunchAttempts: Int = 2

    private let socketClient = PCodeHostSocketClient()
    private let resolveSocketPath: (String?, String?) -> String?
    private let retryInterval: TimeInterval
    private let hostStartTimeout: TimeInterval
    private let hostStoppedTimeout: TimeInterval
    private let now: () -> Date
    private var reducer: PCodeTranscriptEventReducer?
    private weak var documentView: PCodeTranscriptDocumentView?
    private var attachedSessionID: String?
    private var attachedHostID: String?
    private var connectedSocketPath: String?
    /// The settled history the transcript shows now and nothing since, so a host's
    /// welcome carrying the same history needs no redraw. Nil once anything else
    /// (a live event, a catch-up replay) has been drawn on top.
    private var shownHistory: [PCodeHostEvent]?
    /// A host's history has been drawn for this attach: a slower disk preload must not replace it.
    private var hostHistoryDrawn = false
    private var isConnectingSocket = false
    private var attachGeneration: UInt64 = 0
    private var hostRetryTimer: Timer?
    /// When the current host-unavailable wait began (preserved across failed connect retries).
    private var unavailableSince: Date?
    /// When the current timeout window started (reset on `.keepWaiting` / `.relaunched`).
    private var waitStartedAt: Date?
    private var waitingAfterStop = false

    init(
        retryInterval: TimeInterval = 0.5,
        hostStartTimeout: TimeInterval = 10.0,
        hostStoppedTimeout: TimeInterval = 6.0,
        now: @escaping () -> Date = Date.init,
        resolveSocketPath: @escaping (String?, String?) -> String? = { sessionID, hostID in
            PCodeSessionInfo.hostSocketPath(forSessionID: sessionID, hostID: hostID)
        }
    ) {
        self.retryInterval = retryInterval
        self.hostStartTimeout = hostStartTimeout
        self.hostStoppedTimeout = hostStoppedTimeout
        self.now = now
        self.resolveSocketPath = resolveSocketPath
    }

    deinit {
        detach()
    }

    func attach(sessionID: String?, hostID: String? = nil, documentView: PCodeTranscriptDocumentView) {
        let normalizedSession = (sessionID?.isEmpty == false && PCode.valid(sessionID!, length: 1...64)) ? sessionID : nil
        let normalizedHost = (hostID?.isEmpty == false && PCode.valid(hostID!, length: 8...8, hex: true)) ? hostID : nil
        if attachedSessionID == normalizedSession && attachedHostID == normalizedHost && self.documentView === documentView {
            return
        }
        // If a brand-new session gains its session ID on turn 1 (or `/restart` swaps the host ID)
        // while we are already connected to that same host socket, keep the live stream intact.
        if self.documentView === documentView,
           let currentSocket = connectedSocketPath,
           let targetSocket = resolveSocketPath(normalizedSession, normalizedHost),
           currentSocket == targetSocket,
           (connectionState == .connected || isConnectingSocket)
        {
            attachedSessionID = normalizedSession ?? attachedSessionID
            attachedHostID = normalizedHost ?? attachedHostID
            if sessionState.sessionID.isEmpty, let normalizedSession {
                sessionState.sessionID = normalizedSession
            }
            return
        }
        detach()
        attachGeneration &+= 1
        let gen = attachGeneration
        self.documentView = documentView
        self.attachedSessionID = normalizedSession
        self.attachedHostID = normalizedHost
        documentView.paceMode = paceMode
        resetSessionState(sessionID: normalizedSession ?? "")
        connectionState = initialRelaunchAttempt > 0
            ? .relaunching(attempt: initialRelaunchAttempt, maxAttempts: maxRelaunchAttempts)
            : .starting

        let newReducer = PCodeTranscriptEventReducer(documentView: documentView)
        self.reducer = newReducer

        guard normalizedSession != nil || normalizedHost != nil else {
            documentView.setItems([], pinToBottom: true)
            // A brand-new 0-turn pane has no session/host id until pcode boots; still
            // watch for startup timeout so it can auto-relaunch if its host never appears.
            if onRequestRelaunch != nil {
                startHostReconnectTimer(generation: gen, documentView: documentView, reducer: newReducer, afterStop: false)
            }
            return
        }

        documentView.setItems([], pinToBottom: true)
        // Draw the saved conversation straight from disk, as Peek does, even when
        // a host is up: its welcome takes a round trip and a pass over the journal
        // in Python. When it lands with the same history, nothing is redrawn.
        preloadJournal(sessionID: normalizedSession, generation: gen, reducer: newReducer)
        if let sockPath = resolveSocketPath(normalizedSession, normalizedHost) {
            connectToHost(socketPath: sockPath, generation: gen, documentView: documentView, reducer: newReducer)
        } else {
            startHostReconnectTimer(generation: gen, documentView: documentView, reducer: newReducer, afterStop: false)
        }
    }

    func detach() {
        attachGeneration &+= 1
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        unavailableSince = nil
        waitStartedAt = nil
        waitingAfterStop = false
        socketClient.close()
        connectedSocketPath = nil
        isConnectingSocket = false
        shownHistory = nil
        hostHistoryDrawn = false
        reducer = nil
        documentView = nil
        attachedSessionID = nil
        attachedHostID = nil
        resetSessionState(sessionID: "")
    }

    /// Manually reopen/relaunch the session host from the error card's Reopen button.
    func reopenHost() {
        let outcome = triggerRelaunch(isManual: true)
        applyRelaunchOutcome(outcome, isManual: true)
        if hostRetryTimer == nil, !isConnectingSocket, let documentView, let reducer {
            startHostReconnectTimer(
                generation: attachGeneration,
                documentView: documentView,
                reducer: reducer,
                afterStop: waitingAfterStop)
        }
    }

    /// Forget what a host reported, keeping only the attached session id.
    private func resetSessionState(sessionID: String) {
        var state = PCodeHostSessionState()
        state.sessionID = sessionID
        if state != sessionState {
            sessionState = state
        }
    }

    /// Fold a host event into `sessionState`, publishing only real changes.
    private func applySessionEvent(_ event: PCodeHostEvent) {
        var next = sessionState
        if next.apply(event) {
            sessionState = next
            if (attachedSessionID ?? "").isEmpty, !next.sessionID.isEmpty {
                attachedSessionID = next.sessionID
            }
        }
    }

    // MARK: - Hosted Session (Unix Socket Observer)

    private func connectToHost(
        socketPath: String,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        // Keep `unavailableSince` and `waitStartedAt` intact until `onReady` so
        // repeated connect/handshake errors still reach the relaunch timeout.
        connectedSocketPath = socketPath
        isConnectingSocket = true
        socketClient.connect(
            socketPath: socketPath,
            onReady: { [weak self, weak documentView] welcome in
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.isConnectingSocket = false
                self.unavailableSince = nil
                self.waitStartedAt = nil
                self.waitingAfterStop = false
                self.initialRelaunchAttempt = 0
                self.connectionState = .connected
                self.onConnected?()
                self.paceMode = welcome.pacedScrollback
                documentView.paceMode = welcome.pacedScrollback
                self.hostHistoryDrawn = true
                let thinking = welcome.showThinking.map(PCodeThinkingVisibility.isShown(mode:))
                let redraw = Self.welcomeNeedsRedraw(
                    shown: self.shownHistory, welcome: welcome.history,
                    showThinking: thinking, shownThinking: documentView.showThinking)
                if redraw {
                    // `loadHistory` replaces everything; clearing first keeps a `showThinking`
                    // change below from re-projecting rows about to be dropped.
                    documentView.setItems([], pinToBottom: true)
                    if let thinking, thinking != documentView.showThinking {
                        documentView.showThinking = thinking
                    }
                    reducer.loadHistory(welcome.history)
                }
                self.shownHistory = welcome.catchUpCalls.isEmpty ? welcome.history : nil
                var state = PCodeHostSessionState(welcome: welcome)
                for event in welcome.catchUpCalls {
                    state.apply(event)
                    reducer.apply(event, reveal: false, isLive: true)
                }
                if (self.attachedSessionID ?? "").isEmpty, !state.sessionID.isEmpty {
                    self.attachedSessionID = state.sessionID
                }
                if state != self.sessionState {
                    self.sessionState = state
                }
                // Kept rows keep the reader's scroll position; catch-up on top of them
                // follows the bottom only if they were already pinned there.
                if redraw { documentView.scrollToBottomAndPin() }
            },
            onEvent: { [weak self] event in
                guard let self, self.attachGeneration == generation else { return }
                self.shownHistory = nil
                self.applySessionEvent(event)
                reducer.apply(event, reveal: true)
            },
            onError: { [weak self, weak documentView] message in
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.isConnectingSocket = false
                self.connectedSocketPath = nil
                self.resetSessionState(sessionID: self.attachedSessionID ?? "")
                let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
                self.connectionState = .error(text.isEmpty ? "Could not connect to the session host." : text)
                self.startHostReconnectTimer(generation: generation, documentView: documentView, reducer: reducer, afterStop: true)
            },
            onClosed: { [weak self, weak documentView] in
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.isConnectingSocket = false
                self.connectedSocketPath = nil
                self.resetSessionState(sessionID: self.attachedSessionID ?? "")
                self.connectionState = .hostStopped("The session host stopped.")
                self.startHostReconnectTimer(generation: generation, documentView: documentView, reducer: reducer, afterStop: true)
            })
    }

    // MARK: - Initial History Preload & Host Reconnection

    /// Whether a host's welcome must redraw the transcript. Not when it carries the
    /// history already on screen (the disk preload, or the same host after a
    /// reconnect) with the same thinking visibility: every row, its layout and the
    /// scroll position then stay as they are.
    static func welcomeNeedsRedraw(
        shown: [PCodeHostEvent]?, welcome: [PCodeHostEvent],
        showThinking: Bool?, shownThinking: Bool
    ) -> Bool {
        shown != welcome || (showThinking != nil && showThinking != shownThinking)
    }

    /// Draw `sessionID`'s settled journal from disk unless a host's history beats it here.
    private func preloadJournal(
        sessionID: String?,
        generation: UInt64,
        reducer: PCodeTranscriptEventReducer
    ) {
        guard let sessionID else { return }
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl").path
        guard Self.isSafeOwnedDirectory(dir.path) else { return }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let initial = Self.readSettledInitialJournal(atPath: path)
            DispatchQueue.main.async {
                guard let self, self.attachGeneration == generation, !self.hostHistoryDrawn else { return }
                reducer.loadHistory(initial.events)
                self.shownHistory = initial.events
            }
        }
    }

    private func startHostReconnectTimer(
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer,
        afterStop: Bool
    ) {
        hostRetryTimer?.invalidate()
        waitingAfterStop = afterStop
        if case .failedToStart = connectionState {
            waitStartedAt = nil
        } else {
            let current = now()
            if unavailableSince == nil { unavailableSince = current }
            if waitStartedAt == nil { waitStartedAt = current }
        }
        let timer = Timer(timeInterval: retryInterval, repeats: true) { [weak self, weak documentView] t in
            guard let self, let documentView, self.attachGeneration == generation else {
                t.invalidate()
                return
            }
            if let startedAt = self.waitStartedAt {
                let timeout = self.waitingAfterStop ? self.hostStoppedTimeout : self.hostStartTimeout
                if self.now().timeIntervalSince(startedAt) >= timeout {
                    let outcome = self.triggerRelaunch(isManual: false)
                    self.applyRelaunchOutcome(outcome, isManual: false)
                    if case .failed = outcome { return }
                    if case .relaunched = outcome { return }
                }
            }
            if let sockPath = self.resolveSocketPath(self.attachedSessionID, self.attachedHostID) {
                t.invalidate()
                self.hostRetryTimer = nil
                self.connectToHost(socketPath: sockPath, generation: generation, documentView: documentView, reducer: reducer)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        hostRetryTimer = timer
    }

    private func triggerRelaunch(isManual: Bool) -> RelaunchOutcome {
        if let onRequestRelaunch {
            let duration = unavailableSince.map { max(0, now().timeIntervalSince($0)) } ?? 0
            return onRequestRelaunch(isManual, duration)
        }
        return .failed("The session host didn't start in time.")
    }

    private func applyRelaunchOutcome(_ outcome: RelaunchOutcome, isManual: Bool) {
        switch outcome {
        case .relaunched(let attempt, let maxAttempts):
            waitingAfterStop = false
            let current = now()
            unavailableSince = current
            waitStartedAt = current
            connectionState = .relaunching(attempt: attempt, maxAttempts: maxAttempts)
        case .keepWaiting:
            waitStartedAt = now()
            if isManual {
                waitingAfterStop = false
                connectionState = .starting
            }
        case .failed(let detail):
            waitStartedAt = nil
            connectionState = .failedToStart(detail)
        }
    }

    private static func isSafeOwnedDirectory(_ path: String) -> Bool {
        var st = stat()
        guard Darwin.lstat(path, &st) == 0 else { return false }
        return (st.st_mode & S_IFMT) == S_IFDIR && st.st_uid == Darwin.getuid()
    }

    private static func isSafeOwnedRegularFile(_ path: String) -> Bool {
        var st = stat()
        guard Darwin.lstat(path, &st) == 0 else { return false }
        return (st.st_mode & S_IFMT) == S_IFREG && st.st_uid == Darwin.getuid()
    }

    /// Journal kinds the transcript never draws (the host's `OBSERVER_SKIPPED_KINDS`).
    static let undrawnJournalKinds: Set<String> = [
        "TextDelta", "ThinkingDelta", "RunStatus", "PlanUpdated", "PlanPreview", "ChildText",
        "ChildPlan", "WorkerStatus", "CacheBust", "compaction_checkpoint", "request", "response",
    ]
    private static let journalKindPrefix = Data("\"kind\": \"".utf8)

    /// A journal line's kind without parsing it: pcode writes `"kind": "..."` right after
    /// the version and time, so it is in the first couple of hundred bytes.
    static func sniffJournalKind(_ line: Data) -> String? {
        let window = line.prefix(200)
        guard let start = window.range(of: journalKindPrefix)?.upperBound,
              let end = window[start...].firstIndex(of: 0x22)
        else { return nil }
        return String(data: window[start..<end], encoding: .utf8)
    }

    private static func readSettledInitialJournal(
        atPath path: String
    ) -> (events: [PCodeHostEvent], offset: UInt64, inode: UInt64, dev: Int32) {
        guard isSafeOwnedRegularFile(path) else { return ([], 0, 0, 0) }
        let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return ([], 0, 0, 0) }
        defer { Darwin.close(fd) }

        var st = stat()
        guard Darwin.fstat(fd, &st) == 0,
              (st.st_mode & S_IFMT) == S_IFREG,
              st.st_uid == Darwin.getuid()
        else { return ([], 0, 0, 0) }

        let size = max(0, Int(st.st_size))
        guard size > 0 else { return ([], 0, st.st_ino, st.st_dev) }

        var data = Data(count: size)
        var totalRead = 0
        data.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress else { return }
            while totalRead < size {
                let n = Darwin.read(fd, base.advanced(by: totalRead), size - totalRead)
                if n < 0 {
                    if errno == EINTR { continue }
                    break
                }
                if n == 0 { break }
                totalRead += n
            }
        }
        data.count = totalRead

        // Decode settled records only, skipping the deltas and status ticks that are most of a
        // long journal before parsing them (as the host's `observer_history` does).
        let (events, scan) = settledEvents(inJournal: data)
        return (events, UInt64(scan), st.st_ino, st.st_dev)
    }

    /// The selected branch's drawn events in `data` and the end of its last whole line,
    /// as the host's `observer_history` sends them, so a host's welcome can match it.
    static func settledEvents(inJournal data: Data) -> (events: [PCodeHostEvent], end: Int) {
        // Decode settled records only, skipping the deltas and status ticks that are most of a
        // long journal before parsing them (as the host's `observer_history` does).
        var drawn: [(event: PCodeHostEvent, turn: String?)] = []
        var branches = PCodeJournalBranches()
        var recording: String?
        var scan = 0
        while scan < data.count, let nl = data[scan...].firstIndex(of: 0x0A) {
            let line = data[scan..<nl]
            scan = nl + 1
            let kind = sniffJournalKind(line)
            if let kind, undrawnJournalKinds.contains(kind) {
                // Never drawn, but a compaction moves the selected branch.
                if kind == "compaction_checkpoint",
                   let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                    branches.consume(obj)
                }
                continue
            }
            guard !line.isEmpty,
                  let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any]
            else { continue }
            branches.consume(obj)
            if PCodeJournalBranches.isMainTurnStart(obj) {
                recording = obj["run_id"] as? String
            }
            guard let event = PCodeHostEventDecoder.decodeJournalRecord(obj) else { continue }
            drawn.append((event, (obj["run_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? recording))
        }
        guard let selected = branches.selectedTurns() else { return (drawn.map(\.event), scan) }
        return (drawn.filter { $0.turn.map(selected.contains) == true }.map(\.event), scan)
    }
}

/// Which turns a journal's selected `/tree` branch holds: pcode's `ConversationTree`,
/// reduced to the parent links and the active node that `observer_history` filters by.
struct PCodeJournalBranches {
    private var parents: [String: String?] = [:]
    private var active: String?

    /// A turn of the conversation itself, not a `/btw` side question (`side_record`).
    static func isMainTurnStart(_ record: [String: Any]) -> Bool {
        guard record["kind"] as? String == "turn_started" else { return false }
        switch record["side"] {
        case nil, is NSNull: return true
        case let side as [String: Any]: return side.isEmpty
        case let side as Bool: return !side
        case let side as String: return side.isEmpty
        default: return false
        }
    }

    mutating func consume(_ record: [String: Any]) {
        switch record["kind"] as? String {
        case "turn_started":
            guard let id = record["run_id"] as? String, !id.isEmpty else { return }
            // Absent: an old, linear session. Null: a root.
            let parent: String? = record.keys.contains("parent_id") ? record["parent_id"] as? String : active
            parents[id] = parent
            if Self.isMainTurnStart(record) { active = id }
        case "compaction_checkpoint":
            guard let id = record["node_id"] as? String else { return }
            parents[id] = record["parent_id"] as? String
            active = id
        case "tree_selected":
            if let id = record["node_id"] as? String { active = id }
        default:
            break
        }
    }

    /// The selected branch's turns, or nil for a journal with no turns to choose between.
    func selectedTurns() -> Set<String>? {
        guard !parents.isEmpty else { return nil }
        var path: Set<String> = []
        var node = active
        while let id = node, path.insert(id).inserted {
            node = parents[id] ?? nil
        }
        return path
    }
}

/// SwiftUI `NSViewRepresentable` hosting `PCodeTranscriptScrollView` connected to
/// `PCodeLiveTranscriptController`.
struct PCodeLiveTranscriptRepresentable: NSViewRepresentable {
    let sessionID: String?
    var hostID: String? = nil
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    let controller: PCodeLiveTranscriptController
    var bottomContentInset: CGFloat = 0
    weak var composerSurface: Ghostty.SurfaceView?

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeTranscriptScrollView {
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        scrollView.bottomContentInset = bottomContentInset
        // So a live peek of this pane scrolls this transcript, not another pane's.
        composerSurface?.pcodeTranscriptScrollView = scrollView
        wireFocusHandlers(on: scrollView)
        wireWorkspaceRecovery()
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        context.coordinator.lastTypographyObjectID = ObjectIdentifier(typography)
        context.coordinator.lastRequestedSessionID = sessionID
        context.coordinator.lastRequestedHostID = hostID
        context.coordinator.controller = controller
        let sid = sessionID
        let hid = hostID
        DispatchQueue.main.async { [weak controller, weak doc = scrollView.transcriptDocumentView] in
            guard let controller, let doc else { return }
            controller.attach(sessionID: sid, hostID: hid, documentView: doc)
        }
        return scrollView
    }

    func updateNSView(_ nsView: PCodeTranscriptScrollView, context: Context) {
        let coord = context.coordinator
        let snap = typography.snapshot
        let typoID = ObjectIdentifier(typography)

        nsView.bottomContentInset = bottomContentInset
        wireFocusHandlers(on: nsView)
        wireWorkspaceRecovery()

        if coord.lastTypographyObjectID != typoID {
            coord.lastTypographyObjectID = typoID
            nsView.subscribeTypography(typography)
        }
        if coord.lastTheme != theme || coord.lastTypographySnapshot != snap {
            coord.lastTheme = theme
            coord.lastTypographySnapshot = snap
            nsView.transcriptDocumentView.updateAppearance(typography: snap, theme: theme)
            nsView.applyTheme(theme)
        }
        let sid = sessionID
        let hid = hostID
        if coord.lastRequestedSessionID != sid || coord.lastRequestedHostID != hid {
            coord.lastRequestedSessionID = sid
            coord.lastRequestedHostID = hid
            DispatchQueue.main.async { [weak controller, weak doc = nsView.transcriptDocumentView] in
                guard let controller, let doc else { return }
                controller.attach(sessionID: sid, hostID: hid, documentView: doc)
            }
        }
    }

    private func wireWorkspaceRecovery() {
        weak var composer = composerSurface
        controller.initialRelaunchAttempt = composer?.pcodeHostRelaunchAttempt ?? 0
        controller.maxRelaunchAttempts = PCodeWorkspace.maxHostRelaunchAttempts
        controller.onRequestRelaunch = { isManual, unavailableDuration in
            guard let composer, let workspace = PCodeWorkspaceController.existing?.workspace else {
                return .failed("The session host didn't start in time.")
            }
            return workspace.handleHostUnavailable(
                for: composer,
                isManual: isManual,
                unavailableDuration: unavailableDuration)
        }
        controller.onConnected = {
            guard let composer else { return }
            PCodeWorkspaceController.existing?.workspace.noteHostConnected(for: composer)
        }
    }

    private func wireFocusHandlers(on scrollView: PCodeTranscriptScrollView) {
        weak var composer = composerSurface
        scrollView.transcriptDocumentView.onReturnFocusToComposer = {
            guard let composer else { return }
            Ghostty.moveFocus(to: composer)
        }
        scrollView.composerHasSelection = {
            composer?.surface.map(ghostty_surface_has_selection) ?? false
        }
        scrollView.canHandleFindShortcut = { [weak scrollView, weak composer] in
            guard let scrollView,
                  !scrollView.isHiddenOrHasHiddenAncestor,
                  let window = scrollView.window,
                  window.isKeyWindow,
                  let responder = window.firstResponder as? NSView
            else { return false }
            if responder === scrollView || responder.isDescendant(of: scrollView) {
                return true
            }
            if let composer, responder === composer || responder.isDescendant(of: composer) {
                return true
            }
            return false
        }
        scrollView.isPaneActiveProvider = { [weak scrollView, weak composer] in
            guard let scrollView,
                  !scrollView.isHiddenOrHasHiddenAncestor,
                  let window = scrollView.window,
                  window.isKeyWindow,
                  window.occlusionState.contains(.visible),
                  let responder = window.firstResponder as? NSView
            else { return false }
            if responder === scrollView || responder.isDescendant(of: scrollView) {
                return true
            }
            if let composer, responder === composer || responder.isDescendant(of: composer) {
                return true
            }
            return false
        }
        scrollView.refreshPaneActiveState()
    }

    static func dismantleNSView(_ nsView: PCodeTranscriptScrollView, coordinator: Coordinator) {
        let controller = coordinator.controller
        DispatchQueue.main.async {
            controller?.detach()
        }
    }

    final class Coordinator {
        var lastTheme: PCodeTranscriptTheme?
        var lastTypographySnapshot: PCodeTypographySnapshot?
        var lastTypographyObjectID: ObjectIdentifier?
        var lastRequestedSessionID: String?
        var lastRequestedHostID: String?
        weak var controller: PCodeLiveTranscriptController?
    }
}

/// Main-window live AppKit transcript layer mounted over the Ghostty scrollback surface
/// and underneath `PCodeDockCard` and the `/btw` drawer.
struct PCodeMainWindowTranscriptView: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let sessionID: String?
    var hostID: String? = nil
    let bottomContentInset: CGFloat
    var composerSurface: Ghostty.SurfaceView? = nil
    /// The pane's boot card is showing: it already says the session is starting.
    var booting = false
    @ObservedObject private var typography = PCodeTypography.shared
    @StateObject private var controller = PCodeLiveTranscriptController()

    private var transcriptTheme: PCodeTranscriptTheme {
        PCodeTranscriptTheme.make(
            background: NSColor(ghostty.config.backgroundColor),
            foreground: NSColor(ghostty.config.foregroundColor))
    }

    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            PCodeLiveTranscriptRepresentable(
                sessionID: sessionID,
                hostID: hostID,
                theme: transcriptTheme,
                typography: typography,
                controller: controller,
                bottomContentInset: bottomContentInset,
                composerSurface: composerSurface)
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            if bottomContentInset > 0 {
                // Soft vertical fade confined to the composer inset band (0..<bottomContentInset)
                // so scrolling lines melt behind the floating dock without veiling the
                // Jump-to-bottom button or overlapping `PCodeBottomDissolveView`.
                let fadeHeight = min(16, bottomContentInset)
                VStack(spacing: 0) {
                    LinearGradient(
                        colors: [
                            ghostty.config.backgroundColor.opacity(0),
                            ghostty.config.backgroundColor.opacity(0.94),
                        ],
                        startPoint: .top,
                        endPoint: .bottom)
                        .frame(height: fadeHeight)
                    ghostty.config.backgroundColor.opacity(0.94)
                        .frame(height: max(0, bottomContentInset - fadeHeight))
                }
                .allowsHitTesting(false)
            }

            if controller.connectionState != .connected && !(booting && controller.connectionState == .starting) {
                PCodeHostConnectionCard(
                    state: controller.connectionState,
                    onReopen: { controller.reopenHost() })
                    .padding(.horizontal, 18)
                    .padding(.bottom, max(12, bottomContentInset + 10))
                    .allowsHitTesting(controller.connectionState.showsAction)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: controller.connectionState)
        .background(ghostty.config.backgroundColor)
        .environment(\.colorScheme, colorScheme)
    }
}

/// Native status card shown in the transcript pane while a session host is starting,
/// relaunching, has stopped and is waiting to reconnect (e.g. across `/restart`), or failed.
struct PCodeHostConnectionCard: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let state: PCodeLiveTranscriptController.ConnectionState
    var onReopen: (() -> Void)? = nil

    var body: some View {
        switch state {
        case .connected:
            EmptyView()
        case .starting:
            card(
                title: "Starting session host…",
                detail: nil,
                isError: false,
                showsSpinner: true,
                showsReopen: false)
        case .relaunching(let attempt, _):
            card(
                title: "Restarting session host…",
                detail: attempt > 1 ? "Attempt \(attempt)" : nil,
                isError: false,
                showsSpinner: true,
                showsReopen: false)
        case .hostStopped(let message):
            card(
                title: message,
                detail: "Waiting to reconnect…",
                isError: false,
                showsSpinner: true,
                showsReopen: false)
        case .failedToStart(let message):
            card(
                title: "Session host didn't start",
                detail: message,
                isError: true,
                showsSpinner: false,
                showsReopen: true)
        case .error(let message):
            card(
                title: "Session host error",
                detail: message,
                isError: true,
                showsSpinner: false,
                showsReopen: true)
        }
    }

    private func card(
        title: String,
        detail: String?,
        isError: Bool,
        showsSpinner: Bool,
        showsReopen: Bool
    ) -> some View {
        HStack(spacing: 10) {
            if showsSpinner {
                ProgressView()
                    .controlSize(.small)
            } else if isError {
                Circle()
                    .fill(Color.red)
                    .frame(width: 7, height: 7)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(isError ? Color.red : ghostty.config.foregroundColor.opacity(0.88))
                    .lineLimit(1)
                if let detail, !detail.isEmpty {
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(ghostty.config.foregroundColor.opacity(0.58))
                        .lineLimit(4)
                }
            }
            if showsReopen, let onReopen {
                Button("Reopen", action: onReopen)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 9)
                .fill(ghostty.config.backgroundColor)
                .shadow(color: .black.opacity(0.22), radius: 10, x: 0, y: 3)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .strokeBorder(
                    isError ? Color.red.opacity(0.45) : ghostty.config.splitDividerColor,
                    lineWidth: 1)
        )
    }
}
