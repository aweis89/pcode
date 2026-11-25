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
