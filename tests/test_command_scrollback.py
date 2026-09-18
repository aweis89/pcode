import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.config import configure
from pcode.preferences import load_preferences, save_preferences
from pcode.runtime import ToolSummary
from pcode.ui import Transcript, create_prompt


def transcript(width=80):
    stream = StringIO()
    return Transcript(Console(file=stream, width=width, color_system=None)), stream


def test_command_mirroring_is_off_by_default():
    view, stream = transcript()
    event = ToolSummary("run_command", "pytest -q → exit 0", command="pytest -q", result="2 passed")
    assert view.command_output(event) is False
    assert view.streams_command(event) is False
    assert stream.getvalue() == ""


def test_enabled_option_mirrors_command_and_output():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 0",
        call_id="one",
        elapsed_seconds=0.25,
        command="pytest -q",
        result="[stdout]\n2 passed\n[exit code: 0]",
    )
    assert view.command_output(event) is True
    lines = [line.rstrip() for line in stream.getvalue().splitlines()]
    assert lines[0] == lines[-1] == "─" * 80
    assert lines[1] == "✓ Run · 0.2s"
    assert "  $ pytest -q" in lines
    assert "  2 passed" in lines
    assert "  [exit code: 0]" in lines


def test_mirroring_covers_process_tools_and_empty_output():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    assert view.command_output(ToolSummary("check_command", "process → running", result="")) is True
    assert "(no output)" in stream.getvalue()
    assert "✓ Check" in stream.getvalue()


def test_mirroring_ignores_tools_without_commands():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    event = ToolSummary("read_file", "a.py · 2 lines", result="file body")
    assert view.command_output(event) is False
    assert stream.getvalue() == ""


def test_failed_commands_report_failure_in_the_mirrored_block():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        command="pytest -q",
        result="[stderr]\nModuleNotFoundError: example\n[exit code: 1]",
        error="ModuleNotFoundError: example",
    )
    assert view.command_output(event) is True
    assert "✗ Run failed" in stream.getvalue()
    assert "ModuleNotFoundError: example" in stream.getvalue()


def test_mirrored_failure_replaces_the_error_excerpt_block():
    save_preferences(command_scrollback="on")
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        call_id="one",
        command="pytest -q",
        result="[stdout]\nkept context\n[exit code: 1]",
        error="FAILED test_example",
    )
    app.present_events((event,))
    output = stream.getvalue()
    assert "✗ Run failed" in output
    assert output.count("✗ Run failed") == 1
    assert "kept context" in output
    # The pinned tool panel keeps its own record regardless of scrollback.
    assert app.activity.tools.calls[0].event.error == event.error


def test_disabled_option_keeps_successful_commands_out_of_scrollback():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    app.present_events(
        (ToolSummary("run_command", "pytest -q → exit 0", command="pytest -q", result="2 passed"),)
    )
    assert stream.getvalue() == ""


@pytest.mark.parametrize("limit", [1, 3, 20])
def test_command_scrollback_lines_bounds_rows_and_keeps_tail(limit):
    save_preferences(command_scrollback="on", command_scrollback_lines=str(limit))
    view, stream = transcript(width=45)
    view.command_output(
        ToolSummary(
            "run_command",
            "noisy → exit 0",
            command="noisy",
            result="\n".join(f"line {i}" for i in range(60)) + "\nfinal line",
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == lines[-1] == "─" * 45
    assert lines[1] == "✓ Run"
    assert len(lines) == limit + 5  # Borders, heading, command, and omission marker.
    assert lines[2].strip() == "$ noisy"
    assert f"{61 - limit} earlier output rows omitted" in lines[3]
    assert "earlier error output" not in lines[3]
    assert lines[-2].strip() == "final line"


def test_settings_round_trip_through_config():
    configure(["set", "command_scrollback", "on"])
    configure(["set", "command_scrollback_lines", "120"])
    assert load_preferences()["command_scrollback"] == "on"
    assert load_preferences()["command_scrollback_lines"] == "120"
    assert configure(["get", "command_scrollback"]) == "on"
    configure(["unset", "command_scrollback"])
    configure(["unset", "command_scrollback_lines"])
    assert configure(["get", "command_scrollback"]) == "off"
    assert configure(["get", "command_scrollback_lines"]) == "20"
    with pytest.raises(ValueError):
        configure(["set", "command_scrollback", "yes"])
    with pytest.raises(ValueError):
        configure(["set", "command_scrollback_lines", "0"])


def test_slash_command_toggles_state_and_saves_the_default():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    assert app.registry.dispatch("/show-commands")
    assert "Command output in scrollback: off" in stream.getvalue()
    assert app.registry.dispatch("/show-commands on")
    assert app.transcript.command_scrollback is True
    assert load_preferences()["command_scrollback"] == "on"
    assert "Command output in scrollback: on. Usage: /show-commands on|off (Ctrl+G)" in (
        stream.getvalue()
    )
    assert app.registry.dispatch("/show-commands off")
    assert app.transcript.command_scrollback is False
    assert load_preferences()["command_scrollback"] == "off"
    with pytest.raises(ValueError, match=r"Usage: /show-commands"):
        app.registry.dispatch("/show-commands yes")


@pytest.mark.parametrize("vi_mode", [False, True])
def test_ctrl_g_toggles_mirroring_without_starting_a_search_or_inserting_text(vi_mode):
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                on_commands=app.toggle_command_scrollback,
                vi_mode=vi_mode,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("\x07\x07\x07draft\r")
            return await asyncio.wait_for(prompt.prompt_async(), timeout=3)

    assert asyncio.run(run()) == "draft"
    assert app.transcript.command_scrollback is True
    assert load_preferences()["command_scrollback"] == "on"
    notes = [line for line in stream.getvalue().splitlines() if "Command output" in line]
    assert [note.split(": ")[1].split(".")[0] for note in notes] == ["on", "off", "on"]


def test_toggle_survives_an_unwritable_preferences_file(monkeypatch):
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))

    def fail(**updates):
        raise OSError("read-only configuration directory")

    monkeypatch.setattr("pcode.app.save_preferences", fail)
    app.toggle_command_scrollback()
    assert app.transcript.command_scrollback is True
    assert "Could not save defaults" in stream.getvalue()
    assert "Command output in scrollback: on" in stream.getvalue()


def test_toggled_mirroring_takes_effect_on_the_next_settled_command():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command", "echo → exit 0", call_id="one", command="echo hi", result="hi"
    )
    app.present_events((event,))
    assert "✓ Run" not in stream.getvalue()
    app.toggle_command_scrollback()
    app.present_events((event,))
    assert "✓ Run" in stream.getvalue()
    assert "$ echo hi" in stream.getvalue()


def test_mirrored_output_is_sanitized_and_literal():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command",
            "client → exit 0",
            command="client --token 'dummy credential'",
            result="[bold]literal[/bold]\n\x1b[31mred\x1b[0m\u202e",
        )
    )
    output = stream.getvalue()
    assert "dummy credential" not in output
    assert "[redacted]" in output
    assert "[bold]literal[/bold]" in output
    assert "\x1b" not in output
    assert "\u202e" not in output


@pytest.mark.parametrize("width", [1, 2, 3, 12, 45, 80])
def test_command_block_wraps_without_losing_command_or_literal_indentation(width):
    from rich.cells import cell_len

    save_preferences(command_scrollback="on", command_scrollback_lines="3")
    view, stream = transcript(width=width)
    view.command_output(
        ToolSummary(
            "run_command",
            "run",
            command="printf '界 hello'\necho done",
            result="    [bold]literal[/bold]\n```\n# heading\n" + "tail " * 40,
        )
    )
    lines = stream.getvalue().splitlines()
    assert all(cell_len(line) <= width for line in lines)
    joined = "".join(line.strip() for line in lines)
    assert "printf" in joined and "echodone" in joined.replace(" ", "")
    if width == 80:
        assert "earlier output rows omitted" in stream.getvalue()


def test_output_keeps_leading_indentation_and_markdown_literal_without_padding():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command", "run", command="echo hi", result="    # heading\n```\n[bold]x[/bold]"
        )
    )
    assert stream.getvalue().splitlines() == [
        "─" * 80,
        "✓ Run",
        "  $ echo hi",
        "      # heading",
        "  ```",
        "  [bold]x[/bold]",
        "─" * 80,
    ]


def test_process_details_are_not_presented_as_shell_source():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    view.command_output(ToolSummary("check_command", "process → running", result="still running"))
    assert "$" not in stream.getvalue()
    assert "process → running" in stream.getvalue()


def test_command_replay_uses_current_theme_and_output_budget():
    from pcode.command_transcript import CommandTranscript

    save_preferences(command_scrollback="on")
    view, _ = transcript()
    view.tool_result(ToolSummary("run_command", "run", command="echo hi", result="one\ntwo\nthree"))
    view.theme = "light"
    view.command_scrollback_lines = 1
    blocks = [obj for objects, _, _ in view.replay() for obj in objects]
    block = next(obj for obj in blocks if isinstance(obj, CommandTranscript))
    assert block.code_theme == view.code_theme
    assert block.max_lines == 1
    assert block.command == "echo hi"


def test_command_highlighting_and_failure_color_do_not_style_output_as_code():
    from rich.text import Text

    from pcode.command_transcript import CommandTranscript

    view, _ = transcript()
    block = CommandTranscript("echo '$HOME'", "[bold]literal[/bold]", "Run", failed=True)
    with view.console.use_theme(view.rich_theme):
        parts = list(block.__rich_console__(view.console, view.console.options))
        assert isinstance(parts[1], Text)
        assert parts[1].style == "pcode.error"
        segments = list(view.console.render(block))
    command_segments = [segment for segment in segments if "$HOME" in segment.text]
    assert command_segments and command_segments[0].style.color is not None
    output_segments = [segment for segment in segments if "[bold]literal[/bold]" in segment.text]
    assert output_segments and not output_segments[0].style


def test_default_budget_retains_last_twenty_output_rows():
    save_preferences(command_scrollback="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command",
            "run",
            command="noisy",
            result="\n".join(f"row {i}" for i in range(50)),
        )
    )
    lines = stream.getvalue().splitlines()
    assert view.command_scrollback_lines == 20
    assert "30 earlier output rows omitted" in lines[3]
    assert [line.strip() for line in lines[4:-1]] == [f"row {i}" for i in range(30, 50)]


@pytest.mark.parametrize("name", ["run_command", "start_command", "check_command", "stop_command"])
@pytest.mark.parametrize("failed", [False, True])
def test_command_visibility_controls_all_completions(name, failed):
    view, stream = transcript()
    event = ToolSummary(name, "command detail", failed=failed, result="OUTPUT", error="DIAGNOSTIC")
    view.tool_result(event)
    view.events((event,))
    assert stream.getvalue() == ""
    view.command_scrollback = True
    view.tool_result(event)
    assert stream.getvalue().count("OUTPUT") == 1
    assert "DIAGNOSTIC" not in stream.getvalue()


@pytest.mark.parametrize("limit", ["1", "10", "80"])
def test_preview_height_setting_is_independent_of_scrollback_limit(limit):
    configure(["set", "command_preview_lines", limit])
    view, _ = transcript()
    assert view.command_preview_lines == int(limit)
    assert view.command_scrollback_lines == 20
    assert not view.command_scrollback
    configure(["unset", "command_preview_lines"])
    assert configure(["get", "command_preview_lines"]) == "10"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc"])
def test_preview_height_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        configure(["set", "command_preview_lines", value])
