"""Pick one item from a searchable list (`PopupSurface.pick`).

The same list has two faces. In Ghostty Smooth's native overlay the terminal
draws it as a native list (`popup_surface`, protocol 2); everywhere else
`PickList` draws it with prompt_toolkit, on the full screen or in a terminal
overlay. Either way the caller only sees the result, a `Picked`, and keeps
every callback in Python.

Matching is shared, so both faces filter alike: the query is split into words
and every word must match one of an item's search texts, by `fuzzy_match` or
as a casefolded substring. Ghostty ports `fuzzy_match`, `tree_guides` and
`custom_item`; `tests/test_pick_ui.py` pins the cases both must agree on.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import Dialog
from pcode.popup_ui import SearchDebounce, fuzzy_match, popup_container, popup_mouse, popup_style
from pcode.prefix_keys import PrefixKeys

Match = Literal["fuzzy", "substring"]
MATCHES = ("fuzzy", "substring")
# The marker on the row a typed `custom` query makes.
CUSTOM_MARKER = "custom"
# Rows shown at once; the list scrolls past that.
_ROWS = 14
# Widest label column before details; longer labels are cut.
_LABEL_WIDTH = 60


@dataclass(frozen=True)
class PickItem:
    """One row of a pick list.

    `search` holds the texts a query word may match, one per line; empty
    means the label. `depth` nests the row under the nearest shallower row
    above it, drawn with tree guides only while the query is empty. `confirm`
    replaces a confirming action's text for this row.
    """

    id: str
    label: str
    detail: str = ""
    marker: str = ""
    depth: int = 0
    search: str = ""
    confirm: str = ""

    def texts(self) -> list[str]:
        return (self.search or self.label).casefold().split("\n")


@dataclass(frozen=True)
class PickAction:
    """A key the list answers besides Enter: the picker returns with `action` set.

    `key` is one lowercase letter: the shortcut chord in a terminal, Cmd+key
    natively. A non-empty `confirm` arms the action on the first press and
    shows "Press <key> again to <confirm>"; the second press on the same row
    picks. `delete` lets the Delete key do it too, while the query is empty.
    """

    key: str
    label: str
    needs_item: bool = True
    confirm: str = ""
    delete: bool = False


@dataclass(frozen=True)
class Picked:
    """What was picked: `id` is the row's (None for an action that needs none)."""

    id: str | None
    action: str | None = None
    query: str = ""


def key_hints(text: str, label: Callable[[str], str]) -> str:
    """`text` with each `{action:K}` replaced by how to press action K here (`label(K)`).

    So an empty list can say which key starts something, in a terminal
    (Ctrl+N) or natively (Cmd+N), which fills these in itself.
    """
    return re.sub(r"\{action:([a-z])\}", lambda match: label(match[1]), text)


def cut(text: str, width: int) -> str:
    """`text` cut to `width` cells, ending in … when cut."""
    if get_cwidth(text) <= width:
        return text
    kept, used = [], 0
    for char in text:
        size = get_cwidth(char)
        if used + size > width - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept) + "…"


def matches(item: PickItem, terms: Sequence[str], match: Match = "fuzzy") -> bool:
    """Every casefolded query word matches one of the item's texts."""
    texts = item.texts()
    if match == "fuzzy":
        return all(any(fuzzy_match(term, text) for text in texts) for term in terms)
    return all(any(term in text for text in texts) for term in terms)


def custom_item(query: str, items: Sequence[PickItem], custom: str | None) -> PickItem | None:
    """The row a typed query makes when `custom` (a regex) matches all of it."""
    query = query.strip()
    if not custom or not query or re.fullmatch(custom, query) is None:
        return None
    if any(item.id == query for item in items):
        return None
    return PickItem(query, query, marker=CUSTOM_MARKER)


def visible(
    items: Sequence[PickItem], query: str, match: Match = "fuzzy", custom: str | None = None
) -> list[PickItem]:
    """The rows a query leaves, in order, a custom row first."""
    terms = query.casefold().split()
    shown = [item for item in items if matches(item, terms, match)] if terms else list(items)
    extra = custom_item(query, items, custom)
    return [extra, *shown] if extra is not None else shown


def tree_guides(depths: Sequence[int]) -> list[str]:
    """Tree guides for rows listed depth-first, as `popup_ui.nest_forks` draws them.

    A row's depth is at most one more than the row above's (the first is 0).
    """
    levels: list[int] = []
    for depth in depths:
        levels.append(max(0, min(depth, levels[-1] + 1 if levels else 0)))

    def continues(index: int, level: int) -> bool:
        # Another row at `level` follows before the branch it belongs to ends.
        for later in levels[index + 1 :]:
            if later <= level:
                return later == level
        return False

    guides = []
    for index, depth in enumerate(levels):
        parts = []
        for level in range(1, depth + 1):
            more = continues(index, level)
            if level == depth:
                parts.append("├─ " if more else "└─ ")
            else:
                parts.append("│  " if more else "   ")
        guides.append("".join(parts))
    return guides


_HEX_COLOR = re.compile(r"[0-9a-fA-F]{6}")


def native_style(style=None) -> dict[str, str]:
    """The terminal list's own colors, as `#rrggbb`, for the native list to wear.

    Read from the merged style `PickList` draws with (`style` is the editor's):
    the selected row's background (`selected`) and its text (`accent`), and
    muted text (`dim`, as an empty list's text). A reversed row shows its
    text color as its background, so that is sent as `selected`, and its text,
    the terminal's background, is left to the native list. A color the
    terminal names rather than pcode (`/syntax terminal`'s ANSI names, plain
    `dim`) is left out: the native list then takes it from the terminal's own
    config, which is where those come from anyway.
    """
    merged = popup_style(style)
    colors: dict[str, str] = {}
    row = merged.get_attrs_for_style_str("class:popup class:selected")
    muted = merged.get_attrs_for_style_str("class:popup class:plan")
    if row.reverse:
        wanted = [("selected", row.color)]
    else:
        wanted = [("accent", row.color), ("selected", row.bgcolor)]
    if not muted.reverse:
        wanted.append(("dim", muted.color))
    for key, color in wanted:
        if color and _HEX_COLOR.fullmatch(color):
            colors[key] = "#" + color.lower()
    return colors


def payload(
    items: Sequence[PickItem],
    *,
    title: str,
    header: str = "",
    empty: str = "No matches.",
    selected: str | None = None,
    query: str = "",
    match: Match = "fuzzy",
    actions: Sequence[PickAction] = (),
    custom: str | None = None,
    cancel_keys: Sequence[str] = (),
    style: Mapping[str, str] | None = None,
) -> dict:
    """The list as the native picker reads it (see `popup_surface`).

    `style` is `native_style`'s colors, sent as given.
    """
    return {
        "title": title,
        "style": dict(style or {}),
        "cancel_keys": list(cancel_keys),
        "header": header,
        "empty": empty,
        "selected": selected,
        "query": query,
        "match": match,
        "custom": custom,
        "items": [
            {
                "id": item.id,
                "label": item.label,
                "detail": item.detail,
                "marker": item.marker,
                "depth": item.depth,
                # Casefolded here: Swift's case mapping is not Python's.
                "search": item.texts(),
                "confirm": item.confirm,
            }
            for item in items
        ],
        "actions": [
            {
                "key": action.key,
                "label": action.label,
                "needs_item": action.needs_item,
                "confirm": action.confirm,
                "delete": action.delete,
            }
            for action in actions
        ],
    }


def checked(
    reply: object,
    items: Sequence[PickItem],
    actions: Sequence[PickAction],
    custom: str | None,
) -> Picked | None:
    """A native picker's reply as a `Picked`, or None if it is not one this list can give."""
    if not isinstance(reply, dict):
        return None
    pick_id, action, query = reply.get("id"), reply.get("action"), reply.get("query", "")
    if not isinstance(query, str) or not (pick_id is None or isinstance(pick_id, str)):
        return None
    by_key = {entry.key: entry for entry in actions}
    if action is not None and action not in by_key:
        return None
    if pick_id is None:
        needs_item = action is None or by_key[action].needs_item
        return None if needs_item else Picked(None, action, query)
    known = any(item.id == pick_id for item in items)
    typed = custom_item(pick_id, items, custom)
    if not known and (typed is None or typed.id != pick_id):
        return None
    return Picked(pick_id, action, query)


class PickList:
    """The terminal's pick list: a search line over rows, a status line and a hint."""

    def __init__(
        self,
        items: Sequence[PickItem],
        *,
        title: str,
        header: str = "",
        empty: str = "No matches.",
        selected: str | None = None,
        query: str = "",
        match: Match = "fuzzy",
        actions: Sequence[PickAction] = (),
        custom: str | None = None,
        cancel_keys: Sequence[str] = (),
        input=None,
        output=None,
        style=None,
        key_prefix: str | None = None,
    ) -> None:
        self.items = list(items)
        self.match = match
        self.custom = custom
        self.empty = empty
        self.actions = list(actions)
        self.matches: list[PickItem] = []
        self.selected = 0
        self.status = ""
        self.armed: tuple[str, str] | None = None
        self._initial = selected
        self.search = TextArea(height=1, prompt="Search: ", multiline=False, text=query)
        self.search.buffer.cursor_position = len(query)
        self.searching = SearchDebounce(self.search.buffer, self.filter)
        guides = tree_guides([item.depth for item in self.items])
        self._guides = {id(item): guide for item, guide in zip(self.items, guides)}
        self.label_width = min(
            _LABEL_WIDTH,
            max(
                (get_cwidth(guide + item.label) for item, guide in zip(self.items, guides)),
                default=0,
            ),
        )
        self.rows = Window(
            FormattedTextControl(
                self.fragments, get_cursor_position=lambda: Point(x=0, y=self.selected)
            ),
            height=Dimension(min=3, max=_ROWS, preferred=_ROWS),
            # Fit every row, not just the filtered ones, so searching does not resize the box.
            width=Dimension(min=40, preferred=max(map(get_cwidth, self._all_lines()), default=0)),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        keys = KeyBindings()
        shortcuts = self.shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(self.help)

        @keys.add("up", eager=True)
        @keys.add("c-p", eager=True)
        def previous(event):
            self.move(-1)

        @keys.add("down", eager=True)
        @keys.add("c-n", eager=True)
        def following(event):
            self.move(1)

        @keys.add("pageup", eager=True)
        def previous_page(event):
            self.move(-self.page())

        @keys.add("pagedown", eager=True)
        def following_page(event):
            self.move(self.page())

        @keys.add("c-u", eager=True)
        def previous_half(event):
            self.move(-max(1, (self.page() + 1) // 2))

        @keys.add("c-d", eager=True)
        def following_half(event):
            self.move(max(1, (self.page() + 1) // 2))

        @keys.add("enter", eager=True)
        def accept(event):
            self.searching.flush()
            item = self.current()
            if item is not None:
                event.app.exit(result=Picked(item.id, None, self.search.text))

        @keys.add("escape", eager=True)
        def escape(event):
            if self.search.text:
                self.search.text = ""
            else:
                event.app.exit(result=None)

        @keys.add("c-c")
        def cancel(event):
            event.app.exit(result=None)

        # Such as the chord that opened the list (Ctrl+L for /model), to close it again.
        for key in cancel_keys:
            keys.add(key, eager=True)(cancel)

        for action in self.actions:
            shortcuts.add(action.key, action.label)(self._runner(action))
            if action.delete:
                empty_query = Condition(lambda: not self.search.text)
                keys.add("delete", filter=empty_query)(self._runner(action))

        self.app = Application(
            layout=Layout(
                popup_container(
                    Dialog(
                        title=title,
                        body=HSplit(
                            [
                                ConditionalContainer(
                                    Label(header, dont_extend_height=True),
                                    filter=Condition(lambda: bool(header)),
                                ),
                                self.search,
                                self.rows,
                                ConditionalContainer(
                                    Label(lambda: self.status, dont_extend_height=True),
                                    filter=Condition(lambda: bool(self.status)),
                                ),
                                Label(shortcuts.summary, dont_extend_height=True),
                            ],
                            padding=1,
                        ),
                    ),
                    shortcuts,
                ),
                focused_element=self.search,
            ),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            input=input,
            output=output,
            style=popup_style(style),
        )
        self.filter()

    def _runner(self, action: PickAction):
        def run(event) -> None:
            self.searching.flush()
            item = self.current()
            if action.needs_item and item is None:
                return
            if action.confirm and item is not None and self.armed != (action.key, item.id):
                self.armed = (action.key, item.id)
                self.status = (
                    f"Press {self.shortcuts.label(action.key)} again to "
                    f"{item.confirm or action.confirm}"
                )
                return
            event.app.exit(
                result=Picked(item.id if item is not None else None, action.key, self.search.text)
            )

        return run

    def help(self) -> list[tuple[str, str]]:
        rows = [
            ("Type", "Search"),
            ("↑/↓ / Ctrl+P/N", "Select"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter", "Choose the selected row"),
        ]
        rows += [("Delete", action.label) for action in self.actions if action.delete]
        rows += [
            ("Esc", "Clear the search, otherwise cancel"),
            ("Ctrl+C", "Cancel"),
        ]
        return rows

    def current(self) -> PickItem | None:
        return self.matches[self.selected] if self.selected < len(self.matches) else None

    def move(self, rows: int) -> None:
        self.searching.flush()
        self.select(max(0, min(len(self.matches) - 1, self.selected + rows)))

    def select(self, index: int) -> None:
        self.selected = index
        # An armed action stays armed only on its own row.
        current = self.current()
        if self.armed is not None and (current is None or current.id != self.armed[1]):
            self.armed, self.status = None, ""

    def page(self) -> int:
        """Rows actually visible, falling back before the first render."""
        info = self.rows.render_info
        return max(1, info.window_height - 1) if info is not None else 10

    def filter(self) -> None:
        previous = self.current()
        wanted = previous.id if previous is not None else self._initial
        self._initial = None
        self.matches = visible(self.items, self.search.text, self.match, self.custom)
        self.select(next((i for i, item in enumerate(self.matches) if item.id == wanted), 0))

    def line(self, item: PickItem, guide: str) -> str:
        label = cut(guide + item.label, self.label_width)
        text = label
        if item.detail:
            text += " " * (self.label_width - get_cwidth(label)) + "  " + item.detail
        if item.marker:
            text += f" · {item.marker}"
        return text

    def _all_lines(self) -> list[str]:
        return [f"› {self.line(item, self._guides[id(item)])}" for item in self.items]

    def fragments(self):
        if not self.matches:
            return [("class:plan", key_hints(self.empty, self.shortcuts.label))]
        nested = not self.search.text.strip()
        result = []
        for index, item in enumerate(self.matches):
            guide = self._guides.get(id(item), "") if nested else ""
            chosen = index == self.selected
            result.append(
                (
                    "class:selected" if chosen else "",
                    f"{'›' if chosen else ' '} {self.line(item, guide)}",
                    self._mouse(index),
                )
            )
            if index < len(self.matches) - 1:
                result.append(("", "\n"))
        return result

    def _mouse(self, index: int):
        """A click selects the row; the wheel moves the selection."""

        def handler(event: MouseEvent):
            if event.event_type == MouseEventType.MOUSE_UP:
                self.select(index)
            elif event.event_type == MouseEventType.SCROLL_UP:
                self.move(-1)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self.move(1)
            else:
                return NotImplemented
            return None

        return handler

    async def run(self) -> Picked | None:
        return await self.app.run_async()
