import asyncio
from dataclasses import asdict
from io import StringIO

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

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
    content = "[stderr]\n" + "\n".join(f"frame {i}" for i in range(20))
    content += "\nRuntimeError: final cause\n[exit code: 1]"
    error = command_error(content)
    assert error.startswith("… earlier error output truncated\n")
    assert error.endswith("RuntimeError: final cause")
    assert len(error.splitlines()) == 9
    assert "frame 0\n" not in error
    huge = command_error("[stderr]\n" + "x" * 10000 + "END\n[exit code: 1]")
    assert len(huge) < 1700
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
        "  ! Run  pytest -q → exit 1  0.5s",
        "      [stderr] missing module",
        "        traceback context",
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
