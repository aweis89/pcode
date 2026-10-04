"""`group_tools` folds each run of settled calls into one scrollback line."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.preferences import load_preferences
from pcode.runtime import JobFinished, Message, ToolSummary
from pcode.ui import Transcript, create_prompt


def grouped(**preferences):
    stream = StringIO()
    view = Transcript(
        Console(file=stream, width=80, color_system=None),
        preferences={"group_tools": "on", "show_edits": "off", **preferences},
    )
    return view, stream


def replayed(view) -> str:
    stream = StringIO()
    console = Console(file=stream, width=80, color_system=None)
    for objects, end, _ in view.replay():
        console.print(*objects, end=end)
    return stream.getvalue()


def edit(n):
    return ToolSummary("edit_file", f"f{n}.py → edited", call_id=f"e{n}")


def run(n, failed=False):
    return ToolSummary("shell", "cmd → exit 0", failed, call_id=f"s{n}", command=f"make {n}")


def test_a_run_of_calls_waits_for_its_close_and_writes_one_line():
    view, stream = grouped()
    for event in (edit(1), edit(2), run(1), edit(3)):
        view.tool_result(event)
    assert stream.getvalue() == ""
    assert view.pending_group_row(80) == "✓ 4 tools · Edit file ✓3 · Run shell ✓1"
    view.events((Message("Done."),))
    assert stream.getvalue().splitlines()[0] == "✓ 4 tools · Edit file ✓3 · Run shell ✓1"
    assert view.pending_group_row(80) == ""


def test_turn_end_closes_a_run_and_a_redraw_rebuilds_it():
    view, stream = grouped()
    for event in (edit(1), run(1)):
        view.tool_result(event)
    view.settle_tools()
    assert stream.getvalue() == "✓ 2 tools · Edit file ✓1 · Run shell ✓1\n"
    assert replayed(view) == stream.getvalue()


def test_a_redraw_mid_run_keeps_the_run_open():
    view, stream = grouped()
    for event in (edit(1), run(1)):
        view.tool_result(event)
    assert replayed(view) == ""
    assert view.pending_group_row(80) == "✓ 2 tools · Edit file ✓1 · Run shell ✓1"
    view.tool_result(edit(2))
    view.settle_tools()
    assert stream.getvalue() == "✓ 3 tools · Edit file ✓2 · Run shell ✓1\n"


def test_a_run_of_one_keeps_the_calls_own_line():
    view, stream = grouped()
    view.tool_result(edit(1))
    view.settle_tools()
    assert stream.getvalue().startswith("✓ Edit file  f1.py → edited")


def test_a_failure_folds_into_the_run_and_splits_its_tools_count():
    view, stream = grouped()
    for event in (edit(1), edit(2), run(1, failed=True), edit(3), run(2)):
        view.tool_result(event)
    assert view.pending_group_row(80) == "✓ 4 ✗ 1 tools · Edit file ✓3 · Run shell ✓1 ✗1"
    view.settle_tools()
    assert stream.getvalue() == "✓ 4 ✗ 1 tools · Edit file ✓3 · Run shell ✓1 ✗1\n"
    assert replayed(view) == stream.getvalue()


def test_each_mark_counts_only_its_own_outcome():
    view, stream = grouped()
    for event in (edit(1), run(1, failed=True), run(2, failed=True)):
        view.tool_result(event)
    view.settle_tools()
    assert stream.getvalue() == "✓ 1 ✗ 2 tools · Run shell ✗2 · Edit file ✓1\n"
    view, stream = grouped()
    for event in (run(1, failed=True), run(2, failed=True)):
        view.tool_result(event)
    view.settle_tools()
    assert stream.getvalue() == "✗ 2 tools · Run shell ✗2\n"


def test_a_lone_failure_keeps_its_own_line():
    view, stream = grouped()
    view.tool_result(run(1, failed=True))
    view.events((Message("Done."),))
    assert stream.getvalue().startswith("✗ Run shell")


def test_a_background_job_exit_is_not_folded_into_the_run():
    view, stream = grouped()
    view.tool_result(edit(1))
    view.tool_result(edit(2))
    view.tool_result(JobFinished("shell", "make → j1 · exit 0", command="make"))
    lines = stream.getvalue().splitlines()
    assert lines[0] == "✓ 2 tools · Edit file ✓2"
    assert "j1" in lines[1]


def test_a_delegate_gets_its_own_line_with_its_calls_folded_beneath():
    view, stream = grouped()
    view.tool_result(edit(1))
    view.tool_result(edit(2))
    for event in (
        ToolSummary("read_file", "a.py → 3 lines", call_id="p:r", parent_call_id="p"),
        ToolSummary("read_file", "b.py → 3 lines", call_id="p:r2", parent_call_id="p"),
        ToolSummary("shell", "x", call_id="p:s", command="echo", parent_call_id="p"),
        ToolSummary("shell", "x → exit 1", True, call_id="p:f", command="no", parent_call_id="p"),
        ToolSummary("delegate_task", "worker · look → Completed", call_id="p"),
    ):
        view.tool_result(event)
    view.tool_result(edit(3))
    view.settle_tools()
    lines = stream.getvalue().splitlines()
    assert lines[0] == "✓ 2 tools · Edit file ✓2"
    assert lines[1] == "✓ Delegate task  worker · look → Completed"
    assert lines[2] == "    ✓ 3 ✗ 1 tools · Read file ✓2 · Run shell ✓1 ✗1"
    assert lines[3].startswith("✓ Edit file  f3.py")
    assert replayed(view) == stream.getvalue()


def test_output_with_a_body_closes_the_run():
    view, stream = grouped(show_commands="on")
    view.tool_result(edit(1))
    view.tool_result(edit(2))
    view.tool_result(
        ToolSummary("shell", "pytest → exit 0", call_id="s", command="pytest", result="ok")
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "✓ 2 tools · Edit file ✓2"
    assert any("$ pytest" in line for line in lines[1:])


def test_off_by_default_every_call_keeps_its_line():
    stream = StringIO()
    view = Transcript(
        Console(file=stream, width=80, color_system=None), preferences={"show_edits": "off"}
    )
    view.tool_result(edit(1))
    view.tool_result(edit(2))
    assert len(stream.getvalue().splitlines()) == 2


def test_restored_history_closes_its_last_run():
    view, stream = grouped()
    with view.restore():
        view.tool_result(edit(1))
        view.tool_result(edit(2))
    assert "✓ 2 tools · Edit file ✓2" in stream.getvalue()


def test_slash_command_toggles_and_saves_the_default():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    assert app.registry.dispatch("/group-tools on")
    assert app.transcript.group_tools is True
    assert load_preferences()["group_tools"] == "on"
    assert "Group tools: on" in stream.getvalue()
    assert app.registry.dispatch("/group-tools")
    assert app.transcript.group_tools is False
    assert load_preferences()["group_tools"] == "off"
    with pytest.raises(ValueError, match=r"Usage: /group-tools"):
        app.registry.dispatch("/group-tools yes")


def render_panel(running: bool) -> list[str]:
    async def render():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        app.transcript.group_tools = True
        app.transcript.show_edits = False
        app.activity.prompt_state = "running" if running else "done"
        for event in (edit(1), edit(2), run(1)):
            app.transcript.tool_result(event)
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=app.activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[row][col].char for col in range(80)).rstrip()
                for row in range(screen.height)
            ]

    return asyncio.run(render())


def test_a_running_turn_counts_the_run_on_its_status_row():
    lines = render_panel(running=True)
    (row,) = [line for line in lines if "tools" in line]
    # The count sits beside the spinner that says the run is still going.
    assert row.lstrip()[1:].startswith(" Working")
    assert row.lstrip()[0] in "◜◠◝◞◡◟"
    assert row.endswith("✓3 tools · 0s")


def test_without_a_status_row_the_run_is_counted_flush_left():
    lines = render_panel(running=False)
    (row,) = [line for line in lines if "tools" in line]
    assert row == "✓ 3 tools · Edit file ✓2 · Run shell ✓1"
