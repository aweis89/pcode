import CoreGraphics
import Testing
@testable import Ghostty

@Suite
struct PCodeSidebarTests {
    @Test func clampsWidthWithinBounds() {
        #expect(PCodeSidebarLayout.clampedWidth(100, totalWidth: 1200) == PCodeSidebarLayout.minWidth)
        #expect(PCodeSidebarLayout.clampedWidth(320, totalWidth: 1200) == 320)
        #expect(PCodeSidebarLayout.clampedWidth(700, totalWidth: 1200) == PCodeSidebarLayout.maxWidth)
    }

    @Test func clampsWidthToReserveDetailPaneSpace() {
        // 700 - 360 = 340 max sidebar width in a narrow window.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 700) == 340)
        // Even in a tiny window, never shrink below minWidth.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 400) == PCodeSidebarLayout.minWidth)
    }

    @Test func fallsBackToDefaultForNonPositiveOrNonFiniteWidth() {
        #expect(PCodeSidebarLayout.clampedWidth(0, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(-50, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.nan, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.infinity, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
    }

    @Test func revealEdgeHitZone() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 0, y: 400), in: bounds))
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth + 1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: -1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 2, y: 801), in: bounds))
    }

    @Test func revealedZoneIncludesGraceMargin() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        let width: CGFloat = 280
        #expect(PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: 100, y: 400), in: bounds, sidebarWidth: width))
        #expect(PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin + 1, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: -1, y: 400), in: bounds, sidebarWidth: width))
    }

    @Test func displayTitlePrefersLiveTitleOverSavedAndHandlesPlaceholders() {
        #expect(PCodeWorkspace.displayTitle(paneTitle: "", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Saved title") == "Saved title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Renamed title", savedLabel: "Old title") == "Renamed title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "  Trimmed title  ", savedLabel: nil) == "Trimmed title")
        // Shortening `/rename` to a prefix of the old saved label must take effect immediately.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Fix auth bug", savedLabel: "Fix auth bug in login flow") == "Fix auth bug")
        // Normalize internal newlines and consecutive whitespace.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Line one\n  line   two") == "Line one line two")
        // Keep full saved title when OSC 0 live title is a 120-char truncated prefix of it.
        let full = String(repeating: "a", count: 140)
        let prefix = String(full.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: prefix, savedLabel: full) == full)
        let fullWithWhitespace = String(repeating: "a", count: 60) + "\n  " + String(repeating: "b", count: 80)
        let normalizedFull = String(repeating: "a", count: 60) + " " + String(repeating: "b", count: 80)
        let normalizedPrefix = String(normalizedFull.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: normalizedPrefix, savedLabel: fullWithWhitespace) == normalizedFull)
    }
}
