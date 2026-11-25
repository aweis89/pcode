import Foundation
import SwiftUI
import Testing
@testable import Ghostty

/// pcode's optional list `style` and the colors the picker derives from it.
@Suite
struct PCodePickStyleTests {
    private static let black = PCodeRGB(red: 0, green: 0, blue: 0)
    private static let white = PCodeRGB(red: 1, green: 1, blue: 1)

    @Test(arguments: [
        ("#000000", PCodeRGB(red: 0, green: 0, blue: 0)),
        ("#ffffff", PCodeRGB(red: 1, green: 1, blue: 1)),
        ("#FF8000", PCodeRGB(red: 1, green: 128.0 / 255, blue: 0)),
    ])
    func parsesHex(_ text: String, _ expected: PCodeRGB) {
        #expect(PCodeRGB(hex: text) == expected)
    }

    @Test(arguments: ["", "#", "fff000", "#fff", "#ffff000", "#gggggg", " #ffffff", "#ffffff ", "#ff ff0", "#+fffff", "#ＦＦＦＦＦＦ"])
    func rejectsBadHex(_ text: String) {
        #expect(PCodeRGB(hex: text) == nil)
    }

    // MARK: Decoding, through the picker's own list

    /// A list as pcode sends it, with `style` as given (left out when nil).
    private func list(style: String?) throws -> PCodePicker.Payload {
        let styleField = style.map { #", "style": \#($0)"# } ?? ""
        let json = ##"""
        {"title": "t", "header": "", "empty": "", "query": "", "match": "fuzzy",
         "items": [{"id": "a", "label": "A", "detail": "", "marker": "", "depth": 0, "search": ["a"], "confirm": ""}],
         "actions": []\##(styleField)}
        """##
        return try JSONDecoder().decode(PCodePicker.Payload.self, from: Data(json.utf8))
    }

    @Test func styleAbsentOrNull() throws {
        #expect(try list(style: nil).style == nil)
        #expect(try list(style: "null").style == nil)
    }

    @Test func styleDecodes() throws {
        let style = try list(style: ##"{"accent": "#ff0000", "selected": "#00ff00", "dim": "#0000ff", "new": "#123456"}"##).style
        #expect(style == PCodePickStyle(
            accent: PCodeRGB(red: 1, green: 0, blue: 0),
            selected: PCodeRGB(red: 0, green: 1, blue: 0),
            dim: PCodeRGB(red: 0, green: 0, blue: 1)))
    }

    @Test(arguments: [#""red""#, "5", "[]", "true", ##"["#ff0000"]"##])
    func styleOfWrongTypeIsEmpty(_ value: String) throws {
        let list = try list(style: value)
        #expect(list.title == "t")
        #expect(list.items.count == 1)
        #expect(list.style == PCodePickStyle())
    }

    @Test func badValuesAreIgnored() throws {
        let style = try list(style: ##"{"accent": 3, "selected": "#zzzzzz", "dim": {"x": 1}}"##).style
        #expect(style == PCodePickStyle())
        let partial = try list(style: ##"{"accent": "red", "dim": "#808080"}"##).style
        #expect(partial?.accent == nil)
        #expect(partial?.dim == PCodeRGB(hex: "#808080"))
        let null = try list(style: ##"{"accent": null, "selected": "#000080"}"##).style
        #expect(null == PCodePickStyle(selected: PCodeRGB(hex: "#000080")))
    }

    // MARK: Colors

    private static let cyan = PCodeRGB(hex: "#56d6e6")!
    private let dark = PCodePickTheme.Base(background: black, foreground: white)
    private let light = PCodePickTheme.Base(background: white, foreground: PCodeRGB(hex: "#202020")!)

    @Test func contrastRatio() {
        #expect(abs(Self.black.contrast(with: Self.white) - 21) < 0.01)
        #expect(Self.white.contrast(with: Self.white) == 1)
    }

    @Test func styleSetsTheSelectedRow() {
        let styled = PCodePickTheme.selection(
            base: dark, style: PCodePickStyle(accent: PCodeRGB(hex: "#ffff00"), selected: PCodeRGB(hex: "#000080")))
        #expect(styled.background == PCodeRGB(hex: "#000080"))
        #expect(styled.text == PCodeRGB(hex: "#ffff00"))
    }

    @Test func noSelectedIsTheTerminalListsReverseCyan() {
        var base = dark
        base.palette = [6: Self.cyan]
        let plain = PCodePickTheme.selection(base: base, style: nil)
        #expect(plain.background == Self.cyan)
        #expect(plain.text == Self.black)
        let accented = PCodePickTheme.selection(base: base, style: PCodePickStyle(accent: PCodeRGB(hex: "#101040")))
        #expect(accented.background == Self.cyan)
        #expect(accented.text == PCodeRGB(hex: "#101040"))
    }

    @Test func selectedTooCloseToThePanelFallsBack() {
        var base = dark
        base.palette = [6: Self.cyan]
        let almostBlack = PCodePickStyle(selected: PCodeRGB(hex: "#050505"))
        #expect(PCodePickTheme.selection(base: base, style: almostBlack).background == Self.cyan)
        #expect(PCodePickTheme.selection(base: dark, style: almostBlack).background == dark.background.mixed(with: dark.foreground, 0.22))
    }

    @Test func faintSelectedTextFallsBack() {
        // Grey on grey: whichever of the background (black) or foreground (white) stands out more.
        let lightGrey = PCodePickTheme.selection(
            base: dark, style: PCodePickStyle(accent: PCodeRGB(hex: "#aaaaaa"), selected: PCodeRGB(hex: "#a0a0a0")))
        #expect(lightGrey.text == Self.black)
        let darkGrey = PCodePickTheme.selection(
            base: dark, style: PCodePickStyle(accent: PCodeRGB(hex: "#303030"), selected: PCodeRGB(hex: "#333333")))
        #expect(darkGrey.text == Self.white)
    }

    @Test func unsetSelectionTintsTheForeground() {
        let selection = PCodePickTheme.selection(base: dark, style: nil)
        #expect(selection.background == dark.background.mixed(with: dark.foreground, 0.22))
        #expect(selection.text == Self.white)
        #expect(selection.text.contrast(with: selection.background) >= PCodePickTheme.minimumContrast)
    }

    @Test func lightBase() {
        let tint = PCodePickTheme.selection(base: light, style: nil)
        #expect(tint.text == light.foreground)
        #expect(tint.text.contrast(with: tint.background) >= PCodePickTheme.minimumContrast)
        var base = light
        base.palette = [6: Self.cyan]
        let cyan = PCodePickTheme.selection(base: base, style: nil)
        #expect(cyan.background == Self.cyan)
        // The panel's white is too faint on cyan: the dark foreground reads instead.
        #expect(cyan.text == light.foreground)
    }

    @Test func dimNeedsContrastWithThePanel() {
        let readable = PCodePickTheme.muted(base: dark, style: PCodePickStyle(dim: PCodeRGB(hex: "#808080")))
        #expect(readable == PCodeRGB(hex: "#808080")!.color)
        let faint = PCodePickTheme.muted(base: dark, style: PCodePickStyle(dim: PCodeRGB(hex: "#202020")))
        #expect(faint == Self.white.color.opacity(PCodePickTheme.mutedOpacity))
        #expect(PCodePickTheme.muted(base: dark, style: nil) == Self.white.color.opacity(PCodePickTheme.mutedOpacity))
    }
}
