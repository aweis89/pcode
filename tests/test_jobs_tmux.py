"""Background jobs in the live panel: the rows, the watched tail, and the wake."""

import shutil

import pytest
from test_inspector_tmux import modal
from test_tmux import capture, input_rows, resize, settle
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
import os, shlex, sys, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
from pathlib import Path
from pcode.app import PreviewApp
from pcode.commands import Command
from pcode.jobs import JobRegistry
from pcode.runtime import Message

class Runtime:
    session = None
    history = []
    prompts = []

    def __init__(self):
        self.jobs = JobRegistry()

    async def stream(self, prompt):
        self.prompts.append(prompt)
        yield Message("WOKEN: " + prompt.splitlines()[0])

    def reset(self):
        pass

def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"

app = PreviewApp(model="test:local", runtime=Runtime())
jobs = app.runtime.jobs
finish = Path(os.environ["XDG_CONFIG_HOME"]) / "finish-job"
app.registry.register(Command("/finish-job", "Finish the test job", lambda _: finish.touch()))
jobs.launch(
    command("import time, os; print('serving on 8000', flush=True)\n"
            f"while not os.path.exists({str(finish)!r}): time.sleep(0.05)\n"
            "print('bye')"),
    cwd=os.getcwd(), background=True, purpose="serving the docs",
)
jobs.launch(command("import time; time.sleep(600)"), cwd=os.getcwd())
app.run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_jobs_row_watch_and_wake_keep_the_prompt_compact(pane):
    # Both jobs are in the background: nothing is waiting on either.
    screen = capture(pane, "⟳ j2")
    assert "⟳ j1 · serving the docs · " in screen
    assert input_rows(screen) == 1

    pane("send-keys", "-t", "preview:0.0", "/jobs watch j1", "Enter")
    # The pinned tail paints at once; the note waits for the next scrollback flush.
    watching = ("$ ", "serving on 8000", "Watching [j1]")
    screen = settle(pane, lambda screen: all(text in screen for text in watching))
    assert all(text in screen for text in watching), screen
    assert input_rows(screen) == 1

    # Bare /jobs is a popup over the log, not another listing in scrollback.
    pane("send-keys", "-t", "preview:0.0", "/jobs", "Enter")
    screen = modal(pane, "Jobs · 2 this session · 2 running")
    assert "⟳ j1 · running · " in screen and "serving on 8000" in screen
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "serving on 8000")
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "this session" not in history

    # Release only after watch is visible: startup time must not race the exit.
    pane("send-keys", "-t", "preview:0.0", "/finish-job", "Enter")
    # The backgrounded job ends while idle, so its notice starts a turn on its
    # own: the model hears without a prompt, and the watch clears.
    screen = capture(pane, "WOKEN: [j1] serving the docs → exit 0")
    panel = screen.rsplit("WOKEN", 1)[-1]
    assert "serving on 8000" not in panel
    assert "⟳ j2" in screen and "⟳ j1" not in panel
    # Completion uses the normal Run summary, never a finished live row.
    assert "✓ Run · j1 · exit 0 · " in screen.split("WOKEN")[0]
    assert "✓ j1" not in capture(pane, "⟳ j2")
    assert input_rows(screen) == 1

    for columns in (40, 100):
        resize(pane, "resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "⟳ j2", columns=columns)
        assert input_rows(screen) == 1

    pane("send-keys", "-t", "preview:0.0", "/jobs stop all", "Enter")
    screen = capture(pane, "Stopped [j2]")
    assert "⟳ j2" not in screen
    assert input_rows(screen) == 1
