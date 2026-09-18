"""Visibility affects the entire combined Tasks/Tools widget, not its data."""

from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences
from pcode.ui import Activity, create_prompt


def test_visibility_preference_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["show_tasks"].default == "on"
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.activity.show_tasks
    assert app.registry.dispatch("/show-tasks off")
    assert not app.activity.show_tasks
    assert load_preferences()["show_tasks"] == "off"
    assert not PreviewApp().activity.show_tasks
    assert app.registry.dispatch("/show-tasks")
    assert not app.activity.show_tasks
    assert app.registry.dispatch("/show-tasks on")
    assert app.activity.show_tasks
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/show-tasks invalid")
    with pytest.raises(ValueError):
        SETTINGS["show_tasks"].validate("show_tasks", "invalid")


def test_hidden_widget_preserves_data_and_reset_preserves_visibility():
    activity = Activity(plan=[{"content": "Retained task", "status": "pending"}])
    assert activity.plan_rows(10, "*")
    activity.show_tasks = False
    assert activity.plan_rows(10, "*") == []
    activity.plan.append({"content": "New task", "status": "pending"})
    activity.show_tasks = True
    assert len(activity.plan_rows(10, "*")) >= 2
    activity.show_tasks = False
    activity.reset()
    assert not activity.show_tasks


@pytest.mark.parametrize("editing_mode", ["EMACS", "VI"])
def test_shortcut_toggles_and_calls_persistence(editing_mode):
    from prompt_toolkit.enums import EditingMode

    app = PreviewApp(console=Console(file=StringIO()))
    saved = []
    session = create_prompt(
        app.registry,
        activity=app.activity,
        on_tasks=saved.append,
        input=DummyInput(),
        output=DummyOutput(),
        editing_mode=EditingMode[editing_mode],
    )
    binding = session.key_bindings.get_bindings_for_keys(("c-o",))[-1]
    event = SimpleNamespace(app=session.app)
    binding.handler(event)
    assert not app.activity.show_tasks
    binding.handler(event)
    assert app.activity.show_tasks
    assert saved == [False, True]
