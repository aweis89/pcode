"""Exercise editable input and serialized submissions with the real prompt loop."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, TextDelta
from pcode.ui import create_prompt


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
def test_edit_and_queue_during_generation(outcome):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        followup = asyncio.Event()
        calls = []
        active = 0
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                nonlocal active
                active += 1
                assert active == 1
                calls.append(text)
                try:
                    if len(calls) == 1:
                        yield TextDelta("**first**\npartial")
                        started.set()
                        await finish.wait()
                        if outcome == "failure":
                            raise RuntimeError("private provider body")
                        yield TextDelta(" done")
                        yield Message("**first**\npartial done")
                    else:
                        yield TextDelta("second answer")
                        yield Message("second answer")
                        followup.set()
                finally:
                    active -= 1

        app = PreviewApp(
            model="test:local", runtime=Runtime(), console=Console(file=output, color_system=None)
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\rdraft text\x1b[D\x1b[D\x1b[D\x1b[D")
                    await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert session.default_buffer.cursor_position == 6
                    assert app.activity.queued == 1
                    assert app.activity.queued_prompts == ["second"]
                    assert calls == ["first"]
                    assert app.activity.prompt == "first"
                    assert app.activity.prompt_state == "running"
                    if outcome == "cancel":
                        pipe.send_text("\x03")
                    else:
                        finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    assert app.activity.queued_prompts == []
                    if outcome == "success":
                        assert followup.is_set()
                        assert calls == ["first", "second"]
                    else:
                        assert calls == ["first"]
                        assert app.activity.queued == 0
                    assert (
                        app.activity.prompt_state
                        == {"success": "done", "failure": "failed", "cancel": "cancelled"}[outcome]
                    )
                    assert app.activity.prompt == ("second" if outcome == "success" else "first")
                    assert session.default_buffer.text == "draft text"
                    assert session.default_buffer.cursor_position == 6
                    pipe.send_text("my ")
                    await wait_for(lambda: session.default_buffer.text == "draft my text")
                    pipe.send_text("\x03\x04")  # Idle: discard draft, then exit.
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        printed = output.getvalue()
        assert "**first**" not in printed
        assert printed.count("first partial") == 1  # Rendered once, including cancellation.
        assert printed.count("partial") == 1
        assert "private provider body" not in printed
        if outcome == "cancel":
            assert "! Run cancelled" in printed
        elif outcome == "failure":
            assert "✗ Agent failed" in printed
        else:
            assert printed.count("second answer") == 1

    asyncio.run(run())


@pytest.mark.parametrize("state", ["running", "failed", "cancelled", "done", ""])
def test_status_row_shows_the_spinner_and_never_echoes_the_prompt(state):
    from pcode.ui import Activity

    activity = Activity(prompt="first\nsecond\x1b", prompt_state=state, status="Waiting for model…")
    assert activity.status_shown is (state == "running")
    fragments = activity.status_fragments("⠋", 80)
    assert fragments[0] == ("class:activity.prompt", "⠋ ")
    assert fragments[1][1] == "Waiting for model…"


def test_status_row_reports_the_newest_running_tool_call():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Responding…")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    activity.tools.record(ToolStarted("grep", "pattern", "two"))
    style, text = activity.status_fragments("⠋", 80)[1]
    assert style == "class:plan.active"
    assert text.endswith("pattern")
    # A result hands the row back to the call still running.
    activity.tools.record(ToolSummary("grep", "pattern", call_id="two"))
    assert activity.status_fragments("⠋", 80)[1][1].endswith("example.py")


@pytest.mark.parametrize("width", [0, 1, 2, 3, 12, 40, 100])
def test_status_row_truncates_to_terminal_width(width):
    from rich.cells import cell_len

    from pcode.runtime import ToolStarted
    from pcode.ui import Activity

    activity = Activity(prompt_state="running")
    activity.tools.record(ToolStarted("read_file", "界面/path\n" * 30, "one"))
    rendered = "".join(text for _, text in activity.status_fragments("⠋", width))
    assert "\n" not in rendered
    assert cell_len(rendered) <= width
    if width > 2:
        assert rendered.endswith("…")


def test_system_prompt_row_is_badged_and_not_an_echoed_command():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system", detail="keep {tests}")
    fragments = activity.status_fragments("⠋", 80)
    assert fragments == [
        ("class:activity.system", "⠋ ◈ "),
        ("class:activity.system.label", "Compacting context"),
        ("class:activity.system.detail", " ▸ keep {tests}"),
    ]
    rendered = "".join(text for _, text in fragments)
    assert "/compact" not in rendered and "❯" not in rendered


def test_system_prompt_row_drops_empty_detail():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system")
    assert activity.status_fragments("⠋", 80) == [
        ("class:activity.system", "⠋ ◈ "),
        ("class:activity.system.label", "Compacting context"),
    ]


@pytest.mark.parametrize("width", [0, 1, 2, 3, 5, 12, 40, 100])
def test_system_prompt_row_truncates_to_terminal_width(width):
    from rich.cells import cell_len

    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting 界面 context\n" * 9, kind="system", detail="keep 界面\n" * 9)
    rendered = "".join(text for _, text in activity.status_fragments("⠋", width))
    assert "\n" not in rendered
    assert cell_len(rendered) <= width


def test_new_conversation_clears_the_system_prompt_kind():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system", detail="keep tests")
    activity.reset()
    activity.start_prompt("Fix bug")
    assert activity.status_fragments("⠋", 20)[0] == ("class:activity.prompt", "⠋ ")
    assert activity.prompt_detail == ""


def test_queue_previews_are_ordered_bounded_and_single_line():
    from pcode.tool_panel import panel_fragments
    from pcode.ui import Activity

    activity = Activity(queued_prompts=["first\ncontinued", "second", "third", "fourth", "fifth"])
    rows = activity.queue_rows(3)
    assert [text for _, text in rows] == [
        "Queued: first\ncontinued",
        "Queued: second",
        "… 3 more queued",
    ]
    rendered = "".join(text for _, text in panel_fragments(rows, 18))
    assert rendered.splitlines() == ["Queued: first con…", "Queued: second", "… 3 more queued"]
    assert len(activity.queue_rows(1)) == 1
    assert activity.queue_rows(0) == []
    assert Activity().queue_rows(3) == []


def test_queued_system_commands_are_badged_like_the_running_row():
    from pcode.ui import Activity

    activity = Activity(
        queued_prompts=["/compact keep tests", "/compact", "write /compact docs"],
        queued_modes=["queue", "steering", "queue"],
    )
    assert activity.queue_rows(3) == [
        ("class:activity.system.detail", "Queued ◈ Compacting context ▸ keep tests"),
        ("class:activity.system.detail", "Steering (next model request) ◈ Compacting context"),
        ("class:plan", "Queued: write /compact docs"),
    ]


def test_queued_and_running_system_rows_share_one_label():
    from pcode.ui import SYSTEM_COMMAND_LABELS, Activity

    activity = Activity(queued_prompts=["/compact keep tests"])
    queued = activity.queue_rows(1)[0][1]
    activity.start_prompt(SYSTEM_COMMAND_LABELS["/compact"], kind="system", detail="keep tests")
    running = "".join(text for _, text in activity.status_fragments("⠋", 80))
    assert queued.removeprefix("Queued ") == running.removeprefix("⠋ ")


@pytest.mark.parametrize("inspector_command", ["/tools", "/errors"])
def test_commands_run_while_model_waits(inspector_command):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        streamed = asyncio.Event()
        printed = StringIO()
        calls = []

        from pcode.inspection import ToolArchive
        from pcode.runtime import ToolStarted, ToolSummary

        archive = ToolArchive()
        archive.event(ToolStarted("read_file", "example.py", "call-1"))

        class Runtime:
            session = None
            recovery_blocked = ""
            inspections = archive

            async def stream(self, text):
                calls.append(text)
                started.set()
                await finish.wait()
                archive.event(ToolSummary("read_file", "done", call_id="call-1"))
                yield TextDelta("answer while inspecting")
                yield Message("answer while inspecting")
                streamed.set()

            def reset(self):
                raise AssertionError("must not reset an active run")

        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=printed))
        with create_pipe_input() as pipe:
            session = None
            inspector = None
            from pcode.inspector_ui import ToolInspector

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            def browser(*args, **kwargs):
                nonlocal inspector
                inspector = ToolInspector(*args, **kwargs)
                return inspector

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.inspector_ui.ToolInspector", browser),
                patch.object(app.transcript, "user", wraps=app.transcript.user) as user,
            ):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    user.reset_mock()
                    pipe.send_text(
                        "/theme light\r/help\r/new\r/resume\r/tree\r/nope\r/theme invalid\r"
                    )
                    await wait_for(lambda: "Usage: /theme" in printed.getvalue())
                    assert app.transcript.theme == "light"
                    assert "Unknown command" in printed.getvalue()
                    assert "/new is unavailable while working" in printed.getvalue()
                    assert "/resume is unavailable while working" in printed.getvalue()
                    assert "/tree is unavailable while working" in printed.getvalue()
                    user.assert_not_called()
                    assert app.activity.busy
                    assert app.activity.queued_prompts == []
                    assert calls == ["first"]
                    pipe.send_text(inspector_command + "\r")
                    await wait_for(lambda: inspector is not None and inspector.app.is_running)
                    assert inspector.failed == (inspector_command == "/errors")
                    assert inspector.archive is not archive
                    assert inspector.archive.calls[0].state == "running"
                    assert not finish.is_set()
                    finish.set()
                    await asyncio.wait_for(streamed.wait(), 5)
                    assert archive.calls[0].state == "succeeded"
                    assert inspector.archive.calls[0].state == "running"
                    # Streaming continues, but permanent output waits for the modal.
                    assert "answer while inspecting" not in printed.getvalue()
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not inspector.app.is_running)
                    await wait_for(lambda: "answer while inspecting" in printed.getvalue())
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/quit", "/exit"])
def test_quit_cancels_active_run(command):
    async def run():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                try:
                    started.set()
                    await asyncio.Event().wait()
                    yield Message("unreachable")
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                return create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\r" + command + "\r")
                    await asyncio.wait_for(task, 5)
                    assert cleaned.is_set()
                    assert not app.activity.queued_prompts
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
