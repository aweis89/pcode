"""Lightweight entry point: silence terminal echo before importing the frontend."""

import signal
import sys
from contextlib import ExitStack

_startup: ExitStack | None = None
# SIGUSR2 (dev/app-restart.md) arrived before the editor could act on it.
app_restart_signalled = False


def remember_app_restart(signum=None, frame=None) -> None:
    """SIGUSR2 outside the editor: noted for `PreviewApp.run_async`, never the default death."""
    global app_restart_signalled
    app_restart_signalled = True


def restore_stdin() -> None:
    """Hand terminal settings back before the editor or a launch question takes over."""
    if _startup is not None:
        _startup.close()


def ask(prompt: str) -> str:
    restore_stdin()
    return input(prompt)


def _quiet_stdin(stack: ExitStack) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    try:
        import termios
    except ImportError:
        return
    try:
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        # The kernel queues typeahead; no reader thread or input flushing is
        # needed. Canonical mode goes too: canonical without echo is how
        # terminals such as Ghostty recognize a password prompt. Signals stay.
        quiet[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
        quiet[6][termios.VMIN] = 1
        quiet[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    except OSError, ValueError, termios.error:
        return

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except OSError, termios.error:
            pass

    stack.callback(restore)


def main() -> None:
    global _startup
    from pcode.completion import COMPLETE_COMMAND

    if sys.argv[1:2] == [COMPLETE_COMMAND]:
        # Shell completion calls back on every Tab: skip the frontend import.
        from pcode.completion import print_candidates

        sys.exit(print_candidates(sys.argv[2:]))
    # First, before the slow imports: the default action would kill the process.
    signal.signal(signal.SIGUSR2, remember_app_restart)
    from pcode.proctitle import rename

    rename()
    with ExitStack() as stack:
        _startup = stack
        _quiet_stdin(stack)
        try:
            from pcode.app import main as run

            run()
        finally:
            _startup = None


if __name__ == "__main__":
    from pcode.cli import main

    main()
