"""SPIKE: run the editor, tasks and status bar on a terminal docked under pcode's pane.

Opt in with `PCODE_DOCK=1` in Ghostty Smooth built from the matching spike
branch. pcode asks the terminal for a dock (`pcode;dock;<token>`), and the
terminal lays a second terminal in a styled card below pcode's pane. That
terminal runs `pcode.popup_bridge <token>`, which hands its tty back here,
the same way an overlay popup does (`popup_surface.overlay`). The prompt_toolkit
application then runs on the dock, full screen there, while the transcript
goes on writing to pcode's own terminal, which is now only scrollback: no
editor to erase and repaint around every committed row.

    pcode -> terminal (pcode's pane)   pcode;dock;<token>     open the dock
                                       pcode;dock-close       remove it
    pcode -> terminal (the dock)       pcode;ready            shown: focus it
                                       pcode;dock-rows;<n>    the rows it needs
                                       anything else          as if from the pane

Keys typed into pcode's own pane still reach the editor (`Dock.attach`), so
clicking the transcript to select and copy never strands typing.

The card overlays the bottom of pcode's pane rather than shortening it, so
the editor growing never resizes (and reflows) the pane. pcode keeps the rows
under the card clear instead (`Dock.reserve`): a scroll region down to the
card's top edge, which, starting at row 0, still sends the rows scrolled out
of it to scrollback. Growing scrolls the transcript up out of the card's way
before the card is told to grow (`report_rows`); shrinking only widens the
region and leaves the gap.

Not wired up beyond what the spike needs: no probe (a terminal without docks
costs a `CONNECT_TIMEOUT` wait at startup), no tmux, no tests.
"""

import asyncio
import fcntl
import math
import os
import re
import secrets
import socket
import struct
import sys
import termios
import time
from collections.abc import Callable
from contextlib import suppress

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode import popup_surface, smooth_scroll
from pcode.popup_bridge import HELLO, RESIZED
from pcode.startup_input import TerminalReplyParser

CONNECT_TIMEOUT = 3.0
# Never ask for more of the pane than this.
MAX_ROWS = 40
# Rows of the dock's cells the card covers on top of the rows it shows (its
# margins and padding): `PCodeDock.slackRows` in the app, kept in step.
CARD_SLACK_ROWS = 2.0
# The rows the app shows before pcode asks for any (`PCodeDock.rows`).
INITIAL_ROWS = 3
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

_active: "Dock | None" = None


def requested() -> bool:
    return os.environ.get("PCODE_DOCK") == "1" and sys.stdin.isatty() and sys.stdout.isatty()


def active() -> "Dock | None":
    """The open dock, if the editor runs on one."""
    return _active


def _apc(payload: str) -> str:
    return f"\x1b_pcode;{payload}\x1b\\"


def _cell_height(fd: int) -> float | None:
    """A terminal's row height in pixels, from its window size; None if it doesn't say.

    Ghostty's pixel height is the grid's area less padding, so it includes the
    part row left over under the grid: this errs high, by under a row's worth
    spread over all rows.
    """
    try:
        rows, _, _, pixels = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return None
    return pixels / rows if rows and pixels else None


def reserve_for(rows: int, pane_rows: int, ratio: float = 1.0) -> int:
    """Pane rows a card showing ``rows`` dock rows covers, for a dock-to-pane
    cell height ``ratio``: rounded up, and always leaving the pane two rows."""
    covered = math.ceil((rows + CARD_SLACK_ROWS) * ratio - 1e-6)
    return max(0, min(covered, pane_rows - 2))


def _region(rows: int) -> str:
    """A scroll region over the pane's first ``rows`` rows (DECSTBM; it homes the cursor)."""
    return f"\x1b[1;{rows}r"


def _row(row: int) -> str:
    """The cursor to column 1 of 0-based ``row``."""
    return f"\x1b[{row + 1};1H"


# A cursor position report, taken out of pcode's pane's input while one is
# asked for. Column 1 only: every pane write ends a row, so the cursor is
# always there, while Shift/Ctrl+F3 (`ESC[1;2R` and up) never report column 1.
_CPR = re.compile(rb"\x1b\[(\d{1,4});1R")
# The start of one, at the end of a read.
_CPR_OPEN = re.compile(rb"\x1b\[(?:\d{1,4}(?:;1?)?)?\Z")
# A report not in after this long is given up on and asked for again.
CPR_TIMEOUT = 1.0


class Dock:
    def __init__(self, fd: int, connection: socket.socket) -> None:
        self.fd = fd
        self.connection = connection
        self._stdin = open(fd, encoding="utf-8", errors="replace", closefd=False)
        self.input = Vt100Input(self._stdin)
        # Writes `pcode;ready` ahead of the first frame, so the card is never shown empty.
        self.writer = popup_surface._OverlayWriter(fd)
        if trace := os.environ.get("PCODE_DOCK_TRACE"):
            # Everything drawn on the dock, appended with a timestamp per write.
            self._trace = open(trace, "a", encoding="utf-8")  # noqa: SIM115
            write = self.writer.write

            def traced(text: str) -> int:
                self._trace.write(f"\n@{time.monotonic():.3f} {text!r}")
                self._trace.flush()
                return write(text)

            self.writer.write = traced
        self.output = Vt100_Output.from_pty(self.writer, term=os.environ.get("TERM"))
        self.app: Application | None = None
        self._rows = 0
        self._termios = None
        # The dock's background, `#rrggbb`: a shade lifted off the pane's
        # (`Transcript.frame_shades` sets it), sent before the next frame.
        self.tint: str | None = None
        self._sent_tint: str | None = None
        # Where the cursor is on pcode's own pane (0-based row), which holds
        # only scrollback now, for the transcript's glide (`Transcript`'s
        # docked glide); None until the terminal says (`ask_cursor`). The
        # pane's resizes pull rows out of scrollback and back, so after one
        # the row is asked again rather than guessed.
        self.pane_row: int | None = None
        # While a report is out, the last known row moved on by the rows
        # written since: a guess, for scrolling no further than needed.
        self._row_hint: int | None = None
        self._pane_size: tuple[int, int] | None = None
        self._cpr_pending = False
        self._asked_at = 0.0
        self._written_since_ask = 0
        # The outstanding reply predates a home, so it no longer says anything.
        self._stale_reply = False
        # Asked again while a reply was outstanding: that reply is dropped and
        # the question asked anew, never answered with the old one's counts.
        self._requery = False
        # The start of a reply split across reads, held for the next one.
        self._cpr_tail = b""
        # The rows at the bottom of the pane the card covers, kept clear by a
        # scroll region over the rest (`_set_region`).
        self.reserve = 0
        # The pane's scroll region now: its height, or None while it has none
        # (a resize resets it), until the cursor's row is known again. While
        # set, the cursor is always inside it: below it, a newline at the
        # pane's last row would scroll nothing and rows would overwrite.
        self._region: int | None = None
        # The transcript's glide for rows the pane scrolls to make room for
        # the card: the text to send before and after the scroll
        # (`TerminalOutput._docked_room`).
        self.glide: Callable[[int], tuple[str, str]] | None = None

    # The editor's application, once it runs.

    def attach(self, app: Application, on_theme=None) -> None:
        """Hear the dock's resizes, and feed keys typed in pcode's own pane to the editor.

        pcode's own pane is still where its terminal queries go, so the
        replies (a late startup probe answer, appearance reports) arrive here
        too and are taken out before the rest reaches the editor as keys.
        """
        self.app = app
        loop = asyncio.get_running_loop()
        self.connection.setblocking(False)
        loop.add_reader(self.connection.fileno(), self._bridge_spoke)
        main = sys.stdin.fileno()
        with suppress(termios.error):
            self._termios = termios.tcgetattr(main)
            attrs = termios.tcgetattr(main)
            # Only the line discipline: output post-processing (\n to \r\n) stays
            # on, since the transcript is still written to this terminal.
            attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(main, termios.TCSANOW, attrs)
        parser = TerminalReplyParser(app.key_processor.feed, on_theme)
        flush_later: list[asyncio.TimerHandle] = []

        def flush() -> None:
            flush_later.clear()
            if self._cpr_tail:
                # Nothing completed it: it was typing after all (Escape, Alt+[).
                parser.feed(self._cpr_tail.decode("utf-8", "replace"))
                self._cpr_tail = b""
            parser.flush()
            app.key_processor.process_keys()

        def typed() -> None:
            try:
                data = os.read(main, 4096)
            except BlockingIOError, InterruptedError:
                return
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(main)
                return
            for handle in flush_later:
                handle.cancel()
            flush_later.clear()
            if self._cpr_pending:
                data = self._take_reply(self._cpr_tail + data)
            parser.feed(data.decode("utf-8", "replace"))
            app.key_processor.process_keys()
            # A lone Escape only comes out once the parser is told nothing follows.
            flush_later.append(loop.call_later(0.05, flush))

        loop.add_reader(main, typed)
        # pcode's own pane shows no editor, so no cursor either.
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        # The reserve for the card's first rows; the region follows as soon as
        # the cursor's row is in (`_answered`), before the first frame.
        self.pane_rows()

    def raw_pane_rows(self) -> int:
        """pcode's pane's whole height, card included; a new size asks where the cursor ended up."""
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return 0
        if (size.lines, size.columns) != self._pane_size:
            self._pane_size = (size.lines, size.columns)
            # The terminal reset its scroll region with the resize, and the
            # cell size may have changed with the font: set both again once
            # the cursor's row is known.
            self._region = None
            self.reserve = reserve_for(self._rows or INITIAL_ROWS, size.lines, self._ratio())
            self.ask_cursor()
            # The resize moved rows in and out of scrollback: no guess holds.
            self._row_hint = None
        elif self._cpr_pending and time.monotonic() - self._asked_at >= CPR_TIMEOUT:
            self.ask_cursor()
        return size.lines

    def pane_rows(self) -> int:
        """The rows of pcode's pane above the card: where the transcript scrolls."""
        raw = self.raw_pane_rows()
        return raw - self.reserve if raw else 0

    def _ratio(self) -> float:
        """The dock's row height over the pane's; 1 when either terminal doesn't say."""
        dock, pane = _cell_height(self.fd), _cell_height(sys.stdout.fileno())
        return dock / pane if dock and pane else 1.0

    def _bottom(self) -> int:
        """The lowest row the pane's cursor can reach now."""
        raw = self._pane_size[0] if self._pane_size else 1
        return (self._region or raw) - 1

    def _set_region(self) -> None:
        """Set the scroll region above the card, the cursor's row known.

        A cursor already under the card's edge (a resize, the first rows
        before the region) scrolls the whole pane until it isn't.
        """
        if self.pane_row is None or not self._pane_size:
            return
        raw = self._pane_size[0]
        height = raw - self.reserve
        row = self.pane_row
        out = ""
        if row > height - 1:
            lift = row - (height - 1)
            out += "\x1b[r" + _row(raw - 1) + "\n" * lift
            row -= lift
        out += _region(height) + _row(row)
        self.pane_row = row
        self._region = height
        sys.stdout.write(out)
        sys.stdout.flush()

    def _move_card_edge(self) -> None:
        """Follow the card to its new height (``self._rows``) with the reserve.

        Growing, the rows about to go under the card scroll up out of its way
        first: a real scroll, which sends the top rows to scrollback and which
        the transcript's glide animates. Shrinking only widens the region,
        leaving a gap the next rows fill.
        """
        raw = self.raw_pane_rows()
        if not raw:
            return
        reserve = reserve_for(self._rows, raw, self._ratio())
        if reserve == self.reserve:
            return
        self.reserve = reserve
        if self._region is None:
            # Set once the cursor's row is in (`_answered`).
            return
        old, new = self._region, raw - reserve
        row = self.pane_row
        lift = 0
        if row is not None:
            if new < old and (lift := max(0, row - (new - 1))):
                out = _row(old - 1) + "\n" * lift
            else:
                out = ""
            self.pane_row = row - lift
            out += _region(new) + _row(self.pane_row)
        elif new < old:
            # Where the cursor is isn't known (a report is out), but it is
            # inside the region. Scroll as far as the last known row, moved on
            # by the rows written since, says it must go (all the way, as if
            # at the bottom, without one) and take it up as far; then ask
            # again. Were the guess short, the reply lands below the region
            # and `_answered` lifts it.
            hint = old - 1 if self._row_hint is None else self._row_hint
            lift = max(0, hint - (new - 1))
            if self._row_hint is not None:
                self._row_hint -= lift
            out = ""
            if lift:
                out = "\x1b7" + _row(old - 1) + "\n" * lift + f"\x1b8\x1b[{lift}A"
            out += "\x1b7" + _region(new) + "\x1b8"
        else:
            out = "\x1b7" + _region(new) + "\x1b8"
        self._region = new
        if lift and self.glide is not None:
            before, after = self.glide(lift)
            out = SYNC_START + before + out + after + SYNC_END
        sys.stdout.write(out)
        sys.stdout.flush()
        if row is None and new < old:
            self.ask_cursor()

    def ask_cursor(self) -> None:
        """Ask the pane for its cursor row; the answer comes in with typing."""
        if self.pane_row is not None:
            self._row_hint = self.pane_row
        self.pane_row = None
        if self._cpr_pending:
            if time.monotonic() - self._asked_at < CPR_TIMEOUT:
                self._requery = True
                return
            # Never answered (or lost): give up on it.
            self._cpr_pending = False
            self._cpr_tail = b""
        self._cpr_pending = True
        self._asked_at = time.monotonic()
        self._written_since_ask = 0
        self._stale_reply = self._requery = False
        sys.stdout.write("\x1b[6n")
        sys.stdout.flush()

    def _take_reply(self, data: bytes) -> bytes:
        """``data`` without the cursor report asked for, holding a partial one."""
        self._cpr_tail = b""
        if match := _CPR.search(data):
            self._answered(int(match.group(1)) - 1)
            return data[: match.start()] + data[match.end() :]
        if (tail := _CPR_OPEN.search(data)) is not None:
            self._cpr_tail = data[tail.start() :]
            return data[: tail.start()]
        return data

    def _answered(self, row: int) -> None:
        # The reply is where the cursor was when the query arrived; rows
        # written since then moved it on.
        self._cpr_pending = False
        if self._requery:
            self._requery = False
            self.ask_cursor()
            return
        if self._stale_reply:
            self._stale_reply = False
            return
        if self._region is not None and row > self._region - 1:
            # Below the region pcode set: the terminal dropped it (a resize
            # not seen yet, a reset, a child's `ESC[r`), so newlines now
            # scroll the whole pane, under the card. Set it again from here.
            self._region = None
        bottom = self._bottom() if self._pane_size else row
        self.pane_row = min(bottom, row + self._written_since_ask)
        if self._pane_size and self._region != self._pane_size[0] - self.reserve:
            self._set_region()

    def homed(self) -> str:
        """The pane was just cleared and homed (a replay): the region to set again after."""
        self.pane_row = 0
        self._stale_reply = self._cpr_pending
        if not self._pane_size:
            return ""
        self._region = self._pane_size[0] - self.reserve
        return _region(self._region) + _row(0)

    def wrote(self, rows: int) -> None:
        """pcode wrote ``rows`` rows to its pane."""
        if self.pane_row is not None and self._pane_size:
            self.pane_row = min(self._bottom(), self.pane_row + rows)
        elif self._cpr_pending:
            self._written_since_ask += rows
            if self._row_hint is not None:
                self._row_hint = min(self._bottom(), self._row_hint + rows)

    def _bridge_spoke(self) -> None:
        try:
            data = self.connection.recv(256)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            with suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.connection.fileno())
            return
        if RESIZED in data and self.app is not None and self.app.is_running:
            # The dock's cell size may have changed (its font), and the card's
            # height with it: the reserve follows.
            self._move_card_edge()
            self.app._on_resize()

    def layout_size(self, size: Size) -> Size:
        """The size the layout budgets its rows against: the dock's width, the
        whole pane's height less the card's margins (the card overlays the
        pane), but never more than the dock's grid, which the terminal keeps
        at a fixed height and only shows the bottom of."""
        try:
            pane = os.get_terminal_size(sys.stdout.fileno()).lines
        except OSError:
            return size
        # In the dock's rows, whose cells may differ from the pane's: no more
        # than a card the pane fits (`reserve_for` never has to cut it short).
        budget = max(1, math.floor((pane - 2) / self._ratio() - CARD_SLACK_ROWS))
        return Size(rows=min(size.rows, budget), columns=size.columns)

    def report_rows(self, app: Application) -> None:
        """Ask the terminal for as many rows as the layout wants, after each render."""
        root = app.layout.container
        # The height the frame just drawn was measured at (`_MeasuredSplit`);
        # its rows were budgeted against the whole pane (`layout_size`).
        measured = getattr(root, "drawn", None) or getattr(root, "_measured", None)
        if measured is not None:
            wanted = measured[2]
        else:
            size = self.layout_size(app.output.get_size())
            wanted = root.preferred_height(size.columns, size.rows).preferred
        wanted = max(1, min(MAX_ROWS, wanted))
        if self.tint != self._sent_tint and self.tint is not None:
            self._sent_tint = self.tint
            # OSC 11: the dock terminal's default background, its padding included.
            app.output.write_raw(f"\x1b]11;{self.tint}\x1b\\")
            app.output.flush()
            app.invalidate()
        if wanted != self._rows:
            self._rows = wanted
            # The pane makes room first, so the card never covers a row.
            self._move_card_edge()
            app.output.write_raw(_apc(f"dock-rows;{wanted}"))
            app.output.flush()

    def close(self) -> None:
        global _active
        _active = None
        main = sys.stdin.fileno()
        with suppress(Exception):
            asyncio.get_running_loop().remove_reader(main)
        with suppress(Exception):
            asyncio.get_running_loop().remove_reader(self.connection.fileno())
        if self._termios is not None:
            with suppress(termios.error):
                if self._cpr_pending:
                    # Not echoed into the shell once the line discipline is back.
                    time.sleep(0.05)
                    termios.tcflush(main, termios.TCIFLUSH)
                termios.tcsetattr(main, termios.TCSANOW, self._termios)
        # The transcript's glide region (`Transcript._docked_frame`) hides the
        # pane's last row, and the scroll region stops above the card; the
        # pane is about to be an ordinary terminal again. DECSTBM homes the
        # cursor, so it is saved around it.
        sys.stdout.write(
            smooth_scroll.region_off(smooth_scroll.REGION_ID)
            + "\x1b7\x1b[r\x1b8\x1b[?25h"
            + _apc("dock-close")
        )
        sys.stdout.flush()
        with suppress(OSError):
            self.connection.close()
        with suppress(OSError):
            self._stdin.close()
        with suppress(OSError):
            os.close(self.fd)


async def open_dock(timeout: float = CONNECT_TIMEOUT) -> Dock | None:
    """Ask the terminal for a dock and take its tty; None if none came."""
    global _active
    token = secrets.token_hex(8)
    try:
        directory = popup_surface._private_dir()
    except OSError:
        return None
    entry = directory / f"{token}.json"
    address = directory / f"{token}.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    loop = asyncio.get_running_loop()
    connection = None
    fds: list[int] = []
    try:
        server.bind(str(address))
        server.listen(1)
        server.setblocking(False)
        popup_surface._write_entry(entry, sys.executable)
        sys.stdout.write(_apc(f"dock;{token}"))
        sys.stdout.flush()
        async with asyncio.timeout(timeout):
            connection, _ = await loop.sock_accept(server)
            message, fds = await popup_surface._receive(connection)
    except OSError, TimeoutError:
        if connection is not None:
            connection.close()
        for fd in fds:
            os.close(fd)
        sys.stdout.write(_apc("dock-close"))
        sys.stdout.flush()
        return None
    finally:
        for path in (entry, address):
            with suppress(OSError):
                path.unlink()
        server.close()
    fd = fds[0] if fds else None
    if (
        not message.startswith(HELLO)
        or fd is None
        or not os.isatty(fd)
        or not popup_surface._same_user(connection)
    ):
        connection.close()
        for extra in fds:
            os.close(extra)
        sys.stdout.write(_apc("dock-close"))
        sys.stdout.flush()
        return None
    for extra in fds[1:]:
        os.close(extra)
    _active = Dock(fd, connection)
    return _active
