"""Escape sequences for the terminal emulator itself, not the screen: desktop
notifications (OSC 9) and tab progress (OSC 9;4).

Ghostty shows both by default (`desktop-notifications`, `progress-style`);
iTerm2 and WezTerm take OSC 9 too, and terminals that know neither ignore an
unknown OSC. tmux drops them unless wrapped for passthrough and the server has
`allow-passthrough on`.
"""

import os
import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _outer(sequence: str) -> str:
    """Wrap for tmux, which otherwise swallows OSC sequences it does not know."""
    if os.environ.get("TMUX"):
        return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    return sequence


def notification(text: str) -> str:
    # Control characters would end the sequence early; and a body starting
    # with "4;" would be read as a progress report, which "pcode" never is.
    body = _CONTROL.sub(" ", text).strip()[:200]
    return _outer(f"\x1b]9;{body}\x07")


def progress(active: bool) -> str:
    """Indeterminate progress while working (state 3), cleared when idle (state 0)."""
    return _outer("\x1b]9;4;3\x07" if active else "\x1b]9;4;0\x07")


def send(output, sequence: str) -> None:
    """Queue on a prompt_toolkit output; it goes out with the next repaint.

    Never flushed here: scrollback handoffs build one atomic write across
    awaits, and flushing in the middle of one sends half a frame early (the
    tmux cursor tests caught exactly that). An invisible OSC riding along
    with a repaint changes nothing on screen.
    """
    try:
        output.write_raw(sequence)
    except Exception:  # noqa: BLE001 - a notification is never worth an error.
        pass
