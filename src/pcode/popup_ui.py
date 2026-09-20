"""Shared terminal-native styling for alternate-screen popups."""

from io import StringIO

from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.key_binding.bindings.scroll import (
    scroll_one_line_down,
    scroll_one_line_up,
    scroll_page_down,
    scroll_page_up,
)
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style, merge_styles
from rich.console import Console
from rich.theme import Theme

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

    ``TextArea`` holds plain text only, so Rich output is rendered to ANSI at
    the pane's real width on each layout pass and cached until the content or
    width changes.
    """

    def __init__(
        self, *, theme: Theme | None = None, color_system: str | None = "truecolor"
    ) -> None:
        self.theme = theme
        self.color_system = color_system
        self.renderables: list = []
        self._version = 0
        self._cache: tuple[int, int, list] | None = None
        pane = self

        class Control(FormattedTextControl):
            def create_content(self, width, height):
                self.text = pane.fragments(width)
                # preferred_width already cached the previous frame's text for
                # this render pass; without clearing, every frame lags by one.
                self._fragment_cache.clear()
                return super().create_content(width, height)

        self.control = Control("", focusable=True, show_cursor=False)
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
            self._cache = (*key, to_formatted_text(ANSI(console.file.getvalue().rstrip("\n"))))
        return self._cache[2]

    def text(self, width: int = 80) -> str:
        """Unstyled rendering without Rich's line padding, for tests and logs."""
        lines = fragment_list_to_text(self.fragments(width)).splitlines()
        return "\n".join(line.rstrip() for line in lines)

    def __pt_container__(self):
        return self.window

    def bind_scrolling(self, keys) -> None:
        focused = has_focus(self.window)
        keys.add("up", filter=focused)(scroll_one_line_up)
        keys.add("down", filter=focused)(scroll_one_line_down)
        keys.add("pageup", filter=focused)(scroll_page_up)
        keys.add("pagedown", filter=focused)(scroll_page_down)


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
