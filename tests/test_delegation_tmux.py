"""Delegation panel height under real cursor-position reports, not a no-CPR PTY."""

import shutil

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import ToolStarted, ToolSummary

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        yield ToolStarted("delegate_task", "explorer · investigate authentication", "parent",
                          activity="Working")
        for i in range(20):
            yield ToolSummary("read_file", f"chatter-{i}", call_id=str(i))
        yield ToolSummary("search_files", "auth in src → 8 matches", call_id="parent:search",
                          parent_call_id="parent")
        yield ToolStarted("read_file", "src/auth.py", "parent:read", parent_call_id="parent")
        await asyncio.sleep(60)

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_delegate_stays_visible_with_nested_children_resize_and_cancel(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "src/auth.py", running=True)
    assert "explorer" in screen
    assert "│    ✓ Search" in screen
    assert "│    ⟳ Read" in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 14), (100, 32), (60, 20)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "src/auth.py", running=True, columns=width)
        lines = screen.splitlines()
        top = max(i for i, line in enumerate(lines) if line.startswith("┌─ Tools"))
        assert "⟳ Delegate" in lines[top + 1]
        assert lines[top + 2].startswith("│    ✓ Search")
        assert lines[top + 3].startswith("│    ⟳ Read")
        assert lines[top + 4].startswith("└")
        assert input_rows(screen) == 1
        assert "keep draft" in screen
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    assert "Delegate · interrupted" in screen
    assert "⟳ Delegate" not in screen
    assert input_rows(screen) == 1
