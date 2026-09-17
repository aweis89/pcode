"""The editor border badge follows vi state and keeps its explicit colors."""

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import merge_styles
from prompt_toolkit.styles.defaults import default_ui_style
from prompt_toolkit.widgets import TextArea

from pcode.ui import PALETTES, editor_mode_label


@pytest.mark.parametrize(
    "mode,label",
    [
        (InputMode.INSERT, " INSERT "),
        (InputMode.INSERT_MULTIPLE, " INSERT "),
        (InputMode.NAVIGATION, " NORMAL "),
        (InputMode.REPLACE, " REPLACE "),
        (InputMode.REPLACE_SINGLE, " REPLACE "),
    ],
)
def test_editor_mode_label(mode, label):
    app = Application(input=DummyInput(), output=DummyOutput(), editing_mode=EditingMode.VI)
    app.vi_state.input_mode = mode
    assert editor_mode_label(app) == label
    app.editing_mode = EditingMode.EMACS
    assert editor_mode_label(app) == ""


def test_visual_mode_label():
    app = Application(
        layout=Layout(TextArea()),
        input=DummyInput(),
        output=DummyOutput(),
        editing_mode=EditingMode.VI,
    )
    app.current_buffer.start_selection()
    assert editor_mode_label(app) == " VISUAL "


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_editor_mode_colors(theme):
    style = merge_styles([default_ui_style(), PALETTES[theme].prompt_style()])
    attrs = style.get_attrs_for_style_str("class:frame class:label class:editor.mode")
    assert attrs.color == "ffffff"
    assert attrs.bgcolor == "b8b8b8"
    assert not attrs.reverse
    assert not attrs.dim
