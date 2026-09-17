"""Reasoning is mutable prompt state, never a transcript event."""

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import Activity


def test_thinking_is_bounded_hidden_by_default_and_cleared_on_reset():
    activity = Activity()
    activity.append_thinking("secret " * 2000)
    assert len(activity.thinking) == 8192
    assert activity.thinking_rows() == []
    activity.show_thinking = True
    assert "secret" in activity.thinking_rows()[0][1]
    activity.reset()
    assert activity.thinking == ""
    assert activity.thinking_rows() == []
    assert activity.show_thinking


def test_thinking_preview_sanitizes_terminal_controls():
    activity = Activity(show_thinking=True)
    activity.append_thinking("hello\n\x1b[31mworld\x1b[0m\r\x00")
    text = activity.thinking_rows()[0][1]
    assert "\x1b" not in text
    assert "\n" not in text
    assert "\r" not in text
    assert "\x00" not in text


def test_show_thinking_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["show_thinking"].default == "off"
    assert not PreviewApp().activity.show_thinking
    save_preferences(show_thinking="on")
    assert load_preferences()["show_thinking"] == "on"
    assert PreviewApp().activity.show_thinking


def test_show_thinking_command_changes_view_and_saves_default(tmp_path, monkeypatch):
    from io import StringIO

    import pytest
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document
    from rich.console import Console

    from pcode.commands import SlashCompleter

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    app.activity.busy = True  # A display preference is safe to change mid-turn.
    app.activity.append_thinking("PRIVATE_REASONING")
    assert app.registry.dispatch("/show-thinking on")
    assert app.activity.show_thinking
    assert load_preferences()["show_thinking"] == "on"
    assert app.registry.dispatch("/show-thinking")
    assert app.activity.show_thinking
    assert "Show thinking: on" in output.getvalue()
    assert app.registry.dispatch("/show-thinking off")
    assert not app.activity.show_thinking
    assert load_preferences()["show_thinking"] == "off"
    assert app.activity.thinking == "PRIVATE_REASONING"
    assert "PRIVATE_REASONING" not in output.getvalue()
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/show-thinking invalid")
    completions = SlashCompleter(app.registry).get_completions(
        Document("/show-thinking "), CompleteEvent()
    )
    assert [item.text for item in completions] == ["on", "off"]


def test_meridian_thinking_toggle_explains_upstream_requirement(tmp_path, monkeypatch):
    from io import StringIO

    from rich.console import Console

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    app.model = "meridian:claude-fable-5-1"
    assert app.registry.dispatch("/show-thinking on")
    assert "Thinking Passthrough" in output.getvalue()
    assert "only changes pcode's display" in output.getvalue()
