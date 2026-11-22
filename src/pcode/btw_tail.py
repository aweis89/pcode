"""The /btw thread in Ghostty Smooth's drawer: `python -m pcode.btw_tail HOST THREAD`.

The terminal app runs this in the drawer's own terminal (`pcode.drawer`), on the
session host's interpreter. It watches the host's side questions as an
observer, which may call nothing, and draws one thread the way the swap does
(`btw_view.ThreadWriter`), so the drawer has the terminal's font, size and
colors. A width change draws the thread again at the new width; the only key
it reads is Esc (or `q`), which gives the keyboard back to pcode's pane.
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
from pcode.tool_display import literal, plain

# Top of the screen, then the screen and the scrollback cleared: a whole redraw.
CLEAR = "\x1b[H\x1b[2J\x1b[3J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ERASE_LINE = "\r\x1b[2K"
# Width changes come in bursts while the drawer's edge is dragged.
RESIZE_SETTLE = 0.12
# How often to try the host again once it has gone: a /restart brings it back.
RECONNECT_SECONDS = 2.0
# What a host on the web drawer's observer version answers.
OLD_HOST = "Unsupported drawer version."
RESTART = "This session host's pcode predates this drawer: run /restart in pcode."


class Tail:
    """One thread on screen, kept up with the host's side questions."""

    def __init__(self, thread: str, console: Console, transcript) -> None:
        self.thread = thread
        self.console = console
        self.transcript = transcript
        self.asides: list[Aside] = []
        self.writer = ThreadWriter()
        self.note = ""
        # A status row is under the text (the answer being written): erased before more.
        self._status = False

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

    def draw(self, *, whole: bool = False) -> None:
        thread = self.shown()
        out = self.console.file
        with self.console.use_theme(self.transcript.rich_theme):
            if self._status:
                out.write(ERASE_LINE)
                self._status = False
            entries = None if whole else self.writer.entries(thread, self.transcript.code_theme)[0]
            if entries is None:
                out.write(CLEAR)
                self.writer.reset()
                entries = self._heading(thread)
                entries += self.writer.entries(thread, self.transcript.code_theme)[0] or []
            for objects, end, _ in entries:
                self.console.print(*objects, end=end)
            status = plain(self.note or self._progress(thread))
            if status:
                # One row, never wrapped: erasing it takes back exactly that row.
                row = Text(f"  {status}", style="pcode.muted", no_wrap=True, overflow="ellipsis")
                self.console.print(row, end="", crop=True)
                self._status = True
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
        keys = Text("Reply in pcode's editor · Esc back to it", style="pcode.muted")
        return [entry(title), entry(keys), entry()]


async def run(host: str, thread: str) -> None:
    from pcode.ui import Activity, Transcript

    console = Console(force_terminal=True, highlight=False)
    # Reads the preferences and asks this terminal its background, as pcode does.
    transcript = Transcript(console, activity=Activity())
    sys.stdout.write(HIDE_CURSOR)
    tail = Tail(thread, console, transcript)
    loop = asyncio.get_running_loop()

    redraw: asyncio.TimerHandle | None = None

    def resized() -> None:
        nonlocal redraw
        if redraw is not None:
            redraw.cancel()
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
