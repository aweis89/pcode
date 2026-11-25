import AppKit
import SwiftUI

/// Which session the Peek overlay is previewing.
struct PCodePeekTarget: Equatable {
    let sessionID: String?
    let title: String
    let subtitle: String?
    let directory: String
    let turnsHint: Int?
    let updatedDate: Date?
    let isOpen: Bool
    let paneID: UUID?
    let info: PCodeSessionInfo?

    static func == (lhs: PCodePeekTarget, rhs: PCodePeekTarget) -> Bool {
        if lhs.isOpen || rhs.isOpen {
            return lhs.isOpen == rhs.isOpen && lhs.paneID == rhs.paneID
        }
        return lhs.sessionID == rhs.sessionID
    }
}

/// One settled tool call summarized in a session transcript.
struct PCodePeekToolCall: Identifiable {
    let id: Int
    let name: String
    let detail: String
    let failed: Bool
    let elapsedSeconds: Double?
    let command: String

    var summaryText: String {
        if !detail.isEmpty { return detail }
        if !command.isEmpty { return command }
        return ""
    }

    var formattedElapsed: String? {
        guard let elapsed = elapsedSeconds, elapsed >= 0.05 else { return nil }
        if elapsed < 10 { return String(format: "%.1fs", elapsed) }
        return "\(Int(elapsed.rounded()))s"
    }
}

/// One ordered block inside a turn: assistant markdown or a tool summary.
enum PCodePeekBlock: Identifiable {
    /// A whole assistant message, rendered as one `StructuredText` so selection spans its blocks.
    case markdown(id: Int, text: String)
    case tool(PCodePeekToolCall)

    var id: String {
        switch self {
        case .markdown(let id, _): return "m:\(id)"
        case .tool(let call): return "t:\(call.id)"
        }
    }
}

/// One prompt and its response/tool calls from `transcript.jsonl`.
struct PCodePeekTurn: Identifiable {
    enum Status: String {
        case complete
        case cancelled
        case failed
        case running
    }

    let id: Int
    let prompt: String
    var blocks: [PCodePeekBlock]
    var status: Status
    let side: Bool
}

/// Lock-free, cached reader for `~/.local/state/pcode/sessions/<id>/transcript.jsonl`.
///
/// Mirrors `sessions.session_turns()` in pcode: pre-filters lines by the 6 turn-level
/// record markers before JSON parsing so streaming deltas are skipped at raw byte speed.
final class PCodeTranscriptLoader {
    static let shared = PCodeTranscriptLoader()

    private struct CacheEntry {
        let mtime: Date
        let size: UInt64
        let turns: [PCodePeekTurn]
    }

    private let lock = NSLock()
    private var cache: [String: CacheEntry] = [:]
    private var cacheOrder: [String] = []
    private static let maxCacheEntries = 32

    private static let kindMarkers: [Data] = [
        Data("\"turn_started\"".utf8),
        Data("\"Message\"".utf8),
        Data("\"ToolSummary\"".utf8),
        Data("\"turn_completed\"".utf8),
        Data("\"turn_cancelled\"".utf8),
        Data("\"turn_failed\"".utf8),
    ]

    private init() {}

    func load(sessionID: String, completion: @escaping (Result<[PCodePeekTurn], Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.loadSync(sessionID: sessionID)
            DispatchQueue.main.async { completion(result) }
        }
    }

    private enum LoadError: LocalizedError {
        case invalidSessionID
        case missingTranscript
        case symlinkedSession

        var errorDescription: String? {
            switch self {
            case .invalidSessionID: return "Invalid session ID."
            case .missingTranscript: return "No transcript file found for this session."
            case .symlinkedSession: return "Refusing to read a symlinked session."
            }
        }
    }

    private func loadSync(sessionID: String) -> Result<[PCodePeekTurn], Error> {
        guard PCode.valid(sessionID, length: 1...64) else {
            return .failure(LoadError.invalidSessionID)
        }
        let dir = PCodeSessionInfo.root.appendingPathComponent(sessionID)
        let path = dir.appendingPathComponent("transcript.jsonl")

        if isSymlink(dir) || isSymlink(path) {
            return .failure(LoadError.symlinkedSession)
        }

        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
              let mtime = attrs[.modificationDate] as? Date,
              let size = (attrs[.size] as? NSNumber)?.uint64Value
        else {
            return .failure(LoadError.missingTranscript)
        }

        if let cached = cachedTurns(for: sessionID, mtime: mtime, size: size) {
            return .success(cached)
        }

        do {
            let data = try Data(contentsOf: path, options: .mappedIfSafe)
            let turns = Self.parseTranscript(data)
            storeCache(sessionID: sessionID, mtime: mtime, size: size, turns: turns)
            return .success(turns)
        } catch {
            return .failure(error)
        }
    }

    private func isSymlink(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isSymbolicLinkKey]) else { return false }
        return values.isSymbolicLink == true
    }

    private func cachedTurns(for sessionID: String, mtime: Date, size: UInt64) -> [PCodePeekTurn]? {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = cache[sessionID], entry.mtime == mtime, entry.size == size else {
            return nil
        }
        return entry.turns
    }

    private func storeCache(sessionID: String, mtime: Date, size: UInt64, turns: [PCodePeekTurn]) {
        lock.lock()
        defer { lock.unlock() }
        cache[sessionID] = CacheEntry(mtime: mtime, size: size, turns: turns)
        cacheOrder.removeAll { $0 == sessionID }
        cacheOrder.append(sessionID)
        while cacheOrder.count > Self.maxCacheEntries {
            let evicted = cacheOrder.removeFirst()
            cache.removeValue(forKey: evicted)
        }
    }

    private static func parseTranscript(_ data: Data) -> [PCodePeekTurn] {
        var turns: [PCodePeekTurn] = []
        var byRun: [String: Int] = [:]
        var currentIndex: Int?
        var nextBlockID = 0
        let newline = UInt8(ascii: "\n")

        var lineStart = data.startIndex
        let end = data.endIndex

        while lineStart < end {
            let lineEnd = data[lineStart..<end].firstIndex(of: newline) ?? end
            let line = data[lineStart..<lineEnd]
            lineStart = lineEnd < end ? data.index(after: lineEnd) : end

            guard !line.isEmpty else { continue }
            guard kindMarkers.contains(where: { line.range(of: $0) != nil }) else { continue }

            guard let obj = try? JSONSerialization.jsonObject(with: line, options: []),
                  let record = obj as? [String: Any],
                  let kind = record["kind"] as? String
            else { continue }

            let runID = record["run_id"] as? String

            if kind == "turn_started" {
                guard let prompt = record["prompt"] as? String else { continue }
                let isContinuation = (record["continuation"] as? Bool) == true
                let isSide = (record["side"] as? Bool) == true

                if isContinuation, let idx = currentIndex,
                   prompt.isEmpty || turns[idx].prompt == prompt {
                    turns[idx].status = .running
                    if let runID { byRun[runID] = idx }
                    continue
                }

                let idx = turns.count
                turns.append(PCodePeekTurn(
                    id: idx,
                    prompt: prompt,
                    blocks: [],
                    status: .running,
                    side: isSide))
                if let runID { byRun[runID] = idx }
                if !isSide { currentIndex = idx }
                continue
            }

            let targetIndex = (runID.flatMap { byRun[$0] }) ?? currentIndex
            guard let idx = targetIndex else { continue }

            switch kind {
            case "Message":
                if let markdown = record["markdown"] as? String, !markdown.isEmpty {
                    turns[idx].blocks.append(.markdown(id: nextBlockID, text: markdown))
                    nextBlockID += 1
                }
            case "ToolSummary":
                let name = (record["name"] as? String) ?? ""
                let detail = (record["detail"] as? String) ?? ""
                let failed = (record["failed"] as? Bool) == true
                let elapsed = record["elapsed_seconds"] as? Double
                let command = (record["command"] as? String) ?? ""
                let call = PCodePeekToolCall(
                    id: nextBlockID,
                    name: name,
                    detail: detail,
                    failed: failed,
                    elapsedSeconds: elapsed,
                    command: command)
                turns[idx].blocks.append(.tool(call))
                nextBlockID += 1
            case "turn_completed":
                turns[idx].status = .complete
            case "turn_cancelled":
                turns[idx].status = .cancelled
            case "turn_failed":
                turns[idx].status = .failed
            default:
                break
            }
        }

        return turns
    }
}

/// Manages `⌥`-hover and menu-triggered Peek state for `PCodeWorkspaceView`.
final class PCodePeekController: ObservableObject {
    enum LoadState {
        case idle
        case loading
        case loaded([PCodePeekTurn])
        case failed(String)
    }

    @Published private(set) var target: PCodePeekTarget?
    @Published private(set) var state: LoadState = .idle
    @Published private(set) var pinned: Bool = false

    /// Called whenever `target` changes so the workspace can wake/occlude a live-peeked pane.
    var onTargetChange: (() -> Void)?
    /// Called after Peek dismisses (or mounts a live pane) so keyboard focus stays on the selected session.
    var onRestoreFocus: (() -> Void)?
    /// The workspace window, used to scope local event monitors away from sheets and other windows.
    var windowProvider: (() -> NSWindow?)?

    private var hoveredRow: PCodePeekTarget?
    private var hoveringOverlay = false
    private var optionPressed = false
    private var openWork: DispatchWorkItem?
    private var closeWork: DispatchWorkItem?
    private var flagsMonitor: Any?
    private var keyMonitor: Any?
    private var loadToken = UUID()

    private static let hoverOpenDelay: TimeInterval = 0.14
    private static let hoverCloseGrace: TimeInterval = 0.22

    var isShowing: Bool { target != nil }

    func startMonitoring() {
        stopMonitoring()
        optionPressed = Self.isOptionOnly(NSEvent.modifierFlags)
        flagsMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            guard let self else { return event }
            if let window = self.windowProvider?() {
                guard window.isKeyWindow, window.attachedSheet == nil,
                      event.window == nil || event.window === window
                else { return event }
            }
            self.handleFlagsChanged(event.modifierFlags)
            return event
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.target != nil else { return event }
            if let window = self.windowProvider?() {
                guard event.window === window, window.attachedSheet == nil else { return event }
            }
            // Escape dismisses Peek without reaching the underlying terminal (ignoring Caps Lock).
            let mods = event.modifierFlags.intersection([.command, .option, .control, .shift])
            if event.keyCode == 53 && mods.isEmpty {
                self.dismiss()
                return nil
            }
            return event
        }
    }

    func stopMonitoring() {
        openWork?.cancel()
        openWork = nil
        closeWork?.cancel()
        closeWork = nil
        if let flagsMonitor {
            NSEvent.removeMonitor(flagsMonitor)
            self.flagsMonitor = nil
        }
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
    }

    deinit { stopMonitoring() }

    private static func isOptionOnly(_ flags: NSEvent.ModifierFlags) -> Bool {
        let relevant: NSEvent.ModifierFlags = [.option, .command, .control, .shift]
        return flags.intersection(relevant) == .option
    }

    private func handleFlagsChanged(_ flags: NSEvent.ModifierFlags) {
        let nowOption = Self.isOptionOnly(flags)
        guard nowOption != optionPressed else { return }
        optionPressed = nowOption
        if nowOption, let hoveredRow, target != hoveredRow {
            scheduleOpen(for: hoveredRow, delay: target == nil ? Self.hoverOpenDelay : 0)
        } else if !nowOption {
            openWork?.cancel()
            openWork = nil
        }
    }

    func rowHoverChanged(_ rowTarget: PCodePeekTarget, hovering: Bool) {
        if hovering {
            hoveredRow = rowTarget
            closeWork?.cancel()
            closeWork = nil
            optionPressed = Self.isOptionOnly(NSEvent.modifierFlags)

            if target != nil && !pinned {
                // Already peeking in hover mode: switch immediately as pointer scans rows.
                openWork?.cancel()
                openWork = nil
                if target != rowTarget { show(rowTarget, pinned: false) }
            } else if optionPressed {
                scheduleOpen(for: rowTarget, delay: Self.hoverOpenDelay)
            }
        } else {
            if hoveredRow == rowTarget { hoveredRow = nil }
            openWork?.cancel()
            openWork = nil
            if target != nil && !pinned && !hoveringOverlay {
                scheduleClose()
            }
        }
    }

    func overlayHoverChanged(_ hovering: Bool) {
        hoveringOverlay = hovering
        if hovering {
            closeWork?.cancel()
            closeWork = nil
        } else if !pinned && hoveredRow == nil {
            scheduleClose()
        }
    }

    func peekFromMenu(_ menuTarget: PCodePeekTarget) {
        openWork?.cancel()
        openWork = nil
        closeWork?.cancel()
        closeWork = nil
        show(menuTarget, pinned: true)
    }

    func dismiss(restoreFocus: Bool = true) {
        openWork?.cancel()
        openWork = nil
        closeWork?.cancel()
        closeWork = nil
        loadToken = UUID()
        let hadTarget = target != nil
        target = nil
        state = .idle
        pinned = false
        hoveredRow = nil
        hoveringOverlay = false
        if hadTarget {
            onTargetChange?()
            if restoreFocus { onRestoreFocus?() }
        }
    }

    func windowDidResignKey() {
        optionPressed = false
        hoveredRow = nil
        openWork?.cancel()
        openWork = nil
        if !pinned {
            dismiss(restoreFocus: false)
        }
    }

    func clearHover(paneID: UUID) {
        if hoveredRow?.paneID == paneID {
            hoveredRow = nil
            openWork?.cancel()
            openWork = nil
        }
        if target?.paneID == paneID {
            dismiss()
        }
    }

    func clearHover(sessionID: String) {
        if hoveredRow?.sessionID == sessionID {
            hoveredRow = nil
            openWork?.cancel()
            openWork = nil
        }
        if target?.sessionID == sessionID {
            dismiss()
        }
    }

    func clearHoverAndUnpinned() {
        hoveredRow = nil
        openWork?.cancel()
        openWork = nil
        if !pinned {
            dismiss()
        }
    }

    private func scheduleOpen(for rowTarget: PCodePeekTarget, delay: TimeInterval) {
        openWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.hoveredRow == rowTarget, self.optionPressed else { return }
            self.show(rowTarget, pinned: false)
        }
        openWork = work
        if delay <= 0 {
            work.perform()
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    }

    private func scheduleClose() {
        closeWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.pinned, !self.hoveringOverlay, self.hoveredRow == nil else { return }
            self.dismiss()
        }
        closeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.hoverCloseGrace, execute: work)
    }

    private func show(_ newTarget: PCodePeekTarget, pinned: Bool) {
        self.pinned = pinned
        if target == newTarget {
            if newTarget.isOpen { return }
            if case .loaded = state { return }
        }
        target = newTarget
        loadToken = UUID()

        // Open sessions render their live surface directly — no transcript load needed.
        if newTarget.isOpen {
            state = .idle
            onTargetChange?()
            // Keep keyboard focus on the selected session if any subview in the peeked pane grabbed it.
            DispatchQueue.main.async { [weak self] in self?.onRestoreFocus?() }
            return
        }

        guard let sessionID = newTarget.sessionID else {
            state = .failed("No transcript available for this session.")
            onTargetChange?()
            return
        }

        state = .loading
        onTargetChange?()
        let token = loadToken

        PCodeTranscriptLoader.shared.load(sessionID: sessionID) { [weak self] result in
            guard let self, self.loadToken == token else { return }
            switch result {
            case .success(let turns):
                self.state = .loaded(turns)
            case .failure(let error):
                self.state = .failed(error.localizedDescription)
            }
        }
    }
}

/// Transparent overlay that forwards scroll-wheel events to a live-peeked terminal
/// while blocking mouse clicks from stealing focus or sending input into it.
private struct PCodePeekScrollShield: NSViewRepresentable {
    let targetSurface: Ghostty.SurfaceView

    func makeNSView(context: Context) -> ShieldView {
        let view = ShieldView()
        view.targetSurface = targetSurface
        return view
    }

    func updateNSView(_ nsView: ShieldView, context: Context) {
        nsView.targetSurface = targetSurface
    }

    final class ShieldView: NSView {
        weak var targetSurface: Ghostty.SurfaceView?

        override func scrollWheel(with event: NSEvent) {
            targetSurface?.scrollWheel(with: event)
        }

        override func mouseDown(with event: NSEvent) {}
        override func mouseUp(with event: NSEvent) {}
        override func rightMouseDown(with event: NSEvent) {}
        override func otherMouseDown(with event: NSEvent) {}
    }
}

/// Large, scrollable floating preview of a session:
/// - For open (non-selected) sessions: mounts the live terminal at full detail size (zero reflow,
///   does not switch selection, steal focus, or mark unseen responses as read).
/// - For closed (saved) sessions: renders a large scrollable floating transcript card.
struct PCodePeekOverlay: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var peek: PCodePeekController
    @ObservedObject private var typography = PCodeTypography.shared
    let resolveLivePane: (UUID) -> PCodeSessionPane?
    let onOpen: (PCodePeekTarget) -> Void

    private var cardBackground: Color {
        let base = NSColor(ghostty.config.backgroundColor)
        let target: NSColor = base.isLightColor ? .white : .white
        let fraction: CGFloat = base.isLightColor ? 0.04 : 0.05
        return Color(nsColor: base.blended(withFraction: fraction, of: target) ?? .windowBackgroundColor)
    }

    private var transcriptColorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    private var transcriptTheme: PCodeTranscriptTheme {
        PCodeTranscriptTheme.make(
            background: NSColor(ghostty.config.backgroundColor),
            foreground: NSColor(ghostty.config.foregroundColor))
    }

    var body: some View {
        if let target = peek.target {
            if target.isOpen, let paneID = target.paneID, let livePane = resolveLivePane(paneID) {
                livePeekView(target: target, pane: livePane)
            } else if !target.isOpen {
                transcriptPeekView(target: target)
            }
        }
    }

    private func livePeekView(target: PCodePeekTarget, pane: PCodeSessionPane) -> some View {
        ZStack(alignment: .topTrailing) {
            PCodeSessionPaneView(pane: pane)
                .id(pane.id)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(ghostty.config.backgroundColor)

            // Allow scrolling the live terminal without letting clicks steal focus or type into it.
            PCodePeekScrollShield(targetSurface: pane.surface)
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            // Crisp border + compact floating pill so it's unmistakably a live peek, not a switch.
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .stroke(Color.accentColor.opacity(0.75), lineWidth: 2)
                .padding(1)
                .allowsHitTesting(false)

            HStack(spacing: 8) {
                Image(systemName: "eye")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
                Text("Live Peek")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.accentColor)
                Text("·")
                    .foregroundStyle(.secondary)
                Text(target.title)
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: 260, alignment: .leading)

                Button {
                    onOpen(target)
                } label: {
                    Text("Switch")
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.accentColor.opacity(0.18))
                        .foregroundStyle(Color.accentColor)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                }
                .buttonStyle(.plain)

                Button {
                    peek.dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Close Peek (Esc)")
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(cardBackground)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color.primary.opacity(0.16), lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.35), radius: 10, y: 4)
            .padding(.top, 10)
            .padding(.trailing, 14)
        }
        .onHover { peek.overlayHoverChanged($0) }
    }

    private func transcriptPeekView(target: PCodePeekTarget) -> some View {
        ZStack {
            Color.black.opacity(0.28)
                .contentShape(Rectangle())
                .onTapGesture { peek.dismiss() }

            VStack(spacing: 0) {
                header(for: target)
                Divider()
                content(for: target)
                Divider()
                footer
            }
            .background(cardBackground)
            .environment(\.colorScheme, transcriptColorScheme)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Color.primary.opacity(0.15), lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.42), radius: 24, y: 10)
            .padding(.leading, 4)
            .padding(.trailing, 16)
            .padding(.vertical, 14)
            .onHover { peek.overlayHoverChanged($0) }
        }
    }

    private func header(for target: PCodePeekTarget) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "eye")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(target.title)
                        .font(.headline)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if target.isOpen {
                        Text("Open")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.primary.opacity(0.08))
                            .clipShape(Capsule())
                    }
                }
                HStack(spacing: 8) {
                    Text(PCodeWorkspace.abbreviatePath(target.directory))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let count = resolvedTurnCount(for: target), count > 0 {
                        Text("·")
                        Text(count == 1 ? "1 turn" : "\(count) turns")
                    }
                    if let rel = PCodeWorkspace.relativeTime(target.updatedDate) {
                        Text("·")
                        Text(rel)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Spacer(minLength: 8)

            Button {
                onOpen(target)
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: target.isOpen ? "arrow.right.circle" : "arrow.up.right.square")
                        .font(.system(size: 11, weight: .medium))
                    Text(target.isOpen ? "Switch to Session" : "Open Session")
                        .font(.callout.weight(.medium))
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(Color.accentColor.opacity(0.18))
                .foregroundStyle(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            .buttonStyle(.plain)

            Button {
                peek.dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 24, height: 24)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Close Peek (Esc)")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func resolvedTurnCount(for target: PCodePeekTarget) -> Int? {
        if case .loaded(let turns) = peek.state {
            return turns.count
        }
        return target.turnsHint
    }

    @ViewBuilder
    private func content(for target: PCodePeekTarget) -> some View {
        switch peek.state {
        case .idle, .loading:
            VStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Loading transcript…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

        case .failed(let message):
            VStack(spacing: 6) {
                Image(systemName: "doc.text.magnifyingglass")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

        case .loaded(let turns) where turns.isEmpty:
            VStack(spacing: 6) {
                Text("No turns recorded yet")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

        case .loaded(let turns):
            PCodeTranscriptRepresentable(
                sessionID: target.sessionID ?? "",
                turns: turns,
                theme: transcriptTheme,
                typography: typography,
                pinToBottom: false)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            Text("⌥ + hover sidebar to peek")
                .lineLimit(1)
            Text("scroll to read")
                .lineLimit(1)
            Text("esc close")
                .lineLimit(1)
            Spacer(minLength: 8)
            typographyControls
                .fixedSize(horizontal: true, vertical: false)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    private var typographyControls: some View {
        HStack(spacing: 8) {
            // Style preset toggle: Sans + Mono vs Mono
            HStack(spacing: 2) {
                ForEach(PCodeTypography.Style.allCases) { option in
                    let selected = typography.style == option
                    Button {
                        typography.style = option
                    } label: {
                        Text(option.label)
                            .font(.caption2.weight(selected ? .semibold : .regular))
                            .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(selected ? Color.accentColor.opacity(0.18) : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(2)
            .background(Color.primary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))

            // Weight cycle button: Regular -> Medium -> Semibold -> Bold
            Button {
                let all = PCodeTypography.Weight.allCases
                if let idx = all.firstIndex(of: typography.weight) {
                    typography.weight = all[(idx + 1) % all.count]
                }
            } label: {
                Text(typography.weight.label)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.primary.opacity(0.85))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 4)
                    .background(Color.primary.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
            }
            .buttonStyle(.plain)
            .help("Cycle font weight (Regular / Medium / Semibold / Bold)")

            // Size stepper: - 14pt +
            HStack(spacing: 2) {
                Button {
                    typography.decreaseSize()
                } label: {
                    Text("−")
                        .font(.caption2.weight(.semibold))
                        .frame(width: 18, height: 18)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(typography.size <= PCodeTypography.minSize)

                Text("\(Int(typography.size))pt")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.primary.opacity(0.85))
                    .frame(minWidth: 28)

                Button {
                    typography.increaseSize()
                } label: {
                    Text("+")
                        .font(.caption2.weight(.semibold))
                        .frame(width: 18, height: 18)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(typography.size >= PCodeTypography.maxSize)
            }
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .background(Color.primary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        }
    }
}
