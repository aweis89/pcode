import AppKit
import GhosttyKit
import SwiftUI

/// pcode's editor, tasks and status bar in a card docked at the bottom of a session pane.
///
/// In Pcode.app, each workspace pane runs `pcode --host` with `PCODE_EDITOR_ONLY=1`
/// directly on the pane's single `Ghostty.SurfaceView`, which is laid out as
/// `PCodeDockCard` below the native transcript (`PCodeMainWindowTranscriptView`).
///
/// - `pcode;ready`: show the card and give it the keyboard.
/// - `pcode;dock-rows;<n>`: the rows its layout needs.
final class PCodeDock: NSObject, ObservableObject {
    @Published private(set) var ready = false
    @Published private(set) var rows = 3

    static let maxRows = 40
    /// The dock's terminal is always this tall and never resizes; the card
    /// shows only its bottom `rows`, so growing is a clip, not a grid resize.
    static let gridRows = maxRows

    /// Vertical padding inside the card, in points: covers the terminal's
    /// bottom padding and leaves symmetric breathing room above the top row
    /// regardless of font size.
    static let cardPadding: CGFloat = 12
    /// The card's top and bottom margins, inside `footprint`.
    static let topMargin: CGFloat = 2
    static let bottomMargin: CGFloat = 8

    /// The card's own height for `rows` terminal rows of `cell` points.
    /// When `surfaceRows` is given and smaller than `gridRows` (e.g. a user
    /// configured `window-padding-y` > `cardPadding / 2`), expands the clip by
    /// the surface's actual vertical padding so the top row is never clipped.
    static func cardHeight(rows: Int, cell: CGFloat, surfaceRows: Int? = nil) -> CGFloat {
        let surfaceHeight = CGFloat(gridRows) * cell + cardPadding
        let padding: CGFloat
        if let surfaceRows, surfaceRows > 0, surfaceRows < gridRows {
            padding = max(cardPadding, surfaceHeight - CGFloat(surfaceRows) * cell)
        } else {
            padding = cardPadding
        }
        return CGFloat(rows) * cell + padding
    }

    /// Everything the card covers at the bottom of pcode's pane for `rows`
    /// rows of `cell` points, margins included.
    static func footprint(rows: Int, cell: CGFloat, surfaceRows: Int? = nil) -> CGFloat {
        cardHeight(rows: rows, cell: cell, surfaceRows: surfaceRows) + topMargin + bottomMargin
    }

    struct CardLayout: Equatable {
        /// The docked terminal surface's frame in the pane (top-left origin).
        let editorRect: CGRect
        /// Distance from the pane's bottom edge to the top of the visible card.
        let cardTopFromBottom: CGFloat
    }

    /// Where a shown dock card's terminal surface sits inside a pane of `size`
    /// (top-left origin), including when narrowed into the `/btw` drawer.
    static func cardLayout(
        in size: CGSize,
        rows: Int,
        cell: CGFloat,
        surfaceRows: Int? = nil,
        editorHeight: CGFloat? = nil,
        drawerOpen: Bool = false,
        drawerFraction: CGFloat = 0.45
    ) -> CardLayout {
        let outerWidth = drawerOpen
            ? max(0, PCodeDrawer.width(in: size.width, fraction: drawerFraction, minWidth: PCodeDrawer.dockedMinWidth) - 1)
            : size.width
        let minX = max(0, min(size.width - outerWidth + bottomMargin, size.width))
        let width = max(0, min(outerWidth - 2 * bottomMargin, size.width - minX))
        let height: CGFloat
        if let editorHeight, editorHeight > 0 {
            height = editorHeight
        } else if cell > 0 {
            height = cardHeight(rows: gridRows, cell: cell)
        } else {
            height = 0
        }
        let maxY = size.height - bottomMargin
        let minY = maxY - height
        let cardTop = cell > 0
            ? bottomMargin + cardHeight(rows: rows, cell: cell, surfaceRows: surfaceRows)
            : 0
        return CardLayout(
            editorRect: CGRect(x: minX, y: minY, width: width, height: height),
            cardTopFromBottom: cardTop
        )
    }

    /// Where the active dock card sits inside `size`, or nil when not shown.
    func cardLayout(in size: CGSize, drawer: PCodeDrawer?) -> CardLayout? {
        guard ready, let editor = surfaceView else { return nil }
        let cell = editor.cellSize.height
        let surfaceRows = editor.surfaceSize.map { Int($0.rows) }
        return Self.cardLayout(
            in: size,
            rows: rows,
            cell: cell,
            surfaceRows: surfaceRows,
            editorHeight: editor.bounds.height,
            drawerOpen: drawer?.isOpen == true,
            drawerFraction: drawer?.fraction ?? 0.45
        )
    }

    weak var surfaceView: Ghostty.SurfaceView?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// From pcode in the editor terminal this card belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "ready":
            guard !ready else { return }
            ready = true
            if let surfaceView { Ghostty.moveFocus(to: surfaceView) }
        case "dock-rows" where parts.count == 2:
            guard let wanted = Int(parts[1]) else { return }
            let clamped = min(Self.maxRows, max(1, wanted))
            if clamped != rows { rows = clamped }
        case "dock-close":
            ready = false
            rows = 3
        default:
            break
        }
    }
}

/// A session pane's content: the native AppKit transcript, the `/btw` drawer,
/// and pcode's single terminal surface laid out as the docked editor card.
///
/// While the `/btw` drawer is open, the drawer spans the full height on the right
/// and reserves the card's live footprint at its bottom; the card narrows into
/// that slot without leaving the view tree, so it keeps keyboard focus and the
/// drawer's content and hint bar never go underneath it.
struct PCodeDockedSurface: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock
    @ObservedObject var drawer: PCodeDrawer
    @State private var sessionID: String?
    @State private var hostID: String?
    var isSplit: Bool

    private static let fallbackLineHeight: CGFloat = 18

    init(surfaceView: Ghostty.SurfaceView, dock: PCodeDock, isSplit: Bool) {
        self.surfaceView = surfaceView
        self.dock = dock
        self.drawer = surfaceView.pcodeDrawer
        self._sessionID = State(initialValue: surfaceView.pcodeSession)
        self._hostID = State(initialValue: surfaceView.pcodeHost)
        self.isSplit = isSplit
    }

    var body: some View {
        let drawerOpen = drawer.isOpen
        GeometryReader { geometry in
            if isSplit || !surfaceView.pcodeIsSessionPane {
                PCodeDrawerSurface(
                    surfaceView: surfaceView,
                    drawer: drawer,
                    isSplit: isSplit)
            } else {
                let cardWidth = drawerOpen
                    ? max(0, PCodeDrawer.width(
                        in: geometry.size.width,
                        fraction: drawer.fraction,
                        minWidth: PCodeDrawer.dockedMinWidth) - 1)
                    : geometry.size.width
                let sid = surfaceView.pcodeSession ?? sessionID
                let hid = surfaceView.pcodeHost ?? hostID
                ZStack(alignment: .bottomTrailing) {
                    PCodeDockedTranscriptLayer(
                        sessionID: sid,
                        hostID: hid,
                        dock: dock,
                        docked: surfaceView,
                        fallbackCell: paneCell,
                        drawerOpen: drawerOpen)

                    PCodeDockedDrawerLayer(
                        surfaceView: surfaceView,
                        drawer: drawer,
                        dock: dock,
                        docked: surfaceView,
                        fallbackCell: paneCell,
                        isSplit: isSplit)

                    // The editor card: full width at the bottom when no drawer is open, or
                    // docked into the bottom of the drawer when /btw is open. Keeping
                    // it at the same structural spot preserves its surface and focus;
                    // width snaps once instead of animating so the PTY doesn't reflow
                    // on every frame.
                    PCodeDockCard(docked: surfaceView, dock: dock, fallbackCell: paneCell, inDrawer: drawerOpen)
                        .frame(width: cardWidth)
                }
                .animation(.spring(response: 0.28, dampingFraction: 0.86), value: dock.ready)
                .onReceive(NotificationCenter.default.publisher(for: PCodeWorkspace.sessionChanged, object: surfaceView)) { _ in
                    sessionID = surfaceView.pcodeSession
                    hostID = surfaceView.pcodeHost
                }
                .onChange(of: ObjectIdentifier(surfaceView)) { _ in
                    sessionID = surfaceView.pcodeSession
                    hostID = surfaceView.pcodeHost
                }
                .onAppear {
                    if sessionID != surfaceView.pcodeSession {
                        sessionID = surfaceView.pcodeSession
                    }
                    if hostID != surfaceView.pcodeHost {
                        hostID = surfaceView.pcodeHost
                    }
                }
            }
        }
    }

    private var paneCell: CGFloat {
        surfaceView.cellSize.height > 0 ? surfaceView.cellSize.height : Self.fallbackLineHeight
    }
}

/// Observes `dock` and `dock.surface` so the main-window native transcript reserves
/// the exact live bottom footprint of `PCodeDockCard` as the editor grows or zooms.
private struct PCodeDockedTranscriptLayer: View {
    let sessionID: String?
    var hostID: String? = nil
    @ObservedObject var dock: PCodeDock
    @ObservedObject var docked: Ghostty.SurfaceView
    var fallbackCell: CGFloat
    var drawerOpen: Bool

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        let bottomInset: CGFloat = (!drawerOpen && dock.ready)
            ? PCodeDock.footprint(rows: dock.rows, cell: cell, surfaceRows: surfaceRows)
            : 0
        PCodeMainWindowTranscriptView(
            sessionID: sessionID,
            hostID: hostID,
            bottomContentInset: bottomInset,
            composerSurface: docked)
    }
}

/// Observes the dock's surface so font-size changes update the drawer's reserved
/// bottom footprint immediately in lockstep with `PCodeDockCard`.
private struct PCodeDockedDrawerLayer: View {
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var drawer: PCodeDrawer
    @ObservedObject var dock: PCodeDock
    @ObservedObject var docked: Ghostty.SurfaceView
    var fallbackCell: CGFloat
    var isSplit: Bool

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        PCodeDrawerSurface(
            surfaceView: surfaceView,
            drawer: drawer,
            isSplit: isSplit,
            drawsSurface: false,
            dockFootprint: PCodeDock.footprint(rows: dock.rows, cell: cell, surfaceRows: surfaceRows))
    }
}

/// The docked editor's card, with a backstop over its whole footprint.
///
/// Its own view so it observes the dock's terminal: the card's height is in
/// that terminal's cells, and pcode reckons the pane rows the card covers from
/// them, so a font change there must resize the card at once.
private struct PCodeDockCard: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var docked: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock
    var fallbackCell: CGFloat
    var inDrawer: Bool

    private static let cornerRadius: CGFloat = 12

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        ZStack(alignment: .bottom) {
            // The card's whole footprint, margins included, takes the clicks
            // the card itself doesn't: the pane rows under it are hidden, and
            // a click there would start a selection in them. Leave the leading
            // 4pt clear when inside the drawer so the drawer's 8pt drag handle
            // works alongside the card.
            Color.clear
                .frame(height: PCodeDock.footprint(rows: dock.rows, cell: cell, surfaceRows: surfaceRows))
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0))
                .padding(.leading, inDrawer ? 4 : 0)
                .allowsHitTesting(dock.ready)
            card(cell: cell, surfaceRows: surfaceRows)
        }
        // Only the card's clip animates as `dock.rows` changes, never its width.
        .animation(.easeOut(duration: 0.12), value: dock.rows)
    }

    private func card(cell: CGFloat, surfaceRows: Int?) -> some View {
        // The card is the dock's terminal itself, edge to edge: pcode tints its
        // background (OSC 11) a shade off the pane's, and its own padding takes
        // that tint, so the whole card is one raised surface. The backdrop only
        // gives the shadow its shape.
        //
        // The terminal keeps a fixed `gridRows` height, bottom-aligned in the
        // card and clipped: pcode draws its layout at the bottom of it, so a
        // taller layout is already drawn when the card grows to show it.
        return ZStack(alignment: .bottom) {
            Ghostty.SurfaceWrapper(surfaceView: docked, isSplit: false)
                .id(ObjectIdentifier(docked))
                .frame(height: PCodeDock.cardHeight(rows: PCodeDock.gridRows, cell: cell))
        }
            .frame(height: PCodeDock.cardHeight(rows: dock.rows, cell: cell, surfaceRows: surfaceRows), alignment: .bottom)
            .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Self.cornerRadius)
                    .strokeBorder(inDrawer ? ghostty.config.splitDividerColor : .clear, lineWidth: 1)
                    .allowsHitTesting(false)
            )
            .background(
                RoundedRectangle(cornerRadius: Self.cornerRadius)
                    .fill(ghostty.config.backgroundColor)
                    .shadow(
                        color: inDrawer ? .clear : .black.opacity(0.28),
                        radius: inDrawer ? 0 : 18,
                        x: 0,
                        y: inDrawer ? 0 : -2)
            )
            .padding(.horizontal, PCodeDock.bottomMargin)
            .padding(.top, PCodeDock.topMargin)
            .padding(.bottom, PCodeDock.bottomMargin)
            .opacity(dock.ready ? 1 : 0)
            .allowsHitTesting(dock.ready)
    }
}
