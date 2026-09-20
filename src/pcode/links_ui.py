"""Temporary link chooser for URLs seen in the conversation."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, RadioList

from pcode.links import Link
from pcode.popup_ui import popup_container, popup_style

_ROW_WIDTH = 100


def link_rows(links: list[Link]) -> list[tuple[str, str]]:
    rows = []
    for link in links:
        prefix = f"{link.source}: " if link.source else ""
        text = f"{link.label} — {link.url}" if link.label else link.url
        if len(text) > _ROW_WIDTH:
            text = text[: _ROW_WIDTH - 1] + "…"
        rows.append((link.url, prefix + text))
    return rows


def links_dialog(links: list[Link], *, input=None, output=None, style=None):
    # Newest first: the link just mentioned is the one most likely wanted.
    choices = RadioList(link_rows(list(reversed(links))), select_on_focus=True)
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
        title="Links",
        body=HSplit(
            [
                Label("↑/↓ select · Enter open in browser · Esc cancel", dont_extend_height=True),
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
