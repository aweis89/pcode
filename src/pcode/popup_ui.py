"""Shared terminal-native styling for alternate-screen popups."""

import re
from io import StringIO

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text, split_lines
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style, merge_styles
from rich.console import Console
from rich.theme import Theme

# Rich writes Markdown links as OSC 8 hyperlinks on a terminal, but
# prompt_toolkit's ANSI parser reads CSI only and spills the rest as literal
# text ("8;id=1;https://…"). A pane cannot follow a link anyway, so drop them.
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Framed selector panes: rows of list content, before the frame's own borders.
LIST_ROWS_MIN = 3
LIST_ROWS_MAX = 6
_BORDERS = 2

_NATIVE = "bg:default fg:default noreverse"
POPUP_STYLE = Style.from_dict(
    {
        "popup": _NATIVE,
        "popup dialog": _NATIVE,
        "popup dialog.body": _NATIVE,
        "popup text-area": _NATIVE,
        "popup frame.border": _NATIVE,
        "popup frame.label": f"{_NATIVE} bold",
        "popup shadow": _NATIVE,
        "popup scrollbar.background": _NATIVE,
        "popup scrollbar.button": f"{_NATIVE} reverse",
        "popup scrollbar.arrow": f"{_NATIVE} bold",
        "popup selected": f"{_NATIVE} reverse",
        "popup cursor-line": f"{_NATIVE} reverse nounderline",
        "popup text-area last-line": "nounderline",
    }
)


def popup_container(body):
    """Scope every modal surface under the same style class."""
    return HSplit([body], style="class:popup")


def popup_style(base=None):
    """Preserve caller accents while overriding toolkit popup surfaces."""
    return merge_styles([base, POPUP_STYLE] if base is not None else [POPUP_STYLE])


def steer_list_from_query(keys, query, listing) -> None:
    """Keep typing in the query while ↑/↓ move the selection in a TextArea list.

    A one-line query swallows vertical movement otherwise, which is what a
    filter-as-you-type list makes people expect least.
    """
    typing = has_focus(query)

    def page() -> int:
        info = listing.window.render_info
        return max(1, info.window_height - 1) if info else 10

    @keys.add("up", filter=typing)
    @keys.add("c-p", filter=typing)
    def previous(event):
        listing.buffer.cursor_up()

    @keys.add("down", filter=typing)
    @keys.add("c-n", filter=typing)
    def following(event):
        listing.buffer.cursor_down()

    @keys.add("pageup", filter=typing)
    def previous_page(event):
        listing.buffer.cursor_up(page())

    @keys.add("pagedown", filter=typing)
    def following_page(event):
        listing.buffer.cursor_down(page())


class RichPane:
    """A read-only, scrollable pane showing Rich renderables (Markdown, Text).

    Rich output and prepared lines are cached until the content or width changes.
    Scrolling only creates a lightweight UIContent with the current cursor row;
    it must not rescan the entire transcript on every frame.
    """

    def __init__(
        self, *, theme: Theme | None = None, color_system: str | None = "truecolor"
    ) -> None:
        self.theme = theme
        self.color_system = color_system
        self.renderables: list = []
        self._version = 0
        self._cache: tuple[int, int, list] | None = None
        self._line_cache: tuple[int, int, list] | None = None
        pane = self

        class Control(UIControl):
            def is_focusable(self):
                return True

            def preferred_width(self, max_available_width):
                # Rich wraps to the allocated width; measuring the entire text
                # here is both unnecessary and expensive for long transcripts.
                return max_available_width

            def preferred_height(self, width, max_available_height, wrap_lines, get_line_prefix):
                return len(pane.lines(width))

            def create_content(self, width, height):
                lines = pane.lines(width)
                return UIContent(
                    get_line=lines.__getitem__,
                    line_count=len(lines),
                    show_cursor=False,
                    cursor_position=Point(0, pane.window.vertical_scroll),
                )

        # The window scrolls to keep the reported cursor visible on every render,
        # so a fixed row 0 would snap keyboard scrolling straight back to the top.
        self.control = Control()
        self.window = Window(
            self.control, wrap_lines=False, right_margins=[ScrollbarMargin(display_arrows=True)]
        )

    def set(self, renderables: list) -> None:
        self.renderables = renderables
        self._version += 1
        self.window.vertical_scroll = 0

    def fragments(self, width: int) -> list:
        key = (self._version, width)
        if self._cache is None or self._cache[:2] != key:
            console = Console(
                file=StringIO(),
                force_terminal=True,
                color_system=self.color_system,
                width=max(1, width),
                theme=self.theme,
                highlight=False,
            )
            for renderable in self.renderables:
                console.print(renderable)
            rendered = OSC.sub("", console.file.getvalue().rstrip("\n"))
            self._cache = (*key, to_formatted_text(ANSI(rendered)))
        return self._cache[2]

    def lines(self, width: int) -> list:
        key = (self._version, width)
        if self._line_cache is None or self._line_cache[:2] != key:
            self._line_cache = (*key, list(split_lines(self.fragments(width))))
        return self._line_cache[2]

    def text(self, width: int = 80) -> str:
        """Unstyled rendering without Rich's line padding, for tests and logs."""
        lines = fragment_list_to_text(self.fragments(width)).splitlines()
        return "\n".join(line.rstrip() for line in lines)

    def __pt_container__(self):
        return self.window

    def bind_scrolling(self, keys) -> None:
        focused = has_focus(self.window)

        def scroll(direction: int, page: bool = False, half: bool = False):
            def handler(event):
                info = self.window.render_info
                if info is None:
                    return
                rows = (
                    max(1, info.window_height // 2)
                    if half
                    else (max(1, info.window_height - 1) if page else 1)
                )
                bottom = max(0, info.content_height - info.window_height)
                self.window.vertical_scroll = max(
                    0, min(bottom, self.window.vertical_scroll + direction * rows)
                )

            return handler

        keys.add("up", filter=focused)(scroll(-1))
        keys.add("down", filter=focused)(scroll(1))
        keys.add("pageup", filter=focused)(scroll(-1, page=True))
        keys.add("pagedown", filter=focused)(scroll(1, page=True))
        keys.add("c-u", filter=focused)(scroll(-1, half=True))
        keys.add("c-d", filter=focused)(scroll(1, half=True))


def list_pane_height(rows: int = LIST_ROWS_MAX) -> Dimension:
    """Height for a framed selector stacked above a scrolling detail pane.

    ``Dimension`` defaults ``preferred`` to ``min``, and ``HSplit`` gives every
    child its preferred height before letting any of them grow beyond it. With
    no explicit preferred, a long diff or tool payload squeezes the selector
    down to its minimum and the list effectively disappears, so ask for the
    rows up front.
    """
    visible = max(LIST_ROWS_MIN, min(LIST_ROWS_MAX, rows))
    return Dimension(
        min=1 + _BORDERS,
        preferred=visible + _BORDERS,
        max=LIST_ROWS_MAX + _BORDERS,
    )
