"""Pick lists (`pcode.pick_ui`): matching, the terminal list, and the native picker's protocol.

The native tests play Ghostty Smooth: when pcode asks for a picker, a thread
connects to its socket as the Swift client does, reads the list and answers.
"""

import asyncio
import json
import re
import socket
import threading
from contextlib import asynccontextmanager
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from pcode import host_ui, popup_surface, theme
from pcode.host_protocol import HostEntry
from pcode.pick_ui import (
    PickAction,
    Picked,
    PickItem,
    PickList,
    checked,
    custom_item,
    tree_guides,
    visible,
)
from pcode.popup_bridge import popup_dir, socket_path
from pcode.popup_surface import PICK_HELLO, OverlayUnavailable, pick_overlay
from pcode.popup_ui import fuzzy_match, nest_forks

PICK = re.compile(r"\x1b_pcode;pick;([0-9a-f]{16});(sheet|wide)\x1b\\")

# Ghostty's Swift port of `fuzzy_match` must agree on every case
# (macos/Tests/PCode/PCodePickMatchTests.swift holds the same table).
FUZZY_CASES = [
    ("edit_ui", "src/pcode/edit_ui.py", True),
    ("ed_ui", "src/pcode/edit_ui.py", True),
    ("pc/edui", "src/pcode/edit_ui.py", True),
    ("edui", "src/pcode/edit_ui.py", True),
    ("dtui", "src/pcode/edit_ui.py", False),
    ("sel_row", "+self.selected_row = 1", True),
    ("_", "anything", False),
    ("", "anything", True),
    ("anthopus", "anthropic claude-opus-5", True),
    ("anthopus", "anthropic:claude-sonnet-5", False),
    ("claopus", "anthropic:claude-opus-5", True),
    ("codluna", "openai-codex:gpt-5.6-luna", True),
    ("gpt-5.6", "openai-codex:gpt-5.6-luna", True),
    ("gpt56", "openai-codex:gpt-5.6-luna", True),
    ("g5l", "openai-codex:gpt-5.6-luna", True),
    ("lunagpt", "openai-codex:gpt-5.6-luna", False),
    ("aab", "aa ab", True),
    ("abc", "a-b-c", True),
    ("abd", "ab-c", False),
    ("café", "le café", True),
    ("cafe", "le café", False),
    ("x", "", False),
]


@pytest.mark.parametrize(("term", "text", "expected"), FUZZY_CASES)
def test_fuzzy_match_table(term, text, expected):
    assert fuzzy_match(term, text) is expected


def test_tree_guides_draw_what_nest_forks_draws():
    parents = {"b": "a", "c": "a", "d": "b", "e": "d", "f": "c", "h": "g"}
    names = list("abcdefgh")
    rows = nest_forks(names, key=lambda name: name, parent=lambda name: parents.get(name))
    guides = [guide for _, guide in rows]
    assert tree_guides([len(guide) // 3 for guide in guides]) == guides
    assert guides[1:3] == ["├─ ", "│  └─ "]


def test_tree_guides_clamp_a_row_deeper_than_its_parent_allows():
    assert tree_guides([2, 3, 0]) == ["", "└─ ", ""]


def test_queries_match_every_word_against_any_search_text():
    items = [
        PickItem("1", "One", search="alpha\nbeta"),
        PickItem("2", "Two", search="alpha beta"),
        PickItem("3", "Three"),
    ]
    assert [item.id for item in visible(items, "alpha beta", "substring")] == ["1", "2"]
    assert [item.id for item in visible(items, "pha", "substring")] == ["1", "2"]
    assert [item.id for item in visible(items, "THREE")] == ["3"]
    assert [item.id for item in visible(items, "   ")] == ["1", "2", "3"]


def test_a_custom_row_needs_the_whole_query_and_a_new_id():
    items = [PickItem("p:known", "Known")]
    assert custom_item(" p:new ", items, r"p:[a-z]+") == PickItem("p:new", "p:new", marker="custom")
    assert custom_item("p:known", items, r"p:[a-z]+") is None
    assert custom_item("xp:new", items, r"p:[a-z]+") is None
    assert custom_item("p:new", items, None) is None
    assert visible(items, "p:new", custom=r"p:[a-z]+")[0].id == "p:new"


def test_replies_are_checked_against_the_list():
    items = [PickItem("a", "A")]
    actions = (PickAction("n", "New", needs_item=False), PickAction("x", "Stop"))
    assert checked({"id": "a", "query": "q"}, items, actions, None) == Picked("a", None, "q")
    assert checked({"id": None, "action": "n"}, items, actions, None) == Picked(None, "n")
    assert checked({"id": "p:x", "query": "p:x"}, items, actions, r"p:x") == Picked(
        "p:x", None, "p:x"
    )
    for bad in (
        {"id": "b"},  # Not listed.
        {"id": None},  # Enter needs a row.
        {"id": None, "action": "x"},  # So does this action.
        {"id": "a", "action": "z"},  # No such action.
        {"id": 3},
        {"id": "a", "query": 1},
        [],
        None,
    ):
        assert checked(bad, items, actions, None) is None


# The terminal's list


def run_list(picker: PickList, keys: str, pipe):
    async def run():
        task = asyncio.create_task(picker.run())
        await asyncio.sleep(0.05)
        pipe.send_text(keys)
        return await asyncio.wait_for(task, 3)

    return asyncio.run(run())


def hosts():
    original = HostEntry("orig0000", 1, "m", "/w/a", session_id="s-orig", title="orig", updated=1)
    fork = HostEntry(
        "fork0000", 1, "m", "/w/a", session_id="s-fork", forked_from="s-orig", title="fork"
    )
    working = HostEntry("work0000", 1, "m", "/w/b", title="busy", state="working", updated=2)
    stopped = HostEntry("stop0000", 1, "m", "/w/c", title="done", state="stopped", updated=0)
    return [original, fork, working, stopped]


def hosts_list(pipe, current=None):
    entries = hosts()
    return PickList(
        host_ui.host_items(entries, current, now=10),
        title=host_ui.TITLE,
        header=host_ui.header(entries),
        empty=host_ui.empty(entries),
        match="substring",
        actions=(host_ui.NEW_SESSION, host_ui.STOP),
        input=pipe,
        output=DummyOutput(),
    )


def test_attach_rows_order_nest_and_flag():
    items = host_ui.host_items(hosts(), "orig0000", now=10)
    assert [(item.id, item.depth) for item in items] == [
        ("work0000", 0),
        ("orig0000", 0),
        ("fork0000", 1),
        ("stop0000", 0),
    ]
    assert items[0].label.startswith("● working")
    assert items[1].marker == "this terminal"
    assert items[3].detail == "c · just now · stopped"
    assert items[3].confirm.startswith("remove done from this list")
    assert items[0].confirm == "stop work0000 (busy). Its turn is cancelled."


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("\r", ("attach", "work0000")),
        ("\x1b[B\x1b[B\r", ("attach", "fork0000")),
        ("\x0e", ("new", None)),  # Ctrl+N, the new-session shortcut.
        ("\x18\x18", ("stop", "work0000")),  # Ctrl+X twice.
        ("\x1b[3~\x1b[3~", ("stop", "work0000")),  # Delete twice, the query empty.
        ("\x18\x1b[B\x18\x03", None),  # Moving away disarms it.
        ("done\r", ("attach", "stop0000")),
        ("\x1b", None),
    ],
)
def test_attach_keys(keys, expected):
    with create_pipe_input() as pipe:
        assert host_ui.choice(run_list(hosts_list(pipe), keys, pipe)) == expected


def test_attach_stop_asks_first_and_says_what_it_does():
    async def run():
        with create_pipe_input() as pipe:
            picker = hosts_list(pipe)
            task = asyncio.create_task(picker.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\x18")
            await asyncio.sleep(0.1)
            assert picker.status == (
                "Press Ctrl+X again to stop work0000 (busy). Its turn is cancelled."
            )
            assert not task.done()
            pipe.send_text("\x1b[B")
            await asyncio.sleep(0.1)
            assert picker.status == ""
            pipe.send_text("\x03")
            assert await asyncio.wait_for(task, 3) is None

    asyncio.run(run())


def test_attach_nests_forks_only_while_the_query_is_empty():
    with create_pipe_input() as pipe:
        picker = hosts_list(pipe)
        assert "└─ " in str(picker.fragments())
        picker.search.text = "fork"
        assert [item.id for item in picker.matches] == ["fork0000"]
        assert "└─ " not in str(picker.fragments())


def test_a_list_starts_on_its_selected_row_and_query():
    items = [PickItem("a", "Apple"), PickItem("b", "Banana"), PickItem("c", "Cherry")]
    with create_pipe_input() as pipe:
        picker = PickList(
            items, title="t", selected="c", query="an", input=pipe, output=DummyOutput()
        )
        assert [item.id for item in picker.matches] == ["b"]
        assert run_list(picker, "\r", pipe) == Picked("b", None, "an")
    with create_pipe_input() as pipe:
        picker = PickList(items, title="t", selected="c", input=pipe, output=DummyOutput())
        assert picker.current().id == "c"


def test_an_action_without_a_row_still_works_on_an_empty_list():
    toggle = PickAction("t", "Toggle", needs_item=False)
    with create_pipe_input() as pipe:
        picker = PickList([], title="t", actions=(toggle,), input=pipe, output=DummyOutput())
        assert "No matches." in str(picker.fragments())
        assert run_list(picker, "\r\x14", pipe) == Picked(None, "t", "")


# The native picker


class Terminal:
    """pcode's own terminal, which plays Ghostty: asked for a picker, it connects."""

    def __init__(self, answer=None) -> None:
        # `answer(connection, token)` plays the picker; None never connects.
        self.answer = answer
        self.written: list[str] = []
        self.threads: list[threading.Thread] = []

    def write_raw(self, data: str) -> None:
        self.written.append(data)
        match = PICK.search(data)
        if match and self.answer is not None:
            thread = threading.Thread(target=self._connect, args=(match[1],), daemon=True)
            thread.start()
            self.threads.append(thread)

    def flush(self) -> None:
        pass

    def _connect(self, token: str) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(socket_path(token)))
        try:
            self.answer(connection, token)
        finally:
            connection.close()

    def join(self) -> None:
        for thread in self.threads:
            thread.join(5)


class Client:
    """The Swift side of one connection: its hello, then lists in and picks out."""

    def __init__(self, connection: socket.socket, hello: bytes = PICK_HELLO) -> None:
        self.connection = connection
        self.file = connection.makefile("rb")
        connection.sendall(hello)

    def read(self) -> dict | None:
        line = self.file.readline()
        return json.loads(line) if line else None

    def reply(self, **pick) -> None:
        self.connection.sendall(json.dumps(pick).encode() + b"\n")


class EditorInput:
    def __init__(self, keys=()) -> None:
        self.keys = list(keys)

    def read_keys(self):
        return list(self.keys)

    def flush_keys(self):
        return list(self.keys)


@pytest.fixture(autouse=True)
def short_home(monkeypatch):
    """A home short enough for a socket path (104 bytes on macOS); pytest's is not."""
    import shutil
    import tempfile

    home = tempfile.mkdtemp(prefix="pc", dir="/tmp")
    monkeypatch.setenv("HOME", home)
    yield
    shutil.rmtree(home, ignore_errors=True)


ITEMS = [
    PickItem("a", "Apple", detail="fruit", marker="current", search="Apple\nRed"),
    PickItem("b", "Banana", depth=1),
]


def test_a_native_pick_sends_the_list_and_returns_the_reply():
    seen: list = []

    def answer(connection, token):
        client = Client(connection)
        seen.append(client.read())
        client.reply(id="b", action=None, query="ban")
        seen.append(client.read())  # EOF: pcode is done with it.

    terminal = Terminal(answer)

    async def main():
        async with pick_overlay(terminal, EditorInput(), layout="sheet") as surface:
            assert surface.native is not None and surface.input is None
            return await surface.pick(
                ITEMS,
                title="Fruit",
                header="2 fruit",
                selected="a",
                custom=r"p:\w+",
                actions=(PickAction("n", "New", needs_item=False),),
            )

    assert asyncio.run(main()) == Picked("b", None, "ban")
    terminal.join()
    sent, after = seen
    assert after is None
    assert sent["title"] == "Fruit" and sent["header"] == "2 fruit"
    assert sent["selected"] == "a" and sent["match"] == "fuzzy" and sent["custom"] == r"p:\w+"
    assert sent["items"][0] == {
        "id": "a",
        "label": "Apple",
        "detail": "fruit",
        "marker": "current",
        "depth": 0,
        "search": ["apple", "red"],
        "confirm": "",
    }
    assert sent["actions"] == [
        {"key": "n", "label": "New", "needs_item": False, "confirm": "", "delete": False}
    ]
    token = PICK.search(terminal.written[0])[1]
    assert terminal.written[-1] == popup_surface.close_apc(token)
    # The socket was found once and is gone.
    assert list(popup_dir().iterdir()) == []


def test_a_native_picker_shows_the_next_list_in_place():
    lists: list = []

    def answer(connection, token):
        client = Client(connection)
        lists.append(client.read())
        client.reply(id="a", action="t", query="ap")
        lists.append(client.read())
        client.reply(id="b", query="")

    terminal = Terminal(answer)
    toggle = PickAction("t", "Toggle", needs_item=False)

    async def main():
        async with pick_overlay(terminal, EditorInput()) as surface:
            first = await surface.pick(ITEMS, title="One", actions=(toggle,))
            second = await surface.pick(ITEMS[1:], title="Two", query=first.query)
            return first, second

    assert asyncio.run(main()) == (Picked("a", "t", "ap"), Picked("b", None, ""))
    terminal.join()
    assert [entry["title"] for entry in lists] == ["One", "Two"]
    assert lists[1]["query"] == "ap"


@pytest.mark.parametrize(
    "reply", [None, b"not json\n", b'{"id": "zzz"}\n', b'{"id": "a"}'], ids=str
)
def test_a_native_picker_closing_or_talking_nonsense_cancels(reply):
    def answer(connection, token):
        client = Client(connection)
        client.read()
        if reply is not None:
            connection.sendall(reply)

    terminal = Terminal(answer)

    async def main():
        async with pick_overlay(terminal, EditorInput()) as surface:
            first = await surface.pick(ITEMS, title="t")
            # Cancelled once, it stays cancelled.
            return first, await surface.pick(ITEMS, title="t")

    assert asyncio.run(main()) == (None, None)
    terminal.join()


def test_escape_in_the_main_pane_cancels_a_native_pick():
    editor = EditorInput([KeyPress(Keys.Escape, "\x1b")])
    eof = threading.Event()

    def answer(connection, token):
        client = Client(connection)
        client.read()
        if client.read() is None:
            eof.set()

    terminal = Terminal(answer)

    async def main():
        async with pick_overlay(terminal, editor) as surface:
            task = asyncio.ensure_future(surface.pick(ITEMS, title="t"))
            await asyncio.sleep(0.1)
            assert not task.done()
            # The picker went missing without closing; Escape in pcode's pane.
            assert editor.flush_keys() == []
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) is None
    assert eof.wait(5)
    terminal.join()
    token = PICK.search(terminal.written[0])[1]
    assert terminal.written[-1] == popup_surface.close_apc(token)
    # The editor reads keys again.
    assert editor.read_keys() == editor.keys


def test_a_native_picker_that_never_connects_gives_up():
    terminal = Terminal(answer=None)

    async def main():
        async with pick_overlay(terminal, EditorInput(), timeout=0.2):
            raise AssertionError("not reached")

    with pytest.raises(OverlayUnavailable):
        asyncio.run(main())
    token = PICK.search(terminal.written[0])[1]
    assert terminal.written[-1] == popup_surface.close_apc(token)
    assert list(popup_dir().iterdir()) == []


def test_the_bridge_is_not_a_native_picker():
    terminal = Terminal(lambda connection, token: Client(connection, hello=b"pcode-popup 1\n"))

    async def main():
        async with pick_overlay(terminal, EditorInput(), timeout=2):
            raise AssertionError("not reached")

    with pytest.raises(OverlayUnavailable):
        asyncio.run(main())
    terminal.join()


# Where `App.popup(pick=True)` opens


@pytest.mark.parametrize("protocol", [1, 2])
def test_the_probe_reply_says_which_overlays_the_terminal_has(protocol):
    response = bytearray(b"before\x1b_pcode;popup;%d\x1b\\after" % protocol)
    theme._take_drawer_reply(response)
    try:
        assert response == b"beforeafter"
        assert theme.popup_supported()
        assert theme.native_pick_supported() is (protocol >= 2)
    finally:
        theme._take_drawer_reply(bytearray())


def make_app(monkeypatch, native: bool):
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp

    app = PreviewApp(console=Console(file=StringIO()))
    opened: list[str] = []
    monkeypatch.setattr(app, "popups_overlay", lambda: app._overlay_failures < 2)
    monkeypatch.setattr(theme, "native_pick_supported", lambda: native)

    @asynccontextmanager
    async def nowhere(*args, **kwargs):
        opened.append("native")
        raise OverlayUnavailable("no")
        yield

    @asynccontextmanager
    async def terminal_overlay(*args, **kwargs):
        opened.append("overlay")
        yield "overlay"

    @asynccontextmanager
    async def full_screen(output, session):
        opened.append("full screen")
        yield "full screen"

    monkeypatch.setattr(popup_surface, "pick_overlay", nowhere)
    monkeypatch.setattr(popup_surface, "overlay", terminal_overlay)
    monkeypatch.setattr(app, "_full_screen_popup", full_screen)
    session = SimpleNamespace(app=SimpleNamespace(output=None, input=None))
    return app, session, opened


def test_a_native_picker_that_fails_falls_back_to_the_full_screen(monkeypatch):
    app, session, opened = make_app(monkeypatch, native=True)

    async def main():
        surfaces = []
        for _ in range(3):
            async with app.popup(None, session, pick=True) as surface:
                surfaces.append(surface)
        return surfaces

    assert asyncio.run(main()) == ["full screen"] * 3
    # Twice running, then overlays stop being tried.
    assert opened == ["native", "full screen", "native", "full screen", "full screen"]


def test_an_older_terminal_picks_in_a_terminal_overlay(monkeypatch):
    app, session, opened = make_app(monkeypatch, native=False)

    async def main():
        async with app.popup(None, session, pick=True) as surface:
            return surface

    assert asyncio.run(main()) == "overlay"
    assert opened == ["overlay"]
