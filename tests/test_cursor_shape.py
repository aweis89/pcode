"""The prompt's cursor shape follows the editing mode and resets when it lets go."""

import asyncio
from io import StringIO

from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig, SimpleCursorShapeConfig
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.ui import Transcript, create_prompt, suspended_editor

BLOCK, BEAM, UNDERLINE, RESET = "\x1b[2 q", "\x1b[6 q", "\x1b[4 q", "\x1b[0 q"


def test_prompt_uses_modal_cursor_shapes_and_allows_override():
    prompt = create_prompt(CommandRegistry(), output=DummyOutput())
    assert isinstance(prompt.cursor, ModalCursorShapeConfig)
    custom = SimpleCursorShapeConfig()
    assert create_prompt(CommandRegistry(), output=DummyOutput(), cursor=custom).cursor is custom


def shapes(text: str) -> list[str]:
    return [code for code in (BLOCK, BEAM, UNDERLINE, RESET) for _ in range(text.count(code))]


def run_prompt(vi_mode: bool, steps):
    """Run the prompt on a terminal stream, sending each step's keys in turn."""
    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
    marks: list[int] = []

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                input=pipe,
                output=output,
                vi_mode=vi_mode,
                transcript=Transcript(Console(file=StringIO())),
                on_submit=lambda text: None,
            )

            async def drive():
                for step in steps:
                    await asyncio.sleep(0.05)
                    if isinstance(step, str):
                        pipe.send_text(step)
                        # Let a lone Escape time out and the repaint land.
                        await asyncio.sleep(0.7)
                    else:
                        await step(prompt.app, stream)
                    marks.append(len(stream.getvalue()))
                prompt.app.exit()

            task = asyncio.ensure_future(drive())
            await asyncio.wait_for(prompt.app.run_async(), 10)
            await task

    asyncio.run(run())
    return stream.getvalue(), marks


def test_vi_modes_change_cursor_shape_and_exit_resets_it():
    text, marks = run_prompt(True, ["a", "\x1b", "R"])
    insert, normal, replace = (
        text[: marks[0]],
        text[marks[0] : marks[1]],
        text[marks[1] : marks[2]],
    )
    assert BEAM in insert
    assert BLOCK in normal and BEAM not in normal
    assert UNDERLINE in replace
    # The exit hands the terminal its own shape back, after the last set.
    assert text.rindex(RESET) > max(text.rindex(code) for code in (BEAM, BLOCK, UNDERLINE))


def test_emacs_mode_uses_bar_cursor():
    text, _ = run_prompt(False, ["a"])
    assert BEAM in text and BLOCK not in text
    assert text.endswith(RESET) or text.rindex(RESET) > text.rindex(BEAM)


def test_handoff_resets_shape_around_direct_output():
    async def handoff(app, stream):
        async with suspended_editor(app, atomic=True) as done:
            before = stream.getvalue()
            # Direct output sees the terminal's default shape, not the editor's bar.
            assert before.rindex(RESET) > before.rindex(BEAM)
            app.output.write_raw("streamed\r\n")
            done.rows_written = 1
        await asyncio.sleep(0.1)

    text, _ = run_prompt(False, ["a", handoff])
    after = text[text.index("streamed") :]
    # The repaint puts the bar back for typing.
    assert BEAM in after
