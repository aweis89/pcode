"""Temporary alternate-screen diff browser, separate from inline scrollback."""

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Frame, Label, TextArea

from pcode.edit_transcript import DiffLexer
from pcode.edits import edit_text
from pcode.popup_ui import list_pane_height, popup_container, popup_style
from pcode.runtime import EditCompleted

EMPTY = "No file edits in this conversation."
KEYS = "↑↓ File · PgUp/PgDn Scroll diff · Tab Focus · Ctrl+Home/End First/last · Esc Close"


def change_title(change: EditCompleted) -> str:
    return (
        f"{change.operation:9} {edit_text(change.path)} · +{change.added} −{change.removed}"
    ).replace("\n", " ↵ ")


def change_diff(change: EditCompleted) -> str:
    """Render one completed change exactly as the scrollback block describes it."""
    lines = [
        f"{change.operation.capitalize()} {edit_text(change.path)}"
        f" · +{change.added} −{change.removed}",
        "",
    ]
    if change.patch:
        lines.append(edit_text(change.patch))
        if change.truncated:
            lines.append("… additional diff rows omitted")
    if change.omitted:
        lines.append(f"Diff unavailable: {edit_text(change.omitted)}")
    return "\n".join(lines)


class EditBrowser:
    """Most of the screen is the diff; the file selector stays a small bottom pane."""

    def __init__(self, changes, *, code_theme: str = "monokai", **app_options) -> None:
        # Newest first, matching the tool inspector's ordering.
        self.changes = list(reversed(list(changes)))
        self.selected: EditCompleted | None = None
        self._refreshing = False
        self.files = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.files.window.cursorline = Always()
        self.diff = TextArea(
            read_only=True,
            wrap_lines=True,
            scrollbar=True,
            lexer=DiffLexer(code_theme),
        )
        self.files.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-d")
        def close(event):
            event.app.exit()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        # Scroll the diff from either pane: the file list keeps ↑↓ for selection.
        @keys.add("pagedown", filter=has_focus(self.files))
        def page_down(event):
            self.scroll(self.page())

        @keys.add("pageup", filter=has_focus(self.files))
        def page_up(event):
            self.scroll(-self.page())

        header = Label(
            lambda: (
                f"Edit diffs · {self.position()}/{len(self.changes)} changes · "
                f"{edit_text(self.selected.path) if self.selected else 'none'}"
            )
        )
        root = HSplit(
            [
                header,
                Label(KEYS),
                Frame(self.diff, title="Diff"),
                Frame(self.files, title="Files", height=list_pane_height(len(self.changes))),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(root), focused_element=self.files),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def position(self) -> int:
        return self.files.document.cursor_position_row + 1 if self.changes else 0

    def page(self) -> int:
        """Page by what is actually visible, falling back before the first render."""
        info = self.diff.window.render_info
        return max(1, info.window_height - 1) if info is not None else 10

    def scroll(self, rows: int) -> None:
        """Move the cursor, not vertical_scroll: an unfocused window re-centers it."""
        document = self.diff.document
        row = max(0, min(document.line_count - 1, document.cursor_position_row + rows))
        self.diff.buffer.cursor_position = document.translate_row_col_to_index(row, 0)

    def refresh(self) -> None:
        text = "\n".join(change_title(change) for change in self.changes) or EMPTY
        self._refreshing = True
        self.files.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self._refreshing = False
        self.select()

    def select(self) -> None:
        if self._refreshing:
            return
        row = self.files.document.cursor_position_row
        change = self.changes[row] if row < len(self.changes) else None
        if change is self.selected and change is not None:
            return
        self.selected = change
        text = change_diff(change) if change else EMPTY
        self.diff.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self.diff.window.vertical_scroll = 0

    async def run(self) -> None:
        await self.app.run_async()
