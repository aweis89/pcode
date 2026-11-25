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
        // Per-top-level-block splitting: heading, intro paragraph, code block, table, closing paragraph.
        #expect(blocks.count == 5)

        if case .prose(let p0, _) = blocks[0] {
            #expect(p0.contains("# Title"))
        } else {
            Issue.record("Expected heading prose block at index 0")
        }

        if case .prose(let p1, _) = blocks[1] {
            #expect(p1.contains("Intro paragraph"))
        } else {
            Issue.record("Expected intro prose block at index 1")
        }

        if case .codeBlock(let lang, let code, let fullCode, let chunkIdx, let total) = blocks[2] {
            #expect(lang == "swift")
            #expect(code == "let x = 42")
            #expect(fullCode == "let x = 42")
            #expect(chunkIdx == 0)
            #expect(total == 1)
        } else {
            Issue.record("Expected code block at index 2")
        }

        if case .table(let table) = blocks[3] {
            #expect(table.headers.map(\.plainText) == ["Col A", "Col B"])
            #expect(table.alignments == [.left, .right])
            #expect(table.rows.map { $0.map(\.plainText) } == [["one", "1"], ["two", "2"]])
        } else {
            Issue.record("Expected table block at index 3")
        }

        if case .prose(let p4, _) = blocks[4] {
            #expect(p4.contains("Closing paragraph."))
        } else {
            Issue.record("Expected prose block at index 4")
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
        #expect(blocks.count == 2)
        guard case .prose(let listMD, let listRefs) = blocks[0],
              case .prose(let paraMD, let paraRefs) = blocks[1]
        else {
            Issue.record("Expected two prose blocks (list + paragraph)")
            return
        }
        let listAttr = PCodeTranscriptMarkdown.attributedString(
            forProse: listMD,
            referenceDefinitions: listRefs,
            typography: snap,
            theme: theme)
        #expect(listAttr.string.contains("let nested = true"))

        let paraAttr = PCodeTranscriptMarkdown.attributedString(
            forProse: paraMD,
            referenceDefinitions: paraRefs,
            typography: snap,
            theme: theme)
        let str = paraAttr.string as NSString
        let docsRange = str.range(of: "docs")
        #expect(docsRange.location != NSNotFound)
        #expect(paraAttr.attribute(.link, at: docsRange.location, effectiveRange: nil) as? URL == URL(string: "https://example.com/docs"))
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

        // Simulate user dragging a selection inside settled block 1 and scrolling up
        doc.setSelection(PCodeTranscriptSelection(
            anchor: .init(itemIndex: proseIdx, utf16Offset: targetRange.location),
            focus: .init(itemIndex: proseIdx, utf16Offset: NSMaxRange(targetRange))))
        #expect(doc.selectedPlainText() == "settled paragraph")

        // Unpin (as if user scrolled up) and stream a settled fenced code block after the prose
        doc.unpinFromBottom()
        #expect(!scrollView.isJumpToBottomVisible)

        doc.appendStreamingDelta("```swift\nlet x = 42\n```\n\n")
        #expect(doc.hasUnreadStreamingContent)
        #expect(scrollView.isJumpToBottomVisible)
        // Selection in the settled prose block survives live streaming completely intact!
        #expect(doc.selectedPlainText() == "settled paragraph")

        // Clicking Jump to bottom re-pins and hides the pill
        doc.scrollToBottomAndPin()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!doc.hasUnreadStreamingContent)
        #expect(!scrollView.isJumpToBottomVisible)

        doc.finishStreamingMessage()
        doc.finishStreamingTurn(status: .complete)
        #expect(doc.selectedPlainText() == "settled paragraph")
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
            speedMultiplier: 4.0)
        replayer.runAllSynchronously()

        // Each turn: 1 prompt + 10 tools + 2 prose (heading + para) + 1 codeBlock + 1 table = 15 items -> 60 total
        #expect(doc.items.count == 60)
        #expect(doc.layoutEngine.entries.count == 60)
        #expect(doc.layoutEngine.totalHeight > 800)

        // Verify yOffsets are strictly increasing
        for i in 1..<doc.layoutEngine.entries.count {
            #expect(doc.layoutEngine.entries[i].yOffset > doc.layoutEngine.entries[i - 1].yOffset)
        }
    }

    @Test func markdownSettlerHoldsUnsettledBoldUntilBlankLineOrFinish() {
        var settler = PCodeMarkdownSettler()
        // Partial unclosed bold must NOT emit raw `**bold`
        #expect(settler.append("Hello **bold").isEmpty)
        #expect(settler.hasUnsettledTail)

        // Closing the bold still holds the paragraph until a blank line or finish
        #expect(settler.append(" text** here.").isEmpty)

        // Blank line settles the paragraph with final bold Markdown intact
        let settled = settler.append("\n\n")
        #expect(settled.count == 1)
        if case .prose(let md, _) = settled[0] {
            #expect(md == "Hello **bold text** here.")
        } else {
            Issue.record("Expected settled prose block")
        }

        // Trailing paragraph settles on finish()
        #expect(settler.append("Final line").isEmpty)
        let tail = settler.finish()
        #expect(tail.count == 1)
    }

    @Test func revealQueueGatesOnVisibilityAndNeverDecelerates() {
        var queue = PCodeRevealQueue()
        queue.enqueue(itemID: "p1", style: .inkSweep, targetExtent: 400)
        queue.enqueue(itemID: "p2", style: .inkSweep, targetExtent: 400)

        // Off-screen item is held at 0 until visible
        let blocked = queue.step(dt: 1.0 / 60.0, speedMultiplier: 1.0, isVisible: { _ in false })
        #expect(blocked.isEmpty)
        #expect(queue.entries.first?.progress == 0)

        // Once visible, advances smoothly with monotonic speed
        let u1 = queue.step(dt: 1.0 / 60.0, speedMultiplier: 1.0, isVisible: { _ in true })
        #expect(!u1.isEmpty)
        let speed1 = queue.entries.first?.speed ?? 0
        #expect(speed1 > 0)

        let _ = queue.step(dt: 1.0 / 60.0, speedMultiplier: 1.0, isVisible: { _ in true })
        if let speed2 = queue.entries.first?.speed {
            #expect(speed2 >= speed1)
        }
    }

    @Test func layoutEngineInsertAndRemoveKeepYOffsetsConsistent() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 10, bottomPadding: 10, backingScale: 2)
        engine.append(id: "a", height: 40, spacingAfter: 8)
        engine.append(id: "c", height: 60, spacingAfter: 8)
        engine.insert(at: 1, id: "b", height: 50, spacingAfter: 10)
        #expect(engine.entries.map(\.id) == ["a", "b", "c"])
        #expect(engine.entries[0].yOffset == 10)
        #expect(engine.entries[1].yOffset == 58)
        #expect(engine.entries[2].yOffset == 118)

        engine.remove(at: 1)
        #expect(engine.entries.map(\.id) == ["a", "c"])
        #expect(engine.entries[1].yOffset == 58)
    }

    @Test func smoothScrollGlideConvergesCriticallyDampedWithoutOvershoot() {
        var glide = PCodeSmoothScrollGlide(minSpeed: 110.0, maxSpeed: 900.0, catchUpSeconds: 0.55, maxOffset: 2400.0)
        glide.addOffset(60.0)
        #expect(glide.isActive)
        #expect(glide.remainingOffset == 60.0)

        // Absorbing a shrink reduces remainingOffset without pulling viewport backward
        glide.absorbShrink(15.0)
        #expect(glide.remainingOffset == 45.0)

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
        #expect(ticks > 5 && ticks < 240)
    }

    @Test @MainActor func textKit2KeepsTextStorageAttachedAndRespectsSurrogateBoundaries() {
        let view = PCodeTextKit2BlockView(frame: CGRect(x: 0, y: 0, width: 420, height: 100))
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 14)]
        let first = NSAttributedString(string: "Hi 😀", attributes: attrs)
        let second = NSAttributedString(string: "Hi 😁", attributes: attrs)

        // Shared high surrogate (0xD83D) must back up to offset 3 ("Hi "), not 4 (mid-surrogate).
        #expect(PCodeTextKit2BlockView.commonAttributedPrefixLength(first, second) == 3)

        _ = view.configure(attributedText: first, width: 420)
        #expect(view.textContentStorage.textStorage != nil)
        _ = view.configure(attributedText: second, width: 420)
        #expect(view.textContentStorage.textStorage != nil)
        #expect(view.textContentStorage.attributedString?.string == "Hi 😁")
    }

    @Test @MainActor func jumpToBottomPillWidthStaysStableAcrossRepeatedLayouts() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let w1 = scrollView.jumpToBottomButton.frame.width
        scrollView.needsLayout = true
        scrollView.layoutSubtreeIfNeeded()
        scrollView.needsLayout = true
        scrollView.layoutSubtreeIfNeeded()
        #expect(abs(scrollView.jumpToBottomButton.frame.width - w1) < 0.5)
    }

    @Test func streamingDifferGuardsNestedAndIndentedFencesAndKeepsChunkZeroStable() {
        var differ = PCodeStreamingMessageDiffer()

        // Nested fence inside a list item followed by a new unclosed fence must not corrupt body.
        let nested = "- ~~~\n  nested\n  ~~~\n\n```\nhello\n"
        let d1 = differ.update(markdown: nested)
        if case .codeBlock(_, let code, _, _, _) = d1.newBlocks.last {
            #expect(code == "hello")
        } else {
            Issue.record("Expected trailing codeBlock")
        }

        // Indented opening fence stays on exact cmark-gfm path (no indentation jump).
        var indentedDiffer = PCodeStreamingMessageDiffer()
        _ = indentedDiffer.update(markdown: "  ```swift\n  let x = 1\n")
        let dInd = indentedDiffer.update(markdown: "  ```swift\n  let x = 1\n  let y = 2\n")
        #expect(!dInd.usedOpenFenceFastPath)

        // Settled chunk 0 stays in commonPrefixCount when chunk 1 grows past 200 lines.
        var chunkDiffer = PCodeStreamingMessageDiffer()
        let lines202 = (1...202).map { "line_\($0)" }.joined(separator: "\n")
        _ = chunkDiffer.update(markdown: "```swift\n\(lines202)\n")
        let dGrow = chunkDiffer.update(markdown: "```swift\n\(lines202)\nline_203\n")
        #expect(dGrow.usedOpenFenceFastPath)
        #expect(dGrow.newBlocks.count == 2)
        #expect(dGrow.commonPrefixCount == 1) // chunk 0 untouched while chunk 1 grows!
    }

    @Test func layoutEngineRatchetReleaseMarksUnmeasured() {
        var engine = PCodeTranscriptLayoutEngine(topPadding: 0, bottomPadding: 0, backingScale: 2)
        let idx = engine.append(id: "tail", height: 100, spacingAfter: 8, measured: true)
        engine.setTailRatchet(at: idx, active: true)
        #expect(engine.entries[idx].isMeasured)
        engine.setTailRatchet(at: idx, active: false)
        #expect(!engine.entries[idx].isMeasured)
        #expect(engine.entries[idx].minHeight == 0)
    }

    // MARK: - Phase 3: Dual-Format Event Decoder, Reducer & Rich Cards

    @Test func hostEventDecoderHandlesBothJournalAndLiveSocketFormats() {
        // Flat journal record (`transcript.jsonl`)
        let journalEvent = PCodeHostEventDecoder.decodeJournalRecord([
            "kind": "EditCompleted",
            "call_id": "c4",
            "path": "src/app.py",
            "operation": "edit",
            "patch": "@@ -1 +1 @@\n-old\n+new",
            "added": 1,
            "removed": 1,
        ])
        #expect(journalEvent == .editCompleted(
            callID: "c4",
            parentCallID: "",
            path: "src/app.py",
            operation: "edit",
            patch: "@@ -1 +1 @@\n-old\n+new",
            added: 1,
            removed: 1,
            truncated: false))

        // Nested RPC socket notification (`{"$event": {"kind": ..., "fields": ...}}`)
        let rpcEvent = PCodeHostEventDecoder.decodeRPCCall(
            method: "turn_event",
            args: [[
                "$event": [
                    "kind": "EditPreview",
                    "fields": [
                        "call_id": "c4",
                        "path": "src/app.py",
                        "text": "+new",
                        "kind": "edit",
                    ],
                ],
            ]],
            kwargs: [:])
        #expect(rpcEvent == .editPreview(callID: "c4", path: "src/app.py", text: "+new", kind: "edit"))

        // Display-only ChildText and terminal-only redraw are ignored cleanly
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "redraw", args: [], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(kind: "ChildText", fields: ["call_id": "c1", "text": "hi"]) == nil)
    }

    @Test @MainActor func eventReducerHandlesUpsertsParentNestingDiffsThinkingAndPlan() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 760, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Refactor parser", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.thinkingDelta("Checking files…"), reveal: false)
        reducer.apply(.thinking(text: "Checked files and found 2 bugs."), reveal: false)
        // Thinking upserts in place (still 1 thinking item)
        #expect(doc.items.filter { $0.kind == .thinking }.count == 1)

        // Child tool arrives BEFORE parent (orphan buffering), then parent arrives
        reducer.apply(.toolStarted(callID: "child-1", parentCallID: "parent-1", name: "read_file", detail: "a.py", command: "", purpose: ""), reveal: false)
        reducer.apply(.toolStarted(callID: "parent-1", parentCallID: "", name: "delegate", detail: "worker", command: "", purpose: ""), reveal: false)
        let parentIdx = doc.indexOfItem(id: "call-parent-1")
        let childIdx = doc.indexOfItem(id: "call-child-1")
        #expect(parentIdx != nil && childIdx != nil)
        #expect(childIdx == (parentIdx ?? -1) + 1)
        #expect(doc.items[childIdx!].depth == 1)

        // CommandOutput caps tail lines and upserts onto existing tool call
        let hugeOutput = (1...80).map { "out_\($0)" }.joined(separator: "\n")
        reducer.apply(.commandOutput(callID: "parent-1", command: "make test", output: hugeOutput), reveal: false)
        if case .tool(let updatedCall, _) = doc.items[parentIdx!].payload {
            #expect(updatedCall.output.contains("out_80"))
            #expect(updatedCall.output.contains("earlier lines omitted"))
        } else {
            Issue.record("Expected updated tool call")
        }

        // EditPreview morphs into EditCompleted under the same item ID (`edit-e1`)
        reducer.apply(.editPreview(callID: "e1", path: "a.py", text: "+x = 1", kind: "edit"), reveal: false)
        reducer.apply(.editCompleted(callID: "e1", parentCallID: "", path: "a.py", operation: "edit", patch: "@@ -1 +1 @@\n-x = 0\n+x = 1", added: 1, removed: 1, truncated: false), reveal: false)
        #expect(doc.items.filter { $0.kind == .diff }.count == 1)

        // PlanUpdated upserts single card; PlanPreview(nil) restores authoritative plan instead of deleting it!
        reducer.apply(.planUpdated(items: [
            PCodePlanItem(content: "Inspect code", status: "completed"),
            PCodePlanItem(content: "Write tests", status: "in_progress"),
        ]), reveal: false)
        #expect(doc.items.filter { $0.kind == .plan }.count == 1)
        reducer.apply(.planPreview(items: [PCodePlanItem(content: "Draft step", status: "pending")]), reveal: false)
        reducer.apply(.planPreview(items: nil), reveal: false)
        #expect(doc.items.filter { $0.kind == .plan }.count == 1)
        if let planItem = doc.items.first(where: { $0.kind == .plan }),
           case .plan(let restored) = planItem.payload
        {
            #expect(restored.count == 2)
            #expect(restored[0].content == "Inspect code")
        } else {
            Issue.record("Expected restored authoritative plan")
        }

        // Streamed TextDelta followed by closing Message must NOT duplicate the assistant prose!
        let beforeMsgCount = doc.items.filter { $0.kind == .prose }.count
        reducer.apply(.textDelta("Hello streamed "), reveal: false)
        reducer.apply(.textDelta("world."), reveal: false)
        reducer.apply(.message(markdown: "Hello streamed world."), reveal: false)
        #expect(doc.items.filter { $0.kind == .prose }.count == beforeMsgCount + 1)
    }

    @Test func markdownSettlerPreservesLooseListItemsAcrossBlankLines() {
        var settler = PCodeMarkdownSettler()
        // A loose list followed by `\n\n` must NOT prematurely settle before item 2 arrives.
        #expect(settler.append("- item 1\n\n").isEmpty)
        #expect(settler.append("- item 2\n").isEmpty)
        let settled = settler.finish()
        #expect(settled.count == 1)
        if case .prose(let md, _) = settled[0] {
            #expect(md.contains("item 1"))
            #expect(md.contains("item 2"))
        } else {
            Issue.record("Expected complete loose list prose block")
        }
    }

    // MARK: - Prompt Height, Tool Card Line Count / Bottom Collapse & Phase 5 (Find + Accessibility)

    @Test @MainActor func promptHeightStaysTightAfterReuseAndTrailingNewlines() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let textView = PCodeTextKit2BlockView()
        let snap = PCodeTypography.shared.snapshot
        let para = NSMutableParagraphStyle()
        para.minimumLineHeight = snap.lineHeight()
        para.maximumLineHeight = snap.lineHeight()

        // First configure with a tall 10-line prompt (simulating pooled reuse).
        let tall = NSAttributedString(
            string: (1...10).map { "Line \($0)" }.joined(separator: "\n"),
            attributes: [.font: snap.proseNSFont(), .paragraphStyle: para])
        let tallH = textView.configure(attributedText: tall, width: 500)

        // Then reconfigure at the SAME width with a 2-line prompt ("You\nHi").
        let short = NSAttributedString(
            string: "You\nHi",
            attributes: [.font: snap.proseNSFont(), .paragraphStyle: para])
        let shortH = textView.configure(attributedText: short, width: 500)

        #expect(shortH < tallH * 0.4)
        #expect(shortH <= snap.lineHeight() * 2.5)
    }

    @Test @MainActor func toolCardShowsCompactLineCountAndBottomCollapseWhenExpanded() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let snap = PCodeTypography.shared.snapshot
        let output = (1...25).map { "output line \($0)" }.joined(separator: "\n")
        let call = PCodePeekToolCall(
            id: 1,
            name: "run_command",
            detail: "make test",
            failed: false,
            elapsedSeconds: 1.4,
            command: "make test",
            output: output)

        #expect(PCodeToolCardView.expandableLineCount(for: call) == 25)

        let compactAttr = PCodeToolCardView.attributedText(for: call, isExpanded: false, typography: snap, theme: theme)
        #expect(compactAttr.string.contains("25 lines"))

        let expandedAttr = PCodeToolCardView.attributedText(for: call, isExpanded: true, typography: snap, theme: theme)
        #expect(!expandedAttr.string.contains("· 25 lines"))
        #expect(expandedAttr.string.contains("output line 25"))

        let card = PCodeToolCardView()
        let compactH = card.configure(
            call: call,
            isExpanded: false,
            attributedText: compactAttr,
            width: 520,
            theme: theme,
            selectedRange: nil,
            onToggle: {})
        let expandedH = card.configure(
            call: call,
            isExpanded: true,
            attributedText: expandedAttr,
            width: 520,
            theme: theme,
            selectedRange: nil,
            onToggle: {})
        #expect(expandedH > compactH + 100)
    }

    @Test @MainActor func textFinderClientAndSearchIndexRoundTrip() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 480)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView

        let items = [
            PCodeTranscriptItem(id: "p1", payload: .prompt(label: "You", text: "Find the needle", isSide: false), spacingAfter: 10),
            PCodeTranscriptItem(id: "m1", payload: .prose(markdown: "Here is the **needle** in prose.", referenceDefinitions: "", copyMarkdown: nil), spacingAfter: 10),
        ]
        doc.setItems(items, pinToBottom: false)

        let totalLen = doc.stringLength()
        #expect(totalLen > 20)

        var outRange = NSRange(location: 0, length: 0)
        var boundary: ObjCBool = false
        let firstChunk = doc.string(at: 0, effectiveRange: &outRange, endsWithSearchBoundary: &boundary)
        #expect(firstChunk.contains("Find the needle"))
        #expect(boundary.boolValue)

        // Select "needle" inside item 1 via NSTextFinderClient.selectedRanges
        let index = doc.currentSearchIndex()
        let item1Start = index.entries[1].utf16Location
        let item1Text = doc.plainTextForCopy(at: 1) as NSString
        let localRange = item1Text.range(of: "needle")
        #expect(localRange.location != NSNotFound)
        let flatMatch = NSRange(location: item1Start + localRange.location, length: localRange.length)
        doc.selectedRanges = [NSValue(range: flatMatch)]
        #expect(doc.selectedPlainText() == "needle")
        #expect(doc.firstSelectedRange == flatMatch)

        // scrollRangeToVisible unpins sticky-bottom
        doc.scrollToBottomAndPin()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        doc.scrollRangeToVisible(flatMatch)
        #expect(doc.stickyBottom.mode == .userScrolledUp)
    }

    @Test @MainActor func accessibilityProxiesExposeVisibleRowsAndToggleTool() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 480)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView

        let call = PCodePeekToolCall(
            id: 7,
            name: "run_command",
            detail: "ls -la",
            failed: false,
            elapsedSeconds: 0.2,
            command: "ls -la",
            output: "a\nb\nc")
        doc.setItems([
            PCodeTranscriptItem(id: "p1", payload: .prompt(label: "You", text: "List files", isSide: false), spacingAfter: 10),
            PCodeTranscriptItem(id: "t1", payload: .tool(call, isExpanded: false), spacingAfter: 8),
        ], pinToBottom: false)

        #expect(doc.accessibilityRole() == .list)
        let children = (doc.accessibilityChildren() as? [PCodeTranscriptAccessibilityRowElement]) ?? []
        #expect(children.count == 2)
        #expect(children[0].accessibilityLabel() == "You")
        #expect(children[1].isExpandable)
        #expect(!doc.isItemExpanded(id: "t1"))

        // Pressing the expandable tool proxy toggles its expanded state.
        #expect(children[1].accessibilityPerformPress())
        #expect(doc.isItemExpanded(id: "t1"))
    }
}
