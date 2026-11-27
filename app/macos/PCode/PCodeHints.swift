import SwiftUI

/// One key hint along a pcode panel's bottom edge: a keycap and what it does.
///
/// pcode decides every panel's hints (`pcode.popup_hints`) and sends them
/// with each keycap already drawn as macOS writes it (`⌃Y`, `⌘R`, `↩`,
/// `esc`); we only lay them out. They come in a pick list's payload
/// (`hints`), or as `pcode;hints;<base64 JSON>` from a popup's own terminal
/// (popup protocol 4); the /btw drawer builds its own (`PCodeBtwHints`), from
/// the same rules (`POPUPS["btw_drawer"]`). `keep` marks the hints that stay
/// when the panel is too narrow for all of them: the rest go from the end.
struct PCodeHint: Decodable, Equatable, Hashable {
    static let maxCount = 12
    static let keyLimit = 16
    static let labelLimit = 40

    let key: String
    let label: String
    let keep: Bool

    init(key: String, label: String, keep: Bool) {
        self.key = PCodeHint.plain(key, limit: Self.keyLimit)
        self.label = PCodeHint.plain(label, limit: Self.labelLimit)
        self.keep = keep
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            key: try container.decode(String.self, forKey: .key),
            label: try container.decode(String.self, forKey: .label),
            keep: (try? container.decode(Bool.self, forKey: .keep)) ?? false)
    }

    enum CodingKeys: String, CodingKey {
        case key, label, keep
    }

    /// `pcode;hints;<base64>`'s payload, or nil if it is not a hint list.
    /// Empty is a list too: the panel has no hints now.
    static func list(fromBase64 encoded: String) -> [PCodeHint]? {
        // Base64 grows text by a third; this is far more than pcode sends.
        guard encoded.count <= 8192,
              let data = Data(base64Encoded: encoded),
              let hints = try? JSONDecoder().decode([PCodeHint].self, from: data) else { return nil }
        return Array(hints.filter { !$0.key.isEmpty }.prefix(maxCount))
    }

    /// `text` on one line without control characters, cut to `limit`.
    private static func plain(_ text: String, limit: Int) -> String {
        String(PCodePopup.plainTitle(text).prefix(limit))
    }

    /// `hints`, then as many shorter lists as it takes, each without the last
    /// hint that need not stay: what `PCodeHintBar` tries, widest first.
    static func narrowings(_ hints: [PCodeHint]) -> [[PCodeHint]] {
        var result = [hints]
        var shown = hints
        while let last = shown.lastIndex(where: { !$0.keep }) {
            shown.remove(at: last)
            result.append(shown)
        }
        return result
    }
}

/// A row of key hints, as the native picker has always drawn its footer.
///
/// It drops the hints that need not stay, from the end, until the rest fit.
struct PCodeHintBar: View {
    @ObservedObject private var typography = PCodeTypography.shared
    let hints: [PCodeHint]
    let fonts: PCodePickFonts?
    let muted: Color

    private var liveFonts: PCodePickFonts {
        PCodePickFonts(typography: typography.snapshot)
    }

    var body: some View {
        ViewThatFits(in: .horizontal) {
            ForEach(Array(PCodeHint.narrowings(hints).enumerated()), id: \.offset) { _, shown in
                row(shown)
            }
        }
        .font(PCodePickFonts.small(liveFonts, fallbackSize: 11))
        .foregroundStyle(muted)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .accessibilityElement(children: .combine)
    }

    private func row(_ shown: [PCodeHint]) -> some View {
        HStack(spacing: 14) {
            ForEach(Array(shown.enumerated()), id: \.offset) { _, hint in
                HStack(spacing: 4) {
                    Text(hint.key)
                        .font(PCodePickFonts.small(liveFonts, fallbackSize: 11, weight: .semibold))
                        .padding(.horizontal, 4)
                        .background(RoundedRectangle(cornerRadius: 3).fill(Color.primary.opacity(0.08)))
                    Text(hint.label)
                }
                .fixedSize()
            }
        }
        .lineLimit(1)
    }
}
