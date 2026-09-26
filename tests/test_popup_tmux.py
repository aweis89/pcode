"""Rapid popup shortcuts must not leave another modal waiting behind the first."""

import shutil

import pytest
from test_inspector_tmux import modal
from test_tmux import capture
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from unittest.mock import patch
from pcode.app import PreviewApp
from pcode.commands import Command

app = PreviewApp()
app.registry.register(Command(
    "/after", "Mark the end of the command queue",
    lambda _: app.transcript.note("Queued commands finished"),
))
with patch("pcode.models.active_providers", return_value={"anthropic"}):
    app.run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_double_ctrl_l_opens_only_one_picker(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "C-l", "C-l", "/after", "Enter", "draft")
    modal(pane, "Choose model")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    # This marker runs after both requests: don't mistake a transient editor
    # repaint between the first and second picker for successful dismissal.
    after = capture(pane, "Queued commands finished")
    assert "draft" in after
    pane("send-keys", "-t", "preview:0.0", "C-l")
    modal(pane, "Choose model")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    capture(pane, "draft")
