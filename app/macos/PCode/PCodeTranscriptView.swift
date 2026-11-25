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
    private var pendingTailText: String?
    private var displayLink: CADisplayLink?
    private var isDraggingSelection = false
    private var dragMoved = false
    private var trackingArea: NSTrackingArea?

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
            if pendingTailText != nil {
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
        pendingTailText = nil
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
        applyDocumentHeight(layoutEngine.totalHeight, scrollOriginDelta: 0, pinIfNeeded: pinToBottom)
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

    /// Enqueue a streaming text update for the tail item (coalesced to the next display frame).
    func enqueueStreamingTailText(_ fullText: String) {
        pendingTailText = fullText
        if window != nil {
            ensureDisplayLinkRunning()
        } else {
            pendingTailText = nil
            applyStreamingTailText(fullText)
        }
    }

    /// Release the streaming tail height ratchet once the active turn finishes.
    func finishStreamingTail() {
        if let text = pendingTailText {
            pendingTailText = nil
            applyStreamingTailText(text)
        }
        stopDisplayLink()
        if let lastIdx = items.indices.last {
            layoutEngine.setTailRatchet(at: lastIdx, active: false)
            mountViews(in: visibleRect)
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
        }
        lastClipOriginY = originY
        mountViews(in: visibleRect)
    }

    // MARK: - Mounting & Reuse Pool

    private func mountViews(in rect: NSRect, pass: Int = 0) {
        guard !items.isEmpty else { return }
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
    private func applyDocumentHeight(_ newHeight: CGFloat, scrollOriginDelta: CGFloat, pinIfNeeded: Bool) {
        guard let scrollView = enclosingScrollView else {
            frame.size.height = newHeight
            return
        }
        let clipView = scrollView.contentView
        let oldHeight = frame.size.height
        let targetHeight = max(newHeight, clipView.bounds.height)
        let rawOriginY: CGFloat
        if pinIfNeeded {
            rawOriginY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: targetHeight,
                clipHeight: clipView.bounds.height,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
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
        let proxy = PCodeDisplayLinkProxy(target: self)
        let link = displayLink(target: proxy, selector: #selector(PCodeDisplayLinkProxy.tick(_:)))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    private func stopDisplayLink() {
        displayLink?.invalidate()
        displayLink = nil
    }

    fileprivate func handleDisplayLinkTick(_ sender: CADisplayLink) {
        guard let text = pendingTailText else {
            stopDisplayLink()
            return
        }
        pendingTailText = nil
        applyStreamingTailText(text)
    }

    private func applyStreamingTailText(_ text: String) {
        guard let lastIdx = items.indices.last else { return }
        let old = items[lastIdx]
        attributedCache.removeValue(forKey: old.id)
        items[lastIdx] = PCodeTranscriptItem(
            id: old.id,
            payload: .prose(markdown: text, referenceDefinitions: "", copyMarkdown: text),
            spacingAfter: old.spacingAfter,
            revision: old.revision &+ 1)
        mountViews(in: visibleRect)
        layoutEngine.setTailRatchet(at: lastIdx, active: true)
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

/// Native `NSScrollView` hosting `PCodeTranscriptDocumentView` with live-scroll observation
/// (without overriding `scrollWheel`, preserving AppKit responsive scrolling).
final class PCodeTranscriptScrollView: NSScrollView {
    let transcriptDocumentView: PCodeTranscriptDocumentView
    private var observers: [NSObjectProtocol] = []
    private var typographyCancellable: AnyCancellable?

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

    override func layout() {
        super.layout()
        let clipWidth = contentView.bounds.width
        if clipWidth > 0, abs(transcriptDocumentView.frame.width - clipWidth) > 0.5 {
            transcriptDocumentView.frame.size.width = clipWidth
        }
    }
}

/// SwiftUI bridge hosting `PCodeTranscriptScrollView` with explicit `NSAppearance` forwarding.
struct PCodeTranscriptRepresentable: NSViewRepresentable {
    let sessionID: String
    let turns: [PCodePeekTurn]
    let theme: PCodeTranscriptTheme
    let typography: PCodeTypography
    let pinToBottom: Bool

    init(
        sessionID: String,
        turns: [PCodePeekTurn],
        theme: PCodeTranscriptTheme,
        typography: PCodeTypography = .shared,
        pinToBottom: Bool = false
    ) {
        self.sessionID = sessionID
        self.turns = turns
        self.theme = theme
        self.typography = typography
        self.pinToBottom = pinToBottom
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeTranscriptScrollView {
        let scrollView = PCodeTranscriptScrollView(typography: typography, theme: theme)
        let items = PCodeTranscriptItem.flatten(turns: turns)
        context.coordinator.lastSessionID = sessionID
        context.coordinator.lastTurnSignature = Self.turnSignature(turns)
        context.coordinator.lastTheme = theme
        context.coordinator.lastTypographySnapshot = typography.snapshot
        context.coordinator.lastTypographyObjectID = ObjectIdentifier(typography)
        scrollView.transcriptDocumentView.setItems(items, pinToBottom: pinToBottom)
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
        }

        if coord.lastSessionID != sessionID || coord.lastTurnSignature != sig {
            coord.lastSessionID = sessionID
            coord.lastTurnSignature = sig
            let items = PCodeTranscriptItem.flatten(turns: turns)
            nsView.transcriptDocumentView.setItems(items, pinToBottom: pinToBottom)
        }
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
    }
}
