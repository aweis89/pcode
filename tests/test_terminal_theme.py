"""Selectable Rich colors for both direct and queued/streamed output."""

import asyncio
import re
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, PreviewRuntime
from pcode.ui import CursorSafeOutput, TerminalOutput

SGR = re.compile(r"\x1b\[([\d;]*)m")


def assert_terminal_colors(text):
    codes = {int(code) for match in SGR.findall(text) for code in match.split(";") if code}
    assert codes & set(range(30, 38)), "Expected ANSI foreground colors"
    assert not codes & {38, 48}, "RGB/indexed colors bypass the terminal palette"
    assert not codes & (set(range(40, 48)) | set(range(100, 108))), "Painted background"


def assert_colors(text, color_style, keyword, transcript):
    if color_style == "terminal":
        assert_terminal_colors(text)
        assert f"\x1b[{keyword}mdef\x1b[0m" in text
    else:
        assert "38;2;" in text
        assert "48;2;" in text
        assert transcript.code_theme == ("nord" if transcript.theme == "dark" else "friendly")
        inline = transcript.rich_theme.styles["markdown.code"]
        assert inline.color.name == transcript.palette.foreground
        assert inline.bgcolor.name == transcript.palette.surface
        assert transcript.rich_theme.styles["markdown.link"].color.name == transcript.palette.accent


@pytest.mark.parametrize("color_style", ["palette", "terminal"])
@pytest.mark.parametrize("queued", [False, True])
def test_demo_colors_after_theme_and_style_switches(queued, color_style):
    async def run():
        stream = StringIO()
        console = Console(file=stream, width=100, force_terminal=True, color_system="truecolor")
        app = PreviewApp(console=console)
        output = TerminalOutput(
            console,
            SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None),
            code_theme=lambda: app.transcript.code_theme,
            rich_theme=lambda: app.transcript.rich_theme,
        )
        if queued:
            app.transcript.output = output

        original_inline = console.get_style("markdown.code")
        for theme, keyword in (("dark", 94), ("light", 34), ("dark", 94)):
            stream.seek(0)
            stream.truncate()
            app.handle(f"/theme {theme}")
            # Exercise changing color styles on the same console, not just startup.
            app.handle("/colors terminal" if color_style == "palette" else "/colors palette")
            app.handle(f"/colors {color_style}")
            if queued:
                await output.flush()
            stream.seek(0)
            stream.truncate()
            app.handle("/demo")
            if queued:
                await output.flush()
            text = stream.getvalue()
            assert_colors(text, color_style, keyword, app.transcript)
            plain = SGR.sub("", text)
            for sample in ("Theme preview", "greet(name)", "Quotes", "Hello", "世界"):
                assert sample in plain
            assert "No files were read or changed" in plain
            assert "```" not in plain
            # Styling must not leak into other users of an injected console.
            assert console.get_style("markdown.code") == original_inline

    asyncio.run(run())


@pytest.mark.parametrize("color_style", ["palette", "terminal"])
@pytest.mark.parametrize("theme, keyword", [("dark", 94), ("light", 34)])
def test_streamed_demo_uses_selected_styles(theme, keyword, color_style):
    async def run():
        stream = StringIO()
        console = Console(file=stream, width=100, force_terminal=True, color_system="truecolor")
        app = PreviewApp(console=console, theme=theme, color_style=color_style)
        output = TerminalOutput(
            console,
            SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None),
            code_theme=lambda: app.transcript.code_theme,
            rich_theme=lambda: app.transcript.rich_theme,
        )
        for event in PreviewRuntime().demo():
            if isinstance(event, Message):
                for line in event.markdown.splitlines(keepends=True):
                    output.delta(line)
                    await output.flush()
                output.finish()
                await output.flush()
        text = stream.getvalue()
        assert_colors(text, color_style, keyword, app.transcript)
        assert "greet(name)" in text
        assert "```" not in text

    asyncio.run(run())


def test_default_palette_and_color_command_validation():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, color_system=None))
    assert app.transcript.color_style == "palette"
    assert app.transcript.code_theme == "nord"
    app.handle("/colors terminal")
    app.handle("/theme light")
    assert app.transcript.code_theme == "ansi_light"
    app.handle("/colors invalid")
    assert app.transcript.color_style == "terminal"
    assert "Usage: /colors [palette|terminal]" in stream.getvalue()
    app.handle("/colors")
    assert app.transcript.color_style == "terminal"
    app.handle("/colors palette")
    assert app.transcript.code_theme == "friendly"
    assert app.transcript.palette.rich_theme() is app.transcript.rich_theme
    # Palette themes do not set an overall foreground/background for prose.
    for name in ("markdown.text", "markdown.paragraph"):
        style = app.transcript.rich_theme.styles[name]
        assert style.color is None and style.bgcolor is None
