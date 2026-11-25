import AppKit
import SwiftUI

/// App-owned typography settings for Pcode's native SwiftUI views (starting with Peek).
///
/// Independent of the user's Ghostty terminal config: ships with JetBrains Mono bundled
/// in `Contents/Resources/Fonts` (registered via `ATSApplicationFontsPath`) and persists
/// user preferences in `UserDefaults`.
final class PCodeTypography: ObservableObject {
    static let shared = PCodeTypography()

    /// Bundled default monospace font family.
    static let defaultCodeFamily = "JetBrains Mono"
    static let defaultSize: Double = 14
    static let minSize: Double = 11
    static let maxSize: Double = 22

    enum Style: String, CaseIterable, Identifiable {
        /// Proportional system sans for prose/UI, JetBrains Mono for code and tool calls.
        case sansAndMono = "sans_mono"
        /// JetBrains Mono everywhere for a unified, terminal-native feel.
        case mono = "mono"

        var id: String { rawValue }

        var label: String {
            switch self {
            case .sansAndMono: return "Sans + Mono"
            case .mono: return "Mono"
            }
        }
    }

    enum Weight: String, CaseIterable, Identifiable {
        case regular
        case medium
        case semibold
        case bold

        var id: String { rawValue }

        var label: String {
            switch self {
            case .regular: return "Regular"
            case .medium: return "Medium"
            case .semibold: return "Semibold"
            case .bold: return "Bold"
            }
        }

        var fontWeight: Font.Weight {
            switch self {
            case .regular: return .regular
            case .medium: return .medium
            case .semibold: return .semibold
            case .bold: return .bold
            }
        }

        /// Emphasized weight for bold/strong spans and headings so they still stand out
        /// when the user picks a heavier base weight.
        var emphasisWeight: Font.Weight {
            switch self {
            case .regular: return .semibold
            case .medium: return .bold
            case .semibold: return .bold
            case .bold: return .heavy
            }
        }
    }

    private enum Keys {
        static let style = "PCodeTypographyStyle"
        static let size = "PCodeTypographySize"
        static let weight = "PCodeTypographyWeight"
    }

    private let defaults: UserDefaults
    private let codeFamilyAvailable: Bool

    @Published var style: Style {
        didSet {
            guard style != oldValue else { return }
            defaults.set(style.rawValue, forKey: Keys.style)
        }
    }

    @Published var size: Double {
        didSet {
            let clamped = Self.clampSize(size)
            if clamped != size {
                size = clamped
                return
            }
            guard size != oldValue else { return }
            defaults.set(size, forKey: Keys.size)
        }
    }

    @Published var weight: Weight {
        didSet {
            guard weight != oldValue else { return }
            defaults.set(weight.rawValue, forKey: Keys.weight)
        }
    }

    init(defaults: UserDefaults = .standard, codeFamilyAvailable: Bool? = nil) {
        self.defaults = defaults
        self.codeFamilyAvailable = codeFamilyAvailable
            ?? NSFontManager.shared.availableFontFamilies.contains(Self.defaultCodeFamily)

        if let raw = defaults.string(forKey: Keys.style), let parsed = Style(rawValue: raw) {
            self.style = parsed
        } else {
            self.style = .sansAndMono
        }

        let storedSize = defaults.double(forKey: Keys.size)
        self.size = storedSize > 0 ? Self.clampSize(storedSize) : Self.defaultSize

        if let raw = defaults.string(forKey: Keys.weight), let parsed = Weight(rawValue: raw) {
            self.weight = parsed
        } else {
            self.weight = .medium
        }
    }

    static func clampSize(_ value: Double) -> Double {
        min(max(value.rounded(), minSize), maxSize)
    }

    func increaseSize() {
        size = Self.clampSize(size + 1)
    }

    func decreaseSize() {
        size = Self.clampSize(size - 1)
    }

    func reset() {
        style = .sansAndMono
        size = Self.defaultSize
        weight = .medium
    }

    /// Prose/body font according to the selected style preset.
    func proseFont(scale: CGFloat = 1, weight overrideWeight: Font.Weight? = nil) -> Font {
        let resolvedWeight = overrideWeight ?? weight.fontWeight
        let pt = CGFloat(size) * scale
        switch style {
        case .sansAndMono:
            return .system(size: pt, weight: resolvedWeight, design: .default)
        case .mono:
            return codeFont(scale: scale, weight: resolvedWeight)
        }
    }

    /// Code/monospace font (bundled JetBrains Mono, falling back to system monospaced).
    func codeFont(scale: CGFloat = 1, weight overrideWeight: Font.Weight? = nil) -> Font {
        let resolvedWeight = overrideWeight ?? weight.fontWeight
        let pt = CGFloat(size) * scale
        if codeFamilyAvailable {
            return .custom(Self.defaultCodeFamily, fixedSize: pt).weight(resolvedWeight)
        }
        return .system(size: pt, weight: resolvedWeight, design: .monospaced)
    }

    /// Inline code scale: slightly smaller beside proportional sans, full size in all-mono.
    var inlineCodeScale: CGFloat {
        style == .sansAndMono ? 0.92 : 1.0
    }

    /// Code block scale: slightly tighter than body prose so long lines fit cleanly.
    var codeBlockScale: CGFloat {
        style == .sansAndMono ? 0.92 : 0.95
    }
}
