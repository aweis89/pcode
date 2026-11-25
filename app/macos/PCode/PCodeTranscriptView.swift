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

/// One flat, virtualized row in `PCodeTranscriptView`.
struct PCodeTranscriptItem: Identifiable {
    enum Kind: String {
        case prompt
        case prose
        case codeBlock
        case table
        case tool
        case status
    }

    enum Payload {
        case prompt(label: String, text: String, isSide: Bool)
        case prose(markdown: String, referenceDefinitions: String, copyMarkdown: String?)
        case codeBlock(language: String?, code: String, fullCopyCode: String, showHeader: Bool, groupID: String?)
        case table(PCodeMarkdownTable)
        case tool(PCodePeekToolCall, isExpanded: Bool)
        case status(PCodePeekTurn.Status)
    }

    let id: String
    let payload: Payload
    let spacingAfter: CGFloat
    /// Monotonic revision bumped when an item's payload mutates in place (e.g. expand/stream).
    let revision: UInt64

    var kind: Kind {
        switch payload {
        case .prompt: return .prompt
        case .prose: return .prose
        case .codeBlock: return .codeBlock
        case .table: return .table
        case .tool: return .tool
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
            return "\(label)\n\(text)"
        case .prose(let markdown, _, _):
            return markdown
        case .codeBlock(_, let code, _, _, _):
            return code
        case .table(let table):
            return table.plainText
        case .tool(let call, _):
            let summary = call.summaryText
            return summary.isEmpty ? call.name : "\(call.name) \(summary)"
        case .status(let status):
            return PCodeStatusRowView.displayLabel(for: status)
        }
    }

    init(id: String, payload: Payload, spacingAfter: CGFloat = 8, revision: UInt64 = 0) {
        self.id = id
        self.payload = payload
        self.spacingAfter = spacingAfter
        self.revision = revision
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
                label: secondaryText.isEmpty ? (isSidePrompt ? "/btw" : "You") : secondaryText,
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
        case .status:
            let status: PCodePeekTurn.Status = isFailed ? .failed : (PCodePeekTurn.Status(rawValue: plainText.lowercased()) ?? .running)
            self.payload = .status(status)
        }
    }

    func withSpacingAfter(_ spacing: CGFloat) -> PCodeTranscriptItem {
        PCodeTranscriptItem(id: id, payload: payload, spacingAfter: spacing, revision: revision)
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
                    label: turn.side ? "/btw" : "You",
                    text: turn.prompt,
                    isSide: turn.side),
                spacingAfter: 10))

            for block in turn.blocks {
                switch block {
                case .markdown(let blockID, let text):
                    let parsed = PCodeTranscriptMarkdown.parseBlocks(text)
                    var attachedCopy = false
                    var codeGroupCounter = 0
                    var activeCodeGroupID: String?

                    for (subIdx, mdBlock) in parsed.enumerated() {
                        switch mdBlock {
                        case .prose(let proseMD, let refDefs):
                            activeCodeGroupID = nil
                            let copyPayload = attachedCopy ? nil : text
                            attachedCopy = true
                            result.append(PCodeTranscriptItem(
                                id: "turn-\(turn.id)-md-\(blockID)-\(subIdx)",
                                payload: .prose(
                                    markdown: proseMD,
                                    referenceDefinitions: refDefs,
                                    copyMarkdown: copyPayload),
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

    private(set) var items: [PCodeTranscriptItem] = []
    private(set) var layoutEngine = PCodeTranscriptLayoutEngine()
    private(set) var stickyBottom = PCodeStickyBottomState()
    private(set) var selection: PCodeTranscriptSelection?

    var typography: PCodeTypographySnapshot
    var theme: PCodeTranscriptTheme
    var highlighter: any PCodeSyntaxHighlighter

    private var mountedViews: [Int: any PCodeTranscriptRowHosting] = [:]
    private var appliedStates: [Int: AppliedRowState] = [:]
    private var reusePools: [PCodeTranscriptItem.Kind: [any PCodeTranscriptRowHosting]] = [:]
    private var attributedCache: [String: NSAttributedString] = [:]
    private var appearanceGeneration: UInt64 = 0
    private var lastContentWidth: CGFloat = 0
    private var lastClipOriginY: CGFloat = 0
    private var isApplyingDocumentHeight = false
    private var hoveredItemIndex: Int?

    // Frame-coalesced streaming state
    private struct ActiveStreamingMessage {
        let id: String
        let baseItemIndex: Int
        var differ: PCodeStreamingMessageDiffer
        var pacer: PCodeStreamingTextPacer
        var displayedMarkdown: String
        var hasDirectMarkdownUpdate: Bool
        var activeRatchetIndex: Int?
    }

    private var activeStream: ActiveStreamingMessage?
    private var smoothGlide = PCodeSmoothScrollGlide()
    private var lastDisplayLinkTimestamp: CFTimeInterval?
    private var streamingTurnCounter: Int = 0
    private var displayLink: CADisplayLink?
    private var isDraggingSelection = false
    private var dragMoved = false
    private var trackingArea: NSTrackingArea?

    /// True when new streaming content has arrived while the user is scrolled up.
    private(set) var hasUnreadStreamingContent: Bool = false
    /// Invoked whenever the floating "Jump to bottom" pill visibility should change.
    var onJumpToBottomVisibilityChanged: ((Bool) -> Void)?
    /// Exposed for tests/telemetry: whether the most recent streaming diff took the open-fence fast path.
    var lastStreamingUpdateUsedOpenFenceFastPath: Bool {
        activeStream?.differ.lastUpdateUsedOpenFenceFastPath ?? false
    }
    /// True while either the text pacer or the 120Hz smooth-scroll glide has active work.
    var isStreamingAnimationActive: Bool {
        (activeStream?.pacer.hasPending == true)
            || (activeStream?.hasDirectMarkdownUpdate == true)
            || smoothGlide.isActive
    }
    /// True while the 120Hz text pacer still has buffered characters waiting to be revealed.
    var hasPacedStreamingBacklog: Bool {
        activeStream?.pacer.hasPending == true
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
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let window {
            layoutEngine.backingScale = window.backingScaleFactor
            if isStreamingAnimationActive {
                ensureDisplayLinkRunning()
            }
        }
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

    override func mouseExited(with event: NSEvent) {
        setHoveredIndex(nil)
        NSCursor.arrow.set()
    }

    private func updateHover(at point: CGPoint) {
        guard let idx = layoutEngine.itemIndex(atY: point.y),
              let rowView = mountedViews[idx],
              rowView.frame.contains(point)
        else {
            setHoveredIndex(nil)
            return
        }
        setHoveredIndex(idx)
    }

    private func setHoveredIndex(_ newIndex: Int?) {
        guard hoveredItemIndex != newIndex else { return }
        if let old = hoveredItemIndex {
            mountedViews[old]?.setHovered(false)
        }
        hoveredItemIndex = newIndex
        if let idx = newIndex {
            mountedViews[idx]?.setHovered(true)
        }
    }

    /// Replace all transcript items (e.g. when opening Peek or loading history).
    func setItems(_ newItems: [PCodeTranscriptItem], pinToBottom: Bool) {
        items = newItems
        selection = nil
        hoveredItemIndex = nil
        activeStream = nil
        smoothGlide.reset()
        setHasUnreadStreamingContent(false)
        attributedCache.removeAll(keepingCapacity: true)
        stopDisplayLink()
        recycleAllMountedViews()

        layoutEngine.backingScale = window?.backingScaleFactor ?? 2
        let estimates = newItems.map { item in
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
    }

    // MARK: - Live Streaming Pipeline

    /// Start a new turn by appending the user prompt. Pass `forcePin: true` for user-submitted
    /// prompts so the view re-pins to bottom, or `false` when replaying/streaming background turns.
    func beginStreamingTurn(prompt: String, isSide: Bool = false, forcePin: Bool = true) {
        finishStreamingMessage()
        streamingTurnCounter += 1
        let item = PCodeTranscriptItem(
            id: "stream-turn-\(streamingTurnCounter)-prompt",
            payload: .prompt(label: isSide ? "/btw" : "You", text: prompt, isSide: isSide),
            spacingAfter: 10)
        items.append(item)
        layoutEngine.append(
            id: item.id,
            height: estimatedHeight(for: item),
            spacingAfter: item.spacingAfter,
            measured: false)
        if forcePin {
            stickyBottom.pinToBottom()
            setHasUnreadStreamingContent(false)
        } else if stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }
        mountViews(in: visibleRect)
    }

    /// Begin streaming a new assistant Markdown message at the tail of the transcript.
    func beginStreamingMessage(id: String) {
        finishStreamingMessage()
        activeStream = ActiveStreamingMessage(
            id: id,
            baseItemIndex: items.count,
            differ: PCodeStreamingMessageDiffer(),
            pacer: PCodeStreamingTextPacer(),
            displayedMarkdown: "",
            hasDirectMarkdownUpdate: false,
            activeRatchetIndex: nil)
    }

    /// Append a token delta to the active streaming message (paced at 120Hz via `CADisplayLink`).
    func appendStreamingDelta(_ delta: String) {
        guard !delta.isEmpty else { return }
        if activeStream == nil {
            beginStreamingMessage(id: "stream-msg-\(items.count)")
        }
        activeStream?.pacer.enqueue(delta)
        scheduleStreamingFlush()
    }

    /// Replace the active streaming message's full Markdown text (coalesced to the next display frame).
    func setStreamingMessageMarkdown(_ fullMarkdown: String) {
        if activeStream == nil {
            beginStreamingMessage(id: "stream-msg-\(items.count)")
        }
        activeStream?.pacer.reset()
        activeStream?.displayedMarkdown = fullMarkdown
        activeStream?.hasDirectMarkdownUpdate = true
        scheduleStreamingFlush()
    }

    /// Finalize the active streaming message, draining any paced tail, releasing its ratchet,
    /// and attaching Copy Markdown.
    func finishStreamingMessage() {
        if var stream = activeStream {
            if let remaining = stream.pacer.flushAll() {
                stream.displayedMarkdown.append(remaining)
                stream.hasDirectMarkdownUpdate = true
            }
            activeStream = stream
            if stream.hasDirectMarkdownUpdate {
                flushStreamingMessage()
            }
        }
        guard let stream = activeStream else {
            if !smoothGlide.isActive { stopDisplayLink() }
            return
        }
        if let ratchetIdx = stream.activeRatchetIndex {
            layoutEngine.setTailRatchet(at: ratchetIdx, active: false)
        }
        // Attach full copyMarkdown to the first prose block and refresh `fullCopyCode`
        // on any multi-chunk code blocks whose earlier chunks were kept untouched while streaming.
        let blocks = stream.differ.blocks
        let endIdx = min(items.count, stream.baseItemIndex + blocks.count)
        if stream.baseItemIndex < endIdx {
            var attachedCopyMarkdown = false
            for idx in stream.baseItemIndex..<endIdx {
                let localIdx = idx - stream.baseItemIndex
                if !attachedCopyMarkdown,
                   !stream.displayedMarkdown.isEmpty,
                   case .prose(let md, let refDefs, _) = items[idx].payload
                {
                    items[idx] = PCodeTranscriptItem(
                        id: items[idx].id,
                        payload: .prose(markdown: md, referenceDefinitions: refDefs, copyMarkdown: stream.displayedMarkdown),
                        spacingAfter: items[idx].spacingAfter,
                        revision: items[idx].revision &+ 1)
                    attachedCopyMarkdown = true
                } else if case .codeBlock(let lang, let code, let oldFull, let showHeader, let groupID) = items[idx].payload,
                          case .codeBlock(_, _, let latestFull, _, let totalChunks) = blocks[localIdx]
                {
                    let needsGroup = totalChunks > 1 ? (groupID ?? "\(stream.id)-code-\(localIdx)") : nil
                    if oldFull != latestFull || groupID != needsGroup {
                        items[idx] = PCodeTranscriptItem(
                            id: items[idx].id,
                            payload: .codeBlock(
                                language: lang,
                                code: code,
                                fullCopyCode: latestFull,
                                showHeader: showHeader,
                                groupID: needsGroup),
                            spacingAfter: items[idx].spacingAfter,
                            revision: items[idx].revision &+ 1)
                    }
                }
            }
        }
        activeStream = nil
        mountViews(in: visibleRect)
        if !smoothGlide.isActive {
            stopDisplayLink()
        }
    }

    /// Append a tool card to the live stream (finalizing any preceding open assistant message).
    func appendStreamingToolCall(_ call: PCodePeekToolCall) {
        finishStreamingMessage()
        let item = PCodeTranscriptItem(
            id: "stream-turn-\(streamingTurnCounter)-tool-\(call.id)-\(items.count)",
            payload: .tool(call, isExpanded: false),
            spacingAfter: 5)
        items.append(item)
        layoutEngine.append(
            id: item.id,
            height: estimatedHeight(for: item),
            spacingAfter: item.spacingAfter,
            measured: false)
        if stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }
        mountViews(in: visibleRect)
    }

    /// Finalize the current streaming turn with its terminal status.
    func finishStreamingTurn(status: PCodePeekTurn.Status) {
        finishStreamingMessage()
        if status != .complete {
            let item = PCodeTranscriptItem(
                id: "stream-turn-\(streamingTurnCounter)-status-\(items.count)",
                payload: .status(status),
                spacingAfter: 14)
            items.append(item)
            layoutEngine.append(
                id: item.id,
                height: estimatedHeight(for: item),
                spacingAfter: item.spacingAfter,
                measured: false)
            if stickyBottom.mode == .userScrolledUp {
                setHasUnreadStreamingContent(true)
            }
        } else if let lastIdx = items.indices.last {
            items[lastIdx] = items[lastIdx].withSpacingAfter(18)
            layoutEngine.updateSpacingAfter(at: lastIdx, spacingAfter: 18)
        }
        mountViews(in: visibleRect)
    }

    /// Re-pin to bottom and scroll immediately (used by the floating "Jump to bottom" pill).
    func scrollToBottomAndPin() {
        smoothGlide.reset()
        stickyBottom.pinToBottom()
        setHasUnreadStreamingContent(false)
        applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: true, allowSmoothGlide: false)
        mountViews(in: visibleRect)
    }

    /// Explicitly unpin sticky-bottom tracking (e.g. Find jump or programmatic scroll-up).
    func unpinFromBottom() {
        smoothGlide.reset()
        stickyBottom.unpinForUserAction()
    }

    /// Programmatically update the model-owned selection (e.g. Find or tests).
    func setSelection(_ newSelection: PCodeTranscriptSelection?) {
        selection = newSelection
        updateMountedSelectionHighlights()
    }

    /// Legacy convenience wrapper: stream full Markdown into the last item (creating a stream if needed).
    func enqueueStreamingTailText(_ fullText: String) {
        if activeStream == nil {
            if let lastIdx = items.indices.last {
                activeStream = ActiveStreamingMessage(
                    id: items[lastIdx].id,
                    baseItemIndex: lastIdx,
                    differ: PCodeStreamingMessageDiffer(),
                    pacer: PCodeStreamingTextPacer(),
                    displayedMarkdown: "",
                    hasDirectMarkdownUpdate: false,
                    activeRatchetIndex: lastIdx)
            } else {
                beginStreamingMessage(id: "stream-tail")
            }
        }
        setStreamingMessageMarkdown(fullText)
    }

    /// Legacy convenience wrapper: release the streaming tail height ratchet once the turn finishes.
    func finishStreamingTail() {
        finishStreamingMessage()
    }

    private func setHasUnreadStreamingContent(_ value: Bool) {
        guard hasUnreadStreamingContent != value else { return }
        hasUnreadStreamingContent = value
        onJumpToBottomVisibilityChanged?(value && stickyBottom.mode == .userScrolledUp)
    }

    private func scheduleStreamingFlush() {
        if window != nil {
            ensureDisplayLinkRunning()
        } else {
            // Headless / off-window mode: drain synchronously for deterministic tests.
            if var stream = activeStream, let drained = stream.pacer.flushAll() {
                stream.displayedMarkdown.append(drained)
                stream.hasDirectMarkdownUpdate = true
                activeStream = stream
            }
            flushStreamingMessage()
        }
    }

    /// Update typography or theme, invalidating heights while preserving the top-visible scroll anchor.
    func updateAppearance(typography newTypography: PCodeTypographySnapshot, theme newTheme: PCodeTranscriptTheme) {
        let needsRemeasure = newTypography != typography
        let themeChanged = newTheme != theme
        guard needsRemeasure || themeChanged else { return }
        typography = newTypography
        theme = newTheme
        appearanceGeneration &+= 1
        attributedCache.removeAll(keepingCapacity: true)
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
            layoutEngine.invalidateAllMeasurements()
            remountVisibleViews()
        }
    }

    // MARK: - Scroll Notifications (called by PCodeTranscriptScrollView)

    func handleWillStartLiveScroll() {
        smoothGlide.reset()
        stickyBottom.didStartLiveScroll()
        lastClipOriginY = enclosingScrollView?.contentView.bounds.origin.y ?? 0
    }

    func handleDidLiveScroll() {
        guard let scrollView = enclosingScrollView else { return }
        let originY = scrollView.contentView.bounds.origin.y
        let movedUp = originY < lastClipOriginY - 0.5
        lastClipOriginY = originY
        let dist = distanceFromBottom(in: scrollView)
        stickyBottom.didLiveScroll(distanceFromBottom: dist, movedUpward: movedUp)
    }

    func handleDidEndLiveScroll() {
        guard let scrollView = enclosingScrollView else { return }
        stickyBottom.didEndLiveScroll(distanceFromBottom: distanceFromBottom(in: scrollView))
        if stickyBottom.mode == .pinnedToBottom {
            setHasUnreadStreamingContent(false)
        }
    }

    func handleBoundsDidChange() {
        guard let scrollView = enclosingScrollView else { return }
        let originY = scrollView.contentView.bounds.origin.y
        if isApplyingDocumentHeight {
            lastClipOriginY = originY
            return
        }
        if !stickyBottom.isLiveScrolling {
            let movedUp = originY < lastClipOriginY - 0.5
            let dist = distanceFromBottom(in: scrollView)
            stickyBottom.didLegacyWheelScroll(distanceFromBottom: dist, movedUpward: movedUp)
            if movedUp && stickyBottom.mode == .userScrolledUp {
                smoothGlide.reset()
            }
            if stickyBottom.mode == .pinnedToBottom {
                setHasUnreadStreamingContent(false)
            }
        }
        lastClipOriginY = originY
        mountViews(in: visibleRect)
    }

    // MARK: - Mounting & Reuse Pool

    private func mountViews(in rect: NSRect, pass: Int = 0) {
        guard !items.isEmpty else {
            let clipHeight = enclosingScrollView?.contentView.bounds.height ?? 0
            let targetHeight = max(layoutEngine.totalHeight, clipHeight)
            if abs(bounds.height - targetHeight) > 0.5 {
                applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: false, allowSmoothGlide: false)
            }
            return
        }
        let contentWidth = max(1, bounds.width - Self.horizontalPadding * 2)
        let range = layoutEngine.visibleRange(in: rect)

        // Recycle views that scrolled out of `rect`.
        for (idx, view) in mountedViews where !range.contains(idx) {
            view.resetForTranscriptReuse()
            view.removeFromSuperview()
            reusePools[view.itemKind, default: []].append(view)
            mountedViews.removeValue(forKey: idx)
            appliedStates.removeValue(forKey: idx)
        }

        let viewportTop = enclosingScrollView?.contentView.bounds.origin.y ?? 0
        var accumulatedScrollDelta: CGFloat = 0

        for idx in range {
            let item = items[idx]
            let rowView: any PCodeTranscriptRowHosting
            var isFreshMount = false

            if let existing = mountedViews[idx], existing.itemKind == item.kind {
                rowView = existing
            } else {
                if let existing = mountedViews.removeValue(forKey: idx) {
                    existing.resetForTranscriptReuse()
                    existing.removeFromSuperview()
                    reusePools[existing.itemKind, default: []].append(existing)
                }
                appliedStates.removeValue(forKey: idx)
                rowView = dequeueRowView(for: item.kind)
                addSubview(rowView)
                mountedViews[idx] = rowView
                isFreshMount = true
            }

            let targetState = AppliedRowState(
                itemID: item.id,
                revision: item.revision,
                width: contentWidth,
                appearanceGeneration: appearanceGeneration)

            let needsConfigure = isFreshMount
                || appliedStates[idx] != targetState
                || !layoutEngine.entries[idx].isMeasured

            if needsConfigure {
                let measured = applyRowView(rowView, item: item, index: idx, width: contentWidth)
                appliedStates[idx] = targetState

                let delta = layoutEngine.updateHeight(
                    at: idx,
                    measuredHeight: measured,
                    viewportTop: viewportTop + accumulatedScrollDelta,
                    isPinnedToBottom: stickyBottom.shouldProgrammaticallyPin)
                accumulatedScrollDelta += delta.scrollOriginDelta
            } else {
                let len = rowView.selectableTextLength
                rowView.setSelectedRange(selection?.localRange(forItem: idx, utf16Length: len))
            }

            rowView.setHovered(hoveredItemIndex == idx)

            let entry = layoutEngine.entries[idx]
            let targetFrame = CGRect(
                x: Self.horizontalPadding,
                y: entry.yOffset,
                width: contentWidth,
                height: entry.height)
            if rowView.frame != targetFrame {
                rowView.frame = targetFrame
            }
        }

        let clipHeight = enclosingScrollView?.contentView.bounds.height ?? 0
        let targetHeight = max(layoutEngine.totalHeight, clipHeight)
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
                width: width,
                theme: theme,
                selectedRange: localSel)

        case (let view as PCodeProseRowView, .prose(let markdown, let refDefs, let copyMarkdown)):
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
                    self?.toggleToolExpansion(at: index)
                })

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

    private func toggleToolExpansion(at index: Int) {
        guard items.indices.contains(index),
              case .tool(let call, let isExpanded) = items[index].payload
        else { return }
        if !isExpanded {
            stickyBottom.unpinForUserAction()
        }
        attributedCache.removeValue(forKey: items[index].id)
        items[index] = PCodeTranscriptItem(
            id: items[index].id,
            payload: .tool(call, isExpanded: !isExpanded),
            spacingAfter: items[index].spacingAfter,
            revision: items[index].revision &+ 1)
        mountViews(in: visibleRect)
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
        let targetHeight = max(newHeight, clipView.bounds.height)
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
                rawOriginY = targetBottomY - smoothGlide.remainingOffset
            } else {
                smoothGlide.reset()
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
        lastDisplayLinkTimestamp = nil
        let proxy = PCodeDisplayLinkProxy(target: self)
        let link = displayLink(target: proxy, selector: #selector(PCodeDisplayLinkProxy.tick(_:)))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    private func stopDisplayLink() {
        displayLink?.invalidate()
        displayLink = nil
        lastDisplayLinkTimestamp = nil
    }

    fileprivate func handleDisplayLinkTick(_ sender: CADisplayLink) {
        let now = sender.timestamp
        let dt: TimeInterval
        if let last = lastDisplayLinkTimestamp, now > last {
            dt = min(max(now - last, 1.0 / 240.0), 0.1)
        } else {
            dt = 1.0 / 120.0
        }
        lastDisplayLinkTimestamp = now

        // 1. Pace buffered streaming deltas smoothly on this display frame.
        if var stream = activeStream {
            if let drained = stream.pacer.drain(dt: dt) {
                stream.displayedMarkdown.append(drained)
                stream.hasDirectMarkdownUpdate = true
                activeStream = stream
            }
            if activeStream?.hasDirectMarkdownUpdate == true {
                flushStreamingMessage()
            }
        }

        // 2. Step the 120Hz critically-damped spring smooth-scroll glide while pinned.
        if smoothGlide.isActive {
            if stickyBottom.shouldProgrammaticallyPin, let scrollView = enclosingScrollView {
                let remaining = smoothGlide.step(dt: dt)
                let clipView = scrollView.contentView
                let targetBottomY = PCodeStickyBottomState.bottomOriginY(
                    documentHeight: frame.size.height,
                    clipHeight: clipView.bounds.height,
                    topInset: scrollView.contentInsets.top,
                    bottomInset: scrollView.contentInsets.bottom)
                let desiredY = targetBottomY - remaining
                let clamped = clipView.constrainBoundsRect(
                    CGRect(origin: CGPoint(x: clipView.bounds.origin.x, y: desiredY), size: clipView.bounds.size))
                if abs(clamped.origin.y - clipView.bounds.origin.y) > 0.1 {
                    isApplyingDocumentHeight = true
                    CATransaction.begin()
                    CATransaction.setDisableActions(true)
                    clipView.scroll(to: clamped.origin)
                    scrollView.reflectScrolledClipView(clipView)
                    CATransaction.commit()
                    lastClipOriginY = clipView.bounds.origin.y
                    isApplyingDocumentHeight = false
                    mountViews(in: visibleRect)
                }
            } else {
                smoothGlide.reset()
            }
        }

        // Pause the display link as soon as both pacer and smooth-scroll glide settle.
        if !isStreamingAnimationActive {
            stopDisplayLink()
        }
    }

    private func flushStreamingMessage() {
        guard var stream = activeStream, stream.hasDirectMarkdownUpdate else {
            if !isStreamingAnimationActive {
                stopDisplayLink()
            }
            return
        }
        stream.hasDirectMarkdownUpdate = false

        let diff = stream.differ.update(markdown: stream.displayedMarkdown)
        let newBlockItems = Self.buildStreamingItems(messageID: stream.id, blocks: diff.newBlocks)

        let base = stream.baseItemIndex
        let oldEnd = items.count
        let newEnd = base + newBlockItems.count
        let diffStart = base + diff.commonPrefixCount

        // Only mutate blocks at or after `commonPrefixCount` so earlier blocks keep their
        // view identity, attributed cache, layout heights, and user text selection untouched.
        let overlapEnd = min(oldEnd, newEnd)
        if diffStart < overlapEnd {
            for globalIdx in diffStart..<overlapEnd {
                let localIdx = globalIdx - base
                let candidate = newBlockItems[localIdx]
                let existing = items[globalIdx]
                attributedCache.removeValue(forKey: existing.id)

                if existing.kind == candidate.kind {
                    items[globalIdx] = PCodeTranscriptItem(
                        id: existing.id,
                        payload: candidate.payload,
                        spacingAfter: candidate.spacingAfter,
                        revision: existing.revision &+ 1)
                    layoutEngine.updateSpacingAfter(at: globalIdx, spacingAfter: candidate.spacingAfter)
                } else {
                    if let oldView = mountedViews.removeValue(forKey: globalIdx) {
                        oldView.resetForTranscriptReuse()
                        oldView.removeFromSuperview()
                        reusePools[oldView.itemKind, default: []].append(oldView)
                    }
                    appliedStates.removeValue(forKey: globalIdx)
                    items[globalIdx] = candidate
                    layoutEngine.replaceEntry(at: globalIdx, id: candidate.id, spacingAfter: candidate.spacingAfter)
                }
            }
        }

        // Append newly split trailing blocks (e.g. prose -> code block or code block chunk > 200 lines).
        if newEnd > oldEnd {
            for globalIdx in oldEnd..<newEnd {
                let localIdx = globalIdx - base
                let item = newBlockItems[localIdx]
                items.append(item)
                layoutEngine.append(
                    id: item.id,
                    height: estimatedHeight(for: item),
                    spacingAfter: item.spacingAfter,
                    measured: false)
            }
        } else if newEnd < oldEnd {
            // Shrink trailing blocks if an incomplete Markdown construct collapsed.
            for globalIdx in newEnd..<oldEnd {
                attributedCache.removeValue(forKey: items[globalIdx].id)
                if let oldView = mountedViews.removeValue(forKey: globalIdx) {
                    oldView.resetForTranscriptReuse()
                    oldView.removeFromSuperview()
                    reusePools[oldView.itemKind, default: []].append(oldView)
                }
                appliedStates.removeValue(forKey: globalIdx)
            }
            items.removeSubrange(newEnd..<oldEnd)
            layoutEngine.removeTail(from: newEnd)
            clampSelectionToCurrentItems()
        }

        // Move tail ratchet to the active trailing block; release settled predecessor blocks.
        let newTailIdx = newEnd > base ? (newEnd - 1) : nil
        if let oldRatchet = stream.activeRatchetIndex, oldRatchet != newTailIdx {
            layoutEngine.setTailRatchet(at: oldRatchet, active: false)
        }
        stream.activeRatchetIndex = newTailIdx
        activeStream = stream

        if stickyBottom.mode == .userScrolledUp {
            setHasUnreadStreamingContent(true)
        }

        mountViews(in: visibleRect)

        if let tailIdx = newTailIdx, items.indices.contains(tailIdx) {
            layoutEngine.setTailRatchet(at: tailIdx, active: true)
        }
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
        }
        if sel.focus.itemIndex > maxIdx {
            let maxLen = (plainTextForCopy(at: maxIdx) as NSString).length
            sel.focus = .init(itemIndex: maxIdx, utf16Offset: maxLen)
        }
        selection = sel.isEmpty ? nil : sel
    }

    private static func buildStreamingItems(
        messageID: String,
        blocks: [PCodeMarkdownBlock]
    ) -> [PCodeTranscriptItem] {
        var result: [PCodeTranscriptItem] = []
        result.reserveCapacity(blocks.count)
        var codeGroupCounter = 0
        var activeCodeGroupID: String?

        for (subIdx, block) in blocks.enumerated() {
            switch block {
            case .prose(let proseMD, let refDefs):
                activeCodeGroupID = nil
                result.append(PCodeTranscriptItem(
                    id: "\(messageID)-b\(subIdx)",
                    payload: .prose(markdown: proseMD, referenceDefinitions: refDefs, copyMarkdown: nil),
                    spacingAfter: 8))
            case .codeBlock(let lang, let code, let fullCode, let chunkIdx, let totalChunks):
                if chunkIdx == 0 {
                    activeCodeGroupID = "\(messageID)-code-\(codeGroupCounter)"
                    codeGroupCounter += 1
                }
                let isLastChunk = (chunkIdx == totalChunks - 1)
                result.append(PCodeTranscriptItem(
                    id: "\(messageID)-b\(subIdx)",
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
                    id: "\(messageID)-b\(subIdx)",
                    payload: .table(table),
                    spacingAfter: 10))
            }
        }
        return result
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
            }
        }
    }

    func validateUserInterfaceItem(_ item: any NSValidatedUserInterfaceItem) -> Bool {
        if item.action == #selector(copy(_:)) {
            return selection.map { !$0.isEmpty } ?? false
        }
        if item.action == #selector(selectAll(_:)) {
            return !items.isEmpty
        }
        return false
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
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(copied, forType: .string)
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
        case .status(let status):
            return cachedStatusAttributedString(for: item.id, status: status).string
        }
    }

    private func selectionPosition(at docPoint: CGPoint) -> PCodeTranscriptSelection.Position? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y) else { return nil }
        if let rowView = mountedViews[idx], let textView = rowView.primaryTextView {
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
              let rowView = mountedViews[idx],
              rowView.frame.contains(docPoint),
              let textView = rowView.primaryTextView
        else { return nil }
        let localPt = textView.convert(docPoint, from: self)
        return textView.linkURL(at: localPt)
    }

    private func updateMountedSelectionHighlights() {
        for (idx, rowView) in mountedViews {
            let len = rowView.selectableTextLength
            rowView.setSelectedRange(selection?.localRange(forItem: idx, utf16Length: len))
        }
    }

    // MARK: - Attributed String Cache & Height Estimates

    private func estimatedHeight(for item: PCodeTranscriptItem) -> CGFloat {
        switch item.payload {
        case .tool: return 28
        case .status: return 24
        case .prompt(_, let text, _):
            let lines = max(1, min(8, text.count / 68 + 1))
            return 28 + CGFloat(lines) * typography.lineHeight()
        case .prose(let markdown, _, _):
            let lines = max(1, min(14, markdown.count / 72 + 1))
            return CGFloat(lines) * typography.lineHeight()
        case .codeBlock(_, let code, _, let showHeader, _):
            let lineCount = max(1, code.split(separator: "\n", omittingEmptySubsequences: false).count)
            return (showHeader ? PCodeCodeBlockView.headerHeight : 0) + 20 + CGFloat(lineCount) * typography.lineHeight(scale: typography.codeBlockScale)
        case .table(let table):
            let rowCount = table.rows.count + (table.headers.isEmpty ? 0 : 1)
            return CGFloat(max(1, rowCount)) * (typography.lineHeight(scale: 0.92) + 14)
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
        attributedCache[id] = built
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
        let paragraph = NSMutableParagraphStyle()
        let lineHeight = typography.lineHeight()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight

        let out = NSMutableAttributedString()
        let labelAttrs: [NSAttributedString.Key: Any] = [
            .font: typography.proseNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight),
            .foregroundColor: isSide ? NSColor.systemOrange : theme.accent,
        ]
        out.append(NSAttributedString(string: label + "\n", attributes: labelAttrs))
        let bodyAttrs: [NSAttributedString.Key: Any] = [
            .font: typography.proseNSFont(),
            .foregroundColor: theme.foreground,
            .paragraphStyle: paragraph,
        ]
        out.append(NSAttributedString(string: text, attributes: bodyAttrs))
        attributedCache[id] = out
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
        attributedCache[id] = built
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
        attributedCache[id] = built
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

/// Native `NSScrollView` hosting `PCodeTranscriptDocumentView` with live-scroll observation
/// (without overriding `scrollWheel`, preserving AppKit responsive scrolling) and a floating
/// "Jump to bottom" pill when scrolled up during live streaming.
final class PCodeTranscriptScrollView: NSScrollView {
    let transcriptDocumentView: PCodeTranscriptDocumentView
    let jumpToBottomButton = PCodeJumpToBottomButton()
    private var observers: [NSObjectProtocol] = []
    private var typographyCancellable: AnyCancellable?

    var isJumpToBottomVisible: Bool { !jumpToBottomButton.isHidden }

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

        let nc = NotificationCenter.default
        observers.append(nc.addObserver(
            forName: NSScrollView.willStartLiveScrollNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            self?.transcriptDocumentView.handleWillStartLiveScroll()
        })
        observers.append(nc.addObserver(
            forName: NSScrollView.didLiveScrollNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            self?.transcriptDocumentView.handleDidLiveScroll()
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
    }

    @objc private func jumpToBottomPressed() {
        transcriptDocumentView.scrollToBottomAndPin()
    }

    override func layout() {
        super.layout()
        let clipWidth = contentView.bounds.width
        if clipWidth > 0, abs(transcriptDocumentView.frame.width - clipWidth) > 0.5 {
            transcriptDocumentView.frame.size.width = clipWidth
        }
        layoutJumpToBottomButton()
    }

    private func layoutJumpToBottomButton() {
        let naturalWidth = ceil(jumpToBottomButton.fittingSize.width)
        let width = max(136, naturalWidth + 24)
        let height: CGFloat = 28
        let x = floor((bounds.width - width) * 0.5)
        let y: CGFloat = isFlipped ? (bounds.height - height - 14) : 14
        jumpToBottomButton.frame = CGRect(x: x, y: y, width: width, height: height)
    }
}

/// SwiftUI bridge hosting `PCodeTranscriptScrollView` with explicit `NSAppearance` forwarding
/// and optional live 4x streaming replay.
struct PCodeTranscriptRepresentable: NSViewRepresentable {
    let sessionID: String
    let turns: [PCodePeekTurn]
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    let pinToBottom: Bool
    let replayActive: Bool
    let onReplayFinished: (() -> Void)?

    init(
        sessionID: String,
        turns: [PCodePeekTurn],
        theme: PCodeTranscriptTheme,
        typography: PCodeTypography = .shared,
        pinToBottom: Bool = false,
        replayActive: Bool = false,
        onReplayFinished: (() -> Void)? = nil
    ) {
        self.sessionID = sessionID
        self.turns = turns
        self.theme = theme
        self.typography = typography
        self.pinToBottom = pinToBottom
        self.replayActive = replayActive
        self.onReplayFinished = onReplayFinished
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeTranscriptScrollView {
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        context.coordinator.lastSessionID = sessionID
        context.coordinator.lastTurnSignature = Self.turnSignature(turns)
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        context.coordinator.lastTypographyObjectID = ObjectIdentifier(typography)
        context.coordinator.lastReplayActive = replayActive
        if replayActive {
            context.coordinator.startReplay(
                turns: turns,
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
        let sig = Self.turnSignature(turns)
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

        let contentChanged = (coord.lastSessionID != sessionID || coord.lastTurnSignature != sig)
        let replayChanged = (coord.lastReplayActive != replayActive)

        if contentChanged || replayChanged {
            coord.lastSessionID = sessionID
            coord.lastTurnSignature = sig
            coord.lastReplayActive = replayActive

            if replayActive {
                coord.replayCompletedNaturally = false
                coord.startReplay(
                    turns: turns,
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

    private static func turnSignature(_ turns: [PCodePeekTurn]) -> Int {
        var hasher = Hasher()
        hasher.combine(turns.count)
        for turn in turns {
            hasher.combine(turn.id)
            hasher.combine(turn.prompt)
            hasher.combine(turn.status.rawValue)
            hasher.combine(turn.blocks.count)
            for block in turn.blocks {
                switch block {
                case .markdown(let id, let text):
                    hasher.combine(0)
                    hasher.combine(id)
                    hasher.combine(text)
                case .tool(let call):
                    hasher.combine(1)
                    hasher.combine(call.id)
                    hasher.combine(call.name)
                    hasher.combine(call.detail)
                    hasher.combine(call.command)
                    hasher.combine(call.failed)
                }
            }
        }
        return hasher.finalize()
    }

    final class Coordinator {
        var lastSessionID: String = ""
        var lastTurnSignature: Int = 0
        var lastTheme: PCodeTranscriptTheme?
        var lastTypographySnapshot: PCodeTypographySnapshot?
        var lastTypographyObjectID: ObjectIdentifier?
        var lastReplayActive: Bool = false
        var replayCompletedNaturally: Bool = false
        private var replayer: PCodeTranscriptStreamReplayer?

        func startReplay(
            turns: [PCodePeekTurn],
            documentView: PCodeTranscriptDocumentView,
            onComplete: (() -> Void)?
        ) {
            stopReplay()
            replayCompletedNaturally = false
            let r = PCodeTranscriptStreamReplayer(
                turns: turns,
                documentView: documentView,
                speedMultiplier: 4.0,
                chunkSize: 18,
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
