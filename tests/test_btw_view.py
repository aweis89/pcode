"""The /btw thread view: a thread in the drawer, and where Enter sends."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.btw_view import (
    ThreadView,
    ThreadWriter,
    close,
    current,
    next_thread,
    open_thread,
    position,
    reopen_thread,
)
from pcode.ui import Activity, CursorSafeOutput, Handoff, TerminalOutput, Transcript

HOST = "0123abcd"


# Kept before the autouse stand-in replaces it, for the test of the real one.
REAL_DRAWER_HOST = PreviewApp.drawer_host


@pytest.fixture(autouse=True)
def drawer(monkeypatch):
    """A drawer to show threads in (`PreviewApp.drawer_host`): thread mode."""
    monkeypatch.setattr(PreviewApp, "drawer_host", lambda self: HOST)


def no_drawer(monkeypatch) -> None:
    monkeypatch.setattr(PreviewApp, "drawer_host", lambda self: None)


def answered(question: str, answer: str, thread: str = "") -> Aside:
    aside = Aside(question=question, answer=answer, activity="", thread=thread)
    aside.settle("answered")
    aside.reply = SideReply(answer)
    return aside


def seeded_app(*, terminal: bool = True) -> tuple[PreviewApp, list[tuple[str, str]]]:
    console = Console(
        file=StringIO(), width=120, color_system=None, force_terminal=terminal or None
    )
    app = PreviewApp(console=console)
    first = answered("first?", "One.")
    second = answered("second?", "Two.")
    app.controller.asides.items.extend([first, second])
    asked: list[tuple[str, str]] = []

    def follow_up(thread, question):
        app.controller.asides.follows(thread)
        asked.append((thread, question))

    app.controller.follow_up_aside = follow_up
    return app, asked


class Screen:
    """A transcript writing to a terminal stand-in, read one flush at a time."""

    def __init__(self, monkeypatch):
        activity = Activity()
        console = Console(file=StringIO(), width=80, color_system=None, force_terminal=True)
        self.transcript = Transcript(console, activity=activity)
        terminal = DummyOutput()
        terminal.get_size = lambda: Size(rows=40, columns=80)
        self.output = TerminalOutput(
            console, SimpleNamespace(output=CursorSafeOutput(terminal), invalidate=lambda: None)
        )
        self.transcript.output = self.output
        self.activity = activity
        self._read = 0

        async def handoff(app, **kwargs):
            yield Handoff(11)

        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))

    def flush(self) -> str:
        """What the next frames write, until the queue drains."""

        async def drain():
            await self.output.flush()
            while self.output.rows:
                await self.output.flush()

        asyncio.run(drain())
        text = self.transcript.console.file.getvalue()
        new, self._read = text[self._read :], len(text)
        return new


def test_an_error_and_an_empty_answer_say_so():
    failed = Aside(question="why?", activity="")
    failed.settle("failed", error="no network")
    empty = Aside(question="how?", activity="", thread=failed.thread)
    empty.settle("cancelled")
    entries, _ = ThreadWriter().entries([failed, empty], "monokai")
    text = " ".join(str(getattr(obj, "plain", obj)) for objects, _, _ in entries for obj in objects)
    assert "failed: no network" in text and "(cancelled; no answer)" in text


def test_threads_cycle_newest_first_and_wrap():
    activity = Activity()
    assert next_thread(activity) is None and reopen_thread(activity) is None
    first, second = answered("first?", "One."), answered("second?", "Two.")
    activity.asides = [first, second]
    # From the conversation: no next thread, but Ctrl+Q reopens the newest.
    assert next_thread(activity) is None
    assert reopen_thread(activity) == second.thread
    open_thread(activity, second.thread)
    assert current(activity) == [second] and position(activity) == (1, 2)
    assert next_thread(activity) == first.thread
    open_thread(activity, first.thread)
    # The oldest thread wraps to the newest.
    assert position(activity) == (2, 2) and next_thread(activity) == second.thread
    close(activity)
    assert current(activity) == [] and position(activity) == (0, 2)
    # Ctrl+Q brings back the thread last shown, not the newest.
    assert reopen_thread(activity) == first.thread
    # A thread dropped from the records shows the conversation in effect.
    open_thread(activity, "gone")
    assert current(activity) == [] and next_thread(activity) is None
    assert reopen_thread(activity) == second.thread


def test_bare_btw_shows_a_lone_thread_at_once():
    app, _ = seeded_app()
    del app.controller.asides.items[0]
    newest = app.controller.asides.items[-1]
    assert asyncio.run(app.read_asides()) is None
    assert app.activity.btw_thread == newest.thread


def stub_popup(monkeypatch, app) -> None:
    class Popup:
        async def __aenter__(self):
            return SimpleNamespace(input=None, output=None)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app, "popup", lambda output, session: Popup())
    app.prompt_session = SimpleNamespace(
        app=SimpleNamespace(output=None, style=None),
        default_buffer=SimpleNamespace(text=""),
    )


@pytest.mark.parametrize("choose", [True, False])
def test_bare_btw_picks_among_several_threads(monkeypatch, choose):
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    opened = []

    class Browser:
        def __init__(self, asides, **kwargs):
            opened.append((kwargs.get("selected"), kwargs.get("pick")))
            self.picked = None

        async def run(self):
            self.picked = first.thread if choose else None

    monkeypatch.setattr("pcode.aside_ui.AsideBrowser", Browser)
    stub_popup(monkeypatch, app)
    assert asyncio.run(app.read_asides()) is None
    assert opened == [(second.id, True)]
    assert app.activity.btw_thread == (first.thread if choose else None)
    assert not app.aside_view_open


@pytest.mark.parametrize(("keys", "picks"), [("\x1b[A\r", 0), ("\x11", None)])
def test_the_picker_starts_on_the_list_and_enter_picks_the_selection(keys, picks):
    from prompt_toolkit.input import create_pipe_input

    from pcode.aside_ui import AsideBrowser

    app, _ = seeded_app()
    items = app.controller.asides.items
    with create_pipe_input() as pipe:
        browser = AsideBrowser(
            app.controller.asides,
            selected=items[-1].id,
            pick=True,
            key_prefix="ctrl",
            input=pipe,
            output=DummyOutput(),
        )
        assert browser.listing()
        pipe.send_text(keys)  # Up to the older thread and Enter, or Ctrl+Q to close.
        assert asyncio.run(browser.run()) is None
    assert browser.picked == (None if picks is None else items[picks].thread)


def test_an_answer_that_opens_by_itself_never_takes_a_thread_or_a_draft():
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    app._command_popup_generation = app._auto_open_generation = 3
    open_thread(app.activity, first.thread)
    assert not app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread == first.thread
    # Mid-sentence to the conversation, opening a thread would send it there.
    close(app.activity)
    app.prompt_session = SimpleNamespace(default_buffer=SimpleNamespace(text="half a messa"))
    assert not app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread is None
    app.prompt_session.default_buffer.text = ""
    assert app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread == second.thread


def test_ctrl_q_toggles_and_ctrl_backslash_moves_between_threads():
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    app.btw_key()
    assert app.activity.btw_thread == second.thread
    app.btw_key()
    assert app.activity.btw_thread is None  # Straight back to the conversation.
    app.btw_key()
    assert app.activity.btw_thread == second.thread
    app.btw_next()
    assert app.activity.btw_thread == first.thread
    app.btw_next()
    assert app.activity.btw_thread == second.thread  # Wrapped to the newest.
    app.btw_next()
    app.btw_key()
    assert app.activity.btw_thread is None
    app.btw_key()
    assert app.activity.btw_thread == first.thread  # The thread last shown.
    app.close_btw()
    assert app.activity.btw_thread is None
    app.controller.asides.items.clear()
    app.btw_key()
    assert app.activity.btw_thread is None


def test_the_editor_replies_to_the_open_thread():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    # With the conversation shown, the editor is the conversation's.
    assert app.btw_submit("hello") is None
    open_thread(app.activity, second.thread)
    assert app.btw_submit("and then?") is False
    assert asked == [(second.thread, "and then?")]
    # A refused reply keeps the draft.
    second.status = "running"
    assert app.btw_submit("too soon") is True
    second.status = "answered"
    # /copy and /links read the thread, and /btw asks beside it: it stays.
    for command in ("/copy", "/links", "/btw why?"):
        assert app.btw_submit(command) is None
        assert app.activity.btw_thread == second.thread
    # The conversation's own commands close it, as their output goes there.
    assert app.btw_submit("/help") is None
    assert app.activity.btw_thread is None
    open_thread(app.activity, second.thread)
    assert app.btw_submit("/close") is False
    assert app.activity.btw_thread is None


def test_merge_from_the_thread_keeps_it_and_goes_back():
    app, _ = seeded_app()
    second = app.controller.asides.items[-1]
    kept = []

    async def keep_thread(thread, action, instructions=""):
        kept.append((thread, action, instructions))

    app.controller.keep_thread = keep_thread

    async def run():
        open_thread(app.activity, second.thread)
        app.activity.busy = True
        assert app.btw_submit("/merge") is True  # Refused mid-turn, draft kept.
        app.activity.busy = False
        # A preview has no tree to keep it in; pretend it does.
        app.controller.check_bridge = lambda thread, action="merge": second
        assert app.btw_submit("/summarize keep decisions") is False
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert kept == [(second.thread, "summary", "keep decisions")]
        assert app.activity.btw_thread is None

    asyncio.run(run())


def test_the_threads_own_commands_complete_only_while_it_is_shown():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input

    from pcode.commands import Command, CommandRegistry
    from pcode.ui import create_prompt

    app, _ = seeded_app()
    second = app.controller.asides.items[-1]
    registry = CommandRegistry()
    registry.register(Command("/stop", "Stop the running turn", lambda _: None))
    registry.register(Command("/model", "Choose a model", lambda _: None))

    def names(text):
        found = session.completer.get_completions(Document(text), CompleteEvent())
        return {c.text: c.display_meta_text for c in found}

    with create_pipe_input() as pipe:
        session = create_prompt(
            registry,
            activity=app.activity,
            on_btw_close=lambda: None,
            input=pipe,
            output=DummyOutput(),
        )
        assert set(names("/")) == {"/stop", "/model"}
        open_thread(app.activity, second.thread)
        shown = names("/")
        assert {"/merge", "/summarize", "/close", "/model"} <= set(shown)
        # The thread's /stop stops side answers, so its own description wins.
        assert shown["/stop"] == "Stop running side answers"
        assert set(names("/me")) == {"/merge"}
        close(app.activity)
        assert set(names("/")) == {"/stop", "/model"}
        # Without the thread view, a thread on `Activity` takes no commands.
        open_thread(app.activity, second.thread)
        session = create_prompt(registry, activity=app.activity, input=pipe, output=DummyOutput())
        assert set(names("/")) == {"/stop", "/model"}


def test_copy_reads_the_open_thread(monkeypatch):
    from pcode import clipboard

    app, _ = seeded_app()
    copied = []
    monkeypatch.setattr(
        clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )
    first = app.controller.asides.items[0]
    open_thread(app.activity, first.thread)
    asyncio.run(app.choose_copy(output=None, session=None))
    assert copied == ["One."]


def test_paths_are_questions_and_shell_commands_go_back_to_the_conversation():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    open_thread(app.activity, second.thread)
    # A path is a question, not a command.
    assert app.btw_submit("/etc/hosts has what?") is False
    assert asked == [(second.thread, "/etc/hosts has what?")]
    # A `!command` still runs in the shell, its output in the conversation.
    assert app.btw_submit("!ls") is None
    assert app.activity.btw_thread is None


def test_the_shortcut_opens_the_viewer_without_a_drawer(monkeypatch):
    no_drawer(monkeypatch)
    app, _ = seeded_app()
    sent = []
    app._submit_typed = sent.append
    app.btw_key()
    assert sent == ["/btw"]
    assert app.activity.btw_thread is None


@pytest.mark.parametrize("terminal", [True, False])
def test_the_popup_shows_threads_without_a_drawer_and_off_a_terminal(monkeypatch, terminal):
    if terminal:
        no_drawer(monkeypatch)
    app, _ = seeded_app(terminal=terminal)
    opened = []

    class Browser:
        def __init__(self, *args, **kwargs):
            opened.append(kwargs.get("selected"))

        async def run(self):
            return None

    monkeypatch.setattr("pcode.aside_ui.AsideBrowser", Browser)

    class Popup:
        async def __aenter__(self):
            return SimpleNamespace(input=None, output=None)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app, "popup", lambda output, session: Popup())
    app.prompt_session = type(
        "Session", (), {"app": type("App", (), {"output": None, "style": None})()}
    )()
    assert asyncio.run(app.read_asides()) is None
    assert opened == [app.controller.asides.items[-1].id]
    assert app.activity.btw_thread is None


def test_keep_thread_is_a_host_query():
    from pcode.controller import QUERIES

    assert "keep_thread" in QUERIES


def test_a_drawer_shows_the_thread_beside_the_conversation(monkeypatch):
    """An APC names the thread; the screen keeps the conversation."""
    screen = Screen(monkeypatch)
    transcript, activity = screen.transcript, screen.activity
    transcript.message("CONVERSATION")
    screen.flush()
    writes: list[str] = []
    screen.output.app.output.write_raw = writes.append
    aside = answered("why?", "Because.")
    aside.read = False
    activity.asides = [aside]
    view = ThreadView(activity, transcript)
    view.drawer_host = lambda: "a1234567"
    seen = []
    view.on_read = seen.append
    view.sync()
    assert writes == [], "no thread, no drawer: nothing to say"
    assert not view.drawer_open
    open_thread(activity, aside.thread)
    view.sync()
    view.sync()
    assert writes == [f"\x1b_pcode;btw;a1234567;{aside.thread}\x1b\\"]
    assert view.drawer_open and "why?" not in screen.flush()
    assert seen == [[aside.id]]
    close(activity)
    view.sync()
    assert writes[-1] == "\x1b_pcode;close\x1b\\" and not view.drawer_open


def test_a_thread_closes_when_the_drawer_goes(monkeypatch):
    """No drawer any more (another session took it): the editor is the conversation's again."""
    screen = Screen(monkeypatch)
    transcript, activity = screen.transcript, screen.activity
    writes: list[str] = []
    screen.output.app.output.write_raw = writes.append
    aside = answered("why?", "Because.")
    activity.asides = [aside]
    view = ThreadView(activity, transcript)
    view.drawer_host = lambda: "a1234567"
    open_thread(activity, aside.thread)
    view.sync()
    assert view.drawer_open
    view.drawer_host = lambda: None
    view.sync()
    assert writes[-1] == "\x1b_pcode;close\x1b\\"
    assert current(activity) == [] and not view.drawer_open
    assert "Because." not in screen.flush(), "never drawn in the conversation's place"
    # A thread opened with no drawer at all closes without a word to the terminal.
    writes.clear()
    open_thread(activity, aside.thread)
    view.sync()
    assert writes == [] and current(activity) == []
    # Ctrl+Q brings back the thread it closed, once there is a drawer again.
    assert reopen_thread(activity) == aside.thread


def test_the_drawer_is_there_only_with_overlay_popups_and_a_host(monkeypatch):
    """`drawer_host`: an answered probe, `popup_view` not `popup`, and a reachable host."""
    from pcode import drawer, theme
    from pcode.preferences import save_preferences

    # The real `drawer_host`, not the autouse stand-in. Not `monkeypatch.undo()`:
    # that would also undo conftest's isolation and save to the user's own preferences.
    monkeypatch.setattr(PreviewApp, "drawer_host", REAL_DRAWER_HOST)
    app, _ = seeded_app()
    app.controller.runtime = SimpleNamespace(remote=True, id=HOST)
    supported = True
    monkeypatch.setattr(theme, "drawer_supported", lambda: supported)
    monkeypatch.setattr(drawer, "reachable", lambda directory: True)
    monkeypatch.setattr(drawer, "valid_host", lambda identity: True)
    # The default, `auto`, opens the drawer wherever the terminal answered the probe.
    assert app.drawer_host() == HOST and app.btw_drawer()
    supported = False
    assert app.drawer_host() is None
    supported = True
    for view, expected in (("overlay", HOST), ("popup", None)):
        save_preferences(popup_view=view)
        app._drawer_pref_at = float("-inf")  # The setting is read again at most once a second.
        assert app.drawer_host() == expected
    assert not app.btw_drawer()
    # Ctrl+Q is then a bare /btw: the viewer.
    sent = []
    app._submit_typed = sent.append
    app.btw_key()
    assert sent == ["/btw"]
