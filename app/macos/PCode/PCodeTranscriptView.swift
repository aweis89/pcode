import AppKit
import Combine
import QuartzCore

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
        case tool
        case status
    }

    let id: String
    let kind: Kind
    let plainText: String
    let secondaryText: String
    let isSidePrompt: Bool
    let isFailed: Bool
    let elapsedText: String?
    let spacingAfter: CGFloat

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
        self.kind = kind
        self.plainText = plainText
        self.secondaryText = secondaryText
        self.isSidePrompt = isSidePrompt
        self.isFailed = isFailed
        self.elapsedText = elapsedText
        self.spacingAfter = spacingAfter
    }

    /// Flatten `PCodePeekTurn`s into virtualized transcript items for Phase 0 spike & Peek.
    static func flatten(turns: [PCodePeekTurn]) -> [PCodeTranscriptItem] {
        var result: [PCodeTranscriptItem] = []
        result.reserveCapacity(turns.count * 4)
        for turn in turns {
            result.append(PCodeTranscriptItem(
                id: "turn-\(turn.id)-prompt",
                kind: .prompt,
                plainText: turn.prompt,
                secondaryText: turn.side ? "/btw" : "You",
                isSidePrompt: turn.side,
                spacingAfter: 10))
            for block in turn.blocks {
                switch block {
                case .markdown(let blockID, let text):
                    result.append(PCodeTranscriptItem(
                        id: "turn-\(turn.id)-md-\(blockID)",
                        kind: .prose,
                        plainText: text,
                        spacingAfter: 8))
                case .tool(let call):
                    result.append(PCodeTranscriptItem(
                        id: "turn-\(turn.id)-tool-\(call.id)",
                        kind: .tool,
                        plainText: call.name,
                        secondaryText: call.summaryText,
                        isFailed: call.failed,
                        elapsedText: call.formattedElapsed,
                        spacingAfter: 5))
                }
            }
            if turn.status != .complete {
                result.append(PCodeTranscriptItem(
                    id: "turn-\(turn.id)-status",
                    kind: .status,
                    plainText: turn.status.rawValue.capitalized,
                    isFailed: turn.status == .failed,
                    spacingAfter: 14))
            } else if !result.isEmpty {
                let last = result.removeLast()
                result.append(PCodeTranscriptItem(
                    id: last.id,
                    kind: last.kind,
                    plainText: last.plainText,
                    secondaryText: last.secondaryText,
                    isSidePrompt: last.isSidePrompt,
                    isFailed: last.isFailed,
                    elapsedText: last.elapsedText,
                    spacingAfter: 18))
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

/// Reusable item container view for virtualized rows in `PCodeTranscriptDocumentView`.
private final class PCodeTranscriptRowView: NSView {
    let kind: PCodeTranscriptItem.Kind
    let textView = PCodeTextKit2BlockView()
    private let accentBar = NSView()

    override var isFlipped: Bool { true }

    init(kind: PCodeTranscriptItem.Kind) {
        self.kind = kind
        super.init(frame: .zero)
        wantsLayer = true
        if kind == .prompt {
            accentBar.wantsLayer = true
            accentBar.layer?.cornerRadius = 1.5
            addSubview(accentBar)
        }
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    static func contentInsets(for kind: PCodeTranscriptItem.Kind) -> NSEdgeInsets {
        switch kind {
        case .prompt: return NSEdgeInsets(top: 10, left: 22, bottom: 10, right: 12)
        case .prose: return NSEdgeInsets(top: 2, left: 4, bottom: 2, right: 4)
        case .tool: return NSEdgeInsets(top: 5, left: 10, bottom: 5, right: 10)
        case .status: return NSEdgeInsets(top: 3, left: 8, bottom: 3, right: 8)
        }
    }

    func apply(
        item: PCodeTranscriptItem,
        attributedText: NSAttributedString,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        let insets = Self.contentInsets(for: kind)
        let textWidth = max(1, width - insets.left - insets.right)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: insets.left, y: insets.top, width: textWidth, height: textHeight)

        let totalHeight = textHeight + insets.top + insets.bottom
        effectiveAppearance.performAsCurrentDrawingAppearance {
            switch kind {
            case .prompt:
                layer?.backgroundColor = theme.promptBackground.cgColor
                layer?.cornerRadius = 8
                accentBar.layer?.backgroundColor = (item.isSidePrompt ? NSColor.systemOrange : theme.accent).cgColor
                accentBar.frame = CGRect(x: 11, y: 10, width: 3, height: max(12, totalHeight - 20))
            case .tool:
                layer?.backgroundColor = theme.toolBackground.cgColor
                layer?.cornerRadius = 5
            case .prose, .status:
                layer?.backgroundColor = nil
                layer?.cornerRadius = 0
            }
        }
        return totalHeight
    }
}

/// Flipped, virtualized AppKit document view supporting responsive scrolling (`prepareContent(in:)`),
/// bare TextKit 2 rendering, model-owned cross-item selection, and frame-coalesced streaming.
final class PCodeTranscriptDocumentView: NSView, NSUserInterfaceValidations {
    static let horizontalPadding: CGFloat = 18

    private(set) var items: [PCodeTranscriptItem] = []
    private(set) var layoutEngine = PCodeTranscriptLayoutEngine()
    private(set) var stickyBottom = PCodeStickyBottomState()
    private(set) var selection: PCodeTranscriptSelection?

    var typography: PCodeTypographySnapshot
    var theme: PCodeTranscriptTheme

    private var mountedViews: [Int: PCodeTranscriptRowView] = [:]
    private var reusePools: [PCodeTranscriptItem.Kind: [PCodeTranscriptRowView]] = [:]
    private var lastContentWidth: CGFloat = 0
    private var lastClipOriginY: CGFloat = 0
    private var isApplyingDocumentHeight = false

    // Frame-coalesced streaming state
    private var pendingTailText: String?
    private var displayLink: CADisplayLink?
    private var isDraggingSelection = false
    private var dragMoved = false
    private var trackingArea: NSTrackingArea?

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override class var isCompatibleWithResponsiveScrolling: Bool { true }

    init(typography: PCodeTypographySnapshot, theme: PCodeTranscriptTheme) {
        self.typography = typography
        self.theme = theme
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

    /// Own all mouse events directly so cross-item drag selection keeps receiving
    /// `mouseDragged`/`mouseUp` even if the starting row recycles offscreen during autoscroll.
    override func hitTest(_ point: NSPoint) -> NSView? {
        bounds.contains(point) ? self : nil
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
            options: [.activeInKeyWindow, .mouseMoved, .inVisibleRect],
            owner: self,
            userInfo: nil)
        addTrackingArea(area)
        trackingArea = area
    }

    override func mouseMoved(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        if linkURL(at: point) != nil {
            NSCursor.pointingHand.set()
        } else {
            NSCursor.iBeam.set()
        }
    }

    /// Replace all transcript items (e.g. when opening Peek or loading history).
    func setItems(_ newItems: [PCodeTranscriptItem], pinToBottom: Bool) {
        items = newItems
        selection = nil
        pendingTailText = nil
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
        mountViews(in: visibleRect)
    }

    /// Enqueue a streaming text update for the tail item (coalesced to the next display frame).
    func enqueueStreamingTailText(_ fullText: String) {
        pendingTailText = fullText
        if window != nil {
            ensureDisplayLinkRunning()
        } else {
            // Off-window/headless: apply immediately so pending state is never dropped.
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
        typography = newTypography
        theme = newTheme
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
            view.removeFromSuperview()
            reusePools[view.kind, default: []].append(view)
            mountedViews.removeValue(forKey: idx)
        }

        // Mount and measure visible views in one pass, anchoring any height deltas above viewport.
        let viewportTop = enclosingScrollView?.contentView.bounds.origin.y ?? 0
        var accumulatedScrollDelta: CGFloat = 0

        for idx in range {
            let item = items[idx]
            let rowView: PCodeTranscriptRowView
            if let existing = mountedViews[idx] {
                rowView = existing
            } else {
                rowView = dequeueRowView(for: item.kind)
                addSubview(rowView)
                mountedViews[idx] = rowView
            }

            let attr = attributedString(for: item)
            let localSel = selection?.localRange(forItem: idx, utf16Length: attr.length)
            let measured = rowView.apply(
                item: item,
                attributedText: attr,
                width: contentWidth,
                theme: theme,
                selectedRange: localSel)

            let delta = layoutEngine.updateHeight(
                at: idx,
                measuredHeight: measured,
                viewportTop: viewportTop + accumulatedScrollDelta,
                isPinnedToBottom: stickyBottom.shouldProgrammaticallyPin)
            accumulatedScrollDelta += delta.scrollOriginDelta

            let entry = layoutEngine.entries[idx]
            rowView.frame = CGRect(
                x: Self.horizontalPadding,
                y: entry.yOffset,
                width: contentWidth,
                height: entry.height)
        }

        let clipHeight = enclosingScrollView?.contentView.bounds.height ?? 0
        let targetHeight = max(layoutEngine.totalHeight, clipHeight)
        if abs(bounds.height - targetHeight) > 0.5 || abs(accumulatedScrollDelta) > 0.5 {
            applyDocumentHeight(
                layoutEngine.totalHeight,
                scrollOriginDelta: accumulatedScrollDelta,
                pinIfNeeded: stickyBottom.shouldProgrammaticallyPin)
            // Bounded second pass if height shrink or bottom-pin shift exposed new rows.
            if pass < 1, layoutEngine.visibleRange(in: visibleRect) != range {
                mountViews(in: visibleRect, pass: pass + 1)
            }
        }
    }

    private func remountVisibleViews() {
        for (_, rowView) in mountedViews {
            rowView.textView.needsDisplay = true
        }
        mountViews(in: visibleRect)
    }

    private func dequeueRowView(for kind: PCodeTranscriptItem.Kind) -> PCodeTranscriptRowView {
        if var pool = reusePools[kind], let recycled = pool.popLast() {
            reusePools[kind] = pool
            return recycled
        }
        return PCodeTranscriptRowView(kind: kind)
    }

    private func recycleAllMountedViews() {
        for (_, view) in mountedViews {
            view.removeFromSuperview()
            reusePools[view.kind, default: []].append(view)
        }
        mountedViews.removeAll()
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
        items[lastIdx] = PCodeTranscriptItem(
            id: old.id,
            kind: old.kind,
            plainText: text,
            secondaryText: old.secondaryText,
            isSidePrompt: old.isSidePrompt,
            isFailed: old.isFailed,
            elapsedText: old.elapsedText,
            spacingAfter: old.spacingAfter)
        // Measure first (via mountViews when visible), then ratchet so we lock measured height.
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

    @objc func copy(_ sender: Any?) {
        guard let selection, !selection.isEmpty else { return }
        let copied = selection.extractText(itemCount: items.count) { idx in
            attributedString(for: items[idx]).string
        }
        guard !copied.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(copied, forType: .string)
    }

    override func selectAll(_ sender: Any?) {
        guard let lastIdx = items.indices.last else { return }
        let lastLen = attributedString(for: items[lastIdx]).length
        selection = PCodeTranscriptSelection(
            anchor: .init(itemIndex: 0, utf16Offset: 0),
            focus: .init(itemIndex: lastIdx, utf16Offset: lastLen))
        updateMountedSelectionHighlights()
    }

    private func selectionPosition(at docPoint: CGPoint) -> PCodeTranscriptSelection.Position? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y) else { return nil }
        if let rowView = mountedViews[idx] {
            let localPt = rowView.textView.convert(docPoint, from: self)
            let offset = rowView.textView.utf16Offset(at: localPt)
            return PCodeTranscriptSelection.Position(itemIndex: idx, utf16Offset: offset)
        }
        let entry = layoutEngine.entries[idx]
        let len = attributedString(for: items[idx]).length
        let offset = docPoint.y < entry.yOffset + entry.height * 0.5 ? 0 : len
        return PCodeTranscriptSelection.Position(itemIndex: idx, utf16Offset: offset)
    }

    private func linkURL(at docPoint: CGPoint) -> URL? {
        guard let idx = layoutEngine.itemIndex(atY: docPoint.y),
              let rowView = mountedViews[idx],
              rowView.frame.contains(docPoint)
        else { return nil }
        let localPt = rowView.textView.convert(docPoint, from: self)
        return rowView.textView.linkURL(at: localPt)
    }

    private func updateMountedSelectionHighlights() {
        for (idx, rowView) in mountedViews {
            let len = rowView.textView.textContentStorage.attributedString?.length ?? 0
            rowView.textView.selectedRange = selection?.localRange(forItem: idx, utf16Length: len)
        }
    }

    // MARK: - Attributed String Construction & Estimates

    private func estimatedHeight(for item: PCodeTranscriptItem) -> CGFloat {
        switch item.kind {
        case .tool: return 28
        case .status: return 24
        case .prompt: return 56
        case .prose:
            let lines = max(1, min(12, item.plainText.count / 72 + 1))
            return CGFloat(lines) * typography.lineHeight()
        }
    }

    private func attributedString(for item: PCodeTranscriptItem) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        let lineHeight = typography.lineHeight()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight

        switch item.kind {
        case .prompt:
            let out = NSMutableAttributedString()
            let labelAttrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight),
                .foregroundColor: item.isSidePrompt ? NSColor.systemOrange : theme.accent,
            ]
            out.append(NSAttributedString(string: item.secondaryText + "\n", attributes: labelAttrs))
            let bodyAttrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(),
                .foregroundColor: theme.foreground,
                .paragraphStyle: paragraph,
            ]
            out.append(NSAttributedString(string: item.plainText, attributes: bodyAttrs))
            return out

        case .prose:
            let attrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(),
                .foregroundColor: theme.foreground,
                .paragraphStyle: paragraph,
            ]
            return NSAttributedString(string: item.plainText, attributes: attrs)

        case .tool:
            let out = NSMutableAttributedString()
            let nameAttrs: [NSAttributedString.Key: Any] = [
                .font: typography.codeNSFont(scale: 0.84, weight: typography.weight.nsEmphasisWeight),
                .foregroundColor: item.isFailed ? NSColor.systemRed : theme.secondaryForeground,
            ]
            out.append(NSAttributedString(string: item.plainText, attributes: nameAttrs))
            if !item.secondaryText.isEmpty {
                let detailAttrs: [NSAttributedString.Key: Any] = [
                    .font: typography.codeNSFont(scale: 0.84),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.85),
                ]
                out.append(NSAttributedString(string: "  " + item.secondaryText, attributes: detailAttrs))
            }
            if let elapsed = item.elapsedText {
                let timeAttrs: [NSAttributedString.Key: Any] = [
                    .font: typography.codeNSFont(scale: 0.78),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.65),
                ]
                out.append(NSAttributedString(string: "  (\(elapsed))", attributes: timeAttrs))
            }
            return out

        case .status:
            let attrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(scale: 0.82, weight: .medium),
                .foregroundColor: item.isFailed ? NSColor.systemRed : theme.accent,
            ]
            return NSAttributedString(string: item.plainText, attributes: attrs)
        }
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

        typographyCancellable = typography.snapshotPublisher.sink { [weak self] snapshot in
            guard let self else { return }
            self.transcriptDocumentView.updateAppearance(
                typography: snapshot,
                theme: self.transcriptDocumentView.theme)
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

    override func layout() {
        super.layout()
        let clipWidth = contentView.bounds.width
        if clipWidth > 0, abs(transcriptDocumentView.frame.width - clipWidth) > 0.5 {
            transcriptDocumentView.frame.size.width = clipWidth
        }
    }
}
