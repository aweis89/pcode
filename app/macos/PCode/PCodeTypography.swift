import AppKit
import Combine
import SwiftUI

/// Immutable snapshot of typography settings for AppKit layout, TextKit 2 rendering,
/// and height caches.
struct PCodeTypographySnapshot: Equatable, Sendable {
    let style: PCodeTypography.Style
    let size: Double
    let weight: PCodeTypography.Weight
    let codeFamilyAvailable: Bool

    var inlineCodeScale: CGFloat {
        style == .sansAndMono ? 0.92 : 1.0
    }

    var codeBlockScale: CGFloat {
        style == .sansAndMono ? 0.92 : 0.95
    }

    /// Denser monospace scale for the inspector diff view so code diffs remain
    /// scannable in a narrow split pane while still scaling with ⌘+/⌘-.
    var diffCodeScale: CGFloat { 0.78 }

    /// AppKit monospace font sized for the inspector diff view.
    func diffCodeNSFont() -> NSFont {
        let pt = max(9.5, CGFloat(size) * diffCodeScale)
        if codeFamilyAvailable,
           let postScript = Self.jetBrainsPostScriptName(for: weight.nsFontWeight, italic: false),
           let font = NSFont(name: postScript, size: pt)
        {
            return font
        }
        return NSFont.monospacedSystemFont(ofSize: pt, weight: weight.nsFontWeight)
    }

    /// Fixed line height for a given font scale so mixed sans + mono inline runs
    /// never shift baselines.
    func lineHeight(scale: CGFloat = 1) -> CGFloat {
        ceil(CGFloat(size) * scale * 1.42)
    }

    /// AppKit prose/body font according to the selected style preset.
    func proseNSFont(
        scale: CGFloat = 1,
        weight overrideWeight: NSFont.Weight? = nil,
        italic: Bool = false
    ) -> NSFont {
        let resolvedWeight = overrideWeight ?? weight.nsFontWeight
        let pt = CGFloat(size) * scale
        switch style {
        case .sansAndMono:
            let base = NSFont.systemFont(ofSize: pt, weight: resolvedWeight)
            guard italic else { return base }
            let desc = base.fontDescriptor.withSymbolicTraits(
                base.fontDescriptor.symbolicTraits.union(.italic))
            return NSFont(descriptor: desc, size: pt) ?? base
        case .mono:
            return codeNSFont(scale: scale, weight: resolvedWeight, italic: italic)
        }
    }

    /// AppKit monospace font (bundled JetBrains Mono, falling back to system monospaced).
    func codeNSFont(
        scale: CGFloat = 1,
        weight overrideWeight: NSFont.Weight? = nil,
        italic: Bool = false
    ) -> NSFont {
        let resolvedWeight = overrideWeight ?? weight.nsFontWeight
        let pt = CGFloat(size) * scale
        if codeFamilyAvailable,
           let postScript = Self.jetBrainsPostScriptName(for: resolvedWeight, italic: italic),
           let font = NSFont(name: postScript, size: pt)
        {
            return font
        }
        let fallback = NSFont.monospacedSystemFont(ofSize: pt, weight: resolvedWeight)
        guard italic else { return fallback }
        let desc = fallback.fontDescriptor.withSymbolicTraits(
            fallback.fontDescriptor.symbolicTraits.union(.italic))
        return NSFont(descriptor: desc, size: pt) ?? fallback
    }

    /// Maps an `NSFont.Weight` + italic flag onto the exact bundled JetBrains Mono face.
    /// Bundled faces stop at ExtraBold, so `.heavy` / `.black` map cleanly to `ExtraBold`.
    static func jetBrainsPostScriptName(for weight: NSFont.Weight, italic: Bool) -> String? {
        let suffix: String
        if weight.rawValue >= NSFont.Weight.heavy.rawValue {
            suffix = italic ? "ExtraBoldItalic" : "ExtraBold"
        } else if weight.rawValue >= NSFont.Weight.bold.rawValue {
            suffix = italic ? "BoldItalic" : "Bold"
        } else if weight.rawValue >= NSFont.Weight.semibold.rawValue {
            suffix = italic ? "SemiBoldItalic" : "SemiBold"
        } else if weight.rawValue >= NSFont.Weight.medium.rawValue {
            suffix = italic ? "MediumItalic" : "Medium"
        } else {
            suffix = italic ? "Italic" : "Regular"
        }
        return "JetBrainsMono-\(suffix)"
    }
}

/// App-owned typography settings for Pcode's native views (Peek and AppKit transcript).
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

    enum Style: String, CaseIterable, Identifiable, Sendable {
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

    enum Weight: String, CaseIterable, Identifiable, Sendable {
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

        var nsFontWeight: NSFont.Weight {
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

        var nsEmphasisWeight: NSFont.Weight {
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

    /// Current immutable snapshot for AppKit layout and rendering.
    var snapshot: PCodeTypographySnapshot {
        PCodeTypographySnapshot(
            style: style,
            size: size,
            weight: weight,
            codeFamilyAvailable: codeFamilyAvailable)
    }

    /// Post-mutation, coalesced snapshot publisher (so `reset()` emits once, not 3x before mutation).
    var snapshotPublisher: AnyPublisher<PCodeTypographySnapshot, Never> {
        let available = codeFamilyAvailable
        return Publishers.CombineLatest3($style, $size, $weight)
            .map { style, size, weight in
                PCodeTypographySnapshot(
                    style: style,
                    size: Self.clampSize(size),
                    weight: weight,
                    codeFamilyAvailable: available)
            }
            .removeDuplicates()
            .dropFirst()
            .debounce(for: .zero, scheduler: DispatchQueue.main)
            .eraseToAnyPublisher()
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
        guard value.isFinite else {
            return value.isNaN ? defaultSize : (value > 0 ? maxSize : minSize)
        }
        return min(max(value.rounded(), minSize), maxSize)
    }

    func increaseSize() {
        size = Self.clampSize(size + 1)
    }

    func decreaseSize() {
        size = Self.clampSize(size - 1)
    }

    func resetSize() {
        size = Self.defaultSize
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
        Self.makeCodeFont(
            baseSize: CGFloat(size),
            scale: scale,
            weight: overrideWeight ?? weight.fontWeight,
            familyAvailable: codeFamilyAvailable)
    }

    static func makeCodeFont(
        baseSize: CGFloat,
        scale: CGFloat = 1,
        weight: Font.Weight,
        familyAvailable: Bool
    ) -> Font {
        let pt = baseSize * scale
        if familyAvailable {
            return .custom(defaultCodeFamily, fixedSize: pt).weight(weight)
        }
        return .system(size: pt, weight: weight, design: .monospaced)
    }

    var isCodeFamilyAvailable: Bool { codeFamilyAvailable }

    /// Inline code scale: slightly smaller beside proportional sans, full size in all-mono.
    var inlineCodeScale: CGFloat {
        style == .sansAndMono ? 0.92 : 1.0
    }

    /// Code block scale: slightly tighter than body prose so long lines fit cleanly.
    var codeBlockScale: CGFloat {
        style == .sansAndMono ? 0.92 : 0.95
    }
}

/// Workspace-wide ⌘+ / ⌘= / ⌘- / ⌘0 font-size shortcut matcher.
enum PCodeFontSizeShortcut: Equatable, Sendable {
    case increase
    case decrease
    case reset

    static func match(charactersIgnoringModifiers: String?, modifierFlags: NSEvent.ModifierFlags) -> Self? {
        guard let chars = charactersIgnoringModifiers, !chars.isEmpty else { return nil }
        let mods = modifierFlags.intersection([.command, .shift, .option, .control])
        if (chars == "=" || chars == "+") && (mods == .command || mods == [.command, .shift]) {
            return .increase
        }
        if chars == "-" && mods == .command {
            return .decrease
        }
        if chars == "0" && mods == .command {
            return .reset
        }
        return nil
    }

    static func match(_ event: NSEvent) -> Self? {
        match(charactersIgnoringModifiers: event.charactersIgnoringModifiers, modifierFlags: event.modifierFlags)
    }
}
