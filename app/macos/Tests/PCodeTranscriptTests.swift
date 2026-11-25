import AppKit
import Foundation
import Testing
@testable import Ghostty

struct PCodeTranscriptTests {
    // MARK: - Typography Snapshot (AppKit NSFont)

    @Test func typographySnapshotResolvesWeightsAndMapsHeavyToExtraBold() {
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .regular, italic: false) == "JetBrainsMono-Regular")
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .medium, italic: false) == "JetBrainsMono-Medium")
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .semibold, italic: false) == "JetBrainsMono-SemiBold")
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .bold, italic: false) == "JetBrainsMono-Bold")
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .heavy, italic: false) == "JetBrainsMono-ExtraBold")
        #expect(PCodeTypographySnapshot.jetBrainsPostScriptName(for: .heavy, italic: true) == "JetBrainsMono-ExtraBoldItalic")
    }

    @Test func typographySnapshotLineHeightScalesCleanly() {
        let snap = PCodeTypographySnapshot(style: .sansAndMono, size: 14, weight: .medium, codeFamilyAvailable: false)
        #expect(snap.lineHeight() == 20)
        #expect(snap.proseNSFont().pointSize == 14)
        #expect(abs(snap.codeNSFont(scale: 0.92).pointSize - (14 * 0.92)) < 0.01)
    }

    // MARK: - Layout Engine (Virtualization, Pixel Rounding, Upward-Fling Anchoring)

    @Test func layoutEnginePrefixSumsAndBinarySearchVisibleRange() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 10, bottomPadding: 10, backingScale: 2)
        engine.reset(items: [
            (id: "a", estimatedHeight: 40.1, spacingAfter: 8), // rounds to 40.5
            (id: "b", estimatedHeight: 60.0, spacingAfter: 8),
            (id: "c", estimatedHeight: 80.0, spacingAfter: 8),
        ])

        #expect(engine.entries[0].yOffset == 10)
        #expect(engine.entries[0].height == 40.5)
        #expect(engine.entries[1].yOffset == 58.5)
        #expect(engine.entries[2].yOffset == 126.5)

        // Visible band hitting only item 1 ("b": 58.5...118.5)
        let r1 = engine.visibleRange(in: CGRect(x: 0, y: 60, width: 400, height: 50))
        #expect(r1 == 1..<2)

        // Visible band spanning items 0 and 1
        let r2 = engine.visibleRange(in: CGRect(x: 0, y: 20, width: 400, height: 60))
        #expect(r2 == 0..<2)
    }

    @Test func layoutEngineAnchorsScrollWhenItemAboveViewportResizes() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 0, bottomPadding: 0, backingScale: 2)
        engine.reset(items: [
            (id: "0", estimatedHeight: 50, spacingAfter: 10),
            (id: "1", estimatedHeight: 50, spacingAfter: 10),
            (id: "2", estimatedHeight: 50, spacingAfter: 10),
        ])

        // Viewport top is at y=120 (item 2). Item 0 (0...50) grows by +30pt.
        let result = engine.updateHeight(at: 0, measuredHeight: 80, viewportTop: 120, isPinnedToBottom: false)
        #expect(result.didGrow)
        #expect(result.scrollOriginDelta == 30)
        #expect(engine.entries[2].yOffset == 150)

        // When pinned to bottom, scrollOriginDelta is 0 (bottom pin takes over).
        let pinnedResult = engine.updateHeight(at: 0, measuredHeight: 100, viewportTop: 150, isPinnedToBottom: true)
        #expect(pinnedResult.scrollOriginDelta == 0)
    }

    @Test func layoutEngineTailRatchetPreventsStreamingShrinkBounce() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 0, bottomPadding: 0, backingScale: 2)
        let idx = engine.append(id: "tail", height: 90, spacingAfter: 8)
        engine.setTailRatchet(at: idx, active: true)

        // Attempting to shrink while streaming keeps height at 90.
        _ = engine.updateHeight(at: idx, measuredHeight: 72, viewportTop: 0, isPinnedToBottom: true)
        #expect(engine.entries[idx].height == 90)

        // Growing updates the ratchet to 110.
        _ = engine.updateHeight(at: idx, measuredHeight: 110, viewportTop: 0, isPinnedToBottom: true)
        #expect(engine.entries[idx].height == 110)

        // Releasing the ratchet allows final settled height.
        engine.setTailRatchet(at: idx, active: false)
        _ = engine.updateHeight(at: idx, measuredHeight: 84, viewportTop: 0, isPinnedToBottom: true)
        #expect(engine.entries[idx].height == 84)
    }

    @Test func layoutEngineHandlesLargeTranscriptScale() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 16, bottomPadding: 16, backingScale: 2)
        // Mirror the real 45 MB transcript scale (~9,000 items).
        let items = (0..<9_000).map { i in
            (id: "item-\(i)", estimatedHeight: CGFloat(28 + (i % 5) * 14), spacingAfter: CGFloat(6))
        }
        engine.reset(items: items)
        #expect(engine.entries.count == 9_000)

        let midY = engine.totalHeight * 0.5
        let visible = engine.visibleRange(in: CGRect(x: 0, y: midY, width: 800, height: 700))
        #expect(!visible.isEmpty)
        #expect(visible.count < 30)
    }

    // MARK: - Sticky-Bottom State Machine

    @Test func stickyBottomUnpinsOnUpwardScrollAndYieldsDuringLiveScroll() {
        var state = PCodeStickyBottomState()
        #expect(state.shouldProgrammaticallyPin)

        state.didStartLiveScroll()
        // Never programmatically write origin while a live scroll/momentum gesture is active.
        #expect(!state.shouldProgrammaticallyPin)

        state.didLiveScroll(distanceFromBottom: 40, movedUpward: true)
        #expect(state.mode == .userScrolledUp)

        state.didEndLiveScroll(distanceFromBottom: 40)
        #expect(state.mode == .userScrolledUp)
        #expect(!state.shouldProgrammaticallyPin)

        // Scrolling back within threshold re-pins on end.
        state.didStartLiveScroll()
        state.didEndLiveScroll(distanceFromBottom: 4)
        #expect(state.mode == .pinnedToBottom)
        #expect(state.shouldProgrammaticallyPin)
    }

    @Test func stickyBottomHandlesLegacyWheelAndUserActions() {
        var state = PCodeStickyBottomState()
        state.didLegacyWheelScroll(distanceFromBottom: 24, movedUpward: true)
        #expect(state.mode == .userScrolledUp)

        state.didLegacyWheelScroll(distanceFromBottom: 2, movedUpward: false)
        #expect(state.mode == .pinnedToBottom)

        state.unpinForUserAction()
        #expect(state.mode == .userScrolledUp)

        state.pinToBottom()
        #expect(state.mode == .pinnedToBottom)
    }

    @Test func stickyBottomOriginAccountsForContentInsets() {
        let bottomY = PCodeStickyBottomState.bottomOriginY(
            documentHeight: 2000,
            clipHeight: 600,
            topInset: 20,
            bottomInset: 40)
        #expect(bottomY == 1440)
        let dist = PCodeStickyBottomState.distanceFromBottom(
            clipOriginY: 1400,
            documentHeight: 2000,
            clipHeight: 600,
            topInset: 20,
            bottomInset: 40)
        #expect(dist == 40)
    }

    // MARK: - Model-Owned Cross-Item Selection

    @Test func crossItemSelectionExtractsNormalizedText() {
        let texts = ["First block text", "Second code block", "Third summary"]
        // Reversed drag (from item 2 offset 5 up to item 0 offset 6)
        let sel = PCodeTranscriptSelection(
            anchor: .init(itemIndex: 2, utf16Offset: 5),
            focus: .init(itemIndex: 0, utf16Offset: 6))

        #expect(sel.localRange(forItem: 0, utf16Length: texts[0].utf16.count) == NSRange(location: 6, length: 10))
        #expect(sel.localRange(forItem: 1, utf16Length: texts[1].utf16.count) == NSRange(location: 0, length: 17))
        #expect(sel.localRange(forItem: 2, utf16Length: texts[2].utf16.count) == NSRange(location: 0, length: 5))

        let extracted = sel.extractText(itemCount: texts.count) { texts[$0] }
        #expect(extracted == "block text\n\nSecond code block\n\nThird")
    }

    // MARK: - Bare TextKit 2 Block View

    @Test @MainActor func textKit2BlockMeasuresHeightAndFiltersLinks() {
        let view = PCodeTextKit2BlockView(frame: CGRect(x: 0, y: 0, width: 420, height: 100))
        let httpsRange = NSRange(location: 6, length: 19)
        let fileRange = NSRange(location: 29, length: 18)
        let attr = NSMutableAttributedString(string: "Click https://example.com or file:///etc/passwd")
        attr.addAttribute(.font, value: NSFont.systemFont(ofSize: 14), range: NSRange(location: 0, length: attr.length))
        attr.addAttribute(.link, value: URL(string: "https://example.com")!, range: httpsRange)
        attr.addAttribute(.link, value: URL(string: "file:///etc/passwd")!, range: fileRange)

        let height = view.measureHeight(for: attr, width: 420)
        #expect(height > 14)
        view.frame.size.height = height

        // Offset at top-left should be 0 and have no link.
        #expect(view.utf16Offset(at: CGPoint(x: 1, y: 4)) == 0)
        #expect(view.linkURL(at: CGPoint(x: 1, y: 4)) == nil)

        // Clicking inside the https:// link returns it.
        if let httpsRect = view.boundingRect(forUTF16: httpsRange) {
            let mid = CGPoint(x: httpsRect.midX, y: httpsRect.midY)
            #expect(view.linkURL(at: mid) == URL(string: "https://example.com"))
        } else {
            Issue.record("Expected non-nil bounding rect for https link")
        }

        // Clicking inside the file:// link is rejected (http/https only).
        if let fileRect = view.boundingRect(forUTF16: fileRange) {
            let mid = CGPoint(x: fileRect.midX, y: fileRect.midY)
            #expect(view.linkURL(at: mid) == nil)
        } else {
            Issue.record("Expected non-nil bounding rect for file link")
        }

        // Clicking past the end of the line (empty trailing margin) returns nil.
        #expect(view.linkURL(at: CGPoint(x: 415, y: 4)) == nil)
    }

    // MARK: - Markdown Parser & Attributed Prose Builder

    @Test func markdownSplitsProseCodeBlocksAndTables() {
        let md = """
        # Title

        Intro paragraph with `inline` code.

        ```swift
        let x = 42
        ```

        | Col A | Col B |
        | :--- | ---: |
        | one | 1 |
        | two | 2 |

        Closing paragraph.
        """

        let blocks = PCodeTranscriptMarkdown.parseBlocks(md)
        #expect(blocks.count == 4)

        if case .prose(let p0, _) = blocks[0] {
            #expect(p0.contains("# Title"))
            #expect(p0.contains("Intro paragraph"))
        } else {
            Issue.record("Expected prose block at index 0")
        }

        if case .codeBlock(let lang, let code, let fullCode, let chunkIdx, let total) = blocks[1] {
            #expect(lang == "swift")
            #expect(code == "let x = 42")
            #expect(fullCode == "let x = 42")
            #expect(chunkIdx == 0)
            #expect(total == 1)
        } else {
            Issue.record("Expected code block at index 1")
        }

        if case .table(let table) = blocks[2] {
            #expect(table.headers.map(\.plainText) == ["Col A", "Col B"])
            #expect(table.alignments == [.left, .right])
            #expect(table.rows.map { $0.map(\.plainText) } == [["one", "1"], ["two", "2"]])
        } else {
            Issue.record("Expected table block at index 2")
        }

        if case .prose(let p3, _) = blocks[3] {
            #expect(p3.contains("Closing paragraph."))
        } else {
            Issue.record("Expected prose block at index 3")
        }
    }

    @Test func markdownChunksLongCodeBlocksAt200LinesAndPreservesFullCopy() {
        let lines = (1...450).map { "line \($0)" }.joined(separator: "\n")
        let md = "```txt\n\(lines)\n```"
        let blocks = PCodeTranscriptMarkdown.parseBlocks(md)
        #expect(blocks.count == 3)
        if case .codeBlock(_, let chunk0, let full0, let idx0, let total0) = blocks[0] {
            #expect(idx0 == 0 && total0 == 3)
            #expect(chunk0.split(separator: "\n").count == 200)
            #expect(full0 == lines)
        } else {
            Issue.record("Expected chunked code block 0")
        }
        if case .codeBlock(_, _, let full2, let idx2, let total2) = blocks[2] {
            #expect(idx2 == 2 && total2 == 3)
            #expect(full2 == lines)
        } else {
            Issue.record("Expected chunked code block 2")
        }
    }

    @Test func markdownHandlesNestedCodeBlocksAndReferenceLinks() {
        let snap = PCodeTypographySnapshot(style: .sansAndMono, size: 14, weight: .medium, codeFamilyAvailable: false)
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let md = """
        - Item with nested code:

          ```swift
          let nested = true
          ```

        See [docs][ref1] for details.

        [ref1]: https://example.com/docs
        """
        let blocks = PCodeTranscriptMarkdown.parseBlocks(md)
        #expect(blocks.count == 1)
        guard case .prose(let proseMD, let refDefs) = blocks[0] else {
            Issue.record("Expected single prose block")
            return
        }
        let attr = PCodeTranscriptMarkdown.attributedString(
            forProse: proseMD,
            referenceDefinitions: refDefs,
            typography: snap,
            theme: theme)
        let str = attr.string as NSString
        #expect(str.contains("let nested = true"))
        let docsRange = str.range(of: "docs")
        #expect(docsRange.location != NSNotFound)
        #expect(attr.attribute(.link, at: docsRange.location, effectiveRange: nil) as? URL == URL(string: "https://example.com/docs"))
    }

    @Test func markdownProseFiltersLinksRendersRawHTMLAndScalesHeadingInlineCode() {
        let snap = PCodeTypographySnapshot(style: .sansAndMono, size: 14, weight: .medium, codeFamilyAvailable: false)
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let md = """
        # Heading `codeInHeading`

        Visit [safe](https://example.com) and [bad](javascript:alert(1)).

        > Quoted line

        <script>alert("x")</script>

        ---
        """
        let attr = PCodeTranscriptMarkdown.attributedString(forProse: md, typography: snap, theme: theme)
        let str = attr.string as NSString

        // Inline code inside H1 should scale larger than body size (14 * 1.42 * 0.92 ≈ 18.29pt).
        let codeRange = str.range(of: "codeInHeading")
        #expect(codeRange.location != NSNotFound)
        if let font = attr.attribute(.font, at: codeRange.location, effectiveRange: nil) as? NSFont {
            #expect(font.pointSize > 16)
        } else {
            Issue.record("Expected font on heading inline code")
        }
        #expect(attr.attribute(.pcodeInlineCodeFill, at: codeRange.location, effectiveRange: nil) != nil)

        // Safe link has .link; javascript: link does not.
        let safeRange = str.range(of: "safe")
        let badRange = str.range(of: "bad")
        #expect(attr.attribute(.link, at: safeRange.location, effectiveRange: nil) as? URL == URL(string: "https://example.com"))
        #expect(attr.attribute(.link, at: badRange.location, effectiveRange: nil) == nil)

        // Blockquote carries depth attribute.
        let quoteRange = str.range(of: "Quoted line")
        #expect(attr.attribute(.pcodeBlockquoteDepth, at: quoteRange.location, effectiveRange: nil) as? Int == 1)

        // Raw HTML is preserved as literal text.
        #expect(str.contains("<script>alert(\"x\")</script>"))
    }

    // MARK: - Syntax Highlighter

    @Test func syntaxHighlighterNormalizesLanguagesAndTokenizesSwift() async {
        #expect(PCodePrismSyntaxHighlighter.normalizeLanguage("py") == "python")
        #expect(PCodePrismSyntaxHighlighter.normalizeLanguage("TS") == "typescript")
        #expect(PCodePrismSyntaxHighlighter.normalizeLanguage("sh") == "bash")
        #expect(PCodePrismSyntaxHighlighter.normalizeLanguage(nil) == "plain")

        let code = "let count = 42 // answer"
        let tokens = await PCodePrismSyntaxHighlighter.shared.tokenize(code: code, language: "swift")
        let reconstructed = tokens.map(\.content).joined()
        #expect(reconstructed == code)
        #expect(tokens.contains { $0.kind == .keyword })
        #expect(tokens.contains { $0.kind == .comment })
        #expect(PCodePrismSyntaxHighlighter.shared.cachedTokens(code: code, language: "swift") == tokens)

        let pyTokens = await PCodePrismSyntaxHighlighter.shared.tokenize(code: "def greet(name):\n    return f'hi {name}'", language: "py")
        #expect(pyTokens.contains { $0.kind == .keyword })
    }

    @Test @MainActor func textKit2PastEndOfLineClickOnParagraphTwoDoesNotOverflow() {
        let view = PCodeTextKit2BlockView(frame: CGRect(x: 0, y: 0, width: 420, height: 120))
        let attr = NSAttributedString(
            string: "First paragraph\nSecond line 😀",
            attributes: [.font: NSFont.systemFont(ofSize: 14)])
        let h = view.measureHeight(for: attr, width: 420)
        view.frame.size.height = h

        // Clicking far past the right edge of paragraph 2 must clamp cleanly to end of line 2 (no Int.max overflow).
        let offset = view.utf16Offset(at: CGPoint(x: 410, y: h - 4))
        #expect(offset == (attr.string as NSString).length)

        // Clicking far past the right edge of paragraph 1 must clamp to end of paragraph 1 (15), not end of document.
        let p1Offset = view.utf16Offset(at: CGPoint(x: 410, y: 4))
        #expect(p1Offset == 15)
    }

    // MARK: - Rich Block Document View Mounting & Selection

    @Test @MainActor func documentViewMountsRichBlocksAndExtractsSelection() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 600)
        scrollView.layoutSubtreeIfNeeded()

        let longCode = (1...205).map { "row_\($0)" }.joined(separator: "\n")
        let turns = [
            PCodePeekTurn(
                id: 0,
                prompt: "Show me a snippet and a table",
                blocks: [
                    .markdown(id: 0, text: """
                    Here is code:

                    ```swift
                    \(longCode)
                    ```

                    | Key | Val |
                    | --- | --- |
                    | **a** | 1 |
                    """),
                    .tool(PCodePeekToolCall(
                        id: 1,
                        name: "read_file",
                        detail: "app/macos/PCode/PCodePeek.swift",
                        failed: false,
                        elapsedSeconds: 0.2,
                        command: "")),
                ],
                status: .running,
                side: false),
        ]

        let items = PCodeTranscriptItem.flatten(turns: turns)
        // prompt + prose + 2 code chunks + table + tool + status = 7 items
        #expect(items.count == 7)
        scrollView.transcriptDocumentView.setItems(items, pinToBottom: false)
        #expect(scrollView.transcriptDocumentView.layoutEngine.totalHeight > 120)

        scrollView.transcriptDocumentView.selectAll(nil)
        #expect(scrollView.transcriptDocumentView.selection != nil)
        let copied = scrollView.transcriptDocumentView.selectedPlainText()
        // Contiguous chunks of the same code block join with single newline, not blank line.
        #expect(copied.contains("row_200\nrow_201"))
        #expect(!copied.contains("row_200\n\nrow_201"))
        // Table copies plain text (no raw **markdown**).
        #expect(copied.contains("a\t1"))
        #expect(!copied.contains("**a**"))
        // Tool & status copy exact rendered labels.
        #expect(copied.contains("read_file  app/macos/PCode/PCodePeek.swift  (0.2s)"))
        #expect(copied.contains("In progress"))
    }

    // MARK: - Phase 2: Live Streaming Pipeline

    @Test func streamingDifferUsesOpenFenceFastPathAndHandlesChunking() {
        var differ = PCodeStreamingMessageDiffer()

        // 1. Initial prose
        let d1 = differ.update(markdown: "Intro paragraph.\n\n")
        #expect(!d1.usedOpenFenceFastPath)
        #expect(d1.newBlocks.count == 1)

        // 2. Enter an open swift code fence (first frame detects open fence via cmark-gfm)
        let d2 = differ.update(markdown: "Intro paragraph.\n\n```swift\nlet a = 1\n")
        #expect(!d2.usedOpenFenceFastPath)
        #expect(d2.commonPrefixCount == 1) // intro prose unchanged
        #expect(d2.newBlocks.count == 2)

        // 3. Subsequent appends inside the open fence take the append-only fast path
        let d3 = differ.update(markdown: "Intro paragraph.\n\n```swift\nlet a = 1\nlet b = 2\n")
        #expect(d3.usedOpenFenceFastPath)
        #expect(d3.commonPrefixCount == 1) // intro prose still unchanged
        if case .codeBlock(let lang, let code, _, let idx, let total) = d3.newBlocks[1] {
            #expect(lang == "swift")
            #expect(code == "let a = 1\nlet b = 2")
            #expect(idx == 0 && total == 1)
        } else {
            Issue.record("Expected codeBlock at index 1")
        }

        // 4. Streaming past 200 lines splits into 2 chunks on the fast path
        let manyLines = (1...205).map { "let line_\($0) = \($0)" }.joined(separator: "\n")
        let d4 = differ.update(markdown: "Intro paragraph.\n\n```swift\n\(manyLines)\n")
        #expect(d4.usedOpenFenceFastPath)
        #expect(d4.commonPrefixCount == 1)
        #expect(d4.newBlocks.count == 3) // prose + chunk0 + chunk1

        // 5. Closing the fence + adding trailing prose falls back cleanly to exact parse
        let d5 = differ.update(markdown: "Intro paragraph.\n\n```swift\n\(manyLines)\n```\n\nDone!")
        #expect(!d5.usedOpenFenceFastPath)
        #expect(d5.newBlocks.count == 4) // prose + chunk0 + chunk1 + trailing prose
        #expect(d5.commonPrefixCount == 3) // intro + both code chunks unchanged!
    }

    @Test @MainActor func textKit2SuffixOnlyUpdatePreservesPrefixAndMatchesFullText() {
        let view = PCodeTextKit2BlockView(frame: CGRect(x: 0, y: 0, width: 420, height: 100))
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 14)]
        let first = NSAttributedString(string: "Hello world", attributes: attrs)
        let second = NSAttributedString(string: "Hello world, streaming tokens!", attributes: attrs)

        #expect(PCodeTextKit2BlockView.commonAttributedPrefixLength(first, second) == 11)
        let h1 = view.configure(attributedText: first, width: 420)
        let h2 = view.configure(attributedText: second, width: 420)
        #expect(h1 > 10)
        #expect(h2 >= h1)
        #expect(view.textContentStorage.attributedString?.string == "Hello world, streaming tokens!")
    }

    @Test @MainActor func streamingPreservesSelectionAndJumpToBottomPill() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 320)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView

        doc.beginStreamingTurn(prompt: "Explain how streaming works")
        doc.beginStreamingMessage(id: "msg-1")
        doc.appendStreamingDelta("First settled paragraph with selectable words.\n\n")

        // Select "settled paragraph" inside item 1 (prose block 0)
        let proseIdx = 1
        let rendered = doc.plainTextForCopy(at: proseIdx) as NSString
        let targetRange = rendered.range(of: "settled paragraph")
        #expect(targetRange.location != NSNotFound)

        // Simulate user dragging a selection and scrolling up
        doc.setItems(doc.items, pinToBottom: true)
        doc.beginStreamingMessage(id: "msg-2")
        doc.appendStreamingDelta("Keep this selected phrase intact.")
        let streamIdx = doc.items.count - 1
        let streamStr = doc.plainTextForCopy(at: streamIdx) as NSString
        let selRange = streamStr.range(of: "selected phrase")
        #expect(selRange.location != NSNotFound)

        // Select "selected phrase" inside the streaming prose block
        doc.setSelection(PCodeTranscriptSelection(
            anchor: .init(itemIndex: streamIdx, utf16Offset: selRange.location),
            focus: .init(itemIndex: streamIdx, utf16Offset: NSMaxRange(selRange))))
        #expect(doc.selectedPlainText() == "selected phrase")

        // Unpin (as if user scrolled up) and stream a fenced code block after the prose
        doc.unpinFromBottom()
        #expect(!scrollView.isJumpToBottomVisible)

        doc.appendStreamingDelta("\n\n```swift\nlet x = 42\n")
        doc.appendStreamingDelta("let y = 99\n")
        #expect(doc.lastStreamingUpdateUsedOpenFenceFastPath)
        #expect(doc.hasUnreadStreamingContent)
        #expect(scrollView.isJumpToBottomVisible)
        // Selection in the prose block survives live streaming completely intact!
        #expect(doc.selectedPlainText() == "selected phrase")

        // Clicking Jump to bottom re-pins and hides the pill
        doc.scrollToBottomAndPin()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!doc.hasUnreadStreamingContent)
        #expect(!scrollView.isJumpToBottomVisible)

        doc.finishStreamingMessage()
        doc.finishStreamingTurn(status: .complete)
        #expect(doc.selectedPlainText() == "selected phrase")
    }

    @Test @MainActor func streamingFourXReplayStressTest() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 760, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView

        // Build a realistic tool-heavy multi-turn transcript (40 tool cards + prose + code + table)
        var turns: [PCodePeekTurn] = []
        for t in 0..<4 {
            var blocks: [PCodePeekBlock] = []
            for toolIdx in 0..<10 {
                blocks.append(.tool(PCodePeekToolCall(
                    id: t * 100 + toolIdx,
                    name: toolIdx % 2 == 0 ? "read_file" : "run_command",
                    detail: "app/macos/PCode/File\(toolIdx).swift",
                    failed: false,
                    elapsedSeconds: 0.15,
                    command: toolIdx % 2 == 0 ? "" : "make app-test")))
            }
            blocks.append(.markdown(id: t * 100 + 50, text: """
            ## Turn \(t + 1) Summary

            Completed **\(10)** tool calls with `inline_code` and a snippet:

            ```swift
            struct Turn\(t) {
                let count = 10
            }
            ```

            | Metric | Value |
            | --- | --- |
            | Tools | 10 |
            | Status | OK |
            """))
            turns.append(PCodePeekTurn(
                id: t,
                prompt: "Run verification pass \(t + 1)",
                blocks: blocks,
                status: .complete,
                side: false))
        }

        let replayer = PCodeTranscriptStreamReplayer(
            turns: turns,
            documentView: doc,
            speedMultiplier: 4.0,
            chunkSize: 24)
        replayer.runAllSynchronously()

        // Each turn produces: 1 prompt + 10 tools + 1 prose + 1 codeBlock + 1 table = 14 items -> 56 total
        #expect(doc.items.count == 56)
        #expect(doc.layoutEngine.entries.count == 56)
        #expect(doc.layoutEngine.totalHeight > 800)

        // Verify yOffsets are strictly increasing
        for i in 1..<doc.layoutEngine.entries.count {
            #expect(doc.layoutEngine.entries[i].yOffset > doc.layoutEngine.entries[i - 1].yOffset)
        }
    }

    @Test func streamingTextPacerSmoothsBurstsAndPreservesGraphemeClusters() {
        var pacer = PCodeStreamingTextPacer(minCharsPerSecond: 120, maxCharsPerSecond: 2400, catchUpSeconds: 0.2)
        let input = "Hello 👨‍👩‍👧‍👦 streaming world!"
        pacer.enqueue(input)
        #expect(pacer.hasPending)

        var collected = ""
        var ticks = 0
        while pacer.hasPending && ticks < 200 {
            if let chunk = pacer.drain(dt: 1.0 / 120.0) {
                collected.append(chunk)
            }
            ticks += 1
        }
        #expect(collected == input)
        #expect(ticks > 1) // smoothed over multiple 120Hz frames rather than dumped in 1 frame

        // Huge burst accelerates up toward maxCharsPerSecond
        pacer.enqueue(String(repeating: "a", count: 1000))
        #expect(pacer.currentRate() >= 2000)
        #expect(pacer.flushAll()?.count == 1000)
        #expect(!pacer.hasPending)
    }

    @Test func smoothScrollGlideConvergesCriticallyDampedWithoutOvershoot() {
        var glide = PCodeSmoothScrollGlide(stiffness: 220.0, maxOffset: 480.0)
        glide.addOffset(60.0)
        #expect(glide.isActive)
        #expect(glide.remainingOffset == 60.0)

        var prev = glide.remainingOffset
        var ticks = 0
        while glide.isActive && ticks < 240 {
            let next = glide.step(dt: 1.0 / 120.0)
            #expect(next >= 0) // never overshoots past bottom
            #expect(next <= prev + 0.001) // monotonically approaches 0
            prev = next
            ticks += 1
        }
        #expect(!glide.isActive)
        #expect(glide.remainingOffset == 0)
        #expect(ticks > 5 && ticks < 180)
    }
}
