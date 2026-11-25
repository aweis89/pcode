import AppKit
import GhosttyKit
import SwiftUI

/// pcode's brief confirmations ("Copied answer.", "Opened …"), shown as a
/// toast over its editor instead of a row in the pane (`pcode.drawer.toast`).
///
/// One APC (popup protocol 5):
///
/// - `pcode;toast;<dy>;<ms>;<base64 utf-8>`: show the text for `ms`
///   milliseconds, centered over the pane just above the editor's box, which
///   starts `dy` rows above the cursor's row. A newer toast replaces the one
///   on show, and empty text hides it. Display only: it never takes the
///   mouse or the keyboard.
final class PCodeToast: ObservableObject {
    struct Shown: Equatable {
        let id: Int
        let text: String
        let rowsAbove: Int
    }

    /// The toast on show, or nil.
    @Published private(set) var shown: Shown?

    weak var surfaceView: Ghostty.SurfaceView?
    private var count = 0
    private var hide: DispatchWorkItem?

    /// Longer than pcode ever asks: a stuck toast still goes.
    static let maxDuration: TimeInterval = 15
    static let maxRows = 200

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// From pcode in the terminal this toast belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 4, parts[0] == "toast",
              let rows = Int(parts[1]), rows >= 0,
              let ms = Int(parts[2]), ms >= 0 else { return }
        let text = PCodePopup.title(fromBase64: parts[3])
        guard !text.isEmpty else {
            // pcode cleared the notice early (a job's progress that ended).
            hide?.cancel()
            shown = nil
            return
        }
        show(text, rowsAbove: min(rows, Self.maxRows), for: min(TimeInterval(ms) / 1000, Self.maxDuration))
    }

    private func show(_ text: String, rowsAbove: Int, for duration: TimeInterval) {
        hide?.cancel()
        count += 1
        let id = count
        shown = Shown(id: id, text: text, rowsAbove: rowsAbove)
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.shown?.id == id else { return }
            self.shown = nil
        }
        hide = work
        DispatchQueue.main.asyncAfter(deadline: .now() + duration, execute: work)
    }

    /// The cursor's cell in the terminal view, top-left origin, in points.
    ///
    /// With a dock shown, reads the dock's cursor in this terminal's coordinates
    /// so toasts sit above the docked editor card (including inside `/btw`).
    func cursorCell() -> CGRect? {
        guard let surfaceView else { return nil }
        guard let dock = surfaceView.existingPCodeDock, dock.ready, let editor = dock.surface else {
            return surfaceView.pcodeCursorCell
        }
        guard let cell = editor.pcodeCursorCell else { return nil }
        let docked = editor.convert(editor.bounds, to: nil)
        let pane = surfaceView.convert(surfaceView.bounds, to: nil)
        // Window coordinates have a bottom-left origin; the cell's is top-left.
        return cell.offsetBy(dx: docked.minX - pane.minX, dy: pane.maxY - docked.maxY)
    }
}

/// The toast, centered over the pane just above pcode's editor box, or near
/// the bottom when the cursor is unknown.
struct PCodeToastLayer: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var toast: PCodeToast

    static let margin: CGFloat = 6
    static let cornerRadius: CGFloat = 8
    static let fallbackBottom: CGFloat = 48
    static let horizontalPadding: CGFloat = 14
    static let verticalPadding: CGFloat = 6

    var body: some View {
        GeometryReader { geometry in
            // The terminal's own size: with the inspector open, the layer is wider.
            let bounds = toast.surfaceView?.bounds.size ?? geometry.size
            let size = CGSize(
                width: min(geometry.size.width, bounds.width), height: min(geometry.size.height, bounds.height))
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                if let shown = toast.shown {
                    pill(shown.text)
                        .id(shown.id)
                        .transition(.opacity.combined(with: .offset(y: 6)))
                }
                Color.clear.frame(height: Self.bottomGap(toast.shown, cursor: toast.cursorCell(), in: size))
            }
            .frame(width: size.width, height: size.height)
            .animation(.easeOut(duration: 0.18), value: toast.shown)
        }
        .allowsHitTesting(false)
    }

    private func pill(_ text: String) -> some View {
        let fonts = PCodePickFonts.of(toast.surfaceView?.surface)
        return Text(text)
            .font(PCodePickFonts.small(fonts, fallbackSize: 12, weight: .medium))
            .foregroundStyle(ghostty.config.foregroundColor)
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, Self.horizontalPadding)
            .padding(.vertical, Self.verticalPadding)
            .background(ghostty.config.backgroundColor)
            .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Self.cornerRadius)
                    .strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
            .shadow(color: .black.opacity(0.35), radius: 10, x: 0, y: 4)
            .padding(.horizontal, 24)
            .environment(\.colorScheme, colorScheme)
            .accessibilityAddTraits(.isStaticText)
    }

    /// Points from the pane's bottom to the toast's: above the editor box's top row.
    static func bottomGap(_ shown: PCodeToast.Shown?, cursor: CGRect?, in size: CGSize) -> CGFloat {
        guard let shown, let cursor else { return fallbackBottom }
        let top = cursor.minY - CGFloat(shown.rowsAbove) * cursor.height - margin
        // Kept on screen: never below the pane, never off its top.
        return max(margin, min(size.height - top, size.height - 2 * cursor.height))
    }

    /// The toast's own scheme: its background is the terminal's, not the system's.
    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}
