import asyncio
from dataclasses import asdict
from io import StringIO

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console
from rich.text import Text

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


def test_command_error_redacts_before_truncation_and_removes_controls(monkeypatch):
    marker = "synthetic-command-error-fixture"
    monkeypatch.setenv("EXAMPLE_API_KEY", marker)
    content = (
        "[stderr]\n\x1b[31mFailure\x1b[0m\n"
        f"Authorization: Bearer {marker}\n"
        "client --password 'dummy credential'\n"
        "\x1b]0;window title\x07end\u202e\n[exit code: 1]"
    )
    error = command_error(content)
    assert "Failure" in error
    assert "[redacted]" in error
    assert marker not in error
    assert "dummy credential" not in error
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


def test_error_is_indented_literal_text_and_survives_event_round_trip():
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
    transcript.events((ToolSummary(**asdict(event)),))
    assert stream.getvalue().splitlines() == [
        "✗ Run failed",
        "  pytest -q → exit 1",
        "  [stderr] missing module",
        "    traceback context",
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
            assert event.failed == (mode != "success")
            expected = {
                "exit": "ModuleNotFoundError: example",
                "retry": "Execution blocked: pytest is not allowed",
                "timeout": "[Command timed out after 30.0s]",
                "success": "",
            }[mode]
            assert event.error == expected
            record = next(r for r in saved.recent_transcript() if r["kind"] == "ToolSummary")
            assert record["error"] == expected
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("command", ["", "pytest -q"])
def test_failed_command_summary_uses_semantic_error_color(command):
    stream = StringIO()
    transcript = Transcript(Console(file=stream, force_terminal=True, color_system="truecolor"))
    transcript.events(
        (ToolSummary("run_command", "pytest -q → exit 1", failed=True, command=command),)
    )
    output = stream.getvalue()
    assert "✗ Run failed" in output
    assert "pytest -q" in output
    assert "\x1b[1;31m" in output


@pytest.mark.parametrize("limit", [1, 3, 20, 40])
def test_error_scrollback_limits_wrapped_rows_and_keeps_tail(limit):
    from pcode.preferences import save_preferences

    save_preferences(error_scrollback_lines=str(limit))
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=45, color_system=None))
    transcript.error("prefix " * 400 + "\nfinal cause")
    lines = stream.getvalue().splitlines()
    assert lines[0] == "✗ Error"
    assert len(lines) == limit + 1
    assert "truncated" in lines[1]
    if limit > 1:
        assert lines[-1] == "  final cause"


def test_default_error_scrollback_limit():
    stream = StringIO()
    Transcript(Console(file=stream)).error("\n".join(str(i) for i in range(60)))
    assert len(stream.getvalue().splitlines()) == 21
    assert stream.getvalue().splitlines()[-1] == "  59"


def test_hidden_errors_do_not_hide_warnings_or_change_events():
    from pcode.preferences import save_preferences

    save_preferences(error_scrollback="off")
    stream = StringIO()
    transcript = Transcript(Console(file=stream))
    event = ToolSummary("run_command", "exit 1", failed=True, error="saved diagnostic")
    before = asdict(event)
    transcript.error("runtime failure")
    transcript.events((event,))
    assert stream.getvalue() == ""
    assert asdict(event) == before
    transcript.warning("still visible")
    transcript.cancelled()
    assert "still visible" in stream.getvalue()
    assert "Run cancelled" in stream.getvalue()


def test_error_body_uses_rich_highlighting_without_markup():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, force_terminal=True, color_system="truecolor"))
    transcript.error("[bold]literal[/bold] code=123 False")
    body = stream.getvalue().splitlines()[1]
    assert "[bold]" in Text.from_ansi(body).plain
    assert "[/bold]" in Text.from_ansi(body).plain
    assert "\x1b[" in body
