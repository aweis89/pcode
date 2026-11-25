import AppKit
import GhosttyKit
import SwiftUI

/// pcode's editor completion menu (`/` commands, `@` files), drawn natively
/// over the terminal instead of in its grid (`pcode.native_completions`).
///
/// Display only: the list never takes the keyboard. pcode keeps every key
/// (typing, arrows, Tab, Enter, Escape) and runs its completers itself; it
/// only tells us what its menu holds after each redraw. APCs (protocol 3):
///
/// - `pcode;popup?` is answered `pcode;popup;4` in Zig (`stream_handler.zig`).
/// - `pcode;complete;<token>`, from the terminal: connect to
///   `~/.local/state/pcode/popups/<token>.sock` and say `pcode-complete 1\n`.
///   One per pcode session; a new one replaces it.
///
/// Then newline-delimited JSON: pcode sends the menu to show (`Menu`) or
/// `{"hide": true}`, and we send `{"accept": <row>, "seq": <menu's seq>}`
/// when a row is clicked. pcode closing the socket hides the list.
final class PCodeCompletion: ObservableObject {
    struct Item: Decodable {
        let text: String
        let meta: String
    }

    struct Menu: Decodable {
        let seq: Int
        let items: [Item]
        /// Into `items`, which are `total` completions' window from `offset`.
        let selected: Int?
        let offset: Int
        let total: Int
        /// The completed word's start, in cells from the cursor (zero or less).
        let dx: Int
        /// Rows from the editor box's top down to the cursor's row.
        let dy: Int
        let style: PCodePickStyle?
    }

    static let hello = Data("pcode-complete 1\n".utf8)

    /// The menu on show, or nil while hidden.
    @Published private(set) var menu: Menu?

    weak var surfaceView: Ghostty.SurfaceView?
    private var connection: PCodePicker.Connection?
    private var themeCache: (config: Ghostty.Config, fonts: PCodePickFonts?, style: PCodePickStyle?,
                             theme: PCodePickTheme)?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// From pcode in the terminal this list belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 2, parts[0] == "complete", PCode.valid(parts[1], length: 16...16, hex: true)
        else { return }
        start(token: parts[1])
    }

    private func start(token: String) {
        stop()
        let connection = PCodePicker.Connection(hello: Self.hello)
        self.connection = connection
        connection.start(
            path: PCode.statePath("popups/\(token).sock"),
            onLine: { [weak self, weak connection] line in
                guard let self, let connection, self.connection === connection else { return }
                self.receive(line)
            },
            onClosed: { [weak self, weak connection] in
                guard let self, let connection, self.connection === connection else { return }
                self.stop()
            })
    }

    func stop() {
        connection?.close()
        connection = nil
        menu = nil
    }

    private func receive(_ line: Data) {
        if let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
           object["hide"] as? Bool == true {
            menu = nil
            return
        }
        // A menu we cannot read (a newer pcode) is hidden rather than half shown.
        menu = try? JSONDecoder().decode(Menu.self, from: line)
        guard menu != nil else { return }
        // The cursor is read as the list is laid out, but pcode's frame comes
        // over the pty, parsed on another thread, and may land just after this
        // menu: place the list again a frame later.
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.replaceDelay) { [weak self] in
            guard let self, self.menu != nil else { return }
            self.objectWillChange.send()
        }
    }

    static let replaceDelay: TimeInterval = 0.03

    /// A row was clicked: pcode takes it, as Enter on it would.
    func accept(_ row: Int) {
        restoreFocus()
        guard let menu, let connection, menu.items.indices.contains(row),
              let data = try? JSONSerialization.data(withJSONObject: ["accept": row, "seq": menu.seq])
        else { return }
        connection.send(data + Data([0x0A]))
    }

    /// A click anywhere on the list must leave the keyboard with pcode's terminal.
    func restoreFocus() {
        if let editor = editorSurface, editor.window?.firstResponder !== editor {
            Ghostty.moveFocus(to: editor)
        }
    }

    /// SPIKE: the terminal pcode's editor runs in: its docked one while shown.
    private var editorSurface: Ghostty.SurfaceView? {
        if let dock = surfaceView?.existingPCodeDock, dock.ready, let docked = dock.surface { return docked }
        return surfaceView
    }

    func theme(config: Ghostty.Config, fonts: PCodePickFonts?) -> PCodePickTheme {
        let style = menu?.style
        if let cache = themeCache, cache.config === config, cache.fonts == fonts, cache.style == style {
            return cache.theme
        }
        let theme = PCodePickTheme(base: PCodePickTheme.Base(config), style: style, fonts: fonts)
        themeCache = (config, fonts, style, theme)
        return theme
    }

    /// The cursor's cell in the terminal view, top-left origin, in points.
    ///
    /// SPIKE: with a dock shown, the dock's cursor, in this terminal's
    /// coordinates (below its bottom edge): the list layer spans both.
    func cursorCell() -> CGRect? {
        guard let surfaceView else { return nil }
        guard let editor = editorSurface, editor !== surfaceView else { return surfaceView.pcodeCursorCell }
        guard let cell = editor.pcodeCursorCell else { return nil }
        let docked = editor.convert(editor.bounds, to: nil)
        let pane = surfaceView.convert(surfaceView.bounds, to: nil)
        // Window coordinates have a bottom-left origin; the cell's is top-left.
        return cell.offsetBy(dx: docked.minX - pane.minX, dy: pane.maxY - docked.maxY)
    }
}

/// The completion list, hung over pcode's editor box from where the word
/// being completed starts; below the cursor if there is no room above.
struct PCodeCompletionLayer: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var completion: PCodeCompletion

    static let maxRows = 10
    static let margin: CGFloat = 4
    static let rowPadding: CGFloat = 3
    static let listPadding: CGFloat = 4
    static let textInset: CGFloat = 8
    static let cornerRadius: CGFloat = 8
    /// Terminal columns a row's text and meta may take before they are cut.
    static let maxTextColumns = 48
    static let maxMetaColumns = 56

    var body: some View {
        GeometryReader { geometry in
            if let menu = completion.menu, !menu.items.isEmpty, let cursor = completion.cursorCell() {
                let fonts = PCodePickFonts.of(completion.surfaceView?.surface)
                let theme = completion.theme(config: ghostty.config, fonts: fonts)
                // The terminal's own size: with the inspector open, the layer is wider.
                let bounds = completion.surfaceView?.bounds.size ?? geometry.size
                let size = CGSize(
                    width: min(geometry.size.width, bounds.width), height: min(geometry.size.height, bounds.height))
                let frame = Self.frame(menu, cursor: cursor, fonts: fonts, in: size)
                list(menu, theme: theme, rowHeight: Self.rowHeight(cursor: cursor))
                    .frame(width: frame.width, height: frame.height)
                    .background(ghostty.config.backgroundColor)
                    .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
                    .overlay(
                        RoundedRectangle(cornerRadius: Self.cornerRadius)
                            .strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
                    .shadow(color: .black.opacity(0.35), radius: 10, x: 0, y: 4)
                    .simultaneousGesture(TapGesture().onEnded { completion.restoreFocus() })
                    .environment(\.colorScheme, colorScheme)
                    .offset(x: frame.minX, y: frame.minY)
            }
        }
    }

    private func list(_ menu: PCodeCompletion.Menu, theme: PCodePickTheme, rowHeight: CGFloat) -> some View {
        let textWidth = Self.columns(menu.items.map(\.text), max: Self.maxTextColumns)
            * (theme.fonts?.advance ?? 8)
        return ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(menu.items.enumerated()), id: \.offset) { index, item in
                        row(item, chosen: index == menu.selected, theme: theme, textWidth: textWidth)
                            .frame(height: rowHeight)
                            .id(index)
                            .contentShape(Rectangle())
                            .onTapGesture { completion.accept(index) }
                    }
                }
                .padding(Self.listPadding)
            }
            .onAppear { scroll(proxy, to: menu.selected) }
            .onChange(of: menu.seq) { _ in scroll(proxy, to: menu.selected) }
        }
    }

    /// Keep the selected row in view; with none (a fresh or refiltered list), show the top.
    private func scroll(_ proxy: ScrollViewProxy, to row: Int?) {
        proxy.scrollTo(row ?? 0)
    }

    private func row(
        _ item: PCodeCompletion.Item, chosen: Bool, theme: PCodePickTheme, textWidth: CGFloat
    ) -> some View {
        let text = chosen ? theme.selectedText.color : nil
        return HStack(spacing: 12) {
            Text(item.text)
                .font(theme.labelFont(fallbackSize: 13))
                .foregroundStyle(text ?? theme.foreground)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: textWidth, alignment: .leading)
            if !item.meta.isEmpty {
                Text(item.meta)
                    .font(theme.smallFont(fallbackSize: 12))
                    .foregroundStyle(text.map { $0.opacity(0.8) } ?? theme.muted)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Self.textInset)
        .frame(maxHeight: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 5)
                .fill(chosen ? theme.selectedBackground.color : Color.clear))
    }

    /// The widest of `texts` in terminal columns (characters, near enough), up to `max`.
    static func columns(_ texts: [String], max limit: Int) -> CGFloat {
        CGFloat(min(texts.map(\.count).max() ?? 0, limit))
    }

    static func rowHeight(cursor: CGRect) -> CGFloat {
        cursor.height + 2 * rowPadding
    }

    /// Where the list goes in a view of `size`: above the editor box, starting
    /// under the completed word's first cell, kept inside the view.
    static func frame(_ menu: PCodeCompletion.Menu, cursor: CGRect, fonts: PCodePickFonts?, in size: CGSize) -> CGRect {
        let advance = fonts?.advance ?? cursor.width
        let text = columns(menu.items.map(\.text), max: maxTextColumns) * advance
        let metaColumns = columns(menu.items.map(\.meta), max: maxMetaColumns)
        let meta = metaColumns > 0 ? 12 + metaColumns * advance * PCodePickFonts.smallScale : 0
        let width = min(max(text + meta + 2 * (textInset + listPadding) + 4, 160), size.width - 2 * margin)
        let wanted = CGFloat(min(menu.items.count, maxRows)) * rowHeight(cursor: cursor) + 2 * listPadding
        let left = cursor.minX + CGFloat(menu.dx) * cursor.width - textInset - listPadding
        let x = min(max(left, margin), size.width - width - margin)
        // Above the editor box's top row, else below the cursor's.
        let above = cursor.minY - CGFloat(menu.dy) * cursor.height - margin
        let below = cursor.maxY + margin
        let roomAbove = above - margin
        let roomBelow = size.height - below - margin
        if wanted <= roomAbove || roomAbove >= roomBelow {
            let height = max(0, min(wanted, roomAbove))
            return CGRect(x: x, y: above - height, width: width, height: height)
        }
        return CGRect(x: x, y: below, width: width, height: max(0, min(wanted, roomBelow)))
    }

    /// The list's own scheme: its background is the terminal's, not the system's.
    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}
