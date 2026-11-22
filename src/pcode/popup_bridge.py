"""Hands an overlay terminal to the pcode that asked for it (`pcode.popup_surface`).

Ghostty Smooth runs this in a popup surface it laid over pcode's pane, as
`python -I -m pcode.popup_bridge <token>`. It connects to the socket pcode is
listening on, passes its terminal (fd 0) across with SCM_RIGHTS, and then only
waits: pcode reads the keys and draws the popup itself. While it waits it
tells pcode when the terminal was resized (one byte, `w`), since only this
process, in the terminal's foreground group, hears SIGWINCH.

Standard library only, and nothing from pcode: it starts in a few
milliseconds, and the terminal it serves stays as pcode left it.
"""

import os
import re
import signal
import socket
import sys
from pathlib import Path

PROTOCOL = 1
HELLO = b"pcode-popup %d\n" % PROTOCOL
RESIZED = b"w"
_TOKEN = re.compile(r"[0-9a-f]{16}")
# After pcode is done, how long to wait for the terminal to close this process.
_LINGER = 2.0


def popup_dir() -> Path:
    """Where the terminal looks for popups: it cannot see pcode's environment."""
    return Path.home() / ".local" / "state" / "pcode" / "popups"


def socket_path(token: str) -> Path:
    if _TOKEN.fullmatch(token) is None:
        raise ValueError(f"Not a popup token: {token!r}")
    return popup_dir() / f"{token}.sock"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or _TOKEN.fullmatch(argv[1]) is None:
        print("usage: python -m pcode.popup_bridge TOKEN", file=sys.stderr)
        return 2
    # Ctrl+C, Ctrl+\ and Ctrl+Z only reach this process when pcode hands the
    # terminal to a program in cooked mode ($EDITOR): they are that program's.
    for name in ("SIGINT", "SIGQUIT", "SIGTSTP"):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    # The terminal closing the overlay hangs it up, which must end this
    # process even if whatever started it ignored hangups (nohup, say).
    for name in ("SIGHUP", "SIGTERM"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
    path = socket_path(argv[1])
    try:
        info = path.lstat()
        if info.st_uid != os.getuid():
            return 1
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(path))
        socket.send_fds(connection, [HELLO], [0])
    except OSError:
        return 1
    connection.settimeout(None)

    def resized(signum, frame) -> None:
        try:
            connection.send(RESIZED)
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, resized)
    while True:
        try:
            if not connection.recv(64):
                break
        except InterruptedError:
            continue
        except OSError:
            break
    connection.close()
    # pcode closed its end once the popup was over and told the terminal so;
    # the terminal closing the surface ends this process. Exiting first would
    # only race it.
    signal.signal(signal.SIGWINCH, signal.SIG_DFL)
    signal.signal(signal.SIGALRM, lambda signum, frame: None)
    signal.setitimer(signal.ITIMER_REAL, _LINGER)
    signal.pause()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
