"""Out-of-the-box terminal compatibility: color depth, ASCII glyphs, the width probe."""

import codecs
import io
import os
import pty
import select
import sys
import threading

import pytest
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth
from rich.cells import cell_len
from rich.console import Console
from rich.table import Table

from pcode import glyphs, terminal_color, theme
from pcode.runtime import ToolStarted, ToolSummary
from pcode.startup_input import TerminalReplyParser
from pcode.tool_panel import panel_fragments, plan_row
from pcode.ui import (
    TOOL_GLYPHS,
    Activity,
    _row_keys,
    _tool_glyph,
    fit_fragments,
    status_spinner,
    tally,
    tool_glyphs_on,
)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"COLORTERM": "truecolor"}, True),
        ({"COLORTERM": "24bit"}, True),
        ({"TERM_PROGRAM": "iTerm.app"}, True),
        ({"TERM_PROGRAM": "Apple_Terminal"}, False),
        ({"TERM": "xterm-ghostty"}, True),  # Over ssh, where TERM_PROGRAM is gone.
        ({"TERM": "xterm-direct"}, True),
        ({"TERM": "xterm-256color"}, False),
        # tmux replaces both and the server's environment can be stale: only tmux knows.
        ({"TMUX": "/tmp/s,1,0", "TERM_PROGRAM": "iTerm.app"}, False),
        ({"TMUX": "/tmp/s,1,0", "TMUX_PANE": "%rgb"}, True),
    ],
)
def test_true_color_from_the_terminal(monkeypatch, env, expected):
    monkeypatch.setattr(terminal_color, "_tmux_rgb", lambda pane: pane == "%rgb")
    assert terminal_color.advertised_true_color(env) is expected


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_the_user_word_wins(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert terminal_color.color_depth() == ColorDepth.TRUE_COLOR
    assert terminal_color.rich_color_system() == "truecolor"
    terminal_color.configure("256")
    assert terminal_color.color_depth() == ColorDepth.DEPTH_8_BIT
    assert terminal_color.rich_color_system() == "256"
    assert not terminal_color.true_color()
    # NO_COLOR is left to prompt_toolkit and Rich, which both honor it.
    monkeypatch.setenv("NO_COLOR", "1")
    terminal_color.configure("truecolor")
    assert terminal_color.color_depth() is None
    assert terminal_color.rich_color_system() == "auto"
    assert not terminal_color.true_color()


def test_a_pipe_gets_no_forced_colors(monkeypatch):
    # Rich colors even a pipe once told a color system.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert terminal_color.rich_color_system() == "auto"


def test_the_truecolor_setting_needs_no_word_from_the_terminal(monkeypatch):
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert terminal_color.color_depth() is None  # Unchanged: prompt_toolkit's own pick.
    terminal_color.configure("truecolor")
    assert terminal_color.color_depth() == ColorDepth.TRUE_COLOR


def test_every_stand_in_is_one_ascii_cell():
    for point, stand_in in glyphs.TABLE.items():
        assert cell_len(chr(point)) == 1 and len(stand_in) == 1 and stand_in.isascii()
    row = "┌─ ⠋ Thinking · src… ── ✓ 7 ✗ 1 ─┐ ▌ ◈ ▸ ⟳ ○ ● → ↵ café 世界 ✅"
    assert glyphs.to_ascii(row) == "+- . Thinking - src. -- v 7 x 1 -+ | * > ~ o * > < café 世界 ✅"
    assert cell_len(glyphs.to_ascii(row)) == cell_len(row)


@pytest.mark.parametrize(
    "setting, env, wide, expected",
    [
        ("auto", {"LANG": "en_US.UTF-8"}, None, False),
        ("auto", {"LANG": "C"}, None, False),  # Says nothing about the terminal.
        ("auto", {"LANG": "en_US.ISO-8859-1"}, None, True),
        ("auto", {"LC_ALL": "en_US.utf8", "LANG": "en_US.ISO-8859-1"}, None, False),
        ("auto", {"TERM": "linux"}, None, True),
        ("auto", {"TERM": "vt100"}, None, False),  # As often a Unicode terminal misreporting.
        ("auto", {}, True, True),
        ("auto", {}, False, False),
        ("unicode", {"TERM": "linux"}, True, False),
        ("nerd", {"TERM": "linux"}, True, False),
        ("ascii", {}, None, True),
    ],
)
def test_when_ascii_is_wanted(setting, env, wide, expected):
    assert glyphs.wanted(setting, env, wide) is expected


@pytest.mark.parametrize(
    "setting, env, ascii_on, expected",
    [
        ("auto", {"TERM": "xterm-ghostty"}, False, True),
        ("auto", {"TERM_PROGRAM": "ghostty"}, False, True),
        ("auto", {"TERM": "xterm-ghostty", "TMUX": "/tmp/s,1,0"}, False, False),
        ("auto", {"TERM_PROGRAM": "ghostty", "STY": "1234.pts-0.host"}, False, False),
        ("auto", {"TERM": "xterm-kitty"}, False, True),
        # Inherited by terminals launched from a kitty shell, so not trusted.
        ("auto", {"TERM_PROGRAM": "vscode", "KITTY_WINDOW_ID": "1"}, False, False),
        ("auto", {"TERM": "xterm-kitty", "TMUX": "/tmp/s,1,0"}, False, False),
        ("auto", {"TERM": "xterm-kitty", "STY": "1234.pts-0.host"}, False, False),
        ("auto", {"TERM_PROGRAM": "iTerm.app"}, False, False),
        ("auto", {"TERM": "xterm-ghostty"}, True, False),
        ("nerd", {"TERM": "xterm-256color"}, False, True),
        ("nerd", {"TERM": "xterm-ghostty"}, True, False),
        ("unicode", {"TERM": "xterm-ghostty"}, False, False),
        ("ascii", {"TERM": "xterm-ghostty"}, False, False),
    ],
)
def test_when_nerd_is_wanted(setting, env, ascii_on, expected):
    assert glyphs.wanted_nerd(setting, env, ascii_on=ascii_on) is expected


def test_nerd_icons_are_single_cell_and_active_when_enabled():
    assert set(glyphs.NERD_ICONS) == set(glyphs.ICONS)
    for name, char in glyphs.NERD_ICONS.items():
        assert cell_len(char) == 1, f"{name} is not 1 cell wide (Rich)"
        assert get_cwidth(char) == 1, f"{name} is not 1 cell wide (prompt_toolkit)"
        assert len(glyphs.to_ascii(char)) == 1 and glyphs.to_ascii(char).isascii()

    # A wait shares the shell's glyph, not an hourglass that repeats the spinner.
    assert TOOL_GLYPHS["Wait for shell"] == "$"
    assert _tool_glyph("Wait for shell", "auto") == "$"

    assert not glyphs.nerd()
    assert _tool_glyph("Search code", "auto") == "⌕"
    assert plan_row({"status": "cancelled", "content": "Drop it"}, "⠋")[1] == "– Drop it"
    assert tally(2, 1) == "✓ 2 ✗ 1"

    assert glyphs.apply_nerd(True)
    assert glyphs.nerd()
    assert _tool_glyph("Search code", "auto") == glyphs.NERD_ICONS["search"]
    assert _tool_glyph("Run shell", "auto") == glyphs.NERD_ICONS["shell"]
    assert _tool_glyph("Run shell", "off") == "$"
    assert _tool_glyph("Wait for shell", "auto") == glyphs.NERD_ICONS["shell"]
    assert _tool_glyph("Wait for shell", "off") == "$"
    assert (
        plan_row({"status": "cancelled", "content": "Drop it"}, "⠋")[1]
        == f"{glyphs.NERD_ICONS['cancelled']} Drop it"
    )
    assert tally(2, 1) == f"{glyphs.NERD_ICONS['ok']} 2 {glyphs.NERD_ICONS['fail']} 1"

    # End-to-end through `plain()` / `fit_fragments()` / `panel_fragments()`:
    # PUA Nerd Font codepoints must not be stripped to spaces.
    activity = Activity(prompt="Fix bug", prompt_state="running")
    activity.plan = [
        {"status": "completed", "content": "One"},
        {"status": "cancelled", "content": "Two"},
    ]
    activity.tools.record(ToolStarted("read_file", "src/app.py", "c1"))
    running_rows = activity.tool_rows(activity.status_line(), 60, spinner="◜")
    running_text = "".join(text for _, text in running_rows[0])
    assert glyphs.NERD_ICONS["read"] in running_text and "src/app.py" in running_text

    activity.tools.record(ToolSummary("read_file", "src/app.py", call_id="c1"))
    settled_rows = activity.tool_rows(activity.status_line(), 60, spinner="◜")
    settled_text = "".join(text for _, text in settled_rows[0])
    assert glyphs.NERD_ICONS["tool_done"] in settled_text
    assert glyphs.NERD_ICONS["read"] in settled_text

    # `panel_fragments` keeps the task icon styled as `plan.icon.<status>`.
    plan_frags = panel_fragments(activity.plan_rows(5, "⠋"), 60)
    assert ("class:plan.icon.completed", glyphs.NERD_ICONS["completed"]) in plan_frags
    assert ("class:plan.icon.cancelled", glyphs.NERD_ICONS["cancelled"]) in plan_frags

    # `fit_fragments` preserves Nerd Font turn marks.
    fitted = "".join(t for _, t in fit_fragments([("class:session.done", glyphs.icon("ok"))], 10))
    assert fitted == glyphs.NERD_ICONS["ok"]

    # Smooth-scroll `_row_keys` matches a running tool row to its settled row.
    class _Cell:
        def __init__(self, char: str) -> None:
            self.char = char

    class _Screen:
        def __init__(self, row_text: str) -> None:
            self.height = 3
            self.data_buffer = {2: {i: _Cell(ch) for i, ch in enumerate(row_text)}}

    assert _row_keys(_Screen(running_text)) == _row_keys(_Screen(settled_text))

    # ASCII mode always wins over Nerd Fonts, regardless of call order.
    master, slave = pty.openpty()
    try:
        stream = io.TextIOWrapper(os.fdopen(os.dup(slave), "wb"), encoding="utf-8")
        assert glyphs.apply(True, stream)
        assert not glyphs.nerd()
        assert glyphs.icon("ok") == glyphs.ICONS["ok"]
        assert not glyphs.apply_nerd(True)
        stream.close()
    finally:
        os.close(master)
        os.close(slave)


def test_ascii_mode_reencodes_the_terminal_stream(monkeypatch):
    master, slave = pty.openpty()
    try:
        stream = io.TextIOWrapper(
            os.fdopen(os.dup(slave), "wb"), encoding="utf-8", errors="surrogateescape"
        )
        assert glyphs.apply(True, stream)
        assert glyphs.apply(True, stream)  # Again: still one layer.
        assert stream.encoding == "pcode-ascii-utf-8" and stream.errors == "surrogateescape"
        assert glyphs.active() and tool_glyphs_on("auto") is False
        stream.write("\udcff")  # A file name that isn't UTF-8 still prints.
        table = Table("a")
        table.add_row("✓")
        Console(file=stream, width=20, color_system=None, force_terminal=True).print(table)
        stream.close()
        written = b""
        while select.select([master], [], [], 0.5)[0]:
            written += os.read(master, 4096)
            if written.count(b"\n") >= 5:
                break
        # Rich reads the encoding and draws ASCII borders; the codec does the rest.
        assert written.startswith(b"\xff")
        assert written[1:].isascii() and b"| v |" in written and b"+---+" in written
    finally:
        os.close(master)
        os.close(slave)


def test_a_legacy_locale_keeps_its_letters():
    encoded = "é ✓ ─".encode("pcode-ascii-latin-1")
    assert encoded == "é v -".encode("latin-1")
    encoder = codecs.getincrementalencoder("pcode-ascii-euc-jp")()
    assert encoder.encode("か─") == "か-".encode("euc-jp")


def test_a_pipe_keeps_its_symbols():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert not glyphs.apply(True, stream)
    assert stream.encoding == "utf-8" and not glyphs.active()


def test_ascii_spinners_stay_distinct(monkeypatch):
    monkeypatch.setattr(glyphs, "_active", True)
    model, tool = status_spinner(), status_spinner("tool_spinner")
    for spinner in (model, tool):
        assert "".join(spinner.frames).isascii()
    assert model.frames != tool.frames


def test_nerd_spinners_share_the_task_icons_circle(monkeypatch):
    monkeypatch.setattr(glyphs, "_nerd", True)
    model, tool = status_spinner(), status_spinner("tool_spinner")
    assert model.frames == [chr(0xF0A9E + i) for i in range(8)]  # md-circle_slice_1..8
    assert tool.frames == [chr(0xEE06 + i) for i in range(6)]  # extra-progress_spinner_1..6
    # The row sanitizers keep the frames rather than blanking them as unknown PUA.
    for frame in model.frames + tool.frames:
        assert fit_fragments([("class:activity.spinner", f"{frame} ")], 40)[0][1] == f"{frame} "
        row = panel_fragments([("class:plan.in_progress", f"{frame} step")], 40)
        assert frame in "".join(text for _, text in row)
        assert frame.encode("pcode-ascii-utf-8") in (b"o", b"*")
    monkeypatch.setattr(glyphs, "_nerd", False)
    assert status_spinner().frames != model.frames


@pytest.mark.parametrize("columns, wide", [((1, 2), False), ((5, 7), True), (None, None)])
def test_the_width_probe(monkeypatch, columns, wide):
    master, slave = pty.openpty()
    monkeypatch.setattr(theme, "_ambiguous_wide", None)
    monkeypatch.setattr(theme, "_unanswered_cprs", 0)
    monkeypatch.setattr(theme, "_pending_input", bytearray())
    received = bytearray()

    def terminal():
        while b"\x1b[K" not in received and select.select([master], [], [], 2)[0]:
            received.extend(os.read(master, 1024))
        reply = b"\x1b]11;rgb:0/0/0\x1b\\"
        if columns:
            reply += b"".join(b"\x1b[3;%dR" % column for column in columns)
        os.write(master, reply)

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            assert theme._query_background(0.3, probe_width=True) == "dark"
            worker.join(2)
        # The glyph is drawn and wiped within the same write.
        assert received.endswith("\x1b[6n\x1b7─\x1b[6n\x1b8\x1b[K".encode())
        assert theme.ambiguous_wide() is wide
        assert theme._unanswered_cprs == (0 if columns else 2)
        assert theme._pending_input == b""
    finally:
        worker.join(2)
        os.close(master)
        os.close(slave)


def test_late_probe_reports_never_reach_the_editor():
    keys = []
    parser = TerminalReplyParser(keys.append, late_cprs=2)
    parser.feed("a\x1b[3;1R\x1b[3")
    parser.feed(";2Rb\x1b[9;1R")
    parser.flush()
    typed = [key.data for key in keys if key.key != Keys.CPRResponse]
    assert typed == ["a", "b"]
    # The renderer's own report, after the probe's, still reaches it.
    assert [key.data for key in keys if key.key == Keys.CPRResponse] == ["\x1b[9;1R"]
