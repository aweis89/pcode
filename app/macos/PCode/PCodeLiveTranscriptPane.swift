import AppKit
import Combine
import Darwin
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

    private static let maxTailLineBytes = 16 * 1024 * 1024

    @Published private(set) var connectionState: ConnectionState = .idle
    @Published var paceMode: PCodeStreamingPaceMode = PCodeStreamingPaceMode.loadDefaultFromPreferences()

    private let socketClient = PCodeHostSocketClient()
    private var reducer: PCodeTranscriptEventReducer?
    private weak var documentView: PCodeTranscriptDocumentView?
    private var attachedSessionID: String?
    private var attachGeneration: UInt64 = 0

    // Local journal tailing state (fallback when session is not hosted)
    private var tailSource: DispatchSourceFileSystemObject?
    private var tailFD: Int32 = -1
    private var tailOffset: UInt64 = 0
    private var tailInode: UInt64 = 0
    private var tailDev: Int32 = 0
    private var tailLineRemainder = Data()
    private var hostRetryTimer: Timer?

    deinit {
        detach(publishState: false)
    }

    func attach(sessionID: String?, documentView: PCodeTranscriptDocumentView) {
        let normalized = (sessionID?.isEmpty == false) ? sessionID : nil
        if attachedSessionID == normalized && self.documentView === documentView {
            return
        }
        detach(publishState: false)
        attachGeneration &+= 1
        let gen = attachGeneration
        self.documentView = documentView
        self.attachedSessionID = normalized
        documentView.paceMode = paceMode

        let newReducer = PCodeTranscriptEventReducer(documentView: documentView)
        self.reducer = newReducer

        guard let sid = normalized, PCode.valid(sid, length: 1...64) else {
            documentView.setItems([], pinToBottom: true)
            setConnectionState(.waiting)
            return
        }

        if let sockPath = PCodeSessionInfo.hostSocketPath(forSessionID: sid) {
            connectToHost(socketPath: sockPath, sessionID: sid, generation: gen, documentView: documentView, reducer: newReducer)
        } else {
            startJournalTail(sessionID: sid, generation: gen, documentView: documentView, reducer: newReducer)
        }
    }

    func detach(publishState: Bool = true) {
        attachGeneration &+= 1
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        socketClient.close()
        stopJournalTail()
        reducer = nil
        documentView = nil
        attachedSessionID = nil
        if publishState {
            setConnectionState(.idle)
        }
    }

    func applyPaceMode(_ mode: PCodeStreamingPaceMode) {
        paceMode = mode
        documentView?.paceMode = mode
    }

    private func setConnectionState(_ newState: ConnectionState) {
        guard connectionState != newState else { return }
        connectionState = newState
    }

    // MARK: - Hosted Session (Unix Socket Observer)

    private func connectToHost(
        socketPath: String,
        sessionID: String,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        setConnectionState(.waiting)
        socketClient.connect(
            socketPath: socketPath,
            onReady: { [weak self, weak documentView] welcome in
                guard let self, let documentView, self.attachGeneration == generation else { return }
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
                self.setConnectionState(.connectedHost(hostID: welcome.hostID))
            },
            onEvent: { [weak self] event in
                guard let self, self.attachGeneration == generation else { return }
                reducer.apply(event, reveal: true)
            },
            onError: { [weak self, weak documentView] message in
                guard let self, self.attachGeneration == generation else { return }
                if let documentView {
                    self.startJournalTail(sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
                } else {
                    self.setConnectionState(.error(message))
                }
            },
            onClosed: { [weak self, weak documentView] in
                guard let self, self.attachGeneration == generation else { return }
                if let documentView {
                    self.startJournalTail(sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
                } else {
                    self.setConnectionState(.waiting)
                }
            })
    }

    // MARK: - Local Non-Hosted Session (Live Journal Tail)

    private func startJournalTail(
        sessionID: String,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        stopJournalTail()
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl").path
        guard Self.isSafeOwnedDirectory(dir.path) else {
            setConnectionState(.error("Invalid session directory."))
            return
        }

        tailOffset = 0
        tailInode = 0
        tailDev = 0
        tailLineRemainder.removeAll(keepingCapacity: true)

        documentView.setItems([], pinToBottom: true)
        reducer.reset()
        setConnectionState(.tailingJournal)

        // Load settled history off the main thread (skipping historical TextDelta/ThinkingDelta
        // so large sessions never stall the UI), then arm kqueue and drain any tail delta.
        DispatchQueue.global(qos: .userInitiated).async { [weak self, weak documentView] in
            let initial = Self.readSettledInitialJournal(atPath: path)
            DispatchQueue.main.async {
                guard let self, let documentView, self.attachGeneration == generation else { return }
                self.tailOffset = initial.offset
                self.tailInode = initial.inode
                self.tailDev = initial.dev
                for event in initial.events {
                    reducer.apply(event, reveal: false)
                }
                documentView.scrollToBottomAndPin()

                // Arm kqueue watcher first, then drain any bytes written during initial load.
                self.openTailWatcher(atPath: path, sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
                self.readNewJournalBytes(atPath: path, reveal: true, reducer: reducer)
                self.startHostPromotionTimer(atPath: path, sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
            }
        }
    }

    private func openTailWatcher(
        atPath path: String,
        sessionID: String,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        stopTailSourceOnly()
        guard Self.isSafeOwnedRegularFile(path) else { return }
        let fd = Darwin.open(path, O_EVTONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return }
        tailFD = fd
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .rename, .delete],
            queue: .main)
        source.setEventHandler { [weak self, weak documentView] in
            guard let self, let documentView, self.attachGeneration == generation else { return }
            let flags = source.data
            if flags.contains(.rename) || flags.contains(.delete) {
                self.openTailWatcher(atPath: path, sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
            }
            self.readNewJournalBytes(atPath: path, reveal: true, reducer: reducer)
        }
        source.setCancelHandler { [fd] in
            Darwin.close(fd)
        }
        source.resume()
        tailSource = source
    }

    private func startHostPromotionTimer(
        atPath path: String,
        sessionID: String,
        generation: UInt64,
        documentView: PCodeTranscriptDocumentView,
        reducer: PCodeTranscriptEventReducer
    ) {
        hostRetryTimer?.invalidate()
        let timer = Timer(timeInterval: 1.5, repeats: true) { [weak self, weak documentView] t in
            guard let self, let documentView, self.attachGeneration == generation else {
                t.invalidate()
                return
            }
            if let sockPath = PCodeSessionInfo.hostSocketPath(forSessionID: sessionID) {
                t.invalidate()
                self.hostRetryTimer = nil
                self.stopJournalTail()
                self.connectToHost(socketPath: sockPath, sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
                return
            }
            if self.tailSource == nil && Self.isSafeOwnedRegularFile(path) {
                self.openTailWatcher(atPath: path, sessionID: sessionID, generation: generation, documentView: documentView, reducer: reducer)
                self.readNewJournalBytes(atPath: path, reveal: true, reducer: reducer)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        hostRetryTimer = timer
    }

    private func stopTailSourceOnly() {
        if let source = tailSource {
            source.cancel()
            tailSource = nil
            tailFD = -1
        } else if tailFD >= 0 {
            Darwin.close(tailFD)
            tailFD = -1
        }
    }

    private func stopJournalTail() {
        hostRetryTimer?.invalidate()
        hostRetryTimer = nil
        stopTailSourceOnly()
        tailOffset = 0
        tailInode = 0
        tailDev = 0
        tailLineRemainder.removeAll(keepingCapacity: true)
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

    private func readNewJournalBytes(
        atPath path: String,
        reveal: Bool,
        reducer: PCodeTranscriptEventReducer
    ) {
        guard Self.isSafeOwnedRegularFile(path) else { return }
        let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return }
        defer { Darwin.close(fd) }

        var st = stat()
        guard Darwin.fstat(fd, &st) == 0,
              (st.st_mode & S_IFMT) == S_IFREG,
              st.st_uid == Darwin.getuid()
        else { return }

        let fileSize = UInt64(max(0, st.st_size))
        let replaced = (tailInode != 0 && (st.st_ino != tailInode || st.st_dev != tailDev))
        if replaced || fileSize < tailOffset {
            tailOffset = 0
            tailInode = st.st_ino
            tailDev = st.st_dev
            tailLineRemainder.removeAll(keepingCapacity: true)
            reducer.reset()
            documentView?.setItems([], pinToBottom: true)
        } else if tailInode == 0 {
            tailInode = st.st_ino
            tailDev = st.st_dev
        }

        guard fileSize > tailOffset else { return }
        guard Darwin.lseek(fd, off_t(tailOffset), SEEK_SET) >= 0 else { return }

        let toRead = Int(min(fileSize - tailOffset, UInt64(Self.maxTailLineBytes)))
        var chunk = Data(count: toRead)
        var got = 0
        chunk.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress else { return }
            while got < toRead {
                let n = Darwin.read(fd, base.advanced(by: got), toRead - got)
                if n < 0 {
                    if errno == EINTR { continue }
                    break
                }
                if n == 0 { break }
                got += n
            }
        }
        chunk.count = got
        tailOffset += UInt64(got)
        guard !chunk.isEmpty else { return }

        tailLineRemainder.append(chunk)
        if tailLineRemainder.count > Self.maxTailLineBytes {
            tailLineRemainder.removeAll(keepingCapacity: true)
            return
        }

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
        let sid = sessionID
        DispatchQueue.main.async { [weak controller, weak doc = scrollView.transcriptDocumentView] in
            guard let controller, let doc else { return }
            controller.attach(sessionID: sid, documentView: doc)
        }
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
        let sid = sessionID
        if coord.lastRequestedSessionID != sid {
            coord.lastRequestedSessionID = sid
            DispatchQueue.main.async { [weak controller, weak doc = nsView.transcriptDocumentView] in
                guard let controller, let doc else { return }
                controller.attach(sessionID: sid, documentView: doc)
            }
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
