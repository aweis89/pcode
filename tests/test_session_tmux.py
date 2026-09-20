"""Exercise session popup ownership and prompt height with real cursor reports."""

import shutil
import time

import pytest
from test_inspector_tmux import modal
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from pathlib import Path
from tempfile import TemporaryDirectory
from pcode.app import PreviewApp
from pcode.sessions import SavedSession

with TemporaryDirectory() as directory:
    root = Path(directory)
    saved = SavedSession.create("test:local", Path.cwd(), root)
    saved.append("turn_started", prompt="First popup question")
    saved.close()
    PreviewApp(session_dir=root).run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_resume_popup_cancel_restores_prompt_height(pane):
    before = capture(pane, "Enter:")
    pane("send-keys", "-t", "preview:0.0", "/resume", "Enter")
    modal(pane, "First popup question")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    after = capture(pane, "Enter:")
    assert input_rows(before) == input_rows(after)
    pane("send-keys", "-t", "preview:0.0", "still editable")
    capture(pane, "still editable")


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_resume_popup_survives_resize(pane):
    """The suspended editor's size poll must not erase or CPR over a popup.

    Both applications poll the size every 0.5 s and the popup survives only if
    its poll fires last, a fixed phase per popup. Spread the resizes across the
    poll cycle so at least one lands in the losing phase.
    """
    capture(pane, "Enter:")
    pane("send-keys", "-t", "preview:0.0", "/resume", "Enter")
    modal(pane, "First popup question")
    for step in range(10):
        width = "80" if step % 2 == 0 else "90"
        pane("resize-window", "-t", "preview", "-x", width, "-y", "24")
        time.sleep(0.6 + step * 0.05)  # Past both polls; a bad erase has happened by now.
        screen = modal(pane, "First popup question")
        assert "Esc Cancel" in screen, screen  # The footer, below the cursor row.
    pane("send-keys", "-t", "preview:0.0", "Escape")
    after = capture(pane, "Enter:")
    assert input_rows(after) == 1
    pane("send-keys", "-t", "preview:0.0", "still editable")
    capture(pane, "still editable")


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_session_info_popup_cancel_restores_prompt_height(pane):
    before = capture(pane, "Enter:")
    pane("send-keys", "-t", "preview:0.0", "/status", "Enter")
    modal(pane, "Preview turns")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    after = capture(pane, "Enter:")
    assert input_rows(before) == input_rows(after)
    pane("send-keys", "-t", "preview:0.0", "still editable")
    capture(pane, "still editable")
