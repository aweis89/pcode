"""Which scrollback rows start a turn or a block, for Ghostty Smooth's reader mode.

Renderables that start something a reader jumps to (a submitted prompt, a
code block, a diff) call `row_mark` to put an invisible sentinel on their
first row. Only `TerminalOutput` turns that on, around its own renders
(`marking`); it then takes the sentinel back out of each row (`take_mark`)
and writes the terminal's own mark sequence there instead
(`smooth_scroll.mark`). Every other console rendering the same classes, such
as popups and the session tree, never sees one.

The sentinel is a plain, unstyled segment of zero-width characters: Rich
writes it whether or not its file is a terminal (control segments are
dropped on a non-terminal file, which `TerminalOutput`'s StringIO is), and
it costs no cells, so wrapping, padding and cropping leave it on its row.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from rich.segment import Segment

from pcode.smooth_scroll import MARK_BLOCK, MARK_TURN

_KINDS = {MARK_TURN: "\u2061", MARK_BLOCK: "\u2062"}
_SENTINEL = re.compile("\u2063\u2064([\u2061\u2062])\u2064\u2063")
_BY_CHAR = {char: kind for kind, char in _KINDS.items()}

_enabled: ContextVar[bool] = ContextVar("pcode_row_marks", default=False)


@contextmanager
def marking(enabled: bool = True) -> Iterator[None]:
    """Let `row_mark` emit sentinels (or, with ``enabled`` False, stop it) inside."""
    token = _enabled.set(enabled)
    try:
        yield
    finally:
        _enabled.reset(token)


def row_mark(kind: int) -> list[Segment]:
    """Segments to yield first, tagging the row they land on; none unless marking."""
    if not _enabled.get():
        return []
    return [Segment(f"\u2063\u2064{_KINDS[kind]}\u2064\u2063")]


def take_mark(text: str) -> tuple[str, int | None]:
    """``text`` without sentinels, and the kind of the first one (turn wins a tie)."""
    kinds = [_BY_CHAR[char] for char in _SENTINEL.findall(text)]
    if not kinds:
        return text, None
    return _SENTINEL.sub("", text), min(kinds)
