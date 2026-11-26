"""Best-effort startup detection of the terminal's default background.

OSC 11 is an xterm query, not a command to change the terminal's colors.
Never query while prompt_toolkit owns input; cache the result at startup.

The same window measures how wide the terminal draws `─`: Unicode calls it
ambiguous, and a terminal set to draw those two cells wide (a CJK locale,
iTerm2's option) shifts every row pcode lays out with one. `pcode.glyphs`
switches to ASCII when it does.

It can also ask whether the terminal glides (`pcode.smooth_scroll`). That query
goes first: a terminal answers in order, so once the background reply is in,
no glide reply means none is coming.
"""

import os
import re
import select
import sys
import time

THEMES = ("dark", "light", "auto")
_pending_input = bytearray()
_BACKGROUND = re.compile(
    rb"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)"
)
_CPR = re.compile(rb"\x1b\[(\d+);(\d+)R")
# Primary device attributes: every terminal answers it, and answers in order,
# so sent after the capability queries its reply means theirs are all in
# (or never coming), and the probe can stop without waiting out the timeout.
_FENCE = b"\x1b[c"
_FENCE_REPLY = re.compile(rb"\x1b\[\?[0-9;]{1,32}c")
# Where the cursor is, then save it, draw the ambiguous-width glyph, say where
# the cursor is now, and put the cursor back and wipe what was drawn. Sent as
# one write, so the glyph is gone before the terminal can show it; the
# replies still measure it because a terminal answers in order.
_WIDTH_PROBE = "\x1b[6n\x1b7─\x1b[6n\x1b8\x1b[K".encode()
_CURSOR_PROBE = b"\x1b[6n"
# The probe's position reports that had not arrived when it gave up waiting.
# They are the first the editor will see, ahead of its own, so it drops them.
_unanswered_cprs = 0
# The probe's answer: True when `─` took two cells, None when unmeasured.
_ambiguous_wide: bool | None = None
# The cursor's 1-based row before the welcome banner printed, when a startup
# CPR answered in time. Pre-seeding prompt_toolkit's renderer with it avoids
# the first-frame jump when CPR arrives after the initial draw.
_cursor_row: int | None = None
# The smooth-scroll protocol version the terminal reported; None if it didn't.
_glide_protocol: int | None = None
# The drawer protocol the startup probe heard (`pcode.drawer`), or None.
_drawer_protocol: int | None = None
# The overlay popup protocol it heard (`pcode.popup_surface`), or None.
_popup_protocol: int | None = None
# Whether the startup probe heard an OK reply to the Kitty graphics query.
_kitty_graphics: bool | None = None


# The last background the terminal reported, as `#rrggbb` with the theme it
# implies; None until one does. Shaded frames mix their surfaces from it, so
# they suit any theme. The theme rides along because a mode 2031 report can
# flip light/dark without a color: a stale dark color must not be read as the
# new light terminal's.
_background: tuple[str, str] | None = None


def terminal_background() -> tuple[str, str] | None:
    """The last reported background, `(#rrggbb, "light" | "dark")`, or None."""
    return _background


def ambiguous_wide() -> bool | None:
    """Whether the startup probe saw `─` drawn two cells wide; None if it didn't measure."""
    return _ambiguous_wide


def glide_supported() -> bool:
    """Whether the startup probe found a terminal that speaks pcode's glide protocol."""
    from pcode import smooth_scroll

    return (
        _glide_protocol is not None
        and smooth_scroll.MIN_PROTOCOL <= _glide_protocol <= smooth_scroll.PROTOCOL
    )


def marks_supported() -> bool:
    """Whether that terminal also takes reader-mode row marks and park (protocol 10)."""
    from pcode import smooth_scroll

    return (
        _glide_protocol is not None
        and smooth_scroll.MARKS_PROTOCOL <= _glide_protocol <= smooth_scroll.PROTOCOL
    )


def native_app() -> bool:
    """Whether pcode is running inside the native Pcode macOS app."""
    return drawer_supported() or popup_supported()


def drawer_supported() -> bool:
    """Whether the startup probe found a terminal that opens pcode's drawer."""
    from pcode import drawer

    return _drawer_protocol == drawer.PROTOCOL


def popup_supported() -> bool:
    """Whether the startup probe found a terminal that opens pcode's popups over itself."""
    from pcode import drawer

    return _popup_protocol is not None and _popup_protocol >= drawer.POPUP_PROTOCOL


def native_pick_supported() -> bool:
    """Whether that terminal also draws pick lists natively (`PopupSurface.pick`)."""
    from pcode import drawer

    return _popup_protocol is not None and _popup_protocol >= drawer.PICK_PROTOCOL


def native_completion_supported() -> bool:
    """Whether that terminal also draws the editor's completion menu (`native_completions`)."""
    from pcode import drawer

    return _popup_protocol is not None and _popup_protocol >= drawer.COMPLETE_PROTOCOL


def native_hints_supported() -> bool:
    """Whether that terminal also draws an overlay popup's key hints (`popup_hints`)."""
    from pcode import drawer

    return _popup_protocol is not None and _popup_protocol >= drawer.HINTS_PROTOCOL


def native_toast_supported() -> bool:
    """Whether that terminal also shows brief confirmations as toasts (`drawer.toast`)."""
    from pcode import drawer

    return _popup_protocol is not None and _popup_protocol >= drawer.TOAST_PROTOCOL


def kitty_graphics_supported() -> bool:
    """Whether the startup probe confirmed Kitty graphics support."""
    return _kitty_graphics is True


def _take_kitty_reply(response: bytearray) -> None:
    global _kitty_graphics
    from pcode import kitty_spinner

    _kitty_graphics = None
    match = kitty_spinner.REPLY.search(response)
    if match is not None:
        _kitty_graphics = match[1] == b"OK"
        del response[match.start() : match.end()]


def _take_drawer_reply(response: bytearray) -> None:
    """Record and remove the drawer's and the popups' replies (`pcode;ok;N`, `pcode;popup;N`)."""
    global _drawer_protocol, _popup_protocol
    from pcode import drawer

    _drawer_protocol = _popup_protocol = None
    for match in reversed(list(drawer.REPLY.finditer(response))):
        if version := drawer.VERSION.fullmatch(match[1]):
            _drawer_protocol = int(version[1])
        elif version := drawer.POPUP_VERSION.fullmatch(match[1]):
            _popup_protocol = int(version[1])
        del response[match.start() : match.end()]


def _take_glide_reply(response: bytearray) -> None:
    """Record and remove a smooth-scroll reply from `response`."""
    global _glide_protocol
    from pcode import smooth_scroll

    match = smooth_scroll.REPLY.search(response)
    if match is not None:
        version = smooth_scroll.VERSION.fullmatch(match[1])
        _glide_protocol = int(version[1]) if version else None
        del response[match.start() : match.end()]


def take_initial_cursor_row() -> int | None:
    """Return and clear the 1-based cursor row measured during startup, if any."""
    global _cursor_row
    row, _cursor_row = _cursor_row, None
    return row


def _measure_width(response: bytearray) -> bool:
    """Take the probe's two position reports out of `response`; whether both were there."""
    global _ambiguous_wide, _cursor_row
    reports = list(_CPR.finditer(response))[:2]
    if len(reports) < 2:
        return False
    (row, before), (row_after, after) = ((int(match[1]), int(match[2])) for match in reports)
    _cursor_row = row
    # A glyph that wrapped onto the next row measures nothing.
    if row == row_after and after - before in (1, 2):
        _ambiguous_wide = after - before == 2
    for match in reversed(reports):
        del response[match.start() : match.end()]
    return True


def _measure_cursor(response: bytearray) -> bool:
    """Take one position report out of `response`; whether it was there."""
    global _cursor_row
    match = _CPR.search(response)
    if match is None:
        return False
    _cursor_row = int(match[1])
    del response[match.start() : match.end()]
    return True


def background_theme(response: bytes) -> str | None:
    """Classify an OSC 11 RGB response (components may use 1–4 hex digits).

    Also records the color for `terminal_background`.
    """
    global _background
    match = _BACKGROUND.search(response)
    if match is None:
        return None
    rgb = [int(value, 16) / (16 ** len(value) - 1) for value in match.groups()]
    color = "#" + "".join(f"{round(value * 255):02x}" for value in rgb)
    from pcode import dock

    docked = dock.active()
    if docked is not None and docked.owns_tty and color in (docked.tint, docked._sent_tint):
        # EditorOnly's own OSC 11 tint echoed back: never record it as the
        # base background, or the next frame_shades call would lift it again.
        return None
    # Perceived brightness, rather than the unweighted RGB average.
    brightness = sum(value * weight for value, weight in zip(rgb, (0.299, 0.587, 0.114)))
    theme = "light" if brightness >= 0.5 else "dark"
    _background = (color, theme)
    return theme


def _terminal():
    """Descriptors to read the reply on and write the query to, plus any handle to close.

    Standard streams first, so an ordinary session queries exactly what it
    talks to. `--print` can be fed its prompt on stdin and still render to a
    terminal, so a redirected stream falls back to the controlling terminal
    instead of costing detection. Reading and writing are separate because
    stdin is not always opened for writing.
    """
    if sys.stdin.isatty() and sys.stdout.isatty():
        return sys.stdin.fileno(), sys.stdout.fileno(), None
    try:
        handle = open("/dev/tty", "r+b", buffering=0)
    except OSError:  # No controlling terminal: a daemon, or a CI runner.
        return None, None, None
    return handle.fileno(), handle.fileno(), handle


def _query_background(
    timeout: float | None = None,
    *,
    probe_width: bool = False,
    probe_cursor: bool = False,
    probe_glide: bool = False,
    probe_drawer: bool = False,
    probe_kitty: bool = False,
) -> str | None:
    global _unanswered_cprs, _cursor_row, _glide_protocol, _kitty_graphics
    _cursor_row = None
    if probe_kitty:
        _kitty_graphics = None
    # Nothing will be colored, so there is no palette to match: stay quiet
    # rather than write an escape sequence into whatever stdout is.
    if os.environ.get("TERM") == "dumb" or not (sys.stdout.isatty() or sys.stderr.isatty()):
        return None
    try:
        import termios
    except ImportError:
        return None
    fd, query_fd, handle = _terminal()
    if fd is None:
        return None
    response = bytearray()
    # Only the terminal pcode draws on is measured; the controlling terminal
    # fallback serves `--print`, which lays nothing out.
    probe_width = probe_width and handle is None
    probe_cursor = (probe_cursor and not probe_width) and handle is None
    from pcode import smooth_scroll

    glide_query = (
        smooth_scroll.QUERY
        if probe_glide and handle is None and smooth_scroll.worth_asking()
        else b""
    )
    from pcode import drawer

    # Asked of Ghostty only, as the glide is, for the same reasons.
    drawer_query = (
        drawer.QUERY + drawer.POPUP_QUERY
        if probe_drawer and handle is None and smooth_scroll.worth_asking()
        else b""
    )
    from pcode import kitty_spinner

    kitty_query = (
        kitty_spinner.QUERY
        if probe_kitty and handle is None and kitty_spinner.worth_asking()
        else b""
    )
    expected_cprs = 2 if probe_width else 1 if probe_cursor else 0
    measured = expected_cprs == 0
    # Only with a capability query, which goes to Ghostty and kitty alone:
    # without one, the background reply is all there is to wait for.
    fence = b"" if not (glide_query or drawer_query or kitty_query) else _FENCE
    fenced = not fence
    detected = None
    try:
        # Don't steal already queued input, or flush it on entering/leaving cbreak.
        if select.select([fd], [], [], 0)[0]:
            return None
        original = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN] = 0
        mode[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            # Canonical mode hides an unfinished line from select(). Check again
            # now that every queued character is visible, without consuming it.
            if select.select([fd], [], [], 0)[0]:
                return None
            if handle is None:
                # Keep the query behind any text already queued on stdout.
                sys.stdout.flush()
            probe = _WIDTH_PROBE if probe_width else _CURSOR_PROBE if probe_cursor else b""
            os.write(
                query_fd,
                glide_query + drawer_query + kitty_query + b"\x1b]11;?\x1b\\" + probe + fence,
            )
            if timeout is None:
                # The fence ends the wait as soon as the terminal has answered,
                # so a longer cap costs nothing there and rides out a slow
                # launch. Without it, a terminal that ignores OSC 11 would
                # make every launch wait the whole cap.
                timeout = 0.5 if fence else 0.15
            deadline = time.monotonic() + timeout
            while detected is None or not measured or not fenced:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    break
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                response.extend(chunk)
                if detected is None:
                    detected = background_theme(response)
                    if detected is not None:
                        response[:] = _BACKGROUND.sub(b"", response)
                if not measured and chunk == b"R":
                    measured = (
                        _measure_width(response) if probe_width else _measure_cursor(response)
                    )
                if not fenced and chunk == b"c":
                    fenced = _FENCE_REPLY.search(response) is not None
            if fence and fenced:
                response[:] = _FENCE_REPLY.sub(b"", response, count=1)
            if glide_query:
                _glide_protocol = None
                _take_glide_reply(response)
            if drawer_query:
                _take_drawer_reply(response)
            if kitty_query:
                _take_kitty_reply(response)
            if not measured:
                _unanswered_cprs = expected_cprs - len(_CPR.findall(response))
                response[:] = _CPR.sub(b"", response)
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, original)
    except OSError, ValueError, termios.error:
        pass
    finally:
        if handle is not None:
            handle.close()
        else:
            # Typing can race the query. Only the replies belong to us;
            # replay everything else through the editor's normal input parser.
            _pending_input.extend(response)
    return detected


def replay_pending_input(app, on_theme=None) -> None:
    """Return bytes read during the probe before the editor reads newer input.

    Also keeps filtering appearance replies out of typing for the whole
    session, passing each to `on_theme`: a late answer to the startup probe,
    and every later one `pcode.appearance` asks for.
    """
    from prompt_toolkit.input.vt100 import Vt100Input

    from pcode.startup_input import StartupInput

    global _unanswered_cprs
    if not isinstance(app.input, Vt100Input):
        return
    app.input = StartupInput(
        app.input.stdin, bytes(_pending_input), on_theme, late_cprs=_unanswered_cprs
    )
    _pending_input.clear()
    _unanswered_cprs = 0


def detect_theme(
    *,
    probe_width: bool = False,
    probe_cursor: bool = False,
    probe_glide: bool = False,
    probe_drawer: bool = False,
    probe_kitty: bool = False,
) -> str:
    """Use OSC 11, then COLORFGBG (when supplied by the terminal), then dark.

    `probe_width` also measures the ambiguous-width glyph (`ambiguous_wide`);
    `probe_cursor` records the starting cursor row (`take_initial_cursor_row`);
    `probe_glide` asks whether the terminal glides (`glide_supported`);
    `probe_drawer` whether it opens pcode's drawer and popups (`drawer_supported`,
    `popup_supported`);
    `probe_kitty` whether it speaks Kitty graphics (`kitty_graphics_supported`).
    """
    detected = _query_background(
        probe_width=probe_width,
        probe_cursor=probe_cursor,
        probe_glide=probe_glide,
        probe_drawer=probe_drawer,
        probe_kitty=probe_kitty,
    )
    if detected is not None:
        return detected
    try:
        background = int(os.environ.get("COLORFGBG", "").split(";")[-1])
    except ValueError:
        return "dark"
    # COLORFGBG describes ANSI palette indices, not RGB values.
    return "light" if background in (7, 15) else "dark"
