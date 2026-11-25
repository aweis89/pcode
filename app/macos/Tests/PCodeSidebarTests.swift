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
}
