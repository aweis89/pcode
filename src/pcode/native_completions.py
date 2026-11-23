"""The editor's completion menu, drawn natively by the terminal (popup protocol 3).

In Ghostty Smooth the `/` and `@` menus need not be drawn in the terminal
grid, where a tall one scrolls the conversation away (`replay_after_menu`).
The terminal draws them as a floating list over its pane instead. The list is
display only: it never takes the keyboard, so typing, arrows, Tab, Enter and
Escape stay prompt_toolkit's, and every completer runs here unchanged. pcode
mirrors `buffer.complete_state` to the terminal after each render.

The session opens one socket, the first time the editor draws:

    pcode -> terminal   `ESC _ pcode;complete;<token> ESC \\`
    terminal -> pcode   connects to `<popup_dir>/<token>.sock`, sends
                        `pcode-complete 1\\n`

then newline-delimited JSON (UTF-8) for as long as both last:

    pcode -> terminal   the menu to show, replacing the last one:
                        {"seq": int, "items": [{"text": str, "meta": str}],
                         "selected": int|null, "offset": int, "total": int,
                         "dx": int, "dy": int, "x": int, "y": int,
                         "style": {...}}
                        or `{"hide": true}`
    terminal -> pcode   a row was clicked: {"accept": int, "seq": int}

`items` is a window of at most `WINDOW` completions, starting at `offset` of
`total`; `selected` and `accept` index into that window. `dx` is where the
completed word starts, in cells from the cursor (zero or less), and `dy` how
many rows above the cursor's row the editor's box starts: the list sits above
the box, or below the cursor where there is no room above. `style` is as for
a native pick list (`pick_ui.native_style`). A click on a list that has since
changed (its `seq` is not the last sent) is ignored.

Either side closing the socket ends it; the terminal hides the list on EOF.
pcode then draws the menu in the grid again, and reconnects on its next
render. A connection that lasted a while does not count against `ATTEMPTS`
(a stray `pcode;complete` APC in other output replaces it), but one that ends
quickly does. A terminal that does not connect in time, or stops reading,
ends it for the session.

The message also carries the cursor's cell in pcode's screen (`x`, `y`), so a
prompt that moved with an unchanged menu is still sent, and the terminal
places the list again.
"""

import asyncio
import json
import logging
import secrets
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from prompt_toolkit.utils import get_cwidth

from pcode.popup_surface import CONNECT_TIMEOUT, _private_dir, _same_user

HELLO = b"pcode-complete 1\n"
# Completions sent at once; the window follows the selection.
WINDOW = 200
# Characters of a row's text or meta the terminal is sent.
TEXT_MAX = 200
# Bytes queued for a terminal that has stopped reading: give up on it.
_BACKLOG = 1 << 20
# The longest line the terminal may send.
_REPLY_LIMIT = 1 << 12
# Connections tried per session before the grid menu is kept for good.
ATTEMPTS = 3
# Seconds a connection must last not to count against `ATTEMPTS`.
_LASTED = 5.0

log = logging.getLogger(__name__)


def complete_apc(token: str) -> str:
    return f"\x1b_pcode;complete;{token}\x1b\\"


def menu_message(
    completions: list,
    selected: int | None,
    offset: int,
    *,
    dx: int,
    dy: int,
    cursor: tuple[int, int] = (0, 0),
    style: dict[str, str],
) -> dict[str, Any]:
    """The menu as the terminal is sent it, without its `seq`."""
    window = completions[offset : offset + WINDOW]
    return {
        "items": [
            {
                "text": completion.display_text[:TEXT_MAX],
                "meta": completion.display_meta_text[:TEXT_MAX],
            }
            for completion in window
        ],
        "selected": None if selected is None else selected - offset,
        "offset": offset,
        "total": len(completions),
        "dx": dx,
        "dy": dy,
        "x": cursor[0],
        "y": cursor[1],
        "style": style,
    }


def window_offset(offset: int, selected: int | None, total: int) -> int:
    """Keep `offset` while the selection is in its window, else center the selection."""
    if total <= WINDOW:
        return 0
    if selected is None:
        return 0  # A fresh or refiltered list shows its top.
    if offset <= selected < offset + WINDOW and offset + WINDOW <= total:
        return offset
    return max(0, min(selected - WINDOW // 2, total - WINDOW))


def start_column(state, document) -> int:
    """Cells from the cursor back to where the completed word starts, as zero or less.

    Measured from the menu's original document: a selected completion is
    already in `document`, but the text before the word never changes.
    """
    completion = state.current_completion or state.completions[0]
    start = state.original_document.cursor_position + completion.start_position
    word = document.text_before_cursor[max(0, start) :]
    return -get_cwidth(word.rsplit("\n", 1)[-1])


class NativeCompletions:
    """The terminal's completion list for one editor: a socket kept for the session."""

    def __init__(
        self,
        write_raw: Callable[[str], None],
        accept: Callable[[int], None],
        *,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.write_raw = write_raw
        self.accept = accept
        self.timeout = timeout
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task | None = None
        self.attempts = 0
        # The last menu sent (without its seq), or None when hidden.
        self.shown: dict[str, Any] | None = None
        self.seq = 0
        self.offset = 0
        # Called when the connection opens or ends, so the menu is drawn where it now goes.
        self.on_change: Callable[[], None] | None = None

    @property
    def active(self) -> bool:
        """Whether the terminal draws the menu now (the grid menu is hidden)."""
        return self.writer is not None

    @property
    def given_up(self) -> bool:
        return self.writer is None and self.task is None and self.attempts >= ATTEMPTS

    def connect(self) -> None:
        """Open the socket, unless it is open, opening, or given up on."""
        if self.writer is not None or self.task is not None or self.attempts >= ATTEMPTS:
            return
        self.attempts += 1
        self.task = asyncio.get_running_loop().create_task(self._run())

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        self._drop()

    def show(self, message: dict[str, Any]) -> None:
        if self.writer is None or message == self.shown:
            return
        self.seq += 1
        self.shown = message
        self._send({"seq": self.seq, **message})

    def hide(self) -> None:
        if self.writer is None or self.shown is None:
            return
        self.shown = None
        self.seq += 1
        self._send({"hide": True})

    def _send(self, message: dict[str, Any]) -> None:
        writer = self.writer
        if writer is None:
            return
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            writer.write(line.encode("utf-8"))
        except OSError, RuntimeError:
            self._lost("the terminal hung up")
            return
        if writer.transport.get_write_buffer_size() > _BACKLOG:
            # The terminal stopped reading: the grid menu is drawn instead, for good.
            self.attempts = ATTEMPTS
            self._lost("the terminal stopped reading")

    def _drop(self) -> None:
        writer, self.writer = self.writer, None
        self.shown = None
        if writer is not None:
            # Not `close()`, which waits to flush to a terminal that may not read.
            with suppress(Exception):
                writer.transport.abort()

    def _lost(self, reason: str) -> None:
        log.info("Native completion list closed: %s", reason)
        self._drop()
        if self.on_change is not None:
            self.on_change()

    async def _run(self) -> None:
        try:
            reader = await self._open()
            if reader is None:
                return
            opened = time.monotonic()
            await self._read(reader)
            if time.monotonic() - opened > _LASTED:
                self.attempts -= 1
        except asyncio.CancelledError:
            self._drop()
            raise
        finally:
            self.task = None
        if self.writer is not None:  # Else `_send` already gave it up.
            self._lost("the terminal closed it")

    async def _open(self) -> asyncio.StreamReader | None:
        """Connect the terminal; None (and given up on) if it does not."""
        token = secrets.token_hex(8)
        try:
            address = _private_dir() / f"{token}.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as error:
            log.warning("Native completion list unavailable: %s", error)
            self.attempts = ATTEMPTS
            return None
        loop = asyncio.get_running_loop()
        writer: asyncio.StreamWriter | None = None
        try:
            server.bind(str(address))
            server.listen(1)
            server.setblocking(False)
            self.write_raw(complete_apc(token))
            async with asyncio.timeout(self.timeout):
                connection, _ = await loop.sock_accept(server)
                if not _same_user(connection):
                    connection.close()
                    raise OSError("the list is another user's")
                reader, writer = await asyncio.open_unix_connection(
                    sock=connection, limit=_REPLY_LIMIT
                )
                hello = await reader.readline()
        except (TimeoutError, OSError, ValueError, asyncio.LimitOverrunError) as error:
            # A terminal that never connects will not later: the grid it is.
            log.warning("Native completion list unavailable: %s", error or type(error).__name__)
            self.attempts = ATTEMPTS
            if writer is not None:
                writer.transport.abort()
            return None
        finally:
            # Found once; nothing else may connect.
            with suppress(OSError):
                address.unlink()
            server.close()
        if hello != HELLO:
            writer.transport.abort()
            log.warning("Native completion list did not check out: %r", hello[:40])
            self.attempts = ATTEMPTS
            return None
        self.writer = writer
        self.shown = None
        if self.on_change is not None:
            self.on_change()
        return reader

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except OSError, ValueError, asyncio.LimitOverrunError:
                return
            if not line.endswith(b"\n"):
                return
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if not isinstance(reply, dict):
                continue
            index, seq = reply.get("accept"), reply.get("seq")
            shown = self.shown
            if (
                type(index) is int
                and seq == self.seq
                and shown is not None
                and 0 <= index < len(shown["items"])
            ):
                self.accept(shown["offset"] + index)
