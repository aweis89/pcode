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
}
