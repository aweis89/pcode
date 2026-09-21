"""Temporary alternate-screen tool browser, separate from the inline editor."""

import json

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, Label, TextArea
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode.inspection import InspectedCall, ToolArchive
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_style,
    steer_list_from_query,
)

STATE_STYLES = {"failed": "bold red", "succeeded": "bold green", "running": "bold yellow"}

# Argument keys whose values are code rather than prose, and the lexer for each.
CODE_KEYS = {"command": "bash", "content": None, "new_text": None, "old_text": None}


def parse_json(text: str) -> object:
    """The decoded payload when it is JSON, else None; captured payloads are often not."""
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def code_block(text: str, lexer: str | None, code_theme: str) -> Syntax:
    return Syntax(text, lexer or "text", theme=code_theme, word_wrap=True)


def heading(title: str) -> list:
    return [Text(""), Markdown(f"### {title}")]


def arguments_renderables(text: str, code_theme: str) -> list:
    """One row per argument; multi-line or code-like values get their own block."""
    parsed = parse_json(text)
    if not isinstance(parsed, dict) or not parsed:
        return [code_block(text, "json" if parsed is not None else None, code_theme)]
    blocks: list = []
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    later: list[tuple[str, str, str | None]] = []
    for key, value in parsed.items():
        if isinstance(value, str) and (key in CODE_KEYS or "\n" in value):
            later.append((key, value, CODE_KEYS.get(key)))
        else:
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            grid.add_row(key, Text(shown))
    if grid.row_count:
        blocks.append(grid)
    for key, value, lexer in later:
        blocks.append(Text(key, style="bold"))
        blocks.append(code_block(value, lexer, code_theme))
    return blocks


def result_renderables(text: str, code_theme: str) -> list:
    """JSON results get highlighting; everything else stays verbatim, never Markdown."""
    if parse_json(text) is not None:
        return [code_block(text, "json", code_theme)]
    return [Text(text)]


class ToolInspector:
    def __init__(
        self,
        archive: ToolArchive,
        *,
        failed: bool = False,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        **app_options,
    ) -> None:
        self.archive = archive
        self.failed = failed
        self.code_theme = code_theme
        self.tool = "All"
        self.names = ["All", *sorted({call.name for call in archive.calls})]
        self.visible = []
        self.selected = None
        self._refreshing = False
        self.query = TextArea(height=1, prompt="Search tools/commands: ", multiline=False)
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        self.detail = RichPane(theme=rich_theme, color_system=color_system)
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def close(event):
            event.app.exit()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        steer_list_from_query(keys, self.query, self.list)
        bind_list_paging(keys, self.list, has_focus(self.list) | has_focus(self.query))

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
                Frame(self.list, title="Calls", height=lambda: list_pane_height(len(self.visible))),
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
                Label("↑↓ Select/scroll · PgUp/PgDn Page · Ctrl+U/D Half page"),
                Label("Tab Focus · Esc Close"),
                Label("In Calls: f Failures · t Tool filter · / Search (↑↓ select while typing)"),
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
        self.detail.set(self.details(call))

    def details(self, call: InspectedCall | None) -> list:
        """Rich renderables for the Details pane: metadata grid, then each payload."""
        if call is None:
            return [Text("No matching tool calls.")]
        title = Text(call.name, style="bold")
        title.append(" · ")
        title.append(call.state, style=STATE_STYLES.get(call.state, "bold"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(overflow="fold")
        for label, value in call.metadata(self.archive.calls):
            grid.add_row(label, Text(value))
        return [
            title,
            grid,
            *heading("Arguments"),
            *arguments_renderables(call.arguments.read(), self.code_theme),
            *heading("Returned result / error"),
            *result_renderables(call.result.read(), self.code_theme),
            Text(""),
            Text(
                "Only captured tool output is shown; tool-side truncation cannot be recovered.",
                style="dim",
            ),
        ]

    async def run(self) -> None:
        await self.app.run_async()
