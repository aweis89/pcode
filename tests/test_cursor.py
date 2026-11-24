"""Cursor visibility across renderer resets and terminal handoffs."""

import pytest
from prompt_toolkit.output import DummyOutput, Output

from pcode.ui import CursorSafeOutput


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.visible = True
        self.shows = 0

    def show_cursor(self):
        self.visible = True
        self.shows += 1

    def hide_cursor(self):
        self.visible = False


@pytest.mark.parametrize("fail", [False, True])
def test_handoff_suppresses_reset_cursor_until_editor_is_restored(fail):
    terminal = RecordingOutput()
    output = CursorSafeOutput(terminal)
    assert isinstance(output, Output)
    assert output.get_size() == terminal.get_size()
    try:
        with output.hidden_cursor():
            output.show_cursor()  # Renderer.erase/reset before printing.
            output.flush()
            assert not terminal.visible
            with output.hidden_cursor():
                output.show_cursor()  # Reset before repaint.
            assert not terminal.visible
            output.hide_cursor()  # Renderer paints the editor.
            output.show_cursor()  # Renderer positions the editor cursor.
            assert not terminal.visible
            if fail:
                raise RuntimeError("print failed")
    except RuntimeError:
        assert fail
    assert terminal.visible
    assert terminal.shows == 1


def test_every_editor_repaint_is_synchronized_without_the_glide():
    """iTerm2 drew repaints mid-way (cursor hidden, moved to the spinner) otherwise."""
    import asyncio

    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from rich.console import Console

    from pcode.ui import SYNC_END, SYNC_START, TerminalOutput

    class Raw(DummyOutput):
        def __init__(self):
            self.raw = []

        def write_raw(self, data):
            self.raw.append(data)

    async def run():
        with create_pipe_input() as pipe:
            terminal = Raw()
            app = Application(input=pipe, output=terminal)
            output = TerminalOutput(Console(force_terminal=True, width=80), app)
            assert not output.gliding
            task = asyncio.ensure_future(output.run())
            await asyncio.sleep(0)
            app.before_render.fire()
            app.after_render.fire()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            app.before_render.fire()  # Unhooked once the output stops.
        assert terminal.raw == [SYNC_START, SYNC_END]

    asyncio.run(run())


def test_handoff_hide_waits_for_the_synchronized_frame():
    """Flushed alone, the hide reached the terminal ahead of the frame."""
    terminal = RecordingOutput()
    flushes = []
    terminal.flush = lambda: flushes.append(terminal.visible)
    with CursorSafeOutput(terminal).hidden_cursor():
        assert not terminal.visible
        assert flushes == []
    assert flushes == [True]


def test_handoff_respects_renderer_request_to_keep_cursor_hidden():
    terminal = RecordingOutput()
    output = CursorSafeOutput(terminal)
    with output.hidden_cursor():
        output.hide_cursor()
    assert not terminal.visible
    output.show_cursor()
    assert terminal.visible


def test_renderer_erase_avoids_a_full_screen_clear():
    from unittest.mock import Mock, call

    from prompt_toolkit.data_structures import Size

    terminal = Mock(spec=DummyOutput)
    terminal.get_size.return_value = Size(rows=24, columns=80)
    output = CursorSafeOutput(terminal)
    output.erase_down()
    assert terminal.method_calls == [
        call.get_size(),
        call.cursor_forward(1),
        call.erase_down(),
        call.cursor_backward(1),
        call.erase_end_of_line(),
    ]
