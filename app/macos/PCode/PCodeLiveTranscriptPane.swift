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
        /// The session host stopped or disconnected; polling to reconnect (e.g. across `/restart`).
        case hostStopped(String)
        /// The session host reported a handshake or protocol error.
        case error(String)
    }

    @Published var paceMode: PCodeStreamingPaceMode = PCodeStreamingPaceMode.loadDefaultFromPreferences()
    /// Busy, prompt state, turn outcome and session id as the session host reports them.
    @Published private(set) var sessionState = PCodeHostSessionState()
    /// Host connection lifecycle state for native startup, error and host-stopped cards.
    @Published private(set) var connectionState: ConnectionState = .starting

    private let socketClient = PCodeHostSocketClient()
    private let resolveSocketPath: (String?, String?) -> String?
    private let retryInterval: TimeInterval
    private var reducer: PCodeTranscriptEventReducer?
    private weak var documentView: PCodeTranscriptDocumentView?
    private var attachedSessionID: String?
    private var attachedHostID: String?
    private var connectedSocketPath: String?
    private var isConnectingSocket = false
    private var attachGeneration: UInt64 = 0
    private var hostRetryTimer: Timer?

    init(
        retryInterval: TimeInterval = 0.5,
        resolveSocketPath: @escaping (String?, String?) -> String? = { sessionID, hostID in
            PCodeSessionInfo.hostSocketPath(forSessionID: sessionID, hostID: hostID)
        }
    ) {
        self.retryInterval = retryInterval
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
        connectionState = .starting

        let newReducer = PCodeTranscriptEventReducer(documentView: documentView)
        self.reducer = newReducer

        guard normalizedSession != nil || normalizedHost != nil else {
            documentView.setItems([], pinToBottom: true)
            return
        }

        if let sockPath = resolveSocketPath(normalizedSession, normalizedHost) {
            connectToHost(socketPath: sockPath, generation: gen, documentView: documentView, reducer: newReducer)
        } else {
            loadInitialHistoryAndWaitForHost(
                sessionID: normalizedSession,
                generation: gen,
                documentView: documentView,
                reducer: newReducer)
        }
    }

    func detach() {
        attachGeneration &+= 1
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        socketClient.close()
        connectedSocketPath = nil
        isConnectingSocket = false
        reducer = nil
        documentView = nil
        attachedSessionID = nil
        attachedHostID = nil
        resetSessionState(sessionID: "")
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
        connectedSocketPath = socketPath
        isConnectingSocket = true
        socketClient.connect(
            socketPath: socketPath,
            onReady: { [weak self, weak documentView] welcome in
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.isConnectingSocket = false
                self.connectionState = .connected
                self.paceMode = welcome.pacedScrollback
                documentView.paceMode = welcome.pacedScrollback
                documentView.setItems([], pinToBottom: true)
                reducer.reset()
                for event in welcome.history {
                    reducer.apply(event, reveal: false, isLive: false)
                }
                reducer.settleHistory()
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
                documentView.scrollToBottomAndPin()
            },
            onEvent: { [weak self] event in
                guard let self, self.attachGeneration == generation else { return }
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
                self.startHostReconnectTimer(generation: generation, documentView: documentView, reducer: reducer)
            },
            onClosed: { [weak self, weak documentView] in
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.isConnectingSocket = false
                self.connectedSocketPath = nil
                self.resetSessionState(sessionID: self.attachedSessionID ?? "")
                self.connectionState = .hostStopped("The session host stopped.")
                self.startHostReconnectTimer(generation: generation, documentView: documentView, reducer: reducer)
            })
    }

    // MARK: - Initial History Preload & Host Reconnection

    private func loadInitialHistoryAndWaitForHost(
        sessionID: String?,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        documentView.setItems([], pinToBottom: true)
        reducer.reset()
        startHostReconnectTimer(generation: generation, documentView: documentView, reducer: reducer)

        guard let sessionID else { return }
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl").path
        guard Self.isSafeOwnedDirectory(dir.path) else { return }

        DispatchQueue.global(qos: .userInitiated).async { [weak self, weak documentView] in
            let initial = Self.readSettledInitialJournal(atPath: path)
            DispatchQueue.main.async {
                guard let self, let documentView,
                      self.attachGeneration == generation,
                      self.connectedSocketPath == nil,
                      !self.isConnectingSocket
                else { return }
                for event in initial.events {
                    reducer.apply(event, reveal: false, isLive: false)
                }
                reducer.settleHistory()
                documentView.scrollToBottomAndPin()
            }
        }
    }

    private func startHostReconnectTimer(
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        hostRetryTimer?.invalidate()
        let timer = Timer(timeInterval: retryInterval, repeats: true) { [weak self, weak documentView] t in
            guard let self, let documentView, self.attachGeneration == generation else {
                t.invalidate()
                return
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

        // Decode settled records only (skip TextDelta/ThinkingDelta so initial load is O(N) and fast).
        let skipKinds: Set<String> = ["TextDelta", "ThinkingDelta"]
        var events: [PCodeHostEvent] = []
        var scan = 0
        while scan < data.count, let nl = data[scan...].firstIndex(of: 0x0A) {
            let line = data[scan..<nl]
            scan = nl + 1
            guard !line.isEmpty,
                  let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let kind = obj["kind"] as? String,
                  !skipKinds.contains(kind),
                  let event = PCodeHostEventDecoder.decodeJournalRecord(obj)
            else { continue }
            events.append(event)
        }
        return (events, UInt64(scan), st.st_ino, st.st_dev)
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
        wireFocusHandlers(on: scrollView)
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

            if controller.connectionState != .connected {
                PCodeHostConnectionCard(state: controller.connectionState)
                    .padding(.horizontal, 18)
                    .padding(.bottom, max(12, bottomContentInset + 10))
                    .allowsHitTesting(false)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: controller.connectionState)
        .background(ghostty.config.backgroundColor)
        .environment(\.colorScheme, colorScheme)
    }
}

/// Native status card shown in the transcript pane while a session host is starting,
/// has stopped and is waiting to reconnect (e.g. across `/restart`), or reported an error.
struct PCodeHostConnectionCard: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let state: PCodeLiveTranscriptController.ConnectionState

    var body: some View {
        switch state {
        case .connected:
            EmptyView()
        case .starting:
            card(
                title: "Starting session host…",
                detail: nil,
                isError: false,
                showsSpinner: true)
        case .hostStopped(let message):
            card(
                title: message,
                detail: "Waiting to reconnect…",
                isError: false,
                showsSpinner: true)
        case .error(let message):
            card(
                title: "Session host error",
                detail: message,
                isError: true,
                showsSpinner: false)
        }
    }

    private func card(title: String, detail: String?, isError: Bool, showsSpinner: Bool) -> some View {
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
                        .lineLimit(2)
                }
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
