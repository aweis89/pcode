import AppKit
import Combine
import SwiftUI

// The /btw drawer's content, drawn natively with the transcript's block views.
// What it shows mirrors `pcode.btw_view` (`question_blocks`, `outcome_blocks`)
// and `pcode.btw_tail` (heading, running status, connection notes), which the
// drawer used to run in a terminal of its own.

// MARK: - Thread → transcript items

enum PCodeBtwThread {
    /// When the monotonic clock read zero, fixed once so a running row's start
    /// time doesn't jitter between renders (and so rewrite the row each time).
    static let bootDate = Date(timeIntervalSinceNow: -ProcessInfo.processInfo.systemUptime)

    /// `thread`'s questions as transcript rows, with ids stable across updates
    /// (`btw-<aside id>-…`): each question as a /btw prompt, its answer as
    /// Markdown, then a running row while it runs (left out when `docked`:
    /// the editor card's status bar shows it there) or why it stopped.
    static func items(
        for thread: [PCodeAsideState],
        docked: Bool,
        bootDate: Date = PCodeBtwThread.bootDate
    ) -> [PCodeTranscriptItem] {
        var result: [PCodeTranscriptItem] = []
        for (number, aside) in thread.enumerated() {
            let turnPrefix = "turn-\(number)-"
            let prefix = "btw-\(aside.id)-"
            let answer = aside.answer.trimmingCharacters(in: .newlines)
            let turn = PCodePeekTurn(
                id: number,
                prompt: aside.question,
                blocks: answer.isEmpty ? [] : [.markdown(id: 0, text: answer)],
                status: .complete,
                side: true)
            var rows = PCodeTranscriptItem.flatten(turns: [turn]).map { item -> PCodeTranscriptItem in
                let id = item.id.hasPrefix(turnPrefix) ? prefix + item.id.dropFirst(turnPrefix.count) : prefix + item.id
                var payload = item.payload
                if case .prompt(_, let text, let isSide) = payload {
                    payload = .prompt(label: promptLabel(aside, first: number == 0), text: text, isSide: isSide)
                }
                return PCodeTranscriptItem(
                    id: id, payload: payload, spacingAfter: item.spacingAfter, revision: item.revision, depth: item.depth)
            }
            if let call = statusCall(aside, docked: docked, bootDate: bootDate) {
                if let last = rows.popLast() { rows.append(last.withSpacingAfter(8)) }
                rows.append(PCodeTranscriptItem(id: prefix + "status", payload: .tool(call, isExpanded: false), spacingAfter: 18))
            }
            result += rows
        }
        return result
    }

    /// `/btw`, and for a thread's first question where it runs (`question_blocks`).
    static func promptLabel(_ aside: PCodeAsideState, first: Bool) -> String {
        let model = aside.model.isEmpty ? "" : "on \(aside.model)"
        let effort = aside.effort.isEmpty ? "" : "at \(aside.effort) effort"
        let place = [model, effort].filter { !$0.isEmpty }.joined(separator: " ")
        return first && !place.isEmpty ? "/btw · \(place)" : "/btw"
    }

    /// While it runs, what it is doing (`Tail._progress`); once settled, why it
    /// stopped or that there is no answer (`outcome_blocks`); else nothing.
    static func statusCall(
        _ aside: PCodeAsideState,
        docked: Bool,
        bootDate: Date
    ) -> PCodePeekToolCall? {
        if aside.isRunning {
            if docked { return nil }
            let activity = aside.activity.trimmingCharacters(in: CharacterSet(charactersIn: "…. "))
            // Python's `time.monotonic` and `systemUptime` share a clock on macOS.
            let startedAt = aside.started.map { min(Date(), bootDate.addingTimeInterval($0)) }
            return PCodePeekToolCall(
                id: 0, name: "\(activity.isEmpty ? "Answering" : activity)…", detail: "", failed: false,
                elapsedSeconds: nil, command: "", isRunning: true, startedAt: startedAt)
        }
        let elapsed: Double? = {
            guard let started = aside.started, let finished = aside.finished, finished >= started else { return nil }
            return finished - started
        }()
        if !aside.error.isEmpty {
            return PCodePeekToolCall(
                id: 0, name: aside.status, detail: aside.error, failed: true, elapsedSeconds: elapsed, command: "")
        }
        if aside.answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return PCodePeekToolCall(
                id: 0, name: aside.status, detail: "no answer", failed: false, elapsedSeconds: elapsed, command: "")
        }
        return nil
    }
}

// MARK: - Incremental updates

/// How to bring a document's rows to a new list without `setItems`, which
/// drops the selection and the scroll position: upsert changed and new rows
/// by id and remove gone ones. A reorder resets the whole list instead.
enum PCodeTranscriptItemSync {
    enum Step: Equatable {
        case reset
        case remove(String)
        /// Insert at the index, or update in place when the id is already there.
        case upsert(String, at: Int)
    }

    static func plan(from old: [PCodeTranscriptItem], to new: [PCodeTranscriptItem]) -> [Step] {
        if old.isEmpty { return new.isEmpty ? [] : [.reset] }
        let newIDs = Set(new.map(\.id))
        let kept = old.filter { newIDs.contains($0.id) }
        let oldIDs = Set(old.map(\.id))
        // Nothing in common (another thread) starts over, pinned to the bottom;
        // the rows both lists have must keep their order, or upserts can't place them.
        guard !kept.isEmpty, kept.map(\.id) == new.map(\.id).filter(oldIDs.contains) else {
            return [.reset]
        }
        var steps = old.filter { !newIDs.contains($0.id) }.map { Step.remove($0.id) }
        let oldByID = Dictionary(old.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        for (index, item) in new.enumerated() {
            if let previous = oldByID[item.id], signature(previous) == signature(item) { continue }
            steps.append(.upsert(item.id, at: index))
        }
        return steps
    }

    static func apply(_ new: [PCodeTranscriptItem], to document: PCodeTranscriptDocumentView) {
        let steps = plan(from: document.items, to: new)
        let byID = Dictionary(new.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        for step in steps {
            switch step {
            case .reset:
                document.setItems(new, pinToBottom: true)
                return
            case .remove(let id):
                document.removeItem(id: id)
            case .upsert(let id, let index):
                guard var item = byID[id] else { continue }
                // A row the user expanded stays expanded as it changes.
                if case .tool(let call, false) = item.payload, document.isItemExpanded(id: id) {
                    item = PCodeTranscriptItem(
                        id: id, payload: .tool(call, isExpanded: true), spacingAfter: item.spacingAfter,
                        revision: item.revision, depth: item.depth)
                }
                document.upsertItem(item, insertAt: index)
            }
        }
    }

    /// What a row draws: equal signatures need no update.
    static func signature(_ item: PCodeTranscriptItem) -> String {
        var parts = [item.kind.rawValue, "\(item.spacingAfter)", item.plainText]
        if case .tool(let call, _) = item.payload {
            parts += ["\(call.isRunning)", "\(call.failed)", "\(call.elapsedSeconds ?? -1)",
                      "\(call.startedAt?.timeIntervalSinceReferenceDate ?? -1)"]
        }
        return parts.joined(separator: "\u{1F}")
    }
}

// MARK: - Footer hints

/// The keys pcode's editor answers a thread with: `popup_hints.POPUPS["btw_drawer"]`.
enum PCodeBtwHints {
    /// `shortcut_label("\\")` under pcode's default `key_prefix` (ctrl), as a keycap.
    static let defaultNextThreadKey = "⌃\\"

    static func hints(docked: Bool, others: Bool, nextThreadKey: String) -> [PCodeHint] {
        var hints: [PCodeHint] = []
        if !docked { hints.append(PCodeHint(key: "↩", label: "Reply in editor", keep: true)) }
        if others { hints.append(PCodeHint(key: nextThreadKey, label: "Next thread", keep: false)) }
        hints.append(PCodeHint(key: "esc", label: "Close", keep: true))
        return hints
    }

    /// The "Next thread" keycap under the user's `key_prefix`, asked of the
    /// host's own pcode (`prefix_keys.shortcut_label`, `popup_hints.native_keycap`)
    /// so the app never re-implements prefix parsing. Calls back on the main
    /// queue only with a plausible keycap; otherwise the default stays.
    static func loadNextThreadKey(host: String, completion: @escaping (String) -> Void) {
        guard let python = PCode.entryPython(PCode.statePath("hosts/\(host).json")) else { return }
        DispatchQueue.global(qos: .utility).async {
            let script = "from pcode.popup_hints import native_keycap\n"
                + "from pcode.prefix_keys import shortcut_label\n"
                + "print(native_keycap(shortcut_label('\\\\')))"
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-I", "-c", script]
            process.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
            let output = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            process.standardInput = FileHandle.nullDevice
            do { try process.run() } catch { return }
            let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
            DispatchQueue.global().asyncAfter(deadline: .now() + 10, execute: timeout)
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            timeout.cancel()
            guard process.terminationStatus == 0,
                  let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !text.isEmpty, text.count <= PCodeHint.keyLimit, !text.contains("\n") else { return }
            DispatchQueue.main.async { completion(text) }
        }
    }
}

// MARK: - Model

/// The drawer's thread, kept up with its host by a `PCodeAsideObserver`.
/// Changing `thread` on the same host keeps the connection.
final class PCodeBtwThreadModel: ObservableObject {
    let host: String
    @Published var thread: String
    @Published private(set) var store = PCodeAsideStore()
    /// Why the stream is not live (`btw_tail`'s note); empty while it is.
    @Published private(set) var note = ""
    @Published private(set) var nextThreadKey = PCodeBtwHints.defaultNextThreadKey
    private var observer: PCodeAsideObserver?
    /// Only the newest `refreshHints` may set the keycap.
    private var hintsGeneration = 0

    init(host: String, thread: String) {
        self.host = host
        self.thread = thread
        let observer = PCodeAsideObserver(
            host: host, socketPath: PCodeAsideObserver.socketPath(host: host)
        ) { [weak self] event in
            self?.handle(event)
        }
        self.observer = observer
        observer.start()
        refreshHints()
    }

    deinit { observer?.stop() }

    var asides: [PCodeAsideState] { store.thread(thread) }

    /// Read the user's prefix again: it may have changed since the drawer opened.
    func refreshHints() {
        hintsGeneration += 1
        let generation = hintsGeneration
        PCodeBtwHints.loadNextThreadKey(host: host) { [weak self] key in
            guard let self, self.hintsGeneration == generation, self.nextThreadKey != key else { return }
            self.nextThreadKey = key
        }
    }

    func stop() {
        observer?.stop()
        observer = nil
    }

    private func handle(_ event: PCodeAsideObserver.Event) {
        switch event {
        case .message(let message):
            store.apply(message)
        case .note(let text):
            if note != text { note = text }
        }
    }
}

// MARK: - Views

/// The drawer's content: the thread's heading, its rows, any connection note
/// and the editor's key hints.
struct PCodeBtwDrawerContent: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var model: PCodeBtwThreadModel
    @ObservedObject private var typography = PCodeTypography.shared
    var docked: Bool
    var fonts: PCodePickFonts?
    var onFocusTerminal: () -> Void
    var onClose: () -> Void
    /// The transcript's AppKit view once made, so the drawer can tell whether it has the keyboard.
    var onContentView: (NSView) -> Void = { _ in }

    var body: some View {
        let asides = model.asides
        VStack(spacing: 0) {
            heading(asides.first)
            ZStack {
                PCodeBtwTranscriptRepresentable(
                    items: PCodeBtwThread.items(for: asides, docked: docked),
                    theme: theme,
                    typography: typography,
                    onFocusTerminal: onFocusTerminal,
                    onClose: onClose,
                    onMake: onContentView)
                if asides.isEmpty && model.note.isEmpty {
                    Text("Waiting for the side thread…")
                        .foregroundStyle(muted)
                        .font(PCodePickFonts.small(fonts, fallbackSize: 12))
                        .allowsHitTesting(false)
                }
            }
            if !model.note.isEmpty {
                Text(model.note)
                    .font(PCodePickFonts.small(fonts, fallbackSize: 11))
                    .foregroundStyle(muted)
                    .lineLimit(2)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.top, 6)
            }
            let hints = PCodeBtwHints.hints(
                docked: docked, others: model.store.threadCount > 1, nextThreadKey: model.nextThreadKey)
            PCodeHintBar(hints: hints, fonts: fonts, muted: muted)
                .padding(.bottom, 6)
        }
        .environment(\.colorScheme, colorScheme)
    }

    /// `Tail._heading`: the badge, the thread's first question, its label.
    @ViewBuilder
    private func heading(_ first: PCodeAsideState?) -> some View {
        if let first {
            HStack(spacing: 6) {
                Text("btw")
                    .font(PCodePickFonts.small(fonts, fallbackSize: 11, weight: .semibold))
                    .foregroundStyle(Color.black.opacity(0.85))
                    .padding(.horizontal, 5)
                    .background(RoundedRectangle(cornerRadius: 3).fill(Color.orange))
                Text(Self.excerpt(first.question))
                    .font(PCodePickFonts.small(fonts, fallbackSize: 12, weight: .semibold))
                    .lineLimit(1)
                    .truncationMode(.tail)
                if !first.label.isEmpty {
                    Text("· \(first.label)")
                        .font(PCodePickFonts.small(fonts, fallbackSize: 12))
                        .foregroundStyle(muted)
                        .lineLimit(1)
                        .layoutPriority(-1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }

    /// The question's first line, cut to 80 characters (`conversation_tree.excerpt`).
    static func excerpt(_ text: String) -> String {
        let line = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        return trimmed.count > 80 ? String(trimmed.prefix(79)) + "…" : trimmed
    }

    private var theme: PCodeTranscriptTheme {
        PCodeTranscriptTheme.make(
            background: NSColor(ghostty.config.backgroundColor),
            foreground: NSColor(ghostty.config.foregroundColor))
    }

    private var muted: Color { ghostty.config.foregroundColor.opacity(PCodePickTheme.mutedOpacity) }

    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}

/// Holds the drawer's transcript and answers the drawer's own keys: ⌘W while
/// it has the keyboard closes the drawer (no terminal is there to ask).
final class PCodeBtwTranscriptHostView: NSView {
    let scrollView: PCodeTranscriptScrollView
    var onClose: (() -> Void)?

    init(typography: PCodeTypography, theme: PCodeTranscriptTheme) {
        scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        super.init(frame: .zero)
        scrollView.autoresizingMask = [.width, .height]
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layout() {
        super.layout()
        scrollView.frame = bounds
    }

    /// True while this view, or a view inside it, is the window's first responder.
    var hasKeyboard: Bool {
        guard let responder = window?.firstResponder as? NSView else { return false }
        return responder === self || responder.isDescendant(of: self)
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
        if flags == .command, event.charactersIgnoringModifiers?.lowercased() == "w", hasKeyboard, let onClose {
            onClose()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

struct PCodeBtwTranscriptRepresentable: NSViewRepresentable {
    let items: [PCodeTranscriptItem]
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    var onFocusTerminal: () -> Void
    var onClose: () -> Void
    var onMake: (NSView) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> PCodeBtwTranscriptHostView {
        let view = PCodeBtwTranscriptHostView(typography: typography, theme: theme)
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        wire(view)
        view.scrollView.transcriptDocumentView.setItems(items, pinToBottom: true)
        onMake(view)
        return view
    }

    func updateNSView(_ view: PCodeBtwTranscriptHostView, context: Context) {
        let coordinator = context.coordinator
        let snapshot = typography.snapshot
        wire(view)
        if coordinator.lastTheme != theme || coordinator.lastTypographySnapshot != snapshot {
            coordinator.lastTheme = theme
            coordinator.lastTypographySnapshot = snapshot
            view.scrollView.transcriptDocumentView.updateAppearance(typography: snapshot, theme: theme)
            view.scrollView.applyTheme(theme)
        }
        PCodeTranscriptItemSync.apply(items, to: view.scrollView.transcriptDocumentView)
    }

    private func wire(_ view: PCodeBtwTranscriptHostView) {
        view.onClose = onClose
        // Esc, a plain click or typing in the drawer give the keyboard back to
        // pcode's editor (typed keys go on to it), so it never strands focus.
        view.scrollView.transcriptDocumentView.onReturnFocusToComposer = onFocusTerminal
    }

    final class Coordinator {
        var lastTheme: PCodeTranscriptTheme?
        var lastTypographySnapshot: PCodeTypographySnapshot?
    }
}
