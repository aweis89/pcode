import AppKit
import Combine
import QuartzCore
import SwiftUI

/// Visual palette for `PCodeTranscriptView`, derived from the terminal/app theme.
struct PCodeTranscriptTheme: Equatable {
    let isDark: Bool
    let background: NSColor
    let foreground: NSColor
    let secondaryForeground: NSColor
    let accent: NSColor
    let promptBackground: NSColor
    let codeBackground: NSColor
    let inlineCodeFill: NSColor
    let toolBackground: NSColor

    static func make(background: NSColor, foreground: NSColor) -> PCodeTranscriptTheme {
        let isLight = background.isLightColor
        let promptBg = background.blended(
            withFraction: isLight ? 0.06 : 0.11,
            of: isLight ? .black : .white) ?? .controlBackgroundColor
        let codeBg = background.blended(
            withFraction: isLight ? 0.06 : 0.28,
            of: .black) ?? .textBackgroundColor
        let inlineFill = isLight ? NSColor.black.withAlphaComponent(0.06) : NSColor.white.withAlphaComponent(0.10)
        let toolBg = isLight ? NSColor.black.withAlphaComponent(0.04) : NSColor.white.withAlphaComponent(0.05)

        return PCodeTranscriptTheme(
            isDark: !isLight,
            background: background,
            foreground: foreground,
            secondaryForeground: foreground.withAlphaComponent(0.68),
            accent: .controlAccentColor,
            promptBackground: promptBg,
            codeBackground: codeBg,
            inlineCodeFill: inlineFill,
            toolBackground: toolBg)
    }
}

/// One model thinking block: streamed text plus how long it took once settled.
struct PCodeThinkingPayload: Equatable {
    var text: String
    /// True while `ThinkingDelta`s are still arriving.
    var isStreaming: Bool = false
    /// Seconds the block took; nil for older transcripts that never recorded it.
    var elapsedSeconds: Double?
}

/// One flat, virtualized row in `PCodeTranscriptView`.
struct PCodeTranscriptItem: Identifiable {
    enum Kind: String {
        case prompt
        case prose
        case codeBlock
        case table
        case tool
        case toolGroup
        case diff
        case thinking
        case status
    }

    enum Payload {
        case prompt(label: String, text: String, isSide: Bool)
        case prose(markdown: String, referenceDefinitions: String, copyMarkdown: String?)
        case codeBlock(language: String?, code: String, fullCopyCode: String, showHeader: Bool, groupID: String?)
        case table(PCodeMarkdownTable)
        case tool(PCodePeekToolCall, isExpanded: Bool)
        case toolGroup(PCodeToolGroupPayload)
        case diff(PCodeDiffPayload, isExpanded: Bool)
        case thinking(PCodeThinkingPayload)
        case status(PCodePeekTurn.Status)
    }

    let id: String
    let payload: Payload
    let spacingAfter: CGFloat
    /// Monotonic revision bumped when an item's payload mutates in place (e.g. expand/upsert).
    let revision: UInt64
    /// Flattened nesting depth (e.g. `1` for sub-agent tool/edit calls under `parent_call_id`).
    let depth: Int

    var kind: Kind {
        switch payload {
        case .prompt: return .prompt
        case .prose: return .prose
        case .codeBlock: return .codeBlock
        case .table: return .table
        case .tool: return .tool
        case .toolGroup: return .toolGroup
        case .diff: return .diff
        case .thinking: return .thinking
        case .status: return .status
        }
    }

    /// Group ID shared across contiguous >200-line chunks of the same fenced code block.
    var codeBlockGroupID: String? {
        if case .codeBlock(_, _, _, _, let groupID) = payload {
            return groupID
        }
        return nil
    }

    /// Plain-text fallback representation (used for height estimates).
    var plainText: String {
        switch payload {
        case .prompt(let label, let text, _):
            return label.isEmpty ? text : "\(label)\n\(text)"
        case .prose(let markdown, _, _):
            return markdown
        case .codeBlock(_, let code, _, _, _):
            return code
        case .table(let table):
            return table.plainText
        case .tool(let call, _):
            let summary = call.summaryText
            return summary.isEmpty ? call.displayName : "\(call.displayName) \(summary)"
        case .toolGroup(let group):
            return group.plainSummaryText
        case .diff(let diff, let isExpanded):
            return isExpanded ? "\(diff.path)\n\(diff.patch)" : PCodeDiffCardView.summaryLine(for: diff)
        case .thinking(let thinking):
            return thinking.text
        case .status(let status):
            return PCodeStatusRowView.displayLabel(for: status)
        }
    }

    init(id: String, payload: Payload, spacingAfter: CGFloat = 8, revision: UInt64 = 0, depth: Int = 0) {
        self.id = id
        self.payload = payload
        self.spacingAfter = spacingAfter
        self.revision = revision
        self.depth = max(0, depth)
    }

    /// Convenience initializer used by unit tests and simple synthetic items.
    init(
        id: String,
        kind: Kind,
        plainText: String,
        secondaryText: String = "",
        isSidePrompt: Bool = false,
        isFailed: Bool = false,
        elapsedText: String? = nil,
        spacingAfter: CGFloat = 8
    ) {
        self.id = id
        self.spacingAfter = spacingAfter
        self.revision = 0
        switch kind {
        case .prompt:
            self.payload = .prompt(
                label: secondaryText.isEmpty ? (isSidePrompt ? "/btw" : "") : secondaryText,
                text: plainText,
                isSide: isSidePrompt)
        case .prose:
            self.payload = .prose(markdown: plainText, referenceDefinitions: "", copyMarkdown: nil)
        case .codeBlock:
            self.payload = .codeBlock(
                language: secondaryText.isEmpty ? nil : secondaryText,
                code: plainText,
                fullCopyCode: plainText,
                showHeader: true,
                groupID: nil)
        case .table:
            let cell = PCodeMarkdownTableCell(markdown: plainText, plainText: plainText)
            self.payload = .table(PCodeMarkdownTable(headers: [cell], alignments: [.left], rows: []))
        case .tool:
            let call = PCodePeekToolCall(
                id: 0,
                name: plainText,
                detail: secondaryText,
                failed: isFailed,
                elapsedSeconds: nil,
                command: "")
            self.payload = .tool(call, isExpanded: false)
        case .toolGroup:
            let call = PCodePeekToolCall(
                id: 0,
                name: plainText,
                detail: secondaryText,
                failed: isFailed,
                elapsedSeconds: nil,
                command: "")
            self.payload = .toolGroup(PCodeToolGroupPayload(
                entries: [PCodeToolGroupEntry(id: id, content: .tool(call), depth: 0)],
                isExpanded: false))
        case .diff:
            self.payload = .diff(PCodeDiffPayload(
                callID: id,
                path: secondaryText,
                operation: "edit",
                patch: plainText,
                added: 0,
                removed: 0,
                isPreview: false,
                truncated: false,
                depth: 0), isExpanded: false)
        case .thinking:
            self.payload = .thinking(PCodeThinkingPayload(text: plainText))
        case .status:
            let status: PCodePeekTurn.Status = isFailed ? .failed : (PCodePeekTurn.Status(rawValue: plainText.lowercased()) ?? .running)
            self.payload = .status(status)
        }
        self.depth = 0
    }

    func withSpacingAfter(_ spacing: CGFloat) -> PCodeTranscriptItem {
        PCodeTranscriptItem(id: id, payload: payload, spacingAfter: spacing, revision: revision, depth: depth)
    }

    /// Flatten `PCodePeekTurn`s into virtualized transcript items, parsing Markdown messages
    /// into prose runs, syntax-highlighted code blocks, and GFM tables.
    static func flatten(turns: [PCodePeekTurn]) -> [PCodeTranscriptItem] {
        var result: [PCodeTranscriptItem] = []
        result.reserveCapacity(turns.count * 4)
        for turn in turns {
            result.append(PCodeTranscriptItem(
                id: "turn-\(turn.id)-prompt",
                payload: .prompt(
                    label: turn.side ? "/btw" : "",
                    text: turn.prompt,
                    isSide: turn.side),
                spacingAfter: 10))

            for block in turn.blocks {
                switch block {
                case .markdown(let blockID, let text):
                    let parsed = PCodeTranscriptMarkdown.parseBlocks(text)
                    var codeGroupCounter = 0
                    var activeCodeGroupID: String?

                    for (subIdx, mdBlock) in parsed.enumerated() {
                        switch mdBlock {
                        case .prose(let proseMD, let refDefs):
                            activeCodeGroupID = nil
                            result.append(PCodeTranscriptItem(
                                id: "turn-\(turn.id)-md-\(blockID)-\(subIdx)",
                                payload: .prose(
                                    markdown: proseMD,
                                    referenceDefinitions: refDefs,
                                    copyMarkdown: text),
                                spacingAfter: 8))
                        case .codeBlock(let lang, let code, let fullCode, let chunkIdx, let totalChunks):
                            if chunkIdx == 0 {
                                activeCodeGroupID = "turn-\(turn.id)-md-\(blockID)-code-\(codeGroupCounter)"
                                codeGroupCounter += 1
                            }
                            let isLastChunk = (chunkIdx == totalChunks - 1)
                            result.append(PCodeTranscriptItem(
                                id: "turn-\(turn.id)-md-\(blockID)-\(subIdx)",
                                payload: .codeBlock(
                                    language: lang,
                                    code: code,
                                    fullCopyCode: fullCode,
                                    showHeader: chunkIdx == 0,
                                    groupID: totalChunks > 1 ? activeCodeGroupID : nil),
                                spacingAfter: isLastChunk ? 10 : 0))
                        case .table(let table):
                            activeCodeGroupID = nil
                            result.append(PCodeTranscriptItem(
                                id: "turn-\(turn.id)-md-\(blockID)-\(subIdx)",
                                payload: .table(table),
                                spacingAfter: 10))
                        }
                    }

                case .tool(let call):
                    result.append(PCodeTranscriptItem(
                        id: "turn-\(turn.id)-tool-\(call.id)",
                        payload: .tool(call, isExpanded: false),
                        spacingAfter: 5))
                }
            }

            if turn.status != .complete {
                result.append(PCodeTranscriptItem(
                    id: "turn-\(turn.id)-status",
                    payload: .status(turn.status),
                    spacingAfter: 14))
            } else if !result.isEmpty {
                let last = result.removeLast()
                result.append(last.withSpacingAfter(18))
            }
        }
        return result
    }
}

/// Weak target proxy so `CADisplayLink` never creates a retain cycle with `PCodeTranscriptDocumentView`.
private final class PCodeDisplayLinkProxy: NSObject {
    weak var target: PCodeTranscriptDocumentView?

    init(target: PCodeTranscriptDocumentView) {
        self.target = target
    }

    @objc func tick(_ sender: CADisplayLink) {
        guard let target else {
            sender.invalidate()
            return
        }
        target.handleDisplayLinkTick(sender)
    }
}

/// Flipped, virtualized AppKit document view supporting responsive scrolling (`prepareContent(in:)`),
/// bare TextKit 2 rendering, rich Markdown blocks, model-owned cross-item selection, and
/// frame-coalesced streaming.
final class PCodeTranscriptDocumentView: NSView, NSUserInterfaceValidations {
    static let horizontalPadding: CGFloat = 18

    private struct AppliedRowState: Equatable {
        let itemID: String
        let revision: UInt64
        let width: CGFloat
        let appearanceGeneration: UInt64
    }

    private(set) var rawItems: [PCodeTranscriptItem] = []
    private(set) var items: [PCodeTranscriptItem] = []
    private(set) var layoutEngine = PCodeTranscriptLayoutEngine()
    private(set) var stickyBottom = PCodeStickyBottomState()
    private(set) var selection: PCodeTranscriptSelection?
    weak var textFinder: NSTextFinder?
    var cachedSearchIndex: PCodeTranscriptSearchIndex?
    var accessibilityRowProxies: [String: PCodeTranscriptAccessibilityRowElement] = [:]
    /// Called on plain click or non-navigation typing to hand keyboard focus back to `PCodeDockCard`.
    var onReturnFocusToComposer: (() -> Void)?
    /// In the `/btw` drawer, `q` hands focus back to the composer without forwarding `q` (`btw_tail`).
    var swallowsQToReturnFocus = false
    /// When `true` (default), consecutive settled tools/edits fold into an expandable summary pill
    /// above any currently running tools.
    var groupTools: Bool = PCodeTranscriptPreferences.shared.groupTools {
        didSet {
            guard groupTools != oldValue else { return }
            syncProjectedItems(revealNewIDs: [], foldSnapshot: nil, animateShift: true)
        }
    }
    /// Whether thinking rows show (pcode's `show_thinking`; a live pane follows its host).
    /// Hidden rows stay in `rawItems`, so showing them again needs no replay.
    var showThinking: Bool = PCodeThinkingVisibility.loadDefaultFromPreferences() {
        didSet {
            guard showThinking != oldValue else { return }
            syncProjectedItems(revealNewIDs: [], foldSnapshot: nil, animateShift: true)
        }
    }
    /// Framing for prompt rows; changing it re-measures, since padding differs per style.
    var promptStyle: PCodePromptStyle = PCodeTranscriptPreferences.shared.promptStyle {
        didSet {
            guard promptStyle != oldValue else { return }
            appearanceGeneration &+= 1
            layoutEngine.invalidateAllMeasurements()
            remountVisibleViews()
        }
    }

    var typography: PCodeTypographySnapshot
    var theme: PCodeTranscriptTheme
    var highlighter: any PCodeSyntaxHighlighter

    private var mountedViews: [String: any PCodeTranscriptRowHosting] = [:]
    private var appliedStates: [String: AppliedRowState] = [:]
    private var rawItemIndexByID: [String: Int] = [:]
    private var itemIndexByID: [String: Int] = [:]
    private var expandedGroupIDs: Set<String> = []
    private var absorbPulseByGroupID: [String: UInt64] = [:]
    private var groupIDByRawItemID: [String: String] = [:]
    private var activeFoldGhostLayers: [CALayer] = []
    private var reusePools: [PCodeTranscriptItem.Kind: [any PCodeTranscriptRowHosting]] = [:]
    private var attributedCache: [String: NSAttributedString] = [:]
    private var attributedCacheOrder: [String] = []
    private static let maxAttributedCacheEntries = 256
    private var appearanceGeneration: UInt64 = 0
    private var lastContentWidth: CGFloat = 0
    private var lastClipOriginY: CGFloat = 0
    /// Blank space kept below the content so a pinned view never scrolls back up.
    ///
    /// When content at the bottom shrinks (a finished tool folding into its group, a new row
    /// measuring shorter than its estimate), moving the origin up to the new bottom would drag
    /// the whole scrollback downward on screen, against the direction it streams. Instead the
    /// document keeps its height and later growth fills the gap before scrolling resumes.
    private var bottomSlack: CGFloat = 0
    /// Content height (without slack) at the last `applyDocumentHeight`, to tell shrink from growth.
    private var lastAppliedContentHeight: CGFloat = 0
    private var lastMountedRange: Range<Int> = 0..<0
    private var isApplyingDocumentHeight = false
    private var hoveredItemID: String?
    private var liveRunningTimer: Timer?
    private var runningToolIDs: Set<String> = []

    // Settle-then-reveal streaming state
    private struct ActiveStreamingMessage {
        let id: String
        var settler: PCodeMarkdownSettler
        var emittedBlockCount: Int
        var codeGroupCounter: Int
    }

    private var activeStream: ActiveStreamingMessage?
    private var revealQueue = PCodeRevealQueue()
    private var smoothGlide = PCodeSmoothScrollGlide()
    private var lastDisplayLinkTargetTimestamp: CFTimeInterval?
    private var streamingTurnCounter: Int = 0
    private var streamingMessageCounter: Int = 0
    private var displayLink: CADisplayLink?
    private var isDraggingSelection = false
    private var dragMoved = false
    private var trackingArea: NSTrackingArea?

    /// Where Copy puts text; tests swap in a private pasteboard.
    var pasteboard: NSPasteboard = .general

    /// Reveal style matching pcode's `paced_scrollback` preference (`typed`, `rows`, `off`).
    var paceMode: PCodeStreamingPaceMode = PCodeStreamingPaceMode.loadDefaultFromPreferences()
    /// Speed multiplier applied to both reveal queue and smooth-scroll conveyor (`1.0` live / `4.0` fast replay).
    var streamingSpeedMultiplier: CGFloat = 1.0

    /// True when new streaming content has arrived while the user is scrolled up.
    private(set) var hasUnreadStreamingContent: Bool = false
    /// Invoked whenever the floating "Jump to bottom" pill visibility should change.
    var onJumpToBottomVisibilityChanged: ((Bool) -> Void)?
    /// Invoked whenever the smooth-scroll conveyor glide starts or settles (drives bottom-edge dissolve).
    var onSmoothGlideActiveChanged: ((Bool) -> Void)?
    private var lastNotifiedGlideActive: Bool = false
    /// Number of items currently queued in the visual reveal queue.
    var pendingRevealCount: Int { revealQueue.entries.count }
    /// True while either the reveal queue or the 120Hz smooth-scroll conveyor has active work.
    var isStreamingAnimationActive: Bool {
        !revealQueue.isEmpty || smoothGlide.isActive
    }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override class var isCompatibleWithResponsiveScrolling: Bool { true }

    init(
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        highlighter: any PCodeSyntaxHighlighter = PCodePrismSyntaxHighlighter.shared
    ) {
        self.typography = typography
        self.theme = theme
        self.highlighter = highlighter
        super.init(frame: .zero)
        wantsLayer = true
        autoresizingMask = [.width]
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        stopDisplayLink()
        stopLiveRunningTimer()
    }

    /// Route clicks on explicit buttons/scrollers and scroll events on horizontal code/table
    /// scroll views to those views, while owning all text selection drags directly on `self`.
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard frame.contains(point) else { return nil }
        if let hit = super.hitTest(point) {
            if hit is NSButton || hit is NSScroller {
                return hit
            }
            var ancestor: NSView? = hit
            while let current = ancestor, current !== self {
                if current is NSButton || current is NSScroller {
                    return current
                }
                if current is PCodeHorizontalBlockScrollView {
                    return current
                }
                ancestor = current.superview
            }
        }
        return self
    }

    override func viewWillMove(toWindow newWindow: NSWindow?) {
        super.viewWillMove(toWindow: newWindow)
        if newWindow == nil {
            stopDisplayLink()
            stopLiveRunningTimer()
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let window {
            layoutEngine.backingScale = window.backingScaleFactor
            if isStreamingAnimationActive {
                ensureDisplayLinkRunning()
            }
            syncLiveRunningTimer()
        }
    }

    private func trackRunningState(for item: PCodeTranscriptItem) {
        if case .tool(let call, _) = item.payload, call.isRunning && call.startedAt != nil {
            runningToolIDs.insert(item.id)
        } else {
            runningToolIDs.remove(item.id)
        }
        syncLiveRunningTimer()
    }

    private func syncLiveRunningTimer() {
        guard !runningToolIDs.isEmpty, window != nil else {
            stopLiveRunningTimer()
            return
        }
        guard liveRunningTimer == nil else { return }
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = Date()
            for id in self.runningToolIDs {
                if let card = self.mountedViews[id] as? PCodeToolCardView {
                    card.updateLiveElapsed(now: now)
                }
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        liveRunningTimer = timer
    }

    private func stopLiveRunningTimer() {
        liveRunningTimer?.invalidate()
        liveRunningTimer = nil
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingArea { removeTrackingArea(trackingArea) }
        let area = NSTrackingArea(
            rect: .zero,
            options: [.activeInKeyWindow, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect],
            owner: self,
            userInfo: nil)
        addTrackingArea(area)
        trackingArea = area
    }

    override func mouseMoved(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        updateHover(at: point)
        let superPoint = superview?.convert(event.locationInWindow, from: nil) ?? point
        if let hit = super.hitTest(superPoint), hit is NSButton || hit is NSScroller {
            NSCursor.arrow.set()
            return
        }
        if linkURL(at: point) != nil {
            NSCursor.pointingHand.set()
        } else {
            NSCursor.iBeam.set()
        }
    }

    private func updateHover(at point: CGPoint) {
        guard let idx = layoutEngine.itemIndex(atY: point.y),
              items.indices.contains(idx)
        else {
            setHoveredItemID(nil)
            return
        }
        let id = items[idx].id
        guard let rowView = mountedViews[id], rowView.frame.contains(point) else {
            setHoveredItemID(nil)
            return
        }
        setHoveredItemID(id)
    }

    private func setHoveredItemID(_ newID: String?) {
        guard hoveredItemID != newID else { return }
        if let old = hoveredItemID {
            mountedViews[old]?.setHovered(false)
        }
        hoveredItemID = newID
        if let id = newID {
            mountedViews[id]?.setHovered(true)
        }
    }

    override func mouseExited(with event: NSEvent) {
        setHoveredItemID(nil)
        NSCursor.arrow.set()
    }

    /// Replace all transcript items (e.g. when opening Peek or loading history).
    /// Settled history gets no reveal animation.
    func setItems(_ newItems: [PCodeTranscriptItem], pinToBottom: Bool) {
        notifySearchContentWillChange()
        expandedGroupIDs.removeAll(keepingCapacity: false)
        absorbPulseByGroupID.removeAll(keepingCapacity: false)
        clearActiveFoldGhostLayers()
        rawItems = newItems
        resetBottomSlack()
        rebuildRawItemIndexMap()
        let projection = PCodeToolGrouper.project(
            rawItems: rawItems,
            groupTools: groupTools,
            hideThinking: !showThinking,
            expandedGroupIDs: expandedGroupIDs,
            absorbPulses: absorbPulseByGroupID)
        items = projection.items
        groupIDByRawItemID = projection.groupIDByRawItemID
        rebuildItemIndexMap()
        invalidateSearchIndex()
        accessibilityRowProxies.removeAll(keepingCapacity: false)
        selection = nil
        hoveredItemID = nil
        activeStream = nil
        revealQueue.reset()
        resetSmoothGlide()
        setHasUnreadStreamingContent(false)
        clearAttributedCache()
        stopDisplayLink()
        recycleAllMountedViews()
        lastMountedRange = 0..<0

        layoutEngine.backingScale = window?.backingScaleFactor ?? 2
        let estimates = items.map { item in
            (id: item.id, estimatedHeight: estimatedHeight(for: item), spacingAfter: item.spacingAfter)
        }
        layoutEngine.reset(items: estimates)

        if pinToBottom {
            stickyBottom.pinToBottom()
        } else {
            stickyBottom.unpinForUserAction()
        }

        let width = max(1, enclosingScrollView?.contentSize.width ?? bounds.width)
        lastContentWidth = width
        applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: pinToBottom, allowSmoothGlide: false)
        if !pinToBottom, let scrollView = enclosingScrollView {
            let topOrigin = CGPoint(x: 0, y: -scrollView.contentInsets.top)
            isApplyingDocumentHeight = true
            scrollView.contentView.scroll(to: topOrigin)
            scrollView.reflectScrolledClipView(scrollView.contentView)
            lastClipOriginY = topOrigin.y
            isApplyingDocumentHeight = false
        }
        mountViews(in: visibleRect)
        runningToolIDs.removeAll(keepingCapacity: true)
        for item in items {
            if case .tool(let call, _) = item.payload, call.isRunning && call.startedAt != nil {
                runningToolIDs.insert(item.id)
            }
        }
        syncLiveRunningTimer()
    }

    // MARK: - ID-Addressed Upsert & Streaming Pipeline

    private func rebuildRawItemIndexMap() {
        rawItemIndexByID.removeAll(keepingCapacity: true)
        for (idx, item) in rawItems.enumerated() {
            rawItemIndexByID[item.id] = idx
        }
    }

    private func rebuildItemIndexMap() {
        itemIndexByID.removeAll(keepingCapacity: true)
        for (idx, item) in items.enumerated() {
            itemIndexByID[item.id] = idx
        }
    }

    /// Look up a canonical (ungrouped) item's index by its stable `id` (`O(1)`).
    func indexOfRawItem(id: String) -> Int? {
        rawItemIndexByID[id]
    }

    /// Look up a displayed item's current index by its stable `id` (`O(1)`).
    func indexOfItem(id: String) -> Int? {
        itemIndexByID[id]
    }

    /// Return whether an existing `.tool`, `.diff`, or `.toolGroup` item is currently expanded by the user.
    func isItemExpanded(id: String) -> Bool {
        if expandedGroupIDs.contains(id) { return true }
        if let rawIdx = rawItemIndexByID[id], rawItems.indices.contains(rawIdx) {
            switch rawItems[rawIdx].payload {
            case .tool(_, let isExpanded), .diff(_, let isExpanded):
                return isExpanded
            case .toolGroup(let payload):
                return payload.isExpanded
            default:
                return false
            }
        }
        guard let idx = itemIndexByID[id], items.indices.contains(idx) else { return false }
        switch items[idx].payload {
        case .tool(_, let isExpanded), .diff(_, let isExpanded):
            return isExpanded
        case .toolGroup(let payload):
            return payload.isExpanded
        default:
            return false
        }
    }

    private struct FoldSnapshot {
        let rawItemID: String
        let layer: CALayer
        let originFrame: CGRect
    }

    private func captureFoldSnapshot(for itemID: String) -> FoldSnapshot? {
        guard window != nil,
              !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
              let view = mountedViews[itemID],
              view.bounds.width > 1,
              view.bounds.height > 1,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds)
        else { return nil }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let cgImage = rep.cgImage else { return nil }
        let ghost = CALayer()
        ghost.contents = cgImage
        ghost.contentsScale = window?.backingScaleFactor ?? 2.0
        // Start from where the row is drawn, including any shift still sliding it into place.
        let drawnFrame = view.frame.offsetBy(dx: 0, dy: Self.inFlightShift(of: view))
        ghost.frame = drawnFrame
        ghost.cornerRadius = view.layer?.cornerRadius ?? 5
        ghost.masksToBounds = true
        return FoldSnapshot(rawItemID: itemID, layer: ghost, originFrame: drawnFrame)
    }

    /// Insert or update a transcript item by its stable `id` (used by both live streaming
    /// and Phase 3 `call_id` snapshot upserts / `parent_call_id` nested inserts).
    @discardableResult
    func upsertItem(
        _ item: PCodeTranscriptItem,
        insertAt: Int? = nil,
        reveal: Bool = false
    ) -> Int {
        let existingRawIdx = rawItemIndexByID[item.id]
        let existingRaw = existingRawIdx.map { rawItems[$0] }
        let wasActiveBefore = existingRaw.map { PCodeToolGrouper.isActive($0) } ?? false
        let isActiveAfter = PCodeToolGrouper.isActive(item)
        let isGroupableItem = PCodeToolGrouper.isGroupable(item) || (existingRaw.map { PCodeToolGrouper.isGroupable($0) } ?? false)

        // Capture a visual snapshot right before a live active tool/diff folds into its group.
        let foldSnapshot: FoldSnapshot? = (groupTools && reveal && isGroupableItem && wasActiveBefore && !isActiveAfter)
            ? captureFoldSnapshot(for: item.id)
            : nil

        // Update canonical `rawItems`.
        let finalRawIdx: Int
        if let rawIdx = existingRawIdx {
            let prev = rawItems[rawIdx]
            let bumpedRev = (prev.kind == item.kind) ? max(item.revision, prev.revision &+ 1) : item.revision
            rawItems[rawIdx] = PCodeTranscriptItem(
                id: item.id,
                payload: item.payload,
                spacingAfter: item.spacingAfter,
                revision: bumpedRev,
                depth: item.depth)
            finalRawIdx = rawIdx
        } else {
            let targetIdx = insertAt.map { min(max(0, $0), rawItems.count) } ?? rawItems.count
            if targetIdx == rawItems.count {
                rawItems.append(item)
                rawItemIndexByID[item.id] = targetIdx
            } else {
                rawItems.insert(item, at: targetIdx)
                rebuildRawItemIndexMap()
            }
            finalRawIdx = targetIdx
        }

        // Hidden thinking lives only in `rawItems` until the preference changes.
        if item.kind == .thinking && !showThinking {
            return max(0, items.count - 1)
        }

        // Fast path: ungrouped mode, OR non-groupable item appended at tail / updated in place.
        // Hidden thinking makes display indices differ from raw ones, so positional inserts re-project.
        if (!groupTools && (insertAt == nil || showThinking)) || (!isGroupableItem && (existingRawIdx != nil ? itemIndexByID[item.id] != nil : insertAt == nil)) {
            let updatedItem = rawItems[finalRawIdx]
            return upsertDisplayItemDirectly(updatedItem, insertAt: groupTools ? nil : insertAt, reveal: reveal)
        }

        // Grouped path: re-project and apply minimal diff.
        let revealIDs: Set<String> = (reveal && existingRawIdx == nil) ? [item.id] : []
        let pulseRawID: String? = (reveal && wasActiveBefore && !isActiveAfter && PCodeToolGrouper.isGroupable(item))
            ? item.id
            : nil
        syncProjectedItems(
            revealNewIDs: revealIDs,
            foldSnapshot: foldSnapshot,
            pulseForRawItemID: pulseRawID,
            animateShift: reveal && wasActiveBefore && !isActiveAfter)
        return itemIndexByID[item.id] ?? (groupIDByRawItemID[item.id].flatMap { itemIndexByID[$0] } ?? max(0, items.count - 1))
    }

    @discardableResult
    private func upsertDisplayItemDirectly(
        _ item: PCodeTranscriptItem,
        insertAt: Int?,
        reveal: Bool
    ) -> Int {
        notifySearchContentWillChange()
        if let existingIdx = itemIndexByID[item.id] {
            let existing = items[existingIdx]
            removeAttributedCache(for: item.id)
            if existing.kind == item.kind {
                items[existingIdx] = PCodeTranscriptItem(
                    id: item.id,
                    payload: item.payload,
                    spacingAfter: item.spacingAfter,
                    revision: max(item.revision, existing.revision &+ 1),
                    depth: item.depth)
                layoutEngine.updateSpacingAfter(at: existingIdx, spacingAfter: item.spacingAfter)
            } else {
                if let oldView = mountedViews.removeValue(forKey: item.id) {
                    oldView.resetForTranscriptReuse()
                    oldView.removeFromSuperview()
                    reusePools[oldView.itemKind, default: []].append(oldView)
                }
                appliedStates.removeValue(forKey: item.id)
                items[existingIdx] = item
                layoutEngine.replaceEntry(at: existingIdx, id: item.id, spacingAfter: item.spacingAfter)
            }
            invalidateSearchIndex()
            mountViews(in: visibleRect)
            trackRunningState(for: item)
            return existingIdx
        }

        let targetIdx = insertAt.map { min(max(0, $0), items.count) } ?? items.count
        let estHeight = estimatedHeight(for: item)
        if targetIdx == items.count {
            items.append(item)
            itemIndexByID[item.id] = targetIdx
            layoutEngine.append(id: item.id, height: estHeight, spacingAfter: item.spacingAfter, measured: false)
        } else {
            items.insert(item, at: targetIdx)
            rebuildItemIndexMap()
            layoutEngine.insert(at: targetIdx, id: item.id, height: estHeight, spacingAfter: item.spacingAfter, measured: false)
        }
        invalidateSearchIndex()

        if reveal {
            enqueueRevealIfNeeded(for: item, estimatedHeight: estHeight)
        }
        if stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }
        mountViews(in: visibleRect)
        trackRunningState(for: item)
        return targetIdx
    }

    private static func displayItemsMatch(_ lhs: PCodeTranscriptItem, _ rhs: PCodeTranscriptItem) -> Bool {
        guard lhs.id == rhs.id,
              lhs.kind == rhs.kind,
              lhs.depth == rhs.depth,
              abs(lhs.spacingAfter - rhs.spacingAfter) < 0.001
        else { return false }
        if case .toolGroup(let a) = lhs.payload, case .toolGroup(let b) = rhs.payload {
            return a == b
        }
        return lhs.revision == rhs.revision
    }

    private func syncProjectedItems(
        revealNewIDs: Set<String>,
        foldSnapshot: FoldSnapshot?,
        pulseForRawItemID: String? = nil,
        animateShift: Bool = false,
        animateExpandedGroupID: String? = nil
    ) {
        // First projection to discover target group ID for any absorb pulse.
        var projection = PCodeToolGrouper.project(
            rawItems: rawItems,
            groupTools: groupTools,
            hideThinking: !showThinking,
            expandedGroupIDs: expandedGroupIDs,
            absorbPulses: absorbPulseByGroupID)

        if let pulseRawID = pulseForRawItemID,
           let targetGroupID = projection.groupIDByRawItemID[pulseRawID]
        {
            absorbPulseByGroupID[targetGroupID, default: 0] &+= 1
            projection = PCodeToolGrouper.project(
                rawItems: rawItems,
                groupTools: groupTools,
                hideThinking: !showThinking,
                expandedGroupIDs: expandedGroupIDs,
                absorbPulses: absorbPulseByGroupID)
        }

        let oldItems = items
        let newItems = projection.items
        groupIDByRawItemID = projection.groupIDByRawItemID

        let oldCount = oldItems.count
        let newCount = newItems.count
        var prefix = 0
        while prefix < oldCount && prefix < newCount && Self.displayItemsMatch(oldItems[prefix], newItems[prefix]) {
            prefix += 1
        }
        var suffix = 0
        while suffix < (oldCount - prefix) && suffix < (newCount - prefix)
            && Self.displayItemsMatch(oldItems[oldCount - 1 - suffix], newItems[newCount - 1 - suffix])
        {
            suffix += 1
        }

        if prefix + suffix == oldCount && oldCount == newCount {
            return
        }

        // Capture where mounted rows are drawn now (frame plus any in-flight shift) so surviving
        // rows can glide smoothly when heights shift.
        var previousRowYs: [String: CGFloat] = [:]
        let shouldAnimateMotion = window != nil && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let previousClipOriginY = enclosingScrollView?.contentView.bounds.origin.y ?? 0
        let previousTotalHeight = layoutEngine.totalHeight
        if shouldAnimateMotion && (animateShift || animateExpandedGroupID != nil) {
            previousRowYs.reserveCapacity(mountedViews.count)
            for (id, view) in mountedViews {
                previousRowYs[id] = view.frame.minY + Self.inFlightShift(of: view)
            }
        }

        // Capture selection endpoints by stable item ID so selection never jumps onto wrong rows
        // when items are inserted/removed above it.
        let savedSelectionEndpoints: (anchorID: String, anchorOffset: Int, focusID: String, focusOffset: Int)?
        if let sel = selection,
           oldItems.indices.contains(sel.anchor.itemIndex),
           oldItems.indices.contains(sel.focus.itemIndex)
        {
            savedSelectionEndpoints = (
                oldItems[sel.anchor.itemIndex].id, sel.anchor.utf16Offset,
                oldItems[sel.focus.itemIndex].id, sel.focus.utf16Offset)
        } else {
            savedSelectionEndpoints = nil
        }

        // Capture top-visible anchor when scrolled up so layout mutations above the viewport
        // don't jump visible content.
        var savedViewportAnchor: (id: String, offsetFromTop: CGFloat, viewportTop: CGFloat)?
        if !stickyBottom.shouldProgrammaticallyPin,
           let scrollView = enclosingScrollView,
           !layoutEngine.entries.isEmpty
        {
            let viewportTop = scrollView.contentView.bounds.origin.y
            let vis = layoutEngine.visibleRange(in: CGRect(x: 0, y: viewportTop, width: 1, height: max(1, scrollView.contentView.bounds.height)))
            if let firstVis = vis.first, oldItems.indices.contains(firstVis) {
                savedViewportAnchor = (
                    id: oldItems[firstVis].id,
                    offsetFromTop: viewportTop - layoutEngine.entries[firstVis].yOffset,
                    viewportTop: viewportTop)
            }
        }

        let oldMid = prefix..<(oldCount - suffix)
        let newMid = prefix..<(newCount - suffix)
        let newMidIDs = Set(newItems[newMid].map(\.id))

        notifySearchContentWillChange()
        for idx in oldMid {
            let oldID = oldItems[idx].id
            if oldMid.count == newMid.count,
               Self.displayItemsMatch(oldItems[idx], newItems[idx])
            {
                continue
            }
            removeAttributedCache(for: oldID)
            if !newMidIDs.contains(oldID) {
                revealQueue.remove(itemID: oldID)
                accessibilityRowProxies.removeValue(forKey: oldID)
                if let oldView = mountedViews.removeValue(forKey: oldID) {
                    oldView.resetForTranscriptReuse()
                    oldView.removeFromSuperview()
                    reusePools[oldView.itemKind, default: []].append(oldView)
                }
                appliedStates.removeValue(forKey: oldID)
                runningToolIDs.remove(oldID)
            } else {
                appliedStates.removeValue(forKey: oldID)
            }
        }

        if oldMid.count == newMid.count {
            for offset in 0..<newMid.count {
                let idx = prefix + offset
                let oldItem = items[idx]
                let newItem = newItems[idx]
                if Self.displayItemsMatch(oldItem, newItem) {
                    continue
                }
                if oldItem.id == newItem.id && oldItem.kind == newItem.kind {
                    // Only `.toolGroup` (compared by payload equality, not revision) bumps its
                    // revision locally; raw-backed items keep `newItem.revision` so `rawItems`
                    // and `items` never drift and break minimal prefix/suffix diffing.
                    let nextRevision: UInt64 = (newItem.kind == .toolGroup)
                        ? max(newItem.revision, oldItem.revision &+ 1)
                        : newItem.revision
                    items[idx] = PCodeTranscriptItem(
                        id: newItem.id,
                        payload: newItem.payload,
                        spacingAfter: newItem.spacingAfter,
                        revision: nextRevision,
                        depth: newItem.depth)
                    layoutEngine.updateSpacingAfter(at: idx, spacingAfter: newItem.spacingAfter)
                } else {
                    items[idx] = newItem
                    layoutEngine.replaceEntry(at: idx, id: newItem.id, spacingAfter: newItem.spacingAfter)
                }
            }
        } else {
            for idx in oldMid.reversed() {
                layoutEngine.remove(at: idx)
            }
            items.replaceSubrange(oldMid, with: newItems[newMid])
            for idx in newMid {
                let it = items[idx]
                layoutEngine.insert(
                    at: idx,
                    id: it.id,
                    height: estimatedHeight(for: it),
                    spacingAfter: it.spacingAfter,
                    measured: false)
            }
        }

        rebuildItemIndexMap()
        invalidateSearchIndex()
        remapSelection(from: savedSelectionEndpoints)

        // Compensate scroll origin when scrolled up and rows above the viewport changed height.
        if let anchor = savedViewportAnchor,
           let newAnchorIdx = itemIndexByID[anchor.id],
           layoutEngine.entries.indices.contains(newAnchorIdx)
        {
            let desiredTop = layoutEngine.entries[newAnchorIdx].yOffset + anchor.offsetFromTop
            let scrollDelta = desiredTop - anchor.viewportTop
            if abs(scrollDelta) > 0.5 {
                applyDocumentHeight(
                    layoutEngine.totalHeight,
                    scrollOriginDelta: scrollDelta,
                    pinIfNeeded: false,
                    allowSmoothGlide: false)
            }
        }

        for idx in newMid {
            let it = items[idx]
            if case .tool(let call, _) = it.payload, call.isRunning && call.startedAt != nil {
                runningToolIDs.insert(it.id)
            } else {
                runningToolIDs.remove(it.id)
            }
            if revealNewIDs.contains(it.id) {
                enqueueRevealIfNeeded(for: it, estimatedHeight: estimatedHeight(for: it))
            }
        }
        syncLiveRunningTimer()
        if !revealNewIDs.isEmpty && stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }

        mountViews(in: visibleRect)

        // CoreAnimation polish (GPU-only, never mutates layout). Motion is measured on screen:
        // pinned to the bottom, a fold also moves the scroll origin, so document-space deltas
        // would slide rows that never moved and snap the ones that did.
        if shouldAnimateMotion {
            let originShift = (enclosingScrollView?.contentView.bounds.origin.y ?? 0) - previousClipOriginY
            if let snap = foldSnapshot,
               let targetGroupID = groupIDByRawItemID[snap.rawItemID],
               let groupView = mountedViews[targetGroupID]
            {
                animateFoldSnapshot(snap, into: groupView.frame, originShift: originShift)
            }
            if !previousRowYs.isEmpty {
                animateVerticalShifts(
                    from: previousRowYs,
                    originShift: originShift,
                    unchangedAbove: prefix,
                    unchangedFrom: newCount - suffix,
                    tailShift: previousTotalHeight - layoutEngine.totalHeight)
            }
            if let expandedID = animateExpandedGroupID,
               let groupIdx = itemIndexByID[expandedID],
               case .toolGroup(let payload) = items[groupIdx].payload,
               payload.isExpanded
            {
                animateExpandedGroupChildren(payload.entries)
            }
        }
    }

    private func clearActiveFoldGhostLayers() {
        for ghost in activeFoldGhostLayers {
            ghost.removeAllAnimations()
            ghost.removeFromSuperlayer()
        }
        activeFoldGhostLayers.removeAll(keepingCapacity: false)
    }

    /// The `transform.translation.y` a row is currently drawn at by an in-flight shift animation.
    private static func inFlightShift(of view: NSView) -> CGFloat {
        guard let value = view.layer?.presentation()?.value(forKeyPath: "transform.translation.y") as? NSNumber
        else { return 0 }
        return CGFloat(value.doubleValue)
    }

    private func animateFoldSnapshot(_ snap: FoldSnapshot, into targetFrame: CGRect, originShift: CGFloat) {
        guard let hostLayer = layer else { return }
        let ghost = snap.layer
        hostLayer.addSublayer(ghost)
        activeFoldGhostLayers.append(ghost)

        let duration: CFTimeInterval = 0.22
        let timing = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)

        let moveY = CABasicAnimation(keyPath: "position.y")
        // Start where the row was on screen, even if the scroll origin moved with the fold.
        moveY.fromValue = snap.originFrame.midY + originShift
        moveY.toValue = targetFrame.midY

        let scaleY = CABasicAnimation(keyPath: "transform.scale.y")
        scaleY.fromValue = 1.0
        scaleY.toValue = 0.62

        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.95
        fade.toValue = 0.0

        let group = CAAnimationGroup()
        group.animations = [moveY, scaleY, fade]
        group.duration = duration
        group.timingFunction = timing
        group.fillMode = .forwards
        group.isRemovedOnCompletion = false

        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            ghost.removeFromSuperlayer()
            self?.activeFoldGhostLayers.removeAll { $0 === ghost }
        }
        ghost.add(group, forKey: "pcode.tool.fold")
        CATransaction.commit()
    }

    /// Slide each mounted row from where it was drawn on screen to its new frame.
    ///
    /// `previousRowYs` holds each row's old drawn document Y; `originShift` is how far the scroll
    /// origin moved meanwhile. Rows mounted only now (scrolled into view by the change) have no
    /// old Y, so it is inferred: rows before `unchangedAbove` kept their document Y, and rows from
    /// `unchangedFrom` on moved by `tailShift`. Without that, a row revealed above a fold would
    /// sit still while its neighbours slide, overlapping it.
    private func animateVerticalShifts(
        from previousRowYs: [String: CGFloat],
        originShift: CGFloat,
        unchangedAbove: Int,
        unchangedFrom: Int,
        tailShift: CGFloat
    ) {
        let timing = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
        for (id, view) in mountedViews {
            let oldY: CGFloat
            if let known = previousRowYs[id] {
                oldY = known
            } else if let idx = itemIndexByID[id], idx < unchangedAbove {
                oldY = view.frame.minY
            } else if let idx = itemIndexByID[id], idx >= unchangedFrom {
                oldY = view.frame.minY + tailShift
            } else {
                continue
            }
            let dy = oldY + originShift - view.frame.minY
            guard abs(dy) > 1.0 && abs(dy) < 400, let viewLayer = view.layer else {
                // A row that now stays put must not keep sliding toward a stale target.
                view.layer?.removeAnimation(forKey: "pcode.row.shift")
                continue
            }
            let anim = CABasicAnimation(keyPath: "transform.translation.y")
            anim.fromValue = dy
            anim.toValue = 0
            anim.duration = 0.20
            anim.timingFunction = timing
            viewLayer.add(anim, forKey: "pcode.row.shift")
        }
    }

    private func animateExpandedGroupChildren(_ entries: [PCodeToolGroupEntry]) {
        let timing = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
        for (idx, entry) in entries.prefix(12).enumerated() {
            guard let view = mountedViews[entry.id], let viewLayer = view.layer else { continue }
            let fade = CABasicAnimation(keyPath: "opacity")
            fade.fromValue = 0.0
            fade.toValue = 1.0

            let slide = CABasicAnimation(keyPath: "transform.translation.y")
            slide.fromValue = -6.0
            slide.toValue = 0.0

            let group = CAAnimationGroup()
            group.animations = [fade, slide]
            group.duration = 0.18
            group.beginTime = CACurrentMediaTime() + Double(idx) * 0.012
            group.timingFunction = timing
            group.fillMode = .backwards
            viewLayer.add(group, forKey: "pcode.group.childReveal")
        }
    }

    /// Remove an item by stable `id`, recycling its mounted view and updating layout.
    func removeItem(id: String) {
        if let rawIdx = rawItemIndexByID[id] {
            rawItems.remove(at: rawIdx)
            rebuildRawItemIndexMap()
        }
        if groupTools {
            syncProjectedItems(revealNewIDs: [], foldSnapshot: nil, animateShift: false)
            return
        }
        guard let idx = itemIndexByID[id] else { return }
        notifySearchContentWillChange()
        revealQueue.remove(itemID: id)
        removeAttributedCache(for: id)
        accessibilityRowProxies.removeValue(forKey: id)
        if let oldView = mountedViews.removeValue(forKey: id) {
            oldView.resetForTranscriptReuse()
            oldView.removeFromSuperview()
            reusePools[oldView.itemKind, default: []].append(oldView)
        }
        appliedStates.removeValue(forKey: id)
        items.remove(at: idx)
        rebuildItemIndexMap()
        layoutEngine.remove(at: idx)
        invalidateSearchIndex()
        clampSelectionToCurrentItems()
        runningToolIDs.remove(id)
        mountViews(in: visibleRect)
        syncLiveRunningTimer()
    }

    /// Start a new turn by appending the user prompt. Pass `forcePin: true` for user-submitted
    /// prompts so the view re-pins to bottom, or `false` when replaying/streaming background turns.
    func beginStreamingTurn(prompt: String, isSide: Bool = false, forcePin: Bool = true, reveal: Bool = true) {
        finishStreamingMessage(reveal: reveal)
        streamingTurnCounter += 1
        let item = PCodeTranscriptItem(
            id: "stream-turn-\(streamingTurnCounter)-prompt",
            payload: .prompt(label: isSide ? "/btw" : "", text: prompt, isSide: isSide),
            spacingAfter: 10)
        if forcePin {
            stickyBottom.pinToBottom()
            setHasUnreadStreamingContent(false)
        }
        upsertItem(item, reveal: reveal)
    }

    /// Begin streaming a new assistant Markdown message at the tail of the transcript.
    func beginStreamingMessage(id: String) {
        finishStreamingMessage()
        activeStream = ActiveStreamingMessage(
            id: id,
            settler: PCodeMarkdownSettler(),
            emittedBlockCount: 0,
            codeGroupCounter: 0)
    }

    /// Append a token delta to the active streaming message. Only settled top-level blocks
    /// are committed to the layout and reveal queue (zero raw `**bold` snapping).
    func appendStreamingDelta(_ delta: String, reveal: Bool = true) {
        guard !delta.isEmpty else { return }
        if activeStream == nil {
            streamingMessageCounter += 1
            beginStreamingMessage(id: "stream-msg-\(streamingMessageCounter)")
        }
        guard var stream = activeStream else { return }
        let settled = stream.settler.append(delta)
        activeStream = stream
        if !settled.isEmpty {
            commitSettledBlocks(settled, reveal: reveal)
        }
    }

    /// Finalize the active streaming message: settle any remaining tail block into the reveal
    /// queue (never abruptly snapping/flushing active reveals) and attach Copy Markdown.
    func finishStreamingMessage(reveal: Bool = true) {
        guard var stream = activeStream else { return }
        let remaining = stream.settler.finish()
        activeStream = stream
        if !remaining.isEmpty {
            commitSettledBlocks(remaining, reveal: reveal)
        }
        guard let finalStream = activeStream else { return }
        // Every prose block of the finished message offers the whole answer's Markdown,
        // so its Copy button shows wherever in the answer the pointer is.
        let fullMarkdown = finalStream.settler.fullMarkdown
        if !fullMarkdown.isEmpty {
            for blockIndex in 0..<finalStream.emittedBlockCount {
                let itemID = Self.streamingItemID(messageID: finalStream.id, blockIndex: blockIndex)
                if let rawIdx = rawItemIndexByID[itemID],
                   case .prose(let md, let refDefs, _) = rawItems[rawIdx].payload
                {
                    rawItems[rawIdx] = PCodeTranscriptItem(
                        id: itemID,
                        payload: .prose(markdown: md, referenceDefinitions: refDefs, copyMarkdown: fullMarkdown),
                        spacingAfter: rawItems[rawIdx].spacingAfter,
                        revision: rawItems[rawIdx].revision &+ 1,
                        depth: rawItems[rawIdx].depth)
                }
                guard let idx = itemIndexByID[itemID],
                      case .prose(let md, let refDefs, _) = items[idx].payload
                else { continue }
                items[idx] = PCodeTranscriptItem(
                    id: itemID,
                    payload: .prose(markdown: md, referenceDefinitions: refDefs, copyMarkdown: fullMarkdown),
                    spacingAfter: items[idx].spacingAfter,
                    revision: items[idx].revision &+ 1,
                    depth: items[idx].depth)
            }
        }
        activeStream = nil
        mountViews(in: visibleRect)
    }

    private static func streamingItemID(messageID: String, blockIndex: Int) -> String {
        "\(messageID)-b\(blockIndex)"
    }

    private func commitSettledBlocks(_ blocks: [PCodeMarkdownBlock], reveal: Bool) {
        guard var stream = activeStream, !blocks.isEmpty else { return }
        var activeCodeGroupID: String?
        var appended: [PCodeTranscriptItem] = []
        appended.reserveCapacity(blocks.count)

        for block in blocks {
            let subIdx = stream.emittedBlockCount
            stream.emittedBlockCount += 1
            let itemID = Self.streamingItemID(messageID: stream.id, blockIndex: subIdx)

            switch block {
            case .prose(let proseMD, let refDefs):
                activeCodeGroupID = nil
                appended.append(PCodeTranscriptItem(
                    id: itemID,
                    payload: .prose(markdown: proseMD, referenceDefinitions: refDefs, copyMarkdown: nil),
                    spacingAfter: 8))
            case .codeBlock(let lang, let code, let fullCode, let chunkIdx, let totalChunks):
                if chunkIdx == 0 {
                    activeCodeGroupID = "\(stream.id)-code-\(stream.codeGroupCounter)"
                    stream.codeGroupCounter += 1
                }
                let isLastChunk = (chunkIdx == totalChunks - 1)
                appended.append(PCodeTranscriptItem(
                    id: itemID,
                    payload: .codeBlock(
                        language: lang,
                        code: code,
                        fullCopyCode: fullCode,
                        showHeader: chunkIdx == 0,
                        groupID: totalChunks > 1 ? activeCodeGroupID : nil),
                    spacingAfter: isLastChunk ? 10 : 0))
            case .table(let table):
                activeCodeGroupID = nil
                appended.append(PCodeTranscriptItem(
                    id: itemID,
                    payload: .table(table),
                    spacingAfter: 10))
            }
        }

        activeStream = stream
        notifySearchContentWillChange()
        for item in appended {
            let rawIdx = rawItems.count
            rawItems.append(item)
            rawItemIndexByID[item.id] = rawIdx
            let estHeight = estimatedHeight(for: item)
            let idx = items.count
            items.append(item)
            itemIndexByID[item.id] = idx
            layoutEngine.append(id: item.id, height: estHeight, spacingAfter: item.spacingAfter, measured: false)
            if reveal {
                enqueueRevealIfNeeded(for: item, estimatedHeight: estHeight)
            }
        }
        invalidateSearchIndex()
        if stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }
        mountViews(in: visibleRect)
    }

    private func enqueueRevealIfNeeded(for item: PCodeTranscriptItem, estimatedHeight: CGFloat) {
        guard window != nil, paceMode != .off else { return }
        let style: PCodeRevealQueue.Style
        switch paceMode {
        case .off:
            return
        case .rows:
            style = .curtain
        case .typed:
            // Ghostty Smooth parity: ink-sweep for prose, curtain for code/tables/tools/diffs/prompts.
            style = (item.kind == .prose) ? .inkSweep : .curtain
        }
        let initialExtent: CGFloat = (style == .inkSweep)
            ? max(120, CGFloat(item.plainText.count) * 7.5)
            : max(20, estimatedHeight)
        revealQueue.enqueue(itemID: item.id, style: style, targetExtent: initialExtent)
        ensureDisplayLinkRunning()
    }

    /// Append a tool card to the live stream (settling any preceding open assistant message).
    func appendStreamingToolCall(_ call: PCodePeekToolCall) {
        finishStreamingMessage()
        let item = PCodeTranscriptItem(
            id: "stream-turn-\(streamingTurnCounter)-tool-\(call.id)-\(rawItems.count)",
            payload: .tool(call, isExpanded: false),
            spacingAfter: 5)
        upsertItem(item, reveal: true)
    }

    /// Finalize the current streaming turn with its terminal status.
    func finishStreamingTurn(status: PCodePeekTurn.Status, reveal: Bool = true) {
        finishStreamingMessage(reveal: reveal)
        if status != .complete {
            let item = PCodeTranscriptItem(
                id: "stream-turn-\(streamingTurnCounter)-status-\(rawItems.count)",
                payload: .status(status),
                spacingAfter: 14)
            upsertItem(item, reveal: reveal)
        } else if let lastIdx = items.indices.last {
            if let lastRawIdx = rawItems.indices.last {
                rawItems[lastRawIdx] = rawItems[lastRawIdx].withSpacingAfter(18)
            }
            items[lastIdx] = items[lastIdx].withSpacingAfter(18)
            layoutEngine.updateSpacingAfter(at: lastIdx, spacingAfter: 18)
            mountViews(in: visibleRect)
        }
    }

    /// Re-pin to bottom and scroll immediately (used by the floating "Jump to bottom" pill).
    func scrollToBottomAndPin() {
        resetSmoothGlide()
        stickyBottom.pinToBottom()
        setHasUnreadStreamingContent(false)
        resetBottomSlack()
        applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: true, allowSmoothGlide: false)
        mountViews(in: visibleRect)
        if !revealQueue.isEmpty {
            ensureDisplayLinkRunning()
        }
    }

    /// Re-pin to bottom without smooth glide when the bottom dock inset changes.
    func repinToBottomForInsetChange() {
        guard stickyBottom.shouldProgrammaticallyPin else { return }
        applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: true, allowSmoothGlide: false)
        mountViews(in: visibleRect)
    }

    /// Explicitly unpin sticky-bottom tracking (e.g. Find jump or programmatic scroll-up).
    func unpinFromBottom() {
        resetSmoothGlide()
        stickyBottom.unpinForUserAction()
    }

    /// Programmatically update the model-owned selection (e.g. Find or tests).
    func setSelection(_ newSelection: PCodeTranscriptSelection?) {
        selection = newSelection
        updateMountedSelectionHighlights()
    }

    private func setHasUnreadStreamingContent(_ value: Bool) {
        guard hasUnreadStreamingContent != value else { return }
        hasUnreadStreamingContent = value
        onJumpToBottomVisibilityChanged?(value && stickyBottom.mode == .userScrolledUp)
    }

    /// Update typography or theme, invalidating heights while preserving the top-visible scroll anchor.
    func updateAppearance(typography newTypography: PCodeTypographySnapshot, theme newTheme: PCodeTranscriptTheme) {
        let needsRemeasure = newTypography != typography
        let themeChanged = newTheme != theme
        guard needsRemeasure || themeChanged else { return }
        typography = newTypography
        theme = newTheme
        appearanceGeneration &+= 1
        clearAttributedCache()
        let nsAppearance = NSAppearance(named: newTheme.isDark ? .darkAqua : .aqua)
        appearance = nsAppearance
        enclosingScrollView?.appearance = nsAppearance

        if needsRemeasure {
            layoutEngine.invalidateAllMeasurements()
        }
        remountVisibleViews()
    }

    /// Responsive-scrolling hook: AppKit passes the visible rect plus its own overscan band.
    override func prepareContent(in rect: NSRect) {
        super.prepareContent(in: rect)
        mountViews(in: rect)
    }

    override func setFrameSize(_ newSize: NSSize) {
        let widthChanged = abs(newSize.width - lastContentWidth) > 0.5 && newSize.width > 1
        super.setFrameSize(newSize)
        if widthChanged {
            lastContentWidth = newSize.width
            // Rewrapping is a resize, not streaming: let the view settle at the true bottom.
            resetBottomSlack()
            layoutEngine.invalidateAllMeasurements()
            remountVisibleViews()
        }
    }

    /// Drop the slack and forget the last content height, so the next pinned apply lands on the
    /// content's true bottom instead of reading a reload or resize as a streaming shrink.
    private func resetBottomSlack() {
        bottomSlack = 0
        lastAppliedContentHeight = 0
    }

    /// Once the reader scrolls up, trim slack the viewport no longer reaches into, so the
    /// document ends at the content again and scrolling back down re-pins at the last line.
    /// Only blank space below the viewport is removed, so nothing on screen moves.
    private func trimBottomSlackAfterUserScroll(in scrollView: NSScrollView) {
        guard bottomSlack > 0, stickyBottom.mode == .userScrolledUp else { return }
        let clipView = scrollView.contentView
        let contentHeight = layoutEngine.totalHeight
        let reachedSlack = max(
            0, clipView.bounds.origin.y + clipView.bounds.height - scrollView.contentInsets.bottom - contentHeight)
        guard reachedSlack < bottomSlack - 0.5 else { return }
        bottomSlack = reachedSlack
        let targetHeight = max(contentHeight + bottomSlack, unobstructedViewportHeight(in: scrollView))
        isApplyingDocumentHeight = true
        frame.size = CGSize(width: frame.width, height: targetHeight)
        lastClipOriginY = clipView.bounds.origin.y
        isApplyingDocumentHeight = false
    }

    // MARK: - Scroll Notifications (called by PCodeTranscriptScrollView)

    func handleWillStartLiveScroll() {
        resetSmoothGlide()
        stickyBottom.didStartLiveScroll()
        lastClipOriginY = enclosingScrollView?.contentView.bounds.origin.y ?? 0
    }

    func handleDidEndLiveScroll() {
        guard let scrollView = enclosingScrollView else { return }
        stickyBottom.didEndLiveScroll(distanceFromBottom: distanceFromBottom(in: scrollView))
        if stickyBottom.mode == .pinnedToBottom {
            setHasUnreadStreamingContent(false)
        }
    }

    /// Tracks user scroll movement for both live (trackpad) and legacy wheel scrolls.
    /// This is the only place that reads movement: AppKit posts `boundsDidChange` before
    /// `didLiveScroll`, so a separate live-scroll handler would always see a zero delta
    /// and never unpin, letting the next streamed chunk yank the reader back down.
    func handleBoundsDidChange() {
        guard let scrollView = enclosingScrollView else { return }
        let originY = scrollView.contentView.bounds.origin.y
        if isApplyingDocumentHeight {
            lastClipOriginY = originY
            return
        }
        let delta = originY - lastClipOriginY
        if abs(delta) > 0.5 {
            let dist = distanceFromBottom(in: scrollView)
            // dist + delta is the distance before this move. Moving up from past the bottom
            // (negative distance) is the elastic bounce-back of an overshoot, not the user
            // scrolling up; moving up from exactly the bottom is.
            let movedUp = delta < 0 && dist + delta > -0.5
            if stickyBottom.isLiveScrolling {
                stickyBottom.didLiveScroll(distanceFromBottom: dist, movedUpward: movedUp)
            } else {
                stickyBottom.didLegacyWheelScroll(distanceFromBottom: dist, movedUpward: movedUp)
                if stickyBottom.mode == .pinnedToBottom {
                    setHasUnreadStreamingContent(false)
                }
            }
            if movedUp && stickyBottom.mode == .userScrolledUp {
                resetSmoothGlide()
            }
            lastClipOriginY = originY
            trimBottomSlackAfterUserScroll(in: scrollView)
        }
        mountViews(in: visibleRect)
        if !revealQueue.isEmpty, window != nil {
            ensureDisplayLinkRunning()
        }
    }

    // MARK: - Mounting & Reuse Pool

    private func unobstructedViewportHeight(in scrollView: NSScrollView?) -> CGFloat {
        guard let scrollView else { return 0 }
        return max(
            0,
            scrollView.contentView.bounds.height
                - scrollView.contentInsets.top
                - scrollView.contentInsets.bottom)
    }

    private func mountViews(in rect: NSRect, pass: Int = 0) {
        guard !items.isEmpty else {
            let unobstructedH = unobstructedViewportHeight(in: enclosingScrollView)
            let targetHeight = max(layoutEngine.totalHeight, unobstructedH)
            if abs(bounds.height - targetHeight) > 0.5 {
                applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: false, allowSmoothGlide: false)
            }
            return
        }
        let availableWidth = max(bounds.width, enclosingScrollView?.contentView.bounds.width ?? 0)
        guard availableWidth > Self.horizontalPadding * 2 + 1 else { return }
        let contentWidth = max(1, availableWidth - Self.horizontalPadding * 2)
        let range = layoutEngine.visibleRange(in: rect)
        lastMountedRange = range

        var visibleIDs = Set<String>()
        visibleIDs.reserveCapacity(range.count)
        for idx in range {
            visibleIDs.insert(items[idx].id)
        }

        // Recycle views that scrolled out of `rect`.
        for (id, view) in mountedViews where !visibleIDs.contains(id) {
            view.resetForTranscriptReuse()
            view.removeFromSuperview()
            reusePools[view.itemKind, default: []].append(view)
            mountedViews.removeValue(forKey: id)
            appliedStates.removeValue(forKey: id)
        }

        let viewportTop = enclosingScrollView?.contentView.bounds.origin.y ?? 0
        var accumulatedScrollDelta: CGFloat = 0

        for idx in range {
            let item = items[idx]
            let rowView: any PCodeTranscriptRowHosting
            var isFreshMount = false

            if let existing = mountedViews[item.id], existing.itemKind == item.kind {
                rowView = existing
            } else {
                if let existing = mountedViews.removeValue(forKey: item.id) {
                    existing.resetForTranscriptReuse()
                    existing.removeFromSuperview()
                    reusePools[existing.itemKind, default: []].append(existing)
                }
                appliedStates.removeValue(forKey: item.id)
                rowView = dequeueRowView(for: item.kind)
                addSubview(rowView)
                mountedViews[item.id] = rowView
                isFreshMount = true
            }

            let indent = CGFloat(item.depth) * 16
            let effectiveWidth = max(1, contentWidth - indent)
            let targetState = AppliedRowState(
                itemID: item.id,
                revision: item.revision,
                width: effectiveWidth,
                appearanceGeneration: appearanceGeneration)

            let needsConfigure = isFreshMount
                || appliedStates[item.id] != targetState
                || !layoutEngine.entries[idx].isMeasured

            if needsConfigure {
                let measured = applyRowView(rowView, item: item, index: idx, width: effectiveWidth)
                appliedStates[item.id] = targetState

                let delta = layoutEngine.updateHeight(
                    at: idx,
                    measuredHeight: measured,
                    viewportTop: viewportTop + accumulatedScrollDelta,
                    isPinnedToBottom: stickyBottom.shouldProgrammaticallyPin)
                accumulatedScrollDelta += delta.scrollOriginDelta

                // Update reveal queue's exact measured extent (horizontal line span for ink-sweep,
                // or pixel-rounded height for curtain).
                if let revealState = revealQueue.state(for: item.id) {
                    switch revealState.style {
                    case .inkSweep:
                        let span = rowView.primaryTextView?.totalInkSweepLength ?? max(120, measured * 4)
                        revealQueue.updateExtent(for: item.id, targetExtent: span)
                    case .curtain:
                        revealQueue.updateExtent(for: item.id, targetExtent: measured)
                    }
                }
            } else {
                let len = rowView.selectableTextLength
                rowView.setSelectedRange(selection?.localRange(forItem: idx, utf16Length: len))
            }

            rowView.setHovered(hoveredItemID == item.id)

            // Position `rowView.frame` BEFORE applying curtain masks so `bounds` is final.
            let entry = layoutEngine.entries[idx]
            let targetFrame = CGRect(
                x: Self.horizontalPadding + indent,
                y: entry.yOffset,
                width: effectiveWidth,
                height: entry.height)
            if rowView.frame != targetFrame {
                rowView.frame = targetFrame
            }

            // Apply active reveal state (or clear if fully revealed).
            if let revealState = revealQueue.state(for: item.id) {
                switch revealState.style {
                case .inkSweep:
                    rowView.primaryTextView?.inkSweepFrontier = revealState.frontier
                    rowView.setCurtainRevealProgress(nil)
                case .curtain:
                    rowView.primaryTextView?.inkSweepFrontier = nil
                    rowView.setCurtainRevealProgress(revealState.curtainProgress)
                }
            } else {
                rowView.primaryTextView?.inkSweepFrontier = nil
                rowView.setCurtainRevealProgress(nil)
            }
        }

        let unobstructedH = unobstructedViewportHeight(in: enclosingScrollView)
        let targetHeight = max(layoutEngine.totalHeight + bottomSlack, unobstructedH)
        if abs(bounds.height - targetHeight) > 0.5 || abs(accumulatedScrollDelta) > 0.5 {
            applyDocumentHeight(
                layoutEngine.totalHeight,
                scrollOriginDelta: accumulatedScrollDelta,
                pinIfNeeded: stickyBottom.shouldProgrammaticallyPin)
            if pass < 4, layoutEngine.visibleRange(in: visibleRect) != range {
                mountViews(in: visibleRect, pass: pass + 1)
            }
        }
    }

    private func applyRowView(
        _ rowView: any PCodeTranscriptRowHosting,
        item: PCodeTranscriptItem,
        index: Int,
        width: CGFloat
    ) -> CGFloat {
        switch (rowView, item.payload) {
        case (let view as PCodePromptRowView, .prompt(let label, let text, let isSide)):
            let attr = cachedPromptAttributedString(for: item.id, label: label, text: text, isSide: isSide)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                attributedText: attr,
                isSidePrompt: isSide,
                style: promptStyle,
                width: width,
                theme: theme,
                selectedRange: localSel)

        case (let view as PCodeProseRowView, .prose(let markdown, let refDefs, let copyMarkdown)):
            view.pasteboard = pasteboard
            let attr = cachedProseAttributedString(for: item.id, markdown: markdown, referenceDefinitions: refDefs)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                attributedText: attr,
                copyMarkdown: copyMarkdown,
                width: width,
                theme: theme,
                selectedRange: localSel)

        case (let view as PCodeCodeBlockView, .codeBlock(let lang, let code, let fullCopy, let showHeader, _)):
            let localSel = selection?.localRange(forItem: index, utf16Length: (code as NSString).length)
            return view.configure(
                code: code,
                fullCopyCode: fullCopy,
                language: lang,
                showHeader: showHeader,
                width: width,
                typography: typography,
                theme: theme,
                highlighter: highlighter,
                documentView: self,
                selectedRange: localSel)

        case (let view as PCodeTableBlockView, .table(let table)):
            let localSel = selection?.localRange(forItem: index, utf16Length: (table.plainText as NSString).length)
            return view.configure(
                table: table,
                width: width,
                typography: typography,
                theme: theme,
                documentView: self,
                selectedRange: localSel)

        case (let view as PCodeToolCardView, .tool(let call, let isExpanded)):
            let attr = cachedToolAttributedString(for: item.id, call: call, isExpanded: isExpanded)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                call: call,
                isExpanded: isExpanded,
                attributedText: attr,
                width: width,
                theme: theme,
                selectedRange: localSel,
                onToggle: { [weak self] in
                    self?.toggleToolExpansion(id: item.id)
                })

        case (let view as PCodeToolGroupCardView, .toolGroup(let payload)):
            let attr = cachedToolGroupAttributedString(for: item.id, payload: payload)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                payload: payload,
                attributedText: attr,
                width: width,
                theme: theme,
                selectedRange: localSel,
                onToggle: { [weak self] in
                    self?.toggleToolGroupExpansion(id: item.id)
                })

        case (let view as PCodeDiffCardView, .diff(let payload, let isExpanded)):
            let attr = isExpanded
                ? cachedDiffAttributedString(for: item.id, payload: payload)
                : NSAttributedString()
            let localSel = isExpanded ? selection?.localRange(forItem: index, utf16Length: attr.length) : nil
            return view.configure(
                payload: payload,
                isExpanded: isExpanded,
                attributedPatch: attr,
                width: width,
                typography: typography,
                theme: theme,
                documentView: self,
                selectedRange: localSel,
                onToggle: { [weak self] in
                    self?.toggleDiffExpansion(id: item.id)
                })

        case (let view as PCodeThinkingCardView, .thinking(let thinking)):
            let attr = cachedThinkingAttributedString(for: item.id, thinking: thinking)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                thinking: thinking,
                attributedText: attr,
                width: width,
                typography: typography,
                theme: theme,
                selectedRange: localSel)

        case (let view as PCodeStatusRowView, .status(let status)):
            let attr = cachedStatusAttributedString(for: item.id, status: status)
            let localSel = selection?.localRange(forItem: index, utf16Length: attr.length)
            return view.configure(
                attributedText: attr,
                width: width,
                selectedRange: localSel)

        default:
            return 0
        }
    }

    private func toggleToolGroupExpansion(id: String) {
        guard let index = itemIndexByID[id],
              items.indices.contains(index),
              case .toolGroup(let payload) = items[index].payload
        else { return }
        let willExpand = !payload.isExpanded
        if willExpand {
            stickyBottom.unpinForUserAction()
            expandedGroupIDs.insert(id)
        } else {
            expandedGroupIDs.remove(id)
        }
        syncProjectedItems(
            revealNewIDs: [],
            foldSnapshot: nil,
            animateShift: true,
            animateExpandedGroupID: willExpand ? id : nil)
        if let proxy = accessibilityRowProxies[id],
           let updatedIdx = itemIndexByID[id],
           items.indices.contains(updatedIdx),
           case .toolGroup(let updatedPayload) = items[updatedIdx].payload
        {
            let state = updatedPayload.isExpanded ? "expanded" : "collapsed"
            proxy.setAccessibilityLabel("\(updatedPayload.plainSummaryText) (\(state))")
        }
        NSAccessibility.post(element: self, notification: .layoutChanged)
    }

    private func toggleToolExpansion(id: String) {
        guard let index = itemIndexByID[id],
              items.indices.contains(index),
              case .tool(let call, let isExpanded) = items[index].payload
        else { return }
        // Keep sticky-bottom pinned when expanding a live running command so streaming
        // output stays in view; unpin for historical settled cards.
        if !isExpanded && !call.isRunning {
            stickyBottom.unpinForUserAction()
        }
        notifySearchContentWillChange()
        removeAttributedCache(for: id)
        if let rawIdx = rawItemIndexByID[id], rawItems.indices.contains(rawIdx) {
            rawItems[rawIdx] = PCodeTranscriptItem(
                id: id,
                payload: .tool(call, isExpanded: !isExpanded),
                spacingAfter: rawItems[rawIdx].spacingAfter,
                revision: rawItems[rawIdx].revision &+ 1,
                depth: rawItems[rawIdx].depth)
        }
        items[index] = PCodeTranscriptItem(
            id: id,
            payload: .tool(call, isExpanded: !isExpanded),
            spacingAfter: items[index].spacingAfter,
            revision: items[index].revision &+ 1,
            depth: items[index].depth)
        invalidateSearchIndex()
        mountViews(in: visibleRect)
        // If collapsing a long tool card from its bottom button, unpin and keep its top visible.
        if isExpanded, let scrollView = enclosingScrollView, layoutEngine.entries.indices.contains(index) {
            let entry = layoutEngine.entries[index]
            let vis = scrollView.documentVisibleRect
            if entry.yOffset < vis.minY {
                unpinFromBottom()
                let targetY = max(-scrollView.contentInsets.top, entry.yOffset - 12)
                isApplyingDocumentHeight = true
                scrollView.contentView.scroll(to: CGPoint(x: 0, y: targetY))
                scrollView.reflectScrolledClipView(scrollView.contentView)
                lastClipOriginY = scrollView.contentView.bounds.origin.y
                isApplyingDocumentHeight = false
                mountViews(in: visibleRect)
            }
        }
    }

    private func toggleDiffExpansion(id: String) {
        guard let index = itemIndexByID[id],
              items.indices.contains(index),
              case .diff(let payload, let isExpanded) = items[index].payload
        else { return }
        if !isExpanded {
            stickyBottom.unpinForUserAction()
        }
        notifySearchContentWillChange()
        removeAttributedCache(for: id)
        if let rawIdx = rawItemIndexByID[id], rawItems.indices.contains(rawIdx) {
            rawItems[rawIdx] = PCodeTranscriptItem(
                id: id,
                payload: .diff(payload, isExpanded: !isExpanded),
                spacingAfter: rawItems[rawIdx].spacingAfter,
                revision: rawItems[rawIdx].revision &+ 1,
                depth: rawItems[rawIdx].depth)
        }
        items[index] = PCodeTranscriptItem(
            id: id,
            payload: .diff(payload, isExpanded: !isExpanded),
            spacingAfter: items[index].spacingAfter,
            revision: items[index].revision &+ 1,
            depth: items[index].depth)
        invalidateSearchIndex()
        mountViews(in: visibleRect)
        // If collapsing a long diff card from its bottom button, unpin and keep its top visible.
        if isExpanded, let scrollView = enclosingScrollView, layoutEngine.entries.indices.contains(index) {
            let entry = layoutEngine.entries[index]
            let vis = scrollView.documentVisibleRect
            if entry.yOffset < vis.minY {
                unpinFromBottom()
                let targetY = max(-scrollView.contentInsets.top, entry.yOffset - 12)
                isApplyingDocumentHeight = true
                scrollView.contentView.scroll(to: CGPoint(x: 0, y: targetY))
                scrollView.reflectScrolledClipView(scrollView.contentView)
                lastClipOriginY = scrollView.contentView.bounds.origin.y
                isApplyingDocumentHeight = false
                mountViews(in: visibleRect)
            }
        }
    }

    private func remountVisibleViews() {
        appliedStates.removeAll(keepingCapacity: true)
        for (_, rowView) in mountedViews {
            rowView.primaryTextView?.needsDisplay = true
            rowView.needsDisplay = true
        }
        mountViews(in: visibleRect)
    }

    private func dequeueRowView(for kind: PCodeTranscriptItem.Kind) -> any PCodeTranscriptRowHosting {
        if var pool = reusePools[kind], let recycled = pool.popLast() {
            reusePools[kind] = pool
            return recycled
        }
        switch kind {
        case .prompt: return PCodePromptRowView()
        case .prose: return PCodeProseRowView()
        case .codeBlock: return PCodeCodeBlockView()
        case .table: return PCodeTableBlockView()
        case .tool: return PCodeToolCardView()
        case .toolGroup: return PCodeToolGroupCardView()
        case .diff: return PCodeDiffCardView()
        case .thinking: return PCodeThinkingCardView()
        case .status: return PCodeStatusRowView()
        }
    }

    private func recycleAllMountedViews() {
        for (_, view) in mountedViews {
            view.resetForTranscriptReuse()
            view.removeFromSuperview()
            reusePools[view.itemKind, default: []].append(view)
        }
        mountedViews.removeAll()
        appliedStates.removeAll()
    }

    /// Atomic height + scroll-origin update wrapped in `CATransaction` (actions disabled):
    /// growing updates frame then origin; shrinking updates origin then frame to avoid clip clamping.
    /// When `allowSmoothGlide` is true and pinned to bottom on-screen, absorbs downward growth into
    /// `smoothGlide` so `CADisplayLink` glides `clipView.bounds.origin.y` smoothly at 120Hz.
    private func applyDocumentHeight(
        _ newHeight: CGFloat,
        scrollOriginDelta: CGFloat,
        pinIfNeeded: Bool,
        allowSmoothGlide: Bool = true
    ) {
        guard let scrollView = enclosingScrollView else {
            frame.size.height = newHeight
            return
        }
        let clipView = scrollView.contentView
        let oldHeight = frame.size.height
        let unobstructedH = unobstructedViewportHeight(in: scrollView)
        if pinIfNeeded {
            // Slack that keeps the current origin exactly at the bottom. A shrink resets the
            // slack to that; growth and viewport changes may only lower it, so the gap fills
            // before the view scrolls on, and a taller viewport still reveals scrollback.
            let heightHoldingOrigin = clipView.bounds.origin.y + clipView.bounds.height - scrollView.contentInsets.bottom
            let holdingSlack = max(0, heightHoldingOrigin - newHeight)
            bottomSlack = newHeight < lastAppliedContentHeight - 0.5
                ? holdingSlack
                : min(bottomSlack, holdingSlack)
        }
        lastAppliedContentHeight = newHeight
        let targetHeight = max(newHeight + bottomSlack, unobstructedH)
        let rawOriginY: CGFloat
        if pinIfNeeded {
            let targetBottomY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: targetHeight,
                clipHeight: clipView.bounds.height,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
            let prevBottomY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: oldHeight,
                clipHeight: clipView.bounds.height,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
            let deltaBottom = targetBottomY - prevBottomY
            let growthDelta = targetBottomY - clipView.bounds.origin.y
            if allowSmoothGlide && window != nil && (growthDelta > 0.5 || smoothGlide.isActive) {
                if deltaBottom > 0.5 {
                    smoothGlide.addOffset(deltaBottom)
                    ensureDisplayLinkRunning()
                } else if deltaBottom < -0.5 {
                    smoothGlide.absorbShrink(-deltaBottom)
                }
                notifyGlideActiveIfNeeded()
                let scale = max(window?.backingScaleFactor ?? 2.0, 1.0)
                rawOriginY = round((targetBottomY - smoothGlide.remainingOffset) * scale) / scale
            } else {
                smoothGlide.reset()
                notifyGlideActiveIfNeeded()
                rawOriginY = targetBottomY
            }
        } else {
            rawOriginY = clipView.bounds.origin.y + scrollOriginDelta
        }

        isApplyingDocumentHeight = true
        defer {
            isApplyingDocumentHeight = false
            lastClipOriginY = clipView.bounds.origin.y
        }

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        if targetHeight >= oldHeight {
            frame.size = CGSize(width: clipView.bounds.width, height: targetHeight)
            let clamped = clipView.constrainBoundsRect(
                CGRect(origin: CGPoint(x: clipView.bounds.origin.x, y: rawOriginY), size: clipView.bounds.size))
            if abs(clamped.origin.y - clipView.bounds.origin.y) > 0.25 {
                clipView.scroll(to: clamped.origin)
                scrollView.reflectScrolledClipView(clipView)
            }
        } else {
            let maxOriginY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: targetHeight,
                clipHeight: clipView.bounds.height,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
            let clampedY = min(max(-scrollView.contentInsets.top, rawOriginY), maxOriginY)
            if abs(clampedY - clipView.bounds.origin.y) > 0.25 {
                clipView.scroll(to: CGPoint(x: clipView.bounds.origin.x, y: clampedY))
                scrollView.reflectScrolledClipView(clipView)
            }
            frame.size = CGSize(width: clipView.bounds.width, height: targetHeight)
        }
        CATransaction.commit()
    }

    private func distanceFromBottom(in scrollView: NSScrollView) -> CGFloat {
        PCodeStickyBottomState.distanceFromBottom(
            clipOriginY: scrollView.contentView.bounds.origin.y,
            documentHeight: frame.size.height,
            clipHeight: scrollView.contentView.bounds.height,
            topInset: scrollView.contentInsets.top,
            bottomInset: scrollView.contentInsets.bottom)
    }

    // MARK: - DisplayLink Coalesced Streaming

    private func ensureDisplayLinkRunning() {
        guard displayLink == nil, window != nil else { return }
        lastDisplayLinkTargetTimestamp = nil
        let proxy = PCodeDisplayLinkProxy(target: self)
        let link = displayLink(target: proxy, selector: #selector(PCodeDisplayLinkProxy.tick(_:)))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 80, maximum: 120, preferred: 120)
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    private func stopDisplayLink() {
        displayLink?.invalidate()
        displayLink = nil
        lastDisplayLinkTargetTimestamp = nil
    }

    fileprivate func handleDisplayLinkTick(_ sender: CADisplayLink) {
        let targetNow = sender.targetTimestamp
        let dt: TimeInterval
        if let last = lastDisplayLinkTargetTimestamp, targetNow > last {
            dt = min(max(targetNow - last, 1.0 / 240.0), 0.05)
        } else {
            dt = 1.0 / 120.0
        }
        lastDisplayLinkTargetTimestamp = targetNow

        // 1. Step the 120Hz backlog-paced conveyor smooth-scroll glide while pinned.
        if smoothGlide.isActive {
            if stickyBottom.shouldProgrammaticallyPin, let scrollView = enclosingScrollView {
                let remaining = smoothGlide.step(dt: dt, speedMultiplier: streamingSpeedMultiplier)
                let clipView = scrollView.contentView
                let targetBottomY = PCodeStickyBottomState.bottomOriginY(
                    documentHeight: frame.size.height,
                    clipHeight: clipView.bounds.height,
                    topInset: scrollView.contentInsets.top,
                    bottomInset: scrollView.contentInsets.bottom)
                let scale = max(window?.backingScaleFactor ?? 2.0, 1.0)
                let desiredY = round((targetBottomY - remaining) * scale) / scale
                let clamped = clipView.constrainBoundsRect(
                    CGRect(origin: CGPoint(x: clipView.bounds.origin.x, y: desiredY), size: clipView.bounds.size))
                if abs(clamped.origin.y - clipView.bounds.origin.y) >= (0.5 / scale) {
                    isApplyingDocumentHeight = true
                    CATransaction.begin()
                    CATransaction.setDisableActions(true)
                    clipView.scroll(to: clamped.origin)
                    scrollView.reflectScrolledClipView(clipView)
                    CATransaction.commit()
                    lastClipOriginY = clipView.bounds.origin.y
                    isApplyingDocumentHeight = false
                    // Only remount when the visible item range actually changed.
                    let newVisible = layoutEngine.visibleRange(in: visibleRect)
                    if newVisible != lastMountedRange {
                        mountViews(in: visibleRect)
                    }
                }
            } else {
                smoothGlide.reset()
            }
        }
        notifyGlideActiveIfNeeded()

        // 2. Step the visibility-gated visual reveal queue (ink-sweep / curtain).
        var revealMadeProgress = false
        if !revealQueue.isEmpty {
            let visMinY = visibleRect.minY
            let visMaxY = visibleRect.maxY

            // Fast-forward items that are already completely above the viewport (e.g. after Jump to bottom).
            let skippedAbove = revealQueue.completeEntries { [weak self] itemID in
                guard let self,
                      let idx = self.itemIndexByID[itemID],
                      self.layoutEngine.entries.indices.contains(idx)
                else { return true }
                let entry = self.layoutEngine.entries[idx]
                return entry.yOffset + entry.height <= visMinY
            }
            for update in skippedAbove {
                guard let rowView = mountedViews[update.itemID] else { continue }
                rowView.primaryTextView?.inkSweepFrontier = nil
                rowView.setCurtainRevealProgress(nil)
            }

            let updates = revealQueue.step(
                dt: dt,
                speedMultiplier: streamingSpeedMultiplier,
                isVisible: { [weak self] itemID in
                    guard let self,
                          let idx = self.itemIndexByID[itemID],
                          self.layoutEngine.entries.indices.contains(idx)
                    else { return true }
                    return self.layoutEngine.entries[idx].yOffset < visMaxY - 2
                })
            revealMadeProgress = !updates.isEmpty
            for update in updates {
                guard let rowView = mountedViews[update.itemID] else { continue }
                switch update.style {
                case .inkSweep:
                    rowView.primaryTextView?.inkSweepFrontier = update.frontier
                case .curtain:
                    rowView.setCurtainRevealProgress(update.curtainProgress)
                }
            }
        }

        // Pause the display link when idle OR when scrolled up with only offscreen reveals queued
        // (avoids spinning at 120Hz while scrolled up; resumes on scroll/Jump to bottom).
        if !smoothGlide.isActive && (revealQueue.isEmpty || !revealMadeProgress) {
            stopDisplayLink()
        }
    }

    private func resetSmoothGlide() {
        smoothGlide.reset()
        notifyGlideActiveIfNeeded()
    }

    private func notifyGlideActiveIfNeeded() {
        let active = smoothGlide.isActive && stickyBottom.mode == .pinnedToBottom
        guard active != lastNotifiedGlideActive else { return }
        lastNotifiedGlideActive = active
        onSmoothGlideActiveChanged?(active)
    }

    private func remapSelection(
        from endpoints: (anchorID: String, anchorOffset: Int, focusID: String, focusOffset: Int)?
    ) {
        guard let endpoints else {
            clampSelectionToCurrentItems()
            return
        }
        guard let newAnchorIdx = itemIndexByID[endpoints.anchorID],
              let newFocusIdx = itemIndexByID[endpoints.focusID]
        else {
            selection = nil
            updateMountedSelectionHighlights()
            return
        }
        let anchorMax = (plainTextForCopy(at: newAnchorIdx) as NSString).length
        let focusMax = (plainTextForCopy(at: newFocusIdx) as NSString).length
        let remapped = PCodeTranscriptSelection(
            anchor: .init(itemIndex: newAnchorIdx, utf16Offset: min(max(0, endpoints.anchorOffset), anchorMax)),
            focus: .init(itemIndex: newFocusIdx, utf16Offset: min(max(0, endpoints.focusOffset), focusMax)))
        selection = remapped.isEmpty ? nil : remapped
        updateMountedSelectionHighlights()
    }

    private func clampSelectionToCurrentItems() {
        guard var sel = selection else { return }
        guard !items.isEmpty else {
            selection = nil
            return
        }
        let maxIdx = items.count - 1
        if sel.anchor.itemIndex > maxIdx {
            let maxLen = (plainTextForCopy(at: maxIdx) as NSString).length
            sel.anchor = .init(itemIndex: maxIdx, utf16Offset: maxLen)
        } else {
            let maxLen = (plainTextForCopy(at: sel.anchor.itemIndex) as NSString).length
            sel.anchor = .init(itemIndex: sel.anchor.itemIndex, utf16Offset: min(max(0, sel.anchor.utf16Offset), maxLen))
        }
        if sel.focus.itemIndex > maxIdx {
            let maxLen = (plainTextForCopy(at: maxIdx) as NSString).length
            sel.focus = .init(itemIndex: maxIdx, utf16Offset: maxLen)
        } else {
            let maxLen = (plainTextForCopy(at: sel.focus.itemIndex) as NSString).length
            sel.focus = .init(itemIndex: sel.focus.itemIndex, utf16Offset: min(max(0, sel.focus.utf16Offset), maxLen))
        }
        selection = sel.isEmpty ? nil : sel
    }

    // MARK: - Model-Owned Cross-Item Selection & Copy

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        let point = convert(event.locationInWindow, from: nil)
        guard let pos = selectionPosition(at: point) else {
            selection = nil
            updateMountedSelectionHighlights()
            return
        }
        isDraggingSelection = true
        dragMoved = false
        selection = PCodeTranscriptSelection(anchor: pos, focus: pos)
        updateMountedSelectionHighlights()
    }

    override func mouseDragged(with event: NSEvent) {
        guard isDraggingSelection, var current = selection else { return }
        dragMoved = true
        if autoscroll(with: event) {
            stickyBottom.unpinForUserAction()
        }
        let point = convert(event.locationInWindow, from: nil)
        if let pos = selectionPosition(at: point) {
            current.focus = pos
            selection = current
            updateMountedSelectionHighlights()
        }
    }

    override func mouseUp(with event: NSEvent) {
        defer { isDraggingSelection = false }
        if !dragMoved {
            let point = convert(event.locationInWindow, from: nil)
            if let url = linkURL(at: point) {
                NSWorkspace.shared.open(url)
            } else {
                // Plain click with no dragged selection: hand keyboard focus back to the composer.
                selection = nil
                updateMountedSelectionHighlights()
                onReturnFocusToComposer?()
            }
        }
    }

    override func keyDown(with event: NSEvent) {
        let mods = event.modifierFlags.intersection([.command, .option, .control, .shift])
        // Let standard copy/select-all/find shortcuts or Escape (or `q` in the /btw drawer) clear
        // selection; forward ordinary typing back to the docked composer so focus never feels stranded.
        if event.keyCode == 53 || (swallowsQToReturnFocus && mods.isEmpty && event.charactersIgnoringModifiers == "q") {
            selection = nil
            updateMountedSelectionHighlights()
            onReturnFocusToComposer?()
            return
        }
        // Ghostty leaves Edit > Copy without a key equivalent whenever the copy binding is
        // `performable` (its default), so Cmd+C arrives here as a plain key press.
        if mods == .command, let key = event.charactersIgnoringModifiers?.lowercased() {
            if key == "c" {
                copy(nil)
                return
            }
            if key == "a" {
                selectAll(nil)
                return
            }
        }
        if mods.contains(.command) || mods.contains(.control) {
            super.keyDown(with: event)
            return
        }
        if let onReturnFocusToComposer {
            onReturnFocusToComposer()
            if let target = window?.firstResponder, target !== self {
                target.keyDown(with: event)
            }
            return
        }
        super.keyDown(with: event)
    }

    func validateUserInterfaceItem(_ item: any NSValidatedUserInterfaceItem) -> Bool {
        if item.action == #selector(copy(_:)) {
            return selection.map { !$0.isEmpty } ?? false
        }
        if item.action == #selector(selectAll(_:)) {
            return !items.isEmpty
        }
        if item.action == #selector(copyAnswerMarkdown(_:)) {
            return (item as? NSMenuItem)?.representedObject is String
        }
        if item.action == #selector(setPromptStyleFromMenu(_:)) {
            if let menuItem = item as? NSMenuItem {
                menuItem.state = menuItem.representedObject as? String == promptStyle.rawValue ? .on : .off
            }
            return true
        }
        if item.action == #selector(toggleGroupToolsFromMenu(_:)) {
            if let menuItem = item as? NSMenuItem {
                menuItem.state = groupTools ? .on : .off
            }
            return true
        }
        if item.action == #selector(performFindPanelAction(_:)),
           let finder = textFinder,
           let action = NSTextFinder.Action(rawValue: item.tag)
        {
            return finder.validateAction(action)
        }
        return false
    }

    /// Notify `NSTextFinder` BEFORE mutating `items` so its cache never observes stale state.
    func notifySearchContentWillChange() {
        textFinder?.noteClientStringWillChange()
    }

    /// Clear the cached search index AFTER mutating `items`.
    func invalidateSearchIndex() {
        cachedSearchIndex = nil
    }

    /// Scroll the item at `index` (and optionally `localRange` within it) into the unobstructed
    /// viewport (excluding `bottomContentInset`), unpinning sticky-bottom and mounting views.
    func scrollItemToVisible(at index: Int, localRange: NSRange? = nil) {
        guard layoutEngine.entries.indices.contains(index),
              let scrollView = enclosingScrollView
        else { return }
        unpinFromBottom()

        let scrollToY: (CGFloat) -> Void = { targetY in
            let clipHeight = scrollView.contentView.bounds.height
            let maxOriginY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: self.frame.size.height,
                clipHeight: clipHeight,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
            let clampedY = min(max(-scrollView.contentInsets.top, targetY), maxOriginY)
            self.isApplyingDocumentHeight = true
            scrollView.contentView.scroll(to: CGPoint(x: 0, y: clampedY))
            scrollView.reflectScrolledClipView(scrollView.contentView)
            self.lastClipOriginY = scrollView.contentView.bounds.origin.y
            self.isApplyingDocumentHeight = false
        }

        let unobstructedH = max(1, unobstructedViewportHeight(in: scrollView))
        let initialEntry = layoutEngine.entries[index]
        scrollToY(initialEntry.yOffset - max(0, (unobstructedH - initialEntry.height) * 0.35))
        mountViews(in: visibleRect)

        // After mounting/measuring, refine to the exact match rect (important for long blocks).
        guard layoutEngine.entries.indices.contains(index) else { return }
        if let localRange, localRange.length > 0,
           let firstRect = characterRects(forItemAt: index, localRange: localRange).first
        {
            scrollToY(firstRect.minY - max(0, (unobstructedH - firstRect.height) * 0.35))
            mountViews(in: visibleRect)
        } else {
            let finalEntry = layoutEngine.entries[index]
            if abs(finalEntry.yOffset - initialEntry.yOffset) > 1 {
                scrollToY(finalEntry.yOffset - max(0, (unobstructedH - finalEntry.height) * 0.35))
                mountViews(in: visibleRect)
            }
        }
    }

    /// Return highlight rects in document coordinates for `localRange` within item `index`,
    /// clipped to the row view's visible bounds (so horizontally scrolled code/diff matches stay inside).
    func characterRects(forItemAt index: Int, localRange: NSRange) -> [CGRect] {
        guard items.indices.contains(index), layoutEngine.entries.indices.contains(index) else { return [] }
        let item = items[index]
        if let rowView = mountedViews[item.id],
           let textView = rowView.primaryTextView,
           let textRange = textView.textRange(fromUTF16: localRange)
        {
            // Also clip to the text's own container (a scrolled code block, or a thinking
            // row's streaming tail) so hidden lines never paint over neighbouring chrome.
            let container = textView.superview ?? rowView
            let rowClip = rowView.frame.intersection(self.convert(container.bounds, from: container))
            var out: [CGRect] = []
            textView.textLayoutManager.enumerateTextSegments(
                in: textRange,
                type: .selection,
                options: []
            ) { _, segFrame, _, _ in
                if segFrame.width > 0 && segFrame.height > 0 {
                    let docRect = self.convert(segFrame, from: textView).intersection(rowClip)
                    if !docRect.isNull && docRect.width > 0 && docRect.height > 0 {
                        out.append(docRect)
                    }
                }
                return true
            }
            if !out.isEmpty { return out }
        }
        let entry = layoutEngine.entries[index]
        return [CGRect(
            x: Self.horizontalPadding,
            y: entry.yOffset,
            width: max(1, bounds.width - Self.horizontalPadding * 2),
            height: entry.height)]
    }

    /// Accessibility helper to toggle expandable `.tool`, `.toolGroup`, or `.diff` rows.
    func toggleExpansionForAccessibility(id: String) {
        guard let idx = itemIndexByID[id], items.indices.contains(idx) else { return }
        switch items[idx].payload {
        case .tool:
            toggleToolExpansion(id: id)
        case .toolGroup:
            toggleToolGroupExpansion(id: id)
        case .diff:
            toggleDiffExpansion(id: id)
        default:
            break
        }
    }

    /// Plain-text string for the current selection (used by `copy(_:)` and unit tests).
    func selectedPlainText() -> String {
        guard let selection, !selection.isEmpty else { return "" }
        return selection.extractText(
            itemCount: items.count,
            separatorBetween: { prev, next in
                if let g1 = items[prev].codeBlockGroupID,
                   let g2 = items[next].codeBlockGroupID,
                   g1 == g2
                {
                    return "\n"
                }
                return "\n\n"
            },
            textForItem: { idx in
                plainTextForCopy(at: idx)
            })
    }

    @objc func copy(_ sender: Any?) {
        let copied = selectedPlainText()
        guard !copied.isEmpty else { return }
        pasteboard.clearContents()
        pasteboard.setString(copied, forType: .string)
    }

    /// Right-click menu: Copy for the selection, the whole answer's Markdown when the
    /// pointer is on an answer's prose, and Select All.
    override func menu(for event: NSEvent) -> NSMenu? {
        let menu = NSMenu()
        let copyItem = NSMenuItem(title: "Copy", action: #selector(copy(_:)), keyEquivalent: "")
        copyItem.target = self
        menu.addItem(copyItem)
        if let markdown = answerMarkdown(at: convert(event.locationInWindow, from: nil)) {
            let answerItem = NSMenuItem(
                title: "Copy Answer as Markdown",
                action: #selector(copyAnswerMarkdown(_:)),
                keyEquivalent: "")
            answerItem.target = self
            answerItem.representedObject = markdown
            menu.addItem(answerItem)
        }
        menu.addItem(.separator())
        let selectAllItem = NSMenuItem(title: "Select All", action: #selector(selectAll(_:)), keyEquivalent: "")
        selectAllItem.target = self
        menu.addItem(selectAllItem)
        menu.addItem(.separator())
        let groupItem = NSMenuItem(
            title: "Group Tool Calls",
            action: #selector(toggleGroupToolsFromMenu(_:)),
            keyEquivalent: "")
        groupItem.target = self
        groupItem.state = groupTools ? .on : .off
        menu.addItem(groupItem)
        let styleMenu = NSMenu()
        for style in PCodePromptStyle.allCases {
            let item = NSMenuItem(
                title: style.title,
                action: #selector(setPromptStyleFromMenu(_:)),
                keyEquivalent: "")
            item.target = self
            item.representedObject = style.rawValue
            item.state = style == promptStyle ? .on : .off
            styleMenu.addItem(item)
        }
        let styleItem = NSMenuItem(title: "Prompt Style", action: nil, keyEquivalent: "")
        styleItem.submenu = styleMenu
        menu.addItem(styleItem)
        return menu
    }

    @objc func setPromptStyleFromMenu(_ sender: Any?) {
        guard let raw = (sender as? NSMenuItem)?.representedObject as? String,
              let style = PCodePromptStyle(rawValue: raw)
        else { return }
        PCodeTranscriptPreferences.shared.promptStyle = style
        promptStyle = style
    }

    @objc func toggleGroupToolsFromMenu(_ sender: Any?) {
        let next = !groupTools
        PCodeTranscriptPreferences.shared.groupTools = next
        if groupTools != next {
            groupTools = next
        }
    }

    /// The Markdown source of the answer whose prose is at `docPoint`, if any.
    func answerMarkdown(at docPoint: CGPoint) -> String? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y),
              items.indices.contains(idx),
              case .prose(_, _, let copyMarkdown) = items[idx].payload,
              let copyMarkdown, !copyMarkdown.isEmpty,
              // `itemIndex(atY:)` clamps to the nearest row; the point must be on it.
              layoutEngine.entries.indices.contains(idx),
              docPoint.y >= layoutEngine.entries[idx].yOffset,
              docPoint.y <= layoutEngine.entries[idx].yOffset + layoutEngine.entries[idx].height
        else { return nil }
        return copyMarkdown
    }

    @objc func copyAnswerMarkdown(_ sender: Any?) {
        guard let markdown = (sender as? NSMenuItem)?.representedObject as? String else { return }
        pasteboard.clearContents()
        pasteboard.setString(markdown, forType: .string)
    }

    override func selectAll(_ sender: Any?) {
        guard let lastIdx = items.indices.last else { return }
        let lastLen = (plainTextForCopy(at: lastIdx) as NSString).length
        selection = PCodeTranscriptSelection(
            anchor: .init(itemIndex: 0, utf16Offset: 0),
            focus: .init(itemIndex: lastIdx, utf16Offset: lastLen))
        updateMountedSelectionHighlights()
    }

    func plainTextForCopy(at index: Int) -> String {
        let item = items[index]
        switch item.payload {
        case .prose(let markdown, let refDefs, _):
            return cachedProseAttributedString(for: item.id, markdown: markdown, referenceDefinitions: refDefs).string
        case .prompt(let label, let text, let isSide):
            return cachedPromptAttributedString(for: item.id, label: label, text: text, isSide: isSide).string
        case .codeBlock(_, let code, _, _, _):
            return code
        case .table(let table):
            return table.plainText
        case .tool(let call, let isExpanded):
            return cachedToolAttributedString(for: item.id, call: call, isExpanded: isExpanded).string
        case .toolGroup(let payload):
            return cachedToolGroupAttributedString(for: item.id, payload: payload).string
        case .diff(let payload, let isExpanded):
            return isExpanded
                ? cachedDiffAttributedString(for: item.id, payload: payload).string
                : PCodeDiffCardView.summaryLine(for: payload)
        case .thinking(let thinking):
            return cachedThinkingAttributedString(for: item.id, thinking: thinking).string
        case .status(let status):
            return cachedStatusAttributedString(for: item.id, status: status).string
        }
    }

    private func selectionPosition(at docPoint: CGPoint) -> PCodeTranscriptSelection.Position? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y),
              items.indices.contains(idx)
        else { return nil }
        let id = items[idx].id
        if let rowView = mountedViews[id], let textView = rowView.primaryTextView {
            let localPt = textView.convert(docPoint, from: self)
            let offset = textView.utf16Offset(at: localPt)
            return PCodeTranscriptSelection.Position(itemIndex: idx, utf16Offset: offset)
        }
        let entry = layoutEngine.entries[idx]
        let len = (plainTextForCopy(at: idx) as NSString).length
        let offset = docPoint.y < entry.yOffset + entry.height * 0.5 ? 0 : len
        return PCodeTranscriptSelection.Position(itemIndex: idx, utf16Offset: offset)
    }

    private func linkURL(at docPoint: CGPoint) -> URL? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y),
              items.indices.contains(idx),
              let rowView = mountedViews[items[idx].id],
              rowView.frame.contains(docPoint),
              let textView = rowView.primaryTextView
        else { return nil }
        let localPt = textView.convert(docPoint, from: self)
        return textView.linkURL(at: localPt)
    }

    private func updateMountedSelectionHighlights() {
        for (id, rowView) in mountedViews {
            guard let idx = itemIndexByID[id] else { continue }
            let len = rowView.selectableTextLength
            rowView.setSelectedRange(selection?.localRange(forItem: idx, utf16Length: len))
        }
    }

    // MARK: - Attributed String Cache & Height Estimates

    private func storeAttributedCache(_ value: NSAttributedString, for id: String) {
        if attributedCache[id] == nil {
            attributedCacheOrder.append(id)
        }
        attributedCache[id] = value
        while attributedCacheOrder.count > Self.maxAttributedCacheEntries {
            let evicted = attributedCacheOrder.removeFirst()
            attributedCache.removeValue(forKey: evicted)
        }
    }

    private func removeAttributedCache(for id: String) {
        if attributedCache.removeValue(forKey: id) != nil {
            attributedCacheOrder.removeAll { $0 == id }
        }
    }

    private func clearAttributedCache() {
        attributedCache.removeAll(keepingCapacity: true)
        attributedCacheOrder.removeAll(keepingCapacity: true)
    }

    private func estimatedHeight(for item: PCodeTranscriptItem) -> CGFloat {
        switch item.payload {
        case .tool: return 28
        case .toolGroup: return PCodeToolGroupCardView.rowHeight
        case .status: return 24
        case .prompt(let label, let text, _):
            let lines = max(1, min(12, text.count / 68 + 1))
            return (label.isEmpty ? 20 : 28) + CGFloat(lines) * typography.lineHeight()
        case .prose(let markdown, _, _):
            let lines = max(1, min(40, markdown.count / 72 + 1))
            return CGFloat(lines) * typography.lineHeight()
        case .codeBlock(_, let code, _, let showHeader, _):
            let lineCount = max(1, code.split(separator: "\n", omittingEmptySubsequences: false).count)
            return (showHeader ? PCodeCodeBlockView.headerHeight : 0) + 20 + CGFloat(lineCount) * typography.lineHeight(scale: typography.codeBlockScale)
        case .table(let table):
            let rowCount = table.rows.count + (table.headers.isEmpty ? 0 : 1)
            return CGFloat(max(1, rowCount)) * (typography.lineHeight(scale: 0.92) + 14)
        case .diff(let diff, let isExpanded):
            guard isExpanded else { return PCodeDiffCardView.headerHeight }
            let lineCount = max(1, min(200, diff.patch.split(separator: "\n", omittingEmptySubsequences: false).count))
            return PCodeDiffCardView.headerHeight + 40 + CGFloat(lineCount) * typography.lineHeight(scale: typography.codeBlockScale)
        case .thinking(let thinking):
            // Always shown in full, so long thoughts need an honest estimate.
            let lines = max(1, min(400, thinking.text.count / 80 + 1))
            return PCodeThinkingCardView.verticalPadding * 2
                + CGFloat(lines) * typography.lineHeight(scale: PCodeThinkingCardView.bodyScale)
        }
    }

    private func cachedProseAttributedString(
        for id: String,
        markdown: String,
        referenceDefinitions: String
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeTranscriptMarkdown.attributedString(
            forProse: markdown,
            referenceDefinitions: referenceDefinitions,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }

    private func cachedPromptAttributedString(
        for id: String,
        label: String,
        text: String,
        isSide: Bool
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let labelStyle = NSMutableParagraphStyle()
        let labelLineHeight = typography.lineHeight(scale: 0.82)
        labelStyle.minimumLineHeight = labelLineHeight
        labelStyle.maximumLineHeight = labelLineHeight
        labelStyle.paragraphSpacing = 2

        let paragraph = NSMutableParagraphStyle()
        let lineHeight = typography.lineHeight()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight

        var trimmedText = text
        while trimmedText.last?.isNewline == true {
            trimmedText.removeLast()
        }

        let out = NSMutableAttributedString()
        let labelAttrs: [NSAttributedString.Key: Any] = [
            .font: typography.proseNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight),
            .foregroundColor: isSide ? NSColor.systemOrange : theme.accent,
            .paragraphStyle: labelStyle,
        ]
        // Main prompts carry no label: the card and accent bar already say
        // whose turn it is. `/btw` keeps one because it names the model.
        if trimmedText.isEmpty {
            out.append(NSAttributedString(string: label, attributes: labelAttrs))
        } else {
            if !label.isEmpty {
                out.append(NSAttributedString(string: label + "\n", attributes: labelAttrs))
            }
            let bodyAttrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(),
                .foregroundColor: theme.foreground,
                .paragraphStyle: paragraph,
            ]
            out.append(NSAttributedString(string: trimmedText, attributes: bodyAttrs))
        }
        storeAttributedCache(out, for: id)
        return out
    }

    private func cachedToolAttributedString(
        for id: String,
        call: PCodePeekToolCall,
        isExpanded: Bool
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeToolCardView.attributedText(
            for: call,
            isExpanded: isExpanded,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }

    private func cachedToolGroupAttributedString(
        for id: String,
        payload: PCodeToolGroupPayload
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeToolGroupCardView.attributedText(
            for: payload,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }

    private func cachedStatusAttributedString(
        for id: String,
        status: PCodePeekTurn.Status
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeStatusRowView.attributedText(
            for: status,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }

    private func cachedDiffAttributedString(
        for id: String,
        payload: PCodeDiffPayload
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeDiffCardView.attributedPatch(
            for: payload,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }

    private func cachedThinkingAttributedString(
        for id: String,
        thinking: PCodeThinkingPayload
    ) -> NSAttributedString {
        if let cached = attributedCache[id] {
            return cached
        }
        let built = PCodeThinkingCardView.attributedText(
            for: thinking,
            typography: typography,
            theme: theme)
        storeAttributedCache(built, for: id)
        return built
    }
}

/// Floating capsule button shown at bottom-center when scrolled up during live streaming.
final class PCodeJumpToBottomButton: NSButton {
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isBordered = false
        bezelStyle = .inline
        imagePosition = .noImage
        wantsLayer = true
        layer?.cornerRadius = 14
        layer?.borderWidth = 1
        let dropShadow = NSShadow()
        dropShadow.shadowColor = NSColor.black.withAlphaComponent(0.18)
        dropShadow.shadowBlurRadius = 6
        dropShadow.shadowOffset = NSSize(width: 0, height: -2)
        shadow = dropShadow
        applyTitle(foreground: .labelColor)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func applyTheme(_ theme: PCodeTranscriptTheme) {
        let bg = theme.isDark
            ? theme.background.blended(withFraction: 0.18, of: .white) ?? .windowBackgroundColor
            : theme.background.blended(withFraction: 0.04, of: .black) ?? .windowBackgroundColor
        let fg = theme.foreground
        let border = theme.foreground.withAlphaComponent(theme.isDark ? 0.18 : 0.14)
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = bg.cgColor
            layer?.borderColor = border.cgColor
            layer?.shadowColor = NSColor.black.cgColor
        }
        applyTitle(foreground: fg)
    }

    private func applyTitle(foreground: NSColor) {
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        attributedTitle = NSAttributedString(
            string: "↓  Jump to bottom",
            attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium),
                .foregroundColor: foreground,
                .paragraphStyle: style,
            ])
        sizeToFit()
    }
}

/// Non-interactive soft bottom-edge dissolve overlay shown while the smooth-scroll conveyor glides.
private final class PCodeBottomDissolveView: NSView {
    private let gradientLayer = CAGradientLayer()

    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        gradientLayer.startPoint = CGPoint(x: 0.5, y: 0.0)
        gradientLayer.endPoint = CGPoint(x: 0.5, y: 1.0)
        gradientLayer.opacity = 0
        layer?.addSublayer(gradientLayer)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func applyTheme(_ theme: PCodeTranscriptTheme) {
        let base = theme.background
        let topColor = base.withAlphaComponent(0.0).cgColor
        let bottomColor = base.withAlphaComponent(0.92).cgColor
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        gradientLayer.colors = [topColor, bottomColor]
        CATransaction.commit()
    }

    func setActive(_ active: Bool) {
        CATransaction.begin()
        CATransaction.setAnimationDuration(active ? 0.12 : 0.25)
        gradientLayer.opacity = active ? 1.0 : 0.0
        CATransaction.commit()
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        gradientLayer.frame = bounds
        CATransaction.commit()
    }
}

/// Native `NSScrollView` hosting `PCodeTranscriptDocumentView` with live-scroll observation
/// (without overriding `scrollWheel`, preserving AppKit responsive scrolling), a soft bottom-edge
/// dissolve during conveyor glides, and a floating "Jump to bottom" pill when scrolled up.
final class PCodeTranscriptScrollView: NSScrollView {
    let transcriptDocumentView: PCodeTranscriptDocumentView
    let textFinder = NSTextFinder()
    let jumpToBottomButton = PCodeJumpToBottomButton()
    private let bottomDissolveView = PCodeBottomDissolveView()
    private var observers: [NSObjectProtocol] = []
    private var typographyCancellable: AnyCancellable?
    private var preferencesCancellable: AnyCancellable?
    private var promptStyleCancellable: AnyCancellable?
    private var bottomContentInsetValue: CGFloat = 0
    private var lastClipHeight: CGFloat = 0
    /// Optional predicate gating `⌘F`/`⌘G`/`⌘⇧G` so the scroll view never steals shortcuts
    /// from overlays, `/btw` drawer, or the `⌘J` bottom shell.
    var canHandleFindShortcut: (() -> Bool)?
    /// Whether the docked composer has a selection of its own (it then keeps Cmd+C).
    var composerHasSelection: (() -> Bool)?

    var isJumpToBottomVisible: Bool { !jumpToBottomButton.isHidden }

    var bottomContentInset: CGFloat {
        get { bottomContentInsetValue }
        set {
            let clamped = max(0, newValue)
            guard abs(clamped - bottomContentInsetValue) > 0.5 else { return }
            bottomContentInsetValue = clamped
            automaticallyAdjustsContentInsets = false
            contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: clamped, right: 0)
            scrollerInsets = NSEdgeInsets(top: 0, left: 0, bottom: clamped, right: 0)
            needsLayout = true
            if transcriptDocumentView.stickyBottom.shouldProgrammaticallyPin {
                transcriptDocumentView.repinToBottomForInsetChange()
            }
        }
    }

    init(typography: PCodeTypography = .shared, theme: PCodeTranscriptTheme) {
        self.transcriptDocumentView = PCodeTranscriptDocumentView(
            typography: typography.snapshot,
            theme: theme)
        super.init(frame: .zero)

        hasVerticalScroller = true
        hasHorizontalScroller = false
        autohidesScrollers = true
        drawsBackground = false
        borderType = .noBorder
        documentView = transcriptDocumentView
        contentView.postsBoundsChangedNotifications = true
        appearance = NSAppearance(named: theme.isDark ? .darkAqua : .aqua)

        findBarPosition = .aboveContent
        textFinder.client = transcriptDocumentView
        textFinder.findBarContainer = self
        textFinder.isIncrementalSearchingEnabled = true
        transcriptDocumentView.textFinder = textFinder

        bottomDissolveView.applyTheme(theme)
        addSubview(bottomDissolveView)

        jumpToBottomButton.isHidden = true
        jumpToBottomButton.target = self
        jumpToBottomButton.action = #selector(jumpToBottomPressed)
        jumpToBottomButton.applyTheme(theme)
        addSubview(jumpToBottomButton)

        transcriptDocumentView.onJumpToBottomVisibilityChanged = { [weak self] visible in
            guard let self else { return }
            self.jumpToBottomButton.isHidden = !visible
            if visible {
                self.layoutJumpToBottomButton()
            }
        }
        transcriptDocumentView.onSmoothGlideActiveChanged = { [weak self] active in
            self?.bottomDissolveView.setActive(active)
        }

        let nc = NotificationCenter.default
        observers.append(nc.addObserver(
            forName: NSScrollView.willStartLiveScrollNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            self?.transcriptDocumentView.handleWillStartLiveScroll()
        })
        observers.append(nc.addObserver(
            forName: NSScrollView.didEndLiveScrollNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            self?.transcriptDocumentView.handleDidEndLiveScroll()
        })
        observers.append(nc.addObserver(
            forName: NSView.boundsDidChangeNotification,
            object: contentView,
            queue: .main
        ) { [weak self] _ in
            self?.transcriptDocumentView.handleBoundsDidChange()
        })

        subscribeTypography(typography)
        preferencesCancellable = PCodeTranscriptPreferences.shared.$groupTools
            .dropFirst()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] grouped in
                self?.transcriptDocumentView.groupTools = grouped
            }
        promptStyleCancellable = PCodeTranscriptPreferences.shared.$promptStyle
            .dropFirst()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] style in
                self?.transcriptDocumentView.promptStyle = style
            }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        for obs in observers {
            NotificationCenter.default.removeObserver(obs)
        }
    }

    func subscribeTypography(_ typography: PCodeTypography) {
        typographyCancellable = typography.snapshotPublisher.sink { [weak self] snapshot in
            guard let self else { return }
            self.transcriptDocumentView.updateAppearance(
                typography: snapshot,
                theme: self.transcriptDocumentView.theme)
        }
    }

    func applyTheme(_ theme: PCodeTranscriptTheme) {
        jumpToBottomButton.applyTheme(theme)
        bottomDissolveView.applyTheme(theme)
    }

    @objc private func jumpToBottomPressed() {
        transcriptDocumentView.scrollToBottomAndPin()
    }

    /// Whether Cmd+C should copy the transcript's selection although the keyboard is
    /// elsewhere (the composer): typing after selecting moves the keyboard there and
    /// leaves the highlight, which must stay copyable. The composer's own selection wins.
    func copiesSelectionForComposer(firstResponder: NSResponder?) -> Bool {
        guard transcriptDocumentView.selection.map({ !$0.isEmpty }) ?? false else { return false }
        if let view = firstResponder as? NSView, view === self || view.isDescendant(of: self) {
            // The document view copies from its own `keyDown`.
            return false
        }
        return !(composerHasSelection?() ?? false)
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard !isHiddenOrHasHiddenAncestor, let window, window.isKeyWindow else {
            return super.performKeyEquivalent(with: event)
        }
        let allowed: Bool
        if let predicate = canHandleFindShortcut {
            allowed = predicate()
        } else if let responder = window.firstResponder as? NSView {
            allowed = (responder === self || responder.isDescendant(of: self))
        } else {
            allowed = false
        }
        guard allowed else {
            return super.performKeyEquivalent(with: event)
        }
        let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
        if flags == .command, let chars = event.charactersIgnoringModifiers?.lowercased() {
            if chars == "c", copiesSelectionForComposer(firstResponder: window.firstResponder) {
                transcriptDocumentView.copy(nil)
                return true
            }
            if chars == "f", textFinder.validateAction(.showFindInterface) {
                textFinder.performAction(.showFindInterface)
                return true
            }
            if chars == "g", isFindBarVisible, textFinder.validateAction(.nextMatch) {
                textFinder.performAction(.nextMatch)
                return true
            }
        } else if flags == [.command, .shift],
                  let chars = event.charactersIgnoringModifiers?.lowercased(),
                  chars == "g",
                  isFindBarVisible,
                  textFinder.validateAction(.previousMatch)
        {
            textFinder.performAction(.previousMatch)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    override func layout() {
        super.layout()
        let clipWidth = contentView.bounds.width
        let clipHeight = contentView.bounds.height
        if clipWidth > 0, abs(transcriptDocumentView.frame.width - clipWidth) > 0.5 {
            transcriptDocumentView.frame.size.width = clipWidth
        }
        if clipHeight > 0, abs(clipHeight - lastClipHeight) > 0.5 {
            lastClipHeight = clipHeight
            if transcriptDocumentView.stickyBottom.shouldProgrammaticallyPin {
                transcriptDocumentView.repinToBottomForInsetChange()
            }
        }
        let dissolveH: CGFloat = 28
        let bottomOffset = bottomContentInsetValue
        let dissolveY: CGFloat = isFlipped ? (bounds.height - bottomOffset - dissolveH) : bottomOffset
        bottomDissolveView.frame = CGRect(x: 0, y: dissolveY, width: bounds.width, height: dissolveH)
        layoutJumpToBottomButton()
    }

    private func layoutJumpToBottomButton() {
        let naturalWidth = ceil(jumpToBottomButton.fittingSize.width)
        let width = max(136, naturalWidth + 24)
        let height: CGFloat = 28
        let x = floor((bounds.width - width) * 0.5)
        let bottomOffset = bottomContentInsetValue
        let y: CGFloat = isFlipped ? (bounds.height - bottomOffset - height - 14) : (bottomOffset + 14)
        jumpToBottomButton.frame = CGRect(x: x, y: y, width: width, height: height)
    }
}

/// SwiftUI bridge hosting `PCodeTranscriptScrollView` with explicit `NSAppearance` forwarding
/// and optional live streaming replay (`1×` or `4×`).
struct PCodeTranscriptRepresentable: NSViewRepresentable {
    let sessionID: String
    let turns: [PCodePeekTurn]
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    let pinToBottom: Bool
    let replayActive: Bool
    let replaySpeed: Double
    let onReplayFinished: (() -> Void)?

    init(
        sessionID: String,
        turns: [PCodePeekTurn],
        theme: PCodeTranscriptTheme,
        typography: PCodeTypography = .shared,
        pinToBottom: Bool = false,
        replayActive: Bool = false,
        replaySpeed: Double = 1.0,
        onReplayFinished: (() -> Void)? = nil
    ) {
        self.sessionID = sessionID
        self.turns = turns
        self.theme = theme
        self.typography = typography
        self.pinToBottom = pinToBottom
        self.replayActive = replayActive
        self.replaySpeed = replaySpeed
        self.onReplayFinished = onReplayFinished
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeTranscriptScrollView {
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        context.coordinator.lastSessionID = sessionID
        context.coordinator.lastTurnCount = turns.count
        context.coordinator.lastTurnBlockCount = turns.last?.blocks.count ?? 0
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        context.coordinator.lastTypographyObjectID = ObjectIdentifier(typography)
        context.coordinator.lastReplayActive = replayActive
        context.coordinator.lastReplaySpeed = replaySpeed
        if replayActive {
            context.coordinator.startReplay(
                turns: turns,
                speedMultiplier: replaySpeed,
                documentView: scrollView.transcriptDocumentView,
                onComplete: onReplayFinished)
        } else {
            let items = PCodeTranscriptItem.flatten(turns: turns)
            scrollView.transcriptDocumentView.setItems(items, pinToBottom: pinToBottom)
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
            nsView.transcriptDocumentView.updateAppearance(
                typography: snap,
                theme: theme)
            nsView.applyTheme(theme)
        }

        let lastBlocks = turns.last?.blocks.count ?? 0
        let contentChanged = (coord.lastSessionID != sessionID
            || coord.lastTurnCount != turns.count
            || coord.lastTurnBlockCount != lastBlocks)
        let replayChanged = (coord.lastReplayActive != replayActive)
        let speedChanged = abs(coord.lastReplaySpeed - replaySpeed) > 0.01

        if contentChanged || replayChanged || (replayActive && speedChanged) {
            coord.lastSessionID = sessionID
            coord.lastTurnCount = turns.count
            coord.lastTurnBlockCount = lastBlocks
            coord.lastReplayActive = replayActive
            coord.lastReplaySpeed = replaySpeed

            if replayActive {
                coord.replayCompletedNaturally = false
                coord.startReplay(
                    turns: turns,
                    speedMultiplier: replaySpeed,
                    documentView: nsView.transcriptDocumentView,
                    onComplete: onReplayFinished)
            } else {
                coord.stopReplay()
                if contentChanged || !coord.replayCompletedNaturally {
                    coord.replayCompletedNaturally = false
                    let items = PCodeTranscriptItem.flatten(turns: turns)
                    nsView.transcriptDocumentView.setItems(items, pinToBottom: pinToBottom)
                } else {
                    coord.replayCompletedNaturally = false
                }
            }
        }
    }

    static func dismantleNSView(_ nsView: PCodeTranscriptScrollView, coordinator: Coordinator) {
        coordinator.stopReplay()
    }

    final class Coordinator {
        var lastSessionID: String = ""
        var lastTurnCount: Int = 0
        var lastTurnBlockCount: Int = 0
        var lastTheme: PCodeTranscriptTheme?
        var lastTypographySnapshot: PCodeTypographySnapshot?
        var lastTypographyObjectID: ObjectIdentifier?
        var lastReplayActive: Bool = false
        var lastReplaySpeed: Double = 1.0
        var replayCompletedNaturally: Bool = false
        private var replayer: PCodeTranscriptStreamReplayer?

        func startReplay(
            turns: [PCodePeekTurn],
            speedMultiplier: Double,
            documentView: PCodeTranscriptDocumentView,
            onComplete: (() -> Void)?
        ) {
            stopReplay()
            replayCompletedNaturally = false
            let r = PCodeTranscriptStreamReplayer(
                turns: turns,
                documentView: documentView,
                speedMultiplier: speedMultiplier,
                onComplete: { [weak self] in
                    self?.replayer = nil
                    self?.replayCompletedNaturally = true
                    onComplete?()
                })
            replayer = r
            r.start()
        }

        func stopReplay() {
            replayer?.stop()
            replayer = nil
        }
    }
}
