"""Popup reader for side questions; never sends a request itself."""

import asyncio

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, Label, TextArea
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

from pcode.aside import Aside, Asides
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_style,
)
from pcode.session_ui import literal
from pcode.task_prompt import TaskPrompt
from pcode.tool_display import plain

# A streaming answer should look live without repainting the pane every token.
REFRESH_SECONDS = 0.3


def row(aside: Aside, width: int = 90) -> str:
    """One list line: the question, then how the answer is doing."""
    question = plain(" ".join(aside.question.split()), width)
    return f"{question}  ({aside.state()})"


class AsideBrowser:
    """Read side answers beside a conversation that may still be running.

    The list is every question this session asked; the pane shows the selected
    question and its answer, streaming while it arrives. Opening an answer marks
    it read, which is what clears the footer's unread count.
    """

    def __init__(
        self,
        asides: Asides,
        *,
        selected: str | None = None,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        **app_options,
    ) -> None:
        self.asides = asides
        self.code_theme = code_theme
        self.items = list(asides.items)
        self.selected = selected or (self.items[-1].id if self.items else None)
        self._rendered: tuple | None = None
        self._refreshing = False
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        self.detail = RichPane(theme=rich_theme, color_system=color_system)
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)
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

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        header = Label(
            lambda: (
                f"Side questions · {len(self.items)} asked · "
                f"{self.asides.running} running · not part of the conversation"
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
                    self.list, title="Questions", height=lambda: list_pane_height(len(self.items))
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
                Label("↑↓ Select/scroll · Tab Focus · Ctrl+K Stop running · Enter/Esc Close"),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(root_container), focused_element=self.list),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the list from the current records, keeping the selection."""
        self.items = list(self.asides.items)
        lines = [row(aside) for aside in self.items] or ["No side questions yet"]
        index = next(
            (i for i, aside in enumerate(self.items) if aside.id == self.selected),
            max(0, len(self.items) - 1),
        )
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=True)

    def current(self) -> Aside | None:
        return next((aside for aside in self.items if aside.id == self.selected), None)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row_index = self.list.document.cursor_position_row
        if row_index < len(self.items):
            self.selected = self.items[row_index].id
        aside = self.current()
        if aside is not None and not aside.running:
            aside.read = True
        state = (
            (aside.id, aside.status, aside.answer, aside.activity, aside.error)
            if aside
            else (None,)
        )
        if state == self._rendered and not force:
            return
        changed = self._rendered is None or state[0] != self._rendered[0]
        self._rendered = state
        info = self.detail.window.render_info
        offset = self.detail.window.vertical_scroll
        # Following an answer as it streams, unless the reader scrolled up.
        tailing = info is not None and offset >= max(0, info.content_height - info.window_height)
        self.detail.set(self.details(aside))
        if changed or info is None:
            return
        # A streaming repaint must not yank the reader back to the top.
        rows = len(self.detail.lines(info.window_width))
        self.detail.window.vertical_scroll = (
            max(0, rows - info.window_height) if tailing else offset
        )

    def details(self, aside: Aside | None) -> list:
        if aside is None:
            return [Text("Ask one with /btw <question>.", style="dim")]
        blocks: list = [TaskPrompt(literal(aside.question)), Text("")]
        if answer := literal(aside.answer):
            blocks.append(Markdown(answer, code_theme=self.code_theme))
        if aside.running:
            blocks.extend([Text(""), Text(f"  ({aside.activity or 'working'}…)", style="dim")])
        elif aside.error:
            blocks.extend([Text(""), Text(f"  {aside.status}: {aside.error}", style="dim")])
        elif not answer:
            blocks.append(Text(f"  ({aside.status}; no answer)", style="dim"))
        return blocks

    async def run(self) -> None:
        async def follow():
            # Poll rather than subscribe: the records are plain data, and a
            # timer keeps elapsed times and the running count moving too.
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                if len(self.asides.items) != len(self.items):
                    self.refresh()
                else:
                    self.select()
                self.app.invalidate()

        def start():
            self.app.create_background_task(follow())

        await self.app.run_async(pre_run=start)


def aside_dialog(asides, **options) -> Application:
    return AsideBrowser(asides, **options).app
