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
    assert "FULL_RESULT" not in project(transcript)
    assert "✗ Run" in project(transcript)
    transcript.command_scrollback = True
    # Mirroring alone keeps the failure to its summary line.
    assert "FULL_RESULT" not in project(transcript)
    assert "✗ Run" in project(transcript)
    transcript.tool_error_scrollback = True
    assert project(transcript).count("FULL_RESULT") == 1
    assert "EXCERPT" not in project(transcript)
    transcript.command_scrollback = False
    assert "FULL_RESULT" not in project(transcript)
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
        assert [row.text.strip() for row in output.rows] == ["ROW_1", "ROW_2"]
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


def typed_output(monkeypatch, handoffs):
    transcript, output = paced_output(monkeypatch, handoffs)
    output.typed = True
    output.app.invalidate = Mock()
    return transcript, output


def shown(output):
    return "".join(text for _, text in output.typing_fragments())


def test_typed_prose_types_out_live_and_writes_each_row_once_complete(monkeypatch):
    from pcode.ui import TYPED_CHARS_PER_STEP, TYPED_STEP_FRAMES

    async def run():
        handoffs = []
        transcript, output = typed_output(monkeypatch, handoffs)
        sentence = "The quick brown fox jumps over the lazy dog, twice over."
        transcript.message(sentence)
        await output.flush()
        # The first frame types into the live row; scrollback is untouched.
        assert handoffs == [] and transcript.console.file.getvalue() == ""
        assert shown(output) == sentence[:TYPED_CHARS_PER_STEP]
        assert output.changed.is_set()
        assert output.app.invalidate.call_count == 1
        frames = 1
        while output.rows:
            await output.flush()
            frames += 1
            if output.rows:
                assert sentence.startswith(shown(output).rstrip())
        # A step every TYPED_STEP_FRAMES frames, and only a step repaints.
        steps = -(-len(sentence) // TYPED_CHARS_PER_STEP)
        assert frames == (steps - 1) * TYPED_STEP_FRAMES + 1
        assert output.app.invalidate.call_count == steps - 1
        # The row lands once whole; its blank separator comes free in the same frame.
        assert len(handoffs) == 1
        assert transcript.console.file.getvalue().split("\n")[0].rstrip() == sentence
        assert shown(output) == ""
        assert not output.changed.is_set()

    asyncio.run(run())


def test_typed_mode_rolls_code_and_rules_by_row_behind_the_prose_they_follow(monkeypatch):
    async def run():
        handoffs = []
        transcript, output = typed_output(monkeypatch, handoffs)
        transcript.message("Intro line.")
        transcript.message("```text\nCODE_0\nCODE_1\n```")
        transcript.message("---")
        while output.rows or output.pending:
            await output.flush()
            # Code and rules never show up half-typed in the live row.
            assert "CODE" not in shown(output) and "---" not in shown(output)
        text = transcript.console.file.getvalue()
        assert text.index("Intro line.") < text.index("CODE_0") < text.index("CODE_1")
        assert text.index("CODE_1") < text.index("-" * 80)
        # The prose row finishes on the first frame, which still has its
        # one-row budget for code; each later row waits a frame of its own.
        assert len(handoffs) == 3

    asyncio.run(run())


def test_typed_rows_skip_their_indentation():
    from pcode.ui import TYPED_CHARS_PER_STEP, QueuedRow

    output = TerminalOutput(Console(file=StringIO()), SimpleNamespace())
    output.paced = output.typed = True
    output.rows = [QueuedRow(" " * 30 + "A heading" + " " * 30 + "\n", typed=True)]
    assert output.rows[0].lead == 30 and output.rows[0].visible == 39
    # The spaces cost nothing, so the 9-character heading fits in one step.
    assert TYPED_CHARS_PER_STEP >= 9
    assert output._advance() == (1, 0)


def test_typed_backlog_catches_up_and_drain_writes_the_rest_at_once(monkeypatch):
    from pcode.ui import TYPED_DRAIN_STEPS, TYPED_STEP_FRAMES

    async def run():
        handoffs = []
        transcript, output = typed_output(monkeypatch, handoffs)
        transcript.message("\n\n".join("word " * 60 for _ in range(20)))
        frames = 0
        while output.rows or output.pending:
            await output.flush()
            frames += 1
        assert frames <= TYPED_DRAIN_STEPS * TYPED_STEP_FRAMES + 1
        transcript.message("Interrupted by a popup before this sentence types out.")
        await output.flush()
        assert shown(output)
        await output.flush(drain=True)
        assert (
            "Interrupted by a popup before this sentence types out."
            in transcript.console.file.getvalue()
        )
        assert not output.rows and shown(output) == ""

    asyncio.run(run())


def test_replay_mid_typing_writes_the_row_once_and_keeps_queued_notes(monkeypatch):
    async def run():
        handoffs = []
        transcript, output = typed_output(monkeypatch, handoffs)
        transcript.message("A paragraph long enough to still be typing when the replay lands.")
        transcript.note("NOTE_QUEUED")
        await output.flush()
        assert shown(output) and not handoffs
        output.regenerate(transcript.replay)
        await output.flush()
        text = transcript.console.file.getvalue()
        # The replay writes the paragraph whole; the queued rows go, not twice.
        assert text.count("still be typing") == 1
        # The note is not in the retained transcript, but it was never written:
        # the replay carries it rather than dropping it with the queued rows.
        assert text.count("NOTE_QUEUED") == 1
        assert not output.rows and shown(output) == ""
        # Once written, a note is not carried into the next replay.
        output.regenerate(transcript.replay)
        await output.flush()
        assert "NOTE_QUEUED" not in transcript.console.file.getvalue()[len(text) :]

    asyncio.run(run())


def test_replay_during_the_handoff_keeps_a_note_already_rendered(monkeypatch):
    async def run():
        transcript = view()
        terminal = DummyOutput()
        terminal.get_size = lambda: Size(rows=40, columns=80)
        output = TerminalOutput(
            transcript.console, SimpleNamespace(output=CursorSafeOutput(terminal))
        )
        transcript.output = output
        output.paced = True
        replayed = []

        async def handoff(app, **kwargs):
            # A resize settles while the handoff waits for its cursor report.
            if not replayed:
                replayed.append(True)
                output.regenerate(transcript.replay)
            yield Handoff(11)

        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))
        transcript.message("short")
        transcript.note("NOTE_Y")
        while output.rows or output.pending:
            await output.flush()
        assert transcript.console.file.getvalue().count("NOTE_Y") == 1

    asyncio.run(run())


def test_typing_fragments_keep_styles_and_drop_hyperlink_escapes():
    from pcode.ui import QueuedRow

    link = "\x1b]8;id=1;https://example.com\x1b\\"
    row = QueuedRow(f"\x1b[1mBold\x1b[0m {link}link\x1b]8;;\x1b\\ tail      \n", typed=True)
    assert row.visible == len("Bold link tail")
    output = TerminalOutput(Console(file=StringIO()), SimpleNamespace())
    output.rows = [row]
    output._typed = 7
    fragments = output.typing_fragments()
    assert "".join(text for _, text in fragments) == "Bold li"
    assert "bold" in fragments[0][0]
    assert all("\x1b" not in text and "https" not in text for _, text in fragments)


def test_typed_prose_classifies_writes():
    from pcode.thinking_markdown import ThinkingMarkdown
    from pcode.ui import typed_prose

    assert typed_prose((Markdown("A paragraph\n\n- a list"),))
    assert typed_prose((ThinkingMarkdown("Thinking aloud"),))
    assert not typed_prose(())
    assert not typed_prose(("plain",))
    assert not typed_prose((Markdown("```\ncode\n```"),))
    assert not typed_prose((Markdown("| a |\n| - |\n| b |"),))


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
