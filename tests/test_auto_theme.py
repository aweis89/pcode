import os
import pty
import select
import sys
import termios
import threading
from io import StringIO

import pytest
from rich.console import Console

from pcode import theme
from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import PALETTES, Transcript


@pytest.mark.parametrize(
    "rgb, expected",
    [
        (b"ffff/ffff/ffff", "light"),
        (b"0000/0000/0000", "dark"),
        (b"ff/ff/ff", "light"),
        (b"0/0/0", "dark"),
        (b"0/ffff/0", "light"),
        (b"ffff/0/0", "dark"),
    ],
)
@pytest.mark.parametrize("ending", [b"\x07", b"\x1b\\"])
def test_background_response(rgb, expected, ending):
    assert theme.background_theme(b"\x1b]11;rgb:" + rgb + ending) == expected


@pytest.mark.parametrize(
    "response", [b"", b"\x1b]11;rgb:ff/ff/ff", b"rgb:ff/ff/ff\x07", b"\x1b]11;rgb:gg/00/00\x07"]
)
def test_invalid_response(response):
    assert theme.background_theme(response) is None


@pytest.mark.parametrize(
    "value, expected",
    [("0;7", "light"), ("15;0", "dark"), ("0;default;15", "light"), ("bad", "dark")],
)
def test_environment_fallback(monkeypatch, value, expected):
    monkeypatch.setattr(theme, "_query_background", lambda: None)
    monkeypatch.setenv("COLORFGBG", value)
    assert theme.detect_theme() == expected
    monkeypatch.setattr(theme, "_query_background", lambda: "dark")
    assert theme.detect_theme() == "dark"


def test_redirected_input_does_not_query(monkeypatch):
    monkeypatch.setattr(sys, "stdin", StringIO())
    monkeypatch.setattr(sys, "stdout", StringIO())
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_theme() == "dark"
    assert sys.stdout.getvalue() == ""


@pytest.mark.parametrize("respond", [True, False])
def test_query_restores_terminal(monkeypatch, respond):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    monkeypatch.setenv("TERM", "xterm-256color")
    received = []

    def terminal():
        if select.select([master], [], [], 2)[0]:
            received.append(os.read(master, 1024))
            if respond:
                os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            assert theme._query_background(0.1) == ("light" if respond else None)
            worker.join(2)
        restored = termios.tcgetattr(slave)
        # macOS sets PENDIN when returning to canonical mode.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == original
        assert received == [b"\x1b]11;?\x1b\\"]
    finally:
        worker.join(2)
        os.close(master)
        os.close(slave)


def test_auto_palette_and_syntax(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda: "light")
    transcript = Transcript(Console(file=StringIO()), "auto")
    assert transcript.theme == "auto"
    assert transcript.palette == PALETTES["light"]
    assert transcript.code_theme == SETTINGS["syntax_light"].default
    transcript.color_style = "terminal"
    assert transcript.code_theme == "ansi_light"
    transcript.theme = "dark"
    assert transcript.palette == PALETTES["dark"]
    assert transcript.code_theme == "ansi_dark"


def test_auto_setting_persists_and_toggle_uses_resolved_theme(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda: "light")
    save_preferences(theme="auto")
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.transcript.theme == "auto"
    app.theme("")
    assert app.transcript.theme == "dark"
    app.theme("auto")
    assert load_preferences()["theme"] == "auto"
    assert "auto (light)" in app.transcript.console.file.getvalue()


def test_saved_syntax_themes_apply_per_palette(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda: "dark")
    save_preferences(syntax_dark="monokai", syntax_light="tango")
    transcript = Transcript(Console(file=StringIO()), "auto")
    assert transcript.code_theme == "monokai"
    transcript.theme = "light"
    assert transcript.code_theme == "tango"


def test_invalid_saved_syntax_theme_falls_back_to_default():
    save_preferences(syntax_dark="no-such-style")
    transcript = Transcript(Console(file=StringIO()), "dark")
    assert transcript.code_theme == SETTINGS["syntax_dark"].default


def test_syntax_command_persists_the_resolved_palette_only():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    app.syntax("dracula")
    assert app.transcript.code_theme == "dracula"
    assert load_preferences() == {"syntax_dark": "dracula"}
    app.transcript.theme = "light"
    assert app.transcript.code_theme == SETTINGS["syntax_light"].default
    app.syntax("")
    assert "Syntax (light)" in app.transcript.console.file.getvalue()


def test_syntax_command_rejects_an_unknown_style():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    with pytest.raises(ValueError, match="must be one of"):
        app.syntax("no-such-style")
    assert load_preferences() == {}
