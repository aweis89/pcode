"""Editor-only mode (`PCODE_EDITOR_ONLY=1`, `pcode.dock.EditorOnly`).

The editor runs alone on pcode's own terminal: the transcript's writes go
nowhere, while the live panel built from the same events keeps updating.
"""

import asyncio
import os
import select
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from rich.console import Console

from pcode import dock
from pcode.app import PreviewApp
from pcode.runtime import PlanUpdated, TextDelta, ToolStarted, ToolSummary
from pcode.ui import TerminalOutput


def drain(fd: int) -> bytes:
    """Whatever reached the terminal so far."""
    data = b""
    while select.select([fd], [], [], 0.05)[0]:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data += chunk
    return data


class Pane:
    """`sys.stdout`, recording writes that bypass the editor's output."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return self.fd

    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty(monkeypatch):
    """A pty standing in for pcode's own terminal, and `sys.stdout` on it."""
    master, slave = os.openpty()
    pane = Pane(slave)
    monkeypatch.setattr(dock, "sys", SimpleNamespace(stdout=pane, stdin=pane))
    monkeypatch.setattr(dock, "_active", None)
    yield SimpleNamespace(master=master, slave=slave, pane=pane)
    dock._active = None
    os.close(master)
    os.close(slave)


def test_flag_needs_both_ends_on_a_terminal(monkeypatch, tty):
    monkeypatch.delenv(dock.EDITOR_ONLY_ENV, raising=False)
    assert not dock.editor_only_requested()
    monkeypatch.setenv(dock.EDITOR_ONLY_ENV, "1")
    assert dock.editor_only_requested()
    tty.pane.isatty = lambda: False
    assert not dock.editor_only_requested()


def test_only_a_host_client_goes_editor_only(monkeypatch):
    """In-process, the transcript is all there is to read: the flag is ignored."""
    monkeypatch.setattr(dock, "editor_only_requested", lambda: True)
    local = PreviewApp(console=Console(force_terminal=True))
    assert not local.editor_only
    assert not isinstance(local.transcript.console.file, dock.NullSink)
    hosted = PreviewApp(console=Console(force_terminal=True), host=object())
    assert hosted.editor_only
    assert isinstance(hosted.transcript.console.file, dock.NullSink)
    # Still a terminal to everything that asks (popups, drawer, colors).
    assert hosted.transcript.console.is_terminal


def test_editor_only_leaves_the_pane_alone(tty):
    """No cursor reports, scroll regions, glide or hidden cursor: there is no pane."""
    editor = dock.open_editor_only()
    assert dock.active() is editor and editor.owns_tty and editor.input is None
    app = SimpleNamespace(layout=SimpleNamespace(container=SimpleNamespace(drawn=(0, 0, 7))))
    app.output = editor.output
    app.invalidate = lambda: None
    editor.attach(app)
    rows = os.get_terminal_size(tty.slave).lines
    assert editor.pane_rows() == rows and editor.reserve == 0
    editor.ask_cursor()
    editor.wrote(3)
    assert editor.homed() == ""
    assert editor.layout_size(Size(rows=12, columns=40)) == Size(rows=12, columns=40)
    editor.tint = "#202020"
    editor.report_rows(app)
    assert tty.pane.text == ""
    # What the editor's terminal needs still reaches it, on the editor's output.
    sent = drain(tty.master)
    assert b"\x1b_pcode;ready\x1b\\" in sent
    assert b"\x1b]11;#202020\x1b\\" in sent
    assert b"\x1b_pcode;dock-rows;7\x1b\\" in sent
    assert b"\x1b[6n" not in sent and b"\x1b[1;" not in sent
    editor.close()
    assert dock.active() is None
    # The tint goes with it.
    assert drain(tty.master) == b"\x1b]111\x1b\\"


def test_transcript_goes_nowhere_while_the_live_panel_updates(monkeypatch, tty):
    monkeypatch.setattr(dock, "editor_only_requested", lambda: True)
    console = Console(force_terminal=True, width=80)
    app = PreviewApp(console=console, host=object())
    written: list[str] = []
    sink = app.transcript.console.file
    sink.write = lambda text: written.append(text) or len(text)
    editor = dock.open_editor_only()
    output = TerminalOutput(app.transcript.console, SimpleNamespace(invalidate=lambda: None))
    app.transcript.output = output
    app.output = output
    assert output.discard and not output.paced and not output.docked_glide
    assert output.resize_replay is None and editor.glide is None

    async def run() -> None:
        app.turn_started("fix the parser", echo=True)
        app.turn_event(PlanUpdated([{"content": "read parser", "status": "in_progress"}]))
        app.turn_event(ToolStarted(name="shell", detail="pytest", call_id="c1", command="pytest"))
        assert [call.event.call_id for call in app.activity.tools.calls] == ["c1"]
        assert app.activity.tools.running
        app.turn_event(TextDelta("Looking at the parser.\n\n"))
        app.turn_event(ToolSummary(name="shell", detail="pytest", call_id="c1", command="pytest"))
        await output.flush()
        # A replay (attach, a resize, after a full-screen popup) is dropped too.
        app.transcript.regenerate()
        await output.flush(drain=True)

    asyncio.run(run())
    # Tasks and tool rows came from the same events the transcript ignored.
    assert app.activity.plan == [{"content": "read parser", "status": "in_progress"}]
    assert not app.activity.tools.running
    assert written == []
    assert not output.pending and not output.rows and output._regenerate is None
    assert tty.pane.text == ""
    assert drain(tty.master) == b""
    # Direct console prints (before the editor runs, the exit hint) vanish too.
    app.transcript.output = None
    app.transcript.note("not shown")
    assert written and drain(tty.master) == b""


def test_tool_rows_still_show_in_the_editor(monkeypatch, tty):
    """Outside the app's native transcript, the editor keeps its own tool rows."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode import ui_surface
    from pcode.ui import Activity, PromptLayout, Transcript

    activity = Activity(prompt="run tests", prompt_state="running")
    activity.tools.record(
        ToolStarted(call_id="c1", name="shell", detail="pytest", command="pytest")
    )
    transcript = Transcript(Console(force_terminal=True, width=100))
    dock.open_editor_only()
    with create_pipe_input() as pipe:
        layout = PromptLayout(
            PromptSession(input=pipe, output=DummyOutput()), activity, None, transcript
        )
    monkeypatch.setattr(layout, "size", lambda: Size(rows=40, columns=100))
    with ui_surface.override_ui_surface(ui_surface.UISurface.TERMINAL):
        assert len(layout.tool_rows()) == 1


def test_editor_only_tint_does_not_feed_back_into_appearance(monkeypatch, tty):
    """Echoing the dock's OSC 11 tint back on the shared tty never compounds frame_shade."""
    from pcode import appearance, theme, ui
    from pcode.ui import Transcript

    monkeypatch.setattr(theme, "_background", ("#1e1e2e", "dark"))
    monkeypatch.setattr(ui, "true_color", lambda: True)
    editor = dock.open_editor_only()
    transcript = Transcript(Console(force_terminal=True))
    first = transcript.frame_shades()
    assert first is not None and editor.tint == first[0]
    editor._sent_tint = editor.tint
    # An OSC 11 reply echoing the dock's own tint is ignored rather than
    # recorded as the base background.
    r, g, b = editor.tint[1:3], editor.tint[3:5], editor.tint[5:7]
    assert theme.background_theme(f"\x1b]11;rgb:{r}/{g}/{b}\x1b\\".encode()) is None
    assert theme.terminal_background() == ("#1e1e2e", "dark")
    assert transcript.frame_shades() == first

    # When AppearanceWatch asks the terminal again, it resets OSC 11 first.
    watch = appearance.AppearanceWatch(tty.slave)
    watch.enabled = True
    watch.refresh()
    assert editor._sent_tint is None
    assert drain(tty.master) == b"\x1b]111\x1b\\" + appearance.QUERY_BACKGROUND


def test_app_pane_native_layers_need_editor_only_on_a_terminal(monkeypatch, tty):
    """Only the editor-only terminal claims the app's native layers; the native-transcript
    flag alone (inherited by a child process, say) does not, nor does a pipe."""
    from pcode import theme

    monkeypatch.setattr(theme, "_drawer_protocol", None)
    monkeypatch.setattr(theme, "_popup_protocol", None)
    monkeypatch.delenv(dock.EDITOR_ONLY_ENV, raising=False)
    monkeypatch.setenv("PCODE_NATIVE_TRANSCRIPT", "1")
    assert not theme.drawer_supported() and not theme.native_toast_supported()
    monkeypatch.setenv(dock.EDITOR_ONLY_ENV, "1")
    assert theme.drawer_supported() and theme.native_completion_supported()
    tty.pane.isatty = lambda: False  # Both ends must be a terminal.
    assert not theme.popup_supported()


class Keyboard:
    """`sys.stdin` with a line ready, and `sys.stderr`, recording what was shown."""

    def __init__(self) -> None:
        self.shown = ""
        self.read = 0

    def write(self, text: str) -> int:
        self.shown += text
        return len(text)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        self.read += 1
        return "\n"


@pytest.fixture
def keyboard(monkeypatch, tty):
    keys = Keyboard()
    monkeypatch.setattr(dock, "sys", SimpleNamespace(stdout=tty.pane, stdin=tty.pane, stderr=keys))
    tty.pane.readline = keys.readline
    monkeypatch.setattr(dock, "_editor_started", False)
    monkeypatch.setenv(dock.EDITOR_ONLY_ENV, "1")
    return keys


def test_a_failed_start_waits_for_enter_in_an_app_pane(keyboard):
    """The app closes the pane when pcode exits: an early error stays until Enter."""
    dock.hold_failed_start("Traceback: boom\n")
    assert keyboard.shown.startswith("Traceback: boom\n")
    assert "Press Enter to close" in keyboard.shown
    assert keyboard.read == 1


def test_no_hold_once_the_editor_started_or_outside_the_app(monkeypatch, keyboard):
    monkeypatch.setattr(dock, "_editor_started", True)
    dock.hold_failed_start()
    monkeypatch.setattr(dock, "_editor_started", False)
    monkeypatch.delenv(dock.EDITOR_ONLY_ENV)
    dock.hold_failed_start()
    assert keyboard.shown == "" and keyboard.read == 0


def test_opening_the_editor_marks_it_started(monkeypatch, keyboard):
    dock.open_editor_only()
    dock.hold_failed_start()
    assert keyboard.read == 0


@pytest.mark.parametrize(
    ("code", "held"), [(2, True), ("bad flag", True), (0, False), (None, False)]
)
def test_cli_holds_only_a_failed_exit(monkeypatch, code, held):
    import sys as real_sys

    from pcode import cli

    calls = []
    monkeypatch.setattr(cli, "_hold_failed_start", lambda detail="": calls.append(detail))
    monkeypatch.setattr(real_sys, "argv", ["pcode"])
    monkeypatch.setattr(cli, "_quiet_stdin", lambda stack: None)
    monkeypatch.setattr("pcode.proctitle.rename", lambda: None)

    def fail():
        raise SystemExit(code)

    monkeypatch.setattr("pcode.app.main", fail)
    with pytest.raises(SystemExit):
        cli.main()
    assert bool(calls) == held


def test_cli_holds_a_crash_with_its_traceback(monkeypatch):
    import sys as real_sys

    from pcode import cli

    calls = []
    monkeypatch.setattr(cli, "_hold_failed_start", lambda detail="": calls.append(detail))
    monkeypatch.setattr(real_sys, "argv", ["pcode"])
    monkeypatch.setattr(cli, "_quiet_stdin", lambda stack: None)
    monkeypatch.setattr("pcode.proctitle.rename", lambda: None)
    monkeypatch.setattr("pcode.app.main", lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cli.main()
    assert "ZeroDivisionError" in calls[0]


def test_waiting_on_the_user_before_the_editor_tells_the_app(monkeypatch, keyboard, tty):
    """The trust prompt and a held failure drop the app's boot cover (`pcode;startup-input`)."""
    dock.hold_failed_start()
    assert "\x1b_pcode;startup-input\x1b\\" in tty.pane.text
    tty.pane.text = ""
    monkeypatch.delenv(dock.EDITOR_ONLY_ENV)
    dock.show_startup_input()
    assert tty.pane.text == ""
