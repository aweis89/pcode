"""All modal surfaces override toolkit colors without painting a background."""

import pytest
from prompt_toolkit.styles import merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from pcode.popup_ui import popup_style
from pcode.ui import PALETTES


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
        "dialog dialog.body scrollbar.background",
        "scrollbar.background",
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


@pytest.mark.parametrize("kind", ["sessions", "tree", "models", "tools"])
def test_all_popups_share_style_scope(kind):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode.conversation_tree import ConversationTree
    from pcode.inspection import ToolArchive
    from pcode.inspector_ui import ToolInspector
    from pcode.model_ui import ModelPicker
    from pcode.session_ui import session_dialog
    from pcode.tree_ui import tree_dialog

    with create_pipe_input() as pipe:
        options = dict(input=pipe, output=DummyOutput(), style=PALETTES["dark"].prompt_style())
        if kind == "sessions":
            app = session_dialog([("one", "First session")], **options)
        elif kind == "tree":
            app = tree_dialog(ConversationTree(), **options)
        elif kind == "models":
            app = ModelPicker(["test:model"], {"test"}, **options).app
        else:
            app = ToolInspector(ToolArchive(), **options).app
        assert app.layout.container.style == "class:popup"
        attrs = app.style.get_attrs_for_style_str("class:popup class:dialog.body")
        assert attrs.bgcolor == attrs.color == "default"
