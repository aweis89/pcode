"""The /btw thread mode over a real terminal: the editor replies to a thread in the drawer.

tmux has no drawer, so the script stands one in (`PreviewApp.drawer_host`);
the drawer's own APCs reach tmux, which drops them.
"""

import shutil

import pytest
from test_tmux import capture
from test_tmux import pane as pane
from test_tmux import release as release

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

RUNNING = r"""
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
import pcode.delta
pcode.delta.find_delta = lambda: None
from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.runtime import EditPreview, Message

PreviewApp.drawer_host = lambda self: "0123abcd"  # A drawer shows threads.

class Runtime:
    session = None
    tree = None

    async def stream(self, prompt):
        yield EditPreview("one", "sample.py", "-OLD_EDIT_LINE\n+LIVE_EDIT_LINE")
        await gate()
        yield EditPreview("one")
        yield Message("MAIN_TURN_DONE")

    def reset(self):
        pass

app = PreviewApp(model="test:local", runtime=Runtime())
app.transcript.live_edits = True
asides = app.controller.asides
aside = Aside(question="SIDE_QUESTION?", answer="SIDE_ANSWER", activity="")
aside.settle("answered")
aside.reply = SideReply("SIDE_ANSWER")
aside.read = True
asides.items.append(aside)


def follow_up(thread, question):
    asides.follows(thread)
    reply = Aside(question=question, answer=f"Echo: {question}", activity="", thread=thread)
    reply.settle("answered")
    reply.reply = SideReply(reply.answer)
    asides.items.append(reply)
    # The drawer would show it; here a note says where it went.
    app.transcript.note(f"TO_THREAD: {question}")


app.controller.follow_up_aside = follow_up
app.run()
"""


def history(pane) -> str:
    """The whole scrollback, not just the screen."""
    return pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")


@pytest.mark.parametrize("pane", [RUNNING], indirect=True)
def test_the_editor_replies_to_the_drawer_thread_while_a_turn_runs(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "do the work", "Enter")
    capture(pane, "LIVE_EDIT_LINE", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-q")
    # Enter replies to the thread rather than steering the running turn,
    # and the conversation, live edit and all, stays on screen.
    pane("send-keys", "-t", "preview:0.0", "while it runs?", "Enter")
    screen = capture(pane, "TO_THREAD: while it runs?", running=True)
    assert "LIVE_EDIT_LINE" in screen
    assert "SIDE_ANSWER" not in history(pane), "the thread is the drawer's to draw"
    # Escape gives the editor back to the conversation.
    pane("send-keys", "-t", "preview:0.0", "Escape")
    release()
    capture(pane, "MAIN_TURN_DONE")
    pane("send-keys", "-t", "preview:0.0", "second main message", "Enter")
    capture(pane, "▌ second main message")
    scrollback = history(pane)
    # The reply went to the thread: neither steered nor queued for the turn.
    assert "▌ while it runs?" not in scrollback
    assert "TO_THREAD: second main message" not in scrollback
