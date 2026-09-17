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
            and "effort:" in lines[-1]
            and (("working" in lines[-1]) == running)
        ):
            return screen
        time.sleep(0.05)
    pytest.fail(f"Prompt did not settle with {expected!r}:\n{screen}")


def input_rows(screen):
    lines = screen.splitlines()
    assert "effort:" in lines[-1], screen
    assert lines[-2].startswith("└"), screen
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("┌"))
    bottom = next(i for i, line in enumerate(lines[top + 1 :], top + 1) if line.startswith("└"))
    return bottom - top - 1


def test_footer_theme_switch_keeps_editor_compact(pane):
    assert input_rows(capture(pane, "❯")) == 1
    for theme in ("light", "dark"):
        pane("send-keys", "-t", "preview:0.0", "-l", f"/theme {theme}")
        pane("send-keys", "-t", "preview:0.0", "Enter")
        screen = capture(pane, f"Theme: {theme}.")
        assert input_rows(screen) == 1
        assert "preview · effort: n/a" in screen.splitlines()[-1]


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
    yield "COMMITTED LINE\\n\\nFIRST STREAM CHUNK"
    await asyncio.sleep(2)
    yield "\\n\\nLIVE ANSWER COMPLETE"

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
    assert "COMMITTED LINE" in streaming
    assert any(
        line.rstrip("│ ").endswith(" hello") and line.startswith(tuple("│" + f for f in "◜◠◝◞◡◟"))
        for line in streaming.splitlines()
    )
    before = streaming.splitlines().index("FIRST STREAM CHUNK")
    completed = capture(pane, "LIVE ANSWER COMPLETE")
    assert input_rows(completed) == 1
    assert "✓ hello" in completed
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
        yield f"LINE_{i:03d}\\n\\n"
        await asyncio.sleep(0.005)
    yield "**FORMATTED MARKDOWN** " + "wide界 " * 60 + "TAIL_MARKER"
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
    assert history.count("FORMATTED MARKDOWN") == 1
    assert "**FORMATTED MARKDOWN**" not in history
    assert history.count("TAIL_MARKER") == 1


WORD_WRAP_SCRIPT = """
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "WRAP_START\\n\\n"
    for char in "streaming boundaries " * 30:
        yield char
    yield "\\n\\nWRAP_END"

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


CURSOR_SCRIPT = """
import asyncio
import time
from rich.console import Console
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

class SlowConsole(Console):
    def print(self, *objects, **kwargs):
        super().print(*objects, **kwargs)
        if any("CURSOR_LINE_" in getattr(obj, "markup", str(obj)) for obj in objects):
            # Enlarge the handoff window so cursor visibility can be sampled
            # deterministically, even on a fast terminal.
            time.sleep(0.15)

async def model(messages, info):
    await asyncio.sleep(0.5)
    for i in range(12):
        yield f"CURSOR_LINE_{i:03d}\\n\\n"
        await asyncio.sleep(0.05)
    yield "CURSOR_STREAM_DONE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime, console=SlowConsole()).run()
"""


@pytest.mark.parametrize("pane", [CURSOR_SCRIPT], indirect=True)
def test_cursor_is_hidden_while_committing_stream_and_returns_to_draft(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "draft text")
    pane("send-keys", "-t", "preview:0.0", "Left", "Left", "Left", "Left")
    capture(pane, "❯ draft text", running=True)
    samples = 0
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        # These commands run in one tmux invocation, so the screen and cursor
        # mode describe the same terminal state rather than different paints.
        snapshot = pane(
            "capture-pane",
            "-p",
            "-t",
            "preview:0.0",
            ";",
            "display-message",
            "-p",
            "-t",
            "preview:0.0",
            "CURSOR_STATE #{cursor_flag} #{cursor_x} #{cursor_y}",
        )
        screen, state = snapshot.rsplit("CURSOR_STATE ", 1)
        visible, x, y = map(int, state.split())
        lines = screen.splitlines()
        if "CURSOR_LINE_" in screen and not any(line.startswith("│❯") for line in lines):
            samples += 1
            assert not visible, snapshot
        if "CURSOR_STREAM_DONE" in screen and "effort:" in lines[-1] and "working" not in lines[-1]:
            assert visible, snapshot
            assert lines[y].startswith("│❯ draft text"), snapshot
            assert x == 9, snapshot  # Three-cell prompt plus 'draft '.
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"Stream did not complete:\n{snapshot}")
    assert samples > 0, "Did not observe a transcript handoff"


MARKDOWN_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "**Styled response**\\n\\n```python\\n"
    for i in range(60):
        yield f"value_{i:03d} = {i}\\n"
    yield "# PREVIEW_MARKER"
    await asyncio.sleep(2)
    yield "\\n```\\n\\nMARKDOWN_DONE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [MARKDOWN_SCRIPT], indirect=True)
def test_markdown_code_preview_stays_bounded_and_commits_once(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "PREVIEW_MARKER", running=True)
    assert input_rows(screen) == 1
    assert "**Styled response**" not in screen
    assert "value_000" not in screen  # Still buffered, not a pane-sized live block.
    pane("send-keys", "-t", "preview:0.0", "-l", "draft survives")
    assert input_rows(capture(pane, "MARKDOWN_DONE")) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    for i in range(60):
        assert history.count(f"value_{i:03d}") == 1
    assert history.count("PREVIEW_MARKER") == 1
    assert "```" not in history
    assert "❯ draft survives" in history


def single_editor_history(pane, marker, *, frames=1):
    """History-inclusive check; a resize erase/repaint is not an atomic write."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        if history.count("┌") == history.count("└") == frames and history.count(marker) == 1:
            return history
        time.sleep(0.05)
    pytest.fail(
        f"Expected {frames} frames and one {marker!r} across screen and history:\n{history}"
    )


@pytest.mark.parametrize("multiline", [False, True])
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_repeated_resize_keeps_one_editor_frame_and_draft(pane, multiline, split):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "RESIZE_DRAFT")
    if multiline:
        pane("send-keys", "-t", "preview:0.0", "Escape", "Enter")
        pane("send-keys", "-t", "preview:0.0", "-l", "second line " * 8)
    capture(pane, "RESIZE_DRAFT")
    for _ in range(3):
        pane("split-window", split, "-t", "preview:0.0", "cat")
        width = int(pane("display-message", "-p", "-t", "preview:0.0", "#{pane_width}"))
        capture(pane, "RESIZE_DRAFT", columns=width)
        single_editor_history(pane, "RESIZE_DRAFT")
        pane("kill-pane", "-t", "preview:0.1")
        capture(pane, "RESIZE_DRAFT", columns=100)
        single_editor_history(pane, "RESIZE_DRAFT")
    pane("send-keys", "-t", "preview:0.0", "C-a")
    pane("send-keys", "-t", "preview:0.0", "-l", "inserted ")
    capture(pane, "inserted " + ("second line" if multiline else "RESIZE_DRAFT"))


PLAN_SCRIPT = r"""
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import PlanUpdated, ToolSummary

class Runtime:
    session = None
    async def stream(self, prompt):
        items = [{"id": str(i), "content": f"Task {i}", "status": "pending"} for i in range(12)]
        items[8]["status"] = "in_progress"
        yield PlanUpdated(items)
        yield ToolSummary("write_plan", "Plan updated")
        await asyncio.sleep(2)
        yield PlanUpdated([dict(item, status="completed") for item in items])
    def reset(self):
        pass

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [PLAN_SCRIPT], indirect=True)
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_plan_panel_is_bounded_updates_and_clears(pane, split):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Task 8", running=True)
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    first_frame = next(frame for frame in frames if f"{frame} Task 8" in screen)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        animated = pane("capture-pane", "-p", "-t", "preview:0.0")
        if any(f"{frame} Task 8" in animated for frame in frames if frame != first_frame):
            break
        time.sleep(0.03)
    else:
        pytest.fail("Active plan spinner did not animate while waiting for a tool")
    assert "Tasks ·" not in screen and "Tools" not in screen
    lines = screen.splitlines()
    first_task = next(i for i, line in enumerate(lines) if "Task 6" in line)
    assert lines[first_task - 2].startswith("┌")
    assert lines[first_task - 1].startswith("│")
    assert lines[first_task - 1].rstrip("│ ").endswith(" h")
    assert all(
        line.startswith("│") and line.endswith("│") for line in lines[first_task : first_task + 5]
    )
    assert lines[first_task + 5].startswith("└")
    assert input_rows(screen) == 1
    assert "Task 0\n" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "editable draft")
    pane("split-window", split, "-t", "preview:0.0", "cat")
    completed = capture(pane, "✓ Task 0")
    assert input_rows(completed) == 1
    assert completed.count("┌") == completed.count("└") == 2
    pane("kill-pane", "-t", "preview:0.1")
    capture(pane, "editable draft", columns=100)
    history = single_editor_history(pane, "editable draft", frames=2)
    assert "Plan updated" not in history
    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "-l", "/new")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    screen = capture(pane, "Context reset")
    assert "Tasks ·" not in screen
    assert "Task 0" not in screen
    # /new zeroes the whole widget: tasks, tools, and the previous prompt row.
    assert "✓ h" not in screen
    assert screen.count("┌") == screen.count("└") == 1


PAUSED_PREVIEW_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "COMMITTED MARKER\\n\\n" + "x" * 50 + "PAUSED_TAIL"
    await asyncio.sleep(30)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [PAUSED_PREVIEW_SCRIPT], indirect=True)
def test_paused_preview_reflows_on_resize_without_more_tokens(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "PAUSED_TAIL", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "PAUSED_TAIL", running=True, columns=columns)
        # Check the active preview region, not resize ghosting in old history
        # (covered separately by the existing strict-xfail regression).
        lines = screen[: screen.rindex("┌")].splitlines()
        assert lines.pop().startswith("└")
        assert lines.pop().rstrip("│ ").endswith(" h")  # Prompt-only task widget.
        assert lines.pop().startswith("┌")
        tail_row = max(i for i, line in enumerate(lines) if line.strip())
        expected_length = 61 % columns or columns
        assert lines[tail_row] == "x" * (expected_length - 11) + "PAUSED_TAIL", screen
        assert tail_row == 0 or not lines[tail_row - 1].strip(), screen
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1


TOOLS_SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import TextDelta, ToolStarted, ToolSummary

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        yield TextDelta("MODEL CONVERSATION ONLY\\n\\n")
        for i in range(1, 13):
            yield ToolStarted("read_file", f"file_{i:02d}.py", str(i))
            await asyncio.sleep(0.02)
            yield ToolSummary(
                "read_file", f"file_{i:02d}.py", call_id=str(i), failed=i == 12,
                error="INSPECTABLE ERROR" if i == 12 else "",
            )
        yield ToolStarted("run_command", "waiting", "13",
                          command="printf FIRST_DETAIL\\nprintf SECOND_DETAIL")
        await asyncio.sleep(30)

app = PreviewApp(model="test:local", runtime=Runtime())
app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]
app.run()
"""


@pytest.mark.parametrize("pane", [TOOLS_SCRIPT], indirect=True)
def test_recent_tools_are_nested_below_prompt_header_in_task_widget(pane):
    initial = capture(pane, "A task")
    assert "Tools" not in initial and "Tasks ·" not in initial
    assert initial.count("┌") == initial.count("└") == 2
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "⟳ Run", running=True)
    lines = screen.splitlines()
    task = next(i for i, line in enumerate(lines) if "A task" in line)
    assert lines[task - 2].startswith("┌")
    assert lines[task - 1].rstrip("│ ").endswith(" h")
    assert lines[task + 1].startswith("│      ✓ Read · file_11.py")
    assert lines[task + 2].startswith("│      ! Read failed · file_12.py")
    assert lines[task + 3].startswith("│      ⟳ Run")
    assert lines[task + 4].startswith("└")
    assert lines[task + 5].startswith("┌")  # Editor, not another Tools widget.
    assert "Tools" not in screen and "Tasks ·" not in screen
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "file_01.py" not in history
    assert history.count("file_11.py") == 1
    assert "INSPECTABLE ERROR" not in history

    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 20), (100, 32), (40, 14)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        count = 3
        deadline = time.monotonic() + 3
        while True:
            screen = capture(pane, "A task", running=True, columns=width)
            lines = screen.splitlines()
            # Inspect the live widget nearest the editor, not old resize ghosts.
            task = max(i for i, line in enumerate(lines) if "A task" in line)
            if lines[task + count + 1].startswith("└"):
                break
            assert time.monotonic() < deadline, screen
            time.sleep(0.05)
        assert all(line.startswith("│      ") for line in lines[task + 1 : task + count + 1])
        assert "keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled.")
    assert "Run · interrupted" in screen
    assert "│❯ keep draft" in screen


RESIZE_TRANSCRIPT_SCRIPT = TOOLS_SCRIPT.replace(
    'yield TextDelta("MODEL CONVERSATION ONLY\\n\\n")',
    'yield TextDelta("".join(f"RESIZE_TRANSCRIPT_{i:03d}\\n\\n" for i in range(40)))',
)


@pytest.mark.xfail(
    strict=True,
    reason="tmux narrowing reflows old task rows beyond prompt_toolkit's resize erase",
)
@pytest.mark.parametrize("pane", [RESIZE_TRANSCRIPT_SCRIPT], indirect=True)
def test_empty_input_resize_preserves_transcript_without_task_ghosts(pane):
    capture(pane, "A task")
    pane("resize-window", "-t", "preview:0", "-x", "240", "-y", "40")
    capture(pane, "A task", columns=240)
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "⟳ Run", running=True, columns=240)

    for width, height in ((120, 24), (240, 40), (80, 24), (240, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "⟳ Run", running=True, columns=width)
        assert input_rows(screen) == 1
        editor = next(line for line in screen.splitlines() if line.startswith("│❯"))
        assert editor[2:-1].strip() == ""  # No multiline draft needed to trigger this.
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        # A viewport clear can remove the ghost while destroying real output.
        for i in range(40):
            marker = f"RESIZE_TRANSCRIPT_{i:03d}"
            assert history.count(marker) == 1, f"Lost or duplicated {marker}:\n{history}"
        history = single_editor_history(pane, "A task", frames=2)
        assert history.count("file_11.py") == 1


@pytest.mark.parametrize(
    "pane",
    [
        LIVE_SCRIPT.replace(
            'yield "\\n\\nLIVE ANSWER COMPLETE"', 'raise RuntimeError("model broke")'
        )
    ],
    indirect=True,
)
def test_failed_prompt_indicator_stays_visible(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "FIRST STREAM CHUNK", running=True)
    failed = capture(pane, "! hello · failed")
    assert input_rows(failed) == 1
    assert "Run failed" in failed


@pytest.mark.parametrize("pane", [PAUSED_PREVIEW_SCRIPT], indirect=True)
def test_prompt_header_stays_one_line_and_truncates_on_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "LONG PROMPT " * 30)
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "PAUSED_TAIL", running=True)
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "PAUSED_TAIL", running=True, columns=columns)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        assert lines[editor_top - 3].startswith("┌")
        header = lines[editor_top - 2]
        assert header.startswith("│") and header.endswith("…│")
        assert "LONG PROMPT" in header
        assert len(header) == columns
        assert lines[editor_top - 1].startswith("└")
        assert input_rows(screen) == 1


@pytest.mark.parametrize("pane", [PAUSED_PREVIEW_SCRIPT], indirect=True)
def test_queued_messages_stay_directly_above_editor(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "active prompt")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "PAUSED_TAIL", running=True)
    for text in ("first queued message " * 10, "second queued message"):
        pane("send-keys", "-t", "preview:0.0", "-l", text)
        pane("send-keys", "-t", "preview:0.0", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width in (100, 40):
        pane("resize-window", "-t", "preview:0", "-x", str(width))
        screen = capture(pane, "│❯ keep draft", running=True, columns=width)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        assert lines[editor_top - 2].startswith("Queued: first queued message")
        assert lines[editor_top - 2].endswith("…")
        assert lines[editor_top - 1] == "Queued: second queued message"
        assert lines[editor_top - 3].startswith("└")
        assert "active prompt" in lines[editor_top - 4]
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled.")
    assert "Queued:" not in screen
    assert "│❯ keep draft" in screen
