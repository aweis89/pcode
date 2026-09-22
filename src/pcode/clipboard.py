"""Best-effort system clipboard for popups; never raises at the call site.

A local helper (`pbcopy`, `wl-copy`, `xclip`) is tried first because it needs no
cooperation from the terminal emulator. OSC 52 is the fallback, and goes first
over ssh, where the helper would target the clipboard of the wrong machine.
tmux only forwards OSC 52 with `set -g set-clipboard on`, so neither path alone
is enough.
"""

import base64
import os
import shutil
import subprocess

_HELPERS = (
    ("pbcopy",),
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
    ("clip.exe",),
)

# Terminals drop oversized OSC 52 payloads, and a clipboard is not a transfer
# channel: truncate loudly rather than copy something silently incomplete.
LIMIT = 64 * 1024


def osc52(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def _helper(text: str, output=None) -> bool:
    for helper in _HELPERS:
        if shutil.which(helper[0]) is None:
            continue
        try:
            # The caller owns the alternate screen: a chatty helper must not
            # write over it, so its streams are discarded rather than inherited.
            subprocess.run(
                helper,
                input=text.encode("utf-8", "replace"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _terminal(text: str, output) -> bool:
    if output is None:
        return False
    try:
        output.write_raw(osc52(text))
        output.flush()
        return True
    except Exception:
        return False


def copy(text: str, output=None) -> tuple[bool, bool]:
    """Copy `text`, returning whether it worked and whether it was truncated.

    `output` is a prompt_toolkit output to write OSC 52 to; without one, only a
    local helper can copy.
    """
    truncated = len(text) > LIMIT
    if truncated:
        text = text[:LIMIT] + "\n[truncated by pcode at 64 KiB]"
    remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    order = (_terminal, _helper) if remote else (_helper, _terminal)
    return any(attempt(text, output) for attempt in order), truncated
