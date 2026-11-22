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

The overlay protocol (`ESC _ pcode;… ESC \\`):

    pcode;popup?                    probe; the terminal answers `pcode;popup;1`
    pcode;popup;<token>;<layout>    open a popup (`sheet` or `wide`) running the bridge
    pcode;popup-close;<token>       give up on that popup (it never came, say)
    pcode;ready                     (from the overlay) shown it: focus it
    pcode;done                      (from the overlay) close it

The token names a socket and an entry file in `popup_dir()`, which this user
alone can read; the entry names the Python that runs the bridge. The terminal
shows the overlay only once pcode, holding its terminal, says `ready` there, so
no other program's output can raise one. Closing it from the terminal (Cmd+W,
a click outside it) hangs its terminal up, and the popup is cancelled as if
its own cancel key had been pressed: `app.exit(result=None)`, which is what
every popup's cancel does.
"""

import asyncio
import json
import os
import secrets
import socket
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application.current import AppSession, create_app_session
from prompt_toolkit.input import Input
from prompt_toolkit.input.typeahead import clear_typeahead
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import Output
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode.keyboard_protocol import keyboard_output
from pcode.popup_bridge import HELLO, RESIZED, popup_dir
from pcode.startup_input import StartupInput

PROTOCOL = 1  # Probed as `drawer.POPUP_QUERY`.
LAYOUTS = ("sheet", "wide")
READY = b"\x1b_pcode;ready\x1b\\"
DONE = b"\x1b_pcode;done\x1b\\"
# How long the terminal gets to start the bridge. Python starts in ~50 ms;
# not answering at all means it can't reach the socket (a container, a VM).
CONNECT_TIMEOUT = 3.0

# The overlay's terminal while a popup runs on one, for programs it starts.
_overlay_fd: ContextVar[int | None] = ContextVar("overlay_fd", default=None)


def open_apc(token: str, layout: str) -> str:
    if layout not in LAYOUTS:
        raise ValueError(f"Not a popup layout: {layout!r}")
    return f"\x1b_pcode;popup;{token};{layout}\x1b\\"


def close_apc(token: str) -> str:
    return f"\x1b_pcode;popup-close;{token}\x1b\\"


def terminal_fds() -> dict[str, Any]:
    """`subprocess` arguments that run a program in the popup's terminal.

    Empty on the full screen, where the popup's terminal is pcode's own. On an
    overlay the program ($EDITOR) runs in the overlay, as the popup does.
    """
    fd = _overlay_fd.get()
    if fd is None:
        return {}
    return {"stdin": fd, "stdout": fd, "stderr": fd}


@dataclass
class PopupSurface:
    """What a popup draws on: pass `input` and `output` to its Application."""

    input: Input
    output: Output
    overlay: bool = False
    # The overlay's AppSession, whose `app` is the popup once it runs.
    session: AppSession | None = field(default=None, repr=False)


class _OverlayWriter:
    """The overlay's terminal as the file Vt100_Output writes to.

    Once the terminal has closed the overlay, writes fail; the popup is being
    cancelled by then, and its last frames go nowhere.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._pending: list[bytes] = []
        self.gone = False

    def fileno(self) -> int:
        return self.fd

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._pending.append(text.encode(self.encoding, self.errors))
        return len(text)

    def flush(self) -> None:
        data, self._pending = b"".join(self._pending), []
        while data and not self.gone:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
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
    future = getattr(app, "future", None)
    if future is not None and future.done():
        return
    with suppress(Exception):
        app.exit(result=None)


@contextmanager
def _inert_editor(main_input: Input):
    """Drop keys that reach the editor while a popup has the keyboard.

    The terminal moves focus to the overlay, but split navigation or a tab
    switch can bring it back to pcode's pane; a key typed there must not land
    in the draft (a secret, say). Cursor reports still go to the editor, which
    keeps drawing the conversation.
    """
    read_keys, flush_keys = main_input.read_keys, main_input.flush_keys
    kept = (Keys.CPRResponse,)

    def filtered(read):
        return lambda: [key for key in read() if key.key in kept]

    main_input.read_keys = filtered(read_keys)
    main_input.flush_keys = filtered(flush_keys)
    try:
        yield
    finally:
        # Instance attributes shadowed the methods; removing them restores them.
        for name in ("read_keys", "flush_keys"):
            with suppress(AttributeError):
                delattr(main_input, name)


class OverlayUnavailable(Exception):
    """The terminal didn't open the overlay; the popup takes the full screen instead."""


@asynccontextmanager
async def overlay(
    main_output: Output,
    main_input: Input,
    *,
    layout: str = "wide",
    timeout: float = CONNECT_TIMEOUT,
    python: str | None = None,
) -> AsyncIterator[PopupSurface]:
    """Open an overlay popup surface; raises `OverlayUnavailable` before yielding if it can't."""
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
    fd = None
    try:
        try:
            server.bind(str(address))
            server.listen(1)
            server.setblocking(False)
            _write_entry(entry, python or sys.executable)
        except OSError as error:
            raise OverlayUnavailable(str(error)) from error
        main_output.write_raw(open_apc(token, layout))
        main_output.flush()
        try:
            connection, _ = await asyncio.wait_for(loop.sock_accept(server), timeout)
            connection.settimeout(1.0)
            message, fds, _, _ = await asyncio.to_thread(socket.recv_fds, connection, 64, 1)
        except (TimeoutError, OSError) as error:
            main_output.write_raw(close_apc(token))
            main_output.flush()
            raise OverlayUnavailable("the terminal did not open the popup") from error
        finally:
            # Found once; nothing else may connect.
            for path in (entry, address):
                with suppress(OSError):
                    path.unlink()
            server.close()
        fd = fds[0] if fds else None
        for extra in fds[1:]:
            os.close(extra)
        if message != HELLO or fd is None or not os.isatty(fd):
            main_output.write_raw(close_apc(token))
            main_output.flush()
            raise OverlayUnavailable("the popup's terminal did not check out")
        connection.setblocking(False)
        writer = _OverlayWriter(fd)
        session_box: list[AppSession] = []

        def cancel() -> None:
            writer.gone = True
            if session_box:
                _cancel(session_box[0])

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
                return
            if RESIZED in data and session_box:
                app = session_box[0].app
                if app is not None and app.is_running:
                    app._on_resize()

        stdin = open(fd, encoding="utf-8", errors="replace", closefd=False)
        popup_input = _OverlayInput(stdin, lambda: writer.gone, cancel)
        popup_output = keyboard_output(
            Vt100_Output.from_pty(
                writer,
                term=os.environ.get("TERM"),
                default_color_depth=main_output.get_default_color_depth(),
            )
        )
        writer.write(READY.decode())
        writer.flush()
        loop.add_reader(connection.fileno(), bridge_spoke)
        overlay_token = _overlay_fd.set(fd)
        try:
            with (
                _inert_editor(main_input),
                create_app_session(popup_input, popup_output) as session,
            ):
                session_box.append(session)
                yield PopupSurface(popup_input, popup_output, overlay=True, session=session)
        finally:
            _overlay_fd.reset(overlay_token)
            with suppress(Exception):
                loop.remove_reader(connection.fileno())
            clear_typeahead(popup_input)
            popup_input.close()
            stdin.close()
            writer.write(DONE.decode())
            writer.flush()
    finally:
        if connection is not None:
            connection.close()
        if fd is not None:
            os.close(fd)
