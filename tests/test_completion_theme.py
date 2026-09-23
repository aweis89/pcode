"""Completion colors must override toolkit defaults, including reverse video."""

import pytest
from prompt_toolkit.styles import DynamicStyle, merge_styles
from prompt_toolkit.styles.defaults import default_ui_style
from rich.console import Console

from pcode.preferences import SYNTAX_THEMES
from pcode.syntax_colors import _brightness, derive_colors
from pcode.ui import PALETTES, Transcript


def transcript(theme="dark", **kwargs):
    return Transcript(Console(), theme, preferences={}, detected_theme=theme, **kwargs)


def menu_attrs(source, suffix=""):
    style = merge_styles([default_ui_style(), source.prompt_style()])
    return style.get_attrs_for_style_str(f"class:completion-menu{suffix}")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_completion_menu_colors(theme):
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    for suffix, background, foreground in (
        ("", palette.surface, palette.foreground),
        (".completion", palette.surface, palette.foreground),
        (".completion.current", palette.selected, palette.accent),
        (".meta.completion", palette.surface, palette.muted),
        (".meta.completion.current", palette.selected, palette.foreground),
    ):
        attrs = style.get_attrs_for_style_str(f"class:completion-menu{suffix}")
        assert attrs.bgcolor == background.lstrip("#")
        assert attrs.color == foreground.lstrip("#")
        assert not attrs.reverse

    for part, background in (("background", palette.surface), ("button", palette.selected)):
        attrs = style.get_attrs_for_style_str(f"class:completion-menu class:scrollbar.{part}")
        assert attrs.bgcolor == background.lstrip("#")


def test_completion_menu_updates_when_theme_changes():
    palette = PALETTES["dark"]
    style = merge_styles([default_ui_style(), DynamicStyle(lambda: palette.prompt_style())])
    for theme in ("dark", "light", "dark"):
        palette = PALETTES[theme]
        attrs = style.get_attrs_for_style_str("class:completion-menu.completion.current")
        assert attrs.bgcolor == palette.selected.lstrip("#")
        assert attrs.color == palette.accent.lstrip("#")
        assert not attrs.reverse


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_menu_follows_the_selected_syntax_style(theme):
    console = transcript(theme)
    console.syntax_themes[theme] = "dracula"
    assert menu_attrs(console).bgcolor == "282a36"  # Dracula's own background.
    console.syntax_themes[theme] = "solarized-light"
    assert menu_attrs(console).bgcolor == "fdf6e3"


def test_menu_updates_when_the_syntax_style_changes():
    console = transcript()
    style = merge_styles([default_ui_style(), DynamicStyle(console.prompt_style)])
    for name, background in (("monokai", "272822"), ("nord", "2e3440"), ("monokai", "272822")):
        console.syntax_themes["dark"] = name
        assert style.get_attrs_for_style_str("class:completion-menu").bgcolor == background


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_menu_uses_the_terminal_colors_for_terminal_syntax(theme):
    console = transcript(theme)
    assert console.syntax_themes[theme] == "terminal"
    for suffix in ("", ".completion", ".meta.completion"):
        attrs = menu_attrs(console, suffix)
        # No RGB, including the toolkit's own defaults, may show through.
        assert (attrs.bgcolor, attrs.color, attrs.reverse) == ("default", "default", False)
    assert menu_attrs(console, ".meta.completion").dim
    current = menu_attrs(console, ".completion.current")
    assert (current.color, current.bgcolor, current.reverse) == ("ansicyan", "default", True)
    assert menu_attrs(console, ".meta.completion.current").reverse
    scrollbar = menu_attrs(console, " class:scrollbar.button")
    assert scrollbar.reverse


def test_menu_keeps_the_palette_for_an_uninstalled_style():
    console = transcript()
    console.syntax_themes["dark"] = "style-from-an-uninstalled-plugin"
    assert menu_attrs(console).bgcolor == PALETTES["dark"].surface.lstrip("#")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("style_name", SYNTAX_THEMES)
def test_every_syntax_style_yields_a_readable_menu(style_name, theme):
    """No style may hide the menu text, whichever palette it is paired with.

    A style saved for one palette can be shown under the other: `/syntax` only
    writes the slot for the palette in use, and nothing stops a light style
    being chosen while the dark palette is active.
    """
    colors = derive_colors(style_name, PALETTES[theme].__dict__)
    surface = _brightness(colors["surface"])
    selected = _brightness(colors["selected"])
    assert abs(_brightness(colors["foreground"]) - surface) >= 0.3
    assert abs(selected - surface) >= 0.04
    assert abs(_brightness(colors["foreground"]) - selected) >= 0.3
    for field in ("accent", "muted", "task_heading"):
        assert abs(_brightness(colors[field]) - surface) >= 0.18, field
    assert abs(_brightness(colors["accent"]) - selected) >= 0.18
