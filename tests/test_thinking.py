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


def test_thinking_box_follows_latest_lines_and_preserves_paragraphs():
    activity = Activity(show_thinking=True, thinking_lines=3)
    activity.append_thinking("old line\nfirst\n\nlast")
    assert [text for _, text in activity.thinking_rows()] == ["first", "", "last"]
    activity.append_thinking(" token\nnewest")
    assert [text for _, text in activity.thinking_rows()] == ["", "last token", "newest"]
    assert [text for _, text in activity.thinking_rows(height=1)] == ["newest"]
    assert activity.thinking_rows(height=0) == []


def test_thinking_box_wraps_before_taking_tail_and_handles_wide_characters():
    from rich.cells import cell_len

    activity = Activity(show_thinking=True, thinking_lines=2)
    activity.append_thinking("abcdefghij界界界界")
    rows = [text for _, text in activity.thinking_rows(width=4)]
    assert len(rows) == 2
    assert all(cell_len(row) <= 4 for row in rows)
    assert "".join(rows).endswith("界界界")
    assert len(activity.thinking_rows(width=80)) == 1


def test_thinking_box_strips_ansi_but_keeps_newlines():
    activity = Activity(show_thinking=True)
    activity.append_thinking("\x1b[31mfirst\x1b[0m\nsecond\r\x00\x1b]0;title\x07")
    rows = [text for _, text in activity.thinking_rows()]
    assert rows[0] == "first"
    assert rows[1].strip() == "second"
    assert len(rows) == 2


def test_thinking_lines_preference_validates_and_restores():
    import pytest

    from pcode.config import configure

    assert SETTINGS["thinking_lines"].default == "10"
    assert PreviewApp().activity.thinking_lines == 10
    configure(["set", "thinking_lines", "5"])
    assert PreviewApp().activity.thinking_lines == 5
    for invalid in ("0", "-1", "1.5", "many"):
        with pytest.raises(ValueError, match="positive integer"):
            configure(["set", "thinking_lines", invalid])
    activity = Activity(thinking_lines=5)
    activity.reset()
    assert activity.thinking_lines == 5


def test_compact_summary_prefers_latest_heading_and_removes_markdown():
    activity = Activity(show_thinking=True)
    activity.plan = [{"content": "Investigate", "status": "in_progress"}]
    activity.start_thinking()
    activity.append_thinking("**Inspecting workspace**\n\nA longer summary paragraph.")
    assert activity.thinking_summary() == "Inspecting workspace"
    assert activity.panel_heading().endswith(" · Inspecting workspace")
    activity.start_thinking()
    activity.append_thinking("")  # Signature-only blocks don't erase useful status.
    assert activity.thinking_summary() == "Inspecting workspace"
    activity.start_thinking()
    activity.append_thinking("**Locating ")
    assert activity.thinking_summary() == "Locating"
    activity.append_thinking("root evidence**")
    assert activity.thinking_summary() == "Locating root evidence"
    assert "paragraph.\n\n**Locating" in activity.thinking
    activity.show_thinking = False
    assert activity.panel_heading() == activity.panel_title()
    activity.show_thinking = True
    activity.thinking_display = "expanded"
    assert activity.panel_heading() == activity.panel_title()
    assert activity.thinking_rows()
    activity.reset()
    assert activity.thinking_summary() == ""
    assert activity.thinking_latest == ""


def test_compact_plain_summary_uses_latest_line_and_sanitizes_controls():
    activity = Activity(show_thinking=True)
    activity.append_thinking("old line\n\x1b[31mnew `file_name.py`\x1b[0m\x00")
    assert activity.thinking_summary() == "new file_name.py"


def test_thinking_display_default_and_configuration():
    import pytest

    from pcode.config import configure

    assert PreviewApp().activity.thinking_display == "compact"
    configure(["set", "thinking_display", "expanded"])
    assert PreviewApp().activity.thinking_display == "expanded"
    with pytest.raises(ValueError, match="compact, expanded"):
        configure(["set", "thinking_display", "other"])
