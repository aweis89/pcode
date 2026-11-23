"""The completion menu drawn by the terminal (`pcode.native_completions`).

The tests play the terminal: they connect to the socket pcode names in its
APC, as Ghostty Smooth does, and read the menus it sends.
"""

import asyncio
import functools
import json
import re
import shutil
import socket
import tempfile
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.output import DummyOutput

from pcode import native_completions
from pcode.native_completions import (
    ATTEMPTS,
    HELLO,
    WINDOW,
    NativeCompletions,
    menu_message,
    start_column,
    window_offset,
)
from pcode.popup_bridge import popup_dir
from pcode.ui import PromptLayout

OPEN = re.compile(r"\x1b_pcode;complete;([0-9a-f]{16})\x1b\\")


@pytest.fixture(autouse=True)
def short_home(monkeypatch):
    """A home short enough for a socket path (104 bytes on macOS); pytest's is not."""
    home = tempfile.mkdtemp(prefix="pc", dir="/tmp")
    monkeypatch.setenv("HOME", home)
    yield
    shutil.rmtree(home, ignore_errors=True)


class Terminal:
    """The terminal side: connects when asked and records what pcode sends."""

    def __init__(self, *, hello: bytes = HELLO, answer: bool = True) -> None:
        self.hello = hello
        self.answer = answer
        self.connection: socket.socket | None = None
        self.received = b""

    def write_raw(self, text: str) -> None:
        match = OPEN.fullmatch(text)
        assert match, text
        if not self.answer:
            return
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.connect(str(popup_dir() / f"{match[1]}.sock"))
        self.connection.sendall(self.hello)
        self.connection.setblocking(False)

    async def messages(self, count: int) -> list[dict]:
        for _ in range(200):
            with_lines = self.received.count(b"\n")
            if with_lines >= count:
                break
            try:
                assert self.connection is not None
                self.received += self.connection.recv(65536)
            except BlockingIOError:
                await asyncio.sleep(0.01)
        lines = self.received.split(b"\n")[:-1]
        return [json.loads(line) for line in lines]

    def send(self, message: dict) -> None:
        assert self.connection is not None
        self.connection.sendall(json.dumps(message).encode() + b"\n")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()


async def until(condition) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def completions(*names: str) -> list[Completion]:
    return [Completion(name, start_position=-1, display_meta=f"about {name}") for name in names]


MENU = dict(dx=-1, dy=0, style={})


def in_loop(test):
    """Run `test` (async or not) inside an event loop, as the editor does."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        async def main():
            result = test(*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result

        asyncio.run(main())

    return run


@in_loop
async def test_it_connects_and_mirrors_the_menu():
    terminal = Terminal()
    accepted: list[int] = []
    changes: list[bool] = []
    native = NativeCompletions(terminal.write_raw, accepted.append)
    native.on_change = lambda: changes.append(native.active)
    native.connect()
    await until(lambda: native.active)
    assert changes == [True]
    # Nothing is left for another process to connect to.
    assert list(popup_dir().iterdir()) == []

    menu = menu_message(completions("/model", "/mode"), 1, 0, **MENU)
    native.show(menu)
    native.show(menu)  # Unchanged: not sent again.
    native.hide()
    native.hide()
    (shown, hidden) = await terminal.messages(2)
    assert shown == {"seq": 1, **menu}
    assert shown["items"][1] == {"text": "/mode", "meta": "about /mode"}
    assert shown["selected"] == 1
    assert hidden == {"hide": True}

    native.show(menu)
    *_, again = await terminal.messages(3)
    terminal.send({"accept": 0, "seq": 1})  # A list since replaced.
    terminal.send({"accept": 1, "seq": again["seq"]})
    terminal.send({"accept": 7, "seq": again["seq"]})  # Not a row.
    await until(lambda: accepted)
    await asyncio.sleep(0.05)
    assert accepted == [1]

    terminal.close()
    await until(lambda: not native.active)
    assert changes == [True, False]
    native.close()


@in_loop
async def test_a_terminal_that_never_connects_is_given_up_on():
    terminal = Terminal(answer=False)
    native = NativeCompletions(terminal.write_raw, lambda index: None, timeout=0.05)
    native.connect()
    await until(lambda: native.given_up)
    assert not native.active
    native.connect()
    assert native.task is None


@in_loop
async def test_a_wrong_hello_is_given_up_on():
    terminal = Terminal(hello=b"pcode-pick 1\n")
    native = NativeCompletions(terminal.write_raw, lambda index: None)
    native.connect()
    await until(lambda: native.given_up)
    terminal.close()


@in_loop
async def test_it_reconnects_a_few_times_after_the_terminal_hangs_up():
    terminals: list[Terminal] = []

    def write_raw(text: str) -> None:
        terminals.append(Terminal())
        terminals[-1].write_raw(text)

    native = NativeCompletions(write_raw, lambda index: None)
    for _ in range(ATTEMPTS):
        native.connect()
        await until(lambda: native.active)
        terminals[-1].close()
        await until(lambda: not native.active and native.task is None)
    native.connect()
    assert native.given_up and len(terminals) == ATTEMPTS


def test_the_window_follows_the_selection():
    assert window_offset(0, 5, 10) == 0
    assert window_offset(0, None, 1000) == 0
    assert window_offset(0, WINDOW - 1, 1000) == 0
    assert window_offset(0, WINDOW, 1000) == WINDOW - WINDOW // 2
    assert window_offset(300, 350, 1000) == 300
    assert window_offset(300, 999, 1000) == 1000 - WINDOW
    # Refiltered (nothing selected): back to the top, wherever it was.
    assert window_offset(300, None, 1000) == 0
    assert window_offset(900, None, 300) == 0

    menu = menu_message(completions(*map(str, range(1000))), 450, 400, **MENU)
    assert len(menu["items"]) == WINDOW
    assert menu["items"][0]["text"] == "400"
    assert (menu["selected"], menu["offset"], menu["total"]) == (50, 400, 1000)


def test_the_list_starts_where_the_word_does():
    document = Document("look at @src/pcode/ui")
    state = CompletionState(document, [Completion("@src/pcode/ui.py", start_position=-13)])
    assert start_column(state, document) == -13
    # Wide characters take two cells; the line before a newline is not counted.
    document = Document("one\n@日本")
    state = CompletionState(document, [Completion("@日本語", start_position=-3)])
    assert start_column(state, document) == -5


def test_the_list_stays_put_as_the_selection_fills_in_the_word():
    original = Document("hi /he")
    state = CompletionState(
        original, [Completion("/help", -3), Completion("/history-long", -3)], complete_index=1
    )
    # The selected completion is in the buffer by now.
    assert start_column(state, Document("hi /history-long")) == -13


# The editor's side: what `PromptLayout` sends after a render.


class FakeNative:
    active = True

    def __init__(self) -> None:
        self.sent: list[dict | None] = []
        self.offset = 0

    def connect(self) -> None:
        pass

    def show(self, message: dict) -> None:
        self.sent.append(message)

    def hide(self) -> None:
        self.sent.append(None)


def prompt_layout():
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
    transcript = SimpleNamespace(replays_on_resize=False)
    prompt = PromptLayout(session, None, transcript, None)
    native = FakeNative()
    prompt.native = native
    return prompt, native


def render(prompt, *, cursor: Point | None, frame_top: int = 0):
    from prompt_toolkit.layout import Window

    top = Window()
    prompt.frame = top
    screen = SimpleNamespace(
        cursor_positions={} if cursor is None else {prompt.editor: cursor},
        visible_windows_to_write_positions={top: WritePosition(0, frame_top, 10, 1)},
    )
    app = SimpleNamespace(
        renderer=SimpleNamespace(_last_screen=screen),
        layout=SimpleNamespace(has_focus=lambda buffer: True),
        is_done=False,
        style=prompt.session.app.style,
    )
    prompt.sync_native_menu(app)


@in_loop
def test_the_editor_sends_its_menu_and_hides_it():
    prompt, native = prompt_layout()
    buffer = prompt.session.default_buffer
    buffer.document = Document("/mo")
    buffer.complete_state = CompletionState(
        buffer.document, [Completion("/model", -3), Completion("/mode", -3)], complete_index=1
    )
    render(prompt, cursor=Point(x=7, y=12), frame_top=10)
    (menu,) = native.sent
    assert [item["text"] for item in menu["items"]] == ["/model", "/mode"]
    assert (menu["selected"], menu["dx"], menu["dy"]) == (1, -3, 2)

    # The editor not drawn (a popup has the screen): hidden.
    render(prompt, cursor=None)
    buffer.complete_state = None
    render(prompt, cursor=Point(x=7, y=12))
    assert native.sent[1:] == [None, None]


@in_loop
def test_the_grid_menu_steps_aside_while_the_terminal_draws_it():
    prompt, native = prompt_layout()
    buffer = prompt.session.default_buffer
    buffer.complete_state = CompletionState(buffer.document, [Completion("/model", 0)])
    menu = prompt.menu.filter
    with set_app(prompt.session.app):
        native.active = True
        assert not menu()
        native.active = False
        assert menu()


@in_loop
def test_a_click_takes_the_completion_as_enter_would():
    prompt, _ = prompt_layout()
    buffer = prompt.session.default_buffer
    buffer.document = Document("/mo")
    shown = [Completion("/model", -3), Completion("/mode", -3)]
    # A list the terminal never showed (it changed since): the click is ignored.
    buffer.complete_state = CompletionState(buffer.document, list(shown))
    prompt.native_completions = shown
    prompt.accept_native_completion(1)
    assert buffer.text == "/mo"
    buffer.complete_state = CompletionState(buffer.document, shown)
    prompt.accept_native_completion(1)
    assert buffer.text == "/mode"
    assert buffer.complete_state is None
    prompt.accept_native_completion(0)  # No menu now: nothing happens.
    assert buffer.text == "/mode"


@in_loop
def test_only_a_terminal_that_draws_menus_gets_one(monkeypatch):
    from pcode import theme

    for protocol, expected in ((None, False), (2, False), (3, True)):
        monkeypatch.setattr(theme, "_popup_protocol", protocol)
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput())
        prompt = PromptLayout(session, None, SimpleNamespace(), None)
        assert isinstance(prompt.native, native_completions.NativeCompletions) is expected
