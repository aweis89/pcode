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

Not wired up beyond what the spike needs: no probe (a terminal without docks
costs a `CONNECT_TIMEOUT` wait at startup), no tmux, no tests.
"""

import asyncio
import os
import re
import secrets
import socket
import sys
import termios
import time
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

_active: "Dock | None" = None


def requested() -> bool:
    return os.environ.get("PCODE_DOCK") == "1" and sys.stdin.isatty() and sys.stdout.isatty()


def active() -> "Dock | None":
    """The open dock, if the editor runs on one."""
    return _active


def _apc(payload: str) -> str:
    return f"\x1b_pcode;{payload}\x1b\\"


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
        self.pane_rows()

    def pane_rows(self) -> int:
        """pcode's pane's height; a new size asks where the cursor ended up."""
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return 0
        if (size.lines, size.columns) != self._pane_size:
            self._pane_size = (size.lines, size.columns)
            self.ask_cursor()
        elif self._cpr_pending and time.monotonic() - self._asked_at >= CPR_TIMEOUT:
            self.ask_cursor()
        return size.lines

    def ask_cursor(self) -> None:
        """Ask the pane for its cursor row; the answer comes in with typing."""
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
        rows = self._pane_size[0] if self._pane_size else row + 1
        self.pane_row = min(rows - 1, row + self._written_since_ask)

    def wrote(self, rows: int, *, homed: bool = False) -> None:
        """pcode wrote ``rows`` rows to its pane, after homing the cursor if ``homed``."""
        if homed:
            self.pane_row = 0
            self._stale_reply = self._cpr_pending
        if self.pane_row is not None and self._pane_size:
            self.pane_row = min(self._pane_size[0] - 1, self.pane_row + rows)
        elif self._cpr_pending:
            self._written_since_ask += rows

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
            self.app._on_resize()

    def layout_size(self, size: Size) -> Size:
        """The size the layout budgets its rows against: the dock's width, the
        whole pane's height (pcode's own rows and the card's shown rows), but
        never more than the dock's grid, which the terminal keeps at a fixed
        height and only shows the bottom of."""
        try:
            pane = os.get_terminal_size(sys.stdout.fileno()).lines
        except OSError:
            return size
        return Size(rows=min(size.rows, pane + self._rows), columns=size.columns)

    def report_rows(self, app: Application) -> None:
        """Ask the terminal for as many rows as the layout wants, after each render."""
        root = app.layout.container
        # The height the frame just drawn was measured at (`_MeasuredSplit`);
        # its rows were budgeted against the whole pane (`layout_size`).
        measured = getattr(root, "_measured", None)
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
        # pane's last row; the pane is about to be an ordinary terminal again.
        sys.stdout.write(
            smooth_scroll.region_off(smooth_scroll.REGION_ID) + "\x1b[?25h" + _apc("dock-close")
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
