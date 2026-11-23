"""Mouse text selection inside popup panes, copied without wraps or borders.

A terminal selects screen rows: it cannot tell a wrapped line from a hard one,
or a pane's text from its frame and the pane beside it. Popups capture the
mouse anyway, so a pane selects its own text instead and copies what it knows
is there.

Rich wraps to the pane width by inserting real newlines, so `marked_rendering`
has `Text.wrap` mark the rows it split with zero-width characters, which
`unmark` reads back and strips before anything is drawn.

Positions are (row, character index) pairs, as prompt_toolkit's window hands
them to a control's mouse handler: a wide character is one position.

prompt_toolkit sends each mouse report to whatever is under the pointer, so a
drag that strays onto a scroll bar, a frame's padding or the pane beside it
would lose its moves and its release. `drag_capture` keys a popup on: while a
pane's drag is held, every move and the release go to that pane, clamped to
its edges.
"""

import re
import time
import weakref
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from rich._wrap import divide_line  # How Text.wrap finds its breaks.
from rich.cells import cell_len
from rich.containers import Lines
from rich.text import Text

from pcode.clipboard import copy as copy_to_clipboard

# Invisible operators: zero cells wide to Rich, and absent from anything a
# model or a tool prints in practice.
WRAP_END = "\u2063"  # This row continues on the next, broken between words.
FOLD_END = "\u2062"  # This row continues on the next, broken inside a word.
WRAP_START = "\u2064"  # A continued row's text starts here; before it is indent.
_MARKERS = re.compile(f"[{WRAP_END}{FOLD_END}{WRAP_START}]")
_ENDS = re.compile(f"[{WRAP_END}{FOLD_END}]")
# What may follow a wrapped row's text: padding, and the right edge of a box.
_TRAILING_CHROME = re.compile(r"[\s\u2500-\u257f]*")
# What a double-click word stops at besides whitespace: box and quote gutters.
_WORD = re.compile(r"[^\s\u2500-\u259f]+")
_SELECTED = "class:selected"
# Clicks closer together than this, in seconds, count as a double or triple click.
_MULTI_CLICK = 0.4

_marking: ContextVar[bool] = ContextVar("pcode_marking_wraps", default=False)
_rich_wrap = Text.wrap


def _marked_wrap(self: Text, console, width: int, **options) -> Lines:
    # Most text fits on one row; it has nothing to mark, so skip the bookkeeping.
    if not _marking.get() or ("\n" not in self.plain and cell_len(self.plain) <= width):
        return _rich_wrap(self, console, width, **options)
    # Rich splits on newlines before wrapping; wrap each hard line alone so a
    # row's marker says whether Rich broke it, not whether the text did.
    result = Lines()
    for piece in self.split("\n", allow_blank=True):
        rows = list(_rich_wrap(piece, console, width, **options))
        ends = _break_markers(piece, width, len(rows) - 1, **options)
        for index, row in enumerate(rows):
            if index:
                marked = row.blank_copy(WRAP_START)
                marked.append_text(row)
                row = marked
            if index < len(ends):
                row.append(ends[index])
            result.append(row)
    return result


def _break_markers(piece: Text, width: int, breaks: int, **options) -> list[str]:
    """Which marker ends each row Rich split ``piece`` into, but the last.

    Rich pads rows before they come back, so the break itself is found the way
    Rich found it. Should that ever disagree, a space is the likelier break.
    """
    if breaks <= 0:
        return []
    text = piece.plain.expandtabs(options.get("tab_size") or 8)
    overflow = options.get("overflow") or piece.overflow or "fold"
    offsets = divide_line(text, width, fold=overflow == "fold")
    if len(offsets) != breaks:
        return [WRAP_END] * breaks
    return [WRAP_END if text[offset - 1].isspace() else FOLD_END for offset in offsets]


Text.wrap = _marked_wrap  # type: ignore[method-assign]


@contextmanager
def marked_rendering() -> Iterator[None]:
    """Mark wrap points in whatever Rich renders on this thread meanwhile."""
    token = _marking.set(True)
    try:
        yield
    finally:
        _marking.reset(token)


@dataclass(frozen=True)
class Row:
    """One drawn row's plain text and where Rich's wrap left its content."""

    text: str
    # Indices into text: where a continued row's text starts, and where a
    # wrapped row's text ends; None when the row ends a line of its own.
    start: int = 0
    end: int | None = None
    # What rejoins a wrapped row to the next: a space, or nothing inside a word.
    joiner: str = " "


def unmark(line: StyleAndTextTuples) -> tuple[StyleAndTextTuples, Row]:
    """Strip wrap markers from one drawn row, returning it and what they said."""
    text = "".join(fragment[1] for fragment in line)
    if not _MARKERS.search(text):
        return line, Row(text)
    plain = _MARKERS.sub("", text)
    start = text.find(WRAP_START)
    start = len(_MARKERS.sub("", text[:start])) if start >= 0 else 0
    ends = list(_ENDS.finditer(text))
    end, joiner = None, " "
    # A cell wrapping inside a table row is not the row wrapping.
    if ends and _TRAILING_CHROME.fullmatch(text, ends[-1].end()):
        end = len(_MARKERS.sub("", text[: ends[-1].start()]))
        joiner = " " if ends[-1].group() == WRAP_END else ""
    stripped = [(style, _MARKERS.sub("", piece), *rest) for style, piece, *rest in line]
    return [fragment for fragment in stripped if fragment[1]], Row(plain, start, end, joiner)


def highlight(line: StyleAndTextTuples, start: int, end: int) -> StyleAndTextTuples:
    """``line`` with characters ``start`` up to ``end`` drawn as selected."""
    result: StyleAndTextTuples = [
        (f"{style} {_SELECTED}" if start <= index < end else style, character, *rest)
        for index, (style, character, *rest) in enumerate(explode_text_fragments(line))
    ]
    if end > len(result):
        # Past the text's end, as on a selected blank row: show the selection there.
        result.append((_SELECTED, " " * (end - max(start, len(result)))))
    return result


def copy_selected(text: str) -> str:
    """Copy a mouse selection to the clipboard, returning a notice saying how it went."""
    copied, truncated = copy_to_clipboard(text, get_app().output)
    if not copied:
        return "Could not copy the selection"
    lines = text.count("\n") + 1
    size = f"{lines} lines" if lines > 1 else f"{len(text)} characters"
    return f"Copied {size}" + (" (truncated)" if truncated else "")


Position = tuple[int, int]  # (row, character index)


class Selection:
    """A pane's mouse selection over rows from ``rows()``, copied on release."""

    def __init__(self, rows: Callable[[], list[Row]], on_select: Callable[[str], None]):
        self.rows = rows
        self.on_select = on_select
        # The window drawing these rows: a drag held here is captured to it.
        self.window: Window | None = None
        self.anchor: Position | None = None
        self.cursor: Position | None = None
        # Whether anything is selected: a drag that moved, or a word or line
        # clicked, which can be a single character with anchor == cursor.
        self.active = False
        self.pressed = False
        self._clicks: list[tuple[float, Position]] = []

    def clear(self) -> None:
        self.anchor = self.cursor = None
        self.active = self.pressed = False

    def span(self) -> tuple[Position, Position] | None:
        """The selection's first and last positions, both inclusive."""
        if not self.active or self.anchor is None or self.cursor is None:
            return None
        first, last = sorted((self.anchor, self.cursor))
        return first, last

    def columns(self, row: int) -> tuple[int, int] | None:
        """Selected character indices on ``row``, end exclusive."""
        span = self.span()
        # Checked before rows(): a processor asks this of every drawn row.
        if span is None or not span[0][0] <= row <= span[1][0]:
            return None
        rows = self.rows()
        if row >= len(rows):
            return None
        (first_row, first_index), (last_row, last_index) = span
        start = first_index if row == first_row else 0
        if row == last_row:
            return start, last_index + 1
        # Through the end of a middle row's text, so a blank row shows selected too.
        return start, max(start + 1, len(rows[row].text.rstrip()))

    def text(self) -> str:
        """The selected text: wrapped rows rejoined, indent and padding dropped."""
        span = self.span()
        if span is None:
            return ""
        rows = self.rows()
        (first_row, first_index), (last_row, last_index) = span
        pieces: list[str] = []
        for number in range(first_row, min(last_row, len(rows) - 1) + 1):
            row = rows[number]
            continued = number > 0 and rows[number - 1].end is not None
            # A continued row's indent is never text, wherever the drag began.
            start = row.start if continued else 0
            if number == first_row:
                start = max(start, first_index)
            end = len(row.text) if row.end is None else row.end
            if number == last_row:
                end = min(end, last_index + 1)
            # Rich pads a row to the width; the break itself becomes the joiner.
            piece = row.text[start:end].rstrip()
            if continued and rows[number - 1].joiner:
                # Centred or right-aligned rows are padded on the left too.
                piece = piece.lstrip()
            if row.end is not None and number < last_row and end > start:
                piece += row.joiner
            if continued and number > first_row and pieces:
                pieces[-1] += piece
            else:
                pieces.append(piece)
        return "\n".join(pieces).strip("\n")

    def handle(self, event: MouseEvent):
        """Track a press, drag and release.

        None when what is drawn changed, so prompt_toolkit redraws; otherwise
        NotImplemented, as for anything else (the wheel). Every redraw is
        output, and a terminal restarts its cursor's blink on any output, so
        a plain click must not cause one.
        """
        position = (event.position.y, event.position.x)
        kind = event.event_type
        shown = self.span()
        # urxvt's encoding reports no button on a press.
        if kind == MouseEventType.MOUSE_DOWN and event.button in (
            MouseButton.LEFT,
            MouseButton.UNKNOWN,
        ):
            self.anchor = self.cursor = position
            self.active, self.pressed = False, True
            if self.window is not None:
                _held[get_app()] = self
        elif kind == MouseEventType.MOUSE_MOVE and self.pressed:
            # Any-motion tracking reports hovering too. A move with no button
            # held means the release was never seen: end the drag where it
            # last was, not where the pointer came back in.
            if event.button == MouseButton.NONE:
                if self._release(self.cursor or position, click=False):
                    return None
            else:
                self.cursor = position
                self.active = self.active or position != self.anchor
        elif kind == MouseEventType.MOUSE_UP and self.pressed:
            if self._release(position, click=True):
                return None
        else:
            return NotImplemented
        return None if self.span() != shown else NotImplemented

    def _release(self, position: Position, *, click: bool) -> bool:
        """End the drag at ``position``; whether it copied anything."""
        self.pressed = False
        self.cursor = position
        self.active = self.active or position != self.anchor
        clicks = self._count_click(position) if click else 1
        if not self.active and clicks > 1:
            self._select_around(position, whole_line=clicks > 2)
        selected = self.text()
        if selected:
            self.on_select(selected)
        return bool(selected)

    def _count_click(self, position: Position) -> int:
        now = time.monotonic()
        self._clicks = [
            (moment, spot)
            for moment, spot in self._clicks
            if now - moment < _MULTI_CLICK and spot == position
        ][-2:]
        self._clicks.append((now, position))
        return len(self._clicks)

    def _select_around(self, position: Position, *, whole_line: bool) -> None:
        rows = self.rows()
        number, index = position
        if not 0 <= number < len(rows):
            return
        if whole_line:
            first = last = number
            while first and rows[first - 1].end is not None:
                first -= 1
            while last < len(rows) - 1 and rows[last].end is not None:
                last += 1
            text = rows[last].text.rstrip()
            end = len(text) if rows[last].end is None else rows[last].end
            self.anchor, self.cursor = (first, 0), (last, max(0, end - 1))
            self.active = True
            return
        for match in _WORD.finditer(rows[number].text):
            if match.start() <= index < match.end():
                self.anchor, self.cursor = (number, match.start()), (number, match.end() - 1)
                self.active = True
                return


def copy_drags(area, notify: Callable[[str], None]) -> None:
    """Drag-to-copy over a read-only TextArea, reporting each copy through ``notify``.

    A press still reaches the area, so a click moves its cursor, which is how
    a list selects its row. The drag, the release and double- and
    triple-clicks are tracked here instead of in the buffer's own selection:
    that one only works once the area has focus, and a list rebuilding its
    text on a timer moves the cursor under a click, which the buffer then
    reads as a drag.
    """
    control = area.control
    handle = control.mouse_handler
    selection = Selection(
        lambda: [Row(line) for line in area.document.lines],
        lambda text: notify(copy_selected(text)),
    )
    selection.window = area.window

    def source(event: MouseEvent) -> MouseEvent:
        # The window reports drawn columns; a prompt or an ellipsis shifts them.
        x, y = event.position.x, event.position.y
        processed = control._last_get_processed_line
        if processed is not None:
            try:
                x = processed(y).display_to_source(x)
            except IndexError:
                pass
        return MouseEvent(Point(x, y), event.event_type, event.button, event.modifiers)

    def handler(event: MouseEvent):
        kind = event.event_type
        if kind in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            return handle(event)
        if kind == MouseEventType.MOUSE_DOWN:
            redraw = selection.handle(source(event)) is None
            if not selection.pressed:
                return handle(event)
            # The press is the drag's; the buffer's cursor still follows it.
            return None if handle(event) is None or redraw else NotImplemented
        if not selection.pressed:
            # A drag that began elsewhere: never the buffer's own selection.
            return NotImplemented
        result = selection.handle(source(event))
        clicked = event.event_type == MouseEventType.MOUSE_UP and selection.span() is None
        if clicked and get_app().layout.current_control is not control:
            # A plain click on an unfocused area: let it focus, if it does that.
            return handle(event)
        return result

    def forget(_) -> None:
        if not selection.pressed:
            selection.clear()

    class Highlight(Processor):
        def apply_transformation(self, ti: TransformationInput) -> Transformation:
            columns = selection.columns(ti.lineno)
            if columns is None:
                return Transformation(ti.fragments)
            return Transformation(highlight(ti.fragments, *columns))

    control.mouse_handler = handler
    # First, so it sees the buffer's own columns before a prompt or an ellipsis.
    control.input_processors.insert(0, Highlight())
    area.buffer.on_text_changed += forget
    area.buffer.on_cursor_position_changed += forget


# The selection whose drag is held, per app: popups run beside the main app.
_held: "weakref.WeakKeyDictionary[object, Selection]" = weakref.WeakKeyDictionary()
_DRAG_EVENTS = (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP)


def _to_window(window: Window, event: MouseEvent):
    """Hand a screen-position ``event`` to ``window``'s control, clamped to what it drew.

    Above the window is its first drawn row's start, below it the last row's
    end, and past either side that row's nearest character: where a terminal's
    own selection would stop.
    """
    info = window.render_info
    if info is None or not info.visible_line_to_row_col:
        return NotImplemented
    # Content (row, column) to screen (y, x), as the window drew it last.
    cells = {yx: rowcol for rowcol, yx in info._rowcol_to_yx.items()}
    top, left = info._y_offset, info._x_offset
    bottom = top + len(info.visible_line_to_row_col) - 1
    right = left + info.window_width - 1
    y, x = event.position.y, event.position.x
    if y < top:
        y, x = top, left
    elif y > bottom:
        y, x = bottom, right
    # A wide character's right half, or a short row's end, draws no cell.
    for x in range(min(max(x, left), right), left - 1, -1):
        if (y, x) in cells:
            row, column = cells[y, x]
            break
    else:
        return NotImplemented
    return window.content.mouse_handler(
        MouseEvent(Point(column, row), event.event_type, event.button, event.modifiers)
    )


def drag_capture() -> KeyBindings:
    """Mouse reports that send a held drag's moves and release to its own pane.

    The report is still decoded by prompt_toolkit's own binding; only the
    handler it looks up changes, for the length of that one report.
    """
    decode = next(
        binding.handler
        for binding in load_mouse_bindings().bindings
        if binding.keys == (Keys.Vt100MouseEvent,)
    )
    bindings = KeyBindings()

    @bindings.add(Keys.Vt100MouseEvent)
    def _(event):
        held = _held.get(event.app)
        if held is None or not held.pressed or held.window is None:
            _held.pop(event.app, None)
            return decode(event)
        renderer = event.app.renderer
        drawn = renderer.mouse_handlers

        def route(mouse_event: MouseEvent):
            if mouse_event.event_type in _DRAG_EVENTS:
                return _to_window(held.window, mouse_event)
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                # The drag's release was never seen: this press starts anew.
                held.pressed = False
            position = mouse_event.position
            return drawn.mouse_handlers[position.y][position.x](mouse_event)

        captured = MouseHandlers()
        captured.mouse_handlers = defaultdict(lambda: defaultdict(lambda: route))
        renderer.mouse_handlers = captured
        try:
            return decode(event)
        finally:
            if renderer.mouse_handlers is captured:
                renderer.mouse_handlers = drawn

    return bindings
