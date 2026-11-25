import SwiftUI
import Textual

/// Compact Textual style for Peek transcripts: a preview card, not a full-page document.
///
/// Colors are `primary`-relative so they follow the `colorScheme` Peek sets from the
/// terminal background; only the code-card fill comes from the caller, to match the
/// overlay's existing chrome.
struct PCodePeekMarkdownStyle: StructuredText.Style {
    let codeBackground: Color

    var inlineStyle: InlineStyle {
        InlineStyle()
            .code(.monospaced, .fontScale(0.9), .backgroundColor(Self.inlineCodeFill))
            .strong(.fontWeight(.semibold))
            .link(.foregroundColor(Color.accentColor))
    }

    var headingStyle: Heading { Heading() }
    var paragraphStyle: Paragraph { Paragraph() }
    var blockQuoteStyle: BlockQuote { BlockQuote() }
    var codeBlockStyle: CodeBlock { CodeBlock(background: codeBackground) }
    var listItemStyle: StructuredText.DefaultListItemStyle { .default(markerSpacing: .fontScaled(0.4)) }
    var unorderedListMarker: StructuredText.HierarchicalSymbolListMarker {
        .hierarchical(.disc, .circle, .square)
    }
    var orderedListMarker: StructuredText.DecimalListMarker { .decimal }
    var tableStyle: Table { Table() }
    var tableCellStyle: TableCell { TableCell() }
    var thematicBreakStyle: ThematicBreak { ThematicBreak() }

    static let inlineCodeFill = DynamicColor(
        light: Color.black.opacity(0.06),
        dark: Color.white.opacity(0.10))
    static let hairline = Color.primary.opacity(0.12)

    struct Heading: StructuredText.HeadingStyle {
        private static let fontScales: [CGFloat] = [1.3, 1.15, 1.05, 1, 1, 1]

        func makeBody(configuration: Configuration) -> some View {
            let level = min(max(configuration.headingLevel, 1), 6)
            configuration.label
                .textual.fontScale(Self.fontScales[level - 1])
                .fontWeight(.semibold)
                .foregroundStyle(level >= 4 ? Color.secondary : Color.primary)
                .textual.blockSpacing(.fontScaled(top: 0.9, bottom: 0.35))
        }
    }

    struct Paragraph: StructuredText.ParagraphStyle {
        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .textual.lineSpacing(.fontScaled(0.15))
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

        func makeBody(configuration: Configuration) -> some View {
            VStack(alignment: .leading, spacing: 0) {
                if let language = configuration.languageHint, !language.isEmpty {
                    Text(language)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.top, 6)
                }
                // Overflow scrolls long lines horizontally (Textual's default overflow mode).
                Overflow {
                    configuration.label
                        .textual.lineSpacing(.fontScaled(0.2))
                        .textual.fontScale(0.9)
                        .fixedSize(horizontal: false, vertical: true)
                        .monospaced()
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 1)
            )
            .textual.blockSpacing(.fontScaled(top: 0.2, bottom: 0.7))
        }
    }

    struct Table: StructuredText.TableStyle {
        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .textual.tableCellSpacing(horizontal: 1, vertical: 1)
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
                        .stroke(PCodePeekMarkdownStyle.hairline, lineWidth: 1)
                )
                .textual.blockSpacing(.fontScaled(top: 0, bottom: 0.7))
        }
    }

    struct TableCell: StructuredText.TableCellStyle {
        func makeBody(configuration: Configuration) -> some View {
            configuration.label
                .fontWeight(configuration.row == 0 ? .semibold : .regular)
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
