"""A layout that scrolled transcript away and shrank again rebuilds scrollback, debounced."""

import asyncio
import functools
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode import ui
from pcode.ui import Activity, PromptLayout


def run_async(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def short_delay(monkeypatch):
    monkeypatch.setattr(ui, "REFILL_DELAY", 0.05)


def layout(replays_on_resize=True):
    replays = []
    transcript = SimpleNamespace(
        replays_on_resize=replays_on_resize, regenerate=lambda: replays.append(True)
    )
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
    prompt = PromptLayout(session, Activity(), transcript, None)
    tasks = []
    app = SimpleNamespace(
        renderer=SimpleNamespace(
            _in_alternate_screen=False, _last_screen=None, _min_available_height=10
        ),
        create_background_task=lambda coro: tasks.append(asyncio.ensure_future(coro)) or tasks[-1],
    )
    return prompt, app, replays


def render(prompt, app, *, height, needed):
    """A frame: prompt_toolkit's screen ``height``, the layout wanting ``needed``."""
    app.renderer._last_screen = SimpleNamespace(height=height)
    prompt.root = SimpleNamespace(drawn=(80, 24, needed))
    prompt.refill_after_shrink(app)


async def settle():
    await asyncio.sleep(ui.REFILL_DELAY * 3)


@run_async
async def test_a_shrunk_draft_rebuilds_once_after_it_settles():
    prompt, app, replays = layout()
    render(prompt, app, height=10, needed=4)
    # A tall draft scrolls the screen; nothing is blank yet.
    render(prompt, app, height=18, needed=18)
    # Deleting line by line: each frame shrinks the gap's edge.
    for needed in (15, 12, 9, 6):
        render(prompt, app, height=18, needed=needed)
        await asyncio.sleep(ui.REFILL_DELAY / 3)
        assert replays == []
    await settle()
    assert replays == [True]
    # The same gap is not retried if the rebuild left it.
    render(prompt, app, height=18, needed=6)
    await settle()
    assert replays == [True]


@run_async
async def test_a_layout_that_fit_or_disabled_replay_leaves_scrollback():
    prompt, app, replays = layout()
    render(prompt, app, height=10, needed=8)
    render(prompt, app, height=10, needed=4)
    disabled, disabled_app, disabled_replays = layout(replays_on_resize=False)
    render(disabled, disabled_app, height=18, needed=18)
    render(disabled, disabled_app, height=18, needed=4)
    await settle()
    assert replays == disabled_replays == []


@run_async
async def test_a_busy_turn_waits_for_its_end():
    prompt, app, replays = layout()
    prompt.activity.busy = True
    render(prompt, app, height=18, needed=4)
    await settle()
    assert replays == []
    prompt.activity.busy = False
    render(prompt, app, height=18, needed=4)
    await settle()
    assert replays == [True]
