"""Model text is data, not terminal instructions, on every presentation path."""

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from pcode.runtime import Message, TextDelta
from pcode.stream_display import PrintedReply
from pcode.ui import CursorSafeOutput, TerminalOutput, Transcript

PAYLOADS = [
    pytest.param("\x1b[2J\x1b[3J", id="erase-screen-and-history"),
    pytest.param("\x1b[1;1H", id="move-cursor"),
    pytest.param("\x1b]52;c;VEVTVA==\x07", id="clipboard-bell"),
    pytest.param("\x1b]0;spoofed-title\x1b\\", id="title-st"),
    pytest.param("\x1bPignored\x1b\\", id="device-control"),
    pytest.param("\x1b[31mRED\x1b[0m", id="untrusted-color"),
    pytest.param("\x9b2J\x9d0;title\x9c", id="c1-controls"),
    pytest.param("\r\x00\x08\x07\x7f", id="c0-controls"),
]


def assert_no_controls(text):
    assert not any(ord(char) < 32 and char != "\n" or 127 <= ord(char) < 160 for char in text)


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize(
    "route",
    ["stream", "fallback", "direct", "thinking", "transcript", "markdown", "restore"],
)
def test_terminal_controls_are_inert_in_model_text(payload, route):
    async def run():
        stream = StringIO()
        console = Console(file=stream, color_system=None, width=100)
        app = SimpleNamespace(output=CursorSafeOutput(DummyOutput()), invalidate=lambda: None)
        output = TerminalOutput(console, app)
        transcript = Transcript(console)
        source = f"BEFORE{payload}AFTER\n\nLAST"
        if route == "stream":
            # Even an escape split across provider chunks must stay inert.
            for char in source:
                output.delta(char)
            output.finish()
        elif route == "fallback":
            output.finish(source)
        elif route == "direct":
            output.message(source)
        elif route == "thinking":
            for char in source:
                output.thinking_delta(char)
            output.finish_thinking()
        elif route == "transcript":
            transcript.events([Message(source)])
        elif route == "markdown":
            transcript.print(Markdown(source))
        else:
            # A saved response hasn't passed through TerminalOutput.delta.
            with transcript.restore():
                transcript.events([Message(source)])
        await output.flush()
        shown = stream.getvalue()
        assert_no_controls(shown)
        assert all(word in shown for word in ("BEFORE", "AFTER", "LAST"))
        # Only these routes retain history; the standalone output has no log.
        if route not in ("transcript", "markdown", "restore"):
            return
        # Resize / visibility changes re-render the retained entries.
        for width in (40, 100):
            replay = StringIO()
            renderer = Console(file=replay, color_system=None, width=width)
            for objects, end, soft_wrap in transcript.replay():
                renderer.print(*objects, end=end, soft_wrap=soft_wrap)
            shown = replay.getvalue()
            assert_no_controls(shown)
            assert all(word in shown for word in ("BEFORE", "AFTER", "LAST"))

    asyncio.run(run())


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("mode", ["stream", "interrupted", "message"])
def test_print_mode_sanitizes_terminal_but_preserves_piped_source(payload, terminal, mode):
    stream = StringIO()
    transcript = Transcript(Console(file=StringIO(), color_system=None))
    reply = PrintedReply(stream, transcript=transcript, present=lambda events: None)
    if terminal:
        reply.console = Console(file=stream, force_terminal=True, color_system=None, width=100)
    source = f"BEFORE{payload}AFTER"
    if mode != "message":
        for char in source:
            reply.event(TextDelta(char))
    if mode == "interrupted":
        reply.settle()  # A failed/interrupted turn still prints its last block.
    else:
        reply.event(Message(source))
    shown = stream.getvalue()
    if terminal:
        assert_no_controls(shown)
        assert "BEFORE" in shown and "AFTER" in shown
    else:
        assert shown == source + "\n\n"


def test_markdown_formatting_and_literal_escape_examples_survive():
    stream = StringIO()
    transcript = Transcript(
        Console(file=stream, force_terminal=True, color_system="truecolor", width=80)
    )
    transcript.message("**BOLD**\n\n```python\nprint(r'\\x1b[2J')\n```\n\n界e\u0301🙂")
    shown = stream.getvalue()
    assert "\x1b[1mBOLD\x1b[0m" in shown  # Trusted Rich styling is still emitted.
    assert "\\x1b[2J" in Text.from_ansi(shown).plain
    assert "\x1b[2J" not in shown
    assert "界e\u0301🙂" in shown
