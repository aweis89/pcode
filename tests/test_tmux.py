"""Layout regression in real tmux, including cursor-position reports (CPR)."""

import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest
from conftest import tmux_socket_dir

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def tmux_reaper(server, owner_pid):
    """Kill `server` once `owner_pid` exits, even if that exit skips teardown.

    A `-L` server detaches from pytest, so the fixture's `finally` is the only
    thing that stops it -- and that never runs under SIGKILL or a hard timeout.
    `start_new_session` keeps this reaper out of pytest's process group so a
    group-wide kill cannot take the reaper down with its owner.
    """
    script = (
        f"while kill -0 {owner_pid} 2>/dev/null; do sleep 2; done; "
        f"tmux -L {shlex.quote(server)} -f /dev/null kill-server 2>/dev/null"
    )
    return subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def pane(request):
    server = "pcode-test-" + uuid.uuid4().hex
    base = ["tmux", "-L", server, "-f", "/dev/null"]
    env = {**os.environ}
    # These tests exist because a PTY with PROMPT_TOOLKIT_NO_CPR=1 (as in
    # test_terminal.py) never exercises real prompt height: cursor-position
    # reports can stretch the layout into the remaining pane. Keep them on a
    # real tmux, and keep them serial: pane-paint deadlines expire under
    # xdist, even `-n 4`.
    env.pop("PROMPT_TOOLKIT_NO_CPR", None)
    # Most panes assert on the widget while the prompt is idle, so opt them out
    # of the default auto-hide; a test that wants it turns it back on itself.
    config = pathlib.Path(env["XDG_CONFIG_HOME"]) / "pcode"
    config.mkdir(parents=True, exist_ok=True)
    # The pane runs from this checkout, which ships .pcode/worktree-setup; trust
    # it up front or the launch prompt blocks the pane. Panes capture the screen
    # the moment a marker shows and expect scrollback to be complete at that
    # instant, so paced scrollback is off; its own test turns it on.
    config.joinpath("preferences.json").write_text(
        json.dumps({"autohide_tasks": "off", "project_extensions": "on", "paced_scrollback": "off"})
    )
    reaper = tmux_reaper(server, os.getpid())

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
        # kill-server returns before the server is gone, so a surviving server
        # here is wedged rather than merely slow.
        deadline = time.monotonic() + 5
        while subprocess.run([*base, "list-sessions"], capture_output=True).returncode == 0:
            if time.monotonic() > deadline:
                subprocess.run([*base, "kill-server"], capture_output=True, env=env)
                break
            time.sleep(0.05)
        (tmux_socket_dir() / server).unlink(missing_ok=True)
        os.killpg(reaper.pid, signal.SIGTERM)  # The reaper leads its own group.
        reaper.wait(timeout=5)


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
            and "Enter:" in lines[-1]
            # Mode and activity have priority even in narrow real-CPR panes.
            and (("working" in lines[-1]) == running)
        ):
            return screen
        time.sleep(0.05)
    pytest.fail(f"Prompt did not settle with {expected!r}:\n{screen}")


# The last row `/theme-preview` writes: everything above it can scroll away.
GALLERY_TAIL = "pcode config set syntax_dark NAME"


def scrollback(pane):
    """History above the visible screen.

    The live panel is not scrollback, so a plain ``-S -`` capture (which
    includes the visible screen) cannot answer "did this reach history?" for
    anything the panel draws, such as an expiring notice.
    """
    return pane("capture-pane", "-p", "-S", "-", "-E", "-1", "-t", "preview:0.0")


def input_rows(screen):
    lines = screen.splitlines()
    assert "Enter:" in lines[-1], screen
    assert lines[-2].startswith("└"), screen
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("┌"))
    bottom = next(i for i, line in enumerate(lines[top + 1 :], top + 1) if line.startswith("└"))
    return bottom - top - 1


def test_footer_theme_switch_keeps_editor_compact(pane):
    assert input_rows(capture(pane, "❯")) == 1
    for colors in ("terminal", "palette"):
        pane("send-keys", "-t", "preview:0.0", "-l", f"/colors {colors}")
        pane("send-keys", "-t", "preview:0.0", "Enter")
        assert input_rows(capture(pane, f"Colors: {colors}.")) == 1
        for theme in ("light", "dark", "auto"):
            pane("send-keys", "-t", "preview:0.0", "-l", f"/theme {theme}")
            pane("send-keys", "-t", "preview:0.0", "Enter")
            screen = capture(pane, "Theme: auto (" if theme == "auto" else f"Theme: {theme}.")
            assert input_rows(screen) == 1
            assert "· preview" in screen.splitlines()[-1]
            pane("send-keys", "-t", "preview:0.0", "-l", "/theme-preview")
            pane("send-keys", "-t", "preview:0.0", "Enter")
            # The style gallery scrolls the sample away, so wait on its last row.
            assert input_rows(capture(pane, GALLERY_TAIL)) == 1


def test_transcript_uses_terminal_scrollback(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/theme-preview")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, GALLERY_TAIL)) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Hello, world!" in history
    assert "No files were" in history
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
    screen = capture(pane, "\n /help ")
    assert input_rows(screen) == 1
    # The menu shows at most six commands, so assert on one that is always in
    # view rather than a lower entry that a new command can push off the list.
    assert screen.index("\n /help ") < screen.rindex("┌")  # Menu above the fixed frame.

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
    streaming = capture(pane, "COMMITTED LINE", running=True)
    assert input_rows(streaming) == 1
    assert "COMMITTED LINE" in streaming
    # The spinner row reports live work; the prompt itself is already in scrollback.
    assert any(line.startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) for line in streaming.splitlines())
    assert not any(
        line.startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) and line.endswith(" hello")
        for line in streaming.splitlines()
    )
    assert "▌ hello" in streaming
    assert "FIRST STREAM CHUNK" not in streaming
    completed = capture(pane, "LIVE ANSWER COMPLETE")
    assert input_rows(completed) == 1
    assert "✓ hello" not in completed
    assert "FIRST STREAM CHUNK" in completed
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("FIRST STREAM CHUNK") == 1
    assert "▌ hello" in history


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_resize_and_cancellation(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    pane("split-window", "-v", "-t", "preview:0.0", "cat")
    assert input_rows(capture(pane, "❯", running=True)) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "next input")
    capture(pane, "❯ next input", running=True)
    # Ctrl+C clears the draft first; only an empty prompt cancels the run.
    pane("send-keys", "-t", "preview:0.0", "C-c")
    discarded = capture(pane, "Input discarded", running=True)
    assert "❯ next input" not in discarded
    pane("send-keys", "-t", "preview:0.0", "C-c")
    cancelled = capture(pane, "! Run cancelled")
    assert input_rows(cancelled) == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_draft_and_cursor_survive_stream_completion_and_width_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
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
    capture(pane, "! Run cancelled")
    pane("send-keys", "-t", "preview:0.0", "-l", "editable again")
    assert input_rows(capture(pane, "editable again")) == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_width_resize_does_not_leave_a_copy_of_unfinished_line(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
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
    screen = capture(pane, "LINE_079", running=True)
    assert "TAIL_MARKER" not in screen
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
        if "CURSOR_STREAM_DONE" in screen and "Enter:" in lines[-1] and "working" not in lines[-1]:
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
def test_markdown_code_stays_hidden_until_committed_once(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Styled response", running=True)
    assert "PREVIEW_MARKER" not in screen
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


PACED_SCRIPT = """
from pcode.app import PreviewApp
from pcode.preferences import save_preferences
from pcode.runtime import Message, TextDelta

save_preferences(paced_scrollback="on")

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        self.turns += 1
        if self.turns == 1:
            block = "\\n".join(f"PACED_ROW_{i:03d}" for i in range(200))
            yield TextDelta("```text\\n" + block + "\\n```\\n\\nBLOCK_SETTLED\\n\\n")
            yield Message("")
        else:
            yield Message("SECOND_TURN_DONE")

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [PACED_SCRIPT], indirect=True)
def test_paced_scrollback_rolls_a_block_out_and_keeps_taking_input(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    deadline = time.monotonic() + 3
    while "PACED_ROW_000" not in scrollback(pane):
        assert time.monotonic() < deadline, "The block never started to appear"
        time.sleep(0.02)
    # The block is written a few rows per frame, so the first row shows while
    # the last is still queued.
    assert "PACED_ROW_199" not in scrollback(pane)
    # Typing during the roll-out reaches the editor unchanged: the handoffs stay
    # in raw mode, so Return submits rather than landing as a newline.
    pane("send-keys", "-t", "preview:0.0", "-l", "next")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "SECOND_TURN_DONE")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    positions = [history.index(f"PACED_ROW_{i:03d}") for i in range(200)]
    assert positions == sorted(positions)
    assert history.count("PACED_ROW_199") == 1
    assert positions[-1] < history.index("BLOCK_SETTLED") < history.index("▌ next")
    assert history.index("▌ next") < history.index("SECOND_TURN_DONE")


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
    frames = "◜◠◝◞◡◟"
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
    assert lines[first_task - 2].startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
    assert not lines[first_task - 2].startswith("│")
    assert lines[first_task - 1].startswith("┌")
    assert lines[first_task - 1].startswith("┌─ Tasks 0/12 ─")
    assert all(
        line.startswith("│") and line.endswith("│") for line in lines[first_task : first_task + 5]
    )
    assert lines[first_task + 5].startswith("└")
    assert input_rows(screen) == 1
    assert "Task 0\n" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "editable draft")
    pane("split-window", split, "-t", "preview:0.0", "cat")
    completed = capture(pane, "✓ Task 0")
    assert any(line.startswith("┌─ Tasks 12/12 ─") for line in completed.splitlines())
    assert "│✓ Task 0" in completed
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
    assert "Tasks" not in screen
    assert "Task 0" not in screen
    # /new zeroes the whole widget: tasks, tools, and the live status row.
    assert "✓ h" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    # The clear reaches scrollback too, not just the visible rows.
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Type / for commands" not in history


PAUSED_STREAM_SCRIPT = """
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


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
def test_paused_stream_stays_hidden_on_resize_without_more_tokens(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "❯", running=True, columns=columns)
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        assert "PAUSED_TAIL" not in history
        assert "x" * 20 not in history
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1


TOOLS_SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.preferences import save_preferences
from pcode.runtime import TextDelta, ToolStarted, ToolSummary

save_preferences(tool_error_scrollback="on")

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
def test_prompt_sits_above_left_aligned_task_header_and_nested_tools(pane):
    initial = capture(pane, "A task")
    assert "Tools" not in initial and "┌─ Tasks 0/1 ─" in initial
    assert initial.count("┌") == initial.count("└") == 2
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Run · ", running=True)
    lines = screen.splitlines()
    task = next(i for i, line in enumerate(lines) if "A task" in line)
    # The running command owns the status row; the widget holds tasks alone.
    status = lines[task - 2]
    assert status.startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) and "Run" in status
    assert not status.startswith("│")
    assert lines[task - 1].startswith("┌─ Tasks 0/1 ─")
    assert lines[task].startswith("│") and lines[task][1] in "◜◠◝◞◡◟"
    assert lines[task + 1].startswith("└")
    assert lines[task + 2].startswith("┌")  # Editor, not another Tools widget.
    assert "Tools" not in screen and "Tasks ·" not in screen
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("file_11.py") == 1
    assert history.count("INSPECTABLE ERROR") == 1
    assert "✗ Read failed" in history

    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 20), (100, 32), (40, 14)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        deadline = time.monotonic() + 3
        while True:
            screen = capture(pane, "A task", running=True, columns=width)
            lines = screen.splitlines()
            # Inspect the live widget nearest the editor, not old resize ghosts.
            task = max(i for i, line in enumerate(lines) if "A task" in line)
            if lines[task + 1].startswith("└"):
                break
            assert time.monotonic() < deadline, screen
            time.sleep(0.05)
        assert lines[task].startswith("│") and lines[task][1] in "◜◠◝◞◡◟"
        assert "keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")  # Clears the draft.
    assert "keep draft" not in capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    assert "Run · " not in screen


RESIZE_TRANSCRIPT_SCRIPT = TOOLS_SCRIPT.replace(
    'yield TextDelta("MODEL CONVERSATION ONLY\\n\\n")',
    'yield TextDelta("".join(f"RESIZE_TRANSCRIPT_{i:03d}\\n\\n" for i in range(40)))',
)


@pytest.mark.parametrize("pane", [RESIZE_TRANSCRIPT_SCRIPT], indirect=True)
def test_empty_input_resize_preserves_transcript_without_task_ghosts(pane):
    """tmux splits old full-width rows on narrowing; the erase must cover them."""
    capture(pane, "A task")
    pane("resize-window", "-t", "preview:0", "-x", "240", "-y", "40")
    capture(pane, "A task", columns=240)
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "Run · ", running=True, columns=240)

    for width, height in ((120, 24), (240, 40), (80, 24), (240, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "Run · ", running=True, columns=width)
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
def test_failure_is_reported_in_scrollback_and_clears_the_status_row(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    failed = capture(pane, "✗ Agent failed")
    assert input_rows(failed) == 1
    assert "Run failed" in failed
    # The turn is over, so no spinner row survives above the editor.
    assert not any(line.startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")) for line in failed.splitlines())
    assert "▌ hello" in failed


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
def test_prompt_header_stays_one_line_and_truncates_on_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "LONG PROMPT " * 30)
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "❯", running=True, columns=columns)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        header = lines[editor_top - 1]
        # One status row, never the echoed prompt, and never wider than the pane.
        assert not header.startswith("│")
        assert header.startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
        assert "LONG PROMPT" not in header
        assert len(header) <= columns
        assert screen.count("┌") == screen.count("└") == 1
        assert input_rows(screen) == 1


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
@pytest.mark.parametrize("mode", ["queue", "steering"])
def test_queued_messages_stay_directly_above_editor(pane, mode):
    capture(pane, "❯")
    if mode == "queue":
        pane("send-keys", "-t", "preview:0.0", "C-s")
    label = "Queued" if mode == "queue" else "Steering (next model request)"
    pane("send-keys", "-t", "preview:0.0", "-l", "active prompt")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    for text in ("first queued message " * 10, "second queued message"):
        pane("send-keys", "-t", "preview:0.0", "-l", text)
        pane("send-keys", "-t", "preview:0.0", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width in (100, 40):
        pane("resize-window", "-t", "preview:0", "-x", str(width))
        screen = capture(pane, "│❯ keep draft", running=True, columns=width)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        assert lines[editor_top - 2].startswith(f"{label}: first")
        assert lines[editor_top - 2].endswith("…")
        assert lines[editor_top - 1].startswith(f"{label}: second")
        assert lines[editor_top - 3].startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
        assert "active prompt" not in lines[editor_top - 3]
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")  # Clears the draft.
    assert "keep draft" not in capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    assert f"{label}:" not in screen


@pytest.mark.parametrize(
    "pane",
    [
        TOOLS_SCRIPT.replace(
            'app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]', ""
        )
    ],
    indirect=True,
)
def test_single_running_tool_needs_no_box_above_the_editor(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Run · ", running=True)
    lines = screen.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith("┌"))
    # Only the editor is boxed: the lone running call lives on the status row.
    assert screen.count("┌") == screen.count("└") == 1
    assert lines[top - 1].startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
    assert "Run · " in lines[top - 1]
    assert "Tasks" not in screen and "Tools" not in screen
    assert "✓ Read" in pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert input_rows(screen) == 1


SPACING_SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import ToolStarted, ToolSummary

class Runtime:
    session = None

    async def stream(self, prompt):
        for i in range(1, 31):
            yield ToolStarted("read_file", f"file_{i:02d}.py", str(i))
            yield ToolSummary("read_file", f"file_{i:02d}.py", call_id=str(i))
        yield ToolStarted("read_file", "SLOW_FILE", "31")
        await asyncio.sleep(30)

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SPACING_SCRIPT], indirect=True)
def test_status_row_keeps_a_blank_line_below_the_last_tool_line(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "SLOW_FILE", running=True)
    lines = screen.splitlines()
    status = next(i for i, line in enumerate(lines) if "SLOW_FILE" in line)
    assert lines[status].startswith(tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
    assert lines[status - 1].strip() == ""
    assert "✓ Read  file_30.py" in lines[status - 2]


IMMEDIATE_PROMPT_SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import TextDelta, ToolStarted, Message

class Runtime:
    session = None
    async def stream(self, prompt):
        yield ToolStarted("read_file", "WAITING FOR FIRST MESSAGE", "one")
        yield TextDelta("FIRST MODEL")
        await asyncio.sleep(2)
        yield TextDelta(" MESSAGE\\n\\n")
        await asyncio.sleep(2)
        yield Message("FIRST MODEL MESSAGE")

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [IMMEDIATE_PROMPT_SCRIPT], indirect=True)
def test_scrollback_quote_is_committed_on_send_with_a_blank_line_after_it(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "sent prompt")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    waiting = capture(pane, "WAITING FOR FIRST MESSAGE", running=True)
    assert "▌ sent prompt" in waiting
    assert "FIRST MODEL" not in waiting
    assert input_rows(waiting) == 1
    response = capture(pane, "FIRST MODEL MESSAGE", running=True)
    assert "▌ sent prompt\n\nFIRST MODEL MESSAGE" in response
    assert input_rows(response) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("▌ sent prompt") == 1
    assert history.count("FIRST MODEL MESSAGE") == 1


STREAMED_PLAN_SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import Planning
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield {0: DeltaToolCall(name="write_plan", json_args=
        '{"items":[{"content":"STREAMED_TASK", "status":"in_progress"},')}
    # Neither the arguments nor the model response ever finish before cancellation.
    await asyncio.sleep(60)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[Planning()]))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [STREAMED_PLAN_SCRIPT], indirect=True)
def test_streamed_task_preview_has_real_prompt_height_and_cancels_cleanly(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "STREAMED_TASK", running=True)
    assert "Tasks 0/1" in screen
    assert input_rows(screen) == 1
    assert screen.count("┌") == screen.count("└") == 2
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "STREAMED_TASK" not in screen
    assert "Tasks 0/1" not in screen
    assert input_rows(screen) == 1
    assert screen.count("┌") == screen.count("└") == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "STREAMED_TASK" not in history


THINKING_SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield {0: DeltaThinkingPart(content="SAVED_REASONING_TEXT\n")}
    await asyncio.sleep(1)
    yield "Public answer while thinking is visible\n\n"
    await asyncio.sleep(60)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
app = PreviewApp(model="test:local", runtime=runtime)
app.activity.show_thinking = False
app.persist_defaults = lambda **updates: None
app.run()
"""


@pytest.mark.parametrize("pane", [THINKING_SCRIPT], indirect=True)
def test_thinking_toggle_redraws_scrollback_without_growing_prompt(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "❯", running=True)
    assert "SAVED_REASONING_TEXT" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-t")
    screen = capture(pane, "SAVED_REASONING_TEXT", running=True)
    assert input_rows(screen) == 1
    capture(pane, "Public answer while thinking is visible", running=True)
    for width, height in ((80, 24), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "SAVED_REASONING_TEXT", running=True, columns=width)
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-t")
    time.sleep(0.2)
    screen = capture(pane, "❯", running=True)
    assert "SAVED_REASONING_TEXT" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-t")
    capture(pane, "SAVED_REASONING_TEXT", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "SAVED_REASONING_TEXT" in screen
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "SAVED_REASONING_TEXT" in history


@pytest.mark.parametrize(
    "pane",
    [
        "from pcode.preferences import save_preferences; "
        "save_preferences(editing_mode='vi'); "
        "from pcode.app import main; main()"
    ],
    indirect=True,
)
@pytest.mark.parametrize(
    "newline", ["\n", "\x1b[106;5u", "\x1b[27;5;106~", "\x1b[13;2u", "\x1b[27;2;13~"]
)
def test_vi_newline_and_escape_keep_editor_compact(pane, newline):
    screen = capture(pane, "INSERT")
    assert screen.splitlines()[-2].endswith(" INSERT ─┘")
    assert "INSERT" not in screen.split("│❯")[0]
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "first")
    pane("send-keys", "-t", "preview:0.0", "-l", newline)
    pane("send-keys", "-t", "preview:0.0", "-l", "second")
    assert input_rows(capture(pane, "second")) == 2
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "NORMAL")
    assert screen.splitlines()[-2].endswith(" NORMAL ─┘")
    assert input_rows(screen) == 2
    # Normal-mode dd removes the second line rather than inserting literal 'dd'.
    pane("send-keys", "-t", "preview:0.0", "-l", "dd")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        screen = capture(pane, "first")
        if input_rows(screen) == 1:
            break
        time.sleep(0.05)
    assert input_rows(screen) == 1
    assert "second" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "i")
    assert input_rows(capture(pane, "INSERT")) == 1
    for width, height in ((80, 24), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "INSERT", columns=width)
        assert screen.splitlines()[-2].endswith(" INSERT ─┘")
        assert input_rows(screen) == 1


@pytest.mark.parametrize(
    "pane",
    [
        "from pcode.preferences import save_preferences; "
        "save_preferences(editing_mode='vi'); "
        "from pcode.app import main; main()"
    ],
    indirect=True,
)
def test_vi_word_motion_and_newline_keep_editor_compact(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "one two three")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    pane("send-keys", "-t", "preview:0.0", "-l", "bbiX")
    assert input_rows(capture(pane, "one Xtwo three")) == 1
    pane("send-keys", "-t", "preview:0.0", "C-j")
    pane("send-keys", "-t", "preview:0.0", "-l", "NEWLINE")
    assert input_rows(capture(pane, "NEWLINEtwo three")) == 2
    pane("send-keys", "-t", "preview:0.0", "C-c")
    assert input_rows(capture(pane, "Input discarded")) == 1


STARTUP_SCRIPT = r"""
import asyncio
import os
import time
from pathlib import Path
from pcode.app import PreviewApp
from pcode.runtime import Message

root = Path(os.environ['XDG_CONFIG_HOME']).parent

class Runtime:
    session = None
    recovery_blocked = ''

    async def refresh_context(self):
        (root / 'metadata-started').touch()
        await asyncio.Event().wait()

    async def stream(self, text):
        yield Message('Startup response received')

    def close(self):
        pass

class App(PreviewApp):
    def _create_runtime(self):
        deadline = time.monotonic() + 15
        while not (root / 'release-startup').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('test did not release initialization')
            time.sleep(0.01)
        return Runtime()

App(model='test:local').run()
"""


@pytest.mark.parametrize("pane", [STARTUP_SCRIPT], indirect=True)
def test_startup_keeps_real_cpr_editor_editable_and_compact(pane, tmp_path):
    assert input_rows(capture(pane, "starting")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "draft before agent is ready")
    screen = capture(pane, "draft before agent is ready")
    assert "starting" in screen
    assert input_rows(screen) == 1
    pane("resize-window", "-t", "preview:0", "-x", "80", "-y", "40")
    assert input_rows(capture(pane, "draft before agent is ready", columns=80)) == 1
    (tmp_path / "release-startup").touch()
    deadline = time.monotonic() + 5
    while not (tmp_path / "metadata-started").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert input_rows(capture(pane, "draft before agent is ready", columns=80)) == 1
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "Startup response received", columns=80)) == 1
    # Metadata is still blocked, but further typing and the CPR height are unaffected.
    pane("send-keys", "-t", "preview:0.0", "-l", "next draft")
    assert input_rows(capture(pane, "next draft", columns=80)) == 1
