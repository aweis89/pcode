"""The /btw thread in Ghostty Smooth's drawer: `python -m pcode.btw_tail HOST THREAD`.

The terminal app runs this in the drawer's own terminal (`pcode.drawer`), on the
session host's interpreter. It watches the host's side questions as an
observer, which may call nothing, and draws one thread with
`btw_view.ThreadWriter`, so the drawer has the terminal's font, size and
colors. Settled blocks come out paced as the conversation's do (`paced_scrollback`:
rolled in by row, or prose typed out). A width change draws the thread again
at the new width, whole; the only key it reads is Esc (or `q`), which gives
the keyboard back to pcode's pane. The keys that answer the thread there go in
the drawer's footer (`pcode;hints;…`, `popup_hints`).
"""

import asyncio
import contextlib
import os
import signal
import sys

from rich.console import Console
from rich.text import Text

from pcode.aside import Aside, apply_state
from pcode.btw_view import Entry, ThreadWriter, entry
from pcode.conversation_tree import excerpt
from pcode.drawer import FOCUS, OBSERVER_VERSION, valid_host
from pcode.host_protocol import LINE_LIMIT, dumps, read_message, socket_path
from pcode.popup_hints import Hint, hints, hints_apc
from pcode.prefix_keys import shortcut_label
from pcode.tool_display import literal, plain
from pcode.ui import ESCAPES, Pacer, QueuedRow, split_rows, typed_prose

# Top of the screen, then the screen and the scrollback cleared: a whole redraw.
CLEAR = "\x1b[H\x1b[2J\x1b[3J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
# Back to the start of the row, then everything from there down erased.
ERASE_DOWN = "\r\x1b[J"
# A cut-off row closes its styles and any hyperlink it was inside.
CLOSE_ROW = "\x1b[0m\x1b]8;;\x1b\\"
# The conversation's pace: a paced frame every 1/30 s.
FRAME_SECONDS = 1 / 30
# Width changes come in bursts while the drawer's edge is dragged.
RESIZE_SETTLE = 0.12
# How often to try the host again once it has gone: a /restart brings it back.
RECONNECT_SECONDS = 2.0
# What a host on the web drawer's observer version answers.
OLD_HOST = "Unsupported drawer version."
RESTART = "This session host's pcode predates this drawer: run /restart in pcode."


class Tail:
    """One thread on screen, kept up with the host's side questions."""

    def __init__(
        self,
        thread: str,
        console: Console,
        transcript,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.thread = thread
        self.console = console
        self.transcript = transcript
        self.asides: list[Aside] = []
        self.writer = ThreadWriter()
        self.note = ""
        # The footer last sent to the drawer (`footer`); None before the first.
        self._footer: list[Hint] | None = None
        # Pacing needs a loop to time its frames; without one, text lands at once.
        self.loop = loop
        self.paced = loop is not None and transcript.paced_scrollback != "off"
        self.typed = self.paced and transcript.paced_scrollback == "typed"
        # Rendered rows not yet written, and characters of the first already shown.
        self.rows: list[QueuedRow] = []
        self._typed = 0
        self._pacer = Pacer()
        self._frame: asyncio.TimerHandle | None = None
        # Rows under the text, never wrapped (a row being typed, the status
        # row): erased before anything more is written. Kept as written, so a
        # frame that changes nothing writes nothing.
        self._live: list[str] = []

    def apply(self, message: dict) -> None:
        if message.get("type") != "notify":
            return
        method, args = message.get("method"), message.get("args") or []
        if method == "asides":
            self.asides = [self._aside(state) for state in (args[0] if args else [])]
            self.draw(whole=True)
        elif method == "aside_changed" and args:
            state = args[0]
            known = next((aside for aside in self.asides if aside.id == state["id"]), None)
            if known is None:
                self.asides.append(self._aside(state))
            else:
                apply_state(known, state)
            self.draw()
        elif method == "host_closed":
            self.closed("The session host stopped.")

    @staticmethod
    def _aside(state: dict) -> Aside:
        aside = Aside(question=state["question"], id=state["id"])
        apply_state(aside, state)
        return aside

    def closed(self, note: str) -> None:
        self.note = note
        self.draw()

    def reconnected(self) -> None:
        self.note = ""

    def shown(self) -> list[Aside]:
        return [aside for aside in self.asides if aside.thread == self.thread]

    def footer(self) -> list[Hint]:
        """The keys pcode's editor answers this thread with, as the drawer's footer shows them."""
        threads = {aside.thread for aside in self.asides}
        return hints("btw_drawer", label=shortcut_label, state={"others": lambda: len(threads) > 1})

    def _send_footer(self) -> None:
        footer = self.footer()
        if footer != self._footer:
            self._footer = footer
            self.console.file.write(hints_apc(footer).decode("ascii"))
            self.console.file.flush()

    def draw(self, *, whole: bool = False) -> None:
        thread = self.shown()
        with self.console.use_theme(self.transcript.rich_theme):
            entries = None if whole else self.writer.entries(thread, self.transcript.code_theme)[0]
            if entries is None:
                # A whole redraw lands at once, as a rebuilt screen does in the
                # conversation; it covers whatever was still queued.
                self.console.file.write(CLEAR)
                self._live = []
                self.hold()
                self.rows, self._typed = [], 0
                self._pacer.reset()
                self.writer.reset()
                entries = self._heading(thread)
                entries += self.writer.entries(thread, self.transcript.code_theme)[0] or []
                self._queue(entries, typed=False)
                self._show(len(self.rows), 0)
            else:
                self._queue(entries, typed=self.typed)
                if not self.paced:
                    self._show(len(self.rows), 0)
                elif self._frame is None:
                    self._tick()
                else:
                    # The next frame writes the rows; the status row changes now.
                    self._show(0, self._typed)
        # After the frame, so a whole redraw still starts the output.
        self._send_footer()

    def _queue(self, entries: list[Entry], *, typed: bool) -> None:
        """Render entries into rows to write, marking prose to type out."""
        width = self.console.width
        for objects, end, _ in entries:
            with self.console.capture() as captured:
                self.console.print(*objects, end=end, width=width)
            prose = typed and typed_prose(objects)
            self.rows += [QueuedRow(row, prose) for row in split_rows(captured.get())]

    def hold(self) -> None:
        """Stop the frame timer, e.g. while a resize waits to draw everything again."""
        if self._frame is not None:
            self._frame.cancel()
            self._frame = None

    def _tick(self) -> None:
        """One paced frame; another is timed while rows wait."""
        self._frame = None
        with self.console.use_theme(self.transcript.rich_theme):
            self._show(*self._pacer.advance(self.rows, self._typed) if self.rows else (0, 0))
        if self.rows:
            self._frame = self.loop.call_later(FRAME_SECONDS, self._tick)
        else:
            self._pacer.reset()

    def _show(self, due: int, typed: int) -> None:
        """Write ``due`` rows, then the live rows: the next row typed so far, the status."""
        written = "".join(row.text for row in self.rows[:due])
        self.rows = self.rows[due:]
        self._typed = typed
        live = []
        if typed and self.rows:
            live.append(_typed_part(self.rows[0].text, typed) + CLOSE_ROW)
        status = plain(self.note or self._progress(self.shown()))
        if status:
            # One row, never wrapped: erasing it takes back exactly that row.
            row = Text(f"  {status}", style="pcode.muted", no_wrap=True, overflow="ellipsis")
            with self.console.capture() as captured:
                self.console.print(row, end="", crop=True)
            live.append(captured.get())
        if not written and live == self._live:
            return
        out = self.console.file
        if self._live:
            up = len(self._live) - 1
            out.write((f"\x1b[{up}A" if up else "") + ERASE_DOWN)
        out.write(written + "\n".join(live))
        self._live = live
        out.flush()

    @staticmethod
    def _progress(thread: list[Aside]) -> str:
        running = next((aside for aside in thread if aside.running), None)
        if running is None:
            return ""
        return f"{(running.activity or 'Answering').rstrip('…. ')}…"

    def _heading(self, thread: list[Aside]) -> list[Entry]:
        if not thread:
            return [entry(Text("Waiting for the side thread…", style="pcode.muted"))]
        first = thread[0]
        # The host's text, so nothing in it reaches this terminal as a control.
        question = excerpt(literal(first.question))[:80]
        label = plain(first.label, limit=60)
        title = Text.assemble(
            (" btw ", "pcode.btw.badge"),
            (f" {question}", "pcode.btw"),
            (f" · {label}" if label else "", "pcode.muted"),
        )
        return [entry(title), entry()]


def _typed_part(text: str, count: int) -> str:
    """The first ``count`` visible characters of a rendered row, its escapes kept."""
    parts, position = [], 0
    for match in ESCAPES.finditer(text):
        visible = text[position : match.start()]
        if len(visible) >= count:
            break
        parts += [visible, match.group()]
        count -= len(visible)
        position = match.end()
    else:
        visible = text[position:]
    parts.append(visible[:count])
    return "".join(parts)


async def run(host: str, thread: str) -> None:
    from pcode.ui import Activity, Transcript

    console = Console(force_terminal=True, highlight=False)
    # Reads the preferences and asks this terminal its background, as pcode does.
    transcript = Transcript(console, activity=Activity())
    sys.stdout.write(HIDE_CURSOR)
    loop = asyncio.get_running_loop()
    tail = Tail(thread, console, transcript, loop)

    redraw: asyncio.TimerHandle | None = None

    def resized() -> None:
        nonlocal redraw
        if redraw is not None:
            redraw.cancel()
        # Rows queued at the old width would wrap and spoil the erase; the
        # redraw writes them whole at the new one.
        tail.hold()
        redraw = loop.call_later(RESIZE_SETTLE, lambda: tail.draw(whole=True))

    loop.add_signal_handler(signal.SIGWINCH, resized)
    try:
        with _keys(loop):
            # Shown until the drawer closes, which ends this process: a host
            # that goes is tried again, so a /restart comes back by itself.
            while True:
                await _watch(host, tail)
                await asyncio.sleep(RECONNECT_SECONDS)
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()


async def _watch(host: str, tail: Tail) -> None:
    """Draw the host's side questions until it goes, or say why it cannot be reached."""
    try:
        path = str(socket_path(host))
        reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
    except OSError:
        if not tail.note:
            tail.closed(f"Session {host} is not running.")
        return
    try:
        writer.write(dumps({"type": "observe", "version": OBSERVER_VERSION}))
        await writer.drain()
        ready = await read_message(reader)
        if ready is None or ready.get("type") != "ready":
            message = (ready or {}).get("message") or RESTART
            tail.closed(RESTART if message == OLD_HOST else message)
            return
        tail.reconnected()
        while (message := await read_message(reader)) is not None:
            tail.apply(message)
        if not tail.note:
            tail.closed("The session host stopped.")
    except (OSError, ValueError) as error:
        tail.closed(f"Lost the session host: {error}")
    finally:
        writer.close()


@contextlib.contextmanager
def _keys(loop: asyncio.AbstractEventLoop):
    """Keys typed here are not echoed; Esc or `q` asks the terminal app for pcode's pane."""
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except ImportError, OSError, ValueError:
        yield
        return
    tty.setcbreak(fd)

    def typed() -> None:
        try:
            data = os.read(fd, 1024)
        except OSError:
            data = b""
        if not data:
            # The terminal has gone: nothing more will be typed, and a
            # reader left on a closed descriptor would fire forever.
            loop.remove_reader(fd)
            return
        if data.endswith(b"\x1b") or b"q" in data:
            sys.stdout.write(FOCUS)
            sys.stdout.flush()

    loop.add_reader(fd, typed)
    try:
        yield
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not valid_host(args[0]):
        sys.exit("usage: python -m pcode.btw_tail HOST THREAD")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(*args))


if __name__ == "__main__":
    main()
