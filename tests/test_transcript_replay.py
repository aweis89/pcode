import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.markdown import Markdown

from pcode.runtime import ToolSummary
from pcode.transcript_log import TranscriptLog
from pcode.transcript_notice import TranscriptNotice
from pcode.ui import CursorSafeOutput, Handoff, TerminalOutput, Transcript


def view():
    return Transcript(Console(file=StringIO(), width=80, color_system=None))


def project(transcript):
    stream = StringIO()
    console = Console(file=stream, width=80, color_system=None)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return stream.getvalue()


def test_hidden_commands_reappear_in_order_and_repeated_replay_does_not_record():
    transcript = view()
    transcript.user("TASK_MARKER")
    transcript.tool_result(ToolSummary("run_command", "run", command="echo hi", result="HI_RESULT"))
    transcript.print("ANSWER_MARKER")
    original = len(transcript.log.entries)
    assert "HI_RESULT" not in project(transcript)
    transcript.command_scrollback = True
    shown = project(transcript)
    assert shown.index("TASK_MARKER") < shown.index("HI_RESULT") < shown.index("ANSWER_MARKER")
    transcript.command_scrollback = False
    assert "HI_RESULT" not in project(transcript)
    transcript.command_scrollback = True
    assert project(transcript) == shown
    assert len(transcript.log.entries) == original


def test_failure_has_exactly_one_representation_and_uses_command_visibility():
    transcript = view()
    transcript.tool_result(
        ToolSummary(
            "run_command", "run", failed=True, command="test", result="FULL_RESULT", error="EXCERPT"
        )
    )
    assert not project(transcript)
    transcript.command_scrollback = True
    # Mirroring alone keeps the failure to its summary line.
    assert "FULL_RESULT" not in project(transcript)
    assert "✗ Run" in project(transcript)
    transcript.tool_error_scrollback = True
    assert project(transcript).count("FULL_RESULT") == 1
    assert "EXCERPT" not in project(transcript)
    transcript.command_scrollback = False
    assert not project(transcript)
    transcript.command_scrollback = True
    assert project(transcript).count("FULL_RESULT") == 1


def test_replay_rebuilds_markdown_theme_and_notice_line_limits():
    transcript = view()
    transcript.print(Markdown("```python\nprint(1)\n```", code_theme=transcript.code_theme))
    transcript.error("\n".join(str(i) for i in range(50)))
    transcript.theme = "light"
    transcript.error_scrollback_lines = 3
    renderables = [obj for objects, _, _ in transcript.replay() for obj in objects]
    assert renderables[0].code_theme == transcript.code_theme
    notice = next(obj for obj in renderables if isinstance(obj, TranscriptNotice))
    assert notice.code_theme == transcript.code_theme
    assert notice.max_lines == 3


def test_retention_is_bounded_and_snapshot_does_not_follow_mutations():
    from rich.text import Text

    transcript = view()
    transcript.log = TranscriptLog(limit=2)
    text = Text("snapshot")
    transcript.print("evicted")
    transcript.print(text)
    text.append(" MUTATED")
    transcript.print("last")
    rendered = project(transcript)
    assert "omitted" in rendered
    assert "snapshot" in rendered
    assert "evicted" not in rendered and "MUTATED" not in rendered
    assert len(transcript.log.entries) == 2


def test_long_session_replays_in_full_and_is_bounded_by_retained_text():
    """One committed block costs several entries, so the budget counts text."""
    transcript = view()
    for i in range(4000):
        transcript.print(Markdown(f"LINE_{i:04d} body text"))
        transcript.print()
    rendered = project(transcript)
    assert "LINE_0000" in rendered and "LINE_3999" in rendered
    assert not transcript.log.dropped

    transcript = view()
    transcript.log = TranscriptLog(max_chars=100)
    transcript.print("x" * 60)
    transcript.print("y" * 60)
    assert [entry.args for entry in transcript.log.entries] == [("y" * 60,)]
    assert transcript.log.chars == 60
    assert transcript.log.dropped
    # A single oversized entry is still worth keeping; it is the newest history.
    transcript.print("z" * 500)
    assert transcript.log.entries[-1].args == ("z" * 500,)
    assert len(transcript.log.entries) == 1


def test_regenerate_is_noop_for_redirected_output():
    transcript = view()
    output = Mock()
    transcript.output = output
    transcript.regenerate()
    output.regenerate.assert_not_called()


def test_rebuild_includes_arrivals_during_handoff_and_keeps_unfinished_tail(monkeypatch):
    async def run():
        transcript = view()
        terminal = DummyOutput()
        terminal.write_raw = Mock()
        app = SimpleNamespace(output=CursorSafeOutput(terminal))
        output = TerminalOutput(transcript.console, app)
        transcript.output = output
        output.begin_turn("STREAM_TASK")
        output.delta("COMMITTED_TEXT\n\nUNFINISHED_TAIL")

        @asynccontextmanager
        async def handoff(app, **kwargs):
            # Equivalent to events arriving while the handoff awaits CPR.
            transcript.note("ARRIVED_DURING_CPR")
            yield Handoff(None)

        monkeypatch.setattr("pcode.ui.suspended_editor", handoff)
        output.regenerate(transcript.replay)
        output.regenerate(transcript.replay)
        await output.flush()
        text = transcript.console.file.getvalue()
        assert text.count("COMMITTED_TEXT") == 1
        assert text.count("STREAM_TASK") == 1
        assert text.count("ARRIVED_DURING_CPR") == 1
        assert "UNFINISHED_TAIL" not in text
        assert output.tail == "UNFINISHED_TAIL"
        assert output.streamed
        assert not output.pending
        assert not output.changed.is_set()
        terminal.write_raw.assert_called_once_with("\x1b[H\x1b[2J\x1b[3J")
        output.regenerate(transcript.replay)
        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(empty_handoff))
        transcript.console.file.seek(0)
        transcript.console.file.truncate()
        await output.flush()
        assert "ARRIVED_DURING_CPR" not in transcript.console.file.getvalue()
        output.finish()
        assert project(transcript).count("UNFINISHED_TAIL") == 1

    async def empty_handoff(app, **kwargs):
        yield Handoff(None)

    asyncio.run(run())


def test_flush_reports_the_rows_written_at_the_terminal_width(monkeypatch):
    async def run():
        transcript = view()
        terminal = DummyOutput()
        terminal.get_size = lambda: Size(rows=40, columns=20)
        app = SimpleNamespace(output=CursorSafeOutput(terminal))
        output = TerminalOutput(transcript.console, app)
        handoffs = []

        async def handoff(app, **kwargs):
            handoffs.append(Handoff(11))
            yield handoffs[-1]

        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))
        output.print("x " * 25)  # wraps to three rows at width 20
        output.print("second")
        await output.flush()
        assert handoffs[0].rows_written == transcript.console.file.getvalue().count("\n") == 4
        # The console writes to its own file again once the batch is out.
        assert transcript.console.file is not handoffs[0]
        output.regenerate(transcript.replay)
        await output.flush()
        assert handoffs[1].top_row == 1

    asyncio.run(run())


def paced_output(monkeypatch, handoffs):
    transcript = view()
    terminal = DummyOutput()
    terminal.get_size = lambda: Size(rows=40, columns=80)
    app = SimpleNamespace(output=CursorSafeOutput(terminal))
    output = TerminalOutput(transcript.console, app)
    transcript.output = output
    output.paced = True

    async def handoff(app, **kwargs):
        handoffs.append(Handoff(11))
        yield handoffs[-1]

    monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))
    return transcript, output


def test_paced_flush_writes_one_row_per_frame_and_keeps_ticking(monkeypatch):
    async def run():
        handoffs = []
        transcript, output = paced_output(monkeypatch, handoffs)
        for index in range(3):
            output.print(f"ROW_{index}")
        await output.flush()
        written = transcript.console.file.getvalue()
        assert written == "ROW_0\n"
        assert handoffs[0].rows_written == 1
        # The rest is queued, and the loop is asked for another frame.
        assert [row.strip() for row in output.rows] == ["ROW_1", "ROW_2"]
        assert not output.pending
        assert output.changed.is_set()
        await output.flush()
        await output.flush()
        assert transcript.console.file.getvalue() == "ROW_0\nROW_1\nROW_2\n"
        assert len(handoffs) == 3
        assert not output.rows
        assert not output.changed.is_set()

    asyncio.run(run())


def test_paced_flush_keeps_a_big_block_within_the_drain_budget(monkeypatch):
    async def run():
        handoffs = []
        transcript, output = paced_output(monkeypatch, handoffs)
        output.print("\n".join(f"ROW_{index}" for index in range(300)))
        frames = 0
        while output.rows or output.pending:
            await output.flush()
            frames += 1
        assert frames <= 31
        assert transcript.console.file.getvalue().count("\n") == 300
        assert sum(handoff.rows_written for handoff in handoffs) == 300
        # Rows arriving while a backlog drains keep their order behind it.
        output.print("ROW_A")
        output.print("ROW_B")
        await output.flush()
        assert transcript.console.file.getvalue().endswith("ROW_299\nROW_A\n")

    asyncio.run(run())


def test_paced_rows_are_dropped_by_replay_and_written_at_once_when_off(monkeypatch):
    async def run():
        handoffs = []
        transcript, output = paced_output(monkeypatch, handoffs)
        transcript.print("FIRST")
        transcript.print("SECOND")
        await output.flush()
        assert output.rows
        output.regenerate(transcript.replay)
        await output.flush()
        text = transcript.console.file.getvalue()
        assert text.count("FIRST") == 2 and text.count("SECOND") == 1
        assert not output.rows
        output.paced = False
        transcript.print("THIRD")
        transcript.print("FOURTH")
        await output.flush()
        assert transcript.console.file.getvalue().endswith("THIRD\nFOURTH\n")
        assert not output.rows

    asyncio.run(run())


def test_split_rows_keeps_newlines_and_a_trailing_partial_row():
    from pcode.ui import split_rows

    assert split_rows("a\nb\n") == ["a\n", "b\n"]
    assert split_rows("a\nb") == ["a\n", "b"]
    assert split_rows("") == []
    # Only newlines end a row; other control characters stay inside one.
    assert split_rows("a\x0bb\n") == ["a\x0bb\n"]


def test_clear_erases_the_screen_and_keeps_what_is_written_after_it(monkeypatch):
    async def run():
        transcript = Transcript(
            Console(file=StringIO(), width=80, color_system=None, force_terminal=True)
        )
        terminal = DummyOutput()
        terminal.write_raw = Mock()
        app = SimpleNamespace(output=CursorSafeOutput(terminal))
        output = TerminalOutput(transcript.console, app)
        transcript.output = output
        transcript.print("OLD_HISTORY")
        transcript.clear()
        transcript.print("AFTER_CLEAR")
        transcript.note("NEW_NOTICE")

        async def empty_handoff(app, **kwargs):
            yield Handoff(None)

        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(empty_handoff))
        transcript.console.file.seek(0)
        transcript.console.file.truncate()
        await output.flush()
        text = transcript.console.file.getvalue()
        terminal.write_raw.assert_called_once_with("\x1b[H\x1b[2J\x1b[3J")
        assert "OLD_HISTORY" not in text
        assert "AFTER_CLEAR" in text and "NEW_NOTICE" in text
        # A later redraw must not resurrect the cleared history either.
        assert "OLD_HISTORY" not in project(transcript)
        assert "omitted" not in project(transcript)

    asyncio.run(run())


def test_informational_notices_are_shown_once_and_not_retained():
    transcript = view()
    transcript.log = TranscriptLog(limit=2)
    transcript.print("RETAINED")
    for _ in range(3):
        transcript.note("Show commands: off. Usage: /show-commands on|off (Ctrl+S)")
    assert "Show commands: off" in transcript.console.file.getvalue()
    assert project(transcript).strip() == "RETAINED"
    assert project(transcript).strip() == "RETAINED"
    assert not transcript.log.dropped


def test_startup_banner_survives_a_redraw():
    transcript = view()
    transcript.welcome("test-model", "/tmp/workspace")
    transcript.retained_note("Loaded repository instructions: AGENTS.md")
    transcript.note("Show commands: off")
    rebuilt = project(transcript)
    assert "pcode" in rebuilt
    assert "/tmp/workspace" in rebuilt
    assert "Type / for commands" in rebuilt
    assert "Loaded repository instructions: AGENTS.md" in rebuilt
    assert "Command output in scrollback" not in rebuilt
