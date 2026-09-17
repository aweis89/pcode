"""Temporary conversation tree chooser; never executes a model request."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, RadioList

from pcode.popup_ui import popup_container, popup_style


def tree_dialog(tree, *, input=None, output=None, style=None):
    choices = RadioList(
        tree.rows(),
        default=(tree.active, False),
        select_on_focus=True,
    )
    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def accept(event):
        event.app.exit(result=choices.current_value)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    dialog = Dialog(
        title="Conversation tree",
        body=HSplit(
            [
                Label("↑/↓ select · Enter navigate · Esc cancel", dont_extend_height=True),
                Label(
                    "User: edit & fork · Assistant: continue · Start: empty context",
                    dont_extend_height=True,
                ),
                choices,
                Label(
                    "Switching context does not undo file changes or tool effects.",
                    dont_extend_height=True,
                ),
            ],
            padding=1,
        ),
        with_background=True,
    )
    return Application(
        layout=Layout(popup_container(dialog), focused_element=choices),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
        style=popup_style(style),
    )
