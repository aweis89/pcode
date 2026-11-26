import AppKit
import Foundation
import SwiftUI

// MARK: - Syntax Highlighting Cache for Diff Views

/// Per-view cache that tokenizes diff lines asynchronously via `PCodeSyntaxHighlighter`
/// and splits token streams cleanly per line so multi-line constructs stay highlighted.
@MainActor
final class PCodeDiffSyntaxStore: ObservableObject {
    static let maxCacheEntries = 256

    private let highlighter: any PCodeSyntaxHighlighter
    private var tokensByKey: [String: [[PCodeSyntaxToken]]] = [:]
    private var keyOrder: [String] = []
    private var inFlight: Set<String> = []

    init(highlighter: any PCodeSyntaxHighlighter = PCodePrismSyntaxHighlighter.shared) {
        self.highlighter = highlighter
    }

    /// Returns cached per-line tokens if available, or kicks off an async tokenization pass.
    func lineTokens(for lines: [String], language: String?) -> [[PCodeSyntaxToken]]? {
        guard !lines.isEmpty else { return [] }
        let lang = PCodePrismSyntaxHighlighter.normalizeLanguage(language)
        guard lang != "plain" else { return nil }

        let joined = lines.joined(separator: "\n")
        let key = "\(lang)\u{0}\(joined)"
        if let cached = tokensByKey[key] {
            return cached
        }

        if let syncTokens = highlighter.cachedTokens(code: joined, language: lang) {
            let split = Self.splitTokensByLine(syncTokens, lineCount: lines.count)
            storeTokens(split, forKey: key)
            return split
        }

        if !inFlight.contains(key) {
            inFlight.insert(key)
            let hl = highlighter
            let expectedCount = lines.count
            Task { [weak self] in
                let raw = await hl.tokenize(code: joined, language: lang)
                let split = Self.splitTokensByLine(raw, lineCount: expectedCount)
                await MainActor.run {
                    guard let self else { return }
                    self.inFlight.remove(key)
                    self.storeTokens(split, forKey: key)
                    self.objectWillChange.send()
                }
            }
        }
        return nil
    }

    private func storeTokens(_ tokens: [[PCodeSyntaxToken]], forKey key: String) {
        let isNew = tokensByKey[key] == nil
        tokensByKey[key] = tokens
        if isNew {
            keyOrder.append(key)
        }
        while tokensByKey.count > Self.maxCacheEntries, !keyOrder.isEmpty {
            let oldest = keyOrder.removeFirst()
            tokensByKey.removeValue(forKey: oldest)
        }
    }

    /// Split a flat `[PCodeSyntaxToken]` array (representing `\n`-joined lines) into per-line token arrays.
    static func splitTokensByLine(_ tokens: [PCodeSyntaxToken], lineCount: Int) -> [[PCodeSyntaxToken]] {
        var result: [[PCodeSyntaxToken]] = [[]]
        result.reserveCapacity(max(1, lineCount))

        for token in tokens {
            let pieces = token.content.split(separator: "\n", omittingEmptySubsequences: false)
            for (idx, piece) in pieces.enumerated() {
                if idx > 0 {
                    result.append([])
                }
                if !piece.isEmpty {
                    result[result.count - 1].append(PCodeSyntaxToken(content: String(piece), kind: token.kind))
                }
            }
        }

        while result.count < lineCount {
            result.append([])
        }
        return result
    }

    /// Build a styled SwiftUI `AttributedString` combining syntax tokens with intraline word-diff tints.
    static func renderedLine(
        text: String,
        tokens: [PCodeSyntaxToken]?,
        intralineSpans: [PCodeIntralineSpan],
        lineKind: PCodeDiffLine.Kind,
        font: NSFont,
        palette: PCodeSyntaxPalette,
        isDark: Bool
    ) -> AttributedString {
        let mutable = NSMutableAttributedString()
        mutable.beginEditing()

        if let tokens, !tokens.isEmpty {
            for token in tokens {
                mutable.append(NSAttributedString(
                    string: token.content,
                    attributes: [
                        .font: font,
                        .foregroundColor: palette.color(for: token.kind),
                    ]
                ))
            }
        } else {
            let fallbackColor: NSColor = (lineKind == .meta) ? palette.comment : palette.plain
            mutable.append(NSAttributedString(
                string: text,
                attributes: [
                    .font: font,
                    .foregroundColor: fallbackColor,
                ]
            ))
        }

        // Overlay intraline word-diff background tints using exact UTF-16 ranges.
        if !intralineSpans.isEmpty && (lineKind == .added || lineKind == .deleted) {
            let totalUTF16 = mutable.length
            let tint: NSColor = lineKind == .added
                ? NSColor.systemGreen.withAlphaComponent(isDark ? 0.34 : 0.28)
                : NSColor.systemRed.withAlphaComponent(isDark ? 0.34 : 0.28)

            for span in intralineSpans where span.emphasized {
                let start = max(0, min(span.range.lowerBound, totalUTF16))
                let end = max(start, min(span.range.upperBound, totalUTF16))
                let length = end - start
                if length > 0 {
                    mutable.addAttribute(.backgroundColor, value: tint, range: NSRange(location: start, length: length))
                }
            }
        }

        mutable.endEditing()
        return AttributedString(mutable)
    }
}

// MARK: - Expanded Context State

private struct PCodeExpandedGapKey: Hashable {
    let fileID: String
    let gapID: String
}

// MARK: - Changes Inspector View

/// Right-hand Inspector "Changes" tab rendering live session, working-tree, branch, and single-edit diffs.
struct PCodeChangesInspectorView: View {
    @ObservedObject var state: PCodeInspectorState
    let diffs: [PCodeFileDiff]
    let actions: PCodeInspectorActions
    let singleEditLabel: String?
    let isLoading: Bool
    let emptyMessage: String?
    let codeFont: NSFont?
    let surfaceBackground: Color
    let foregroundColor: NSColor?
    let onScopeChange: ((PCodeDiffScope) -> Void)?
    let onRefresh: (() -> Void)?

    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var syntaxStore: PCodeDiffSyntaxStore

    @State private var selection: PCodeDiffSelection?
    @State private var selectionAnchor: (path: String, line: Int)?
    @State private var expandedGaps: [PCodeExpandedGapKey: [PCodeDiffLine]] = [:]
    @State private var seenHunkIDs: Set<String> = []
    @State private var pulsingHunkIDs: Set<String> = []
    @State private var updatedUnseenFiles: Set<String> = []
    @State private var lastModifiedByPath: [String: Date] = [:]
    @State private var copiedPath: String?
    @State private var containerWidth: CGFloat = 460
    @FocusState private var isDiffListFocused: Bool

    /// Minimum pane width before Side-by-Side layout is enabled without expanding the Inspector.
    static let sideBySideMinWidth: CGFloat = 640

    init(
        state: PCodeInspectorState,
        diffs: [PCodeFileDiff],
        actions: PCodeInspectorActions = .noop,
        singleEditLabel: String? = nil,
        isLoading: Bool = false,
        emptyMessage: String? = nil,
        highlighter: any PCodeSyntaxHighlighter = PCodePrismSyntaxHighlighter.shared,
        codeFont: NSFont? = nil,
        surfaceBackground: Color = Color(nsColor: .textBackgroundColor),
        foregroundColor: NSColor? = nil,
        onScopeChange: ((PCodeDiffScope) -> Void)? = nil,
        onRefresh: (() -> Void)? = nil
    ) {
        self.state = state
        self.diffs = diffs
        self.actions = actions
        self.singleEditLabel = singleEditLabel
        self.isLoading = isLoading
        self.emptyMessage = emptyMessage
        self.codeFont = codeFont
        self.surfaceBackground = surfaceBackground
        self.foregroundColor = foregroundColor
        self.onScopeChange = onScopeChange
        self.onRefresh = onRefresh
        self._syntaxStore = StateObject(wrappedValue: PCodeDiffSyntaxStore(highlighter: highlighter))
    }

    private var isDark: Bool { colorScheme == .dark }

    private var resolvedCodeFont: NSFont {
        if let codeFont { return codeFont }
        if let jb = NSFont(name: "JetBrainsMono-Regular", size: 12) {
            return jb
        }
        return NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
    }

    private var syntaxPalette: PCodeSyntaxPalette {
        // Avoid dynamic `.labelColor`/`.textColor`: when Ghostty uses a dark theme while macOS
        // is in Light Mode, `AttributedString(NSAttributedString)` resolves catalog colors against
        // the system appearance (turning plain/punctuation tokens black on a dark background).
        let concreteForeground = foregroundColor
            ?? (isDark ? NSColor(white: 0.90, alpha: 1.0) : NSColor(white: 0.12, alpha: 1.0))
        return PCodeSyntaxPalette.make(isDark: isDark, foreground: concreteForeground)
    }

    private var canUseSideBySide: Bool {
        state.isExpanded || containerWidth >= Self.sideBySideMinWidth
    }

    private var effectiveLayout: PCodeDiffLayout {
        (state.diffLayout == .sideBySide && canUseSideBySide) ? .sideBySide : .unified
    }

    private var collidingBasenames: Set<String> {
        var counts: [String: Int] = [:]
        for diff in diffs {
            counts[diff.basename, default: 0] += 1
        }
        return Set(counts.filter { $0.value > 1 }.keys)
    }

    var body: some View {
        GeometryReader { geo in
            VStack(spacing: 0) {
                toolbar
                Divider()
                contentArea
            }
            .onAppear {
                containerWidth = geo.size.width
                seedTrackingState()
            }
            .onChange(of: geo.size.width) { newWidth in
                containerWidth = newWidth
            }
        }
        .background(surfaceBackground)
    }

    // MARK: - Top Toolbar

    private var toolbar: some View {
        HStack(spacing: 8) {
            scopeControl

            fileJumpMenu

            Spacer(minLength: 4)

            if isLoading {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.75)
            } else if let onRefresh {
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.borderless)
                .help("Refresh diffs")
            }

            followAgentButton

            layoutToggleButton
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.primary.opacity(isDark ? 0.04 : 0.035))
    }

    @ViewBuilder
    private var scopeControl: some View {
        if case .singleEdit = state.diffScope {
            HStack(spacing: 4) {
                Button {
                    setScope(.session)
                } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.borderless)
                .help("Back to This Session")

                HStack(spacing: 4) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 10))
                    Text(singleEditLabel ?? state.diffScope.displayTitle)
                        .font(.system(size: 11, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    Capsule()
                        .fill(Color.accentColor.opacity(isDark ? 0.24 : 0.14))
                )
                .foregroundStyle(Color.accentColor)
            }
        } else {
            Menu {
                scopeMenuItem(.session, icon: "sparkles")
                scopeMenuItem(.workingTree, icon: "tray.full")
                scopeMenuItem(.branch, icon: "arrow.triangle.branch")
            } label: {
                HStack(spacing: 4) {
                    Text(state.diffScope.displayTitle)
                        .font(.system(size: 11, weight: .medium))
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Color.primary.opacity(isDark ? 0.08 : 0.06))
                )
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
    }

    private func scopeMenuItem(_ scope: PCodeDiffScope, icon: String) -> some View {
        Button {
            setScope(scope)
        } label: {
            Label(scope.displayTitle, systemImage: state.diffScope == scope ? "checkmark" : icon)
        }
    }

    private func setScope(_ scope: PCodeDiffScope) {
        guard state.diffScope != scope else { return }
        state.diffScope = scope
        resetTransientDiffState()
        onScopeChange?(scope)
    }

    private func resetTransientDiffState() {
        selection = nil
        selectionAnchor = nil
        expandedGaps.removeAll()
        seenHunkIDs.removeAll()
        pulsingHunkIDs.removeAll()
        updatedUnseenFiles.removeAll()
        lastModifiedByPath.removeAll()
    }

    private var fileJumpMenu: some View {
        Menu {
            if diffs.isEmpty {
                Text("No changed files")
            } else {
                ForEach(diffs) { diff in
                    Button {
                        jumpToFile(diff.path)
                    } label: {
                        let title = collidingBasenames.contains(diff.basename) && !diff.parentDirectory.isEmpty
                            ? "\(diff.basename) — \(diff.parentDirectory)"
                            : diff.basename
                        Text("[\(diff.kind.badge)] \(title)   +\(diff.added) -\(diff.removed)")
                    }
                }
                Divider()
                Button("Expand All Files") {
                    state.collapsedFiles.removeAll()
                }
                Button("Collapse All Files") {
                    state.collapsedFiles = Set(diffs.map(\.path))
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text("All files (\(diffs.count))")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(diffs.isEmpty ? .secondary : .primary)
                Image(systemName: "chevron.down")
                    .font(.system(size: 8.5, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.primary.opacity(isDark ? 0.06 : 0.04))
            )
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(diffs.isEmpty)
    }

    private var followAgentButton: some View {
        Button {
            state.followAgent.toggle()
            if state.followAgent, let latest = diffs.first {
                updatedUnseenFiles.removeAll()
                state.focusedFilePath = latest.path
            }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: state.followAgent ? "pin.fill" : "pin")
                    .font(.system(size: 10.5, weight: .medium))
                if containerWidth >= 420 {
                    Text("Follow")
                        .font(.system(size: 10.5, weight: .medium))
                }
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(state.followAgent ? Color.accentColor.opacity(isDark ? 0.24 : 0.14) : Color.clear)
            )
            .foregroundStyle(state.followAgent ? Color.accentColor : Color.secondary)
        }
        .buttonStyle(.borderless)
        .help(state.followAgent ? "Following incoming edits (click to pause)" : "Paused auto-scroll (click to follow agent edits)")
    }

    private var layoutToggleButton: some View {
        Button {
            guard canUseSideBySide else { return }
            state.diffLayout = (state.diffLayout == .unified) ? .sideBySide : .unified
        } label: {
            Image(systemName: effectiveLayout == .sideBySide ? "rectangle.split.2x1.fill" : "rectangle.split.1x2")
                .font(.system(size: 11, weight: .medium))
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(effectiveLayout == .sideBySide ? Color.accentColor.opacity(isDark ? 0.24 : 0.14) : Color.clear)
                )
                .foregroundStyle(
                    !canUseSideBySide
                        ? Color.secondary.opacity(0.45)
                        : (effectiveLayout == .sideBySide ? Color.accentColor : Color.secondary)
                )
        }
        .buttonStyle(.borderless)
        .disabled(!canUseSideBySide)
        .help(
            canUseSideBySide
                ? (effectiveLayout == .sideBySide ? "Switch to Unified diff" : "Switch to Side-by-Side diff")
                : "Expand Inspector (⌘⇧F) or widen pane for Side-by-Side diff"
        )
    }

    // MARK: - Main Scrollable Content

    private var contentArea: some View {
        ScrollViewReader { proxy in
            Group {
                if diffs.isEmpty {
                    emptyStateView
                } else {
                    ZStack(alignment: .bottomTrailing) {
                        ScrollView(.vertical) {
                            LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                                ForEach(diffs) { diff in
                                    Section {
                                        if !state.collapsedFiles.contains(diff.path) {
                                            fileDiffBody(for: diff)
                                        }
                                    } header: {
                                        fileHeader(for: diff)
                                    }
                                    .id(fileAnchorID(diff.path))
                                }
                            }
                            .padding(.bottom, selection != nil ? 56 : 16)
                        }
                        .focusable(true)
                        .focused($isDiffListFocused)
                        .modifier(PCodeDiffKeyHandlerModifier(
                            onPrevFile: { navigateFile(direction: -1, proxy: proxy) },
                            onNextFile: { navigateFile(direction: 1, proxy: proxy) },
                            onEscape: {
                                guard selection != nil else { return false }
                                clearSelection()
                                return true
                            },
                            onReferenceSelection: {
                                guard selection != nil else { return false }
                                referenceCurrentSelection()
                                return true
                            },
                            onOpenInEditor: {
                                guard !diffs.isEmpty else { return false }
                                openFocusedOrSelectedInEditor()
                                return true
                            }
                        ))
                        .onAppear {
                            scrollToFocusedFile(state.focusedFilePath, proxy: proxy, animated: false)
                        }
                        .onChange(of: state.focusedFilePath) { targetPath in
                            scrollToFocusedFile(targetPath, proxy: proxy, animated: true)
                        }

                        if let selection {
                            floatingSelectionBar(for: selection)
                                .padding(.trailing, 14)
                                .padding(.bottom, 12)
                                .transition(.move(edge: .bottom).combined(with: .opacity))
                        }
                    }
                    .animation(.easeOut(duration: 0.16), value: selection)
                }
            }
            .onChange(of: state.diffScope) { _ in
                resetTransientDiffState()
            }
            .onChange(of: diffTrackingFingerprint) { _ in
                handleDiffUpdates(proxy: proxy)
            }
        }
    }

    private var emptyStateView: some View {
        VStack(spacing: 8) {
            Image(systemName: isLoading ? "hourglass" : "doc.text.magnifyingglass")
                .font(.system(size: 24, weight: .light))
                .foregroundStyle(.secondary)
            Text(emptyStateTitle)
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(.primary)
            Text(emptyMessage ?? defaultEmptySubtitle)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 260)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }

    private var emptyStateTitle: String {
        if isLoading { return "Loading diffs…" }
        switch state.diffScope {
        case .session: return "No edits in this session yet"
        case .workingTree: return "Working tree is clean"
        case .branch: return "No branch changes"
        case .singleEdit: return "Edit diff unavailable"
        }
    }

    private var defaultEmptySubtitle: String {
        switch state.diffScope {
        case .session: return "File edits made by the agent will stream in here automatically."
        case .workingTree: return "Uncommitted changes and untracked files will appear here."
        case .branch: return "Commits ahead of the merge base and uncommitted edits appear here."
        case .singleEdit: return "Return to This Session to view all session edits."
        }
    }

    // MARK: - Sticky File Header

    private func fileHeader(for diff: PCodeFileDiff) -> some View {
        let isCollapsed = state.collapsedFiles.contains(diff.path)
        let isFocused = state.focusedFilePath == diff.path
        let hasUnseenUpdate = updatedUnseenFiles.contains(diff.path)

        return HStack(spacing: 6) {
            Button {
                state.toggleCollapsed(filePath: diff.path)
                updatedUnseenFiles.remove(diff.path)
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: isCollapsed ? "chevron.right" : "chevron.down")
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 10)

                    changeKindBadge(diff.kind)

                    Text(diff.basename)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    if !diff.parentDirectory.isEmpty {
                        Text(diff.parentDirectory)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.head)
                    }

                    if diff.isStreaming {
                        PCodePulsingDotView(color: .green)
                            .help("Streaming edit…")
                    } else if hasUnseenUpdate {
                        PCodePulsingDotView(color: .accentColor)
                            .help("Updated")
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            Spacer(minLength: 6)

            // +N -M stat counters
            HStack(spacing: 4) {
                if diff.added > 0 {
                    Text("+\(diff.added)")
                        .foregroundStyle(isDark ? Color(red: 0.52, green: 0.87, blue: 0.56) : Color(red: 0.11, green: 0.50, blue: 0.22))
                }
                if diff.removed > 0 {
                    Text("-\(diff.removed)")
                        .foregroundStyle(isDark ? Color(red: 0.98, green: 0.50, blue: 0.50) : Color(red: 0.76, green: 0.14, blue: 0.16))
                }
            }
            .font(.system(size: 11, weight: .medium, design: .monospaced))

            // Open in Editor button
            Button {
                let targetPath = diff.absolutePath ?? diff.path
                let line = (selection?.path == diff.path ? selection?.startLine : nil) ?? diff.firstChangedLine
                actions.openInEditor(targetPath, line)
            } label: {
                Image(systemName: "arrow.up.forward.square")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Open in Editor (⌘O)")

            // Copy path button
            Button {
                actions.copyToClipboard(diff.path)
                copiedPath = diff.path
                let path = diff.path
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                    if copiedPath == path {
                        copiedPath = nil
                    }
                }
            } label: {
                Image(systemName: copiedPath == diff.path ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 10.5))
                    .foregroundStyle(copiedPath == diff.path ? Color.green : Color.secondary)
            }
            .buttonStyle(.borderless)
            .help("Copy path")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            ZStack {
                surfaceBackground
                Color.primary.opacity(isDark ? 0.06 : 0.045)
                if isFocused {
                    Color.accentColor.opacity(isDark ? 0.10 : 0.06)
                } else if diff.isStreaming {
                    Color.green.opacity(isDark ? 0.08 : 0.05)
                }
            }
        )
        .overlay(alignment: .bottom) {
            Divider()
        }
        .overlay(alignment: .top) {
            Divider().opacity(0.6)
        }
    }

    private func changeKindBadge(_ kind: PCodeFileChangeKind) -> some View {
        let color: Color
        switch kind {
        case .added: color = .green
        case .modified: color = .orange
        case .deleted: color = .red
        case .renamed: color = .purple
        }
        return Text(kind.badge)
            .font(.system(size: 9.5, weight: .bold, design: .monospaced))
            .foregroundStyle(color)
            .frame(width: 15, height: 15)
            .background(
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(color.opacity(isDark ? 0.20 : 0.14))
            )
    }

    // MARK: - File Diff Body

    @ViewBuilder
    private func fileDiffBody(for diff: PCodeFileDiff) -> some View {
        if diff.hunks.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                if PCodeDiffParser.isBinaryPatch(diff.rawPatch) {
                    Label("Binary file changed", systemImage: "doc.fill")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                } else if diff.kind == .renamed {
                    Label("File renamed with no content changes", systemImage: "arrow.right.doc.on.clipboard")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                } else if diff.kind == .added {
                    Label("Empty file created", systemImage: "doc.badge.plus")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                } else if diff.kind == .deleted {
                    Label("File deleted", systemImage: "trash")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                } else {
                    Text("No textual hunks to display")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(diff.hunks.enumerated()), id: \.element.id) { hunkIndex, hunk in
                    // Optional top gap expander above first hunk.
                    if hunkIndex == 0 && hunk.newStart > 1, let absPath = diff.absolutePath {
                        expandableGapView(
                            diff: diff,
                            gapID: "top-\(hunk.id)",
                            filePath: absPath,
                            afterLine: 0,
                            beforeLine: hunk.newStart,
                            oldLineOffset: hunk.oldStart - hunk.newStart,
                            fromBottom: true
                        )
                    } else if hunkIndex > 0, let absPath = diff.absolutePath {
                        let prev = diff.hunks[hunkIndex - 1]
                        let prevEndNew = prev.newStart + max(0, prev.newCount - 1)
                        let prevEndOld = prev.oldStart + max(0, prev.oldCount - 1)
                        if hunk.newStart - prevEndNew > 1 {
                            expandableGapView(
                                diff: diff,
                                gapID: "between-\(prev.id)-\(hunk.id)",
                                filePath: absPath,
                                afterLine: prevEndNew,
                                beforeLine: hunk.newStart,
                                oldLineOffset: prevEndOld - prevEndNew,
                                fromBottom: false
                            )
                        }
                    }

                    hunkView(hunk, in: diff)
                }

                if diff.truncated {
                    HStack(spacing: 6) {
                        Image(systemName: "ellipsis.circle")
                        Text("Diff output was truncated")
                    }
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                }
            }
            .padding(.bottom, 6)
        }
    }

    // MARK: - Expandable Context Bar

    @ViewBuilder
    private func expandableGapView(
        diff: PCodeFileDiff,
        gapID: String,
        filePath: String,
        afterLine: Int,
        beforeLine: Int,
        oldLineOffset: Int,
        fromBottom: Bool
    ) -> some View {
        let key = PCodeExpandedGapKey(fileID: diff.id, gapID: gapID)
        let loaded = expandedGaps[key] ?? []

        let remainingAfter: Int = {
            if fromBottom {
                return afterLine
            }
            return loaded.last?.newLineNumber ?? afterLine
        }()
        let remainingBefore: Int = {
            if fromBottom {
                return loaded.first?.newLineNumber ?? beforeLine
            }
            return beforeLine
        }()
        let hiddenCount = max(0, remainingBefore - remainingAfter - 1)

        VStack(alignment: .leading, spacing: 0) {
            if !fromBottom && !loaded.isEmpty {
                expandedLinesView(loaded, in: diff)
            }

            if hiddenCount > 0 {
                let step = min(20, hiddenCount)
                Button {
                    let newlyLoaded = PCodeDiffParser.expandContext(
                        filePath: filePath,
                        afterLine: remainingAfter,
                        beforeLine: remainingBefore,
                        limit: step,
                        fromBottom: fromBottom,
                        oldLineOffset: oldLineOffset
                    )
                    guard !newlyLoaded.isEmpty else { return }
                    if fromBottom {
                        expandedGaps[key] = newlyLoaded + loaded
                    } else {
                        expandedGaps[key] = loaded + newlyLoaded
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: fromBottom ? "arrow.up.to.line" : "chevron.up.chevron.down")
                            .font(.system(size: 10))
                        Text(hiddenCount > step ? "Show \(step) more lines (\(hiddenCount) hidden)" : "Show \(hiddenCount) hidden lines")
                            .font(.system(size: 10.5, weight: .medium))
                        Spacer()
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                    .background(Color.primary.opacity(isDark ? 0.04 : 0.03))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }

            if fromBottom && !loaded.isEmpty {
                expandedLinesView(loaded, in: diff)
            }
        }
    }

    private func expandedLinesView(_ lines: [PCodeDiffLine], in diff: PCodeFileDiff) -> some View {
        let texts = lines.map(\.text)
        let perLineTokens = syntaxStore.lineTokens(for: texts, language: diff.language)
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.element.id) { idx, line in
                unifiedLineRow(
                    line,
                    tokens: perLineTokens.flatMap { idx < $0.count ? $0[idx] : nil },
                    in: diff
                )
            }
        }
    }

    // MARK: - Hunk Rendering

    @ViewBuilder
    private func hunkView(_ hunk: PCodeDiffHunk, in diff: PCodeFileDiff) -> some View {
        let isPulsing = pulsingHunkIDs.contains(scopedHunkID(hunk, in: diff))
        let lineTexts = hunk.lines.map(\.text)
        let perLineTokens = syntaxStore.lineTokens(for: lineTexts, language: diff.language)

        VStack(alignment: .leading, spacing: 0) {
            // Hunk header bar
            HStack(spacing: 6) {
                Text(hunk.header)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(Color.accentColor.opacity(isDark ? 0.85 : 0.90))
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer()
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 3)
            .background(Color.accentColor.opacity(isDark ? 0.08 : 0.05))

            if effectiveLayout == .sideBySide {
                sideBySideHunkBody(hunk, perLineTokens: perLineTokens, in: diff)
            } else {
                unifiedHunkBody(hunk, perLineTokens: perLineTokens, in: diff)
            }
        }
        .overlay {
            if isPulsing {
                Rectangle()
                    .fill(Color.green.opacity(isDark ? 0.14 : 0.10))
                    .allowsHitTesting(false)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.55), value: isPulsing)
    }

    private func unifiedHunkBody(
        _ hunk: PCodeDiffHunk,
        perLineTokens: [[PCodeSyntaxToken]]?,
        in diff: PCodeFileDiff
    ) -> some View {
        ScrollView(.horizontal, showsIndicators: true) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(hunk.lines.enumerated()), id: \.element.id) { idx, line in
                    unifiedLineRow(
                        line,
                        tokens: perLineTokens.flatMap { idx < $0.count ? $0[idx] : nil },
                        in: diff
                    )
                }
            }
            .frame(minWidth: max(200, containerWidth), alignment: .leading)
        }
    }

    private func unifiedLineRow(
        _ line: PCodeDiffLine,
        tokens: [PCodeSyntaxToken]?,
        in diff: PCodeFileDiff
    ) -> some View {
        let isSelected = isLineSelected(line, in: diff)
        let rendered = PCodeDiffSyntaxStore.renderedLine(
            text: line.text,
            tokens: tokens,
            intralineSpans: line.intralineSpans,
            lineKind: line.kind,
            font: resolvedCodeFont,
            palette: syntaxPalette,
            isDark: isDark
        )

        return HStack(alignment: .top, spacing: 0) {
            // Old line number
            Text(line.oldLineNumber.map(String.init) ?? "")
                .frame(width: 38, alignment: .trailing)
                .padding(.trailing, 5)
                .foregroundStyle(.secondary.opacity(0.75))

            // New line number
            Text(line.newLineNumber.map(String.init) ?? "")
                .frame(width: 38, alignment: .trailing)
                .padding(.trailing, 6)
                .foregroundStyle(.secondary.opacity(0.75))

            // Sign marker
            Text(markerText(for: line.kind))
                .frame(width: 14, alignment: .center)
                .foregroundStyle(markerColor(for: line.kind))

            // Highlighted line text
            Text(rendered)
                .fixedSize(horizontal: true, vertical: false)
                .padding(.trailing, 12)

            Spacer(minLength: 0)
        }
        .font(.system(size: 11, design: .monospaced))
        .padding(.vertical, 1.5)
        .background(rowBackground(kind: line.kind, isSelected: isSelected))
        .overlay(alignment: .leading) {
            if isSelected {
                Rectangle()
                    .fill(Color.accentColor)
                    .frame(width: 2.5)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            handleLineTap(line, in: diff)
        }
    }

    // MARK: - Side-by-Side Hunk Rendering

    private func sideBySideHunkBody(
        _ hunk: PCodeDiffHunk,
        perLineTokens: [[PCodeSyntaxToken]]?,
        in diff: PCodeFileDiff
    ) -> some View {
        let rows = PCodeDiffParser.sideBySideRows(for: hunk)
        let tokenByLineID: [String: [PCodeSyntaxToken]] = {
            guard let perLineTokens else { return [:] }
            var map: [String: [PCodeSyntaxToken]] = [:]
            for (idx, line) in hunk.lines.enumerated() where idx < perLineTokens.count {
                map[line.id] = perLineTokens[idx]
            }
            return map
        }()

        let halfWidth = max(160, (containerWidth - 1) / 2)
        let rowHeight: CGFloat = ceil(resolvedCodeFont.pointSize * 1.48)

        return HStack(alignment: .top, spacing: 0) {
            ScrollView(.horizontal, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { row in
                        if row.isMeta, let meta = row.left {
                            sideBySideMetaCell(meta.text, rowHeight: rowHeight)
                        } else {
                            sideBySideCell(
                                line: row.left,
                                lineNumber: row.left?.oldLineNumber,
                                tokens: row.left.flatMap { tokenByLineID[$0.id] },
                                rowHeight: rowHeight,
                                in: diff
                            )
                        }
                    }
                }
                .frame(minWidth: halfWidth, alignment: .leading)
            }
            .frame(width: halfWidth, alignment: .leading)

            Divider()

            ScrollView(.horizontal, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { row in
                        if row.isMeta {
                            sideBySideMetaCell("", rowHeight: rowHeight)
                        } else {
                            sideBySideCell(
                                line: row.right,
                                lineNumber: row.right?.newLineNumber,
                                tokens: row.right.flatMap { tokenByLineID[$0.id] },
                                rowHeight: rowHeight,
                                in: diff
                            )
                        }
                    }
                }
                .frame(minWidth: halfWidth, alignment: .leading)
            }
            .frame(width: halfWidth, alignment: .leading)
        }
    }

    private func sideBySideMetaCell(_ text: String, rowHeight: CGFloat) -> some View {
        HStack(spacing: 6) {
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .frame(height: rowHeight, alignment: .leading)
        .background(Color.primary.opacity(isDark ? 0.03 : 0.02))
    }

    @ViewBuilder
    private func sideBySideCell(
        line: PCodeDiffLine?,
        lineNumber: Int?,
        tokens: [PCodeSyntaxToken]?,
        rowHeight: CGFloat,
        in diff: PCodeFileDiff
    ) -> some View {
        if let line {
            let isSelected = isLineSelected(line, in: diff)
            let rendered = PCodeDiffSyntaxStore.renderedLine(
                text: line.text,
                tokens: tokens,
                intralineSpans: line.intralineSpans,
                lineKind: line.kind,
                font: resolvedCodeFont,
                palette: syntaxPalette,
                isDark: isDark
            )

            HStack(alignment: .center, spacing: 0) {
                Text(lineNumber.map(String.init) ?? "")
                    .font(.system(size: 10.5, design: .monospaced))
                    .frame(width: 36, alignment: .trailing)
                    .padding(.trailing, 5)
                    .foregroundStyle(.secondary.opacity(0.75))

                Text(markerText(for: line.kind))
                    .font(.system(size: 11, design: .monospaced))
                    .frame(width: 12, alignment: .center)
                    .foregroundStyle(markerColor(for: line.kind))

                Text(rendered)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.trailing, 10)

                Spacer(minLength: 0)
            }
            .frame(height: rowHeight, alignment: .leading)
            .background(rowBackground(kind: line.kind, isSelected: isSelected))
            .overlay(alignment: .leading) {
                if isSelected {
                    Rectangle()
                        .fill(Color.accentColor)
                        .frame(width: 2.5)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture {
                handleLineTap(line, in: diff)
            }
        } else {
            // Empty counterpart filler cell
            Color.primary.opacity(isDark ? 0.035 : 0.025)
                .frame(maxWidth: .infinity, minHeight: rowHeight, maxHeight: rowHeight)
        }
    }

    // MARK: - Line Styling Helpers

    private func markerText(for kind: PCodeDiffLine.Kind) -> String {
        switch kind {
        case .added: return "+"
        case .deleted: return "-"
        case .context: return " "
        case .meta: return "·"
        }
    }

    private func markerColor(for kind: PCodeDiffLine.Kind) -> Color {
        switch kind {
        case .added:
            return isDark ? Color(red: 0.52, green: 0.87, blue: 0.56) : Color(red: 0.11, green: 0.50, blue: 0.22)
        case .deleted:
            return isDark ? Color(red: 0.98, green: 0.50, blue: 0.50) : Color(red: 0.76, green: 0.14, blue: 0.16)
        case .context, .meta:
            return .secondary.opacity(0.5)
        }
    }

    private func rowBackground(kind: PCodeDiffLine.Kind, isSelected: Bool) -> Color {
        if isSelected {
            return Color.accentColor.opacity(isDark ? 0.24 : 0.16)
        }
        switch kind {
        case .added:
            return Color.green.opacity(isDark ? 0.14 : 0.10)
        case .deleted:
            return Color.red.opacity(isDark ? 0.14 : 0.10)
        case .context:
            return .clear
        case .meta:
            return Color.primary.opacity(isDark ? 0.04 : 0.03)
        }
    }

    // MARK: - Selection & Floating Reference Action

    private func isLineSelected(_ line: PCodeDiffLine, in diff: PCodeFileDiff) -> Bool {
        guard let selection,
              selection.path == diff.path,
              let lineNum = line.referenceLineNumber
        else {
            return false
        }
        return lineNum >= selection.startLine && lineNum <= selection.endLine
    }

    private func handleLineTap(_ line: PCodeDiffLine, in diff: PCodeFileDiff) {
        guard let lineNum = line.referenceLineNumber else { return }
        let isShiftHeld = NSEvent.modifierFlags.contains(.shift)

        state.focusedFilePath = diff.path
        updatedUnseenFiles.remove(diff.path)

        if isShiftHeld, let anchor = selectionAnchor, anchor.path == diff.path {
            selection = PCodeDiffSelection(path: diff.path, startLine: anchor.line, endLine: lineNum)
        } else if let current = selection,
                  current.path == diff.path,
                  current.startLine == lineNum && current.endLine == lineNum
        {
            clearSelection()
        } else {
            selectionAnchor = (path: diff.path, line: lineNum)
            selection = PCodeDiffSelection(path: diff.path, startLine: lineNum, endLine: lineNum)
        }
    }

    private func clearSelection() {
        selection = nil
        selectionAnchor = nil
    }

    private func referenceCurrentSelection() {
        guard let selection else { return }
        actions.insertIntoComposer(
            PCodeComposerInsertion.formattedInsertion(reference: selection.promptReference)
        )
    }

    private func openFocusedOrSelectedInEditor() {
        if let selection {
            let match = diffs.first { $0.path == selection.path }
            actions.openInEditor(match?.absolutePath ?? selection.path, selection.startLine)
            return
        }
        if let focused = state.focusedFilePath, let match = diffs.first(where: { $0.path == focused }) {
            actions.openInEditor(match.absolutePath ?? match.path, match.firstChangedLine)
            return
        }
        if let first = diffs.first {
            actions.openInEditor(first.absolutePath ?? first.path, first.firstChangedLine)
        }
    }

    private func floatingSelectionBar(for selection: PCodeDiffSelection) -> some View {
        HStack(spacing: 8) {
            Text(selection.promptReference)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.head)
                .frame(maxWidth: 200, alignment: .leading)

            Button {
                referenceCurrentSelection()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "text.quote")
                        .font(.system(size: 10.5))
                    Text("Reference (⌘L)")
                        .font(.system(size: 11, weight: .semibold))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Color.accentColor)
                )
                .foregroundStyle(.white)
            }
            .buttonStyle(.plain)

            Button {
                openFocusedOrSelectedInEditor()
            } label: {
                Image(systemName: "arrow.up.forward.square")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Open selected line in Editor (⌘O)")

            Button {
                clearSelection()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Clear selection (Esc)")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(.regularMaterial)
                .shadow(color: Color.black.opacity(isDark ? 0.45 : 0.16), radius: 8, y: 3)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(Color.primary.opacity(isDark ? 0.16 : 0.10), lineWidth: 0.75)
        )
    }

    // MARK: - Navigation & Streaming Update Tracking

    private func fileAnchorID(_ path: String) -> String {
        "diff-file-\(path)"
    }

    private func scopedHunkID(_ hunk: PCodeDiffHunk, in diff: PCodeFileDiff) -> String {
        "\(diff.id)::\(hunk.id)"
    }

    private func scrollToFocusedFile(_ targetPath: String?, proxy: ScrollViewProxy, animated: Bool) {
        guard let targetPath, diffs.contains(where: { $0.path == targetPath }) else { return }
        updatedUnseenFiles.remove(targetPath)
        let anchorID = fileAnchorID(targetPath)
        if animated {
            withAnimation(.easeInOut(duration: 0.22)) {
                proxy.scrollTo(anchorID, anchor: .top)
            }
        } else {
            DispatchQueue.main.async {
                proxy.scrollTo(anchorID, anchor: .top)
            }
        }
    }

    private func jumpToFile(_ path: String) {
        state.collapsedFiles.remove(path)
        state.focusedFilePath = path
        updatedUnseenFiles.remove(path)
    }

    private func navigateFile(direction: Int, proxy: ScrollViewProxy) {
        let next: String? = direction >= 0
            ? PCodeDiffNavigation.selectNextFile(in: diffs, state: state)
            : PCodeDiffNavigation.selectPreviousFile(in: diffs, state: state)
        guard let next else { return }
        updatedUnseenFiles.remove(next)
        withAnimation(.easeInOut(duration: 0.2)) {
            proxy.scrollTo(fileAnchorID(next), anchor: .top)
        }
    }

    /// Lightweight fingerprint capturing incoming hunks, streaming flags, and file modification timestamps.
    private var diffTrackingFingerprint: String {
        diffs.map { diff in
            let hunkCount = diff.hunks.count
            let lineCount = diff.hunks.reduce(0) { $0 + $1.lines.count }
            return "\(diff.id)|\(diff.lastModifiedAt.timeIntervalSinceReferenceDate)|\(diff.isStreaming)|\(hunkCount)|\(lineCount)"
        }.joined(separator: ";")
    }

    private func seedTrackingState() {
        var initialHunks: Set<String> = []
        var initialDates: [String: Date] = [:]
        for diff in diffs {
            initialDates[diff.path] = diff.lastModifiedAt
            for hunk in diff.hunks {
                initialHunks.insert(scopedHunkID(hunk, in: diff))
            }
        }
        seenHunkIDs = initialHunks
        lastModifiedByPath = initialDates
    }

    private func handleDiffUpdates(proxy: ScrollViewProxy) {
        var newlyArrivedHunks: [String] = []
        var touchedFiles: [PCodeFileDiff] = []

        for diff in diffs {
            let prevDate = lastModifiedByPath[diff.path]
            let fileUpdated = prevDate == nil || (prevDate != nil && diff.lastModifiedAt > prevDate!)
            if fileUpdated || diff.isStreaming {
                touchedFiles.append(diff)
            }
            lastModifiedByPath[diff.path] = diff.lastModifiedAt

            for hunk in diff.hunks {
                let scopedID = scopedHunkID(hunk, in: diff)
                if !seenHunkIDs.contains(scopedID) {
                    seenHunkIDs.insert(scopedID)
                    newlyArrivedHunks.append(scopedID)
                }
            }
        }

        // Trigger a brief streaming reveal pulse on newly arrived hunks.
        if !newlyArrivedHunks.isEmpty {
            pulsingHunkIDs.formUnion(newlyArrivedHunks)
            let batch = Set(newlyArrivedHunks)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.65) {
                pulsingHunkIDs.subtract(batch)
            }
        }

        guard let latest = touchedFiles.max(by: { $0.lastModifiedAt < $1.lastModifiedAt }) else {
            return
        }

        if state.followAgent {
            // Auto-scroll to incoming edit when Follow is pinned.
            state.collapsedFiles.remove(latest.path)
            state.focusedFilePath = latest.path
            updatedUnseenFiles.remove(latest.path)
            withAnimation(.easeInOut(duration: 0.25)) {
                proxy.scrollTo(fileAnchorID(latest.path), anchor: .top)
            }
        } else {
            // Do NOT yank scroll when Follow is off — mark updated files with a subtle indicator dot.
            for file in touchedFiles {
                updatedUnseenFiles.insert(file.path)
            }
        }
    }
}

// MARK: - Streaming Indicator Dot

private struct PCodePulsingDotView: View {
    let color: Color
    @State private var pulse = false

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(pulse ? 0.0 : 0.35))
                .frame(width: 11, height: 11)
                .scaleEffect(pulse ? 1.35 : 0.75)
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: false)) {
                pulse = true
            }
        }
    }
}

// MARK: - Focused `[` / `]`, `⌘L` / `⌘O` & `Esc` Key Handler

private struct PCodeDiffKeyHandlerModifier: ViewModifier {
    let onPrevFile: () -> Void
    let onNextFile: () -> Void
    let onEscape: () -> Bool
    let onReferenceSelection: () -> Bool
    let onOpenInEditor: () -> Bool

    func body(content: Content) -> some View {
        if #available(macOS 14.0, *) {
            content
                .onKeyPress(characters: CharacterSet(charactersIn: "[]")) { press in
                    if press.characters == "[" {
                        onPrevFile()
                        return .handled
                    }
                    if press.characters == "]" {
                        onNextFile()
                        return .handled
                    }
                    return .ignored
                }
                .onKeyPress(characters: CharacterSet(charactersIn: "lLoO"), phases: .down) { press in
                    guard press.modifiers == .command else { return .ignored }
                    let lower = press.characters.lowercased()
                    if lower == "l" {
                        return onReferenceSelection() ? .handled : .ignored
                    }
                    if lower == "o" {
                        return onOpenInEditor() ? .handled : .ignored
                    }
                    return .ignored
                }
                .onKeyPress(.escape) {
                    onEscape() ? .handled : .ignored
                }
        } else {
            content
        }
    }
}
