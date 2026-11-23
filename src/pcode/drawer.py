"""Experimental: /btw threads in a drawer beside the terminal (`popup_view auto`).

Ghostty Smooth (the pcode fork of Ghostty) can lay a second terminal over the
right of a pane. pcode asks for it with one short APC, which names what to
show and never carries it:

    ESC _ pcode;?  ESC \\                    probe; the terminal answers `pcode;ok;1`
    ESC _ pcode;btw;<host>;<thread> ESC \\   show this host's /btw thread
    ESC _ pcode;close ESC \\                 close the drawer
    ESC _ pcode;hide ESC \\                  hide it, still running (under a full-screen popup)
    ESC _ pcode;show ESC \\                  show it again
    ESC _ pcode;focus ESC \\                 (from the drawer) give the keyboard back
    ESC _ pcode;session;<id> ESC \\          this pane shows session <id> (empty: none);
                                            restored windows run `pcode -c <id>` there

The drawer's terminal runs `pcode.btw_tail` with the session host's own Python
(`HostEntry.python`), which connects to the host as an observer
(`SessionHost`'s `observe` hello): it does not count as a terminal, may call
nothing, and is sent the side questions and each change to them. It draws them
as the popup's thread does, so the drawer has the terminal's font, size and
colors.

The editor replies to the thread while the drawer shows it; only drawing it
moves beside the conversation. Popups open over the drawer the same way, as a
terminal of their own (`pcode.popup_surface`, probed with `pcode;popup?`).
"""

import os
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path

PROTOCOL = 1
QUERY = b"\x1b_pcode;?\x1b\\"
# Overlay popups (`pcode.popup_surface`), asked after with the drawer.
POPUP_PROTOCOL = 1
# A terminal answering this or later also draws pick lists natively (`popup_surface`).
PICK_PROTOCOL = 2
POPUP_QUERY = b"\x1b_pcode;popup?\x1b\\"
# Any pcode reply, so a future shape is still kept out of typing.
REPLY = re.compile(rb"\x1b_pcode;([^\x1b\x07]{0,32})\x1b\\")
VERSION = re.compile(rb"ok;(\d{1,4})")
POPUP_VERSION = re.compile(rb"popup;(\d{1,4})")
CLOSE = "\x1b_pcode;close\x1b\\"
HIDE = "\x1b_pcode;hide\x1b\\"
SHOW = "\x1b_pcode;show\x1b\\"
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


_SESSION = re.compile(r"[0-9A-Za-z_-]{1,64}")


def session_marker(identity: str | None) -> str:
    """Tell the terminal which session this pane shows, so a restored window reopens it."""
    identity = identity if identity and _SESSION.fullmatch(identity) else ""
    return f"\x1b_pcode;session;{identity}\x1b\\"


class PaneSession:
    """Keep Ghostty Smooth told of the session on screen (a `Reconciler` item).

    `source` is read each tick, so /attach, /resume and /new need no hook.
    On close the marker is cleared: a pane whose pcode quit reopens a shell.
    """

    def __init__(self, source: Callable[[], str | None], fd: int | None) -> None:
        self.source = source
        self.fd = fd
        self.sent: str | None = None

    def tick(self) -> None:
        from pcode.terminal_notify import _write_all

        try:
            identity = self.source() or ""
        except Exception:  # noqa: BLE001 - never worth an error.
            return
        if identity != self.sent and _write_all(self.fd, session_marker(identity).encode()):
            self.sent = identity

    def close(self) -> None:
        from pcode.terminal_notify import _write_all

        if self.sent and _write_all(self.fd, session_marker(None).encode()):
            self.sent = None


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
