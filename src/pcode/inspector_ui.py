"""Temporary alternate-screen tool browser, separate from the inline editor."""

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, Label, TextArea

from pcode.inspection import ToolArchive
from pcode.popup_ui import popup_container, popup_style


class ToolInspector:
    def __init__(self, archive: ToolArchive, *, failed: bool = False, **app_options) -> None:
        self.archive = archive
        self.failed = failed
        self.tool = "All"
        self.names = ["All", *sorted({call.name for call in archive.calls})]
        self.visible = []
        self.selected = None
        self._refreshing = False
        self.query = TextArea(height=1, prompt="Search tools/commands: ", multiline=False)
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        self.detail = TextArea(read_only=True, wrap_lines=True, scrollbar=True)
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-d")
        def close(event):
            event.app.exit()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        @keys.add("f", filter=has_focus(self.list))
        def failures(event):
            self.failed = not self.failed
            self.refresh()

        @keys.add("t", filter=has_focus(self.list))
        def tool(event):
            self.tool = self.names[(self.names.index(self.tool) + 1) % len(self.names)]
            self.refresh()

        @keys.add("/", filter=has_focus(self.list))
        @keys.add("c-f")
        def search(event):
            event.app.layout.focus(self.query)

        @keys.add("enter", filter=has_focus(self.query))
        def search_done(event):
            event.app.layout.focus(self.list)

        header = Label(
            lambda: (
                f"Tool inspector · {len(self.visible)}/{len(self.archive.calls)} calls · "
                f"Status: {'Failed' if self.failed else 'All'} · Tool: {self.tool}"
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Calls", width=Dimension(weight=2)),
                Frame(self.detail, title="Details", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Calls", height=Dimension(min=3, max=8)),
                Frame(self.detail, title="Details"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root = HSplit(
            [
                header,
                self.query,
                body,
                Label("↑↓ Select/scroll · PgUp/PgDn Page · Tab Focus · Esc Close"),
                Label("In Calls: f Failures · t Tool filter · / Search · Ctrl+Home/End First/last"),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(root), focused_element=self.list),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def refresh(self) -> None:
        previous = self.selected
        query = self.query.text.casefold()
        self.visible = [
            call
            for call in reversed(self.archive.calls)
            if (not self.failed or call.state == "failed")
            and (self.tool == "All" or call.name == self.tool)
            and query in call.title().casefold()
        ]
        selected = next((i for i, c in enumerate(self.visible) if c is previous), 0)
        lines = [call.title().replace("\n", " ↵ ") for call in self.visible]
        text = "\n".join(lines) or "No matching tool calls."
        position = sum(len(line) + 1 for line in lines[:selected])
        self._refreshing = True
        self.list.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._refreshing = False
        self.select()

    def select(self) -> None:
        if self._refreshing:
            return
        row = self.list.document.cursor_position_row
        call = self.visible[row] if row < len(self.visible) else None
        if call is self.selected and call is not None:
            return
        self.selected = call
        text = call.details(self.archive.calls) if call else "No matching tool calls."
        self.detail.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self.detail.window.vertical_scroll = 0

    async def run(self) -> None:
        await self.app.run_async()
