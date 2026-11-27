import AppKit
import GhosttyKit

/// What pcode's drawer, popup and dock share: checks on what pcode sends,
/// the overlay terminals the popup runs, and how APCs are routed.
///
/// pcode talks to us with APCs (`ESC _ pcode;<payload> ESC \`), which reach us
/// as `GHOSTTY_ACTION_PCODE_EVENT`. A terminal of the window speaks to its own
/// drawer, popup and dock card (`PCodeDrawer`, `PCodePopup`, `PCodeDock`); an
/// overlay terminal the popup runs speaks only to its owner. Neither can use the other's
/// verbs. The drawer is native and runs no terminal.
enum PCode {
    /// True if `value` is plain: ASCII letters, digits, `-` and `_`, or
    /// lowercase hex digits only if `hex`, with a length in `length`.
    static func valid(_ value: String, length: ClosedRange<Int>, hex: Bool = false) -> Bool {
        length.contains(value.count) && value.allSatisfy {
            guard $0.isASCII else { return false }
            if hex { return $0.isHexDigit && !$0.isUppercase }
            return $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_"
        }
    }

    /// `~/.local/state/pcode/<relative>`.
    static func statePath(_ relative: String) -> String {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/pcode/\(relative)").path
    }

    /// `python` from a pcode state file this user owns, if it names an executable at a plain path.
    static func entryPython(_ path: String) -> String? {
        // Opened without following a link, then checked: what is checked is what is read.
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(),
              info.st_size < 1 << 20,
              let data = try? handle.readToEnd(),
              let entry = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let python = entry["python"] as? String,
              python.hasPrefix("/"),
              // `env` would take a word with `=` for a variable to set.
              !python.contains("="),
              !python.contains(where: { $0.asciiValue.map { $0 < 0x20 } ?? false }),
              FileManager.default.isExecutableFile(atPath: python) else { return nil }
        return python
    }

    /// `python -I -m <module> <args>`, as a surface command.
    ///
    /// The embedded API always runs a surface's command in a shell, as
    /// `bash -c "exec -l <command>"` (`embedded.zig`, `Exec.zig`). `-l` puts a
    /// dash before argv[0], and a venv's Python finds its venv from argv[0], so
    /// `env` starts it with a plain one. `-I` keeps the directory, user site
    /// and PYTHON* variables out of its imports. `args` must be plain already.
    static func pythonCommand(_ python: String, module: String, args: [String]) -> String {
        (["/usr/bin/env", shellQuoted(python), "-I", "-m", module] + args).joined(separator: " ")
    }

    static func shellQuoted(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// A terminal laid over `surfaceView` that runs `command`, owned by `owner`.
    static func makeOverlaySurface(
        over surfaceView: Ghostty.SurfaceView?,
        command: String,
        owner: PCodeOverlayOwner
    ) -> Ghostty.SurfaceView? {
        guard let surfaceView, let surface = surfaceView.surface else { return nil }
        let app = ghostty_surface_app(surface)
        // As a new split would be: this terminal's font size, directory and environment.
        var config = Ghostty.SurfaceConfiguration(
            from: ghostty_surface_inherited_config(surface, GHOSTTY_SURFACE_CONTEXT_SPLIT))
        config.command = command
        // Suppress macOS `login(1)` "Last login:" banner in pcode overlays.
        config.environmentVariables["PCODE_HUSH_LOGIN"] = "1"
        // Not the pane's directory: nothing from a checkout is near the overlay's command.
        config.workingDirectory = NSHomeDirectory()
        config.initialInput = nil
        // Ignore inherited Ghostty font_size so we apply PCodeTypography's delta
        // via changeFontSize (which also marks font_size_adjusted so config reloads
        // don't reset overlays out of sync with the workspace).
        config.fontSize = nil
        config.waitAfterCommand = false
        let view = Ghostty.SurfaceView(app, baseConfig: config)
        view.becomePCodeOverlay(of: owner)
        // Overlays are in no window's surface tree, so nothing else tells them
        // the light/dark scheme a `theme = light:…,dark:…` config switches on.
        applyColorScheme(to: view)
        applyTypographyFontSize(to: view)
        return view
    }

    /// Apply `PCodeTypography.shared`'s current font-size offset (relative to default) to `view`.
    static func applyTypographyFontSize(to view: Ghostty.SurfaceView) {
        guard let surface = view.surface else { return }
        let delta = Int((PCodeTypography.clampSize(PCodeTypography.shared.size) - PCodeTypography.defaultSize).rounded())
        guard delta != 0 else { return }
        let action = delta > 0 ? "increase_font_size:\(delta)" : "decrease_font_size:\(-delta)"
        _ = ghostty_surface_binding_action(surface, action, UInt(action.lengthOfBytes(using: .utf8)))
    }

    /// Give `view` the app's current light/dark scheme (a no-op if it has it).
    static func applyColorScheme(to view: Ghostty.SurfaceView) {
        guard let surface = view.surface else { return }
        ghostty_surface_set_color_scheme(
            surface,
            NSApplication.shared.effectiveAppearance.isDark ? GHOSTTY_COLOR_SCHEME_DARK : GHOSTTY_COLOR_SCHEME_LIGHT)
    }

    /// End an overlay terminal its owner has dropped: free its core surface,
    /// which hangs up its process and closes its pty, now rather than whenever
    /// the last reference to the view goes (SwiftUI, a pending focus move).
    /// pcode, which holds the popup's tty, sees the hangup at once.
    ///
    /// On the next turn of the main loop: the drop may come from inside one of
    /// that surface's own callbacks (`pcode;done`), which is still using it.
    static func retire(_ overlay: Ghostty.SurfaceView) {
        DispatchQueue.main.async { overlay.freeSurface() }
    }

    /// A pcode overlay terminal that has the keyboard in `window`, if one has.
    ///
    /// Overlays stand for the terminal they cover as the window's focused
    /// surface (`pcodeWindowSurface`), so actions that would end or reset
    /// "the focused surface" must ask this first: Cmd+W in a popup closes the
    /// popup, not pcode's terminal under it.
    static func keyboardOverlay(in window: NSWindow?) -> Ghostty.SurfaceView? {
        var view = window?.firstResponder as? NSView
        while let current = view {
            if let surface = current as? Ghostty.SurfaceView {
                return surface.isPCodeOverlay ? surface : nil
            }
            view = current.superview
        }
        return nil
    }

    /// A native pcode picker whose search field has the keyboard in `window`, if one has.
    ///
    /// No terminal has the keyboard then, so `keyboardOverlay` finds none and
    /// "the focused surface" is still pcode's terminal under the picker: ask
    /// this first too, so Cmd+W closes the picker rather than that terminal.
    static func keyboardPicker(in window: NSWindow?) -> PCodePicker? {
        guard let editor = window?.firstResponder as? NSTextView,
              // The field editor's delegate is its text field, typed as a text view delegate.
              let field = (editor.delegate as AnyObject?) as? PCodePickField else { return nil }
        return field.picker
    }
}

/// A popup that runs overlay terminals over a terminal of the window.
protocol PCodeOverlayOwner: AnyObject {
    /// The terminal of the window the overlay is laid over.
    var surfaceView: Ghostty.SurfaceView? { get }

    /// An APC from `overlay`, a terminal this owner made. It may be one the
    /// owner has since dropped, so the owner checks it is still its own.
    func handleFromOverlay(_ payload: String, from overlay: Ghostty.SurfaceView)
}

private var pcodeDrawerKey: UInt8 = 0
private var pcodePopupKey: UInt8 = 0
private var pcodeCompletionKey: UInt8 = 0
private var pcodeToastKey: UInt8 = 0
private var pcodeDockKey: UInt8 = 0
private var pcodeOwnerKey: UInt8 = 0
private var pcodeSessionKey: UInt8 = 0
private var pcodeHostKey: UInt8 = 0
private var pcodeSessionPaneKey: UInt8 = 0
private var pcodeHostRelaunchAttemptKey: UInt8 = 0
private var pcodeTranscriptKey: UInt8 = 0

/// Holds an associated object without keeping it alive.
private final class PCodeWeakBox {
    weak var value: AnyObject?
    init(_ value: AnyObject?) { self.value = value }
}

/// Holds the owner weakly: the owner holds its overlay's surface, not the other way round.
private final class WeakOwner: NSObject {
    weak var owner: PCodeOverlayOwner?
    init(_ owner: PCodeOverlayOwner) { self.owner = owner }
}

extension Ghostty.SurfaceView {
    /// This surface's pcode drawer, made the first time it is asked for.
    var pcodeDrawer: PCodeDrawer {
        if let drawer = existingPCodeDrawer {
            return drawer
        }
        let drawer = PCodeDrawer(surfaceView: self)
        objc_setAssociatedObject(self, &pcodeDrawerKey, drawer, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return drawer
    }

    var existingPCodeDrawer: PCodeDrawer? {
        objc_getAssociatedObject(self, &pcodeDrawerKey) as? PCodeDrawer
    }

    /// This surface's pcode popup slot, made the first time it is asked for.
    var pcodePopup: PCodePopup {
        if let popup = existingPCodePopup { return popup }
        let popup = PCodePopup(surfaceView: self)
        objc_setAssociatedObject(self, &pcodePopupKey, popup, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return popup
    }

    /// This surface's native completion list for pcode, made the first time it is asked for.
    var pcodeCompletion: PCodeCompletion {
        if let completion = objc_getAssociatedObject(self, &pcodeCompletionKey) as? PCodeCompletion {
            return completion
        }
        let completion = PCodeCompletion(surfaceView: self)
        objc_setAssociatedObject(self, &pcodeCompletionKey, completion, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return completion
    }

    /// This surface's toast for pcode's confirmations, made the first time it is asked for.
    var pcodeToast: PCodeToast {
        if let toast = objc_getAssociatedObject(self, &pcodeToastKey) as? PCodeToast {
            return toast
        }
        let toast = PCodeToast(surfaceView: self)
        objc_setAssociatedObject(self, &pcodeToastKey, toast, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return toast
    }

    /// This surface's editor card layout state, made the first time it is asked for.
    var pcodeDock: PCodeDock {
        if let dock = objc_getAssociatedObject(self, &pcodeDockKey) as? PCodeDock {
            return dock
        }
        let dock = PCodeDock(surfaceView: self)
        objc_setAssociatedObject(self, &pcodeDockKey, dock, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return dock
    }

    var existingPCodeDock: PCodeDock? {
        objc_getAssociatedObject(self, &pcodeDockKey) as? PCodeDock
    }

    var existingPCodePopup: PCodePopup? {
        objc_getAssociatedObject(self, &pcodePopupKey) as? PCodePopup
    }

    private var pcodeOwnerBox: WeakOwner? {
        objc_getAssociatedObject(self, &pcodeOwnerKey) as? WeakOwner
    }

    /// Make this surface `owner`'s overlay terminal, for good: once dropped or
    /// orphaned it stays an overlay, so it never passes for a terminal of the
    /// window: its APCs go nowhere instead of to a drawer or popup of its own.
    fileprivate func becomePCodeOverlay(of owner: PCodeOverlayOwner) {
        objc_setAssociatedObject(self, &pcodeOwnerKey, WeakOwner(owner), .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }

    /// The pcode session this terminal shows (`pcode;session;<id>`), saved with
    /// the window so a restored terminal reopens it with `pcode -c <id>`.
    var pcodeSession: String? {
        get { objc_getAssociatedObject(self, &pcodeSessionKey) as? String }
        set {
            guard newValue != pcodeSession else { return }
            objc_setAssociatedObject(self, &pcodeSessionKey, newValue, .OBJC_ASSOCIATION_COPY_NONATOMIC)
            // The window encodes its surface tree, so it is the window that is now stale.
            window?.invalidateRestorableState()
            NotificationCenter.default.post(name: PCodeWorkspace.sessionChanged, object: self)
        }
    }

    /// The pcode session host this terminal attaches to (`pcode;host;<id>`).
    var pcodeHost: String? {
        get { objc_getAssociatedObject(self, &pcodeHostKey) as? String }
        set {
            guard newValue != pcodeHost else { return }
            objc_setAssociatedObject(self, &pcodeHostKey, newValue, .OBJC_ASSOCIATION_COPY_NONATOMIC)
            NotificationCenter.default.post(name: PCodeWorkspace.sessionChanged, object: self)
        }
    }

    /// What a restored terminal types to reopen `session`, or nil if it is not a session ID.
    static func pcodeResumeInput(_ session: String?) -> String? {
        guard let session, !session.isEmpty, session.count <= 64,
              session.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") })
        else { return nil }
        return "pcode -c \(session)\n"
    }

    /// The cursor's cell in this terminal's view, top-left origin, in points:
    /// where pcode's completion list and toast hang from its editor.
    var pcodeCursorCell: CGRect? {
        guard let surface else { return nil }
        let cell = cellSize
        guard cell.width > 0, cell.height > 0 else { return nil }
        var x: Double = 0, y: Double = 0, width: Double = 0, height: Double = 0
        ghostty_surface_ime_point(surface, &x, &y, &width, &height)
        // `x` is the cell's middle and `y` its bottom.
        return CGRect(x: x - cell.width / 2, y: y - cell.height, width: cell.width, height: cell.height)
    }

    /// True if this surface is a pcode popup's overlay terminal, even a dropped one.
    var isPCodeOverlay: Bool { pcodeOwnerBox != nil }

    /// True for a `PCodeSessionPane`'s primary surface, whose popup, completion
    /// and toast layers are mounted at pane level in `PCodeSessionPaneView`.
    var pcodeIsSessionPane: Bool {
        get { (objc_getAssociatedObject(self, &pcodeSessionPaneKey) as? Bool) ?? false }
        set { objc_setAssociatedObject(self, &pcodeSessionPaneKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    /// How many times the workspace has automatically relaunched this pane's host.
    var pcodeHostRelaunchAttempt: Int {
        get { (objc_getAssociatedObject(self, &pcodeHostRelaunchAttemptKey) as? Int) ?? 0 }
        set { objc_setAssociatedObject(self, &pcodeHostRelaunchAttemptKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    /// The popup this surface is the overlay terminal of, while it is still there.
    var pcodeOwner: PCodeOverlayOwner? { pcodeOwnerBox?.owner }

    /// The surface this one stands for in its window: for a pcode overlay, the
    /// terminal it is laid over, which is in the window's split tree where the
    /// overlay is not. Focusing an overlay then leaves the window's title,
    /// directory and split actions on that terminal.
    var pcodeWindowSurface: Ghostty.SurfaceView {
        pcodeOwner?.surfaceView ?? self
    }

    /// An APC from pcode in this surface.
    func pcodeEvent(_ payload: String) {
        if isPCodeOverlay {
            pcodeOwner?.handleFromOverlay(payload, from: self)
            return
        }
        switch payload.prefix(while: { $0 != ";" }) {
        case "session":
            let id = String(payload.dropFirst("session;".count))
            let valid = Self.pcodeResumeInput(id) == nil ? nil : id
            // A resuming workspace pane's client says "no session" once before it
            // attaches to its host; dropping the session the app opened the pane for
            // would show it untitled until then. Once attached, an empty marker
            // (`/new` before its first turn) is honored.
            if valid == nil, pcodeIsSessionPane, pcodeHost == nil, pcodeSession != nil { return }
            pcodeSession = valid
        case "host":
            let id = String(payload.dropFirst("host;".count))
            pcodeHost = PCode.valid(id, length: 8...8, hex: true) ? id : nil
        case "popup", "pick", "popup-close":
            pcodePopup.handle(payload)
        case "complete":
            pcodeCompletion.handle(payload)
        case "toast":
            pcodeToast.handle(payload)
        case "ready", "dock-rows", "startup-input", "composer":
            pcodeDock.handle(payload)
        default:
            pcodeDrawer.handle(payload)
        }
    }

    /// For a session pane's editor terminal, the height the card shows from its
    /// bottom edge; the rest of the 40-row grid is clipped off and must not take
    /// clicks. Nil (all of it) before pcode is ready, while it fills the pane.
    var pcodeDockVisibleHeight: CGFloat? {
        guard pcodeIsSessionPane, let dock = existingPCodeDock else { return nil }
        return PCodeDock.visibleHeight(
            ready: dock.ready,
            rows: dock.rows,
            cell: cellSize.height,
            surfaceRows: surfaceSize.map { Int($0.rows) })
    }

    /// The native transcript a session pane's editor terminal sits under, most
    /// recently mounted (a live peek mounts its own). Weak: the view owns it.
    var pcodeTranscriptScrollView: PCodeTranscriptScrollView? {
        get { (objc_getAssociatedObject(self, &pcodeTranscriptKey) as? PCodeWeakBox)?.value as? PCodeTranscriptScrollView }
        set { objc_setAssociatedObject(self, &pcodeTranscriptKey, PCodeWeakBox(newValue), .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    func pcodeWillKeyDown(_ event: NSEvent) {
        NotificationCenter.default.post(name: PCodeSessionPane.surfaceKeyPressed, object: pcodeWindowSurface)
    }

    /// This surface took the keyboard. While its popup is shown, the popup gets it back.
    func pcodeDidFocus() {
        guard !isPCodeOverlay else { return }
        existingPCodePopup?.windowSurfaceDidFocus()
    }

    /// A popup's terminal before pcode is ready: mounted unseen, so the
    /// mouse must neither focus it nor reach it.
    var pcodeIgnoresMouse: Bool {
        guard let popup = pcodeOwner as? PCodePopup else { return false }
        return !popup.isShowing(self)
    }

    /// False while the mouse should not focus this surface: an unseen popup
    /// terminal, or a terminal under its shown popup (hovering the scrim would
    /// otherwise focus it, and the popup take focus back, on every move).
    var pcodeAllowsMouseFocus: Bool {
        if isPCodeOverlay { return !pcodeIgnoresMouse }
        return existingPCodePopup?.ready != true
    }
}
