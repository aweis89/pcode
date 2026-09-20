"""Prompt chrome follows the syntax style, but only where it stays legible.

The completion menu paints the style's own background behind its text. The
prompt, plan rows and frame do not: they sit on the terminal's background, so
a style saved for the other appearance has to be dropped rather than used.
"""

import pytest
from prompt_toolkit.styles import DynamicStyle, merge_styles
from prompt_toolkit.styles.defaults import default_ui_style
from rich.console import Console

from pcode.preferences import SYNTAX_THEMES
from pcode.syntax_colors import _ACCENT_GAP, _brightness, derive_colors
from pcode.ui import PALETTES, Transcript

CHROME = ("prompt", "plan.heading", "frame.border", "reference", "auto-suggestion")


def transcript(theme="dark", syntax=None, **kwargs):
    console = Transcript(Console(), theme, preferences={}, detected_theme=theme, **kwargs)
    if syntax:
        console.syntax_themes[theme] = syntax
    return console


def color_of(console, class_name):
    style = merge_styles([default_ui_style(), console.prompt_style()])
    return style.get_attrs_for_style_str(f"class:{class_name}").color


@pytest.mark.parametrize(
    "theme, syntax, accent, heading",
    [
        ("dark", "dracula", "50fa7b", "ff79c6"),
        ("dark", "monokai", "a6e22e", "66d9ef"),
        ("light", "solarized-light", "268bd2", "859900"),
    ],
)
def test_chrome_follows_a_matching_syntax_style(theme, syntax, accent, heading):
    console = transcript(theme, syntax)
    assert color_of(console, "prompt") == accent
    assert color_of(console, "plan.heading") == heading


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_chrome_drops_colors_lost_against_the_terminal(theme):
    """`bw` is near-black on white: unusable as chrome, fine inside the popup."""
    console = transcript(theme, "bw")
    palette = PALETTES[theme]
    assert color_of(console, "prompt") == palette.accent.lstrip("#")
    assert color_of(console, "auto-suggestion") == palette.muted.lstrip("#")
    menu = merge_styles([default_ui_style(), console.prompt_style()])
    assert menu.get_attrs_for_style_str("class:completion-menu").bgcolor == "ffffff"


def test_chrome_keeps_the_palette_for_terminal_colors():
    console = transcript("dark", "dracula", color_style="terminal")
    assert color_of(console, "prompt") == PALETTES["dark"].accent.lstrip("#")


def test_chrome_updates_when_the_syntax_style_changes():
    console = transcript()
    style = merge_styles([default_ui_style(), DynamicStyle(console.prompt_style)])
    for name, accent in (("monokai", "a6e22e"), ("dracula", "50fa7b"), ("monokai", "a6e22e")):
        console.syntax_themes["dark"] = name
        assert style.get_attrs_for_style_str("class:prompt").color == accent


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("style_name", SYNTAX_THEMES)
def test_every_syntax_style_yields_legible_chrome(style_name, theme):
    palette = PALETTES[theme]
    colors = derive_colors(style_name, palette.__dict__, palette.surface)
    backdrop = _brightness(palette.surface)
    for field in ("accent", "muted", "task_heading"):
        assert abs(_brightness(colors[field]) - backdrop) >= _ACCENT_GAP, field


@pytest.mark.parametrize("class_name", CHROME)
def test_chrome_classes_are_colored(class_name):
    """A renamed style key would silently stop following the theme."""
    assert color_of(transcript("dark", "dracula"), class_name) not in ("", "default")
