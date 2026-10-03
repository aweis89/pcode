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
                    # A host reports the queue back a moment after the send.
                    await wait_for(lambda: app.activity.queued == 1)
                    assert app.activity.queued_prompts == ["second"]
                    assert calls == ["first"]
                    assert app.activity.prompt == "first"
                    assert app.activity.prompt_state == "running"
                    if outcome == "cancel":
                        # The draft absorbs the first Ctrl+C; the run keeps going.
                        pipe.send_text("\x03")
                        await wait_for(lambda: not session.default_buffer.text)
                        assert app.activity.busy
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
                    if outcome == "cancel":
                        pipe.send_text("draft text\x1b[D\x1b[D\x1b[D\x1b[D")
                        await wait_for(lambda: session.default_buffer.text == "draft text")
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


def styled(fragments):
    """`{style: text}` for one status row, minus the unstyled padding."""
    return {style.removeprefix("class:"): text for style, text in fragments if style}


@pytest.mark.parametrize("state", ["running", "failed", "cancelled", "done", ""])
def test_status_row_shows_the_spinner_and_never_echoes_the_prompt(state):
    from pcode.ui import Activity

    activity = Activity(prompt="first\nsecond\x1b", prompt_state=state, status="Waiting for model…")
    assert activity.status_shown is (state == "running")
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.spinner"] == "⠋ "
    assert parts["activity.phase"] == "Waiting for model"
    assert "first" not in "".join(parts.values())


@pytest.mark.parametrize(
    ("status", "phase", "detail"),
    [
        ("Thinking…", "Thinking", ""),
        ("Responding…", "Responding", ""),
        ("Waiting for model…", "Waiting for model", ""),
        ("Retrying · Overloaded. Retrying 1/3…", "Retrying", "Overloaded. Retrying 1/3"),
        # A sentence with no phase is detail, never one long accented word.
        (
            "Enabling MCP 'x' — complete browser sign-in…",
            "Working",
            "Enabling MCP 'x' — complete browser sign-in",
        ),
        ("", "Working", ""),
    ],
)
def test_status_text_splits_into_phase_and_detail(status, phase, detail):
    from pcode.ui import status_parts

    assert status_parts(status) == (phase, detail)


def test_status_row_keeps_one_shape_with_meta_right_aligned():
    from rich.cells import cell_len

    from pcode.ui import Activity

    activity = Activity(prompt_state="running", status="Responding…")
    fragments = activity.status_fragments("⠋", 60, "✓7 ✗1 tools")
    rendered = "".join(text for _, text in fragments)
    assert cell_len(rendered) == 60
    assert rendered.startswith("⠋ Responding ")
    assert rendered.endswith("✓7 ✗1 tools · 0s")
    assert styled(fragments)["activity.meta"] == "✓7 ✗1 tools · 0s"


def test_status_row_reports_the_newest_running_tool_call():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Responding…")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    activity.tools.record(ToolStarted("grep", "pattern", "two"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Running 2 tools"
    assert parts["activity.detail"].endswith("pattern")
    # A result hands the row back to the call still running.
    activity.tools.record(ToolSummary("grep", "pattern", call_id="two"))
    parts = styled(activity.status_fragments("⠋", 80))
    # One call is its own phase: its label is already the verb.
    assert parts["activity.phase"] == "Read file"
    assert parts["activity.detail"] == " · example.py"


def test_finished_call_is_held_as_done_under_the_models_phase():
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Running read_file…")
    activity.tools.record(ToolStarted("read_file", "example.py", "one"))
    activity.tools.record(ToolSummary("read_file", "example.py", call_id="one"))
    parts = styled(activity.status_fragments("⠋", 80))
    # Nothing runs, so a stale `Running` status gives the model the turn,
    # and the held call is muted chrome with its result mark, not live work.
    assert parts["activity.phase"] == "Waiting for model"
    fragments = activity.status_fragments("⠋", 80)
    assert ("class:activity.meta", " · ✓ Read file · example.py") in fragments
    activity.tools.clear()
    parts = styled(activity.status_fragments("⠋", 80))
    assert "activity.detail" not in parts and parts["activity.meta"] == "0s"


def test_status_row_waits_on_sub_agents_listed_in_the_panel(monkeypatch):
    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.ui import Activity

    def row():
        return "".join(text for _, text in activity.status_fragments("⠋", 120))

    activity = Activity(prompt="Fix bug", prompt_state="running", status="Running delegate_task…")
    for call_id in ("a", "b"):
        activity.tools.record(
            ToolStarted("delegate_task", "", call_id, agent="worker", task=f"task {call_id}")
        )
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 2 sub-agents"
    assert "Worker" not in row()
    # A sub-agent's tool call is still reported on the row while it runs.
    activity.tools.record(ToolStarted("read_file", "x.py", "a:read", parent_call_id="a"))
    assert styled(activity.status_fragments("⠋", 80))["activity.phase"] == "Read file"
    # Held as done, it no longer reads as the model's turn.
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 1e9)
    activity.tools.record(ToolSummary("read_file", "x.py", call_id="a:read"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 2 sub-agents"
    assert "✓ Read file" in row()
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 0.0)
    activity.tools.record(ToolSummary("delegate_task", "done", call_id="b"))
    parts = styled(activity.status_fragments("⠋", 80))
    assert parts["activity.phase"] == "Waiting for 1 sub-agent"
    # With the panel hidden, the row is the only place left to name one.
    activity.show_tasks = False
    assert "Worker" in row() and "task a" in row()


def test_phase_clock_restarts_when_the_phase_changes(monkeypatch):
    from pcode import ui

    now = [100.0]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    activity = ui.Activity(prompt_state="running", status="Thinking…")

    def clock():
        return styled(activity.status_fragments("⠋", 80))["activity.meta"]

    assert clock() == "0s"
    now[0] += 0.5
    assert clock() == "0s"
    now[0] += 0.9
    assert clock() == "1s"
    activity.status = "Responding…"
    now[0] += 0.5
    assert clock() == "0s"
    # A gap in drawing means the row went away between turns.
    now[0] += 30
    assert clock() == "0s"


def test_status_row_holds_each_line_before_changing(monkeypatch):
    from pcode import ui
    from pcode.runtime import ToolStarted, ToolSummary

    now = [100.0]
    monkeypatch.setattr(ui, "monotonic", lambda: now[0])
    activity = ui.Activity(prompt_state="running", status="Thinking…")

    def row():
        return styled(activity.status_fragments("⠋", 80, hold=2.5))

    assert row()["activity.phase"] == "Thinking"
    # Changes inside the hold are skipped; the clock keeps moving meanwhile.
    activity.status = "Responding…"
    now[0] += 0.5
    activity.status = "Compacting context…"
    now[0] += 0.5
    assert row()["activity.phase"] == "Thinking"
    now[0] += 0.5
    assert row()["activity.meta"] == "1s"
    # Once it is up, the row jumps to what is current and holds that in turn.
    now[0] += 1.0
    assert row()["activity.phase"] == "Compacting context"
    activity.status = "Thinking…"
    now[0] += 0.5
    assert row()["activity.phase"] == "Compacting context"
    # A gap in drawing drops the hold.
    now[0] += 30
    assert row()["activity.phase"] == "Thinking"
    # So does a retry: news, not churn.
    activity.status = "Retrying · Overloaded…"
    assert row()["activity.phase"] == "Retrying"
    # And a new turn, even one queued right behind the last.
    activity.finish_prompt("done")
    activity.start_prompt("next")
    activity.status = "Thinking…"
    assert row()["activity.phase"] == "Thinking"
    # A held tool call that finishes is not left reading as running.
    activity.tools.record(ToolStarted("read_file", "x.py", "r"))
    now[0] += 3
    assert row()["activity.phase"] == "Read file"
    activity.tools.record(ToolSummary("read_file", "x.py", call_id="r"))
    activity.tools.record(ToolStarted("run_shell", "ls", "s"))
    activity.tools.record(ToolSummary("run_shell", "ls", call_id="s"))
    now[0] += 0.1
    assert row()["activity.phase"] != "Read file"
    # Without a hold, every change shows at once.
    activity.status = "Responding…"
    assert styled(activity.status_fragments("⠋", 80))["activity.phase"] == "Responding"


def test_thinking_rows_hold_the_newest_thought_above_the_status_row():
    from rich.cells import cell_len

    from pcode.runtime import TextDelta, Thinking, ThinkingDelta, ToolStarted
    from pcode.stream_display import present_stream_event
    from pcode.ui import Activity

    class Output:
        """Accepts every output call; `app.invalidate()` included."""

        def __getattr__(self, name):
            return self if name == "app" else lambda *args: None

    activity = Activity(prompt_state="running", status="Thinking…")

    def feed(event):
        present_stream_event(
            event, output=Output(), transcript=None, activity=activity, present=None
        )

    def rows(width=80, limit=3):
        return [text for _, text in activity.thought_fragments(width, limit)]

    def row(width=80):
        return "\n".join(rows(width))

    assert activity.thinking_mode == "status-line" and row() == ""
    # Untitled text shows its newest line, faded and indented past the spinner.
    feed(ThinkingDelta("The status row"))
    feed(ThinkingDelta(" is empty\n\n"))
    assert activity.thought_fragments(80) == [
        ("class:activity.thinking", "  The status row is empty")
    ]
    # A long line wraps to a few rows and keeps its newest words, cut from the front.
    feed(ThinkingDelta("so " + "word " * 40 + "newest"))
    assert len(rows(50)) == 3 and all(cell_len(text) <= 50 for text in rows(50))
    assert rows(50)[0].startswith("  …word") and rows(50)[-1].endswith("newest")
    assert "…" not in "".join(rows(50)[1:])
    assert all(style == "class:activity.thinking" for style, _ in activity.thought_fragments(50))
    # A short pane gets fewer rows; a wide one needs no cut.
    assert len(rows(50, 1)) == 1 and rows(50, 1)[0].endswith("newest")
    assert rows(400) == ["  so " + "word " * 40 + "newest"]
    # A titled section shows its title, not the prose under it.
    feed(ThinkingDelta("\n\n**Tracing the resize path**\n\nI need to check the replay"))
    assert row() == "  Tracing the resize path"
    # Its own row: a running tool takes the status row, not this one.
    activity.tools.record(ToolStarted("read_file", "ui.py", "one"))
    assert "Read file" in "".join(t for _, t in activity.status_fragments("⠋", 80))
    assert row() == "  Tracing the resize path"
    # An ended block is held, through the answer, until the next replaces it.
    feed(Thinking("done"))
    feed(TextDelta("Answer"))
    assert row() == "  Tracing the resize path"
    feed(Thinking("A whole block, with no deltas"))
    assert row() == "  A whole block, with no deltas"
    feed(ThinkingDelta("Next idea"))
    assert row() == "  Next idea"
    # The other modes have no row; a new turn starts empty.
    activity.thinking_mode = "scrollback"
    assert row() == ""
    activity.thinking_mode = "status-line"
    activity.start_prompt("again")
    assert row() == ""


@pytest.mark.parametrize("width", [0, 1, 2, 3, 12, 40, 100])
def test_status_row_truncates_to_terminal_width(width):
    from rich.cells import cell_len

    from pcode.runtime import ToolStarted
    from pcode.ui import Activity

    activity = Activity(prompt_state="running")
    activity.tools.record(ToolStarted("read_file", "界面/path\n" * 30, "one"))
    rendered = "".join(text for _, text in activity.status_fragments("⠋", width, "✓3 tools"))
    assert "\n" not in rendered
    assert cell_len(rendered) <= width
    if width >= 40:
        assert "…" in rendered
    elif width > 2:
        # Too narrow for the detail to say anything: the phase alone, cut if it must be.
        assert "界面" not in rendered


def test_narrow_status_row_drops_the_tally_before_the_detail():
    from pcode.ui import Activity

    activity = Activity(
        prompt_state="running", status="Retrying · Overloaded, retrying request 1/3…"
    )
    parts = styled(activity.status_fragments("⠋", 32, "✓12 ✗3 tools"))
    assert parts["activity.meta"] == "0s"
    assert parts["activity.detail"].startswith(" · Overloaded")


def test_system_prompt_row_is_badged_and_not_an_echoed_command():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system", detail="keep {tests}")
    fragments = activity.status_fragments("⠋", 80)
    assert styled(fragments) == {
        "activity.spinner": "⠋ ",
        "activity.badge": "◈ ",
        "activity.phase": "Compacting context",
        "activity.detail": " ▸ keep {tests}",
        "activity.meta": "0s",
    }
    rendered = "".join(text for _, text in fragments)
    assert "/compact" not in rendered and "❯" not in rendered


def test_system_prompt_row_drops_empty_detail():
    from pcode.ui import Activity

    activity = Activity()
    activity.start_prompt("Compacting context", kind="system")
    assert "activity.detail" not in styled(activity.status_fragments("⠋", 80))


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
    assert "activity.badge" not in styled(activity.status_fragments("⠋", 40))
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
    parts = styled(activity.status_fragments("⠋", 80))
    running = parts["activity.badge"] + parts["activity.phase"] + parts["activity.detail"]
    assert queued.removeprefix("Queued ") == running


@pytest.mark.parametrize("inspector_command", ["/tools", "/tools failed"])
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
        call = ToolStarted("read_file", "example.py", "call-1")
        archive.event(call)

        class Runtime:
            session = None
            recovery_blocked = ""
            inspections = archive

            async def stream(self, text):
                calls.append(text)
                yield call
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
                    # The prompt's own echo, which a host sends as its turn starts.
                    await wait_for(lambda: user.called)
                    user.reset_mock()
                    pipe.send_text("/theme light\r/help\r/new\r/resume\r/nope\r/theme invalid\r")
                    # With a host, the terminal's own commands do not wait behind
                    # the session's, so wait for every answer rather than the last.
                    expected = [
                        "Usage: /theme",
                        "Unknown command",
                        "/new is unavailable while working",
                    ]
                    if not app.hosted:
                        # A hosted terminal resumes elsewhere, leaving this session working.
                        expected.append("/resume is unavailable while working")
                    await wait_for(lambda: all(text in printed.getvalue() for text in expected))
                    assert app.transcript.theme == "light"
                    user.assert_not_called()
                    assert app.activity.busy
                    assert app.activity.queued_prompts == []
                    assert calls == ["first"]
                    pipe.send_text(inspector_command + "\r")
                    await wait_for(lambda: inspector is not None and inspector.app.is_running)
                    assert inspector.failed == (inspector_command == "/tools failed")
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


def test_tools_lists_every_call_of_the_running_turn(tmp_path):
    """/tools mid-turn shows the turn's finished calls, not only the running one.

    The turn creates its session journal lazily, as a real first turn does. A
    terminal attached to a host (the socket transport) learns the journal's path
    only from the host, and its copy of the tree was stale until the turn ended.
    """

    async def run():
        from pcode.inspection import ToolArchive
        from pcode.inspector_ui import ToolInspector
        from pcode.runtime import ToolStarted, ToolSummary
        from pcode.sessions import SavedSession

        started = asyncio.Event()
        finish = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""
            inspections = ToolArchive()

            async def stream(self, text):
                self.session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
                self.session.append("turn_started", prompt=text, run_id="run-1", sync=True)
                for index in (1, 2):
                    for event in (
                        ToolStarted("read_file", f"file{index}.py", f"call-{index}"),
                        ToolSummary("read_file", "done", call_id=f"call-{index}"),
                    ):
                        self.session.event(event, run_id="run-1")
                        yield event
                running = ToolStarted("shell", "make test", "call-3")
                self.session.event(running, run_id="run-1")
                yield running
                started.set()
                await finish.wait()
                yield Message("done")

        runtime = Runtime()
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        with create_pipe_input() as pipe:
            session = None
            inspector = None

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
                    await wait_for(
                        lambda: any(
                            call.event.call_id == "call-3" for call in app.activity.tools.calls
                        )
                    )
                    pipe.send_text("/tools\r")
                    await wait_for(lambda: inspector is not None and inspector.app.is_running)
                    calls = {call.call_id: call.state for call in inspector.archive.calls}
                    assert calls == {
                        "call-1": "succeeded",
                        "call-2": "succeeded",
                        "call-3": "running",
                    }
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not inspector.app.is_running)
                    finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    finish.set()
                    # A failed assertion leaves the modal open, and cancelling
                    # the app underneath it never returns.
                    if inspector is not None and inspector.app.is_running:
                        inspector.app.exit()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if runtime.session is not None:
                        runtime.session.close()

    asyncio.run(run())


@pytest.mark.in_process("a hosted /quit detaches; the turn carries on in the host")
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
