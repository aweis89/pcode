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
            assert "Run cancelled" in printed
        elif outcome == "failure":
            assert "Run failed" in printed
        else:
            assert printed.count("second answer") == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "state,icon", [("running", "⠋"), ("failed", "!"), ("cancelled", "■"), ("done", "✓")]
)
def test_prompt_indicator(state, icon):
    from pcode.ui import Activity

    activity = Activity(prompt="first\nsecond\x1b", prompt_state=state)
    fragments = activity.prompt_fragments("⠋", 80)
    assert fragments[0][1] == icon + " "
    assert fragments[1][1].startswith("first second ")
    if state == "failed":
        assert "ansired" in fragments[0][0]
        assert fragments[1][1].endswith(" · failed")


@pytest.mark.parametrize("width", [0, 1, 2, 3, 12, 40, 100])
@pytest.mark.parametrize("state", ["running", "done", "failed", "cancelled"])
def test_prompt_indicator_truncates_to_terminal_width(width, state):
    from rich.cells import cell_len

    from pcode.ui import Activity

    prompt = "Work on 界面\n" * 30
    activity = Activity(prompt=prompt, prompt_state=state)
    fragments = activity.prompt_fragments("⠋", width)
    rendered = "".join(text for _, text in fragments)
    assert "\n" not in rendered
    assert cell_len(rendered) <= width
    if width > 2:
        assert rendered.endswith("…")
    assert activity.prompt == prompt


def test_prompt_indicator_keeps_short_prompt_intact():
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running")
    assert activity.prompt_fragments("⠋", 9) == [("class:prompt", "⠋ "), ("", "Fix bug")]


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
            ):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("/theme light\r/help\r/new\r/session\r/nope\r/theme invalid\r")
                    await wait_for(lambda: "Usage: /theme" in printed.getvalue())
                    assert app.transcript.theme == "light"
                    assert "Unknown command" in printed.getvalue()
                    assert "/new is unavailable while working" in printed.getvalue()
                    assert "/session is unavailable while working" in printed.getvalue()
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
