"""Thinking is a saved, visibility-filtered part of normal scrollback."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import Activity, TerminalOutput, Transcript, create_prompt


def rendered(transcript):
    buffer = StringIO()
    console = Console(file=buffer, width=60)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return "\n".join(line.rstrip() for line in buffer.getvalue().split("\n"))


def test_thinking_retained_without_old_tail_limit_and_hidden_by_default():
    transcript = Transcript(Console(file=StringIO()), activity=Activity())
    text = "FIRST_THOUGHT\n" + "reasoning line\n" * 2000 + "LAST_THOUGHT\n"
    transcript.thinking(text)
    assert rendered(transcript) == ""
    transcript.activity.show_thinking = True
    assert rendered(transcript) == text + "\n"
    transcript.activity.show_thinking = False
    assert rendered(transcript) == ""
    assert len(transcript.log.entries) == 1


def test_thinking_is_muted_and_sanitizes_controls():
    transcript = Transcript(Console(file=StringIO()), activity=Activity(show_thinking=True))
    transcript.thinking("hello\n\x1b[31mworld\x1b[0m\r\x00\x1b]0;title\x07\n")
    replay = transcript.replay()
    assert replay[0][0][0].style == "pcode.thinking"
    text = rendered(transcript)
    assert "hello\nworld" in text
    assert "title" not in text
    assert not any(char in text for char in ("\x1b", "\r", "\x00"))


def test_show_thinking_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["show_thinking"].default == "off"
    assert not PreviewApp().activity.show_thinking
    save_preferences(show_thinking="on")
    assert load_preferences()["show_thinking"] == "on"
    assert PreviewApp().activity.show_thinking


def test_show_thinking_command_redraws_and_saves_default(tmp_path, monkeypatch):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pcode.commands import SlashCompleter

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    redraws = []
    monkeypatch.setattr(
        app.transcript, "regenerate", lambda: redraws.append(rendered(app.transcript))
    )
    app.activity.busy = True
    app.transcript.thinking("RETAINED_REASONING\n")
    assert app.registry.dispatch("/show-thinking on")
    assert "RETAINED_REASONING" in redraws[-1]
    assert load_preferences()["show_thinking"] == "on"
    assert app.registry.dispatch("/show-thinking")
    assert len(redraws) == 1
    assert app.registry.dispatch("/show-thinking off")
    assert "RETAINED_REASONING" not in redraws[-1]
    assert load_preferences()["show_thinking"] == "off"
    assert "RETAINED_REASONING" in repr(app.transcript.log.entries)
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/show-thinking invalid")
    completions = SlashCompleter(app.registry).get_completions(
        Document("/show-thinking "), CompleteEvent()
    )
    assert [item.text for item in completions] == ["on", "off"]


def test_meridian_thinking_toggle_explains_upstream_requirement(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    app.model = "meridian:claude-fable-5-1"
    assert app.registry.dispatch("/show-thinking on")
    assert "Thinking Passthrough" in output.getvalue()
    assert "only changes pcode's display" in output.getvalue()


def test_tasks_heading_never_contains_thinking_and_legacy_settings_are_ignored():
    save_preferences(show_thinking="on", thinking_display="expanded", thinking_lines="5")
    app = PreviewApp()
    app.activity.plan = [{"content": "Investigate", "status": "in_progress"}]
    app.transcript.thinking("**Inspecting workspace**\n")
    assert app.activity.panel_heading() == app.activity.panel_title()
    assert "Inspecting workspace" not in app.activity.panel_heading()
    assert "thinking_display" not in load_preferences()
    assert "thinking_lines" not in load_preferences()


def test_streaming_lines_are_retained_once_and_keep_answer_order():
    async def run():
        buffer = StringIO()
        app = PreviewApp(console=Console(file=buffer))
        app.activity.show_thinking = True
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, prompt.app)
            app.transcript.output = writer
            writer.begin_turn("Question")
            writer.thinking_delta("First ")
            writer.thinking_delta("line\nSecond line\n\nPartial")
            await writer.flush()
            assert "First line\nSecond line" in "\n".join(
                line.rstrip() for line in buffer.getvalue().splitlines()
            )
            assert "Partial" not in buffer.getvalue()
            writer.finish_thinking("First line\nSecond line\nPartial")
            writer.delta("Answer\n\n")
            writer.finish("Answer\n\n")
            writer.end_turn()
            await writer.flush()
            text = buffer.getvalue()
            assert text.count("First line") == 1
            assert text.count("Second line") == 1
            assert text.count("Partial") == 1
            assert text.index("Partial") < text.index("Answer")
            assert not writer._thinking_tail
            shown = rendered(app.transcript)
            assert shown.count("First line") == 1
            app.activity.show_thinking = False
            hidden = rendered(app.transcript)
            assert "First line" not in hidden
            assert "Answer" in hidden
            assert "Question" in hidden

    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("color_style", ["palette", "terminal"])
@pytest.mark.parametrize(
    "source",
    [
        "Readable thinking",
        "**Bold** and *italic*",
        "# Heading",
        "- item",
        "```python\nprint(1)\n```",
    ],
)
def test_thinking_style_is_dim_and_theme_aware(theme, color_style, source):
    console = Console(file=StringIO(), force_terminal=True)
    transcript = Transcript(
        console, theme=theme, color_style=color_style, activity=Activity(show_thinking=True)
    )
    transcript.thinking(source)
    with console.use_theme(transcript.rich_theme):
        segments = list(console.render(transcript.replay()[0][0][0]))
    visible = [segment for segment in segments if segment.text.strip()]
    assert visible
    for segment in visible:
        assert segment.style.dim
        if color_style == "palette":
            assert segment.style.color.triplet.hex == transcript.palette.muted
    if source.startswith("**"):
        assert next(segment for segment in visible if "Bold" in segment.text).style.bold


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**Inspecting workspace**", "Inspecting workspace\n\n"),
        ("**Heading**\n\nBody with **inline bold**.\n", "Heading\n\nBody with inline bold.\n\n"),
        ("**First**\n**Second**\n", "First\nSecond\n\n"),
        ("**Unclosed", "**Unclosed\n\n"),
        ("Unopened**", "Unopened**\n\n"),
        ("Plain **inline** text", "Plain inline text\n\n"),
        ("*Single* and `code`", "Single and code\n\n"),
        ("", ""),
    ],
)
def test_thinking_renders_muted_markdown_only_for_display(source, expected):
    buffer = StringIO()
    transcript = Transcript(Console(file=buffer, width=60), activity=Activity(show_thinking=True))
    transcript.thinking(source)
    assert rendered(transcript) == expected
    assert buffer.getvalue() == render_raw(transcript)
    assert transcript.log.entries[0][1] == (source,)


def render_raw(transcript):
    buffer = StringIO()
    console = Console(file=buffer, width=60)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return buffer.getvalue()


def test_streamed_thinking_renders_split_markdown_and_buffers_code_and_lists():
    buffer = StringIO()
    transcript = Transcript(Console(file=buffer, width=60), activity=Activity(show_thinking=True))
    with create_pipe_input() as pipe:
        app = PreviewApp()
        prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
        writer = TerminalOutput(transcript.console, prompt.app)
        writer.commit_thinking = transcript.thinking
        for chunk in ["*", "*First", "*", "*\n\n*", "*Second*", "*\n\n"]:
            writer.thinking_delta(chunk)
        assert rendered(transcript) == "First\n\nSecond\n\n"
        writer.thinking_delta("```python\nprint('hello')\n")
        assert "hello" not in buffer.getvalue()
        writer.thinking_delta("```\n")
        assert "hello" in buffer.getvalue()
        writer.thinking_delta("- one\n\n- two\n")
        assert "one" not in buffer.getvalue()
        writer.finish_thinking("ignored streamed fallback")
        assert "one" in buffer.getvalue()
        assert "two" in buffer.getvalue()
        assert "ignored streamed fallback" not in buffer.getvalue()
        assert buffer.getvalue() == render_raw(transcript)
        assert "First\n\nSecond\n\n" in rendered(transcript)
        writer.finish_thinking("**Fallback**")
        assert rendered(transcript).endswith("Fallback\n\n")
