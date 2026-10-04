"""Temporary link chooser for URLs seen in the conversation."""

from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import Dialog
from pcode.links import Link
from pcode.popup_ui import (
    bind_list_paging,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys

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


class LinkPicker:
    """A search line over the links, newest first; Enter picks, Esc clears then backs out.

    ``container`` and ``key_bindings`` let a popup host it as an overlay, which
    the side-answer viewer does; ``links_dialog`` wraps it in an application of
    its own and adds the tool-link toggle.
    """

    def __init__(
        self,
        links: list[Link],
        on_pick: Callable[[str], None],
        on_cancel: Callable[[], None],
        *,
        message_links: list[Link] | None = None,
        toggles_tools: bool = False,
        shortcuts: PrefixKeys | None = None,
        footer: tuple = (),
    ) -> None:
        self.links = links
        # Collect message-only links separately: a URL first seen in a tool can also
        # appear in a reply, and must survive hiding tools despite URL deduplication.
        if message_links is None:
            message_links = [link for link in links if link.source in ("", "user", "assistant")]
        self.message_links = message_links
        self.toggles_tools = toggles_tools
        self.show_tools = True
        self.visible: list[Link] = []
        self.query = TextArea(height=1, prompt="Search: ", multiline=False)
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        keys = self.key_bindings = KeyBindings()
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        steer_list_from_query(keys, self.query, self.list)
        bind_list_paging(keys, self.list, has_focus(self.list) | has_focus(self.query))
        # Modal, so the bindings of whatever frames it cannot reach here.
        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        # The picker opens in the search line, so Enter picks from there too.
        @keys.add("enter", eager=True)
        def pick(event):
            url = self.selected()
            if url is not None:
                on_pick(url)

        @keys.add("escape", eager=True)
        def escape(event):
            if self.query.text and event.app.layout.has_focus(self.query):
                self.query.text = ""
            else:
                on_cancel()

        @keys.add("c-c")
        def cancel(event):
            on_cancel()

        self.container = HSplit(
            [
                Label(self.summary, dont_extend_height=True),
                self.query,
                self.list,
                *footer,
            ],
            padding=1,
            # Gated, so a waiting leader still owns Enter and Esc.
            key_bindings=shortcuts.gate(keys) if shortcuts else keys,
            modal=True,
        )
        self.refresh()

    def help(self) -> list[tuple[str, str]]:
        return [
            ("Type", "Search in the search field"),
            ("↑/↓", "Select link"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter", "Open selected link"),
            ("Tab/Shift+Tab", "Switch search / list"),
            ("Esc", "Clear search when focused, otherwise cancel"),
            ("Ctrl+C", "Cancel"),
        ]

    def summary(self) -> str:
        tools = f" · Tools: {'shown' if self.show_tools else 'hidden'}"
        return f"{len(self.visible)} links" + (tools if self.toggles_tools else "")

    def selected(self) -> str | None:
        row = self.list.document.cursor_position_row
        return self.visible[row].url if row < len(self.visible) else None

    def toggle_tools(self) -> None:
        self.show_tools = not self.show_tools
        self.refresh()

    def refresh(self) -> None:
        previous = self.selected()
        term = self.query.text.casefold()
        self.visible[:] = [
            link
            for link in reversed(self.links if self.show_tools else self.message_links)
            if term in f"{link.url} {link.label} {link.source}".casefold()
        ]
        selected = next((i for i, link in enumerate(self.visible) if link.url == previous), 0)
        lines = [text for _, text in link_rows(self.visible)]
        position = sum(len(line) + 1 for line in lines[:selected])
        self.list.buffer.set_document(
            Document("\n".join(lines) or "No matching links.", position), bypass_readonly=True
        )


def links_dialog(
    links: list[Link],
    *,
    message_links: list[Link] | None = None,
    input=None,
    output=None,
    style=None,
    key_prefix: str | None = None,
):
    """A popup application whose result is the picked URL, or None."""
    app: Application
    shortcuts = PrefixKeys(key_prefix)
    picker = LinkPicker(
        links,
        on_pick=lambda url: app.exit(result=url),
        on_cancel=lambda: app.exit(result=None),
        message_links=message_links,
        toggles_tools=True,
        shortcuts=shortcuts,
        footer=(Label(shortcuts.summary),),
    )
    shortcuts.set_help(picker.help)

    @shortcuts.add("f", "Search")
    def search(event):
        event.app.layout.focus(picker.query)

    @shortcuts.add("t", "Show/hide tool links")
    def toggle_tools(event):
        picker.toggle_tools()

    dialog = Dialog(title="Links", body=picker.container)
    app = Application(
        # Open in the search line, so typing filters straight away.
        layout=Layout(popup_container(dialog, shortcuts), focused_element=picker.query),
        key_bindings=shortcuts.key_bindings(KeyBindings()),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
    return app
