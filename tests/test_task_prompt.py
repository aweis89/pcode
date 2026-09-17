"""Submitted prompts are colored quotes, not interpreted Markdown."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.task_prompt import TaskPrompt
from pcode.ui import Transcript


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("color_style", ["palette", "terminal"])
def test_prompt_uses_current_accent(theme, color_style):
    console = Console(file=StringIO(), width=80)
    transcript = Transcript(console, theme=theme, color_style=color_style)
    with console.use_theme(transcript.rich_theme):
        segments = list(console.render(TaskPrompt("literal **text**")))
        accent = console.get_style("pcode.accent")
    assert all(segment.style == accent for segment in segments if segment.text.strip())


def test_prompt_preserves_literal_text_and_blank_lines():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.user("[red] **bold** `code`\n\n> quote")
    assert stream.getvalue() == "\n▌ [red] **bold** `code`\n▌ \n▌ > quote\n"


def test_prompt_rail_repeats_on_wrapped_lines():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=8, color_system=None))
    transcript.user("abcdefghijkl")
    assert stream.getvalue() == "\n▌ abcdef\n▌ ghijkl\n"


@pytest.mark.parametrize("width", [1, 2])
def test_tiny_terminal_prioritizes_text(width):
    stream = StringIO()
    Transcript(Console(file=stream, width=width, color_system=None)).user("abc")
    assert stream.getvalue().replace("\n", "") == "abc"


def test_prompt_has_blank_line_after_repository_instructions():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.note("Loaded repository instructions: AGENTS.md")
    transcript.user("submitted prompt")
    assert stream.getvalue() == (
        "Loaded repository instructions: AGENTS.md\n\n▌ submitted prompt\n"
    )


def test_live_submission_does_not_echo_quote_before_response():
    from pcode.app import PreviewApp

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=object(), console=Console(file=stream))
    assert app.handle("waiting prompt")
    assert stream.getvalue() == ""
