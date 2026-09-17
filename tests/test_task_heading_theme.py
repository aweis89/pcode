"""Task headings have a dedicated, theme-aware accent, not a global frame tint."""

import pytest
from prompt_toolkit.styles import DynamicStyle, merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from pcode.ui import PALETTES


@pytest.mark.parametrize("theme,color", [("light", "7c3aed"), ("dark", "c4b5fd")])
def test_task_heading_color_is_scoped(theme, color):
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    attrs = style.get_attrs_for_style_str("class:frame.label class:plan.heading")
    assert attrs.color == color
    assert attrs.bold
    assert not attrs.reverse
    assert attrs.bgcolor == ""
    assert style.get_attrs_for_style_str("class:frame.border").color == palette.muted[1:]
    assert style.get_attrs_for_style_str("class:frame.label").color != color
    assert style.get_attrs_for_style_str("class:plan.active").color == palette.accent[1:]


def test_task_heading_tracks_theme_changes():
    palette = PALETTES["dark"]
    style = merge_styles([default_ui_style(), DynamicStyle(lambda: palette.prompt_style())])
    for theme in ("dark", "light", "dark"):
        palette = PALETTES[theme]
        attrs = style.get_attrs_for_style_str("class:frame.label class:plan.heading")
        assert attrs.color == palette.task_heading[1:]
        assert attrs.bold
