import AppKit
import GhosttyKit
import SwiftUI

/// A drawer over the right of a terminal surface that shows a pcode /btw thread.
///
/// pcode drives it with APCs from the terminal (see `PCode`):
///
/// - `pcode;btw;<host>;<thread>`: show the thread (`host` is 8 lowercase hex
///   digits, `thread` 1-64 plain characters), starting a viewer if needed.
/// - `pcode;close`: close the drawer and end its viewer.
/// - `pcode;hide`: hide the drawer but keep its viewer running.
/// - `pcode;show`: show it again, if it has a viewer.
/// - `pcode;focus`, from the viewer's own terminal while it has the keyboard:
///   give the keyboard back to the terminal.
/// - `pcode;hints;<base64 JSON>`, from the viewer's own terminal: the keys
///   pcode's editor answers the thread with, in a footer pinned below it
///   (`PCodeHint`; hidden while empty).
///
/// `pcode;?` is answered in Zig (`stream_handler.zig`) with `pcode;ok;1`.
/// Popups (`pcode;popup;...`) are `PCodePopup`'s.
///
/// The drawer is a second terminal surface, so it has this terminal's font,
/// size and colors. It runs `python -m pcode.btw_tail <host> <thread>` with
/// the session host's own interpreter, read from the host's entry file; that
/// viewer reads the thread from the host and draws it. Nothing in the APC
/// becomes part of the command but the checked host id and thread.
final class PCodeDrawer: NSObject, ObservableObject, PCodeOverlayOwner {
    @Published var visible = false
    /// The drawer's share of the surface's width, set by dragging its left edge.
    @Published var fraction: CGFloat = 0.70
    /// The drawer's terminal while it is open; dropping it ends the viewer.
    @Published private(set) var viewer: Ghostty.SurfaceView?
    /// The viewer's key hints: empty for none.
    @Published private(set) var hints: [PCodeHint] = []
    /// pcode's terminal font as the viewer started, for the footer.
    private(set) var fonts: PCodePickFonts?

    static let minWidth: CGFloat = 260
    /// Minimum width when the docked editor card lives inside the drawer.
    static let dockedMinWidth: CGFloat = 360
    static let maxFraction: CGFloat = 0.85

    /// True while the drawer is actually shown on screen with a live viewer.
    var isOpen: Bool { visible && viewer != nil }

    static func width(in total: CGFloat, fraction: CGFloat, minWidth: CGFloat = PCodeDrawer.minWidth) -> CGFloat {
        let floor = min(total, minWidth)
        let upper = max(floor, total * maxFraction)
        return min(upper, max(floor, total * fraction))
    }

    weak var surfaceView: Ghostty.SurfaceView?
    private var shown: (host: String, thread: String)?
    private var closeObserver: NSObjectProtocol?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    deinit {
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
    }

    // MARK: Commands

    /// From pcode in the terminal this drawer belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "btw" where parts.count == 3:
            guard PCode.valid(parts[1], length: 8...8, hex: true), PCode.valid(parts[2], length: 1...64) else {
                return
            }
            show(host: parts[1], thread: parts[2])
        case "close":
            close()
        case "hide" where parts.count == 1:
            // While pcode draws a full-screen popup of its own over the terminal.
            let hadFocus = viewerHasFocus
            visible = false
            if hadFocus { focusTerminal() }
        case "show" where parts.count == 1:
            if viewer != nil { visible = true }
        default:
            break
        }
    }

    /// From the viewer in the drawer's own terminal: its footer, or asking for the keyboard to go back.
    func handleFromOverlay(_ payload: String, from overlay: Ghostty.SurfaceView) {
        guard overlay === viewer else { return }
        if payload.hasPrefix("hints;") {
            if let decoded = PCodeHint.list(fromBase64: String(payload.dropFirst("hints;".count))),
               decoded != hints {
                hints = decoded
            }
            return
        }
        // Only while the drawer has the keyboard: anything else that prints
        // the sequence there must not pull focus from another split.
        if payload == "focus" && viewerHasFocus { focusTerminal() }
    }

    private func show(host: String, thread: String) {
        if viewer == nil || shown?.host != host || shown?.thread != thread {
            replaceViewer(with: makeViewer(host: host, thread: thread))
            shown = (host, thread)
        }
        // Shown beside the terminal without taking its keyboard: typing goes on
        // in pcode's editor, which replies to the thread.
        visible = true
    }

    func close() {
        let hadFocus = viewerHasFocus
        visible = false
        shown = nil
        replaceViewer(with: nil)
        if hadFocus { focusTerminal() }
    }

    func focusTerminal() {
        guard let surfaceView else { return }
        if let dock = surfaceView.existingPCodeDock, dock.ready, let docked = dock.surface {
            Ghostty.moveFocus(to: docked)
            return
        }
        Ghostty.moveFocus(to: surfaceView)
    }

    private var viewerHasFocus: Bool {
        guard let viewer, let responder = viewer.window?.firstResponder as? NSView else { return false }
        return responder === viewer || responder.isDescendant(of: viewer)
    }

    // MARK: The viewer's terminal

    private func replaceViewer(with next: Ghostty.SurfaceView?) {
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
        closeObserver = nil
        if let viewer, viewer !== next { PCode.retire(viewer) }
        if viewer !== next {
            // A new viewer sends its own.
            hints = []
            fonts = PCodePickFonts.of(surfaceView?.surface)
        }
        viewer = next
        guard let next else { return }
        // Cmd+W in the drawer, or the viewer exiting, asks to close its surface.
        // No window controller owns this one, so the drawer closes instead.
        closeObserver = NotificationCenter.default.addObserver(
            forName: Ghostty.Notification.ghosttyCloseSurface, object: next, queue: .main
        ) { [weak self, weak next] _ in
            // After the notification, not inside the callback that posted it,
            // which is still using the surface this frees. Only if it is still
            // this viewer: a later one may have replaced it meanwhile.
            DispatchQueue.main.async {
                guard let self, let next, self.viewer === next else { return }
                self.close()
            }
        }
    }

    private func makeViewer(host: String, thread: String) -> Ghostty.SurfaceView? {
        let docked = surfaceView?.existingPCodeDock?.surface != nil
        return PCode.makeOverlaySurface(
            over: surfaceView, command: Self.viewerCommand(host: host, thread: thread, docked: docked), owner: self)
    }

    /// The viewer, run with the interpreter the session host runs on, so it
    /// is the same pcode; or a message, if the host cannot say which that is.
    private static func viewerCommand(host: String, thread: String, docked: Bool) -> String {
        guard let python = PCode.entryPython(PCode.statePath("hosts/\(host).json")) else {
            return message("This session host's pcode predates the drawer: run /restart in pcode.")
        }
        // The ids are plain by now.
        // Tells the viewer we draw its key footer, so it leaves its own key line out.
        return PCode.pythonCommand(
            python,
            module: "pcode.btw_tail",
            args: [host, thread],
            env: ["PCODE_NATIVE_HINTS=1", "PCODE_DOCKED=\(docked ? 1 : 0)"])
    }

    /// A command that only shows `text`; the embedded API waits after every
    /// command, so it stays until a key or the drawer closes it.
    private static func message(_ text: String) -> String {
        "printf '%s\\n' \(PCode.shellQuoted(text))"
    }
}

/// The terminal surface, with its pcode drawer laid over its right side while one is shown.
///
/// An overlay rather than a split: the terminal keeps its size, so nothing
/// reflows and pcode's editor stays where it is, and the surface never leaves
/// the view tree, so it keeps the keyboard. The drawer slides in from the right
/// and stops several rows above the bottom so the editor and status bar stay
/// uncovered.
struct PCodeDrawerSurface: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var drawer: PCodeDrawer
    var isSplit: Bool
    /// SPIKE: the docked layout draws the terminal and the drawer as separate
    /// layers (`PCodeDockedSurface`), so the drawer spans the full height and
    /// reserves space at its bottom for the docked editor card.
    var drawsSurface = true
    var drawsDrawer = true
    /// Height reserved at the bottom of the drawer for the docked editor card.
    /// When set, the drawer spans the full pane height and hosts the card at
    /// its bottom; when nil, the drawer stops above the undocked inline editor.
    var dockFootprint: CGFloat?

    /// `drawer.fraction` when the current drag began: drags report distance from their start.
    @State private var dragStart: CGFloat?
    private static let minHeight: CGFloat = 120
    /// Terminal rows left uncovered at the bottom so pcode's inline editor and status bar stay visible.
    private static let bottomLines: CGFloat = 6
    private static let fallbackLineHeight: CGFloat = 18

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                if drawsSurface {
                    Ghostty.SurfaceWrapper(surfaceView: surfaceView, isSplit: isSplit)
                } else {
                    Color.clear.allowsHitTesting(false)
                }
            }
                .overlay(alignment: .topTrailing) {
                    ZStack(alignment: .topTrailing) {
                        Color.clear
                        if drawsDrawer, drawer.visible, let viewer = drawer.viewer {
                            overlay(
                                viewer,
                                width: width(in: geometry.size.width),
                                height: height(in: geometry.size.height),
                                total: geometry.size.width,
                                totalHeight: geometry.size.height
                            )
                            .allowsHitTesting(drawer.visible)
                            .transition(.move(edge: .trailing))
                            .zIndex(1)
                        }
                    }
                    .clipped()
                    .animation(.easeOut(duration: 0.18), value: drawer.visible)
                }
        }
    }

    private var minDrawerWidth: CGFloat {
        dockFootprint == nil ? PCodeDrawer.minWidth : PCodeDrawer.dockedMinWidth
    }

    private func width(in total: CGFloat) -> CGFloat {
        PCodeDrawer.width(in: total, fraction: drawer.fraction, minWidth: minDrawerWidth)
    }

    private func height(in total: CGFloat) -> CGFloat {
        if dockFootprint != nil { return total }
        let lineHeight = surfaceView.cellSize.height > 0 ? surfaceView.cellSize.height : Self.fallbackLineHeight
        let extraPadding: CGFloat
        if let rows = surfaceView.surfaceSize?.rows, rows > 0, surfaceView.cellSize.height > 0 {
            extraPadding = max(0, total - CGFloat(rows) * lineHeight) / 2
        } else {
            extraPadding = 0
        }
        let inset = min(lineHeight * Self.bottomLines + extraPadding, max(0, total - Self.minHeight))
        return max(0, total - inset)
    }

    private func overlay(
        _ viewer: Ghostty.SurfaceView,
        width: CGFloat,
        height: CGFloat,
        total: CGFloat,
        totalHeight: CGFloat
    ) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                // The left edge: a thin line to see, a wider strip to drag.
                Rectangle()
                    .fill(ghostty.config.splitDividerColor)
                    .frame(width: 1)
                    .overlay(
                        Color.clear
                            .frame(width: 8)
                            .contentShape(Rectangle())
                            .backport.pointerStyle(.resizeLeftRight)
                            .onHover { inside in
                                if #available(macOS 15, *) { return }
                                if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() }
                            }
                            .gesture(
                                DragGesture(coordinateSpace: .global)
                                    .onChanged { gesture in
                                        guard total > 0 else { return }
                                        let start = dragStart ?? (width / total)
                                        dragStart = start
                                        let next = start - gesture.translation.width / total
                                        let minFraction = min(1, minDrawerWidth / total)
                                        let maxFraction = max(minFraction, PCodeDrawer.maxFraction)
                                        drawer.fraction = min(maxFraction, max(minFraction, next))
                                    }
                                    .onEnded { _ in dragStart = nil }
                            )
                    )
                    .zIndex(1)
                VStack(spacing: 0) {
                    Ghostty.SurfaceWrapper(surfaceView: viewer, isSplit: false)
                        .id(ObjectIdentifier(viewer))
                    if !drawer.hints.isEmpty {
                        PCodeHintBar(
                            hints: drawer.hints,
                            fonts: drawer.fonts,
                            muted: ghostty.config.foregroundColor.opacity(PCodePickTheme.mutedOpacity))
                        .padding(.bottom, 6)
                        .environment(\.colorScheme, colorScheme)
                    }
                    if let dockFootprint {
                        // Reserve the card's exact footprint inside the drawer so the
                        // viewer and hint bar always end above it, with no overlap.
                        Color.clear
                            .frame(height: min(dockFootprint, max(0, totalHeight - Self.minHeight)))
                            .allowsHitTesting(false)
                    }
                }
                // Snap viewer/hint-bar height without animating so the viewer resizes
                // once per row change rather than on every animation frame.
                .animation(nil, value: dockFootprint)
            }
            if dockFootprint == nil {
                // Bottom edge above the uncovered inline editor rows.
                Rectangle()
                    .fill(ghostty.config.splitDividerColor)
                    .frame(height: 1)
            }
        }
        .frame(width: width, height: height)
        .background(
            // Shadow the drawer's backdrop rather than its VStack, or SwiftUI
            // casts a separate drop shadow from the terminal surface, dividers
            // and every keycap in the hint footer.
            ghostty.config.backgroundColor
                .shadow(color: .black.opacity(0.35), radius: 8, x: -2, y: 2)
        )
    }

    /// The drawer's own scheme: its background is the terminal's, not the system's.
    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}

