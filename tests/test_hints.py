"""Inline shortcut hints: the send mode's key in the footer, the task list's hide key."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from test_status_line import make_app

from pcode import preferences
from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.prefix_keys import PrefixKeys
from pcode.ui import Activity, PromptLayout

PLAN = [{"content": "Task", "status": "pending"}]


@pytest.fixture
def shipped_default(monkeypatch):
    """conftest turns hints off for every other test; restore the real default."""
    original = preferences.SETTINGS["show_hints"]
    monkeypatch.setitem(preferences.SETTINGS, "show_hints", replace(original, default="on"))


def heading(activity: Activity, prefix: str = "ctrl", columns: int = 80) -> str:
    layout = SimpleNamespace(
        activity=activity,
        shortcuts=PrefixKeys(prefix),
        size=lambda: SimpleNamespace(columns=columns),
    )
    return fragment_list_to_text(PromptLayout.plan_heading(layout))


def test_hints_default_on(shipped_default):
    assert SETTINGS["show_hints"].default == "on"
    assert PreviewApp().activity.show_hints


def test_footer_shows_the_send_mode_key(tmp_path, monkeypatch, shipped_default):
    app, _ = make_app(tmp_path, monkeypatch)
    text = fragment_list_to_text(app.toolbar())
    assert " · steering (^S) · preview" in text
    save_preferences(show_hints="off")
    app.handle("/config get show_hints")  # Any /config re-reads the layout settings.
    assert " · steering · preview" in fragment_list_to_text(app.toolbar())


def test_narrow_footer_drops_the_hint_before_the_model(tmp_path, monkeypatch, shipped_default):
    app, _ = make_app(tmp_path, monkeypatch, width=len(" steering · preview") + 2)
    assert fragment_list_to_text(app.toolbar()) == " steering · preview"


def test_config_applies_hints_at_once(tmp_path, monkeypatch, shipped_default):
    app, stream = make_app(tmp_path, monkeypatch)
    app.handle("/config set show_hints off")
    assert not app.activity.show_hints
    assert load_preferences()["show_hints"] == "off"
    assert "Layout settings apply immediately." in stream.getvalue()
    app.handle("/config unset show_hints")
    assert app.activity.show_hints


def test_task_heading_names_the_hide_key():
    assert heading(Activity(plan=PLAN)) == "Tasks 0/1 (^O hide)"
    assert heading(Activity()) == "Tools (^O hide)"
    assert heading(Activity(plan=PLAN), prefix="ctrl+p") == "Tasks 0/1 (^P o hide)"
    assert heading(Activity(plan=PLAN, show_hints=False)) == "Tasks 0/1"


def test_narrow_pane_drops_the_hint_before_the_heading():
    # 8 columns of border chrome, then exactly "Tasks 0/1" fits.
    assert heading(Activity(plan=PLAN), columns=8 + len("Tasks 0/1")) == "Tasks 0/1"
