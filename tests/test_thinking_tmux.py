"""The multiline thinking frame must stay transient under real cursor reports."""

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

save_preferences(show_thinking="on", thinking_lines="LIMIT")
async def model(messages, info):
    app.activity.plan = [{"content": "ACTIVE_TASK", "status": "in_progress"}]
    for i in range(30):
        yield {0: DeltaThinkingPart(content=("\n" if i else "") + f"REASONING_{i:02d} live text")}
        await asyncio.sleep(0.03)
    yield "Public answer"
    await asyncio.sleep(60)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
app = PreviewApp(model="test:local", runtime=runtime)
app.run()
"""


def thinking_body(screen):
    lines = screen.splitlines()
    top = max(i for i, line in enumerate(lines) if "Thinking · Ctrl+T" in line)
    bottom = next(i for i in range(top + 1, len(lines)) if lines[i].startswith("└"))
    task = max(i for i, line in enumerate(lines) if "ACTIVE_TASK" in line)
    editor = max(i for i, line in enumerate(lines) if line.startswith("│❯"))
    assert top < bottom < task < editor
    return lines[top + 1 : bottom]


@pytest.mark.parametrize(
    "pane, limit",
    [(SCRIPT.replace("LIMIT", str(limit)), limit) for limit in (5, 10, 1000)],
    indirect=["pane"],
)
def test_streaming_thinking_frame_height_toggle_and_cleanup(pane, limit):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "REASONING_29", running=True)
    assert len(thinking_body(screen)) == min(limit, 20)
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-t")
    time.sleep(0.2)
    screen = capture(pane, "❯", running=True)
    assert "REASONING_" not in screen
    assert "Thinking · Ctrl+T" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-t")
    capture(pane, "REASONING_29", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "REASONING_" not in screen
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "REASONING_" not in history


@pytest.mark.parametrize("pane", [SCRIPT.replace("LIMIT", "10")], indirect=True)
def test_thinking_box_rebudgets_real_prompt_height_on_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "REASONING_29", running=True)
    for width, height in ((80, 24), (35, 16), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "REASONING_29", running=True, columns=width)
        # Like the task panel regression, inspect the live frame nearest the
        # editor: tmux may have copied old rows into history before SIGWINCH.
        assert 1 <= len(thinking_body(screen)) <= min(10, height - 8)
        assert input_rows(screen) == 1
