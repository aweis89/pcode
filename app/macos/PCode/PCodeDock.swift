import AppKit
import GhosttyKit
import SwiftUI

/// SPIKE: pcode's editor, tasks and status bar in a card docked under its terminal.
///
/// pcode (run with `PCODE_DOCK=1`) asks for the dock, and runs its whole
/// prompt_toolkit editor on the dock's terminal, the same way it runs an
/// overlay popup: the dock's terminal runs `pcode.popup_bridge <token>`, which
/// hands its tty to pcode. pcode's own terminal is left as scrollback only.
///
/// - `pcode;dock;<token>`, from the terminal: open the dock (unseen until ready).
/// - `pcode;dock-close`, from the terminal: remove it.
/// - `pcode;ready`, from the dock's terminal: show it and give it the keyboard.
/// - `pcode;dock-rows;<n>`, from the dock's terminal: the rows its layout needs.
/// - Anything else from the dock's terminal is handled as if pcode had sent it
///   in its own terminal (popups, the /btw drawer, toasts, its session id):
///   pcode now writes everything to the terminal its editor runs on.
///
/// The dock ending (pcode exits and its bridge with it) removes it.
final class PCodeDock: NSObject, ObservableObject, PCodeOverlayOwner {
    @Published private(set) var surface: Ghostty.SurfaceView?
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

    weak var surfaceView: Ghostty.SurfaceView?
    private var closeObserver: NSObjectProtocol?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    deinit {
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
        if let surface { PCode.retire(surface) }
    }

    /// From pcode in the terminal this dock belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "dock" where parts.count == 2:
            guard PCode.valid(parts[1], length: 16...16, hex: true) else { return }
            open(token: parts[1])
        case "dock-close":
            close()
        default:
            break
        }
    }

    /// From the dock's own terminal, where pcode's editor now runs.
    func handleFromOverlay(_ payload: String, from overlay: Ghostty.SurfaceView) {
        guard overlay === surface else { return }
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "ready":
            guard !ready else { return }
            ready = true
            Ghostty.moveFocus(to: overlay)
        case "dock-rows" where parts.count == 2:
            guard let wanted = Int(parts[1]) else { return }
            let clamped = min(Self.maxRows, max(1, wanted))
            if clamped != rows { rows = clamped }
        case "dock", "dock-close", "done", "title", "hints", "focus":
            break
        default:
            surfaceView?.pcodeEvent(payload)
        }
    }

    func close() {
        guard let surface else { return }
        let hadFocus = Self.hasFocus(surface)
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
        closeObserver = nil
        ready = false
        rows = 3
        self.surface = nil
        PCode.retire(surface)
        if hadFocus, let surfaceView { Ghostty.moveFocus(to: surfaceView) }
    }

    private func open(token: String) {
        close()
        guard let python = PCode.entryPython(PCode.statePath("popups/\(token).json")) else { return }
        let command = PCode.pythonCommand(python, module: "pcode.popup_bridge", args: [token])
        guard let view = PCode.makeOverlaySurface(over: surfaceView, command: command, owner: self) else { return }
        // The bridge exits a moment after pcode lets go of it (or dies).
        closeObserver = NotificationCenter.default.addObserver(
            forName: Ghostty.Notification.ghosttyCloseSurface, object: view, queue: .main
        ) { [weak self, weak view] _ in
            DispatchQueue.main.async {
                guard let self, let view, self.surface === view else { return }
                self.close()
            }
        }
        surface = view
    }

    private static func hasFocus(_ view: NSView) -> Bool {
        guard let responder = view.window?.firstResponder as? NSView else { return false }
        return responder === view || responder.isDescendant(of: view)
    }
}

/// The terminal (with its /btw drawer) above pcode's docked editor card.
///
/// An overlay, not a stack: pcode's terminal keeps its full height, and the
/// card covers its bottom rows. pcode keeps those rows clear (a scroll region
/// above the card, `pcode/dock.py`), so growing the editor scrolls the
/// transcript like any output, which glides, instead of resizing the pane
/// (a reflow and a redraw per step of the card's animation).
///
/// While the /btw drawer is open, the drawer spans the full height on the right
/// and reserves the card's live footprint at its bottom; the card narrows into
/// that slot without leaving the view tree, so it keeps keyboard focus and the
/// drawer's viewer and hint bar never go underneath it.
struct PCodeDockedSurface: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock
    @ObservedObject var drawer: PCodeDrawer
    var isSplit: Bool

    private static let fallbackLineHeight: CGFloat = 18

    init(surfaceView: Ghostty.SurfaceView, dock: PCodeDock, isSplit: Bool) {
        self.surfaceView = surfaceView
        self.dock = dock
        self.drawer = surfaceView.pcodeDrawer
        self.isSplit = isSplit
    }

    var body: some View {
        let drawerOpen = drawer.isOpen
        GeometryReader { geometry in
            let cardWidth = drawerOpen
                ? max(0, PCodeDrawer.width(
                    in: geometry.size.width,
                    fraction: drawer.fraction,
                    minWidth: PCodeDrawer.dockedMinWidth) - 1)
                : geometry.size.width
            ZStack(alignment: .bottomTrailing) {
                // pcode's terminal, full height under the card and drawer: its size
                // never follows either one.
                PCodeDrawerSurface(
                    surfaceView: surfaceView, drawer: drawer, isSplit: isSplit,
                    drawsDrawer: false)
                // The /btw drawer over the right side, full height, reserving the
                // card's live footprint at its bottom so the viewer and hint bar
                // always end above the card (observing the dock's surface so font
                // zoom updates the footprint immediately).
                if let docked = dock.surface {
                    PCodeDockedDrawerLayer(
                        surfaceView: surfaceView,
                        drawer: drawer,
                        dock: dock,
                        docked: docked,
                        fallbackCell: paneCell,
                        isSplit: isSplit)
                } else {
                    PCodeDrawerSurface(
                        surfaceView: surfaceView, drawer: drawer, isSplit: isSplit,
                        drawsSurface: false)
                }
                // The card: full width at the bottom when no drawer is open, or
                // docked into the bottom of the drawer when /btw is open. Keeping
                // it at the same structural spot preserves its surface and focus;
                // width snaps once instead of animating so the PTY doesn't reflow
                // on every frame.
                if let docked = dock.surface {
                    PCodeDockCard(docked: docked, dock: dock, fallbackCell: paneCell, inDrawer: drawerOpen)
                        .frame(width: cardWidth)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.spring(response: 0.28, dampingFraction: 0.86), value: dock.ready)
        }
    }

    private var paneCell: CGFloat {
        surfaceView.cellSize.height > 0 ? surfaceView.cellSize.height : Self.fallbackLineHeight
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
                    .strokeBorder(ghostty.config.splitDividerColor, lineWidth: inDrawer ? 1 : 0)
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
