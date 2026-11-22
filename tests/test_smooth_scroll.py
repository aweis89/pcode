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
    assert (
        smooth_scroll.visible_declaration(8, 20, 23, 80) == "\x1b_smooth;8;20;23;0;79;0;0;1\x1b\\"
    )
    assert smooth_scroll.advance(2) == "\x1b_smooth;advance;7;2\x1b\\"
    assert smooth_scroll.advance(1, smooth_scroll.PANEL_ID) == "\x1b_smooth;advance;8;1\x1b\\"
    assert smooth_scroll.OFF == "".join(f"\x1b_smooth;off;{i}\x1b\\" for i in (7, 8, 9))


@pytest.mark.parametrize(
    "env, asked",
    [
        ({"TERM": "xterm-ghostty"}, True),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "ghostty"}, True),
        # Inherited by terminals started from a Ghostty shell: not enough.
        ({"TERM": "xterm-256color", "GHOSTTY_RESOURCES_DIR": "/x"}, False),
        ({"TERM": "xterm-ghostty", "TMUX": "/tmp/tmux,1,0"}, False),
        ({"TERM": "xterm-256color"}, False),
        ({"TERM": "xterm-kitty"}, False),
    ],
)
def test_only_ghostty_outside_tmux_is_asked(env, asked):
    assert smooth_scroll.worth_asking(env) is asked


def _probe(
    monkeypatch,
    *,
    term="xterm-ghostty",
    answer=b"",
    probe_glide=True,
    probe_drawer=False,
    background=True,
):
    """Run the startup query on a pty; return what was sent and the leftover input."""
    master, slave = pty.openpty()
    monkeypatch.setenv("TERM", term)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr(theme, "_glide_protocol", None)
    monkeypatch.setattr(theme, "_drawer_protocol", None)
    monkeypatch.setattr(theme, "_pending_input", bytearray())
    received = []

    def terminal():
        if select.select([master], [], [], 2)[0]:
            received.append(os.read(master, 1024))
            os.write(master, answer + (BACKGROUND if background else b""))

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            timeout = 1.0 if background else 0.3
            detected = theme._query_background(
                timeout, probe_glide=probe_glide, probe_drawer=probe_drawer
            )
            assert detected == ("dark" if background else None)
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


@pytest.mark.parametrize("reply", [b"\x1b_smooth;ok;4\x1b\\", b"\x1b_smooth;ok;5;x\x1b\\"])
def test_probe_needs_the_exact_protocol(monkeypatch, reply):
    _, left = _probe(monkeypatch, answer=reply)
    assert not theme.glide_supported()
    assert left == b""  # Taken out of typing all the same.


def test_a_reply_without_a_background_one_still_counts(monkeypatch):
    _, left = _probe(monkeypatch, answer=REPLY, background=False)
    assert theme.glide_supported()
    assert left == b""


def test_no_reply_means_no_glide(monkeypatch):
    _probe(monkeypatch)
    assert not theme.glide_supported()


@pytest.mark.parametrize("term, probe_glide", [("xterm-256color", True), ("xterm-ghostty", False)])
def test_query_is_not_sent_unasked(monkeypatch, term, probe_glide):
    sent, _ = _probe(monkeypatch, term=term, probe_glide=probe_glide)
    assert b"smooth" not in sent
    assert not theme.glide_supported()


def test_probe_finds_a_terminal_with_a_drawer_beside_the_glide(monkeypatch):
    sent, left = _probe(monkeypatch, answer=REPLY + b"\x1b_pcode;ok;1\x1b\\", probe_drawer=True)
    assert sent.startswith(smooth_scroll.QUERY + b"\x1b_pcode;?\x1b\\")
    assert theme.glide_supported() and theme.drawer_supported()
    assert left == b""


def test_the_drawer_is_not_asked_after_unless_wanted(monkeypatch):
    sent, _ = _probe(monkeypatch, answer=REPLY)
    assert b"pcode;" not in sent and not theme.drawer_supported()


@pytest.mark.parametrize(
    "reply", [REPLY.decode(), "\x1b_smooth;error;7\x1b\\", "\x1b_pcode;ok;1\x1b\\"]
)
def test_a_late_reply_is_kept_out_of_typing(reply):
    text = "ab" + reply + "c\x1b[A"
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


def test_glide_runs_downward_too_and_never_past_rest():
    glide = Glide()
    glide.advance(-1, 0.0)
    assert glide.remaining(0.0) == pytest.approx(-1.0)
    assert -1 < glide.remaining(0.05) < 0
    glide.advance(-10, 0.05)
    assert glide.remaining(0.05) == -MAX_GLIDE
    assert glide.remaining(2.0) == 0.0
    # Drawn higher, then lower: the new advance lands on what is left.
    glide = Glide()
    glide.advance(-1, 0.0)
    left = glide.remaining(0.1)
    glide.advance(2, 0.1)
    assert glide.remaining(0.1) == pytest.approx(left + 2)


def test_a_row_leaving_drops_the_rows_above_it():
    from pcode.ui import _block_move

    # Counted up from the bottom: status 1, commands 3 to 5, the middle one leaves.
    before = {1: "status", 3: "$ make", 4: "$ ls", 5: "$ cd /tmp"}
    after = {1: "status", 3: "$ make", 4: "$ cd /tmp"}
    assert _block_move(after, before) == (-1, 4, 4)


def test_a_row_arriving_lifts_the_rows_above_it():
    from pcode.ui import _block_move

    before = {1: "status", 3: "$ ls", 4: "$ cd /tmp"}
    after = {1: "status", 3: "$ make", 4: "$ ls", 5: "$ cd /tmp"}
    assert _block_move(after, before) == (1, 4, 5)


@pytest.mark.parametrize(
    "after",
    [
        # A row between the movers changed: not one block.
        {1: "status", 3: "$ make", 4: "$ ls", 5: "$ cd /tmp && ls", 6: "$ pwd"},
        # Too far to glide.
        {1: "status", 9: "$ ls", 10: "$ cd /tmp"},
    ],
)
def test_no_glide_when_it_is_not_one_block_moving(after):
    from pcode.ui import _block_move

    before = {1: "status", 3: "$ ls", 4: "$ cd /tmp", 5: "$ pwd"}
    assert _block_move(after, before) is None
    assert _block_move(before, before) is None


def test_row_keys_ignore_spinners_and_marks():
    from prompt_toolkit.layout.screen import Char, Screen

    from pcode.ui import _row_keys

    def screen(mark):
        s = Screen()
        for x, char in enumerate(f" {mark} $ ls"):
            s.data_buffer[3][x] = Char(char)
        s.height = 5
        return s

    # pcode's done mark `√` is a math symbol, not `So`: stripped as a leading mark.
    assert _row_keys(screen("◜")) == _row_keys(screen("√")) == _row_keys(screen("-")) == {2: "$ ls"}


class _RecordingOutput:
    """Just enough of prompt_toolkit's output for the glide's region bookkeeping."""

    def __init__(self, rows=30, columns=80):
        from prompt_toolkit.data_structures import Size

        self.size = Size(rows=rows, columns=columns)
        self.written: list[str] = []

    def get_size(self):
        return self.size

    def write_raw(self, text):
        self.written.append(text)

    def flush(self):
        pass


def _glide_output(monkeypatch, clock):
    from types import SimpleNamespace

    monkeypatch.setattr("pcode.ui.monotonic", lambda: clock[0])
    out = _RecordingOutput()
    app = SimpleNamespace(output=out, invalidate=lambda: None)
    return TerminalOutput(Console(force_terminal=True, width=80), app), out


def test_a_row_written_before_the_screen_fills_unrolls_and_holds_the_next(monkeypatch):
    from types import SimpleNamespace

    clock = [100.0]
    output, out = _glide_output(monkeypatch, clock)
    # The layout drew the bottom 20 rows of 30, so the typing row is row 10.
    output._glide_insert(20, 1, [SimpleNamespace(typed=False)])
    sent = "".join(out.written)
    # Cleared first, so the terminal starts this glide fresh as pcode does.
    assert sent == (
        "\x1b_smooth;off;9\x1b\\\x1b_smooth;9;10;11;0;79;0;0;1\x1b\\\x1b_smooth;advance;9;-1\x1b\\"
    )
    assert output._insert_held()
    clock[0] += 0.5
    assert not output._insert_held()
    out.written.clear()
    clock[0] += 1.0
    output._drop_settled_regions(output.app)
    assert out.written == ["\x1b_smooth;off;9\x1b\\"]
    assert not output._insert_declared


def test_a_typed_row_does_not_unroll(monkeypatch):
    from types import SimpleNamespace

    output, out = _glide_output(monkeypatch, [0.0])
    output._glide_insert(20, 1, [SimpleNamespace(typed=True)])
    assert out.written == [] and not output._insert_held()


def test_a_settled_panel_region_is_cleared_and_a_moving_one_kept(monkeypatch):
    clock = [5.0]
    output, out = _glide_output(monkeypatch, clock)
    output._glide_panel_region(output.app, 24, 26, -1)
    assert out.written[-1].endswith("\x1b_smooth;advance;8;-1\x1b\\")
    out.written.clear()
    clock[0] += 0.05
    output._drop_settled_regions(output.app)
    assert out.written == []
    clock[0] += 2.0
    output._drop_settled_regions(output.app)
    assert out.written == ["\x1b_smooth;off;8\x1b\\"]


def test_repeated_rows_follow_the_block_but_do_not_vote():
    from pcode.ui import _block_move

    # The same command twice: it moves with the block, never decides it.
    before = {1: "status", 3: "$ cd /tmp", 4: "$ cd /tmp", 5: "$ ls"}
    after = {1: "status", 3: "$ make", 4: "$ cd /tmp", 5: "$ cd /tmp", 6: "$ ls"}
    # Row 4 reads the same as before, so it counts as staying; it is under
    # the block, inside what the glide lifts into view, so it rises in.
    assert _block_move(after, before) == (1, 5, 6)
    # Nothing unique moved: nothing to go on.
    assert _block_move({1: "a", 3: "b", 4: "b"}, {1: "a", 2: "b", 3: "b"}) is None
