import asyncio
import os
import pty
from io import StringIO

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
    environment_supports,
    progress,
    progress_transport,
    terminal_fd,
)
from pcode.ui import Activity

PASSTHROUGH = terminal_notify._passthrough
INTO_TMUX = terminal_notify._into_tmux


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
        ({"GHOSTTY_RESOURCES_DIR": "/opt/ghostty"}, True),  # inside tmux
        ({"WEZTERM_EXECUTABLE": "/usr/bin/wezterm-gui"}, True),
        # kitty before 0.38 shows it as a notification, and says no version.
        ({"TERM": "xterm-kitty"}, False),
        ({"KITTY_WINDOW_ID": "1"}, False),
        # tmux started in Ghostty, attached from an old iTerm2 over ssh.
        ({"GHOSTTY_RESOURCES_DIR": "/opt/ghostty", "LC_TERMINAL": "iTerm2"}, False),
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


def test_transport_outside_tmux():
    ghostty = {"TERM_PROGRAM": "ghostty"}
    assert progress_transport("auto", ghostty, tty=True) is str
    assert progress_transport("auto", {"TERM": "alacritty"}, tty=True) is None
    assert progress_transport("on", {"TERM": "alacritty"}, tty=True) is str
    assert progress_transport("off", ghostty, tty=True) is None
    # Not a terminal at all, or one that says it cannot do anything.
    assert progress_transport("on", ghostty, tty=False) is None
    assert progress_transport("on", {"TERM": "dumb"}, tty=True) is None


def test_tmux_gets_both_a_passthrough_and_a_raw_copy():
    # What a pane sees: tmux replaces TERM and TERM_PROGRAM, and the server's
    # environment still names the terminal it started in.
    pane = {
        "TMUX": "/tmp/tmux-1/default,1,0",
        "TERM": "tmux-256color",
        "TERM_PROGRAM": "tmux",
        "TERM_PROGRAM_VERSION": "3.7c",
    }
    assert progress_transport("auto", pane, tty=True) is None
    assert progress_transport("on", pane, tty=True) is INTO_TMUX
    ghostty = {**pane, "GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/Contents/Resources"}
    assert progress_transport("auto", ghostty, tty=True) is INTO_TMUX
    assert INTO_TMUX(progress(CLEAR)) == PASSTHROUGH(progress(CLEAR)) + progress(CLEAR)


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

    # Plan steps fill it; none started yet still bounces.
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
    tab.turn_started()
    tab.tick(0)
    sent()
    activity.prompt_state = "failed"
    # Typing ahead as it fails, before the red bar was ever up, keeps it.
    tab.key_pressed()
    tab.tick(1)
    assert sent() == [progress(ERROR, 100)]
    tab.tick(1 + KEEPALIVE_SECONDS)
    assert sent() == [progress(ERROR, 100)]
    tab.key_pressed()
    tab.tick(2 + KEEPALIVE_SECONDS)
    assert sent() == [progress(CLEAR)]

    # The next turn runs, then fails again: red again.
    activity.prompt_state = "running"
    tab.turn_started()
    tab.tick(3 + KEEPALIVE_SECONDS)
    activity.prompt_state = "failed"
    tab.tick(4 + KEEPALIVE_SECONDS)
    assert sent() == [progress(INDETERMINATE), progress(ERROR, 100)]

    # Another session on screen starts undismissed and unpaused.
    tab.key_pressed()
    tab.switched()
    activity.prompt_state = "cancelled"
    tab.tick(5 + KEEPALIVE_SECONDS)
    assert sent() == [progress(CLEAR)]


def test_a_turn_that_fails_mid_retry_does_not_pause_the_next_command(keeper):
    tab, activity, sent = keeper
    activity.prompt_state = "running"
    tab.turn_started()
    tab.turn_retry()
    tab.tick(0)
    assert sent() == [progress(PAUSED)]
    activity.prompt_state = "failed"
    tab.turn_ended()
    # A `!command` runs with no turn_started, and may print nothing for a while.
    activity.user_command = True
    tab.tick(1)
    assert sent() == [progress(INDETERMINATE)]


def test_writes_are_whole_even_on_a_non_blocking_fd(keeper, monkeypatch):
    tab, activity, sent = keeper
    os.set_blocking(tab.fd, False)
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, data[:3]))
    activity.prompt_state = "running"
    tab.tick(0)
    assert sent() == [progress(INDETERMINATE)]
    assert os.get_blocking(tab.fd) is False


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


def test_no_terminal_sends_nothing_and_its_hooks_still_work():
    tab = TabProgress(Activity(prompt_state="running"), None, "on", env={"TERM_PROGRAM": "ghostty"})

    async def run():
        async with tab.shown():
            tab.turn_retry()
            tab.turn_event()
            await asyncio.sleep(0.05)

    asyncio.run(run())
    assert tab.wrap is None and tab.sent is None


def test_the_terminal_is_the_first_stream_that_is_one():
    main, terminal = pty.openpty()
    read, write = os.pipe()
    try:
        with open(write, "w", closefd=False) as pipe, open(terminal, "w", closefd=False) as tty:
            assert terminal_fd(StringIO(), pipe, tty) == terminal
            assert terminal_fd(tty, pipe) == terminal
            assert terminal_fd(StringIO(), pipe) is None
            assert terminal_fd(None) is None
    finally:
        for fd in (main, terminal, read, write):
            os.close(fd)


def test_shown_takes_the_bar_down_as_its_block_ends():
    main, terminal = pty.openpty()
    try:
        activity = Activity(prompt_state="running")
        tab = TabProgress(activity, terminal, "auto", env={"TERM_PROGRAM": "ghostty"})

        async def run():
            async with tab.shown():
                await asyncio.sleep(0.1)

        asyncio.run(run())
        assert os.read(main, 4096) == (progress(INDETERMINATE) + progress(CLEAR)).encode()
    finally:
        os.close(main)
        os.close(terminal)


def test_cancelling_the_task_clears_a_real_terminal():
    main, terminal = pty.openpty()
    try:
        activity = Activity(prompt_state="running")
        tab = TabProgress(activity, terminal, "auto", env={"TERM_PROGRAM": "ghostty"})

        async def run():
            task = asyncio.create_task(tab.run())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        assert os.read(main, 4096) == (progress(INDETERMINATE) + progress(CLEAR)).encode()
    finally:
        os.close(main)
        os.close(terminal)
