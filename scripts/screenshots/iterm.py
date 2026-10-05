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
    indeterminate = state == 3
    percent = 100 if indeterminate else value or 0
    if state not in (1, 2, 3, 4) or not percent:
        return ""
    # Capped so a tall profile setting stays clear of the title.
    height = min(look.bar_height, 8)
    y = bottom - height
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
    return (
        f'<defs><linearGradient id="iterm-progress">{stops}</linearGradient></defs>'
        f'<rect fill="url(#iterm-progress)" x="{x}" y="{y}" width="{width:.1f}" '
        f'height="{height}"/>'
    )
