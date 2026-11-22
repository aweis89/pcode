"""Pick lists (`pcode.pick_ui`): matching, the terminal list, and the native picker's protocol.

The native tests play Ghostty Smooth: when pcode asks for a picker, a thread
connects to its socket as the Swift client does, reads the list and answers.
"""

import asyncio
import json
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path

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

# Ghostty's Swift port of `fuzzy_match` must agree on every case: its
# `PCodePickMatchTests` reads a byte-for-byte copy of this file.
FUZZY_FIXTURE = Path(__file__).parent / "data" / "pick_fuzzy_cases.json"
FIXTURE = json.loads(FUZZY_FIXTURE.read_text())
FUZZY_CASES = [(case["term"], case["text"], case["match"]) for case in FIXTURE["fuzzy"]]


@pytest.mark.parametrize(("term", "text", "expected"), FUZZY_CASES)
def test_fuzzy_match_table(term, text, expected):
    assert fuzzy_match(term, text) is expected


@pytest.mark.parametrize("case", FIXTURE["casefold"], ids=lambda case: case["text"])
def test_casefold_table(case):
    # Queries are casefolded on both sides; Swift's fold must give the same.
    assert case["text"].casefold() == case["folded"]


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

    def __init__(self, connection: socket.socket, hello: bytes = PICK_HELLO, ack=True) -> None:
        self.connection = connection
        self.file = connection.makefile("rb")
        self.ack = ack
        connection.sendall(hello)

    def read(self) -> dict | None:
        """The next list, said shown as Ghostty does; None at EOF."""
        line = self.file.readline()
        if not line:
            return None
        if self.ack:
            self.connection.sendall(b'{"shown": true}\n')
        return json.loads(line)

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
    from pcode.ui import PALETTES

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
                style=PALETTES["dark"].prompt_style(),
            )

    assert asyncio.run(main()) == Picked("b", None, "ban")
    terminal.join()
    sent, after = seen
    assert after is None
    assert sent["title"] == "Fruit" and sent["header"] == "2 fruit"
    assert sent["selected"] == "a" and sent["match"] == "fuzzy" and sent["custom"] == r"p:\w+"
    # The editor's colors ride along, for the terminal to draw the list in.
    assert sent["style"] == {"accent": "#88c0d0", "selected": "#384457", "dim": "#8994a6"}
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


def test_a_cancel_does_not_wait_on_a_picker_that_stopped_reading():
    """A list bigger than the socket's buffers, to a terminal that never reads it."""
    hold = threading.Event()

    def answer(connection, token):
        Client(connection)
        hold.wait(10)

    terminal = Terminal(answer)
    items = [PickItem(str(n), f"item {n} " + "x" * 80) for n in range(5000)]
    editor = EditorInput([KeyPress(Keys.ControlC, "\x03")])

    async def main():
        async with pick_overlay(terminal, editor) as surface:
            task = asyncio.ensure_future(surface.pick(items, title="Big"))
            await asyncio.sleep(0.2)
            assert not task.done()
            # pcode is stuck in drain(): the list does not fit the socket.
            assert surface.native.writer.transport.get_write_buffer_size() > 0
            started = time.monotonic()
            editor.read_keys()  # Ctrl+C in pcode's pane.
            result = await asyncio.wait_for(task, 1)
        return result, time.monotonic() - started

    try:
        result, took = asyncio.run(main())
    finally:
        hold.set()
        terminal.join()
    assert result is None
    # The surface closed too, without flushing to the stalled reader.
    assert took < 1


def fallback_screen(seen: list):
    @asynccontextmanager
    async def full_screen():
        class Screen:
            async def pick(self, items, **options):
                seen.append(("full screen", [item.id for item in items], options["title"]))
                return Picked(items[-1].id)

        yield Screen()

    return full_screen


@pytest.mark.parametrize("error", [True, False], ids=["error", "hangup"])
def test_a_picker_that_cannot_show_the_list_falls_back(error):
    """A version skew: the terminal cannot read pcode's list."""

    def answer(connection, token):
        client = Client(connection, ack=False)
        client.file.readline()
        if error:
            connection.sendall(b'{"error": "unknown key: items"}\n')

    terminal = Terminal(answer)
    seen: list = []
    failures: list[str] = []

    async def main():
        async with pick_overlay(terminal, EditorInput()) as surface:
            surface.full_screen = fallback_screen(seen)
            surface.native_failed = failures.append
            first = await surface.pick(ITEMS, title="One")
            # Every later pick on the surface takes the full screen at once.
            second = await surface.pick(ITEMS[:1], title="Two")
            assert surface.native.broken
            return first, second

    assert asyncio.run(main()) == (Picked("b"), Picked("a"))
    terminal.join()
    assert seen == [("full screen", ["a", "b"], "One"), ("full screen", ["a"], "Two")]
    assert len(failures) == 1
    assert ("unknown key: items" in failures[0]) is error
    token = PICK.search(terminal.written[0])[1]
    assert popup_surface.close_apc(token) in terminal.written


def test_a_hangup_after_the_list_was_shown_is_a_cancel():
    def answer(connection, token):
        Client(connection).read()

    terminal = Terminal(answer)
    seen: list = []

    async def main():
        async with pick_overlay(terminal, EditorInput()) as surface:
            surface.full_screen = fallback_screen(seen)
            return await surface.pick(ITEMS, title="t")

    assert asyncio.run(main()) is None
    terminal.join()
    assert seen == []


def test_cancel_keys_close_the_list_and_escape_clears_first():
    with create_pipe_input() as pipe:
        picker = PickList(ITEMS, title="t", cancel_keys=("c-l",), input=pipe, output=DummyOutput())
        assert run_list(picker, "ban\x0c", pipe) is None
    with create_pipe_input() as pipe:
        picker = PickList(ITEMS, title="t", cancel_keys=("c-l",), input=pipe, output=DummyOutput())

        async def run():
            task = asyncio.create_task(picker.run())
            await asyncio.sleep(0.05)
            pipe.send_text("ban\x1b")
            await asyncio.sleep(0.6)
            assert picker.search.text == "" and not task.done()
            pipe.send_text("\r")
            return await asyncio.wait_for(task, 3)

        # The row picked while searching stays selected.
        assert asyncio.run(run()) == Picked("b")


def test_the_model_list_closes_on_the_key_that_opens_it():
    from pcode.pick_ui import payload

    assert payload([], title="t", cancel_keys=("c-l",))["cancel_keys"] == ["c-l"]


HEX_COLOR = re.compile(r"#[0-9a-f]{6}")


def _palette_styles():
    from pcode.ui import PALETTES, syntax_palette

    for name, palette in PALETTES.items():
        yield pytest.param(name, palette.prompt_style(), id=name)
        # A syntax style colors the list's selected row (the menu palette).
        for syntax in ("gruvbox-dark", "solarized-light", "bw"):
            name_syntax = f"{name}+{syntax}"
            style = palette.prompt_style(syntax_palette(syntax, palette))
            yield pytest.param(name_syntax, style, id=name_syntax)


@pytest.mark.parametrize(("name", "style"), list(_palette_styles()))
def test_a_native_list_wears_the_terminal_lists_colors(name, style):
    from pcode.pick_ui import native_style, payload

    colors = native_style(style)
    assert set(colors) == {"accent", "selected", "dim"}
    assert all(HEX_COLOR.fullmatch(color) for color in colors.values()), colors
    sent = json.loads(json.dumps(payload(ITEMS, title="t", style=colors)))
    assert sent["style"] == colors


def test_the_palettes_own_colors_are_the_ones_sent():
    from pcode.pick_ui import native_style
    from pcode.ui import PALETTES

    light = PALETTES["light"]
    assert native_style(light.prompt_style()) == {
        "accent": light.accent,
        "selected": light.selected,
        "dim": light.muted,
    }


def test_a_reversed_row_sends_its_text_color_as_the_selected_background():
    """A reversed row draws its text color behind the terminal's background."""
    from prompt_toolkit.styles import Style

    from pcode.pick_ui import native_style

    colors = native_style(Style.from_dict({"popup selected": "reverse bg:default #2AA1B3"}))
    assert colors["selected"] == "#2aa1b3"
    assert "accent" not in colors


def test_colors_the_terminal_names_itself_are_left_to_it():
    """`/syntax terminal` names ANSI colors and reverses the row: nothing to send."""
    from pcode.pick_ui import native_style, payload
    from pcode.ui import TERMINAL_PALETTE

    assert native_style(TERMINAL_PALETTE.prompt_style()) == {}
    assert native_style(None) == {}
    assert payload([], title="t")["style"] == {}


def test_an_empty_attach_list_says_how_to_start_one():
    with create_pipe_input() as pipe:
        picker = PickList(
            [],
            title="t",
            empty=host_ui.empty([]),
            actions=(host_ui.NEW_SESSION,),
            input=pipe,
            output=DummyOutput(),
        )
        assert "Ctrl+N starts one" in str(picker.fragments())


def test_labels_are_cut_by_display_width():
    from prompt_toolkit.utils import get_cwidth

    from pcode.pick_ui import cut

    assert cut("短い名前のセッション", 9) == "短い名前…"
    assert get_cwidth(cut("短い名前のセッション", 9)) <= 9
    assert cut("short", 9) == "short"


def native_app(monkeypatch):
    """An app whose `popup(pick=True)` opens a native picker that only records."""
    from types import SimpleNamespace

    app, session, opened = make_app(monkeypatch, native=True)

    @asynccontextmanager
    async def native(*args, **kwargs):
        opened.append("native")
        picker = SimpleNamespace(hang_up=lambda: opened.append("hung up"), broken=False)
        yield popup_surface.PopupSurface(None, None, overlay=True, native=picker)

    monkeypatch.setattr(popup_surface, "pick_overlay", native)
    return app, session, opened


def test_a_native_surface_takes_the_full_screen_while_it_holds_the_popup(monkeypatch):
    """/links runs $EDITOR so: a popup queued meanwhile cannot supersede it."""
    app, session, opened = native_app(monkeypatch)
    app._command_popup_generation = app._popup_generation

    async def main():
        async with app.popup(None, session, pick=True) as surface:
            # A popup requested meanwhile moved the generation on.
            app._popup_generation += 1
            surface.leave_native()
            async with surface.full_screen() as screen:
                assert app._popup_lock.locked()
                return screen

    assert asyncio.run(main()) == "full screen"
    assert opened == ["native", "hung up", "full screen"]


def test_a_failed_native_list_turns_native_lists_off(monkeypatch):
    app, session, opened = native_app(monkeypatch)

    async def main():
        async with app.popup(None, session, pick=True) as surface:
            surface.native_failed("it could not read the list")
        assert app._overlay_failures == 1 and app._native_pick_off
        async with app.popup(None, session, pick=True) as surface:
            return surface

    assert asyncio.run(main()) == "overlay"
    assert opened == ["native", "overlay"]
    assert "could not show a native list" in app.transcript.console.file.getvalue()


def test_the_swift_copy_of_the_fixture_is_in_sync():
    """Ghostty Smooth's `PCodePickMatchTests` reads a copy; point at a checkout to compare."""
    import os

    root = os.environ.get("PCODE_GHOSTTY_DIR")
    if not root:
        pytest.skip("set PCODE_GHOSTTY_DIR to a Ghostty Smooth checkout")
    copy = Path(root) / "macos" / "Tests" / "Features" / "PCode" / FUZZY_FIXTURE.name
    assert copy.read_bytes() == FUZZY_FIXTURE.read_bytes()
