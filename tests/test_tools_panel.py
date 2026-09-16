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


def test_calls_update_in_place_even_when_results_arrive_out_of_order():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "first.py", "first"))
    history.record(ToolStarted("read_file", "second.py", "second"))
    history.record(ToolSummary("read_file", "second.py → read", call_id="second"))
    assert history.calls[0].running
    assert not history.calls[1].running
    history.record(ToolSummary("read_file", "first.py → read", call_id="first"))
    assert [call.event.call_id for call in history.calls] == ["first", "second"]
    assert all(not call.running for call in history.calls)


def test_history_retains_ten_calls_and_shows_five_without_numbers():
    history = ToolHistory()
    for i in range(12):
        history.record(ToolSummary("read_file", f"file_{i}.py", call_id=str(i)))
    assert len(history.calls) == 10
    assert [call.event.call_id for call in history.calls] == [str(i) for i in range(2, 12)]
    lines = "".join(text for _, text in panel_fragments(history.rows(5), 100)).splitlines()
    assert len(lines) == 5
    assert all(line.startswith("  ✓ Read ·") for line in lines)
    assert "/tools" not in "".join(lines)
    assert "file_7.py" in lines[0]
    assert "file_11.py" in lines[-1]
    history.clear()
    assert history.calls == []
    assert panel_fragments(history.rows(5), 100) == []


@pytest.mark.parametrize("width", [1, 8, 24, 80])
def test_panel_rows_are_cell_bounded_and_controls_cannot_change_layout(width):
    history = ToolHistory()
    history.record(ToolStarted("read_file", "界e\u0301🙂\n\x1b[2J" * 20, "one"))
    history.record(ToolSummary("run_command", "failed", failed=True, command="pytest -q"))
    fragments = panel_fragments(history.rows(5, nested=True), width)
    lines = "".join(text for _, text in fragments).splitlines()
    assert len(lines) == 2
    assert all(cell_len(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines)
    assert any(style == "class:tool.failed" for style, _ in fragments)


def test_running_calls_stop_on_interruption_and_planning_success_is_not_duplicated():
    history = ToolHistory()
    history.record(ToolStarted("read_file", "path", "one"))
    history.record(ToolStarted("write_plan", "", "plan"))
    history.record(ToolSummary("write_plan", "Plan updated", call_id="plan"))
    history.record(ToolSummary("write_plan", "Invalid task", failed=True, call_id="bad"))
    history.interrupt_running()
    assert len(history.calls) == 2
    assert not any(call.running for call in history.calls)
    assert "interrupted" in history.calls[0].line()
    assert "failed" in history.calls[1].line()


def test_tools_do_not_commit_model_tail_or_enqueue_permanent_output():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield TextDelta("model ")
            yield ToolStarted("read_file", "hidden_tool_target", "one")
            assert output.tail == "model "
            assert not output.pending
            yield ToolSummary("read_file", "hidden_tool_target → read", call_id="one")
            assert output.tail == "model "
            assert not output.pending
            yield TextDelta("answer")
            yield Message("model answer")

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=stream))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, app.activity, terminal_app)
    app.transcript.output = output

    async def run():
        assert await app.run_live(output, "go")
        await output.flush()
        assert "model answer" in stream.getvalue()
        assert "hidden_tool_target" not in stream.getvalue()
        assert len(app.activity.tools.calls) == 1
        assert not app.activity.tools.calls[0].running

    asyncio.run(run())


def test_errors_are_retained_without_numbered_expansion_and_reset_clears_them():
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
    app.transcript.events((event,))
    assert stream.getvalue() == ""
    assert app.activity.tools.calls[0].event.error == event.error
    assert "! Run failed" in app.activity.tools.calls[0].line()
    assert app.registry.find("/tools") is not None
    with pytest.raises(ValueError, match="Usage: /tools"):
        app.registry.dispatch("/tools 1")
    app.new("")
    assert not app.activity.tools.calls


def test_cancelled_run_marks_outstanding_tool_interrupted():
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield ToolStarted("run_command", "sleep 30", "one", command="sleep 30")
            raise asyncio.CancelledError

    app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
    terminal_app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
    output = TerminalOutput(app.transcript.console, app.activity, terminal_app)
    assert not asyncio.run(app.run_live(output, "go"))
    assert app.activity.tools.calls[0].interrupted
    assert not app.activity.tools.calls[0].running


def test_late_result_of_evicted_start_does_not_count_as_a_new_call():
    history = ToolHistory()
    for i in range(11):
        history.record(ToolStarted("read_file", f"file_{i}", str(i)))
    history.record(ToolSummary("read_file", "file_0 completed", call_id="0"))
    assert [call.event.call_id for call in history.calls] == [str(i) for i in range(1, 11)]
    assert "0" not in history._running
    history.interrupt_running()
    assert not history._running


def test_resume_restores_tools_independently_of_conversation_limit(tmp_path):
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
        calls = app.activity.tools.calls
        assert [call.event.call_id for call in calls] == ["one", "two", "three"]
        assert calls[-1].interrupted
        assert calls[-1].event.command == "sleep 30"
        assert not any(call.running for call in calls)
    finally:
        saved.close()


def test_recent_tools_follow_active_task_without_headers_or_empty_rows():
    history = ToolHistory()
    history.record(ToolSummary("read_file", "example.py"))
    items = [
        {"id": "one", "content": "Inspect", "status": "completed"},
        {"id": "two", "content": "Implement", "status": "in_progress"},
        {"id": "three", "content": "Validate", "status": "pending"},
    ]
    lines = task_panel_rows(items, history, 10, "⟳")
    assert [text for _, text in lines] == [
        "  ✓ Inspect",
        "  ⟳ Implement",
        "      ✓ Read · example.py",
        "  ○ Validate",
    ]
    items[1]["status"] = "completed"
    items[2]["status"] = "in_progress"
    assert [text for _, text in task_panel_rows(items, history, 10, "⟳")][-2:] == [
        "  ⟳ Validate",
        "      ✓ Read · example.py",
    ]
    history.clear()
    assert len(task_panel_rows(items, history, 10, "⟳")) == 3
    assert task_panel_rows([], history, 10, "⟳") == []


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_without_active_task_tools_are_root_rows_not_children_of_inactive_task(status):
    history = ToolHistory()
    history.record(ToolSummary("read_file", "example.py"))
    items = [{"id": "one", "content": "A task", "status": status}]
    assert task_panel_rows(items, history, 10, "⟳")[-1] == (
        "class:plan",
        "  ✓ Read · example.py",
    )
    assert task_panel_rows([], history, 10, "⟳") == [("class:plan", "  ✓ Read · example.py")]


@pytest.mark.parametrize("budget", [1, 2, 4, 6, 10])
@pytest.mark.parametrize("final_status", ["completed", "cancelled"])
def test_finished_plan_hides_tools_and_retains_task_rows(budget, final_status):
    history = ToolHistory()
    for i in range(5):
        history.record(ToolSummary("read_file", f"file_{i}.py"))
    items = [{"id": str(i), "content": f"Task {i}", "status": "completed"} for i in range(5)]
    items[-1]["status"] = "in_progress"
    assert any("Read" in text for _, text in task_panel_rows(items, history, 10, "⟳"))

    items[-1]["status"] = final_status
    expected = [
        ("class:plan", f"  {'–' if item['status'] == 'cancelled' else '✓'} {item['content']}")
        for item in items[:budget]
    ]
    assert task_panel_rows(items, history, budget, "⟳") == expected
    # Rendering hides activity without destroying history or completed tasks.
    assert len(history.calls) == 5
    assert task_panel_rows(items, history, budget, "⟳") == expected


@pytest.mark.parametrize("budget", [1, 2, 4, 6, 10])
def test_shared_task_tool_budget_keeps_active_item_and_latest_calls_visible(budget):
    history = ToolHistory()
    for i in range(10):
        history.record(ToolSummary("read_file", f"file_{i}.py"))
    items = [{"id": str(i), "content": f"Task {i}", "status": "pending"} for i in range(12)]
    items[8]["status"] = "in_progress"
    lines = task_panel_rows(items, history, budget, "⟳")
    assert len(lines) <= budget
    text = [text for _, text in lines]
    active = text.index("  ⟳ Task 8")
    count = min(3, budget - 1)
    assert sum("Read ·" in line for line in text) == count
    assert all(line.startswith("      ✓ Read") for line in text[active + 1 : active + 1 + count])
    if count:
        assert text[active + count] == "      ✓ Read · file_9.py"
    assert all("Tasks ·" not in line and "Tools" not in line for line in text)
