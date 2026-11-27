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
    /// Optional custom system font family for prose (`nil` = macOS System SF Pro).
    let proseFamily: String?
    /// Optional custom system font family for code/mono (`nil` = bundled JetBrains Mono).
    let codeFamily: String?

    init(
        style: PCodeTypography.Style,
        size: Double,
        weight: PCodeTypography.Weight,
        codeFamilyAvailable: Bool,
        proseFamily: String? = nil,
        codeFamily: String? = nil
    ) {
        self.style = style
        self.size = size
        self.weight = weight
        self.codeFamilyAvailable = codeFamilyAvailable
        self.proseFamily = Self.normalizedFamily(proseFamily)
        self.codeFamily = Self.normalizedFamily(codeFamily)
    }

    static func normalizedFamily(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty
        else { return nil }
        return trimmed
    }

    /// Effective monospace font family name, or `nil` if falling back to system monospaced.
    var resolvedCodeFamily: String? {
        if let codeFamily { return codeFamily }
        return codeFamilyAvailable ? PCodeTypography.defaultCodeFamily : nil
    }

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
        return resolveCodeNSFont(pt: pt, weight: weight.nsFontWeight, italic: false)
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
            if let proseFamily,
               let custom = Self.resolveFamilyNSFont(
                   family: proseFamily, size: pt, weight: resolvedWeight, italic: italic)
            {
                return custom
            }
            let base = NSFont.systemFont(ofSize: pt, weight: resolvedWeight)
            guard italic else { return base }
            let desc = base.fontDescriptor.withSymbolicTraits(
                base.fontDescriptor.symbolicTraits.union(.italic))
            return NSFont(descriptor: desc, size: pt) ?? base
        case .mono:
            return codeNSFont(scale: scale, weight: resolvedWeight, italic: italic)
        }
    }

    /// AppKit monospace font (user-selected family or bundled JetBrains Mono, falling back to system monospaced).
    func codeNSFont(
        scale: CGFloat = 1,
        weight overrideWeight: NSFont.Weight? = nil,
        italic: Bool = false
    ) -> NSFont {
        let resolvedWeight = overrideWeight ?? weight.nsFontWeight
        let pt = CGFloat(size) * scale
        return resolveCodeNSFont(pt: pt, weight: resolvedWeight, italic: italic)
    }

    private func resolveCodeNSFont(pt: CGFloat, weight: NSFont.Weight, italic: Bool) -> NSFont {
        if let family = resolvedCodeFamily,
           let custom = Self.resolveFamilyNSFont(family: family, size: pt, weight: weight, italic: italic)
        {
            return custom
        }
        let fallback = NSFont.monospacedSystemFont(ofSize: pt, weight: weight)
        guard italic else { return fallback }
        let desc = fallback.fontDescriptor.withSymbolicTraits(
            fallback.fontDescriptor.symbolicTraits.union(.italic))
        return NSFont(descriptor: desc, size: pt) ?? fallback
    }

    private struct FontMemberCandidate: Sendable {
        let postScript: String
        let weight: Int
        let isItalic: Bool
        let isCondensedOrExpanded: Bool
    }

    private static let familyMembersLock = NSLock()
    private static var familyMembersCache: [String: [FontMemberCandidate]] = [:]

    private static func cachedMembers(for family: String) -> [FontMemberCandidate] {
        familyMembersLock.lock()
        if let cached = familyMembersCache[family] {
            familyMembersLock.unlock()
            return cached
        }
        familyMembersLock.unlock()

        let italicMask = NSFontTraitMask.italicFontMask.rawValue
        let condensedOrExpandedMask =
            NSFontTraitMask.condensedFontMask.rawValue
            | NSFontTraitMask.expandedFontMask.rawValue
            | NSFontTraitMask.compressedFontMask.rawValue
            | NSFontTraitMask.narrowFontMask.rawValue

        let members = NSFontManager.shared.availableMembers(ofFontFamily: family) ?? []
        let parsed: [FontMemberCandidate] = members.compactMap { entry in
            guard entry.count >= 4,
                  let ps = entry[0] as? String,
                  let w = entry[2] as? Int,
                  let traits = entry[3] as? UInt
            else { return nil }
            return FontMemberCandidate(
                postScript: ps,
                weight: w,
                isItalic: (traits & italicMask) != 0,
                isCondensedOrExpanded: (traits & condensedOrExpandedMask) != 0)
        }

        familyMembersLock.lock()
        familyMembersCache[family] = parsed
        familyMembersLock.unlock()
        return parsed
    }

    /// Resolves a named font family to the closest matching `NSFont` for `weight` and `italic`.
    /// Uses cached `availableMembers(ofFontFamily:)` so families missing intermediate weights (e.g. Medium)
    /// pick the nearest weight in the same family rather than silently falling back to Helvetica.
    static func resolveFamilyNSFont(
        family: String,
        size pt: CGFloat,
        weight: NSFont.Weight,
        italic: Bool
    ) -> NSFont? {
        if family == PCodeTypography.defaultCodeFamily,
           let postScript = jetBrainsPostScriptName(for: weight, italic: italic),
           let font = NSFont(name: postScript, size: pt)
        {
            return font
        }

        let candidates = cachedMembers(for: family)
        guard !candidates.isEmpty else { return nil }

        let targetAppKitWeight = appKitWeight(for: weight)

        // Prefer matching italic trait; if italic was requested and the family has no
        // italic faces, pick the best upright face and convert it to italic below.
        let matchingItalic = candidates.filter { $0.isItalic == italic }
        let pool = matchingItalic.isEmpty ? candidates.filter { !$0.isItalic } : matchingItalic
        let searchPool = pool.isEmpty ? candidates : pool

        let best = searchPool.min { lhs, rhs in
            if lhs.isCondensedOrExpanded != rhs.isCondensedOrExpanded {
                return !lhs.isCondensedOrExpanded
            }
            let ld = abs(lhs.weight - targetAppKitWeight)
            let rd = abs(rhs.weight - targetAppKitWeight)
            if ld != rd { return ld < rd }
            // Break ties toward slightly lighter/regular weight so Regular wins over Bold for Medium.
            return lhs.weight < rhs.weight
        }

        guard let best, let font = NSFont(name: best.postScript, size: pt) else { return nil }
        if italic && !best.isItalic {
            let converted = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask)
            if converted.familyName == font.familyName {
                return converted
            }
        }
        return font
    }

    /// Maps `NSFont.Weight` onto `NSFontManager`'s 0...15 weight scale (5 = Regular, 6 = Medium, 8 = Semibold, 9 = Bold).
    static func appKitWeight(for weight: NSFont.Weight) -> Int {
        if weight.rawValue >= NSFont.Weight.heavy.rawValue { return 11 }
        if weight.rawValue >= NSFont.Weight.bold.rawValue { return 9 }
        if weight.rawValue >= NSFont.Weight.semibold.rawValue { return 8 }
        if weight.rawValue >= NSFont.Weight.medium.rawValue { return 6 }
        if weight.rawValue <= NSFont.Weight.light.rawValue { return 3 }
        return 5
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
        static let proseFamily = "PCodeTypographyProseFamily"
        static let codeFamily = "PCodeTypographyCodeFamily"
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

    /// Optional custom system font family for prose (`nil` = macOS System SF Pro).
    @Published var proseFamily: String? {
        didSet {
            let normalized = PCodeTypographySnapshot.normalizedFamily(proseFamily)
            if normalized != proseFamily {
                proseFamily = normalized
                return
            }
            guard proseFamily != oldValue else { return }
            if let proseFamily {
                defaults.set(proseFamily, forKey: Keys.proseFamily)
            } else {
                defaults.removeObject(forKey: Keys.proseFamily)
            }
        }
    }

    /// Optional custom system font family for code/mono (`nil` = bundled JetBrains Mono).
    @Published var codeFamily: String? {
        didSet {
            let normalized = PCodeTypographySnapshot.normalizedFamily(codeFamily)
            if normalized != codeFamily {
                codeFamily = normalized
                return
            }
            guard codeFamily != oldValue else { return }
            if let codeFamily {
                defaults.set(codeFamily, forKey: Keys.codeFamily)
            } else {
                defaults.removeObject(forKey: Keys.codeFamily)
            }
        }
    }

    /// Current immutable snapshot for AppKit layout and rendering.
    var snapshot: PCodeTypographySnapshot {
        PCodeTypographySnapshot(
            style: style,
            size: size,
            weight: weight,
            codeFamilyAvailable: codeFamilyAvailable,
            proseFamily: proseFamily,
            codeFamily: codeFamily)
    }

    /// Post-mutation, coalesced snapshot publisher (so `reset()` emits once, not 5x before mutation).
    var snapshotPublisher: AnyPublisher<PCodeTypographySnapshot, Never> {
        let available = codeFamilyAvailable
        let base = Publishers.CombineLatest3($style, $size, $weight)
        let families = Publishers.CombineLatest($proseFamily, $codeFamily)
        return Publishers.CombineLatest(base, families)
            .map { base, families in
                PCodeTypographySnapshot(
                    style: base.0,
                    size: Self.clampSize(base.1),
                    weight: base.2,
                    codeFamilyAvailable: available,
                    proseFamily: families.0,
                    codeFamily: families.1)
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

        self.proseFamily = PCodeTypographySnapshot.normalizedFamily(
            defaults.string(forKey: Keys.proseFamily))
        self.codeFamily = PCodeTypographySnapshot.normalizedFamily(
            defaults.string(forKey: Keys.codeFamily))
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
        proseFamily = nil
        codeFamily = nil
    }

    /// Prose/body font according to the selected style preset.
    func proseFont(scale: CGFloat = 1, weight overrideWeight: Font.Weight? = nil) -> Font {
        let nsWeight = overrideWeight.map(Self.nsFontWeight(from:))
        return Font(snapshot.proseNSFont(scale: scale, weight: nsWeight) as CTFont)
    }

    /// Code/monospace font (user-selected family or bundled JetBrains Mono, falling back to system monospaced).
    func codeFont(scale: CGFloat = 1, weight overrideWeight: Font.Weight? = nil) -> Font {
        let nsWeight = overrideWeight.map(Self.nsFontWeight(from:))
        return Font(snapshot.codeNSFont(scale: scale, weight: nsWeight) as CTFont)
    }

    static func nsFontWeight(from weight: Font.Weight) -> NSFont.Weight {
        switch weight {
        case .ultraLight: return .ultraLight
        case .thin: return .thin
        case .light: return .light
        case .regular: return .regular
        case .medium: return .medium
        case .semibold: return .semibold
        case .bold: return .bold
        case .heavy: return .heavy
        case .black: return .black
        default: return .regular
        }
    }

    static func makeCodeFont(
        baseSize: CGFloat,
        scale: CGFloat = 1,
        weight: Font.Weight,
        familyAvailable: Bool,
        family: String? = nil
    ) -> Font {
        let snap = PCodeTypographySnapshot(
            style: .mono,
            size: Double(baseSize),
            weight: .regular,
            codeFamilyAvailable: familyAvailable,
            codeFamily: family)
        return Font(snap.codeNSFont(scale: scale, weight: nsFontWeight(from: weight)) as CTFont)
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

    // MARK: - System font discovery

    /// Discovers all user-facing font families and fixed-pitch (monospace) families in a single pass.
    static func discoverSystemFontFamilies() -> (all: [String], monospace: [String]) {
        let manager = NSFontManager.shared
        let fixedPitchMask = NSFontTraitMask.fixedPitchFontMask.rawValue
        let all = manager.availableFontFamilies
            .filter { !$0.hasPrefix(".") }
            .sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
        let mono = all.filter { family in
            if family == defaultCodeFamily { return true }
            guard let members = manager.availableMembers(ofFontFamily: family),
                  !members.isEmpty
            else { return false }
            return members.contains { entry in
                entry.count >= 4 && ((entry[3] as? UInt) ?? 0) & fixedPitchMask != 0
            }
        }
        return (all: all, monospace: mono)
    }

    /// All installed user-facing font families (excluding hidden `.`-prefixed system fonts).
    static func availableSystemFontFamilies() -> [String] {
        discoverSystemFontFamilies().all
    }

    /// Installed fixed-pitch (monospace) font families suitable for code blocks, diffs, and the docked editor.
    static func availableMonospaceFontFamilies() -> [String] {
        discoverSystemFontFamilies().monospace
    }

    // MARK: - Ghostty terminal typography override

    /// Managed Ghostty config snippet loaded after user config so the docked editor's
    /// terminal surface matches Pcode's code font family and base size.
    static var ghosttyOverrideFilePath: String {
        PCode.statePath("ghostty-typography.conf")
    }

    /// Generates the Ghostty config snippet for the given monospace family.
    static func ghosttyOverrideConfigText(
        codeFamily: String?,
        codeFamilyAvailable: Bool
    ) -> String {
        let resolved = PCodeTypographySnapshot.normalizedFamily(codeFamily)
            ?? (codeFamilyAvailable ? defaultCodeFamily : nil)

        var lines: [String] = [
            "# Managed by Pcode.app typography settings.",
            "font-family = \"\"",
            "font-family-bold = \"\"",
            "font-family-italic = \"\"",
            "font-family-bold-italic = \"\"",
            "font-style = default",
            "font-style-bold = default",
            "font-style-italic = default",
            "font-style-bold-italic = default",
        ]
        if let resolved {
            let safe = sanitizeGhosttyConfigValue(resolved)
            if !safe.isEmpty {
                lines.append("font-family = \"\(safe)\"")
            }
        }
        lines.append("font-size = \(Int(defaultSize))")
        return lines.joined(separator: "\n") + "\n"
    }

    /// Ghostty's config `LineIterator` strips outer quotes verbatim without unescaping backslashes,
    /// so sanitize by dropping control chars and double quotes rather than inserting backslashes.
    private static func sanitizeGhosttyConfigValue(_ value: String) -> String {
        let filtered = value.unicodeScalars.filter {
            $0.value >= 0x20 && $0.value != 0x7F && $0 != "\""
        }
        return String(String.UnicodeScalarView(filtered))
    }

    /// Writes `ghosttyOverrideFilePath` atomically and returns the path if ready.
    @discardableResult
    func syncGhosttyOverrideFile(to path: String? = nil) -> String? {
        let targetPath = path ?? Self.ghosttyOverrideFilePath
        let url = URL(fileURLWithPath: targetPath)
        let dir = url.deletingLastPathComponent()
        let content = Self.ghosttyOverrideConfigText(
            codeFamily: codeFamily,
            codeFamilyAvailable: codeFamilyAvailable)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            if let existing = try? String(contentsOf: url, encoding: .utf8), existing == content {
                return targetPath
            }
            try content.write(to: url, atomically: true, encoding: .utf8)
            return targetPath
        } catch {
            return nil
        }
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
