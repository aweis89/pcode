"""Typeahead before frontend imports and during terminal detection survives startup."""

import os
import sys
import termios
from io import StringIO

import pytest

pexpect = pytest.importorskip("pexpect")


@pytest.mark.parametrize("stage", ["imports", "query"])
def test_early_input_reaches_editor(tmp_path, stage):
    script = """
import sys
import time
from pcode import cli
quiet = cli._quiet_stdin

def delayed(stack):
    quiet(stack)
    print('IMPORTS', flush=True)
    time.sleep(0.5)

cli._quiet_stdin = delayed
sys.argv = ['pcode', '--no-worktree', '--no-save', '--theme', 'auto', '-C', sys.argv[1]]
cli.main()
"""
    log = StringIO()
    child = pexpect.spawn(
        sys.executable,
        ["-c", script, str(tmp_path)],
        env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
        encoding="utf-8",
        timeout=10,
    )
    child.logfile_read = log
    try:
        child.expect_exact("IMPORTS")
        # Canonical without echo is what terminals read as a password prompt.
        assert not termios.tcgetattr(child.child_fd)[3] & (termios.ECHO | termios.ICANON)
        if stage == "query":
            child.expect_exact("\x1b]11;?\x1b\\")
        child.send("/theme light")  # Deliberately no Enter before the editor starts.
        if stage == "query":
            child.send("\x1b]11;rgb:0000/0000/0000\x1b\\")
        child.expect_exact("\x1b[?2004h")  # Editor enables bracketed paste.
        assert "/theme light" not in child.before  # No shell-style echo above the editor.
        child.expect_exact("light")
        child.send("\r")
        child.expect_exact("Theme: light.")
        child.sendcontrol("d")
        child.expect(pexpect.EOF)
        child.close()
        assert child.exitstatus == 0, log.getvalue()
    finally:
        if child.isalive():
            child.terminate(force=True)


@pytest.mark.parametrize(
    "reply, detected",
    [
        ("\x1b]11;rgb:ffff/ffff/ffff\x07", "light"),
        ("\x1b]11;rgb:0000/0000/0000\x1b\\", "dark"),
        ("\x1b[?997;1n", "dark"),
        ("\x1b[?997;2n", "light"),
    ],
)
def test_appearance_reply_is_filtered_at_every_split(reply, detected):
    from pcode.startup_input import TerminalReplyParser

    text = "early \x1b[A" + reply + "draft\x1b[B"
    for split in range(len(text) + 1):
        keys, themes = [], []
        parser = TerminalReplyParser(keys.append, themes.append)
        parser.feed(text[:split])
        parser.feed(text[split:])
        parser.flush()
        typed = [key.data for key in keys]
        assert "".join(typed) == "early \x1b[Adraft\x1b[B", split
        assert "\x1b[A" in typed and "\x1b[B" in typed, split  # Still parsed as arrows.
        assert themes == [detected], split


def test_replies_keep_being_filtered_after_the_first():
    from pcode.startup_input import TerminalReplyParser

    keys, themes = [], []
    parser = TerminalReplyParser(keys.append, themes.append)
    for reply in ("\x1b]11;rgb:0/0/0\x07", "a", "\x1b[?997;2n", "b", "\x1b]11;rgb:f/f/f\x07"):
        parser.feed(reply)
    parser.flush()
    assert "".join(key.data for key in keys) == "ab"
    assert themes == ["dark", "light", "light"]


@pytest.mark.parametrize("reads", [["\x1b]"], ["\x1b", "]"], ["\x1b[", "?"]])
def test_keys_that_start_like_a_reply_survive_the_escape_timeout(reads):
    from pcode.startup_input import TerminalReplyParser

    keys = []
    parser = TerminalReplyParser(keys.append)
    for data in reads:
        parser.feed(data)
    parser.flush()
    assert "".join(key.data for key in keys) == "".join(reads)


def test_typing_after_a_truncated_reply_is_released_without_a_pause():
    from pcode.startup_input import TerminalReplyParser

    keys = []
    parser = TerminalReplyParser(keys.append)
    parser.feed("\x1b]11;rgb:ff")
    parser.feed("hello world")
    assert "hello world" in "".join(key.data for key in keys)


def test_truncated_background_reply_does_not_hold_later_typing():
    from pcode.startup_input import TerminalReplyParser

    keys = []
    parser = TerminalReplyParser(keys.append)
    parser.feed("\x1b]11;rgb:ffff/")
    parser.flush()
    parser.feed("next prompt\r")
    parser.flush()
    assert "".join(key.data for key in keys) == "next prompt\r"


def test_buffered_escape_uses_the_normal_input_timeout():
    import asyncio
    import pty

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import DummyOutput

    from pcode.startup_input import StartupInput

    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as stdin:
            bindings = KeyBindings()

            @bindings.add("escape")
            def escape(event):
                event.app.exit(result="escaped")

            app = Application(
                input=StartupInput(stdin, b"\x1b"),
                output=DummyOutput(),
                key_bindings=bindings,
            )
            app.ttimeoutlen = 0.01
            app.timeoutlen = 0.01

            async def run():
                return await asyncio.wait_for(app.run_async(), 2)

            assert asyncio.run(run()) == "escaped"
    finally:
        os.close(master)
        os.close(slave)


def _settings(fd):
    # Re-entering canonical mode makes BSD kernels set PENDIN (reprocess any
    # queued input). Only the kernel can clear it, and with nothing queued it
    # changes nothing.
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.PENDIN
    return attrs


@pytest.mark.parametrize("outcome", ["return", "error", "interrupt", "handoff"])
def test_bootstrap_restores_terminal(monkeypatch, outcome):
    import pty

    from pcode import app, cli

    master, slave = pty.openpty()
    original = _settings(slave)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)

            def run():
                assert not termios.tcgetattr(slave)[3] & (termios.ECHO | termios.ICANON)
                if outcome == "error":
                    raise ValueError("startup failed")
                if outcome == "interrupt":
                    raise KeyboardInterrupt
                if outcome == "handoff":
                    cli.restore_stdin()
                    assert _settings(slave) == original

            monkeypatch.setattr(app, "main", run)
            if outcome in ("error", "interrupt"):
                with pytest.raises(ValueError if outcome == "error" else KeyboardInterrupt):
                    cli.main()
            else:
                cli.main()
            assert _settings(slave) == original
    finally:
        os.close(master)
        os.close(slave)
