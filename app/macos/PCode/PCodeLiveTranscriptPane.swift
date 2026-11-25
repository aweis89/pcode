import AppKit
import Combine
import Foundation
import SwiftUI

/// Drives a live `PCodeTranscriptDocumentView` alongside the Ghostty terminal for the
/// currently selected workspace session.
///
/// Connects via `PCodeHostSocketClient` (`observe_transcript` v1) whenever a running
/// session host owns the session, and falls back to live-tailing
/// `~/.local/state/pcode/sessions/<id>/transcript.jsonl` for local non-hosted sessions.
final class PCodeLiveTranscriptController: ObservableObject {
    enum ConnectionState: Equatable {
        case idle
        case waiting
        case connectedHost(hostID: String)
        case tailingJournal
        case error(String)

        var badgeText: String {
            switch self {
            case .idle, .waiting: return "Waiting…"
            case .connectedHost: return "Live · Host"
            case .tailingJournal: return "Live · Journal"
            case .error: return "Disconnected"
            }
        }

        var isLive: Bool {
            switch self {
            case .connectedHost, .tailingJournal: return true
            default: return false
            }
        }
    }

    @Published private(set) var connectionState: ConnectionState = .idle
    @Published var paceMode: PCodeStreamingPaceMode = PCodeStreamingPaceMode.loadDefaultFromPreferences()

    private let socketClient = PCodeHostSocketClient()
    private var reducer: PCodeTranscriptEventReducer?
    private weak var documentView: PCodeTranscriptDocumentView?
    private var attachedSessionID: String?

    // Local journal tailing state (fallback when session is not hosted)
    private var tailSource: DispatchSourceFileSystemObject?
    private var tailFD: Int32 = -1
    private var tailOffset: UInt64 = 0
    private var tailLineRemainder = Data()
    private var tailSessionPath: String?
    private var hostRetryTimer: Timer?

    deinit {
        detach()
    }

    func attach(sessionID: String?, documentView: PCodeTranscriptDocumentView) {
        let normalized = (sessionID?.isEmpty == false) ? sessionID : nil
        if attachedSessionID == normalized && self.documentView === documentView {
            return
        }
        detach()
        self.documentView = documentView
        self.attachedSessionID = normalized
        documentView.paceMode = paceMode

        let newReducer = PCodeTranscriptEventReducer(documentView: documentView)
        self.reducer = newReducer

        guard let sid = normalized, PCode.valid(sid, length: 1...64) else {
            documentView.setItems([], pinToBottom: true)
            connectionState = .waiting
            return
        }

        if let sockPath = PCodeSessionInfo.hostSocketPath(forSessionID: sid) {
            connectToHost(socketPath: sockPath, sessionID: sid, documentView: documentView, reducer: newReducer)
        } else {
            startJournalTail(sessionID: sid, documentView: documentView, reducer: newReducer)
        }
    }

    func detach() {
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        socketClient.close()
        stopJournalTail()
        reducer = nil
        attachedSessionID = nil
        connectionState = .idle
    }

    func applyPaceMode(_ mode: PCodeStreamingPaceMode) {
        paceMode = mode
        documentView?.paceMode = mode
    }

    // MARK: - Hosted Session (Unix Socket Observer)

    private func connectToHost(
        socketPath: String,
        sessionID: String,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        connectionState = .waiting
        socketClient.connect(
            socketPath: socketPath,
            onReady: { [weak self, weak documentView] welcome in
                guard let self, let documentView, self.attachedSessionID == sessionID else { return }
                self.paceMode = welcome.pacedScrollback
                documentView.paceMode = welcome.pacedScrollback
                documentView.setItems([], pinToBottom: true)
                reducer.reset()
                for event in welcome.history {
                    reducer.apply(event, reveal: false)
                }
                for event in welcome.catchUpCalls {
                    reducer.apply(event, reveal: false)
                }
                documentView.scrollToBottomAndPin()
                self.connectionState = .connectedHost(hostID: welcome.hostID)
            },
            onEvent: { [weak self] event in
                guard let self, self.attachedSessionID == sessionID else { return }
                reducer.apply(event, reveal: true)
            },
            onError: { [weak self, weak documentView] message in
                guard let self, self.attachedSessionID == sessionID else { return }
                // Fall back cleanly to journal tailing if host socket closed or refused.
                if let documentView {
                    self.startJournalTail(sessionID: sessionID, documentView: documentView, reducer: reducer)
                } else {
                    self.connectionState = .error(message)
                }
            },
            onClosed: { [weak self, weak documentView] in
                guard let self, self.attachedSessionID == sessionID else { return }
                if let documentView {
                    self.startJournalTail(sessionID: sessionID, documentView: documentView, reducer: reducer)
                } else {
                    self.connectionState = .waiting
                }
            })
    }

    // MARK: - Local Non-Hosted Session (Live Journal Tail)

    private func startJournalTail(
        sessionID: String,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        stopJournalTail()
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl").path
        tailSessionPath = path
        tailOffset = 0
        tailLineRemainder.removeAll(keepingCapacity: true)

        documentView.setItems([], pinToBottom: true)
        reducer.reset()
        connectionState = .tailingJournal

        // Initial settled load (no reveal animation).
        readNewJournalBytes(atPath: path, reveal: false, reducer: reducer)
        documentView.scrollToBottomAndPin()

        // Watch for appends via kqueue (`DispatchSourceFileSystemObject`) + light poll fallback
        // for newly created files or host promotion.
        openTailWatcher(atPath: path, sessionID: sessionID, documentView: documentView, reducer: reducer)
    }

    private func openTailWatcher(
        atPath path: String,
        sessionID: String,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        let fd = Darwin.open(path, O_EVTONLY)
        if fd >= 0 {
            tailFD = fd
            let source = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: fd,
                eventMask: [.write, .extend, .rename, .delete],
                queue: .main)
            source.setEventHandler { [weak self] in
                guard let self, self.attachedSessionID == sessionID else { return }
                self.readNewJournalBytes(atPath: path, reveal: true, reducer: reducer)
            }
            source.setCancelHandler { [fd] in
                Darwin.close(fd)
            }
            source.resume()
            tailSource = source
        } else {
            // File may not exist yet on a brand-new session; poll briefly until created or hosted.
            let timer = Timer(timeInterval: 0.75, repeats: true) { [weak self, weak documentView] _ in
                guard let self, let documentView, self.attachedSessionID == sessionID else { return }
                if let sockPath = PCodeSessionInfo.hostSocketPath(forSessionID: sessionID) {
                    self.stopJournalTail()
                    self.connectToHost(socketPath: sockPath, sessionID: sessionID, documentView: documentView, reducer: reducer)
                    return
                }
                if FileManager.default.fileExists(atPath: path) {
                    self.hostRetryTimer?.invalidate()
                    self.hostRetryTimer = nil
                    self.readNewJournalBytes(atPath: path, reveal: true, reducer: reducer)
                    self.openTailWatcher(atPath: path, sessionID: sessionID, documentView: documentView, reducer: reducer)
                }
            }
            RunLoop.main.add(timer, forMode: .common)
            hostRetryTimer = timer
        }
    }

    private func stopJournalTail() {
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        if let source = tailSource {
            source.cancel()
            tailSource = nil
            tailFD = -1
        } else if tailFD >= 0 {
            Darwin.close(tailFD)
            tailFD = -1
        }
        tailOffset = 0
        tailLineRemainder.removeAll(keepingCapacity: true)
        tailSessionPath = nil
    }

    private func readNewJournalBytes(
        atPath path: String,
        reveal: Bool,
        reducer: PCodeTranscriptEventReducer
    ) {
        guard let handle = FileHandle(forReadingAtPath: path) else { return }
        defer { try? handle.close() }
        do {
            let endOffset = try handle.seekToEnd()
            if endOffset < tailOffset {
                // File truncated/rewritten; restart from 0.
                tailOffset = 0
                tailLineRemainder.removeAll(keepingCapacity: true)
                reducer.reset()
                documentView?.setItems([], pinToBottom: true)
            }
            guard endOffset > tailOffset else { return }
            try handle.seek(toOffset: tailOffset)
            let chunk = try handle.readToEnd() ?? Data()
            tailOffset += UInt64(chunk.count)
            guard !chunk.isEmpty else { return }

            tailLineRemainder.append(chunk)
            var scan = 0
            while scan < tailLineRemainder.count,
                  let nl = tailLineRemainder[scan...].firstIndex(of: 0x0A)
            {
                let line = tailLineRemainder[scan..<nl]
                scan = nl + 1
                guard !line.isEmpty,
                      let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                      let event = PCodeHostEventDecoder.decodeJournalRecord(obj)
                else { continue }
                reducer.apply(event, reveal: reveal)
            }
            if scan > 0 {
                tailLineRemainder.removeSubrange(0..<scan)
            }
        } catch {
            // Ignore transient read errors while pcode is writing.
        }
    }
}

/// SwiftUI `NSViewRepresentable` hosting `PCodeTranscriptScrollView` connected to
/// `PCodeLiveTranscriptController`.
private struct PCodeLiveTranscriptRepresentable: NSViewRepresentable {
    let sessionID: String?
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    let controller: PCodeLiveTranscriptController

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeTranscriptScrollView {
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        context.coordinator.lastTypographyObjectID = ObjectIdentifier(typography)
        context.coordinator.controller = controller
        controller.attach(sessionID: sessionID, documentView: scrollView.transcriptDocumentView)
        return scrollView
    }

    func updateNSView(_ nsView: PCodeTranscriptScrollView, context: Context) {
        let coord = context.coordinator
        let snap = typography.snapshot
        let typoID = ObjectIdentifier(typography)

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
        controller.attach(sessionID: sessionID, documentView: nsView.transcriptDocumentView)
    }

    static func dismantleNSView(_ nsView: PCodeTranscriptScrollView, coordinator: Coordinator) {
        coordinator.controller?.detach()
    }

    final class Coordinator {
        var lastTheme: PCodeTranscriptTheme?
        var lastTypographySnapshot: PCodeTypographySnapshot?
        var lastTypographyObjectID: ObjectIdentifier?
        weak var controller: PCodeLiveTranscriptController?
    }
}

/// Non-destructive live AppKit transcript split pane shown alongside the Ghostty terminal.
struct PCodeLiveTranscriptPaneView: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject private var typography = PCodeTypography.shared
    @StateObject private var controller = PCodeLiveTranscriptController()
    let onClose: () -> Void

    private var transcriptTheme: PCodeTranscriptTheme {
        PCodeTranscriptTheme.make(
            background: NSColor(ghostty.config.backgroundColor),
            foreground: NSColor(ghostty.config.foregroundColor))
    }

    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            PCodeLiveTranscriptRepresentable(
                sessionID: pane.sessionID,
                theme: transcriptTheme,
                typography: typography,
                controller: controller)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(ghostty.config.backgroundColor)
        .environment(\.colorScheme, colorScheme)
        .onDisappear {
            controller.detach()
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(controller.connectionState.isLive ? Color.green : Color.secondary.opacity(0.5))
                .frame(width: 7, height: 7)

            Text("Transcript")
                .font(.caption.weight(.semibold))
                .lineLimit(1)

            Text(controller.connectionState.badgeText)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Spacer(minLength: 4)

            // Pace mode cycle button: Typed / Rows / Off
            Button {
                let all = PCodeStreamingPaceMode.allCases
                if let idx = all.firstIndex(of: controller.paceMode) {
                    controller.applyPaceMode(all[(idx + 1) % all.count])
                }
            } label: {
                Text(controller.paceMode.rawValue.capitalized)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.primary.opacity(0.85))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.primary.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
            }
            .buttonStyle(.plain)
            .help("Cycle reveal mode: Typed (ink-sweep) / Rows (curtain) / Off")

            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 20, height: 20)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Close Live Transcript pane (⌥⌘T)")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }
}
