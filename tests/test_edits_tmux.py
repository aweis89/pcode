"""Edit previews and redraw with real cursor-position reporting."""

import shutil
import time

import pytest
from test_tmux import TIMEOUT, capture, input_rows
from test_tmux import pane as pane
from test_tmux import release as release

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
# Assertions read the +/- gutter, which delta drops; render diffs with Rich.
import pcode.delta
pcode.delta.find_delta = lambda: None
from pcode.app import PreviewApp
from pcode.edits import completed_change
from pcode.runtime import EditPreview, Message

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        self.turns += 1
        yield EditPreview("one", "sample.py", "-OLD_EDIT_LINE\n+LIVE_EDIT_LINE")
        await gate()
        yield EditPreview("one")
        yield completed_change("sample.py", "OLD_EDIT_LINE\n", "SAVED_EDIT_LINE\n")
        yield Message(f"TURN_{self.turns}_DONE")

    def reset(self):
        pass

app = PreviewApp(model="test:local", runtime=Runtime())
app.run()
"""


CODE_SCRIPT = r"""
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
# Assertions read the +/- gutter, which delta drops; render diffs with Rich.
import pcode.delta
pcode.delta.find_delta = lambda: None
from pcode.app import PreviewApp
from pcode.runtime import EditPreview, Message

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        self.turns += 1
        snippet = "LIVE_CODE_LINE = await grep(pattern='x')"
        yield EditPreview("one", "run_code", snippet, kind="code")
        await gate()
        yield EditPreview("one")
        yield Message(f"TURN_{self.turns}_DONE")

    def reset(self):
        pass

app = PreviewApp(model="test:local", runtime=Runtime())
app.run()
"""


def history(pane):
    return pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_completed_edits_toggle_and_resize_without_duplicates(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "go", "Enter")
    screen = capture(pane, "LIVE_EDIT_LINE", running=True)
    assert "⟳ Editing · sample.py" in screen
    assert "Preparing edit" not in screen and "not applied" not in screen
    assert "TURN_1_DONE" not in screen
    assert input_rows(screen) == 1
    release()
    capture(pane, "TURN_1_DONE")
    assert history(pane).count("+SAVED_EDIT_LINE") == 1
    assert "LIVE_EDIT_LINE" not in history(pane)
    for state in ("off", "on", "off", "on"):
        pane("send-keys", "-t", "preview:0.0", f"/show-edits {state}", "Enter")
        deadline = time.monotonic() + TIMEOUT
        while True:
            screen = capture(pane, "❯")
            text = history(pane)
            if ("+SAVED_EDIT_LINE" in text) == (state == "on"):
                break
            assert time.monotonic() < deadline, text
            time.sleep(0.1)
        assert input_rows(screen) == 1
        assert text.count("TURN_1_DONE") == 1
        assert text.count("+SAVED_EDIT_LINE") == (1 if state == "on" else 0)
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    for width in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(width))
        time.sleep(0.5)
        screen = capture(pane, "kept draft", columns=width)
        assert input_rows(screen) == 1
        assert history(pane).count("+SAVED_EDIT_LINE") == 1


DELTA_SCRIPT = SCRIPT.replace("pcode.delta.find_delta = lambda: None\n", "")


@pytest.mark.skipif(shutil.which("delta") is None, reason="delta is optional")
@pytest.mark.parametrize("pane", [DELTA_SCRIPT], indirect=True)
def test_delta_draws_the_live_preview_and_the_settled_block(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "go", "Enter")
    # The line shows at once without its +/- gutter, as delta lays it out, and
    # delta's own rendering (an added-line background) replaces it once its
    # worker finishes.
    screen = capture(pane, "LIVE_EDIT_LINE", running=True)
    assert "+LIVE_EDIT_LINE" not in screen and input_rows(screen) == 1

    def styled_row():
        styled = pane("capture-pane", "-p", "-e", "-t", "preview:0.0")
        return next(line for line in styled.splitlines() if "LIVE_EDIT_LINE" in line)

    deadline = time.monotonic() + TIMEOUT
    while "[48;" not in (row := styled_row()):
        assert time.monotonic() < deadline, repr(row)
        time.sleep(0.1)
    release()
    capture(pane, "TURN_1_DONE")
    text = history(pane)
    assert text.count("SAVED_EDIT_LINE") == 1 and "+SAVED_EDIT_LINE" not in text
    assert "LIVE_EDIT_LINE" not in text


@pytest.mark.parametrize("pane", [CODE_SCRIPT], indirect=True)
def test_sandboxed_snippets_preview_as_code_without_growing_the_editor(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "go", "Enter")
    screen = capture(pane, "LIVE_CODE_LINE", running=True)
    assert "Preparing code" in screen and "not applied" not in screen
    assert input_rows(screen) == 1
    release()
    capture(pane, "TURN_1_DONE")
    # The snippet is a pending argument, so it never reaches the transcript.
    assert "LIVE_CODE_LINE" not in history(pane)


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_cancelled_preview_never_enters_scrollback_and_keeps_editor_height(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "go", "Enter")
    capture(pane, "LIVE_EDIT_LINE", running=True)
    for height in (18, 12, 32):
        pane("resize-window", "-t", "preview:0", "-y", str(height))
        time.sleep(0.5)
        screen = capture(pane, "LIVE_EDIT_LINE", running=True)
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-d")
    screen = capture(pane, "Run cancelled")
    assert "LIVE_EDIT_LINE" not in screen
    assert "SAVED_EDIT_LINE" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "/redraw", "Enter")
    capture(pane, "❯")
    assert "LIVE_EDIT_LINE" not in history(pane)
