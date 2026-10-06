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
    assert style.get_attrs_for_style_str("class:plan.in_progress").color == palette.accent[1:]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_finished_task_heading_turns_the_success_colour(theme):
    from pcode.ui import Activity

    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    done = style.get_attrs_for_style_str("class:frame.label class:plan.heading.done")
    assert done.color == palette.success[1:] and done.bold
    assert Activity(plan=[{"content": "a", "status": "completed"}]).plan_done
    assert not Activity(plan=[{"content": "a", "status": "pending"}]).plan_done
    assert not Activity().plan_done


def test_task_heading_tracks_theme_changes():
    palette = PALETTES["dark"]
    style = merge_styles([default_ui_style(), DynamicStyle(lambda: palette.prompt_style())])
    for theme in ("dark", "light", "dark"):
        palette = PALETTES[theme]
        attrs = style.get_attrs_for_style_str("class:frame.label class:plan.heading")
        assert attrs.color == palette.task_heading[1:]
        assert attrs.bold


@pytest.mark.parametrize("theme", ["dark", "light", "terminal"])
def test_session_name_and_status_detail_wear_the_heading_hue_undimmed(theme):
    from pcode.ui import TERMINAL_PALETTE

    palette = TERMINAL_PALETTE if theme == "terminal" else PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    hue = palette.task_heading.lstrip("#")
    # Under the frame's own classes, so an inherited dim (the terminal
    # palette's muted border is `fg:default dim`) would show here.
    for classes in (
        "class:frame.border class:frame.label class:session.name",
        "class:frame.border class:activity.detail",
    ):
        attrs = style.get_attrs_for_style_str(classes)
        assert attrs.color == hue and not attrs.dim and not attrs.bold, classes


def test_a_sub_agent_detail_on_the_status_row_keeps_its_own_hue():
    from pcode.ui import StatusLine

    palette = PALETTES["dark"]
    style = palette.prompt_style()
    fragments = StatusLine("Waiting for 1 agent", "✦ Worker · Thinking", hue=1).fragments("⠋", 80)
    detail = next(name for name, text in fragments if "Worker" in text)
    assert style.get_attrs_for_style_str(detail).color == palette.agents[1].lstrip("#")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_derived_task_heading_never_matches_the_accent(theme):
    from pygments.styles import get_all_styles

    from pcode.syntax_colors import derive_colors

    palette = PALETTES[theme]
    for name in get_all_styles():
        colors = derive_colors(name, palette.__dict__, palette.surface)
        assert colors["task_heading"] != colors["accent"], name
