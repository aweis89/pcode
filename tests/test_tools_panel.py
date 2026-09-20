import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, TextDelta, ToolStarted, ToolSummary
from pcode.tool_panel import ToolHistory, panel_fragments, task_panel_rows
from pcode.ui import CursorSafeOutput, TerminalOutput


def test_results_remove_their_call_even_when_they_arrive_out_of_order():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "first.py", "first"))
    history.record(ToolStarted("read_file", "second.py", "second"))
    history.record(ToolSummary("read_file", "second.py → read", call_id="second"))
    assert [call.event.call_id for call in history.calls] == ["first"]
    history.record(ToolSummary("read_file", "first.py → read", call_id="first"))
    assert history.calls == []


def test_restated_start_updates_in_place_and_the_newest_call_owns_the_status_row():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "first.py", "first"))
    history.record(ToolStarted("read_file", "first.py", "first", activity="reading"))
    assert len(history.calls) == 1
    assert "reading" in history.active.line()
    history.record(ToolStarted("grep", "pattern", "second"))
    assert history.active.event.call_id == "second"
    assert [call.event.call_id for call in history.background] == ["first"]


def test_settled_calls_do_not_linger_in_the_panel():
    history = ToolHistory()
    for i in range(12):
        history.record(ToolStarted("read_file", f"file_{i}.py", str(i)))
        history.record(ToolSummary("read_file", f"file_{i}.py", call_id=str(i)))
    assert history.calls == []
    assert panel_fragments(history.rows(5), 100) == []


def test_a_command_that_finishes_instantly_still_holds_the_status_row(monkeypatch):
    history = ToolHistory()
    history.record(ToolStarted("run_command", "quick", "one", command="true"))
    history.record(ToolSummary("run_command", "quick", call_id="one"))
    # Gone from the panel, but the status row keeps it long enough to read.
    assert history.calls == []
    assert history.active is not None and history.active.event.call_id == "one"
    frozen = history.active.line()
    assert history.active.line() == frozen  # Its duration stops ticking.
    # Real work always wins the row back.
    history.record(ToolStarted("grep", "pattern", "two"))
    assert history.active.event.call_id == "two"
    history.record(ToolSummary("grep", "pattern", call_id="two"))
    monkeypatch.setattr("pcode.tool_panel.STATUS_DWELL", 0.0)
    assert history.active is None
    assert history.recent is None


@pytest.mark.parametrize("width", [1, 8, 24, 80])
def test_panel_rows_are_cell_bounded_and_controls_cannot_change_layout(width):
    history = ToolHistory()
    history.record(ToolStarted("read_file", "界e\u0301🙂\n\x1b[2J" * 20, "one"))
    history.record(ToolStarted("run_command", "running", "two", command="pytest -q"))
    history.record(ToolStarted("grep", "pattern", "three"))
    fragments = panel_fragments(history.rows(5, nested=True), width)
    lines = "".join(text for _, text in fragments).splitlines()
    # The newest call belongs to the status row, leaving two background rows.
    assert len(lines) == 2
    assert all(cell_len(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines)
    assert all(style == "class:plan.active" for style, _ in fragments)


def test_planning_calls_never_reach_the_panel():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "path", "one"))
    history.record(ToolStarted("write_plan", "", "plan"))
    history.record(ToolSummary("write_plan", "Invalid task", failed=True, call_id="plan"))
    assert [call.event.call_id for call in history.calls] == ["one"]
    history.clear()
    assert history.calls == []


def test_tools_do_not_commit_model_tail_but_summaries_reach_scrollback():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield TextDelta("model ")
            yield ToolStarted("read_file", "hidden_tool_target", "one")
            assert output.tail == "model "
            yield ToolSummary("read_file", "hidden_tool_target → read", call_id="one")
            yield TextDelta("answer")
            yield Message("model answer")

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=stream))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, terminal_app)
    app.transcript.output = output

    async def run():
        assert await app.run_live(output, "go")
        await output.flush()
        printed = stream.getvalue()
        # A settled call commits the prose before it, then summarizes itself.
        assert printed.index("model") < printed.index("hidden_tool_target")
        assert printed.index("hidden_tool_target") < printed.index("answer")
        assert printed.count("hidden_tool_target") == 1
        assert app.activity.tools.calls == []

    asyncio.run(run())


def test_failed_commands_stay_out_of_scrollback_without_command_mirroring():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream))
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        call_id="one",
        command="pytest -q",
        error="FAILED test_example: missing module",
    )
    app.present_events((event,))
    assert stream.getvalue() == ""
    assert app.activity.tools.calls == []
    assert app.registry.find("/tools") is not None
    with pytest.raises(ValueError, match="Usage: /tools"):
        app.registry.dispatch("/tools 1")


def test_new_clears_task_panel_and_previous_prompt_row():
    app = PreviewApp(console=Console(file=StringIO()))
    app.activity.plan = [{"id": "one", "content": "A task", "status": "completed"}]
    app.activity.tools.record(ToolStarted("read_file", "file.py", "one"))
    app.activity.prompt = "previous prompt"
    app.activity.prompt_state = "done"
    app.activity.status = "Responding…"
    app.new("")
    assert app.activity.plan == []
    assert not app.activity.tools.calls
    assert app.activity.prompt == ""
    assert app.activity.prompt_state == ""
    assert app.activity.status == ""
    assert task_panel_rows(app.activity.plan, app.activity.tools, 10, "⠋") == []


def test_cancelled_run_drops_outstanding_tools_from_the_panel():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield ToolStarted("run_command", "sleep 30", "one", command="sleep 30")
            raise asyncio.CancelledError

    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, terminal_app)
    assert not asyncio.run(app.run_live(output, "go"))
    assert app.activity.tools.calls == []


def test_resume_leaves_no_stale_running_tools(tmp_path):
    from pcode.sessions import SavedSession

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.append("turn_started", prompt="go")
        saved.event(ToolStarted("read_file", "one", "one"))
        saved.event(ToolStarted("read_file", "two", "two"))
        saved.event(ToolSummary("read_file", "two → read", call_id="two"))
        saved.event(ToolStarted("run_command", "waiting", "three", command="sleep 30"))
        saved.event(ToolSummary("read_file", "one → read", call_id="one"))
        saved.append("turn_cancelled")
        for _ in range(50):
            saved.append("Message", markdown="later conversation")
        assert not any(record["kind"] == "ToolSummary" for record in saved.recent_transcript())
        app = PreviewApp(
            model="test:local",
            runtime=SimpleNamespace(session=saved),
            console=Console(file=StringIO()),
        )
        app.replay()
        assert app.activity.tools.calls == []
    finally:
        saved.close()


def test_concurrent_tools_follow_active_task_without_headers_or_empty_rows():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "example.py", "one"))
    history.record(ToolStarted("grep", "pattern", "status-row"))
    items = [
        {"id": "one", "content": "Inspect", "status": "completed"},
        {"id": "two", "content": "Implement", "status": "in_progress"},
        {"id": "three", "content": "Validate", "status": "pending"},
    ]
    text = [text for _, text in task_panel_rows(items, history, 10, "⟳")]
    assert text[:2] == ["✓ Inspect", "⟳ Implement"]
    assert text[2].startswith("    ⟳ Read") and text[2].endswith("example.py")
    assert text[3] == "○ Validate"
    items[1]["status"] = "completed"
    items[2]["status"] = "in_progress"
    assert [text for _, text in task_panel_rows(items, history, 10, "⟳")][-2] == "⟳ Validate"
    history.clear()
    assert len(task_panel_rows(items, history, 10, "⟳")) == 3
    assert task_panel_rows([], history, 10, "⟳") == []


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_without_active_task_tools_are_root_rows_not_children_of_inactive_task(status):
    history = ToolHistory()
    history.record(ToolStarted("read_file", "example.py", "one"))
    history.record(ToolStarted("grep", "pattern", "status-row"))
    items = [{"id": "one", "content": "A task", "status": status}]
    style, text = task_panel_rows(items, history, 10, "⟳")[-1]
    assert style == "class:plan.active"
    assert text.startswith("⟳ Read") and text.endswith("example.py")
    assert task_panel_rows([], history, 10, "⟳")[0][1] == text


@pytest.mark.parametrize("budget", [1, 2, 4, 6, 10])
def test_shared_task_tool_budget_keeps_active_item_and_oldest_calls_visible(budget):
    history = ToolHistory()
    for i in range(10):
        history.record(ToolStarted("read_file", f"file_{i}.py", str(i)))
    items = [{"id": str(i), "content": f"Task {i}", "status": "pending"} for i in range(12)]
    items[8]["status"] = "in_progress"
    lines = task_panel_rows(items, history, budget, "⟳")
    assert len(lines) <= budget
    text = [text for _, text in lines]
    active = text.index("⟳ Task 8")
    count = min(3, budget - 1)
    assert sum("Read ·" in line for line in text) == count
    assert all(line.startswith("    ⟳ Read") for line in text[active + 1 : active + 1 + count])
    if count:
        assert text[active + 1].endswith("file_0.py")
    assert all("Tasks ·" not in line and "Tools" not in line for line in text)
