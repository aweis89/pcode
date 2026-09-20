"""Thinking scrollback/replay must not grow the live prompt under real CPR."""

import shutil
import time

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences

save_preferences(show_thinking="on", autohide_tasks="off")
async def model(messages, info):
    app.activity.plan = [{"content": "ACTIVE_TASK", "status": "in_progress"}]
    for i in range(30):
        yield {0: DeltaThinkingPart(content=f"**REASONING_{i:02d}** live text\n\n")}
        await asyncio.sleep(0.03)
    # Keep the thinking block open: the text must stream before completion.
    await asyncio.sleep(60)
    yield "Public answer"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
app = PreviewApp(model="test:local", runtime=runtime)
app.run()
"""


def history(pane):
    return pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")


def assert_compact(screen):
    assert input_rows(screen) == 1
    assert "Reasoning summary" not in screen
    assert "Summary ·" not in screen
    lines = screen.splitlines()
    top = max(i for i, line in enumerate(lines) if "Tasks 0/1" in line)
    bottom = next(i for i in range(top + 1, len(lines)) if lines[i].startswith("└"))
    assert bottom - top == 2  # Only the active task, never thinking rows.
    assert "REASONING" not in lines[top]


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_streaming_thinking_enters_history_toggle_redraws_and_cancel_retains(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "REASONING_29", running=True)
    assert_compact(screen)
    for i in range(30):
        assert history(pane).count(f"REASONING_{i:02d}") == 1
    lines = history(pane).splitlines()
    first = next(i for i, line in enumerate(lines) if "REASONING_00" in line)
    assert all(f"REASONING_{i:02d}" in lines[first + 2 * i] for i in range(30))
    assert all(not lines[first + 2 * i + 1].strip() for i in range(29))
    assert "**REASONING" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "draft preserved")
    pane("send-keys", "-t", "preview:0.0", "C-t")
    time.sleep(0.3)
    screen = capture(pane, "draft preserved", running=True)
    assert_compact(screen)
    assert "REASONING_" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "C-t")
    capture(pane, "REASONING_29", running=True)
    assert history(pane).count("REASONING_00") == 1
    assert history(pane).count("REASONING_29") == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")  # Discards the draft.
    capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "REASONING_29" in history(pane)
    assert input_rows(screen) == 1
    # The same toggle works after the turn, rather than clearing thinking forever.
    pane("send-keys", "-t", "preview:0.0", "C-t")
    time.sleep(0.3)
    assert "REASONING_" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "C-t")
    capture(pane, "REASONING_29")
    assert history(pane).count("REASONING_00") == 1


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_thinking_scrollback_resize_keeps_real_prompt_height_and_no_duplicates(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "REASONING_29", running=True)
    for width, height in ((80, 24), (35, 16), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        time.sleep(0.5)
        screen = capture(pane, "REASONING_29", running=True, columns=width)
        assert_compact(screen)
        assert history(pane).count("REASONING_00") == 1
        assert history(pane).count("REASONING_29") == 1


COMPLETION_SCRIPT = SCRIPT.replace("await asyncio.sleep(60)", "await asyncio.sleep(0.2)")


@pytest.mark.parametrize("pane", [COMPLETION_SCRIPT], indirect=True)
def test_completed_thinking_stays_in_scrollback(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "Public answer")
    assert history(pane).count("REASONING_29") == 1
    pane("send-keys", "-t", "preview:0.0", "/show-thinking off", "Enter")
    capture(pane, "Show thinking: off")
    assert "REASONING_" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "/show-thinking on", "Enter")
    capture(pane, "Show thinking: on")
    assert history(pane).count("REASONING_00") == 1
    assert history(pane).count("Public answer") == 1
