"""Shortcuts behind the configurable prefix: Ctrl chords, or a leader and a letter."""

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.popup_ui import popup_container
from pcode.preferences import SETTINGS, parse_key_prefix, save_preferences
from pcode.prefix_keys import PrefixKeys, key_label, shortcut_label
from pcode.ui import create_prompt


@pytest.mark.parametrize(
    "value,keys",
    [
        ("ctrl", ()),
        (" CTRL ", ()),
        ("ctrl+p", ("c-p",)),
        ("C-p", ("c-p",)),
        ("Ctrl-P", ("c-p",)),
        ("ctrl+space", ("c-@",)),
        ("ctrl+]", ("c-]",)),
        ("ctrl+\\", ("c-\\",)),
        ("f2", ("f2",)),
        # Several keys make one leader, pressed in turn, as in Emacs.
        ("ctrl+x ctrl+p", ("c-x", "c-p")),
    ],
)
def test_parse_key_prefix(value, keys):
    assert parse_key_prefix(value) == keys
    SETTINGS["key_prefix"].validate("key_prefix", value)


@pytest.mark.parametrize(
    "value,message",
    [
        ("", "must be ctrl"),
        ("p", "not a key"),
        ("alt+p", "not a key"),
        ("ctrl+1", "not a key"),
        ("f25", "not a key"),
        ("ctrl+ctrl", "not a key"),
        # A leader must not take a key every surface needs for itself.
        ("ctrl+c", "cancels"),
        ("ctrl+j", "newline"),
        ("ctrl+m", "is Enter"),
        ("ctrl+x ctrl+d", "exits"),
    ],
)
def test_parse_key_prefix_rejects_unusable_keys(value, message):
    with pytest.raises(ValueError, match=message):
        parse_key_prefix(value)
    with pytest.raises(ValueError, match=message):
        SETTINGS["key_prefix"].validate("key_prefix", value)


def test_labels_follow_the_prefix():
    assert key_label("c-p") == "Ctrl+P"
    assert key_label("c-@") == "Ctrl+Space"
    assert key_label("f2") == "F2"
    assert shortcut_label("s", "ctrl") == "Ctrl+S"
    assert shortcut_label("^", "ctrl") == "Ctrl+^"
    assert shortcut_label("s", "ctrl+p") == "Ctrl+P s"
    assert shortcut_label("s", "ctrl+x ctrl+p") == "Ctrl+X Ctrl+P s"
    # Without an argument it reads the saved setting.
    assert shortcut_label("s") == "Ctrl+S"
    save_preferences(key_prefix="ctrl+space")
    assert shortcut_label("s") == "Ctrl+Space s"
    assert PrefixKeys().leader == ("c-@",)


@pytest.mark.parametrize("key", ["c", "d", "m", "j", "A", "1", "/"])
def test_shortcuts_need_a_free_ctrl_chord(key):
    with pytest.raises(ValueError, match="free Ctrl chord"):
        PrefixKeys("ctrl+p").add(key, "Nope")


def test_a_key_is_one_shortcut():
    shortcuts = PrefixKeys("ctrl")
    shortcuts.add("y", "Copy")(lambda event: None)
    with pytest.raises(ValueError, match="already"):
        shortcuts.add("y", "Copy again")


def test_summary_and_hint_list_what_applies_now():
    available = [True]
    for prefix, summary in [
        ("ctrl", "Ctrl+/ Keys"),
        ("ctrl+p", "Ctrl+P Keys"),
    ]:
        shortcuts = PrefixKeys(prefix)
        shortcuts.add("y", "Copy")(lambda event: None)
        shortcuts.add("k", "Stop", filter=Condition(lambda: available[0]))(lambda event: None)
        assert shortcuts.summary() == summary
        assert shortcuts.label("k") == ("Ctrl+K" if prefix == "ctrl" else "Ctrl+P k")
        assert shortcuts.hint_rows() == [
            (shortcuts.label("y"), "Copy"),
            (shortcuts.label("k"), "Stop"),
        ]
        assert shortcuts.hint_footer() == [("Esc", "cancel"), ("Ctrl+/", "all keys")]
    shortcuts.pending = True
    assert shortcuts.summary() == "^P …"
    assert shortcuts.hint_rows() == [
        ("y", "Copy"),
        ("k", "Stop"),
    ]
    available[0] = False
    assert shortcuts.hint_rows() == [("y", "Copy")]
    shortcuts.dismiss()
    assert shortcuts.summary() == "Ctrl+P Keys"


class Surface:
    """A search line with a bare Enter binding, the shape every popup has."""

    def __init__(self, prefix: str, pipe, output=None) -> None:
        self.pipe = pipe
        self.fired: list[str] = []
        self.pasted: list[str] = []
        self.shortcuts = PrefixKeys(prefix)
        self.shortcuts.add("y", "Copy")(lambda event: self.fired.append("y"))
        self.shortcuts.add("k", "Stop", filter=False)(lambda event: self.fired.append("k"))
        self.query = TextArea(multiline=False)
        keys = KeyBindings()

        @keys.add("enter")
        def enter(event):
            event.app.exit(result=self.query.text)

        @keys.add(Keys.BracketedPaste)
        def paste(event):
            self.pasted.append(event.data)

        self.app = Application(
            # A filler below, as in a real popup: the hint floats over the screen.
            layout=Layout(popup_container(HSplit([self.query, Window()]), self.shortcuts)),
            key_bindings=self.shortcuts.key_bindings(keys),
            full_screen=True,
            input=pipe,
            output=output or DummyOutput(),
        )

    async def press(self, text: str) -> None:
        self.pipe.send_text(text)
        await asyncio.sleep(0.05)


async def surface(prefix: str, pipe, output=None) -> tuple[Surface, asyncio.Task]:
    view = Surface(prefix, pipe, output)
    task = asyncio.create_task(view.app.run_async())
    await asyncio.sleep(0.05)
    return view, task


def test_ctrl_chords_fire_from_a_search_line_while_letters_type():
    async def run():
        with create_pipe_input() as pipe:
            view, task = await surface("ctrl", pipe)
            await view.press("yak\x19")
            assert view.query.text == "yak" and view.fired == ["y"]
            # An unavailable shortcut does nothing; its chord keeps its old job.
            await view.press("\x0b")
            assert view.fired == ["y"]
            await view.press("\r")
            assert await asyncio.wait_for(task, 2) == "yak"

    asyncio.run(run())


def test_a_leader_owns_the_next_key_whatever_has_focus():
    async def run():
        with create_pipe_input() as pipe:
            view, task = await surface("ctrl+p", pipe)
            await view.press("a\x10")
            assert view.shortcuts.pending
            await view.press("y")
            assert not view.shortcuts.pending
            assert view.fired == ["y"] and view.query.text == "a"
            # Unknown/unavailable keys and Enter are consumed, with feedback;
            # they neither type nor reach the popup's own Enter binding.
            await view.press("\x10")
            for unknown in ("z", "\r", "k"):
                await view.press(unknown)
                assert view.shortcuts.pending
                assert view.shortcuts.message == f"No binding for {unknown!r}"
                assert view.query.text == "a" and not task.done()
            for cancel in ("\x1b", "\x10"):
                await view.press(cancel)
                await asyncio.sleep(0.6 if cancel == "\x1b" else 0)
                assert not view.shortcuts.pending, repr(cancel)
                assert not view.shortcuts.message
                assert view.query.text == "a" and not task.done()
                if cancel == "\x1b":
                    await view.press("\x10")
            assert view.fired == ["y"]
            # A paste is not an answer: it reaches its handler, the leader waits on.
            await view.press("\x10\x1b[200~text\x1b[201~")
            assert view.pasted == ["text"] and view.shortcuts.pending
            await view.press("y\r")
            assert await asyncio.wait_for(task, 2) == "a"
            assert view.fired == ["y", "y"]

    asyncio.run(run())


def test_a_leader_of_several_keys():
    async def run():
        with create_pipe_input() as pipe:
            view, task = await surface("ctrl+x ctrl+p", pipe)
            await view.press("\x18\x10y")
            assert view.fired == ["y"]
            await view.press("\x10y\r")
            assert await asyncio.wait_for(task, 2) == "y"
            assert view.fired == ["y"]

    asyncio.run(run())


def test_the_popup_hint_lists_the_shortcuts_while_the_leader_waits():
    async def run():
        stream = StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=20, columns=60), enable_cpr=False)
        with create_pipe_input() as pipe:
            view, task = await surface("ctrl+p", pipe, output)

            def screen() -> str:
                stream.seek(0)
                stream.truncate()
                view.app.renderer.reset()
                with set_app(view.app):
                    view.app.renderer.render(view.app, view.app.layout)
                return stream.getvalue()

            assert "Copy" not in screen()
            await view.press("\x10")
            shown = screen()
            assert "Keybindings" in shown and "Copy" in shown and "cancel" in shown
            # Unavailable shortcuts are left out.
            assert "Stop" not in shown
            await view.press("\x1b")
            await asyncio.sleep(0.6)
            assert "Copy" not in screen()
            view.app.exit()
            await task

    asyncio.run(run())


def prompt_session(prefix, app, pipe, output):
    return create_prompt(
        CommandRegistry(),
        activity=app.activity,
        transcript=app.transcript,
        on_submit=lambda text: None,
        on_send_mode=app.cycle_send_mode,
        on_effort=app.adjust_effort,
        key_prefix=prefix,
        input=pipe,
        output=output,
    )


def test_the_prompt_takes_its_shortcuts_after_a_leader():
    async def run():
        runtime = SimpleNamespace(agent=SimpleNamespace(model=None, model_settings={}))
        app = PreviewApp(
            model="openai-codex:test", runtime=runtime, console=Console(file=StringIO())
        )
        with create_pipe_input() as pipe:
            session = prompt_session("ctrl+p", app, pipe, DummyOutput())
            app.prompt_session = session
            task = asyncio.create_task(session.prompt_async())
            try:
                async with asyncio.timeout(5):
                    while not session.app.is_running:
                        await asyncio.sleep(0.01)
                assert app.next_send_mode == "steering"
                # Ctrl+S alone is no longer the send-mode key.
                pipe.send_text("draft\x13")
                await asyncio.sleep(0.05)
                assert app.next_send_mode == "steering"
                pipe.send_text("\x10s")
                await asyncio.sleep(0.05)
                assert app.next_send_mode == "queue"
                pipe.send_text("\x10n")
                await asyncio.sleep(0.05)
                assert app.current_effort() == "high"
                assert session.default_buffer.text == "draft"
                assert app.shortcut("s") == "Ctrl+P s"
            finally:
                session.app.exit(result="")
                await task

    asyncio.run(run())


def test_the_prompt_renders_descriptive_actions_in_shared_help():
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = prompt_session("ctrl+p", app, pipe, output)
            app.transcript.output = type(
                "Stub",
                (),
                {
                    "app": session.app,
                    "print": lambda *a, **k: None,
                    "typing_fragments": lambda self: [],
                },
            )()
            session.shortcuts.pending = True
            stream.seek(0)
            stream.truncate()
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            assert screen is not None
            return "\n".join(
                "".join(row[x].char for x in range(80))
                for _, row in sorted(screen.data_buffer.items())
            )

    screen = asyncio.run(run())
    assert "Keybindings" in screen
    assert "Cycle send mode" in screen
    assert "n / p  Thinking effort up / down" in screen
    assert "Select thinking visibility" in screen
    assert "Copy draft / choose response" in screen
    assert "Esc cancel · Ctrl+/ all keys" in screen


def test_help_and_flashes_name_the_prompts_own_keys():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=120, color_system=None))
    app.handle("/help")
    text = stream.getvalue()
    assert "Ctrl+O tasks widget" in text and "Ctrl+^ (Ctrl+6) back" in text
    app.prompt_session = type("Session", (), {"shortcuts": PrefixKeys("ctrl+space")})()
    stream.seek(0)
    stream.truncate()
    app.handle("/help")
    text = stream.getvalue()
    assert "Ctrl+Space o tasks widget" in text
    assert "Ctrl+Space ^ back to the previous session" in text
    assert "Ctrl+Space s picks steering" in text
