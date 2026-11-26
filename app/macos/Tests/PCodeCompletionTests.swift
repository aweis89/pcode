import Foundation
import Testing
@testable import Ghostty

/// pcode's completion menu as it arrives, and where the list is laid out.
@Suite
struct PCodeCompletionTests {
    private static let cursor = CGRect(x: 100, y: 500, width: 10, height: 20)

    private func menu(items: Int = 3, dx: Int = -2, dy: Int = 1) throws -> PCodeCompletion.Menu {
        let rows = (0..<items).map { #"{"text": "/c\#($0)", "meta": "about"}"# }.joined(separator: ",")
        let json = ##"{"seq": 4, "items": [\##(rows)], "selected": 1, "offset": 0, "total": \##(items), "dx": \##(dx), "dy": \##(dy), "style": {"dim": "#808080"}}"##
        return try JSONDecoder().decode(PCodeCompletion.Menu.self, from: Data(json.utf8))
    }

    @Test func decodesAMenu() throws {
        let menu = try menu()
        #expect(menu.seq == 4 && menu.items.count == 3 && menu.selected == 1)
        #expect(menu.items[2].text == "/c2" && menu.items[2].meta == "about")
        #expect(menu.style?.dim == PCodeRGB(hex: "#808080"))
    }

    @Test func aMenuWithoutStyleOrSelectionDecodes() throws {
        let json = #"{"seq": 1, "items": [], "selected": null, "offset": 0, "total": 0, "dx": 0, "dy": 0}"#
        let menu = try JSONDecoder().decode(PCodeCompletion.Menu.self, from: Data(json.utf8))
        #expect(menu.selected == nil && menu.style == nil)
    }

    @Test func hangsAboveTheEditorBoxFromTheWordsStart() throws {
        let size = CGSize(width: 800, height: 600)
        let frame = PCodeCompletionLayer.frame(try menu(), cursor: Self.cursor, fonts: nil, in: size)
        // Its bottom is a margin above the box's top row, one row over the cursor's.
        #expect(frame.maxY == Self.cursor.minY - Self.cursor.height - PCodeCompletionLayer.margin)
        let rowHeight = Self.cursor.height + 2 * PCodeCompletionLayer.rowPadding
        #expect(frame.height == 3 * rowHeight + 2 * PCodeCompletionLayer.listPadding)
        // The rows' text starts under the word, two cells left of the cursor.
        let textLeft = frame.minX + PCodeCompletionLayer.textInset + PCodeCompletionLayer.listPadding
        #expect(textLeft == Self.cursor.minX - 2 * Self.cursor.width)
    }

    @Test func dropsBelowTheCursorWithoutRoomAbove() throws {
        let cursor = CGRect(x: 0, y: 40, width: 10, height: 20)
        let size = CGSize(width: 800, height: 600)
        let frame = PCodeCompletionLayer.frame(try menu(dy: 0), cursor: cursor, fonts: nil, in: size)
        #expect(frame.minY == cursor.maxY + PCodeCompletionLayer.margin)
        #expect(frame.minX == PCodeCompletionLayer.margin)
    }

    @Test func staysInsideTheView() throws {
        let size = CGSize(width: 300, height: 200)
        let cursor = CGRect(x: 290, y: 180, width: 10, height: 20)
        let frame = PCodeCompletionLayer.frame(try menu(items: 50, dy: 0), cursor: cursor, fonts: nil, in: size)
        #expect(frame.minX >= PCodeCompletionLayer.margin && frame.maxX <= size.width - PCodeCompletionLayer.margin)
        #expect(frame.minY >= PCodeCompletionLayer.margin && frame.maxY <= size.height)
    }

    @Test func dockCardLayoutPositionsEditorInPaneAndInDrawer() {
        let size = CGSize(width: 1000, height: 600)
        let full = PCodeDock.cardLayout(in: size, rows: 4, cell: 20, drawerOpen: false)
        // Full-width card inset by `bottomMargin` on left, right and bottom;
        // the 40-row editor grid sits bottom-aligned at `size.height - bottomMargin`.
        #expect(full.editorRect.minX == PCodeDock.bottomMargin)
        #expect(full.editorRect.width == size.width - 2 * PCodeDock.bottomMargin)
        #expect(full.editorRect.maxY == size.height - PCodeDock.bottomMargin)
        #expect(full.editorRect.height == PCodeDock.cardHeight(rows: PCodeDock.gridRows, cell: 20))
        #expect(full.cardTopFromBottom == PCodeDock.bottomMargin + PCodeDock.cardHeight(rows: 4, cell: 20))

        // With the /btw drawer open on the trailing side, the card narrows into the drawer.
        let inDrawer = PCodeDock.cardLayout(
            in: size, rows: 4, cell: 20, drawerOpen: true, drawerFraction: 0.45)
        let expectedOuter = PCodeDrawer.width(in: size.width, fraction: 0.45, minWidth: PCodeDrawer.dockedMinWidth) - 1
        #expect(inDrawer.editorRect.minX == size.width - expectedOuter + PCodeDock.bottomMargin)
        #expect(inDrawer.editorRect.width == expectedOuter - 2 * PCodeDock.bottomMargin)
    }

    @Test func dockCardCapsWidthAndCentersOnWidePanes() {
        let wideSize = CGSize(width: 1600, height: 600)
        let wide = PCodeDock.cardLayout(in: wideSize, rows: 4, cell: 20, drawerOpen: false)
        #expect(wide.editorRect.width == PCodeDock.maxCardWidth)
        #expect(wide.editorRect.minX == floor((wideSize.width - PCodeDock.maxCardWidth) * 0.5))

        // Odd pane width keeps `outerMinX + outerWidth + trailingInset == paneWidth`.
        let odd = PCodeDock.horizontalLayout(in: 1401, drawerOpen: false)
        #expect(odd.cardWidth == PCodeDock.maxCardWidth)
        #expect(odd.outerMinX + odd.outerWidth + odd.trailingInset == 1401)

        // Wide /btw drawer also caps the card at `maxCardWidth` and centers it inside the drawer.
        let wideDrawerSize = CGSize(width: 2000, height: 600)
        let wideDrawer = PCodeDock.cardLayout(
            in: wideDrawerSize, rows: 4, cell: 20, drawerOpen: true, drawerFraction: 0.70)
        let drawerSlot = PCodeDrawer.width(
            in: wideDrawerSize.width, fraction: 0.70, minWidth: PCodeDrawer.dockedMinWidth) - 1
        let drawerMinX = wideDrawerSize.width - drawerSlot
        let expectedOuterMinX = drawerMinX + floor((drawerSlot - (PCodeDock.maxCardWidth + 2 * PCodeDock.bottomMargin)) * 0.5)
        #expect(wideDrawer.editorRect.width == PCodeDock.maxCardWidth)
        #expect(wideDrawer.editorRect.minX == expectedOuterMinX + PCodeDock.bottomMargin)
    }

    @Test func dockFillsThePaneUntilReadyThenShowsTheCard() {
        // Before `pcode;ready`: the whole pane inside the margins, and all of it takes clicks.
        #expect(PCodeDock.editorHeight(ready: false, cell: 20, paneHeight: 600)
            == 600 - PCodeDock.topMargin - PCodeDock.bottomMargin)
        #expect(PCodeDock.visibleHeight(ready: false, rows: 4, cell: 20) == nil)
        // Once ready: the fixed grid, with clicks only on the card's rows.
        #expect(PCodeDock.editorHeight(ready: true, cell: 20, paneHeight: 600)
            == PCodeDock.cardHeight(rows: PCodeDock.gridRows, cell: 20))
        #expect(PCodeDock.visibleHeight(ready: true, rows: 4, cell: 20)
            == PCodeDock.cardHeight(rows: 4, cell: 20))
        // No cell size yet: nothing to clip by.
        #expect(PCodeDock.visibleHeight(ready: true, rows: 4, cell: 0) == nil)
        #expect(PCodeDock.editorHeight(ready: false, cell: 20, paneHeight: 4) == 0)
    }
}
