"""The experimental glide (`pcode.smooth_scroll`) and how pcode decides to use it."""

import os
import pty
import select
import sys
import threading

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode import smooth_scroll, theme
from pcode.smooth_scroll import MAX_GLIDE, Glide, Pacer
from pcode.startup_input import TerminalReplyParser
from pcode.ui import TerminalOutput, Transcript

REPLY = b"\x1b_smooth;ok;%d\x1b\\" % smooth_scroll.PROTOCOL
BACKGROUND = b"\x1b]11;rgb:0000/0000/0000\x1b\\"


def test_glide_settles_and_never_exceeds_its_cap():
    glide = Glide()
    assert glide.remaining(0.0) == 0.0
    glide.advance(1, 0.0)
    assert glide.remaining(0.0) == pytest.approx(1.0)
    assert 0 < glide.remaining(0.05) < 1
    assert glide.remaining(1.0) < 0.01
    for step in range(10):
        glide.advance(2, step * 0.001)
    assert glide.remaining(0.01) <= MAX_GLIDE


def test_glide_adds_an_advance_to_what_is_left_not_to_the_start():
    glide = Glide()
    glide.advance(1, 0.0)
    left = glide.remaining(0.1)
    glide.advance(1, 0.1)
    assert glide.remaining(0.1) == pytest.approx(left + 1)


def test_pacer_lets_the_first_row_go_then_holds_to_the_rate():
    pacer = Pacer()
    assert pacer.allowance(5, 0.0) == 1
    pacer.spend(1)
    assert pacer.allowance(5, 0.01) == 0
    # At least MIN_ROWS_PER_SECOND: a row is due within 1/5 s.
    assert pacer.allowance(5, 0.0 + 1 / smooth_scroll.MIN_ROWS_PER_SECOND) == 1
    # Nothing queued resets it: the next row after a pause goes at once.
    assert pacer.allowance(0, 0.3) == 0
    assert pacer.allowance(1, 0.31) == 1


def test_sequences():
    assert smooth_scroll.declaration(9, 80) == "\x1b_smooth;7;0;9;0;79;0;0\x1b\\"
    assert smooth_scroll.panel_declaration(20, 23, 80) == "\x1b_smooth;8;20;23;0;79;0;0;1\x1b\\"
    assert smooth_scroll.advance(2) == "\x1b_smooth;advance;7;2\x1b\\"
    assert smooth_scroll.advance(1, smooth_scroll.PANEL_ID) == "\x1b_smooth;advance;8;1\x1b\\"
    assert smooth_scroll.OFF == "\x1b_smooth;off;7\x1b\\\x1b_smooth;off;8\x1b\\"


@pytest.mark.parametrize(
    "env, asked",
    [
        ({"TERM": "xterm-ghostty"}, True),
        ({"TERM": "xterm-256color", "GHOSTTY_RESOURCES_DIR": "/x"}, True),
        ({"TERM": "xterm-ghostty", "TMUX": "/tmp/tmux,1,0"}, False),
        ({"TERM": "xterm-256color"}, False),
        ({"TERM": "xterm-kitty"}, False),
    ],
)
def test_only_ghostty_outside_tmux_is_asked(env, asked):
    assert smooth_scroll.worth_asking(env) is asked


def _probe(monkeypatch, *, term="xterm-ghostty", answer=b"", probe_glide=True):
    """Run the startup query on a pty; return what was sent and the leftover input."""
    master, slave = pty.openpty()
    monkeypatch.setenv("TERM", term)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("GHOSTTY_RESOURCES_DIR", raising=False)
    monkeypatch.setattr(theme, "_glide_protocol", None)
    monkeypatch.setattr(theme, "_pending_input", bytearray())
    received = []

    def terminal():
        if select.select([master], [], [], 2)[0]:
            received.append(os.read(master, 1024))
            os.write(master, answer + BACKGROUND)

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            assert theme._query_background(1.0, probe_glide=probe_glide) == "dark"
            worker.join(2)
    finally:
        worker.join(2)
        os.close(master)
        os.close(slave)
    return b"".join(received), bytes(theme._pending_input)


def test_probe_finds_a_terminal_that_glides(monkeypatch):
    sent, left = _probe(monkeypatch, answer=REPLY)
    assert sent.startswith(smooth_scroll.QUERY)  # Ahead of OSC 11, which ends the wait.
    assert theme.glide_supported()
    assert left == b""


def test_probe_needs_the_exact_protocol(monkeypatch):
    _probe(monkeypatch, answer=b"\x1b_smooth;ok;4\x1b\\")
    assert not theme.glide_supported()


def test_no_reply_means_no_glide(monkeypatch):
    _probe(monkeypatch)
    assert not theme.glide_supported()


@pytest.mark.parametrize("term, probe_glide", [("xterm-256color", True), ("xterm-ghostty", False)])
def test_query_is_not_sent_unasked(monkeypatch, term, probe_glide):
    sent, _ = _probe(monkeypatch, term=term, probe_glide=probe_glide)
    assert b"smooth" not in sent
    assert not theme.glide_supported()


def test_a_late_reply_is_kept_out_of_typing():
    text = "ab" + REPLY.decode() + "c\x1b[A"
    for split in range(len(text) + 1):
        keys = []
        parser = TerminalReplyParser(keys.append)
        parser.feed(text[:split])
        parser.feed(text[split:])
        parser.flush()
        typed = [key.data for key in keys]
        assert "".join(typed) == "abc\x1b[A", split
        assert "\x1b[A" in typed, split


@pytest.mark.parametrize(
    "setting, supported, gliding",
    [
        ("auto", True, True),
        ("off", True, False),
        ("auto", False, False),
    ],
)
def test_setting_and_probe_both_gate_the_glide(monkeypatch, setting, supported, gliding):
    monkeypatch.setattr("pcode.ui.glide_supported", lambda: supported)
    console = Console(force_terminal=True, width=80)
    transcript = Transcript(console, detected_theme="dark", preferences={"smooth_scroll": setting})
    with create_pipe_input() as pipe:
        app = Application(input=pipe, output=DummyOutput())
        output = TerminalOutput(console, app)
        transcript.output = output
    assert output.gliding is gliding


def test_nothing_is_sent_when_not_gliding():
    """With the glide off, releasing the screen writes no smooth-scroll sequence."""
    written = []
    console = Console(force_terminal=True, width=80)
    with create_pipe_input() as pipe:
        out = DummyOutput()
        out.write_raw = written.append
        app = Application(input=pipe, output=out)
        output = TerminalOutput(console, app)
        output.release_screen()
    assert not output.gliding
    assert not any("\x1b_" in text for text in written)
