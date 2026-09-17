"""Real cursor-position reports must not stretch the prompt during compaction."""

import shutil

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
from pcode.app import PreviewApp

class Runtime:
    session = None
    history = []
    async def compact(self, focus):
        await asyncio.sleep(60)

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_compaction_keeps_editor_height_and_cancels_with_draft(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/compact keep test failures")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "Compacting context", running=True)) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "keep this draft")
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "keep this draft", running=True, columns=columns)
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Compaction cancelled")
    assert "keep this draft" in screen
    assert input_rows(screen) == 1
