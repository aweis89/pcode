"""Derive prompt-chrome colors from the selected Pygments style.

A Pygments style is written for code sitting on its own background, not for
UI chrome, so nothing in it can be trusted as-is:

- Token colors are optional. `gruvbox-light` leaves `Token.Text` uncolored.
- `highlight_color` is only loosely related to the background. `gruvbox-dark`
  pairs a near-white highlight (#ebdbb2) with near-white text (#dddddd), so
  using it as the selected-row background would hide the row.
- Nothing forces the style saved for the dark palette to actually be dark:
  `/syntax solarized-light` is accepted while `/theme` is dark.

Every derived color is therefore checked against the surface it will be
painted on, and falls back to the palette's own value (then to a blend) when
the contrast is too low. Brightness uses the same weighting as
`theme.background_theme`, so "is this light or dark" means one thing here.
"""

import re
from collections.abc import Iterable, Mapping

from pygments.styles import get_style_by_name
from pygments.token import Token
from pygments.util import ClassNotFound

# The fields a Palette is built from; every one of them is produced below.
FIELDS = ("accent", "muted", "surface", "foreground", "selected", "task_heading")

_HEX = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_WEIGHTS = (0.299, 0.587, 0.114)

# Minimum brightness gap between a color and the surface behind it. Text has
# to be read, so it demands more than a background band that only has to be
# seen. Both are deliberately loose: the aim is to catch invisible pairings,
# not to impose a contrast standard on a style the user chose on purpose.
_TEXT_GAP = 0.3
_ACCENT_GAP = 0.18
_BAND_GAP = 0.04


def _normalize(color: str | None) -> str | None:
    """Return `#rrggbb`, or None when the style left the value unusable."""
    if not color or not _HEX.match(color):
        return None
    digits = color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    return f"#{digits.lower()}"


def _rgb(color: str) -> tuple[float, ...]:
    digits = color.lstrip("#")
    return tuple(int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _brightness(color: str) -> float:
    return sum(value * weight for value, weight in zip(_rgb(color), _WEIGHTS))


def _mix(color: str, other: str, weight: float) -> str:
    blended = (
        round((left * (1 - weight) + right * weight) * 255)
        for left, right in zip(_rgb(color), _rgb(other))
    )
    return "#" + "".join(f"{value:02x}" for value in blended)


def _pick(
    candidates: Iterable[str | None],
    requirements: Iterable[tuple[str, float]],
    default: str,
) -> str:
    """First candidate keeping the required brightness gap from every backdrop."""
    requirements = tuple(requirements)
    for candidate in candidates:
        color = _normalize(candidate)
        if color and all(
            abs(_brightness(color) - _brightness(backdrop)) >= gap for backdrop, gap in requirements
        ):
            return color
    return default


def _token(style, token) -> str | None:
    return dict(style).get(token, {}).get("color")


def derive_colors(style_name: str, fallback: Mapping[str, str]) -> dict[str, str]:
    """Palette field values for `style_name`, backed by `fallback`'s values.

    An unknown style name yields the fallback unchanged: a saved preference
    can outlive the Pygments plugin that provided the style.
    """
    try:
        style = get_style_by_name(style_name)
    except ClassNotFound:
        return {field: fallback[field] for field in FIELDS}

    surface = _normalize(style.background_color) or fallback["surface"]
    foreground = _pick(
        (_token(style, Token.Text), fallback["foreground"]),
        ((surface, _TEXT_GAP),),
        "#ffffff" if _brightness(surface) < 0.5 else "#1c1c1c",
    )
    # The selected row is a background: it only has to separate from the
    # surface, but whatever is written on it still has to stay readable. The
    # palette's own `selected` is not a candidate -- it belongs to a different
    # background, so a blend off this style's surface stays more coherent.
    selected = _pick(
        (style.highlight_color, getattr(style, "line_number_background_color", None)),
        ((surface, _BAND_GAP), (foreground, _TEXT_GAP)),
        _mix(surface, foreground, 0.25),
    )
    return {
        "surface": surface,
        "foreground": foreground,
        "selected": selected,
        # The accent is written on the surface and on the selected row both.
        "accent": _pick(
            (
                _token(style, Token.Name.Function),
                _token(style, Token.Keyword),
                fallback["accent"],
            ),
            ((surface, _ACCENT_GAP), (selected, _ACCENT_GAP)),
            foreground,
        ),
        "muted": _pick(
            (_token(style, Token.Comment), fallback["muted"]),
            ((surface, _ACCENT_GAP),),
            _mix(foreground, surface, 0.45),
        ),
        "task_heading": _pick(
            (
                _token(style, Token.Keyword),
                _token(style, Token.Name.Class),
                fallback["task_heading"],
            ),
            ((surface, _ACCENT_GAP),),
            foreground,
        ),
    }
