"""The colors of the iTerm2 profile this terminal is using, for `run.py --iterm`.

iTerm2 keeps profiles in its preferences plist. A profile with "Use Separate
Colors for Light and Dark Mode" holds both sets under `(Light)` and `(Dark)`
keys, so the set shown is whichever macOS appearance is active now.

Not covered: Dynamic Profiles (not in the plist), iTerm2's own Light/Dark theme
override (the system appearance is read instead), and preferences loaded from a
custom folder.
"""

import os
import plistlib
import subprocess
from dataclasses import dataclass
from html import escape
from pathlib import Path

from rich.terminal_theme import TerminalTheme

PLIST = Path.home() / "Library/Preferences/com.googlecode.iterm2.plist"


def macos_appearance() -> str:
    """`dark` or `light`: the key only exists while dark mode is on."""
    found = subprocess.run(
        ["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True
    )
    return "dark" if "Dark" in found.stdout else "light"


def _profile(prefs: dict) -> dict:
    """$ITERM_PROFILE (set in every iTerm2 session) by name, else the default profile."""
    profiles = prefs.get("New Bookmarks", [])
    name = os.environ.get("ITERM_PROFILE")
    default = prefs.get("Default Bookmark Guid")
    key, wanted = ("Name", name) if name else ("Guid", default)
    for profile in profiles:
        if profile.get(key) == wanted:
            return profile
    raise LookupError(f"no iTerm2 profile {name or 'default'!r} in {PLIST}")


@dataclass
class Look:
    """How a screenshot is drawn: the terminal's colors, and its progress bar's."""

    theme: TerminalTheme | None = None  # None: Rich's dark default
    palette: str = "dark"  # pcode's `theme` preference to match
    bar_scheme: str = "default"
    bar_height: float = 2.0  # iTerm2's default
    dark: bool = True  # the window's appearance, which picks the default bar colors


def profile_look() -> Look:
    """The current profile's colors and progress bar style."""
    if not PLIST.exists():
        raise LookupError(f"{PLIST} not found: --iterm needs iTerm2")
    with PLIST.open("rb") as file:
        profile = _profile(plistlib.load(file))
    mode = macos_appearance()
    separate = profile.get("Use Separate Colors for Light and Dark Mode")
    suffix = f" ({mode.title()})" if separate else ""

    def color(key: str) -> tuple[int, int, int]:
        value = profile.get(key + suffix) or profile.get(key)
        if value is None:
            raise LookupError(f"iTerm2 profile {profile.get('Name')!r} has no {key!r}")
        # Components are 0..1 floats; the color space (sRGB, P3, calibrated)
        # is ignored, which is close enough for a screenshot.
        return tuple(round(value[f"{c} Component"] * 255) for c in ("Red", "Green", "Blue"))

    background = color("Background Color")
    theme = TerminalTheme(
        background,
        color("Foreground Color"),
        [color(f"Ansi {n} Color") for n in range(8)],
        [color(f"Ansi {n} Color") for n in range(8, 16)],
    )
    # pcode's palette follows the background, as `theme auto` would.
    luminance = 0.2126 * background[0] + 0.7152 * background[1] + 0.0722 * background[2]
    return Look(
        theme=theme,
        palette="light" if luminance > 128 else "dark",
        bar_scheme=profile.get("Progress Bar Color Scheme", "default"),
        bar_height=float(profile.get("Progress Bar Height", 2.0)),
        dark=mode == "dark",
    )


# The progress bar iTerm2 draws along the top of a session for OSC 9;4, after
# sources/TerminalView/iTermProgressBarView.swift: a horizontal gradient, the
# fill's width for a percentage, a sliding segment while indeterminate.
# A color is (r, g, b, opacity), 0..1.
SCHEMES = {
    "rainbow": [(1, 0, 0, 1), (1, 0.5, 0, 1), (1, 1, 0, 1), (0, 1, 0, 1),
                (0, 0.5, 1, 1), (0.5, 0, 1, 1), (1, 0, 0.5, 1)],
    "red": [(0.8, 0, 0, 1), (1, 0.2, 0.2, 1)],
    "green": [(0, 0.8, 0, 1), (0.2, 1, 0.2, 1)],
    "blue": [(0, 0, 0.8, 1), (0.2, 0.2, 1, 1)],
    "yellow": [(0.8, 0.8, 0, 1), (1, 1, 0.2, 1)],
    "purple": [(0.6, 0, 0.8, 1), (0.8, 0.2, 1, 1)],
    "cyan": [(0, 0.8, 0.8, 1), (0.2, 1, 1, 1)],
    "orange": [(1, 0.5, 0, 1), (1, 0.7, 0.2, 1)],
}  # fmt: skip
ERROR = [(1, 0, 0, 1), (1, 0.2, 0.2, 1)]
WARNING = {True: [(1, 0.5, 0, 1), (1, 0.7, 0.2, 1)], False: [(0.8, 0.6, 0, 1), (1, 0.8, 0.2, 1)]}


def _scheme(look: Look, indeterminate: bool) -> list:
    if look.bar_scheme in SCHEMES:
        return SCHEMES[look.bar_scheme]
    if indeterminate:
        if look.dark:
            return [(0, 1, 0, a) for a in (0, 0.5, 1, 1, 0.5, 0)]
        return [(0, 0, 1, a) for a in (0, 0.3, 1, 0.3, 0)]
    return [(0, 1, 0, 1), (0.2, 1, 0.2, 1)] if look.dark else [(0, 0, 1, 1), (0.2, 0.2, 1, 1)]


def progress_bar_svg(
    state: int, value: int | None, look: Look, x: float, bottom: float, width: float
):
    """SVG for an OSC 9;4 bar, `width` wide from x, ending at `bottom`; empty for none.

    States: 1 a percentage, 2 an error, 3 indeterminate, 4 a warning (paused).
    """
    # Capped so a tall profile setting stays clear of the title.
    height = min(look.bar_height, 8)
    return _progress_fill(state, value, look, x, bottom - height, width, height)


def _progress_fill(
    state: int,
    value: int | None,
    look: Look,
    x: float,
    y: float,
    width: float,
    height: float,
    gradient: str = "iterm-progress",
    clip: str = "",
) -> str:
    """The bar's gradient fill over the box at (x, y), as far as the report says."""
    indeterminate = state == 3
    percent = 100 if indeterminate else value or 0
    if state not in (1, 2, 3, 4) or not percent:
        return ""
    colors = {2: ERROR, 4: WARNING[look.dark]}.get(state) or _scheme(look, indeterminate)
    if indeterminate:
        # Stands in for the animation: iTerm2 slides two bar-wide gradients.
        x, width = x + width * 0.3, width * 0.4
    else:
        width = width * percent / 100
    stops = "".join(
        f'<stop offset="{i / max(1, len(colors) - 1):.3f}" '
        f'stop-color="rgb({r * 255:.0f},{g * 255:.0f},{b * 255:.0f})" stop-opacity="{a}"/>'
        for i, (r, g, b, a) in enumerate(colors)
    )
    clipped = f' clip-path="url(#{clip})"' if clip else ""
    return (
        f'<defs><linearGradient id="{gradient}">{stops}</linearGradient></defs>'
        f'<rect fill="url(#{gradient})" x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" '
        f'height="{height:.1f}"{clipped}/>'
    )


# iTerm2's tab bar with more than one tab, after the Tahoe style of 3.7
# (ThirdParty/PSMTabBarControl/source/PSMTahoeTabStyle.swift): a 36pt bar
# holding a 28pt rounded container, each tab a pill in it, the selected one
# filled. With the bar showing, a session's OSC 9;4 report is drawn as a 2pt
# ring around its tab's pill (filled left to right as far as the percentage
# goes) instead of the bar along the top of the session.
TAB_FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif"
# (bar, container, selected pill, selected text, other text), sRGB.
TAB_COLORS = {
    True: ((45, 48, 50), (43, 46, 48), (98, 100, 102), (239, 239, 239), (126, 128, 129)),
    False: ((225, 225, 225), (230, 230, 230), (247, 247, 247), (70, 70, 70), (70, 70, 70)),
}


def _rgb(color: tuple[int, int, int]) -> str:
    return "rgb({},{},{})".format(*color)


def _pill(x: float, y: float, width: float, height: float) -> str:
    """A stadium's outline as path data, for an even-odd ring."""
    r = height / 2
    return (
        f"M{x + r:.1f},{y:.1f} H{x + width - r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + width - r:.1f},{y + height:.1f} "
        f"H{x + r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{y:.1f} Z"
    )


def _fit(title: str, width: float, size: float) -> str:
    """`title` cut with an ellipsis to roughly fit `width` in a proportional font."""
    room = int(width / (size * 0.52))
    return title if len(title) <= room else title[: max(1, room - 1)].rstrip() + "…"


def tab_bar_svg(tabs: list[tuple], look: Look, x: float, y: float, width: float, scale: float):
    """SVG for iTerm2's tab bar across `width` from (x, y), and its height.

    `tabs` holds (title, progress, selected) per tab, progress as
    `last_progress` returns it. `scale` is pixels per point, so the bar keeps its
    size next to the terminal's text.
    """
    bar, container, pill, selected_text, text = TAB_COLORS[look.dark]
    k = scale
    height = 36 * k
    add = 32 * k  # the new-tab button, right of the container
    box_x, box_y, box_h = x + 8 * k, y + 4 * k, 28 * k
    box_w = width - 16 * k - add
    parts = [
        f'<rect fill="{_rgb(bar)}" x="{x}" y="{y}" width="{width}" height="{height:.1f}"/>',
        f'<rect fill="{_rgb(container)}" x="{box_x:.1f}" y="{box_y:.1f}" width="{box_w:.1f}" '
        f'height="{box_h:.1f}" rx="{box_h / 2:.1f}"/>',
        f'<text fill="{_rgb(text)}" font-family="{TAB_FONT}" font-size="{20 * k:.1f}" '
        f'font-weight="300" text-anchor="middle" dominant-baseline="central" '
        f'x="{box_x + box_w + add / 2:.1f}" y="{box_y + box_h / 2:.1f}">+</text>',
    ]
    cell_w = box_w / max(1, len(tabs))
    font = 11 * k
    ring = 2 * k
    for index, (title, progress, selected) in enumerate(tabs):
        # backgroundRect: 2pt in from the cell's top, 1pt from its bottom.
        px, py = box_x + index * cell_w + 2 * k, box_y + 2 * k
        pw, ph = cell_w - 4 * k, box_h - 3 * k
        if selected:
            parts.append(
                f'<rect fill="{_rgb(pill)}" x="{px:.1f}" y="{py:.1f}" width="{pw:.1f}" '
                f'height="{ph:.1f}" rx="{ph / 2:.1f}"/>'
            )
        clip = f"iterm-tab-ring-{index}"
        ox, oy, ow, oh = px - ring, py - ring, pw + 2 * ring, ph + 2 * ring
        gradient = f"iterm-tab-progress-{index}"
        fill = progress and _progress_fill(*progress, look, ox, oy, ow, oh, gradient, clip)
        if fill:
            parts.append(
                f'<defs><clipPath id="{clip}"><path clip-rule="evenodd" '
                f'd="{_pill(ox, oy, ow, oh)} {_pill(px, py, pw, ph)}"/></clipPath></defs>{fill}'
            )
        color = selected_text if selected else text
        parts.append(
            f'<text fill="{_rgb(color)}" font-family="{TAB_FONT}" font-size="{font:.1f}" '
            f'text-anchor="middle" dominant-baseline="central" '
            f'x="{px + pw / 2:.1f}" y="{py + ph / 2:.1f}">'
            f"{escape(_fit(title, pw - 24 * k, font))}</text>"
        )
    return "".join(parts), height
