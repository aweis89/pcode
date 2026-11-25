import AppKit
import CoreGraphics

/// Common interface for virtualized block views pooled by `PCodeTranscriptDocumentView`.
protocol PCodeTranscriptRowHosting: NSView {
    var itemKind: PCodeTranscriptItem.Kind { get }
    /// Primary text view for selection/link hit-testing (if applicable).
    var primaryTextView: PCodeTextKit2BlockView? { get }
    /// Set selection range in this item's local UTF-16 coordinates.
    func setSelectedRange(_ range: NSRange?)
    /// Current selectable text UTF-16 length.
    var selectableTextLength: Int { get }
    /// Update hover state (managed centrally by the document view's single `NSTrackingArea`).
    func setHovered(_ hovered: Bool)
    /// Reset transient state (scroll offsets, in-flight tasks, copy flash, selection) before pooling.
    func resetForTranscriptReuse()
    /// Apply a compositor-only top-to-bottom curtain reveal mask (`nil` = fully revealed, `0...1` = revealing).
    func setCurtainRevealProgress(_ progress: CGFloat?)
}

extension PCodeTranscriptRowHosting {
    func setHovered(_ hovered: Bool) {}

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        primaryTextView?.inkSweepFrontier = nil
    }

    func setCurtainRevealProgress(_ progress: CGFloat?) {
        guard let layer else { return }
        guard let progress else {
            if layer.mask != nil {
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                layer.mask = nil
                CATransaction.commit()
            }
            return
        }

        let clamped = min(max(0, progress), 1)
        if clamped >= 1 {
            setCurtainRevealProgress(nil)
            return
        }

        let maskLayer: CAGradientLayer
        if let existing = layer.mask as? CAGradientLayer {
            maskLayer = existing
        } else {
            let created = CAGradientLayer()
            created.colors = [
                NSColor.white.withAlphaComponent(1.0).cgColor,
                NSColor.white.withAlphaComponent(1.0).cgColor,
                NSColor.white.withAlphaComponent(0.0).cgColor,
            ]
            created.startPoint = CGPoint(x: 0.5, y: 0.0)
            created.endPoint = CGPoint(x: 0.5, y: 1.0)
            layer.mask = created
            maskLayer = created
        }

        let h = max(1, bounds.height)
        // Soft 1.5-row (~30pt) feather matching Ghostty Smooth's curtain reveal.
        let featherFrac = min(0.45, 30.0 / h)
        let lead = min(1.0, clamped * (1.0 + featherFrac))
        let solidEnd = max(0.0, lead - featherFrac)

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        maskLayer.frame = bounds
        maskLayer.locations = [
            NSNumber(value: 0.0),
            NSNumber(value: Double(solidEnd)),
            NSNumber(value: Double(lead)),
        ]
        CATransaction.commit()
    }
}

/// Inner horizontal `NSScrollView` for code blocks and wide tables.
///
/// - Forces `.overlay` scroller style so legacy scrollers never alter row heights.
/// - Latches gesture axis across finger lift + momentum phase and forwards vertical-dominant
///   scrolls to the outer `PCodeTranscriptScrollView` so vertical scrolling never gets trapped.
/// - Forwards mouse selection drags to `transcriptDocumentView` so cross-item selection
///   survives even if this row recycles offscreen during autoscroll.
final class PCodeHorizontalBlockScrollView: NSScrollView {
    weak var transcriptDocumentView: PCodeTranscriptDocumentView?

    private enum GestureAxis {
        case undecided
        case horizontal
        case vertical
    }

    private var gestureAxis: GestureAxis = .undecided

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        scrollerStyle = .overlay
        hasHorizontalScroller = true
        hasVerticalScroller = false
        autohidesScrollers = true
        drawsBackground = false
        borderType = .noBorder
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var scrollerStyle: NSScroller.Style {
        get { .overlay }
        set { super.scrollerStyle = .overlay }
    }

    private var canScrollHorizontally: Bool {
        guard let doc = documentView else { return false }
        return doc.frame.width > contentView.bounds.width + 1
    }

    func resetScrollOrigin() {
        gestureAxis = .undecided
        if contentView.bounds.origin != .zero {
            contentView.scroll(to: .zero)
            reflectScrolledClipView(contentView)
        }
    }

    override func scrollWheel(with event: NSEvent) {
        let phase = event.phase
        let momentum = event.momentumPhase

        if !canScrollHorizontally {
            gestureAxis = .undecided
            forwardScrollToOuter(event)
            return
        }

        // Wait for real deltas on `.mayBegin` instead of prematurely forwarding to outer.
        if phase == .mayBegin {
            gestureAxis = .undecided
            return
        }

        if phase == .began || (phase == [] && momentum == []) {
            gestureAxis = classifyAxis(dx: event.scrollingDeltaX, dy: event.scrollingDeltaY)
        } else if gestureAxis == .undecided {
            gestureAxis = classifyAxis(dx: event.scrollingDeltaX, dy: event.scrollingDeltaY)
        }

        let currentAxis = gestureAxis
        // Keep axis latched across `phase == .ended` so horizontal trackpad momentum stays horizontal.
        if momentum == .ended || momentum == .cancelled || phase == .cancelled {
            gestureAxis = .undecided
        }

        if currentAxis == .horizontal {
            super.scrollWheel(with: event)
        } else if currentAxis == .vertical {
            forwardScrollToOuter(event)
        }
    }

    private func classifyAxis(dx: CGFloat, dy: CGFloat) -> GestureAxis {
        let adx = abs(dx)
        let ady = abs(dy)
        if adx > ady * 1.15 && adx > 0.1 {
            return .horizontal
        }
        if ady > 0.1 {
            return .vertical
        }
        return .undecided
    }

    private func forwardScrollToOuter(_ event: NSEvent) {
        if let outer = transcriptDocumentView?.enclosingScrollView {
            outer.scrollWheel(with: event)
        } else {
            nextResponder?.scrollWheel(with: event)
        }
    }

    override func mouseDown(with event: NSEvent) {
        transcriptDocumentView?.mouseDown(with: event)
    }

    override func mouseDragged(with event: NSEvent) {
        transcriptDocumentView?.mouseDragged(with: event)
    }

    override func mouseUp(with event: NSEvent) {
        transcriptDocumentView?.mouseUp(with: event)
    }
}

/// Small, borderless action button used for "Copy" / "Copy Markdown" inside transcript blocks.
final class PCodeBlockActionButton: NSButton {
    private var resetWork: DispatchWorkItem?
    private var idleTitle: String
    private var idleSymbol: String
    private var isShowingCopied = false

    init(title: String, symbol: String) {
        self.idleTitle = title
        self.idleSymbol = symbol
        super.init(frame: .zero)
        isBordered = false
        bezelStyle = .inline
        imagePosition = .imageLeading
        wantsLayer = true
        layer?.cornerRadius = 4
        applyIdleState()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func applyTheme(foreground: NSColor, background: NSColor) {
        contentTintColor = foreground
        layer?.backgroundColor = background.cgColor
        if !isShowingCopied {
            applyIdleState()
        }
    }

    func flashCopied() {
        resetWork?.cancel()
        isShowingCopied = true
        setTitle("Copied", symbol: "checkmark", color: .systemGreen)
        let work = DispatchWorkItem { [weak self] in
            self?.isShowingCopied = false
            self?.applyIdleState()
        }
        resetWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2, execute: work)
    }

    func cancelCopiedFlash() {
        resetWork?.cancel()
        resetWork = nil
        if isShowingCopied {
            isShowingCopied = false
            applyIdleState()
        }
    }

    private func applyIdleState() {
        setTitle(idleTitle, symbol: idleSymbol, color: contentTintColor ?? .secondaryLabelColor)
    }

    private func setTitle(_ text: String, symbol: String, color: NSColor) {
        let cfg = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
        image = NSImage(systemSymbolName: symbol, accessibilityDescription: text)?
            .withSymbolConfiguration(cfg)
        attributedTitle = NSAttributedString(
            string: text.isEmpty ? "" : " " + text,
            attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: color,
            ])
        sizeToFit()
    }
}

// MARK: - Prompt Row

final class PCodePromptRowView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .prompt
    let textView = PCodeTextKit2BlockView()
    private let accentBar = NSView()
    private var lastTheme: PCodeTranscriptTheme?
    private var lastIsSide = false

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        accentBar.wantsLayer = true
        accentBar.layer?.cornerRadius = 1.5
        addSubview(accentBar)
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        attributedText: NSAttributedString,
        isSidePrompt: Bool,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        lastTheme = theme
        lastIsSide = isSidePrompt
        let insets = NSEdgeInsets(top: 10, left: 22, bottom: 10, right: 12)
        let textWidth = max(1, width - insets.left - insets.right)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: insets.left, y: insets.top, width: textWidth, height: textHeight)

        let totalHeight = textHeight + insets.top + insets.bottom
        accentBar.frame = CGRect(x: 11, y: 10, width: 3, height: max(12, totalHeight - 20))
        applyLayerColors()
        return totalHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.promptBackground.cgColor
            layer?.cornerRadius = 8
            accentBar.layer?.backgroundColor = (lastIsSide ? NSColor.systemOrange : theme.accent).cgColor
        }
    }
}

// MARK: - Prose Row (TextKit 2 Markdown + Hover "Copy Markdown")

final class PCodeProseRowView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .prose
    let textView = PCodeTextKit2BlockView()
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private var copyPayload: String?
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
        copyButton.isHidden = true
        copyButton.target = self
        copyButton.action = #selector(copyMarkdownPressed)
        addSubview(copyButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func setHovered(_ hovered: Bool) {
        copyButton.isHidden = !(hovered && copyPayload != nil)
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        copyPayload = nil
        copyButton.cancelCopiedFlash()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        if let theme = lastTheme {
            effectiveAppearance.performAsCurrentDrawingAppearance {
                copyButton.applyTheme(foreground: theme.secondaryForeground, background: theme.toolBackground)
            }
        }
    }

    func configure(
        attributedText: NSAttributedString,
        copyMarkdown: String?,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        copyPayload = copyMarkdown
        lastTheme = theme
        let insets = NSEdgeInsets(top: 2, left: 4, bottom: 2, right: 4)
        let textWidth = max(1, width - insets.left - insets.right)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: insets.left, y: insets.top, width: textWidth, height: textHeight)

        effectiveAppearance.performAsCurrentDrawingAppearance {
            copyButton.applyTheme(
                foreground: theme.secondaryForeground,
                background: theme.toolBackground)
        }
        let btnSize = CGSize(width: max(54, copyButton.frame.width + 10), height: 20)
        copyButton.frame = CGRect(
            x: max(insets.left, width - btnSize.width - 4),
            y: 2,
            width: btnSize.width,
            height: btnSize.height)
        if copyPayload == nil {
            copyButton.isHidden = true
        }
        return textHeight + insets.top + insets.bottom
    }

    @objc private func copyMarkdownPressed() {
        guard let copyPayload, !copyPayload.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(copyPayload, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Code Block Row (Header + Copy + Horizontal Scroll + Async Prism Highlighting)

final class PCodeCodeBlockView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .codeBlock
    let scrollView = PCodeHorizontalBlockScrollView()
    let textView = PCodeTextKit2BlockView()
    private let headerView = NSView()
    private let languageLabel = NSTextField(labelWithString: "")
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private let dividerLayer = CALayer()

    private var copyCode: String = ""
    private var highlightGeneration: UInt64 = 0
    private var highlightTask: Task<Void, Never>?
    private var lastTheme: PCodeTranscriptTheme?
    private var lastHighlightedCode: String?
    private var lastHighlightedLanguage: String?
    private var lastHighlightedAttr: NSAttributedString?
    private var lastHighlightedPaletteDark: Bool?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    static let headerHeight: CGFloat = 28

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        headerView.wantsLayer = true
        headerView.layer?.addSublayer(dividerLayer)
        languageLabel.isEditable = false
        languageLabel.isSelectable = false
        languageLabel.drawsBackground = false
        languageLabel.isBezeled = false
        languageLabel.lineBreakMode = .byTruncatingTail

        copyButton.target = self
        copyButton.action = #selector(copyCodePressed)

        headerView.addSubview(languageLabel)
        headerView.addSubview(copyButton)
        addSubview(headerView)

        scrollView.documentView = textView
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        highlightTask?.cancel()
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        highlightTask?.cancel()
        highlightTask = nil
        highlightGeneration &+= 1
        lastHighlightedCode = nil
        lastHighlightedLanguage = nil
        lastHighlightedAttr = nil
        lastHighlightedPaletteDark = nil
        scrollView.resetScrollOrigin()
        copyButton.cancelCopiedFlash()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        code: String,
        fullCopyCode: String,
        language: String?,
        showHeader: Bool,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        highlighter: any PCodeSyntaxHighlighter,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        copyCode = fullCopyCode
        lastTheme = theme
        highlightTask?.cancel()
        highlightGeneration &+= 1
        let gen = highlightGeneration

        let codeFont = typography.codeNSFont(scale: typography.codeBlockScale)
        let lineHeight = typography.lineHeight(scale: typography.codeBlockScale)
        let palette = PCodeSyntaxPalette.make(isDark: theme.isDark, foreground: theme.foreground)

        let initialAttr: NSAttributedString
        let needsAsyncHighlight: Bool
        let isStreamingExtension: Bool
        if let cached = highlighter.cachedTokens(code: code, language: language) {
            let styled = PCodePrismSyntaxHighlighter.attributedString(
                from: cached,
                font: codeFont,
                palette: palette,
                lineHeight: lineHeight)
            initialAttr = styled
            lastHighlightedCode = code
            lastHighlightedLanguage = language
            lastHighlightedAttr = styled
            lastHighlightedPaletteDark = theme.isDark
            needsAsyncHighlight = false
            isStreamingExtension = false
        } else if let prevCode = lastHighlightedCode,
                  let prevAttr = lastHighlightedAttr,
                  lastHighlightedLanguage == language,
                  lastHighlightedPaletteDark == theme.isDark,
                  code.hasPrefix(prevCode),
                  code.count > prevCode.count
        {
            // Preserve the already-highlighted prefix while streaming new lines/chars
            // so syntax colors don't flicker back to plain text on every 120Hz frame.
            let suffix = String(code.dropFirst(prevCode.count))
            let combined = NSMutableAttributedString(attributedString: prevAttr)
            combined.append(PCodePrismSyntaxHighlighter.plainAttributedString(
                code: suffix,
                font: codeFont,
                foreground: theme.foreground,
                lineHeight: lineHeight))
            initialAttr = combined
            needsAsyncHighlight = true
            isStreamingExtension = true
        } else {
            initialAttr = PCodePrismSyntaxHighlighter.plainAttributedString(
                code: code,
                font: codeFont,
                foreground: theme.foreground,
                lineHeight: lineHeight)
            needsAsyncHighlight = true
            isStreamingExtension = false
        }

        let horizontalPad: CGFloat = 12
        let verticalPad: CGFloat = 10
        let contentWidth = max(1, width - horizontalPad * 2)
        let maxLineWidth = Self.widestLineWidth(in: code, font: codeFont)
        let layoutWidth = max(contentWidth, ceil(maxLineWidth) + 12)
        let textHeight = textView.configure(attributedText: initialAttr, width: layoutWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 0, y: 0, width: layoutWidth, height: textHeight)

        let headerH: CGFloat = showHeader ? Self.headerHeight : 0
        headerView.isHidden = !showHeader
        if showHeader {
            headerView.frame = CGRect(x: 0, y: 0, width: width, height: headerH)
            languageLabel.stringValue = (language?.isEmpty == false) ? (language ?? "") : "code"
            languageLabel.font = typography.codeNSFont(scale: 0.78, weight: .medium)
            languageLabel.textColor = theme.secondaryForeground
            languageLabel.sizeToFit()
            languageLabel.frame = CGRect(
                x: horizontalPad,
                y: floor((headerH - languageLabel.frame.height) * 0.5),
                width: max(40, width - 90),
                height: languageLabel.frame.height)

            effectiveAppearance.performAsCurrentDrawingAppearance {
                copyButton.applyTheme(foreground: theme.secondaryForeground, background: .clear)
            }
            let btnW = max(54, copyButton.frame.width + 8)
            copyButton.frame = CGRect(
                x: max(horizontalPad, width - btnW - 8),
                y: floor((headerH - 20) * 0.5),
                width: btnW,
                height: 20)
            dividerLayer.frame = CGRect(x: 0, y: headerH - 1, width: width, height: 1)
        }

        scrollView.frame = CGRect(
            x: horizontalPad,
            y: headerH + verticalPad,
            width: contentWidth,
            height: textHeight)

        let totalHeight = headerH + textHeight + verticalPad * 2
        applyLayerColors()

        if needsAsyncHighlight {
            highlightTask = Task { [weak self] in
                if isStreamingExtension {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                    guard !Task.isCancelled else { return }
                }
                let tokens = await highlighter.tokenize(code: code, language: language)
                guard !Task.isCancelled else { return }
                await MainActor.run { [weak self] in
                    guard let self, self.highlightGeneration == gen else { return }
                    let styled = PCodePrismSyntaxHighlighter.attributedString(
                        from: tokens,
                        font: codeFont,
                        palette: palette,
                        lineHeight: lineHeight)
                    self.lastHighlightedCode = code
                    self.lastHighlightedLanguage = language
                    self.lastHighlightedAttr = styled
                    self.lastHighlightedPaletteDark = theme.isDark
                    _ = self.textView.configure(attributedText: styled, width: layoutWidth)
                    self.textView.needsDisplay = true
                }
            }
        }

        return totalHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.codeBackground.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
            dividerLayer.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.10).cgColor
        }
    }

    /// Measure the widest line accurately (ASCII fast path for pure printable ASCII without tabs,
    /// full `NSString.size(withAttributes:)` whenever tabs or non-ASCII/emoji/CJK are present).
    static func widestLineWidth(in code: String, font: NSFont) -> CGFloat {
        let advance = ("M" as NSString).size(withAttributes: [.font: font]).width
        let attrs: [NSAttributedString.Key: Any] = [.font: font]
        var maxWidth: CGFloat = 0

        for line in code.split(separator: "\n", omittingEmptySubsequences: false) {
            let hasComplex = line.unicodeScalars.contains { scalar in
                scalar.value < 0x20 || scalar.value > 0x7E
            }
            let w: CGFloat
            if hasComplex {
                w = (String(line) as NSString).size(withAttributes: attrs).width
            } else {
                w = CGFloat(line.count) * advance
            }
            if w > maxWidth { maxWidth = w }
        }
        return maxWidth
    }

    @objc private func copyCodePressed() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(copyCode, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Table Block Row (Horizontal Scroll + 1px Hairline Grid)

private final class PCodeTableGridView: NSView {
    struct CellLayout {
        let attributed: NSAttributedString
        let alignment: PCodeMarkdownTable.Alignment
    }

    var columnWidths: [CGFloat] = []
    var rowHeights: [CGFloat] = []
    var rows: [[CellLayout]] = []
    var hasHeader = false
    var headerFill: NSColor = .clear
    var gridColor: NSColor = .separatorColor
    var isSelected = false

    static let cellPadX: CGFloat = 10
    static let cellPadY: CGFloat = 7

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext, !rowHeights.isEmpty else { return }
        ctx.saveGState()
        defer { ctx.restoreGState() }

        let totalWidth = columnWidths.reduce(0, +)
        let totalHeight = rowHeights.reduce(0, +)

        if isSelected {
            ctx.setFillColor(NSColor.selectedTextBackgroundColor.withAlphaComponent(0.35).cgColor)
            ctx.fill(CGRect(x: 0, y: 0, width: totalWidth, height: totalHeight))
        }

        if hasHeader, let firstH = rowHeights.first {
            ctx.setFillColor(headerFill.cgColor)
            ctx.fill(CGRect(x: 0, y: 0, width: totalWidth, height: firstH))
        }

        var y: CGFloat = 0
        for (rowIdx, row) in rows.enumerated() {
            let h = rowHeights[rowIdx]
            var x: CGFloat = 0
            for (colIdx, width) in columnWidths.enumerated() {
                if colIdx < row.count {
                    let cell = row[colIdx]
                    let drawRect = CGRect(
                        x: x + Self.cellPadX,
                        y: y + Self.cellPadY,
                        width: max(1, width - Self.cellPadX * 2),
                        height: max(1, h - Self.cellPadY * 2))
                    if drawRect.intersects(dirtyRect) {
                        cell.attributed.draw(with: drawRect, options: [.usesLineFragmentOrigin, .usesFontLeading])
                    }
                }
                x += width
            }
            y += h
        }

        ctx.setFillColor(gridColor.cgColor)
        let px: CGFloat = 1.0 / (window?.backingScaleFactor ?? 2.0)

        var rowY: CGFloat = 0
        for (idx, h) in rowHeights.enumerated() {
            rowY += h
            if idx < rowHeights.count - 1 {
                ctx.fill(CGRect(x: 0, y: rowY - px, width: totalWidth, height: px))
            }
        }
        var colX: CGFloat = 0
        for (idx, w) in columnWidths.enumerated() {
            colX += w
            if idx < columnWidths.count - 1 {
                ctx.fill(CGRect(x: colX - px, y: 0, width: px, height: totalHeight))
            }
        }
    }
}

final class PCodeTableBlockView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .table
    let scrollView = PCodeHorizontalBlockScrollView()
    private let gridView = PCodeTableGridView()
    private var plainLength: Int = 0
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { nil }
    var selectableTextLength: Int { plainLength }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        scrollView.documentView = gridView
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        let selected = (range?.length ?? 0) > 0
        if gridView.isSelected != selected {
            gridView.isSelected = selected
            gridView.needsDisplay = true
        }
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        scrollView.resetScrollOrigin()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        table: PCodeMarkdownTable,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        lastTheme = theme
        plainLength = (table.plainText as NSString).length

        let colCount = max(table.headers.count, table.rows.map(\.count).max() ?? 0)
        guard colCount > 0 else { return 0 }

        var allRows: [[PCodeTableGridView.CellLayout]] = []
        let hasHeader = !table.headers.isEmpty
        if hasHeader {
            allRows.append(buildRow(
                table.headers,
                alignments: table.alignments,
                colCount: colCount,
                isHeader: true,
                typography: typography,
                theme: theme))
        }
        for row in table.rows {
            allRows.append(buildRow(
                row,
                alignments: table.alignments,
                colCount: colCount,
                isHeader: false,
                typography: typography,
                theme: theme))
        }

        var colWidths = Array(repeating: CGFloat(68), count: colCount)
        for row in allRows {
            for (c, cell) in row.enumerated() {
                let measured = cell.attributed.boundingRect(
                    with: CGSize(width: 320 - PCodeTableGridView.cellPadX * 2, height: .greatestFiniteMagnitude),
                    options: [.usesLineFragmentOrigin, .usesFontLeading]).width
                let w = min(320, max(68, ceil(measured) + PCodeTableGridView.cellPadX * 2 + 4))
                if w > colWidths[c] { colWidths[c] = w }
            }
        }

        let naturalTotal = colWidths.reduce(0, +)
        if naturalTotal < width && naturalTotal > 0 {
            let scale = width / naturalTotal
            colWidths = colWidths.map { floor($0 * scale) }
            if let last = colWidths.indices.last {
                colWidths[last] += width - colWidths.reduce(0, +)
            }
        }

        var rowHeights: [CGFloat] = []
        rowHeights.reserveCapacity(allRows.count)
        for row in allRows {
            var maxH: CGFloat = ceil(typography.lineHeight(scale: 0.92))
            for (c, cell) in row.enumerated() {
                let textW = max(1, colWidths[c] - PCodeTableGridView.cellPadX * 2)
                let h = cell.attributed.boundingRect(
                    with: CGSize(width: textW, height: .greatestFiniteMagnitude),
                    options: [.usesLineFragmentOrigin, .usesFontLeading]).height
                if h > maxH { maxH = ceil(h) }
            }
            rowHeights.append(maxH + PCodeTableGridView.cellPadY * 2)
        }

        let totalW = colWidths.reduce(0, +)
        let totalH = rowHeights.reduce(0, +)

        gridView.columnWidths = colWidths
        gridView.rowHeights = rowHeights
        gridView.rows = allRows
        gridView.hasHeader = hasHeader
        gridView.isSelected = (selectedRange?.length ?? 0) > 0
        gridView.frame = CGRect(x: 0, y: 0, width: totalW, height: totalH)

        applyLayerColors()
        gridView.needsDisplay = true
        scrollView.frame = CGRect(x: 0, y: 0, width: width, height: totalH)
        return totalH
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            gridView.headerFill = theme.toolBackground
            gridView.gridColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.14 : 0.16)
            layer?.backgroundColor = theme.background.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.14 : 0.16).cgColor
            layer?.masksToBounds = true
        }
    }

    private func buildRow(
        _ cells: [PCodeMarkdownTableCell],
        alignments: [PCodeMarkdownTable.Alignment],
        colCount: Int,
        isHeader: Bool,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> [PCodeTableGridView.CellLayout] {
        (0..<colCount).map { idx in
            let raw = idx < cells.count ? cells[idx].markdown : ""
            let align = idx < alignments.count ? alignments[idx] : .left
            let attr = NSMutableAttributedString(attributedString: PCodeTranscriptMarkdown.attributedString(
                forInlineCell: raw,
                typography: typography,
                theme: theme,
                scale: 0.92,
                weight: isHeader ? typography.weight.nsEmphasisWeight : nil))
            let style = NSMutableParagraphStyle()
            style.minimumLineHeight = typography.lineHeight(scale: 0.92)
            style.maximumLineHeight = typography.lineHeight(scale: 0.92)
            switch align {
            case .left: style.alignment = .left
            case .center: style.alignment = .center
            case .right: style.alignment = .right
            }
            if attr.length > 0 {
                attr.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: attr.length))
            }
            return PCodeTableGridView.CellLayout(attributed: attr, alignment: align)
        }
    }
}

// MARK: - Tool Card Row (Compact Pill + Optional Expandable Detail)

final class PCodeToolCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .tool
    let textView = PCodeTextKit2BlockView()
    private let iconView = NSImageView()
    private let toggleButton = NSButton()
    private let bottomCollapseButton = NSButton()
    private let bottomDivider = CALayer()
    private var onToggle: (() -> Void)?
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    private static let bottomCollapseBarHeight: CGFloat = 24

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        iconView.imageScaling = .scaleProportionallyDown
        addSubview(iconView)
        addSubview(textView)

        toggleButton.isBordered = false
        toggleButton.title = ""
        toggleButton.target = self
        toggleButton.action = #selector(togglePressed)
        addSubview(toggleButton)

        layer?.addSublayer(bottomDivider)
        bottomDivider.isHidden = true

        bottomCollapseButton.isBordered = false
        bottomCollapseButton.bezelStyle = .inline
        bottomCollapseButton.imagePosition = .imageLeading
        bottomCollapseButton.target = self
        bottomCollapseButton.action = #selector(togglePressed)
        bottomCollapseButton.isHidden = true
        addSubview(bottomCollapseButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        primaryTextView?.inkSweepFrontier = nil
        onToggle = nil
        bottomCollapseButton.isHidden = true
        bottomDivider.isHidden = true
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    /// Number of lines in the expandable payload (output if present, otherwise multiline summary).
    static func expandableLineCount(for call: PCodePeekToolCall) -> Int {
        func countLines(_ raw: String) -> Int {
            var s = raw[...]
            if s.last?.isNewline == true {
                s = s.dropLast()
            }
            guard !s.isEmpty else { return 0 }
            return s.split(separator: "\n", omittingEmptySubsequences: false).count
        }
        if !call.output.isEmpty {
            return countLines(call.output)
        }
        let summary = call.summaryText
        if summary.contains("\n") {
            return countLines(summary)
        }
        return 0
    }

    /// Single source of truth for rendered tool card text (shared by drawing and copy).
    static func attributedText(
        for call: PCodePeekToolCall,
        isExpanded: Bool,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let lineHeight = typography.lineHeight(scale: 0.84)
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = lineHeight
        style.maximumLineHeight = lineHeight
        style.lineBreakMode = isExpanded ? .byWordWrapping : .byTruncatingTail

        out.append(NSAttributedString(
            string: call.name,
            attributes: [
                .font: typography.codeNSFont(scale: 0.84, weight: typography.weight.nsEmphasisWeight),
                .foregroundColor: call.failed ? NSColor.systemRed : theme.secondaryForeground,
                .paragraphStyle: style,
            ]))

        let summary = call.summaryText
        if !summary.isEmpty {
            let displaySummary: String
            if isExpanded {
                displaySummary = "\n" + summary
            } else {
                let firstLine = summary.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? summary
                displaySummary = "  " + (firstLine.count > 120 ? String(firstLine.prefix(120)) + "…" : firstLine)
            }
            out.append(NSAttributedString(
                string: displaySummary,
                attributes: [
                    .font: typography.codeNSFont(scale: 0.84),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.85),
                    .paragraphStyle: style,
                ]))
        }

        if let elapsed = call.formattedElapsed {
            out.append(NSAttributedString(
                string: "  (\(elapsed))",
                attributes: [
                    .font: typography.codeNSFont(scale: 0.78),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.62),
                    .paragraphStyle: style,
                ]))
        }

        if !isExpanded {
            let lineCount = expandableLineCount(for: call)
            if lineCount > 0 {
                let label = lineCount == 1 ? "1 line" : "\(lineCount) lines"
                out.append(NSAttributedString(
                    string: "  · \(label)",
                    attributes: [
                        .font: typography.codeNSFont(scale: 0.78),
                        .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.62),
                        .paragraphStyle: style,
                    ]))
            }
        }

        // Lazy tool output rendering: only format/append output when expanded.
        if isExpanded && !call.output.isEmpty {
            out.append(NSAttributedString(
                string: "\n" + call.output,
                attributes: [
                    .font: typography.codeNSFont(scale: 0.80),
                    .foregroundColor: theme.foreground.withAlphaComponent(0.82),
                    .paragraphStyle: style,
                ]))
        }
        return out
    }

    func configure(
        call: PCodePeekToolCall,
        isExpanded: Bool,
        attributedText: NSAttributedString,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?,
        onToggle: @escaping () -> Void
    ) -> CGFloat {
        self.onToggle = onToggle
        self.lastTheme = theme
        let summary = call.summaryText
        let canExpand = summary.count > 90 || summary.contains("\n") || !call.output.isEmpty

        let cfg = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
        let symbolName = call.failed ? "xmark.circle.fill" : "checkmark.circle.fill"
        iconView.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: call.failed ? "Failed" : "Succeeded")?
            .withSymbolConfiguration(cfg)
        iconView.contentTintColor = call.failed ? .systemRed : theme.secondaryForeground.withAlphaComponent(0.8)

        let leftPad: CGFloat = 26
        let rightPad: CGFloat = canExpand ? 26 : 10
        let topPad: CGFloat = 5
        let bottomPad: CGFloat = 5
        let textWidth = max(1, width - leftPad - rightPad)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: leftPad, y: topPad, width: textWidth, height: textHeight)

        let showBottomCollapse = canExpand && isExpanded
        let contentHeight = max(26, textHeight + topPad + bottomPad)
        let totalHeight = contentHeight + (showBottomCollapse ? Self.bottomCollapseBarHeight : 0)
        iconView.frame = CGRect(x: 9, y: floor((min(contentHeight, 26) - 12) * 0.5), width: 12, height: 12)

        toggleButton.isHidden = !canExpand
        if canExpand {
            let chevron = isExpanded ? "chevron.down" : "chevron.right"
            toggleButton.image = NSImage(systemSymbolName: chevron, accessibilityDescription: isExpanded ? "Collapse" : "Expand")?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
            toggleButton.contentTintColor = theme.secondaryForeground
            toggleButton.frame = CGRect(x: width - 22, y: 3, width: 18, height: 20)
        }

        bottomCollapseButton.isHidden = !showBottomCollapse
        bottomDivider.isHidden = !showBottomCollapse
        if showBottomCollapse {
            let lineCount = Self.expandableLineCount(for: call)
            let title = lineCount > 1 ? " Collapse (\(lineCount) lines)" : " Collapse"
            let symCfg = NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold)
            bottomCollapseButton.image = NSImage(systemSymbolName: "chevron.up", accessibilityDescription: "Collapse")?
                .withSymbolConfiguration(symCfg)
            bottomCollapseButton.contentTintColor = theme.secondaryForeground
            bottomCollapseButton.attributedTitle = NSAttributedString(
                string: title,
                attributes: [
                    .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                    .foregroundColor: theme.secondaryForeground,
                ])
            bottomCollapseButton.frame = CGRect(
                x: 8,
                y: contentHeight + 1,
                width: max(1, width - 16),
                height: Self.bottomCollapseBarHeight - 2)
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            bottomDivider.frame = CGRect(x: 8, y: contentHeight, width: max(0, width - 16), height: 1)
            CATransaction.commit()
        }

        applyLayerColors()
        return totalHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.toolBackground.cgColor
            layer?.cornerRadius = 5
            bottomDivider.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
        }
    }

    @objc private func togglePressed() {
        onToggle?()
    }
}

// MARK: - Status Badge Row

final class PCodeStatusRowView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .status
    let textView = PCodeTextKit2BlockView()

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    /// Single source of truth for rendered status text (shared by drawing and copy).
    static func displayLabel(for status: PCodePeekTurn.Status) -> String {
        switch status {
        case .running: return "In progress"
        case .cancelled: return "Cancelled"
        case .failed: return "Failed"
        case .complete: return "Complete"
        }
    }

    static func attributedText(
        for status: PCodePeekTurn.Status,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let color: NSColor
        switch status {
        case .running: color = theme.accent
        case .cancelled: color = .systemOrange
        case .failed: color = .systemRed
        case .complete: color = theme.secondaryForeground
        }
        return NSAttributedString(
            string: displayLabel(for: status),
            attributes: [
                .font: typography.proseNSFont(scale: 0.80, weight: .medium),
                .foregroundColor: color,
            ])
    }

    func configure(
        attributedText: NSAttributedString,
        width: CGFloat,
        selectedRange: NSRange?
    ) -> CGFloat {
        let textW = min(width, ceil(attributedText.size().width) + 2)
        let textH = textView.configure(attributedText: attributedText, width: textW)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 8, y: 3, width: textW, height: textH)
        return textH + 6
    }
}

// MARK: - Diff Card Row (`EditPreview` / `EditCompleted`)

final class PCodeDiffCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .diff
    let scrollView = PCodeHorizontalBlockScrollView()
    let textView = PCodeTextKit2BlockView()
    private let headerView = NSView()
    private let pathLabel = NSTextField(labelWithString: "")
    private let statsLabel = NSTextField(labelWithString: "")
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private let dividerLayer = CALayer()

    private var patchToCopy: String = ""
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    static let headerHeight: CGFloat = 28

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        headerView.wantsLayer = true
        headerView.layer?.addSublayer(dividerLayer)
        for label in [pathLabel, statsLabel] {
            label.isEditable = false
            label.isSelectable = false
            label.drawsBackground = false
            label.isBezeled = false
            label.lineBreakMode = .byTruncatingMiddle
        }
        copyButton.target = self
        copyButton.action = #selector(copyPatchPressed)

        headerView.addSubview(pathLabel)
        headerView.addSubview(statsLabel)
        headerView.addSubview(copyButton)
        addSubview(headerView)

        scrollView.documentView = textView
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        scrollView.resetScrollOrigin()
        copyButton.cancelCopiedFlash()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    static func attributedPatch(
        for payload: PCodeDiffPayload,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let font = typography.codeNSFont(scale: typography.codeBlockScale)
        let lh = typography.lineHeight(scale: typography.codeBlockScale)
        let para = NSMutableParagraphStyle()
        para.minimumLineHeight = lh
        para.maximumLineHeight = lh

        let addBg = NSColor.systemGreen.withAlphaComponent(theme.isDark ? 0.16 : 0.12)
        let delBg = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.16 : 0.12)
        let hunkColor = theme.accent.withAlphaComponent(0.85)
        let addFg = theme.isDark ? NSColor(calibratedRed: 0.56, green: 0.90, blue: 0.62, alpha: 1) : NSColor(calibratedRed: 0.08, green: 0.45, blue: 0.18, alpha: 1)
        let delFg = theme.isDark ? NSColor(calibratedRed: 0.98, green: 0.56, blue: 0.56, alpha: 1) : NSColor(calibratedRed: 0.65, green: 0.12, blue: 0.14, alpha: 1)

        let out = NSMutableAttributedString()
        let rawLines = payload.patch.split(separator: "\n", omittingEmptySubsequences: false)
        let capped = rawLines.prefix(200)
        for (idx, lineSub) in capped.enumerated() {
            let line = String(lineSub) + (idx < capped.count - 1 ? "\n" : "")
            var attrs: [NSAttributedString.Key: Any] = [
                .font: font,
                .foregroundColor: theme.foreground,
                .paragraphStyle: para,
            ]
            if lineSub.hasPrefix("+") && !lineSub.hasPrefix("+++") {
                attrs[.foregroundColor] = addFg
                attrs[.backgroundColor] = addBg
            } else if lineSub.hasPrefix("-") && !lineSub.hasPrefix("---") {
                attrs[.foregroundColor] = delFg
                attrs[.backgroundColor] = delBg
            } else if lineSub.hasPrefix("@@") {
                attrs[.foregroundColor] = hunkColor
            }
            out.append(NSAttributedString(string: line, attributes: attrs))
        }
        if rawLines.count > 200 {
            out.append(NSAttributedString(
                string: "\n… (\(rawLines.count - 200) more diff lines)",
                attributes: [.font: font, .foregroundColor: theme.secondaryForeground, .paragraphStyle: para]))
        }
        return out
    }

    func configure(
        payload: PCodeDiffPayload,
        attributedPatch: NSAttributedString,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        patchToCopy = payload.patch
        lastTheme = theme

        let padX: CGFloat = 12
        let padY: CGFloat = 8
        let contentWidth = max(1, width - padX * 2)
        let codeFont = typography.codeNSFont(scale: typography.codeBlockScale)
        // Measure only the capped display text (<= 200 lines) rather than scanning huge full patches.
        let widest = PCodeCodeBlockView.widestLineWidth(in: attributedPatch.string, font: codeFont)
        let layoutWidth = max(contentWidth, ceil(widest) + 12)
        let textHeight = attributedPatch.length > 0
            ? textView.configure(attributedText: attributedPatch, width: layoutWidth)
            : 0
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 0, y: 0, width: layoutWidth, height: textHeight)

        headerView.frame = CGRect(x: 0, y: 0, width: width, height: Self.headerHeight)
        pathLabel.stringValue = payload.isPreview ? "\(payload.path) (preview)" : payload.path
        pathLabel.font = typography.codeNSFont(scale: 0.80, weight: .medium)
        pathLabel.textColor = theme.foreground
        pathLabel.sizeToFit()

        let statsAttr = NSMutableAttributedString()
        if payload.added > 0 {
            statsAttr.append(NSAttributedString(
                string: "+\(payload.added)",
                attributes: [.font: typography.codeNSFont(scale: 0.76, weight: .semibold), .foregroundColor: NSColor.systemGreen]))
        }
        if payload.removed > 0 {
            if statsAttr.length > 0 { statsAttr.append(NSAttributedString(string: " ")) }
            statsAttr.append(NSAttributedString(
                string: "−\(payload.removed)",
                attributes: [.font: typography.codeNSFont(scale: 0.76, weight: .semibold), .foregroundColor: NSColor.systemRed]))
        }
        statsLabel.attributedStringValue = statsAttr
        statsLabel.sizeToFit()

        effectiveAppearance.performAsCurrentDrawingAppearance {
            copyButton.applyTheme(foreground: theme.secondaryForeground, background: .clear)
        }
        let btnW = max(54, copyButton.frame.width + 8)
        let statsW = statsLabel.frame.width
        let pathMaxW = max(40, width - padX - btnW - statsW - 24)
        pathLabel.frame = CGRect(
            x: padX,
            y: floor((Self.headerHeight - pathLabel.frame.height) * 0.5),
            width: min(pathLabel.frame.width, pathMaxW),
            height: pathLabel.frame.height)
        statsLabel.frame = CGRect(
            x: pathLabel.frame.maxX + 8,
            y: floor((Self.headerHeight - statsLabel.frame.height) * 0.5),
            width: statsW,
            height: statsLabel.frame.height)
        copyButton.frame = CGRect(
            x: max(padX, width - btnW - 8),
            y: floor((Self.headerHeight - 20) * 0.5),
            width: btnW,
            height: 20)
        dividerLayer.frame = CGRect(x: 0, y: Self.headerHeight - 1, width: width, height: 1)

        let bodyVisible = textHeight > 0
        scrollView.isHidden = !bodyVisible
        scrollView.frame = CGRect(x: padX, y: Self.headerHeight + padY, width: contentWidth, height: textHeight)
        applyLayerColors()
        return bodyVisible ? (Self.headerHeight + textHeight + padY * 2) : Self.headerHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.codeBackground.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
            dividerLayer.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.10).cgColor
        }
    }

    @objc private func copyPatchPressed() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(patchToCopy, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Thinking Card Row (Collapsed-by-Default Reasoning Preview)

final class PCodeThinkingCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .thinking
    let textView = PCodeTextKit2BlockView()
    private let toggleButton = NSButton()
    private var onToggle: (() -> Void)?
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
        toggleButton.isBordered = false
        toggleButton.title = ""
        toggleButton.target = self
        toggleButton.action = #selector(togglePressed)
        addSubview(toggleButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    static func attributedText(
        for text: String,
        isExpanded: Bool,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let lh = typography.lineHeight(scale: 0.86)
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = lh
        style.maximumLineHeight = lh

        let out = NSMutableAttributedString()
        out.append(NSAttributedString(
            string: "Thinking",
            attributes: [
                .font: typography.proseNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight, italic: true),
                .foregroundColor: theme.secondaryForeground,
                .paragraphStyle: style,
            ]))

        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            let body: String
            if isExpanded {
                body = "\n" + trimmed
            } else {
                let lines = trimmed.split(separator: "\n", omittingEmptySubsequences: true)
                let preview = lines.suffix(2).joined(separator: " ")
                body = "  " + (preview.count > 140 ? String(preview.prefix(140)) + "…" : preview)
            }
            out.append(NSAttributedString(
                string: body,
                attributes: [
                    .font: typography.proseNSFont(scale: 0.86, italic: true),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.78),
                    .paragraphStyle: style,
                ]))
        }
        return out
    }

    func configure(
        isExpanded: Bool,
        attributedText: NSAttributedString,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?,
        onToggle: @escaping () -> Void
    ) -> CGFloat {
        self.onToggle = onToggle
        self.lastTheme = theme
        let padX: CGFloat = 12
        let padY: CGFloat = 6
        let textWidth = max(1, width - padX - 28)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: padX, y: padY, width: textWidth, height: textHeight)

        let chevron = isExpanded ? "chevron.down" : "chevron.right"
        toggleButton.image = NSImage(systemSymbolName: chevron, accessibilityDescription: isExpanded ? "Collapse" : "Expand")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
        toggleButton.contentTintColor = theme.secondaryForeground
        toggleButton.frame = CGRect(x: width - 22, y: 5, width: 18, height: 20)

        applyLayerColors()
        return max(28, textHeight + padY * 2)
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.toolBackground.cgColor
            layer?.cornerRadius = 6
        }
    }

    @objc private func togglePressed() {
        onToggle?()
    }
}

// MARK: - Plan Card Row (`PlanUpdated` / `PlanPreview`)

final class PCodePlanCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .plan
    let textView = PCodeTextKit2BlockView()
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    static func attributedText(
        for items: [PCodePlanItem],
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let lh = typography.lineHeight(scale: 0.88)
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = lh
        style.maximumLineHeight = lh
        style.paragraphSpacing = 3

        let out = NSMutableAttributedString()
        out.append(NSAttributedString(
            string: "Plan\n",
            attributes: [
                .font: typography.proseNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight),
                .foregroundColor: theme.accent,
                .paragraphStyle: style,
            ]))

        for (idx, item) in items.enumerated() {
            let (icon, color): (String, NSColor)
            switch item.status.lowercased() {
            case "completed", "done": (icon, color) = ("●", .systemGreen)
            case "in_progress", "running": (icon, color) = ("◐", theme.accent)
            case "failed", "error": (icon, color) = ("×", .systemRed)
            default: (icon, color) = ("○", theme.secondaryForeground)
            }
            out.append(NSAttributedString(
                string: "\(icon) ",
                attributes: [.font: typography.codeNSFont(scale: 0.86), .foregroundColor: color, .paragraphStyle: style]))
            let suffix = idx < items.count - 1 ? "\n" : ""
            let isDone = ["completed", "done"].contains(item.status.lowercased())
            out.append(NSAttributedString(
                string: item.content + suffix,
                attributes: [
                    .font: typography.proseNSFont(scale: 0.88),
                    .foregroundColor: isDone ? theme.secondaryForeground : theme.foreground,
                    .paragraphStyle: style,
                ]))
        }
        return out
    }

    func configure(
        attributedText: NSAttributedString,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        lastTheme = theme
        let padX: CGFloat = 12
        let padY: CGFloat = 8
        let textWidth = max(1, width - padX * 2)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: padX, y: padY, width: textWidth, height: textHeight)
        applyLayerColors()
        return textHeight + padY * 2
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.promptBackground.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
        }
    }
}
