"""Mirrored command output must reach scrollback without stretching the prompt."""

import shutil
import time

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
import asyncio, os, tempfile
# Never touch the developer's saved defaults: Ctrl+G persists its choice.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
from pcode.preferences import save_preferences
save_preferences(autohide_tasks="off")  # This pane asserts on the idle widget.
from pcode.app import PreviewApp
from pcode.runtime import Message, ToolStarted, ToolSummary

OUTPUT = "[stdout]\n" + "\n".join(f"OUTPUT_LINE_{i:02d}" for i in range(4)) + "\n[exit code: 0]"

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        self.turns += 1
        yield ToolStarted("run_command", "printf", "one", command="printf MIRRORED_COMMAND")
        await asyncio.sleep(0.05)
        yield ToolSummary(
            "run_command",
            "printf MIRRORED_COMMAND → exit 0",
            call_id="one",
            elapsed_seconds=0.4,
            command="printf MIRRORED_COMMAND",
            result=OUTPUT,
        )
        yield Message(f"TURN_{self.turns}_DONE")

    def reset(self):
        pass

app = PreviewApp(model="test:local", runtime=Runtime())
app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]
app.run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_ctrl_g_mirrors_commands_into_scrollback_and_keeps_the_prompt_compact(pane):
    assert input_rows(capture(pane, "▌")) == 1
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "TURN_1_DONE")
    assert "OUTPUT_LINE_00" not in screen
    # One option governs every command trace: with it off, nothing is mirrored.
    assert "✓ Run" not in screen
    assert input_rows(screen) == 1

    pane("send-keys", "-t", "preview:0.0", "C-g")
    screen = capture(pane, "Show commands: on")
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "TURN_2_DONE")
    assert "✓ Run · 0.4s" in screen
    assert "$ printf MIRRORED_COMMAND" in screen
    assert "OUTPUT_LINE_03" in screen
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "OUTPUT_LINE_00" in history

    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        assert input_rows(capture(pane, "▌", columns=columns)) == 1
    pane("send-keys", "-t", "preview:0.0", "C-g")
    screen = capture(pane, "Show commands: off", columns=35)
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    time.sleep(0.2)
    screen = capture(pane, "kept draft", columns=35)
    assert input_rows(screen) == 1


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_toggle_rebuilds_existing_history_without_rerunning_commands(pane):
    capture(pane, "▌")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "TURN_1_DONE")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")

    def history():
        return pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")

    for enabled in (True, False, True, False):
        pane("send-keys", "-t", "preview:0.0", "C-g")
        # Wait for the coalesced terminal handoff, not an old toggle notice.
        deadline = time.monotonic() + 3
        while True:
            screen = capture(pane, "keep draft")
            text = history()
            if ("OUTPUT_LINE_03" in text) == enabled:
                break
            assert time.monotonic() < deadline, text
            time.sleep(0.05)
        assert input_rows(screen) == 1
        assert text.count("TURN_1_DONE") == 1
        assert "TURN_2_DONE" not in text
        assert text.count("OUTPUT_LINE_00") == int(enabled)
        assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"


RESIZE_SCRIPT = SCRIPT.replace(
    "from pcode.app import PreviewApp",
    "from pcode.preferences import save_preferences\n"
    'save_preferences(show_commands="on")\n'
    "from pcode.app import PreviewApp",
)


@pytest.mark.parametrize("pane", [RESIZE_SCRIPT], indirect=True)
def test_resize_replay_reflows_history_without_duplicates_or_stretched_editor(pane):
    capture(pane, "▌")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "TURN_1_DONE")
    pane("send-keys", "-t", "preview:0.0", "-l", "/show-commands on")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "Show commands: on")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(width))
        # Let the default debounce and handoff both run under real CPR.
        time.sleep(0.8)
        screen = capture(pane, "keep draft", columns=width)
        assert input_rows(screen) == 1
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        assert history.count("OUTPUT_LINE_00") == 1
        assert history.count("TURN_1_DONE") == 1
        assert "Show commands:" not in history


LIVE_SCRIPT = (
    SCRIPT.replace(
        "from pcode.runtime import Message, ToolStarted, ToolSummary",
        "from pcode.runtime import CommandOutput, Message, ToolStarted, ToolSummary",
    )
    .replace(
        "from pcode.app import PreviewApp",
        "from pcode.preferences import save_preferences\n"
        'save_preferences(show_commands="on")\n'
        "from pcode.app import PreviewApp",
    )
    .replace(
        "await asyncio.sleep(0.05)",
        'yield CommandOutput("one", "printf MIRRORED_COMMAND", OUTPUT)\n'
        "        await asyncio.sleep(4)",
    )
)


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_active_output_precedes_completion_and_keeps_real_cpr_height(pane):
    capture(pane, "▌")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "OUTPUT_LINE_03", running=True)
    assert "TURN_1_DONE" not in screen
    assert "$ printf MIRRORED_COMMAND" in screen
    assert "Show commands" not in screen
    assert input_rows(screen) == 1
    for width in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(width))
        screen = capture(pane, "OUTPUT_LINE_03", columns=width, running=True)
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-g")
    screen = capture(pane, "Show commands: off", columns=35, running=True)
    assert "OUTPUT_LINE_03" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-g")
    screen = capture(pane, "OUTPUT_LINE_03", columns=35, running=True)
    assert input_rows(screen) == 1
    time.sleep(2)
    time.sleep(6)
    screen = capture(pane, "TURN_1_DONE", columns=35)
    assert "running · Ctrl+G" not in screen
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("OUTPUT_LINE_03") == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_cancel_clears_active_command_preview(pane):
    capture(pane, "▌")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "OUTPUT_LINE_03", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-d")
    screen = capture(pane, "Run cancelled")
    assert "OUTPUT_LINE_03" not in screen
    assert input_rows(screen) == 1


PRESSURE_SCRIPT = (
    LIVE_SCRIPT.replace(
        'save_preferences(show_commands="on")',
        'save_preferences(show_commands="on", command_preview_lines="6")',
    )
    .replace("range(4)", "range(60)")
    .replace("await asyncio.sleep(4)", "await asyncio.sleep(60)")
    .replace(
        'app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]',
        'app.activity.plan = [{"id": str(i), "content": f"TASK_{i}", '
        '"status": "in_progress" if i == 2 else "pending"} for i in range(5)]\n'
        "for i in range(2):\n"
        '    app.activity.tools.record(ToolSummary("read_file", f"file_{i}", call_id=str(i)))',
    )
    .replace('"\\n[exit code: 0]"', '""')
)


@pytest.mark.parametrize("pane", [PRESSURE_SCRIPT], indirect=True)
def test_live_tail_uses_remaining_height_without_disappearing_or_growing_editor(pane):
    capture(pane, "▌")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "OUTPUT_LINE_59", running=True)
    assert sum("OUTPUT_LINE_" in line for line in screen.splitlines()) == 6
    assert sum("TASK_" in line for line in screen.splitlines()) == 5
    for height in (24, 18, 14, 12, 32):
        pane("resize-window", "-t", "preview:0", "-y", str(height))
        time.sleep(0.8)
        screen = capture(pane, "OUTPUT_LINE_59", running=True)
        count = sum("OUTPUT_LINE_" in line for line in screen.splitlines())
        if not 1 <= count <= 6:
            pytest.fail(f"height={height} count={count}\n{screen}")
        assert input_rows(screen) == 1
        assert "Show commands" not in screen
        assert "Ctrl+G to hide" not in screen
        if height == 32:
            assert sum("OUTPUT_LINE_" in line for line in screen.splitlines()) == 6
            assert sum("TASK_" in line for line in screen.splitlines()) == 5
    pane("send-keys", "-t", "preview:0.0", "C-d")
    capture(pane, "Run cancelled")
