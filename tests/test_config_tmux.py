"""Configuration edits and prompt restoration in a real terminal."""

import shutil

import pytest
from test_inspector_tmux import modal
from test_tmux import capture, input_rows, resize
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from pcode.app import PreviewApp

class App(PreviewApp):
    async def browse_config(self, output, session):
        session.default_buffer.text = "draft must survive"
        await super().browse_config(output, session)

App().run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
@pytest.mark.parametrize("width,height", [(80, 16), (70, 24)])
def test_config_edit_resize_and_restore(pane, width, height):
    capture(pane, "❯")
    resize(pane, "resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
    pane("send-keys", "-t", "preview:0.0", "/config", "Enter")
    modal(pane, "Saved configuration")
    modal(pane, "Saved effective:")
    modal(pane, "may require next launch")
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    pane("send-keys", "-t", "preview:0.0", "tool_max_lines", "Enter", "C-a", "C-k", "7", "Enter")
    modal(pane, 'Saved effective: "7" (from user)')
    # A compact terminal must retain the editor and its save/cancel controls.
    pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
    modal(pane, 'Saved effective: "7" (from user)')
    pane("send-keys", "-t", "preview:0.0", "Enter", "C-a", "C-k", "bad", "Enter")
    modal(pane, "Value: bad")
    modal(pane, "must be")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    modal(pane, "Edit cancelled")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "draft must survive", columns=width)
    assert input_rows(screen) == 1
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Saved effective:" not in history
