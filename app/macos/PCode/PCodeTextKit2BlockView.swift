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

        // 1. Thematic break (horizontal rule)
        if let ruleColor = paragraph.attribute(.pcodeThematicBreak, at: 0, effectiveRange: nil) as? NSColor {
            let y = origin.y + layoutFragmentFrame.height * 0.5
            let rect = CGRect(x: origin.x, y: y, width: layoutFragmentFrame.width, height: 1)
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
        for line in textLineFragments {
            let lineRange = line.characterRange
            guard lineRange.length > 0, lineRange.location + lineRange.length <= paragraph.length else { continue }
            paragraph.enumerateAttribute(.pcodeInlineCodeFill, in: lineRange, options: []) { value, runRange, _ in
                guard let fill = value as? NSColor, runRange.length > 0 else { return }
                let startInLine = runRange.location - lineRange.location
                let endInLine = startInLine + runRange.length
                let startPt = line.locationForCharacter(at: startInLine)
                let endPt = line.locationForCharacter(at: endInLine)
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
    @discardableResult
    func configure(attributedText: NSAttributedString, width: CGFloat) -> CGFloat {
        let clampedWidth = max(1, width)
        let textChanged = lastAttributedString.map { !$0.isEqual(to: attributedText) } ?? true
        let widthChanged = abs(lastLayoutWidth - clampedWidth) > 0.25

        if textChanged {
            textContentStorage.performEditingTransaction {
                textContentStorage.attributedString = attributedText
            }
            lastAttributedString = attributedText
        }
        if widthChanged || textChanged {
            textContainer.size = CGSize(width: clampedWidth, height: 0)
            lastLayoutWidth = clampedWidth
            textLayoutManager.ensureLayout(for: textLayoutManager.documentRange)
            needsDisplay = true
        }
        return textLayoutManager.usageBoundsForTextContainer.height
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

        // 2. Draw visible TextKit 2 layout fragments.
        textLayoutManager.ensureLayout(for: dirtyRect)
        let startLocation = textLayoutManager.textLayoutFragment(for: CGPoint(x: 0, y: max(0, dirtyRect.minY)))?.rangeInElement.location
            ?? textLayoutManager.documentRange.location
        textLayoutManager.enumerateTextLayoutFragments(
            from: startLocation,
            options: [.ensuresLayout]
        ) { fragment in
            let frame = fragment.layoutFragmentFrame
            if frame.minY > dirtyRect.maxY {
                return false
            }
            if frame.maxY >= dirtyRect.minY {
                fragment.draw(at: frame.origin, in: context)
            }
            return true
        }
    }

    /// Map a point in this view's flipped coordinate space to a UTF-16 offset in the text.
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

        for line in fragment.textLineFragments {
            let lineBounds = line.typographicBounds
            if localY <= lineBounds.maxY || line === fragment.textLineFragments.last {
                let idx = line.characterIndex(for: CGPoint(x: localX - lineBounds.origin.x, y: localY - lineBounds.origin.y))
                return min(max(0, baseOffset + idx), totalLength)
            }
        }
        return min(max(0, baseOffset), totalLength)
    }

    /// Return an `http`/`https` link URL at `point`, if any.
    func linkURL(at point: CGPoint) -> URL? {
        guard let attr = textContentStorage.attributedString, attr.length > 0 else { return nil }
        let offset = utf16Offset(at: point)
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
        guard let url, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return nil
        }
        return url
    }

    private func textRange(fromUTF16 range: NSRange) -> NSTextRange? {
        let docStart = textLayoutManager.documentRange.location
        guard let start = textContentStorage.location(docStart, offsetBy: range.location),
              let end = textContentStorage.location(start, offsetBy: range.length)
        else { return nil }
        return NSTextRange(location: start, end: end)
    }
}
