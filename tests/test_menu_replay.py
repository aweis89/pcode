"""Closing a completion menu that scrolled the terminal replays scrollback."""

from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.ui import PromptLayout


def layout(replays_on_resize=True):
    replays = []
    transcript = SimpleNamespace(
        replays_on_resize=replays_on_resize, regenerate=lambda: replays.append(True)
    )
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
    return PromptLayout(session, None, transcript, None), replays


def render(prompt, *, menu, height, available=10):
    buffer = prompt.session.default_buffer
    buffer.complete_state = CompletionState(buffer.document) if menu else None
    renderer = SimpleNamespace(
        _in_alternate_screen=False,
        _last_screen=SimpleNamespace(height=height),
        _min_available_height=available,
    )
    prompt.replay_after_menu(SimpleNamespace(renderer=renderer))


def test_closing_an_overflowing_menu_replays_once():
    prompt, replays = layout()
    render(prompt, menu=True, height=4)
    render(prompt, menu=True, height=14)
    # prompt_toolkit keeps the tall screen after the menu closes.
    render(prompt, menu=False, height=14)
    render(prompt, menu=False, height=14)
    assert replays == [True]


@pytest.mark.parametrize("replays_on_resize", [True, False])
def test_a_menu_that_fit_or_disabled_replay_leaves_scrollback(replays_on_resize):
    prompt, replays = layout(replays_on_resize)
    render(prompt, menu=True, height=14 if not replays_on_resize else 8)
    render(prompt, menu=False, height=8)
    assert replays == []
