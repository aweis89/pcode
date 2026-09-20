"""Editor soft wraps land between words, not in the middle of one."""

import asyncio
from io import StringIO

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.ui import Activity, Transcript, create_prompt
from pcode.word_wrap import wrap_padding


def rows(text: str, width: int) -> list[str]:
    """Render `text` the way prompt_toolkit wraps it, padding included."""
    padding = wrap_padding(text, width)
    padded = "".join(" " * padding.get(i, 0) + c for i, c in enumerate(text))
    return [padded[i : i + width] for i in range(0, len(padded), width)]


def test_word_moves_to_next_row_whole():
    assert rows("alpha bravo charlie", 12) == ["alpha bravo ", "charlie"]


def test_word_wider_than_a_row_is_still_split():
    assert wrap_padding("x supercalifragilistic", 10) == {}


def test_no_padding_when_everything_fits():
    assert wrap_padding("alpha bravo", 40) == {}
    assert wrap_padding("", 40) == {}


def test_word_ending_exactly_at_the_edge_is_left_alone():
    assert wrap_padding("alpha bravo charlie", 11) == {}


def test_every_row_holds_only_complete_words():
    text = "one two three four five six seven eight nine ten"
    words = set(text.split())
    for row in rows(text, 14):
        assert set(row.split()) <= words


def test_padding_counts_columns_not_characters():
    # "ab " leaves 5 columns; the 6-column word needs the whole next row.
    assert wrap_padding("ab 日本語", 8) == {3: 5}


def test_editor_renders_words_unsplit():
    async def run():
        stream = StringIO()
        activity = Activity()
        transcript = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=30), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app = session.app
            with set_app(app):
                try:
                    session.default_buffer.insert_text("alpha bravo charlie delta echo")
                    stream.seek(0)
                    stream.truncate()
                    app.renderer.render(app, app.layout)
                    data = stream.getvalue()
                    for word in ("alpha", "bravo", "charlie", "delta", "echo"):
                        assert word in data
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
