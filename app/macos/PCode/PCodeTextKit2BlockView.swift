import AppKit
import CoreGraphics

extension NSAttributedString.Key {
    /// Background pill color drawn behind inline code spans by `PCodeTextLayoutFragment`.
    static let pcodeInlineCodeFill = NSAttributedString.Key("PCodeInlineCodeFill")
    /// Nesting depth (`Int`) for blockquote left accent bars drawn by `PCodeTextLayoutFragment`.
    static let pcodeBlockquoteDepth = NSAttributedString.Key("PCodeBlockquoteDepth")
    /// Horizontal rule color drawn by `PCodeTextLayoutFragment`.
    static let pcodeThematicBreak = NSAttributedString.Key("PCodeThematicBreak")
}

/// Custom TextKit 2 layout fragment that draws inline-code pills, continuous blockquote
/// left bars, and thematic breaks in CoreGraphics without text attachments.
final class PCodeTextLayoutFragment: NSTextLayoutFragment {
    override var renderingSurfaceBounds: CGRect {
        // Expand slightly horizontally for inline-code pill padding and blockquote bars.
        super.renderingSurfaceBounds.insetBy(dx: -6, dy: -2)
    }

    override func draw(at point: CGPoint, in context: CGContext) {
        if let paragraph = (textElement as? NSTextParagraph)?.attributedString, paragraph.length > 0 {
            drawDecorations(for: paragraph, at: point, in: context)
        }
        super.draw(at: point, in: context)
    }

    private func drawDecorations(for paragraph: NSAttributedString, at origin: CGPoint, in context: CGContext) {
        context.saveGState()
        defer { context.restoreGState() }

        // 1. Thematic break (horizontal rule spanning full column width)
        if let ruleColor = paragraph.attribute(.pcodeThematicBreak, at: 0, effectiveRange: nil) as? NSColor {
            let y = origin.y + layoutFragmentFrame.height * 0.5
            let width = textLayoutManager?.textContainer?.size.width ?? layoutFragmentFrame.width
            let rect = CGRect(x: origin.x, y: y, width: width, height: 1)
            context.setFillColor(ruleColor.cgColor)
            context.fill(rect)
            return
        }

        // 2. Blockquote vertical left bars (continuous across line spacing)
        if let depth = paragraph.attribute(.pcodeBlockquoteDepth, at: 0, effectiveRange: nil) as? Int, depth > 0 {
            let barColor = NSColor.labelColor.withAlphaComponent(0.20)
            context.setFillColor(barColor.cgColor)
            for level in 0..<depth {
                let x = origin.x + CGFloat(level) * 14
                let rect = CGRect(x: x, y: origin.y, width: 3, height: layoutFragmentFrame.height)
                let path = CGPath(roundedRect: rect, cornerWidth: 1.5, cornerHeight: 1.5, transform: nil)
                context.addPath(path)
                context.fillPath()
            }
        }

        // 3. Inline code rounded background pills
        // `NSTextLineFragment.locationForCharacter(at:)` expects paragraph-relative indices.
        for line in textLineFragments {
            let lineRange = line.characterRange
            guard lineRange.length > 0, lineRange.location + lineRange.length <= paragraph.length else { continue }
            paragraph.enumerateAttribute(.pcodeInlineCodeFill, in: lineRange, options: []) { value, runRange, _ in
                guard let fill = value as? NSColor, runRange.length > 0 else { return }
                let startPt = line.locationForCharacter(at: runRange.location)
                let endPt = line.locationForCharacter(at: runRange.location + runRange.length)
                let minX = min(startPt.x, endPt.x)
                let maxX = max(startPt.x, endPt.x)
                guard maxX > minX else { return }

                let bounds = line.typographicBounds
                let pillRect = CGRect(
                    x: origin.x + bounds.origin.x + minX - 3,
                    y: origin.y + bounds.origin.y + 1,
                    width: (maxX - minX) + 6,
                    height: max(1, bounds.height - 2))
                let path = CGPath(roundedRect: pillRect, cornerWidth: 4, cornerHeight: 4, transform: nil)
                context.setFillColor(fill.cgColor)
                context.addPath(path)
                context.fillPath()
            }
        }
    }
}

/// Bare TextKit 2 (`NSTextLayoutManager` + `NSTextContentStorage`) hosted in a plain `NSView`.
///
/// Avoids `NSTextView`'s private mouse-tracking loop, first-responder theft from the composer,
/// and unwanted `scrollRangeToVisible` calls, while giving full control over model-owned
/// cross-item selection, exact measurement, and custom fragment drawing.
final class PCodeTextKit2BlockView: NSView, NSTextLayoutManagerDelegate {
    let textContentStorage = NSTextContentStorage()
    let textLayoutManager = NSTextLayoutManager()
    let textContainer = NSTextContainer()

    private var lastAttributedString: NSAttributedString?
    private var lastLayoutWidth: CGFloat = 0
    private var lastMeasuredHeight: CGFloat = 0
    private(set) var totalInkSweepLength: CGFloat = 0
    /// Bounding rect of the final laid-out line fragment in this view's flipped coordinates.
    private(set) var lastLineFrame: CGRect = .zero

    /// Active ink-sweep frontier in linear horizontal points across all lines (`nil` = fully revealed).
    var inkSweepFrontier: CGFloat? {
        didSet {
            guard inkSweepFrontier != oldValue else { return }
            if let dirty = inkSweepDirtyRect(from: oldValue, to: inkSweepFrontier) {
                setNeedsDisplay(dirty)
            } else {
                needsDisplay = true
            }
        }
    }

    /// Width of the soft alpha feather at the leading edge of the ink-sweep reveal (~6 chars).
    var inkSweepFeatherWidth: CGFloat = 48

    /// Active selection range in this item's local UTF-16 coordinates, owned by the transcript model.
    var selectedRange: NSRange? {
        didSet {
            if selectedRange != oldValue {
                needsDisplay = true
            }
        }
    }

    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        textContainer.lineFragmentPadding = 0
        textContainer.widthTracksTextView = false
        textContainer.heightTracksTextView = false
        textLayoutManager.textContainer = textContainer
        textLayoutManager.delegate = self
        textContentStorage.addTextLayoutManager(textLayoutManager)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func textLayoutManager(
        _ textLayoutManager: NSTextLayoutManager,
        textLayoutFragmentFor location: any NSTextLocation,
        in textElement: NSTextElement
    ) -> NSTextLayoutFragment {
        PCodeTextLayoutFragment(textElement: textElement, range: textElement.elementRange)
    }

    /// Update the rendered attributed text and layout width, returning exact measured height.
    /// When only a trailing suffix changed (e.g. live streaming), applies a minimal suffix edit
    /// to `textContentStorage.textStorage` so TextKit 2 preserves earlier layout fragments.
    @discardableResult
    func configure(attributedText: NSAttributedString, width: CGFloat) -> CGFloat {
        let clampedWidth = max(1, width)
        let textChanged = lastAttributedString.map { !$0.isEqual(to: attributedText) } ?? true
        let widthChanged = abs(lastLayoutWidth - clampedWidth) > 0.25

        if textChanged {
            let immutableCopy = (attributedText.copy() as? NSAttributedString) ?? attributedText
            let storage: NSTextStorage
            if let existing = textContentStorage.textStorage {
                storage = existing
            } else {
                let created = NSTextStorage()
                textContentStorage.textStorage = created
                storage = created
            }

            if let oldAttr = lastAttributedString, storage.length == oldAttr.length {
                let prefixLen = Self.commonAttributedPrefixLength(oldAttr, immutableCopy)
                if prefixLen > 0 {
                    let replaceRange = NSRange(location: prefixLen, length: oldAttr.length - prefixLen)
                    let suffix = immutableCopy.attributedSubstring(
                        from: NSRange(location: prefixLen, length: immutableCopy.length - prefixLen))
                    textContentStorage.performEditingTransaction {
                        storage.replaceCharacters(in: replaceRange, with: suffix)
                    }
                } else {
                    textContentStorage.performEditingTransaction {
                        storage.setAttributedString(immutableCopy)
                    }
                }
            } else {
                textContentStorage.performEditingTransaction {
                    storage.setAttributedString(immutableCopy)
                }
            }
            lastAttributedString = immutableCopy
        }
        if widthChanged || textChanged {
            textContainer.size = CGSize(width: clampedWidth, height: 0)
            lastLayoutWidth = clampedWidth
            textLayoutManager.ensureLayout(for: textLayoutManager.documentRange)
            let metrics = computeLayoutMetrics()
            totalInkSweepLength = metrics.inkSweepLength
            lastMeasuredHeight = metrics.height
            lastLineFrame = metrics.lastLineFrame
            needsDisplay = true
        }
        return lastMeasuredHeight
    }

    private func computeLayoutMetrics() -> (height: CGFloat, inkSweepLength: CGFloat, lastLineFrame: CGRect) {
        if (textContentStorage.textStorage?.length ?? 0) == 0 {
            return (0, 0, .zero)
        }
        var totalSpan: CGFloat = 0
        var maxY: CGFloat = 0
        var finalLine: CGRect = .zero
        textLayoutManager.enumerateTextLayoutFragments(
            from: textLayoutManager.documentRange.location,
            options: [.ensuresLayout]
        ) { fragment in
            let frame = fragment.layoutFragmentFrame
            if frame.maxY > maxY {
                maxY = frame.maxY
            }
            for line in fragment.textLineFragments {
                totalSpan += max(1, ceil(line.typographicBounds.width))
                let b = line.typographicBounds
                finalLine = CGRect(
                    x: frame.origin.x + b.origin.x,
                    y: frame.origin.y + b.origin.y,
                    width: b.width,
                    height: b.height)
            }
            return true
        }
        let height = maxY > 0 ? maxY : textLayoutManager.usageBoundsForTextContainer.height
        return (height, totalSpan, finalLine)
    }

    private func inkSweepDirtyRect(from oldFrontier: CGFloat?, to newFrontier: CGFloat?) -> CGRect? {
        guard let oldF = oldFrontier, let newF = newFrontier else { return nil }
        let lo = min(oldF, newF) - inkSweepFeatherWidth - 4
        let hi = max(oldF, newF) + 4
        var cursor: CGFloat = 0
        var union: CGRect?
        textLayoutManager.enumerateTextLayoutFragments(
            from: textLayoutManager.documentRange.location,
            options: []
        ) { fragment in
            let fragOrigin = fragment.layoutFragmentFrame.origin
            for line in fragment.textLineFragments {
                let lineW = max(1, ceil(line.typographicBounds.width))
                let lineStart = cursor
                let lineEnd = cursor + lineW
                cursor = lineEnd
                if lineEnd >= lo && lineStart <= hi {
                    let b = line.typographicBounds
                    let rect = CGRect(
                        x: fragOrigin.x + b.origin.x - 8,
                        y: fragOrigin.y + b.origin.y - 4,
                        width: b.width + 16,
                        height: b.height + 8)
                    union = union.map { $0.union(rect) } ?? rect
                }
                if lineStart > hi { return false }
            }
            return true
        }
        return union
    }

    /// Compute the longest UTF-16 prefix length where both text and attributes match identically,
    /// backing up across any split UTF-16 surrogate pair or composed character sequence.
    static func commonAttributedPrefixLength(_ lhs: NSAttributedString, _ rhs: NSAttributedString) -> Int {
        let lhsStr = lhs.string as NSString
        let rhsStr = rhs.string as NSString
        let textPrefixLen = (lhsStr.commonPrefix(with: rhs.string, options: .literal) as NSString).length
        guard textPrefixLen > 0 else { return 0 }

        var matched = 0
        while matched < textPrefixLen {
            var lhsRange = NSRange()
            var rhsRange = NSRange()
            let lhsAttrs = lhs.attributes(at: matched, longestEffectiveRange: &lhsRange, in: NSRange(location: matched, length: textPrefixLen - matched))
            let rhsAttrs = rhs.attributes(at: matched, longestEffectiveRange: &rhsRange, in: NSRange(location: matched, length: textPrefixLen - matched))
            guard NSDictionary(dictionary: lhsAttrs).isEqual(to: rhsAttrs) else {
                break
            }
            let step = min(NSMaxRange(lhsRange), NSMaxRange(rhsRange))
            guard step > matched else { break }
            matched = step
        }

        // Never split a UTF-16 surrogate pair or extended grapheme cluster in either string.
        while matched > 0 {
            var backedUp = false
            if matched < lhsStr.length {
                let r = lhsStr.rangeOfComposedCharacterSequence(at: matched)
                if r.location < matched {
                    matched = r.location
                    backedUp = true
                }
            }
            if matched > 0 && matched < rhsStr.length {
                let r = rhsStr.rangeOfComposedCharacterSequence(at: matched)
                if r.location < matched {
                    matched = r.location
                    backedUp = true
                }
            }
            if !backedUp { break }
        }
        return matched
    }

    /// Measure exact height using the same TextKit 2 engine that draws the view.
    func measureHeight(for attributedText: NSAttributedString, width: CGFloat) -> CGFloat {
        configure(attributedText: attributedText, width: width)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        context.saveGState()
        defer { context.restoreGState() }

        // 1. Draw model-owned selection rects behind text fragments.
        if let range = selectedRange, range.length > 0,
           let textRange = textRange(fromUTF16: range)
        {
            context.setFillColor(NSColor.selectedTextBackgroundColor.cgColor)
            textLayoutManager.enumerateTextSegments(
                in: textRange,
                type: .selection,
                options: []
            ) { _, segmentFrame, _, _ in
                if segmentFrame.intersects(dirtyRect) {
                    context.fill(segmentFrame)
                }
                return true
            }
        }

        // 2. Draw visible TextKit 2 layout fragments (include 4pt margin for pill/bar overhangs).
        let expandedDirty = dirtyRect.insetBy(dx: 0, dy: -4)
        textLayoutManager.ensureLayout(for: expandedDirty)

        if let frontier = inkSweepFrontier {
            drawInkSweepFragments(frontier: frontier, in: expandedDirty, context: context)
            return
        }

        let startLocation = textLayoutManager.textLayoutFragment(for: CGPoint(x: 0, y: max(0, expandedDirty.minY)))?.rangeInElement.location
            ?? textLayoutManager.documentRange.location
        textLayoutManager.enumerateTextLayoutFragments(
            from: startLocation,
            options: [.ensuresLayout]
        ) { fragment in
            let frame = fragment.layoutFragmentFrame
            if frame.minY > expandedDirty.maxY {
                return false
            }
            if frame.maxY >= expandedDirty.minY {
                fragment.draw(at: frame.origin, in: context)
            }
            return true
        }
    }

    /// Draw settled layout fragments with a soft left-to-right ink-sweep reveal frontier.
    /// Lines before the frontier draw normally; lines after are skipped; the single frontier
    /// line is masked with a soft horizontal feather.
    private func drawInkSweepFragments(frontier: CGFloat, in expandedDirty: CGRect, context: CGContext) {
        let feather = max(8, inkSweepFeatherWidth)
        var cursor: CGFloat = 0

        textLayoutManager.enumerateTextLayoutFragments(
            from: textLayoutManager.documentRange.location,
            options: [.ensuresLayout]
        ) { fragment in
            let fragFrame = fragment.layoutFragmentFrame
            let lines = fragment.textLineFragments
            guard !lines.isEmpty else { return true }

            var fragSpan: CGFloat = 0
            for line in lines {
                fragSpan += max(1, ceil(line.typographicBounds.width))
            }
            let fragStart = cursor
            let fragEnd = cursor + fragSpan

            if fragStart >= frontier {
                // Entire fragment is beyond the reveal frontier; stop walking.
                return false
            }

            if fragEnd + feather <= frontier {
                // Entire fragment is completely revealed.
                cursor = fragEnd
                if fragFrame.maxY >= expandedDirty.minY && fragFrame.minY <= expandedDirty.maxY {
                    fragment.draw(at: fragFrame.origin, in: context)
                }
                return true
            }

            // Partial fragment: draw revealed lines whole, and soft-feather the single frontier line.
            for line in lines {
                let lineW = max(1, ceil(line.typographicBounds.width))
                let lineStart = cursor
                let lineEnd = cursor + lineW
                cursor = lineEnd

                if lineStart >= frontier {
                    return false
                }

                let lb = line.typographicBounds
                // Extend left edge to `fragFrame.origin.x` so blockquote left accent bars are
                // never clipped out during ink-sweep, and keep exact vertical bounds (no ±2pt
                // overlap into adjacent lines).
                let leftX = min(fragFrame.origin.x, fragFrame.origin.x + lb.origin.x - 6)
                let rightX = fragFrame.origin.x + lb.origin.x + lb.width + 6
                let lineRect = CGRect(
                    x: leftX,
                    y: fragFrame.origin.y + lb.origin.y,
                    width: max(1, rightX - leftX),
                    height: lb.height)

                guard lineRect.intersects(expandedDirty) else { continue }

                if lineEnd + feather <= frontier {
                    context.saveGState()
                    context.clip(to: lineRect)
                    fragment.draw(at: fragFrame.origin, in: context)
                    context.restoreGState()
                } else {
                    let revealX = fragFrame.origin.x + lb.origin.x + (frontier - lineStart)
                    let clipRight = min(lineRect.maxX, revealX)
                    guard clipRight > lineRect.minX else { continue }

                    let clippedRect = CGRect(
                        x: lineRect.minX,
                        y: lineRect.minY,
                        width: clipRight - lineRect.minX,
                        height: lineRect.height)

                    context.saveGState()
                    context.clip(to: clippedRect)
                    context.beginTransparencyLayer(in: clippedRect, auxiliaryInfo: nil)
                    fragment.draw(at: fragFrame.origin, in: context)

                    // Soft right-edge alpha dissolve via `.destinationIn` gradient.
                    let gradStartX = max(lineRect.minX, revealX - feather)
                    let gradEndX = revealX
                    if gradEndX > gradStartX,
                       let space = CGColorSpace(name: CGColorSpace.sRGB),
                       let gradient = CGGradient(
                           colorsSpace: space,
                           colors: [
                               NSColor.white.withAlphaComponent(1.0).cgColor,
                               NSColor.white.withAlphaComponent(0.0).cgColor,
                           ] as CFArray,
                           locations: [0.0, 1.0])
                    {
                        context.setBlendMode(.destinationIn)
                        context.drawLinearGradient(
                            gradient,
                            start: CGPoint(x: gradStartX, y: clippedRect.midY),
                            end: CGPoint(x: gradEndX, y: clippedRect.midY),
                            options: [.drawsBeforeStartLocation])
                    }
                    context.endTransparencyLayer()
                    context.restoreGState()
                }
            }
            return true
        }
    }

    /// Map a point in this view's flipped coordinate space to a UTF-16 caret offset in the text.
    func utf16Offset(at point: CGPoint) -> Int {
        let totalLength = (textContentStorage.attributedString?.length) ?? 0
        guard totalLength > 0 else { return 0 }

        let clampedPoint = CGPoint(
            x: max(0, point.x),
            y: max(0, min(point.y, max(0, bounds.height - 0.5))))
        textLayoutManager.ensureLayout(for: textLayoutManager.documentRange)

        guard let fragment = textLayoutManager.textLayoutFragment(for: clampedPoint) else {
            return point.y <= 0 ? 0 : totalLength
        }

        let fragOrigin = fragment.layoutFragmentFrame.origin
        let localY = clampedPoint.y - fragOrigin.y
        let localX = clampedPoint.x - fragOrigin.x

        let baseOffset = textContentStorage.offset(
            from: textLayoutManager.documentRange.location,
            to: fragment.rangeInElement.location)

        let fullNSString = (textContentStorage.attributedString?.string ?? "") as NSString

        for line in fragment.textLineFragments {
            let lineBounds = line.typographicBounds
            if localY <= lineBounds.maxY || line === fragment.textLineFragments.last {
                let ptInLine = CGPoint(x: localX - lineBounds.origin.x, y: localY - lineBounds.origin.y)
                let lineRange = line.characterRange
                let rawIdx = line.characterIndex(for: ptInLine)

                // `characterIndex(for:)` returns `NSNotFound` (`Int.max`) when `ptInLine.x`
                // falls past the line's glyphs. Clamp safely to the line boundaries.
                if rawIdx == NSNotFound || !NSLocationInRange(rawIdx, lineRange) {
                    if ptInLine.x <= 0 {
                        return min(max(0, baseOffset + lineRange.location), totalLength)
                    }
                    var endOffset = min(baseOffset + NSMaxRange(lineRange), totalLength)
                    if endOffset > baseOffset + lineRange.location && endOffset > 0 {
                        let lastChar = fullNSString.character(at: endOffset - 1)
                        if lastChar == 0x0A || lastChar == 0x2028 {
                            endOffset -= 1
                        }
                    }
                    return min(max(0, endOffset), totalLength)
                }

                var offset = min(max(0, baseOffset + rawIdx), totalLength)
                let fraction = line.fractionOfDistanceThroughGlyph(for: ptInLine)
                if fraction >= 0.5, offset < totalLength {
                    let composed = fullNSString.rangeOfComposedCharacterSequence(at: offset)
                    offset = min(NSMaxRange(composed), totalLength)
                }
                return offset
            }
        }
        return min(max(0, baseOffset), totalLength)
    }

    /// Return an allowed link URL (`http`, `https`, or local `file`) strictly under `point`,
    /// ignoring margins and trailing whitespace.
    func linkURL(at point: CGPoint) -> URL? {
        guard bounds.contains(point),
              let attr = textContentStorage.attributedString,
              attr.length > 0
        else { return nil }

        textLayoutManager.ensureLayout(for: textLayoutManager.documentRange)
        guard let fragment = textLayoutManager.textLayoutFragment(for: point),
              fragment.layoutFragmentFrame.contains(point)
        else { return nil }

        let fragOrigin = fragment.layoutFragmentFrame.origin
        let localPt = CGPoint(x: point.x - fragOrigin.x, y: point.y - fragOrigin.y)
        let baseOffset = textContentStorage.offset(
            from: textLayoutManager.documentRange.location,
            to: fragment.rangeInElement.location)

        for line in fragment.textLineFragments {
            let lineBounds = line.typographicBounds
            guard lineBounds.contains(localPt) else { continue }
            let ptInLine = CGPoint(x: localPt.x - lineBounds.origin.x, y: localPt.y - lineBounds.origin.y)
            let idx = line.characterIndex(for: ptInLine)
            guard NSLocationInRange(idx, line.characterRange) else { return nil }
            let offset = baseOffset + idx
            guard offset >= 0, offset < attr.length else { return nil }
            let raw = attr.attribute(.link, at: offset, effectiveRange: nil)
            let url: URL?
            if let u = raw as? URL {
                url = u
            } else if let s = raw as? String {
                url = URL(string: s)
            } else {
                url = nil
            }
            guard let url, PCodeTranscriptMarkdown.isAllowedLinkURL(url) else {
                return nil
            }
            return url
        }
        return nil
    }

    /// Bounding rect (in this view's coordinates) of a UTF-16 range, useful for testing and menus.
    func boundingRect(forUTF16 range: NSRange) -> CGRect? {
        guard range.length > 0, let textRange = textRange(fromUTF16: range) else { return nil }
        textLayoutManager.ensureLayout(for: textLayoutManager.documentRange)
        var union: CGRect?
        textLayoutManager.enumerateTextSegments(in: textRange, type: .standard, options: []) { _, rect, _, _ in
            union = union.map { $0.union(rect) } ?? rect
            return true
        }
        return union
    }

    func textRange(fromUTF16 range: NSRange) -> NSTextRange? {
        let docStart = textLayoutManager.documentRange.location
        guard let start = textContentStorage.location(docStart, offsetBy: range.location),
              let end = textContentStorage.location(start, offsetBy: range.length)
        else { return nil }
        return NSTextRange(location: start, end: end)
    }
}
