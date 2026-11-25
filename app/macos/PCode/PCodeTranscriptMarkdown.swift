import AppKit
import Foundation
import Markdown

/// Structured GFM table extracted from Markdown AST.
struct PCodeMarkdownTable: Equatable {
    enum Alignment: Equatable {
        case left
        case center
        case right
    }

    let headers: [String]
    let alignments: [Alignment]
    let rows: [[String]]

    /// Plain-text tab-separated representation for cross-item copy.
    var plainText: String {
        var lines: [String] = []
        lines.reserveCapacity(rows.count + 1)
        if !headers.isEmpty {
            lines.append(headers.joined(separator: "\t"))
        }
        for row in rows {
            lines.append(row.joined(separator: "\t"))
        }
        return lines.joined(separator: "\n")
    }
}

/// Top-level block split from a single Markdown message for virtualized rendering.
enum PCodeMarkdownBlock: Equatable {
    /// One or more contiguous prose elements (paragraphs, headings, lists, blockquotes, rules)
    /// rendered together in a single TextKit 2 block view so selection flows naturally across them.
    case prose(markdown: String)
    /// Fenced or indented code block (chunked to <= `maxCodeLinesPerItem` lines).
    case codeBlock(language: String?, code: String, chunkIndex: Int, totalChunks: Int)
    /// GitHub Flavored Markdown table.
    case table(PCodeMarkdownTable)
}

/// Markdown parser and TextKit 2 `NSAttributedString` builder powered by `apple/swift-markdown`.
///
/// Security & layout invariants:
/// - Block directives are disabled (`options: []`).
/// - Raw HTML blocks and inline HTML are rendered as literal text.
/// - Only `http` and `https` URLs receive `.link` attributes.
/// - Fixed line heights keep mixed sans + mono inline code aligned on one baseline.
enum PCodeTranscriptMarkdown {
    static let maxCodeLinesPerItem = 200

    /// Split a Markdown string into top-level virtualized blocks (coalescing adjacent prose).
    static func parseBlocks(_ markdown: String) -> [PCodeMarkdownBlock] {
        guard !markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return []
        }

        let document = Document(parsing: markdown, options: [])
        let lines = markdown.split(separator: "\n", omittingEmptySubsequences: false)
        var blocks: [PCodeMarkdownBlock] = []
        var pendingProseParts: [String] = []

        func flushProse() {
            guard !pendingProseParts.isEmpty else { return }
            let combined = pendingProseParts.joined(separator: "\n\n")
            pendingProseParts.removeAll(keepingCapacity: true)
            if !combined.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                blocks.append(.prose(markdown: combined))
            }
        }

        for child in document.children {
            if let codeBlock = child as? CodeBlock {
                flushProse()
                let rawCode = codeBlock.code.hasSuffix("\n")
                    ? String(codeBlock.code.dropLast())
                    : codeBlock.code
                let lang = codeBlock.language?.trimmingCharacters(in: .whitespacesAndNewlines)
                let normalizedLang = (lang?.isEmpty == false) ? lang : nil
                let chunks = chunkLines(rawCode, maxLines: maxCodeLinesPerItem)
                for (idx, chunk) in chunks.enumerated() {
                    blocks.append(.codeBlock(
                        language: normalizedLang,
                        code: chunk,
                        chunkIndex: idx,
                        totalChunks: chunks.count))
                }
            } else if let table = child as? Table {
                flushProse()
                blocks.append(.table(extractTable(table)))
            } else {
                if let slice = sourceSlice(for: child, in: lines) {
                    pendingProseParts.append(slice)
                } else {
                    pendingProseParts.append(child.format())
                }
            }
        }
        flushProse()
        return blocks
    }

    /// Render a prose Markdown slice into a rich `NSAttributedString` for `PCodeTextKit2BlockView`.
    static func attributedString(
        forProse markdown: String,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let document = Document(parsing: markdown, options: [])
        var renderer = ProseAttributedRenderer(typography: typography, theme: theme)
        renderer.visit(document)
        // Trim any single trailing newline added between blocks.
        let result = renderer.result
        while result.length > 0 && (result.string as NSString).character(at: result.length - 1) == 0x0A {
            result.deleteCharacters(in: NSRange(location: result.length - 1, length: 1))
        }
        return result
    }

    /// Render inline Markdown (used for table cells).
    static func attributedString(
        forInline markdown: String,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        scale: CGFloat = 0.92,
        weight: NSFont.Weight? = nil
    ) -> NSAttributedString {
        let document = Document(parsing: markdown, options: [])
        var renderer = ProseAttributedRenderer(
            typography: typography,
            theme: theme,
            baseScale: scale,
            baseWeightOverride: weight)
        renderer.visit(document)
        let result = renderer.result
        while result.length > 0 && (result.string as NSString).character(at: result.length - 1) == 0x0A {
            result.deleteCharacters(in: NSRange(location: result.length - 1, length: 1))
        }
        return result
    }

    static func chunkLines(_ text: String, maxLines: Int) -> [String] {
        guard maxLines > 0 else { return [text] }
        let allLines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard allLines.count > maxLines else { return [text] }
        var chunks: [String] = []
        var i = 0
        while i < allLines.count {
            let end = min(allLines.count, i + maxLines)
            chunks.append(allLines[i..<end].joined(separator: "\n"))
            i = end
        }
        return chunks
    }

    private static func sourceSlice(for markup: any Markup, in lines: [Substring]) -> String? {
        guard let range = markup.range else { return nil }
        let startLine = range.lowerBound.line - 1
        let endLine = range.upperBound.line - 1
        guard startLine >= 0, endLine >= startLine, endLine < lines.count else { return nil }
        return lines[startLine...endLine].joined(separator: "\n")
    }

    private static func extractTable(_ table: Table) -> PCodeMarkdownTable {
        let alignments: [PCodeMarkdownTable.Alignment] = table.columnAlignments.map { align in
            switch align {
            case .center: return .center
            case .right: return .right
            case .left, .none: return .left
            }
        }
        let headers: [String] = Array(table.head.cells).map { cell in
            inlineSource(for: cell)
        }
        let rows: [[String]] = Array(table.body.rows).map { row in
            Array(row.cells).map { cell in
                inlineSource(for: cell)
            }
        }
        return PCodeMarkdownTable(headers: headers, alignments: alignments, rows: rows)
    }

    private static func inlineSource(for cell: Table.Cell) -> String {
        cell.children.map { $0.format() }.joined().trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// AST walker converting `swift-markdown` prose nodes into a TextKit 2 `NSAttributedString`.
private struct ProseAttributedRenderer: MarkupWalker {
    private static let headingScales: [CGFloat] = [1.42, 1.26, 1.14, 1.04, 0.96, 0.90]

    let typography: PCodeTypographySnapshot
    let theme: PCodeTranscriptTheme
    let baseScale: CGFloat
    let baseWeightOverride: NSFont.Weight?

    let result = NSMutableAttributedString()

    private var currentScale: CGFloat
    private var boldDepth = 0
    private var italicDepth = 0
    private var strikethroughDepth = 0
    private var blockquoteDepth = 0
    private var activeLinkURL: URL?
    private var listStack: [ListState] = []
    private var pendingListPrefix: String?
    private var inHeading = false
    private var headingLevel = 0

    private struct ListState {
        let ordered: Bool
        var nextIndex: Int
    }

    init(
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        baseScale: CGFloat = 1.0,
        baseWeightOverride: NSFont.Weight? = nil
    ) {
        self.typography = typography
        self.theme = theme
        self.baseScale = baseScale
        self.baseWeightOverride = baseWeightOverride
        self.currentScale = baseScale
    }

    private var effectiveWeight: NSFont.Weight {
        if boldDepth > 0 || inHeading {
            return typography.weight.nsEmphasisWeight
        }
        return baseWeightOverride ?? typography.weight.nsFontWeight
    }

    private var effectiveForeground: NSColor {
        if activeLinkURL != nil {
            return theme.accent
        }
        if inHeading && headingLevel >= 4 {
            return theme.secondaryForeground
        }
        if blockquoteDepth > 0 {
            return theme.secondaryForeground
        }
        return theme.foreground
    }

    private func paragraphStyle(
        scale: CGFloat,
        spacingBefore: CGFloat = 0,
        spacingAfter: CGFloat = 6,
        isListItem: Bool = false
    ) -> NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        let lh = typography.lineHeight(scale: scale)
        style.minimumLineHeight = lh
        style.maximumLineHeight = lh
        style.paragraphSpacingBefore = spacingBefore
        style.paragraphSpacing = spacingAfter

        var leftIndent: CGFloat = 0
        if blockquoteDepth > 0 {
            leftIndent += CGFloat(blockquoteDepth) * 14 + 4
        }
        if !listStack.isEmpty {
            let depth = CGFloat(listStack.count)
            let markerWidth: CGFloat = 20
            let baseIndent = leftIndent + (depth - 1) * 18
            if isListItem {
                style.firstLineHeadIndent = baseIndent
                style.headIndent = baseIndent + markerWidth
                style.tabStops = [NSTextTab(textAlignment: .left, location: baseIndent + markerWidth)]
            } else {
                style.firstLineHeadIndent = baseIndent + markerWidth
                style.headIndent = baseIndent + markerWidth
            }
        } else {
            style.firstLineHeadIndent = leftIndent
            style.headIndent = leftIndent
        }
        return style
    }

    private func ensureNewlineBeforeBlock() {
        if result.length > 0 && (result.string as NSString).character(at: result.length - 1) != 0x0A {
            result.append(NSAttributedString(string: "\n"))
        }
    }

    private func applyBlockAttributes(to range: NSRange, style: NSParagraphStyle) {
        guard range.length > 0 else { return }
        result.addAttribute(.paragraphStyle, value: style, range: range)
        if blockquoteDepth > 0 {
            result.addAttribute(.pcodeBlockquoteDepth, value: blockquoteDepth, range: range)
        }
    }

    mutating func visitHeading(_ heading: Heading) {
        ensureNewlineBeforeBlock()
        let level = min(max(heading.level, 1), 6)
        let savedScale = currentScale
        inHeading = true
        headingLevel = level
        currentScale = Self.headingScales[level - 1] * baseScale

        let start = result.length
        descendInto(heading)
        result.append(NSAttributedString(string: "\n"))
        let range = NSRange(location: start, length: result.length - start)
        let spacingBefore: CGFloat = start == 0 ? 0 : (level <= 2 ? 12 : 8)
        let spacingAfter: CGFloat = level <= 2 ? 6 : 4
        applyBlockAttributes(
            to: range,
            style: paragraphStyle(scale: currentScale, spacingBefore: spacingBefore, spacingAfter: spacingAfter))

        inHeading = false
        headingLevel = 0
        currentScale = savedScale
    }

    mutating func visitParagraph(_ paragraph: Paragraph) {
        ensureNewlineBeforeBlock()
        let start = result.length
        let hasPrefix = pendingListPrefix != nil
        if let prefix = pendingListPrefix {
            pendingListPrefix = nil
            let markerAttrs: [NSAttributedString.Key: Any] = [
                .font: typography.proseNSFont(scale: currentScale, weight: typography.weight.nsFontWeight),
                .foregroundColor: theme.secondaryForeground,
            ]
            result.append(NSAttributedString(string: prefix + "\t", attributes: markerAttrs))
        }

        descendInto(paragraph)
        result.append(NSAttributedString(string: "\n"))
        let range = NSRange(location: start, length: result.length - start)
        let spacingAfter: CGFloat = listStack.isEmpty ? 7 : 3
        applyBlockAttributes(
            to: range,
            style: paragraphStyle(scale: currentScale, spacingBefore: 0, spacingAfter: spacingAfter, isListItem: hasPrefix))
    }

    mutating func visitBlockQuote(_ blockQuote: BlockQuote) {
        blockquoteDepth += 1
        descendInto(blockQuote)
        blockquoteDepth -= 1
    }

    mutating func visitUnorderedList(_ unorderedList: UnorderedList) {
        listStack.append(ListState(ordered: false, nextIndex: 1))
        descendInto(unorderedList)
        listStack.removeLast()
    }

    mutating func visitOrderedList(_ orderedList: OrderedList) {
        listStack.append(ListState(ordered: true, nextIndex: Int(orderedList.startIndex)))
        descendInto(orderedList)
        listStack.removeLast()
    }

    mutating func visitListItem(_ listItem: ListItem) {
        let depth = listStack.count
        let prefix: String
        if let checkbox = listItem.checkbox {
            prefix = checkbox == .checked ? "☑" : "☐"
        } else if let top = listStack.last, top.ordered {
            prefix = "\(top.nextIndex)."
            listStack[listStack.count - 1].nextIndex += 1
        } else {
            let bullets = ["•", "◦", "▪"]
            prefix = bullets[max(0, depth - 1) % bullets.count]
        }
        pendingListPrefix = prefix
        descendInto(listItem)
        // If a list item contained no paragraph (e.g. empty item), emit marker anyway.
        if let leftover = pendingListPrefix {
            pendingListPrefix = nil
            ensureNewlineBeforeBlock()
            let start = result.length
            result.append(NSAttributedString(
                string: leftover + "\n",
                attributes: [
                    .font: typography.proseNSFont(scale: currentScale),
                    .foregroundColor: theme.secondaryForeground,
                ]))
            applyBlockAttributes(
                to: NSRange(location: start, length: result.length - start),
                style: paragraphStyle(scale: currentScale, spacingAfter: 3, isListItem: true))
        }
    }

    mutating func visitThematicBreak(_ thematicBreak: ThematicBreak) {
        ensureNewlineBeforeBlock()
        let start = result.length
        let attrs: [NSAttributedString.Key: Any] = [
            .font: typography.proseNSFont(scale: 0.6),
            .foregroundColor: NSColor.clear,
            .pcodeThematicBreak: theme.foreground.withAlphaComponent(0.18),
        ]
        result.append(NSAttributedString(string: "\u{00A0}\n", attributes: attrs))
        let range = NSRange(location: start, length: result.length - start)
        applyBlockAttributes(
            to: range,
            style: paragraphStyle(scale: 0.6, spacingBefore: 6, spacingAfter: 6))
    }

    mutating func visitHTMLBlock(_ html: HTMLBlock) {
        // Security invariant: raw HTML is always rendered as literal text.
        ensureNewlineBeforeBlock()
        let start = result.length
        let literal = html.rawHTML.trimmingCharacters(in: .newlines)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: typography.codeNSFont(scale: currentScale * typography.inlineCodeScale),
            .foregroundColor: theme.secondaryForeground,
        ]
        result.append(NSAttributedString(string: literal + "\n", attributes: attrs))
        applyBlockAttributes(
            to: NSRange(location: start, length: result.length - start),
            style: paragraphStyle(scale: currentScale, spacingAfter: 6))
    }

    // MARK: - Inline Nodes

    mutating func visitText(_ text: Text) {
        appendInlineText(text.string)
    }

    mutating func visitSoftBreak(_ softBreak: SoftBreak) {
        appendInlineText(" ")
    }

    mutating func visitLineBreak(_ lineBreak: LineBreak) {
        appendInlineText("\n")
    }

    mutating func visitStrong(_ strong: Strong) {
        boldDepth += 1
        descendInto(strong)
        boldDepth -= 1
    }

    mutating func visitEmphasis(_ emphasis: Emphasis) {
        italicDepth += 1
        descendInto(emphasis)
        italicDepth -= 1
    }

    mutating func visitStrikethrough(_ strikethrough: Strikethrough) {
        strikethroughDepth += 1
        descendInto(strikethrough)
        strikethroughDepth -= 1
    }

    mutating func visitInlineCode(_ inlineCode: InlineCode) {
        let codeFont = typography.codeNSFont(
            scale: currentScale * typography.inlineCodeScale,
            weight: effectiveWeight,
            italic: italicDepth > 0)
        var attrs: [NSAttributedString.Key: Any] = [
            .font: codeFont,
            .foregroundColor: effectiveForeground,
            .pcodeInlineCodeFill: theme.inlineCodeFill,
        ]
        if strikethroughDepth > 0 {
            attrs[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
        }
        if let url = activeLinkURL {
            attrs[.link] = url
            attrs[.underlineStyle] = NSUnderlineStyle.single.rawValue
        }
        result.append(NSAttributedString(string: inlineCode.code, attributes: attrs))
    }

    mutating func visitInlineHTML(_ inlineHTML: InlineHTML) {
        // Security invariant: raw HTML is rendered as literal text.
        appendInlineText(inlineHTML.rawHTML)
    }

    mutating func visitLink(_ link: Link) {
        let previous = activeLinkURL
        if let dest = link.destination,
           let url = URL(string: dest),
           let scheme = url.scheme?.lowercased(),
           scheme == "http" || scheme == "https"
        {
            activeLinkURL = url
        } else {
            activeLinkURL = nil
        }
        descendInto(link)
        activeLinkURL = previous
    }

    mutating func visitImage(_ image: Image) {
        let alt = image.plainText
        appendInlineText(alt.isEmpty ? "[image]" : "[\(alt)]")
    }

    private mutating func appendInlineText(_ string: String) {
        guard !string.isEmpty else { return }
        let font = typography.proseNSFont(
            scale: currentScale,
            weight: effectiveWeight,
            italic: italicDepth > 0)
        var attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: effectiveForeground,
        ]
        if strikethroughDepth > 0 {
            attrs[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
        }
        if let url = activeLinkURL {
            attrs[.link] = url
            attrs[.underlineStyle] = NSUnderlineStyle.single.rawValue
        }
        result.append(NSAttributedString(string: string, attributes: attrs))
    }
}
