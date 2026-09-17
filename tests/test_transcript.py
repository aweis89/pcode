"""Append-only output and buffered streaming without a real terminal."""

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.markdown import Markdown

from pcode.ui import CursorSafeOutput, TerminalOutput


def make_output(width=80):
    stream = StringIO()
    terminal = DummyOutput()
    terminal.get_size = lambda: Size(rows=24, columns=width)
    app = SimpleNamespace(output=CursorSafeOutput(terminal), invalidate=lambda: None)
    output = TerminalOutput(Console(file=stream, color_system=None), app)
    return output, stream


def rendered(stream):
    return "\n".join(line.rstrip() for line in stream.getvalue().split("\n"))


def test_complete_blocks_commit_once_and_final_message_is_not_reprinted():
    async def run():
        output, stream = make_output()
        output.delta("**hello**\n\npartial")
        await output.flush()
        assert rendered(stream) == "hello\n\n"
        assert output.tail == "partial"
        output.delta(" answer")
        await output.flush()
        assert rendered(stream) == "hello\n\n"
        assert output.tail == "partial answer"
        output.finish("**hello**\n\npartial answer")
        await output.flush()
        assert rendered(stream) == "hello\n\npartial answer\n\n"
        assert output.tail == ""
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
        assert rendered(stream) == "No deltas\n\ntool finished\nnext\n\n"

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
            results.append(rendered(stream))
        assert results == ["hello\nworld\nagain\n界界界\nnext\n\n"] * 3

    asyncio.run(run())


def test_exact_width_word_does_not_push_space_into_next_word():
    async def run():
        output, stream = make_output(width=5)
        output.delta("hello world again")
        output.finish()
        await output.flush()
        assert rendered(stream) == "hello\nworld\nagain\n\n"

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
        assert rendered(stream) == "hello\nworld\n\n"

    asyncio.run(run())


def test_long_tokens_split_and_paragraphs_render_as_markdown():
    async def run():
        output, stream = make_output(width=8)
        output.delta("  x =  1\n\nabcdefghijklmnopq")
        output.finish()
        await output.flush()
        assert rendered(stream) == "x =  1\n\nabcdefgh\nijklmnop\nq\n\n"

    asyncio.run(run())


def test_uncommitted_tail_uses_new_width_after_resize():
    async def run():
        output, stream = make_output(width=40)
        output.delta("hello world again")
        await output.flush()
        assert rendered(stream) == ""
        output.app.output.get_size = lambda: Size(rows=24, columns=10)
        output.finish()
        await output.flush()
        assert rendered(stream) == "hello\nworld\nagain\n\n"

    asyncio.run(run())


def test_markdown_structures_match_static_renderer_across_chunk_boundaries():
    samples = [
        "## Heading\n\n**bold** and `inline` with *emphasis*.",
        "```python\n  x = 1\n\n  print(x)\n```\n",
        "~~~~python\nx = 1\n```\n~~~~\n",
        "- first\n\n- second\n  - nested\n",
        "> first\n>\n> second\n",
        "| Name | Value |\n| --- | --- |\n| one | two |\n\n",
        "Title\n=====\n",
        "    x = 1\n\n    print(x)\n",
    ]

    async def run():
        for source in samples:
            expected = StringIO()
            console = Console(file=expected, color_system=None, width=40)
            console.print(Markdown(source, code_theme="nord"))
            console.print()
            for chunks in ([source], list(source)):
                output, stream = make_output(width=40)
                for chunk in chunks:
                    output.delta(chunk)
                    await output.flush()
                output.finish(source)
                await output.flush()
                assert rendered(stream) == rendered(expected), source

    asyncio.run(run())


def test_fence_blank_lines_stay_buffered_until_closing_fence():
    async def run():
        output, stream = make_output()
        output.delta("```python\nprint('first')\n\n")
        await output.flush()
        assert stream.getvalue() == ""
        output.delta("print('second')\n```\n")
        await output.flush()
        assert "first" in stream.getvalue()
        assert "second" in stream.getvalue()
        assert "```" not in stream.getvalue()
        assert output.tail == ""

    asyncio.run(run())


def test_unfinished_block_is_hidden_and_finish_preserves_all_source():
    async def run():
        output, stream = make_output(width=20)
        source = "\n".join(f"line_{i:03d}" for i in range(100))
        output.delta(source)
        await output.flush()
        assert output.tail == source
        assert stream.getvalue() == ""
        output.finish()  # Also used for cancellation and tool boundaries.
        await output.flush()
        for i in range(100):
            assert stream.getvalue().count(f"line_{i:03d}") == 1
        assert output.tail == ""

    asyncio.run(run())


def test_streamed_markdown_emits_styles_and_uses_selected_code_theme():
    async def run():
        output, stream = make_output()
        output.console = Console(file=stream, force_terminal=True, color_system="truecolor")
        output.code_theme = lambda: "ansi_light"
        output.delta("**bold**\n\n```python\nx = 1\n```\n")
        assert any(
            isinstance(obj, Markdown) and obj.code_theme == "ansi_light"
            for objects, _, _ in output.pending
            for obj in objects
        )
        output.finish()
        await output.flush()
        assert "\x1b[" in stream.getvalue()
        assert "**" not in stream.getvalue()
        assert "```" not in stream.getvalue()

    asyncio.run(run())


def test_pending_prints_share_one_rich_write_and_flush():
    class CountingStream(StringIO):
        writes = 0
        flushes = 0

        def write(self, text):
            self.writes += 1
            return super().write(text)

        def flush(self):
            self.flushes += 1
            return super().flush()

    async def run():
        output, _ = make_output()
        stream = CountingStream()
        output.console = Console(file=stream, color_system=None)
        output.delta("**first**\n\nsecond\n\n")
        output.print("tool finished")
        await output.flush()
        assert rendered(stream).splitlines() == ["first", "", "second", "", "tool finished"]
        assert stream.writes == 1
        assert stream.flushes == 1

    asyncio.run(run())


def test_unfinished_text_and_noop_flush_do_not_invalidate():
    async def run():
        output, stream = make_output(width=10)
        invalidations = []
        output.app.invalidate = lambda: invalidations.append(True)
        for chunk in ("hello", " world", " again"):
            output.delta(chunk)
            await output.flush()
        for _ in range(5):
            await output.flush()
        assert invalidations == []
        assert stream.getvalue() == ""
        assert output.tail == "hello world again"
        output.finish()
        await output.flush()
        assert rendered(stream) == "hello\nworld\nagain\n\n"

    asyncio.run(run())


def test_turn_quote_waits_for_first_visible_block_and_is_printed_once():
    async def run():
        output, stream = make_output()
        output.begin_turn("literal **prompt**")
        await output.flush()
        assert stream.getvalue() == ""
        output.delta("First partial")
        await output.flush()
        assert stream.getvalue() == ""
        output.print("Unrelated notice")
        await output.flush()
        assert rendered(stream) == "Unrelated notice\n"
        output.delta(" response\n\n")
        await output.flush()
        assert (
            rendered(stream)
            == "Unrelated notice\n\n▌ literal **prompt**\n\nFirst partial response\n\n"
        )
        output.finish("First partial response")
        output.finish("Second message")
        output.end_turn()
        await output.flush()
        assert stream.getvalue().count("▌ literal **prompt**") == 1
        assert rendered(stream).endswith("Second message\n\n")

    asyncio.run(run())


def test_turn_quote_attaches_to_fallback_or_interrupted_partial_response():
    async def run():
        output, stream = make_output()
        output.begin_turn("fallback prompt")
        output.finish("Fallback message")
        output.end_turn()
        output.begin_turn("interrupted prompt")
        output.delta("Partial reply")
        output.end_turn()
        await output.flush()
        assert rendered(stream) == (
            "\n▌ fallback prompt\n\nFallback message\n\n\n▌ interrupted prompt\n\nPartial reply\n\n"
        )

    asyncio.run(run())


def test_empty_turn_drops_deferred_quote_without_leaking_to_next_turn():
    async def run():
        output, stream = make_output()
        output.begin_turn("unanswered prompt")
        output.delta(" \n\n")
        output.end_turn()
        await output.flush()
        assert stream.getvalue() == ""
        output.print("Run cancelled.")
        output.begin_turn("next prompt")
        output.finish("Next answer")
        output.end_turn()
        await output.flush()
        assert rendered(stream) == "Run cancelled.\n\n▌ next prompt\n\nNext answer\n\n"

    asyncio.run(run())


@pytest.mark.parametrize("width_changes", [True, False])
def test_resize_replay_debounces_width_changes_and_ignores_height(monkeypatch, width_changes):
    async def run():
        output, _ = make_output()
        sizes = iter([(24, 80), (30, 80), (30, 60), (30, 40)] + [(30, 40)] * 8)
        current = Size(rows=24, columns=80)
        clock = 0.0
        replays = []
        output.resize_replay = lambda: None
        output.app.output.get_size = lambda: current
        output.regenerate = lambda replay: replays.append((clock, replay))

        async def wait_for(awaitable, *, timeout):
            nonlocal current, clock
            awaitable.close()
            rows, columns = next(sizes)
            current = Size(rows=rows, columns=columns if width_changes else 80)
            clock += timeout
            raise TimeoutError

        async def sleep(delay):
            pass

        async def flush():
            if clock >= 1.0:
                raise asyncio.CancelledError

        monkeypatch.setattr("pcode.ui.monotonic", lambda: clock)
        monkeypatch.setattr("pcode.ui.asyncio.wait_for", wait_for)
        monkeypatch.setattr("pcode.ui.asyncio.sleep", sleep)
        output.flush = flush
        try:
            await output.run()
        except asyncio.CancelledError:
            pass
        assert len(replays) == int(width_changes)
        if width_changes:
            assert replays[0][0] >= 0.65
            assert replays[0][1] is output.resize_replay

    asyncio.run(run())
