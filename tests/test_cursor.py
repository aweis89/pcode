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


def test_handoff_respects_renderer_request_to_keep_cursor_hidden():
    terminal = RecordingOutput()
    output = CursorSafeOutput(terminal)
    with output.hidden_cursor():
        output.hide_cursor()
    assert not terminal.visible
    output.show_cursor()
    assert terminal.visible
