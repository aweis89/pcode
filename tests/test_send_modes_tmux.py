"""Cycling send mode must preserve the draft and compact real-CPR layout."""

import shutil

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
from pcode.app import PreviewApp
PreviewApp().run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_cycle_send_mode_keeps_draft_and_height(pane):
    assert input_rows(capture(pane, "Enter: steering")) == 1
    pane("send-keys", "-t", "preview:0.0", "draft")
    for mode in ("queue", "interrupt", "steering"):
        pane("send-keys", "-t", "preview:0.0", "C-s")
        screen = capture(pane, f"Enter: {mode}")
        assert "draft" in screen
        assert input_rows(screen) == 1
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "draft", columns=columns)
        assert "Enter: steering" in screen.splitlines()[-1]
        assert input_rows(screen) == 1
        for mode in ("queue", "interrupt", "steering"):
            pane("send-keys", "-t", "preview:0.0", "C-s")
            screen = capture(pane, f"Enter: {mode}", columns=columns)
            assert f"Enter: {mode}" in screen.splitlines()[-1]
            assert "draft" in screen
            assert input_rows(screen) == 1
