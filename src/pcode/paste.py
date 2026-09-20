"""Collapse large pastes in the editor to a short preview plus a size marker.

A multi-kilobyte paste turns the prompt into a wall of text that hides the
question being typed around it. Pastes longer than ``PASTE_THRESHOLD`` are
inserted as ``<preview>…[+N chars]`` instead; the full text is kept aside and
substituted back into the buffer when the prompt is submitted, so the model
still receives everything. A placeholder the user has edited no longer matches
and is sent verbatim, which is deliberate: the marker is visible and the
transcript shows exactly what went out.
"""

from __future__ import annotations

import re

PASTE_THRESHOLD = 100
PREVIEW_CHARS = 60
MARKER_PATTERN = re.compile(r"…\[\+[\d,]+ chars\]")

__all__ = ["MARKER_PATTERN", "PASTE_THRESHOLD", "PastedText"]


class PastedText:
    """Placeholders currently standing in for pasted text in the editor."""

    def __init__(self) -> None:
        self._full: dict[str, str] = {}

    def collapse(self, text: str) -> str:
        """Return what to insert for ``text``: itself, or a placeholder."""
        if len(text) <= PASTE_THRESHOLD:
            return text
        preview = text[:PREVIEW_CHARS].replace("\n", "⏎")
        placeholder = f"{preview}…[+{len(text) - PREVIEW_CHARS:,} chars]"
        self._full[placeholder] = text
        return placeholder

    def expand(self, text: str) -> str:
        """Substitute every intact placeholder back with its full paste."""
        for placeholder, full in self._full.items():
            text = text.replace(placeholder, full)
        return text

    def clear(self) -> None:
        self._full.clear()
