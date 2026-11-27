import AppKit
import CoreText
import GhosttyKit
import SwiftUI

/// An opaque sRGB color, 0...1 per channel: what the picker's colors are
/// worked out in, so contrast can be measured before anything is drawn.
struct PCodeRGB: Equatable {
    let red: Double
    let green: Double
    let blue: Double

    init(red: Double, green: Double, blue: Double) {
        self.red = red
        self.green = green
        self.blue = blue
    }

    /// Strictly `#rrggbb`: anything else is nil.
    init?(hex: String) {
        let digits = Array(hex.utf8)
        guard digits.count == 7, digits[0] == UInt8(ascii: "#") else { return nil }
        var channels: [Double] = []
        for index in stride(from: 1, to: 7, by: 2) {
            guard let high = Self.hexDigit(digits[index]), let low = Self.hexDigit(digits[index + 1]) else {
                return nil
            }
            channels.append(Double(high * 16 + low) / 255)
        }
        self.init(red: channels[0], green: channels[1], blue: channels[2])
    }

    init?(_ color: Color) {
        guard let rgb = NSColor(color).usingColorSpace(.sRGB) else { return nil }
        self.init(red: Double(rgb.redComponent), green: Double(rgb.greenComponent), blue: Double(rgb.blueComponent))
    }

    var color: Color { Color(.sRGB, red: red, green: green, blue: blue) }
    var nsColor: NSColor { NSColor(srgbRed: red, green: green, blue: blue, alpha: 1) }

    /// WCAG relative luminance.
    var luminance: Double {
        func linear(_ channel: Double) -> Double {
            channel <= 0.04045 ? channel / 12.92 : pow((channel + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    }

    /// WCAG contrast ratio, 1...21.
    func contrast(with other: PCodeRGB) -> Double {
        let (light, dark) = (max(luminance, other.luminance), min(luminance, other.luminance))
        return (light + 0.05) / (dark + 0.05)
    }

    /// This color moved `amount` (0...1) of the way to `other`.
    func mixed(with other: PCodeRGB, _ amount: Double) -> PCodeRGB {
        PCodeRGB(
            red: red + (other.red - red) * amount,
            green: green + (other.green - green) * amount,
            blue: blue + (other.blue - blue) * amount)
    }

    private static func hexDigit(_ byte: UInt8) -> Int? {
        switch byte {
        case UInt8(ascii: "0")...UInt8(ascii: "9"): Int(byte - UInt8(ascii: "0"))
        case UInt8(ascii: "a")...UInt8(ascii: "f"): Int(byte - UInt8(ascii: "a")) + 10
        case UInt8(ascii: "A")...UInt8(ascii: "F"): Int(byte - UInt8(ascii: "A")) + 10
        default: nil
        }
    }
}

/// pcode's optional `"style"` in a list (`pcode.pick_ui.native_style`): each
/// key a `#rrggbb`. Decoding never fails: a style that isn't an object, a key
/// we don't know, and a value that isn't a strict `#rrggbb` are all ignored.
struct PCodePickStyle: Decodable, Equatable {
    /// The selected row's text.
    var accent: PCodeRGB?
    /// The selected row's background.
    var selected: PCodeRGB?
    /// Muted text.
    var dim: PCodeRGB?

    init(accent: PCodeRGB? = nil, selected: PCodeRGB? = nil, dim: PCodeRGB? = nil) {
        self.accent = accent
        self.selected = selected
        self.dim = dim
    }

    init(from decoder: Decoder) {
        let values = (try? decoder.singleValueContainer().decode([String: Lenient].self)) ?? [:]
        func color(_ key: String) -> PCodeRGB? { values[key]?.string.flatMap { PCodeRGB(hex: $0) } }
        self.init(accent: color("accent"), selected: color("selected"), dim: color("dim"))
    }

    /// A JSON value that is kept only if it is a string.
    private struct Lenient: Decodable {
        let string: String?

        init(from decoder: Decoder) {
            string = try? decoder.singleValueContainer().decode(String.self)
        }
    }
}

/// Typography for native popups, pickers, completions, hints, toasts, and drawers:
/// aligned with `PCodeTypography` (scrollback) rather than the user's standalone Ghostty font.
struct PCodePickFonts: Equatable {
    static let smallScale = 0.85

    let regular: NSFont
    let small: NSFont
    let smallMedium: NSFont
    let smallSemibold: NSFont
    /// One monospace column width (used for picker guide alignment and completion width).
    let advance: CGFloat

    init(typography: PCodeTypographySnapshot = PCodeTypography.shared.snapshot) {
        regular = typography.proseNSFont()
        small = typography.proseNSFont(scale: Self.smallScale)
        smallMedium = typography.proseNSFont(scale: Self.smallScale, weight: .medium)
        smallSemibold = typography.proseNSFont(scale: Self.smallScale, weight: .semibold)
        advance = ("M" as NSString).size(withAttributes: [.font: typography.codeNSFont()]).width
    }

    init(_ font: CTFont) {
        regular = font as NSFont
        let smallFont = CTFontCreateCopyWithAttributes(font, CTFontGetSize(font) * Self.smallScale, nil, nil) as NSFont
        small = smallFont
        smallMedium = smallFont
        smallSemibold = smallFont
        advance = ("M" as NSString).size(withAttributes: [.font: regular]).width
    }

    /// Current popup fonts from `PCodeTypography.shared` (consistent with scrollback).
    static func of(_ surface: ghostty_surface_t?) -> PCodePickFonts? {
        PCodePickFonts(typography: PCodeTypography.shared.snapshot)
    }

    func smallFont(weight: Font.Weight = .regular) -> Font {
        switch weight {
        case .semibold, .bold, .heavy, .black:
            return Font(smallSemibold as CTFont)
        case .medium:
            return Font(smallMedium as CTFont)
        default:
            return Font(small as CTFont)
        }
    }

    static func small(_ fonts: PCodePickFonts?, fallbackSize: CGFloat, weight: Font.Weight = .regular) -> Font {
        if let fonts {
            return fonts.smallFont(weight: weight)
        }
        return PCodePickFonts(typography: PCodeTypography.shared.snapshot).smallFont(weight: weight)
    }
}

/// The picker's colors and fonts: the terminal's, with pcode's style on top.
struct PCodePickTheme: Equatable {
    /// Below this contrast the selected row's text falls back to the terminal's.
    static let minimumContrast = 3.0
    /// pcode's `selected` must stand out this much from the panel to be used.
    static let minimumSelectedContrast = 1.15
    static let mutedOpacity = 0.6
    /// Palette entries the picker uses: yellows for the status line, blues
    /// and cyans for badges, and cyan for the selected row.
    static let paletteIndexes = [3, 11, 4, 12, 6, 14]
    static let cyan = 6

    /// The terminal's colors, as the config has them.
    struct Base: Equatable {
        var background: PCodeRGB
        var foreground: PCodeRGB
        /// Only `paletteIndexes`; empty if the config can't be read.
        var palette: [Int: PCodeRGB] = [:]
    }

    var foreground: Color
    /// Details, hints, the header and the footer.
    var muted: Color
    var selectedBackground: PCodeRGB
    var selectedText: PCodeRGB
    /// An action's confirm line.
    var status: Color
    /// A row's marker badge.
    var badge: Color
    var fonts: PCodePickFonts?

    init(base: Base, style: PCodePickStyle?, fonts: PCodePickFonts?) {
        foreground = base.foreground.color
        muted = Self.muted(base: base, style: style)
        let selection = Self.selection(base: base, style: style)
        selectedBackground = selection.background
        selectedText = selection.text
        status = Self.pick([3, 11], from: base).map(\.color) ?? .orange
        badge = Self.pick([4, 12, 6, 14], from: base).map(\.color) ?? base.foreground.color.opacity(Self.mutedOpacity)
        self.fonts = fonts
    }

    /// pcode's `dim` if it reads on the panel, else the faded foreground.
    static func muted(base: Base, style: PCodePickStyle?) -> Color {
        if let dim = style?.dim, dim.contrast(with: base.background) >= minimumContrast { return dim.color }
        return base.foreground.color.opacity(mutedOpacity)
    }

    /// The selected row's colors, its text kept readable on its background:
    /// pcode's `selected` if it stands out from the panel; else the terminal
    /// list's own look (`reverse ansicyan`), palette cyan behind the panel's
    /// background color; else, without a palette, a tint of the foreground.
    static func selection(base: Base, style: PCodePickStyle?) -> (background: PCodeRGB, text: PCodeRGB) {
        let background: PCodeRGB
        let text: PCodeRGB
        if let selected = style?.selected, selected.contrast(with: base.background) >= minimumSelectedContrast {
            (background, text) = (selected, style?.accent ?? base.foreground)
        } else if let cyan = base.palette[Self.cyan] {
            (background, text) = (cyan, style?.accent ?? base.background)
        } else {
            (background, text) = (base.background.mixed(with: base.foreground, 0.22), style?.accent ?? base.foreground)
        }
        return (background, readable(text, on: background, base: base))
    }

    /// `text`, unless it is too faint on `background`: then the foreground or
    /// background, whichever stands out more.
    static func readable(_ text: PCodeRGB, on background: PCodeRGB, base: Base) -> PCodeRGB {
        guard text.contrast(with: background) < minimumContrast else { return text }
        return base.foreground.contrast(with: background) >= base.background.contrast(with: background)
            ? base.foreground : base.background
    }

    /// Of the palette's `indexes`, the one that stands out most on the background.
    private static func pick(_ indexes: [Int], from base: Base) -> PCodeRGB? {
        indexes.compactMap { base.palette[$0] }
            .max { $0.contrast(with: base.background) < $1.contrast(with: base.background) }
    }

    func labelFont(fallbackSize: CGFloat) -> Font {
        fonts.map { Font($0.regular as CTFont) } ?? .system(size: fallbackSize)
    }

    func smallFont(fallbackSize: CGFloat, weight: Font.Weight = .regular) -> Font {
        PCodePickFonts.small(fonts, fallbackSize: fallbackSize, weight: weight)
    }
}

extension PCodePickTheme.Base {
    /// The terminal's colors from `config`.
    init(_ config: Ghostty.Config) {
        self.init(
            background: PCodeRGB(config.backgroundColor) ?? PCodeRGB(red: 0, green: 0, blue: 0),
            foreground: PCodeRGB(config.foregroundColor) ?? PCodeRGB(red: 1, green: 1, blue: 1),
            palette: (config.palette(PCodePickTheme.paletteIndexes) ?? [:]).compactMapValues { PCodeRGB($0) })
    }
}
