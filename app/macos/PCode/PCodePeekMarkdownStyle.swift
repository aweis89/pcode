import SwiftUI
import Textual

/// Compact Textual style for Peek transcripts: a preview card, not a full-page document.
///
/// Colors are `primary`-relative so they follow the `colorScheme` Peek sets from the
/// terminal background; fonts come from `PCodeTypography` so inline code and fenced blocks
/// stay in the app's bundled monospace font instead of falling back to SF Mono.
struct PCodePeekMarkdownStyle: StructuredText.Style {
    let codeBackground: Color
    let inheritInlineCodeFont: Bool
    let baseSize: CGFloat
    let codeFamilyAvailable: Bool
    let inlineCodeScale: CGFloat
    let codeBlockFont: Font
    let codeLabelFont: Font
    let emphasisWeight: Font.Weight
    let baseWeight: Font.Weight

    var inlineStyle: InlineStyle {
        Self.makeInlineStyle(
            inheritCodeFont: inheritInlineCodeFont,
            codeFont: PCodeTypography.makeCodeFont(
                baseSize: baseSize,
                scale: inlineCodeScale,
                weight: baseWeight,
                familyAvailable: codeFamilyAvailable),
            emphasisWeight: emphasisWeight)
    }

    var headingStyle: Heading {
        Heading(
            inheritInlineCodeFont: inheritInlineCodeFont,
            baseSize: baseSize,
            codeFamilyAvailable: codeFamilyAvailable,
            inlineCodeScale: inlineCodeScale,
            emphasisWeight: emphasisWeight)
    }
    var paragraphStyle: Paragraph { Paragraph() }
    var blockQuoteStyle: BlockQuote { BlockQuote() }
    var codeBlockStyle: CodeBlock {
        CodeBlock(background: codeBackground, codeFont: codeBlockFont, labelFont: codeLabelFont)
    }
    var listItemStyle: StructuredText.DefaultListItemStyle { .default(markerSpacing: .fontScaled(0.4)) }
    var unorderedListMarker: StructuredText.HierarchicalSymbolListMarker {
        .hierarchical(.disc, .circle, .square)
    }
    var orderedListMarker: StructuredText.DecimalListMarker { .decimal }
    var tableStyle: Table { Table() }
    var tableCellStyle: TableCell { TableCell(emphasisWeight: emphasisWeight, baseWeight: baseWeight) }
    var thematicBreakStyle: ThematicBreak { ThematicBreak() }

    static let inlineCodeFill = DynamicColor(
        light: Color.black.opacity(0.06),
        dark: Color.white.opacity(0.10))
    static let hairline = Color.primary.opacity(0.12)

    fileprivate static func makeInlineStyle(
        inheritCodeFont: Bool,
        codeFont: Font,
        emphasisWeight: Font.Weight
    ) -> InlineStyle {
        let base = InlineStyle()
            .strong(.fontWeight(emphasisWeight))
            .link(.foregroundColor(Color.accentColor))
        if inheritCodeFont {
            return base.code(.backgroundColor(Self.inlineCodeFill))
        }
        return base.code(.font(codeFont), .backgroundColor(Self.inlineCodeFill))
    }

    struct Heading: StructuredText.HeadingStyle {
        let inheritInlineCodeFont: Bool
        let baseSize: CGFloat
        let codeFamilyAvailable: Bool
        let inlineCodeScale: CGFloat
        let emphasisWeight: Font.Weight
        private static let fontScales: [CGFloat] = [1.3, 1.15, 1.05, 1, 1, 1]

        func makeBody(configuration: Configuration) -> some View {
            let level = min(max(configuration.headingLevel, 1), 6)
            let scale = Self.fontScales[level - 1]
            let styled = configuration.label
                .textual.fontScale(scale)
                .fontWeight(emphasisWeight)
                .textual.inlineStyle(
                    PCodePeekMarkdownStyle.makeInlineStyle(
                        inheritCodeFont: inheritInlineCodeFont,
                        codeFont: PCodeTypography.makeCodeFont(
                            baseSize: baseSize,
                            scale: scale * inlineCodeScale,
                            weight: emphasisWeight,
                            familyAvailable: codeFamilyAvailable),
                        emphasisWeight: emphasisWeight)
                )
                .textual.blockSpacing(.fontScaled(top: 0.9, bottom: 0.35))
            if level >= 4 {
                styled.foregroundStyle(Color.secondary)
            } else {
                styled
            }
        }
    }

    struct Paragraph: StructuredText.ParagraphStyle {
        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .textual.lineSpacing(.fontScaled(0.18))
                .textual.blockSpacing(.fontScaled(top: 0, bottom: 0.6))
        }
    }

    struct BlockQuote: StructuredText.BlockQuoteStyle {
        func makeBody(configuration: Configuration) -> some View {
            HStack(spacing: 0) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.primary.opacity(0.18))
                    .frame(width: 3)
                configuration.label
                    .foregroundStyle(.secondary)
                    .padding(.leading, 10)
            }
            .textual.blockSpacing(.fontScaled(top: 0, bottom: 0.6))
        }
    }

    struct CodeBlock: StructuredText.CodeBlockStyle {
        let background: Color
        let codeFont: Font
        let labelFont: Font

        func makeBody(configuration: Configuration) -> some View {
            VStack(alignment: .leading, spacing: 0) {
                if let language = configuration.languageHint, !language.isEmpty {
                    Text(language)
                        .font(labelFont)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.top, 6)
                }
                // Overflow scrolls long lines horizontally (Textual's default overflow mode).
                Overflow {
                    configuration.label
                        .font(codeFont)
                        .textual.lineSpacing(.fontScaled(0.2))
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
            )
            .textual.blockSpacing(.fontScaled(top: 0.2, bottom: 0.7))
        }
    }

    struct Table: StructuredText.TableStyle {
        func makeBody(configuration: Configuration) -> some View {
            // Wide tables scroll sideways (up to 1.5x the card) instead of wrapping word by word.
            Overflow { state in
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
                    .textual.tableOverlay { layout in
                        Canvas { context, _ in
                            for divider in layout.dividers() {
                                context.fill(Path(divider), with: .color(PCodePeekMarkdownStyle.hairline))
                            }
                        }
                    }
                    .padding(1)
                    .overlay(
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .strokeBorder(PCodePeekMarkdownStyle.hairline, lineWidth: 1)
                    )
                    .frame(maxWidth: state.containerWidth.map { $0 * 1.5 }, alignment: .leading)
            }
            .textual.tableCellSpacing(horizontal: 1, vertical: 1)
            .textual.blockSpacing(.fontScaled(top: 0, bottom: 0.7))
        }
    }

    struct TableCell: StructuredText.TableCellStyle {
        let emphasisWeight: Font.Weight
        let baseWeight: Font.Weight

        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .fontWeight(configuration.row == 0 ? emphasisWeight : baseWeight)
                .padding(.vertical, 4)
                .padding(.horizontal, 8)
                .textual.lineSpacing(.fontScaled(0.15))
        }
    }

    struct ThematicBreak: StructuredText.ThematicBreakStyle {
        func makeBody(configuration _: Configuration) -> some View {
            Rectangle()
                .fill(PCodePeekMarkdownStyle.hairline)
                .frame(height: 1)
                .textual.blockSpacing(.fontScaled(top: 0.6, bottom: 0.6))
        }
    }
}
