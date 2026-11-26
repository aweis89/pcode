import asyncio
import re
from dataclasses import asdict
from io import StringIO

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console
from rich.text import Text

from pcode.command_transcript import SEPARATOR
from pcode.live import AgentRuntime
from pcode.runtime import ToolSummary
from pcode.sessions import SavedSession
from pcode.tool_display import command_error
from pcode.ui import Transcript


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            "[stdout]\nprogress\n[stderr]\nerror: missing module\n[exit code: 1]",
            "error: missing module",
        ),
        ("[stdout]\nFAILED tests/test_example.py\n[exit code: 1]", "FAILED tests/test_example.py"),
        ("[stdout]\nFAILED test\n[stderr]\n\n[exit code: 1]", "FAILED test"),
        (
            "[stderr]\nzsh: command not found: example\n[exit code: 127]",
            "zsh: command not found: example",
        ),
        ("[Command timed out after 30.0s]", "[Command timed out after 30.0s]"),
        ("Execution blocked by command policy", "Execution blocked by command policy"),
        ("(no output)\n[exit code: 2]", "No error output returned."),
        ([{"msg": "Expected a string", "input": "not for display"}], "Expected a string"),
    ],
)
def test_command_error_prefers_stderr_and_falls_back_to_stdout(content, expected):
    assert command_error(content) == expected


def test_command_error_removes_controls_and_keeps_text():
    marker = "synthetic-command-error-fixture"
    content = (
        "[stderr]\n\x1b[31mFailure\x1b[0m\n"
        f"Authorization: Bearer {marker}\n"
        "client --password 'dummy credential'\n"
        "\x1b]0;window title\x07end\u202e\n[exit code: 1]"
    )
    error = command_error(content)
    assert "Failure" in error
    assert f"Bearer {marker}" in error
    assert "client --password 'dummy credential'" in error
    assert "window title" not in error
    assert "\x1b" not in error
    assert "\u202e" not in error
    assert "[31m" not in error


def test_command_error_keeps_bounded_tail_with_notice():
    content = "[stderr]\n" + "\n".join(f"frame {i}" for i in range(220))
    content += "\nRuntimeError: final cause\n[exit code: 1]"
    error = command_error(content)
    assert error.startswith("… earlier error output truncated\n")
    assert error.endswith("RuntimeError: final cause")
    assert len(error.splitlines()) == 201
    assert "frame 0\n" not in error
    huge = command_error("[stderr]\n" + "x" * 40000 + "END\n[exit code: 1]")
    assert len(huge) < 32100
    assert huge.endswith("END")


def test_command_diagnostic_survives_event_round_trip_and_uses_output_fallback():
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        True,
        "call-1",
        0.5,
        "[stderr] missing module\n  traceback context",
    )
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=100, color_system=None))
    transcript.command_scrollback = True
    transcript.tool_error_scrollback = True
    transcript.events((ToolSummary(**asdict(event)),))
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "✗ Run shell · 0.5s " + "─" * 81,
        "  pytest -q → exit 1",
        "  " + SEPARATOR,
        "  [stderr] missing module",
        "    traceback context",
        "─" * 100,
    ]
    assert ToolSummary(**{"name": "run_command", "detail": "old summary"}).error == ""


@pytest.mark.parametrize("mode", ["exit", "retry", "success", "timeout"])
def test_command_errors_reach_live_events_and_saved_transcript(tmp_path, mode):
    async def run():
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {0: DeltaToolCall(name="run_command", json_args='{"command":"pytest -q"}')}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def run_command(command: str) -> str:
            if mode == "retry":
                raise ModelRetry("Execution blocked: pytest is not allowed")
            if mode == "timeout":
                return "[Command timed out after 30.0s]"
            output = "[stderr]\nModuleNotFoundError: example"
            return output + ("\n[exit code: 1]" if mode == "exit" else "")

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(agent, saved)
        try:
            events = [event async for event in runtime.stream("Run tests")]
            event = next(e for e in events if isinstance(e, ToolSummary))
            # A non-zero exit ran: it keeps its diagnostic but is not a failure.
            assert event.failed == (mode in {"retry", "timeout"})
            assert event.exit_code == (1 if mode == "exit" else None)
            expected = {
                "exit": "ModuleNotFoundError: example",
                "retry": "Execution blocked: pytest is not allowed",
                "timeout": "[Command timed out after 30.0s]",
                "success": "",
            }[mode]
            assert event.error == expected
            record = next(r for r in saved.transcript_records() if r["kind"] == "ToolSummary")
            assert record["error"] == expected
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("command", ["", "pytest -q"])
def test_failed_command_block_keeps_the_ordinary_title_color(command):
    """A non-zero exit is routine: it is marked, not coloured like a crash."""
    from pcode.preferences import save_preferences

    save_preferences(show_commands="on", tool_error_scrollback="on")

    def render(failed: bool) -> str:
        stream = StringIO()
        transcript = Transcript(Console(file=stream, force_terminal=True, color_system="truecolor"))
        transcript.events(
            (ToolSummary("run_command", "pytest -q → exit 1", failed=failed, command=command),)
        )
        return stream.getvalue()

    output = render(True)
    assert "✗ Run shell" in output and "failed" not in Text.from_ansi(output).plain
    assert "pytest -q" in Text.from_ansi(output).plain
    assert "\x1b[1;31m" not in output
    codes = re.compile(r"\x1b\[[0-9;]*m")
    assert codes.findall(output) == codes.findall(render(False))


@pytest.mark.parametrize("limit", [1, 3, 20, 40])
def test_error_scrollback_limits_wrapped_rows_and_keeps_tail(limit):
    from pcode.preferences import save_preferences

    save_preferences(error_scrollback_lines=str(limit))
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=45, color_system=None))
    transcript.error("prefix " * 400 + "\nfinal cause", title="Agent failed")
    lines = stream.getvalue().splitlines()
    assert lines[0] == "▎ Agent failed"
    assert all(line.startswith("▎ ") for line in lines)
    assert len(lines) == limit + 1  # The headline, then the bounded detail.
    assert "truncated" in lines[1]
    if limit > 1:
        assert lines[-1] == "▎ final cause"


def test_default_error_scrollback_limit():
    stream = StringIO()
    Transcript(Console(file=stream)).error("\n".join(str(i) for i in range(60)))
    # The first line is the headline; the remaining 59 are bounded to 20.
    assert len(stream.getvalue().splitlines()) == 21
    assert stream.getvalue().splitlines()[-1].strip() == "▎ 59"


@pytest.mark.parametrize("name", ["read_file", "write_plan", "edit_file"])
def test_tool_failures_keep_a_marked_summary_line_without_their_diagnostic(name):
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    event = ToolSummary(name, "missing.py", failed=True, error="Not found")
    assert transcript.writes_tool_result(event)
    transcript.tool_result(event)
    output = stream.getvalue()
    assert "✗ " in output and "✓" not in output
    assert "missing.py" in output
    assert "Not found" not in output


@pytest.mark.parametrize("name", ["read_file", "write_plan", "edit_file"])
def test_tool_error_scrollback_restores_full_diagnostics(name):
    from pcode.preferences import save_preferences

    save_preferences(tool_error_scrollback="on")
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    event = ToolSummary(name, "missing.py", failed=True, error="Not found")
    assert transcript.writes_tool_result(event)
    transcript.tool_result(event)
    assert "failed" in stream.getvalue()
    assert "Not found" in stream.getvalue()


def test_failed_commands_need_mirroring_before_the_failure_option_applies():
    from pcode.preferences import save_preferences

    save_preferences(tool_error_scrollback="on")
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command", "pytest → exit 1", failed=True, command="pytest", error="DIAGNOSTIC"
    )
    assert transcript.writes_tool_result(event)
    transcript.tool_result(event)
    # Without mirroring, a failure keeps its summary line and nothing else.
    assert "✗ Run shell" in stream.getvalue()
    assert "DIAGNOSTIC" not in stream.getvalue()


def test_mirrored_command_failures_keep_only_their_summary_line_by_default():
    from pcode.preferences import save_preferences

    save_preferences(show_commands="on")
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command",
        "pytest → exit 1",
        failed=True,
        command="pytest",
        result="[stderr]\nModuleNotFoundError\n[exit code: 1]",
        error="ModuleNotFoundError",
    )
    transcript.tool_result(event)
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "✗ Run shell · exit 1 · pytest",
    ]


def test_legacy_error_visibility_is_ignored_and_hidden_commands_keep_events():

    from pcode.preferences import preferences_path

    preferences_path().parent.mkdir(parents=True, exist_ok=True)
    preferences_path().write_text('{"error_scrollback": "off"}')
    stream = StringIO()
    transcript = Transcript(Console(file=stream))
    event = ToolSummary("run_command", "exit 1", failed=True, error="saved diagnostic")
    before = asdict(event)
    transcript.error("runtime failure")
    transcript.events((event,))
    assert "runtime failure" in stream.getvalue()
    assert "saved diagnostic" not in stream.getvalue()
    assert asdict(event) == before
    transcript.warning("still visible")
    transcript.cancelled()
    assert "still visible" in stream.getvalue()
    assert "Run cancelled" in stream.getvalue()


def test_error_is_a_red_bar_block_with_a_muted_literal_body():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, force_terminal=True, color_system="truecolor"))
    transcript.error("[bold]literal[/bold] failed. Run it again\n    indented")
    rows = [Text.from_ansi(row) for row in stream.getvalue().splitlines()]
    assert [row.plain for row in rows] == [
        "▎ [bold]literal[/bold] failed",
        "▎ Run it again",
        "▎     indented",
    ]

    def colour(row, at):
        # Standard colour 1 is red, the error bar and headline.
        return row.get_style_at_offset(transcript.console, at).color.number

    assert all(colour(row, 0) == 1 for row in rows)
    assert colour(rows[0], 2) == 1
    assert colour(rows[1], 2) != 1


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 10, 30])
def test_error_block_handles_narrow_panes(width):
    from pcode.preferences import save_preferences

    save_preferences(error_scrollback_lines="3")
    stream = StringIO()
    Transcript(Console(file=stream, width=width, color_system=None)).error(
        "long log " * 100, title="Agent failed"
    )
    lines = stream.getvalue().splitlines()
    assert all(len(line) <= width for line in lines)
    # The headline can wrap; the body stays bounded.
    assert len(lines) <= len("Agent failed") + 3


def test_untitled_error_headline_without_a_sentence_break_is_bounded_too():
    from pcode.preferences import save_preferences

    save_preferences(error_scrollback_lines="3")
    stream = StringIO()
    Transcript(Console(file=stream, width=40, color_system=None)).error('{"body": "' + "x" * 300)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert lines[-1].endswith("…") and all(len(line) <= 40 for line in lines)


def test_titled_error_drops_blank_edge_lines_and_keeps_indentation():
    stream = StringIO()
    Transcript(Console(file=stream, color_system=None)).error(
        "  \n\n    indented\n\n  \n", title="Agent failed"
    )
    assert stream.getvalue().splitlines() == ["▎ Agent failed", "▎     indented"]
