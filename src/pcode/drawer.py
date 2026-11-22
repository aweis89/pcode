"""Experimental: /btw threads in a drawer beside the terminal (`btw_view drawer`).

Ghostty Smooth (the pcode fork of Ghostty) can lay a second terminal over the
right of a pane. pcode asks for it with one short APC, which names what to
show and never carries it:

    ESC _ pcode;?  ESC \\                    probe; the terminal answers `pcode;ok;1`
    ESC _ pcode;btw;<host>;<thread> ESC \\   show this host's /btw thread
    ESC _ pcode;close ESC \\                 close the drawer
    ESC _ pcode;focus ESC \\                 (from the drawer) give the keyboard back

The drawer's terminal runs `pcode.btw_tail` with the session host's own Python
(`HostEntry.python`), which connects to the host as an observer
(`SessionHost`'s `observe` hello): it does not count as a terminal, may call
nothing, and is sent the side questions and each change to them. It draws them
as the swap does, so the drawer has the terminal's font, size and colors.

The editor keeps replying to the thread as with `btw_view swap`; only drawing
it moves beside the conversation.
"""

import os
import re
from functools import cache
from pathlib import Path

PROTOCOL = 1
QUERY = b"\x1b_pcode;?\x1b\\"
# Any pcode reply, so a future shape is still kept out of typing.
REPLY = re.compile(rb"\x1b_pcode;([^\x1b\x07]{0,32})\x1b\\")
VERSION = re.compile(rb"ok;(\d{1,4})")
CLOSE = "\x1b_pcode;close\x1b\\"
FOCUS = "\x1b_pcode;focus\x1b\\"
# What the drawer's hello sends: it speaks this, not the terminals' `PROTOCOL`.
# 1 was a web page fed rendered HTML; 2 is the raw side questions.
OBSERVER_VERSION = 2

# What the terminal accepts as a host id and a thread (it checks again).
_ID = re.compile(r"[0-9a-f]{8}")
_THREAD = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_host(host: str) -> bool:
    return _ID.fullmatch(host) is not None


def open_btw(host: str, thread: str) -> str:
    if not (_ID.fullmatch(host) and _THREAD.fullmatch(thread)):
        raise ValueError(f"Not a drawer host and thread: {host!r}, {thread!r}")
    return f"\x1b_pcode;btw;{host};{thread}\x1b\\"


def default_host_dir() -> Path:
    """Where the terminal looks for hosts: it cannot see this process's environment."""
    return Path.home() / ".local" / "state" / "pcode" / "hosts"


@cache
def reachable(directory: Path) -> bool:
    """Whether the terminal app can find hosts in `directory`: local, and the default place.

    Cached: asked every frame, and neither answer changes while pcode runs.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        return directory.resolve() == default_host_dir().resolve()
    except OSError:
        return False
