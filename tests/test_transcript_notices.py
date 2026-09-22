"""Persistent diagnostics are literal renderables; normal tools stay live-only."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import result_detail
from pcode.ui import Transcript


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("color_style", ["palette", "terminal"])
def test_notices_are_literal_compact_and_readable_without_color(theme, color_style):
    stream = StringIO()
    transcript = Transcript(
        Console(file=stream, width=80, color_system=None), theme, color_style=color_style
    )
    transcript.error("[red]**literal**[/red]\nConnection reset", title="Agent failed")
    transcript.warning("Context window nearly full")
    transcript.cancelled()
    transcript.note("Ordinary notice")
    assert "\n".join(line.rstrip() for line in stream.getvalue().splitlines()) + "\n" == (
        "✗ Agent failed\n\n [red]**literal**[/red]\n Connection reset\n\n"
        "! Warning\n  Context window nearly full\n"
        "! Run cancelled\n  Completed tool effects are not undone.\nOrdinary notice\n"
    )


def test_warning_wraps_without_clipping():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=24, color_system=None))
    transcript.warning("A long warning with important final details")
    lines = stream.getvalue().splitlines()
    assert all(len(line) <= 24 for line in lines)
    assert " ".join(line.strip() for line in lines[1:]) == (
        "A long warning with important final details"
    )


@pytest.mark.parametrize("command", ["pytest", "make build", "ruff check .", "mypy ."])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_tool_persistence_is_decided_on_completion(command, exit_code):
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, color_system=None))
    app.present_events((ToolStarted("run_command", command, "call", command=command),))
    assert stream.getvalue() == ""
    detail, failed = result_detail(
        "run_command", {"command": command}, f"[exit code: {exit_code}]", "success"
    )
    app.present_events(
        (
            ToolSummary(
                "run_command",
                detail,
                failed=failed,
                call_id="call",
                command=command,
                error="diagnostic",
            ),
        )
    )
    # The settled call leaves the live panel for a summary line; its output and
    # diagnostic stay hidden while command visibility is off.
    assert app.activity.tools.calls == []
    assert stream.getvalue().startswith("✗ Run" if failed else "✓ Run")
    assert "diagnostic" not in stream.getvalue()
    assert "exit code" not in stream.getvalue()


def test_exceptional_tool_completion_flushes_prose_before_queued_diagnostic():
    import asyncio
    from unittest.mock import MagicMock

    from pcode.preferences import save_preferences
    from pcode.runtime import Message, TextDelta
    from pcode.ui import TerminalOutput

    save_preferences(tool_error_scrollback="on")

    class Runtime:
        session = None

        async def stream(self, prompt):
            yield TextDelta("Before **failure**")
            yield ToolSummary("read_file", "missing.py", failed=True, error="Not found")
            yield TextDelta("After failure")
            yield Message("After failure")

    async def run():
        stream = StringIO()
        app = PreviewApp(
            model="test:local", runtime=Runtime(), console=Console(file=stream, color_system=None)
        )
        output = TerminalOutput(
            app.transcript.console, MagicMock(), rich_theme=lambda: app.transcript.rich_theme
        )
        output.app.output.get_size.return_value.columns = 80
        app.transcript.output = output
        assert await app.run_live(output, "Read missing file")
        assert stream.getvalue() == ""  # Uses the terminal handoff, not console.print.
        await output.flush()
        text = stream.getvalue()
        assert (
            text.index("Before failure") < text.index("✗ Read failed") < text.index("After failure")
        )
        assert text.count("Not found") == 1

    asyncio.run(run())
