import asyncio
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from rich.console import Console

from pcode.app import PreviewApp, main
from pcode.preferences import (
    load_preferences,
    model_efforts,
    preferences_path,
    save_preferences,
)


def make_app(model="openai-codex:test"):
    return PreviewApp(
        model=model,
        runtime=SimpleNamespace(agent=SimpleNamespace(model_settings=None, model=None)),
        console=Console(file=StringIO()),
    )


def test_effort_persists_per_model_and_restores():
    app = make_app()
    app.effort("high")
    assert load_preferences() == {"model": "openai-codex:test"}
    assert model_efforts() == {"openai-codex:test": "high"}
    assert make_app().current_effort() == "high"
    # Another model keeps its own effort: nothing saved means nothing applied.
    for provider in ("anthropic", "meridian"):
        assert make_app(f"{provider}:test").runtime.agent.model_settings is None
    make_app("anthropic:test").effort("low")
    assert make_app("anthropic:test").runtime.agent.model_settings == {"anthropic_effort": "low"}
    assert make_app().current_effort() == "high"
    assert make_app("google:test").runtime.agent.model_settings is None
    app.effort("default")
    assert make_app().current_effort() == "default"
    assert make_app("anthropic:test").current_effort() == "low"


def test_shared_effort_default_applies_only_to_models_without_their_own():
    save_preferences(effort="medium")
    assert make_app().current_effort() == "medium"
    make_app().effort("low")
    assert make_app().current_effort() == "low"
    assert make_app("openai-codex:other").current_effort() == "medium"


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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_persists_and_restores_completion_palette(theme):
    from pcode.ui import PALETTES

    save_preferences(model="test:saved", effort="high")
    app = PreviewApp(console=Console(file=StringIO()))
    app.handle(f"/theme {theme}")
    reopened = PreviewApp(console=Console(file=StringIO()))
    assert reopened.transcript.theme == theme
    style = dict(reopened.transcript.palette.prompt_style().style_rules)
    assert style["completion-menu"] == (
        f"bg:{PALETTES[theme].surface} {PALETTES[theme].foreground}"
    )
    assert load_preferences() == {"model": "test:saved", "effort": "high", "theme": theme}
    save_preferences(effort="low")
    assert load_preferences()["theme"] == theme


def test_theme_toggle_persists_and_invalid_selection_does_not_change_default():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    app.handle("/theme")
    assert load_preferences()["theme"] == "light"
    app.handle("/theme invalid")
    assert load_preferences()["theme"] == "light"


@pytest.mark.parametrize("arguments, expected", [([], "light"), (["--theme", "dark"], "dark")])
def test_theme_startup_default_and_explicit_override(monkeypatch, arguments, expected):
    save_preferences(theme="light")
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", *arguments])
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["theme"] == expected
    assert load_preferences()["theme"] == "light"


def test_theme_defaults_to_auto(monkeypatch):
    assert PreviewApp(console=Console(file=StringIO())).transcript.theme == "auto"
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo"])
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["theme"] == "auto"
    assert "theme" not in load_preferences()


@pytest.mark.parametrize("value", ["invalid", None, [], 42])
def test_invalid_saved_theme_falls_back_to_auto(value):
    import json

    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": value}))
    assert PreviewApp(console=Console(file=StringIO())).transcript.theme == "auto"


def test_theme_save_failure_keeps_current_selection():
    app = PreviewApp(console=Console(file=StringIO()))
    with patch("pcode.app.save_preferences", side_effect=PermissionError):
        app.theme("light")
    assert app.transcript.theme == "light"
    assert "Could not save defaults" in app.transcript.console.file.getvalue()


def test_config_dir_override_moves_every_config_file(monkeypatch, tmp_path):
    from pcode.anthropic_oauth import credentials_path
    from pcode.ext import user_extension_dir
    from pcode.mcp import config_path as mcp_config_path

    instance = tmp_path / "instance-b"
    monkeypatch.setenv("PCODE_CONFIG_DIR", str(instance))

    assert preferences_path() == instance / "preferences.json"
    assert credentials_path() == instance / "credentials.json"
    assert mcp_config_path() == instance / "mcp.json"
    assert user_extension_dir() == instance / "extensions"


def test_config_dir_falls_back_to_xdg_when_override_is_blank(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_CONFIG_DIR", "  ")
    assert preferences_path() == tmp_path / "config" / "pcode" / "preferences.json"
