"""Shared terminal-native styling for alternate-screen popups."""

from prompt_toolkit.layout import HSplit
from prompt_toolkit.styles import Style, merge_styles

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
