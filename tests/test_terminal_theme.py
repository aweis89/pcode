"""Terminal-native Rich colors for both direct and queued/streamed output."""

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


@pytest.mark.parametrize("queued", [False, True])
def test_demo_uses_terminal_colors_after_theme_switches(queued):
    async def run():
        stream = StringIO()
        console = Console(file=stream, width=100, force_terminal=True, color_system="truecolor")
        app = PreviewApp(console=console)
        output = TerminalOutput(
            console,
            app.activity,
            SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None),
            code_theme=lambda: app.transcript.palette.syntax,
        )
        if queued:
            app.transcript.output = output

        original_inline = console.get_style("markdown.code")
        for theme, keyword in (("dark", 94), ("light", 34), ("dark", 94)):
            stream.seek(0)
            stream.truncate()
            app.handle(f"/theme {theme}")
            app.handle("/demo")
            if queued:
                await output.flush()
            text = stream.getvalue()
            assert_terminal_colors(text)
            assert f"\x1b[{keyword}mdef\x1b[0m" in text
            plain = SGR.sub("", text)
            for sample in ("Terminal-native colors", "greet(name)", "Quotes", "Hello", "世界"):
                assert sample in plain
            assert "No files were read or changed" in plain
            assert "```" not in plain
            # Styling must not leak into other users of an injected console.
            assert console.get_style("markdown.code") == original_inline

    asyncio.run(run())


@pytest.mark.parametrize("theme, keyword", [("dark", 94), ("light", 34)])
def test_streamed_demo_uses_same_terminal_styles(theme, keyword):
    async def run():
        stream = StringIO()
        console = Console(file=stream, width=100, force_terminal=True, color_system="truecolor")
        app = PreviewApp(console=console, theme=theme)
        output = TerminalOutput(
            console,
            app.activity,
            SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None),
            code_theme=lambda: app.transcript.palette.syntax,
        )
        for event in PreviewRuntime().demo():
            if isinstance(event, Message):
                for line in event.markdown.splitlines(keepends=True):
                    output.delta(line)
                    await output.flush()
                output.finish()
                await output.flush()
        text = stream.getvalue()
        assert_terminal_colors(text)
        assert f"\x1b[{keyword}mdef\x1b[0m" in text
        assert "greet(name)" in text
        assert "```" not in text

    asyncio.run(run())
