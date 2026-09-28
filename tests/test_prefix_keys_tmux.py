"""A waiting leader lists the prompt's shortcuts above the editor, in real-CPR layout."""

import shutil

import pytest
from test_tmux import capture, input_rows, settle
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import json, os, pathlib, tempfile
os.environ["XDG_CONFIG_HOME"] = config = tempfile.mkdtemp()
pathlib.Path(config, "pcode").mkdir()
pathlib.Path(config, "pcode", "preferences.json").write_text(json.dumps({"key_prefix": "ctrl+p"}))
from pcode.app import PreviewApp
PreviewApp().run()
"""
HINT = "Ctrl+P … s Send mode"


def draft_line(screen: str) -> str:
    return next(line for line in screen.splitlines() if line.startswith("│❯"))


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_leader_hint_shows_above_the_editor_and_runs_the_shortcut(pane):
    assert input_rows(capture(pane, "steering")) == 1
    pane("send-keys", "-t", "preview:0.0", "draft")
    capture(pane, "draft")
    # Ctrl+S alone no longer cycles the send mode; the leader and `s` do.
    pane("send-keys", "-t", "preview:0.0", "C-s")
    pane("send-keys", "-t", "preview:0.0", "C-p")
    screen = capture(pane, HINT)
    lines = screen.splitlines()
    hint = next(i for i, line in enumerate(lines) if HINT in line)
    editor = next(i for i, line in enumerate(lines) if line.startswith("┌"))
    assert hint < editor, screen
    assert "steering" in lines[-1] and input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "s")
    screen = capture(pane, "queue")
    assert HINT not in screen
    # The letter ran the shortcut instead of being typed.
    assert "draft" in draft_line(screen) and "drafts" not in draft_line(screen)
    assert input_rows(screen) == 1
    # Esc backs out, leaving the draft and the mode alone.
    pane("send-keys", "-t", "preview:0.0", "C-p")
    capture(pane, HINT)
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: HINT not in screen)
    assert HINT not in screen and "queue" in screen.splitlines()[-1], screen
    assert "draft" in draft_line(screen) and input_rows(screen) == 1
