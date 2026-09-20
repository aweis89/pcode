"""Tree navigation owns the alternate screen and preserves real-CPR prompt height."""

import shutil

import pytest
from test_inspector_tmux import modal
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "Answer for this branch"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
async def prepare():
    for prompt in ("First tree question", "Second tree question"):
        _ = [event async for event in runtime.stream(prompt)]
asyncio.run(prepare())

class App(PreviewApp):
    async def choose_tree(self, output, session):
        session.default_buffer.text = "draft survives cancellation"
        await super().choose_tree(output, session)

App(runtime=runtime, model="test").run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_tree_cancel_edit_fork_and_resize(pane):
    before = capture(pane, "effort:")
    pane("send-keys", "-t", "preview:0.0", "/tree", "Enter")
    modal(pane, "Second tree question")
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    pane("send-keys", "-t", "preview:0.0", "Escape")
    after = capture(pane, "draft survives cancellation")
    assert input_rows(before) == input_rows(after)
    pane("send-keys", "-t", "preview:0.0", "C-c", "/tree", "Enter")
    modal(pane, "Second tree question")
    pane("resize-window", "-t", "preview:0", "-x", "80", "-y", "24")
    modal(pane, "Second tree question")
    # Default is the active answer. Previous row edits that turn's user prompt.
    pane("send-keys", "-t", "preview:0.0", "Up", "Enter")
    screen = capture(pane, "▌ Second tree question", columns=80)
    assert input_rows(screen) == 1
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    pane("send-keys", "-t", "preview:0.0", "C-c", "Alternative question", "Enter")
    capture(pane, "Answer for this branch", columns=80)
    pane("send-keys", "-t", "preview:0.0", "/tree", "Enter")
    screen = modal(pane, "Conversation tree")
    assert "Alternative question" in screen
    assert "Second tree question" in screen
    assert "First tree question" in screen
    pane("send-keys", "-t", "preview:0.0", "Escape")
    assert input_rows(capture(pane, "draft survives cancellation", columns=80)) == 1
