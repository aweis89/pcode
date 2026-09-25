"""Popup reader for side questions, with an editor for follow-ups.

The browser never sends a request itself: a follow-up is handed to the `ask`
callback, which starts it the way `/btw` starts a question.
"""

import asyncio
from collections.abc import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Filter, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, Label, TextArea
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

from pcode.aside import Aside, Asides
from pcode.clipboard import copy as copy_to_clipboard
from pcode.popup_ui import (
    PopupInput,
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.session_ui import literal
from pcode.task_prompt import TaskPrompt
from pcode.tool_display import plain

# A streaming answer should look live without repainting the pane every token.
REFRESH_SECONDS = 0.3


def row(thread: list[Aside], width: int = 90) -> str:
    """One list line: the model if one was named, the first question, then its state."""
    first, newest = thread[0], thread[-1]
    question = plain(" ".join(first.question.split()), width)
    model = f"[{first.label}] " if first.label else ""
    more = len(thread) - 1
    follow_ups = f" · {more} follow-up{'s' if more > 1 else ''}" if more else ""
    return f"{model}{question}  ({newest.state()}{follow_ups})"


def exchange(aside: Aside, *, code_theme: str, first: bool = True) -> list:
    """One question and whatever of its answer has arrived."""
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
    if answer := literal(aside.answer):
        blocks.append(Markdown(answer, code_theme=code_theme))
    if aside.running:
        blocks.extend([Text(""), Text(f"  ({aside.activity or 'working'}…)", style="dim")])
    elif aside.error:
        blocks.extend([Text(""), Text(f"  {aside.status}: {aside.error}", style="dim")])
    elif not answer:
        blocks.append(Text(f"  ({aside.status}; no answer)", style="dim"))
    return blocks


class AsideBrowser:
    """Read side answers beside a conversation that may still be running.

    The list is one row per thread: a question and the follow-ups asked about
    it here. The pane shows the selected thread, streaming while an answer
    arrives. Opening a thread marks its answers read, which is what clears the
    footer's unread count.

    With `ask`, an editor under the pane sends follow-ups: `ask(thread,
    question)` starts one, or raises `ValueError` saying why it cannot yet.
    """

    def __init__(
        self,
        asides: Asides,
        *,
        ask: Callable[[str, str], None] | None = None,
        selected: str | None = None,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        **app_options,
    ) -> None:
        self.asides = asides
        self.ask = ask
        self.code_theme = code_theme
        self.threads = asides.threads()
        # `selected` names a question; the list selects the thread it is in.
        chosen = next((aside for aside in asides.items if aside.id == selected), None)
        if chosen is not None:
            self.selected: str | None = chosen.thread
        else:
            self.selected = self.threads[-1][0].thread if self.threads else None
        self._rendered: tuple | None = None
        self._refreshing = False
        self._ids: list[str] = []
        self.notice = ""
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        self.detail = RichPane(theme=rich_theme, color_system=color_system)
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        self.input = (
            PopupInput(
                self.follow_up,
                home=self.list,
                title=self.input_title,
                placeholder="Ask a follow-up about this answer…",
            )
            if ask is not None
            else None
        )
        # One-letter keys would otherwise fire instead of typing in the editor.
        browsing: Filter = self.input.browsing if self.input else Always()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys, paging=self.input.editing if self.input else None)
        bind_list_paging(keys, self.list, has_focus(self.list))

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("enter")
        def close(event):
            event.app.exit(result=None)

        @keys.add("c-k")
        def stop(event):
            # Same key meaning as elsewhere: stop the work, keep the record.
            self.asides.cancel()

        @keys.add("c", filter=browsing)
        def copy_answer(event):
            self.copy(event.app.output)

        if self.input is not None:

            @keys.add("r", filter=browsing)
            def reply(event):
                self.input.open(event.app)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        header = Label(
            lambda: (
                f"Side questions · {len(self.asides.items)} asked · "
                f"{self.asides.running} running · not part of the conversation"
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Questions", width=Dimension(weight=2)),
                Frame(self.detail, title="Answer", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(
                    self.list, title="Questions", height=lambda: list_pane_height(len(self.threads))
                ),
                Frame(self.detail, title="Answer"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                header,
                body,
                *([self.input] if self.input else []),
                Label(self.hints),
                Label(self.shortcuts),
            ]
        )
        self.app = Application(
            # Opens on the list, never the editor: the viewer can open by itself
            # when an answer lands, mid-keystroke at the main prompt.
            layout=Layout(popup_container(root_container), focused_element=self.list),
            key_bindings=keys,
            full_screen=True,
            mouse_support=popup_mouse(),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def editing(self) -> bool:
        return self.input is not None and self.app.layout.has_focus(self.input.area)

    def hints(self) -> str:
        if self.editing():
            return "Enter Send · Ctrl+J Newline · PgUp/PgDn Scroll answer"
        return "↑↓ Select/scroll · PgUp/PgDn Page · Ctrl+U/D Half page"

    def shortcuts(self) -> str:
        if self.editing():
            return "Esc Back to the list (keeps the draft) · Tab Focus · Ctrl+K Stop running"
        reply = "R Follow up · " if self.input else ""
        return f"Tab Focus · {reply}C Copy answer · Ctrl+K Stop running · Enter/Esc Close"

    def input_title(self) -> str:
        thread = self.current()
        label = thread[0].label if thread else ""
        return "Follow up" + (f" on {label}" if label else "")

    def refresh(self, *, force: bool = True) -> None:
        """Rebuild the list from the current records, keeping the selection."""
        self.threads = self.asides.threads()
        self._ids = [aside.id for aside in self.asides.items]
        lines = [row(thread) for thread in self.threads] or ["No side questions yet"]
        index = next(
            (i for i, thread in enumerate(self.threads) if thread[0].thread == self.selected),
            max(0, len(self.threads) - 1),
        )
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=force)

    def current(self) -> list[Aside]:
        """The selected thread's questions, oldest first; empty when there is none."""
        return next((thread for thread in self.threads if thread[0].thread == self.selected), [])

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row_index = self.list.document.cursor_position_row
        if row_index < len(self.threads):
            self.selected = self.threads[row_index][0].thread
        thread = self.current()
        for aside in thread:
            if not aside.running:
                aside.read = True
        state = (
            self.selected,
            tuple(
                (aside.id, aside.status, aside.answer, aside.activity, aside.error)
                for aside in thread
            ),
        )
        if state == self._rendered and not force:
            return
        switched = self._rendered is None or state[0] != self._rendered[0]
        grew = not switched and len(state[1]) > len(self._rendered[1])
        self._rendered = state
        blocks, newest = self.details(thread)
        if switched or grew:
            # A thread opens on its newest question, and a follow-up just
            # asked comes into view with its answer streaming below it.
            self.detail.set(blocks, anchor=newest)
        else:
            # A streaming repaint must not yank the reader away from where
            # they scrolled; one reading the tail keeps following it.
            self.detail.follow(blocks)

    def follow_up(self, question: str) -> None:
        """Ask `question` in the selected thread; `ValueError` says why not."""
        if self.ask is None or not self.selected:
            raise ValueError("No side question to follow up on")
        self.ask(self.selected, question)
        self.refresh()

    def copy(self, output=None) -> None:
        """Copy the thread's newest answer as written, even while it is still arriving."""
        answered = [aside for aside in self.current() if aside.answer]
        if not answered:
            self.notice = "No answer to copy"
            return
        copied, truncated = copy_to_clipboard(answered[-1].answer, output)
        limit = " (truncated)" if truncated else ""
        self.notice = f"Copied answer{limit}" if copied else "Could not copy answer"

    def details(self, thread: list[Aside]) -> tuple[list, int]:
        """The thread's questions and answers in order, and where the newest begins."""
        if not thread:
            return [Text("Ask one with /btw <question>.", style="dim")], 0
        blocks: list = []
        newest = 0
        for number, aside in enumerate(thread):
            if number:
                blocks.append(Text(""))
            newest = len(blocks)
            blocks.extend(exchange(aside, code_theme=self.code_theme, first=not number))
        return blocks, newest

    async def run(self) -> None:
        async def follow():
            # Poll rather than subscribe: the records are plain data, and a
            # timer keeps elapsed times and the running count moving too.
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                if [aside.id for aside in self.asides.items] != self._ids:
                    self.refresh()
                elif self.asides.running:
                    # List rows carry elapsed time and state; keep them moving,
                    # repainting the answer only when it changed.
                    self.refresh(force=False)
                else:
                    self.select()
                self.app.invalidate()

        def start():
            self.app.create_background_task(follow())

        await self.app.run_async(pre_run=start)


def aside_dialog(asides, **options) -> Application:
    return AsideBrowser(asides, **options).app
