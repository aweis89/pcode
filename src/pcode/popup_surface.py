"""Where a popup draws: today's full screen, or a native overlay (`popup_view`).

Every popup is a prompt_toolkit `Application` given an input and an output.
`App.popup` yields a `PopupSurface` holding both, from one of two places:

- The full screen (`popup_view popup`, or wherever an overlay can't open):
  the editor is suspended and the popup takes pcode's own terminal, and the
  conversation is drawn again after it.
- An overlay (`popup_view overlay` in Ghostty Smooth): the terminal lays a
  second terminal over pcode's pane and runs `pcode.popup_bridge` in it, which
  hands that terminal's fd to this process. The popup then runs here, as ever,
  with every callback it has, but reads keys from and draws on the overlay. The
  conversation stays on screen behind it and keeps streaming.
- A native picker (protocol 2): for a pick list (`PopupSurface.pick`) the
  terminal draws the list itself, a search field over native rows, and only
  the result comes back. Without one, `pick` runs `pick_ui.PickList` on the
  terminal overlay or the full screen.

The overlay protocol (`ESC _ pcode;… ESC \\`):

    pcode;popup?                    probe; the terminal answers `pcode;popup;N`
                                    (1: terminal overlays, 2: native pickers too)
    pcode;popup;<token>;<layout>[;<base64 utf-8 title>]
                                    open a popup (`sheet` or `wide`) running the bridge;
                                    a title heads it from the start
    pcode;pick;<token>;<layout>     open a native picker (protocol 2), see below
    pcode;popup-close;<token>       give up on that popup (it never came, say)
    pcode;ready                     (from the overlay) shown it: focus it
    pcode;title;<base64 utf-8>      (from the overlay) the popup's title, for its header
    pcode;done                      (from the overlay) close it

The token names a socket and an entry file in `popup_dir()`, which this user
alone can read; the entry names the Python that runs the bridge. The terminal
shows the overlay only once pcode, holding its terminal, says `ready` there, so
no other program's output can raise one. Closing it from the terminal (Cmd+W,
a click outside it) hangs its terminal up, and the popup is cancelled as if
its own cancel key had been pressed: `app.exit(result=None)`, which is what
every popup's cancel does.

A native picker has no terminal and no bridge: the terminal connects to the
token's socket itself and speaks newline-delimited JSON (UTF-8) over it.

    terminal -> pcode   `pcode-pick 1\\n`, its hello (the bridge's differs)
    pcode -> terminal   a list: `pick_ui.payload`, one JSON object per line
    terminal -> pcode   the pick: `{"id": str|null, "action": str|null, "query": str}`
    pcode -> terminal   another list to show in its place, or EOF: close it

The terminal shows and focuses the picker only once the first list arrives.
It closing the socket (Escape, Cmd+W, a click outside) is a cancel; so is
Escape or Ctrl+C in pcode's own pane, after which pcode closes the socket and
sends `popup-close`. Nothing else may connect: the socket is in the private
`popup_dir()`, the token is 16 hex digits, and it is unlinked once found.
"""

import asyncio
import base64
import json
import os
import secrets
import select
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application.current import AppSession, create_app_session, get_app_or_none
from prompt_toolkit.input import Input
from prompt_toolkit.input.typeahead import clear_typeahead
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import Output
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode.keyboard_protocol import keyboard_output
from pcode.popup_bridge import HELLO, RESIZED, popup_dir
from pcode.startup_input import StartupInput

if TYPE_CHECKING:
    # Imported where used: `pick_ui` draws with `frame`, which imports this module.
    from pcode.pick_ui import Match, PickAction, Picked, PickItem

PROTOCOL = 1  # Probed as `drawer.POPUP_QUERY`.
PICK_HELLO = b"pcode-pick 1\n"
# The longest pick a native picker may send back.
_PICK_REPLY_LIMIT = 1 << 16
LAYOUTS = ("sheet", "wide")
READY = b"\x1b_pcode;ready\x1b\\"
DONE = b"\x1b_pcode;done\x1b\\"
# How long the terminal gets to start the bridge. Python starts in ~50 ms;
# not answering at all means it can't reach the socket (a container, a VM).
CONNECT_TIMEOUT = 3.0
# Older entries in `popup_dir()` are leftovers: each is found within seconds.
_STALE_AFTER = 600.0
# How long a frame may wait for the overlay to take it.
_WRITE_TIMEOUT = 2.0
# Characters of a title the header shows; the terminal cuts it there too.
TITLE_MAX = 200


class _Terminal:
    """The overlay's tty while its popup runs; emptied after, for tasks that outlive it."""

    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd


# The overlay's terminal while a popup runs on one, for programs it starts.
# A holder rather than the fd: a task the popup started keeps this context
# after the popup, and must not then find a closed (or reused) fd number.
_overlay_terminal: ContextVar[_Terminal | None] = ContextVar("overlay_terminal", default=None)


def open_apc(token: str, layout: str, title: str = "") -> str:
    if layout not in LAYOUTS:
        raise ValueError(f"Not a popup layout: {layout!r}")
    named = f";{_encode_title(title)}" if title else ""
    return f"\x1b_pcode;popup;{token};{layout}{named}\x1b\\"


def pick_apc(token: str, layout: str) -> str:
    if layout not in LAYOUTS:
        raise ValueError(f"Not a popup layout: {layout!r}")
    return f"\x1b_pcode;pick;{token};{layout}\x1b\\"


def close_apc(token: str) -> str:
    return f"\x1b_pcode;popup-close;{token}\x1b\\"


def _encode_title(title: str) -> str:
    return base64.b64encode(title[:TITLE_MAX].encode("utf-8", "replace")).decode("ascii")


def title_apc(title: str) -> bytes:
    return b"\x1b_pcode;title;" + _encode_title(title).encode("ascii") + b"\x1b\\"


def _overlay_writer(output: Output | None) -> "_OverlayWriter | None":
    # An overlay's output is a Vt100_Output (perhaps wrapped, which forwards
    # `stdout`) writing to the overlay's tty.
    writer = getattr(output, "stdout", None)
    return writer if isinstance(writer, _OverlayWriter) else None


def drawn_in_overlay() -> bool:
    """True while the app being drawn draws on an overlay, which has its own frame.

    Asked at render: a popup's widgets may be built before its surface is.
    """
    app = get_app_or_none()
    return app is not None and _overlay_writer(app.output) is not None


def set_title(title: str) -> None:
    """Show `title` in the overlay's header, if the app being drawn is on one.

    Sent with the next frame, and only when it changed. An empty one puts
    back the title the popup opened with.
    """
    app = get_app_or_none()
    writer = _overlay_writer(app.output if app is not None else None)
    if writer is not None:
        writer.title = title or writer.opened_title


def terminal_fds() -> dict[str, Any]:
    """`subprocess` arguments that run a program in the popup's terminal.

    Empty on the full screen, where the popup's terminal is pcode's own. On an
    overlay the program ($EDITOR) runs in the overlay, as the popup does.
    """
    terminal = _overlay_terminal.get()
    fd = terminal.fd if terminal is not None else None
    if fd is None:
        return {}
    return {"stdin": fd, "stdout": fd, "stderr": fd}


@dataclass
class PopupSurface:
    """What a popup draws on: pass `input` and `output` to its Application.

    A native picker's surface has neither (both None): only `pick` works on it.
    """

    input: Input | None
    output: Output | None
    overlay: bool = False
    # The overlay's AppSession, whose `app` is the popup once it runs.
    session: AppSession | None = field(default=None, repr=False)
    native: "_NativePicker | None" = field(default=None, repr=False)

    async def pick(
        self,
        items: "list[PickItem]",
        *,
        title: str,
        header: str = "",
        empty: str = "No matches.",
        selected: str | None = None,
        query: str = "",
        match: "Match" = "fuzzy",
        actions: "tuple[PickAction, ...]" = (),
        custom: str | None = None,
        style=None,
    ) -> "Picked | None":
        """Pick from `items`; None when cancelled. See `pick_ui` for the options.

        Natively the terminal draws the list; otherwise `PickList` does, with
        `style` (the editor's). Call it again on the same surface to show
        another list in its place, as a toggle does.
        """
        options = dict(
            title=title,
            header=header,
            empty=empty,
            selected=selected,
            query=query,
            match=match,
            actions=actions,
            custom=custom,
        )
        if self.native is not None:
            return await self.native.pick(items, **options)
        from pcode.pick_ui import PickList

        picker = PickList(items, **options, input=self.input, output=self.output, style=style)
        return await picker.run()


class _NativePicker:
    """The terminal's native pick list, over the socket it connected to."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.cancelled = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    async def pick(self, items: "list[PickItem]", **options) -> "Picked | None":
        from pcode.pick_ui import checked, payload

        if self.cancelled.is_set():
            return None
        message = json.dumps(payload(items, **options), ensure_ascii=False) + "\n"
        reading = waiting = None
        try:
            self.writer.write(message.encode("utf-8"))
            await self.writer.drain()
            reading = asyncio.ensure_future(self.reader.readline())
            waiting = asyncio.ensure_future(self.cancelled.wait())
            await asyncio.wait({reading, waiting}, return_when=asyncio.FIRST_COMPLETED)
            line = reading.result() if reading.done() else b""
        except OSError, ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError:
            line = b""
        finally:
            for task in (reading, waiting):
                if task is not None and not task.done():
                    task.cancel()
        try:
            # A line cut short is the terminal closing the picker.
            reply = json.loads(line) if line.endswith(b"\n") else None
        except ValueError:
            reply = None
        picked = checked(reply, items, options["actions"], options["custom"])
        if picked is None:
            self.cancel()
        return picked


class _OverlayWriter:
    """The overlay's terminal as the file Vt100_Output writes to.

    Once the terminal has closed the overlay, writes fail; the popup is being
    cancelled by then, and its last frames go nowhere.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, fd: int, title: str = "") -> None:
        self.fd = fd
        self._pending: list[bytes] = []
        self.gone = False
        # Sent with the first frame, so the terminal never shows an empty popup.
        self.ready = READY
        # The header's title, sent before the next frame whenever it changes.
        # The one it opened with went in the open request, already shown.
        self.title = self.opened_title = self._sent_title = title

    def fileno(self) -> int:
        return self.fd

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._pending.append(text.encode(self.encoding, self.errors))
        return len(text)

    def flush(self) -> None:
        data, self._pending = b"".join(self._pending), []
        if data and self.ready:
            data, self.ready = self.ready + data, b""
        # Before `ready`, so the popup is shown with its header.
        if self.title != self._sent_title:
            data, self._sent_title = title_apc(self.title) + data, self.title
        deadline = time.monotonic() + _WRITE_TIMEOUT
        while data and not self.gone:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                # Shared with the bridge, the file could be non-blocking.
                if time.monotonic() > deadline:
                    # Not drained, yet not hung up: as good as gone.
                    self.gone = True
                    return
                select.select([], [self.fd], [], 0.05)
                continue
            except InterruptedError:
                continue
            except OSError:
                self.gone = True
                return
            data = data[written:]


class _OverlayInput(StartupInput):
    """The overlay's keys; its terminal hanging up cancels the popup, not EOFError.

    A hung-up terminal stays readable, so the popup keeps reading it and
    `hung_up` is asked again until the popup has gone: a hangup heard before
    the popup started running still cancels it once it does.
    """

    def __init__(self, stdin, hung_up: Callable[[], bool], cancel: Callable[[], None]) -> None:
        super().__init__(stdin, b"")
        self._hung_up = hung_up
        self._cancel = cancel

    def read_keys(self):
        keys = super().read_keys()
        if self._hung_up() or self.stdin_reader.closed:
            self._cancel()
            return []
        return keys

    @property
    def closed(self) -> bool:
        # prompt_toolkit raises EOFError for a closed input; the popup is
        # cancelled instead (`read_keys`), so post-popup code still runs.
        return False


def _private_dir():
    """`popup_dir()`, made if need be, and only if this user alone can use it."""
    directory = popup_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if info.st_uid != os.getuid() or not directory.is_dir() or directory.is_symlink():
        raise OSError(f"{directory} is not this user's own directory")
    if info.st_mode & 0o077:
        directory.chmod(0o700)
    # A pcode that died waiting for its overlay left its socket and entry.
    stale = time.time() - _STALE_AFTER
    for path in directory.iterdir():
        with suppress(OSError):
            if path.lstat().st_mtime < stale:
                path.unlink()
    return directory


def _write_entry(path, python: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as entry:
        json.dump({"python": python, "protocol": PROTOCOL}, entry)


def _cancel(session: AppSession) -> None:
    """Cancel the popup running in `session`, as its own cancel key would."""
    app = session.app
    if app is None or not app.is_running:
        return
    if getattr(app, "_running_in_terminal", False):
        # $EDITOR has the overlay: leave it be. Once it ends the popup reads
        # its input again, and a hung-up one cancels it then.
        return
    future = getattr(app, "future", None)
    if future is not None and future.done():
        return
    with suppress(Exception):
        app.exit(result=None)


@contextmanager
def _inert_editor(main_input: Input, cancel: Callable[[], None]):
    """Drop keys that reach the editor while a popup has the keyboard.

    The terminal moves focus to the overlay, but split navigation or a tab
    switch can bring it back to pcode's pane; a key typed there must not land
    in the draft (a secret, say). Cursor reports still go to the editor, which
    keeps drawing the conversation. Ctrl+C there, or Escape on its own,
    cancels the popup: if the overlay ever went without hanging up, that is
    the way out.
    """
    read_keys, flush_keys = main_input.read_keys, main_input.flush_keys
    kept = (Keys.CPRResponse,)

    def filtered(read, cancelling):
        def keys():
            pressed = read()
            if any(key.key in cancelling for key in pressed):
                cancel()
            return [key for key in pressed if key.key in kept]

        return keys

    # Read keys carry Escape before Alt+key and unknown sequences; a lone
    # Escape only ever comes out when the parser flushes it after a pause.
    main_input.read_keys = filtered(read_keys, (Keys.ControlC,))
    main_input.flush_keys = filtered(flush_keys, (Keys.ControlC, Keys.Escape))
    try:
        yield
    finally:
        # Instance attributes shadowed the methods; removing them restores them.
        for name in ("read_keys", "flush_keys"):
            with suppress(AttributeError):
                delattr(main_input, name)


class OverlayUnavailable(Exception):
    """The terminal didn't open the overlay; the popup takes the full screen instead."""


async def _receive(connection: socket.socket) -> tuple[bytes, list[int]]:
    """The bridge's hello and the fds sent with it, without blocking the loop."""
    loop = asyncio.get_running_loop()
    connection.setblocking(False)
    while True:
        try:
            message, fds, flags, _ = socket.recv_fds(connection, 64, 1)
        except BlockingIOError:
            readable = loop.create_future()
            loop.add_reader(connection.fileno(), readable.set_result, None)
            try:
                await readable
            finally:
                loop.remove_reader(connection.fileno())
            continue
        if flags & socket.MSG_CTRUNC:
            for fd in fds:
                os.close(fd)
            return b"", []
        return message, fds


def _give_up(main_output: Output, token: str) -> None:
    main_output.write_raw(close_apc(token))
    main_output.flush()


@asynccontextmanager
async def overlay(
    main_output: Output,
    main_input: Input,
    *,
    layout: str = "wide",
    title: str = "",
    timeout: float = CONNECT_TIMEOUT,
    python: str | None = None,
) -> AsyncIterator[PopupSurface]:
    """Open an overlay popup surface; raises `OverlayUnavailable` before yielding if it can't.

    `title` heads the overlay until the popup sets its own (`set_title`).
    """
    token = secrets.token_hex(8)
    try:
        directory = _private_dir()
        entry = directory / f"{token}.json"
        address = directory / f"{token}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise OverlayUnavailable(str(error)) from error
    loop = asyncio.get_running_loop()
    connection = None
    fds: list[int] = []
    try:
        try:
            server.bind(str(address))
            server.listen(1)
            server.setblocking(False)
            _write_entry(entry, python or sys.executable)
        except OSError as error:
            raise OverlayUnavailable(str(error)) from error
        main_output.write_raw(open_apc(token, layout, title))
        main_output.flush()
        try:
            async with asyncio.timeout(timeout):
                connection, _ = await loop.sock_accept(server)
                message, fds = await _receive(connection)
        except (TimeoutError, OSError) as error:
            _give_up(main_output, token)
            raise OverlayUnavailable("the terminal did not open the popup") from error
        except asyncio.CancelledError:
            _give_up(main_output, token)
            raise
        finally:
            # Found once; nothing else may connect.
            for path in (entry, address):
                with suppress(OSError):
                    path.unlink()
            server.close()
        fd = fds[0] if fds else None
        if not message.startswith(HELLO) or fd is None or not os.isatty(fd):
            _give_up(main_output, token)
            raise OverlayUnavailable("the popup's terminal did not check out")
        writer = _OverlayWriter(fd, title)
        session_box: list[AppSession] = []

        def cancel() -> None:
            writer.gone = True
            if session_box:
                _cancel(session_box[0])

        def resized() -> None:
            app = session_box[0].app if session_box else None
            if app is not None and app.is_running:
                app._on_resize()

        def bridge_spoke() -> None:
            try:
                data = connection.recv(256)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                # The bridge went: the terminal closed the overlay.
                loop.remove_reader(connection.fileno())
                cancel()
            elif RESIZED in data:
                resized()

        stdin = open(fd, encoding="utf-8", errors="replace", closefd=False)
        popup_input = _OverlayInput(stdin, lambda: writer.gone, cancel)
        popup_output = keyboard_output(
            Vt100_Output.from_pty(
                writer,
                term=os.environ.get("TERM"),
                default_color_depth=main_output.get_default_color_depth(),
            )
        )
        loop.add_reader(connection.fileno(), bridge_spoke)
        terminal = _Terminal(fd)
        terminal_token = _overlay_terminal.set(terminal)
        try:
            with (
                _inert_editor(main_input, cancel),
                create_app_session(popup_input, popup_output) as session,
            ):
                session_box.append(session)
                yield PopupSurface(popup_input, popup_output, overlay=True, session=session)
        finally:
            terminal.fd = None
            _overlay_terminal.reset(terminal_token)
            with suppress(Exception):
                loop.remove_reader(connection.fileno())
            clear_typeahead(popup_input)
            popup_input.close()
            stdin.close()
            # Shown or not, it closes: never flash an empty popup first.
            writer.ready = b""
            writer.write(DONE.decode())
            writer.flush()
    finally:
        if connection is not None:
            connection.close()
        for fd in fds:
            os.close(fd)


@asynccontextmanager
async def pick_overlay(
    main_output: Output,
    main_input: Input,
    *,
    layout: str = "sheet",
    timeout: float = CONNECT_TIMEOUT,
) -> AsyncIterator[PopupSurface]:
    """Open a native picker (protocol 2); raises `OverlayUnavailable` before yielding if it can't.

    The surface it yields only picks (`PopupSurface.pick`): the terminal draws
    the list. Escape or Ctrl+C in pcode's pane cancels it, as on an overlay.
    """
    token = secrets.token_hex(8)
    try:
        address = _private_dir() / f"{token}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise OverlayUnavailable(str(error)) from error
    loop = asyncio.get_running_loop()
    writer: asyncio.StreamWriter | None = None
    try:
        try:
            server.bind(str(address))
            server.listen(1)
            server.setblocking(False)
        except OSError as error:
            raise OverlayUnavailable(str(error)) from error
        main_output.write_raw(pick_apc(token, layout))
        main_output.flush()
        try:
            async with asyncio.timeout(timeout):
                connection, _ = await loop.sock_accept(server)
                reader, writer = await asyncio.open_unix_connection(
                    sock=connection, limit=_PICK_REPLY_LIMIT
                )
                hello = await reader.readline()
        except (TimeoutError, OSError, ValueError, asyncio.LimitOverrunError) as error:
            _give_up(main_output, token)
            raise OverlayUnavailable("the terminal did not open the picker") from error
        except asyncio.CancelledError:
            _give_up(main_output, token)
            raise
        finally:
            # Found once; nothing else may connect.
            with suppress(OSError):
                address.unlink()
            server.close()
        if hello != PICK_HELLO:
            _give_up(main_output, token)
            raise OverlayUnavailable("the picker did not check out")
        picker = _NativePicker(reader, writer)
        with _inert_editor(main_input, picker.cancel):
            yield PopupSurface(None, None, overlay=True, native=picker)
    finally:
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            # The terminal closes it on EOF; this covers one that missed it.
            _give_up(main_output, token)
