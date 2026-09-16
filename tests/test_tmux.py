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


def test_fullscreen_transcript_scrolls(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/demo")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "No files were")) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Hello, world!" in history
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    pane("send-keys", "-t", "preview:0.0", "PPage")
    capture(pane, "pcode  /  UI preview")
    pane("send-keys", "-t", "preview:0.0", "C-End")
    capture(pane, "No files were")


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
    yield "FIRST STREAM CHUNK"
    await asyncio.sleep(1)
    yield "\\nLIVE ANSWER COMPLETE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_keeps_prompt_at_bottom_and_commits_once(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "FIRST STREAM CHUNK", running=True)) == 1
    assert input_rows(capture(pane, "LIVE ANSWER COMPLETE")) == 1
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
    pane("send-keys", "-t", "preview:0.0", "C-c")
    assert input_rows(capture(pane, "Run cancelled.")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "next input")
    assert input_rows(capture(pane, "next input")) == 1


REFLOW_SCRIPT = """
from pcode.app import PreviewApp
from pcode.runtime import Message
app = PreviewApp()
app.transcript.full_screen = True
app.transcript.events((Message("REFLOW_START " + "word " * 45 + "REFLOW_END"),))
app.run()
"""


@pytest.mark.parametrize("pane", [REFLOW_SCRIPT], indirect=True)
def test_completed_response_reflows_on_width_resize(pane):
    def response_lines(screen):
        lines = screen.splitlines()
        start = next(i for i, line in enumerate(lines) if "REFLOW_START" in line)
        end = next(i for i, line in enumerate(lines) if "REFLOW_END" in line)
        return lines[start : end + 1]

    wide = response_lines(capture(pane, "REFLOW_END"))
    pane("split-window", "-h", "-t", "preview:0.0", "cat")
    narrow_screen = capture(pane, "REFLOW_END")
    narrow = response_lines(narrow_screen)
    assert input_rows(narrow_screen) == 1
    assert len(narrow) > len(wide)
    assert " ".join(" ".join(narrow).split()) == " ".join(" ".join(wide).split())
    pane("kill-pane", "-t", "preview:0.1")
    assert response_lines(capture(pane, "REFLOW_END", columns=100)) == wide


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_immediate_cancellation_unlocks_editor(pane):
    capture(pane, "❯")
    # Deliver submission and cancellation together, before the model task can start.
    pane("send-keys", "-t", "preview:0.0", "h", "Enter", "C-c")
    capture(pane, "Run cancelled.")
    pane("send-keys", "-t", "preview:0.0", "-l", "editable again")
    assert input_rows(capture(pane, "editable again")) == 1


def test_cancel_history_search_discards_draft(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "draft")
    pane("send-keys", "-t", "preview:0.0", "C-r", "C-c")
    capture(pane, "Input discarded.")
    pane("send-keys", "-t", "preview:0.0", "-l", "fresh")
    screen = capture(pane, "❯ fresh")
    assert "draft" not in screen
