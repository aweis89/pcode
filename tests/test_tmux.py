"""Layout regression in real tmux, including cursor-position reports (CPR)."""

import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


@pytest.fixture
def pane(request):
    server = "pcode-test-" + uuid.uuid4().hex
    base = ["tmux", "-L", server, "-f", "/dev/null"]
    env = {**os.environ}
    env.pop("PROMPT_TOOLKIT_NO_CPR", None)

    def command(*args):
        return subprocess.check_output([*base, *args], text=True, env=env)

    try:
        command(
            "new-session",
            "-d",
            "-s",
            "preview",
            "-x",
            "100",
            "-y",
            "32",
            "-c",
            os.getcwd(),
            shlex.join(
                [sys.executable, "-c", request.param]
                if hasattr(request, "param")
                else [sys.executable, "-m", "pcode.app"]
            ),
        )
        yield command
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, env=env)


def capture(pane, expected, *, running=False, columns=None):
    """Allow asynchronous completion and resize paints to settle, with a deadline."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        screen = pane("capture-pane", "-p", "-t", "preview:0.0")
        lines = screen.splitlines()
        if (
            expected in screen
            and len(lines) >= 2
            and lines[-2].startswith("└")
            and (columns is None or len(lines[-2]) == columns)
            and ("Ctrl+C cancel" if running else "Ctrl+D exit") in lines[-1]
        ):
            return screen
        time.sleep(0.05)
    pytest.fail(f"Prompt did not settle with {expected!r}:\n{screen}")


def input_rows(screen):
    lines = screen.splitlines()
    assert "Ctrl+D exit" in lines[-1] or "Ctrl+C cancel" in lines[-1], screen
    assert lines[-2].startswith("└"), screen
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("┌"))
    bottom = next(i for i, line in enumerate(lines[top + 1 :], top + 1) if line.startswith("└"))
    return bottom - top - 1


def test_transcript_uses_terminal_scrollback(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/demo")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "No files were")) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Hello, world!" in history
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    assert "pcode  /  UI preview" in history


@pytest.mark.parametrize("split", ["-h", "-v"])
def test_input_only_grows_for_text(pane, split):
    assert input_rows(capture(pane, "❯")) == 1
    pane("split-window", split, "-t", "preview:0.0", "cat")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    assert input_rows(capture(pane, "hello")) == 1

    pane("send-keys", "-t", "preview:0.0", "Escape", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "second line")
    assert input_rows(capture(pane, "second line")) == 2

    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "-l", "/")
    screen = capture(pane, "\n /demo ")
    assert input_rows(screen) == 1
    assert screen.index("\n /demo ") < screen.rindex("┌")  # Menu above the fixed frame.

    pane("send-keys", "-t", "preview:0.0", "C-c")
    text = "x" * 120 + "END"
    pane("send-keys", "-t", "preview:0.0", "-l", text)
    screen = capture(pane, "END")
    assert input_rows(screen) > 1  # Wrapped input, not just explicit newlines.

    pane("send-keys", "-t", "preview:0.0", "C-u")
    pane("send-keys", "-t", "preview:0.0", "-l", "short")
    assert input_rows(capture(pane, "short")) == 1
    pane("kill-pane", "-t", "preview:0.1")
    pane("send-keys", "-t", "preview:0.0", "-l", " again")
    assert input_rows(capture(pane, "short again")) == 1


LIVE_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "COMMITTED LINE\\nFIRST STREAM CHUNK"
    await asyncio.sleep(2)
    yield "\\nLIVE ANSWER COMPLETE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_keeps_prompt_at_bottom_and_commits_once(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    streaming = capture(pane, "FIRST STREAM CHUNK", running=True)
    assert input_rows(streaming) == 1
    assert "COMMITTED LINE\nFIRST STREAM CHUNK" in streaming
    before = streaming.splitlines().index("FIRST STREAM CHUNK")
    completed = capture(pane, "LIVE ANSWER COMPLETE")
    assert input_rows(completed) == 1
    assert completed.splitlines().index("FIRST STREAM CHUNK") == before
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("FIRST STREAM CHUNK") == 1
    assert "❯ hello" in history


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_resize_and_cancellation(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "FIRST STREAM CHUNK", running=True)
    pane("split-window", "-v", "-t", "preview:0.0", "cat")
    assert input_rows(capture(pane, "FIRST STREAM CHUNK", running=True)) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "next input")
    capture(pane, "❯ next input", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    cancelled = capture(pane, "Run cancelled.")
    assert input_rows(cancelled) == 1
    assert "❯ next input" in cancelled


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_draft_and_cursor_survive_stream_completion_and_width_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "FIRST STREAM CHUNK", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "draft text")
    pane("send-keys", "-t", "preview:0.0", "Left", "Left", "Left", "Left")
    capture(pane, "❯ draft text", running=True)
    pane("split-window", "-h", "-t", "preview:0.0", "cat")
    capture(pane, "❯ draft text", running=True)
    screen = capture(pane, "LIVE ANSWER COMPLETE")
    assert "❯ draft text" in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "my ")
    capture(pane, "❯ draft my text")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("COMMITTED LINE") == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_immediate_cancellation_unlocks_editor(pane):
    capture(pane, "❯")
    # Deliver submission and cancellation together, before the model task can start.
    pane("send-keys", "-t", "preview:0.0", "h", "Enter", "C-c")
    capture(pane, "Run cancelled.")
    pane("send-keys", "-t", "preview:0.0", "-l", "editable again")
    assert input_rows(capture(pane, "editable again")) == 1


@pytest.mark.xfail(
    strict=True,
    reason="prompt_toolkit's resize erase can leave the live line behind after frame reflow",
)
@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_width_resize_does_not_leave_a_copy_of_unfinished_line(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "FIRST STREAM CHUNK", running=True)
    pane("split-window", "-h", "-t", "preview:0.0", "cat")
    capture(pane, "LIVE ANSWER COMPLETE")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("FIRST STREAM CHUNK") == 1


def test_cancel_history_search_discards_draft(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "draft")
    pane("send-keys", "-t", "preview:0.0", "C-r", "C-c")
    capture(pane, "Input discarded.")
    pane("send-keys", "-t", "preview:0.0", "-l", "fresh")
    screen = capture(pane, "❯ fresh")
    assert "draft" not in screen


LONG_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    for i in range(80):
        yield f"LINE_{i:03d}\\n"
        await asyncio.sleep(0.005)
    yield "**UNCHANGED MARKDOWN** " + "wide界 " * 60 + "TAIL_MARKER"
    await asyncio.sleep(1)
    yield " FINISHED"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [LONG_SCRIPT], indirect=True)
def test_long_stream_remains_in_scrollback_without_truncation(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "TAIL_MARKER", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "still editable")
    screen = capture(pane, "FINISHED")
    assert "❯ still editable" in screen
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    for i in range(80):
        assert history.count(f"LINE_{i:03d}") == 1
    assert history.count("**UNCHANGED MARKDOWN**") == 1
    assert history.count("TAIL_MARKER") == 1


WORD_WRAP_SCRIPT = """
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "WRAP_START\\n"
    for char in "streaming boundaries " * 30:
        yield char
    yield "\\nWRAP_END"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("pane", [WORD_WRAP_SCRIPT], indirect=True)
def test_words_stay_whole_in_regular_and_split_panes(pane, split):
    capture(pane, "❯")
    if split:
        pane("split-window", "-h", "-t", "preview:0.0", "cat")
        capture(pane, "❯", columns=50)
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "WRAP_END")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    text = history.split("WRAP_START\n", 1)[1].split("WRAP_END", 1)[0]
    assert text.split() == ["streaming", "boundaries"] * 30
