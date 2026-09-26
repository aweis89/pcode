import asyncio
import os
import subprocess

import pytest

from pcode import terminal_notify
from pcode.terminal_notify import (
    CLEAR,
    ERROR,
    INDETERMINATE,
    KEEPALIVE_SECONDS,
    NORMAL,
    PAUSED,
    TabProgress,
    Tmux,
    environment_supports,
    progress,
    progress_transport,
    query_tmux,
)
from pcode.ui import Activity

PASSTHROUGH = terminal_notify._passthrough


def test_reports_are_conemu_osc_9_4():
    assert progress(INDETERMINATE) == "\x1b]9;4;3\x07"
    assert progress(NORMAL, 40) == "\x1b]9;4;1;40\x07"
    assert progress(CLEAR) == "\x1b]9;4;0\x07"
    assert PASSTHROUGH(progress(CLEAR)) == "\x1bPtmux;\x1b\x1b]9;4;0\x07\x1b\\"


@pytest.mark.parametrize(
    ("env", "supported"),
    [
        ({"TERM_PROGRAM": "ghostty"}, True),
        ({"TERM": "xterm-ghostty"}, True),  # over ssh, where TERM_PROGRAM is gone
        ({"TERM_PROGRAM": "WezTerm"}, True),
        ({"TERM": "xterm-kitty"}, True),
        ({"KITTY_WINDOW_ID": "1"}, True),
        ({"TERM_PROGRAM": "vscode"}, True),
        ({"WT_SESSION": "abc"}, True),
        ({"ConEmuANSI": "ON"}, True),
        ({"VTE_VERSION": "7900"}, True),
        ({"VTE_VERSION": "7802"}, False),
        ({"KONSOLE_VERSION": "260400"}, True),
        ({"KONSOLE_VERSION": "250800"}, False),
        # iTerm2 before 3.6.6 would show "4;3" as a desktop notification.
        ({"TERM_PROGRAM": "iTerm.app", "TERM_PROGRAM_VERSION": "3.6.6"}, True),
        ({"TERM_PROGRAM": "iTerm.app", "TERM_PROGRAM_VERSION": "3.5.14"}, False),
        ({"TERM_PROGRAM": "iTerm.app"}, False),
        ({"LC_TERMINAL": "iTerm2", "LC_TERMINAL_VERSION": "3.6.9"}, True),
        ({"TERM_FEATURES": "T3LrMSc7UUw9Ts3BFGsSyHNoSxFP"}, True),
        ({"TERM_FEATURES": "T3LrMSc7UUw9Ts3BFGsSyHNoSxF"}, False),
        ({"TERM_PROGRAM": "Apple_Terminal"}, False),
        ({"TERM": "alacritty"}, False),
        ({}, False),
    ],
)
def test_auto_sends_only_to_terminals_that_draw_the_bar(env, supported):
    assert environment_supports(env) is supported


def tmux_says(**fields):
    info = Tmux(**{"version": (3, 7), "passthrough": False, **fields})
    return lambda env: info


def test_transport_outside_tmux():
    ghostty = {"TERM_PROGRAM": "ghostty"}
    assert progress_transport("auto", ghostty, tty=True) is str
    assert progress_transport("auto", {"TERM": "alacritty"}, tty=True) is None
    assert progress_transport("on", {"TERM": "alacritty"}, tty=True) is str
    assert progress_transport("off", ghostty, tty=True) is None
    # Not a terminal at all, or one that says it cannot do anything.
    assert progress_transport("on", ghostty, tty=False) is None
    assert progress_transport("on", {"TERM": "dumb"}, tty=True) is None


def test_transport_inside_tmux():
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TERM": "tmux-256color"}
    inherited = {**env, "TERM_PROGRAM": "ghostty"}

    def transport(tmux, mode="auto", env=env):
        return progress_transport(mode, env, tty=True, tmux=tmux)

    ghostty = {"termtype": "ghostty 1.2.0", "features": ("RGB", "progressbar")}
    # Passthrough reaches the terminal as sent, so keep-alives survive.
    assert transport(tmux_says(passthrough=True, **ghostty)) is PASSTHROUGH
    # Without it tmux 3.7 forwards the active pane's report itself.
    assert transport(tmux_says(**ghostty)) is str
    old = tmux_says(version=(3, 6), termtype="ghostty 1.2.0")
    assert transport(old) is None
    assert transport(old, "on") is None

    # The attached terminal decides, not the environment the server started in.
    alacritty = tmux_says(passthrough=True, termtype="alacritty 0.15", termname="alacritty")
    assert transport(alacritty, env=inherited) is None
    assert transport(alacritty, "on") is PASSTHROUGH
    for outer in (
        {"termtype": "iTerm2 3.6.6"},
        {"termname": "xterm-kitty"},
        {"termtype": "kitty(0.40.1)"},
    ):
        assert transport(tmux_says(passthrough=True, **outer)) is PASSTHROUGH
    assert transport(tmux_says(passthrough=True, termtype="iTerm2 3.5.0")) is None

    # tmux cannot be asked: fall back to the inherited environment.
    assert transport(lambda env: None, env=inherited) is PASSTHROUGH
    assert transport(lambda env: None) is None


def test_query_tmux_reads_one_display_message(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout="3.7c\tall\tghostty 1.2.0\txterm-ghostty\tRGB,progressbar,sync\n"
        )

    monkeypatch.setattr(subprocess, "run", run)
    info = query_tmux({"TMUX_PANE": "%3"})
    assert info == Tmux(
        version=(3, 7),
        passthrough=True,
        termtype="ghostty 1.2.0",
        termname="xterm-ghostty",
        features=("RGB", "progressbar", "sync"),
    )
    assert info.outer_supports and info.forwards
    assert calls[0][:4] == ["tmux", "display-message", "-p", "-t"]

    def missing(command, **kwargs):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", missing)
    assert query_tmux({}) is None


@pytest.fixture
def keeper():
    read, write = os.pipe()
    os.set_blocking(read, False)
    activity = Activity()
    tab = TabProgress(activity, write, "on", env={})
    tab.wrap = str

    def sent() -> list[str]:
        try:
            data = os.read(read, 4096).decode()
        except BlockingIOError:
            return []
        return [f"\x1b]9;4;{part}" for part in data.split("\x1b]9;4;") if part]

    yield tab, activity, sent
    os.close(read)
    os.close(write)


def test_bar_follows_the_turn_and_is_kept_alive(keeper):
    tab, activity, sent = keeper

    tab.tick(0)
    assert sent() == [progress(CLEAR)]  # clears whatever a dead pcode left
    tab.tick(10)
    assert sent() == []  # idle is never refreshed

    activity.prompt_state = "running"
    tab.turn_started()
    tab.tick(11)
    assert sent() == [progress(INDETERMINATE)]
    tab.tick(11 + KEEPALIVE_SECONDS / 2)
    assert sent() == []
    tab.tick(11 + KEEPALIVE_SECONDS)
    assert sent() == [progress(INDETERMINATE)]

    # Plan steps fill it; none done yet still bounces.
    activity.plan = [{"status": "pending"}] * 4
    tab.tick(20)
    assert sent() == []
    activity.plan = [{"status": "completed"}] + [{"status": "pending"}] * 3
    tab.tick(21)
    assert sent() == [progress(NORMAL, 25)]

    # A retried provider request pauses it until the next event arrives.
    tab.turn_retry()
    tab.tick(22)
    assert sent() == [progress(PAUSED)]
    tab.turn_event()
    tab.tick(23)
    assert sent() == [progress(NORMAL, 25)]

    activity.prompt_state = "done"
    tab.tick(24)
    assert sent() == [progress(CLEAR)]


def test_failed_turn_stays_red_until_seen(keeper):
    tab, activity, sent = keeper
    activity.prompt_state = "running"
    tab.key_pressed()  # typing during a turn dismisses nothing yet
    activity.prompt_state = "failed"
    tab.tick(0)
    assert sent() == [progress(ERROR, 100)]
    tab.tick(KEEPALIVE_SECONDS)
    assert sent() == [progress(ERROR, 100)]
    tab.key_pressed()
    tab.tick(KEEPALIVE_SECONDS + 1)
    assert sent() == [progress(CLEAR)]

    # The next failure is red again.
    tab.turn_started()
    tab.tick(KEEPALIVE_SECONDS + 2)
    assert sent() == [progress(ERROR, 100)]

    activity.prompt_state = "cancelled"
    tab.tick(KEEPALIVE_SECONDS + 3)
    assert sent() == [progress(CLEAR)]


def test_close_takes_the_bar_down_once(keeper):
    tab, activity, sent = keeper
    tab.close()
    assert sent() == []  # nothing was ever shown
    activity.prompt_state = "running"
    tab.tick(0)
    sent()
    tab.close()
    tab.close()
    assert sent() == [progress(CLEAR)]


def test_a_pipe_gets_nothing():
    read, write = os.pipe()
    try:
        tab = TabProgress(Activity(), write, "on", env={"TERM_PROGRAM": "ghostty"})
        asyncio.run(asyncio.wait_for(tab.run(), 5))
        assert tab.wrap is None and tab.sent is None
    finally:
        os.close(read)
        os.close(write)
