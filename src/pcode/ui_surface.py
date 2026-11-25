"""Global UI surface detection: Swift app (Pcode.app) vs. terminal-only rendering.

Use these helpers to gate UI behavior that belongs exclusively to the native
Swift transcript/app or exclusively to the terminal UI:

    from pcode.ui_surface import is_swift_ui, is_terminal_ui, native_transcript_active

`PCODE_NATIVE_TRANSCRIPT=1` is exported by Pcode.app for session panes running
with the native AppKit transcript above the docked editor.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from enum import StrEnum

ENV_VAR = "PCODE_NATIVE_TRANSCRIPT"


class UISurface(StrEnum):
    TERMINAL = "terminal"
    SWIFT = "swift"


_override: UISurface | None = None


def current_surface() -> UISurface:
    """Return the active UI surface (`UISurface.SWIFT` or `UISurface.TERMINAL`)."""
    if _override is not None:
        return _override
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return UISurface.SWIFT
    return UISurface.TERMINAL


def is_swift_ui() -> bool:
    """True when running inside Pcode.app with the Swift UI enabled."""
    return current_surface() is UISurface.SWIFT


def is_terminal_ui() -> bool:
    """True when running in a standard terminal without the Swift UI."""
    return current_surface() is UISurface.TERMINAL


def native_transcript_active() -> bool:
    """True when the Swift app's native transcript is active over a docked editor.

    Checking `dock.active()` ensures that an undocked subshell or plain terminal
    session inside Pcode.app still renders terminal-side activity rows.
    """
    if not is_swift_ui():
        return False
    from pcode import dock

    return dock.active() is not None


def set_ui_surface(surface: UISurface | None) -> None:
    """Override the detected UI surface for the current process (pass `None` to clear)."""
    global _override
    _override = surface


@contextmanager
def override_ui_surface(surface: UISurface | None):
    """Temporarily override `current_surface()` (for tests and scoped checks)."""
    global _override
    previous = _override
    _override = surface
    try:
        yield
    finally:
        _override = previous
