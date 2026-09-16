"""Stable palette identity prevents full editor repaints on ordinary redraws."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import DynamicStyle
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.ui import PALETTES, Activity, Transcript, create_prompt


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_palette_reuses_prompt_style(theme):
    palette = PALETTES[theme]
    style = palette.prompt_style()
    assert palette.prompt_style() is style
    assert palette.prompt_style().invalidation_hash() == style.invalidation_hash()


def test_idle_and_typing_redraws_do_not_erase_editor_but_theme_changes_do():
    async def run():
        stream = StringIO()
        activity = Activity()
        transcript = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            # Inspect real VT100 renderer output, not DummyOutput. Height behavior
            # with CPR remains covered separately by the real-tmux regressions.
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app = session.app
            # Match the production dynamic-style callback in PreviewApp.run_async.
            app.style = DynamicStyle(lambda: transcript.palette.prompt_style())

            def render():
                stream.seek(0)
                stream.truncate()
                app.renderer.render(app, app.layout)
                return stream.getvalue()

            def assert_incremental(data):
                assert "\x1b[J" not in data  # No erase-down/full editor repaint.
                assert "┌" not in data
                assert "└" not in data

            with set_app(app):
                try:
                    assert "┌" in render()
                    for _ in range(3):
                        assert_incremental(render())
                    for char in "ordinary typing":
                        session.default_buffer.insert_text(char)
                        data = render()
                        assert char in data
                        assert_incremental(data)
                    # Changing palettes must still repaint, then become stable
                    # again, including when switching back to a cached palette.
                    for theme in ("light", "dark"):
                        transcript.theme = theme
                        data = render()
                        assert "\x1b[J" in data
                        assert "┌" in data
                        assert "ordinary typing" in data
                        assert_incremental(render())
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
