"""Completion colors must override toolkit defaults, including reverse video."""

import pytest
from prompt_toolkit.styles import DynamicStyle, merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from pcode.ui import PALETTES


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
