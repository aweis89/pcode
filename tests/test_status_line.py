"""The editor footer is context, not a second keyboard-help menu."""

import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.styles import default_ui_style, merge_styles
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import PreviewRuntime
from pcode.ui import PALETTES


def make_app(workspace, monkeypatch, *, model=None, width=100):
    monkeypatch.setattr(
        "pcode.app.get_app",
        lambda: SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=width))
        ),
    )
    stream = StringIO()
    app = PreviewApp(
        workspace=workspace,
        model=model,
        runtime=PreviewRuntime(),
        console=Console(file=stream, width=120, color_system=None),
    )
    return app, stream


def test_footer_home_branch_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setattr("pcode.context_usage.context_window", lambda model: 400_000)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path / "p/pcode", monkeypatch, model="openai:gpt-5")
    app.branch = "master"
    assert (
        fragment_list_to_text(app.toolbar())
        == " ~/p/pcode master · send: steering · openai:gpt-5 · effort: default · ctx: 0/400k"
    )
    app.runtime.agent = SimpleNamespace(
        model=SimpleNamespace(settings={"openai_reasoning_effort": "low"}),
        model_settings={"openai_reasoning_effort": "high"},
    )
    assert fragment_list_to_text(app.toolbar()).endswith(
        "openai:gpt-5 · effort: high · ctx: 0/400k"
    )
    app.runtime.agent.model_settings = None
    assert fragment_list_to_text(app.toolbar()).endswith("effort: low · ctx: 0/400k")


def test_preview_home_and_help(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, stream = make_app(tmp_path, monkeypatch)
    assert fragment_list_to_text(app.toolbar()) == " ~ · send: steering · preview · effort: n/a"
    app.handle("/help")
    help_text = stream.getvalue()
    for hint in (
        "/ commands",
        "Enter send",
        "Alt+Enter newline",
        "Ctrl+D exit",
        "Ctrl+S cycles",
        "cancel",
    ):
        assert hint in help_text
        assert hint not in fragment_list_to_text(app.toolbar())


def test_footer_outside_home_and_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    app, _ = make_app(tmp_path, monkeypatch, width=200)
    app.activity.busy = True
    app.activity.queued = 2
    text = fragment_list_to_text(app.toolbar())
    assert str(tmp_path) in text
    assert text.endswith("send: steering · working · 2 queued · preview · effort: n/a")
    assert "Ctrl" not in text


def test_footer_shows_a_model_chosen_during_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr("pcode.context_usage.context_window", lambda model: 400_000)
    app, _ = make_app(tmp_path, monkeypatch, model="openai:gpt-5", width=200)
    app.activity.busy = True
    app.pending_model = "anthropic:claude-opus-5"
    text = fragment_list_to_text(app.toolbar())
    # The running turn keeps its model; the arrow names what the next one uses.
    assert "openai:gpt-5 → anthropic:claude-opus-5 · effort:" in text


@pytest.mark.parametrize("width", [20, 40, 60, 100])
def test_long_unicode_path_stays_one_row(tmp_path, monkeypatch, width):
    app, _ = make_app(tmp_path / ("界" * 100 + "\npath"), monkeypatch, width=width)
    text = fragment_list_to_text(app.toolbar())
    assert cell_len(text) <= width
    assert "\n" not in text
    if width >= 40:
        assert "preview · effort: n/a" in text


def test_narrow_busy_footer_keeps_send_mode_and_activity(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch, model="test:local", width=40)
    app.activity.busy = True
    text = fragment_list_to_text(app.toolbar())
    assert text.startswith(" send: steering · working · test:local")
    assert text.endswith("…")
    assert cell_len(text) <= 40


def test_branch_refresh_handles_switches_detached_and_non_repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-b", "main")
    app, _ = make_app(tmp_path, monkeypatch)
    app.refresh_branch()
    assert app.branch == "main"  # Even an unborn branch has a useful name.
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Test",
    )
    git("checkout", "-b", "feature")
    app.refresh_branch()
    assert app.branch == "feature"
    git("checkout", "--detach")
    app.refresh_branch()
    assert app.branch == git("rev-parse", "--short", "HEAD")
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    app.workspace = elsewhere
    app.refresh_branch()
    assert app.branch == ""


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("git", 1)])
def test_git_unavailable_does_not_break_footer(tmp_path, monkeypatch, error):
    app, _ = make_app(tmp_path, monkeypatch)
    app.branch = "old"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("pcode.app.subprocess.run", fail)
    app.refresh_branch()
    assert app.branch == ""
    assert "preview" in fragment_list_to_text(app.toolbar())


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_footer_styles_use_terminal_foreground_and_background(theme):
    # A light terminal can use the default dark app palette (and vice versa).
    # Keep both colors terminal-native rather than assuming they match.
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    for role in ("text", "location", "model", "activity"):
        attrs = style.get_attrs_for_style_str(
            f"class:bottom-toolbar class:bottom-toolbar.text class:bottom-toolbar.{role}"
        )
        assert attrs.bgcolor == "default"
        assert attrs.color == "default"
        assert not attrs.reverse
        assert not attrs.dim
        assert attrs.bold == (role in ("location", "activity"))


def test_footer_segments_highlight_context_and_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    app, _ = make_app(tmp_path, monkeypatch, model="test:local", width=200)
    app.activity.busy = True
    app.activity.queued = 2
    fragments = app.toolbar()
    assert ("class:bottom-toolbar.location", str(tmp_path)) in fragments
    assert ("class:bottom-toolbar.model", "test:local") in fragments
    assert ("class:bottom-toolbar.activity", "working") in fragments
    assert ("class:bottom-toolbar.activity", "2 queued") in fragments


@pytest.mark.parametrize("model", ["anthropic:claude-sonnet-4-6", "openai-codex:gpt-5"])
def test_footer_provider_and_context(tmp_path, monkeypatch, model):
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.usage import RequestUsage

    app, _ = make_app(tmp_path, monkeypatch, model=model, width=200)
    app.runtime.history = [ModelResponse(parts=[], usage=RequestUsage(input_tokens=12_500))]
    text = fragment_list_to_text(app.toolbar())
    assert model in text
    assert "ctx: 12.5k/" in text
    app.runtime.history = []
    assert "ctx: 0/" in fragment_list_to_text(app.toolbar())


@pytest.mark.parametrize("width", [1, 20, 40, 60, 100])
def test_provider_and_context_stay_one_row(tmp_path, monkeypatch, width):
    monkeypatch.setattr("pcode.context_usage.context_window", lambda model: 1_000_000)
    app, _ = make_app(
        tmp_path / ("界" * 100), monkeypatch, model="anthropic:claude-sonnet-4-6", width=width
    )
    text = fragment_list_to_text(app.toolbar())
    assert cell_len(text) <= width
    if width >= 60:
        assert "anthropic:claude-sonnet-4-6" in text
    if width >= 100:
        assert "ctx: 0/1m" in text


@pytest.mark.parametrize("mode", ["steering", "queue", "interrupt"])
@pytest.mark.parametrize("width", [20, 35, 40, 100])
def test_send_mode_survives_long_model_and_path(tmp_path, monkeypatch, mode, width):
    app, _ = make_app(
        tmp_path / ("workspace" * 20),
        monkeypatch,
        model="provider:" + "long-model-name" * 20,
        width=width,
    )
    app.send_mode = mode
    text = fragment_list_to_text(app.toolbar())
    assert text.startswith(f" send: {mode}")
    assert cell_len(text) <= width
