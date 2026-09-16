"""Append-only output and partial-line rendering without a real terminal."""

import asyncio
from io import StringIO
from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.ui import Activity, TerminalOutput


def make_output(width=80):
    stream = StringIO()
    terminal = DummyOutput()
    terminal.get_size = lambda: Size(rows=24, columns=width)
    app = SimpleNamespace(output=terminal, invalidate=lambda: None)
    output = TerminalOutput(Console(file=stream, color_system=None), Activity(), app)
    return output, stream


def test_complete_lines_commit_once_and_final_message_is_not_reprinted():
    async def run():
        output, stream = make_output()
        output.delta("**hello**\npartial")
        await output.flush()
        assert stream.getvalue() == "**hello**\n"
        assert output.activity.text == "partial"
        output.delta(" answer")
        await output.flush()
        assert stream.getvalue() == "**hello**\n"
        assert output.activity.text == "partial answer"
        output.finish("**hello**\npartial answer")
        await output.flush()
        assert stream.getvalue() == "**hello**\npartial answer\n\n"
        assert output.activity.text == ""
        output.finish()
        await output.flush()
        assert stream.getvalue().count("partial answer") == 1

    asyncio.run(run())


def test_wrapped_unicode_line_is_bounded_without_losing_text():
    async def run():
        output, stream = make_output(width=8)
        text = "界e\u0301🙂abc" * 20
        for char in text:
            output.delta(char)
            await output.flush()
        output.finish()
        await output.flush()
        assert stream.getvalue().replace("\n", "") == text
        assert len(stream.getvalue().splitlines()) > 10

    asyncio.run(run())


def test_fallback_messages_tools_and_empty_completion_stay_ordered():
    async def run():
        output, stream = make_output()
        output.finish("No deltas")
        output.print("tool finished")
        output.delta("next\n")
        output.finish("next\n")
        await output.flush()
        assert stream.getvalue() == "No deltas\n\ntool finished\nnext\n\n"

    asyncio.run(run())


def test_model_control_sequences_are_not_written_to_terminal():
    async def run():
        output, stream = make_output()
        output.delta("hello\x1b[2J\r\x00\x9b2J\tworld")
        output.finish()
        await output.flush()
        assert "\x1b" not in stream.getvalue()
        assert "\r" not in stream.getvalue()
        assert "\x00" not in stream.getvalue()
        assert "\x9b" not in stream.getvalue()
        assert "world" in stream.getvalue()

    asyncio.run(run())


def test_words_wrap_identically_across_delta_boundaries():
    async def run():
        text = "hello world again 界界界 next"
        results = []
        for chunks in ([text], list(text), ["hello wor", "ld again 界", "界界 next"]):
            output, stream = make_output(width=10)
            for chunk in chunks:
                output.delta(chunk)
                await output.flush()
            output.finish()
            await output.flush()
            results.append(stream.getvalue())
        assert results == ["hello\nworld\nagain\n界界界\nnext\n\n"] * 3

    asyncio.run(run())


def test_exact_width_word_does_not_push_space_into_next_word():
    async def run():
        output, stream = make_output(width=5)
        output.delta("hello world again")
        output.finish()
        await output.flush()
        assert stream.getvalue() == "hello\nworld\nagain\n\n"

    asyncio.run(run())


def test_trailing_separator_at_right_edge_survives_until_next_delta():
    async def run():
        output, stream = make_output(width=5)
        output.delta("hello ")
        await output.flush()
        assert output.tail == "hello "
        output.delta("world")
        output.finish()
        await output.flush()
        assert stream.getvalue() == "hello\nworld\n\n"

    asyncio.run(run())


def test_long_tokens_split_but_indentation_and_explicit_newlines_survive():
    async def run():
        output, stream = make_output(width=8)
        output.delta("  x =  1\n\nabcdefghijklmnopq")
        output.finish()
        await output.flush()
        assert stream.getvalue() == "  x =  1\n\nabcdefgh\nijklmnop\nq\n\n"

    asyncio.run(run())


def test_uncommitted_tail_uses_new_width_after_resize():
    async def run():
        output, stream = make_output(width=40)
        output.delta("hello world again")
        await output.flush()
        assert stream.getvalue() == ""
        output.app.output.get_size = lambda: Size(rows=24, columns=10)
        output.finish()
        await output.flush()
        assert stream.getvalue() == "hello\nworld\nagain\n\n"

    asyncio.run(run())
