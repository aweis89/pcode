"""Temporary link chooser for URLs seen in the conversation."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import ScrollingTextArea
from pcode.links import Link
from pcode.pick_ui import PickAction, PickItem
from pcode.popup_ui import (
    SearchDebounce,
    bind_list_paging,
    fit_width,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys

if TYPE_CHECKING:
    from pcode.popup_surface import PopupSurface

_ROW_WIDTH = 100
TITLE = "Links"
EMPTY = "No matching links."
TOGGLE_TOOLS = PickAction("t", "Show/hide tool links", needs_item=False)


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
    the side-answer viewer does. `/links` itself is a pick list (`link_items`).
    """

    def __init__(
        self,
        links: list[Link],
        on_pick: Callable[[str], None],
        on_cancel: Callable[[], None],
        *,
        shortcuts: PrefixKeys | None = None,
    ) -> None:
        self.links = links
        self.visible: list[Link] = []
        self.query = TextArea(height=1, prompt="Search: ", multiline=False)
        self.list = ScrollingTextArea(
            read_only=True,
            wrap_lines=False,
            scrollbar=True,
            # Fit every row, not just the filtered ones, so searching does not resize the box.
            width=fit_width([text for _, text in link_rows(links)]),
        )
        self.list.window.cursorline = Always()
        keys = self.key_bindings = KeyBindings()
        self.searching = SearchDebounce(self.query.buffer, self.refresh)
        steer_list_from_query(keys, self.query, self.list, self.searching)
        bind_list_paging(keys, self.list, has_focus(self.list) | has_focus(self.query))
        # Modal, so the bindings of whatever frames it cannot reach here.
        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        # The picker opens in the search line, so Enter picks from there too.
        @keys.add("enter", eager=True)
        def pick(event):
            self.searching.flush()
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
        return f"{len(self.visible)} links"

    def selected(self) -> str | None:
        row = self.list.document.cursor_position_row
        return self.visible[row].url if row < len(self.visible) else None

    def refresh(self) -> None:
        previous = self.selected()
        term = self.query.text.casefold()
        self.visible[:] = [
            link
            for link in reversed(self.links)
            if term in f"{link.url} {link.label} {link.source}".casefold()
        ]
        selected = next((i for i, link in enumerate(self.visible) if link.url == previous), 0)
        lines = [text for _, text in link_rows(self.visible)]
        position = sum(len(line) + 1 for line in lines[:selected])
        self.list.buffer.set_document(
            Document("\n".join(lines) or "No matching links.", position), bypass_readonly=True
        )


def link_items(links: list[Link]) -> list[PickItem]:
    """The `/links` rows, newest first, searched by URL, label and source."""
    newest = list(reversed(links))
    return [
        PickItem(url, text, search=f"{link.url}\n{link.label}\n{link.source}")
        for link, (url, text) in zip(newest, link_rows(newest))
    ]


def header(count: int, show_tools: bool) -> str:
    return f"{count} links · Tools: {'shown' if show_tools else 'hidden'}"


async def pick_link(
    surface: "PopupSurface", links: list[Link], message_links: list[Link], style=None
) -> str | None:
    """The URL picked on `surface`, or None.

    The tool-link toggle shows the other list in its place, keeping the search
    and the selection. `message_links` are collected apart from `links`: a URL
    first seen in a tool can also appear in a reply, and must survive hiding
    tools despite URL deduplication.
    """
    show_tools, query, selected = True, "", None
    while True:
        shown = links if show_tools else message_links
        picked = await surface.pick(
            link_items(shown),
            title=TITLE,
            header=header(len(shown), show_tools),
            empty=EMPTY,
            selected=selected,
            query=query,
            match="substring",
            actions=(TOGGLE_TOOLS,),
            style=style,
        )
        if picked is None:
            return None
        if picked.action != TOGGLE_TOOLS.key:
            return picked.id
        show_tools, query, selected = not show_tools, picked.query, picked.id
