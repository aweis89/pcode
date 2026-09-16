"""Temporary session chooser; the editor is suspended while it owns the terminal."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, RadioList


def session_dialog(values, *, input=None, output=None, style=None):
    choices = RadioList(values, select_on_focus=True)
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
        title="Resume session",
        body=HSplit(
            [
                Label("↑/↓ select · Enter resume · Esc cancel", dont_extend_height=True),
                choices,
            ],
            padding=1,
        ),
        with_background=True,
    )
    return Application(
        layout=Layout(dialog, focused_element=choices),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
        style=style,
    )
