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
        let view = PCodeTextKit2BlockView(frame: CGRect(x: 0, y: 0, width: 320, height: 100))
        let attr = NSMutableAttributedString(string: "Click https://example.com or file:///etc/passwd")
        attr.addAttribute(.font, value: NSFont.systemFont(ofSize: 14), range: NSRange(location: 0, length: attr.length))
        attr.addAttribute(.link, value: URL(string: "https://example.com")!, range: NSRange(location: 6, length: 19))
        attr.addAttribute(.link, value: URL(string: "file:///etc/passwd")!, range: NSRange(location: 29, length: 18))

        let height = view.measureHeight(for: attr, width: 320)
        #expect(height > 14)
        view.frame.size.height = height

        // Offset at top-left should be 0.
        #expect(view.utf16Offset(at: CGPoint(x: 1, y: 4)) == 0)
        // Non-http(s) links are never returned.
        #expect(view.linkURL(at: CGPoint(x: 1, y: 4)) == nil)
    }
}
