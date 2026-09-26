"""The `/switch` chooser over running session hosts."""

import time
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Dialog, Label, TextArea

from pcode.host_protocol import HostEntry, code_fingerprint
from pcode.popup_ui import (
    bind_list_paging,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)

_TITLE_WIDTH = 40
STATES = {"working": "● working", "idle": "○ idle   ", "starting": "◌ starting"}
NEW = "✓ new    "


def unseen(entry: HostEntry) -> bool:
    """Finished while no terminal was showing it, and not looked at since."""
    return entry.unseen and entry.state != "working"


def ordered(entries: list[HostEntry]) -> list[HostEntry]:
    """Finished-and-unseen first, then working, then the rest; newest activity first in each."""

    def rank(entry: HostEntry) -> tuple[int, float]:
        group = 0 if unseen(entry) else 1 if entry.state == "working" else 2
        return group, -entry.updated

    return sorted(entries, key=rank)


def age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def host_row(
    entry: HostEntry, current: str | None, now: float | None = None, *, code: str | None = None
) -> str:
    """One line per host; `code` is the installed fingerprint, to flag hosts on older code."""
    now = time.time() if now is None else now
    title = " ".join(entry.label().split())
    if len(title) > _TITLE_WIDTH:
        title = title[: _TITLE_WIDTH - 1] + "…"
    marker = "▸" if entry.id == current else " "
    state = NEW if unseen(entry) else STATES.get(entry.state, entry.state)
    where = Path(entry.workspace).name
    old = " · old code" if code is not None and entry.stale(code) else ""
    return f"{marker} {state}  {title:<{_TITLE_WIDTH}}  {where} · {age(now - entry.updated)}{old}"


def hosts_dialog(
    entries: list[HostEntry], *, current: str | None, input=None, output=None, style=None
):
    """Returns ("attach", id), ("stop", id), ("new", None), or None when cancelled."""
    entries = ordered(entries)
    code = code_fingerprint()
    visible: list[HostEntry] = []
    query = TextArea(height=1, prompt="Search: ", multiline=False)
    choices = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
    choices.window.cursorline = Always()
    status = [""]
    armed: list[str | None] = [None]
    bindings = KeyBindings()

    def selected() -> HostEntry | None:
        row = choices.document.cursor_position_row
        return visible[row] if row < len(visible) else None

    def refresh():
        previous = selected()
        term = query.text.casefold()
        visible[:] = [
            entry
            for entry in entries
            if term in f"{entry.title} {entry.last_prompt} {entry.workspace} {entry.id}".casefold()
        ]
        index = next((i for i, entry in enumerate(visible) if entry is previous), 0)
        lines = [host_row(entry, current, code=code) for entry in visible]
        position = sum(len(line) + 1 for line in lines[:index])
        choices.buffer.set_document(
            Document("\n".join(lines) or "No matching sessions.", position), bypass_readonly=True
        )

    query.buffer.on_text_changed += lambda _: refresh()
    steer_list_from_query(bindings, query, choices)
    bind_list_paging(bindings, choices, has_focus(choices) | has_focus(query))

    @bindings.add("enter", eager=True)
    def accept(event):
        entry = selected()
        if entry is not None:
            event.app.exit(result=("attach", entry.id))

    @bindings.add("c-n")
    @bindings.add("n", filter=has_focus(choices))
    def new(event):
        event.app.exit(result=("new", None))

    @bindings.add("x", filter=has_focus(choices))
    @bindings.add("delete", filter=has_focus(choices))
    def stop(event):
        entry = selected()
        if entry is None:
            return
        if armed[0] == entry.id:
            event.app.exit(result=("stop", entry.id))
            return
        armed[0] = entry.id
        status[0] = (
            f"Press x again to stop {entry.id} ({entry.label()[:40]}). Its turn is cancelled."
        )

    @bindings.add("/", filter=has_focus(choices))
    @bindings.add("c-f")
    def search(event):
        event.app.layout.focus(query)

    @bindings.add("escape", eager=True)
    def escape(event):
        if event.app.layout.has_focus(query) and query.text:
            query.text = ""
        else:
            event.app.exit(result=None)

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    working = sum(entry.state == "working" for entry in entries)
    fresh = sum(unseen(entry) for entry in entries)
    dialog = Dialog(
        title="Sessions",
        body=HSplit(
            [
                Label(
                    f"{len(entries)} running · {working} working · {fresh} new · ▸ this terminal",
                    dont_extend_height=True,
                ),
                query,
                choices,
                Label(lambda: status[0], dont_extend_height=True),
                Label("Type to search · Enter switch · Ctrl+N new session · Tab list · Esc cancel"),
                Label("In the list: n new · x stop (twice) · / search"),
            ],
            padding=1,
        ),
        with_background=True,
    )
    refresh()
    return Application(
        layout=Layout(popup_container(dialog), focused_element=query),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=popup_mouse(),
        input=input,
        output=output,
        style=popup_style(style),
    )
