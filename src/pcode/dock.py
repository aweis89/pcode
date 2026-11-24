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
import secrets
import socket
import sys
import termios
from contextlib import suppress

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode import popup_surface
from pcode.popup_bridge import HELLO, RESIZED

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


class Dock:
    def __init__(self, fd: int, connection: socket.socket) -> None:
        self.fd = fd
        self.connection = connection
        self._stdin = open(fd, encoding="utf-8", errors="replace", closefd=False)
        self.input = Vt100Input(self._stdin)
        # Writes `pcode;ready` ahead of the first frame, so the card is never shown empty.
        self.writer = popup_surface._OverlayWriter(fd)
        self.output = Vt100_Output.from_pty(self.writer, term=os.environ.get("TERM"))
        self.app: Application | None = None
        self._rows = 0
        self._termios = None
        # The dock's background, `#rrggbb`: a shade lifted off the pane's
        # (`Transcript.frame_shades` sets it), sent before the next frame.
        self.tint: str | None = None
        self._sent_tint: str | None = None

    # The editor's application, once it runs.

    def attach(self, app: Application) -> None:
        """Hear the dock's resizes, and feed keys typed in pcode's own pane to the editor."""
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
        parser = Vt100Parser(app.key_processor.feed)
        flush_later: list[asyncio.TimerHandle] = []

        def flush() -> None:
            flush_later.clear()
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
            parser.feed(data.decode("utf-8", "replace"))
            app.key_processor.process_keys()
            # A lone Escape only comes out once the parser is told nothing follows.
            flush_later.append(loop.call_later(0.05, flush))

        loop.add_reader(main, typed)
        # pcode's own pane shows no editor, so no cursor either.
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()

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
        whole pane's height (pcode's own rows and the dock's together)."""
        try:
            pane = os.get_terminal_size(sys.stdout.fileno()).lines
        except OSError:
            return size
        return Size(rows=pane + size.rows, columns=size.columns)

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
                termios.tcsetattr(main, termios.TCSANOW, self._termios)
        sys.stdout.write("\x1b[?25h" + _apc("dock-close"))
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
