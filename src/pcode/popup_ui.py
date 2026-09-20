"""Shared terminal-native styling for alternate-screen popups."""

from prompt_toolkit.layout import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style, merge_styles

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
