"""Tests for the global Swift/Terminal UI surface checker and docked editor suppression."""

from types import SimpleNamespace

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode import dock, ui_surface
from pcode.runtime import CommandOutput, ToolStarted
from pcode.ui import Activity, PromptLayout, Transcript


def test_ui_surface_env_and_override(monkeypatch):
    monkeypatch.delenv(ui_surface.ENV_VAR, raising=False)
    assert ui_surface.current_surface() is ui_surface.UISurface.TERMINAL
    assert ui_surface.is_terminal_ui()
    assert not ui_surface.is_swift_ui()
    assert not ui_surface.native_transcript_active()

    monkeypatch.setenv(ui_surface.ENV_VAR, "1")
    assert ui_surface.current_surface() is ui_surface.UISurface.SWIFT
    assert ui_surface.is_swift_ui()
    assert not ui_surface.is_terminal_ui()
    # Still False until the dock is active.
    assert not ui_surface.native_transcript_active()

    monkeypatch.setattr(dock, "_active", object())
    assert ui_surface.native_transcript_active()

    with ui_surface.override_ui_surface(ui_surface.UISurface.TERMINAL):
        assert ui_surface.is_terminal_ui()
        assert not ui_surface.native_transcript_active()

    assert ui_surface.is_swift_ui()
    assert ui_surface.native_transcript_active()


def test_native_transcript_hides_editor_tool_rows_and_command_preview(monkeypatch):
    activity = Activity(prompt="run tests", prompt_state="running")
    activity.tools.record(
        ToolStarted(call_id="c1", name="shell", detail="pytest", command="pytest")
    )
    activity.command_outputs["c1"] = CommandOutput("c1", "pytest", "running...\n")
    transcript = Transcript(Console(force_terminal=False, width=100))
    with create_pipe_input() as pipe:
        layout = PromptLayout(
            PromptSession(input=pipe, output=DummyOutput()),
            activity,
            None,
            transcript,
        )
    monkeypatch.setattr(layout, "size", lambda: Size(rows=40, columns=100))

    # In terminal mode, tool rows show above the editor.
    with ui_surface.override_ui_surface(ui_surface.UISurface.TERMINAL):
        assert len(layout.tool_rows()) == 1

    # In Swift native transcript mode (docked), tool rows, command previews, and group rows
    # are suppressed so the main status line stays the top line of the docked editor.
    monkeypatch.setattr(dock, "_active", SimpleNamespace())
    with ui_surface.override_ui_surface(ui_surface.UISurface.SWIFT):
        assert layout.tool_rows() == []
        assert layout.preview_layout() is None
        assert layout.group_rows() == []
        assert layout.status_height() == 0


def test_native_transcript_omits_model_and_context_from_footer(monkeypatch):
    from pathlib import Path

    from prompt_toolkit.formatted_text import fragment_list_to_text

    from pcode import app as app_mod
    from pcode.app import PreviewApp
    from pcode.aside import Aside
    from pcode.btw_view import close, open_thread

    app = PreviewApp(model="antigravity:gemini-4-neon", workspace=Path("/tmp/proj"))
    app._startup_pending = False
    fake_output = SimpleNamespace(get_size=lambda: Size(rows=24, columns=160))
    monkeypatch.setattr(app_mod, "get_app", lambda: SimpleNamespace(output=fake_output))
    monkeypatch.setattr(app.controller, "context_label", lambda: " · 42k/200k")
    monkeypatch.setattr(app, "current_effort", lambda: "medium")

    # Terminal UI keeps model and context in the editor footer.
    with ui_surface.override_ui_surface(ui_surface.UISurface.TERMINAL):
        terminal_text = fragment_list_to_text(app.toolbar())
        assert "antigravity:gemini-4-neon (medium)" in terminal_text
        assert "42k/200k" in terminal_text
        assert "steering" in terminal_text

    # Undocked Swift surface (e.g., plain subshell / non-workspace) keeps the full footer.
    monkeypatch.setattr(dock, "_active", None)
    with ui_surface.override_ui_surface(ui_surface.UISurface.SWIFT):
        undocked_text = fragment_list_to_text(app.toolbar())
        assert "antigravity:gemini-4-neon (medium)" in undocked_text
        assert "42k/200k" in undocked_text

    # Native Swift UI (docked) moves model and context to the window titlebar.
    monkeypatch.setattr(dock, "_active", SimpleNamespace())
    with ui_surface.override_ui_surface(ui_surface.UISurface.SWIFT):
        swift_text = fragment_list_to_text(app.toolbar())
        assert "antigravity:gemini-4-neon" not in swift_text
        assert "42k/200k" not in swift_text
        assert "steering" in swift_text

        # Plain /btw (no model/effort override) keeps the session model omitted.
        plain_aside = Aside(thread="t-plain", question="q?", status="answered", answer="a.")
        app.controller.asides.items.append(plain_aside)
        open_thread(app.activity, plain_aside.thread)
        plain_btw_text = fragment_list_to_text(app.toolbar())
        assert "btw reply" in plain_btw_text
        assert "antigravity:gemini-4-neon" not in plain_btw_text
        assert "42k/200k" not in plain_btw_text

        # A /btw thread with its own model override still shows that override in the footer.
        aside = Aside(
            thread="t-model",
            question="q?",
            status="answered",
            answer="a.",
            model="anthropic:claude-haiku",
            label="haiku",
        )
        app.controller.asides.items.append(aside)
        open_thread(app.activity, aside.thread)
        btw_text = fragment_list_to_text(app.toolbar())
        assert "btw reply" in btw_text
        assert "haiku" in btw_text
        assert "42k/200k" not in btw_text

        # An effort-only `/btw +high` override also shows in the footer.
        effort_aside = Aside(
            thread="t-effort",
            question="q?",
            status="answered",
            answer="a.",
            effort="high",
            label="high",
        )
        app.controller.asides.items.append(effort_aside)
        open_thread(app.activity, effort_aside.thread)
        effort_btw_text = fragment_list_to_text(app.toolbar())
        assert "antigravity:gemini-4-neon (high)" in effort_btw_text
        assert "42k/200k" not in effort_btw_text
        close(app.activity)
