"""The `/attach` chooser over running session hosts, and stopped ones nobody has seen."""

import time
from pathlib import Path

from pcode.host_protocol import HostEntry
from pcode.pick_ui import PickAction, Picked, PickItem
from pcode.popup_ui import nest_forks

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


def _title(entry: HostEntry, guide: str = "") -> str:
    title = guide + " ".join(entry.label().split())
    return title[: _TITLE_WIDTH - 1] + "…" if len(title) > _TITLE_WIDTH else title


def _state(entry: HostEntry) -> str:
    return NEW if unseen(entry) else STATES.get(entry.state, entry.state)


def _detail(entry: HostEntry, now: float, code: str | None) -> str:
    """Where it runs and when it last did anything, and what is odd about it."""
    if entry.state == "stopped":
        flags = " · stopped"  # Enter starts it again on the code installed now.
    else:
        flags = " · old code" if code is not None and entry.stale(code) else ""
        if entry.held and not entry.attached:
            flags += f" · held: {entry.held}"
    return f"{Path(entry.workspace).name} · {age(now - entry.updated)}{flags}"


def host_row(
    entry: HostEntry,
    current: str | None,
    now: float | None = None,
    *,
    code: str | None = None,
    guide: str = "",
) -> str:
    """One line per host; `code` is the installed fingerprint, to flag hosts on older code.

    `guide` draws a fork under its original (`nested`), within the title's width.
    """
    now = time.time() if now is None else now
    marker = "▸" if entry.id == current else " "
    title = _title(entry, guide)
    return f"{marker} {_state(entry)}  {title:<{_TITLE_WIDTH}}  {_detail(entry, now, code)}"


def nested(entries: list[HostEntry]) -> list[tuple[HostEntry, str]]:
    """`entries`, in order, with each fork under the session it was copied from."""
    return nest_forks(
        entries,
        # A host with no session yet is nobody's original: its own ID keeps it apart.
        key=lambda entry: entry.session_id or entry.id,
        parent=lambda entry: entry.forked_from,
    )


def status_rows(entry: HostEntry, now: float | None = None, *, code: str | None = None):
    """`/status` rows about the host itself, read from its status file."""
    now = time.time() if now is None else now
    terminals = f"{entry.attached} terminal{'s' if entry.attached != 1 else ''} attached"
    rows = [("Host", f"{entry.id} · pid {entry.pid} · started {age(now - entry.started)}")]
    rows.append(("Attached", terminals))
    if entry.log:
        rows.append(("Host log", entry.log))
    if entry.stale(code):
        rows.append(("Host code", "older than the pcode installed now; /restart updates it"))
    return rows


TITLE = "Sessions"
NEW_SESSION = PickAction("n", "New session", needs_item=False)
STOP = PickAction("x", "Stop (twice)", confirm="stop it. Its turn is cancelled.", delete=True)


def host_items(
    entries: list[HostEntry],
    current: str | None,
    *,
    now: float | None = None,
    code: str | None = None,
) -> list[PickItem]:
    """The `/attach` rows: new, then working, then the rest, each fork under its original.

    Searched by title, last prompt, workspace and ID.
    """
    now = time.time() if now is None else now
    items = []
    for entry, guide in nested(ordered(entries)):
        label = entry.label()[:40]
        confirm = (
            f"remove {label} from this list. /resume still opens it."
            if entry.state == "stopped"
            else f"stop {entry.id} ({label}). Its turn is cancelled."
        )
        items.append(
            PickItem(
                entry.id,
                f"{_state(entry)}  {_title(entry)}",
                detail=_detail(entry, now, code),
                marker="this terminal" if entry.id == current else "",
                depth=len(guide) // 3,
                search="\n".join((entry.title, entry.last_prompt, entry.workspace, entry.id)),
                confirm=confirm,
            )
        )
    return items


def header(entries: list[HostEntry]) -> str:
    running = sum(entry.state != "stopped" for entry in entries)
    working = sum(entry.state == "working" for entry in entries)
    fresh = sum(unseen(entry) for entry in entries)
    return f"{running} running · {working} working · {fresh} new"


def empty(entries: list[HostEntry]) -> str:
    return "No matching sessions." if entries else "No background sessions are running."


def choice(picked: Picked | None) -> tuple[str, str | None] | None:
    """("attach", id), ("stop", id), ("new", None), or None when cancelled.

    For a stopped entry "attach" means resume it, and "stop" forget it.
    """
    if picked is None:
        return None
    if picked.action == NEW_SESSION.key:
        return "new", None
    if picked.id is None:
        return None
    return ("stop" if picked.action == STOP.key else "attach"), picked.id
