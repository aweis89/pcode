import asyncio
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from rich.console import Console

from pcode.app import PreviewApp, main
from pcode.preferences import load_preferences, preferences_path, save_preferences


def make_app(model="openai-codex:test"):
    return PreviewApp(
        model=model,
        runtime=SimpleNamespace(agent=SimpleNamespace(model_settings=None, model=None)),
        console=Console(file=StringIO()),
    )


def test_effort_persists_and_restores():
    app = make_app()
    app.effort("high")
    assert load_preferences() == {"model": "openai-codex:test", "effort": "high"}
    assert make_app().current_effort() == "high"
    assert make_app("anthropic:test").runtime.agent.model_settings is None
    app.effort("default")
    assert make_app().current_effort() == "default"
    assert load_preferences()["effort"] == "default"


def test_switch_persists_only_successful_selection(monkeypatch):
    save_preferences(model="openai-codex:old", effort="low")
    app = PreviewApp(console=Console(file=StringIO()))
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
    asyncio.run(app.switch_model("openai-codex:new"))
    assert load_preferences() == {"model": "openai-codex:new", "effort": "low"}
    assert app.current_effort() == "low"
    with patch("pcode.agent.create_agent", side_effect=ValueError("failed")):
        with pytest.raises(ValueError):
            asyncio.run(app.switch_model("openai-codex:bad"))
    assert load_preferences()["model"] == "openai-codex:new"
    app.runtime.close()


@pytest.mark.parametrize(
    "arguments, expected", [([], "test:saved"), (["-m", "test:override"], "test:override")]
)
def test_startup_default_and_explicit_override(monkeypatch, arguments, expected):
    save_preferences(model="test:saved")
    monkeypatch.setattr(sys, "argv", ["pcode", *arguments])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["model"] == expected


@pytest.mark.parametrize("content", ["{", "[]", '{"model": 42, "effort": []}'])
def test_invalid_preferences_are_ignored(content):
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert load_preferences() == {}


def test_save_failure_does_not_discard_selection():
    app = make_app()
    with patch("pcode.app.save_preferences", side_effect=PermissionError):
        app.effort("high")
    assert app.current_effort() == "high"
    assert "Could not save defaults" in app.transcript.console.file.getvalue()
