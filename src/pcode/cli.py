"""Lightweight entry point: silence terminal echo before importing the frontend."""

import sys
from contextlib import ExitStack

_startup: ExitStack | None = None


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
        # Leave canonical editing and signals alone. The kernel queues typeahead;
        # no reader thread, input flushing, or raw-mode shell is needed.
        quiet[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    except (OSError, ValueError, termios.error):
        return

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except (OSError, termios.error):
            pass

    stack.callback(restore)


def main() -> None:
    global _startup
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
