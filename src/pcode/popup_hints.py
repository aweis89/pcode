"""The key hints along every popup's bottom edge, decided here and nowhere else.

`POPUPS` says, for each kind of popup, which keys its hint bar lists and what
they read. Each popup only names its kind and the bits of state its entries
depend on; the entries, their wording and their order are this module's:

    primary (Enter) · the popup's own actions · navigation (Tab) · leave (Esc) · help

A shortcut's entry shows how it is pressed under the user's `key_prefix`
(Ctrl+Y, or Ctrl+P y after a leader) and only while that shortcut works.

Where the hints are drawn depends on the popup:

- `HintBar`, the last row of a prompt_toolkit popup, draws them as keycaps.
  On a Ghostty Smooth overlay that draws hint footers (popup protocol 4) it
  draws nothing and hands them to the terminal (`popup_surface.set_hints`),
  which draws them natively below the popup's terminal.
- A native pick list gets them in its payload (`pick_ui.payload`).
- The /btw drawer's viewer sends its own (`btw_tail`).

The terminal is handed `native_hints`: each keycap already as macOS writes it
(⌃Y, ⌘R, ↩, esc), so it only draws them.
"""

import base64
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import (
    AnyContainer,
    DynamicContainer,
    FormattedTextControl,
    HSplit,
    Window,
)
from prompt_toolkit.utils import get_cwidth

if TYPE_CHECKING:
    from pcode.prefix_keys import PrefixKeys


class Role(IntEnum):
    """Where a hint sits in the bar, and whether it may be dropped for room."""

    PRIMARY = 0
    ACTION = 1
    NAVIGATE = 2
    LEAVE = 3
    HELP = 4


# Kept however narrow the bar gets: how to act, how to leave, how to learn more.
KEPT = frozenset({Role.PRIMARY, Role.LEAVE, Role.HELP})


@dataclass(frozen=True)
class Hint:
    """One key and what it does. `key` reads as pcode writes keys: Enter, Esc, Ctrl+Y."""

    key: str
    label: str
    role: Role = Role.ACTION

    @property
    def kept(self) -> bool:
        return self.role in KEPT


@dataclass(frozen=True)
class Entry:
    """A registry line. `key` is a key name (Enter, Esc, Tab, a letter), or for a
    `shortcut` its letter, pressed as the popup's `PrefixKeys` says.

    `when` names a condition the popup reports (`HintBar(state=...)`); a leading
    `!` negates it. Unnamed conditions are false, so a popup that does not
    report one never shows its entry.
    """

    key: str
    label: str
    role: Role = Role.ACTION
    shortcut: bool = False
    when: str = ""


def enter(label: str, when: str = "") -> Entry:
    return Entry("Enter", label, Role.PRIMARY, when=when)


def esc(label: str, when: str = "") -> Entry:
    return Entry("Esc", label, Role.LEAVE, when=when)


def tab(label: str, when: str = "") -> Entry:
    return Entry("Tab", label, Role.NAVIGATE, when=when)


def key(name: str, label: str, when: str = "") -> Entry:
    """A plain key the popup binds itself, such as /agents' `a`."""
    return Entry(name, label, Role.ACTION, when=when)


def shortcut(letter: str, label: str, when: str = "", role: Role = Role.ACTION) -> Entry:
    return Entry(letter, label, role, shortcut=True, when=when)


# Every popup's hints. A pick list (`pick`) adds its own actions after Enter.
POPUPS: dict[str, tuple[Entry, ...]] = {
    "pick": (enter("Choose"), esc("Cancel")),
    # /btw in its own viewer (`aside_ui`).
    "btw": (
        # Its link picker, while open over it; its follow-up editor, while typing.
        enter("Open link", when="picking"),
        enter("Send", when="typing"),
        enter("Open", when="listing"),
        shortcut("r", "Reply"),
        shortcut("y", "Copy"),
        shortcut("o", "Links"),
        shortcut("t", "Merge"),
        shortcut("k", "Stop"),
        tab("Switch pane"),
        esc("Back"),
        shortcut("q", "Close", role=Role.LEAVE),
    ),
    # /btw in Ghostty Smooth's drawer, answered from pcode's editor (`btw_tail`).
    "btw_drawer": (
        enter("Reply in editor", when="!docked"),
        shortcut("\\", "Next thread", when="others"),
        esc("Close"),
    ),
    "config": (
        enter("Save", when="editing"),
        enter("Edit", when="!editing"),
        shortcut("t", "Scope", when="!editing"),
        shortcut("o", "Overrides", when="!editing"),
        shortcut("r", "Reset", when="!editing"),
        tab("Back", when="details"),
        tab("Details", when="!details"),
        esc("Cancel", when="editing"),
        esc("Close", when="!editing"),
    ),
    "agents": (
        key("a", "Answer"),
        key("m", "Message"),
        key("r", "Report"),
        shortcut("t", "Thinking"),
        esc("Close"),
    ),
    "jobs": (shortcut("w", "Watch"), shortcut("k", "Stop"), esc("Close")),
    "edits": (
        enter("Save note", when="noting"),
        shortcut("v", "View", when="!noting"),
        shortcut("n", "Note", when="!noting"),
        shortcut("a", "Reviewed", when="!noting"),
        shortcut("e", "Edit", when="!noting"),
        shortcut("f", "Search", when="!noting"),
        tab("Switch pane", when="!noting"),
        esc("Cancel", when="noting"),
        esc("Close", when="!noting"),
    ),
    "tools": (
        shortcut("f", "Search"),
        shortcut("x", "Failures"),
        shortcut("e", "Edits"),
        shortcut("t", "Tool"),
        shortcut("y", "Copy command"),
        esc("Close"),
    ),
    "tree": (
        # Its snippet picker (Ctrl+Y on a response with code), while open.
        enter("Copy", when="picking"),
        enter("Go to turn", when="navigable"),
        shortcut("y", "Copy"),
        tab("Switch pane"),
        esc("Cancel", when="navigable"),
        esc("Close", when="!navigable"),
    ),
    "copy": (enter("Copy"), esc("Cancel")),
    "resume": (
        enter("Resume"),
        shortcut("f", "Search"),
        shortcut("r", "Prompts/all"),
        shortcut("g", "All workspaces"),
        shortcut("x", "Delete"),
        esc("Close"),
    ),
    "session": (esc("Close"),),
    "secret": (enter("Submit"), esc("Cancel")),
}


def hints(
    kind: str,
    *,
    label: Callable[[str], str],
    available: Callable[[str], bool] = lambda _key: True,
    state: Mapping[str, Callable[[], bool]] | None = None,
    extra: Iterable[Hint] = (),
    help_hint: Hint | None = None,
) -> list[Hint]:
    """`kind`'s hints as they stand now, in the bar's order.

    `label` says how a shortcut letter is pressed, `available` whether it
    works now. `extra` are hints the popup adds (a pick list's actions), and
    `help_hint` how to open its key help, last.
    """
    state = state or {}

    def holds(when: str) -> bool:
        if not when:
            return True
        name = when.removeprefix("!")
        check = state.get(name)
        value = bool(check()) if check is not None else False
        return value != when.startswith("!")

    found = [
        Hint(label(entry.key) if entry.shortcut else entry.key, entry.label, entry.role)
        for entry in POPUPS[kind]
        if holds(entry.when) and (not entry.shortcut or available(entry.key))
    ]
    found.extend(extra)
    if help_hint is not None:
        found.append(help_hint)
    return ordered(found)


def ordered(found: Iterable[Hint]) -> list[Hint]:
    """By role, keeping each role's own order; a key listed twice keeps its first."""
    seen: set[str] = set()
    unique = []
    for hint in found:
        if hint.key not in seen:
            seen.add(hint.key)
            unique.append(hint)
    return sorted(unique, key=lambda hint: hint.role)


def shortcut_hints(shortcuts: "PrefixKeys", kind: str, **options) -> list[Hint]:
    """`hints` for a popup whose shortcuts are `shortcuts`, ending in its help key."""
    working = {shortcut.key for shortcut in shortcuts.available()}
    help_key, help_label = shortcuts.help_hint()
    return hints(
        kind,
        label=shortcuts.label,
        available=working.__contains__,
        help_hint=Hint(help_key, help_label, Role.HELP),
        **options,
    )


# --- Keycaps -------------------------------------------------------------------

_NATIVE_NAMES = {
    "Enter": "↩",
    "Esc": "esc",
    "Tab": "tab",
    "Delete": "⌦",
    "Space": "space",
}


def native_keycap(key: str) -> str:
    """`key` as a macOS keycap: Ctrl+Y is ⌃Y, Cmd+R ⌘R, Enter ↩."""
    if key in _NATIVE_NAMES:
        return _NATIVE_NAMES[key]
    return re.sub(r"Ctrl\+", "⌃", key).replace("Cmd+", "⌘").replace("Shift+", "⇧")


def terminal_keycap(key: str) -> str:
    """`key` as a terminal hint writes it: Ctrl+Y is ^Y, the rest as named."""
    return key.replace("Ctrl+", "^")


# --- The terminal's copy (`pcode;hints;…`) ----------------------------------------

# Bounds the terminal holds a hint list to as well.
MAX_HINTS = 12
KEY_MAX = 16
LABEL_MAX = 40
# Ghostty Smooth drops a longer APC whole (`unknown_max_bytes`, 4096).
APC_MAX = 4000


def native_hints(found: Sequence[Hint]) -> list[dict]:
    """`found` as the terminal draws them: keycap, label, and whether it stays when narrow."""
    return [
        {
            "key": native_keycap(hint.key)[:KEY_MAX],
            "label": hint.label[:LABEL_MAX],
            "keep": hint.kept,
        }
        for hint in found[:MAX_HINTS]
    ]


def hints_apc(found: Sequence[Hint]) -> bytes:
    """`pcode;hints;<base64 JSON>`: the overlay's hint footer, or none when empty.

    Hints go from the end until it fits the terminal's limit.
    """
    shown = list(found[:MAX_HINTS])
    while True:
        data = json.dumps(native_hints(shown), ensure_ascii=False, separators=(",", ":"))
        apc = b"\x1b_pcode;hints;" + base64.b64encode(data.encode("utf-8")) + b"\x1b\\"
        if len(apc) <= APC_MAX or not shown:
            return apc
        shown.pop()


# --- prompt_toolkit's bar ------------------------------------------------------

GAP = "  "


def fit(found: Sequence[Hint], width: int) -> list[Hint]:
    """`found` cut to `width` cells, dropping the last droppable hint first."""
    shown = list(found)
    while shown and bar_width(shown) > width:
        droppable = [index for index, hint in enumerate(shown) if not hint.kept]
        if not droppable:
            break
        del shown[droppable[-1]]
    return shown


def bar_width(found: Sequence[Hint]) -> int:
    return sum(get_cwidth(f"{terminal_keycap(h.key)} {h.label}") for h in found) + get_cwidth(
        GAP
    ) * max(0, len(found) - 1)


def fragments(found: Sequence[Hint]) -> StyleAndTextTuples:
    result: StyleAndTextTuples = []
    for index, hint in enumerate(found):
        if index:
            result.append(("class:hint", GAP))
        result.append(("class:hint.key bold", terminal_keycap(hint.key)))
        result.append(("class:hint", f" {hint.label}"))
    return result


def _drawn_natively() -> bool:
    from pcode import theme
    from pcode.popup_surface import drawn_in_overlay

    return drawn_in_overlay() and theme.native_hints_supported()


class _FittedHints(FormattedTextControl):
    """The hints cut to the width the bar is given, not the terminal's."""

    def __init__(self, found: Callable[[], Sequence[Hint]]) -> None:
        self.found = found
        self._width = 80
        super().__init__(lambda: fragments(fit(self.found(), self._width)))

    def preferred_width(self, max_available_width: int) -> int:
        # Only what must stay: a bar never widens its popup for optional hints.
        return min(max_available_width, bar_width([h for h in self.found() if h.kept]))

    def create_content(self, width: int, height: int | None):
        self._width = width
        return super().create_content(width, height)


class HintBar:
    """A popup's last row: its hints, or on a native overlay the terminal's footer.

    `found` is read each render, so the bar follows the popup's state. With
    `gap` it keeps a blank row above itself, which it drops with itself on a
    native overlay: for a dialog whose rows are spaced apart (`padding=1`),
    where the bar goes after the spaced rows rather than among them.
    """

    def __init__(self, found: Callable[[], Sequence[Hint]], *, gap: bool = False) -> None:
        self.found = found
        line = Window(_FittedHints(lambda: self.found()), height=1, dont_extend_height=True)
        bar = HSplit([Window(height=1), line]) if gap else line
        handed_over = Window(height=0)

        def current() -> AnyContainer:
            # Asked at each render, as `frame.Dialog` sets the overlay's title.
            if not _drawn_natively():
                return bar
            from pcode.popup_surface import set_hints

            set_hints(self.found())
            return handed_over

        self.container = DynamicContainer(current)

    def __pt_container__(self) -> AnyContainer:
        return self.container


def popup_bar(shortcuts: "PrefixKeys", kind: str, *, gap: bool = False, **options) -> HintBar:
    """The `HintBar` of a popup with `shortcuts`, listing `kind`'s hints."""
    return HintBar(lambda: shortcut_hints(shortcuts, kind, **options), gap=gap)
