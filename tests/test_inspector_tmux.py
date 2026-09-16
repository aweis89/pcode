"""Real CPR/alternate-screen regression for the temporary inspector."""

import shutil
import time

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from pcode.app import PreviewApp
from pcode.inspection import ToolArchive
from pcode.runtime import ToolStarted, ToolSummary

class App(PreviewApp):
    async def inspect_tools(self, output, session):
        session.default_buffer.text = "draft must survive"
        await super().inspect_tools(output, session)

app = App()
app.runtime.inspections = archive = ToolArchive()
for i in range(30):
    archive.event(ToolStarted("run_command", f"command {i}", str(i), arguments=f"command {i}"))
    archive.event(ToolSummary(
        "run_command", f"exit {i % 2}", failed=bool(i % 2), call_id=str(i),
        result="\n".join(f"DETAIL {i} LINE {n:03}" for n in range(200))
    ))
app.run()
"""


def modal(pane, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        screen = pane("capture-pane", "-p", "-t", "preview:0.0")
        if expected in screen:
            return screen
        time.sleep(0.05)
    pytest.fail(f"Inspector did not show {expected!r}:\n{screen}")


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_modal_scroll_resize_and_restore_editor(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/errors")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    modal(pane, "Status: Failed")
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    modal(pane, "15/30 calls")
    pane("send-keys", "-t", "preview:0.0", "Tab", "C-End")
    modal(pane, "DETAIL 29 LINE 199")
    pane("resize-window", "-t", "preview:0", "-x", "70", "-y", "24")
    modal(pane, "DETAIL 29 LINE 199")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "draft must survive", columns=70)
    assert input_rows(screen) == 1
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "DETAIL 29 LINE" not in history
    # A second open/close must not retain modal focus or corrupt the prompt height.
    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "-l", "/tools")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    modal(pane, "30/30 calls")
    pane("send-keys", "-t", "preview:0.0", "C-c")
    assert input_rows(capture(pane, "draft must survive", columns=70)) == 1


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_mouse_wheel_scrolls_details_and_restores_editor(pane):
    capture(pane, "❯")
    pane("set-option", "-g", "mouse", "on")
    pane("send-keys", "-t", "preview:0.0", "-l", "/tools")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    modal(pane, "30/30 calls")
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{mouse_any_flag}").strip() == "1"
    # Inject the SGR reports a mouse-enabled tmux forwards to the application.
    # Click the detail pane, then wheel down without any keyboard focus/scroll keys.
    pane("send-keys", "-t", "preview:0.0", "-l", "\x1b[<0;70;10M\x1b[<0;70;10m")
    for _ in range(45):
        pane("send-keys", "-t", "preview:0.0", "-l", "\x1b[<65;70;10M")
        time.sleep(0.04)
    modal(pane, "DETAIL 29 LINE 040")
    for _ in range(60):
        pane("send-keys", "-t", "preview:0.0", "-l", "\x1b[<64;70;10M")
        time.sleep(0.04)
    modal(pane, "Arguments")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    assert input_rows(capture(pane, "draft must survive")) == 1
