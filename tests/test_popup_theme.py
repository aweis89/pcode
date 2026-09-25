"""All modal surfaces override toolkit colors without painting a background."""

from pathlib import Path

import pytest
from prompt_toolkit.styles import DynamicStyle, merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from pcode.popup_ui import popup_style
from pcode.ui import PALETTES, Transcript


@pytest.mark.parametrize("theme", [None, "dark", "light"])
@pytest.mark.parametrize(
    "classes",
    [
        "",
        "dialog",
        "dialog dialog.body",
        "dialog dialog.body text-area",
        "dialog frame.border",
        "dialog frame.label",
        "dialog shadow",
        "dialog dialog.body shadow",
        "text-area",
        "frame.border",
        "frame.label",
    ],
)
def test_popup_surfaces_use_terminal_defaults(theme, classes):
    base = PALETTES[theme].prompt_style() if theme else None
    style = merge_styles([default_ui_style(), popup_style(base)])
    attrs = style.get_attrs_for_style_str(
        "class:popup " + " ".join(f"class:{c}" for c in classes.split())
    )
    assert attrs.bgcolor == "default"
    assert attrs.color == "default"
    assert not attrs.reverse


@pytest.mark.parametrize(
    "classes",
    [
        "selected",
        "dialog dialog.body selected",
        "text-area cursor-line",
        "dialog dialog.body scrollbar.button",
    ],
)
def test_popup_selection_is_highlighted(classes):
    style = merge_styles([default_ui_style(), popup_style()])
    attrs = style.get_attrs_for_style_str(
        "class:popup " + " ".join(f"class:{c}" for c in classes.split())
    )
    assert attrs.reverse
    assert attrs.bgcolor == attrs.color == "default"


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize(
    "classes", ["selected", "dialog dialog.body selected", "text-area cursor-line"]
)
def test_popup_selection_uses_theme_colors(theme, classes):
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), popup_style(palette.prompt_style())])
    attrs = style.get_attrs_for_style_str(
        "class:popup " + " ".join(f"class:{c}" for c in classes.split())
    )
    assert attrs.bgcolor == palette.selected.lstrip("#")
    assert attrs.color == palette.accent.lstrip("#")
    assert not attrs.reverse
    assert not attrs.underline


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("prefix", ["", "class:dialog class:dialog.body "])
def test_popup_scrollbars_use_theme_colors(theme, prefix):
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), popup_style(palette.prompt_style())])
    for part, background, foreground in (
        ("background", palette.surface, "default"),
        ("button", palette.accent, "default"),
        ("arrow", "default", palette.accent),
    ):
        attrs = style.get_attrs_for_style_str(f"class:popup {prefix}class:scrollbar.{part}")
        assert attrs.bgcolor == background.lstrip("#")
        assert attrs.color == foreground.lstrip("#")
        assert not attrs.reverse


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_popup_colors_follow_syntax_and_theme_changes(theme):
    from rich.console import Console

    transcript = Transcript(Console(), theme, preferences={}, detected_theme=theme)
    style = merge_styles([default_ui_style(), popup_style(DynamicStyle(transcript.prompt_style))])
    for name in ("monokai", "solarized-light", "terminal", "dracula"):
        transcript.syntax_themes[theme] = name
        palette = transcript.menu_palette
        for part in ("selected", "cursor-line"):
            attrs = style.get_attrs_for_style_str(f"class:popup class:{part}")
            assert attrs.color == palette.accent.lstrip("#")
            assert attrs.bgcolor == (
                "default" if name == "terminal" else palette.selected.lstrip("#")
            )
            assert attrs.reverse == (name == "terminal")
        thumb = style.get_attrs_for_style_str("class:popup class:scrollbar.button")
        assert thumb.bgcolor == palette.accent.lstrip("#")
        assert not thumb.reverse
        surface = style.get_attrs_for_style_str("class:popup class:text-area")
        assert surface.bgcolor == surface.color == "default"


def test_popup_colors_follow_appearance_changes():
    palette = PALETTES["dark"]
    style = popup_style(DynamicStyle(lambda: palette.prompt_style()))
    for theme in ("dark", "light", "dark"):
        palette = PALETTES[theme]
        attrs = style.get_attrs_for_style_str("class:popup class:cursor-line")
        assert attrs.bgcolor == palette.selected.lstrip("#")
        assert attrs.color == palette.accent.lstrip("#")


@pytest.mark.parametrize(
    "kind", ["sessions", "tree", "models", "tools", "edits", "links", "asides", "status"]
)
def test_all_popups_share_style_scope(kind):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode.aside import Asides
    from pcode.aside_ui import AsideBrowser
    from pcode.conversation_tree import ConversationTree
    from pcode.edit_ui import EditBrowser
    from pcode.inspection import ToolArchive
    from pcode.inspector_ui import ToolInspector
    from pcode.links_ui import links_dialog
    from pcode.model_ui import ModelPicker
    from pcode.session_ui import SessionBrowser, session_info_dialog
    from pcode.tree_ui import tree_dialog

    with create_pipe_input() as pipe:
        options = dict(input=pipe, output=DummyOutput(), style=PALETTES["dark"].prompt_style())
        if kind == "sessions":
            app = SessionBrowser([], root=Path("."), workspace=Path("."), **options).app
        elif kind == "tree":
            app = tree_dialog(ConversationTree(), **options)
        elif kind == "models":
            app = ModelPicker(["test:model"], {"test"}, **options).app
        elif kind == "tools":
            app = ToolInspector(ToolArchive(), **options).app
        elif kind == "edits":
            app = EditBrowser([], **options).app
        elif kind == "links":
            app = links_dialog([], **options)
        elif kind == "asides":
            app = AsideBrowser(Asides(), **options).app
        else:
            app = session_info_dialog([], **options)
        for part in ("selected", "cursor-line"):
            attrs = app.style.get_attrs_for_style_str(f"class:popup class:{part}")
            assert attrs.bgcolor == PALETTES["dark"].selected.lstrip("#")
            assert attrs.color == PALETTES["dark"].accent.lstrip("#")
            assert not attrs.reverse
        thumb = app.style.get_attrs_for_style_str("class:popup class:scrollbar.button")
        assert thumb.bgcolor == PALETTES["dark"].accent.lstrip("#")
        assert app.layout.container.style == "class:popup"
        attrs = app.style.get_attrs_for_style_str("class:popup class:dialog.body")
        assert attrs.bgcolor == attrs.color == "default"


def test_rich_pane_keyboard_scrolling_survives_a_render():
    """The pane's reported cursor follows the scroll, or every render snaps it to the top."""
    import asyncio
    from io import StringIO

    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.output.vt100 import Vt100_Output
    from rich.text import Text

    from pcode.popup_ui import RichPane

    async def run():
        with create_pipe_input() as pipe:
            output = Vt100_Output(StringIO(), lambda: Size(rows=10, columns=80), enable_cpr=False)
            pane = RichPane()
            pane.set([Text("\n".join(f"line {i}" for i in range(100)))])
            keys = KeyBindings()
            pane.bind_scrolling(keys)
            keys.add("escape", eager=True)(lambda event: event.app.exit())
            app = Application(
                layout=Layout(pane, focused_element=pane.window),
                key_bindings=keys,
                full_screen=True,
                input=pipe,
                output=output,
            )
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[B")  # Down
            await asyncio.sleep(0.05)
            assert pane.window.vertical_scroll == 1
            pipe.send_text("\x1b[6~")  # PageDown
            await asyncio.sleep(0.05)
            assert pane.window.vertical_scroll == 10
            for _ in range(15):
                pipe.send_text("\x1b[6~")
                await asyncio.sleep(0.03)
            assert pane.window.vertical_scroll == 90  # Clamped at the last page.
            # An anchor lands the renderable's first line on the top row.
            tail = Text("\n".join(f"tail {i}" for i in range(30)))
            pane.set([Text("one\ntwo"), Text("x " * 100), tail], anchor=2)
            app.invalidate()
            await asyncio.sleep(0.05)
            assert pane.line_offset(2, 80) == 5  # Two lines, then three wrapped at 80.
            assert pane.window.vertical_scroll == 5
            pane.scroll_to(1)
            app.invalidate()
            await asyncio.sleep(0.05)
            assert pane.window.vertical_scroll == 2
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())
