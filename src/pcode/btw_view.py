"""The /btw thread view: a side thread drawn in the conversation's place.

With `btw_view swap`, opening a thread rebuilds the screen and the terminal's
scrollback from the thread instead of the conversation
(`TerminalOutput.open_view`), and closing it rebuilds the conversation, the way
a resize does. While it is shown the thread is ordinary scrollback, so the
terminal's own wheel scrolling and text selection work on it, and the main
editor, with everything it can do, sends follow-ups to it.

A turn still running carries on meanwhile: its writes wait, kept in the
transcript's log, and the rebuild that closes the view draws them. Its spinner
stays on the editor's border, and the side question's own row in the live
panel above the editor. Its warnings and errors flash there as well.

What is shown lives on `Activity.btw_thread`: the thread on screen, or None.

With `btw_view drawer` in a terminal that has one (`pcode.drawer`), the thread
is drawn in a drawer beside the terminal instead, and the screen keeps the
conversation; the editor still replies to the thread.
"""

from dataclasses import dataclass

from rich.text import Text

from pcode.aside import Aside
from pcode.conversation_tree import excerpt
from pcode.drawer import CLOSE
from pcode.markdown import Markdown
from pcode.terminal_text import safe_text

Entry = tuple[tuple, str, bool]

# What the editor takes as the shown thread's own commands (`PreviewApp.btw_submit`),
# offered first by the prompt's completion while a thread is shown.
THREAD_COMMANDS = (
    ("/merge", "Merge this thread into the conversation"),
    ("/summarize", "Send the model a summary of this thread: [focus]"),
    ("/stop", "Stop running side answers"),
    ("/close", "Back to the conversation"),
)


def threads(activity) -> list[list[Aside]]:
    """Each thread's questions in order, threads in the order they began."""
    grouped: dict[str, list[Aside]] = {}
    for aside in activity.asides:
        grouped.setdefault(aside.thread, []).append(aside)
    return list(grouped.values())


def current(activity) -> list[Aside]:
    """The thread on screen; empty while the conversation is, or once it was dropped.

    While it is not empty the editor sends to it rather than to the conversation.
    """
    thread = activity.btw_thread
    return [aside for aside in activity.asides if aside.thread == thread] if thread else []


def open_thread(activity, thread: str) -> None:
    activity.btw_thread = activity.btw_last = thread


def close(activity) -> None:
    activity.btw_thread = None


def _ordered(activity) -> list[str]:
    return [thread[0].thread for thread in threads(activity)]


def reopen_thread(activity) -> str | None:
    """Where Ctrl+Q goes from the conversation: the thread last shown, else the newest."""
    ordered = _ordered(activity)
    if activity.btw_last in ordered:
        return activity.btw_last
    return ordered[-1] if ordered else None


def position(activity) -> tuple[int, int]:
    """The thread on screen as (number, of), newest first; (0, n) while none is."""
    ordered = _ordered(activity)[::-1]
    shown = activity.btw_thread
    return (ordered.index(shown) + 1 if shown in ordered else 0), len(ordered)


def next_thread(activity) -> str | None:
    """Where Ctrl+\\ goes: the next older thread, wrapping from the oldest to the newest.

    None while no thread is shown, or the one shown is the only one.
    """
    number, count = position(activity)
    if not number or count < 2:
        return None
    return _ordered(activity)[::-1][number % count]


def entry(*objects) -> Entry:
    return (objects, "\n", False)


@dataclass
class _Written:
    """How much of one question and its answer is already on screen."""

    answer: str = ""
    done: bool = False


def question_blocks(aside: Aside, *, first: bool = True) -> list:
    """A question as a thread shows it, and the blank row its answer starts after."""
    from pcode.task_prompt import TaskPrompt
    from pcode.tool_display import literal

    blocks: list = [TaskPrompt(literal(aside.question))]
    where = " ".join(
        part
        for part in (
            f"on {aside.model}" if aside.model else "",
            f"at {aside.effort} effort" if aside.effort else "",
        )
        if part
    )
    # A follow-up runs where its thread began, so only the first says where.
    if where and first:
        blocks.append(Text(f"  {where}", style="dim"))
    blocks.append(Text(""))
    return blocks


def outcome_blocks(aside: Aside, answered: bool) -> list:
    """What follows a settled answer: why it stopped, or that there is none."""
    if aside.running:
        return []  # Its progress is the viewer's status row, not text here.
    if aside.error:
        return [Text(f"  {aside.status}: {aside.error}", style="dim")]
    if not answered:
        return [Text(f"  ({aside.status}; no answer)", style="dim")]
    return []


class ThreadWriter:
    """What of a thread is new since the last call, as entries to print.

    Answers go out a whole block at a time once settled, as the conversation's
    do. Shared by the swap (`ThreadView`) and the drawer (`pcode.btw_tail`).
    """

    def __init__(self) -> None:
        self._written: dict[str, _Written] = {}

    def reset(self) -> None:
        """Nothing is on screen: the next call writes the thread from the top."""
        self._written = {}

    def entries(self, thread: list[Aside], code_theme: str) -> tuple[list[Entry] | None, list[str]]:
        """New entries, or None if text already written changed (draw it all again);
        and the ids of answers now on screen whole, which this marks read.
        """
        from pcode.ui import settled_lines

        entries: list[Entry] = []
        seen: list[str] = []
        for number, aside in enumerate(thread):
            written = self._written.get(aside.id)
            if written is None:
                written = self._written[aside.id] = _Written()
                if number:
                    entries.append(entry())
                entries += [entry(block) for block in question_blocks(aside, first=not number)]
            answer = safe_text(aside.answer)
            if not answer.startswith(written.answer) or (written.done and answer != written.answer):
                return None, seen
            if written.done:
                continue
            rest = answer[len(written.answer) :]
            if aside.running:
                # As the conversation's answers do: whole blocks, once settled,
                # judged on whole lines (half an indent is not a blank line).
                complete = rest[: rest.rfind("\n") + 1]
                settled = "".join(complete.splitlines(keepends=True)[: settled_lines(complete)])
            else:
                settled = rest
            if settled.strip():
                entries += [entry(Markdown(settled.strip("\n"), code_theme=code_theme)), entry()]
            written.answer += settled
            if aside.running:
                # A follow-up waits for this answer, so nothing comes after it yet.
                break
            entries += [entry(block) for block in outcome_blocks(aside, bool(answer.strip()))]
            written.done = True
            if not aside.read:
                # On screen whole is read: the footer stops announcing it.
                aside.read = True
                seen.append(aside.id)
        return entries, seen


class ThreadView:
    """Keeps the screen on `Activity.btw_thread`, synced before every redraw."""

    def __init__(self, activity, transcript, key_label=lambda key: f"Ctrl+{key.upper()}") -> None:
        self.activity = activity
        self.transcript = transcript
        # How to press a prompt shortcut: Ctrl+Q, or a leader and `q`.
        self.key_label = key_label
        # Told the ids of answers once they are on screen whole, e.g. so a
        # session host stops announcing them to its other terminals.
        self.on_read = None
        # The session host whose drawer shows threads (`pcode.drawer`), or None
        # to draw them here; asked before every sync.
        self.drawer_host = lambda: None
        self._drawn: tuple[str, str | None] | None = None
        self._shown: str | None = None
        self.writer = ThreadWriter()

    def sync(self) -> None:
        """Open, switch, close or extend the view to match the thread on `Activity`."""
        output = self.transcript.output if self.transcript is not None else None
        if output is None or not self.transcript.console.is_terminal:
            return
        thread = current(self.activity)
        shown = thread[0].thread if thread else None
        host = self.drawer_host()
        if host is not None:
            self._sync_drawer(output, host, shown, thread)
            return
        if self._drawn is not None:
            # The drawer went (another session, say): close it, and draw here.
            self._drawn = None
            output.app.output.write_raw(CLOSE)
        # Judged against the screen too, not only the last call: whatever
        # left a view open, or replaced the output under one, this frame
        # puts the screen back where `Activity` says it is. (`open_view` keeps
        # the replay as `output.view`, so this settles after one call.)
        if shown is None:
            self._shown = None
            output.close_view(self.transcript.replay)
            return
        if shown != self._shown or output.view != self.replay:
            self._shown = shown
            output.open_view(self.replay)
            return
        entries = self._entries(thread)
        if entries is None:
            # Something already written changed under it: draw it again.
            output.open_view(self.replay)
        else:
            output.view_print(entries)

    def _sync_drawer(self, output, host: str, shown: str | None, thread: list[Aside]) -> None:
        """Point the drawer at the thread on `Activity`; the screen keeps the conversation."""
        from pcode.drawer import open_btw

        if output.view is not None:
            self._shown = None
            output.close_view(self.transcript.replay)
        drawn = (host, shown)
        if drawn != self._drawn:
            # Never opened, nothing to close: a terminal with no drawer open hears nothing.
            if shown is not None or self._drawn is not None:
                output.app.output.write_raw(open_btw(host, shown) if shown else CLOSE)
            self._drawn = drawn if shown is not None else None
        # Shown whole in the drawer is read, as on screen.
        seen = []
        for aside in thread:
            if not aside.running and not aside.read:
                aside.read = True
                seen.append(aside.id)
        self._report(seen)

    def replay(self) -> list[Entry]:
        """The whole view from the top, as the rebuild that opens or redraws it asks."""
        self.writer.reset()
        thread = current(self.activity)
        if not thread:
            return []
        return self._heading(thread) + (self._entries(thread) or [])

    def _heading(self, thread: list[Aside]) -> list[Entry]:
        first = thread[0]
        model = f" · {first.label}" if first.label else ""
        title = Text.assemble(
            (" btw ", "pcode.btw.badge"),
            (f" {excerpt(first.question)[:80]}", "pcode.btw"),
            (model, "pcode.muted"),
        )
        # Written once, so it names no position and offers Ctrl+\ only if
        # there was another thread then; the divider above the editor keeps up
        # as threads come and go. A key /unbind took away is not offered.
        back, after = self.key_label("q"), self.key_label("\\")
        cycle = "Esc back to the conversation"
        if back != "unbound":
            cycle = f"{back} or {cycle}"
        if position(self.activity)[1] > 1 and after != "unbound":
            cycle += f" · {after} next thread"
        keys = f"Enter replies here · {cycle} · /merge /summarize /copy /links /stop"
        return [entry(title), entry(Text(keys, style="pcode.muted")), entry()]

    def _entries(self, thread: list[Aside]) -> list[Entry] | None:
        """What of ``thread`` is new since the last call; None if written text changed."""
        entries, seen = self.writer.entries(thread, self.transcript.code_theme)
        self._report(seen)
        return entries

    def _report(self, seen: list[str]) -> None:
        if seen and self.on_read is not None:
            self.on_read(seen)
