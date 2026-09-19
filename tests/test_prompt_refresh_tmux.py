"""Active timers still update real CPR terminals after idle refresh is removed."""

import re
import shutil
import time

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import ToolStarted

class Runtime:
    session = None
    async def stream(self, prompt):
        yield ToolStarted("shell", "WAITING_TOOL", "one")
        await asyncio.sleep(60)
    def reset(self):
        pass

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_animation_elapsed_resize_and_cancel_after_idle(pane):
    capture(pane, "❯")
    time.sleep(0.3)
    pane("send-keys", "-t", "preview:0.0", "ANIMATING_PROMPT", "Enter")
    first = capture(pane, "WAITING_TOOL", running=True)
    first_time = float(re.search(r"· ([0-9.]+)s", first)[1])
    first_icon = next(line[0] for line in first.splitlines() if "ANIMATING_PROMPT" in line)
    deadline = time.monotonic() + 3
    while True:
        time.sleep(0.13)
        screen = capture(pane, "WAITING_TOOL", running=True)
        elapsed = float(re.search(r"· ([0-9.]+)s", screen)[1])
        icon = next(line[0] for line in screen.splitlines() if "ANIMATING_PROMPT" in line)
        if elapsed > first_time and icon != first_icon:
            break
        assert time.monotonic() < deadline, screen
    pane("resize-window", "-t", "preview:0", "-x", "40")
    assert input_rows(capture(pane, "WAITING_TOOL", running=True, columns=40)) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "cancelled", columns=40)
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    assert input_rows(capture(pane, "kept draft", columns=40)) == 1
