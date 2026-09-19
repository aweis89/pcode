"""Temporary session popups; the editor is suspended while one owns the terminal."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, RadioList, TextArea

from pcode.popup_ui import popup_container, popup_style


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
        layout=Layout(popup_container(dialog), focused_element=choices),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
        style=popup_style(style),
    )


def session_info_dialog(rows, *, input=None, output=None, style=None):
    """Read-only view of the live session; every key that closes it exits the same way."""
    width = max((len(label) for label, _ in rows), default=0)
    body = TextArea(
        text="\n".join(f"{label.ljust(width)}  {value}" for label, value in rows),
        read_only=True,
        scrollbar=True,
        focus_on_click=True,
    )
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    @bindings.add("enter", eager=True)
    @bindings.add("q", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def close(event):
        event.app.exit(result=None)

    dialog = Dialog(
        title="Session",
        body=HSplit(
            [
                Label("↑/↓ scroll · Esc close · /resume switches session", dont_extend_height=True),
                body,
            ],
            padding=1,
        ),
        with_background=True,
    )
    return Application(
        layout=Layout(popup_container(dialog), focused_element=body),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
        style=popup_style(style),
    )
