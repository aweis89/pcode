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

    @Test func stickyBottomUnpinsOnSmallUpwardScrollAndOnlyRepinsMovingDown() {
        var state = PCodeStickyBottomState()

        // A short trackpad nudge inside the re-pin band still unpins and stays unpinned.
        state.didStartLiveScroll()
        state.didLiveScroll(distanceFromBottom: 4, movedUpward: true)
        #expect(state.mode == .userScrolledUp)
        state.didEndLiveScroll(distanceFromBottom: 4)
        #expect(state.mode == .userScrolledUp)

        // Scrolling back down into the band re-pins at gesture end.
        state.didStartLiveScroll()
        state.didLiveScroll(distanceFromBottom: 3, movedUpward: false)
        state.didEndLiveScroll(distanceFromBottom: 3)
        #expect(state.mode == .pinnedToBottom)

        // Same for small legacy wheel steps.
        state.didLegacyWheelScroll(distanceFromBottom: 3, movedUpward: true)
        #expect(state.mode == .userScrolledUp)
        state.didLegacyWheelScroll(distanceFromBottom: 6, movedUpward: true)
        #expect(state.mode == .userScrolledUp)
        state.didLegacyWheelScroll(distanceFromBottom: 0, movedUpward: false)
        #expect(state.mode == .pinnedToBottom)

        // Landing exactly on the bottom re-pins even when the move was upward (a clamp).
        state.unpinForUserAction()
        state.didLegacyWheelScroll(distanceFromBottom: 0, movedUpward: true)
        #expect(state.mode == .pinnedToBottom)
    }

    @Test @MainActor func userScrollUpIsNotPulledBackByStreaming() {
        for isLive in [true, false] {
            let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
            let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
            scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 300)
            scrollView.layoutSubtreeIfNeeded()
            let doc = scrollView.transcriptDocumentView
            let clip = scrollView.contentView

            doc.beginStreamingTurn(prompt: "Stream a long answer")
            doc.beginStreamingMessage(id: "msg-1")
            for i in 0..<40 {
                doc.appendStreamingDelta("Paragraph \(i) with enough words to wrap a little.\n\n")
            }
            #expect(doc.stickyBottom.mode == .pinnedToBottom)

            // Scroll up a few points, as a slow trackpad drag or a smooth mouse wheel would.
            if isLive { doc.handleWillStartLiveScroll() }
            clip.scroll(to: CGPoint(x: 0, y: clip.bounds.origin.y - 4))
            doc.handleBoundsDidChange()
            if isLive { doc.handleDidEndLiveScroll() }
            #expect(doc.stickyBottom.mode == .userScrolledUp)

            let readingY = clip.bounds.origin.y
            for i in 0..<10 {
                doc.appendStreamingDelta("More streamed text \(i).\n\n")
            }
            #expect(abs(clip.bounds.origin.y - readingY) < 0.5)
            #expect(doc.stickyBottom.mode == .userScrolledUp)
        }
    }

    @Test @MainActor func overshootBounceBackDoesNotUnpin() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 300)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let clip = scrollView.contentView

        doc.beginStreamingTurn(prompt: "Stream a long answer")
        doc.beginStreamingMessage(id: "msg-1")
        for i in 0..<40 {
            doc.appendStreamingDelta("Paragraph \(i) with enough words to wrap a little.\n\n")
        }
        let bottomY = clip.bounds.origin.y

        // Flick past the bottom, then the elastic bounce moves the origin back up.
        doc.handleWillStartLiveScroll()
        clip.setBoundsOrigin(CGPoint(x: 0, y: bottomY + 30))
        doc.handleBoundsDidChange()
        clip.setBoundsOrigin(CGPoint(x: 0, y: bottomY + 10))
        doc.handleBoundsDidChange()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        doc.handleDidEndLiveScroll()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
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

    @Test @MainActor func copiedSubAgentReportCodeUsesRealNewlines() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let call = PCodePeekToolCall(
            id: 1, name: "delegate_task", detail: "worker · fix", failed: false,
            elapsedSeconds: 3, command: "", output: "Run:\n\n```sh\nmake lint\nmake test\n```")
        let doc = scrollView.transcriptDocumentView
        doc.setItems([PCodeTranscriptItem(id: "call-1", payload: .tool(call, isExpanded: true), spacingAfter: 5)],
                     pinToBottom: false)

        doc.selectAll(nil)
        let copied = doc.selectedPlainText()
        #expect(copied.contains("make lint\nmake test"))
        #expect(!copied.contains("\u{2028}"))
    }

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

        // Default grouped mode folds each turn's 10 consecutive tools into 1 toolGroup row:
        // (1 prompt + 1 toolGroup + 2 prose + 1 codeBlock + 1 table) * 4 = 24 displayed items (60 raw).
        #expect(doc.rawItems.count == 60)
        #expect(doc.items.count == 24)
        #expect(doc.layoutEngine.entries.count == 24)
        #expect(doc.layoutEngine.totalHeight > 800)

        // Verify yOffsets are strictly increasing in grouped mode
        for i in 1..<doc.layoutEngine.entries.count {
            #expect(doc.layoutEngine.entries[i].yOffset > doc.layoutEngine.entries[i - 1].yOffset)
        }

        // Switching to flat layout restores all 60 items cleanly.
        doc.groupTools = false
        #expect(doc.items.count == 60)
        #expect(doc.layoutEngine.entries.count == 60)
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

        // Display-only ChildText, plan snapshots, and terminal-only redraw are ignored cleanly
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "redraw", args: [], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(kind: "ChildText", fields: ["call_id": "c1", "text": "hi"]) == nil)
        let planFields: [String: Any] = ["items": [["content": "Step", "status": "pending"]]]
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(kind: "PlanUpdated", fields: planFields) == nil)
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(kind: "PlanPreview", fields: planFields) == nil)
    }

    @Test func thinkingAccessibilityTitleFormatsDuration() {
        func title(_ streaming: Bool, _ seconds: Double?) -> String {
            PCodeThinkingCardView.accessibilityTitle(for: PCodeThinkingPayload(text: "x", isStreaming: streaming, elapsedSeconds: seconds))
        }
        #expect(title(true, nil) == "Thinking")
        #expect(title(false, nil) == "Thought")
        #expect(title(false, 0.2) == "Thought for 1s")
        #expect(title(false, 12.4) == "Thought for 12s")
        #expect(title(false, 120) == "Thought for 2m")
        #expect(title(false, 125) == "Thought for 2m 5s")
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(
            kind: "Thinking", fields: ["text": "t", "elapsed_seconds": 4.5]) == .thinking(text: "t", elapsedSeconds: 4.5))
        #expect(PCodeHostEventDecoder.decodeRuntimeEvent(
            kind: "Thinking", fields: ["text": "t"]) == .thinking(text: "t", elapsedSeconds: nil))
    }

    @Test @MainActor func thinkingShowsInFullOrHidesWithShowThinking() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 760, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.showThinking = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        func thinkingRows() -> [(index: Int, payload: PCodeThinkingPayload)] {
            doc.items.enumerated().compactMap { index, item in
                if case .thinking(let payload) = item.payload { return (index, payload) }
                return nil
            }
        }

        reducer.apply(.turnStarted(prompt: "Go", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.thinkingDelta("Weighing **options**"), reveal: false)
        #expect(thinkingRows().first?.payload.isStreaming == true)
        // The whole thought is selectable text, not a collapsed summary.
        if let row = thinkingRows().first {
            #expect(doc.plainTextForCopy(at: row.index).contains("Weighing"))
        }
        // Answer text without a `Thinking` marker still settles the block.
        reducer.apply(.textDelta("Done."), reveal: false)
        #expect(thinkingRows().first?.payload.isStreaming == false)
        if let row = thinkingRows().first {
            // Settled thinking renders its Markdown.
            #expect(doc.plainTextForCopy(at: row.index) == "Weighing options")
        }

        // The host's `/show-thinking off` hides every row; new thinking stays hidden...
        reducer.apply(.thinkingMode("off"), reveal: false)
        #expect(thinkingRows().isEmpty)
        reducer.apply(.message(markdown: "Done."), reveal: false)
        reducer.apply(.thinkingDelta("More"), reveal: false)
        reducer.apply(.thinking(text: "More thoughts", elapsedSeconds: 2), reveal: false)
        #expect(thinkingRows().isEmpty)
        #expect(doc.items.contains { $0.kind == .prose })
        // ...and any other mode brings them all back.
        reducer.apply(.thinkingMode("scrollback"), reveal: false)
        #expect(thinkingRows().count == 2)
        reducer.apply(.thinkingMode("off"), reveal: false)
        reducer.apply(.thinkingMode("activity-panel"), reveal: false)
        #expect(thinkingRows().count == 2)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "thinking_mode", args: ["off"], kwargs: [:]) == .thinkingMode("off"))
        #expect(PCodeHostSocketClient.decodeWelcome(["show_thinking": "off"]).showThinking == "off")
        #expect(PCodeHostSocketClient.decodeWelcome([:]).showThinking == nil)
    }

    @Test @MainActor func eventReducerHandlesUpsertsParentNestingDiffsAndThinking() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 760, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.showThinking = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Refactor parser", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.thinkingDelta("Checking files…"), reveal: false)
        reducer.apply(.thinking(text: "Checked files and found 2 bugs.", elapsedSeconds: 3.2), reveal: false)
        // Thinking upserts in place (still 1 thinking item)
        #expect(doc.items.filter { $0.kind == .thinking }.count == 1)

        // Child tool arrives BEFORE parent (orphan buffering), then parent arrives
        reducer.apply(.toolStarted(callID: "child-1", parentCallID: "parent-1", name: "read_file", detail: "a.py", command: "", purpose: ""), reveal: false)
        reducer.apply(.toolStarted(callID: "parent-1", parentCallID: "", name: "delegate", detail: "worker", command: "", purpose: ""), reveal: false)
        // Nesting is a raw-item property; grouped display folds the child into a window.
        let parentIdx = doc.indexOfItem(id: "call-parent-1")
        let rawParentIdx = doc.indexOfRawItem(id: "call-parent-1")
        let rawChildIdx = doc.indexOfRawItem(id: "call-child-1")
        #expect(parentIdx != nil && rawParentIdx != nil && rawChildIdx != nil)
        #expect(rawChildIdx == (rawParentIdx ?? -1) + 1)
        #expect(doc.rawItems[rawChildIdx!].depth == 1)

        // CommandOutput caps tail lines and upserts onto existing tool call
        let totalLines = PCodeTranscriptEventReducer.maxCommandOutputLines + 20
        let hugeOutput = (1...totalLines).map { "out_\($0)" }.joined(separator: "\n")
        reducer.apply(.commandOutput(callID: "parent-1", command: "make test", output: hugeOutput), reveal: false)
        if case .tool(let updatedCall, _) = doc.items[parentIdx!].payload {
            #expect(updatedCall.output.contains("out_\(totalLines)"))
            #expect(updatedCall.output.contains("earlier lines omitted"))
        } else {
            Issue.record("Expected updated tool call")
        }

        // ToolStarted + EditPreview + EditCompleted + ToolSummary coalesce into ONE collapsible diff row under `call-e1`
        reducer.apply(.toolStarted(callID: "e1", parentCallID: "", name: "edit_file", detail: "a.py", command: "", purpose: ""), reveal: false)
        reducer.apply(.editPreview(callID: "e1", path: "a.py", text: "+x = 1", kind: "edit"), reveal: false)
        reducer.apply(.editCompleted(callID: "e1", parentCallID: "", path: "a.py", operation: "edited", patch: "@@ -1 +1 @@\n-x = 0\n+x = 1", added: 1, removed: 1, truncated: false), reveal: false)
        reducer.apply(.toolSummary(callID: "e1", parentCallID: "", name: "edit_file", detail: "a.py", command: "", failed: false, elapsedSeconds: 0.04, error: "", result: "Edited a.py.", isBackgroundJob: false), reveal: false)
        #expect(doc.items.filter { $0.kind == .diff }.count == 1)
        #expect(doc.indexOfItem(id: "call-e1") != nil)
        if let idx = doc.indexOfItem(id: "call-e1"),
           case .diff(let diffPayload, let isExpanded) = doc.items[idx].payload
        {
            #expect(!isExpanded)
            #expect(diffPayload.toolName == "edit_file")
            #expect(diffPayload.added == 1 && diffPayload.removed == 1)
            #expect(diffPayload.patch.contains("+x = 1"))
            doc.toggleExpansionForAccessibility(id: "call-e1")
            #expect(doc.isItemExpanded(id: "call-e1"))
            #expect(doc.plainTextForCopy(at: idx).contains("+x = 1"))
        } else {
            Issue.record("Expected coalesced collapsible diff row for call-e1")
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

    @Test @MainActor func delegateCardNamesSubAgentAndRendersReportMarkdown() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let snap = PCodeTypography.shared.snapshot
        let call = PCodePeekToolCall(
            id: 1,
            name: "delegate_task",
            detail: "worker · auditing the cache",
            failed: false,
            elapsedSeconds: 12,
            command: "",
            output: "## Findings\n\n- **cache** is `stale`")

        let compact = PCodeToolCardView.attributedText(for: call, isExpanded: false, typography: snap, theme: theme)
        #expect(compact.string.hasPrefix("Sub-agent  worker · auditing the cache"))
        #expect(!compact.string.contains("delegate_task"))

        let expanded = PCodeToolCardView.attributedText(for: call, isExpanded: true, typography: snap, theme: theme)
        #expect(expanded.string.contains("Findings"))
        #expect(expanded.string.contains("cache is stale"))
        #expect(!expanded.string.contains("##"))
        #expect(!expanded.string.contains("**"))
        #expect(!expanded.string.contains("`"))
    }

    @Test @MainActor func jobWaitCardNamesTheShellCommandAndStreamsItsOutput() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let snap = PCodeTypography.shared.snapshot
        let call = PCodePeekToolCall(
            id: 1,
            name: "wait_for_job",
            detail: "j3",
            failed: false,
            elapsedSeconds: nil,
            command: "make e2e",
            output: "checks: 1/12\nchecks: 2/12",
            isRunning: true,
            startedAt: Date())

        let compact = PCodeToolCardView.attributedText(for: call, isExpanded: false, typography: snap, theme: theme)
        #expect(compact.string.hasPrefix("Wait for shell  j3 · make e2e"))
        // The newest line streams on a row of its own, under the command, while collapsed.
        #expect(compact.string.hasSuffix("\nlive · 2 lines  checks: 2/12"))
        #expect(!compact.string.contains("checks: 1/12"))
        #expect(!compact.string.contains("wait_for_job"))
        #expect(call.canExpand)

        let expanded = PCodeToolCardView.attributedText(for: call, isExpanded: true, typography: snap, theme: theme)
        #expect(expanded.string.contains("make e2e"))
        #expect(expanded.string.contains("checks: 2/12"))

        // The btw drawer redraws a running card when its output grows.
        let more = PCodePeekToolCall(
            id: 1, name: "wait_for_job", detail: "j3", failed: false, elapsedSeconds: nil,
            command: "make e2e", output: call.output + "\nchecks: 3/12", isRunning: true,
            startedAt: call.startedAt)
        #expect(PCodeTranscriptItemSync.signature(PCodeTranscriptItem(id: "w", payload: .tool(call, isExpanded: false)))
            != PCodeTranscriptItemSync.signature(PCodeTranscriptItem(id: "w", payload: .tool(more, isExpanded: false))))

        // A job the runtime could not name still says which one is waited on.
        let unknown = PCodePeekToolCall(
            id: 2, name: "wait_for_job", detail: "j9", failed: false, elapsedSeconds: nil, command: "")
        #expect(unknown.summaryText == "j9")
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
            PCodeTranscriptItem(id: "p1", payload: .prompt(label: "", text: "List files", isSide: false), spacingAfter: 10),
            PCodeTranscriptItem(id: "t1", payload: .tool(call, isExpanded: false), spacingAfter: 8),
        ], pinToBottom: false)

        #expect(doc.accessibilityRole() == .list)
        let children = (doc.accessibilityChildren() as? [PCodeTranscriptAccessibilityRowElement]) ?? []
        #expect(children.count == 2)
        // Unlabeled main prompts render only their text but still announce "You".
        #expect(children[0].accessibilityLabel() == "You")
        #expect(doc.plainTextForCopy(at: 0) == "List files")
        #expect(children[1].isExpandable)
        #expect(!doc.isItemExpanded(id: "t1"))

        // Pressing the expandable tool proxy toggles its expanded state.
        #expect(children[1].accessibilityPerformPress())
        #expect(doc.isItemExpanded(id: "t1"))
        #expect(!children[0].isAccessibilitySelectorAllowed(#selector(NSAccessibilityElement.accessibilityPerformPress)))
        #expect(children[1].isAccessibilitySelectorAllowed(#selector(NSAccessibilityElement.accessibilityPerformPress)))
    }

    @Test @MainActor func shortTranscriptWithBottomContentInsetDoesNotScrollTopOffscreen() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 800)
        scrollView.bottomContentInset = 140
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView

        doc.setItems([
            PCodeTranscriptItem(id: "p1", payload: .prompt(label: "You", text: "Hello", isSide: false), spacingAfter: 10),
        ], pinToBottom: true)
        scrollView.layoutSubtreeIfNeeded()

        #expect(scrollView.contentView.bounds.origin.y <= 0.5)

        // Trailing newline in command output must not overcount lines by 1.
        let call = PCodePeekToolCall(id: 1, name: "run", detail: "cmd", failed: false, elapsedSeconds: nil, command: "cmd", output: "a\nb\nc\n")
        #expect(PCodeToolCardView.expandableLineCount(for: call) == 3)

        // contentView(at:effectiveCharacterRange:) covers separator offsets.
        doc.setItems([
            PCodeTranscriptItem(id: "a", payload: .prompt(label: "You", text: "Hi", isSide: false), spacingAfter: 10),
            PCodeTranscriptItem(id: "b", payload: .prose(markdown: "Bye", referenceDefinitions: "", copyMarkdown: nil), spacingAfter: 10),
        ], pinToBottom: false)
        let sepIdx = doc.currentSearchIndex().entries[0].utf16End
        var outRange = NSRange(location: 0, length: 0)
        _ = doc.contentView(at: sepIdx, effectiveCharacterRange: &outRange)
        #expect(NSLocationInRange(sepIdx, outRange))
    }

    @Test @MainActor func runningCommandStreamsLiveOutputAndSettlesCleanly() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let snap = PCodeTypography.shared.snapshot
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 480)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        // Instant settles here are about folding, not the minimum time on screen.
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Run tests", isSide: false, runID: "r1"), reveal: true)
        reducer.apply(.toolStarted(
            callID: "c1",
            parentCallID: "",
            name: "shell",
            detail: "pytest tests/test_ui_surface.py",
            command: "pytest tests/test_ui_surface.py",
            purpose: "Run unit tests"), reveal: true)

        // Immediately visible as running and expandable before any output arrives.
        let idx = doc.indexOfItem(id: "call-c1")
        #expect(idx != nil)
        if let idx, case .tool(let runningCall, let expanded) = doc.items[idx].payload {
            #expect(runningCall.isRunning)
            #expect(runningCall.canExpand)
            #expect(!expanded)
            let compact = PCodeToolCardView.attributedText(for: runningCall, isExpanded: false, typography: snap, theme: theme)
            #expect(compact.string.contains("running"))
            let waiting = PCodeToolCardView.attributedText(for: runningCall, isExpanded: true, typography: snap, theme: theme)
            #expect(waiting.string.contains("Waiting for output"))
        } else {
            Issue.record("Expected running tool item for call-c1")
        }

        // Expand while running and stream live CommandOutput chunks.
        let children = (doc.accessibilityChildren() as? [PCodeTranscriptAccessibilityRowElement]) ?? []
        let toolProxy = children.first { $0.itemID == "call-c1" }
        #expect(toolProxy != nil)
        #expect(toolProxy?.accessibilityLabel()?.contains("running") == true)
        #expect(toolProxy?.accessibilityPerformPress() == true)
        #expect(doc.isItemExpanded(id: "call-c1"))

        reducer.apply(.commandOutput(callID: "c1", command: "pytest tests/test_ui_surface.py", output: "line 1\nline 2"), reveal: true)
        if let idx = doc.indexOfItem(id: "call-c1"), case .tool(let streamingCall, let expanded) = doc.items[idx].payload {
            #expect(streamingCall.isRunning)
            #expect(expanded)
            #expect(streamingCall.output.contains("line 2"))
            let collapsedLive = PCodeToolCardView.attributedText(for: streamingCall, isExpanded: false, typography: snap, theme: theme)
            #expect(collapsedLive.string.contains("live · 2 lines"))
        } else {
            Issue.record("Expected streaming tool item for call-c1")
        }

        // Settling via ToolSummary stops the running indicator and preserves final output.
        reducer.apply(.toolSummary(
            callID: "c1",
            parentCallID: "",
            name: "shell",
            detail: "pytest tests/test_ui_surface.py",
            command: "pytest tests/test_ui_surface.py",
            failed: false,
            elapsedSeconds: 1.2,
            error: "",
            result: "line 1\nline 2\n2 passed",
            isBackgroundJob: false), reveal: true)
        if let idx = doc.indexOfItem(id: "call-c1"), case .tool(let settledCall, let expanded) = doc.items[idx].payload {
            #expect(!settledCall.isRunning)
            #expect(expanded)
            #expect(settledCall.output.contains("2 passed"))
            #expect(settledCall.formattedElapsed == "1.2s")
        } else {
            Issue.record("Expected settled tool item for call-c1")
        }
    }

    @Test func steeringJournalRecordDecodesAsUserPrompt() {
        let event = PCodeHostEventDecoder.decodeJournalRecord([
            "kind": "steering", "run_id": "r1", "prompt": "also check the docs",
        ])
        #expect(event == .user(text: "also check the docs"))
        #expect(PCodeHostEventDecoder.decodeJournalRecord(["kind": "steering", "prompt": ""]) == nil)
    }

    @Test @MainActor func steeringMidTurnShowsPromptRowAndTurnContinues() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 480)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Fix it", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.message(markdown: "Looking."), reveal: false)
        reducer.apply(.toolStarted(callID: "c1", parentCallID: "", name: "shell", detail: "make", command: "make", purpose: ""), reveal: false)
        reducer.apply(.user(text: "also check the docs"), reveal: false)
        reducer.apply(.message(markdown: "Will do."), reveal: false)

        func promptIndex(_ text: String) -> Int? {
            doc.items.firstIndex {
                if case .prompt(_, let prompt, _) = $0.payload { return prompt == text }
                return false
            }
        }
        func proseIndex(_ text: String) -> Int? {
            doc.items.firstIndex {
                if case .prose(let markdown, _, _) = $0.payload { return markdown.contains(text) }
                return false
            }
        }
        guard let steer = promptIndex("also check the docs"), let reply = proseIndex("Will do.") else {
            Issue.record("Expected the steering prompt row and the reply after it")
            return
        }
        #expect(promptIndex("Fix it")! < steer)
        #expect(steer < reply)
        // A steering message is not a new turn: the running tool keeps running.
        if let idx = doc.indexOfItem(id: "call-c1"), case .tool(let call, _) = doc.items[idx].payload {
            #expect(call.isRunning)
        } else {
            Issue.record("Expected tool item c1")
        }
    }

    @Test @MainActor func turnEndedAndShellModeSettleRunningTools() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 480)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        // Instant settles here are about folding, not the minimum time on screen.
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        // Cancelled turn settles any still-running tool call as failed and not running.
        reducer.apply(.turnStarted(prompt: "Sleep", isSide: false, runID: "r2"), reveal: true)
        reducer.apply(.toolStarted(callID: "c2", parentCallID: "", name: "shell", detail: "sleep 60", command: "sleep 60", purpose: ""), reveal: true)
        reducer.apply(.turnEnded(status: .cancelled), reveal: true)
        if let idx = doc.indexOfItem(id: "call-c2"), case .tool(let call, _) = doc.items[idx].payload {
            #expect(!call.isRunning)
            #expect(call.failed)
        } else {
            Issue.record("Expected cancelled tool item c2 to settle")
        }

        // `!command` shell mode: user -> commandOutput -> dropOutput -> shellResult coalesces onto one row.
        reducer.apply(.user(text: "!echo hi"), reveal: true)
        reducer.apply(.commandOutput(callID: "shell_mode_abc", command: "echo hi", output: "hi"), reveal: true)
        reducer.apply(.dropOutput(callID: "shell_mode_abc"), reveal: true)
        reducer.apply(.shellResult(command: "echo hi", output: "hi\n", failed: false, elapsedSeconds: 0.1), reveal: true)
        if let idx = doc.indexOfItem(id: "call-shell_mode_abc"), case .tool(let call, _) = doc.items[idx].payload {
            #expect(!call.isRunning)
            #expect(!call.failed)
            #expect(call.output.contains("hi"))
        } else {
            Issue.record("Expected shell_mode_abc to settle in-place")
        }

        // Completed edit_file diff card must NOT be overwritten by settleRunningItems at turnEnded.
        reducer.apply(.turnStarted(prompt: "Edit file", isSide: false, runID: "r3"), reveal: true)
        reducer.apply(.toolStarted(callID: "e1", parentCallID: "", name: "edit_file", detail: "foo.py", command: "", purpose: ""), reveal: true)
        reducer.apply(.editCompleted(callID: "e1", parentCallID: "", path: "foo.py", operation: "edit", patch: "+a\n-b", added: 1, removed: 1, truncated: false), reveal: true)
        reducer.apply(.toolSummary(callID: "e1", parentCallID: "", name: "edit_file", detail: "foo.py", command: "", failed: false, elapsedSeconds: 0.3, error: "", result: nil, isBackgroundJob: false), reveal: true)
        reducer.apply(.turnEnded(status: .complete), reveal: true)
        if let idx = doc.indexOfItem(id: "call-e1"), case .diff(let payload, _) = doc.items[idx].payload {
            #expect(payload.path == "foo.py")
            #expect(payload.added == 1)
            #expect(!payload.isPreview)
        } else {
            Issue.record("Expected completed diff card e1 to survive turnEnded")
        }

        // Clearing EditPreview (empty path) removes the preview card cleanly.
        reducer.apply(.turnStarted(prompt: "Preview", isSide: false, runID: "r4"), reveal: true)
        reducer.apply(.editPreview(callID: "edit-preview:0", path: "bar.py", text: "+x", kind: "edit"), reveal: true)
        #expect(doc.indexOfItem(id: "call-edit-preview:0") != nil)
        reducer.apply(.editPreview(callID: "edit-preview:0", path: "", text: "", kind: "edit"), reveal: true)
        #expect(doc.indexOfItem(id: "call-edit-preview:0") == nil)

        // Expanding a running silent shell command auto-collapses when it settles with no output.
        reducer.apply(.toolStarted(callID: "s1", parentCallID: "", name: "shell", detail: "sleep 1", command: "sleep 1", purpose: ""), reveal: true)
        let children = (doc.accessibilityChildren() as? [PCodeTranscriptAccessibilityRowElement]) ?? []
        #expect(children.first { $0.itemID == "call-s1" }?.accessibilityPerformPress() == true)
        #expect(doc.isItemExpanded(id: "call-s1"))
        reducer.apply(.toolSummary(callID: "s1", parentCallID: "", name: "shell", detail: "sleep 1", command: "sleep 1", failed: false, elapsedSeconds: 1.0, error: "", result: "", isBackgroundJob: false), reveal: true)
        #expect(!doc.isItemExpanded(id: "call-s1"))
    }

    // MARK: - Copy

    @MainActor private func commandKey(_ characters: String, keyCode: UInt16) -> NSEvent {
        NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0, windowNumber: 0,
            context: nil, characters: characters, charactersIgnoringModifiers: characters,
            isARepeat: false, keyCode: keyCode)!
    }

    @Test @MainActor func commandCCopiesSelectionWithoutAMenuShortcut() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let pasteboard = NSPasteboard.withUniqueName()
        defer { pasteboard.releaseGlobally() }
        doc.pasteboard = pasteboard

        doc.beginStreamingTurn(prompt: "Explain")
        doc.beginStreamingMessage(id: "msg-1")
        doc.appendStreamingDelta("First paragraph with **bold** words.\n\n```swift\nlet x = 42\n```\n\n")
        doc.finishStreamingMessage()

        // No selection: Cmd+C leaves the pasteboard alone.
        doc.keyDown(with: commandKey("c", keyCode: 8))
        #expect(pasteboard.string(forType: .string) == nil)

        let rendered = doc.plainTextForCopy(at: 1) as NSString
        let range = rendered.range(of: "paragraph with bold")
        #expect(range.location != NSNotFound)
        doc.setSelection(PCodeTranscriptSelection(
            anchor: .init(itemIndex: 1, utf16Offset: range.location),
            focus: .init(itemIndex: 1, utf16Offset: NSMaxRange(range))))
        doc.keyDown(with: commandKey("c", keyCode: 8))
        #expect(pasteboard.string(forType: .string) == "paragraph with bold")

        // Cmd+A then Cmd+C takes every block, prose into code.
        doc.keyDown(with: commandKey("a", keyCode: 0))
        doc.keyDown(with: commandKey("c", keyCode: 8))
        let all = pasteboard.string(forType: .string) ?? ""
        #expect(all.contains("First paragraph with bold words."))
        #expect(all.contains("let x = 42"))
    }

    @Test @MainActor func everyProseBlockOfAnAnswerOffersItsMarkdown() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let pasteboard = NSPasteboard.withUniqueName()
        defer { pasteboard.releaseGlobally() }
        doc.pasteboard = pasteboard

        let markdown = "Intro **paragraph**.\n\n```swift\nlet x = 42\n```\n\n- one\n- two\n"
        doc.beginStreamingTurn(prompt: "Explain")
        doc.beginStreamingMessage(id: "msg-1")
        doc.appendStreamingDelta(markdown)
        doc.finishStreamingMessage()

        var proseCount = 0
        for item in doc.items {
            guard case .prose(_, _, let copyMarkdown) = item.payload else { continue }
            proseCount += 1
            #expect(copyMarkdown == markdown)
        }
        #expect(proseCount == 2)

        // Hovering any of them shows its button, which copies the whole answer.
        let rows = doc.subviews.compactMap { $0 as? PCodeProseRowView }
        #expect(rows.count == 2)
        for row in rows {
            let button = row.subviews.compactMap { $0 as? NSButton }.first
            #expect(button?.isHidden == true)
            row.setHovered(true)
            #expect(button?.isHidden == false)
            pasteboard.clearContents()
            button?.performClick(nil)
            #expect(pasteboard.string(forType: .string) == markdown)
        }

        // Saved turns flattened for Peek carry it the same way.
        let flattened = PCodeTranscriptItem.flatten(turns: [
            PCodePeekTurn(id: 0, prompt: "Explain", blocks: [.markdown(id: 0, text: markdown)], status: .complete, side: false),
        ])
        let payloads = flattened.compactMap { item -> String? in
            guard case .prose(_, _, let copyMarkdown) = item.payload else { return nil }
            return copyMarkdown
        }
        #expect(payloads == [markdown, markdown])
    }

    @Test @MainActor func contextMenuOffersCopySelectAllAndTheAnswer() throws {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let pasteboard = NSPasteboard.withUniqueName()
        defer { pasteboard.releaseGlobally() }
        doc.pasteboard = pasteboard

        let markdown = "Only **paragraph**.\n"
        doc.beginStreamingTurn(prompt: "Explain")
        doc.beginStreamingMessage(id: "msg-1")
        doc.appendStreamingDelta(markdown)
        doc.finishStreamingMessage()

        // The prompt row has no answer to copy; the prose row does.
        #expect(doc.answerMarkdown(at: CGPoint(x: 40, y: 4)) == nil)
        let proseRect = try #require(doc.characterRects(forItemAt: 1, localRange: NSRange(location: 0, length: 4)).first)
        let prosePoint = CGPoint(x: proseRect.midX, y: proseRect.midY)
        #expect(doc.answerMarkdown(at: prosePoint) == markdown)
        // Blank space under the last row belongs to no answer.
        #expect(doc.answerMarkdown(at: CGPoint(x: prosePoint.x, y: doc.frame.height + 200)) == nil)

        let windowPoint = doc.convert(prosePoint, to: nil)
        let event = try #require(NSEvent.mouseEvent(
            with: .rightMouseDown, location: windowPoint, modifierFlags: [], timestamp: 0,
            windowNumber: 0, context: nil, eventNumber: 0, clickCount: 1, pressure: 1))
        let menu = try #require(doc.menu(for: event))
        #expect(menu.items.filter { !$0.isSeparatorItem }.map(\.title) == ["Copy", "Copy Answer as Markdown", "Select All", "Group Tool Calls", "Prompt Style"])
        let styles = try #require(menu.items.last?.submenu)
        #expect(styles.items.map(\.title) == PCodePromptStyle.allCases.map(\.title))
        #expect(styles.items.filter { $0.state == .on }.count == 1)
        let original = PCodeTranscriptPreferences.shared.promptStyle
        defer { PCodeTranscriptPreferences.shared.promptStyle = original }
        let barItem = try #require(styles.items.first { $0.representedObject as? String == "bar" })
        doc.setPromptStyleFromMenu(barItem)
        #expect(doc.promptStyle == .bar)

        // Copy is offered only with a selection; the other two always.
        #expect(!doc.validateUserInterfaceItem(menu.items[0]))
        #expect(doc.validateUserInterfaceItem(menu.items[1]))
        doc.selectAll(nil)
        #expect(doc.validateUserInterfaceItem(menu.items[0]))

        doc.copyAnswerMarkdown(menu.items[1])
        #expect(pasteboard.string(forType: .string) == markdown)
    }

    @Test @MainActor func composerFocusStillCopiesTheTranscriptSelection() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let composer = NSView()

        doc.beginStreamingTurn(prompt: "Explain")
        doc.appendStreamingDelta("A paragraph.\n")
        doc.finishStreamingMessage()

        // Nothing selected: Cmd+C stays with the composer.
        #expect(!scrollView.copiesSelectionForComposer(firstResponder: composer))

        doc.selectAll(nil)
        #expect(scrollView.copiesSelectionForComposer(firstResponder: composer))
        // The document view handles its own Cmd+C in keyDown.
        #expect(!scrollView.copiesSelectionForComposer(firstResponder: doc))
        // A selection in the composer wins.
        scrollView.composerHasSelection = { true }
        #expect(!scrollView.copiesSelectionForComposer(firstResponder: composer))
    }

    // MARK: - Tool Grouping (Collapsed Summary Above Active Tools)

    @Test func transcriptPreferencesDefaultsToGroupedAndPersists() {
        let suiteName = "com.pcode.test.prefs.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let prefs = PCodeTranscriptPreferences(defaults: defaults)
        #expect(prefs.groupTools == true)

        prefs.groupTools = false
        let reloaded = PCodeTranscriptPreferences(defaults: defaults)
        #expect(reloaded.groupTools == false)
    }

    @Test func promptStyleDefaultsToClassicAndPersists() {
        let suiteName = "com.pcode.test.prefs.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        #expect(PCodeTranscriptPreferences(defaults: defaults).promptStyle == .classic)
        PCodeTranscriptPreferences(defaults: defaults).promptStyle = .bar
        #expect(PCodeTranscriptPreferences(defaults: defaults).promptStyle == .bar)
        defaults.set("bogus", forKey: "PCodeTranscriptPromptStyle")
        #expect(PCodeTranscriptPreferences(defaults: defaults).promptStyle == .classic)
    }

    @Test @MainActor func promptStylesFrameTheRowDifferently() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let text = NSAttributedString(string: "Hello")
        func height(_ style: PCodePromptStyle) -> (CGFloat, PCodePromptRowView) {
            let row = PCodePromptRowView()
            let h = row.configure(
                attributedText: text, isSidePrompt: false, style: style,
                width: 400, theme: theme, selectedRange: nil)
            return (h, row)
        }
        let (classic, classicRow) = height(.classic)
        let (tinted, tintedRow) = height(.tinted)
        let (bar, barRow) = height(.bar)
        // Tighter padding per style: classic 10pt, tinted 8pt, bar 2pt.
        #expect(classic - tinted == 4)
        #expect(tinted - bar == 12)
        #expect(classicRow.layer?.borderWidth == 0)
        #expect(tintedRow.layer?.borderWidth == 1)
        #expect(barRow.layer?.backgroundColor == nil)
    }

    @Test @MainActor func toolGrouperFoldsSettledToolsAboveActiveRunningTools() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        // Instant settles here are about folding, not the minimum time on screen.
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Inspect repo", isSide: false, runID: "r1"), reveal: false)
        // Two tools start in parallel -> both show as full active running cards.
        reducer.apply(.toolStarted(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", purpose: ""), reveal: true)
        reducer.apply(.toolStarted(callID: "t2", parentCallID: "", name: "shell", detail: "pytest", command: "pytest", purpose: ""), reveal: true)

        #expect(doc.items.count == 3) // prompt + t1(running) + t2(running)
        #expect(doc.items[1].kind == .tool)
        #expect(doc.items[2].kind == .tool)

        // First tool finishes while second is still running -> t1 folds into group row above active t2.
        reducer.apply(.toolSummary(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.2, error: "", result: nil, isBackgroundJob: false), reveal: true)

        #expect(doc.items.count == 3) // prompt + toolGroup([t1]) + t2(running)
        #expect(doc.items[1].kind == .toolGroup)
        let groupID = doc.items[1].id
        if case .toolGroup(let payload) = doc.items[1].payload {
            #expect(payload.totalCount == 1)
            #expect(payload.plainSummaryText == "1 tool · 1 read")
            #expect(payload.absorbPulse == 1)
        } else {
            Issue.record("Expected toolGroup payload at index 1")
        }
        #expect(doc.items[2].id == "call-t2")
        if case .tool(let activeCall, _) = doc.items[2].payload {
            #expect(activeCall.isRunning == true)
        } else {
            Issue.record("Expected running tool at index 2")
        }

        // Second tool finishes -> folds into the SAME group row (stable ID, pulse incremented).
        reducer.apply(.toolSummary(callID: "t2", parentCallID: "", name: "shell", detail: "pytest", command: "pytest", failed: false, elapsedSeconds: 1.4, error: "", result: "ok", isBackgroundJob: false), reveal: true)

        #expect(doc.items.count == 2) // prompt + toolGroup([t1, t2])
        #expect(doc.items[1].id == groupID)
        if case .toolGroup(let payload) = doc.items[1].payload {
            #expect(payload.totalCount == 2)
            #expect(payload.plainSummaryText == "2 tools · 1 read · 1 shell")
            #expect(payload.absorbPulse == 2)
            #expect(payload.formattedTotalElapsed == "1.6s")
        } else {
            Issue.record("Expected updated toolGroup at index 1")
        }
    }

    @Test @MainActor func fastSettlingToolKeepsItsCardUntilMinimumTimeOnScreen() async throws {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        doc.minimumLiveCardSeconds = 0.2
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Inspect repo", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", purpose: ""), reveal: true)
        reducer.apply(.toolStarted(callID: "t2", parentCallID: "", name: "shell", detail: "pytest", command: "pytest", purpose: ""), reveal: true)
        reducer.apply(.toolSummary(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.01, error: "", result: nil, isBackgroundJob: false), reveal: true)

        // Settled at once, so its card stays (now finished) instead of folding straight away.
        #expect(doc.items.map(\.kind) == [.prompt, .tool, .tool])
        #expect(doc.items[1].id == "call-t1")
        if case .tool(let call, _) = doc.items[1].payload {
            #expect(call.isRunning == false)
        } else {
            Issue.record("Expected the settled t1 card at index 1")
        }

        try await Task.sleep(nanoseconds: 500_000_000)

        #expect(doc.items.map(\.kind) == [.prompt, .toolGroup, .tool])
        if case .toolGroup(let payload) = doc.items[1].payload {
            #expect(payload.totalCount == 1)
            #expect(payload.absorbPulse == 1)
        } else {
            Issue.record("Expected t1 folded into a group")
        }
        #expect(doc.items[2].id == "call-t2")
    }

    @Test @MainActor func heldToolCardIgnoresAnEarlierHoldsTimerAndWaitsWhileExpanded() async throws {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        doc.minimumLiveCardSeconds = 0.4
        let reducer = PCodeTranscriptEventReducer(documentView: doc)
        func start() {
            reducer.apply(.toolStarted(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", purpose: ""), reveal: true)
        }
        func settle() {
            reducer.apply(.toolSummary(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.01, error: "", result: nil, isBackgroundJob: false), reveal: true)
        }

        reducer.apply(.turnStarted(prompt: "Inspect repo", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "t2", parentCallID: "", name: "shell", detail: "pytest", command: "pytest", purpose: ""), reveal: true)
        start()
        settle() // First hold: its timer fires at ~0.4s.
        try await Task.sleep(nanoseconds: 250_000_000)
        start()
        settle() // Second hold, armed at ~0.25s: due at ~0.65s.

        try await Task.sleep(nanoseconds: 250_000_000) // ~0.5s: past the first hold's timer.
        #expect(doc.items.map(\.kind) == [.prompt, .tool, .tool])

        // Expanded by the user: the fold waits rather than pulling it out from under them.
        doc.toggleExpansionForAccessibility(id: "call-t1")
        try await Task.sleep(nanoseconds: 400_000_000) // ~0.9s
        #expect(doc.items.contains { $0.id == "call-t1" })

        doc.toggleExpansionForAccessibility(id: "call-t1")
        try await Task.sleep(nanoseconds: 600_000_000)
        #expect(doc.items.map(\.kind) == [.prompt, .toolGroup, .tool])
    }

    @Test @MainActor func toolGrouperKeepsLoneSettledToolUngrouped() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Read one file", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "solo", parentCallID: "", name: "read_file", detail: "README.md", command: "", purpose: ""), reveal: false)
        reducer.apply(.toolSummary(callID: "solo", parentCallID: "", name: "read_file", detail: "README.md", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.message(markdown: "Done."), reveal: false)

        #expect(doc.items.map(\.kind) == [.prompt, .tool, .prose])
    }

    @Test @MainActor func toolGrouperFoldsCompletedEditsAndReportsFailures() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Fix bug", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.editPreview(callID: "e1", path: "a.swift", text: "+line", kind: "edit"), reveal: false)
        reducer.apply(.editCompleted(callID: "e1", parentCallID: "", path: "a.swift", operation: "edit", patch: "+line", added: 1, removed: 0, truncated: false), reveal: false)
        reducer.apply(.toolSummary(callID: "e1", parentCallID: "", name: "edit_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.editCompleted(callID: "e2", parentCallID: "", path: "b.swift", operation: "edit", patch: "+line2", added: 1, removed: 0, truncated: false), reveal: false)
        reducer.apply(.toolSummary(callID: "e2", parentCallID: "", name: "edit_file", detail: "b.swift", command: "", failed: false, elapsedSeconds: 0.2, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "s1", parentCallID: "", name: "shell", detail: "make test", command: "make test", failed: true, elapsedSeconds: 0.9, error: "exit 1", result: "FAIL", isBackgroundJob: false), reveal: false)
        // Active edit preview stays below the settled group until it completes.
        reducer.apply(.editPreview(callID: "e3", path: "c.swift", text: "+wip", kind: "edit"), reveal: false)

        #expect(doc.items.count == 3) // prompt + toolGroup([e1, e2, s1]) + e3(preview)
        #expect(doc.items[1].kind == .toolGroup)
        #expect(doc.items[2].kind == .diff)
        if case .toolGroup(let payload) = doc.items[1].payload {
            #expect(payload.totalCount == 3)
            #expect(payload.succeededCount == 2)
            #expect(payload.failedCount == 1)
            #expect(payload.plainSummaryText == "2 ok · 1 failed · 2 edits · 1 shell (1 failed)")
        } else {
            Issue.record("Expected toolGroup payload")
        }
    }

    @Test func toolSummaryExitCodeDecodesAsIntegerOnly() throws {
        // Fields arrive as `JSONSerialization` output, where a bool is an NSNumber too.
        let json = #"{"nonzero": {"name": "shell", "failed": false, "exit_code": 1},"#
            + #" "hard": {"name": "shell", "failed": true, "exit_code": 127},"#
            + #" "legacy": {"name": "shell", "failed": true},"#
            + #" "boolish": {"name": "shell", "failed": false, "exit_code": true}}"#
        let records = try #require(
            JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: [String: Any]])
        func decoded(_ key: String, kind: String = "ToolSummary") -> (failed: Bool, exitCode: Int?)? {
            guard case .toolSummary(_, _, _, _, _, let failed, _, _, _, _, let exitCode)? =
                PCodeHostEventDecoder.decodeRuntimeEvent(kind: kind, fields: records[key] ?? [:])
            else { return nil }
            return (failed, exitCode)
        }
        #expect(decoded("nonzero")! == (false, 1))
        #expect(decoded("nonzero", kind: "JobFinished")! == (false, 1))
        #expect(decoded("hard")! == (true, 127))
        #expect(decoded("legacy")! == (true, nil))
        #expect(decoded("boolish")! == (false, nil))
        #expect(PCodePeekToolCall.exitCode(from: 1.5) == nil)
        #expect(PCodePeekToolCall.exitCode(from: "1") == nil)
        #expect(PCodePeekToolCall.exitCode(from: -9) == -9)
    }

    @Test func settledToolMarkSeparatesNonZeroExitFromFailure() {
        func call(failed: Bool, exitCode: Int?) -> PCodePeekToolCall {
            PCodePeekToolCall(
                id: 1, name: "shell", detail: "grep x", failed: failed, elapsedSeconds: 0.1,
                command: "grep x", exitCode: exitCode)
        }
        let nonzero = call(failed: false, exitCode: 1)
        #expect(nonzero.exitedNonZero)
        #expect(PCodeToolCardView.settledMark(for: nonzero).symbol == "circle.fill")

        let hard = call(failed: true, exitCode: 127)
        #expect(!hard.exitedNonZero)
        #expect(PCodeToolCardView.settledMark(for: hard).symbol == "xmark.circle.fill")

        // Older transcripts carry no exit code: `failed` alone decides, as before.
        #expect(PCodeToolCardView.settledMark(for: call(failed: true, exitCode: nil)).symbol == "xmark.circle.fill")
        #expect(PCodeToolCardView.settledMark(for: call(failed: false, exitCode: nil)).symbol == "checkmark.circle.fill")
        #expect(PCodeToolCardView.settledMark(for: call(failed: false, exitCode: 0)).symbol == "checkmark.circle.fill")
    }

    @Test func groupWindowRowGlyphSeparatesNonZeroExitFromFailure() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        func glyph(failed: Bool, exitCode: Int?) -> String {
            let call = PCodePeekToolCall(
                id: 1, name: "shell", detail: "grep x", failed: failed, elapsedSeconds: 0.1,
                command: "grep x", exitCode: exitCode)
            let entry = PCodeToolGroupEntry(id: "call-1", content: .tool(call), depth: 0)
            return PCodeToolGroupCardView.windowGlyph(for: entry, theme: theme).0
        }
        #expect(glyph(failed: false, exitCode: 1) == "•")
        #expect(glyph(failed: true, exitCode: 127) == "✗")
        #expect(glyph(failed: false, exitCode: 0) == "✓")
        #expect(glyph(failed: false, exitCode: nil) == "✓")
    }

    @Test @MainActor func nonZeroExitIsNotCountedAsFailureAndSurvivesReplay() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Search", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolSummary(callID: "s1", parentCallID: "", name: "shell", detail: "grep x", command: "grep x", failed: false, elapsedSeconds: 0.1, error: "", result: "", isBackgroundJob: false, exitCode: 1), reveal: false)
        reducer.apply(.toolSummary(callID: "s2", parentCallID: "", name: "shell", detail: "ls", command: "ls", failed: false, elapsedSeconds: 0.1, error: "", result: "a", isBackgroundJob: false, exitCode: 0), reveal: false)

        guard case .toolGroup(let payload) = doc.items[1].payload else {
            Issue.record("Expected toolGroup payload")
            return
        }
        #expect(payload.failedCount == 0)
        guard case .tool(let call) = payload.entries[0].content else {
            Issue.record("Expected tool entry")
            return
        }
        #expect(call.exitCode == 1 && call.exitedNonZero)

        let turn = PCodePeekTurn(id: 0, prompt: "Search", blocks: [.tool(call)], status: .complete, side: false)
        let steps = PCodeTranscriptStreamReplayer.buildSteps(from: [turn])
        #expect(steps.contains(.toolCall(name: "shell", detail: "grep x", command: "grep x", failed: false, exitCode: 1, elapsed: 0.1, id: call.id)))
    }

    @Test @MainActor func toolGrouperExcludesDelegatesAndBackgroundJobs() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Delegate work", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolSummary(callID: "d1", parentCallID: "", name: "delegate_task", detail: "audit", command: "", failed: false, elapsedSeconds: 2.0, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "c1", parentCallID: "d1", name: "read_file", detail: "x.py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "c2", parentCallID: "d1", name: "read_file", detail: "y.py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "bg1", parentCallID: "", name: "shell", detail: "build", command: "build", failed: false, elapsedSeconds: 5.0, error: "", result: nil, isBackgroundJob: true), reveal: false)

        // prompt + delegate(depth 0) + child toolGroup(depth 1) + background job(depth 0)
        #expect(doc.items.map(\.kind) == [.prompt, .tool, .toolGroup, .tool])
        #expect(doc.items[1].depth == 0)
        #expect(doc.items[2].depth == 1)
        #expect(doc.items[3].depth == 0)
    }

    @Test @MainActor func subAgentCallsStreamIntoAWindowThatNeverShrinks() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Delegate", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "d1", parentCallID: "", name: "delegate_task", detail: "audit", command: "", purpose: ""), reveal: true)
        reducer.apply(.message(markdown: "Waiting on the worker."), reveal: true)

        func window() -> PCodeToolGroupPayload? {
            guard let idx = doc.indexOfItem(id: "call-d1"), doc.items.indices.contains(idx + 1),
                  case .toolGroup(let payload) = doc.items[idx + 1].payload
            else { return nil }
            return payload
        }

        var shownRows = 0
        for n in 1...7 {
            reducer.apply(.toolStarted(callID: "c\(n)", parentCallID: "d1", name: "read_file", detail: "f\(n).py", command: "", purpose: ""), reveal: true)
            for settle in [false, true] {
                if settle {
                    reducer.apply(.toolSummary(callID: "c\(n)", parentCallID: "d1", name: "read_file", detail: "f\(n).py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: true)
                }
                guard let payload = window() else {
                    Issue.record("Expected a window right below the delegate after call \(n)")
                    return
                }
                #expect(payload.windowRows == PCodeToolGrouper.nestedWindowRows)
                #expect(payload.totalCount == n)
                #expect(payload.runningCount == (settle ? 0 : 1))
                // Rows only ever grow to the cap; the prose below never moves back up.
                #expect(payload.windowEntries.count >= shownRows)
                #expect(payload.windowEntries.count == min(n, PCodeToolGrouper.nestedWindowRows))
                #expect(payload.windowEntries.last?.id == "call-c\(n)")
                shownRows = payload.windowEntries.count
                // prompt + delegate + window + prose, whatever state the calls are in.
                #expect(doc.items.count == 4)
            }
        }

        // Expanding is the user's call, and lists every call below the summary.
        let windowID = doc.items[2].id
        doc.toggleExpansionForAccessibility(id: windowID)
        #expect(doc.items.count == 4 + 7)
        #expect(doc.items[3].id == "call-c1")
        #expect(doc.items[3].depth == 2)
        #expect(window()?.windowEntries.isEmpty == true)
        doc.toggleExpansionForAccessibility(id: windowID)
        #expect(doc.items.count == 4)
    }

    @Test @MainActor func expandedWindowSummaryTracksNonGroupableChildren() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Delegate", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "d1", parentCallID: "", name: "delegate_task", detail: "audit", command: "", purpose: ""), reveal: true)
        // A nested delegate is never groupable, so its updates once skipped re-projection.
        reducer.apply(.toolStarted(callID: "n1", parentCallID: "d1", name: "delegate_task", detail: "inner", command: "", purpose: ""), reveal: true)
        doc.toggleExpansionForAccessibility(id: doc.items[2].id)
        #expect(doc.indexOfItem(id: "call-n1") != nil)

        reducer.apply(.toolSummary(callID: "n1", parentCallID: "d1", name: "delegate_task", detail: "inner", command: "", failed: false, elapsedSeconds: 1.0, error: "", result: nil, isBackgroundJob: false), reveal: true)
        guard case .toolGroup(let payload) = doc.items[2].payload else {
            Issue.record("Expected the window below the delegate")
            return
        }
        #expect(payload.runningCount == 0)
        #expect(payload.succeededCount == 1)
    }

    @Test @MainActor func toolGroupExpandCollapseAndLivePreferenceToggle() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 400)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.groupTools = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Run tools", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolSummary(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "t2", parentCallID: "", name: "shell", detail: "ls", command: "ls", failed: false, elapsedSeconds: 0.2, error: "", result: "a.swift", isBackgroundJob: false), reveal: false)

        #expect(doc.items.count == 2) // prompt + toolGroup
        let groupID = doc.items[1].id

        // Expand group -> children appear indented directly below the group header.
        doc.toggleExpansionForAccessibility(id: groupID)
        #expect(doc.items.count == 4) // prompt + toolGroup + t1 + t2
        #expect(doc.items[2].id == "call-t1")
        #expect(doc.items[2].depth == 1)
        #expect(doc.items[3].id == "call-t2")
        #expect(doc.items[3].depth == 1)

        // Collapse group -> back to single summary row.
        doc.toggleExpansionForAccessibility(id: groupID)
        #expect(doc.items.count == 2)

        // Toggle flat layout option -> shows all individual tools without group row.
        doc.groupTools = false
        #expect(doc.items.map(\.kind) == [.prompt, .tool, .tool])
        #expect(doc.items[1].depth == 0)
        #expect(doc.items[2].depth == 0)

        // Toggle back to grouped layout -> folds cleanly again.
        doc.groupTools = true
        #expect(doc.items.map(\.kind) == [.prompt, .toolGroup])
    }

    @Test @MainActor func expandedToolGroupAbsorbPreservesRevisionsAndSelectionRemapsAcrossExpand() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 720, height: 500)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.groupTools = true
        // Instant settles here are about folding, not the minimum time on screen.
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Inspect", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolSummary(callID: "t1", parentCallID: "", name: "read_file", detail: "a.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "t2", parentCallID: "", name: "read_file", detail: "b.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolStarted(callID: "t3", parentCallID: "", name: "shell", detail: "ls", command: "ls", purpose: ""), reveal: false)

        // Expand the group while t3 is running, then finish t3 (oldMid.count == newMid.count == 4).
        let groupID = doc.items[1].id
        doc.toggleExpansionForAccessibility(id: groupID)
        #expect(doc.items.count == 5) // prompt + group + t1 + t2 + t3(running)
        let t1RevBefore = doc.items[2].revision

        reducer.apply(.toolSummary(callID: "t3", parentCallID: "", name: "shell", detail: "ls", command: "ls", failed: false, elapsedSeconds: 0.2, error: "", result: "ok", isBackgroundJob: false), reveal: true)
        // t1's revision must stay synced with rawItems (no revision drift).
        #expect(doc.items[2].revision == t1RevBefore)
        if let rawIdx = doc.indexOfRawItem(id: "call-t1") {
            #expect(doc.items[2].revision == doc.rawItems[rawIdx].revision)
        }

        // Collapse group, append prose, select part of prose, then expand group above it:
        // selection must remap onto the prose row instead of shifting onto a tool row.
        doc.toggleExpansionForAccessibility(id: groupID)
        reducer.apply(.message(markdown: "Final answer text."), reveal: false)
        guard let proseIdxBefore = doc.items.firstIndex(where: { $0.kind == .prose }) else {
            Issue.record("Expected prose item")
            return
        }
        doc.setSelection(PCodeTranscriptSelection(
            anchor: .init(itemIndex: proseIdxBefore, utf16Offset: 0),
            focus: .init(itemIndex: proseIdxBefore, utf16Offset: 5)))
        #expect(doc.selectedPlainText() == "Final")

        doc.toggleExpansionForAccessibility(id: groupID)
        #expect(doc.selectedPlainText() == "Final")
        doc.toggleExpansionForAccessibility(id: groupID)
        #expect(doc.selectedPlainText() == "Final")
    }

    @Test @MainActor func outOfOrderParallelCompletionAndCustomParentToolGrouping() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true
        // Instant settles here are about folding, not the minimum time on screen.
        doc.minimumLiveCardSeconds = 0
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        reducer.apply(.turnStarted(prompt: "Parallel", isSide: false, runID: "r1"), reveal: false)
        reducer.apply(.toolStarted(callID: "p1", parentCallID: "", name: "shell", detail: "slow", command: "slow", purpose: ""), reveal: true)
        reducer.apply(.toolStarted(callID: "p2", parentCallID: "", name: "read_file", detail: "fast.swift", command: "", purpose: ""), reveal: true)

        // p2 finishes before p1 -> p2 folds into group above still-running p1.
        reducer.apply(.toolSummary(callID: "p2", parentCallID: "", name: "read_file", detail: "fast.swift", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: true)
        #expect(doc.items.map(\.kind) == [.prompt, .toolGroup, .tool])
        #expect(doc.items[2].id == "call-p1")
        let stableGroupID = doc.items[1].id

        // p1 finishes -> folds into the SAME group ID.
        reducer.apply(.toolSummary(callID: "p1", parentCallID: "", name: "shell", detail: "slow", command: "slow", failed: false, elapsedSeconds: 1.0, error: "", result: nil, isBackgroundJob: false), reveal: true)
        #expect(doc.items.map(\.kind) == [.prompt, .toolGroup])
        #expect(doc.items[1].id == stableGroupID)

        // Non-delegate parent tool with nested depth-1 children stays ungrouped so children aren't orphaned.
        reducer.apply(.message(markdown: "Running workflow."), reveal: false)
        reducer.apply(.toolSummary(callID: "wf", parentCallID: "", name: "run_workflow", detail: "audit", command: "", failed: false, elapsedSeconds: 1.0, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "wc1", parentCallID: "wf", name: "read_file", detail: "x.py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)
        reducer.apply(.toolSummary(callID: "wc2", parentCallID: "wf", name: "read_file", detail: "y.py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false), reveal: false)

        let tailKinds = Array(doc.items.suffix(2).map(\.kind))
        #expect(tailKinds == [.tool, .toolGroup])
        #expect(doc.items[doc.items.count - 2].id == "call-wf")
        #expect(doc.items[doc.items.count - 1].depth == 1)
    }

    /// A long conversation's settled history, with grouped tools, nesting, thinking and prose.
    private static func sampleHistory(turns: Int) -> [PCodeHostEvent] {
        var events: [PCodeHostEvent] = []
        for turn in 0..<turns {
            events.append(.turnStarted(prompt: "Turn \(turn)", isSide: false, runID: "r\(turn)"))
            events.append(.thinking(text: "Considering turn \(turn)", elapsedSeconds: 1))
            for call in 0..<4 {
                let id = "t\(turn)-\(call)"
                events.append(.toolStarted(callID: id, parentCallID: "", name: "read_file", detail: "f\(call).py", command: "", purpose: ""))
                events.append(.toolSummary(callID: id, parentCallID: "", name: "read_file", detail: "f\(call).py", command: "", failed: false, elapsedSeconds: 0.1, error: "", result: nil, isBackgroundJob: false))
            }
            events.append(.message(markdown: "Answer \(turn).\n\nA second paragraph that wraps " + String(repeating: "word ", count: 40)))
            events.append(.turnEnded(status: turn % 7 == 3 ? .cancelled : .complete))
        }
        // A tool the history leaves running settles as it loads.
        events.append(.toolStarted(callID: "dangling", parentCallID: "", name: "shell", detail: "sleep", command: "sleep", purpose: ""))
        return events
    }

    @Test @MainActor func batchedHistoryLoadMatchesEventByEventReplay() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        func makeDoc() -> PCodeTranscriptDocumentView {
            let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
            doc.groupTools = true
            doc.showThinking = true
            return doc
        }
        let events = Self.sampleHistory(turns: 12)

        let stepped = makeDoc()
        let steppedReducer = PCodeTranscriptEventReducer(documentView: stepped)
        for event in events {
            steppedReducer.apply(event, reveal: false, isLive: false)
        }
        // What loading also does at the end: settle the dangling tool.
        steppedReducer.apply(.turnStarted(prompt: "next", isSide: false, runID: ""), reveal: false, isLive: false)

        let batched = makeDoc()
        let batchedReducer = PCodeTranscriptEventReducer(documentView: batched)
        // Whatever was shown before is replaced.
        batched.setItems([PCodeTranscriptItem(id: "stale", payload: .status(.failed), spacingAfter: 0)], pinToBottom: false)
        batchedReducer.loadHistory(events)
        batchedReducer.apply(.turnStarted(prompt: "next", isSide: false, runID: ""), reveal: false, isLive: false)

        #expect(batched.items.map(\.id) == stepped.items.map(\.id))
        #expect(batched.items.map(\.kind) == stepped.items.map(\.kind))
        #expect(batched.rawItems.map(\.id) == stepped.rawItems.map(\.id))
        #expect(batched.layoutEngine.entries.count == batched.items.count)
        #expect(!batched.items.contains { $0.id == "stale" })
        #expect(batched.stickyBottom.mode == .pinnedToBottom)
    }

    @Test @MainActor func hostHistoryNotifyDecodesJournalRecords() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "history",
            args: [[["kind": "turn_started", "prompt": "Hi"], ["kind": "RunStatus"], ["kind": "Message", "markdown": "Yo"]]],
            kwargs: [:])
        #expect(event == .history([.turnStarted(prompt: "Hi", isSide: false, runID: ""), .message(markdown: "Yo")]))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "history", args: [], kwargs: [:]) == nil)

        let line = Data(#"{"version": 1, "time": "2026-01-01T00:00:00+00:00", "kind": "RunStatus", "text": "x"}"#.utf8)
        #expect(PCodeLiveTranscriptController.sniffJournalKind(line) == "RunStatus")
        #expect(PCodeLiveTranscriptController.sniffJournalKind(Data("{}".utf8)) == nil)
        // At the call site a line is a slice of the whole journal, indexed from its offset.
        let journal = Data("{\"kind\": \"Message\"}\n".utf8) + line
        #expect(PCodeLiveTranscriptController.sniffJournalKind(journal[20...]) == "RunStatus")
    }

    @Test func diskPreloadKeepsOnlyTheSelectedTreeBranch() throws {
        func line(_ record: [String: Any]) throws -> String {
            var record = record
            record["version"] = 1
            return String(decoding: try JSONSerialization.data(withJSONObject: record), as: UTF8.self)
        }
        func turn(_ id: String, parent: Any?, _ text: String) throws -> [String] {
            var start: [String: Any] = ["kind": "turn_started", "run_id": id, "prompt": "ask \(text)"]
            if let parent { start["parent_id"] = parent }
            return [
                try line(start),
                try line(["kind": "Message", "run_id": id, "markdown": text]),
                try line(["kind": "turn_completed", "run_id": id]),
            ]
        }
        func messages(_ journal: [String]) -> [String] {
            let data = Data((journal.joined(separator: "\n") + "\n").utf8)
            return PCodeLiveTranscriptController.settledEvents(inJournal: data).events.compactMap {
                if case .message(let markdown) = $0 { return markdown }
                return nil
            }
        }

        // Linear, and an old journal with no parent links: everything.
        let linear = try turn("a", parent: NSNull(), "A") + turn("b", parent: "a", "B")
        #expect(messages(linear) == ["A", "B"])
        #expect(messages(try turn("a", parent: nil, "A") + turn("b", parent: nil, "B")) == ["A", "B"])
        // `/tree` went back to `a` and branched: `b` is off the selected path.
        let branched = linear + [try line(["kind": "tree_selected", "node_id": "a"])]
            + (try turn("c", parent: "a", "C"))
        #expect(messages(branched) == ["A", "C"])
        // Selecting `b` again shows that branch instead.
        #expect(messages(branched + [try line(["kind": "tree_selected", "node_id": "b"])]) == ["A", "B"])
        // A compaction starts a new root the next turn hangs off.
        let compacted = linear + [try line(["kind": "compaction_checkpoint", "node_id": "k", "parent_id": "b", "messages": []])]
            + (try turn("d", parent: "k", "D"))
        #expect(messages(compacted) == ["A", "B", "D"])
    }

    @Test func dockCardIgnoresTheFullPaneGridStillReportedAfterReady() {
        let card = PCodeDock.cardHeight(rows: 4, cell: 20)
        // A stale full-pane grid of 20 rows would add 20 rows of padding.
        #expect(PCodeDock.cardHeight(rows: 4, cell: 20, surfaceRows: 20) == card)
        // A grid a row short of `gridRows` is the card's own, with tall padding.
        #expect(PCodeDock.cardHeight(rows: 4, cell: 20, surfaceRows: PCodeDock.gridRows - 1) == card + 20)
    }

    @Test func hostWelcomeRedrawsOnlyWhatDiffersFromThePreload() {
        let history = Self.sampleHistory(turns: 3)
        func redraw(_ shown: [PCodeHostEvent]?, _ thinking: Bool? = nil) -> Bool {
            PCodeLiveTranscriptController.welcomeNeedsRedraw(
                shown: shown, welcome: history, showThinking: thinking, shownThinking: true)
        }
        #expect(!redraw(history))  // The disk preload already drew it.
        #expect(!redraw(history, true))
        #expect(redraw(history, false))  // Thinking visibility changes every row.
        #expect(redraw(nil))  // Live events were drawn on top.
        #expect(redraw(Array(history.dropLast())))  // The journal grew meanwhile.
    }

    @Test @MainActor func loadedHistoryLandsOnTheTrueBottomWithNoBlankSlack() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 760, height: 600)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.groupTools = true
        doc.showThinking = true
        let reducer = PCodeTranscriptEventReducer(documentView: doc)

        func expectAtTrueBottom(_ when: Comment) {
            let clip = scrollView.contentView
            let viewport = clip.bounds.height - scrollView.contentInsets.top - scrollView.contentInsets.bottom
            // No slack: the document is exactly the content (or the viewport, when shorter)...
            #expect(abs(doc.frame.height - max(doc.layoutEngine.totalHeight, viewport)) <= 0.5, when)
            // ...and the clip shows its end.
            let bottomY = PCodeStickyBottomState.bottomOriginY(
                documentHeight: doc.frame.height,
                clipHeight: clip.bounds.height,
                topInset: scrollView.contentInsets.top,
                bottomInset: scrollView.contentInsets.bottom)
            #expect(abs(clip.bounds.origin.y - bottomY) <= 0.5, when)
        }

        reducer.loadHistory(Self.sampleHistory(turns: 40))
        #expect(doc.layoutEngine.totalHeight > 2000)
        expectAtTrueBottom("after loading")

        // AppKit's overscan then measures rows above the viewport; their corrections must
        // move the origin with them instead of opening a gap under the last row.
        let visible = doc.visibleRect
        doc.prepareContent(in: visible.insetBy(dx: 0, dy: -visible.height * 2))
        expectAtTrueBottom("after measuring rows above")

        // Rewrapping at a new width settles on the new bottom too.
        scrollView.frame.size.width = 520
        scrollView.layoutSubtreeIfNeeded()
        expectAtTrueBottom("after a resize")
    }

    @Test @MainActor func btwItemSyncDiffsAgainstRawItemsWhenGrouped() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        doc.groupTools = true

        let t1 = PCodeTranscriptItem(id: "t1", kind: .tool, plainText: "read_file", secondaryText: "a.py")
        let t2 = PCodeTranscriptItem(id: "t2", kind: .tool, plainText: "shell", secondaryText: "ls")
        let prose = PCodeTranscriptItem(id: "p1", kind: .prose, plainText: "Done")

        PCodeTranscriptItemSync.apply([t1, t2], to: doc)
        #expect(doc.rawItems.count == 2)
        #expect(doc.items.map(\.kind) == [.toolGroup])

        // Re-applying with an appended prose row must not reset or duplicate tools.
        PCodeTranscriptItemSync.apply([t1, t2, prose], to: doc)
        #expect(doc.rawItems.count == 3)
        #expect(doc.items.map(\.kind) == [.toolGroup, .prose])
    }

    @Test func hostRecoveryActionCapsRetriesAndGuardsConflicts() {
        // Trust prompt / held error: keep waiting automatically, but manual Reopen still relaunches.
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: true, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == .keepWaiting)
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: true, needsInput: true, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == .relaunched(attempt: 1, maxAttempts: 2))

        // Live host still booting within grace: keep waiting; past grace: relaunch.
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: false, hostEntryAlive: true, unavailableDuration: 12, openElsewhere: false
        ) == .keepWaiting)
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: false, hostEntryAlive: true, unavailableDuration: 35, openElsewhere: false
        ) == .relaunched(attempt: 1, maxAttempts: 2))

        // Open in another terminal without a host: first automatic sighting waits (transient lock),
        // second consecutive sighting (or manual Reopen) fails without forking.
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: true, sawOpenElsewhere: false
        ) == .keepWaiting)
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: true, sawOpenElsewhere: true
        ) == .failed("This session is open in another terminal."))
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: true, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: true, sawOpenElsewhere: false
        ) == .failed("This session is open in another terminal."))

        // Capped retries, then exhausted (nil -> caller attaches host log tail); manual resets.
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 0, isManual: false, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == .relaunched(attempt: 1, maxAttempts: 2))
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 1, isManual: false, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == .relaunched(attempt: 2, maxAttempts: 2))
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 2, isManual: false, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == nil)
        #expect(PCodeWorkspace.hostRecoveryAction(
            attempts: 2, isManual: true, needsInput: false, hostEntryAlive: false, unavailableDuration: 15, openElsewhere: false
        ) == .relaunched(attempt: 1, maxAttempts: 2))
    }

    @Test func hostLogTailSkipsServingBannerAndReturnsErrorTail() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pcode-log-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let benign = dir.appendingPathComponent("aaaa1111.log")
        try "serving /tmp/aaaa1111.sock\n".write(to: benign, atomically: true, encoding: .utf8)
        #expect(PCodeSessionInfo.hostLogTail(hostID: "aaaa1111", in: dir) == nil)

        let crash = dir.appendingPathComponent("bbbb2222.log")
        try "serving /tmp/bbbb2222.sock\nTraceback:\nValueError: missing key\n".write(
            to: crash, atomically: true, encoding: .utf8)
        #expect(PCodeSessionInfo.hostLogTail(hostID: "bbbb2222", in: dir) == "Traceback:\nValueError: missing key")
        #expect(PCodeSessionInfo.hostLogTail(hostID: "missing0", in: dir) == nil)

        // Large log (> 8 KB) with multi-byte UTF-8 still decodes cleanly without mojibake.
        let large = dir.appendingPathComponent("cccc3333.log")
        let filler = String(repeating: "éééééééééé\n", count: 900)
        try (filler + "Fatal: café broke\n").write(to: large, atomically: true, encoding: .utf8)
        let largeTail = PCodeSessionInfo.hostLogTail(hostID: "cccc3333", in: dir, maxLines: 1)
        #expect(largeTail == "Fatal: café broke")
    }

    @Test @MainActor func liveTranscriptControllerAutoRelaunchesThenShowsFailureAndManualReopen() {
        let theme = PCodeTranscriptTheme.make(background: .black, foreground: .white)
        let doc = PCodeTranscriptDocumentView(typography: PCodeTypography.shared.snapshot, theme: theme)
        var currentTime = Date(timeIntervalSinceReferenceDate: 1000)
        var outcomes: [PCodeLiveTranscriptController.RelaunchOutcome] = [
            .relaunched(attempt: 1, maxAttempts: 2),
            .failed("ValueError: bad config"),
            .relaunched(attempt: 1, maxAttempts: 2),
        ]
        var calls: [Bool] = []
        let controller = PCodeLiveTranscriptController(
            retryInterval: 0.01,
            hostStartTimeout: 5,
            hostStoppedTimeout: 3,
            now: { currentTime },
            resolveSocketPath: { _, _ in nil })
        controller.onRequestRelaunch = { isManual, _ in
            calls.append(isManual)
            return outcomes.removeFirst()
        }

        controller.attach(sessionID: "sess-retry-test", documentView: doc)
        #expect(controller.connectionState == .starting)

        // Advance past hostStartTimeout -> triggers automatic relaunch.
        currentTime = currentTime.addingTimeInterval(6)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        #expect(calls == [false])
        #expect(controller.connectionState == .relaunching(attempt: 1, maxAttempts: 2))
        #expect(!controller.connectionState.showsAction)

        // Advance past hostStartTimeout again -> retries exhausted, shows fallback card with Reopen.
        currentTime = currentTime.addingTimeInterval(6)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        #expect(calls == [false, false])
        #expect(controller.connectionState == .failedToStart("ValueError: bad config"))
        #expect(controller.connectionState.showsAction)

        // Manual Reopen button triggers manual relaunch.
        controller.reopenHost()
        #expect(calls == [false, false, true])
        #expect(controller.connectionState == .relaunching(attempt: 1, maxAttempts: 2))
        controller.detach()

        // Brand-new 0-turn pane (sessionID == nil, hostID == nil) still times out and triggers relaunch.
        var freshCalls = 0
        let freshController = PCodeLiveTranscriptController(
            retryInterval: 0.01,
            hostStartTimeout: 5,
            hostStoppedTimeout: 3,
            now: { currentTime },
            resolveSocketPath: { _, _ in nil })
        freshController.onRequestRelaunch = { _, _ in
            freshCalls += 1
            return .failed("Host exited")
        }
        freshController.attach(sessionID: nil, hostID: nil, documentView: doc)
        currentTime = currentTime.addingTimeInterval(6)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        #expect(freshCalls == 1)
        #expect(freshController.connectionState == .failedToStart("Host exited"))
        freshController.detach()
    }

    // MARK: - Pause-Away & Return-to-Pane Dwell

    @Test func stickyBottomPauseAwayAndResumingDwellStateMachine() {
        var state = PCodeStickyBottomState()
        #expect(state.mode == .pinnedToBottom)
        #expect(!state.showsJumpToBottomWhenUnread)

        // Leaving while pinned freezes in `.pausedAway`.
        state.didLeavePane()
        #expect(state.mode == .pausedAway)
        #expect(!state.shouldProgrammaticallyPin)
        #expect(state.showsJumpToBottomWhenUnread)

        // Returning with nothing below the fold resumes `.pinnedToBottom` immediately.
        #expect(state.didReturnToPane(distanceFromBottom: 4) == .pinnedToBottom)
        #expect(state.shouldProgrammaticallyPin)

        // Leaving again and returning with unread content below the fold enters `.resumingDwell`.
        state.didLeavePane()
        #expect(state.mode == .pausedAway)
        #expect(state.didReturnToPane(distanceFromBottom: 140) == .resumingDwell)
        #expect(!state.shouldProgrammaticallyPin)
        #expect(state.showsJumpToBottomWhenUnread)

        // Leaving mid-dwell returns to `.pausedAway`.
        state.didLeavePane()
        #expect(state.mode == .pausedAway)

        // Returning again enters `.resumingDwell`; finishing dwell resumes `.pinnedToBottom`.
        #expect(state.didReturnToPane(distanceFromBottom: 140) == .resumingDwell)
        state.didFinishResumingDwell()
        #expect(state.mode == .pinnedToBottom)

        // Instant bailout on live scroll during dwell.
        state.didLeavePane()
        #expect(state.didReturnToPane(distanceFromBottom: 140) == .resumingDwell)
        state.didStartLiveScroll()
        #expect(state.mode == .userScrolledUp)
        state.didEndLiveScroll(distanceFromBottom: 120)
        #expect(state.mode == .userScrolledUp)

        // Leaving and returning while already `.userScrolledUp` never auto-resumes.
        state.didLeavePane()
        #expect(state.mode == .userScrolledUp)
        #expect(state.didReturnToPane(distanceFromBottom: 0) == .userScrolledUp)
        #expect(state.mode == .userScrolledUp)

        // Legacy wheel scroll during dwell bails out to `.userScrolledUp` unless landing at bottom.
        state.pinToBottom()
        state.didLeavePane()
        #expect(state.didReturnToPane(distanceFromBottom: 120) == .resumingDwell)
        state.didLegacyWheelScroll(distanceFromBottom: 80, movedUpward: false)
        #expect(state.mode == .userScrolledUp)

        // Pinning while pane is inactive lands in `.pausedAway`.
        state.pinToBottom(isPaneActive: false)
        #expect(state.mode == .pausedAway)
    }

    @Test func smoothScrollGlideResumeModeStartsGentleAndDoesNotClamp() {
        var resume = PCodeSmoothScrollGlide()
        resume.startResumeGlide(offset: 4000)
        #expect(resume.isResumeGlide)
        #expect(resume.remainingOffset == 4000)
        #expect(resume.velocity == 0)

        // Additional streaming deltas during resume glide do not clamp to `maxOffset`.
        resume.addOffset(500)
        #expect(resume.remainingOffset == 4500)

        // Compare velocity after a few steps against a normal glide at the same offset.
        var normal = PCodeSmoothScrollGlide()
        normal.addOffset(2400)
        var gentle = PCodeSmoothScrollGlide()
        gentle.startResumeGlide(offset: 2400)
        for _ in 0..<30 {
            _ = normal.step(dt: 1.0 / 60.0)
            _ = gentle.step(dt: 1.0 / 60.0)
        }
        #expect(gentle.velocity < normal.velocity)

        // Draining to zero clears `isResumeGlide`.
        var shortResume = PCodeSmoothScrollGlide()
        shortResume.startResumeGlide(offset: 20)
        for _ in 0..<240 {
            if !shortResume.isActive { break }
            _ = shortResume.step(dt: 1.0 / 60.0)
        }
        #expect(!shortResume.isActive)
        #expect(!shortResume.isResumeGlide)
    }

    @Test @MainActor func pauseAwayFreezesViewportShowsJumpCueAndDwellsBeforeResuming() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 300)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        let clip = scrollView.contentView
        doc.resumingDwellDuration = 0.05

        doc.beginStreamingTurn(prompt: "Start streaming")
        doc.beginStreamingMessage(id: "msg-pause")
        for i in 0..<25 {
            doc.appendStreamingDelta("Initial paragraph \(i) filling the viewport.\n\n")
        }
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!scrollView.isJumpToBottomVisible)

        // Leave pane: viewport freezes in place while new content appends below the fold.
        doc.setPaneActive(false)
        #expect(doc.stickyBottom.mode == .pausedAway)
        let frozenY = clip.bounds.origin.y
        for i in 0..<20 {
            doc.appendStreamingDelta("Background paragraph \(i) arriving while unfocused.\n\n")
        }
        #expect(abs(clip.bounds.origin.y - frozenY) < 0.5)
        #expect(doc.stickyBottom.mode == .pausedAway)
        #expect(scrollView.isJumpToBottomVisible)

        // Return to pane: enters `.resumingDwell`, keeps viewport frozen and pill visible.
        doc.setPaneActive(true)
        #expect(doc.stickyBottom.mode == .resumingDwell)
        #expect(abs(clip.bounds.origin.y - frozenY) < 0.5)
        #expect(scrollView.isJumpToBottomVisible)

        // Wait for dwell timer to fire -> resumes `.pinnedToBottom` and hides pill.
        RunLoop.main.run(until: Date().addingTimeInterval(0.12))
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!scrollView.isJumpToBottomVisible)
        #expect(clip.bounds.origin.y > frozenY)
    }

    @Test @MainActor func resumingDwellInstantBailoutAndIntentionalSkip() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 300)
        scrollView.layoutSubtreeIfNeeded()
        let doc = scrollView.transcriptDocumentView
        doc.resumingDwellDuration = 0.05

        doc.beginStreamingTurn(prompt: "Stream")
        doc.beginStreamingMessage(id: "msg-dwell")
        for i in 0..<25 {
            doc.appendStreamingDelta("Line \(i) in the initial viewport.\n\n")
        }

        // 1. Live scroll during dwell cancels dwell and transitions to `.userScrolledUp`.
        doc.setPaneActive(false)
        for i in 0..<15 {
            doc.appendStreamingDelta("Away chunk \(i).\n\n")
        }
        doc.setPaneActive(true)
        #expect(doc.stickyBottom.mode == .resumingDwell)
        doc.handleWillStartLiveScroll()
        #expect(doc.stickyBottom.mode == .userScrolledUp)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        #expect(doc.stickyBottom.mode == .userScrolledUp)

        // 2. Clicking "Jump to bottom" skips dwell immediately.
        doc.scrollToBottomAndPin()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        doc.setPaneActive(false)
        for i in 0..<15 {
            doc.appendStreamingDelta("Second away chunk \(i).\n\n")
        }
        doc.setPaneActive(true)
        #expect(doc.stickyBottom.mode == .resumingDwell)
        doc.scrollToBottomAndPin()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!scrollView.isJumpToBottomVisible)

        // 3. Submitting a new prompt in an active dwelling pane skips dwell immediately.
        doc.setPaneActive(false)
        for i in 0..<15 {
            doc.appendStreamingDelta("Third away chunk \(i).\n\n")
        }
        doc.setPaneActive(true)
        #expect(doc.stickyBottom.mode == .resumingDwell)
        doc.beginStreamingTurn(prompt: "Follow-up prompt", forcePin: false)
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(!scrollView.isJumpToBottomVisible)
    }

    @Test @MainActor func pauseAwayCompletesRevealsAndStartsResumeGlideInWindow() {
        let theme = PCodeTranscriptTheme.make(background: .white, foreground: .black)
        let scrollView = PCodeTranscriptScrollView(typography: .shared, theme: theme)
        scrollView.frame = CGRect(x: 0, y: 0, width: 640, height: 300)
        let window = NSWindow(
            contentRect: CGRect(x: 0, y: 0, width: 640, height: 300),
            styleMask: [.titled],
            backing: .buffered,
            defer: false)
        window.contentView = scrollView
        scrollView.layoutSubtreeIfNeeded()

        let doc = scrollView.transcriptDocumentView
        doc.paceMode = .typed
        doc.resumingDwellDuration = 5.0

        doc.beginStreamingTurn(prompt: "Typed reveal test")
        doc.beginStreamingMessage(id: "msg-reveal")
        for i in 0..<20 {
            doc.appendStreamingDelta("Visible streaming paragraph \(i).\n\n")
        }
        #expect(doc.pendingRevealCount > 0)

        // Leaving completes in-flight reveals so unfocused splits never freeze half-hidden,
        // and new blocks arriving while paused do not enqueue stale reveals.
        doc.setPaneActive(false)
        #expect(doc.pendingRevealCount == 0)
        for i in 0..<15 {
            doc.appendStreamingDelta("Missed paragraph \(i) while away.\n\n")
        }
        #expect(doc.pendingRevealCount == 0)

        // Returning enters dwell with zero pending reveals (no double-pace), then starts resume glide.
        doc.setPaneActive(true)
        #expect(doc.stickyBottom.mode == .resumingDwell)
        #expect(doc.pendingRevealCount == 0)

        doc.completeResumingDwellIfNeeded()
        #expect(doc.stickyBottom.mode == .pinnedToBottom)
        #expect(doc.isResumeGlideActive)
        #expect(!scrollView.isJumpToBottomVisible)
        window.contentView = nil
    }
}
