"""Temporary link chooser for URLs seen in the conversation."""

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, TextArea

from pcode.links import Link
from pcode.popup_ui import (
    bind_list_paging,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)

_ROW_WIDTH = 100


def link_rows(links: list[Link]) -> list[tuple[str, str]]:
    rows = []
    for link in links:
        prefix = f"{link.source}: " if link.source else ""
        text = f"{link.label} — {link.url}" if link.label else link.url
        text = text.replace("\n", " ↵ ").replace("\r", " ")
        if len(text) > _ROW_WIDTH:
            text = text[: _ROW_WIDTH - 1] + "…"
        rows.append((link.url, prefix + text))
    return rows


def links_dialog(
    links: list[Link],
    *,
    message_links: list[Link] | None = None,
    input=None,
    output=None,
    style=None,
):
    # Collect message-only links separately: a URL first seen in a tool can also
    # appear in a reply, and must survive hiding tools despite URL deduplication.
    if message_links is None:
        message_links = [link for link in links if link.source in ("", "user", "assistant")]
    show_tools = True
    visible: list[Link] = []
    query = TextArea(height=1, prompt="/ Search: ", multiline=False)
    choices = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
    choices.window.cursorline = Always()
    bindings = KeyBindings()

    def selected_url():
        row = choices.document.cursor_position_row
        return visible[row].url if row < len(visible) else None

    def refresh():
        previous = selected_url()
        term = query.text.casefold()
        visible[:] = [
            link
            for link in reversed(links if show_tools else message_links)
            if term in f"{link.url} {link.label} {link.source}".casefold()
        ]
        selected = next((i for i, link in enumerate(visible) if link.url == previous), 0)
        lines = [text for _, text in link_rows(visible)]
        position = sum(len(line) + 1 for line in lines[:selected])
        choices.buffer.set_document(
            Document("\n".join(lines) or "No matching links.", position), bypass_readonly=True
        )

    query.buffer.on_text_changed += lambda _: refresh()
    steer_list_from_query(bindings, query, choices)
    bind_list_paging(bindings, choices, has_focus(choices) | has_focus(query))

    @bindings.add("t", filter=has_focus(choices))
    def toggle_tools(event):
        nonlocal show_tools
        show_tools = not show_tools
        refresh()

    @bindings.add("/", filter=has_focus(choices))
    @bindings.add("c-f")
    def search(event):
        event.app.layout.focus(query)

    @bindings.add("enter", filter=has_focus(query), eager=True)
    def search_done(event):
        event.app.layout.focus(choices)

    @bindings.add("enter", filter=has_focus(choices), eager=True)
    def accept(event):
        url = selected_url()
        if url is not None:
            event.app.exit(result=url)

    @bindings.add("escape", eager=True)
    def escape(event):
        if event.app.layout.has_focus(query):
            query.text = ""
            event.app.layout.focus(choices)
        else:
            event.app.exit(result=None)

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    dialog = Dialog(
        title="Links",
        body=HSplit(
            [
                Label(
                    lambda: f"{len(visible)} links · Tools: {'shown' if show_tools else 'hidden'}",
                    dont_extend_height=True,
                ),
                query,
                choices,
                Label("↑↓ Select · PgUp/PgDn Page · Ctrl+U/D Half page"),
                Label("Enter open · t toggle tools · / search · Esc cancel"),
                Label("In search: Enter keeps filter · Esc clears filter"),
            ],
            padding=1,
        ),
        with_background=True,
    )
    refresh()
    return Application(
        layout=Layout(popup_container(dialog), focused_element=choices),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=popup_mouse(),
        input=input,
        output=output,
        style=popup_style(style),
    )
