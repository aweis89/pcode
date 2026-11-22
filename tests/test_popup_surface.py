"""Overlay popups (`pcode.popup_surface`) against a real pty and the real bridge.

The test plays the terminal: when pcode asks for a popup it starts
`pcode.popup_bridge` on a fresh pty, as Ghostty Smooth does in the overlay
surface, then types into and reads from the pty's other end.
"""

import asyncio
import os
import pty
import re
import signal
import subprocess
import sys
import threading

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth

from pcode import popup_surface
from pcode.popup_bridge import popup_dir
from pcode.popup_surface import OverlayUnavailable, overlay, terminal_fds

OPEN = re.compile(r"\x1b_pcode;popup;([0-9a-f]{16});(sheet|wide)\x1b\\")


class Terminal:
    """pcode's own terminal, and the overlay it opens when asked."""

    def __init__(self, *, answer: bool = True) -> None:
        self.answer = answer
        self.written: list[str] = []
        self.bridge: subprocess.Popen | None = None
        self.master: int | None = None
        self.screen = bytearray()
        self._reader: threading.Thread | None = None

    # The main output pcode writes its APCs to.
    def write_raw(self, data: str) -> None:
        self.written.append(data)
        match = OPEN.search(data)
        if match and self.answer:
            self.open(match[1])

    def flush(self) -> None:
        pass

    def get_size(self):
        return (24, 80)

    def get_default_color_depth(self) -> ColorDepth:
        return ColorDepth.DEPTH_8_BIT

    def open(self, token: str) -> None:
        self.master, slave = pty.openpty()
        self.bridge = subprocess.Popen(
            [sys.executable, "-I", "-m", "pcode.popup_bridge", token],
            stdin=slave,
            start_new_session=True,
        )
        os.close(slave)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            self.screen.extend(data)

    def type(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> None:
        if self.bridge is not None and self.bridge.poll() is None:
            self.bridge.kill()
            self.bridge.wait(5)
        # macOS blocks close() while another thread reads the fd: the reader
        # ends once every slave fd is closed (pcode's, the bridge's).
        if self._reader is not None:
            self._reader.join(5)
        if self.master is not None:
            os.close(self.master)


class EditorInput:
    """The editor's input: keys typed into pcode's pane, and a cursor report."""

    def __init__(self) -> None:
        from prompt_toolkit.key_binding.key_processor import KeyPress

        self.keys = [KeyPress("x", "x"), KeyPress(Keys.CPRResponse, "\x1b[3;1R")]

    def read_keys(self):
        return list(self.keys)

    def flush_keys(self):
        return list(self.keys)


def picker(surface, seen: list) -> Application:
    keys = KeyBindings()

    @keys.add("enter")
    def _(event):
        event.app.exit(result="picked")

    @keys.add("escape", eager=True)
    def _(event):
        event.app.exit(result=None)

    def before_render(app):
        seen.append(app.output.get_size())

    app = Application(
        layout=Layout(Window(FormattedTextControl("pick one"))),
        key_bindings=keys,
        full_screen=True,
        input=surface.input,
        output=surface.output,
    )
    app.before_render += before_render
    return app


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture(autouse=True)
def short_home(monkeypatch):
    """A home short enough for a socket path (104 bytes on macOS); pytest's is not."""
    import shutil
    import tempfile

    home = tempfile.mkdtemp(prefix="pc", dir="/tmp")
    monkeypatch.setenv("HOME", home)
    yield
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def terminal():
    terminal = Terminal()
    yield terminal
    terminal.close()


def test_a_popup_runs_on_the_overlay_and_gives_it_back(terminal):
    editor = EditorInput()
    seen: list = []

    async def main():
        async with overlay(terminal, editor, layout="sheet") as surface:
            assert surface.overlay
            # The editor hears cursor reports, never keys, while the popup is up.
            assert [key.key for key in editor.read_keys()] == [Keys.CPRResponse]
            assert terminal_fds()["stdin"] == surface.input.fileno()
            app = picker(surface, seen)
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            terminal.type(b"\r")
            result = await asyncio.wait_for(task, 5)
        return result

    assert asyncio.run(main()) == "picked"
    assert b"\x1b_pcode;ready\x1b\\" in terminal.screen
    assert terminal.screen.rstrip().endswith(b"\x1b_pcode;done\x1b\\")
    assert terminal_fds() == {}
    assert [key.key for key in editor.read_keys()] == ["x", Keys.CPRResponse]
    # The socket and entry are gone once found.
    assert list(popup_dir().iterdir()) == []
    assert terminal.bridge.wait(5) is not None


def test_the_overlay_is_titled_before_it_is_shown(terminal):
    seen: list = []

    async def main():
        async with overlay(terminal, EditorInput(), title="Diffs") as surface:
            app = picker(surface, seen)
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            terminal.type(b"\r")
            await asyncio.wait_for(task, 5)

    asyncio.run(main())
    screen = bytes(terminal.screen)
    title = popup_surface.title_apc("Diffs")
    assert screen.count(title) == 1
    assert screen.index(title) < screen.index(popup_surface.READY)


def test_closing_the_overlay_cancels_the_popup(terminal):
    seen: list = []

    async def main():
        async with overlay(terminal, EditorInput()) as surface:
            app = picker(surface, seen)
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            # As the terminal does on Cmd+W: hang the overlay up.
            os.killpg(terminal.bridge.pid, signal.SIGHUP)
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) is None


def test_a_resize_reaches_the_popup(terminal):
    import fcntl
    import struct
    import termios

    seen: list = []

    async def main():
        async with overlay(terminal, EditorInput()) as surface:
            app = picker(surface, seen)
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            fcntl.ioctl(terminal.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            os.killpg(terminal.bridge.pid, signal.SIGWINCH)
            await _wait_for(lambda: any(size.columns == 100 for size in seen), timeout=0.4)
            terminal.type(b"\r")
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) == "picked"
    assert seen[-1].rows == 30


def test_a_terminal_that_never_opens_it_falls_back():
    terminal = Terminal(answer=False)

    async def main():
        async with overlay(terminal, EditorInput(), timeout=0.2):
            raise AssertionError("not reached")

    with pytest.raises(OverlayUnavailable):
        asyncio.run(main())
    token = OPEN.search(terminal.written[0])[1]
    assert terminal.written[-1] == popup_surface.close_apc(token)
    assert list(popup_dir().iterdir()) == []


def test_the_popup_directory_is_private(terminal):
    async def main():
        async with overlay(terminal, EditorInput()):
            pass

    asyncio.run(main())
    assert popup_dir().stat().st_mode & 0o777 == 0o700


def test_escape_in_the_main_pane_cancels_the_popup(terminal):
    from prompt_toolkit.key_binding.key_processor import KeyPress

    editor = EditorInput()
    editor.keys = [KeyPress(Keys.Escape, "\x1b")]

    async def main():
        async with overlay(terminal, editor) as surface:
            app = picker(surface, [])
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            # The overlay went missing without hanging up; Escape in pcode's pane.
            assert editor.flush_keys() == []
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) is None


def test_a_task_the_popup_started_finds_no_terminal_after_it(terminal):
    async def main():
        async with overlay(terminal, EditorInput()):
            assert terminal_fds()
            gate = asyncio.Event()

            async def outlives():
                await gate.wait()
                return terminal_fds()

            task = asyncio.ensure_future(outlives())
        gate.set()
        return await task

    assert asyncio.run(main()) == {}


def test_leftovers_are_swept(terminal):
    import time as clock

    directory = popup_dir()
    directory.mkdir(mode=0o700, parents=True)
    old = directory / ("0" * 16 + ".json")
    old.write_text("{}")
    os.utime(old, (clock.time() - 3600,) * 2)

    async def main():
        async with overlay(terminal, EditorInput()):
            assert not old.exists()

    asyncio.run(main())


def test_alt_keys_in_the_main_pane_leave_the_popup_alone(terminal):
    from prompt_toolkit.key_binding.key_processor import KeyPress

    editor = EditorInput()
    editor.keys = [KeyPress(Keys.Escape, "\x1b"), KeyPress("b", "b")]

    async def main():
        async with overlay(terminal, editor) as surface:
            app = picker(surface, [])
            task = asyncio.ensure_future(app.run_async())
            await _wait_for(lambda: b"pick one" in terminal.screen)
            assert editor.read_keys() == []  # Alt+b: dropped, nothing cancelled
            await asyncio.sleep(0.1)
            assert not task.done()
            terminal.type(b"\r")
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) == "picked"
